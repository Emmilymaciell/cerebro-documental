"""
14_auditar_excecoes — fecha a conta das excecoes da reconciliacao.

SOMENTE LEITURA. Nao muda estado de processamento, nao grava vinculo, nao
reconcilia nada. So investiga e relata.

Duas perguntas:

  1. Os arquivos que "nao estao na nuvem" sumiram mesmo? Procura cada um em
     TODOS os drives inventariados, por nome+tamanho, so por nome, e pela
     cauda do caminho. Tambem compara a arvore local com a da nuvem na pasta
     onde eles se concentram, para distinguir exclusao de renomeacao/mudanca.

  2. Fechamento contabil: cada numero publicado tem de bater com uma consulta.

Limite honesto, declarado desde ja: as linhas antigas **nao tem hash**
(calcular exigiria baixar 1 TB), entao nao da para cruzar `quickXorHash` com
elas. O cruzamento por hash so vale entre itens da nuvem.

    python scripts\\14_auditar_excecoes.py
    python scripts\\14_auditar_excecoes.py --sem-rede    # so o que ja esta no banco
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402
import graph_ro  # noqa: E402

CAUDA = 3          # quantos segmentos finais do caminho formam a "cauda"


def cauda_de(caminho: str, n: int = CAUDA) -> str:
    return "\\".join(caminho.lower().split("\\")[-n:])


# ----------------------------------------------------------------------
# 1. Os ausentes
# ----------------------------------------------------------------------

def indexar_nuvem(con):
    por_nome_tam: dict[tuple, list] = {}
    por_nome: dict[str, list] = {}
    por_cauda: dict[str, list] = {}
    itens: dict[str, dict] = {}
    for linha in con.execute(
        "SELECT drive_id, item_id, nome, caminho_rel, tamanho_bytes, quick_xor, "
        "criado_em, modificado_em FROM item_nuvem WHERE eh_pasta=0 AND presente=1"
    ):
        item = dict(linha)
        itens[item["item_id"]] = item
        nome = (item["nome"] or "").lower()
        por_nome_tam.setdefault((nome, item["tamanho_bytes"]), []).append(item["item_id"])
        por_nome.setdefault(nome, []).append(item["item_id"])
        por_cauda.setdefault(cauda_de(item["caminho_rel"] or ""), []).append(item["item_id"])
    return itens, por_nome_tam, por_nome, por_cauda


def investigar(con, log) -> tuple[list, Counter]:
    itens, por_nome_tam, por_nome, por_cauda = indexar_nuvem(con)
    log.info(f"{len(itens):,} arquivos da nuvem indexados".replace(",", "."))

    ausentes = con.execute(
        "SELECT a.id, a.caminho_rel, a.caminho_nuvem, a.nome, a.tamanho_bytes, a.estado, "
        "a.mtime_utc, r.drive_id AS drive_esperado "
        "FROM reconciliacao r JOIN arquivo a ON a.id = r.arquivo_id "
        "WHERE r.confianca='SEM_CORRESPONDENCIA' AND r.metodo='nenhum' "
        "ORDER BY a.caminho_rel").fetchall()
    log.info(f"{len(ausentes):,} linhas a investigar".replace(",", "."))

    # Todo item da nuvem ja reivindicado por alguma linha: candidato "livre"
    # e mais forte do que candidato que ja tem dono.
    com_dono = {r[0] for r in con.execute(
        "SELECT item_id FROM arquivo WHERE item_id IS NOT NULL")}

    resultados = []
    contagem = Counter()
    for linha in ausentes:
        nome = (linha["nome"] or "").lower()
        alvo_cauda = cauda_de(linha["caminho_nuvem"] or linha["caminho_rel"] or "")

        exatos = por_nome_tam.get((nome, linha["tamanho_bytes"]), [])
        por_nome_so = por_nome.get(nome, [])
        pela_cauda = por_cauda.get(alvo_cauda, [])

        livres = [i for i in exatos if i not in com_dono]
        candidatos = livres or exatos
        veredito, detalhe, escolhido = "realmente_ausente", "", None

        if candidatos:
            mesmo_drive = [i for i in candidatos
                           if itens[i]["drive_id"] == linha["drive_esperado"]]
            outro_drive = [i for i in candidatos
                           if itens[i]["drive_id"] != linha["drive_esperado"]]
            preferidos = mesmo_drive or outro_drive
            escolhido = preferidos[0]
            item = itens[escolhido]
            ocupado = " (o item ja tem dono: e uma copia, nao o mesmo arquivo)" \
                      if escolhido in com_dono else ""
            if len(preferidos) > 1:
                veredito = "varios_candidatos"
                detalhe = f"{len(preferidos)} arquivos com mesmo nome e tamanho{ocupado}"
            elif mesmo_drive:
                veredito = "mesmo_drive_outro_caminho" if not ocupado else "conteudo_existe_como_copia"
                detalhe = f"esta em {item['caminho_rel']}{ocupado}"
            else:
                veredito = "outro_drive"
                detalhe = f"drive {item['drive_id'][-10:]} em {item['caminho_rel']}{ocupado}"
        elif pela_cauda:
            escolhido = pela_cauda[0]
            veredito = "cauda_igual_tamanho_diferente"
            detalhe = (f"mesma posicao relativa em {itens[escolhido]['caminho_rel']}, "
                       f"tamanho {itens[escolhido]['tamanho_bytes']} "
                       f"(local {linha['tamanho_bytes']})")
        elif por_nome_so:
            escolhido = por_nome_so[0]
            veredito = "nome_igual_tamanho_diferente"
            detalhe = (f"{len(por_nome_so)} com o mesmo nome; ex.: "
                       f"{itens[escolhido]['caminho_rel']} "
                       f"({itens[escolhido]['tamanho_bytes']} bytes)")

        contagem[veredito] += 1
        resultados.append({
            "arquivo_id": linha["id"], "estado": linha["estado"],
            "caminho_local": linha["caminho_rel"], "veredito": veredito,
            "detalhe": detalhe,
            "item_candidato": escolhido or "",
            "caminho_candidato": itens[escolhido]["caminho_rel"] if escolhido else "",
        })
    return resultados, contagem


def comparar_subarvore(con, prefixo_local: str, prefixo_nuvem: str) -> dict:
    """Pastas irmas: quais existem no banco antigo, quais na nuvem, hoje."""
    locais = {r[0] for r in con.execute(
        "SELECT DISTINCT SUBSTR(pasta_rel, LENGTH(?)+2, "
        "  CASE WHEN INSTR(SUBSTR(pasta_rel, LENGTH(?)+2), '\\') > 0 "
        "       THEN INSTR(SUBSTR(pasta_rel, LENGTH(?)+2), '\\') - 1 "
        "       ELSE LENGTH(pasta_rel) END) "
        "FROM arquivo WHERE pasta_rel LIKE ? || '\\%'",
        (prefixo_local, prefixo_local, prefixo_local, prefixo_local)) if r[0]}
    nuvem = {r[0] for r in con.execute(
        "SELECT DISTINCT SUBSTR(caminho_rel, LENGTH(?)+2, "
        "  CASE WHEN INSTR(SUBSTR(caminho_rel, LENGTH(?)+2), '\\') > 0 "
        "       THEN INSTR(SUBSTR(caminho_rel, LENGTH(?)+2), '\\') - 1 "
        "       ELSE LENGTH(caminho_rel) END) "
        "FROM item_nuvem WHERE presente=1 AND caminho_rel LIKE ? || '\\%'",
        (prefixo_nuvem, prefixo_nuvem, prefixo_nuvem, prefixo_nuvem)) if r[0]}
    return {"so_local": sorted(locais - nuvem), "so_nuvem": sorted(nuvem - locais),
            "nos_dois": sorted(locais & nuvem)}


# ----------------------------------------------------------------------
# 2. Fechamento contabil
# ----------------------------------------------------------------------

def contabilidade(con) -> list[tuple]:
    def um(sql, *p):
        return con.execute(sql, p).fetchone()[0]

    linhas = [
        ("item_nuvem — total", um("SELECT COUNT(*) FROM item_nuvem")),
        ("  arquivos (eh_pasta=0, presente=1)",
         um("SELECT COUNT(*) FROM item_nuvem WHERE eh_pasta=0 AND presente=1")),
        ("  pastas (eh_pasta=1, presente=1)",
         um("SELECT COUNT(*) FROM item_nuvem WHERE eh_pasta=1 AND presente=1")),
        ("    das quais, raizes de drive (sem parent_id)",
         um("SELECT COUNT(*) FROM item_nuvem WHERE parent_id IS NULL")),
        ("  removidos (presente=0)",
         um("SELECT COUNT(*) FROM item_nuvem WHERE presente=0")),
        ("arquivo — total de linhas", um("SELECT COUNT(*) FROM arquivo")),
        ("  com item_id (ligadas a nuvem)",
         um("SELECT COUNT(*) FROM arquivo WHERE item_id IS NOT NULL")),
        ("  sem item_id", um("SELECT COUNT(*) FROM arquivo WHERE item_id IS NULL")),
        ("reconciliacao — decisoes", um("SELECT COUNT(*) FROM reconciliacao")),
        ("  ALTA", um("SELECT COUNT(*) FROM reconciliacao WHERE confianca='ALTA'")),
        ("  MEDIA", um("SELECT COUNT(*) FROM reconciliacao WHERE confianca='MEDIA'")),
        ("  AMBIGUO", um("SELECT COUNT(*) FROM reconciliacao WHERE confianca='AMBIGUO'")),
        ("  SEM_CORRESPONDENCIA",
         um("SELECT COUNT(*) FROM reconciliacao WHERE confianca='SEM_CORRESPONDENCIA'")),
        ("    metodo sem_mapa (fora do escopo)",
         um("SELECT COUNT(*) FROM reconciliacao WHERE confianca='SEM_CORRESPONDENCIA' "
            "AND metodo='sem_mapa'")),
        ("    metodo nenhum (nao achado na nuvem)",
         um("SELECT COUNT(*) FROM reconciliacao WHERE confianca='SEM_CORRESPONDENCIA' "
            "AND metodo='nenhum'")),
        ("      destes, temporarios ~$ / .tmp",
         um("SELECT COUNT(*) FROM reconciliacao r JOIN arquivo a ON a.id=r.arquivo_id "
            "WHERE r.confianca='SEM_CORRESPONDENCIA' AND r.metodo='nenhum' "
            "AND a.temporario=1")),
        ("      destes, ja processados (COMPLETED)",
         um("SELECT COUNT(*) FROM reconciliacao r JOIN arquivo a ON a.id=r.arquivo_id "
            "WHERE r.confianca='SEM_CORRESPONDENCIA' AND r.metodo='nenhum' "
            "AND a.estado='COMPLETED'")),
        ("      destes, demais (nao temporarios, nao processados)",
         um("SELECT COUNT(*) FROM reconciliacao r JOIN arquivo a ON a.id=r.arquivo_id "
            "WHERE r.confianca='SEM_CORRESPONDENCIA' AND r.metodo='nenhum' "
            "AND a.temporario=0 AND a.estado<>'COMPLETED'")),
    ]
    return linhas


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Auditoria das excecoes (somente leitura)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--sem-rede", action="store_true")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    con = cu.conectar(cfg["_banco_path"])
    execucao_id = cu.abrir_execucao(con, "auditoria", "", vars(args))
    log = cu.Log(cfg["_logs_path"], "auditoria", con, execucao_id)

    resultados, contagem = investigar(con, log)

    # A subarvore onde os ausentes se concentram
    prefixo_local = ("Programa - 3. Desenvolvimento\\G2 -  Engenharia\\RD103\\"
                     "4_Projeto Executivo\\3. Viario Secundario\\DESAPROPRIAÇÃO")
    prefixo_nuvem = ("3. Desenvolvimento\\G2 -  Engenharia\\RD103\\"
                     "4_Projeto Executivo\\3. Viario Secundario\\DESAPROPRIAÇÃO")
    arvore = comparar_subarvore(con, prefixo_local, prefixo_nuvem)

    # Confirmacao ao vivo: a pasta existe agora, com que filhos?
    ao_vivo = {}
    if not args.sem_rede:
        try:
            graph = graph_ro.GraphRO(pausa_minima_s=0.05)
            drive_id = con.execute(
                "SELECT drive_id FROM drive WHERE nome='Programa'").fetchone()[0]
            pasta = graph.item_por_caminho(drive_id, prefixo_nuvem)
            filhos = list(graph.filhos(drive_id, pasta["id"]))
            ao_vivo = {"existe": True, "id": pasta["id"],
                       "modificado_em": pasta.get("lastModifiedDateTime"),
                       "modificado_por": (pasta.get("lastModifiedBy") or {})
                                          .get("user", {}).get("displayName"),
                       "filhos": [f["name"] for f in filhos]}
        except graph_ro.ErroGraph as exc:
            ao_vivo = {"existe": False, "erro": f"{exc.status} {exc.codigo}"}

    conta = contabilidade(con)

    # ---- saidas --------------------------------------------------------
    saida = cfg["_saida_path"]
    saida.mkdir(parents=True, exist_ok=True)
    with open(saida / "auditoria_ausentes.csv", "w", encoding="utf-8-sig",
              newline="") as fh:
        escritor = csv.writer(fh, delimiter=";")
        escritor.writerow(["veredito", "arquivo_id", "estado", "caminho_local",
                           "caminho_candidato", "detalhe"])
        for r in resultados:
            escritor.writerow([r["veredito"], r["arquivo_id"], r["estado"],
                               r["caminho_local"], r["caminho_candidato"], r["detalhe"]])

    md = [f"# Auditoria das exceções — {cu.agora_iso()[:16]}", "",
          "Investigação **somente leitura**: nenhum estado de processamento foi",
          "alterado e nenhum vínculo foi criado a partir destas conclusões.", "",
          "## 1. Os arquivos que não foram encontrados na nuvem", "",
          f"**Total investigado: {len(resultados):,}**", "",
          "| Veredito | Linhas |", "|---|---:|"]
    for veredito, n in contagem.most_common():
        md.append(f"| {veredito} | {n:,} |")
    md.append("")
    md.append("Critérios usados, em ordem: nome+tamanho idênticos (em qualquer drive) →")
    md.append("cauda do caminho (últimos 3 segmentos) → nome igual com tamanho diferente.")
    md.append("")
    md.append("> **Limite declarado:** as linhas antigas não têm hash — calcular exigiria")
    md.append("> baixar o acervo inteiro. Por isso o cruzamento por `quickXorHash` só é")
    md.append("> possível **entre itens da nuvem**, não entre nuvem e inventário antigo.")
    md.append("> Nome + tamanho + posição relativa é o sinal mais forte disponível aqui.")

    md.append("\n## 2. A subárvore onde eles se concentram\n")
    md.append(f"`{prefixo_local}`\n")
    md.append("| Pasta | Onde está |")
    md.append("|---|---|")
    for nome in arvore["nos_dois"]:
        md.append(f"| `{nome}` | nos dois |")
    for nome in arvore["so_local"]:
        md.append(f"| `{nome}` | **só no inventário antigo** |")
    for nome in arvore["so_nuvem"]:
        md.append(f"| `{nome}` | **só na nuvem** |")
    if ao_vivo:
        md.append("\n### Conferência ao vivo no Graph\n")
        if ao_vivo.get("existe"):
            md.append(f"- A pasta **existe** agora (`{ao_vivo['id']}`).")
            md.append(f"- Modificada em {ao_vivo.get('modificado_em')} "
                      f"por {ao_vivo.get('modificado_por')}.")
            md.append(f"- Filhos hoje: {', '.join('`'+f+'`' for f in ao_vivo['filhos'])}")
        else:
            md.append(f"- A pasta **não existe mais**: {ao_vivo.get('erro')}")

    md.append("\n## 3. Fechamento contábil\n")
    md.append("| Medida | Valor |")
    md.append("|---|---:|")
    for rotulo, valor in conta:
        md.append(f"| {rotulo} | {valor:,} |")

    texto = "\n".join(md).replace(",", ".")
    destino = saida / "auditoria_excecoes.md"
    destino.write_text(texto, encoding="utf-8")

    # ---- console -------------------------------------------------------
    print("=" * 70)
    print("AUDITORIA DAS EXCECOES (somente leitura)")
    print("=" * 70)
    print(f"  investigados: {len(resultados):,}".replace(",", "."))
    for veredito, n in contagem.most_common():
        print(f"    {veredito:<34} {n:,}".replace(",", "."))
    print("\n  SUBARVORE DA DESAPROPRIACAO")
    print(f"    nos dois:   {arvore['nos_dois']}")
    print(f"    so local:   {arvore['so_local']}")
    print(f"    so nuvem:   {arvore['so_nuvem']}")
    if ao_vivo:
        print(f"    ao vivo:    existe={ao_vivo.get('existe')} "
              f"filhos={ao_vivo.get('filhos', ao_vivo.get('erro'))}")
    print("\n  FECHAMENTO CONTABIL")
    for rotulo, valor in conta:
        print(f"    {rotulo:<48} {valor:>9,}".replace(",", "."))
    print(f"\n  relatorio: {destino}")
    print("=" * 70)

    cu.fechar_execucao(con, execucao_id, "concluida", {"arquivos": len(resultados)}, 0.0)
    log.fechar()
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
