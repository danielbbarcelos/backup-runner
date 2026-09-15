"""Widgets que aparecem em toda tela: hero, barra de estado, estado vazio.

O hero tem duas linhas, não cinco. Num terminal de 32 linhas, um cabeçalho de
cinco custaria 15% da tela em toda navegação; o wordmark em blocos fica na
abertura, onde não há conteúdo para competir.
"""
from __future__ import annotations

import datetime as dt

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ... import APP_NAME, __version__
from ...i18n import t
from .. import markup as m
from .. import theme as T


class Hero(Static):
    """Cabeçalho de duas linhas mais régua."""

    DEFAULT_CSS = """
    Hero {
        height: 3;
        padding: 0 1;
        background: $br-bg;
    }
    """

    screen_title: reactive[str] = reactive("")
    right_top: reactive[str] = reactive("")
    right_bottom: reactive[str] = reactive("")

    def __init__(self, titulo: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.screen_title = titulo

    def render(self) -> str:
        largura = max(40, self.size.width - 2)
        esquerda_topo = m.primary(APP_NAME, bold=True) + " " + m.muted(f"v{__version__}")
        topo = _duas_colunas(f"{APP_NAME} v{__version__}", self.right_top, largura,
                             esquerda_render=esquerda_topo,
                             direita_render=m.muted(self.right_top))
        base = _duas_colunas(self.screen_title, self.right_bottom, largura,
                             esquerda_render=m.body(self.screen_title),
                             direita_render=m.muted(self.right_bottom))
        regua = m.dim("─" * largura)
        return f"{topo}\n{base}\n{regua}"


def _duas_colunas(esquerda: str, direita: str, largura: int, *, esquerda_render: str, direita_render: str) -> str:
    """Alinha a direita pela largura visível, não pela do markup."""
    espaco = largura - len(esquerda) - len(direita)
    if espaco < 1:
        return esquerda_render
    return esquerda_render + " " * espaco + direita_render


class StatusBar(Widget):
    """Barra inferior: uma linha de estado, uma linha de teclas.

    Quando o tick não está instalado ela troca de conteúdo e ocupa a linha
    inteira em danger, com a tecla de conserto ao lado. Grita uma linha, não
    uma tela: banner em tela de 32 linhas rouba espaço em todas as outras.
    """

    DEFAULT_CSS = """
    StatusBar {
        height: 3;
        dock: bottom;
        background: $br-surface;
        padding: 0 1;
    }
    """

    worker_state: reactive[str] = reactive("ativo")
    queue: reactive[int] = reactive(0)
    tick: reactive[bool] = reactive(True)
    staging_free: reactive[str] = reactive("")
    health_ok: reactive[bool] = reactive(True)
    detail: reactive[str] = reactive("")
    hints: reactive[str] = reactive("")

    def render(self) -> str:
        largura = max(40, self.size.width - 2)
        regua = m.dim("─" * largura)
        agora = dt.datetime.now().strftime("%H:%M")

        if not self.tick:
            estado = (
                m.c(f"{T.SYM_FAIL} {t('status.tick')} {t('status.tick_missing')}", T.DANGER, bold=True)
                + "  " + m.c(t("status.tick_missing_loud"), T.DANGER)
                + "   " + m.key("i", t("key.install_tick"))
                + "  " + m.key("s", t("key.health"))
                + "   " + m.muted(f"{t('status.worker')} ") + self._worker_chunk()
            )
        else:
            partes = [
                m.muted(t("status.worker") + " ") + self._worker_chunk(),
                m.muted(f"{t('status.queue')} ") + m.body(str(self.queue)),
                m.muted(f"{t('status.tick')} ") + m.c(T.SYM_OK, T.SUCCESS) + m.muted(f" {t('status.tick_installed')}"),
            ]
            if self.staging_free:
                partes.append(m.muted(f"{t('status.staging')} {self.staging_free}"))
            if self.detail:
                partes.append(m.muted(self.detail))
            partes.append(
                m.muted(f"{t('status.health')} ")
                + (m.c(T.SYM_OK, T.SUCCESS) if self.health_ok else m.c(T.SYM_WARN, T.WARNING))
            )
            estado = "   ".join(partes)

        estado = estado + "   " + m.muted(agora)
        return f"{regua}\n {estado}\n {self.hints}"

    def _worker_chunk(self) -> str:
        if self.worker_state == "rodando":
            return m.c(T.SYM_RUNNING, T.PRIMARY) + m.muted(f" {t('status.worker_running')}")
        if self.worker_state == "parado":
            return m.c(T.SYM_FAIL, T.DANGER) + m.c(f" {t('status.worker_stopped')}", T.DANGER)
        return m.c(T.SYM_ACTIVE, T.SUCCESS) + m.muted(f" {t('status.worker_active')}")


class EmptyState(Static):
    """Padrão para qualquer lista vazia.

    Três partes fixas: a frase que diz o que falta, a frase que diz quando ou
    como aquilo aparece, e uma tecla que resolve. Nunca danger, nunca só a
    palavra "vazio": lista vazia é estado esperado, não erro.
    """

    def __init__(self, titulo: str, quando: str = "", tecla: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._titulo = titulo
        self._quando = quando
        self._tecla = tecla

    def render(self) -> str:
        linhas = ["", m.body(self._titulo), ""]
        if self._quando:
            linhas.append(m.muted("    " + self._quando))
        if self._tecla:
            linhas.append(m.muted("    ") + self._tecla)
        linhas.append("")
        return "\n".join(linhas)


class Rule(Static):
    """Divisória horizontal com rótulo opcional."""

    def __init__(self, rotulo: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._rotulo = rotulo

    def render(self) -> str:
        largura = max(10, self.size.width)
        if not self._rotulo:
            return m.dim("─" * largura)
        texto = f" {self._rotulo} "
        resto = max(0, largura - len(texto) - 2)
        return m.dim("─") + m.secondary(texto) + m.dim("─" * resto)


class InlineMessage(Static):
    """Uma linha: símbolo, texto, sem caixa em volta."""

    level: reactive[str] = reactive("info")
    text: reactive[str] = reactive("")

    def __init__(self, nivel: str = "info", texto: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.level = nivel
        self.text = texto

    def render(self) -> str:
        return m.inline(self.level, self.text) if self.text else ""

    def show(self, nivel: str, texto: str) -> None:
        self.level = nivel
        self.text = texto
        self.display = bool(texto)


class DynamicText(Static):
    """Static cujo conteúdo é montado com a largura real do widget.

    Existe porque régua, truncamento e alinhamento à direita precisam saber
    quantas células há de fato. Guardar o texto pronto significa recalcular em
    todo resize, ou aceitar que a linha quebre quando a janela encolhe.
    """

    def __init__(self, montar, **kwargs) -> None:
        super().__init__(markup=True, **kwargs)
        self._montar = montar

    def render(self) -> str:
        return self._montar(max(10, self.size.width))

    def on_resize(self) -> None:
        self.refresh(layout=True)

    def rebuild(self) -> None:
        """Repinta e remede.

        `refresh()` sozinho só repinta: a altura continua a do conteúdo
        anterior, e um texto que cresceu fica cortado sem erro nenhum.
        """
        self.refresh(layout=True)
