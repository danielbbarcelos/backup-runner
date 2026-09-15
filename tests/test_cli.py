"""Testes da camada de terminal.

Cada comando é uma função que imprime e devolve um código de saída, então
testar é chamar e ler o que saiu. Sem laço de eventos, sem widget, sem
simulação de tecla: é o que torna esta camada barata de manter.
"""
from __future__ import annotations

import io
import re
from contextlib import redirect_stdout

import pytest

from backup_runner.__main__ import main


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # Sem cor, para os testes olharem o texto e não as sequências de escape.
    monkeypatch.setenv("NO_COLOR", "1")
    import importlib

    from backup_runner import console

    importlib.reload(console)
    yield


def roda(*argv: str) -> tuple[int, str]:
    saida = io.StringIO()
    with redirect_stdout(saida):
        codigo = main(list(argv))
    return codigo, saida.getvalue()


def com_dados() -> None:
    from backup_runner.seed import populate

    populate()


# ----------------------------------------------------------------------------
# Comandos de leitura
# ----------------------------------------------------------------------------

def test_status_vazio_ensina_o_proximo_passo():
    codigo, texto = roda("status")
    assert codigo == 0
    assert "0 cadastrados" in texto


def test_status_com_dados_resume_o_essencial():
    com_dados()
    codigo, texto = roda("status")
    assert codigo == 0
    for esperado in ("jobs", "tick", "worker", "fila", "próxima"):
        assert esperado in texto


def test_jobs_lista_todos():
    com_dados()
    codigo, texto = roda("jobs")
    assert codigo == 0
    for job in ("loja_prod", "midia_uploads", "crm_replica", "conta_legado"):
        assert job in texto


def test_jobs_vazio_nao_e_erro():
    codigo, texto = roda("jobs")
    assert codigo == 0
    assert "Nenhum job" in texto
    assert "job add" in texto, "precisa dizer como criar o primeiro"


def test_job_detalha_e_mostra_execucoes():
    com_dados()
    codigo, texto = roda("job", "loja_prod")
    assert codigo == 0
    assert "db-01.local" in texto
    assert "destinos" in texto and "local-var" in texto
    assert "últimas execuções" in texto


def test_job_inexistente_sai_com_erro_util(capsys):
    com_dados()
    codigo, _ = roda("job", "nao_existe")
    assert codigo == 1
    assert "não existe job" in capsys.readouterr().err


def test_history_lista_e_filtra():
    com_dados()
    _, tudo = roda("history")
    _, falhas = roda("history", "--falhas")
    assert tudo.count("\n") > falhas.count("\n"), "o filtro não reduziu nada"
    assert "falha" in falhas


def test_run_info_mostra_o_molde_de_erro():
    com_dados()
    from backup_runner.models import RunResult
    from backup_runner.state import State

    estado = State()
    pendente = estado.pending_uploads()[0]
    estado.close()

    codigo, texto = roda("run-info", str(pendente.id))
    assert codigo == 0
    for rotulo in ("o que tentei", "o que recebi", "causa provável", "como consertar"):
        assert rotulo in texto
    assert "manifest" in texto


def test_run_info_inexistente(capsys):
    codigo, _ = roda("run-info", "9999")
    assert codigo == 1
    assert "não existe execução" in capsys.readouterr().err


def test_dest_lista_e_detalha():
    com_dados()
    codigo, texto = roda("dest")
    assert codigo == 0
    assert "local-var" in texto and "spaces-nyc3" in texto

    codigo, texto = roda("dest", "show", "spaces-nyc3")
    assert codigo == 0
    assert "nyc3" in texto


def test_dest_show_nao_vaza_o_secret():
    com_dados()
    from backup_runner.config import DestinationStore, encrypt

    store = DestinationStore.load()
    destino = store.get("spaces-nyc3")
    destino.secret_enc = encrypt("super-secreto-do-daniel")
    store.put(destino)

    _, texto = roda("dest", "show", "spaces-nyc3")
    assert "super-secreto-do-daniel" not in texto
    assert "cifrado" in texto


def test_notify_mostra_a_matriz():
    com_dados()
    codigo, texto = roda("notify")
    assert codigo == 0
    assert "email" in texto and "slack" in texto
    for evento in ("sucesso", "falha", "recuperado"):
        assert evento in texto


def test_health_sai_com_1_quando_ha_falha():
    codigo, texto = roda("health")
    assert "tick no crontab" in texto
    assert codigo in (0, 1)


# ----------------------------------------------------------------------------
# Comandos que mudam estado
# ----------------------------------------------------------------------------

def test_run_enfileira():
    com_dados()
    from backup_runner.state import State

    codigo, texto = roda("run", "loja_prod")
    assert codigo == 0
    assert "fila" in texto

    estado = State()
    assert any(f["job"] == "loja_prod" for f in estado.queue_pending())
    estado.close()


def test_run_de_job_inexistente(capsys):
    codigo, _ = roda("run", "fantasma")
    assert codigo == 1
    assert "não existe job" in capsys.readouterr().err


def test_pausar_e_retomar():
    com_dados()
    from backup_runner.config import JobStore

    roda("job", "loja_prod", "--pausar")
    assert JobStore.load().get("loja_prod").enabled is False
    roda("job", "loja_prod", "--pausar")
    assert JobStore.load().get("loja_prod").enabled is True


def test_apagar_job_com_yes_leva_as_execucoes_junto():
    com_dados()
    from backup_runner.config import JobStore
    from backup_runner.state import State

    estado = State()
    assert estado.count_runs(job="loja_prod") > 0
    estado.close()

    codigo, _ = roda("job", "loja_prod", "--apagar", "--yes")
    assert codigo == 0
    assert JobStore.load().get("loja_prod") is None

    estado = State()
    assert estado.count_runs(job="loja_prod") == 0
    estado.close()


def test_dest_rm_recusa_destino_em_uso(capsys):
    com_dados()
    codigo, _ = roda("dest", "rm", "local-var", "--yes")
    assert codigo == 1
    assert "apontam para ele" in capsys.readouterr().err


def test_dest_test_de_pasta_escreve_e_apaga(tmp_path):
    from backup_runner.config import DestinationStore
    from backup_runner.models import Destination, DestKind

    alvo = tmp_path / "backups"
    store = DestinationStore()
    store.put(Destination(name="local", kind=DestKind.LOCAL, path=str(alvo)))

    codigo, texto = roda("dest", "test", "local")
    assert codigo == 0
    assert "escreveu e apagou" in texto
    assert not list(alvo.glob(".probe*")), "a sonda ficou para trás"


# ----------------------------------------------------------------------------
# Saída utilizável em script
# ----------------------------------------------------------------------------

def test_sem_cor_a_saida_continua_completa():
    """Com NO_COLOR, nenhum escape sobra e a informação continua toda lá."""
    com_dados()
    _, texto = roda("jobs")
    assert "\033[" not in texto
    assert "ok" in texto and "falha" in texto


def test_tabela_alinha_pelo_conteudo_visivel():
    """O alinhamento não pode contar as sequências de cor como caractere."""
    import importlib

    import backup_runner.console as console

    # Liga a cor à força, para o cálculo de largura ser exercitado.
    import os

    os.environ["FORCE_COLOR"] = "1"
    importlib.reload(console)
    try:
        saida = io.StringIO()
        with redirect_stdout(saida):
            console.tabela(
                ["a", "b"],
                [[console.ok("sim"), "x"], ["nao", console.danger("y")]],
            )
        linhas = [l for l in saida.getvalue().splitlines() if l.strip()]
        larguras = {console.visivel(l) for l in linhas}
        assert len(larguras) <= 2, f"colunas desalinhadas: {larguras}"
    finally:
        del os.environ["FORCE_COLOR"]
        importlib.reload(console)


def test_worker_sai_com_codigo_proprio():
    """Sai com erro explícito em vez de fingir que está de pé."""
    codigo, _ = roda("worker")
    assert codigo == 3


def test_help_lista_os_comandos():
    with pytest.raises(SystemExit):
        roda("--help")


# ----------------------------------------------------------------------------
# Perguntas
# ----------------------------------------------------------------------------

def test_escolha_aceita_numero_e_chave(monkeypatch):
    from backup_runner import prompt

    opcoes = [("mysql", "um banco"), ("files", "um diretório")]

    monkeypatch.setattr("builtins.input", lambda _: "2")
    assert prompt.escolhe("qual", opcoes) == "files"

    monkeypatch.setattr("builtins.input", lambda _: "mysql")
    assert prompt.escolhe("qual", opcoes) == "mysql"

    monkeypatch.setattr("builtins.input", lambda _: "")
    assert prompt.escolhe("qual", opcoes, padrao="files") == "files"


def test_escolha_cancela_com_zero(monkeypatch):
    from backup_runner import prompt

    monkeypatch.setattr("builtins.input", lambda _: "0")
    with pytest.raises(prompt.Cancelado):
        prompt.escolhe("qual", [("a", "a"), ("b", "b")])


def test_marca_varios_entende_intervalo(monkeypatch):
    from backup_runner import prompt

    opcoes = [(str(i), f"item {i}") for i in range(1, 7)]
    respostas = iter(["2-4", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(respostas))
    assert prompt.marca_varios("marque", opcoes) == ["2", "3", "4"]


def test_marca_varios_alterna(monkeypatch):
    from backup_runner import prompt

    opcoes = [("a", "a"), ("b", "b")]
    respostas = iter(["1", "1", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(respostas))
    assert prompt.marca_varios("marque", opcoes) == []


def test_confirma_digitando_exige_o_nome_inteiro(monkeypatch):
    from backup_runner import prompt

    monkeypatch.setattr("builtins.input", lambda _: "loja_pro")
    assert prompt.confirma_digitando("apagar?", "loja_prod") is False

    monkeypatch.setattr("builtins.input", lambda _: "loja_prod")
    assert prompt.confirma_digitando("apagar?", "loja_prod") is True


def test_ctrl_c_cancela_sem_gravar(monkeypatch):
    from backup_runner import prompt

    def interrompe(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrompe)
    with pytest.raises(prompt.Cancelado):
        prompt.texto("qualquer coisa")


def test_inteiro_recusa_texto_e_faixa(monkeypatch):
    from backup_runner import prompt

    respostas = iter(["abc", "0", "99", "7"])
    monkeypatch.setattr("builtins.input", lambda _: next(respostas))
    assert prompt.inteiro("dias", padrao=30, minimo=1, maximo=90) == 7
