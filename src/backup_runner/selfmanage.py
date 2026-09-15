"""Instalar, desinstalar e reinstalar o próprio backup-runner.

O app é distribuído por pipx a partir do repositório, então instalar é sempre
a mesma operação com uma referência diferente: uma tag de release, o último
release, um commit do main, ou o diretório local durante o desenvolvimento.

A fonte da verdade sobre o que está instalado é o metadata do próprio pipx, e
não um arquivo de estado nosso: assim `self status` continua certo mesmo se
alguém rodar `pipx install` na mão.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import APP_SLUG, __version__

OWNER = "danielbbarcelos"
REPO = "backup-runner"
GIT_URL = f"https://github.com/{OWNER}/{REPO}"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"

HASH = re.compile(r"^[0-9a-f]{7,40}$")
TAG = re.compile(r"^v?\d+\.\d+(\.\d+)?$")


class SelfError(Exception):
    pass


# ----------------------------------------------------------------------------
# Referências
# ----------------------------------------------------------------------------

@dataclass
class Ref:
    """Uma referência resolvida, pronta para virar URL de instalação."""
    tipo: str      # release | tag | commit | branch | local
    valor: str     # a tag, o hash, o nome do branch, ou o caminho
    origem: str    # como foi pedida, para a mensagem

    def spec(self) -> str:
        if self.tipo == "local":
            return self.valor
        return f"git+{GIT_URL}@{self.valor}"

    def descricao(self) -> str:
        rotulos = {
            "release": "release",
            "tag": "tag",
            "commit": "commit no main",
            "branch": "branch",
            "local": "diretório local",
        }
        return f"{rotulos[self.tipo]} {self.valor}"


def _http_json(url: str, *, timeout: int = 10) -> dict:
    requisicao = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_SLUG}/{__version__}",
    })
    try:
        with urllib.request.urlopen(requisicao, timeout=timeout) as resposta:
            return json.loads(resposta.read().decode())
    except urllib.error.HTTPError as exc:
        # 422 é o que o GitHub devolve para um SHA que não existe, e 404 para
        # tag ou release ausente. Para quem digitou a referência, os dois
        # significam a mesma coisa.
        if exc.code in (404, 422):
            raise SelfError(f"não encontrado no repositório: {url.rsplit('/', 1)[-1]}") from exc
        if exc.code == 403:
            raise SelfError(
                "o GitHub recusou (403), provavelmente limite de requisições.\n"
                "tente de novo em alguns minutos, ou passe a tag exata em vez de latest"
            ) from exc
        raise SelfError(f"o GitHub respondeu {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SelfError(f"sem acesso ao GitHub: {exc.reason}") from exc


def resolve_ref(pedido: str | None, *, local: str | None = None) -> Ref:
    """Traduz o que a pessoa pediu para algo que o pipx entende.

    Sem argumento, ou `latest`, é o último release publicado, que é o caminho
    que a maioria quer e o único que não exige saber um identificador.
    """
    if local:
        caminho = Path(local).expanduser().resolve()
        if not (caminho / "pyproject.toml").exists():
            raise SelfError(f"{caminho} não parece um clone do backup-runner")
        return Ref("local", str(caminho), "local")

    pedido = (pedido or "latest").strip()

    if pedido == "latest":
        dados = _http_json(f"{API}/releases/latest")
        tag = dados.get("tag_name")
        if not tag:
            raise SelfError("o repositório ainda não tem release publicado")
        return Ref("release", tag, pedido)

    if pedido in ("main", "master"):
        return Ref("branch", pedido, pedido)

    if TAG.match(pedido):
        tag = pedido if pedido.startswith("v") else f"v{pedido}"
        # Confere que existe antes de mandar o pipx tentar, para o erro sair
        # aqui e não no meio de um clone.
        _http_json(f"{API}/git/ref/tags/{tag}")
        return Ref("tag", tag, pedido)

    if HASH.match(pedido):
        dados = _http_json(f"{API}/commits/{pedido}")
        return Ref("commit", dados.get("sha", pedido)[:40], pedido)

    raise SelfError(
        f"não entendi a referência {pedido!r}.\n"
        "use: latest, uma tag (v0.2.0), um hash de commit, ou main"
    )


def list_releases(limite: int = 10) -> list[tuple[str, str, str]]:
    """(tag, data, título) dos releases publicados."""
    try:
        dados = _http_json(f"{API}/releases?per_page={limite}")
    except SelfError:
        return []
    return [
        (r.get("tag_name", "?"), (r.get("published_at") or "")[:10], r.get("name") or "")
        for r in dados
    ]


# ----------------------------------------------------------------------------
# pipx
# ----------------------------------------------------------------------------

def pipx_path() -> str | None:
    return shutil.which("pipx")


def _rodar(args: list[str], *, titulo: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        return False, f"{args[0]} não encontrado"
    except subprocess.TimeoutExpired:
        return False, f"{titulo} passou de 15 minutos e foi interrompido"
    saida = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, saida


@dataclass
class Installed:
    presente: bool
    versao: str = ""
    spec: str = ""
    caminho: str = ""
    python: str = ""

    def origem(self) -> str:
        """Lê a spec do pipx de volta em linguagem humana."""
        if not self.spec:
            return "desconhecida"
        if self.spec.startswith("git+"):
            resto = self.spec[4:]
            if "@" in resto:
                url, ref = resto.rsplit("@", 1)
                return f"{url.replace('https://github.com/', '')} em {ref}"
            return resto
        return self.spec


def installed() -> Installed:
    """O que o pipx diz sobre a instalação atual."""
    base = Path(
        os.environ.get("PIPX_HOME")
        or (Path.home() / ".local" / "share" / "pipx")
    )
    metadata = base / "venvs" / APP_SLUG / "pipx_metadata.json"
    binario = shutil.which(APP_SLUG)

    if not metadata.exists():
        return Installed(presente=bool(binario), caminho=binario or "")

    try:
        dados = json.loads(metadata.read_text())
    except (json.JSONDecodeError, OSError):
        return Installed(presente=bool(binario), caminho=binario or "")

    principal = (dados.get("main_package") or {})
    return Installed(
        presente=True,
        versao=principal.get("package_version", ""),
        spec=principal.get("package_or_url", ""),
        caminho=binario or "",
        python=dados.get("python_version", ""),
    )


def install(ref: Ref, *, force: bool = False) -> tuple[bool, str]:
    pipx = pipx_path()
    if not pipx:
        return False, (
            "o pipx não está no PATH.\n"
            "instale com: python3 -m pip install --user pipx && python3 -m pipx ensurepath"
        )
    args = [pipx, "install", "--python", "python3"]
    if force:
        args.append("--force")
    args.append(ref.spec())
    return _rodar(args, titulo="a instalação")


def uninstall() -> tuple[bool, str]:
    pipx = pipx_path()
    if not pipx:
        return False, "o pipx não está no PATH"
    return _rodar([pipx, "uninstall", APP_SLUG], titulo="a remoção")


def purge_paths() -> list[Path]:
    """Config e dados, que a remoção não apaga sem pedirem.

    Desinstalar o programa e apagar os backups agendados são decisões
    diferentes, e quem desinstala para reinstalar não quer perder os jobs.
    """
    from .config import config_dir, data_dir

    return [config_dir(), data_dir()]
