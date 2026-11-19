# Arquitetura

## Princípio que governa tudo

```
código local determinístico → extração estruturada → indexação
      → heurística → IA só quando realmente necessária
```

E o corolário operacional: **o que se repete milhares de vezes é do Python.**
O Claude constrói, diagnostica, melhora e decide o que exige raciocínio. O
processamento em si não consome nenhum token.

---

## As camadas

```
┌──────────────────────────────────────────────────────────────────┐
│ 0. FONTE — SharePoint pelo Microsoft Graph (READ-ONLY)           │
│    1,166 TB · 137.856 arquivos · 22.149 pastas                   │
│    identidade (drive_id, item_id) · hash do servidor de graça    │
└───────────────────────────┬──────────────────────────────────────┘
                            │ graph_ro (só GET) + delta query
┌───────────────────────────▼──────────────────────────────────────┐
│ 1. INVENTÁRIO · 12_scan_graph.py                            ✅   │
│    delta paginado → `item_nuvem` (espelho da nuvem)              │
│    caminho derivado da cadeia de parent_id · eventos por item    │
│    checkpoint por página; delta_link só quando a rodada termina  │
│                                                                  │
│ 1b. RECONCILIAÇÃO · 13_reconciliar.py                       ✅   │
│    liga `item_nuvem` a `arquivo` por (drive_id, item_id),        │
│    preservando estado, derivados, índice e prioridade.           │
│    Dúvida não vira certeza: vira fila de exceções.               │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│ 2. CLASSIFICADOR · 02_classificar.py                        ✅   │
│    lê a própria árvore: obra + categoria documental + descarte,  │
│    com herança de pai para filho e MOTIVO gravado                │
│    → `pasta` (P0..P4) → prioridade propagada para `arquivo`      │
│    → QUEUED (tem extrator) | UNSUPPORTED (catalogado, não some)  │
└───────────────────────────┬──────────────────────────────────────┘
                            │ `v_fila`: prioridade → local → pequeno
┌───────────────────────────▼──────────────────────────────────────┐
│ 3. WORKER · worker.py — autônomo, roda dias                 ✅   │
│                                                                  │
│    ┌─ lê comando (RUN/PAUSE/STOP) e checa disco                  │
│    ├─ pega o próximo item da fila                                │
│    ├─ hidrata (se autorizado, orçado e couber no disco)          │
│    ├─ sha256 → duplicata exata?                                  │
│    ├─ extrai (pdf, docx, pptx, xlsx, csv, txt)                   │
│    ├─ grava texto.gz + perfil.json + trechos no FTS5             │
│    ├─ estado final + COMMIT  ← isto é o checkpoint               │
│    ├─ devolve o arquivo para a nuvem (libera espaço)             │
│    └─ próximo                                                    │
│                                                                  │
│    Um arquivo ruim vira ERROR_* e o laço continue. Sempre.       │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│ 4. CONTROLE · control.py + tabela `controle`                ✅   │
│    pause (espera o item fechar) / resume / stop / status         │
│    Nenhuma sessão de IA envolvida. Pausa é persistente.          │
│    `checkpoint` guarda a foto periódica para auditoria.          │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│ 5. ÍNDICE · texto_fts (FTS5, sem acento, por trecho + página)    │
│    responde "quais trechos respondem a esta pergunta"            │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│ 6. FUTURO (fora desta fase)                                      │
│    OCR (fila própria) · extrator de .zip/.msg/CAD ·              │
│    classificação de tipo de negócio · camada de IA:              │
│      pergunta → filtro por metadados → FTS → ranking →           │
│      poucos trechos → IA com citação obrigatória                 │
└──────────────────────────────────────────────────────────────────┘
```

---

## Por que SQLite

- **Um arquivo.** Backup é copiar; restaurar é copiar de volta.
- **FTS5 embutido** — busca com ranking, sem servidor, sem Docker.
- **WAL** — sobrevive a queda de energia e permite ler enquanto o worker escreve.
- Aguenta milhões de linhas de metadados com folga.

Trocaria se: vários computadores processando ao mesmo tempo (Postgres) ou busca
semântica por embedding (`sqlite-vec`, ainda sem servidor). Nenhum é o caso hoje.

## Por que o texto não fica no banco

Texto de ~1 TB de documentos passa de 10 GB. Fica em `derivados/texto/<id>.txt.gz`
(comprimido, fragmentado em pastas de mil), e o banco guarda caminho e métricas.
O FTS5 indexa **trechos**, não o texto inteiro.

Assim: backup do banco continua leve, reindexar não exige reprocessar documento,
e dá para abrir o `.txt.gz` e conferir na mão (`control.py texto <id>`).

---

## Como o sistema tolera falha

| Falha | O que acontece |
|---|---|
| Ctrl+C / queda de energia / reboot | commit por item; na subida, `HYDRATING`/`EXTRACTING` voltam para a fila |
| Internet cai / OneDrive fora | erro **transitório**: `WAITING_HYDRATION` com backoff; não gasta as tentativas do documento |
| Muitas falhas de rede seguidas | `AGUARDANDO_REDE`: espera e volta; depois de 25 seguidas, para e avisa |
| Disco enchendo | `PAUSA_SEGURA_DISCO` antes de baixar o próximo arquivo |
| Derivados crescendo demais | mesma pausa, por `worker.derivados_max_gb` |
| Arquivo corrompido / com senha | `ERROR_RETRYABLE` → `ERROR_FINAL`, com a mensagem; a fila segue |
| Arquivo de 0 byte | `vazio` — não é erro nem duplicata |
| Formato sem extrator | `UNSUPPORTED`; volta para a fila sozinho quando o extrator existir |
| Pasta sem permissão | erro registrado em `log` e em `progresso_diretorio.erro`; a varredura continua |
| Extrator melhorou | `extracao.versao` muda → só os arquivos daquele extrator reprocessam |

---

## O que o sistema nunca faz

- Escrever qualquer coisa dentro de `Dynatest Engenharia\`.
- Baixar arquivo da nuvem fora do orçamento e do piso de disco.
- Apagar linha do banco.
- Mandar documento para serviço externo.
- Concluir que dois arquivos são "a mesma versão" por parecença de nome — isso é
  **candidato** (`v_familia_versao`), para conferência humana.
