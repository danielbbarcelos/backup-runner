"""Histórico de execuções.

Tabela densa de propósito, sem linha em branco entre registros: aqui a pessoa
varre e compara, diferente do dashboard, que é leitura calma. O detalhe embaixo
é uma prévia curta; `enter` abre a execução inteira com log, manifest e as
tabelas ignoradas naquele dia.
"""
from __future__ import annotations

import datetime as dt

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Static

from ...i18n import t
from ...models import Run, RunResult
from .. import markup as m
from .. import theme as T
from ..widgets import Hero, StatusBar

FILTROS_RESULTADO: list[tuple[str, tuple[RunResult, ...]]] = [
    ("todos", ()),
    ("falha + atrasado", (RunResult.FAILED, RunResult.LATE, RunResult.PENDING_UPLOAD, RunResult.MISSED)),
    ("só falha", (RunResult.FAILED,)),
    ("só ok", (RunResult.OK,)),
]

PERIODOS = [("30 dias", 30), ("7 dias", 7), ("90 dias", 90), ("tudo", 3650)]


class HistoryScreen(Screen):
    CSS = """
    HistoryScreen { background: $br-bg; }
    #filtros { height: 1; padding: 0 1; }
    #tabela { height: 1fr; border: round $br-border; }
    #rodape-tabela { height: 1; padding: 0 2; }
    #previa { height: 9; border: round $br-border; padding: 0 1; }
    """

    BINDINGS = [
        ("up,k", "mover(-1)", "linha"),
        ("down,j", "mover(1)", "linha"),
        ("enter", "abrir", "abrir"),
        ("f", "ciclar_resultado", "filtrar resultado"),
        ("j", "ciclar_job", "filtrar job"),
        ("p", "ciclar_periodo", "período"),
        ("x", "limpar", "limpar filtros"),
        ("escape,q", "voltar", "voltar"),
        ("question_mark", "ajuda", "ajuda"),
    ]

    def __init__(self, job: str | None = None) -> None:
        super().__init__()
        self._job = job
        self._i_resultado = 0
        self._i_periodo = 0
        self._runs: list[Run] = []

    def compose(self) -> ComposeResult:
        yield Hero(t("screen.history"), id="hero")
        yield Static(id="filtros", markup=True)
        yield DataTable(id="tabela", cursor_type="row", zebra_stripes=False)
        yield Static(id="rodape-tabela", markup=True)
        yield Vertical(Static(id="previa", markup=True))
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        tabela = self.query_one("#tabela", DataTable)
        tabela.add_column(t("hist.when"), width=15)
        tabela.add_column(t("hist.job"), width=19)
        tabela.add_column(t("hist.result"), width=15)
        tabela.add_column(t("hist.size"), width=11)
        tabela.add_column(t("hist.duration"), width=13)
        tabela.add_column(t("hist.destinations"), width=18)
        self.query_one("#previa").border_title = "prévia"
        self.refresh_data()
        tabela.focus()

    # ------------------------------------------------------------------
    def refresh_data(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        _, resultados = FILTROS_RESULTADO[self._i_resultado]
        _, dias = PERIODOS[self._i_periodo]
        desde = dt.datetime.now() - dt.timedelta(days=dias)

        self._runs = ctx.state.runs(
            job=self._job, results=resultados or None, since=desde, limit=500
        )
        tabela = self.query_one("#tabela", DataTable)
        linha_atual = tabela.cursor_row
        tabela.clear()
        for run in self._runs:
            tabela.add_row(*self._linha(run), key=str(run.id))
        if self._runs:
            tabela.move_cursor(row=min(linha_atual, len(self._runs) - 1))

        self._preencher_filtros()
        self._preencher_rodape()
        self._preencher_previa()
        self._preencher_hero()
        self._preencher_status()

    def _linha(self, run: Run) -> list[Text]:
        simbolo, rotulo, cor = m.badge_parts(run.result)
        destinos = (
            t("hist.n_dest", n=len(run.destinations_done))
            if len(run.destinations_done) > 1
            else (run.destinations_done[0] if run.destinations_done else t("hist.none"))
        )
        if run.destinations_pending:
            destinos = f"{destinos}, {len(run.destinations_pending)} falta"
        return [
            Text(run.started_at.strftime("%d/%m %H:%M"), style=T.TEXT),
            Text(run.job, style=T.TEXT),
            Text(f"{simbolo} {rotulo}", style=cor),
            Text(T.format_bytes(run.bytes), style=T.MUTED),
            Text(T.format_duration(run.duration), style=T.MUTED),
            Text(destinos, style=T.MUTED),
        ]

    def _preencher_filtros(self) -> None:
        nome_resultado = FILTROS_RESULTADO[self._i_resultado][0]
        nome_periodo = PERIODOS[self._i_periodo][0]
        texto = (
            m.muted(f"{t('hist.filter_job')} ") + m.c(T.SYM_FIELD, T.PRIMARY)
            + m.body(self._job or t("hist.all")) + "   "
            + m.muted(f"{t('hist.filter_result')} ") + m.c(T.SYM_FIELD, T.PRIMARY)
            + m.body(nome_resultado) + "   "
            + m.muted(f"{t('hist.filter_period')} ") + m.c(T.SYM_FIELD, T.PRIMARY)
            + m.body(nome_periodo) + "        "
            + m.muted(t("hist.items", n=len(self._runs)))
        )
        self.query_one("#filtros", Static).update(texto)

    def _preencher_rodape(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        tabela = self.query_one("#tabela", DataTable)
        visiveis = min(len(self._runs), max(1, tabela.size.height - 1))
        total_ok = ctx.state.count_runs() - len(self._runs)
        texto = m.muted(
            t("hist.visible", n=visiveis, total=len(self._runs), ok=total_ok)
        ) if self._runs else m.muted(t("empty.generic"))
        self.query_one("#rodape-tabela", Static).update(texto)

    def _preencher_hero(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        hero = self.query_one("#hero", Hero)
        hero.right_top = f"{t('hist.runs', n=ctx.state.count_runs())}       {PERIODOS[self._i_periodo][0]}"
        hero.right_bottom = t("hist.keeps", d=ctx.settings.history_days)
        hero.refresh()

    def _preencher_status(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        barra = self.query_one("#status", StatusBar)
        barra.worker_state = "ativo" if ctx.worker.running else "parado"
        barra.queue = ctx.queue_size
        barra.tick = ctx.tick_ok
        barra.detail = t("hist.filter_state", filtro=FILTROS_RESULTADO[self._i_resultado][0])
        barra.hints = m.keys(
            ("↑↓", t("key.line")), ("enter", t("key.open")), ("f", t("key.filter_result")),
            ("j", t("key.filter_job")), ("p", t("hist.filter_period")),
            ("x", t("key.clear_filters")), ("esc", t("key.back")),
        )
        barra.refresh()

    # ------------------------------------------------------------------
    @property
    def run_atual(self) -> Run | None:
        tabela = self.query_one("#tabela", DataTable)
        if not self._runs or tabela.cursor_row is None:
            return None
        if tabela.cursor_row >= len(self._runs):
            return None
        return self._runs[tabela.cursor_row]

    def _preencher_previa(self) -> None:
        alvo = self.query_one("#previa", Static)
        run = self.run_atual
        if run is None:
            alvo.update("\n" + m.muted("  " + t("empty.generic")))
            return

        simbolo, rotulo, cor = m.badge_parts(run.result)
        cabecalho = (
            m.body(f"{run.started_at.strftime('%d/%m %H:%M')} {run.job}").ljust(0)
            + "   " + m.c(f"{simbolo} {rotulo}", cor)
        )
        if run.error_stage:
            # O badge já disse "falha"; aqui só falta onde ela aconteceu.
            cabecalho += m.c(f" no estágio {run.error_stage.value}", cor)
        cabecalho += "        " + m.muted(t("hist.open"))

        linhas = ["", cabecalho, ""]
        rotulo_col = lambda texto: m.secondary(texto.ljust(16))
        if run.error_got:
            linhas.append(rotulo_col(t("hist.message")) + m.body(_uma_linha(run.error_got, 60)))
        if run.result is RunResult.PENDING_UPLOAD:
            linhas.append(rotulo_col(t("hist.artifact")) + m.body(run.artifact or "?"))
            quando = run.retry_at.strftime("%H:%M") if run.retry_at else "?"
            linhas.append(rotulo_col(t("hist.resend")) + m.body(
                f"{', '.join(run.destinations_pending)} às {quando}"))
        elif run.error_stage:
            linhas.append(rotulo_col(t("hist.artifact")) + m.muted(t("hist.artifact_none")))
            linhas.append(rotulo_col(t("hist.resend")) + m.muted(t("hist.not_applicable")))
        else:
            linhas.append(rotulo_col(t("hist.artifact")) + m.body(run.artifact or "?"))
            linhas.append(
                rotulo_col(t("det.manifest"))
                + m.muted(f"{len(run.manifest)} cópias conferem, sha {run.manifest[0].sha256[:12]}" if run.manifest else T.SYM_NONE)
            )
        alvo.update("\n".join(linhas))

    # ------------------------------------------------------------------
    @on(DataTable.RowHighlighted)
    def _mudou_linha(self) -> None:
        self._preencher_previa()

    @on(DataTable.RowSelected)
    def _selecionou_linha(self) -> None:
        # O DataTable consome o enter antes do binding da tela, então a
        # abertura precisa vir do evento dele.
        self.action_abrir()

    def on_resize(self) -> None:
        # A altura só existe depois do layout, então a contagem de visíveis
        # precisa ser refeita aqui, não no primeiro refresh_data.
        self._preencher_rodape()

    def action_mover(self, passo: int) -> None:
        tabela = self.query_one("#tabela", DataTable)
        tabela.action_cursor_up() if passo < 0 else tabela.action_cursor_down()

    def action_abrir(self) -> None:
        run = self.run_atual
        if run is None:
            return
        from .run_detail import RunDetailScreen

        self.app.push_screen(RunDetailScreen(run.id))

    def action_ciclar_resultado(self) -> None:
        self._i_resultado = (self._i_resultado + 1) % len(FILTROS_RESULTADO)
        self.refresh_data()

    def action_ciclar_periodo(self) -> None:
        self._i_periodo = (self._i_periodo + 1) % len(PERIODOS)
        self.refresh_data()

    def action_ciclar_job(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        nomes = [None] + [v.name for v in ctx.views]
        atual = nomes.index(self._job) if self._job in nomes else 0
        self._job = nomes[(atual + 1) % len(nomes)]
        self.refresh_data()

    def action_limpar(self) -> None:
        self._job = None
        self._i_resultado = 0
        self._i_periodo = 0
        self.refresh_data()

    def action_voltar(self) -> None:
        self.app.pop_screen()

    def action_ajuda(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen(self.BINDINGS, t("screen.history")))


def _uma_linha(texto: str, largura: int) -> str:
    achatado = " ".join(texto.split())
    return achatado if len(achatado) <= largura else achatado[: largura - 1] + "…"
