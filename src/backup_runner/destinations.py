"""Para onde o backup vai: pasta local, S3 compatível, SFTP.

Os três respondem à mesma interface, então o worker não sabe a diferença e a
retenção é a mesma conta nos três. A pasta de cada execução carrega a data no
nome, e é dela que sai a idade: copiar arquivo mexe no mtime, o nome não mente.

    <job>/2026-09-15_03-00-00/
        dump_loja_prod.sql.gz
        dump.log
        manifest.json

boto3 e paramiko só são importados quando o destino daquele tipo é usado, para
quem só tem pasta local não pagar o tempo de carregar as duas.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .config import decrypt
from .models import Destination, DestKind

# A pasta de uma execução: data e hora, sem espaço e sem dois-pontos.
PASTA = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})$")


class DestinationError(Exception):
    pass


def normaliza_endpoint(endpoint: str, bucket: str) -> str:
    """Tira o bucket do endpoint, quando ele veio junto.

    O painel da DigitalOcean mostra a URL completa do bucket
    (`meu-bucket.nyc3.digitaloceanspaces.com`), e é natural copiar aquilo para
    o campo de endpoint. Mas o cliente espera o endpoint da região: com o
    bucket junto, ele monta `meu-bucket.meu-bucket.nyc3...` e nada funciona.
    """
    limpo = endpoint.strip().rstrip("/")
    for prefixo in ("https://", "http://"):
        if limpo.startswith(prefixo):
            limpo = limpo[len(prefixo):]
    if bucket and limpo.startswith(f"{bucket}."):
        limpo = limpo[len(bucket) + 1:]
    return limpo


def estilo_endereco(bucket: str) -> str:
    """`path` quando o bucket tem ponto no nome, `virtual` no resto.

    No endereçamento virtual, o bucket vira subdomínio do endpoint. Um bucket
    chamado `dbmv.cold-storage` produz `dbmv.cold-storage.nyc3.digitalocean...`,
    e o certificado curinga do provedor (`*.nyc3.digitalocean...`) cobre apenas
    um nível, então a conexão é recusada por nome que não confere.

    Com `path`, o bucket vai no caminho e o host continua sendo o da região,
    que o certificado cobre.
    """
    return "path" if "." in bucket else "virtual"


@dataclass
class TestResult:
    ok: bool
    mensagem: str = ""
    tried: str = ""
    got: str = ""
    cause: str = ""
    fix: str = ""
    seconds: float = 0.0


@dataclass
class RemoteRun:
    prefix: str            # <job>/<pasta>
    started_at: dt.datetime
    bytes: int = 0


def parse_pasta(nome: str) -> dt.datetime | None:
    """A data que está no nome da pasta, que é a idade da execução."""
    casou = PASTA.match(nome)
    if not casou:
        return None
    dia, hora, minuto, segundo = casou.groups()
    try:
        return dt.datetime.strptime(f"{dia} {hora}:{minuto}:{segundo}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


class Backend(Protocol):
    def test(self) -> TestResult: ...
    def upload(self, pasta: Path, prefixo: str, on_progress: Callable[[int], None] | None) -> int: ...
    def list_runs(self, job: str) -> list[RemoteRun]: ...
    def delete_run(self, prefixo: str) -> int: ...


def backend(destino: Destination) -> Backend:
    if destino.kind is DestKind.LOCAL:
        return LocalBackend(destino)
    if destino.kind is DestKind.S3:
        return S3Backend(destino)
    return SFTPBackend(destino)


# ----------------------------------------------------------------------------
# Pasta local
# ----------------------------------------------------------------------------

class LocalBackend:
    def __init__(self, destino: Destination) -> None:
        self.destino = destino
        self.base = Path(destino.path).expanduser()

    def test(self) -> TestResult:
        inicio = dt.datetime.now()
        try:
            if not self.base.exists():
                if not self.destino.create_missing:
                    return TestResult(
                        False, "o diretório não existe",
                        tried=f"escrever em {self.base}",
                        cause="o caminho não existe e 'criar se faltar' está desligado",
                        fix=f"mkdir -p {self.base}",
                    )
                self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
            sonda = self.base / f".probe-{dt.datetime.now():%m%d%H%M%S}"
            sonda.write_text("backup-runner")
            sonda.unlink()
        except OSError as exc:
            return TestResult(
                False, str(exc),
                tried=f"escrever e apagar uma sonda em {self.base}",
                got=str(exc),
                cause="permissão ou disco",
                fix=f"confira as permissões de {self.base}",
            )
        levou = (dt.datetime.now() - inicio).total_seconds()
        return TestResult(True, f"escreveu e apagou uma sonda em {levou:.1f}s", seconds=levou)

    def upload(self, pasta: Path, prefixo: str, on_progress=None) -> int:
        alvo = self.base / prefixo
        alvo.mkdir(parents=True, exist_ok=True)
        enviados = 0
        for arquivo in sorted(pasta.iterdir()):
            if not arquivo.is_file():
                continue
            shutil.copy2(arquivo, alvo / arquivo.name)
            enviados += arquivo.stat().st_size
            if on_progress:
                on_progress(enviados)
        return enviados

    def list_runs(self, job: str) -> list[RemoteRun]:
        raiz = self.base / job
        if not raiz.is_dir():
            return []
        execucoes = []
        for pasta in raiz.iterdir():
            quando = parse_pasta(pasta.name) if pasta.is_dir() else None
            if quando is None:
                continue
            tamanho = sum(f.stat().st_size for f in pasta.rglob("*") if f.is_file())
            execucoes.append(RemoteRun(f"{job}/{pasta.name}", quando, tamanho))
        return sorted(execucoes, key=lambda r: r.started_at)

    def delete_run(self, prefixo: str) -> int:
        alvo = self.base / prefixo
        if not alvo.is_dir():
            return 0
        tamanho = sum(f.stat().st_size for f in alvo.rglob("*") if f.is_file())
        shutil.rmtree(alvo, ignore_errors=True)
        return tamanho


# ----------------------------------------------------------------------------
# S3 compatível
# ----------------------------------------------------------------------------

class S3Backend:
    def __init__(self, destino: Destination) -> None:
        self.destino = destino
        self._cliente = None

    def cliente(self):
        if self._cliente is not None:
            return self._cliente
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover
            raise DestinationError("boto3 não está instalado") from exc

        segredo = decrypt(self.destino.secret_enc)
        if not (self.destino.access_key and segredo):
            raise DestinationError("faltam as credenciais deste destino")

        endpoint = normaliza_endpoint(self.destino.endpoint, self.destino.bucket)
        if endpoint and not endpoint.startswith("http"):
            endpoint = f"https://{endpoint}"

        self._cliente = boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            region_name=self.destino.region or None,
            aws_access_key_id=self.destino.access_key,
            aws_secret_access_key=segredo,
            config=Config(
                # Três tentativas no modo adaptativo já lidam com o 503
                # ocasional de um Spaces ocupado, sem transformar cada soluço
                # em falha.
                retries={"max_attempts": 3, "mode": "adaptive"},
                s3={"addressing_style": estilo_endereco(self.destino.bucket)},
            ),
        )
        return self._cliente

    def _chave(self, prefixo: str, nome: str = "") -> str:
        partes = [self.destino.prefix.strip("/"), prefixo.strip("/"), nome]
        return "/".join(p for p in partes if p)

    def test(self) -> TestResult:
        inicio = dt.datetime.now()
        chave = self._chave("", f".probe-{dt.datetime.now():%m%d%H%M%S}")
        try:
            cliente = self.cliente()
            cliente.put_object(Bucket=self.destino.bucket, Key=chave, Body=b"backup-runner")
            cliente.delete_object(Bucket=self.destino.bucket, Key=chave)
        except DestinationError as exc:
            falta_lib = "boto3" in str(exc)
            return TestResult(
                False, str(exc),
                tried=f"conectar em {self.destino.location()}",
                cause=str(exc),
                fix=(
                    "reinstale o programa: backup-runner self reinstall --limpo"
                    if falta_lib
                    else "abra o destino e preencha a chave e o secret"
                ),
            )
        except Exception as exc:
            return TestResult(
                False, str(exc),
                tried=f"gravar e apagar {chave} em {self.destino.bucket}",
                got=_resumo_erro(exc),
                cause=_causa_s3(exc),
                fix="confira endpoint, bucket, chave e secret",
            )
        levou = (dt.datetime.now() - inicio).total_seconds()
        return TestResult(True, f"gravou e apagou uma sonda em {levou:.1f}s", seconds=levou)

    def upload(self, pasta: Path, prefixo: str, on_progress=None) -> int:
        cliente = self.cliente()
        enviados = 0
        for arquivo in sorted(pasta.iterdir()):
            if not arquivo.is_file():
                continue
            # O boto3 entrega o tamanho do bloco, não o total já enviado (o
            # paramiko faz o contrário), então quem soma é este contador.
            corrente = {"n": enviados}

            def progresso(bloco: int, conta=corrente) -> None:
                conta["n"] += bloco
                if on_progress:
                    on_progress(conta["n"])

            # upload_file faz multipart sozinho acima de 8 MB, com retomada das
            # partes: um dump de 2 GB numa rede doméstica não recomeça do zero.
            cliente.upload_file(
                str(arquivo), self.destino.bucket, self._chave(prefixo, arquivo.name),
                Callback=progresso,
            )
            enviados += arquivo.stat().st_size
        return enviados

    def list_runs(self, job: str) -> list[RemoteRun]:
        cliente = self.cliente()
        raiz = self._chave(job) + "/"
        paginador = cliente.get_paginator("list_objects_v2")
        tamanhos: dict[str, int] = {}
        for pagina in paginador.paginate(Bucket=self.destino.bucket, Prefix=raiz):
            for objeto in pagina.get("Contents", []):
                resto = objeto["Key"][len(raiz):]
                if "/" not in resto:
                    continue
                pasta = resto.split("/", 1)[0]
                tamanhos[pasta] = tamanhos.get(pasta, 0) + objeto.get("Size", 0)

        execucoes = []
        for pasta, tamanho in tamanhos.items():
            quando = parse_pasta(pasta)
            if quando is not None:
                execucoes.append(RemoteRun(f"{job}/{pasta}", quando, tamanho))
        return sorted(execucoes, key=lambda r: r.started_at)

    def delete_run(self, prefixo: str) -> int:
        cliente = self.cliente()
        raiz = self._chave(prefixo) + "/"
        paginador = cliente.get_paginator("list_objects_v2")
        apagados = 0
        for pagina in paginador.paginate(Bucket=self.destino.bucket, Prefix=raiz):
            objetos = [{"Key": o["Key"]} for o in pagina.get("Contents", [])]
            if not objetos:
                continue
            apagados += sum(o.get("Size", 0) for o in pagina["Contents"])
            cliente.delete_objects(Bucket=self.destino.bucket, Delete={"Objects": objetos})
        return apagados


def _resumo_erro(exc: Exception) -> str:
    texto = str(exc)
    return texto if len(texto) < 300 else texto[:297] + "…"


def _causa_s3(exc: Exception) -> str:
    texto = str(exc)
    if "SSL validation failed" in texto or "hostname" in texto and "doesn't match" in texto:
        return (
            "o nome do host não bate com o certificado do provedor; "
            "costuma ser bucket com ponto no nome, ou o bucket repetido no endpoint"
        )
    if "SignatureDoesNotMatch" in texto:
        return "a secret key não confere"
    if "InvalidAccessKeyId" in texto:
        return "a access key não existe nesse provedor"
    if "NoSuchBucket" in texto:
        return "o bucket não existe nessa região"
    if "AccessDenied" in texto:
        return "a chave não tem permissão nesse bucket"
    if "EndpointConnectionError" in texto or "Could not connect" in texto:
        return "o endpoint não respondeu"
    return "resposta inesperada do provedor"


# ----------------------------------------------------------------------------
# SFTP
# ----------------------------------------------------------------------------

class SFTPBackend:
    def __init__(self, destino: Destination) -> None:
        self.destino = destino

    def _conecta(self):
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover
            raise DestinationError("paramiko não está instalado") from exc

        cliente = paramiko.SSHClient()
        cliente.load_system_host_keys()
        # O host precisa estar no known_hosts. Aceitar chave desconhecida
        # automaticamente tiraria justamente a proteção contra alguém no meio.
        cliente.set_missing_host_key_policy(paramiko.RejectPolicy())

        d = self.destino
        parametros = {"hostname": d.host, "port": d.port, "username": d.user, "timeout": 20}
        if d.auth == "key":
            caminho = Path(d.private_key).expanduser()
            if not caminho.exists():
                raise DestinationError(f"a chave {caminho} não existe")
            parametros["key_filename"] = str(caminho)
        else:
            senha = decrypt(d.password_enc)
            if not senha:
                raise DestinationError("falta a senha deste destino")
            parametros["password"] = senha
        cliente.connect(**parametros)
        return cliente

    def test(self) -> TestResult:
        inicio = dt.datetime.now()
        try:
            cliente = self._conecta()
        except DestinationError as exc:
            return TestResult(False, str(exc), tried=f"conectar em {self.destino.location()}",
                              cause=str(exc), fix="abra o destino e confira a autenticação")
        except Exception as exc:
            return TestResult(
                False, _resumo_erro(exc),
                tried=f"ssh {self.destino.user}@{self.destino.host}:{self.destino.port}",
                got=_resumo_erro(exc),
                cause=_causa_sftp(exc),
                fix=_conserto_sftp(self.destino, exc),
            )
        try:
            sftp = cliente.open_sftp()
            self._garante_pasta(sftp, self.destino.remote_path)
            sonda = f"{self.destino.remote_path.rstrip('/')}/.probe-{dt.datetime.now():%m%d%H%M%S}"
            with sftp.open(sonda, "w") as f:
                f.write("backup-runner")
            sftp.remove(sonda)
        except Exception as exc:
            return TestResult(
                False, _resumo_erro(exc),
                tried=f"escrever em {self.destino.remote_path}",
                got=_resumo_erro(exc),
                cause="conectou, mas não conseguiu escrever",
                fix=f"confira se {self.destino.user} pode escrever em {self.destino.remote_path}",
            )
        finally:
            cliente.close()
        levou = (dt.datetime.now() - inicio).total_seconds()
        return TestResult(True, f"escreveu e apagou uma sonda em {levou:.1f}s", seconds=levou)

    @staticmethod
    def _garante_pasta(sftp, caminho: str) -> None:
        partes = [p for p in caminho.strip("/").split("/") if p]
        atual = "/" if caminho.startswith("/") else ""
        for parte in partes:
            atual = f"{atual.rstrip('/')}/{parte}"
            try:
                sftp.stat(atual)
            except IOError:
                sftp.mkdir(atual)

    def upload(self, pasta: Path, prefixo: str, on_progress=None) -> int:
        cliente = self._conecta()
        enviados = 0
        try:
            sftp = cliente.open_sftp()
            alvo = f"{self.destino.remote_path.rstrip('/')}/{prefixo}"
            self._garante_pasta(sftp, alvo)
            for arquivo in sorted(pasta.iterdir()):
                if not arquivo.is_file():
                    continue
                acumulado = enviados

                def progresso(feito: int, total: int, base=acumulado) -> None:
                    if on_progress:
                        on_progress(base + feito)

                sftp.put(str(arquivo), f"{alvo}/{arquivo.name}", callback=progresso)
                enviados += arquivo.stat().st_size
        finally:
            cliente.close()
        return enviados

    def list_runs(self, job: str) -> list[RemoteRun]:
        cliente = self._conecta()
        execucoes = []
        try:
            sftp = cliente.open_sftp()
            raiz = f"{self.destino.remote_path.rstrip('/')}/{job}"
            try:
                entradas = sftp.listdir_attr(raiz)
            except IOError:
                return []
            for entrada in entradas:
                quando = parse_pasta(entrada.filename)
                if quando is None:
                    continue
                tamanho = 0
                try:
                    for arquivo in sftp.listdir_attr(f"{raiz}/{entrada.filename}"):
                        tamanho += arquivo.st_size or 0
                except IOError:
                    pass
                execucoes.append(RemoteRun(f"{job}/{entrada.filename}", quando, tamanho))
        finally:
            cliente.close()
        return sorted(execucoes, key=lambda r: r.started_at)

    def delete_run(self, prefixo: str) -> int:
        cliente = self._conecta()
        apagados = 0
        try:
            sftp = cliente.open_sftp()
            alvo = f"{self.destino.remote_path.rstrip('/')}/{prefixo}"
            try:
                for arquivo in sftp.listdir_attr(alvo):
                    apagados += arquivo.st_size or 0
                    sftp.remove(f"{alvo}/{arquivo.filename}")
                sftp.rmdir(alvo)
            except IOError:
                return apagados
        finally:
            cliente.close()
        return apagados


def _causa_sftp(exc: Exception) -> str:
    texto = str(exc)
    if "Authentication failed" in texto or "publickey" in texto:
        return "o servidor recusou a autenticação"
    if "not found in known_hosts" in texto or "Server" in texto and "not found" in texto:
        return "o host não está no known_hosts desta máquina"
    if "timed out" in texto or "Unable to connect" in texto:
        return "o host não respondeu"
    return "falha na conexão ssh"


def _conserto_sftp(destino: Destination, exc: Exception) -> str:
    texto = str(exc)
    if "known_hosts" in texto:
        return f"ssh-keyscan -H {destino.host} >> ~/.ssh/known_hosts"
    if destino.auth == "key":
        return f"ssh-copy-id -i {destino.private_key} {destino.user}@{destino.host}"
    return f"ssh {destino.user}@{destino.host} e confira o acesso"


# ----------------------------------------------------------------------------
# Retenção
# ----------------------------------------------------------------------------

@dataclass
class Retencao:
    apagadas: list[str] = field(default_factory=list)
    bytes_liberados: int = 0
    erro: str = ""


def aplica_retencao(destino: Destination, job: str, dias: int, *, agora: dt.datetime | None = None) -> Retencao:
    """Apaga as execuções mais velhas que `dias`, contando pelo nome da pasta.

    Só roda depois de um envio bem sucedido, porque apagar o antigo antes de o
    novo chegar é a maneira mais rápida de ficar sem backup nenhum.
    """
    agora = agora or dt.datetime.now()
    corte = agora - dt.timedelta(days=dias)
    resultado = Retencao()
    try:
        motor = backend(destino)
        for execucao in motor.list_runs(job):
            if execucao.started_at < corte:
                resultado.bytes_liberados += motor.delete_run(execucao.prefix)
                resultado.apagadas.append(execucao.prefix)
    except Exception as exc:
        resultado.erro = _resumo_erro(exc)
    return resultado
