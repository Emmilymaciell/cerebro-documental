"""
13_reconciliar — E5: liga o inventario antigo (139.459 linhas) aos itens da
nuvem, por `drive_id + item_id`, sem perder trabalho feito.

Roda em dois tempos, de proposito:

    python scripts\\13_reconciliar.py            # SIMULA: mede e relata, nao altera
    python scripts\\13_reconciliar.py --aplicar  # faz backup e aplica

Como cada linha antiga e ligada (primeiro metodo que der resultado UNICO):

  1. `caminho_exato`      mesmo caminho na nuvem            -> ALTA
  2. `nome_tamanho_unico` mesmo nome e tamanho, unico no drive -> MEDIA
                          (tipico de arquivo movido/renomeado depois da varredura local)
  3. nada disso                                             -> SEM_CORRESPONDENCIA

Um item da nuvem so pode ser reivindicado por UMA linha antiga. A segunda
vira AMBIGUO e **nao e aplicada**. Duvida nao vira certeza: vira fila.

O que e preservado sem excecao: estado (COMPLETED e companhia), derivados,
extracoes, indice e a prioridade P0..P4. So muda para `CHANGED` quem teve o
conteudo alterado na nuvem (tamanho ou hash diferentes) — e mesmo assim os
derivados antigos continuam no disco ate o worker refazer.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

MAPA = cu.RAIZ_REPO / "config" / "mapa_drives.yaml"
ROTULOS_P = {0: "P0", 1: "P1", 2: "P2", 3: "P3", 4: "P4"}


# ----------------------------------------------------------------------
# Retrato do banco: serve para provar que nada se perdeu
# ----------------------------------------------------------------------

def retrato(con) -> dict:
    def um(sql, *p):
        return con.execute(sql, p).fetchone()[0]

    dados = {
        "arquivos": um("SELECT COUNT(*) FROM arquivo"),
        "presentes": um("SELECT COUNT(*) FROM arquivo WHERE presente=1"),
        "com_item_id": um("SELECT COUNT(*) FROM arquivo WHERE item_id IS NOT NULL"),
        "extracoes": um("SELECT COUNT(*) FROM extracao"),
        "trechos": um("SELECT COUNT(*) FROM texto_fts"),
        "eventos": um("SELECT COUNT(*) FROM evento_arquivo"),
        "pastas": um("SELECT COUNT(*) FROM pasta"),
        "itens_nuvem": um("SELECT COUNT(*) FROM item_nuvem WHERE eh_pasta=0 AND presente=1"),
    }
    for estado in ("COMPLETED", "OCR_REQUIRED", "PARTIAL", "DUPLICATE", "UNSUPPORTED",
                   "QUEUED", "CHANGED", "SKIPPED", "MISSING", "ERROR_FINAL",
                   "ERROR_RETRYABLE", "INVENTORIED"):
        dados[f"estado_{estado}"] = um("SELECT COUNT(*) FROM arquivo WHERE estado=?", estado)
    for p in ROTULOS_P:
        dados[f"prio_{p}"] = um("SELECT COUNT(*) FROM arquivo WHERE prioridade=?", p)
    return dados


def derivados_no_disco(con) -> tuple[int, int]:
    """(quantos derivados o banco aponta, quantos existem mesmo em disco)."""
    linhas = con.execute("SELECT texto_path FROM extracao WHERE texto_path IS NOT NULL")
    caminhos = [l[0] for l in linhas]
    existem = sum(1 for c in caminhos if Path(c).exists())
    return len(caminhos), existem


# ----------------------------------------------------------------------
# Mapa biblioteca local -> pasta na nuvem
# ----------------------------------------------------------------------

def carregar_mapa(caminho: Path | str | None = None) -> tuple[dict, dict]:
    dados = yaml.safe_load(Path(caminho or MAPA).read_text(encoding="utf-8"))
    direto = {}
    inverso = {}
    for biblioteca, regra in (dados.get("mapa_biblioteca") or {}).items():
        direto[biblioteca] = (regra["drive_id"], regra["pasta_nuvem"])
        inverso[(regra["drive_id"], regra["pasta_nuvem"].lower())] = biblioteca
    return direto, inverso


def caminho_candidato(caminho_local: str, biblioteca: str, mapa: dict) -> tuple | None:
    regra = mapa.get(biblioteca)
    if not regra:
        return None
    drive_id, pasta_nuvem = regra
    resto = caminho_local.split("\\", 1)[1] if "\\" in caminho_local else ""
    return drive_id, (f"{pasta_nuvem}\\{resto}" if resto else pasta_nuvem)


def caminho_local_de(caminho_nuvem: str, drive_id: str, nome_drive: str,
                     inverso: dict) -> str:
    """Caminho no estilo do banco antigo, para linhas novas vindas da nuvem."""
    primeiro = caminho_nuvem.split("\\", 1)[0]
    resto = caminho_nuvem.split("\\", 1)[1] if "\\" in caminho_nuvem else ""
    biblioteca = inverso.get((drive_id, primeiro.lower()))
    if biblioteca:
        return f"{biblioteca}\\{resto}" if resto else biblioteca
    return f"{nome_drive}\\{caminho_nuvem}"


# ----------------------------------------------------------------------
# Reconciliacao
# ----------------------------------------------------------------------

class Reconciliador:
    def __init__(self, con, cfg, execucao_id: int, log, aplicar: bool):
        self.con = con
        self.cfg = cfg
        self.execucao_id = execucao_id
        self.log = log
        self.aplicar = aplicar
        self.mapa, self.inverso = carregar_mapa(cfg.get('mapa_drives'))
        self.categorias = cfg.get("categorias") or {}
        self.suportadas = set(cfg.get("extratores_ativos") or [])
        self.padroes_temp = cfg.get("arquivos_temporarios") or []
        self.contadores = {
            "locais": 0, "alta": 0, "media": 0, "ambiguo": 0, "sem_correspondencia": 0,
            "sem_mapa": 0, "conteudo_mudou": 0, "aplicados": 0, "novos_da_nuvem": 0,
            "completed_preservados": 0, "completed_para_refazer": 0,
        }
        self.usados: dict[str, int] = {}

    # -- indices da nuvem ------------------------------------------------
    def indexar_nuvem(self):
        self.por_caminho: dict[tuple, list] = {}
        self.por_nome_tam: dict[tuple, list] = {}
        self.itens: dict[str, dict] = {}
        for linha in self.con.execute(
            "SELECT drive_id, item_id, parent_id, nome, caminho_rel, tamanho_bytes, "
            "quick_xor, etag, ctag, web_url, criado_em, modificado_em, criado_por, "
            "modificado_por FROM item_nuvem WHERE eh_pasta=0 AND presente=1"
        ):
            item = dict(linha)
            self.itens[item["item_id"]] = item
            chave = (item["drive_id"], (item["caminho_rel"] or "").lower())
            self.por_caminho.setdefault(chave, []).append(item["item_id"])
            chave2 = (item["drive_id"], (item["nome"] or "").lower(), item["tamanho_bytes"])
            self.por_nome_tam.setdefault(chave2, []).append(item["item_id"])
        self.log.info(f"{len(self.itens):,} arquivos da nuvem indexados".replace(",", "."))

    # -- 1a passada: caminho exato (o metodo forte) -----------------------
    def decidir_por_caminho(self, linha) -> dict | None:
        """ALTA quando o caminho bate. `None` = indeciso, vai para a 2a passada.

        Esta passada roda para TODAS as linhas antes da segunda. Sem isso, um
        casamento fraco (nome+tamanho) de uma linha anterior rouba o item que
        uma linha posterior reivindicaria pelo caminho exato — foi o que
        aconteceu com as revisoes REV 00 / REV 01 da desapropriacao da RD-103,
        onde o mesmo nome e tamanho existe nas duas pastas.
        """
        candidato = caminho_candidato(linha["caminho_rel"], linha["biblioteca"], self.mapa)
        if candidato is None:
            return {"metodo": "sem_mapa", "confianca": "SEM_CORRESPONDENCIA",
                    "item_id": None, "drive_id": None, "candidatos": 0,
                    "detalhe": f"biblioteca '{linha['biblioteca']}' fora do escopo da nuvem"}

        drive_id, caminho_nuvem = candidato
        achados = self.por_caminho.get((drive_id, caminho_nuvem.lower()), [])
        livres = [i for i in achados if i not in self.usados]

        if len(livres) == 1:
            return {"metodo": "caminho_exato", "confianca": "ALTA", "item_id": livres[0],
                    "drive_id": drive_id, "caminho_nuvem": caminho_nuvem, "candidatos": 1}
        if len(livres) > 1:
            return {"metodo": "caminho_exato", "confianca": "AMBIGUO", "item_id": None,
                    "drive_id": drive_id, "caminho_nuvem": caminho_nuvem,
                    "candidatos": len(livres),
                    "detalhe": "mais de um item da nuvem no mesmo caminho"}
        if achados:
            return {"metodo": "caminho_exato", "confianca": "AMBIGUO", "item_id": None,
                    "drive_id": drive_id, "caminho_nuvem": caminho_nuvem,
                    "candidatos": len(achados),
                    "detalhe": "o item deste caminho ja foi reivindicado por outra linha"}
        return None

    # -- 2a passada: nome + tamanho, so no que sobrou ---------------------
    def decidir_por_nome(self, linha) -> dict:
        drive_id, caminho_nuvem = caminho_candidato(
            linha["caminho_rel"], linha["biblioteca"], self.mapa)
        chave = (drive_id, (linha["nome"] or "").lower(), linha["tamanho_bytes"])
        achados = self.por_nome_tam.get(chave, [])
        livres = [i for i in achados if i not in self.usados]
        if len(livres) == 1:
            item = self.itens[livres[0]]
            return {"metodo": "nome_tamanho_unico", "confianca": "MEDIA",
                    "item_id": livres[0], "drive_id": drive_id,
                    "caminho_nuvem": item["caminho_rel"], "candidatos": 1,
                    "detalhe": "casou por nome+tamanho; o caminho mudou na nuvem"}
        if len(livres) > 1:
            return {"metodo": "nome_tamanho_unico", "confianca": "AMBIGUO", "item_id": None,
                    "drive_id": drive_id, "caminho_nuvem": caminho_nuvem,
                    "candidatos": len(livres),
                    "detalhe": "varios arquivos com o mesmo nome e tamanho"}
        return {"metodo": "nenhum", "confianca": "SEM_CORRESPONDENCIA", "item_id": None,
                "drive_id": drive_id, "caminho_nuvem": caminho_nuvem, "candidatos": 0,
                "detalhe": "nao existe na nuvem (apagado, movido para fora, ou so local)"}

    # -- execucao ---------------------------------------------------------
    def rodar(self):
        self.indexar_nuvem()
        # Inclui os MISSING de proposito: se o item reaparecer na nuvem, esta
        # e a rodada que o traz de volta — com o estado que ele tinha antes,
        # guardado no evento `ausente_na_nuvem`.
        locais = self.con.execute(
            "SELECT id, caminho_rel, biblioteca, nome, tamanho_bytes, mtime_utc, estado, "
            "prioridade, suportado, temporario FROM arquivo "
            "WHERE presente=1 OR estado='MISSING' ORDER BY id").fetchall()
        self.log.info(f"{len(locais):,} linhas antigas a reconciliar".replace(",", "."))

        agora = cu.agora_iso()

        # 1a passada: so caminho exato, para TODAS as linhas. Quem nao decidir
        # aqui espera a 2a passada — assim o metodo forte escolhe primeiro.
        decisoes: dict[int, dict] = {}
        indecisos = []
        for linha in locais:
            decisao = self.decidir_por_caminho(linha)
            if decisao is None:
                indecisos.append(linha)
                continue
            decisoes[linha["id"]] = decisao
            if decisao["item_id"]:
                self.usados[decisao["item_id"]] = linha["id"]
        self.log.info(f"1a passada (caminho exato): {len(decisoes):,} decididas, "
                      f"{len(indecisos):,} para a 2a".replace(",", "."))

        # 2a passada: nome + tamanho, so entre os itens que sobraram livres.
        for linha in indecisos:
            decisao = self.decidir_por_nome(linha)
            decisoes[linha["id"]] = decisao
            if decisao["item_id"]:
                self.usados[decisao["item_id"]] = linha["id"]

        self.usados.clear()   # a gravacao abaixo reivindica de novo, em ordem
        for linha in locais:
            self.contadores["locais"] += 1
            decisao = decisoes[linha["id"]]
            confianca = decisao["confianca"]
            item = self.itens.get(decisao["item_id"]) if decisao["item_id"] else None

            mudou = 0
            if item is not None:
                # Cuidado: `0 or -1` devolve -1 em Python. Arquivo de 0 byte
                # (ha 952 no acervo) seria marcado como alterado sem ter mudado.
                t_nuvem = item["tamanho_bytes"]
                t_local = linha["tamanho_bytes"]
                mudou = int((t_nuvem if t_nuvem is not None else -1)
                            != (t_local if t_local is not None else -1))

            if confianca == "ALTA":
                self.contadores["alta"] += 1
            elif confianca == "MEDIA":
                self.contadores["media"] += 1
            elif confianca == "AMBIGUO":
                self.contadores["ambiguo"] += 1
            else:
                self.contadores["sem_correspondencia"] += 1
                if decisao["metodo"] == "sem_mapa":
                    self.contadores["sem_mapa"] += 1

            self.con.execute(
                "INSERT INTO reconciliacao (execucao_id, arquivo_id, drive_id, item_id, "
                "caminho_local, caminho_nuvem, metodo, confianca, aplicado, conteudo_mudou, "
                "candidatos, detalhe, em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.execucao_id, linha["id"], decisao.get("drive_id"), decisao["item_id"],
                 linha["caminho_rel"], decisao.get("caminho_nuvem"), decisao["metodo"],
                 confianca, int(self.aplicar and item is not None), mudou,
                 decisao.get("candidatos", 0), decisao.get("detalhe"), agora))

            if item is None:
                if self.aplicar:
                    self.con.execute(
                        "UPDATE arquivo SET presente_nuvem=0, origem='local', "
                        "visto_nuvem_em=? WHERE id=?", (agora, linha["id"]))
                continue

            self.usados[item["item_id"]] = linha["id"]
            if mudou:
                self.contadores["conteudo_mudou"] += 1

            if self.aplicar:
                self.vincular(linha, item, mudou, agora)
                self.contadores["aplicados"] += 1

            if linha["estado"] == "COMPLETED":
                if mudou and linha["suportado"]:
                    self.contadores["completed_para_refazer"] += 1
                else:
                    self.contadores["completed_preservados"] += 1

            if self.contadores["locais"] % 20000 == 0:
                self.con.commit()
                self.log.info(f"  ... {self.contadores['locais']:,} linhas"
                              .replace(",", "."))
        self.con.commit()

    def vincular(self, linha, item: dict, mudou: int, agora: str):
        """Escreve a identidade da nuvem na linha antiga, preservando o estado."""
        novo_estado = linha["estado"]

        # Reaparecimento: o item estava MISSING e voltou a existir na nuvem.
        # O estado anterior foi guardado no evento — voltamos exatamente para
        # ele, sem inventar e sem perder o historico.
        if linha["estado"] == "MISSING":
            registro = self.con.execute(
                "SELECT detalhe FROM evento_arquivo WHERE arquivo_id=? "
                "AND evento='ausente_na_nuvem' ORDER BY id DESC LIMIT 1",
                (linha["id"],)).fetchone()
            try:
                novo_estado = (json.loads(registro[0]).get("estado_anterior")
                               if registro and registro[0] else "QUEUED")
            except (json.JSONDecodeError, TypeError):
                novo_estado = "QUEUED"
            self.con.execute(
                "UPDATE arquivo SET presente=1 WHERE id=?", (linha["id"],))
            self.con.execute(
                "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, detalhe, em) "
                "VALUES (?,?,?,?,?)",
                (linha["id"], self.execucao_id, "reapareceu_na_nuvem",
                 json.dumps({"estado_restaurado": novo_estado,
                             "item_id": item["item_id"]}, ensure_ascii=False), agora))
            self.contadores["reativados"] = self.contadores.get("reativados", 0) + 1
        # So volta para a fila quem tem extrator E teve conteudo alterado.
        if mudou and linha["suportado"] and linha["estado"] in (
                "COMPLETED", "PARTIAL", "OCR_REQUIRED", "DUPLICATE",
                "ERROR_FINAL", "ERROR_RETRYABLE"):
            novo_estado = "CHANGED"
        self.con.execute(
            "UPDATE arquivo SET drive_id=?, item_id=?, parent_id=?, caminho_nuvem=?, "
            "quick_xor=?, etag=?, ctag=?, web_url=?, criado_por=?, modificado_por=?, "
            # Linha que nasceu da nuvem (origem='graph') nao vira 'ambos' numa
            # 2a passada: nunca existiu no disco local (R1, idempotencia).
            "criado_em_nuvem=?, modificado_em_nuvem=?, presente_nuvem=1, "
            "origem=CASE WHEN origem='graph' THEN 'graph' ELSE 'ambos' END, "
            "visto_nuvem_em=?, tamanho_bytes=?, estado=?, alterado_em=CASE WHEN ? THEN ? "
            "ELSE alterado_em END WHERE id=?",
            (item["drive_id"], item["item_id"], item["parent_id"], item["caminho_rel"],
             item["quick_xor"], item["etag"], item["ctag"], item["web_url"],
             item["criado_por"], item["modificado_por"], item["criado_em"],
             item["modificado_em"], agora, item["tamanho_bytes"], novo_estado,
             mudou, agora, linha["id"]))

    # -- o que so existe na nuvem ------------------------------------------
    def inserir_novos(self):
        nomes_drive = dict(self.con.execute("SELECT drive_id, nome FROM drive"))
        agora = cu.agora_iso()
        novos = []
        for item_id, item in self.itens.items():
            if item_id in self.usados:
                continue
            caminho_nuvem = item["caminho_rel"] or item["nome"]
            caminho_local = caminho_local_de(caminho_nuvem, item["drive_id"],
                                             nomes_drive.get(item["drive_id"], "nuvem"),
                                             self.inverso)
            nome = item["nome"] or ""
            ext = os.path.splitext(nome)[1].lower()
            categoria = cu.categoria_de(ext, self.categorias)
            temporario = 1 if cu.combina_padrao(nome, self.padroes_temp) else 0
            pasta_rel = caminho_local.rsplit("\\", 1)[0] if "\\" in caminho_local else ""
            novos.append((
                caminho_local, caminho_local.split("\\", 1)[0], pasta_rel, nome, ext,
                categoria, cu.normalizar_nome(nome), caminho_local.count("\\") + 1,
                len(caminho_local), item["tamanho_bytes"], item["modificado_em"],
                item["criado_em"], 1, temporario,
                0 if temporario else int(categoria in self.suportadas),
                "SKIPPED" if temporario else "INVENTORIED", agora, agora,
                self.execucao_id, item["drive_id"], item["item_id"], item["parent_id"],
                caminho_nuvem, item["quick_xor"], item["etag"], item["ctag"],
                item["web_url"], item["criado_por"], item["modificado_por"],
                item["criado_em"], item["modificado_em"], agora))
        if novos and self.aplicar:
            self.con.executemany(
                "INSERT INTO arquivo (caminho_rel, biblioteca, pasta_rel, nome, ext, "
                "categoria, nome_normalizado, profundidade, comprimento_caminho, "
                "tamanho_bytes, mtime_utc, ctime_utc, online, temporario, suportado, "
                "estado, primeira_vez_em, ultima_vez_em, ultima_execucao_id, drive_id, "
                "item_id, parent_id, caminho_nuvem, quick_xor, etag, ctag, web_url, "
                "criado_por, modificado_por, criado_em_nuvem, modificado_em_nuvem, "
                "presente_nuvem, origem, visto_nuvem_em) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "1,'graph',?)", novos)
            self.con.commit()
        self.contadores["novos_da_nuvem"] = len(novos)


# ----------------------------------------------------------------------
# Relatorio
# ----------------------------------------------------------------------

def relatorio(con, cfg, antes: dict, depois: dict, contadores: dict, aplicado: bool,
              derivados: tuple, backup: Path | None) -> Path:
    saida = cfg["_saida_path"]
    saida.mkdir(parents=True, exist_ok=True)

    with open(saida / "reconciliacao_excecoes.csv", "w", encoding="utf-8-sig",
              newline="") as fh:
        escritor = csv.writer(fh, delimiter=";")
        escritor.writerow(["confianca", "metodo", "candidatos", "caminho_local",
                           "caminho_nuvem", "detalhe"])
        for linha in con.execute(
            "SELECT confianca, metodo, candidatos, caminho_local, caminho_nuvem, detalhe "
            "FROM reconciliacao WHERE confianca IN ('AMBIGUO','SEM_CORRESPONDENCIA') "
            "ORDER BY confianca, caminho_local"
        ):
            escritor.writerow(list(linha))

    md = [f"# Reconciliação local ↔ nuvem — {cu.agora_iso()[:16]}", ""]
    md.append(f"**Modo:** {'APLICADO' if aplicado else 'SIMULAÇÃO (nada foi alterado)'}")
    if backup:
        md.append(f"**Backup:** `{backup}`")
    md.append("")
    md.append("## Resultado do casamento\n")
    md.append("| Resultado | Linhas | O que significa |")
    md.append("|---|---:|---|")
    md.append(f"| **ALTA** | {contadores['alta']:,} | mesmo caminho na nuvem |")
    md.append(f"| **MEDIA** | {contadores['media']:,} | nome e tamanho idênticos e únicos "
              f"(mudou de lugar depois da varredura local) |")
    md.append(f"| **AMBÍGUO** | {contadores['ambiguo']:,} | mais de um candidato — "
              f"**não aplicado**, foi para a fila de exceções |")
    md.append(f"| **SEM CORRESPONDÊNCIA** | {contadores['sem_correspondencia']:,} | "
              f"não existe na nuvem (dos quais {contadores['sem_mapa']:,} são de "
              f"biblioteca fora do escopo) |")
    md.append(f"| Total examinado | {contadores['locais']:,} | |")
    md.append("")
    md.append(f"- **Conteúdo alterado na nuvem desde a varredura local:** "
              f"{contadores['conteudo_mudou']:,}")
    md.append("  > Critério: **tamanho diferente**. As linhas antigas não têm hash "
              "(ele exigiria baixar 1 TB), então uma edição que preserve o tamanho "
              "não é detectável nesta migração. A partir daqui isso deixa de ser um "
              "problema: `ctag` e `quickXorHash` vêm do servidor a cada delta.")
    md.append(f"- **Itens da nuvem sem vínculo** (entram como linhas novas): "
              f"{contadores['novos_da_nuvem']:,}")
    md.append("  > Inclui tanto arquivos realmente novos quanto os que ficaram sem "
              "vínculo por ambiguidade. Nada da nuvem fica fora do catálogo; a linha "
              "antiga correspondente está na fila de exceções. Pode haver duplicação "
              "temporária até a revisão manual.")

    md.append("\n## Preservação do trabalho já feito\n")
    md.append("| Item | Antes | Depois |")
    md.append("|---|---:|---:|")
    for rotulo, chave in (("Arquivos no banco", "arquivos"),
                          ("COMPLETED", "estado_COMPLETED"),
                          ("OCR_REQUIRED", "estado_OCR_REQUIRED"),
                          ("DUPLICATE", "estado_DUPLICATE"),
                          ("UNSUPPORTED", "estado_UNSUPPORTED"),
                          ("QUEUED", "estado_QUEUED"),
                          ("CHANGED", "estado_CHANGED"),
                          ("Extrações", "extracoes"),
                          ("Trechos no índice", "trechos"),
                          ("Eventos", "eventos"),
                          ("Pastas classificadas", "pastas"),
                          ("Com item_id", "com_item_id")):
        md.append(f"| {rotulo} | {antes[chave]:,} | {depois[chave]:,} |")
    md.append("")
    md.append("| Prioridade | Antes | Depois |")
    md.append("|---|---:|---:|")
    for p in ROTULOS_P:
        md.append(f"| {ROTULOS_P[p]} | {antes[f'prio_{p}']:,} | {depois[f'prio_{p}']:,} |")
    md.append("")
    md.append(f"- **Derivados apontados pelo banco:** {derivados[0]}; "
              f"**encontrados em disco:** {derivados[1]}")
    md.append(f"- **COMPLETED preservados:** {contadores['completed_preservados']:,}")
    md.append(f"- **COMPLETED que voltaram para a fila** (conteúdo mudou na nuvem): "
              f"{contadores['completed_para_refazer']:,}")

    perdas = []
    for chave in ("extracoes", "trechos", "eventos", "pastas"):
        if depois[chave] < antes[chave]:
            perdas.append(f"{chave}: {antes[chave]} → {depois[chave]}")
    if depois["arquivos"] < antes["arquivos"]:
        perdas.append(f"arquivos: {antes['arquivos']} → {depois['arquivos']}")
    md.append("\n## Perda de informação\n")
    md.append("**Nenhuma.**" if not perdas else "⚠️ " + "; ".join(perdas))

    md.append("\n## Fila de tratamento manual\n")
    total_excecoes = con.execute(
        "SELECT COUNT(*) FROM reconciliacao WHERE confianca IN "
        "('AMBIGUO','SEM_CORRESPONDENCIA')").fetchone()[0]
    md.append(f"{total_excecoes:,} linhas em `output/reconciliacao_excecoes.csv` "
              f"e na view `v_excecoes_reconciliacao`.\n")
    md.append("| Motivo | Linhas |")
    md.append("|---|---:|")
    for linha in con.execute(
        "SELECT confianca, metodo, COUNT(*) n FROM reconciliacao "
        "WHERE confianca IN ('AMBIGUO','SEM_CORRESPONDENCIA') "
        "GROUP BY confianca, metodo ORDER BY n DESC"
    ):
        md.append(f"| {linha['confianca']} · {linha['metodo']} | {linha['n']:,} |")

    md.append("\n### Amostra das exceções\n")
    md.append("| Confiança | Caminho local | Detalhe |")
    md.append("|---|---|---|")
    for linha in con.execute(
        "SELECT confianca, caminho_local, detalhe FROM reconciliacao "
        "WHERE confianca IN ('AMBIGUO','SEM_CORRESPONDENCIA') "
        "ORDER BY confianca, RANDOM() LIMIT 15"
    ):
        md.append(f"| {linha['confianca']} | `{(linha['caminho_local'] or '')[:70]}` | "
                  f"{(linha['detalhe'] or '')[:60]} |")

    texto = "\n".join(md).replace(",", ".")
    destino = saida / "reconciliacao.md"
    destino.write_text(texto, encoding="utf-8")
    return destino


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcilia o banco antigo com a nuvem")
    ap.add_argument("--config", default=None)
    ap.add_argument("--aplicar", action="store_true",
                    help="sem isto, apenas simula e relata")
    ap.add_argument("--quieto", action="store_true")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])

    if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='item_nuvem'").fetchone():
        print("banco sem schema v3. Rode:  python scripts\\03_migrar_v3.py")
        return 2
    na_nuvem = con.execute("SELECT COUNT(*) FROM item_nuvem WHERE eh_pasta=0 "
                           "AND presente=1").fetchone()[0]
    if na_nuvem == 0:
        print("item_nuvem vazio. Rode antes:  python scripts\\12_scan_graph.py")
        return 2
    pendente = con.execute("SELECT COUNT(*) FROM delta_token WHERE status='em_andamento'"
                           ).fetchone()[0]
    if pendente:
        print("ATENCAO: ha inventario de nuvem em andamento. Termine-o antes "
              "(python scripts\\12_scan_graph.py).")
        return 2

    antes = retrato(con)
    backup = None
    if args.aplicar:
        con.close()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrar", cu.RAIZ_REPO / "scripts" / "03_migrar_v3.py")
        migrar = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migrar)
        backup = migrar.fazer_backup(cfg["_banco_path"])
        print(f"  backup: {backup} ({backup.stat().st_size/1024**2:.0f} MB)")
        con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
        con.execute("DELETE FROM reconciliacao")
        con.commit()

    execucao_id = cu.abrir_execucao(con, "reconciliacao", "", vars(args))
    log = cu.Log(cfg["_logs_path"], "reconciliacao", con, execucao_id,
                 verboso=not args.quieto)

    rec = Reconciliador(con, cfg, execucao_id, log, args.aplicar)
    rec.rodar()
    rec.inserir_novos()

    depois = retrato(con)
    derivados = derivados_no_disco(con)
    destino = relatorio(con, cfg, antes, depois, rec.contadores, args.aplicar,
                        derivados, backup)
    cu.fechar_execucao(con, execucao_id, "concluida",
                       {"arquivos": rec.contadores["locais"],
                        "novos": rec.contadores["novos_da_nuvem"],
                        "alterados": rec.contadores["conteudo_mudou"]}, 0.0)

    c = rec.contadores
    print()
    print("=" * 70)
    print(f"RECONCILIACAO — {'APLICADA' if args.aplicar else 'SIMULACAO'}")
    print("=" * 70)
    print(f"  linhas antigas examinadas   {c['locais']:,}".replace(",", "."))
    print(f"    ALTA (caminho exato)      {c['alta']:,}".replace(",", "."))
    print(f"    MEDIA (nome+tamanho)      {c['media']:,}".replace(",", "."))
    print(f"    AMBIGUO (nao aplicado)    {c['ambiguo']:,}".replace(",", "."))
    print(f"    SEM CORRESPONDENCIA       {c['sem_correspondencia']:,}".replace(",", "."))
    print(f"      (fora do escopo)        {c['sem_mapa']:,}".replace(",", "."))
    print(f"  conteudo mudou na nuvem     {c['conteudo_mudou']:,}".replace(",", "."))
    print(f"  so na nuvem (novos)         {c['novos_da_nuvem']:,}".replace(",", "."))
    print()
    print(f"  COMPLETED preservados       {c['completed_preservados']}")
    print(f"  COMPLETED para refazer      {c['completed_para_refazer']}")
    print(f"  derivados no banco/disco    {derivados[0]}/{derivados[1]}")
    print(f"  extracoes  {antes['extracoes']} -> {depois['extracoes']}")
    print(f"  trechos    {antes['trechos']} -> {depois['trechos']}")
    print(f"  arquivos   {antes['arquivos']:,} -> {depois['arquivos']:,}".replace(",", "."))
    print(f"  com item_id {antes['com_item_id']:,} -> {depois['com_item_id']:,}"
          .replace(",", "."))
    print(f"\n  relatorio: {destino}")
    if not args.aplicar:
        print("\n  SIMULACAO: nada foi alterado. Para aplicar:")
        print("     python scripts\\13_reconciliar.py --aplicar")
    log.fechar()
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
