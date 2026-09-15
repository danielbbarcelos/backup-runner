"""Ponto de entrada.

  backup-runner            abre a interface
  backup-runner tick       decide o que entra na fila (o cron chama isto)
  backup-runner worker     consome a fila (o supervisord chama isto)
  backup-runner install    escreve o cron e gera o conf do supervisord
  backup-runner status     estado do sistema, sem abrir a interface
  backup-runner demo       popula dados de demonstração

O `install` escreve no crontab do próprio usuário sozinho, porque isso não
precisa de sudo. Para o supervisord ele gera o arquivo e imprime as linhas de
sudo para você colar: o app nunca chama sudo por conta própria, já que instalar
um serviço que roda para sempre merece ser lido antes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import APP_SLUG, __version__
from .i18n import set_locale, t


def cmd_tui(args: argparse.Namespace) -> int:
    from .ui.app import BackupRunnerApp

    BackupRunnerApp(skip_splash=args.no_splash, tela=args.tela).run()
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    from .health import install_tick, tick_installed, uninstall_tick

    if args.install:
        ok, mensagem = install_tick()
        print(("✓ " if ok else "✗ ") + mensagem)
        return 0 if ok else 1
    if args.uninstall:
        ok, mensagem = uninstall_tick()
        print(("✓ " if ok else "✗ ") + mensagem)
        return 0 if ok else 1
    if args.check:
        instalado = tick_installed()
        print("✓ instalado" if instalado else "✗ não instalado")
        return 0 if instalado else 1

    from .tick import run_tick

    resultado = run_tick()
    if args.verbose or resultado.enfileirados or resultado.perdidos or resultado.reenvios:
        print(resultado.resumo())
        for nome, janela, atrasado in resultado.enfileirados:
            marca = " (atrasado)" if atrasado else ""
            print(f"  fila  {nome}  janela {janela:%d/%m %H:%M}{marca}")
        for nome, janela in resultado.perdidos:
            print(f"  perdida  {nome}  janela {janela:%d/%m %H:%M}")
        for nome in resultado.reenvios:
            print(f"  reenvio  {nome}")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """O worker ainda não existe.

    A interface, o agendador e o modelo de dados estão prontos; a execução
    (dump, compressão, envio, retenção, aviso) é a próxima frente. Sair com
    erro explícito é melhor que um laço que não faz nada: assim o supervisord
    mostra o programa em FATAL e a tela de saúde não mente dizendo que o
    backup está de pé.
    """
    print(
        "o worker ainda não foi implementado.\n"
        "a fila, o agendador e a interface funcionam; falta a camada de execução\n"
        "(dump, compressão, envio e aviso). Enquanto isso, a fila acumula e a\n"
        "interface mostra o que está esperando.",
        file=sys.stderr,
    )
    return 3


def cmd_install(args: argparse.Namespace) -> int:
    from .health import install_tick, supervisor_conf, tick_installed
    from tempfile import NamedTemporaryFile

    if args.check:
        print(("✓" if tick_installed() else "✗") + " tick no crontab")
        return 0

    ok, mensagem = install_tick()
    print(("✓ crontab: " if ok else "✗ crontab: ") + mensagem)

    with NamedTemporaryFile("w", suffix=".conf", prefix=f"{APP_SLUG}-", delete=False) as f:
        f.write(supervisor_conf())
        caminho = f.name

    print()
    print("● supervisord: rode você mesmo, depois de ler o arquivo")
    print(f"    cat {caminho}")
    print(f"    sudo cp {caminho} /etc/supervisor/conf.d/{APP_SLUG}.conf")
    print("    sudo supervisorctl reread && sudo supervisorctl update")
    print()
    print(supervisor_conf())
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    from .health import collect, summary
    from .ui.context import Context

    ctx = Context()
    itens = collect(ctx.destinations.list())
    ok, warn, fail = summary(itens)
    simbolos = {"ok": "✓", "warn": "!", "fail": "✗"}
    for item in itens:
        print(f"{simbolos[item.level.value]} {item.title:<28} {item.detail}")
        if item.fix_command:
            print(f"    conserto: {item.fix_command}")
    print()
    print(f"{ok} ok, {warn} avisos, {fail} falhas   fila: {ctx.queue_size}   jobs: {len(ctx.views)}")
    return 0 if fail == 0 else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from .config import config_dir, data_dir
    from .seed import populate

    if not args.yes:
        print(f"isto sobrescreve jobs, destinos e histórico em:\n  {config_dir()}\n  {data_dir()}")
        resposta = input("continuar? [s/N] ").strip().lower()
        if resposta not in ("s", "sim", "y", "yes"):
            print("cancelado")
            return 1
    populate()
    print("✓ dados de demonstração escritos")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_SLUG, description=__doc__.splitlines()[0])
    parser.add_argument("--locale", choices=["pt-BR", "en-US"], default=None)
    parser.add_argument("--version", action="version", version=f"{APP_SLUG} {__version__}")
    parser.add_argument("--no-splash", action="store_true", help="pula o hero de abertura")
    parser.add_argument("--tela", choices=["destinos", "saude", "historico", "avisos"], default=None)
    parser.set_defaults(func=cmd_tui)

    sub = parser.add_subparsers(dest="comando")

    p_tick = sub.add_parser("tick", help="decide o que entra na fila")
    p_tick.add_argument("--install", action="store_true", help="escreve a linha no crontab")
    p_tick.add_argument("--uninstall", action="store_true", help="remove a linha do crontab")
    p_tick.add_argument("--check", action="store_true", help="só verifica se está instalado")
    p_tick.add_argument("-v", "--verbose", action="store_true")
    p_tick.set_defaults(func=cmd_tick)

    p_worker = sub.add_parser("worker", help="consome a fila")
    p_worker.set_defaults(func=cmd_worker)

    p_install = sub.add_parser("install", help="cron e supervisord")
    p_install.add_argument("--check", action="store_true")
    p_install.set_defaults(func=cmd_install)

    p_status = sub.add_parser("status", help="saúde do sistema no terminal")
    p_status.set_defaults(func=cmd_status)

    p_demo = sub.add_parser("demo", help="popula dados de demonstração")
    p_demo.add_argument("--yes", action="store_true", help="não perguntar")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.locale:
        set_locale(args.locale)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
