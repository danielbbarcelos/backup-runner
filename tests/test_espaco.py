"""Guarda de espaço do staging: o backup para antes de encher o disco."""
from __future__ import annotations

import datetime as dt
from collections import namedtuple

import pytest

from backup_runner import espaco
from backup_runner.config import DestinationStore, JobStore, Settings, staging_dir
from backup_runner.models import Destination, DestKind, FilesSource, Job, JobDestination, RunResult, Stage
from backup_runner.state import State

Uso = namedtuple("Uso", "total used free")
GB = 1024 ** 3


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


def disco(monkeypatch, total_gb, livre_gb):
    monkeypatch.setattr(espaco.shutil, "disk_usage",
                        lambda _c: Uso(total_gb * GB, (total_gb - livre_gb) * GB, livre_gb * GB))


def test_recusa_com_pouco_livre_ou_disco_muito_cheio(monkeypatch, tmp_path):
    guarda = espaco.EspacoGuarda(tmp_path, min_livre=5 * GB, max_pct=90)
    disco(monkeypatch, 193, 27)           # o / da Peer Saúde: 86% usado, 27 GB livres
    guarda.checa()
    disco(monkeypatch, 193, 4)            # menos que 5 GB livres
    with pytest.raises(espaco.SemEspaco):
        guarda.checa()
    disco(monkeypatch, 1000, 50)          # 50 GB livres, mas 95% usado
    with pytest.raises(espaco.SemEspaco, match="95% usado"):
        guarda.checa()


def test_no_progresso_le_o_disco_so_de_tempos_em_tempos(monkeypatch, tmp_path):
    leituras = []
    monkeypatch.setattr(espaco.shutil, "disk_usage",
                        lambda c: leituras.append(c) or Uso(100 * GB, 10 * GB, 90 * GB))
    guarda = espaco.EspacoGuarda(tmp_path, min_livre=1, max_pct=99, intervalo=60)
    for _ in range(1000):
        guarda()
    assert len(leituras) == 1


def test_staging_path_muda_o_lugar_do_staging(tmp_path):
    s = Settings.load()
    s.staging_path = str(tmp_path / "volume" / "staging")
    s.save()
    assert staging_dir() == tmp_path / "volume" / "staging"
    assert staging_dir().is_dir()


def test_job_para_antes_de_comecar_com_disco_cheio_e_limpa_o_staging(monkeypatch, tmp_path):
    from backup_runner import worker

    (tmp_path / "origem").mkdir()
    (tmp_path / "origem" / "a.txt").write_text("a")
    DestinationStore.load().put(Destination(name="disco", kind=DestKind.LOCAL, path=str(tmp_path / "d")))
    job = Job(name="uploads", source=FilesSource(path=str(tmp_path / "origem")),
              destinations=[JobDestination("disco")])
    JobStore.load().put(job)
    disco(monkeypatch, 193, 2)

    estado = State()
    estado.enqueue(job.name, dt.datetime.now())
    run = estado.get_run(worker.drena(estado)[-1].run.id)
    estado.close()

    assert run.result is RunResult.FAILED
    assert run.error_stage is Stage.ARCHIVE
    assert "limite configurado" in run.error_cause
    assert "staging_path" in run.error_fix
    assert not any((staging_dir() / "uploads").glob("*"))
