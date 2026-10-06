"""O vigia: batida de coração e prazo fora do callback de progresso.

A razão de existir está num caso real de 2026-09-16. O worker informava que
estava vivo, cobrava o próprio prazo e contava progresso pelo mesmo canal: o
callback da biblioteca que fazia a entrada e saída. Quando essa biblioteca
bloqueou dentro de uma única chamada, as três coisas pararam juntas, "empacado"
e "morto" ficaram indistinguíveis, e o prazo de quatro horas deixou de ser
cobrado exatamente quando era necessário.

O que está sob teste aqui é a separação dos três sinais, e principalmente o
caso que a cooperação não resolve: a thread principal presa numa chamada que
não volta.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backup_runner.models import FilesSource, Job, Run, RunResult
from backup_runner.state import State
from backup_runner.worker import Sentinela, Vigilancia


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


def execucao(estado: State, *, inicio: dt.datetime | None = None) -> Run:
    """Execução em curso de um job que existe.

    Cadastrar o job importa: `_por_que_parar` checa cancelamento, depois job
    apagado, depois prazo. Sem cadastro, "job apagado" venceria sempre e o
    teste mediria outra coisa.
    """
    from backup_runner.config import JobStore
    from backup_runner.models import FilesSource, Job

    JobStore.load().put(Job(name="vigiado", source=FilesSource(path="/tmp"),
                            destinations=[], schedule="0 3 * * *"))
    run = Run(id=0, job="vigiado", started_at=inicio or dt.datetime.now(),
              result=RunResult.RUNNING)
    estado.insert_run(run)
    return run


def espera(condicao, limite=3.0, passo=0.02):
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(passo)
    return False


# ----------------------------------------------------------------------------
# A batida, que é o sinal novo
# ----------------------------------------------------------------------------

def test_bate_o_coracao_sem_nenhum_progresso_acontecendo():
    """É esta a diferença que faz o resto funcionar.

    Nenhum callback é chamado durante este teste, nenhuma entrada e saída
    acontece, e ainda assim a batida avança. Era o que faltava para separar
    worker empacado de worker morto.
    """
    estado = State()
    run = execucao(estado)
    limite = dt.datetime.now() + dt.timedelta(hours=1)

    assert estado.progresso_de(run.id)["heartbeat"] is None
    with Vigilancia(estado, run, "vigiado", limite, intervalo=0.02):
        assert espera(lambda: estado.progresso_de(run.id)["heartbeat"] is not None)
        primeira = estado.progresso_de(run.id)["heartbeat"]
        assert espera(lambda: estado.progresso_de(run.id)["heartbeat"] != primeira)
    estado.close()


def test_o_vigia_usa_conexao_propria_e_nao_a_de_quem_o_criou():
    """O sqlite3 recusa conexão usada fora da thread que a criou.

    Sem conexão própria, todo ciclo do vigia levantava ProgrammingError, e como
    o laço engole exceção para não derrubar o backup, o vigia ficava mudo
    fingindo vigiar. Foi assim que este defeito apareceu.
    """
    estado = State()
    run = execucao(estado)
    with Vigilancia(estado, run, "vigiado",
                    dt.datetime.now() + dt.timedelta(hours=1), intervalo=0.02) as v:
        assert espera(lambda: estado.progresso_de(run.id)["heartbeat"] is not None)
        assert v.falhas == 0, "o vigia está engolindo erro em silêncio"
    estado.close()


# ----------------------------------------------------------------------------
# As três razões de parar, publicadas para a sentinela
# ----------------------------------------------------------------------------

def test_prazo_estourado_e_publicado_para_a_sentinela():
    estado = State()
    run = execucao(estado)
    passado = dt.datetime.now() - dt.timedelta(minutes=1)

    with Vigilancia(estado, run, "vigiado", passado, intervalo=0.02) as v:
        assert espera(lambda: v.motivo is not None)
        assert v.motivo[0] == "prazo"
        with pytest.raises(TimeoutError):
            Sentinela(v)()
    estado.close()


def test_cancelamento_pedido_e_publicado_para_a_sentinela():
    from backup_runner.worker import Cancelado

    estado = State()
    run = execucao(estado)
    with Vigilancia(estado, run, "vigiado",
                    dt.datetime.now() + dt.timedelta(hours=1), intervalo=0.02) as v:
        sentinela = Sentinela(v)
        sentinela()                      # nada pedido ainda, não levanta
        estado.pede_cancelamento(run.id)
        assert espera(lambda: v.motivo is not None)
        assert v.motivo[0] == "cancelado"
        with pytest.raises(Cancelado):
            sentinela()
    estado.close()


def test_sentinela_nao_toca_o_disco():
    """O caminho quente do progresso não pode ter entrada e saída.

    A sentinela é chamada a cada bloco lido. Ela só lê o que o vigia já
    decidiu, e a versão anterior relia o arquivo de jobs de dois em dois
    segundos sem necessidade.
    """
    estado = State()
    run = execucao(estado)
    vigia = Vigilancia(estado, run, "vigiado", dt.datetime.now() + dt.timedelta(hours=1))
    sentinela = Sentinela(vigia)         # vigia nem foi iniciado

    import backup_runner.config as cfg
    antes = cfg.JobStore.load
    cfg.JobStore.load = lambda *a, **k: pytest.fail("a sentinela foi ao disco")
    try:
        for _ in range(500):
            sentinela()
    finally:
        cfg.JobStore.load = antes
    estado.close()


# ----------------------------------------------------------------------------
# O caso que a cooperação não resolve
# ----------------------------------------------------------------------------

def test_principal_presa_numa_chamada_e_encerrada_pelo_vigia(tmp_path):
    """Thread principal bloqueada para sempre, prazo estourado.

    Python não interrompe syscall bloqueada de fora, então não há saída
    elegante. O que tem que acontecer é o vigia gravar o desfecho **antes** de
    encerrar o processo, senão a execução vira órfã e a linha de fila fica
    presa, trocando um travamento por outro.

    Roda em processo separado de propósito, porque o vigia chama `os._exit`.
    """
    codigo = f'''
import datetime as dt, os, sys, time
sys.path.insert(0, {str(Path.cwd() / "src")!r})
os.environ["XDG_CONFIG_HOME"] = {str(tmp_path / "config")!r}
os.environ["XDG_DATA_HOME"] = {str(tmp_path / "data")!r}
from backup_runner.config import JobStore
from backup_runner.models import FilesSource, Job, Run, RunResult
from backup_runner.state import State
from backup_runner.worker import Vigilancia

JobStore.load().put(Job(name="preso", source=FilesSource(path="/tmp"),
                        destinations=[], schedule="0 3 * * *"))
estado = State()
run = Run(id=0, job="preso", started_at=dt.datetime.now(), result=RunResult.RUNNING)
estado.insert_run(run)
fila = estado.enqueue("preso", dt.datetime.now())
item = estado.claim_next()
print("PRONTO", run.id, item["id"], flush=True)

# Prazo já vencido, e graça curta para o teste não demorar.
vigia = Vigilancia(estado, run, "preso", dt.datetime.now() - dt.timedelta(minutes=1),
                   item["id"], intervalo=0.05, graca=0.3)
vigia.start()
# A principal nunca mais devolve controle, como no CompleteMultipartUpload.
time.sleep(60)
print("NAO DEVERIA CHEGAR AQUI", flush=True)
'''
    proc = subprocess.run([sys.executable, "-c", codigo], capture_output=True,
                          text=True, timeout=30)

    assert "PRONTO" in proc.stdout
    assert "NAO DEVERIA CHEGAR AQUI" not in proc.stdout
    assert proc.returncode == 75, f"saída inesperada: {proc.returncode} {proc.stderr}"

    # E o estado no banco tem que estar consistente, que é o ponto todo.
    estado = State()
    run = estado.runs(limit=1)[0]
    assert run.result is RunResult.FAILED
    assert "tempo limite" in run.error_cause
    assert "não devolveu controle" in run.error_got
    presas = [x for x in estado.conn.execute("SELECT status FROM queue")]
    assert [x["status"] for x in presas] == ["abandoned"], "a fila ficou presa"
    estado.close()
