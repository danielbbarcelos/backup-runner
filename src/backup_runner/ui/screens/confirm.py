"""Confirmações destrutivas.

Apagar um job apaga também os artefatos nos destinos, e isso não tem volta. O
modal diz o número exato do que some, exige o nome digitado por inteiro e
começa com o foco em cancelar. Um enter distraído não apaga nada.
"""
from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from ...i18n import t
from .. import markup as m
from .. import theme as T
from ..context import JobView


class ConfirmDeleteJob(ModalScreen[bool]):
    CSS = """
    ConfirmDeleteJob { align: center middle; }
    #caixa { width: 74; height: auto; border: round $br-danger; background: $br-surface; padding: 1 2; }
    #nome { width: 40; }
    """

    BINDINGS = [("escape", "cancelar", "cancelar")]

    def __init__(self, view: JobView) -> None:
        super().__init__()
        self.view = view

    def compose(self) -> ComposeResult:
        with Vertical(id="caixa"):
            yield Static(self._cabecalho(), markup=True, id="cabecalho")
            yield Input(placeholder=self.view.name, id="nome")
            yield Static(self._rodape(), markup=True, id="rodape")

    def on_mount(self) -> None:
        # O foco começa no campo, mas o enter só libera com o nome inteiro.
        self.query_one("#nome", Input).focus()

    def _cabecalho(self) -> str:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        execucoes = ctx.state.count_runs(job=self.view.name)
        destinos = ctx.job_destinations(self.view.job)
        artefatos = sum(jd.days(d) for jd, d in destinos)
        detalhe = ", ".join(
            f"{jd.days(d)} em {jd.name}" for jd, d in destinos
        ) or t("common.none")

        linhas = [
            "",
            m.c(f"{T.SYM_FAIL} {t('confirm.delete_job', nome=self.view.name)}", T.DANGER, bold=True),
            "",
            m.body(t("confirm.no_undo")),
            "",
            "   " + m.body(t("confirm.job_and_schedule")),
            "   " + m.body(t("confirm.run_records", n=execucoes)),
            "   " + m.body(t("confirm.artifacts", n=f"até {artefatos}", size="conforme a retenção")),
            "       " + m.muted(detalhe),
            "",
            m.dim("─" * 68),
            "",
            m.body(t("confirm.type_name")),
            "",
        ]
        return "\n".join(linhas)

    def _rodape(self, digitado: str = "") -> str:
        faltam = len(self.view.name) - len(digitado)
        liberado = digitado == self.view.name
        if liberado:
            aviso = m.c(f"{T.SYM_OK} nome confere", T.SUCCESS)
        elif faltam > 0:
            chave = "confirm.chars_left" if faltam == 1 else "confirm.chars_left_plural"
            aviso = m.muted(t(chave, n=faltam))
        else:
            aviso = m.c("não confere", T.DANGER)
        botao = (
            m.c(f"▏ {t('confirm.delete_all')}    enter", T.DANGER, bold=True)
            if liberado
            else m.dim(f"▏ {t('confirm.delete_all')}    enter ({t('confirm.blocked')})")
        )
        return "\n".join([
            "   " + aviso,
            "",
            "  " + botao,
            "  " + m.muted(f"▏ {t('key.cancel')}       esc"),
            "",
            m.dim("  " + t("confirm.focus_note")),
        ])

    @on(Input.Changed)
    def _mudou(self, evento: Input.Changed) -> None:
        self.query_one("#rodape", Static).update(self._rodape(evento.value))

    @on(Input.Submitted)
    def _enviou(self, evento: Input.Submitted) -> None:
        if evento.value != self.view.name:
            return
        ctx = self.app.ctx  # type: ignore[attr-defined]
        ctx.state.delete_job_runs(self.view.name)
        ctx.jobs.delete(self.view.name)
        self.dismiss(True)

    def action_cancelar(self) -> None:
        self.dismiss(False)


class ConfirmQuit(ModalScreen[bool]):
    CSS = """
    ConfirmQuit { align: center middle; }
    #caixa { width: 60; height: auto; border: round $br-primary; background: $br-surface; padding: 1 2; }
    """

    BINDINGS = [("escape", "ficar", "ficar"), ("y", "sair", "sair"), ("n", "ficar", "ficar")]

    def compose(self) -> ComposeResult:
        with Vertical(id="caixa"):
            yield Static(
                "\n".join([
                    "",
                    m.body("Há uma execução em andamento."),
                    "",
                    m.muted("Sair fecha só esta janela. O worker continua rodando"),
                    m.muted("sob o supervisord e termina o backup normalmente."),
                    "",
                    "  " + m.key("y", " sair mesmo assim") + "     " + m.key("n", " ficar"),
                    "",
                ]),
                markup=True,
            )

    def action_sair(self) -> None:
        self.dismiss(True)

    def action_ficar(self) -> None:
        self.dismiss(False)
