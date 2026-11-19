# Cérebro Documental — regras para qualquer sessão neste repositório

---

## ⛔ REGRA Nº 1 — O SHAREPOINT É READ-ONLY

```
C:\caminho\exemplo\acervo\
```

Tudo abaixo deste caminho é **somente leitura, sem exceção**. É proibido, lá dentro:
excluir, renomear, mover, alterar, sobrescrever, **criar** arquivo ou pasta,
reorganizar diretórios — qualquer coisa que o OneDrive sincronize de volta.

Toda escrita acontece **dentro deste repositório**: banco em `data/`, textos e
perfis em `derivados/`, logs em `logs/`, relatórios em `output/`.

`cerebro_util.assert_escrita_permitida(destino, raiz)` levanta `PermissionError`
se um caminho de escrita cair dentro do SharePoint. **Chamar em todo script que
escreve arquivo.**

Única exceção, já validada e autorizada: **liberar espaço** (`attrib +U -P`,
o mesmo que o Explorer faz em *Liberar espaço*). Não apaga, não move, não
renomeia, não altera conteúdo — só o marcador local de cache do OneDrive.
Prova: `python tests\test_hidratacao.py --confirmar` → sha256 idêntico antes e
depois, tamanho e datas intactos.

---

## ⛔ REGRA Nº 2 — A FONTE É A NUVEM (desde 2026-09-20)

O acervo é lido pelo **Microsoft Graph**, não pelo disco.

- **Fonte única de verdade:** `scripts/graph_ro.py`. A pasta sincronizada local
  **não é fonte nem cache** — não ler de lá (`DEC-012`).
- **Identidade canônica: `(drive_id, item_id)`.** Caminho e nome são atributos
  mutáveis. Nenhuma chave importante pode depender de caminho (`DEC-015`).
- **Acabou a hidratação** (`DEC-013`): conteúdo vem por download temporário
  para `tmp/`, é usado e apagado no `finally`. Sem orçamento de disco, sem
  mexer no cache do OneDrive.
- **Duplicata e integridade usam o `quickXorHash` do servidor** (`DEC-018`),
  que vem de graça no inventário. `scripts/quickxor.py` calcula o mesmo hash
  localmente para conferir download. **Duplicata tem níveis de evidência** —
  `quick_xor` + tamanho é `FORTE`, não é "confirmada": ver
  `docs/07_POLITICA_DUPLICATAS.md` antes de usar duplicata para pular trabalho.
- **A porta é somente leitura por construção** (`DEC-014`): `graph_ro` recusa
  qualquer método HTTP diferente de GET, e `tests/test_graph_ro.py` lê a AST do
  módulo e falha se aparecer escrita. Escrita no SharePoint é do projeto
  `PROG_Organizacao_SharePoint` — ver `docs/06_FRONTEIRA_PROJETOS.md`.

O código de hidratação (`pode_hidratar`, `garantir_local`, `liberar_espaco`)
continua em `cerebro_util.py` e testado em `tests/test_hidratacao.py`, mas
**está fora do fluxo**. Não usar em código novo.

---

## ⚠️ REGRA Nº 3 — MAX_PATH

Caminhos do SharePoint passam de 260 caracteres. **Sempre** acessar via
`cerebro_util.lp(path)` (prefixo `\\?\`). Em PowerShell, `-LiteralPath`.

---

## ⚠️ REGRA Nº 4 — heredoc do Bash corrompe backslash

O tool Bash deste ambiente colapsa `\\` em `\` dentro de heredocs.
**Escrever arquivos `.py` com Write/Edit, nunca com `cat > arquivo.py <<'EOF'`.**

---

## REGRA Nº 5 — PROGRAMAÇÃO PRIMEIRO, IA POR ÚLTIMO

Ordem obrigatória:

```
código local determinístico -> extração estruturada -> indexação
      -> heurística -> IA só quando realmente necessária
```

Não usar IA para o que Python resolve: contagem, tamanho, hash, data, extensão,
duplicata exata, número de páginas, detecção de PDF escaneado, agrupamento por
nome normalizado, classificação por nome de pasta.

**Nesta fase não há chamada de API de IA e nenhum documento sai desta máquina.**

Corolário operacional: se uma ação é determinística e se repete milhares de
vezes, ela é do `worker.py`, não de uma sessão de IA. O Claude constrói,
diagnostica, melhora e decide o que exige raciocínio — o Python executa.

### ⛔ O processamento massivo NUNCA depende de IA (`DEC-039`)

Isto não é uma preferência desta fase: é a arquitetura. O pipeline de produção
é inteiramente determinístico e roda sem nenhuma sessão aberta:

```
Graph -> inventario -> fila SQLite -> download temporario -> validacao
      -> extrator Python -> TXT/JSON/SQLite/FTS -> limpeza -> proximo
```

Dentro desse laço é **proibido**: chamar API de IA, enviar documento para
Claude ou para qualquer serviço externo, decidir arquivo por arquivo com IA,
ou depender de uma sessão de IA estar aberta. Um worker que precise de IA para
processar um item está errado por construção — a decisão dele tem de caber numa
regra determinística, e se não couber, o item vira estado explícito
(`UNSUPPORTED`, `OCR_REQUIRED`, `ERROR_*`) e espera.

Claude serve para: escrever código, revisar arquitetura, criar regras e testes,
diagnosticar erro, melhorar desempenho, analisar relatório agregado, e um dia
construir a camada inteligente **por cima** do acervo já indexado. Nunca para
processar o acervo.

---

## REGRA Nº 6 — NADA SOME DO CATÁLOGO

- Formato sem extrator → `UNSUPPORTED` (inventariado, tipo registrado, esperando).
- Arquivo que sumiu → `MISSING` com `presente=0`. **Nunca `DELETE`.**
- Arquivo que falhou → `ERROR_RETRYABLE` → `ERROR_FINAL`, com a mensagem.
- Toda mudança vira linha em `evento_arquivo`.
- Um arquivo problemático **nunca** pode travar a fila nem derrubar o worker.

---

## REGRA Nº 7 — ESTADO EXPLÍCITO, NUNCA INFERÊNCIA

A máquina de estados está documentada em `sql/schema_v2.sql` e em
`docs/05_OPERACAO.md`. Toda pergunta do tipo "quanto falta?" se responde com
`SELECT * FROM v_progresso` ou `v_fila` — não com estimativa.

Ao criar estado novo: registrar nos dois lugares e em `cerebro_util.ESTADOS`.

---

## REGRA Nº 8 — CHECKPOINT É POR ITEM

Cada item processado é **uma transação**: resultado + estado + evento +
índice, tudo junto, e commit. Se a luz cair no meio, o item volta para a fila
inteiro. Nunca escrever derivado sem gravar o estado na mesma transação.

Na subida, o worker devolve para a fila tudo que ficou em `HYDRATING`,
`EXTRACTING` ou `EXTRACTED` (`cu.ESTADOS_ORFAOS`).

---

## REGRA Nº 9 — RASTREABILIDADE

Toda informação aponta para `caminho_rel` + `sha256` + data + `execucao_id`.
Perfil JSON e texto derivado guardam a versão do extrator que os produziu —
mudou a versão, aquele extrator reprocessa; os outros não.

---

## Abertura e encerramento de sessão

```
/session-start   ->  reconstrói o contexto pelo repositório e PARA
/session-close   ->  persiste o que só existe na conversa e PARA
```

**A conversa é descartável; o repositório é a fonte canônica.** Uma sessão
pode terminar por completo e outra continuar — outra janela, outra conta,
outro computador, meses depois — sem nenhum histórico de conversa.

`/session-start` é só leitura: não implementa, não edita, não commita.
`/session-close` não inicia tarefa nova e **não commita por iniciativa
própria** (ver *Versionamento*, abaixo).

O mapa de onde cada tipo de informação mora está em
[`docs/12_CONTINUIDADE.md`](docs/12_CONTINUIDADE.md) — inclusive o estado que
vive no SQLite e não em arquivo. Histórico de sessões: `state/sessoes/`.

---

## Como o trabalho anda

- Etapas pequenas e executáveis, cada uma com **um teste objetivo**.
- Erro que aparecer: diagnosticar **o erro atual**, não redesenhar o projeto.
- `STATE.md` é a verdade sobre onde o projeto está. Manter atualizado.
- Scripts numerados por ordem de execução em `scripts/`.
- Nada de arquitetura sofisticada sem medição que justifique.

### Testes

```bash
python tests\test_scanner.py         #  48 PASS — inventário local e classificação P0..P4
python tests\test_worker.py          #  35 PASS — processamento ponta a ponta
python tests\test_delta.py           #  68 PASS — inventário por delta (Graph falso)
python tests\test_reconciliacao.py   #  52 PASS — migração sem perder trabalho
python tests\test_worker_nuvem.py    # 178 PASS — worker na nuvem: teto e prazo antes/no download
python tests\test_aplicar_delta.py   #  35 PASS — do espelho para o catálogo, sem perder nada
python tests\test_ingestao.py        #  77 PASS — itens novos entram sem duplicar
python tests\test_extratores.py      # 419 PASS — o contrato de cada extrator (E9, pdf/1.3)
python tests\test_busca.py           # 306 PASS — busca v5, corte, migração 05
python tests\test_operacao.py        # 131 PASS — lock, orçamento, janela, métricas, cobertura
python tests\test_schema.py          #  45 PASS — a receita recria o schema de produção
python tests\test_dedupe.py          # 112 PASS — representante, contador, backfill, T-17
python tests\test_worker_v5.py       #  24 PASS — o worker alimentando a busca v5
python tests\test_worker_formatos.py #  83 PASS — formato sem leitor não é baixado
python tests\test_worker_robustez.py #  58 PASS — queda em série, credencial, quickXor, heartbeat
python tests\test_graph_ro.py        #  57 PASS — a porta da nuvem é só de leitura  [rede]
python tests\test_quickxor.py        #   9 PASS — o hash bate com o do servidor     [rede]
```

Offline (sem rede): 48+35+68+52+178+35+77+419+306+131+45+112+24+83+58 = **1.671**.
Com rede: `test_graph_ro` 57 (49 deles rodam sem rede com `--sem-rede`) +
`test_quickxor` 9. Reproduções de defeito, fora da conta:
`python tests\reproducoes_qa.py` → "0 defeito(s) ainda reproduz(em)".

Congelado (testa mecanismo aposentado pela `DEC-013`), não faz parte da conta:
`python tests\test_hidratacao.py --confirmar` → 22 PASS.

---

## Versionamento

Commit **só quando a Emmily pedir explicitamente** ("salva essa versão",
"commita", "atualiza o git", `/versionar`).

Checkpoint operacional (do processamento) e commit de Git são coisas
**diferentes**. O worker grava checkpoint o tempo todo; isso nunca vira commit.

Nunca: `push`, `reset --hard`, `clean -fd`, reescrita de histórico, troca de
branch.

**Fora do Git:** `data/*.db`, `derivados/`, `logs/*.jsonl`, `tmp/`, arquivos
hidratados. São dados reconstruíveis, não código.
