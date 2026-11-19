"""
test_dedupe — a deduplicacao continua certa quando o acervo MUDA.

    python tests\\test_dedupe.py

Cobre o que a auditoria achou (docs/11_QA_AUDITORIA.md) e o Agente B corrigiu:

  T-11 / A-1  representante que vira MISSING no meio da rodada — pelo worker
              (404) e pelo delta (17_aplicar_delta): contador certo, copia viva
              eleita NA MESMA transacao, trechos mudam de dono, duplicata_de
              re-apontado, nada apagado;
  T-13 / A-5  requeue() com bytes novos desliga o conteudo antigo;
  CR-2 / M-4  21_backfill_conteudo: legados ganham conteudo, duplicatas orfas
              voltam a ser visiveis (ou voltam a fila), simulacao nao grava,
              idempotente;
  T-17        22_reprocessar_extrator: so versao antiga volta, so o
              representante, COMPLETED de versao atual nunca;
  invariantes cu.checar_invariantes acusa cada violacao num banco sabotado.
"""

from __future__ import annotations

import json
import shutil
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import area_temporaria, checar, escrever_config, placar_final, rodar  # noqa: E402

import cerebro_util as cu  # noqa: E402
import worker as worker_mod  # noqa: E402

DRIVE = "drive!X"
SEQ = {"n": 0}
aplicar_delta = cu.importar_script("17_aplicar_delta")


# ----------------------------------------------------------------------
# Andaime
# ----------------------------------------------------------------------
def banco_novo(prefixo: str):
    area = area_temporaria(prefixo)
    raiz = area / "acervo_falso"
    raiz.mkdir()
    config = escrever_config(area, raiz)
    r = rodar("00_init_db.py", config)
    assert r.returncode == 0, r.stderr
    cfg = cu.carregar_config(config)
    return area, config, cfg, cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])


def ocorrencia(con, nome, *, quick="Q1", tamanho=100, estado="QUEUED", prioridade=0,
               presente=1, conteudo_id=None, duplicata_de=None, espelho=True,
               item_id=None, criado="2026-01-01T00:00:00Z", categoria="pdf"):
    SEQ["n"] += 1
    item_id = item_id or f"it!{SEQ['n']:04d}"
    caminho = f"Programa\\P{prioridade}\\{SEQ['n']:04d}_{nome}"
    cur = con.execute(
        "INSERT INTO arquivo (caminho_rel, biblioteca, pasta_rel, nome, ext, categoria, "
        "tamanho_bytes, prioridade, suportado, estado, presente, presente_nuvem, drive_id, "
        "item_id, quick_xor, caminho_nuvem, conteudo_id, duplicata_de, criado_em_nuvem, "
        "primeira_vez_em, ultima_vez_em) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)",
        (caminho, "Programa", f"P{prioridade}", nome, Path(nome).suffix, categoria, tamanho,
         prioridade, estado, presente, presente, DRIVE, item_id, quick,
         caminho.replace("\\", "/"), conteudo_id, duplicata_de, criado,
         cu.agora_iso(), cu.agora_iso()))
    arquivo_id = int(cur.lastrowid)
    if espelho:
        con.execute(
            "INSERT INTO item_nuvem (drive_id, item_id, parent_id, nome, caminho_rel, eh_pasta, "
            "tamanho_bytes, quick_xor, presente) VALUES (?,?,?,?,?,0,?,?,?)",
            (DRIVE, item_id, "root", nome, caminho.replace("\\", "/"), tamanho, quick,
             presente))
    return arquivo_id


def extracao(con, arquivo_id, *, versao="pdf/1.1", status="ok", trechos=3, extrator="pdf",
             erro=None):
    con.execute("INSERT INTO extracao (arquivo_id, extrator, versao, status, texto_path, "
                "caracteres, trechos, bytes_texto, erro, executado_em) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (arquivo_id, extrator, versao, status, f"derivados/texto/{arquivo_id}.gz",
                 500, trechos, 99, erro, cu.agora_iso()))
    caminho = con.execute("SELECT caminho_rel FROM arquivo WHERE id=?",
                          (arquivo_id,)).fetchone()[0]
    for i in range(trechos):
        con.execute("INSERT INTO texto_fts (arquivo_id, caminho_rel, pagina, trecho_idx, texto) "
                    "VALUES (?,?,0,?,?)", (arquivo_id, caminho, i, f"medicao trecho {i}"))


def grupo(con, quick="Q1", n_copias=2, versao="pdf/1.1"):
    """Representante COMPLETED com extracao/trechos + copias DUPLICATE (v4)."""
    rep = ocorrencia(con, f"rep_{quick}.pdf", quick=quick, estado="COMPLETED")
    extracao(con, rep, versao=versao)
    cid = con.execute(
        "INSERT INTO conteudo (quick_xor, tamanho_bytes, representante_id, extraido_de_item, "
        "estado, extrator, versao_extrator, texto_path, trechos, ocorrencias) "
        "VALUES (?,100,?,?, 'EXTRAIDO','pdf',?,?,3,1)",
        (quick, rep, f"it!{rep}", versao, f"derivados/texto/{rep}.gz")).lastrowid
    con.execute("UPDATE arquivo SET conteudo_id=? WHERE id=?", (cid, rep))
    copias = [ocorrencia(con, f"copia{i}_{quick}.pdf", quick=quick, estado="DUPLICATE",
                         prioridade=i + 1, conteudo_id=cid, duplicata_de=rep)
              for i in range(n_copias)]
    cu.recontar_ocorrencias(con, cid)
    con.commit()
    return rep, copias, int(cid)


def linha(con, tabela, id_):
    return con.execute(f"SELECT * FROM {tabela} WHERE id=?", (id_,)).fetchone()


def trechos_de(con, arquivo_id) -> int:
    return con.execute("SELECT COUNT(*) FROM texto_fts WHERE arquivo_id=?",
                       (arquivo_id,)).fetchone()[0]


def eventos(con, arquivo_id, evento):
    return [json.loads(l[0] or "{}") for l in con.execute(
        "SELECT detalhe FROM evento_arquivo WHERE arquivo_id=? AND evento=? ORDER BY id",
        (arquivo_id, evento))]


def sem_violacao(con) -> tuple[bool, str]:
    v = cu.checar_invariantes(con)
    ruins = {k: ids for k, ids in v.items() if ids}
    return not ruins, str(ruins)


class FonteAusente:
    """Fonte falsa: o Graph responde 404 para tudo."""
    nome = "graph"

    def obter(self, item):
        return {"ok": False, "ausente": True, "transitorio": False,
                "motivo": "404 itemNotFound (falso)"}

    def descartar(self, caminho):
        pass

    def limpar_sobras(self):
        return 0


class FonteComBytes:
    """Fonte falsa: entrega sempre o mesmo arquivo local, com o hash do inventario."""
    nome = "graph"

    def __init__(self, caminho: Path):
        self.caminho = caminho

    def obter(self, item):
        return {"ok": True, "caminho": str(self.caminho), "bytes": self.caminho.stat().st_size,
                "quick_xor": item["quick_xor"], "remover": False, "motivo": "",
                "transitorio": False, "ausente": False, "interrompido": False}

    def descartar(self, caminho):
        pass

    def limpar_sobras(self):
        return 0


def montar_worker(cfg, con, fonte=None):
    args = types.SimpleNamespace(comando="run", config=None, limite=0, prioridade_max=None,
                                 sem_hidratar=False, so_nuvem=False, uma_vez=True,
                                 fonte="graph", ids=None, manter_tmp=False, quieto=True)
    execucao_id = cu.abrir_execucao(con, "worker", "", {"teste": True})
    log = cu.Log(cfg["_logs_path"], "dedupe", con, execucao_id, verboso=False)
    return worker_mod.Worker(cfg, con, log, execucao_id, args,
                             fonte_pronta=fonte or FonteAusente())


# ----------------------------------------------------------------------
def teste_worker_404(cfg, con):
    print("\n--- 1. T-11: o representante da 404 no WORKER, no meio da rodada ---")
    rep, (b, g), cid = grupo(con, "QW")
    w = montar_worker(cfg, con)
    item = con.execute("SELECT * FROM arquivo WHERE id=?", (rep,)).fetchone()
    desfecho = w.processar(item)
    c = linha(con, "conteudo", cid)
    checar(desfecho == "ausente" and linha(con, "arquivo", rep)["estado"] == "MISSING",
           "o 404 virou MISSING", str(desfecho))
    checar(c["representante_id"] == b, "a copia viva de maior prioridade assumiu NA HORA",
           f"rep={c['representante_id']} esperado={b}")
    checar(c["ocorrencias"] == 2, "contador = ocorrencias vivas (2)", str(c["ocorrencias"]))
    checar(trechos_de(con, rep) == 0 and trechos_de(con, b) == 3,
           "os 3 trechos mudaram de dono (nenhum apagado)",
           f"{trechos_de(con, rep)}/{trechos_de(con, b)}")
    caminho_b = linha(con, "arquivo", b)["caminho_rel"]
    checar(con.execute("SELECT COUNT(*) FROM texto_fts WHERE arquivo_id=? AND caminho_rel=?",
                       (b, caminho_b)).fetchone()[0] == 3,
           "e respondem pelo caminho da nova representante")
    checar(linha(con, "arquivo", g)["duplicata_de"] == b and
           linha(con, "arquivo", b)["duplicata_de"] is None,
           "duplicata_de re-apontado; a representante nao e copia de ninguem")
    checar(linha(con, "arquivo", rep)["conteudo_id"] == cid,
           "a ausente guarda o vinculo (historico, DEC-029)")
    checar(con.execute("SELECT COUNT(*) FROM extracao WHERE arquivo_id=?", (rep,)).fetchone()[0]
           == 1, "a extracao da ausente continua la")
    ev = eventos(con, rep, "ausente_na_nuvem")
    checar(ev and ev[-1].get("novo_representante") == b,
           "o evento de ausencia diz quem assumiu", str(ev))
    checar(len(eventos(con, b, "virou_representante")) == 1, "a troca virou evento")
    ok, det = sem_violacao(con)
    checar(ok, "nenhuma invariante violada logo apos o 404", det)


def teste_delta_sumiu(con):
    print("\n--- 2. T-11: o representante some no DELTA (17_aplicar_delta) ---")
    rep, (b, _g), cid = grupo(con, "QD")
    con.execute("UPDATE item_nuvem SET presente=0 WHERE item_id=(SELECT item_id FROM arquivo "
                "WHERE id=?)", (rep,))
    con.commit()
    p = aplicar_delta.Propagacao(con, cu.abrir_execucao(con, "teste", "", {}), aplicar=True)
    trechos_antes = con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0]
    p.marcar_sumidos(p.sumiram())
    con.commit()
    c = linha(con, "conteudo", cid)
    checar(c["representante_id"] == b, "reelegeu dentro do marcar_sumidos",
           str(c["representante_id"]))
    checar(c["ocorrencias"] == 2, "contador recontado (2 vivas)", str(c["ocorrencias"]))
    checar(trechos_de(con, b) == 3 and trechos_de(con, rep) == 0, "trechos com a viva")
    checar(con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0] == trechos_antes,
           "o total de trechos nao mudou")
    checar(p.contadores["reeleitos"] == 1, "o 17 conta a reeleicao")
    ok, det = sem_violacao(con)
    checar(ok, "invariantes limpas depois do delta", det)

    print("\n--- 5. ele volta: recontado ---")
    con.execute("UPDATE item_nuvem SET presente=1 WHERE item_id=(SELECT item_id FROM arquivo "
                "WHERE id=?)", (rep,))
    con.execute("INSERT INTO evento_arquivo (arquivo_id, evento, detalhe, em) VALUES "
                "(?, 'ausente_na_nuvem', '{\"estado_anterior\": \"COMPLETED\"}', ?)",
                (rep, cu.agora_iso()))
    con.commit()
    p.restaurar(p.voltaram())
    con.commit()
    c = linha(con, "conteudo", cid)
    checar(c["ocorrencias"] == 3, "contador voltou a 3", str(c["ocorrencias"]))
    checar(c["representante_id"] == b, "e quem assumiu continua (sem troca a toa)")
    ok, det = sem_violacao(con)
    checar(ok, "invariantes limpas depois da volta", det)


def teste_sem_copia_viva(con):
    print("\n--- 3. representante some e NAO ha copia viva: historico intacto ---")
    rep, _c, cid = grupo(con, "QS", n_copias=0)
    con.execute("UPDATE item_nuvem SET presente=0 WHERE item_id=(SELECT item_id FROM arquivo "
                "WHERE id=?)", (rep,))
    con.commit()
    p = aplicar_delta.Propagacao(con, cu.abrir_execucao(con, "teste", "", {}), aplicar=True)
    p.marcar_sumidos(p.sumiram())
    con.commit()
    c = linha(con, "conteudo", cid)
    checar(c["representante_id"] == rep and c["ocorrencias"] == 0,
           "ninguem para assumir: representante fica, contador 0")
    checar(trechos_de(con, rep) == 3, "trechos preservados (voltam a valer se reaparecer)")
    ok, det = sem_violacao(con)
    checar(ok, "isso NAO e violacao: e historico", det)


def teste_requeue(con):
    print("\n--- 4. T-13: requeue() com bytes novos desliga o conteudo antigo ---")
    rep, (b, _g), cid = grupo(con, "QR")
    con.execute("UPDATE item_nuvem SET quick_xor='NOVO', tamanho_bytes=777 WHERE item_id="
                "(SELECT item_id FROM arquivo WHERE id=?)", (rep,))
    con.commit()
    p = aplicar_delta.Propagacao(con, cu.abrir_execucao(con, "teste", "", {}), aplicar=True)
    p.requeue(p.alterados())
    con.commit()
    a = linha(con, "arquivo", rep)
    c = linha(con, "conteudo", cid)
    checar(a["estado"] == "CHANGED" and a["conteudo_id"] is None,
           "CHANGED e SEM o conteudo velho", f"{a['estado']}/{a['conteudo_id']}")
    checar(c["representante_id"] == b and c["ocorrencias"] == 2,
           "o conteudo velho foi para uma copia viva e recontou",
           f"{c['representante_id']}/{c['ocorrencias']}")
    checar(trechos_de(con, b) == 3 and trechos_de(con, rep) == 0,
           "o texto velho nao responde mais pelo arquivo alterado")
    ev = eventos(con, rep, "alterado_na_nuvem")
    checar(ev and ev[-1].get("conteudo_id_anterior") == cid,
           "o evento guarda o conteudo anterior (historico)", str(ev))
    checar(con.execute("SELECT COUNT(*) FROM v_ocorrencia WHERE arquivo_id=?",
                       (rep,)).fetchone()[0] == 0,
           "v_ocorrencia nao diz mais que esse caminho carrega o texto velho")
    ok, det = sem_violacao(con)
    checar(ok, "invariantes limpas depois do requeue", det)

    print("\n--- 4b. requeue sem copia viva: conteudo vira historico ---")
    so, _c, cid2 = grupo(con, "QR2", n_copias=0)
    con.execute("UPDATE item_nuvem SET quick_xor='NOVO2' WHERE item_id=(SELECT item_id FROM "
                "arquivo WHERE id=?)", (so,))
    con.commit()
    p.requeue(p.alterados())
    con.commit()
    c2 = linha(con, "conteudo", cid2)
    checar(linha(con, "arquivo", so)["conteudo_id"] is None and c2["ocorrencias"] == 0,
           "desligado e recontado para 0")
    checar(c2["texto_path"] is not None and
           con.execute("SELECT COUNT(*) FROM extracao WHERE arquivo_id=?", (so,)).fetchone()[0]
           == 1, "conteudo, derivado e extracao preservados (REGRA 6)")
    ok, det = sem_violacao(con)
    checar(ok, "e nada disso e violacao", det)


def teste_cobrir_troca(cfg, con):
    print("\n--- 6. representante que vira copia de OUTRO conteudo passa o posto ---")
    rep, (b, _g), cid = grupo(con, "QC")
    _r2, _c2, cid_n = grupo(con, "QN", n_copias=0)
    w = montar_worker(cfg, con)
    con.execute("UPDATE arquivo SET quick_xor='QN', estado='CHANGED' WHERE id=?", (rep,))
    item = con.execute("SELECT * FROM arquivo WHERE id=?", (rep,)).fetchone()
    w.cobrir_por_conteudo(item, linha(con, "conteudo", cid_n))
    con.commit()
    checar(linha(con, "conteudo", cid)["representante_id"] == b,
           "o grupo antigo ganhou representante vivo e membro")
    checar(linha(con, "conteudo", cid)["ocorrencias"] == 2, "e recontou")
    ok, det = sem_violacao(con)
    checar(ok, "invariantes limpas", det)


def teste_gravar_conteudo(cfg, con):
    print("\n--- 7. gravar_conteudo: conta so as vivas e o grupo antigo reelege ---")
    rep, (b, g), cid = grupo(con, "QG")
    con.execute("UPDATE arquivo SET presente=0, estado='MISSING' WHERE id=?", (g,))
    cu.recontar_ocorrencias(con, cid)
    con.commit()
    w = montar_worker(cfg, con)
    ok_res = {"status": "ok", "extrator": "pdf", "versao": "pdf/1.1",
              "paginas": 1, "caracteres": 500}
    item = con.execute("SELECT * FROM arquivo WHERE id=?", (rep,)).fetchone()
    w.gravar_conteudo(item, ok_res, "QG", None, None, 3, 99)
    con.commit()
    checar(linha(con, "conteudo", cid)["ocorrencias"] == 2,
           "reextrair o representante nao conta a copia MISSING",
           str(linha(con, "conteudo", cid)["ocorrencias"]))
    # O representante mudou de bytes e foi reextraido: conteudo novo. O
    # worker grava o hash dos bytes que chegaram (passo 2b) antes do passo 5.
    con.execute("UPDATE arquivo SET quick_xor='QH' WHERE id=?", (rep,))
    item = con.execute("SELECT * FROM arquivo WHERE id=?", (rep,)).fetchone()
    novo = w.gravar_conteudo(item, ok_res, "QH", None, None, 3, 99)
    con.commit()
    checar(novo is not None and novo != cid, "conteudo novo para os bytes novos")
    antigo = linha(con, "conteudo", cid)
    checar(antigo["representante_id"] == b,
           "o grupo antigo ganhou a copia viva como representante",
           str(antigo["representante_id"]))
    checar(antigo["ocorrencias"] == 1, "e recontou so a viva", str(antigo["ocorrencias"]))
    ok, det = sem_violacao(con)
    checar(ok, "invariantes limpas", det)


def teste_invariantes():
    print("\n--- 7. cada invariante acusa a sua violacao ---")
    area, _config, _cfg, con = banco_novo("cerebro_inv_")
    ok, det = sem_violacao(con)
    checar(ok, "banco vazio: nenhuma violacao", det)
    rep, (b, g), cid = grupo(con, "QI")
    sabotagens = {
        "duplicata_com_representante_ausente":
            f"UPDATE arquivo SET presente=0 WHERE id={rep}",
        "conteudo_orfao_reelegivel": f"UPDATE arquivo SET presente=0 WHERE id={rep}",
        "contador_divergente": f"UPDATE conteudo SET ocorrencias=99 WHERE id={cid}",
        "conteudo_id_inexistente": f"UPDATE arquivo SET conteudo_id=4242 WHERE id={g}",
        "ocorrencia_com_hash_de_outro_conteudo":
            f"UPDATE arquivo SET quick_xor='OUTRO' WHERE id={g}",
        "trecho_em_ausente_com_copia_viva": f"UPDATE arquivo SET presente=0 WHERE id={rep}",
        "duplicata_sem_conteudo": f"UPDATE arquivo SET conteudo_id=NULL WHERE id={b}",
        "extraido_sem_conteudo": f"UPDATE arquivo SET conteudo_id=NULL WHERE id={rep}",
    }
    for nome, sql in sabotagens.items():
        con.execute("SAVEPOINT s")
        con.execute(sql)
        acusou = cu.checar_invariantes(con)[nome]
        con.execute("ROLLBACK TO s")
        con.execute("RELEASE s")
        checar(bool(acusou), f"acusa {nome}", str(acusou))
    ok, det = sem_violacao(con)
    checar(ok, "desfeitas as sabotagens, limpo de novo", det)
    con.close()
    shutil.rmtree(area, ignore_errors=True)


def teste_backfill():
    print("\n--- 8. CR-2 / M-4: 21_backfill_conteudo num banco com legados ---")
    area, config, _cfg, con = banco_novo("cerebro_backfill_")
    # legado 1: representante MISSING (sumiu no E8) com 2 copias vivas DUPLICATE
    morto = ocorrencia(con, "legado_morto.pdf", quick="L1", estado="MISSING", presente=0)
    extracao(con, morto, versao="pdf/1.0")
    c1 = ocorrencia(con, "copia_a.pdf", quick="L1", estado="DUPLICATE", prioridade=2,
                    duplicata_de=morto)
    c2 = ocorrencia(con, "copia_b.pdf", quick="L1", estado="DUPLICATE", prioridade=1,
                    duplicata_de=morto)
    # legado 2: representante vivo COMPLETED com 1 copia
    vivo = ocorrencia(con, "legado_vivo.pdf", quick="L2", estado="COMPLETED")
    extracao(con, vivo, versao="pdf/1.0")
    c3 = ocorrencia(con, "copia_c.pdf", quick="L2", estado="DUPLICATE", duplicata_de=vivo)
    # orfa sem conserto: representante ausente SEM extracao
    sem_ext = ocorrencia(con, "rep_sem_extracao.pdf", quick="L3", estado="MISSING", presente=0)
    orfa = ocorrencia(con, "orfa.pdf", quick="L3", estado="DUPLICATE", duplicata_de=sem_ext)
    # vazio legado: 0 byte nao ganha conteudo (DEC-011)
    vazio = ocorrencia(con, "vazio.pdf", quick="L0", tamanho=0, estado="COMPLETED")
    extracao(con, vazio, versao="-", status="vazio", trechos=0)
    # A-5 ja no banco: CHANGED com conteudo velho
    rep_a5, (cop_a5,), cid_a5 = grupo(con, "QA5", n_copias=1)
    con.execute("UPDATE arquivo SET estado='CHANGED', quick_xor='NOVO' WHERE id=?", (rep_a5,))
    con.commit()
    v = cu.checar_invariantes(con)
    checar(len(v["duplicata_com_representante_ausente"]) == 3 and
           len(v["duplicata_sem_conteudo"]) == 4 and len(v["extraido_sem_conteudo"]) == 1
           and v["ocorrencia_com_hash_de_outro_conteudo"] == [rep_a5],
           "o cenario reproduz CR-2, M-4 e A-5", str(v))
    antes = con.execute("SELECT (SELECT COUNT(*) FROM arquivo), (SELECT COUNT(*) FROM conteudo),"
                        " (SELECT COUNT(*) FROM evento_arquivo), (SELECT COUNT(*) FROM texto_fts)"
                        ).fetchone()
    con.close()

    sim = rodar("21_backfill_conteudo.py", config)
    con = cu.conectar(cu.carregar_config(config)["_banco_path"])
    depois_sim = con.execute("SELECT (SELECT COUNT(*) FROM arquivo), (SELECT COUNT(*) FROM "
                             "conteudo), (SELECT COUNT(*) FROM evento_arquivo), "
                             "(SELECT COUNT(*) FROM texto_fts)").fetchone()
    con.close()
    checar(tuple(antes) == tuple(depois_sim), "SIMULACAO nao grava nada",
           f"{tuple(antes)} -> {tuple(depois_sim)}")
    checar("legados_vinculados" in sim.stdout and sim.returncode == 0,
           "a simulacao preve o resultado limpo (saida 0)", sim.stdout[-600:] + sim.stderr[-300:])

    r = rodar("21_backfill_conteudo.py", config, "--aplicar")
    checar(r.returncode == 0, "--aplicar termina sem violacao", r.stdout[-900:] + r.stderr[-300:])
    backups = list((area / "backup").glob("*pre_backfill*.db"))
    checar(len(backups) == 1, "fez backup ANTES de gravar (data/backup/)", str(backups))
    con = cu.conectar(cu.carregar_config(config)["_banco_path"])
    m = linha(con, "arquivo", morto)
    cid = m["conteudo_id"]
    checar(cid is not None, "o legado morto ganhou conteudo")
    c = linha(con, "conteudo", cid)
    checar(c["representante_id"] == c2, "a copia viva de maior prioridade virou representante",
           f"{c['representante_id']} esperado {c2}")
    checar(c["versao_extrator"] == "pdf/1.0" and c["estado"] == "EXTRAIDO",
           "o conteudo herda versao e estado da extracao legada")
    checar(linha(con, "arquivo", c1)["conteudo_id"] == cid and
           linha(con, "arquivo", c1)["duplicata_de"] == c2, "a outra copia entrou e re-apontou")
    checar(trechos_de(con, c2) == 3 and trechos_de(con, morto) == 0,
           "os trechos do morto mudaram para a viva (achaveis de novo)")
    checar(linha(con, "arquivo", c3)["conteudo_id"] == linha(con, "arquivo", vivo)["conteudo_id"]
           and linha(con, "arquivo", vivo)["conteudo_id"] is not None,
           "legado vivo e sua copia no mesmo conteudo")
    o = linha(con, "arquivo", orfa)
    checar(o["estado"] == "QUEUED" and o["duplicata_de"] is None,
           "a orfa sem conserto VOLTOU para a fila", f"{o['estado']}/{o['duplicata_de']}")
    checar(eventos(con, orfa, "duplicata_orfa_devolvida")[0]["duplicata_de_anterior"] == sem_ext,
           "e o evento guarda de quem ela era copia")
    checar(linha(con, "arquivo", vazio)["conteudo_id"] is None, "0 byte nao ganha conteudo")
    checar(linha(con, "arquivo", rep_a5)["conteudo_id"] is None and
           linha(con, "conteudo", cid_a5)["representante_id"] == cop_a5,
           "o caso A-5 (#128398) foi desligado e o conteudo passou a copia viva")
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == antes[0] and
           con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0] == antes[3],
           "nenhuma linha e nenhum trecho apagados")
    ok, det = sem_violacao(con)
    checar(ok, "zero violacoes depois do backfill", det)
    eventos_depois = con.execute("SELECT COUNT(*) FROM evento_arquivo").fetchone()[0]
    con.close()

    r2 = rodar("21_backfill_conteudo.py", config, "--aplicar", "--sem-backup")
    con = cu.conectar(cu.carregar_config(config)["_banco_path"])
    checar(r2.returncode == 0 and con.execute("SELECT COUNT(*) FROM evento_arquivo").fetchone()[0]
           == eventos_depois, "idempotente: a segunda passada nao faz nada")
    checar(con.execute("SELECT COUNT(*) FROM execucao WHERE status='em_andamento'").fetchone()[0]
           == 0, "nenhuma execucao ficou aberta")
    con.close()
    shutil.rmtree(area, ignore_errors=True)


def teste_reprocessar():
    import extratores
    print("\n--- 9. T-17: reprocessar por versao de extrator ---")
    area, config, cfg, con = banco_novo("cerebro_versao_")
    velho, (cv,), cid_v = grupo(con, "V1", n_copias=1, versao="pdf/1.0")
    atual, _c, _cid = grupo(con, "V2", n_copias=0, versao=cu.versao_atual_extrator("pdf"))
    vazio = ocorrencia(con, "vazio.pdf", quick="V0", tamanho=0, estado="COMPLETED")
    extracao(con, vazio, versao="-", status="vazio", trechos=0)
    # R5a: o PARTIAL adiado pelo teto, com a MARCA que o worker grava de verdade
    # (versao e erro saem do proprio worker, nao de uma copia da string).
    adiado = ocorrencia(con, "grande.pdf", quick="V3", estado="PARTIAL",
                        tamanho=400 * 1024 ** 2)
    marca = worker_mod.resultado_adiado_por_tamanho(linha(con, "arquivo", adiado), 300)
    extracao(con, adiado, versao=marca["versao"], status=marca["status"], trechos=0,
             erro=marca["erro"])
    # R5a: UNSUPPORTED "sem leitor" gravado por uma versao ANTERIOR do extrator
    # da categoria: subir a versao do xlsx nao da leitor ao .xls.
    sem_leitor = ocorrencia(con, "planilha.xls", quick="V5", estado="UNSUPPORTED",
                            categoria="xlsx")
    r_sl = extratores.resultado_sem_leitor(".xls", "xlsx")
    extracao(con, sem_leitor, extrator="xlsx", versao="xlsx/1.0", status=r_sl["status"],
             trechos=0, erro=f"{worker_mod.PREFIXO_ERRO_SEM_LEITOR}: {r_sl['erro']}; "
                             f"nada foi baixado")
    # ... e o nao_suportado de uma extensao QUE TEM leitor (OLE renomeado): a
    # versao nova do extrator pode le-lo, entao ele volta no padrao.
    ole = ocorrencia(con, "renomeado.xlsx", quick="V6", estado="UNSUPPORTED", categoria="xlsx")
    extracao(con, ole, extrator="xlsx", versao="xlsx/1.0", status="nao_suportado", trechos=0,
             erro="binario OLE com extensao OOXML (formato legado renomeado)")
    # representante velho que ja sumiu e cuja copia viva assumiu
    morto, (viva,), cid_m = grupo(con, "V4", n_copias=1, versao="pdf/1.0")
    con.execute("UPDATE arquivo SET presente=0, estado='MISSING' WHERE id=?", (morto,))
    cu.ocorrencia_saiu(con, morto)
    # todas as extracoes acima sao "de antes": a reextracao vem depois
    con.execute("UPDATE extracao SET executado_em='2026-09-18T00:00:00+00:00'")
    con.commit()

    alvos = {c["alvo_id"] for c in cu.extracoes_desatualizadas(con)}
    checar(alvos == {velho, viva, ole},
           "padrao: so as de versao antiga, pelo representante VIVO",
           f"{alvos} esperado {{{velho}, {viva}, {ole}}}")
    checar(atual not in alvos and vazio not in alvos,
           "versao atual e vazio ficam de fora no padrao")
    checar(adiado not in alvos,
           f"R5a: a marca '{worker_mod.VERSAO_ADIADO_TETO}' NAO e versao antiga (fora no padrao)")
    checar(sem_leitor not in alvos,
           "R5a: UNSUPPORTED sem leitor (extensao ainda sem leitor) fica de fora no padrao")
    adiados = {c["alvo_id"] for c in cu.extracoes_desatualizadas(con, erro_contem="adiada")}
    checar(adiados == {adiado}, "o filtro por erro pega os PARTIAL adiados", str(adiados))
    por_marca = {c["alvo_id"] for c in cu.extracoes_desatualizadas(
        con, versoes=[worker_mod.VERSAO_ADIADO_TETO])}
    checar(por_marca == {adiado}, "pedir a marca por --versao tambem os traz", str(por_marca))
    por_prefixo = {c["alvo_id"] for c in cu.extracoes_desatualizadas(
        con, erro_contem=worker_mod.PREFIXO_ERRO_SEM_LEITOR)}
    checar(por_prefixo == {sem_leitor}, "o sem leitor so volta pedido por --erro SEM_LEITOR",
           str(por_prefixo))
    checar(cu.extracoes_desatualizadas(con, estados=["COMPLETED"], categorias=["pdf"],
                                       versoes=[cu.versao_atual_extrator("pdf")]) == [],
           "COMPLETED de versao atual nunca, nem pedindo explicitamente")
    con.close()

    antes = rodar("22_reprocessar_extrator.py", config)
    con = cu.conectar(cu.carregar_config(config)["_banco_path"])
    checar(linha(con, "arquivo", velho)["estado"] == "COMPLETED" and "SIMULACAO" in antes.stdout,
           "a simulacao nao grava", antes.stdout[-300:])
    con.close()
    r = rodar("22_reprocessar_extrator.py", config, "--aplicar")
    con = cu.conectar(cu.carregar_config(config)["_banco_path"])
    checar(r.returncode == 0, "--aplicar roda", r.stdout[-400:] + r.stderr[-300:])
    checar(linha(con, "arquivo", velho)["estado"] == "QUEUED" and
           linha(con, "arquivo", viva)["estado"] == "QUEUED",
           "os representantes de versao antiga voltaram para a fila")
    checar(linha(con, "arquivo", cv)["estado"] == "DUPLICATE",
           "a copia continua coberta (nao baixa de novo)")
    checar(linha(con, "arquivo", atual)["estado"] == "COMPLETED",
           "COMPLETED de versao atual NAO voltou")
    ev = eventos(con, velho, "reprocessar_versao_extrator")
    checar(ev and ev[0]["versao_anterior"] == "pdf/1.0" and ev[0]["extracao_anterior"],
           "o evento guarda o retrato da extracao anterior", str(ev)[:200])
    checar(linha(con, "conteudo", cid_v)["texto_path"] is not None and
           trechos_de(con, velho) == 3,
           "enquanto espera, a extracao anterior continua pesquisavel")
    checar(con.execute("SELECT COUNT(*) FROM v_fila_nuvem WHERE id IN (?,?)",
                       (velho, viva)).fetchone()[0] == 2, "e o worker da nuvem os enxerga")
    checar(linha(con, "arquivo", adiado)["estado"] == "PARTIAL" and
           linha(con, "arquivo", sem_leitor)["estado"] == "UNSUPPORTED",
           "R5a: --aplicar sem filtro nao devolve o adiado nem o sem leitor")
    con.close()
    r2 = rodar("22_reprocessar_extrator.py", config, "--aplicar")
    checar("voltam para a fila (1 por conteudo): 0" in r2.stdout,
           "rodar de novo nao duplica nada", r2.stdout[-300:])

    print("\n--- 9b. R5b: depois da reeleicao, reextraido NAO volta (sem laco) ---")
    import fitz
    pdf = area / "reextrair.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((50, 60), "medicao de pavimento reextraida " * 4, fontsize=9)
    doc.save(str(pdf))
    doc.close()
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    w = montar_worker(cfg, con, FonteComBytes(pdf))
    for alvo in (viva, velho):                    # o worker reextrai o que voltou
        desfecho = w.processar(con.execute("SELECT * FROM arquivo WHERE id=?",
                                           (alvo,)).fetchone())
        checar(desfecho == "ok" and linha(con, "arquivo", alvo)["estado"] == "COMPLETED",
               f"o worker reextraiu #{alvo}", f"{desfecho} {linha(con, 'arquivo', alvo)['estado']}")
    cu.fechar_execucao(con, w.execucao_id, "concluida", w.contadores, 0.0)
    perfil_path = con.execute("SELECT perfil_path FROM extracao WHERE arquivo_id=?",
                              (viva,)).fetchone()[0]
    perfil = json.loads(Path(perfil_path).read_text(encoding="utf-8")) if perfil_path else {}
    checar(extratores.metricas_ocr_de_texto(
               (perfil.get("ocr_metricas") or {}).get("paginas", "")) != [],
           "R6: o worker leva as metricas de OCR por pagina ao perfil JSON (chave extra)",
           str(perfil.get("ocr_metricas")))
    atual_pdf = cu.versao_atual_extrator("pdf")
    checar(con.execute("SELECT versao FROM extracao WHERE arquivo_id=?", (viva,)).fetchone()[0]
           == atual_pdf, "o novo representante tem extracao na versao atual")
    checar(con.execute("SELECT versao FROM extracao WHERE arquivo_id=?", (morto,)).fetchone()[0]
           == "pdf/1.0", "a linha velha do ex-representante MISSING continua la (historico)")
    alvos2 = {c["alvo_id"] for c in cu.extracoes_desatualizadas(con)}
    checar(viva not in alvos2,
           "a decisao e pelo CONTEUDO: a extracao mais recente ja e da versao atual",
           str(alvos2))
    con.close()
    r3 = rodar("22_reprocessar_extrator.py", config, "--aplicar")
    checar(r3.returncode == 0 and "voltam para a fila (1 por conteudo): 0" in r3.stdout,
           "R5b: 2a passada do 22 --aplicar depois da reextracao devolve 0",
           r3.stdout[-400:] + r3.stderr[-200:])
    con = cu.conectar(cu.carregar_config(config)["_banco_path"])
    checar(linha(con, "arquivo", viva)["estado"] == "COMPLETED",
           "o representante reextraido continua COMPLETED (nao voltou para a fila)")
    checar(con.execute("SELECT COUNT(*) FROM execucao WHERE status='em_andamento'").fetchone()[0]
           == 0, "execucoes fechadas")

    print("\n--- 9c. R5c: o 22 so conta o que o UPDATE de fato mudou ---")
    mod22 = cu.importar_script("22_reprocessar_extrator")
    v7, _c7, _cid7 = grupo(con, "V7", n_copias=0, versao="pdf/1.0")
    v8, _c8, _cid8 = grupo(con, "V8", n_copias=0, versao="pdf/1.0")
    args = types.SimpleNamespace(categoria="pdf", versao=None, erro=None, estado=None,
                                 sem_versao=False, limite=0)
    plano, _fora = mod22.planejar(con, args)
    checar({c["alvo_id"] for c in plano} == {v7, v8}, "plano com 2 itens",
           str([c["alvo_id"] for c in plano]))
    # entre o plano e o aplicar, alguem (o worker) mexeu em um deles
    con.execute("UPDATE arquivo SET estado='EXTRACTING' WHERE id=?", (v8,))
    con.commit()
    eventos_antes = len(eventos(con, v8, "reprocessar_versao_extrator"))
    res = mod22.aplicar(con, plano, cu.abrir_execucao(con, "teste", "", {}))
    feitos = res[0] if isinstance(res, tuple) else res
    checar(feitos == 1, "feitos = 1 (o UPDATE do outro nao casou linha)", str(res))
    checar(linha(con, "arquivo", v8)["estado"] == "EXTRACTING" and
           len(eventos(con, v8, "reprocessar_versao_extrator")) == eventos_antes,
           "o item que mudou de estado nao foi tocado nem ganhou evento falso")
    checar(linha(con, "arquivo", v7)["estado"] == "QUEUED", "o outro voltou para a fila")
    con.close()
    shutil.rmtree(area, ignore_errors=True)


def teste_simulacao_fecha_execucao():
    print("\n--- 10. M-5: a simulacao do 17 fecha a execucao ---")
    area, config, _cfg, con = banco_novo("cerebro_m5_")
    con.close()
    r = rodar("17_aplicar_delta.py", config)
    con = cu.conectar(cu.carregar_config(config)["_banco_path"])
    st = con.execute("SELECT status FROM execucao WHERE etapa='aplicar_delta'").fetchall()
    checar(r.returncode == 0 and [s[0] for s in st] == ["simulacao"],
           "execucao registrada como 'simulacao', nao 'em_andamento'", str([tuple(s) for s in st]))
    con.close()
    shutil.rmtree(area, ignore_errors=True)


def teste_realinhar_caminho():
    print("\n--- 11. A-6/T-14: o caminho do CATALOGO acompanha o move ---")
    import yaml
    area = area_temporaria("cerebro_a6_")
    raiz = area / "acervo_falso"
    raiz.mkdir()
    mapa = area / "mapa.yaml"
    mapa.write_text(yaml.safe_dump({"mapa_biblioteca": {"Programa - 3. Desenvolvimento": {
        "drive_id": DRIVE, "pasta_nuvem": "3. Desenvolvimento"}}}), encoding="utf-8")
    config = escrever_config(area, raiz, mapa_drives=str(mapa))
    assert rodar("00_init_db.py", config).returncode == 0
    cfg = cu.carregar_config(config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])

    def par(nome, pasta_nuvem, estado, ext=".pdf", categoria="pdf"):
        SEQ["n"] += 1
        item = f"it!a6_{SEQ['n']}"
        cam_nuvem = f"3. Desenvolvimento\\{pasta_nuvem}\\{nome}"
        cam_cat = f"Programa - 3. Desenvolvimento\\{pasta_nuvem}\\{nome}"
        cur = con.execute(
            "INSERT INTO arquivo (caminho_rel, biblioteca, pasta_rel, nome, ext, categoria, "
            "tamanho_bytes, prioridade, suportado, estado, presente, presente_nuvem, drive_id, "
            "item_id, quick_xor, caminho_nuvem, primeira_vez_em, ultima_vez_em) "
            "VALUES (?,?,?,?,?,?,100,0,1,?,1,1,?,?,?,?,?,?)",
            (cam_cat, "Programa - 3. Desenvolvimento", cam_cat.rsplit("\\", 1)[0], nome, ext,
             categoria, estado, DRIVE, item, f"Q{item}", cam_nuvem, cu.agora_iso(),
             cu.agora_iso()))
        con.execute("INSERT INTO item_nuvem (drive_id, item_id, parent_id, nome, caminho_rel, "
                    "eh_pasta, ext, tamanho_bytes, quick_xor, presente) "
                    "VALUES (?,?,?,?,?,0,?,100,?,1)",
                    (DRIVE, item, "p", nome, cam_nuvem, ext, f"Q{item}"))
        return int(cur.lastrowid), item

    movido, item_m = par("medicao_41.pdf", "RD103\\Medicoes", "COMPLETED")
    con.execute("INSERT INTO texto_fts (arquivo_id, caminho_rel, pagina, trecho_idx, texto) "
                "VALUES (?,?,0,0,'medicao')",
                (movido, "Programa - 3. Desenvolvimento\\RD103\\Medicoes\\medicao_41.pdf"))
    parado, _ = par("intocado.pdf", "RD103\\Medicoes", "COMPLETED")
    trocou_ext, item_t = par("planilha.txt", "RD103\\Medicoes", "QUEUED", ".txt", "texto")
    con.commit()
    p0 = aplicar_delta.Propagacao(con, cu.abrir_execucao(con, "teste", "", {}), True, cfg=cfg)
    checar(p0.desalinhados() == [], "banco coerente: nada a realinhar (sem falso positivo)")

    con.execute("UPDATE item_nuvem SET caminho_rel=? WHERE item_id=?",
                ("3. Desenvolvimento\\RD103\\0 - Obsoleto\\medicao_41.pdf", item_m))
    con.execute("UPDATE item_nuvem SET caminho_rel=?, nome='planilha.dwg', ext='.dwg' "
                "WHERE item_id=?", ("3. Desenvolvimento\\RD103\\Medicoes\\planilha.dwg", item_t))
    con.commit()
    p = aplicar_delta.Propagacao(con, cu.abrir_execucao(con, "teste", "", {}), True, cfg=cfg)
    p.recaminhar(p.recaminhados())
    des = p.desalinhados()
    checar(sorted(l["arquivo_id"] for l in des) == sorted([movido, trocou_ext]),
           "acha exatamente os 2 que mudaram", str([l["arquivo_id"] for l in des]))
    p.realinhar(des)
    con.commit()
    m = linha(con, "arquivo", movido)
    checar(m["caminho_rel"] == "Programa - 3. Desenvolvimento\\RD103\\0 - Obsoleto\\medicao_41.pdf"
           and m["pasta_rel"] == "Programa - 3. Desenvolvimento\\RD103\\0 - Obsoleto",
           "caminho_rel e pasta_rel no lugar novo", f"{m['caminho_rel']} | {m['pasta_rel']}")
    checar(m["prioridade"] == 4, "movido para Obsoleto: prioridade da pasta nova (P4)",
           str(m["prioridade"]))
    checar(m["estado"] == "COMPLETED" and m["item_id"] == item_m,
           "estado e identidade intactos (mover nao devolve a fila)")
    checar(con.execute("SELECT caminho_rel FROM texto_fts WHERE arquivo_id=?",
                       (movido,)).fetchone()[0] == m["caminho_rel"],
           "o trecho do indice mostra o caminho novo")
    checar(con.execute("SELECT COUNT(*) FROM pasta WHERE caminho_rel=?",
                       (m["pasta_rel"],)).fetchone()[0] == 1, "a pasta nova entrou em `pasta`")
    ev = eventos(con, movido, "caminho_realinhado")
    checar(ev and ev[0]["prioridade_antes"] == 0 and ev[0]["prioridade_depois"] == 4,
           "o evento guarda de onde veio e a prioridade antes/depois", str(ev)[:200])
    t = linha(con, "arquivo", trocou_ext)
    checar(t["ext"] == ".dwg" and t["estado"] == "UNSUPPORTED" and t["suportado"] == 0,
           "rename para extensao sem extrator: sai da fila como UNSUPPORTED",
           f"{t['ext']}/{t['estado']}/{t['suportado']}")
    checar(eventos(con, parado, "caminho_realinhado") == [], "quem nao mudou nao foi tocado")
    p2 = aplicar_delta.Propagacao(con, cu.abrir_execucao(con, "teste", "", {}), True, cfg=cfg)
    checar(p2.desalinhados() == [], "idempotente")
    con.close()
    shutil.rmtree(area, ignore_errors=True)


def main() -> int:
    area, _config, cfg, con = banco_novo("cerebro_dedupe_")
    teste_worker_404(cfg, con)
    teste_delta_sumiu(con)
    teste_sem_copia_viva(con)
    teste_requeue(con)
    teste_cobrir_troca(cfg, con)
    teste_gravar_conteudo(cfg, con)
    con.close()
    shutil.rmtree(area, ignore_errors=True)
    teste_invariantes()
    teste_backfill()
    teste_reprocessar()
    teste_simulacao_fecha_execucao()
    teste_realinhar_caminho()
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
