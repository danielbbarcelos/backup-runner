"""Modo sync: espelho de uma pasta grande no S3, sem staging.

O S3 é um dicionário em memória que imita as quatro chamadas que o sync usa
(listagem paginada, upload_file, delete_objects). O que está sob teste é a
decisão: o que sobe, o que fica, o que é apagado, e quando o sync se recusa a
apagar.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pytest

from backup_runner.config import DestinationStore, JobStore, encrypt
from backup_runner.destinations import S3Backend, SyncRecusado
from backup_runner.models import (
    Destination, DestKind, ExcludePattern, FilesMode, FilesSource, Job, JobDestination, RunResult,
)
from backup_runner.state import State


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


class S3Falso:
    def __init__(self):
        self.objetos: dict[str, dict] = {}
        self.uploads: list[str] = []
        self.falhar: set[str] = set()

    def get_paginator(self, _nome):
        return self

    def paginate(self, Bucket, Prefix):
        chaves = sorted(k for k in self.objetos if k.startswith(Prefix))
        for i in range(0, max(len(chaves), 1), 2):  # páginas de 2 para exercitar a paginação
            yield {"Contents": [{"Key": k, "Size": self.objetos[k]["size"],
                                 "LastModified": self.objetos[k]["modified"]} for k in chaves[i:i + 2]]}

    def upload_file(self, caminho, Bucket, Key, Config=None, ExtraArgs=None):
        if Key in self.falhar:
            raise RuntimeError("SlowDown")
        self.uploads.append(Key)
        self.objetos[Key] = {"size": os.path.getsize(caminho),
                             "modified": dt.datetime.now(dt.timezone.utc),
                             "class": (ExtraArgs or {}).get("StorageClass", "STANDARD")}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objetos.pop(o["Key"], None)
        return {}


@pytest.fixture
def s3(monkeypatch):
    falso = S3Falso()
    monkeypatch.setattr(S3Backend, "cliente", lambda self: falso)
    return falso


def destino(storage_class="STANDARD_IA") -> Destination:
    return Destination(
        name="s3-files", kind=DestKind.S3, bucket="backup-runner-peersaude", region="us-east-2",
        prefix="files", access_key="chave", secret_enc=encrypt("segredo"), storage_class=storage_class,
    )


@pytest.fixture
def origem(tmp_path) -> Path:
    base = tmp_path / "file_manager"
    (base / "private" / "2026").mkdir(parents=True)
    for i in range(10):
        (base / "private" / "2026" / f"doc{i}.pdf").write_bytes(b"x" * (100 + i))
    (base / "cache").mkdir()
    (base / "cache" / "lixo.bin").write_bytes(b"c")
    return base


def envelhece(origem: Path) -> None:
    """mtime no passado, como num servidor: o objeto que subiu agora é mais novo."""
    antigo = time.time() - 3600
    for raiz, _, arquivos in os.walk(origem):
        for nome in arquivos:
            os.utime(os.path.join(raiz, nome), (antigo, antigo))


def test_primeira_vez_sobe_tudo_com_a_classe_e_respeita_exclusoes(s3, origem):
    envelhece(origem)
    r = S3Backend(destino()).sync(origem, "peersaude-files", excludes=["cache/**"])
    assert r.uploaded == 10 and r.deleted == 0 and r.unchanged == 0
    assert all(k.startswith("files/peersaude-files/private/2026/") for k in s3.objetos)
    assert {o["class"] for o in s3.objetos.values()} == {"STANDARD_IA"}
    assert not any("cache" in k for k in s3.objetos)


def test_segunda_vez_sem_mudanca_nao_sobe_nada(s3, origem):
    envelhece(origem)
    S3Backend(destino()).sync(origem, "peersaude-files")
    s3.uploads.clear()
    r = S3Backend(destino()).sync(origem, "peersaude-files")
    assert r.uploaded == 0 and r.unchanged == r.total_files == 11 and not s3.uploads


def test_sobe_o_alterado_e_o_novo_e_apaga_o_que_sumiu(s3, origem):
    envelhece(origem)
    S3Backend(destino()).sync(origem, "peersaude-files")
    s3.uploads.clear()

    pasta = origem / "private" / "2026"
    futuro = time.time() + 60
    (pasta / "doc1.pdf").write_bytes(b"y" * 101)          # mesmo tamanho, conteúdo novo
    os.utime(pasta / "doc1.pdf", (futuro, futuro))
    (pasta / "novo.pdf").write_bytes(b"n" * 50)
    (pasta / "doc9.pdf").unlink()

    r = S3Backend(destino()).sync(origem, "peersaude-files")
    assert sorted(k.rsplit("/", 1)[1] for k in s3.uploads) == ["doc1.pdf", "novo.pdf"]
    assert r.deleted == 1
    assert "files/peersaude-files/private/2026/doc9.pdf" not in s3.objetos


def test_remocao_em_massa_e_recusada_sem_apagar_nada(s3, origem):
    envelhece(origem)
    S3Backend(destino()).sync(origem, "peersaude-files")
    antes = set(s3.objetos)
    for i in range(5):
        (origem / "private" / "2026" / f"doc{i}.pdf").unlink()

    with pytest.raises(SyncRecusado) as erro:
        S3Backend(destino()).sync(origem, "peersaude-files", max_delete_pct=5)
    assert erro.value.a_apagar == 5 and erro.value.total == 11
    assert set(s3.objetos) == antes          # nada apagado

    r = S3Backend(destino()).sync(origem, "peersaude-files", max_delete_pct=50)
    assert r.deleted == 5                    # com o limite maior, de propósito, apaga


def test_um_arquivo_que_falha_nao_derruba_os_outros(s3, origem):
    envelhece(origem)
    s3.falhar.add("files/peersaude-files/private/2026/doc3.pdf")
    r = S3Backend(destino()).sync(origem, "peersaude-files")
    assert r.uploaded == 10 and len(r.failed) == 1 and "doc3.pdf" in r.failed[0]


def test_job_sync_ponta_a_ponta_sem_staging(s3, origem):
    from backup_runner import worker
    from backup_runner.config import staging_dir

    envelhece(origem)
    DestinationStore.load().put(destino())
    job = Job(name="peersaude-files",
              source=FilesSource(path=str(origem), mode=FilesMode.SYNC,
                                 excludes=[ExcludePattern("cache/**", True)]),
              destinations=[JobDestination("s3-files")])
    JobStore.load().put(job)
    estado = State()
    estado.enqueue(job.name, dt.datetime.now())
    feitos = worker.drena(estado)
    run = estado.get_run(feitos[-1].run.id)
    estado.close()

    assert run.result is RunResult.OK
    assert run.destinations_done == ["s3-files"]
    assert run.bytes == sum(100 + i for i in range(10))
    assert "espelho de 10 arquivos" in run.artifact
    assert not any(staging_dir().iterdir())   # nada passou pelo staging


def test_job_sync_com_destino_que_nao_e_s3_falha_explicando(tmp_path, origem):
    from backup_runner import worker

    DestinationStore.load().put(Destination(name="disco", kind=DestKind.LOCAL, path=str(tmp_path / "d")))
    job = Job(name="arquivos", source=FilesSource(path=str(origem), mode=FilesMode.SYNC),
              destinations=[JobDestination("disco")])
    JobStore.load().put(job)
    estado = State()
    estado.enqueue(job.name, dt.datetime.now())
    run = estado.get_run(worker.drena(estado)[-1].run.id)
    estado.close()
    assert run.result is RunResult.FAILED
    assert "só funciona com S3" in run.error_cause
