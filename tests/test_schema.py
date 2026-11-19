"""
test_schema — CR-1: a receita do repositorio recria o schema de PRODUCAO.

    python tests\\test_schema.py

A auditoria (docs/11_QA_AUDITORIA.md, CR-1) mostrou que `00_init_db` +
`03_migrar_v3` + `04_migrar_v4` produziam uma tabela `arquivo` diferente da
de producao — e que, por isso, TODAS as suites rodavam contra um schema que
nao era o real. Este teste fecha isso de forma mecanica:

  1. banco do zero (00_init_db) == snapshot versionado de producao
     (`sql/schema_producao_v4.snapshot.sql`, gerado por
     `scripts/20_snapshot_schema.py` lendo o banco real em modo ro);
  2. rodar as migracoes por cima (03, 04) nao muda nada (idempotencia);
  3. um banco v2 LEGADO, com o `UNIQUE(caminho_rel)`, migrado por 03 + 04,
     chega EXATAMENTE ao mesmo schema, sem perder linha;
  4. a reconstrucao do 03 e atomica de verdade: falhou, rollback — a tabela
     antiga continua la (o defeito 3 da CR-1 terminava depois do DROP TABLE);
  5. regressao da DEC-015: nenhum UNIQUE em caminho_rel; dois item_id no
     mesmo caminho convivem; o mesmo (drive_id, item_id) e recusado;
  6. CONTROLE NEGATIVO: reintroduzir o UNIQUE ou remover um indice faz a
     comparacao falhar — prova de que o teste nao e vacuo.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import RAIZ, area_temporaria, checar, escrever_config, placar_final, rodar  # noqa: E402

import cerebro_util as cu  # noqa: E402

snap = cu.importar_script("20_snapshot_schema")
SNAPSHOT = RAIZ / "sql" / "schema_producao_v4.snapshot.sql"


# ----------------------------------------------------------------------
# Detectores — usados no banco bom E nos bancos sabotados
# ----------------------------------------------------------------------
def unique_em_caminho(con) -> bool:
    """True se QUALQUER coisa garante unicidade de caminho_rel sozinho:
    UNIQUE de coluna/tabela (autoindex) ou indice UNIQUE explicito."""
    for idx in con.execute("PRAGMA index_list('arquivo')"):
        if not idx[2]:                                    # coluna `unique`
            continue
        cols = [r[2] for r in con.execute(f"PRAGMA index_info('{idx[1]}')")]
        if cols == ["caminho_rel"]:
            return True
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='arquivo'").fetchone()[0]
    return bool(re.search(r"caminho_rel\s+TEXT\s+NOT\s+NULL\s+UNIQUE", sql, re.I))


def bate_com_snapshot(banco: Path) -> list[str]:
    return snap.diferencas(snap.ler_snapshot(SNAPSHOT), snap.retrato_de(banco),
                           ("snapshot de producao", "banco testado"))


def montar(area: Path) -> tuple[Path, Path]:
    raiz = area / "acervo_falso"
    raiz.mkdir(parents=True, exist_ok=True)
    config = escrever_config(area, raiz)
    return config, area / "teste.db"


def inserir(con, caminho: str, item_id: str | None, drive="drive!X"):
    con.execute(
        "INSERT INTO arquivo (caminho_rel, pasta_rel, nome, drive_id, item_id, "
        "primeira_vez_em, ultima_vez_em) VALUES (?,?,?,?,?,?,?)",
        (caminho, "P", Path(caminho).name, drive, item_id, cu.agora_iso(), cu.agora_iso()))


def banco_v2_legado(destino: Path, com_violacao_fk: bool = False) -> None:
    """Um banco como o v2 ORIGINAL: `caminho_rel TEXT NOT NULL UNIQUE`."""
    ddl = (RAIZ / "sql" / "schema_v2.sql").read_text(encoding="utf-8")
    ddl, n = re.subn(r"(caminho_rel\s+TEXT NOT NULL),", r"\1 UNIQUE,", ddl, count=1)
    assert n == 1, "nao achei a coluna caminho_rel no schema_v2.sql"
    con = sqlite3.connect(str(destino))
    con.executescript(ddl)
    con.execute("INSERT INTO meta VALUES ('schema_versao','2',?)", (cu.agora_iso(),))
    for i in range(3):
        con.execute("INSERT INTO arquivo (caminho_rel, pasta_rel, nome, estado, "
                    "primeira_vez_em, ultima_vez_em) VALUES (?,?,?,?,?,?)",
                    (f"B\\doc{i}.pdf", "B", f"doc{i}.pdf", "COMPLETED",
                     cu.agora_iso(), cu.agora_iso()))
    con.execute("INSERT INTO evento_arquivo (arquivo_id, evento, em) VALUES (1,'x',?)",
                (cu.agora_iso(),))
    if com_violacao_fk:
        con.commit()                       # PRAGMA dentro de transacao e ignorado
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("INSERT INTO evento_arquivo (arquivo_id, evento, em) "
                    "VALUES (999,'orfao',?)", (cu.agora_iso(),))
    con.commit()
    con.close()


# ----------------------------------------------------------------------
def main() -> int:
    checar(SNAPSHOT.exists(), "o snapshot de producao esta versionado", str(SNAPSHOT))
    linhas_snapshot = snap.ler_snapshot(SNAPSHOT)
    checar(any(l == "META schema_versao=4" for l in linhas_snapshot),
           "o snapshot e do schema v4")

    # ==================================================================
    print("\n--- 1. banco do zero == producao ---")
    area = area_temporaria("cerebro_schema_")
    config, banco = montar(area)
    r = rodar("00_init_db.py", config)
    checar(r.returncode == 0, "00_init_db roda", r.stderr[-400:])
    dif = bate_com_snapshot(banco)
    checar(dif == [], "00_init_db cria EXATAMENTE o schema de producao",
           "\n" + "\n".join(dif[:30]))

    print("\n--- 2. migracoes por cima nao mudam nada ---")
    r3 = rodar("03_migrar_v3.py", config, "--sem-backup")
    r4 = rodar("04_migrar_v4.py", config, "--sem-backup")
    checar(r3.returncode == 0 and r4.returncode == 0, "03 e 04 rodam no banco novo",
           (r3.stderr + r4.stderr)[-400:])
    dif = bate_com_snapshot(banco)
    checar(dif == [], "00 + 03 + 04 continua identico a producao",
           "\n" + "\n".join(dif[:30]))
    con = cu.conectar(banco)
    checar(con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()[0]
           == "4", "o 03 nao rebaixou schema_versao de 4 para 3")
    colunas = [c[1] for c in con.execute("PRAGMA table_info(arquivo)")]
    checar(colunas[-3:] == ["caminho_anterior", "caminho_nuvem", "conteudo_id"],
           "ordem das colunas igual a de producao (nuvem no fim, conteudo_id por ultimo)",
           str(colunas[-3:]))

    print("\n--- 5. regressao da DEC-015 no banco novo ---")
    checar(not unique_em_caminho(con), "nenhum UNIQUE em caminho_rel")
    inserir(con, "A\\mesmo_lugar.pdf", "item!velho")
    try:
        inserir(con, "A\\mesmo_lugar.pdf", "item!novo")
        convivem = True
    except sqlite3.IntegrityError:
        convivem = False
    checar(convivem, "dois item_id no MESMO caminho convivem (apagado e subido de novo)")
    try:
        inserir(con, "B\\outro_lugar.pdf", "item!velho")
        recusou = False
    except sqlite3.IntegrityError:
        recusou = True
    checar(recusou, "o mesmo (drive_id, item_id) e recusado (ux_arquivo_item)")
    try:
        inserir(con, "C\\x.pdf", "item!velho", drive="drive!OUTRO")
        outro_drive = True
    except sqlite3.IntegrityError:
        outro_drive = False
    checar(outro_drive, "o mesmo item_id em OUTRO drive e outro item")
    con.rollback()
    for nome, valor in (("presente_nuvem", 0), ("origem", "local")):
        con.execute("INSERT INTO arquivo (caminho_rel, pasta_rel, nome, primeira_vez_em, "
                    "ultima_vez_em) VALUES ('D\\d.pdf','D','d.pdf',?,?)",
                    (cu.agora_iso(), cu.agora_iso()))
        obtido = con.execute(f"SELECT {nome} FROM arquivo ORDER BY id DESC LIMIT 1").fetchone()[0]
        checar(obtido == valor, f"{nome} nasce com DEFAULT {valor!r} (NOT NULL, como producao)",
               repr(obtido))
    con.rollback()
    con.close()

    # ==================================================================
    print("\n--- 6. CONTROLE NEGATIVO: o teste falha quando deve ---")
    sabotado = area / "sabotado_unique.db"
    shutil.copy2(banco, sabotado)
    s = sqlite3.connect(str(sabotado))
    s.execute("CREATE UNIQUE INDEX ux_caminho_volta ON arquivo(caminho_rel)")
    s.commit()
    checar(unique_em_caminho(s), "detector ACHA o UNIQUE reintroduzido")
    inserir(s, "Z\\z.pdf", "i1")
    try:
        inserir(s, "Z\\z.pdf", "i2")
        barrou = False
    except sqlite3.IntegrityError:
        barrou = True
    checar(barrou, "com o UNIQUE de volta, reuso de caminho QUEBRA (o que a DEC-015 proibe)")
    s.close()
    checar(bate_com_snapshot(sabotado) != [], "comparacao com o snapshot FALHA com UNIQUE extra")

    sem_indice = area / "sabotado_indice.db"
    shutil.copy2(banco, sem_indice)
    s = sqlite3.connect(str(sem_indice))
    s.execute("DROP INDEX ix_arquivo_qxh")
    s.commit()
    s.close()
    dif = bate_com_snapshot(sem_indice)
    checar(any("ix_arquivo_qxh" in l for l in dif),
           "comparacao com o snapshot FALHA quando some um indice", str(dif))

    nulavel = area / "sabotado_coluna.db"
    # coluna de nuvem do jeito antigo (anulavel) — o defeito exato da CR-1
    s = sqlite3.connect(str(nulavel))
    s.executescript((RAIZ / "sql" / "schema_v2.sql").read_text(encoding="utf-8"))
    s.execute("ALTER TABLE arquivo ADD COLUMN presente_nuvem INTEGER")
    s.commit()
    s.close()
    checar(any("presente_nuvem INTEGER" in l and "NOT NULL" not in l
               for l in snap.retrato_de(nulavel)),
           "o retrato distingue coluna anulavel de NOT NULL DEFAULT")

    # ==================================================================
    print("\n--- 3. banco v2 LEGADO (com UNIQUE) migrado chega ao mesmo schema ---")
    area_v2 = area_temporaria("cerebro_schema_v2_")
    config_v2, banco_v2 = montar(area_v2)
    banco_v2_legado(banco_v2)
    m3 = cu.importar_script("03_migrar_v3")
    con = sqlite3.connect(str(banco_v2))
    checar(m3.caminho_unico(con), "o 03 DETECTA o UNIQUE do v2 (defeito 1 da CR-1)")
    con.close()
    r3 = rodar("03_migrar_v3.py", config_v2, "--sem-backup")
    checar(r3.returncode == 0, "03 migra o v2 legado sem erro", (r3.stdout + r3.stderr)[-800:])
    checar("reconstruida" in r3.stdout, "e diz que reconstruiu", r3.stdout[-300:])
    r4 = rodar("04_migrar_v4.py", config_v2, "--sem-backup")
    checar(r4.returncode == 0, "04 roda depois", r4.stderr[-400:])
    dif = bate_com_snapshot(banco_v2)
    checar(dif == [], "v2 legado + 03 + 04 == schema de producao", "\n" + "\n".join(dif[:30]))
    con = cu.conectar(banco_v2)
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == 3,
           "as 3 linhas atravessaram a reconstrucao")
    checar(con.execute("SELECT COUNT(*) FROM evento_arquivo").fetchone()[0] == 1,
           "o historico atravessou a reconstrucao")
    checar(not unique_em_caminho(con), "o UNIQUE saiu")
    checar(con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()[0]
           == "4", "meta.schema_versao = 4")
    checar(list(con.execute("PRAGMA foreign_key_check")) == [], "foreign_key_check limpo")
    con.close()

    # ==================================================================
    print("\n--- 4. reconstrucao atomica: falhou, rollback ---")
    area_rb = area_temporaria("cerebro_schema_rb_")
    config_rb, banco_rb = montar(area_rb)
    banco_v2_legado(banco_rb, com_violacao_fk=True)
    r3 = rodar("03_migrar_v3.py", config_rb, "--sem-backup")
    checar(r3.returncode != 0, "a migracao RECUSA terminar com chave estrangeira quebrada")
    checar("chaves estrangeiras" in r3.stderr, "e diz por que", r3.stderr[-300:])
    checar("no transaction is active" not in r3.stderr,
           "sem o 'cannot commit/rollback - no transaction is active' da CR-1")
    con = sqlite3.connect(str(banco_rb))
    checar(m3.caminho_unico(con), "rollback: a tabela ANTIGA (com UNIQUE) continua la")
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == 3,
           "rollback: nenhuma linha perdida")
    checar(con.execute("SELECT name FROM sqlite_master WHERE name='arquivo_v3'").fetchone()
           is None, "rollback: a tabela temporaria nao ficou para tras")
    checar(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()[0]
           > 0, "rollback: as views derrubadas voltaram")
    checar(con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()[0]
           == "2", "rollback: nao declarou sucesso (schema_versao continua 2)")
    con.close()

    area_v5 = teste_04_nao_rebaixa()

    for pasta in (area, area_v2, area_rb, area_v5):
        shutil.rmtree(pasta, ignore_errors=True)
    return placar_final()


def teste_04_nao_rebaixa() -> Path:
    """R10: o 04 rodado num banco ja em v5 nao rebaixa nem faz backup a toa."""
    print("\n--- 7. R10: 04 num banco v5 mantem a versao e nao faz backup ---")
    area = area_temporaria("cerebro_schema_v5_")
    config, banco = montar(area)
    checar(rodar("00_init_db.py", config).returncode == 0, "00_init_db roda")
    r5 = rodar("05_migrar_v5.py", config, "--aplicar", "--sem-backup")
    con = cu.conectar(banco)
    versao = lambda: con.execute(  # noqa: E731
        "SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()[0]
    checar(r5.returncode == 0 and versao() == "5", "o banco esta em v5 (05 aplicado)",
           f"{r5.returncode} v{versao()} {(r5.stdout + r5.stderr)[-300:]}")
    objetos_v5 = sorted(r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'v_busca%' OR name LIKE 'v_reindexar' "
        "OR name LIKE 'v_trecho_ocorrencia'"))
    con.close()

    r4 = rodar("04_migrar_v4.py", config)            # SEM --sem-backup: e o caso real
    con = cu.conectar(banco)
    checar(r4.returncode == 0, "04 roda no banco v5", (r4.stdout + r4.stderr)[-400:])
    checar(versao() == "5", "04 NAO rebaixa schema_versao de 5 para 4", f"v{versao()}")
    backups = list((area / "backup").glob("*.db")) if (area / "backup").exists() else []
    checar(backups == [], "sem mudanca estrutural: nenhum backup (era ~1 GB em producao)",
           str(backups))
    checar(sorted(r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'v_busca%' OR name LIKE 'v_reindexar' "
        "OR name LIKE 'v_trecho_ocorrencia'")) == objetos_v5 and objetos_v5,
           "os objetos da v5 continuam la", str(objetos_v5))
    checar(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND "
                       "name IN ('v_ocorrencia','v_cobertura_conteudo','v_conteudo_orfao',"
                       "'v_fila_nuvem')").fetchone()[0] == 4,
           "as views do v4 foram reaplicadas (idempotente)")
    con.execute("CREATE INDEX ix_arquivo_quickxor ON arquivo(quick_xor)")
    con.commit()
    con.close()
    r4b = rodar("04_migrar_v4.py", config)
    con = cu.conectar(banco)
    checar(r4b.returncode == 0 and con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='ix_arquivo_quickxor'").fetchone()[0] == 0
           and versao() == "5",
           "e o que e idempotente do v4 (DROP do indice duplicado M-12) continua valendo")
    con.close()
    return area


if __name__ == "__main__":
    raise SystemExit(main())
