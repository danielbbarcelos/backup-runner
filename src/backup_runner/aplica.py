"""Configuração declarativa: `backup-runner apply servidor.yaml`.

O menu é bom para mexer num job; para montar um servidor do zero, ou deixar
dois servidores iguais, um arquivo versionado é melhor. Este módulo lê o
arquivo e cria ou atualiza destinos, jobs, Slack e staging para ficarem como
ele descreve. Rodar duas vezes dá o mesmo resultado, e o histórico dos jobs é
preservado (ele é ligado ao nome do job).

Segredo nunca fica no arquivo. Onde vai uma senha ou chave, o arquivo aponta a
origem e o valor é lido na hora:

    {env: NOME}                         variável de ambiente
    {dotenv: /caminho/.env, key: NOME}  linha NOME= de um .env (o da aplicação)
    {file: /caminho}                    conteúdo de um arquivo

O bloco `s3:` é a convenção da DBMV: com `project` e `region` ele cria
`s3-<project>` (prefixo mysql/, para os dumps) e `s3-<project>-sync` (prefixo
sync/, Standard-IA, para o espelho de arquivos), e cada job aponta para o
destino certo sem precisar dizer.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import relogio
from .config import DestinationStore, JobStore, Settings, decrypt, encrypt
from .models import (
    DEFAULT_IGNORE_REGEX, Destination, DestKind, ExcludePattern, FilesMode, FilesSource, Job,
    JobDestination, MySQLSource, SourceKind,
)
from .schedule import is_valid


class ApplyError(ValueError):
    """O arquivo não pode ser aplicado; a mensagem diz onde e por quê."""


@dataclass
class Plano:
    """O que mudaria. `aplica` executa; `--dry-run` só mostra."""
    linhas: list[tuple[str, str, str]] = field(default_factory=list)  # (ação, tipo, nome)
    settings: Settings | None = None
    destinos: dict[str, Destination] = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)
    remover_jobs: list[str] = field(default_factory=list)

    @property
    def mudancas(self) -> int:
        return sum(1 for acao, _, _ in self.linhas if acao != "igual")


# ----------------------------------------------------------------------------
# Segredos
# ----------------------------------------------------------------------------

def segredo(valor: Any, onde: str) -> str:
    """Resolve {env}, {dotenv} ou {file}. Texto puro é aceito, mas desaconselhado."""
    if isinstance(valor, str):
        return valor
    if not isinstance(valor, dict) or len({"env", "dotenv", "file"} & set(valor)) != 1:
        raise ApplyError(f"{onde}: use {{env: NOME}}, {{dotenv: caminho, key: NOME}} ou {{file: caminho}}")
    if "env" in valor:
        nome = valor["env"]
        if nome not in os.environ or not os.environ[nome]:
            raise ApplyError(f"{onde}: a variável de ambiente {nome} não está definida")
        return os.environ[nome]
    if "file" in valor:
        caminho = Path(valor["file"]).expanduser()
        if not caminho.is_file():
            raise ApplyError(f"{onde}: o arquivo {caminho} não existe")
        return caminho.read_text().strip()
    caminho, chave = Path(valor["dotenv"]).expanduser(), valor.get("key")
    if not chave:
        raise ApplyError(f"{onde}: dotenv precisa de key")
    if not caminho.is_file():
        raise ApplyError(f"{onde}: o arquivo {caminho} não existe")
    casou = re.search(rf"^\s*{re.escape(chave)}\s*=\s*(.*?)\s*$", caminho.read_text(), re.M)
    if not casou:
        raise ApplyError(f"{onde}: {chave} não está em {caminho}")
    texto = casou.group(1)
    if len(texto) >= 2 and texto[0] == texto[-1] and texto[0] in "\"'":
        texto = texto[1:-1]
    return texto


# ----------------------------------------------------------------------------
# Montagem
# ----------------------------------------------------------------------------

def _destinos_s3(cfg: dict) -> dict[str, Destination]:
    projeto = cfg.get("project")
    regiao = cfg.get("region")
    if not projeto or not regiao:
        raise ApplyError("s3: project e region são obrigatórios")
    base = dict(
        kind=DestKind.S3,
        endpoint=cfg.get("endpoint") or f"s3.{regiao}.amazonaws.com",
        region=regiao,
        bucket=cfg.get("bucket") or f"backup-runner-{projeto}",
        access_key=segredo(cfg.get("access_key"), "s3.access_key"),
        secret_enc=encrypt(segredo(cfg.get("secret_key"), "s3.secret_key")),
        retention_days=int(cfg.get("retention_days", 30)),
    )
    return {
        f"s3-{projeto}": Destination(
            name=f"s3-{projeto}", prefix="mysql",
            storage_class=cfg.get("mysql_storage_class", ""), **base,
        ),
        f"s3-{projeto}-sync": Destination(
            name=f"s3-{projeto}-sync", prefix="sync",
            storage_class=cfg.get("sync_storage_class", "STANDARD_IA"), **base,
        ),
    }


def _job(nome: str, cfg: dict, projeto: str | None, existente: Job | None) -> Job:
    onde = f"jobs.{nome}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", nome):
        raise ApplyError(f"{onde}: nome só com letras minúsculas, números e hífen")
    tipos = [t for t in ("mysql", "sync", "archive") if t in cfg]
    if len(tipos) != 1:
        raise ApplyError(f"{onde}: precisa de exatamente um entre mysql, sync e archive")
    tipo = tipos[0]
    agenda = str(cfg.get("schedule", ""))
    if not is_valid(agenda):
        raise ApplyError(f"{onde}.schedule: cron inválido: {agenda!r}")

    if tipo == "mysql":
        m = cfg["mysql"]
        if not m.get("database"):
            raise ApplyError(f"{onde}.mysql.database é obrigatório")
        fonte: Any = MySQLSource(
            host=m.get("host", "localhost"), port=int(m.get("port", 3306)), user=m.get("user", "root"),
            password_enc=encrypt(segredo(m.get("password"), f"{onde}.mysql.password")),
            database=m["database"], ignore_regex=m.get("ignore_regex", DEFAULT_IGNORE_REGEX),
            ignore_manual=list(m.get("ignore", [])), keep_manual=list(m.get("keep", [])),
        )
        destino_padrao = f"s3-{projeto}" if projeto else None
    else:
        f = cfg[tipo] if isinstance(cfg[tipo], dict) else {"path": cfg[tipo]}
        if not f.get("path"):
            raise ApplyError(f"{onde}.{tipo}.path é obrigatório")
        fonte = FilesSource(
            path=f["path"], excludes=[ExcludePattern(p, True) for p in f.get("exclude", [])],
            follow_links=bool(f.get("follow_links", False)),
            mode=FilesMode.SYNC if tipo == "sync" else FilesMode.ARCHIVE,
            max_delete_pct=int(f.get("max_delete_pct", 5)),
        )
        destino_padrao = (f"s3-{projeto}-sync" if tipo == "sync" else f"s3-{projeto}") if projeto else None

    destinos = cfg.get("destinations") or ([destino_padrao] if destino_padrao else [])
    if not destinos:
        raise ApplyError(f"{onde}: sem destino (declare s3: no topo ou destinations no job)")

    job = Job(
        name=nome, source=fonte, schedule=agenda,
        timezone=cfg.get("timezone", "America/Sao_Paulo"),
        enabled=bool(cfg.get("enabled", True)),
        timeout_minutes=int(cfg.get("timeout_minutes", 240)),
        destinations=[JobDestination(d) for d in destinos],
        created=existente.created if existente else relogio.agora().date().isoformat(),
    )
    if existente is not None:
        job.notify = existente.notify          # avisos por job são decididos no menu
        job.stale_after_hours = existente.stale_after_hours
        job.catch_up_window_minutes = existente.catch_up_window_minutes
        if not job.enabled and existente.paused_at:
            job.paused_at = existente.paused_at
    return job


def _sem_segredo(d: Any) -> Any:
    """O dicionário sem os campos *_enc, em qualquer nível.

    A criptografia gera um texto diferente a cada vez, então comparar o cifrado
    acusaria mudança em todo apply.
    """
    if isinstance(d, dict):
        return {k: _sem_segredo(v) for k, v in d.items() if not k.endswith("_enc")}
    if isinstance(d, list):
        return [_sem_segredo(v) for v in d]
    return d


def _segredos(d: Any, prefixo: str = "") -> dict[str, str | None]:
    if isinstance(d, dict):
        saida: dict[str, str | None] = {}
        for k, v in d.items():
            if k.endswith("_enc"):
                saida[prefixo + k] = decrypt(v)
            else:
                saida.update(_segredos(v, f"{prefixo}{k}."))
        return saida
    return {}


def _mesmo_segredo(a: dict, b: dict) -> bool:
    return _segredos(a) == _segredos(b)


def _compara(antes: dict | None, depois: dict) -> str:
    if antes is None:
        return "criar"
    if _sem_segredo(antes) != _sem_segredo(depois):
        return "alterar"
    if not _mesmo_segredo(antes, depois):
        return "alterar segredo"
    return "igual"


def planeja(cfg: dict, *, prune: bool = False) -> Plano:
    if not isinstance(cfg, dict):
        raise ApplyError("o arquivo precisa ser um mapa YAML")
    desconhecidas = set(cfg) - {"timezone", "staging_path", "slack", "s3", "jobs"}
    if desconhecidas:
        raise ApplyError(f"chaves desconhecidas no topo: {', '.join(sorted(desconhecidas))}")

    plano = Plano()
    s = Settings.load()
    antes_s = {"timezone": s.timezone, "staging_path": s.staging_path, "slack": decrypt(s.slack.get("webhook_enc"))}
    if "timezone" in cfg:
        s.timezone = cfg["timezone"]
    if "staging_path" in cfg:
        s.staging_path = cfg["staging_path"] or ""
    webhook = None
    if cfg.get("slack", {}).get("webhook") is not None:
        webhook = segredo(cfg["slack"]["webhook"], "slack.webhook")
        if not webhook.startswith("https://hooks.slack.com/"):
            raise ApplyError("slack.webhook: não parece um incoming webhook do Slack")
        s.slack = {"webhook_enc": encrypt(webhook), "channel": cfg["slack"].get("channel", "webhook")}
    depois_s = {"timezone": s.timezone, "staging_path": s.staging_path, "slack": webhook or antes_s["slack"]}
    for chave in ("timezone", "staging_path", "slack"):
        acao = "igual" if antes_s[chave] == depois_s[chave] else ("criar" if not antes_s[chave] else "alterar")
        plano.linhas.append((acao, "config", chave))
    plano.settings = s

    atuais_d = DestinationStore.load()
    projeto = None
    if cfg.get("s3"):
        projeto = cfg["s3"].get("project")
        for nome, d in _destinos_s3(cfg["s3"]).items():
            atual = atuais_d.get(nome)
            plano.linhas.append((_compara(atual.to_dict() if atual else None, d.to_dict()), "destino", nome))
            plano.destinos[nome] = d

    atuais_j = JobStore.load()
    conhecidos = set(plano.destinos) | {d.name for d in atuais_d.list()}
    for nome, jcfg in (cfg.get("jobs") or {}).items():
        atual = atuais_j.get(nome)
        job = _job(nome, jcfg or {}, projeto, atual)
        for ligacao in job.destinations:
            if ligacao.name not in conhecidos:
                raise ApplyError(f"jobs.{nome}: destino {ligacao.name} não existe")
        dest = plano.destinos.get(job.destinations[0].name) or atuais_d.get(job.destinations[0].name)
        if job.kind is SourceKind.FILES and job.source.mode is FilesMode.SYNC and dest.kind is not DestKind.S3:
            raise ApplyError(f"jobs.{nome}: modo sync só com destino S3")
        plano.linhas.append((_compara(atual.to_dict() if atual else None, job.to_dict()), "job", nome))
        plano.jobs[nome] = job

    if prune:
        for job in atuais_j.list():
            if job.name not in plano.jobs:
                plano.linhas.append(("remover", "job", job.name))
                plano.remover_jobs.append(job.name)
    return plano


def aplica(plano: Plano) -> None:
    if plano.settings is not None:
        plano.settings.save()
    destinos = DestinationStore.load()
    for d in plano.destinos.values():
        destinos.put(d)
    jobs = JobStore.load()
    for job in plano.jobs.values():
        jobs.put(job)
    for nome in plano.remover_jobs:
        jobs.delete(nome)


def carrega(caminho: Path) -> dict:
    import yaml

    try:
        return yaml.safe_load(caminho.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ApplyError(f"{caminho}: YAML inválido: {exc}") from exc
