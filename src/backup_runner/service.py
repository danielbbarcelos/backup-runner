"""Como manter o worker de pé: systemd de usuário ou supervisord.

Os dois fazem a mesma coisa, e a escolha não é indiferente.

O **systemd de usuário** não precisa de sudo, sobe junto com a sessão, e é
isolado: um serviço quebrado de outro projeto não impede o seu de subir. Com
`loginctl enable-linger` ele sobe no boot mesmo sem ninguém logado.

O **supervisord** costuma já existir na máquina, mas é compartilhado. Um único
`.conf` inválido em `/etc/supervisor/conf.d/` derruba o daemon inteiro, e junto
todos os programas dele. Isso não é hipótese: foi assim que um conf apontando
para um diretório apagado deixou este backup sem worker.

Por isso o systemd de usuário é o padrão sugerido, e o supervisord fica
disponível para quem já organiza tudo por ele.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import APP_SLUG
from .config import data_dir

UNIDADE = f"{APP_SLUG}.service"
PROGRAMA = f"{APP_SLUG}-worker"


@dataclass
class Estado:
    gerenciador: str = ""      # systemd | supervisord | ""
    rodando: bool = False
    pid: int | None = None
    desde: str = ""
    instalado: bool = False    # a unidade ou o programa existe
    mensagem: str = ""
    conserto: str = ""


def binario() -> str:
    return shutil.which(APP_SLUG) or str(Path.home() / ".local" / "bin" / APP_SLUG)


# ----------------------------------------------------------------------------
# systemd de usuário
# ----------------------------------------------------------------------------

def systemd_disponivel() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # "running" e "degraded" servem; o que não serve é não haver sessão.
    return proc.returncode == 0 or "degraded" in proc.stdout


def unidade_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / UNIDADE


def unidade_conteudo() -> str:
    return (
        "[Unit]\n"
        "Description=backup-runner worker\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={binario()} worker\n"
        "Restart=always\n"
        "RestartSec=10\n"
        # O worker faz I/O pesado; ficar atrás de quem usa a máquina é o certo.
        "Nice=10\n"
        "IOSchedulingClass=idle\n"
        f"StandardOutput=append:{data_dir()}/worker.log\n"
        f"StandardError=append:{data_dir()}/worker.err.log\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemd_status() -> Estado:
    if not systemd_disponivel():
        return Estado(gerenciador="systemd", mensagem="systemd de usuário não disponível")
    if not unidade_path().exists():
        return Estado(gerenciador="systemd", mensagem="unidade não instalada")

    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", UNIDADE,
             "--property=ActiveState,MainPID,ExecMainStartTimestamp"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Estado(gerenciador="systemd", instalado=True, mensagem="systemctl não respondeu")

    dados = dict(
        linha.split("=", 1) for linha in proc.stdout.splitlines() if "=" in linha
    )
    ativo = dados.get("ActiveState", "") == "active"
    pid = int(dados.get("MainPID") or 0) or None
    return Estado(
        gerenciador="systemd",
        instalado=True,
        rodando=ativo,
        pid=pid if ativo else None,
        desde=dados.get("ExecMainStartTimestamp", ""),
        mensagem="rodando" if ativo else dados.get("ActiveState", "parado"),
        conserto="" if ativo else f"systemctl --user restart {UNIDADE}",
    )


def systemd_instala() -> tuple[bool, list[str]]:
    """Escreve a unidade e sobe o serviço. Sem sudo em nenhum passo."""
    passos: list[str] = []
    caminho = unidade_path()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(unidade_conteudo())
    passos.append(f"unidade escrita em {caminho}")

    for args, descricao in (
        (["systemctl", "--user", "daemon-reload"], "systemd recarregado"),
        (["systemctl", "--user", "enable", "--now", UNIDADE], "serviço habilitado e iniciado"),
    ):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, passos + [f"falhou: {exc}"]
        if proc.returncode != 0:
            return False, passos + [(proc.stderr or proc.stdout).strip()]
        passos.append(descricao)

    # Sem linger, o serviço morre quando a sessão gráfica fecha.
    if not _linger_ligado():
        try:
            subprocess.run(
                ["loginctl", "enable-linger", os.environ.get("USER", "")],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if _linger_ligado():
            passos.append("linger ligado: o worker sobe no boot mesmo sem login")
        else:
            passos.append(
                "! sem linger, o worker só roda com a sessão aberta. "
                f"para ligar: sudo loginctl enable-linger {os.environ.get('USER', '')}"
            )
    else:
        passos.append("linger já estava ligado")
    return True, passos


def _linger_ligado() -> bool:
    try:
        proc = subprocess.run(
            ["loginctl", "show-user", os.environ.get("USER", "")],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "Linger=yes" in proc.stdout


def systemd_remove() -> tuple[bool, str]:
    caminho = unidade_path()
    if not caminho.exists():
        return True, "não estava instalada"
    for args in (
        ["systemctl", "--user", "disable", "--now", UNIDADE],
        ["systemctl", "--user", "daemon-reload"],
    ):
        subprocess.run(args, capture_output=True, text=True, timeout=20)
    caminho.unlink(missing_ok=True)
    return True, "unidade removida"


# ----------------------------------------------------------------------------
# supervisord
# ----------------------------------------------------------------------------

def supervisor_conf() -> str:
    return (
        f"[program:{PROGRAMA}]\n"
        f"command={binario()} worker\n"
        f"user={Path.home().name}\n"
        f'environment=HOME="{Path.home()}"\n'
        "autostart=true\n"
        "autorestart=true\n"
        "startsecs=5\n"
        f"stdout_logfile={data_dir()}/worker.log\n"
        f"stderr_logfile={data_dir()}/worker.err.log\n"
    )


def supervisor_status() -> Estado:
    if not shutil.which("supervisorctl"):
        return Estado(gerenciador="supervisord", mensagem="supervisorctl não está no PATH")
    try:
        proc = subprocess.run(
            ["supervisorctl", "status", PROGRAMA],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Estado(gerenciador="supervisord", mensagem="supervisorctl não respondeu")

    saida = (proc.stdout or proc.stderr).strip()
    baixo = saida.lower()

    # O caso que mais confunde: o daemon não está de pé, então o socket não
    # existe. A mensagem crua fala de um arquivo, e quem lê procura o arquivo.
    if "no such file" in baixo or "refused connection" in baixo or "unix://" in baixo:
        return Estado(
            gerenciador="supervisord",
            mensagem="o supervisord não está rodando",
            conserto=(
                "sudo systemctl status supervisor   # veja por que ele não sobe\n"
                "    um único .conf inválido em /etc/supervisor/conf.d/ derruba o daemon inteiro"
            ),
        )
    if "no such process" in baixo or not saida:
        return Estado(gerenciador="supervisord", mensagem="programa não cadastrado")

    partes = saida.split()
    estado = partes[1] if len(partes) > 1 else ""
    resultado = Estado(
        gerenciador="supervisord", instalado=True,
        rodando=estado == "RUNNING", mensagem=saida,
    )
    if "pid" in saida:
        try:
            pedaco = saida.split("pid", 1)[1].strip()
            resultado.pid = int(pedaco.split(",")[0])
            if "uptime" in pedaco:
                resultado.desde = pedaco.split("uptime", 1)[1].strip()
        except (ValueError, IndexError):
            pass
    if not resultado.rodando:
        resultado.conserto = f"sudo supervisorctl restart {PROGRAMA}"
    return resultado


# ----------------------------------------------------------------------------
# Visão unificada
# ----------------------------------------------------------------------------

def status() -> Estado:
    """O worker está de pé, por qualquer um dos dois caminhos?

    Procura primeiro onde ele foi instalado. Se os dois estiverem configurados,
    o que está rodando ganha, porque é o que de fato importa para o backup.
    """
    pelo_systemd = systemd_status()
    if pelo_systemd.rodando:
        return pelo_systemd
    pelo_supervisor = supervisor_status()
    if pelo_supervisor.rodando:
        return pelo_supervisor
    # Nenhum rodando: reporta o que pelo menos está instalado.
    if pelo_systemd.instalado:
        return pelo_systemd
    if pelo_supervisor.instalado:
        return pelo_supervisor
    # Nem instalado: o conselho depende do que a máquina oferece.
    if systemd_disponivel():
        return Estado(
            gerenciador="systemd",
            mensagem="o worker não está instalado",
            conserto=f"{APP_SLUG} install",
        )
    return pelo_supervisor or Estado(mensagem="o worker não está instalado")


def pid_vivo(pid: int | None) -> bool:
    """Existe um processo com este pid?

    `os.kill(pid, 0)` não mata nada: pergunta ao núcleo se dá para sinalizar.
    Permissão negada também é resposta afirmativa, o processo existe e é de
    outro dono. É assim que se distingue um backup demorado de um worker que
    morreu no meio e deixou a linha "rodando" para sempre.
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
