"""
test_busca — a camada de busca (schema v5): `scripts/busca.py` e
`scripts/05_migrar_v5.py`, em bancos temporarios.

    python tests\\test_busca.py

NAO TOCA em `data/cerebro.db`. Os bancos sao criados pela receita do
repositorio (`00_init_db.py` com config temporaria = v4 de verdade).

O que tem de provar:

  1. o DDL v5 aplica sobre um v4, dentro de uma transacao, e e idempotente;
  2. normalizacao `norm/1.1`: NFKC, invisiveis, CRLF, hifenizacao, espacos,
     acento solto de PDF (LaTeX), consulta com a mesma dobra;
  3. cabecalho/rodape corrido sai das PAGINAS — e nao das abas;
  4. propriedades do corte `chunk/2.0`: deterministico (bytes identicos),
     cobertura total, nenhum trecho vazio, nenhum maior que o maximo,
     inicio em palavra, acento nunca separado da letra, unicode PT-BR,
     texto vazio;
  5..13. pipeline: 1 conteudo = 1 indexacao, N ocorrencias no resultado,
     pagina/slide/aba de origem, trecho nunca cruza segmento, reindexacao
     substitui sem acumular, texto igual nao reindexa, MISSING, acento e
     caixa, saude zerada, FK protege a ordem;
 14. manutencao: conteudo que muda de bytes, remocao quando nao ha
     ocorrencia presente (e volta), rastreabilidade trecho -> ocorrencias;
 15. a entrada do worker (`indexar_resultado`) e a trava de versoes;
 16. `05_migrar_v5.py` ponta a ponta: simulacao nao escreve, falha no meio
     faz ROLLBACK ate do DDL, aplicacao com backup e conferencias, segunda
     rodada nao muda nada, worker rodando bloqueia a migracao;
 17. o banco real nao foi tocado.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import (RAIZ, area_temporaria, checar, escrever_config,  # noqa: E402
                   placar_final, rodar)

import busca  # noqa: E402  (comum poe scripts/ no sys.path)
import cerebro_util as cu  # noqa: E402
import operacao as op  # noqa: E402

for _saida in (sys.stdout, sys.stderr):
    try:
        _saida.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # pragma: no cover
        pass

DRIVE = "drive!Programa"
T = busca.TRECHO_CHARS
S = busca.TRECHO_SOBREPOSICAO


# ======================================================================
# Montagem
# ======================================================================

def montar_banco(area: Path) -> tuple[sqlite3.Connection, Path]:
    raiz_falsa = area / "arvore"
    (raiz_falsa / "vazio").mkdir(parents=True, exist_ok=True)
    config = escrever_config(area, raiz_falsa)
    r = rodar("00_init_db.py", config)
    if r.returncode != 0:
        raise RuntimeError(f"00_init_db falhou:\n{r.stdout}\n{r.stderr}")
    con = sqlite3.connect(area / "teste.db", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con, config


def aplicar_v5(con) -> None:
    con.execute("BEGIN")
    busca.aplicar_ddl(con)
    busca.gravar_versoes(con)
    con.execute("COMMIT")


def tx(con, func, *args, **kw):
    con.execute("BEGIN")
    try:
        r = func(*args, **kw)
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
    return r


def criar_conteudo(con, quick: str, tamanho: int, *, extrator="pdf",
                   versao="pdf/1.1", estado="EXTRAIDO", texto_path=None) -> int:
    cur = con.execute(
        "INSERT INTO conteudo (quick_xor, tamanho_bytes, evidencia, estado,"
        " extrator, versao_extrator, texto_path, ocorrencias, primeira_vez_em,"
        " atualizado_em) VALUES (?,?,'FORTE',?,?,?,?,0,'2026-10-04','2026-10-04')",
        (quick, tamanho, estado, extrator, versao, texto_path))
    return int(cur.lastrowid)


def criar_ocorrencia(con, conteudo_id, nome: str, item_id: str, *, prioridade=0,
                     presente=1, estado="COMPLETED", ultima_vez="2026-10-04T00:00:00+00:00",
                     biblioteca="Programa - 3. Desenvolvimento") -> int:
    caminho = f"{biblioteca}\\{nome}"
    cur = con.execute(
        "INSERT INTO arquivo (caminho_rel, biblioteca, pasta_rel, nome, ext,"
        " categoria, nome_normalizado, tamanho_bytes, prioridade, suportado,"
        " estado, presente, presente_nuvem, temporario, drive_id, item_id,"
        " conteudo_id, caminho_nuvem, web_url, quick_xor, primeira_vez_em, ultima_vez_em)"
        " VALUES (?,?,?,?,?,?,?,?,?,1,?,?,1,0,?,?,?,?,?,?,'2026-10-01',?)",
        (caminho, biblioteca, biblioteca, nome, Path(nome).suffix, "pdf",
         nome.lower(), 1000, prioridade, estado, presente, DRIVE, item_id,
         conteudo_id, f"3. Desenvolvimento/{nome}", f"https://sp/{item_id}",
         f"QX-{item_id}", ultima_vez))
    arquivo_id = int(cur.lastrowid)
    if conteudo_id:
        con.execute("UPDATE conteudo SET ocorrencias=(SELECT COUNT(*) FROM arquivo"
                    " WHERE conteudo_id=? AND presente=1),"
                    " representante_id=COALESCE(representante_id, ?) WHERE id=?",
                    (conteudo_id, arquivo_id, conteudo_id))
    return arquivo_id


def zeros(s: dict) -> dict:
    return {k: v for k, v in s.items() if k != "a_reindexar"}


def buscar_itens(con, termo: str) -> list:
    return busca.buscar(con, busca.consulta_de_termos(termo))


# ======================================================================
# 1. DDL
# ======================================================================

def teste_ddl(con):
    print("\n1. o DDL v5 aplica sobre um banco v4, em transacao, e e idempotente")
    con.execute("BEGIN")
    busca.aplicar_ddl(con)
    con.execute("ROLLBACK")
    checar(not busca.v5_ativo(con),
           "DDL dentro de BEGIN + ROLLBACK nao deixa objeto nenhum (DDL e transacional)")
    aplicar_v5(con)
    nomes = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    for objeto in ("segmento", "trecho", "busca_fts", "indice_conteudo",
                   "trg_trecho_ai", "trg_trecho_ad", "trg_trecho_au",
                   "v_trecho_ocorrencia", "v_reindexar", "v_busca_saude",
                   "v_busca_cobertura", "v_conteudo_sem_ocorrencia",
                   "ix_arquivo_conteudo_vivo"):
        checar(objeto in nomes, f"objeto criado: {objeto}")
    checar(busca.conferir_versoes(con) == [],
           "meta carrega as versoes que o codigo produz (normalizador, chunker, tokenizer)")
    try:
        aplicar_v5(con)
        checar(True, "aplicar o DDL uma segunda vez nao levanta erro")
    except Exception as exc:                                   # pragma: no cover
        checar(False, "aplicar o DDL uma segunda vez nao levanta erro", str(exc))
    checar(con.execute("SELECT COUNT(*) FROM meta WHERE chave LIKE 'busca.%'")
           .fetchone()[0] == 4, "a segunda aplicacao nao duplicou linhas em meta")
    checar("fonte_texto" in {r[1] for r in con.execute("PRAGMA table_info(indice_conteudo)")},
           "indice_conteudo registra a fonte do texto (REGRA 9)")


# ======================================================================
# 2. Normalizacao
# ======================================================================

def teste_normalizacao():
    print("\n2. normalizacao deterministica (norm/1.1)")
    n = busca.normalizar
    checar(n("eﬁciência") == "eficiência", "NFKC desfaz ligadura (eﬁ -> efi), acento fica")
    checar(n("area de 30m²") == "area de 30m2", "NFKC baixa o sobrescrito: m² -> m2")
    checar(n("a​b­c") == "abc", "invisiveis (zero-width, soft hyphen) somem")
    checar(n("a b") == "a b", "nbsp vira espaco comum")
    checar(n("a\r\nb\rc") == "a\nb\nc", "CRLF e CR viram LF")
    checar(n("desapropria-\ncao") == "desapropriacao", "hifenizacao de fim de linha junta")
    checar(n("RD-101\nRelatorio") == "RD-101\nRelatorio", "RD-101 + maiuscula NAO junta")
    checar(n("obra -\n12") == "obra -\n12", "hifen solto antes de numero NAO junta")
    checar(n("a   b\tc") == "a b c", "espacos repetidos colapsam")
    checar(n("a   \nb") == "a\nb", "espaco antes da quebra some")
    checar(n("a\n\n\n\n\nb") == "a\n\nb", "3+ quebras viram 2")
    checar(n("MEDIÇÃO Final") == "MEDIÇÃO Final", "caixa e acento NAO sao tocados")
    checar(n("Certificado de Calibra¸c˜ao") == "Certificado de Calibração",
           "acento solto do PDF LaTeX: 'Calibra¸c˜ao' -> 'Calibração' (achado na amostra)")
    checar(n("tamb´em tendˆencia n˜ao") == "também tendência não",
           "acento solto: agudo, circunflexo e til antes da letra")
    checar(n("`a") == "`a" and "ý" not in n("x ´ y") and n("x ´ y").endswith(" y"),
           "crase de codigo e acento sem letra colada NAO viram letra acentuada")
    checar(unicodedata.is_normalized("NFC", n("ação")),
           "acento combinante decomposto sai composto (NFC dentro do NFKC)")
    corpus = ["x", "a\r\n b ", "eﬁciência  m²  ", "medi¸c˜ao -\nfinal", ""]
    checar(all(n(t) == n(n(t)) for t in corpus), "normalizar e idempotente")
    checar(busca.normalizar_consulta("30m²") == "30m2",
           "a CONSULTA passa pela mesma dobra (m² digitado acha m2 guardado)")
    checar(busca.consulta_de_termos('RD-101 "faixa"') == '"RD-101" """faixa"""',
           "texto livre vira frases FTS5 seguras (hifen e aspas nao quebram a sintaxe)")


def teste_cabecalho_rodape():
    print("\n3. cabecalho e rodape corridos — so em PAGINA")
    paginas = [(n, f"DYNATEST ENGENHARIA\nRD-101 Rev 02\n\ncorpo da pagina {n} "
                   f"falando de medicao\n\n{n}") for n in range(1, 7)]
    paginas[2] = (3, "DYNATEST ENGENHARIA\nRD-101 Rev 02\n\naviso que so aparece aqui\n"
                     "corpo da pagina 3 falando de medicao\n\n3")
    segmentos, removidas = busca.preparar(paginas, "pdf")
    checar("DYNATEST ENGENHARIA" in removidas, "cabecalho corrido foi removido")
    checar("RD-101 Rev 02" in removidas, "segunda linha do cabecalho tambem")
    checar("<numero de pagina>" in removidas, "rodape que e so numero de pagina sai")
    todos = "\n".join(s[4] for s in segmentos)
    checar("DYNATEST" not in todos, "o cabecalho nao sobrou em nenhuma pagina")
    checar("aviso que so aparece aqui" in todos, "linha que aparece em 1 de 6 paginas FICA")
    _, removidas4 = busca.preparar(paginas[:4], "pdf")
    checar(removidas4 == [], "com menos de 5 segmentos a regra nao age")
    abas = [(n, f"[aba] Aba {n}\nEstaca | Cota\n100 | 512\n{n * 7}") for n in range(1, 7)]
    seg_abas, rem_abas = busca.preparar(abas, "xlsx")
    checar(rem_abas == [] and all(s[4].endswith(str(s[2] * 7)) for s in seg_abas),
           "em ABA de planilha a ultima linha numerica e DADO: nada e removido")


# ======================================================================
# 4. Propriedades do corte
# ======================================================================

def corpus_de_corte() -> dict[str, str]:
    frase = ("A medição do pavimento flexível no trecho da RD-101 foi aprovada; "
             "o ensaio de compactação ficou acima de 100% do Proctor. ")
    paragrafos = "\n\n".join(f"Parágrafo {i}: " + frase * (1 + i % 5) for i in range(40))
    linhas = "\n".join(f"{i} | + | 0 | LE | 0.64 | Limpeza, desmatamento e destocamento"
                       for i in range(400))
    sem_espaco = "x" * (3 * T + 17)
    decomposto = unicodedata.normalize("NFD", "ação" * (T // 2))   # letra + acento soltos
    astral = ("obra 🚧 concluída 𝔸 ✔ " * 300)
    return {
        "frases e paragrafos (PT-BR)": busca.normalizar(paragrafos),
        "linhas de planilha": busca.normalizar(linhas),
        "token gigante sem espaco": sem_espaco,
        "acentos decompostos sem espaco": decomposto,
        "emoji e plano astral": busca.normalizar(astral),
        "exatamente o maximo": "a" * T,
        "maximo + 1": "a " * (T // 2) + "b",
        "curto": "uma linha só",
    }


def checar_propriedades(nome: str, texto: str, politica: busca.Politica) -> None:
    pedacos = politica.cortar(texto)
    de_novo = politica.cortar(texto)
    bytes_1 = b"\x1e".join(p.texto.encode("utf-8") for p in pedacos)
    bytes_2 = b"\x1e".join(p.texto.encode("utf-8") for p in de_novo)
    rotulo = f"[{politica.versao}] {nome}"
    checar(pedacos == de_novo and bytes_1 == bytes_2,
           f"{rotulo}: mesma entrada -> mesmos trechos, bytes identicos "
           f"(sha {hashlib.sha256(bytes_1).hexdigest()[:10]})")
    checar(busca.reconstruir(pedacos) == texto,
           f"{rotulo}: juntar sem a sobreposicao reconstroi o texto ({len(pedacos)} trechos)")
    checar(all(p.texto.strip() for p in pedacos), f"{rotulo}: nenhum trecho vazio")
    checar(all(len(p.texto) <= politica.tamanho for p in pedacos),
           f"{rotulo}: nenhum trecho passa de {politica.tamanho} caracteres")
    checar(all(texto[p.inicio:p.fim] == p.texto for p in pedacos)
           and [p.ordinal for p in pedacos] == list(range(len(pedacos)))
           and pedacos[0].inicio == 0 and pedacos[-1].fim == len(texto),
           f"{rotulo}: posicoes exatas, ordinais 0..N-1, do inicio ao fim")
    checar(all(0 <= a.fim - b.inicio <= politica.sobreposicao
               for a, b in zip(pedacos, pedacos[1:])),
           f"{rotulo}: sobreposicao entre 0 e {politica.sobreposicao}, sem buraco")
    checar(all(not unicodedata.combining(texto[p.inicio]) for p in pedacos),
           f"{rotulo}: nenhum trecho comeca com acento separado da letra")


def teste_corte():
    print("\n4. corte em trechos: propriedades")
    corpus = corpus_de_corte()
    for nome, texto in corpus.items():
        checar_propriedades(nome, texto, busca.POLITICA)
    for nome in ("frases e paragrafos (PT-BR)", "linhas de planilha"):
        checar_propriedades(nome, corpus[nome], busca.Politica(800, 100, "janela"))
        checar_propriedades(nome, corpus[nome], busca.Politica(1500, 0, "paragrafo"))

    texto = corpus["frases e paragrafos (PT-BR)"]
    pedacos = busca.POLITICA.cortar(texto)
    checar(all(p.inicio == 0 or (texto[p.inicio - 1].isspace() and not texto[p.inicio].isspace())
               for p in pedacos),
           "todo trecho comeca em INICIO de palavra (o chunk/1.0 comecava no meio)")
    legado = busca.Politica(T, S, "legado").cortar(texto)
    no_meio = sum(1 for p in legado if p.inicio and not texto[p.inicio - 1].isspace())
    checar(no_meio > 0, f"linha de base: o chunk/1.0 comeca {no_meio} trecho(s) no meio "
                        "da palavra — e o defeito que o 2.0 corrige")
    checar(all(p.texto.endswith(("\n\n", ". ", "; ")) for p in pedacos[:-1]),
           "com paragrafos e frases na janela, o corte cai em paragrafo ou fim de frase")
    checar(busca.POLITICA.cortar("") == [] and busca.POLITICA.cortar("   \n ") == [],
           "texto com 0 caracteres (ou so espaco) nao rende trecho")
    curto = busca.POLITICA.cortar("uma linha só")
    checar(len(curto) == 1 and (curto[0].inicio, curto[0].fim) == (0, 12),
           "unidade menor que o trecho rende exatamente 1 trecho, inteira")
    acentos = corpus["acentos decompostos sem espaco"]
    cortes = [p.fim for p in busca.POLITICA.cortar(acentos)][:-1]
    checar(all(not unicodedata.combining(acentos[f]) for f in cortes),
           "corte duro nunca separa a letra do acento combinante")
    for ruim in ((40, 0), (1500, 750), (1500, -1)):
        try:
            busca.Politica(*ruim)
            checar(False, f"politica invalida {ruim} e recusada")
        except ValueError:
            checar(True, f"politica invalida {ruim} e recusada")
    checar(busca.VERSAO_CHUNKER == f"chunk/2.0:{T}:{S}",
           f"a versao carimbada carrega os parametros ({busca.VERSAO_CHUNKER})")


# ======================================================================
# 5..13. Pipeline
# ======================================================================

def teste_uma_indexacao_tres_ocorrencias(con):
    print("\n5. um conteudo com tres ocorrencias e indexado UMA vez")
    cid = criar_conteudo(con, "QXH-CONTRATO", 5000)
    for i, (nome, item, prio) in enumerate([
            ("Contrato RD-101.pdf", "ITEM-A1", 0),
            ("Contrato RD-101.pdf", "ITEM-B2", 2),
            ("Contrato RD-101 (copia).pdf", "ITEM-C3", 3)]):
        criar_ocorrencia(con, cid, f"{i}_{nome}", item, prioridade=prio)
    paginas = [(1, "CONTRATO DE MEDIÇÃO\nobra RD-101, primeira parcela"),
               (2, "Cláusula segunda: a medição será mensal.")]
    r1 = tx(con, busca.indexar_conteudo, con, cid, paginas, extrator="pdf",
            versao_extrator="pdf/1.1")
    checar(r1["acao"] == "INDEXADO", "primeira indexacao do conteudo")
    checar(con.execute("SELECT COUNT(*) FROM indice_conteudo").fetchone()[0] == 1,
           "existe UMA linha de indexacao para as 3 ocorrencias")
    trechos = con.execute("SELECT COUNT(*) FROM trecho").fetchone()[0]
    checar(trechos == 2, f"2 paginas curtas = 2 trechos, nao 6 ({trechos})")
    checar(con.execute("SELECT COUNT(*) FROM busca_fts").fetchone()[0] == trechos,
           "o invertido tem exatamente uma entrada por trecho")
    r2 = tx(con, busca.indexar_conteudo, con, cid, paginas, extrator="pdf",
            versao_extrator="pdf/1.1")
    checar(r2["acao"] == "INALTERADO", "processar outra copia do mesmo conteudo NAO reindexa")
    checar(con.execute("SELECT COUNT(*) FROM trecho").fetchone()[0] == trechos,
           "e nao duplica trecho nenhum")
    return cid


def teste_busca_devolve_todas_as_ocorrencias(con, cid):
    print("\n6. a busca devolve as TRES ocorrencias, cada uma com sua identidade")
    linhas = buscar_itens(con, "medicao")
    checar(len(linhas) == 3, f"1 conteudo indexado -> 3 linhas de resultado ({len(linhas)})")
    checar({l["conteudo_id"] for l in linhas} == {cid}, "todas apontam para o mesmo conteudo")
    checar({l["item_id"] for l in linhas} == {"ITEM-A1", "ITEM-B2", "ITEM-C3"},
           "cada linha traz o seu proprio item_id")
    checar(len({l["caminho_rel"] for l in linhas}) == 3, "cada linha traz o seu caminho")
    checar(all(l["drive_id"] == DRIVE and l["web_url"] for l in linhas),
           "cada linha traz drive_id e web_url")
    checar([l["prioridade"] for l in linhas] == [0, 2, 3],
           "as ocorrencias vem ordenadas por prioridade documental")
    checar(sum(1 for l in linhas if l["eh_representante"]) == 1,
           "exatamente uma das tres e o representante")
    checar(all("[" in (l["contexto"] or "") for l in linhas),
           "o snippet destaca o termo (content externo funciona)")


def teste_trecho_sabe_a_origem(con):
    print("\n7. o trecho sabe de que pagina, slide ou aba veio")
    casos = [
        ("QXH-PDF", "pdf", "pdf/1.1",
         [(11, "texto irrelevante"), (12, "o pavimento flexivel foi restaurado")],
         "PavimentoRestaurado.pdf", "ITEM-PDF"),
        ("QXH-PPTX", "pptx", "pptx/1.1",
         [(1, "capa"), (2, "cronograma"), (3, "o pavimento flexivel foi restaurado")],
         "Apresentacao.pptx", "ITEM-PPT"),
        ("QXH-XLSX", "xlsx", "xlsx/1.0",
         [(1, "[aba] Resumo\nresumo"),
          (2, "[aba] Medições 2025\no pavimento flexivel foi restaurado")],
         "Planilha.xlsx", "ITEM-XLS"),
        ("QXH-DOCX", "docx", "docx/1.0",
         [(0, "o pavimento flexivel foi restaurado na ata")], "Ata.docx", "ITEM-DOC"),
    ]
    for quick, extrator, versao, paginas, arquivo, item in casos:
        cid = criar_conteudo(con, quick, 7000, extrator=extrator, versao=versao)
        criar_ocorrencia(con, cid, arquivo, item, prioridade=1)
        tx(con, busca.indexar_conteudo, con, cid, paginas, extrator=extrator,
           versao_extrator=versao)
    linhas = busca.buscar(con, '"pavimento flexivel"')
    por_tipo = {l["segmento_tipo"]: l for l in linhas}
    checar(set(por_tipo) == {"pagina", "slide", "aba", "documento"},
           "o mesmo termo foi achado em PDF, PPTX, XLSX e DOCX")
    checar(por_tipo["pagina"]["segmento_numero"] == 12
           and por_tipo["pagina"]["segmento_rotulo"] == "p. 12",
           "o acerto no PDF diz 'p. 12' — o numero real da pagina")
    checar(por_tipo["slide"]["segmento_rotulo"] == "slide 3", "o acerto no PPTX diz 'slide 3'")
    checar(por_tipo["aba"]["segmento_rotulo"] == "Medições 2025",
           "o acerto no XLSX diz o NOME da aba (lido do '[aba] ...' do extrator)")
    checar(por_tipo["documento"]["segmento_rotulo"] == "documento"
           and por_tipo["documento"]["segmento_numero"] == 0,
           "DOCX nao pagina: 'documento', sem pagina inventada")


def teste_trecho_nao_cruza_segmento(con):
    print("\n8. o trecho nunca atravessa a fronteira da pagina")
    cid = criar_conteudo(con, "QXH-LONGO", 99000)
    criar_ocorrencia(con, cid, "Longo.pdf", "ITEM-LONGO", prioridade=1)
    paginas = [(n, " ".join(f"pag{n}palavra{i:04d}" for i in range(700))) for n in (1, 2, 3)]
    tx(con, busca.indexar_conteudo, con, cid, paginas, extrator="pdf",
       versao_extrator="pdf/1.1")
    ruins = 0
    for seg_id, numero in con.execute("SELECT id, numero FROM segmento WHERE conteudo_id=?",
                                      (cid,)).fetchall():
        for (texto,) in con.execute("SELECT texto FROM trecho WHERE segmento_id=?", (seg_id,)):
            outros = {f"pag{n}palavra" for n in (1, 2, 3) if n != numero}
            ruins += any(o in texto for o in outros)
    checar(ruins == 0, "nenhum trecho mistura texto de duas paginas")
    seg = {r["numero"]: r for r in con.execute(
        "SELECT * FROM segmento WHERE conteudo_id=? ORDER BY ordem", (cid,))}
    ok = True
    for numero, bruto in paginas:
        linhas = con.execute("SELECT char_inicio, char_fim, texto FROM trecho"
                             " WHERE segmento_id=? ORDER BY ordinal",
                             (seg[numero]["id"],)).fetchall()
        pedacos = [busca.Pedaco(i, a, b, t) for i, (a, b, t) in enumerate(linhas)]
        ok &= busca.reconstruir(pedacos) == busca.normalizar(bruto)
        ok &= max(len(p.texto) for p in pedacos) <= T
    checar(ok, "por pagina, os trechos do banco reconstroem o texto normalizado inteiro")


def teste_reindexa_sem_orfao(con):
    print("\n9. conteudo alterado reindexa: substitui, nao acumula")
    cid = criar_conteudo(con, "QXH-NOTIF", 9000, versao="pdf/1.0")
    criar_ocorrencia(con, cid, "Notificacao.pdf", "ITEM-NOT", prioridade=0)
    longo = " ".join(f"paragrafo{i:04d} sobre notificacao" for i in range(200))
    tx(con, busca.indexar_conteudo, con, cid, [(1, longo)], extrator="pdf",
       versao_extrator="pdf/1.0")
    ids_antigos = [r[0] for r in con.execute("SELECT id FROM trecho WHERE conteudo_id=?",
                                             (cid,))]
    checar(len(ids_antigos) > 2, f"a versao antiga rendeu {len(ids_antigos)} trechos")
    checar(buscar_itens(con, "paragrafo0007") != [], "o texto antigo e achavel")
    con.execute("UPDATE conteudo SET versao_extrator='pdf/2.0' WHERE id=?", (cid,))
    motivo = con.execute("SELECT motivo FROM v_reindexar WHERE conteudo_id=?",
                         (cid,)).fetchone()
    checar(motivo is not None and motivo[0] == "EXTRATOR_NOVO",
           "v_reindexar aponta o conteudo e diz o motivo (EXTRATOR_NOVO)")
    novo = " ".join(f"secao{i:04d} sobre embargo" for i in range(300))
    r = tx(con, busca.indexar_conteudo, con, cid, [(1, novo)], extrator="pdf",
           versao_extrator="pdf/2.0")
    checar(r["acao"] == "REINDEXADO", "a acao registrada foi REINDEXADO")
    checar(buscar_itens(con, "paragrafo0007") == [], "o texto ANTIGO nao e mais achavel")
    checar(buscar_itens(con, "secao0007") != [], "o texto NOVO e achavel")
    marcas = ",".join("?" * len(ids_antigos))
    checar(con.execute(f"SELECT COUNT(*) FROM trecho WHERE id IN ({marcas})",
                       ids_antigos).fetchone()[0] == 0, "nenhuma linha de trecho antiga sobrou")
    checar(con.execute("SELECT trechos FROM indice_conteudo WHERE conteudo_id=?",
                       (cid,)).fetchone()[0] ==
           con.execute("SELECT COUNT(*) FROM trecho WHERE conteudo_id=?", (cid,)).fetchone()[0],
           "a contabilidade do conteudo = os trechos dele (nao acumulou)")
    checar(con.execute("SELECT COUNT(*) FROM segmento WHERE conteudo_id=?",
                       (cid,)).fetchone()[0] == 1, "o segmento foi trocado, nao acumulado")
    checar(con.execute("SELECT COUNT(*) FROM v_reindexar WHERE conteudo_id=?",
                       (cid,)).fetchone()[0] == 0, "depois de reindexar, sai de v_reindexar")
    checar(busca.integridade_fts(con), "FTS5 integrity-check (rank=1) passa")


def teste_nao_reindexa_o_que_nao_mudou(con):
    print("\n10. o que nao mudou nao e reindexado")
    cid = criar_conteudo(con, "QXH-ATA", 4000, versao="docx/1.0", extrator="docx")
    criar_ocorrencia(con, cid, "Ata2.docx", "ITEM-ATA", prioridade=1)
    paginas = [(0, "Ata da reunião de obra: pavimentação do trecho 3.")]
    tx(con, busca.indexar_conteudo, con, cid, paginas, extrator="docx",
       versao_extrator="docx/1.0")
    ids = [r[0] for r in con.execute("SELECT id FROM trecho WHERE conteudo_id=?", (cid,))]
    con.execute("UPDATE conteudo SET versao_extrator='docx/1.1' WHERE id=?", (cid,))
    r = tx(con, busca.indexar_conteudo, con, cid, paginas, extrator="docx",
           versao_extrator="docx/1.1")
    checar(r["acao"] == "INALTERADO", "extrator novo com texto identico: NAO reindexa")
    checar(ids == [r0[0] for r0 in con.execute("SELECT id FROM trecho WHERE conteudo_id=?",
                                               (cid,))],
           "os mesmos rowid de trecho continuam la")
    checar(con.execute("SELECT versao_extrator FROM indice_conteudo WHERE conteudo_id=?",
                       (cid,)).fetchone()[0] == "docx/1.1", "so o carimbo foi atualizado")
    con.execute("UPDATE meta SET valor='norm/9.9' WHERE chave='busca.versao_normalizador'")
    checar(con.execute("SELECT motivo FROM v_reindexar WHERE conteudo_id=?",
                       (cid,)).fetchone()[0] == "NORMALIZADOR_NOVO",
           "mudar a versao do normalizador poe TUDO na fila — de proposito")
    checar(busca.conferir_versoes(con) != [],
           "conferir_versoes acusa banco e codigo divergentes (o worker recusaria subir)")
    con.execute("UPDATE meta SET valor=? WHERE chave='busca.versao_normalizador'",
                (busca.VERSAO_NORMALIZADOR,))


def teste_missing(con, cid_contrato):
    print("\n11. ocorrencia MISSING some do resultado; o conteudo continua indexado")
    checar(len(buscar_itens(con, "medicao")) == 3, "antes: as 3 ocorrencias aparecem")
    trechos_antes = con.execute("SELECT COUNT(*) FROM trecho WHERE conteudo_id=?",
                                (cid_contrato,)).fetchone()[0]
    con.execute("UPDATE arquivo SET estado='MISSING', presente=0, presente_nuvem=0"
                " WHERE item_id='ITEM-B2'")
    depois = buscar_itens(con, "medicao")
    checar({"ITEM-A1", "ITEM-C3"} == {l["item_id"] for l in depois},
           "a MISSING sai; as outras duas continuam achaveis")
    checar(con.execute("SELECT COUNT(*) FROM trecho WHERE conteudo_id=?",
                       (cid_contrato,)).fetchone()[0] == trechos_antes,
           "NENHUM trecho foi apagado: o indice e do conteudo")
    con.execute("UPDATE arquivo SET estado='COMPLETED', presente=1, presente_nuvem=1"
                " WHERE item_id='ITEM-B2'")
    checar(len(buscar_itens(con, "medicao")) == 3,
           "item que reaparece volta ao resultado sem reextrair e sem reindexar")


def teste_acento_e_caixa(con):
    print("\n12. acento e maiuscula nao atrapalham")
    cid = criar_conteudo(con, "QXH-ACENTO", 3000)
    criar_ocorrencia(con, cid, "Relatorio.pdf", "ITEM-ACC", prioridade=1)
    tx(con, busca.indexar_conteudo, con, cid,
       [(1, "MEDIÇÃO da Execução: manutenção do pavimento e desapropriação da área "
            "de 30m². Certificado de Calibra¸c˜ao.")],
       extrator="pdf", versao_extrator="pdf/1.1")
    for termo in ("medicao", "MEDICAO", "Medição", "MEDIÇÃO", "mediçao", "execucao",
                  "EXECUÇÃO", "manutencao", "desapropriacao", "area", "30m²", "calibracao"):
        achou = [l for l in buscar_itens(con, termo) if l["item_id"] == "ITEM-ACC"]
        checar(len(achou) == 1, f"'{termo}' acha o documento")
    guardado = con.execute("SELECT texto FROM trecho WHERE conteudo_id=?", (cid,)).fetchone()[0]
    checar("MEDIÇÃO" in guardado and "Calibração" in guardado,
           "o texto GUARDADO manteve acento e caixa (e consertou o acento solto)")
    linha = [l for l in buscar_itens(con, "desapropriacao") if l["item_id"] == "ITEM-ACC"][0]
    checar("[desapropriação]" in linha["contexto"], "o snippet destaca a forma acentuada")


def teste_saude(con, rotulo="13. a saude do indice fecha em zero"):
    print(f"\n{rotulo}")
    s = busca.saude(con)
    for chave, valor in zeros(s).items():
        checar(valor == 0, f"v_busca_saude.{chave} = 0", f"deu {valor}")
    checar(s["a_reindexar"] == 0, "nada pendente de reindexacao")
    checar(con.execute("SELECT COUNT(*) FROM busca_fts").fetchone()[0] ==
           con.execute("SELECT COALESCE(SUM(trechos),0) FROM indice_conteudo").fetchone()[0],
           "o invertido bate com a contabilidade de indice_conteudo")
    checar(busca.integridade_fts(con), "FTS5 integrity-check (rank=1) passa")
    checar(con.execute("PRAGMA foreign_key_check").fetchall() == [],
           "foreign_key_check sem violacao")


def teste_fk_protege_a_ordem(con):
    print("\n13b. a ordem da limpeza e protegida pelo banco")
    cid = con.execute("SELECT conteudo_id FROM segmento LIMIT 1").fetchone()[0]
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM segmento WHERE conteudo_id=?", (cid,))
        con.execute("COMMIT")
        checar(False, "apagar segmento antes do trecho tem de falhar")
    except sqlite3.IntegrityError:
        con.execute("ROLLBACK")
        checar(True, "apagar segmento antes do trecho falha (FK sem cascata)")


# ======================================================================
# 14. Manutencao
# ======================================================================

def teste_manutencao(con):
    print("\n14. manutencao do indice")
    # a) a ocorrencia muda de BYTES: vira outro conteudo.
    velho = criar_conteudo(con, "QXH-V1", 1111)
    novo = criar_conteudo(con, "QXH-V2", 2222)
    a_mut = criar_ocorrencia(con, velho, "Mutante.pdf", "ITEM-MUT", prioridade=1)
    criar_ocorrencia(con, velho, "Copia do mutante.pdf", "ITEM-MUT-COPIA", prioridade=2)
    tx(con, busca.indexar_conteudo, con, velho, [(1, "versao antiga do levantamento")],
       extrator="pdf", versao_extrator="pdf/1.1")
    con.execute("UPDATE arquivo SET conteudo_id=? WHERE id=?", (novo, a_mut))
    tx(con, busca.indexar_conteudo, con, novo, [(1, "versao revisada do levantamento")],
       extrator="pdf", versao_extrator="pdf/1.1")
    checar({l["item_id"] for l in buscar_itens(con, "revisada")} == {"ITEM-MUT"},
           "bytes novos: o texto novo leva a ocorrencia que mudou")
    checar({l["item_id"] for l in buscar_itens(con, "antiga")} == {"ITEM-MUT-COPIA"},
           "o conteudo antigo continua achavel so pela copia que nao mudou")

    # b) remocao: so quando nao ha ocorrencia presente, e com carencia.
    cid = criar_conteudo(con, "QXH-SOZINHO", 2000)
    criar_ocorrencia(con, cid, "Unico.pdf", "ITEM-UNICO", prioridade=2)
    tx(con, busca.indexar_conteudo, con, cid, [(1, "laudo geotecnico do aterro")],
       extrator="pdf", versao_extrator="pdf/1.1")
    try:
        tx(con, busca.desindexar_conteudo, con, cid)
        checar(False, "desindexar conteudo com ocorrencia viva e recusado")
    except ValueError:
        checar(True, "desindexar conteudo com ocorrencia viva e recusado")
    con.execute("UPDATE arquivo SET presente=0, estado='MISSING',"
                " ultima_vez_em='2026-08-01T00:00:00+00:00' WHERE item_id='ITEM-UNICO'")
    checar(buscar_itens(con, "geotecnico") == [],
           "sem ocorrencia presente, some do resultado na hora (sem escrita no indice)")
    checar(cid in [r[0] for r in con.execute("SELECT conteudo_id FROM v_conteudo_sem_ocorrencia")],
           "v_conteudo_sem_ocorrencia lista o conteudo")
    checar(cid not in busca.conteudos_para_limpeza(con, 90, "2026-10-04T00:00:00+00:00"),
           "dentro da carencia (90 dias) NAO e candidato a limpeza")
    checar(cid in busca.conteudos_para_limpeza(con, 30, "2026-10-04T00:00:00+00:00"),
           "fora da carencia (30 dias) e candidato a limpeza")
    fts_antes = con.execute("SELECT COUNT(*) FROM busca_fts").fetchone()[0]
    saiu = tx(con, busca.desindexar_conteudo, con, cid)
    checar(saiu == 1 and con.execute("SELECT COUNT(*) FROM trecho WHERE conteudo_id=?",
                                     (cid,)).fetchone()[0] == 0,
           "desindexar tira os trechos do conteudo")
    checar(con.execute("SELECT COUNT(*) FROM busca_fts").fetchone()[0] == fts_antes - 1,
           "e o invertido perde exatamente essa linha (gatilho)")
    checar(con.execute("SELECT estado FROM indice_conteudo WHERE conteudo_id=?",
                       (cid,)).fetchone()[0] == "REMOVIDO",
           "a contabilidade fica, com estado REMOVIDO (REGRA 6)")
    checar(cid not in [r[0] for r in con.execute(
        "SELECT conteudo_id FROM v_conteudo_sem_ocorrencia")],
           "ja limpo, sai da fila de limpeza")
    checar(all(v == 0 for v in zeros(busca.saude(con)).values())
           and busca.integridade_fts(con), "saude e integrity-check seguem zerados")
    con.execute("UPDATE arquivo SET presente=1, estado='COMPLETED' WHERE item_id='ITEM-UNICO'")
    checar(con.execute("SELECT motivo FROM v_reindexar WHERE conteudo_id=?",
                       (cid,)).fetchone()[0] == "REMOVIDO",
           "a ocorrencia voltou: o conteudo reentra em v_reindexar (motivo REMOVIDO)")
    r = tx(con, busca.indexar_conteudo, con, cid, [(1, "laudo geotecnico do aterro")],
           extrator="pdf", versao_extrator="pdf/1.1")
    checar(r["acao"] == "REINDEXADO" and buscar_itens(con, "geotecnico") != [],
           "reindexado, volta a ser achavel")

    # c) rastreabilidade: trecho -> conteudo -> ocorrencias presentes.
    con.execute("UPDATE arquivo SET presente=0, estado='MISSING' WHERE item_id='ITEM-B2'")
    tid = buscar_itens(con, "mensal")[0]["trecho_id"]
    rast = busca.rastrear(con, tid)
    checar(rast["segmento_rotulo"] == "p. 2" and rast["quick_xor"] == "QXH-CONTRATO",
           "rastrear: o trecho diz a pagina e o conteudo (quick_xor) de onde veio")
    checar(rast["versao_chunker"] == busca.VERSAO_CHUNKER
           and rast["versao_normalizador"] == busca.VERSAO_NORMALIZADOR
           and rast["fonte_texto"] == "extrator" and rast["indexado_em"],
           "rastrear: versoes de extrator/normalizador/chunker, fonte e data")
    checar({o["item_id"] for o in rast["ocorrencias"]} == {"ITEM-A1", "ITEM-C3"},
           "rastrear: so as ocorrencias PRESENTES, cada uma com item_id e caminho")
    con.execute("UPDATE arquivo SET presente=1, estado='COMPLETED' WHERE item_id='ITEM-B2'")


def teste_entrada_do_worker(con):
    print("\n15. a entrada do worker: indexar_resultado()")
    cid = criar_conteudo(con, "QXH-WORKER", 4242, extrator="pptx", versao="pptx/1.1")
    criar_ocorrencia(con, cid, "Reuniao.pptx", "ITEM-PPTX", prioridade=1)
    resultado = {"status": "ok", "extrator": "pptx", "versao": "pptx/1.1",
                 "texto": "capa\n\nterraplenagem em execucao",
                 "paginas_texto": [(1, "capa"), (2, "terraplenagem em execucao")]}
    r = tx(con, busca.indexar_resultado, con, cid, resultado, execucao_id=None)
    checar(r["acao"] == "INDEXADO" and r["segmentos"] == 2, "resultado do extrator indexado")
    checar(buscar_itens(con, "terraplenagem")[0]["segmento_rotulo"] == "slide 2",
           "o slide de origem chega ao resultado")
    cid2 = criar_conteudo(con, "QXH-SO-TEXTO", 4343, extrator="csv", versao="csv/1.1")
    criar_ocorrencia(con, cid2, "Tabela.csv", "ITEM-CSV", prioridade=1)
    r2 = tx(con, busca.indexar_resultado, con, cid2,
            {"extrator": "csv", "versao": "csv/1.1", "texto": "estaca;cota",
             "paginas_texto": []})
    checar(r2["acao"] == "INDEXADO", "sem paginas_texto, o `texto` vira um segmento documento")
    cid3 = criar_conteudo(con, "QXH-VAZIO", 4444, estado="OCR_REQUIRED")
    criar_ocorrencia(con, cid3, "Escaneado.pdf", "ITEM-OCR", prioridade=1)
    r3 = tx(con, busca.indexar_resultado, con, cid3,
            {"extrator": "pdf", "versao": "pdf/1.1", "texto": "", "paginas_texto": []})
    checar(r3["acao"] == "SEM_TEXTO", "sem texto: SEM_TEXTO registrado, nenhum trecho")
    r4 = tx(con, busca.indexar_resultado, con, cid3,
            {"extrator": "pdf", "versao": "pdf/1.1", "texto": "", "paginas_texto": []})
    checar(r4["acao"] == "INALTERADO", "SEM_TEXTO repetido nao reescreve nada")


# ======================================================================
# 15c. Fronteira de pagina no derivado (form feed) — E9
# ======================================================================

def teste_fronteira_no_derivado(con, area: Path):
    """O derivado .txt.gz de pdf/pptx/xlsx guarda onde cada unidade comeca.

    Ida e volta: extrator -> `texto` -> gzip (cu.gravar_texto) -> ler_derivado
    -> paginas_do_derivado tem de devolver EXATAMENTE o `paginas_texto` que o
    extrator entregou — numero e texto —, e reindexar a partir do derivado
    nao pode mudar o indice (INALTERADO).
    """
    print("\n15c. fronteira de pagina/slide/aba sobrevive no derivado (\\f)")
    import fitz
    import openpyxl
    from pptx import Presentation
    import extratores

    pasta = area / "fronteira"
    pasta.mkdir(exist_ok=True)

    pdf = pasta / "tres_paginas.pdf"
    doc = fitz.open()
    for numero in (1, 2, 3):
        pagina = doc.new_page()
        if numero != 2:                                # a 2 fica em branco
            for linha in range(6):
                pagina.insert_text((50, 60 + 16 * linha),
                                   f"pagina {numero} linha {linha}: terraplenagem na "
                                   f"estaca {numero}20 da RD-103, medicao mensal", fontsize=9)
    doc.save(str(pdf))
    doc.close()

    pptx = pasta / "tres_slides.pptx"
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = "Reuniao de obra - pauta"
    prs.slides.add_slide(prs.slide_layouts[6])         # slide 2 em branco
    s3 = prs.slides.add_slide(prs.slide_layouts[5])
    s3.shapes.title.text = "Encaminhamentos"
    s3.notes_slide.notes_text_frame.text = "cobrar o cronograma"
    prs.save(str(pptx))

    xlsx = pasta / "tres_abas.xlsx"
    livro = openpyxl.Workbook()
    livro.active.title = "Resumo"
    livro.active["A1"] = "item"
    livro.create_sheet("Branca")
    livro.create_sheet("Detalhe")["A1"] = "estaca 120"
    livro.save(str(xlsx))

    for indice, (arquivo, categoria, numeros) in enumerate(
            ((pdf, "pdf", [1, 3]), (pptx, "pptx", [1, 3]), (xlsx, "xlsx", [1, 3]))):
        r = extratores.extrair(arquivo, categoria, {})
        checar(r["status"] == "ok" and [n for n, _ in r["paginas_texto"]] == numeros,
               f"{categoria}: o extrator numera as unidades {numeros}",
               f"{r['status']} {[n for n, _ in r['paginas_texto']]} {r['erro']}")
        checar(r["texto"].endswith("\f") and r["texto"].count("\f") == 3,
               f"{categoria}: cada unidade terminada por \\f (inclusive a vazia)",
               repr(r["texto"][-40:]))
        destino = pasta / f"{categoria}.txt.gz"
        cu.gravar_texto(destino, r["texto"])
        lido = busca.ler_derivado(destino)
        checar(lido == r["texto"], f"{categoria}: gzip devolve o texto identico, \\f incluso")
        paginas = busca.paginas_do_derivado(lido, categoria)
        checar(paginas == r["paginas_texto"],
               f"{categoria}: paginas_do_derivado == paginas_texto do extrator",
               f"{paginas!r:.200}")
        a, _ = busca.preparar(r["paginas_texto"], categoria)
        b, _ = busca.preparar(paginas, categoria)
        checar(busca.sha_texto(a) == busca.sha_texto(b) and len(a) == 2,
               f"{categoria}: mesmos segmentos (tipo, numero, rotulo, texto)")

        cid = criar_conteudo(con, f"QXH-FF-{indice}", 7000 + indice, extrator=categoria,
                             versao=r["versao"], texto_path=str(destino))
        criar_ocorrencia(con, cid, arquivo.name, f"ITEM-FF-{indice}", prioridade=1)
        r1 = tx(con, busca.indexar_resultado, con, cid, r, execucao_id=None)
        conteudo = con.execute("SELECT * FROM conteudo WHERE id=?", (cid,)).fetchone()
        pags, fonte, _ = busca.fonte_do_conteudo(conteudo, {}, [], T, S)
        checar(fonte == "derivado" and pags == r["paginas_texto"],
               f"{categoria}: fonte_do_conteudo reconstroi as unidades do derivado",
               f"{fonte} {pags!r:.120}")
        r2 = tx(con, busca.indexar_conteudo, con, cid, pags, extrator=categoria,
                versao_extrator=r["versao"], fonte_texto="derivado")
        checar(r1["acao"] == "INDEXADO" and r2["acao"] == "INALTERADO",
               f"{categoria}: reindexar do derivado nao muda o indice",
               f"{r1['acao']} -> {r2['acao']}")

    print("\n15d. derivado antigo e texto livre continuam como antes")
    antigo = "pagina um\n\npagina dois"
    checar(busca.paginas_do_derivado(antigo, "pdf") == [(0, antigo)],
           "PDF antigo (sem \\f): um segmento documento, como antes")
    antigo_xlsx = "[aba] A\nx\n\n[aba] B\ny"
    checar([n for n, _ in busca.paginas_do_derivado(antigo_xlsx, "xlsx")] == [1, 2],
           "XLSX antigo: fronteira pelo rotulo [aba], como antes")
    impressora = "relatorio\fpagina 2 do relatorio"
    checar(busca.paginas_do_derivado(impressora, "texto") == [(0, impressora)],
           ".txt com form feed de impressora continua um documento so")
    checar(busca.paginas_do_derivado("\f\f", "pdf") == [],
           "derivado so de separadores nao inventa segmento")


# ======================================================================
# 16. Migracao ponta a ponta
# ======================================================================

def popular_v4(con, area: Path) -> dict:
    """Um banco v4 como o worker antigo deixa: conteudo, ocorrencias,
    `texto_fts` por arquivo_id (chunk/1.0) e derivados .txt.gz."""
    derivados = area / "derivados" / "texto"
    derivados.mkdir(parents=True, exist_ok=True)
    esperado = {}

    def conteudo_v4(quick, extrator, paginas, ocorrencias, *, com_fts=True,
                    com_derivado=True, estado="EXTRAIDO"):
        texto = "\n\n".join(t for _, t in paginas)
        caminho = None
        cid = criar_conteudo(con, quick, len(texto), extrator=extrator,
                             versao=f"{extrator}/1.0", estado=estado)
        ids = []
        for nome, item, presente in ocorrencias:
            ids.append(criar_ocorrencia(con, cid, nome, item, presente=presente,
                                        estado="COMPLETED" if presente else "MISSING"))
        if com_derivado:
            caminho = derivados / f"{ids[0]}.txt.gz"
            with gzip.open(caminho, "wt", encoding="utf-8") as fh:
                fh.write(texto)
            con.execute("UPDATE conteudo SET texto_path=? WHERE id=?", (str(caminho), cid))
        if com_fts:
            for pagina, t in paginas:
                for i, trecho in enumerate(cu.trechos_de(t, 1500, 150)):
                    con.execute("INSERT INTO texto_fts (arquivo_id, caminho_rel, pagina,"
                                " trecho_idx, texto) VALUES (?,?,?,?,?)",
                                (ids[0], "x", pagina, i, trecho))
        esperado[quick] = cid
        return cid

    longa = " ".join(f"medicao{i:04d} do pavimento" for i in range(400))   # > 1500 chars
    con.execute("BEGIN")
    conteudo_v4("MIG-PDF", "pdf",
                [(1, "RELATÓRIO SEMANAL\nobra RD-101"), (2, longa),
                 (3, "Não conformidades emitidas pelo Programa no período")],
                [("Relatorio.pdf", "M-PDF-1", 1), ("Relatorio (copia).pdf", "M-PDF-2", 1),
                 ("Relatorio antigo.pdf", "M-PDF-3", 0)])
    conteudo_v4("MIG-XLSX", "xlsx",
                [(1, "[aba] Resumo\nEstaca | Cota"),
                 (3, "[aba] Ensaios 2026\nDesgaste por abrasão Los Angeles | 32%")],
                [("Ensaios.xlsx", "M-XLS-1", 1)])
    conteudo_v4("MIG-PPTX", "pptx",
                [(1, "One Page"), (2, "cronograma"), (3, "alteração de traçado")],
                [("Slides.pptx", "M-PPT-1", 1)])
    conteudo_v4("MIG-DOCX", "docx",
                [(0, "Memorial descritivo da faixa de domínio")],
                [("Memorial.docx", "M-DOC-1", 1)])
    conteudo_v4("MIG-SO-DERIVADO", "pdf",
                [(1, "pagina um do laudo"), (2, "laudo de sondagem rotativa")],
                [("Laudo.pdf", "M-DER-1", 1)], com_fts=False)
    conteudo_v4("MIG-SEM-FONTE", "pdf", [(1, "texto perdido")],
                [("Perdido.pdf", "M-SEM-1", 1)], com_fts=False, com_derivado=False)
    conteudo_v4("MIG-OCR", "pdf", [(1, "")], [("Escaneado.pdf", "M-OCR-1", 1)],
                com_fts=False, com_derivado=False, estado="OCR_REQUIRED")
    conteudo_v4("MIG-SO-MISSING", "pdf", [(1, "documento que sumiu")],
                [("Sumiu.pdf", "M-MIS-1", 0)])
    # Lixo da era local: trechos de um arquivo sem conteudo_id.
    orfao = criar_ocorrencia(con, None, "Local.pdf", "M-LOCAL-1")
    con.execute("INSERT INTO texto_fts (arquivo_id, caminho_rel, pagina, trecho_idx, texto)"
                " VALUES (?,?,?,?,?)", (orfao, "x", 1, 0, "trecho da era local"))
    con.execute("COMMIT")
    return esperado


def sha_arquivo(caminho: Path) -> str:
    return hashlib.sha256(caminho.read_bytes()).hexdigest()


def teste_migracao():
    print("\n16. 05_migrar_v5.py ponta a ponta, em banco criado pela receita")
    area = area_temporaria("cerebro_mig_v5_")
    con, config = montar_banco(area)
    banco = area / "teste.db"
    ids = popular_v4(con, area)
    v4_antes = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("arquivo", "conteudo", "texto_fts")}
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()

    foto = sha_arquivo(banco)
    r = rodar("05_migrar_v5.py", config)
    checar(r.returncode == 0 and "SIMULACAO" in r.stdout,
           "sem --aplicar: simulacao", r.stdout[-600:] + r.stderr[-600:])
    checar(re.search(r"a indexar\s+6 conteudos", r.stdout) is not None,
           "a simulacao conta 6 conteudos elegiveis (EXTRAIDO + ocorrencia presente)",
           r.stdout[-900:])
    checar(sha_arquivo(banco) == foto, "a simulacao nao escreveu um byte no banco")

    r = rodar("05_migrar_v5.py", config, "--aplicar", "--sem-backup", "--simular-falha")
    checar(r.returncode == 1 and "ROLLBACK" in r.stdout,
           "falha no meio da migracao -> ROLLBACK", r.stdout[-600:] + r.stderr[-600:])
    ro = sqlite3.connect(banco)
    checar(ro.execute("SELECT name FROM sqlite_master WHERE name IN"
                      " ('segmento','trecho','busca_fts','indice_conteudo')").fetchall() == []
           and ro.execute("SELECT valor FROM meta WHERE chave='schema_versao'")
           .fetchone()[0] == "4"
           and ro.execute("SELECT COUNT(*) FROM execucao WHERE etapa='migrar_v5'")
           .fetchone()[0] == 0,
           "depois do ROLLBACK nem o DDL ficou: sem tabela v5, schema v4, sem execucao")
    ro.close()

    r = rodar("05_migrar_v5.py", config, "--aplicar")
    checar(r.returncode == 0, "--aplicar terminou com sucesso", r.stdout[-1500:] + r.stderr[-800:])
    backups = list((area / "backup").glob("teste_pre_v5_*.db"))
    checar(len(backups) == 1, "backup criado em <banco>/../backup pela API de backup")
    if backups:
        b = sqlite3.connect(backups[0])
        checar(b.execute("PRAGMA quick_check").fetchone()[0] == "ok"
               and b.execute("SELECT valor FROM meta WHERE chave='schema_versao'")
               .fetchone()[0] == "4", "o backup e um v4 integro (quick_check ok)")
        b.close()
    checar(re.search(r"quick_check\s+ok", r.stdout) is not None,
           "quick_check do banco depois do COMMIT: ok")

    con = sqlite3.connect(banco, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    checar(con.execute("SELECT valor FROM meta WHERE chave='schema_versao'").fetchone()[0] == "5",
           "meta.schema_versao = '5'")
    checar(busca.conferir_versoes(con) == [], "versoes de busca carimbadas = as do codigo")
    checar({t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in v4_antes}
           == v4_antes, "arquivo, conteudo e texto_fts com as mesmas contagens (texto_fts fica)")
    fontes = dict(con.execute("SELECT fonte_texto, COUNT(*) FROM indice_conteudo"
                              " GROUP BY fonte_texto").fetchall())
    checar(fontes == {"indice_v4": 4, "derivado": 1},
           f"4 conteudos reconstruidos do indice v4, 1 do derivado ({fontes})")
    checar(con.execute("SELECT COUNT(*) FROM indice_conteudo WHERE conteudo_id IN (?,?)",
                       (ids["MIG-SO-MISSING"], ids["MIG-OCR"])).fetchone()[0] == 0,
           "conteudo sem ocorrencia presente e OCR_REQUIRED ficam fora")
    pendentes = [r0[0] for r0 in con.execute("SELECT conteudo_id FROM v_reindexar")]
    checar(pendentes == [ids["MIG-SEM-FONTE"]],
           "o conteudo sem fonte continua em v_reindexar (estado explicito, nao sumiu)")
    checar(con.execute("SELECT execucao_id FROM indice_conteudo LIMIT 1").fetchone()[0] ==
           con.execute("SELECT id FROM execucao WHERE etapa='migrar_v5'").fetchone()[0],
           "cada indexacao aponta para a execucao da migracao (REGRA 9)")

    rot = {r0["rotulo"] for r0 in con.execute(
        "SELECT s.rotulo FROM segmento s WHERE s.conteudo_id IN (?,?,?)",
        (ids["MIG-PDF"], ids["MIG-XLSX"], ids["MIG-PPTX"]))}
    checar({"p. 1", "p. 2", "p. 3", "Resumo", "Ensaios 2026", "slide 3"} <= rot,
           "a migracao preservou pagina, aba (com nome) e slide do indice v4")
    seg_der = con.execute("SELECT tipo, rotulo FROM segmento WHERE conteudo_id=?",
                          (ids["MIG-SO-DERIVADO"],)).fetchall()
    checar([tuple(x) for x in seg_der] == [("documento", "documento")],
           "do derivado de PDF nao se inventa pagina: um segmento 'documento'")
    pdf_txt = "\n\n".join(r0[0] for r0 in con.execute(
        "SELECT texto FROM trecho WHERE conteudo_id=? AND ordinal=0 ORDER BY id",
        (ids["MIG-PDF"],)))
    checar("medicao0000" in pdf_txt and "RELATÓRIO SEMANAL" in pdf_txt,
           "o texto reconstruido e o texto original das paginas")

    linhas = buscar_itens(con, "abrasao los angeles")
    checar(len(linhas) == 1 and linhas[0]["segmento_rotulo"] == "Ensaios 2026",
           "busca pos-migracao acha a planilha e diz a aba")
    linhas = buscar_itens(con, "nao conformidades emitidas")
    checar({l["item_id"] for l in linhas} == {"M-PDF-1", "M-PDF-2"}
           and all(l["segmento_rotulo"] == "p. 3" for l in linhas),
           "1 conteudo, 2 ocorrencias presentes no resultado (a MISSING nao), p. 3")
    checar(buscar_itens(con, "sumiu") == [] and buscar_itens(con, "era local") == [],
           "nada do que nao tem ocorrencia presente ou conteudo aparece")
    teste_saude_pos_migracao(con)
    ids_trechos = [r0[0] for r0 in con.execute("SELECT id FROM trecho ORDER BY id")]
    con.close()

    r = rodar("05_migrar_v5.py", config, "--aplicar")
    con = sqlite3.connect(banco)
    checar(r.returncode == 0 and len(list((area / "backup").glob("*.db"))) == 1
           and [r0[0] for r0 in con.execute("SELECT id FROM trecho ORDER BY id")] == ids_trechos,
           "segunda rodada: idempotente (nenhum trecho reescrito, nenhum backup novo)",
           r.stdout[-800:])
    con.close()

    cfg = cu.carregar_config(config)
    with op.TravaProcesso(op.caminho_lock(cfg), rotulo="worker-de-teste") as trava:
        checar(trava.adquirida, "um 'worker' segura o lock")
        r = rodar("05_migrar_v5.py", config, "--aplicar")
        checar(r.returncode == 3 and "RECUSADO" in r.stdout,
               "com o worker rodando, a migracao recusa", r.stdout[-500:])


def teste_saude_pos_migracao(con):
    s = busca.saude(con)
    checar(all(v == 0 for v in zeros(s).values()),
           f"pos-migracao: v_busca_saude zerada ({zeros(s)})")
    checar(busca.integridade_fts(con), "pos-migracao: integrity-check (rank=1) passa")

def teste_mesmo_texto() -> None:
    print("\n15f. derivado novo (com \\f) nao 'diverge' do indice v4 de mesmo texto")
    paginas = [(1, "Pagina um do laudo."), (2, ""), (3, "Pagina tres, conclusao.")]
    antigo = "\n\n".join(t for _, t in paginas)
    novo = "Pagina um do laudo.\f\fPagina tres, conclusao.\f"
    checar(busca.mesmo_texto(paginas, antigo, "pdf"), "derivado antigo (linha em branco) bate")
    checar(busca.mesmo_texto(paginas, novo, "pdf"), "derivado novo (form feed) bate")
    checar(not busca.mesmo_texto(paginas, novo.replace("tres", "TRES"), "pdf"),
           "texto diferente continua divergindo")
    checar(not busca.mesmo_texto(paginas, novo, "texto"),
           "fora de formato paginado o \\f nao e fronteira")


# ======================================================================

def main() -> int:
    banco_real = RAIZ / "data" / "cerebro.db"
    foto = (banco_real.stat().st_size, banco_real.stat().st_mtime_ns) \
        if banco_real.exists() else None

    area = area_temporaria("cerebro_busca_")
    print(f"area: {area}")
    con, _ = montar_banco(area)
    try:
        teste_ddl(con)
        teste_normalizacao()
        teste_cabecalho_rodape()
        teste_corte()
        cid = teste_uma_indexacao_tres_ocorrencias(con)
        teste_busca_devolve_todas_as_ocorrencias(con, cid)
        teste_trecho_sabe_a_origem(con)
        teste_trecho_nao_cruza_segmento(con)
        teste_reindexa_sem_orfao(con)
        teste_nao_reindexa_o_que_nao_mudou(con)
        teste_missing(con, cid)
        teste_acento_e_caixa(con)
        teste_saude(con)
        teste_fk_protege_a_ordem(con)
        teste_manutencao(con)
        teste_entrada_do_worker(con)
        teste_saude(con, "15b. saude depois da manutencao")
        teste_fronteira_no_derivado(con, area)
        teste_saude(con, "15e. saude depois da fronteira no derivado")
        teste_mesmo_texto()
    finally:
        con.close()
    teste_migracao()

    print("\n17. o banco real nao foi tocado")
    if foto is None:
        checar(True, "data/cerebro.db nao existe nesta arvore — nada a conferir")
    else:
        agora = (banco_real.stat().st_size, banco_real.stat().st_mtime_ns)
        checar(agora == foto, "data/cerebro.db intacto (mesmo tamanho, mesmo mtime)",
               f"{foto} -> {agora}")
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
