"""Dados de demonstração, para ver a interface de pé antes do worker existir.

Reproduz o cenário dos artboards: quatro jobs em estados diferentes (ok,
atrasado, pausado, sem execução), quatro destinos e um histórico com falha,
reenvio pendente e execuções boas.

Escreve nos stores apontados por XDG_CONFIG_HOME e XDG_DATA_HOME, então rodar
com essas variáveis para um diretório temporário não encosta na configuração
real de ninguém.
"""
from __future__ import annotations

import datetime as dt
import random

from .config import DestinationStore, JobStore, Settings
from .models import (
    ArchiveFormat,
    Channel,
    Destination,
    DestKind,
    ExcludePattern,
    FilesSource,
    Job,
    JobDestination,
    ManifestEntry,
    MySQLSource,
    NotifyEvent,
    NotifyMatrix,
    Run,
    RunResult,
    Stage,
    StageRecord,
    StageState,
)
from .state import State


def populate() -> None:
    destinos = DestinationStore()
    destinos.destinations = {
        "local-var": Destination(
            name="local-var", kind=DestKind.LOCAL, path="/var/backups", retention_days=7,
        ),
        "spaces-nyc3": Destination(
            name="spaces-nyc3", kind=DestKind.S3, endpoint="nyc3.digitaloceanspaces.com",
            region="nyc3", bucket="acme-bkp", prefix="loja/", access_key="DO00XQ7YB4KZ9WEXAMPLE",
            retention_days=30,
        ),
        "nas-sftp": Destination(
            name="nas-sftp", kind=DestKind.SFTP, host="nas.local", port=22, user="backup",
            auth="key", private_key="~/.ssh/id_nas", remote_path="/volume1/bkp",
            retention_days=14,
        ),
        "s3-frio": Destination(
            name="s3-frio", kind=DestKind.S3, endpoint="s3.us-east-1.amazonaws.com",
            region="us-east-1", bucket="acme-frio", enabled=False, retention_days=365,
        ),
    }
    destinos.save()

    jobs = JobStore()

    loja = Job(
        name="loja_prod",
        source=MySQLSource(
            host="db-01.local", port=3306, user="backup", database="loja_prod",
            ignore_regex=r".*_log$|^tmp_",
            ignore_manual=["cart_log", "view_log_daily"],
        ),
        schedule="0 3 * * *",
        destinations=[
            JobDestination("local-var", 7),
            JobDestination("spaces-nyc3", 30),
            JobDestination("nas-sftp", 14),
        ],
        created="2026-08-02",
    )
    # Sucesso não avisa; falha e janela perdida avisam nos dois canais.
    loja.notify.set(NotifyEvent.SUCCESS, Channel.SLACK, True)
    loja.notify.set(NotifyEvent.RECOVERED, Channel.EMAIL, False)
    jobs.jobs["loja_prod"] = loja

    jobs.jobs["midia_uploads"] = Job(
        name="midia_uploads",
        source=FilesSource(
            path="/srv/app/storage/uploads",
            excludes=[
                ExcludePattern("*.tmp", True),
                ExcludePattern("cache/**", True),
                ExcludePattern("*.log", True),
                ExcludePattern(".git/**", False),
            ],
            archive_format=ArchiveFormat.TARGZ,
        ),
        schedule="30 3 * * *",
        destinations=[JobDestination("local-var"), JobDestination("spaces-nyc3")],
        created="2026-08-10",
    )

    jobs.jobs["crm_replica"] = Job(
        name="crm_replica",
        source=MySQLSource(host="db-02.local", user="backup", database="crm"),
        schedule="0 4 * * *",
        destinations=[JobDestination("local-var")],
        created="2026-08-20",
    )

    jobs.jobs["conta_legado"] = Job(
        name="conta_legado",
        source=MySQLSource(host="db-03.local", user="backup", database="legado"),
        schedule="0 5 * * *",
        enabled=False,
        paused_at="02/09",
        destinations=[JobDestination("local-var")],
        created="2026-07-15",
    )
    jobs.save()

    settings = Settings()
    settings.notify_global = NotifyMatrix.default_global()
    settings.smtp = {"host": "smtp.acme.com", "port": 587, "to": "ops@acme.com", "from": "backup@acme.com"}
    settings.slack = {"channel": "#backups", "webhook_enc": "demo"}
    settings.save()

    _historico()


def _historico() -> None:
    estado = State()
    estado.conn.execute("DELETE FROM runs")
    estado.clear_queue()
    hoje = dt.datetime.now().replace(second=0, microsecond=0)

    for dias in range(1, 12):
        base = (hoje - dt.timedelta(days=dias)).replace(hour=3, minute=0)
        if dias == 3:
            estado.insert_run(_falha_conexao("loja_prod", base))
        else:
            estado.insert_run(_sucesso("loja_prod", base,
                                       2_100_000_000 + random.randint(-6_000_000, 6_000_000),
                                       ["local-var", "spaces-nyc3", "nas-sftp"]))

        base_midia = base.replace(minute=30)
        if dias == 1:
            estado.insert_run(_pendente("midia_uploads", base_midia))
        else:
            estado.insert_run(_sucesso("midia_uploads", base_midia, 880_000_000,
                                       ["local-var", "spaces-nyc3"], arquivo=True))

        base_crm = base.replace(hour=4)
        if dias in (1, 5):
            estado.insert_run(_falha_conexao("crm_replica", base_crm, host="db-02.local"))
        elif dias == 2:
            estado.insert_run(_sucesso("crm_replica", base_crm, 1_500_000_000,
                                       ["local-var"], resultado=RunResult.LATE))
        else:
            estado.insert_run(_sucesso("crm_replica", base_crm, 1_480_000_000, ["local-var"]))
    estado.close()


def _sucesso(job: str, quando: dt.datetime, tamanho: int, destinos: list[str], *,
             arquivo: bool = False, resultado: RunResult = RunResult.OK) -> Run:
    duracao = 252.0 if not arquivo else 390.0
    primeiro = Stage.ARCHIVE if arquivo else Stage.DUMP
    rotulo = "leitura" if arquivo else "dump"
    detalhe = "12.481 arquivos, 2,9 GB" if arquivo else "186 tabelas"
    sha = f"{abs(hash((job, quando))) % (16**14):014x}"
    return Run(
        id=0,
        job=job,
        started_at=quando,
        finished_at=quando + dt.timedelta(seconds=duracao),
        result=resultado,
        bytes=tamanho,
        duration=duracao,
        artifact=f"{job}.{'tar.gz' if arquivo else 'sql.gz'}",
        stages=[
            StageRecord(primeiro, StageState.DONE, rotulo, detalhe, 158.0),
            StageRecord(Stage.COMPRESS, StageState.DONE, "gzip", "nível 6", 54.0),
            *[
                StageRecord(Stage.UPLOAD, StageState.DONE, d, "enviado", 6.0 + i * 12)
                for i, d in enumerate(destinos)
            ],
            StageRecord(Stage.RETENTION, StageState.DONE, "retenção", "1 pasta antiga apagada", 0.4),
        ],
        manifest=[ManifestEntry(d, sha, tamanho) for d in destinos],
        ignored_regex=[] if arquivo else [
            "access_log", "audit_log", "cache_log", "event_log", "import_log",
            "job_log", "mail_log", "payment_log", "request_log", "session_log",
        ],
        ignored_manual=[] if arquivo else ["cart_log", "view_log_daily"],
        destinations_done=destinos,
        log=[
            (quando.strftime("%H:%M:%S"), "tick", f"job {job} enfileirado"),
            ((quando + dt.timedelta(seconds=1)).strftime("%H:%M:%S"), "worker", "execução iniciada"),
            ((quando + dt.timedelta(seconds=1)).strftime("%H:%M:%S"), rotulo, detalhe),
            ((quando + dt.timedelta(seconds=158)).strftime("%H:%M:%S"), "gzip", "nível 6"),
            ((quando + dt.timedelta(seconds=duracao)).strftime("%H:%M:%S"), "ok", "execução concluída"),
        ],
    )


def _falha_conexao(job: str, quando: dt.datetime, host: str = "db-01.local") -> Run:
    return Run(
        id=0,
        job=job,
        started_at=quando,
        finished_at=quando + dt.timedelta(seconds=21),
        result=RunResult.FAILED,
        duration=21.0,
        stages=[StageRecord(Stage.DUMP, StageState.FAILED, "dump", "não conectou", 21.0)],
        error_stage=Stage.DUMP,
        error_tried=f"mysqldump em {host}:3306 com usuário backup",
        error_got=f"Can't connect to MySQL server on '{host}' (111)",
        error_cause="conexão",
        error_fix=f"confira se o {host} aceita conexão da sua rede",
        log=[
            (quando.strftime("%H:%M:%S"), "worker", "execução iniciada"),
            ((quando + dt.timedelta(seconds=21)).strftime("%H:%M:%S"), "dump", f"erro 2003 em {host}"),
        ],
    )


def _pendente(job: str, quando: dt.datetime) -> Run:
    tamanho = 880_000_000
    sha = "7c1e00000000009a"
    return Run(
        id=0,
        job=job,
        started_at=quando,
        finished_at=quando + dt.timedelta(seconds=390),
        result=RunResult.PENDING_UPLOAD,
        bytes=tamanho,
        duration=390.0,
        artifact=f"staging/{job}-{quando.strftime('%Y-%m-%d_%H-%M-%S')}/uploads.tar.gz",
        stages=[
            StageRecord(Stage.ARCHIVE, StageState.DONE, "leitura", "12.481 arquivos, 2,9 GB", 182.0),
            StageRecord(Stage.COMPRESS, StageState.DONE, "tar.gz", "840 MB finais", 171.0),
            StageRecord(Stage.UPLOAD, StageState.DONE, "local-var", "/var/backups/midia", 4.0),
            StageRecord(Stage.UPLOAD, StageState.FAILED, "spaces-nyc3", "403 em 3 tentativas", 33.0),
            StageRecord(Stage.UPLOAD, StageState.WAITING, "nas-sftp", "a fila parou aqui", None),
        ],
        manifest=[ManifestEntry("local-var", sha, tamanho)],
        destinations_done=["local-var"],
        destinations_pending=["spaces-nyc3", "nas-sftp"],
        error_stage=Stage.UPLOAD,
        error_tried="PUT multipart em nyc3.digitaloceanspaces.com/acme-bkp",
        error_got="SignatureDoesNotMatch: request signature we calculated does not match the signature you provided (HTTP 403)",
        error_cause="o secret do destino spaces-nyc3 mudou ou expirou",
        error_fix="abra os destinos, cole o secret novo e teste",
        retry_at=quando + dt.timedelta(hours=8),
        retry_count=1,
        log=[
            (quando.strftime("%H:%M:%S"), "worker", "execução iniciada"),
            ((quando + dt.timedelta(seconds=182)).strftime("%H:%M:%S"), "leitura", "12.481 arquivos"),
            ((quando + dt.timedelta(seconds=357)).strftime("%H:%M:%S"), "local", "gravado em /var/backups/midia"),
            ((quando + dt.timedelta(seconds=390)).strftime("%H:%M:%S"), "spaces", "403 na tentativa 3 de 3"),
        ],
    )
