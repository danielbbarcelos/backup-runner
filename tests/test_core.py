"""Testes das partes que rodam sem ninguém olhando.

O foco é o que decide sozinho às três da manhã: o cron, a resolução de tabelas
ignoradas, a idempotência da fila e a política de janela perdida. A interface
é verificada rodando o app; estes aqui protegem as regras.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def ambiente_isolado(monkeypatch, tmp_path):
    """Cada teste escreve num XDG próprio, nunca na configuração real."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    yield


# ----------------------------------------------------------------------------
# Cron
# ----------------------------------------------------------------------------

def test_cron_diario():
    from backup_runner.schedule import Cron

    cron = Cron.parse("0 3 * * *")
    agora = dt.datetime(2026, 9, 14, 20, 30)
    assert cron.next_after(agora) == dt.datetime(2026, 9, 15, 3, 0)
    assert cron.prev_before(agora) == dt.datetime(2026, 9, 14, 3, 0)


def test_cron_dias_uteis():
    from backup_runner.schedule import Cron

    cron = Cron.parse("30 2 * * 1-5")
    # Sábado 19/09/2026: o próximo é segunda.
    assert cron.next_after(dt.datetime(2026, 9, 19, 10, 0)) == dt.datetime(2026, 9, 21, 2, 30)


def test_cron_passo():
    from backup_runner.schedule import Cron

    cron = Cron.parse("*/15 * * * *")
    assert cron.next_after(dt.datetime(2026, 9, 14, 20, 31)) == dt.datetime(2026, 9, 14, 20, 45)


def test_cron_invalido():
    from backup_runner.schedule import CronError, Cron, is_valid

    assert not is_valid("0 3 * *")
    assert not is_valid("99 3 * * *")
    with pytest.raises(CronError):
        Cron.parse("a b c d e")


def test_cron_dia_ou_semana():
    """Com dia e dia-da-semana restritos, casa se qualquer um casar."""
    from backup_runner.schedule import Cron

    cron = Cron.parse("0 0 1 * 0")
    assert cron.matches(dt.datetime(2026, 9, 1, 0, 0))    # dia 1, terça
    assert cron.matches(dt.datetime(2026, 9, 20, 0, 0))   # domingo
    assert not cron.matches(dt.datetime(2026, 9, 15, 0, 0))


# ----------------------------------------------------------------------------
# Tabelas ignoradas
# ----------------------------------------------------------------------------

def test_regex_reavaliada_pega_tabela_nova():
    """Regex reavaliada a cada execução: tabela criada depois do cadastro já nasce ignorada."""
    from backup_runner.models import MySQLSource

    fonte = MySQLSource(ignore_regex=r"_[0-9]{6}$")
    tabelas = ["pedidos", "pedidos_202603", "pedidos_202607"]
    por_regex, por_mao = fonte.resolve_ignored(tabelas)
    assert por_regex == ["pedidos_202603", "pedidos_202607"]
    assert por_mao == []


def test_marca_a_mao_vence_a_regex_nos_dois_sentidos():
    from backup_runner.models import MySQLSource

    fonte = MySQLSource(
        ignore_regex=r"_log$",
        ignore_manual=["clientes"],   # não casa a regex, mas foi marcada
        keep_manual=["auditoria_log"],  # casa a regex, mas foi resgatada
    )
    por_regex, por_mao = fonte.resolve_ignored(
        ["clientes", "acesso_log", "auditoria_log", "pedidos"]
    )
    assert por_mao == ["clientes"]
    assert por_regex == ["acesso_log"]
    assert "auditoria_log" not in por_regex + por_mao


def test_padrao_ignora_telescope_e_pulse_e_nada_parecido():
    from backup_runner.models import MySQLSource

    fonte = MySQLSource(database="x")
    tabelas = ["telescope_entries", "telescope_entries_tags", "telescope_monitoring",
               "pulse_entries", "pulse_aggregates", "pulse_values",
               "telescopes", "my_pulse_entries", "clientes", "logs_202610"]
    por_regex, _ = fonte.resolve_ignored(tabelas)
    # Tabela mensal entra com dados desde a 0.8.2.
    assert por_regex == sorted(["telescope_entries", "telescope_entries_tags", "telescope_monitoring",
                                "pulse_entries", "pulse_aggregates", "pulse_values"])


def test_job_com_o_padrao_antigo_ganha_o_novo_e_o_personalizado_fica():
    from backup_runner.models import DEFAULT_IGNORE_REGEX, MySQLSource

    for antigo in (r"_[0-9]{6}$|_(19|20)[0-9]{2}(0[1-9]|1[0-2])(_v[0-9]+)?$",
                   r"_[0-9]{6}$|_(19|20)[0-9]{2}(0[1-9]|1[0-2])(_v[0-9]+)?$|^telescope_|^pulse_"):
        assert MySQLSource.from_dict({"ignore_regex": antigo}).ignore_regex == DEFAULT_IGNORE_REGEX
    assert MySQLSource.from_dict({"ignore_regex": r"_log$"}).ignore_regex == r"_log$"
    assert MySQLSource.from_dict({"ignore_regex": ""}).ignore_regex == ""


def test_regex_quebrada_nao_derruba():
    """Regex inválida ignora a regra em vez de estourar no meio do dump."""
    from backup_runner.models import MySQLSource

    fonte = MySQLSource(ignore_regex="[nao fecha")
    por_regex, por_mao = fonte.resolve_ignored(["a", "b"])
    assert por_regex == [] and por_mao == []


# ----------------------------------------------------------------------------
# Fila
# ----------------------------------------------------------------------------

def test_fila_nao_duplica_a_mesma_janela():
    """O tick roda a cada minuto; enfileirar a mesma janela duas vezes seria
    dois dumps do mesmo banco na mesma noite."""
    from backup_runner.state import State

    estado = State()
    janela = dt.datetime(2026, 9, 14, 3, 0)
    assert estado.enqueue("loja", janela) is not None
    assert estado.enqueue("loja", janela) is None
    assert estado.queue_size() == 1
    estado.close()


def test_claim_entrega_um_item_por_vez():
    from backup_runner.state import State

    estado = State()
    estado.enqueue("a", dt.datetime(2026, 9, 14, 3, 0))
    estado.enqueue("b", dt.datetime(2026, 9, 14, 4, 0))

    primeiro = estado.claim_next()
    segundo = estado.claim_next()
    assert primeiro["job"] == "a"      # ordem pela janela, não pela inserção
    assert segundo["job"] == "b"
    assert estado.claim_next() is None
    assert estado.queue_size() == 0
    estado.close()


# ----------------------------------------------------------------------------
# Tick
# ----------------------------------------------------------------------------

def _job(nome: str, schedule: str = "0 3 * * *", **kwargs):
    from backup_runner.models import Job, MySQLSource

    return Job(name=nome, source=MySQLSource(database="x"), schedule=schedule, **kwargs)


def test_tick_enfileira_dentro_da_janela():
    from backup_runner.config import JobStore
    from backup_runner.state import State
    from backup_runner.tick import run_tick

    store = JobStore()
    store.jobs["loja"] = _job("loja", catch_up_window_minutes=360)
    store.save()

    estado = State()
    # 08:00, janela das 03:00, cinco horas de atraso: dentro das seis aceitas.
    resultado = run_tick(dt.datetime(2026, 9, 14, 8, 0), state=estado)
    assert [nome for nome, _, _ in resultado.enfileirados] == ["loja"]
    assert resultado.enfileirados[0][2] is True   # marcado como atrasado
    assert resultado.perdidos == []
    estado.close()


def test_tick_marca_perdida_fora_da_janela():
    from backup_runner.config import JobStore
    from backup_runner.models import RunResult
    from backup_runner.state import State
    from backup_runner.tick import run_tick

    store = JobStore()
    store.jobs["loja"] = _job("loja", catch_up_window_minutes=360)
    store.save()

    estado = State()
    # 10:00, sete horas depois da janela: passou das seis, não roda.
    resultado = run_tick(dt.datetime(2026, 9, 14, 10, 0), state=estado)
    assert resultado.enfileirados == []
    assert [nome for nome, _ in resultado.perdidos] == ["loja"]
    assert estado.queue_size() == 0

    registradas = estado.runs(job="loja", results=[RunResult.MISSED])
    assert len(registradas) == 1

    # Rodar de novo não registra a mesma perda duas vezes.
    run_tick(dt.datetime(2026, 9, 14, 10, 1), state=estado)
    assert len(estado.runs(job="loja", results=[RunResult.MISSED])) == 1
    estado.close()


def test_tick_pula_job_pausado():
    from backup_runner.config import JobStore
    from backup_runner.state import State
    from backup_runner.tick import run_tick

    store = JobStore()
    store.jobs["loja"] = _job("loja", enabled=False)
    store.save()

    estado = State()
    resultado = run_tick(dt.datetime(2026, 9, 14, 3, 1), state=estado)
    assert resultado.enfileirados == []
    assert resultado.pulados == ["loja"]
    estado.close()


def test_tick_nao_repete_janela_ja_executada():
    from backup_runner.config import JobStore
    from backup_runner.models import Run, RunResult
    from backup_runner.state import State
    from backup_runner.tick import run_tick

    store = JobStore()
    store.jobs["loja"] = _job("loja")
    store.save()

    estado = State()
    estado.insert_run(Run(
        id=0, job="loja",
        started_at=dt.datetime(2026, 9, 14, 3, 0),
        finished_at=dt.datetime(2026, 9, 14, 3, 5),
        result=RunResult.OK, bytes=10, duration=300.0,
    ))
    resultado = run_tick(dt.datetime(2026, 9, 14, 3, 30), state=estado)
    assert resultado.enfileirados == []
    estado.close()


def _relogio_do_programa(fuso: str) -> None:
    from backup_runner.config import Settings

    s = Settings.load()
    s.timezone = fuso
    s.save()


def test_tick_le_o_cron_no_fuso_do_job():
    """Programa em UTC e job em São Paulo: 03:00 do job é 06:00 do programa.

    Foi o que aconteceu na Sul Minas: o campo `timezone` era ignorado e o
    backup das três da manhã rodava à meia-noite de Brasília.
    """
    from backup_runner.config import JobStore
    from backup_runner.state import State
    from backup_runner.tick import run_tick

    _relogio_do_programa("UTC")
    store = JobStore()
    store.jobs["loja"] = _job("loja", timezone="America/Sao_Paulo")
    store.save()

    estado = State()
    # 03:01 UTC é 00:01 em São Paulo: a janela das 03:00 de hoje ainda não chegou
    # e a de ontem já passou das seis horas de tolerância.
    resultado = run_tick(dt.datetime(2026, 10, 20, 3, 1), state=estado)
    assert resultado.enfileirados == []

    resultado = run_tick(dt.datetime(2026, 10, 20, 6, 1), state=estado)
    assert [(nome, janela) for nome, janela, _ in resultado.enfileirados] == [
        ("loja", dt.datetime(2026, 10, 20, 6, 0)),
    ]
    estado.close()


def test_tick_job_sem_fuso_ou_com_fuso_invalido_segue_o_programa():
    from backup_runner.config import JobStore
    from backup_runner.state import State
    from backup_runner.tick import run_tick

    _relogio_do_programa("UTC")
    store = JobStore()
    store.jobs["vazio"] = _job("vazio", timezone="")
    store.jobs["errado"] = _job("errado", timezone="America/Nao_Existe")
    store.save()

    estado = State()
    resultado = run_tick(dt.datetime(2026, 10, 20, 3, 1), state=estado)
    assert sorted(nome for nome, _, _ in resultado.enfileirados) == ["errado", "vazio"]
    estado.close()


def test_proxima_execucao_respeita_o_fuso():
    from backup_runner.schedule import next_run, previous_run

    _relogio_do_programa("UTC")
    agora = dt.datetime(2026, 10, 20, 12, 0)
    assert next_run("0 3 * * *", agora, tz="America/Sao_Paulo") == dt.datetime(2026, 10, 21, 6, 0)
    assert previous_run("0 3 * * *", agora, tz="America/Sao_Paulo") == dt.datetime(2026, 10, 20, 6, 0)
    assert next_run("0 3 * * *", agora) == dt.datetime(2026, 10, 21, 3, 0)


def test_relogio_do_programa_ignora_o_fuso_do_servidor(relogio_do_servidor):
    """Servidor em UTC, programa em São Paulo: a hora do histórico é a de Brasília."""
    from backup_runner import relogio

    relogio_do_servidor("UTC")
    _relogio_do_programa("America/Sao_Paulo")
    diferenca = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - relogio.agora()
    assert abs(diferenca - dt.timedelta(hours=3)) < dt.timedelta(minutes=1)


def test_relogio_vazio_usa_o_do_servidor(relogio_do_servidor):
    from backup_runner import relogio

    relogio_do_servidor("UTC")
    _relogio_do_programa("")
    assert abs(dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - relogio.agora()) < dt.timedelta(minutes=1)


def test_pasta_leva_o_deslocamento_e_volta_igual():
    from backup_runner import relogio

    _relogio_do_programa("America/Sao_Paulo")
    inicio = dt.datetime(2026, 10, 20, 3, 0, 5)
    assert relogio.nome_pasta(inicio) == "2026-10-20_03-00-05-0300"
    assert relogio.parse_pasta("2026-10-20_03-00-05-0300") == inicio


def test_pasta_de_outro_relogio_e_convertida():
    """Pasta gravada em UTC é lida na hora de Brasília, então a retenção conta certo."""
    from backup_runner import relogio

    _relogio_do_programa("America/Sao_Paulo")
    assert relogio.parse_pasta("2026-10-20_06-00-05+0000") == dt.datetime(2026, 10, 20, 3, 0, 5)


def test_pasta_antiga_sem_deslocamento_ainda_e_lida():
    from backup_runner import relogio

    assert relogio.parse_pasta("2026-10-20_03-00-05") == dt.datetime(2026, 10, 20, 3, 0, 5)
    assert relogio.parse_pasta("staging") is None


# ----------------------------------------------------------------------------
# Segredos
# ----------------------------------------------------------------------------

def test_segredo_ida_e_volta_e_chave_privada():
    from backup_runner.config import decrypt, encrypt, key_file

    cifra = encrypt("senha-do-banco")
    assert cifra != "senha-do-banco"
    assert decrypt(cifra) == "senha-do-banco"

    modo = os.stat(key_file()).st_mode & 0o777
    assert modo == 0o600, "a chave precisa ficar ilegível para outros usuários"


def test_decrypt_de_lixo_devolve_none():
    """Chave trocada ou arquivo corrompido não pode derrubar a interface."""
    from backup_runner.config import decrypt

    assert decrypt("isto-nao-e-fernet") is None


def test_config_nao_guarda_segredo_em_claro():
    from backup_runner.config import DestinationStore, destinations_file, encrypt
    from backup_runner.models import Destination, DestKind

    store = DestinationStore()
    store.destinations["spaces"] = Destination(
        name="spaces", kind=DestKind.S3, secret_enc=encrypt("super-secreto"),
    )
    store.save()
    conteudo = destinations_file().read_text()
    assert "super-secreto" not in conteudo


# ----------------------------------------------------------------------------
# Nomenclatura e retenção
# ----------------------------------------------------------------------------

def test_pasta_da_execucao_nao_tem_espaco_nem_dois_pontos():
    """Espaço vira %20 em chave S3 e dois-pontos quebram scp e rsync."""
    from backup_runner.models import Run, RunResult

    run = Run(id=1, job="loja", started_at=dt.datetime(2026, 9, 14, 3, 0, 0), result=RunResult.OK)
    assert run.folder == "2026-09-14_03-00-00-0300"
    assert " " not in run.folder and ":" not in run.folder


def test_ordem_alfabetica_das_pastas_bate_com_a_cronologica():
    from backup_runner.models import Run, RunResult

    pastas = [
        Run(id=i, job="x", started_at=quando, result=RunResult.OK).folder
        for i, quando in enumerate([
            dt.datetime(2026, 9, 14, 3, 0),
            dt.datetime(2026, 9, 14, 13, 0),
            dt.datetime(2026, 10, 1, 3, 0),
            dt.datetime(2027, 1, 1, 3, 0),
        ])
    ]
    assert pastas == sorted(pastas)


def test_retencao_do_job_sobrescreve_a_do_destino():
    from backup_runner.models import Destination, DestKind, JobDestination

    destino = Destination(name="spaces", kind=DestKind.S3, retention_days=30)
    assert JobDestination("spaces").days(destino) == 30
    assert JobDestination("spaces", 7).days(destino) == 7


# ----------------------------------------------------------------------------
# Avisos
# ----------------------------------------------------------------------------

def test_padrao_global_nao_avisa_sucesso():
    """Silêncio precisa significar sucesso, senão o alerta vira ruído."""
    from backup_runner.models import Channel, NotifyEvent, NotifyMatrix

    padrao = NotifyMatrix.default_global()
    assert padrao.get(NotifyEvent.SUCCESS, Channel.EMAIL) is False
    assert padrao.get(NotifyEvent.FAILURE, Channel.EMAIL) is True
    assert padrao.get(NotifyEvent.STALE, Channel.SLACK) is True


def test_celula_ausente_herda_do_global():
    from backup_runner.models import Channel, NotifyEvent, NotifyMatrix

    padrao = NotifyMatrix.default_global()
    do_job = NotifyMatrix()
    assert do_job.get(NotifyEvent.FAILURE, Channel.EMAIL) is None
    assert do_job.resolve(NotifyEvent.FAILURE, Channel.EMAIL, padrao) is True

    do_job.set(NotifyEvent.FAILURE, Channel.EMAIL, False)
    assert do_job.resolve(NotifyEvent.FAILURE, Channel.EMAIL, padrao) is False
    assert do_job.overrides(padrao) == 1

    do_job.clear(NotifyEvent.FAILURE, Channel.EMAIL)
    assert do_job.resolve(NotifyEvent.FAILURE, Channel.EMAIL, padrao) is True


# ----------------------------------------------------------------------------
# Serialização
# ----------------------------------------------------------------------------

def test_job_sobrevive_a_ida_e_volta_no_disco():
    from backup_runner.config import JobStore
    from backup_runner.models import (
        ArchiveFormat, Channel, ExcludePattern, FilesSource, Job,
        JobDestination, NotifyEvent,
    )

    job = Job(
        name="midia",
        source=FilesSource(
            path="/srv/uploads",
            excludes=[ExcludePattern("*.tmp", True), ExcludePattern("cache/**", False)],
            archive_format=ArchiveFormat.ZIP,
        ),
        schedule="30 3 * * *",
        destinations=[JobDestination("local", 5)],
        catch_up_window_minutes=120,
        timeout_minutes=90,
    )
    job.notify.set(NotifyEvent.SUCCESS, Channel.SLACK, True)

    store = JobStore()
    store.put(job)

    recarregado = JobStore.load().get("midia")
    assert recarregado is not None
    assert recarregado.source.path == "/srv/uploads"
    assert recarregado.source.archive_format is ArchiveFormat.ZIP
    assert recarregado.source.active_excludes() == ["*.tmp"]
    assert recarregado.destinations[0].retention_days == 5
    assert recarregado.catch_up_window_minutes == 120
    assert recarregado.notify.get(NotifyEvent.SUCCESS, Channel.SLACK) is True


def test_execucao_sobrevive_a_ida_e_volta_no_banco():
    from backup_runner.models import (
        ManifestEntry, Run, RunResult, Stage, StageRecord, StageState,
    )
    from backup_runner.state import State

    estado = State()
    run = Run(
        id=0, job="loja",
        started_at=dt.datetime(2026, 9, 14, 3, 0),
        finished_at=dt.datetime(2026, 9, 14, 3, 4),
        result=RunResult.PENDING_UPLOAD,
        bytes=2_100_000_000, duration=252.0,
        stages=[StageRecord(Stage.DUMP, StageState.DONE, "dump", "186 tabelas", 158.0)],
        manifest=[ManifestEntry("local", "abc123", 2_100_000_000)],
        ignored_regex=["a_log"], ignored_manual=["cart_log"],
        destinations_done=["local"], destinations_pending=["spaces"],
        error_got="403",
    )
    run_id = estado.insert_run(run)

    lido = estado.get_run(run_id)
    assert lido is not None
    assert lido.result is RunResult.PENDING_UPLOAD
    assert lido.stages[0].state is StageState.DONE
    assert lido.manifest[0].sha256 == "abc123"
    assert lido.ignored_total == 2
    assert lido.destinations_pending == ["spaces"]
    estado.close()


# ----------------------------------------------------------------------------
# Formatação
# ----------------------------------------------------------------------------

def test_formatacao_usa_virgula_decimal():
    from backup_runner.format import format_bytes, format_count, format_duration

    assert format_bytes(2_100_000_000) == "2,0 GB"
    assert format_bytes(None) == "·"
    assert format_duration(252) == "4min 12s"
    assert format_duration(None) == "·"
    assert format_count(12481) == "12.481"


def test_cada_resultado_tem_simbolo_proprio():
    """Cor é reforço: o símbolo precisa distinguir sozinho.

    Em terminal sem cor, com NO_COLOR, ou num pipe, é só o símbolo que resta.
    """
    from backup_runner import views
    from backup_runner.models import RunResult

    simbolos = set()
    for resultado in RunResult:
        simbolo, rotulo, _ = views.BADGE[resultado]
        assert rotulo and not rotulo.startswith("badge.")
        simbolos.add(simbolo)
    assert len(simbolos) >= 5
