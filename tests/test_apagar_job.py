"""Apagar um job encerra tudo que era dele.

Antes disto, apagar removia só o cadastro e os registros. O que estava na
fila continuava lá, o artefato meio pronto ficava ocupando disco, e a execução
em curso seguia até o fim para então mandar email e Slack sobre um job que já
não existia. Era esse último o sintoma que aparecia para quem usava: "apaguei
o job e continuo recebendo aviso de erro".
"""
from __future__ import annotations

import datetime as dt
import os
import stat
from pathlib import Path

import pytest

from backup_runner.config import DestinationStore, JobStore, staging_dir
from backup_runner.context import Context, encerra_job
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
from backup_runner.state import State


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


def cria_job(tmp_path, nome="alvo") -> Job:
    origem = tmp_path / nome
    origem.mkdir(exist_ok=True)
    for i in range(20):
        (origem / f"{i}.txt").write_text("x" * 500)
    DestinationStore.load().put(Destination(
        name="disco", kind=DestKind.LOCAL,
        path=str(tmp_path / "destino"), retention_days=7,
    ))
    job = Job(
        name=nome,
        source=FilesSource(path=str(origem), archive_format=ArchiveFormat.TARGZ),
        destinations=[JobDestination(name="disco")], schedule="0 3 * * *",
    )
    JobStore.load().put(job)
    return job


# ----------------------------------------------------------------------------
# A limpeza
# ----------------------------------------------------------------------------

def test_apagar_tira_da_fila_o_que_ainda_nao_rodou(tmp_path):
    cria_job(tmp_path)
    estado = State()
    estado.enqueue("alvo", dt.datetime.now())
    assert estado.queue_size() == 1

    parado = encerra_job(Context(), "alvo")

    assert parado["fila"] == 1
    assert estado.queue_size() == 0
    estado.close()


def test_apagar_leva_junto_o_staging(tmp_path):
    cria_job(tmp_path)
    pasta = staging_dir() / "alvo" / "2026-09-17_03-00-00"
    pasta.mkdir(parents=True)
    (pasta / "meio-pronto.tar.gz").write_bytes(b"z" * 4096)

    parado = encerra_job(Context(), "alvo")

    assert parado["staging"] == 4096
    assert not (staging_dir() / "alvo").exists()


def test_apagar_diz_que_havia_execucao_em_curso(tmp_path):
    cria_job(tmp_path)
    estado = State()
    estado.insert_run(Run(id=0, job="alvo", started_at=dt.datetime.now(),
                          result=RunResult.RUNNING))

    parado = encerra_job(Context(), "alvo")

    assert parado["rodando"] is True
    estado.close()


def test_apagar_nao_mexe_na_fila_dos_outros(tmp_path):
    cria_job(tmp_path, "alvo")
    cria_job(tmp_path, "vizinho")
    estado = State()
    estado.enqueue("alvo", dt.datetime.now())
    estado.enqueue("vizinho", dt.datetime.now())

    encerra_job(Context(), "alvo")

    assert estado.queue_size() == 1
    assert JobStore.load().get("vizinho") is not None
    estado.close()


# ----------------------------------------------------------------------------
# O worker desistindo no meio
# ----------------------------------------------------------------------------

def test_worker_para_e_cala_quando_o_job_some_no_meio(tmp_path, monkeypatch):
    """O sintoma relatado: backup seguia até o fim e avisava assim mesmo."""
    from backup_runner import worker

    job = cria_job(tmp_path)
    # Muitos arquivos para o arquivamento durar o suficiente para a sentinela
    # rodar pelo menos uma vez.
    origem = Path(job.source.path)
    for i in range(20, 3000):
        (origem / f"{i}.txt").write_text("x" * 500)

    avisos = []
    monkeypatch.setattr(worker, "_avisa", lambda *a, **k: avisos.append(a))

    # Sem espera nenhuma, a sentinela consulta o disco em toda chamada.
    monkeypatch.setattr(worker, "INTERVALO_SENTINELA", 0.0)

    # Apaga o job assim que o arquivamento começar de verdade.
    original = worker.Sentinela.__call__
    estado_chamadas = {"n": 0}

    def espiao(self):
        estado_chamadas["n"] += 1
        if estado_chamadas["n"] == 2:
            JobStore.load().delete("alvo")
        return original(self)

    monkeypatch.setattr(worker.Sentinela, "__call__", espiao)

    estado = State()
    estado.enqueue("alvo", dt.datetime.now())
    item = estado.claim_next()
    resultado = worker.executa_item(item, estado)

    assert resultado.ok
    assert "apagado" in resultado.mensagem
    assert avisos == [], "não pode avisar sobre um job que foi apagado"
    assert not (staging_dir() / "alvo").exists(), "o artefato pela metade tem que sair"
    assert estado.count_runs(job="alvo") == 0, "o registro da execução abortada some junto"
    estado.close()


def test_reenvio_de_job_apagado_e_descartado(tmp_path):
    """O tick pode enfileirar o reenvio e o job sumir antes de ser atendido."""
    from backup_runner import worker

    job = cria_job(tmp_path)
    estado = State()
    run = Run(id=0, job="alvo", started_at=dt.datetime.now(),
              result=RunResult.PENDING_UPLOAD)
    run.destinations_pending = ["disco"]
    estado.insert_run(run)
    JobStore.load().delete("alvo")

    resultado = worker.reenvia_pendentes(job, estado)

    assert resultado.ok
    assert "apagado" in resultado.mensagem
    estado.close()


def test_job_que_existe_continua_avisando(tmp_path, monkeypatch):
    """A sentinela não pode calar o aviso de um backup legítimo."""
    from backup_runner import worker

    cria_job(tmp_path)
    avisos = []
    monkeypatch.setattr(worker, "_avisa", lambda *a, **k: avisos.append(a))

    estado = State()
    estado.enqueue("alvo", dt.datetime.now())
    item = estado.claim_next()
    resultado = worker.executa_item(item, estado)

    assert resultado.ok
    assert len(avisos) == 1
    estado.close()
