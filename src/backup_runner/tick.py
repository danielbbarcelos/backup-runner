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
from dataclasses import dataclass, field

from .config import JobStore, Settings
from .models import Job, Run, RunResult, Stage
from .schedule import Cron, CronError
from .state import State


@dataclass
class TickResult:
    enfileirados: list[tuple[str, dt.datetime, bool]]
    perdidos: list[tuple[str, dt.datetime]]
    reenvios: list[str]
    pulados: list[str]
    abandonadas: list[tuple[int, str]] = field(default_factory=list)

    def resumo(self) -> str:
        partes = []
        if self.enfileirados:
            partes.append(f"{len(self.enfileirados)} enfileirados")
        if self.perdidos:
            partes.append(f"{len(self.perdidos)} janelas perdidas")
        if self.reenvios:
            partes.append(f"{len(self.reenvios)} reenvios")
        if self.abandonadas:
            partes.append(f"{len(self.abandonadas)} abandonadas")
        return ", ".join(partes) or "nada a fazer"


def run_tick(agora: dt.datetime | None = None, *, state: State | None = None) -> TickResult:
    agora = (agora or dt.datetime.now()).replace(second=0, microsecond=0)
    jobs = JobStore.load()
    settings = Settings.load()
    estado = state or State()
    resultado = TickResult([], [], [], [])

    try:
        _fecha_orfas(estado, resultado)
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


def _fecha_orfas(estado: State, resultado: TickResult) -> None:
    """Encerra execuções cujo worker morreu sem escrever o desfecho.

    Roda antes de enfileirar qualquer coisa, e é por isso que existe: enquanto
    a linha antiga diz "rodando", o job parece ocupado e o backup de hoje não
    entra na fila. Um worker morto em janeiro adiaria todo backup até alguém
    notar.

    O critério tem duas pernas, e as duas precisam ceder: silêncio longo no
    heartbeat e nenhum processo com aquele pid. Só o silêncio não basta, porque
    uma máquina suspensa deixa qualquer worker mudo sem que ele tenha morrido.
    """
    from .service import pid_vivo

    for run in estado.orfas():
        pid = estado.pid_de(run.id)
        if pid_vivo(pid):
            continue
        run.result = RunResult.FAILED
        run.finished_at = dt.datetime.now()
        run.error_stage = run.error_stage or Stage.DUMP
        run.error_cause = f"o worker (pid {pid or '?'}) morreu no meio da execução"
        run.error_fix = "veja os logs do worker e rode de novo: backup-runner run " + run.job
        run.log.append((
            run.finished_at.strftime("%H:%M:%S"), "tick",
            "execução sem sinal de vida, marcada como falha",
        ))
        estado.update_run(run)
        resultado.abandonadas.append((run.id, run.job))


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
