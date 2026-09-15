"""Detalhe de uma execução.

Duas caras. A bem sucedida é consulta: log, manifest com os hashes e as tabelas
ignoradas naquele dia. A que falhou é a tela que a pessoa abre depois de receber
o alerta, e responde em ordem: o que falhou, o que o processo disse, se o
artefato sobreviveu, se o reenvio acontece sozinho, e qual tecla resolve agora.

O erro usa sempre o mesmo molde de quatro linhas (o que tentei, o que recebi,
causa provável, como consertar), igual ao teste de destino e ao de conexão, para
a pessoa aprender a ler o erro uma vez só.
"""
from __future__ import annotations

import datetime as dt

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from ...i18n import t
from ...models import Run, RunResult, Stage, StageState
from .. import markup as m
from .. import theme as T
from ..widgets import Hero, StatusBar

ABAS = ["log", "manifest", "tabelas ignoradas"]


class RunDetailScreen(Screen):
    CSS = """
    RunDetailScreen { background: $br-bg; }
    #abas-run { height: 1; padding: 0 1; }
    #corpo-run { height: 1fr; border: round $br-border; padding: 0 1; }
    #acoes { height: 2; padding: 0 1; }
    """

    BINDINGS = [
        ("tab", "proxima_aba", "aba"),
        ("up,k", "rolar(-3)", "rolar"),
        ("down,j", "rolar(3)", "rolar"),
        ("R", "reenviar", "reenviar"),
        ("t", "corrigir_destino", "destino"),
        ("r", "rodar_job", "rodar job"),
        ("c", "copiar", "copiar"),
        ("m", "conferir", "conferir hashes"),
        ("escape,q", "voltar", "voltar"),
        ("question_mark", "ajuda", "ajuda"),
    ]

    def __init__(self, run_id: int) -> None:
        super().__init__()
        self._run_id = run_id
        self._aba = 0
        self.run: Run | None = None

    def compose(self) -> ComposeResult:
        yield Hero("", id="hero")
        yield Static(id="abas-run", markup=True)
        yield VerticalScroll(Static(id="corpo-run", markup=True))
        yield Static(id="acoes", markup=True)
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.refresh_data()

    # ------------------------------------------------------------------
    def refresh_data(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        self.run = ctx.state.get_run(self._run_id)
        if self.run is None:
            self.app.pop_screen()
            return

        run = self.run
        falhou = run.result in (RunResult.FAILED, RunResult.PENDING_UPLOAD)
        simbolo, rotulo, cor = m.badge_parts(run.result)

        hero = self.query_one("#hero", Hero)
        hero.screen_title = f"{run.job}, {run.started_at.strftime('%d/%m %H:%M')}"
        hero.right_top = t("det.run_n", n=run.id)
        hero.right_bottom = (
            f"{simbolo} {rotulo}      "
            + (f"parou em {T.format_duration(run.duration)}" if falhou
               else f"{T.format_bytes(run.bytes)} em {T.format_duration(run.duration)}")
        )
        hero.refresh()

        self.query_one("#abas-run").display = not falhou
        if not falhou:
            self._preencher_abas()
        self.query_one("#corpo-run", Static).update(
            self._corpo_falha(run) if falhou else self._corpo_ok(run)
        )
        self._preencher_acoes(run, falhou)
        self._preencher_status(run, falhou)

    def _preencher_abas(self) -> None:
        partes = []
        for i, nome in enumerate(ABAS):
            partes.append(m.primary(f"▌{nome}", bold=True) if i == self._aba else m.muted(f" {nome}"))
        self.query_one("#abas-run", Static).update("   ".join(partes) + "     " + m.dim("◂ tab ▸"))

    # ------------------------------------------------------------------
    def _corpo_ok(self, run: Run) -> str:
        if self._aba == 0:
            return self._log(run)
        if self._aba == 1:
            return self._manifest(run)
        return self._ignoradas(run)

    def _log(self, run: Run) -> str:
        linhas = ["", m.secondary(t("det.full_log")), ""]
        for hora, origem, texto in run.log:
            linhas.append(f"  {m.muted(hora)} {m.secondary(origem.ljust(10))} {m.body(texto)}")
        linhas.append("")
        linhas.append(m.dim("─" * 60))
        linhas.append(m.secondary(t("det.stages")))
        linhas.append("")
        for estagio in run.stages:
            simbolo, cor = _estado(estagio.state)
            nome = (estagio.label or estagio.stage.value).ljust(12)
            linhas.append(
                f"  {m.c(simbolo, cor)} {m.body(nome)}{m.muted(estagio.detail.ljust(28))}"
                f"{m.muted(T.format_duration(estagio.seconds))}"
            )
        return "\n".join(linhas)

    def _manifest(self, run: Run) -> str:
        linhas = ["", m.secondary(t("det.manifest")), m.muted(t("det.manifest_why")), ""]
        if not run.manifest:
            linhas.append("  " + m.muted(t("common.none")))
            return "\n".join(linhas)
        for entrada in run.manifest:
            estado = (
                m.c(f"{T.SYM_OK} {t('det.matches')}", T.SUCCESS)
                if entrada.verified
                else m.c(f"{T.SYM_FAIL} {t('det.mismatch')}", T.DANGER)
            )
            linhas.append(
                f"  {m.secondary(entrada.destination.ljust(14))}"
                f"{m.body(entrada.sha256[:16].ljust(20))}{estado}"
                f"   {m.muted(T.format_bytes(entrada.bytes))}"
            )
        linhas += [
            "",
            m.dim("─" * 60),
            "",
            m.secondary(t("det.file")),
            "  " + m.body(run.artifact or T.SYM_NONE),
            "",
            m.muted(f"  pasta da execução: {run.folder}/"),
            "",
            m.key("m", f" {t('key.check_hashes')}") + "     " + m.key("c", f" {t('key.copy_path')}"),
        ]
        return "\n".join(linhas)

    def _ignoradas(self, run: Run) -> str:
        linhas = [
            "",
            m.secondary(t("det.ignored_tables")),
            m.muted(t("det.ignored_day", n=run.ignored_total)),
            m.muted(t("det.ignored_counts", regex=len(run.ignored_regex), mao=len(run.ignored_manual))),
            "",
        ]
        if run.ignored_regex:
            linhas.append("  " + m.c(T.CHECK_RULE, T.SECONDARY) + m.muted(" pela regex"))
            linhas.extend(_colunas(run.ignored_regex, prefixo="    "))
            linhas.append("")
        if run.ignored_manual:
            linhas.append("  " + m.c(T.CHECK_MANUAL, T.PRIMARY) + m.muted(" à mão"))
            linhas.extend(_colunas(run.ignored_manual, prefixo="    "))
        if not run.ignored_total:
            linhas.append("  " + m.muted("nenhuma, o dump levou todas as tabelas"))
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    def _corpo_falha(self, run: Run) -> str:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        estagio = run.error_stage.value if run.error_stage else "?"
        quando = run.finished_at.strftime("%H:%M:%S") if run.finished_at else ""
        falhou_em = next(
            (s.label for s in run.stages if s.state is StageState.FAILED), estagio
        )

        linhas = [
            "",
            m.c(f"{T.SYM_FAIL} {falhou_em}", T.DANGER, bold=True)
            + m.muted(f"            estágio {estagio}, {quando}"),
            "",
        ]
        rotulo = lambda texto: m.secondary(texto.ljust(18))
        if run.error_tried:
            linhas.append("  " + rotulo(t("err.tried")) + m.body(run.error_tried))
        if run.error_got:
            linhas.append("  " + rotulo(t("err.got")))
            for pedaco in _quebrar(run.error_got, 66):
                linhas.append("    " + m.body(pedaco))
        if run.error_cause:
            linhas.append("  " + rotulo(t("err.cause")) + m.body(run.error_cause))
        if run.error_fix:
            linhas.append("  " + rotulo(t("err.fix")) + m.body(run.error_fix))

        linhas += ["", m.dim("─" * 66), "", m.secondary(t("det.what_happened")), ""]
        for e in run.stages:
            simbolo, cor = _estado(e.state)
            nome = (e.label or e.stage.value).ljust(16)
            linhas.append(
                f"  {m.c(simbolo, cor)} {m.body(nome)}{m.muted(e.detail.ljust(34))}"
                f"{m.muted(T.format_duration(e.seconds))}"
            )

        linhas.append("")
        if run.result is RunResult.PENDING_UPLOAD and run.artifact:
            boas = run.destinations_done
            linhas.append(m.c(f"  {T.SYM_OK} {t('det.artifact_survived')}", T.SUCCESS))
            linhas.append("    " + m.body(run.artifact))
            if boas:
                linhas.append("    " + m.muted(t("det.one_good_copy", destino=", ".join(boas))))
            horas = ctx.settings.staging_hold_hours
            ate = (run.started_at + dt.timedelta(hours=horas)).strftime("%d/%m %H:%M")
            linhas.append("    " + m.muted(t("det.staging_holds", h=horas, quando=ate)))
            linhas.append("")
            if run.retry_at:
                falta = (run.retry_at - dt.datetime.now()).total_seconds()
                linhas.append(m.c(f"  {T.SYM_WARN} {t('det.auto_retry')}", T.WARNING))
                linhas.append(
                    "    " + m.muted(t(
                        "det.retry_plan",
                        destinos=", ".join(run.destinations_pending) or "os destinos que faltam",
                        hora=run.retry_at.strftime("%H:%M"),
                        t=T.format_relative(falta),
                        n=3,
                    ))
                )
        else:
            linhas.append(m.inline("warn", "nenhum artefato foi escrito, a janela ficou sem backup"))
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    def _preencher_acoes(self, run: Run, falhou: bool) -> None:
        alvo = self.query_one("#acoes", Static)
        if not falhou:
            alvo.update("")
            return
        pendente = run.result is RunResult.PENDING_UPLOAD
        linha = "  ".join([
            m.c(f"▏ {t('det.resend_now')} R", T.PRIMARY if pendente else T.DISABLED, bold=pendente),
            m.body(f"▏ {t('det.fix_dest')} t"),
            m.body(f"▏ {t('det.run_whole')} r"),
        ])
        alvo.update(linha + "\n " + m.muted(t("det.resend_vs_run")))

    def _preencher_status(self, run: Run, falhou: bool) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        barra = self.query_one("#status", StatusBar)
        barra.worker_state = "ativo" if ctx.worker.running else "parado"
        barra.queue = ctx.queue_size
        barra.tick = ctx.tick_ok
        pendentes = len(ctx.state.pending_uploads())
        barra.detail = t("det.pending_uploads", n=pendentes) if pendentes else ""
        if falhou:
            barra.hints = m.keys(
                ("R", t("key.retry")), ("t", t("key.fix_dest")), ("r", t("key.run_job")),
                ("c", t("key.copy_error")), ("esc", t("key.back")),
            )
        else:
            barra.hints = m.keys(
                ("↑↓", t("key.scroll")), ("tab", t("key.tab")), ("c", t("key.copy_path")),
                ("m", t("key.check_hashes")), ("esc", t("key.back")),
            )
        barra.refresh()

    # ------------------------------------------------------------------
    def action_proxima_aba(self) -> None:
        self._aba = (self._aba + 1) % len(ABAS)
        self.refresh_data()

    def action_rolar(self, passo: int) -> None:
        self.query_one(VerticalScroll).scroll_relative(y=passo, animate=False)

    def action_reenviar(self) -> None:
        if self.run is None or self.run.result is not RunResult.PENDING_UPLOAD:
            return
        ctx = self.app.ctx  # type: ignore[attr-defined]
        ctx.state.enqueue(self.run.job, dt.datetime.now(), kind="upload_retry")
        self.notify("reenvio enfileirado, usando o artefato do staging", severity="information")

    def action_corrigir_destino(self) -> None:
        from .destinations import DestinationsScreen

        alvo = self.run.destinations_pending[0] if self.run and self.run.destinations_pending else None
        self.app.push_screen(DestinationsScreen(selecionado=alvo))

    def action_rodar_job(self) -> None:
        if self.run is None:
            return
        ctx = self.app.ctx  # type: ignore[attr-defined]
        ctx.state.enqueue(self.run.job, dt.datetime.now())
        self.notify(f"{self.run.job} enfileirado do zero", severity="information")

    def action_copiar(self) -> None:
        if self.run is None:
            return
        texto = self.run.error_got or self.run.artifact or ""
        if not texto:
            return
        try:
            self.app.copy_to_clipboard(texto)
            self.notify("copiado", severity="information")
        except Exception:
            self.notify(texto, title="copie daqui", timeout=15)

    def action_conferir(self) -> None:
        self.notify(
            "conferência de hash no destino entra junto com o worker",
            severity="warning",
        )

    def action_voltar(self) -> None:
        self.app.pop_screen()

    def action_ajuda(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen(self.BINDINGS, t("screen.run")))


# ----------------------------------------------------------------------------

def _estado(estado: StageState) -> tuple[str, str]:
    return {
        StageState.DONE: (T.SYM_OK, T.SUCCESS),
        StageState.RUNNING: (T.SYM_RUNNING, T.PRIMARY),
        StageState.WAITING: (T.SYM_INACTIVE, T.DISABLED),
        StageState.FAILED: (T.SYM_FAIL, T.DANGER),
        StageState.SKIPPED: (T.SYM_INACTIVE, T.DISABLED),
    }[estado]


def _quebrar(texto: str, largura: int) -> list[str]:
    palavras = " ".join(texto.split()).split(" ")
    linhas, atual = [], ""
    for palavra in palavras:
        if len(atual) + len(palavra) + 1 > largura:
            linhas.append(atual)
            atual = palavra
        else:
            atual = f"{atual} {palavra}".strip()
    if atual:
        linhas.append(atual)
    return linhas


def _colunas(nomes: list[str], *, por_linha: int = 4, prefixo: str = "") -> list[str]:
    linhas = []
    for i in range(0, len(nomes), por_linha):
        fatia = nomes[i : i + por_linha]
        linhas.append(prefixo + m.body("  ".join(n.ljust(18) for n in fatia).rstrip()))
    return linhas
