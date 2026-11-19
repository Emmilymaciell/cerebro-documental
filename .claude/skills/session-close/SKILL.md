---
name: session-close
description: Protocolo de ENCERRAMENTO de sessão do Cérebro Documental. Para o desenvolvimento, reconcilia com Git e banco, valida o que foi feito, persiste no repositório tudo que só existe nesta conversa, atualiza STATE.md e o registro da sessão, e PARA. Use ao invocar /session-close ou quando quiserem encerrar/fechar a sessão. Não inicia tarefa nova e não commita por iniciativa própria.
---

# /session-close — nada importante pode morrer com a conversa

Pergunta que governa tudo aqui:

> **Se esta conversa sumir agora, alguma informação importante some junto?**

Se a resposta for sim, persista antes de encerrar.

## Proibido nesta fase

Não comece funcionalidade nova, não inicie a próxima tarefa, não refatore de
oportunidade, não amplie escopo, não conserte coisa não relacionada. Não
descarte, esconda ou resete trabalho — nem com `stash`.

E, acima de tudo: **não declare o que não aconteceu.** Teste não executado é
"não executado". Investigação não é implementação. Intenção não é resultado.
Hipótese não vira fato.

---

## O roteiro

### 1. Parar e olhar o terreno

```bash
git branch --show-current && git rev-parse HEAD && git rev-parse --short HEAD
git status --short && git diff --stat
git log --oneline -10
```

Compare com o banco (`python scripts\cerebro.py status`). A realidade
verificável prevalece sobre qualquer resumo.

### 2. Auditar a sessão

Percorra o que aconteceu e classifique com honestidade: concluído ·
parcialmente concluído · investigado mas não implementado · decisão tomada ·
descoberta · hipótese · hipótese descartada · bug encontrado · limitação ·
risco · pendência · bloqueio · pergunta aberta.

Para cada item, pergunte onde ele mora depois que a conversa sumir.

### 3. Validar o que foi feito

Rode as verificações **pertinentes ao trabalho desta sessão** — não a bateria
inteira por reflexo. A lista está em `docs/12_CONTINUIDADE.md`. Se mexeu no
worker, `test_worker` e `test_worker_nuvem` são obrigatórios.

Registre **comando exato** e **resultado real**. Não pôde rodar algo? Registre
"não executado", o motivo, e o risco que fica.

### 4. Persistir

Cada tipo de informação tem um lugar certo, e duplicar é ruim. O mapa está em
[`docs/12_CONTINUIDADE.md`](../../../docs/12_CONTINUIDADE.md); o procedimento
e os modelos prontos estão em
[references/persistencia.md](references/persistencia.md) — **carregue essa
referência agora**, é o núcleo desta skill.

Em resumo: `STATE.md` é **só o estado de agora** (não deixe virar diário);
o histórico vai para `state/sessoes/NNN-nome.md`; decisão durável vira linha
`DEC-xxx`; pendência vira linha `T-xx`; conhecimento de domínio vai para
`docs/`.

### 5. Git — e aqui tem regra forte

**Commit só quando a Emmily pedir explicitamente.** Está em `CLAUDE.md` e na
configuração global dela. Terminar a sessão **não** é pedido de commit.

Então: deixe a árvore coerente, mostre `git status` e `git diff --stat`, diga
o que um commit incluiria e proponha a mensagem — **e espere**. Se ela já
tiver pedido nesta sessão, siga `/versionar`.

Nunca `push`, `reset --hard`, `clean -fd`, reescrita de histórico ou troca de
branch.

### 6. Definir a próxima unidade

Uma ação executável. Não vale "continuar o trabalho" nem "seguir a análise".
Com objetivo, critério de conclusão, arquivos de alto sinal, dependências,
riscos e verificação esperada.

---

## O relatório

```
SESSÃO (número, nome, objetivo)
REPOSITÓRIO (diretório, branch, HEAD, working tree)
CONCLUÍDO · PARCIAL · INVESTIGADO E NÃO IMPLEMENTADO
DESCOBERTAS · DECISÕES DURÁVEIS · HIPÓTESES E PERGUNTAS ABERTAS
VALIDAÇÕES EXECUTADAS (comando → resultado real)
ALTERAÇÕES NÃO COMMITADAS
DOCUMENTAÇÃO ATUALIZADA
ESTADO DO PROCESSAMENTO (worker, fila, checkpoint)
INCONSISTÊNCIAS ENCONTRADAS
PRÓXIMO PASSO EXATO
ARQUIVOS DE ALTO SINAL  ·  RISCOS / BLOQUEIOS
GIT FINAL (branch, HEAD, status)
```

Termine exatamente com:

**"Handoff concluído. Nenhuma nova implementação foi iniciada."**

E **pare**.
