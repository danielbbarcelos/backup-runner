"""Contexto compartilhado pelas telas.

Junta os stores, o banco de estado e as contas derivadas (próxima execução,
última execução, tamanho da fila) num objeto só, para que cada tela leia dados
prontos em vez de recalcular. Recarrega sob demanda: a TUI observa processos
externos (tick e worker) que mudam o estado por baixo dela.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .config import DestinationStore, JobStore, Settings
from .health import HealthItem, StagingInfo, collect, staging_info, tick_installed, worker_status
from .models import Destination, Job, JobDestination, Run, RunResult, SourceKind
from .schedule import humanize, next_run, previous_run
from .state import State


@dataclass
class JobView:
    """Um job com tudo que o dashboard precisa mostrar, já resolvido."""
    job: Job
    last: Run | None = None
    last_success: Run | None = None
    next_at: dt.datetime | None = None
    queued: bool = False
    running: bool = False
    queue_position: int | None = None
    queue_behind: str | None = None

    @property
    def name(self) -> str:
        return self.job.name

    @property
    def paused(self) -> bool:
        return not self.job.enabled

    @property
    def kind_label(self) -> str:
        return "mysql" if self.job.kind is SourceKind.MYSQL else "arqs"

    @property
    def result(self) -> RunResult | None:
        if self.running:
            return RunResult.RUNNING
        if self.queued:
            return RunResult.QUEUED
        if self.paused:
            return RunResult.SKIPPED
        return self.last.result if self.last else None

    def next_in_seconds(self, agora: dt.datetime | None = None) -> float | None:
        if self.paused or self.next_at is None:
            return None
        return (self.next_at - (agora or dt.datetime.now())).total_seconds()

    def schedule_human(self) -> str:
        return humanize(self.job.schedule)

    def is_stale(self, agora: dt.datetime | None = None) -> bool:
        """Sem sucesso por mais tempo que o job admite.

        É o único sinal que pega worker morto, tick removido e job desativado
        sem querer, que são justamente as falhas que nenhum alerta de execução
        consegue emitir, porque não houve execução.
        """
        if self.paused:
            return False
        agora = agora or dt.datetime.now()
        if self.last_success is None:
            return self.last is not None
        limite = dt.timedelta(hours=self.job.stale_after_hours)
        return (agora - self.last_success.started_at) > limite


def encerra_job(ctx: "Context", nome: str) -> dict:
    """Apaga o job e tudo que era dele, devolvendo o que foi encerrado.

    Apagar um job é dizer "não quero mais nada disto". Até aqui o programa
    entendia pela metade: tirava o cadastro e os registros, mas deixava o que
    estava na fila, o artefato meio pronto no staging, e principalmente a
    execução em curso, que seguia até o fim e ainda mandava email e Slack
    sobre um job que já não existia.

    A execução em curso não morre aqui: quem a interrompe é o worker, que
    percebe a ausência do job em até dois segundos e desiste sem avisar
    ninguém. Esta função só precisa dizer que existia uma.
    """
    import shutil

    from .config import staging_dir

    rodando = ctx.state.running()
    # A fila primeiro, e só depois os registros: `delete_job_runs` limpa a fila
    # inteira de quebra, então contar depois dele daria sempre zero.
    parado = {
        "fila": ctx.state.cancel_queue(nome),
        "execucoes": ctx.state.delete_job_runs(nome),
        "rodando": rodando is not None and rodando.job == nome,
        "staging": 0,
    }

    pasta = staging_dir() / nome
    if pasta.is_dir():
        parado["staging"] = sum(
            f.stat().st_size for f in pasta.rglob("*") if f.is_file()
        )
        shutil.rmtree(pasta, ignore_errors=True)

    # O cadastro sai por último: enquanto ele existir, o worker em curso ainda
    # se considera legítimo, e é a ausência dele que serve de sinal de parada.
    ctx.jobs.delete(nome)
    ctx.refresh()
    return parado


def cancela_execucao(ctx: "Context", run) -> dict:
    """Interrompe uma execução, com worker vivo ou sem ele.

    São dois caminhos, e os dois precisam existir. Com worker vivo, a marca no
    banco basta: o vigia a lê em segundos e a execução sai pelo caminho limpo,
    removendo o staging e gravando o desfecho. Matar o processo daqui deixaria
    exatamente a sujeira que o estudo mandou parar de produzir, com artefato
    órfão no disco e linha de fila presa.

    Sem worker vivo, não há quem obedeça, então a limpeza é feita aqui mesmo.
    É o caso de quem pediu cancelamento de uma execução que já estava morta sem
    ninguém ter notado.

    Execução com envio pendente também é cancelável, e aí cancelar quer dizer
    "pare de tentar reenviar".
    """
    import shutil

    from .config import staging_dir
    from .models import RunResult
    from .service import pid_vivo

    bruto = ctx.state.progresso_de(run.id) or {}
    pid = bruto.get("prog_pid")
    vivo = run.result is RunResult.RUNNING and pid_vivo(pid)

    ctx.state.pede_cancelamento(run.id)
    feito = {"vivo": vivo, "pid": pid, "staging": 0, "multiparts": 0}
    if vivo:
        # O vigia cuida do resto. Mexer no staging agora seria tirar o chão de
        # quem ainda está escrevendo nele.
        return feito

    from .worker import aborta_multiparts

    feito["multiparts"] = aborta_multiparts(ctx.state, run.id)

    pasta = staging_dir() / run.job / run.folder
    if pasta.is_dir():
        feito["staging"] = sum(f.stat().st_size for f in pasta.rglob("*") if f.is_file())
        shutil.rmtree(pasta, ignore_errors=True)
        try:
            if pasta.parent.is_dir() and not any(pasta.parent.iterdir()):
                pasta.parent.rmdir()
        except OSError:
            pass

    for item in ctx.state.filas_presas():
        if item["job"] == run.job and not pid_vivo(item.get("claimed_pid")):
            ctx.state.solta_fila(item["id"])

    run.result = RunResult.FAILED
    run.finished_at = run.finished_at or dt.datetime.now()
    from .worker import _etapa_atual

    run.error_stage = run.error_stage or _etapa_atual(ctx.state, run)
    run.error_cause = "cancelada por você"
    run.error_fix = f"rode de novo quando quiser: backup-runner run {run.job}"
    run.log.append((dt.datetime.now().strftime("%H:%M:%S"), "cancelado",
                    "cancelada sem worker ativo, limpeza feita na hora"))
    ctx.state.update_run(run)
    ctx.refresh()
    return feito


@dataclass
class Context:
    jobs: JobStore = field(default_factory=JobStore.load)
    destinations: DestinationStore = field(default_factory=DestinationStore.load)
    settings: Settings = field(default_factory=Settings.load)
    state: State = field(default_factory=State)

    _views: list[JobView] = field(default_factory=list, init=False)
    _staging: StagingInfo | None = field(default=None, init=False)
    _health: list[HealthItem] = field(default_factory=list, init=False)
    # `supervisorctl status` custa quase 0,4s, e a mesma tela pergunta várias
    # vezes. Guardar por ciclo de refresh é o que mantém o menu instantâneo.
    _worker: object | None = field(default=None, init=False)
    _tick: bool | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self.jobs = JobStore.load()
        self.destinations = DestinationStore.load()
        self.settings = Settings.load()
        self._views = self._build_views()
        self._staging = None
        self._health = []
        self._worker = None
        self._tick = None

    def _build_views(self) -> list[JobView]:
        fila = self.state.queue_pending()
        pendentes = [f for f in fila if f["status"] == "pending"]
        rodando = {f["job"] for f in fila if f["status"] == "running"}
        posicoes = {f["job"]: i for i, f in enumerate(pendentes)}
        primeiro_da_fila = next(iter(rodando), None)

        views = []
        for job in self.jobs.list():
            view = JobView(
                job=job,
                last=self.state.last_run(job.name),
                last_success=self.state.last_success(job.name),
                next_at=next_run(job.schedule) if job.enabled else None,
                queued=job.name in posicoes,
                running=job.name in rodando,
            )
            if view.queued:
                view.queue_position = posicoes[job.name] + 1
                view.queue_behind = primeiro_da_fila
            views.append(view)
        return sorted(views, key=_ordem)

    # ------------------------------------------------------------------
    @property
    def views(self) -> list[JobView]:
        return self._views

    def view(self, nome: str) -> JobView | None:
        for v in self._views:
            if v.name == nome:
                return v
        return None

    def destination(self, nome: str) -> Destination | None:
        return self.destinations.get(nome)

    def job_destinations(self, job: Job) -> list[tuple[JobDestination, Destination | None]]:
        return [(jd, self.destinations.get(jd.name)) for jd in job.destinations]

    def destination_users(self, nome: str) -> list[str]:
        return [j.name for j in self.jobs.list() if any(d.name == nome for d in j.destinations)]

    # ------------------------------------------------------------------
    @property
    def staging(self) -> StagingInfo:
        if self._staging is None:
            self._staging = staging_info()
        return self._staging

    @property
    def health(self) -> list[HealthItem]:
        if not self._health:
            self._health = collect(self.destinations.list())
        return self._health

    def invalidate_health(self) -> None:
        self._health = []
        self._staging = None
        self._worker = None
        self._tick = None

    # ------------------------------------------------------------------
    @property
    def tick_ok(self) -> bool:
        if self._tick is None:
            self._tick = tick_installed()
        return self._tick

    @property
    def worker(self):
        if self._worker is None:
            self._worker = worker_status()
        return self._worker

    @property
    def queue_size(self) -> int:
        return self.state.queue_size()

    @property
    def running_run(self) -> Run | None:
        return self.state.running()

    def counts(self) -> tuple[int, int, int]:
        total = len(self._views)
        ativos = sum(1 for v in self._views if not v.paused)
        return total, ativos, total - ativos

    def next_overall(self) -> tuple[JobView, float] | None:
        agora = dt.datetime.now()
        candidatos = [
            (v, v.next_in_seconds(agora))
            for v in self._views
            if v.next_in_seconds(agora) is not None
        ]
        if not candidatos:
            return None
        return min(candidatos, key=lambda par: par[1])  # type: ignore[arg-type]

    def last_overall(self) -> Run | None:
        todas = self.state.runs(limit=1)
        return todas[0] if todas else None

    def stale_jobs(self) -> list[JobView]:
        return [v for v in self._views if v.is_stale()]


def _ordem(view: JobView) -> tuple:
    """Ordem da lista: o que está acontecendo primeiro, o que dorme por último.

    Rodando, depois na fila, depois os ativos pela próxima execução, e os
    pausados no fim. Ordem alfabética colocaria um job desligado no topo, que é
    o lugar de quem precisa de atenção agora.
    """
    if view.running:
        grupo = 0
    elif view.queued:
        grupo = 1
    elif view.paused:
        grupo = 3
    else:
        grupo = 2
    segundos = view.next_in_seconds()
    return (grupo, segundos if segundos is not None else float("inf"), view.name.lower())
