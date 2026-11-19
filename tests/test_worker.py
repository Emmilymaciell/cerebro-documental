"""
test_worker — o processador autonomo, ponta a ponta, em documentos reais
gerados na hora. Nao toca no SharePoint.

    python tests\\test_worker.py

Cobre: extracao por formato, derivados, indice de busca, PDF escaneado
(OCR_REQUIRED), duplicata exata, arquivo corrompido (nao derruba o worker),
formato sem extrator, retomada apos queda, pausa segura, checkpoint,
incremental (nao reprocessa) e a garantia de acervo intocado.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import (RAIZ, area_temporaria, checar, escrever_config, foto_exata,  # noqa: E402
                   placar_final, rodar)

import cerebro_util as cu  # noqa: E402

PALAVRA = "terraplenagem"


def montar_arvore(raiz: Path):
    import fitz
    import docx
    import openpyxl
    from pptx import Presentation
    from pptx.util import Inches

    (raiz / "RD103" / "3. Notificações").mkdir(parents=True)
    (raiz / "RD103" / "Medições").mkdir(parents=True)
    (raiz / "Outros").mkdir(parents=True)

    # PDF com camada de texto (2 paginas)
    doc = fitz.open()
    for numero in (1, 2):
        pagina = doc.new_page()
        pagina.insert_text((72, 100), f"Notificacao 00{numero} - servico de {PALAVRA} "
                                      f"na estaca 120 da RD-103.", fontsize=12)
        pagina.insert_text((72, 130), "Prazo de resposta: 5 dias uteis.", fontsize=12)
    caminho_pdf = raiz / "RD103" / "3. Notificações" / "notificacao 001.pdf"
    doc.save(str(caminho_pdf))
    doc.close()

    # Copia identica em outra pasta -> duplicata exata
    shutil.copyfile(caminho_pdf, raiz / "Outros" / "copia da notificacao.pdf")

    # PDF sem camada textual (simula escaneado)
    doc = fitz.open()
    pagina = doc.new_page()
    pagina.draw_rect(fitz.Rect(50, 50, 500, 700), color=(0, 0, 0), width=2)
    doc.save(str(raiz / "RD103" / "3. Notificações" / "escaneado.pdf"))
    doc.close()

    # PDF corrompido
    (raiz / "RD103" / "3. Notificações" / "corrompido.pdf").write_bytes(b"%PDF-1.4 lixo lixo")

    # DOCX
    documento = docx.Document()
    documento.add_paragraph("Relatorio de medicao 12 - RD-103")
    tabela = documento.add_table(rows=1, cols=2)
    tabela.rows[0].cells[0].text = "Servico"
    tabela.rows[0].cells[1].text = PALAVRA
    documento.save(str(raiz / "RD103" / "Medições" / "medicao 12.docx"))

    # XLSX
    livro = openpyxl.Workbook()
    aba = livro.active
    aba.title = "Resumo"
    aba["A1"] = "item"
    aba["B1"] = PALAVRA
    aba["A2"] = "quantidade"
    aba["B2"] = 1500
    livro.save(str(raiz / "RD103" / "Medições" / "planilha de medicao.xlsx"))

    # PPTX
    apresentacao = Presentation()
    slide = apresentacao.slides.add_slide(apresentacao.slide_layouts[5])
    slide.shapes.title.text = f"Reuniao de obra - {PALAVRA}"
    caixa = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(6), Inches(1))
    caixa.text_frame.text = "Decisao: refazer o trecho."
    apresentacao.save(str(raiz / "Outros" / "reuniao.pptx"))

    # CSV e TXT
    (raiz / "Outros" / "dados.csv").write_text(
        f"item;servico\n1;{PALAVRA}\n2;drenagem\n", encoding="utf-8")
    (raiz / "Outros" / "anotacao.txt").write_text(
        f"conferir {PALAVRA} do lote 3", encoding="utf-8")

    # Formato sem extrator
    (raiz / "Outros" / "planta.dwg").write_bytes(b"DWG binario")


def main() -> int:
    area = area_temporaria("cerebro_worker_")
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir()
    montar_arvore(raiz_falsa)
    # liberar_apos_processar fica ligado de proposito: a arvore falsa nao e
    # OneDrive, entao nada foi hidratado por nos e nada pode ser desidratado.
    config = escrever_config(area, raiz_falsa)
    antes = foto_exata(raiz_falsa)

    print("\n--- preparo ---")
    rodar("00_init_db.py", config)
    rodar("01_scan_inventario.py", config, "--quieto")
    rodar("02_classificar.py", config)
    con = cu.conectar(area / "teste.db")
    total = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    checar(total == 10, f"10 arquivos inventariados (achou {total})")
    fila = con.execute("SELECT COUNT(*) FROM v_fila").fetchone()[0]
    checar(fila == 9, f"9 na fila (dwg fica UNSUPPORTED) — fila={fila}")

    print("\n--- pausa pendente bloqueia o worker ---")
    r = rodar("control.py", config, "pause")
    checar(r.returncode == 0 and "SEGURO" in r.stdout, "control.py pause responde", r.stdout[-200:])
    r = rodar("worker.py", config, "run", "--uma-vez", "--quieto")
    checar(r.returncode == 4, "worker se recusa a rodar com pausa pendente", r.stdout[-200:])
    checar(con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0] == 0,
           "nada foi processado durante a pausa")
    r = rodar("control.py", config, "resume")
    checar(r.returncode == 0 and cu.ler_controle(con)["comando"] == "RUN",
           "control.py resume libera a fila")

    print("\n--- processamento ---")
    r = rodar("worker.py", config, "run", "--uma-vez", "--quieto")
    checar(r.returncode == 0, "worker roda ate esvaziar a fila", r.stderr[-800:])
    estados = dict(con.execute("SELECT estado, COUNT(*) FROM arquivo GROUP BY estado"))
    checar(estados.get("COMPLETED") == 6,
           "6 documentos COMPLETED (pdf, docx, xlsx, pptx, csv, txt)", str(estados))
    checar(estados.get("OCR_REQUIRED") == 1, "PDF sem texto vira OCR_REQUIRED", str(estados))
    checar(estados.get("DUPLICATE") == 1, "copia identica vira DUPLICATE", str(estados))
    checar(estados.get("UNSUPPORTED") == 1, "dwg segue catalogado como UNSUPPORTED",
           str(estados))
    checar(estados.get("ERROR_RETRYABLE") == 1,
           "PDF corrompido vira ERROR_RETRYABLE sem derrubar o worker", str(estados))

    print("\n--- derivados e indice ---")
    linha = con.execute(
        "SELECT a.id, e.texto_path, e.perfil_path, e.paginas, e.caracteres, e.trechos "
        "FROM arquivo a JOIN extracao e ON e.arquivo_id=a.id "
        "WHERE a.nome='notificacao 001.pdf'").fetchone()
    checar(linha is not None and Path(linha["texto_path"]).exists(),
           "texto derivado gravado (.txt.gz)")
    texto = cu.ler_texto(linha["texto_path"]) if linha else ""
    checar(PALAVRA in texto, "texto extraido contem o conteudo do PDF")
    # Sem a extracao do PDF original o teste nao pode morrer com TypeError
    # antes do placar: falha e segue.
    checar(linha is not None and linha["paginas"] == 2,
           f"contou as 2 paginas do PDF (paginas={linha['paginas'] if linha else None})")
    checar(linha is not None and Path(linha["perfil_path"]).exists(), "perfil JSON gravado")
    checar(linha is not None and linha["trechos"] >= 1, "trechos indexados")

    # Era `len(achados) >= 4`, com 6 arquivos contendo o termo: perder dois
    # formatos inteiros passava (achado da varredura de vacuidade do sprint).
    achados = {a[0] for a in con.execute(
        "SELECT DISTINCT caminho_rel FROM texto_fts WHERE texto MATCH ?", (PALAVRA,))}
    esperados = {"RD103\\3. Notificações\\notificacao 001.pdf",
                 "RD103\\Medições\\medicao 12.docx",
                 "RD103\\Medições\\planilha de medicao.xlsx",
                 "Outros\\reuniao.pptx", "Outros\\dados.csv", "Outros\\anotacao.txt"}
    checar(achados == esperados,
           f"busca textual encontra o termo nos 6 formatos ({len(achados)})",
           str(sorted(achados ^ esperados)))
    achados_acento = con.execute(
        "SELECT COUNT(*) FROM texto_fts WHERE texto MATCH ?", ("medicao",)).fetchone()[0]
    checar(achados_acento >= 1, "busca ignora acento (medicao ~ medição)")

    xlsx = con.execute("SELECT abas FROM extracao e JOIN arquivo a ON a.id=e.arquivo_id "
                       "WHERE a.ext='.xlsx'").fetchone()
    checar(xlsx and xlsx["abas"] == 1, "contou as abas da planilha")
    pptx = con.execute("SELECT slides FROM extracao e JOIN arquivo a ON a.id=e.arquivo_id "
                       "WHERE a.ext='.pptx'").fetchone()
    checar(pptx and pptx["slides"] == 1, "contou os slides da apresentacao")

    print("\n--- incremental ---")
    r = rodar("worker.py", config, "run", "--uma-vez", "--quieto")
    processados = con.execute(
        "SELECT arquivos_vistos FROM execucao WHERE etapa='worker' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0]
    checar(processados == 0, f"segunda rodada nao reprocessa nada (processou {processados})")

    print("\n--- retomada apos queda ---")
    alvo = con.execute("SELECT id FROM arquivo WHERE nome='medicao 12.docx'").fetchone()[0]
    con.execute("UPDATE arquivo SET estado='EXTRACTING' WHERE id=?", (alvo,))
    con.commit()
    r = rodar("worker.py", config, "run", "--uma-vez", "--quieto")
    checar(con.execute("SELECT estado FROM arquivo WHERE id=?", (alvo,)).fetchone()[0]
           == "COMPLETED", "item preso em EXTRACTING volta para a fila e conclui")
    checar(con.execute("SELECT COUNT(*) FROM evento_arquivo WHERE evento='retomado_apos_queda'"
                       ).fetchone()[0] == 1, "retomada registrada no historico")

    print("\n--- checkpoint e controle ---")
    checar(con.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0] >= 2,
           "checkpoints gravados")
    ultimo = con.execute("SELECT * FROM checkpoint ORDER BY id DESC LIMIT 1").fetchone()
    checar(ultimo["estado_worker"] == "FILA_VAZIA",
           "ultimo checkpoint registra o motivo do encerramento", str(ultimo["estado_worker"]))
    controle = cu.ler_controle(con)
    checar(controle["estado"] == "FILA_VAZIA" and not cu.processo_vivo(controle["pid"]),
           "controle mostra worker encerrado sem processo vivo")
    r = rodar("control.py", config, "status")
    checar(r.returncode == 0 and "FILA_VAZIA" in r.stdout, "control.py status funciona")
    r = rodar("99_status.py", config)
    checar(r.returncode == 0 and "ESTADOS" in r.stdout, "99_status funciona")

    print("\n--- reprocessa so o que mudou ---")
    import time as _t
    _t.sleep(1.1)
    (raiz_falsa / "Outros" / "anotacao.txt").write_text(
        "conferir drenagem do lote 4", encoding="utf-8")
    # O unico arquivo que o TESTE alterou, fotografado logo apos a escrita e
    # antes de qualquer script rodar de novo.
    anotacao_escrita = foto_exata(raiz_falsa)["Outros\\anotacao.txt"]
    rodar("01_scan_inventario.py", config, "--quieto")
    checar(con.execute("SELECT estado FROM arquivo WHERE nome='anotacao.txt'").fetchone()[0]
           == "CHANGED", "arquivo alterado volta para a fila")
    rodar("worker.py", config, "run", "--uma-vez", "--quieto")
    processados = con.execute(
        "SELECT arquivos_vistos FROM execucao WHERE etapa='worker' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0]
    checar(processados == 1, f"reprocessou so o arquivo alterado (processou {processados})")
    novo_texto = cu.ler_texto(con.execute(
        "SELECT e.texto_path FROM extracao e JOIN arquivo a ON a.id=e.arquivo_id "
        "WHERE a.nome='anotacao.txt'").fetchone()[0])
    checar("drenagem" in novo_texto and PALAVRA not in novo_texto,
           "derivado atualizado com o conteudo novo")

    print("\n--- prioridade manda na ordem ---")
    # Antes este bloco lia `v_fila` quando ela estava VAZIA (o unico item na
    # fila, o corrompido, espera o backoff) e avaliava `[] == sorted([])`
    # (M-3). E o worker nem consome `v_fila`: tem consulta propria. Agora tres
    # documentos voltam para a fila com prioridades na ordem INVERSA do
    # tamanho, e o teste le em que ordem o WORKER os processou.
    ordem_esperada = ["medicao 12.docx", "reuniao.pptx", "dados.csv"]
    for prioridade, nome in enumerate(ordem_esperada, start=1):
        con.execute("UPDATE arquivo SET estado='CHANGED', prioridade=? WHERE nome=?",
                    (prioridade, nome))
    con.commit()
    tamanhos = dict(con.execute(
        "SELECT nome, tamanho_bytes FROM arquivo WHERE nome IN (?,?,?)", ordem_esperada))
    checar(tamanhos["dados.csv"] < min(tamanhos["medicao 12.docx"], tamanhos["reuniao.pptx"]),
           "fixture: o de MENOR prioridade e o menor arquivo (tamanho nao decide)",
           str(tamanhos))
    rodar("worker.py", config, "run", "--uma-vez", "--quieto")
    execucao = con.execute("SELECT MAX(id) FROM execucao WHERE etapa='worker'").fetchone()[0]
    ordem = [l[0] for l in con.execute(
        "SELECT a.nome FROM evento_arquivo e JOIN arquivo a ON a.id=e.arquivo_id "
        "WHERE e.execucao_id=? AND e.evento='processado' ORDER BY e.id", (execucao,))]
    checar(ordem == ordem_esperada, "o worker processa na ordem P1, P2, P3", str(ordem))

    print("\n--- o acervo nao foi tocado ---")
    # Antes o valor esperado de anotacao.txt era copiado do proprio `depois`
    # (M-10): para essa chave a prova virava depois[k] == depois[k]. Agora o
    # esperado e o que o teste mediu logo apos escrever; mtime em ns + sha256.
    depois = foto_exata(raiz_falsa)
    esperado = dict(antes)
    esperado["Outros\\anotacao.txt"] = anotacao_escrita
    divergentes = sorted(k for k in set(depois) | set(esperado)
                         if depois.get(k) != esperado.get(k))
    checar(depois == esperado, "worker nao criou, apagou nem alterou nada na arvore",
           str(divergentes))

    con.close()
    shutil.rmtree(area, ignore_errors=True)
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
