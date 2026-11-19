"""
19_ingerir_novos — o que o delta viu e o catalogo ainda nao conhece.

`12_scan_graph.py` atualiza o espelho (`item_nuvem`). `17_aplicar_delta.py`
propaga sumico, volta, rename, move e alteracao — mas **de proposito nao
insere arquivo novo** (`DEC-038`), porque entrar no catalogo exige biblioteca,
classificacao P0..P4 e prioridade, que sao do `01`/`02`.

Este script fecha esse buraco: pega os itens que so existem no espelho e os
incorpora a `arquivo`, com tudo que uma linha do catalogo precisa ter.

    python scripts\\19_ingerir_novos.py             # simula e mostra os numeros
    python scripts\\19_ingerir_novos.py --aplicar   # grava

Como cada campo e decidido:

  identidade   `(drive_id, item_id)` — NUNCA caminho (`DEC-015`). E o unico
               criterio de "ja conheco este item": renomear ou mover nao
               produz item novo, e um item ja catalogado nunca e reinserido
               nem devolvido para a fila.
  caminho      `caminho_rel` no estilo do catalogo, derivado do caminho do
               espelho pela mesma funcao que o `13_reconciliar.py` usa
               (`caminho_local_de`): a biblioteca vem de
               `config/mapa_drives.yaml` (`3. Desenvolvimento` ->
               `Programa - 3. Desenvolvimento`); sem regra, o nome do drive vira
               a biblioteca.
  prioridade   P0..P4 resolvida por `02_classificar.resolver_arvore()` — as
               regras nao sao reimplementadas aqui, sao importadas.
  estado       `02_classificar.estado_inicial()`: `SKIPPED` se temporario,
               `QUEUED` se tem extrator, `UNSUPPORTED` se nao tem (REGRA 6).

O que este script NAO faz:

  - nao marca duplicata. O hash do servidor ja esta na linha; quem decide
    cobrir uma ocorrencia por conteudo ja extraido e o worker, ANTES de
    baixar (`DEC-026`/`DEC-027`). Aqui a ocorrencia so entra na fila; o
    relatorio conta quantas ja tem conteudo conhecido;
  - nao toca em nenhuma linha que ja existe no catalogo;
  - nao baixa um byte.

Simula por padrao. So grava com `--aplicar`, e confere que o total inserido
e exatamente o anunciado.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

classificacao = cu.importar_script("02_classificar")
reconciliacao = cu.importar_script("13_reconciliar")

ROTULOS = classificacao.ROTULOS


class Ingestao:
    """Incorpora ao catalogo os itens que so existem no espelho."""

    def __init__(self, con, cfg: dict, regras, execucao_id: int, log=None):
        self.con = con
        self.cfg = cfg
        self.regras = regras
        self.execucao_id = execucao_id
        self.log = log
        self.categorias = cfg.get("categorias") or {}
        self.suportadas = set(cfg.get("extratores_ativos") or [])
        self.padroes_temp = cfg.get("arquivos_temporarios") or []
        self.raiz = str(cfg["_raiz_path"])
        self.commit_a_cada = int((cfg.get("limites") or {}).get("commit_a_cada", 1000))
        self.nomes_drive = {linha["drive_id"]: linha["nome"] for linha in
                            con.execute("SELECT drive_id, nome FROM drive")}
        _, self.inverso = reconciliacao.carregar_mapa(cfg.get("mapa_drives"))
        self.contadores = {"pendentes": 0, "inseridos": 0, "por_estado": {},
                           "por_prioridade": {}, "sem_regra_de_biblioteca": 0,
                           "caminho_reaproveitado": 0, "conteudo_ja_extraido": 0,
                           "pastas_novas": 0}

    # -- 1. o que falta -------------------------------------------------
    def pendentes(self, limite: int = 0) -> list:
        """Arquivo presente no espelho sem linha correspondente no catalogo.

        A comparacao e pela identidade canonica `(drive_id, item_id)`. Pasta
        nao entra: `arquivo` guarda arquivo; a arvore de pastas e derivada do
        caminho e vive em `pasta`.
        """
        sql = ("SELECT i.* FROM item_nuvem i WHERE i.presente=1 AND i.eh_pasta=0 "
               "AND NOT EXISTS (SELECT 1 FROM arquivo a WHERE a.drive_id=i.drive_id "
               "                AND a.item_id=i.item_id) "
               "ORDER BY i.drive_id, i.item_id")
        if limite:
            sql += f" LIMIT {int(limite)}"
        linhas = self.con.execute(sql).fetchall()
        self.contadores["pendentes"] = len(linhas)
        return linhas

    # -- 2. de item da nuvem para linha do catalogo ----------------------
    def caminho_do_catalogo(self, item) -> str:
        """Caminho no estilo do catalogo, a partir do caminho do espelho."""
        caminho_nuvem = item["caminho_rel"] or item["nome"] or ""
        return reconciliacao.caminho_local_de(
            caminho_nuvem, item["drive_id"],
            self.nomes_drive.get(item["drive_id"], "nuvem"), self.inverso)

    def tem_regra_de_biblioteca(self, item) -> bool:
        primeiro = (item["caminho_rel"] or "").split("\\", 1)[0]
        return (item["drive_id"], primeiro.lower()) in self.inverso

    def preparar(self, itens) -> list[dict]:
        """Monta as linhas, sem gravar nada. Simulacao e aplicacao usam isto."""
        derivados = []
        for item in itens:
            caminho_rel = self.caminho_do_catalogo(item)
            if not caminho_rel:
                continue
            if not self.tem_regra_de_biblioteca(item):
                self.contadores["sem_regra_de_biblioteca"] += 1
                if self.log:
                    self.log.aviso(
                        "sem regra de biblioteca em mapa_drives.yaml; usei o nome "
                        f"do drive: {caminho_rel}", caminho_rel)
            nome = item["nome"] or ""
            ext = (item["ext"] or os.path.splitext(nome)[1] or "").lower()
            categoria = cu.categoria_de(ext, self.categorias)
            temporario = bool(cu.combina_padrao(nome, self.padroes_temp))
            suportado = (not temporario) and (categoria in self.suportadas)
            pasta_rel = caminho_rel.rsplit("\\", 1)[0] if "\\" in caminho_rel else ""
            derivados.append({
                "item": item,
                "caminho_rel": caminho_rel,
                "biblioteca": caminho_rel.split("\\", 1)[0] if "\\" in caminho_rel else "",
                "pasta_rel": pasta_rel,
                "nome": nome,
                "ext": ext,
                "categoria": categoria,
                "nome_normalizado": cu.normalizar_nome(nome),
                "profundidade": caminho_rel.count("\\") + 1,
                "comprimento_caminho": len(self.raiz) + 1 + len(caminho_rel),
                "temporario": int(temporario),
                "suportado": int(suportado),
                "estado": classificacao.estado_inicial(temporario, suportado),
            })

        # Prioridade: uma unica resolucao para todas as pastas do lote.
        resolvidas = classificacao.resolver_arvore(
            self.regras, {d["pasta_rel"] for d in derivados})
        for d in derivados:
            dados = resolvidas.get(d["pasta_rel"], {})
            d["prioridade"] = dados.get("prioridade", 3)
            d["motivo_prioridade"] = dados.get("motivo", "acervo geral")

        self.resolvidas = resolvidas
        self._medir(derivados)
        return derivados

    def _medir(self, derivados: list[dict]) -> None:
        por_estado: dict = {}
        por_prioridade: dict = {}
        for d in derivados:
            por_estado[d["estado"]] = por_estado.get(d["estado"], 0) + 1
            rotulo = ROTULOS[d["prioridade"]]
            por_prioridade[rotulo] = por_prioridade.get(rotulo, 0) + 1
        self.contadores["por_estado"] = por_estado
        self.contadores["por_prioridade"] = por_prioridade

        # Caminho que ja pertence a outra linha: o arquivo foi apagado e
        # subido de novo, ganhando `item_id` novo. Sao itens DIFERENTES
        # (`DEC-015`) — a linha velha fica com o historico dela.
        reaproveitados = 0
        for d in derivados:
            if self.con.execute("SELECT 1 FROM arquivo WHERE caminho_rel=? LIMIT 1",
                                (d["caminho_rel"],)).fetchone():
                reaproveitados += 1
        self.contadores["caminho_reaproveitado"] = reaproveitados

        # Ja existe conteudo extraido com este hash? Quem vai aproveitar e o
        # worker, antes de baixar. Aqui so contamos.
        cobertos = 0
        for d in derivados:
            item = d["item"]
            if not item["quick_xor"]:
                continue
            if self.con.execute(
                    "SELECT 1 FROM conteudo WHERE quick_xor=? AND tamanho_bytes=? "
                    "AND estado='EXTRAIDO' LIMIT 1",
                    (item["quick_xor"], item["tamanho_bytes"])).fetchone():
                cobertos += 1
        self.contadores["conteudo_ja_extraido"] = cobertos

        # Pastas que o catalogo ainda nao conhece.
        novas = 0
        for caminho in {d["pasta_rel"] for d in derivados}:
            if not self.con.execute("SELECT 1 FROM pasta WHERE caminho_rel=?",
                                    (caminho,)).fetchone():
                novas += 1
        self.contadores["pastas_novas"] = novas

    # -- 3. gravar ------------------------------------------------------
    def inserir(self, derivados: list[dict]) -> int:
        agora = cu.agora_iso()
        inseridos = 0
        for d in derivados:
            item = d["item"]
            cur = self.con.execute(
                "INSERT INTO arquivo (caminho_rel, biblioteca, pasta_rel, nome, ext, "
                "categoria, nome_normalizado, profundidade, comprimento_caminho, "
                "tamanho_bytes, mtime_utc, ctime_utc, online, temporario, suportado, "
                "prioridade, estado, presente, primeira_vez_em, ultima_vez_em, "
                "ultima_execucao_id, drive_id, item_id, parent_id, caminho_nuvem, "
                "quick_xor, etag, ctag, web_url, criado_por, modificado_por, "
                "criado_em_nuvem, modificado_em_nuvem, presente_nuvem, origem, "
                "visto_nuvem_em) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,1,'graph',?)",
                (d["caminho_rel"], d["biblioteca"], d["pasta_rel"], d["nome"], d["ext"],
                 d["categoria"], d["nome_normalizado"], d["profundidade"],
                 d["comprimento_caminho"], item["tamanho_bytes"], item["modificado_em"],
                 item["criado_em"], d["temporario"], d["suportado"], d["prioridade"],
                 d["estado"], agora, agora, self.execucao_id, item["drive_id"],
                 item["item_id"], item["parent_id"], item["caminho_rel"],
                 item["quick_xor"], item["etag"], item["ctag"], item["web_url"],
                 item["criado_por"], item["modificado_por"], item["criado_em"],
                 item["modificado_em"], agora))
            arquivo_id = int(cur.lastrowid)
            self.con.execute(
                "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, detalhe, em) "
                "VALUES (?,?,?,?,?)",
                (arquivo_id, self.execucao_id, "ingerido_da_nuvem",
                 json.dumps({"item_id": item["item_id"], "drive_id": item["drive_id"],
                             "caminho": d["caminho_rel"], "estado": d["estado"],
                             "prioridade": ROTULOS[d["prioridade"]],
                             "motivo_prioridade": d["motivo_prioridade"],
                             "detectado_por": "delta"},
                            ensure_ascii=False), agora))
            inseridos += 1
            if inseridos % self.commit_a_cada == 0:
                self.con.commit()
        self.con.commit()
        self.contadores["inseridos"] = inseridos
        return inseridos

    def atualizar_pastas(self, derivados: list[dict]) -> int:
        """Poe as pastas novas em `pasta` e reconta as afetadas.

        Mesma semantica de `02_classificar.classificar()`: `arquivos`,
        `arquivos_suportados` e `bytes` contam os arquivos DIRETOS da pasta.
        Nao apaga linha nenhuma — so insere e atualiza as que mudaram.
        """
        alvos = classificacao.expandir_ancestrais({d["pasta_rel"] for d in derivados})
        resolvidas = self.resolvidas
        faltando = [c for c in alvos if c not in resolvidas]
        if faltando:
            resolvidas = classificacao.resolver_arvore(self.regras, alvos)
        agora = cu.agora_iso()
        tocadas = 0
        for caminho in sorted(alvos, key=lambda c: (c.count("\\"), c)):
            dados = resolvidas[caminho]
            partes = caminho.split("\\") if caminho else []
            contagem = self.con.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(tamanho_bytes),0) b, "
                "COALESCE(SUM(suportado),0) sup FROM arquivo "
                "WHERE presente=1 AND pasta_rel=?", (caminho,)).fetchone()
            self.con.execute(
                "INSERT INTO pasta (caminho_rel, biblioteca, nome, nivel, obra, "
                "categoria_doc, critica, prioridade, motivo, arquivos, "
                "arquivos_suportados, bytes, classificado_em) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(caminho_rel) DO UPDATE SET obra=excluded.obra, "
                "categoria_doc=excluded.categoria_doc, critica=excluded.critica, "
                "prioridade=excluded.prioridade, motivo=excluded.motivo, "
                "arquivos=excluded.arquivos, "
                "arquivos_suportados=excluded.arquivos_suportados, "
                "bytes=excluded.bytes, classificado_em=excluded.classificado_em",
                (caminho, partes[0] if partes else "",
                 partes[-1] if partes else "<raiz>", len(partes), dados["obra"],
                 dados["categoria"], int(bool(dados["critica"])), dados["prioridade"],
                 dados["motivo"], contagem["n"], contagem["sup"], contagem["b"], agora))
            tocadas += 1
        self.con.commit()
        return tocadas


# ----------------------------------------------------------------------
# Relatorio
# ----------------------------------------------------------------------

def retrato(con) -> dict:
    return {
        "arquivos": con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0],
        "presentes": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE presente=1").fetchone()[0],
        "QUEUED": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='QUEUED'").fetchone()[0],
        "UNSUPPORTED": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='UNSUPPORTED'").fetchone()[0],
        "SKIPPED": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='SKIPPED'").fetchone()[0],
        "MISSING": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='MISSING'").fetchone()[0],
        "COMPLETED": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0],
        "extracoes": con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0],
        "trechos": con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0],
        "conteudos": con.execute("SELECT COUNT(*) FROM conteudo").fetchone()[0],
        "fila": con.execute("SELECT COUNT(*) FROM v_fila_nuvem").fetchone()[0],
        "pastas": con.execute("SELECT COUNT(*) FROM pasta").fetchone()[0],
    }


def imprimir(ing: Ingestao, derivados: list[dict], antes: dict,
             depois: dict | None, conferido: bool | None) -> None:
    c = ing.contadores
    print("=" * 70)
    print("INGESTAO DOS NOVOS — espelho `item_nuvem` -> catalogo `arquivo`")
    print("=" * 70)
    print(f"  {'itens so no espelho':<36} {c['pendentes']:>8}")
    print(f"  {'linhas preparadas':<36} {len(derivados):>8}")
    print(f"  {'pastas ainda sem linha em `pasta`':<36} {c['pastas_novas']:>8}")
    print(f"  {'caminho ja usado por outra linha':<36} {c['caminho_reaproveitado']:>8}")
    print(f"  {'conteudo ja extraido (o worker cobre)':<36} "
          f"{c['conteudo_ja_extraido']:>8}")
    print(f"  {'sem regra de biblioteca no mapa':<36} "
          f"{c['sem_regra_de_biblioteca']:>8}")
    print()
    print("  por estado inicial:")
    for estado, n in sorted(c["por_estado"].items(), key=lambda x: -x[1]):
        print(f"    {estado:<20} {n:>8}")
    print()
    print("  por prioridade:")
    for rotulo in ("P0", "P1", "P2", "P3", "P4"):
        if rotulo in c["por_prioridade"]:
            print(f"    {rotulo:<20} {c['por_prioridade'][rotulo]:>8}")
    print()
    if derivados:
        print("  primeiros 5:")
        for d in derivados[:5]:
            print(f"    {ROTULOS[d['prioridade']]} {d['estado']:<12} "
                  f"{d['caminho_rel'][:80]}")
        print()
    if depois is None:
        print("  SIMULACAO — nada foi gravado. Para gravar:  --aplicar")
        return
    print("  APLICADO. Total inserido conferido:", "SIM" if conferido else "NAO !!")
    print()
    print(f"  {'':<13}{'antes':>10}{'depois':>10}")
    for chave in antes:
        marca = " " if antes[chave] == depois[chave] else "*"
        print(f"  {marca}{chave:<12}{antes[chave]:>10}{depois[chave]:>10}")
    print()
    print("  Preservado (ingerir arquivo novo nao pode mexer nisto):")
    for chave in ("extracoes", "trechos", "conteudos", "COMPLETED", "MISSING"):
        estado = "OK" if antes[chave] == depois[chave] else "!! MUDOU"
        print(f"    {chave:<12} {estado}")


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Incorpora ao catalogo os arquivos que so existem no espelho")
    ap.add_argument("--config", default=None)
    ap.add_argument("--classificacao", default=None)
    ap.add_argument("--aplicar", action="store_true",
                    help="grava de verdade (sem isto, so simula)")
    ap.add_argument("--limite", type=int, default=0,
                    help="processa no maximo N itens (para prova controlada)")
    ap.add_argument("--quieto", action="store_true")
    args = ap.parse_args(argv)

    cfg = cu.carregar_config(args.config)
    cu.assert_escrita_permitida(cfg["_banco_path"], cfg["_raiz_path"])
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='item_nuvem'").fetchone():
        print("banco sem schema v3. Rode antes:  python scripts\\03_migrar_v3.py")
        return 2

    regras = classificacao.carregar_regras(args.classificacao)
    execucao_id = cu.abrir_execucao(con, "ingerir_novos", "", vars(args))
    log = cu.Log(cfg["_logs_path"], "ingerir_novos", con, execucao_id,
                 verboso=not args.quieto)

    ing = Ingestao(con, cfg, regras, execucao_id, log)
    antes = retrato(con)
    derivados = ing.preparar(ing.pendentes(args.limite))

    if not args.aplicar:
        imprimir(ing, derivados, antes, None, None)
        cu.fechar_execucao(con, execucao_id, "concluida",
                           {"arquivos": len(derivados)}, 0.0)
        log.fechar()
        con.close()
        return 0

    esperado = len(derivados)
    inseridos = ing.inserir(derivados)
    ing.atualizar_pastas(derivados)
    depois = retrato(con)
    conferido = (inseridos == esperado
                 and depois["arquivos"] - antes["arquivos"] == esperado)
    imprimir(ing, derivados, antes, depois, conferido)
    log.info(f"{inseridos} arquivo(s) incorporados ao catalogo")

    cu.fechar_execucao(con, execucao_id, "concluida" if conferido else "erro",
                       {"arquivos": inseridos, "novos": inseridos}, 0.0)
    log.fechar()
    con.close()
    return 0 if conferido else 1


if __name__ == "__main__":
    raise SystemExit(main())
