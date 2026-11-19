"""
00_init_db — cria (ou atualiza) o banco do Cerebro Documental.

Idempotente: pode rodar quantas vezes quiser, nao apaga dado.

    python scripts\\00_init_db.py
    python scripts\\00_init_db.py --diagnostico
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

SCHEMA = cu.RAIZ_REPO / "sql" / "schema_v2.sql"
VERSAO_SCHEMA = "4"


def main() -> int:
    ap = argparse.ArgumentParser(description="Inicializa o banco do cerebro documental")
    ap.add_argument("--config", default=None)
    ap.add_argument("--diagnostico", action="store_true",
                    help="so mostra o diagnostico do ambiente e sai")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)

    diag = cu.diagnostico(cfg)
    print("--- diagnostico do ambiente ---")
    for chave, valor in diag.items():
        print(f"  {chave:24} {valor}")
    print()

    if args.diagnostico:
        return 0

    if not diag["fts5"]:
        print("AVISO: este Python nao tem FTS5. A busca textual ficara indisponivel.")

    # Guarda: o banco e os derivados nunca podem cair dentro do SharePoint.
    cu.assert_escrita_permitida(cfg["_banco_path"], cfg["_raiz_path"])
    cu.assert_escrita_permitida(cfg["_derivados_path"], cfg["_raiz_path"])
    cu.assert_escrita_permitida(cfg["_logs_path"], cfg["_raiz_path"])

    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    with open(SCHEMA, "r", encoding="utf-8") as fh:
        con.executescript(fh.read())

    # Banco novo nasce no schema ATUAL, nao no v2: as colunas de nuvem e as
    # tabelas de `item_nuvem`/`conteudo` fazem parte do desenho desde o E4/E7.
    # Reaproveita as listas dos proprios scripts de migracao — uma fonte so.
    import importlib.util

    def _carregar(nome, arquivo):
        spec = importlib.util.spec_from_file_location(
            nome, cu.RAIZ_REPO / "scripts" / arquivo)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        return modulo

    migrar3 = _carregar("migrar3", "03_migrar_v3.py")
    existentes = {linha[1] for linha in con.execute("PRAGMA table_info(arquivo)")}
    for nome, tipo in migrar3.COLUNAS_NUVEM:
        if nome not in existentes:
            con.execute(f"ALTER TABLE arquivo ADD COLUMN {nome} {tipo}")
    if "conteudo_id" not in existentes:
        con.execute("ALTER TABLE arquivo ADD COLUMN conteudo_id INTEGER")
    con.commit()
    for arquivo_sql in ("schema_v3.sql", "schema_v4.sql"):
        con.executescript((cu.RAIZ_REPO / "sql" / arquivo_sql).read_text(encoding="utf-8"))
    con.executescript(migrar3.INDICES)
    # As views do v3 (a `v_fila` com drive_id/item_id) moram no 03_migrar_v3.
    # Sem esta linha o banco novo ficava com a `v_fila` do v2 (CR-1, B-2).
    con.executescript(migrar3.VIEWS)
    con.execute("CREATE INDEX IF NOT EXISTS ix_arquivo_conteudo ON arquivo(conteudo_id)")
    con.commit()

    con.execute(
        "INSERT INTO meta (chave, valor, atualizado_em) VALUES ('schema_versao', ?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, atualizado_em=excluded.atualizado_em",
        (VERSAO_SCHEMA, cu.agora_iso()),
    )
    con.execute(
        "INSERT INTO meta (chave, valor, atualizado_em) VALUES ('raiz', ?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, atualizado_em=excluded.atualizado_em",
        (str(cfg["_raiz_path"]), cu.agora_iso()),
    )
    con.commit()

    tabelas = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]
    con.close()

    for pasta in (cfg["_derivados_path"] / "texto", cfg["_derivados_path"] / "perfil",
                  cfg["_logs_path"]):
        pasta.mkdir(parents=True, exist_ok=True)

    print(f"banco pronto: {cfg['_banco_path']}")
    print(f"objetos ({len(tabelas)}): {', '.join(tabelas)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
