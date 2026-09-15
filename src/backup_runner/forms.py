"""Cadastro por perguntas, uma de cada vez.

Nada é gravado antes do resumo final: cada fluxo monta o objeto em memória,
mostra o que vai salvar, e só então escreve. Desistir no meio (Ctrl-C) não
deixa metade de um job no disco.
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from . import console as c
from . import mysql, prompt
from .config import decrypt, encrypt
from .context import Context
from .format import format_bytes, format_count, format_relative
from .models import (
    ArchiveFormat,
    Destination,
    DestKind,
    ExcludePattern,
    FilesSource,
    Job,
    JobDestination,
    MySQLSource,
    SourceKind,
)
from .schedule import humanize, is_valid, next_run

AGENDAS = [
    ("0 3 * * *", "todo dia às 03:00"),
    ("30 3 * * *", "todo dia às 03:30"),
    ("0 */6 * * *", "a cada 6 horas"),
    ("0 3 * * 0", "todo domingo às 03:00"),
    ("0 3 1 * *", "todo dia 1 às 03:00"),
    ("outro", "escrever a expressão cron"),
]


def nome_valido(ctx: Context, atual: str = ""):
    def valida(valor: str) -> str | None:
        if not valor.replace("_", "").replace("-", "").isalnum():
            return "use letras, números, _ e -"
        if valor != atual and ctx.jobs.get(valor) is not None:
            return f"já existe um job chamado {valor}"
        return None

    return valida


# ----------------------------------------------------------------------------
# Job
# ----------------------------------------------------------------------------

def novo_job(ctx: Context) -> Job | None:
    tipo = prompt.escolhe(
        "o que este job vai guardar",
        [("mysql", "um banco MySQL  (dump com mysqldump)"),
         ("files", "um diretório    (tar.gz ou zip)")],
        padrao="mysql",
    )
    c.nota("um job tem uma fonte só: banco e storage do mesmo projeto são dois jobs")
    if tipo == "mysql":
        return job_mysql(ctx)
    return job_arquivos(ctx)


def job_mysql(ctx: Context, job: Job | None = None) -> Job | None:
    editando = job is not None
    fonte: MySQLSource = job.source if editando else MySQLSource()  # type: ignore[assignment]
    job = job or Job(name="", source=fonte, created=dt.date.today().isoformat())

    c.titulo("conexão" if not editando else f"editando {job.name}")
    job.name = prompt.texto(
        "nome do job", padrao=job.name, obrigatorio=True,
        valida=nome_valido(ctx, job.name),
    )
    fonte.host = prompt.texto("host", padrao=fonte.host or "127.0.0.1", obrigatorio=True)
    fonte.port = prompt.inteiro("porta", padrao=fonte.port or 3306, minimo=1, maximo=65535)
    fonte.user = prompt.texto("usuário", padrao=fonte.user or "root", obrigatorio=True)
    senha = prompt.senha("senha", manter=bool(fonte.password_enc))
    if senha:
        fonte.password_enc = encrypt(senha)
    fonte.database = prompt.texto("banco de dados", padrao=fonte.database, obrigatorio=True)

    tabelas = _testa_conexao(fonte, senha)
    if tabelas is None:
        if not prompt.confirma("continuar mesmo sem conseguir conectar", padrao=False):
            return None
        tabelas = []

    _escolhe_tabelas(fonte, tabelas)
    _agenda(job)
    _destinos(ctx, job)
    return _confirma_e_salva(ctx, job, tabelas)


def _testa_conexao(fonte: MySQLSource, senha: str) -> list | None:
    """Testa de verdade e devolve as tabelas, ou None se não conectou."""
    from .config import decrypt

    c.secao("testando a conexão")
    conn = mysql.Connection(
        host=fonte.host, port=fonte.port, user=fonte.user,
        password=senha or decrypt(fonte.password_enc) or "",
        database=fonte.database,
    )
    try:
        ok, mensagem = mysql.test_connection(conn)
    except mysql.MySQLError as exc:
        ok, mensagem = False, str(exc)

    if not ok:
        c.erro(f"não conectou: {mensagem}")
        c.nota("confira usuário, senha, e se o host aceita conexão desta máquina")
        return None

    try:
        tabelas = mysql.list_tables_info(conn)
    except mysql.MySQLError as exc:
        c.aviso(f"conectou, mas não listou as tabelas: {exc}")
        tabelas = []
    c.sucesso(f"conectado, MySQL {mensagem}, {len(tabelas)} tabelas")
    return tabelas


def _escolhe_tabelas(fonte: MySQLSource, tabelas: list) -> None:
    c.titulo("tabelas a ignorar", sub="a estrutura entra no dump, os dados não")
    c.nota("a regex é reavaliada a cada execução, então tabela nova que casar já entra ignorada")

    fonte.ignore_regex = prompt.texto("regex", padrao=fonte.ignore_regex)
    if not tabelas:
        c.nota("sem lista de tabelas, só a regex vale")
        return

    por_regex, por_mao = fonte.resolve_ignored(t.name for t in tabelas)
    c.sucesso(f"a regex pega {len(por_regex)} de {len(tabelas)} tabelas")
    if por_regex:
        c.nota(", ".join(por_regex[:10]) + (" …" if len(por_regex) > 10 else ""))

    if not prompt.confirma("escolher tabelas à mão também", padrao=False):
        return

    busca = prompt.texto("filtrar a lista por nome (Enter mostra todas)", padrao="")
    visiveis = [t for t in tabelas if busca.lower() in t.name.lower()] if busca else tabelas
    if len(visiveis) > 60:
        c.aviso(f"{len(visiveis)} tabelas: use um filtro para reduzir a lista")
        return

    conjunto_regex = set(por_regex)
    opcoes = []
    for t in visiveis:
        tamanho = format_bytes(t.bytes).rjust(9)
        linhas_txt = (f"{format_count(t.rows)} linhas" if not t.is_view else "view").rjust(16)
        marca = c.secondary(" (regex)") if t.name in conjunto_regex else ""
        opcoes.append((t.name, f"{t.name.ljust(28)}{linhas_txt} {tamanho}{marca}"))

    escolhidas = prompt.marca_varios(
        "alterne as que quer ignorar", opcoes, marcados=fonte.ignore_manual,
    )
    # A marca à mão vence a regex nos dois sentidos.
    fonte.ignore_manual = [n for n in escolhidas if n not in conjunto_regex]
    fonte.keep_manual = [n for n in conjunto_regex if n not in escolhidas]

    por_regex, por_mao = fonte.resolve_ignored(t.name for t in tabelas)
    total = len(por_regex) + len(por_mao)
    c.sucesso(f"{total} ignoradas, {len(tabelas) - total} entram no dump")


def job_arquivos(ctx: Context, job: Job | None = None) -> Job | None:
    editando = job is not None
    fonte: FilesSource = job.source if editando else FilesSource()  # type: ignore[assignment]
    job = job or Job(name="", source=fonte, created=dt.date.today().isoformat())

    c.titulo("origem" if not editando else f"editando {job.name}")
    job.name = prompt.texto(
        "nome do job", padrao=job.name, obrigatorio=True,
        valida=nome_valido(ctx, job.name),
    )

    def valida_dir(valor: str) -> str | None:
        caminho = Path(valor).expanduser()
        if not caminho.exists():
            return "não existe"
        if not caminho.is_dir():
            return "não é um diretório"
        return None

    fonte.path = prompt.texto("diretório", padrao=fonte.path, obrigatorio=True, valida=valida_dir)
    fonte.archive_format = ArchiveFormat(
        prompt.escolhe(
            "formato",
            [(ArchiveFormat.TARGZ.value, "tar.gz  (preserva permissão, dono e symlink)"),
             (ArchiveFormat.ZIP.value, "zip     (abre no Windows com duplo clique)")],
            padrao=fonte.archive_format.value,
        )
    )
    fonte.follow_links = prompt.confirma("seguir links simbólicos", padrao=fonte.follow_links)

    c.secao("padrões de exclusão")
    atuais = [e.pattern for e in fonte.excludes if e.enabled]
    if atuais:
        c.nota("atuais: " + ", ".join(atuais))
    entrada = prompt.texto(
        "padrões separados por vírgula (Enter mantém)",
        padrao=", ".join(atuais),
    )
    fonte.excludes = [
        ExcludePattern(p.strip(), True) for p in entrada.split(",") if p.strip()
    ]

    _previa(fonte)
    _agenda(job)
    _destinos(ctx, job)
    return _confirma_e_salva(ctx, job, [])


def _previa(fonte: FilesSource) -> None:
    """Mede o diretório de verdade, em vez de estimar."""
    if not prompt.confirma("calcular o tamanho agora", padrao=True):
        return
    import fnmatch
    import time

    inicio = time.monotonic()
    base = Path(fonte.path).expanduser()
    padroes = fonte.active_excludes()
    arquivos = bytes_ = fora = bytes_fora = 0
    for item in base.rglob("*"):
        if not item.is_file():
            continue
        relativo = str(item.relative_to(base))
        excluido = any(
            fnmatch.fnmatch(relativo, p) or fnmatch.fnmatch(item.name, p)
            or relativo.startswith(p.rstrip("*/"))
            for p in padroes
        )
        try:
            tamanho = item.stat().st_size
        except OSError:
            continue
        if excluido:
            fora += 1
            bytes_fora += tamanho
        else:
            arquivos += 1
            bytes_ += tamanho
    levou = time.monotonic() - inicio
    c.sucesso(f"{format_count(arquivos)} arquivos, {format_bytes(bytes_)}")
    if fora:
        c.nota(f"{format_count(fora)} arquivos e {format_bytes(bytes_fora)} ficaram fora pelas exclusões")
    c.nota(f"medido em {levou:.1f}s, não estimado".replace(".", ","))


def _agenda(job: Job) -> None:
    c.titulo("quando rodar")
    escolha = prompt.escolhe(
        "agenda",
        AGENDAS,
        padrao=job.schedule if any(job.schedule == k for k, _ in AGENDAS) else "outro",
    )
    if escolha == "outro":
        job.schedule = prompt.texto(
            "expressão cron (minuto hora dia mês dia-da-semana)",
            padrao=job.schedule,
            valida=lambda v: None if is_valid(v) else "cron inválido, precisa de cinco campos",
        )
    else:
        job.schedule = escolha

    proxima = next_run(job.schedule)
    if proxima:
        falta = (proxima - dt.datetime.now()).total_seconds()
        c.sucesso(f"{humanize(job.schedule)}, próxima em {format_relative(falta)}")

    if prompt.confirma("ajustar tolerância e tempo limite", padrao=False):
        c.nota("dentro da tolerância, uma janela perdida ainda roda quando a máquina liga")
        job.catch_up_window_minutes = prompt.inteiro(
            "tolerância de atraso, em minutos", padrao=job.catch_up_window_minutes, minimo=0,
        )
        job.timeout_minutes = prompt.inteiro(
            "tempo limite, em minutos", padrao=job.timeout_minutes, minimo=1,
        )
        job.stale_after_hours = prompt.inteiro(
            "avisar se ficar sem sucesso por quantas horas", padrao=job.stale_after_hours, minimo=1,
        )


def _destinos(ctx: Context, job: Job) -> None:
    c.titulo("para onde enviar")
    destinos = ctx.destinations.list()
    if not destinos:
        c.aviso("nenhum destino cadastrado")
        c.nota("sem destino, o artefato é descartado ao fim do job")
        c.nota("crie um depois com: backup-runner dest add")
        return

    atuais = [d.name for d in job.destinations]
    opcoes = [
        (d.name, f"{d.name.ljust(16)} {d.kind.value.ljust(6)} {d.location()}")
        for d in destinos
    ]
    escolhidos = prompt.marca_varios("marque os destinos", opcoes, marcados=atuais)

    novos = []
    for nome in escolhidos:
        destino = ctx.destinations.get(nome)
        atual = next((d for d in job.destinations if d.name == nome), None)
        padrao = atual.retention_days if atual and atual.retention_days else destino.retention_days
        dias = prompt.inteiro(f"manter quantos dias em {nome}", padrao=padrao, minimo=1)
        novos.append(JobDestination(nome, dias))
    job.destinations = novos


def _confirma_e_salva(ctx: Context, job: Job, tabelas: list) -> Job | None:
    c.titulo("confira antes de salvar", sub="nada foi gravado ainda")

    if job.kind is SourceKind.MYSQL:
        fonte = job.source
        c.linha("fonte", f"mysql  {fonte.database} @ {fonte.host}:{fonte.port}")
        if tabelas:
            por_regex, por_mao = fonte.resolve_ignored(t.name for t in tabelas)
            ignoradas = len(por_regex) + len(por_mao)
            c.linha("tabelas", f"{len(tabelas) - ignoradas} entram, {ignoradas} ignoradas")
    else:
        c.linha("fonte", f"arquivos  {job.source.path}  ({job.source.archive_format.value})")

    c.linha("quando", humanize(job.schedule))
    if job.destinations:
        for i, jd in enumerate(job.destinations):
            destino = ctx.destinations.get(jd.name)
            c.linha("destinos" if i == 0 else "", f"{jd.name} → manter {jd.days(destino)} dias")
    else:
        c.linha("destinos", c.warn("nenhum"))

    print()
    if not ctx.tick_ok:
        c.aviso("o tick não está no crontab, então este job não vai rodar sozinho")
        c.nota("instale depois com: backup-runner tick --install")

    if not prompt.confirma("salvar", padrao=True):
        c.info("cancelado, nada foi gravado")
        return None

    ctx.jobs.put(job)
    ctx.refresh()
    c.sucesso(f"job {job.name} salvo")
    return job


# ----------------------------------------------------------------------------
# Destino
# ----------------------------------------------------------------------------

def novo_destino(ctx: Context, destino: Destination | None = None) -> Destination | None:
    editando = destino is not None
    if not editando:
        tipo = prompt.escolhe(
            "tipo de destino",
            [("local", "pasta local   (no disco desta máquina)"),
             ("s3", "S3 compatível (DigitalOcean Spaces, AWS, Backblaze, MinIO)"),
             ("sftp", "SFTP          (um servidor por ssh)")],
            padrao="local",
        )
        destino = Destination(name="", kind=DestKind(tipo))

    c.titulo(f"destino {destino.kind.value}")

    def nome_livre(valor: str) -> str | None:
        if valor != destino.name and ctx.destinations.get(valor) is not None:
            return "já existe um destino com esse nome"
        return None

    destino.name = prompt.texto(
        "nome", padrao=destino.name, obrigatorio=True, valida=nome_livre,
    )

    if destino.kind is DestKind.LOCAL:
        destino.path = prompt.texto("caminho", padrao=destino.path or str(Path.home() / "backups"), obrigatorio=True)
        destino.create_missing = prompt.confirma("criar o diretório se faltar", padrao=True)
    elif destino.kind is DestKind.S3:
        destino.endpoint = prompt.texto(
            "endpoint", padrao=destino.endpoint or "nyc3.digitaloceanspaces.com", obrigatorio=True,
        )
        destino.region = prompt.texto("região", padrao=destino.region or "nyc3", obrigatorio=True)
        destino.bucket = prompt.texto("bucket", padrao=destino.bucket, obrigatorio=True)
        destino.prefix = prompt.texto("prefixo (opcional)", padrao=destino.prefix)
        destino.access_key = prompt.texto("access key", padrao=destino.access_key, obrigatorio=True)
        segredo = prompt.senha("secret key", manter=bool(destino.secret_enc))
        if segredo:
            destino.secret_enc = encrypt(segredo)
        c.nota("o secret é cifrado em disco, e nunca aparece no arquivo de config em claro")
    else:
        destino.host = prompt.texto("host", padrao=destino.host, obrigatorio=True)
        destino.port = prompt.inteiro("porta", padrao=destino.port or 22, minimo=1, maximo=65535)
        destino.user = prompt.texto("usuário", padrao=destino.user, obrigatorio=True)
        destino.auth = prompt.escolhe(
            "autenticação",
            [("key", "chave privada"), ("password", "senha")],
            padrao=destino.auth,
        )
        if destino.auth == "key":
            destino.private_key = prompt.texto(
                "caminho da chave", padrao=destino.private_key or "~/.ssh/id_ed25519",
                valida=lambda v: None if Path(v).expanduser().exists() else "esse arquivo não existe",
            )
        else:
            senha = prompt.senha("senha", manter=bool(destino.password_enc))
            if senha:
                destino.password_enc = encrypt(senha)
        destino.remote_path = prompt.texto("caminho remoto", padrao=destino.remote_path, obrigatorio=True)

    destino.retention_days = prompt.inteiro(
        "manter por quantos dias", padrao=destino.retention_days, minimo=1,
    )
    c.nota("a retenção é contada aqui no destino, pela data na pasta da execução")

    if prompt.confirma("testar agora", padrao=True):
        testa_destino(destino)

    if not prompt.confirma("salvar", padrao=True):
        c.info("cancelado, nada foi gravado")
        return None

    ctx.destinations.put(destino)
    ctx.refresh()
    c.sucesso(f"destino {destino.name} salvo")
    return destino


def testa_destino(destino: Destination) -> bool:
    """Escreve e apaga uma sonda. S3 e SFTP entram junto com o worker."""
    if destino.kind is not DestKind.LOCAL:
        c.aviso(f"o teste de {destino.kind.value} entra junto com o worker")
        c.linha("o que tentei", f"conectar em {destino.location()}", largura_rotulo=16)
        c.linha("causa provável", "a camada de envio ainda não foi implementada", largura_rotulo=16)
        return False

    caminho = Path(destino.path).expanduser()
    try:
        if not caminho.exists():
            if not destino.create_missing:
                c.erro("o diretório não existe e 'criar se faltar' está desligado")
                c.linha("como consertar", f"mkdir -p {caminho}", largura_rotulo=16)
                return False
            caminho.mkdir(parents=True, exist_ok=True, mode=0o700)
        sonda = caminho / f".probe-{dt.datetime.now():%m%d%H%M%S}"
        inicio = dt.datetime.now()
        sonda.write_text("backup-runner")
        sonda.unlink()
        levou = (dt.datetime.now() - inicio).total_seconds()
        c.sucesso(f"escreveu e apagou {sonda.name} em {levou:.1f}s".replace(".", ","))
        c.nota("permissões de leitura, escrita e remoção confirmadas")
        return True
    except OSError as exc:
        c.erro("o teste falhou")
        c.linha("o que tentei", f"escrever e apagar uma sonda em {caminho}", largura_rotulo=16)
        c.linha("o que recebi", str(exc), largura_rotulo=16)
        c.linha("como consertar", f"confira as permissões de {caminho}", largura_rotulo=16)
        return False


# ----------------------------------------------------------------------------
# Canais de aviso
# ----------------------------------------------------------------------------

def configura_canais(ctx: Context) -> None:
    """SMTP e Slack: sem isto, a matriz de avisos não tem por onde avisar."""
    escolha = prompt.escolhe(
        "qual canal",
        [("email", "email por SMTP"), ("slack", "Slack por webhook")],
        rotulo_saida="voltar",
    )
    if escolha == "email":
        configura_smtp(ctx)
    else:
        configura_slack(ctx)


def configura_smtp(ctx: Context) -> None:
    from . import notify

    smtp = dict(ctx.settings.smtp)
    c.titulo("email por SMTP")

    smtp["host"] = prompt.texto("servidor", padrao=smtp.get("host", ""), obrigatorio=True)
    smtp["port"] = prompt.inteiro(
        "porta", padrao=int(smtp.get("port", 587) or 587), minimo=1, maximo=65535,
    )
    c.nota("587 usa STARTTLS, 465 usa TLS direto; o programa escolhe pela porta")

    smtp["user"] = prompt.texto("usuário", padrao=smtp.get("user", ""))
    if smtp["user"]:
        senha = prompt.senha("senha", manter=bool(smtp.get("password_enc")))
        if senha:
            smtp["password_enc"] = encrypt(senha)
        c.nota("a senha é cifrada em disco, como as dos bancos e destinos")

    smtp["from"] = prompt.texto(
        "remetente", padrao=smtp.get("from") or smtp.get("user", ""), obrigatorio=True,
    )
    smtp["to"] = prompt.texto(
        "enviar para", padrao=smtp.get("to", ""), obrigatorio=True,
    )

    ctx.settings.smtp = smtp
    ctx.settings.save()
    c.sucesso("SMTP salvo")

    if prompt.confirma("mandar uma mensagem de teste agora", padrao=True):
        resultado = notify.teste("email")
        if resultado.ok:
            c.sucesso(f"enviado para {resultado.detalhe}")
        else:
            c.erro("não enviou")
            c.linha("o que recebi", resultado.detalhe, largura_rotulo=16)
            c.linha("causa provável", _causa_smtp(resultado.detalhe), largura_rotulo=16)


def _causa_smtp(erro: str) -> str:
    texto = erro.lower()
    if "authentication" in texto or "535" in texto:
        return "usuário ou senha recusados pelo servidor"
    if "name or service not known" in texto or "getaddrinfo" in texto:
        return "o servidor não resolve; confira o nome"
    if "timed out" in texto or "timeout" in texto:
        return "o servidor não respondeu na porta informada"
    if "starttls" in texto or "ssl" in texto:
        return "descompasso de TLS; tente a outra porta (587 ou 465)"
    return "resposta inesperada do servidor"


def configura_slack(ctx: Context) -> None:
    from . import notify

    slack = dict(ctx.settings.slack)
    c.titulo("Slack por webhook")
    c.nota("crie em api.slack.com/apps → Incoming Webhooks → Add New Webhook")
    c.nota("a URL aponta para um canal específico, escolhido lá")

    atual = decrypt(slack.get("webhook_enc"))
    if atual:
        c.linha("webhook atual", "•" * 24 + f"  (termina em {atual[-6:]})")
    url = prompt.senha("URL do webhook", manter=bool(atual))
    if url:
        if not url.startswith("https://hooks.slack.com/"):
            c.aviso("a URL não parece um webhook do Slack")
            if not prompt.confirma("usar mesmo assim", padrao=False):
                return
        slack["webhook_enc"] = encrypt(url)

    slack["channel"] = prompt.texto(
        "rótulo do canal, só para aparecer aqui",
        padrao=slack.get("channel", "#backups"),
    )

    ctx.settings.slack = slack
    ctx.settings.save()
    c.sucesso("Slack salvo")

    if prompt.confirma("mandar uma mensagem de teste agora", padrao=True):
        resultado = notify.teste("slack")
        if resultado.ok:
            c.sucesso(f"enviado para {resultado.detalhe or 'o canal do webhook'}")
        else:
            c.erro("não enviou")
            c.linha("o que recebi", resultado.detalhe, largura_rotulo=16)
            c.linha("causa provável", _causa_slack(resultado.detalhe), largura_rotulo=16)


def _causa_slack(erro: str) -> str:
    if "404" in erro:
        return "o webhook não existe mais, ou a URL está errada"
    if "403" in erro:
        return "o app perdeu acesso ao canal"
    if "410" in erro:
        return "o webhook foi revogado"
    return "o Slack não aceitou a mensagem"
