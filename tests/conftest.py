"""Relógio do processo fixo para a suíte inteira.

O job nasce com `timezone="America/Sao_Paulo"` e o tick lê o cron nesse fuso.
Os testes escrevem as horas como hora do servidor, então o servidor dos testes
precisa estar no mesmo fuso do job, em qualquer máquina que rode a suíte. Quem
quer testar servidor em outro fuso usa `relogio_do_servidor`.
"""
from __future__ import annotations

import time

import pytest


def _fixa(monkeypatch, fuso: str) -> None:
    monkeypatch.setenv("TZ", fuso)
    time.tzset()


@pytest.fixture(autouse=True)
def _servidor_em_sao_paulo(monkeypatch):
    _fixa(monkeypatch, "America/Sao_Paulo")
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def relogio_do_servidor(monkeypatch):
    """Troca o fuso do servidor no meio do teste: `relogio_do_servidor("UTC")`."""
    return lambda fuso: _fixa(monkeypatch, fuso)
