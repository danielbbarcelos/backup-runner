"""`backup-runner apply`: o servidor descrito num YAML, sem segredo no arquivo."""
from __future__ import annotations

import pytest
import yaml

from backup_runner import aplica
from backup_runner.config import DestinationStore, JobStore, Settings, decrypt, staging_dir
from backup_runner.models import DestKind, FilesMode, SourceKind


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("BR_S3_KEY", "AKIAEXEMPLO")
    monkeypatch.setenv("BR_S3_SECRET", "segredo-s3")
    monkeypatch.setenv("BR_SLACK", "https://hooks.slack.com/services/T/B/x")
    yield


@pytest.fixture
def envfile(tmp_path):
    p = tmp_path / ".env"
    p.write_text('APP_ENV=production\nDB_PASSWORD_ADMIN="senha com espaço"\n')
    return p


def config(envfile, tmp_path, **extra):
    c = {
        "timezone": "America/Sao_Paulo",
        "staging_path": str(tmp_path / "staging"),
        "slack": {"webhook": {"env": "BR_SLACK"}},
        "s3": {"project": "peersaude", "region": "us-east-2",
               "access_key": {"env": "BR_S3_KEY"}, "secret_key": {"env": "BR_S3_SECRET"}},
        "jobs": {
            "peersaude-app": {"mysql": {"database": "live_peersaude_db",
                                        "password": {"dotenv": str(envfile), "key": "DB_PASSWORD_ADMIN"}},
                              "schedule": "0 3 * * *"},
            "peersaude-files": {"sync": {"path": str(tmp_path), "exclude": ["*.tmp"]},
                                "schedule": "0 4 * * *", "timeout_minutes": 1440},
        },
    }
    c.update(extra)
    return c


def test_cria_destinos_da_convencao_jobs_slack_e_staging(envfile, tmp_path):
    plano = aplica.planeja(config(envfile, tmp_path))
    aplica.aplica(plano)

    d = DestinationStore.load()
    banco, sync = d.get("s3-peersaude"), d.get("s3-peersaude-sync")
    assert (banco.kind, banco.bucket, banco.prefix, banco.endpoint) == (
        DestKind.S3, "backup-runner-peersaude", "mysql", "s3.us-east-2.amazonaws.com")
    assert (sync.prefix, sync.storage_class) == ("sync", "STANDARD_IA")
    assert decrypt(sync.secret_enc) == "segredo-s3"

    j = JobStore.load()
    app, files = j.get("peersaude-app"), j.get("peersaude-files")
    assert app.kind is SourceKind.MYSQL and decrypt(app.source.password_enc) == "senha com espaço"
    assert app.destinations[0].name == "s3-peersaude"
    assert app.source.ignore_regex == r"^telescope_|^pulse_"
    assert files.source.mode is FilesMode.SYNC and files.destinations[0].name == "s3-peersaude-sync"
    assert files.timeout_minutes == 1440 and files.source.active_excludes() == ["*.tmp"]

    s = Settings.load()
    assert s.channel_configured("slack") and staging_dir() == tmp_path / "staging"


def test_classe_dos_dumps_e_opcional_e_aparece_no_plano(envfile, tmp_path):
    aplica.aplica(aplica.planeja(config(envfile, tmp_path)))
    assert DestinationStore.load().get("s3-peersaude").storage_class == ""

    c = config(envfile, tmp_path)
    c["s3"]["mysql_storage_class"] = "STANDARD_IA"
    plano = aplica.planeja(c)
    assert plano.mudancas == 1, plano.linhas
    aplica.aplica(plano)
    d = DestinationStore.load()
    assert d.get("s3-peersaude").storage_class == "STANDARD_IA"
    assert d.get("s3-peersaude-sync").storage_class == "STANDARD_IA"


def test_aplicar_de_novo_nao_muda_nada(envfile, tmp_path):
    aplica.aplica(aplica.planeja(config(envfile, tmp_path)))
    plano = aplica.planeja(config(envfile, tmp_path))
    assert plano.mudancas == 0, plano.linhas


def test_troca_de_senha_no_env_aparece_como_segredo_alterado(envfile, tmp_path):
    aplica.aplica(aplica.planeja(config(envfile, tmp_path)))
    envfile.write_text('DB_PASSWORD_ADMIN="nova"\n')
    plano = aplica.planeja(config(envfile, tmp_path))
    assert ("alterar segredo", "job", "peersaude-app") in plano.linhas


def test_historico_e_avisos_do_job_sobrevivem(envfile, tmp_path):
    from backup_runner.models import Channel, NotifyEvent

    aplica.aplica(aplica.planeja(config(envfile, tmp_path)))
    j = JobStore.load()
    job = j.get("peersaude-app")
    job.notify.set(NotifyEvent.SUCCESS, Channel.EMAIL, True)
    criado = job.created
    j.put(job)
    aplica.aplica(aplica.planeja(config(envfile, tmp_path)))
    job = JobStore.load().get("peersaude-app")
    assert job.notify.get(NotifyEvent.SUCCESS, Channel.EMAIL) is True and job.created == criado


def test_prune_remove_so_com_a_flag(envfile, tmp_path):
    aplica.aplica(aplica.planeja(config(envfile, tmp_path)))
    cfg = config(envfile, tmp_path)
    del cfg["jobs"]["peersaude-files"]
    assert ("remover", "job", "peersaude-files") not in aplica.planeja(cfg).linhas
    aplica.aplica(aplica.planeja(cfg, prune=True))
    assert JobStore.load().get("peersaude-files") is None


@pytest.mark.parametrize("mexe, mensagem", [
    (lambda c: c["s3"].update(secret_key={"env": "NAO_EXISTE"}), "NAO_EXISTE não está definida"),
    (lambda c: c["jobs"]["peersaude-app"].update(schedule="todo dia"), "cron inválido"),
    (lambda c: c["jobs"]["peersaude-app"]["mysql"].update(password={"dotenv": "/nao/existe", "key": "X"}), "não existe"),
    (lambda c: c["jobs"].update(Errado={"sync": "/x", "schedule": "0 1 * * *"}), "letras minúsculas"),
    (lambda c: c.update(desconhecida=1), "chaves desconhecidas"),
    (lambda c: c["slack"].update(webhook="https://exemplo.com/x"), "incoming webhook"),
])
def test_erros_do_arquivo_explicam_onde(envfile, tmp_path, mexe, mensagem):
    c = config(envfile, tmp_path)
    mexe(c)
    with pytest.raises(aplica.ApplyError, match=mensagem):
        aplica.planeja(c)


def test_cli_dry_run_nao_grava(envfile, tmp_path, capsys):
    from backup_runner.__main__ import main

    arquivo = tmp_path / "servidor.yaml"
    arquivo.write_text(yaml.safe_dump(config(envfile, tmp_path)))
    assert main(["apply", str(arquivo), "--dry-run"]) == 0
    assert JobStore.load().get("peersaude-app") is None
    assert "criar" in capsys.readouterr().out
    assert main(["apply", str(arquivo)]) == 0
    assert JobStore.load().get("peersaude-app") is not None
