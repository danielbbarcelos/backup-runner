"""Testes do destino S3, com foco no que custou um backup inteiro.

Um envio de doze gigabytes chegou a subir as mil e quinhentas partes em quase
quatro horas e mesmo assim falhou, porque a chamada final que manda o provedor
montar o objeto passou dos sessenta segundos que o botocore espera por padrão.
O que está sob teste aqui é a configuração que evita isso: tempo de leitura
generoso, e partes grandes o bastante para a montagem final ser curta.

Pulam quando o boto3 não está instalado, porque o ambiente de teste não o traz.
"""
from __future__ import annotations

import pytest

boto3 = pytest.importorskip("boto3")

from backup_runner.destinations import (  # noqa: E402
    LIMIAR_MULTIPART,
    MAX_PARTES,
    READ_TIMEOUT,
    S3Backend,
    _transferencia,
)
from backup_runner.models import Destination, DestKind  # noqa: E402


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


def destino_s3() -> Destination:
    from backup_runner.config import encrypt

    return Destination(
        name="spaces", kind=DestKind.S3, bucket="dbmv.cold-storage",
        endpoint="nyc3.digitaloceanspaces.com", region="nyc3",
        access_key="chave", secret_enc=encrypt("segredo"), retention_days=30,
    )


def test_cliente_espera_muito_pela_resposta_e_pouco_pela_conexao():
    """O padrão de 60s do botocore é o que jogou fora quatro horas de envio."""
    cfg = S3Backend(destino_s3()).cliente().meta.config
    assert cfg.read_timeout == READ_TIMEOUT >= 900
    assert cfg.connect_timeout == 15


def test_partes_grandes_para_a_montagem_final_ser_curta():
    """Doze gigabytes em partes de 8 MB dão 1500 partes. Em 64 MB, menos de 200."""
    doze_gb = 12_243_496_327
    cfg = _transferencia(doze_gb)
    assert cfg.multipart_chunksize == 64 * 1024 * 1024
    assert doze_gb / cfg.multipart_chunksize < 200


@pytest.mark.parametrize("tamanho", [
    1,
    LIMIAR_MULTIPART,
    12_243_496_327,
    5 * 1024 ** 4,       # 5 TB
    500 * 1024 ** 4,     # absurdo de propósito: a conta não pode quebrar
])
def test_nunca_passa_do_teto_de_partes_do_s3(tamanho):
    """Acima de dez mil partes o S3 recusa o objeto inteiro, no fim de tudo."""
    cfg = _transferencia(tamanho)
    assert tamanho / cfg.multipart_chunksize <= MAX_PARTES


def test_arquivo_pequeno_nao_vira_multipart():
    cfg = _transferencia(1024)
    assert cfg.multipart_threshold == LIMIAR_MULTIPART > 1024
