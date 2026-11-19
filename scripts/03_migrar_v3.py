"""
03_migrar_v3 — leva o banco do schema v2 para o v3, sem perder nada.

O que faz, nesta ordem:

  1. BACKUP do banco inteiro (`data/backup/cerebro_v2_<carimbo>.db`);
  2. cria as tabelas da nuvem (`sql/schema_v3.sql`);
  3. acrescenta as colunas de nuvem em `arquivo` (ADD COLUMN, aditivo);
  4. RECONSTROI `arquivo` para remover o `UNIQUE(caminho_rel)`.

O passo 4 e o unico destrutivo — e por isso vem depois do backup. Ele existe
porque caminho deixou de ser identidade: renomear um arquivo e criar outro
com o nome antigo e um evento legitimo, e o banco tem de aceitar. A
identidade agora e `(drive_id, item_id)`.

Idempotente: rodar de novo nao faz nada se o banco ja estiver em v3.

    python scripts\\03_migrar_v3.py
    python scripts\\03_migrar_v3.py --conferir     # so diz em que versao esta
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

SCHEMA_V3 = cu.RAIZ_REPO / "sql" / "schema_v3.sql"
VERSAO = "3"

# Colunas de nuvem acrescentadas a `arquivo`.
#
# ORDEM, TIPO, NOT NULL e DEFAULT sao os do banco de PRODUCAO, e nao uma
# escolha estetica (CR-1): `ALTER TABLE ADD COLUMN` acrescenta no fim, na
# ordem desta lista, e o banco recriado tem de sair identico ao real —
# `tests/test_schema.py` compara os dois contra
# `sql/schema_producao_v4.snapshot.sql`. `presente_nuvem` e `origem` sao
# NOT NULL com DEFAULT em producao: linha que ainda nao foi reconciliada e
# `presente_nuvem=0` / `origem='local'`, nunca NULL.
COLUNAS_NUVEM = [
    ("drive_id", "TEXT"),
    ("item_id", "TEXT"),
    ("parent_id", "TEXT"),
    ("etag", "TEXT"),
    ("ctag", "TEXT"),                   # muda quando o conteudo muda
    ("quick_xor", "TEXT"),              # hash do servidor: identidade de conteudo
    ("web_url", "TEXT"),
    ("criado_por", "TEXT"),
    ("modificado_por", "TEXT"),
    ("criado_em_nuvem", "TEXT"),
    ("modificado_em_nuvem", "TEXT"),
    ("presente_nuvem", "INTEGER NOT NULL DEFAULT 0"),
    ("origem", "TEXT NOT NULL DEFAULT 'local'"),    # local | graph | ambos
    ("visto_nuvem_em", "TEXT"),
    ("caminho_anterior", "TEXT"),       # de onde veio, quando o delta o move
    ("caminho_nuvem", "TEXT"),          # caminho relativo a raiz do drive
]
# A confianca de cada vinculo NAO fica em `arquivo`: fica em
# `reconciliacao.confianca`, com metodo, candidatos e detalhe — uma linha
# por decisao, auditavel, inclusive as que nao foram aplicadas.

VIEWS = """
DROP VIEW IF EXISTS v_fila;
CREATE VIEW v_fila AS
SELECT id, caminho_rel, categoria, prioridade, tamanho_bytes, online, estado,
       tentativas, proxima_tentativa_em, drive_id, item_id
FROM arquivo
WHERE presente = 1
  AND suportado = 1
  AND estado IN ('QUEUED', 'CHANGED', 'READY', 'WAITING_HYDRATION', 'ERROR_RETRYABLE')
  AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= datetime('now'))
ORDER BY prioridade ASC, online ASC, tamanho_bytes ASC;

DROP VIEW IF EXISTS v_progresso;
CREATE VIEW v_progresso AS
SELECT estado, COUNT(*) AS arquivos, COALESCE(SUM(tamanho_bytes), 0) AS bytes
FROM arquivo WHERE presente = 1
GROUP BY estado ORDER BY arquivos DESC;

DROP VIEW IF EXISTS v_cobertura;
CREATE VIEW v_cobertura AS
SELECT categoria,
       COUNT(*)                                  AS arquivos,
       COALESCE(SUM(tamanho_bytes), 0)           AS bytes,
       SUM(online)                               AS so_na_nuvem,
       SUM(estado = 'COMPLETED')                 AS concluidos,
       SUM(estado = 'OCR_REQUIRED')              AS aguardando_ocr,
       SUM(estado = 'UNSUPPORTED')               AS sem_extrator,
       SUM(estado IN ('ERROR_RETRYABLE','ERROR_FINAL')) AS com_erro,
       ROUND(100.0 * SUM(estado = 'COMPLETED') / COUNT(*), 1) AS pct_concluido
FROM arquivo
WHERE presente = 1 AND temporario = 0
GROUP BY categoria ORDER BY arquivos DESC;

DROP VIEW IF EXISTS v_cobertura_prioridade;
CREATE VIEW v_cobertura_prioridade AS
SELECT prioridade,
       COUNT(*)                                        AS arquivos,
       SUM(suportado)                                  AS suportados,
       SUM(estado = 'COMPLETED')                       AS concluidos,
       SUM(estado IN ('QUEUED','CHANGED','READY','WAITING_HYDRATION','ERROR_RETRYABLE')) AS na_fila,
       COALESCE(SUM(tamanho_bytes), 0)                 AS bytes
FROM arquivo
WHERE presente = 1 AND temporario = 0
GROUP BY prioridade ORDER BY prioridade;

DROP VIEW IF EXISTS v_duplicata;
CREATE VIEW v_duplicata AS
SELECT sha256, COUNT(*) AS copias, SUM(tamanho_bytes) AS bytes_total,
       MIN(caminho_rel) AS exemplo
FROM arquivo
WHERE sha256 IS NOT NULL AND presente = 1
GROUP BY sha256 HAVING COUNT(*) > 1
ORDER BY copias DESC;

DROP VIEW IF EXISTS v_familia_versao;
CREATE VIEW v_familia_versao AS
SELECT nome_normalizado,
       COUNT(*)         AS variantes,
       MAX(mtime_utc)   AS mais_recente,
       MIN(caminho_rel) AS exemplo
FROM arquivo
WHERE presente = 1 AND temporario = 0 AND nome_normalizado <> ''
GROUP BY nome_normalizado HAVING COUNT(*) > 1
ORDER BY variantes DESC;
"""

INDICES = """
CREATE INDEX IF NOT EXISTS ix_arquivo_estado     ON arquivo(estado);
CREATE INDEX IF NOT EXISTS ix_arquivo_fila       ON arquivo(estado, prioridade, online, tamanho_bytes);
CREATE INDEX IF NOT EXISTS ix_arquivo_categoria  ON arquivo(categoria);
CREATE INDEX IF NOT EXISTS ix_arquivo_pasta      ON arquivo(pasta_rel);
CREATE INDEX IF NOT EXISTS ix_arquivo_biblioteca ON arquivo(biblioteca);
CREATE INDEX IF NOT EXISTS ix_arquivo_sha        ON arquivo(sha256);
CREATE INDEX IF NOT EXISTS ix_arquivo_norm       ON arquivo(nome_normalizado);
CREATE INDEX IF NOT EXISTS ix_arquivo_retry      ON arquivo(estado, proxima_tentativa_em);
CREATE INDEX IF NOT EXISTS ix_arquivo_caminho    ON arquivo(caminho_rel);
CREATE INDEX IF NOT EXISTS ix_arquivo_qxh        ON arquivo(quick_xor);
CREATE UNIQUE INDEX IF NOT EXISTS ux_arquivo_item
    ON arquivo(drive_id, item_id) WHERE item_id IS NOT NULL;
"""

def tabela_nova_sql(con) -> str:
    """O DDL da `arquivo` reconstruida = o DDL VIVO menos o UNIQUE.

    Antes isto era um molde fixo, e o molde envelheceu: faltava
    `caminho_anterior` (CR-1, defeito 2), e `presente_nuvem`/`origem` saiam
    sem NOT NULL/DEFAULT. Derivar da tabela viva garante que a reconstrucao
    preserva exatamente ordem, tipo, NOT NULL, DEFAULT e REFERENCES de
    TODAS as colunas — inclusive as que entrarem depois — e muda uma coisa
    so: o `UNIQUE` de `caminho_rel`.
    """
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                      "AND name='arquivo'").fetchone()[0]
    sql = _CAMINHO_UNICO.sub("caminho_rel TEXT NOT NULL", sql, count=1)
    sql = re.sub(r'(?i)^\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?["`]?arquivo["`]?\s*\(',
                 "CREATE TABLE arquivo_v3 (", sql, count=1)
    # auto-referencia (duplicata_de): o RENAME no fim devolve o nome certo
    return re.sub(r'(?i)REFERENCES\s+["`]?arquivo["`]?\s*\(', "REFERENCES arquivo_v3(", sql)


def colunas(con, tabela: str) -> list[str]:
    return [linha[1] for linha in con.execute(f"PRAGMA table_info({tabela})")]


def executar_ddl(con, script: str) -> None:
    """Como `executescript`, mas SEM commitar.

    `executescript()` fecha a transacao aberta antes de rodar. Dentro da
    reconstrucao isso desmanchava o `BEGIN IMMEDIATE` — o `COMMIT` no fim
    encontrava transacao nenhuma, e o `ROLLBACK` do `except` tambem nao
    tinha o que desfazer. Aqui cada comando vai por `execute()`, e a
    transacao continua de pe do inicio ao fim.
    """
    acumulado = ""
    for linha in script.splitlines(keepends=True):
        acumulado += linha
        if sqlite3.complete_statement(acumulado):
            comando = acumulado.strip()
            if comando:
                con.execute(comando)
            acumulado = ""
    resto = acumulado.strip()
    if resto:
        con.execute(resto)


# O DDL do `sql/schema_v2.sql` alinha as colunas com varios espacos
# (`caminho_rel        TEXT NOT NULL UNIQUE`). Procurar a frase com um espaco
# so nunca casava, e a reconstrucao do passo 4 jamais rodava num banco criado
# do zero pelo `00_init_db.py` — ele ficava com o UNIQUE de v2 para sempre.
_CAMINHO_UNICO = re.compile(r"caminho_rel\s+TEXT\s+NOT\s+NULL\s+UNIQUE", re.IGNORECASE)


def caminho_unico(con) -> bool:
    """True se ainda existe UNIQUE em caminho_rel (schema v2)."""
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                      "AND name='arquivo'").fetchone()
    return bool(sql) and bool(_CAMINHO_UNICO.search(sql[0] or ""))


def fazer_backup(banco: Path) -> Path:
    pasta = banco.parent / "backup"
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f"{banco.stem}_v2_{datetime.now():%Y%m%d_%H%M%S}.db"
    # VACUUM INTO gera uma copia consistente mesmo com WAL aberto.
    con = cu.conectar(banco)
    try:
        con.execute("VACUUM INTO ?", (str(destino),))
    except Exception:
        con.close()
        shutil.copy2(banco, destino)
    else:
        con.close()
    return destino


def reconstruir_sem_unique(con, antes: int) -> int:
    """Reconstroi `arquivo` sem o UNIQUE(caminho_rel), numa transacao REAL.

    CR-1, defeito 3: o `BEGIN IMMEDIATE` manual convivia com o controle de
    transacao implicito do modulo `sqlite3`, e a migracao chegou a terminar
    com "cannot commit - no transaction is active" DEPOIS do `DROP TABLE`
    — sem rollback possivel e sem nenhuma das conferencias. Aqui a conexao
    passa para autocommit (`isolation_level=None`) durante a reconstrucao:
    so o nosso `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` mandam, e a
    conferencia de chaves estrangeiras roda DENTRO da transacao, antes do
    commit. Falhou qualquer coisa: rollback e a tabela antiga continua la.

    Devolve o numero de linhas copiadas.
    """
    con.commit()
    nivel_antigo = con.isolation_level
    con.isolation_level = None
    lista = ", ".join(colunas(con, "arquivo"))
    # TODA view precisa sair antes do RENAME: o SQLite valida as views
    # existentes ao renomear a tabela, e qualquer uma que fale de
    # `arquivo` (as do v4, por exemplo) derruba a migracao inteira.
    views_salvas = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' "
        "AND sql IS NOT NULL ORDER BY name").fetchall()
    # O DROP TABLE leva os indices junto. Os que a tabela viva tem (inclusive
    # os que nao estao em INDICES, como `ix_arquivo_conteudo`) voltam iguais.
    indices_salvos = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='arquivo' AND sql IS NOT NULL ORDER BY name").fetchall()
    con.execute("PRAGMA foreign_keys=OFF")
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            for nome_view, _ in views_salvas:
                con.execute(f"DROP VIEW IF EXISTS {nome_view}")
            con.execute(tabela_nova_sql(con))
            con.execute(f"INSERT INTO arquivo_v3 ({lista}) SELECT {lista} FROM arquivo")
            copiados = con.execute("SELECT COUNT(*) FROM arquivo_v3").fetchone()[0]
            if copiados != antes:
                raise RuntimeError(f"copia incompleta: {copiados} de {antes}")
            con.execute("DROP TABLE arquivo")
            con.execute("ALTER TABLE arquivo_v3 RENAME TO arquivo")
            for _, sql_indice in indices_salvos:
                con.execute(sql_indice)
            executar_ddl(con, INDICES)
            for _, sql_view in views_salvas:
                con.execute(sql_view)
            executar_ddl(con, VIEWS)
            problemas = con.execute("PRAGMA foreign_key_check").fetchall()
            if problemas:
                raise RuntimeError("chaves estrangeiras quebradas apos a reconstrucao: "
                                   f"{[tuple(p) for p in problemas[:3]]}")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
    finally:
        con.execute("PRAGMA foreign_keys=ON")
        con.isolation_level = nivel_antigo
    return copiados


def main() -> int:
    ap = argparse.ArgumentParser(description="Migra o banco para o schema v3")
    ap.add_argument("--config", default=None)
    ap.add_argument("--conferir", action="store_true")
    ap.add_argument("--sem-backup", action="store_true",
                    help="so para teste automatizado; nunca usar no banco real")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    banco = cfg["_banco_path"]
    if not banco.exists():
        print(f"banco nao existe: {banco}")
        return 2

    con = cu.conectar(banco, cfg["_raiz_path"])
    versao = (con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()
              or ["?"])[0]
    faltando = [c for c, _ in COLUNAS_NUVEM if c not in colunas(con, "arquivo")]
    tem_nuvem = con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name='item_nuvem'").fetchone() is not None

    print(f"  schema atual        v{versao}")
    print(f"  tabelas da nuvem    {'ja existem' if tem_nuvem else 'faltam'}")
    print(f"  colunas de nuvem    {len(COLUNAS_NUVEM) - len(faltando)}/{len(COLUNAS_NUVEM)}")
    print(f"  UNIQUE(caminho_rel) {'ainda existe (v2)' if caminho_unico(con) else 'ja removido'}")
    antes = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    print(f"  arquivos no banco   {antes:,}".replace(",", "."))

    if args.conferir:
        con.close()
        return 0

    if tem_nuvem and not faltando and not caminho_unico(con):
        print("\nnada a fazer: o banco ja esta em v3.")
        con.close()
        return 0

    # ---- 1. backup ----------------------------------------------------
    if not args.sem_backup:
        con.close()
        destino = fazer_backup(banco)
        tamanho = destino.stat().st_size / 1024 ** 2
        print(f"\n  backup: {destino}  ({tamanho:.1f} MB)")
        con = cu.conectar(banco, cfg["_raiz_path"])

    # ---- 2. colunas novas em `arquivo` --------------------------------
    # Vem ANTES das tabelas: o schema v3 cria um indice sobre
    # arquivo(drive_id, item_id), que so existe depois destes ALTER.
    for nome, tipo in COLUNAS_NUVEM:
        if nome not in colunas(con, "arquivo"):
            con.execute(f"ALTER TABLE arquivo ADD COLUMN {nome} {tipo}")
    con.commit()
    print(f"  colunas de nuvem em `arquivo`: {len(COLUNAS_NUVEM)} garantidas")

    # ---- 3. tabelas da nuvem ------------------------------------------
    con.executescript(SCHEMA_V3.read_text(encoding="utf-8"))
    con.commit()
    print("  tabelas da nuvem: drive, delta_token, item_nuvem, evento_nuvem, "
          "reconciliacao")

    # ---- 4. reconstrucao: caminho_rel deixa de ser UNIQUE -------------
    if caminho_unico(con):
        copiados = reconstruir_sem_unique(con, antes)
        problemas = list(con.execute("PRAGMA foreign_key_check"))
        if problemas:
            raise RuntimeError(f"chaves estrangeiras quebradas apos a migracao: {problemas[:3]}")
        print(f"  `arquivo` reconstruida: {copiados:,} linhas, "
              f"UNIQUE(caminho_rel) removido".replace(",", "."))
    else:
        con.executescript(INDICES)
        con.executescript(VIEWS)
        con.commit()

    # Nunca rebaixa: um banco ja em v4 que passou por aqui continua v4.
    if not str(versao).isdigit() or int(versao) < int(VERSAO):
        con.execute("INSERT INTO meta (chave, valor, atualizado_em) VALUES "
                    "('schema_versao', ?, ?) ON CONFLICT(chave) DO UPDATE SET "
                    "valor=excluded.valor, atualizado_em=excluded.atualizado_em",
                    (VERSAO, cu.agora_iso()))
        con.commit()

    depois = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    completos = con.execute("SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0]
    extracoes = con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0]
    trechos = con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0]
    print(f"\n  CONFERENCIA: {depois:,} arquivos (antes {antes:,}) | "
          f"{completos} COMPLETED | {extracoes} extracoes | {trechos} trechos"
          .replace(",", "."))
    agora = con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()
    print(f"  schema agora        v{agora[0] if agora else '?'}")
    con.close()
    return 0 if depois == antes else 1


if __name__ == "__main__":
    raise SystemExit(main())
