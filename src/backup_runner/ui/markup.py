"""Helpers de markup Rich, na linguagem dos tokens.

Nenhuma tela escreve cor crua: pede `badge()`, `key()`, `inline()`. Isso é o que
mantém a regra do design de que a cor é reforço e o símbolo é a informação, e
faz uma mudança de paleta acontecer num arquivo só.
"""
from __future__ import annotations

from ..i18n import t
from ..models import RunResult
from . import theme as T


def c(texto: object, cor: str, *, bold: bool = False) -> str:
    estilo = f"bold {cor}" if bold else cor
    return f"[{estilo}]{texto}[/]"


def muted(texto: object) -> str:
    return c(texto, T.MUTED)


def dim(texto: object) -> str:
    return c(texto, T.DISABLED)


def primary(texto: object, *, bold: bool = False) -> str:
    return c(texto, T.PRIMARY, bold=bold)


def secondary(texto: object) -> str:
    return c(texto, T.SECONDARY)


def body(texto: object, *, bold: bool = False) -> str:
    return c(texto, T.TEXT, bold=bold)


# ----------------------------------------------------------------------------
# Badges
# ----------------------------------------------------------------------------
# Cada resultado tem símbolo próprio antes da palavra. Em terminal sem cor, ou
# para quem não distingue as matizes, o símbolo continua dizendo tudo.

_BADGES: dict[RunResult, tuple[str, str, str]] = {
    RunResult.OK: (T.SYM_OK, "badge.ok", T.SUCCESS),
    RunResult.FAILED: (T.SYM_FAIL, "badge.failed", T.DANGER),
    RunResult.LATE: (T.SYM_WARN, "badge.late", T.WARNING),
    RunResult.PENDING_UPLOAD: (T.SYM_PENDING, "badge.pending_upload", T.WARNING),
    RunResult.MISSED: (T.SYM_WARN, "badge.missed", T.WARNING),
    RunResult.SKIPPED: (T.SYM_PAUSED, "badge.paused", T.DISABLED),
    RunResult.RUNNING: (T.SYM_RUNNING, "badge.running", T.PRIMARY),
    RunResult.QUEUED: (T.SYM_ACTIVE, "badge.queued", T.SECONDARY),
}


def badge(resultado: RunResult | None, *, sem_cor: bool = False) -> str:
    if resultado is None:
        return muted(T.SYM_NONE)
    simbolo, chave, cor = _BADGES[resultado]
    texto = f"{simbolo} {t(chave)}"
    return texto if sem_cor else c(texto, cor)


def badge_plain(resultado: RunResult | None) -> str:
    if resultado is None:
        return T.SYM_NONE
    simbolo, chave, _ = _BADGES[resultado]
    return f"{simbolo} {t(chave)}"


def badge_parts(resultado: RunResult | None) -> tuple[str, str, str]:
    """(símbolo, rótulo traduzido, cor). O rótulo já vem pronto para a tela.

    O dicionário guarda a chave de i18n, não o texto, então quem monta célula
    de tabela precisa passar por aqui em vez de ler a tupla crua.
    """
    if resultado is None:
        return (T.SYM_NONE, "", T.MUTED)
    simbolo, chave, cor = _BADGES[resultado]
    return simbolo, t(chave), cor


def result_color(resultado: RunResult | None) -> str:
    return _BADGES[resultado][2] if resultado else T.MUTED


def dot(ativo: bool) -> str:
    """Marca de job ativo ou pausado na lista."""
    return c(T.SYM_ACTIVE, T.SUCCESS) if ativo else c(T.SYM_INACTIVE, T.DISABLED)


# ----------------------------------------------------------------------------
# Teclas
# ----------------------------------------------------------------------------

def key(tecla: str, rotulo: str, *, enabled: bool = True, destrutiva: bool = False, motivo: str = "") -> str:
    """Hint de tecla.

    Disponível: tecla em primary, rótulo em muted, sem espaço entre os dois.
    Indisponível: tudo em disabled, e a tecla continua visível, porque sumir
    mudaria o mapa de teclas de uma tela para a outra.
    Destrutiva: é a única que usa danger no hint.
    """
    if not enabled:
        extra = dim(f" ({motivo})") if motivo else ""
        return dim(tecla) + dim(rotulo) + extra
    cor_tecla = T.DANGER if destrutiva else T.PRIMARY
    return c(tecla, cor_tecla, bold=True) + muted(rotulo)


def keys(*pares: tuple[str, str] | tuple[str, str, bool]) -> str:
    partes = []
    for par in pares:
        tecla, rotulo = par[0], par[1]
        ativo = par[2] if len(par) > 2 else True
        partes.append(key(tecla, rotulo, enabled=bool(ativo)))
    return " ".join(partes)


# ----------------------------------------------------------------------------
# Mensagem inline
# ----------------------------------------------------------------------------
# Uma linha, símbolo na coluna 2, texto na coluna 4, sem caixa em volta.

def inline(nivel: str, texto: str) -> str:
    mapa = {
        "info": (T.SYM_HINT, T.MUTED),
        "warn": (T.SYM_WARN, T.WARNING),
        "error": (T.SYM_FAIL, T.DANGER),
        "ok": (T.SYM_OK, T.SUCCESS),
    }
    simbolo, cor = mapa.get(nivel, mapa["info"])
    return f" {c(simbolo, cor)} {c(texto, cor if nivel != 'info' else T.MUTED)}"


# ----------------------------------------------------------------------------
# Marcações de lista
# ----------------------------------------------------------------------------

def check(marcado_mao: bool, por_regra: bool) -> str:
    """Marca de tabela: à mão, por regra, ou fora.

    Formas diferentes de propósito. Sem isso, a pessoa não consegue saber se
    uma tabela está ignorada porque ela escolheu ou porque a regex pegou, que é
    exatamente a dúvida que faz alguém desmarcar a tabela errada.
    """
    if marcado_mao:
        return c(T.CHECK_MANUAL, T.PRIMARY)
    if por_regra:
        return c(T.CHECK_RULE, T.SECONDARY)
    return dim(T.CHECK_OFF)


def checkbox(marcado: bool, *, bloqueado: bool = False) -> str:
    if bloqueado:
        return dim(T.CHECK_OFF)
    return c(T.CHECK_MANUAL, T.PRIMARY) if marcado else dim(T.CHECK_OFF)


def radio(marcado: bool) -> str:
    return c("(•)", T.PRIMARY) if marcado else dim("( )")


# ----------------------------------------------------------------------------
# Barras
# ----------------------------------------------------------------------------

def bar(fracao: float, largura: int = 40) -> str:
    """Barra determinada."""
    fracao = max(0.0, min(1.0, fracao))
    cheios = int(round(fracao * largura))
    return c(T.BAR_FULL * cheios, T.PRIMARY) + dim(T.BAR_EMPTY * (largura - cheios))


def bar_indeterminate(posicao: int, largura: int = 40) -> str:
    """Barra indeterminada: o par ▌▐ caminha de ponta a ponta.

    Sem total conhecido não existe porcentagem, e inventar uma seria mentira
    sobre o que falta. O que prova vida aqui é o movimento mais o carimbo de
    hora da última linha de log, porque numa espera de 40 minutos uma barra
    parada e uma barra travada são idênticas na tela.
    """
    largura = max(4, largura)
    ciclo = (largura - 1) * 2
    p = posicao % ciclo
    if p >= largura - 1:
        p = ciclo - p
    esquerda = dim(T.BAR_EMPTY * p)
    direita = dim(T.BAR_EMPTY * (largura - p - 2))
    return esquerda + c("▌▐", T.PRIMARY) + direita


def spinner(quadro: int) -> str:
    return c(T.SPINNER_ARC[quadro % len(T.SPINNER_ARC)], T.PRIMARY)


# ----------------------------------------------------------------------------
# Campos
# ----------------------------------------------------------------------------

def field_line(rotulo: str, valor: str, *, largura: int = 16, hint: str = "", erro: str = "", foco: bool = False) -> str:
    marca = c(T.SYM_FIELD, T.PRIMARY if foco else T.BORDER)
    texto_valor = body(valor) if valor else dim("(vazio)")
    if foco:
        texto_valor = body(valor) + c(T.SYM_CURSOR, T.PRIMARY)
    linha = f"{secondary(rotulo.ljust(largura))} {marca} {texto_valor}"
    if erro:
        linha += "   " + c(f"{T.SYM_FAIL} {erro}", T.DANGER)
    elif hint:
        linha += "   " + dim(hint)
    return linha
