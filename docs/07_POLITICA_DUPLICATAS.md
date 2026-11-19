# Política de duplicatas

Escrita em 2026-09-20, antes do E7, para que "duplicata" tenha um significado
único e verificável em todo o projeto.

> **Nada aqui autoriza apagar, mover ou esconder arquivo.** Ação destrutiva é do
> projeto `PROG_Organizacao_SharePoint`. Aqui, duplicata serve para **não
> extrair o mesmo conteúdo duas vezes** e para **responder onde mais ele está**.

---

## 1. O que é o `quickXorHash` — e o que ele não é

| | |
|---|---|
| O que é | 160 bits, XOR com deslocamento circular de 11 bits sobre o conteúdo, e o **tamanho do arquivo** entra por XOR no fim. Calculado **pelo servidor**, vem de graça na listagem. |
| O que **não** é | **Não é hash criptográfico.** Uma colisão pode ser construída de propósito por quem queira. Não serve para provar integridade contra adulteração. |
| Onde conferimos | `scripts/quickxor.py` reproduz o mesmo hash localmente — validado em 20/20 arquivos reais contra o servidor (`tests/test_quickxor.py`). |

**Medido neste acervo (2026-09-20):** 137.820 arquivos têm hash do servidor, e
**nenhum hash aparece com mais de um tamanho** (0 casos em 137.820). Isso é
esperado, já que o tamanho faz parte do hash — mas continuamos comparando
tamanho **explicitamente**, porque é barato e torna o critério legível.

**`sha1Hash`: o Graph não fornece neste tenant** — 0 de 137.857 itens. É
esperado para SharePoint/OneDrive for Business (o `sha1` costuma aparecer só no
OneDrive pessoal). A política registra o campo caso ele passe a vir, mas **não
depende dele**.

---

## 2. Os níveis de evidência

| Nível | Critério | Para que pode ser usado |
|---|---|---|
| `CANDIDATO` | mesmo `quick_xor` | agrupar para análise. **Nunca sozinho** para decidir nada |
| **`FORTE`** | mesmo `quick_xor` **e** mesmo `tamanho_bytes` | agrupar, relatar e **pular extração** (regra da seção 3) |
| `CONFIRMADO_SERVIDOR` | o acima **+** `sha1` igual | idem, com confiança maior — indisponível hoje |
| `CONFIRMADO_LOCAL` | `sha256` calculado sobre o conteúdo **baixado dos dois** | exigido antes de qualquer ação irreversível — que **não é deste projeto** |

O relatório e as views devem dizer **"conteúdo idêntico (evidência FORTE)"**,
nunca "duplicata exata confirmada".

---

## 3. A regra proposta para o E7

### Grupo

Arquivos presentes, com extrator, no mesmo tenant, com **o mesmo par
(`quick_xor`, `tamanho_bytes`)**. Pastas e arquivos de 0 byte ficam de fora.

### Representante

Escolhido de forma **determinística e reproduzível** — a mesma entrada dá sempre
o mesmo representante:

```
ORDER BY prioridade ASC,      -- o de maior prioridade documental
         criado_em_nuvem ASC, -- em empate, o mais antigo
         item_id ASC          -- desempate estável
LIMIT 1
```

### O que o worker faz

```
1. pega o representante do grupo
2. baixa para tmp/
3. calcula o quickXorHash do que baixou  (quickxor.py)
4. compara com o hash que o servidor anunciou
     ≠  -> ERROR_RETRYABLE; o grupo INTEIRO perde a otimização
           e cada cópia passa a ser extraída por si
     =  -> extrai, grava derivados, indexa, COMPLETED
5. as demais cópias: estado DUPLICATE, duplicata_de = id do representante
6. apaga o tmp/
```

O passo 4 é o que impede o pior caso — "deixar de extrair um arquivo diferente
por engano". Só confiamos no hash do servidor **depois** de reproduzi-lo sobre
os bytes que realmente chegaram.

### O caso dos 10 caminhos

Sim: **1 extrai, 9 referenciam** — e os 9 continuam existindo por inteiro.

| O que acontece com as 9 cópias | |
|---|---|
| `drive_id`, `item_id`, `caminho_rel`, `caminho_nuvem`, `web_url` | **preservados** |
| prioridade, datas, autoria, biblioteca | **preservados** |
| `estado` | `DUPLICATE` |
| `duplicata_de` | id do representante |
| `extracao` própria | não tem — os derivados são do representante |
| aparece na busca | **sim**, pela view de ocorrências |

Nada finge que os outros 9 arquivos deixaram de existir: eles estão no catálogo,
com caminho e identidade próprios, e uma busca por conteúdo devolve **os 10
lugares** onde aquele documento está.

```sql
-- proposta (não criada ainda): onde mais está o conteúdo de um resultado
CREATE VIEW v_ocorrencia AS
SELECT r.id AS representante_id, r.quick_xor,
       c.id, c.drive_id, c.item_id, c.caminho_rel, c.prioridade, c.web_url
FROM arquivo r
JOIN arquivo c ON c.id = r.id OR c.duplicata_de = r.id
WHERE r.estado IN ('COMPLETED','PARTIAL','OCR_REQUIRED');
```

### Quando o grupo muda

| Evento no delta | O que fazer |
|---|---|
| o **representante** some da nuvem | eleger o próximo pela mesma ordenação; o derivado continua válido (o conteúdo é o mesmo), só o `duplicata_de` é re-apontado |
| uma **cópia** muda de conteúdo (`ctag`/`quick_xor` diferentes) | ela **sai do grupo** e volta para a fila como `CHANGED` |
| aparece uma cópia nova | entra como `DUPLICATE` do representante existente |
| o grupo perde todos menos um | o que sobrou continua `COMPLETED`, sem `duplicata_de` |

### Como isso está implementado (2026-10-04, T-11/T-13/CR-2)

A lógica mora num lugar só, `scripts/cerebro_util.py`, e roda **dentro da
transação de quem chama** (REGRA 8) — worker, `17_aplicar_delta` e
`21_backfill_conteudo` usam as mesmas funções:

| Evento | Função | O que acontece |
|---|---|---|
| ocorrência vira `MISSING` (404 no worker, ou sumiu no delta) | `ocorrencia_saiu` | reconta; se era o representante e há cópia viva, ela assume **na hora** |
| ocorrência reaparece | `ocorrencia_voltou` | reconta; reelege se o conteúdo estava sem representante vivo |
| bytes mudaram (`CHANGED` com hash novo) | `desvincular_conteudo` | `conteudo_id` e `duplicata_de` zerados (o antigo vai no evento); reconta e reelege no grupo antigo |
| cópia passa a outro conteúdo já extraído | `worker.cobrir_por_conteudo` | reconta os dois lados e reelege no grupo que ela deixou |
| subida do worker | `reeleger_orfaos` | varre o que sobrou |

A reeleição (`reeleger_conteudo`):

- **representante órfão** = nulo, `presente=0` **ou que não pertence mais ao
  conteúdo** (bytes mudaram);
- escolhe a próxima ocorrência **viva** pela ordem da seção 3 (`ORDEM_REPRESENTANTE`);
- re-aponta o `duplicata_de` das demais cópias para ela e zera o dela —
  representante não é duplicata de ninguém (evita o ciclo de dois passos);
- **muda os trechos do índice de dono** (`texto_fts.arquivo_id` e `caminho_rel`),
  sem tocar no texto: a busca continua achando o conteúdo, agora pelo caminho
  de um arquivo que existe. Nada é reextraído, nada é apagado;
- sem nenhuma cópia viva, nada muda: o conteúdo fica como **registro
  histórico** (derivado, extração e trechos intactos), e volta a valer se a
  ocorrência reaparecer.

`conteudo.ocorrencias` conta só as **vivas** — a mesma conta de `v_ocorrencia`.

### Reextrair por versão de extrator é decisão por CONTEÚDO (T-17, R5b)

`scripts/22_reprocessar_extrator.py` (`cerebro_util.extracoes_desatualizadas`)
devolve à fila **um** item por conteúdo — o representante — e decide pela
extração **mais recente** de qualquer ocorrência daquele conteúdo
(`executado_em`; empate → a do representante). Depois de uma reeleição, o
ex-representante `MISSING` guarda a sua linha de `extracao` antiga (histórico,
REGRA 6); quando o novo representante é reextraído na versão atual, essa linha
nova é a vigente e o conteúdo **não volta mais**. Antes, a linha velha fazia o
`22` devolver o novo representante a cada passada (laço).

### Legados (antes do v4) e o backfill

Extrações anteriores ao v4 não tinham linha em `conteudo` (M-4), e as
duplicatas da era `sha256` ficavam presas a um representante morto, invisíveis à
reeleição (CR-2). `scripts/21_backfill_conteudo.py` cria o `conteudo` a partir da
`extracao` existente (sem baixar nada), liga as cópias com o **mesmo**
`(quick_xor, tamanho)` e reelege. Cópia que não tem como ser ligada
(representante sem extração, ou hash diferente) **volta para a fila** com evento
`duplicata_orfa_devolvida` — nunca fica presa num estado terminal.
Arquivo de 0 byte não ganha conteúdo (DEC-011).

### As invariantes — `cerebro_util.checar_invariantes(con)`

Lista vazia em cada uma = saudável. Não conserta nada, só mostra (REGRA 7).

| Invariante | Viola quando |
|---|---|
| `duplicata_com_representante_ausente` | cópia viva com `duplicata_de` → linha `presente=0` |
| `duplicata_sem_conteudo` | `duplicata_de` preenchido e `conteudo_id` nulo (o padrão da CR-2) |
| `extraido_sem_conteudo` | `COMPLETED/PARTIAL/OCR_REQUIRED` com extração, hash e tamanho > 0, sem conteúdo (M-4) |
| `conteudo_orfao_reelegivel` | representante órfão **e** existe ocorrência viva |
| `contador_divergente` | `conteudo.ocorrencias` ≠ ocorrências vivas |
| `conteudo_id_inexistente` | ponteiro para conteúdo que não existe (não há FK, M-16) |
| `ocorrencia_com_hash_de_outro_conteudo` | o hash/tamanho da ocorrência não é o do conteúdo dela (A-5) |
| `trecho_em_ausente_com_copia_viva` | trecho sob arquivo ausente cujo conteúdo tem cópia viva |

`informativo_dedupe(con)` dá os números que **não** são defeito: conteúdo sem
nenhuma ocorrência viva (histórico), trechos sob ausentes sem cópia viva,
trechos de ocorrência sem conteúdo (ex.: `CHANGED` aguardando reextração).

---

## 4. Quanto isso vale, hoje

| | |
|---|---|
| Grupos de conteúdo idêntico (evidência FORTE) | **15.703** |
| Arquivos envolvidos · cópias excedentes | 66.503 · **50.800** |
| Espaço ocupado pelas cópias | **617 GB** |
| **Na fila do worker:** cópias de um arquivo anterior | **27.409 de 65.859 (41,6%)** |
| **Download que deixa de ser feito** | **88 GB de 231 GB (38%)** |

Esses 88 GB são o ganho direto da política no E7 — sem contar o tempo de
extração economizado.

---

## 5. O que esta política proíbe

- Chamar de "duplicata confirmada" o que é evidência FORTE.
- Pular extração sem a conferência do hash sobre os bytes baixados (passo 4).
- Agrupar por **nome** parecido. Nome nunca entra neste critério.
- Apagar, mover ou ocultar qualquer cópia. Não é escopo deste projeto.
- Perder o rastro de qualquer `drive_id + item_id + caminho`.
