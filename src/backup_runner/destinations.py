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


class Registro(Protocol):
    """Onde um envio em partes anota o que já subiu, para poder continuar.

    Implementado por `state.PartesDeEnvio`. Está declarado aqui como protocolo
    para este módulo não precisar conhecer o banco: ele sabe enviar, não sabe
    onde o programa guarda histórico.
    """

    def upload_id(self, destino: str, chave: str) -> str | None: ...
    def guarda_upload(self, destino: str, chave: str, upload_id: str) -> None: ...
    def guarda_parte(self, destino: str, chave: str, upload_id: str,
                     numero: int, etag: str, tamanho: int) -> None: ...
    def partes(self, destino: str, chave: str) -> dict[int, str]: ...
    def esquece(self, destino: str, chave: str) -> None: ...


class Backend(Protocol):
    def test(self) -> TestResult: ...
    def upload(self, pasta: Path, prefixo: str, on_progress: Callable[[int], None] | None,
               registro: "Registro | None" = None) -> int: ...
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

    def upload(self, pasta: Path, prefixo: str, on_progress=None,
               registro: "Registro | None" = None) -> int:
        alvo = self.base / prefixo
        alvo.mkdir(parents=True, exist_ok=True)
        enviados = 0
        for arquivo in sorted(pasta.iterdir()):
            if not arquivo.is_file():
                continue
            # Copia para `.parcial` e renomeia no fim. Sem isto, uma cópia
            # interrompida fica com o nome final, e aí ela parece artefato
            # completo para a retenção e para quem for restaurar. O multipart
            # do S3 dá essa atomicidade de graça, porque o objeto só aparece no
            # `complete`; aqui ela tem que ser feita à mão.
            final = alvo / arquivo.name
            meio = alvo / (arquivo.name + PARCIAL)
            shutil.copy2(arquivo, meio)
            meio.replace(final)
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

# Quanto esperar por uma resposta já pedida. Ver o comentário no cliente: é o
# `CompleteMultipartUpload` de um objeto grande que precisa disto.
READ_TIMEOUT = 900

# Acima deste tamanho o boto3 parte o arquivo; abaixo, manda de uma vez.
LIMIAR_MULTIPART = 16 * 1024 * 1024

# O S3 aceita no máximo dez mil partes por objeto.
MAX_PARTES = 10_000

# Quantas partes sobem ao mesmo tempo. Dez é o que o boto3 usava por padrão, e
# numa rede doméstica saturar mais que isso só aumenta a chance de uma falhar.
CONCORRENCIA = 10

# Sufixo do arquivo em andamento. Só o nome definitivo significa "backup
# completo", e é por isso que a retenção e a restauração podem confiar nele.
# O multipart do S3 dá essa atomicidade de graça, porque o objeto só aparece no
# `complete`. No destino local e no SFTP ela tem que ser feita à mão.
PARCIAL = ".parcial"


def tamanho_de_parte(tamanho: int) -> int:
    """Quantos bytes por parte, para um arquivo deste tamanho.

    64 MB é o ponto de partida, e não os 8 MB padrão do boto3, porque o tempo
    da montagem final no provedor cresce com o número de partes, e foi essa
    montagem que estourou o tempo de leitura e custou um backup inteiro. Num
    arquivo de doze gigabytes isto é a diferença entre 1500 partes e 182.

    A parte dobra conforme necessário para caber nas dez mil que o S3 aceita,
    então a conta continua valendo para arquivo de qualquer tamanho.
    """
    parte = 64 * 1024 * 1024
    while tamanho / parte > MAX_PARTES:
        parte *= 2
    return parte


def _transferencia(tamanho: int):
    """Como partir este arquivo, em função do tamanho dele.

    O padrão do boto3 é parte de 8 MB, que num arquivo de doze gigabytes dá
    mil e quinhentas partes. Montar mil e quinhentas partes no fim é justamente
    a chamada que estourou o tempo e custou um backup inteiro.

    Partes de 64 MB derrubam isso para menos de duzentas, e a conta continua
    valendo para arquivos muito maiores: a parte cresce até caber nas dez mil
    que o S3 permite. Partes maiores também significam menos requisições, o que
    numa rede doméstica é menos chance de uma delas falhar.
    """
    from boto3.s3.transfer import TransferConfig

    parte = tamanho_de_parte(tamanho)
    return TransferConfig(
        multipart_threshold=LIMIAR_MULTIPART,
        multipart_chunksize=parte,
        max_concurrency=10,
        use_threads=True,
    )


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
                # Conectar é rápido ou não é: quinze segundos bastam, e esperar
                # mais por um endpoint que não responde só atrasa o diagnóstico.
                connect_timeout=15,
                # Ler, não. O padrão do botocore é sessenta segundos, e foi ele
                # que jogou fora um backup de doze gigabytes: as mil e quinhentas
                # partes subiram em três horas e quarenta e cinco minutos, e aí o
                # `CompleteMultipartUpload` passou de um minuto montando o objeto
                # no lado do provedor. O cliente desistiu, o S3Transfer abortou o
                # multipart, e as três horas e quarenta e cinco viraram nada.
                #
                # A montagem final é proporcional ao número de partes, e acontece
                # inteira dentro de uma única resposta HTTP. Quinze minutos é
                # folga para um objeto grande sem deixar um destino morto pendurar
                # o worker por horas.
                read_timeout=READ_TIMEOUT,
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

    def upload(self, pasta: Path, prefixo: str, on_progress=None,
               registro: "Registro | None" = None) -> int:
        cliente = self.cliente()
        enviados = 0
        for arquivo in sorted(pasta.iterdir()):
            if not arquivo.is_file():
                continue
            chave = self._chave(prefixo, arquivo.name)
            tamanho = arquivo.stat().st_size
            base = enviados

            def progresso(feito: int, inicio=base) -> None:
                if on_progress:
                    on_progress(inicio + feito)

            if registro is not None and tamanho >= LIMIAR_MULTIPART:
                self._em_partes(cliente, arquivo, chave, tamanho, registro, progresso)
            else:
                # Sem registro para anotar, ou arquivo pequeno: o caminho do
                # boto3 serve, e em arquivo pequeno retomar não compra nada.
                # O boto3 entrega o tamanho do bloco, não o total já enviado (o
                # paramiko faz o contrário), então quem soma é este contador.
                conta = {"n": 0}

                def bloco(n: int, c=conta) -> None:
                    c["n"] += n
                    progresso(c["n"])

                cliente.upload_file(
                    str(arquivo), self.destino.bucket, chave,
                    Callback=bloco, Config=_transferencia(tamanho),
                )
            enviados += tamanho
        return enviados

    # -- envio em partes, com retomada --------------------------------

    def _em_partes(self, cliente, arquivo: Path, chave: str, tamanho: int,
                   registro: "Registro", progresso) -> None:
        """Sobe o arquivo em partes, continuando de onde parou.

        O caminho do `upload_file` é tudo ou nada: ele aborta o multipart
        quando falha, e foi assim que um envio de doze gigabytes, com as mil e
        quinhentas partes já no provedor depois de três horas e quarenta e
        cinco minutos, virou nada por um tempo de leitura estourado na montagem
        final.

        A ordem dos passos aqui não é arbitrária. Primeiro pergunta se o objeto
        já existe, porque a montagem pode ter concluído do lado do provedor
        **depois** de o cliente desistir, e foi exatamente esse o caso de
        2026-09-16. Depois confere no provedor quais partes existem, porque o
        provedor é a verdade e o registro local é só o índice de qual multipart
        continuar.
        """
        nome = self.destino.name

        if self._objeto_confere(cliente, chave, tamanho):
            # Já está lá, inteiro. Acontece quando o `complete` deu certo e a
            # resposta não chegou a tempo.
            registro.esquece(nome, chave)
            progresso(tamanho)
            return

        upload_id = registro.upload_id(nome, chave)
        if upload_id is not None and not self._multipart_vivo(cliente, chave, upload_id):
            # O provedor não conhece mais aquele multipart: expirou, foi
            # abortado, ou já foi concluído. Começar de novo é a única saída.
            registro.esquece(nome, chave)
            upload_id = None

        if upload_id is None:
            upload_id = cliente.create_multipart_upload(
                Bucket=self.destino.bucket, Key=chave,
            )["UploadId"]
            registro.guarda_upload(nome, chave, upload_id)

        parte = tamanho_de_parte(tamanho)
        total = max(1, -(-tamanho // parte))
        prontas = self._partes_no_provedor(cliente, chave, upload_id)
        for numero, etag in prontas.items():
            registro.guarda_parte(nome, chave, upload_id, numero, etag,
                                  min(parte, tamanho - (numero - 1) * parte))

        feito = sum(min(parte, tamanho - (n - 1) * parte) for n in prontas)
        progresso(feito)
        faltando = [n for n in range(1, total + 1) if n not in prontas]

        if faltando:
            self._sobe_faltando(
                cliente, arquivo, chave, upload_id, parte, tamanho,
                faltando, prontas, registro, progresso, feito,
            )

        self._conclui(cliente, chave, upload_id, prontas, tamanho, registro)
        progresso(tamanho)

    def _sobe_faltando(self, cliente, arquivo: Path, chave: str, upload_id: str,
                       parte: int, tamanho: int, faltando: list[int],
                       prontas: dict[int, str], registro: "Registro",
                       progresso, feito: int) -> int:
        """Sobe em paralelo as partes que faltam, anotando cada uma que chega.

        Quem anota é esta thread, não as que sobem: a conexão do SQLite não
        atravessa thread, e colher no `as_completed` mantém a escrita onde ela
        pode acontecer. Parte anotada é parte que não se reenvia.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        nome = self.destino.name

        def sobe(numero: int) -> tuple[int, str, int]:
            inicio = (numero - 1) * parte
            quanto = min(parte, tamanho - inicio)
            with arquivo.open("rb") as f:
                f.seek(inicio)
                corpo = f.read(quanto)
            r = cliente.upload_part(
                Bucket=self.destino.bucket, Key=chave, UploadId=upload_id,
                PartNumber=numero, Body=corpo,
            )
            return numero, r["ETag"], quanto

        erro: Exception | None = None
        with ThreadPoolExecutor(max_workers=CONCORRENCIA) as pool:
            futuros = [pool.submit(sobe, n) for n in faltando]
            for fut in as_completed(futuros):
                # Colhe todos antes de levantar. Soltar a exceção no meio do
                # laço deixaria sem registro as partes que deram certo, e com
                # dez subindo em paralelo uma falha descartaria até nove
                # sucessos. O `list_parts` da próxima tentativa as recuperaria,
                # mas jogar fora trabalho concluído não é opção.
                try:
                    numero, etag, quanto = fut.result()
                except Exception as exc:  # noqa: BLE001
                    erro = erro or exc
                    continue
                prontas[numero] = etag
                registro.guarda_parte(nome, chave, upload_id, numero, etag, quanto)
                feito += quanto
                progresso(feito)
        if erro is not None:
            raise erro
        return feito

    def _conclui(self, cliente, chave: str, upload_id: str,
                 prontas: dict[int, str], tamanho: int, registro: "Registro") -> None:
        """Manda o provedor montar o objeto, e trata o tempo estourado.

        Esta é a chamada que custou o backup de 2026-09-16. Ela acontece inteira
        dentro de uma resposta HTTP, e o tempo dela cresce com o número de
        partes. Se estourar, **não se aborta**: o registro fica, a tentativa
        seguinte pergunta se o objeto existe, e normalmente ele existe, porque o
        provedor terminou de montar depois de o cliente desistir.
        """
        partes = [{"PartNumber": n, "ETag": prontas[n]} for n in sorted(prontas)]
        try:
            cliente.complete_multipart_upload(
                Bucket=self.destino.bucket, Key=chave, UploadId=upload_id,
                MultipartUpload={"Parts": partes},
            )
        except Exception:
            if self._objeto_confere(cliente, chave, tamanho):
                registro.esquece(self.destino.name, chave)
                return
            raise
        registro.esquece(self.destino.name, chave)

    # -- perguntas ao provedor ----------------------------------------

    def _objeto_confere(self, cliente, chave: str, tamanho: int) -> bool:
        """O objeto já está lá, com o tamanho certo?"""
        try:
            r = cliente.head_object(Bucket=self.destino.bucket, Key=chave)
        except Exception:
            return False
        return int(r.get("ContentLength", -1)) == tamanho

    def _multipart_vivo(self, cliente, chave: str, upload_id: str) -> bool:
        try:
            cliente.list_parts(
                Bucket=self.destino.bucket, Key=chave, UploadId=upload_id, MaxParts=1,
            )
        except Exception:
            return False
        return True

    def _partes_no_provedor(self, cliente, chave: str, upload_id: str) -> dict[int, str]:
        """O que o provedor diz que já recebeu. É esta a verdade."""
        prontas: dict[int, str] = {}
        marcador = None
        while True:
            kw = dict(Bucket=self.destino.bucket, Key=chave, UploadId=upload_id)
            if marcador is not None:
                kw["PartNumberMarker"] = marcador
            r = cliente.list_parts(**kw)
            for parte in r.get("Parts", []):
                prontas[parte["PartNumber"]] = parte["ETag"]
            if not r.get("IsTruncated"):
                return prontas
            marcador = r["NextPartNumberMarker"]

    def aborta(self, chave: str, upload_id: str) -> bool:
        """Descarta um multipart pendente, para não pagar por parte pendurada.

        Guardar o `upload_id` para retomar é assumir esta responsabilidade:
        antes era o boto3 que abortava sozinho ao falhar.
        """
        try:
            self.cliente().abort_multipart_upload(
                Bucket=self.destino.bucket, Key=chave, UploadId=upload_id,
            )
        except Exception:
            return False
        return True

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

    def upload(self, pasta: Path, prefixo: str, on_progress=None,
               registro: "Registro | None" = None) -> int:
        cliente = self._conecta()
        enviados = 0
        try:
            sftp = cliente.open_sftp()
            # Sem isto, transferência parada fica parada para sempre: o
            # `timeout` do connect cobre só a conexão, e um destino que congela
            # no meio travava o worker sem prazo nenhum, do mesmo jeito que o
            # S3 travava antes do `read_timeout`.
            canal = sftp.get_channel()
            if canal is not None:
                canal.settimeout(READ_TIMEOUT)
            alvo = f"{self.destino.remote_path.rstrip('/')}/{prefixo}"
            self._garante_pasta(sftp, alvo)
            for arquivo in sorted(pasta.iterdir()):
                if not arquivo.is_file():
                    continue
                enviados += self._envia_um(sftp, arquivo, alvo, enviados, on_progress)
        finally:
            cliente.close()
        return enviados

    def _envia_um(self, sftp, arquivo: Path, alvo: str, base: int, on_progress) -> int:
        """Sobe um arquivo, continuando de onde parou se já houver pedaço lá.

        O nome só vira o definitivo quando o arquivo está inteiro. O arquivo em
        andamento mora no `.parcial`, que é o que torna a retomada segura: um
        pedaço com o nome final pareceria backup completo para a retenção.
        """
        tamanho = arquivo.stat().st_size
        final = f"{alvo}/{arquivo.name}"
        meio = final + PARCIAL

        if self._tamanho_remoto(sftp, final) == tamanho:
            if on_progress:
                on_progress(base + tamanho)
            return tamanho

        feito = self._tamanho_remoto(sftp, meio) or 0
        if feito > tamanho:
            feito = 0                      # pedaço maior que a origem não serve
        modo = "ab" if feito else "wb"

        with arquivo.open("rb") as origem, sftp.open(meio, modo) as destino:
            destino.set_pipelined(True)
            origem.seek(feito)
            while True:
                bloco = origem.read(1024 * 1024)
                if not bloco:
                    break
                destino.write(bloco)
                feito += len(bloco)
                if on_progress:
                    on_progress(base + feito)

        try:
            sftp.remove(final)
        except IOError:
            pass
        sftp.rename(meio, final)
        return tamanho

    def _tamanho_remoto(self, sftp, caminho: str) -> int | None:
        try:
            return sftp.stat(caminho).st_size
        except IOError:
            return None

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
