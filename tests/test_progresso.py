"""Testes do acompanhamento ao vivo.

O que está sob teste é a legenda do backup: durante um job longo, alguém de
fora precisa conseguir responder "começou?", "anda?" e "ainda tem alguém do
outro lado?" sem depender de ver o processo.
"""
from __future__ import annotations

import datetime as dt
import os
import stat
from pathlib import Path

import pytest

from backup_runner.archive import ArchiveRequest, create
from backup_runner.config import DestinationStore, JobStore
from backup_runner.format import format_eta, progress_bar
from backup_runner.models import (
    ArchiveFormat,
    Destination,
    DestKind,
    FilesSource,
    Job,
    JobDestination,
    Run,
    RunResult,
)
from backup_runner.state import State, _fmt
from backup_runner.tick import run_tick
from backup_runner.worker import Progresso


def envelhece(estado: State, run_id: int, segundos: int) -> None:
    """Empurra o heartbeat para trás no tempo.

    Passa pelo `_fmt` do próprio módulo de propósito: escrever a data à mão
    aqui já fez um teste passar por acaso, comparando string com formato
    diferente do que o código grava.
    """
    estado.conn.execute(
        "UPDATE runs SET heartbeat=? WHERE id=?",
        (_fmt(dt.datetime.now() - dt.timedelta(seconds=segundos)), run_id),
    )
    assert estado.progresso_de(run_id)["heartbeat"] is not None


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


# ----------------------------------------------------------------------------
# A escrita do progresso
# ----------------------------------------------------------------------------

def test_etapa_sempre_escreve_e_avanco_e_represado():
    estado = State()
    run = Run(id=0, job="grande", started_at=dt.datetime.now(), result=RunResult.RUNNING)
    estado.insert_run(run)

    prog = Progresso(estado, run.id, intervalo=60.0)
    prog.etapa("dump", "83 tabelas", total=1000)
    assert estado.progresso_de(run.id)["prog_stage"] == "dump"

    # Com intervalo de um minuto, cem avanços seguidos não podem virar cem
    # escritas: é justamente isso que faria o SQLite atrapalhar o backup.
    for i in range(1, 101):
        prog.anda(i * 10)
    assert estado.progresso_de(run.id)["prog_done"] == 0

    # Mas a troca de etapa é notícia, e leva junto o número mais recente.
    prog.etapa("enviando", "spaces", total=2000)
    depois = estado.progresso_de(run.id)
    assert depois["prog_stage"] == "enviando"
    assert depois["prog_pid"] == os.getpid()
    estado.close()


def test_progresso_quebrado_nao_derruba_o_backup():
    """A legenda pode falhar; o backup, não."""

    class BancoMorto:
        def progresso(self, *a, **k):
            raise RuntimeError("banco sumiu")

    prog = Progresso(BancoMorto(), 1)
    prog.etapa("dump")  # não levanta
    prog.anda(10)


def test_heartbeat_marca_a_hora_da_ultima_noticia():
    estado = State()
    run = Run(id=0, job="x", started_at=dt.datetime.now(), result=RunResult.RUNNING)
    estado.insert_run(run)
    antes = dt.datetime.now() - dt.timedelta(seconds=1)
    Progresso(estado, run.id).etapa("dump")
    assert estado.progresso_de(run.id)["heartbeat"] >= antes.replace(microsecond=0)
    estado.close()


# ----------------------------------------------------------------------------
# Execuções órfãs
# ----------------------------------------------------------------------------

def test_tick_fecha_execucao_cujo_worker_morreu():
    estado = State()
    run = Run(id=0, job="orfa", started_at=dt.datetime.now(), result=RunResult.RUNNING)
    estado.insert_run(run)
    # Silêncio longo e um pid que não existe: o worker morreu.
    estado.progresso(run.id, stage="dump", pid=999_999)
    envelhece(estado, run.id, 7200)

    resultado = run_tick(state=estado)

    assert (run.id, "orfa") in resultado.abandonadas
    fechada = estado.get_run(run.id)
    assert fechada.result is RunResult.FAILED
    assert "morreu" in fechada.error_cause
    estado.close()


def test_execucao_viva_e_silenciosa_nao_e_abandonada():
    """Máquina suspensa cala o worker sem matá-lo. O pid desempata."""
    estado = State()
    run = Run(id=0, job="lento", started_at=dt.datetime.now(), result=RunResult.RUNNING)
    estado.insert_run(run)
    estado.progresso(run.id, stage="enviando", pid=os.getpid())
    envelhece(estado, run.id, 7200)

    resultado = run_tick(state=estado)

    assert resultado.abandonadas == []
    assert estado.get_run(run.id).result is RunResult.RUNNING
    estado.close()


def test_execucao_antiga_sem_heartbeat_fica_em_paz():
    """Banco de versão anterior não tem batida, e não dá para julgar."""
    estado = State()
    run = Run(id=0, job="antiga", started_at=dt.datetime.now(), result=RunResult.RUNNING)
    estado.insert_run(run)
    assert estado.orfas() == []
    estado.close()


# ----------------------------------------------------------------------------
# Os totais, que são o denominador
# ----------------------------------------------------------------------------

def test_arquivamento_conhece_o_total_antes_de_comecar(tmp_path):
    base = tmp_path / "dados"
    base.mkdir()
    for i in range(150):
        (base / f"{i}.txt").write_text("x" * 1000)

    chamadas = []
    create(
        ArchiveRequest(source=base, output_file=tmp_path / "saida"),
        on_progress=lambda a, b, ta, tb: chamadas.append((a, b, ta, tb)),
    )

    # A primeira chamada é a medição: nada lido ainda, total já sabido.
    assert chamadas[0] == (0, 0, 150, 150_000)
    assert all(c[2] == 150 for c in chamadas)


def test_medir_antes_pode_ser_desligado(tmp_path):
    base = tmp_path / "dados"
    base.mkdir()
    for i in range(150):
        (base / f"{i}.txt").write_text("x" * 1000)

    chamadas = []
    create(
        ArchiveRequest(source=base, output_file=tmp_path / "saida", medir_antes=False),
        on_progress=lambda a, b, ta, tb: chamadas.append((a, b, ta, tb)),
    )
    assert all(c[2] == 0 for c in chamadas)


# ----------------------------------------------------------------------------
# O desenho
# ----------------------------------------------------------------------------

def test_barra_sem_total_nao_finge_zero_por_cento():
    """Total desconhecido não é zero por cento: é ausência de barra."""
    assert progress_bar(0, 0, 10).strip() == ""
    assert progress_bar(5, 10, 10) == "█████░░░░░"
    assert progress_bar(999, 10, 10) == "█" * 10


def test_eta_cala_quando_nao_tem_o_que_dizer():
    assert format_eta(0, 100, 10) == ""
    assert format_eta(50, 0, 10) == ""
    assert format_eta(100, 100, 10) == ""
    assert format_eta(50, 100, 10) != ""


def test_job_de_arquivos_registra_progresso_durante_a_execucao(tmp_path, monkeypatch):
    """O teste que representa o caso real: um job grande deixando rastro."""
    from backup_runner import worker

    base = tmp_path / "grande"
    base.mkdir()
    for i in range(300):
        (base / f"{i}.bin").write_bytes(b"z" * 4000)

    DestinationStore.load().put(Destination(
        name="disco", kind=DestKind.LOCAL, path=str(tmp_path / "dest"), retention_days=7,
    ))
    job = Job(
        name="grande", source=FilesSource(path=str(base), archive_format=ArchiveFormat.TARGZ),
        destinations=[JobDestination(name="disco")], schedule="0 3 * * *",
    )
    JobStore.load().put(job)

    vistos = []
    original = State.progresso

    def espiao(self, run_id, **kw):
        vistos.append(kw["stage"])
        return original(self, run_id, **kw)

    monkeypatch.setattr(State, "progresso", espiao)

    estado = State()
    estado.enqueue(job.name, dt.datetime.now())
    item = estado.claim_next()
    resultado = worker.executa_item(item, estado)
    estado.close()

    assert resultado.ok
    # As etapas contadas na ordem em que acontecem, sem repetir vizinhas.
    etapas = [e for i, e in enumerate(vistos) if i == 0 or vistos[i - 1] != e]
    assert etapas[0] == "preparando"
    assert "medindo" in etapas and "lendo" in etapas
    assert "enviando" in etapas
    assert etapas[-1] == "concluído"


def test_heartbeat_no_futuro_nao_conta_como_silencio():
    """Relógio corrigido para trás põe batidas no futuro. Nada a abandonar."""
    estado = State()
    run = Run(id=0, job="relogio", started_at=dt.datetime.now(), result=RunResult.RUNNING)
    estado.insert_run(run)
    estado.progresso(run.id, stage="dump", pid=999_999)
    envelhece(estado, run.id, -3600)
    assert estado.orfas() == []
    estado.close()


def test_prazo_avisa_quando_o_ritmo_nao_cabe_no_limite(capsys):
    """O caso que motivou tudo: envio lento contra timeout curto."""
    from backup_runner.config import JobStore
    from backup_runner.context import Context
    from backup_runner import views

    JobStore.load().put(Job(
        name="grande", source=FilesSource(path="/tmp"),
        destinations=[], schedule="0 3 * * *", timeout_minutes=120,
    ))
    estado = State()
    run = Run(id=0, job="grande",
              started_at=dt.datetime.now() - dt.timedelta(minutes=110),
              result=RunResult.RUNNING)
    estado.insert_run(run)
    # Dez por cento em 110 min: o resto não cabe nos 10 min que sobram.
    estado.progresso(run.id, stage="enviando", done=1_000, total=10_000, pid=os.getpid())

    views.andamento(Context())
    saida = capsys.readouterr().out
    assert "neste ritmo" in saida
    estado.close()


def test_prazo_estourado_e_dito_sem_rodeio(capsys):
    from backup_runner.config import JobStore
    from backup_runner.context import Context
    from backup_runner import views

    JobStore.load().put(Job(
        name="atrasado", source=FilesSource(path="/tmp"),
        destinations=[], schedule="0 3 * * *", timeout_minutes=30,
    ))
    estado = State()
    run = Run(id=0, job="atrasado",
              started_at=dt.datetime.now() - dt.timedelta(minutes=90),
              result=RunResult.RUNNING)
    estado.insert_run(run)
    estado.progresso(run.id, stage="enviando", done=1, total=100, pid=os.getpid())

    views.andamento(Context())
    assert "passou do limite de 30 min" in capsys.readouterr().out
    estado.close()
