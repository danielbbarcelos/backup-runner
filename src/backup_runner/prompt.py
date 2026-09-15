"""Perguntas no terminal, com `input()`.

Sem captura de tela e sem tecla de atalho: cada pergunta é uma linha, a
resposta é o que a pessoa digita, e Enter aceita o padrão que está entre
colchetes. É o que funciona por ssh ruim, dentro de `tmux`, num terminal que
não entende sequência de escape, e é o que dá para ler depois no scrollback.

Ctrl-C e Ctrl-D cancelam em qualquer ponto, sempre com o mesmo efeito: nada é
gravado, porque quem grava é o passo final de cada fluxo.
"""
from __future__ import annotations

import getpass
import sys
from typing import Callable, Sequence

from . import console as c


class Cancelado(Exception):
    """A pessoa desistiu (Ctrl-C, Ctrl-D, ou 'cancelar')."""


def _ler(texto: str) -> str:
    try:
        return input(texto)
    except (KeyboardInterrupt, EOFError):
        print()
        raise Cancelado from None


def texto(
    pergunta: str,
    *,
    padrao: str = "",
    obrigatorio: bool = False,
    valida: Callable[[str], str | None] | None = None,
) -> str:
    """Campo de texto. `valida` devolve a mensagem de erro, ou None se está bom."""
    sufixo = f" [{c.muted(padrao)}]" if padrao else ""
    while True:
        resposta = _ler(f"  {c.secondary(pergunta)}{sufixo}: ").strip()
        if not resposta:
            resposta = padrao
        if obrigatorio and not resposta:
            c.erro("precisa de um valor")
            continue
        if valida is not None:
            problema = valida(resposta)
            if problema:
                c.erro(problema)
                continue
        return resposta


def senha(pergunta: str, *, manter: bool = False) -> str:
    """Senha sem eco. Enter em branco mantém a atual, quando há uma."""
    sufixo = c.muted(" [Enter mantém a atual]") if manter else ""
    try:
        return getpass.getpass(f"  {c.secondary(pergunta)}{sufixo}: ")
    except (KeyboardInterrupt, EOFError):
        print()
        raise Cancelado from None


def inteiro(pergunta: str, *, padrao: int, minimo: int | None = None, maximo: int | None = None) -> int:
    def valida(valor: str) -> str | None:
        try:
            n = int(valor)
        except ValueError:
            return "precisa ser um número"
        if minimo is not None and n < minimo:
            return f"mínimo {minimo}"
        if maximo is not None and n > maximo:
            return f"máximo {maximo}"
        return None

    return int(texto(pergunta, padrao=str(padrao), valida=valida))


def confirma(pergunta: str, *, padrao: bool = False) -> bool:
    marca = "S/n" if padrao else "s/N"
    while True:
        resposta = _ler(f"  {c.secondary(pergunta)} [{marca}]: ").strip().lower()
        if not resposta:
            return padrao
        if resposta in ("s", "sim", "y", "yes"):
            return True
        if resposta in ("n", "nao", "não", "no"):
            return False
        c.erro("responda s ou n")


def escolhe(
    pergunta: str,
    opcoes: Sequence[tuple[str, str]],
    *,
    padrao: str | None = None,
    permitir_cancelar: bool = True,
) -> str:
    """Escolha por número.

    Número em vez de setas de propósito: a pessoa vê a lista inteira de uma vez,
    pode conferir antes de digitar, e o que ela escolheu fica no scrollback.
    """
    if not opcoes:
        raise Cancelado
    print()
    indice_padrao = None
    for i, (chave, rotulo) in enumerate(opcoes, 1):
        marca = " "
        if padrao is not None and chave == padrao:
            indice_padrao = i
            marca = c.primary("•")
        print(f"   {marca} {c.primary(str(i).rjust(2))}  {rotulo}")
    if permitir_cancelar:
        print(f"     {c.muted(' 0')}  {c.muted('cancelar')}")
    print()

    sufixo = f" [{indice_padrao}]" if indice_padrao else ""
    while True:
        resposta = _ler(f"  {c.secondary(pergunta)}{sufixo}: ").strip()
        if not resposta and indice_padrao:
            return opcoes[indice_padrao - 1][0]
        if resposta == "0" and permitir_cancelar:
            raise Cancelado
        # Aceita o número ou a própria chave digitada.
        for chave, _ in opcoes:
            if resposta == chave:
                return chave
        try:
            n = int(resposta)
        except ValueError:
            c.erro("digite o número da opção")
            continue
        if 1 <= n <= len(opcoes):
            return opcoes[n - 1][0]
        c.erro(f"escolha entre 1 e {len(opcoes)}")


def marca_varios(
    pergunta: str,
    opcoes: Sequence[tuple[str, str]],
    *,
    marcados: Sequence[str] = (),
) -> list[str]:
    """Marcação múltipla por números separados por espaço ou vírgula.

    Aceita também `todos`, `nenhum`, e intervalos como `2-5`, porque escolher
    doze itens entre duzentos um a um é onde a paciência acaba.
    """
    escolhidos = set(marcados)
    while True:
        print()
        for i, (chave, rotulo) in enumerate(opcoes, 1):
            marca = c.primary("[✓]") if chave in escolhidos else c.dim("[ ]")
            print(f"   {c.primary(str(i).rjust(3))} {marca} {rotulo}")
        print()
        c.nota("números separados por espaço alternam; 2-5 marca o intervalo")
        c.nota("'todos', 'nenhum', ou Enter para confirmar")
        resposta = _ler(f"  {c.secondary(pergunta)}: ").strip().lower()

        if not resposta:
            return [chave for chave, _ in opcoes if chave in escolhidos]
        if resposta in ("todos", "todas", "all"):
            escolhidos = {chave for chave, _ in opcoes}
            continue
        if resposta in ("nenhum", "nenhuma", "none"):
            escolhidos.clear()
            continue

        for pedaco in resposta.replace(",", " ").split():
            if "-" in pedaco:
                try:
                    inicio, fim = (int(x) for x in pedaco.split("-", 1))
                except ValueError:
                    c.erro(f"não entendi {pedaco!r}")
                    continue
                for n in range(inicio, fim + 1):
                    if 1 <= n <= len(opcoes):
                        escolhidos.add(opcoes[n - 1][0])
                continue
            try:
                n = int(pedaco)
            except ValueError:
                c.erro(f"não entendi {pedaco!r}")
                continue
            if 1 <= n <= len(opcoes):
                chave = opcoes[n - 1][0]
                escolhidos.discard(chave) if chave in escolhidos else escolhidos.add(chave)
            else:
                c.erro(f"{n} está fora da lista")


def pausa(texto_: str = "Enter para continuar") -> None:
    try:
        input(f"\n  {c.muted(texto_)}")
    except (KeyboardInterrupt, EOFError):
        print()
        raise Cancelado from None


def confirma_digitando(pergunta: str, esperado: str) -> bool:
    """Confirmação destrutiva: exige o nome digitado por inteiro.

    Um Enter distraído não pode apagar 51 artefatos em três destinos.
    """
    print()
    c.aviso(pergunta)
    resposta = _ler(f"  digite {c.bold(esperado)} para confirmar: ").strip()
    return resposta == esperado
