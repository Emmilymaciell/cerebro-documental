"""
12_scan_graph — E4: inventario do SharePoint pela nuvem, por delta query.

Le `GET /drives/{id}/root/delta`, pagina a pagina, e grava em `item_nuvem`.
Nao baixa arquivo nenhum: nome, tamanho, datas, autor e o `quickXorHash`
vem na propria listagem.

IDENTIDADE = (drive_id, item_id). Caminho e nome sao atributos: renomear
ou mover NAO cria item novo — gera evento e atualiza o caminho.

Checkpoint (REGRA 8):
  - a cada pagina: grava os itens, o `next_link` e commita;
  - so no fim da rodada o `delta_link` e sobrescrito.
  Erro de API no meio nunca destroi o checkpoint valido anterior.

Retomada automatica: se a ultima rodada ficou `em_andamento` com `next_link`,
a proxima execucao continua exatamente de onde parou.

    python scripts\\12_scan_graph.py                  # auto (retoma/incremental/completo)
    python scripts\\12_scan_graph.py --completo       # ignora tokens e varre tudo
    python scripts\\12_scan_graph.py --incremental    # so o que mudou
    python scripts\\12_scan_graph.py --limite 5000    # para cedo (fica retomavel)
    python scripts\\12_scan_graph.py --drive Programa
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402
import graph_ro  # noqa: E402

CAMPOS = graph_ro.CAMPOS_ITEM
PAGINA = 999


def resync_exigido(exc) -> bool:
    """HTTP 410 `resyncRequired`: o token de delta morreu.

    O proprio Graph diz o que fazer — "Replace any local items with the
    server's version (including deletes)". Ou seja: recomecar do zero E
    tratar como removido tudo que a varredura completa nao devolver. Nao ha
    como pedir "so o que mudou" a partir de um token que o servidor jogou
    fora; insistir no incremental so repete o 410.
    """
    return (getattr(exc, "status", 0) == 410
            or str(getattr(exc, "codigo", "")).lower() == "resyncrequired")


def _autor(bloco: dict | None) -> str | None:
    if not bloco:
        return None
    usuario = bloco.get("user") or bloco.get("application") or {}
    return usuario.get("displayName") or usuario.get("email")


class InventarioNuvem:
    """Aplica paginas de delta em `item_nuvem`, com evento e checkpoint."""

    def __init__(self, graph, con, execucao_id: int, log, limite: int = 0):
        self.graph = graph
        self.con = con
        self.execucao_id = execucao_id
        self.log = log
        self.limite = limite
        self.contadores = {"itens": 0, "novos": 0, "alterados": 0, "renomeados": 0,
                           "movidos": 0, "removidos": 0, "reapareceram": 0,
                           "paginas": 0, "erros": 0, "removidos_cascata": 0,
                           "removidos_varredura": 0, "resyncs": 0}

    # -- drives ---------------------------------------------------------
    def registrar_drives(self, site_id: str) -> list[dict]:
        drives = self.graph.drives(site_id)
        for d in drives:
            self.con.execute(
                "INSERT INTO drive (drive_id, nome, tipo, web_url, site_id, usado_bytes, "
                "no_escopo, atualizado_em) VALUES (?,?,?,?,?,?,1,?) "
                "ON CONFLICT(drive_id) DO UPDATE SET nome=excluded.nome, "
                "tipo=excluded.tipo, web_url=excluded.web_url, site_id=excluded.site_id, "
                "usado_bytes=excluded.usado_bytes, atualizado_em=excluded.atualizado_em",
                (d["id"], d.get("name"), d.get("driveType"), d.get("webUrl"), site_id,
                 (d.get("quota") or {}).get("used"), cu.agora_iso()))
            self.con.execute(
                "INSERT OR IGNORE INTO delta_token (drive_id, status, atualizado_em) "
                "VALUES (?, 'nunca_rodou', ?)", (d["id"], cu.agora_iso()))
        self.con.commit()
        return drives

    # -- checkpoint -----------------------------------------------------
    def _token(self, drive_id: str):
        return self.con.execute("SELECT * FROM delta_token WHERE drive_id=?",
                                (drive_id,)).fetchone()

    def _gravar_token(self, drive_id: str, **campos):
        campos["atualizado_em"] = cu.agora_iso()
        sets = ", ".join(f"{k}=?" for k in campos)
        self.con.execute(f"UPDATE delta_token SET {sets} WHERE drive_id=?",
                         list(campos.values()) + [drive_id])
        self.con.commit()

    def url_inicial(self, drive_id: str, modo: str) -> tuple[str, str]:
        """Devolve (url, modo_efetivo)."""
        token = self._token(drive_id)
        base = f"/drives/{drive_id}/root/delta?$top={PAGINA}&$select={CAMPOS}"
        if modo == "completo":
            return base, "completo"
        if token and token["status"] == "em_andamento" and token["next_link"] and modo == "auto":
            return token["next_link"], "retomada"
        if token and token["delta_link"] and modo in ("auto", "incremental"):
            return token["delta_link"], "incremental"
        if modo == "incremental":
            raise RuntimeError("nao ha delta_link salvo: rode uma vez sem --incremental")
        return base, "completo"

    # -- aplicacao de uma pagina ----------------------------------------
    def aplicar(self, drive_id: str, itens: list[dict]) -> None:
        agora = cu.agora_iso()
        for item in itens:
            item_id = item.get("id")
            if not item_id:
                continue
            pai = item.get("parentReference") or {}
            parent_id = pai.get("id")
            nome = item.get("name") or ""
            antigo = self.con.execute(
                "SELECT nome, parent_id, tamanho_bytes, quick_xor, ctag, presente, caminho_rel "
                "FROM item_nuvem WHERE drive_id=? AND item_id=?", (drive_id, item_id)).fetchone()

            # --- removido -------------------------------------------------
            if item.get("deleted"):
                if antigo:
                    self.con.execute(
                        "UPDATE item_nuvem SET presente=0, removido_em=?, ultima_vez_em=?, "
                        "execucao_id=? WHERE drive_id=? AND item_id=?",
                        (agora, agora, self.execucao_id, drive_id, item_id))
                    self._evento(drive_id, item_id, "removido",
                                 {"caminho": antigo["caminho_rel"]})
                    self.contadores["removidos"] += 1
                self.contadores["itens"] += 1
                continue

            eh_pasta = 1 if item.get("folder") else 0
            tamanho = int(item.get("size") or 0)
            quick = graph_ro.hash_do_item(item)
            hashes = ((item.get("file") or {}).get("hashes") or {})
            ext = os.path.splitext(nome)[1].lower() if not eh_pasta else ""

            if antigo is None:
                self.con.execute(
                    "INSERT INTO item_nuvem (drive_id, item_id, parent_id, nome, eh_pasta, "
                    "ext, tamanho_bytes, quick_xor, sha1, etag, ctag, web_url, criado_em, "
                    "modificado_em, criado_por, modificado_por, presente, primeira_vez_em, "
                    "ultima_vez_em, execucao_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                    (drive_id, item_id, parent_id, nome, eh_pasta, ext, tamanho, quick,
                     hashes.get("sha1Hash"), item.get("eTag"), item.get("cTag"),
                     item.get("webUrl"), item.get("createdDateTime"),
                     item.get("lastModifiedDateTime"), _autor(item.get("createdBy")),
                     _autor(item.get("lastModifiedBy")), agora, agora, self.execucao_id))
                self._evento(drive_id, item_id, "novo", {"nome": nome})
                self.contadores["novos"] += 1
            else:
                eventos = []
                if antigo["nome"] != nome:
                    eventos.append(("renomeado", {"de": antigo["nome"], "para": nome}))
                    self.contadores["renomeados"] += 1
                if (antigo["parent_id"] or "") != (parent_id or ""):
                    eventos.append(("movido", {"de_pai": antigo["parent_id"],
                                               "para_pai": parent_id}))
                    self.contadores["movidos"] += 1
                mudou_conteudo = (antigo["tamanho_bytes"] != tamanho
                                  or (antigo["quick_xor"] or "") != (quick or "")
                                  or (antigo["ctag"] or "") != (item.get("cTag") or ""))
                if mudou_conteudo and not eh_pasta:
                    eventos.append(("alterado", {"tamanho_antes": antigo["tamanho_bytes"],
                                                 "tamanho_depois": tamanho}))
                    self.contadores["alterados"] += 1
                if not antigo["presente"]:
                    eventos.append(("reapareceu", None))
                    self.contadores["reapareceram"] += 1

                self.con.execute(
                    "UPDATE item_nuvem SET parent_id=?, nome=?, eh_pasta=?, ext=?, "
                    "tamanho_bytes=?, quick_xor=?, sha1=?, etag=?, ctag=?, web_url=?, "
                    "criado_em=?, modificado_em=?, criado_por=?, modificado_por=?, "
                    "presente=1, removido_em=NULL, ultima_vez_em=?, execucao_id=? "
                    "WHERE drive_id=? AND item_id=?",
                    (parent_id, nome, eh_pasta, ext, tamanho, quick, hashes.get("sha1Hash"),
                     item.get("eTag"), item.get("cTag"), item.get("webUrl"),
                     item.get("createdDateTime"), item.get("lastModifiedDateTime"),
                     _autor(item.get("createdBy")), _autor(item.get("lastModifiedBy")),
                     agora, self.execucao_id, drive_id, item_id))
                for evento, detalhe in eventos:
                    self._evento(drive_id, item_id, evento, detalhe)

            self.contadores["itens"] += 1

    def _evento(self, drive_id: str, item_id: str, evento: str, detalhe: dict | None):
        self.con.execute(
            "INSERT INTO evento_nuvem (drive_id, item_id, evento, detalhe, execucao_id, em) "
            "VALUES (?,?,?,?,?,?)",
            (drive_id, item_id, evento,
             json.dumps(detalhe, ensure_ascii=False) if detalhe else None,
             self.execucao_id, cu.agora_iso()))

    # -- caminhos: derivados da cadeia de pais, nao do que a API mandou --
    def recalcular_caminhos(self, drive_id: str) -> int:
        """Monta `caminho_rel` subindo pela cadeia de `parent_id`.

        Por que nao usar o `parentReference.path` de cada item: quando uma
        PASTA e renomeada, o delta pode nao devolver os descendentes. Assim
        o caminho dos filhos sairia velho. Subindo pela cadeia, o caminho
        sempre reflete a arvore que temos.
        """
        linhas = self.con.execute(
            "SELECT item_id, parent_id, nome, caminho_rel FROM item_nuvem "
            "WHERE drive_id=?", (drive_id,)).fetchall()
        if not linhas:
            return 0
        pais = {l["item_id"]: (l["parent_id"], l["nome"] or "") for l in linhas}
        atuais = {l["item_id"]: l["caminho_rel"] for l in linhas}

        memoria: dict[str, str] = {}

        def caminho(item_id: str) -> str:
            visto = []
            atual = item_id
            while atual is not None and atual not in memoria:
                if atual not in pais:          # pai fora do que conhecemos: para aqui
                    memoria[atual] = ""
                    break
                pai = pais[atual][0]
                if pai is None:
                    # Raiz do drive: caminho vazio. O nome dela ("root") NAO
                    # entra no caminho — os caminhos sao relativos a raiz.
                    memoria[atual] = ""
                    break
                visto.append(atual)
                atual = pai
                if len(visto) > 200:           # cinto de seguranca contra ciclo
                    break
            base = memoria.get(atual, "") if atual is not None else ""
            for ident in reversed(visto):
                nome = pais[ident][1]
                base = f"{base}\\{nome}" if base else nome
                memoria[ident] = base
            return memoria.get(item_id, "")

        mudancas = []
        for linha in linhas:
            novo = caminho(linha["item_id"])
            if novo != (linha["caminho_rel"] or ""):
                mudancas.append((novo, linha["caminho_rel"], cu.agora_iso(),
                                 drive_id, linha["item_id"]))
        if mudancas:
            self.con.executemany(
                "UPDATE item_nuvem SET caminho_rel=?, caminho_anterior=?, ultima_vez_em=? "
                "WHERE drive_id=? AND item_id=?", mudancas)
            self.con.commit()
        return len(mudancas)

    # -- a rodada -------------------------------------------------------
    # -- pasta removida leva os descendentes junto -----------------------
    def propagar_remocao_de_pastas(self, drive_id: str) -> int:
        """Pasta removida => tudo abaixo dela tambem saiu.

        O delta costuma devolver APENAS a pasta apagada, nao os milhares de
        descendentes. Sem esta propagacao, o espelho ficaria com uma pasta
        `presente=0` cheia de filhos `presente=1` — e o inventario diria que
        arquivos existem num lugar que nao existe mais.

        Desce em largura a partir de cada pasta removida. Nao apaga linha
        nenhuma: marca `presente=0` e grava evento, como manda a REGRA 6.
        """
        fronteira = [linha["item_id"] for linha in self.con.execute(
            "SELECT item_id FROM item_nuvem WHERE drive_id=? AND eh_pasta=1 "
            "AND presente=0", (drive_id,))]
        agora = cu.agora_iso()
        atingidos = 0
        vistos = set(fronteira)
        while fronteira:
            lote = fronteira[:400]
            fronteira = fronteira[400:]
            marcas = ",".join("?" * len(lote))
            filhos = self.con.execute(
                f"SELECT item_id, eh_pasta, caminho_rel FROM item_nuvem "
                f"WHERE drive_id=? AND presente=1 AND parent_id IN ({marcas})",
                [drive_id] + lote).fetchall()
            for filho in filhos:
                if filho["item_id"] in vistos:
                    continue
                vistos.add(filho["item_id"])
                self.con.execute(
                    "UPDATE item_nuvem SET presente=0, removido_em=?, ultima_vez_em=?, "
                    "execucao_id=? WHERE drive_id=? AND item_id=?",
                    (agora, agora, self.execucao_id, drive_id, filho["item_id"]))
                self._evento(drive_id, filho["item_id"], "removido",
                             {"motivo": "pasta ancestral removida",
                              "caminho": filho["caminho_rel"]})
                atingidos += 1
                if filho["eh_pasta"]:
                    fronteira.append(filho["item_id"])
        if atingidos:
            self.con.commit()
            self.contadores["removidos_cascata"] += atingidos
            self.log.aviso(f"{atingidos} descendente(s) de pasta removida sairam junto")
        return atingidos

    # -- o que o resync completo NAO devolveu, sumiu ---------------------
    def varrer_ausentes(self, drive_id: str) -> int:
        """So depois de uma varredura COMPLETA que chegou ao fim.

        Um delta completo nao devolve exclusoes: ele devolve o que existe. O
        que existia no espelho e nao voltou nesta rodada, portanto, nao
        existe mais. E a unica leitura correta do que o 410 pede —
        "including deletes".

        A trava e o `execucao_id`: so e varrido quem nao foi tocado por ESTA
        execucao. Rodada parcial, retomada ou incremental nunca chega aqui.
        """
        agora = cu.agora_iso()
        candidatos = self.con.execute(
            "SELECT item_id, eh_pasta, caminho_rel FROM item_nuvem "
            "WHERE drive_id=? AND presente=1 AND (execucao_id IS NULL OR execucao_id<>?)",
            (drive_id, self.execucao_id)).fetchall()
        for linha in candidatos:
            self.con.execute(
                "UPDATE item_nuvem SET presente=0, removido_em=?, ultima_vez_em=?, "
                "execucao_id=? WHERE drive_id=? AND item_id=?",
                (agora, agora, self.execucao_id, drive_id, linha["item_id"]))
            self._evento(drive_id, linha["item_id"], "removido",
                         {"motivo": "ausente no resync completo",
                          "caminho": linha["caminho_rel"]})
        if candidatos:
            self.con.commit()
            self.contadores["removidos_varredura"] += len(candidatos)
            self.contadores["removidos"] += len(candidatos)
            self.log.aviso(f"{len(candidatos)} item(ns) nao voltaram no resync "
                           f"completo — marcados como removidos")
        return len(candidatos)

    def sincronizar(self, drive_id: str, nome_drive: str, modo: str = "auto",
                    _apos_resync: bool = False) -> str:
        url, efetivo = self.url_inicial(drive_id, modo)
        self.log.info(f"[{nome_drive}] modo: {efetivo}")
        token = self._token(drive_id)
        paginas = int(token["paginas"] or 0) if efetivo == "retomada" else 0
        self._gravar_token(drive_id, status="em_andamento", erro=None,
                           iniciado_em=cu.agora_iso(),
                           paginas=paginas, next_link=url if efetivo == "retomada" else None)

        try:
            while True:
                try:
                    pagina = self.graph.get(url)
                except graph_ro.ErroGraph as exc:
                    if _apos_resync or not resync_exigido(exc):
                        raise
                    # O servidor jogou fora o nosso token. Insistir no
                    # incremental so repete o 410: a saida e recomecar do
                    # zero e varrer o que nao voltar.
                    self.log.aviso(f"[{nome_drive}] o Graph exigiu resync "
                                   f"(HTTP 410). Recomecando a varredura "
                                   f"completa e tratando ausentes como removidos.")
                    self._gravar_token(drive_id, status="resync_exigido",
                                       delta_link=None, next_link=None,
                                       erro=str(exc)[:400])
                    self.contadores["resyncs"] += 1
                    return self.sincronizar(drive_id, nome_drive, "completo",
                                            _apos_resync=True)
                itens = pagina.get("value", [])
                proximo = pagina.get("@odata.nextLink")
                final = pagina.get("@odata.deltaLink")

                self.aplicar(drive_id, itens)
                paginas += 1
                self.contadores["paginas"] += 1
                # Checkpoint: itens + posicao, na mesma transacao.
                self.con.execute(
                    "UPDATE delta_token SET next_link=?, paginas=?, itens_ultima=?, "
                    "atualizado_em=? WHERE drive_id=?",
                    (proximo, paginas, self.contadores["itens"], cu.agora_iso(), drive_id))
                self.con.commit()

                if paginas % 10 == 0 or not proximo:
                    self.log.info(f"[{nome_drive}] {self.contadores['itens']:,} itens, "
                                  f"{paginas} paginas".replace(",", "."))

                if self.limite and self.contadores["itens"] >= self.limite:
                    self.log.aviso(f"limite de {self.limite} itens atingido — "
                                   f"rodada fica retomavel")
                    return "interrompida"

                if proximo:
                    url = proximo
                    continue

                # Fim da rodada: SO AGORA o delta_link e substituido.
                # A varredura de ausentes exige que a rodada COMPLETA tenha
                # comecado e terminado nesta execucao — nunca depois de uma
                # retomada ou de um incremental, onde "nao veio" quer dizer
                # "nao mudou", e nao "sumiu".
                if efetivo == "completo":
                    self.varrer_ausentes(drive_id)
                self.propagar_remocao_de_pastas(drive_id)
                self.recalcular_caminhos(drive_id)
                self._gravar_token(drive_id, status="concluido", delta_link=final,
                                   next_link=None, paginas=paginas,
                                   itens_ultima=self.contadores["itens"], erro=None)
                return "concluida"
        except Exception as exc:
            self.con.commit()
            self._gravar_token(drive_id, status="erro", erro=str(exc)[:400])
            self.contadores["erros"] += 1
            self.log.erro(f"[{nome_drive}] {type(exc).__name__}: {exc}")
            raise


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Inventario da nuvem por delta")
    ap.add_argument("--config", default=None)
    ap.add_argument("--completo", action="store_true", help="ignora tokens, varre tudo")
    ap.add_argument("--incremental", action="store_true", help="so o que mudou")
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--drive", default=None, help="nome da biblioteca (ex.: Programa)")
    ap.add_argument("--quieto", action="store_true")
    args = ap.parse_args()

    modo = "completo" if args.completo else ("incremental" if args.incremental else "auto")
    cfg = cu.carregar_config(args.config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='item_nuvem'").fetchone():
        print("banco sem schema v3. Rode antes:  python scripts\\03_migrar_v3.py")
        return 2

    graph = graph_ro.GraphRO(pausa_minima_s=0.02)
    execucao_id = cu.abrir_execucao(con, "scan_graph", args.drive or "", vars(args))
    log = cu.Log(cfg["_logs_path"], "scan_graph", con, execucao_id, verboso=not args.quieto)

    inventario = InventarioNuvem(graph, con, execucao_id, log, args.limite)
    t0 = time.perf_counter()
    status = "concluida"
    try:
        drives = inventario.registrar_drives(graph.site_id())
        alvos = [d for d in drives if not args.drive or d.get("name") == args.drive]
        if not alvos:
            print(f"drive '{args.drive}' nao encontrado. Ha: "
                  f"{[d.get('name') for d in drives]}")
            return 2
        for d in alvos:
            resultado = inventario.sincronizar(d["id"], d.get("name", "?"), modo)
            if resultado != "concluida":
                status = "interrompida"
                break
    except KeyboardInterrupt:
        status = "interrompida"
        log.aviso("interrompido pelo usuario — o checkpoint da pagina esta salvo")
    except Exception as exc:
        status = "erro"
        log.erro(f"parou: {exc}")
    finally:
        con.commit()

    decorrido = time.perf_counter() - t0
    cu.fechar_execucao(con, execucao_id, status,
                       {"arquivos": inventario.contadores["itens"],
                        "novos": inventario.contadores["novos"],
                        "alterados": inventario.contadores["alterados"],
                        "erros": inventario.contadores["erros"]}, decorrido)

    c = inventario.contadores
    print()
    print("=" * 66)
    print(f"INVENTARIO PELA NUVEM — {status}")
    print("=" * 66)
    print(f"  tempo            {decorrido/60:.2f} min ({decorrido:.0f}s)")
    print(f"  paginas          {c['paginas']}")
    print(f"  itens no delta   {c['itens']:,}".replace(",", "."))
    for rotulo in ("novos", "alterados", "renomeados", "movidos", "removidos",
                   "reapareceram"):
        print(f"    {rotulo:<14} {c[rotulo]:,}".replace(",", "."))
    print(f"      por cascata de pasta   {c['removidos_cascata']:,}".replace(",", "."))
    print(f"      por ausencia no resync {c['removidos_varredura']:,}".replace(",", "."))
    if c["resyncs"]:
        print(f"  resyncs exigidos {c['resyncs']} (HTTP 410: o token de delta morreu)")
    print(f"  chamadas Graph   {graph.chamadas} (esperas por limite: {graph.esperas_429})")
    print()
    for linha in con.execute("SELECT * FROM v_nuvem_resumo"):
        print(f"  [{linha['biblioteca']}] {linha['arquivos']:,} arquivos, "
              f"{linha['pastas']:,} pastas, {(linha['bytes'] or 0)/1024**4:.3f} TB, "
              f"{linha['com_hash']:,} com hash".replace(",", "."))
    print("=" * 66)
    if status == "concluida":
        print("  proximo passo:  python scripts\\13_reconciliar.py")
    else:
        print("  rode de novo para continuar de onde parou (o checkpoint esta salvo)")
    log.fechar()
    con.close()
    return 0 if status == "concluida" else 1


if __name__ == "__main__":
    raise SystemExit(main())
