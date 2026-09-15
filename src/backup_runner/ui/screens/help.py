"""Ajuda sobreposta.

Lista as teclas da tela de onde foi chamada, não um manual genérico. Quem
aperta `?` está perdido naquela tela, não no app inteiro.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ...i18n import t
from .. import markup as m

DESCRICOES = {
    "up,k": "sobe na lista",
    "down,j": "desce na lista",
    "tab": "alterna o painel em foco",
    "enter": "abre o item",
    "escape": "volta um nível, nunca fecha o app",
    "n": "cria um job novo",
    "r": "enfileira o job agora, sem esperar a hora",
    "p": "pausa ou retoma o job na hora",
    "d": "apaga o job, com confirmação por nome",
    "h": "abre o histórico completo",
    "t": "abre os destinos",
    "a": "abre a matriz de avisos",
    "s": "abre a saúde do sistema",
    "i": "instala a linha do tick no crontab",
    "slash": "busca na lista",
    "question_mark": "esta ajuda",
    "q": "sai do app, a partir do dashboard",
}


class HelpScreen(ModalScreen[None]):
    CSS = """
    HelpScreen { align: center middle; }
    #caixa { width: 74; height: auto; max-height: 80%; border: round $br-primary;
             background: $br-surface; padding: 1 2; }
    """

    BINDINGS = [("escape,question_mark,q", "fechar", "fechar")]

    def __init__(self, bindings: list, titulo: str) -> None:
        super().__init__()
        self._bindings_tela = bindings
        self._titulo = titulo

    def compose(self) -> ComposeResult:
        with Vertical(id="caixa"):
            yield VerticalScroll(Static(self._texto(), markup=True))

    def _texto(self) -> str:
        linhas = [
            "",
            m.primary(f"Teclas de {self._titulo}", bold=True),
            "",
        ]
        for binding in self._bindings_tela:
            tecla = binding[0] if isinstance(binding, tuple) else str(binding)
            rotulo = binding[2] if isinstance(binding, tuple) and len(binding) > 2 else ""
            descricao = DESCRICOES.get(tecla, rotulo)
            mostrada = _legivel(tecla)
            linhas.append("  " + m.primary(mostrada.ljust(12), bold=True) + m.muted(descricao))
        linhas += [
            "",
            m.dim("─" * 68),
            "",
            m.muted("  esc volta um nível e nunca fecha o app."),
            m.muted("  q só encerra a partir do dashboard, e pergunta se algo estiver rodando."),
            "",
            "  " + m.key("esc", " fechar esta ajuda"),
            "",
        ]
        return "\n".join(linhas)

    def action_fechar(self) -> None:
        self.dismiss(None)


def _legivel(tecla: str) -> str:
    mapa = {
        "up,k": "↑ / k",
        "down,j": "↓ / j",
        "slash": "/",
        "question_mark": "?",
        "escape": "esc",
        "left,h": "← / h",
        "right,l": "→ / l",
        "space": "espaço",
    }
    return mapa.get(tecla, tecla)
