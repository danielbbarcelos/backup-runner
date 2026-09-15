"""Hero de abertura.

Fica alguns segundos ou até a primeira tecla. Ele já informa em vez de só
enfeitar: worker, tick, fila, jobs, próxima e última execução. Quem abre o app
para saber se o backup de ontem rodou recebe a resposta antes de navegar.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.screen import Screen
from textual.widgets import Static

from ..widgets import DynamicText

from ... import __version__
from ...i18n import t
from ...models import RunResult
from .. import markup as m
from .. import theme as T

WORDMARK = (Path(__file__).resolve().parent.parent / "wordmark.txt").read_text().rstrip("\n").splitlines()

DURACAO = 2.5


class SplashScreen(Screen):
    CSS = """
    SplashScreen { background: $br-bg; }
    #splash-body { width: auto; height: auto; }
    #splash-foot { dock: bottom; height: 2; padding: 0 1; }
    """

    BINDINGS = [("q", "sair", "sair")]

    def __init__(self) -> None:
        super().__init__()
        self._quadro = 0
        self._timer = None

    def compose(self) -> ComposeResult:
        with Middle():
            with Center():
                yield DynamicText(lambda _: self._corpo(), id="splash-body")
        yield DynamicText(self._rodape, id="splash-foot")

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.12, self._girar)
        self.set_timer(DURACAO, self._entrar)

    def _girar(self) -> None:
        # O intervalo sobrevive ao pop da tela: sem esta guarda, ele continua
        # pedindo repintura de uma tela que já saiu da pilha.
        if not self.is_current:
            return
        self._quadro += 1
        corpo = self.query("#splash-body")
        if corpo:
            corpo.first(DynamicText).refresh()

    def _rodape(self, largura: int) -> str:
        rodape = m.dim("─" * largura) + "\n " + m.muted(t("app.any_key"))
        espaco = largura - len(t("app.any_key")) - 7
        if espaco > 0:
            rodape += " " * espaco + m.key("q", t("key.quit"))
        return rodape

    def _corpo(self) -> str:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        linhas = [m.primary(linha) for linha in WORDMARK]
        linhas.append("")
        linhas.append(m.muted(f"v{__version__}    ") + m.muted(t("app.tagline")))
        linhas.append("")
        linhas.append(m.dim("─" * 53))
        linhas.append("")
        linhas.extend(self._estado(ctx))
        linhas.append("")
        linhas.append("")
        linhas.append("        " + m.spinner(self._quadro) + " " + m.muted(t("app.loading")))
        return "\n".join(linhas)

    def _estado(self, ctx) -> list[str]:
        total, ativos, pausados = ctx.counts()
        worker = ctx.worker
        tick = ctx.tick_ok
        fila = ctx.queue_size

        def linha(rotulo: str, valor: str) -> str:
            return m.muted(rotulo.ljust(10)) + valor

        saida = [
            linha(
                t("status.worker"),
                (m.c(T.SYM_ACTIVE, T.SUCCESS) + m.body(f" {t('status.worker_active')}, pid {worker.pid}"))
                if worker.running
                else m.c(f"{T.SYM_FAIL} {worker.message or t('status.worker_stopped')}", T.DANGER),
            ),
            linha(
                t("status.tick"),
                (m.c(T.SYM_OK, T.SUCCESS) + m.body(f" {t('status.tick_installed')}, roda a cada minuto"))
                if tick
                else m.c(f"{T.SYM_FAIL} {t('status.tick_missing')}, {t('status.tick_missing_loud')}", T.DANGER),
            ),
            linha(t("status.queue"), m.body("vazia, nada esperando" if fila == 0 else f"{fila} esperando")),
            linha(
                t("dash.jobs"),
                m.body(f"{total} cadastrados, {ativos} ativos, {pausados} pausado")
                if total
                else m.muted("nenhum cadastrado ainda"),
            ),
            "",
        ]

        proxima = ctx.next_overall()
        if proxima is not None:
            view, segundos = proxima
            quando = view.next_at.strftime("%H:%M") if view.next_at else "?"
            saida.append(
                m.muted("próxima execução  ")
                + m.body(f"{view.name}, {_dia(view.next_at)} às {quando} ")
                + m.muted(f"(em {T.format_relative(segundos)})")
            )
        ultima = ctx.last_overall()
        if ultima is not None:
            saida.append(
                m.muted("última execução   ")
                + m.badge(ultima.result)
                + m.body(f" {ultima.job}, {T.format_bytes(ultima.bytes)} em {T.format_duration(ultima.duration)}")
            )
        return saida

    # ------------------------------------------------------------------
    def on_key(self, evento: events.Key) -> None:
        if evento.key == "q":
            self.app.exit()
            return
        self._entrar()

    def _entrar(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self.is_current:
            self.app.pop_screen()

    def action_sair(self) -> None:
        self.app.exit()


def _dia(quando: dt.datetime | None) -> str:
    if quando is None:
        return ""
    hoje = dt.date.today()
    if quando.date() == hoje:
        return "hoje"
    if quando.date() == hoje + dt.timedelta(days=1):
        return "amanhã"
    return quando.strftime("%d/%m")
