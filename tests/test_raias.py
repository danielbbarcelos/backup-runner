"""As duas raias: um job não prende o outro.

A fila era estritamente serial, então um envio de quatro horas atrasava por
quatro horas o dump de trinta segundos de qualquer outro job, e as janelas
deles chegavam a virar janela perdida. Nenhuma das outras correções resolvia
isso, porque o problema não era travamento: era desenho.

Produzir e enviar passaram a ser itens de fila separados, consumidos por laços
independentes. Dentro de cada raia o trabalho continua serial, de propósito:
dois dumps pesados disputando disco demoram mais que os dois em sequência, e
dois envios disputando a mesma rede não sobem mais rápido.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path

import pytest

from backup_runner.config import DestinationStore, JobStore, staging_dir
from backup_runner.models import (
    ArchiveFormat,
    Destination,
    DestKind,
    FilesSource,
    Job,
    JobDestination,
    RunResult,
)
from backup_runner.state import RAIA_ENVIO, RAIA_PRODUCAO, State


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


def cria_job(tmp_path, nome, *, arquivos=10) -> Job:
    origem = tmp_path / nome
    origem.mkdir(exist_ok=True)
    for i in range(arquivos):
        (origem / f"{i}.txt").write_text("x" * 200)
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
# A separação em si
# ----------------------------------------------------------------------------

def test_um_backup_sao_dois_itens_em_raias_diferentes(tmp_path):
    from backup_runner import worker

    cria_job(tmp_path, "diario")
    estado = State()
    estado.enqueue("diario", dt.datetime.now())

    # A raia de envio não tem o que fazer antes de a produção terminar.
    assert estado.claim_next(RAIA_ENVIO) is None

    producao = worker.processa_um(estado, RAIA_PRODUCAO)
    assert producao is not None and producao.ok
    # O artefato está pronto e esperando, não concluído.
    meio = estado.get_run(producao.run.id)
    assert meio.result is RunResult.QUEUED
    assert (staging_dir() / "diario" / meio.folder).is_dir()
    assert not list((tmp_path / "destino").rglob("*.tar.gz")), "enviou na raia errada"

    envio = worker.processa_um(estado, RAIA_ENVIO)
    assert envio is not None and envio.ok
    fim = estado.get_run(producao.run.id)
    assert fim.result is RunResult.OK
    assert list((tmp_path / "destino").rglob("*.tar.gz"))
    estado.close()


def test_envio_longo_nao_atrasa_o_dump_de_outro_job(tmp_path, monkeypatch):
    """O caso que motivou a separação, medido.

    O envio do job A é travado de propósito. Antes, isso deixaria o job B
    esperando o fim de A. Agora B produz enquanto A ainda está enviando.
    """
    from backup_runner import destinations, worker

    cria_job(tmp_path, "lento")
    cria_job(tmp_path, "rapido")

    envio_comecou = threading.Event()
    pode_terminar = threading.Event()
    original = destinations.LocalBackend.upload

    def upload_que_demora(self, pasta, prefixo, on_progress=None, **kw):
        if prefixo.startswith("lento/"):
            envio_comecou.set()
            assert pode_terminar.wait(10), "o teste travou"
        return original(self, pasta, prefixo, on_progress, **kw)

    monkeypatch.setattr(destinations.LocalBackend, "upload", upload_que_demora)

    estado = State()
    estado.enqueue("lento", dt.datetime.now())
    worker.processa_um(estado, RAIA_PRODUCAO)          # produz o lento

    # Raia de envio ocupada com o job lento, numa thread.
    parar = {"agora": False}
    fio = threading.Thread(target=worker._laco, args=("envio", 0.02, parar), daemon=True)
    fio.start()
    assert envio_comecou.wait(10), "o envio do job lento não começou"

    # Com o envio preso, a produção do outro job tem que andar.
    inicio = time.monotonic()
    estado.enqueue("rapido", dt.datetime.now())
    producao = worker.processa_um(estado, RAIA_PRODUCAO)
    levou = time.monotonic() - inicio

    assert producao is not None and producao.ok, "a produção ficou presa atrás do envio"
    assert estado.get_run(producao.run.id).job == "rapido"
    assert levou < 5, f"a produção esperou o envio: {levou:.1f}s"

    pode_terminar.set()
    parar["agora"] = True
    fio.join(timeout=5)
    estado.close()


def test_dentro_da_raia_o_trabalho_continua_serial(tmp_path):
    """Dois dumps disputando disco demoram mais que os dois em sequência."""
    from backup_runner import worker

    cria_job(tmp_path, "a")
    cria_job(tmp_path, "b")
    estado = State()
    estado.enqueue("a", dt.datetime.now())
    estado.enqueue("b", dt.datetime.now())

    primeiro = estado.claim_next(RAIA_PRODUCAO)
    # Com um item já reivindicado, o próximo claim na mesma raia pega o outro
    # job, mas quem consome é um laço só, então eles saem em sequência.
    segundo = estado.claim_next(RAIA_PRODUCAO)
    assert {primeiro["job"], segundo["job"]} == {"a", "b"}
    assert estado.claim_next(RAIA_PRODUCAO) is None
    estado.close()


# ----------------------------------------------------------------------------
# Não criar travamento novo
# ----------------------------------------------------------------------------

def test_job_nao_concorre_consigo_mesmo(tmp_path):
    """Raia separada não pode virar dois backups do mesmo job ao mesmo tempo."""
    from backup_runner import worker
    from backup_runner.tick import run_tick

    cria_job(tmp_path, "diario")
    estado = State()
    estado.enqueue("diario", dt.datetime(2026, 10, 6, 3, 0))
    worker.processa_um(estado, RAIA_PRODUCAO)        # fica QUEUED, esperando envio

    # Janela nova chegando enquanto o envio do anterior não aconteceu.
    r = run_tick(dt.datetime(2026, 10, 7, 3, 5), state=estado)
    estado.close()

    assert r.enfileirados == [], "enfileirou um segundo backup do mesmo job"


def test_execucao_esperando_envio_nao_e_morta_pelo_detector_de_orfa(tmp_path):
    """No intervalo entre as fases não existe worker batendo o coração.

    Deixar o estado como `RUNNING` faria o detector de órfã fechar uma execução
    perfeitamente sadia, e o artefato pronto iria para o lixo.
    """
    from backup_runner import worker
    from backup_runner.tick import run_tick

    cria_job(tmp_path, "diario")
    estado = State()
    estado.enqueue("diario", dt.datetime.now())
    producao = worker.processa_um(estado, RAIA_PRODUCAO)

    r = run_tick(dt.datetime.now() + dt.timedelta(hours=2), state=estado)
    depois = estado.get_run(producao.run.id)
    estado.close()

    assert r.abandonadas == []
    assert depois.result is RunResult.QUEUED


def test_staging_de_quem_espera_envio_nao_e_varrido(tmp_path):
    """Apagar o artefato de quem só espera a raia seria destruir backup pronto."""
    from backup_runner import worker
    from backup_runner.tick import run_tick

    cria_job(tmp_path, "diario")
    estado = State()
    estado.enqueue("diario", dt.datetime.now())
    producao = worker.processa_um(estado, RAIA_PRODUCAO)
    pasta = staging_dir() / "diario" / estado.get_run(producao.run.id).folder
    assert pasta.is_dir()

    run_tick(dt.datetime.now() + dt.timedelta(hours=2), state=estado)
    estado.close()

    assert pasta.is_dir(), "a varredura apagou artefato que ainda ia ser enviado"


def test_envio_perdido_volta_para_a_fila(tmp_path):
    """Artefato pronto sem item de envio ficaria no disco para sempre."""
    from backup_runner import worker
    from backup_runner.tick import run_tick

    cria_job(tmp_path, "diario")
    estado = State()
    estado.enqueue("diario", dt.datetime.now())
    worker.processa_um(estado, RAIA_PRODUCAO)

    # Alguém perdeu o item: worker morto no momento errado, banco mexido à mão.
    estado.clear_queue()
    assert estado.claim_next(RAIA_ENVIO) is None

    run_tick(dt.datetime.now(), state=estado)
    assert estado.claim_next(RAIA_ENVIO) is not None, "o backup pronto ficou órfão"
    estado.close()


def test_espera_vencida_vira_falha_e_libera_disco(tmp_path):
    """Toda espera precisa de prazo, inclusive a que está entre as duas fases."""
    from backup_runner import worker
    from backup_runner.config import Settings
    from backup_runner.tick import run_tick

    cria_job(tmp_path, "diario")
    estado = State()
    estado.enqueue("diario", dt.datetime.now())
    producao = worker.processa_um(estado, RAIA_PRODUCAO)
    pasta = staging_dir() / "diario" / estado.get_run(producao.run.id).folder
    estado.clear_queue()

    horas = Settings.load().staging_hold_hours
    run_tick(dt.datetime.now() + dt.timedelta(hours=horas * 2), state=estado)
    final = estado.get_run(producao.run.id)
    estado.close()

    assert final.result is RunResult.FAILED
    assert "sem o envio chegar a acontecer" in final.error_cause
    assert not pasta.exists()
