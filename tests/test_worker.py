"""Testes do worker: o backup acontecendo de ponta a ponta.

Usam um `mysqldump` de mentira e um destino em pasta, então rodam em qualquer
máquina, sem banco e sem rede. O que está sob teste é a costura: produzir,
enviar, registrar, aplicar retenção, limpar o staging, e o que acontece quando
um desses passos falha.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import stat
import tarfile
from pathlib import Path

import pytest

from backup_runner.config import DestinationStore, JobStore, staging_dir
from backup_runner.models import (
    Destination,
    DestKind,
    ExcludePattern,
    FilesSource,
    Job,
    JobDestination,
    MySQLSource,
    RunResult,
    Stage,
    StageState,
)
from backup_runner.state import State


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


@pytest.fixture
def destino(tmp_path) -> Destination:
    d = Destination(
        name="disco", kind=DestKind.LOCAL,
        path=str(tmp_path / "destino"), retention_days=7,
    )
    DestinationStore.load().put(d)
    return d


@pytest.fixture
def origem(tmp_path) -> Path:
    base = tmp_path / "uploads"
    (base / "sub").mkdir(parents=True)
    (base / "a.txt").write_text("a" * 3000)
    (base / "sub" / "b.txt").write_text("b" * 3000)
    (base / "lixo.tmp").write_text("t")
    return base


@pytest.fixture
def mysqldump_falso(tmp_path, monkeypatch):
    binario = tmp_path / "bin"
    binario.mkdir(exist_ok=True)
    (binario / "mysql").write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if '--version' in sys.argv or 'VERSION' in ' '.join(sys.argv):\n"
        "    print('8.0.36'); sys.exit(0)\n"
        "for t in os.environ.get('FALSO_TABELAS', 'clientes,pedidos,acesso_log').split(','):\n"
        "    print(f'{t}\\t1000\\t50000\\tBASE TABLE')\n"
    )
    (binario / "mysqldump").write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "for i in range(200):\n"
        "    sys.stdout.write(f'INSERT INTO t VALUES ({i});\\n')\n"
        "sys.exit(int(os.environ.get('FALSO_CODIGO', '0')))\n"
    )
    for nome in ("mysql", "mysqldump"):
        f = binario / nome
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{binario}{os.pathsep}{os.environ['PATH']}")
    return binario


def roda_job(job: Job) -> tuple:
    """Enfileira e processa, devolvendo (resultado, execução)."""
    from backup_runner import worker

    JobStore.load().put(job)
    estado = State()
    estado.enqueue(job.name, dt.datetime.now())
    item = estado.claim_next()
    resultado = worker.executa_item(item, estado)
    estado.finish_queue_item(item["id"], resultado.run.id)
    run = estado.get_run(resultado.run.id)
    estado.close()
    return resultado, run


# ----------------------------------------------------------------------------
# Arquivos
# ----------------------------------------------------------------------------

def test_backup_de_diretorio_ponta_a_ponta(origem, destino, tmp_path):
    job = Job(
        name="uploads",
        source=FilesSource(path=str(origem), excludes=[ExcludePattern("*.tmp", True)]),
        destinations=[JobDestination("disco", 7)],
    )
    resultado, run = roda_job(job)

    assert resultado.ok
    assert run.result is RunResult.OK
    assert run.destinations_done == ["disco"]
    assert not run.destinations_pending

    # O artefato chegou, dentro de uma pasta com a data no nome.
    pastas = list((tmp_path / "destino" / "uploads").iterdir())
    assert len(pastas) == 1
    assert pastas[0].name == run.folder

    arquivos = {f.name for f in pastas[0].iterdir()}
    assert "uploads.tar.gz" in arquivos
    assert "manifest.json" in arquivos

    # A exclusão valeu.
    with tarfile.open(pastas[0] / "uploads.tar.gz") as t:
        nomes = set(t.getnames())
    assert "a.txt" in nomes and "sub/b.txt" in nomes
    assert not [n for n in nomes if n.endswith(".tmp")]


def test_staging_fica_limpo_depois_do_envio(origem, destino):
    job = Job(
        name="uploads", source=FilesSource(path=str(origem)),
        destinations=[JobDestination("disco", 7)],
    )
    roda_job(job)
    sobrou = [p for p in staging_dir().rglob("*") if p.is_file()]
    assert not sobrou, f"o staging guardou {sobrou}"


def test_manifest_tem_hash_do_que_foi_enviado(origem, destino, tmp_path):
    import hashlib
    import json

    job = Job(
        name="uploads", source=FilesSource(path=str(origem)),
        destinations=[JobDestination("disco", 7)],
    )
    _, run = roda_job(job)

    pasta = next((tmp_path / "destino" / "uploads").iterdir())
    manifest = json.loads((pasta / "manifest.json").read_text())
    artefato = pasta / manifest["artifact"]

    real = hashlib.sha256(artefato.read_bytes()).hexdigest()
    assert manifest["sha256"] == real, "o hash do manifest não bate com o arquivo"
    assert run.manifest[0].sha256 == real


# ----------------------------------------------------------------------------
# MySQL
# ----------------------------------------------------------------------------

def test_dump_mysql_sai_comprimido(destino, mysqldump_falso, tmp_path):
    job = Job(
        name="loja",
        source=MySQLSource(host="h", user="u", database="loja", ignore_regex=r"_log$"),
        destinations=[JobDestination("disco", 7)],
    )
    resultado, run = roda_job(job)

    assert resultado.ok, run.error_got
    pasta = next((tmp_path / "destino" / "loja").iterdir())
    artefato = pasta / "dump_loja.sql.gz"
    assert artefato.exists(), [f.name for f in pasta.iterdir()]

    conteudo = gzip.open(artefato, "rt").read()
    assert "INSERT INTO t" in conteudo

    # A regex pegou a tabela de log, e isso ficou registrado na execução.
    assert run.ignored_regex == ["acesso_log"]


def test_dump_que_falha_nao_vira_execucao_bem_sucedida(destino, mysqldump_falso, monkeypatch, tmp_path):
    """O gzip termina feliz mesmo com a origem morta; o registro não pode."""
    monkeypatch.setenv("FALSO_CODIGO", "2")
    job = Job(
        name="loja", source=MySQLSource(host="h", user="u", database="loja"),
        destinations=[JobDestination("disco", 7)],
    )
    resultado, run = roda_job(job)

    assert not resultado.ok
    assert run.result is RunResult.FAILED
    assert run.error_stage is Stage.DUMP
    assert not (tmp_path / "destino" / "loja").exists(), "enviou um dump quebrado"


# ----------------------------------------------------------------------------
# Falhas de destino
# ----------------------------------------------------------------------------

def test_destino_inalcancavel_deixa_a_execucao_pendente(origem, tmp_path):
    """O artefato fica no staging para o reenvio, sem refazer o trabalho."""
    DestinationStore.load().put(Destination(
        name="quebrado", kind=DestKind.LOCAL,
        path="/proc/impossivel", create_missing=True,
    ))
    job = Job(
        name="uploads", source=FilesSource(path=str(origem)),
        destinations=[JobDestination("quebrado", 7)],
    )
    resultado, run = roda_job(job)

    assert not resultado.ok
    assert run.result is RunResult.PENDING_UPLOAD
    assert run.destinations_pending == ["quebrado"]
    assert run.retry_at is not None

    guardado = list((staging_dir() / "uploads").rglob("*.tar.gz"))
    assert guardado, "o artefato não ficou no staging para o reenvio"


def test_um_destino_ok_e_outro_falho_guarda_so_o_que_falta(origem, destino, tmp_path):
    DestinationStore.load().put(Destination(
        name="quebrado", kind=DestKind.LOCAL, path="/proc/impossivel",
    ))
    job = Job(
        name="uploads", source=FilesSource(path=str(origem)),
        destinations=[JobDestination("disco", 7), JobDestination("quebrado", 7)],
    )
    _, run = roda_job(job)

    assert run.destinations_done == ["disco"]
    assert run.destinations_pending == ["quebrado"]
    assert (tmp_path / "destino" / "uploads").exists(), "o destino bom não recebeu"


def test_reenvio_usa_o_artefato_do_staging(origem, destino, tmp_path):
    """Reenviar não pode refazer o dump: o arquivo já existe."""
    from backup_runner import worker

    DestinationStore.load().put(Destination(
        name="quebrado", kind=DestKind.LOCAL, path="/proc/impossivel",
    ))
    job = Job(
        name="uploads", source=FilesSource(path=str(origem)),
        destinations=[JobDestination("disco", 7), JobDestination("quebrado", 7)],
    )
    _, run = roda_job(job)
    assert run.result is RunResult.PENDING_UPLOAD

    # Conserta o destino e manda reenviar.
    store = DestinationStore.load()
    consertado = store.get("quebrado")
    consertado.path = str(tmp_path / "segundo")
    store.put(consertado)

    estado = State()
    resultado = worker.reenvia_pendentes(JobStore.load().get("uploads"), estado)
    depois = estado.get_run(run.id)
    estado.close()

    assert resultado.ok
    assert depois.result is RunResult.OK
    assert not depois.destinations_pending
    assert (tmp_path / "segundo" / "uploads").exists()
    # E o artefato saiu do staging só agora, depois de todos receberem.
    assert not list((staging_dir() / "uploads").rglob("*.tar.gz"))


# ----------------------------------------------------------------------------
# Retenção
# ----------------------------------------------------------------------------

def test_retencao_apaga_o_que_passou_da_idade(origem, destino, tmp_path):
    """A idade sai do nome da pasta, não do mtime: copiar mexe no mtime."""
    antiga = tmp_path / "destino" / "uploads" / "2020-01-01_03-00-00"
    antiga.mkdir(parents=True)
    (antiga / "uploads.tar.gz").write_bytes(b"velho")

    job = Job(
        name="uploads", source=FilesSource(path=str(origem)),
        destinations=[JobDestination("disco", 7)],
    )
    _, run = roda_job(job)

    restantes = {p.name for p in (tmp_path / "destino" / "uploads").iterdir()}
    assert "2020-01-01_03-00-00" not in restantes, "a execução velha sobreviveu"
    assert run.folder in restantes, "a execução nova sumiu"


def test_retencao_nao_roda_quando_o_envio_falha(origem, tmp_path):
    """Apagar o antigo antes de o novo chegar é ficar sem backup nenhum."""
    DestinationStore.load().put(Destination(
        name="quebrado", kind=DestKind.LOCAL, path="/proc/impossivel",
    ))
    velha = tmp_path / "guardado"
    velha.mkdir()
    (velha / "importante.tar.gz").write_bytes(b"nao apague")

    job = Job(
        name="uploads", source=FilesSource(path=str(origem)),
        destinations=[JobDestination("quebrado", 1)],
    )
    roda_job(job)
    assert (velha / "importante.tar.gz").exists()


# ----------------------------------------------------------------------------
# Registro
# ----------------------------------------------------------------------------

def test_execucao_registra_estagios_e_log(origem, destino):
    job = Job(
        name="uploads", source=FilesSource(path=str(origem)),
        destinations=[JobDestination("disco", 7)],
    )
    _, run = roda_job(job)

    etapas = [s.stage for s in run.stages]
    assert Stage.ARCHIVE in etapas
    assert Stage.COMPRESS in etapas
    assert Stage.UPLOAD in etapas
    assert all(s.state is StageState.DONE for s in run.stages)

    origens = {origem for _, origem, _ in run.log}
    assert "worker" in origens and "ok" in origens


def test_job_sumido_no_meio_da_fila_nao_derruba_o_worker():
    from backup_runner import worker

    estado = State()
    estado.enqueue("fantasma", dt.datetime.now())
    item = estado.claim_next()
    resultado = worker.executa_item(item, estado)
    estado.close()

    assert not resultado.ok
    assert resultado.run.result is RunResult.FAILED


# ----------------------------------------------------------------------------
# Endereçamento S3
# ----------------------------------------------------------------------------

def test_endpoint_com_o_bucket_junto_e_corrigido():
    """O painel da DigitalOcean mostra a URL do bucket, e é ela que se copia.

    Com o bucket dentro do endpoint, o cliente monta `bucket.bucket.região` e
    nada funciona.
    """
    from backup_runner.destinations import normaliza_endpoint

    assert normaliza_endpoint(
        "dbmv.cold-storage.nyc3.digitaloceanspaces.com", "dbmv.cold-storage"
    ) == "nyc3.digitaloceanspaces.com"
    assert normaliza_endpoint(
        "https://meubucket.nyc3.digitaloceanspaces.com/", "meubucket"
    ) == "nyc3.digitaloceanspaces.com"
    # Endpoint já correto continua igual.
    assert normaliza_endpoint(
        "nyc3.digitaloceanspaces.com", "meubucket"
    ) == "nyc3.digitaloceanspaces.com"
    # Bucket que por acaso começa igual a um pedaço do host não é removido.
    assert normaliza_endpoint("nyc3.digitaloceanspaces.com", "nyc") == "nyc3.digitaloceanspaces.com"


def test_bucket_com_ponto_usa_caminho_em_vez_de_subdominio():
    """O certificado curinga do provedor cobre um nível só.

    Um bucket `a.b` viraria `a.b.nyc3.provedor.com`, que `*.nyc3.provedor.com`
    não cobre, e a conexão morre em validação de certificado.
    """
    from backup_runner.destinations import estilo_endereco

    assert estilo_endereco("dbmv.cold-storage") == "path"
    assert estilo_endereco("backups.empresa.com") == "path"
    assert estilo_endereco("meubucket") == "virtual"
    assert estilo_endereco("meu-bucket-2026") == "virtual"


def test_erro_de_certificado_explica_o_motivo():
    from backup_runner.destinations import _causa_s3

    erro = Exception(
        "SSL validation failed for https://a.b.nyc3.digitaloceanspaces.com/ "
        "hostname 'a.b.nyc3.digitaloceanspaces.com' doesn't match either of "
        "'*.nyc3.digitaloceanspaces.com'"
    )
    causa = _causa_s3(erro)
    assert "certificado" in causa
    assert "ponto" in causa or "endpoint" in causa
