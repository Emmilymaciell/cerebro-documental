"""
test_scanner — inventario + classificacao P0..P4, numa arvore falsa.

    python tests\\test_scanner.py

Cobre: unidades do util, criacao do banco, inventario, idempotencia,
deteccao de novo/alterado/sumido, retomada apos interrupcao,
classificacao por obra e categoria, enfileiramento e guarda read-only.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import (RAIZ, area_temporaria, checar, escrever_config, foto_exata,  # noqa: E402
                   placar_final, rodar)

import cerebro_util as cu  # noqa: E402


def montar_arvore(raiz: Path):
    (raiz / "RD103" / "3. Notificações").mkdir(parents=True)
    (raiz / "RD103" / "Fotos").mkdir(parents=True)
    (raiz / "Administrativo" / "Contratos").mkdir(parents=True)
    (raiz / "Outra").mkdir(parents=True)
    (raiz / "Backup").mkdir(parents=True)
    (raiz / "RD103" / "3. Notificações" / "notificacao 001.pdf").write_bytes(b"%PDF-1.4 x")
    (raiz / "RD103" / "Fotos" / "foto.JPG").write_bytes(b"\xff\xd8\xff")
    (raiz / "Administrativo" / "Contratos" / "contrato.pdf").write_bytes(b"%PDF-1.4 a")
    (raiz / "Administrativo" / "Contratos" / "Contrato Rev 02 (cópia).pdf").write_bytes(b"%PDF-1.4 b")
    (raiz / "Outra" / "apresentacao.pptx").write_bytes(b"PK zip")
    (raiz / "Outra" / "desconhecido.xyz").write_bytes(b"?")
    (raiz / "Outra" / "~$temp.xlsx").write_bytes(b"lixo")
    (raiz / "Backup" / "antigo.pdf").write_bytes(b"%PDF-1.4 c")


def main() -> int:
    area = area_temporaria()
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir()
    montar_arvore(raiz_falsa)
    config = escrever_config(area, raiz_falsa)
    antes = foto_exata(raiz_falsa)

    print("\n--- unidades ---")
    checar(cu.normalizar_nome("Contrato Rev 02 (cópia).pdf") == cu.normalizar_nome("contrato.pdf"),
           "normalizar_nome agrupa variantes")
    checar(cu.esta_online(0x400000 | 0x1000) is True, "esta_online detecta placeholder")
    checar(cu.esta_online(0x20) is False, "esta_online nao marca arquivo local")
    checar(cu.lp(r"C:\temp\x").startswith("\\\\?\\"), "lp aplica prefixo long path")
    checar(cu.combina_padrao("~$temp.xlsx", ["~$*"]), "temporario reconhecido")
    checar(cu.backoff_segundos(1) < cu.backoff_segundos(3) <= 6 * 3600, "backoff cresce e tem teto")
    checar(cu.erro_transitorio(OSError(362, "cloud file provider not running")),
           "erro de nuvem e classificado como transitorio")
    checar(not cu.erro_transitorio(ValueError("pdf corrompido")),
           "erro de conteudo nao e transitorio")
    checar(len(list(cu.trechos_de("palavra " * 800, 1500, 150))) > 1, "texto longo vira trechos")

    # Regressao: sem `restype`, o ctypes devolvia -1 (com sinal) para arquivo
    # inexistente, o -1 nao batia com 0xFFFFFFFF e o arquivo era lido como se
    # existisse, com TODOS os atributos ligados (inclusive `online`).
    try:
        cu.atributos_de(raiz_falsa / "nao-existe-mesmo.pdf")
        checar(False, "atributos_de acusa arquivo inexistente")
    except OSError:
        checar(True, "atributos_de acusa arquivo inexistente")
    checar(cu.atributos_de(raiz_falsa / "RD103" / "3. Notificações" /
                           "notificacao 001.pdf") > 0,
           "atributos_de le arquivo que existe")

    try:
        cu.assert_escrita_permitida(raiz_falsa / "novo.txt", raiz_falsa)
        checar(False, "guarda read-only bloqueia escrita na raiz")
    except PermissionError:
        checar(True, "guarda read-only bloqueia escrita na raiz")

    print("\n--- init db ---")
    r = rodar("00_init_db.py", config)
    checar(r.returncode == 0, "00_init_db roda", r.stderr[-400:])
    con = cu.conectar(area / "teste.db")
    tabelas = {t[0] for t in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    checar({"arquivo", "execucao", "extracao", "evento_arquivo", "log", "pasta",
            "controle", "checkpoint", "progresso_diretorio"} <= tabelas,
           "tabelas do schema v2 criadas", str(sorted(tabelas)))
    checar(con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()[0] == "4",
           "schema marcado como v4 (v2 + nuvem + conteudo)")

    # Regressao (DEC-015): banco novo nascia com UNIQUE(caminho_rel), porque
    # o `03_migrar_v3.caminho_unico()` procurava a frase com um espaco e o
    # DDL alinha com varios — o script dizia "ja removido" e saia 0. Efeito:
    # arquivo apagado e subido de novo no mesmo lugar (item_id novo, que e
    # item NOVO) derrubava a insercao. O banco de producao nao tinha o
    # problema so porque foi reconstruido por outra sessao (DEC-024).
    ddl_arquivo = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='arquivo'").fetchone()[0]
    import re as _re
    checar(not _re.search(r"caminho_rel\s+TEXT\s+NOT NULL\s+UNIQUE", ddl_arquivo),
           "banco novo NAO nasce com UNIQUE(caminho_rel) — caminho e atributo")
    indices = {i[0] for i in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='arquivo'")}
    checar("ux_arquivo_item" in indices,
           "quem garante unicidade e (drive_id, item_id)", str(sorted(indices)))
    inserir = ("INSERT INTO arquivo (caminho_rel, pasta_rel, nome, drive_id, item_id,"
               " primeira_vez_em, ultima_vez_em) VALUES (?,?,?,?,?,?,?)")
    agora = cu.agora_iso()
    con.execute(inserir, ("X\\reencarnado.pdf", "X", "reencarnado.pdf", "d",
                          "item-velho", agora, agora))
    con.execute(inserir, ("X\\reencarnado.pdf", "X", "reencarnado.pdf", "d",
                          "item-novo", agora, agora))
    con.commit()
    checar(con.execute("SELECT COUNT(*) FROM arquivo WHERE caminho_rel='X\\reencarnado.pdf'"
                       ).fetchone()[0] == 2,
           "mesmo caminho com item_id diferente convive (caminho reencarnado)")
    try:
        con.execute(inserir, ("X\\outro.pdf", "X", "outro.pdf", "d", "item-novo",
                              agora, agora))
        con.commit()
        checar(False, "o MESMO (drive_id, item_id) e recusado")
    except Exception:
        con.rollback()
        checar(True, "o MESMO (drive_id, item_id) e recusado")
    con.execute("DELETE FROM arquivo")
    con.commit()

    print("\n--- inventario ---")
    r = rodar("01_scan_inventario.py", config, "--quieto")
    checar(r.returncode == 0, "scanner roda", r.stderr[-600:])
    n = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    checar(n == 8, f"inventariou 8 arquivos (achou {n})")
    estados = dict(con.execute("SELECT estado, COUNT(*) FROM arquivo GROUP BY estado"))
    checar(estados.get("INVENTORIED") == 7 and estados.get("SKIPPED") == 1,
           "estados iniciais: 7 INVENTORIED + 1 SKIPPED", str(estados))
    cat = dict(con.execute("SELECT categoria, COUNT(*) FROM arquivo GROUP BY categoria"))
    checar(cat.get("pdf") == 4 and cat.get("imagem") == 1 and cat.get("outro") == 1,
           "categorias por extensao", str(cat))
    checar(con.execute("SELECT COUNT(*) FROM arquivo WHERE suportado=1").fetchone()[0] == 5,
           "5 arquivos com extrator disponivel")

    print("\n--- idempotencia ---")
    rodar("01_scan_inventario.py", config, "--quieto")
    novos = con.execute("SELECT arquivos_novos FROM execucao WHERE etapa='scan' "
                        "ORDER BY id DESC LIMIT 1").fetchone()[0]
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == 8 and novos == 0,
           "segunda varredura nao duplica nem recria")

    print("\n--- classificacao P0..P4 ---")
    r = rodar("02_classificar.py", config)
    checar(r.returncode == 0, "02_classificar roda", r.stderr[-600:])
    prioridades = {linha["caminho_rel"]: (linha["prioridade"], linha["obra"],
                                          linha["categoria_doc"])
                   for linha in con.execute("SELECT * FROM pasta")}
    checar(prioridades.get("RD103\\3. Notificações", (9,))[0] == 0,
           "P0: obra + categoria critica (notificacoes)",
           str(prioridades.get("RD103\\3. Notificações")))
    checar(prioridades.get("RD103\\Fotos", (9,))[0] == 2,
           "P2: obra + categoria nao critica", str(prioridades.get("RD103\\Fotos")))
    checar(prioridades.get("Administrativo\\Contratos", (9,))[0] == 1,
           "P1: categoria critica fora de obra",
           str(prioridades.get("Administrativo\\Contratos")))
    checar(prioridades.get("Outra", (9,))[0] == 3, "P3: acervo geral",
           str(prioridades.get("Outra")))
    checar(prioridades.get("Backup", (9,))[0] == 4, "P4: pasta de descarte",
           str(prioridades.get("Backup")))
    checar(prioridades.get("RD103\\3. Notificações", (9, None))[1] == "RD103",
           "obra detectada pelo nome da pasta")
    checar(con.execute("SELECT prioridade FROM arquivo WHERE nome='notificacao 001.pdf'"
                       ).fetchone()[0] == 0, "prioridade propagada para o arquivo")

    estados = dict(con.execute("SELECT estado, COUNT(*) FROM arquivo GROUP BY estado"))
    checar(estados.get("QUEUED") == 5, "5 arquivos enfileirados", str(estados))
    checar(estados.get("UNSUPPORTED") == 2,
           "2 sem extrator continuam catalogados (nao somem)", str(estados))
    checar(con.execute("SELECT COUNT(*) FROM v_familia_versao").fetchone()[0] == 1,
           "familia de versao detectada (contrato x contrato rev 02)")
    checar((Path(area / "output" / "prioridades.md")).exists()
           and (Path(area / "output" / "pastas_classificadas.csv")).exists(),
           "relatorios de prioridade gerados")

    print("\n--- alteracao, novo e sumico ---")
    time.sleep(1.1)
    (raiz_falsa / "Administrativo" / "Contratos" / "contrato.pdf").write_bytes(b"%PDF-1.4 MAIOR")
    (raiz_falsa / "Outra" / "novo_documento.docx").write_bytes(b"PK zip")
    (raiz_falsa / "RD103" / "Fotos" / "foto.JPG").unlink()
    # O que o TESTE escreveu, medido antes de qualquer script rodar de novo.
    escrito_pelo_teste = foto_exata(raiz_falsa)
    rodar("01_scan_inventario.py", config, "--quieto")
    linha = con.execute("SELECT arquivos_novos, arquivos_alterados, arquivos_sumidos "
                        "FROM execucao WHERE etapa='scan' ORDER BY id DESC LIMIT 1").fetchone()
    checar(linha["arquivos_novos"] == 1, "detectou 1 arquivo novo", str(dict(linha)))
    checar(linha["arquivos_alterados"] == 1, "detectou 1 alteracao", str(dict(linha)))
    checar(linha["arquivos_sumidos"] == 1, "detectou 1 sumico", str(dict(linha)))
    checar(con.execute("SELECT estado FROM arquivo WHERE nome='contrato.pdf'").fetchone()[0]
           == "CHANGED", "arquivo alterado volta para a fila como CHANGED")
    checar(con.execute("SELECT estado, presente FROM arquivo WHERE nome='foto.JPG'"
                       ).fetchone()[0] == "MISSING",
           "arquivo sumido vira MISSING (linha preservada)")

    print("\n--- retomada apos interrupcao ---")
    con.close()
    for sufixo in ("", "-wal", "-shm"):
        p = Path(str(area / "teste.db") + sufixo)
        if p.exists():
            p.unlink()
    rodar("00_init_db.py", config)
    con = cu.conectar(area / "teste.db")
    rodar("01_scan_inventario.py", config, "--quieto", "--limite", "2")
    status = con.execute("SELECT status FROM execucao WHERE etapa='scan' "
                         "ORDER BY id DESC LIMIT 1").fetchone()[0]
    parcial = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    checar(status == "interrompida", "execucao limitada fica 'interrompida'", status)
    # Era `0 < parcial < 8` com --limite 2: aceitava o limite ignorado com
    # erro de 350% (M-9). O limite e exato.
    checar(parcial == 2, f"inventario parcial respeita --limite 2 ({parcial} arquivos)")
    rodar("01_scan_inventario.py", config, "--quieto")
    total = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    execs = con.execute("SELECT COUNT(*) FROM execucao WHERE etapa='scan'").fetchone()[0]
    checar(total == 8, f"retomada completa o inventario (total {total})")
    checar(execs == 1, f"retomada reaproveita a execucao (execucoes={execs})")

    print("\n--- status ---")
    r = rodar("99_status.py", config)
    checar(r.returncode == 0 and "POR PRIORIDADE" in r.stdout, "99_status roda",
           r.stderr[-400:])

    print("\n--- o acervo nao foi tocado ---")
    # Antes os valores esperados de contrato.pdf e novo_documento.docx eram
    # copiados do proprio `depois` (M-10): para essas chaves a prova era
    # depois[k] == depois[k]. Agora vem do que o teste mediu logo apos
    # escrever; o resto vem da foto inicial. mtime em ns + sha256.
    depois = foto_exata(raiz_falsa)
    esperado = dict(antes)
    esperado.pop("RD103\\Fotos\\foto.JPG", None)
    for chave in ("Administrativo\\Contratos\\contrato.pdf", "Outra\\novo_documento.docx"):
        esperado[chave] = escrito_pelo_teste[chave]
    divergentes = sorted(k for k in set(depois) | set(esperado)
                         if depois.get(k) != esperado.get(k))
    checar(depois == esperado,
           "scanner nao criou, apagou nem alterou nada na arvore", str(divergentes))

    con.close()
    shutil.rmtree(area, ignore_errors=True)
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
