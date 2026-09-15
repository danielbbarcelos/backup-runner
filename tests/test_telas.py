"""Teste de fumaça da interface: cada tela abre, navega e não estoura.

Não verifica pixel nem texto exato, que mudariam a cada ajuste de layout.
Verifica o que de fato quebra na prática: tela que não monta, tecla que chama
ação inexistente, e largura estreita que derruba o layout.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def ambiente_com_dados(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from backup_runner.seed import populate

    populate()
    yield


def linhas(app) -> list[str]:
    compositor = app.screen._compositor
    if compositor is None:
        return []
    return [
        "".join(seg.text for seg in strip).rstrip()
        for strip in compositor.render_strips()
    ]


async def _abrir(teclas, tamanho, splash):
    from backup_runner.ui.app import BackupRunnerApp

    app = BackupRunnerApp(skip_splash=not splash)
    async with app.run_test(size=tamanho) as pilot:
        await pilot.pause()
        for tecla in teclas:
            await pilot.press(tecla)
            await pilot.pause()
        # A tela recém-empurrada só ganha compositor depois de um ciclo de
        # layout, então espera até ela ter o que renderizar.
        for _ in range(10):
            if linhas(app):
                break
            await pilot.pause()
        return type(app.screen).__name__, "\n".join(linhas(app))


def tela(teclas=(), tamanho=(100, 32), *, splash=False):
    """Abre o app, aperta as teclas e devolve (tela atual, texto na tela).

    Síncrono de propósito: envolver com asyncio.run evita depender do
    pytest-asyncio só para um punhado de testes de fumaça.
    """
    return asyncio.run(_abrir(list(teclas), tamanho, splash))


def test_dashboard_lista_os_jobs():
    nome, texto = tela([])
    assert nome == "DashboardScreen"
    for job in ("loja_prod", "midia_uploads", "crm_replica", "conta_legado"):
        assert job in texto


def test_dashboard_estreito_vira_abas():
    nome, texto = tela([], tamanho=(80, 32))
    assert "tab alterna aba" in texto


def test_splash_mostra_estado_real():
    """O hero de abertura informa, não só enfeita.

    O conteúdo é verificado direto porque o Textual dispara os timers na hora
    em modo de teste, então a tela sai da pilha antes da captura.
    """
    from backup_runner.ui.app import BackupRunnerApp
    from backup_runner.ui.screens.splash import SplashScreen

    app = BackupRunnerApp(skip_splash=True)
    splash = SplashScreen()
    texto = splash._estado(app.ctx)
    junto = "\n".join(texto)
    assert "cadastrados" in junto
    assert "próxima execução" in junto
    assert "última execução" in junto


def test_splash_tem_o_wordmark():
    from backup_runner.ui.screens.splash import WORDMARK

    assert len(WORDMARK) == 12
    assert all("█" in linha or "╚" in linha for linha in WORDMARK)


def test_historico_abre_e_lista():
    nome, texto = tela(["h"])
    assert nome == "HistoryScreen"
    assert "Histórico" in texto
    # O rótulo do badge precisa vir traduzido, não como chave de i18n.
    assert "badge." not in texto


def test_detalhe_de_execucao_abre_pelo_enter():
    nome, texto = tela(["h", "enter"])
    assert nome == "RunDetailScreen"
    assert "execução #" in texto


def test_execucao_pendente_mostra_o_molde_de_erro():
    nome, texto = tela(["h", "down", "enter"])
    assert nome == "RunDetailScreen"
    for rotulo in ("o que tentei", "o que recebi", "causa provável", "como consertar"):
        assert rotulo in texto


def test_destinos_abre_com_formulario():
    nome, texto = tela(["t"])
    assert nome == "DestinationsScreen"
    assert "local-var" in texto
    assert "retenção padrão" in texto


def test_destino_local_testa_de_verdade(tmp_path):
    """O teste de pasta escreve e apaga uma sonda; não pode fingir sucesso."""
    from backup_runner.models import Destination, DestKind
    from backup_runner.ui.screens.destinations import _testar

    destino = Destination(name="x", kind=DestKind.LOCAL, path=str(tmp_path / "bkp"))
    estado, mensagem, _ = _testar(destino)
    assert estado == "ok"
    assert not list((tmp_path / "bkp").glob(".probe*")), "a sonda precisa ser apagada"


def test_destino_sem_permissao_falha_com_conserto():
    from backup_runner.models import Destination, DestKind
    from backup_runner.ui.screens.destinations import _testar

    destino = Destination(
        name="x", kind=DestKind.LOCAL, path="/proc/impossivel", create_missing=True,
    )
    estado, _, extra = _testar(destino)
    assert estado == "erro"
    assert extra.get("fix")


def test_avisos_mostra_as_duas_linhas():
    nome, texto = tela(["a"])
    assert nome == "NotificationsScreen"
    assert "global" in texto
    assert "sobrescrito" in texto or "= global" in texto


def test_saude_lista_os_itens_com_conserto():
    nome, texto = tela(["s"])
    assert nome == "HealthScreen"
    assert "tick no crontab" in texto
    assert "conserto" in texto


def test_wizard_pede_o_tipo_de_fonte():
    nome, texto = tela(["n"])
    assert "um banco MySQL" in texto
    assert "um diretório" in texto


def test_wizard_de_arquivos_abre_no_passo_um():
    nome, texto = tela(["n", "a"])
    assert nome == "WizardScreen"
    assert "passo 1 de 6" in texto
    assert "origem" in texto


def test_wizard_barra_avanco_sem_nome():
    """Avançar sem nome não pode criar um job anônimo."""
    nome, texto = tela(["n", "a", "enter"])
    assert "passo 1 de 6" in texto


def test_confirmacao_de_apagar_exige_o_nome():
    nome, texto = tela(["d"])
    assert nome == "ConfirmDeleteJob"
    assert "não tem volta" in texto
    assert "digite o nome do job" in texto


def test_ajuda_lista_as_teclas_da_tela():
    nome, texto = tela(["question_mark"])
    assert nome == "HelpScreen"
    assert "Teclas de" in texto


def test_esc_volta_um_nivel_sem_fechar():
    nome, _ = tela(["h"])
    assert nome == "HistoryScreen"
    nome, _ = tela(["h", "escape"])
    assert nome == "DashboardScreen"


def test_dashboard_vazio_ensina_os_passos(monkeypatch, tmp_path):
    from backup_runner.config import JobStore

    store = JobStore()
    store.jobs.clear()
    store.save()

    nome, texto = tela([])
    assert "primeiro backup" in texto
    assert "três passos" in texto
    assert "tick" in texto


# ----------------------------------------------------------------------------
# Regressões de navegação e de área de transferência
# ----------------------------------------------------------------------------

def test_o_item_em_foco_tem_marca_visivel():
    """O destaque não pode depender só de cor de fundo.

    O bug original: o CSS mirava `.--highlight` e o Textual usa `-highlight`,
    então não havia destaque nenhum, e o seletor errado falha em silêncio.
    """
    _, texto = tela([])
    assert "› " in texto, "nenhuma marca de cursor na lista"

    _, depois = tela(["down"])
    assert texto != depois, "descer na lista não mudou nada na tela"


def test_a_classe_de_destaque_do_textual_nao_mudou():
    """Trava o nome da classe que o CSS depende.

    Se uma versão nova do Textual renomear isto, o teste falha aqui em vez de
    a interface ficar sem destaque sem ninguém perceber.
    """
    import asyncio

    from backup_runner.ui.app import BackupRunnerApp
    from backup_runner.ui.screens.dashboard import JobRow

    async def _rodar():
        app = BackupRunnerApp(skip_splash=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            linhas = list(app.screen.query(JobRow))
            destacadas = [l for l in linhas if l.highlighted]
            assert len(destacadas) == 1, "deveria haver exatamente um item em foco"
            assert destacadas[0].has_class("-highlight"), (
                "o Textual mudou o nome da classe de destaque; o CSS precisa acompanhar"
            )

    asyncio.run(_rodar())


def test_marca_de_foco_nas_tres_listas():
    for tecla, tela_esperada in [("t", "DestinationsScreen"), ("s", "HealthScreen")]:
        nome, texto = tela([tecla])
        assert nome == tela_esperada
        assert "› " in texto, f"sem marca de cursor em {tela_esperada}"


def test_clipboard_nao_mente_sobre_o_resultado():
    """Copiar precisa confirmar, não supor.

    O bug original: o xclip continua vivo servindo a seleção, então capturar a
    saída esperava até o timeout e a função reportava falha mesmo tendo
    copiado. O contrário é pior ainda: dizer "copiado" sem ter copiado.
    """
    from backup_runner.clipboard import copy, disponivel

    resultado = copy("backup-runner tick --install")
    if disponivel():
        assert resultado.ok, f"há ferramenta ({disponivel()}) e a cópia falhou: {resultado.erro}"
        assert resultado.via
    else:
        assert not resultado.ok
        assert resultado.sugestao, "sem ferramenta, precisa dizer como instalar"


def test_clipboard_recusa_texto_vazio():
    from backup_runner.clipboard import copy

    assert not copy("").ok
