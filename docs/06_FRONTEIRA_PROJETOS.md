# Fronteira entre os dois projetos do SharePoint

Existem dois projetos trabalhando sobre o **mesmo acervo**, com papéis opostos.
Este documento define quem faz o quê, o que foi separado e o que — por
enquanto — continua compartilhado.

| | **cerebro-documental** (este) | **PROG_Organizacao_SharePoint** |
|---|---|---|
| Papel | **entender** o acervo | **arrumar** o acervo |
| Acesso ao SharePoint | **somente leitura**, garantido por código | leitura **e escrita** |
| Produto | índice pesquisável, texto extraído, perfis | renomeação, movimentação, remoção de duplicata |
| Banco | `data/cerebro.db` | `data/organizacao.db` |
| Identidade do arquivo | `item_id` do Graph | `item_id` do Graph |
| Quem decide apagar/mover | **ninguém aqui** | a Emmily, com base nos relatórios de lá |

> **Regra de ouro:** este projeto nunca escreve no SharePoint. Não é promessa de
> documentação — `scripts/graph_ro.py` recusa qualquer método HTTP diferente de
> GET, e `tests/test_graph_ro.py` lê a própria AST do módulo e falha se
> aparecer escrita.

---

## O que foi separado (2026-09-20)

| Item | Antes | Agora |
|---|---|---|
| Cliente do Graph | `graph_util.py` do outro repo | **`scripts/graph_ro.py`** próprio, só leitura |
| Credenciais | `.env` do outro repo | **`.env` próprio**, protegido no `.gitignore` |
| Teste de acesso | `10_testar_graph.py` do outro repo | **`tests/test_graph_ro.py`** (25 checagens) |
| Mapa de drives | não existia | **`config/mapa_drives.yaml`**, gerado aqui |

Verificação: `grep -rn "Organizacao_SharePoint\|graph_util" scripts/ tests/` só
encontra menções em comentário. **Nenhum import cruza a fronteira.**

---

## O que ainda é compartilhado — e o que fazer

### O registro de aplicativo no Entra ID

Os dois projetos usam **a mesma credencial**, que tem `Sites.ReadWrite.All`.
Este projeto só precisa de leitura. O código garante isso, mas a **credencial**
não: se alguém colar um `PATCH` em outro script deste repo, o token aceita.

> **Recomendação para a TI:** um segundo registro de aplicativo, só leitura
> (`Sites.Selected` com role `read`), para o Cérebro. É a diferença entre
> "nosso código não escreve" e "esta credencial não consegue escrever".

Enquanto isso não existe, o `.env` daqui guarda a mesma credencial — e o
segredo deve ser **rotacionado** pela TI assim que a migração estabilizar.

### Fatos do site (não são credencial)

`site_id`, `drive_id` e a lista de bibliotecas ficam em `config/mapa_drives.yaml`
de cada projeto. Cada um gera o seu; são leitura pública dentro do tenant.

---

## Por que o Cérebro faz o próprio inventário

O projeto de organização já tem `20_inventario.py` (delta + `quickXorHash`) e
`30_duplicatas.py`. Seria tentador o Cérebro apenas ler o `organizacao.db`.
**Não vamos fazer isso**, por três motivos:

1. **O inventário do Cérebro carrega estado que o outro não tem:** `QUEUED`,
   `COMPLETED`, `OCR_REQUIRED`, tentativas, derivados, trechos indexados. Não é
   a mesma tabela com colunas a mais — é outro ciclo de vida.
2. **Acoplamento operacional.** O worker roda por dias, sozinho. Se dependesse
   do outro projeto ter rodado antes, a fila pararia por um motivo que não é
   dela.
3. **O custo é baixo.** O delta completo são ~140 chamadas paginadas (999 itens
   por página) — minutos, não horas. Duplicar isso é mais barato que acoplar.

O que **não** vamos duplicar: a decisão sobre duplicatas. Aqui, duplicata serve
para **pular extração** (mesmo conteúdo, não processa duas vezes). Lá, serve
para **decidir o que apagar**. Mesmo fato, usos diferentes, donos diferentes.

---

## O ponto crítico: a organização vai mover arquivos

Quando o outro projeto renomear ou mover um documento, **o caminho muda**. Um
índice que usa caminho como identidade quebra em silêncio.

Por isso a migração do Cérebro para `item_id` como identidade **não é
opcional**: é o que permite os dois projetos operarem no mesmo acervo sem que
um invalide o trabalho do outro.

```
organizacao move o arquivo
        │
        ▼
delta do Cerebro traz o MESMO item_id com caminho novo
        │
        ▼
atualiza `caminho_rel`, mantém COMPLETED, texto e índice
        │
        ▼  (o conteúdo não mudou: quickXorHash igual)
nada é reprocessado
```

Sem `item_id`, esse mesmo evento seria lido como "sumiu um arquivo e apareceu
outro" — e reprocessaríamos o acervo inteiro a cada faxina.

---

## Contrato mínimo entre os projetos

1. O Cérebro **lê**; a Organização **escreve**. Nenhum dos dois invade o outro.
2. Identidade comum: **`item_id` do Graph**. Caminho é atributo, não identidade.
3. Nenhum dos dois apaga linha de banco: mudança vira evento com data.
4. Credencial só em `.env`, um por projeto, nunca em código, log ou commit.
5. Se a Organização fizer uma faxina grande, o Cérebro roda o delta depois —
   e o resultado esperado é **zero reprocessamento**, só atualização de caminho.
