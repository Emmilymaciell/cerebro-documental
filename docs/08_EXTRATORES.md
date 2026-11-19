# Extratores — o que existe hoje, medido

Auditoria de `scripts/extratores.py` feita em 2026-09-27 (antes do E9) e
**fechamento do E9 em 2026-10-04**: PDF, DOCX, XLSX/XLSM e PPTX
finalizados, L2, L4 e L7 resolvidas, fronteira de página no derivado.
Tudo aqui foi lido no código e conferido rodando; nada é suposição.

Números do acervo: `data/cerebro.db` (leitura `mode=ro`), foto de
2026-09-27 — fila da nuvem (`v_fila_nuvem`) com **62.742 itens, 243,21 GB**.
Cobertura por teste: `python tests\test_extratores.py` → **419 PASS**;
`python tests\test_worker_formatos.py` → **83 PASS** (novo).

**2026-10-04, depois do E9:** `pdf/1.2` → **`pdf/1.3`** — a decisão de OCR
passa a sair de uma função pura (`decidir_ocr_paginas`) sobre métricas por
página, e as métricas vão para o perfil (`ocr_metricas`, §1.1 e §2). O
texto extraído não muda; muda o que o perfil guarda.

---

## 1. Contrato

Ponto de entrada único: `extrair(caminho, categoria, cfg) -> dict`.
O despacho é pela **categoria** do `config/projeto.yaml`; a extensão só
decide se aquela categoria tem leitor para o arquivo (`motivo_sem_leitor`).
Nenhum extrator levanta exceção para fora — erro vira `status` (REGRA 6).

O worker (`processar` → `_registrar`) lê **estas 14 chaves** e grava todas
na tabela `extracao` e no perfil JSON. Faltar uma quebra a transação do item:

| chave | tipo | quem preenche |
|---|---|---|
| `status` | str | todos |
| `extrator` | str | todos |
| `versao` | str | todos |
| `texto` | str | todos — **pdf/pptx/xlsx com `\f` entre unidades, ver §1.2** |
| `paginas_texto` | `[(numero, texto), ...]` | todos |
| `paginas` | int \| None | **só PDF** |
| `paginas_com_texto` | int \| None | **só PDF** |
| `slides` | int \| None | **só PPTX** |
| `abas` | int \| None | **só XLSX** |
| `imagens` | int \| None | **PDF, DOCX, PPTX, XLSX** |
| `caracteres` | int | todos (`== len(texto)`, separadores inclusos) |
| `palavras` | int | todos |
| `densidade_texto` | float \| None | **só PDF** (métrica; não decide mais OCR) |
| `erro` | str (nunca None) | todos — em `parcial` e `ocr_requerido` diz **por quê** |

### 1.1 Chaves extras (E9)

`extratores.CHAVES_EXTRAS`. Todo retorno as traz (None quando não se
aplica); nenhuma vai para coluna do banco — o worker as copia para o
**perfil JSON** quando preenchidas. Quem não as conhece não quebra.

| chave | tipo | quem preenche |
|---|---|---|
| `metadados` | dict só com o que existe: `titulo, autor, assunto, palavras_chave, ultimo_autor, criado_em, modificado_em, revisao, categoria, aplicativo, produtor, macros` | PDF, DOCX, PPTX, XLSX |
| `tabelas` | int | DOCX (inclui aninhadas), PPTX |
| `paginas_ocr` | `[int]` — quais páginas pedem OCR | PDF |
| `ocr_tipo` | `"misto"` \| `"escaneado"` \| None | PDF |
| `ocr_metricas` | `{"formato": "c,cob_mil,d,img;v1", "paginas": "<string compacta>"}` — métricas por página para recalibrar o OCR sem baixar de novo (§2, PDF) | PDF (`pdf/1.3`) |

Datas viram ISO 8601. python-docx/python-pptx **inventam** propriedades
(autor "python-docx", modificado = agora) quando o pacote não tem
`docProps/core.xml`: só lemos se a relação existir de verdade.

### 1.2 Fronteira de unidade no `texto`: form feed (`\f`, U+000C)

Até o E9, `texto` juntava páginas com `"\n\n"`, e o derivado `.txt.gz`
perdia onde cada página começa: reindexar a partir dele dava **um segmento
só** (e, em planilha com aba vazia no meio, o número da aba saía errado).

Agora, em **pdf, pptx e xlsx**, cada unidade estrutural (página, slide,
aba) é **terminada** por `\f`, na posição do seu número — a convenção do
`pdftotext`. Unidade sem texto é string vazia entre dois `\f`; as vazias do
fim são cortadas; documento sem texto nenhum → `texto = ""`.

```
página 1 \f  \f  página 3 \f        ← PDF de 3 páginas, a 2 em branco
```

Por que é seguro:

- O `\f` **nunca** aparece dentro de uma unidade: `_limpar_unidade` o troca
  por `\n` (PDF pode trazê-lo; XML de DOCX/XLSX/PPTX nem admite o caractere).
- `gzip` em modo texto não traduz `\f` (testado: ida e volta idêntica).
- Indexação não usa o `texto`: v4 e v5 usam `paginas_texto`. O `texto` só
  vai para o derivado, para `caracteres`/`palavras` e para o `control.py`.
- `busca.paginas_do_derivado()` separa por `\f` **só** em formato paginado
  (`TIPO_POR_EXTRATOR`): um `.txt`/`.log` com form feed de impressora
  continua um documento, como o extrator de texto o indexa.
- Derivado antigo, sem `\f`, segue a regra de antes (PDF/PPTX → um segmento
  `documento`; XLSX → pelo rótulo `[aba]`).

Teste de ida e volta (`tests/test_busca.py` §15c): extrator → gzip →
`paginas_do_derivado` devolve **exatamente** o `paginas_texto` do extrator,
mesmos segmentos (sha), e reindexar do derivado dá `INALTERADO`.
DOCX, CSV e texto não são paginados e não ganham `\f`.

**Efeito colateral conhecido (fora deste módulo):** `busca.fonte_do_conteudo`
confere o índice v4 contra o derivado juntando páginas por `"\n\n"`. Para
derivado novo essa conferência acusa "diverge do derivado" e cai no
caminho do derivado — que agora reconstrói as mesmas páginas. O resultado
é o mesmo; só a mensagem de detalhe engana. Só afeta conteúdo extraído com
v4 ativa e versão nova de extrator antes da migração v5.

### 1.3 `status` → estado do catálogo

| status | estado | observação |
|---|---|---|
| `ok` | COMPLETED | |
| `vazio` | COMPLETED | documento sem texto e **sem imagem** não é erro |
| `parcial` | PARTIAL | `erro` começa com `LIMITE:` ou lista o que falhou |
| `ocr_requerido` | OCR_REQUIRED | `erro` começa com `OCR`; o texto que existe **é indexado** |
| `protegido` | ERROR_FINAL | senha: não adianta tentar de novo |
| `nao_suportado` | UNSUPPORTED | sai da fila de primeira |
| `erro` | ERROR_RETRYABLE → ERROR_FINAL | 4 tentativas (`worker.max_tentativas`) |

Três `status` vêm do próprio worker, sem abrir o arquivo: 0 bytes (`vazio`,
`versao="-"`), acima de `tamanho_max_extracao_mb` (`parcial`,
`versao="adiado/teto_tamanho"`) e **formato sem leitor** (`nao_suportado`,
versão do extrator da categoria, `erro` começando com `SEM_LEITOR:`) — §4 L7.

---

## 2. Matriz por formato

Versões conferidas nesta máquina em 2026-10-04. Python **3.12.0**, 64 bits.
Limites: todos lidos de `worker:` no `projeto.yaml` **com padrão no código**
(yaml antigo continua valendo; nenhuma chave nova foi escrita no yaml).

### PDF — `extrair_pdf`, `pdf/1.1` → `pdf/1.2` → **`pdf/1.3`**

| | |
|---|---|
| Biblioteca | **PyMuPDF 1.26.7** |
| Texto | `page.get_text("text")` por página |
| Estrutura | uma unidade por **página**, numerada a partir de 1; página sem texto ocupa a posição no `texto` (`\f`) e não vira trecho |
| Tabelas | **só o texto das células** (ordem de leitura do MuPDF). Sem estrutura, `tabelas=None` — `find_tables()` custa caro por página |
| Imagens | soma de `get_images()` por página (logotipo repetido conta em cada página) |
| Metadados | título, autor, assunto, palavras-chave, criador, produtor, criado/modificado (data PDF → ISO) |
| OCR | **por página** — ver abaixo |
| `parcial` | mais de `paginas_max_pdf` (3000) páginas → lê as primeiras, `erro = "LIMITE: lidas N de M páginas"`; página que não carrega → listada no `erro` |
| `protegido` | senha de abertura (`needs_pass`). Senha só de permissão abre normal |
| `erro` | MuPDF não abre (corrompido, não é PDF) |
| **Não** extrai | estrutura de tabela, marcadores, anotações e comentários, campos de formulário, anexos embutidos, ordem de leitura em coluna dupla |

**Regra de OCR, página a página (L2 — resolvida).** Para cada página lida,
`c` = caracteres do texto da página (sem espaços nas pontas):

```
página pede OCR  ⇔  c < min_caracteres_por_pagina (80)  E  (
                       imagens cobrem ≥ ocr_cobertura_imagem (50%) da página
                    OU c == 0 e a página tem conteúdo desenhado )
página em branco ⇔  c == 0, nenhuma imagem, fluxo de conteúdo vazio  → não conta
documento        ⇒  ≥ 1 página que pede OCR → ocr_requerido
                    ocr_tipo = "escaneado" se todas as não-brancas pedem, senão "misto"
```

`paginas_ocr` guarda **quais** páginas — o OCR futuro é por página, não
por documento. O texto das páginas digitais é extraído e indexado do mesmo
jeito (o worker grava derivado e índice para `ocr_requerido`).

| caso (teste sintético) | antes (média) | agora (por página) |
|---|---|---|
| 1 página densa + 9 escaneadas | `ok` → COMPLETED, 9 páginas somem | `ocr_requerido`, `misto`, `paginas_ocr = 2-10` |
| 10 pranchas vetoriais com carimbo de 20 caracteres | `ocr_requerido` (falso) | `ok` |
| capa curta com logotipo pequeno + páginas de texto | `ok` | `ok` |
| página escaneada com carimbo digital curto | `ocr_requerido` | `ocr_requerido` |
| página só com desenho, sem texto (texto em curva de CAD) | depende da média | `ocr_requerido` naquela página |
| página em branco no meio | conta na média | ignorada |

Limitações conhecidas, deliberadas: página escaneada com **mais de 80
caracteres** de carimbo digital (ex.: assinatura eletrônica longa) conta
como texto; página de **relatório fotográfico** (foto grande + legenda
curta) pede OCR que não vai render nada. As duas são decisões de faixa,
ajustáveis por `min_caracteres_por_pagina` e `ocr_cobertura_imagem`, e a
**L1** (amostra real) é que deve calibrá-las. `OCR_REQUIRED` é estado de
espera, não perda: nada do que foi lido sai do índice.

**Métricas por página no perfil (`pdf/1.3`, R6).** Em `pdf/1.2` a regra
decidia pela cobertura de imagem, mas a cobertura não era gravada:
recalibrar depois exigiria baixar de novo todos os PDFs. Agora a regra é a
função pura `decidir_ocr_paginas(metricas, cfg)` — **a mesma** que o
extrator usa — e as métricas vão para o perfil em `ocr_metricas`:

```
"ocr_metricas": {"formato": "c,cob_mil,d,img;v1",
                 "paginas": "2954,0,1,0;0,1000,1,1;22,1000,1,1;19,0,1,0;0,0,0,0;-"}
```

| campo | o que é |
|---|---|
| `c` | caracteres do texto da página (o mesmo `c` da regra) |
| `cob_mil` | cobertura de imagem em **milésimos** (0..1000). Vazio = não medida: página com ≥ `ocr_metrica_max_caracteres` (padrão **1000**) caracteres — medir custa ~70% do tempo de extrair o texto da página (medido: 967 ms contra 1.326 ms em 300 páginas com logotipo) |
| `d` | 1 se o fluxo de conteúdo não é vazio (há vetor, imagem ou texto desenhado); 0 = vazio. Página com texto: 1 |
| `img` | imagens da página (`get_images`) |
| `-` | página que não carregou |

Uma página por item, separadas por `;`, na ordem (item *i* = página *i+1*).
Recalibrar: `extratores.decidir_ocr_paginas(metricas_ocr_de_texto(perfil["ocr_metricas"]["paginas"]), cfg_novo)`
— dá exatamente o que a reextração com `cfg_novo` daria (testado com três
calibrações). Vale para `min_caracteres_por_pagina` até o teto da métrica
(1000); acima disso a página de texto longo com imagem volta como
`indeterminadas` (declarada, nunca decidida no chute). A cobertura é
arredondada para milésimos **antes** de decidir, também no extrator: o
arredondamento é parte da regra, por isso recalcular dá o mesmo resultado.

**Por que uma string e não lista de listas:** o perfil é gravado com
`indent=1` (`cerebro_util.gravar_perfil`), que põe cada número de uma lista
numa linha. Medido em 300 páginas: **10,2 bytes/página** como string contra
**34** como lista de listas. Por arquivo, +~70 bytes fixos (`formato`).
Estimativa para o acervo: a fila tem **36.378 PDFs / 150,6 GB**; a amostra
já extraída dá 6,5 páginas/MB → ~1 milhão de páginas → **~10 MB** de
métricas no total (~11 bytes/página; um PDF de 3.000 páginas, o teto, ~33
KB). Em produção ainda **não existe nenhuma extração `pdf/1.2`** (banco de
2026-10-04: só `pdf/1.0` e `'-'`), então nenhum PDF precisa ser reprocessado
só para ganhar as métricas.

### DOCX / DOCM / DOTX — `extrair_docx`, `docx/1.0` → **`docx/1.1`**

| | |
|---|---|
| Biblioteca | **python-docx 1.2.0** (abertura) + percurso próprio do XML (`w:`) |
| Texto | parágrafos (runs, hyperlink, inserção, controle de conteúdo `w:sdt`), tab e quebra de linha |
| Estrutura | **cabeçalhos** (`[cabecalho] …`, primeiro), **corpo na ordem do documento**, **rodapés** (`[rodape] …`, por último). Cabeçalho repetido (1ª página = padrão) sai uma vez |
| Caixas de texto | `w:txbxContent` entra como linhas próprias logo após o parágrafo-âncora; o `mc:Fallback` (cópia VML da mesma caixa) é pulado — sem isso a caixa sairia duas vezes |
| Tabelas | **no lugar certo da ordem**, uma linha por linha da tabela, células por ` \| `; tabela aninhada entra dentro da célula; `tabelas` conta todas |
| Imagens | partes de imagem em `/word/` (a miniatura do pacote não conta) |
| Metadados | título, autor, assunto, palavras-chave, último autor, criado, modificado, revisão, categoria |
| Páginas | não há: página de DOCX depende de layout. Documento inteiro = **unidade 0**, sem `\f` |
| OCR | sem texto **e** com imagem → `ocr_requerido` (antes: `vazio` → COMPLETED) |
| `parcial` | texto acima de `max_caracteres_documento` (20.000.000) → cortado, `LIMITE:`; cabeçalho/rodapé que falha ao ler → listado |
| `.docm` `.dotx` `.dotm` | python-docx recusa **só pelo content type**; o extrator monta uma cópia **em memória** com o content type de `.docx` (nada no disco, original intocado). Macro nunca é executada — só se lê XML |
| `protegido` | arquivo OLE com fluxo `EncryptedPackage` (senha de abertura do Office) |
| `nao_suportado` | `.doc`, `.rtf`, `.odt`; OLE **sem** cifra com extensão `.docx` (legado renomeado) |
| **Não** extrai | notas de rodapé e de fim, comentários, texto excluído no controle de alterações (`w:delText`), código de campo, SmartArt, texto em gráfico |

### XLSX / XLSM — `extrair_xlsx`, `xlsx/1.0` → **`xlsx/1.1`**

| | |
|---|---|
| Biblioteca | **openpyxl 3.1.5**, `read_only=True, data_only=True, keep_vba=False` |
| Texto | valores de célula; linha = células não vazias por ` \| ` |
| Estrutura | uma unidade por **aba**, numerada pela posição da aba, começando por `[aba] Nome`; aba sem célula ocupa a posição (`\f`) e não vira trecho |
| Abas | `abas` conta todas, inclusive ocultas e de gráfico |
| Aba de gráfico | `chartsheet` não tem célula: vira unidade vazia. **Antes derrubava a planilha inteira** (`AttributeError` → `erro` ×4) — bug B6 |
| Fórmulas | **valor em cache** que quem salvou gravou (o Excel sempre grava). **Sem cache** (planilha gerada por programa e nunca aberta no Excel): a célula chega vazia e some **em silêncio** — não há aviso, o arquivo sai `ok`. Detectar exigiria uma segunda leitura de cada aba (`data_only=False`); não vale o custo enquanto não houver medição de quantas existem |
| Imagens | membros `xl/media/` do pacote |
| Metadados | título, autor, assunto, palavras-chave, último autor, criado, modificado; `macros: true` se há `vbaProject.bin` |
| XLSM | lido como XLSX. `keep_vba=False` + `read_only`: o projeto VBA não é carregado, nunca executado, e o código da macro não vira texto (testado com `Auto_Open`) |
| `parcial` | `xlsx_max_linhas_aba` (5000) por aba ou `xlsx_max_caracteres` (4.000.000) no arquivo, `LIMITE:` com a aba |
| `protegido` / `nao_suportado` | igual ao DOCX (OLE cifrado / OLE renomeado); `.xls`, `.xlsb`, `.ods` → `nao_suportado` |
| **Não** extrai | gráficos, comentários, tabelas dinâmicas, formatação condicional, nomes definidos, validação de dados |

### PPTX / PPTM / POTX — `extrair_pptx`, `pptx/1.1` → **`pptx/1.2`**

| | |
|---|---|
| Biblioteca | **python-pptx 1.0.2** |
| Texto | toda forma com `text_frame`, células de tabela, forma agrupada (recursivo, 8 níveis) |
| Estrutura | uma unidade por **slide**, numerada a partir de 1, com as **notas do apresentador na mesma unidade** (`[notas] …`); slide sem texto ocupa a posição (`\f`) |
| Tabelas | texto por linha (` \| `), `tabelas` conta |
| Imagens | figuras, inclusive em placeholder de imagem |
| Metadados | igual ao DOCX |
| OCR | sem texto **e** com imagem → `ocr_requerido` (álbum de fotos, slide escaneado) |
| `parcial` | texto acima de `max_caracteres_documento`; forma ou nota que falha ao ler (antes era engolida em silêncio) |
| `.pptm` | python-pptx abre direto. `.potx` `.potm` `.ppsx` `.ppsm`: cópia em memória com o content type de `.pptx` |
| `protegido` / `nao_suportado` | igual ao DOCX; `.ppt`, `.odp` → `nao_suportado` |
| **Não** extrai | SmartArt, texto em gráfico, slide mestre e layout, comentários |

### CSV / TSV — `extrair_csv`, `csv/1.1` (sem mudança)

| | |
|---|---|
| Biblioteca | `csv` e `io` da biblioteca padrão |
| Extrai | linhas, campos por ` \| `, separador detectado entre `; , tab \|` |
| Limites | 20 MB e 20.000 linhas → `parcial` |
| Indexação | arquivo inteiro como **unidade 0** |

### Texto puro — `extrair_texto`, `texto/1.1` (sem mudança)

Categoria `texto`: `.txt .md .json .xml .log .html .htm`. Decodificação em
cadeia com BOM primeiro; **nenhuma limpeza de marcação** (L5, aberta);
20 MB → `parcial`; unidade 0.

### Bibliotecas

Presentes e usadas: `pymupdf 1.26.7`, `python-docx 1.2.0`,
`python-pptx 1.0.2`, `openpyxl 3.1.5`. `zipfile` (biblioteca padrão) para
content type, imagens e macro. Nenhuma dependência nova.

---

## 3. Bugs encontrados e corrigidos

Todos reproduzidos em teste antes da correção.

| | bug | efeito | correção |
|---|---|---|---|
| B1–B5 | auditoria de 2026-09-27 (densidade truncada, ODF/RTF como `erro`, PPTX sem tabela/grupo, UTF-16, campo CSV gigante) | ver histórico do git | `pdf/1.1`, `pptx/1.1`, `csv/1.1`, `texto/1.1` |
| B6 | XLSX: aba de gráfico (`chartsheet`) não tem `iter_rows` | a planilha **inteira** virava `erro` ×4 → ERROR_FINAL | aba de gráfico vira unidade vazia |
| B7 | `.docm`/`.dotx`/`.potx`: a biblioteca recusa pelo content type | `erro` ×4 → ERROR_FINAL (1 `.docm` na fila real) | cópia em memória com o content type equivalente |
| B8 | OOXML com senha de abertura ou legado renomeado (é OLE, não ZIP) | `erro` ×4 ("File is not a zip file") | OLE + `EncryptedPackage` → `protegido`; OLE sem cifra → `nao_suportado` |
| B9 | derivado de XLSX com aba vazia no meio | reindexar do derivado numerava a aba errada (2 em vez de 3) | `\f` na posição da aba (§1.2) |
| B10 | PPTX: forma que falhava era descartada sem registro | perda silenciosa | contada; documento sai `parcial` |

Versões subidas no E9: **`pdf/1.2`, `docx/1.1`, `xlsx/1.1`, `pptx/1.2`**;
depois, `pdf/1.3` (métricas de OCR, §2). `csv/1.1` e `texto/1.1` não
mudaram no E9. O dicionário `VERSOES` manteve a estrutura.

Conferido no banco de **2026-10-04** (cópia pela API de backup, `mode=ro`;
o número anterior, "77", estava errado — somava 78 linhas e esquecia o
`texto/1.0`): **90 linhas de `extracao`** com versão anterior —

| versão | linhas | estado |
|---|---|---|
| `pdf/1.0` | 51 | 48 COMPLETED, 1 OCR_REQUIRED, 2 MISSING |
| `docx/1.0` | 5 | COMPLETED |
| `pptx/1.0` | 2 | 1 COMPLETED, 1 CHANGED |
| `xlsx/1.0` | 20 | 18 COMPLETED, 2 PARTIAL |
| `texto/1.0` | 12 | 10 COMPLETED, 2 MISSING |

O `22_reprocessar_extrator.py` (simulação na cópia) devolve **85 conteúdos**
à fila — 49 pdf, 5 docx, 1 pptx, 20 xlsx, 10 texto; ficam de fora 4 sem
ocorrência viva (2 pdf + 2 texto, só histórico) e 1 que já está na fila
(o pptx CHANGED). Nenhuma marca `adiado/teto_tamanho` nem UNSUPPORTED
`SEM_LEITOR` existe ainda no banco; quando existirem, **não** entram no
padrão (R5a, ver L6).

---

## 4. Lacunas

### L1 — Não sabemos quantos dos PDFs são escaneados — **aberta**

A regra de OCR agora é por página (L2) e registra `paginas_ocr`; falta a
**amostra real** (300–500 PDFs por `16_amostra_e7.py`) para medir a fração
de páginas sem camada de texto e calibrar `min_caracteres_por_pagina` e
`ocr_cobertura_imagem`. Sem isso, dimensionar OCR é chute.

### L2 — Critério de OCR por média — **resolvida em `pdf/1.2`**

Regra por página em §2 (PDF). Muda o estado de documentos já extraídos
quando forem reprocessados (T-17): um PDF misto que estava COMPLETED passa
a OCR_REQUIRED — é a correção pretendida (REGRA 6), não regressão.

### L3 — Compactados escondem entregas inteiras — **aberta**

1.391 arquivos, 89,31 GB. Proposta inalterada: ler só o índice do `.zip`
com `zipfile`; `.rar` precisaria de dependência nova.

### L4 — DOCX perde cabeçalho, rodapé e caixa de texto — **resolvida em `docx/1.1`**

Conferido em teste: `CONTRATO 123/2025 - DYNATEST` no cabeçalho aparece
uma vez, marcado `[cabecalho]`; caixa de texto aparece uma vez.

### L5 — HTML e XML entram como sopa de tags — **aberta**

236 arquivos, 1,91 GB. `bs4`/`lxml` instalados; volume pequeno.

### L6 — Reprocessamento por versão de extrator — **resolvida (T-17, `22_reprocessar_extrator.py`)**

Este módulo só expõe `VERSOES`; a comparação com `extracao.versao` é de
`cerebro_util.extracoes_desatualizadas`. O que conta como "versão antiga":

- só **versão de extrator** (`<extrator>/<n.n>`, `eh_versao_de_extrator`)
  diferente da atual. **Marca não é versão**: `'-'` (0 byte) e
  `adiado/teto_tamanho` (PARTIAL acima do teto, sem download) só voltam com
  `--sem-versao` ou filtro explícito (`--erro adiada`,
  `--versao adiado/teto_tamanho`, `--estado PARTIAL`);
- `nao_suportado` de extensão **que continua sem leitor** (o UNSUPPORTED
  `SEM_LEITOR` do worker, L7) também não volta no padrão — reextrair daria o
  mesmo resultado. Volta sozinho no dia em que `motivo_sem_leitor` aceitar a
  extensão; antes disso, só com `--erro SEM_LEITOR`;
- a decisão é por **conteúdo**: vale a extração mais recente de qualquer
  ocorrência (ver `docs/07_POLITICA_DUPLICATAS.md`).

### L7 — Formato legado baixado para nada — **resolvida**

`extratores.motivo_sem_leitor(ext, categoria)` / `suporta(ext, categoria)`:
função pura, a **mesma regra** que o extrator aplica depois do download.
`worker.processar()` a consulta **antes** do download (passo 1a, antes do
teto de tamanho): item sem leitor termina `UNSUPPORTED` pelo mesmo caminho
de gravação (`_registrar`: extração, perfil, conteúdo, estado, evento, uma
transação), **sem estado novo**, sem gastar tentativa:

- `extracao.status = 'nao_suportado'`, `versao` = a do extrator da categoria;
- `erro`/`ultimo_erro` = `SEM_LEITOR: formato legado .xls; nada foi baixado`;
- evento `processado` com `baixado: false, adiado: "sem_leitor"`;
- contador de sessão `adiados_sem_leitor`.

Extensões sem leitor hoje: `.doc .xls .ppt .xlsb .rtf .odt .ods .odp`
(e `.pst .dwg .dgn`, se algum dia caírem numa dessas categorias), mais
qualquer item de categoria sem extrator que chegue à fila com `suportado=1`.
`csv` e `texto` leem qualquer extensão. `.docm .dotx .xlsm .pptm .potx`
**têm** leitor.

Medido na fila real (`v_fila_nuvem`, banco de 2026-09-27, `mode=ro`):

| extensão | itens | GB |
|---|---|---|
| `.xls` | 526 | 1,041 |
| `.xlsb` | 100 | 1,061 |
| `.doc` | 41 | 0,045 |
| `.rtf` | 1 | 0,009 |
| **poupados** | **668** | **2,158** |

de 62.742 itens / 243,21 GB na fila. Nenhum item ainda tinha sido baixado
e marcado UNSUPPORTED por esse caminho. Se algum dia importarem, `.xls`
tem `xlrd`/`olefile` e `.rtf` tem `striprtf` — dependências novas.

---

## 5. Formatos sem extrator — o que fazer com cada grupo

70.364 arquivos, 852,92 GB em UNSUPPORTED (foto de 2026-09-22). A maior
parte **deve continuar assim** — e isso é resposta, não omissão (REGRA 6).

| grupo | arquivos | volume | proposta |
|---|---|---|---|
| **Levantamento e geoespacial** (`.laz .las .jp2 .ecw .dav .xyz .ubx`, RINEX, shapefile, `.hcn`, `.mms`) | 20.677 | 298,9 GB | **UNSUPPORTED definitivo.** Dado bruto de medição; o que interessa está no `caminho_rel` |
| **Imagem — foto de campo** | 41.537 | 17,8 GB | **UNSUPPORTED.** Se um dia precisar, EXIF com Pillow, não OCR |
| **Imagem — raster de projeto** (`.tif`, `.png` grandes) | 2.033 | 42,4 GB | **UNSUPPORTED.** MDT, não página escaneada |
| **Compactado** `.zip` | 1.097 | 71,7 GB | extrator só de índice — L3 |
| **Compactado** `.rar` | 294 | 17,7 GB | idem, com dependência nova |
| **CAD** `.dwg .dxf .dgn` | 1.649 | 63,0 GB | **UNSUPPORTED.** O nome já é a identificação |
| **CAD geo** `.shp .kml .kmz` | 1.068 | 0,68 GB | só metadados, se algum dia |
| **Vídeo** `.mp4` | 59 | 338,1 GB | **UNSUPPORTED definitivo** (REGRA 5) |
| **Power BI** `.pbix` | 4 | 0,006 GB | **UNSUPPORTED** |
| **E-mail** `.msg .eml .pst` | 0 | — | nada no acervo hoje |
| **Resto de `outro`** | ~1.947 | 2,7 GB | **UNSUPPORTED** |
| **Legado Office** `.xls .xlsb .doc .rtf` | 668 na fila | 2,16 GB | **UNSUPPORTED sem download** (L7) |

Nenhuma dessas propostas envolve OCR em massa, chamada de IA, ou tirar
arquivo do catálogo.

---

## 6. Cobertura de teste

| arquivo | o que cobre |
|---|---|
| `tests/test_extratores.py` (**419 PASS**) | contrato de 14 chaves em todo retorno; tudo da auditoria; **pdf/1.3:** métricas por página num PDF misto de 9 páginas (escaneada, carimbo, prancha, logotipo, só desenho, branca, texto acima e abaixo do teto da métrica), decisão recalculada só das métricas = a do extrator, três recalibrações = reextrair, `indeterminadas` acima do teto, codec de ida e volta, tamanho no perfil (≤ 12 bytes/página; **sem a correção: 3 FAIL**); **E9:** `suporta()` concorda com o extrator em toda extensão do yaml; OCR por página (misto, escaneado, carimbo curto, prancha vetorial, logotipo, desenho sem texto, página branca, só brancas); `\f` e numeração; tabela em PDF; metadados e datas PDF; DOCX com cabeçalho, rodapé, caixa de texto (com Fallback), ordem, tabela aninhada, metadados, só imagem, teto de caracteres; `.docm`/`.dotx`/`.potx` em memória sem tocar o original; OOXML cifrado (marca atravessando o bloco de 1 MB) e OLE renomeado nos três formatos; XLSX com aba de gráfico, aba vazia, imagem, metadados, tetos de linhas e de caracteres, fórmula com e sem cache; XLSM com macro (não vira texto, original intocado); PPTX com notas na unidade, slide vazio, tabela, imagem, metadados, álbum de fotos, teto |
| `tests/test_worker_formatos.py` (**83 PASS**, novo) | worker de produção + Graph falso: 8 extensões legadas → UNSUPPORTED **sem pedido de download**, sem tentativa, extração/perfil/conteúdo/evento completos; categoria sem extrator; legado acima do teto vira UNSUPPORTED (não PARTIAL); `.xlsx`/`.xlsm`/`.docm` baixados e COMPLETED; metadados no perfil JSON; derivado de PDF com `\f` e `texto_fts` por página. **Sem a correção: 51 PASS / 32 FAIL** (o `.docm` termina ERROR_RETRYABLE) |
| `tests/test_busca.py` §15c–15e | ida e volta do `\f` para pdf/pptx/xlsx, `INALTERADO` ao reindexar do derivado, compatibilidade com derivado antigo e `.txt` com form feed. **Sem a correção: 15 FAIL** |
| `tests/test_worker.py`, `tests/test_worker_nuvem.py`, `tests/test_worker_v5.py` | os extratores de ponta a ponta dentro do worker |
