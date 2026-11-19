# 11 — Auditoria de QA (Agente E)

**Data:** 2026-09-27 · **Alvo:** commit `100a39e` (E8) · **Banco:** `data/cerebro.db`, schema v4, 139.908 linhas, lido **somente** em modo `ro`.

Nada foi alterado: nem código, nem teste, nem `STATE.md`, nem o banco. O worker não foi
executado, a fila não foi processada, nenhum commit foi feito. Outros agentes estavam
alterando `scripts/` durante a auditoria; tudo que está aqui foi verificado contra uma
cópia fiel de `100a39e` extraída com `git archive`, fora da árvore de trabalho.

**Placar: 2 CRÍTICOS · 8 ALTOS · 17 MÉDIOS · 9 BAIXOS · 4 suspeitas não confirmadas.**
**Nenhum crítico impede o processamento em massa de começar.** Os dois críticos são
sobre *sobrevivência do que já existe*, não sobre a fila.

---

## Como ler isto

Cada achado tem **a consulta e o número que ela devolveu**, ou **arquivo e linha**.
Quando escrevi "reproduzi", quer dizer que rodei o código de `100a39e` num banco
descartável e o resultado está transcrito. Suspeita sem prova está na última seção.

---

# CRÍTICO

## CR-1 — O repositório não sabe recriar o banco de produção. São três defeitos no único caminho que existe.

**O que é.** `00_init_db.py` + `03_migrar_v3.py` + `04_migrar_v4.py` são a única receita
para reconstruir o banco. Ela produz uma tabela `arquivo` **diferente** da que está em
produção, e o script que deveria corrigir isso mente dizendo que já corrigiu.

**Evidência — o diff mecânico.** Criei um banco do zero com a receita do repositório e
comparei objeto por objeto com `data/cerebro.db`:

| | banco real | banco novo pela receita |
|---|---|---|
| `arquivo.caminho_rel` | `TEXT NOT NULL` | **`TEXT NOT NULL UNIQUE`** |
| `arquivo.presente_nuvem` | `INTEGER NOT NULL DEFAULT 0` | `INTEGER` (aceita NULL) |
| `arquivo.origem` | `TEXT NOT NULL DEFAULT 'local'` | `TEXT` (aceita NULL) |
| `v_fila` | expõe `drive_id, item_id` | **não expõe** (é a view do v2) |
| `ix_arquivo_quickxor` | existe | não existe |
| ordem das colunas de `arquivo` | nuvem no fim (ALTER) | nuvem no meio |

O `UNIQUE(caminho_rel)` é uma **regressão direta da `DEC-015`** ("`item_id` passa a ser a
identidade; `caminho_rel` vira atributo"). Num banco recriado, renomear `A` para `B` e
depois criar um novo `A` — o evento que a `DEC-015` existe para permitir — estoura
`IntegrityError` no `01_scan_inventario`/`13_reconciliar`.

**Defeito 1 — `caminho_unico()` nunca detecta o UNIQUE, e o script declara sucesso.**
`scripts/03_migrar_v3.py:203` procura a frase literal com **um** espaço:

```python
return bool(sql) and "caminho_rel TEXT NOT NULL UNIQUE" in (sql[0] or "")
```

mas `sql/schema_v2.sql:96` escreve a coluna alinhada com **oito** espaços
(`caminho_rel        TEXT NOT NULL UNIQUE`). A substring nunca casa.

Reproduzido com o script exatamente como está em `100a39e`, sobre um banco v2 novo:

```
  UNIQUE(caminho_rel) ja removido          <- FALSO
  CONFERENCIA: 1 arquivos (antes 1) | ...
  schema agora        v3
returncode: 0
>> UNIQUE(caminho_rel) ainda presente? True
>> meta.schema_versao: ['3']
```

O script sai **0**, grava `schema_versao='3'` e o `UNIQUE` continua lá. É o pior tipo de
falha: silenciosa e auto-declarada como sucesso.

**Defeito 2 — se a detecção for corrigida, a reconstrução quebra.** `COLUNAS_NUVEM`
(`03_migrar_v3.py:37-54`) tem 16 colunas, incluindo `caminho_anterior`, acrescentada no
E8 ("Também alinhei `03_migrar_v3.py`…", `STATE.md`). `TABELA_NOVA` (`:140-192`) declara
só 15 — **`caminho_anterior` ficou de fora**. O passo 4 monta a lista de colunas a partir
da tabela viva (`:284`), que já foi alterada no passo 2. Reproduzido com `caminho_unico()`
corrigido:

```
sqlite3.OperationalError: table arquivo_v3 has no column named caminho_anterior
returncode: 1
>> meta.schema_versao: ['2']
```

**Defeito 3 — se o defeito 2 for corrigido, o bloco explode depois do `DROP TABLE`.**
`03_migrar_v3.py:287` abre `BEGIN IMMEDIATE`, mas as instruções seguintes são DDL
(`DROP VIEW`, `CREATE TABLE`, `DROP TABLE`, `ALTER TABLE`), e o driver `sqlite3` em modo
legado faz commit implícito antes de cada uma. Quando chega em `con.execute("COMMIT")`
(`:301`) não há mais transação. Reproduzido com a versão da árvore de trabalho, que já
tem os defeitos 1 e 2 corrigidos por outro agente:

```
File "scripts/03_migrar_v3.py", line 328, in main
    con.execute("COMMIT")
sqlite3.OperationalError: cannot commit - no transaction is active
During handling of the above exception, another exception occurred:
    con.execute("ROLLBACK")
sqlite3.OperationalError: cannot rollback - no transaction is active
```

Ou seja: a tabela antiga **já foi apagada** (commit implícito), o `foreign_key_check`
(`:307`) nunca roda, `meta.schema_versao` nunca vira `3`, e o usuário vê um traceback
duplo. O único passo destrutivo do projeto termina sem nenhuma das conferências que ele
promete.

**Consequência colateral — os testes rodam no schema errado.** `test_aplicar_delta.py:92-93`
e `test_worker_nuvem.py:143` montam o banco com `00_init_db.py` (+ `03_migrar_v3.py`).
Por causa do defeito 1, **toda a suíte de 327 checagens roda contra uma tabela `arquivo`
com `UNIQUE(caminho_rel)` e `presente_nuvem` anulável** — não contra o schema de produção.
Nenhum teste exercita reuso de caminho, então ninguém percebeu.

**Consequência colateral — a promessa do `.gitignore` é falsa.** O `.gitignore` justifica
excluir `data/*.db` e `derivados/` com "São 100% reconstruíveis a partir do SharePoint +
scripts". Não são: (a) a receita não reproduz o schema; (b) o acervo de origem já perdeu
**5.941 arquivos** (o Parceiro, `T-06`), então 4 `COMPLETED`, parte das 125 extrações e dos
4.361 trechos **não podem mais ser reconstruídos de fonte nenhuma**. O único exemplar
desses derivados está em `data/`, `derivados/` e `data/backup/` — os três fora do Git,
numa máquina só.

**É regressão?** Sim, da `DEC-015` e da `DEC-024` (que registrou exatamente esta deriva:
"o schema já existia no banco, aplicado por outra sessão sem deixar script"). A `DEC-024`
foi fechada extraindo o `schema_v3.sql`, mas o caminho de *aplicação* continuou quebrado.
A prova é a própria DDL do banco real: `CREATE TABLE "arquivo"` (com aspas, marca de
`ALTER TABLE ... RENAME TO`) e com o texto do `schema_v2.sql` menos a palavra `UNIQUE` —
layout que **nenhum script do repositório gera**. A tabela em produção foi construída por
código que não está versionado.

**Correção sugerida.** (1) Trocar a detecção por regex tolerante a espaço — já feito na
árvore de trabalho; (2) gerar `TABELA_NOVA` a partir de `PRAGMA table_info` em vez de um
DDL fixo, ou no mínimo acrescentar `caminho_anterior` e `conteudo_id`; (3) trocar
`BEGIN/COMMIT` manual por `with con:` ou `con.isolation_level=None` com `savepoint`;
(4) extrair o `arquivo` de produção para `sql/schema_v4.sql` (como a `DEC-024` fez com o
v3) e acrescentar um teste que compare `sqlite_master` do banco recriado com o esperado;
(5) versionar uma cópia de segurança dos derivados dos itens que o SharePoint já perdeu.

---

## CR-2 — 45 duplicatas apontam para um representante que sumiu, e a reeleição não consegue vê-las. Nunca serão consertadas.

**O que é.** 45 ocorrências têm `duplicata_de` apontando para uma linha com `presente=0`.
As 45 têm `conteudo_id IS NULL` (são da era `sha256`/`DEC-004`, anteriores ao v4), e por
isso **não aparecem em `v_conteudo_orfao`** — que só olha a tabela `conteudo`. Logo
`worker.reeleger_representantes()` (`worker.py:362-389`), que consome essa view, é
estruturalmente incapaz de reelegê-las.

**Evidência.**

```sql
SELECT COUNT(*) FROM arquivo a JOIN arquivo b ON b.id=a.duplicata_de
 WHERE b.presente=0;                                            -- 45
SELECT a.conteudo_id IS NULL, COUNT(*) FROM arquivo a
  JOIN arquivo b ON b.id=a.duplicata_de WHERE b.presente=0 GROUP BY 1;
                                                                -- (1, 45)  todas sem conteudo_id
SELECT b.presente, b.estado, COUNT(*) FROM arquivo a JOIN arquivo b ON b.id=a.duplicata_de
 WHERE a.estado='DUPLICATE' AND a.conteudo_id IS NULL GROUP BY 1,2;
                                                -- (0,'MISSING',41) · (1,'COMPLETED',37)
SELECT COUNT(*) FROM v_conteudo_orfao;                          -- 0
```

41 delas estão em `DUPLICATE`, que é **estado terminal** (`cu.ESTADOS_TERMINAIS`): não
entram em `v_fila`, não entram em `v_fila_nuvem`, o worker nunca as oferece. Distribuição:

```sql
SELECT a.biblioteca, COUNT(*) FROM arquivo a JOIN arquivo b ON b.id=a.duplicata_de
 WHERE a.conteudo_id IS NULL AND b.presente=0 GROUP BY 1;
-- Programa - 3. Desenvolvimento      27
-- Programa - 5. Compartilhado Parceiro    10
-- Programa - 2. Compartilhado interno  4
-- Programa - 6. Arquivos Parceiro          4
```

**Cenário de falha (já ocorrido).** Em 2026-09-22 o `17_aplicar_delta` marcou 5.937 itens
como `MISSING`. Entre eles havia representantes de duplicatas legadas. Cada duplicata
ficou com `duplicata_de` → linha morta e sem nenhum `conteudo_id` para ancorá-la. O texto
dessas ocorrências só existe no índice sob o `arquivo_id` do representante morto:

```sql
SELECT COUNT(DISTINCT b.id) FROM arquivo a JOIN arquivo b ON b.id=a.duplicata_de
 WHERE b.presente=0 AND EXISTS(SELECT 1 FROM texto_fts f WHERE f.arquivo_id=b.id);  -- 2
```

E `sql/consultas_busca.sql` consulta 2 faz `JOIN conteudo c ON c.id = extraido.conteudo_id`
— `INNER JOIN`. Com `conteudo_id` nulo dos dois lados, **a busca não devolve nem o
representante morto nem as 45 cópias vivas**. O conteúdo está catalogado, o texto está
extraído, e ninguém o encontra.

**É regressão?** Sim, da `DEC-027` ("nenhuma cópia some do catálogo, e a busca devolve
todos os lugares") e da `REGRA 6`. A `DEC-029` prometia que `MISSING` preserva "vínculos";
o vínculo foi preservado como ponteiro, mas sem utilidade.

**Correção sugerida.** Um passo de backfill que, para todo `arquivo` com `duplicata_de`
não nulo e `conteudo_id` nulo, crie/associe a linha de `conteudo` a partir do
`(quick_xor, tamanho_bytes)` do representante — e depois rode `reeleger_representantes()`.
Complementarmente, ampliar `v_conteudo_orfao` (ou criar `v_duplicata_orfa`) para enxergar
o caminho legado por `duplicata_de`, de modo que o problema não possa reaparecer em
silêncio.

---

# ALTO

## A-1 — Representante que vira `MISSING` no meio da rodada deixa conteúdo órfão, contador divergente e índice sujo — e nada conserta isso até a próxima subida do worker.

**O que é.** `reeleger_representantes()` só é chamado em `worker.rodar()` (`worker.py:636`),
na subida. `marcar_ausente()` (`:217-238`) e `17_aplicar_delta.marcar_sumidos()`
(`17_aplicar_delta.py:85-99`) põem `presente=0` **sem** recontar `conteudo.ocorrencias`,
sem reeleger e sem limpar `texto_fts`.

**Evidência — reproduzido com o código de `100a39e`**, Graph falso, dois arquivos com os
mesmos bytes (representante + cópia), depois um 404 no representante:

```
[estado saudavel]              v_conteudo_orfao=0  contador!=v_ocorrencia=0  trechos_mortos=0
reprocessa o representante e o Graph responde 404: ausente
[logo apos o 404, DENTRO da rodada]
                               v_conteudo_orfao=1  contador!=v_ocorrencia=1 [(id=1, contador=2, vivas=1)]
                               trechos_apontando_p/_presente=0=1
conteudo.representante_id=1 (arquivo #1, que esta presente=0/MISSING)
```

E o mesmo pelo lado do delta, com a fixture **idêntica à do `test_aplicar_delta`**:

```
[antes]                          v_conteudo_orfao=0  contador!=v_ocorrencia=0  trechos_mortos=0
[depois de marcar_sumidos]       v_conteudo_orfao=1  contador!=v_ocorrencia=1 [(1,1,0)]  trechos_mortos=1
```

**Cenário concreto.** É exatamente o que aconteceu na prova do E7: 6 itens deram 404
durante a rodada. Com 61.337 itens na fila, 7.400 já `MISSING` e o delta rodando em
paralelo, isto ocorrerá centenas de vezes por sessão. Entre o 404 e a próxima subida do
worker, `v_ocorrencia` e a busca respondem com dados de um arquivo que não existe mais, e
`conteudo.ocorrencias` fica acima da realidade.

**Estado hoje no banco real:** `v_conteudo_orfao = 0` e contador divergente `= 0`. Isso é
**sorte**, não garantia: nenhum dos 4 `COMPLETED` que viraram `MISSING` no E8 tinha
`conteudo_id` (todos são dos 90 legados — ver M-4). O índice já sujou:

```sql
SELECT COUNT(*) FROM texto_fts f JOIN arquivo a ON a.id=f.arquivo_id WHERE a.presente=0;  -- 6
SELECT COUNT(DISTINCT f.arquivo_id) ... ;                                                  -- 4
```

**É regressão?** Sim, da `DEC-032` ("trocar de conteúdo reconta os dois lados… um contador
que diverge de `v_ocorrencia` é bug"). O worker reconta nos dois lados quando a *troca* é
por conteúdo novo, mas não quando a ocorrência **some**.

**Correção sugerida.** Chamar `recontar_ocorrencias()` + `reeleger_representantes()` dentro
de `marcar_ausente()` e de `marcar_sumidos()`, na mesma transação (`REGRA 8`), e apagar as
linhas de `texto_fts` do representante morto (ou passar a indexar por `conteudo_id`).

---

## A-2 — 592 itens ficam na fila para sempre, e o worker diz "FILA_VAZIA" com 65.384 pendentes no próprio checkpoint.

**Evidência.**

```sql
SELECT (SELECT COUNT(*) FROM v_fila)-(SELECT COUNT(*) FROM v_fila_nuvem);          -- 592
SELECT estado, biblioteca, COUNT(*) FROM arquivo WHERE presente=1 AND presente_nuvem=0
  AND suportado=1 AND estado IN ('QUEUED','CHANGED','READY','ERROR_RETRYABLE','WAITING_HYDRATION')
  GROUP BY 1,2;
-- ('QUEUED', 'Coordenacao Programa - 02 Repositorio', 592)
```

São os arquivos fora do escopo pela `DEC-017`. Eles satisfazem `v_fila` (que não exige
`presente_nuvem`) mas não `v_fila_nuvem` (que exige, `DEC-030`). O worker em modo `graph`
nunca os pega, e `restam_em_espera()` (`worker.py:192-195`) só conta
`WAITING_HYDRATION`/`ERROR_RETRYABLE` — não conta `QUEUED`. Resultado: o worker encerra
com `FILA_VAZIA` enquanto o seu próprio `checkpoint` grava o número de `v_fila`:

```sql
SELECT id, estado_worker, concluidos, pendentes, observacao FROM checkpoint ORDER BY id DESC LIMIT 1;
-- (16, 'FILA_VAZIA', 121, 65384, 'nao ha mais nada para processar')
```

**Cenário de falha.** `99_status.py:93` imprime `>> FILA PRONTA` = `COUNT(v_fila)` = 61.929
e `:137-139` calcula a previsão de horas dividindo esse número pela taxa. A previsão que
vai embasar a decisão sobre os parâmetros do processamento em massa está inflada em 592
itens que jamais serão processados — e a `DEC-017` diz que "fora do escopo **não é**
ausente", então marcá-los `MISSING` seria errado.

**É regressão?** Da `REGRA 7` ("estado explícito, nunca inferência — 'quanto falta?' se
responde com `SELECT * FROM v_progresso` ou `v_fila`"). A `v_fila` responde errado.

**Correção sugerida.** Criar um estado explícito (`OUT_OF_SCOPE`) ou excluir
`presente_nuvem=0` de `v_fila`, e fazer `checkpoint()`/`99_status` usarem `v_fila_nuvem`
quando a fonte é `graph`. Hoje `worker.checkpoint()` (`worker.py:134`) usa `v_fila` fixo.

---

## A-3 — O teto de extração é aplicado **depois** do download: 74 itens, 39 GB, baixados inteiros para virar `PARTIAL` sem uma linha de texto.

**O que é.** Em `worker.processar()` a ordem é: `obtido = self.fonte.obter(item)`
(`worker.py:417`, baixa tudo) → só então `elif (item["tamanho_bytes"] or 0) > teto_mb *
1024**2` (`:467`) devolve `{"status": "parcial", "erro": "arquivo acima de 300 MB:
extracao adiada"}`. `FonteGraph.obter` só recusa acima de `tamanho_max_download_gb`
(padrão 2 GB, `fonte.py:67`), nunca os 300 MB de `worker.tamanho_max_extracao_mb`.

**Evidência.**

```sql
SELECT COUNT(*), ROUND(SUM(tamanho_bytes)/1024.0/1024/1024,1) FROM v_fila_nuvem
 WHERE tamanho_bytes > 314572800;        -- 74 itens, 39 GB
SELECT COUNT(*) FROM v_fila_nuvem WHERE tamanho_bytes > 2147483648;   -- 0 (o teto de 2 GB nunca dispara)
```

**Cenário de falha.** Na velocidade medida no E7 (~1,1 MB/s), 39 GB são **~10 horas** de
download puro para produzir zero texto. Pior: `PARTIAL` está em `cu.ESTADOS_TERMINAIS`, ou
seja, esses 74 saem da fila e ninguém volta neles — o download foi gasto e perdido.

**Contexto de planejamento** (a decisão que está na mesa):

```sql
SELECT ROUND(SUM(tamanho_bytes)/1024.0/1024/1024,1) FROM v_fila_nuvem;                    -- 220,6 GB
SELECT ROUND(SUM(t)/1024.0/1024/1024,1) FROM (SELECT MIN(tamanho_bytes) t FROM v_fila_nuvem
  GROUP BY quick_xor, tamanho_bytes);                                                     -- 138,3 GB
SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM v_fila_nuvem WHERE quick_xor IS NOT NULL
  GROUP BY quick_xor, tamanho_bytes HAVING COUNT(*)>1);                                   -- 25.513 cópias
```

A deduplicação deve poupar 25.513 downloads e ~82 GB — mas os 39 GB do teto de extração
são desperdício puro.

**Correção sugerida.** Mover a checagem de `tamanho_max_extracao_mb` para **antes** de
`fonte.obter()` e marcar o item com um estado próprio e retomável (não `PARTIAL`
terminal), para quando houver extrator de arquivo grande.

---

## A-4 — Não existe trava de processo único: dois workers na mesma máquina se atropelam, e um deles apaga o download em curso do outro.

**O que é.** A guarda é `worker.py:776`:

```python
if controle["estado"] == "RODANDO" and cu.processo_vivo(controle["pid"]):
```

Três buracos, todos reais:

1. **TOCTOU.** Entre ler `controle` e escrever `estado='RODANDO'` (`worker.py:626`) há a
   abertura da execução, do log e do `Worker`. Dois processos iniciados juntos passam os
   dois pela guarda.
2. **`host` é gravado e nunca conferido.** `rodar()` grava `host=COMPUTERNAME` (`:627`),
   mas a guarda só consulta o `pid`. `cu.processo_vivo` chama `OpenProcess` **local**
   (`cerebro_util.py:488-493`). O pid de um worker rodando noutra máquina é julgado contra
   a tabela de processos desta — resultado sem significado.
3. **Não há lease no item.** `proximo()` faz `... LIMIT 1` sem reservar nada.

**Cenário de falha.** Worker B sobe enquanto A processa. B executa, nesta ordem:
`recuperar_orfaos()` (`worker.py:85-101`) — que devolve para `QUEUED` **todo** item em
`HYDRATING`/`EXTRACTING`/`EXTRACTED`, inclusive o que A está extraindo neste instante — e
`fonte.limpar_sobras()` (`fonte.py:72-81`) — que faz `unlink()` em **todos** os
`tmp/e7_*`, inclusive o temporário que A acabou de baixar e ainda vai abrir. A então
extrai de um arquivo apagado, ou de um arquivo pela metade. Depois os dois gravam no mesmo
`arquivo_id`: `indexar()` (`:594`) faz `DELETE FROM texto_fts WHERE arquivo_id=?` seguido
de `INSERT`s, e o intercalamento pode duplicar ou zerar os trechos.

**Correção sugerida.** Trava de arquivo (`msvcrt.locking` / `O_EXCL`) ou
`BEGIN IMMEDIATE` numa tabela de lease com `expira_em`, conferindo também `host`. Um lease
por item (`UPDATE arquivo SET estado='HYDRATING', dono=? WHERE id=? AND estado=?` e checar
`rowcount`) elimina o resto. *Nota: há um `data/cerebro.db.lock` e um `operacao.py` novos
na árvore de trabalho — parece que outro agente já está resolvendo isto.*

---

## A-5 — `17_aplicar_delta.requeue()` marca `CHANGED` com o hash novo mas deixa `conteudo_id` no conteúdo **antigo**. A busca afirma que o arquivo contém um texto que ele não contém mais.

**Evidência.** `17_aplicar_delta.py:162-177` atualiza `estado`, `quick_xor`,
`tamanho_bytes`, `tentativas` — e não toca em `conteudo_id` nem chama recontagem.
No banco real:

```sql
SELECT a.id, a.estado, a.quick_xor, c.quick_xor, a.tamanho_bytes, c.tamanho_bytes
  FROM arquivo a JOIN conteudo c ON c.id=a.conteudo_id
 WHERE a.quick_xor IS NOT c.quick_xor OR a.tamanho_bytes IS NOT c.tamanho_bytes;
-- (128398, 'CHANGED', 'RJZ1ohBhk8+IzY4TyYWB5YApthg=', 'mZnRyj9wklGS7yY1EQMMm00NX4s=',
--          30090564, 30081573)
SELECT COUNT(DISTINCT f.arquivo_id) FROM texto_fts f JOIN arquivo a ON a.id=f.arquivo_id
 WHERE a.estado='CHANGED';                                                         -- 1
```

É o `[Parceiro] Draft - One Page.pptx` citado no `STATE.md`. Hash **e** tamanho diferentes, e
o `conteudo_id` ainda aponta para os bytes velhos. `v_ocorrencia` (que exige só
`a.presente=1`) devolve esse caminho como lugar onde o texto antigo está, e os 4.361
trechos incluem os dele.

**Cenário de falha.** Cada rodada de delta produz `CHANGED`s. Entre a propagação e o
reprocessamento — que hoje é *indefinido*, o worker está `PAUSADO` desde 2026-09-21 — a
busca devolve conteúdo desatualizado como se fosse atual, apontando para um arquivo real e
acessível. É o pior modo de errar: plausível.

**É regressão?** Da `DEC-032` (o worker reconta os dois lados; o `17` não reconta nenhum) e
do espírito da `REGRA 9` (rastreabilidade: o derivado guarda de onde veio, mas a ocorrência
passa a mentir sobre o que carrega).

**Correção sugerida.** Em `requeue()`: guardar `conteudo_id` no evento `alterado_na_nuvem`,
zerar `arquivo.conteudo_id`, chamar a recontagem do grupo antigo e apagar os trechos de
`texto_fts` daquele `arquivo_id`.

---

## A-6 — `caminho_rel` e `pasta_rel` não acompanham o recaminhamento: 39 ocorrências apontam para uma pasta que não existe, 35 delas para uma "REV 04" que virou "REV 03".

**O que é.** `17_aplicar_delta.recaminhar()` (`:133-146`) atualiza `caminho_nuvem`,
`web_url`, `nome` e `caminho_anterior`. Não atualiza `caminho_rel`, `pasta_rel`, `ext`,
`categoria`, `nome_normalizado`, `biblioteca`, `suportado` nem `prioridade`.

**Evidência.**

```sql
SELECT COUNT(*) FROM arquivo a WHERE a.caminho_rel LIKE '%REV 04 - 28 08 26%'
  AND EXISTS(SELECT 1 FROM evento_arquivo e WHERE e.arquivo_id=a.id
             AND e.evento='recaminhado_na_nuvem');                                   -- 35
SELECT COUNT(*) FROM arquivo WHERE caminho_nuvem LIKE '%REV 03 - 28 08 26%';         -- 35
```

Amostra (`id 82736`):

```
caminho_rel  : ...\Relatório de Componente Ambiental\REV 04 - 28 08 26\checklist ... .xlsx
caminho_nuvem: ...\Relatório de Componente Ambiental\REV 03 - 28 08 26\checklist ... .xlsx
```

E `id 80865` foi movido para dentro de `0 - Obsoleto` na nuvem; o `caminho_rel` e o
`pasta_rel` do catálogo ainda dizem que ele está na pasta de trabalho. Os 39 divergem:

```sql
SELECT COUNT(*) FROM arquivo a WHERE EXISTS(SELECT 1 FROM evento_arquivo e
  WHERE e.arquivo_id=a.id AND e.evento='recaminhado_na_nuvem')
  AND REPLACE(a.pasta_rel,'\','/') NOT LIKE '%'||REPLACE(REPLACE(a.caminho_nuvem,'\','/'),'/'||a.nome,'')||'%';
-- 39
```

**Cenário de falha.** Três danos distintos:
1. `caminho_rel` é o que `texto_fts.caminho_rel`, `v_fila`, `v_ocorrencia`, `99_status` e
   os relatórios de `output/` mostram. A busca devolve um caminho que não existe.
2. `pasta_rel` é a chave de junção com a tabela `pasta`, que carrega `prioridade` e
   `categoria_doc`. Um arquivo que migrou para `0 - Obsoleto` (território P4) continua
   sendo processado com a prioridade da pasta de trabalho.
3. Se o rename mudar a extensão (`.txt` → `.zip`), `ext`, `categoria` e `suportado`
   continuam os antigos e o worker manda o arquivo para um extrator errado. Hoje não há
   nenhum caso (`ext` divergente do espelho: **0**), mas nada impede.

**É regressão?** Não de uma `DEC-xxx`, mas contradiz o fundamento 3 do `STATE.md`
("caminho e nome são atributos mutáveis"): se são atributos, têm de ser atualizados.

**Correção sugerida.** Em `recaminhar()`, recompor `caminho_rel`/`pasta_rel` a partir de
`biblioteca` + `caminho_nuvem`, recalcular `ext`/`categoria`/`suportado`/`nome_normalizado`
com as funções do `cerebro_util`, e reavaliar `prioridade` pela tabela `pasta` (ou marcar
para reclassificação pelo `02`). Atualizar também `texto_fts.caminho_rel`.

---

## A-7 — A prova de que a porta da nuvem é read-only é um grep de AST, não uma prova. Vários caminhos de escrita passariam.

**O que é.** `tests/test_graph_ro.py:33-59` promete, no cabeçalho, "falha se aparecer
qualquer requisição HTTP que não seja GET". O que ele faz é:

```python
alvo = ast.unparse(no.func)
if not alvo.endswith("Request"):
    continue
```

Só olha chamadas cujo nome termina em `Request`. Passariam sem nenhum FAIL:
`urllib.request.urlopen(url, data=...)`, `http.client.HTTPSConnection(...).request("PUT", …)`,
`requests.post(...)`, `subprocess.run(["curl","-X","DELETE",…])`, ou um
`req.method = "POST"` depois da construção. O blacklist de strings (`:72`) e a checagem de
nomes de método (`:76-78`) são keyword matching: um método chamado `enviar()` ou
`put_item()` não contém "upload/criar/apagar/mover/renomear/escrever" e passa.

**O que salva o projeto hoje** é o código, não o teste: `graph_ro._requisicao` levanta
`PermissionError` para qualquer método (`graph_ro.py:143-152`) e sempre constrói o
`Request` com `method=METODO_PERMITIDO`. Isso **é** verificado de verdade, em tempo de
execução, nas linhas 89-94 do teste. Mas esse bloco só roda se as três variáveis do `.env`
existirem (`:82-86`); sem credencial, a suíte retorna cedo e continua reportando PASS.

**Cenário de falha.** Alguém acrescenta um helper de escrita em `graph_ro.py` usando
`urlopen(url, data=...)`. `test_graph_ro` dá PASS em todas as 26 checagens. A `DEC-014`
("a porta é somente leitura **por construção**, e o teste verifica a AST") passa a ser
falsa sem ninguém saber.

**Correção sugerida.** Trocar o filtro por nome por uma checagem estrutural: proibir, na
AST, qualquer chamada a `urlopen`/`HTTPConnection`/`requests.*`/`subprocess.*` que não seja
a única função autorizada, e exigir que toda saída de rede passe por `_requisicao`. E
separar o teste de credencial dos testes de código, para que a ausência de `.env` não
disfarce cobertura perdida.

---

## A-8 — Um resync interrompido no meio perde para sempre a detecção de remoções.

**O que é.** `12_scan_graph.varrer_ausentes()` (`:315-345`) só roda quando
`efetivo == "completo"` (`:408`). Mas se a varredura completa disparada pelo 410 for
interrompida — `--limite` (`:394-397`) ou queda —, o token fica
`status='em_andamento'` com `next_link`. Na próxima execução em modo `auto`,
`url_inicial()` (`:113`) devolve `"retomada"`, e ao terminar a retomada o
`if efetivo == "completo"` é falso: **a varredura de ausentes é pulada**, o
`delta_link` é gravado como `concluido` (`:412`), e a instrução do servidor
("including deletes", `DEC-035`) morre em silêncio.

**Cenário de falha.** Exatamente o do E8, se tivesse caído no meio: os 6.755 itens do Parceiro
teriam ficado `presente=1` no espelho para sempre, o `17_aplicar_delta` nunca os teria
marcado `MISSING`, e o worker gastaria 3.456 downloads para colecionar 404s. Nada no
código nem em `v_progresso` sinalizaria que a detecção foi perdida.

**É regressão?** É um furo na implementação da `DEC-036`, cuja trava declarada é
"a varredura só roda quando a rodada **completa** começou e terminou na mesma execução" —
o que está certo; o que falta é o caso "começou completa, não terminou".

**Correção sugerida.** Marcar no `delta_token` que a rodada em curso nasceu completa
(ex.: `status='em_andamento_completo'`) e, ao concluir uma retomada desse tipo dentro da
execução original, rodar `varrer_ausentes()`. Se a retomada for de outra execução, recusar
e exigir `--completo`.

---

# MÉDIO

## M-1 — `checar(... or True)`: uma checagem que não pode falhar, inflando o placar.

`tests/test_reconciliacao.py:252`:

```python
checar((RAIZ / "output" / "reconciliacao.md").exists() is False or True,
       "relatorio gerado na pasta de saida do teste")
```

`X or True` é a constante `True`. Passa com `13_reconciliar.py` vazio. Agrava que o rótulo
diz "pasta de saída **do teste**" mas o caminho é `RAIZ/output` (a do repositório). A
verificação real está na linha 254. **Correção:** apagar a linha 252 — o placar de
`test_reconciliacao` cai de 49 para 48, que é o número honesto.

## M-2 — O teste do checkpoint por página nunca chega à segunda página.

`tests/test_delta.py:274-287`, rótulo *"o delta_link valido anterior foi PRESERVADO"*.
O fixture faz `graph.falhar_na_chamada = 1` e `GraphFalso.get` falha em
`self.chamadas >= self.falhar_na_chamada` (`:96-98`). `registrar_drives()` usa
`graph.drives()`, que não incrementa `chamadas` (`12_scan_graph.py:78-93`): a **primeira**
chamada de delta estoura, nenhuma página é aplicada. O `delta_link` está preservado porque
nada teve chance de escrevê-lo. Mesma coisa para *"nenhum item perdido por causa do erro"*
(`COUNT(*) == 7`): nenhuma linha foi tocada.
**Prova de vacuidade:** mover a gravação de `delta_link` da linha 412 para dentro do laço
(`:384-388`) — que quebra exatamente a `DEC-021` — e o teste continua PASS.
**Correção:** `falhar_na_chamada = 2`.

## M-3 — "prioridade manda na ordem" avalia `[] == sorted([])`.

`tests/test_worker.py:222-225`. Neste ponto o único item em estado de fila é o
`corrompido.pdf` em `ERROR_RETRYABLE`, e `falhar()` gravou `proxima_tentativa_em` a
+60 s (`cerebro_util.backoff_segundos(1)` = 60), que `v_fila` filtra. A lista vem vazia.
Mesmo cheia, `ordem == sorted(ordem)` compara o `ORDER BY` da view consigo mesmo. E o
rótulo fala do worker, que **não consome `v_fila`** — `Worker.proximo()` tem consulta
própria (`worker.py:166-190`). Apagar o `ORDER BY` de `worker.py:179` e `:189` não faz este
teste falhar, nem nenhum outro.

## M-4 — 161 trechos (60 arquivos) estão no índice e são invisíveis à consulta que "prova o modelo".

```sql
SELECT COUNT(*) FROM texto_fts f JOIN arquivo a ON a.id=f.arquivo_id
 WHERE a.conteudo_id IS NULL;                                             -- 161 (de 4.361)
SELECT COUNT(DISTINCT f.arquivo_id) ...;                                  -- 60 arquivos
SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED' AND conteudo_id IS NULL;  -- 90 de 116
```

`sql/consultas_busca.sql` consulta 2 usa `JOIN conteudo c ON c.id = extraido.conteudo_id`
(INNER). Com `conteudo_id` nulo, o trecho existe mas a consulta oficial nunca o devolve.
São os `COMPLETED` anteriores ao v4, que a migração `04_migrar_v4.py` não fez backfill
(ela é declaradamente "aditiva"). Efeito colateral no processamento em massa:

```sql
SELECT COUNT(*) FROM v_fila_nuvem q JOIN arquivo a ON a.quick_xor=q.quick_xor
  AND a.tamanho_bytes=q.tamanho_bytes AND a.estado='COMPLETED' AND a.conteudo_id IS NULL;  -- 2.055
SELECT COUNT(DISTINCT q.quick_xor||'|'||q.tamanho_bytes) FROM v_fila_nuvem q JOIN ... ;    -- 9 grupos
```

2.055 itens da fila têm conteúdo idêntico a um `COMPLETED` que o `conteudo_de()`
(`worker.py:263-275`) não enxerga. Serão baixados e extraídos de novo (9 downloads
redundantes) e o mesmo texto ficará indexado duas vezes, sob dois `arquivo_id`.
**Correção:** um backfill que crie a linha de `conteudo` para os 90 legados a partir do
`(quick_xor, tamanho_bytes)` e da `extracao` existente.

## M-5 — Uma execução aberta em 2026-09-22 nunca foi fechada.

```sql
SELECT id, etapa, iniciada_em, encerrada_em, status FROM execucao WHERE status='em_andamento';
-- (20, 'aplicar_delta', '2026-09-22T20:13:38+00:00', None, 'em_andamento')
```

Causa em `17_aplicar_delta.py`: `abrir_execucao` na linha 198, e o `return 0` da simulação
está na linha 242 — antes do `fechar_execucao` da linha 275. Toda simulação (o modo
**padrão** do script) deixa uma execução pendurada. Os mesmos `abrir_execucao` sem fecho
aparecem no `test_aplicar_delta.py:142,154,220`. Viola a `REGRA 7`: `execucao.status` deixa
de ser estado explícito confiável. **Correção:** `try/finally` em torno do corpo do `main`.

## M-6 — O comentário diz que a rede não gasta as tentativas do documento. Gasta.

`worker.falhar()` (`worker.py:240-258`):

```python
tentativas = int(item["tentativas"]) + 1
if transitorio and tentativas <= self.max_tentativas * 3:
    # Nuvem/rede fora do ar nao gasta as tentativas do documento.
    estado = "WAITING_HYDRATION"
```

O contador é **um só** e é incrementado sempre (`:252`). Um documento que sofreu 3 quedas
de rede chega ao seu primeiro erro real com `tentativas=4`, cai no `elif tentativas >=
self.max_tentativas` e vai direto para `ERROR_FINAL`, sem nenhuma tentativa de extração.
Numa sessão de 2 dias com instabilidade de rede isto produz `ERROR_FINAL` em massa por
motivo errado. **Correção:** contadores separados (`tentativas` × `tentativas_rede`).

## M-7 — `checkpoint.pendentes` usa `v_fila` mesmo quando a fonte é a nuvem.

`worker.checkpoint()` (`worker.py:134`) tem `SELECT COUNT(*) FROM v_fila` fixo, enquanto o
relatório final (`:826`) escolhe corretamente entre `v_fila_nuvem` e `v_fila`. Todo
checkpoint gravado em modo `graph` registra um número que não é a fila do worker (hoje,
+592). **Correção:** escolher a view pela fonte, como já faz a linha 826.

## M-8 — Idempotência afirmada com `>=`, que não detecta vínculo criado a mais.

`tests/test_reconciliacao.py:266-267`: `COUNT(item_id IS NOT NULL) >= antes_ids`, rótulo
"os vínculos continuam". Idempotência exige `==`. Remover a guarda que impede aplicar
vínculo `AMBIGUO` (a `DEC-023`) faria a segunda passada chutar um `item_id` para o id=5, a
contagem subiria de 9 para 10, e tanto este `checar` quanto o "não duplica linhas" da linha
264 continuariam PASS. **Correção:** `==`, e reexaminar o id=5 depois do re-run.

## M-9 — `0 < parcial < 8` onde o valor esperado é 2.

`tests/test_scanner.py:176`, com `--limite 2` sobre uma árvore de 8 arquivos. A faixa
aceita 7, ou seja, aceita o `--limite` ignorado com erro de 350%. **Correção:** `== 2`.

## M-10 — A "foto do acervo intocado" compara os arquivos alterados consigo mesmos.

`tests/test_worker.py:228-232` e `tests/test_scanner.py:189-197`:

```python
esperado = dict(antes)
esperado["Outros\\anotacao.txt"] = depois.get("Outros\\anotacao.txt")
checar(depois == esperado, "worker nao criou, apagou nem alterou nada na arvore", ...)
```

Para essas chaves a asserção vira `depois[k] == depois[k]`. Se o worker também tivesse
mexido nesses arquivos por cima da escrita do teste, a prova da `REGRA 1` não veria nada.
O teste conhece exatamente o que escreveu e poderia afirmar o valor.
**Correção:** afirmar o tamanho/conteúdo esperados em vez de copiar de `depois`.

## M-11 — Código e documentação da era pré-`DEC-013` continuam no caminho.

- `worker.Worker.liberar()` (`worker.py:607-620`) — **nunca é chamada**. Devolve o arquivo
  para "somente na nuvem" via `attrib UNPINNED`, mecanismo aposentado pela `DEC-013`.
- `self.permitir_hidratar` (`:63-64`) — calculado, nunca lido.
- `--so-nuvem` (`:742`) — declarado, nunca consultado.
- `elif desfecho == "sem_hidratacao": pass` (`:691-692`) — `processar()` nunca devolve
  esse valor (só `ok`/`erro`/`duplicata`/`ausente`/`transitorio`).
- `cu.ESTADOS_TERMINAIS` e `cu.ESTADOS_NA_FILA` — definidos em `cerebro_util.py:75-79` e
  não usados em lugar nenhum; a lista de estados da fila está duplicada literalmente em
  `worker.proximo()`, em `v_fila` e em `v_fila_nuvem` (quatro cópias que podem divergir).
- `docs/05_OPERACAO.md:120-131` documenta "Hidratação e disco — as três travas",
  `max_hidratacao_gb: 15` e `liberar_somente_o_que_hidratamos` como se fossem o fluxo
  vigente. `docs/00_CHECKLIST_AMBIENTE.md` (7 menções), `03_PLANO_MVP.md` (3),
  `04_BENCHMARK.md` (3), `02_ESQUEMA_SQLITE.md` (2) idem.
- `docs/02_ESQUEMA_SQLITE.md` ainda se intitula "Esquema do banco — **v3**" e não menciona
  `conteudo` uma única vez (`grep -c conteudo` → **0**), embora o banco esteja em v4 e a
  separação ocorrência × conteúdo seja o coração do E7.

**Correção:** apagar o que está morto, ou marcá-lo explicitamente como congelado como já
foi feito com `test_hidratacao.py`; e atualizar o `02_ESQUEMA_SQLITE.md` para o v4.

## M-12 — Dois índices idênticos sobre `arquivo(quick_xor)`.

```
ix_arquivo_quickxor  ON arquivo(quick_xor)   <- só no banco real, nenhum script o cria
ix_arquivo_qxh       ON arquivo(quick_xor)   <- 03_migrar_v3.INDICES
```

Custo de escrita dobrado em 139.908 linhas, sem benefício. É mais uma marca da sessão sem
script da `DEC-024`. **Correção:** `DROP INDEX ix_arquivo_quickxor` num script versionado.

## M-13 — Todo download custa duas chamadas ao Graph.

`graph_ro.baixar()` faz sempre um `GET /drives/{d}/items/{i}?$select=id,size,downloadUrl`
antes do conteúdo. Para os 61.337 da fila são **~122.674 chamadas** em vez de ~61.337 —
o dobro do consumo de quota e das oportunidades de 429. Os metadados (tamanho, hash) já
estão em `item_nuvem`/`arquivo` e são justamente o que o `FonteGraph` usa para validar.
**Correção:** usar `/content` direto (segue redirect) e conferir contra o que o catálogo
já sabe; manter o `$select` só como fallback.

## M-14 — A pausa graciosa pode demorar minutos porque o `sleep` do download não é interrompível.

`graph_ro.baixar()` dorme `min(Retry-After, 120)` até 3 vezes (`tentativas=4`), e
`urlopen(..., timeout=600)`. `PARAR["pedido"]` e o comando `PAUSE` só são lidos em
`rodar()` (`worker.py:641-648`), entre itens. Numa tempestade de 429 um único item pode
segurar a parada por ~6 min, enquanto `control.py pausar` espera 180 s por padrão e
conclui que o worker não respondeu. A afirmação "Pausa graciosa com worker vivo — parou no
item, gravou, respondeu SEGURO" (`STATE.md`) foi medida sem rede instável.

## M-15 — O "caso 9" do `test_reconciliacao` não exercita a reconciliação.

`tests/test_reconciliacao.py:218-220` lê `v_duplicata_nuvem`, que é um `GROUP BY quick_xor
HAVING COUNT(*)>1` puro sobre `item_nuvem`. As linhas `H1` e `H5` foram inseridas pelo
próprio fixture, e `13_reconciliar.py` nunca escreve em `item_nuvem`. O teste verifica que
o `GROUP BY` do SQLite conta 2 quando o teste inseriu 2. Esvaziar `13_reconciliar.py` não
o faria falhar.

## M-16 — `arquivo.conteudo_id` não tem chave estrangeira, então `foreign_key_check` nunca vê ponteiro quebrado.

No banco real a coluna é só `conteudo_id INTEGER` (entrou por `ALTER TABLE` em
`04_migrar_v4.py:82`, sem `REFERENCES`). O `foreign_key_check` que o `STATE.md` cita como
prova de integridade ("sem violações") é, para o vínculo mais importante do schema v4,
cego por construção. Hoje não há ponteiro quebrado (`A14 = 0`), mas a verificação não é o
que parece. **Correção:** `ADD COLUMN conteudo_id INTEGER REFERENCES conteudo(id)` na
receita (SQLite aceita em `ALTER TABLE ADD COLUMN`), ou uma view de conferência explícita.

## M-17 — O `delta_token` da biblioteca "Documentos" afirma 171.162 itens.

```sql
SELECT drive_id, status, paginas, itens_ultima FROM delta_token;
-- (b!...EXEMPLO1..., 'concluido', 156, 171162)   <- Programa
-- (b!...EXEMPLO2..., 'concluido',   1, 171162)   <- Documentos (tem 1 arquivo e 2 pastas)
```

`12_scan_graph.sincronizar` grava `self.contadores["itens"]`, que é o acumulado do objeto
`InventarioNuvem`, compartilhado entre os drives (`:387` e `:414`). Métrica falsa por drive.

---

# BAIXO

- **B-1** `sql/consultas_busca.sql:9` manda rodar `python scripts\98_consultar.py`. Esse
  script **não existe** no repositório.
- **B-2** `v_fila` do banco recriado não tem `drive_id`/`item_id`: `00_init_db.py:73-76`
  aplica `schema_v3.sql`, `schema_v4.sql` e `migrar3.INDICES`, mas **nunca**
  `migrar3.VIEWS`, que é onde a `v_fila` do v3 está definida (`03_migrar_v3.py:59-123`).
  Detalhe de CR-1.
- **B-3** `fonte.py:103` nomeia o temporário com `item_id[:24]`, truncando um id de 34
  caracteres. Colisão é improvável (16 caracteres aleatórios sobram), mas o truncamento não
  tem motivo.
- **B-4** `fonte.py:119`: `if esperado and baixados != esperado` — quando o Graph anuncia
  0 byte, a validação de tamanho é pulada inteira. Um arquivo anunciado como vazio que
  baixasse 5 MB passaria.
- **B-5** `FonteGraph` lê `worker.tamanho_max_download_gb` (`fonte.py:67`), chave que
  **não existe** em `config/projeto.yaml`; sempre cai no padrão de 2 GB. Enquanto isso
  `nuvem.tamanho_max_arquivo_gb: 2`, documentada em `docs/05_OPERACAO.md`, não tem efeito
  nenhum no caminho da nuvem. Mexer nela não muda nada, silenciosamente.
- **B-6** 17 itens da fila não têm `quick_xor`
  (`SELECT COUNT(*) FROM v_fila_nuvem WHERE quick_xor IS NULL` → 17): serão baixados **sem
  validação de hash** (`fonte.py:129`, `if anunciado:`) e não gerarão linha de `conteudo`
  (`worker.py:323`), portanto ficarão fora da deduplicação e da busca por conteúdo. São o
  mesmo padrão dos 90 de M-4, agora nascendo de novo.
- **B-7** A ordem das colunas de `arquivo` difere entre o banco real e o recriado. Só
  importa se alguém escrever `INSERT INTO arquivo VALUES (...)` posicional — hoje ninguém
  escreve, mas é uma armadilha latente.
- **B-8** `GraphFalso.__getattr__` (`test_worker_nuvem.py:72-74`) levanta `AssertionError`
  para qualquer método fora de `baixar` — é a tripwire que sustenta a afirmação
  "o worker só sabe baixar (DEC-014)". Nenhum `checar()` a exercita: se ela fosse apagada,
  as 101 checagens continuariam PASS. **Correção:** um `checar` que chame, por exemplo,
  `graph.apagar(...)` e espere `AssertionError`.
- **B-9** `worker.recuperar_orfaos()` ignora `--ids`: numa amostra controlada
  (`DEC-034`, `16_amostra_e7.py --ids`) ele ressuscita órfãos de qualquer parte do acervo,
  fora do escopo declarado da amostra.

---

# Suspeitas não confirmadas

Coisas que parecem erradas e que **não consegui provar**. Estão aqui separadas de
propósito, com o que falta para confirmar.

1. **`recalcular_caminhos()` pode produzir caminho truncado em silêncio.**
   `12_scan_graph.py:230-252`: quando o pai não está no espelho, faz `memoria[atual] = ""`
   e para; o cinto contra ciclo (`if len(visto) > 200: break`) também deixa `base = ""`.
   Nos dois casos o item ganha um `caminho_rel` relativo a um ancestral desconhecido, sem
   erro nem evento. No banco real hoje não há caso
   (`item_nuvem presente=1 com parent_id inexistente` → **0**;
   `item_nuvem presente=1 cujo pai está presente=0` → **0**). **Para confirmar:** montar um
   espelho com a cadeia de pais quebrada e verificar o caminho resultante.

2. **Ciclo de `duplicata_de` de dois passos após reeleição.**
   `reeleger_representantes()` promove uma linha em estado `DUPLICATE` cujo
   `duplicata_de` aponta para o representante anterior, e não limpa esse ponteiro. Se a
   linha antiga voltar e for coberta pela nova, ficaria `X.duplicata_de=Y` e
   `Y.duplicata_de=X`. Hoje: **0** ciclos e **0** auto-referências
   (`duplicata_de = id` → 0; `b.duplicata_de = a.id` → 0). **Para confirmar:** simular
   reeleição seguida de `reapareceu_na_nuvem` + reprocessamento.

3. **Derivados órfãos no disco quando `processar()` faz rollback.**
   `worker.py:576-580`: o `except` faz `con.rollback()`, mas `texto.gz` e `perfil.json` já
   foram gravados em `derivados/` (`:483` e `:510`) e não são removidos. O banco esquece; o
   disco não. **Para confirmar:** contar arquivos em `derivados/texto|perfil` sem linha
   correspondente em `extracao` — não fiz porque `derivados/` está fora do Git e não quis
   varrer a pasta de dados.

4. **Duas linhas de `item_nuvem` sem `caminho_rel`.**
   `SELECT COUNT(*) FROM item_nuvem WHERE caminho_rel IS NULL OR caminho_rel=''` → **2**.
   Quase certamente as duas raízes de drive (o próprio código as define com caminho vazio,
   `12_scan_graph.py:241`), mas não confirmei item a item.

---

# Resumo executivo

**2 CRÍTICOS.** Nenhum deles impede o processamento em massa de começar — os dois são
sobre preservar o que já existe:

- **CR-1** o repositório não sabe recriar o banco (três defeitos encadeados no único
  caminho, todos reproduzidos), e o que está em `derivados/`, `data/` e `data/backup/`
  está fora do Git, numa máquina só, referente a um acervo que já perdeu 5.941 arquivos;
- **CR-2** 45 duplicatas presas a um representante morto, invisíveis por construção à
  rotina que existe para consertá-las.

**O que eu resolveria antes de soltar o worker nos 61.337** (nenhum é bloqueio formal,
todos economizam trabalho ou evitam sujeira difícil de desfazer):

| | achado | ganho |
|---|---|---|
| 1 | **A-3** mover o teto de 300 MB para antes do download | ~10 h e 39 GB de banda |
| 2 | **A-1** recontar + reeleger dentro de `marcar_ausente()` | evita órfãos e índice sujo em escala |
| 3 | **A-4** trava de processo único de verdade | evita corrupção por dois workers |
| 4 | **M-4** backfill de `conteudo` para os 90 `COMPLETED` legados | 9 downloads e 161 trechos hoje invisíveis |
| 5 | **A-2** separar a fila real dos 592 fora de escopo | a previsão de horas passa a ser verdade |

**Números que vale registrar** (todos de consulta, nenhum estimado):

```
fila da nuvem                61.337 itens · 220,6 GB · 138,3 GB únicos após dedupe
cópias que a dedupe poupa    25.513
fila que o worker nunca pega    592  (Coordenacao, DEC-017)
itens > 300 MB na fila           74 ·  39 GB baixados para virar PARTIAL sem texto
itens sem quick_xor na fila      17
COMPLETED sem conteudo_id        90 de 116
trechos invisíveis à busca      161 de 4.361 (60 arquivos)
trechos apontando p/ ausente      6 (4 arquivos)
duplicatas órfãs                 45 (41 em DUPLICATE, estado terminal)
execuções nunca fechadas          1 (#20, aplicar_delta, 2026-09-22)
conteúdo órfão / contador         0 / 0 — por sorte, não por garantia (ver A-1)
integridade do arquivo SQLite    quick_check ok · foreign_key_check sem violações
```

---

# Status — sprint de convergência (Agente C, 2026-10-04)

Detalhe, mutações e placar em [`11a_QA_VACUIDADE.md`](11a_QA_VACUIDADE.md).

| Achado | Status | Commit |
|---|---|---|
| **A-7** (T-15) | **Fechado.** `test_graph_ro` agora prova read-only sem rede e sem `.env`: AST estrutural com o verificador testado contra 17 vetores, e transporte falso exercitando todo método público. Provado por mutação (`urlopen(..., data=)`, método novo, `req.method = "PATCH"`). | `a831b18` |
| **M-1** | **Fechado.** `or True` virou foto do `output/` do repositório (a mutação provou que o teste antigo deixava sobrescrever o `.md` versionado). | `ae619be` |
| **M-2** | **Fechado.** Queda na 2ª chamada, depois de uma página aplicada. | `ae619be` |
| **M-3** | **Fechado.** Lê a ordem em que o worker processou P1/P2/P3. | `ae619be` |
| **M-8** | **Fechado**, com ressalva: a mutação descrita aqui não reproduz (o índice único `ux_arquivo_item` derruba a 2ª passada). `==` + foto linha a linha da 2ª passada, provada com outra mutação. | `ae619be` |
| **M-9**, **M-10** (worker e scanner) | **Fechados.** `== 2`; foto exata (`comum.foto_exata`: mtime em ns + sha256). | `ae619be` |
| **M-15** | **Fechado.** O caso 9 lê o catálogo, não o fixture. | `ae619be` |
| CR-1, "consequência colateral" | **Reavaliado.** O `UNIQUE(caminho_rel)` sumiu do banco das suítes (`35d1eb1`). Restam `presente_nuvem`/`origem` anuláveis, `v_fila` sem `item_id`, falta `ix_arquivo_quickxor`; nenhuma suíte depende disso. | — |
| novos | **R1** (`origem` muda na 2ª reconciliação) e **R2** (`15_marcar_ausentes` quebra em banco recriado) em `tests/reproducoes_qa.py`, fora da contagem. | `caaaf0e` |
