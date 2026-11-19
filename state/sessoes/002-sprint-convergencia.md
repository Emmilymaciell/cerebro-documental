# Sessão 002 — Sprint de convergência pré-massificação

**Data:** 2026-10-04 · **Base:** `fec07a2` · **Branch:** main · **HEAD final:**
o commit que contém este arquivo (`git log -1 -- state/sessoes/002-sprint-convergencia.md`)

## Objetivo

Levar o Cérebro da fundação até o ponto seguro imediatamente anterior ao
processamento massivo, fechando primeiro as dívidas que ficam caras depois de
dezenas de milhares de documentos processados. Massificação **não** começou.

## Como foi executado

Lead/integrador + 7 agentes em worktrees isoladas com fronteiras de arquivo
disjuntas, cada um commitando no próprio branch; o Lead integrou um por um
(cherry-pick), resolveu os conflitos, rodou a regressão e commitou. Mais uma
auditoria de prontidão independente, só leitura.

| Frente | Entregou |
|---|---|
| A — worker | `T-10` (teto antes do download), `T-18` (prazo duro), `T-12`, M-6, M-13 (download em 1 chamada, token fora do redirect), M-14 |
| B — dedupe/banco | `CR-1` (receita = produção, snapshot + `test_schema`), `T-11`, `T-13`, `T-09`+M-4 (`21_backfill`), `T-17` (`22_reprocessar`), `T-14` (realinhar caminho), invariantes, M-5 |
| C — QA | `T-16` (testes vácuos com prova por mutação), `T-15` (prova real de read-only), `tests/reproducoes_qa.py` |
| D — busca | `busca.py`, `05_migrar_v5.py`, política de trecho (`DEC-041`) com benchmark |
| E — extratores | E9: pdf/1.2, docx/1.1, xlsx/1.1, pptx/1.2; OCR por página; formato sem leitor não é baixado (L7); `\f` entre páginas no derivado |
| F — robustez | B3 (pílula envenenada), B4 (disjuntor de credencial, código 6), R4 (quickXor sempre), heartbeat no download |
| G — consistência | R5 (`22`: marcas, decisão por conteúdo, rowcount), R10 (`04` não rebaixa), R6 (`pdf/1.3` com métricas de OCR por página) |
| Lead | conflito `graph_ro`×prova de AST; v5 ligada ao worker (`test_worker_v5`); `cerebro.py erros|cobertura|sync`; `control.py` pela `v_fila_nuvem`; R1 do QA (`13_reconciliar` origem); contador vivo no worker; `17` sem CHANGED falso; `busca.mesmo_texto` |

## Resultado

- **feito e validado (código):** tudo da tabela acima.
- **NÃO feito — bloqueado:** aplicar as correções no **banco real**. O
  `04_migrar_v4.py` contra produção foi negado pela política de permissões da
  sessão; por regra, nada foi tentado por outro caminho. Ficou como `T-21`, com
  os comandos e os resultados esperados no `STATE.md` e no G5–G11 do
  `docs/13_PRONTIDAO_MASSIFICACAO.md`. Feito antes do bloqueio: backup
  `data/backup/cerebro_pre_sprint002_20261004_105618.db` (958 MB, quick_check ok).
- **investigado e NÃO implementado:** `A-8` (`T-24`), trava nos scripts de
  dados (`T-23`), índice da fila (`T-22`), heartbeat na extração (`T-26`).

## Descobertas

- O banco real bate exatamente com o que os agentes mediram em cópias:
  `integrity_check` ok, FK ok, invariantes 41/82/56/1/2 (todas corrigidas pelo
  `21` na cópia), único desvio de schema = índice duplicado `ix_arquivo_quickxor`.
- O heredoc do Bash comeu barra invertida **três vezes** nesta sessão (REGRA 4):
  `\0` virou caractere de controle, `\t` virou tab, `\n` virou quebra real.
  Toda edição com barra invertida vai por Write/Edit.
- O 403 do Graph não distingue "app perdeu o site" de "item com permissão
  própria"; o disjuntor decide pelo contexto da sessão (`docs/10`).
- Fila real: 668 itens / 2,2 GB sem leitor e 75 / 40 GB acima do teto deixam de
  ser baixados; 85 conteúdos voltam pelo `22`.

## Decisões

`DEC-041` trecho `chunk/2.0:1500:150` · `DEC-042` o que não será lido não é
baixado · `DEC-043` OCR por página · `DEC-044` snapshot de schema é contrato ·
`DEC-045` ocorrência viva e reprocessamento por conteúdo.

## Validação (executado em 2026-10-04, no `main` final)

| comando | resultado |
|---|---|
| 15 suítes offline do `CLAUDE.md` | **1.671 PASS / 0 FAIL** |
| `python tests\test_graph_ro.py` (rede) | 57 PASS / 0 FAIL |
| `python tests\test_quickxor.py` (rede) | 9 PASS / 0 FAIL |
| `python tests\reproducoes_qa.py` | 0 defeitos reproduzindo |
| banco real, `mode=ro`: `quick_check` · `integrity_check` · `foreign_key_check` | ok · ok · vazio |
| `python scripts\20_snapshot_schema.py --snapshot sql\schema_producao_v4.snapshot.sql` | só `+INDEX ix_arquivo_quickxor` (some com o `04`) |

Cada correção tem prova de que o teste falha sem ela (mutação ou código
anterior) — registrada na mensagem do commit correspondente.

**Não executado:** nenhum script de escrita contra o banco real (bloqueado);
nenhum processamento do acervo; nenhum OCR; nenhuma IA.

## Pendências abertas

`T-21` (aplicar no banco — **próximo**), `T-08` (prazo ~22/12), `T-19`,
`T-20`, `T-22`–`T-27`. Tabela completa no `STATE.md`.

## Próximo passo

`T-21`: rodar, com o worker pausado, a sequência G3→G11 do
`docs/13_PRONTIDAO_MASSIFICACAO.md` (comandos no "Próximo passo exato" do
`STATE.md`). Concluído quando `schema_versao='5'`, invariantes todas vazias,
`v_reindexar`=0, `v_busca_saude` toda 0 e nenhuma execução `em_andamento`.
Depois, a rodada 1 controlada.
