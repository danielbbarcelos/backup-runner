"""Cron de cinco campos, sem dependência externa.

Suporta `*`, número, lista (`1,15`), faixa (`1-5`) e passo (`*/10`, `0-30/5`),
que é tudo que um agendamento de backup usa. Nomes de mês e de dia da semana
por extenso ficam de fora de propósito: a TUI escreve o cron, então a entrada é
controlada, e um parser menor é um parser que erra menos às três da manhã.

Dia-do-mês e dia-da-semana seguem a regra do cron de verdade: quando os dois
estão restritos, a data casa se qualquer um dos dois casar.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .i18n import t

CAMPOS = [
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),
]


class CronError(ValueError):
    pass


def _expandir(termo: str, minimo: int, maximo: int) -> set[int]:
    valores: set[int] = set()
    for parte in termo.split(","):
        parte = parte.strip()
        if not parte:
            raise CronError("campo vazio")
        passo = 1
        if "/" in parte:
            parte, texto_passo = parte.split("/", 1)
            try:
                passo = int(texto_passo)
            except ValueError as exc:
                raise CronError(f"passo inválido: {texto_passo}") from exc
            if passo < 1:
                raise CronError("passo precisa ser positivo")
        if parte == "*":
            inicio, fim = minimo, maximo
        elif "-" in parte:
            a, b = parte.split("-", 1)
            try:
                inicio, fim = int(a), int(b)
            except ValueError as exc:
                raise CronError(f"faixa inválida: {parte}") from exc
        else:
            try:
                inicio = fim = int(parte)
            except ValueError as exc:
                raise CronError(f"valor inválido: {parte}") from exc
        if inicio < minimo or fim > maximo or inicio > fim:
            raise CronError(f"fora da faixa {minimo}-{maximo}: {parte}")
        valores.update(range(inicio, fim + 1, passo))
    return valores


@dataclass
class Cron:
    expressao: str
    minute: set[int]
    hour: set[int]
    day: set[int]
    month: set[int]
    weekday: set[int]
    day_restrito: bool
    weekday_restrito: bool

    @classmethod
    def parse(cls, expressao: str) -> "Cron":
        termos = expressao.split()
        if len(termos) != 5:
            raise CronError("o cron tem cinco campos: minuto hora dia mês dia-da-semana")
        conjuntos = [
            _expandir(termo, minimo, maximo)
            for termo, (_, minimo, maximo) in zip(termos, CAMPOS)
        ]
        return cls(
            expressao=expressao,
            minute=conjuntos[0],
            hour=conjuntos[1],
            day=conjuntos[2],
            month=conjuntos[3],
            weekday=conjuntos[4],
            day_restrito=termos[2] != "*",
            weekday_restrito=termos[4] != "*",
        )

    def matches(self, quando: dt.datetime) -> bool:
        if quando.minute not in self.minute or quando.hour not in self.hour:
            return False
        if quando.month not in self.month:
            return False
        dia_ok = quando.day in self.day
        # Python: segunda=0. Cron: domingo=0.
        semana_ok = ((quando.weekday() + 1) % 7) in self.weekday
        if self.day_restrito and self.weekday_restrito:
            return dia_ok or semana_ok
        if self.day_restrito:
            return dia_ok
        if self.weekday_restrito:
            return semana_ok
        return True

    def next_after(self, depois: dt.datetime, *, limite_dias: int = 400) -> dt.datetime | None:
        atual = depois.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        fim = depois + dt.timedelta(days=limite_dias)
        while atual <= fim:
            if self.matches(atual):
                return atual
            # Salto barato: se a hora não serve, pula para a próxima hora cheia.
            if atual.hour not in self.hour:
                atual = (atual + dt.timedelta(hours=1)).replace(minute=0)
            elif atual.day not in self.day and self.day_restrito and not self.weekday_restrito:
                atual = (atual + dt.timedelta(days=1)).replace(hour=0, minute=0)
            else:
                atual += dt.timedelta(minutes=1)
        return None

    def prev_before(self, antes: dt.datetime, *, limite_dias: int = 400) -> dt.datetime | None:
        atual = antes.replace(second=0, microsecond=0)
        fim = antes - dt.timedelta(days=limite_dias)
        while atual >= fim:
            if self.matches(atual):
                return atual
            atual -= dt.timedelta(minutes=1)
        return None

    def humanize(self) -> str:
        """Frase curta para o painel de detalhe."""
        if (
            len(self.hour) == 1
            and len(self.minute) == 1
            and not self.day_restrito
            and not self.weekday_restrito
        ):
            hora = f"{next(iter(self.hour)):02d}:{next(iter(self.minute)):02d}"
            return t("dash.daily_at", hora=hora)
        if len(self.minute) == 1 and len(self.hour) > 1:
            horas = ", ".join(f"{h:02d}h" for h in sorted(self.hour))
            return f"todo dia às {horas}"
        return self.expressao


def next_run(expressao: str, depois: dt.datetime | None = None) -> dt.datetime | None:
    try:
        return Cron.parse(expressao).next_after(depois or dt.datetime.now())
    except CronError:
        return None


def previous_run(expressao: str, antes: dt.datetime | None = None) -> dt.datetime | None:
    try:
        return Cron.parse(expressao).prev_before(antes or dt.datetime.now())
    except CronError:
        return None


def humanize(expressao: str) -> str:
    try:
        return Cron.parse(expressao).humanize()
    except CronError:
        return expressao


def is_valid(expressao: str) -> bool:
    try:
        Cron.parse(expressao)
        return True
    except CronError:
        return False
