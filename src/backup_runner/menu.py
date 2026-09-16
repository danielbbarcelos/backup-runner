"""Menu, a porta de entrada para quem não quer decorar comando.

É uma casca fina: cada item chama exatamente a mesma função que o comando
direto chama. Não existe caminho que só funcione pelo menu, o que significa que
tudo é automatizável e que o menu não vira um segundo programa para manter.

Cada tela limpa e redesenha o mesmo cabeçalho, com a trilha de onde se está.
Rolar para trás atrás do contexto é o que cansa num menu de terminal; aqui o
que importa fica sempre nas primeiras linhas.
"""
from __future__ import annotations

import time

from . import console as c
from . import keys
from . import forms, prompt, views
from . import __version__
from .context import Context
from .models import RunResult


def moldura(ctx: Context, *trilha: str) -> None:
    """Limpa a tela e redesenha o cabeçalho com a trilha de navegação."""
    c.limpa()
    total, ativos, _ = ctx.counts()
    proxima = ctx.next_overall()
    estado = c.muted(f"{total} jobs, {ativos} ativos")
    if proxima is not None:
        from .format import format_relative

        estado += c.muted(f"   próxima em {format_relative(proxima[1])}")
    if not ctx.tick_ok:
        estado = c.danger(f"{c.SYM_FAIL} tick não instalado, nada roda")
    c.hero(__version__, "backup agendado de bancos mysql e diretórios", estado=estado)
    if trilha:
        print("  " + c.dim(" › ".join(("início",) + trilha)))


def principal(ctx: Context) -> int:
    try:
        return _laco(ctx)
    finally:
        # Sai deixando o terminal como encontrou: cursor visível, na coluna 0,
        # numa linha nova. Sem isto o prompt do shell aparece no meio da linha.
        keys.mostra_cursor()
        print()


def _laco(ctx: Context) -> int:
    while True:
        ctx.refresh()
        moldura(ctx)
        views.resumo(ctx)
        opcoes = [
            ("jobs", "jobs           listar, criar, editar, rodar"),
            ("hist", "execuções      histórico e detalhe"),
            ("dest", "destinos       listar, criar, testar"),
            ("avisos", "avisos         quais eventos notificam"),
            ("saude", "saúde          tick, worker, chave, espaço"),
        ]
        # Só aparece quando há o que acompanhar, e aparece em primeiro lugar:
        # com um backup em curso, é essa a pergunta de quem abriu o programa.
        if ctx.running_run is not None:
            opcoes.insert(0, ("acompanhar", "acompanhar     o backup em curso, ao vivo"))

        try:
            escolha = prompt.escolhe(
                "o que você quer fazer",
                opcoes,
                permitir_cancelar=True,
                rotulo_saida="sair",
            )
        except prompt.Cancelado:
            return 0

        try:
            if escolha == "sair":
                return 0
            {
                "acompanhar": menu_acompanhar,
                "jobs": menu_jobs,
                "hist": menu_historico,
                "dest": menu_destinos,
                "avisos": menu_avisos,
                "saude": menu_saude,
            }[escolha](ctx)
        except prompt.Cancelado:
            c.info("cancelado")


# ----------------------------------------------------------------------------

def menu_acompanhar(ctx: Context) -> None:
    """Redesenha o andamento até o backup terminar, ou até ctrl-c.

    Sai sozinho quando a execução acaba, mostrando o desfecho: quem esperou
    meia hora olhando a barra merece ver como terminou sem ter que procurar.
    """
    ultima = ctx.running_run
    if ultima is None:
        return
    alvo = ultima.id
    try:
        while True:
            ctx.refresh()
            moldura(ctx, "acompanhar")
            if ctx.running_run is None:
                final = ctx.state.get_run(alvo)
                if final is not None:
                    views.detalhe_execucao(ctx, final)
                print()
                prompt.pausa("terminou. enter para voltar")
                return
            views.andamento(ctx)
            print()
            c.nota("ctrl-c para voltar ao menu, o backup continua rodando")
            time.sleep(2.0)
    except KeyboardInterrupt:
        return


def menu_jobs(ctx: Context) -> None:
    while True:
        ctx.refresh()
        moldura(ctx, "jobs")
        views.lista_jobs(ctx, dicas=False)

        opcoes = [("novo", "criar um job")]
        if ctx.views:
            opcoes = [
                ("abrir", "abrir um job"),
                ("rodar", "rodar um job agora"),
                ("pausar", "pausar ou retomar"),
                ("editar", "editar"),
                ("avisos", "avisos deste job"),
                ("apagar", "apagar"),
            ] + opcoes

        try:
            escolha = prompt.escolhe("jobs", opcoes, rotulo_saida="voltar")
        except prompt.Cancelado:
            return
        if escolha == "voltar":
            return
        if escolha == "novo":
            forms.novo_job(ctx)
            continue

        view = _escolhe_job(ctx)
        if view is None:
            continue

        if escolha == "abrir":
            moldura(ctx, "jobs", view.name)
            views.detalhe_job(ctx, view, dicas=False)
            prompt.pausa("Enter volta")
        elif escolha == "rodar":
            ctx.state.enqueue(view.name, _agora())
            c.sucesso(f"{view.name} entrou na fila")
            _avisa_sem_worker(ctx)
            prompt.pausa("Enter volta")
        elif escolha == "pausar":
            job = view.job
            job.enabled = not job.enabled
            job.paused_at = None if job.enabled else _hoje()
            ctx.jobs.put(job)
            c.sucesso(f"{job.name} " + ("retomado" if job.enabled else "pausado"))
            prompt.pausa("Enter volta")
        elif escolha == "editar":
            if view.job.kind.value == "mysql":
                forms.job_mysql(ctx, view.job)
            else:
                forms.job_arquivos(ctx, view.job)
        elif escolha == "avisos":
            moldura(ctx, "jobs", view.name, "avisos")
            forms.edita_avisos(ctx, view.job)
            prompt.pausa("Enter volta")
        elif escolha == "apagar":
            _apaga_job(ctx, view)


def _apaga_job(ctx: Context, view) -> None:
    execucoes = ctx.state.count_runs(job=view.name)
    destinos = ctx.job_destinations(view.job)
    c.aviso(f"apagar {view.name} remove também:")
    c.item("•", f"{execucoes} registros de execução")
    for jd, destino in destinos:
        c.item("•", f"os artefatos em {jd.name}, conforme a retenção de {jd.days(destino)} dias")
    if not prompt.confirma_digitando("isto não tem volta", view.name):
        c.info("cancelado")
        return
    ctx.state.delete_job_runs(view.name)
    ctx.jobs.delete(view.name)
    ctx.refresh()
    c.sucesso(f"{view.name} apagado")


def _escolhe_job(ctx: Context):
    if not ctx.views:
        return None
    try:
        nome = prompt.escolhe(
            "qual job",
            [(v.name, f"{v.name.ljust(20)} {views.badge(v.last.result if v.last else None)}")
             for v in ctx.views],
            rotulo_saida="voltar",
        )
    except prompt.Cancelado:
        return None
    return ctx.view(nome)


# ----------------------------------------------------------------------------

def menu_historico(ctx: Context) -> None:
    filtro_job = None
    filtro_resultado = None
    while True:
        execucoes = ctx.state.runs(
            job=filtro_job,
            results=[filtro_resultado] if filtro_resultado else None,
            limit=40,
        )
        rotulo = ", ".join(
            x for x in [filtro_job, filtro_resultado.value if filtro_resultado else None] if x
        )
        moldura(ctx, "execuções" + (f" ({rotulo})" if rotulo else ""))
        views.historico(ctx, execucoes, filtro=rotulo, dicas=False)

        try:
            escolha = prompt.escolhe(
                "execuções",
                [
                    ("abrir", "abrir uma execução"),
                    ("job", "filtrar por job"),
                    ("falhas", "só falhas e pendências"),
                    ("limpar", "limpar filtros"),
                ],
                rotulo_saida="voltar",
            )
        except prompt.Cancelado:
            return
        if escolha == "voltar":
            return
        if escolha == "abrir":
            if not execucoes:
                continue
            numero = prompt.inteiro("número da execução", padrao=execucoes[0].id, minimo=1)
            run = ctx.state.get_run(numero)
            if run is None:
                c.erro(f"não existe execução {numero}")
                continue
            moldura(ctx, "execuções", f"#{run.id}")
            views.detalhe_execucao(ctx, run)
            prompt.pausa("Enter volta")
        elif escolha == "job":
            view = _escolhe_job(ctx)
            filtro_job = view.name if view else None
        elif escolha == "falhas":
            filtro_resultado = RunResult.FAILED
        elif escolha == "limpar":
            filtro_job = filtro_resultado = None


# ----------------------------------------------------------------------------

def menu_destinos(ctx: Context) -> None:
    while True:
        ctx.refresh()
        moldura(ctx, "destinos")
        views.lista_destinos(ctx, dicas=False)

        opcoes = [("novo", "criar um destino")]
        if ctx.destinations.list():
            opcoes = [
                ("abrir", "abrir um destino"),
                ("testar", "testar"),
                ("editar", "editar"),
                ("apagar", "apagar"),
            ] + opcoes

        try:
            escolha = prompt.escolhe("destinos", opcoes, rotulo_saida="voltar")
        except prompt.Cancelado:
            return
        if escolha == "voltar":
            return
        if escolha == "novo":
            forms.novo_destino(ctx)
            continue

        destino = _escolhe_destino(ctx)
        if destino is None:
            continue

        if escolha == "abrir":
            moldura(ctx, "destinos", destino.name)
            views.detalhe_destino(ctx, destino)
            prompt.pausa("Enter volta")
        elif escolha == "testar":
            moldura(ctx, "destinos", destino.name, "teste")
            forms.testa_destino(destino)
            prompt.pausa("Enter volta")
        elif escolha == "editar":
            forms.novo_destino(ctx, destino)
        elif escolha == "apagar":
            usos = ctx.destination_users(destino.name)
            if usos:
                c.erro(f"{len(usos)} job(s) apontam para ele: {', '.join(usos)}")
                c.nota("tire o destino desses jobs antes de apagar")
                continue
            if prompt.confirma(f"apagar {destino.name}", padrao=False):
                ctx.destinations.delete(destino.name)
                ctx.refresh()
                c.sucesso("apagado")


def _escolhe_destino(ctx: Context):
    destinos = ctx.destinations.list()
    if not destinos:
        return None
    try:
        nome = prompt.escolhe(
            "qual destino",
            [(d.name, f"{d.name.ljust(16)} {d.kind.value.ljust(6)} {d.location()}") for d in destinos],
            rotulo_saida="voltar",
        )
    except prompt.Cancelado:
        return None
    return ctx.destinations.get(nome)


# ----------------------------------------------------------------------------

def menu_avisos(ctx: Context) -> None:
    moldura(ctx, "avisos")
    try:
        alvo = prompt.escolhe(
            "avisos de quem",
            [("global", "o padrão global, que todo job herda")]
            + [(v.name, f"o job {v.name}") for v in ctx.views]
            + [("canais", "configurar os canais (SMTP e Slack)")],
            rotulo_saida="voltar",
        )
    except prompt.Cancelado:
        return

    if alvo == "canais":
        forms.configura_canais(ctx)
        ctx.refresh()
        prompt.pausa("Enter volta")
        return

    job = None if alvo == "global" else ctx.jobs.get(alvo)
    moldura(ctx, "avisos", alvo)
    forms.edita_avisos(ctx, job)
    prompt.pausa("Enter volta")


# ----------------------------------------------------------------------------

def menu_saude(ctx: Context) -> None:
    ctx.invalidate_health()
    moldura(ctx, "saúde")
    views.saude(ctx)

    from .health import install_tick, tick_installed

    opcoes = []
    if not tick_installed():
        opcoes.append(("tick", "instalar o tick no crontab"))
    opcoes.append(("rever", "verificar de novo"))

    try:
        escolha = prompt.escolhe("saúde", opcoes, rotulo_saida="voltar")
    except prompt.Cancelado:
        return
    if escolha == "voltar":
        return
    if escolha == "tick":
        ok, mensagem = install_tick()
        (c.sucesso if ok else c.erro)(mensagem)
    elif escolha == "rever":
        menu_saude(ctx)


# ----------------------------------------------------------------------------

def _agora():
    import datetime

    return datetime.datetime.now()


def _hoje() -> str:
    import datetime

    return datetime.date.today().strftime("%d/%m")


def _avisa_sem_worker(ctx: Context) -> None:
    if not ctx.worker.running:
        c.nota("o worker não está de pé, então a fila espera")
        c.nota("para rodar agora: backup-runner worker --uma-vez")
