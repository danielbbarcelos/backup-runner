"""Saída no terminal: cor, símbolo, tabela, cabeçalho.

Só stdlib e sequências ANSI. Nada de framework de interface: o que sai daqui é
texto que qualquer terminal imprime, que `grep` filtra e que `tee` guarda num
arquivo.

Cor é sempre reforço, nunca a informação. Cada estado tem símbolo próprio, e
com `NO_COLOR` definido ou fora de um terminal a saída continua completa, o que
também é o que faz `backup-runner jobs | grep falha` funcionar num cron.
"""
from __future__ import annotations

import os
import shutil
import sys

# ----------------------------------------------------------------------------
# Cor
# ----------------------------------------------------------------------------

def _usar_cor() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


COR = _usar_cor()

RESET = "\033[0m" if COR else ""
BOLD = "\033[1m" if COR else ""
DIM = "\033[2m" if COR else ""

# A mesma paleta do desenho original, em ANSI 256.
PRIMARY = "\033[38;5;147m" if COR else ""    # lavanda
SECONDARY = "\033[38;5;115m" if COR else ""  # verde-água
MUTED = "\033[38;5;103m" if COR else ""
DISABLED = "\033[38;5;60m" if COR else ""
SUCCESS = "\033[38;5;114m" if COR else ""
WARNING = "\033[38;5;179m" if COR else ""
DANGER = "\033[38;5;204m" if COR else ""

SYM_OK = "✓"
SYM_FAIL = "✗"
SYM_WARN = "!"
SYM_PENDING = "↑"
SYM_PAUSED = "⏸"
SYM_RUNNING = "◐"
SYM_ACTIVE = "●"
SYM_INACTIVE = "○"
SYM_NONE = "·"


def cor(texto: object, c: str, *, bold: bool = False) -> str:
    if not COR:
        return str(texto)
    return f"{BOLD if bold else ''}{c}{texto}{RESET}"


def primary(t: object, *, bold: bool = False) -> str:
    return cor(t, PRIMARY, bold=bold)


def secondary(t: object) -> str:
    return cor(t, SECONDARY)


def muted(t: object) -> str:
    return cor(t, MUTED)


def dim(t: object) -> str:
    return cor(t, DISABLED)


def ok(t: object) -> str:
    return cor(t, SUCCESS)


def warn(t: object) -> str:
    return cor(t, WARNING)


def danger(t: object) -> str:
    return cor(t, DANGER)


def bold(t: object) -> str:
    return f"{BOLD}{t}{RESET}" if COR else str(t)


def largura(padrao: int = 80) -> int:
    try:
        return shutil.get_terminal_size().columns
    except OSError:
        return padrao


def visivel(texto: str) -> int:
    """Comprimento sem contar as sequências de escape."""
    import re

    return len(re.sub(r"\033\[[0-9;]*m", "", texto))


# ----------------------------------------------------------------------------
# Blocos
# ----------------------------------------------------------------------------

def titulo(texto: str, *, sub: str = "") -> None:
    print()
    print(primary(texto, bold=True) + ("   " + muted(sub) if sub else ""))
    print(dim("─" * min(largura() - 1, max(len(texto) + len(sub) + 4, 40))))


def secao(texto: str) -> None:
    print()
    print(secondary(texto))


def linha(rotulo: str, valor: object, *, largura_rotulo: int = 14) -> None:
    print(f"  {secondary(rotulo.ljust(largura_rotulo))} {valor}")


def item(marca: str, texto: str, *, indent: int = 2) -> None:
    print(" " * indent + f"{marca} {texto}")


def aviso(texto: str) -> None:
    print(f"  {warn(SYM_WARN)} {warn(texto)}")


def erro(texto: str) -> None:
    print(f"  {danger(SYM_FAIL)} {danger(texto)}", file=sys.stderr)


def sucesso(texto: str) -> None:
    print(f"  {ok(SYM_OK)} {texto}")


def info(texto: str) -> None:
    print(f"  {muted('›')} {muted(texto)}")


def nota(texto: str) -> None:
    print(f"  {dim(texto)}")


def vazio(titulo_: str, sugestao: str = "") -> None:
    """Lista vazia é estado esperado, não erro: diz o que falta e como resolver."""
    print()
    print(f"  {titulo_}")
    if sugestao:
        print(f"  {muted(sugestao)}")
    print()


# ----------------------------------------------------------------------------
# Tabela
# ----------------------------------------------------------------------------

def tabela(
    cabecalho: list[str],
    linhas: list[list[str]],
    *,
    alinhamento: str = "",
    indent: int = 2,
) -> None:
    """Tabela de largura fixa, alinhada pelo conteúdo visível.

    `alinhamento` é uma string com `l` ou `r` por coluna. Colunas de número
    ficam à direita, que é o que deixa tamanhos e durações comparáveis de
    relance.
    """
    if not linhas:
        return
    colunas = len(cabecalho)
    larguras = [visivel(c) for c in cabecalho]
    for linha_ in linhas:
        for i, celula in enumerate(linha_[:colunas]):
            larguras[i] = max(larguras[i], visivel(celula))

    alinhamento = (alinhamento + "l" * colunas)[:colunas]
    prefixo = " " * indent

    def formata(celula: str, i: int) -> str:
        preenche = larguras[i] - visivel(celula)
        return (" " * preenche + celula) if alinhamento[i] == "r" else (celula + " " * preenche)

    print(prefixo + "  ".join(secondary(formata(c, i)) for i, c in enumerate(cabecalho)))
    print(prefixo + dim("─" * (sum(larguras) + 2 * (colunas - 1))))
    for linha_ in linhas:
        celulas = list(linha_[:colunas]) + [""] * (colunas - len(linha_))
        print(prefixo + "  ".join(formata(c, i) for i, c in enumerate(celulas)))


def barra(fracao: float, comprimento: int = 30) -> str:
    fracao = max(0.0, min(1.0, fracao))
    cheios = int(round(fracao * comprimento))
    return primary("█" * cheios) + dim("░" * (comprimento - cheios))


# ----------------------------------------------------------------------------
# Tela
# ----------------------------------------------------------------------------

def limpa() -> None:
    """Limpa a tela e leva o cursor ao topo.

    Só num terminal: num pipe ou num cron, uma sequência de escape no meio da
    saída seria lixo no arquivo.
    """
    import sys

    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def caixa(linhas: list[str], *, cor_borda: str = PRIMARY, largura_interna: int | None = None) -> list[str]:
    """Envolve as linhas numa borda arredondada, alinhando pelo texto visível."""
    conteudo = max((visivel(l) for l in linhas), default=0)
    interna = largura_interna or (conteudo + 2)
    topo = cor(f"╭{'─' * interna}╮", cor_borda)
    base = cor(f"╰{'─' * interna}╯", cor_borda)
    lado = cor("│", cor_borda)
    saida = [topo]
    for l in linhas:
        preenche = interna - visivel(l) - 1
        saida.append(f"{lado} {l}{' ' * max(0, preenche)}{lado}")
    saida.append(base)
    return saida


# ----------------------------------------------------------------------------
# Abertura
# ----------------------------------------------------------------------------

def hero(versao: str, tagline: str, *, estado: str = "") -> None:
    """Cabeçalho compacto, dentro de uma caixa.

    O wordmark em blocos ocupava doze linhas, que é quase metade de um terminal
    curto e reaparecia a cada navegação. Aqui são quatro, com a marca à
    esquerda e o estado do sistema à direita.
    """
    marca = primary("●─╮", bold=True)
    marca2 = primary("├─●", bold=True)
    marca3 = primary("●─╯", bold=True)
    nome = bold(primary("backup-runner")) + "  " + muted(f"v{versao}")
    linhas = [
        f"{marca}   {nome}",
        f"{marca2}   {muted(tagline)}",
        f"{marca3}   {estado}" if estado else f"{marca3}",
    ]
    for l in caixa(linhas, largura_interna=max(46, min(largura() - 4, 72))):
        print(l)
