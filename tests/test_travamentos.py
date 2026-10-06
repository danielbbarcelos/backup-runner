"""Defeitos conhecidos e ainda não corrigidos, do estudo de 2026-10-06.

Estes testes descrevem comportamento que o programa **deveria** ter e hoje não
tem. Ficam marcados como `xfail(strict=True)`, o que significa duas coisas: a
suíte continua verde enquanto o defeito existe, e no dia em que alguém
corrigir, o teste passa a falhar por "passou inesperadamente" e obriga a tirar
a marca. É a forma de um defeito conhecido não virar defeito esquecido.

O estudo completo, com a causa comum e a ordem de correção proposta, está em
`Projetos/labs/[2026-09-14] Backup Runner — backup agendado de bancos e arquivos.md`,
seção "Arquivos grandes: progresso, retomada e desistência".
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from backup_runner.config import JobStore, Settings, staging_dir
from backup_runner.models import FilesSource, Job, Run, RunResult
from backup_runner.state import State, _fmt
from backup_runner.tick import run_tick


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


def job_noturno(nome="noturno") -> Job:
    job = Job(name=nome, source=FilesSource(path="/tmp"), destinations=[],
              schedule="0 3 * * *", catch_up_window_minutes=120)
    JobStore.load().put(job)
    return job


@pytest.mark.xfail(strict=True, reason="defeito 1 do estudo: linha de fila presa mata o job")
def test_fila_presa_em_running_nao_pode_matar_o_job():
    """Worker morto depois do claim deixa a fila em 'running' para sempre.

    `_avaliar` decide `ja_na_fila` com `queue_pending()`, que inclui 'running',
    então toda janela futura é descartada. O `_fecha_orfas` da v0.6.0 conserta a
    linha de `runs` e não toca na de `queue`, então fechar a órfã não basta.

    Agrava: `queue_size()` conta só 'pending', então o `status` mostra "fila
    vazia" enquanto a linha fantasma bloqueia tudo, e o bloqueio acontece antes
    da lógica de janela perdida, então nem esse aviso sai.
    """
    job_noturno()
    estado = State()

    # O worker reivindicou e morreu: fila em 'running', execução em 'running'
    # com pid que não existe mais.
    estado.enqueue("noturno", dt.datetime(2026, 10, 1, 3, 0))
    estado.claim_next()
    run = Run(id=0, job="noturno", started_at=dt.datetime(2026, 10, 1, 3, 0),
              result=RunResult.RUNNING)
    estado.insert_run(run)
    estado.progresso(run.id, stage="dump", pid=999_999)
    estado.conn.execute("UPDATE runs SET heartbeat=? WHERE id=?",
                        (_fmt(dt.datetime(2026, 10, 1, 3, 5)), run.id))

    # Cinco dias depois, o tick tem que voltar a enfileirar as 3h.
    enfileirados = 0
    for dia in (6, 7, 8, 9):
        r = run_tick(dt.datetime(2026, 10, dia, 9, 0), state=estado)
        enfileirados += len(r.enfileirados)
    estado.close()

    assert enfileirados > 0, "o job parou de ser feito e nada avisou"


@pytest.mark.xfail(strict=True, reason="defeito 2 do estudo: pendente sem estado terminal")
def test_pendente_que_esgota_tentativas_vira_falha_e_libera_disco():
    """Todo estado de espera precisa de prazo para um estado terminal.

    Passado o `staging_hold_hours`, `_reenvios_pendentes` só para de tentar. A
    execução fica em 'pending' indefinidamente, sem virar falha, sem aviso, e
    com o artefato ocupando disco para sempre. A doze gigabytes por ocorrência,
    enche disco calado.
    """
    job_noturno("grande")
    estado = State()
    hold = Settings.load().staging_hold_hours

    inicio = dt.datetime(2026, 10, 1, 3, 0)
    run = Run(id=0, job="grande", started_at=inicio,
              finished_at=inicio + dt.timedelta(hours=4),
              result=RunResult.PENDING_UPLOAD, bytes=12_000_000_000)
    run.destinations_pending = ["spaces"]
    run.retry_count = 3
    run.retry_at = inicio + dt.timedelta(hours=5)
    estado.insert_run(run)

    pasta = staging_dir() / "grande" / run.folder
    pasta.mkdir(parents=True)
    (pasta / "artefato.zip").write_bytes(b"x" * 100_000)

    # Bem depois do prazo de retenção do staging.
    run_tick(inicio + dt.timedelta(hours=hold * 3), state=estado)
    final = estado.get_run(run.id)
    estado.close()

    assert final.result is RunResult.FAILED, "espera sem saída: ficou 'pending' para sempre"
    assert not pasta.exists(), "o artefato abandonado continua ocupando disco"
