# Manual de operação — rodar o cérebro sozinho

Tudo aqui funciona **sem nenhuma sessão de IA aberta**. É Python e SQLite.

> **Para deixar rodando por dias, sem ninguém olhando**, o manual é
> [`10_OPERACAO_AUTONOMA.md`](10_OPERACAO_AUTONOMA.md): o comando único
> (`scripts\cerebro.py`), o lock de processo, os tetos de banda e disco, a
> janela de execução, as métricas com ETA e a tarefa do Windows. Este
> documento aqui continua valendo para o ciclo manual, passo a passo.

```bash
cd "C:\caminho\exemplo\cerebro-documental"
```

---

## O ciclo completo

| # | Comando | O que faz | Quanto demora |
|---|---|---|---|
| 1 | `python scripts\00_init_db.py` | cria o banco (idempotente) | segundos |
| 2 | `python scripts\03_migrar_v3.py` | leva o banco ao schema da nuvem, com backup | segundos |
| 3 | `python scripts\11_mapear_drives.py` | descobre as bibliotecas e o mapa de caminhos | ~1 min |
| 4 | `python scripts\12_scan_graph.py` | **inventaria pela nuvem** (delta), sem baixar arquivo | ~7 min |
| 5 | `python scripts\13_reconciliar.py` | simula a ligação com o banco e relata | ~1 min |
| 6 | `python scripts\13_reconciliar.py --aplicar` | aplica, com backup antes | ~1 min |
| 7 | `python scripts\02_classificar.py` | descobre P0..P4 pela árvore e monta a fila | segundos |
| 8 | `python scripts\worker.py run` | **processa até acabar** — pode ficar dias | o tempo que precisar |
| 9 | `python scripts\99_status.py` | onde tudo está | segundos |

No dia a dia o ciclo é **4 → 5/6 → 7 → 8**: o delta traz só o que mudou, e o
passo 8 pode ficar rodando o tempo todo.

> `01_scan_inventario.py` (a varredura do disco sincronizado) **saiu do fluxo**:
> a fonte é a nuvem (`DEC-012`). O script continua no repositório porque os
> testes de classificação e do worker rodam sobre árvores locais de mentira.

---

## Deixar processando a noite toda

```bash
python scripts\worker.py run
```

Deixe a janela aberta. Ele mostra o progresso de tempos em tempos e para
sozinho quando a fila acaba (`FILA_VAZIA`), quando o disco aperta
(`PAUSA_SEGURA_DISCO`) ou quando você mandar parar.

Variações úteis:

```bash
python scripts\worker.py run --prioridade-max 1    # só P0 e P1, para o cérebro ficar útil antes
python scripts\worker.py run --sem-hidratar        # só o que já está no disco, não baixa nada
python scripts\worker.py run --limite 200          # uma rodada curta, para acompanhar
python scripts\worker.py run --max-gb-sessao 5     # para depois de 5 GB baixados
```

> `--limite` encerra a sessão no estado `LIMITE_SESSAO` e `--max-gb-sessao`
> em `LIMITE_BANDA` — os dois são encerramentos limpos (código 0), com tudo
> gravado. Ver [`10_OPERACAO_AUTONOMA.md`](10_OPERACAO_AUTONOMA.md).

---

## Pausar com segurança

```bash
python scripts\control.py pause
```

O comando **espera** o worker terminar o item em andamento e só então responde.
Quando aparecer `SEGURO`, o banco está gravado, o checkpoint registrado, e você
pode fechar o processo e desligar o computador.

```bash
python scripts\control.py status     # onde parou, o que falta, espaço livre
python scripts\control.py resume     # libera a fila
python scripts\worker.py run         # volta a processar de onde parou
```

> A pausa **não se desfaz sozinha**. Se você pausar e o Windows reiniciar, o
> worker sobe e recusa rodar até você dar `resume`. Isso é de propósito.

`Ctrl+C` na janela do worker faz a mesma coisa: ele termina o item atual, grava
e sai. Não corrompe nada.

---

## Se o computador desligar sem aviso

Não tem cerimônia: rode `python scripts\worker.py run` de novo.

Na subida ele:
1. encontra os itens que ficaram presos em `HYDRATING` / `EXTRACTING`;
2. devolve todos para a fila (com evento `retomado_apos_queda` no histórico);
3. continua da fila, pelo mesmo critério de prioridade.

Nada é reprocessado sem necessidade: cada item já concluído está em
`COMPLETED` e não volta para a fila a menos que o arquivo mude no disco.

---

## Os estados (é assim que se responde "quanto falta?")

```bash
python scripts\99_status.py
```

| Estado | Significado |
|---|---|
| `INVENTORIED` | metadados capturados, ainda não classificado |
| `QUEUED` | na fila |
| `WAITING_HYDRATION` | precisa vir da nuvem e agora não dá (rede, orçamento, disco) |
| `HYDRATING` / `EXTRACTING` | em andamento neste instante |
| `COMPLETED` | texto extraído, derivados gravados e indexados |
| `PARTIAL` | extraiu parte (páginas ilegíveis) **ou** foi adiado pelo teto de extração sem ser baixado — ver abaixo |
| `OCR_REQUIRED` | é escaneado; precisa de OCR (fila própria, ainda não implementada) |
| `UNSUPPORTED` | ainda não há extrator para o formato — **continua no catálogo** |
| `DUPLICATE` | conteúdo idêntico (mesmo sha256) a outro já processado |
| `SKIPPED` | temporário (`~$`, `.tmp`) |
| `CHANGED` | mudou no disco, vai ser reprocessado |
| `MISSING` | sumiu do disco (a linha fica, com histórico) |
| `ERROR_RETRYABLE` | falhou, tem nova tentativa marcada |
| `ERROR_FINAL` | falhou o número máximo de vezes; espera revisão humana |

Falta = `QUEUED + CHANGED + WAITING_HYDRATION + ERROR_RETRYABLE` **que o
worker consegue pegar**. Com a fonte na nuvem (o padrão, `DEC-012`) isso é a
`v_fila_nuvem` (`DEC-030`), e é ela que `99_status`, o `checkpoint.pendentes`
do worker e a previsão de horas usam (`T-12`). A `v_fila` conta também quem
não tem identidade ou presença na nuvem — hoje, os 592 de
`Coordenacao Programa - 02 Repositorio`, fora do escopo pela `DEC-017` — e o
`99_status` mostra essa diferença numa linha própria, `fora do alcance`, em
vez de somá-la à fila. (`control.py status` ainda lê a `v_fila`.)

### Itens adiados pelo teto de extração (`T-10`)

`worker.tamanho_max_extracao_mb` (300 MB) é conferido **antes** do download,
pelo tamanho que o inventário já conhece. Item acima do teto **não é baixado**:
vira `PARTIAL` na hora, numa transação só, pelo mesmo caminho de gravação de
qualquer extração (linha em `extracao`, perfil JSON, `conteudo`, estado e
evento). Não existe estado novo — o que o distingue é a marca:

| Onde | Marca |
|---|---|
| `extracao.versao` | `adiado/teto_tamanho` |
| `extracao.extrator` | a categoria (`pdf`, `xlsx`…) — a retomada faz *upsert* na mesma linha |
| `extracao.erro` / `arquivo.ultimo_erro` | começa com `ADIADO_TETO_TAMANHO:` |
| `conteudo.versao_extrator` | `adiado/teto_tamanho` (estado `PARCIAL`) |
| `evento_arquivo` | `processado` com `"adiado": "teto_tamanho"` e `"baixado": false` |

O `quick_xor` do inventário é gravado em `conteudo` como seria depois do
download, então **as cópias do mesmo arquivo grande viram `DUPLICATE` sem
download** (passo 1 do worker). Item sem `quick_xor` é adiado do mesmo jeito,
só que sem grupo de conteúdo.

Quando existir extrator de arquivo grande, os itens a retomar são estes
(só os representantes têm linha em `extracao`; as cópias continuam cobertas
pelo `conteudo`):

```sql
SELECT a.id, a.caminho_rel, a.tamanho_bytes, a.categoria
  FROM arquivo a JOIN extracao e ON e.arquivo_id = a.id
 WHERE a.estado = 'PARTIAL' AND e.versao = 'adiado/teto_tamanho';
```

Para retomar: subir o teto (ou ligar o extrator novo) e devolver esses ids
para `QUEUED`. O representante reprocessado reextrai e atualiza o `conteudo`;
as cópias passam a ver o texto novo sem reprocessar.

---

## Hidratação e disco — as três travas

Estão em `config/projeto.yaml`, seção `nuvem`:

| Chave | Hoje | O que faz |
|---|---|---|
| `permitir_hidratacao` | `true` | se `false`, o worker só processa o que já está local |
| `max_hidratacao_gb` | `15` | orçamento de download **por sessão** do worker |
| `espaco_livre_minimo_gb` | `25` | abaixo disso o worker entra em `PAUSA_SEGURA_DISCO` |
| `tamanho_max_arquivo_gb` | `2` | arquivo maior que isso não é baixado |
| `liberar_apos_processar` | `true` | devolve o arquivo para "somente na nuvem" depois de extrair |
| `liberar_somente_o_que_hidratamos` | `true` | o que já era local **continua** local |

O `worker.derivados_max_gb` (60 GB) é uma quarta trava: se os textos extraídos
crescerem demais, o worker pausa e avisa.

**O que "liberar espaço" faz:** marca o atributo `UNPINNED` — exatamente o que o
botão *Liberar espaço* do Explorer faz. Não apaga, não move, não renomeia, não
escreve conteúdo. Validado em arquivos reais:

```bash
python tests\test_hidratacao.py --confirmar
```

→ 22 PASS / 0 FAIL, com sha256 idêntico antes e depois do ciclo.

---

## Buscar no que já foi processado

```bash
python scripts\control.py texto 1234        # o texto extraído do arquivo 1234
```

```sql
-- sqlite3 data\cerebro.db
SELECT a.caminho_rel, f.pagina,
       snippet(texto_fts, 4, '[', ']', '…', 12) AS trecho
FROM texto_fts f JOIN arquivo a ON a.id = f.arquivo_id
WHERE texto_fts MATCH 'notificacao NEAR/8 prazo'
ORDER BY bm25(texto_fts) LIMIT 20;
```

---

## Ligar sozinho depois de reiniciar o Windows (quando você quiser)

Ainda **não** está ativado — primeiro valide na mão. Quando quiser:

```powershell
$acao   = New-ScheduledTaskAction -Execute "python" `
          -Argument "scripts\worker.py run" `
          -WorkingDirectory "C:\caminho\exemplo\cerebro-documental"
$gatilho = New-ScheduledTaskTrigger -AtLogOn
$config = New-ScheduledTaskSettingsSet -StartWhenAvailable `
          -DontStopOnIdleEnd -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "CerebroDocumental" -Action $acao `
          -Trigger $gatilho -Settings $config
```

Como a pausa é persistente, se você tiver pausado antes de desligar, a tarefa
sobe e não processa nada até você dar `resume`. É o comportamento desejado.

---

## Quando chamar o Claude

Python faz o trabalho repetitivo — não gasta token nenhum enquanto processa.
O Claude entra para:

- interpretar `ERROR_FINAL` que se repete e corrigir o extrator;
- ajustar `config/classificacao.yaml` quando a árvore enganar a heurística;
- implementar extrator novo (OCR, `.msg`, `.zip`, CAD);
- decidir arquitetura;
- construir a camada de busca/IA em cima do índice.
