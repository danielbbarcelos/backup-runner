"""O tick: chamado pelo cron a cada minuto, decide o que entra na fila.

É barato de propósito. Lê os jobs, compara com a última execução, escreve na
fila e sai. Nada de dump, nada de rede, nada que possa demorar: se o tick
travar, o cron acumula processos e o problema vira outro.

A chave única (job, janela, tipo) na fila é o que torna isso idempotente. O
tick pode rodar sessenta vezes por hora sem nunca duplicar a execução das 3h.

Janela perdida: cada job tem um `catch_up_window`. Dentro dela, a janela
atrasada ainda entra na fila, marcada como atrasada. Fora, é registrada como
perdida e o aviso sai, porque um backup de sábado tirado na segunda é só mais
uma cópia de segunda, não o dado de sábado.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import JobStore, Settings
from .models import Job, Run, RunResult
from .schedule import Cron, CronError
from .state import State


@dataclass
class TickResult:
    enfileirados: list[tuple[str, dt.datetime, bool]]
    perdidos: list[tuple[str, dt.datetime]]
    reenvios: list[str]
    pulados: list[str]

    def resumo(self) -> str:
        partes = []
        if self.enfileirados:
            partes.append(f"{len(self.enfileirados)} enfileirados")
        if self.perdidos:
            partes.append(f"{len(self.perdidos)} janelas perdidas")
        if self.reenvios:
            partes.append(f"{len(self.reenvios)} reenvios")
        return ", ".join(partes) or "nada a fazer"


def run_tick(agora: dt.datetime | None = None, *, state: State | None = None) -> TickResult:
    agora = (agora or dt.datetime.now()).replace(second=0, microsecond=0)
    jobs = JobStore.load()
    settings = Settings.load()
    estado = state or State()
    resultado = TickResult([], [], [], [])

    try:
        for job in jobs.list():
            if not job.enabled:
                resultado.pulados.append(job.name)
                continue
            _avaliar(job, agora, estado, resultado)
        _reenvios_pendentes(agora, settings, estado, resultado)
    finally:
        if state is None:
            estado.close()
    return resultado


def _avaliar(job: Job, agora: dt.datetime, estado: State, resultado: TickResult) -> None:
    try:
        cron = Cron.parse(job.schedule)
    except CronError:
        return

    janela = cron.prev_before(agora)
    if janela is None:
        return

    ultima = estado.last_run(job.name)
    # Uma janela já atendida não volta: o corte é o início da última execução.
    if ultima is not None and ultima.started_at >= janela:
        return

    ja_na_fila = any(
        f["job"] == job.name and f["kind"] == "full"
        for f in estado.queue_pending()
    )
    if ja_na_fila:
        return

    atraso = (agora - janela).total_seconds() / 60
    if atraso <= job.catch_up_window_minutes:
        estado.enqueue(job.name, janela, late=atraso > 1)
        resultado.enfileirados.append((job.name, janela, atraso > 1))
        return

    # Fora da janela de tolerância: registra como perdida, uma vez só.
    if _ja_registrou_perda(estado, job.name, janela):
        return
    perdida = Run(
        id=0,
        job=job.name,
        started_at=janela,
        finished_at=agora,
        result=RunResult.MISSED,
        error_cause=f"janela perdida, {int(atraso)} min de atraso passam dos "
                    f"{job.catch_up_window_minutes} aceitos",
    )
    estado.insert_run(perdida)
    resultado.perdidos.append((job.name, janela))


def _ja_registrou_perda(estado: State, job: str, janela: dt.datetime) -> bool:
    for run in estado.runs(job=job, results=[RunResult.MISSED], limit=20):
        if run.started_at == janela:
            return True
    return False


def _reenvios_pendentes(agora: dt.datetime, settings: Settings, estado: State, resultado: TickResult) -> None:
    """Artefato pronto no staging que ainda não chegou em todos os destinos.

    O reenvio não refaz o dump: o arquivo já existe, e refazer custaria uma
    nova leitura do banco de produção por um problema que é de rede.
    """
    for run in estado.pending_uploads():
        if run.retry_at is None or run.retry_at > agora:
            continue
        if run.retry_count >= 3:
            continue
        limite = run.started_at + dt.timedelta(hours=settings.staging_hold_hours)
        if agora > limite:
            continue
        if estado.enqueue(run.job, agora, kind="upload_retry") is not None:
            resultado.reenvios.append(run.job)
