"""
11_mapear_drives — descobre a geografia da nuvem e como ela corresponde
aos caminhos que ja estao no banco.

Responde tres perguntas antes de qualquer inventario:

  1. Quantos drives (bibliotecas) o site tem de verdade? Os dois que
     aparecem ('Programa' e 'Documentos') sao o mesmo conteudo ou nao?
  2. Como o caminho guardado no banco ('Programa - 3. Desenvolvimento\\...')
     vira caminho na nuvem ('3. Desenvolvimento/...')?
  3. Essa regra resolve quantos por cento dos caminhos? (amostra real)

Nao escreve nada na nuvem e nao le nada do disco sincronizado.

    python scripts\\11_mapear_drives.py
    python scripts\\11_mapear_drives.py --amostra 200
"""

from __future__ import annotations

import argparse
import random
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402
import graph_ro  # noqa: E402

import yaml  # noqa: E402


def normalizar(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(texto)).lower()
    limpo = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(limpo.replace("-", " ").replace("_", " ").split())


def casar(nome_local: str, pastas_nuvem: dict) -> tuple[str, str, str] | None:
    """(drive_id, nome_na_nuvem, como_casou) para uma biblioteca local."""
    alvo = normalizar(nome_local)
    for chave, (drive_id, nome) in pastas_nuvem.items():
        if chave == alvo:
            return drive_id, nome, "nome identico"
    # O OneDrive prefixa a pasta sincronizada com o nome da biblioteca:
    # "Programa - 3. Desenvolvimento" na maquina  ->  "3. Desenvolvimento" na nuvem.
    if " - " in nome_local:
        sufixo = normalizar(nome_local.split(" - ", 1)[1])
        for chave, (drive_id, nome) in pastas_nuvem.items():
            if chave == sufixo:
                return drive_id, nome, "prefixo da biblioteca removido"
    for chave, (drive_id, nome) in pastas_nuvem.items():
        if chave and (chave in alvo or alvo in chave):
            return drive_id, nome, "correspondencia parcial (conferir)"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Mapeia drives e caminhos da nuvem")
    ap.add_argument("--config", default=None)
    ap.add_argument("--amostra", type=int, default=100,
                    help="quantos caminhos do banco testar contra a nuvem")
    ap.add_argument("--semente", type=int, default=20260920)
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    graph = graph_ro.GraphRO(pausa_minima_s=0.05)

    print("=" * 72)
    print("MAPA DA NUVEM")
    print("=" * 72)

    site = graph.site_id()
    print(f"  site      {site}")

    # ---- 1. drives ----------------------------------------------------
    drives = graph.drives(site)
    print(f"\n  DRIVES ({len(drives)})")
    raizes = {}
    for d in drives:
        raiz = graph.raiz_do_drive(d["id"])
        quota = (d.get("quota") or {}).get("used", 0)
        raizes[d["id"]] = raiz
        print(f"    {d.get('name'):<16} tipo={d.get('driveType','?'):<12} "
              f"usado={quota/1024**3:>8.1f} GB")
        print(f"      id       {d['id']}")
        print(f"      raiz id  {raiz.get('id')}")
        print(f"      webUrl   {d.get('webUrl','')}")

    # ATENCAO: neste tenant, bibliotecas DIFERENTES devolvem o mesmo `id` de
    # item-raiz. Comparar o id da raiz nao serve. O que distingue de verdade
    # e o driveId e o conteudo do primeiro nivel.
    assinaturas: dict[tuple, list] = {}
    for d in drives:
        filhos = tuple(sorted((f.get("id", ""), f.get("name", ""))
                              for f in graph.filhos(d["id"])))
        assinaturas.setdefault(filhos, []).append(d.get("name"))
    duplicados = {str(k[:1]): v for k, v in assinaturas.items() if len(v) > 1 and k}
    if duplicados:
        print("\n  ATENCAO: drives com o MESMO conteudo de primeiro nivel —")
        for _, nomes in duplicados.items():
            print(f"    {nomes} -> inventariar so um")
    else:
        print("\n  Os drives tem conteudos distintos: sao bibliotecas diferentes,")
        print("  e todas entram no inventario.")

    # ---- 2. pastas de primeiro nivel na nuvem -------------------------
    pastas_nuvem = {}
    print("\n  PASTAS DE PRIMEIRO NIVEL")
    for d in drives:
        if duplicados and d.get("name") not in [v[0] for v in duplicados.values()]:
            pass  # ainda assim listamos: o relatorio mostra tudo
        filhos = [f for f in graph.filhos(d["id"]) if f.get("folder")]
        print(f"    [{d.get('name')}] {len(filhos)} pasta(s)")
        for f in filhos:
            print(f"      {f['name']:<40} {f.get('folder',{}).get('childCount','?'):>6} itens")
            pastas_nuvem.setdefault(normalizar(f["name"]), (d["id"], f["name"]))

    # ---- 3. bibliotecas que o banco conhece ---------------------------
    con = cu.conectar(cfg["_banco_path"])
    locais = [r[0] for r in con.execute(
        "SELECT biblioteca, COUNT(*) n FROM arquivo WHERE presente=1 "
        "GROUP BY biblioteca ORDER BY n DESC")]
    contagem = dict(con.execute(
        "SELECT biblioteca, COUNT(*) FROM arquivo WHERE presente=1 GROUP BY biblioteca"))

    print("\n  CORRESPONDENCIA — banco  ->  nuvem")
    mapa = {}
    nao_casaram = []
    for nome_local in locais:
        if not nome_local.strip():          # arquivo solto na raiz (desktop.ini)
            continue
        achado = casar(nome_local, pastas_nuvem)
        if achado:
            drive_id, nome_nuvem, como = achado
            mapa[nome_local] = {"drive_id": drive_id, "pasta_nuvem": nome_nuvem,
                                "como_casou": como, "arquivos_no_banco": contagem[nome_local]}
            print(f"    OK   {nome_local[:42]:<44} -> {nome_nuvem[:28]:<30} ({como})")
        else:
            nao_casaram.append(nome_local)
            print(f"    ???  {nome_local[:42]:<44} -> nao encontrado na nuvem "
                  f"({contagem[nome_local]} arquivos)")

    # ---- 4. teste real: resolver caminhos do banco na nuvem -----------
    print(f"\n  TESTE: resolvendo {args.amostra} caminhos do banco na nuvem")
    random.seed(args.semente)
    total = con.execute("SELECT COUNT(*) FROM arquivo WHERE presente=1 "
                        "AND temporario=0").fetchone()[0]
    amostra = con.execute(
        "SELECT caminho_rel, biblioteca, tamanho_bytes FROM arquivo "
        "WHERE presente=1 AND temporario=0 ORDER BY RANDOM() LIMIT ?",
        (args.amostra,)).fetchall()

    acertos = falhas = sem_mapa = 0
    divergencia_tamanho = 0
    exemplos_falha = []
    for linha in amostra:
        regra = mapa.get(linha["biblioteca"])
        if not regra:
            sem_mapa += 1
            continue
        resto = linha["caminho_rel"].split("\\", 1)[1] if "\\" in linha["caminho_rel"] else ""
        caminho_nuvem = f"{regra['pasta_nuvem']}\\{resto}" if resto else regra["pasta_nuvem"]
        try:
            item = graph.item_por_caminho(regra["drive_id"], caminho_nuvem)
            acertos += 1
            if int(item.get("size") or -1) != int(linha["tamanho_bytes"] or -2):
                divergencia_tamanho += 1
        except graph_ro.ErroGraph as exc:
            falhas += 1
            if len(exemplos_falha) < 5:
                exemplos_falha.append(f"{exc.status} {exc.codigo}: {caminho_nuvem}")

    testados = acertos + falhas
    taxa = 100 * acertos / testados if testados else 0
    print(f"    resolvidos         {acertos}/{testados}  ({taxa:.1f}%)")
    print(f"    sem regra de mapa  {sem_mapa}")
    print(f"    tamanho divergente {divergencia_tamanho}")
    for exemplo in exemplos_falha:
        print(f"      falhou: {exemplo}")

    # ---- 5. gravar o mapa ---------------------------------------------
    destino = cu.RAIZ_REPO / "config" / "mapa_drives.yaml"
    conteudo = {
        "gerado_em": cu.agora_iso(),
        "site_id": site,
        "drives": [{"id": d["id"], "nome": d.get("name"),
                    "tipo": d.get("driveType"), "web_url": d.get("webUrl"),
                    "raiz_id": raizes[d["id"]].get("id"),
                    "usado_gb": round((d.get("quota") or {}).get("used", 0) / 1024 ** 3, 1)}
                   for d in drives],
        "drives_com_raiz_compartilhada": {k: v for k, v in duplicados.items()},
        "mapa_biblioteca": mapa,
        "sem_correspondencia": nao_casaram,
        "amostra": {"testados": testados, "resolvidos": acertos,
                    "taxa_pct": round(taxa, 1), "sem_regra": sem_mapa,
                    "tamanho_divergente": divergencia_tamanho,
                    "universo": total},
    }
    destino.write_text(yaml.safe_dump(conteudo, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
    print(f"\n  mapa gravado em {destino}")
    print(f"  chamadas ao Graph: {graph.chamadas} | esperas por limite: {graph.esperas_429}")
    print("=" * 72)
    con.close()
    return 0 if taxa >= 95 or testados == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
