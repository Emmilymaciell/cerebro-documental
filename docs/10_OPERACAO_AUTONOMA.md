# Operação autônoma — o Cérebro rodando sozinho, por dias

> Este manual é para operar **sem nenhuma sessão de IA aberta**. É Python,
> SQLite e o Agendador de Tarefas do Windows. Nada aqui chama Claude, ChatGPT
> ou qualquer API de IA, e nenhum documento sai desta máquina.

```powershell
cd "C:\caminho\exemplo\cerebro-documental"
```

---

## 1. O comando único

Tudo se faz por **um** script:

```powershell
python scripts\cerebro.py processar     # processa a fila (é o que o Agendador chama)
python scripts\cerebro.py status        # onde tudo está + ETA medido
python scripts\cerebro.py pausar        # para com segurança (termina o item atual)
python scripts\cerebro.py retomar       # desfaz a pausa
python scripts\cerebro.py parar         # encerra a sessão depois do item atual
python scripts\cerebro.py relatorio     # relatório da sessão + erros por categoria
python scripts\cerebro.py erros         # só os erros, agrupados por causa
python scripts\cerebro.py cobertura     # quanto já foi lido, por categoria (--por prioridade)
python scripts\cerebro.py sync          # delta -> propagação -> ingestão; SIMULA por padrão
python scripts\cerebro.py sync --aplicar
```

`cobertura` põe cada estado numa coluna só. `UNSUPPORTED` (decisão, não
pendência — REGRA 6), `SKIPPED` e `MISSING` ficam **fora** do denominador do
"lido %"; a coluna `fila` conta pelo **estado**, então inclui os itens fora do
alcance da credencial (`T-12`) — a fila que o worker de fato pega é a do
`status`. `sync` delega ao `18_sincronizar.py` e devolve os códigos dele.

`scripts\worker.py` e `scripts\control.py` continuam funcionando igual — o
`cerebro.py` só reúne tudo num verbo só. Se você já tem um dedo viciado em
`worker.py run`, pode continuar; é o mesmo motor e o mesmo cadeado.

### Códigos de saída (é assim que o Agendador sabe o que houve)

| Código | Significa | O que fazer |
|---:|---|---|
| `0` | rodou e encerrou limpo (fila vazia, janela fechada, teto atingido, pausa) | nada |
| `2` | banco sem schema, ou `--concorrencia` > 1 | `python scripts\00_init_db.py` |
| `3` | **já existe worker rodando** — o lock está tomado; nada foi feito | nada: é o comportamento correto |
| `4` | há `PAUSE`/`STOP` pendente | `python scripts\cerebro.py retomar` |
| `5` | busca v5 aplicada com versão de normalizador/corte diferente do código | `python scripts\05_migrar_v5.py` |
| `6` | **`AGUARDANDO_CREDENCIAL`**: o Graph recusou a credencial do aplicativo 3 vezes seguidas (`401`, login recusado, ou `403` sem prova de acesso). Nenhuma tentativa de documento foi gasta | renovar `GRAPH_CLIENT_SECRET` no `.env` (ou a permissão do app no site) — ver seção 5, *A credencial expirou* |

Nenhum deles é motivo para pânico. `3` e `4` são o sistema se protegendo.
`6` é o único que pede uma ação sua — e por isso não é `0`: o Agendador
mostra "último resultado 0x6". A tarefa continua disparando a cada 30 min e,
assim que o segredo for renovado, a fila volta a andar sozinha.

---

## 2. Ligar de vez: a tarefa do Windows

### A receita curta (PowerShell, na sua conta)

```powershell
$py   = "C:\caminho\exemplo\Python312\python.exe"
$raiz = "C:\caminho\exemplo\cerebro-documental"

$acao = New-ScheduledTaskAction -Execute $py `
        -Argument "scripts\cerebro.py processar" -WorkingDirectory $raiz

# Sobe ao entrar no Windows e TENTA de novo a cada 30 min, para sempre.
$gatilho = New-ScheduledTaskTrigger -AtLogOn
$gatilho.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration ([TimeSpan]::MaxValue)).Repetition

$config = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
          -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
          -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "CerebroDocumental" -Action $acao `
          -Trigger $gatilho -Settings $config -Description `
          "Processamento autonomo do Cerebro Documental (sem IA)"
```

**Por que repetir a cada 30 minutos se o worker roda até acabar a fila?**
Porque essa é a recuperação de tudo de uma vez só. A tarefa dispara, e:

- se já há worker rodando → sai com `3` em menos de um segundo (o lock avisa);
- se a janela está fechada → sai com `0` e volta daqui a 30 min;
- se você pausou → sai com `4` e **não** desfaz a sua pausa;
- se caiu a luz / o worker morreu → **sobe e continua de onde parou.**

Você não precisa se lembrar de nada. A tarefa bate na porta; o sistema decide.

### O XML equivalente

Salve como `CerebroDocumental.xml` e importe com
`schtasks /Create /TN CerebroDocumental /XML CerebroDocumental.xml`
(ou pelo Agendador de Tarefas → *Importar Tarefa*).

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Processamento autonomo do Cerebro Documental (sem IA)</Description>
    <URI>\CerebroDocumental</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>MAQUINA\usuario</UserId>
      <Repetition>
        <Interval>PT30M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>MAQUINA\usuario</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT10M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\caminho\exemplo\Python312\python.exe</Command>
      <Arguments>scripts\cerebro.py processar --quieto</Arguments>
      <WorkingDirectory>C:\caminho\exemplo\cerebro-documental</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

Três linhas importantes desse XML:

- `MultipleInstancesPolicy = IgnoreNew` — o Windows nem tenta abrir um segundo.
  Mesmo que tentasse, o **lock** (seção 4) barraria.
- `ExecutionTimeLimit = PT0S` — sem prazo. O worker pode ficar dias.
- `AllowHardTerminate = false` — o Windows não mata o processo no meio de um
  item. Para parar, use `cerebro.py pausar`.

### Conferir depois

```powershell
Get-ScheduledTaskInfo -TaskName "CerebroDocumental"    # ultima execucao e resultado
Start-ScheduledTask   -TaskName "CerebroDocumental"    # disparar na mao
Disable-ScheduledTask -TaskName "CerebroDocumental"    # desligar por uns dias
```

`LastTaskResult` é o código de saída da tabela acima.

---

## 3. Ler as métricas

```powershell
python scripts\cerebro.py status
```

```
==================================================================
CEREBRO — PAUSADO  (comando pendente: PAUSE)
==================================================================
  arquivos no catalogo  132.508
  ja cobertos           199
  fila                  61.337   (220.6 GB)

  ritmo                 39.7 itens/min   [127 itens em 3.2 min de worker ligado]
  banda                 595.0 KB/s recente · 439.8 KB/s historico
  falta                 25.8 h  ->  2026-09-28 12:33

  espaco livre          75.7 GB
  derivados             0.00 GB
  ultimo heartbeat      2026-09-21 16:32:10
  ...
  lock                  livre  (cerebro.db.lock)
  orcamento             disco livre >= 25 GB · derivados <= 60 GB · ...
  janela                sempre (janela desligada)  [aberta]
```

O que cada número quer dizer:

| Linha | De onde vem | Como ler |
|---|---|---|
| `fila` | `SELECT COUNT(*) FROM v_fila_nuvem` | é contagem, não estimativa |
| `ritmo` | intervalos entre eventos reais do worker | e o colchete diz **quantos** itens e **quanto tempo** deram esse número |
| `banda` | `extracao` × `arquivo.tamanho_bytes` | "recente" é a amostra atual; "histórico" é a média de todas as sessões |
| `falta` | `fila ÷ ritmo` | recalibra sozinho a cada sessão |
| `ultimo heartbeat` | `controle.heartbeat` | se o worker está vivo, isso muda a cada ~20 s |

**Nada aqui é chute** (REGRA 7). Quando não há medição, ele escreve
`sem medicao` em vez de inventar um número.

Métricas cruas, se você quiser conferir na mão:

```powershell
python scripts\operacao.py metricas
python scripts\operacao.py erros
python scripts\operacao.py sessao
python scripts\99_status.py
```

### O número que importa

`ritmo` é medido **só enquanto o worker esteve ligado**. Uma noite de
computador desligado não conta como lentidão — e um intervalo entre duas
sessões também não. Por isso o ETA não despenca para "meses" só porque você
não rodou nada no fim de semana.

---

## 4. As quatro travas que fazem ele se cuidar sozinho

### 4.1 O lock de processo — um worker de cada vez

Arquivo: `data\cerebro.db.lock` (fora do Git, é runtime).

É um **lock de arquivo do sistema operacional**, não um número de PID gravado
numa tabela. A diferença importa:

- o `pid` na tabela `controle` continua existindo, mas só como **diagnóstico
  legível** ("quem estava segurando?"). Ele **não decide** mais nada;
- PID é reciclado. Depois de um reboot, o `pid` deixado por um worker morto
  pode pertencer a outro programa qualquer — e aí a fila ficaria travada para
  sempre, sem ninguém rodando. Já aconteceu em projetos assim;
- o lock do sistema operacional é **devolvido pelo núcleo** quando o processo
  morre, de qualquer jeito: Ctrl+C, `kill`, tela azul, queda de energia.
  Não existe "lock preso";
- e ele é atômico: dois `processar` disparados no mesmo segundo não passam os
  dois.

```powershell
python scripts\operacao.py trava      # "ocupada: sim/nao" e quem foi o ultimo dono
```

> Se `status` mostrar **`RODANDO` no banco mas o lock livre**, isso quer dizer
> que a sessão anterior morreu sem encerrar. **Não há nada a fazer.** O próximo
> `processar` assume, avisa na tela e devolve os órfãos para a fila.

### 4.2 Orçamento — disco, banda, derivados, itens

Tudo em `config\projeto.yaml`. As três primeiras já existiam:

| Chave | Padrão | O que faz | Estado ao bater |
|---|---:|---|---|
| `nuvem.espaco_livre_minimo_gb` | 25 | piso de espaço livre em disco | `PAUSA_SEGURA_DISCO` |
| `worker.derivados_max_gb` | 60 | teto do que os textos extraídos podem ocupar | `PAUSA_SEGURA_DISCO` |
| `worker.max_bytes_sessao_gb` | `0` (sem teto) | **teto de download por sessão** | `LIMITE_BANDA` |
| `worker.max_itens_sessao` | `0` (sem teto) | teto de itens por sessão | `LIMITE_SESSAO` |

As duas últimas são novas e vêm **desligadas**: sem mexer em nada, o
comportamento é o de antes. Para uma rodada curta sem editar arquivo nenhum:

```powershell
python scripts\cerebro.py processar --limite 200          # 200 itens e para
python scripts\cerebro.py processar --max-gb-sessao 5     # 5 GB baixados e para
```

Bater num teto **não é erro**: o worker termina o item que estava fazendo,
grava tudo, registra o motivo no checkpoint e sai com código `0`. A fila
continua exatamente onde estava, e a próxima sessão pega de onde parou.

### 4.3 Janela de execução — dividir a banda com o escritório

Desligada por padrão. Para ligar, acrescente em `config\projeto.yaml`, dentro
de `worker:`:

```yaml
worker:
  janela:
    ativa: true
    inicio: "19:00"
    fim:    "07:00"
    dias:   [0, 1, 2, 3, 4]    # 0=segunda ... 6=domingo; [] = todos os dias
```

A janela pode atravessar a meia-noite (`19:00 → 07:00`) — e nesse caso o dia
que conta é o dia em que ela **abriu**: sábado 2h ainda é a janela da sexta.

Fora da janela, o `processar` **nem abre sessão**: escreve `FORA_DA_JANELA`
no controle, diz quando a janela reabre e sai com `0`. Ele não fica dormindo
segurando o lock. Quem o traz de volta é a tarefa agendada.

Para furar a janela uma vez: `--ignorar-janela`.

```powershell
python scripts\operacao.py janela     # "agora dentro? sim/nao" e a proxima abertura
```

### 4.4 A pausa — a única trava que é decisão sua

```powershell
python scripts\cerebro.py pausar
```

O comando **espera** o worker fechar o item em andamento e só então responde.
Quando aparecer `SEGURO`, o banco está gravado e você pode desligar o
computador.

Se a pausa chega **no meio de um download** — inclusive durante a espera de
um `429` com `Retry-After` de até 120 s —, o download é interrompido em
~1 s (`M-14`): o temporário é apagado, o item volta **exatamente ao estado
de antes**, sem gastar tentativa, com evento `download_interrompido`. Antes,
uma tempestade de 429 segurava a pausa por até ~6 min e o `pausar` desistia
de esperar.

A pausa é **persistente**: sobrevive a reinicialização. Enquanto houver
`PAUSE` pendente, todo `processar` sai com `4` sem fazer nada — inclusive o
da tarefa agendada. Para voltar: `python scripts\cerebro.py retomar`.

---

## 5. Quando dá problema

### Caiu a luz / o computador desligou sozinho

**Não faça nada.** Se a tarefa agendada está ativa, em até 30 minutos ela
sobe o worker de novo. Se quiser adiantar:

```powershell
python scripts\cerebro.py processar
```

Na subida ele: pega o lock (livre, porque o dono morreu) → encontra os itens
presos em `HYDRATING`/`EXTRACTING` → devolve todos para a fila com evento
`retomado_apos_queda` → apaga os temporários que ficaram em `tmp\` →
continua. Nada é reprocessado sem necessidade: o que já estava `COMPLETED`
continua `COMPLETED` (REGRA 8: cada item é uma transação).

Exceção — o item que **já derrubou o worker antes** (próxima seção): na
2ª queda com ele no meio, ele não volta para a fila; vai para `ERROR_FINAL`.

### Um arquivo que derruba o worker (pílula envenenada, `B3`)

Há arquivos que não dão erro: **matam o processo** — crash nativo do
PyMuPDF, falta de memória, ou a janela fechada com a extração travada. O
item fica em `HYDRATING`/`EXTRACTING`, e na subida a retomada o devolve para a
fila. Só que ele é, pela ordem da fila, o **primeiro** a ser pego de novo: sem
limite, derrubava o worker a cada disparo do Agendador e a fila nunca mais
andava.

Agora a retomada conta as quedas de cada item — os eventos
`retomado_apos_queda` desde o último `processado`/`coberto_por_conteudo`
(`Worker.quedas_do_item()`; sem coluna nova). Na `worker.max_quedas_por_item`-ésima
queda (padrão **2**) o item vai para `ERROR_FINAL` com
`ultimo_erro = "derrubou o worker N vezes (a ultima com o item em …)"`,
evento `quarentena_apos_queda`, e a sessão segue com o próximo. No
`cerebro.py erros` ele aparece na categoria **`derrubou o worker`**.

Uma queda de luz isolada não isola ninguém (é 1 queda; o contador zera
quando o item é processado). Duas quedas de luz no **mesmo** item são raras;
se acontecer, ou depois de investigar o arquivo, para dar outra chance:

```sql
UPDATE arquivo SET estado='QUEUED', tentativas=0, ultimo_erro=NULL,
       proxima_tentativa_em=NULL WHERE id = <id>;
```

O isolamento também zera o contador: o item ganha de novo as N quedas.

### A credencial expirou (`B4`)

O segredo do aplicativo (`GRAPH_CLIENT_SECRET`) tem validade. Expirado ou
revogado, **todo** download responde `401` (ou o login do Entra ID recusa,
`falha_autenticacao`). Tratado como erro do documento, cada item gastava uma
tentativa até `ERROR_FINAL` — e o worker varreria os ~62 mil para
`ERROR_FINAL` numa noite.

O disjuntor (`Worker._falha_credencial`, `scripts/worker.py`):

- falha de credencial **não gasta tentativa do documento**: o item volta ao
  estado de antes, com `ultimo_erro`, uma espera própria
  (`espera_credencial_item_s` × n², para o próximo da fila ser tentado) e o
  evento `falha_credencial`;
- depois de `worker.falhas_credencial_para_parar` falhas **seguidas**
  (padrão **3**) a sessão encerra em **`AGUARDANDO_CREDENCIAL`**, código de
  saída **`6`**, com o motivo no `controle` e no checkpoint. O
  `cerebro.py status` mostra o estado, o motivo e a ação;
- qualquer resposta autenticada do Graph (bytes ou `404`) zera a sequência.

**`401` × `403`.** `401` (e login recusado) é sempre do aplicativo: token não
é por arquivo. `403` é ambíguo — o Graph devolve o mesmo `accessDenied` para
"o app perdeu acesso ao site" e para "este item tem permissão própria", e a
resposta não permite separar os dois com segurança. A separação é pelo
contexto: se a credencial **já funcionou nesta sessão** e não há outra falha
de credencial em sequência, o `403` é tratado como **do item** — gasta
tentativa dele, como erro de documento (termina `ERROR_FINAL` na 4ª, na
categoria `nuvem: autenticacao`) e o worker **não** para. Antes de qualquer
prova (começo da sessão) ou em sequência, é tratado como do aplicativo.
Custo no pior caso: uma revogação no meio da sessão gasta 1 tentativa de 1
item antes de o disjuntor abrir.

O que fazer: renovar o segredo no portal do Entra ID, gravar no `.env`
(`GRAPH_CLIENT_SECRET`) e conferir com `python tests\test_graph_ro.py`. Não é
preciso `retomar` — `AGUARDANDO_CREDENCIAL` não é pausa sua; o próximo
disparo da tarefa (ou `cerebro.py processar`) segue de onde parou.

### Sumiu a rede

O worker aguenta sozinho. Falha de rede não gasta as tentativas do documento:
o item volta para a fila como `WAITING_HYDRATION`, com backoff crescente.
A rede tem **contador próprio** (`M-6`): o número de eventos `falha` com
`detalhe.rede = 1` desde o último processamento do item
(`Worker.falhas_de_rede()`), e `arquivo.tentativas` fica intocado. Antes o
contador era um só, e um item com 3 quedas de rede chegava ao primeiro erro
real com `tentativas=4` e ia direto para `ERROR_FINAL`. O orçamento da rede
é `max_tentativas × 3` (12); esgotado, cada nova falha passa a contar para o
documento — nenhum item fica esperando a nuvem para sempre.
Depois de 25 falhas seguidas (`worker.falhas_seguidas_para_pausar`) ele
encerra em `AGUARDANDO_REDE` — limpo, com tudo gravado. A tarefa agendada
tenta de novo depois.

Para ver o tamanho do estrago:

```powershell
python scripts\cerebro.py relatorio --erros
```

Se o grupo dominante for **`nuvem: indisponivel`**, é rede/Graph: não há nada
a corrigir, só esperar. Se for **`nuvem: item nao existe`**, o acervo mudou —
é caso de rodar o delta (`scripts\12_scan_graph.py`), não de mexer no worker.

### O disco encheu

O worker para sozinho em `PAUSA_SEGURA_DISCO` antes de ficar sem espaço.
Para liberar, na ordem:

1. `python scripts\cerebro.py status` → veja `derivados` e `espaco livre`;
2. esvazie `tmp\` (só tem download descartável):
   `Remove-Item "tmp\e7_*" -Force`;
3. apague backups antigos em `data\backup\` e `backups\`;
4. se os **derivados** é que cresceram, aumente `worker.derivados_max_gb` —
   mas antes veja se compensa: eles são o produto do trabalho.

Depois: `python scripts\cerebro.py processar`. Não precisa de `retomar` —
`PAUSA_SEGURA_DISCO` não é pausa pedida por você.

### Como saber se está travado

| O que `status` mostra | Diagnóstico | Ação |
|---|---|---|
| lock **TOMADO** + heartbeat de minutos atrás + item atual | trabalhando normalmente | nenhuma |
| lock **TOMADO** + heartbeat parado há horas | travado de verdade (extrator preso num arquivo) — download pendurado **não** chega a isso: tem prazo duro (abaixo) | `cerebro.py pausar`; se não responder em 3 min, feche a janela do worker — o item volta para a fila inteiro |
| lock **livre** + estado `RODANDO` | sessão morreu sem encerrar | nenhuma: o próximo `processar` assume |
| lock **livre** + estado `PAUSADO` + comando `PAUSE` | você pausou | `cerebro.py retomar` |
| lock **livre** + estado `FILA_VAZIA` | acabou o trabalho | nenhuma |
| lock **livre** + estado `AGUARDANDO_CREDENCIAL` | o Graph recusou a credencial do app | renovar o segredo (seção *A credencial expirou*) |
| `processar` sai `3` na hora | outro worker está rodando | nenhuma |

O heartbeat também anda **durante o download** (`R9`): a mesma pergunta
"pediram para parar?" que o `graph_ro.baixar` faz ~1×/s bate o heartbeat, no
máximo a cada `worker.heartbeat_s` (20 s), sem mexer no item atual. Um
download de 300 MB não deixa mais o heartbeat parado por minutos. Durante a
**extração** ele continua parado (a chamada ao extrator é bloqueante; bater
o heartbeat dali exigiria uma thread com conexão própria ao banco) — então
heartbeat parado há muitos minutos com estado `EXTRACTING` no item atual é
extrator preso, não download.

Um arquivo problemático **nunca** trava a fila nem derruba o worker
(REGRA 6): vira `ERROR_RETRYABLE` → `ERROR_FINAL`, com a mensagem, e o worker
segue.

### Download pendurado: o prazo duro por item (`T-18`)

O `timeout` de socket vale **por operação**: um servidor que pinga um byte
por minuto nunca estoura, e as esperas de `429` somavam por cima. Por isso
cada download tem dois limites (`scripts/graph_ro.py`, classe `Prazo`):

| Chave (`worker:`) | Padrão | O que acontece ao estourar |
|---|---:|---|
| `timeout_download_s` | 900 | prazo **total** do download, tentativas e esperas somadas. Vira `ERROR_RETRYABLE` gastando 1 tentativa do documento; na 4ª (`max_tentativas`), `ERROR_FINAL`. Mensagem `0 tempo_esgotado: download passou de 900s` → categoria **`rede: tempo esgotado`** no relatório. |
| `timeout_leitura_s` | 120 | máximo **sem receber nenhum byte**. É rede: `WAITING_HYDRATION` (espera a nuvem), rotulado `sem_rede` — antes aparecia como "erro de disco". |

O pior caso de um item fica limitado a ~`timeout_download_s +
timeout_leitura_s`, e o temporário parcial é apagado no `finally` da
`FonteGraph` em qualquer saída (erro, prazo, pausa, exceção, Ctrl+C).

Por que o prazo gasta tentativa e a queda de rede não: um download que
pendura **sempre** no mesmo arquivo é problema daquele item; tratá-lo como
espera de rede custaria até 12 × 15 min por arquivo.

### Uma chamada ao Graph por download (`M-13`)

`graph_ro.baixar()` pede direto `GET /drives/{d}/items/{i}/content`, que
responde `302` para a URL pré-autenticada do CDN. O token vai como cabeçalho
**não redirecionável**: chega ao Graph e não segue o `302` (a credencial não
vaza para o CDN). Antes eram duas chamadas por item (`?$select=…downloadUrl`
e depois o conteúdo) — ~122 mil em vez de ~61 mil para a fila, o dobro de
quota e de chance de `429`. Se o `/content` responder `401`/`403`, o
download cai uma vez no caminho antigo (metadados + `downloadUrl`). Tamanho
e `quickXorHash` continuam conferidos contra o catálogo pela `FonteGraph`.

> Validado offline contra um servidor HTTP local (`test_worker_nuvem`
> 23b–23d). **Falta validar no Graph real**: `python tests\test_graph_ro.py`
> e `python tests\test_quickxor.py` (suítes `[rede]`) exercitam
> `baixar()` de verdade e devem passar antes de soltar o worker.

---

## 6. Concorrência: por que 1, e o que falta para mudar

O parâmetro `--concorrencia` existe, o padrão é **1**, e qualquer valor maior
é **recusado de propósito** (código `2`, com o motivo na tela).

Não é limitação de código. É a regra do projeto: *nada de arquitetura
sofisticada sem medição que justifique*.

1. **Não há benchmark.** A única medição real (amostra E7, 39 arquivos) é de
   um worker só: ~23 itens/min, ~1,1 MB/s. Não se sabe se o gargalo é a
   banda, o Graph ou o extrator. Se for a banda, dois workers entregam o
   mesmo total e dobram a chance de erro.
2. **O Graph tem *rate limit* por aplicativo**, não por processo. Paralelizar
   aumenta o `429 activityLimitReached`, e o backoff devolve o ganho.
3. **SQLite em WAL aceita muitos leitores, mas um escritor.** Cada item é uma
   transação de escrita (REGRA 8). Dois workers disputariam o `busy_timeout`
   no ponto mais caro do ciclo.
4. **O lock é exclusivo por construção.** Concorrência de verdade exigiria
   particionar a fila (por `prioridade`, ou por `id % N`) e um lock por
   partição. Isso é desenho novo, não um parâmetro.

**O benchmark que decide** (quando alguém quiser fazer): rodar 500 itens com
1 worker e 500 com 2, medindo `itens/min`, `MB/s`, taxa de `429` e ocorrências
de `database is locked`. Se o ganho for menor que ~40%, não compensa a
complexidade.

Até lá, a resposta honesta é 1 — e, com dedupe, a fila de 61.337 deve levar
**da ordem de um a dois dias** de worker ligado.

---

## 7. Referência rápida das chaves novas

Todas opcionais, todas lidas com padrão. **`config\projeto.yaml` não precisa
mudar** para nada disto funcionar.

```yaml
worker:
  max_bytes_sessao_gb: 0        # teto de download por sessao (0 = sem teto)
  max_itens_sessao: 0           # teto de itens por sessao   (0 = sem teto)
  lock_arquivo: ""              # caminho do lock (padrao: <banco>.lock)
  concorrencia: 1               # ver secao 6 — hoje so 1
  timeout_download_s: 900       # prazo duro TOTAL de um download (T-18)
  timeout_leitura_s: 120        # maximo sem receber byte nenhum
  max_quedas_por_item: 2        # B3: na N-esima queda com o item no meio -> ERROR_FINAL
  falhas_credencial_para_parar: 3   # B4: seguidas -> AGUARDANDO_CREDENCIAL (codigo 6)
  espera_credencial_item_s: 300 # B4: espera do item que tomou 401/403 (x n^2, teto 6 h)
  heartbeat_s: 20               # tambem durante o download (R9)
  janela:
    ativa: false
    inicio: "19:00"
    fim: "07:00"
    dias: []                    # 0=segunda ... 6=domingo; [] = todos
```

---

## 8. Estados do worker

Ficam em `controle.estado` e em todo `checkpoint`. Todos, menos `RODANDO`,
são **seguros para desligar o computador**.

| Estado | Significa |
|---|---|
| `RODANDO` | trabalhando agora |
| `PAUSADO` | você pediu (`pausar` ou Ctrl+C) — **persistente** |
| `PARADO` | você pediu (`parar`) |
| `FILA_VAZIA` | acabou o que havia para processar |
| `PAUSA_SEGURA_DISCO` | piso de disco livre ou teto de derivados |
| `AGUARDANDO_REDE` | falhas seguidas de nuvem/rede |
| `AGUARDANDO_CREDENCIAL` | o Graph recusou a credencial do app N vezes seguidas — **pede ação sua** (código `6`) |
| `LIMITE_BANDA` | teto de download da sessão |
| `LIMITE_SESSAO` | teto de itens da sessão |
| `FORA_DA_JANELA` | fora do horário permitido |

Os estados de cada **arquivo** (`QUEUED`, `COMPLETED`, `MISSING`…) são outra
coisa e estão em [`05_OPERACAO.md`](05_OPERACAO.md) e em `sql/schema_v2.sql`.

---

## 9. Testes

```powershell
python tests\test_operacao.py      # 118 PASS — lock, orcamento, janela, queda, ETA
python tests\test_worker.py        #  34 PASS
python tests\test_worker_nuvem.py  # 178 PASS
python tests\test_worker_robustez.py  # 58 PASS — pilula envenenada, disjuntor de
                                      #   credencial, quickXor sempre, heartbeat no download
```

O `test_worker_robustez` usa o laço `rodar()` e o `executar()` de produção
com o Graph falso: um item órfão duas vezes vira `ERROR_FINAL` e o seguinte é
processado; `401` em série deixa as tentativas intactas, para em
`AGUARDANDO_CREDENCIAL` com código `6` e aparece no `cerebro.py status`; `403`
isolado gasta só do item; item sem hash no catálogo sai com `quick_xor`,
`conteudo_id` e na busca v5. Rodado contra o código anterior, falha em 33
verificações.

O `test_worker_nuvem` também sobe um servidor HTTP em `127.0.0.1` que
responde como o Graph e o CDN, e aponta para ele o `GraphRO` **de produção**:
prova o prazo duro por item (download que pinga devagar, servidor mudo), a
pausa que interrompe a espera de `429`, a chamada única ao `/content` sem
token no redirect, o teto de extração conferido antes do download e o
contador de rede separado das tentativas do documento.

O `test_operacao` prova, com processos de verdade e banco de mentira: dois
workers simultâneos são impossíveis; o lock de um processo morto é recuperado;
um `pid` vivo mas alheio não trava a fila; a parada graciosa conclui o item
atual e grava; a retomada devolve os órfãos; os tetos encerram limpo; a janela
é respeitada; e o ETA sai do banco com números conferidos na mão.
