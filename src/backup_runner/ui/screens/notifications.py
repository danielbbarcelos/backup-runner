"""Matriz de avisos: evento por canal.

Cada evento ocupa duas linhas. Em cima o que vale para este job, que é o que
manda. Embaixo, em disabled, o que o padrão global diz. A seta marca a célula
que o job sobrescreveu, e `r` devolve ao global.

Sem as duas linhas, a pessoa não consegue distinguir "este job está ligado"
de "este job herdou ligado", e é essa confusão que faz alguém desligar o
aviso errado e só descobrir quando um backup falha em silêncio.
"""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from ...i18n import t
from ...models import CHANNELS, NOTIFY_EVENTS, Channel, Job, NotifyEvent
from .. import markup as m
from .. import theme as T
from ..widgets import DynamicText, Hero, StatusBar

SETA_OVERRIDE = "↷"


class NotificationsScreen(Screen):
    CSS = """
    NotificationsScreen { background: $br-bg; }
    #escopo { height: 1; padding: 0 1; }
    #grade { height: 1fr; border: round $br-border; padding: 0 1; }
    #canais { height: 1; padding: 0 1; }
    """

    BINDINGS = [
        ("up,k", "mover_linha(-1)", "célula"),
        ("down,j", "mover_linha(1)", "célula"),
        ("left,h", "mover_coluna(-1)", "célula"),
        ("right,l", "mover_coluna(1)", "célula"),
        ("space", "alternar", "alternar"),
        ("r", "voltar_global", "voltar ao global"),
        ("tab", "trocar_escopo", "escopo"),
        ("e", "enviar_teste", "enviar teste"),
        ("escape,q", "voltar", "voltar"),
        ("question_mark", "ajuda", "ajuda"),
    ]

    def __init__(self, job: Job | None = None) -> None:
        super().__init__()
        self.job = job
        self._global = job is None
        self._linha = 0
        self._coluna = 0

    def compose(self) -> ComposeResult:
        yield Hero(t("screen.notifications"), id="hero")
        yield Static(id="escopo", markup=True)
        yield VerticalScroll(DynamicText(self._montar_grade, id="grade"))
        yield Static(id="canais", markup=True)
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.refresh_data()

    # ------------------------------------------------------------------
    def refresh_data(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        hero = self.query_one("#hero", Hero)
        if self.job is not None:
            sobrescritas = self.job.notify.overrides(ctx.settings.notify_global)
            hero.right_top = t("notif.scope_job", nome=self.job.name).replace("job ", "avisos de ")
            hero.right_bottom = t("notif.overridden_count", n=sobrescritas)
        else:
            hero.right_top = t("notif.scope_global")
            hero.right_bottom = "vale para todo job que não sobrescrever"
        hero.refresh()

        self._preencher_escopo()
        self.query_one("#grade", DynamicText).rebuild()
        self._preencher_canais()
        self._preencher_status()

    def _preencher_escopo(self) -> None:
        if self.job is None:
            self.query_one("#escopo", Static).update(
                m.primary(f"▌{t('notif.scope_global')}", bold=True)
                + "      " + m.dim("nenhum job selecionado para comparar")
            )
            return
        abas = [
            (t("notif.scope_job", nome=self.job.name), not self._global),
            (t("notif.scope_global"), self._global),
        ]
        partes = [
            m.primary(f"▌{nome}", bold=True) if ativo else m.muted(f" {nome}")
            for nome, ativo in abas
        ]
        self.query_one("#escopo", Static).update(
            "      ".join(partes) + "     " + m.dim(f"◂ {t('notif.tab_scope')} ▸")
        )

    def _matriz_atual(self):
        ctx = self.app.ctx  # type: ignore[attr-defined]
        if self.job is None or self._global:
            return ctx.settings.notify_global
        return self.job.notify

    # ------------------------------------------------------------------
    def _montar_grade(self, largura: int) -> str:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        padrao = ctx.settings.notify_global
        editando_global = self.job is None or self._global

        col_evento = 36
        col_canal = 16
        linhas = [
            "",
            m.secondary(t("notif.event").ljust(col_evento))
            + "".join(m.secondary(c.value.capitalize().ljust(col_canal)) for c in CHANNELS)
            + m.secondary(t("notif.origin")),
            m.dim("─" * max(10, min(largura, col_evento + col_canal * len(CHANNELS) + 18))),
            "",
        ]

        for i, evento in enumerate(NOTIFY_EVENTS):
            foco_linha = i == self._linha
            marca = m.c(T.SYM_HINT, T.PRIMARY) if foco_linha else " "
            nome = t(f"notif.ev_{evento.value}")
            rotulo = (m.body(nome, bold=foco_linha) if foco_linha else m.body(nome))
            linha_cima = f"{marca} {rotulo}" + " " * max(1, col_evento - len(nome) - 2)

            origem = ""
            for j, canal in enumerate(CHANNELS):
                configurado = ctx.settings.channel_configured(canal.value)
                proprio = self._matriz_atual().get(evento, canal)
                valor = (
                    proprio if proprio is not None
                    else padrao.resolve(evento, canal, padrao)
                )
                celula = m.checkbox(bool(valor), bloqueado=not configurado)
                if foco_linha and j == self._coluna:
                    celula = m.c("▏", T.PRIMARY) + celula
                else:
                    celula = " " + celula
                linha_cima += celula.ljust(0) + " " * (col_canal - 4)

            if not editando_global and self.job is not None:
                sobrescritos = [
                    canal for canal in CHANNELS
                    if self.job.notify.get(evento, canal) is not None
                    and self.job.notify.get(evento, canal) != padrao.get(evento, canal)
                ]
                if sobrescritos:
                    origem = m.c(f"{SETA_OVERRIDE} {t('notif.overridden')}", T.WARNING)
                else:
                    origem = m.muted(t("notif.same_global"))
            linhas.append(linha_cima + origem)

            # Segunda linha: a razão do evento e o que o global diz.
            porque = t(f"notif.ev_{evento.value}_why", h=self.job.stale_after_hours if self.job else 48)
            # Truncado para a coluna não empurrar as marcas dos canais.
            if len(porque) > col_evento - 3:
                porque = porque[: col_evento - 4] + "…"
            baixo = "  " + m.dim(porque) + " " * max(1, col_evento - len(porque) - 2)
            if not editando_global:
                for canal in CHANNELS:
                    configurado = ctx.settings.channel_configured(canal.value)
                    if not configurado:
                        baixo += " " + m.dim(t("notif.unconfigured"))[:col_canal] + " "
                        continue
                    global_valor = bool(padrao.get(evento, canal))
                    baixo += " " + m.dim(t("notif.global") + " ") + m.dim(
                        T.CHECK_MANUAL if global_valor else T.CHECK_OFF
                    ) + " " * (col_canal - 11)
            linhas.append(baixo)
            linhas.append("")

        linhas += [
            m.dim("─" * max(10, min(largura, 78))),
            m.secondary(t("notif.reading")),
            "  " + m.checkbox(True) + " " + m.dim(T.CHECK_OFF) + " " + m.muted(t("notif.read_top")),
            "  " + m.dim(t("notif.global") + " " + T.CHECK_MANUAL) + " " + m.muted(t("notif.read_bottom")),
            "  " + m.c(SETA_OVERRIDE, T.WARNING) + " " + m.muted(t("notif.read_mark")),
            "  " + m.dim(T.CHECK_OFF) + " " + m.muted(t("notif.read_disabled")),
        ]
        return "\n".join(linhas)

    def _preencher_canais(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        email = ctx.settings.email_summary() or m.dim("não configurado")
        slack = ctx.settings.slack_summary() or m.dim("não configurado")
        self.query_one("#canais", Static).update(
            m.secondary("email: ") + m.body(email) + "       "
            + m.secondary("Slack: ") + m.body(slack) + "         "
            + m.key("c", " configurar canais")
        )

    def _preencher_status(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        barra = self.query_one("#status", StatusBar)
        barra.worker_state = "ativo" if ctx.worker.running else "parado"
        barra.queue = ctx.queue_size
        barra.tick = ctx.tick_ok
        barra.hints = m.keys(
            ("↑↓←→", t("key.cell")), ("espaço", t("key.toggle")), ("r", t("key.reset_global")),
            ("tab", t("key.scope")), ("e", t("key.send_test")), ("esc", t("key.back")),
        )
        barra.refresh()

    # ------------------------------------------------------------------
    def action_mover_linha(self, passo: int) -> None:
        self._linha = (self._linha + passo) % len(NOTIFY_EVENTS)
        self.query_one("#grade", DynamicText).rebuild()

    def action_mover_coluna(self, passo: int) -> None:
        self._coluna = (self._coluna + passo) % len(CHANNELS)
        self.query_one("#grade", DynamicText).rebuild()

    def action_alternar(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        evento = NOTIFY_EVENTS[self._linha]
        canal = CHANNELS[self._coluna]
        if not ctx.settings.channel_configured(canal.value):
            self.notify(
                f"o canal {canal.value} não está configurado, então nem o global manda nele",
                severity="warning",
            )
            return
        matriz = self._matriz_atual()
        padrao = ctx.settings.notify_global
        atual = matriz.get(evento, canal)
        if atual is None:
            atual = bool(padrao.get(evento, canal))
        matriz.set(evento, canal, not atual)
        self._salvar()
        self.refresh_data()

    def action_voltar_global(self) -> None:
        if self.job is None or self._global:
            return
        evento = NOTIFY_EVENTS[self._linha]
        canal = CHANNELS[self._coluna]
        self.job.notify.clear(evento, canal)
        self._salvar()
        self.refresh_data()

    def _salvar(self) -> None:
        ctx = self.app.ctx  # type: ignore[attr-defined]
        if self.job is None or self._global:
            ctx.settings.save()
        else:
            ctx.jobs.put(self.job)

    def action_trocar_escopo(self) -> None:
        if self.job is None:
            return
        self._global = not self._global
        self.refresh_data()

    def action_enviar_teste(self) -> None:
        self.notify(
            "o envio de teste entra junto com os canais de email e Slack",
            severity="warning",
        )

    def action_voltar(self) -> None:
        self.app.ctx.refresh()  # type: ignore[attr-defined]
        self.app.pop_screen()

    def action_ajuda(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen(self.BINDINGS, t("screen.notifications")))
