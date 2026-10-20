"""Guarda de espaço em disco do staging.

O dump e o arquivamento escrevem no staging sem saber de antemão o tamanho
final. Num servidor com o disco do sistema quase cheio, isso enchia o disco
no meio da madrugada e derrubava o banco e a aplicação junto com o backup.

A guarda olha o disco antes de começar e de novo durante a escrita, a cada
poucos segundos, e para a execução com uma explicação antes de chegar no
limite. O artefato parcial é apagado por quem trata a falha.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

GB = 1024 ** 3


class SemEspaco(Exception):
    """O disco do staging passou do limite configurado."""

    def __init__(self, caminho: Path, livre: int, usado_pct: float, min_livre: int, max_pct: int) -> None:
        self.caminho, self.livre, self.usado_pct = caminho, livre, usado_pct
        self.min_livre, self.max_pct = min_livre, max_pct
        super().__init__(
            f"disco do staging ({caminho}) com {livre / GB:.1f} GB livres e {usado_pct:.0f}% usado; "
            f"o limite é {min_livre / GB:.0f} GB livres e {max_pct}% usado"
        )


@dataclass
class EspacoGuarda:
    caminho: Path
    min_livre: int          # bytes
    max_pct: int            # porcentagem do disco
    intervalo: float = 2.0  # segundos entre leituras durante a escrita
    _ultima: float = field(default=0.0, repr=False)

    @classmethod
    def do_staging(cls, caminho: Path) -> "EspacoGuarda":
        from .config import Settings

        s = Settings.load()
        return cls(Path(caminho), s.staging_min_free_gb * GB, s.staging_max_disk_pct)

    def checa(self) -> None:
        """Lê o disco agora e levanta SemEspaco se passou do limite."""
        alvo = self.caminho
        while not alvo.exists() and alvo != alvo.parent:
            alvo = alvo.parent
        uso = shutil.disk_usage(str(alvo))
        usado_pct = 100.0 * (uso.total - uso.free) / uso.total if uso.total else 0.0
        if uso.free < self.min_livre or usado_pct > self.max_pct:
            raise SemEspaco(self.caminho, uso.free, usado_pct, self.min_livre, self.max_pct)

    def __call__(self) -> None:
        """Para os callbacks de progresso: só lê o disco de tempos em tempos."""
        agora = time.monotonic()
        if agora - self._ultima < self.intervalo:
            return
        self._ultima = agora
        self.checa()
