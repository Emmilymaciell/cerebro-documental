# Checklist do ambiente

Parte já foi medida nesta máquina em **2026-09-17**. O que falta depende de
decisão sua e está marcado com ❓.

---

## A. Já medido (não precisa responder)

| # | Item | Valor medido | Como foi obtido |
|---|---|---|---|
| A1 | Raiz sincronizada | `C:\caminho\exemplo\acervo` | existe, 7 bibliotecas de primeiro nível |
| A2 | Bibliotecas de 1º nível | Programa 1. Gestão do Contrato · 2. Compartilhado interno · 3. Desenvolvimento · 4. Produtos · 5. Compartilhado Parceiro · 6. Arquivos Parceiro · Coordenação Programa - 02 Repositório | listagem da raiz |
| A3 | Python | 3.12.0 | `python --version` |
| A4 | Bibliotecas presentes | `pymupdf` (fitz), `python-docx`, `python-pptx`, `openpyxl`, `pdfplumber`, `PyYAML`, `tqdm` | `importlib.util.find_spec` |
| A5 | SQLite FTS5 | **disponível** | teste em memória |
| A6 | Espaço livre em `C:` | **~86 GB** (de 474 GB) | `Get-PSDrive C` |
| A7 | Estado de sincronização | **Arquivos Sob Demanda ligado**; na amostra de 5.000 arquivos, **~100% estão só na nuvem** | atributos `OFFLINE` + `RECALL_ON_DATA_ACCESS` |
| A8 | Velocidade do scanner | **~6.600 arquivos/s** (5.000 arq / 516 pastas em 0,8 s) | `--limite 5000` |
| A9 | MAX_PATH | caminhos longos existem; tratados com prefixo `\\?\` | `cerebro_util.lp()` |

### ⚠️ O achado que muda o projeto

O acervo **não está baixado** — está em *Arquivos Sob Demanda*. Cada arquivo é um
ponteiro; **abrir baixa**. O acervo tem ~1 TB e o disco tem ~86 GB livres:
**hidratar tudo é impossível**. Isso não atrapalha o inventário (que não abre
arquivo nenhum), mas define toda a fase de extração de texto:

> a extração roda **por escopo priorizado**, hidratando um lote, extraindo o
> texto, e — se você autorizar — devolvendo o lote para a nuvem (liberar
> espaço) antes do lote seguinte.

Na amostra inicial: **121 arquivos compactados somam 97 GB**. Sozinhos já não
cabem no disco. Vão precisar de tratamento à parte (ler o índice do ZIP sem
extrair, ou ficar de fora).

---

## B. O que eu preciso de você

> **Atualização 2026-09-17 — resolvido.** As decisões abaixo foram tomadas e já
> estão no código:
>
> - **B1 (pastas prioritárias):** não é mais necessário listar na mão. O
>   `02_classificar.py` descobre P0..P4 lendo a própria árvore
>   (`config/classificacao.yaml`) — 6.744 pastas em P0. Escopo é o acervo
>   **inteiro**; prioridade é só a ordem. Confira em `output/prioridades.md`.
> - **B2 (pastas a ignorar):** nada foi excluído. Backup/superado/lixeira vai
>   para P4 e é processado por último.
> - **B3 (hidratação):** autorizada, com orçamento e devolução. Validada em
>   arquivos reais (`tests/test_hidratacao.py`).
> - **B4 (teto de disco):** 15 GB por sessão de hidratação, piso de 25 GB
>   livres, teto de 60 GB de derivados. Editável em `config/projeto.yaml`.
> - **B5 (computador ligado):** worker autônomo, pausa segura e retomada após
>   queda. Autostart documentado, não ativado.
> - **B10 (OCR):** ainda aberto — depende de instalar o Tesseract.
>
> Continuam úteis, mas não bloqueiam: **B6** (5 perguntas reais para calibrar a
> busca), **B7** (padrão de nomenclatura por tipo de negócio), **B8** (como
> vocês marcam a revisão vigente), **B9** (outro computador).

| # | Pergunta | Por que importa | Sua resposta |
|---|---|---|---|
| B1 | ❓ **Quais pastas são prioritárias?** Liste de 3 a 10 caminhos, na ordem em que você quer ver funcionando. Pode ser no formato `Programa - 3. Desenvolvimento\G2 -  Engenharia\RD103` | Define a ordem da fila. É o ajuste que mais muda o resultado prático. | |
| B2 | ❓ **Alguma pasta que NÃO deve ser indexada?** (pessoal, jurídico sensível, backup bruto, lixo) | Evita processar o que não pode ou não vale. | |
| B3 | ❓ **Posso hidratar arquivos?** Ou seja: deixar o OneDrive baixar os arquivos de uma pasta prioritária para extrair texto — e depois liberar espaço. | Sem isso, o cérebro fica só com metadados: nome, tamanho, data, tipo. Sem texto, sem busca por conteúdo. | |
| B4 | ❓ **Qual o teto de espaço em disco** que posso ocupar (banco + derivados + cache de hidratação)? Sugiro 40 GB dos 86 GB livres. | Define o tamanho do lote e quando o processo para sozinho. | |
| B5 | ❓ **Este computador fica ligado quanto tempo?** Ele hiberna? Você quer que rode à noite, ou o tempo todo em segundo plano? | Define se agendo tarefa do Windows ou se rodamos manualmente. | |
| B6 | ❓ **Que perguntas você quer fazer ao cérebro depois?** 5 exemplos reais ("onde está o projeto executivo vigente da RD-103?", "quais relatórios citam a estaca 120?") | Perguntas reais definem o que precisa virar campo indexado. Sem isso, o índice fica genérico. | |
| B7 | ❓ **"Tipo de negócio" do documento** — existe padrão de nomenclatura ou pasta que já diz o tipo (relatório semanal, medição, projeto executivo, ART, nota fiscal)? | Se existir padrão, classifico com regra (grátis, auditável). Se não, fica para a fase de IA. | |
| B8 | ❓ **Revisão vigente** — como vocês marcam hoje? (`Rev 02`, `_final`, data no nome, pasta `SUPERADO`?) | É o que permite dizer qual versão vale. | |
| B9 | ❓ **Outro computador/usuário** vai rodar isso também? | Define se os caminhos precisam ser relativos ao usuário. | |
| B10 | ❓ **OCR**: pode instalar Tesseract nesta máquina? (offline, grátis, sem enviar nada para fora) | PDF escaneado só vira texto com OCR. Sem ele, fica listado como `ocr_pendente`. | |

---

## C. Como responder

Não precisa responder tudo de uma vez. **B1 e B3 desbloqueiam a próxima etapa** —
o resto pode vir depois. Responda em texto corrido mesmo, eu preencho o
`config/projeto.yaml`.
