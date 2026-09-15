"""Ponto de entrada.

  backup-runner                 abre a interface
  backup-runner tick            decide o que entra na fila (o cron chama isto)
  backup-runner worker          consome a fila (o supervisord chama isto)
  backup-runner install         escreve o cron e gera o conf do supervisord
  backup-runner status          estado do sistema, sem abrir a interface
  backup-runner demo            popula dados de demonstração
  backup-runner self install    instala ou atualiza o próprio programa
  backup-runner self uninstall  remove o programa
  backup-runner self reinstall  reinstala, na mesma referência ou noutra
  backup-runner self status     de onde veio a instalação atual

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


def cmd_self(args: argparse.Namespace) -> int:
    from . import selfmanage as sm

    acao = args.acao or "status"

    if acao == "status":
        return _self_status(sm)
    if acao == "releases":
        releases = sm.list_releases()
        if not releases:
            print("nenhum release publicado ainda")
            return 1
        for tag, data, titulo in releases:
            print(f"{tag:<12} {data}  {titulo}")
        return 0
    if acao == "uninstall":
        return _self_uninstall(sm, args)
    return _self_install(sm, args, reinstalar=acao == "reinstall")


def _self_status(sm) -> int:
    atual = sm.installed()
    if not atual.presente:
        print("✗ não está instalado")
        print(f"  instale com: python3 -m {__package__} self install")
        return 1
    print(f"✓ instalado      {atual.versao or 'versão desconhecida'}")
    print(f"  origem         {atual.origem()}")
    if atual.caminho:
        print(f"  binário        {atual.caminho}")
    if atual.python:
        print(f"  python         {atual.python}")
    if not sm.pipx_path():
        print("  ! o pipx não está no PATH, então atualizar daqui não vai funcionar")
    return 0


def _self_install(sm, args: argparse.Namespace, *, reinstalar: bool) -> int:
    try:
        ref = sm.resolve_ref(args.ref, local=args.local)
    except sm.SelfError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    atual = sm.installed()
    if atual.presente and not reinstalar and not args.force:
        print(f"já instalado: {atual.versao} ({atual.origem()})")
        print(f"para trocar a referência use: {APP_SLUG} self reinstall --ref {args.ref or 'latest'}")
        return 1

    print(f"● instalando do {ref.descricao()}")
    ok, saida = sm.install(ref, force=reinstalar or args.force or atual.presente)
    if not ok:
        print(saida, file=sys.stderr)
        print("✗ a instalação falhou", file=sys.stderr)
        return 1

    depois = sm.installed()
    print(f"✓ {APP_SLUG} {depois.versao or ''} instalado de {ref.descricao()}".rstrip())
    if depois.caminho:
        print(f"  {depois.caminho}")
    else:
        print("  ! o binário não apareceu no PATH; talvez seja preciso reabrir o shell")
    print()
    print(f"  abra com: {APP_SLUG}")
    print(f"  agende:   {APP_SLUG} install")
    return 0


def _self_uninstall(sm, args: argparse.Namespace) -> int:
    atual = sm.installed()
    if not atual.presente:
        print("não está instalado")
        return 1

    if not args.yes:
        print(f"isto remove o programa ({atual.versao or 'versão desconhecida'}).")
        if args.purge:
            print("e APAGA jobs, destinos, segredos e histórico em:")
            for caminho in sm.purge_paths():
                print(f"  {caminho}")
        else:
            print("jobs, destinos e histórico ficam onde estão.")
        resposta = input("continuar? [s/N] ").strip().lower()
        if resposta not in ("s", "sim", "y", "yes"):
            print("cancelado")
            return 1

    # O cron fica órfão se o binário sumir, então sai junto.
    from .health import tick_installed, uninstall_tick

    if tick_installed():
        ok_tick, msg_tick = uninstall_tick()
        print(("✓ crontab: " if ok_tick else "! crontab: ") + msg_tick)

    ok, saida = sm.uninstall()
    if not ok:
        print(saida, file=sys.stderr)
        return 1
    print("✓ programa removido")

    if args.purge:
        import shutil as _shutil

        for caminho in sm.purge_paths():
            if caminho.exists():
                _shutil.rmtree(caminho, ignore_errors=True)
                print(f"✓ apagado {caminho}")
    else:
        print("  jobs, destinos e histórico continuam em ~/.config e ~/.local/share")
        print(f"  para apagar também: {APP_SLUG} self uninstall --purge")

    print("  o worker do supervisord, se existir, precisa sair na mão:")
    print("    sudo rm /etc/supervisor/conf.d/backup-runner.conf")
    print("    sudo supervisorctl reread && sudo supervisorctl update")
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

    p_self = sub.add_parser("self", help="instala, remove e atualiza o próprio programa")
    p_self.add_argument(
        "acao", nargs="?", default="status",
        choices=["install", "uninstall", "reinstall", "status", "releases"],
    )
    p_self.add_argument(
        "--ref", default=None,
        help="latest (padrão), uma tag como v0.2.0, um hash de commit do main, ou main",
    )
    p_self.add_argument("--local", default=None, help="instala de um clone local em vez do GitHub")
    p_self.add_argument("--force", action="store_true", help="instala por cima sem perguntar")
    p_self.add_argument("--purge", action="store_true", help="na remoção, apaga também config e dados")
    p_self.add_argument("--yes", action="store_true", help="não perguntar na remoção")
    p_self.set_defaults(func=cmd_self)

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
