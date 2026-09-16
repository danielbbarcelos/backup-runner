"""O que o programa mostra: listas e fichas, em texto.

Cada função aqui imprime e não pergunta nada. Isso é o que permite a mesma
função servir ao comando direto (`backup-runner jobs`) e ao menu, e é o que
faz a saída ser útil num pipe ou redirecionada para um arquivo.
"""
from __future__ import annotations

import datetime as dt

from . import console as c
from .context import Context, JobView
from .format import (
    eta_segundos as _eta_segundos,
    format_bytes,
    format_count,
    format_duration,
    format_rate,
    format_relative,
    progress_bar,
)
from .health import Level, summary
from .models import CHANNELS, NOTIFY_EVENTS, RunResult, SourceKind
from .schedule import humanize

# Símbolo e cor de cada resultado. O símbolo é a informação; a cor, reforço.
BADGE = {
    RunResult.OK: (c.SYM_OK, "ok", c.SUCCESS),
    RunResult.FAILED: (c.SYM_FAIL, "falha", c.DANGER),
    RunResult.LATE: (c.SYM_WARN, "atrasado", c.WARNING),
    RunResult.PENDING_UPLOAD: (c.SYM_PENDING, "envio pendente", c.WARNING),
    RunResult.MISSED: (c.SYM_WARN, "janela perdida", c.WARNING),
    RunResult.SKIPPED: (c.SYM_PAUSED, "pausado", c.DISABLED),
    RunResult.RUNNING: (c.SYM_RUNNING, "rodando", c.PRIMARY),
    RunResult.QUEUED: (c.SYM_ACTIVE, "na fila", c.SECONDARY),
}


def badge(resultado: RunResult | None, *, curto: bool = False) -> str:
    if resultado is None:
        return c.muted(c.SYM_NONE)
    simbolo, rotulo, cor = BADGE[resultado]
    return c.cor(simbolo if curto else f"{simbolo} {rotulo}", cor)


def quando(momento: dt.datetime | None) -> str:
    if momento is None:
        return c.SYM_NONE
    hoje = dt.date.today()
    if momento.date() == hoje:
        return momento.strftime("hoje %H:%M")
    if momento.date() == hoje + dt.timedelta(days=1):
        return momento.strftime("amanhã %H:%M")
    return momento.strftime("%d/%m %H:%M")


# ----------------------------------------------------------------------------
# Andamento do que está rodando agora
# ----------------------------------------------------------------------------

# Depois disto sem o worker escrever nada, o progresso é velho demais para
# valer. O worker grava no máximo uma vez por segundo, então noventa segundos
# de silêncio são silêncio de verdade, não throttle.
SEM_SINAL_SEGUNDOS = 90


def andamento(ctx: Context) -> bool:
    """O que o backup em curso está fazendo agora. Devolve se havia algo.

    Esta tela existe porque "rodando" não é resposta quando o job tem doze
    gigabytes: depois de meia hora, quem olha precisa saber se aquilo anda, e
    se não anda, se ainda há alguém do outro lado.
    """
    from .service import pid_vivo

    run = ctx.running_run
    if run is None:
        return False

    prog = ctx.state.progresso_de(run.id) or {}
    etapa = prog.get("prog_stage")
    feito, total = prog.get("prog_done") or 0, prog.get("prog_total") or 0
    rotulo, batida = prog.get("prog_label") or "", prog.get("heartbeat")
    decorrido = (dt.datetime.now() - run.started_at).total_seconds()

    c.secao(f"em execução: {run.job}")
    if etapa:
        c.linha("etapa", c.primary(etapa) + (f"  {c.dim(rotulo)}" if rotulo else ""))
    else:
        # Execução iniciada por um worker anterior ao acompanhamento. Dizer
        # "iniciando" seria inventar: ela pode estar em qualquer etapa.
        c.linha("etapa", c.dim("sem detalhe, o worker desta execução não reporta progresso"))

    if total > 0:
        pct = feito / total * 100
        # Estimado pelo tamanho no disco: no dump o SQL em texto é maior, e
        # passar de 100% é normal. Dizer ">99%" é honesto; dizer 100% não.
        texto = f"{min(pct, 99.9):.1f}%" if pct < 100 else ">99%"
        c.linha("progresso", f"{c.primary(progress_bar(feito, total))} {texto}"
                             f"  {format_bytes(feito)} de {format_bytes(total)}")
    elif feito > 0:
        c.linha("progresso", f"{format_bytes(feito)}, total ainda desconhecido")

    ritmo = format_rate(feito, decorrido)
    eta = _eta_segundos(feito, total, decorrido)
    c.linha("tempo", f"{format_duration(decorrido)} até aqui"
            + (f", {ritmo}" if ritmo else "")
            + (f", faltam ~{format_relative(eta)}" if eta else ""))

    # O prazo do job é um limite real: ao estourar, a execução é interrompida
    # e o que já subiu vira reenvio pendente. Numa transferência de horas, ver
    # que sobram vinte minutos de prazo para uma hora de envio é o aviso que
    # permite aumentar o timeout antes de perder o trabalho, não depois.
    vista = ctx.view(run.job)
    if vista is not None:
        minutos = vista.job.timeout_minutes
        sobra = (run.started_at + dt.timedelta(minutes=minutos)
                 - dt.datetime.now()).total_seconds()
        texto = f"limite de {minutos} min, {format_relative(max(sobra, 0))} restantes"
        if sobra <= 0:
            c.linha("prazo", c.danger(f"{c.SYM_FAIL} passou do limite de {minutos} min"))
        elif eta and eta > sobra:
            c.linha("prazo", c.danger(
                f"{c.SYM_FAIL} {texto}, mas neste ritmo faltam {format_relative(eta)}"))
        elif sobra < 900:
            c.linha("prazo", c.warn(f"{c.SYM_WARN} {texto}"))
        else:
            c.linha("prazo", c.muted(texto))

    atraso = (dt.datetime.now() - batida).total_seconds() if batida else None
    pid = prog.get("prog_pid")
    if atraso is not None and atraso > SEM_SINAL_SEGUNDOS and not pid_vivo(pid):
        c.linha("sinal", c.danger(
            f"{c.SYM_FAIL} sem sinal há {format_relative(atraso)} e o processo {pid} sumiu"))
        print()
        c.aviso("esta execução morreu sem terminar. o tick a marca como falha "
                "no próximo minuto, ou force agora com: backup-runner tick")
    elif atraso is not None and atraso > SEM_SINAL_SEGUNDOS:
        c.linha("sinal", c.warn(
            f"{c.SYM_WARN} vivo (pid {pid}), mas sem progresso há {format_relative(atraso)}"))
    elif atraso is not None:
        c.linha("sinal", c.ok(f"{c.SYM_OK} há {format_relative(atraso)}"))
    return True


# ----------------------------------------------------------------------------
# Resumo geral
# ----------------------------------------------------------------------------

def resumo(ctx: Context) -> None:
    """A resposta para "está tudo bem?", em quatro linhas."""
    total, ativos, pausados = ctx.counts()
    worker = ctx.worker
    tick = ctx.tick_ok

    c.secao("estado")
    c.linha("jobs", f"{total} {_plural(total, 'cadastrado')}, {ativos} {_plural(ativos, 'ativo')}, {pausados} {_plural(pausados, 'pausado')}")
    c.linha(
        "tick",
        c.ok(f"{c.SYM_OK} instalado, roda a cada minuto") if tick
        else c.danger(f"{c.SYM_FAIL} não instalado, nenhum job vai rodar"),
    )
    c.linha(
        "worker",
        c.ok(f"{c.SYM_OK} rodando, pid {worker.pid}") if worker.running
        else c.danger(f"{c.SYM_FAIL} {worker.message or 'parado'}"),
    )
    c.linha("fila", "vazia" if ctx.queue_size == 0 else f"{ctx.queue_size} esperando")

    proxima = ctx.next_overall()
    if proxima is not None:
        view, segundos = proxima
        c.linha("próxima", f"{view.name}, {quando(view.next_at)} (em {format_relative(segundos)})")
    ultima = ctx.last_overall()
    if ultima is not None:
        c.linha(
            "última",
            f"{badge(ultima.result)}  {ultima.job}, {format_bytes(ultima.bytes)}"
            f" em {format_duration(ultima.duration)}",
        )

    if ctx.running_run is not None:
        print()
        andamento(ctx)

    atrasados = ctx.stale_jobs()
    if atrasados:
        print()
        c.aviso(
            f"{len(atrasados)} {_plural(len(atrasados), 'job')} sem execução bem sucedida há muito tempo: "
            + ", ".join(v.name for v in atrasados)
        )
    if not tick:
        print()
        c.aviso("sem o tick no crontab nada roda. instale com: backup-runner tick --install")


# ----------------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------------

def lista_jobs(ctx: Context, *, dicas: bool = True) -> None:
    if not ctx.views:
        c.vazio(
            "Nenhum job cadastrado.",
            "crie o primeiro com: backup-runner job add",
        )
        return

    linhas = []
    for v in ctx.views:
        marca = c.ok(c.SYM_ACTIVE) if not v.paused else c.dim(c.SYM_INACTIVE)
        resultado = v.last.result if (v.last and not v.paused) else (
            RunResult.SKIPPED if v.paused else None
        )
        if v.running:
            resultado = RunResult.RUNNING
        elif v.queued:
            resultado = RunResult.QUEUED
        alerta = c.warn(" " + c.SYM_WARN) if v.is_stale() else ""
        linhas.append([
            f"{marca} {v.name}{alerta}",
            "mysql" if v.job.kind is SourceKind.MYSQL else "arquivos",
            badge(resultado),
            format_bytes(v.last.bytes) if v.last else c.SYM_NONE,
            humanize(v.job.schedule),
            quando(v.next_at) if not v.paused else c.dim("pausado"),
        ])
    c.tabela(
        ["job", "fonte", "último", "tamanho", "quando", "próxima"],
        linhas,
        alinhamento="lllrll",
    )
    if dicas:
        print()
        c.nota("detalhe de um job: backup-runner job <nome>")


def detalhe_job(ctx: Context, view: JobView, *, dicas: bool = True) -> None:
    job = view.job
    c.titulo(view.name, sub="pausado" if view.paused else "")

    if job.kind is SourceKind.MYSQL:
        fonte = job.source
        c.linha("fonte", f"mysql  {fonte.database} @ {fonte.host}:{fonte.port}  (usuário {fonte.user})")
        c.linha("ignorar", f"regex {fonte.ignore_regex}")
        if fonte.ignore_manual:
            c.linha("", f"à mão: {', '.join(fonte.ignore_manual)}", largura_rotulo=14)
        if fonte.keep_manual:
            c.linha("", f"resgatadas: {', '.join(fonte.keep_manual)}", largura_rotulo=14)
    else:
        fonte = job.source
        c.linha("fonte", f"arquivos  {fonte.path}")
        c.linha("formato", fonte.archive_format.value)
        ativos = fonte.active_excludes()
        c.linha("excluir", ", ".join(ativos) if ativos else c.muted("nada"))

    c.linha("quando", f"{humanize(job.schedule)}   ({job.schedule}, {job.timezone})")
    segundos = view.next_in_seconds()
    if segundos is not None:
        c.linha("próxima", f"{quando(view.next_at)}, em {format_relative(segundos)}")
    else:
        c.linha("próxima", c.dim("nenhuma, o job está pausado"))
    c.linha("tolerância", f"{job.catch_up_window_minutes} min de atraso ainda rodam")
    c.linha("tempo limite", f"{job.timeout_minutes} min")

    c.secao("destinos")
    if job.destinations:
        linhas = []
        for jd, destino in ctx.job_destinations(job):
            if destino is None:
                linhas.append([c.danger(jd.name), c.danger("não existe mais"), ""])
                continue
            marca = "" if destino.enabled else c.warn(f" {c.SYM_WARN}")
            linhas.append([
                jd.name + marca, destino.location(), f"manter {jd.days(destino)} dias",
            ])
        c.tabela(["destino", "onde", "retenção"], linhas)
    else:
        c.aviso("nenhum destino: o artefato é descartado ao fim do job")

    c.secao("avisos")
    padrao = ctx.settings.notify_global
    linhas = []
    for evento in NOTIFY_EVENTS:
        canais = [canal.value for canal in CHANNELS if job.notify.resolve(evento, canal, padrao)]
        proprio = any(job.notify.get(evento, canal) is not None for canal in CHANNELS)
        linhas.append([
            _nome_evento(evento),
            ", ".join(canais) if canais else c.dim("nenhum"),
            c.muted("do job") if proprio else c.dim("global"),
        ])
    c.tabela(["evento", "canais", "origem"], linhas)

    c.secao("últimas execuções")
    execucoes = ctx.state.runs(job=view.name, limit=5)
    if not execucoes:
        c.vazio("Nenhuma execução ainda.", f"rode agora com: backup-runner run {view.name}")
        return
    _tabela_execucoes(execucoes, com_job=False)
    if dicas:
        print()
        c.nota(f"histórico completo: backup-runner history --job {view.name}")
        c.nota("uma execução: backup-runner run-info <número>")


def _nome_evento(evento) -> str:
    return {
        "success": "sucesso",
        "failure": "falha",
        "recovered": "recuperado",
        "missed": "janela perdida",
        "stale": "silêncio longo",
    }[evento.value]


# ----------------------------------------------------------------------------
# Execuções
# ----------------------------------------------------------------------------

def _tabela_execucoes(execucoes, *, com_job: bool = True) -> None:
    cabecalho = ["nº", "quando", "job", "resultado", "tamanho", "duração", "destinos"]
    alinhamento = "rlllrrl"
    if not com_job:
        cabecalho.pop(2)
        alinhamento = "rllrrl"
    linhas = []
    for run in execucoes:
        destinos = (
            ", ".join(run.destinations_done) if run.destinations_done
            else (run.error_cause[:24] if run.error_cause else c.muted("nenhum"))
        )
        if run.destinations_pending:
            destinos += c.warn(f"  (falta {len(run.destinations_pending)})")
        celulas = [
            str(run.id),
            run.started_at.strftime("%d/%m %H:%M"),
            run.job,
            badge(run.result),
            format_bytes(run.bytes),
            format_duration(run.duration),
            destinos,
        ]
        if not com_job:
            celulas.pop(2)
        linhas.append(celulas)
    c.tabela(cabecalho, linhas, alinhamento=alinhamento)


def historico(ctx: Context, execucoes, *, filtro: str = "", dicas: bool = True) -> None:
    if not execucoes:
        c.vazio("Nenhuma execução no filtro.", "tente sem filtro: backup-runner history")
        return
    _tabela_execucoes(execucoes)
    print()
    c.nota(f"{len(execucoes)} execuções{(' | filtro: ' + filtro) if filtro else ''}")
    if dicas:
        c.nota("detalhe: backup-runner run-info <número>")


def detalhe_execucao(ctx: Context, run) -> None:
    simbolo, rotulo, cor = BADGE[run.result]
    c.titulo(
        f"execução {run.id}: {run.job}",
        sub=f"{run.started_at:%d/%m/%Y %H:%M}",
    )
    c.linha("resultado", c.cor(f"{simbolo} {rotulo}", cor))
    c.linha("tamanho", format_bytes(run.bytes))
    c.linha("duração", format_duration(run.duration))
    c.linha("pasta", run.folder + "/")
    if run.artifact:
        c.linha("arquivo", run.artifact)

    if run.error_stage:
        c.secao("o que falhou")
        c.linha("estágio", run.error_stage.value)
        # O mesmo molde de quatro linhas em todo erro do programa.
        for chave, valor in (
            ("o que tentei", run.error_tried),
            ("o que recebi", run.error_got),
            ("causa provável", run.error_cause),
            ("como consertar", run.error_fix),
        ):
            if valor:
                c.linha(chave, valor, largura_rotulo=16)

    if run.stages:
        c.secao("estágios")
        linhas = []
        for e in run.stages:
            marca = {
                "done": c.ok(c.SYM_OK),
                "running": c.primary(c.SYM_RUNNING),
                "waiting": c.dim(c.SYM_INACTIVE),
                "failed": c.danger(c.SYM_FAIL),
                "skipped": c.dim(c.SYM_INACTIVE),
            }[e.state.value]
            linhas.append([
                f"{marca} {e.label or e.stage.value}",
                e.detail,
                format_duration(e.seconds) if e.seconds else c.muted("·"),
            ])
        c.tabela(["estágio", "detalhe", "tempo"], linhas, alinhamento="llr")

    if run.manifest:
        c.secao("manifest")
        c.nota("sha256 de cada cópia, para conferir no destino")
        linhas = [
            [
                e.destination,
                e.sha256[:16],
                c.ok("confere") if e.verified else c.danger("não confere"),
                format_bytes(e.bytes),
            ]
            for e in run.manifest
        ]
        c.tabela(["destino", "sha256", "estado", "tamanho"], linhas, alinhamento="lllr")

    if run.ignored_total:
        c.secao(f"tabelas ignoradas ({run.ignored_total})")
        if run.ignored_regex:
            c.linha("pela regex", ", ".join(run.ignored_regex), largura_rotulo=12)
        if run.ignored_manual:
            c.linha("à mão", ", ".join(run.ignored_manual), largura_rotulo=12)

    if run.log:
        c.secao("log")
        for hora, origem, texto in run.log:
            print(f"  {c.muted(hora)}  {c.secondary(origem.ljust(9))}  {texto}")

    if run.result is RunResult.PENDING_UPLOAD:
        print()
        c.aviso(f"falta enviar para: {', '.join(run.destinations_pending)}")
        if run.retry_at:
            c.nota(f"o worker tenta de novo em {quando(run.retry_at)}")
        c.nota(f"reenviar agora: backup-runner retry {run.id}")


# ----------------------------------------------------------------------------
# Destinos
# ----------------------------------------------------------------------------

def lista_destinos(ctx: Context, *, dicas: bool = True) -> None:
    destinos = ctx.destinations.list()
    if not destinos:
        c.vazio(
            "Nenhum destino cadastrado.",
            "sem destino o backup fica só no staging. crie com: backup-runner dest add",
        )
        return
    linhas = []
    for d in destinos:
        marca = c.ok(c.SYM_ACTIVE) if d.enabled else c.dim(c.SYM_INACTIVE)
        usos = ctx.destination_users(d.name)
        linhas.append([
            f"{marca} {d.name}",
            d.kind.value,
            d.location(),
            f"{d.retention_days} dias",
            f"{len(usos)} job(s)" if usos else c.muted("nenhum job"),
        ])
    c.tabela(["destino", "tipo", "onde", "retenção", "usado por"], linhas, alinhamento="lllrl")
    if dicas:
        print()
        c.nota("testar: backup-runner dest test <nome>")


def detalhe_destino(ctx: Context, destino) -> None:
    c.titulo(destino.name, sub=destino.kind.value)
    c.linha("ativo", "sim" if destino.enabled else c.dim("não"))
    c.linha("retenção", f"{destino.retention_days} dias, contados no destino")
    if destino.kind.value == "local":
        c.linha("caminho", destino.path)
        c.linha("criar", "sim" if destino.create_missing else "não")
    elif destino.kind.value == "s3":
        c.linha("endpoint", destino.endpoint)
        c.linha("região", destino.region)
        c.linha("bucket", destino.bucket)
        c.linha("prefixo", destino.prefix or c.muted("nenhum"))
        c.linha("chave", destino.access_key)
        c.linha("secret", c.muted("•" * 12 + "  (cifrado em disco)") if destino.secret_enc else c.dim("não definido"))
    else:
        c.linha("host", f"{destino.user}@{destino.host}:{destino.port}")
        c.linha("autenticação", destino.auth)
        if destino.auth == "key":
            c.linha("chave privada", destino.private_key)
        c.linha("caminho", destino.remote_path)

    usos = ctx.destination_users(destino.name)
    c.secao("uso")
    if usos:
        for nome in usos:
            c.item("•", nome)
        c.nota("apagar exige que nenhum job aponte para ele")
    else:
        c.nota("nenhum job aponta para este destino")


# ----------------------------------------------------------------------------
# Saúde
# ----------------------------------------------------------------------------

def saude(ctx: Context) -> None:
    itens = ctx.health
    ok_, warn_, fail_ = summary(itens)
    c.titulo("saúde do sistema", sub=f"{c.SYM_OK} {ok_}   {c.SYM_WARN} {warn_}   {c.SYM_FAIL} {fail_}")

    for item in itens:
        marca = {
            Level.OK: c.ok(c.SYM_OK),
            Level.WARN: c.warn(c.SYM_WARN),
            Level.FAIL: c.danger(c.SYM_FAIL),
        }[item.level]
        print(f"  {marca} {c.bold(item.title.ljust(26))} {item.detail}")
        if item.why:
            print(f"      {c.muted(item.why)}")
        for extra in item.extra:
            print(f"      {c.muted(extra)}")
        if item.progress is not None:
            print(f"      {c.barra(item.progress, 40)}")
        if item.fix_command:
            print(f"      {c.secondary('conserto')}  {item.fix_command}")
        print()


# ----------------------------------------------------------------------------
# Avisos
# ----------------------------------------------------------------------------

def matriz_avisos(ctx: Context, job=None) -> None:
    padrao = ctx.settings.notify_global
    alvo = job.notify if job is not None else padrao
    c.titulo(
        f"avisos de {job.name}" if job else "avisos, padrão global",
        sub="o que o job não define, herda do global" if job else "vale para todo job",
    )

    linhas = []
    for evento in NOTIFY_EVENTS:
        celulas = [_nome_evento(evento)]
        for canal in CHANNELS:
            configurado = ctx.settings.channel_configured(canal.value)
            if not configurado:
                celulas.append(c.dim("sem canal"))
                continue
            valor = alvo.resolve(evento, canal, padrao)
            proprio = alvo.get(evento, canal) if job is not None else None
            texto = c.ok("sim") if valor else c.dim("não")
            if job is not None and proprio is not None and proprio != padrao.get(evento, canal):
                texto += c.warn(" *")
            celulas.append(texto)
        linhas.append(celulas)

    c.tabela(["evento", "email", "slack"], linhas)
    print()
    if job is not None:
        c.nota("* marca o que este job sobrescreveu do padrão global")
    email = ctx.settings.email_summary()
    slack = ctx.settings.slack_summary()
    c.nota(f"email: {email or 'não configurado'}    slack: {slack or 'não configurado'}")


def _plural(n: int, palavra: str) -> str:
    return palavra if n == 1 else palavra + "s"
