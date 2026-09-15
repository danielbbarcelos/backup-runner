"""Dashboard: a tela que abre e para onde tudo volta.

Duas colunas de 42 e 58. A lista precisa de duas linhas por job (nome, depois
resultado) para caber o que importa sem abreviar, e as 58 restantes seguram a
linha mais longa do detalhe, que é o destino com caminho e retenção. Abaixo de
90 colunas a divisão piora as duas, então o detalhe vira aba em vez de encolher.

Três estados moram aqui: com jobs, com execução em andamento e vazio. O vazio
não é erro, é a primeira abertura, e ensina os três passos na ordem.
"""
from __future__ import annotations

import datetime as dt

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import ListItem, ListView, Static

from ...i18n import t
from ...models import RunResult, SourceKind, Stage, StageState
from ...schedule import humanize
from .. import markup as m
from .. import theme as T
from ..context import JobView
from ..widgets import Hero, StatusBar

LARGURA_ESTREITA = 90


class JobCell(Static):
    """Corpo de uma linha da lista, renderizado com a largura real do painel.

    O texto é montado no render, e não guardado pronto, porque a coluna encolhe
    com a janela e alinhar com número fixo de espaços quebraria a linha em duas
    assim que o nome do job crescesse.
    """

    def __init__(self, view: JobView, **kwargs) -> None:
        super().__init__(markup=True, **kwargs)
        self.view = view

    def render(self) -> str:
        return _linha_job(self.view, max(24, self.size.width), self.app, self._em_foco())

    def _em_foco(self) -> bool:
        """A marca › vem do ListItem pai, que é quem o ListView marca.

        Usa a propriedade `highlighted` e não a classe CSS: o Textual chama a
        classe de `-highlight`, com um hífen só, e errar esse nome falha em
        silêncio, sem destaque nenhum e sem erro.
        """
        return bool(getattr(self.parent, "highlighted", False))


class JobRow(ListItem):
    """Duas linhas: identidade em cima, estado embaixo."""

    def __init__(self, view: JobView, **kwargs) -> None:
        super().__init__(**kwargs)
        self.view = view

    def compose(self) -> ComposeResult:
        yield JobCell(self.view)

    def refresh_row(self) -> None:
        self.query_one(JobCell).refresh()


def _linha_job(v: JobView, largura: int, app, foco: bool = False) -> str:
    """Duas linhas por job: identidade em cima, estado embaixo.

    Duas linhas em vez de uma porque numa coluna de 40 células não cabe nome,
    tipo, resultado, tamanho e próxima execução sem abreviar justamente o nome,
    que é por onde a pessoa procura.

    A marca › na coluna 1 é o que diz onde está o cursor. Fundo sozinho não
    basta: num terminal com tema claro, ou com a lista fora de foco, a
    diferença de cor some e a pessoa perde a posição.
    """
    marca = m.c(T.SYM_HINT, T.PRIMARY) if foco else " "
    nome = m.body(v.name, bold=not v.paused) if not v.paused else m.dim(v.name)
    tipo_texto = v.kind_label
    tipo = m.muted(tipo_texto) if not v.paused else m.dim(tipo_texto)
    espaco = max(1, largura - 4 - len(v.name) - len(tipo_texto))
    topo = f"{marca} {m.dot(not v.paused)} {nome}" + " " * espaco + tipo

    if v.running:
        baixo = m.badge(RunResult.RUNNING) + m.muted("  " + _estagio_atual(app))
    elif v.queued:
        rotulo = (
            t("run.queue_behind", n=v.queue_position, job=v.queue_behind)
            if v.queue_behind
            else t("run.queue_pos", n=v.queue_position)
        )
        baixo = m.badge(RunResult.QUEUED) + m.muted("  " + rotulo)
    elif v.paused:
        desde = f" desde {v.job.paused_at}" if v.job.paused_at else ""
        baixo = m.badge(RunResult.SKIPPED) + m.dim(desde)
    elif v.last is None:
        proxima = v.next_in_seconds()
        baixo = m.muted("nunca rodou") + (
            m.muted(f", primeira {_dia_hora(v.next_at)}") if proxima is not None else ""
        )
    else:
        # Orçamento de largura: o badge nunca sai, o tamanho sai antes da
        # próxima execução, e a próxima sai antes de a linha quebrar em duas.
        badge_texto = m.badge_plain(v.last.result)
        tamanho_texto = T.format_bytes(v.last.bytes) if v.last.bytes else ""
        proxima = v.next_in_seconds()
        proxima_texto = (
            f"próx. {_dia_hora(v.next_at)}" if proxima is not None else t("dash.no_next")
        )
        alerta = "  !" if v.is_stale() else ""
        # A segunda linha começa na coluna 5 (marca, ponto e dois espaços),
        # então o orçamento precisa descontar isso ou o alerta cai sozinho na
        # linha seguinte.
        disponivel = largura - 6 - len(badge_texto) - len(alerta)

        partes = [m.badge(v.last.result)]
        if tamanho_texto and len(tamanho_texto) + 2 <= disponivel - len(proxima_texto) - 2:
            partes.append(m.muted(tamanho_texto))
            disponivel -= len(tamanho_texto) + 2
        if len(proxima_texto) > disponivel and proxima is not None:
            # Sem espaço para o prefixo, a hora sozinha ainda informa.
            proxima_texto = _dia_hora(v.next_at)
        if len(proxima_texto) <= disponivel:
            partes.append(m.muted(proxima_texto) if proxima is not None else m.dim(proxima_texto))
        baixo = "  ".join(partes)
        if alerta:
            baixo += "  " + m.c(T.SYM_WARN, T.WARNING)
    return f"{topo}\n     {baixo}"


def _estagio_atual(app) -> str:
    run = app.ctx.running_run
    if run is None:
        return ""
    atual = next((s for s in run.stages if s.state is StageState.RUNNING), None)
    return f"{atual.label}" if atual else ""


class DetailPane(Static):
    """Painel direito: ficha do job, ou o andamento quando ele está rodando.

    Renderiza com a largura real, então a régua, o caminho do destino e a
    tabela de execuções acompanham a janela em vez de estourar a coluna.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(markup=True, **kwargs)
        self.view: JobView | None = None
        self.quadro = 0

    def render(self) -> str:
        if self.view is None:
            return ""
        largura = max(30, self.size.width)
        if self.view.running:
            return self._andamento(self.view, largura)
        return self._ficha(self.view, largura)

    # ------------------------------------------------------------------
    def _ficha(self, view: JobView, largura: int) -> str:
        ctx = self.app.ctx
        job = view.job
        rotulo = lambda texto: m.secondary(texto.ljust(10))
        linhas: list[str] = [""]

        if job.kind is SourceKind.MYSQL:
            fonte = f"mysql, {job.source.database} @ {job.source.host}:{job.source.port}"
        else:
            fonte = f"arquivos, {job.source.path}"
        linhas.append(rotulo(t("dash.source")) + m.body(_corta(fonte, largura - 11)))

        ultima = view.last
        if job.kind is SourceKind.MYSQL and ultima and ultima.ignored_total:
            linhas.append(
                " " * 10
                + m.muted(_corta(
                    f"{ultima.ignored_total} ignoradas na última, "
                    f"{len(ultima.ignored_regex)} pela regex",
                    largura - 11,
                ))
            )
        elif job.kind is SourceKind.FILES:
            ativos = len(job.source.active_excludes())
            linhas.append(" " * 10 + m.muted(_corta(
                f"{ativos} exclusões ativas, {job.source.archive_format.value}", largura - 11)))

        linhas.append("")
        linhas.append(rotulo(t("dash.when")) + m.body(humanize(job.schedule)))
        segundos = view.next_in_seconds()
        if segundos is not None:
            linhas.append(" " * 10 + m.muted(_corta(
                f"{t('dash.next_in', t=T.format_relative(segundos))}, tz {job.timezone}", largura - 11)))
        else:
            linhas.append(" " * 10 + m.dim(t("dash.no_next")))

        linhas.append("")
        linhas.extend(self._destinos(job, largura, rotulo))

        linhas.append("")
        linhas.extend(self._avisos(job, largura, rotulo))

        if view.is_stale():
            linhas.append("")
            linhas.append(m.inline("warn", f"sem sucesso há mais de {job.stale_after_hours}h"))

        linhas.append("")
        linhas.append(m.dim("─" * largura))
        linhas.append(m.secondary(t("dash.last_runs")))
        linhas.append("")
        linhas.extend(self._execucoes(view, largura, segundos))
        return "\n".join(linhas)

    def _destinos(self, job, largura: int, rotulo) -> list[str]:
        ctx = self.app.ctx
        if not job.destinations:
            return [rotulo(t("dash.destinations"))
                    + m.c(f"{T.SYM_WARN} nenhum, o backup não sai do staging", T.WARNING)]
        linhas = []
        # nome (11) + local (resto) + retenção (11), tudo dentro da largura real.
        largura_local = max(12, largura - 10 - 13 - 11)
        for i, (jd, destino) in enumerate(ctx.job_destinations(job)):
            prefixo = rotulo(t("dash.destinations")) if i == 0 else " " * 10
            nome = _corta(jd.name, 12).ljust(13)
            local = _corta(destino.location() if destino else "destino ausente", largura_local).ljust(largura_local + 1)
            dias = m.muted(t("dash.keep_days", d=jd.days(destino)))
            corpo = m.body(local) if destino else m.c(local, T.DANGER)
            marca = "" if (destino and destino.enabled) else " " + m.c(T.SYM_WARN, T.WARNING)
            linhas.append(prefixo + m.secondary(nome) + corpo + dias + marca)
        return linhas

    def _avisos(self, job, largura: int, rotulo) -> list[str]:
        """Uma linha por conjunto de canais, não uma frase única.

        Cinco eventos numa linha só viravam um parágrafo ilegível, que é
        exatamente o que a matriz existe para evitar.
        """
        from ...models import CHANNELS, NOTIFY_EVENTS

        ctx = self.app.ctx
        padrao = ctx.settings.notify_global
        agrupado: dict[str, list[str]] = {}
        for evento in NOTIFY_EVENTS:
            canais = [c.value for c in CHANNELS if job.notify.resolve(evento, c, padrao)]
            if canais:
                agrupado.setdefault(", ".join(canais), []).append(t(f"notif.ev_{evento.value}"))
        if not agrupado:
            return [rotulo(t("dash.notices")) + m.dim("nenhum aviso ligado")]
        linhas = []
        for i, (canais, eventos) in enumerate(agrupado.items()):
            prefixo = rotulo(t("dash.notices")) if i == 0 else " " * 10
            texto = _corta(", ".join(eventos), largura - 10 - len(canais) - 3)
            linhas.append(prefixo + m.body(texto) + m.muted(f": {canais}"))
        return linhas

    def _execucoes(self, view: JobView, largura: int, segundos: float | None) -> list[str]:
        ctx = self.app.ctx
        execucoes = ctx.state.runs(job=view.name, limit=4)
        if not execucoes:
            linhas = ["  " + m.body(t("empty.runs_title")), ""]
            if segundos is not None:
                linhas.append("      " + m.muted(t("empty.runs_when", quando=f"em {T.format_relative(segundos)}")))
            linhas.append("      " + m.key("r", " rodar agora sem esperar"))
            return linhas
        linhas = []
        for run in execucoes:
            simbolo, _, cor = m.badge_parts(run.result)
            quando = run.started_at.strftime("%d/%m %H:%M").ljust(14)
            tamanho = T.format_bytes(run.bytes).ljust(10)
            duracao = T.format_duration(run.duration).ljust(11)
            destinos = (
                t("hist.n_dest", n=len(run.destinations_done))
                if run.destinations_done
                else (_corta(run.error_cause, 12) if run.error_cause else t("hist.none"))
            )
            linhas.append("  " + m.c(simbolo, cor) + " " + m.body(quando) + m.muted(tamanho + duracao + destinos))
        linhas.append("")
        linhas.append(m.muted(t("dash.open_history")))
        return linhas

    # ------------------------------------------------------------------
    def _andamento(self, view: JobView, largura: int) -> str:
        """Detalhe enquanto a execução corre.

        Progresso determinado quando o total é conhecido (o upload sabe quantos
        bytes tem). Indeterminado quando não é: o dump não sabe o tamanho antes
        de terminar, e aí a interface conta em vez de estimar.
        """
        ctx = self.app.ctx
        run = ctx.running_run
        if run is None:
            return self._ficha(view, largura)

        linhas = [""]
        feitos = sum(1 for s in run.stages if s.state is StageState.DONE)
        total = len(run.stages) or 1
        linhas.append(
            m.muted(t("run.stage_of", n=min(feitos + 1, total), total=total).ljust(28))
            + m.muted(t("run.started", hora=run.started_at.strftime("%H:%M")))
        )
        linhas.append("")

        largura_detalhe = max(10, largura - 30)
        for estagio in run.stages:
            simbolo, cor = _estado_parts(estagio.state)
            nome = (estagio.label or estagio.stage.value)[:9].ljust(10)
            detalhe = _corta(estagio.detail, largura_detalhe).ljust(largura_detalhe + 1)
            duracao = T.format_duration(estagio.seconds) if estagio.seconds else (
                t("run.in_progress") if estagio.state is StageState.RUNNING else t("run.waiting")
            )
            linhas.append(f"  {m.c(simbolo, cor)} {m.body(nome)}{m.muted(detalhe)}{m.muted(duracao)}")

        linhas.append("")
        barra = max(20, largura - 14)
        atual = next((s for s in run.stages if s.state is StageState.RUNNING), None)
        if atual is not None and atual.stage is Stage.UPLOAD and run.bytes:
            enviado = int(run.bytes * _fracao_ficticia(self.quadro))
            fracao = enviado / run.bytes if run.bytes else 0
            linhas.append(m.secondary(t("run.known_total")))
            linhas.append("  " + m.bar(fracao, barra) + m.body(f" {int(fracao * 100)}%"))
            linhas.append("  " + m.muted(f"{T.format_bytes(enviado)} {t('run.of')} {T.format_bytes(run.bytes)}"))
        else:
            linhas.append(m.secondary(t("run.unknown_total")))
            linhas.append("  " + m.bar_indeterminate(self.quadro, barra) + " " + m.spinner(self.quadro))
            decorrido = (dt.datetime.now() - run.started_at).total_seconds()
            linhas.append("  " + m.muted(f"{T.format_bytes(run.bytes)} lidos, {T.format_duration(decorrido)}"))
            linhas.append("  " + m.dim(t("run.unknown_hint")))

        if run.log:
            linhas.append("")
            linhas.append(m.dim("─" * largura))
            linhas.append(m.secondary(t("run.output")) + "   " + m.muted(t("run.full_log")))
            for hora, origem, texto in run.log[-3:]:
                linhas.append(f"  {m.muted(hora)} {m.secondary(origem.ljust(9))} {m.body(_corta(texto, largura - 22))}")
        return "\n".join(linhas)


class DashboardScreen(Screen):
    CSS = """
    DashboardScreen { background: $br-bg; }
    #corpo { height: 1fr; }
    #lista-painel { width: 42%; }
    #detalhe-painel { width: 1fr; }
    #detalhe { padding: 0 1; }
    #vazio { height: 1fr; padding: 1 2; }
    #abas { height: 1; padding: 0 1; display: none; }
    """

    BINDINGS = [
        ("up,k", "mover(-1)", "mover"),
        ("down,j", "mover(1)", "mover"),
        ("tab", "alternar_painel", "painel"),
        ("enter", "abrir_historico", "abrir"),
        ("n", "novo", "novo"),
        ("r", "rodar", "rodar"),
        ("p", "pausar", "pausar"),
        ("d", "apagar", "apagar"),
        ("h", "historico", "histórico"),
        ("t", "destinos", "destinos"),
        ("a", "avisos", "avisos"),
        ("s", "saude", "saúde"),
        ("i", "instalar_tick", "instalar tick"),
        ("slash", "buscar", "buscar"),
        ("question_mark", "ajuda", "ajuda"),
        ("q", "sair", "sair"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._quadro = 0
        self._aba = 0  # 0 lista, 1 detalhe, no modo estreito

    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Hero(t("screen.dashboard"), id="hero")
        yield Static(id="abas")
        with Horizontal(id="corpo"):
            with Vertical(id="lista-painel", classes="panel"):
                yield ListView(id="lista")
            with VerticalScroll(id="detalhe-painel", classes="panel"):
                yield DetailPane(id="detalhe")
        yield VerticalScroll(Static(id="vazio-texto", markup=True), id="vazio")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.query_one("#lista-painel").border_title = t("dash.jobs")
        self.refresh_data()
        self.set_interval(0.12, self._tick_animacao)
        self.set_interval(5.0, self._recarregar)

    # ------------------------------------------------------------------
    def refresh_data(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        vazio = not ctx.views
        self.query_one("#corpo").display = not vazio
        self.query_one("#vazio").display = vazio

        if vazio:
            self.query_one("#vazio-texto", Static).update(self._texto_vazio())
        else:
            self._preencher_lista()
            self._preencher_detalhe()
        self._preencher_hero()
        self._preencher_status()

    def _preencher_lista(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        lista = self.query_one("#lista", ListView)
        indice = lista.index or 0
        lista.clear()
        for view in ctx.views:
            lista.append(JobRow(view))
        if ctx.views:
            lista.index = min(indice, len(ctx.views) - 1)

    @property
    def selecionado(self) -> JobView | None:
        lista = self.query_one("#lista", ListView)
        item = lista.highlighted_child
        return item.view if isinstance(item, JobRow) else None

    # ------------------------------------------------------------------
    def _preencher_hero(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        hero = self.query_one("#hero", Hero)
        total, ativos, pausados = ctx.counts()
        rodando = ctx.running_run is not None

        if rodando:
            hero.right_top = f"{t('dash.jobs')} {total}    {t('dash.running')} 1    {t('status.queue')} {ctx.queue_size}"
            run = ctx.running_run
            decorrido = (dt.datetime.now() - run.started_at).total_seconds()
            hero.right_bottom = t("run.elapsed", t=T.format_duration(decorrido))
        else:
            hero.right_top = f"{t('dash.jobs')} {total}    {t('dash.active')} {ativos}     {t('dash.paused')} {pausados}"
            proxima = ctx.next_overall()
            hero.right_bottom = (
                t("dash.next_in", t=T.format_relative(proxima[1])) if proxima else t("dash.nothing_scheduled")
            )
        hero.refresh()

    def _preencher_status(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        barra = self.query_one("#status", StatusBar)
        rodando = ctx.running_run is not None
        barra.worker_state = "rodando" if rodando else ("ativo" if ctx.worker.running else "parado")
        barra.queue = ctx.queue_size
        barra.tick = ctx.tick_ok
        barra.staging_free = t("status.staging_free", size=T.format_bytes(ctx.staging.free))
        barra.health_ok = not ctx.stale_jobs()
        vazio = not ctx.views
        if vazio:
            barra.hints = m.keys(
                ("i", t("key.install_tick")), ("t", t("key.destinations")), ("n", t("key.new_job")),
                ("s", t("key.health")), ("?", t("key.help")), ("q", t("key.quit")),
            )
        else:
            rodando_agora = self.selecionado.running if self.selecionado else False
            barra.hints = m.keys(
                ("↑↓", t("key.move")), ("tab", t("key.panel")), ("enter", t("key.open")),
                ("n", t("key.new")), ("r", t("key.run"), not rodando_agora), ("p", t("key.pause")),
                ("d", t("key.delete")), ("/", t("key.search")), ("h", t("key.history")),
                ("?", t("key.help")),
            )
        barra.refresh()

    # ------------------------------------------------------------------
    def _preencher_detalhe(self) -> None:
        view = self.selecionado
        painel = self.query_one("#detalhe-painel")
        alvo = self.query_one("#detalhe", DetailPane)
        alvo.view = view
        alvo.quadro = self._quadro
        painel.border_title = view.name if view else ""
        alvo.refresh()

    # ------------------------------------------------------------------
    def _texto_vazio(self) -> str:
        """Primeira abertura: ensina o próximo passo sem parecer erro."""
        ctx = self.app.ctx  # type: ignore[attr-defined]
        tick = ctx.tick_ok
        tem_destino = bool(ctx.destinations.list())

        def passo(numero: str, titulo: str, porque: str, estado: str, teclas: str, feito: bool) -> list[str]:
            marca = m.c(f"{T.SYM_OK} feito", T.SUCCESS) if feito else m.c(f"{T.SYM_WARN} {estado}", T.WARNING)
            return [
                f"      {m.secondary(numero)}   {m.body(titulo)}" + " " * max(2, 34 - len(titulo)) + marca,
                f"          {m.muted(porque)}",
                f"          {teclas}",
                "",
            ]

        linhas = [
            "",
            "      " + m.body(t("empty.title")),
            "      " + m.muted(t("empty.subtitle")),
            "",
            "      " + m.dim("─" * 56),
            "",
            "      " + m.muted(t("empty.steps")),
            "",
        ]
        linhas += passo(
            "1", t("empty.step1"), t("empty.step1_why"), t("empty.pending"),
            m.key("i", " instalar agora") + m.dim("      ou backup-runner tick --install"),
            tick,
        )
        linhas += passo(
            "2", t("empty.step2"), t("empty.step2_why"), t("empty.pending"),
            m.key("t", " abrir destinos"),
            tem_destino,
        )
        linhas += passo(
            "3", t("empty.step3"), t("empty.step3_why"),
            t("empty.after_step2") if not tem_destino else t("empty.pending"),
            m.key("n", " novo job"),
            False,
        )
        linhas += [
            "      " + m.dim("─" * 56),
            "",
            "      " + m.muted(t("empty.worker_ready")),
            "      " + m.key("s", " ver saúde do sistema") + "      " + m.key("?", " ajuda e teclas"),
        ]
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    def _tick_animacao(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        if ctx.running_run is None:
            return
        self._quadro += 1
        self._preencher_detalhe()
        self._preencher_hero()

    def _recarregar(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        ctx.refresh()
        self.refresh_data()

    def on_resize(self) -> None:
        estreito = self.size.width < LARGURA_ESTREITA
        self.query_one("#abas").display = estreito
        if estreito:
            self.query_one("#lista-painel").styles.width = "1fr"
            self.query_one("#detalhe-painel").display = self._aba == 1
            self.query_one("#lista-painel").display = self._aba == 0
            self._preencher_abas()
        else:
            self.query_one("#lista-painel").styles.width = "42%"
            self.query_one("#detalhe-painel").display = True
            self.query_one("#lista-painel").display = True

    def _preencher_abas(self) -> None:
        nomes = [t("dash.jobs"), t("dash.detail")]
        partes = []
        for i, nome in enumerate(nomes):
            partes.append(m.primary(f"▌{nome}", bold=True) if i == self._aba else m.muted(f" {nome}"))
        self.query_one("#abas", Static).update(
            "  ".join(partes) + "       " + m.dim(f"◂ {t('dash.tab_switches')} ▸")
        )

    # ------------------------------------------------------------------
    @on(ListView.Highlighted)
    def _mudou_selecao(self) -> None:
        for linha in self.query(JobRow):
            linha.refresh_row()
        self._preencher_detalhe()
        self._preencher_status()

    def action_mover(self, passo: int) -> None:
        lista = self.query_one("#lista", ListView)
        if passo < 0:
            lista.action_cursor_up()
        else:
            lista.action_cursor_down()

    def action_alternar_painel(self) -> None:
        if self.size.width < LARGURA_ESTREITA:
            self._aba = 1 - self._aba
            self.on_resize()
            return
        detalhe = self.query_one("#detalhe-painel")
        lista = self.query_one("#lista", ListView)
        if detalhe.has_focus_within:
            lista.focus()
        else:
            detalhe.focus()

    # ------------------------------------------------------------------
    def action_novo(self) -> None:
        from .wizard import escolher_fonte

        escolher_fonte(self.app)

    def action_rodar(self) -> None:
        view = self.selecionado
        if view is None or view.running:
            return
        ctx = self.app.ctx  # type: ignore[attr-defined]
        ctx.state.enqueue(view.name, dt.datetime.now())
        self.notify(f"{view.name} enfileirado", severity="information")
        self._recarregar()

    def action_pausar(self) -> None:
        view = self.selecionado
        if view is None:
            return
        ctx = self.app.ctx  # type: ignore[attr-defined]
        job = view.job
        job.enabled = not job.enabled
        job.paused_at = None if job.enabled else dt.date.today().strftime("%d/%m")
        ctx.jobs.put(job)
        self._recarregar()

    def action_apagar(self) -> None:
        view = self.selecionado
        if view is None:
            return
        from .confirm import ConfirmDeleteJob

        self.app.push_screen(ConfirmDeleteJob(view), self._depois_de_apagar)

    def _depois_de_apagar(self, apagou: bool | None) -> None:
        if apagou:
            self._recarregar()

    def action_abrir_historico(self) -> None:
        view = self.selecionado
        from .history import HistoryScreen

        self.app.push_screen(HistoryScreen(job=view.name if view else None))

    def action_historico(self) -> None:
        from .history import HistoryScreen

        self.app.push_screen(HistoryScreen())

    def action_destinos(self) -> None:
        from .destinations import DestinationsScreen

        self.app.push_screen(DestinationsScreen())

    def action_avisos(self) -> None:
        from .notifications import NotificationsScreen

        view = self.selecionado
        self.app.push_screen(NotificationsScreen(job=view.job if view else None))

    def action_saude(self) -> None:
        from .health import HealthScreen

        self.app.push_screen(HealthScreen())

    def action_instalar_tick(self) -> None:
        from ...health import install_tick

        ok, mensagem = install_tick()
        self.notify(mensagem, severity="information" if ok else "error")
        self._recarregar()

    def action_buscar(self) -> None:
        self.notify("busca ainda não implementada", severity="warning")

    def action_ajuda(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen(self.BINDINGS, t("screen.dashboard")))

    def action_sair(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        if ctx.running_run is not None:
            from .confirm import ConfirmQuit

            self.app.push_screen(ConfirmQuit(), lambda sair: self.app.exit() if sair else None)
            return
        self.app.exit()


# ----------------------------------------------------------------------------

def _corta(texto: str, largura: int) -> str:
    """Trunca pela largura visível, com reticências de uma célula."""
    if largura <= 1:
        return ""
    return texto if len(texto) <= largura else texto[: largura - 1] + "…"


def _dia_hora(quando: dt.datetime | None) -> str:
    if quando is None:
        return "?"
    hoje = dt.date.today()
    if quando.date() == hoje:
        return quando.strftime("%H:%M")
    if quando.date() == hoje + dt.timedelta(days=1):
        return "amanhã " + quando.strftime("%H:%M")
    return quando.strftime("%d/%m %H:%M")


def _estado_parts(estado: StageState) -> tuple[str, str]:
    mapa = {
        StageState.DONE: (T.SYM_OK, T.SUCCESS),
        StageState.RUNNING: (T.SYM_RUNNING, T.PRIMARY),
        StageState.WAITING: (T.SYM_INACTIVE, T.DISABLED),
        StageState.FAILED: (T.SYM_FAIL, T.DANGER),
        StageState.SKIPPED: (T.SYM_INACTIVE, T.DISABLED),
    }
    return mapa[estado]


def _fracao_ficticia(quadro: int) -> float:
    """Progresso do upload enquanto o worker real não publica bytes enviados.

    Substituir por leitura do callback do boto3 quando o worker existir.
    """
    return min(0.99, (quadro % 400) / 400)
