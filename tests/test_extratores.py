"""
test_extratores — o contrato de `scripts/extratores.py`, formato a formato,
em arquivos sinteticos gerados na hora. Nao toca no SharePoint, nao usa rede,
nao abre o banco real.

    python tests\\test_extratores.py

Cobre, para PDF, DOCX, XLSX, PPTX, CSV e texto:
  - o dicionario devolvido tem SEMPRE as mesmas chaves (contrato do worker);
  - `status` coerente com o conteudo (ok / vazio / parcial / ocr_requerido /
    protegido / nao_suportado / erro);
  - contagem certa de paginas, abas e slides;
  - arquivo corrompido, arquivo inexistente e diretorio viram `erro` — nunca
    excecao para fora (REGRA 6: nada derruba o worker);
  - formato legado e formato sem biblioteca viram `nao_suportado`, nao `erro`
    (senao entram no ciclo de 4 tentativas do worker por nada);
  - caminho acima de 260 caracteres (REGRA 3);
  - decisao de OCR do PDF, inclusive quando o documento e truncado.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import area_temporaria, checar, foto_exata, placar_final  # noqa: E402

import extratores as ex  # noqa: E402

PALAVRA = "terraplenagem"

CFG = {"worker": {"paginas_max_pdf": 3000, "min_caracteres_por_pagina": 80}}

# Chaves que TODO retorno precisa ter — o worker le todas elas em `processar`
# e as grava na tabela `extracao`. Faltar uma e quebrar a transacao do item.
CHAVES = {"status", "extrator", "versao", "texto", "paginas_texto", "paginas",
          "paginas_com_texto", "slides", "abas", "imagens", "caracteres",
          "palavras", "densidade_texto", "erro"}

STATUS_VALIDOS = {"ok", "parcial", "ocr_requerido", "vazio", "protegido",
                  "nao_suportado", "erro"}


def contrato(rotulo: str, r: dict) -> bool:
    """Todo retorno, de qualquer extrator, passa por aqui."""
    faltando = CHAVES - set(r)
    ok = checar(not faltando, f"{rotulo}: dicionario completo", f"faltou {faltando}")
    ok &= checar(r.get("status") in STATUS_VALIDOS,
                 f"{rotulo}: status valido", str(r.get("status")))
    ok &= checar(isinstance(r.get("texto"), str) and isinstance(r.get("paginas_texto"), list),
                 f"{rotulo}: texto e paginas_texto com o tipo certo")
    ok &= checar(isinstance(r.get("erro"), str), f"{rotulo}: erro e string (nunca None)")
    ok &= checar(r.get("caracteres") == len(r.get("texto") or ""),
                 f"{rotulo}: caracteres bate com o texto")
    return ok


# ----------------------------------------------------------------------
# construtores de arquivo de teste
# ----------------------------------------------------------------------

def pdf_com_texto(destino: Path, paginas: int, linhas_por_pagina: int = 6):
    import fitz
    doc = fitz.open()
    for numero in range(paginas):
        pagina = doc.new_page()
        for linha in range(linhas_por_pagina):
            pagina.insert_text((50, 60 + linha * 16),
                               f"Pagina {numero + 1}: servico de {PALAVRA} na estaca 120 "
                               f"da RD-103, medicao mensal.", fontsize=9)
    doc.save(ex._lp(destino))
    doc.close()


def pdf_sem_texto(destino: Path, paginas: int = 3):
    import fitz
    doc = fitz.open()
    for _ in range(paginas):
        pagina = doc.new_page()
        pagina.draw_rect(fitz.Rect(50, 50, 500, 700), color=(0, 0, 0), width=2)
    doc.save(ex._lp(destino))
    doc.close()


def main() -> int:
    area = area_temporaria("cerebro_extratores_")
    print(f"area de teste: {area}")

    import fitz
    import docx
    import openpyxl
    from pptx import Presentation
    from pptx.util import Inches

    # ==================================================================
    print("\n--- PDF: documento com camada de texto ---")
    # ==================================================================
    p = area / "notificacao.pdf"
    pdf_com_texto(p, 4)
    r = ex.extrair_pdf(p, CFG)
    contrato("pdf ok", r)
    checar(r["status"] == "ok", "PDF com texto -> ok", str(r["status"]))
    checar(r["paginas"] == 4, f"contou as 4 paginas (paginas={r['paginas']})")
    checar(r["paginas_com_texto"] == 4, f"4 paginas com texto ({r['paginas_com_texto']})")
    checar(len(r["paginas_texto"]) == 4, "paginas_texto traz uma entrada por pagina")
    checar([n for n, _ in r["paginas_texto"]] == [1, 2, 3, 4],
           "numeracao das paginas comeca em 1", str([n for n, _ in r["paginas_texto"]]))
    checar(PALAVRA in r["texto"], "o conteudo foi realmente extraido")
    checar(r["extrator"] == "pdf" and r["versao"].startswith("pdf/"),
           "identifica extrator e versao", f"{r['extrator']} {r['versao']}")
    checar(r["slides"] is None and r["abas"] is None,
           "campos de outro formato ficam None no PDF")
    checar(r["densidade_texto"] and r["densidade_texto"] > 80,
           f"densidade_texto preenchida ({r['densidade_texto']})")

    print("\n--- PDF: escaneado (sem camada textual) ---")
    p = area / "escaneado.pdf"
    pdf_sem_texto(p, 3)
    r = ex.extrair_pdf(p, CFG)
    contrato("pdf escaneado", r)
    checar(r["status"] == "ocr_requerido", "PDF sem texto -> ocr_requerido", str(r["status"]))
    checar(r["paginas"] == 3 and r["paginas_com_texto"] == 0,
           "conta as paginas mesmo sem texto", f"{r['paginas']}/{r['paginas_com_texto']}")

    print("\n--- PDF: truncado por paginas_max_pdf ---")
    # Um PDF longo, todo com texto. Lendo so as 5 primeiras paginas o
    # resultado e PARCIAL; nao pode virar ocr_requerido so porque a
    # densidade foi dividida pelo total de paginas do documento.
    # Paginas curtas de proposito: 5 paginas lidas dao ~150 caracteres cada
    # (bem acima do minimo), mas divididas pelas 20 paginas do documento dao
    # ~37 — abaixo do minimo. A conta tem que ser sobre o que foi lido.
    p = area / "longo.pdf"
    pdf_com_texto(p, 20, linhas_por_pagina=2)
    r = ex.extrair_pdf(p, {"worker": {"paginas_max_pdf": 5,
                                      "min_caracteres_por_pagina": 80}})
    contrato("pdf truncado", r)
    checar(r["caracteres"] / 20 < 80 <= r["caracteres"] / 5,
           "o caso de teste realmente distingue as duas contas",
           f"{r['caracteres']} caracteres em 5 de 20 paginas")
    checar(r["status"] == "parcial",
           "PDF de texto truncado -> parcial (nao ocr_requerido)", str(r["status"]))
    checar(r["paginas"] == 20 and r["paginas_com_texto"] == 5,
           "registra o total real e quantas foram lidas",
           f"{r['paginas']}/{r['paginas_com_texto']}")
    r_inteiro = ex.extrair_pdf(p, {"worker": {"paginas_max_pdf": 3000,
                                              "min_caracteres_por_pagina": 80}})
    checar(r_inteiro["status"] == "ok" and r_inteiro["paginas_com_texto"] == 20,
           "o mesmo PDF sem truncar -> ok com as 20 paginas",
           f"{r_inteiro['status']} {r_inteiro['paginas_com_texto']}")

    print("\n--- PDF: escaneado E truncado continua pedindo OCR ---")
    p = area / "escaneado_longo.pdf"
    pdf_sem_texto(p, 20)
    r = ex.extrair_pdf(p, {"worker": {"paginas_max_pdf": 5,
                                      "min_caracteres_por_pagina": 80}})
    checar(r["status"] == "ocr_requerido",
           "truncar nao esconde que o documento e escaneado", str(r["status"]))

    print("\n--- PDF: protegido por senha ---")
    p = area / "com_senha.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "confidencial", fontsize=12)
    doc.save(ex._lp(p), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    doc.close()
    r = ex.extrair_pdf(p, CFG)
    contrato("pdf protegido", r)
    checar(r["status"] == "protegido", "PDF com senha -> protegido", str(r["status"]))
    checar(r["erro"] != "", "o motivo fica registrado")

    print("\n--- PDF: corrompido ---")
    p = area / "corrompido.pdf"
    p.write_bytes(b"%PDF-1.4 isto nao e um PDF")
    r = ex.extrair_pdf(p, CFG)
    contrato("pdf corrompido", r)
    checar(r["status"] == "erro", "PDF corrompido -> erro", str(r["status"]))
    checar(r["erro"] != "" and r["texto"] == "", "erro descrito e sem texto falso")

    # ==================================================================
    print("\n--- DOCX ---")
    # ==================================================================
    p = area / "medicao.docx"
    documento = docx.Document()
    documento.add_paragraph("Relatorio de medicao 12 - RD-103")
    documento.add_paragraph("")                       # paragrafo vazio: ignorado
    tabela = documento.add_table(rows=2, cols=2)
    tabela.rows[0].cells[0].text = "Servico"
    tabela.rows[0].cells[1].text = PALAVRA
    tabela.rows[1].cells[0].text = "Quantidade"
    tabela.rows[1].cells[1].text = "1500 m3"
    documento.save(ex._lp(p))
    r = ex.extrair_docx(p, CFG)
    contrato("docx ok", r)
    checar(r["status"] == "ok", "DOCX -> ok", str(r["status"]))
    checar(PALAVRA in r["texto"], "texto de tabela entra na extracao")
    checar("Servico | terraplenagem" in r["texto"],
           "celulas da linha viram uma linha so, separadas por |")
    checar(r["paginas"] is None and r["slides"] is None and r["abas"] is None,
           "DOCX nao tem paginas/slides/abas — ficam None (documentado)")
    checar(r["paginas_texto"] == [(0, r["texto"])],
           "DOCX indexa como pagina 0 (documento inteiro)")

    print("\n--- DOCX: sem texto nenhum ---")
    p = area / "vazio.docx"
    docx.Document().save(ex._lp(p))
    r = ex.extrair_docx(p, CFG)
    contrato("docx vazio", r)
    checar(r["status"] == "vazio", "DOCX sem paragrafos -> vazio", str(r["status"]))

    print("\n--- DOCX: corrompido ---")
    p = area / "corrompido.docx"
    p.write_bytes(b"PK\x03\x04 isto nao e um docx")
    r = ex.extrair_docx(p, CFG)
    contrato("docx corrompido", r)
    checar(r["status"] == "erro", "DOCX corrompido -> erro", str(r["status"]))

    print("\n--- formatos sem biblioteca viram nao_suportado, nao erro ---")
    # Um formato que nunca vai abrir nao pode entrar no ciclo de 4 tentativas
    # do worker: ele tem que sair como UNSUPPORTED de primeira (REGRA 6).
    p = area / "antigo.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 OLE")
    r = ex.extrair_docx(p, CFG)
    checar(r["status"] == "nao_suportado", ".doc (OLE) -> nao_suportado", str(r["status"]))
    p = area / "antigo.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 OLE")
    checar(ex.extrair_xlsx(p, CFG)["status"] == "nao_suportado", ".xls -> nao_suportado")
    p = area / "antigo.ppt"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 OLE")
    checar(ex.extrair_pptx(p, CFG)["status"] == "nao_suportado", ".ppt -> nao_suportado")
    p = area / "planilha.xlsb"
    p.write_bytes(b"PK\x03\x04 xlsb binario")
    checar(ex.extrair_xlsx(p, CFG)["status"] == "nao_suportado", ".xlsb -> nao_suportado")
    p = area / "texto_rico.rtf"
    p.write_bytes(rb"{\rtf1\ansi Teste de RTF com " + PALAVRA.encode() + rb".}")
    r = ex.extrair_docx(p, CFG)
    contrato("rtf", r)
    checar(r["status"] == "nao_suportado",
           ".rtf -> nao_suportado (python-docx nao le RTF)", str(r["status"]))
    for ext, funcao in ((".odt", ex.extrair_docx), (".ods", ex.extrair_xlsx),
                        (".odp", ex.extrair_pptx)):
        p = area / f"aberto{ext}"
        p.write_bytes(b"PK\x03\x04 OpenDocument")
        checar(funcao(p, CFG)["status"] == "nao_suportado",
               f"{ext} (OpenDocument) -> nao_suportado")

    # ==================================================================
    print("\n--- XLSX ---")
    # ==================================================================
    p = area / "planilha.xlsx"
    livro = openpyxl.Workbook()
    aba = livro.active
    aba.title = "Resumo"
    aba["A1"] = "item"
    aba["B1"] = PALAVRA
    aba["A2"] = "quantidade"
    aba["B2"] = 1500
    segunda = livro.create_sheet("Detalhe")
    segunda["A1"] = "estaca"
    segunda["B1"] = 120
    livro.create_sheet("Branca")
    livro.save(ex._lp(p))
    r = ex.extrair_xlsx(p, CFG)
    contrato("xlsx ok", r)
    checar(r["status"] == "ok", "XLSX -> ok", str(r["status"]))
    checar(r["abas"] == 3, f"contou as 3 abas (abas={r['abas']})")
    checar(len(r["paginas_texto"]) == 2,
           "so as abas com conteudo viram pagina no indice",
           str(len(r["paginas_texto"])))
    checar("[aba] Resumo" in r["texto"] and "[aba] Detalhe" in r["texto"],
           "o nome da aba fica no texto extraido")
    checar(PALAVRA in r["texto"] and "1500" in r["texto"],
           "valores de texto e numericos entram")

    print("\n--- XLSX: limite de linhas -> parcial ---")
    p = area / "grande.xlsx"
    livro = openpyxl.Workbook()
    aba = livro.active
    for i in range(1, 60):
        aba.cell(row=i, column=1, value=f"linha {i} {PALAVRA}")
    livro.save(ex._lp(p))
    r = ex.extrair_xlsx(p, CFG)
    checar(r["status"] == "ok", "59 linhas: cabe inteiro -> ok", str(r["status"]))
    checar(r["texto"].count("linha ") == 59, "todas as linhas extraidas")

    print("\n--- XLSX: corrompido ---")
    p = area / "corrompida.xlsx"
    p.write_bytes(b"PK\x03\x04 isto nao e um xlsx")
    r = ex.extrair_xlsx(p, CFG)
    contrato("xlsx corrompido", r)
    checar(r["status"] == "erro", "XLSX corrompido -> erro", str(r["status"]))

    print("\n--- XLSX: formula sem valor em cache (limitacao conhecida) ---")
    p = area / "formula.xlsx"
    livro = openpyxl.Workbook()
    aba = livro.active
    aba["A1"] = "total"
    aba["B1"] = "=1*2"
    livro.save(ex._lp(p))
    r = ex.extrair_xlsx(p, CFG)
    checar("total" in r["texto"] and "2" not in r["texto"],
           "data_only=True: formula nunca calculada pelo Excel some (documentado)",
           repr(r["texto"]))

    # ==================================================================
    print("\n--- PPTX ---")
    # ==================================================================
    p = area / "reuniao.pptx"
    apresentacao = Presentation()
    slide = apresentacao.slides.add_slide(apresentacao.slide_layouts[5])
    slide.shapes.title.text = f"Reuniao de obra - {PALAVRA}"
    caixa = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(6), Inches(1))
    caixa.text_frame.text = "Decisao: refazer o trecho."
    slide.notes_slide.notes_text_frame.text = "Lembrar de cobrar o cronograma."
    apresentacao.slides.add_slide(apresentacao.slide_layouts[6])   # slide em branco
    apresentacao.save(ex._lp(p))
    r = ex.extrair_pptx(p, CFG)
    contrato("pptx ok", r)
    checar(r["status"] == "ok", "PPTX -> ok", str(r["status"]))
    checar(r["slides"] == 2, f"contou os 2 slides (slides={r['slides']})")
    checar(len(r["paginas_texto"]) == 1, "o slide em branco nao vira pagina do indice")
    checar(PALAVRA in r["texto"] and "refazer o trecho" in r["texto"],
           "titulo e caixa de texto extraidos")
    checar("[notas]" in r["texto"] and "cronograma" in r["texto"],
           "notas do apresentador extraidas e marcadas")

    print("\n--- PPTX: tabela dentro do slide ---")
    p = area / "com_tabela.pptx"
    apresentacao = Presentation()
    slide = apresentacao.slides.add_slide(apresentacao.slide_layouts[6])
    tabela = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(6), Inches(2)).table
    tabela.cell(0, 0).text = "Servico"
    tabela.cell(0, 1).text = PALAVRA
    tabela.cell(1, 0).text = "Prazo"
    tabela.cell(1, 1).text = "30 dias"
    apresentacao.save(ex._lp(p))
    r = ex.extrair_pptx(p, CFG)
    contrato("pptx tabela", r)
    checar(r["status"] == "ok", "slide que so tem tabela nao pode virar 'vazio'",
           str(r["status"]))
    checar(PALAVRA in r["texto"] and "30 dias" in r["texto"],
           "conteudo das celulas da tabela extraido", repr(r["texto"])[:160])

    print("\n--- PPTX: formas agrupadas ---")
    p = area / "com_grupo.pptx"
    apresentacao = Presentation()
    slide = apresentacao.slides.add_slide(apresentacao.slide_layouts[6])
    c1 = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(1))
    c1.text_frame.text = f"dentro do grupo: {PALAVRA}"
    c2 = slide.shapes.add_textbox(Inches(4), Inches(1), Inches(2), Inches(1))
    c2.text_frame.text = "segunda caixa agrupada"
    slide.shapes.add_group_shape([c1, c2])
    apresentacao.save(ex._lp(p))
    r = ex.extrair_pptx(p, CFG)
    contrato("pptx grupo", r)
    checar(PALAVRA in r["texto"] and "segunda caixa" in r["texto"],
           "texto dentro de forma agrupada nao se perde", repr(r["texto"])[:160])

    print("\n--- PPTX: sem texto e corrompido ---")
    p = area / "so_imagem.pptx"
    apresentacao = Presentation()
    for _ in range(4):
        apresentacao.slides.add_slide(apresentacao.slide_layouts[6])
    apresentacao.save(ex._lp(p))
    r = ex.extrair_pptx(p, CFG)
    checar(r["status"] == "vazio" and r["slides"] == 4,
           "apresentacao sem texto -> vazio, mas conta os slides",
           f"{r['status']} {r['slides']}")
    p = area / "corrompida.pptx"
    p.write_bytes(b"PK\x03\x04 isto nao e um pptx")
    r = ex.extrair_pptx(p, CFG)
    contrato("pptx corrompido", r)
    checar(r["status"] == "erro", "PPTX corrompido -> erro", str(r["status"]))

    # ==================================================================
    print("\n--- CSV ---")
    # ==================================================================
    p = area / "dados.csv"
    p.write_text(f"item;servico\n1;{PALAVRA}\n2;drenagem\n", encoding="utf-8")
    r = ex.extrair_csv(p, CFG)
    contrato("csv ok", r)
    checar(r["status"] == "ok", "CSV -> ok", str(r["status"]))
    checar(PALAVRA in r["texto"] and "drenagem" in r["texto"], "linhas extraidas")
    checar(r["texto"].count("\n") == 2, "3 linhas, 2 quebras", repr(r["texto"]))

    p = area / "virgula.csv"
    p.write_text(f"item,servico\n1,{PALAVRA}\n", encoding="utf-8")
    checar(PALAVRA in ex.extrair_csv(p, CFG)["texto"], "detecta separador virgula")

    p = area / "tabulado.tsv"
    p.write_text(f"item\tservico\n1\t{PALAVRA}\n", encoding="utf-8")
    checar(PALAVRA in ex.extrair_csv(p, CFG)["texto"], "detecta separador tabulacao")

    p = area / "campo_gigante.csv"
    p.write_text("a;b\n" + ("x" * 200_000) + ";2\n", encoding="utf-8")
    r = ex.extrair_csv(p, CFG)
    contrato("csv campo gigante", r)
    checar(r["status"] == "ok",
           "campo maior que o limite padrao do modulo csv nao vira erro",
           f"{r['status']} {r['erro'][:80]}")

    p = area / "vazio.csv"
    p.write_text("", encoding="utf-8")
    checar(ex.extrair_csv(p, CFG)["status"] == "vazio", "CSV sem conteudo -> vazio")

    # ==================================================================
    print("\n--- texto puro ---")
    # ==================================================================
    p = area / "anotacao.txt"
    p.write_text(f"conferir {PALAVRA} do lote 3", encoding="utf-8")
    r = ex.extrair_texto(p, CFG)
    contrato("txt ok", r)
    checar(r["status"] == "ok" and PALAVRA in r["texto"], "TXT -> ok")

    p = area / "com_bom.txt"
    p.write_bytes("﻿medição de terraplenagem".encode("utf-8"))
    r = ex.extrair_texto(p, CFG)
    checar(r["texto"].startswith("medi"), "BOM de UTF-8 nao entra no texto", repr(r["texto"]))

    p = area / "cp1252.txt"
    p.write_bytes("medição de terraplenagem".encode("cp1252"))
    r = ex.extrair_texto(p, CFG)
    checar("medição" in r["texto"], "arquivo cp1252 decodificado certo", repr(r["texto"]))

    p = area / "utf16.txt"
    p.write_bytes("medição de terraplenagem".encode("utf-16"))
    r = ex.extrair_texto(p, CFG)
    contrato("txt utf-16", r)
    checar("medição" in r["texto"],
           "arquivo UTF-16 (BOM) decodificado certo, nao como lixo cp1252",
           repr(r["texto"])[:120])
    checar("\x00" not in r["texto"], "nenhum byte nulo vai para o indice")

    p = area / "vazio.txt"
    p.write_text("", encoding="utf-8")
    checar(ex.extrair_texto(p, CFG)["status"] == "vazio", "TXT vazio -> vazio")

    # ==================================================================
    print("\n--- despacho por categoria ---")
    # ==================================================================
    r = ex.extrair(area / "qualquer.zip", "compactado", CFG)
    contrato("categoria sem extrator", r)
    checar(r["status"] == "nao_suportado",
           "categoria sem extrator -> nao_suportado", str(r["status"]))
    r = ex.extrair(area / "qualquer", None, CFG)
    checar(r["status"] == "nao_suportado", "categoria None -> nao_suportado")
    checar(set(ex.EXTRATORES) == {"pdf", "docx", "pptx", "xlsx", "csv", "texto"},
           "o despacho cobre exatamente os extratores documentados",
           str(sorted(ex.EXTRATORES)))
    checar(set(ex.VERSOES) == set(ex.EXTRATORES),
           "todo extrator tem versao declarada", str(set(ex.VERSOES) ^ set(ex.EXTRATORES)))

    print("\n--- nada derruba o worker ---")
    for categoria in ("pdf", "docx", "pptx", "xlsx", "csv", "texto"):
        # arquivo que nao existe
        r = ex.extrair(area / f"nao_existe.{categoria}", categoria, CFG)
        contrato(f"{categoria} inexistente", r)
        checar(r["status"] == "erro",
               f"{categoria}: arquivo inexistente -> erro sem excecao", str(r["status"]))
        # um diretorio no lugar de um arquivo
        r = ex.extrair(area, categoria, CFG)
        checar(r["status"] == "erro",
               f"{categoria}: diretorio no lugar do arquivo -> erro sem excecao",
               str(r["status"]))
        # bytes aleatorios com a extensao errada
        lixo = area / f"lixo_{categoria}.bin"
        lixo.write_bytes(bytes(range(256)) * 4)
        r = ex.extrair(lixo, categoria, CFG)
        contrato(f"{categoria} lixo binario", r)

    # ==================================================================
    print("\n--- REGRA 3: caminho acima de 260 caracteres ---")
    # ==================================================================
    fundo = area
    for i in range(12):
        fundo = fundo / f"pasta_bem_comprida_para_estourar_o_max_path_{i:02d}"
        os.mkdir(ex._lp(fundo))
    checar(len(str(fundo)) > 260, f"caminho de teste tem {len(str(fundo))} caracteres")

    alvo = fundo / "documento.pdf"
    pdf_com_texto(alvo, 2)
    r = ex.extrair_pdf(alvo, CFG)
    checar(r["status"] == "ok" and r["paginas"] == 2,
           "PDF em caminho longo", f"{r['status']} {r['erro'][:80]}")

    alvo = fundo / "documento.docx"
    d = docx.Document()
    d.add_paragraph(f"{PALAVRA} no fundo do poco")
    d.save(ex._lp(alvo))
    r = ex.extrair_docx(alvo, CFG)
    checar(r["status"] == "ok", "DOCX em caminho longo", f"{r['status']} {r['erro'][:80]}")

    alvo = fundo / "documento.xlsx"
    livro = openpyxl.Workbook()
    livro.active["A1"] = PALAVRA
    livro.save(ex._lp(alvo))
    r = ex.extrair_xlsx(alvo, CFG)
    checar(r["status"] == "ok", "XLSX em caminho longo", f"{r['status']} {r['erro'][:80]}")

    alvo = fundo / "documento.pptx"
    apresentacao = Presentation()
    apresentacao.slides.add_slide(apresentacao.slide_layouts[5]).shapes.title.text = PALAVRA
    apresentacao.save(ex._lp(alvo))
    r = ex.extrair_pptx(alvo, CFG)
    checar(r["status"] == "ok", "PPTX em caminho longo", f"{r['status']} {r['erro'][:80]}")

    alvo = fundo / "documento.txt"
    Path(ex._lp(alvo)).write_text(PALAVRA, encoding="utf-8")
    r = ex.extrair_texto(alvo, CFG)
    checar(r["status"] == "ok", "TXT em caminho longo", f"{r['status']} {r['erro'][:80]}")

    alvo = fundo / "documento.csv"
    Path(ex._lp(alvo)).write_text(f"a;b\n1;{PALAVRA}\n", encoding="utf-8")
    r = ex.extrair_csv(alvo, CFG)
    checar(r["status"] == "ok", "CSV em caminho longo", f"{r['status']} {r['erro'][:80]}")

    # ==================================================================
    print("\n--- o extrator nunca escreve nada ---")
    # ==================================================================
    # Nenhum extrator pode criar arquivo ao lado do original (REGRA 1).
    controle = area / "somente_leitura"
    controle.mkdir()
    origem = controle / "unico.pdf"
    pdf_com_texto(origem, 1)
    antes = sorted(x.name for x in controle.iterdir())
    ex.extrair(origem, "pdf", CFG)
    depois = sorted(x.name for x in controle.iterdir())
    checar(antes == depois, "extrair nao cria nada na pasta do arquivo", str(depois))

    # A checagem acima so via NOMES e so do PDF: um extrator que regravasse o
    # original (mesmo nome) passava. Agora os seis formatos, com tamanho,
    # mtime em ns e sha256 do conteudo (REGRA 1: nao alterar, nao so nao criar).
    todos = area / "somente_leitura_todos"
    todos.mkdir()
    por_categoria = {"pdf": "documento.pdf", "docx": "documento.docx",
                     "xlsx": "documento.xlsx", "pptx": "documento.pptx",
                     "texto": "documento.txt", "csv": "documento.csv"}
    for nome in por_categoria.values():
        shutil.copy2(ex._lp(fundo / nome), todos / nome)
    foto_antes = foto_exata(todos)
    status = {cat: ex.extrair(todos / nome, cat, CFG)["status"]
              for cat, nome in por_categoria.items()}
    foto_depois = foto_exata(todos)
    checar(set(status.values()) == {"ok"}, "os seis extratores leram a copia", str(status))
    checar(foto_depois == foto_antes,
           "nenhum extrator cria, altera ou regrava o original (6 formatos)",
           str(sorted(k for k in set(foto_antes) | set(foto_depois)
                      if foto_antes.get(k) != foto_depois.get(k))))

    testes_e9(area)
    testes_metricas_ocr(area)

    shutil.rmtree(ex._lp(area), ignore_errors=True)
    return placar_final()


# ======================================================================
# E9 — extratores definitivos de PDF, DOCX, XLSX/XLSM e PPTX
# ======================================================================

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def png(lado: int = 64) -> bytes:
    import fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, lado, lado), 0)
    pix.clear_with(180)
    return pix.tobytes("png")


def regravar_zip(origem: Path, destino: Path, trocas: dict, novos: dict | None = None):
    """Copia um pacote OOXML trocando o conteudo de membros (bytes -> bytes)."""
    import zipfile
    with zipfile.ZipFile(ex._lp(origem)) as z, \
            zipfile.ZipFile(ex._lp(destino), "w", zipfile.ZIP_DEFLATED) as saida:
        for info in z.infolist():
            dados = z.read(info)
            if info.filename in trocas:
                dados = trocas[info.filename](dados)
            saida.writestr(info, dados)
        for nome, dados in (novos or {}).items():
            saida.writestr(nome, dados)


def trocar_bytes(antigo: str, novo: str):
    def troca(dados: bytes) -> bytes:
        assert antigo.encode() in dados, f"{antigo!r} nao esta no membro"
        return dados.replace(antigo.encode(), novo.encode())
    return troca


def pdf_paginas(destino: Path, roteiro: list, metadados: dict | None = None):
    """roteiro: por pagina, uma lista de ('texto', linhas) | ('carimbo', txt) |
    ('imagem_cheia',) | ('logo',) | ('tabela',) | ('desenho',) | () em branco."""
    import fitz
    doc = fitz.open()
    figura = png()
    for itens in roteiro:
        pagina = doc.new_page()
        for item in itens:
            if item[0] == "texto":
                for linha in range(item[1]):
                    pagina.insert_text((50, 60 + linha * 16),
                                       f"Linha {linha}: servico de {PALAVRA} na estaca 120 "
                                       f"da RD-103, medicao mensal.", fontsize=9)
            elif item[0] == "carimbo":
                pagina.insert_text((400, 800), item[1], fontsize=8)
            elif item[0] == "imagem_cheia":
                pagina.insert_image(pagina.rect, stream=figura)
            elif item[0] == "logo":
                pagina.insert_image(fitz.Rect(20, 20, 60, 60), stream=figura)
            elif item[0] == "tabela":
                celulas = [["Servico", "Unidade", "Quantidade"],
                           [PALAVRA, "m3", "1500"], ["drenagem", "m", "320"]]
                for i, linha in enumerate(celulas):
                    for j, valor in enumerate(linha):
                        pagina.draw_rect(fitz.Rect(50 + j * 150, 100 + i * 20,
                                                   200 + j * 150, 120 + i * 20))
                        pagina.insert_text((55 + j * 150, 115 + i * 20), valor, fontsize=9)
            elif item[0] == "desenho":
                pagina.draw_rect(fitz.Rect(50, 50, 500, 700), color=(0, 0, 0), width=2)
    if metadados:
        doc.set_metadata(metadados)
    doc.save(ex._lp(destino))
    doc.close()


def caixa_de_texto_xml(texto: str) -> str:
    """Paragrafo com uma caixa de texto como o Word grava: DrawingML em
    mc:Choice e a MESMA caixa em VML no mc:Fallback."""
    return (
        f'<w:p xmlns:w="{W_NS}" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
        'xmlns:v="urn:schemas-microsoft-com:vml">'
        '<w:r><w:t>ancora</w:t></w:r><w:r><mc:AlternateContent>'
        '<mc:Choice Requires="wps"><w:drawing><wp:anchor><a:graphic>'
        '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        f'<wps:wsp><wps:txbx><w:txbxContent><w:p><w:r><w:t>{texto}</w:t></w:r></w:p>'
        '</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor>'
        '</w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox>'
        f'<w:txbxContent><w:p><w:r><w:t>{texto}</w:t></w:r></w:p></w:txbxContent>'
        '</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>')


def testes_e9(area: Path) -> None:
    import datetime as dt
    import hashlib
    import io

    import docx
    import openpyxl
    import yaml
    from docx.oxml import parse_xml
    from pptx import Presentation
    from pptx.util import Inches

    def sha(p: Path) -> str:
        return hashlib.sha256(Path(ex._lp(p)).read_bytes()).hexdigest()

    pasta = area / "e9"
    pasta.mkdir()

    # ==================================================================
    print("\n--- E9 / L7: suporta() — quem tem leitor, decidido pela extensao ---")
    # ==================================================================
    checar(ex.extensao(r"Programa\Obra.v2\Planilha FINAL.XLS") == ".xls",
           "extensao() com '\\', ponto na pasta e maiusculas")
    checar(ex.extensao("Programa/pasta.v2/sem_extensao") == "" and ex.extensao(".oculto") == "",
           "sem extensao -> '' (ponto na pasta nao conta)")
    for ext, cat in ((".xls", "xlsx"), (".xlsb", "xlsx"), (".doc", "docx"), (".ppt", "pptx"),
                     (".rtf", "docx"), (".odt", "docx"), (".ods", "xlsx"), (".odp", "pptx"),
                     (".zip", "compactado"), (".pdf", None)):
        checar(not ex.suporta(ext, cat), f"suporta({ext}, {cat}) = False",
               str(ex.motivo_sem_leitor(ext, cat)))
    for ext, cat in ((".pdf", "pdf"), (".docx", "docx"), (".docm", "docx"), (".dotx", "docx"),
                     (".xlsx", "xlsx"), (".xlsm", "xlsx"), (".pptx", "pptx"),
                     (".pptm", "pptx"), (".potx", "pptx"), (".qualquer", "texto"),
                     (".xls", "csv")):
        checar(ex.suporta(ext, cat), f"suporta({ext}, {cat}) = True")

    # A regra pre-download e a MESMA do extrator: para cada extensao do yaml,
    # suporta() == False  <=>  o extrator devolve nao_suportado pelo formato.
    cfg_yaml = yaml.safe_load((Path(__file__).resolve().parents[1] / "config"
                               / "projeto.yaml").read_text(encoding="utf-8"))
    divergentes = []
    for categoria, extensoes in (cfg_yaml.get("categorias") or {}).items():
        for ext in extensoes:
            alvo = pasta / f"amostra_{categoria}{ext}"
            alvo.write_bytes(b"PK\x03\x04 bytes que nenhuma biblioteca abre")
            r = ex.extrair(alvo, categoria, CFG)
            if (r["status"] == "nao_suportado") != (not ex.suporta(ext, categoria)):
                divergentes.append(f"{categoria}{ext}: {r['status']}")
    checar(not divergentes, "suporta() concorda com o extrator em toda extensao do yaml",
           str(divergentes))

    # ==================================================================
    print("\n--- E9 / PDF: OCR decidido pagina a pagina (L2) ---")
    # ==================================================================
    p = pasta / "misto.pdf"
    pdf_paginas(p, [[("texto", 40)]] + [[("imagem_cheia",)]] * 9)
    r = ex.extrair_pdf(p, CFG)
    contrato("pdf misto", r)
    checar(r["densidade_texto"] >= 80,
           "o caso e o 'falso completo' da auditoria: pela MEDIA daria ok",
           str(r["densidade_texto"]))
    checar(r["status"] == "ocr_requerido", "1 pagina de texto + 9 escaneadas -> ocr_requerido",
           f"{r['status']} {r['erro']}")
    checar(r.get("ocr_tipo") == "misto" and r.get("paginas_ocr") == list(range(2, 11)),
           "registra o tipo (misto) e QUAIS paginas pedem OCR",
           f"{r.get('ocr_tipo')} {r.get('paginas_ocr')}")
    checar(r["erro"].startswith("OCR (misto): 9 de 10") and "2-10" in r["erro"],
           "o motivo diz quantas e quais paginas", r["erro"])
    checar([n for n, _ in r["paginas_texto"]] == [1] and PALAVRA in r["texto"],
           "o texto da pagina digital e extraido e indexado mesmo assim")

    p = pasta / "escaneado_imagem.pdf"
    pdf_paginas(p, [[("imagem_cheia",)]] * 3)
    r = ex.extrair_pdf(p, CFG)
    checar(r["status"] == "ocr_requerido" and r.get("ocr_tipo") == "escaneado"
           and r.get("paginas_ocr") == [1, 2, 3], "todas as paginas imagem -> escaneado",
           f"{r['status']} {r.get('ocr_tipo')} {r.get('paginas_ocr')}")
    checar(r["imagens"] == 3, f"conta as imagens ({r['imagens']})")

    p = pasta / "escaneado_com_carimbo.pdf"
    pdf_paginas(p, [[("imagem_cheia",), ("carimbo", "Assinado em 12/03/2025")]] * 2)
    r = ex.extrair_pdf(p, CFG)
    checar(r["status"] == "ocr_requerido" and r.get("paginas_ocr") == [1, 2],
           "pagina escaneada com carimbo digital curto continua pedindo OCR",
           f"{r['status']} {r.get('paginas_ocr')}")

    p = pasta / "prancha.pdf"
    pdf_paginas(p, [[("desenho",), ("carimbo", "PRANCHA 01 - REV 03")]] * 10)
    r = ex.extrair_pdf(p, CFG)
    checar(r["densidade_texto"] < 80, "o caso e o 'falso OCR' da auditoria (densidade baixa)",
           str(r["densidade_texto"]))
    checar(r["status"] == "ok" and r.get("paginas_ocr") == [],
           "prancha vetorial com carimbo de texto real -> ok, sem OCR",
           f"{r['status']} {r.get('paginas_ocr')} {r['erro']}")

    p = pasta / "capa_com_logo.pdf"
    pdf_paginas(p, [[("logo",), ("carimbo", "RELATORIO MENSAL")], [("texto", 10)]])
    r = ex.extrair_pdf(p, CFG)
    checar(r["status"] == "ok", "capa curta com logo pequeno nao vira OCR",
           f"{r['status']} {r.get('paginas_ocr')}")

    p = pasta / "so_desenho.pdf"
    pdf_paginas(p, [[("texto", 10)], [("desenho",)]])
    r = ex.extrair_pdf(p, CFG)
    checar(r["status"] == "ocr_requerido" and r.get("paginas_ocr") == [2],
           "pagina so com desenho e nenhum texto (texto em curva) -> OCR",
           f"{r['status']} {r.get('paginas_ocr')}")

    p = pasta / "branca_no_meio.pdf"
    pdf_paginas(p, [[("texto", 8)], [], [("texto", 8)]])
    r = ex.extrair_pdf(p, CFG)
    checar(r["status"] == "ok" and r.get("paginas_ocr") == [],
           "pagina em branco (sem conteudo nenhum) nao pede OCR", f"{r['status']} {r['erro']}")
    checar([n for n, _ in r["paginas_texto"]] == [1, 3], "a numeracao pula a branca")
    unidades = r["texto"].split("\f")
    checar(len(unidades) == 4 and unidades[1] == "" and unidades[3] == ""
           and unidades[0] == r["paginas_texto"][0][1],
           "texto: cada pagina terminada por \\f, a branca vazia no seu lugar",
           repr([u[:15] for u in unidades]))
    checar(r["caracteres"] == len(r["texto"]), "caracteres conta o texto com separadores")

    p = pasta / "so_brancas.pdf"
    pdf_paginas(p, [[], []])
    r = ex.extrair_pdf(p, CFG)
    checar(r["status"] == "vazio" and r["texto"] == "" and r.get("paginas_ocr") == [],
           "PDF so de paginas brancas -> vazio (nada a ler, nada a OCR)", str(r["status"]))

    p = pasta / "tabela.pdf"
    pdf_paginas(p, [[("tabela",), ("texto", 6)]])
    r = ex.extrair_pdf(p, CFG)
    checar(all(v in r["texto"] for v in ("Quantidade", "1500", "drenagem", "320")),
           "texto das celulas de tabela entra (sem estrutura de tabela)")
    checar(r.get("tabelas") is None, "PDF nao conta tabelas (documentado)")

    p = pasta / "metadados.pdf"
    pdf_paginas(p, [[("texto", 6)]], {"title": "Notificacao 12", "author": "Fiscal do Programa",
                                     "subject": "Medicao", "keywords": "RD-103",
                                     "creationDate": "D:20240315143000-03'00'",
                                     "modDate": "D:20240316090000Z"})
    r = ex.extrair_pdf(p, CFG)
    m = (r.get("metadados") or {})
    checar(m.get("titulo") == "Notificacao 12" and m.get("autor") == "Fiscal do Programa",
           "metadados: titulo e autor", str(m))
    checar(m.get("criado_em") == "2024-03-15T14:30:00-03:00"
           and m.get("modificado_em") == "2024-03-16T09:00:00+00:00",
           "datas do PDF viram ISO 8601", f"{m.get('criado_em')} {m.get('modificado_em')}")
    checar(ex._data_pdf("lixo") == "lixo" and ex._data_pdf("") is None,
           "data fora do padrao fica como veio; vazia some")

    p = pasta / "longo_erro.pdf"
    pdf_paginas(p, [[("texto", 6)]] * 6)
    r = ex.extrair_pdf(p, {"worker": {"paginas_max_pdf": 4, "min_caracteres_por_pagina": 80}})
    checar(r["status"] == "parcial" and r["erro"].startswith("LIMITE: lidas 4 de 6"),
           "parcial por teto de paginas diz o porque", f"{r['status']} {r['erro']}")

    checar(ex._limpar_unidade("a\fb\x00c ") == "a\nbc",
           "\\f e nulo nunca entram numa unidade (o separador e exato)")
    checar(ex._faixas([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10", "faixas de paginas compactas")

    # ==================================================================
    print("\n--- E9 / DOCX: cabecalho, rodape, caixa de texto, ordem (L4) ---")
    # ==================================================================
    p = pasta / "contrato.docx"
    d = docx.Document()
    secao = d.sections[0]
    secao.header.paragraphs[0].text = "CONTRATO 123/2025 - DYNATEST"
    secao.footer.paragraphs[0].text = "Rev. 03 - documento controlado"
    secao.different_first_page_header_footer = True
    secao.first_page_header.paragraphs[0].text = "CONTRATO 123/2025 - DYNATEST"
    d.add_paragraph("ANTES da tabela")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Servico"
    t.cell(0, 1).text = PALAVRA
    t.cell(1, 0).text = "Detalhe"
    interna = t.cell(1, 1).add_table(rows=1, cols=2)
    interna.cell(0, 0).text = "aninhada A"
    interna.cell(0, 1).text = "aninhada B"
    d.add_paragraph("DEPOIS da tabela")
    d.paragraphs[-1]._p.addnext(parse_xml(caixa_de_texto_xml("CAIXA: OBRA RD-103 LOTE 3")))
    d.core_properties.title = "Contrato de supervisao"
    d.core_properties.author = "Juridico"
    d.core_properties.created = dt.datetime(2025, 1, 10, 8, 0, 0)
    d.save(ex._lp(p))
    r = ex.extrair_docx(p, CFG)
    contrato("docx completo", r)
    checar(r["status"] == "ok", "DOCX -> ok", f"{r['status']} {r['erro']}")
    checar("[cabecalho] CONTRATO 123/2025 - DYNATEST" in r["texto"],
           "cabecalho entra no texto, marcado", r["texto"][:120])
    checar(r["texto"].count("CONTRATO 123/2025") == 1,
           "cabecalho repetido (1a pagina = padrao) sai uma vez so")
    checar("[rodape] Rev. 03" in r["texto"], "rodape entra no texto, marcado")
    checar(r["texto"].count("CAIXA: OBRA RD-103 LOTE 3") == 1,
           "caixa de texto entra UMA vez (o Fallback VML e ignorado)",
           str(r["texto"].count("CAIXA: OBRA RD-103 LOTE 3")))
    ordem = [r["texto"].find(x) for x in ("[cabecalho]", "ANTES", f"Servico | {PALAVRA}",
                                          "DEPOIS", "CAIXA:", "[rodape]")]
    checar(-1 not in ordem and ordem == sorted(ordem),
           "ordem do documento: cabecalho, corpo (paragrafo, tabela, paragrafo, caixa), rodape",
           str(ordem))
    checar("aninhada A | aninhada B" in r["texto"], "tabela aninhada na celula entra")
    checar(r.get("tabelas") == 2, f"conta as tabelas, inclusive a aninhada ({r.get('tabelas')})")
    checar((r.get("metadados") or {}).get("titulo") == "Contrato de supervisao"
           and (r.get("metadados") or {}).get("autor") == "Juridico"
           and (r.get("metadados") or {}).get("criado_em", "").startswith("2025-01-10T08:00:00"),
           "metadados: titulo, autor, criado", str((r.get("metadados") or {})))
    checar(r["paginas_texto"] == [(0, r["texto"])] and "\f" not in r["texto"],
           "DOCX continua um documento so (pagina depende de layout), sem \\f")

    p = pasta / "so_imagem.docx"
    d = docx.Document()
    d.add_picture(io.BytesIO(png()))
    d.save(ex._lp(p))
    r = ex.extrair_docx(p, CFG)
    checar(r["status"] == "ocr_requerido" and r["imagens"] == 1,
           "DOCX so com imagem colada -> ocr_requerido (antes: vazio -> COMPLETED)",
           f"{r['status']} {r['imagens']}")
    p = pasta / "texto_e_imagem.docx"
    d = docx.Document()
    d.add_paragraph(f"Relatorio fotografico de {PALAVRA}")
    d.add_picture(io.BytesIO(png()))
    d.save(ex._lp(p))
    r = ex.extrair_docx(p, CFG)
    checar(r["status"] == "ok" and r["imagens"] == 1,
           "DOCX com texto e imagem -> ok, imagem contada", f"{r['status']} {r['imagens']}")
    r = ex.extrair_docx(pasta / "contrato.docx", {"worker": {"max_caracteres_documento": 30}})
    checar(r["status"] == "parcial" and r["caracteres"] == 30
           and r["erro"].startswith("LIMITE:"),
           "DOCX acima de max_caracteres_documento -> parcial, cortado e explicado",
           f"{r['status']} {r['caracteres']} {r['erro']}")

    print("\n--- E9 / DOCX: .docm e .dotx (mesmo pacote, outro content type) ---")
    ct_doc = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    for ext, ct in ((".docm", "application/vnd.ms-word.document.macroEnabled.main+xml"),
                    (".dotx", "application/vnd.openxmlformats-officedocument."
                              "wordprocessingml.template.main+xml")):
        alvo = pasta / f"modelo{ext}"
        regravar_zip(pasta / "contrato.docx", alvo,
                     {"[Content_Types].xml": trocar_bytes(ct_doc, ct)})
        try:
            docx.Document(ex._lp(alvo))
            recusa = False
        except ValueError:
            recusa = True
        checar(recusa, f"python-docx sozinho recusa {ext} (antes: erro x4 -> ERROR_FINAL)")
        antes = sha(alvo)
        r = ex.extrair_docx(alvo, CFG)
        checar(r["status"] == "ok" and "CONTRATO 123/2025" in r["texto"],
               f"{ext} lido pela copia em memoria", f"{r['status']} {r['erro'][:80]}")
        checar(sha(alvo) == antes, f"{ext}: o original nao foi tocado")

    print("\n--- E9 / OOXML cifrado ou binario renomeado ---")
    for ext, funcao in ((".docx", ex.extrair_docx), (".xlsx", ex.extrair_xlsx),
                        (".pptx", ex.extrair_pptx)):
        cifrado = pasta / f"com_senha{ext}"
        # a marca atravessa a fronteira do bloco de 1 MB da leitura
        marca = "EncryptedPackage".encode("utf-16-le")
        corpo = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * ((1 << 20) - 20)
        cifrado.write_bytes(corpo + marca + b"\x00" * 512)
        r = funcao(cifrado, CFG)
        contrato(f"{ext} cifrado", r)
        checar(r["status"] == "protegido", f"{ext} com senha (OLE EncryptedPackage) -> protegido",
               f"{r['status']} {r['erro']}")
        renomeado = pasta / f"legado_renomeado{ext}"
        renomeado.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 4096)
        r = funcao(renomeado, CFG)
        checar(r["status"] == "nao_suportado",
               f"{ext} que e binario OLE sem senha -> nao_suportado (nao erro x4)",
               f"{r['status']} {r['erro']}")

    # ==================================================================
    print("\n--- E9 / XLSX e XLSM ---")
    # ==================================================================
    p = pasta / "abas.xlsx"
    livro = openpyxl.Workbook()
    livro.active.title = "Resumo"
    livro.active["A1"] = PALAVRA
    livro.create_sheet("Branca")
    det = livro.create_sheet("Detalhe")
    for i in range(1, 21):
        det.cell(row=i, column=1, value=f"estaca {i}")
        det.cell(row=i, column=2, value=i * 10)
    from openpyxl.chart import BarChart, Reference
    grafico = BarChart()
    grafico.add_data(Reference(det, min_col=2, min_row=1, max_row=5))
    livro.create_chartsheet("Grafico").add_chart(grafico)
    from openpyxl.drawing.image import Image as ImagemXL
    livro["Resumo"].add_image(ImagemXL(io.BytesIO(png())), "C3")
    livro.properties.title = "Medicao 41"
    livro.properties.creator = "Engenharia"
    livro.save(ex._lp(p))
    r = ex.extrair_xlsx(p, CFG)
    contrato("xlsx abas", r)
    checar(r["status"] == "ok",
           "aba de grafico (chartsheet) nao derruba a planilha (antes: erro x4)",
           f"{r['status']} {r['erro']}")
    checar(r["abas"] == 4 and [n for n, _ in r["paginas_texto"]] == [1, 3],
           "4 abas; so as com celula viram unidade, com o numero da aba",
           f"{r['abas']} {[n for n, _ in r['paginas_texto']]}")
    checar(r["paginas_texto"][1][1].startswith("[aba] Detalhe\nestaca 1 | 10"),
           "unidade = nome da aba + linhas, celulas por ' | '", r["paginas_texto"][1][1][:40])
    checar(r["texto"].split("\f")[1] == "", "aba vazia ocupa a sua posicao no texto")
    checar(r["imagens"] == 1, f"imagem contada pelo pacote ({r['imagens']})")
    checar((r.get("metadados") or {}).get("titulo") == "Medicao 41"
           and (r.get("metadados") or {}).get("autor") == "Engenharia", "metadados", str((r.get("metadados") or {})))

    r = ex.extrair_xlsx(p, {"worker": {"xlsx_max_linhas_aba": 10}})
    checar(r["status"] == "parcial" and "estaca 10" in r["texto"]
           and "estaca 11" not in r["texto"] and "Detalhe' cortada em 10" in r["erro"],
           "teto de linhas por aba -> parcial, com a aba no motivo",
           f"{r['status']} {r['erro']}")
    r = ex.extrair_xlsx(p, {"worker": {"xlsx_max_caracteres": 40}})
    checar(r["status"] == "parcial" and "xlsx_max_caracteres" in r["erro"],
           "teto de caracteres -> parcial, explicado", f"{r['status']} {r['erro']}")

    print("\n--- E9 / XLSX: formula COM valor em cache ---")
    p = pasta / "formula_cache.xlsx"
    regravar_zip(area / "formula.xlsx", p,                # criado na secao XLSX acima
                 {"xl/worksheets/sheet1.xml": trocar_bytes("<f>1*2</f><v></v>",
                                                           "<f>1*2</f><v>2</v>")})
    r = ex.extrair_xlsx(p, CFG)
    checar(r["texto"].startswith("[aba] Sheet\ntotal | 2"),
           "data_only: a formula vale o valor que o Excel deixou em cache", repr(r["texto"]))

    print("\n--- E9 / XLSM: lido sem carregar macro ---")
    p = pasta / "macro.xlsm"
    rels = "xl/_rels/workbook.xml.rels"
    regravar_zip(area / "planilha.xlsx", p, {
        "[Content_Types].xml": trocar_bytes(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
            "application/vnd.ms-excel.sheet.macroEnabled.main+xml"),
        rels: trocar_bytes("</Relationships>",
                           '<Relationship Id="rIdVBA" Type="http://schemas.microsoft.com/'
                           'office/2006/relationships/vbaProject" Target="vbaProject.bin"/>'
                           "</Relationships>"),
    }, {"xl/vbaProject.bin": b"\xd0\xcf\x11\xe0 Sub Auto_Open() Kill \"C:\\*\" End Sub"})
    antes = sha(p)
    r = ex.extrair_xlsx(p, CFG)
    contrato("xlsm", r)
    checar(r["status"] == "ok" and PALAVRA in r["texto"], "XLSM -> ok, celulas lidas",
           f"{r['status']} {r['erro']}")
    checar((r.get("metadados") or {}).get("macros") is True, "registra que ha macro (nunca executada)")
    checar("Auto_Open" not in r["texto"], "o codigo da macro nao vira texto")
    checar(sha(p) == antes, "XLSM: o original nao foi tocado")

    # ==================================================================
    print("\n--- E9 / PPTX ---")
    # ==================================================================
    p = pasta / "apresentacao.pptx"
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = f"Status da obra - {PALAVRA}"
    s.notes_slide.notes_text_frame.text = "falar do cronograma"
    prs.slides.add_slide(prs.slide_layouts[6])
    s = prs.slides.add_slide(prs.slide_layouts[6])
    s.shapes.add_table(1, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table.cell(0, 0).text = \
        "Prazo"
    s.shapes.add_picture(io.BytesIO(png()), Inches(1), Inches(3))
    prs.core_properties.title = "Reuniao mensal"
    prs.core_properties.author = "Coordenacao"
    prs.save(ex._lp(p))
    r = ex.extrair_pptx(p, CFG)
    contrato("pptx e9", r)
    checar(r["status"] == "ok" and r["slides"] == 3, "PPTX -> ok, 3 slides",
           f"{r['status']} {r['slides']}")
    checar([n for n, _ in r["paginas_texto"]] == [1, 3], "slide em branco pula o numero")
    checar("[notas] falar do cronograma" in r["paginas_texto"][0][1],
           "notas ficam NA unidade do seu slide")
    checar(r["texto"].split("\f")[1] == "" and r["texto"].count("\f") == 3,
           "texto: cada slide terminado por \\f, o vazio no seu lugar")
    checar(r.get("tabelas") == 1 and r["imagens"] == 1, "conta tabelas e imagens",
           f"{r.get('tabelas')} {r['imagens']}")
    checar((r.get("metadados") or {}).get("titulo") == "Reuniao mensal"
           and (r.get("metadados") or {}).get("autor") == "Coordenacao", "metadados", str((r.get("metadados") or {})))

    p = pasta / "album.pptx"
    prs = Presentation()
    for _ in range(2):
        prs.slides.add_slide(prs.slide_layouts[6]).shapes.add_picture(
            io.BytesIO(png()), Inches(1), Inches(1))
    prs.save(ex._lp(p))
    r = ex.extrair_pptx(p, CFG)
    checar(r["status"] == "ocr_requerido" and r["imagens"] == 2,
           "apresentacao so de imagens -> ocr_requerido", f"{r['status']} {r['imagens']}")

    r = ex.extrair_pptx(pasta / "apresentacao.pptx", {"worker": {"max_caracteres_documento": 10}})
    checar(r["status"] == "parcial" and r["caracteres"] <= 11 and r["erro"].startswith("LIMITE"),
           "PPTX acima de max_caracteres_documento -> parcial", f"{r['status']} {r['erro']}")

    alvo = pasta / "modelo.potx"
    regravar_zip(pasta / "apresentacao.pptx", alvo, {"[Content_Types].xml": trocar_bytes(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml")})
    try:
        Presentation(ex._lp(alvo))
        recusa = False
    except ValueError:
        recusa = True
    r = ex.extrair_pptx(alvo, CFG)
    checar(recusa and r["status"] == "ok" and PALAVRA in r["texto"],
           ".potx: python-pptx recusa, o extrator le pela copia em memoria",
           f"{recusa} {r['status']} {r['erro'][:80]}")

    print("\n--- E9 / versoes ---")
    checar(ex.VERSOES == {"pdf": "pdf/1.3", "docx": "docx/1.1", "pptx": "pptx/1.2",
                          "xlsx": "xlsx/1.1", "csv": "csv/1.1", "texto": "texto/1.1"},
           "versoes sobem so nos extratores alterados (pdf/1.3: metricas de OCR)",
           str(ex.VERSOES))
    checar(set(ex.CHAVES_EXTRAS) <= set(ex._vazio("pdf", "erro")),
           "todo retorno traz tambem as chaves extras (None quando nao se aplica)")


def testes_metricas_ocr(area: Path) -> None:
    """R6 / pdf/1.3: as metricas por pagina vao para o perfil e bastam para
    recalcular a decisao de OCR com outra calibracao, sem baixar o PDF."""
    import json

    print("\n--- pdf/1.3: metricas de OCR por pagina no resultado (R6) ---")
    pasta = area / "metricas_ocr"
    pasta.mkdir()
    p = pasta / "misto_metricas.pdf"
    pdf_paginas(p, [
        [("texto", 40)],                                          # 1 digital
        [("imagem_cheia",)],                                      # 2 escaneada
        [("imagem_cheia",), ("carimbo", "Assinado em 12/03/2025")],  # 3 escaneada + carimbo
        [("desenho",), ("carimbo", "PRANCHA 01 - REV 03")],       # 4 prancha vetorial
        [("logo",), ("carimbo", "RELATORIO MENSAL")],             # 5 capa com logo
        [("desenho",)],                                           # 6 so desenho
        [],                                                       # 7 branca
        [("logo",), ("texto", 45)],                               # 8 texto longo + logo
        [("logo",), ("texto", 5)],                                # 9 texto medio + logo
    ])
    r = ex.extrair_pdf(p, CFG)
    contrato("pdf com metricas", r)
    om = r.get("ocr_metricas")
    checar(isinstance(om, dict) and om.get("formato") == ex.FORMATO_OCR_METRICAS
           and isinstance(om.get("paginas"), str),
           "ocr_metricas = {formato, paginas: string compacta}", str(om)[:120])
    m = ex.metricas_ocr_de_texto(om["paginas"]) if om else []
    checar(len(m) == 9, "uma entrada por pagina lida", str(len(m)))
    if len(m) != 9:
        return
    texto_por_pagina = dict(r["paginas_texto"])
    checar(all(m[n - 1][0] == len(texto_por_pagina.get(n, "")) for n in range(1, 10)),
           "c = caracteres do texto da pagina (o mesmo que foi indexado)",
           str([x[0] for x in m]))
    checar(m[0][1:] == (0, 1, 0), "pagina digital sem imagem: cob 0, desenho 1, 0 imagens",
           str(m[0]))
    checar(m[1][1] >= 990 and m[1][3] == 1 and m[2][1] >= 990,
           "pagina escaneada: cobertura ~1000 milesimos, 1 imagem", f"{m[1]} {m[2]}")
    checar(m[3][3] == 0 and m[3][2] == 1 and m[5] == (0, 0, 1, 0),
           "prancha e pagina so com desenho: desenho=1, sem imagem", f"{m[3]} {m[5]}")
    checar(m[6] == (0, 0, 0, 0), "pagina branca: (0, 0, 0, 0)", str(m[6]))
    checar(m[4][3] == 1 and m[4][1] is not None and 0 < m[4][1] < 50,
           "logotipo pequeno: cobertura medida e baixa", str(m[4]))
    checar(m[7][0] >= ex.PADRAO_OCR_METRICA_MAX_CARACTERES and m[7][1] is None,
           "texto acima do teto da metrica: cobertura NAO medida (custo)", str(m[7]))
    checar(80 <= m[8][0] < ex.PADRAO_OCR_METRICA_MAX_CARACTERES and m[8][1] is not None,
           "texto abaixo do teto: cobertura medida mesmo sem decidir OCR hoje", str(m[8]))
    checar(sum(x[3] for x in m) == r["imagens"], "soma das imagens = `imagens` do documento")

    d = ex.decidir_ocr_paginas(m, CFG)
    checar(r["paginas_ocr"] == [2, 3, 6] and d["paginas_ocr"] == r["paginas_ocr"],
           "a decisao recalculada SO das metricas e a mesma do extrator",
           f"{d} x {r['paginas_ocr']}")
    checar(d["brancas"] == 1 and d["indeterminadas"] == [], "1 branca, nada indeterminado",
           str(d))

    print("\n--- pdf/1.3: recalibrar sem baixar = reextrair com a nova calibracao ---")
    for rotulo, cfg_alt, esperado in (
            ("min_caracteres_por_pagina=10", {"worker": {"min_caracteres_por_pagina": 10}},
             [2, 6]),
            ("ocr_cobertura_imagem=0.001", {"worker": {"min_caracteres_por_pagina": 80,
                                                       "ocr_cobertura_imagem": 0.001}},
             [2, 3, 5, 6]),
            ("min=400, cobertura=0.9", {"worker": {"min_caracteres_por_pagina": 400,
                                                   "ocr_cobertura_imagem": 0.9}},
             [2, 3, 6])):
        recalc = ex.decidir_ocr_paginas(m, cfg_alt)
        reextraido = ex.extrair_pdf(p, cfg_alt)
        checar(recalc["paginas_ocr"] == reextraido["paginas_ocr"] == esperado
               and recalc["indeterminadas"] == [],
               f"{rotulo}: metricas antigas -> {esperado}, igual a reextrair",
               f"{recalc} x {reextraido['paginas_ocr']}")

    acima = ex.decidir_ocr_paginas(m, {"worker": {"min_caracteres_por_pagina": 5000}})
    checar(acima["indeterminadas"] == [8] and 8 not in acima["paginas_ocr"],
           "limiar acima do teto da metrica: a pagina sem cobertura medida e DECLARADA "
           "indeterminada, nao decidida no chute", str(acima))

    print("\n--- pdf/1.3: formato e tamanho ---")
    amostra = [(0, 1000, 1, 1), (2310, None, 1, 2), None, (15, 3, 1, 1), (0, 0, 0, 0)]
    texto = ex.metricas_ocr_para_texto(amostra)
    checar(texto == "0,1000,1,1;2310,,1,2;-;15,3,1,1;0,0,0,0", "codec compacto", texto)
    checar(ex.metricas_ocr_de_texto(texto) == amostra,
           "ida e volta exata (inclusive pagina que nao carregou e cobertura nao medida)")
    checar(ex.metricas_ocr_de_texto("") == [], "string vazia -> lista vazia")
    longo = pasta / "trezentas.pdf"
    pdf_com_texto(longo, 300)
    rl = ex.extrair_pdf(longo, CFG)
    no_perfil = len(json.dumps({"ocr_metricas": rl["ocr_metricas"]}, ensure_ascii=False,
                               indent=1).encode("utf-8"))
    por_pagina = no_perfil / 300
    checar(por_pagina <= 12, f"no perfil (indent=1): {por_pagina:.1f} bytes/pagina (<= 12)",
           str(no_perfil))
    lista = len(json.dumps({"x": [list(t) for t in ex.metricas_ocr_de_texto(
        rl["ocr_metricas"]["paginas"])]}, indent=1).encode("utf-8")) / 300
    checar(lista > 3 * por_pagina,
           f"(a mesma coisa como lista de listas com indent=1: {lista:.0f} bytes/pagina)")
    r_docx = ex._vazio("docx", "ok")
    checar(r_docx["ocr_metricas"] is None, "so o PDF preenche ocr_metricas")


if __name__ == "__main__":
    raise SystemExit(main())
