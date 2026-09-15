"""Wizard de job, seis passos dentro da tela cheia.

Nada de prompt fora da tela: o indicador de passo fica sempre visível, e o
rascunho vive em memória até a revisão. Só o último passo grava, e é por isso
que o cabeçalho diz "nada foi salvo ainda" até lá.

O passo das tabelas é o mais difícil do app, e o desenho reflete isso:
marcação à mão e marcação por regex usam formas diferentes ([✓] e [▪]), o
resumo conta as origens separadas, e a marca à mão vence a regex nos dois
sentidos. Escolher doze tabelas entre duzentas sem contagem confiável é onde a
pessoa erra.
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Static

from ... import mysql
from ...config import encrypt
from ...i18n import t
from ...models import (
    ArchiveFormat,
    ExcludePattern,
    FilesSource,
    Job,
    JobDestination,
    MySQLSource,
    SourceKind,
)
from ...schedule import humanize, is_valid, next_run
from .. import markup as m
from .. import theme as T
from ..widgets import DynamicText, Hero, StatusBar
from ..widgets.form import Choice, Field, Toggle

PASSOS_MYSQL = ["wiz.s_connection", "wiz.s_tables", "wiz.s_schedule",
                "wiz.s_destinations", "wiz.s_notices", "wiz.s_review"]
PASSOS_ARQUIVOS = ["wiz.s_origin", "wiz.s_excludes", "wiz.s_schedule",
                   "wiz.s_destinations", "wiz.s_notices", "wiz.s_review"]


def escolher_fonte(app) -> None:
    app.push_screen(EscolherFonte(), lambda tipo: _abrir(app, tipo))


def _abrir(app, tipo: str | None) -> None:
    if tipo:
        app.push_screen(WizardScreen(SourceKind(tipo)))


class EscolherFonte(ModalScreen[str]):
    CSS = """
    EscolherFonte { align: center middle; }
    #caixa-fonte { width: 72; height: auto; border: round $br-primary;
                   background: $br-surface; padding: 1 2; }
    """

    BINDINGS = [("escape", "cancelar", "cancelar"), ("m", "mysql", "mysql"), ("a", "arquivos", "arquivos")]

    def compose(self) -> ComposeResult:
        with Vertical(id="caixa-fonte"):
            yield Static("\n".join([
                "",
                m.primary("O que este job vai guardar?", bold=True),
                "",
                "  " + m.key("m", " um banco MySQL") + m.dim("     dump com mysqldump"),
                "  " + m.key("a", " um diretório") + m.dim("       tar.gz ou zip do diretório"),
                "",
                m.dim("  um job tem uma fonte só: banco e storage do mesmo projeto"),
                m.dim("  são dois jobs, e é isso que deixa o histórico legível"),
                "",
                "  " + m.key("esc", " cancelar"),
                "",
            ]), markup=True)

    def action_mysql(self) -> None:
        self.dismiss("mysql")

    def action_arquivos(self) -> None:
        self.dismiss("files")

    def action_cancelar(self) -> None:
        self.dismiss("")


class WizardScreen(Screen):
    CSS = """
    WizardScreen { background: $br-bg; }
    #passos { height: 2; padding: 0 1; }
    #conteudo { height: 1fr; }
    #painel-passo { width: 1fr; }
    #form-passo { height: auto; padding: 0 1; }
    #lado { width: 36; }
    #rodape-wiz { height: 1; padding: 0 1; }
    """

    BINDINGS = [
        ("tab", "proximo_campo", "campo"),
        ("shift+tab", "campo_anterior", "campo"),
        ("ctrl+t", "testar", "testar"),
        ("v", "revelar", "revelar"),
        ("enter", "avancar", "avançar"),
        ("escape", "voltar", "voltar"),
        ("ctrl+s", "salvar", "salvar"),
        ("question_mark", "ajuda", "ajuda"),
    ]

    def __init__(self, tipo: SourceKind, job: Job | None = None) -> None:
        super().__init__()
        self.tipo = tipo
        self.passos = PASSOS_MYSQL if tipo is SourceKind.MYSQL else PASSOS_ARQUIVOS
        self.passo = 0
        self.editando = job is not None
        fonte = MySQLSource() if tipo is SourceKind.MYSQL else FilesSource()
        self.job = job or Job(name="", source=fonte, created=dt.date.today().isoformat())
        self.teste: tuple[str, str] | None = None
        self.tabelas: list[mysql.TableInfo] = []
        self.busca = ""
        self.cursor_tabela = 0
        self.previa: dict | None = None

    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        titulo = t("screen.new_job_mysql") if self.tipo is SourceKind.MYSQL else t("screen.new_job_files")
        yield Hero(titulo, id="hero")
        yield DynamicText(self._montar_passos, id="passos")
        with Horizontal(id="conteudo"):
            with VerticalScroll(id="painel-passo", classes="panel"):
                yield Vertical(id="form-passo")
            with VerticalScroll(id="lado", classes="panel"):
                yield DynamicText(self._montar_lado, id="lado-texto")
        yield Static(id="rodape-wiz", markup=True)
        yield StatusBar(id="status")

    async def on_mount(self) -> None:
        await self._montar_conteudo()

    # ------------------------------------------------------------------
    def _montar_passos(self, largura: int) -> str:
        """Trilha de passos mais barra de progresso."""
        partes = []
        for i, chave in enumerate(self.passos):
            nome = f"{i + 1} {t(chave)}"
            if i < self.passo:
                partes.append(m.c(f"{T.SYM_OK} {nome}", T.SUCCESS))
            elif i == self.passo:
                partes.append(m.primary(f"▌{nome}", bold=True))
            else:
                partes.append(m.dim(f" {nome}"))
        cheios = int((self.passo + 1) / len(self.passos) * largura)
        barra = m.c(T.BAR_FULL * cheios, T.PRIMARY) + m.dim("▒" * max(0, largura - cheios))
        return "  ".join(partes) + "\n" + barra

    async def _montar_conteudo(self) -> None:
        form = self.query_one("#form-passo", Vertical)
        await form.remove_children()
        chave = self.passos[self.passo]
        painel = self.query_one("#painel-passo")
        painel.border_title = t(chave)

        montar = {
            "wiz.s_connection": self._passo_conexao,
            "wiz.s_origin": self._passo_origem,
            "wiz.s_tables": self._passo_tabelas,
            "wiz.s_excludes": self._passo_exclusoes,
            "wiz.s_schedule": self._passo_agenda,
            "wiz.s_destinations": self._passo_destinos,
            "wiz.s_notices": self._passo_avisos,
            "wiz.s_review": self._passo_revisao,
        }[chave]
        montar(form)

        self._atualizar_cabecalho()
        self.query_one("#passos", DynamicText).rebuild()
        self.query_one("#lado-texto", DynamicText).rebuild()

    def _atualizar_cabecalho(self) -> None:
        hero = self.query_one("#hero", Hero)
        hero.right_top = f"novo job        {t('wiz.step', n=self.passo + 1, total=len(self.passos))}"
        hero.right_bottom = (
            t("wiz.nothing_saved") if self.passo == len(self.passos) - 1 else t("wiz.esc_cancels")
        )
        hero.refresh()

        barra = self.query_one("#status", StatusBar)
        ctx = self.app.ctx  # type: ignore[attr-defined]
        barra.worker_state = "ativo" if ctx.worker.running else "parado"
        barra.tick = ctx.tick_ok
        barra.detail = (
            t("wiz.draft_complete") if self.passo == len(self.passos) - 1 else t("wiz.draft_memory")
        )
        barra.hints = self._teclas()
        barra.refresh()

        self.query_one("#rodape-wiz", Static).update(self._rodape())

    def _teclas(self) -> str:
        chave = self.passos[self.passo]
        if chave in ("wiz.s_connection",):
            return m.keys(("tab", t("key.field")), ("^t", t("key.test")), ("v", t("key.reveal")),
                          ("enter", t("key.next")), ("esc", t("key.cancel")))
        if chave == "wiz.s_tables":
            return m.keys(("↑↓", t("key.move")), ("espaço", t("key.mark")), ("/", t("key.search")),
                          ("enter", t("key.next")), ("esc", t("key.back")))
        if chave == "wiz.s_review":
            return m.keys(("1..5", t("key.step_back")), ("enter", t("key.save")),
                          ("^s", t("key.save_run")), ("esc", t("key.cancel")))
        return m.keys(("tab", t("key.field")), ("enter", t("key.next")), ("esc", t("key.back")))

    def _rodape(self) -> str:
        chave = self.passos[self.passo]
        if chave == "wiz.s_connection" and (self.teste is None or self.teste[0] != "ok"):
            return m.dim(t("wiz.needs_test"))
        if chave == "wiz.s_review":
            return (
                m.primary(f"▏ {t('rev.save')}  enter", bold=True) + "     "
                + m.body(f"▏ {t('rev.save_run')} ctrl+s") + "     "
                + m.muted(f"▏ {t('key.cancel')} esc")
            )
        return ""

    # ------------------------------------------------------------------
    # Passo 1, MySQL
    # ------------------------------------------------------------------
    def _passo_conexao(self, form: Vertical) -> None:
        fonte: MySQLSource = self.job.source  # type: ignore[assignment]
        binario = shutil.which("mysqldump")
        versao = _versao_mysqldump() if binario else ""
        form.mount(Static("", markup=True))
        form.mount(Field(t("wiz.job_name"), self.job.name, dica=t("wiz.name_chars"), campo_id="w-nome"))
        form.mount(Static("", markup=True))
        form.mount(Field(t("wiz.host"), fonte.host, campo_id="w-host", placeholder="db-01.local"))
        form.mount(Field(t("wiz.port"), str(fonte.port), dica=t("wiz.port_default"), campo_id="w-porta"))
        form.mount(Field(t("wiz.user"), fonte.user, campo_id="w-user"))
        form.mount(Field(t("wiz.password"), "", senha=True, dica=t("wiz.reveal_5s"), campo_id="w-senha"))
        form.mount(Field(t("wiz.database"), fonte.database, dica=t("wiz.one_db"), campo_id="w-banco"))
        form.mount(Static("", markup=True))
        campo_bin = Field(t("wiz.binary"), "mysqldump", campo_id="w-bin")
        if binario:
            campo_bin._dica = t("wiz.in_path", versao=versao)
        else:
            campo_bin._erro = "não encontrado no PATH"
        form.mount(campo_bin)
        form.mount(Static("", markup=True))
        form.mount(DynamicText(self._montar_teste_conexao, id="w-teste"))

    def _montar_teste_conexao(self, largura: int) -> str:
        linhas = [m.dim("─" * largura), m.secondary(t("wiz.test_title")), ""]
        if self.teste is None:
            linhas.append("  " + m.key("^t", f" {t('wiz.test_untested')}"))
        else:
            estado, mensagem = self.teste
            if estado == "ok":
                linhas.append("  " + m.c(f"{T.SYM_OK} {mensagem}", T.SUCCESS))
            else:
                linhas.append("  " + m.c(f"{T.SYM_FAIL} {mensagem}", T.DANGER))
                linhas.append("  " + m.dim("confira usuário, senha e se o host aceita a sua rede"))
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    # Passo 1, arquivos
    # ------------------------------------------------------------------
    def _passo_origem(self, form: Vertical) -> None:
        fonte: FilesSource = self.job.source  # type: ignore[assignment]
        form.mount(Static("", markup=True))
        form.mount(Field(t("wiz.job_name"), self.job.name, campo_id="w-nome"))
        form.mount(Static("", markup=True))
        campo = Field(t("files.directory"), fonte.path, campo_id="w-path",
                      placeholder="/srv/app/storage/uploads")
        caminho = Path(fonte.path).expanduser() if fonte.path else None
        if caminho and caminho.is_dir():
            campo._dica = t("files.exists", n=_profundidade(caminho))
        elif fonte.path:
            campo._erro = "não existe ou não é diretório"
        form.mount(campo)
        form.mount(Static("                    " + m.dim(t("files.tab_completes")), markup=True))
        form.mount(Static("", markup=True))
        form.mount(Toggle(t("files.follow_links"), fonte.follow_links, id="w-links"))
        form.mount(Choice(t("files.format"),
                          [(ArchiveFormat.TARGZ.value, "tar.gz"), (ArchiveFormat.ZIP.value, "zip")],
                          fonte.archive_format.value, id="w-formato"))
        form.mount(Static("                    " + m.dim(
            "tar.gz " + t("files.format_why") + "; zip abre no Windows"), markup=True))

    def _passo_exclusoes(self, form: Vertical) -> None:
        fonte: FilesSource = self.job.source  # type: ignore[assignment]
        form.mount(Static("\n" + m.secondary(t("files.excludes")) + "   " + m.muted(t("files.add")), markup=True))
        form.mount(Static("", markup=True))
        for i, padrao in enumerate(fonte.excludes):
            form.mount(Toggle(padrao.pattern, padrao.enabled, id=f"w-ex-{i}"))
        form.mount(Static("", markup=True))
        form.mount(Field("novo padrão", "", campo_id="w-novo-padrao", placeholder="*.tmp"))
        form.mount(Static("\n" + m.dim("  " + t("files.toggle_hint")), markup=True))

    # ------------------------------------------------------------------
    # Passo 2, tabelas
    # ------------------------------------------------------------------
    def _passo_tabelas(self, form: Vertical) -> None:
        form.mount(Field("/", self.busca, campo_id="w-busca", placeholder="filtrar por nome"))
        form.mount(DynamicText(self._montar_tabelas, id="w-tabelas"))
        fonte: MySQLSource = self.job.source  # type: ignore[assignment]
        form.mount(Static("", markup=True))
        form.mount(Field(t("tab.regex_auto"), fonte.ignore_regex, campo_id="w-regex"))
        form.mount(Static("\n" + m.dim("  " + t("tab.regex_why")), markup=True))

    def _filtradas(self) -> list[mysql.TableInfo]:
        if not self.busca:
            return self.tabelas
        alvo = self.busca.lower().lstrip("/")
        return [tab for tab in self.tabelas if alvo in tab.name.lower()]

    def _montar_tabelas(self, largura: int) -> str:
        if not self.tabelas:
            return "\n".join([
                "",
                m.muted("  Nenhuma tabela listada ainda."),
                "",
                m.dim("  a lista vem do banco no passo 1, e precisa do teste de conexão"),
                "  " + m.key("esc", " voltar ao passo 1"),
            ])
        fonte: MySQLSource = self.job.source  # type: ignore[assignment]
        visiveis = self._filtradas()
        por_regex, por_mao = fonte.resolve_ignored(tab.name for tab in visiveis)
        conj_regex, conj_mao = set(por_regex), set(por_mao)

        linhas = [m.muted(f"  {t('tab.matching', n=len(visiveis), total=len(self.tabelas))}"), ""]
        janela = visiveis[max(0, self.cursor_tabela - 8) : max(0, self.cursor_tabela - 8) + 14]
        for tab in janela:
            i = visiveis.index(tab)
            foco = i == self.cursor_tabela
            marca = m.check(tab.name in conj_mao, tab.name in conj_regex)
            prefixo = m.c(T.SYM_HINT, T.PRIMARY) if foco else " "
            nome = tab.name[:22].ljust(24)
            linhas_txt = (t("tab.view") if tab.is_view else f"{T.format_count(tab.rows)} {t('tab.rows')}").ljust(18)
            peso = T.format_bytes(tab.bytes) if not tab.is_view else T.SYM_NONE
            linhas.append(
                f"{prefixo} {marca} {m.body(nome, bold=foco)}{m.muted(linhas_txt)}{m.muted(peso)}"
            )
        if len(visiveis) > len(janela):
            linhas.append("")
            linhas.append(m.muted(f"  {t('tab.visible', n=len(janela), total=len(visiveis))}"))
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    def _passo_agenda(self, form: Vertical) -> None:
        form.mount(Static("", markup=True))
        campo = Field("cron", self.job.schedule, campo_id="w-cron")
        campo._dica = humanize(self.job.schedule)
        form.mount(campo)
        form.mount(Static("                    " + m.dim("minuto hora dia mês dia-da-semana"), markup=True))
        form.mount(Static("", markup=True))
        form.mount(Field("fuso", self.job.timezone, campo_id="w-tz"))
        form.mount(Static("", markup=True))
        form.mount(Field("janela de atraso", str(self.job.catch_up_window_minutes),
                         dica="minutos", campo_id="w-janela"))
        form.mount(Static(
            "                    " + m.dim("dentro dela, uma janela perdida ainda roda ao ligar a máquina"),
            markup=True))
        form.mount(Static("", markup=True))
        form.mount(Field("tempo limite", str(self.job.timeout_minutes), dica="minutos", campo_id="w-timeout"))
        form.mount(Static(
            "                    " + m.dim("estourou, o worker mata e passa para o próximo da fila"),
            markup=True))
        form.mount(Static("", markup=True))
        form.mount(Field("silêncio aceito", str(self.job.stale_after_hours), dica="horas", campo_id="w-stale"))
        form.mount(Static(
            "                    " + m.dim("sem sucesso por mais que isso, você recebe aviso"),
            markup=True))

    def _passo_destinos(self, form: Vertical) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        destinos = ctx.destinations.list()
        form.mount(Static("", markup=True))
        if not destinos:
            form.mount(Static("\n".join([
                m.body("  Nenhum destino cadastrado."),
                "",
                m.muted("      sem destino, o backup fica só no staging e é apagado depois"),
                "      " + m.key("esc", " voltar") + m.dim("   e criar um destino antes"),
            ]), markup=True))
            return
        escolhidos = {d.name: d for d in self.job.destinations}
        for destino in destinos:
            ligado = destino.name in escolhidos
            form.mount(Toggle(
                destino.name, ligado,
                dica=f"{destino.location()}",
                id=f"w-dest-{destino.name}",
            ))
            dias = escolhidos[destino.name].days(destino) if ligado else destino.retention_days
            form.mount(Field("    manter por", str(dias), dica="dias neste destino",
                             campo_id=f"w-ret-{destino.name}"))
        form.mount(Static("\n" + m.dim("  a retenção é contada no destino, pela data na pasta da execução"), markup=True))

    def _passo_avisos(self, form: Vertical) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        from ...models import CHANNELS, NOTIFY_EVENTS

        padrao = ctx.settings.notify_global
        form.mount(Static("\n" + m.muted("  o job herda o padrão global, e pode sobrescrever depois"), markup=True))
        form.mount(Static("", markup=True))
        for evento in NOTIFY_EVENTS:
            canais = [c.value for c in CHANNELS if padrao.resolve(evento, c, padrao)]
            nome = t(f"notif.ev_{evento.value}")
            form.mount(Static(
                f"  {m.secondary(nome.ljust(30))}"
                + (m.body(", ".join(canais)) if canais else m.dim("nenhum canal")),
                markup=True,
            ))
        form.mount(Static("\n" + m.dim("  depois de salvar, a tecla a no dashboard abre a matriz deste job"), markup=True))

    def _passo_revisao(self, form: Vertical) -> None:
        form.mount(DynamicText(self._montar_revisao, id="w-revisao"))

    def _montar_revisao(self, largura: int) -> str:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        job = self.job
        rotulo = lambda x: m.secondary(x.ljust(18))
        linhas = ["", ]

        if job.kind is SourceKind.MYSQL:
            fonte: MySQLSource = job.source  # type: ignore[assignment]
            linhas.append("  " + rotulo(t("rev.source"))
                          + m.body(f"mysql, {fonte.database} @ {fonte.host}:{fonte.port}, usuário {fonte.user}")
                          + m.muted("   1 editar"))
            if self.tabelas:
                por_regex, por_mao = fonte.resolve_ignored(tab.name for tab in self.tabelas)
                mantidas = len(self.tabelas) - len(por_regex) - len(por_mao)
                linhas.append("")
                linhas.append("  " + rotulo(t("rev.tables"))
                              + m.body(t("rev.tables_line", kept=mantidas, total=len(self.tabelas),
                                         ignored=len(por_regex) + len(por_mao)))
                              + m.muted("   2 editar"))
                linhas.append("  " + " " * 18 + m.c(T.CHECK_RULE, T.SECONDARY)
                              + m.muted(f" {len(por_regex)} pela regex {fonte.ignore_regex}"))
                if por_mao:
                    linhas.append("  " + " " * 18 + m.c(T.CHECK_MANUAL, T.PRIMARY)
                                  + m.muted(f" {len(por_mao)} à mão: {', '.join(por_mao[:4])}"))
        else:
            fonte_f: FilesSource = job.source  # type: ignore[assignment]
            linhas.append("  " + rotulo(t("rev.source"))
                          + m.body(f"arquivos, {fonte_f.path}") + m.muted("   1 editar"))
            linhas.append("  " + " " * 18
                          + m.muted(f"{fonte_f.archive_format.value}, "
                                    f"{len(fonte_f.active_excludes())} exclusões ativas"))

        linhas.append("")
        proxima = next_run(job.schedule)
        linhas.append("  " + rotulo(t("rev.schedule"))
                      + m.body(f"{humanize(job.schedule)}, {job.timezone}") + m.muted("   3 editar"))
        if proxima:
            falta = (proxima - dt.datetime.now()).total_seconds()
            linhas.append("  " + " " * 18 + m.muted(
                t("rev.first_run", quando=f"{proxima.strftime('%d/%m %H:%M')}, em {T.format_relative(falta)}")))

        linhas.append("")
        if job.destinations:
            for i, jd in enumerate(job.destinations):
                destino = ctx.destinations.get(jd.name)
                prefixo = rotulo(t("rev.source").replace(t("rev.source"), t("dash.destinations"))) if i == 0 else " " * 18
                linhas.append("  " + prefixo + m.secondary(jd.name.ljust(14))
                              + m.body((destino.location() if destino else "?")[:28].ljust(30))
                              + m.muted(f"manter {jd.days(destino)}d")
                              + (m.muted("   4 editar") if i == 0 else ""))
        else:
            linhas.append("  " + rotulo(t("dash.destinations"))
                          + m.c(f"{T.SYM_WARN} nenhum, o artefato será descartado", T.WARNING)
                          + m.muted("   4 editar"))

        linhas.append("")
        linhas.append("  " + rotulo(t("dash.notices")) + m.body("herdado do padrão global")
                      + m.muted("   5 editar"))

        linhas.append("")
        linhas.append(m.dim("─" * largura))
        linhas.extend(self._checagens())
        return "\n".join(linhas)

    def _checagens(self) -> list[str]:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        linhas = []
        if self.job.kind is SourceKind.MYSQL:
            if self.teste and self.teste[0] == "ok":
                linhas.append(m.inline("ok", f"conexão testada, {len(self.job.destinations)} destinos escolhidos"))
            else:
                linhas.append(m.inline("warn", "a conexão não foi testada neste rascunho"))
        livre = ctx.staging.free
        linhas.append(m.inline("info", f"staging com {T.format_bytes(livre)} livres"))
        if not self.job.destinations:
            linhas.append(m.inline("warn", "sem destino, o artefato é apagado ao fim do job"))
        if not ctx.tick_ok:
            linhas.append(m.inline("warn", "o tick não está no crontab, então este job não vai rodar sozinho"))
        return linhas

    # ------------------------------------------------------------------
    def _montar_lado(self, largura: int) -> str:
        """Painel lateral: resumo do passo, quando ele tem um."""
        chave = self.passos[self.passo]
        if chave == "wiz.s_tables":
            return self._lado_tabelas(largura)
        if chave in ("wiz.s_origin", "wiz.s_excludes"):
            return self._lado_previa(largura)
        if chave == "wiz.s_review":
            return ""
        return "\n" + m.dim("  nada a conferir neste passo")

    def _lado_tabelas(self, largura: int) -> str:
        fonte: MySQLSource = self.job.source  # type: ignore[assignment]
        if not self.tabelas:
            return "\n" + m.dim("  o resumo aparece com a lista")
        por_regex, por_mao = fonte.resolve_ignored(tab.name for tab in self.tabelas)
        ignoradas = len(por_regex) + len(por_mao)
        mantidas = len(self.tabelas) - ignoradas
        peso = sum(tab.bytes for tab in self.tabelas if tab.name not in set(por_regex) | set(por_mao))
        linhas = [
            "",
            m.secondary(t("tab.summary")),
            "",
            f"  {m.body(str(len(self.tabelas)).ljust(5))}{m.muted(t('tab.in_db'))}",
            f"  {m.body(str(len(por_regex)).ljust(5))}{m.muted(t('tab.by_regex'))}",
            f"  {m.body(str(len(por_mao)).ljust(5))}{m.muted(t('tab.by_hand'))}",
            f"  {m.body(str(len(fonte.keep_manual)).ljust(5))}{m.muted(t('tab.unmarked_by_hand'))}",
            f"  {m.body(str(ignoradas).ljust(5))}{m.muted(t('tab.ignored_total'))}",
            f"  {m.body(str(mantidas).ljust(5))}{m.muted(t('tab.in_dump'))}",
            f"  {m.muted(t('tab.estimated', size=T.format_bytes(peso)))}",
            "",
            m.dim("─" * largura),
            m.secondary(t("tab.legend")),
            f"  {m.c(T.CHECK_MANUAL, T.PRIMARY)}  {m.muted(t('tab.legend_manual'))}",
            f"  {m.c(T.CHECK_RULE, T.SECONDARY)}  {m.muted(t('tab.legend_rule'))}",
            f"  {m.dim(T.CHECK_OFF)}  {m.muted(t('tab.legend_off'))}",
            "",
            m.dim("─" * largura),
            m.secondary(t("tab.attention")),
            "",
            m.inline("warn", t("tab.hand_wins")),
        ]
        return "\n".join(linhas)

    def _lado_previa(self, largura: int) -> str:
        if self.previa is None:
            return "\n".join([
                "",
                m.secondary(t("files.preview")),
                "",
                m.dim("  ainda não medida"),
                "  " + m.key("p", " calcular agora"),
                "",
                m.dim("  a prévia percorre o diretório"),
                m.dim("  de verdade: é medida, não"),
                m.dim("  estimativa"),
            ])
        p = self.previa
        return "\n".join([
            "",
            m.secondary(t("files.preview")),
            "",
            f"  {m.body(T.format_count(p['arquivos']))} {m.muted(t('files.files'))}",
            f"  {m.body(T.format_bytes(p['bytes']))} {m.muted('em disco')}",
            "",
            f"  {m.muted(t('files.excluded', n=T.format_count(p['excluidos']), size=T.format_bytes(p['bytes_excluidos'])))}",
            "",
            m.dim("─" * largura),
            m.muted("  " + t("files.measured", t=_segundos(p["t"]))),
            "  " + m.key("p", " recalcular"),
        ])

    # ------------------------------------------------------------------
    # Ações
    # ------------------------------------------------------------------
    def action_proximo_campo(self) -> None:
        self.focus_next()

    def action_campo_anterior(self) -> None:
        self.focus_previous()

    def action_revelar(self) -> None:
        campos = [f for f in self.query(Field) if f._senha]
        if not campos:
            return
        campo = campos[0]
        campo.input.password = False
        campo.set_hint("! visível por 5s")
        self.set_timer(5.0, lambda: (setattr(campo.input, "password", True), campo.set_hint(t("wiz.reveal_5s"))))

    def action_testar(self) -> None:
        if self.passos[self.passo] != "wiz.s_connection":
            return
        self._ler_conexao()
        fonte: MySQLSource = self.job.source  # type: ignore[assignment]
        senha = self._texto("w-senha")
        conn = mysql.Connection(
            host=fonte.host, port=fonte.port, user=fonte.user,
            password=senha, database=fonte.database,
        )
        try:
            ok, mensagem = mysql.test_connection(conn)
        except mysql.MySQLError as exc:
            ok, mensagem = False, str(exc)
        if ok:
            try:
                self.tabelas = mysql.list_tables_info(conn)
            except mysql.MySQLError:
                self.tabelas = []
            self.teste = ("ok", t("wiz.test_ok", versao=mensagem, n=len(self.tabelas)))
            if senha:
                fonte.password_enc = encrypt(senha)
        else:
            self.teste = ("erro", mensagem)
        self.query_one("#w-teste", DynamicText).rebuild()
        self._atualizar_cabecalho()

    def _texto(self, ident: str, padrao: str = "") -> str:
        campos = self.query(f"#{ident}")
        return campos.first(Input).value if campos else padrao

    def _ler_conexao(self) -> None:
        fonte: MySQLSource = self.job.source  # type: ignore[assignment]
        self.job.name = self._texto("w-nome", self.job.name).strip()
        fonte.host = self._texto("w-host", fonte.host)
        fonte.port = _inteiro(self._texto("w-porta", str(fonte.port)), fonte.port)
        fonte.user = self._texto("w-user", fonte.user)
        fonte.database = self._texto("w-banco", fonte.database)

    def _ler_passo(self) -> bool:
        """Lê o passo atual para o rascunho. Devolve False se não pode avançar."""
        chave = self.passos[self.passo]
        if chave == "wiz.s_connection":
            self._ler_conexao()
            if not self.job.name:
                self.notify("o job precisa de um nome", severity="error")
                return False
            if self.teste is None or self.teste[0] != "ok":
                self.notify("teste a conexão antes de escolher as tabelas", severity="warning")
                return False
        elif chave == "wiz.s_origin":
            fonte: FilesSource = self.job.source  # type: ignore[assignment]
            self.job.name = self._texto("w-nome", self.job.name).strip()
            fonte.path = self._texto("w-path", fonte.path)
            links = self.query("#w-links")
            if links:
                fonte.follow_links = links.first(Toggle).valor
            formato = self.query("#w-formato")
            if formato:
                fonte.archive_format = ArchiveFormat(formato.first(Choice).valor)
            if not self.job.name:
                self.notify("o job precisa de um nome", severity="error")
                return False
            if not Path(fonte.path).expanduser().is_dir():
                self.notify("o diretório de origem não existe", severity="error")
                return False
        elif chave == "wiz.s_tables":
            fonte_m: MySQLSource = self.job.source  # type: ignore[assignment]
            fonte_m.ignore_regex = self._texto("w-regex", fonte_m.ignore_regex)
        elif chave == "wiz.s_excludes":
            fonte_f: FilesSource = self.job.source  # type: ignore[assignment]
            for i, padrao in enumerate(fonte_f.excludes):
                campo = self.query(f"#w-ex-{i}")
                if campo:
                    padrao.enabled = campo.first(Toggle).valor
            novo = self._texto("w-novo-padrao").strip()
            if novo:
                fonte_f.excludes.append(ExcludePattern(novo, True))
        elif chave == "wiz.s_schedule":
            cron = self._texto("w-cron", self.job.schedule)
            if not is_valid(cron):
                self.notify("o cron precisa de cinco campos válidos", severity="error")
                return False
            self.job.schedule = cron
            self.job.timezone = self._texto("w-tz", self.job.timezone)
            self.job.catch_up_window_minutes = _inteiro(
                self._texto("w-janela", str(self.job.catch_up_window_minutes)), self.job.catch_up_window_minutes)
            self.job.timeout_minutes = _inteiro(
                self._texto("w-timeout", str(self.job.timeout_minutes)), self.job.timeout_minutes)
            self.job.stale_after_hours = _inteiro(
                self._texto("w-stale", str(self.job.stale_after_hours)), self.job.stale_after_hours)
        elif chave == "wiz.s_destinations":
            ctx = self.app.ctx  # type: ignore[attr-defined]
            escolhidos = []
            for destino in ctx.destinations.list():
                campo = self.query(f"#w-dest-{destino.name}")
                if campo and campo.first(Toggle).valor:
                    dias = _inteiro(self._texto(f"w-ret-{destino.name}", str(destino.retention_days)),
                                    destino.retention_days)
                    escolhidos.append(JobDestination(destino.name, dias))
            self.job.destinations = escolhidos
        return True

    async def action_avancar(self) -> None:
        if self.passos[self.passo] == "wiz.s_review":
            await self._salvar(rodar=False)
            return
        if not self._ler_passo():
            return
        self.passo = min(self.passo + 1, len(self.passos) - 1)
        await self._montar_conteudo()

    async def action_voltar(self) -> None:
        if self.passo == 0:
            self.app.pop_screen()
            return
        self._ler_passo()
        self.passo -= 1
        await self._montar_conteudo()

    async def action_salvar(self) -> None:
        if self.passos[self.passo] == "wiz.s_review":
            await self._salvar(rodar=True)

    async def _salvar(self, *, rodar: bool) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        if not self.job.name:
            self.notify("o job precisa de um nome", severity="error")
            return
        if ctx.jobs.get(self.job.name) and not self.editando:
            self.notify(f"já existe um job chamado {self.job.name}", severity="error")
            return
        ctx.jobs.put(self.job)
        if rodar:
            ctx.state.enqueue(self.job.name, dt.datetime.now())
        ctx.refresh()
        self.notify(
            f"{self.job.name} salvo" + (" e enfileirado" if rodar else ""),
            severity="information",
        )
        self.app.pop_screen()
        self.app.refresh_context()

    def action_ajuda(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen(self.BINDINGS, t("screen.new_job_mysql")))

    # ------------------------------------------------------------------
    @on(Input.Changed, "#w-busca")
    def _mudou_busca(self, evento: Input.Changed) -> None:
        self.busca = evento.value
        self.cursor_tabela = 0
        self.query_one("#w-tabelas", DynamicText).rebuild()

    @on(Input.Changed, "#w-cron")
    def _mudou_cron(self, evento: Input.Changed) -> None:
        campo = evento.input.parent
        if not isinstance(campo, Field):
            return
        if is_valid(evento.value):
            campo.set_hint(humanize(evento.value))
        else:
            campo.set_error("cron inválido")

    def on_key(self, evento) -> None:
        chave = self.passos[self.passo]
        if chave != "wiz.s_tables" or not self.tabelas:
            return
        visiveis = self._filtradas()
        if evento.key == "space" and visiveis:
            self._alternar_tabela(visiveis[self.cursor_tabela])
            evento.stop()
        elif evento.key == "down" and self.cursor_tabela < len(visiveis) - 1:
            self.cursor_tabela += 1
            self.query_one("#w-tabelas", DynamicText).rebuild()
            evento.stop()
        elif evento.key == "up" and self.cursor_tabela > 0:
            self.cursor_tabela -= 1
            self.query_one("#w-tabelas", DynamicText).rebuild()
            evento.stop()

    def _alternar_tabela(self, tabela: mysql.TableInfo) -> None:
        """A marca à mão vence a regex, nos dois sentidos.

        Se a regex pega a tabela, desmarcar à mão a resgata (entra em
        `keep_manual`). Se a regex não pega, marcar à mão a ignora.
        """
        fonte: MySQLSource = self.job.source  # type: ignore[assignment]
        nome = tabela.name
        por_regex, por_mao = fonte.resolve_ignored([nome])
        if nome in por_mao:
            fonte.ignore_manual.remove(nome)
        elif nome in por_regex:
            fonte.keep_manual.append(nome)
        elif nome in fonte.keep_manual:
            fonte.keep_manual.remove(nome)
        else:
            fonte.ignore_manual.append(nome)
        self.query_one("#w-tabelas", DynamicText).rebuild()
        self.query_one("#lado-texto", DynamicText).rebuild()


# ----------------------------------------------------------------------------

def _inteiro(texto: str, padrao: int) -> int:
    try:
        return int(texto)
    except (TypeError, ValueError):
        return padrao


def _segundos(valor: float) -> str:
    return f"{valor:.1f}s".replace(".", ",")


def _versao_mysqldump() -> str:
    import subprocess

    try:
        proc = subprocess.run(["mysqldump", "--version"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    saida = proc.stdout.strip()
    for pedaco in saida.split():
        if pedaco[:1].isdigit():
            return pedaco.rstrip(",")
    return saida[:20]


def _profundidade(caminho: Path, limite: int = 6) -> int:
    maior = 0
    for item in caminho.rglob("*"):
        if item.is_dir():
            nivel = len(item.relative_to(caminho).parts)
            maior = max(maior, nivel)
            if maior >= limite:
                break
    return maior
