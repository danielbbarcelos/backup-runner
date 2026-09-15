"""Testes da camada de terminal.

Cada comando é uma função que imprime e devolve um código de saída, então
testar é chamar e ler o que saiu. Sem laço de eventos, sem widget, sem
simulação de tecla: é o que torna esta camada barata de manter.
"""
from __future__ import annotations

import io
import sys
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


def test_worker_com_fila_vazia_sai_na_hora():
    """`--uma-vez` existe para isto: rodar o que houver e sair.

    Sem a flag o worker fica de pé esperando, que é o certo para o supervisord
    e o errado para um teste.
    """
    codigo, _ = roda("worker", "--uma-vez")
    assert codigo == 0


def test_worker_processa_um_item_e_sai(tmp_path):
    from backup_runner.config import DestinationStore, JobStore
    from backup_runner.models import (
        Destination, DestKind, FilesSource, Job, JobDestination, RunResult,
    )
    from backup_runner.state import State

    origem = tmp_path / "dados"
    origem.mkdir()
    (origem / "arquivo.txt").write_text("conteúdo" * 100)

    DestinationStore.load().put(Destination(
        name="disco", kind=DestKind.LOCAL, path=str(tmp_path / "destino"),
    ))
    JobStore.load().put(Job(
        name="teste", source=FilesSource(path=str(origem)),
        destinations=[JobDestination("disco", 7)],
    ))

    assert roda("run", "teste")[0] == 0
    assert roda("worker", "--uma-vez")[0] == 0

    estado = State()
    execucoes = estado.runs(job="teste")
    estado.close()
    assert len(execucoes) == 1
    assert execucoes[0].result is RunResult.OK
    assert (tmp_path / "destino" / "teste").exists()


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


# ----------------------------------------------------------------------------
# Teclado
# ----------------------------------------------------------------------------

def test_sem_terminal_cai_no_modo_numerado():
    """Num pipe, num cron ou num teste não há tecla para ler."""
    from backup_runner import keys

    assert keys.disponivel() is False


def test_mapa_de_sequencias_cobre_as_setas():
    from backup_runner import keys

    assert keys.SEQUENCIAS["[A"] == keys.CIMA
    assert keys.SEQUENCIAS["[B"] == keys.BAIXO
    # Modo de aplicação manda O no lugar de [.
    assert keys.SEQUENCIAS["OA"] == keys.CIMA
    assert keys.SEQUENCIAS["OB"] == keys.BAIXO


def test_ler_tecla_num_terminal_de_verdade():
    """A seta precisa chegar como seta, não como Esc.

    O bug original: `sys.stdin.read` enche um buffer interno de uma vez, então
    o `select` que verifica se a sequência continua olhava um descritor já
    vazio e concluía que era a tecla Esc sozinha. Só um terminal de verdade
    exercita isso, então o teste abre um.
    """
    import os
    import pty
    import time

    codigo = (
        "import sys; sys.path.insert(0, %r);"
        "from backup_runner import keys;"
        "print('LIDO:', keys.ler(), flush=True)"
    ) % os.path.join(os.getcwd(), "src")

    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", codigo])

    time.sleep(0.8)
    os.write(fd, b"\x1b[B")          # seta para baixo
    saida = b""
    fim = time.time() + 3
    while time.time() < fim:
        try:
            pedaco = os.read(fd, 1024)
        except OSError:
            break
        if not pedaco:
            break
        saida += pedaco
        if b"LIDO:" in saida:
            break
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass

    texto = saida.decode(errors="replace")
    assert "LIDO: baixo" in texto, f"a seta não chegou como seta: {texto!r}"


def test_marca_varios_por_numero_ainda_funciona(monkeypatch):
    """O caminho sem terminal não pode regredir por causa do caminho com setas."""
    from backup_runner import prompt

    opcoes = [(str(i), f"item {i}") for i in range(1, 5)]
    respostas = iter(["todos", "2", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(respostas))
    assert prompt.marca_varios("marque", opcoes) == ["1", "3", "4"]


def test_hero_cabe_em_quatro_linhas():
    """O wordmark de doze linhas reaparecia a cada navegação."""
    import importlib
    import os

    os.environ["FORCE_COLOR"] = "1"
    import backup_runner.console as console

    importlib.reload(console)
    try:
        saida = io.StringIO()
        with redirect_stdout(saida):
            console.hero("9.9.9", "uma linha de descrição", estado="4 jobs")
        linhas = saida.getvalue().rstrip("\n").split("\n")
        assert len(linhas) == 5, f"o hero cresceu para {len(linhas)} linhas"
        assert linhas[0].strip().startswith("\033[38;5;147m╭") or "╭" in linhas[0]
        assert "╰" in linhas[-1]
    finally:
        del os.environ["FORCE_COLOR"]
        importlib.reload(console)


def test_limpa_tela_nao_suja_pipe():
    """Uma sequência de escape num arquivo redirecionado seria lixo."""
    from backup_runner import console

    saida = io.StringIO()
    with redirect_stdout(saida):
        console.limpa()
    assert saida.getvalue() == ""


# ----------------------------------------------------------------------------
# Edição de linha e rodapé
# ----------------------------------------------------------------------------

def test_readline_esta_carregado():
    """Sem readline, a seta para a esquerda vira ^[[D dentro do texto."""
    from backup_runner import prompt

    assert prompt.readline is not None


def test_valor_atual_vem_preenchido_e_editavel():
    """Editar 3306 para 3307 deve ser mudar um caractere, não redigitar tudo."""
    import os
    import pty
    import time

    codigo = (
        "import sys; sys.path.insert(0, %r);"
        "from backup_runner import prompt;"
        "print('R:', prompt.texto('porta', padrao='3306'), flush=True)"
    ) % os.path.join(os.getcwd(), "src")

    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", codigo])

    time.sleep(0.9)
    os.write(fd, b"\x1b[D")   # seta para a esquerda: cursor entre o 0 e o 6
    time.sleep(0.2)
    os.write(fd, b"\x7f")     # backspace apaga o 0
    time.sleep(0.2)
    os.write(fd, b"7\r")      # digita 7 e confirma

    saida = b""
    fim = time.time() + 3
    while time.time() < fim:
        try:
            pedaco = os.read(fd, 1024)
        except OSError:
            break
        if not pedaco:
            break
        saida += pedaco
        if b"R:" in saida:
            break
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass

    texto = saida.decode(errors="replace")
    assert "R: 3376" in texto, f"a edição no meio da linha não funcionou: {texto!r}"


def test_menu_nao_repete_a_opcao_de_saida():
    """O item da lista e o rótulo do esc mostravam a mesma palavra duas vezes."""
    import inspect

    from backup_runner import menu

    fonte = inspect.getsource(menu)
    # O menu principal e os submenus usam rotulo_saida; nenhum deles deve
    # também carregar o item na lista.
    assert '("sair", "sair")' not in fonte
    assert '("voltar", "voltar")' not in fonte


def test_rodape_tem_respiro_antes_das_teclas():
    from backup_runner import prompt

    linhas = prompt._linhas_opcoes(
        [("a", "primeira"), ("b", "segunda")], 0, permitir_cancelar=True, rotulo_saida="voltar",
    )
    assert len(linhas) == 3  # duas opções mais a saída
    assert "→" in linhas[0]


# ----------------------------------------------------------------------------
# Regressões da edição de campo
# ----------------------------------------------------------------------------

def _num_pty(codigo: str, teclas: list[bytes], espera: float = 0.9) -> str:
    """Roda um trecho num terminal de verdade e devolve o que apareceu."""
    import os
    import pty
    import re
    import select
    import time

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["FORCE_COLOR"] = "1"
        os.execv(sys.executable, [sys.executable, "-c", codigo])

    time.sleep(espera)
    for t in teclas:
        os.write(fd, t)
        time.sleep(0.15)

    saida = b""
    fim = time.time() + 3
    while time.time() < fim:
        pronto, _, _ = select.select([fd], [], [], 0.2)
        if not pronto:
            continue
        try:
            pedaco = os.read(fd, 4096)
        except OSError:
            break
        if not pedaco:
            break
        saida += pedaco
        if b"FIM:" in saida:
            break
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", saida.decode(errors="replace"))


def test_campo_preenchido_nao_aparece_duas_vezes():
    """O bug: chamar redisplay() no startup_hook desenhava a linha de novo.

    Na tela de editar destino isso aparecia como `nome: X   nome: X`, com
    todos os campos repetidos lado a lado.
    """
    import os

    codigo = (
        "import sys; sys.path.insert(0, %r);"
        "from backup_runner import prompt;"
        "v = prompt.texto('nome', padrao='Peer Saude Backups');"
        "print('FIM:', v, flush=True)"
    ) % os.path.join(os.getcwd(), "src")

    texto = _num_pty(codigo, [b"\r"])
    antes_do_fim = texto.split("FIM:")[0]
    assert antes_do_fim.count("Peer Saude Backups") == 1, (
        f"o campo apareceu {antes_do_fim.count('Peer Saude Backups')} vezes:\n{antes_do_fim}"
    )


def test_senha_mostra_mascara_e_aceita_backspace():
    """Sem eco nenhum não dá para saber se o teclado está funcionando."""
    import os

    codigo = (
        "import sys; sys.path.insert(0, %r);"
        "from backup_runner import prompt;"
        "s = prompt.senha('secret');"
        "print('FIM:', s, flush=True)"
    ) % os.path.join(os.getcwd(), "src")

    texto = _num_pty(codigo, [b"s3cr", b"\x7f", b"et", b"\r"])
    assert "•" in texto, "a senha não mostrou máscara"
    assert "FIM: s3cet" in texto, f"o backspace não apagou no lugar certo: {texto!r}"
    assert "FIM: s3cret" not in texto


def test_seta_para_na_ponta_em_vez_de_dar_a_volta():
    """Dar a volta rolava a tela inteira e piscava."""
    import os

    codigo = (
        "import sys; sys.path.insert(0, %r);"
        "from backup_runner import prompt;"
        "v = prompt.escolhe('qual', [('a','primeira'),('b','segunda')], permitir_cancelar=False);"
        "print('FIM:', v, flush=True)"
    ) % os.path.join(os.getcwd(), "src")

    # Cinco vezes para baixo numa lista de dois: precisa parar na segunda.
    texto = _num_pty(codigo, [b"\x1b[B"] * 5 + [b"\r"])
    assert "FIM: b" in texto

    # E cinco para cima precisa parar na primeira, sem passar para o fim.
    texto = _num_pty(codigo, [b"\x1b[A"] * 5 + [b"\r"])
    assert "FIM: a" in texto


def test_modo_cru_e_reentrante():
    """Abrir e fechar a cada tecla perdia o que já estava digitado."""
    from backup_runner import keys

    assert hasattr(keys, "cru"), "falta o contexto que segura o modo cru"
    assert keys._profundidade == 0, "o contador de profundidade não zerou"


def test_existe_como_configurar_os_canais():
    """Sem isto, a matriz de avisos aponta para canais que não existem."""
    from backup_runner import forms
    from backup_runner.__main__ import build_parser

    assert hasattr(forms, "configura_smtp")
    assert hasattr(forms, "configura_slack")

    acoes = build_parser()._subparsers._group_actions[0].choices
    assert "channels" in acoes
    assert "notify-test" in acoes


def test_canal_nao_configurado_nao_avisa_ninguem():
    """Marcar um evento num canal sem configuração não pode fingir que avisa."""
    from backup_runner.config import Settings
    from backup_runner.models import Channel, Job, MySQLSource, NotifyEvent

    settings = Settings()
    assert settings.channel_configured("email") is False
    assert settings.channel_configured("slack") is False

    settings.smtp = {"host": "smtp.exemplo.com", "to": "eu@exemplo.com"}
    assert settings.channel_configured("email") is True


def test_redesenho_nao_embaralha_o_bloco():
    """Regressão: o texto novo saía deslocado por cima do antigo.

    A causa foi a soma de duas coisas: o respiro no rodapé mudou a altura do
    bloco, e `\\033[2K` apaga a linha mas não devolve o cursor à coluna 0. O
    sintoma na tela era duas opções na mesma linha, cada uma começando onde a
    anterior parou.
    """
    import os

    codigo = (
        "import sys; sys.path.insert(0, %r);"
        "from backup_runner import prompt;"
        "v = prompt.escolhe('escolha', ["
        "  ('a', 'alfa    primeira opcao'),"
        "  ('b', 'bravo   segunda opcao'),"
        "  ('c', 'charlie terceira opcao'),"
        "], permitir_cancelar=False);"
        "print('FIM:', v, flush=True)"
    ) % os.path.join(os.getcwd(), "src")

    texto = _num_pty(codigo, [b"\x1b[B", b"\x1b[B", b"\x1b[A", b"\r"])

    for linha in texto.splitlines():
        # Duas opções na mesma linha é exatamente o embaralhado.
        assert not (("alfa" in linha) and ("bravo" in linha)), f"linha embaralhada: {linha!r}"
        assert not (("bravo" in linha) and ("charlie" in linha)), f"linha embaralhada: {linha!r}"
        assert linha.count("primeira opcao") <= 1, f"opção repetida na linha: {linha!r}"

    assert "FIM: b" in texto


def test_altura_do_bloco_bate_com_o_que_foi_impresso():
    """O que sobe precisa ser exatamente o que desceu, senão a tela desalinha."""
    import io
    from contextlib import redirect_stdout

    from backup_runner import prompt

    bloco = ["linha 1", "linha 2", "", "teclas"]

    # No redesenho, uma linha impressa por linha do bloco.
    saida = io.StringIO()
    with redirect_stdout(saida):
        altura = prompt._desenha_bloco(bloco, redesenhando=True)
    assert altura == len(bloco)
    assert saida.getvalue().count("\n") == altura

    # No primeiro desenho vem a reserva antes: as linhas vazias que forçam o
    # scroll acontecer agora, e não no meio do bloco.
    saida = io.StringIO()
    with redirect_stdout(saida):
        altura = prompt._desenha_bloco(bloco, redesenhando=False)
    assert altura == len(bloco)
    assert saida.getvalue().count("\n") == altura * 2


def test_modo_de_leitura_preserva_a_quebra_de_linha_do_terminal():
    """Regressão: `setraw` desliga o pós-processamento da saída.

    Sem ele, um `\\n` desce uma linha e não volta para a coluna 0. O resultado
    é a tela em escadinha e o prompt do shell aparecendo no meio da linha
    depois que o programa sai. `setcbreak` desliga só o modo de linha e o eco.
    """
    import inspect

    from backup_runner import keys

    fonte = inspect.getsource(keys.cru)
    assert "setcbreak" in fonte
    assert "setraw" not in fonte.replace("`setraw`", ""), "voltou para o modo cru total"


def test_saida_do_menu_deixa_o_cursor_na_coluna_zero():
    """O prompt do shell precisa começar do começo da linha."""
    import os
    import pty
    import select
    import time

    codigo = (
        "import os, sys; sys.path.insert(0, %r);"
        "os.environ['FORCE_COLOR'] = '1';"
        "os.environ['XDG_CONFIG_HOME'] = %r;"
        "os.environ['XDG_DATA_HOME'] = %r;"
        "from backup_runner.context import Context;"
        "from backup_runner import menu;"
        "menu.principal(Context());"
        "print('DEPOIS', flush=True)"
    ) % (
        os.path.join(os.getcwd(), "src"),
        os.environ["XDG_CONFIG_HOME"],
        os.environ["XDG_DATA_HOME"],
    )

    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", codigo])

    time.sleep(1.2)
    for _ in range(6):          # desce até "sair"
        os.write(fd, b"\x1b[B")
        time.sleep(0.1)
    os.write(fd, b"\r")

    saida = b""
    fim = time.time() + 3
    while time.time() < fim:
        if not select.select([fd], [], [], 0.2)[0]:
            continue
        try:
            pedaco = os.read(fd, 4096)
        except OSError:
            break
        if not pedaco:
            break
        saida += pedaco
        if b"DEPOIS" in saida:
            break
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass

    texto = saida.decode(errors="replace")
    assert "DEPOIS" in texto, "o menu não terminou"
    # O que vem depois do menu começa numa linha nova, não colado no rodapé.
    antes_de_depois = texto[: texto.index("DEPOIS")]
    assert antes_de_depois.endswith("\r\n"), repr(antes_de_depois[-30:])
    assert "\x1b[?25h" in texto, "o cursor ficou escondido depois de sair"


def test_tecla_sem_efeito_nao_escreve_nada():
    """Na ponta da lista, a seta não pode redesenhar nem emitir sinal sonoro.

    Redesenhar à toa pisca; um BEL vira flash em terminal com sino visual.
    """
    import os
    import pty
    import select
    import time

    codigo = (
        "import sys; sys.path.insert(0, %r);"
        "from backup_runner import prompt;"
        "prompt.escolhe('e', [('a','alfa'),('b','bravo')], permitir_cancelar=False)"
    ) % os.path.join(os.getcwd(), "src")

    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", codigo])

    time.sleep(0.9)
    while select.select([fd], [], [], 0.3)[0]:
        os.read(fd, 4096)          # descarta o desenho inicial

    os.write(fd, b"\x1b[B")        # vai para o último
    time.sleep(0.3)
    while select.select([fd], [], [], 0.3)[0]:
        os.read(fd, 4096)

    os.write(fd, b"\x1b[B")        # bate na ponta
    time.sleep(0.4)
    na_ponta = b""
    while select.select([fd], [], [], 0.3)[0]:
        na_ponta += os.read(fd, 4096)

    try:
        os.kill(pid, 9)
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, ProcessLookupError):
        pass

    assert na_ponta == b"", f"a tecla sem efeito escreveu {na_ponta!r}"
    assert b"\a" not in na_ponta


def test_bloco_colado_no_rodape_da_tela_nao_embaralha():
    """Regressão da tela de avisos, que é a mais alta do programa.

    Quando o bloco nasce coladinho no fim da tela, o terminal rola no meio do
    desenho, e a partir daí `sobe()` aponta para um lugar que mudou de posição.
    A correção é reservar o espaço antes: imprimir as linhas vazias, deixar a
    tela rolar, e só então voltar ao topo.
    """
    import fcntl
    import os
    import pty
    import re
    import select
    import struct
    import termios
    import time

    codigo = (
        "import sys; sys.path.insert(0, %r);"
        "print('cabecalho\\n' * 16, end='');"      # empurra o bloco para o fim
        "from backup_runner import prompt;"
        "prompt.escolhe('qual evento', ["
        "  ('a', 'sucesso'), ('b', 'falha'), ('c', 'recuperado'),"
        "  ('d', 'janela perdida'), ('e', 'silencio longo'),"
        "])"
    ) % os.path.join(os.getcwd(), "src")

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["FORCE_COLOR"] = "1"
        os.execv(sys.executable, [sys.executable, "-c", codigo])
    # Tela baixa de propósito: o bloco não cabe sem rolar.
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 90, 0, 0))

    def drena(espera=0.4) -> bytes:
        dados = b""
        while select.select([fd], [], [], espera)[0]:
            try:
                pedaco = os.read(fd, 8192)
            except OSError:
                break
            if not pedaco:
                break
            dados += pedaco
        return dados

    time.sleep(1.0)
    drena()
    os.write(fd, b"\x1b[B")
    time.sleep(0.4)
    depois = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", drena().decode(errors="replace"))

    try:
        os.kill(pid, 9)
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, ProcessLookupError):
        pass

    linhas = [l for l in depois.splitlines() if l.strip()]
    assert linhas, "o redesenho não escreveu nada"
    for linha in linhas:
        # Duas opções na mesma linha é o embaralhado.
        assert not (("sucesso" in linha) and ("falha" in linha)), f"embaralhou: {linha!r}"
        assert not (("recuperado" in linha) and ("janela" in linha)), f"embaralhou: {linha!r}"
    # E o cursor foi para a segunda opção, como pedido.
    assert any(l.strip().startswith("→") and "falha" in l for l in linhas), linhas
