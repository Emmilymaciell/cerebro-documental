# Continuidade — onde cada coisa mora

> **A conversa é descartável. O repositório é a fonte canônica.**
>
> Uma sessão pode terminar por completo — outra janela, outra conta, outro
> computador, meses depois, contexto perdido — e o trabalho continua. Isto
> aqui é o mapa que torna isso verdade. É a referência comum das skills
> `/session-start` e `/session-close`: elas apontam para cá em vez de repetir
> o mapa dentro de cada uma.

---

## A regra de ouro

**Evidência verificável vence texto.** Um resumo em Markdown é orientação;
Git, disco e banco são fato. Quando divergirem, **mostre a divergência** — não
conserte em silêncio e não escolha o que for mais conveniente.

Neste projeto há uma peculiaridade que muda tudo: **boa parte do estado real
não está em arquivo, está no SQLite**. Quem lê só os `.md` não sabe se o worker
está pausado, quantos itens faltam na fila, nem onde o último lote parou.

---

## Quem é autoridade sobre o quê

| Pergunta | Fonte canônica | Nunca |
|---|---|---|
| Como se trabalha aqui? | `CLAUDE.md` (REGRAS 1–9) | não duplicar regra em outro lugar |
| Onde o projeto está **agora**? | `STATE.md` — cabeçalho, "Próximo passo exato", "Pendências abertas" | não é diário |
| Por que está assim? | tabela **`DEC-xxx`** em `STATE.md` | decisão registrada não se reabre sem motivo técnico novo |
| O que falta? | tabela **`T-xx`** em `STATE.md` + `docs/11_QA_AUDITORIA.md` | |
| O que **realmente** aconteceu? | `git log` | mensagem de commit não se reescreve |
| Como está o **processamento**? | **`data/cerebro.db`** — ver abaixo | nunca estimar (REGRA 7) |
| O que cada sessão fez? | `state/sessoes/NNN-*.md` | um arquivo por sessão, curto |
| Conhecimento de domínio | `docs/00..11` | |
| Última sincronização | `logs/sincronizacao_*.json` | |

**`docs/decisoes/` existe e está vazia.** A tabela `DEC-xxx` do `STATE.md` já
cumpre essa função e funciona bem. Não migre as decisões para lá sem decidir
isso explicitamente — e, se migrar, atualize este mapa.

---

## O estado que mora no banco, não em arquivo

`data/cerebro.db` (fora do Git, ~940 MB, schema v4). As consultas abaixo são
**rápidas** e são as que as skills usam:

```sql
SELECT valor FROM meta WHERE chave='schema_versao';
SELECT comando, estado, motivo, heartbeat FROM controle;      -- o worker
SELECT estado, COUNT(*) FROM arquivo GROUP BY estado;          -- o universo
SELECT COUNT(*) FROM v_fila_nuvem;                             -- a fila real
SELECT COUNT(*) FROM extracao;
SELECT etapa, status, iniciada_em FROM execucao ORDER BY id DESC LIMIT 1;
SELECT em, estado_worker, concluidos, pendentes
  FROM checkpoint ORDER BY id DESC LIMIT 1;
SELECT status, paginas, atualizado_em FROM delta_token;
```

Tudo isso junto leva **menos de 10 segundos**.

> ⚠️ **`PRAGMA integrity_check` leva minutos** neste banco. Não rode na
> abertura de sessão. Só vale a pena depois de migração ou de escrita em massa.

O atalho pronto, que já traz ritmo e ETA **medidos**:

```bash
python scripts\cerebro.py status
```

### Armadilha conhecida

`checkpoint.pendentes` guarda o número de `v_fila` (que inclui 592 itens fora
de escopo que o worker da nuvem nunca pega — `DEC-017`, `T-12`). A fila
verdadeira é `v_fila_nuvem`. Um checkpoint antigo dizendo `FILA_VAZIA` com
`pendentes` alto é esse defeito, não trabalho perdido.

---

## Validação — o que existe de verdade

Não há CI, lint, typecheck nem build. A validação deste projeto é:

```bash
# offline, sem rede — a bateria que importa
python tests\test_scanner.py        python tests\test_worker.py
python tests\test_delta.py          python tests\test_reconciliacao.py
python tests\test_worker_nuvem.py   python tests\test_aplicar_delta.py
python tests\test_ingestao.py       python tests\test_extratores.py
python tests\test_busca.py          python tests\test_operacao.py

# tocam a nuvem de verdade (precisam do .env)
python tests\test_graph_ro.py       python tests\test_quickxor.py
```

Os números esperados estão em `CLAUDE.md`. **Resultado histórico não é
verificação executada agora** — ao relatar, diga qual dos dois é.

---

## Política de Git

**Commit só quando a Emmily pedir explicitamente** ("salva essa versão",
"commita", "atualiza o git", `/versionar`). Está em `CLAUDE.md` e na
configuração global dela. `/session-close` **não commita por iniciativa
própria** — prepara, mostra, e espera o pedido.

Nunca: `push`, `reset --hard`, `clean -fd`, reescrita de histórico, troca de
branch.

Fora do Git: `data/*.db`, `derivados/`, `logs/*.jsonl`, `logs/*.json`, `tmp/`,
`data/backup/`, `*.lock`, `.env`.

---

## Relação com as skills genéricas da plataforma

A Emmily tem o plugin `os@emmily-platform`, com `/os:resume`, `/os:status`,
`/os:handoff` e `/os:checkpoint`. São boas, mas **genéricas**: assumem
`docs/STATE.md`, `docs/decisions/` e `project.yaml`, que **não** é o layout
deste projeto, e não sabem olhar o SQLite.

`/session-start` e `/session-close` são a versão deste projeto. Não substituem
as genéricas; convivem (nomes qualificados não colidem). Use:

- `/session-start` e `/session-close` — aqui, sempre;
- `/os:handoff` — quando for passar o projeto para **outra pessoa**, que é
  outro problema (documento de entrega, não retomada);
- `/versionar` ou `/os:checkpoint` — quando ela pedir o commit.
