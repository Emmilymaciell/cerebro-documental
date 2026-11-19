# STATE — Estado do projeto

**Atualizado em:** 2026-10-11 (só infraestrutura; o estado do trabalho abaixo é o de 2026-10-04)

> 🔧 **2026-10-11 — infraestrutura (Emmily AI Project OS).** Remoto GitHub **privado**
> (`cerebro-documental`), backup do repositório e do `cerebro.db` no Google Drive, conferidos
> por hash. Entrou o `requirements.txt` (python-docx, PyMuPDF, openpyxl, python-pptx, PyYAML,
> com as versões do Python 3.12 desta máquina): é o que um computador novo instala.
**Fase atual:** **código pronto para a massificação; banco real ainda não.**
O sprint de convergência fechou no código `T-09`–`T-18`, a busca v5, os
extratores E9 e a robustez do worker sem supervisão. As **correções de dados
não foram aplicadas no banco real** (a escrita em produção foi bloqueada pela
política de permissões da sessão): é o próximo passo, e é da Emmily.
Veredito de prontidão: **NO-GO** até G5–G11 de
[`docs/13_PRONTIDAO_MASSIFICACAO.md`](docs/13_PRONTIDAO_MASSIFICACAO.md).

| | |
|---|---|
| **Git** | `main` · árvore limpa · histórico da sessão em `state/sessoes/002-sprint-convergencia.md` |
| **Testes** | offline **1.671 PASS / 0 FAIL** (15 suítes) · rede `graph_ro` 57 + `quickxor` 9 = **66 PASS / 0 FAIL** · `reproducoes_qa` 0 defeitos — executados em 2026-10-04 |
| **Banco** | schema **v4** (sem v5, sem backfill) · 142.664 linhas · 135.259 presentes · 116 `COMPLETED` · 7.405 `MISSING` · 61 `CHANGED` · fila `v_fila_nuvem` 62.742 (226,5 GB) · `integrity_check` ok · invariantes: 41/82/56/1/2 violações conhecidas (corrigidas pelo `21`) |
| **Worker** | `PAUSADO` (comando `PAUSE`) · lock livre · `tmp/` sem `e7_*` |
| **Backup** | `data/backup/cerebro_pre_sprint002_20261004_105618.db` (958 MB, `quick_check` ok) — antes de qualquer aplicação |
| **Disco** | 58,4 GB livres; `data/backup/` ~7,5 GB em 10 cópias (`T-19`) |
| **Abertos** | `T-08` (prazo ~22/12), `T-19`, `T-20`, `T-21` (aplicar no banco), `T-22`–`T-27` |

> **Retomando?** Rode `/session-start`. Ele reconstrói tudo isto pelo
> repositório e pelo banco, e para antes de implementar. O mapa de onde cada
> coisa mora está em [`docs/12_CONTINUIDADE.md`](docs/12_CONTINUIDADE.md).

---

## Fundamentos — o que vale a partir do checkpoint E0–E6

Dez afirmações que governam tudo daqui para a frente. Mudar qualquer uma delas
exige decisão explícita e nova entrada na tabela de decisões.

1. **O Microsoft Graph é a fonte única de verdade.** A pasta sincronizada local
   não é fonte nem cache (`DEC-012`).
2. **`drive_id + item_id` é a identidade canônica do item** (`DEC-015`).
3. **Caminho e nome são atributos mutáveis** — nunca chave. Renomear ou mover
   não cria item novo.
4. **`delta` é o mecanismo de sincronização incremental** (`DEC-021`): checkpoint
   por página, `delta_link` só quando a rodada termina.
5. **`quickXorHash` + tamanho é evidência `FORTE`, não confirmação
   criptográfica** (`DEC-025`). "Confirmada" exige hash criptográfico sobre o
   conteúdo — e só antes de ação irreversível, que não é deste projeto.
6. **Itens duplicados continuam existindo individualmente no inventário**
   (`DEC-027`): cada um com seu `drive_id`, `item_id`, caminho, datas, autoria
   e prioridade. Um extrai; os outros referenciam — nenhum desaparece.
7. **Este projeto não realiza exclusões no SharePoint.** Nem exclusão, nem
   movimentação, nem renomeação. A porta é somente leitura por construção
   (`DEC-014`).
8. **`Coordenacao Programa - 02 Repositorio` está fora do escopo** (`DEC-017`) —
   outro site, fora do alcance da credencial. Fora do escopo **não é** ausente.
9. **Arquivo ausente permanece registrado como `MISSING`** (`DEC-029`), com
   `presente=0`, histórico intacto e caminho de volta se reaparecer.
10. **O worker está PAUSADO neste checkpoint.** Nada de processamento massivo
    até o E7 ser validado numa amostra pequena.

---

## Migração para o Microsoft Graph (em andamento)

### Concluído

| Etapa | Entregável | Teste |
|---|---|---|
| **E0** | `.env` próprio + `.gitignore` protegendo credencial | `git check-ignore .env` confirma |
| **E1** | `scripts/graph_ro.py` — cliente **somente leitura** | `tests/test_graph_ro.py` → **25 PASS** |
| **E2** | `scripts/11_mapear_drives.py` + `config/mapa_drives.yaml` | **147/149 caminhos resolvidos (98,7%)** |
| **E3** | `scripts/quickxor.py` — o mesmo hash que o SharePoint calcula | `tests/test_quickxor.py` → **20/20 contra o servidor**, em 9 formatos |
| **E4** | `scripts/12_scan_graph.py` + schema v3 — inventário por delta | `tests/test_delta.py` → **46 PASS** (Graph falso, 11 cenários) |
| **E5** | `scripts/13_reconciliar.py` — migração por `drive_id+item_id` | `tests/test_reconciliacao.py` → **44 PASS** |

### O que o E2 respondeu

- **Os dois drives NÃO são o mesmo conteúdo.** `Programa` (6 pastas, o acervo) e
  `Documentos` (1 pasta, `relatorio`) são bibliotecas distintas. O `id` do
  item-raiz se repete entre elas neste tenant — comparar raiz **engana**; o que
  distingue é o `driveId` e o conteúdo do primeiro nível.
- **6 das 7 bibliotecas locais casam com a nuvem** pela regra "remover o
  prefixo `Programa - `".
- **`Coordenacao Programa - 02 Repositorio` (593 arquivos) não está neste site.**
  É outro site do SharePoint, sincronizado na mesma máquina. O `Sites.Selected`
  atual não alcança. Precisa de liberação da TI, ou fica fora do escopo.
- **Bug real encontrado e corrigido:** nome de arquivo com `%` (existe no
  acervo: `RD-103 PLANILHA 20%A.xlsx`) quebrava a URL (HTTP 400). O
  caminho agora é codificado segmento a segmento, com teste de regressão.
- Os 2 caminhos que não resolveram são divergência real entre disco e nuvem —
  exatamente o que a reconciliação (E5) tem de listar.

### O inventário da nuvem (E4) — 2026-09-20

| | |
|---|---|
| Itens no delta | 175.559 (160.008 únicos) |
| Arquivos · pastas | **137.856 · 22.149** na biblioteca Programa (+1 · 2 em "Documentos") |
| Volume | **1,166 TB** |
| Com `quickXorHash` do servidor | **137.820** (os 36 sem hash são de 0 byte) |
| Tempo · chamadas | **7,2 min** · 164 chamadas, **0 esperas por limite** |
| Erros | **0** |

### A reconciliação (E5) — 2026-09-20

| Resultado | Linhas | % |
|---|---:|---:|
| **ALTA** (caminho exato) | **137.408** | 98,5% |
| MÉDIA | 0 | — |
| AMBÍGUO | **0** | — |
| SEM CORRESPONDÊNCIA | 2.051 | 1,5% |
| Itens só na nuvem (linhas novas) | 449 | |

Os 2.051 sem correspondência estão detalhados na auditoria logo abaixo, que
os fechou um a um: **1.457** não foram achados na nuvem (destes, **1.198** na
árvore `RD103\...\DESAPROPRIAÇÃO\REV 01`, **excluída** — ver auditoria) e
**594** são da `Coordenacao`, fora do escopo (`DEC-017`).

**Preservado:** 94 `COMPLETED` · 94 extrações · 161 trechos · 60/60 derivados
em disco · P0..P4 recalculados sobre o acervo maior (P0 33.284 → **33.359**).
*(Depois da marcação de ausentes, os `COMPLETED` passaram a 93: um deles era o
PDF de 0 byte que sumiu da nuvem.)*

**Achado de graça:** `v_duplicata_nuvem` já mostra **15.703 grupos de
duplicatas exatas, 50.800 cópias excedentes, 617 GB redundantes** — calculado
com o hash do servidor, **sem baixar um byte**. Agir sobre isso é do projeto de
organização.

### Auditoria das exceções (2026-09-20) — `scripts/14_auditar_excecoes.py`

**Fechamento contábil** (cada número tem uma consulta que o reproduz):

```
item_nuvem            160.008 = 137.857 arquivos + 22.151 pastas (2 são raízes de drive)
arquivo               139.908 = 137.857 com item_id + 2.051 sem
reconciliação         139.459 decisões = 137.408 ALTA + 2.051 SEM_CORRESPONDÊNCIA
  2.051               =   594 fora do escopo (Coordenacao)
                        + 1.457 não achados na nuvem
  1.457               =   252 temporários ~$  +  1 já processado  +  1.204 demais
```

As duas diferenças que apareciam nos relatórios anteriores:

- **2.050 vs 2.051** — faltava contar **1 arquivo já processado** (`COMPLETED`)
  que sumiu da nuvem: um PDF de **0 byte** da REV 01, cuja extração é `vazio`
  e que portanto não tem derivado a perder.
- **137.856 vs 137.857** — 137.856 são da biblioteca `Programa` e **1** é da
  biblioteca `Documentos`. Idem para as pastas: 22.149 + 2 = 22.151.

**O destino da árvore `RD103\...\DESAPROPRIAÇÃO\REV 01 - 15 09 26` (1.198
arquivos):** foi **excluída**, não renomeada nem movida.

| Sinal | Resultado |
|---|---|
| A pasta-mãe, conferida ao vivo no Graph | existe, e hoje tem **só** `REV 00 - 17 08 26` |
| Código do documento da REV 01 (`DC.PES.06x_02`) em toda a nuvem | **0 ocorrências** (o da REV 00 tem 1.324) |
| Pasta com nome `REV 01*` de desapropriação em qualquer drive | nenhuma |
| Como o scanner local a viu em 17/09 | **1.198 placeholders do OneDrive** — ou seja, existiam na nuvem |
| No disco hoje | a pasta **não existe mais** |

Dos 1.198: **680** têm conteúdo equivalente (nome+tamanho) na REV 00, que
continua lá; **421** não têm equivalente nenhum; 66 têm nome igual com tamanho
diferente; 23 têm vários candidatos; 8 casam só pela posição relativa.

**Os 7 restantes fora da REV 01:** 6 são o arquivo interno de sincronização do
OneDrive (`.849C9593-…`, um por biblioteca, nunca esteve na nuvem) e **1 é um
documento real: `NC-050-1.pdf`**, na raiz de `G2 -  Engenharia`, que estava na
fila e não está mais na nuvem.

> **Limite declarado:** as linhas antigas não têm hash — calcular exigiria baixar
> o acervo. O cruzamento por `quickXorHash` só é possível **entre itens da
> nuvem**. Para o inventário antigo, o sinal mais forte disponível é
> nome + tamanho + posição relativa.

### E7 — CONCLUÍDO E COMMITADO EM `deaa61c` (2026-09-21)

> O worker passou a ler da nuvem. Validado numa amostra controlada de **39
> arquivos reais**. Tudo abaixo está no diretório de trabalho, **sem commit**.

**O que o E7 entregou:**

| Peça | O que é |
|---|---|
| `sql/schema_v4.sql` + `scripts/04_migrar_v4.py` | tabela **`conteudo`** (os bytes) separada da **ocorrência** (`arquivo`), + `arquivo.conteudo_id`, + views `v_ocorrencia`, `v_cobertura_conteudo`, `v_conteudo_orfao`, + `v_fila_nuvem` redefinida. Aplicado no banco real, sem perder nada. |
| `scripts/fonte.py` | `FonteGraph` (baixa para `tmp/`, valida tamanho **e** `quickXorHash`, apaga no `finally`) e `FonteLocalTeste` (**só teste**; nunca apaga original). |
| `scripts/worker.py` | `processar()` pela nuvem: fila → download → validação → extração → `conteudo` → apagar tmp. Dedupe em **dois pontos** (hash do inventário, antes de baixar; hash dos bytes, depois). `reeleger_representantes()` e `limpar_sobras()` na subida. `--fonte`, `--ids`, `--manter-tmp`. |
| `scripts/16_amostra_e7.py` | `selecionar` (amostra determinística + retrato do antes) e `relatar` (métricas antes/depois). |
| `tests/test_worker_nuvem.py` | **101 PASS** — Graph falso na camada do cliente HTTP; `FonteGraph` e `Worker` são os de produção. |
| `sql/consultas_busca.sql` | as três consultas que provam o modelo: 1 conteúdo → N lugares; busca textual → todos os lugares; contador × realidade. |

**Quatro bugs reais encontrados e corrigidos** (nenhum teste foi afrouxado para
passar):

1. **Fonte local não calculava hash** → a deduplicação nunca era exercitada nos
   testes. `FonteLocalTeste` agora calcula o `quickXorHash`.
2. **Representante virava duplicata de si mesmo** ao ser reprocessado. Guarda
   `ja["representante_id"] != arquivo_id` nos dois pontos de dedupe.
3. **Item preso em `WAITING_HYDRATION`** (`DEC-031`): uma queda de rede tirava o
   item da fila **para sempre** — o estado não estava na consulta do worker nem
   em `v_fila_nuvem`, e `restam_em_espera()` continuava contando, deixando o
   worker dormindo à espera de algo que nunca voltaria.
4. **Contador de ocorrências à deriva** (`DEC-032`): quando uma cópia mudava de
   bytes, o conteúdo antigo ficava com o contador inflado para sempre —
   `conteudo.ocorrencias` dizia 3 e `v_ocorrencia` mostrava 2. Agora os **dois
   lados** da troca são recontados.

Fora isso, `tests/test_graph_ro.py` tinha um fixture frágil (procurava arquivo
pequeno só nos filhos diretos do primeiro nível) que parou de achar quando o
acervo mudou. A busca virou em largura, com teto de 40 pastas.

### A prova real — 39 arquivos, 2026-09-21

Amostra determinística (`ORDER BY item_id` dentro de cada cota, sem `RANDOM()`),
registrada em `output/amostra_e7.md`, `.csv` e `_antes.json` **antes** de
qualquer download. Composição: pdf 18 · xlsx 10 · docx 5 · pptx 3 · texto 3;
P0 21 · P1 2 · P2 13 · P3 2 · P4 1; de 0 byte a 28,7 MB; 3 grupos de cópias
completos e 3 ocorrências únicas.

| | |
|---|---:|
| Selecionados | 39 |
| Extraídos (baixados) | **31** — 28 `COMPLETED`, 2 `PARTIAL`, 1 `OCR_REQUIRED` |
| Evitados por deduplicação | **2** (nenhum byte baixado) |
| Ausentes na nuvem (404) | **6** → `MISSING` |
| Bytes previstos / lidos / poupados | 126,3 MB / **109,4 MB** / 10,3 MB (dedupe) + 6,6 MB (404) |
| `quickXorHash` conferidos | 31 de 31 · **0 divergências** |
| Tamanho conferido | 31 de 31 · **0 divergências** |
| Tempo · velocidade | 1,7 min · ~23 itens/min · ~1,1 MB/s |
| Temporários criados / restantes | 31 / **0** |
| Linhas novas em `conteudo` | 30 (o arquivo de 0 byte não gera conteúdo — `DEC-011`) |
| `COMPLETED` no banco | 93 → **121** (os 93 antigos intactos) |
| `extracao` no banco | 94 → 125 |
| `MISSING` | 1.457 → 1.463 |

**Nenhum original no SharePoint foi alterado.** O caminho de escrita não existe:
`graph_ro` recusa todo método HTTP diferente de `GET` e `tests/test_graph_ro.py`
lê a AST do módulo para garantir isso (`DEC-014`); o worker só escreve em
`data/`, `derivados/`, `logs/` e `tmp/`, com `assert_escrita_permitida` em cada
gravação. A pasta sincronizada não foi lida nem uma vez.

### Descoberta da prova: `6. Arquivos Parceiro` saiu do drive

Os 6 erros não eram defeito. A biblioteca **`Programa - 6. Arquivos Parceiro`
(5.941 arquivos, 120 GB, 3.460 na fila) desapareceu do drive Programa** entre o
inventário de 2026-09-20 21:14 e hoje. Verificado ao vivo, só leitura:

- a raiz do drive tem **5 pastas**; o inventário conhece 6. As outras cinco
  (e o `relatorio` do drive `Documentos`) têm `item_id` **idêntico** ao vivo e
  no inventário — mesma unidade, inventário correto, exatamente uma pasta a
  menos. A que falta é `6. Arquivos Parceiro`,
  `item_id = 01EXEMPLO0000000000000000000000000`;
- os itens dão 404 **por `item_id`** — e `item_id` sobrevive a renomear e a
  mover dentro do drive, então não foi rename nem move interno;
- a pasta-mãe também dá 404 **por caminho**;
- 12 de 12 itens sorteados da biblioteca: 404.

Isso expôs a quarta correção: um 404 virava `ERROR_RETRYABLE` e, depois de
4 tentativas, `ERROR_FINAL` — rotular de "erro nosso" um item que simplesmente
saiu do acervo, e gastar 4 chamadas × 3.460 arquivos para chegar lá. Agora
404/410 vira **`MISSING` com `presente_nuvem=0`** e evento `ausente_na_nuvem`
guardando o estado anterior (`DEC-033`), exatamente como a `DEC-029`. Os 6 da
amostra foram reprocessados pelo código corrigido e ficaram `MISSING`.

**Pendência para a Emmily decidir (`T-06`):** os outros **3.454 arquivos da
mesma biblioteca continuam `QUEUED`**. Não os marquei — são 58% de uma
biblioteca inteira e a decisão é sua, não minha. As opções: rodar o delta (E8),
que fecha isso com autoridade; ou perguntar à TI se a pasta foi movida para
outro site.

### Validação final

```
offline  test_scanner 44 · test_worker 34 · test_delta 46 ·
         test_reconciliacao 49 · test_worker_nuvem 101        = 274 PASS / 0 FAIL
rede     test_graph_ro 26 · test_quickxor 9                   =  35 PASS / 0 FAIL
banco    integrity_check ok · quick_check ok · foreign_key_check sem violações
         schema v4 · 139.908 arquivos · conteúdo órfão 0 · contador × realidade 0 divergências
tmp/     0 temporários `e7_*`
worker   PAUSADO
```

**Impedimento para fechar o E7:** nenhum. Falta só a sua revisão e a decisão
sobre `T-06` e sobre os parâmetros do processamento em massa.

### E8 (parcial) — sincronização por delta e o veredito do `T-06` — commit `100a39e` (2026-09-22)

> Escopo desta rodada: **só o necessário para fechar o `T-06`**. Não houve
> processamento em massa, OCR, paralelismo nem loop contínuo. Worker `PAUSADO`
> o tempo todo.

**O Graph exigiu ressincronização.** O delta incremental morreu com
**HTTP 410 `resyncRequired`** na primeira página: o servidor havia descartado
nosso token. A mensagem dele é literal — *"Replace any local items with the
server's version (including deletes)"*. O `12_scan_graph.py` não sabia fazer
isso: registrava erro e parava, e insistir no incremental só repetiria o 410
para sempre.

**Três correções, com 19 checagens novas no `test_delta` (46 → 65):**

| `DEC-035` | **410 `resyncRequired` dispara varredura completa automática**, descartando o token morto. Não há como pedir "só o que mudou" a partir de um token que o servidor jogou fora. |
| `DEC-036` | **Depois de um resync completo, o que não voltou foi removido.** Um delta completo não devolve exclusões — devolve o que existe. A trava é o `execucao_id`: a varredura só roda quando a rodada **completa** começou e terminou na mesma execução. Rodada incremental, parcial ou retomada nunca chega lá, porque ali "não veio" quer dizer "não mudou", não "sumiu". |
| `DEC-037` | **Pasta removida leva os descendentes junto.** O delta devolve apenas a pasta apagada, não os milhares de filhos. Sem a cascata, o inventário afirmaria que existem arquivos dentro de um lugar que não existe mais. |

Também alinhei `03_migrar_v3.py` e `sql/schema_v3.sql`: a coluna
`caminho_anterior` e o índice composto `(quick_xor, tamanho_bytes)` existiam no
banco real mas faltavam na receita do repositório — mesmo tipo de deriva que a
`DEC-024` registrou.

### O resync — 9,1 min, 156 páginas, 171.162 itens

| | |
|---|---:|
| Itens recebidos | **171.162** |
| Novos | 1.575 |
| Alterados | 16 |
| Renomeados / movidos | 0 / 0 (os 5 do dia vieram na página incremental antes do 410) |
| Removidos | **6.757** |
| — por ausência no resync | 6.755 |
| — anunciados pelo Graph | 2 |
| — por cascata de pasta | 0 (a varredura já os tinha pego) |
| Resyncs exigidos | 1 |

### O veredito do `T-06`: o Parceiro foi excluída

Os 6.757 removidos se separam sem sobra: **6.755 são a árvore
`6. Arquivos Parceiro` inteira** — 814 pastas e 5.941 arquivos — e **2 são arquivos
avulsos** em `2. Compartilhado interno` que o Graph anunciou como apagados.
A pasta raiz ficou `presente=0` em 2026-09-22T20:09:07Z, com o evento
`removido` e o motivo `ausente no resync completo`.

A pasta `item_id 01EXEMPLO0000000000000000000000000` não voltou na varredura
completa. As outras cinco pastas de primeiro nível e o `relatorio` do drive
`Documentos` voltaram com `item_id` idêntico — mesma unidade, inventário
correto, falta exatamente uma pasta.

**Não houve realocação.** A pergunta "os itens reapareceram em outro lugar do
escopo?" tem resposta medida: dos 5.941 arquivos, 2.413 (40,6%) têm conteúdo
idêntico ainda vivo — mas **nenhum** desses gêmeos é um item novo desta rodada.
São cópias que já existiam antes, em `3. Desenvolvimento` e
`2. Compartilhado interno`. Não há identidade nova para reconciliar: o conteúdo
que sobreviveu já estava catalogado por conta própria. **3.528 arquivos não têm
nenhuma cópia viva no escopo acessível.**

### A propagação para o catálogo

`scripts/17_aplicar_delta.py` (novo, com `tests/test_aplicar_delta.py` — 34
checagens) leva o espelho para o catálogo pela identidade `(drive_id, item_id)`.
Simula por padrão; só grava com `--aplicar`, e confere que o total afetado é
exatamente o anunciado.

| | antes | depois |
|---|---:|---:|
| `arquivo` (linhas) | 139.908 | **139.908** |
| presentes | 138.445 | 132.508 |
| `MISSING` | 1.463 | **7.400** |
| `COMPLETED` | 121 | 116 |
| `CHANGED` | 0 | 28 |
| extrações | 125 | **125** |
| trechos indexados | 4.361 | **4.361** |
| conteúdos | 30 | **30** |
| fila da nuvem | 64.792 | 61.337 |

**5.937 viraram `MISSING`** (3.456 `QUEUED` + 2.473 `UNSUPPORTED` + 4
`COMPLETED` + 4 `DUPLICATE`), cada um com evento `ausente_na_nuvem` guardando
estado anterior, motivo, data e quem detectou. **39 recaminhados**: uma pasta
ancestral foi renomeada de `REV 04 - 28 08 26` para `REV 03 - 28 08 26`, e os
39 arquivos abaixo herdaram o caminho novo — `item_id` igual, estado intacto,
ninguém voltou para a fila por ter mudado de lugar. **28 viraram `CHANGED`**
por conteúdo alterado.

**Os 5 `COMPLETED` a menos, um a um:** 4 estavam dentro do Parceiro e viraram
`MISSING`; o quinto é o `[Parceiro] Draft - One Page.pptx` (#128398), que mudou de
conteúdo na nuvem e voltou para a fila como `CHANGED`. É o mesmo arquivo cujo
`mtime` divergente eu tinha sinalizado na prova do E7 — o delta confirmou que a
divergência era real. **Extrações, trechos, conteúdos, classificação e vínculos:
todos preservados.** Nenhuma linha apagada (REGRA 6). Conteúdo órfão: 0.
Contador × realidade: 0 divergências.

**2.023 arquivos novos no espelho não foram inseridos** no catálogo, de
propósito: inserir exige biblioteca, classificação e prioridade, que são do
`01_scan_inventario` e do `02_classificar`. Estão contados e à espera
(`T-07`).

### Validação

```
offline  scanner 44 · worker 34 · delta 65 · reconciliação 49 ·
         worker_nuvem 101 · aplicar_delta 34              = 327 PASS / 0 FAIL
rede     graph_ro 26 · quickxor 9                         =  35 PASS / 0 FAIL
banco    integrity_check ok · quick_check ok · foreign_key_check sem violações
tokens   os dois drives com `delta_link` novo, status `concluido`
tmp/     0 temporários `e7_*`
worker   PAUSADO
```

**Impedimento:** nenhum para a parte que foi feita. O E8 ainda não estava
completo aqui — faltavam o monitor recorrente e a entrada de arquivos novos
(`T-07`). Essa parte foi atacada na janela turbo de 2026-09-27, registrada
mais abaixo.

### Janela turbo — 2026-09-27 — cinco frentes em paralelo

> Cinco subagentes com fronteiras de arquivo disjuntas, integrados e
> verificados um a um pelo agente principal. **Nenhum documento do acervo foi
> processado. Nenhuma chamada de IA por documento. Worker `PAUSADO` o tempo
> todo.** Commits: `0cb7c30` · `821293a` · `017acb1` · `f090357` · `35d1eb1`.

| Frente | Entregou | Commit |
|---|---|---|
| **A** T-07 + sincronizador | `19_ingerir_novos.py`, `18_sincronizar.py`, `tests/test_ingestao.py` (77) | `f090357` |
| **B** extratores | `docs/08_EXTRATORES.md`, `tests/test_extratores.py` (251), 5 bugs corrigidos | `821293a` |
| **C** busca/FTS5 | `docs/09_BUSCA.md`, `sql/schema_v5_busca.sql` (proposto), `tests/test_busca.py` (124) | `0cb7c30` |
| **D** operação autônoma | `cerebro.py`, `operacao.py`, `tests/test_operacao.py` (118), `docs/10_OPERACAO_AUTONOMA.md` | `017acb1` |
| **E** QA | `docs/11_QA_AUDITORIA.md` — 2 CRÍTICOS, 8 ALTOS, 17 MÉDIOS, 9 BAIXOS | `35d1eb1` |

### Os três bugs latentes que teriam mordido em produção

1. **Banco novo nascia com `UNIQUE(caminho_rel)`** — regressão da `DEC-015`.
   Arquivo apagado e subido de novo no mesmo lugar (`item_id` novo, que é item
   novo) derrubaria a inserção. Já aconteceu 1 vez no acervo real. O banco de
   produção escapou só porque foi reconstruído por outra sessão (`DEC-024`) —
   ou seja, **as suítes rodavam contra um schema que não era o de produção**.
   Corrigido em `35d1eb1`, com 4 checagens de regressão.
2. **PID é reciclado.** Depois de uma queda de energia, o `pid` deixado por um
   worker morto pode pertencer a outro programa; `processo_vivo()` diria "vivo"
   e o worker **se recusaria a rodar para sempre**, sem ninguém rodando.
   Corrigido com lock de sistema operacional (`017acb1`).
3. **`.rtf/.odt/.ods/.odp` faziam 4 downloads e 4 tentativas por arquivo** até
   `ERROR_FINAL`, porque caíam nas categorias docx/xlsx/pptx. Corrigido
   (`821293a`).

### O que cada frente descobriu que muda o planejamento

**Extratores (B).** Os 70.364 arquivos sem extrator (852,9 GB) **devem
continuar `UNSUPPORTED`** — é resposta, não pendência: são dado bruto de
levantamento (LAZ, ECW, RINEX, shapefile), 41.537 fotos de campo e 338 GB de
filmagem de tráfego concentrados em 4 arquivos. O único grupo que merece
extrator novo é o `.zip` — 1.391 arquivos, 89,3 GB, 382 sob "Projeto
Executivo" — e só de índice, com a stdlib. Não sabemos quantos dos 36.584 PDFs
(148 GB, 21.864 em P0) são escaneados: há **1 amostra** no banco. Sem medir
isso, dimensionar OCR é chute.

**Busca (C).** O `texto_fts` atual indexa por `arquivo_id`, ou seja, pelo
representante — um conteúdo copiado em 30 pastas fica indexado sob o caminho de
uma só, e sobram trechos órfãos quando o representante muda (**161 hoje**). A
v5 indexa por `conteudo_id`: 35.872 conteúdos distintos para 61.427 ocorrências,
**41,6% de indexação economizada**. Conteúdo externo em vez de `contentless`
não é preferência: o SQLite desta máquina é **3.42.0** e `contentless_delete`
só existe a partir do 3.43 (verificado).

**Operação (D).** Medido no banco real: **39,7 itens/min, ~596 KB/s, ETA ~26 h**
para a fila. `--concorrencia` existe, padrão 1, e acima de 1 é **recusado** com
o motivo impresso — não há benchmark, o rate limit do Graph é por aplicativo, e
o WAL do SQLite tem um escritor só.

**QA (E).** Nenhum dos 2 CRÍTICOS impede o processamento em massa de começar:
são sobre preservar o que existe, não sobre a fila. O banco vivo está íntegro.

### O que a auditoria achou (2026-09-27) — fechado no código em 2026-10-04 (ver Pendências abertas)

| # | Achado | Por que importa |
|---|---|---|
| `T-09` | **45 duplicatas apontam para representante `presente=0`**, todas com `conteudo_id IS NULL` → invisíveis a `v_conteudo_orfao` e à reeleição. 41 estão em `DUPLICATE`, estado terminal. Nada no código as conserta. | Regressão da `DEC-027`. Precisa de backfill de `conteudo` para os 90 `COMPLETED` legados. |
| `T-10` | **Teto de extração de 300 MB aplicado DEPOIS do download**: 74 itens / 39 GB serão baixados inteiros para virar `PARTIAL`. | ~10 h de banda jogadas fora. Correção barata, alto retorno. |
| `T-11` | **Representante que vira `MISSING` no meio da rodada** deixa contador divergente e trecho apontando para `presente=0` — `reeleger_representantes()` só roda na subida. | Regressão da `DEC-032`. Recontar e reeleger dentro de `marcar_ausente()`. |
| `T-12` | **592 itens na fila que o worker nunca pode pegar** (`Coordenacao`, `DEC-017`): `v_fila` 61.929 − `v_fila_nuvem` 61.337. `99_status` usa esse número para prever horas. | Viola a `REGRA 7` — a previsão mente. |
| `T-13` | **`17_aplicar_delta.requeue()` deixa `conteudo_id` no conteúdo antigo.** `#128398` comprova: `CHANGED` com hash novo e `conteudo` com hash velho. | `v_ocorrencia` afirma que um caminho carrega um texto que ele não carrega mais. |
| `T-14` | **`caminho_rel`/`pasta_rel` não acompanham o recaminhamento**: 35 dos 39 ainda dizem `REV 04`. `pasta_rel` é a chave de junção com `pasta`, logo com a prioridade. | Arquivo movido para `0 - Obsoleto` mantém a prioridade da pasta de trabalho. |
| `T-15` | **A prova de read-only do `graph_ro` é um grep de AST** que só olha chamadas terminadas em `Request`. `urlopen(url, data=…)` ou `requests.post` passariam. | O que salva o projeto é o código, não o teste. A `DEC-014` não está provada como o cabeçalho afirma. |
| `T-16` | **Cinco testes que passam sem testar**, com prova de vacuidade: `test_reconciliacao.py:252` é `checar(X or True, …)`; `test_delta.py:276` é verdade por construção; `test_worker.py:222` avalia `[] == sorted([])`; `test_scanner.py:176` aceita `0 < parcial < 8` onde o esperado é 2; `foto_da_arvore` compara os arquivos alterados consigo mesmos — justamente no `checar` que sustenta a `REGRA 1`. | O pior tipo de dívida: dá confiança falsa. |
| `T-17` | **Sem reprocessamento por versão de extrator.** A `REGRA 9` promete, mas nada compara `extracao.versao`. | As correções de `821293a` (pdf/1.1, pptx/1.1, csv/1.1, texto/1.1) **não alcançam nada já processado**. Hoje são 125 extrações — consertar agora é barato. |
| `T-18` | **Item que pendura o download mantém o lock tomado** e o heartbeat parado. Falta timeout duro por item em `graph_ro`/`fonte`. | É o que eu trataria antes de deixar rodando a noite toda sem ninguém olhando. |

### Depois do E7

`E8` monitor por delta · `E9` autoria e versões · `E10` desligar o caminho local.

Detalhe da fronteira com o projeto de organização:
[`docs/06_FRONTEIRA_PROJETOS.md`](docs/06_FRONTEIRA_PROJETOS.md).

---

## Retomada rápida

`/session-start`, e depois "Próximo passo exato" no fim deste arquivo. O
comando único de operação é `python scripts\cerebro.py` (`status`,
`processar`, `pausar`, `retomar`, `erros`, `cobertura`, `sync`). A
massificação só começa com o checklist de `docs/13_PRONTIDAO_MASSIFICACAO.md`
verde; o worker está `PAUSADO` e não sobe sozinho.

---

## O acervo, medido (2026-09-17)

| | |
|---|---|
| Arquivos | **139.459** |
| Pastas | **20.101** |
| Volume | **1,168 TB** |
| Só na nuvem | **136.406 (97,8%)** |
| Vazios (0 byte) | 952 |
| Temporários (`~$`) | 268 |
| Erros na varredura | **0** |
| Tempo do inventário | **1,72 min** (1.352 arquivos/s) |

### Composição

| Categoria | Arquivos | Volume | Tem extrator? |
|---|---:|---:|---|
| imagem (.jpeg/.jpg/.png) | 46.280 | 65 GB | não (fila de OCR/visão, depois) |
| pdf | 39.217 | 156 GB | **sim** |
| outro (.dav, .idx, .prj, .dbf…) | 22.683 | 302 GB | não |
| xlsx | 22.351 | 50 GB | **sim** |
| docx | 3.374 | 22 GB | **sim** |
| cad (.dwg, .shp) | 2.791 | 64 GB | não |
| compactado (.zip) | 1.530 | 197 GB | não (ler índice sem extrair, depois) |
| texto | 874 | 2 GB | **sim** |
| mídia (.mp4…) | 60 | 338 GB | não |
| pptx / csv / bi / email | 31 | 0,2 GB | pptx e csv **sim** |

**65.842 arquivos têm extrator hoje** (a fila). **73.349 estão catalogados como
`UNSUPPORTED`** — não sumiram: têm tipo, tamanho, hash pendente e lugar na base,
esperando extrator.

---

## Prioridades descobertas pela árvore (sem IA)

| | Pastas | Arquivos | Com extrator | Volume |
|---|---:|---:|---:|---:|
| **P0** obra + categoria crítica | 6.744 | 33.132 | 30.111 | 201 GB |
| **P1** categoria crítica fora de obra | 589 | 3.682 | 3.402 | 26 GB |
| **P2** demais pastas de obra | 7.384 | 95.405 | 26.608 | 902 GB |
| **P3** acervo geral | 262 | 1.367 | 1.253 | 8 GB |
| **P4** backup/superado/lixeira | 1.023 | 5.605 | 4.468 | 58 GB |

P0 por obra: RD101 9.733 · RD104 8.482 · RD103 8.176 · RD102A 6.893.

P0 por categoria: medições 12.540 · não conformidades 6.971 · relatórios
semanais 3.486 · atas/reuniões 1.496 · relatórios mensais 1.450 · contratos
1.375 · ofícios 1.358 · prestação de contas 1.293 · desapropriação 1.250 ·
notas técnicas 894 · notificações 610 · Parceiro 377 · Órgão Contratante 137 · jurídico 47.

Lista completa: [`output/prioridades.md`](output/prioridades.md) e
[`output/pastas_classificadas.csv`](output/pastas_classificadas.csv).

---

## Validado em execução real

| O quê | Resultado |
|---|---|
| Inventário completo | 139.459 arquivos, 0 erros, 1,72 min |
| Classificação P0..P4 | 20.101 pastas, motivo gravado em cada uma |
| Worker em 146 documentos reais | 0 erros; texto extraído, indexado e buscável |
| Hidratação + liberação de espaço | 136 arquivos devolvidos para a nuvem |
| Integridade do original | sha256, tamanho e datas idênticos antes/depois |
| Pausa graciosa com worker vivo | parou no item, gravou, respondeu `SEGURO` |
| Retomada após queda simulada | item preso em `EXTRACTING` volta para a fila |
| Incremental | segunda rodada reprocessa **0** arquivos |

---

## Decisões tomadas

| # | Decisão |
|---|---|
| `DEC-001` | O scanner **não abre arquivo**: inventaria 1 TB sem baixar 1 TB. |
| `DEC-002` | SQLite + FTS5; texto derivado **fora do banco**, em `.txt.gz`. |
| `DEC-003` | Nada é apagado do banco: sumiu vira `MISSING`; sem extrator vira `UNSUPPORTED`. |
| `DEC-004` | Duplicata = mesmo sha256. Nome parecido é **candidato**, não conclusão. |
| `DEC-005` | Escopo é o acervo inteiro. P0..P4 é **ordem**, não recorte. |
| `DEC-006` | Sem API de IA e sem upload externo nesta fase. |
| `DEC-007` | Processador autônomo com `controle` no banco: pause/resume/stop sem IA. |
| `DEC-008` | Checkpoint = uma transação por item. Queda custa um arquivo. |
| `DEC-009` | Hidratação orçada (15 GB/sessão, piso de 25 GB livres) e devolvida depois. |
| `DEC-010` | Pausa é persistente: não se desfaz em reinício nem por autostart. |
| `DEC-011` | Arquivo de 0 byte não é erro nem duplicata — é `vazio` registrado. |
| `DEC-012` | **A nuvem (Microsoft Graph) é a fonte única de verdade e de conteúdo.** O disco sincronizado deixa de ser consultado — nem como cache. |
| `DEC-013` | **Acaba a hidratação.** Conteúdo vem por download temporário para `tmp/`, é usado e apagado. Sem orçamento de 15 GB, sem piso de 25 GB, sem tocar no cache do OneDrive. |
| `DEC-014` | **A porta para a nuvem é read-only por construção** (`graph_ro.py` recusa tudo que não for GET; o teste verifica a AST do módulo). Escrita é do `PROG_Organizacao_SharePoint`. |
| `DEC-015` | **`item_id` passa a ser a identidade**; `caminho_rel` vira atributo. É o que sobrevive à reorganização do acervo pelo outro projeto. |
| `DEC-016` | O Cérebro faz **o próprio inventário** por delta, em vez de ler o banco do projeto de organização — ciclos de vida e estados diferentes (ver `docs/06_FRONTEIRA_PROJETOS.md`). |
| `DEC-017` | `Coordenacao Programa - 02 Repositorio` (593 arquivos, outro site) fica **fora do escopo**. Registrado em `config/projeto.yaml`. |
| `DEC-018` | Duplicata exata passa a ser **`quickXorHash` do servidor**, não sha256 baixado (substitui a prática do `DEC-004`). O critério continua sendo hash, nunca nome. |
| `DEC-019` | O inventário da nuvem vai para **`item_nuvem` (área de espelho)**, não direto em `arquivo`. O E4 nunca põe em risco as 139 mil linhas existentes; quem decide o que entra em `arquivo` é o E5, com backup e métricas. |
| `DEC-020` | **Caminho é derivado da cadeia de `parent_id`**, não do `parentReference.path` de cada item. É o que mantém o caminho dos filhos correto quando uma pasta é renomeada e o delta não devolve os descendentes. |
| `DEC-021` | O `delta_link` só é gravado **quando a rodada termina**. Erro de API no meio preserva o checkpoint válido anterior e a rodada é retomada pelo `next_link`. |
| `DEC-022` | A reconciliação roda em **duas passadas**: primeiro todos os caminhos exatos, depois nome+tamanho no que sobrou. Sem isso, um casamento fraco rouba o item de um forte (aconteceu de verdade nas REV 00/REV 01 da desapropriação). |
| `DEC-023` | Vínculo duvidoso **não é aplicado**: vira linha em `reconciliacao` com `confianca` e fica em `v_excecoes_reconciliacao`. Fila explícita em vez de migração silenciosamente errada. |
| `DEC-024` | O schema v3 já existia no banco, aplicado por outra sessão sem deixar script. Foi **adotado como está** e extraído para `sql/schema_v3.sql`, para o repositório saber recriá-lo. |
| `DEC-025` | "Duplicata" tem **níveis de evidência**. `quick_xor` + `tamanho` = **FORTE**, e é o que basta para agrupar e pular extração. "Confirmada" só com hash criptográfico sobre o conteúdo — exigido apenas antes de ação irreversível, que não é deste projeto. Ver `docs/07_POLITICA_DUPLICATAS.md`. |
| `DEC-026` | Antes de pular a extração de uma cópia, o worker **reproduz o `quickXorHash` sobre os bytes que baixou** e compara com o do servidor. Não bateu: o grupo inteiro perde a otimização e cada cópia é extraída por si. |
| `DEC-027` | Num grupo de conteúdo idêntico, **1 representante extrai e as demais referenciam** (`duplicata_de`), preservando `drive_id`, `item_id`, `caminho` e prioridade de cada uma. Nenhuma cópia some do catálogo, e a busca devolve todos os lugares. Representante escolhido por `prioridade → criado_em → item_id`, de forma reproduzível. |
| `DEC-028` | `sha1Hash` **não é fornecido** por este tenant (0 de 137.857). A política registra o campo, mas não depende dele. |
| `DEC-029` | **`MISSING` = "o Cérebro conhece este registro histórico, mas o item não está disponível hoje no escopo do Graph".** Não é exclusão: a linha fica, com classificação, extrações, derivados e vínculos. O estado anterior vai no evento `ausente_na_nuvem`; se o item reaparecer no delta, a reconciliação **restaura o estado anterior** e grava `reapareceu_na_nuvem`. Fora do escopo (`sem_mapa`) **não** vira `MISSING`. |
| `DEC-030` | O worker da nuvem consome **`v_fila_nuvem`**, que exige `drive_id`, `item_id`, `presente=1` e `presente_nuvem=1`. Sem identidade da nuvem não há por onde baixar — e não existe fallback para o disco local. |
| `DEC-031` | **`WAITING_HYDRATION` quer dizer "esperando a nuvem"**, não "esperando hidratação" — o nome sobrou da `DEC-013`. É para onde `worker.falhar()` manda quem tropeçou em 429/503/timeout, e por isso ele **precisa** estar na fila da nuvem (worker e `v_fila_nuvem`). Sem isso, uma queda de rede tirava o item da fila para sempre. |
| `DEC-032` | **Trocar de conteúdo reconta os dois lados.** Quando uma ocorrência passa a apontar para outro `conteudo`, o grupo que a perdeu também é recontado. Um contador que diverge de `v_ocorrencia` é bug, e a consulta 3 de `sql/consultas_busca.sql` existe para que ele não volte em silêncio. |
| `DEC-033` | **404/410 do Graph no download = `MISSING`, não `ERROR_FINAL`.** O item não estar mais lá não é falha de extração: é o acervo que mudou. Marca `presente_nuvem=0`, grava `ausente_na_nuvem` com o estado anterior, não gasta tentativa e não reagenda. A linha fica inteira e a reconciliação a devolve se o item reaparecer (`DEC-029`, REGRA 6). |
| `DEC-034` | **A prova de qualquer etapa nova é uma amostra determinística e registrada antes de rodar.** `16_amostra_e7.py selecionar` grava os ids, a composição e o retrato do estado; `relatar` compara depois. Amostra sorteada sem registro não prova nada. |
| `DEC-035` | **HTTP 410 `resyncRequired` dispara varredura completa automática.** O servidor descartou o token; insistir no incremental só repete o 410. O token morto é descartado e a rodada recomeça do zero. |
| `DEC-036` | **Depois de um resync completo que chegou ao fim, o que não voltou foi removido.** Um delta completo não devolve exclusões — devolve o que existe. A trava é o `execucao_id`: rodada incremental, parcial ou retomada nunca varre, porque ali "não veio" quer dizer "não mudou". |
| `DEC-037` | **Pasta removida leva os descendentes junto.** O delta devolve só a pasta apagada. Sem a cascata, o inventário afirmaria que existem arquivos dentro de um lugar que não existe mais. |
| `DEC-038` | **O espelho (`item_nuvem`) e o catálogo (`arquivo`) são propagados por um passo explícito** (`17_aplicar_delta.py`), pela identidade `(drive_id, item_id)`, com simulação obrigatória antes de gravar. Arquivo novo no espelho **não** entra no catálogo por ali: isso exige classificação e prioridade, que são do `01`/`02`. |
| `DEC-040` | **A conversa é descartável; o repositório é a fonte canônica.** A continuidade do projeto não depende da memória de nenhuma sessão: `/session-start` reconstrói pelo repositório, pelo Git e **pelo banco** (boa parte do estado real não está em arquivo), e `/session-close` persiste o que só existiria na conversa. Evidência verificável vence texto, e divergência se **mostra**, não se conserta em silêncio. O mapa de autoridade está em `docs/12_CONTINUIDADE.md`; o histórico de sessões em `state/sessoes/`. |
| `DEC-039` | **O processamento massivo nunca depende de IA — é arquitetura, não fase.** O pipeline de produção (`Graph → inventário → fila → download temporário → validação → extrator Python → TXT/JSON/SQLite/FTS → limpeza`) é inteiramente determinístico e roda sem nenhuma sessão aberta. Dentro do laço é proibido chamar API de IA, enviar documento para fora, decidir arquivo por arquivo com IA ou depender de sessão viva. Item cuja decisão não cabe numa regra determinística vira estado explícito e espera. Claude constrói a máquina; a máquina trabalha sozinha. Registrado também na REGRA 5 do `CLAUDE.md`. |
| `DEC-041` | **A busca corta o texto em trechos de até 1.500 caracteres com até 150 de sobreposição, alinhada a início de palavra, sem nunca atravessar página, slide ou aba (`chunk/2.0:1500:150`).** O corte procura, na metade final da janela, parágrafo > fim de frase > linha > espaço. A versão (com os parâmetros) é carimbada em `meta.busca.versao_chunker`; mudar qualquer número é mudança de versão e põe tudo em `v_reindexar`. Medido em 2026-10-04 contra 800/100, 2500/250, parágrafo sem sobreposição e o corte atual: mesma qualidade de recuperação do melhor, metade dos trechos do 800/100, zero trecho começando no meio da palavra (o atual: 56%), e a sobreposição evita 1,5% de frases partidas que o corte por parágrafo perde. A Emmily delegou a decisão ao sprint (STATE.md, "Decisões que dependem da Emmily", item 1). |
| `DEC-042` | **O que se sabe que não será lido não é baixado.** Antes do download, o worker confere duas regras com o que o inventário já sabe: tamanho acima de `worker.tamanho_max_extracao_mb` (300) → `PARTIAL` com `extracao.versao='adiado/teto_tamanho'` e erro `ADIADO_TETO_TAMANHO:`; extensão sem leitor (`extratores.suporta()`: `.doc .xls .ppt .xlsb .rtf .odt .ods .odp`) → `UNSUPPORTED` com erro `SEM_LEITOR:`. Nenhum estado novo; as duas marcas são consultáveis para retomada quando houver leitor (ver `22_reprocessar_extrator.py --erro`). Poupa ~75 itens / 40 GB (teto) e 668 itens / 2,2 GB (sem leitor) da fila de 2026-10-04. |
| `DEC-043` | **OCR é decidido por página, não pela média do documento** (`pdf/1.2`; desde `pdf/1.3` as métricas por página ficam no perfil, `ocr_metricas`, e recalibrar não exige baixar de novo). Uma página pede OCR quando tem menos de 80 caracteres **e** imagem cobrindo ≥ 50% da área ou desenho sem texto; página em branco não conta. Basta uma página assim para o PDF virar `OCR_REQUIRED` (`ocr_tipo` = `misto` ou `escaneado`, `paginas_ocr` no perfil) — o texto das outras páginas é indexado assim mesmo. Calibração provisória: depende da amostra real de PDFs escaneados (`L1`), que ainda não existe. |
| `DEC-044` | **O schema de produção tem contrato versionado.** `sql/schema_producao_v4.snapshot.sql` é o retrato normalizado do banco real; `tests/test_schema.py` prova que a receita do repositório (`00_init_db` + migrações) chega nele, inclusive a partir de um v2 legado. Mudança de schema só entra com script de migração **e** snapshot regenerado por `20_snapshot_schema.py` — nenhuma estrutura pode existir só porque uma sessão rodou SQL no banco (fecha `CR-1`, reforça `DEC-024`). |
| `DEC-045` | **Ocorrência conta só se está viva (`presente=1`), e o reprocessamento por versão volta só o representante.** `cu.recontar_ocorrencias` é a definição única (worker, `17_`, `21_`); representante ausente ou que mudou de bytes é substituído na mesma transação, movendo os trechos sem apagar. `22_reprocessar_extrator.py` compara `extracao.versao` com `extratores.VERSOES` e devolve à fila um item por conteúdo; a extração antiga continua pesquisável até a nova chegar (fecha `T-11`, `T-13`, `T-17`). |

---

## Próximo passo exato — LEIA ISTO PRIMEIRO AO RETOMAR

**Onde parou:** `main` limpo no fim da sessão 002, 1.671 testes offline e 66
de rede verdes (2026-10-04), worker `PAUSADO`. O **código** está pronto; o
**banco real** continua v4, sem as correções de dados.

**A próxima unidade é o `T-21`: aplicar as correções no banco real**, na
ordem do checklist de [`docs/13_PRONTIDAO_MASSIFICACAO.md`](docs/13_PRONTIDAO_MASSIFICACAO.md)
(G3 → G11). Worker pausado o tempo todo — esses scripts não tomam a trava.

```
python scripts\cerebro.py status                         # G3: lock livre, PAUSE
python scripts\04_migrar_v4.py                           # G5: grava ao rodar (views, DROP do indice duplicado)
python scripts\20_snapshot_schema.py --snapshot sql\schema_producao_v4.snapshot.sql   # "IDENTICO"
python scripts\21_backfill_conteudo.py                   # G6: simula -> 60 / 60 / 82 / 1 / 2
python scripts\21_backfill_conteudo.py --aplicar         #     backup antes; invariantes viram 0
python scripts\cerebro.py sync --aplicar                 # G7: espelho novo + ~149 caminhos realinhados
python scripts\05_migrar_v5.py                           # G8: simula -> ~86 conteudos, ~4.688 trechos
python scripts\05_migrar_v5.py --aplicar                 #     backup antes; schema_versao 5
python scripts\22_reprocessar_extrator.py                # G9: simula -> ~85 conteudos (pdf 49, xlsx 20, texto 10, docx 5, pptx 1)
python scripts\22_reprocessar_extrator.py --aplicar
```

Mais a execução `#20` (aberta desde 2026-09-22), que nenhum script fecha:
`UPDATE execucao SET status='abandonada', encerrada_em=<agora> WHERE id=20`
— com o banco já em backup (G10).

**Critério de conclusão:** `meta.schema_versao='5'`, `cu.checar_invariantes`
com todas as listas vazias (G11), `v_reindexar` = 0, `v_busca_saude` toda 0,
nenhuma execução `em_andamento`. Os números esperados de cada passo estão no
G5–G10; se divergirem, **pare e investigue** antes do passo seguinte.

**Depois do `T-21`:** a **rodada 1 controlada** (supervisionada, de dia):
`cerebro.py retomar` → `cerebro.py processar --prioridade-max 0 --limite 300
--max-gb-sessao 3 --uma-vez`, com os critérios de aceite do `docs/13`. Só
então a primeira noite (G12 já está verde no código; falta G13 — Agendador,
janela 19–07 no yaml, validade do segredo).

**Decisões que dependem da Emmily:**

1. **`T-08`** — 3.528 arquivos do Parceiro sem cópia viva. A lixeira do
   SharePoint guarda ~93 dias: a exclusão de ~20/09 expira por volta de
   **22/12** (confirmar com a TI). Tem prazo.
2. **`T-19`** — quantas cópias manter em `data/backup/` (~7,5 GB, e o `21` e o
   `05` acrescentam ~1,9 GB). Apagar backup é decisão dela.
3. **Amostra L1** (quantos PDFs são escaneados): usar a rodada 1 com ~300 PDFs
   P0 estratificados por tamanho. A regra de OCR (`DEC-043`) é provisória até
   isso, mas o `pdf/1.3` grava as métricas por página: recalibrar não exige
   baixar de novo.
4. **Extrator de índice de `.zip`** (1.391 arquivos, 89,3 GB): independente da
   massificação — pode entrar depois sem reprocessar nada.
5. **Gatilho do Agendador** (`T-25`): `LogonTrigger` não roda depois de reboot
   de madrugada até alguém logar.

---

## Pendências abertas

| # | Assunto | Situação |
|---|---|---|
| `T-01` | 73.349 arquivos sem extrator (imagem, CAD, .zip, .dav, mídia) | `UNSUPPORTED` por decisão; só `.zip` (índice) é candidato |
| `T-02` | OCR dos PDFs escaneados | fila `OCR_REQUIRED`; depende de instalar Tesseract e da amostra L1 |
| `T-03` | Autostart no Windows | receita em `docs/10_OPERACAO_AUTONOMA.md`; ver `T-25` |
| `T-04` | 952 arquivos de 0 byte | tratados como `vazio`; conferir se é falha de sync |
| `T-05` | `.dav` (9.968 arq, 41 GB) parece vídeo de CFTV | confirmar se entra no escopo |
| ~~`T-06`~~ | ~~Parceiro sumiu do drive~~ | **FECHADO 2026-09-22.** Ver sessões anteriores no histórico deste arquivo. |
| ~~`T-07`~~ | ~~2.023 novos fora do catálogo~~ | **FECHADO 2026-09-27.** |
| `T-08` | 3.528 arquivos do Parceiro sem nenhuma cópia viva | restauração da lixeira com a TI; **prazo ~22/12** |
| ~~`T-09`~~ | ~~duplicatas órfãs / legados sem conteúdo~~ | **FECHADO no código 2026-10-04** (`21_backfill_conteudo.py`); aplicar no banco = `T-21` |
| ~~`T-10`~~ | ~~teto de 300 MB depois do download~~ | **FECHADO 2026-10-04** — `DEC-042` |
| ~~`T-11`~~ | ~~representante MISSING no meio da rodada~~ | **FECHADO 2026-10-04** — `DEC-045` |
| ~~`T-12`~~ | ~~previsão pela `v_fila`~~ | **FECHADO 2026-10-04** — tudo pela `v_fila_nuvem` |
| ~~`T-13`~~ | ~~`requeue()` deixa `conteudo_id` velho~~ | **FECHADO 2026-10-04** — `cu.desvincular_conteudo` |
| ~~`T-14`~~ | ~~caminho não acompanha recaminhamento~~ | **FECHADO no código 2026-10-04** — `17` realinha; aplicar = `T-21` (G7) |
| ~~`T-15`~~ | ~~prova de read-only era grep de AST~~ | **FECHADO 2026-10-04** — AST + transporte gravador |
| ~~`T-16`~~ | ~~testes vácuos~~ | **FECHADO 2026-10-04** — provas em `docs/11a_QA_VACUIDADE.md` |
| ~~`T-17`~~ | ~~sem reprocessamento por versão~~ | **FECHADO 2026-10-04** — `22_reprocessar_extrator.py`, `DEC-045` |
| ~~`T-18`~~ | ~~download pendurado segura o lock~~ | **FECHADO 2026-10-04** — prazo duro por item |
| `T-19` | `data/backup/` acumula sem poda (~7,5 GB) | decisão da Emmily (exclusão de arquivo) |
| `T-20` | `STATE.md` ainda carrega o histórico E0–E8 | enxugar quando houver folga; histórico novo já vai para `state/sessoes/` |
| `T-21` | **Correções de dados não aplicadas no banco real** | **próximo passo** — comandos acima e G5–G11 do `docs/13` |
| `T-22` | `proximo()` custa ~389 ms (sort da fila inteira) → ~3–7 h na massa | índice parcial; é mudança de schema (`DEC-044`: migração + snapshot) |
| `T-23` | `sync`/`17`/`21`/`22`/`04` não tomam a trava do worker (R1) | até corrigir: **scripts de dados só com o worker pausado** |
| `T-24` | `A-8`: resync interrompido perde detecção de remoções (`12_scan_graph.py`) | mitigação: `cerebro.py sync --aplicar --completo` depois de interrupção/410 |
| `T-25` | Agendador não registrado; `--max-gb-sessao` não limita a noite; sem `worker.janela` no yaml (R7/R8) | G13 do `docs/13` |
| `T-26` | Heartbeat parado durante a **extração** (o do download já existe) | exige thread com conexão própria; aceitável com o prazo do download |
| `T-27` | Sem ferramenta para devolver `ERROR_FINAL` à fila por causa; conteúdo `ERRO`/`NAO_SUPORTADO` não cobre as cópias | melhorias, não bloqueiam |
