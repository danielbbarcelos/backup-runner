"""O worker: tira o backup de verdade.

Consome a fila em série, um job por vez, porque dois dumps pesados brigando por
disco demoram mais que os dois em sequência. Cada execução passa pelos mesmos
estágios, e cada estágio vira uma linha no registro:

    produzir → enviar para cada destino → retenção → avisar

O artefato nasce no staging e só sai de lá quando todos os destinos escolhidos
receberam. Se algum falhar, a execução fica marcada como pendente e o arquivo
continua no staging para o tick reenfileirar só o envio, sem refazer o dump.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import archive, destinations, mysql
from .config import DestinationStore, JobStore, Settings, decrypt, staging_dir
from .models import (
    ArchiveFormat,
    FilesSource,
    Job,
    ManifestEntry,
    MySQLSource,
    Run,
    RunResult,
    SourceKind,
    Stage,
    StageRecord,
    StageState,
)
from .state import PartesDeEnvio, State


class Cancelado(Exception):
    """Sinal recebido: termina o que está fazendo e sai."""


class JobRemovido(Exception):
    """O job foi apagado enquanto a execução dele acontecia.

    Não é falha: é ordem. Quem apaga um job está dizendo que não quer mais
    nada dele, e continuar o dump até o fim para então avisar por email e
    Slack sobre um job que não existe mais é o contrário do que foi pedido.
    """


@dataclass
class Resultado:
    run: Run
    ok: bool
    mensagem: str = ""


class Progresso:
    """Conta para fora o que está acontecendo aqui dentro.

    Um job de doze gigabytes leva quarenta minutos, e durante esses quarenta
    minutos o `status` só sabia dizer "rodando". Quem olhava não tinha como
    distinguir um backup andando de um worker travado. Cada chamada aqui grava
    a etapa, o quanto já foi e a hora do último sinal de vida, e é desse
    carimbo de hora que o `status` deduz que um worker morreu.

    A escrita é limitada a uma por segundo. O watcher do dump chama isto duas
    vezes por segundo e o tar a cada cem arquivos, o que num diretório grande
    dá centenas de chamadas por segundo; um UPDATE em cada uma faria o SQLite
    virar o gargalo do backup que ele só deveria estar observando.

    Um progresso que falha nunca derruba o job: o backup é o trabalho, isto
    aqui é a legenda.
    """

    def __init__(self, estado: State, run_id: int, *, intervalo: float = 1.0) -> None:
        self.estado, self.run_id, self.intervalo = estado, run_id, intervalo
        self.stage, self.label = "iniciando", ""
        self.done = self.total = 0
        self._ultima = 0.0

    def etapa(self, stage: str, label: str = "", *, total: int = 0) -> None:
        """Troca de etapa. Sempre escreve, porque etapa nova é notícia."""
        self.stage, self.label, self.done, self.total = stage, label, 0, total
        self._grava()

    def anda(self, done: int, total: int | None = None, label: str | None = None) -> None:
        """Avanço dentro da etapa atual, escrito no máximo uma vez por segundo."""
        self.done = done
        if total is not None:
            self.total = total
        if label is not None:
            self.label = label
        agora = time.monotonic()
        if agora - self._ultima < self.intervalo:
            return
        self._grava()

    def _grava(self) -> None:
        self._ultima = time.monotonic()
        try:
            self.estado.progresso(
                self.run_id, stage=self.stage, done=self.done,
                total=self.total, label=self.label, pid=os.getpid(),
            )
        except Exception:  # noqa: BLE001 - legenda quebrada não cancela backup
            pass


# ----------------------------------------------------------------------------
# Laço principal
# ----------------------------------------------------------------------------

def run_forever(*, intervalo: float = 5.0, uma_vez: bool = False) -> int:
    """Pega da fila e executa, até ser interrompido.

    O supervisord manda SIGTERM para parar. Aqui isso vira uma saída limpa
    depois do job atual, e não um dump cortado no meio.
    """
    parar = {"agora": False}

    def encerra(_sig, _frame):
        parar["agora"] = True

    signal.signal(signal.SIGTERM, encerra)
    signal.signal(signal.SIGINT, encerra)

    estado = State()
    try:
        while not parar["agora"]:
            item = estado.claim_next()
            if item is None:
                if uma_vez:
                    return 0
                time.sleep(intervalo)
                continue

            resultado = executa_item(item, estado)
            estado.finish_queue_item(item["id"], resultado.run.id if resultado.run else None)
            if uma_vez:
                return 0 if resultado.ok else 1
    finally:
        estado.close()
    return 0


def executa_item(item: dict, estado: State) -> Resultado:
    """Executa um item da fila: um job inteiro, ou só o reenvio pendente."""
    jobs = JobStore.load()
    job = jobs.get(item["job"])
    if job is None:
        run = Run(
            id=0, job=item["job"], started_at=dt.datetime.now(),
            finished_at=dt.datetime.now(), result=RunResult.FAILED,
            error_cause="o job não existe mais",
        )
        estado.insert_run(run)
        return Resultado(run, False, "job inexistente")

    if item.get("kind") == "upload_retry":
        return reenvia_pendentes(job, estado)
    return executa(job, estado, atrasado=bool(item.get("late")),
                   queue_id=item.get("id"))


# ----------------------------------------------------------------------------
# Execução completa
# ----------------------------------------------------------------------------

def executa(job: Job, estado: State, *, atrasado: bool = False,
            queue_id: int | None = None) -> Resultado:
    inicio = dt.datetime.now()
    run = Run(id=0, job=job.name, started_at=inicio, result=RunResult.RUNNING)
    run.log.append((inicio.strftime("%H:%M:%S"), "worker", f"execução de {job.name} iniciada"))
    estado.insert_run(run)

    pasta = staging_dir() / job.name / run.folder
    pasta.mkdir(parents=True, exist_ok=True)
    limite = inicio + dt.timedelta(minutes=job.timeout_minutes)
    prog = Progresso(estado, run.id)
    prog.etapa("preparando", job.name)

    # Tudo daqui para baixo acontece sob o vigia, que bate o coração e cobra o
    # prazo numa thread própria. O `with` garante que ele morre junto, inclusive
    # quando a execução sai por exceção.
    with Vigilancia(estado, run, job.name, limite, queue_id) as vigia:
        return _executa_vigiado(
            job, run, estado, pasta, limite, prog, Sentinela(vigia),
            inicio=inicio, atrasado=atrasado,
        )


def _executa_vigiado(
    job: Job, run: Run, estado: State, pasta: Path, limite: dt.datetime,
    prog: Progresso, sentinela: Sentinela, *,
    inicio: dt.datetime, atrasado: bool,
) -> Resultado:
    try:
        _produz(job, run, pasta, limite, prog, sentinela)
        _escreve_manifest(run, pasta, sentinela)
        _envia(job, run, pasta, estado, prog, sentinela)
    except Cancelado as exc:
        return _cancela(run, estado, pasta, str(exc))
    except mysql.MySQLError as exc:
        return _falha(run, estado, Stage.DUMP, exc, job)
    except archive.ArchiveError as exc:
        return _falha(run, estado, Stage.ARCHIVE, exc, job)
    except JobRemovido:
        return _desiste(run, estado, pasta)
    except TimeoutError as exc:
        return _falha(run, estado, run.error_stage or Stage.DUMP, exc, job, limpa=pasta)
    except Exception as exc:  # noqa: BLE001 - o worker não pode morrer por um job
        return _falha(run, estado, Stage.UPLOAD, exc, job)

    run.finished_at = dt.datetime.now()
    run.duration = (run.finished_at - inicio).total_seconds()
    run.result = RunResult.LATE if atrasado else RunResult.OK

    if run.destinations_pending:
        run.result = RunResult.PENDING_UPLOAD
        run.retry_at = run.finished_at + dt.timedelta(hours=1)
    else:
        _retencao(job, run)
        _limpa_staging(job, pasta, run)

    prog.etapa("concluído", run.artifact or "")
    run.log.append((run.finished_at.strftime("%H:%M:%S"), "ok", "execução concluída"))
    estado.update_run(run)
    _avisa(job, run)
    return Resultado(run, run.result in (RunResult.OK, RunResult.LATE))


def _produz(job: Job, run: Run, pasta: Path, limite: dt.datetime, prog: Progresso,
            sentinela: "Sentinela") -> None:
    """Gera o artefato no staging, comprimindo em fluxo."""
    if job.kind is SourceKind.MYSQL:
        _dump(job, run, pasta, limite, prog, sentinela)
    else:
        _arquiva(job, run, pasta, limite, prog, sentinela)


def _dump(job: Job, run: Run, pasta: Path, limite: dt.datetime, prog: Progresso,
          sentinela: "Sentinela") -> None:
    fonte: MySQLSource = job.source  # type: ignore[assignment]
    conexao = mysql.Connection(
        host=fonte.host, port=fonte.port, user=fonte.user,
        password=decrypt(fonte.password_enc) or "", database=fonte.database,
    )
    prog.etapa("lendo tabelas", fonte.database)
    info = mysql.list_tables_info(conexao)
    tabelas = [t.name for t in info]
    por_regex, por_mao = fonte.resolve_ignored(tabelas)
    run.ignored_regex, run.ignored_manual = por_regex, por_mao
    ignoradas = sorted(set(por_regex) | set(por_mao))

    run.log.append((
        dt.datetime.now().strftime("%H:%M:%S"), "dump",
        f"{len(tabelas)} tabelas, {len(ignoradas)} sem dados",
    ))

    # O information_schema dá o tamanho das tabelas que vão sair com dados, e é
    # daí que sai a porcentagem. É estimativa: o SQL de texto costuma ser maior
    # que o dado em disco, então o número passa de 100% em vez de mentir para
    # baixo, e a barra trata isso.
    fora = set(ignoradas)
    estimado = sum(t.bytes for t in info if t.name not in fora and not t.is_view)
    prog.etapa("dump", f"{len(tabelas) - len(ignoradas)} tabelas", total=estimado)

    resultado = mysql.run_dump(
        mysql.DumpRequest(
            connection=conexao,
            ignore_tables=ignoradas,
            output_file=pasta / f"dump_{fonte.database}.sql",
            log_file=pasta / "dump.log",
            compress=True,
        ),
        on_progress=lambda cru, _gravado: (sentinela(), prog.anda(cru)),
        on_phase=lambda fase: prog.etapa(
            "dump" if fase == "dump" else "estrutura",
            f"{len(tabelas) - len(ignoradas)} tabelas" if fase == "dump"
            else f"{len(ignoradas)} ignoradas", total=estimado if fase == "dump" else 0,
        ),
    )
    run.artifact = resultado.output_file.name
    run.bytes = resultado.bytes_written
    run.stages.append(StageRecord(
        Stage.DUMP, StageState.DONE, "dump",
        f"{len(tabelas) - len(ignoradas)} tabelas com dados", resultado.elapsed_seconds,
    ))
    run.stages.append(StageRecord(
        Stage.COMPRESS, StageState.DONE, "gzip",
        f"{_pct(resultado.ratio)} menor, em fluxo", 0.0,
    ))
    run.log.append((
        dt.datetime.now().strftime("%H:%M:%S"), "dump",
        f"{_tam(resultado.bytes_raw)} crus viraram {_tam(resultado.bytes_written)}",
    ))


def _arquiva(job: Job, run: Run, pasta: Path, limite: dt.datetime, prog: Progresso,
             sentinela: "Sentinela") -> None:
    fonte: FilesSource = job.source  # type: ignore[assignment]
    prog.etapa("medindo", fonte.path)

    def andamento(arquivos: int, cru: int, total_arquivos: int, total_bytes: int) -> None:
        sentinela()
        # A primeira chamada vem do percurso de medição, com zero lido e os
        # totais já conhecidos: é ela que troca "medindo" por "lendo".
        if prog.stage == "medindo":
            prog.etapa("lendo", f"{total_arquivos} arquivos", total=total_bytes)
            return
        prog.anda(cru, total_bytes, f"{arquivos} de {total_arquivos} arquivos")

    resultado = archive.create(
        archive.ArchiveRequest(
            source=Path(fonte.path),
            output_file=pasta / Path(fonte.path).name,
            excludes=fonte.active_excludes(),
            follow_links=fonte.follow_links,
            format=fonte.archive_format.value,
        ),
        on_progress=andamento,
    )
    run.artifact = resultado.output_file.name
    run.bytes = resultado.bytes_written
    run.stages.append(StageRecord(
        Stage.ARCHIVE, StageState.DONE, "leitura",
        f"{_plural(resultado.files, 'arquivo')}, {_tam(resultado.bytes_raw)}", resultado.elapsed_seconds,
    ))
    run.stages.append(StageRecord(
        Stage.COMPRESS, StageState.DONE, fonte.archive_format.value,
        f"{_tam(resultado.bytes_written)} finais, {_pct(resultado.ratio)} menor", 0.0,
    ))
    run.log.append((
        dt.datetime.now().strftime("%H:%M:%S"), "leitura",
        f"{_plural(resultado.files, 'arquivo')}"
        + (f", {resultado.skipped} fora pelas exclusões" if resultado.skipped else ""),
    ))


def _checa_prazo(limite: dt.datetime) -> None:
    if dt.datetime.now() > limite:
        raise TimeoutError("o job passou do tempo limite")


# De quanto em quanto o vigia acorda para bater o coração e reavaliar.
INTERVALO_VIGILANCIA = 5.0

# Quanto o vigia espera a thread principal reagir antes de encerrar na marra.
# Só é alcançado quando a principal está bloqueada dentro de uma chamada que
# não devolve controle, que é exatamente o caso que a cooperação não resolve.
GRACA_VIGILANCIA = 60.0


class Vigilancia(threading.Thread):
    """Bate o coração e cobra o prazo numa thread só sua.

    Esta classe existe por causa de um defeito de desenho que custou um backup
    de doze gigabytes. O worker informava que estava vivo, verificava o próprio
    prazo e contava o progresso pelo mesmo canal: o callback de progresso da
    biblioteca que fazia a entrada e saída. Quando a biblioteca bloqueou dentro
    de uma única chamada (o `CompleteMultipartUpload`, onze threads em
    `futex_wait` e uma em `do_poll`), as três coisas pararam juntas. "Empacado"
    e "morto" ficaram indistinguíveis, e o prazo de quatro horas deixou de ser
    cobrado justamente quando era necessário.

    Separando os sinais, cada um passa a dizer uma coisa só:

    - batida de coração: o processo existe e o laço está girando
    - progresso: a entrada e saída está rendendo
    - prazo: alguém cobra, com ou sem progresso

    E "batida fresca sem progresso" deixa de ser ambiguidade e passa a ser um
    estado legível: empacado numa chamada, não morto.

    A cobrança é em dois tempos. Primeiro o vigia publica o motivo, a
    `Sentinela` o lê no próximo callback e levanta a exceção, e aí a saída é
    limpa: staging removido, desfecho gravado, fila encerrada. Se a principal
    não reagir dentro da graça, porque está bloqueada e não vai voltar, o vigia
    grava o desfecho **antes** de encerrar o processo à força. Essa ordem não é
    detalhe: encerrar sem gravar recriaria a execução órfã e a linha de fila
    presa, trocando um travamento por outro.
    """

    def __init__(
        self,
        estado: State,
        run: Run,
        job: str,
        limite: dt.datetime,
        queue_id: int | None = None,
        *,
        intervalo: float | None = None,
        graca: float | None = None,
    ) -> None:
        super().__init__(name="vigilancia", daemon=True)
        # A conexão do chamador não serve: o sqlite3 recusa conexão usada fora
        # da thread que a criou, então o vigia abre a própria dentro do `run`.
        # O WAL já existe justamente para dois escritores conviverem.
        self.estado: State | None = None
        self.execucao, self.job = run, job
        self.limite, self.queue_id = limite, queue_id
        # Lidos aqui e não como valor padrão de argumento: valor padrão é
        # avaliado na definição da função, então ajustar a constante do módulo
        # depois (em teste, por exemplo) não surtiria efeito nenhum.
        self.intervalo = INTERVALO_VIGILANCIA if intervalo is None else intervalo
        self.graca = GRACA_VIGILANCIA if graca is None else graca
        self.motivo: tuple[str, str] | None = None   # (tipo, texto)
        self.falhas = 0
        self._desde = 0.0
        self._parar = threading.Event()

    # -- ciclo de vida ------------------------------------------------

    def parar(self) -> None:
        self._parar.set()

    def __enter__(self) -> "Vigilancia":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.parar()
        self.join(timeout=self.intervalo + 1)

    def run(self) -> None:
        # Nome exigido pela Thread. É por isso que a execução vigiada mora em
        # `self.execucao`: chamar o atributo de `run` sobrescreveria este
        # método, e a thread morreria na largada tentando chamar o dataclass.
        self.estado = State()
        try:
            while not self._parar.is_set():
                try:
                    self._ciclo()
                    self.falhas = 0
                except Exception as exc:  # noqa: BLE001
                    # Vigia que morre não pode derrubar o backup. Mas vigia
                    # mudo é pior que vigia nenhum, porque dá a impressão de
                    # que alguém está olhando. Então ele insiste e reclama.
                    self.falhas += 1
                    if self.falhas in (1, 10, 100):
                        print(
                            f"[vigia] falha {self.falhas} ao vigiar a execução"
                            f" {self.execucao.id}: {type(exc).__name__}: {exc}",
                            file=sys.stderr, flush=True,
                        )
                self._parar.wait(self.intervalo)
        finally:
            if self.estado is not None:
                self.estado.close()

    # -- o que ele faz a cada volta -----------------------------------

    def _ciclo(self) -> None:
        assert self.estado is not None
        self.estado.bate_coracao(self.execucao.id)
        motivo = self._por_que_parar()
        if motivo is None:
            self.motivo, self._desde = None, 0.0
            return
        if self.motivo is None:
            # Primeira vez: publica e deixa a thread principal reagir sozinha.
            self.motivo, self._desde = motivo, time.monotonic()
            return
        if time.monotonic() - self._desde > self.graca:
            self._encerra_na_marra(motivo)

    def _por_que_parar(self) -> tuple[str, str] | None:
        assert self.estado is not None
        if self.estado.cancelamento_pedido(self.execucao.id):
            return ("cancelado", "cancelada por você")
        if JobStore.load().get(self.job) is None:
            return ("job_removido", f"o job {self.job} foi apagado")
        if dt.datetime.now() > self.limite:
            return ("prazo", "o job passou do tempo limite")
        return None

    def _encerra_na_marra(self, motivo: tuple[str, str]) -> None:
        """Grava o desfecho e mata o processo. O systemd sobe outro limpo.

        Chegar aqui significa que a thread principal está presa numa chamada
        que não devolve controle. Python não interrompe syscall bloqueada de
        fora, então não existe saída elegante: o que existe é deixar o banco
        consistente antes de sair, para ninguém herdar o estrago.
        """
        import os as _os

        tipo, texto = motivo
        try:
            if tipo == "job_removido":
                # Mesmo desfecho do caminho cooperativo: quem apagou o job não
                # quer registro nem aviso. Deixar uma falha aqui ressuscitaria
                # uma linha para um job que já não existe.
                assert self.estado is not None
                self.estado.delete_run(self.execucao.id)
                if self.queue_id is not None:
                    self.estado.solta_fila(self.queue_id)
                return
            self.execucao.result = RunResult.FAILED
            self.execucao.finished_at = dt.datetime.now()
            self.execucao.duration = (self.execucao.finished_at - self.execucao.started_at).total_seconds()
            self.execucao.error_stage = (
                self.execucao.error_stage or _etapa_atual(self.estado, self.execucao)
            )
            self.execucao.error_cause = texto
            self.execucao.error_got = (
                "o worker ficou preso numa chamada que não devolveu controle,"
                f" e foi encerrado {int(self.graca)}s depois do pedido"
            )
            self.execucao.error_fix = (
                f"o artefato pode estar no staging. veja: backup-runner run-info {self.execucao.id}"
            )
            self.execucao.log.append((
                self.execucao.finished_at.strftime("%H:%M:%S"), "vigia",
                f"encerrado à força: {texto}",
            ))
            self.estado.update_run(self.execucao)
            if self.queue_id is not None:
                self.estado.solta_fila(self.queue_id)
        finally:
            # `_exit` e não `sys.exit`: a principal está bloqueada e um
            # SystemExit nela não chegaria a ser processado. Está no `finally`
            # para valer também no `return` acima e se a gravação falhar: uma
            # vez decidido encerrar, o processo sai de qualquer forma.
            _os._exit(_CODIGO_ENCERRADO_PELO_VIGIA)


_CODIGO_ENCERRADO_PELO_VIGIA = 75


class Sentinela:
    """A parte cooperativa da parada, chamada de dentro dos callbacks.

    Ela não consulta disco nem relógio de parede: só lê o que o vigia já
    decidiu. Antes esta classe relia o arquivo de jobs de dois em dois
    segundos, o que punha entrada e saída no caminho quente do progresso sem
    necessidade, e, pior, não funcionava justamente quando o callback parava de
    ser chamado.
    """

    def __init__(self, vigia: Vigilancia) -> None:
        self.vigia = vigia

    def __call__(self) -> None:
        motivo = self.vigia.motivo
        if motivo is None:
            return
        tipo, texto = motivo
        if tipo == "cancelado":
            raise Cancelado(texto)
        if tipo == "job_removido":
            raise JobRemovido(self.vigia.job)
        raise TimeoutError(texto)


# ----------------------------------------------------------------------------
# Envio
# ----------------------------------------------------------------------------

def _envia(
    job: Job, run: Run, pasta: Path, estado: State, prog: Progresso,
    sentinela: "Sentinela",
) -> None:
    destinos = DestinationStore.load()
    prefixo = f"{job.name}/{run.folder}"
    # Num artefato grande o envio é a etapa mais demorada, e a única em que a
    # espera não é culpa nossa. Saber que subiram 3 de 12 GB é a diferença
    # entre esperar e reiniciar o worker achando que travou.
    a_enviar = sum(f.stat().st_size for f in pasta.iterdir() if f.is_file())

    for ligacao in job.destinations:
        destino = destinos.get(ligacao.name)
        if destino is None or not destino.enabled:
            run.stages.append(StageRecord(
                Stage.UPLOAD, StageState.SKIPPED, ligacao.name,
                "destino ausente ou desativado",
            ))
            run.destinations_pending.append(ligacao.name)
            continue

        inicio = time.monotonic()
        prog.etapa("enviando", f"{ligacao.name}: {destino.location()}", total=a_enviar)

        def andamento(enviados_ate_agora: int) -> None:
            # O prazo do job também vale aqui. Sem esta checagem um envio lento
            # corria para sempre: o dump e o arquivamento olhavam o relógio, o
            # upload não, e é ele a etapa mais longa de um artefato grande.
            sentinela()
            prog.anda(enviados_ate_agora)

        try:
            motor = destinations.backend(destino)
            enviados = motor.upload(
                pasta, prefixo, on_progress=andamento,
                # O registro é o que transforma "recomeçar do zero" em
                # "continuar de onde parou" se este envio falhar.
                registro=PartesDeEnvio(estado, run.id),
                on_phase=lambda fase: prog.etapa(fase, ligacao.name, total=a_enviar),
            )
        except TimeoutError:
            # Fica pendente em vez de virar falha: o artefato continua no
            # staging e o tick reenfileira só o envio, sem refazer o backup.
            run.stages.append(StageRecord(
                Stage.UPLOAD, StageState.FAILED, ligacao.name,
                f"passou do limite de {job.timeout_minutes} min enviando",
                time.monotonic() - inicio,
            ))
            run.destinations_pending.append(ligacao.name)
            run.error_stage = Stage.UPLOAD
            run.error_cause = f"o envio não coube nos {job.timeout_minutes} min do job"
            run.error_fix = ("aumente o timeout do job, ou deixe o reenvio automático"
                             " terminar o que falta")
            run.log.append((
                dt.datetime.now().strftime("%H:%M:%S"), ligacao.name,
                "envio interrompido pelo prazo do job",
            ))
            continue
        except Exception as exc:  # noqa: BLE001
            run.stages.append(StageRecord(
                Stage.UPLOAD, StageState.FAILED, ligacao.name,
                destinations._resumo_erro(exc), time.monotonic() - inicio,
            ))
            run.destinations_pending.append(ligacao.name)
            run.error_stage = Stage.UPLOAD
            run.error_tried = f"enviar para {destino.location()}"
            run.error_got = destinations._resumo_erro(exc)
            run.error_cause = _causa(destino, exc)
            run.error_fix = "abra o destino, teste, e use retry quando resolver"
            run.log.append((
                dt.datetime.now().strftime("%H:%M:%S"), ligacao.name, "falhou no envio",
            ))
            continue

        run.destinations_done.append(ligacao.name)
        run.manifest.append(ManifestEntry(
            destination=ligacao.name,
            sha256=_sha256(pasta / (run.artifact or "")),
            bytes=enviados,
        ))
        run.stages.append(StageRecord(
            Stage.UPLOAD, StageState.DONE, ligacao.name,
            destino.location(), time.monotonic() - inicio,
        ))
        run.log.append((
            dt.datetime.now().strftime("%H:%M:%S"), ligacao.name,
            f"{_tam(enviados)} enviados",
        ))
    estado.update_run(run)


def reenvia_pendentes(job: Job, estado: State) -> Resultado:
    """Tenta de novo os destinos que faltaram, usando o artefato do staging.

    Não refaz o dump: o arquivo já existe, e refazer custaria uma leitura nova
    do banco de produção por um problema que é de rede.
    """
    if JobStore.load().get(job.name) is None:
        # O tick enfileirou o reenvio, o job sumiu antes de ele ser atendido.
        return Resultado(
            Run(id=0, job=job.name, started_at=dt.datetime.now(),
                finished_at=dt.datetime.now(), result=RunResult.OK),
            True, "job apagado, reenvio descartado",
        )

    pendentes = [r for r in estado.pending_uploads() if r.job == job.name]
    if not pendentes:
        run = Run(id=0, job=job.name, started_at=dt.datetime.now(),
                  finished_at=dt.datetime.now(), result=RunResult.OK,
                  error_cause="nada pendente")
        return Resultado(run, True, "nada a reenviar")

    run = pendentes[0]
    pasta = staging_dir() / job.name / run.folder
    if not pasta.is_dir():
        run.result = RunResult.FAILED
        run.error_cause = "o artefato não está mais no staging"
        run.error_fix = f"rode o job de novo: backup-runner run {job.name}"
        estado.update_run(run)
        return Resultado(run, False, "artefato sumiu")

    faltando = list(run.destinations_pending)
    run.destinations_pending = []
    run.retry_count += 1
    destinos = DestinationStore.load()
    prefixo = f"{job.name}/{run.folder}"
    a_enviar = sum(f.stat().st_size for f in pasta.iterdir() if f.is_file())
    prog = Progresso(estado, run.id)

    for nome in faltando:
        destino = destinos.get(nome)
        if destino is None:
            run.destinations_pending.append(nome)
            continue
        prog.etapa("reenviando", f"{nome}: {destino.location()}", total=a_enviar)
        try:
            enviados = destinations.backend(destino).upload(
                pasta, prefixo, on_progress=prog.anda,
                # Mesmo registro da primeira tentativa: é por ele que as partes
                # já no provedor são reaproveitadas em vez de reenviadas.
                registro=PartesDeEnvio(estado, run.id),
                on_phase=lambda fase: prog.etapa(fase, nome, total=a_enviar),
            )
        except Exception as exc:  # noqa: BLE001
            run.destinations_pending.append(nome)
            run.error_got = destinations._resumo_erro(exc)
            continue
        run.destinations_done.append(nome)
        run.manifest.append(ManifestEntry(nome, _sha256(pasta / (run.artifact or "")), enviados))
        run.stages.append(StageRecord(Stage.UPLOAD, StageState.DONE, nome, "reenviado", 0.0))
        run.log.append((dt.datetime.now().strftime("%H:%M:%S"), nome, "reenviado"))

    if run.destinations_pending:
        run.retry_at = dt.datetime.now() + dt.timedelta(hours=1)
        estado.update_run(run)
        _avisa(JobStore.load().get(job.name) or job, run)
        return Resultado(run, False, "ainda falta destino")

    run.result = RunResult.OK
    run.error_stage = None
    run.error_got = run.error_cause = run.error_fix = ""
    _retencao(job, run)
    _limpa_staging(job, pasta, run)
    estado.update_run(run)
    _avisa(job, run, recuperado=True)
    return Resultado(run, True, "reenvio completo")


# ----------------------------------------------------------------------------
# Depois do envio
# ----------------------------------------------------------------------------

def _retencao(job: Job, run: Run) -> None:
    """Apaga o que passou da idade, em cada destino, pela data na pasta.

    Só depois de o envio dar certo: apagar o antigo antes de o novo chegar é o
    jeito mais rápido de ficar sem backup nenhum.
    """
    destinos = DestinationStore.load()
    total = 0
    for ligacao in job.destinations:
        destino = destinos.get(ligacao.name)
        if destino is None or ligacao.name not in run.destinations_done:
            continue
        resultado = destinations.aplica_retencao(destino, job.name, ligacao.days(destino))
        total += len(resultado.apagadas)
        if resultado.apagadas:
            run.log.append((
                dt.datetime.now().strftime("%H:%M:%S"), "retenção",
                f"{ligacao.name}: {len(resultado.apagadas)} antigas apagadas",
            ))
    if total:
        run.stages.append(StageRecord(
            Stage.RETENTION, StageState.DONE, "retenção", f"{total} pastas antigas", 0.0,
        ))


def _limpa_staging(job: Job, pasta: Path, run: Run) -> None:
    """O staging é passagem, não cópia.

    O artefato some quando todos os destinos receberam, a menos que um dos
    destinos seja justamente uma pasta local, caso em que a cópia já está lá.
    """
    shutil.rmtree(pasta, ignore_errors=True)
    raiz = pasta.parent
    try:
        if raiz.is_dir() and not any(raiz.iterdir()):
            raiz.rmdir()
    except OSError:
        pass


def _escreve_manifest(run: Run, pasta: Path, sentinela: "Sentinela | None" = None) -> None:
    """Hash e tamanho do que foi produzido, gravados junto do artefato."""
    artefato = pasta / (run.artifact or "")
    dados = {
        "job": run.job,
        "started_at": run.started_at.isoformat(timespec="seconds"),
        "folder": run.folder,
        "artifact": run.artifact,
        "bytes": artefato.stat().st_size if artefato.exists() else 0,
        "sha256": _sha256(artefato, sentinela),
        "ignored_regex": run.ignored_regex,
        "ignored_manual": run.ignored_manual,
    }
    (pasta / "manifest.json").write_text(json.dumps(dados, indent=2, ensure_ascii=False))


def _sha256(caminho: Path, sentinela: "Sentinela | None" = None) -> str:
    """Hash do artefato, com a sentinela consultada no caminho.

    A 455 MB/s nesta máquina, doze gigabytes são 27 segundos. Pouco, mas eram
    27 segundos em que um cancelamento pedido não era obedecido.
    """
    if not caminho.exists():
        return ""
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            if sentinela is not None:
                sentinela()
            h.update(bloco)
    return h.hexdigest()


def _falha(run: Run, estado: State, estagio: Stage, exc: Exception, job: Job, *, limpa: Path | None = None) -> Resultado:
    run.finished_at = dt.datetime.now()
    run.duration = (run.finished_at - run.started_at).total_seconds()
    run.result = RunResult.FAILED
    run.error_stage = estagio
    run.error_got = destinations._resumo_erro(exc)
    if not run.error_cause:
        run.error_cause = type(exc).__name__
    run.stages.append(StageRecord(estagio, StageState.FAILED, estagio.value, run.error_got))
    run.log.append((run.finished_at.strftime("%H:%M:%S"), estagio.value, run.error_got))
    if limpa is not None:
        shutil.rmtree(limpa, ignore_errors=True)
    estado.update_run(run)
    _avisa(job, run)
    return Resultado(run, False, run.error_got)


def _desiste(run: Run, estado: State, pasta: Path) -> Resultado:
    """O job foi apagado no meio: apaga o rastro e cala a boca.

    Nada de registro de falha, nada de aviso. Quem apagou o job já sabe o que
    aconteceu com ele, e receber um email dizendo que o backup de um job
    inexistente foi interrompido é ruído pelo qual ninguém pediu.

    O artefato pela metade vai junto: ele só existia para este job.
    """
    aborta_multiparts(estado, run.id)
    shutil.rmtree(pasta, ignore_errors=True)
    raiz = pasta.parent
    try:
        if raiz.is_dir() and not any(raiz.iterdir()):
            raiz.rmdir()
    except OSError:
        pass
    estado.delete_run(run.id)
    return Resultado(run, True, f"{run.job} foi apagado durante a execução")


# De que etapa é cada rótulo de progresso. Serve para o desfecho dizer onde a
# execução estava, em vez de chutar "upload" para tudo.
ETAPA_DE = {
    "preparando": Stage.DUMP,
    "lendo tabelas": Stage.DUMP,
    "dump": Stage.DUMP,
    "estrutura": Stage.DUMP,
    "medindo": Stage.ARCHIVE,
    "lendo": Stage.ARCHIVE,
    "enviando": Stage.UPLOAD,
    "reenviando": Stage.UPLOAD,
    "fechando envio": Stage.UPLOAD,
}


def _etapa_atual(estado: State, run: Run) -> Stage:
    bruto = estado.progresso_de(run.id) or {}
    return ETAPA_DE.get(bruto.get("prog_stage") or "", Stage.UPLOAD)


def aborta_multiparts(estado: State, run_id: int) -> int:
    """Descarta os envios em partes registrados desta execução.

    Guardar o `upload_id` para poder retomar é assumir a responsabilidade de
    abortar: parte pendurada no provedor ocupa espaço cobrado, e antes era o
    boto3 que abortava sozinho ao falhar. Chamado só quando se desiste de vez,
    nunca entre tentativas, porque é justamente entre tentativas que as partes
    precisam continuar lá.
    """
    registro = PartesDeEnvio(estado, run_id)
    abertos = registro.abertos()
    if not abertos:
        return 0

    destinos = DestinationStore.load()
    abortados = 0
    for item in abertos:
        destino = destinos.get(item["destino"])
        if destino is None:
            continue
        motor = destinations.backend(destino)
        if hasattr(motor, "aborta") and motor.aborta(item["chave"], item["upload_id"]):
            abortados += 1
        registro.esquece(item["destino"], item["chave"])
    return abortados


def _cancela(run: Run, estado: State, pasta: Path, texto: str) -> Resultado:
    """Encerra a pedido, deixando registro e sem avisar por email nem Slack.

    Vira falha, porque o backup não aconteceu e o histórico não pode sugerir
    que aconteceu. Mas não dispara aviso: quem cancelou está olhando, e receber
    alerta do que você mesmo acabou de pedir é ruído.
    """
    run.result = RunResult.FAILED
    run.finished_at = dt.datetime.now()
    run.duration = (run.finished_at - run.started_at).total_seconds()
    run.error_stage = run.error_stage or _etapa_atual(estado, run)
    run.error_cause = texto
    run.error_fix = f"rode de novo quando quiser: backup-runner run {run.job}"
    run.log.append((run.finished_at.strftime("%H:%M:%S"), "cancelado", texto))
    aborta_multiparts(estado, run.id)
    shutil.rmtree(pasta, ignore_errors=True)
    raiz = pasta.parent
    try:
        if raiz.is_dir() and not any(raiz.iterdir()):
            raiz.rmdir()
    except OSError:
        pass
    estado.update_run(run)
    return Resultado(run, False, texto)


def _avisa(job: Job, run: Run, *, recuperado: bool = False) -> None:
    from . import notify

    try:
        notify.sobre_execucao(job, run, recuperado=recuperado)
    except Exception:  # noqa: BLE001 - aviso que falha não derruba o backup
        pass


def _causa(destino, exc: Exception) -> str:
    from .models import DestKind

    if destino.kind is DestKind.S3:
        return destinations._causa_s3(exc)
    if destino.kind is DestKind.SFTP:
        return destinations._causa_sftp(exc)
    return "não conseguiu escrever no destino"


def _plural(n: int, palavra: str) -> str:
    return f"{n} {palavra}" if n == 1 else f"{n} {palavra}s"


def _tam(n: int) -> str:
    from .format import format_bytes

    return format_bytes(n)


def _pct(fracao: float) -> str:
    return f"{fracao * 100:.0f}%".replace(".", ",")
