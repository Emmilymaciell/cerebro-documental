"""
test_ingestao — T-07: o que so existe no espelho entra no catalogo.

    python tests\\test_ingestao.py

Banco falso, sem rede, sem baixar um byte. O que este teste tem de provar:

  arquivo novo        -> entra em `arquivo` com caminho, biblioteca, categoria,
                         hash, autoria e prioridade P0..P4;
  pasta nova          -> ganha linha em `pasta`, com motivo, e os ancestrais
                         tambem;
  duplicata           -> um novo cujo conteudo JA foi extraido entra na fila
                         normalmente; quem cobre e o worker, antes de baixar
                         (`DEC-026`/`DEC-027`). A linha de `conteudo` nao e
                         tocada aqui;
  sem extrator        -> `UNSUPPORTED` (catalogado, nunca perdido — REGRA 6);
  temporario `~$`     -> `SKIPPED`;
  ja conhecido        -> NAO e reinserido nem devolvido para a fila;
  reaparecido         -> nao e "novo": tem `item_id`, e do `17_aplicar_delta`;
  renomeado / movido  -> idem: identidade e `(drive_id, item_id)` (`DEC-015`);
  caminho reencarnado -> mesmo caminho, `item_id` novo = item NOVO, e a linha
                         velha fica intacta;
  simular             -> nao grava uma linha sequer;
  rodar duas vezes    -> a segunda nao insere nada.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import area_temporaria, checar, escrever_config, placar_final, rodar  # noqa: E402

import cerebro_util as cu  # noqa: E402

ingestao_mod = cu.importar_script("19_ingerir_novos")

DRIVE = "drive!Programa"
DOCS = "drive!DOCS"
BIB = "Programa - 3. Desenvolvimento"


# ----------------------------------------------------------------------
# Cenario
# ----------------------------------------------------------------------

def escrever_mapa(area: Path) -> Path:
    destino = area / "mapa_drives_teste.yaml"
    destino.write_text(yaml.safe_dump({
        "site_id": "site!teste",
        "mapa_biblioteca": {
            BIB: {"drive_id": DRIVE, "pasta_nuvem": "3. Desenvolvimento",
                  "como_casou": "teste"},
        },
    }, allow_unicode=True), encoding="utf-8")
    return destino


def espelho(con, item_id: str, caminho: str, *, drive=DRIVE, nome=None, tamanho=100,
            quick="Q-", presente=1, eh_pasta=0):
    nome = nome if nome is not None else caminho.rsplit("\\", 1)[-1]
    con.execute(
        "INSERT INTO item_nuvem (drive_id, item_id, parent_id, nome, caminho_rel, "
        "eh_pasta, ext, tamanho_bytes, quick_xor, etag, ctag, web_url, criado_em, "
        "modificado_em, criado_por, modificado_por, presente, primeira_vez_em, "
        "ultima_vez_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (drive, item_id, "root", nome, caminho, eh_pasta,
         Path(nome).suffix.lower(), tamanho, quick + item_id, "e" + item_id,
         "c" + item_id, f"https://x/{item_id}", "2026-09-01T10:00:00Z",
         "2026-09-02T11:00:00Z", "Fulano da Silva", "Beltrana Souza",
         presente, cu.agora_iso(), cu.agora_iso()))
    con.commit()


def catalogo(con, item_id: str, caminho_catalogo: str, *, drive=DRIVE,
             estado="COMPLETED", presente=1, presente_nuvem=1, tamanho=100,
             quick=None) -> int:
    nome = caminho_catalogo.rsplit("\\", 1)[-1]
    cur = con.execute(
        "INSERT INTO arquivo (caminho_rel, biblioteca, pasta_rel, nome, ext, categoria, "
        "nome_normalizado, tamanho_bytes, prioridade, suportado, estado, presente, "
        "presente_nuvem, drive_id, item_id, quick_xor, caminho_nuvem, web_url, origem, "
        "primeira_vez_em, ultima_vez_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (caminho_catalogo, caminho_catalogo.split("\\", 1)[0],
         caminho_catalogo.rsplit("\\", 1)[0], nome, Path(nome).suffix.lower(), "pdf",
         cu.normalizar_nome(nome), tamanho, 0, 1, estado, presente, presente_nuvem,
         drive, item_id, quick or ("Q-" + item_id), "", f"https://x/{item_id}",
         "ambos", cu.agora_iso(), cu.agora_iso()))
    con.commit()
    return int(cur.lastrowid)


def por_nome(con, nome: str):
    return con.execute("SELECT * FROM arquivo WHERE nome=? ORDER BY id", (nome,)).fetchall()


def montar(con):
    """O cenario inteiro. Devolve os ids que os testes vao conferir."""
    for drive, nome in ((DRIVE, "Programa"), (DOCS, "Documentos")):
        con.execute("INSERT INTO drive (drive_id, nome, tipo, no_escopo, atualizado_em) "
                    "VALUES (?,?,'documentLibrary',1,?)", (drive, nome, cu.agora_iso()))
    con.commit()

    # --- 1. novos, so no espelho -------------------------------------
    espelho(con, "n!p0", "3. Desenvolvimento\\RD103\\3. Notificações\\notificacao 001.pdf")
    espelho(con, "n!p1", "3. Desenvolvimento\\Administrativo\\Contratos\\contrato.pdf")
    espelho(con, "n!p2", "3. Desenvolvimento\\RD103\\Fotos\\relato semanal da obra.pdf")
    espelho(con, "n!p3", "3. Desenvolvimento\\Outra\\qualquer.pdf")
    espelho(con, "n!p4", "3. Desenvolvimento\\Backup\\velho.pdf")
    espelho(con, "n!dwg", "3. Desenvolvimento\\Outra\\planta.dwg")
    espelho(con, "n!tmp", "3. Desenvolvimento\\Outra\\~$rascunho.docx")
    # outro drive, sem regra no mapa: a biblioteca vira o nome do drive
    espelho(con, "n!doc", "relatorio\\extrato.csv", drive=DOCS)

    # --- 2. novo cujo conteudo JA foi extraido ------------------------
    id_velho = catalogo(con, "v!repr", f"{BIB}\\Outra\\original.pdf",
                        estado="COMPLETED", tamanho=4242, quick="HASH-GEMEO")
    espelho(con, "v!repr", f"3. Desenvolvimento\\Outra\\original.pdf", tamanho=4242,
            quick="")
    con.execute("UPDATE item_nuvem SET quick_xor='HASH-GEMEO' WHERE item_id='v!repr'")
    con.execute(
        "INSERT INTO conteudo (quick_xor, tamanho_bytes, evidencia, representante_id, "
        "estado, extrator, ocorrencias, primeira_vez_em) "
        "VALUES ('HASH-GEMEO', 4242, 'FORTE', ?, 'EXTRAIDO', 'pdf', 1, ?)",
        (id_velho, cu.agora_iso()))
    con.execute("UPDATE arquivo SET conteudo_id=(SELECT id FROM conteudo "
                "WHERE quick_xor='HASH-GEMEO') WHERE id=?", (id_velho,))
    con.execute("INSERT INTO extracao (arquivo_id, extrator, versao, status, texto_path, "
                "bytes_texto, trechos, executado_em) VALUES (?,?,?,?,?,?,?,?)",
                (id_velho, "pdf", "1", "ok", "derivados/texto/x.gz", 10, 1, cu.agora_iso()))
    con.execute("INSERT INTO texto_fts (arquivo_id, caminho_rel, pagina, trecho_idx, texto) "
                "VALUES (?,?,0,0,'medicao da obra')",
                (id_velho, f"{BIB}\\Outra\\original.pdf"))
    con.commit()
    # a copia: item novo, mesmo hash e mesmo tamanho do que ja foi extraido
    espelho(con, "n!gemeo", "3. Desenvolvimento\\Outra\\copia.pdf", tamanho=4242,
            quick="")
    con.execute("UPDATE item_nuvem SET quick_xor='HASH-GEMEO' WHERE item_id='n!gemeo'")
    con.commit()

    # --- 3. ja conhecidos: nao podem ser tocados ----------------------
    id_parado = catalogo(con, "k!parado", f"{BIB}\\Outra\\parado.pdf")
    espelho(con, "k!parado", "3. Desenvolvimento\\Outra\\parado.pdf")
    # reaparecendo: o espelho tem, o catalogo marcou MISSING. E do 17, nao daqui.
    id_voltou = catalogo(con, "k!voltou", f"{BIB}\\Outra\\voltou.pdf", estado="MISSING",
                         presente=0, presente_nuvem=0)
    espelho(con, "k!voltou", "3. Desenvolvimento\\Outra\\voltou.pdf")
    # renomeado: mesmo item_id, nome diferente no espelho
    id_renomeou = catalogo(con, "k!renom", f"{BIB}\\Outra\\antigo.pdf")
    espelho(con, "k!renom", "3. Desenvolvimento\\Outra\\novissimo.pdf")
    # movido: mesmo item_id, caminho diferente no espelho
    id_moveu = catalogo(con, "k!moveu", f"{BIB}\\Outra\\andou.pdf")
    espelho(con, "k!moveu", "3. Desenvolvimento\\OUTRA PASTA\\andou.pdf")

    # --- 4. caminho reencarnado: apagado e subido de novo -------------
    id_encarnado = catalogo(con, "k!morto", f"{BIB}\\Outra\\reencarnado.pdf",
                            estado="MISSING", presente=0, presente_nuvem=0)
    espelho(con, "n!vivo", "3. Desenvolvimento\\Outra\\reencarnado.pdf")

    return {"velho": id_velho, "parado": id_parado, "voltou": id_voltou,
            "renomeou": id_renomeou, "moveu": id_moveu, "encarnado": id_encarnado}


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main() -> int:
    area = area_temporaria("cerebro_t07_")
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir(parents=True)
    mapa = escrever_mapa(area)
    config = escrever_config(area, raiz_falsa, mapa_drives=str(mapa))
    rodar("00_init_db.py", config)
    # O banco nasce no v2, onde `caminho_rel` ainda e UNIQUE. O v3 reconstroi a
    # tabela sem isso — caminho e atributo, nao identidade (`DEC-015`). E o que
    # o banco real tem, e o que o caso "caminho reencarnado" exige.
    rodar("03_migrar_v3.py", config, "--sem-backup")
    cfg = cu.carregar_config(config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    ids = montar(con)

    antes_linhas = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    antes_extracoes = con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0]
    antes_trechos = con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0]
    antes_conteudos = con.execute("SELECT COUNT(*) FROM conteudo").fetchone()[0]

    # ------------------------------------------------------------------
    print("\n--- 1. quem e novo, e quem so parece ---")
    regras = cu.importar_script("02_classificar").carregar_regras()
    ing = ingestao_mod.Ingestao(con, cfg, regras,
                                cu.abrir_execucao(con, "teste", "", {}))
    pendentes = ing.pendentes()
    identificados = sorted(linha["item_id"] for linha in pendentes)
    esperados = sorted(["n!p0", "n!p1", "n!p2", "n!p3", "n!p4", "n!dwg", "n!tmp",
                        "n!doc", "n!gemeo", "n!vivo"])
    checar(identificados == esperados, "achou exatamente os 10 itens novos",
           str(identificados))
    for conhecido in ("k!parado", "k!voltou", "k!renom", "k!moveu", "v!repr"):
        checar(conhecido not in identificados,
               f"{conhecido}: ja tem linha no catalogo, nao e novo")

    derivados = ing.preparar(pendentes)
    checar(len(derivados) == 10, "preparou 10 linhas", str(len(derivados)))
    checar(ing.contadores["conteudo_ja_extraido"] == 1,
           "contou 1 novo cujo conteudo ja foi extraido",
           str(ing.contadores["conteudo_ja_extraido"]))
    checar(ing.contadores["caminho_reaproveitado"] == 1,
           "contou 1 caminho que ja pertence a outra linha",
           str(ing.contadores["caminho_reaproveitado"]))
    checar(ing.contadores["sem_regra_de_biblioteca"] == 1,
           "contou 1 sem regra de biblioteca (outro drive)",
           str(ing.contadores["sem_regra_de_biblioteca"]))
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == antes_linhas,
           "preparar() nao gravou nada")

    # ------------------------------------------------------------------
    print("\n--- 2. simulacao pela linha de comando nao grava ---")
    r = rodar("19_ingerir_novos.py", config, "--quieto")
    checar(r.returncode == 0, "19_ingerir_novos simula e sai 0", r.stderr[-500:])
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == antes_linhas,
           "a simulacao nao inseriu uma linha sequer")
    checar(con.execute("SELECT COUNT(*) FROM pasta").fetchone()[0] == 0,
           "a simulacao nao mexeu em `pasta`")

    # ------------------------------------------------------------------
    print("\n--- 3. aplicando ---")
    r = rodar("19_ingerir_novos.py", config, "--aplicar", "--quieto")
    checar(r.returncode == 0, "19_ingerir_novos --aplicar sai 0", r.stderr[-500:])
    depois_linhas = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    checar(depois_linhas == antes_linhas + 10, "inseriu exatamente 10 linhas",
           f"{antes_linhas} -> {depois_linhas}")

    print("\n--- 4. o arquivo novo, campo a campo ---")
    novo = con.execute("SELECT * FROM arquivo WHERE item_id='n!p0'").fetchone()
    checar(novo is not None, "o novo entrou no catalogo")
    checar(novo["caminho_rel"] ==
           f"{BIB}\\RD103\\3. Notificações\\notificacao 001.pdf",
           "caminho derivado no estilo do catalogo", str(novo["caminho_rel"]))
    checar(novo["biblioteca"] == BIB, "biblioteca pelo mapa de drives",
           str(novo["biblioteca"]))
    checar(novo["pasta_rel"] == f"{BIB}\\RD103\\3. Notificações",
           "pasta_rel", str(novo["pasta_rel"]))
    checar(novo["nome"] == "notificacao 001.pdf" and novo["ext"] == ".pdf"
           and novo["categoria"] == "pdf", "nome, ext e categoria")
    checar(novo["nome_normalizado"] == cu.normalizar_nome("notificacao 001.pdf"),
           "nome normalizado", str(novo["nome_normalizado"]))
    checar(novo["quick_xor"] == "Q-n!p0" and novo["tamanho_bytes"] == 100,
           "hash do servidor e tamanho vieram do espelho")
    checar(novo["web_url"] == "https://x/n!p0" and novo["criado_por"] == "Fulano da Silva"
           and novo["modificado_por"] == "Beltrana Souza", "url e autoria")
    checar(novo["criado_em_nuvem"] == "2026-09-01T10:00:00Z"
           and novo["modificado_em_nuvem"] == "2026-09-02T11:00:00Z",
           "datas da nuvem")
    checar(novo["caminho_nuvem"] ==
           "3. Desenvolvimento\\RD103\\3. Notificações\\notificacao 001.pdf",
           "caminho_nuvem e o do espelho", str(novo["caminho_nuvem"]))
    checar(novo["presente"] == 1 and novo["presente_nuvem"] == 1
           and novo["origem"] == "graph", "presente, presente_nuvem e origem")
    checar(novo["drive_id"] == DRIVE and novo["item_id"] == "n!p0",
           "identidade canonica gravada")
    checar(con.execute("SELECT COUNT(*) FROM evento_arquivo WHERE arquivo_id=? "
                       "AND evento='ingerido_da_nuvem'", (novo["id"],)).fetchone()[0] == 1,
           "gravou o evento de ingestao")

    print("\n--- 5. classificacao P0..P4 ---")
    esperado_p = {"n!p0": 0, "n!p1": 1, "n!p2": 2, "n!p3": 3, "n!p4": 4}
    for item_id, prioridade in esperado_p.items():
        linha = con.execute("SELECT prioridade, caminho_rel FROM arquivo WHERE item_id=?",
                            (item_id,)).fetchone()
        checar(linha["prioridade"] == prioridade,
               f"P{prioridade}: {linha['caminho_rel'].split(chr(92))[-2]}",
               f"veio P{linha['prioridade']}")

    print("\n--- 6. estado inicial ---")
    checar(con.execute("SELECT estado FROM arquivo WHERE item_id='n!p0'").fetchone()[0]
           == "QUEUED", "tem extrator -> QUEUED")
    checar(con.execute("SELECT estado FROM arquivo WHERE item_id='n!dwg'").fetchone()[0]
           == "UNSUPPORTED", "sem extrator -> UNSUPPORTED (nao some do catalogo)")
    checar(con.execute("SELECT estado, temporario FROM arquivo WHERE item_id='n!tmp'"
                       ).fetchone()[0] == "SKIPPED", "temporario ~$ -> SKIPPED")
    checar(con.execute("SELECT temporario FROM arquivo WHERE item_id='n!tmp'"
                       ).fetchone()[0] == 1, "temporario marcado")
    checar(con.execute("SELECT COUNT(*) FROM v_fila_nuvem WHERE item_id IN "
                       "('n!dwg','n!tmp')").fetchone()[0] == 0,
           "nem UNSUPPORTED nem SKIPPED entram na fila da nuvem")

    print("\n--- 7. pasta nova ---")
    p = con.execute("SELECT * FROM pasta WHERE caminho_rel=?",
                    (f"{BIB}\\RD103\\3. Notificações",)).fetchone()
    checar(p is not None, "a pasta nova ganhou linha em `pasta`")
    checar(p and p["prioridade"] == 0 and p["obra"] == "RD103"
           and p["categoria_doc"] == "notificacoes",
           "com obra, categoria e prioridade", str(dict(p)) if p else "")
    checar(p and p["motivo"], "e com o motivo gravado (auditavel)")
    checar(p and p["arquivos"] == 1 and p["arquivos_suportados"] == 1,
           "contagem de arquivos diretos", str(dict(p)) if p else "")
    ancestral = con.execute("SELECT * FROM pasta WHERE caminho_rel=?",
                            (f"{BIB}\\RD103",)).fetchone()
    checar(ancestral is not None and ancestral["obra"] == "RD103",
           "o ancestral tambem foi classificado")
    checar(con.execute("SELECT COUNT(*) FROM pasta WHERE caminho_rel=?",
                       (f"{BIB}\\Backup",)).fetchone()[0] == 1,
           "a pasta de descarte tambem entrou")

    print("\n--- 8. duplicata: entra na fila, o worker e quem cobre ---")
    gemeo = con.execute("SELECT * FROM arquivo WHERE item_id='n!gemeo'").fetchone()
    checar(gemeo["estado"] == "QUEUED", "a copia entra como QUEUED, nao DUPLICATE",
           gemeo["estado"])
    checar(gemeo["quick_xor"] == "HASH-GEMEO" and gemeo["tamanho_bytes"] == 4242,
           "com o hash e o tamanho que permitem cobri-la sem baixar")
    checar(gemeo["conteudo_id"] is None and gemeo["duplicata_de"] is None,
           "a ingestao NAO decide duplicata (DEC-026/DEC-027)")
    conteudo = con.execute("SELECT * FROM conteudo WHERE quick_xor='HASH-GEMEO'"
                           ).fetchone()
    checar(conteudo["ocorrencias"] == 1 and conteudo["representante_id"] == ids["velho"],
           "a linha de `conteudo` nao foi tocada")
    checar(con.execute("SELECT COUNT(*) FROM v_fila_nuvem WHERE item_id='n!gemeo'"
                       ).fetchone()[0] == 1, "a copia esta na fila da nuvem")

    print("\n--- 9. quem ja era conhecido nao foi tocado ---")
    checar(con.execute("SELECT estado FROM arquivo WHERE item_id='k!parado'").fetchone()[0]
           == "COMPLETED", "COMPLETED continua COMPLETED (nao reprocessa)")
    checar(con.execute("SELECT COUNT(*) FROM arquivo WHERE item_id='k!parado'"
                       ).fetchone()[0] == 1, "e nao ganhou linha duplicada")
    checar(con.execute("SELECT estado FROM arquivo WHERE item_id='k!voltou'").fetchone()[0]
           == "MISSING", "o reaparecido continua com o 17_aplicar_delta, nao aqui")
    checar(len(por_nome(con, "antigo.pdf")) == 1
           and con.execute("SELECT COUNT(*) FROM arquivo WHERE item_id='k!renom'"
                           ).fetchone()[0] == 1,
           "renomeado: uma linha so, identidade preservada")
    checar(con.execute("SELECT COUNT(*) FROM arquivo WHERE item_id='k!moveu'"
                       ).fetchone()[0] == 1,
           "movido: uma linha so, identidade preservada")
    checar(con.execute("SELECT COUNT(*) FROM evento_arquivo WHERE arquivo_id IN (?,?,?,?)",
                       (ids["parado"], ids["voltou"], ids["renomeou"], ids["moveu"])
                       ).fetchone()[0] == 0, "e nenhum deles ganhou evento a toa")

    print("\n--- 10. caminho reencarnado: item novo, linha velha intacta ---")
    linhas = con.execute("SELECT id, item_id, estado, presente FROM arquivo "
                         "WHERE caminho_rel=? ORDER BY id",
                         (f"{BIB}\\Outra\\reencarnado.pdf",)).fetchall()
    checar(len(linhas) == 2, "duas linhas: a que morreu e a que nasceu",
           str(len(linhas)))
    checar(linhas[0]["item_id"] == "k!morto" and linhas[0]["estado"] == "MISSING"
           and linhas[0]["presente"] == 0, "a velha continua MISSING, sem perder nada")
    checar(linhas[1]["item_id"] == "n!vivo" and linhas[1]["estado"] == "QUEUED",
           "a nova entrou na fila com o item_id novo")

    print("\n--- 11. o outro drive, sem regra no mapa ---")
    doc = con.execute("SELECT * FROM arquivo WHERE item_id='n!doc'").fetchone()
    checar(doc["caminho_rel"] == "Documentos\\relatorio\\extrato.csv",
           "sem regra, a biblioteca vira o nome do drive", str(doc["caminho_rel"]))
    checar(doc["biblioteca"] == "Documentos", "biblioteca = nome do drive")

    print("\n--- 12. nada do trabalho anterior se perdeu ---")
    checar(con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0] == antes_extracoes,
           "extracoes preservadas")
    checar(con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0] == antes_trechos,
           "trechos preservados")
    checar(con.execute("SELECT COUNT(*) FROM conteudo").fetchone()[0] == antes_conteudos,
           "conteudos preservados")

    print("\n--- 13. rodar de novo nao muda mais nada (idempotente) ---")
    r = rodar("19_ingerir_novos.py", config, "--aplicar", "--quieto")
    checar(r.returncode == 0, "segunda passada sai 0", r.stderr[-500:])
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == depois_linhas,
           "segunda passada nao inseriu nada")
    checar(con.execute("SELECT COUNT(*) FROM evento_arquivo "
                       "WHERE evento='ingerido_da_nuvem'").fetchone()[0] == 10,
           "e nao gravou evento de novo")
    ing2 = ingestao_mod.Ingestao(con, cfg, regras,
                                 cu.abrir_execucao(con, "teste", "", {}))
    checar(len(ing2.pendentes()) == 0, "nao sobrou nenhum pendente")

    print("\n--- 14. 02_classificar continua de acordo ---")
    r = rodar("02_classificar.py", config)
    checar(r.returncode == 0, "02_classificar roda sobre o catalogo ingerido",
           r.stderr[-500:])
    divergentes = con.execute(
        "SELECT COUNT(*) FROM arquivo a JOIN pasta p ON p.caminho_rel=a.pasta_rel "
        "WHERE a.presente=1 AND a.prioridade <> p.prioridade").fetchone()[0]
    checar(divergentes == 0,
           "a prioridade que a ingestao deu e a mesma que o 02 daria",
           str(divergentes))

    print("\n--- 15. o sincronizador one-shot (sem falar com a nuvem) ---")
    linhas_antes = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    r = rodar("18_sincronizar.py", config, "--simular", "--quieto")
    checar(r.returncode == 0, "18_sincronizar --simular sai 0", r.stderr[-500:])
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == linhas_antes,
           "a simulacao do sincronizador nao gravou nada")
    r = rodar("18_sincronizar.py", config, "--sem-delta", "--aplicar", "--quieto")
    checar(r.returncode == 0, "18_sincronizar --sem-delta --aplicar sai 0",
           r.stderr[-500:])
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == linhas_antes,
           "rodar o sincronizador de novo nao muda o catalogo (idempotente)")
    relatorios = sorted(Path(cfg["_logs_path"]).glob("sincronizacao_*.json"))
    checar(len(relatorios) >= 2, "cada rodada deixou um relatorio em logs/",
           str(len(relatorios)))
    dados = json.loads(relatorios[-1].read_text(encoding="utf-8"))
    checar(dados["codigo_saida"] == 0 and dados["status"] == "concluida",
           "o relatorio registra o resultado da rodada", str(dados.get("status")))
    checar("antes" in dados and "depois" in dados and dados["worker"],
           "e o retrato do banco e o estado do worker")

    con.close()
    shutil.rmtree(area, ignore_errors=True)
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
