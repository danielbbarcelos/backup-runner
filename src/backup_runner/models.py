"""Entidades: job, fonte, destino, matriz de avisos, execução.

Serialização em dicionário puro, para o store JSON e para o SQLite. Nada aqui
importa Textual nem toca disco: o modelo precisa servir igual à TUI, ao tick e
ao worker.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


# ----------------------------------------------------------------------------
# Enumerações
# ----------------------------------------------------------------------------

class SourceKind(str, Enum):
    MYSQL = "mysql"
    FILES = "files"


class DestKind(str, Enum):
    LOCAL = "local"
    S3 = "s3"
    SFTP = "sftp"


class ArchiveFormat(str, Enum):
    TARGZ = "tar.gz"
    ZIP = "zip"


class RunResult(str, Enum):
    OK = "ok"
    FAILED = "failed"
    LATE = "late"                 # rodou, mas fora da janela original
    PENDING_UPLOAD = "pending"    # artefato pronto, falta destino
    MISSED = "missed"             # janela perdida, não rodou
    RUNNING = "running"
    QUEUED = "queued"
    SKIPPED = "skipped"           # job pausado quando a janela chegou


class Stage(str, Enum):
    DUMP = "dump"
    ARCHIVE = "archive"
    COMPRESS = "compress"
    UPLOAD = "upload"
    RETENTION = "retention"


class StageState(str, Enum):
    DONE = "done"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"
    SKIPPED = "skipped"


class NotifyEvent(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    RECOVERED = "recovered"
    MISSED = "missed"
    STALE = "stale"


class Channel(str, Enum):
    EMAIL = "email"
    SLACK = "slack"


# A ordem da grade na tela de notificações.
NOTIFY_EVENTS = [
    NotifyEvent.SUCCESS,
    NotifyEvent.FAILURE,
    NotifyEvent.RECOVERED,
    NotifyEvent.MISSED,
    NotifyEvent.STALE,
]
CHANNELS = [Channel.EMAIL, Channel.SLACK]


# Padrão herdado do mysql-dumper: tabelas que terminam em _YYYYMM, com ou sem
# sufixo de versão. Reavaliado a cada execução, então tabela mensal nova já
# nasce ignorada.
DEFAULT_IGNORE_REGEX = r"_[0-9]{6}$|_(19|20)[0-9]{2}(0[1-9]|1[0-2])(_v[0-9]+)?$"


# ----------------------------------------------------------------------------
# Fontes
# ----------------------------------------------------------------------------

@dataclass
class MySQLSource:
    host: str = ""
    port: int = 3306
    user: str = ""
    password_enc: str | None = None
    database: str = ""
    ignore_regex: str = DEFAULT_IGNORE_REGEX
    # A marca à mão vence a regex nos dois sentidos: `ignore_manual` acrescenta
    # e `keep_manual` resgata uma tabela que a regex pegaria.
    ignore_manual: list[str] = field(default_factory=list)
    keep_manual: list[str] = field(default_factory=list)

    kind = SourceKind.MYSQL

    def target(self) -> str:
        return f"{self.database} @ {self.host}:{self.port}"

    def resolve_ignored(self, tables: Iterable[str]) -> tuple[list[str], list[str]]:
        """Devolve (ignoradas_por_regex, ignoradas_à_mão) para as tabelas dadas.

        A regex roda contra a lista do dia, não contra a do cadastro. Uma
        tabela resgatada à mão sai do resultado mesmo que a regex a pegue.
        """
        try:
            rx = re.compile(self.ignore_regex) if self.ignore_regex else None
        except re.error:
            rx = None
        resgatadas = set(self.keep_manual)
        mao = set(self.ignore_manual)
        por_regex = []
        por_mao = []
        for nome in tables:
            if nome in mao:
                por_mao.append(nome)
            elif rx is not None and rx.search(nome) and nome not in resgatadas:
                por_regex.append(nome)
        return sorted(por_regex), sorted(por_mao)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": SourceKind.MYSQL.value,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password_enc": self.password_enc,
            "database": self.database,
            "ignore_regex": self.ignore_regex,
            "ignore_manual": list(self.ignore_manual),
            "keep_manual": list(self.keep_manual),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MySQLSource":
        return cls(
            host=d.get("host", ""),
            port=int(d.get("port", 3306) or 3306),
            user=d.get("user", ""),
            password_enc=d.get("password_enc"),
            database=d.get("database", ""),
            ignore_regex=d.get("ignore_regex", DEFAULT_IGNORE_REGEX),
            ignore_manual=list(d.get("ignore_manual") or []),
            keep_manual=list(d.get("keep_manual") or []),
        )


@dataclass
class ExcludePattern:
    pattern: str
    enabled: bool = True


@dataclass
class FilesSource:
    path: str = ""
    excludes: list[ExcludePattern] = field(default_factory=list)
    follow_links: bool = False
    archive_format: ArchiveFormat = ArchiveFormat.TARGZ

    kind = SourceKind.FILES

    def target(self) -> str:
        return self.path

    def active_excludes(self) -> list[str]:
        return [e.pattern for e in self.excludes if e.enabled]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": SourceKind.FILES.value,
            "path": self.path,
            "excludes": [{"pattern": e.pattern, "enabled": e.enabled} for e in self.excludes],
            "follow_links": self.follow_links,
            "format": self.archive_format.value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FilesSource":
        return cls(
            path=d.get("path", ""),
            excludes=[
                ExcludePattern(e.get("pattern", ""), bool(e.get("enabled", True)))
                for e in (d.get("excludes") or [])
            ],
            follow_links=bool(d.get("follow_links", False)),
            archive_format=ArchiveFormat(d.get("format", ArchiveFormat.TARGZ.value)),
        )


Source = MySQLSource | FilesSource


def source_from_dict(d: dict[str, Any]) -> Source:
    if d.get("kind") == SourceKind.FILES.value:
        return FilesSource.from_dict(d)
    return MySQLSource.from_dict(d)


# ----------------------------------------------------------------------------
# Destinos
# ----------------------------------------------------------------------------

@dataclass
class Destination:
    name: str
    kind: DestKind
    enabled: bool = True
    # Retenção contada no próprio destino, por idade, a partir do nome da pasta
    # da execução. Nunca por mtime: copiar arquivo mexe no mtime.
    retention_days: int = 30
    # local
    path: str = ""
    create_missing: bool = True
    mode: str = "0700"
    # s3
    endpoint: str = ""
    region: str = ""
    bucket: str = ""
    prefix: str = ""
    access_key: str = ""
    secret_enc: str | None = None
    # sftp
    host: str = ""
    port: int = 22
    user: str = ""
    auth: str = "key"  # key | password
    private_key: str = ""
    password_enc: str | None = None
    remote_path: str = ""

    def summary(self) -> str:
        if self.kind is DestKind.LOCAL:
            return self.path
        if self.kind is DestKind.S3:
            return f"{self.region}/{self.bucket}" + (f"/{self.prefix.strip('/')}" if self.prefix else "")
        return f"{self.user}@{self.host}"

    def location(self) -> str:
        """Endereço completo, como aparece no detalhe do job."""
        if self.kind is DestKind.LOCAL:
            return self.path
        if self.kind is DestKind.S3:
            return f"{self.region}/{self.bucket}/{self.prefix.strip('/')}".rstrip("/")
        return f"{self.user}@{self.host}:{self.remote_path}"

    def to_dict(self) -> dict[str, Any]:
        d = {
            "kind": self.kind.value,
            "enabled": self.enabled,
            "retention_days": self.retention_days,
        }
        if self.kind is DestKind.LOCAL:
            d.update(path=self.path, create_missing=self.create_missing, mode=self.mode)
        elif self.kind is DestKind.S3:
            d.update(
                endpoint=self.endpoint, region=self.region, bucket=self.bucket,
                prefix=self.prefix, access_key=self.access_key, secret_enc=self.secret_enc,
            )
        else:
            d.update(
                host=self.host, port=self.port, user=self.user, auth=self.auth,
                private_key=self.private_key, password_enc=self.password_enc,
                remote_path=self.remote_path,
            )
        return d

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "Destination":
        return cls(
            name=name,
            kind=DestKind(d.get("kind", DestKind.LOCAL.value)),
            enabled=bool(d.get("enabled", True)),
            retention_days=int(d.get("retention_days", 30) or 30),
            path=d.get("path", ""),
            create_missing=bool(d.get("create_missing", True)),
            mode=d.get("mode", "0700"),
            endpoint=d.get("endpoint", ""),
            region=d.get("region", ""),
            bucket=d.get("bucket", ""),
            prefix=d.get("prefix", ""),
            access_key=d.get("access_key", ""),
            secret_enc=d.get("secret_enc"),
            host=d.get("host", ""),
            port=int(d.get("port", 22) or 22),
            user=d.get("user", ""),
            auth=d.get("auth", "key"),
            private_key=d.get("private_key", ""),
            password_enc=d.get("password_enc"),
            remote_path=d.get("remote_path", ""),
        )


@dataclass
class JobDestination:
    """Ligação entre job e destino, com retenção que pode sobrescrever."""
    name: str
    retention_days: int | None = None

    def days(self, dest: Destination | None) -> int:
        if self.retention_days is not None:
            return self.retention_days
        return dest.retention_days if dest else 30


# ----------------------------------------------------------------------------
# Avisos
# ----------------------------------------------------------------------------

@dataclass
class NotifyMatrix:
    """Grade de evento por canal.

    `cells` guarda apenas o que foi decidido neste escopo. Célula ausente
    significa herdar, e é por isso que a tela consegue mostrar as duas linhas
    (o que vale e o que o global diz) sem inventar dado.
    """
    cells: dict[str, bool] = field(default_factory=dict)

    @staticmethod
    def _key(evento: NotifyEvent, canal: Channel) -> str:
        return f"{evento.value}:{canal.value}"

    def get(self, evento: NotifyEvent, canal: Channel) -> bool | None:
        return self.cells.get(self._key(evento, canal))

    def set(self, evento: NotifyEvent, canal: Channel, valor: bool) -> None:
        self.cells[self._key(evento, canal)] = valor

    def clear(self, evento: NotifyEvent, canal: Channel) -> None:
        self.cells.pop(self._key(evento, canal), None)

    def resolve(self, evento: NotifyEvent, canal: Channel, padrao: "NotifyMatrix") -> bool:
        valor = self.get(evento, canal)
        if valor is None:
            valor = padrao.get(evento, canal)
        return bool(valor)

    def overrides(self, padrao: "NotifyMatrix") -> int:
        n = 0
        for chave, valor in self.cells.items():
            if padrao.cells.get(chave) != valor:
                n += 1
        return n

    def to_dict(self) -> dict[str, bool]:
        return dict(self.cells)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "NotifyMatrix":
        return cls(cells={k: bool(v) for k, v in (d or {}).items()})

    @classmethod
    def default_global(cls) -> "NotifyMatrix":
        """Padrão de fábrica: silêncio significa sucesso.

        Sucesso não avisa. Falha, recuperação, janela perdida e silêncio longo
        avisam nos dois canais.
        """
        m = cls()
        for evento in NOTIFY_EVENTS:
            for canal in CHANNELS:
                m.set(evento, canal, evento is not NotifyEvent.SUCCESS)
        return m


# ----------------------------------------------------------------------------
# Job
# ----------------------------------------------------------------------------

@dataclass
class Job:
    name: str
    source: Source
    schedule: str = "0 3 * * *"
    timezone: str = "America/Sao_Paulo"
    enabled: bool = True
    # Atraso aceito antes de desistir da janela.
    catch_up_window_minutes: int = 360
    # Teto de duração. Estourou, o worker mata e passa para o próximo da fila.
    timeout_minutes: int = 240
    destinations: list[JobDestination] = field(default_factory=list)
    notify: NotifyMatrix = field(default_factory=NotifyMatrix)
    stale_after_hours: int = 48
    paused_at: str | None = None
    created: str = ""

    @property
    def kind(self) -> SourceKind:
        return self.source.kind

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "schedule": self.schedule,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "catch_up_window_minutes": self.catch_up_window_minutes,
            "timeout_minutes": self.timeout_minutes,
            "destinations": [
                {"name": d.name, "retention_days": d.retention_days} for d in self.destinations
            ],
            "notify": self.notify.to_dict(),
            "stale_after_hours": self.stale_after_hours,
            "paused_at": self.paused_at,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "Job":
        return cls(
            name=name,
            source=source_from_dict(d.get("source") or {}),
            schedule=d.get("schedule", "0 3 * * *"),
            timezone=d.get("timezone", "America/Sao_Paulo"),
            enabled=bool(d.get("enabled", True)),
            catch_up_window_minutes=int(d.get("catch_up_window_minutes", 360) or 360),
            timeout_minutes=int(d.get("timeout_minutes", 240) or 240),
            destinations=[
                JobDestination(x.get("name", ""), x.get("retention_days"))
                for x in (d.get("destinations") or [])
            ],
            notify=NotifyMatrix.from_dict(d.get("notify")),
            stale_after_hours=int(d.get("stale_after_hours", 48) or 48),
            paused_at=d.get("paused_at"),
            created=d.get("created", ""),
        )


# ----------------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------------

@dataclass
class StageRecord:
    stage: Stage
    state: StageState
    label: str = ""
    detail: str = ""
    seconds: float | None = None


@dataclass
class ManifestEntry:
    destination: str
    sha256: str
    bytes: int
    verified: bool = True


@dataclass
class Run:
    id: int
    job: str
    started_at: dt.datetime
    result: RunResult
    finished_at: dt.datetime | None = None
    bytes: int | None = None
    duration: float | None = None
    stages: list[StageRecord] = field(default_factory=list)
    manifest: list[ManifestEntry] = field(default_factory=list)
    ignored_regex: list[str] = field(default_factory=list)
    ignored_manual: list[str] = field(default_factory=list)
    artifact: str | None = None
    log: list[tuple[str, str, str]] = field(default_factory=list)  # hora, origem, texto
    error_stage: Stage | None = None
    error_tried: str = ""
    error_got: str = ""
    error_cause: str = ""
    error_fix: str = ""
    destinations_done: list[str] = field(default_factory=list)
    destinations_pending: list[str] = field(default_factory=list)
    retry_at: dt.datetime | None = None
    retry_count: int = 0

    @property
    def folder(self) -> str:
        """Pasta da execução: timestamp sem espaço e sem dois-pontos.

        Espaço vira %20 em chave S3 e dois-pontos quebram scp e rsync, que leem
        `host:caminho`. A ordenação alfabética coincide com a cronológica, então
        a listagem de qualquer destino já sai na ordem certa, e a idade da
        execução sai daqui, não do mtime.
        """
        return self.started_at.strftime("%Y-%m-%d_%H-%M-%S")

    @property
    def ignored_total(self) -> int:
        return len(self.ignored_regex) + len(self.ignored_manual)
