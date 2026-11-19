# Persistência — onde cada coisa vai, e os modelos

Carregue no passo 4 do `/session-close`. O mapa de autoridade está em
`docs/12_CONTINUIDADE.md`; aqui está o **procedimento** e os **modelos**.

---

## A triagem

| O que é | Vai para | Cuidado |
|---|---|---|
| Onde o projeto está agora | cabeçalho do `STATE.md` | **substituir**, não acumular |
| Próxima ação concreta | "Próximo passo exato" do `STATE.md` | reescrever inteira |
| Pendência nova | tabela `T-xx` do `STATE.md` | próximo número livre |
| Pendência resolvida | mesma tabela, `~~T-xx~~` + como foi fechada | não apagar a linha |
| Decisão arquitetural durável | tabela `DEC-xxx` do `STATE.md` | próximo número; só decisão de verdade |
| O que esta sessão fez | `state/sessoes/NNN-nome.md` (novo) | é aqui que o histórico mora |
| Conhecimento de domínio | `docs/NN_*.md` | mede o acervo, explica o domínio |
| Regra permanente nova | `CLAUDE.md` | raro; exige decisão explícita dela |
| Estado do processamento | já está no banco | não copiar número para `.md` sem data |

**Não duplique.** Se o fato cabe num lugar só, use um lugar só. O registro de
sessão pode citar `DEC-041`, não repeti-la.

---

## Regra contra o diário

O `STATE.md` deste projeto já passou de 670 linhas por acumular histórico. Ele
deve responder **"onde estamos agora"**, não "o que já fizemos". Ao encerrar:

- **substitua** o cabeçalho e o "Próximo passo exato" — não acrescente;
- remova do `STATE.md` o que virou passado e já está no registro da sessão;
- não crie seção nova de histórico lá dentro.

Se precisar contar uma história longa, ela vai para `state/sessoes/`.

---

## Decisão durável — o teste antes de criar um `DEC-`

Só é `DEC-` o que **restringe escolhas futuras**. Se a próxima sessão puder
decidir diferente sem prejuízo, não é decisão durável — é só o que você fez.

Registre: contexto, a decisão, o motivo, o que foi descartado e a
consequência. Uma linha densa na tabela basta; o projeto já tem 39 exemplos.

---

## Modelo do registro de sessão

`state/sessoes/NNN-nome-curto.md` — **curto e de alto sinal**. Se passar de
uma tela, provavelmente é documentação disfarçada e pertence a `docs/`.

```markdown
# Sessão NNN — <nome>

**Data:** AAAA-MM-DD · **HEAD final:** `<curto>` · **Branch:** main

## Objetivo
Uma frase: o que esta sessão se propôs a fazer.

## Resultado
- feito e validado: ...
- parcial: ... (o que falta, exatamente)
- investigado e NÃO implementado: ...

## Descobertas
O que a próxima sessão perderia tempo redescobrindo.

## Decisões
`DEC-xxx` — uma linha cada. (A decisão inteira mora no STATE.md.)

## Validação
comando exato → resultado real. Inclusive o que não foi executado, e por quê.

## Pendências abertas
`T-xx` — uma linha cada.

## Próximo passo
Uma ação executável, com critério de conclusão.
```

Numeração: `ls state/sessoes/` → maior + 1, três dígitos. Pasta vazia → `001`.

---

## Processamento em lote

Se a sessão mexeu na fila, o registro precisa dizer **exatamente** onde parar
e retomar:

- universo, inventariados, processados, validados, pendentes;
- último lote concluído e próximo lote;
- erros por categoria (`python scripts\operacao.py erros`);
- `UNSUPPORTED`, retries, prioridades restantes;
- estado do worker e do lock ao encerrar.

O banco já guarda tudo isso. O registro de sessão guarda **a interpretação**:
o que estava acontecendo e por quê.

---

## Antes de fechar — a varredura de segurança

- nenhum segredo em arquivo versionado; `.env` continua ignorado;
- nada escrito dentro de `C:\caminho\exemplo\acervo\` (REGRA 1);
- `tmp/` sem temporários `e7_*` órfãos;
- worker num estado explícito (`PAUSADO` ou `PARADO`), não no meio de nada;
- lock liberado;
- alterações da Emmily que não são suas: **preservadas e listadas**, nunca
  escondidas.
