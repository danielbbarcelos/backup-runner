"""Perguntas no terminal.

Cada pergunta é uma linha, e a resposta é o que a pessoa digita. É o que
funciona por ssh ruim, dentro de `tmux`, e num terminal que não entende
sequência de escape.

O campo tem edição de linha completa (setas para os lados, Home, End, Ctrl-A,
Ctrl-E, Backspace, Delete), e o valor atual já vem preenchido e editável, em
vez de ficar escondido atrás de um `[padrão]` que só o Enter aceita. Quem quer
mudar uma porta de 3306 para 3307 muda um caractere.

Ctrl-C e Ctrl-D cancelam em qualquer ponto, sempre com o mesmo efeito: nada é
gravado, porque quem grava é o passo final de cada fluxo.
"""
from __future__ import annotations

import getpass
import sys
from typing import Callable, Sequence

from . import console as c
from . import keys

# Importar readline é o que dá edição de linha ao input(). Sem ele, a seta para
# a esquerda vira ^[[D literal dentro do texto digitado.
try:
    import readline
except ImportError:  # pragma: no cover - Windows sem pyreadline
    readline = None  # type: ignore[assignment]


class Cancelado(Exception):
    """A pessoa desistiu (Ctrl-C, Ctrl-D, ou 'cancelar')."""


def _ler(texto: str, *, preenchido: str = "") -> str:
    """Lê uma linha, opcionalmente já com um valor dentro, pronto para editar."""
    if readline is not None and preenchido:
        def _preenche() -> None:
            readline.insert_text(preenchido)
            readline.redisplay()

        readline.set_startup_hook(_preenche)
    try:
        return input(texto)
    except (KeyboardInterrupt, EOFError):
        print()
        raise Cancelado from None
    finally:
        if readline is not None:
            readline.set_startup_hook(None)
            # Cada campo começa limpo: subir a seta num campo de host não pode
            # trazer o nome do job digitado duas perguntas atrás.
            try:
                readline.clear_history()
            except AttributeError:
                pass


def texto(
    pergunta: str,
    *,
    padrao: str = "",
    obrigatorio: bool = False,
    valida: Callable[[str], str | None] | None = None,
) -> str:
    """Campo de texto. `valida` devolve a mensagem de erro, ou None se está bom.

    Com readline, o valor atual entra já digitado e editável. Sem ele, cai no
    velho `[padrão]` com Enter para aceitar.
    """
    edita = readline is not None and bool(padrao) and sys.stdin.isatty()
    sufixo = "" if edita else (f" [{c.muted(padrao)}]" if padrao else "")
    while True:
        resposta = _ler(
            f"  {c.secondary(pergunta)}{sufixo}: ",
            preenchido=padrao if edita else "",
        ).strip()
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
    rotulo_saida: str = "cancelar",
) -> str:
    """Escolha de uma opção.

    Com terminal de verdade, anda com as setas e a opção em foco ganha `→`.
    Digitar o número também funciona, e é o único caminho quando a entrada não
    é um terminal (num pipe, num teste, num script), porque aí não há tecla
    para ler.
    """
    if not opcoes:
        raise Cancelado
    if keys.disponivel():
        return _escolhe_setas(
            pergunta, opcoes, padrao=padrao,
            permitir_cancelar=permitir_cancelar, rotulo_saida=rotulo_saida,
        )
    return _escolhe_numero(
        pergunta, opcoes, padrao=padrao,
        permitir_cancelar=permitir_cancelar, rotulo_saida=rotulo_saida,
    )


def _linhas_opcoes(
    opcoes: Sequence[tuple[str, str]], foco: int, *, permitir_cancelar: bool,
    rotulo_saida: str = "cancelar",
) -> list[str]:
    linhas = []
    for i, (_, rotulo) in enumerate(opcoes):
        if i == foco:
            linhas.append(f"   {c.primary('→', bold=True)} {c.bold(rotulo)}")
        else:
            linhas.append(f"     {rotulo}")
    if permitir_cancelar:
        marca = c.primary("→", bold=True) if foco == len(opcoes) else " "
        texto_ = c.bold(rotulo_saida) if foco == len(opcoes) else c.muted(rotulo_saida)
        linhas.append(f"   {marca} {texto_}")
    return linhas


def _escolhe_setas(
    pergunta: str,
    opcoes: Sequence[tuple[str, str]],
    *,
    padrao: str | None,
    permitir_cancelar: bool,
    rotulo_saida: str = "cancelar",
) -> str:
    total = len(opcoes) + (1 if permitir_cancelar else 0)
    foco = 0
    if padrao is not None:
        for i, (chave, _) in enumerate(opcoes):
            if chave == padrao:
                foco = i
                break

    rodape = "\n" + c.dim(
        f"   ↑↓ navega   enter escolhe   esc {rotulo_saida}"
        if permitir_cancelar
        else "   ↑↓ navega   enter escolhe"
    )
    print()
    print(f"  {c.secondary(pergunta)}")
    linhas = _linhas_opcoes(
        opcoes, foco, permitir_cancelar=permitir_cancelar, rotulo_saida=rotulo_saida,
    )
    for linha in linhas:
        print(linha)
    print(rodape)

    keys.esconde_cursor()
    try:
        while True:
            try:
                tecla = keys.ler()
            except (KeyboardInterrupt, EOFError):
                print()
                raise Cancelado from None

            if tecla == keys.ENTER:
                if permitir_cancelar and foco == len(opcoes):
                    raise Cancelado
                return opcoes[foco][0]
            if tecla == keys.ESC or tecla == "q":
                if permitir_cancelar:
                    raise Cancelado
                continue
            if tecla == keys.CIMA:
                foco = (foco - 1) % total
            elif tecla == keys.BAIXO:
                foco = (foco + 1) % total
            elif tecla == keys.HOME:
                foco = 0
            elif tecla == keys.FIM:
                foco = total - 1
            elif tecla.isdigit():
                # Digitar o número continua funcionando como atalho.
                n = int(tecla)
                if n == 0 and permitir_cancelar:
                    raise Cancelado
                if 1 <= n <= len(opcoes):
                    return opcoes[n - 1][0]
                continue
            else:
                continue

            # Redesenha só o bloco das opções, para não piscar a tela inteira.
            keys.sobe(len(linhas) + 2)
            linhas = _linhas_opcoes(
                opcoes, foco, permitir_cancelar=permitir_cancelar, rotulo_saida=rotulo_saida,
            )
            for linha in linhas:
                print("\033[2K" + linha)
            print("\033[2K" + rodape)
    finally:
        keys.mostra_cursor()


def _escolhe_numero(
    pergunta: str,
    opcoes: Sequence[tuple[str, str]],
    *,
    padrao: str | None,
    permitir_cancelar: bool,
    rotulo_saida: str = "cancelar",
) -> str:
    print()
    indice_padrao = None
    for i, (chave, rotulo) in enumerate(opcoes, 1):
        marca = " "
        if padrao is not None and chave == padrao:
            indice_padrao = i
            marca = c.primary("•")
        print(f"   {marca} {c.primary(str(i).rjust(2))}  {rotulo}")
    if permitir_cancelar:
        print(f"     {c.muted(' 0')}  {c.muted(rotulo_saida)}")
    print()

    sufixo = f" [{indice_padrao}]" if indice_padrao else ""
    while True:
        resposta = _ler(f"  {c.secondary(pergunta)}{sufixo}: ").strip()
        if not resposta and indice_padrao:
            return opcoes[indice_padrao - 1][0]
        if resposta == "0" and permitir_cancelar:
            raise Cancelado
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
    """Marcação múltipla.

    Com terminal, as setas andam e o espaço marca. Sem terminal, números
    separados por espaço, com intervalos como `2-5`, porque marcar doze itens
    entre duzentos um a um é onde a paciência acaba.
    """
    if keys.disponivel():
        return _marca_setas(pergunta, opcoes, marcados=marcados)
    return _marca_numeros(pergunta, opcoes, marcados=marcados)


def _linhas_marcacao(
    opcoes: Sequence[tuple[str, str]], escolhidos: set[str], foco: int
) -> list[str]:
    linhas = []
    for i, (chave, rotulo) in enumerate(opcoes):
        marca = c.primary("[✓]") if chave in escolhidos else c.dim("[ ]")
        if i == foco:
            linhas.append(f"   {c.primary('→', bold=True)} {marca} {c.bold(rotulo)}")
        else:
            linhas.append(f"     {marca} {rotulo}")
    return linhas


def _marca_setas(
    pergunta: str,
    opcoes: Sequence[tuple[str, str]],
    *,
    marcados: Sequence[str],
) -> list[str]:
    escolhidos = set(marcados)
    foco = 0
    rodape = "\n" + c.dim("   ↑↓ navega   espaço marca   a todos   n nenhum   enter confirma")

    print()
    print(f"  {c.secondary(pergunta)}")
    linhas = _linhas_marcacao(opcoes, escolhidos, foco)
    for linha in linhas:
        print(linha)
    print(rodape)

    keys.esconde_cursor()
    try:
        while True:
            try:
                tecla = keys.ler()
            except (KeyboardInterrupt, EOFError):
                print()
                raise Cancelado from None

            if tecla == keys.ENTER:
                return [chave for chave, _ in opcoes if chave in escolhidos]
            if tecla == keys.ESC:
                raise Cancelado
            if tecla == keys.CIMA:
                foco = (foco - 1) % len(opcoes)
            elif tecla == keys.BAIXO:
                foco = (foco + 1) % len(opcoes)
            elif tecla == keys.ESPACO:
                chave = opcoes[foco][0]
                escolhidos.discard(chave) if chave in escolhidos else escolhidos.add(chave)
            elif tecla == "a":
                escolhidos = {chave for chave, _ in opcoes}
            elif tecla == "n":
                escolhidos.clear()
            else:
                continue

            keys.sobe(len(linhas) + 2)
            linhas = _linhas_marcacao(opcoes, escolhidos, foco)
            for linha in linhas:
                print("\033[2K" + linha)
            print("\033[2K" + rodape)
    finally:
        keys.mostra_cursor()


def _marca_numeros(
    pergunta: str,
    opcoes: Sequence[tuple[str, str]],
    *,
    marcados: Sequence[str],
) -> list[str]:
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
