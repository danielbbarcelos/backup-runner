"""Tokens semânticos e CSS base.

As cores vêm da especificação do Claude Design (projeto "backup-runner TUI").
Cada token tem um hex e o equivalente ANSI 256, para o caso de o terminal não
suportar cor de 24 bits.

Regra que a especificação trata como dura: `muted` é texto vivo (metadado
legível) e `disabled` é ausência de ação. Os dois convivem no mesmo fundo e
`disabled` fica na família lavanda fria, então nunca é lido como erro.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    hex: str
    ansi: int
    uso: str


TOKENS: dict[str, Token] = {
    "primary": Token("#b4a7ff", 147, "borda do painel em foco, item selecionado, tecla ativa, logo"),
    "secondary": Token("#87d7c0", 115, "rótulo de campo, cabeçalho de coluna, nome de destino, passo concluído"),
    "muted": Token("#8f8ba6", 103, "metadado legível: tamanho, duração, data, hint de tecla"),
    "disabled": Token("#56506e", 60, "job pausado, tecla indisponível, campo bloqueado"),
    "success": Token("#86d98b", 114, "execução ok, teste ok, item de saúde saudável"),
    "warning": Token("#e6c37a", 179, "atrasado, janela perdida, envio pendente, disco apertado"),
    "danger": Token("#f2798f", 204, "falha, erro de campo, confirmação destrutiva"),
    "bg": Token("#14131a", 234, "fundo da tela cheia"),
    "surface": Token("#1e1c27", 235, "modal, barra de estado, linha selecionada, aba ativa"),
    "border": Token("#3a3750", 238, "box-drawing em repouso, régua, divisória"),
    "text": Token("#d8d6e3", 253, "conteúdo: nome de job, valor de campo, corpo de log"),
}

PRIMARY = TOKENS["primary"].hex
SECONDARY = TOKENS["secondary"].hex
MUTED = TOKENS["muted"].hex
DISABLED = TOKENS["disabled"].hex
SUCCESS = TOKENS["success"].hex
WARNING = TOKENS["warning"].hex
DANGER = TOKENS["danger"].hex
BG = TOKENS["bg"].hex
SURFACE = TOKENS["surface"].hex
BORDER = TOKENS["border"].hex
TEXT = TOKENS["text"].hex


# ----------------------------------------------------------------------------
# Símbolos
# ----------------------------------------------------------------------------
# Cada estado tem símbolo próprio, então a cor é reforço e nunca a informação:
# a interface continua legível em terminal sem cor e para quem não distingue
# as matizes.

SYM_OK = "✓"
SYM_FAIL = "✗"
SYM_WARN = "!"
SYM_PENDING = "↑"
SYM_PAUSED = "⏸"
SYM_RUNNING = "◐"
SYM_ACTIVE = "●"
SYM_INACTIVE = "○"
SYM_HINT = "›"
SYM_CURSOR = "▏"
SYM_FIELD = "▌"
SYM_NONE = "·"  # valor ausente, uma célula

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ARC = "◐◓◑◒"

CHECK_MANUAL = "[✓]"   # marcado à mão
CHECK_RULE = "[▪]"     # marcado por regra (regex)
CHECK_OFF = "[ ]"      # não marcado

BAR_FULL = "█"
BAR_EMPTY = "░"


def progress_bar(feito: int, total: int, largura: int = 24) -> str:
    """A barra em si, sem cor e sem número: só o desenho.

    Total zero significa "ainda não sei quanto é", e aí não há barra para
    desenhar, só espaço em branco: uma barra vazia parada mentiria dizendo
    zero por cento de um todo conhecido.

    A porcentagem do dump é estimada e às vezes passa de 100 (o SQL em texto é
    maior que o dado em disco). A barra enche e para; quem mostra o número
    decide o que dizer.
    """
    if total <= 0:
        return " " * largura
    fracao = min(1.0, max(0.0, feito / total))
    cheias = int(round(fracao * largura))
    return BAR_FULL * cheias + BAR_EMPTY * (largura - cheias)


def eta_segundos(feito: int, total: int, segundos_decorridos: float) -> float | None:
    """Quantos segundos faltam, pela média até agora, ou None se não dá para dizer.

    Deliberadamente burro: média simples, sem suavizar. Uma estimativa que
    oscila avisa que a taxa está oscilando, o que é informação verdadeira.

    Devolve número, e não texto, porque quem chama precisa comparar isto com o
    prazo do job. Formatar cedo demais obrigaria a desformatar depois.
    """
    if total <= 0 or feito <= 0 or segundos_decorridos <= 0 or feito >= total:
        return None
    taxa = feito / segundos_decorridos
    if taxa <= 0:
        return None
    return (total - feito) / taxa


def format_eta(feito: int, total: int, segundos_decorridos: float) -> str:
    restam = eta_segundos(feito, total, segundos_decorridos)
    return format_relative(restam) if restam else ""


def format_rate(bytes_feitos: int, segundos: float) -> str:
    if segundos <= 0 or bytes_feitos <= 0:
        return ""
    return f"{format_bytes(int(bytes_feitos / segundos))}/s"


def css_variables() -> dict[str, str]:
    """Variáveis expostas ao CSS do Textual, com prefixo `br-`.

    O prefixo evita colidir com o design system embutido do Textual, cujos
    `$primary`/`$surface` têm semântica própria e são usados pelos widgets
    nativos.
    """
    return {f"br-{name}": token.hex for name, token in TOKENS.items()}


# ----------------------------------------------------------------------------
# Formatação
# ----------------------------------------------------------------------------
# O separador decimal é a vírgula, como na especificação ("2,1 GB").

def format_bytes(n: int | None) -> str:
    if n is None:
        return SYM_NONE
    if n < 1024:
        return f"{n} B"
    valor = float(n)
    for unidade in ("KB", "MB", "GB", "TB", "PB"):
        valor /= 1024
        if valor < 1024:
            texto = f"{valor:.1f}".replace(".", ",")
            return f"{texto} {unidade}"
    return f"{valor:.1f}".replace(".", ",") + " EB"


def format_duration(segundos: float | None) -> str:
    """Duração no formato da especificação: `4min 12s`, `0m 21s`, `6h 42min`."""
    if segundos is None:
        return SYM_NONE
    s = int(segundos)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min {s % 60:02d}s"
    h, resto = divmod(s, 3600)
    return f"{h}h {resto // 60:02d}min"


def format_relative(segundos: float | None) -> str:
    """Distância no tempo, para "próxima em 6h 42min"."""
    if segundos is None:
        return SYM_NONE
    s = int(abs(segundos))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min"
    if s < 86400:
        h, resto = divmod(s, 3600)
        return f"{h}h {resto // 60:02d}min"
    d, resto = divmod(s, 86400)
    return f"{d}d {resto // 3600}h"


def format_count(n: int) -> str:
    """Milhar com ponto, como na especificação ("12.481 arquivos")."""
    return f"{n:,}".replace(",", ".")
