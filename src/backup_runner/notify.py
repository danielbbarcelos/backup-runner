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
import socket
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage

from . import __version__, relogio
from .config import Settings, decrypt
from .format import format_bytes, format_duration
from .models import Channel, FilesMode, Job, NotifyEvent, Run, RunResult
from .schedule import humanize


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
    assunto, corpo = _mensagem(job, run, evento)
    return despacha(job, evento, assunto, corpo, slack=_cartao_execucao(job, run, evento))


def sobre_inicio(job: Job, run: Run, *, atrasado: bool = False) -> list[Envio]:
    """O aviso de que o backup começou. Desligado no padrão de fábrica."""
    quando = _quando(run.started_at)
    assunto = f"[backup-runner] início: {job.name} {run.started_at:%d/%m %H:%M}"
    destinos = ", ".join(d.name for d in job.destinations) or "nenhum"
    corpo = "\n".join([
        f"job        {job.name}",
        f"origem     {job.source.target()}",
        f"início     {quando}" + (" (atrasado)" if atrasado else ""),
        f"destinos   {destinos}",
    ])
    campos = [
        ("Origem", f"`{_esc(job.source.target())}`"),
        ("Início", quando + (" · atrasado" if atrasado else "")),
        ("Destinos", _esc(destinos)),
        ("Agenda", _esc(humanize(job.schedule))),
    ]
    cartao = _cartao(NotifyEvent.START, job.name, campos, rodape_extra=f"execução {run.id}")
    return despacha(job, NotifyEvent.START, assunto, corpo, slack=cartao)


def despacha(job: Job | None, evento: NotifyEvent, assunto: str, corpo: str,
             *, slack: dict | None = None) -> list[Envio]:
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
            enviados.append(_slack(settings, assunto, corpo, payload=slack))
    return enviados


# ----------------------------------------------------------------------------
# Texto
# ----------------------------------------------------------------------------

def _mensagem(job: Job, run: Run, evento: NotifyEvent) -> tuple[str, str]:
    quando = run.started_at.strftime("%d/%m %H:%M")
    marcas = {
        NotifyEvent.START: "início",
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
    campos = [
        ("Último sucesso", _quando(ultima) if ultima else "nunca"),
        ("Limite aceito", f"{horas} h"),
    ]
    cartao = _cartao(
        NotifyEvent.STALE, job.name, campos,
        titulo=f"Sem backup há mais de {horas} h",
        detalhe=("Costuma ser o tick fora do crontab, o worker parado ou o job pausado "
                 "sem querer. O diagnóstico responde qual dos três: `backup-runner health`"),
    )
    return despacha(job, NotifyEvent.STALE, assunto, corpo, slack=cartao)


# ----------------------------------------------------------------------------
# Cartão do Slack
# ----------------------------------------------------------------------------

# Ícone, cor da barra lateral e título de cada evento. A cor é o que se lê de
# relance numa coluna de avisos; o ícone é o que aparece na notificação do
# celular, onde a cor não chega.
ESTILO: dict[NotifyEvent, tuple[str, str, str]] = {
    NotifyEvent.START: ("▶️", "#3b82f6", "Backup iniciado"),
    NotifyEvent.SUCCESS: ("✅", "#22c55e", "Backup concluído"),
    NotifyEvent.RECOVERED: ("✅", "#22c55e", "Backup voltou a funcionar"),
    NotifyEvent.FAILURE: ("🚨", "#ef4444", "Backup falhou"),
    NotifyEvent.MISSED: ("⚠️", "#f59e0b", "Janela perdida"),
    NotifyEvent.STALE: ("⏰", "#f59e0b", "Sem backup recente"),
}


def _esc(texto: str) -> str:
    """O Slack lê `&`, `<` e `>` como marcação."""
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quando(momento: dt.datetime) -> str:
    return f"{momento:%d/%m %H:%M} ({relogio.rotulo_fuso(momento)})"


def _cartao(evento: NotifyEvent, job: str, campos: list[tuple[str, str]], *,
            titulo: str | None = None, detalhe: str | None = None,
            rodape_extra: str | None = None) -> dict:
    icone, cor, titulo_padrao = ESTILO[evento]
    titulo = titulo or titulo_padrao
    # O título vai em `blocks`, fora da barra colorida. Com `blocks` presente o
    # Slack usa o `text` só na notificação; sem ele, mostra o `text` e o título
    # do cartão um embaixo do outro.
    cabecalho = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{icone} *{titulo}*  ·  *{_esc(job)}*"}},
    ]
    blocos: list[dict] = []
    visiveis = [(r, v) for r, v in campos if v][:10]   # o Slack aceita dez campos por seção
    if visiveis:
        blocos.append({
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": f"*{r}*\n{v}"} for r, v in visiveis],
        })
    if detalhe:
        blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": detalhe}})
    rodape = [f"🖥️ {_esc(socket.gethostname())}", f"backup-runner {__version__}"]
    if rodape_extra:
        rodape.append(rodape_extra)
    blocos.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(rodape)}]})
    return {
        # `text` é o que aparece na notificação e em cliente sem suporte a blocos.
        "text": f"{icone} {titulo}: {job}",
        "blocks": cabecalho,
        "attachments": [{"color": cor, "blocks": blocos}],
    }


def _cartao_execucao(job: Job, run: Run, evento: NotifyEvent) -> dict:
    titulo = None
    if run.result is RunResult.PENDING_UPLOAD:
        titulo = "Backup pronto, envio pendente"

    campos = [
        ("Origem", f"`{_esc(job.source.target())}`"),
        ("Início", _quando(run.started_at)),
    ]
    if run.duration:
        campos.append(("Duração", format_duration(run.duration)))
    if run.bytes:
        campos.append(("Tamanho", format_bytes(run.bytes)))
    if run.destinations_done:
        campos.append(("Enviado para", _esc(", ".join(run.destinations_done))))
    if run.destinations_pending:
        campos.append(("Pendente", _esc(", ".join(run.destinations_pending))))
    if evento in (NotifyEvent.SUCCESS, NotifyEvent.RECOVERED):
        if getattr(job.source, "mode", None) is FilesMode.SYNC:
            campos.append(("Espelho", _esc(run.artifact or "")))
        else:
            campos.append(("Pasta", f"`{run.folder}`"))
    if evento is NotifyEvent.MISSED and run.error_cause:
        campos.append(("Motivo", _esc(run.error_cause)))

    detalhe = None
    if run.error_stage:
        linhas = [f"*Falhou no estágio* `{run.error_stage.value}`"]
        for rotulo, valor in (
            ("O que tentei", run.error_tried),
            ("O que recebi", run.error_got),
            ("Causa provável", run.error_cause),
            ("Como consertar", run.error_fix),
        ):
            if valor:
                linhas.append(f"*{rotulo}:* {_esc(valor)}")
        if run.result is RunResult.PENDING_UPLOAD:
            linhas.append(f"O artefato está no staging e o worker tenta de novo sozinho. "
                          f"Para forçar agora: `backup-runner retry {run.id}`")
        detalhe = "\n".join(linhas)

    return _cartao(evento, job.name, campos, titulo=titulo, detalhe=detalhe,
                   rodape_extra=f"`backup-runner run-info {run.id}`")


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


def _slack(settings: Settings, assunto: str, corpo: str, *, payload: dict | None = None) -> Envio:
    webhook = decrypt(settings.slack.get("webhook_enc"))
    if not webhook:
        return Envio("slack", False, "webhook não configurado")

    payload = json.dumps(payload or {
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
    cartao = _cartao(NotifyEvent.SUCCESS, "teste de aviso", [], titulo="Canal configurado",
                     detalhe=corpo)
    return _slack(settings, assunto, corpo, payload=cartao)
