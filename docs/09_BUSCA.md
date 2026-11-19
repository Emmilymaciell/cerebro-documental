# A camada de busca documental

Escrita em 2026-09-27 (desenho) e fechada em **2026-10-04** (sprint de
convergência: política de corte medida e decidida, módulo real, migração
testada em cópia do banco real). Descreve a camada que responde **"onde está
escrito isto?"** — determinística, local, sem IA (REGRA 5, `DEC-039`).

| Artefato | O que é |
|---|---|
| [`scripts/busca.py`](../scripts/busca.py) | normalização, corte, indexação por conteúdo, consulta, manutenção, benchmark |
| [`scripts/05_migrar_v5.py`](../scripts/05_migrar_v5.py) | migração v4 → v5: simulação por padrão, `--aplicar` com backup e transação única |
| [`sql/schema_v5_busca.sql`](../sql/schema_v5_busca.sql) | DDL v5 — **ainda não aplicado no banco real** (aplicação é do Lead, seção 10) |
| [`tests/test_busca.py`](../tests/test_busca.py) | 266 PASS / 0 FAIL — propriedades, pipeline, manutenção, migração ponta a ponta |
| [`sql/consultas_busca.sql`](../sql/consultas_busca.sql) | consultas do v4 que continuam valendo |
| [`docs/07_POLITICA_DUPLICATAS.md`](07_POLITICA_DUPLICATAS.md) | a política que sustenta o modelo por conteúdo |

---

## 1. O caminho, ponta a ponta

```
ocorrência          arquivo(drive_id, item_id, caminho, prioridade, autoria)
     |                  ...............................................
     |  (quick_xor, tamanho_bytes)                                    :
     v                                                                :
conteúdo            conteudo(id)  — os bytes, uma vez só              :
     |                                                                :
     |  extrator -> paginas_texto: [(número, texto), ...]             :
     v                                                                :
texto normalizado   busca.normalizar()          norm/1.1              :
     v                                                                :
segmentos           busca.preparar()  — página | slide | aba | documento
     |                                                                :
     |  Politica.cortar()  chunk/2.0:1500:150, nunca cruza segmento   :
     v                                                                :
trechos             trecho(conteudo_id, segmento_id, ordinal,         :
     |                     char_inicio, char_fim, texto)              :
     |  gatilho                                                       :
     v                                                                :
índice invertido    busca_fts  FTS5, content='trecho'                 :
     |                                                                :
     |  busca.buscar()  MATCH + bm25, LIMIT por conteúdo              :
     v                                                                :
resultado  ------> v_trecho_ocorrencia  -> v_ocorrencia (v4) ..........:
                   (volta para TODAS as ocorrências presentes)
```

**Entra por ocorrência, é processado por conteúdo, e volta por ocorrência.**
Um contrato copiado em dez pastas é extraído uma vez, indexado uma vez, e
encontrado nas dez. Provado ponta a ponta no teste 16, que parte de um banco
v4 criado pela receita do repositório (`00_init_db.py`), roda o `05` e busca.

---

## 2. A decisão central: o índice é por **conteúdo**, não por ocorrência

O v4 separou ocorrência (`arquivo`) de conteúdo (`conteudo`). O índice atual
`texto_fts` ficou do lado errado dessa linha: é chaveado por `arquivo_id`.

1. **Reeleição de representante** troca o `arquivo_id` que representa o
   conteúdo, e o índice continua apontando para o antigo.
2. **Uma ocorrência que vira `MISSING` apaga da busca um conteúdo vivo** se ela
   era a que carregava o índice.
3. **Reprocessar duas cópias escreve o mesmo texto duas vezes.**

Em v5, `trecho.conteudo_id` é a chave. **Um conteúdo = uma indexação.**

| Medido em 2026-09-27 | |
|---|---:|
| Ocorrências suportadas, vivas, com hash | 61.427 |
| Conteúdos distintos entre elas | **35.872** |
| Indexações que não são feitas | **25.555 (41,6%)** |

**Correção de diagnóstico (2026-10-04).** As 161 linhas de `texto_fts` "a mais"
que a seção 10 da versão anterior chamava de lixo de reeleição **não são lixo**:
são trechos de **60 arquivos extraídos na era local (2026-09-18)** que nunca
ganharam `conteudo_id` — **56 deles estão presentes e `COMPLETED`**. Eles ficam
fora do índice v5 até ganharem uma linha em `conteudo` (seção 13, risco 1).

---

## 3. Normalização determinística — `norm/1.1`

Função pura (`busca.normalizar`), sem estado, sem rede, sem IA, idempotente.
Na ordem:

| Passo | O que faz | Por quê |
|---|---|---|
| 0. **Acento solto** *(novo em 1.1)* | `¸c`→`ç`, `˜a`→`ã`, `´e`→`é`, `ˆe`→`ê`, `¨u`→`ü` — **só** se o resultado for UM caractere pré-composto (NFC) | PDF gerado por LaTeX desenha o acento como glifo separado **antes** da letra. Achado na amostra: o certificado de calibração (conteúdo 23) dizia `Calibra¸c˜ao`, e `calibracao` **não achava nada** |
| 1. **NFKC** | ligaduras (`ﬁ`→`fi`), sobrescritos (`m²`→`m2`), largura plena | `eﬁciência` e `eficiência` são a mesma palavra |
| 2. **Invisíveis** | remove `U+200B..D`, `U+FEFF`, `U+00AD`, `U+2060` | quebram token sem aparecer |
| 3. **Espaços exóticos** | `U+00A0`, `U+1680`, `U+2000..A`, `U+202F`, `U+205F`, `U+3000`, tab → espaço | idem |
| 4. **Fim de linha** | `\r\n`, `\r` → `\n` | origem mista |
| 5. **Controles** | remove categoria `C*`, exceto `\n` | ruído do extrator |
| 6. **Hifenização** | `minúscula-\nminúscula` → junta | `desapropria-\nção` |
| 7. **Espaços** | 2+ → 1; espaço antes de `\n` some; 3+ `\n` → 2 | colunas do PDF |
| 8. **Cabeçalho/rodapé corrido** | só em **página** *(1.1)* | ver abaixo |

**Não baixa caixa, não tira acento, não remove pontuação, não aplica
stemming.** Quem dobra caixa e acento é o tokenizador (`unicode61
remove_diacritics 2`), na consulta. O snippet devolve o que o documento diz.

**A consulta passa pela mesma dobra** (`busca.normalizar_consulta`): sem isso,
`m²` digitado não acharia o `m2` que o NFKC gravou. Texto livre vira frases
FTS5 seguras com `busca.consulta_de_termos` (`RD-101` não quebra a sintaxe).

### Regra 8: cabeçalho e rodapé corridos — só em página

```
BORDA_LINHAS 3 · MIN_SEGMENTOS 5 · FRACAO_REPETICAO 0,6 · MAX_CHARS_LINHA 120
```

Linha de borda que se repete em ≥ 60% das páginas sai; linha de borda que é
**só número de página** sai sempre. As linhas removidas ficam em
`indice_conteudo.cabecalho_removido` (JSON) — nada some sem rastro.

**Mudança da 1.1:** a regra só age quando **todos** os segmentos são páginas
(PDF). Em aba de planilha a "última linha que é só um número" é **dado**
(`2150`, uma cota), não rodapé; em slide, o título repetido é conteúdo. Teste 3.

---

## 4. Corte em trechos — `chunk/2.0:1500:150`

### A política

| | |
|---|---|
| Tamanho máximo | **1.500 caracteres** (nenhum trecho passa disso) |
| Sobreposição | **até 150 caracteres**, recuando até o **início de uma palavra** |
| Fronteira | **o trecho nunca atravessa o segmento** (página/slide/aba) |
| Onde corta | na metade final da janela, no separador mais forte que houver: **parágrafo** (`\n\n`) > **fim de frase** (`[.!?;]` + espaço) > **quebra de linha** > **espaço** > corte duro |
| Corte duro | só em token sem espaço maior que 750 caracteres; **nunca separa letra de acento combinante** |
| Separador | fica no **fim** do trecho que termina |
| Unidade curta | menor que 1.500 → **exatamente 1 trecho**, inteira |
| Unidade vazia | normalizou para nada → **não vira segmento** (e conteúdo sem nenhum segmento vira `SEM_TEXTO`) |
| Limite de trechos por conteúdo | **nenhum.** O teto é do extrator (`paginas_max_pdf` 3.000; planilha 4 M caracteres / 5.000 linhas por aba), e ele marca `PARCIAL`. Cortar no índice seria esconder texto em silêncio |

Propriedades provadas no teste 4, sobre um corpus fixo (PT-BR com acento,
linhas de planilha, token gigante sem espaço, acentos decompostos, emoji e
plano astral, texto de exatamente 1.500 e de 1.501 caracteres, texto vazio):
mesma entrada → **mesmos trechos, bytes idênticos**; juntar os trechos sem a
sobreposição **reconstrói o texto normalizado**; nenhum trecho vazio; nenhum
maior que o máximo; posições exatas; sobreposição entre 0 e 150 sem buraco;
nenhum trecho começa em acento solto. Teste 8 prova a fronteira de página com
o banco: nenhum trecho mistura duas páginas, e os trechos de cada página
reconstroem a página.

### Por que a 2.0 e não a 1.0

A 1.0 (`cerebro_util.trechos_de`, o que o worker faz hoje) recuava exatamente
150 caracteres — **no meio da palavra**. `medição` cortada vira o token `ção`
no índice: lixo que casa com consulta errada e infla o vocabulário. Na amostra
real, **2.345 de 4.196 trechos (56%) começavam no meio de uma palavra.** Na
2.0: zero.

### DECISÃO — `DEC-041` — 2026-10-04

> | `DEC-041` | **A busca corta o texto em trechos de até 1.500 caracteres com até 150 de sobreposição, alinhada a início de palavra, sem nunca atravessar página, slide ou aba (`chunk/2.0:1500:150`).** O corte procura, na metade final da janela, parágrafo > fim de frase > linha > espaço. A versão (com os parâmetros) é carimbada em `meta.busca.versao_chunker`; mudar qualquer número é mudança de versão e põe tudo em `v_reindexar`. Medido em 2026-10-04 contra 800/100, 2500/250, parágrafo sem sobreposição e o corte atual: mesma qualidade de recuperação do melhor, metade dos trechos do 800/100, zero trecho começando no meio da palavra (o atual: 56%), e a sobreposição evita 1,5% de frases partidas que o corte por parágrafo perde. A Emmily delegou a decisão ao sprint (STATE.md, "Decisões que dependem da Emmily", item 1). |

### O benchmark que sustenta a decisão

Reproduzível: `python scripts\busca.py bench --banco <caminho do banco>` — abre
o banco **somente leitura**, copia pela API de backup para `%TEMP%`, aplica o
v5 na cópia, indexa a amostra com cada política, mede e **apaga a cópia**.
Nada sai da máquina; nada escreve no banco de origem.

**Amostra:** os 29 conteúdos com texto do banco real em 2026-10-04 (13 PDF,
2 PPTX, 8 XLSX, 4 DOCX, 2 texto) — 5.498.732 caracteres, 284 segmentos.
Honestamente pequena e **enviesada por uma planilha** (conteúdo 12, 4,9 M
caracteres, ~80% dos trechos). O texto de cada um foi reconstruído do índice v4
e conferido byte a byte contra o derivado (seção 10).

**Consultas:** 25, escritas à mão a partir do vocabulário real da amostra
(certidão negativa, tubo de concreto, aterro Proctor, faixa de domínio RD-101,
terraplenagem, balancim, condicionante de efluentes, eventograma, recursos
hídricos, conformidade ambiental, projeto executivo, RINEX, SICRO, estudos
geotécnicos, abrasão Los Angeles, sítio arqueológico, calibração, boletim de
sondagem, decreto de utilidade pública, traçado, resina acrílica...). Cada uma
tem um **conteúdo-alvo** e uma **frase-alvo** que o bench confere estar no
alvo antes de medir — se o banco mudar, a consulta é acusada de inválida em vez
de medir errado. Lista em `busca.CONSULTAS_BENCH`.

| Política | trechos | por conteúdo | redundância | índice FTS | tempo | início no meio da palavra | frases partidas | acerto@1 | acerto@3 | MRR | trecho certo | frase exata | contexto médio |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A — chunk/1.0 1500/150 (atual) | 4.196 | 144,7 | 10,7% | 2.403 KB | 7,1 s | **2.345** | 2 / 29.193 | 22/25 | 25/25 | 0,94 | 25/25 | 25/25 | 1.252 |
| B — chunk/2.0 800/100 | 9.539 | 328,9 | 16,1% | 2.618 KB | 9,2 s | 1 | 2 / 29.193 | 22/25 | 25/25 | 0,94 | 24/25 | 25/25 | 650 |
| **C — chunk/2.0 1500/150** | **4.545** | **156,7** | **11,3%** | **2.426 KB** | **6,7 s** | **0** | **2 / 29.193** | **22/25** | **25/25** | **0,94** | **25/25** | **25/25** | **1.141** |
| D — chunk/2.0 2500/250 | 2.729 | 94,1 | 10,9% | 2.436 KB | 5,7 s | 0 | 1 / 29.193 | 21/25 | 25/25 | 0,92 | 24/25 | 25/25 | 1.630 |
| E — parágrafo 1500, sem sobreposição | 4.107 | 141,6 | 0,0% | 2.304 KB | 5,5 s | 0 | **452 / 29.193** | 22/25 | 25/25 | 0,94 | 25/25 | 25/25 | 1.119 |

Como ler cada coluna (todas determinísticas):

- **redundância** — texto guardado em `trecho` além do texto normalizado (o custo da sobreposição);
- **índice FTS** — bytes do invertido (`busca_fts_data` + `_idx` + `_docsize`), sem o texto;
- **início no meio da palavra** — trechos cujo primeiro caractere continua uma palavra do trecho anterior (token falso no índice);
- **frases partidas** — amostra sistemática de frases de 6 palavras (uma a cada 40 palavras de cada segmento) que **não cabem inteiras em nenhum trecho**: é a busca de frase que falha por causa do corte;
- **acerto@k / MRR** — o conteúdo-alvo entre os k primeiros conteúdos de `busca.buscar()`;
- **trecho certo** — o trecho que a busca devolve para o alvo **contém a frase-alvo** (snippet útil);
- **frase exata** — a frase-alvo como consulta de frase (`"..."`) acha o alvo;
- **contexto médio** — caracteres do trecho devolvido (o que a pessoa lê para conferir).

**Leitura:**

- **E está descartada**: sem sobreposição, 1,55% das frases ficam partidas
  entre dois trechos — busca de frase que falha em silêncio.
- **B não compra nada**: 2,1× mais trechos, +8% de índice, +16% de texto
  redundante, e a qualidade não sobe (trecho certo cai para 24/25).
- **D perde precisão**: −40% de linhas, mas acerto@1 cai para 21/25 (o
  "decreto de utilidade pública" passa para 2º), o trecho certo cai para
  24/25, e o trecho devolvido tem 1.630 caracteres — localiza pior.
- **C = A em qualidade**, com o mesmo índice (+1%) e **sem nenhum trecho
  começando no meio da palavra**. Os +8% de trechos vêm de cortar em fim de
  frase/parágrafo em vez de no último espaço.

As 3 consultas fora do 1º lugar são **iguais em todas as políticas** e vêm do
ranking, não do corte: a planilha gigante (conteúdo 12, 3.664 trechos) tem
tantos trechos que algum sempre casa bem com consultas genéricas. É ponto para
a seção 9 (se um dia virar problema medido: ponderar por conteúdo, não por
trecho), não para o chunking.

**Tempo:** ~0,8 M caracteres por segundo nesta máquina. Indexar não é gargalo
diante do download.

---

## 5. O modelo de dados

```
conteudo (v4)  1 ──── N  segmento  1 ──── N  trecho  1 ──── 1  busca_fts
    │                                                          (content externo)
    │ 1 ──── N  arquivo (v4) = a ocorrência
    │ 1 ──── 1  indice_conteudo = a contabilidade da indexação
```

| Tabela | O que é | Chave |
|---|---|---|
| `segmento` | a unidade de **origem**: página, slide, aba, documento | `(conteudo_id, ordem)` |
| `trecho` | o pedaço indexável, com posição no segmento e texto normalizado | `id` (= `busca_fts.rowid`) |
| `busca_fts` | FTS5, **conteúdo externo** sobre `trecho` | `rowid` |
| `indice_conteudo` | o que foi indexado: estado, versões, `sha256_texto`, **`fonte_texto`**, `execucao_id` | `conteudo_id` |

| Categoria | `tipo` | `numero` | `rotulo` |
|---|---|---|---|
| pdf | `pagina` | página real, 1-based | `p. 12` |
| pptx | `slide` | slide, 1-based | `slide 3` |
| xlsx | `aba` | ordem da aba | **o nome da aba**, lido da linha `[aba] Nome` que o extrator escreve |
| docx, csv, texto | `documento` | `0` | `documento` — o extrator não pagina, e paginação inventada é pior que nenhuma |

`indice_conteudo.estado`: `INDEXADO` | `SEM_TEXTO` (considerado, sem texto) |
`REMOVIDO` (limpeza manual, seção 8). `fonte_texto`: `extrator` (worker) |
`indice_v4` (reconstruído na migração) | `derivado` (o `.txt.gz`).

Continuam valendo, do desenho de 2026-09-27: `trecho` é tabela real porque o
SQLite desta máquina é **3.42.0, sem `contentless_delete`**; o FTS5 tem
conteúdo externo (o texto existe uma vez); a sincronização é por **gatilho**
(`trg_trecho_ai/_ad/_au`); **sem `ON DELETE CASCADE`** de `segmento` para
`trecho` (apagar segmento antes do trecho dá erro — teste 13b); tokenizador
`unicode61 remove_diacritics 2`, sem `tokenchars`, sem índice de prefixo.

---

## 6. Reindexar sem deixar lixo e sem refazer o que não mudou

`v_reindexar` é a fila (`SELECT COUNT(*)` — REGRA 7). Motivos:

| `motivo` | Quando |
|---|---|
| `NUNCA_INDEXADO` | texto extraído, nenhuma linha em `indice_conteudo` |
| `REMOVIDO` | foi desindexado e uma ocorrência voltou |
| `EXTRATOR_NOVO` | `conteudo.versao_extrator` mudou |
| `NORMALIZADOR_NOVO` | `meta.busca.versao_normalizador` mudou |
| `CHUNKER_NOVO` | `meta.busca.versao_chunker` mudou |

Só entra conteúdo `EXTRAIDO`/`PARCIAL` com **ocorrência presente**.

**Trava fina, `sha256_texto`:** antes de tocar em qualquer linha, a indexação
calcula o hash do texto normalizado (segmentos, rótulos e texto). Igual e
mesmas versões → **nenhuma linha de `trecho` é tocada**, só os carimbos
(`INALTERADO`; teste 10 prova os mesmos `rowid`).

**Texto mudou:** uma transação — `DELETE trecho` (gatilho limpa o invertido) →
`DELETE segmento` → `INSERT` → `UPSERT indice_conteudo`. **Substitui, não
acumula** (teste 9: nenhum `id` antigo sobrevive, contabilidade = trechos).

**Bytes mudaram:** é outro `conteudo_id`. A ocorrência passa a apontar para o
novo; o antigo continua achável pelas cópias que não mudaram (teste 14a).

`busca.conferir_versoes(con)` devolve as divergências entre o que o código
produz e o que o banco carimbou. **O worker deve chamá-la na subida e recusar
subir se vier algo** — indexar com versão diferente da carimbada poria cada
item novo direto em `v_reindexar`.

---

## 7. Duplicata: indexa uma vez, devolve todos os lugares

Teste 5/6: 1 conteúdo com 3 ocorrências = **1** linha em `indice_conteudo`,
**2** trechos (não 6); processar a 2ª cópia = `INALTERADO`; buscar devolve
**3** linhas com `item_id` e caminho próprios, por prioridade, com exatamente
um representante. Teste 16 repete na migração: 1 conteúdo com 2 ocorrências
presentes + 1 `MISSING` → 2 linhas no resultado.

**A unidade do resultado é o conteúdo:** o `LIMIT` fica na etapa do FTS5,
sobre conteúdos com ocorrência presente; a expansão nas ocorrências vem
depois. A consulta canônica está em `busca.SQL_BUSCA`.

---

## 8. Manutenção: atualização, remoção, rastreabilidade

| Evento | O que acontece |
|---|---|
| Ocorrência vira `MISSING` | **nada no índice.** `v_ocorrencia` filtra `presente=1`; ela some do resultado, as outras continuam |
| O representante vira `MISSING` | **nada.** O índice é do conteúdo |
| A ocorrência reaparece (`DEC-029`) | volta ao resultado na hora, sem reextrair nem reindexar |
| **Última** ocorrência some | o conteúdo some do resultado **na hora, sem escrita** (a consulta exige ocorrência presente); os trechos ficam e ele entra em `v_conteudo_sem_ocorrencia` |
| Limpeza decidida (manual) | `busca.conteudos_para_limpeza(con, carencia_dias)` lista quem está sem ocorrência há mais que a carência; `busca.desindexar_conteudo(con, id)` tira trechos e segmentos (o gatilho limpa o invertido), **recusa** se houver ocorrência presente, e deixa `indice_conteudo.estado='REMOVIDO'` (REGRA 6) |
| Ocorrência de um `REMOVIDO` volta | o conteúdo reentra em `v_reindexar` com motivo `REMOVIDO` |
| Conteúdo muda de bytes | outro `conteudo_id` (seção 6) |

**Remoção física nunca é job automático.** O índice é derivado e
reconstruível, mas a decisão de limpar (e a carência) é da Emmily.

**Rastreabilidade (REGRA 9):** `busca.rastrear(con, trecho_id)` devolve o
trecho com posição, o segmento (tipo, número, rótulo), o conteúdo
(`quick_xor`, tamanho, derivado), as versões de extrator/normalizador/chunker,
a fonte do texto, a `execucao_id`, a data, e **as ocorrências presentes** com
`drive_id`, `item_id`, caminho e `web_url` (teste 14c).

**Saúde:** `SELECT * FROM v_busca_saude` — todos os contadores de defeito em
zero (agora inclui `trecho_de_nao_indexado`), e
`INSERT INTO busca_fts(busca_fts, rank) VALUES('integrity-check', 1)` — o
`rank = 1` não é opcional (sem ele o FTS5 não confere contra `trecho`).

---

## 9. Por que SQLite/FTS5 basta — e qual é o gatilho para sair dele

Inalterado desde 2026-09-27. Um escritor (o worker), muitos leitores, arquivo
único, nada saindo da máquina. Gatilhos medidos para reconsiderar: **p95 de
consulta de dois termos > 1 s** com o índice completo (antes, testar índice de
prefixo); **banco > 50 GB** ou `integrity_check` > 10 min; **necessidade real
de dois escritores**; **cobertura travada por culpa do motor**. Nenhum banco
vetorial: a ordem é frase → operadores → prefixo → sinônimos na consulta → só
então embeddings, com número na mão.

Medido na cópia do banco real migrada: consultas de 1 a 3 termos em ≤ 0,01 s.

---

## 10. A migração — `scripts/05_migrar_v5.py`

### De onde vem o texto: **reconstrução conferida do índice v4**

A versão anterior deste documento mandava reindexar a partir dos derivados.
Medindo, isso **perderia a página**: o derivado `.txt.gz` guarda as páginas
unidas por linha em branco — que também aparece **dentro** da página. Separar
ali seria inventar fronteira. Só a planilha guarda a fronteira (`[aba] Nome`).

O `texto_fts` v4, por outro lado, guarda **cada trecho com o número da
página**, e o corte 1.0 é determinístico: sabendo o tamanho de cada trecho,
sabe-se onde o seguinte começou. Então, por conteúdo:

1. reconstrói o texto de cada página a partir dos trechos v4;
2. **confere**: recortado de novo pela regra 1.0, tem de dar **trecho a trecho**
   o que estava no índice; e unido por `\n\n` tem de dar **byte a byte** o
   derivado;
3. passou → indexa por `chunk/2.0` com página/slide/aba preservados
   (`fonte_texto='indice_v4'`);
4. não passou → o derivado (`fonte_texto='derivado'`; planilha separada por
   aba, o resto vira um segmento `documento` — honesto);
5. sem nenhum dos dois → fica em `v_reindexar` e o relatório diz qual.

**Não migra linha de `texto_fts` como está** (sem `conteudo_id`, sem posição,
sem normalização) e **não apaga o `texto_fts`** — aposentar é passo separado.

### O que o script faz

| Modo | Efeito |
|---|---|
| sem argumento | **simulação**: banco aberto `mode=ro`; contagens de antes, fonte de cada conteúdo, trechos que sairiam (calculados de verdade, em memória) |
| `--aplicar` | 1. toma o **lock do worker** (worker rodando → recusa, código 3) · 2. **backup** pela API de backup do SQLite para `data/backup/<banco>_pre_v5_<data>.db` + `quick_check` **na cópia** · 3. **uma transação** (`isolation_level=None` + `BEGIN IMMEDIATE`; o DDL vai instrução por instrução, porque `executescript()` faz `COMMIT` antes): execução registrada, DDL, versões, `schema_versao='5'`, indexação, conferências (saúde zerada, `integrity-check` rank 1, `foreign_key_check` das tabelas v5, contagens do v4 iguais, versões) — qualquer falha → `ROLLBACK`, **inclusive do DDL** · 4. `quick_check` do banco inteiro |
| de novo | idempotente: nada com fonte na fila → **sem backup**, nenhum trecho reescrito |

### Ensaio na cópia do banco real — 2026-10-04

Cópia por API de backup (conexão `mode=ro`) em `%TEMP%`, config temporária;
apagada no fim.

| | antes | depois |
|---|---:|---:|
| `arquivo` | 142.664 | 142.664 |
| `conteudo` | 30 | 30 |
| `extracao` | 125 | 125 |
| `texto_fts` | 4.361 | 4.361 |
| `evento_arquivo` | 150.210 | 150.210 |
| `item_nuvem` | 162.974 | 162.974 |
| `segmento` | — | 284 |
| `trecho` / `busca_fts` | — | 4.545 / 4.545 |
| `indice_conteudo` | — | 29 (todos `INDEXADO`, todos `fonte_texto='indice_v4'`) |
| `meta.schema_versao` | 4 | 5 |

- 29 de 29 conteúdos reconstruídos do índice v4 e **idênticos ao derivado**; 1
  conteúdo `OCR_REQUIRED` fica fora (sem texto), como deve.
- `v_busca_saude` zerada; `integrity-check` (rank 1) ok; `foreign_key_check`
  ok; `quick_check` ok; `v_reindexar` = 0.
- Tempos: planejamento 3,3 s · backup + `quick_check` da cópia **143 s** (913
  MB) · transação **36,7 s** · `quick_check` final ≈ 130 s (por diferença; 73 s
  medidos na segunda rodada) · **total 315 s**.
- Segunda rodada: "nada a indexar", sem backup, transação 0,4 s, nenhum
  trecho reescrito.

### Comando para o banco real (Lead)

```powershell
# 0. worker parado (o script recusa se o lock estiver tomado)
python scripts\05_migrar_v5.py              # simulação: conferir "29 conteudos {'indice_v4': 29}", "4545"
python scripts\05_migrar_v5.py --aplicar    # ~5 min; +913 MB em data\backup (ver T-19)
```

Enquanto o `worker.indexar()` não for trocado (seção 11), o worker continua
alimentando só o `texto_fts` com o corte 1.0 — e **rodar o `05 --aplicar` de
novo indexa o que chegou** (os conteúdos novos entram em `v_reindexar` como
`NUNCA_INDEXADO` e são reconstruídos do `texto_fts`). O `DROP TABLE texto_fts`
só depois da troca do worker e de uma rodada limpa.

---

## 11. Integração com o worker (edição de `scripts/worker.py`, do Lead)

```python
busca.indexar_resultado(con, conteudo_id: int, resultado: dict, *,
                        execucao_id: int | None = None) -> dict
# -> {"acao": INDEXADO|REINDEXADO|INALTERADO|SEM_TEXTO, "segmentos", "trechos", ...}
```

Onde e como:

1. **Na subida** (`Worker.__init__` ou `executar()`):
   `self.busca_v5 = busca.v5_ativo(con)`; se `True` e
   `busca.conferir_versoes(con)` não vier vazio → recusar subir (mensagem com
   as divergências).
2. **Em `processar()`, passo 4:** remover
   `trechos = self.indexar(arquivo_id, caminho_rel, resultado)` (linha ~500)
   quando `self.busca_v5` — o índice v5 precisa do `conteudo_id`, que só existe
   depois do passo 5.
3. **Em `processar()`, passo 5, logo depois de
   `conteudo_id = self.gravar_conteudo(...)` e antes do `self.con.commit()`:**

   ```python
   if self.busca_v5 and conteudo_id is not None:
       r = busca.indexar_resultado(self.con, conteudo_id, resultado,
                                   execucao_id=self.execucao_id)
       self.con.execute("UPDATE conteudo SET trechos=? WHERE id=?",
                        (r["trechos"], conteudo_id))
       self.con.execute("UPDATE extracao SET trechos=? WHERE arquivo_id=? AND extrator=?",
                        (r["trechos"], arquivo_id, resultado.get("extrator")))
   ```

   Mesma transação do item (REGRA 8); `indexar_resultado` não faz commit. Vale
   para todo `status` com `conteudo_id` (sem texto vira `SEM_TEXTO`).
   O perfil JSON grava `trechos_indexados` antes disso — mover a gravação do
   perfil para depois, ou aceitar 0 lá.
4. **`cobrir_por_conteudo()` e `reeleger_representantes()`: nada.** O conteúdo
   já está indexado; o índice nunca soube de representante.
5. **`texto_fts` deixa de ser alimentado** com v5 ativo (congelado até o `DROP`).
6. `limites.trecho_chars/trecho_sobreposicao` do `config/projeto.yaml` passam a
   servir só para a migração reconstruir o v4; a política v5 é do código
   (`busca.POLITICA`), carimbada em `meta`.

---

## 12. O que os testes provam

`python tests\test_busca.py` → **266 PASS / 0 FAIL**, em bancos temporários
criados pelo `00_init_db.py` (v4 de verdade). O banco real não é tocado.

| # | Prova |
|---|---|
| 1 | DDL v5 dentro de transação (BEGIN+ROLLBACK não deixa objeto), idempotente, versões batem com o código |
| 2 | `norm/1.1`: NFKC, invisíveis, CRLF, hifenização, espaços, **acento solto do LaTeX**, consulta com a mesma dobra, frases FTS5 seguras |
| 3 | cabeçalho corrido sai das páginas; **aba de planilha não perde a linha numérica** |
| 4 | **propriedades do corte** (determinismo byte a byte, cobertura, não vazio, máximo, posições, sobreposição, acento) em 8 textos × 3 políticas; início em palavra; fim de frase/parágrafo; texto vazio; parâmetros inválidos recusados |
| 5–7 | 1 conteúdo = 1 indexação; 3 ocorrências no resultado; `p. 12`, `slide 3`, nome da aba, `documento` |
| 8 | **o trecho nunca cruza a página**; os trechos do banco reconstroem cada página |
| 9–10 | reindexação substitui sem acumular; texto igual não reindexa; versão divergente é acusada |
| 11–13b | `MISSING`; acento/caixa/`m²`/`calibracao`; saúde zerada; FK protege a ordem |
| 14 | **manutenção**: bytes mudam → outro conteúdo; remoção só sem ocorrência e fora da carência, com volta via `REMOVIDO`; **rastreabilidade** trecho → conteúdo → ocorrências presentes |
| 15 | `indexar_resultado` (entrada do worker), `SEM_TEXTO` idempotente |
| 16 | **`05_migrar_v5.py` ponta a ponta**: simulação não escreve um byte; falha no meio → ROLLBACK **até do DDL**; aplicação com backup (quick_check ok), fontes `indice_v4`/`derivado`, páginas/abas/slides preservados, sem-fonte fica em `v_reindexar`, `MISSING` e era local fora do resultado; segunda rodada idempotente sem backup; **worker com lock → migração recusa** |
| 17 | `data/cerebro.db` intacto |

---

## 13. Riscos e o que ficou em aberto

| | |
|---|---|
| 1. **56 ocorrências presentes com texto e sem `conteudo_id`** (era local, 161 trechos v4) | ficam fora do v5 até alguém criar o `conteudo` delas (backfill do v4 — fronteira do agente do schema/`conteudo_id`). Até lá só o `texto_fts` as acha |
| 2. **609 arquivos suportados vivos sem `quick_xor`** (medido 2026-10-04; 17 deles alcançáveis pela fila da nuvem, `v_fila_nuvem`) | sem hash não há `conteudo`, e sem `conteudo` o v5 não indexa. **Até o R4 o worker NÃO resolvia isso**: a `FonteGraph` só calculava o quickXor do baixado quando o catálogo já tinha um para comparar, e o item sem hash virava `COMPLETED` sem `conteudo_id` — invisível à busca v5. **Desde o R4** (`fonte.FonteGraph.obter`) o hash é calculado sempre: com hash no catálogo, valida; sem, o calculado vira o `quick_xor` do item e a chave do `conteudo` (`processar()`, passo 2b, que também cobre a cópia sem hash como `DUPLICATE`). Prova: `tests/test_worker_robustez.py`, parte C. **Pendente fora desta camada:** `17_aplicar_delta.alterados()` compara `IFNULL(a.quick_xor,'~') <> IFNULL(i.quick_xor,'~')`; com o hash calculado no catálogo e o espelho (`item_nuvem`) ainda sem hash, o item seria marcado `CHANGED` a cada sync. A comparação precisa ignorar o lado do espelho quando ele é `NULL` (o tamanho continua detectando mudança) |
| 3. Derivado sem fronteira de página | reindexar **a partir do derivado** perde a página de PDF/PPTX. Sugestão: o worker gravar as páginas separadas por `\f` (form feed), ou um JSON de deslocamentos no perfil — decisão de formato do derivado, não desta camada |
| 4. Ranking dominado por planilha gigante | as 3 consultas fora do 1º lugar perdem para o conteúdo 12 (3.664 trechos). Medir de novo com o índice cheio antes de mexer (seção 9) |
| 5. Amostra pequena | 29 conteúdos, um deles com 80% dos trechos. A decisão se apoia mais nas propriedades (meio de palavra, frases partidas, linhas) do que no acerto@k, que empatou |
| 6. Backup de 913 MB por aplicação | soma ao `T-19` (`data/backup/` sem poda) |
| Aposentar `texto_fts` (`DROP`) | Lead/Emmily, depois da troca do worker e de uma rodada limpa |
| PDFs `OCR_REQUIRED` | fora da busca até haver OCR |
| Índice de prefixo | só depois do p95 medido com índice cheio |
