"""
20_snapshot_schema — retrato NORMALIZADO do schema de um banco (CR-1).

Existe para responder, de forma mecanica, a pergunta da auditoria CR-1:
"o MESMO codigo do repositorio recria o schema de producao?". Le o
`sqlite_master` de um banco SEMPRE em modo somente leitura (`mode=ro`) e
escreve um texto canonico, independente de espacos, comentarios, aspas e de
como a tabela nasceu (CREATE inteiro x CREATE + ALTER TABLE ADD COLUMN).

    # retrato do banco de producao (le em modo ro; nao escreve no banco)
    python scripts\\20_snapshot_schema.py --saida sql\\schema_producao_v4.snapshot.sql

    # compara dois bancos (ex.: producao x um recriado pela receita)
    python scripts\\20_snapshot_schema.py --banco A.db --comparar B.db

O que entra no retrato, por objeto:
  tabela  -> colunas NA ORDEM (nome, tipo, NOT NULL, DEFAULT, PK), chaves
             estrangeiras, UNIQUE de tabela, CHECK, AUTOINCREMENT,
             WITHOUT ROWID. A ordem das colunas entra de proposito: so assim
             um `INSERT ... VALUES` posicional ficaria seguro (B-7).
  indice  -> tabela, colunas, UNIQUE e a clausula WHERE de indice parcial;
  view    -> o SQL, sem comentarios e com espacos/aspas normalizados;
  virtual -> idem (o FTS5 e suas tabelas-sombra);
  meta    -> `schema_versao`.

Nada aqui altera banco nenhum. A unica escrita e o arquivo `--saida`, que
nunca pode cair dentro do SharePoint (`assert_escrita_permitida`).
"""

from __future__ import annotations

import argparse
import difflib
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

SNAPSHOT_PADRAO = cu.RAIZ_REPO / "sql" / "schema_producao_v4.snapshot.sql"


def abrir_ro(banco: Path | str) -> sqlite3.Connection:
    """Abre SOMENTE LEITURA. Qualquer escrita levanta `attempt to write a
    readonly database` — e a garantia de que tirar o retrato do banco de
    producao nao muda uma vírgula nele."""
    caminho = Path(banco).resolve().as_posix()
    con = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ----------------------------------------------------------------------
# Normalizacao de SQL
# ----------------------------------------------------------------------

_COMENTARIO = re.compile(r"--[^\n]*")


def normalizar_sql(sql: str) -> str:
    """Tira comentarios, aspas duplas/crases de identificador e espacos.

    Aspas SIMPLES ficam: sao literais ('QUEUED'), e mudar um literal muda o
    significado. `IF NOT EXISTS` sai: e detalhe de como o objeto foi criado,
    nao do que ele e.
    """
    texto = _COMENTARIO.sub(" ", sql or "")
    texto = texto.replace('"', "").replace("`", "")
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = re.sub(r"\s*([(),;])\s*", r"\1", texto)
    texto = re.sub(r"(?i)\bIF NOT EXISTS\b\s*", "", texto)
    texto = re.sub(r",", ", ", texto)
    return texto.rstrip(";")


def _clausulas_check(sql: str) -> list[str]:
    """CHECK (...) com parenteses balanceados — PRAGMA nao os expoe."""
    texto = _COMENTARIO.sub(" ", sql or "")
    achados = []
    for m in re.finditer(r"(?i)\bCHECK\s*\(", texto):
        nivel, i = 1, m.end()
        while i < len(texto) and nivel:
            nivel += {"(": 1, ")": -1}.get(texto[i], 0)
            i += 1
        achados.append(re.sub(r"\s+", " ", texto[m.end():i - 1]).strip())
    return achados


# ----------------------------------------------------------------------
# O retrato
# ----------------------------------------------------------------------

def _tabela(con, nome: str, sql: str) -> list[str]:
    linhas = [f"TABLE {nome}"]
    for c in con.execute(f"PRAGMA table_xinfo('{nome}')"):
        if c["hidden"]:
            continue
        linhas.append(
            f"  COL {c['name']} {(c['type'] or '').upper()}"
            f"{' NOT NULL' if c['notnull'] else ''}"
            f"{' DEFAULT ' + str(c['dflt_value']) if c['dflt_value'] is not None else ''}"
            f"{' PK' + str(c['pk']) if c['pk'] else ''}")
    for fk in con.execute(f"PRAGMA foreign_key_list('{nome}')"):
        linhas.append(f"  FK {fk['from']} -> {fk['table']}({fk['to'] or ''})"
                      f" ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']}")
    for idx in con.execute(f"PRAGMA index_list('{nome}')"):
        if idx["origin"] == "u":        # UNIQUE declarado na tabela
            cols = [r["name"] for r in con.execute(f"PRAGMA index_info('{idx['name']}')")]
            linhas.append(f"  UNIQUE ({', '.join(cols)})")
    for chk in _clausulas_check(sql):
        linhas.append(f"  CHECK ({chk})")
    if re.search(r"(?i)\bAUTOINCREMENT\b", sql or ""):
        linhas.append("  AUTOINCREMENT")
    if re.search(r"(?i)\bWITHOUT\s+ROWID\b", sql or ""):
        linhas.append("  WITHOUT ROWID")
    return linhas


def _indice(con, nome: str, tabela: str, sql: str) -> list[str]:
    unico = bool(re.match(r"(?i)\s*CREATE\s+UNIQUE\b", sql or ""))
    cols = [r["name"] if r["name"] is not None else "<expr>"
            for r in con.execute(f"PRAGMA index_info('{nome}')")]
    parcial = re.search(r"(?is)\bWHERE\b(.*)$", _COMENTARIO.sub(" ", sql or ""))
    onde = f" WHERE {normalizar_sql(parcial.group(1))}" if parcial else ""
    return [f"INDEX {nome} ON {tabela} ({', '.join(cols)})"
            f"{' UNIQUE' if unico else ''}{onde}"]


def retrato(con: sqlite3.Connection) -> list[str]:
    """Lista de linhas canonicas, em ordem estavel (tipo, nome)."""
    objetos = con.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY "
        "CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 "
        "ELSE 3 END, name").fetchall()
    saida: list[str] = []
    for o in objetos:
        tipo, nome, tabela, sql = o["type"], o["name"], o["tbl_name"], o["sql"] or ""
        if tipo == "table" and re.match(r"(?i)\s*CREATE\s+VIRTUAL\b", sql):
            saida.append(f"VIRTUAL {nome} :: {normalizar_sql(sql)}")
        elif tipo == "table":
            saida.extend(_tabela(con, nome, sql))
        elif tipo == "index":
            saida.extend(_indice(con, nome, tabela, sql))
        elif tipo in ("view", "trigger"):
            saida.append(f"{tipo.upper()} {nome} :: {normalizar_sql(sql)}")
    tem_meta = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='meta'").fetchone()
    if tem_meta:
        versao = con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()
        saida.append(f"META schema_versao={versao[0] if versao else '?'}")
    return saida


def retrato_de(banco: Path | str) -> list[str]:
    con = abrir_ro(banco)
    try:
        return retrato(con)
    finally:
        con.close()


def ler_snapshot(caminho: Path | str = SNAPSHOT_PADRAO) -> list[str]:
    """As linhas do snapshot versionado, sem o cabecalho de comentario."""
    return [linha.rstrip("\n") for linha in
            Path(caminho).read_text(encoding="utf-8").splitlines()
            if linha.strip() and not linha.startswith("#")]


def diferencas(esperado: list[str], obtido: list[str],
               rotulos=("esperado", "obtido")) -> list[str]:
    return [linha for linha in difflib.unified_diff(
        esperado, obtido, fromfile=rotulos[0], tofile=rotulos[1], lineterm="", n=0)
        if not linha.startswith("@@")]


def main() -> int:
    ap = argparse.ArgumentParser(description="Retrato normalizado do schema (somente leitura)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--banco", default=None,
                    help="banco a retratar (padrao: o da config, aberto em modo ro)")
    ap.add_argument("--saida", default=None,
                    help="grava o retrato neste arquivo (ex.: sql/schema_producao_v4.snapshot.sql)")
    ap.add_argument("--comparar", default=None,
                    help="outro banco: mostra as diferencas de schema entre os dois")
    ap.add_argument("--snapshot", default=None,
                    help="compara o --banco com um snapshot versionado")
    ap.add_argument("--nota", default="",
                    help="linha de procedencia gravada no cabecalho do --saida")
    args = ap.parse_args()

    if args.banco:
        banco = Path(args.banco)
        raiz = None
    else:
        cfg = cu.carregar_config(args.config)
        banco, raiz = cfg["_banco_path"], cfg["_raiz_path"]
    linhas = retrato_de(banco)

    if args.comparar:
        dif = diferencas(linhas, retrato_de(args.comparar), (str(banco), args.comparar))
        print("\n".join(dif) if dif else "schemas IDENTICOS")
        return 1 if dif else 0
    if args.snapshot:
        dif = diferencas(ler_snapshot(args.snapshot), linhas, (args.snapshot, str(banco)))
        print("\n".join(dif) if dif else "schema IDENTICO ao snapshot")
        return 1 if dif else 0
    if args.saida:
        destino = Path(args.saida)
        if raiz is not None:
            cu.assert_escrita_permitida(destino, raiz)
        cabecalho = [
            "# Retrato normalizado do schema — gerado por scripts/20_snapshot_schema.py",
            f"# fonte: {banco.name} (aberto em modo somente leitura) em {cu.agora_iso()}",
            "# Linhas com '#' sao ignoradas na comparacao. NAO editar a mao: regenerar.",
            "# tests/test_schema.py exige que a receita do repositorio produza EXATAMENTE isto.",
        ] + ([f"# {args.nota}"] if args.nota else [])
        destino.write_text("\n".join(cabecalho + linhas) + "\n", encoding="utf-8",
                           newline="\n")
        print(f"retrato gravado: {destino} ({len(linhas)} linhas)")
        return 0
    print("\n".join(linhas))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
