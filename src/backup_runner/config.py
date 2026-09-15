"""Stores em disco e cifragem dos segredos.

Duas árvores, de propósito:

  ~/.config/backup-runner/     jobs.json, destinations.json, settings.json
  ~/.local/share/backup-runner/  .key, state.db, staging/

A chave mora fora do diretório de config para o config poder ir para o git ou
para uma pasta sincronizada sem levar a chave junto.

Sobre o que a cifragem entrega: ela protege o arquivo copiado, sincronizado ou
lido por engano. Não protege contra um processo rodando como o mesmo usuário, e
nenhum esquema desacompanhado protege, porque o worker precisa decifrar sozinho
às três da manhã sem ninguém na frente.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import APP_SLUG
from .models import Destination, Job, NotifyMatrix


# ----------------------------------------------------------------------------
# Caminhos
# ----------------------------------------------------------------------------

def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    caminho = Path(base) / APP_SLUG
    caminho.mkdir(parents=True, exist_ok=True)
    return caminho


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    caminho = Path(base) / APP_SLUG
    caminho.mkdir(parents=True, exist_ok=True)
    return caminho


def jobs_file() -> Path:
    return config_dir() / "jobs.json"


def destinations_file() -> Path:
    return config_dir() / "destinations.json"


def settings_file() -> Path:
    return config_dir() / "settings.json"


def key_file() -> Path:
    return data_dir() / ".key"


def state_file() -> Path:
    return data_dir() / "state.db"


def staging_dir() -> Path:
    caminho = data_dir() / "staging"
    caminho.mkdir(parents=True, exist_ok=True)
    return caminho


# ----------------------------------------------------------------------------
# Escrita atômica
# ----------------------------------------------------------------------------

def atomic_write(caminho: Path, dados: str, *, mode: int | None = None) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd, temporario = tempfile.mkstemp(dir=str(caminho.parent), prefix=caminho.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(dados)
        if mode is not None:
            os.chmod(temporario, mode)
        os.replace(temporario, caminho)
    except Exception:
        try:
            os.unlink(temporario)
        except OSError:
            pass
        raise


def _read_json(caminho: Path) -> dict[str, Any]:
    if not caminho.exists():
        return {}
    try:
        return json.loads(caminho.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ----------------------------------------------------------------------------
# Chave e cifragem
# ----------------------------------------------------------------------------

_key_lock = threading.Lock()


def load_or_create_key() -> bytes:
    caminho = key_file()
    with _key_lock:
        if caminho.exists():
            return caminho.read_bytes().strip()
        from cryptography.fernet import Fernet

        chave = Fernet.generate_key()
        atomic_write(caminho, chave.decode(), mode=0o600)
        return chave


def encrypt(texto: str | None) -> str | None:
    if not texto:
        return None
    from cryptography.fernet import Fernet

    return Fernet(load_or_create_key()).encrypt(texto.encode()).decode()


def decrypt(cifra: str | None) -> str | None:
    if not cifra:
        return None
    from cryptography.fernet import Fernet, InvalidToken

    try:
        return Fernet(load_or_create_key()).decrypt(cifra.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def key_info() -> dict[str, Any]:
    """Dados que a tela de saúde mostra sobre a chave."""
    caminho = key_file()
    if not caminho.exists():
        return {"exists": False, "path": str(caminho)}
    st = caminho.stat()
    return {
        "exists": True,
        "path": str(caminho),
        "bytes": st.st_size,
        "mode": oct(st.st_mode & 0o777),
        "created": st.st_mtime,
        "secure": (st.st_mode & 0o077) == 0,
    }


# ----------------------------------------------------------------------------
# Stores
# ----------------------------------------------------------------------------

class JobStore:
    """Os jobs em disco.

    Atenção: `JobStore()` nasce vazio, e o primeiro `put` grava só o que está
    nele, apagando o resto do arquivo. Para acrescentar a algo que já existe,
    use sempre `JobStore.load()`. O construtor vazio serve para recriar o
    arquivo do zero, que é o que o `demo` faz.
    """

    def __init__(self, jobs: dict[str, Job] | None = None) -> None:
        self.jobs: dict[str, Job] = jobs or {}

    @classmethod
    def load(cls) -> "JobStore":
        cru = _read_json(jobs_file())
        jobs = {
            nome: Job.from_dict(nome, dados)
            for nome, dados in (cru.get("jobs") or {}).items()
        }
        return cls(jobs)

    def save(self) -> None:
        payload = {"jobs": {nome: job.to_dict() for nome, job in self.jobs.items()}}
        atomic_write(jobs_file(), json.dumps(payload, indent=2, ensure_ascii=False), mode=0o600)

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.name.lower())

    def get(self, nome: str) -> Job | None:
        return self.jobs.get(nome)

    def put(self, job: Job) -> None:
        self.jobs[job.name] = job
        self.save()

    def delete(self, nome: str) -> bool:
        if nome in self.jobs:
            del self.jobs[nome]
            self.save()
            return True
        return False


class DestinationStore:
    """Os destinos em disco.

    Mesma ressalva do JobStore: `DestinationStore()` começa vazio e o `put`
    reescreve o arquivo inteiro. Para acrescentar, `DestinationStore.load()`.
    """

    def __init__(self, destinations: dict[str, Destination] | None = None) -> None:
        self.destinations: dict[str, Destination] = destinations or {}

    @classmethod
    def load(cls) -> "DestinationStore":
        cru = _read_json(destinations_file())
        itens = {
            nome: Destination.from_dict(nome, dados)
            for nome, dados in (cru.get("destinations") or {}).items()
        }
        return cls(itens)

    def save(self) -> None:
        payload = {
            "destinations": {nome: d.to_dict() for nome, d in self.destinations.items()}
        }
        atomic_write(destinations_file(), json.dumps(payload, indent=2, ensure_ascii=False), mode=0o600)

    def list(self) -> list[Destination]:
        return sorted(self.destinations.values(), key=lambda d: d.name.lower())

    def get(self, nome: str) -> Destination | None:
        return self.destinations.get(nome)

    def put(self, destino: Destination) -> None:
        self.destinations[destino.name] = destino
        self.save()

    def delete(self, nome: str) -> bool:
        if nome in self.destinations:
            del self.destinations[nome]
            self.save()
            return True
        return False


class Settings:
    """Padrão global: canais de aviso e a matriz herdada pelos jobs."""

    def __init__(self, dados: dict[str, Any] | None = None) -> None:
        d = dados or {}
        self.notify_global = NotifyMatrix.from_dict(d.get("notify_global")) if d.get("notify_global") else NotifyMatrix.default_global()
        self.smtp: dict[str, Any] = d.get("smtp") or {}
        self.slack: dict[str, Any] = d.get("slack") or {}
        self.staging_cap_gb: int = int(d.get("staging_cap_gb", 20) or 20)
        self.staging_hold_hours: int = int(d.get("staging_hold_hours", 72) or 72)
        self.history_days: int = int(d.get("history_days", 90) or 90)
        self.locale: str | None = d.get("locale")

    @classmethod
    def load(cls) -> "Settings":
        return cls(_read_json(settings_file()))

    def save(self) -> None:
        payload = {
            "notify_global": self.notify_global.to_dict(),
            "smtp": self.smtp,
            "slack": self.slack,
            "staging_cap_gb": self.staging_cap_gb,
            "staging_hold_hours": self.staging_hold_hours,
            "history_days": self.history_days,
            "locale": self.locale,
        }
        atomic_write(settings_file(), json.dumps(payload, indent=2, ensure_ascii=False), mode=0o600)

    def channel_configured(self, canal: str) -> bool:
        if canal == "email":
            return bool(self.smtp.get("host") and self.smtp.get("to"))
        if canal == "slack":
            return bool(self.slack.get("webhook_enc") or self.slack.get("token_enc"))
        return False

    def email_summary(self) -> str:
        if not self.channel_configured("email"):
            return ""
        return f"{self.smtp.get('to')} via {self.smtp.get('host')}"

    def slack_summary(self) -> str:
        if not self.channel_configured("slack"):
            return ""
        return str(self.slack.get("channel") or "")
