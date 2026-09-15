"""Diagnóstico do sistema: tick, worker, chave, destinos e espaço.

Cada item responde três coisas, na ordem em que a tela mostra: em que estado
está, por que isso importa, e o comando exato que conserta. Item sem conserto
conhecido não vira item.

O modo de falha mais provável deste sistema não é dar erro: é não rodar em
silêncio porque o tick nunca foi instalado. Por isso o tick é o primeiro item e
o único que também ocupa a barra de estado inteira quando falta.
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import APP_SLUG
from .config import config_dir, data_dir, key_info, staging_dir
from .i18n import t

CRON_MARK_START = f"# >>> {APP_SLUG}"
CRON_MARK_END = f"# <<< {APP_SLUG}"
SUPERVISOR_PROGRAM = f"{APP_SLUG}-worker"


class Level(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class HealthItem:
    key: str
    title: str
    level: Level
    detail: str = ""
    why: str = ""
    fix_command: str = ""
    fix_key: str = ""
    extra: list[str] = field(default_factory=list)
    progress: float | None = None


# ----------------------------------------------------------------------------
# tick no crontab
# ----------------------------------------------------------------------------

def read_crontab() -> str:
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def tick_installed() -> bool:
    return CRON_MARK_START in read_crontab()


def tick_line() -> str:
    binario = shutil.which(APP_SLUG) or str(Path.home() / ".local" / "bin" / APP_SLUG)
    return f"* * * * * {binario} tick"


def install_tick() -> tuple[bool, str]:
    """Escreve a linha no crontab do usuário, entre marcadores.

    Não precisa de sudo: é o crontab do próprio usuário. Os marcadores existem
    para o `--uninstall` conseguir remover exatamente o que foi posto, sem
    tocar no resto do arquivo.
    """
    atual = read_crontab()
    if CRON_MARK_START in atual:
        return True, "já instalado"
    bloco = f"{CRON_MARK_START}\n{tick_line()}\n{CRON_MARK_END}\n"
    novo = (atual.rstrip("\n") + "\n\n" if atual.strip() else "") + bloco
    try:
        proc = subprocess.run(["crontab", "-"], input=novo, text=True, capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or f"crontab saiu com {proc.returncode}"
    return True, "instalado"


def uninstall_tick() -> tuple[bool, str]:
    atual = read_crontab()
    if CRON_MARK_START not in atual:
        return True, "não estava instalado"
    linhas = atual.splitlines()
    saida, dentro = [], False
    for linha in linhas:
        if linha.strip() == CRON_MARK_START:
            dentro = True
            continue
        if linha.strip() == CRON_MARK_END:
            dentro = False
            continue
        if not dentro:
            saida.append(linha)
    novo = "\n".join(saida).rstrip("\n") + "\n"
    proc = subprocess.run(["crontab", "-"], input=novo, text=True, capture_output=True, timeout=5)
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return True, "removido"


# ----------------------------------------------------------------------------
# worker no supervisord
# ----------------------------------------------------------------------------

@dataclass
class WorkerStatus:
    running: bool = False
    pid: int | None = None
    uptime: str = ""
    known: bool = False   # o supervisord conhece o programa
    message: str = ""


def worker_status() -> WorkerStatus:
    if not shutil.which("supervisorctl"):
        return WorkerStatus(message="supervisorctl não está no PATH")
    try:
        proc = subprocess.run(
            ["supervisorctl", "status", SUPERVISOR_PROGRAM],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return WorkerStatus(message="supervisorctl não respondeu")
    saida = (proc.stdout or proc.stderr).strip()
    if "no such process" in saida.lower() or not saida:
        return WorkerStatus(known=False, message="programa não cadastrado no supervisord")
    partes = saida.split()
    estado = partes[1] if len(partes) > 1 else ""
    status = WorkerStatus(known=True, running=estado == "RUNNING", message=saida)
    if "pid" in saida:
        try:
            pedaco = saida.split("pid", 1)[1].strip()
            status.pid = int(pedaco.split(",")[0])
            if "uptime" in pedaco:
                status.uptime = pedaco.split("uptime", 1)[1].strip()
        except (ValueError, IndexError):
            pass
    return status


def supervisor_conf() -> str:
    binario = shutil.which(APP_SLUG) or str(Path.home() / ".local" / "bin" / APP_SLUG)
    return (
        f"[program:{SUPERVISOR_PROGRAM}]\n"
        f"command={binario} worker\n"
        f"user={Path.home().name}\n"
        f'environment=HOME="{Path.home()}"\n'
        "autostart=true\n"
        "autorestart=true\n"
        "startsecs=5\n"
        f"stdout_logfile={data_dir()}/worker.log\n"
        f"stderr_logfile={data_dir()}/worker.err.log\n"
    )


# ----------------------------------------------------------------------------
# staging
# ----------------------------------------------------------------------------

@dataclass
class StagingInfo:
    total: int
    used: int
    free: int
    pct: float
    artifacts: int


def staging_info() -> StagingInfo:
    caminho = staging_dir()
    uso = shutil.disk_usage(str(caminho))
    ocupado = 0
    artefatos = 0
    for item in caminho.rglob("*"):
        if item.is_file():
            ocupado += item.stat().st_size
            artefatos += 1
    return StagingInfo(
        total=uso.total,
        used=ocupado,
        free=uso.free,
        pct=(uso.used / uso.total * 100) if uso.total else 0.0,
        artifacts=artefatos,
    )


# ----------------------------------------------------------------------------
# Coleta
# ----------------------------------------------------------------------------

def collect(destinations: list | None = None, next_job: tuple[str, int] | None = None) -> list[HealthItem]:
    from .format import format_bytes, format_duration

    itens: list[HealthItem] = []

    instalado = tick_installed()
    itens.append(
        HealthItem(
            key="tick",
            title=t("health.tick"),
            level=Level.OK if instalado else Level.FAIL,
            detail=t("status.tick_installed") if instalado else t("status.tick_missing"),
            why="" if instalado else t("health.tick_bad"),
            fix_command="" if instalado else f"{APP_SLUG} tick --install",
            fix_key="i",
        )
    )

    worker = worker_status()
    if worker.running:
        nivel, detalhe = Level.OK, t("health.worker_ok", pid=worker.pid or 0, t=worker.uptime or "?")
    elif worker.known:
        nivel, detalhe = Level.FAIL, worker.message
    else:
        nivel, detalhe = Level.WARN, worker.message
    itens.append(
        HealthItem(
            key="worker",
            title=t("health.worker"),
            level=nivel,
            detail=detalhe,
            why="" if worker.running else t("health.worker_conf"),
            fix_command="" if worker.running else f"sudo supervisorctl reread && sudo supervisorctl update",
            extra=[t("health.worker_conf")] if worker.running else [],
        )
    )

    chave = key_info()
    if chave["exists"]:
        criada = dt.datetime.fromtimestamp(chave["created"]).strftime("%d/%m")
        nivel = Level.OK if chave["secure"] else Level.WARN
        itens.append(
            HealthItem(
                key="key",
                title=t("health.key"),
                level=nivel,
                detail=t("health.key_ok", caminho=chave["path"], n=chave["bytes"]),
                why="" if chave["secure"] else "o arquivo está legível por outros usuários",
                fix_command="" if chave["secure"] else f"chmod 600 {chave['path']}",
                extra=[t("health.key_used", data=criada, n=len(destinations or []))],
            )
        )
    else:
        itens.append(
            HealthItem(
                key="key",
                title=t("health.key"),
                level=Level.WARN,
                detail="ainda não existe, nasce no primeiro segredo salvo",
            )
        )

    for destino in destinations or []:
        itens.append(
            HealthItem(
                key=f"dest:{destino.name}",
                title=t("health.dest", nome=destino.name),
                level=Level.OK if destino.enabled else Level.WARN,
                detail=destino.location() if destino.enabled else t("dest.disabled"),
            )
        )

    staging = staging_info()
    pct = staging.pct
    nivel = Level.OK if pct < 80 else (Level.WARN if pct < 92 else Level.FAIL)
    item = HealthItem(
        key="staging",
        title=t("health.staging"),
        level=nivel,
        detail=t("health.staging_line", livre=format_bytes(staging.free), total=format_bytes(staging.total)),
        progress=pct / 100,
        extra=[t("health.staging_pct", pct=int(pct))],
    )
    if nivel is not Level.OK:
        item.fix_command = f"{APP_SLUG} staging --prune"
    itens.append(item)

    return itens


def summary(itens: list[HealthItem]) -> tuple[int, int, int]:
    ok = sum(1 for i in itens if i.level is Level.OK)
    warn = sum(1 for i in itens if i.level is Level.WARN)
    fail = sum(1 for i in itens if i.level is Level.FAIL)
    return ok, warn, fail
