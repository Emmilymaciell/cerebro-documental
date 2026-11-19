# 13 — Prontidão para o processamento massivo

**Auditado em 2026-10-04** (sessão 002), só leitura, sobre `main` depois do
sprint de convergência. O banco real foi lido em `mode=ro`; o efeito dos
comandos de dados foi medido numa **cópia** (apagada depois).

Este documento é o **checklist de GO/NO-GO**. Cada item tem comando e
resultado esperado. "Pronto" é quando todos os G estão verdes — não opinião.

---

## Veredito em 2026-10-04: **NO-GO**

| | situação |
|---|---|
| Código e testes | verde — 14 suítes offline, 0 FAIL; rede `graph_ro` 57/0 e `quickxor` 9/0 (executado em 2026-10-04, depois do M-13) |
| Banco real | **v4, sem as correções de dados** — o sprint não pôde aplicá-las (a escrita no banco de produção foi bloqueada pela política de permissões da sessão; a aplicação é da Emmily, comandos em G5–G10) |
| Massivo sem supervisão | G12 verde no código (B3/B4 fechados, `test_worker_robustez` 58/0); falta G13 (Agendador, janela, segredo) |

**Vira GO para a rodada 1 controlada** com G1–G11 verdes.
**Vira GO para o massivo sem supervisão** com G12–G14 verdes depois disso.

---

## O checklist

Leituras do banco sempre em `mode=ro`. Worker **pausado** em todos os passos
de escrita (G5–G10): nenhum desses scripts toma a trava do worker, exceto o
`05` (R1).

| # | Critério | Comando | Esperado |
|---|---|---|---|
| G1 | código e testes | `git status`; as 14 suítes offline do `CLAUDE.md` | árvore limpa; 0 FAIL |
| G2 | rede | `python tests\test_graph_ro.py` · `python tests\test_quickxor.py` | 0 FAIL (verde em 2026-10-04) |
| G3 | worker parado | `python scripts\cerebro.py status` | lock **livre**, comando `PAUSE` |
| G4 | espaço | `Get-PSDrive C` | ≥ 45 GB livres depois de decidir o `T-19` |
| G5 | schema | `python scripts\04_migrar_v4.py` e depois `python scripts\20_snapshot_schema.py --snapshot sql\schema_producao_v4.snapshot.sql` | "schema IDENTICO ao snapshot", saída 0. **O `04` não tem modo simulação: grava ao rodar** (reaplica views, remove o índice duplicado `ix_arquivo_quickxor`) |
| G6 | backfill | `python scripts\21_backfill_conteudo.py` (simula) → `--aplicar` | simulação: legados 60, conteúdos criados 60, cópias ligadas 82, desvinculado 1, reeleições 2; depois: invariantes todas 0 e nova simulação toda 0. A simulação segura o lock de escrita ~55 s |
| G7 | espelho | `python scripts\cerebro.py sync --aplicar` | saída 0; os 2 `delta_token` `concluido`; "conferido SIM". Substitui o `17` isolado (renova o espelho de 27/09 e propaga: ~149 caminhos realinhados, `T-14`) |
| G8 | busca v5 | `python scripts\05_migrar_v5.py` (simula) → `--aplicar` | simulação ≈ "86 conteudos {'indice_v4': 83, 'derivado': 3}", ≈ 4.688 trechos (muda se G7 trouxer novidade); depois: `meta.schema_versao='5'`, `v_reindexar` = 0, `v_busca_saude` toda 0 |
| G9 | reprocesso por versão | `python scripts\22_reprocessar_extrator.py` (simula) → `--aplicar` | ≈ 85 conteúdos (pdf 49, xlsx 20, texto 10, docx 5, pptx 1); uma 2ª simulação logo depois do `--aplicar` deve dar 0 |
| G10 | execuções abertas | `SELECT COUNT(*) FROM execucao WHERE status='em_andamento'` | 0. A `#20` (2026-09-22) precisa ser fechada à mão, com backup |
| G11 | invariantes | `cu.checar_invariantes(con)` em `mode=ro` | todas as listas vazias |
| G12 | sem supervisão | testes de B3 e B4 | "item que derruba o worker 2× → ERROR_FINAL" e "401 em série → worker para, tentativas intactas" |
| G13 | operação | `Get-ScheduledTask CerebroDocumental`; `worker.janela` no yaml; validade do segredo do app no Entra ID | tarefa `Ready`; janela 19:00–07:00; segredo válido além do fim previsto |
| G14 | rodada 1 aceita | critérios da seção "Rodada 1" | todos atendidos |

Ordem obrigatória: G5 antes de G6 antes de G8 (senão os 56 legados ficam fora
da v5); G8 antes de soltar o worker (sem v5 o worker alimenta o `texto_fts`, e
a reeleição faz SCAN da tabela inteira — B1). O `04` pode ser repetido a qualquer momento (R10 corrigido).
**Nunca** `DROP TABLE texto_fts`: `17`, `19`, `21`, `04`, `13`, `15` e
`cerebro_util` ainda a consultam (R11).

---

## Achados

### Bloqueadores

| | achado | situação |
|---|---|---|
| B1 | correções de dados não aplicadas no banco real (v4, invariantes 41/82/56/1/2, execução #20 aberta) | **aberto — Emmily**, G5–G10 |
| B2 | download por `/content` (M-13) sem validação no Graph real | **fechado** — rede verde em 2026-10-04 |
| B3 | pílula envenenada: item que derruba o processo volta à fila sem limite e é escolhido de novo | **fechado** — 2ª queda no mesmo item → `ERROR_FINAL` (`worker.max_quedas_por_item`), `tests/test_worker_robustez.py` |
| B4 | 401/403 em série viram `ERROR_FINAL` em massa sem parar o worker | **fechado** — disjuntor: tentativa intacta, sessão para em `AGUARDANDO_CREDENCIAL` (código 6) |

### Riscos aceitáveis (com mitigação)

| | risco | mitigação |
|---|---|---|
| R1 | `sync`/`17`/`21`/`22`/`04` não tomam a trava: com worker rodando, "database is locked" gasta tentativa; item em voo marcado MISSING termina COMPLETED com `presente=0` | sync e scripts de dados **só com o worker pausado**; melhoria: sync tomar a trava (~30 min) |
| R2 | `A-8`: resync interrompido perde a detecção de remoções (`12_scan_graph.py`) | depois de interrupção/410, `cerebro.py sync --aplicar --completo` (~9 min) |
| R3 | disco: projeção 6–16 GB (banco v5 4–11, derivados 0,6–2,7, perfis ~0,2) contra ~31 GB acima do piso | o piso de 25 GB pausa com segurança; `05` não confere espaço antes do backup; `derivados_max_gb` não mede o banco |
| R4 | 17 itens sem `quick_xor` ficam sem `conteudo` → invisíveis na v5 | **fechado** — quickXor sempre calculado; o `17` não marca CHANGED quando só o espelho não tem hash |
| R5 | `22`: inclui por padrão os adiados por teto; laço após reeleição; conta `feitos` sem `rowcount` | **fechado** — marcas só com filtro explícito, decisão por conteúdo, `rowcount` |
| R6 | regra de OCR por página sem calibração (L1); métrica de cobertura não gravada → recalibrar exigiria rebaixar ~12,5 mil PDFs | **métricas gravadas** (`pdf/1.3`, `ocr_metricas`, ~10 B/página); a calibração ainda depende da amostra L1 (rodada 1) |
| R7 | `--max-gb-sessao`/`--limite` são por sessão: com tarefa a cada 30 min, não limitam a noite; não há `worker.janela` no yaml | limitar a noite pela janela; primeira noite com gatilho único |
| R8 | tarefa do Agendador não registrada; `LogonTrigger` + `InteractiveToken` não roda após reboot de madrugada até alguém logar | decidir o gatilho ao registrar |
| R9 | heartbeat parado durante download (até ~17 min) e extração | download **fechado** (pulso a cada `heartbeat_s`); extração aberta (`T-26`) |
| R10 | `04` rodado com banco em v5 rebaixa `schema_versao` para 4 | **fechado** — o `04` nunca rebaixa nem faz backup sem mudança estrutural |
| R11 | `DROP TABLE texto_fts` quebraria 7 scripts | não fazer |
| R12 | execução #20 aberta | G10 |
| R13 | `T-08`: lixeira do SharePoint guarda ~93 dias → Parceiro excluída ~20/09 expira ~22/12 (confirmar com a TI) | decisão da Emmily, independente da massificação |

### Melhorias (não bloqueiam)

- `proximo()` custa ~389 ms por chamada (`TEMP B-TREE FOR ORDER BY` sobre a
  fila inteira) → ~3–7 h de overhead na massa; índice parcial
  `(prioridade, tamanho_bytes) WHERE <fila>` resolve (é mudança de schema:
  exige migração e snapshot novo, `DEC-044`).
- Ferramenta para devolver `ERROR_FINAL` à fila por categoria de erro.
- Conteúdo `ERRO`/`NAO_SUPORTADO` não cobre as cópias: cada cópia de um
  protegido/OLE é baixada de novo.
- `tests/test_delta.py` escreve log em `tmp/` do repositório (30 arquivos
  `teste_delta_*.jsonl`).

---

## Rodada 1 — controlada, supervisionada

De dia, no terminal (não pelo Agendador), depois de G1–G11:

```
python scripts\cerebro.py retomar
python scripts\cerebro.py processar --prioridade-max 0 --limite 300 --max-gb-sessao 3 --uma-vez
```

A fila anda por tamanho crescente: esses 300 são os menores P0 — valida a
mecânica, não o ETA nem o L1. Para o **L1** (quantos PDFs são escaneados),
gravar antes em `output/` uma lista determinística de ~300 PDFs P0
estratificados por faixa de tamanho, ordenados por `item_id`, e rodar
`processar --ids ...`.

**Aceite:** invariantes todas 0 · `v_busca_saude` 0 e `v_reindexar` 0 ·
`cerebro.py erros` sem autenticação nem divergência de hash · `tmp\e7_*` vazio ·
os itens do `22` na versão atual · taxa de `OCR_REQUIRED` nos PDFs, itens/min e
MB/s registrados.

**Primeira noite** (só com G12): P0+P1 (29.987 itens, 127 GB), janela 19–07,
1 worker, gatilho único. Previsão (estimativa, não medição): ~115 GB únicos a
0,6–1,1 MB/s → **29–53 h de download**, 3 a 5 noites para a fila inteira.
