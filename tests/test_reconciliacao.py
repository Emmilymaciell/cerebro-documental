"""
test_reconciliacao — E5: a migracao do inventario antigo para a identidade
da nuvem, num banco sintetico onde cada caso e conhecido.

    python tests\\test_reconciliacao.py

Os casos montados de proposito:

  1. igual nos dois lados                -> ALTA, estado preservado
  2. COMPLETED com derivado, sem mudanca -> ALTA, continua COMPLETED
  3. COMPLETED com conteudo alterado     -> ALTA, vira CHANGED (refaz)
  4. movido/renomeado na nuvem           -> MEDIA por nome+tamanho
  5. dois candidatos iguais              -> AMBIGUO, NAO aplicado
  6. so no disco (sumiu da nuvem)        -> SEM_CORRESPONDENCIA
  7. biblioteca fora do escopo           -> SEM_CORRESPONDENCIA (sem_mapa)
  8. so na nuvem                         -> linha nova, INVENTORIED
  9. duplicata por hash na nuvem         -> aparece em v_duplicata_nuvem
 10. rodar de novo                       -> idempotente
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import (RAIZ, area_temporaria, checar, escrever_config,  # noqa: E402
                   placar_final, rodar)

import cerebro_util as cu  # noqa: E402

DRIVE = "drive!T"
AGORA = "2026-09-20T00:00:00+00:00"


def local(con, ident, caminho, tamanho, estado="QUEUED", prioridade=0, suportado=1,
          categoria="pdf", temporario=0):
    nome = caminho.rsplit("\\", 1)[-1]
    con.execute(
        "INSERT INTO arquivo (id, caminho_rel, biblioteca, pasta_rel, nome, ext, categoria, "
        "nome_normalizado, profundidade, tamanho_bytes, mtime_utc, online, temporario, "
        "suportado, prioridade, estado, presente, primeira_vez_em, ultima_vez_em) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,1,?,?)",
        (ident, caminho, caminho.split("\\", 1)[0], caminho.rsplit("\\", 1)[0], nome,
         ".pdf", categoria, cu.normalizar_nome(nome), caminho.count("\\") + 1, tamanho,
         AGORA, temporario, suportado, prioridade, estado, AGORA, AGORA))


def nuvem(con, item_id, caminho, tamanho, quick="HASH", pai="pasta"):
    con.execute(
        "INSERT INTO item_nuvem (drive_id, item_id, parent_id, nome, caminho_rel, "
        "eh_pasta, ext, tamanho_bytes, quick_xor, etag, ctag, web_url, criado_em, "
        "modificado_em, criado_por, modificado_por, presente, primeira_vez_em, "
        "ultima_vez_em) VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (DRIVE, item_id, pai, caminho.rsplit("\\", 1)[-1], caminho, ".pdf", tamanho,
         quick, "e1", "c1", f"https://x/{item_id}", AGORA, AGORA, "Fulano", "Ciclana",
         AGORA, AGORA))


def montar(area: Path, config: Path):
    con = cu.conectar(area / "teste.db")
    con.execute("INSERT INTO drive (drive_id, nome, no_escopo, atualizado_em) "
                "VALUES (?,?,1,?)", (DRIVE, "Programa", AGORA))
    con.execute("INSERT INTO delta_token (drive_id, status, delta_link, atualizado_em) "
                "VALUES (?,'concluido','dl:1',?)", (DRIVE, AGORA))

    B = "Programa - 3. Desenvolvimento"
    local(con, 1, f"{B}\\RD103\\igual.pdf", 100)
    local(con, 2, f"{B}\\RD103\\completo.pdf", 200, estado="COMPLETED")
    local(con, 3, f"{B}\\RD103\\mudou.pdf", 300, estado="COMPLETED")
    local(con, 4, f"{B}\\RD103\\movido.pdf", 400)
    local(con, 5, f"{B}\\RD103\\gemeo-a.pdf", 500)
    local(con, 6, f"{B}\\RD103\\gemeo-b.pdf", 500)
    local(con, 7, f"{B}\\RD103\\sumiu.pdf", 600)
    local(con, 8, "Coordenacao Programa - 02 Repositorio\\fora.pdf", 700)
    # 9: estava MISSING (marcado por 15_marcar_ausentes) e volta a existir
    local(con, 9, f"{B}\\RD103\\voltou.pdf", 900, estado="COMPLETED")
    con.execute("UPDATE arquivo SET estado='MISSING', presente=0 WHERE id=9")
    con.execute("INSERT INTO evento_arquivo (arquivo_id, evento, detalhe, em) "
                "VALUES (9,'ausente_na_nuvem',?,?)",
                ('{"estado_anterior": "COMPLETED"}', AGORA))

    nuvem(con, "i1", "3. Desenvolvimento\\RD103\\igual.pdf", 100, "H1")
    nuvem(con, "i2", "3. Desenvolvimento\\RD103\\completo.pdf", 200, "H2")
    nuvem(con, "i3", "3. Desenvolvimento\\RD103\\mudou.pdf", 999, "H3-NOVO")
    nuvem(con, "i4", "3. Desenvolvimento\\RD103\\ARQUIVO\\movido.pdf", 400, "H4")
    # dois iguais em nome+tamanho, em caminhos que nao batem com os locais:
    nuvem(con, "i5a", "3. Desenvolvimento\\RD103\\X\\gemeo-a.pdf", 500, "H5")
    nuvem(con, "i5b", "3. Desenvolvimento\\RD103\\Y\\gemeo-a.pdf", 500, "H5")
    nuvem(con, "i9", "3. Desenvolvimento\\RD103\\so-na-nuvem.pdf", 800, "H9")
    # duplicata exata por hash do servidor
    nuvem(con, "i10", "3. Desenvolvimento\\RD103\\copia-de-igual.pdf", 100, "H1")
    # o item que o MISSING (id=9) procura: existe de novo na nuvem
    nuvem(con, "i11", "3. Desenvolvimento\\RD103\\voltou.pdf", 900, "H11")

    # derivado de verdade para o COMPLETED, com extracao e trecho no indice
    derivado = area / "derivados" / "texto" / "0000"
    derivado.mkdir(parents=True, exist_ok=True)
    caminho_txt = derivado / "2.txt.gz"
    cu.gravar_texto(caminho_txt, "conteudo extraido do completo")
    con.execute(
        "INSERT INTO extracao (arquivo_id, extrator, versao, status, paginas, caracteres, "
        "texto_path, bytes_texto, trechos, executado_em) "
        "VALUES (2,'pdf','pdf/1.0','ok',3,29,?,100,1,?)", (str(caminho_txt), AGORA))
    con.execute("INSERT INTO texto_fts (arquivo_id, caminho_rel, pagina, trecho_idx, texto) "
                "VALUES (2,?,1,0,'conteudo extraido do completo')",
                (f"{B}\\RD103\\completo.pdf",))
    con.execute("INSERT INTO pasta (caminho_rel, nome, nivel, prioridade, arquivos, "
                "classificado_em) VALUES (?,?,?,0,8,?)",
                (f"{B}\\RD103", "RD103", 3, AGORA))
    con.commit()

    mapa = area / "mapa_teste.yaml"
    mapa.write_text(yaml.safe_dump({
        "mapa_biblioteca": {B: {"drive_id": DRIVE, "pasta_nuvem": "3. Desenvolvimento",
                                "como_casou": "teste", "arquivos_no_banco": 7}}},
        allow_unicode=True), encoding="utf-8")
    dados = yaml.safe_load(config.read_text(encoding="utf-8"))
    dados["mapa_drives"] = str(mapa)
    config.write_text(yaml.safe_dump(dados, allow_unicode=True), encoding="utf-8")
    con.close()


def confianca(con, arquivo_id) -> tuple:
    linha = con.execute(
        "SELECT confianca, metodo, item_id, aplicado FROM reconciliacao "
        "WHERE arquivo_id=? ORDER BY id DESC LIMIT 1", (arquivo_id,)).fetchone()
    return (linha["confianca"], linha["metodo"], linha["item_id"], linha["aplicado"]) \
        if linha else (None, None, None, None)


def foto_arquivo(caminho: Path) -> tuple | None:
    """(tamanho, mtime_ns, sha256) — None se nao existe."""
    if not caminho.exists():
        return None
    st = caminho.stat()
    return (st.st_size, st.st_mtime_ns, hashlib.sha256(caminho.read_bytes()).hexdigest())


def foto_catalogo(con) -> list:
    """O que a reconciliacao decide, linha a linha — para provar idempotencia.

    Inclui `origem`: a segunda passada trocava 'graph' por 'ambos' nas linhas
    que a primeira criou (R1, `tests/reproducoes_qa.py`), corrigido em
    `13_reconciliar.vincular()`.
    """
    return [tuple(l) for l in con.execute(
        "SELECT id, caminho_rel, drive_id, item_id, estado, presente, presente_nuvem, "
        "quick_xor, tamanho_bytes, caminho_nuvem, origem FROM arquivo ORDER BY id")]


def main() -> int:
    area = area_temporaria("cerebro_reconc_")
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir(parents=True)
    config = escrever_config(area, raiz_falsa)
    # A pasta output/ do REPOSITORIO tem um reconciliacao.md versionado. O
    # teste nao pode tocar nele: o relatorio vai para a saida da config.
    relatorio_repo = RAIZ / "output" / "reconciliacao.md"
    relatorio_repo_antes = foto_arquivo(relatorio_repo)
    for script, extra in (("00_init_db.py", ()), ("03_migrar_v3.py", ("--sem-backup",))):
        r = rodar(script, config, *extra)
        if r.returncode != 0:
            checar(False, f"fixture: {script} roda", r.stderr[-600:])
            return placar_final()
    montar(area, config)
    con = cu.conectar(area / "teste.db")

    print("\n--- simulacao nao altera nada ---")
    antes = dict(con.execute("SELECT estado, COUNT(*) FROM arquivo GROUP BY estado"))
    r = rodar("13_reconciliar.py", config)
    checar(r.returncode == 0, "13_reconciliar simula", r.stderr[-500:])
    depois = dict(con.execute("SELECT estado, COUNT(*) FROM arquivo GROUP BY estado"))
    checar(antes == depois, "simulacao preserva todos os estados", f"{antes} != {depois}")
    checar(con.execute("SELECT COUNT(*) FROM arquivo WHERE item_id IS NOT NULL"
                       ).fetchone()[0] == 0, "simulacao nao grava item_id")
    checar(con.execute("SELECT COUNT(*) FROM reconciliacao").fetchone()[0] == 9,
           "mesmo simulando, registra as 9 decisoes")

    print("\n--- aplicando ---")
    r = rodar("13_reconciliar.py", config, "--aplicar")
    checar(r.returncode == 0, "13_reconciliar aplica", r.stderr[-600:])

    print("\n--- cada caso ---")
    checar(confianca(con, 1)[:2] == ("ALTA", "caminho_exato"), "1. igual -> ALTA",
           str(confianca(con, 1)))
    checar(con.execute("SELECT item_id FROM arquivo WHERE id=1").fetchone()[0] == "i1",
           "1. item_id gravado na linha antiga")
    checar(con.execute("SELECT estado FROM arquivo WHERE id=1").fetchone()[0] == "QUEUED",
           "1. estado preservado")
    checar(con.execute("SELECT quick_xor FROM arquivo WHERE id=1").fetchone()[0] == "H1",
           "1. hash do servidor copiado para o inventario")

    checar(con.execute("SELECT estado FROM arquivo WHERE id=2").fetchone()[0] == "COMPLETED",
           "2. COMPLETED sem mudanca continua COMPLETED")
    extracao = con.execute("SELECT texto_path FROM extracao WHERE arquivo_id=2").fetchone()
    checar(extracao and Path(extracao[0]).exists(), "2. derivado continua no disco")
    checar(cu.ler_texto(extracao[0]).startswith("conteudo extraido"),
           "2. o texto derivado continua legivel")
    checar(con.execute("SELECT COUNT(*) FROM texto_fts WHERE arquivo_id=2").fetchone()[0] == 1,
           "2. trecho continua no indice")

    checar(con.execute("SELECT estado FROM arquivo WHERE id=3").fetchone()[0] == "CHANGED",
           "3. COMPLETED com conteudo alterado volta para a fila")
    checar(con.execute("SELECT tamanho_bytes FROM arquivo WHERE id=3").fetchone()[0] == 999,
           "3. tamanho atualizado pelo da nuvem")
    checar(confianca(con, 3)[0] == "ALTA", "3. o vinculo continua sendo de ALTA confianca")

    checar(confianca(con, 4)[:2] == ("MEDIA", "nome_tamanho_unico"),
           "4. movido -> MEDIA por nome+tamanho", str(confianca(con, 4)))
    checar(con.execute("SELECT caminho_nuvem FROM arquivo WHERE id=4").fetchone()[0]
           == "3. Desenvolvimento\\RD103\\ARQUIVO\\movido.pdf",
           "4. caminho da nuvem gravado (o local continua como estava)")
    checar(con.execute("SELECT caminho_rel FROM arquivo WHERE id=4").fetchone()[0]
           == "Programa - 3. Desenvolvimento\\RD103\\movido.pdf",
           "4. caminho local preservado — e atributo, nao identidade")

    checar(confianca(con, 5)[0] == "AMBIGUO", "5. dois candidatos -> AMBIGUO",
           str(confianca(con, 5)))
    checar(confianca(con, 5)[3] == 0, "5. ambiguo NAO e aplicado")
    checar(con.execute("SELECT item_id FROM arquivo WHERE id=5").fetchone()[0] is None,
           "5. nenhum item_id foi chutado")

    checar(confianca(con, 7)[0] == "SEM_CORRESPONDENCIA", "6. sumiu da nuvem",
           str(confianca(con, 7)))
    checar(con.execute("SELECT presente_nuvem FROM arquivo WHERE id=7").fetchone()[0] == 0,
           "6. marcado como ausente na nuvem (linha preservada)")
    checar(con.execute("SELECT estado FROM arquivo WHERE id=7").fetchone()[0] == "QUEUED",
           "6. o estado NAO foi destruido")

    checar(confianca(con, 8)[:2] == ("SEM_CORRESPONDENCIA", "sem_mapa"),
           "7. biblioteca fora do escopo", str(confianca(con, 8)))

    novo = con.execute("SELECT * FROM arquivo WHERE item_id='i9'").fetchone()
    checar(novo is not None, "8. item so na nuvem virou linha nova")
    checar(novo and novo["estado"] == "INVENTORIED", "8. entra como INVENTORIED")
    checar(novo and novo["origem"] == "graph", "8. origem registrada")
    checar(novo and novo["caminho_rel"].startswith("Programa - 3. Desenvolvimento"),
           "8. caminho traduzido para a convencao do banco",
           novo["caminho_rel"] if novo else "")

    # Antes este caso lia `v_duplicata_nuvem`, que e um GROUP BY sobre
    # `item_nuvem` — tabela que o proprio fixture preenche e que o
    # 13_reconciliar nunca escreve. Passava com o script vazio (M-15).
    # O que a reconciliacao promete e que o CATALOGO (`arquivo`) herda o
    # hash do servidor, nas linhas antigas vinculadas e nas novas.
    dup = dict(con.execute(
        "SELECT quick_xor, COUNT(*) FROM arquivo WHERE quick_xor IS NOT NULL "
        "AND presente=1 GROUP BY quick_xor HAVING COUNT(*) > 1"))
    checar(dup == {"H1": 2, "H5": 2},
           "9. duplicatas por hash do servidor visiveis no catalogo, sem baixar nada",
           str(dup))
    checar(con.execute("SELECT quick_xor FROM arquivo WHERE item_id='i10'").fetchone()[0]
           == "H1", "9. linha nova da nuvem entra com o quick_xor do servidor")

    print("\n--- reaparecimento: MISSING volta sem perder historico ---")
    linha9 = con.execute("SELECT * FROM arquivo WHERE id=9").fetchone()
    checar(linha9["presente"] == 1, "10. item que reapareceu volta a presente=1")
    checar(linha9["estado"] == "COMPLETED",
           "10. o estado ANTERIOR foi restaurado (nao virou QUEUED)", linha9["estado"])
    checar(linha9["item_id"] == "i11", "10. ganhou a identidade da nuvem")
    checar(con.execute("SELECT COUNT(*) FROM evento_arquivo WHERE arquivo_id=9 "
                       "AND evento='reapareceu_na_nuvem'").fetchone()[0] == 1,
           "10. reaparecimento registrado no historico")
    checar(con.execute("SELECT COUNT(*) FROM evento_arquivo WHERE arquivo_id=9 "
                       "AND evento='ausente_na_nuvem'").fetchone()[0] == 1,
           "10. o evento de ausencia continua no historico")

    print("\n--- nada se perdeu ---")
    checar(con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0] == 1,
           "extracoes preservadas")
    checar(con.execute("SELECT COUNT(*) FROM texto_fts").fetchone()[0] == 1,
           "indice preservado")
    checar(con.execute("SELECT COUNT(*) FROM pasta").fetchone()[0] == 1,
           "classificacao de pastas preservada")
    checar(con.execute("SELECT prioridade FROM arquivo WHERE id=2").fetchone()[0] == 0,
           "prioridade P0 preservada")
    # Os itens da nuvem que ficaram sem vinculo (i9, i10 e os dois gemeos
    # ambiguos) entram como linhas novas: nada da nuvem pode ficar fora do
    # catalogo. As linhas antigas correspondentes estao na fila de excecoes.
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == 13,
           "9 antigas + 4 itens da nuvem sem vinculo = 13 linhas",
           str(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]))
    checar(con.execute("SELECT COUNT(*) FROM arquivo WHERE origem='graph'").fetchone()[0] == 4,
           "as 4 linhas novas vieram da nuvem")
    # Era `checar(X or True, ...)` — constante True (M-1). Agora afirma o que
    # o rotulo dizia: o relatorio NAO foi para a pasta output/ do repositorio.
    checar(foto_arquivo(relatorio_repo) == relatorio_repo_antes,
           "relatorio NAO foi escrito na pasta output/ do repositorio",
           f"{relatorio_repo_antes} -> {foto_arquivo(relatorio_repo)}")
    checar((area / "output" / "reconciliacao.md").exists(),
           "relatorio .md gerado na pasta de saida do teste")
    checar((area / "output" / "reconciliacao_excecoes.csv").exists(),
           "planilha de excecoes gerada")

    print("\n--- idempotencia ---")
    antes_total = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    antes_ids = con.execute("SELECT COUNT(*) FROM arquivo WHERE item_id IS NOT NULL"
                            ).fetchone()[0]
    antes_catalogo = foto_catalogo(con)
    r = rodar("13_reconciliar.py", config, "--aplicar")
    checar(r.returncode == 0, "roda de novo sem erro", r.stderr[-400:])
    checar(con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0] == antes_total,
           "nao duplica linhas")
    # Era `>=` (M-8): idempotencia e igualdade, nao "pelo menos".
    checar(con.execute("SELECT COUNT(*) FROM arquivo WHERE item_id IS NOT NULL"
                       ).fetchone()[0] == antes_ids, "os vinculos continuam (nem a mais)")
    depois_catalogo = foto_catalogo(con)
    mudaram = [(a, d) for a, d in zip(antes_catalogo, depois_catalogo) if a != d]
    checar(depois_catalogo == antes_catalogo,
           "segunda passada nao muda nenhuma linha (vinculo, estado, hash, caminho)",
           str(mudaram[:3]))
    checar(con.execute("SELECT estado FROM arquivo WHERE id=2").fetchone()[0] == "COMPLETED",
           "COMPLETED continua COMPLETED na segunda passada")
    checar(foto_arquivo(relatorio_repo) == relatorio_repo_antes,
           "nem a segunda passada escreve na pasta output/ do repositorio")

    con.close()
    shutil.rmtree(area, ignore_errors=True)
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
