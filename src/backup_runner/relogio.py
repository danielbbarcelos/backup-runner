"""O relógio do programa: um fuso só para histórico, fila, pastas e agendamento.

Servidor costuma rodar em UTC e quem lê o histórico pensa na hora de Brasília.
Em vez de cada parte do programa decidir sozinha, todas perguntam a hora aqui,
e a resposta vem no fuso de `settings.json` (`timezone`, padrão
`America/Sao_Paulo`). As horas continuam naive por dentro, como sempre foram;
o fuso só decide qual relógio de parede elas representam.

O nome da pasta de cada execução leva o deslocamento (`-0300`), para que a
pasta diga sozinha de que relógio veio, mesmo lida fora do servidor.
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FUSO_PADRAO = "America/Sao_Paulo"

# Fuso lido do settings.json, guardado junto com o mtime do arquivo. O tick e o
# worker pedem a hora dezenas de vezes por execução, e um stat é bem mais
# barato que reler o JSON a cada vez.
_cache: tuple[str, float, ZoneInfo | None] | None = None


def zona(nome: str | None) -> ZoneInfo | None:
    """O fuso pelo nome, ou None para usar o relógio do servidor.

    Nome vazio ou desconhecido cai no relógio do servidor em vez de derrubar o
    tick: um fuso digitado errado ainda deixa o backup rodar.
    """
    if not nome:
        return None
    try:
        return ZoneInfo(nome)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def fuso_do_programa() -> ZoneInfo | None:
    global _cache
    from .config import settings_file

    arquivo = settings_file()
    try:
        mtime = arquivo.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _cache is not None and _cache[0] == str(arquivo) and _cache[1] == mtime:
        return _cache[2]

    from .config import Settings

    fuso = zona(Settings.load().timezone)
    _cache = (str(arquivo), mtime, fuso)
    return fuso


def agora() -> dt.datetime:
    """A hora de agora no relógio do programa, naive."""
    fuso = fuso_do_programa()
    if fuso is None:
        return dt.datetime.now()
    return dt.datetime.now(fuso).replace(tzinfo=None)


def com_fuso(quando: dt.datetime, fuso: ZoneInfo | None) -> dt.datetime:
    """Prende uma hora naive ao fuso dado (None é o relógio do servidor)."""
    if fuso is None:
        return quando.astimezone()
    return quando.replace(tzinfo=fuso)


def para_fuso(quando: dt.datetime, nome: str | None) -> dt.datetime:
    """Hora do programa (naive) para a hora de parede de outro fuso (naive)."""
    destino = zona(nome)
    if destino is None:
        return quando
    return com_fuso(quando, fuso_do_programa()).astimezone(destino).replace(tzinfo=None)


def do_fuso(quando: dt.datetime, nome: str | None) -> dt.datetime:
    """Hora de parede de outro fuso (naive) de volta para a hora do programa (naive)."""
    origem = zona(nome)
    if origem is None:
        return quando
    programa = fuso_do_programa()
    convertido = quando.replace(tzinfo=origem)
    convertido = convertido.astimezone(programa) if programa else convertido.astimezone()
    return convertido.replace(tzinfo=None)


def deslocamento(quando: dt.datetime) -> str:
    """O deslocamento do relógio do programa naquela hora, como `-0300`."""
    offset = com_fuso(quando, fuso_do_programa()).utcoffset() or dt.timedelta(0)
    minutos = int(offset.total_seconds() // 60)
    sinal = "-" if minutos < 0 else "+"
    minutos = abs(minutos)
    return f"{sinal}{minutos // 60:02d}{minutos % 60:02d}"


def rotulo_fuso(quando: dt.datetime) -> str:
    """O deslocamento para gente ler: `-03:00`."""
    d = deslocamento(quando)
    return f"{d[:3]}:{d[3:]}"


# ----------------------------------------------------------------------------
# Pasta da execução
# ----------------------------------------------------------------------------

# `2026-10-20_03-00-05-0300`, e também o formato antigo sem deslocamento, que
# ainda está nos destinos de quem instalou antes.
PASTA = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})(?:([+-])(\d{2})(\d{2}))?$")


def nome_pasta(inicio: dt.datetime) -> str:
    """Timestamp sem espaço e sem dois-pontos, com o deslocamento no fim."""
    return inicio.strftime("%Y-%m-%d_%H-%M-%S") + deslocamento(inicio)


def parse_pasta(nome: str) -> dt.datetime | None:
    """A hora que está no nome da pasta, já no relógio do programa.

    Pasta com deslocamento é convertida, então a idade sai certa mesmo que o
    fuso do programa tenha mudado depois. Pasta antiga, sem deslocamento, é lida
    como se já estivesse no relógio do programa.
    """
    casou = PASTA.match(nome)
    if not casou:
        return None
    dia, hora, minuto, segundo, sinal, oh, om = casou.groups()
    try:
        quando = dt.datetime.strptime(f"{dia} {hora}:{minuto}:{segundo}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if not sinal:
        return quando
    offset = dt.timedelta(hours=int(oh), minutes=int(om))
    origem = dt.timezone(-offset if sinal == "-" else offset)
    convertido = quando.replace(tzinfo=origem)
    programa = fuso_do_programa()
    convertido = convertido.astimezone(programa) if programa else convertido.astimezone()
    return convertido.replace(tzinfo=None)
