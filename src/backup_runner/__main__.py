"""Linha de comando.

Cada coisa que o programa faz é um comando, e o menu (`backup-runner` sem
argumento) só chama esses mesmos comandos. Não existe caminho que funcione só
pelo menu: tudo é scriptável, e a saída é texto que `grep` filtra.

  backup-runner                  menu
  backup-runner status           resumo do sistema
  backup-runner jobs             lista os jobs
  backup-runner job <nome>       detalhe de um job
  backup-runner job add          cadastra um job, por perguntas
  backup-runner run <nome>       põe um job na fila agora
  backup-runner history          execuções
  backup-runner run-info <nº>    detalhe de uma execução
  backup-runner dest             destinos
  backup-runner channels         configura SMTP e Slack
  backup-runner health           diagnóstico
  backup-runner tick             o que o cron chama
  backup-runner worker           o que o supervisord chama
  backup-runner install          instala o agendamento
  backup-runner self ...         instala, remove e atualiza o programa
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from . import APP_SLUG, __version__
from . import console as c
from . import prompt
from .i18n import set_locale


def _ctx():
    from .context import Context

    return Context()


# ----------------------------------------------------------------------------
# Menu e resumo
# ----------------------------------------------------------------------------

def cmd_menu(args: argparse.Namespace) -> int:
    from . import menu

    if not sys.stdin.isatty():
        c.erro("o menu precisa de um terminal. use os comandos diretos, veja --help")
        return 2
    return menu.principal(_ctx())


def cmd_status(args: argparse.Namespace) -> int:
    from . import views

    ctx = _ctx()
    views.resumo(ctx)
    if args.jobs:
        print()
        views.lista_jobs(ctx)
    return 0


# ----------------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------------

def cmd_jobs(args: argparse.Namespace) -> int:
    from . import views

    views.lista_jobs(_ctx())
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    from . import forms, views

    ctx = _ctx()
    alvo = args.nome

    if alvo == "add":
        return 0 if forms.novo_job(ctx) else 1

    view = ctx.view(alvo)
    if view is None:
        c.erro(f"não existe job chamado {alvo}")
        c.nota("veja os nomes com: backup-runner jobs")
        return 1

    if args.editar:
        job = view.job
        novo = forms.job_mysql(ctx, job) if job.kind.value == "mysql" else forms.job_arquivos(ctx, job)
        return 0 if novo else 1
    if args.pausar:
        job = view.job
        job.enabled = not job.enabled
        job.paused_at = None if job.enabled else dt.date.today().strftime("%d/%m")
        ctx.jobs.put(job)
        c.sucesso(f"{job.name} " + ("retomado" if job.enabled else "pausado"))
        return 0
    if args.apagar:
        if not args.yes and not prompt.confirma_digitando(
            f"apagar {alvo} e seus {ctx.state.count_runs(job=alvo)} registros de execução", alvo
        ):
            c.info("cancelado")
            return 1
        ctx.state.delete_job_runs(alvo)
        ctx.jobs.delete(alvo)
        c.sucesso(f"{alvo} apagado")
        return 0

    views.detalhe_job(ctx, view)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ctx = _ctx()
    view = ctx.view(args.nome)
    if view is None:
        c.erro(f"não existe job chamado {args.nome}")
        return 1
    ctx.state.enqueue(args.nome, dt.datetime.now())
    c.sucesso(f"{args.nome} entrou na fila")
    if not ctx.worker.running:
        c.nota("o worker não está de pé, então a fila espera")
        c.nota("para rodar agora mesmo: backup-runner worker --uma-vez")
    return 0


# ----------------------------------------------------------------------------
# Execuções
# ----------------------------------------------------------------------------

def cmd_history(args: argparse.Namespace) -> int:
    from . import views
    from .models import RunResult

    ctx = _ctx()
    resultados = None
    if args.falhas:
        resultados = [RunResult.FAILED, RunResult.MISSED, RunResult.PENDING_UPLOAD, RunResult.LATE]
    desde = dt.datetime.now() - dt.timedelta(days=args.dias) if args.dias else None
    execucoes = ctx.state.runs(job=args.job, results=resultados, since=desde, limit=args.limite)
    filtro = ", ".join(x for x in [args.job, "falhas" if args.falhas else None] if x)
    views.historico(ctx, execucoes, filtro=filtro)
    return 0


def cmd_run_info(args: argparse.Namespace) -> int:
    from . import views

    ctx = _ctx()
    run = ctx.state.get_run(args.numero)
    if run is None:
        c.erro(f"não existe execução {args.numero}")
        c.nota("veja os números com: backup-runner history")
        return 1
    views.detalhe_execucao(ctx, run)
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    from .models import RunResult

    ctx = _ctx()
    run = ctx.state.get_run(args.numero)
    if run is None:
        c.erro(f"não existe execução {args.numero}")
        return 1
    if run.result is not RunResult.PENDING_UPLOAD:
        c.erro("essa execução não tem envio pendente")
        return 1
    ctx.state.enqueue(run.job, dt.datetime.now(), kind="upload_retry")
    c.sucesso(f"reenvio de {run.job} na fila, usando o artefato do staging")
    return 0


# ----------------------------------------------------------------------------
# Destinos
# ----------------------------------------------------------------------------

def cmd_dest(args: argparse.Namespace) -> int:
    from . import forms, views

    ctx = _ctx()
    acao = args.acao or "list"

    if acao == "list":
        views.lista_destinos(ctx)
        return 0
    if acao == "add":
        return 0 if forms.novo_destino(ctx) else 1

    if not args.nome:
        c.erro(f"o comando dest {acao} precisa do nome do destino")
        return 2
    destino = ctx.destinations.get(args.nome)
    if destino is None:
        c.erro(f"não existe destino chamado {args.nome}")
        return 1

    if acao == "show":
        views.detalhe_destino(ctx, destino)
        return 0
    if acao == "test":
        return 0 if forms.testa_destino(destino) else 1
    if acao == "edit":
        return 0 if forms.novo_destino(ctx, destino) else 1
    if acao == "rm":
        usos = ctx.destination_users(destino.name)
        if usos:
            c.erro(f"{len(usos)} job(s) apontam para ele: {', '.join(usos)}")
            return 1
        if not args.yes and not prompt.confirma(f"apagar {destino.name}", padrao=False):
            return 1
        ctx.destinations.delete(destino.name)
        c.sucesso("apagado")
        return 0
    return 2


# ----------------------------------------------------------------------------
# Avisos e saúde
# ----------------------------------------------------------------------------

def cmd_notify(args: argparse.Namespace) -> int:
    from . import views

    ctx = _ctx()
    job = ctx.jobs.get(args.job) if args.job else None
    if args.job and job is None:
        c.erro(f"não existe job chamado {args.job}")
        return 1
    views.matriz_avisos(ctx, job)
    return 0


def cmd_channels(args: argparse.Namespace) -> int:
    from . import forms

    ctx = _ctx()
    if args.canal == "email":
        forms.configura_smtp(ctx)
    elif args.canal == "slack":
        forms.configura_slack(ctx)
    else:
        forms.configura_canais(ctx)
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    from . import notify

    resultado = notify.teste(args.canal)
    if resultado.ok:
        c.sucesso(f"{resultado.canal} enviado para {resultado.detalhe}")
        return 0
    c.erro(f"{resultado.canal}: {resultado.detalhe}")
    return 1


def cmd_health(args: argparse.Namespace) -> int:
    from . import views
    from .health import Level

    ctx = _ctx()
    views.saude(ctx)
    return 0 if not any(i.level is Level.FAIL for i in ctx.health) else 1


# ----------------------------------------------------------------------------
# Agendamento
# ----------------------------------------------------------------------------

def cmd_tick(args: argparse.Namespace) -> int:
    from .health import install_tick, tick_installed, uninstall_tick

    if args.install:
        ok, mensagem = install_tick()
        (c.sucesso if ok else c.erro)(mensagem)
        return 0 if ok else 1
    if args.uninstall:
        ok, mensagem = uninstall_tick()
        (c.sucesso if ok else c.erro)(mensagem)
        return 0 if ok else 1
    if args.check:
        instalado = tick_installed()
        (c.sucesso if instalado else c.erro)("instalado" if instalado else "não instalado")
        return 0 if instalado else 1

    from .tick import run_tick

    resultado = run_tick()
    if args.verbose or resultado.enfileirados or resultado.perdidos or resultado.reenvios:
        print(resultado.resumo())
        for nome, janela, atrasado in resultado.enfileirados:
            print(f"  fila     {nome}  janela {janela:%d/%m %H:%M}" + (" (atrasado)" if atrasado else ""))
        for nome, janela in resultado.perdidos:
            print(f"  perdida  {nome}  janela {janela:%d/%m %H:%M}")
        for nome in resultado.reenvios:
            print(f"  reenvio  {nome}")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Consome a fila. É isto que o supervisord mantém de pé."""
    from . import worker

    if args.uma_vez:
        c.info("processando um item da fila, se houver")
    return worker.run_forever(intervalo=args.intervalo, uma_vez=args.uma_vez)


def cmd_install(args: argparse.Namespace) -> int:
    from tempfile import NamedTemporaryFile

    from .health import install_tick, supervisor_conf, tick_installed

    if args.check:
        instalado = tick_installed()
        (c.sucesso if instalado else c.erro)(
            "tick no crontab" if instalado else "tick não está no crontab"
        )
        return 0 if instalado else 1

    ok, mensagem = install_tick()
    (c.sucesso if ok else c.erro)(f"crontab: {mensagem}")

    with NamedTemporaryFile("w", suffix=".conf", prefix=f"{APP_SLUG}-", delete=False) as f:
        f.write(supervisor_conf())
        caminho = f.name

    print()
    c.info("supervisord: rode você mesmo, depois de ler o arquivo")
    print(f"    cat {caminho}")
    print(f"    sudo cp {caminho} /etc/supervisor/conf.d/{APP_SLUG}.conf")
    print("    sudo supervisorctl reread && sudo supervisorctl update")
    return 0 if ok else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from .config import config_dir, data_dir
    from .seed import populate

    if not args.yes:
        c.aviso("isto sobrescreve jobs, destinos e histórico em:")
        c.nota(str(config_dir()))
        c.nota(str(data_dir()))
        if not prompt.confirma("continuar", padrao=False):
            return 1
    populate()
    c.sucesso("dados de demonstração escritos")
    return 0


# ----------------------------------------------------------------------------
# self
# ----------------------------------------------------------------------------

def cmd_self(args: argparse.Namespace) -> int:
    from . import selfmanage as sm

    acao = args.acao or "status"
    if acao == "status":
        return _self_status(sm)
    if acao == "releases":
        releases = sm.list_releases()
        if not releases:
            c.erro("nenhum release publicado ainda")
            return 1
        c.tabela(
            ["tag", "data", "título"],
            [[tag, data, titulo] for tag, data, titulo in releases],
        )
        return 0
    if acao == "uninstall":
        return _self_uninstall(sm, args)
    return _self_install(sm, args, reinstalar=acao == "reinstall")


def _self_status(sm) -> int:
    atual = sm.installed()
    if not atual.presente:
        c.erro("não está instalado")
        c.nota(f"instale com: python3 -m {__package__} self install")
        return 1
    c.sucesso(f"instalado {atual.versao or 'versão desconhecida'}")
    c.linha("origem", atual.origem())
    if atual.caminho:
        c.linha("binário", atual.caminho)
    if atual.python:
        c.linha("python", atual.python)
    if not sm.pipx_path():
        c.aviso("o pipx não está no PATH, então atualizar daqui não vai funcionar")
    return 0


def _self_install(sm, args: argparse.Namespace, *, reinstalar: bool) -> int:
    try:
        ref = sm.resolve_ref(args.ref, local=args.local)
    except sm.SelfError as exc:
        c.erro(str(exc))
        return 2

    atual = sm.installed()
    if atual.presente and not reinstalar and not args.force:
        c.info(f"já instalado: {atual.versao} ({atual.origem()})")
        c.nota(f"para trocar: {APP_SLUG} self reinstall --ref {args.ref or 'latest'}")
        return 1

    c.info(f"instalando do {ref.descricao()}")
    ok, saida = sm.install(
        ref,
        force=reinstalar or args.force or atual.presente,
        limpo=args.limpo,
    )
    if not ok:
        print(saida, file=sys.stderr)
        c.erro("a instalação falhou")
        return 1

    depois = sm.installed()
    c.sucesso(f"{APP_SLUG} {depois.versao or ''} instalado de {ref.descricao()}".rstrip())
    if depois.caminho:
        c.nota(depois.caminho)
    else:
        c.aviso("o binário não apareceu no PATH; talvez seja preciso reabrir o shell")
    sobras = sm.orfas()
    if sobras:
        c.aviso(f"sobraram no ambiente pacotes que o projeto não usa mais: {', '.join(sobras)}")
        c.nota(f"para limpar: {APP_SLUG} self reinstall --limpo")

    print()
    c.nota(f"abra com: {APP_SLUG}")
    c.nota(f"agende:   {APP_SLUG} install")
    return 0


def _self_uninstall(sm, args: argparse.Namespace) -> int:
    atual = sm.installed()
    if not atual.presente:
        c.info("não está instalado")
        return 1

    if not args.yes:
        c.aviso(f"isto remove o programa ({atual.versao or 'versão desconhecida'})")
        if args.purge:
            c.nota("e APAGA jobs, destinos, segredos e histórico em:")
            for caminho in sm.purge_paths():
                c.nota(f"  {caminho}")
        else:
            c.nota("jobs, destinos e histórico ficam onde estão")
        if not prompt.confirma("continuar", padrao=False):
            c.info("cancelado")
            return 1

    from .health import tick_installed, uninstall_tick

    # O cron fica órfão se o binário sumir, então sai junto.
    if tick_installed():
        ok_tick, msg_tick = uninstall_tick()
        (c.sucesso if ok_tick else c.aviso)(f"crontab: {msg_tick}")

    ok, saida = sm.uninstall()
    if not ok:
        print(saida, file=sys.stderr)
        return 1
    c.sucesso("programa removido")

    if args.purge:
        import shutil as _shutil

        for caminho in sm.purge_paths():
            if caminho.exists():
                _shutil.rmtree(caminho, ignore_errors=True)
                c.sucesso(f"apagado {caminho}")
    else:
        c.nota("jobs, destinos e histórico continuam em ~/.config e ~/.local/share")
        c.nota(f"para apagar também: {APP_SLUG} self uninstall --purge")

    c.nota("o worker do supervisord, se existir, sai na mão:")
    c.nota(f"  sudo rm /etc/supervisor/conf.d/{APP_SLUG}.conf")
    c.nota("  sudo supervisorctl reread && sudo supervisorctl update")
    return 0


# ----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP_SLUG,
        description="Backup agendado de bancos MySQL e diretórios",
        epilog="sem argumento, abre o menu",
    )
    p.add_argument("--locale", choices=["pt-BR", "en-US"], default=None)
    p.add_argument("--version", action="version", version=f"{APP_SLUG} {__version__}")
    p.set_defaults(func=cmd_menu)
    sub = p.add_subparsers(dest="comando")

    s = sub.add_parser("status", help="resumo do sistema")
    s.add_argument("--jobs", action="store_true", help="lista os jobs junto")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("jobs", help="lista os jobs")
    s.set_defaults(func=cmd_jobs)

    s = sub.add_parser("job", help="detalhe de um job, ou 'add' para criar")
    s.add_argument("nome")
    s.add_argument("--editar", action="store_true")
    s.add_argument("--pausar", action="store_true", help="pausa ou retoma")
    s.add_argument("--apagar", action="store_true")
    s.add_argument("--yes", action="store_true", help="não perguntar ao apagar")
    s.set_defaults(func=cmd_job)

    s = sub.add_parser("run", help="põe um job na fila agora")
    s.add_argument("nome")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("history", help="execuções")
    s.add_argument("--job", default=None)
    s.add_argument("--falhas", action="store_true", help="só falhas e pendências")
    s.add_argument("--dias", type=int, default=30)
    s.add_argument("--limite", type=int, default=40)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("run-info", help="detalhe de uma execução")
    s.add_argument("numero", type=int)
    s.set_defaults(func=cmd_run_info)

    s = sub.add_parser("retry", help="reenvia o artefato de uma execução pendente")
    s.add_argument("numero", type=int)
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("dest", help="destinos")
    s.add_argument("acao", nargs="?", default="list",
                   choices=["list", "add", "show", "test", "edit", "rm"])
    s.add_argument("nome", nargs="?", default=None)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_dest)

    s = sub.add_parser("notify", help="quais eventos avisam, e por onde")
    s.add_argument("--job", default=None)
    s.set_defaults(func=cmd_notify)

    s = sub.add_parser("health", help="diagnóstico do sistema")
    s.set_defaults(func=cmd_health)

    s = sub.add_parser("channels", help="configura SMTP e Slack")
    s.add_argument("canal", nargs="?", choices=["email", "slack"], default=None)
    s.set_defaults(func=cmd_channels)

    s = sub.add_parser("notify-test", help="manda uma mensagem de teste")
    s.add_argument("canal", choices=["email", "slack"])
    s.set_defaults(func=cmd_notify_test)

    s = sub.add_parser("tick", help="decide o que entra na fila (o cron chama isto)")
    s.add_argument("--install", action="store_true")
    s.add_argument("--uninstall", action="store_true")
    s.add_argument("--check", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_tick)

    s = sub.add_parser("worker", help="consome a fila (o supervisord chama isto)")
    s.add_argument("--uma-vez", action="store_true", dest="uma_vez",
                   help="processa um item e sai, em vez de ficar de pé")
    s.add_argument("--intervalo", type=float, default=5.0,
                   help="segundos entre consultas à fila")
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser("install", help="instala o agendamento: cron e supervisord")
    s.add_argument("--check", action="store_true")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("demo", help="popula dados de demonstração")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("self", help="instala, remove e atualiza o próprio programa")
    s.add_argument("acao", nargs="?", default="status",
                   choices=["install", "uninstall", "reinstall", "status", "releases"])
    s.add_argument("--ref", default=None,
                   help="latest (padrão), uma tag como v0.3.0, um hash de commit, ou main")
    s.add_argument("--local", default=None, help="instala de um clone local")
    s.add_argument("--force", action="store_true")
    s.add_argument("--limpo", action="store_true",
                   help="recria o ambiente do zero, tirando dependências antigas")
    s.add_argument("--purge", action="store_true", help="na remoção, apaga config e dados")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_self)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.locale:
        set_locale(args.locale)
    try:
        return int(args.func(args) or 0)
    except prompt.Cancelado:
        c.info("cancelado")
        return 130
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        # Acontece ao mandar a saída para `head`; não é erro do programa.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
