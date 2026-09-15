"""Saúde do sistema.

Cada item responde na mesma ordem: em que estado está, por que isso importa, e
o comando exato que conserta. O tick vem primeiro porque é a falha que não
gera erro nenhum: sem ele o app parece saudável e nada roda.
"""
from __future__ import annotations

import datetime as dt

from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import ListItem, ListView, Static

from ...clipboard import copy as copiar_texto
from ...health import HealthItem, Level, install_tick, summary, supervisor_conf
from ...i18n import t
from .. import markup as m
from .. import theme as T
from ..widgets import Hero, StatusBar

SIMBOLOS = {
    Level.OK: (T.SYM_OK, T.SUCCESS),
    Level.WARN: (T.SYM_WARN, T.WARNING),
    Level.FAIL: (T.SYM_FAIL, T.DANGER),
}


class HealthRow(ListItem):
    def __init__(self, item: HealthItem, **kwargs) -> None:
        super().__init__(**kwargs)
        self.item = item

    def compose(self) -> ComposeResult:
        yield HealthCell(self.item)

    def refresh_row(self) -> None:
        self.query_one(HealthCell).refresh()


class HealthCell(Static):
    def __init__(self, item: HealthItem, **kwargs) -> None:
        super().__init__(markup=True, **kwargs)
        self.item = item

    def render(self) -> str:
        item = self.item
        pai = self.parent
        foco = bool(getattr(pai, "highlighted", False))
        marca = m.c(T.SYM_HINT, T.PRIMARY) if foco else " "
        simbolo, cor = SIMBOLOS[item.level]
        titulo = item.title.ljust(30)
        linhas = [f"{marca} {m.c(simbolo, cor)} {m.body(titulo, bold=foco)}{m.muted(item.detail)}"]
        if item.why:
            linhas.append("     " + m.muted(item.why))
        for extra in item.extra:
            linhas.append("     " + m.muted(extra))
        if item.progress is not None:
            linhas.append("     " + m.bar(item.progress, 44))
        if item.fix_command:
            linhas.append("     " + m.secondary(t("health.fix").ljust(9)) + m.body(item.fix_command))
            teclas = []
            if item.fix_key:
                teclas.append(m.key(item.fix_key, f" {t('key.fix')}"))
            teclas.append(m.key("c", f" {t('key.copy')}"))
            linhas.append("               " + "     ".join(teclas))
        linhas.append("")
        return "\n".join(linhas)


class HealthScreen(Screen):
    CSS = """
    HealthScreen { background: $br-bg; }
    #lista-saude { height: 1fr; border: round $br-border; padding: 0 1; }
    """

    BINDINGS = [
        ("up,k", "mover(-1)", "item"),
        ("down,j", "mover(1)", "item"),
        ("enter,i", "consertar", "consertar"),
        ("c", "copiar", "copiar comando"),
        ("v", "reverificar", "verificar tudo de novo"),
        ("escape,q", "voltar", "voltar"),
        ("question_mark", "ajuda", "ajuda"),
    ]

    def compose(self) -> ComposeResult:
        yield Hero(t("screen.health"), id="hero")
        yield ListView(id="lista-saude")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        ctx.invalidate_health()
        itens = ctx.health
        ok, warn, fail = summary(itens)

        lista = self.query_one("#lista-saude", ListView)
        indice = lista.index or 0
        lista.clear()
        for item in itens:
            lista.append(HealthRow(item))
        lista.index = min(indice, max(0, len(itens) - 1))

        hero = self.query_one("#hero", Hero)
        hero.right_top = (
            f"{t('health.items', n=len(itens))}     "
            f"{T.SYM_OK} {ok}     {T.SYM_WARN} {warn}     {T.SYM_FAIL} {fail}"
        )
        hero.right_bottom = t("health.checked_now", hora=dt.datetime.now().strftime("%H:%M"))
        hero.refresh()

        barra = self.query_one("#status", StatusBar)
        barra.worker_state = "ativo" if ctx.worker.running else "parado"
        barra.queue = ctx.queue_size
        barra.tick = ctx.tick_ok
        barra.staging_free = t("status.staging_free", size=T.format_bytes(ctx.staging.free))
        barra.health_ok = fail == 0
        barra.hints = m.keys(
            ("↑↓", t("key.item")), ("enter", t("key.fix")), ("c", t("key.copy_cmd")),
            ("v", t("key.recheck")), ("esc", t("key.back")),
        )
        barra.refresh()

    @property
    def item_atual(self) -> HealthItem | None:
        item = self.query_one("#lista-saude", ListView).highlighted_child
        return item.item if isinstance(item, HealthRow) else None

    @on(ListView.Highlighted)
    def _mudou_item(self) -> None:
        for linha in self.query(HealthRow):
            linha.refresh_row()

    def action_mover(self, passo: int) -> None:
        lista = self.query_one("#lista-saude", ListView)
        lista.action_cursor_up() if passo < 0 else lista.action_cursor_down()

    def action_consertar(self) -> None:
        item = self.item_atual
        if item is None:
            return
        if item.key == "tick":
            ok, mensagem = install_tick()
            self.notify(mensagem, severity="information" if ok else "error")
            self.refresh_data()
            return
        if item.key == "worker":
            self._mostrar_conf_supervisor()
            return
        if item.fix_command:
            self.notify(
                f"rode você mesmo: {item.fix_command}",
                severity="warning",
                title="o app não executa sudo por conta própria",
            )

    def _mostrar_conf_supervisor(self) -> None:
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile("w", suffix=".conf", prefix="backup-runner-", delete=False) as f:
            f.write(supervisor_conf())
            caminho = f.name
        self.notify(
            f"arquivo gerado em {caminho}\n"
            f"sudo cp {caminho} /etc/supervisor/conf.d/\n"
            "sudo supervisorctl reread && sudo supervisorctl update",
            severity="information",
            title="leia antes de instalar um serviço que roda para sempre",
            timeout=20,
        )

    def action_copiar(self) -> None:
        item = self.item_atual
        if item is None or not item.fix_command:
            self.notify("este item não tem comando de conserto", severity="warning")
            return
        _copiar(self, item.fix_command, "comando")

    def action_reverificar(self) -> None:
        self.refresh_data()
        self.notify("verificado de novo", severity="information")

    def action_voltar(self) -> None:
        self.app.pop_screen()

    def action_ajuda(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen(self.BINDINGS, t("screen.health")))


def _copiar(tela, texto: str, rotulo: str) -> None:
    """Copia e diz a verdade sobre o resultado.

    Quando não há ferramenta na máquina, mostra o texto numa notificação longa
    para a pessoa copiar com o mouse, junto com o comando que resolve de vez.
    """
    resultado = copiar_texto(texto, app=tela.app)
    if resultado.ok:
        tela.notify(f"{rotulo} copiado via {resultado.via}", severity="information")
        return
    tela.notify(
        f"{texto}\n\n{resultado.erro}."
        + (f"\ninstale com: {resultado.sugestao}" if resultado.sugestao else ""),
        title="copie daqui com o mouse",
        severity="warning",
        timeout=30,
    )
