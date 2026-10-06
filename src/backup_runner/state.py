"""Fila e histórico em SQLite, modo WAL.

Três processos escrevem e leem ao mesmo tempo: o tick enfileira, o worker
executa, a TUI observa. O WAL deixa a TUI ler sem travar quem escreve, e pegar
o próximo item da fila é uma transação, o que elimina a corrida entre dois
ticks que acordem no mesmo minuto.

Jobs e destinos continuam em JSON, porque são o que a pessoa lê e edita. Fila e
histórico ficam aqui, porque são o que a máquina escreve e consulta.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import state_file
from .models import (
    ManifestEntry,
    Run,
    RunResult,
    Stage,
    StageRecord,
    StageState,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    result        TEXT    NOT NULL,
    bytes         INTEGER,
    duration      REAL,
    artifact      TEXT,
    error_stage   TEXT,
    error_tried   TEXT,
    error_got     TEXT,
    error_cause   TEXT,
    error_fix     TEXT,
    retry_at      TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    payload       TEXT    NOT NULL DEFAULT '{}',
    -- Progresso ao vivo: o worker escreve enquanto trabalha, para quem
    -- perguntar de fora saber o que está acontecendo. Sem isto, um dump de
    -- doze gigabytes fica quarenta minutos dizendo apenas "rodando".
    prog_stage    TEXT,
    prog_done     INTEGER NOT NULL DEFAULT 0,
    prog_total    INTEGER NOT NULL DEFAULT 0,
    prog_label    TEXT,
    prog_pid      INTEGER,
    heartbeat     TEXT,
    -- Pedido de cancelamento. É uma marca, não um sinal: quem cancela escreve
    -- aqui e segue a vida, e o worker obedece quando passar pela sentinela.
    -- Matar o processo deixaria staging sujo e linha de fila presa.
    cancel_at     TEXT
);
CREATE INDEX IF NOT EXISTS runs_job_started ON runs(job, started_at DESC);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT    NOT NULL,
    due_at      TEXT    NOT NULL,
    enqueued_at TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    late        INTEGER NOT NULL DEFAULT 0,
    kind        TEXT    NOT NULL DEFAULT 'full',
    run_id      INTEGER,
    -- Quem pegou o item, e quando. Sem isto, uma linha em 'running' deixada
    -- por worker morto é indistinguível de trabalho em andamento, e como
    -- `ja_na_fila` olha 'running', ela descarta toda janela futura do job: a
    -- ferramenta para de fazer backup e continua dizendo que está tudo bem.
    claimed_pid  INTEGER,
    claimed_at   TEXT,
    UNIQUE(job, due_at, kind)
);
CREATE INDEX IF NOT EXISTS queue_status ON queue(status, due_at);

-- Envio em partes, para retomar de onde parou em vez de recomeçar do zero.
-- O `upload_id` é o estado que precisa sobreviver ao processo morrer: sem ele,
-- as partes já no provedor são inalcançáveis e só restaria reenviar tudo.
-- Cada parte concluída vira uma linha, e parte registrada é parte que não
-- se reenvia.
CREATE TABLE IF NOT EXISTS upload_parts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    destino     TEXT    NOT NULL,
    chave       TEXT    NOT NULL,
    upload_id   TEXT    NOT NULL,
    part_number INTEGER NOT NULL,
    etag        TEXT,
    bytes       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(run_id, destino, chave, part_number)
);
CREATE INDEX IF NOT EXISTS partes_envio ON upload_parts(run_id, destino, chave);
"""

# As duas raias da fila. Um envio de quatro horas não pode mais atrasar o dump
# de trinta segundos de outro job, e por isso produzir e enviar são itens
# separados, consumidos por laços independentes.
#
# `upload_retry` continua aceito na raia de envio porque é o nome que o reenvio
# manual e os bancos antigos usam, e renomear não compraria nada.
RAIA_PRODUCAO = ("producao",)
RAIA_ENVIO = ("envio", "upload_retry")
RAIAS = {"producao": RAIA_PRODUCAO, "envio": RAIA_ENVIO}

ISO = "%Y-%m-%dT%H:%M:%S"


def _parse(valor: str | None) -> dt.datetime | None:
    if not valor:
        return None
    try:
        return dt.datetime.strptime(valor, ISO)
    except ValueError:
        return None


def _fmt(valor: dt.datetime | None) -> str | None:
    return valor.strftime(ISO) if valor else None


class State:
    def __init__(self, caminho: Path | None = None) -> None:
        self.path = caminho or state_file()
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.executescript(SCHEMA)
        self._migra()

    def _migra(self) -> None:
        """Acrescenta colunas que versões novas passaram a usar.

        Um banco criado por versão anterior não tem as colunas novas, e
        `CREATE TABLE IF NOT EXISTS` não as adiciona. Sem isto, atualizar o
        programa quebraria a leitura do histórico que já existe.
        """
        novas = {
            "runs": {
                "prog_stage": "TEXT",
                "prog_done": "INTEGER NOT NULL DEFAULT 0",
                "prog_total": "INTEGER NOT NULL DEFAULT 0",
                "prog_label": "TEXT",
                "prog_pid": "INTEGER",
                "heartbeat": "TEXT",
                "cancel_at": "TEXT",
            },
            "queue": {
                "claimed_pid": "INTEGER",
                "claimed_at": "TEXT",
            },
        }
        # `full` virou `producao` quando produzir e enviar passaram a ser itens
        # separados. Renomear no lugar é obrigatório: deixar o nome antigo
        # conviver com o novo criaria duas linhas para a mesma janela, e o
        # backup rodaria duas vezes.
        self.conn.execute("UPDATE queue SET kind='producao' WHERE kind='full'")

        for tabela, colunas in novas.items():
            existentes = {
                linha["name"]
                for linha in self.conn.execute(f"PRAGMA table_info({tabela})")
            }
            for coluna, tipo in colunas.items():
                if coluna not in existentes:
                    self.conn.execute(
                        f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}"
                    )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Fila
    # ------------------------------------------------------------------

    def enqueue(self, job: str, due_at: dt.datetime, *, late: bool = False,
                kind: str = "producao", run_id: int | None = None) -> int | None:
        """Enfileira uma janela. Devolve None se ela já estava na fila.

        A chave única (job, due_at, kind) é o que torna o tick idempotente: ele
        pode rodar a cada minuto sem duplicar a execução das 3h.
        """
        agora = dt.datetime.now()
        try:
            cur = self.conn.execute(
                "INSERT INTO queue (job, due_at, enqueued_at, status, late, kind, run_id)"
                " VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (job, _fmt(due_at), _fmt(agora), int(late), kind, run_id),
            )
            return int(cur.lastrowid or 0)
        except sqlite3.IntegrityError:
            return None

    def claim_next(self, kinds: Iterable[str] | None = None) -> dict[str, Any] | None:
        """Pega o próximo item pendente, em transação.

        BEGIN IMMEDIATE trava a escrita antes de ler, então dois workers (ou um
        worker e um tick teimoso) nunca levam o mesmo item. É isto que torna
        seguro ter um laço por raia, cada um na sua thread.

        Sem `kinds`, pega de qualquer raia. É o que o `--uma-vez` usa.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if kinds:
                marcas = ",".join("?" for _ in kinds)
                sql = (f"SELECT * FROM queue WHERE status='pending' AND kind IN ({marcas})"
                       " ORDER BY due_at LIMIT 1")
                linha = self.conn.execute(sql, tuple(kinds)).fetchone()
            else:
                linha = self.conn.execute(
                    "SELECT * FROM queue WHERE status='pending' ORDER BY due_at LIMIT 1"
                ).fetchone()
            if linha is None:
                self.conn.execute("COMMIT")
                return None
            # Quem pegou e quando. É o que permite distinguir depois trabalho
            # em andamento de rastro de worker morto.
            self.conn.execute(
                "UPDATE queue SET status='running', claimed_pid=?, claimed_at=? WHERE id=?",
                (os.getpid(), _fmt(dt.datetime.now()), linha["id"]),
            )
            self.conn.execute("COMMIT")
            return dict(linha)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def finish_queue_item(self, queue_id: int, run_id: int | None = None) -> None:
        self.conn.execute(
            "UPDATE queue SET status='done', run_id=? WHERE id=?", (run_id, queue_id)
        )

    def queue_pending(self) -> list[dict[str, Any]]:
        linhas = self.conn.execute(
            "SELECT * FROM queue WHERE status IN ('pending','running') ORDER BY due_at"
        ).fetchall()
        return [dict(l) for l in linhas]

    def filas_presas(self) -> list[dict[str, Any]]:
        """Itens em 'running' cujo processo não existe mais.

        São o rastro de um worker que morreu entre o `claim_next` e o
        `finish_queue_item`. Enquanto a linha fica, `ja_na_fila` dá verdadeiro e
        o tick descarta toda janela futura daquele job, em silêncio, porque
        `queue_size` conta só 'pending' e o `status` segue dizendo "fila vazia".

        Decidir pelo pid, e não pela idade, é deliberado: um envio legítimo de
        seis horas tem claim antigo e está vivo, e roubar o item dele colocaria
        dois workers no mesmo backup.
        """
        linhas = self.conn.execute(
            "SELECT * FROM queue WHERE status='running' ORDER BY id"
        ).fetchall()
        return [dict(l) for l in linhas]

    def solta_fila(self, queue_id: int) -> None:
        """Marca o item como encerrado sem sucesso, liberando a janela.

        Não volta para 'pending': refazer sozinho um backup cujo worker morreu
        pode repetir trabalho caro sem ninguém pedir. A janela seguinte entra
        normalmente, e o tick registra a perdida se for o caso.
        """
        self.conn.execute(
            "UPDATE queue SET status='abandoned' WHERE id=?", (queue_id,)
        )

    def queue_por_raia(self) -> dict[str, dict[str, int]]:
        """Itens esperando e em andamento, por raia.

        Contar os dois importa, e separado. "Fila vazia" escondendo trabalho é
        o que fez um backup parar em silêncio: o item preso por worker morto
        fica em 'running', e um resumo que conta só os pendentes diz que não há
        nada enquanto aquele item descarta toda janela futura do job.
        """
        contas = {nome: {"esperando": 0, "andando": 0} for nome in RAIAS}
        for item in self.conn.execute(
            "SELECT kind, status, COUNT(*) AS n FROM queue"
            " WHERE status IN ('pending','running') GROUP BY kind, status"
        ):
            campo = "esperando" if item["status"] == "pending" else "andando"
            for nome, tipos in RAIAS.items():
                if item["kind"] in tipos:
                    contas[nome][campo] += item["n"]
        return contas

    def queue_size(self) -> int:
        linha = self.conn.execute(
            "SELECT COUNT(*) AS n FROM queue WHERE status='pending'"
        ).fetchone()
        return int(linha["n"])

    def clear_queue(self) -> None:
        self.conn.execute("DELETE FROM queue")

    # ------------------------------------------------------------------
    # Execuções
    # ------------------------------------------------------------------

    def insert_run(self, run: Run) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (job, started_at, finished_at, result, bytes, duration,"
            " artifact, error_stage, error_tried, error_got, error_cause, error_fix,"
            " retry_at, retry_count, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.job,
                _fmt(run.started_at),
                _fmt(run.finished_at),
                run.result.value,
                run.bytes,
                run.duration,
                run.artifact,
                run.error_stage.value if run.error_stage else None,
                run.error_tried,
                run.error_got,
                run.error_cause,
                run.error_fix,
                _fmt(run.retry_at),
                run.retry_count,
                json.dumps(_payload(run), ensure_ascii=False),
            ),
        )
        run.id = int(cur.lastrowid or 0)
        return run.id

    def update_run(self, run: Run) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, result=?, bytes=?, duration=?, artifact=?,"
            " error_stage=?, error_tried=?, error_got=?, error_cause=?, error_fix=?,"
            " retry_at=?, retry_count=?, payload=? WHERE id=?",
            (
                _fmt(run.finished_at),
                run.result.value,
                run.bytes,
                run.duration,
                run.artifact,
                run.error_stage.value if run.error_stage else None,
                run.error_tried,
                run.error_got,
                run.error_cause,
                run.error_fix,
                _fmt(run.retry_at),
                run.retry_count,
                json.dumps(_payload(run), ensure_ascii=False),
                run.id,
            ),
        )

    def progresso(
        self,
        run_id: int,
        *,
        stage: str,
        done: int = 0,
        total: int = 0,
        label: str = "",
        pid: int | None = None,
    ) -> None:
        """Marca onde a execução está. Uma escrita curta, chamada com frequência.

        Não toca no payload nem no resultado: é só a batida de coração mais o
        contador, para a escrita ser barata o bastante para acontecer a cada
        poucos segundos durante horas.
        """
        self.conn.execute(
            "UPDATE runs SET prog_stage=?, prog_done=?, prog_total=?, prog_label=?,"
            " prog_pid=COALESCE(?, prog_pid), heartbeat=? WHERE id=?",
            (stage, done, total, label, pid, _fmt(dt.datetime.now()), run_id),
        )

    def bate_coracao(self, run_id: int) -> None:
        """Só a hora, sem tocar em progresso.

        É o vigia dizendo "estou aqui" independente de haver entrada e saída
        rendendo. Separar isto do progresso é o que permite distinguir worker
        empacado dentro de uma chamada de worker morto.
        """
        self.conn.execute(
            "UPDATE runs SET heartbeat=?, prog_pid=COALESCE(prog_pid, ?) WHERE id=?",
            (_fmt(dt.datetime.now()), os.getpid(), run_id),
        )

    def pede_cancelamento(self, run_id: int) -> bool:
        """Marca o pedido. Devolve se havia execução para marcar."""
        cur = self.conn.execute(
            "UPDATE runs SET cancel_at=? WHERE id=? AND cancel_at IS NULL",
            (_fmt(dt.datetime.now()), run_id),
        )
        return cur.rowcount > 0

    def cancelamento_pedido(self, run_id: int) -> bool:
        linha = self.conn.execute(
            "SELECT cancel_at FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        return bool(linha and linha["cancel_at"])

    def progresso_de(self, run_id: int) -> dict | None:
        """O progresso cru, sem montar a execução inteira."""
        linha = self.conn.execute(
            "SELECT prog_stage, prog_done, prog_total, prog_label, prog_pid, heartbeat,"
            " cancel_at, started_at, job FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if linha is None:
            return None
        dados = dict(linha)
        dados["heartbeat"] = _parse(dados["heartbeat"])
        dados["cancel_at"] = _parse(dados["cancel_at"])
        dados["started_at"] = _parse(dados["started_at"])
        return dados

    def run_da_pasta(self, job: str, pasta: str) -> Run | None:
        """A execução que produziu aquela pasta de staging.

        A pasta é o timestamp de início, então a busca é por isso. Serve para a
        varredura de staging saber se pode apagar: execução viva ou com envio
        pendente é intocável.
        """
        try:
            inicio = dt.datetime.strptime(pasta, "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            return None
        linha = self.conn.execute(
            "SELECT * FROM runs WHERE job=? AND started_at=? ORDER BY id DESC LIMIT 1",
            (job, _fmt(inicio)),
        ).fetchone()
        return _row_to_run(linha) if linha else None

    def get_run(self, run_id: int) -> Run | None:
        linha = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _row_to_run(linha) if linha else None

    def runs(
        self,
        *,
        job: str | None = None,
        results: Iterable[RunResult] | None = None,
        since: dt.datetime | None = None,
        limit: int = 200,
    ) -> list[Run]:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list[Any] = []
        if job:
            sql += " AND job=?"
            params.append(job)
        if results:
            valores = [r.value for r in results]
            sql += f" AND result IN ({','.join('?' * len(valores))})"
            params.extend(valores)
        if since:
            sql += " AND started_at >= ?"
            params.append(_fmt(since))
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        return [_row_to_run(l) for l in self.conn.execute(sql, params).fetchall()]

    def count_runs(self, *, job: str | None = None) -> int:
        if job:
            linha = self.conn.execute("SELECT COUNT(*) AS n FROM runs WHERE job=?", (job,)).fetchone()
        else:
            linha = self.conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
        return int(linha["n"])

    def last_run(self, job: str) -> Run | None:
        linha = self.conn.execute(
            "SELECT * FROM runs WHERE job=? ORDER BY started_at DESC LIMIT 1", (job,)
        ).fetchone()
        return _row_to_run(linha) if linha else None

    def last_success(self, job: str) -> Run | None:
        linha = self.conn.execute(
            "SELECT * FROM runs WHERE job=? AND result=? ORDER BY started_at DESC LIMIT 1",
            (job, RunResult.OK.value),
        ).fetchone()
        return _row_to_run(linha) if linha else None

    def running(self) -> Run | None:
        linha = self.conn.execute(
            "SELECT * FROM runs WHERE result=? ORDER BY started_at DESC LIMIT 1",
            (RunResult.RUNNING.value,),
        ).fetchone()
        return _row_to_run(linha) if linha else None

    def orfas(self, limite_segundos: int = 90) -> list[Run]:
        """Execuções marcadas como rodando que pararam de dar sinal de vida.

        Uma delas é o rastro de um worker morto: o processo caiu entre o
        `insert_run` e o `update_run` final, e a linha ficou em "rodando" para
        sempre, escondendo o próximo backup atrás de um que já acabou.

        Só o carimbo de hora decide aqui; conferir se o processo ainda existe é
        de quem chama, porque este módulo não fala com o sistema operacional.
        Execuções antigas, de antes das colunas de progresso, têm heartbeat
        nulo e caem fora: sem batida nenhuma não há como distinguir um worker
        morto de um que nunca soube reportar.
        """
        corte = _fmt(dt.datetime.now() - dt.timedelta(seconds=limite_segundos))
        linhas = self.conn.execute(
            "SELECT * FROM runs WHERE result=? AND heartbeat IS NOT NULL AND heartbeat < ?"
            " ORDER BY id",
            (RunResult.RUNNING.value, corte),
        ).fetchall()
        return [_row_to_run(linha) for linha in linhas]

    def pid_de(self, run_id: int) -> int | None:
        linha = self.conn.execute(
            "SELECT prog_pid FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        return linha["prog_pid"] if linha else None

    def pending_uploads(self) -> list[Run]:
        linhas = self.conn.execute(
            "SELECT * FROM runs WHERE result=? ORDER BY started_at DESC",
            (RunResult.PENDING_UPLOAD.value,),
        ).fetchall()
        return [_row_to_run(l) for l in linhas]

    def delete_run(self, run_id: int) -> None:
        self.conn.execute("DELETE FROM runs WHERE id=?", (run_id,))

    def cancel_queue(self, job: str) -> int:
        """Tira da fila o que ainda não começou, e diz quantos eram.

        O que já está rodando não sai por aqui: quem para aquilo é o worker,
        ao perceber que o job sumiu.
        """
        cur = self.conn.execute(
            "DELETE FROM queue WHERE job=? AND status='pending'", (job,)
        )
        return cur.rowcount

    def delete_job_runs(self, job: str) -> int:
        cur = self.conn.execute("DELETE FROM runs WHERE job=?", (job,))
        self.conn.execute("DELETE FROM queue WHERE job=?", (job,))
        return cur.rowcount

    def prune_history(self, dias: int) -> int:
        corte = dt.datetime.now() - dt.timedelta(days=dias)
        cur = self.conn.execute("DELETE FROM runs WHERE started_at < ?", (_fmt(corte),))
        return cur.rowcount


# ----------------------------------------------------------------------------
# Serialização do payload
# ----------------------------------------------------------------------------

class PartesDeEnvio:
    """O que já subiu de um envio em partes, para poder continuar depois.

    Existe porque `upload_file` do boto3 é tudo ou nada: ele aborta o multipart
    quando falha, e um envio de doze gigabytes que levou três horas e quarenta
    e cinco minutos virou nada por causa de um tempo de leitura estourado na
    chamada final. O `upload_id` é o estado que precisa sobreviver ao processo
    morrer, porque sem ele as partes já no provedor são inalcançáveis e só
    restaria reenviar tudo.

    É um índice, não a verdade. A verdade é o `list_parts` do provedor, e quem
    retoma confere lá antes de decidir o que falta. Este registro serve para
    saber **qual** multipart continuar.

    Escrita só da thread que criou a conexão, como todo o resto do `State`. As
    partes sobem em paralelo, mas quem as registra é a thread principal, ao
    colher cada uma que termina.
    """

    def __init__(self, estado: "State", run_id: int) -> None:
        self.estado, self.run_id = estado, run_id

    def upload_id(self, destino: str, chave: str) -> str | None:
        linha = self.estado.conn.execute(
            "SELECT upload_id FROM upload_parts WHERE run_id=? AND destino=? AND chave=?"
            " LIMIT 1",
            (self.run_id, destino, chave),
        ).fetchone()
        return linha["upload_id"] if linha else None

    def guarda_upload(self, destino: str, chave: str, upload_id: str) -> None:
        """Registra o multipart aberto, antes de qualquer parte subir.

        A linha de número zero é só um marcador: ela guarda o `upload_id` para
        o caso de o processo morrer antes de a primeira parte terminar.
        """
        self.estado.conn.execute(
            "INSERT OR REPLACE INTO upload_parts"
            " (run_id, destino, chave, upload_id, part_number, etag, bytes)"
            " VALUES (?, ?, ?, ?, 0, NULL, 0)",
            (self.run_id, destino, chave, upload_id),
        )

    def guarda_parte(
        self, destino: str, chave: str, upload_id: str, numero: int,
        etag: str, tamanho: int,
    ) -> None:
        self.estado.conn.execute(
            "INSERT OR REPLACE INTO upload_parts"
            " (run_id, destino, chave, upload_id, part_number, etag, bytes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, destino, chave, upload_id, numero, etag, tamanho),
        )

    def partes(self, destino: str, chave: str) -> dict[int, str]:
        linhas = self.estado.conn.execute(
            "SELECT part_number, etag FROM upload_parts"
            " WHERE run_id=? AND destino=? AND chave=? AND part_number > 0",
            (self.run_id, destino, chave),
        ).fetchall()
        return {l["part_number"]: l["etag"] for l in linhas if l["etag"]}

    def esquece(self, destino: str, chave: str) -> None:
        self.estado.conn.execute(
            "DELETE FROM upload_parts WHERE run_id=? AND destino=? AND chave=?",
            (self.run_id, destino, chave),
        )

    def abertos(self) -> list[dict[str, Any]]:
        """Multiparts registrados e não concluídos, para abortar ao desistir.

        Guardar o `upload_id` é assumir a responsabilidade de abortar: parte
        pendurada no provedor ocupa espaço cobrado, e antes era o boto3 que
        abortava sozinho.
        """
        linhas = self.estado.conn.execute(
            "SELECT DISTINCT destino, chave, upload_id FROM upload_parts WHERE run_id=?",
            (self.run_id,),
        ).fetchall()
        return [dict(l) for l in linhas]


def _payload(run: Run) -> dict[str, Any]:
    return {
        "stages": [
            {
                "stage": s.stage.value,
                "state": s.state.value,
                "label": s.label,
                "detail": s.detail,
                "seconds": s.seconds,
            }
            for s in run.stages
        ],
        "manifest": [
            {"destination": m.destination, "sha256": m.sha256, "bytes": m.bytes, "verified": m.verified}
            for m in run.manifest
        ],
        "ignored_regex": run.ignored_regex,
        "ignored_manual": run.ignored_manual,
        "log": run.log,
        "destinations_done": run.destinations_done,
        "destinations_pending": run.destinations_pending,
    }


def _row_to_run(linha: sqlite3.Row) -> Run:
    dados = json.loads(linha["payload"] or "{}")
    return Run(
        id=int(linha["id"]),
        job=linha["job"],
        started_at=_parse(linha["started_at"]) or dt.datetime.now(),
        finished_at=_parse(linha["finished_at"]),
        result=RunResult(linha["result"]),
        bytes=linha["bytes"],
        duration=linha["duration"],
        artifact=linha["artifact"],
        error_stage=Stage(linha["error_stage"]) if linha["error_stage"] else None,
        error_tried=linha["error_tried"] or "",
        error_got=linha["error_got"] or "",
        error_cause=linha["error_cause"] or "",
        error_fix=linha["error_fix"] or "",
        retry_at=_parse(linha["retry_at"]),
        retry_count=int(linha["retry_count"] or 0),
        stages=[
            StageRecord(
                stage=Stage(s["stage"]),
                state=StageState(s["state"]),
                label=s.get("label", ""),
                detail=s.get("detail", ""),
                seconds=s.get("seconds"),
            )
            for s in dados.get("stages", [])
        ],
        manifest=[
            ManifestEntry(
                destination=m["destination"],
                sha256=m["sha256"],
                bytes=int(m.get("bytes", 0)),
                verified=bool(m.get("verified", True)),
            )
            for m in dados.get("manifest", [])
        ],
        ignored_regex=dados.get("ignored_regex", []),
        ignored_manual=dados.get("ignored_manual", []),
        log=[tuple(x) for x in dados.get("log", [])],
        destinations_done=dados.get("destinations_done", []),
        destinations_pending=dados.get("destinations_pending", []),
    )
