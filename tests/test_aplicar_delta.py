"""
test_aplicar_delta — E8: o que o delta descobriu chega ao catalogo.

    python tests\\test_aplicar_delta.py

`12_scan_graph.py` mexe no espelho (`item_nuvem`). Este teste cobre o passo
seguinte: `17_aplicar_delta.py` levando isso para `arquivo` pela identidade
`(drive_id, item_id)`, sem perder nada.

O que ele tem de provar:
  sumiu    -> `MISSING`/`presente=0`, com o estado anterior no evento, e
              extracao, trechos, conteudo e classificacao INTACTOS;
  voltou   -> o estado anterior e restaurado, nao inventado;
  renomeou -> muda o caminho e NADA mais: nao volta para a fila;
  alterou  -> volta para a fila como `CHANGED`, com o hash novo;
  novo     -> e contado, mas NAO inserido (isso e do 01/02);
  simular  -> nao grava uma linha sequer.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import RAIZ, area_temporaria, checar, escrever_config, placar_final, rodar  # noqa: E402

import cerebro_util as cu  # noqa: E402

DRIVE = "drive!Programa"


def carregar():
    caminho = RAIZ / "scripts" / "17_aplicar_delta.py"
    spec = importlib.util.spec_from_file_location("aplicar_delta", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def por_arquivo(con, nome: str):
    return con.execute("SELECT * FROM arquivo WHERE nome=?", (nome,)).fetchone()


def eventos(con, arquivo_id: int) -> list[str]:
    return [l[0] for l in con.execute(
        "SELECT evento FROM evento_arquivo WHERE arquivo_id=? ORDER BY id", (arquivo_id,))]


def inserir_par(con, nome, item_id, *, estado="QUEUED", tamanho=100, hash_="H1",
                presente=1, presente_nuvem=1, no_espelho=True, espelho_presente=1,
                espelho_caminho=None, espelho_hash=None, espelho_tamanho=None,
                espelho_nome=None):
    """Cria a ocorrencia em `arquivo` e (opcionalmente) a linha no espelho."""
    caminho = f"Programa - 3. Desenvolvimento\\{nome}"
    # O catalogo guarda o caminho ANTIGO; o espelho, o que o delta trouxe.
    # Sao diferentes de proposito quando o item foi movido.
    nuvem = f"3. Desenvolvimento/{nome}"
    nuvem_espelho = espelho_caminho if espelho_caminho is not None else nuvem
    cur = con.execute(
        "INSERT INTO arquivo (caminho_rel, biblioteca, pasta_rel, nome, ext, categoria,"
        " nome_normalizado, tamanho_bytes, prioridade, suportado, estado, presente,"
        " presente_nuvem, drive_id, item_id, quick_xor, caminho_nuvem, web_url,"
        " primeira_vez_em, ultima_vez_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (caminho, "Programa - 3. Desenvolvimento", "3. Desenvolvimento", nome,
         Path(nome).suffix, "pdf", cu.normalizar_nome(nome), tamanho, 0, 1, estado,
         presente, presente_nuvem, DRIVE, item_id, hash_, nuvem,
         f"https://x/{item_id}", cu.agora_iso(), cu.agora_iso()))
    if no_espelho:
        con.execute(
            "INSERT INTO item_nuvem (drive_id, item_id, parent_id, nome, eh_pasta, ext,"
            " tamanho_bytes, quick_xor, web_url, presente, removido_em, caminho_rel,"
            " primeira_vez_em, ultima_vez_em) VALUES (?,?,?,?,0,?,?,?,?,?,?,?,?,?)",
            (DRIVE, item_id, "root", espelho_nome or nome, Path(nome).suffix,
             espelho_tamanho if espelho_tamanho is not None else tamanho,
             espelho_hash if espelho_hash is not None else hash_,
             f"https://x/{item_id}", espelho_presente,
             None if espelho_presente else cu.agora_iso(), nuvem_espelho,
             cu.agora_iso(), cu.agora_iso()))
    con.commit()
    return int(cur.lastrowid)


def main() -> int:
    modulo = carregar()
    area = area_temporaria("cerebro_e8_")
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir(parents=True)
    config = escrever_config(area, raiz_falsa)
    rodar("00_init_db.py", config)
    rodar("03_migrar_v3.py", config, "--sem-backup")
    cfg = cu.carregar_config(config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])

    # --- o cenario ---------------------------------------------------
    # sumiu, e ja tinha trabalho feito: e o caso que mais importa preservar
    id_sumiu = inserir_par(con, "sumiu.pdf", "it!sumiu", estado="COMPLETED",
                           espelho_presente=0)
    con.execute("INSERT INTO extracao (arquivo_id, extrator, versao, status, texto_path, "
                "bytes_texto, trechos, executado_em) VALUES (?,?,?,?,?,?,?,?)",
                (id_sumiu, "pdf", "1", "ok", "derivados/texto/x.gz", 10, 2, cu.agora_iso()))
    con.execute("INSERT INTO texto_fts (arquivo_id, caminho_rel, pagina, trecho_idx, texto) "
                "VALUES (?,?,0,0,'medicao da obra')", (id_sumiu, "Programa - 3. Desenvolvimento\\sumiu.pdf"))
    con.execute("INSERT INTO conteudo (quick_xor, tamanho_bytes, representante_id, estado, "
                "ocorrencias, primeira_vez_em) VALUES ('H1',100,?, 'EXTRAIDO',1,?)",
                (id_sumiu, cu.agora_iso()))
    con.execute("UPDATE arquivo SET conteudo_id=(SELECT id FROM conteudo WHERE quick_xor='H1') "
                "WHERE id=?", (id_sumiu,))
    con.commit()

    id_voltou = inserir_par(con, "voltou.pdf", "it!voltou", estado="MISSING", presente=0,
                            presente_nuvem=0, espelho_presente=1)
    con.execute("INSERT INTO evento_arquivo (arquivo_id, evento, detalhe, em) "
                "VALUES (?, 'ausente_na_nuvem', '{\"estado_anterior\": \"OCR_REQUIRED\"}', ?)",
                (id_voltou, cu.agora_iso()))
    id_voltou_sem_hist = inserir_par(con, "voltou2.pdf", "it!voltou2", estado="MISSING",
                                     presente=0, presente_nuvem=0, espelho_presente=1)
    id_mudou_lugar = inserir_par(con, "andou.pdf", "it!andou", estado="COMPLETED",
                                 espelho_caminho="3. Desenvolvimento/OUTRA PASTA/andou.pdf")
    id_renomeou = inserir_par(con, "antigo.pdf", "it!renom", estado="COMPLETED",
                              espelho_nome="novo.pdf")
    id_alterou = inserir_par(con, "alterou.pdf", "it!alt", estado="COMPLETED",
                             espelho_hash="H2", espelho_tamanho=999)
    id_intocavel = inserir_par(con, "semextrator.dwg", "it!skip", estado="UNSUPPORTED",
                               espelho_hash="H9")
    id_parado = inserir_par(con, "parado.pdf", "it!parado", estado="COMPLETED")
    # hash calculado pelo worker (R4): o catalogo tem, o espelho nao tem.
    id_hash_local = inserir_par(con, "hash_local.pdf", "it!hloc", estado="COMPLETED",
                                hash_="HCALC")
    con.execute("UPDATE item_nuvem SET quick_xor=NULL WHERE item_id='it!hloc'")
    con.commit()
    # novo: so no espelho, sem par em `arquivo`
    con.execute("INSERT INTO item_nuvem (drive_id, item_id, parent_id, nome, eh_pasta, "
                "tamanho_bytes, presente, caminho_rel) "
                "VALUES (?,?,?,?,0,50,1,?)", (DRIVE, "it!novo", "root", "novo.pdf",
                                              "3. Desenvolvimento/novo.pdf"))
    con.commit()

    antes_extracoes = con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0]
    antes_trechos = con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0]
    antes_conteudos = con.execute("SELECT COUNT(*) FROM conteudo").fetchone()[0]
    antes_linhas = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]

    print("\n--- 1. simulacao nao grava nada ---")
    p = modulo.Propagacao(con, cu.abrir_execucao(con, "teste", "", {}), aplicar=False)
    checar(len(p.sumiram()) == 1, "viu 1 que sumiu", str(len(p.sumiram())))
    checar(len(p.voltaram()) == 2, "viu 2 que voltaram", str(len(p.voltaram())))
    checar(len(p.recaminhados()) == 2, "viu 2 recaminhados (movido + renomeado)",
           str(len(p.recaminhados())))
    checar(len(p.alterados()) == 1, "viu 1 com conteudo alterado (o UNSUPPORTED nao conta)",
           str(len(p.alterados())))
    checar(p.novos_no_espelho() == 1, "viu 1 novo so no espelho")
    checar(por_arquivo(con, "sumiu.pdf")["estado"] == "COMPLETED",
           "a simulacao nao mexeu no banco")

    print("\n--- 2. aplicando ---")
    p = modulo.Propagacao(con, cu.abrir_execucao(con, "teste", "", {}), aplicar=True)
    p.marcar_sumidos(p.sumiram())
    p.restaurar(p.voltaram())
    p.recaminhar(p.recaminhados())
    p.requeue(p.alterados())
    con.commit()

    print("\n--- 3. sumiu: MISSING sem perder trabalho ---")
    a = por_arquivo(con, "sumiu.pdf")
    checar(a["estado"] == "MISSING", "virou MISSING", a["estado"])
    checar(a["presente"] == 0 and a["presente_nuvem"] == 0, "presente=0 e presente_nuvem=0")
    checar("ausente_na_nuvem" in eventos(con, id_sumiu), "gravou o evento")
    detalhe = con.execute("SELECT detalhe FROM evento_arquivo WHERE arquivo_id=? "
                          "AND evento='ausente_na_nuvem'", (id_sumiu,)).fetchone()[0]
    checar("COMPLETED" in detalhe and "delta" in detalhe,
           "o evento guarda o estado anterior e quem detectou", detalhe[:90])
    checar(con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0] == antes_extracoes,
           "extracao preservada")
    checar(con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0] == antes_trechos,
           "trechos preservados")
    checar(con.execute("SELECT COUNT(*) FROM conteudo").fetchone()[0] == antes_conteudos,
           "conteudo preservado")
    checar(a["conteudo_id"] is not None and a["prioridade"] == 0 and a["categoria"] == "pdf",
           "classificacao e vinculo de conteudo intactos")
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == antes_linhas,
           "nenhuma linha apagada (REGRA 6)")

    print("\n--- 4. voltou: restaura o estado que tinha ---")
    v = por_arquivo(con, "voltou.pdf")
    checar(v["estado"] == "OCR_REQUIRED", "restaurou o estado do evento, nao inventou",
           v["estado"])
    checar(v["presente"] == 1 and v["presente_nuvem"] == 1, "voltou a ser presente")
    checar("reapareceu_na_nuvem" in eventos(con, id_voltou), "gravou o reaparecimento")
    v2 = por_arquivo(con, "voltou2.pdf")
    checar(v2["estado"] == "QUEUED", "sem historico, volta para a fila (nao some)",
           v2["estado"])

    print("\n--- 5. renomeado/movido: so o caminho ---")
    m = por_arquivo(con, "andou.pdf")
    checar(m["caminho_nuvem"] == "3. Desenvolvimento/OUTRA PASTA/andou.pdf",
           "caminho da nuvem atualizado", str(m["caminho_nuvem"]))
    checar(m["estado"] == "COMPLETED", "mover NAO devolve para a fila", m["estado"])
    checar(m["item_id"] == "it!andou", "a identidade nao mudou")
    checar(m["caminho_anterior"], "guardou de onde veio", str(m["caminho_anterior"]))
    r = con.execute("SELECT * FROM arquivo WHERE item_id='it!renom'").fetchone()
    checar(r["nome"] == "novo.pdf", "nome atualizado pelo delta", r["nome"])
    checar(r["estado"] == "COMPLETED", "renomear NAO devolve para a fila", r["estado"])

    print("\n--- 6. conteudo alterado: volta para a fila ---")
    alt = por_arquivo(con, "alterou.pdf")
    checar(alt["estado"] == "CHANGED", "virou CHANGED", alt["estado"])
    checar(alt["quick_xor"] == "H2" and alt["tamanho_bytes"] == 999,
           "pegou o hash e o tamanho novos", f"{alt['quick_xor']}/{alt['tamanho_bytes']}")
    checar(alt["tentativas"] == 0, "zerou as tentativas do ciclo anterior")
    checar("alterado_na_nuvem" in eventos(con, id_alterou), "gravou o evento")

    print("\n--- 7. quem nao devia ser tocado nao foi ---")
    checar(por_arquivo(con, "semextrator.dwg")["estado"] == "UNSUPPORTED",
           "UNSUPPORTED nao volta para a fila nem com hash diferente")
    checar(por_arquivo(con, "parado.pdf")["estado"] == "COMPLETED",
           "quem nao mudou continua COMPLETED")
    checar(eventos(con, id_parado) == [], "e nem ganhou evento a toa")
    hl = con.execute("SELECT * FROM arquivo WHERE id=?", (id_hash_local,)).fetchone()
    checar(hl["estado"] == "COMPLETED" and hl["quick_xor"] == "HCALC",
           "espelho sem hash nao derruba o hash calculado pelo worker (R4)",
           f"{hl['estado']}/{hl['quick_xor']}")
    checar(con.execute("SELECT COUNT(*) FROM arquivo WHERE item_id='it!novo'").fetchone()[0] == 0,
           "novo do espelho NAO foi inserido (e do 01/02)")

    print("\n--- 8. rodar de novo nao muda mais nada (idempotente) ---")
    p2 = modulo.Propagacao(con, cu.abrir_execucao(con, "teste", "", {}), aplicar=True)
    checar(len(p2.sumiram()) == 0 and len(p2.voltaram()) == 0
           and len(p2.recaminhados()) == 0 and len(p2.alterados()) == 0,
           "segunda passada nao acha mais nada a fazer",
           f"{len(p2.sumiram())}/{len(p2.voltaram())}/"
           f"{len(p2.recaminhados())}/{len(p2.alterados())}")

    con.close()
    shutil.rmtree(area, ignore_errors=True)
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
