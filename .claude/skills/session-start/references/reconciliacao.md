# Reconciliação — quando o registro e a realidade divergem

Carregue este arquivo quando o passo 5 do `/session-start` encontrar
divergência, ou quando a sessão for retomar processamento em lote. Em
abertura de rotina ele não é necessário.

---

## Princípio

Evidência vence texto. Mas **mostrar a divergência é obrigatório** — consertar
em silêncio é pior que a divergência, porque apaga o sinal de que algo saiu do
trilho.

Ao relatar, diga sempre: o que o registro afirma, o que a realidade mostra,
qual dos dois você está adotando, e por quê.

---

## As seis checagens

### 1. O `STATE.md` está velho?

```bash
git log -1 --format='%h %ad %s' --date=short -- STATE.md
git log -1 --format='%h %ad %s' --date=short
```

Se o último commit do projeto é mais novo que o último commit do `STATE.md`,
houve trabalho não registrado. O cabeçalho do `STATE.md` cita um commit —
compare com o `HEAD`.

### 2. O "próximo passo" já foi feito?

Leia a seção e procure no `git log -15` por commit que a satisfaça. É comum a
tarefa ter sido feita e o `STATE.md` não ter sido atualizado — nesse caso o
próximo passo verdadeiro é o seguinte da lista, e isso precisa ser dito.

### 3. Alterações locais órfãs

```bash
git status --short
git diff --stat
git diff --cached --stat
```

Para cada arquivo modificado, pergunte: isto corresponde a alguma pendência
declarada? Se não, **pode ser trabalho da Emmily, não seu**. Nunca descarte,
nunca esconda, nunca faça stash. Liste e pergunte.

### 4. Os números batem?

O cabeçalho do `STATE.md` traz contagens (arquivos, `COMPLETED`, `MISSING`,
fila). Compare com o banco. Divergência significa que alguém processou,
sincronizou ou migrou sem atualizar o registro.

### 5. Registro de sessão mais novo que o estado

```bash
ls state/sessoes/
```

Se o último registro de sessão descreve trabalho que o `STATE.md` não reflete,
o `STATE.md` está atrasado — o registro de sessão é mais específico, mas o
`STATE.md` é o que manda sobre "onde estamos". Reconcilie dizendo os dois.

### 6. Testes: histórico × agora

`CLAUDE.md` lista os números esperados por suíte. **Esses são o último
resultado registrado, não uma verificação de agora.** Se você não rodou nesta
sessão, diga "último resultado registrado". Se rodou, diga "executado agora" e
mostre o número real, mesmo que seja pior.

---

## Processamento em lote — nunca recomeçar nem pular em silêncio

Quando a sessão for mexer na fila, levante antes:

| | consulta |
|---|---|
| universo | `SELECT COUNT(*) FROM arquivo` |
| por estado | `SELECT estado, COUNT(*) FROM arquivo GROUP BY estado` |
| fila real | `SELECT COUNT(*) FROM v_fila_nuvem` |
| já extraído | `SELECT COUNT(*) FROM extracao` |
| conteúdos | `SELECT COUNT(*) FROM conteudo` |
| último lote | `SELECT * FROM checkpoint ORDER BY id DESC LIMIT 1` |
| última execução | `SELECT * FROM execucao ORDER BY id DESC LIMIT 1` |
| erros por causa | `python scripts\operacao.py erros` |
| delta | `SELECT status, paginas, atualizado_em FROM delta_token` |

Regras: **nunca reprocessar o que já está `COMPLETED`**; nunca tratar
`UNSUPPORTED` como pendência (é estado terminal por decisão, não falha);
`MISSING` é registro histórico preservado, não erro.

---

## Armadilhas já pagas neste projeto

**`checkpoint.pendentes` mente.** Guarda o número de `v_fila`, que inclui 592
itens fora de escopo que o worker da nuvem nunca pega (`DEC-017`, `T-12`). Um
checkpoint `FILA_VAZIA` com `pendentes` alto é esse defeito, não trabalho
perdido. A fila verdadeira é `v_fila_nuvem`.

**O banco pode estar à frente do Git.** Scripts como `17_aplicar_delta.py` e
`19_ingerir_novos.py` escrevem no banco; o banco está fora do Git. Working
tree limpa **não** quer dizer que nada aconteceu.

**O lock.** `data/cerebro.db.lock` com dono vivo significa worker rodando
agora — não suba outro. Dono morto: o núcleo já devolveu o lock, o conteúdo
do arquivo ainda dizer "tomado" é normal.

**Heartbeat parado com lock tomado** = item pendurado no download (`T-18`).
Está documentado em `docs/10_OPERACAO_AUTONOMA.md`.

**`PRAGMA integrity_check` leva minutos.** Não é checagem de abertura.
