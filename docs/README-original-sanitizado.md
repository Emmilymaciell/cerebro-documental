# Cérebro Documental do SharePoint

Mapeamento e processamento **de todo** o acervo documental do SharePoint
corporativo (~1 TB, sincronizado localmente), **sem alterar, mover ou apagar
nenhum documento original**.

Não é um script que roda uma vez: é um **processador autônomo e persistente**.
Liga, processa por horas ou dias, pausa com segurança, retoma de onde parou e
nunca precisa de uma sessão de IA aberta para continuar.

---

## O que ele é

| | |
|---|---|
| **Fonte** | `C:\caminho\exemplo\acervo\` — **read-only absoluto** |
| **Escopo** | a árvore inteira. Prioridade define a **ordem**, não o escopo |
| **Banco** | `data/cerebro.db` — SQLite + FTS5: inventário, estados, eventos, checkpoints, busca |
| **Derivados** | `derivados/texto/*.txt.gz` e `derivados/perfil/*.json` |
| **IA** | nenhuma nesta fase. Tudo determinístico, tudo local |

---

## Arquitetura

```
SharePoint sincronizado (read-only, ~100% "só na nuvem")
        │  os.scandir + \\?\   — NÃO abre arquivo, NÃO baixa nada
        ▼
  01_scan_inventario ─────────► inventário completo (tabela `arquivo`)
        │                              │
        │                              ▼
  02_classificar ──────────────► P0..P4 pela própria árvore (tabela `pasta`)
        │                              │
        │                              ▼
        │                        fila (`v_fila`): prioridade → local → pequeno
        │                              │
        ▼                              ▼
  worker.py  ──►  hidrata ──► extrai ──► grava texto.gz + perfil.json
        │              │                   ──► indexa trechos no FTS5
        │              └──► devolve o arquivo para a nuvem (libera espaço)
        │
        ├── control.py pause / resume / stop / status   (sem IA)
        ├── checkpoint por item + recuperação após queda
        └── 99_status.py — quanto falta, em que estado, a que velocidade
                                       │
                                       ▼
                        [futuro] OCR · camada de IA com citação
```

---

## Uso

```bash
cd "C:\caminho\exemplo\cerebro-documental"

python scripts\00_init_db.py            # cria o banco
python scripts\01_scan_inventario.py    # inventaria TUDO (não baixa nada)
python scripts\02_classificar.py        # descobre P0..P4 e monta a fila
python scripts\worker.py run            # processa até acabar — pode ficar dias
python scripts\99_status.py             # onde estamos

python scripts\control.py pause         # pausa graciosa (espera o item atual)
python scripts\control.py resume
```

Manual completo de operação: [`docs/05_OPERACAO.md`](docs/05_OPERACAO.md).

---

## Estrutura

```
cerebro-documental/
├── config/
│   ├── projeto.yaml           # caminhos, hidratação, limites do worker
│   └── classificacao.yaml     # regras de P0..P4 (obras + categorias documentais)
├── sql/schema_v2.sql          # 9 tabelas + 6 views + máquina de estados
├── scripts/
│   ├── cerebro_util.py        #   long path, guarda read-only, nuvem, estados, log
│   ├── 00_init_db.py          #   cria o banco
│   ├── 01_scan_inventario.py  #   inventário somente leitura
│   ├── 02_classificar.py      #   P0..P4 pela árvore
│   ├── extratores.py          #   pdf, docx, pptx, xlsx, csv, txt
│   ├── worker.py              #   o processador autônomo
│   ├── control.py             #   pause / resume / stop / status / texto
│   └── 99_status.py           #   fotografia completa
├── tests/                     # 98 checagens, nenhuma toca no SharePoint real
├── data/ derivados/ logs/     # dados operacionais (fora do Git)
├── output/                    # prioridades.md, pastas_classificadas.csv
└── docs/                      # checklist, arquitetura, esquema, plano, benchmark, operação
```

---

## Princípios

1. **Programação primeiro.** `código determinístico → extração → índice → heurística → IA`.
2. **Sem API de IA e sem upload externo** nesta fase.
3. **Escopo é o acervo inteiro.** P0..P4 é ordem, não recorte.
4. **Nada some do catálogo**: formato sem extrator vira `UNSUPPORTED`, não desaparece.
5. **Checkpoint por item.** Queda de energia custa um arquivo, não um dia.
6. **O SharePoint é read-only** — validado em teste com sha256 antes e depois.
7. **Python faz o repetitivo**; a IA entra para construir, diagnosticar e decidir.
