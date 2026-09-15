"""O worker: tira o backup de verdade.

Consome a fila em série, um job por vez, porque dois dumps pesados brigando por
disco demoram mais que os dois em sequência. Cada execução passa pelos mesmos
estágios, e cada estágio vira uma linha no registro:

    produzir → enviar para cada destino → retenção → avisar

O artefato nasce no staging e só sai de lá quando todos os destinos escolhidos
receberam. Se algum falhar, a execução fica marcada como pendente e o arquivo
continua no staging para o tick reenfileirar só o envio, sem refazer o dump.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from . import archive, destinations, mysql
from .config import DestinationStore, JobStore, Settings, decrypt, staging_dir
from .models import (
    ArchiveFormat,
    FilesSource,
    Job,
    ManifestEntry,
    MySQLSource,
    Run,
    RunResult,
    SourceKind,
    Stage,
    StageRecord,
    StageState,
)
from .state import State


class Cancelado(Exception):
    """Sinal recebido: termina o que está fazendo e sai."""


@dataclass
class Resultado:
    run: Run
    ok: bool
    mensagem: str = ""


# ----------------------------------------------------------------------------
# Laço principal
# ----------------------------------------------------------------------------

def run_forever(*, intervalo: float = 5.0, uma_vez: bool = False) -> int:
    """Pega da fila e executa, até ser interrompido.

    O supervisord manda SIGTERM para parar. Aqui isso vira uma saída limpa
    depois do job atual, e não um dump cortado no meio.
    """
    parar = {"agora": False}

    def encerra(_sig, _frame):
        parar["agora"] = True

    signal.signal(signal.SIGTERM, encerra)
    signal.signal(signal.SIGINT, encerra)

    estado = State()
    try:
        while not parar["agora"]:
            item = estado.claim_next()
            if item is None:
                if uma_vez:
                    return 0
                time.sleep(intervalo)
                continue

            resultado = executa_item(item, estado)
            estado.finish_queue_item(item["id"], resultado.run.id if resultado.run else None)
            if uma_vez:
                return 0 if resultado.ok else 1
    finally:
        estado.close()
    return 0


def executa_item(item: dict, estado: State) -> Resultado:
    """Executa um item da fila: um job inteiro, ou só o reenvio pendente."""
    jobs = JobStore.load()
    job = jobs.get(item["job"])
    if job is None:
        run = Run(
            id=0, job=item["job"], started_at=dt.datetime.now(),
            finished_at=dt.datetime.now(), result=RunResult.FAILED,
            error_cause="o job não existe mais",
        )
        estado.insert_run(run)
        return Resultado(run, False, "job inexistente")

    if item.get("kind") == "upload_retry":
        return reenvia_pendentes(job, estado)
    return executa(job, estado, atrasado=bool(item.get("late")))


# ----------------------------------------------------------------------------
# Execução completa
# ----------------------------------------------------------------------------

def executa(job: Job, estado: State, *, atrasado: bool = False) -> Resultado:
    inicio = dt.datetime.now()
    run = Run(id=0, job=job.name, started_at=inicio, result=RunResult.RUNNING)
    run.log.append((inicio.strftime("%H:%M:%S"), "worker", f"execução de {job.name} iniciada"))
    estado.insert_run(run)

    pasta = staging_dir() / job.name / run.folder
    pasta.mkdir(parents=True, exist_ok=True)
    limite = inicio + dt.timedelta(minutes=job.timeout_minutes)

    try:
        _produz(job, run, pasta, limite)
        _escreve_manifest(run, pasta)
        _envia(job, run, pasta, estado)
    except mysql.MySQLError as exc:
        return _falha(run, estado, Stage.DUMP, exc, job)
    except archive.ArchiveError as exc:
        return _falha(run, estado, Stage.ARCHIVE, exc, job)
    except TimeoutError as exc:
        return _falha(run, estado, run.error_stage or Stage.DUMP, exc, job, limpa=pasta)
    except Exception as exc:  # noqa: BLE001 - o worker não pode morrer por um job
        return _falha(run, estado, Stage.UPLOAD, exc, job)

    run.finished_at = dt.datetime.now()
    run.duration = (run.finished_at - inicio).total_seconds()
    run.result = RunResult.LATE if atrasado else RunResult.OK

    if run.destinations_pending:
        run.result = RunResult.PENDING_UPLOAD
        run.retry_at = run.finished_at + dt.timedelta(hours=1)
    else:
        _retencao(job, run)
        _limpa_staging(job, pasta, run)

    run.log.append((run.finished_at.strftime("%H:%M:%S"), "ok", "execução concluída"))
    estado.update_run(run)
    _avisa(job, run)
    return Resultado(run, run.result in (RunResult.OK, RunResult.LATE))


def _produz(job: Job, run: Run, pasta: Path, limite: dt.datetime) -> None:
    """Gera o artefato no staging, comprimindo em fluxo."""
    if job.kind is SourceKind.MYSQL:
        _dump(job, run, pasta, limite)
    else:
        _arquiva(job, run, pasta, limite)


def _dump(job: Job, run: Run, pasta: Path, limite: dt.datetime) -> None:
    fonte: MySQLSource = job.source  # type: ignore[assignment]
    conexao = mysql.Connection(
        host=fonte.host, port=fonte.port, user=fonte.user,
        password=decrypt(fonte.password_enc) or "", database=fonte.database,
    )
    tabelas = [t.name for t in mysql.list_tables_info(conexao)]
    por_regex, por_mao = fonte.resolve_ignored(tabelas)
    run.ignored_regex, run.ignored_manual = por_regex, por_mao
    ignoradas = sorted(set(por_regex) | set(por_mao))

    run.log.append((
        dt.datetime.now().strftime("%H:%M:%S"), "dump",
        f"{len(tabelas)} tabelas, {len(ignoradas)} sem dados",
    ))

    resultado = mysql.run_dump(
        mysql.DumpRequest(
            connection=conexao,
            ignore_tables=ignoradas,
            output_file=pasta / f"dump_{fonte.database}.sql",
            log_file=pasta / "dump.log",
            compress=True,
        ),
        on_progress=lambda _b: _checa_prazo(limite),
    )
    run.artifact = resultado.output_file.name
    run.bytes = resultado.bytes_written
    run.stages.append(StageRecord(
        Stage.DUMP, StageState.DONE, "dump",
        f"{len(tabelas) - len(ignoradas)} tabelas com dados", resultado.elapsed_seconds,
    ))
    run.stages.append(StageRecord(
        Stage.COMPRESS, StageState.DONE, "gzip",
        f"{_pct(resultado.ratio)} menor, em fluxo", 0.0,
    ))
    run.log.append((
        dt.datetime.now().strftime("%H:%M:%S"), "dump",
        f"{_tam(resultado.bytes_raw)} crus viraram {_tam(resultado.bytes_written)}",
    ))


def _arquiva(job: Job, run: Run, pasta: Path, limite: dt.datetime) -> None:
    fonte: FilesSource = job.source  # type: ignore[assignment]
    resultado = archive.create(
        archive.ArchiveRequest(
            source=Path(fonte.path),
            output_file=pasta / Path(fonte.path).name,
            excludes=fonte.active_excludes(),
            follow_links=fonte.follow_links,
            format=fonte.archive_format.value,
        ),
        on_progress=lambda _a, _b: _checa_prazo(limite),
    )
    run.artifact = resultado.output_file.name
    run.bytes = resultado.bytes_written
    run.stages.append(StageRecord(
        Stage.ARCHIVE, StageState.DONE, "leitura",
        f"{_plural(resultado.files, 'arquivo')}, {_tam(resultado.bytes_raw)}", resultado.elapsed_seconds,
    ))
    run.stages.append(StageRecord(
        Stage.COMPRESS, StageState.DONE, fonte.archive_format.value,
        f"{_tam(resultado.bytes_written)} finais, {_pct(resultado.ratio)} menor", 0.0,
    ))
    run.log.append((
        dt.datetime.now().strftime("%H:%M:%S"), "leitura",
        f"{_plural(resultado.files, 'arquivo')}"
        + (f", {resultado.skipped} fora pelas exclusões" if resultado.skipped else ""),
    ))


def _checa_prazo(limite: dt.datetime) -> None:
    if dt.datetime.now() > limite:
        raise TimeoutError("o job passou do tempo limite")


# ----------------------------------------------------------------------------
# Envio
# ----------------------------------------------------------------------------

def _envia(job: Job, run: Run, pasta: Path, estado: State) -> None:
    destinos = DestinationStore.load()
    prefixo = f"{job.name}/{run.folder}"

    for ligacao in job.destinations:
        destino = destinos.get(ligacao.name)
        if destino is None or not destino.enabled:
            run.stages.append(StageRecord(
                Stage.UPLOAD, StageState.SKIPPED, ligacao.name,
                "destino ausente ou desativado",
            ))
            run.destinations_pending.append(ligacao.name)
            continue

        inicio = time.monotonic()
        try:
            motor = destinations.backend(destino)
            enviados = motor.upload(pasta, prefixo)
        except Exception as exc:  # noqa: BLE001
            run.stages.append(StageRecord(
                Stage.UPLOAD, StageState.FAILED, ligacao.name,
                destinations._resumo_erro(exc), time.monotonic() - inicio,
            ))
            run.destinations_pending.append(ligacao.name)
            run.error_stage = Stage.UPLOAD
            run.error_tried = f"enviar para {destino.location()}"
            run.error_got = destinations._resumo_erro(exc)
            run.error_cause = _causa(destino, exc)
            run.error_fix = "abra o destino, teste, e use retry quando resolver"
            run.log.append((
                dt.datetime.now().strftime("%H:%M:%S"), ligacao.name, "falhou no envio",
            ))
            continue

        run.destinations_done.append(ligacao.name)
        run.manifest.append(ManifestEntry(
            destination=ligacao.name,
            sha256=_sha256(pasta / (run.artifact or "")),
            bytes=enviados,
        ))
        run.stages.append(StageRecord(
            Stage.UPLOAD, StageState.DONE, ligacao.name,
            destino.location(), time.monotonic() - inicio,
        ))
        run.log.append((
            dt.datetime.now().strftime("%H:%M:%S"), ligacao.name,
            f"{_tam(enviados)} enviados",
        ))
    estado.update_run(run)


def reenvia_pendentes(job: Job, estado: State) -> Resultado:
    """Tenta de novo os destinos que faltaram, usando o artefato do staging.

    Não refaz o dump: o arquivo já existe, e refazer custaria uma leitura nova
    do banco de produção por um problema que é de rede.
    """
    pendentes = [r for r in estado.pending_uploads() if r.job == job.name]
    if not pendentes:
        run = Run(id=0, job=job.name, started_at=dt.datetime.now(),
                  finished_at=dt.datetime.now(), result=RunResult.OK,
                  error_cause="nada pendente")
        return Resultado(run, True, "nada a reenviar")

    run = pendentes[0]
    pasta = staging_dir() / job.name / run.folder
    if not pasta.is_dir():
        run.result = RunResult.FAILED
        run.error_cause = "o artefato não está mais no staging"
        run.error_fix = f"rode o job de novo: backup-runner run {job.name}"
        estado.update_run(run)
        return Resultado(run, False, "artefato sumiu")

    faltando = list(run.destinations_pending)
    run.destinations_pending = []
    run.retry_count += 1
    destinos = DestinationStore.load()
    prefixo = f"{job.name}/{run.folder}"

    for nome in faltando:
        destino = destinos.get(nome)
        if destino is None:
            run.destinations_pending.append(nome)
            continue
        try:
            enviados = destinations.backend(destino).upload(pasta, prefixo)
        except Exception as exc:  # noqa: BLE001
            run.destinations_pending.append(nome)
            run.error_got = destinations._resumo_erro(exc)
            continue
        run.destinations_done.append(nome)
        run.manifest.append(ManifestEntry(nome, _sha256(pasta / (run.artifact or "")), enviados))
        run.stages.append(StageRecord(Stage.UPLOAD, StageState.DONE, nome, "reenviado", 0.0))
        run.log.append((dt.datetime.now().strftime("%H:%M:%S"), nome, "reenviado"))

    if run.destinations_pending:
        run.retry_at = dt.datetime.now() + dt.timedelta(hours=1)
        estado.update_run(run)
        _avisa(JobStore.load().get(job.name) or job, run)
        return Resultado(run, False, "ainda falta destino")

    run.result = RunResult.OK
    run.error_stage = None
    run.error_got = run.error_cause = run.error_fix = ""
    _retencao(job, run)
    _limpa_staging(job, pasta, run)
    estado.update_run(run)
    _avisa(job, run, recuperado=True)
    return Resultado(run, True, "reenvio completo")


# ----------------------------------------------------------------------------
# Depois do envio
# ----------------------------------------------------------------------------

def _retencao(job: Job, run: Run) -> None:
    """Apaga o que passou da idade, em cada destino, pela data na pasta.

    Só depois de o envio dar certo: apagar o antigo antes de o novo chegar é o
    jeito mais rápido de ficar sem backup nenhum.
    """
    destinos = DestinationStore.load()
    total = 0
    for ligacao in job.destinations:
        destino = destinos.get(ligacao.name)
        if destino is None or ligacao.name not in run.destinations_done:
            continue
        resultado = destinations.aplica_retencao(destino, job.name, ligacao.days(destino))
        total += len(resultado.apagadas)
        if resultado.apagadas:
            run.log.append((
                dt.datetime.now().strftime("%H:%M:%S"), "retenção",
                f"{ligacao.name}: {len(resultado.apagadas)} antigas apagadas",
            ))
    if total:
        run.stages.append(StageRecord(
            Stage.RETENTION, StageState.DONE, "retenção", f"{total} pastas antigas", 0.0,
        ))


def _limpa_staging(job: Job, pasta: Path, run: Run) -> None:
    """O staging é passagem, não cópia.

    O artefato some quando todos os destinos receberam, a menos que um dos
    destinos seja justamente uma pasta local, caso em que a cópia já está lá.
    """
    shutil.rmtree(pasta, ignore_errors=True)
    raiz = pasta.parent
    try:
        if raiz.is_dir() and not any(raiz.iterdir()):
            raiz.rmdir()
    except OSError:
        pass


def _escreve_manifest(run: Run, pasta: Path) -> None:
    """Hash e tamanho do que foi produzido, gravados junto do artefato."""
    artefato = pasta / (run.artifact or "")
    dados = {
        "job": run.job,
        "started_at": run.started_at.isoformat(timespec="seconds"),
        "folder": run.folder,
        "artifact": run.artifact,
        "bytes": artefato.stat().st_size if artefato.exists() else 0,
        "sha256": _sha256(artefato),
        "ignored_regex": run.ignored_regex,
        "ignored_manual": run.ignored_manual,
    }
    (pasta / "manifest.json").write_text(json.dumps(dados, indent=2, ensure_ascii=False))


def _sha256(caminho: Path) -> str:
    if not caminho.exists():
        return ""
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def _falha(run: Run, estado: State, estagio: Stage, exc: Exception, job: Job, *, limpa: Path | None = None) -> Resultado:
    run.finished_at = dt.datetime.now()
    run.duration = (run.finished_at - run.started_at).total_seconds()
    run.result = RunResult.FAILED
    run.error_stage = estagio
    run.error_got = destinations._resumo_erro(exc)
    if not run.error_cause:
        run.error_cause = type(exc).__name__
    run.stages.append(StageRecord(estagio, StageState.FAILED, estagio.value, run.error_got))
    run.log.append((run.finished_at.strftime("%H:%M:%S"), estagio.value, run.error_got))
    if limpa is not None:
        shutil.rmtree(limpa, ignore_errors=True)
    estado.update_run(run)
    _avisa(job, run)
    return Resultado(run, False, run.error_got)


def _avisa(job: Job, run: Run, *, recuperado: bool = False) -> None:
    from . import notify

    try:
        notify.sobre_execucao(job, run, recuperado=recuperado)
    except Exception:  # noqa: BLE001 - aviso que falha não derruba o backup
        pass


def _causa(destino, exc: Exception) -> str:
    from .models import DestKind

    if destino.kind is DestKind.S3:
        return destinations._causa_s3(exc)
    if destino.kind is DestKind.SFTP:
        return destinations._causa_sftp(exc)
    return "não conseguiu escrever no destino"


def _plural(n: int, palavra: str) -> str:
    return f"{n} {palavra}" if n == 1 else f"{n} {palavra}s"


def _tam(n: int) -> str:
    from .format import format_bytes

    return format_bytes(n)


def _pct(fracao: float) -> str:
    return f"{fracao * 100:.0f}%".replace(".", ",")
