"""
test_worker_formatos — E9 / L7: o worker nao baixa o que nenhum extrator le.

    python tests\\test_worker_formatos.py

Banco criado pela receita do repositorio (`00_init_db.py`), worker de
producao, o mesmo Graph falso do `test_worker_nuvem` (so sabe baixar e
registra cada pedido). Todo item legado e ROTEIRIZADO: se o worker pedisse,
o download daria certo. A prova e que ele nem pede.

Prova, objetivamente:
  .xls .xlsb .doc .rtf .odt .ods .ppt .odp -> UNSUPPORTED sem download, sem
  gastar tentativa, com extracao/perfil/conteudo/evento como qualquer item
  (REGRA 6 e 8) e motivo SEM_LEITOR · categoria sem extrator idem · legado
  acima do teto vira UNSUPPORTED (nao PARTIAL adiado) · o que tem leitor
  continua sendo baixado e extraido (.xlsx, .xlsm, .docm) · metadados do
  extrator chegam ao perfil JSON · o derivado de PDF guarda a fronteira de
  pagina (\\f) e o indice v4 continua numerando as paginas.
"""

from __future__ import annotations

import gzip
import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import area_temporaria, checar, escrever_config, placar_final, rodar  # noqa: E402

import cerebro_util as cu  # noqa: E402
import extratores  # noqa: E402
import quickxor  # noqa: E402
from test_worker_nuvem import (GraphFalso, contar, inserir, linha,  # noqa: E402
                               montar_worker, processar_id, sobras)

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# (nome, categoria) — o que a fila real tem (.xls 526, .xlsb 100, .doc 41,
# .rtf 1) e o que o yaml ainda manda para categoria com extrator.
LEGADOS = [("medicao_antiga.xls", "xlsx"), ("orcamento.xlsb", "xlsx"),
           ("oficio.doc", "docx"), ("memorial.rtf", "docx"), ("ata.odt", "docx"),
           ("quantitativos.ods", "xlsx"), ("apresentacao.ppt", "pptx"),
           ("reuniao.odp", "pptx")]


def bytes_xlsx(texto: str, macro: bool = False) -> bytes:
    import openpyxl
    livro = openpyxl.Workbook()
    livro.active["A1"] = texto
    saida = io.BytesIO()
    livro.save(saida)
    if not macro:
        return saida.getvalue()
    final = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(saida.getvalue())) as z, \
            zipfile.ZipFile(final, "w", zipfile.ZIP_DEFLATED) as w:
        for info in z.infolist():
            dados = z.read(info)
            if info.filename == "[Content_Types].xml":
                dados = dados.replace(
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                    b"application/vnd.ms-excel.sheet.macroEnabled.main+xml")
            w.writestr(info, dados)
        w.writestr("xl/vbaProject.bin", OLE + b" Sub Auto_Open() End Sub")
    return final.getvalue()


def bytes_docm(texto: str) -> bytes:
    import docx
    d = docx.Document()
    d.add_paragraph(texto)
    d.core_properties.title = "Planilha de controle"
    d.core_properties.author = "Engenharia"
    saida = io.BytesIO()
    d.save(saida)
    final = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(saida.getvalue())) as z, \
            zipfile.ZipFile(final, "w", zipfile.ZIP_DEFLATED) as w:
        for info in z.infolist():
            dados = z.read(info)
            if info.filename == "[Content_Types].xml":
                dados = dados.replace(
                    b"application/vnd.openxmlformats-officedocument.wordprocessingml."
                    b"document.main+xml",
                    b"application/vnd.ms-word.document.macroEnabled.main+xml")
            w.writestr(info, dados)
    return final.getvalue()


def bytes_pdf(paginas: list[str]) -> bytes:
    import fitz
    doc = fitz.open()
    for texto in paginas:
        pagina = doc.new_page()
        for i in range(6):
            pagina.insert_text((50, 60 + 16 * i), f"{texto} linha {i}: medicao mensal "
                                                  f"da obra RD-103, estaca 120.", fontsize=9)
    dados = doc.tobytes()
    doc.close()
    return dados


def eventos(con, arquivo_id, evento="processado"):
    return [json.loads(e[0] or "{}") for e in con.execute(
        "SELECT detalhe FROM evento_arquivo WHERE arquivo_id=? AND evento=? ORDER BY id",
        (arquivo_id, evento))]


def main() -> int:
    area = area_temporaria("cerebro_formatos_")
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir()
    (area / "tmp").mkdir()
    config = escrever_config(area, raiz_falsa, fonte="graph")
    r = rodar("00_init_db.py", config)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        return 1
    cfg = cu.carregar_config(config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    graph = GraphFalso()
    w = montar_worker(cfg, con, area, graph)

    # ==================================================================
    print("\n--- 1. formato legado: UNSUPPORTED sem download ---")
    # ==================================================================
    ids = {}
    for nome, categoria in LEGADOS:
        corpo = OLE + nome.encode() * 200
        arquivo_id, item_id = inserir(con, nome, len(corpo), quickxor.de_bytes(corpo),
                                      categoria=categoria)
        graph.roteirizar(item_id, corpo)              # baixaria, se pedissem
        ids[nome] = (arquivo_id, item_id, categoria)

    baixados_antes = w.contadores["bytes_baixados"]
    for nome, (arquivo_id, item_id, categoria) in ids.items():
        desfecho, _ = processar_id(w, arquivo_id)
        a = linha(con, arquivo_id)
        ext = Path(nome).suffix
        checar(item_id not in graph.baixados, f"{ext}: NENHUM pedido de download",
               str(graph.baixados))
        checar(desfecho == "ok" and a["estado"] == "UNSUPPORTED",
               f"{ext}: termina UNSUPPORTED (estado que ja existe)", f"{desfecho}/{a['estado']}")
        checar(a["tentativas"] == 0 and a["proxima_tentativa_em"] is None,
               f"{ext}: nao gasta tentativa nem agenda retentativa")
        checar((a["ultimo_erro"] or "").startswith("SEM_LEITOR: ")
               and "nada foi baixado" in a["ultimo_erro"],
               f"{ext}: ultimo_erro explica (SEM_LEITOR)", str(a["ultimo_erro"]))
        ext_rows = con.execute("SELECT * FROM extracao WHERE arquivo_id=?",
                               (arquivo_id,)).fetchall()
        checar(len(ext_rows) == 1 and ext_rows[0]["status"] == "nao_suportado"
               and ext_rows[0]["extrator"] == categoria
               and ext_rows[0]["versao"] == extratores.VERSOES[categoria],
               f"{ext}: extracao igual a que o extrator gravaria (status/extrator/versao)",
               str([dict(x) for x in ext_rows])[:200])
        ev = eventos(con, arquivo_id)
        checar(len(ev) == 1 and ev[0].get("baixado") is False
               and ev[0].get("adiado") == "sem_leitor" and ev[0].get("estado") == "UNSUPPORTED",
               f"{ext}: evento 'processado' com baixado=false e adiado=sem_leitor", str(ev))
        perfil = ext_rows[0]["perfil_path"] if ext_rows else None
        checar(perfil and Path(perfil).exists(), f"{ext}: perfil JSON gravado")
        c = con.execute("SELECT estado FROM conteudo WHERE id=?",
                        (a["conteudo_id"],)).fetchone() if a["conteudo_id"] else None
        checar(c is not None and c["estado"] == "NAO_SUPORTADO",
               f"{ext}: conteudo registrado como NAO_SUPORTADO pelo hash do inventario")

    checar(w.contadores["bytes_baixados"] == baixados_antes, "nenhum byte baixado")
    checar(w.contadores.get("adiados_sem_leitor") == len(LEGADOS),
           f"contador da sessao: {len(LEGADOS)} sem leitor",
           str(w.contadores.get("adiados_sem_leitor")))
    checar(w.contadores["adiados_tamanho"] == 0, "nao foram contados como teto de tamanho")
    checar(sobras(area) == [], "nada em tmp/", str(sobras(area)))
    fila = contar(con, "v_fila_nuvem", "id IN (%s)" % ",".join(
        str(v[0]) for v in ids.values()))
    checar(fila == 0, "nenhum deles volta para a fila", str(fila))

    # ==================================================================
    print("\n--- 2. categoria sem extrator, marcada suportado=1 ---")
    # ==================================================================
    corpo = b"PK\x03\x04 um zip qualquer" * 50
    id_zip, item_zip = inserir(con, "Entrega 51.zip", len(corpo), quickxor.de_bytes(corpo),
                               categoria="compactado")
    graph.roteirizar(item_zip, corpo)
    processar_id(w, id_zip)
    z = linha(con, id_zip)
    checar(item_zip not in graph.baixados and z["estado"] == "UNSUPPORTED",
           "sem extrator para a categoria -> UNSUPPORTED sem download", z["estado"])

    # ==================================================================
    print("\n--- 3. legado ACIMA do teto: UNSUPPORTED, nao PARTIAL adiado ---")
    # ==================================================================
    w.w["tamanho_max_extracao_mb"] = 0.001
    try:
        corpo = OLE + b"x" * 4096
        id_g, item_g = inserir(con, "planilha_gigante.xls", len(corpo),
                               quickxor.de_bytes(corpo), categoria="xlsx")
        graph.roteirizar(item_g, corpo)
        processar_id(w, id_g)
        g = linha(con, id_g)
        versao = con.execute("SELECT versao FROM extracao WHERE arquivo_id=?",
                             (id_g,)).fetchone()
        checar(g["estado"] == "UNSUPPORTED" and item_g not in graph.baixados
               and versao[0] != "adiado/teto_tamanho",
               "formato sem leitor vence o teto: nao ha o que adiar",
               f"{g['estado']} {versao[0] if versao else None}")
    finally:
        w.w.pop("tamanho_max_extracao_mb", None)

    # ==================================================================
    print("\n--- 4. o que tem leitor continua sendo baixado e extraido ---")
    # ==================================================================
    casos = [("medicao_41.xlsx", "xlsx", bytes_xlsx("terraplenagem lote 3")),
             ("controle.xlsm", "xlsx", bytes_xlsx("terraplenagem lote 4", macro=True)),
             ("planilha_controle.docm", "docx", bytes_docm("terraplenagem lote 5"))]
    for nome, categoria, corpo in casos:
        arquivo_id, item_id = inserir(con, nome, len(corpo), quickxor.de_bytes(corpo),
                                      categoria=categoria)
        graph.roteirizar(item_id, corpo)
        desfecho, _ = processar_id(w, arquivo_id)
        a = linha(con, arquivo_id)
        checar(item_id in graph.baixados, f"{Path(nome).suffix}: foi baixado")
        checar(a["estado"] == "COMPLETED", f"{Path(nome).suffix}: COMPLETED",
               f"{a['estado']} {a['ultimo_erro']}")
        if nome.endswith(".docm"):
            perfil = json.loads(Path(con.execute(
                "SELECT perfil_path FROM extracao WHERE arquivo_id=?",
                (arquivo_id,)).fetchone()[0]).read_text(encoding="utf-8"))
            checar((perfil.get("metadados") or {}).get("titulo") == "Planilha de controle",
                   "metadados do extrator chegam ao perfil JSON", str(perfil.get("metadados")))
        if nome.endswith(".xlsm"):
            perfil = json.loads(Path(con.execute(
                "SELECT perfil_path FROM extracao WHERE arquivo_id=?",
                (arquivo_id,)).fetchone()[0]).read_text(encoding="utf-8"))
            checar((perfil.get("metadados") or {}).get("macros") is True,
                   "XLSM: o perfil registra que ha macro (nao executada)")
    checar(sobras(area) == [], "tmp/ limpo", str(sobras(area)))

    # ==================================================================
    print("\n--- 5. PDF: derivado com fronteira de pagina, indice v4 por pagina ---")
    # ==================================================================
    corpo = bytes_pdf(["primeira pagina", "segunda pagina"])
    id_pdf, item_pdf = inserir(con, "notificacao.pdf", len(corpo), quickxor.de_bytes(corpo),
                               categoria="pdf")
    graph.roteirizar(item_pdf, corpo)
    processar_id(w, id_pdf)
    p = linha(con, id_pdf)
    checar(p["estado"] == "COMPLETED", "PDF COMPLETED", f"{p['estado']} {p['ultimo_erro']}")
    caminho = con.execute("SELECT texto_path FROM extracao WHERE arquivo_id=?",
                          (id_pdf,)).fetchone()[0]
    with gzip.open(caminho, "rt", encoding="utf-8") as fh:
        derivado = fh.read()
    unidades = derivado.split("\f")
    checar(len(unidades) == 3 and unidades[0].startswith("primeira pagina")
           and unidades[1].startswith("segunda pagina") and unidades[2] == "",
           "o .txt.gz guarda as 2 paginas, cada uma terminada por \\f",
           repr([u[:20] for u in unidades]))
    paginas = sorted({r[0] for r in con.execute(
        "SELECT pagina FROM texto_fts WHERE arquivo_id=?", (id_pdf,))})
    checar(paginas == [1, 2], "indice v4 numera as paginas", str(paginas))

    con.close()
    return placar_final()


if __name__ == "__main__":
    sys.exit(main())
