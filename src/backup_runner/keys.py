"""Leitura de tecla, uma de cada vez, com `termios`.

Só stdlib. O terminal entra em modo cru pelo tempo da leitura e volta ao normal
logo depois, inclusive quando dá erro ou Ctrl-C, senão o shell fica sem eco
depois que o programa sai.

Sem tty (num pipe, num cron, dentro de um teste) nada disso existe, e quem
chama precisa perceber isso: `disponivel()` responde, e a navegação por número
continua valendo como caminho principal.
"""
from __future__ import annotations

import os
import select
import sys

CIMA = "cima"
BAIXO = "baixo"
DIREITA = "direita"
ESQUERDA = "esquerda"
ENTER = "enter"
ESC = "esc"
ESPACO = "espaco"
BACKSPACE = "backspace"
TAB = "tab"
HOME = "home"
FIM = "fim"
PAGE_UP = "page_up"
PAGE_DOWN = "page_down"

# O que cada sequência de escape significa. O terminal manda ESC [ A para a
# seta de cima, e variações com O no lugar de [ em modo de aplicação.
SEQUENCIAS = {
    "[A": CIMA, "OA": CIMA,
    "[B": BAIXO, "OB": BAIXO,
    "[C": DIREITA, "OC": DIREITA,
    "[D": ESQUERDA, "OD": ESQUERDA,
    "[H": HOME, "OH": HOME, "[1~": HOME,
    "[F": FIM, "OF": FIM, "[4~": FIM,
    "[5~": PAGE_UP,
    "[6~": PAGE_DOWN,
}


def disponivel() -> bool:
    """Dá para ler tecla a tecla neste ambiente?"""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    if os.environ.get("BACKUP_RUNNER_SEM_SETAS"):
        return False
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


def ler() -> str:
    """Bloqueia até uma tecla, e devolve o nome dela ou o caractere digitado.

    Lê com `os.read` no descritor, e não com `sys.stdin.read`: o objeto de
    arquivo do Python enche um buffer interno de uma vez, então o `select` que
    verifica se a sequência de escape continua olha um descritor já vazio e
    conclui que a seta era a tecla Esc sozinha.

    Ctrl-C vira KeyboardInterrupt como em qualquer programa, e Ctrl-D vira
    EOFError, para quem chama tratar como trataria num `input()`.
    """
    import termios
    import tty

    fd = sys.stdin.fileno()
    antes = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = _byte(fd)

        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\x04", ""):
            raise EOFError
        if ch in ("\r", "\n"):
            return ENTER
        if ch == "\t":
            return TAB
        if ch == " ":
            return ESPACO
        if ch in ("\x7f", "\b"):
            return BACKSPACE
        if ch != "\x1b":
            return ch

        # Escape: ou a tecla sozinha, ou o começo de uma sequência.
        if not _tem_mais(fd):
            return ESC
        sequencia = _byte(fd)
        if sequencia in ("[", "O"):
            while _tem_mais(fd):
                sequencia += _byte(fd)
                if sequencia[-1].isalpha() or sequencia[-1] == "~":
                    break
        return SEQUENCIAS.get(sequencia, ESC)
    finally:
        # Sempre devolve o terminal, inclusive se estourar no meio.
        termios.tcsetattr(fd, termios.TCSADRAIN, antes)


def _byte(fd: int) -> str:
    dado = os.read(fd, 1)
    return dado.decode("utf-8", errors="replace") if dado else ""


def _tem_mais(fd: int, espera: float = 0.02) -> bool:
    pronto, _, _ = select.select([fd], [], [], espera)
    return bool(pronto)


# ----------------------------------------------------------------------------
# Cursor
# ----------------------------------------------------------------------------

def esconde_cursor() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()


def mostra_cursor() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def sobe(linhas: int) -> None:
    """Volta o cursor para o topo do bloco, para redesenhar por cima.

    Redesenhar só o bloco, em vez de limpar a tela inteira, é o que evita a
    piscada a cada seta.
    """
    if linhas > 0 and sys.stdout.isatty():
        sys.stdout.write(f"\033[{linhas}A")
        sys.stdout.flush()


def limpa_abaixo() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[J")
        sys.stdout.flush()
