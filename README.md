# backup-runner

Backup agendado de bancos MySQL e de diretórios, com destinos em pasta local,
S3 compatível (DigitalOcean Spaces, AWS, Backblaze, MinIO) e SFTP.

Interface de terminal em Textual, navegação só por teclado.

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

| Comando | O que faz |
|---|---|
| `backup-runner` | abre a interface |
| `backup-runner status` | saúde do sistema, sem abrir a interface |
| `backup-runner install` | escreve o cron e gera o conf do supervisord |
| `backup-runner tick` | decide o que entra na fila (o cron chama isto) |
| `backup-runner tick --install` | só a linha do crontab |
| `backup-runner worker` | consome a fila (ainda não implementado) |
| `backup-runner demo` | popula dados de demonstração |
| `backup-runner self install` | instala o programa |
| `backup-runner self reinstall` | troca de versão, com `--ref` |
| `backup-runner self uninstall` | remove o programa |
| `backup-runner self status` | de onde veio a instalação atual |
| `backup-runner self releases` | releases publicados |

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

## Área de transferência

As telas que oferecem `c copiar` usam, nesta ordem, `wl-copy`, `xclip`, `xsel`
ou `pbcopy`, e conferem lendo de volta antes de dizer que copiaram. Sem
nenhuma dessas ferramentas, o texto aparece numa notificação longa para você
copiar com o mouse, junto com o comando que instala a que falta:

```sh
sudo apt install wl-clipboard   # Wayland
sudo apt install xclip          # X11
```

## Teclas

| Tecla | O que faz |
|---|---|
| `↑` `↓` | move no painel que está em foco |
| `tab` | alterna entre a lista de jobs e o detalhe |
| `enter` | abre o item em foco |
| `esc` | volta um nível, nunca fecha o app |
| `n` | novo job |
| `r` | enfileira o job agora |
| `p` | pausa ou retoma |
| `d` | apaga, com confirmação por nome |
| `h` `t` `a` `s` | histórico, destinos, avisos, saúde |
| `i` | instala o tick no crontab |
| `?` | ajuda da tela onde foi chamada |
| `q` | sai, a partir do dashboard |

No dashboard, o `tab` não é só decoração: ele entra no painel de detalhe, e
lá as setas andam pelas últimas execuções do job, com `enter` abrindo a
execução em foco. Com o foco na lista de jobs, o mesmo `enter` abre o
histórico completo daquele job. A barra de baixo diz qual dos dois vale no
momento.

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

A interface veio de uma especificação visual feita no Claude Design, com
paleta em tokens semânticos (`ui/theme.py`), alvo de 100 por 32 células e
comportamento definido para 80 colunas.
