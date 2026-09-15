"""Aplicação Textual.

A navegação segue o mapa do design: a abertura cai no dashboard, e o dashboard
é o centro para onde tudo volta. `esc` sobe um nível e nunca fecha o app; `q`
só encerra a partir do dashboard, e pede confirmação se algo estiver rodando.
"""
from __future__ import annotations

from textual.app import App

from .. import APP_SLUG
from ..i18n import t
from .context import Context
from .theme import css_variables


class BackupRunnerApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "backup-runner"

    def __init__(self, *, skip_splash: bool = False, tela: str | None = None) -> None:
        super().__init__()
        self.ctx = Context()
        self._skip_splash = skip_splash
        self._tela_inicial = tela

    def get_css_variables(self) -> dict[str, str]:
        variaveis = super().get_css_variables()
        variaveis.update(css_variables())
        return variaveis

    def on_mount(self) -> None:
        from .screens.dashboard import DashboardScreen
        from .screens.splash import SplashScreen

        self.push_screen(DashboardScreen())
        if self._tela_inicial:
            self._abrir_inicial(self._tela_inicial)
        elif not self._skip_splash:
            self.push_screen(SplashScreen())

    def _abrir_inicial(self, nome: str) -> None:
        from .screens.destinations import DestinationsScreen
        from .screens.health import HealthScreen
        from .screens.history import HistoryScreen
        from .screens.notifications import NotificationsScreen

        telas = {
            "destinos": DestinationsScreen,
            "saude": HealthScreen,
            "historico": HistoryScreen,
            "avisos": NotificationsScreen,
        }
        fabrica = telas.get(nome)
        if fabrica is not None:
            self.push_screen(fabrica())

    # ------------------------------------------------------------------
    def refresh_context(self) -> None:
        self.ctx.refresh()
        for tela in self.screen_stack:
            atualizar = getattr(tela, "refresh_data", None)
            if callable(atualizar):
                atualizar()


def run(**kwargs) -> None:
    BackupRunnerApp(**kwargs).run()
