"""
17_aplicar_delta — leva o que o delta descobriu do espelho para o catalogo.

`12_scan_graph.py` atualiza `item_nuvem` (o espelho da nuvem). Este script
propaga isso para `arquivo` (o catalogo), pela identidade canonica
`(drive_id, item_id)` — nunca por caminho (`DEC-015`).

    python scripts\\17_aplicar_delta.py              # simula e mostra os numeros
    python scripts\\17_aplicar_delta.py --aplicar    # grava

O que ele faz:

  sumiu no espelho      -> `MISSING`, `presente=0`, `presente_nuvem=0`, com
                           evento `ausente_na_nuvem` guardando o estado
                           anterior (`DEC-029`). Nunca `DELETE`.
  voltou no espelho     -> restaura o estado anterior gravado no evento e
                           emite `reapareceu_na_nuvem`.
  renomeado / movido    -> atualiza `caminho_nuvem` e `web_url`. A identidade
                           NAO muda, o estado de processamento NAO muda, e
                           nada volta para a fila por ter mudado de lugar.
                           E (A-6) o caminho DO CATALOGO acompanha:
                           `caminho_rel`, `pasta_rel`, biblioteca, extensao,
                           categoria e prioridade sao refeitos pelo mesmo
                           derivador do `19_ingerir_novos`, com evento
                           `caminho_realinhado` (antes/depois).
  conteudo alterado     -> `CHANGED` (volta para a fila), com o hash novo, e
                           SEM o vinculo ao conteudo antigo (A-5).
  representante some    -> o conteudo e recontado e uma copia viva assume na
                           mesma transacao, com os trechos (A-1).

O que ele NAO faz, de proposito: **inserir arquivos novos**. Isso exige
classificacao, prioridade e biblioteca, que sao do `01`/`02`. Aqui os novos
sao apenas contados e reportados.

Simula por padrao. So grava com `--aplicar`, e confere que o total afetado e
exatamente o que foi anunciado.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

# Estados que nao voltam para a fila so porque o conteudo mudou: ja estao
# fora do fluxo por decisao propria.
INTOCAVEIS = ("SKIPPED", "UNSUPPORTED")


def estado_anterior_de(con, arquivo_id: int) -> str | None:
    """O estado que o arquivo tinha quando foi marcado como ausente."""
    linha = con.execute(
        "SELECT detalhe FROM evento_arquivo WHERE arquivo_id=? "
        "AND evento='ausente_na_nuvem' ORDER BY id DESC LIMIT 1",
        (arquivo_id,)).fetchone()
    if not linha or not linha[0]:
        return None
    try:
        return (json.loads(linha[0]) or {}).get("estado_anterior")
    except (ValueError, TypeError):
        return None


class Propagacao:
    def __init__(self, con, execucao_id: int, aplicar: bool, cfg: dict | None = None):
        self.con = con
        self.execucao_id = execucao_id
        self.aplicar = aplicar
        # `cfg` habilita o realinhamento do caminho do catalogo (A-6), que
        # precisa do mapa de drives e das regras de classificacao. Sem ele o
        # comportamento e o de antes: so os campos da nuvem acompanham o move.
        self.cfg = cfg
        self.contadores = {"sumiram": 0, "voltaram": 0, "recaminhados": 0,
                           "alterados": 0, "novos_no_espelho": 0, "reeleitos": 0,
                           "realinhados": 0}

    def evento(self, arquivo_id: int, evento: str, detalhe: dict):
        self.con.execute(
            "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, detalhe, em) "
            "VALUES (?,?,?,?,?)",
            (arquivo_id, self.execucao_id, evento,
             json.dumps(detalhe, ensure_ascii=False, default=str), cu.agora_iso()))

    # -- 1. sumiu no espelho -> MISSING ---------------------------------
    def sumiram(self) -> list:
        return self.con.execute(
            "SELECT a.id, a.estado, a.caminho_rel, a.item_id, i.removido_em, "
            "       i.caminho_rel AS caminho_nuvem_espelho "
            "FROM arquivo a JOIN item_nuvem i "
            "  ON i.drive_id = a.drive_id AND i.item_id = a.item_id "
            "WHERE i.presente = 0 AND a.presente = 1 "
            "ORDER BY a.id").fetchall()

    def marcar_sumidos(self, linhas) -> int:
        agora = cu.agora_iso()
        for linha in linhas:
            self.con.execute(
                "UPDATE arquivo SET estado='MISSING', presente=0, presente_nuvem=0, "
                "proxima_tentativa_em=NULL, alterado_em=? WHERE id=?",
                (agora, linha["id"]))
            # T-11 (A-1): reconta o conteudo e, se este era o representante,
            # uma copia viva assume — com os trechos do indice — AQUI, na
            # mesma transacao, e nao so na proxima subida do worker.
            dedupe = cu.ocorrencia_saiu(self.con, linha["id"], self.execucao_id,
                                        "representante ausente no delta")
            if dedupe.get("novo_representante"):
                self.contadores["reeleitos"] += 1
            self.evento(linha["id"], "ausente_na_nuvem",
                        {"estado_anterior": linha["estado"],
                         "motivo": "o delta do Graph nao devolveu mais este item",
                         "detectado_por": "delta",
                         "removido_em": linha["removido_em"],
                         "caminho": linha["caminho_rel"], **dedupe})
        self.contadores["sumiram"] = len(linhas)
        return len(linhas)

    # -- 2. voltou no espelho -> restaura -------------------------------
    def voltaram(self) -> list:
        return self.con.execute(
            "SELECT a.id, a.estado, a.caminho_rel FROM arquivo a JOIN item_nuvem i "
            "  ON i.drive_id = a.drive_id AND i.item_id = a.item_id "
            "WHERE i.presente = 1 AND a.presente = 0 AND a.estado = 'MISSING' "
            "ORDER BY a.id").fetchall()

    def restaurar(self, linhas) -> int:
        agora = cu.agora_iso()
        for linha in linhas:
            anterior = estado_anterior_de(self.con, linha["id"]) or "QUEUED"
            self.con.execute(
                "UPDATE arquivo SET estado=?, presente=1, presente_nuvem=1, "
                "alterado_em=? WHERE id=?", (anterior, agora, linha["id"]))
            # Voltou a contar como ocorrencia viva: o contador do conteudo
            # sobe, e um conteudo sem representante vivo ganha um.
            dedupe = cu.ocorrencia_voltou(self.con, linha["id"], self.execucao_id)
            self.evento(linha["id"], "reapareceu_na_nuvem",
                        {"estado_restaurado": anterior, "detectado_por": "delta",
                         **dedupe})
        self.contadores["voltaram"] = len(linhas)
        return len(linhas)

    # -- 3. renomeado ou movido -> so o caminho --------------------------
    def recaminhados(self) -> list:
        return self.con.execute(
            "SELECT a.id, a.caminho_nuvem, a.web_url, a.nome, i.caminho_rel AS novo, "
            "       i.web_url AS novo_url, i.nome AS novo_nome "
            "FROM arquivo a JOIN item_nuvem i "
            "  ON i.drive_id = a.drive_id AND i.item_id = a.item_id "
            "WHERE i.presente = 1 AND a.presente = 1 "
            "  AND (IFNULL(a.caminho_nuvem,'') <> IFNULL(i.caminho_rel,'') "
            "       OR IFNULL(a.nome,'') <> IFNULL(i.nome,'')) "
            "ORDER BY a.id").fetchall()

    def recaminhar(self, linhas) -> int:
        agora = cu.agora_iso()
        for linha in linhas:
            self.con.execute(
                "UPDATE arquivo SET caminho_nuvem=?, web_url=?, nome=?, "
                "caminho_anterior=?, alterado_em=? WHERE id=?",
                (linha["novo"], linha["novo_url"], linha["novo_nome"],
                 linha["caminho_nuvem"], agora, linha["id"]))
            self.evento(linha["id"], "recaminhado_na_nuvem",
                        {"de": linha["caminho_nuvem"], "para": linha["novo"],
                         "nome_antes": linha["nome"], "nome_depois": linha["novo_nome"],
                         "detectado_por": "delta"})
        self.contadores["recaminhados"] = len(linhas)
        return len(linhas)

    # -- 3b. o caminho DO CATALOGO acompanha (A-6 / T-14) ----------------
    def _ingestao(self):
        """O mesmo derivador de caminho/classificacao do 19_ingerir_novos —
        um item movido fica exatamente como ficaria se entrasse hoje."""
        ingerir = cu.importar_script("19_ingerir_novos")
        regras = ingerir.classificacao.carregar_regras(self.cfg.get("classificacao"))
        return ingerir.Ingestao(self.con, self.cfg, regras, self.execucao_id)

    def desalinhados(self) -> list:
        """Ocorrencias vivas cujo `caminho_rel` nao e o que o caminho ATUAL na
        nuvem produz. `caminho_rel`/`pasta_rel` sao o que a busca mostra e a
        chave de junção com `pasta` (prioridade); se ficam no lugar antigo,
        um arquivo movido para `0 - Obsoleto` mantem a prioridade da pasta de
        trabalho. Medido no banco real em 2026-10-04: 149 divergem, e os 149
        tem evento `recaminhado_na_nuvem` — nenhum falso positivo."""
        if not self.cfg:
            return []
        ing = self._ingestao()
        saida = []
        for linha in self.con.execute(
                "SELECT a.id AS arquivo_id, a.caminho_rel AS atual, i.* "
                "FROM arquivo a JOIN item_nuvem i "
                "  ON i.drive_id = a.drive_id AND i.item_id = a.item_id "
                "WHERE i.presente = 1 AND a.presente = 1 AND i.eh_pasta = 0 "
                "ORDER BY a.id"):
            if ing.caminho_do_catalogo(linha) != linha["atual"]:
                saida.append(linha)
        return saida

    def realinhar(self, linhas) -> int:
        if not linhas:
            return 0
        ing = self._ingestao()
        derivados = ing.preparar(linhas)
        pastas_tocadas = []
        for d in derivados:
            linha = d["item"]                   # a propria linha de desalinhados()
            a = self.con.execute(
                "SELECT caminho_rel, pasta_rel, prioridade, ext, categoria, suportado, "
                "estado FROM arquivo WHERE id=?", (linha["arquivo_id"],)).fetchone()
            estado = a["estado"]
            # O nome pode ter trocado de extensao: o extrator certo e outro.
            # So o estado de FILA acompanha; terminal (COMPLETED etc.) fica.
            if a["suportado"] and not d["suportado"] and estado in (
                    "QUEUED", "CHANGED", "READY", "WAITING_HYDRATION", "ERROR_RETRYABLE"):
                estado = "UNSUPPORTED"
            elif not a["suportado"] and d["suportado"] and estado == "UNSUPPORTED":
                estado = "QUEUED"
            self.con.execute(
                "UPDATE arquivo SET caminho_rel=?, biblioteca=?, pasta_rel=?, "
                "nome_normalizado=?, profundidade=?, comprimento_caminho=?, ext=?, "
                "categoria=?, suportado=?, prioridade=?, estado=?, alterado_em=? WHERE id=?",
                (d["caminho_rel"], d["biblioteca"], d["pasta_rel"], d["nome_normalizado"],
                 d["profundidade"], d["comprimento_caminho"], d["ext"], d["categoria"],
                 d["suportado"], d["prioridade"], estado, cu.agora_iso(),
                 linha["arquivo_id"]))
            # O indice mostra o caminho: o trecho acompanha (texto intacto).
            self.con.execute("UPDATE texto_fts SET caminho_rel=? WHERE arquivo_id=?",
                             (d["caminho_rel"], linha["arquivo_id"]))
            self.evento(linha["arquivo_id"], "caminho_realinhado",
                        {"de": a["caminho_rel"], "para": d["caminho_rel"],
                         "pasta_antes": a["pasta_rel"], "pasta_depois": d["pasta_rel"],
                         "prioridade_antes": a["prioridade"],
                         "prioridade_depois": d["prioridade"],
                         "motivo_prioridade": d.get("motivo_prioridade"),
                         "categoria_antes": a["categoria"], "categoria_depois": d["categoria"],
                         "estado_antes": a["estado"], "estado_depois": estado,
                         "detectado_por": "delta"})
            pastas_tocadas += [{"pasta_rel": a["pasta_rel"]}, {"pasta_rel": d["pasta_rel"]}]
        # contagens das pastas de origem e de destino (nenhuma pasta apagada)
        ing.atualizar_pastas(pastas_tocadas)
        self.contadores["realinhados"] = len(derivados)
        return len(derivados)

    # -- 4. conteudo alterado -> volta para a fila -----------------------
    def alterados(self) -> list:
        marcas = ",".join("?" * len(INTOCAVEIS))
        return self.con.execute(
            f"SELECT a.id, a.estado, a.caminho_rel, a.quick_xor, a.tamanho_bytes, "
            f"       i.quick_xor AS novo_hash, i.tamanho_bytes AS novo_tamanho "
            f"FROM arquivo a JOIN item_nuvem i "
            f"  ON i.drive_id = a.drive_id AND i.item_id = a.item_id "
            f"WHERE i.presente = 1 AND a.presente = 1 AND i.eh_pasta = 0 "
            f"  AND a.estado NOT IN ({marcas}) "
            # Espelho sem hash nao e prova de mudanca: o worker calcula o
            # quickXor dos bytes quando o inventario nao tinha (R4) e grava no
            # catalogo. Comparar com NULL marcaria CHANGED a cada sync e
            # rebaixaria o item para sempre. O tamanho continua valendo.
            f"  AND ((i.quick_xor IS NOT NULL "
            f"        AND IFNULL(a.quick_xor,'~') <> i.quick_xor) "
            f"       OR IFNULL(a.tamanho_bytes,-1) <> IFNULL(i.tamanho_bytes,-1)) "
            f"ORDER BY a.id", INTOCAVEIS).fetchall()

    def requeue(self, linhas) -> int:
        agora = cu.agora_iso()
        for linha in linhas:
            self.con.execute(
                "UPDATE arquivo SET estado='CHANGED', quick_xor=?, tamanho_bytes=?, "
                "tentativas=0, ultimo_erro=NULL, proxima_tentativa_em=NULL, "
                "alterado_em=? WHERE id=?",
                (linha["novo_hash"], linha["novo_tamanho"], agora, linha["id"]))
            # T-13 (A-5): bytes novos nao carregam o conteudo antigo. Desliga o
            # vinculo, reconta o grupo que ele deixou e, se era o representante,
            # passa o posto (e os trechos) a uma copia viva. O conteudo_id
            # antigo fica no evento — o historico nao se perde.
            dedupe = cu.desvincular_conteudo(self.con, linha["id"], self.execucao_id,
                                             "representante alterado na nuvem")
            if dedupe.get("novo_representante"):
                self.contadores["reeleitos"] += 1
            self.evento(linha["id"], "alterado_na_nuvem",
                        {"estado_anterior": linha["estado"],
                         "hash_antes": linha["quick_xor"], "hash_depois": linha["novo_hash"],
                         "tamanho_antes": linha["tamanho_bytes"],
                         "tamanho_depois": linha["novo_tamanho"],
                         "detectado_por": "delta", **dedupe})
        self.contadores["alterados"] = len(linhas)
        return len(linhas)

    # -- 5. novos: so contados ------------------------------------------
    def novos_no_espelho(self) -> int:
        n = self.con.execute(
            "SELECT COUNT(*) FROM item_nuvem i WHERE i.presente=1 AND i.eh_pasta=0 "
            "AND NOT EXISTS (SELECT 1 FROM arquivo a WHERE a.drive_id=i.drive_id "
            "                AND a.item_id=i.item_id)").fetchone()[0]
        self.contadores["novos_no_espelho"] = n
        return n


def executar(args, con, p: "Propagacao") -> tuple[int, str]:
    """O corpo do script. Devolve (codigo de saida, status da execucao)."""
    antes = {
        "arquivos": con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0],
        "presentes": con.execute("SELECT COUNT(*) FROM arquivo WHERE presente=1").fetchone()[0],
        "MISSING": con.execute("SELECT COUNT(*) FROM arquivo WHERE estado='MISSING'").fetchone()[0],
        "COMPLETED": con.execute("SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0],
        "extracoes": con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0],
        "trechos": con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0],
        "conteudos": con.execute("SELECT COUNT(*) FROM conteudo").fetchone()[0],
        "fila": con.execute("SELECT COUNT(*) FROM v_fila_nuvem").fetchone()[0],
    }

    sumiram, voltaram = p.sumiram(), p.voltaram()
    recam, alter = p.recaminhados(), p.alterados()
    desal = p.desalinhados()
    novos = p.novos_no_espelho()

    print("=" * 70)
    print("PROPAGACAO DO DELTA — espelho `item_nuvem` -> catalogo `arquivo`")
    print("=" * 70)
    print(f"  {'sumiram (-> MISSING)':<34} {len(sumiram):>8}")
    print(f"  {'voltaram (estado restaurado)':<34} {len(voltaram):>8}")
    print(f"  {'renomeados/movidos (so caminho)':<34} {len(recam):>8}")
    print(f"  {'conteudo alterado (-> CHANGED)':<34} {len(alter):>8}")
    print(f"  {'caminho do catalogo realinhado':<34} {len(desal):>8}")
    print(f"  {'novos no espelho (NAO inseridos)':<34} {novos:>8}")
    print()

    if sumiram:
        por_estado: dict = {}
        for linha in sumiram:
            por_estado[linha["estado"]] = por_estado.get(linha["estado"], 0) + 1
        print("  os que sumiram, por estado atual:")
        for estado, n in sorted(por_estado.items(), key=lambda x: -x[1]):
            print(f"    {estado:<20} {n:>8}")
        print()
        print("  primeiros 5:")
        for linha in sumiram[:5]:
            print(f"    #{linha['id']:<7} {linha['estado']:<12} {linha['caminho_rel'][:70]}")
        print()

    if not args.aplicar:
        print("  SIMULACAO — nada foi gravado. Para gravar:  --aplicar")
        return 0, "simulacao"

    esperado = {"sumiram": len(sumiram), "voltaram": len(voltaram),
                "recaminhados": len(recam), "alterados": len(alter),
                "realinhados": len(desal)}
    p.marcar_sumidos(sumiram)
    p.restaurar(voltaram)
    p.recaminhar(recam)
    p.realinhar(desal)
    p.requeue(alter)
    con.commit()

    conferido = all(p.contadores[k] == v for k, v in esperado.items())
    depois = {
        "arquivos": con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0],
        "presentes": con.execute("SELECT COUNT(*) FROM arquivo WHERE presente=1").fetchone()[0],
        "MISSING": con.execute("SELECT COUNT(*) FROM arquivo WHERE estado='MISSING'").fetchone()[0],
        "COMPLETED": con.execute("SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0],
        "extracoes": con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0],
        "trechos": con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0],
        "conteudos": con.execute("SELECT COUNT(*) FROM conteudo").fetchone()[0],
        "fila": con.execute("SELECT COUNT(*) FROM v_fila_nuvem").fetchone()[0],
    }
    print("  APLICADO. Total afetado conferido:", "SIM" if conferido else "NAO !!")
    print()
    print(f"  {'':<12}{'antes':>10}{'depois':>10}")
    for chave in antes:
        marca = " " if antes[chave] == depois[chave] else "*"
        print(f"  {marca}{chave:<11}{antes[chave]:>10}{depois[chave]:>10}")
    print()
    print("  Preservado (nada disto pode mudar por causa de ausencia):")
    for chave in ("extracoes", "trechos", "conteudos"):
        estado = "OK" if antes[chave] == depois[chave] else "!! MUDOU"
        print(f"    {chave:<12} {estado}")
    print(f"  representantes reeleitos na hora (T-11/T-13): {p.contadores['reeleitos']}")
    return (0, "concluida") if conferido else (1, "divergente")


def main() -> int:
    ap = argparse.ArgumentParser(description="Propaga o delta do espelho para o catalogo")
    ap.add_argument("--config", default=None)
    ap.add_argument("--aplicar", action="store_true",
                    help="grava de verdade (sem isto, so simula)")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    execucao_id = cu.abrir_execucao(con, "aplicar_delta", "", vars(args))
    p = Propagacao(con, execucao_id, args.aplicar, cfg=cfg)
    t0 = time.perf_counter()
    status = "erro"
    try:
        codigo, status = executar(args, con, p)
        return codigo
    finally:
        # M-5: toda execucao aberta e fechada — inclusive a simulacao (o modo
        # PADRAO) e a que morre com excecao. `execucao.status` e estado
        # explicito (REGRA 7), nao pode ficar 'em_andamento' para sempre.
        try:
            con.rollback()
            cu.fechar_execucao(con, execucao_id, status, p.contadores,
                               time.perf_counter() - t0)
        finally:
            con.close()


if __name__ == "__main__":
    raise SystemExit(main())
