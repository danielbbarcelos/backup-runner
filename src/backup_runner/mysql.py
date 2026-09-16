"""Camada MySQL: wrappers dos binários `mysql` e `mysqldump`.

Copiado do mysql-dumper (`~/dev/labs/mysql`) e adaptado. O mysql-dumper
continua existindo e não é importado: os dois evoluem separados, e uma
mudança lá não pode quebrar um backup agendado aqui.

O que veio de lá e vale manter:

- a senha vai por `--defaults-file` com modo 600, nunca inline, então não
  aparece em `ps` para nenhum outro processo da máquina;
- o dump roda em duas passadas, dados com `--ignore-table` e depois só a
  estrutura das ignoradas, o que produz um arquivo restaurável mesmo sem os
  dados pesados;
- nenhum driver MySQL em Python, só os binários, o que mantém a instalação
  livre de extensão C.

O que mudou: a lista de tabelas agora traz linhas e tamanho (o wizard precisa
disso para a pessoa escolher o que ignorar sabendo o peso), a conexão devolve
a versão do servidor, e os nomes de banco são citados em vez de interpolados.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


# Default pattern used by `mysqldump_ignore_pattern.sh`:
# - ends with _YYYYMM
# - or ends with _YYYYMM_vX
DEFAULT_IGNORE_PATTERN = (
    r"_[0-9]{6}$|"
    r"_(19|20)[0-9]{2}(0[1-9]|1[0-2])(_v[0-9]+)?$"
)


# ----------------------------------------------------------------------------
# Connection record
# ----------------------------------------------------------------------------

@dataclass
class Connection:
    host: str
    port: int
    user: str
    password: str
    database: str


class MySQLError(Exception):
    pass


# ----------------------------------------------------------------------------
# Binary lookup
# ----------------------------------------------------------------------------

def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MySQLError(f"{name!r} não encontrado no PATH")
    return path


def ensure_clients() -> None:
    require_binary("mysql")
    require_binary("mysqldump")


# ----------------------------------------------------------------------------
# defaults-file helper
# ----------------------------------------------------------------------------

def _write_defaults_file(conn: Connection) -> Path:
    """Write a mode-600 defaults-file with [client] creds and return the path.

    Using --defaults-file hides the password from ps/proc listings that would
    otherwise leak it when using -p<password> inline.
    """
    fd, name = tempfile.mkstemp(prefix="mysqldumper-", suffix=".cnf")
    os.close(fd)
    path = Path(name)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    path.write_text(
        "[client]\n"
        f"host = {conn.host}\n"
        f"port = {conn.port}\n"
        f"user = {conn.user}\n"
        f"password = {conn.password}\n",
        encoding="utf-8",
    )
    return path


# ----------------------------------------------------------------------------
# Queries
# ----------------------------------------------------------------------------

def test_connection(conn: Connection, *, timeout: int = 10) -> tuple[bool, str]:
    """(ok, mensagem). Em caso de sucesso a mensagem é a versão do servidor."""
    require_binary("mysql")
    defaults = _write_defaults_file(conn)
    try:
        proc = subprocess.run(
            [
                "mysql",
                f"--defaults-file={defaults}",
                "--connect-timeout",
                str(timeout),
                "-N",
                "-e",
                "SELECT VERSION();",
                conn.database,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    finally:
        try:
            defaults.unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        return False, stderr or f"código de saída {proc.returncode}"
    return True, proc.stdout.strip() or "ok"


@dataclass
class TableInfo:
    name: str
    rows: int
    bytes: int
    is_view: bool = False


def list_tables(conn: Connection) -> list[str]:
    return [t.name for t in list_tables_info(conn)]


def list_tables_info(conn: Connection) -> list[TableInfo]:
    """Tabelas do banco com linhas e tamanho estimados.

    `TABLE_ROWS` e `DATA_LENGTH` do information_schema são estimativas do
    engine, não contagem exata. Servem para ordenar e para dar noção de peso na
    hora de escolher o que ignorar, que é o uso aqui; não servem para prometer
    o tamanho do dump.
    """
    require_binary("mysql")
    defaults = _write_defaults_file(conn)
    banco = _quote(conn.database)
    query = (
        "SELECT TABLE_NAME, IFNULL(TABLE_ROWS,0), "
        "IFNULL(DATA_LENGTH,0)+IFNULL(INDEX_LENGTH,0), TABLE_TYPE "
        "FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = {banco} "
        "ORDER BY TABLE_NAME;"
    )
    try:
        proc = subprocess.run(
            [
                "mysql",
                f"--defaults-file={defaults}",
                "-N",
                "-e",
                query,
            ],
            capture_output=True,
            text=True,
        )
    finally:
        try:
            defaults.unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        raise MySQLError(proc.stderr.strip() or f"mysql saiu com {proc.returncode}")

    tabelas: list[TableInfo] = []
    for linha in proc.stdout.splitlines():
        if not linha.strip():
            continue
        partes = linha.split("\t")
        if len(partes) < 4:
            continue
        nome, linhas_txt, bytes_txt, tipo = partes[0], partes[1], partes[2], partes[3]
        tabelas.append(TableInfo(
            name=nome.strip(),
            rows=_inteiro(linhas_txt),
            bytes=_inteiro(bytes_txt),
            is_view=tipo.strip() != "BASE TABLE",
        ))
    return tabelas


def _inteiro(texto: str) -> int:
    try:
        return int(texto.strip())
    except (TypeError, ValueError):
        return 0


def _quote(valor: str) -> str:
    """Cita um literal para o information_schema.

    O nome do banco vem do formulário, então interpolar direto abriria espaço
    para uma aspa solta quebrar a consulta.
    """
    return "'" + valor.replace("\\", "\\\\").replace("'", "\\'") + "'"


def match_pattern(tables: Iterable[str], pattern: str) -> list[str]:
    rx = re.compile(pattern)
    return [t for t in tables if rx.search(t)]


# ----------------------------------------------------------------------------
# Execução do dump
# ----------------------------------------------------------------------------

@dataclass
class DumpRequest:
    connection: Connection
    ignore_tables: list[str]   # tabelas cujos DADOS são pulados; a estrutura vai
    output_file: Path          # sem o .gz: a compressão acrescenta a extensão
    log_file: Path | None
    compress: bool = True
    compress_level: int = 6


@dataclass
class DumpResult:
    returncode: int
    elapsed_seconds: float
    output_file: Path
    log_file: Path | None
    bytes_written: int
    bytes_raw: int = 0         # quanto o mysqldump produziu antes de comprimir

    @property
    def ratio(self) -> float:
        if not self.bytes_raw:
            return 0.0
        return 1 - (self.bytes_written / self.bytes_raw)


def run_dump(
    request: DumpRequest,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> DumpResult:
    """Roda o mysqldump em duas passadas, comprimindo em fluxo.

    As duas passadas vêm do mysql-dumper: primeiro os dados com `--ignore-table`
    para as pesadas, depois só a estrutura das ignoradas. O arquivo final é
    restaurável mesmo sem os dados que foram deixados de fora.

    A compressão acontece no caminho, e não depois. Comprimir no fim exigiria
    espaço para o arquivo cru inteiro (num caso real, cinco gigabytes para
    chegar em dois), e o staging tem teto. Aqui o SQL cru nunca toca o disco.

    O preço do fluxo é que o código de saída do mysqldump não aparece sozinho:
    quem escreve o arquivo é o gzip, que termina feliz mesmo se a origem morreu
    no meio. Por isso os dois lados são verificados, e um dump interrompido vira
    erro em vez de um .gz pela metade que ninguém percebe até precisar dele.
    """
    require_binary("mysqldump")
    conn = request.connection
    defaults = _write_defaults_file(conn)

    destino = request.output_file
    if request.compress and destino.suffix != ".gz":
        destino = destino.with_suffix(destino.suffix + ".gz")
    destino.parent.mkdir(parents=True, exist_ok=True)

    contagem = {"cru": 0}
    stop_event = threading.Event()
    watcher: threading.Thread | None = None
    if on_progress is not None:
        watcher = threading.Thread(
            target=_stream_size_watcher,
            args=(destino, stop_event, on_progress, contagem),
            daemon=True,
        )
        watcher.start()

    inicio = time.monotonic()
    try:
        log_cm = request.log_file.open("a") if request.log_file else contextlib.nullcontext(None)
        with log_cm as log:
            abrir = _abre_saida(destino, request)
            with abrir as saida:
                if on_phase:
                    on_phase("dump")
                _passada(_args_dados(defaults, conn, request.ignore_tables), saida, log, contagem)

                if request.ignore_tables:
                    if on_phase:
                        on_phase("structure")
                    _passada(
                        _args_estrutura(defaults, conn, request.ignore_tables),
                        saida, log, contagem,
                    )
    finally:
        stop_event.set()
        if watcher is not None:
            watcher.join(timeout=1)
        try:
            defaults.unlink()
        except OSError:
            pass

    return DumpResult(
        returncode=0,
        elapsed_seconds=time.monotonic() - inicio,
        output_file=destino,
        log_file=request.log_file,
        bytes_written=destino.stat().st_size if destino.exists() else 0,
        bytes_raw=contagem["cru"],
    )


def _abre_saida(destino: Path, request: DumpRequest):
    """Arquivo de saída, comprimido ou não, aberto em binário."""
    if request.compress:
        import gzip

        return gzip.open(destino, "wb", compresslevel=request.compress_level)
    return destino.open("wb")


def _args_dados(defaults: Path, conn: Connection, ignoradas: list[str]) -> list[str]:
    args = [
        "mysqldump",
        f"--defaults-file={defaults}",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--no-tablespaces",
    ]
    for tabela in ignoradas:
        args.append(f"--ignore-table={conn.database}.{tabela}")
    args.append(conn.database)
    return args


def _args_estrutura(defaults: Path, conn: Connection, ignoradas: list[str]) -> list[str]:
    return [
        "mysqldump",
        f"--defaults-file={defaults}",
        "--no-data",
        "--no-tablespaces",
        conn.database,
        *ignoradas,
    ]


def _passada(args: list[str], saida, log, contagem: dict) -> None:
    """Uma chamada do mysqldump, escrita em fluxo no arquivo de saída.

    Lê em blocos de 1 MB e escreve direto: o processo do mysqldump nunca precisa
    caber na memória, e nem o SQL cru precisa caber no disco.
    """
    if log is not None:
        log.write(f"\n$ {' '.join(_shell_quote(a) for a in args)}\n")
        log.flush()

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=(log if log is not None else subprocess.DEVNULL),
        bufsize=0,
    )
    try:
        assert proc.stdout is not None
        while True:
            bloco = proc.stdout.read(1024 * 1024)
            if not bloco:
                break
            contagem["cru"] += len(bloco)
            saida.write(bloco)
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()

    codigo = proc.wait()
    if codigo != 0:
        # O gzip terminaria feliz de qualquer jeito; quem reclama é este check.
        raise MySQLError(
            f"mysqldump saiu com {codigo}"
            + (f", veja {log.name}" if log is not None and hasattr(log, "name") else "")
        )


def _stream_size_watcher(
    path: Path,
    stop_event: threading.Event,
    on_tick: Callable[[int, int], None],
    contagem: dict,
) -> None:
    """Avisa de fora quanto já saiu, em bytes crus e em bytes gravados.

    Os dois números são diferentes e ambos importam: o cru é o que dá para
    comparar com o tamanho do banco no information_schema, e portanto o que
    vira porcentagem; o gravado é o arquivo que está crescendo no disco.
    """
    while not stop_event.is_set():
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        on_tick(contagem["cru"], size)
        stop_event.wait(0.5)


def _shell_quote(arg: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=\-]+", arg):
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"
