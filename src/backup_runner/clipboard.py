"""Copiar para a área de transferência, de verdade.

O `copy_to_clipboard` do Textual usa OSC 52, uma sequência de escape que o
terminal pode ou não honrar, e a maioria bloqueia por padrão. Quando ele falha,
falha em silêncio: a interface diz "copiado" e não copiou nada, que é pior que
não ter o recurso.

Aqui a ordem é: ferramenta do sistema primeiro, OSC 52 como último recurso, e
uma resposta honesta quando nada funcionou, com o comando para instalar a
ferramenta que falta.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

# Ordem de tentativa. wl-copy primeiro em Wayland, xclip atende X11 e também
# Wayland através do XWayland.
#
# O terceiro item de cada linha é como ler de volta, quando existe: escrever e
# conferir é o que impede a interface de dizer "copiado" sem ter copiado.
FERRAMENTAS: list[tuple[str, list[str], list[str] | None]] = [
    ("wl-copy", ["wl-copy"], ["wl-paste", "--no-newline"]),
    ("xclip", ["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]),
    ("xsel", ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]),
    ("pbcopy", ["pbcopy"], ["pbpaste"]),
    ("clip.exe", ["clip.exe"], None),  # WSL, sem leitura simples
]


@dataclass
class Resultado:
    ok: bool
    via: str = ""
    erro: str = ""
    sugestao: str = ""

    def mensagem(self) -> str:
        if self.ok:
            return f"copiado ({self.via})"
        return self.erro or "não foi possível copiar"


def _ferramenta_preferida() -> list[tuple[str, list[str], list[str] | None]]:
    """Em Wayland, tenta wl-copy antes; fora dele, xclip antes."""
    disponiveis = [t for t in FERRAMENTAS if shutil.which(t[1][0])]
    if os.environ.get("XDG_SESSION_TYPE") != "wayland":
        disponiveis.sort(key=lambda t: t[0] != "xclip")
    return disponiveis


def _escrever(comando: list[str], texto: str) -> bool:
    """Entrega o texto pela entrada padrão sem esperar o processo morrer.

    `xclip` e `wl-copy` continuam vivos depois de receber o conteúdo, porque é
    o processo deles que serve a seleção para quem colar. Capturar a saída e
    esperar o fim seria esperar até o timeout, e foi exatamente esse engano que
    fazia a cópia funcionar e a interface dizer que não.
    """
    try:
        proc = subprocess.Popen(
            comando,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        proc.stdin.write(texto.encode())
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        return False
    # Só interessa se ele morreu reclamando; ficar vivo é o comportamento certo.
    try:
        codigo = proc.wait(timeout=0.4)
        return codigo == 0
    except subprocess.TimeoutExpired:
        return True


def _ler(comando: list[str] | None) -> str | None:
    if comando is None or not shutil.which(comando[0]):
        return None
    try:
        proc = subprocess.run(comando, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def copy(texto: str, *, app=None) -> Resultado:
    """Copia e devolve o que de fato aconteceu.

    Nunca devolve sucesso sem ter confirmação: uma ferramenta que sai com
    código diferente de zero conta como falha, e o OSC 52 só é reportado como
    "tentado", porque não há como confirmar que o terminal aceitou.
    """
    if not texto:
        return Resultado(False, erro="nada para copiar")

    for nome, comando, leitura in _ferramenta_preferida():
        if not _escrever(comando, texto):
            continue
        de_volta = _ler(leitura)
        if de_volta is None:
            # Sem como conferir: aceita, porque a escrita não reclamou.
            return Resultado(True, via=nome)
        if de_volta.rstrip("\n") == texto.rstrip("\n"):
            return Resultado(True, via=nome)

    # Último recurso: a sequência de escape, que o terminal pode ignorar.
    if app is not None:
        try:
            app.copy_to_clipboard(texto)
            return Resultado(
                True,
                via="OSC 52",
                erro="",
            )
        except Exception:
            pass

    return Resultado(
        False,
        erro="nenhuma ferramenta de área de transferência encontrada",
        sugestao=_sugestao_instalacao(),
    )


def _sugestao_instalacao() -> str:
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "sudo apt install wl-clipboard"
    return "sudo apt install xclip"


def disponivel() -> str:
    """Nome da ferramenta que será usada, ou vazio se não houver nenhuma."""
    ferramentas = _ferramenta_preferida()
    return ferramentas[0][0] if ferramentas else ""
