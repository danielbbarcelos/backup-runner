"""Avisos por email e Slack.

O padrão é o que a matriz diz: sucesso não avisa, falha avisa. Silêncio precisa
significar sucesso, senão o alerta vira ruído e a pessoa para de ler justamente
o que importa.

Nada aqui derruba um backup. Um SMTP fora do ar, um webhook expirado ou uma
rede caída viram uma linha no log da execução, e o artefato continua no destino
onde já chegou.
"""
from __future__ import annotations

import datetime as dt
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage

from .config import Settings, decrypt
from .format import format_bytes, format_duration
from .models import Channel, Job, NotifyEvent, Run, RunResult


@dataclass
class Envio:
    canal: str
    ok: bool
    detalhe: str = ""


def evento_de(run: Run, *, recuperado: bool = False) -> NotifyEvent | None:
    """Qual evento esta execução representa, na linguagem da matriz."""
    if recuperado:
        return NotifyEvent.RECOVERED
    if run.result is RunResult.MISSED:
        return NotifyEvent.MISSED
    if run.result in (RunResult.FAILED, RunResult.PENDING_UPLOAD):
        return NotifyEvent.FAILURE
    if run.result in (RunResult.OK, RunResult.LATE):
        return NotifyEvent.SUCCESS
    return None


def sobre_execucao(job: Job, run: Run, *, recuperado: bool = False) -> list[Envio]:
    evento = evento_de(run, recuperado=recuperado)
    if evento is None:
        return []
    return despacha(job, evento, *_mensagem(job, run, evento))


def despacha(job: Job | None, evento: NotifyEvent, assunto: str, corpo: str) -> list[Envio]:
    settings = Settings.load()
    padrao = settings.notify_global
    matriz = job.notify if job is not None else padrao

    enviados = []
    for canal in (Channel.EMAIL, Channel.SLACK):
        if not settings.channel_configured(canal.value):
            continue
        if not matriz.resolve(evento, canal, padrao):
            continue
        if canal is Channel.EMAIL:
            enviados.append(_email(settings, assunto, corpo))
        else:
            enviados.append(_slack(settings, assunto, corpo))
    return enviados


# ----------------------------------------------------------------------------
# Texto
# ----------------------------------------------------------------------------

def _mensagem(job: Job, run: Run, evento: NotifyEvent) -> tuple[str, str]:
    quando = run.started_at.strftime("%d/%m %H:%M")
    marcas = {
        NotifyEvent.SUCCESS: "ok",
        NotifyEvent.FAILURE: "FALHA",
        NotifyEvent.RECOVERED: "recuperado",
        NotifyEvent.MISSED: "janela perdida",
        NotifyEvent.STALE: "sem backup",
    }
    assunto = f"[backup-runner] {marcas[evento]}: {job.name} {quando}"

    linhas = [
        f"job        {job.name}",
        f"quando     {quando}",
        f"resultado  {run.result.value}",
    ]
    if run.bytes:
        linhas.append(f"tamanho    {format_bytes(run.bytes)}")
    if run.duration:
        linhas.append(f"duração    {format_duration(run.duration)}")
    if run.destinations_done:
        linhas.append(f"enviado    {', '.join(run.destinations_done)}")
    if run.destinations_pending:
        linhas.append(f"pendente   {', '.join(run.destinations_pending)}")

    if run.error_stage:
        # O mesmo molde de quatro linhas que a interface usa, para quem lê o
        # email não precisar aprender um segundo formato.
        linhas += ["", f"falhou no estágio {run.error_stage.value}"]
        for rotulo, valor in (
            ("o que tentei", run.error_tried),
            ("o que recebi", run.error_got),
            ("causa provável", run.error_cause),
            ("como consertar", run.error_fix),
        ):
            if valor:
                linhas.append(f"{rotulo:<16} {valor}")

    if run.result is RunResult.PENDING_UPLOAD:
        linhas += [
            "",
            "o artefato está no staging e o worker tenta de novo sozinho.",
            f"para forçar agora: backup-runner retry {run.id}",
        ]
    linhas += ["", f"detalhe: backup-runner run-info {run.id}"]
    return assunto, "\n".join(linhas)


def sobre_silencio(job: Job, horas: int, ultima: dt.datetime | None) -> list[Envio]:
    """O aviso que nenhuma execução consegue emitir, porque não houve execução."""
    quando = ultima.strftime("%d/%m %H:%M") if ultima else "nunca"
    assunto = f"[backup-runner] sem backup: {job.name} há mais de {horas}h"
    corpo = "\n".join([
        f"job              {job.name}",
        f"último sucesso   {quando}",
        f"limite aceito    {horas}h",
        "",
        "isto costuma ser o tick fora do crontab, o worker parado, ou o job",
        "pausado sem querer. o diagnóstico responde qual dos três:",
        "",
        "  backup-runner health",
    ])
    return despacha(job, NotifyEvent.STALE, assunto, corpo)


# ----------------------------------------------------------------------------
# Canais
# ----------------------------------------------------------------------------

def _email(settings: Settings, assunto: str, corpo: str) -> Envio:
    smtp = settings.smtp
    mensagem = EmailMessage()
    mensagem["Subject"] = assunto
    mensagem["From"] = smtp.get("from") or smtp.get("user") or "backup-runner@localhost"
    mensagem["To"] = smtp.get("to", "")
    mensagem.set_content(corpo)

    host = smtp.get("host", "")
    porta = int(smtp.get("port", 587) or 587)
    usuario = smtp.get("user") or ""
    senha = decrypt(smtp.get("password_enc")) or ""

    try:
        if porta == 465:
            with smtplib.SMTP_SSL(host, porta, timeout=20, context=ssl.create_default_context()) as s:
                if usuario:
                    s.login(usuario, senha)
                s.send_message(mensagem)
        else:
            with smtplib.SMTP(host, porta, timeout=20) as s:
                s.ehlo()
                if s.has_extn("starttls"):
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if usuario:
                    s.login(usuario, senha)
                s.send_message(mensagem)
    except Exception as exc:  # noqa: BLE001
        return Envio("email", False, str(exc)[:200])
    return Envio("email", True, smtp.get("to", ""))


def _slack(settings: Settings, assunto: str, corpo: str) -> Envio:
    webhook = decrypt(settings.slack.get("webhook_enc"))
    if not webhook:
        return Envio("slack", False, "webhook não configurado")

    payload = json.dumps({
        "text": f"*{assunto}*\n```{corpo}```",
    }).encode()
    requisicao = urllib.request.Request(
        webhook, data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(requisicao, timeout=15) as resposta:
            if resposta.status >= 300:
                return Envio("slack", False, f"HTTP {resposta.status}")
    except urllib.error.HTTPError as exc:
        return Envio("slack", False, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        return Envio("slack", False, str(exc)[:200])
    return Envio("slack", True, settings.slack.get("channel", ""))


def teste(canal: str) -> Envio:
    """Manda uma mensagem de teste, para conferir a configuração."""
    settings = Settings.load()
    assunto = "[backup-runner] teste de aviso"
    corpo = "Se você está lendo isto, o canal está configurado corretamente."
    if canal == "email":
        return _email(settings, assunto, corpo)
    return _slack(settings, assunto, corpo)
