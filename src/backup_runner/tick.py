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

# Três tentativas de envio. Além disso é insistência: se três falharam, o
# problema não é o soluço de rede que a retentativa resolve.
MAX_REENVIOS = 3


@dataclass
class TickResult:
    enfileirados: list[tuple[str, dt.datetime, bool]]
    perdidos: list[tuple[str, dt.datetime]]
    reenvios: list[str]
    pulados: list[str]
    abandonadas: list[tuple[int, str]] = field(default_factory=list)
    filas_soltas: list[tuple[int, str]] = field(default_factory=list)
    desistencias: list[tuple[int, str]] = field(default_factory=list)

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
        if self.filas_soltas:
            partes.append(f"{len(self.filas_soltas)} filas presas soltas")
        if self.desistencias:
            partes.append(f"{len(self.desistencias)} pendentes encerradas")
        return ", ".join(partes) or "nada a fazer"


def run_tick(agora: dt.datetime | None = None, *, state: State | None = None) -> TickResult:
    agora = (agora or dt.datetime.now()).replace(second=0, microsecond=0)
    jobs = JobStore.load()
    settings = Settings.load()
    estado = state or State()
    resultado = TickResult([], [], [], [])

    try:
        # A ordem importa: soltar a fila presa antes de avaliar os jobs, senão
        # a linha fantasma ainda faz `ja_na_fila` descartar a janela deste tick.
        _solta_filas_presas(estado, resultado)
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


def _solta_filas_presas(estado: State, resultado: TickResult) -> None:
    """Libera itens de fila cujo worker morreu sem encerrar o item.

    Este é o defeito que fazia a ferramenta parar de tirar backup e continuar
    dizendo que estava tudo bem. A linha em 'running' bloqueava toda janela
    futura, não aparecia no `queue_size` e nem gerava aviso de janela perdida,
    porque o bloqueio acontecia antes dessa lógica.

    Critério é o pid, não a idade. Um envio legítimo de seis horas tem claim
    antigo e processo vivo, e tomar o item dele poria dois workers no mesmo
    backup, que é um problema pior que o original.
    """
    from .service import pid_vivo

    for item in estado.filas_presas():
        if pid_vivo(item.get("claimed_pid")):
            continue
        estado.solta_fila(item["id"])
        resultado.filas_soltas.append((item["id"], item["job"]))


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
        # Dois registros do mesmo fato. Consertar só o de `runs`, como a v0.6.0
        # fazia, deixava a linha de fila presa e o job morto assim mesmo.
        for item in estado.filas_presas():
            if item["job"] == run.job and not pid_vivo(item.get("claimed_pid")):
                estado.solta_fila(item["id"])
                resultado.filas_soltas.append((item["id"], item["job"]))
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
        # Toda espera precisa de prazo para um estado terminal. Sem isto a
        # execução ficava em 'pending' indefinidamente: sem reenvio, sem virar
        # falha, sem aviso, e com o artefato ocupando disco para sempre. A doze
        # gigabytes por ocorrência, enche disco calado.
        limite = run.started_at + dt.timedelta(hours=settings.staging_hold_hours)
        if agora > limite or run.retry_count >= MAX_REENVIOS:
            _desiste_do_pendente(run, estado, resultado, agora, settings)
            continue
        if run.retry_at is None or run.retry_at > agora:
            continue
        if estado.enqueue(run.job, agora, kind="upload_retry") is not None:
            resultado.reenvios.append(run.job)


def _desiste_do_pendente(
    run: Run, estado: State, resultado: TickResult,
    agora: dt.datetime, settings: Settings,
) -> None:
    """Encerra de vez uma execução que não vai mais ser reenviada.

    Vira falha com causa explícita, avisa uma vez e libera o staging. Avisar é
    o ponto: até aqui a desistência era muda, e um backup que nunca chegou ao
    destino ficava parecendo pendente para sempre.
    """
    import shutil

    from .config import JobStore, staging_dir

    faltando = ", ".join(run.destinations_pending) or "o destino"
    if run.retry_count >= MAX_REENVIOS:
        motivo = f"{run.retry_count} tentativas de envio para {faltando}, todas falharam"
    else:
        motivo = (f"o artefato passou das {settings.staging_hold_hours}h de staging"
                  f" sem conseguir chegar em {faltando}")

    run.result = RunResult.FAILED
    run.finished_at = run.finished_at or agora
    run.error_stage = Stage.UPLOAD
    run.error_cause = motivo
    run.error_fix = f"resolva o destino e rode de novo: backup-runner run {run.job}"
    run.log.append((agora.strftime("%H:%M:%S"), "tick", "envio abandonado, staging liberado"))

    pasta = staging_dir() / run.job / run.folder
    shutil.rmtree(pasta, ignore_errors=True)
    raiz = pasta.parent
    try:
        if raiz.is_dir() and not any(raiz.iterdir()):
            raiz.rmdir()
    except OSError:
        pass

    estado.update_run(run)
    resultado.desistencias.append((run.id, run.job))

    job = JobStore.load().get(run.job)
    if job is not None:
        from .worker import _avisa

        _avisa(job, run)
