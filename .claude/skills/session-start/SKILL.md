---
name: session-start
description: Protocolo de ABERTURA de sessão do Cérebro Documental. Reconstrói o contexto pelo repositório e pelo banco — nunca pela conversa anterior — reconcilia com Git e disco, apresenta o briefing e PARA. Use ao abrir qualquer sessão neste projeto, ao invocar /session-start, ou quando perguntarem "onde paramos". Não implementa, não edita, não commita.
---

# /session-start — reconstruir antes de desenvolver

A conversa anterior não existe. Tudo que você precisa está no repositório, no
Git e no banco. Se não estiver, **isso é um defeito do projeto** — nomeie o
defeito em vez de preencher a lacuna com suposição.

## Proibido nesta fase

Não edite arquivo, não implemente, não corrija "rapidinho", não atualize
documentação, não instale dependência, não commite, não faça stash, não
resete, não apague. **Só leitura.**

Teste você pode rodar — são offline e não destrutivos (`docs/12_CONTINUIDADE.md`)
— mas só se for estabelecer baseline, e dizendo que foi executado agora.

**Não rode `PRAGMA integrity_check`** (leva minutos) nem nada que processe a
fila.

---

## O roteiro

Leia tudo antes de falar. Não narre enquanto lê.

### 1. Onde estou

```bash
pwd && git rev-parse --show-toplevel
git branch --show-current && git rev-parse HEAD && git rev-parse --short HEAD
git status --short
git log --oneline -8
```

Confirme que é o `cerebro-documental`. Se não for, pare e diga.

### 2. Regras — a maior autoridade primeiro

`CLAUDE.md` inteiro. São as REGRAS 1–9 e elas mandam em tudo que você fizer
depois. Em especial: SharePoint é read-only, a fonte é a nuvem, commit só a
pedido.

### 3. Estado atual — por partes, não o arquivo inteiro

`STATE.md` tem ~670 linhas e é majoritariamente histórico. Leia **só**:

- o cabeçalho (primeiras ~20 linhas): Git, testes, banco, worker;
- a seção **"Próximo passo exato"**;
- a seção **"Pendências abertas"**.

Desça para as seções históricas apenas se a próxima tarefa exigir.

### 4. O estado que não está em arquivo

Aqui está a diferença deste projeto. Rode:

```bash
python scripts\cerebro.py status
```

Traz worker, fila, estados, ritmo e ETA **medidos**. Se o comando falhar mas
o banco existir, use as consultas diretas de `docs/12_CONTINUIDADE.md`.

**Se `data/cerebro.db` não existir** — clone novo, máquina nova — não é erro:
o banco está fora do Git de propósito. Diga isso com todas as letras no
briefing: *o código e a história estão aqui, o estado do processamento não*.
O catálogo é reconstruível (`00_init_db` → `12_scan_graph` → `13_reconciliar`),
mas **o trabalho já extraído não é**, porque `derivados/` também está fora do
Git. Nessa situação a sessão começa por decidir com a Emmily se é para
reconstruir ou se o banco vem de um backup — **não reconstrua por conta
própria**, e não apresente o acervo como se estivesse catalogado.

### 5. Reconciliar

Compare registro × realidade e **mostre toda divergência**, sem consertar:

- `STATE.md` cita um commit que não é o `HEAD`?
- o "próximo passo" já aparece feito no `git log`?
- há arquivo modificado que não corresponde a nenhuma pendência declarada?
- os números do banco batem com os do cabeçalho do `STATE.md`?
- `state/sessoes/` tem registro mais recente que o `STATE.md`?

O detalhe de cada checagem, e as armadilhas conhecidas, estão em
[references/reconciliacao.md](references/reconciliacao.md) — **carregue esse
arquivo só quando encontrar divergência** ou quando a sessão for retomar
processamento em lote.

### 6. A próxima unidade de trabalho

A **menor** unidade coerente, não um replanejamento. Normalmente ela já está
escrita em "Próximo passo exato". Determine: objetivo, critério de conclusão,
arquivos prováveis, dependências, riscos, verificação esperada.

Antes de perguntar qualquer coisa à Emmily, procure a resposta no repositório,
no Git, no banco e nos `docs/`. Pergunte só o que depende de decisão dela.

### 7. Numerar a sessão

`ls state/sessoes/` → próximo número é o maior + 1, com três dígitos. Se a
pasta estiver vazia, esta é a `001`. Proponha `NNN - Nome objetivo` e ofereça
`/rename`, sem executar nada.

---

## O briefing

Apresente, omitindo o que não se aplicar (não invente seção vazia):

```
PROJETO · SESSÃO (número, nome sugerido, objetivo)
GIT (branch, HEAD, status)
BANCO / PROCESSAMENTO (worker, fila, estados, último checkpoint)
CONCLUÍDO · PARCIAL · EM ANDAMENTO · PENDENTE
PRÓXIMA TAREFA EXATA + critérios de conclusão
ARQUIVOS DE ALTO SINAL  (e quais NÃO precisa reler)
DECISÕES QUE RESTRINGEM ESTA TAREFA  (DEC-xxx)
VALIDAÇÃO — último resultado registrado × executado agora
INCONSISTÊNCIAS ENCONTRADAS
RISCOS / BLOQUEIOS
```

Termine exatamente com:

**"Contexto reconstruído. Nenhuma alteração foi realizada."**

E **pare**. Não comece a próxima tarefa. Espere ela autorizar.
