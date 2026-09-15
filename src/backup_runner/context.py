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


@dataclass
class Context:
    jobs: JobStore = field(default_factory=JobStore.load)
    destinations: DestinationStore = field(default_factory=DestinationStore.load)
    settings: Settings = field(default_factory=Settings.load)
    state: State = field(default_factory=State)

    _views: list[JobView] = field(default_factory=list, init=False)
    _staging: StagingInfo | None = field(default=None, init=False)
    _health: list[HealthItem] = field(default_factory=list, init=False)

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

    # ------------------------------------------------------------------
    @property
    def tick_ok(self) -> bool:
        return tick_installed()

    @property
    def worker(self):
        return worker_status()

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
