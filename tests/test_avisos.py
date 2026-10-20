"""Avisos no Slack: o que sai, quando sai e como aparece.

O webhook nunca é chamado de verdade. O `urlopen` é trocado por um que guarda
o payload, e é o payload que está sob teste: ícone, cor e campos de cada evento.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    yield


@pytest.fixture
def slack(monkeypatch):
    """Liga o canal Slack e devolve a lista de payloads enviados."""
    from backup_runner import notify
    from backup_runner.config import Settings, encrypt

    s = Settings.load()
    s.slack = {"webhook_enc": encrypt("https://hooks.slack.com/services/T/B/x"), "channel": "#backups"}
    s.save()

    enviados: list[dict] = []

    class Resposta:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(requisicao, timeout=None):
        enviados.append(json.loads(requisicao.data))
        return Resposta()

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    return enviados


def _liga(*eventos):
    from backup_runner.config import Settings
    from backup_runner.models import Channel

    s = Settings.load()
    for evento in eventos:
        s.notify_global.set(evento, Channel.SLACK, True)
    s.save()


def _job():
    from backup_runner.models import Job, JobDestination, MySQLSource

    return Job(name="sulminas-app",
               source=MySQLSource(host="127.0.0.1", database="db_prod_sulminas_app"),
               destinations=[JobDestination("s3-sulminas")])


def _run(**kw):
    from backup_runner.models import Run, RunResult

    base = dict(id=12, job="sulminas-app", started_at=dt.datetime(2026, 10, 20, 3, 0, 5),
                result=RunResult.OK, bytes=3_100_000, duration=3.0,
                destinations_done=["s3-sulminas"])
    base.update(kw)
    return Run(**base)


def _texto(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_inicio_nao_avisa_no_padrao_de_fabrica(slack):
    from backup_runner import notify

    notify.sobre_inicio(_job(), _run())
    assert slack == []


def test_inicio_ligado_sai_com_play_e_barra_azul(slack):
    from backup_runner import notify
    from backup_runner.models import NotifyEvent

    _liga(NotifyEvent.START)
    notify.sobre_inicio(_job(), _run(), atrasado=True)

    assert len(slack) == 1
    payload = slack[0]
    assert payload["text"].startswith("▶️ Backup iniciado")
    assert payload["attachments"][0]["color"] == "#3b82f6"
    texto = _texto(payload)
    assert "db_prod_sulminas_app" in texto and "atrasado" in texto and "s3-sulminas" in texto


def test_sucesso_sai_com_check_tamanho_e_pasta(slack):
    from backup_runner import notify
    from backup_runner.models import NotifyEvent

    _liga(NotifyEvent.SUCCESS)
    notify.sobre_execucao(_job(), _run())

    payload = slack[0]
    assert payload["text"].startswith("✅ Backup concluído")
    assert payload["attachments"][0]["color"] == "#22c55e"
    texto = _texto(payload)
    assert "2026-10-20_03-00-05-0300" in texto      # a pasta, com o fuso
    assert "20/10 03:00 (-03:00)" in texto          # a hora, com o fuso
    assert "backup-runner run-info 12" in texto


def test_falha_sai_com_sirene_e_o_diagnostico(slack):
    from backup_runner import notify
    from backup_runner.models import RunResult, Stage

    notify.sobre_execucao(_job(), _run(
        result=RunResult.FAILED, bytes=0, destinations_done=[],
        error_stage=Stage.DUMP, error_got="Access denied for user 'root'",
        error_cause="senha do banco mudou", error_fix="backup-runner job sulminas-app --editar",
    ))

    payload = slack[0]
    assert payload["text"].startswith("🚨 Backup falhou")
    assert payload["attachments"][0]["color"] == "#ef4444"
    texto = _texto(payload)
    assert "senha do banco mudou" in texto and "Como consertar" in texto
    assert "&#39;" not in texto and "Access denied for user 'root'" in texto


def test_texto_do_erro_nao_vira_marcacao_do_slack(slack):
    from backup_runner import notify
    from backup_runner.models import RunResult, Stage

    notify.sobre_execucao(_job(), _run(
        result=RunResult.FAILED, error_stage=Stage.UPLOAD, error_got="<Error> & <Code>",
    ))
    assert "&lt;Error&gt; &amp; &lt;Code&gt;" in _texto(slack[0])


def test_titulo_aparece_uma_vez_so(slack):
    """Sem `blocks` no topo, o Slack mostra o `text` e o título do cartão duplicados."""
    from backup_runner import notify
    from backup_runner.models import NotifyEvent

    _liga(NotifyEvent.SUCCESS)
    notify.sobre_execucao(_job(), _run())
    payload = slack[0]
    assert "Backup concluído" in payload["blocks"][0]["text"]["text"]
    assert "Backup concluído" not in json.dumps(payload["attachments"], ensure_ascii=False)


def test_aviso_de_teste_tambem_sai_como_cartao(slack):
    from backup_runner import notify

    assert notify.teste("slack").ok
    assert slack[0]["blocks"] and slack[0]["attachments"][0]["blocks"]
