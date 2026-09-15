# backup-runner

Backup agendado de bancos MySQL e de diretórios, com destinos em pasta local,
S3 compatível (DigitalOcean Spaces, AWS, Backblaze, MinIO) e SFTP.

Interface de linha de comando, com um menu numerado para quem não quer decorar
comando. Sem framework de terminal: a saída é `print` e a entrada é `input`.

Sucessor funcional do `mysql-dumper` (`~/dev/labs/mysql`), que continua
existindo e não é tocado. O que passou de um para o outro é código copiado,
nunca importado: uma mudança lá não pode quebrar um backup agendado aqui.

## Estado

A interface, o modelo de dados, o agendador (`tick`) e a instalação funcionam.
**O worker ainda não existe**: a camada que de fato dumpa, compacta, envia e
avisa é a próxima frente. `backup-runner worker` sai com erro explícito em vez
de fingir que está de pé, para o supervisord mostrar o programa em FATAL e a
tela de saúde não mentir.

## Como funciona

Três processos, cada um com um papel:

```
crontab (uma linha)    * * * * * backup-runner tick
  tick    lê os jobs, compara com a última execução, escreve na fila e sai

supervisord            [program:backup-runner-worker]
  worker  consome a fila em série, um job por vez
```

O cron é o batimento resiliente: se o worker morrer, o tick continua
enfileirando e o supervisord ressuscita o worker, que pega a fila acumulada.
O worker em série garante que dois dumps pesados nunca briguem por disco.

### Janela perdida

Cada job tem uma janela de tolerância (padrão 6h). Dentro dela, uma janela
atrasada ainda roda, marcada como atrasada. Fora, é registrada como perdida e
o aviso sai: um backup de sábado tirado na segunda é só mais uma cópia de
segunda, não o dado de sábado.

### Retenção

Contada **no destino**, por idade, a partir da data no nome da pasta da
execução, nunca do mtime, porque copiar arquivo mexe no mtime. O arquivo local
é staging, não cópia: some ao fim do job, a menos que `local` seja um dos
destinos escolhidos.

```
peer-db/2026-09-14_03-00-00/
  dump_peer_saude_app.sql.gz
  mysqldump.log
  manifest.json
```

Sem espaço e sem dois-pontos no caminho: espaço vira `%20` em chave S3 e
dois-pontos quebram `scp` e `rsync`, que leem `host:caminho`. A ordenação
alfabética coincide com a cronológica.

## Instalação

```sh
pipx install "git+https://github.com/danielbbarcelos/backup-runner@latest"
backup-runner install
```

Se o `pipx` não estiver na máquina:

```sh
python3 -m pip install --user pipx && python3 -m pipx ensurepath
```

Depois de instalado, o programa se administra sozinho. `backup-runner self`
resolve a referência antes de chamar o pipx, então uma tag errada falha ali
mesmo, e não no meio de um clone.

### Atualizar

```sh
backup-runner self reinstall --ref latest        # o último release publicado
backup-runner self reinstall --ref v0.2.0        # uma versão específica
backup-runner self reinstall --ref 37be30d1      # um commit do main
backup-runner self reinstall --ref main          # a ponta do main, para testar
```

`latest` é o padrão, então `backup-runner self reinstall` sozinho já traz o
último release. Para instalar de um clone durante o desenvolvimento:

```sh
backup-runner self reinstall --local ~/dev/labs/backup-runner
```

O `--limpo` recria o ambiente do zero em vez de instalar por cima. Vale quando
uma versão deixa de usar uma dependência: o `pipx install --force` reaproveita
o ambiente e a biblioteca antiga fica para trás. O programa avisa quando
encontra uma dessas sobras.

```sh
backup-runner self reinstall --ref latest --limpo
```

### Ver o que está instalado

```sh
backup-runner self status      # versão, de onde veio, e em que referência
backup-runner self releases    # os releases publicados
```

A origem vem do metadata do próprio pipx, não de um arquivo de estado nosso,
então continua certa mesmo se alguém rodar `pipx install` na mão.

### Desinstalar

```sh
backup-runner self uninstall           # tira o programa, mantém jobs e histórico
backup-runner self uninstall --purge   # tira tudo, inclusive config e dados
```

A remoção tira junto a linha do crontab, que ficaria órfã apontando para um
binário que não existe mais. **Jobs, destinos, segredos e histórico ficam onde
estão**, porque desinstalar o programa e apagar os backups agendados são
decisões diferentes, e quem desinstala para reinstalar não quer perder o
cadastro. O `--purge` apaga também, e pergunta antes.

O worker do supervisord sai na mão, já que o programa não chama `sudo`:

```sh
sudo rm /etc/supervisor/conf.d/backup-runner.conf
sudo supervisorctl reread && sudo supervisorctl update
```

### Agendamento

O `backup-runner install` é outra coisa: ele não instala o programa, instala o
**agendamento**. Escreve a linha no crontab do próprio usuário sozinho, porque
isso não precisa de sudo, e gera o conf do supervisord para você aplicar. O
programa nunca chama `sudo` por conta própria, já que instalar um serviço que
roda para sempre merece ser lido antes.

## Comandos

Tudo que o programa faz é um comando. O menu (`backup-runner` sem argumento) é
só uma casca por cima deles, então não existe caminho que funcione apenas pelo
menu e tudo é automatizável.

| Comando | O que faz |
|---|---|
| `backup-runner` | menu numerado |
| `backup-runner status` | resumo: jobs, tick, worker, fila, próxima execução |
| `backup-runner jobs` | lista os jobs |
| `backup-runner job <nome>` | detalhe de um job |
| `backup-runner job add` | cadastra um job, por perguntas |
| `backup-runner job <nome> --editar` | edita |
| `backup-runner job <nome> --pausar` | pausa ou retoma |
| `backup-runner job <nome> --apagar` | apaga, pedindo o nome digitado |
| `backup-runner run <nome>` | põe um job na fila agora |
| `backup-runner history` | execuções (`--job`, `--falhas`, `--dias`) |
| `backup-runner run-info <nº>` | detalhe de uma execução |
| `backup-runner retry <nº>` | reenvia o artefato de uma execução pendente |
| `backup-runner dest` | destinos (`add`, `show`, `test`, `edit`, `rm`) |
| `backup-runner notify` | quais eventos avisam, e por onde |
| `backup-runner health` | diagnóstico |
| `backup-runner tick` | o que o cron chama |
| `backup-runner worker` | o que o supervisord chama |
| `backup-runner install` | instala o agendamento |
| `backup-runner self ...` | instala, remove e atualiza o programa |

### O menu

`backup-runner` sem argumento abre o menu. Num terminal, as setas andam e a
opção em foco ganha `→`; `enter` escolhe, `esc` volta um nível, e digitar o
número também funciona como atalho. Nas listas de marcação (destinos de um job,
tabelas a ignorar), `espaço` marca, `a` marca todos e `n` limpa.

Cada tela limpa e redesenha o mesmo cabeçalho com a trilha de onde se está, em
vez de empilhar saída no scrollback.

Nos campos de texto, o valor atual vem já preenchido e editável: as setas
laterais andam com o cursor, `Home` e `End` vão às pontas, e trocar uma porta
de 3306 para 3307 é mudar um caractere, não redigitar tudo.

Fora de um terminal (num pipe, num cron, num teste) nada disso existe, e a
escolha volta a ser por número. `BACKUP_RUNNER_SEM_SETAS=1` força esse modo.

### Em script

A saída dos comandos é texto simples, sem controle de tela, então funciona por
ssh ruim, dentro de `tmux`, e num terminal que não entende sequência de escape.
Com `NO_COLOR` ou fora de um terminal a cor some e a informação continua
inteira, o que faz isto valer num cron:

```sh
backup-runner history --falhas | mail -s "backups com falha" eu@exemplo.com
backup-runner health || echo "algo errado no backup"
```

Os códigos de saída seguem a convenção: `0` deu certo, `1` não encontrou ou
falhou, `2` erro de uso, `3` o worker ainda não existe.

## Onde ficam as coisas

```
~/.config/backup-runner/
  jobs.json          você edita, pode versionar
  destinations.json
  settings.json
~/.local/share/backup-runner/
  .key               chave Fernet, modo 600
  state.db           SQLite WAL: fila e histórico
  staging/           artefato em trânsito e pendentes de envio
```

A chave mora fora do diretório de config para o config poder ir para o git ou
para uma pasta sincronizada sem levar a chave junto.

### Sobre os segredos

A cifragem protege o arquivo copiado, sincronizado ou lido por engano. **Não
protege contra um processo rodando como o mesmo usuário**, e nenhum esquema
desacompanhado protege: se o worker consegue decifrar sozinho às três da
manhã, qualquer coisa rodando na sua conta também consegue. Isso está
registrado aqui para não haver ilusão depois.

## Desenvolvimento

```sh
PYTHONPATH=src python3 -m pytest tests/ -q
PYTHONPATH=src python3 -m backup_runner
```

Para ver a interface com dados sem tocar na configuração real:

```sh
export XDG_CONFIG_HOME=/tmp/br/config XDG_DATA_HOME=/tmp/br/data
PYTHONPATH=src python3 -m backup_runner demo --yes
PYTHONPATH=src python3 -m backup_runner
```

Para instalar o que está no clone, por cima da versão publicada:

```sh
backup-runner self reinstall --local .
```

### Como o código está dividido

O motor não sabe que existe terminal, e a camada de terminal não sabe o que é
um dump. Dá para trocar uma sem tocar na outra.

| Camada | Módulos |
|---|---|
| Motor | `models`, `config`, `state`, `schedule`, `tick`, `health`, `mysql` |
| Terminal | `console` (cor e tabela), `prompt` (perguntas), `views` (o que mostra), `forms` (cadastro), `menu` |
| Comandos | `__main__` |

`views` imprime e nunca pergunta; `forms` pergunta e só grava no fim. É o que
permite a mesma função servir ao comando direto e ao menu.
