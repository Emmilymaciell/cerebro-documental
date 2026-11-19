"""
04_migrar_v4 — acrescenta a tabela `conteudo` (ocorrencia x conteudo).

Aditivo e idempotente: cria a tabela, a coluna `arquivo.conteudo_id` e as
views. Nao altera nenhum estado, nao mexe em extracao, derivado ou indice.

    python scripts\\04_migrar_v4.py
    python scripts\\04_migrar_v4.py --conferir

Regras (R10):
  - NUNCA rebaixa `meta.schema_versao`. Banco em v4 ou acima (v5 da busca)
    continua na versao que tinha; so banco abaixo de v4 passa a '4'.
  - Backup SO quando ha mudanca estrutural: falta a tabela `conteudo` ou a
    coluna `arquivo.conteudo_id`. Com a estrutura presente, o que o script
    faz e reaplicar o idempotente do `sql/schema_v4.sql` — DROP/CREATE das
    views do v4, `DROP INDEX IF EXISTS` do indice duplicado (M-12) e
    `CREATE INDEX IF NOT EXISTS` — que nao toca em linha nenhuma. Antes,
    rodado por engano num banco v5, fazia backup de ~1 GB e gravava '4'.
  - As views do v4 nao sao redefinidas pela v5 (`schema_v5_busca.sql` cria
    outras), entao reaplica-las num banco v5 nao desfaz nada da v5.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

SCHEMA_V4 = cu.RAIZ_REPO / "sql" / "schema_v4.sql"
VERSAO = "4"


def colunas(con, tabela: str) -> list[str]:
    return [linha[1] for linha in con.execute(f"PRAGMA table_info({tabela})")]


def gravar_versao(con) -> None:
    """schema_versao = '4'. So chamar quando o banco esta ABAIXO de v4."""
    con.execute(
        "INSERT INTO meta (chave, valor, atualizado_em) VALUES ('schema_versao', ?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, "
        "atualizado_em=excluded.atualizado_em", (VERSAO, cu.agora_iso()))


def main() -> int:
    ap = argparse.ArgumentParser(description="Migra o banco para o schema v4")
    ap.add_argument("--config", default=None)
    ap.add_argument("--conferir", action="store_true")
    ap.add_argument("--sem-backup", action="store_true",
                    help="so para teste automatizado")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    versao = (con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()
              or ["?"])[0]
    tem_tabela = con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                             "AND name='conteudo'").fetchone() is not None
    tem_coluna = "conteudo_id" in colunas(con, "arquivo")
    print(f"  schema atual   v{versao}")
    print(f"  tabela conteudo  {'existe' if tem_tabela else 'falta'}")
    print(f"  arquivo.conteudo_id {'existe' if tem_coluna else 'falta'}")

    if args.conferir:
        con.close()
        return 0
    ja_v4_ou_mais = str(versao).isdigit() and int(versao) >= int(VERSAO)
    if tem_tabela and tem_coluna:
        # A estrutura ja existe, mas as VIEWS podem ter mudado (o script usa
        # DROP VIEW + CREATE VIEW). Reaplicar e barato e nao toca em dado:
        # e assim que uma correcao de view chega ao banco que ja migrou.
        # Sem mudanca estrutural, sem backup; e a versao nunca desce.
        con.executescript(SCHEMA_V4.read_text(encoding="utf-8"))
        con.execute("CREATE INDEX IF NOT EXISTS ix_arquivo_conteudo ON arquivo(conteudo_id)")
        if not ja_v4_ou_mais:
            gravar_versao(con)
        con.commit()
        agora = con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()
        print(f"\nestrutura do v4 ja presente: views e indices do sql/schema_v4.sql "
              f"reaplicados, sem backup (nada estrutural mudou). schema agora "
              f"v{agora[0] if agora else '?'}.")
        con.close()
        return 0

    if not args.sem_backup:
        con.close()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrar3", cu.RAIZ_REPO / "scripts" / "03_migrar_v3.py")
        migrar = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migrar)
        destino = migrar.fazer_backup(cfg["_banco_path"])
        print(f"\n  backup: {destino} ({destino.stat().st_size/1024**2:.0f} MB)")
        con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])

    antes = {
        "arquivos": con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0],
        "extracoes": con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0],
        "trechos": con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0],
        "completed": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0],
    }

    if "conteudo_id" not in colunas(con, "arquivo"):
        con.execute("ALTER TABLE arquivo ADD COLUMN conteudo_id INTEGER")
    con.commit()
    con.executescript(SCHEMA_V4.read_text(encoding="utf-8"))
    con.execute("CREATE INDEX IF NOT EXISTS ix_arquivo_conteudo ON arquivo(conteudo_id)")
    if not ja_v4_ou_mais:                       # nunca rebaixa (R10)
        gravar_versao(con)
    con.commit()

    depois = {
        "arquivos": con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0],
        "extracoes": con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0],
        "trechos": con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0],
        "completed": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0],
    }
    print(f"\n  tabela `conteudo` + views v_ocorrencia, v_cobertura_conteudo, "
          f"v_conteudo_orfao")
    for chave in antes:
        marca = "OK " if antes[chave] == depois[chave] else "!! "
        print(f"    {marca}{chave:<11} {antes[chave]} -> {depois[chave]}")
    agora = con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()
    print(f"  schema agora   v{agora[0] if agora else '?'}")
    con.close()
    return 0 if antes == depois else 1


if __name__ == "__main__":
    raise SystemExit(main())
