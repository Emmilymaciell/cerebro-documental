"""
test_delta — E4: o inventario da nuvem, com um Graph falso e roteirizado.

    python tests\\test_delta.py

Por que um Graph falso: o SharePoint e READ-ONLY para este projeto — nao
podemos renomear nem apagar nada la para testar. Entao o teste roteiriza as
respostas do delta e verifica o que o inventario faz com elas.

Cobre os dez casos pedidos: inventario inicial, interrupcao e retomada,
incremental, novo, alterado, renomeado, movido, removido, reaparecimento e
erro de API sem perder o checkpoint anterior.

A prova central: **renomear ou mover NAO cria identidade nova** enquanto o
`item_id` for o mesmo.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import RAIZ, area_temporaria, checar, escrever_config, placar_final, rodar  # noqa: E402

import cerebro_util as cu  # noqa: E402
import graph_ro  # noqa: E402

DRIVE = "drive!Programa"


def carregar_modulo():
    """`12_scan_graph.py` comeca com digito: import normal nao resolve."""
    caminho = RAIZ / "scripts" / "12_scan_graph.py"
    spec = importlib.util.spec_from_file_location("scan_graph", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


# ----------------------------------------------------------------------
# Graph falso
# ----------------------------------------------------------------------

def arquivo(ident, nome, pai, tamanho=100, hash_="HASH1", ctag="c1"):
    return {"id": ident, "name": nome, "size": tamanho,
            "parentReference": {"driveId": DRIVE, "id": pai},
            "file": {"hashes": {"quickXorHash": hash_}},
            "eTag": "e1", "cTag": ctag,
            "createdDateTime": "2026-01-01T00:00:00Z",
            "lastModifiedDateTime": "2026-01-02T00:00:00Z",
            "createdBy": {"user": {"displayName": "Fulano"}},
            "lastModifiedBy": {"user": {"displayName": "Ciclana"}},
            "webUrl": f"https://x/{nome}"}


def pasta(ident, nome, pai=None):
    item = {"id": ident, "name": nome, "folder": {"childCount": 1},
            "parentReference": {"driveId": DRIVE}, "size": 0}
    if pai:
        item["parentReference"]["id"] = pai
    return item


def raiz():
    return {"id": "root", "name": "root", "folder": {"childCount": 2},
            "parentReference": {"driveId": DRIVE}, "size": 0,
            "root": {}}


def removido(ident):
    return {"id": ident, "deleted": {"state": "deleted"},
            "parentReference": {"driveId": DRIVE}}


class GraphFalso:
    """Serve paginas de delta roteirizadas. Nenhuma rede envolvida."""

    def __init__(self, rodadas: list[list[list[dict]]]):
        self.rodadas = rodadas           # rodadas[r] = lista de paginas
        self.chamadas = 0
        self.esperas_429 = 0
        self.falhar_na_chamada = None
        self.base_rodada = 0

    def site_id(self):
        return "site!1"

    def drives(self, site_id):
        return [{"id": DRIVE, "name": "Programa", "driveType": "documentLibrary",
                 "webUrl": "https://x/Programa", "quota": {"used": 1234}}]

    def get(self, url, **kw):
        self.chamadas += 1
        if self.falhar_na_chamada is not None and self.chamadas >= self.falhar_na_chamada:
            raise graph_ro.ErroGraph(503, "serviceNotAvailable", "queda simulada", url)
        if url.startswith("pg:"):
            _, r, p = url.split(":")
            r, p = int(r), int(p)
        elif url.startswith("dl:"):
            r, p = int(url.split(":")[1]), 0
        else:
            r, p = self.base_rodada, 0
        paginas = self.rodadas[r]
        pagina = {"value": paginas[p]}
        if p + 1 < len(paginas):
            pagina["@odata.nextLink"] = f"pg:{r}:{p+1}"
        else:
            pagina["@odata.deltaLink"] = f"dl:{r+1}"
        return pagina


# ----------------------------------------------------------------------
# Cenario
# ----------------------------------------------------------------------

def montar_rodadas():
    inicial_p1 = [raiz(), pasta("p1", "Notificacoes", "root"),
                  arquivo("a", "a.pdf", "p1"), arquivo("b", "b.pdf", "p1", 200, "HASHB")]
    inicial_p2 = [pasta("p2", "Medicoes", "root"),
                  arquivo("c", "c.docx", "p2", 300, "HASHC")]
    return [
        [inicial_p1, inicial_p2],                                  # 0 inventario inicial
        [[]],                                                      # 1 incremental sem mudanca
        [[arquivo("a", "a-renomeado.pdf", "p1")]],                 # 2 renomeado
        [[arquivo("b", "b.pdf", "p2", 200, "HASHB")]],             # 3 movido p1 -> p2
        [[arquivo("c", "c.docx", "p2", 999, "HASHC2", "c2")]],     # 4 conteudo alterado
        [[removido("b")]],                                         # 5 removido
        [[arquivo("b", "b.pdf", "p2", 200, "HASHB")]],             # 6 reapareceu
        [[arquivo("d", "d.xlsx", "p2", 400, "HASHD")]],            # 7 novo
        [[pasta("p1", "Notificacoes 2026", "root")]],              # 8 pasta renomeada
    ]


def sincronizar(modulo, con, graph, modo="auto", limite=0):
    execucao = cu.abrir_execucao(con, "teste_delta", "", {})
    log = cu.Log(Path(con.execute("SELECT 1").fetchone() and RAIZ / "tmp"), "teste_delta",
                 verboso=False)
    inv = modulo.InventarioNuvem(graph, con, execucao, log, limite)
    inv.registrar_drives(graph.site_id())
    resultado = inv.sincronizar(DRIVE, "Programa", modo)
    log.fechar()
    return inv, resultado


def caminhos(con) -> dict:
    return {l["item_id"]: l["caminho_rel"] for l in
            con.execute("SELECT item_id, caminho_rel FROM item_nuvem")}


def eventos(con, item_id=None) -> list[str]:
    if item_id:
        return [l[0] for l in con.execute(
            "SELECT evento FROM evento_nuvem WHERE item_id=? ORDER BY id", (item_id,))]
    return [l[0] for l in con.execute("SELECT evento FROM evento_nuvem ORDER BY id")]


def main() -> int:
    modulo = carregar_modulo()
    area = area_temporaria("cerebro_delta_")
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir(parents=True)
    config = escrever_config(area, raiz_falsa)
    rodar("00_init_db.py", config)
    rodar("03_migrar_v3.py", config, "--sem-backup")
    con = cu.conectar(area / "teste.db")

    rodadas = montar_rodadas()

    print("\n--- 1. inventario inicial completo ---")
    graph = GraphFalso(rodadas)
    inv, resultado = sincronizar(modulo, con, graph, "completo")
    checar(resultado == "concluida", "rodada inicial conclui", resultado)
    total = con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0]
    checar(total == 6, f"6 itens gravados (root, 2 pastas, 3 arquivos) — achou {total}")
    checar(inv.contadores["paginas"] == 2, "percorreu as 2 paginas")
    mapa = caminhos(con)
    checar(mapa["a"] == "Notificacoes\\a.pdf", "caminho montado pela cadeia de pais",
           mapa.get("a"))
    checar(mapa["c"] == "Medicoes\\c.docx", "caminho do segundo ramo", mapa.get("c"))
    linha = con.execute("SELECT * FROM item_nuvem WHERE item_id='a'").fetchone()
    checar(linha["quick_xor"] == "HASH1", "quickXorHash do servidor foi gravado")
    checar(linha["criado_por"] == "Fulano" and linha["modificado_por"] == "Ciclana",
           "autoria gravada")
    token = con.execute("SELECT * FROM delta_token WHERE drive_id=?", (DRIVE,)).fetchone()
    checar(token["status"] == "concluido" and token["delta_link"] == "dl:1",
           "delta_link salvo no fim da rodada", dict(token) and token["status"])
    checar(token["next_link"] is None, "next_link limpo ao concluir")

    print("\n--- 2. incremental sem mudancas ---")
    antes_eventos = len(eventos(con))
    graph = GraphFalso(rodadas)
    inv, _ = sincronizar(modulo, con, graph, "auto")
    checar(inv.contadores["itens"] == 0, "delta nao devolve nada quando nada mudou")
    checar(len(eventos(con)) == antes_eventos, "nenhum evento novo")
    checar(con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0] == 6,
           "nada foi criado nem apagado")

    print("\n--- 3. renomear NAO cria identidade nova ---")
    ids_antes = {l[0] for l in con.execute("SELECT item_id FROM item_nuvem")}
    graph = GraphFalso(rodadas)
    sincronizar(modulo, con, graph, "auto")
    ids_depois = {l[0] for l in con.execute("SELECT item_id FROM item_nuvem")}
    checar(ids_antes == ids_depois, "o conjunto de item_id nao mudou")
    checar(con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0] == 6,
           "continua com 6 itens (nao criou um sétimo)")
    linha = con.execute("SELECT * FROM item_nuvem WHERE item_id='a'").fetchone()
    checar(linha["nome"] == "a-renomeado.pdf", "nome atualizado")
    checar(linha["caminho_rel"] == "Notificacoes\\a-renomeado.pdf", "caminho atualizado",
           linha["caminho_rel"])
    checar(linha["caminho_anterior"] == "Notificacoes\\a.pdf",
           "caminho anterior preservado", linha["caminho_anterior"])
    checar("renomeado" in eventos(con, "a"), "evento 'renomeado' registrado",
           str(eventos(con, "a")))

    print("\n--- 4. mover dentro do mesmo drive ---")
    graph = GraphFalso(rodadas)
    sincronizar(modulo, con, graph, "auto")
    linha = con.execute("SELECT * FROM item_nuvem WHERE item_id='b'").fetchone()
    checar(linha["parent_id"] == "p2", "novo pai gravado")
    checar(linha["caminho_rel"] == "Medicoes\\b.pdf", "caminho refeito", linha["caminho_rel"])
    checar(linha["caminho_anterior"] == "Notificacoes\\b.pdf", "caminho anterior guardado")
    checar("movido" in eventos(con, "b"), "evento 'movido' registrado")
    checar(con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0] == 6,
           "mover tambem nao cria item novo")

    print("\n--- 5. conteudo alterado ---")
    graph = GraphFalso(rodadas)
    sincronizar(modulo, con, graph, "auto")
    linha = con.execute("SELECT * FROM item_nuvem WHERE item_id='c'").fetchone()
    checar(linha["tamanho_bytes"] == 999 and linha["quick_xor"] == "HASHC2",
           "tamanho e hash atualizados")
    checar("alterado" in eventos(con, "c"), "evento 'alterado' registrado")

    print("\n--- 6. removido (some da nuvem, fica no catalogo) ---")
    graph = GraphFalso(rodadas)
    sincronizar(modulo, con, graph, "auto")
    linha = con.execute("SELECT * FROM item_nuvem WHERE item_id='b'").fetchone()
    checar(linha is not None, "a linha continua existindo (nada e apagado)")
    checar(linha["presente"] == 0 and linha["removido_em"], "marcado como removido")
    checar("removido" in eventos(con, "b"), "evento 'removido' registrado")

    print("\n--- 7. reaparecimento ---")
    graph = GraphFalso(rodadas)
    sincronizar(modulo, con, graph, "auto")
    linha = con.execute("SELECT * FROM item_nuvem WHERE item_id='b'").fetchone()
    checar(linha["presente"] == 1 and linha["removido_em"] is None, "voltou a presente=1")
    checar("reapareceu" in eventos(con, "b"), "evento 'reapareceu' registrado")

    print("\n--- 8. arquivo novo ---")
    graph = GraphFalso(rodadas)
    inv, _ = sincronizar(modulo, con, graph, "auto")
    checar(inv.contadores["novos"] == 1, "1 item novo")
    checar(con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0] == 7,
           "agora sao 7 itens")

    print("\n--- 9. pasta renomeada: filhos seguem junto ---")
    renames_do_filho_antes = eventos(con, "a").count("renomeado")
    graph = GraphFalso(rodadas)
    sincronizar(modulo, con, graph, "auto")
    mapa = caminhos(con)
    checar(mapa["p1"] == "Notificacoes 2026", "pasta renomeada")
    checar(mapa["a"] == "Notificacoes 2026\\a-renomeado.pdf",
           "o caminho do FILHO foi recalculado, mesmo sem o delta devolve-lo",
           mapa.get("a"))
    checar(eventos(con, "a").count("renomeado") == renames_do_filho_antes,
           "o filho nao ganhou evento de rename (quem mudou foi a pasta)",
           f"{eventos(con, 'a').count('renomeado')} != {renames_do_filho_antes}")

    print("\n--- 10. erro de API nao destroi o checkpoint ---")
    token_antes = con.execute("SELECT delta_link FROM delta_token WHERE drive_id=?",
                              (DRIVE,)).fetchone()["delta_link"]
    # A rodada tem DUAS paginas e a queda e na SEGUNDA chamada. Antes a queda
    # era na primeira (`falhar_na_chamada = 1`): nenhuma pagina chegava a ser
    # aplicada, e "delta_link preservado" era verdade por construcao (M-2).
    # Agora a pagina 1 e aplicada e gravada; so a 2 falha.
    graph = GraphFalso(rodadas + [[[arquivo("e", "e.pdf", "p2")],
                                   [arquivo("f", "f.pdf", "p2")]]])
    graph.falhar_na_chamada = 2
    try:
        sincronizar(modulo, con, graph, "auto")
        checar(False, "a falha simulada propaga")
    except graph_ro.ErroGraph:
        checar(True, "a falha simulada propaga")
    checar(graph.chamadas == 2, "a queda foi DEPOIS de uma pagina aplicada",
           f"chamadas={graph.chamadas}")
    token = con.execute("SELECT * FROM delta_token WHERE drive_id=?", (DRIVE,)).fetchone()
    checar(token["delta_link"] == token_antes,
           "o delta_link valido anterior foi PRESERVADO", f"{token['delta_link']}")
    checar(token["status"] == "erro" and token["erro"], "o erro ficou registrado")
    checar(token["next_link"] == f"pg:{len(rodadas)}:1",
           "o checkpoint aponta para a pagina que falhou", str(token["next_link"]))
    vivos = {l[0] for l in con.execute("SELECT item_id FROM item_nuvem")}
    checar("e" in vivos and "f" not in vivos,
           "a pagina aplicada ficou gravada; a que falhou nao", str(sorted(vivos)))
    checar(con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0] == 8,
           "nenhum item perdido por causa do erro (7 + o da pagina aplicada)")

    print("\n--- 11. interrupcao no meio e retomada ---")
    con.execute("DELETE FROM item_nuvem")
    con.execute("DELETE FROM evento_nuvem")
    con.execute("UPDATE delta_token SET delta_link=NULL, next_link=NULL, "
                "status='nunca_rodou', paginas=0")
    con.commit()
    graph = GraphFalso(rodadas)
    inv, resultado = sincronizar(modulo, con, graph, "completo", limite=4)
    checar(resultado == "interrompida", "para no limite pedido", resultado)
    parcial = con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0]
    token = con.execute("SELECT * FROM delta_token WHERE drive_id=?", (DRIVE,)).fetchone()
    checar(parcial == 4, f"inventario parcial ({parcial} de 6)")
    checar(token["status"] == "em_andamento" and token["next_link"] == "pg:0:1",
           "checkpoint aponta para a proxima pagina", str(token["next_link"]))
    checar(token["delta_link"] is None, "nao gravou delta_link de rodada incompleta")

    graph = GraphFalso(rodadas)
    inv, resultado = sincronizar(modulo, con, graph, "auto")
    checar(resultado == "concluida", "a retomada conclui")
    total = con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0]
    checar(total == 6, f"retomada completa o inventario ({total} itens)")
    checar(caminhos(con)["c"] == "Medicoes\\c.docx",
           "caminhos ficam corretos depois da retomada")

    print("\n--- 12. pasta removida leva os descendentes junto ---")
    # O delta devolve so a pasta apagada. Sem propagacao, o espelho ficaria
    # com uma pasta ausente cheia de filhos "presentes".
    con.execute("DELETE FROM item_nuvem")
    con.execute("DELETE FROM evento_nuvem")
    con.execute("UPDATE delta_token SET delta_link=NULL, next_link=NULL, "
                "status='nunca_rodou', paginas=0")
    con.commit()
    arvore = [raiz(), pasta("pX", "Biblioteca", "root"),
              pasta("pX1", "Sub", "pX"),
              arquivo("f1", "f1.pdf", "pX"),
              arquivo("f2", "f2.pdf", "pX1"),
              arquivo("f3", "f3.pdf", "pX1")]
    graph = GraphFalso([[arvore], [[removido("pX")]]])
    sincronizar(modulo, con, graph, "completo")
    checar(con.execute("SELECT COUNT(*) FROM item_nuvem WHERE presente=1").fetchone()[0] == 6,
           "arvore montada com 6 itens presentes")
    graph.base_rodada = 1
    inv, _ = sincronizar(modulo, con, graph, "incremental")
    vivos = {l[0] for l in con.execute("SELECT item_id FROM item_nuvem WHERE presente=1")}
    checar(vivos == {"root"}, "so a raiz sobrou viva", str(sorted(vivos)))
    checar(inv.contadores["removidos_cascata"] == 4,
           f"4 descendentes sairam por cascata (contou "
           f"{inv.contadores['removidos_cascata']})")
    checar(con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0] == 6,
           "nenhuma linha foi apagada — so marcada (REGRA 6)")
    for alvo in ("f2", "f3", "pX1"):
        detalhe = con.execute("SELECT detalhe FROM evento_nuvem WHERE item_id=? "
                              "AND evento='removido'", (alvo,)).fetchone()
        checar(detalhe is not None and "ancestral" in (detalhe[0] or ""),
               f"{alvo} registra o motivo (pasta ancestral removida)",
               str(detalhe[0] if detalhe else None))

    print("\n--- 13. HTTP 410 resyncRequired vira varredura completa ---")
    con.execute("DELETE FROM item_nuvem")
    con.execute("DELETE FROM evento_nuvem")
    con.execute("UPDATE delta_token SET delta_link=NULL, next_link=NULL, "
                "status='nunca_rodou', paginas=0")
    con.commit()
    # Rodada 0 monta a arvore. Rodada 1 (o que o resync devolve) traz a arvore
    # SEM a pasta pX1 e seus filhos: o Graph nao manda "deleted" num resync,
    # simplesmente nao devolve o que nao existe mais.
    apos = [raiz(), pasta("pX", "Biblioteca", "root"), arquivo("f1", "f1.pdf", "pX")]
    graph = GraphFalso([[arvore], [apos]])
    sincronizar(modulo, con, graph, "completo")
    checar(con.execute("SELECT COUNT(*) FROM item_nuvem WHERE presente=1").fetchone()[0] == 6,
           "arvore de novo com 6 presentes")

    class Graph410(GraphFalso):
        def get(self, url):
            if url.startswith("dl:"):          # o token salvo morreu
                raise graph_ro.ErroGraph(410, "resyncRequired", "Resync required.")
            return super().get(url)

    graph = Graph410([[arvore], [apos]])
    graph.base_rodada = 1
    inv, resultado = sincronizar(modulo, con, graph, "incremental")
    checar(resultado == "concluida", "o 410 nao derruba a rodada", str(resultado))
    checar(inv.contadores["resyncs"] == 1, "registrou 1 resync exigido",
           str(inv.contadores["resyncs"]))
    vivos = {l[0] for l in con.execute("SELECT item_id FROM item_nuvem WHERE presente=1")}
    checar(vivos == {"root", "pX", "f1"},
           "o que o resync nao devolveu foi marcado como removido", str(sorted(vivos)))
    checar(inv.contadores["removidos_varredura"] == 3,
           f"3 ausentes varridos (contou {inv.contadores['removidos_varredura']})")
    detalhe = con.execute("SELECT detalhe FROM evento_nuvem WHERE item_id='f2' "
                          "AND evento='removido' ORDER BY id DESC LIMIT 1").fetchone()
    checar(detalhe is not None and "resync" in (detalhe[0] or ""),
           "o motivo distingue varredura de exclusao anunciada",
           str(detalhe[0] if detalhe else None))
    token = con.execute("SELECT * FROM delta_token WHERE drive_id=?", (DRIVE,)).fetchone()
    checar(token["status"] == "concluido" and token["delta_link"],
           "token novo gravado so no fim do resync", str(token["status"]))
    checar(con.execute("SELECT COUNT(*) FROM item_nuvem").fetchone()[0] == 6,
           "nenhuma linha apagada pelo resync")

    print("\n--- 14. a varredura NAO roda em rodada parcial nem incremental ---")
    # A trava que impede o pior erro possivel: uma rodada interrompida
    # marcando meio acervo como removido.
    con.execute("UPDATE item_nuvem SET presente=1, removido_em=NULL")
    con.execute("UPDATE delta_token SET delta_link='dl:1', next_link=NULL, "
                "status='concluido'")
    con.commit()
    graph = GraphFalso([[arvore], [[]]])
    graph.base_rodada = 1
    inv, _ = sincronizar(modulo, con, graph, "incremental")
    presentes = con.execute("SELECT COUNT(*) FROM item_nuvem WHERE presente=1").fetchone()[0]
    checar(presentes == 6, f"incremental vazio nao remove nada ({presentes} presentes)")
    checar(inv.contadores["removidos_varredura"] == 0, "varredura nao rodou no incremental")

    con.execute("UPDATE delta_token SET delta_link=NULL, next_link=NULL, "
                "status='nunca_rodou', paginas=0")
    con.commit()
    graph = GraphFalso([[arvore[:3], arvore[3:]]])
    inv, resultado = sincronizar(modulo, con, graph, "completo", limite=3)
    presentes = con.execute("SELECT COUNT(*) FROM item_nuvem WHERE presente=1").fetchone()[0]
    checar(resultado == "interrompida" and presentes == 6,
           f"rodada completa INTERROMPIDA nao varre ({presentes} presentes)")
    checar(inv.contadores["removidos_varredura"] == 0,
           "varredura nao roda em rodada que nao chegou ao fim")

    con.close()
    shutil.rmtree(area, ignore_errors=True)
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
