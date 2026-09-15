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
# Dump execution
# ----------------------------------------------------------------------------

@dataclass
class DumpRequest:
    connection: Connection
    ignore_tables: list[str]  # tables whose DATA we skip (structure kept)
    output_file: Path
    log_file: Path | None
    compress: bool = False


@dataclass
class DumpResult:
    returncode: int
    elapsed_seconds: float
    output_file: Path
    log_file: Path | None
    bytes_written: int


def _stream_size_watcher(
    path: Path,
    stop_event: threading.Event,
    on_tick: Callable[[int], None],
) -> None:
    while not stop_event.is_set():
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        on_tick(size)
        stop_event.wait(0.5)


def run_dump(
    request: DumpRequest,
    *,
    on_progress: Callable[[int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> DumpResult:
    """Run mysqldump in two passes: data (with ignores) + structure-only for ignored tables.

    on_progress(bytes_written): called ~2Hz with current output file size.
    on_phase(phase_name): called when transitioning between passes.
    """
    require_binary("mysqldump")
    conn = request.connection
    defaults = _write_defaults_file(conn)

    # Ensure output dir exists.
    request.output_file.parent.mkdir(parents=True, exist_ok=True)
    # Truncate the output before starting.
    request.output_file.write_text("")

    stop_event = threading.Event()
    watcher: threading.Thread | None = None
    if on_progress is not None:
        watcher = threading.Thread(
            target=_stream_size_watcher,
            args=(request.output_file, stop_event, on_progress),
            daemon=True,
        )
        watcher.start()

    start = time.monotonic()
    returncode = 0
    try:
        if on_phase:
            on_phase("data")

        base_args = [
            "mysqldump",
            f"--defaults-file={defaults}",
            "--single-transaction",
            "--quick",
            "--skip-lock-tables",
            "--no-tablespaces",
        ]
        for tbl in request.ignore_tables:
            base_args.append(f"--ignore-table={conn.database}.{tbl}")
        base_args.append(conn.database)

        log_cm = request.log_file.open("a") if request.log_file else contextlib.nullcontext(subprocess.DEVNULL)
        with request.output_file.open("ab") as out, log_cm as log:
            rc = _run_piped(base_args, out, log)
            returncode = rc
            if rc != 0:
                raise MySQLError(f"mysqldump data pass failed (rc={rc})")

            if request.ignore_tables:
                if on_phase:
                    on_phase("structure")
                struct_args = [
                    "mysqldump",
                    f"--defaults-file={defaults}",
                    "--no-data",
                    "--no-tablespaces",
                    conn.database,
                    *request.ignore_tables,
                ]
                rc = _run_piped(struct_args, out, log)
                returncode = rc
                if rc != 0:
                    raise MySQLError(f"mysqldump structure pass failed (rc={rc})")
    finally:
        stop_event.set()
        if watcher is not None:
            watcher.join(timeout=1)
        try:
            defaults.unlink()
        except OSError:
            pass

    elapsed = time.monotonic() - start

    # Optional gzip compression of the final file.
    final_path = request.output_file
    if request.compress:
        if on_phase:
            on_phase("compress")
        gz_path = final_path.with_suffix(final_path.suffix + ".gz")
        _gzip_file(final_path, gz_path)
        final_path.unlink()
        final_path = gz_path

    return DumpResult(
        returncode=returncode,
        elapsed_seconds=elapsed,
        output_file=final_path,
        log_file=request.log_file,
        bytes_written=final_path.stat().st_size if final_path.exists() else 0,
    )


def _run_piped(args: list[str], stdout_fp, log_fp) -> int:
    """Run a subprocess, stream stdout to file, stderr to log file.

    log_fp may be a writable file-like object or the `subprocess.DEVNULL`
    sentinel when logging is disabled.

    Returns the process exit code. SIGINT is propagated.
    """
    if hasattr(log_fp, "write"):
        log_fp.write(f"\n$ {' '.join(_shell_quote(a) for a in args)}\n")
        log_fp.flush()
    proc = subprocess.Popen(
        args,
        stdout=stdout_fp,
        stderr=log_fp,
    )
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise


def _shell_quote(arg: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=\-]+", arg):
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"


def _gzip_file(src: Path, dst: Path) -> None:
    import gzip

    with src.open("rb") as fin, gzip.open(dst, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
