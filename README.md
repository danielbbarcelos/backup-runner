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
./install.sh
backup-runner install     # escreve o cron, gera o conf do supervisord
```

O `install` escreve no crontab do próprio usuário sozinho, porque isso não
precisa de sudo. Para o supervisord ele gera o arquivo e imprime as linhas de
sudo para você colar: o app nunca chama sudo por conta própria, já que
instalar um serviço que roda para sempre merece ser lido antes.

## Comandos

```
backup-runner                 abre a interface
backup-runner tick            decide o que entra na fila (o cron chama isto)
backup-runner tick --install  escreve a linha no crontab
backup-runner worker          consome a fila (ainda não implementado)
backup-runner status          saúde do sistema, sem abrir a interface
backup-runner demo            popula dados de demonstração
```

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

## Teclas

| Tecla | O que faz |
|---|---|
| `↑` `↓` | move na lista |
| `tab` | alterna o painel em foco |
| `enter` | abre o item |
| `esc` | volta um nível, nunca fecha o app |
| `n` | novo job |
| `r` | enfileira o job agora |
| `p` | pausa ou retoma |
| `d` | apaga, com confirmação por nome |
| `h` `t` `a` `s` | histórico, destinos, avisos, saúde |
| `i` | instala o tick no crontab |
| `?` | ajuda da tela onde foi chamada |
| `q` | sai, a partir do dashboard |

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

A interface veio de uma especificação visual feita no Claude Design, com
paleta em tokens semânticos (`ui/theme.py`), alvo de 100 por 32 células e
comportamento definido para 80 colunas.
