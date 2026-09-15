"""Destinos: pasta local, S3 compatível e SFTP.

O formulário troca de campos conforme o tipo, e o teste usa o mesmo molde de
erro de quatro linhas do resto do app. O segredo aparece mascarado e só revela
por alguns segundos: a cópia é sempre para dentro do campo, nunca para fora.

Apagar um destino exige que nenhum job aponte para ele, porque um job apontando
para destino inexistente falharia às três da manhã, longe de quem apagou.
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, ListItem, ListView, Static

from ...config import decrypt, encrypt
from ...i18n import t
from ...models import Destination, DestKind
from .. import markup as m
from .. import theme as T
from ..widgets import DynamicText, Hero, StatusBar
from ..widgets.form import Choice, Field, Toggle

TIPOS = [("local", "pasta"), ("s3", "s3"), ("sftp", "sftp")]


class DestRow(ListItem):
    def __init__(self, destino: Destination, **kwargs) -> None:
        super().__init__(**kwargs)
        self.destino = destino

    def compose(self) -> ComposeResult:
        d = self.destino
        nome = m.body(d.name) if d.enabled else m.dim(d.name)
        tipo = m.muted(d.kind.value) if d.enabled else m.dim(d.kind.value)
        espaco = max(1, 24 - len(d.name) - len(d.kind.value))
        topo = f"{m.dot(d.enabled)} {nome}" + " " * espaco + tipo
        baixo = m.muted(d.summary()) if d.enabled else m.dim(t("dest.disabled"))
        yield Static(f"{topo}\n   {baixo}", markup=True)


class DestinationsScreen(Screen):
    CSS = """
    DestinationsScreen { background: $br-bg; }
    #corpo-dest { height: 1fr; }
    #painel-lista { width: 38%; }
    #painel-form { width: 1fr; }
    #form { height: auto; padding: 0 1; }
    #uso { height: auto; padding: 1 1; }
    #teste { height: auto; padding: 0 1; }
    """

    BINDINGS = [
        ("up,k", "mover(-1)", "destino"),
        ("down,j", "mover(1)", "destino"),
        ("tab", "proximo_campo", "campo"),
        ("n", "novo", "novo destino"),
        ("ctrl+t", "testar", "testar"),
        ("v", "revelar", "revelar"),
        ("ctrl+s", "salvar", "salvar"),
        ("ctrl+d", "apagar", "apagar"),
        ("escape", "voltar", "voltar"),
        ("question_mark", "ajuda", "ajuda"),
    ]

    def __init__(self, selecionado: str | None = None) -> None:
        super().__init__()
        self._selecionado = selecionado
        self._editando: Destination | None = None
        self._novo = False
        self._resultado_teste: tuple[str, str, dict] | None = None

    def compose(self) -> ComposeResult:
        yield Hero(t("screen.destinations"), id="hero")
        with Horizontal(id="corpo-dest"):
            with Vertical(id="painel-lista", classes="panel"):
                yield ListView(id="lista-dest")
                yield DynamicText(self._montar_uso, id="uso")
            with VerticalScroll(id="painel-form", classes="panel"):
                yield Vertical(id="form")
                yield DynamicText(self._montar_teste, id="teste")
        yield StatusBar(id="status")

    async def on_mount(self) -> None:
        self.query_one("#painel-lista").border_title = t("dest.registered")
        await self.refresh_data()

    # ------------------------------------------------------------------
    async def refresh_data(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        lista = self.query_one("#lista-dest", ListView)
        indice = lista.index or 0
        lista.clear()
        destinos = ctx.destinations.list()
        for destino in destinos:
            lista.append(DestRow(destino))
        if destinos:
            if self._selecionado:
                nomes = [d.name for d in destinos]
                indice = nomes.index(self._selecionado) if self._selecionado in nomes else indice
                self._selecionado = None
            lista.index = min(indice, len(destinos) - 1)
            await self._carregar_form(destinos[lista.index])
        else:
            await self._carregar_form(None)
        self._preencher_hero()
        self._preencher_status()

    def _preencher_hero(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        destinos = ctx.destinations.list()
        hero = self.query_one("#hero", Hero)
        hero.right_top = f"{t('screen.destinations').lower()} {len(destinos)}        esc volta"
        problemas = [d for d in destinos if not d.enabled]
        hero.right_bottom = (
            t("dest.unreachable", nome=problemas[0].name) if problemas
            else "segredos cifrados em disco"
        )
        hero.refresh()

    def _preencher_status(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        destinos = ctx.destinations.list()
        barra = self.query_one("#status", StatusBar)
        barra.worker_state = "ativo" if ctx.worker.running else "parado"
        barra.queue = ctx.queue_size
        barra.tick = ctx.tick_ok
        barra.detail = t("dest.reachable", ok=sum(1 for d in destinos if d.enabled), total=len(destinos))
        barra.hints = m.keys(
            ("↑↓", t("key.move")), ("tab", t("key.field")), ("n", t("key.new_dest")),
            ("^t", t("key.test")), ("v", t("key.reveal")), ("^s", t("key.save")),
            ("^d", t("key.delete")), ("esc", t("key.back")),
        )
        barra.refresh()

    # ------------------------------------------------------------------
    async def _carregar_form(self, destino: Destination | None) -> None:
        self._editando = destino
        self._resultado_teste = None
        form = self.query_one("#form", Vertical)
        # remove_children é assíncrono: sem o await, o mount abaixo corre
        # antes da remoção e colide com os ids do formulário anterior.
        await form.remove_children()
        painel = self.query_one("#painel-form")

        if destino is None:
            painel.border_title = ""
            form.mount(Static(
                "\n" + m.body("  Nenhum destino cadastrado.") + "\n\n"
                + m.muted("      um destino é para onde o backup vai depois de pronto") + "\n"
                + "      " + m.key("n", " criar o primeiro"),
                markup=True,
            ))
            self.query_one("#uso", DynamicText).rebuild()
            self.query_one("#teste", DynamicText).rebuild()
            return

        painel.border_title = t("dest.editing", nome=destino.name)
        form.mount(Static("", markup=True))
        form.mount(Choice(t("dest.type"), TIPOS, destino.kind.value, id="tipo"))
        form.mount(Static("", markup=True))
        form.mount(Field(t("dest.name"), destino.name, campo_id="f-nome"))

        if destino.kind is DestKind.LOCAL:
            livre = ""
            if destino.path and Path(destino.path).parent.exists():
                livre = t("dest.free", size=T.format_bytes(shutil.disk_usage(_pai(destino.path)).free))
            form.mount(Field(t("dest.path"), destino.path, dica=livre, campo_id="f-path"))
            form.mount(Toggle(t("dest.create_missing"), destino.create_missing,
                              dica=t("dest.mode", modo=destino.mode), id="f-criar"))
        elif destino.kind is DestKind.S3:
            form.mount(Field(t("dest.endpoint"), destino.endpoint, campo_id="f-endpoint",
                             placeholder="nyc3.digitaloceanspaces.com"))
            form.mount(Field(t("dest.region"), destino.region, campo_id="f-region"))
            form.mount(Field(t("dest.bucket"), destino.bucket, campo_id="f-bucket"))
            form.mount(Field(t("dest.prefix"), destino.prefix, dica=t("dest.optional"), campo_id="f-prefix"))
            form.mount(Field(t("dest.key"), destino.access_key, campo_id="f-key"))
            form.mount(Field(t("dest.secret"), decrypt(destino.secret_enc) or "", senha=True,
                             dica=t("wiz.reveal_5s"), campo_id="f-secret"))
            form.mount(Static("                    " + m.dim(t("dest.secret_where")), markup=True))
        else:
            form.mount(Field(t("dest.host"), destino.host, campo_id="f-host"))
            form.mount(Field(t("wiz.port"), str(destino.port), campo_id="f-port"))
            form.mount(Field(t("wiz.user"), destino.user, campo_id="f-user"))
            form.mount(Choice(t("dest.auth"),
                              [("key", t("dest.auth_key")), ("password", t("dest.auth_password"))],
                              destino.auth, id="f-auth"))
            if destino.auth == "key":
                existe = Path(destino.private_key).expanduser().exists() if destino.private_key else False
                campo = Field(t("dest.private_key"), destino.private_key, campo_id="f-pkey")
                if destino.private_key and not existe:
                    campo._erro = t("dest.file_missing")
                form.mount(campo)
            else:
                form.mount(Field(t("wiz.password"), decrypt(destino.password_enc) or "", senha=True,
                                 campo_id="f-pass"))
            form.mount(Field(t("dest.remote_path"), destino.remote_path, campo_id="f-remote"))

        form.mount(Static("", markup=True))
        form.mount(Field(t("dest.retention"), str(destino.retention_days),
                         dica=t("dest.job_overrides"), campo_id="f-retencao"))
        form.mount(Static("                    " + m.dim("em dias, contados no próprio destino"), markup=True))

        self.query_one("#uso", DynamicText).rebuild()
        self.query_one("#teste", DynamicText).rebuild()

    def _montar_uso(self, largura: int) -> str:
        destino = self._editando
        if destino is None:
            return ""
        ctx = self.app.ctx  # type: ignore[attr-defined]
        usuarios = ctx.destination_users(destino.name)
        linhas = [m.dim("─" * largura), m.secondary(t("dest.usage")), ""]
        if usuarios:
            chave = "dest.used_by_one" if len(usuarios) == 1 else "dest.used_by"
            linhas.append(m.muted(t(chave, nome=destino.name, n=len(usuarios))))
            linhas.extend("  " + m.body(nome) for nome in usuarios)
            linhas.append("")
            linhas.append(m.dim(t("dest.delete_rule")))
        else:
            linhas.append(m.muted("nenhum job aponta para este destino"))
        return "\n".join(linhas)

    def _montar_teste(self, largura: int) -> str:
        if self._editando is None:
            return ""
        if self._resultado_teste is None:
            return (
                "\n" + m.dim("─" * largura) + "\n" + m.secondary(t("dest.test")) + "\n\n"
                + "  " + m.key("^t", f" {t('wiz.test_untested')}") + "\n"
            )
        estado, mensagem, extra = self._resultado_teste
        linhas = ["", m.dim("─" * largura), m.secondary(t("dest.test")), ""]
        if estado == "ok":
            linhas.append("  " + m.c(f"{T.SYM_OK} {mensagem}", T.SUCCESS))
            linhas.append("  " + m.muted(t("dest.perms_ok")))
        else:
            linhas.append("  " + m.c(f"{T.SYM_FAIL} {t('err.test_failed')}", T.DANGER)
                          + m.muted(f"          {extra.get('quando', '')}"))
            for chave, valor in (
                (t("err.tried"), extra.get("tried", "")),
                (t("err.got"), mensagem),
                (t("err.cause"), extra.get("cause", "")),
                (t("err.fix"), extra.get("fix", "")),
            ):
                if valor:
                    linhas.extend(_linha_erro(chave, valor, largura))
        linhas.append("")
        linhas.append("  " + m.key("^t", f" {t('dest.test_again')}") + "     "
                      + m.key("^s", f" {t('key.save')}") + "     "
                      + m.key("esc", f" {t('key.back')}"))
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    @on(ListView.Highlighted)
    async def _mudou_destino(self, evento: ListView.Highlighted) -> None:
        if isinstance(evento.item, DestRow):
            await self._carregar_form(evento.item.destino)

    @on(Choice.Changed)
    async def _mudou_tipo(self, evento: Choice.Changed) -> None:
        if self._editando is None:
            return
        if evento.choice.id == "tipo":
            self._editando.kind = DestKind(evento.value)
        elif evento.choice.id == "f-auth":
            self._editando.auth = evento.value
        await self._carregar_form(self._editando)

    # ------------------------------------------------------------------
    def action_mover(self, passo: int) -> None:
        lista = self.query_one("#lista-dest", ListView)
        lista.action_cursor_up() if passo < 0 else lista.action_cursor_down()

    def action_proximo_campo(self) -> None:
        self.focus_next()

    async def action_novo(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        base = "destino"
        n = 1
        while ctx.destinations.get(f"{base}-{n}"):
            n += 1
        novo = Destination(name=f"{base}-{n}", kind=DestKind.LOCAL, path=str(Path.home() / "backups"))
        ctx.destinations.put(novo)
        ctx.refresh()
        self._selecionado = novo.name
        await self.refresh_data()

    def action_revelar(self) -> None:
        campos = [f for f in self.query(Field) if f._senha]
        if not campos:
            return
        campo = campos[0]
        campo.input.password = False
        campo.set_hint("! visível por 5s")
        self.set_timer(5.0, lambda: self._esconder(campo))

    def _esconder(self, campo: Field) -> None:
        campo.input.password = True
        campo.set_hint(t("wiz.reveal_5s"))

    def action_testar(self) -> None:
        destino = self._coletar()
        if destino is None:
            return
        self._resultado_teste = _testar(destino)
        self.query_one("#teste", DynamicText).rebuild()

    def _texto(self, ident: str, padrao: str = "") -> str:
        """Valor de um campo que pode não existir no tipo atual do formulário."""
        campos = self.query(f"#{ident}")
        return campos.first(Input).value if campos else padrao

    def _coletar(self) -> Destination | None:
        """Lê o formulário de volta para o objeto em edição."""
        d = self._editando
        if d is None:
            return None
        d.name = self._texto("f-nome", d.name).strip() or d.name

        if d.kind is DestKind.LOCAL:
            d.path = self._texto("f-path", d.path)
            criar = self.query("#f-criar")
            if criar:
                d.create_missing = criar.first(Toggle).valor
        elif d.kind is DestKind.S3:
            d.endpoint = self._texto("f-endpoint", d.endpoint)
            d.region = self._texto("f-region", d.region)
            d.bucket = self._texto("f-bucket", d.bucket)
            d.prefix = self._texto("f-prefix", d.prefix)
            d.access_key = self._texto("f-key", d.access_key)
            segredo = self._texto("f-secret")
            if segredo:
                d.secret_enc = encrypt(segredo)
        else:
            d.host = self._texto("f-host", d.host)
            d.port = _inteiro(self._texto("f-port", str(d.port)), d.port)
            d.user = self._texto("f-user", d.user)
            d.private_key = self._texto("f-pkey", d.private_key)
            senha = self._texto("f-pass")
            if senha:
                d.password_enc = encrypt(senha)
            d.remote_path = self._texto("f-remote", d.remote_path)

        d.retention_days = _inteiro(self._texto("f-retencao", str(d.retention_days)), d.retention_days)
        return d

    async def action_salvar(self) -> None:
        destino = self._coletar()
        if destino is None:
            return
        ctx = self.app.ctx  # type: ignore[attr-defined]
        antigo = self._editando.name if self._editando else None
        if antigo and antigo != destino.name:
            ctx.destinations.delete(antigo)
        ctx.destinations.put(destino)
        ctx.refresh()
        self._selecionado = destino.name
        await self.refresh_data()
        self.notify(f"{destino.name} salvo", severity="information")

    async def action_apagar(self) -> None:
        if self._editando is None:
            return
        ctx = self.app.ctx  # type: ignore[attr-defined]
        usuarios = ctx.destination_users(self._editando.name)
        if usuarios:
            self.notify(
                f"{len(usuarios)} jobs apontam para este destino: {', '.join(usuarios)}",
                severity="error",
                title="apagar exige que nenhum job aponte",
            )
            return
        nome = self._editando.name
        ctx.destinations.delete(nome)
        ctx.refresh()
        await self.refresh_data()
        self.notify(f"{nome} apagado", severity="information")

    def action_voltar(self) -> None:
        self.app.ctx.refresh()  # type: ignore[attr-defined]
        self.app.pop_screen()

    def action_ajuda(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen(self.BINDINGS, t("screen.destinations")))


# ----------------------------------------------------------------------------

def _linha_erro(rotulo: str, texto: str, largura: int) -> list[str]:
    """Molde de erro: rótulo numa coluna fixa, texto quebrado embaixo.

    O mesmo molde vale no teste de destino, na conexão mysql e na execução que
    falhou, para a pessoa aprender a ler o erro uma vez.
    """
    disponivel = largura - 22
    pedacos = _quebrar(texto, max(20, disponivel))
    linhas = ["  " + m.secondary(rotulo.ljust(18)) + m.body(pedacos[0])]
    linhas.extend("  " + " " * 18 + m.body(p) for p in pedacos[1:])
    return linhas


def _inteiro(texto: str, padrao: int) -> int:
    try:
        return int(texto)
    except (TypeError, ValueError):
        return padrao


def _quebrar(texto: str, largura: int) -> list[str]:
    palavras = " ".join(texto.split()).split(" ")
    linhas, atual = [], ""
    for palavra in palavras:
        if len(atual) + len(palavra) + 1 > largura:
            linhas.append(atual)
            atual = palavra
        else:
            atual = f"{atual} {palavra}".strip()
    if atual:
        linhas.append(atual)
    return linhas


def _pai(caminho: str) -> str:
    p = Path(caminho).expanduser()
    while not p.exists() and p != p.parent:
        p = p.parent
    return str(p)


def _testar(destino: Destination) -> tuple[str, str, dict]:
    """Teste real do que dá para testar sem rede: a pasta local.

    S3 e SFTP dependem de boto3 e paramiko com credencial de verdade, e entram
    junto com o worker. Até lá o teste diz isso em vez de fingir sucesso, que
    seria a pior resposta possível numa tela de backup.
    """
    agora = dt.datetime.now().strftime("%H:%M:%S")
    if destino.kind is DestKind.LOCAL:
        caminho = Path(destino.path).expanduser()
        try:
            if not caminho.exists():
                if not destino.create_missing:
                    return ("erro", "o diretório não existe", {
                        "tried": f"escrever em {caminho}",
                        "cause": "o caminho não existe e 'criar se faltar' está desligado",
                        "fix": f"mkdir -p {caminho}",
                        "quando": agora,
                    })
                caminho.mkdir(parents=True, exist_ok=True, mode=0o700)
            sonda = caminho / f".probe-{dt.datetime.now().strftime('%m%d%H%M%S')}"
            inicio = dt.datetime.now()
            sonda.write_text("backup-runner")
            sonda.unlink()
            levou = (dt.datetime.now() - inicio).total_seconds()
            return ("ok", t("dest.test_ok", arquivo=sonda.name, t=f"{levou:.1f} s").replace(".", ","), {})
        except OSError as exc:
            return ("erro", str(exc), {
                "tried": f"escrever e apagar uma sonda em {caminho}",
                "cause": "permissão ou disco",
                "fix": f"confira as permissões de {caminho}",
                "quando": agora,
            })
    tipo = "S3" if destino.kind is DestKind.S3 else "SFTP"
    return ("erro", f"o teste de {tipo} entra junto com o worker", {
        "tried": f"conectar em {destino.location()}",
        "cause": "a camada de envio ainda não foi implementada",
        "fix": "por enquanto, teste destinos do tipo pasta",
        "quando": agora,
    })
