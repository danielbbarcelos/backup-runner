"""Retomada do envio em partes, sem rede.

O caso real: um envio de doze gigabytes subiu as mil e quinhentas partes em
três horas e quarenta e cinco minutos e virou nada, porque o
`CompleteMultipartUpload` passou dos sessenta segundos de tempo de leitura
padrão e o `upload_file` do boto3, que é tudo ou nada, abortou o multipart.

O provedor de mentira aqui registra cada chamada e sabe falhar nos mesmos
pontos, o que permite testar a retomada sem subir um byte para a rede.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backup_runner.destinations import S3Backend, tamanho_de_parte
from backup_runner.models import Destination, DestKind
from backup_runner.state import PartesDeEnvio, State


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    yield


class Provedor:
    """S3 de mentira, com o bastante para exercitar o multipart."""

    def __init__(self) -> None:
        self.objetos: dict[str, int] = {}
        self.uploads: dict[str, dict[int, tuple[str, int]]] = {}
        self.chamadas: list[str] = []
        self.falhar_parte: set[int] = set()
        self.falhar_complete = 0
        self.complete_monta_mesmo_falhando = False
        self._seq = 0

    # -- a API que o código usa ---------------------------------------

    def head_object(self, Bucket, Key):
        self.chamadas.append("head")
        if Key not in self.objetos:
            raise RuntimeError("404")
        return {"ContentLength": self.objetos[Key]}

    def create_multipart_upload(self, Bucket, Key):
        self._seq += 1
        uid = f"UP{self._seq}"
        self.uploads[uid] = {}
        self.chamadas.append("create")
        return {"UploadId": uid}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.chamadas.append(f"part{PartNumber}")
        if PartNumber in self.falhar_parte:
            raise RuntimeError(f"rede caiu na parte {PartNumber}")
        etag = f'"etag-{PartNumber}"'
        self.uploads[UploadId][PartNumber] = (etag, len(Body))
        return {"ETag": etag}

    def list_parts(self, Bucket, Key, UploadId, MaxParts=None, PartNumberMarker=None):
        self.chamadas.append("list_parts")
        if UploadId not in self.uploads:
            raise RuntimeError("NoSuchUpload")
        partes = [
            {"PartNumber": n, "ETag": e, "Size": t}
            for n, (e, t) in sorted(self.uploads[UploadId].items())
        ]
        return {"Parts": partes, "IsTruncated": False}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self.chamadas.append("complete")
        if self.falhar_complete > 0:
            self.falhar_complete -= 1
            if self.complete_monta_mesmo_falhando:
                # O provedor terminou de montar depois de o cliente desistir.
                self.objetos[Key] = sum(t for _, t in self.uploads[UploadId].values())
            raise RuntimeError("Read timeout on endpoint URL")
        self.objetos[Key] = sum(t for _, t in self.uploads[UploadId].values())
        del self.uploads[UploadId]
        return {"ETag": '"final"'}

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.chamadas.append("abort")
        self.uploads.pop(UploadId, None)
        return {}


# Parte minúscula só nos testes. Com a de produção, 64 MB, cada caso destes
# gravaria duzentos megabytes em disco para exercitar lógica que não depende do
# tamanho. O mínimo real do S3 é 5 MiB, e isso é regra do provedor, não nossa.
PARTE_DE_TESTE = 4096


def monta(tmp_path, monkeypatch, provedor, *, partes=4):
    """Backend apontado para o provedor de mentira, com arquivo de N partes."""
    import backup_runner.destinations as d

    monkeypatch.setattr(d, "tamanho_de_parte", lambda _n: PARTE_DE_TESTE)
    monkeypatch.setattr(d, "LIMIAR_MULTIPART", PARTE_DE_TESTE // 2)

    destino = Destination(name="spaces", kind=DestKind.S3, bucket="b",
                          endpoint="e.example.com", region="r",
                          access_key="k", retention_days=7)
    motor = S3Backend(destino)
    monkeypatch.setattr(motor, "cliente", lambda: provedor)

    pasta = tmp_path / "staging"
    pasta.mkdir()
    (pasta / "dump.sql.gz").write_bytes(b"z" * (PARTE_DE_TESTE * (partes - 1) + 1234))
    return motor, pasta, PARTE_DE_TESTE


def registro(run_id=1):
    return PartesDeEnvio(State(), run_id)


# ----------------------------------------------------------------------------
# O caminho feliz
# ----------------------------------------------------------------------------

def test_envio_em_partes_sobe_tudo_e_limpa_o_registro(tmp_path, monkeypatch):
    prov = Provedor()
    motor, pasta, parte = monta(tmp_path, monkeypatch, prov)
    reg = registro()

    enviados = motor.upload(pasta, "job/2026", registro=reg)

    tamanho = (pasta / "dump.sql.gz").stat().st_size
    assert enviados == tamanho
    assert prov.objetos["job/2026/dump.sql.gz"] == tamanho
    # Registro vazio depois do sucesso: não há o que retomar.
    assert reg.abertos() == []


def test_progresso_e_monotonico_e_chega_ao_total(tmp_path, monkeypatch):
    prov = Provedor()
    motor, pasta, parte = monta(tmp_path, monkeypatch, prov)
    vistos: list[int] = []

    motor.upload(pasta, "job/2026", on_progress=vistos.append, registro=registro())

    assert vistos == sorted(vistos), "a barra andou para trás"
    assert vistos[-1] == (pasta / "dump.sql.gz").stat().st_size


# ----------------------------------------------------------------------------
# A retomada, que é a razão de tudo isso existir
# ----------------------------------------------------------------------------

def test_segunda_tentativa_so_sobe_a_parte_que_faltou(tmp_path, monkeypatch):
    """Era aqui que três horas e quarenta e cinco minutos viravam nada."""
    prov = Provedor()
    motor, pasta, parte = monta(tmp_path, monkeypatch, prov, partes=4)
    reg = registro()

    prov.falhar_parte = {3}
    with pytest.raises(RuntimeError):
        motor.upload(pasta, "job/2026", registro=reg)

    # O upload_id sobreviveu, com as partes que deram certo.
    guardado = reg.upload_id("spaces", "job/2026/dump.sql.gz")
    assert guardado is not None
    assert set(reg.partes("spaces", "job/2026/dump.sql.gz")) >= {1, 2}

    prov.falhar_parte = set()
    prov.chamadas.clear()
    motor.upload(pasta, "job/2026", registro=reg)

    # Só a parte 3 (e a 4, se ela também não tinha subido) foi reenviada.
    reenviadas = {c for c in prov.chamadas if c.startswith("part")}
    assert "part1" not in reenviadas, "reenviou parte que já estava no provedor"
    assert "part2" not in reenviadas
    assert "part3" in reenviadas
    assert prov.objetos["job/2026/dump.sql.gz"] == (pasta / "dump.sql.gz").stat().st_size


def test_complete_estourou_mas_o_provedor_montou_e_isso_e_sucesso(
    tmp_path, monkeypatch,
):
    """O cliente desiste por tempo e o provedor termina de montar.

    Conferido que o objeto está lá com o tamanho certo, o envio deu certo e não
    há por que levantar. Abortar aqui jogaria fora o objeto que já existe, e é
    exatamente o que o `upload_file` do boto3 fazia.
    """
    prov = Provedor()
    motor, pasta, parte = monta(tmp_path, monkeypatch, prov)
    reg = registro()
    tamanho = (pasta / "dump.sql.gz").stat().st_size

    prov.falhar_complete = 1
    prov.complete_monta_mesmo_falhando = True

    enviados = motor.upload(pasta, "job/2026", registro=reg)

    assert enviados == tamanho
    assert prov.objetos["job/2026/dump.sql.gz"] == tamanho
    assert "abort" not in prov.chamadas, "abortou e jogaria fora o objeto montado"
    assert reg.abertos() == [], "o registro ficou com multipart que já concluiu"


def test_complete_estourou_e_o_objeto_nao_existe_entao_retoma_sem_reenviar(
    tmp_path, monkeypatch,
):
    """O caso exato de 2026-09-16.

    O tempo de leitura estourou na montagem e o objeto não apareceu: a listagem
    do bucket deu zero objeto e a de multiparts deu zero upload, porque o
    S3Transfer abortou. Aqui não se aborta, então a tentativa seguinte só
    repete a montagem, sem subir um byte de novo.
    """
    prov = Provedor()
    motor, pasta, parte = monta(tmp_path, monkeypatch, prov, partes=4)
    reg = registro()
    tamanho = (pasta / "dump.sql.gz").stat().st_size

    prov.falhar_complete = 1
    with pytest.raises(RuntimeError, match="Read timeout"):
        motor.upload(pasta, "job/2026", registro=reg)

    assert "abort" not in prov.chamadas
    assert reg.upload_id("spaces", "job/2026/dump.sql.gz") is not None
    assert len(reg.partes("spaces", "job/2026/dump.sql.gz")) == 4

    prov.chamadas.clear()
    enviados = motor.upload(pasta, "job/2026", registro=reg)

    assert enviados == tamanho
    assert not any(c.startswith("part") for c in prov.chamadas), \
        "reenviou parte que já estava no provedor"
    assert "complete" in prov.chamadas
    assert prov.objetos["job/2026/dump.sql.gz"] == tamanho


def test_uma_parte_falha_mas_as_outras_ficam_registradas(tmp_path, monkeypatch):
    """Dez partes sobem em paralelo. Uma falhar não pode apagar as nove."""
    prov = Provedor()
    motor, pasta, parte = monta(tmp_path, monkeypatch, prov, partes=5)
    reg = registro()

    prov.falhar_parte = {2}
    with pytest.raises(RuntimeError):
        motor.upload(pasta, "job/2026", registro=reg)

    guardadas = set(reg.partes("spaces", "job/2026/dump.sql.gz"))
    assert guardadas == {1, 3, 4, 5}, f"sucessos foram descartados: {guardadas}"


def test_multipart_que_o_provedor_esqueceu_recomeca(tmp_path, monkeypatch):
    """Multipart expirado ou abortado do outro lado. Começar de novo é a saída."""
    prov = Provedor()
    motor, pasta, parte = monta(tmp_path, monkeypatch, prov)
    reg = registro()
    reg.guarda_upload("spaces", "job/2026/dump.sql.gz", "UP-QUE-NAO-EXISTE")

    motor.upload(pasta, "job/2026", registro=reg)

    assert "create" in prov.chamadas
    assert prov.objetos["job/2026/dump.sql.gz"] == (pasta / "dump.sql.gz").stat().st_size


def test_objeto_ja_completo_nao_sobe_nada(tmp_path, monkeypatch):
    prov = Provedor()
    motor, pasta, parte = monta(tmp_path, monkeypatch, prov)
    tamanho = (pasta / "dump.sql.gz").stat().st_size
    prov.objetos["job/2026/dump.sql.gz"] = tamanho

    enviados = motor.upload(pasta, "job/2026", registro=registro())

    assert enviados == tamanho
    assert not any(c.startswith("part") for c in prov.chamadas)
    assert "create" not in prov.chamadas


# ----------------------------------------------------------------------------
# Sem registro, o comportamento antigo
# ----------------------------------------------------------------------------

def test_sem_registro_usa_o_caminho_do_boto3(tmp_path, monkeypatch):
    """Quem chama sem registro não ganha retomada, e nada quebra."""
    pytest.importorskip("boto3")   # `_transferencia` monta um TransferConfig
    prov = Provedor()
    motor, pasta, parte = monta(tmp_path, monkeypatch, prov, partes=2)
    chamadas = []
    prov.upload_file = lambda *a, **k: chamadas.append(a)  # type: ignore[attr-defined]

    motor.upload(pasta, "job/2026")

    assert chamadas, "não usou upload_file"
    assert "create" not in prov.chamadas
