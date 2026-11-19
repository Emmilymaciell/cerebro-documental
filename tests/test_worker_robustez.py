"""
test_worker_robustez — o worker sem ninguem olhando: o que o derruba, o que o
para, e o que ele deixa de registrar.

    python tests\\test_worker_robustez.py

Banco criado do zero pela receita do repositorio (`00_init_db.py`) e migrado
pelo `05_migrar_v5.py --aplicar` (busca v5 ativa, como em producao). O worker,
a `FonteGraph` e o laco `rodar()` sao os de producao; so o cliente HTTP e o
`GraphFalso` do `test_worker_nuvem` (so sabe baixar).

Prova, objetivamente:
  A. B3 — pilula envenenada: item que derruba o worker N vezes (padrao 2,
     `worker.max_quedas_por_item`) vai para ERROR_FINAL com motivo e evento,
     e a fila anda; o contador zera quando o item e processado; o limite e
     configuravel.
  B. B4 — disjuntor de credencial: 401/`falha_autenticacao` em serie nao gasta
     tentativa de documento, e o worker encerra em AGUARDANDO_CREDENCIAL
     (codigo 6) depois de 3 seguidas, visivel no `cerebro.py status`; 403
     isolado (credencial ja comprovada na sessao) gasta tentativa do item e
     nao para o worker; 403 antes de comprovar nao trava a fila.
  C. R4 — quickXor sempre: item sem hash no catalogo sai com `quick_xor`,
     `conteudo_id` e trechos na busca v5; a copia sem hash vira DUPLICATE.
  D. R9 — heartbeat durante o download, no maximo a cada `heartbeat_s`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import area_temporaria, checar, escrever_config, placar_final, rodar  # noqa: E402

import busca  # noqa: E402
import cerebro_util as cu  # noqa: E402
import fonte as fonte_mod  # noqa: E402
import graph_ro  # noqa: E402
import operacao as op  # noqa: E402
import quickxor  # noqa: E402
import worker as worker_mod  # noqa: E402
from test_worker_nuvem import (GraphFalso, contar, inserir, linha,  # noqa: E402
                               montar_worker, processar_id)


def corpo(rotulo: str, n: int = 30) -> bytes:
    # Linhas numeradas: o quickXorHash e um XOR com deslocamento, e texto
    # periodico (a mesma linha N vezes) se cancela — dois corpos diferentes do
    # mesmo tamanho dariam o mesmo hash e o teste mediria a deduplicacao.
    return "".join(f"{rotulo} linha {i:03d} - relatorio tecnico de pavimentacao, "
                   f"trecho RD-201.\n" for i in range(n)).encode()


def texto_seguro(texto: str) -> str:
    return texto.encode("ascii", "replace").decode("ascii")


def eventos(con, arquivo_id, evento):
    return contar(con, "evento_arquivo", "arquivo_id=? AND evento=?", arquivo_id, evento)


def cair_no_meio(con, arquivo_id, estado="EXTRACTING"):
    """O processo morreu com o item no meio do caminho (crash nativo, OOM,
    janela fechada): e exatamente o rastro que fica no banco."""
    con.execute("UPDATE arquivo SET estado=? WHERE id=?", (estado, arquivo_id))
    con.commit()


# ======================================================================
# A. B3 — pilula envenenada
# ======================================================================
def parte_pilula(cfg, con, area) -> None:
    graph = GraphFalso()

    print("\n--- A1. 1a queda: o item volta para a fila (como sempre) ---")
    b_p, b_q = corpo("PILULA"), corpo("SEGUINTE")
    id_p, item_p = inserir(con, "pilula.txt", len(b_p), quickxor.de_bytes(b_p), prioridade=0)
    id_q, item_q = inserir(con, "seguinte.txt", len(b_q), quickxor.de_bytes(b_q),
                           prioridade=1)
    graph.roteirizar(item_p, b_p)
    graph.roteirizar(item_q, b_q)
    cair_no_meio(con, id_p, "EXTRACTING")
    w1 = montar_worker(cfg, con, area, graph)
    w1.recuperar_orfaos()
    p = linha(con, id_p)
    checar(p["estado"] == "QUEUED", "1a queda: QUEUED de novo", p["estado"])
    checar(eventos(con, id_p, "retomado_apos_queda") == 1, "evento retomado_apos_queda")
    w1.args.ids = [id_p, id_q]
    checar(w1.proximo()["id"] == id_p,
           "o mesmo item e o primeiro da fila de novo (e a pilula)")

    print("\n--- A2. 2a queda no MESMO item: ERROR_FINAL, e a fila anda ---")
    cair_no_meio(con, id_p, "HYDRATING")
    w2 = montar_worker(cfg, con, area, graph)
    w2.recuperar_orfaos()
    p = linha(con, id_p)
    checar(p["estado"] == "ERROR_FINAL", "2a queda: ERROR_FINAL", p["estado"])
    checar("derrubou o worker 2 vezes" in (p["ultimo_erro"] or ""),
           "motivo explicito: 'derrubou o worker 2 vezes'", str(p["ultimo_erro"]))
    checar(p["proxima_tentativa_em"] is None, "sem reagendamento")
    checar(eventos(con, id_p, "quarentena_apos_queda") == 1,
           "evento quarentena_apos_queda no historico")
    checar(eventos(con, id_p, "retomado_apos_queda") == 1,
           "nao foi 'retomado' de novo — foi isolado")
    checar(contar(con, "arquivo", "id=?", id_p) == 1, "a linha continua no catalogo")
    checar(op.categoria_erro(p["ultimo_erro"]) == "derrubou o worker",
           "categoria propria no relatorio de erros", op.categoria_erro(p["ultimo_erro"]))
    w2.args.ids = [id_p, id_q]
    proximo = w2.proximo()
    checar(proximo is not None and proximo["id"] == id_q,
           "o proximo da fila e o item seguinte", str(proximo and proximo["id"]))
    desfecho = w2.processar(proximo) if proximo is not None else None
    checar(desfecho == "ok" and linha(con, id_q)["estado"] == "COMPLETED",
           "e ele e processado", f"{desfecho}/{linha(con, id_q)['estado']}")

    print("\n--- A3. processar zera o contador de quedas ---")
    b_r = corpo("REINCIDENTE")
    id_r, item_r = inserir(con, "reincidente.txt", len(b_r), quickxor.de_bytes(b_r))
    graph.roteirizar(item_r, b_r)
    cair_no_meio(con, id_r)
    w3 = montar_worker(cfg, con, area, graph)
    w3.recuperar_orfaos()
    processar_id(w3, id_r)
    checar(linha(con, id_r)["estado"] == "COMPLETED", "queda 1, depois processado")
    con.execute("UPDATE arquivo SET estado='CHANGED' WHERE id=?", (id_r,))
    con.commit()
    cair_no_meio(con, id_r)
    w4 = montar_worker(cfg, con, area, graph)
    w4.recuperar_orfaos()
    checar(linha(con, id_r)["estado"] == "QUEUED",
           "nova queda depois de um processamento conta do zero",
           linha(con, id_r)["estado"])

    print("\n--- A4. limite configuravel: worker.max_quedas_por_item = 3 ---")
    b_s = corpo("TRES QUEDAS")
    id_s, _ = inserir(con, "tres_quedas.txt", len(b_s), quickxor.de_bytes(b_s))
    cfg3 = dict(cfg)
    cfg3["worker"] = {**(cfg.get("worker") or {}), "max_quedas_por_item": 3}
    for n in (1, 2):
        cair_no_meio(con, id_s)
        montar_worker(cfg3, con, area, graph).recuperar_orfaos()
        checar(linha(con, id_s)["estado"] == "QUEUED", f"limite 3: queda {n} volta",
               linha(con, id_s)["estado"])
    cair_no_meio(con, id_s)
    montar_worker(cfg3, con, area, graph).recuperar_orfaos()
    s = linha(con, id_s)
    checar(s["estado"] == "ERROR_FINAL" and "3 vezes" in (s["ultimo_erro"] or ""),
           "limite 3: a 3a queda isola", f"{s['estado']}/{s['ultimo_erro']}")

    print("\n--- A5. pelo laco de producao: a pilula nao trava a sessao ---")
    b_u, b_v = corpo("PILULA DOIS"), corpo("FILA ANDA")
    id_u, item_u = inserir(con, "pilula_2.txt", len(b_u), quickxor.de_bytes(b_u))
    id_v, item_v = inserir(con, "fila_anda.txt", len(b_v), quickxor.de_bytes(b_v),
                           prioridade=1)
    graph.roteirizar(item_u, b_u)
    graph.roteirizar(item_v, b_v)
    cair_no_meio(con, id_u)
    montar_worker(cfg, con, area, graph).recuperar_orfaos()      # 1a queda
    cair_no_meio(con, id_u)                                      # 2a queda
    w5 = montar_worker(cfg, con, area, graph, ids=[id_u, id_v], ignorar_janela=True)
    baixados = len(graph.baixados)
    estado = w5.rodar()
    checar(estado == "FILA_VAZIA", "a sessao vai ate o fim", estado)
    checar(linha(con, id_u)["estado"] == "ERROR_FINAL", "a pilula ficou isolada",
           linha(con, id_u)["estado"])
    checar(linha(con, id_v)["estado"] == "COMPLETED", "o item seguinte foi processado",
           linha(con, id_v)["estado"])
    checar(graph.baixados[baixados:] == [item_v], "a pilula nem foi baixada de novo",
           str(graph.baixados[baixados:]))


# ======================================================================
# B. B4 — disjuntor de credencial
# ======================================================================
ERRO_401 = graph_ro.ErroGraph(401, "InvalidAuthenticationToken", "Access token has expired")
ERRO_SEGREDO = graph_ro.ErroGraph(401, "falha_autenticacao",
                                  "AADSTS7000222: The provided client secret keys are expired")
ERRO_403 = graph_ro.ErroGraph(403, "accessDenied", "Access denied")


def parte_credencial(cfg, con, area, config) -> None:
    graph = GraphFalso()

    print("\n--- B1. 401 em serie: nenhuma tentativa gasta, worker para ---")
    ids = []
    for n in range(6):
        b = corpo(f"CRED {n}")
        ident, item = inserir(con, f"cred_{n}.txt", len(b), quickxor.de_bytes(b),
                              prioridade=n)
        graph.roteirizar(item, ERRO_SEGREDO if n % 2 else ERRO_401)
        ids.append(ident)
    w = montar_worker(cfg, con, area, graph, ids=ids, ignorar_janela=True)
    antes = len(graph.baixados)
    estado = w.rodar()
    checar(estado == "AGUARDANDO_CREDENCIAL", "encerra em AGUARDANDO_CREDENCIAL", estado)
    checar(len(graph.baixados) - antes == 3,
           "parou na 3a falha seguida (worker.falhas_credencial_para_parar)",
           str(len(graph.baixados) - antes))
    linhas = [linha(con, i) for i in ids]
    checar(all(x["tentativas"] == 0 for x in linhas), "tentativas intactas em todos",
           str([x["tentativas"] for x in linhas]))
    checar(all(x["estado"] == "QUEUED" for x in linhas),
           "todos no estado de antes (QUEUED) — nenhum ERROR_*",
           str([x["estado"] for x in linhas]))
    checar(contar(con, "arquivo", f"id IN ({','.join('?' * len(ids))}) AND estado LIKE "
                  "'ERROR%'", *ids) == 0, "nenhum ERROR_FINAL nem ERROR_RETRYABLE")
    checar(sum(eventos(con, i, "falha_credencial") for i in ids) == 3,
           "3 eventos falha_credencial")
    checar(sum(eventos(con, i, "falha") for i in ids) == 0,
           "nenhum evento 'falha' (nao e falha do documento)")
    controle = cu.ler_controle(con)
    checar(controle["estado"] == "AGUARDANDO_CREDENCIAL", "controle.estado",
           controle["estado"])
    checar("credencial" in (controle["motivo"] or "").lower(), "motivo legivel",
           str(controle["motivo"]))
    ultimo = con.execute("SELECT estado_worker FROM checkpoint ORDER BY id DESC LIMIT 1"
                         ).fetchone()
    checar(ultimo["estado_worker"] == "AGUARDANDO_CREDENCIAL", "checkpoint registra",
           ultimo["estado_worker"])
    checar("AGUARDANDO_CREDENCIAL" in op.ESTADOS_WORKER_SEGUROS,
           "e um estado de worker seguro para desligar")

    print("\n--- B2. executar() devolve o codigo 6; status mostra o motivo ---")
    original = worker_mod.fonte.criar
    worker_mod.fonte.criar = lambda nome, c, **_: fonte_mod.FonteGraph(
        c, graph=graph, pasta_tmp=area / "tmp")
    try:
        con.execute("UPDATE arquivo SET proxima_tentativa_em=NULL WHERE id IN "
                    f"({','.join('?' * len(ids))})", ids)
        con.commit()
        args = worker_mod.montar_parser().parse_args(
            ["--config", str(config), "--quieto", "--uma-vez", "--ignorar-janela",
             "--ids", ",".join(str(i) for i in ids)])
        codigo, estado = worker_mod.executar(args)
    finally:
        worker_mod.fonte.criar = original
    checar(codigo == 6 and estado == "AGUARDANDO_CREDENCIAL",
           "executar() -> (6, AGUARDANDO_CREDENCIAL)", f"{codigo}/{estado}")
    checar(all(linha(con, i)["tentativas"] == 0 for i in ids),
           "segunda sessao tambem nao gastou tentativa")
    r = rodar("cerebro.py", config, "status")
    checar(r.returncode == 0 and "AGUARDANDO_CREDENCIAL" in r.stdout,
           "cerebro.py status mostra AGUARDANDO_CREDENCIAL",
           texto_seguro(r.stdout[-400:] + r.stderr[-400:]))
    checar("credencial" in r.stdout.split("AGUARDANDO_CREDENCIAL", 1)[-1].lower(),
           "e o motivo/acao", texto_seguro(r.stdout[-600:]))
    # Devolve os 6 para fora do caminho das proximas partes.
    con.execute(f"UPDATE arquivo SET estado='MISSING', presente=0 WHERE id IN "
                f"({','.join('?' * len(ids))})", ids)
    con.commit()

    print("\n--- B3. 403 isolado, com a credencial ja comprovada: gasta do item ---")
    b1, b2, b3 = corpo("OK UM"), corpo("PROIBIDO"), corpo("OK DOIS")
    id1, it1 = inserir(con, "ok_um.txt", len(b1), quickxor.de_bytes(b1), prioridade=0)
    id2, it2 = inserir(con, "proibido.txt", len(b2), quickxor.de_bytes(b2), prioridade=1)
    id3, it3 = inserir(con, "ok_dois.txt", len(b3), quickxor.de_bytes(b3), prioridade=2)
    graph.roteirizar(it1, b1)
    graph.roteirizar(it2, ERRO_403)
    graph.roteirizar(it3, b3)
    w = montar_worker(cfg, con, area, graph, ids=[id1, id2, id3], ignorar_janela=True)
    estado = w.rodar()
    checar(estado == "FILA_VAZIA", "o worker NAO para por um 403 isolado", estado)
    x2 = linha(con, id2)
    checar(x2["estado"] == "ERROR_RETRYABLE" and x2["tentativas"] == 1,
           "o 403 isolado gasta 1 tentativa do item (termina ERROR_FINAL na 4a)",
           f"{x2['estado']}/{x2['tentativas']}")
    checar(linha(con, id1)["estado"] == "COMPLETED" and linha(con, id3)["estado"]
           == "COMPLETED", "os vizinhos foram processados")
    checar(w.falhas_credencial_seguidas == 0, "um sucesso zera o disjuntor",
           str(w.falhas_credencial_seguidas))

    print("\n--- B4. 403 antes de comprovar a credencial: nao trava a fila ---")
    b4, b5 = corpo("PROIBIDO PRIMEIRO"), corpo("DEPOIS DELE")
    id4, it4 = inserir(con, "proibido_primeiro.txt", len(b4), quickxor.de_bytes(b4),
                       prioridade=0)
    id5, it5 = inserir(con, "depois_dele.txt", len(b5), quickxor.de_bytes(b5), prioridade=1)
    graph.roteirizar(it4, ERRO_403)
    graph.roteirizar(it5, b5)
    w = montar_worker(cfg, con, area, graph, ids=[id4, id5], ignorar_janela=True)
    estado = w.rodar()
    x4 = linha(con, id4)
    checar(estado == "FILA_VAZIA", "a sessao segue", estado)
    checar(x4["estado"] == "QUEUED" and x4["tentativas"] == 0,
           "403 sem prova de credencial: volta como estava, sem gastar tentativa",
           f"{x4['estado']}/{x4['tentativas']}")
    checar(x4["proxima_tentativa_em"] is not None,
           "com espera propria — o mesmo item nao e pego de novo na hora")
    checar("403" in (x4["ultimo_erro"] or ""), "ultimo_erro registra o 403",
           str(x4["ultimo_erro"]))
    checar(linha(con, id5)["estado"] == "COMPLETED", "o seguinte foi processado")


# ======================================================================
# C. R4 — quickXor sempre
# ======================================================================
def parte_quickxor(cfg, con, area) -> None:
    graph = GraphFalso()
    w = montar_worker(cfg, con, area, graph)

    print("\n--- C1. item sem quick_xor no catalogo ---")
    b = "".join(f"LAUDO DE SONDAGEM SPT furo SP-{i:02d} cota 812,40 nivel dagua 3,2 m.\n"
                for i in range(40)).encode()
    esperado = quickxor.de_bytes(b)
    id_a, it_a = inserir(con, "laudo_sem_hash.txt", len(b), None)
    graph.roteirizar(it_a, b)
    desfecho, _ = processar_id(w, id_a)
    a = linha(con, id_a)
    checar(desfecho == "ok" and a["estado"] == "COMPLETED", "processa normalmente",
           f"{desfecho}/{a['estado']}")
    checar(a["quick_xor"] == esperado, "quick_xor calculado do baixado e gravado",
           str(a["quick_xor"]))
    checar(a["conteudo_id"] is not None, "tem conteudo_id")
    c = con.execute("SELECT * FROM conteudo WHERE id=?", (a["conteudo_id"],)).fetchone()
    checar(c is not None and c["quick_xor"] == esperado, "conteudo pelo hash calculado")
    achados = {r["arquivo_id"] for r in busca.buscar(
        con, busca.consulta_de_termos("sondagem SPT"))}
    checar(id_a in achados, "visivel na busca v5", str(achados))

    print("\n--- C2. a copia, tambem sem hash, vira DUPLICATE ---")
    id_b, it_b = inserir(con, "laudo_sem_hash (copia).txt", len(b), None, prioridade=1)
    graph.roteirizar(it_b, b)
    desfecho, _ = processar_id(w, id_b)
    x = linha(con, id_b)
    checar(desfecho == "duplicata" and x["estado"] == "DUPLICATE"
           and x["conteudo_id"] == a["conteudo_id"],
           "coberta pelo mesmo conteudo, sem extrair de novo",
           f"{desfecho}/{x['estado']}/{x['conteudo_id']}")
    checar(x["quick_xor"] == esperado, "e com o hash gravado")

    print("\n--- C3. com hash no catalogo, a validacao continua valendo ---")
    id_c, it_c = inserir(con, "hash_errado.txt", len(b), "HASHQUENAOBATE==")
    graph.roteirizar(it_c, b)
    processar_id(w, id_c)
    checar("divergente" in (linha(con, id_c)["ultimo_erro"] or ""),
           "hash anunciado diferente do calculado ainda reprova",
           str(linha(con, id_c)["ultimo_erro"]))


# ======================================================================
# D. R9 — heartbeat durante o download
# ======================================================================
SENTINELA = "2000-01-01 00:00:00"


class GraphLento(GraphFalso):
    """Baixa devagar, perguntando `deve_parar()` como o `graph_ro.baixar`."""

    def __init__(self, con, chamadas: int):
        super().__init__()
        self.__dict__["con"] = con
        self.__dict__["chamadas"] = chamadas
        self.__dict__["vistos"] = []

    def baixar(self, drive_id, item_id, destino, tamanho_max_bytes=None, tentativas=4,
               deve_parar=None, **_):
        self.con.execute("UPDATE controle SET heartbeat=? WHERE id=1", (SENTINELA,))
        self.con.commit()
        for _ in range(self.chamadas):
            time.sleep(0.02)
            if deve_parar is not None:
                deve_parar()
        c = cu.ler_controle(self.con)
        self.vistos.append((c["heartbeat"], c["item_atual"]))
        return super().baixar(drive_id, item_id, destino, tamanho_max_bytes, tentativas)


def parte_heartbeat(cfg, con, area) -> None:
    print("\n--- D1. download longo: o heartbeat anda ---")
    graph = GraphLento(con, chamadas=5)
    w = montar_worker(cfg, con, area, graph)
    w.w = {**w.w, "heartbeat_s": 0.05}
    b = corpo("DOWNLOAD LONGO")
    ident, item = inserir(con, "download_longo.txt", len(b), quickxor.de_bytes(b))
    graph.roteirizar(item, b)
    processar_id(w, ident)
    batida, atual = graph.vistos[-1] if graph.vistos else (None, None)
    checar(batida not in (None, SENTINELA),
           "heartbeat gravado DURANTE o download", str(batida))
    checar(atual and "download_longo" in atual, "sem apagar o item atual", str(atual))

    print("\n--- D2. no maximo 1 escrita a cada heartbeat_s ---")
    graph = GraphLento(con, chamadas=20)
    w = montar_worker(cfg, con, area, graph)
    w.w = {**w.w, "heartbeat_s": 3600}
    b = corpo("DOWNLOAD CURTO")
    ident, item = inserir(con, "download_curto.txt", len(b), quickxor.de_bytes(b))
    graph.roteirizar(item, b)
    processar_id(w, ident)
    checar(graph.vistos and graph.vistos[-1][0] == SENTINELA,
           "20 perguntas em <1 s com heartbeat_s alto: nenhuma escrita",
           str(graph.vistos[-1:]))


# ----------------------------------------------------------------------
def main() -> int:
    area = area_temporaria("cerebro_robustez_")
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir()
    (area / "tmp").mkdir()
    config = escrever_config(area, raiz_falsa, fonte="graph")

    print("\n--- 0. banco do zero + busca v5 pelo script real ---")
    r = rodar("00_init_db.py", config)
    checar(r.returncode == 0, "00_init_db cria o banco", r.stderr[-800:])
    r = rodar("05_migrar_v5.py", config, "--aplicar", "--sem-backup")
    checar(r.returncode == 0, "05_migrar_v5 --aplicar sai 0", (r.stdout + r.stderr)[-800:])
    cfg = cu.carregar_config(config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])

    for parte in (lambda: parte_pilula(cfg, con, area),
                  lambda: parte_credencial(cfg, con, area, config),
                  lambda: parte_quickxor(cfg, con, area),
                  lambda: parte_heartbeat(cfg, con, area)):
        try:
            parte()
        except Exception as exc:                       # uma parte nao esconde as outras
            import traceback
            traceback.print_exc()
            checar(False, f"a parte terminou sem excecao ({type(exc).__name__})", str(exc))
            con.rollback()
    con.close()
    return placar_final()


if __name__ == "__main__":
    sys.exit(main())
