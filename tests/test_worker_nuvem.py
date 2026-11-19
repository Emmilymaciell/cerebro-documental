"""
test_worker_nuvem — E7: o worker lendo da nuvem, com um Graph falso.

    python tests\\test_worker_nuvem.py

Por que um Graph falso: o SharePoint e READ-ONLY para este projeto (REGRA 1).
Nao da para provocar hash divergente, tamanho divergente ou queda de rede
mexendo no acervo real. Entao o teste roteiriza os bytes que o Graph devolve
e verifica o que o worker faz com eles.

O falso entra na camada CERTA: `GraphFalso` substitui o cliente HTTP, mas
`fonte.FonteGraph` e `worker.Worker` sao os de producao, sem adaptacao. Quem
so sabe baixar e o falso — qualquer outro metodo que o worker tente chamar
levanta AssertionError.

Cobre, objetivamente:
  download valido · quickXorHash divergente · tamanho divergente · erro de
  download · temporario apagado no sucesso · temporario apagado no erro ·
  `--manter-tmp` · sobras de execucao interrompida · duplicata conhecida sem
  download · criacao e vinculo em `conteudo` · `v_ocorrencia` · N ocorrencias
  para 1 conteudo · representante nao e duplicata de si mesmo · reeleicao de
  representante · copia alterada troca de conteudo · COMPLETED nao volta para
  a fila · item sem drive_id/item_id nunca vai ao Graph · MISSING/presente=0
  fora da fila · falha de validacao nunca termina em COMPLETED.
"""

from __future__ import annotations

import http.server
import json
import shutil
import sys
import threading
import time
import types
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import area_temporaria, checar, escrever_config, placar_final, rodar  # noqa: E402

import cerebro_util as cu  # noqa: E402
import fonte as fonte_mod  # noqa: E402
import graph_ro  # noqa: E402
import quickxor  # noqa: E402
import worker as worker_mod  # noqa: E402

DRIVE = "drive!Programa"
SEQ = {"n": 0}


# ----------------------------------------------------------------------
# Graph falso — so sabe baixar, e so o que o roteiro mandar
# ----------------------------------------------------------------------
class GraphFalso:
    def __init__(self):
        self.roteiro: dict = {}     # item_id -> bytes | Exception
        self.baixados: list = []    # item_ids pedidos, na ordem
        self.bytes_servidos = 0

    def roteirizar(self, item_id: str, resposta) -> None:
        self.roteiro[item_id] = resposta

    def baixar(self, drive_id, item_id, destino, tamanho_max_bytes=None, tentativas=4,
               **_):
        self.baixados.append(item_id)
        resposta = self.roteiro.get(item_id)
        if resposta is None:
            raise graph_ro.ErroGraph(404, "itemNotFound", f"sem roteiro para {item_id}")
        if isinstance(resposta, Exception):
            raise resposta
        destino = Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(resposta)
        self.bytes_servidos += len(resposta)
        return len(resposta)

    def __getattr__(self, nome):
        raise AssertionError(f"o worker chamou `{nome}` no Graph — a fonte da "
                             f"nuvem so pode baixar (DEC-014)")


# ----------------------------------------------------------------------
# Andaime
# ----------------------------------------------------------------------
def inserir(con, nome, tamanho, quick, *, categoria="texto", prioridade=0,
            estado="QUEUED", presente=1, presente_nuvem=1, drive_id=DRIVE,
            item_id="auto", suportado=1):
    SEQ["n"] += 1
    if item_id == "auto":
        item_id = f"item!{SEQ['n']:03d}"
    caminho = f"Programa\\P{prioridade}\\{SEQ['n']:03d}_{nome}"
    cur = con.execute(
        "INSERT INTO arquivo (caminho_rel, biblioteca, pasta_rel, nome, ext, categoria,"
        " nome_normalizado, profundidade, comprimento_caminho, tamanho_bytes, prioridade,"
        " suportado, estado, presente, presente_nuvem, drive_id, item_id, quick_xor,"
        " caminho_nuvem, tentativas, primeira_vez_em, ultima_vez_em, criado_em_nuvem)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
        (caminho, "Programa", f"P{prioridade}", nome, Path(nome).suffix.lower(), categoria,
         cu.normalizar_nome(nome), 2, len(caminho), tamanho, prioridade, suportado,
         estado, presente, presente_nuvem, drive_id, item_id, quick,
         caminho.replace("\\", "/"), cu.agora_iso(), cu.agora_iso(),
         "2026-01-01T00:00:00Z"))
    con.commit()
    return int(cur.lastrowid), item_id


def montar_worker(cfg, con, area, graph, **extra):
    args = types.SimpleNamespace(comando="run", config=None, limite=0,
                                 prioridade_max=None, sem_hidratar=False,
                                 so_nuvem=False, uma_vez=True, fonte="graph",
                                 ids=None, manter_tmp=False, quieto=True)
    for chave, valor in extra.items():
        setattr(args, chave, valor)
    execucao_id = cu.abrir_execucao(con, "worker", "", {"teste": True})
    log = cu.Log(cfg["_logs_path"], "worker_nuvem", con, execucao_id, verboso=False)
    porta = fonte_mod.FonteGraph(cfg, graph=graph, pasta_tmp=area / "tmp")
    return worker_mod.Worker(cfg, con, log, execucao_id, args, fonte_pronta=porta)


def linha(con, arquivo_id):
    return con.execute("SELECT * FROM arquivo WHERE id=?", (arquivo_id,)).fetchone()


def sobras(area):
    return sorted(p.name for p in (area / "tmp").glob("e7_*"))


def processar_id(w, arquivo_id):
    """Pega o item pela fila (exercita o filtro) e processa."""
    w.args.ids = [arquivo_id]
    item = w.proximo()
    if item is None:
        return None, None
    return w.processar(item), item


def falhas_de_rede(w, arquivo_id):
    """`Worker.falhas_de_rede`, tolerando a versao antiga (para provar o teste)."""
    medir = getattr(w, "falhas_de_rede", None)
    return medir(arquivo_id) if medir else None


def contar(con, tabela, onde="1=1", *params):
    return con.execute(f"SELECT COUNT(*) FROM {tabela} WHERE {onde}", params).fetchone()[0]


# ----------------------------------------------------------------------
# T-10 / A-3 — teto de extracao conferido ANTES do download
# ----------------------------------------------------------------------
# A consulta que a documentacao (docs/05_OPERACAO.md) manda usar para achar
# os adiados quando existir extrator de arquivo grande. O teste roda a MESMA
# consulta: se a marca mudar, a documentacao e o teste quebram juntos.
SQL_ADIADOS_TETO = (
    "SELECT a.id FROM arquivo a JOIN extracao e ON e.arquivo_id = a.id "
    "WHERE a.estado = 'PARTIAL' AND e.versao = 'adiado/teto_tamanho' ORDER BY a.id")


def parte_teto_antes_do_download(con, w, graph, area) -> None:
    print("\n--- 19. T-10: item acima do teto de extracao NAO e baixado ---")
    # Teto pequeno so neste trecho: 16384 bytes (0.015625 MB, exato em bytes).
    teto_original = w.w.get("tamanho_max_extracao_mb")
    w.w["tamanho_max_extracao_mb"] = 0.015625
    teto_bytes = 16384
    try:
        GRANDE = ("PROJETO EXECUTIVO - volume unico, desenhos e memoriais.\n"
                  * 400).encode("utf-8")
        assert len(GRANDE) > teto_bytes
        Q_GRANDE = quickxor.de_bytes(GRANDE)
        id_g1, item_g1 = inserir(con, "projeto_executivo.txt", len(GRANDE), Q_GRANDE)
        id_g2, item_g2 = inserir(con, "projeto_executivo (copia).txt", len(GRANDE),
                                 Q_GRANDE, prioridade=2)
        # Roteirizados: se o worker pedisse, o download DARIA certo. A prova e
        # que ele nem pede.
        graph.roteirizar(item_g1, GRANDE)
        graph.roteirizar(item_g2, GRANDE)
        pedidos_antes = list(graph.baixados)
        baixados_antes = w.contadores["bytes_baixados"]

        desfecho, _ = processar_id(w, id_g1)
        g1 = linha(con, id_g1)
        checar(item_g1 not in graph.baixados[len(pedidos_antes):],
               "NENHUM pedido de download para o item acima do teto",
               str(graph.baixados[len(pedidos_antes):]))
        checar(desfecho == "ok", "o item adiado tem desfecho normal", str(desfecho))
        checar(g1["estado"] == "PARTIAL", "termina em PARTIAL (sem estado novo)",
               g1["estado"])
        checar(w.contadores["bytes_baixados"] == baixados_antes,
               "nenhum byte contado como baixado")
        checar(w.contadores.get("adiados_tamanho") == 1, "contou 1 adiado por tamanho",
               str(w.contadores.get("adiados_tamanho")))
        checar(sobras(area) == [], "nada em tmp/", str(sobras(area)))
        ext = con.execute("SELECT * FROM extracao WHERE arquivo_id=?", (id_g1,)).fetchall()
        checar(len(ext) == 1, "gravou exatamente 1 extracao", str(len(ext)))
        if ext:
            e = ext[0]
            checar(e["versao"] == getattr(worker_mod, "VERSAO_ADIADO_TETO", None)
                   == "adiado/teto_tamanho",
                   "a extracao e identificavel pela versao 'adiado/teto_tamanho'",
                   str(e["versao"]))
            checar(e["status"] == "parcial" and e["extrator"] == "texto",
                   "status parcial, extrator = categoria (retomada faz upsert na mesma linha)",
                   f"{e['status']}/{e['extrator']}")
            checar((e["erro"] or "").startswith("ADIADO_TETO_TAMANHO:"),
                   "o erro tem o prefixo ADIADO_TETO_TAMANHO", str(e["erro"]))
            checar(e["sha256_fonte"] == Q_GRANDE, "guarda o quick_xor do inventario")
            checar(e["perfil_path"] and Path(e["perfil_path"]).exists(),
                   "perfil JSON gravado, como em qualquer PARTIAL")
        checar((g1["ultimo_erro"] or "").startswith("ADIADO_TETO_TAMANHO:"),
               "arquivo.ultimo_erro diz por que nao foi extraido", str(g1["ultimo_erro"]))
        ev = con.execute("SELECT detalhe FROM evento_arquivo WHERE arquivo_id=? "
                         "AND evento='processado'", (id_g1,)).fetchall()
        detalhe = ev[0][0] if ev else ""
        checar(len(ev) == 1 and '"adiado": "teto_tamanho"' in detalhe
               and '"baixado": false' in detalhe,
               "evento 'processado' com adiado=teto_tamanho e baixado=false", detalhe)
        conteudo = con.execute("SELECT * FROM conteudo WHERE quick_xor=? AND "
                               "tamanho_bytes=?", (Q_GRANDE, len(GRANDE))).fetchone()
        checar(conteudo is not None and g1["conteudo_id"] == conteudo["id"],
               "registrou o conteudo pelo quick_xor do inventario, sem baixar")
        if conteudo is not None:
            checar(conteudo["estado"] == "PARCIAL" and conteudo["representante_id"] == id_g1,
                   "conteudo PARCIAL, representado pelo adiado",
                   f"{conteudo['estado']}/{conteudo['representante_id']}")
            checar(conteudo["versao_extrator"] == "adiado/teto_tamanho",
                   "o conteudo tambem carrega a marca", str(conteudo["versao_extrator"]))

        print("\n--- 20. T-10: a copia do arquivo grande e coberta sem download ---")
        desfecho, _ = processar_id(w, id_g2)
        g2 = linha(con, id_g2)
        checar(desfecho == "duplicata" and g2["estado"] == "DUPLICATE",
               "a copia vira DUPLICATE no passo 1", f"{desfecho}/{g2['estado']}")
        checar(item_g2 not in graph.baixados, "e tambem nao foi baixada")
        checar(g2["conteudo_id"] == g1["conteudo_id"], "aponta para o mesmo conteudo")

        print("\n--- 21. T-10: sem quick_xor no inventario, adia igual ---")
        id_sem, item_sem = inserir(con, "grande_sem_hash.txt", len(GRANDE), None)
        graph.roteirizar(item_sem, GRANDE)
        desfecho, _ = processar_id(w, id_sem)
        sem = linha(con, id_sem)
        checar(sem["estado"] == "PARTIAL" and item_sem not in graph.baixados,
               "PARTIAL sem download", f"{sem['estado']}")
        checar(sem["conteudo_id"] is None, "sem hash nao ha grupo de conteudo")

        print("\n--- 22. T-10: dentro do teto continua sendo baixado ---")
        NO_TETO = (b"x" * (teto_bytes - 20)) + b"\nfim do arquivo\n12345"
        NO_TETO = NO_TETO[:teto_bytes]
        assert len(NO_TETO) == teto_bytes
        id_lim, item_lim = inserir(con, "exatamente_no_teto.txt", len(NO_TETO),
                                   quickxor.de_bytes(NO_TETO))
        graph.roteirizar(item_lim, NO_TETO)
        desfecho, _ = processar_id(w, id_lim)
        lim = linha(con, id_lim)
        checar(item_lim in graph.baixados, "item exatamente no teto FOI baixado")
        checar(lim["estado"] == "COMPLETED", "e extraido normalmente", lim["estado"])
        ext_lim = con.execute("SELECT versao FROM extracao WHERE arquivo_id=?",
                              (id_lim,)).fetchone()
        checar(ext_lim is not None and ext_lim[0] != "adiado/teto_tamanho",
               "sem a marca de adiado")
        checar(sobras(area) == [], "tmp/ limpo", str(sobras(area)))

        achados = [r[0] for r in con.execute(SQL_ADIADOS_TETO)]
        checar(achados == sorted([id_g1, id_sem]),
               "a consulta documentada acha exatamente os adiados", str(achados))
    finally:
        if teto_original is None:
            w.w.pop("tamanho_max_extracao_mb", None)
        else:
            w.w["tamanho_max_extracao_mb"] = teto_original


# ----------------------------------------------------------------------
# Um "Graph" HTTP de verdade, em 127.0.0.1 — para exercitar o graph_ro.py
# de producao (sockets, timeouts, redirect, Retry-After) sem rede externa.
# ----------------------------------------------------------------------
class ServidorGraphLocal:
    """Responde como o Graph + o CDN de download, segundo um roteiro por item.

    Rotas:
      /v1.0/drives/<d>/items/<i>?$select=...   metadados com downloadUrl
      /v1.0/drives/<d>/items/<i>/content       302 -> /cdn/<i>
      /cdn/<i>                                 o conteudo, conforme o roteiro:
          ("ok", bytes) · ("ok401", bytes) /content da 401, metadados ok ·
          ("pinga", total) 1 byte a cada 0,1 s ·
          ("mudo",) cabecalho e silencio · ("429", retry_after, ao_servir) ·
          ("404",)
    Cada pedido fica em `pedidos` como (rota, item, veio_com_authorization).
    """

    def __init__(self):
        self.roteiro: dict = {}
        self.pedidos: list = []
        self.parar = threading.Event()
        falso = self

        class Manipulador(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                falso._atender(self)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Manipulador)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def fechar(self):
        self.parar.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------
    def _json(self, h, status, corpo, extra=None):
        bruto = json.dumps(corpo).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(bruto)))
        for chave, valor in (extra or {}).items():
            h.send_header(chave, valor)
        h.end_headers()
        h.wfile.write(bruto)

    def _atender(self, h):
        partes = urllib.parse.unquote(urllib.parse.urlsplit(h.path).path).strip("/").split("/")
        auth = "Authorization" in h.headers
        if partes[0] == "cdn" and len(partes) == 2:
            self.pedidos.append(("cdn", partes[1], auth))
            return self._cdn(h, partes[1])
        if len(partes) >= 5 and partes[0] == "v1.0" and partes[3] == "items":
            item = partes[4]
            roteiro = self.roteiro.get(item, ("404",))
            if len(partes) == 6 and partes[5] == "content":
                self.pedidos.append(("content", item, auth))
                if roteiro[0] == "404":
                    return self._json(h, 404, {"error": {"code": "itemNotFound",
                                                         "message": "nao existe"}})
                if roteiro[0] == "ok401":
                    return self._json(h, 401, {"error": {"code": "unauthenticated",
                                                         "message": "sem redirect"}})
                h.send_response(302)
                h.send_header("Location", f"{self.base}/cdn/{item}")
                h.send_header("Content-Length", "0")
                h.end_headers()
                return None
            self.pedidos.append(("meta", item, auth))
            if roteiro[0] == "404":
                return self._json(h, 404, {"error": {"code": "itemNotFound",
                                                     "message": "nao existe"}})
            tamanho = len(roteiro[1]) if roteiro[0] in ("ok", "ok401") else 1000
            return self._json(h, 200, {"id": item, "size": tamanho,
                                       "@microsoft.graph.downloadUrl":
                                           f"{self.base}/cdn/{item}"})
        return self._json(h, 400, {"error": {"code": "rotaDesconhecida", "message": h.path}})

    def _cdn(self, h, item):
        roteiro = self.roteiro.get(item, ("404",))
        tipo = roteiro[0]
        try:
            if tipo in ("ok", "ok401"):
                h.send_response(200)
                h.send_header("Content-Length", str(len(roteiro[1])))
                h.end_headers()
                h.wfile.write(roteiro[1])
            elif tipo == "pinga":
                h.send_response(200)
                h.send_header("Content-Length", str(roteiro[1]))
                h.end_headers()
                h.wfile.flush()
                for _ in range(roteiro[1]):
                    if self.parar.wait(0.1):
                        return
                    h.wfile.write(b"x")
                    h.wfile.flush()
            elif tipo == "mudo":
                h.send_response(200)
                h.send_header("Content-Length", "1000")
                h.end_headers()
                h.wfile.flush()
                self.parar.wait(60)
            elif tipo == "429":
                if len(roteiro) > 2 and roteiro[2]:
                    roteiro[2]()
                self._json(h, 429, {"error": {"code": "activityLimitReached",
                                              "message": "devagar"}},
                           {"Retry-After": str(roteiro[1])})
            else:
                self._json(h, 404, {"error": {"code": "itemNotFound", "message": "nao existe"}})
        except (ConnectionError, OSError):
            return


def graph_real_local(servidor) -> graph_ro.GraphRO:
    """O GraphRO de PRODUCAO, apontado para o servidor local, com token falso."""
    graph = graph_ro.GraphRO(env={"GRAPH_TENANT_ID": "t", "GRAPH_CLIENT_ID": "c",
                                  "GRAPH_CLIENT_SECRET": "s"})
    graph.token = lambda: "TOKEN-FALSO"
    return graph


def parte_prazo_e_parada(cfg, con, area) -> None:
    """T-18 (prazo duro por item) e M-14 (parada interrompe o download)."""
    servidor = ServidorGraphLocal()
    graph_antigo = graph_ro.GRAPH
    graph_ro.GRAPH = servidor.base + "/v1.0"
    # Sem proxy do sistema no caminho de 127.0.0.1 (so neste processo de teste).
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    graph = graph_real_local(servidor)
    w = montar_worker(cfg, con, area, graph)
    w.fonte.prazo_s = 2.0
    w.fonte.timeout_leitura_s = 1.0
    try:
        print("\n--- 23. graph_ro de producao contra o servidor local: download ok ---")
        dados = ("RELATORIO SEMANAL 33 - avanco fisico da obra.\n" * 30).encode()
        id_ok, item_ok = inserir(con, "semanal_33.txt", len(dados), quickxor.de_bytes(dados))
        servidor.roteiro[item_ok] = ("ok", dados)
        desfecho, _ = processar_id(w, id_ok)
        checar(desfecho == "ok" and linha(con, id_ok)["estado"] == "COMPLETED",
               "o caminho real de download funciona", f"{desfecho}")
        cdn = [p for p in servidor.pedidos if p[0] == "cdn" and p[1] == item_ok]
        checar(cdn and not any(p[2] for p in cdn),
               "o token NAO vai para o CDN de download", str(cdn))
        checar(sobras(area) == [], "tmp/ limpo", str(sobras(area)))

        print("\n--- 23b. M-13: uma chamada ao Graph por download ---")
        do_item = [p for p in servidor.pedidos if p[1] == item_ok]
        no_graph = [p for p in do_item if p[0] in ("meta", "content")]
        checar(len(no_graph) == 1 and no_graph[0][0] == "content",
               "1 chamada ao Graph (/content), sem o GET de metadados", str(do_item))
        checar(no_graph and no_graph[0][2], "o /content leva o token (e o Graph)")
        checar(do_item == [("content", item_ok, True), ("cdn", item_ok, False)],
               "302 seguido para o CDN, sem a credencial", str(do_item))

        print("\n--- 23c. M-13: /content recusado -> caminho antigo (metadados) ---")
        dados2 = ("ATA 14 - reuniao de coordenacao.\n" * 30).encode()
        id_401, item_401 = inserir(con, "ata_14.txt", len(dados2), quickxor.de_bytes(dados2))
        servidor.roteiro[item_401] = ("ok401", dados2)
        desfecho, _ = processar_id(w, id_401)
        do_item = [p for p in servidor.pedidos if p[1] == item_401]
        checar(desfecho == "ok" and linha(con, id_401)["estado"] == "COMPLETED",
               "baixou pelo fallback", f"{desfecho}")
        checar([p[0] for p in do_item] == ["content", "meta", "cdn"],
               "/content (401) -> metadados -> downloadUrl", str(do_item))
        checar(not do_item[-1][2], "e o CDN continua sem token", str(do_item))

        print("\n--- 23d. M-13: 404 no /content continua virando MISSING ---")
        id_sum, item_sum = inserir(con, "sumiu_do_drive.txt", 100, "QSUMIU")
        servidor.roteiro[item_sum] = ("404",)
        desfecho, _ = processar_id(w, id_sum)
        checar(desfecho == "ausente" and linha(con, id_sum)["estado"] == "MISSING",
               "404 do Graph -> MISSING (DEC-033)", f"{desfecho}")
        checar(sobras(area) == [], "tmp/ limpo", str(sobras(area)))

        print("\n--- 24. T-18: download que pinga devagar estoura o prazo duro ---")
        id_p, item_p = inserir(con, "pendurado.txt", 5000, "QPINGA")
        servidor.roteiro[item_p] = ("pinga", 5000)
        t0 = time.monotonic()
        desfecho, _ = processar_id(w, id_p)
        gasto = time.monotonic() - t0
        p = linha(con, id_p)
        checar(gasto < 2.0 + 1.0 + 2.0,
               f"devolveu o controle em {gasto:.1f}s (prazo 2s + leitura 1s + folga)")
        checar(desfecho == "erro", "desfecho de erro (nao 'transitorio')", str(desfecho))
        checar(p["estado"] == "ERROR_RETRYABLE" and p["tentativas"] == 1,
               "ERROR_RETRYABLE, gastando 1 tentativa do documento",
               f"{p['estado']}/{p['tentativas']}")
        checar("tempo_esgotado" in (p["ultimo_erro"] or ""), "o motivo diz tempo_esgotado",
               str(p["ultimo_erro"]))
        checar(p["proxima_tentativa_em"] is not None, "com backoff agendado")
        checar(sobras(area) == [], "o temporario parcial foi apagado", str(sobras(area)))
        import operacao as op
        checar(op.categoria_erro(p["ultimo_erro"]) == "rede: tempo esgotado",
               "o relatorio de erros o classifica como tempo esgotado",
               op.categoria_erro(p["ultimo_erro"]))
        for _ in range(3):
            con.execute("UPDATE arquivo SET proxima_tentativa_em=NULL WHERE id=?", (id_p,))
            con.commit()
            processar_id(w, id_p)
        p = linha(con, id_p)
        checar(p["estado"] == "ERROR_FINAL" and p["tentativas"] == 4,
               "na 4a vez vira ERROR_FINAL (mecanismo existente de tentativas)",
               f"{p['estado']}/{p['tentativas']}")
        checar(sobras(area) == [], "tmp/ limpo depois das 4", str(sobras(area)))

        print("\n--- 25. T-18: servidor mudo depois do cabecalho e falha de rede ---")
        id_m, item_m = inserir(con, "mudo.txt", 1000, "QMUDO")
        servidor.roteiro[item_m] = ("mudo",)
        t0 = time.monotonic()
        desfecho, _ = processar_id(w, id_m)
        gasto = time.monotonic() - t0
        m = linha(con, id_m)
        checar(gasto < 2.0 + 1.0 + 2.0, f"devolveu o controle em {gasto:.1f}s")
        checar(desfecho == "transitorio" and m["estado"] == "WAITING_HYDRATION",
               "silencio do servidor e rede: espera a nuvem", f"{desfecho}/{m['estado']}")
        checar("sem_rede" in (m["ultimo_erro"] or "") and "disco" not in (m["ultimo_erro"] or ""),
               "rotulado como rede, nao como 'erro de disco'", str(m["ultimo_erro"]))
        checar(sobras(area) == [], "tmp/ limpo", str(sobras(area)))

        print("\n--- 26. 429 com Retry-After maior que o prazo: falha ja, sem dormir ---")
        id_r, item_r = inserir(con, "limitado.txt", 1000, "QLIM")
        servidor.roteiro[item_r] = ("429", 60)
        t0 = time.monotonic()
        desfecho, _ = processar_id(w, id_r)
        gasto = time.monotonic() - t0
        checar(gasto < 2.0, f"nao dormiu os 60s do Retry-After ({gasto:.1f}s)")
        checar(desfecho == "transitorio", "429 continua transitorio", str(desfecho))

        print("\n--- 27. M-14: pausa pedida durante a espera de 429 interrompe ---")
        w.fonte.prazo_s = 900.0           # prazo folgado: so a pausa pode cortar
        id_z, item_z = inserir(con, "tempestade_429.txt", 1000, "QZ")

        def pausar_pelo_control():
            # O que `control.py pause` faz: grava o comando, por OUTRA conexao,
            # enquanto o worker esta dormindo o Retry-After.
            outra = cu.conectar(cfg["_banco_path"])
            try:
                cu.escrever_controle(outra, comando="PAUSE")
            finally:
                outra.close()

        servidor.roteiro[item_z] = ("429", 60, pausar_pelo_control)
        esperas_antes = graph.esperas_429
        t0 = time.monotonic()
        desfecho, _ = processar_id(w, id_z)
        gasto = time.monotonic() - t0
        cu.escrever_controle(con, comando="RUN")
        z = linha(con, id_z)
        checar(graph.esperas_429 == esperas_antes + 1,
               "o download entrou na espera do Retry-After (e foi la que parou)")
        checar(gasto < 3.0, f"a espera de 60s foi interrompida em {gasto:.1f}s")
        checar(desfecho == "interrompido", "desfecho 'interrompido'", str(desfecho))
        checar(z["estado"] == "QUEUED" and z["tentativas"] == 0,
               "o item volta ao estado de antes, sem gastar tentativa",
               f"{z['estado']}/{z['tentativas']}")
        checar(z["proxima_tentativa_em"] is None and z["ultimo_erro"] is None,
               "sem backoff nem erro registrado: nao foi falha")
        checar(contar(con, "evento_arquivo", "arquivo_id=? AND evento='download_interrompido'",
                      id_z) == 1, "evento download_interrompido gravado")
        checar(sobras(area) == [], "tmp/ limpo", str(sobras(area)))

        print("\n--- 28. M-14: Ctrl+C no meio da espera encerra o laco em PAUSADO ---")
        id_c, item_c = inserir(con, "ctrl_c.txt", 1000, "QC")
        servidor.roteiro[item_c] = ("429", 60, lambda: worker_mod.PARAR.update(pedido=True))
        w.orcamento.piso_disco_gb = 0          # o teste nao depende do disco da maquina
        w.args.ids = [id_c]
        t0 = time.monotonic()
        try:
            estado = w.rodar()
        finally:
            worker_mod.PARAR["pedido"] = False
        gasto = time.monotonic() - t0
        checar(estado == "PAUSADO", "o worker encerrou em PAUSADO", str(estado))
        checar(gasto < 5.0, f"em {gasto:.1f}s, sem esperar o Retry-After de 60s")
        checar(linha(con, id_c)["estado"] == "QUEUED", "o item ficou na fila, intacto",
               linha(con, id_c)["estado"])
        w.args.ids = None
    finally:
        graph_ro.GRAPH = graph_antigo
        servidor.fechar()


# ----------------------------------------------------------------------
# T-12 / A-2 — a fila que se reporta e a fila que o worker consome
# ----------------------------------------------------------------------
def parte_fila_real(con, w, config) -> None:
    print("\n--- 29. T-12: checkpoint, espera e 99_status usam a v_fila_nuvem ---")
    # Um fora de escopo (DEC-017): QUEUED, presente, mas sem presenca na nuvem.
    inserir(con, "coordenacao_fora.txt", 100, "QFORA1", presente_nuvem=0,
            drive_id=None, item_id=None)
    v_fila = contar(con, "v_fila")
    v_nuvem = contar(con, "v_fila_nuvem")
    checar(v_fila > v_nuvem, f"o cenario tem itens fora do alcance ({v_fila} > {v_nuvem})")

    w.checkpoint(observacao="T-12")
    pendentes = con.execute("SELECT pendentes FROM checkpoint ORDER BY id DESC "
                            "LIMIT 1").fetchone()[0]
    checar(pendentes == v_nuvem,
           "checkpoint.pendentes = v_fila_nuvem quando a fonte e a nuvem",
           f"pendentes={pendentes} v_fila_nuvem={v_nuvem} v_fila={v_fila}")

    # Em espera, mas fora da nuvem: o worker nao pode dormir esperando por ele.
    antes = w.restam_em_espera()
    inserir(con, "espera_fora.txt", 100, "QFORA2", estado="WAITING_HYDRATION",
            presente_nuvem=0)
    checar(w.restam_em_espera() == antes,
           "restam_em_espera ignora quem a nuvem nao pode entregar",
           f"{antes} -> {w.restam_em_espera()}")

    v_fila, v_nuvem = contar(con, "v_fila"), contar(con, "v_fila_nuvem")
    r = rodar("99_status.py", config)
    linha_fila = next((ln for ln in r.stdout.splitlines() if ">> FILA PRONTA" in ln), "")
    numero = int(linha_fila.split()[3].replace(".", "")) if linha_fila else -1
    checar(r.returncode == 0, "99_status roda", r.stderr[-300:])
    checar(numero == v_nuvem and "v_fila_nuvem" in linha_fila,
           "99_status mostra a v_fila_nuvem como FILA PRONTA", linha_fila.strip())
    fora = next((ln for ln in r.stdout.splitlines() if "fora do alcance" in ln), "")
    checar(fora and int(fora.split()[3].replace(".", "")) == v_fila - v_nuvem,
           "e mostra a diferenca como 'fora do alcance', sem esconder", fora.strip())


# ----------------------------------------------------------------------
# M-6 — a rede tem contador proprio; nao gasta as tentativas do documento
# ----------------------------------------------------------------------
def parte_contador_de_rede(con, w, graph) -> None:
    print("\n--- 30. M-6: 3 quedas de rede e depois o 1o erro real ---")
    BYTES = ("OFICIO 211 - encaminhamento de medicao.\n" * 25).encode()
    QUICK = quickxor.de_bytes(BYTES)
    ident, item = inserir(con, "oficio_211.txt", len(BYTES), QUICK)

    def de_novo():
        con.execute("UPDATE arquivo SET proxima_tentativa_em=NULL WHERE id=?", (ident,))
        con.commit()
        return processar_id(w, ident)[0]

    graph.roteirizar(item, graph_ro.ErroGraph(503, "serviceNotAvailable", "queda"))
    for _ in range(3):
        de_novo()
    x = linha(con, ident)
    checar(x["estado"] == "WAITING_HYDRATION" and x["tentativas"] == 0,
           "3 quedas: esperando a nuvem, 0 tentativas do documento",
           f"{x['estado']}/{x['tentativas']}")
    checar(falhas_de_rede(w, ident) == 3, "3 falhas de rede contadas",
           str(falhas_de_rede(w, ident)))
    # Agora o documento falha de verdade (bytes que nao batem com o hash).
    graph.roteirizar(item, BYTES[:-1] + b"#")
    de_novo()
    x = linha(con, ident)
    checar(x["estado"] == "ERROR_RETRYABLE" and x["tentativas"] == 1,
           "o 1o erro real e a 1a tentativa — nao ERROR_FINAL direto",
           f"{x['estado']}/{x['tentativas']}")

    print("\n--- 31. M-6: a rede tem orcamento proprio, e ele acaba ---")
    ident2, item2 = inserir(con, "sempre_fora.txt", len(BYTES), QUICK)
    graph.roteirizar(item2, graph_ro.ErroGraph(503, "serviceNotAvailable", "queda"))
    orcamento = w.max_tentativas * 3
    for _ in range(orcamento):
        con.execute("UPDATE arquivo SET proxima_tentativa_em=NULL WHERE id=?", (ident2,))
        con.commit()
        processar_id(w, ident2)
    y = linha(con, ident2)
    checar(y["estado"] == "WAITING_HYDRATION" and y["tentativas"] == 0,
           f"{orcamento} quedas ainda sao espera de rede",
           f"{y['estado']}/{y['tentativas']}")
    con.execute("UPDATE arquivo SET proxima_tentativa_em=NULL WHERE id=?", (ident2,))
    con.commit()
    processar_id(w, ident2)
    y = linha(con, ident2)
    checar(y["estado"] == "ERROR_RETRYABLE" and y["tentativas"] == 1,
           "a seguinte passa a gastar tentativa do documento (nunca fica para sempre)",
           f"{y['estado']}/{y['tentativas']}")

    print("\n--- 32. M-6: processar zera o contador de rede ---")
    graph.roteirizar(item, BYTES)
    de_novo()
    checar(linha(con, ident)["estado"] == "COMPLETED", "depois de tudo, processou")
    checar(falhas_de_rede(w, ident) == 0, "o contador de rede zerou com o processamento",
           str(falhas_de_rede(w, ident)))


# ----------------------------------------------------------------------
def main() -> int:
    area = area_temporaria("cerebro_e7_")
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

    BYTES_A = ("MEDICAO 41 - relatorio mensal da obra RD-103.\n"
               "Servico executado conforme planilha anexa.\n" * 20).encode("utf-8")
    QUICK_A = quickxor.de_bytes(BYTES_A)
    BYTES_N = ("NOTA TECNICA 07 - revisao do projeto geometrico.\n" * 20).encode("utf-8")
    QUICK_N = quickxor.de_bytes(BYTES_N)
    BYTES_X = ("ATA DE REUNIAO 12 - pauta, deliberacoes e encaminhamentos.\n"
               * 15).encode("utf-8")
    QUICK_X = quickxor.de_bytes(BYTES_X)

    # ==================================================================
    print("\n--- 1. download valido ---")
    id_a, item_a = inserir(con, "medicao_41.txt", len(BYTES_A), QUICK_A)
    graph.roteirizar(item_a, BYTES_A)
    desfecho, _ = processar_id(w, id_a)
    a = linha(con, id_a)
    checar(desfecho == "ok", "download valido processa", str(desfecho))
    checar(a["estado"] == "COMPLETED", "item vira COMPLETED", a["estado"])
    checar(graph.baixados == [item_a], "baixou exatamente 1 vez", str(graph.baixados))
    checar(w.contadores["bytes_baixados"] == len(BYTES_A),
           "contou os bytes baixados", str(w.contadores["bytes_baixados"]))
    checar(contar(con, "extracao", "arquivo_id=?", id_a) == 1, "gravou a extracao")
    checar(contar(con, "texto_fts", "arquivo_id=?", id_a) > 0, "indexou o texto")

    print("\n--- 2. temporario apagado no sucesso ---")
    checar(sobras(area) == [], "tmp/ limpo depois do sucesso", str(sobras(area)))

    print("\n--- 3. conteudo criado e vinculado ---")
    conteudos = con.execute("SELECT * FROM conteudo").fetchall()
    checar(len(conteudos) == 1, f"1 linha em conteudo (achou {len(conteudos)})")
    c1 = conteudos[0]
    checar(c1["quick_xor"] == QUICK_A and c1["tamanho_bytes"] == len(BYTES_A),
           "conteudo identificado por (quick_xor, tamanho)")
    checar(c1["estado"] == "EXTRAIDO", "conteudo EXTRAIDO", c1["estado"])
    checar(c1["representante_id"] == id_a, "o primeiro a extrair e o representante")
    checar(c1["extraido_de_item"] == item_a, "conteudo guarda de qual item_id leu")
    checar(a["conteudo_id"] == c1["id"], "a ocorrencia aponta para o conteudo")
    checar(c1["ocorrencias"] == 1, "1 ocorrencia por enquanto", str(c1["ocorrencias"]))
    checar(Path(c1["texto_path"]).exists(), "derivado de texto no lugar")

    print("\n--- 4. duplicata conhecida: vincula sem baixar ---")
    id_b, item_b = inserir(con, "medicao_41 (copia).txt", len(BYTES_A), QUICK_A,
                           prioridade=2)
    id_g, item_g = inserir(con, "MEDICAO 41 REV.txt", len(BYTES_A), QUICK_A, prioridade=3)
    antes_baixados = len(graph.baixados)
    desfecho_b, _ = processar_id(w, id_b)
    desfecho_g, _ = processar_id(w, id_g)
    b, g = linha(con, id_b), linha(con, id_g)
    checar(desfecho_b == "duplicata" and desfecho_g == "duplicata",
           "as copias sao resolvidas como duplicata", f"{desfecho_b}/{desfecho_g}")
    checar(len(graph.baixados) == antes_baixados,
           "NENHUM download para as copias", str(graph.baixados[antes_baixados:]))
    checar(b["estado"] == "DUPLICATE" and g["estado"] == "DUPLICATE",
           "estado DUPLICATE", f"{b['estado']}/{g['estado']}")
    checar(b["conteudo_id"] == c1["id"] and g["conteudo_id"] == c1["id"],
           "as copias apontam para o MESMO conteudo")
    checar(b["duplicata_de"] == id_a and g["duplicata_de"] == id_a,
           "duplicata_de aponta para o representante")
    checar(w.contadores["bytes_economizados"] == 2 * len(BYTES_A),
           "contabilizou os bytes economizados",
           str(w.contadores["bytes_economizados"]))
    c1 = con.execute("SELECT * FROM conteudo WHERE id=?", (c1["id"],)).fetchone()
    checar(c1["ocorrencias"] == 3, f"conteudo com 3 ocorrencias (achou {c1['ocorrencias']})")
    checar(contar(con, "extracao") == 1, "extracao continua sendo 1 so",
           str(contar(con, "extracao")))

    print("\n--- 5. a ocorrencia nao perde nada ao virar duplicata ---")
    checar(b["drive_id"] == DRIVE and b["item_id"] == item_b,
           "a copia mantem drive_id e item_id proprios")
    checar(b["caminho_rel"] != a["caminho_rel"] and b["prioridade"] == 2,
           "a copia mantem caminho e prioridade proprios")
    checar(b["presente"] == 1 and b["presente_nuvem"] == 1,
           "a copia continua presente no catalogo (nao sumiu)")

    print("\n--- 6. v_ocorrencia: um conteudo, N lugares ---")
    ocs = con.execute("SELECT * FROM v_ocorrencia WHERE conteudo_id=? "
                      "ORDER BY arquivo_id", (c1["id"],)).fetchall()
    checar(len(ocs) == 3, f"v_ocorrencia devolve os 3 lugares (achou {len(ocs)})")
    checar(sum(o["eh_representante"] for o in ocs) == 1,
           "exatamente 1 representante entre eles")
    checar(len({o["item_id"] for o in ocs}) == 3 and
           len({o["caminho_rel"] for o in ocs}) == 3,
           "cada ocorrencia tem item_id e caminho proprios")
    checar(all(o["texto_path"] == c1["texto_path"] for o in ocs),
           "as 3 apontam para o mesmo derivado de texto")

    # ==================================================================
    print("\n--- 7. quickXorHash divergente ---")
    id_h, item_h = inserir(con, "adulterado.txt", len(BYTES_N), "HASHQUENAOBATE==")
    graph.roteirizar(item_h, BYTES_N)
    conteudos_antes = contar(con, "conteudo")
    desfecho, _ = processar_id(w, id_h)
    h = linha(con, id_h)
    checar(desfecho == "erro", "hash divergente nao e sucesso", str(desfecho))
    checar(h["estado"] != "COMPLETED", "NUNCA termina COMPLETED", h["estado"])
    checar(h["estado"] == "ERROR_RETRYABLE", "vira ERROR_RETRYABLE", h["estado"])
    checar("divergente" in (h["ultimo_erro"] or ""), "o motivo fica registrado",
           str(h["ultimo_erro"]))
    checar(contar(con, "extracao", "arquivo_id=?", id_h) == 0,
           "nao gravou extracao de bytes suspeitos")
    checar(contar(con, "conteudo") == conteudos_antes,
           "nao criou conteudo de bytes suspeitos")
    checar(contar(con, "evento_arquivo", "arquivo_id=? AND evento='validacao_falhou'",
                  id_h) == 1, "registrou o evento validacao_falhou")
    checar(sobras(area) == [], "tmp/ limpo depois do hash divergente", str(sobras(area)))

    print("\n--- 8. tamanho divergente ---")
    id_t, item_t = inserir(con, "truncado.txt", len(BYTES_N) + 5000, QUICK_N)
    graph.roteirizar(item_t, BYTES_N)          # chegam menos bytes que o anunciado
    desfecho, _ = processar_id(w, id_t)
    t = linha(con, id_t)
    checar(t["estado"] != "COMPLETED", "tamanho divergente nunca vira COMPLETED",
           t["estado"])
    checar("tamanho divergente" in (t["ultimo_erro"] or ""),
           "o motivo fica registrado", str(t["ultimo_erro"]))
    checar(contar(con, "extracao", "arquivo_id=?", id_t) == 0,
           "nao extraiu arquivo truncado")
    checar(sobras(area) == [], "tmp/ limpo depois do tamanho divergente",
           str(sobras(area)))
    checar(w.contadores["validacoes_falhas"] == 2,
           "duas falhas de validacao contabilizadas",
           str(w.contadores["validacoes_falhas"]))

    print("\n--- 9. erro de download (rede) ---")
    id_r, item_r = inserir(con, "queda_de_rede.txt", len(BYTES_N), QUICK_N)
    graph.roteirizar(item_r, graph_ro.ErroGraph(503, "serviceNotAvailable",
                                                "queda simulada"))
    desfecho, _ = processar_id(w, id_r)
    rr = linha(con, id_r)
    checar(desfecho == "transitorio", "erro 503 e tratado como transitorio",
           str(desfecho))
    checar(rr["estado"] != "COMPLETED", "erro de rede nunca vira COMPLETED",
           rr["estado"])
    # M-6: a queda de rede conta no contador da REDE (evento), nao nas
    # tentativas do documento. Antes esta linha afirmava tentativas == 1 —
    # justamente o defeito.
    checar(rr["tentativas"] == 0, "NAO gastou tentativa do documento",
           str(rr["tentativas"]))
    checar(falhas_de_rede(w, id_r) == 1, "contou 1 falha de rede",
           str(falhas_de_rede(w, id_r)))
    checar(rr["proxima_tentativa_em"] is not None, "agendou nova tentativa (backoff)")
    checar(sobras(area) == [], "tmp/ limpo depois do erro de rede", str(sobras(area)))

    print("\n--- 9b. 404 do Graph vira MISSING, nao ERROR_FINAL ---")
    # Aconteceu de verdade na prova do E7: uma biblioteca inteira saiu do
    # drive entre o inventario e o processamento. Item que nao esta mais la
    # nao e "erro de extracao" — e ausencia (DEC-029, REGRA 6).
    id_404, item_404 = inserir(con, "pasta_que_sumiu.txt", len(BYTES_N), QUICK_N)
    graph.roteirizar(item_404, graph_ro.ErroGraph(404, "itemNotFound",
                                                  "The resource could not be found."))
    desfecho, _ = processar_id(w, id_404)
    ausente = linha(con, id_404)
    checar(desfecho == "ausente", "404 tem desfecho proprio", str(desfecho))
    checar(ausente["estado"] == "MISSING", "o item vira MISSING", ausente["estado"])
    checar(ausente["presente_nuvem"] == 0 and ausente["presente"] == 0,
           "presente=0 e presente_nuvem=0, como o E6 gravou nos 1.457")
    checar(contar(con, "arquivo", "id=?", id_404) == 1,
           "a linha NAO e apagada do catalogo (REGRA 6)")
    checar(ausente["caminho_rel"] and ausente["prioridade"] is not None
           and ausente["item_id"] == item_404,
           "caminho, prioridade e item_id continuam registrados")
    checar(ausente["tentativas"] == 0, "nao gasta tentativa: nao e falha nossa",
           str(ausente["tentativas"]))
    checar(ausente["proxima_tentativa_em"] is None, "nao reagenda o que nao existe")
    evento = con.execute("SELECT detalhe FROM evento_arquivo WHERE arquivo_id=? "
                         "AND evento='ausente_na_nuvem'", (id_404,)).fetchone()
    checar(evento is not None and "QUEUED" in (evento[0] or ""),
           "o evento guarda o estado anterior, para poder voltar",
           str(evento[0] if evento else None))
    w.args.ids = [id_404]
    checar(w.proximo() is None, "e sai da fila — nao insiste 4 vezes num 404")
    checar(contar(con, "extracao", "arquivo_id=?", id_404) == 0, "nao extraiu nada")
    checar(sobras(area) == [], "tmp/ limpo depois do 404", str(sobras(area)))

    print("\n--- 10. item que falhou por rede volta para a fila ---")
    # Sem isto o item fica preso: o estado sai da fila e ninguem mais o pega.
    con.execute("UPDATE arquivo SET proxima_tentativa_em=NULL WHERE id=?", (id_r,))
    con.commit()
    w.args.ids = [id_r]
    checar(w.proximo() is not None,
           "o item em espera de rede e reofertado depois do backoff",
           f"estado={linha(con, id_r)['estado']}")
    checar(contar(con, "v_fila_nuvem", "id=?", id_r) == 1,
           "v_fila_nuvem tambem o reoferece")

    print("\n--- 11. --manter-tmp preserva o baixado ---")
    id_m, item_m = inserir(con, "diagnostico.txt", len(BYTES_N), QUICK_N)
    graph.roteirizar(item_m, BYTES_N)
    w.args.manter_tmp = True
    desfecho, _ = processar_id(w, id_m)
    w.args.manter_tmp = False
    checar(desfecho == "ok", "processa normalmente com --manter-tmp", str(desfecho))
    checar(len(sobras(area)) == 1, "o temporario foi PRESERVADO", str(sobras(area)))

    print("\n--- 12. sobras de execucao interrompida sao varridas na subida ---")
    (area / "tmp" / "e7_restodeontem.pdf").write_bytes(b"lixo")
    removidos = w.fonte.limpar_sobras()
    checar(removidos == 2, f"limpar_sobras apagou os 2 restos (apagou {removidos})")
    checar(sobras(area) == [], "tmp/ limpo de novo", str(sobras(area)))

    # ==================================================================
    print("\n--- 13. quem nao pode ir ao Graph nao vai ---")
    id_semid, _ = inserir(con, "sem_identidade.txt", 100, "Q1", item_id=None)
    id_missing, _ = inserir(con, "sumido.txt", 100, "Q2", estado="MISSING", presente=0)
    id_fora, _ = inserir(con, "fora_da_nuvem.txt", 100, "Q3", presente_nuvem=0)
    for rotulo, alvo in (("sem drive_id/item_id", id_semid),
                         ("MISSING com presente=0", id_missing),
                         ("presente_nuvem=0", id_fora)):
        w.args.ids = [alvo]
        checar(w.proximo() is None, f"fila ignora item {rotulo}")
        checar(contar(con, "v_fila_nuvem", "id=?", alvo) == 0,
               f"v_fila_nuvem ignora item {rotulo}")
    chamadas_antes = len(graph.baixados)
    sem_id = linha(con, id_semid)
    resultado = w.fonte.obter(sem_id)
    checar(resultado["ok"] is False and "sem drive_id/item_id" in resultado["motivo"],
           "a fonte recusa item sem identidade", str(resultado["motivo"]))
    checar(len(graph.baixados) == chamadas_antes,
           "e nem chega a falar com o Graph")

    print("\n--- 14. COMPLETED nao e reprocessado sozinho ---")
    w.args.ids = None
    restantes = set()
    while True:
        item = w.proximo()
        if item is None or item["id"] in restantes:
            break
        restantes.add(item["id"])
        con.execute("UPDATE arquivo SET estado='SKIPPED' WHERE id=?", (item["id"],))
        con.commit()
    checar(id_a not in restantes, "o COMPLETED nunca reapareceu na fila")
    checar(id_b not in restantes and id_g not in restantes,
           "as DUPLICATE tambem nao reaparecem")
    con.execute("UPDATE arquivo SET estado='ERROR_RETRYABLE' WHERE estado='SKIPPED'")
    con.commit()

    # ==================================================================
    print("\n--- 15. representante nao e duplicata de si mesmo ---")
    con.execute("UPDATE arquivo SET estado='CHANGED' WHERE id=?", (id_a,))
    con.commit()
    antes_baixados = len(graph.baixados)
    desfecho, _ = processar_id(w, id_a)
    a = linha(con, id_a)
    checar(desfecho == "ok", "o representante reprocessado REEXTRAI", str(desfecho))
    checar(len(graph.baixados) == antes_baixados + 1,
           "baixou de novo, como tem de ser")
    checar(a["estado"] == "COMPLETED" and a["duplicata_de"] is None,
           "e nao vira duplicata de si mesmo",
           f"{a['estado']}/{a['duplicata_de']}")
    c1 = con.execute("SELECT * FROM conteudo WHERE id=?", (c1["id"],)).fetchone()
    checar(c1["representante_id"] == id_a, "continua sendo o representante")
    checar(c1["ocorrencias"] == 3, f"ainda 3 ocorrencias (achou {c1['ocorrencias']})")

    print("\n--- 16a. copia alterada para bytes INEDITOS: reextrai ---")
    graph.roteirizar(item_g, BYTES_X)
    con.execute("UPDATE arquivo SET estado='CHANGED', quick_xor=?, tamanho_bytes=? "
                "WHERE id=?", (QUICK_X, len(BYTES_X), id_g))
    con.commit()
    desfecho, _ = processar_id(w, id_g)
    g = linha(con, id_g)
    novo = con.execute("SELECT * FROM conteudo WHERE quick_xor=? AND tamanho_bytes=?",
                       (QUICK_X, len(BYTES_X))).fetchone()
    velho = con.execute("SELECT * FROM conteudo WHERE id=?", (c1["id"],)).fetchone()
    checar(desfecho == "ok", "a copia com bytes ineditos e reextraida", str(desfecho))
    checar(novo is not None and g["conteudo_id"] == novo["id"] and novo["id"] != c1["id"],
           "passou a apontar para um conteudo NOVO")
    checar(velho["ocorrencias"] == 2,
           f"o conteudo antigo recontou para 2 (achou {velho['ocorrencias']})")
    checar(contar(con, "v_ocorrencia", "conteudo_id=?", c1["id"]) == 2,
           "v_ocorrencia concorda com o contador")

    print("\n--- 16b. copia alterada para bytes JA CONHECIDOS: so revincula ---")
    id_d, item_d = inserir(con, "medicao_41 (outra copia).txt", len(BYTES_A), QUICK_A,
                           prioridade=4)
    processar_id(w, id_d)
    checar(linha(con, id_d)["conteudo_id"] == c1["id"], "entrou no grupo antigo")
    baixados_antes = len(graph.baixados)
    con.execute("UPDATE arquivo SET estado='CHANGED', quick_xor=?, tamanho_bytes=? "
                "WHERE id=?", (QUICK_N, len(BYTES_N), id_d))
    con.commit()
    desfecho, _ = processar_id(w, id_d)
    d = linha(con, id_d)
    conteudo_n = con.execute("SELECT * FROM conteudo WHERE quick_xor=? AND "
                             "tamanho_bytes=?", (QUICK_N, len(BYTES_N))).fetchone()
    velho = con.execute("SELECT * FROM conteudo WHERE id=?", (c1["id"],)).fetchone()
    checar(desfecho == "duplicata",
           "virou copia de um conteudo ja extraido", str(desfecho))
    checar(len(graph.baixados) == baixados_antes, "e nem precisou baixar")
    checar(d["conteudo_id"] == conteudo_n["id"], "aponta para o conteudo ja existente")
    checar(velho["ocorrencias"] == 2,
           f"o grupo antigo voltou a 2 (achou {velho['ocorrencias']})")

    print("\n--- 17. reeleicao de representante, sem reextrair ---")
    extracoes_antes = contar(con, "extracao")
    baixados_antes = len(graph.baixados)
    con.execute("UPDATE arquivo SET presente=0, estado='MISSING' WHERE id=?", (id_a,))
    con.commit()
    orfaos = contar(con, "v_conteudo_orfao", "conteudo_id=?", c1["id"])
    trocas = w.reeleger_representantes()
    depois = con.execute("SELECT * FROM conteudo WHERE id=?", (c1["id"],)).fetchone()
    checar(orfaos == 1, "o conteudo aparece como orfao quando o representante some")
    checar(trocas == 1, f"reelegeu 1 representante (reelegeu {trocas})")
    checar(depois["representante_id"] == id_b, "a outra ocorrencia viva assumiu",
           str(depois["representante_id"]))
    checar(depois["reeleicoes"] == 1, "contou a reeleicao", str(depois["reeleicoes"]))
    checar(contar(con, "extracao") == extracoes_antes,
           "NAO reextraiu nada — o conteudo e o mesmo")
    checar(len(graph.baixados) == baixados_antes, "e nao baixou nada de novo")
    checar(depois["texto_path"] == c1["texto_path"],
           "o derivado continua valendo depois da troca")
    checar(contar(con, "evento_arquivo",
                  "arquivo_id=? AND evento='virou_representante'", id_b) == 1,
           "a troca ficou registrada em evento_arquivo")
    checar(linha(con, id_b)["estado"] == "DUPLICATE",
           "a nova representante nao volta para a fila por causa disso")

    print("\n--- 18. nada de write no Graph, nada sobrando em tmp/ ---")
    checar(sobras(area) == [], "tmp/ limpo ao final de tudo", str(sobras(area)))
    checar(contar(con, "arquivo", "estado='COMPLETED'") >= 1,
           "ha COMPLETED no final")
    checar(contar(con, "conteudo", "representante_id IS NULL") == 0,
           "nenhum conteudo ficou sem representante")

    parte_teto_antes_do_download(con, w, graph, area)
    parte_prazo_e_parada(cfg, con, area)
    parte_fila_real(con, w, config)
    parte_contador_de_rede(con, w, graph)

    con.close()
    shutil.rmtree(area, ignore_errors=True)
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
