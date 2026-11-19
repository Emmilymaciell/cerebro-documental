# 11a — Prova de vacuidade dos testes (sprint de convergência, Agente C)

**Data:** 2026-10-04 · **Base:** `a379ef4` · **Commits:** `ae619be` (T-16), `a831b18` (T-15),
`caaaf0e` (reproduções). Complementa o [`11_QA_AUDITORIA.md`](11_QA_AUDITORIA.md).

## Método

Para cada checagem suspeita: (1) introduzir **temporariamente** em `scripts/` o bug que
ela deveria pegar; (2) rodar a suíte **original** (`a379ef4`) — se continua verde, a
vacuidade está provada; (3) corrigir o teste; (4) rodar o teste novo com a mutação
(tem de ficar vermelho) e sem ela (verde). A mutação é revertida com
`git checkout -- <arquivo mutado>` logo depois; nenhuma mutação foi commitada. Tudo em
banco temporário, sem rede, sem tocar no banco de produção.

## Provas

| Achado | Mutação temporária em `scripts/` | Teste original | Teste novo |
|---|---|---|---|
| **M-1** `checar(X or True)` | `13_reconciliar`: relatório gravado em `RAIZ_REPO/output/reconciliacao.md` | a linha 252 **PASS** (só a 254 falhou) — e a mutação sobrescreveu o `.md` versionado | **3 FAIL**: "não foi escrito na output/ do repositório" (1ª e 2ª passada) + "gerado na saída do teste" |
| **M-15** caso 9 | `13_reconciliar.inserir_novos`: linha nova sem `quick_xor` | **49/0** | **3 FAIL** (duplicatas no catálogo, hash da linha nova, idempotência) |
| **M-8** `>=` | (a) a do relatório — `decidir_por_nome` reivindica item já usado | **não reproduz como descrito**: o índice único `ux_arquivo_item` derruba a 2ª passada com `IntegrityError` ("roda de novo sem erro" FAIL) | idem |
| **M-8** idempotência | (b) `vincular()` troca `CHANGED` por `QUEUED` (só age na 2ª passada) | **49/0** | **1 FAIL**: "segunda passada não muda nenhuma linha" |
| **M-2** checkpoint por página | `12_scan_graph`: grava `delta_link` a cada página, só no incremental | **65/0** | **1 FAIL**: "delta_link anterior PRESERVADO" (`pg:9:1`) |
| **M-2** (variante) | o mesmo em qualquer modo | seção 10 **PASS**; pego só pela seção 11 | seção 10 também FAIL |
| **M-3** prioridade | `worker.proximo` (fonte local): `ORDER BY (prioridade>0), prioridade DESC, …` — P0 primeiro, resto invertido | **34/0** (avaliava `[] == sorted([])`) | **1 FAIL**: ordem `['dados.csv','reuniao.pptx','medicao 12.docx']` |
| **M-10** worker | `worker.processar`: `os.utime` no original quando `estado='CHANGED'` | **34/0** | **1 FAIL**: 4 arquivos alterados, inclusive `anotacao.txt` |
| **M-9** `0 < parcial < 8` | `01_scan_inventario`: `>= limite` → `> limite` | **48/0** (aceitou 3) | **1 FAIL**: "respeita --limite 2 (3 arquivos)" |
| **M-10** scanner | `01_scan_inventario`: `os.utime` no arquivo detectado como alterado | **48/0** | **1 FAIL**: `contrato.pdf` |
| **A-7** (T-15) | `graph_ro.get`: `urlopen(self._requisicao(url), data=b"x", …)` | **6 PASS / 1 FAIL** — o único FAIL é a falta de `.env`; "nenhuma requisição fora do GET" **PASS** | **2 FAIL** (AST + transporte: corpo enviado) |
| **A-7** | método novo `atualizar()` com `urlopen(url, data=corpo)` | idem, AST **PASS** | **2 FAIL** (AST + "método público sem roteiro") |
| **A-7** | `_requisicao` faz `req.method = "PATCH"` depois de montar | idem, AST **PASS**; a checagem de `PermissionError` também passaria | **3 FAIL** (AST, guarda, transporte) |
| **A-7** controle negativo | `urlopen(..., timeout=self.timeout_s)` (o que o Agente A pode fazer) | — | **49/0**: timeout legítimo não reprova |
| varredura: busca `>= 4` | `worker.indexar` não indexa `.csv`/`.txt` | **34/0** | **1 FAIL**: faltam 2 formatos |
| varredura: extratores | `extrair_texto` regrava o original (mesmo conteúdo) | **251/0** (só olhava nomes, só PDF) | **1 FAIL**: `documento.txt` |

Achado lateral da M-3: qualquer mutação que tire a prioridade do `ORDER BY` derruba
`test_worker` **por acidente** — a cópia idêntica (P3, rowid menor) é processada antes da
original (P0) e a original vira `DUPLICATE`. O teste morria com `TypeError` antes do
placar; agora falha limpo. A afirmação do relatório "apagar o `ORDER BY` não faz nenhum
teste falhar" não vale mais no código atual; a checagem da linha 225 continuava vácua.

## T-15 — o que a prova de read-only cobre agora

Sem rede e sem `.env` (antes, sem `.env`, as checagens de guarda e de URL nem rodavam):
AST estrutural (imports permitidos; `urlopen` só com `Request` vindo de
`self._requisicao`, inclusive via auxiliar que centralize o `timeout`; único `Request`
com corpo é o login em `login.microsoftonline.com`; nada de `.method`/`.data`
reatribuído, `get_method`, alias de `urlopen`, `build_opener`, `http.client`,
`requests`, `socket`, `subprocess`, `__import__`), com o **próprio verificador** testado
contra 17 vetores sintéticos e 3 casos legítimos; e comportamento com transporte falso
e socket bloqueado, exercitando **todo** método público de `GraphRO`. Método público
novo sem roteiro reprova. A parte de rede continua separada e exige `.env`.

## Banco de teste × banco de produção (CR-1, "consequência colateral")

Comparação objeto a objeto (`sqlite_master`, `PRAGMA table_info/index_list`) do banco
que as suítes constroem com `data/cerebro.db` aberto em `mode=ro`:

| Receita | Suítes |
|---|---|
| `00_init_db` | scanner, worker, worker_nuvem, busca, operacao |
| `00_init_db` + `03_migrar_v3 --sem-backup` | delta, reconciliacao, aplicar_delta, ingestao |

As duas dão **o mesmo banco**: em banco novo o `03_migrar_v3` diz "nada a fazer". O
`UNIQUE(caminho_rel)` do CR-1 **não existe mais** (corrigido em `35d1eb1`; o
`test_scanner` já checa). Diferenças que restam:

| | produção | banco das suítes | alguma suíte depende disso? |
|---|---|---|---|
| `arquivo.presente_nuvem` | `NOT NULL DEFAULT 0` | anulável, sem default | não: nenhuma leitura usa `= 0`/`IS NULL`; só escrita |
| `arquivo.origem` | `NOT NULL DEFAULT 'local'` | anulável | não: nenhuma lógica lê `origem` |
| `v_fila` | expõe `drive_id, item_id` | **não expõe** (é a do v2) | nenhuma suíte; mas `15_marcar_ausentes.py:140` sim → **R2** |
| `ix_arquivo_quickxor` | existe | não (só o gêmeo `ix_arquivo_qxh`, M-12) | não |
| ordem das colunas | nuvem no fim | nuvem no meio | não |

## Defeitos de produção revelados (em `tests/reproducoes_qa.py`, fora da contagem)

- **R1** — `13_reconciliar --aplicar` não é idempotente em `arquivo.origem`: a 2ª passada
  troca `'graph'` por `'ambos'` nas linhas que a 1ª criou (`vincular()` sempre grava
  `'ambos'`). Gravidade baixa (campo só informativo), mas contradiz "as linhas novas
  vieram da nuvem". Por isso `origem` está fora da foto de idempotência do
  `test_reconciliacao`.
- **R2** — `00_init_db.py` não aplica o bloco `VIEWS` do `03_migrar_v3`; em banco
  recriado, `15_marcar_ausentes --aplicar` marca os `MISSING`, faz commit e morre com
  `OperationalError: no such column: item_id`. Do Agente B (schema).

## Placar

| Suíte | antes | depois | por quê |
|---|---|---|---|
| scanner | 48 | 48 | mesmas checagens, agora exatas |
| worker | 34 | 35 | −1 vácua (`v_fila`); +2 (premissa de tamanho, ordem real) |
| delta | 65 | 68 | +3 (queda depois de 1 página, `next_link`, item gravado) |
| reconciliacao | 49 | 52 | +1 hash da linha nova, +1 foto da 2ª passada, +1 output/ na 2ª passada |
| extratores | 251 | 253 | +2 (seis extratores leem a cópia; nenhum altera o original) |
| worker_nuvem · aplicar_delta · ingestao · busca · operacao | 101 · 34 · 77 · 124 · 118 | idem | não editadas |
| **offline (as 10 do CLAUDE.md)** | **901** | **910** | |
| graph_ro `--sem-rede` | 6 PASS / 1 FAIL (sem `.env`) | **49 / 0** | prova local completa, independente de `.env` |
| quickxor `--sem-rede` | 6 | 6 | não editada |
