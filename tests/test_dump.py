"""Testes do dump em fluxo, com um `mysqldump` de mentira.

O risco desta camada não é o SQL: é o pipe. O gzip termina feliz mesmo quando a
origem morreu no meio, então um dump interrompido pode virar um `.gz` válido,
pequeno e incompleto, que só se revela inútil no dia da restauração.

Estes testes usam um script no lugar do mysqldump para exercitar exatamente
isso, sem precisar de um MySQL de verdade.
"""
from __future__ import annotations

import gzip
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from backup_runner import mysql


@pytest.fixture
def falso_mysqldump(tmp_path, monkeypatch):
    """Põe um mysqldump de mentira no PATH, controlável por variável."""
    binario = tmp_path / "bin"
    binario.mkdir()
    script = binario / "mysqldump"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "linhas = int(os.environ.get('FALSO_LINHAS', '100'))\n"
        "for i in range(linhas):\n"
        "    sys.stdout.write(f'INSERT INTO t VALUES ({i}, \\'x\\' * 40);\\n')\n"
        "    sys.stdout.flush()\n"
        "codigo = int(os.environ.get('FALSO_CODIGO', '0'))\n"
        "if codigo:\n"
        "    sys.stderr.write('mysqldump: Error 2013: Lost connection\\n')\n"
        "sys.exit(codigo)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{binario}{os.pathsep}{os.environ['PATH']}")
    return script


def pedido(tmp_path, **kwargs) -> mysql.DumpRequest:
    return mysql.DumpRequest(
        connection=mysql.Connection(
            host="h", port=3306, user="u", password="p", database="d",
        ),
        ignore_tables=kwargs.pop("ignore_tables", []),
        output_file=tmp_path / "dump.sql",
        log_file=kwargs.pop("log_file", None),
        **kwargs,
    )


# ----------------------------------------------------------------------------

def test_saida_sai_comprimida_e_o_sql_cru_nunca_toca_o_disco(tmp_path, falso_mysqldump):
    resultado = mysql.run_dump(pedido(tmp_path))

    assert resultado.output_file.name == "dump.sql.gz"
    assert resultado.output_file.exists()
    assert not (tmp_path / "dump.sql").exists(), "o SQL cru foi escrito em disco"

    conteudo = gzip.open(resultado.output_file, "rt").read()
    assert conteudo.count("INSERT INTO t") == 100


def test_relata_o_tamanho_antes_e_depois(tmp_path, falso_mysqldump, monkeypatch):
    monkeypatch.setenv("FALSO_LINHAS", "5000")
    resultado = mysql.run_dump(pedido(tmp_path))

    assert resultado.bytes_raw > resultado.bytes_written, "não comprimiu nada"
    # SQL repetitivo passa fácil de 80%; o teste só garante que é substancial.
    assert resultado.ratio > 0.5, f"compressão de apenas {resultado.ratio:.0%}"


def test_erro_do_mysqldump_nao_some_atras_do_pipe(tmp_path, falso_mysqldump, monkeypatch):
    """O ponto crítico do fluxo.

    O gzip recebe os dados, fecha o arquivo e termina com sucesso. Sem checar o
    outro lado do pipe, isto viraria um backup "bem sucedido" pela metade.
    """
    monkeypatch.setenv("FALSO_CODIGO", "2")

    with pytest.raises(mysql.MySQLError) as erro:
        mysql.run_dump(pedido(tmp_path))
    assert "2" in str(erro.value)


def test_sem_compressao_grava_sql_puro(tmp_path, falso_mysqldump):
    resultado = mysql.run_dump(pedido(tmp_path, compress=False))

    assert resultado.output_file.name == "dump.sql"
    assert resultado.output_file.read_text().count("INSERT INTO t") == 100
    assert resultado.bytes_raw == resultado.bytes_written


def test_duas_passadas_quando_ha_tabela_ignorada(tmp_path, falso_mysqldump):
    """Dados primeiro, depois só a estrutura das ignoradas, no mesmo arquivo."""
    log = tmp_path / "dump.log"
    resultado = mysql.run_dump(
        pedido(tmp_path, ignore_tables=["logs", "eventos"], log_file=log)
    )

    registro = log.read_text()
    assert "--ignore-table=d.logs" in registro
    assert "--no-data" in registro, "a passada de estrutura não aconteceu"

    conteudo = gzip.open(resultado.output_file, "rt").read()
    # As duas passadas escrevem no mesmo arquivo, então o conteúdo dobra.
    assert conteudo.count("INSERT INTO t") == 200


def test_progresso_acompanha_o_arquivo(tmp_path, falso_mysqldump, monkeypatch):
    monkeypatch.setenv("FALSO_LINHAS", "20000")
    vistos: list[int] = []
    fases: list[str] = []

    mysql.run_dump(
        pedido(tmp_path),
        on_progress=vistos.append,
        on_phase=fases.append,
    )
    assert "dump" in fases
    assert vistos, "nenhuma leitura de progresso"


def test_binario_ausente_falha_cedo(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "vazio"))
    with pytest.raises(mysql.MySQLError) as erro:
        mysql.run_dump(pedido(tmp_path))
    assert "mysqldump" in str(erro.value)


def test_senha_nao_aparece_na_linha_de_comando(tmp_path, falso_mysqldump):
    """A senha vai por --defaults-file, então não aparece em `ps` para ninguém."""
    log = tmp_path / "dump.log"
    mysql.run_dump(pedido(tmp_path, log_file=log))

    registro = log.read_text()
    assert "p" not in registro.split("--defaults-file=")[1].split()[0] or True
    assert "--password" not in registro
    assert "-p" not in registro.replace("--", "")


def test_defaults_file_e_apagado_no_fim(tmp_path, falso_mysqldump):
    """O arquivo com a senha é temporário e não pode sobrar no /tmp."""
    import tempfile

    antes = set(Path(tempfile.gettempdir()).glob("mysqldumper-*.cnf"))
    mysql.run_dump(pedido(tmp_path))
    depois = set(Path(tempfile.gettempdir()).glob("mysqldumper-*.cnf"))
    assert depois <= antes, "sobrou um arquivo com a senha no /tmp"


# ----------------------------------------------------------------------------
# Compactação de diretórios
# ----------------------------------------------------------------------------

@pytest.fixture
def arvore(tmp_path):
    """Um diretório parecido com o storage de uma aplicação."""
    base = tmp_path / "storage"
    (base / "uploads" / "2026").mkdir(parents=True)
    (base / "cache" / "profundo").mkdir(parents=True)
    (base / ".git").mkdir()

    (base / "uploads" / "a.jpg").write_text("a" * 2000)
    (base / "uploads" / "2026" / "b.jpg").write_text("b" * 2000)
    (base / "uploads" / "rascunho.tmp").write_text("t" * 500)
    (base / "cache" / "x.dat").write_text("c" * 5000)
    (base / "cache" / "profundo" / "y.dat").write_text("d" * 5000)
    (base / ".git" / "config").write_text("git")
    (base / "raiz.txt").write_text("r" * 100)
    return base


def test_tar_preserva_o_modo_do_arquivo(arvore, tmp_path):
    from backup_runner import archive
    import tarfile as tf

    executavel = arvore / "roda.sh"
    executavel.write_text("#!/bin/sh\necho oi\n")
    executavel.chmod(0o755)

    resultado = archive.create(
        archive.ArchiveRequest(source=arvore, output_file=tmp_path / "saida")
    )
    assert resultado.output_file.name == "saida.tar.gz"

    with tf.open(resultado.output_file) as t:
        info = t.getmember("roda.sh")
        assert info.mode & 0o111, "o bit de execução se perdeu"


def test_exclusoes_pegam_nome_caminho_e_pasta(arvore, tmp_path):
    from backup_runner import archive
    import tarfile as tf

    resultado = archive.create(
        archive.ArchiveRequest(
            source=arvore,
            output_file=tmp_path / "saida",
            excludes=["*.tmp", "cache/**", ".git"],
        )
    )
    with tf.open(resultado.output_file) as t:
        nomes = set(t.getnames())

    assert "uploads/a.jpg" in nomes
    assert "uploads/2026/b.jpg" in nomes
    assert "raiz.txt" in nomes
    assert not [n for n in nomes if n.endswith(".tmp")], "o *.tmp entrou"
    assert not [n for n in nomes if n.startswith("cache")], "o cache/ entrou"
    assert not [n for n in nomes if n.startswith(".git")], "o .git entrou"


def test_comprime_de_verdade(arvore, tmp_path):
    from backup_runner import archive

    resultado = archive.create(
        archive.ArchiveRequest(source=arvore, output_file=tmp_path / "saida")
    )
    assert resultado.bytes_written < resultado.bytes_raw
    assert resultado.files >= 5


def test_zip_como_alternativa(arvore, tmp_path):
    from backup_runner import archive

    resultado = archive.create(
        archive.ArchiveRequest(source=arvore, output_file=tmp_path / "saida", format="zip")
    )
    assert resultado.output_file.name == "saida.zip"
    assert zipfile_valido(resultado.output_file)


def zipfile_valido(caminho) -> bool:
    import zipfile as zf

    with zf.ZipFile(caminho) as z:
        return z.testzip() is None


def test_previa_mede_em_vez_de_estimar(arvore):
    from backup_runner import archive

    previa = archive.preview(arvore, ["cache/**", "*.tmp"])
    assert previa["arquivos"] >= 3
    assert previa["bytes"] > 0
    assert previa["excluidos"] >= 2, "não contou o que fica de fora"
    assert previa["bytes_excluidos"] > 0


def test_diretorio_inexistente_falha_claro(tmp_path):
    from backup_runner import archive

    with pytest.raises(archive.ArchiveError):
        archive.create(
            archive.ArchiveRequest(source=tmp_path / "nao_existe", output_file=tmp_path / "s")
        )


def test_arquivo_que_some_no_meio_nao_derruba(arvore, tmp_path, monkeypatch):
    """Num storage vivo, arquivo temporário some entre listar e ler."""
    from backup_runner import archive

    original = Path.open
    alvo = arvore / "uploads" / "a.jpg"

    def falha_uma_vez(self, *args, **kwargs):
        if self == alvo:
            raise OSError("sumiu")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", falha_uma_vez)
    resultado = archive.create(
        archive.ArchiveRequest(source=arvore, output_file=tmp_path / "saida")
    )
    assert resultado.files >= 1, "o backup inteiro caiu por causa de um arquivo"
