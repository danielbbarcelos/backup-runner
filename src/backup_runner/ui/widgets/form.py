"""Campos de formulário na grade do terminal.

Rótulo à esquerda em `secondary`, marca `▌` que fica em `primary` quando o campo
tem foco, valor, e à direita uma dica em `disabled` que vira erro em `danger`.
Os quatro estados do design (repouso, foco, preenchido, erro) são o mesmo
widget mudando de cor, não quatro desenhos diferentes.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static

from .. import markup as m
from .. import theme as T


class Field(Horizontal):
    """Uma linha de formulário: rótulo, marca, entrada, dica."""

    DEFAULT_CSS = """
    Field { height: 1; }
    Field > .rotulo { width: 17; content-align: left middle; }
    Field > .marca { width: 2; content-align: left middle; }
    Field > Input { width: 1fr; min-width: 18; }
    Field > .dica { width: auto; max-width: 30; content-align: left middle; padding: 0 0 0 1; }
    """

    def __init__(
        self,
        rotulo: str,
        valor: str = "",
        *,
        dica: str = "",
        senha: bool = False,
        placeholder: str = "",
        campo_id: str = "",
        largura_dica: int = 34,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._rotulo = rotulo
        self._valor = valor
        self._dica = dica
        self._senha = senha
        self._placeholder = placeholder
        self._campo_id = campo_id or f"campo-{abs(hash(rotulo)) % 10000}"
        self._erro = ""
        self._largura_dica = largura_dica

    def compose(self) -> ComposeResult:
        yield Static(m.secondary(self._rotulo), classes="rotulo", markup=True)
        yield Static(m.c(T.SYM_FIELD, T.BORDER), classes="marca", markup=True)
        yield Input(
            value=self._valor,
            password=self._senha,
            placeholder=self._placeholder,
            id=self._campo_id,
        )
        yield Static(m.dim(self._dica), classes="dica", markup=True)

    def on_mount(self) -> None:
        # Um erro marcado antes da montagem (validação no carregamento do
        # formulário) só pode ser pintado agora, quando os filhos existem.
        if self._erro:
            self.set_error(self._erro)

    # ------------------------------------------------------------------
    @property
    def input(self) -> Input:
        return self.query_one(Input)

    @property
    def value(self) -> str:
        return self.input.value

    @value.setter
    def value(self, novo: str) -> None:
        self.input.value = novo

    def _pintar(self, seletor: str, texto: str) -> bool:
        alvo = self.query(seletor)
        if not alvo:
            return False
        alvo.first(Static).update(texto)
        return True

    def set_hint(self, texto: str) -> None:
        self._dica = texto
        self._erro = ""
        self._pintar(".dica", m.dim(texto))

    def set_error(self, texto: str) -> None:
        self._erro = texto
        if self._pintar(".dica", m.c(f"{T.SYM_FAIL} {texto}", T.DANGER)):
            self._pintar(".marca", m.c(T.SYM_FIELD, T.DANGER))

    def set_ok(self, texto: str) -> None:
        self._erro = ""
        self._pintar(".dica", m.c(f"{T.SYM_OK} {texto}", T.SUCCESS))

    def on_descendant_focus(self) -> None:
        self._pintar(".marca", m.c(T.SYM_FIELD, T.PRIMARY))

    def on_descendant_blur(self) -> None:
        cor = T.DANGER if self._erro else T.BORDER
        self._pintar(".marca", m.c(T.SYM_FIELD, cor))


class Choice(Widget):
    """Seleção única numa linha, trocada com as setas.

    Rádio em vez de lista suspensa porque com duas ou três opções o menu
    esconde as alternativas atrás de um enter, e aqui elas cabem na linha.
    """

    DEFAULT_CSS = """
    Choice { height: 1; }
    """

    def __init__(self, rotulo: str, opcoes: list[tuple[str, str]], valor: str, *, dica: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._rotulo = rotulo
        self.opcoes = opcoes
        self.valor = valor
        self._dica = dica
        self.can_focus = True

    def render(self) -> str:
        partes = []
        for chave, rotulo in self.opcoes:
            marca = m.radio(chave == self.valor)
            texto = m.body(rotulo) if chave == self.valor else m.muted(rotulo)
            partes.append(f"{marca} {texto}")
        marca_cor = T.PRIMARY if self.has_focus else T.BORDER
        linha = (
            m.secondary(self._rotulo.ljust(17))
            + m.c(T.SYM_FIELD, marca_cor) + " "
            + "     ".join(partes)
        )
        if self._dica:
            linha += "      " + m.dim(self._dica)
        return linha

    def on_key(self, evento) -> None:
        if evento.key in ("left", "right", "space"):
            indices = [c for c, _ in self.opcoes]
            atual = indices.index(self.valor) if self.valor in indices else 0
            passo = -1 if evento.key == "left" else 1
            self.valor = indices[(atual + passo) % len(indices)]
            self.refresh()
            self.post_message(Choice.Changed(self))
            evento.stop()

    class Changed(Message):
        def __init__(self, choice: "Choice") -> None:
            super().__init__()
            self.choice = choice
            self.value = choice.valor


class Toggle(Widget):
    """Marcação simples, ligada com espaço."""

    DEFAULT_CSS = """
    Toggle { height: 1; }
    """

    def __init__(self, rotulo: str, valor: bool = False, *, dica: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._rotulo = rotulo
        self.valor = valor
        self._dica = dica
        self.can_focus = True

    def render(self) -> str:
        marca_cor = T.PRIMARY if self.has_focus else T.BORDER
        linha = (
            m.secondary(self._rotulo.ljust(17))
            + m.c(T.SYM_FIELD, marca_cor) + " "
            + m.checkbox(self.valor) + " " + m.body("sim" if self.valor else "não")
        )
        if self._dica:
            linha += "      " + m.dim(self._dica)
        return linha

    def on_key(self, evento) -> None:
        if evento.key in ("space", "enter"):
            self.valor = not self.valor
            self.refresh()
            evento.stop()
