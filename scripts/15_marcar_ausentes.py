"""
15_marcar_ausentes — marca como MISSING o que a auditoria provou ausente.

`MISSING` aqui quer dizer exatamente isto, e nada mais:

    o Cerebro conhece este registro historico, mas o item NAO esta
    disponivel hoje no escopo acessivel pelo Graph.

O que **nao** acontece: nenhuma linha e apagada, nenhuma extracao e removida,
nenhum derivado e deletado, a classificacao P0..P4 continua, os vinculos
continuam, e o estado anterior fica gravado no evento para poder voltar.

Quem entra: as linhas que a reconciliacao classificou como
`SEM_CORRESPONDENCIA` **com metodo `nenhum`** — procuradas na nuvem por
caminho, por nome+tamanho e pela posicao relativa, e nao encontradas.

Quem NAO entra: as 594 linhas de `Coordenacao Programa - 02 Repositorio`
(metodo `sem_mapa`). Elas nao estao ausentes — estao **fora do escopo**
(outro site do SharePoint). Confundir as duas coisas seria mentir no
inventario.

    python scripts\\15_marcar_ausentes.py            # so conta e mostra
    python scripts\\15_marcar_ausentes.py --aplicar  # marca, com backup antes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

MOTIVO = "auditoria 2026-09-20: nao encontrado na nuvem por caminho, nome+tamanho nem posicao relativa"

SELECAO = """
SELECT a.id, a.estado, a.caminho_rel, a.temporario, a.prioridade
FROM arquivo a
JOIN reconciliacao r ON r.arquivo_id = a.id
WHERE r.confianca = 'SEM_CORRESPONDENCIA'
  AND r.metodo = 'nenhum'
  AND a.presente = 1
"""


def levantar(con) -> list:
    return con.execute(SELECAO).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description="Marca como MISSING o que sumiu da nuvem")
    ap.add_argument("--config", default=None)
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--quieto", action="store_true")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])

    alvos = levantar(con)
    por_estado: dict[str, int] = {}
    for linha in alvos:
        por_estado[linha["estado"]] = por_estado.get(linha["estado"], 0) + 1

    print("=" * 66)
    print(f"{'APLICANDO' if args.aplicar else 'SIMULACAO'} — marcar ausentes como MISSING")
    print("=" * 66)
    print(f"  registros a afetar: {len(alvos):,}".replace(",", "."))
    for estado, n in sorted(por_estado.items(), key=lambda x: -x[1]):
        print(f"    {estado:<16} {n:>6}")
    fora_do_escopo = con.execute(
        "SELECT COUNT(*) FROM reconciliacao WHERE confianca='SEM_CORRESPONDENCIA' "
        "AND metodo='sem_mapa'").fetchone()[0]
    print(f"  NAO entram (fora do escopo, Coordenacao): {fora_do_escopo}")

    if not args.aplicar:
        print("\n  nada foi alterado. Para aplicar:")
        print("     python scripts\\15_marcar_ausentes.py --aplicar")
        con.close()
        return 0

    # --- backup antes -------------------------------------------------
    con.close()
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrar", cu.RAIZ_REPO / "scripts" / "03_migrar_v3.py")
    migrar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migrar)
    backup = migrar.fazer_backup(cfg["_banco_path"])
    print(f"\n  backup: {backup} ({backup.stat().st_size/1024**2:.0f} MB)")
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])

    execucao_id = cu.abrir_execucao(con, "marcar_ausentes", "", vars(args))
    log = cu.Log(cfg["_logs_path"], "marcar_ausentes", con, execucao_id,
                 verboso=not args.quieto)

    antes = {
        "extracoes": con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0],
        "trechos": con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0],
        "linhas": con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0],
        "pastas": con.execute("SELECT COUNT(*) FROM pasta").fetchone()[0],
        "completed": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0],
    }

    agora = cu.agora_iso()
    marcados = 0
    for linha in alvos:
        # O estado anterior vai para o evento: e o que permite voltar atras
        # sem adivinhacao se o item reaparecer no delta.
        con.execute(
            "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, detalhe, em) "
            "VALUES (?,?,?,?,?)",
            (linha["id"], execucao_id, "ausente_na_nuvem",
             json.dumps({"estado_anterior": linha["estado"], "motivo": MOTIVO,
                         "prioridade": linha["prioridade"]}, ensure_ascii=False), agora))
        con.execute(
            "UPDATE arquivo SET estado='MISSING', presente=0, presente_nuvem=0, "
            "visto_nuvem_em=? WHERE id=?", (agora, linha["id"]))
        marcados += 1
        if marcados % 500 == 0:
            con.commit()
    con.commit()

    depois = {
        "extracoes": con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0],
        "trechos": con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0],
        "linhas": con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0],
        "pastas": con.execute("SELECT COUNT(*) FROM pasta").fetchone()[0],
        "completed": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0],
    }
    agora_missing = con.execute(
        "SELECT COUNT(*) FROM arquivo WHERE estado='MISSING'").fetchone()[0]
    eventos = con.execute(
        "SELECT COUNT(*) FROM evento_arquivo WHERE evento='ausente_na_nuvem'").fetchone()[0]
    fila_sem_id = con.execute(
        "SELECT COUNT(*) FROM v_fila WHERE item_id IS NULL").fetchone()[0]

    print(f"\n  marcados: {marcados:,}".replace(",", "."))
    print(f"  estado MISSING no banco: {agora_missing:,}".replace(",", "."))
    print(f"  eventos 'ausente_na_nuvem': {eventos:,}".replace(",", "."))
    print(f"  fila com item_id nulo: {fila_sem_id}")
    # `completed` PODE cair: um dos ausentes ja tinha sido processado, e agora
    # e MISSING. A extracao dele continua na base — o que nao pode mudar sao
    # os acervos de trabalho: extracoes, indice, linhas e classificacao.
    intocaveis = ("extracoes", "trechos", "linhas", "pastas")
    print("\n  PRESERVACAO (antes -> depois)")
    for chave in antes:
        if chave in intocaveis:
            marca = "OK " if antes[chave] == depois[chave] else "!! "
        else:
            marca = "-- "
        print(f"    {marca}{chave:<12} {antes[chave]} -> {depois[chave]}"
              + ("   (esperado: ausente que ja tinha sido processado)"
                 if chave == "completed" and antes[chave] != depois[chave] else ""))

    intacto = all(antes[c] == depois[c] for c in intocaveis)
    cu.fechar_execucao(con, execucao_id, "concluida", {"arquivos": marcados}, 0.0)
    log.fechar()
    con.close()
    return 0 if marcados == len(alvos) and intacto else 1


if __name__ == "__main__":
    raise SystemExit(main())
