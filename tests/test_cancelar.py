"""Cancelar uma execução, com worker vivo ou sem ele.

São dois caminhos e os dois precisam existir. Com worker vivo, a marca no banco
basta: o vigia a lê em segundos e a execução sai pelo caminho limpo. Sem worker
vivo não há quem obedeça, e a limpeza é feita por quem deu o comando.

Matar o processo de fora resolveria rápido e deixaria exatamente a sujeira que
o estudo mandou parar de produzir: artefato órfão no disco e linha de fila
presa bloqueando toda janela futura do job.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
from pathlib import Path

import pytest

from backup_runner.config import DestinationStore, JobStore, staging_dir
from backup_runner.context import Context, cancela_execucao
from backup_runner.models import (
    ArchiveFormat,
    Destination,
    DestKind,
    FilesSource,
    Job,
    JobDestination,
    Run,
    RunResult,
    Stage,
)
from backup_runner.state import State


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


def cria_job(tmp_path, nome="demorado", arquivos=4000) -> Job:
    origem = tmp_path / nome
    origem.mkdir(exist_ok=True)
    for i in range(arquivos):
        (origem / f"{i}.txt").write_text("x" * 500)
    DestinationStore.load().put(Destination(
        name="disco", kind=DestKind.LOCAL,
        path=str(tmp_path / "destino"), retention_days=7,
    ))
    job = Job(name=nome, source=FilesSource(path=str(origem),
                                            archive_format=ArchiveFormat.TARGZ),
              destinations=[JobDestination(name="disco")], schedule="0 3 * * *")
    JobStore.load().put(job)
    return job


# ----------------------------------------------------------------------------
# Worker vivo: o caminho cooperativo
# ----------------------------------------------------------------------------

def test_cancelamento_interrompe_backup_em_curso_sem_deixar_lixo(tmp_path, monkeypatch):
    """O caso que motivou o comando: parar um backup grande no meio."""
    from backup_runner import worker

    cria_job(tmp_path)
    monkeypatch.setattr(worker, "INTERVALO_VIGILANCIA", 0.02)
    avisos = []
    monkeypatch.setattr(worker, "_avisa", lambda *a, **k: avisos.append(a))

    estado = State()
    estado.enqueue("demorado", dt.datetime.now())

    # Pede o cancelamento de fora, como o comando faria, enquanto roda.
    def pede():
        alvo = State()
        for _ in range(200):
            atual = alvo.runs(limit=1)
            if atual and atual[0].result is RunResult.RUNNING:
                alvo.pede_cancelamento(atual[0].id)
                break
            threading.Event().wait(0.02)
        alvo.close()

    t = threading.Thread(target=pede)
    t.start()
    resultado = worker.drena(estado)[-1]
    t.join()

    final = estado.get_run(resultado.run.id)
    estado.close()

    assert final.result is RunResult.FAILED
    assert final.error_cause == "cancelada por você"
    # A etapa é a de verdade, não um chute. Ele estava lendo arquivos.
    assert final.error_stage is Stage.ARCHIVE
    assert not (staging_dir() / "demorado").exists(), "artefato pela metade ficou no disco"
    assert not list((tmp_path / "destino").rglob("*.tar.gz")), "subiu artefato incompleto"
    assert avisos == [], "quem cancelou está olhando, não precisa de email"


# ----------------------------------------------------------------------------
# Worker ausente: a limpeza na hora
# ----------------------------------------------------------------------------

def test_cancelar_execucao_sem_worker_limpa_na_hora(tmp_path):
    """Execução que já estava morta sem ninguém ter notado."""
    cria_job(tmp_path, arquivos=5)
    estado = State()
    run = Run(id=0, job="demorado", started_at=dt.datetime.now(),
              result=RunResult.RUNNING)
    estado.insert_run(run)
    estado.progresso(run.id, stage="lendo", pid=999_999)   # pid que não existe

    estado.enqueue("demorado", dt.datetime.now())
    item = estado.claim_next()
    estado.conn.execute("UPDATE queue SET claimed_pid=999999 WHERE id=?", (item["id"],))

    pasta = staging_dir() / "demorado" / run.folder
    pasta.mkdir(parents=True)
    (pasta / "meio.tar.gz").write_bytes(b"z" * 8192)

    feito = cancela_execucao(Context(), run)

    final = estado.get_run(run.id)
    fila = {x["id"]: x["status"] for x in estado.conn.execute("SELECT id, status FROM queue")}
    estado.close()

    assert feito["vivo"] is False
    assert feito["staging"] == 8192
    assert final.result is RunResult.FAILED
    assert not pasta.exists()
    # A linha de fila sai junto, senão o job ficaria travado para sempre.
    assert fila[item["id"]] == "abandoned"


def test_cancelar_com_worker_vivo_nao_mexe_no_staging(tmp_path):
    """Tirar o chão de quem ainda escreve criaria o problema que queremos evitar."""
    cria_job(tmp_path, arquivos=5)
    estado = State()
    run = Run(id=0, job="demorado", started_at=dt.datetime.now(),
              result=RunResult.RUNNING)
    estado.insert_run(run)
    estado.progresso(run.id, stage="lendo", pid=os.getpid())   # vivo

    pasta = staging_dir() / "demorado" / run.folder
    pasta.mkdir(parents=True)
    (pasta / "em-uso.tar.gz").write_bytes(b"z" * 4096)

    feito = cancela_execucao(Context(), run)
    estado.close()

    assert feito["vivo"] is True
    assert feito["staging"] == 0
    assert pasta.exists(), "o staging de um worker vivo não pode ser removido daqui"
    assert (pasta / "em-uso.tar.gz").exists()


def test_envio_pendente_tambem_e_cancelavel(tmp_path):
    """Cancelar um pendente quer dizer: pare de tentar reenviar."""
    cria_job(tmp_path, arquivos=5)
    estado = State()
    run = Run(id=0, job="demorado", started_at=dt.datetime.now(),
              finished_at=dt.datetime.now(), result=RunResult.PENDING_UPLOAD)
    run.destinations_pending = ["disco"]
    estado.insert_run(run)
    pasta = staging_dir() / "demorado" / run.folder
    pasta.mkdir(parents=True)
    (pasta / "pronto.tar.gz").write_bytes(b"z" * 2048)

    feito = cancela_execucao(Context(), run)
    final = estado.get_run(run.id)
    sobraram = estado.pending_uploads()
    estado.close()

    assert feito["vivo"] is False
    assert final.result is RunResult.FAILED
    assert not pasta.exists(), "o artefato que não vai mais subir tem que sair"
    assert sobraram == [], "o tick continuaria tentando reenviar"
