"""
02_classificar — descobre P0..P4 lendo a propria arvore.

Nao e IA. Sao regras sobre o nome das pastas (config/classificacao.yaml),
com heranca de pai para filho e MOTIVO gravado em cada pasta.

  P0 = obra + categoria critica  (contratos, notificacoes, NC, oficios,
       medicoes, relatorios, juridico, atas, desapropriacao, Parceiro/Orgao...)
  P1 = categoria critica fora de obra
  P2 = demais pastas de obra (projeto executivo, fotos, gestao tecnica...)
  P3 = restante do acervo      <- TAMBEM sera processado
  P4 = backup/superado/lixeira <- processado por ultimo

Depois de classificar, coloca na fila:
  INVENTORIED + suportado  -> QUEUED
  INVENTORIED sem extrator -> UNSUPPORTED  (catalogado, nunca perdido)

    python scripts\\02_classificar.py
    python scripts\\02_classificar.py --relatorio      # so reescreve os relatorios
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

CLASSIFICACAO_PADRAO = cu.RAIZ_REPO / "config" / "classificacao.yaml"
ROTULOS = {0: "P0", 1: "P1", 2: "P2", 3: "P3", 4: "P4"}


# ----------------------------------------------------------------------
# Regras
# ----------------------------------------------------------------------

class Regras:
    def __init__(self, dados: dict):
        self.obras = {obra: [cu.normalizar_texto(p) for p in padroes]
                      for obra, padroes in (dados.get("obras") or {}).items()}
        self.categorias = []
        for cat in (dados.get("categorias_documentais") or []):
            self.categorias.append((
                cat["nome"],
                bool(cat.get("critica", False)),
                [cu.normalizar_texto(p) for p in cat.get("padroes", [])],
            ))
        self.descarte = [cu.normalizar_texto(p)
                         for p in ((dados.get("descarte") or {}).get("padroes") or [])]
        self.excecoes = []
        for exc in (dados.get("excecoes") or []):
            if exc.get("prefixo"):
                self.excecoes.append((str(exc["prefixo"]).lower().rstrip("\\"),
                                      int(exc.get("prioridade", 3)),
                                      exc.get("motivo", "excecao manual")))
        self.excecoes.sort(key=lambda e: -len(e[0]))

    def obra_de(self, segmento_norm: str) -> str | None:
        for obra, padroes in self.obras.items():
            if any(p and p in segmento_norm for p in padroes):
                return obra
        return None

    def categoria_de(self, segmento_norm: str):
        for nome, critica, padroes in self.categorias:
            if any(p and p in segmento_norm for p in padroes):
                return nome, critica
        return None, False

    def eh_descarte(self, segmento_norm: str) -> bool:
        return any(p and p in segmento_norm for p in self.descarte)

    def excecao_de(self, caminho_rel: str):
        alvo = caminho_rel.lower()
        for prefixo, prioridade, motivo in self.excecoes:
            if alvo == prefixo or alvo.startswith(prefixo + "\\"):
                return prioridade, motivo
        return None, None


def carregar_regras(caminho: Path | str | None = None) -> Regras:
    """As regras do `config/classificacao.yaml`, prontas para uso."""
    dados = yaml.safe_load(
        Path(caminho or CLASSIFICACAO_PADRAO).read_text(encoding="utf-8"))
    return Regras(dados)


def prioridade_de(obra, categoria, critica, descarte) -> tuple[int, str]:
    """P0 documento critico de obra · P1 documento critico fora de obra ·
    P2 demais pastas de obra · P3 acervo geral · P4 descarte."""
    if descarte:
        return 4, "pasta de descarte/backup/superado"
    if obra and categoria and critica:
        return 0, f"obra {obra} + categoria critica '{categoria}'"
    if categoria and critica:
        return 1, f"categoria critica '{categoria}' (fora de obra)"
    if obra and categoria:
        return 2, f"obra {obra} + categoria '{categoria}'"
    if obra:
        return 2, f"obra {obra}, sem categoria identificada"
    if categoria:
        return 3, f"categoria '{categoria}' (fora de obra)"
    return 3, "acervo geral"


# ----------------------------------------------------------------------
# A arvore resolvida — UNICA implementacao das regras P0..P4
# ----------------------------------------------------------------------

def expandir_ancestrais(caminhos) -> set[str]:
    """Os caminhos pedidos + todas as pastas intermediarias + a raiz ("")."""
    todas = set(caminhos)
    for caminho in list(todas):
        partes = caminho.split("\\") if caminho else []
        for i in range(1, len(partes)):
            todas.add("\\".join(partes[:i]))
    todas.add("")
    return todas


def resolver_arvore(regras: Regras, caminhos) -> dict[str, dict]:
    """caminho de pasta -> {obra, categoria, critica, descarte, prioridade, motivo}.

    Do topo para baixo, herdando do pai. O resultado de uma pasta depende
    apenas dos nomes da sua propria cadeia de ancestrais, por isso resolver
    um punhado de caminhos novos da exatamente o mesmo que resolver a arvore
    inteira.

    Esta e a UNICA implementacao das regras P0..P4: `classificar()` (acervo
    inteiro) e `19_ingerir_novos.py` (arquivos novos da nuvem) chamam aqui.
    """
    resolvidas: dict[str, dict] = {}
    for caminho in sorted(expandir_ancestrais(caminhos), key=lambda c: (c.count("\\"), c)):
        partes = caminho.split("\\") if caminho else []
        nome = partes[-1] if partes else "<raiz>"
        pai = "\\".join(partes[:-1]) if len(partes) > 1 else ("" if partes else None)
        herdado = resolvidas.get(pai, {}) if pai is not None else {}

        seg = cu.normalizar_texto(nome)
        obra = regras.obra_de(seg) or herdado.get("obra")
        categoria, critica = regras.categoria_de(seg)
        if not categoria:
            categoria, critica = herdado.get("categoria"), herdado.get("critica", False)
        descarte = regras.eh_descarte(seg) or herdado.get("descarte", False)

        prioridade, motivo = prioridade_de(obra, categoria, critica, descarte)

        forcada, motivo_exc = regras.excecao_de(caminho)
        if forcada is not None:
            prioridade, motivo = forcada, f"EXCECAO: {motivo_exc}"

        resolvidas[caminho] = {"obra": obra, "categoria": categoria, "critica": critica,
                               "descarte": descarte, "prioridade": prioridade,
                               "motivo": motivo}
    return resolvidas


def estado_inicial(temporario: bool, suportado: bool) -> str:
    """O estado com que um arquivo recem-inventariado entra na maquina.

    E a forma "por item" da mesma regra que `enfileirar()` aplica em massa
    por SQL: temporario -> SKIPPED; tem extrator -> QUEUED; nao tem ->
    UNSUPPORTED (catalogado, nunca perdido — REGRA 6).
    """
    if temporario:
        return "SKIPPED"
    return "QUEUED" if suportado else "UNSUPPORTED"


# ----------------------------------------------------------------------
# Classificacao da arvore
# ----------------------------------------------------------------------

def classificar(con, regras: Regras, log: cu.Log) -> dict:
    estatistica = {p: 0 for p in ROTULOS}

    # 1. todas as pastas que contem arquivo + todas as pastas intermediarias
    diretas = {linha["pasta_rel"]: (linha["n"], linha["b"] or 0, linha["sup"] or 0)
               for linha in con.execute(
                   "SELECT pasta_rel, COUNT(*) n, SUM(tamanho_bytes) b, SUM(suportado) sup "
                   "FROM arquivo WHERE presente=1 GROUP BY pasta_rel")}

    # 2. do topo para baixo, herdando do pai
    resolvidas = resolver_arvore(regras, diretas.keys())
    log.info(f"{len(resolvidas):,} pastas a classificar".replace(",", "."))

    linhas = []
    for caminho in sorted(resolvidas, key=lambda c: (c.count("\\"), c)):
        dados = resolvidas[caminho]
        partes = caminho.split("\\") if caminho else []
        nome = partes[-1] if partes else "<raiz>"
        prioridade = dados["prioridade"]
        n, b, sup = diretas.get(caminho, (0, 0, 0))
        biblioteca = partes[0] if partes else ""
        linhas.append((caminho, biblioteca, nome, len(partes), dados["obra"],
                       dados["categoria"], int(bool(dados["critica"])), prioridade,
                       dados["motivo"], n, sup, b, cu.agora_iso()))
        estatistica[prioridade] += 1

    con.execute("DELETE FROM pasta")
    con.executemany(
        "INSERT INTO pasta (caminho_rel, biblioteca, nome, nivel, obra, categoria_doc, "
        "critica, prioridade, motivo, arquivos, arquivos_suportados, bytes, classificado_em) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", linhas)
    con.commit()

    # 3. propaga a prioridade para os arquivos
    atualizacoes = [(dados["prioridade"], caminho) for caminho, dados in resolvidas.items()
                    if caminho in diretas]
    con.executemany("UPDATE arquivo SET prioridade=? WHERE pasta_rel=?", atualizacoes)
    con.commit()
    return estatistica


def enfileirar(con, log: cu.Log) -> tuple[int, int]:
    """INVENTORIED -> QUEUED (tem extrator) ou UNSUPPORTED (nao tem, mas fica no catalogo)."""
    cur = con.execute(
        "UPDATE arquivo SET estado='QUEUED' WHERE presente=1 AND temporario=0 "
        "AND suportado=1 AND estado='INVENTORIED'")
    fila = cur.rowcount
    cur = con.execute(
        "UPDATE arquivo SET estado='UNSUPPORTED' WHERE presente=1 AND temporario=0 "
        "AND suportado=0 AND estado='INVENTORIED'")
    sem_extrator = cur.rowcount
    # Formato que ganhou extrator depois: volta para a fila.
    con.execute(
        "UPDATE arquivo SET estado='QUEUED' WHERE presente=1 AND temporario=0 "
        "AND suportado=1 AND estado='UNSUPPORTED'")
    con.commit()
    log.info(f"fila: +{fila} enfileirados | {sem_extrator} sem extrator (catalogados)")
    return fila, sem_extrator


# ----------------------------------------------------------------------
# Relatorios
# ----------------------------------------------------------------------

def relatorios(con, cfg: dict, estatistica: dict) -> Path:
    saida = cfg["_saida_path"]
    saida.mkdir(parents=True, exist_ok=True)

    with open(saida / "pastas_classificadas.csv", "w", encoding="utf-8-sig", newline="") as fh:
        escritor = csv.writer(fh, delimiter=";")
        escritor.writerow(["prioridade", "obra", "categoria", "arquivos", "suportados",
                           "bytes", "motivo", "caminho"])
        for linha in con.execute(
            "SELECT prioridade, obra, categoria_doc, arquivos, arquivos_suportados, bytes, "
            "motivo, caminho_rel FROM pasta WHERE arquivos > 0 "
            "ORDER BY prioridade, arquivos DESC"
        ):
            escritor.writerow([ROTULOS[linha["prioridade"]], linha["obra"] or "",
                               linha["categoria_doc"] or "", linha["arquivos"],
                               linha["arquivos_suportados"], linha["bytes"],
                               linha["motivo"], linha["caminho_rel"]])

    md = [f"# Classificação de prioridades — {cu.agora_iso()[:10]}", ""]
    md.append("Gerado por `scripts/02_classificar.py` a partir de `config/classificacao.yaml`.")
    md.append("Prioridade define **a ordem da extração**, não o escopo: "
              "todo o acervo será processado.\n")

    md.append("## Resumo\n")
    md.append("| Prioridade | Pastas | Arquivos | Suportados | Volume |")
    md.append("|---|---:|---:|---:|---:|")
    for p in sorted(ROTULOS):
        linha = con.execute(
            "SELECT COUNT(*) pastas, COALESCE(SUM(arquivos),0) arq, "
            "COALESCE(SUM(arquivos_suportados),0) sup, COALESCE(SUM(bytes),0) b "
            "FROM pasta WHERE prioridade=? AND arquivos>0", (p,)).fetchone()
        md.append(f"| **{ROTULOS[p]}** | {linha['pastas']:,} | {linha['arq']:,} | "
                  f"{linha['sup']:,} | {linha['b']/1024**3:,.1f} GB |".replace(",", "."))

    md.append("\n## P0 — obra + categoria crítica\n")
    md.append("| Obra | Categoria | Arquivos | Pasta |")
    md.append("|---|---|---:|---|")
    for linha in con.execute(
        "SELECT obra, categoria_doc, arquivos, caminho_rel FROM pasta "
        "WHERE prioridade=0 AND arquivos>0 ORDER BY arquivos DESC LIMIT 120"
    ):
        md.append(f"| {linha['obra']} | {linha['categoria_doc']} | {linha['arquivos']:,} | "
                  f"`{linha['caminho_rel']}` |".replace(",", "."))

    md.append("\n## P1 — obra, ou categoria crítica fora de obra\n")
    md.append("| Obra | Categoria | Arquivos | Pasta |")
    md.append("|---|---|---:|---|")
    for linha in con.execute(
        "SELECT obra, categoria_doc, arquivos, caminho_rel FROM pasta "
        "WHERE prioridade=1 AND arquivos>0 ORDER BY arquivos DESC LIMIT 80"
    ):
        md.append(f"| {linha['obra'] or '—'} | {linha['categoria_doc'] or '—'} | "
                  f"{linha['arquivos']:,} | `{linha['caminho_rel']}` |".replace(",", "."))

    md.append("\n## Categorias documentais encontradas\n")
    md.append("| Categoria | Pastas | Arquivos |")
    md.append("|---|---:|---:|")
    for linha in con.execute(
        "SELECT categoria_doc, COUNT(*) p, SUM(arquivos) a FROM pasta "
        "WHERE categoria_doc IS NOT NULL AND arquivos>0 GROUP BY categoria_doc "
        "ORDER BY a DESC"
    ):
        md.append(f"| {linha['categoria_doc']} | {linha['p']:,} | "
                  f"{linha['a']:,} |".replace(",", "."))

    md.append("\n---\n")
    md.append("Lista completa em `output/pastas_classificadas.csv`. ")
    md.append("Para corrigir uma classificação, edite `config/classificacao.yaml` "
              "(seção `excecoes`) e rode `python scripts\\02_classificar.py` de novo.")

    destino = saida / "prioridades.md"
    destino.write_text("\n".join(md), encoding="utf-8")
    return destino


def main() -> int:
    ap = argparse.ArgumentParser(description="Classifica a arvore em P0..P4")
    ap.add_argument("--config", default=None)
    ap.add_argument("--classificacao", default=None)
    ap.add_argument("--relatorio", action="store_true", help="so reescreve os relatorios")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    regras = carregar_regras(args.classificacao)

    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    execucao_id = cu.abrir_execucao(con, "classificacao", "", vars(args))
    log = cu.Log(cfg["_logs_path"], "classificacao", con, execucao_id)

    estatistica = {}
    if not args.relatorio:
        estatistica = classificar(con, regras, log)
        enfileirar(con, log)
    destino = relatorios(con, cfg, estatistica)

    print()
    print("=" * 66)
    print("CLASSIFICACAO")
    print("=" * 66)
    for p in sorted(ROTULOS):
        linha = con.execute(
            "SELECT COUNT(*) pastas, COALESCE(SUM(arquivos),0) arq, "
            "COALESCE(SUM(arquivos_suportados),0) sup FROM pasta "
            "WHERE prioridade=? AND arquivos>0", (p,)).fetchone()
        print(f"  {ROTULOS[p]}  {linha['pastas']:>7,} pastas  {linha['arq']:>9,} arquivos  "
              f"{linha['sup']:>9,} com extrator".replace(",", "."))
    fila = con.execute("SELECT COUNT(*) FROM v_fila").fetchone()[0]
    print(f"\n  fila de extracao: {fila:,} arquivos".replace(",", "."))
    print(f"  relatorio: {destino}")
    print(f"  planilha : {cfg['_saida_path'] / 'pastas_classificadas.csv'}")
    print("=" * 66)

    cu.fechar_execucao(con, execucao_id, "concluida", {"arquivos": fila}, 0.0)
    log.fechar()
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
