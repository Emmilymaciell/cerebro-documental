"""
test_worker_v5 — o worker de producao alimentando a busca v5 (indice por conteudo).

    python tests\\test_worker_v5.py

Banco criado do zero pela receita do repositorio (`00_init_db.py`) e migrado
pelo `05_migrar_v5.py --aplicar` de verdade. O worker e o de producao, com o
mesmo Graph falso do `test_worker_nuvem` (so sabe baixar).

Prova, objetivamente:
  v5 ativa e detectada · 1 conteudo indexado 1 vez · as N copias recuperaveis
  pela busca, sem novo download e sem novo indice · `texto_fts` (v4) nao e mais
  alimentado · trechos com pagina/segmento e rastreaveis ate as ocorrencias ·
  contagem de trechos igual em `conteudo`, `extracao` e perfil JSON · item que
  muda de conteudo e reindexado sem acumular · subida recusada (codigo 5) se a
  versao carimbada no banco diverge do codigo.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import area_temporaria, checar, escrever_config, placar_final, rodar  # noqa: E402

import busca  # noqa: E402
import cerebro_util as cu  # noqa: E402
import quickxor  # noqa: E402
import worker as worker_mod  # noqa: E402
from test_worker_nuvem import (GraphFalso, contar, inserir, linha,  # noqa: E402
                               montar_worker, processar_id)


def main() -> int:
    area = area_temporaria("cerebro_v5w_")
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir()
    (area / "tmp").mkdir()
    config = escrever_config(area, raiz_falsa, fonte="graph")

    print("\n--- 1. banco do zero + migracao v5 pelo script real ---")
    r = rodar("00_init_db.py", config)
    checar(r.returncode == 0, "00_init_db cria o banco", r.stderr[-800:])
    r = rodar("05_migrar_v5.py", config, "--aplicar", "--sem-backup")
    checar(r.returncode == 0, "05_migrar_v5 --aplicar sai 0", (r.stdout + r.stderr)[-1500:])

    cfg = cu.carregar_config(config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    checar(busca.v5_ativo(con), "v5 ativa no banco migrado")
    checar(busca.conferir_versoes(con) == [], "versoes carimbadas = codigo",
           str(busca.conferir_versoes(con)))

    graph = GraphFalso()
    w = montar_worker(cfg, con, area, graph)
    checar(w.busca_v5 is True, "o worker detecta a v5 na subida")

    # Duas "paginas" separadas: o extrator de texto devolve uma so, mas o
    # caminho e o mesmo para qualquer formato (paginas_texto do resultado).
    corpo = ("RELATORIO DE ENSAIO DE DEFLECTOMETRIA FWD na rodovia RD-103.\n"
             "Bacia de deformacao medida a cada 40 metros no trecho km 12 ao km 18.\n"
             * 60).encode("utf-8")
    quick = quickxor.de_bytes(corpo)

    print("\n--- 2. o representante indexa UMA vez, por conteudo ---")
    id_a, item_a = inserir(con, "ensaio_fwd.txt", len(corpo), quick)
    graph.roteirizar(item_a, corpo)
    desfecho, _ = processar_id(w, id_a)
    a = linha(con, id_a)
    checar(desfecho == "ok" and a["estado"] == "COMPLETED", "representante COMPLETED",
           f"{desfecho}/{a['estado']}")
    cid = a["conteudo_id"]
    checar(cid is not None, "ocorrencia vinculada a um conteudo")
    n_trechos = contar(con, "trecho", "conteudo_id=?", cid)
    checar(n_trechos > 1, f"o conteudo virou trechos v5 ({n_trechos})")
    checar(contar(con, "indice_conteudo", "conteudo_id=?", cid) == 1,
           "1 linha em indice_conteudo")
    checar(contar(con, "texto_fts") == 0, "texto_fts (v4) NAO foi alimentado com v5 ativa",
           str(contar(con, "texto_fts")))
    c = con.execute("SELECT trechos, perfil_path FROM conteudo WHERE id=?", (cid,)).fetchone()
    ext = con.execute("SELECT trechos FROM extracao WHERE arquivo_id=?", (id_a,)).fetchone()
    perfil = json.loads(Path(c["perfil_path"]).read_text(encoding="utf-8"))
    checar(c["trechos"] == n_trechos and ext["trechos"] == n_trechos
           and perfil["trechos_indexados"] == n_trechos,
           "contagem de trechos igual em conteudo, extracao e perfil",
           f"{c['trechos']}/{ext['trechos']}/{perfil['trechos_indexados']}/{n_trechos}")
    eventos = [json.loads(e["detalhe"]) for e in con.execute(
        "SELECT detalhe FROM evento_arquivo WHERE arquivo_id=? AND evento='processado'",
        (id_a,))]
    checar(eventos and eventos[-1].get("trechos") == n_trechos,
           "evento 'processado' registra os trechos v5", str(eventos[-1:]))

    print("\n--- 3. as copias: recuperaveis, sem download e sem reindexar ---")
    id_b, _ = inserir(con, "ensaio_fwd (copia).txt", len(corpo), quick, prioridade=2)
    id_c, _ = inserir(con, "ENSAIO FWD REV.txt", len(corpo), quick, prioridade=3)
    baixados = len(graph.baixados)
    processar_id(w, id_b)
    processar_id(w, id_c)
    checar(len(graph.baixados) == baixados, "nenhum download para as copias")
    checar(contar(con, "trecho", "conteudo_id=?", cid) == n_trechos,
           "o indice nao cresceu com as copias")
    resultados = busca.buscar(con, busca.consulta_de_termos("deflectometria bacia"))
    lugares = {r["arquivo_id"] for r in resultados}
    checar({id_a, id_b, id_c} <= lugares,
           "a busca devolve as 3 ocorrencias fisicas do conteudo", str(sorted(lugares)))
    checar(len({r["conteudo_id"] for r in resultados}) == 1,
           "e um conteudo so (1 indexado, N recuperaveis)")

    print("\n--- 4. rastreabilidade trecho -> conteudo -> ocorrencias ---")
    tid = con.execute("SELECT id FROM trecho WHERE conteudo_id=? ORDER BY ordinal LIMIT 1",
                      (cid,)).fetchone()[0]
    rast = busca.rastrear(con, tid)
    checar(rast is not None and rast["conteudo_id"] == cid, "rastrear acha o conteudo")
    checar({o["arquivo_id"] for o in rast["ocorrencias"]} == {id_a, id_b, id_c},
           "rastrear lista as 3 ocorrencias", str(rast["ocorrencias"]))
    checar(rast["segmento_tipo"] is not None and rast["versao_chunker"] == busca.POLITICA.versao,
           "trecho carrega segmento e versao do corte",
           f"{rast['segmento_tipo']}/{rast['versao_chunker']}")

    print("\n--- 5. reprocessar o representante substitui, nao acumula ---")
    con.execute("UPDATE arquivo SET estado='QUEUED' WHERE id=?", (id_a,))
    con.commit()
    processar_id(w, id_a)
    checar(contar(con, "trecho", "conteudo_id=?", cid) == n_trechos,
           "mesmo conteudo reprocessado: mesmo numero de trechos",
           str(contar(con, "trecho", "conteudo_id=?", cid)))
    checar(contar(con, "trecho") == n_trechos, "nenhum trecho duplicado no indice",
           str(contar(con, "trecho")))
    checar(busca.integridade_fts(con), "FTS5 integrity-check ok")
    con.close()

    print("\n--- 6. versao carimbada divergente: o worker recusa subir ---")
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    con.execute("UPDATE meta SET valor='chunk/0.0-teste' WHERE chave='busca.versao_chunker'")
    con.commit()
    con.close()
    args = worker_mod.montar_parser().parse_args(["--config", str(config), "--quieto"])
    codigo, _ = worker_mod.executar(args)
    checar(codigo == 5, "executar() devolve 5 com versao divergente", str(codigo))
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    checar(contar(con, "execucao", "etapa='worker' AND argumentos NOT LIKE '%teste%'") == 0,
           "nenhuma sessao de worker foi aberta")
    con.close()

    return placar_final()


if __name__ == "__main__":
    sys.exit(main())
