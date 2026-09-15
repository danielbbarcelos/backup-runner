"""Compactação de diretórios, em fluxo.

`tar.gz` é o padrão: preserva modo, dono, symlink e mtime, e o `tarfile` do
Python em modo `w|gz` escreve enquanto lê, sem montar nada em memória nem
precisar de espaço para uma cópia crua.

`zip` existe para o caso de o arquivo ir abrir no Windows. Ele perde
permissão e dono, e a diferença aparece na hora de restaurar o `storage/` de
uma aplicação, então não é o padrão.
"""
from __future__ import annotations

import fnmatch
import os
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


class ArchiveError(Exception):
    pass


@dataclass
class ArchiveRequest:
    source: Path
    output_file: Path        # sem extensão: o formato decide
    excludes: list[str] = field(default_factory=list)
    follow_links: bool = False
    format: str = "tar.gz"
    compress_level: int = 6


@dataclass
class ArchiveResult:
    output_file: Path
    files: int
    bytes_raw: int
    bytes_written: int
    skipped: int
    elapsed_seconds: float

    @property
    def ratio(self) -> float:
        if not self.bytes_raw:
            return 0.0
        return 1 - (self.bytes_written / self.bytes_raw)


def excluded(relativo: str, nome: str, padroes: Iterable[str]) -> bool:
    """Um caminho casa a exclusão pelo nome, pelo caminho relativo ou pelo prefixo.

    `cache/**` precisa pegar `cache/a/b.txt`, e `*.tmp` precisa pegar qualquer
    arquivo com essa extensão em qualquer nível. Cobrir os três casos é o que
    faz o padrão se comportar como a pessoa espera ao digitá-lo.
    """
    for padrao in padroes:
        if fnmatch.fnmatch(nome, padrao) or fnmatch.fnmatch(relativo, padrao):
            return True
        raiz = padrao.rstrip("*/")
        if raiz and (relativo == raiz or relativo.startswith(raiz + "/")):
            return True
    return False


def _percorre(base: Path, padroes: list[str], seguir_links: bool, fora: list[int]):
    """Gera (caminho, relativo) do que entra, somando em `fora` o que sai.

    O contador vem de fora numa lista porque um gerador não tem como devolver
    um total no fim, e passá-lo em cada item fazia o valor final depender da
    ordem em que o walk visitou as pastas.
    """
    for raiz, diretorios, arquivos in os.walk(base, followlinks=seguir_links):
        raiz_path = Path(raiz)
        # Poda diretório excluído antes de descer: não adianta listar dez mil
        # arquivos dentro de node_modules para descartar um a um.
        mantidos = []
        for d in diretorios:
            rel = str((raiz_path / d).relative_to(base))
            if excluded(rel, d, padroes):
                # Conta o que existe dentro da pasta podada, não a pasta.
                for r, _, ns in os.walk(raiz_path / d):
                    fora[0] += len(ns)
            else:
                mantidos.append(d)
        diretorios[:] = mantidos

        for nome in arquivos:
            caminho = raiz_path / nome
            rel = str(caminho.relative_to(base))
            if excluded(rel, nome, padroes):
                fora[0] += 1
                continue
            yield caminho, rel


def create(
    request: ArchiveRequest,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> ArchiveResult:
    """Compacta o diretório, escrevendo enquanto lê.

    `on_progress(arquivos, bytes)` é chamado a cada 200 arquivos, o suficiente
    para o terminal mostrar movimento sem gastar tempo com isso.
    """
    base = request.source.expanduser().resolve()
    if not base.is_dir():
        raise ArchiveError(f"{base} não é um diretório")

    destino = request.output_file
    extensao = ".tar.gz" if request.format == "tar.gz" else ".zip"
    if not str(destino).endswith(extensao):
        destino = destino.with_name(destino.name + extensao)
    destino.parent.mkdir(parents=True, exist_ok=True)

    inicio = time.monotonic()
    if request.format == "tar.gz":
        arquivos, cru, fora = _tar(base, destino, request, on_progress)
    else:
        arquivos, cru, fora = _zip(base, destino, request, on_progress)

    return ArchiveResult(
        output_file=destino,
        files=arquivos,
        bytes_raw=cru,
        bytes_written=destino.stat().st_size if destino.exists() else 0,
        skipped=fora,
        elapsed_seconds=time.monotonic() - inicio,
    )


def _tar(base: Path, destino: Path, request: ArchiveRequest, on_progress) -> tuple[int, int, int]:
    arquivos = cru = 0
    fora = [0]
    # `w|gz` é o modo de fluxo: escreve sem voltar atrás e sem montar índice.
    # O nome vai como string: em modo de fluxo o tarfile checa a extensão do
    # nome e um Path não tem `.endswith`.
    with tarfile.open(str(destino), "w|gz", compresslevel=request.compress_level) as tar:
        for caminho, relativo in _percorre(base, request.excludes, request.follow_links, fora):
            try:
                info = tar.gettarinfo(str(caminho), arcname=relativo)
            except OSError:
                continue
            if info is None:
                continue
            try:
                if info.isreg():
                    with caminho.open("rb") as f:
                        tar.addfile(info, f)
                    cru += info.size
                else:
                    tar.addfile(info)
            except OSError:
                # Arquivo que sumiu ou sem permissão não derruba o backup
                # inteiro; o que importa é levar o resto.
                continue
            arquivos += 1
            if on_progress and arquivos % 200 == 0:
                on_progress(arquivos, cru)
    return arquivos, cru, fora[0]


def _zip(base: Path, destino: Path, request: ArchiveRequest, on_progress) -> tuple[int, int, int]:
    arquivos = cru = 0
    fora = [0]
    with zipfile.ZipFile(
        destino, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=request.compress_level
    ) as zf:
        for caminho, relativo in _percorre(base, request.excludes, request.follow_links, fora):
            try:
                zf.write(caminho, arcname=relativo)
                cru += caminho.stat().st_size
            except OSError:
                continue
            arquivos += 1
            if on_progress and arquivos % 200 == 0:
                on_progress(arquivos, cru)
    return arquivos, cru, fora[0]


def preview(source: Path, excludes: list[str], follow_links: bool = False) -> dict:
    """Conta o que entraria, sem compactar nada.

    Percorre de verdade em vez de estimar, porque a diferença entre "2,9 GB" e
    "2,9 GB menos o cache" é justamente o que a pessoa quer conferir antes de
    agendar.
    """
    base = source.expanduser()
    if not base.is_dir():
        raise ArchiveError(f"{base} não é um diretório")

    inicio = time.monotonic()
    arquivos = cru = fora = bytes_fora = 0
    for raiz, diretorios, nomes in os.walk(base, followlinks=follow_links):
        raiz_path = Path(raiz)
        mantidos = []
        for d in diretorios:
            rel = str((raiz_path / d).relative_to(base))
            if excluded(rel, d, excludes):
                for r, _, ns in os.walk(raiz_path / d):
                    for n in ns:
                        try:
                            bytes_fora += (Path(r) / n).stat().st_size
                            fora += 1
                        except OSError:
                            pass
            else:
                mantidos.append(d)
        diretorios[:] = mantidos

        for nome in nomes:
            caminho = raiz_path / nome
            rel = str(caminho.relative_to(base))
            try:
                tamanho = caminho.stat().st_size
            except OSError:
                continue
            if excluded(rel, nome, excludes):
                fora += 1
                bytes_fora += tamanho
            else:
                arquivos += 1
                cru += tamanho

    return {
        "arquivos": arquivos,
        "bytes": cru,
        "excluidos": fora,
        "bytes_excluidos": bytes_fora,
        "t": time.monotonic() - inicio,
    }
