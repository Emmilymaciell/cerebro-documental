# Como medir o desempenho e projetar o tempo total

## Medicao do acervo COMPLETO (2026-09-17)

| Métrica | Valor |
|---|---|
| Arquivos inventariados | **139.459** |
| Pastas | **20.101** |
| Volume | **1,168 TB** |
| Tempo | **1 min 43 s** |
| Velocidade | **1.352 arquivos/s** (pastas frias, árvore inteira) |
| Erros | **0** |
| Só na nuvem | **97,8%** |

O inventário **não é gargalo**: a árvore inteira cabe em menos de dois minutos.
O gargalo é a extração, que depende de baixar arquivo da nuvem.

## Medição da EXTRAÇÃO (amostra de 146 documentos reais)

| Métrica | Valor |
|---|---|
| Velocidade observada | **~69 itens/min** |
| Tempo médio por item | 0,52 s |
| Erros | 0 |
| Bytes hidratados | 0,3 MB |

⚠️ **Essa amostra é otimista**: a fila começa pelos arquivos menores e pelos que
já estavam locais. Conforme o worker avançar para PDFs de dezenas de MB que
precisam vir da internet, a velocidade cai. A previsão que o `99_status.py`
mostra se recalibra sozinha a cada rodada — é ordem de grandeza, não promessa.

---

## A medição anterior (amostra de 5.000 arquivos, cache quente)

```
python scripts\01_scan_inventario.py --limite 5000
```

| Métrica | Valor |
|---|---|
| Arquivos inventariados | 5.000 |
| Pastas percorridas | 516 |
| Tempo | 0,8 s |
| Velocidade | **~6.600 arquivos/s** |
| Erros | 0 |
| Volume observado na amostra | 106 GB (97 GB só em 121 arquivos compactados) |
| Arquivos só na nuvem | **~100%** |

**Projeção:** 1.000.000 de arquivos ≈ **2,5 minutos**. Mesmo com um fator 10 de
folga (rede, pasta fria, antivírus), o inventário completo é questão de
**minutos a poucas horas** — não de dias. O gargalo do projeto **não é o
scanner**; é a extração de texto, que depende de hidratar arquivo.

---

## Como refazer a medição você mesma

### 1. Rodada de aquecimento (descarta)

```bash
python scripts\01_scan_inventario.py --limite 1000 --reiniciar
```

A primeira leitura de uma pasta é mais lenta (cache frio do Windows e do
OneDrive). Essa rodada existe só para aquecer.

### 2. Medição de verdade, em três tamanhos

```bash
python scripts\01_scan_inventario.py --limite 1000 --reiniciar
python scripts\01_scan_inventario.py --limite 5000 --reiniciar
python scripts\01_scan_inventario.py --limite 20000 --reiniciar
```

Cada rodada imprime `velocidade (arquivos/s)` e a projeção. Anote as três.

**Leitura do resultado:**

- As três velocidades parecidas ⇒ o custo é por arquivo. Projeção linear vale:
  `tempo_total = total_de_arquivos / velocidade`.
- Velocidade caindo conforme o `--limite` cresce ⇒ o custo está na escrita em
  banco. Solução: aumentar `limites.commit_a_cada` no `config/projeto.yaml`.
- Velocidade oscilando muito ⇒ antivírus ou OneDrive competindo. Medir de novo
  com o OneDrive pausado por 10 minutos.

### 3. Estimar o total de arquivos do acervo

```powershell
# roda alguns minutos; conta sem abrir nada
(Get-ChildItem -LiteralPath "C:\caminho\exemplo\acervo" -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object).Count
```

Ou, mais rápido, por biblioteca:

```bash
python scripts\01_scan_inventario.py --escopo "Programa - 4. Produtos" --reiniciar
python scripts\99_status.py
```

### 4. Fazer a conta

```
tempo_total_estimado = total_de_arquivos / velocidade_medida
```

Some **30% de folga** para pastas frias, caminhos longos e erros.

---

## Depois: medir a extração (o que realmente demora)

O scanner mede arquivos/s. A extração se mede em **MB/s e em minutos por 100
documentos**, e tem um custo que o scanner não tem: **baixar o arquivo da
nuvem**.

Quando o MVP-4 existir, a medição será:

```bash
python scripts\03_extrair_pdf.py --limite 100
```

E a projeção passa a ter três termos:

```
tempo = (bytes_a_baixar / velocidade_da_rede)
      + (paginas_totais / paginas_por_segundo)
      + (arquivos_com_ocr × segundos_por_pagina_de_ocr)
```

Por isso a fase de extração é **por escopo priorizado**, e não pelo acervo
inteiro: 1 TB não cabe em 86 GB livres.

---

## Números de referência (para comparar quando medirmos)

| Operação | Ordem de grandeza esperada |
|---|---|
| Inventário (metadados) | milhares de arquivos por segundo |
| sha256 de arquivo local | ~200–500 MB/s |
| Hidratação da nuvem | limitada pela internet — é o gargalo |
| Texto de PDF nativo (pymupdf) | ~50–200 páginas/s |
| OCR (Tesseract) | ~1–3 s **por página** |

A diferença entre a linha 1 e a última linha é de **seis ordens de grandeza**.
É exatamente por isso que o projeto começa pelo inventário e deixa o OCR por
último.
