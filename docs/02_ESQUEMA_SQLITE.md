# Esquema do banco — v4

Arquivos executáveis: [`sql/schema_v2.sql`](../sql/schema_v2.sql) (base),
[`sql/schema_v3.sql`](../sql/schema_v3.sql) (a nuvem) e
[`sql/schema_v4.sql`](../sql/schema_v4.sql) (conteúdo × ocorrência), aplicados
por `00_init_db.py` (banco novo já nasce em v4), `03_migrar_v3.py` e
`04_migrar_v4.py`. Todos idempotentes; as migrações fazem **backup** antes de
mexer em estrutura (e só então) e nunca rebaixam `schema_versao` — ver
*Evolução do schema*.

> **Identidade canônica: `(drive_id, item_id)`.** Caminho e nome são atributos
> mutáveis. `caminho_rel` **não** é `UNIQUE` (DEC-015): renomear um arquivo e
> criar outro com o nome antigo é um evento legítimo, e o banco tem de aceitar.
> Quem garante unicidade é `ux_arquivo_item (drive_id, item_id) WHERE item_id IS NOT NULL`.

## A receita recria a produção — e isso é testado (CR-1)

O schema de produção está versionado, normalizado, em
[`sql/schema_producao_v4.snapshot.sql`](../sql/schema_producao_v4.snapshot.sql),
gerado por `scripts/20_snapshot_schema.py` (que abre o banco **sempre** em modo
`ro`). `tests/test_schema.py` exige que:

- `00_init_db` num diretório vazio produza **exatamente** esse snapshot —
  colunas na mesma **ordem**, tipo, `NOT NULL`, `DEFAULT`, FKs, índices e views;
- `03` + `04` rodados por cima não mudem nada (e não rebaixem `schema_versao`);
- um banco **v2 legado** (com o `UNIQUE(caminho_rel)` antigo) migrado por
  `03` + `04` chegue ao mesmo snapshot, sem perder linha;
- a reconstrução do `03` seja atômica: falhou, `ROLLBACK`, a tabela antiga fica;
- reintroduzir o `UNIQUE` ou remover um índice **faça o teste falhar**
  (controle negativo dentro do próprio teste).

Conferir produção contra o snapshot (somente leitura):

```
python scripts\20_snapshot_schema.py --snapshot sql\schema_producao_v4.snapshot.sql
```

Diferença conhecida até a próxima aplicação de `04_migrar_v4.py` no banco real:
o índice `ix_arquivo_quickxor`, idêntico a `ix_arquivo_qxh` (M-12). A decisão
foi **manter `ix_arquivo_qxh`** (é o que a receita cria) e remover o duplicado —
`sql/schema_v4.sql` tem o `DROP INDEX IF EXISTS`, então reaplicar o v4 alinha a
produção. Fora isso, produção = receita.

Ordem das colunas de `arquivo`: as 33 do v2, depois as 16 de nuvem **na ordem em
que o `ALTER TABLE` as pôs em produção** (`drive_id, item_id, parent_id, etag,
ctag, quick_xor, web_url, criado_por, modificado_por, criado_em_nuvem,
modificado_em_nuvem, presente_nuvem, origem, visto_nuvem_em, caminho_anterior,
caminho_nuvem`) e por último `conteudo_id`. `presente_nuvem` é
`INTEGER NOT NULL DEFAULT 0` e `origem` é `TEXT NOT NULL DEFAULT 'local'` —
linha ainda não reconciliada é `0`/`'local'`, nunca `NULL`. Nenhum código usa
`INSERT ... VALUES` posicional em `arquivo` (verificado); as leituras são por
nome (`sqlite3.Row`).

Sem chave estrangeira em `arquivo.conteudo_id` (entrou por `ALTER TABLE`; criar a
FK exigiria reconstruir a tabela de produção). O ponteiro quebrado é vigiado por
`cerebro_util.checar_invariantes()` → `conteudo_id_inexistente` (M-16).

## Conteúdo × ocorrência (v4)

| Tabela / view | Uma linha é… | Papel |
|---|---|---|
| `conteudo` | os bytes, por `(quick_xor, tamanho_bytes)` | extraído **uma** vez; tem derivado, versão do extrator e `representante_id` |
| `arquivo.conteudo_id` | o vínculo de cada ocorrência | N ocorrências → 1 conteúdo |
| `v_ocorrencia` | uma ocorrência **viva** de um conteúdo | "este texto está nestes N lugares" |
| `v_cobertura_conteudo` | um estado de conteúdo | quanto do acervo é pesquisável |
| `v_conteudo_orfao` | conteúdo com representante nulo/ausente | consumida pela reeleição |
| `v_fila_nuvem` | um item que o worker da nuvem pode pegar | exige `drive_id`, `item_id`, `presente_nuvem=1` |

Regras (detalhe em [`07_POLITICA_DUPLICATAS.md`](07_POLITICA_DUPLICATAS.md)):

- `conteudo.ocorrencias` = ocorrências **vivas** (`presente=1`) — a mesma conta
  de `v_ocorrencia` e da consulta 3 de `sql/consultas_busca.sql`;
- ocorrência que vira `MISSING` **mantém** `conteudo_id` (histórico, DEC-029);
  ocorrência cujos bytes mudaram **perde** `conteudo_id` (o antigo vai no evento);
- os trechos de `texto_fts` ficam sob o `arquivo_id` do representante e mudam de
  dono quando ele muda — nunca são apagados por isso.

## Tabelas da nuvem (v3)

| Tabela | Uma linha é… | Papel |
|---|---|---|
| `drive` | uma biblioteca do site | o que inventariar |
| `delta_token` | o checkpoint de um drive | `next_link` (retomada) e `delta_link` (incremental) |
| `item_nuvem` | um item do SharePoint | **a verdade da nuvem**, com o `quick_xor` do servidor |
| `evento_nuvem` | uma mudança vista no delta | novo · renomeado · movido · alterado · removido · reapareceu |
| `reconciliacao` | uma decisão de vínculo | método, confiança e candidatos — inclusive as **não** aplicadas |

Views novas: `v_nuvem_resumo`, `v_duplicata_nuvem` (duplicata exata **sem baixar
nada**), `v_excecoes_reconciliacao` e `v_migracao`.

Em `arquivo`, 16 colunas de nuvem (ordem acima), entre elas `caminho_nuvem`
(o caminho atual no drive) e `caminho_anterior` (de onde veio no último move).
`caminho_rel`/`pasta_rel` acompanham o move pelo `17_aplicar_delta` (A-6).

```sql
-- o que ainda não tem identidade da nuvem
SELECT COUNT(*) FROM arquivo WHERE item_id IS NULL AND presente = 1;
-- o que ficou para revisão humana
SELECT * FROM v_excecoes_reconciliacao LIMIT 20;
-- duplicatas, sem baixar um byte
SELECT * FROM v_duplicata_nuvem LIMIT 20;
```

---

## Base (v2)

---

## Tabelas

| Tabela | Uma linha é… | Papel |
|---|---|---|
| `meta` | uma configuração do banco | versão do schema, raiz indexada |
| `execucao` | uma rodada de scan/classificação/worker | rastreabilidade |
| `pasta` | uma pasta da árvore | classificação P0..P4 **com o motivo** |
| `arquivo` | um caminho relativo | **o inventário + a máquina de estados** |
| `evento_arquivo` | uma mudança observada | histórico: novo, alterado, sumiu, duplicata, falha, retomado |
| `progresso_diretorio` | uma pasta já varrida | retomada da varredura |
| `extracao` | um arquivo × um extrator | resultado da leitura do conteúdo |
| `texto_fts` | um trecho de texto | busca (FTS5, sem acento, com página) |
| `controle` | **uma única linha** | o worker obedece a ela: RUN / PAUSE / STOP |
| `checkpoint` | uma foto do progresso | auditoria e retomada |
| `log` | um evento de execução | diagnóstico (espelha `logs/*.jsonl`) |

---

## Máquina de estados (`arquivo.estado`)

```
                        (scanner)
                            │
                            ▼
                      INVENTORIED ──(classificador)──┐
                            │                        │
              tem extrator  │                        │ não tem
                            ▼                        ▼
                         QUEUED                  UNSUPPORTED
                            │                  (fica no catálogo;
       só na nuvem e        │                   volta à fila quando
       não pode baixar ─────┼──► WAITING_HYDRATION   o extrator existir)
                            │         │
                            ▼         │ (backoff)
                        HYDRATING ◄───┘
                            │
                            ▼
                          READY ──► EXTRACTING ──► COMPLETED
                                         │           PARTIAL
                                         │           OCR_REQUIRED
                                         │           DUPLICATE
                                         │
                                         ├──► ERROR_RETRYABLE ──(N vezes)──► ERROR_FINAL
                                         │
   arquivo mudou no disco ──► CHANGED ───┘
   arquivo sumiu          ──► MISSING   (presente=0, linha preservada)
   temporário (~$, .tmp)  ──► SKIPPED
```

**Quanto falta** = `QUEUED + CHANGED + WAITING_HYDRATION + ERROR_RETRYABLE`
= exatamente o que `v_fila` devolve.

Se o processo morrer, `HYDRATING` / `EXTRACTING` / `EXTRACTED` voltam para
`QUEUED` na próxima subida (`cerebro_util.ESTADOS_ORFAOS`).

---

## `arquivo` — as colunas que importam

| Coluna | Por que existe |
|---|---|
| `caminho_rel` | chave natural, relativa à raiz |
| `biblioteca`, `pasta_rel`, `profundidade` | agrupar e priorizar sem quebrar string em SQL |
| `categoria` | `pdf`/`docx`/`xlsx`/… pela extensão, sem abrir o arquivo |
| `suportado` | 1 = existe extrator hoje (define quem entra na fila) |
| `nome_normalizado` | sem acento, sem `rev 02`, sem `(1)` → agrupa variantes |
| `online` | **1 = só na nuvem.** Ler = baixar. Governa a hidratação |
| `fixado` / `hidratado_por_nos` | quem pediu o arquivo local: o usuário ou nós |
| `temporario` | `~$`, `.tmp`, `desktop.ini` |
| `prioridade` | 0..4, vinda de `pasta` |
| `estado` | a máquina de estados acima |
| `sha256` / `duplicata_de` | identidade do conteúdo; duplicata exata e "movido" |
| `tentativas`, `ultimo_erro`, `proxima_tentativa_em` | política de retry com backoff |
| `presente` | 0 = sumiu do disco. **Nunca apagamos linha** |
| `primeira_vez_em`, `ultima_vez_em`, `alterado_em`, `processado_em` | linha do tempo |
| `ultima_execucao_id` | qual execução viu por último |

---

## Views

| View | Responde |
|---|---|
| `v_fila` | o que processar agora: prioridade → já local → menor primeiro |
| `v_progresso` | quantos arquivos em cada estado (a resposta de "quanto falta") |
| `v_cobertura` | por categoria de arquivo: concluídos, OCR, sem extrator, erro |
| `v_cobertura_prioridade` | o mesmo por P0..P4 |
| `v_duplicata` | grupos com o mesmo `sha256` |
| `v_familia_versao` | mesmo `nome_normalizado` → **candidatos** a versões |

---

## Consultas úteis

```sql
SELECT * FROM v_progresso;                       -- quanto falta
SELECT * FROM v_cobertura_prioridade;            -- P0 primeiro
SELECT * FROM v_fila LIMIT 20;                   -- os próximos da fila

-- por que esta pasta é P0?
SELECT prioridade, obra, categoria_doc, motivo FROM pasta
WHERE caminho_rel LIKE '%Notifica%';

-- busca textual, com página
SELECT a.caminho_rel, f.pagina,
       snippet(texto_fts, 4, '[', ']', '…', 12) AS trecho
FROM texto_fts f JOIN arquivo a ON a.id = f.arquivo_id
WHERE texto_fts MATCH 'medicao NEAR/8 glosa'
ORDER BY bm25(texto_fts) LIMIT 20;

-- o que mudou nos últimos 7 dias
SELECT e.evento, a.caminho_rel, e.em
FROM evento_arquivo e JOIN arquivo a ON a.id = e.arquivo_id
WHERE e.em > datetime('now','-7 days') ORDER BY e.em DESC;

-- erros que precisam de olho humano
SELECT caminho_rel, tentativas, ultimo_erro FROM arquivo
WHERE estado = 'ERROR_FINAL';
```

---

## Evolução do schema

`meta.schema_versao` guarda a versão (hoje `4`). Mudança = novo arquivo
`sql/schema_vN.sql` + migração idempotente + **snapshot regenerado** a partir de
uma cópia de produção migrada:

```
python scripts\20_snapshot_schema.py --banco <copia_migrada.db> --saida sql\schema_producao_v4.snapshot.sql --nota "procedencia: ..."
```

e `tests/test_schema.py` verde. **Nunca** editar o v2 em cima de um banco já
populado. Mudança de schema que o snapshot não reflita faz o teste falhar — de
propósito.

**Migração antiga rodada num banco mais novo (R10).** Nenhuma migração
rebaixa `schema_versao`: o `03` e o `04` só gravam a sua versão se o banco
estiver **abaixo** dela. O `04` num banco v4 ou v5 só reaplica o que é
idempotente do `schema_v4.sql` (DROP/CREATE das views do v4, `DROP INDEX IF
EXISTS ix_arquivo_quickxor`, `CREATE INDEX IF NOT EXISTS`) e mantém a versão;
as views do v4 não são redefinidas pela v5, então nada da v5 é desfeito.
**Backup só quando há mudança estrutural** (falta a tabela `conteudo` ou a
coluna `arquivo.conteudo_id`) — antes, o `04` rodado por engano num banco v5
fazia backup de ~1 GB e gravava `'4'`. Teste: `tests/test_schema.py` §7.
