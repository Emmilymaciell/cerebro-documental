# Plano — etapas e estado

Cada etapa tem **entregável** e **teste objetivo**. Etapa sem teste passado não
avança.

---

## E1 — Inventário de toda a árvore ✅

| | |
|---|---|
| **Entregável** | `00_init_db.py`, `01_scan_inventario.py`, `schema_v2.sql` |
| **Teste** | `python tests\test_scanner.py` → **42 PASS / 0 FAIL** |
| **No real** | 139.459 arquivos, 20.101 pastas, 1,168 TB, **0 erros**, 1,72 min |

---

## E2 — Classificação P0..P4 pela própria árvore ✅

| | |
|---|---|
| **Entregável** | `02_classificar.py` + `config/classificacao.yaml` |
| **Teste** | incluso no `test_scanner.py`: P0/P1/P2/P3/P4 em árvore controlada |
| **No real** | P0 33.132 · P1 3.682 · P2 95.405 · P3 1.367 · P4 5.605 |
| **Auditoria** | `output/prioridades.md` e `output/pastas_classificadas.csv`, com o motivo de cada pasta |

Errou alguma pasta? Edite `config/classificacao.yaml` (padrões ou `excecoes`) e
rode de novo — leva segundos e não reprocessa documento nenhum.

---

## E3 — Processador autônomo ✅

| | |
|---|---|
| **Entregável** | `worker.py`, `control.py`, `extratores.py`, tabelas `controle` e `checkpoint` |
| **Teste** | `python tests\test_worker.py` → **34 PASS / 0 FAIL** |
| **No real** | 146 documentos processados, 0 erros, texto indexado e buscável |
| **Pausa** | testada com worker vivo: parou no item, gravou, respondeu `SEGURO` |
| **Queda** | item preso em `EXTRACTING` volta para a fila na subida |

---

## E4 — Hidratação segura ✅

| | |
|---|---|
| **Entregável** | `cu.pode_hidratar` / `garantir_local` / `liberar_espaco` + travas em `config` |
| **Teste** | `python tests\test_hidratacao.py --confirmar` → **22 PASS / 0 FAIL** |
| **Prova** | sha256, tamanho e datas idênticos antes e depois do ciclo completo |
| **No real** | 136 arquivos devolvidos para a nuvem depois de processados |

---

## E5 — Extração massiva ⏸️ *aguardando sua ordem*

```bash
python scripts\control.py resume
python scripts\worker.py run --prioridade-max 0     # P0 primeiro
python scripts\worker.py run --prioridade-max 1     # depois P1
python scripts\worker.py run                        # e o acervo inteiro
```

**Teste de aceitação:** você faz 5 perguntas reais ao índice e confere se os
trechos devolvidos (com caminho e página) respondem. Acerta ou não acerta —
a gente mede e ajusta.

---

## E6 — Formatos que ainda não têm extrator

73.349 arquivos estão em `UNSUPPORTED` — catalogados, esperando. Em ordem de
custo-benefício:

| Formato | Arquivos | Volume | O que dá para fazer |
|---|---:|---:|---|
| `.jpeg/.jpg/.png` | 46.178 | 28 GB | OCR (E7) — são fotos de obra e documentos escaneados |
| `.dav` + `.idx` | 13.894 | 42 GB | vídeo de CFTV: catalogar metadados, não extrair |
| `.zip` | 1.207 | 110 GB | ler o **índice** do zip sem extrair: já dá mapa do conteúdo |
| `.dwg/.shp` | 2.270 | 62 GB | metadados e, se valer, texto de anotação |
| `.msg/.eml` | 1 | — | remetente, assunto, data, corpo |

Cada um é uma função nova em `extratores.py` + uma linha em
`extratores_ativos`. O que já está catalogado volta sozinho para a fila.

---

## E7 — OCR (fila própria, opt-in)

Depende de instalar o Tesseract (offline, grátis, nada sai da máquina). Roda
sobre a fila `OCR_REQUIRED`. É a etapa mais cara (~1–3 s por página) — por isso
é separada e sob demanda.

---

## E8 — Monitor incremental permanente

O ciclo `01_scan` + `02_classificar` + `worker` já é incremental: só o que mudou
de tamanho/data volta para a fila. Falta apenas agendar (receita pronta em
`docs/05_OPERACAO.md`) e implementar a detecção de **movido** (mesmo sha256 em
caminho novo → não reprocessa o conteúdo, só atualiza o caminho).

---

## E9 — Camada de IA (fora desta fase)

```
pergunta → filtro por metadados (obra, categoria, período)
         → FTS5 → ranking bm25 → poucos trechos
         → IA, com citação obrigatória (arquivo + página)
```

A arquitetura já está pronta para isso: o índice guarda `arquivo_id`, `pagina` e
`trecho_idx`. Nunca mandar centenas de documentos quando cinco trechos resolvem.
