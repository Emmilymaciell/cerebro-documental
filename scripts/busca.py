"""
busca — a camada de busca documental (schema v5). Deterministica, local, sem IA.

Responde "onde esta escrito isto?" pelo caminho inteiro:

    conteudo -> paginas_texto do extrator -> normalizar() -> segmento
             -> cortar() em trechos -> trecho (+ gatilho -> busca_fts)
             -> buscar() -> TODAS as ocorrencias presentes daquele conteudo

Um conteudo e indexado UMA vez (chave `conteudo_id`); a volta para os N
lugares fisicos e a `v_ocorrencia` do v4, via `v_trecho_ocorrencia`.

Nada aqui chama rede, IA ou servico externo (REGRA 5, DEC-039). Entrada igual,
saida igual — byte a byte. As regras e os numeros que as sustentam estao em
`docs/09_BUSCA.md`.

Uso de linha de comando (as duas formas so LEEM o banco indicado):

    python scripts\\busca.py buscar "faixa de dominio" --banco <caminho.db>
    python scripts\\busca.py bench --banco <caminho.db>

`bench` copia o banco (API de backup, a partir de uma conexao somente
leitura) para uma pasta temporaria FORA do repositorio, aplica o v5 na copia,
indexa a amostra com cada politica de corte, mede, e apaga a copia.

Ponto de entrada para o worker (integracao do Lead):

    busca.indexar_resultado(con, conteudo_id, resultado, execucao_id=...)

chamado DEPOIS de `gravar_conteudo()` (e o que conhece o `conteudo_id`), na
mesma transacao do item (REGRA 8). Nao faz commit.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ_REPO = Path(__file__).resolve().parents[1]
DDL_V5 = RAIZ_REPO / "sql" / "schema_v5_busca.sql"

# ======================================================================
# 1. VERSOES — tem de bater com o que `sql/schema_v5_busca.sql` semeia em
#    `meta`. Mudou um parametro, muda a versao, e tudo entra em v_reindexar.
# ======================================================================

VERSAO_NORMALIZADOR = "norm/1.1"
TOKENIZER = "unicode61 remove_diacritics 2"

# Politica de corte escolhida pelo benchmark de 2026-10-04 (docs/09_BUSCA.md,
# secao 4). Os numeros estao la; aqui so a decisao.
TRECHO_CHARS = 1500
TRECHO_SOBREPOSICAO = 150


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ======================================================================
# 2. NORMALIZACAO — `norm/1.1`
#
# Igual ao `norm/1.0` proposto em 2026-09-27, mais duas correcoes achadas
# na amostra real (docs/09_BUSCA.md, secao 3):
#   a) acento "solto" de PDF gerado por LaTeX: `medi¸c˜ao` -> `medição`;
#   b) a regra de cabecalho/rodape corrido so age em PAGINA (pdf). Em aba de
#      planilha a "ultima linha que e so um numero" e DADO, nao rodape.
# ======================================================================

_INVISIVEIS = dict.fromkeys([0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD, 0x2060], None)

_ESPACOS = ("          "
            "     　\t")

_MINUSCULA = "a-zà-öø-ÿ"
_HIFEN_FIM = re.compile(rf"([{_MINUSCULA}])-\n([{_MINUSCULA}])")

# Acento de espacamento seguido de letra: o PDF do LaTeX desenha o acento
# como glifo separado ANTES da letra. So junta se o resultado for UM
# caractere pre-composto (NFC) — `¸c` -> `ç`, `˜a` -> `ã`, `´e` -> `é`.
# Crase (`) fica de fora de proposito: e comum em codigo e em markdown.
_ACENTO_SOLTO = {
    "´": "́",   # ´  agudo
    "˜": "̃",   # ˜  til
    "ˆ": "̂",   # ˆ  circunflexo
    "¸": "̧",   # ¸  cedilha
    "¨": "̈",   # ¨  trema
}
_RE_ACENTO_SOLTO = re.compile("([" + "".join(_ACENTO_SOLTO) + r"])(\w)")


def _juntar_acento_solto(texto: str) -> str:
    def troca(m: re.Match) -> str:
        composto = unicodedata.normalize("NFC", m.group(2) + _ACENTO_SOLTO[m.group(1)])
        return composto if len(composto) == 1 else m.group(0)
    return _RE_ACENTO_SOLTO.sub(troca, texto)


def normalizar(texto: str) -> str:
    """Texto -> texto normalizado. Pura, deterministica, idempotente.

    NAO baixa caixa e NAO tira acento: disso cuida o tokenizador do FTS5 na
    hora da consulta. O que fica guardado e o que o documento diz.
    """
    if not texto:
        return ""
    t = _juntar_acento_solto(texto)           # antes do NFKC, que separaria ¸ em " ̧"
    t = unicodedata.normalize("NFKC", t)
    t = t.translate(_INVISIVEIS)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    for espaco in _ESPACOS:
        t = t.replace(espaco, " ")
    t = "".join(c for c in t if c == "\n" or unicodedata.category(c)[0] != "C")
    t = _HIFEN_FIM.sub(r"\1\2", t)
    t = re.sub(r" {2,}", " ", t)
    t = re.sub(r" +\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def normalizar_consulta(texto: str) -> str:
    """A consulta passa pela mesma dobra de compatibilidade do texto guardado.

    Sem isto, `m²` digitado na busca nao acharia o `m2` que o NFKC gravou.
    """
    t = _juntar_acento_solto(texto or "")
    t = unicodedata.normalize("NFKC", t).translate(_INVISIVEIS)
    for espaco in _ESPACOS:
        t = t.replace(espaco, " ")
    return t.strip()


def consulta_de_termos(texto: str) -> str:
    """Texto livre -> expressao FTS5 segura: cada palavra vira uma frase.

    `RD-101 faixa` -> `"RD-101" "faixa"`. Hifen, barra e ponto nao viram
    erro de sintaxe; dentro da frase o tokenizador os trata como separador,
    entao `"RD-101"` casa com a sequencia `rd 101`.
    """
    partes = [p for p in normalizar_consulta(texto).split() if p.strip()]
    return " ".join('"' + p.replace('"', '""') + '"' for p in partes)


# Rodape que e so numero de pagina.
_SO_NUMERO = re.compile(
    r"^\s*(?:p[aá]g(?:ina)?\.?\s*)?\d{1,4}(?:\s*(?:/|de)\s*\d{1,4})?\s*$",
    re.IGNORECASE)

BORDA_LINHAS = 3
MIN_SEGMENTOS = 5
FRACAO_REPETICAO = 0.6
MAX_CHARS_LINHA = 120


def remover_repetidos(segmentos: list[str]) -> tuple[list[str], list[str]]:
    """Tira cabecalho/rodape corrido das bordas das PAGINAS. (textos, removidas)."""
    if len(segmentos) < MIN_SEGMENTOS:
        return list(segmentos), []

    def bordas(texto: str):
        linhas = texto.split("\n")
        cheias = [i for i, linha in enumerate(linhas) if linha.strip()]
        return linhas, set(cheias[:BORDA_LINHAS]) | set(cheias[-BORDA_LINHAS:])

    contagem: Counter[str] = Counter()
    for texto in segmentos:
        linhas, idx = bordas(texto)
        for linha in {linhas[i].strip() for i in idx}:
            if 0 < len(linha) <= MAX_CHARS_LINHA:
                contagem[linha] += 1

    limite = max(2, math.ceil(FRACAO_REPETICAO * len(segmentos)))
    corridas = {linha for linha, n in contagem.items() if n >= limite}

    saida, removidas = [], set()
    for texto in segmentos:
        linhas, idx = bordas(texto)
        mantidas = []
        for i, linha in enumerate(linhas):
            nua = linha.strip()
            if i in idx and nua:
                if nua in corridas:
                    removidas.add(nua)
                    continue
                if _SO_NUMERO.match(nua):
                    removidas.add("<numero de pagina>")
                    continue
            mantidas.append(linha)
        saida.append(re.sub(r"\n{3,}", "\n\n", "\n".join(mantidas)).strip())
    return saida, sorted(removidas)


# ======================================================================
# 3. CORTE EM TRECHOS — `chunk/2.0:<tamanho>:<sobreposicao>`
#
# Regras, todas deterministicas (docs/09_BUSCA.md, secao 4):
#   - o trecho NUNCA atravessa o segmento (pagina/slide/aba);
#   - nenhum trecho passa de `tamanho` caracteres; nenhum sai vazio;
#   - o corte cai na metade final da janela, no separador mais forte que
#     existir: paragrafo (\n\n) > fim de frase ([.!?;] + espaco) > quebra de
#     linha > espaco > corte duro (que nunca separa letra de acento);
#   - o separador fica no FIM do trecho que termina;
#   - o trecho seguinte recua ate `sobreposicao` caracteres, mas COMECA EM
#     INICIO DE PALAVRA — o chunk/1.0 comecava no meio da palavra e punha
#     um token falso no indice (`ação` de `medição`);
#   - a concatenacao dos trechos sem a sobreposicao reconstroi o segmento
#     exatamente.
# ======================================================================

_FIM_FRASE = re.compile(r"[.!?;]\s")


@dataclass(frozen=True)
class Pedaco:
    ordinal: int
    inicio: int
    fim: int
    texto: str


@dataclass(frozen=True)
class Politica:
    tamanho: int = TRECHO_CHARS
    sobreposicao: int = TRECHO_SOBREPOSICAO
    modo: str = "janela"          # janela | paragrafo | legado

    def __post_init__(self):
        if self.modo not in ("janela", "paragrafo", "legado"):
            raise ValueError(f"modo de corte desconhecido: {self.modo}")
        if self.tamanho < 50:
            raise ValueError("tamanho do trecho abaixo de 50 caracteres")
        if not 0 <= self.sobreposicao < self.tamanho // 2:
            raise ValueError("sobreposicao tem de ficar em [0, tamanho/2)")

    @property
    def versao(self) -> str:
        if self.modo == "legado":
            return f"chunk/1.0:{self.tamanho}:{self.sobreposicao}"
        if self.modo == "paragrafo":
            return f"par/2.0:{self.tamanho}:{self.sobreposicao}"
        return f"chunk/2.0:{self.tamanho}:{self.sobreposicao}"

    def cortar(self, texto: str) -> list[Pedaco]:
        if self.modo == "legado":
            return _cortar_legado(texto, self.tamanho, self.sobreposicao)
        return _cortar(texto, self.tamanho, self.sobreposicao,
                       paragrafo=self.modo == "paragrafo")


POLITICA = Politica(TRECHO_CHARS, TRECHO_SOBREPOSICAO, "janela")
VERSAO_CHUNKER = POLITICA.versao


def _ponto_de_corte(texto: str, inicio: int, tamanho: int, paragrafo: bool) -> int:
    limite = inicio + tamanho                      # fim exclusivo maximo
    if paragrafo:
        p = texto.rfind("\n\n", inicio + 1, limite)
        if p != -1:
            return p + 2
    minimo = inicio + tamanho // 2
    p = texto.rfind("\n\n", minimo, limite)
    if p != -1:
        return p + 2
    ultimo = None
    for m in _FIM_FRASE.finditer(texto, minimo, limite):
        ultimo = m
    if ultimo is not None:
        return ultimo.end()
    p = texto.rfind("\n", minimo, limite)
    if p != -1:
        return p + 1
    p = texto.rfind(" ", minimo, limite)
    if p != -1:
        return p + 1
    fim = limite                                   # corte duro
    while fim > minimo + 1 and unicodedata.combining(texto[fim]):
        fim -= 1
    return fim


def _cortar(texto: str, tamanho: int, sobreposicao: int, paragrafo: bool) -> list[Pedaco]:
    if not texto or not texto.strip():
        return []
    n = len(texto)
    saida: list[Pedaco] = []
    inicio = 0
    while True:
        if n - inicio <= tamanho:
            saida.append(Pedaco(len(saida), inicio, n, texto[inicio:n]))
            return saida
        fim = _ponto_de_corte(texto, inicio, tamanho, paragrafo)
        saida.append(Pedaco(len(saida), inicio, fim, texto[inicio:fim]))
        if paragrafo and texto[fim - 2:fim] == "\n\n":
            proximo = fim                          # paragrafo inteiro: sem recuo
        else:
            proximo = fim - sobreposicao if sobreposicao else fim
            while proximo < fim and not (texto[proximo - 1].isspace()
                                         and not texto[proximo].isspace()):
                proximo += 1
        inicio = proximo


def _cortar_legado(texto: str, tamanho: int, sobreposicao: int) -> list[Pedaco]:
    """O corte do `cerebro_util.trechos_de` (chunk/1.0), com posicao.

    Mantido so para duas coisas: reconstruir o texto das paginas a partir do
    `texto_fts` do v4 (a migracao) e servir de linha de base no benchmark.
    """
    texto = texto.strip()
    if not texto:
        return []
    n, inicio, saida = len(texto), 0, []
    while inicio < n:
        fim = min(inicio + tamanho, n)
        if fim < n:
            corte = texto.rfind(" ", inicio + tamanho // 2, fim)
            if corte > inicio:
                fim = corte
        saida.append(Pedaco(len(saida), inicio, fim, texto[inicio:fim]))
        if fim >= n:
            break
        inicio = max(fim - sobreposicao, inicio + 1)
    return saida


def reconstruir(pedacos: list[Pedaco]) -> str:
    """Junta os trechos tirando a sobreposicao. Tem de devolver o segmento."""
    if not pedacos:
        return ""
    texto = pedacos[0].texto
    fim = pedacos[0].fim
    for p in pedacos[1:]:
        texto += p.texto[fim - p.inicio:]
        fim = p.fim
    return texto


# ======================================================================
# 4. SEGMENTOS — a unidade de origem (pagina, slide, aba, documento)
# ======================================================================

TIPO_POR_EXTRATOR = {"pdf": "pagina", "pptx": "slide", "xlsx": "aba"}
_ROTULO_ABA = re.compile(r"^\[aba\] (.+)$")


def tipo_segmento(extrator: str | None, numero: int) -> str:
    if not numero:
        return "documento"            # extrator nao paginou: nao inventamos pagina
    return TIPO_POR_EXTRATOR.get((extrator or "").lower(), "documento")


def rotular(tipo: str, numero: int, texto_bruto: str) -> str:
    if tipo == "aba":
        primeira = (texto_bruto or "").split("\n", 1)[0].strip()
        m = _ROTULO_ABA.match(primeira)
        return m.group(1).strip() if m else f"aba {numero}"
    return {"pagina": f"p. {numero}", "slide": f"slide {numero}"}.get(tipo, "documento")


def preparar(paginas_texto, extrator: str | None):
    """[(numero, texto_bruto)] -> ([(ordem, tipo, numero, rotulo, texto)], removidas)."""
    entrada = [(int(numero or 0), bruto or "") for numero, bruto in paginas_texto]
    normalizados = [normalizar(bruto) for _, bruto in entrada]
    tipos = [tipo_segmento(extrator, numero) for numero, _ in entrada]
    removidas: list[str] = []
    if tipos and all(t == "pagina" for t in tipos):
        normalizados, removidas = remover_repetidos(normalizados)
    segmentos, ordem = [], 0
    for (numero, bruto), tipo, texto in zip(entrada, tipos, normalizados):
        if not texto:
            continue                  # unidade que normalizou para nada nao vira linha
        ordem += 1
        segmentos.append((ordem, tipo, numero, rotular(tipo, numero, bruto), texto))
    return segmentos, removidas


def sha_texto(segmentos) -> str:
    h = hashlib.sha256()
    for ordem, tipo, numero, rotulo, texto in segmentos:
        h.update(f"{ordem}\x1f{tipo}\x1f{numero}\x1f{rotulo or ''}\x1f".encode("utf-8"))
        h.update(texto.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


# ======================================================================
# 5. INDEXACAO — uma transacao por conteudo (REGRA 8). Nunca faz commit.
# ======================================================================

def indexar_conteudo(con, conteudo_id: int, paginas_texto, *, extrator: str | None,
                     versao_extrator: str | None, execucao_id: int | None = None,
                     fonte_texto: str = "extrator", agora: str | None = None,
                     politica: Politica = POLITICA) -> dict:
    """Indexa UM conteudo. Devolve {'acao', 'segmentos', 'trechos', ...}.

    acao:
      INDEXADO    entrou agora
      REINDEXADO  o texto mudou: os trechos velhos sairam, os novos entraram
      INALTERADO  texto normalizado identico e mesmas versoes: so os carimbos
                  foram atualizados; nenhuma linha de `trecho` foi tocada
      SEM_TEXTO   nao rendeu texto; fica registrado para nao ser reavaliado
    """
    segmentos, removidas = preparar(paginas_texto, extrator)
    sha = sha_texto(segmentos) if segmentos else None
    agora = agora or _agora()
    versao_chunker = politica.versao

    antes = con.execute(
        "SELECT estado, sha256_texto, versao_normalizador, versao_chunker, segmentos,"
        " trechos FROM indice_conteudo WHERE conteudo_id=?", (conteudo_id,)).fetchone()

    estado_novo = "INDEXADO" if segmentos else "SEM_TEXTO"
    if (antes is not None and antes[0] == estado_novo and antes[1] == sha
            and antes[2] == VERSAO_NORMALIZADOR and antes[3] == versao_chunker):
        con.execute("UPDATE indice_conteudo SET versao_extrator=?, indexado_em=?,"
                    " execucao_id=COALESCE(?, execucao_id) WHERE conteudo_id=?",
                    (versao_extrator, agora, execucao_id, conteudo_id))
        return {"acao": "INALTERADO", "segmentos": antes[4], "trechos": antes[5]}

    # Ordem obrigatoria: trecho antes de segmento (FK sem cascata). O gatilho
    # de DELETE em `trecho` tira cada linha do invertido, nesta transacao.
    con.execute("DELETE FROM trecho   WHERE conteudo_id=?", (conteudo_id,))
    con.execute("DELETE FROM segmento WHERE conteudo_id=?", (conteudo_id,))

    total_trechos = caracteres = 0
    for ordem, tipo, numero, rotulo, texto in segmentos:
        cur = con.execute(
            "INSERT INTO segmento (conteudo_id, ordem, tipo, numero, rotulo, caracteres)"
            " VALUES (?,?,?,?,?,?)", (conteudo_id, ordem, tipo, numero, rotulo, len(texto)))
        segmento_id = cur.lastrowid
        caracteres += len(texto)
        for p in politica.cortar(texto):
            con.execute(
                "INSERT INTO trecho (conteudo_id, segmento_id, ordinal, char_inicio,"
                " char_fim, texto) VALUES (?,?,?,?,?,?)",
                (conteudo_id, segmento_id, p.ordinal, p.inicio, p.fim, p.texto))
            total_trechos += 1

    con.execute(
        "INSERT INTO indice_conteudo (conteudo_id, estado, versao_extrator,"
        " versao_normalizador, versao_chunker, sha256_texto, segmentos, trechos,"
        " caracteres, cabecalho_removido, fonte_texto, execucao_id, indexado_em)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(conteudo_id) DO UPDATE SET estado=excluded.estado,"
        " versao_extrator=excluded.versao_extrator,"
        " versao_normalizador=excluded.versao_normalizador,"
        " versao_chunker=excluded.versao_chunker, sha256_texto=excluded.sha256_texto,"
        " segmentos=excluded.segmentos, trechos=excluded.trechos,"
        " caracteres=excluded.caracteres, cabecalho_removido=excluded.cabecalho_removido,"
        " fonte_texto=excluded.fonte_texto, execucao_id=excluded.execucao_id,"
        " indexado_em=excluded.indexado_em",
        (conteudo_id, estado_novo, versao_extrator, VERSAO_NORMALIZADOR, versao_chunker,
         sha, len(segmentos), total_trechos, caracteres,
         json.dumps(removidas, ensure_ascii=False) if removidas else None,
         fonte_texto, execucao_id, agora))

    if not segmentos:
        acao = "SEM_TEXTO"
    else:
        acao = "REINDEXADO" if antes is not None else "INDEXADO"
    return {"acao": acao, "segmentos": len(segmentos), "trechos": total_trechos,
            "caracteres": caracteres, "removidas": removidas}


def indexar_resultado(con, conteudo_id: int, resultado: dict, *,
                      execucao_id: int | None = None) -> dict:
    """Entrada do worker: o `resultado` de `extratores.extrair()` como veio.

    Chamar DEPOIS de `gravar_conteudo()` (que devolve o `conteudo_id`) e ANTES
    do commit do item — resultado, estado, evento e indice na mesma transacao.
    """
    paginas = resultado.get("paginas_texto") or []
    if not paginas and resultado.get("texto"):
        paginas = [(0, resultado["texto"])]
    return indexar_conteudo(con, conteudo_id, paginas,
                            extrator=resultado.get("extrator"),
                            versao_extrator=resultado.get("versao"),
                            execucao_id=execucao_id, fonte_texto="extrator")


def desindexar_conteudo(con, conteudo_id: int, *, agora: str | None = None) -> int:
    """Tira do indice um conteudo SEM nenhuma ocorrencia presente.

    Recusa (ValueError) se ainda houver ocorrencia viva: o indice e do
    conteudo, e um conteudo vivo em qualquer lugar continua pesquisavel.
    A linha de `indice_conteudo` fica, com estado REMOVIDO (REGRA 6: nada
    some sem rastro). Se uma ocorrencia reaparecer, o conteudo volta para
    `v_reindexar` com motivo REMOVIDO. Devolve quantos trechos sairam.
    """
    vivas = con.execute("SELECT COUNT(*) FROM arquivo WHERE conteudo_id=? AND presente=1",
                        (conteudo_id,)).fetchone()[0]
    if vivas:
        raise ValueError(f"conteudo {conteudo_id} tem {vivas} ocorrencia(s) presente(s)")
    n = con.execute("SELECT COUNT(*) FROM trecho WHERE conteudo_id=?",
                    (conteudo_id,)).fetchone()[0]
    con.execute("DELETE FROM trecho   WHERE conteudo_id=?", (conteudo_id,))
    con.execute("DELETE FROM segmento WHERE conteudo_id=?", (conteudo_id,))
    con.execute("UPDATE indice_conteudo SET estado='REMOVIDO', segmentos=0, trechos=0,"
                " indexado_em=? WHERE conteudo_id=?", (agora or _agora(), conteudo_id))
    return n


def conteudos_para_limpeza(con, carencia_dias: int, agora: str | None = None) -> list[int]:
    """Conteudos indexados cuja ULTIMA ocorrencia sumiu ha mais de `carencia_dias`.

    E a fila de uma limpeza decidida por gente — nunca um job automatico.
    """
    referencia = datetime.fromisoformat(agora) if agora else datetime.now(timezone.utc)
    if referencia.tzinfo is None:
        referencia = referencia.replace(tzinfo=timezone.utc)
    corte = (referencia - timedelta(days=int(carencia_dias))).isoformat(timespec="seconds")
    return [int(r[0]) for r in con.execute(
        "SELECT conteudo_id FROM v_conteudo_sem_ocorrencia"
        " WHERE vista_pela_ultima_vez IS NULL OR vista_pela_ultima_vez < ?"
        " ORDER BY conteudo_id", (corte,))]


# ======================================================================
# 6. CONSULTA
# ======================================================================

SQL_BUSCA = """
WITH acerto AS (
    SELECT rowid AS trecho_id, rank
    FROM busca_fts
    WHERE busca_fts MATCH :q
    ORDER BY rank
    LIMIT :teto
),
ordenado AS (
    SELECT t.conteudo_id, a.trecho_id, a.rank,
           ROW_NUMBER() OVER (PARTITION BY t.conteudo_id
                              ORDER BY a.rank, a.trecho_id) AS rn
    FROM acerto a JOIN trecho t ON t.id = a.trecho_id
    WHERE EXISTS (SELECT 1 FROM arquivo o
                   WHERE o.conteudo_id = t.conteudo_id AND o.presente = 1)
),
melhor AS (
    SELECT conteudo_id, trecho_id, rank FROM ordenado WHERE rn = 1
    ORDER BY rank LIMIT :limite
)
SELECT m.rank, m.conteudo_id,
       v.trecho_id, v.segmento_tipo, v.segmento_numero, v.segmento_rotulo,
       v.char_inicio, v.char_fim,
       v.arquivo_id, v.item_id, v.drive_id, v.caminho_rel, v.web_url,
       v.prioridade, v.eh_representante, v.modificado_em_nuvem,
       snippet(busca_fts, 0, '[', ']', ' ... ', 14) AS contexto
FROM melhor m
JOIN v_trecho_ocorrencia v ON v.trecho_id = m.trecho_id
JOIN busca_fts ON busca_fts.rowid = m.trecho_id
WHERE busca_fts MATCH :q
ORDER BY m.rank, v.prioridade ASC, v.caminho_rel
"""


def buscar(con, expressao: str, limite: int = 50, teto: int = 500) -> list:
    """A consulta canonica: o FTS5 escolhe o melhor trecho de cada conteudo;
    o SQL comum expande cada um em TODAS as ocorrencias presentes.

    `expressao` e sintaxe FTS5 (use `consulta_de_termos()` para texto livre).
    O LIMIT e sobre CONTEUDOS: um documento copiado em 30 pastas e UM
    resultado com 30 lugares, nao 30 resultados.
    """
    q = normalizar_consulta(expressao)
    return con.execute(SQL_BUSCA, {"q": q, "limite": int(limite),
                                   "teto": int(teto)}).fetchall()


def rastrear(con, trecho_id: int) -> dict | None:
    """trecho -> segmento -> conteudo -> ocorrencias presentes (REGRA 9)."""
    t = con.execute(
        "SELECT t.id, t.conteudo_id, t.ordinal, t.char_inicio, t.char_fim, t.texto,"
        " s.tipo, s.numero, s.rotulo, s.ordem, i.versao_extrator, i.versao_normalizador,"
        " i.versao_chunker, i.fonte_texto, i.execucao_id, i.indexado_em,"
        " c.quick_xor, c.tamanho_bytes, c.texto_path"
        " FROM trecho t JOIN segmento s ON s.id = t.segmento_id"
        " JOIN conteudo c ON c.id = t.conteudo_id"
        " LEFT JOIN indice_conteudo i ON i.conteudo_id = t.conteudo_id"
        " WHERE t.id = ?", (trecho_id,)).fetchone()
    if t is None:
        return None
    nomes = ["trecho_id", "conteudo_id", "ordinal", "char_inicio", "char_fim", "texto",
             "segmento_tipo", "segmento_numero", "segmento_rotulo", "segmento_ordem",
             "versao_extrator", "versao_normalizador", "versao_chunker", "fonte_texto",
             "execucao_id", "indexado_em", "quick_xor", "tamanho_bytes", "texto_path"]
    saida = dict(zip(nomes, tuple(t)))
    saida["ocorrencias"] = [dict(zip(("arquivo_id", "drive_id", "item_id", "caminho_rel",
                                      "web_url", "prioridade", "eh_representante"), tuple(r)))
                            for r in con.execute(
        "SELECT arquivo_id, drive_id, item_id, caminho_rel, web_url, prioridade,"
        " eh_representante FROM v_ocorrencia WHERE conteudo_id=?"
        " ORDER BY prioridade, caminho_rel", (saida["conteudo_id"],))]
    return saida


# ======================================================================
# 7. DDL e saude
# ======================================================================

def instrucoes_ddl(caminho: Path = DDL_V5) -> list[str]:
    """O arquivo SQL em instrucoes, uma a uma (gatilho inteiro e UMA instrucao).

    Existe porque `executescript()` do Python faz COMMIT antes de rodar: a
    migracao precisa do DDL DENTRO da transacao dela.
    """
    instrucoes, atual = [], ""
    for linha in caminho.read_text(encoding="utf-8").splitlines(keepends=True):
        if not atual and (not linha.strip() or linha.lstrip().startswith("--")):
            continue
        atual += linha
        if sqlite3.complete_statement(atual):
            instrucao = atual.strip()
            if not instrucao.upper().startswith("PRAGMA"):
                instrucoes.append(instrucao)
            atual = ""
    if atual.strip():
        raise ValueError(f"instrucao SQL incompleta no fim de {caminho.name}")
    return instrucoes


def aplicar_ddl(con) -> int:
    """Executa o DDL v5 instrucao a instrucao, na transacao corrente do chamador."""
    feitas = 0
    for instrucao in instrucoes_ddl():
        con.execute(instrucao)
        feitas += 1
    return feitas


def gravar_versoes(con, agora: str | None = None, politica: Politica = POLITICA) -> None:
    """Carimba em `meta` as versoes que ESTE codigo produz."""
    agora = agora or _agora()
    for chave, valor in (("busca.versao_esquema", "5"),
                         ("busca.versao_normalizador", VERSAO_NORMALIZADOR),
                         ("busca.versao_chunker", politica.versao),
                         ("busca.tokenizer", TOKENIZER)):
        con.execute("INSERT INTO meta (chave, valor, atualizado_em) VALUES (?,?,?)"
                    " ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor,"
                    " atualizado_em=excluded.atualizado_em", (chave, valor, agora))


def v5_ativo(con) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                       " AND name='indice_conteudo'").fetchone() is not None


def conferir_versoes(con, politica: Politica = POLITICA) -> list[str]:
    """Divergencias entre o que este codigo produz e o que o banco carimbou.

    Vazio = pode indexar. O worker deve chamar isto UMA vez, na subida, e
    recusar subir se vier algo — indexar com versao diferente da carimbada
    poria cada item novo direto em `v_reindexar`.
    """
    meta = dict(con.execute("SELECT chave, valor FROM meta WHERE chave LIKE 'busca.%'"))
    esperado = {"busca.versao_normalizador": VERSAO_NORMALIZADOR,
                "busca.versao_chunker": politica.versao,
                "busca.tokenizer": TOKENIZER}
    return [f"{k}: banco={meta.get(k)!r} codigo={v!r}"
            for k, v in esperado.items() if meta.get(k) != v]


def saude(con) -> dict:
    linha = con.execute("SELECT * FROM v_busca_saude").fetchone()
    nomes = [d[0] for d in con.execute("SELECT * FROM v_busca_saude LIMIT 0").description]
    return dict(zip(nomes, tuple(linha)))


def integridade_fts(con) -> bool:
    """`rank = 1` e obrigatorio: confere o invertido CONTRA a tabela `trecho`."""
    try:
        con.execute("INSERT INTO busca_fts(busca_fts, rank) VALUES('integrity-check', 1)")
        return True
    except sqlite3.DatabaseError:
        return False


def bytes_indice_fts(con) -> int:
    """Tamanho do indice invertido (sem o texto, que mora em `trecho`)."""
    total = 0
    for tabela, expr in (("busca_fts_data", "length(block)"),
                         ("busca_fts_idx", "length(term) + 16"),
                         ("busca_fts_docsize", "length(sz) + 8")):
        total += con.execute(f"SELECT COALESCE(SUM({expr}), 0) FROM {tabela}").fetchone()[0]
    return int(total)


# ======================================================================
# 8. DE ONDE VEM O TEXTO quando nao e o worker (migracao, reindexacao)
# ======================================================================

def ler_derivado(caminho) -> str | None:
    if not caminho:
        return None
    try:
        with gzip.open(caminho, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def carregar_indice_v4(con) -> dict[int, dict]:
    """texto_fts (v4) -> {arquivo_id: {pagina: [trecho em ordem]}}. Somente leitura."""
    existe = con.execute("SELECT 1 FROM sqlite_master WHERE name='texto_fts'").fetchone()
    if not existe:
        return {}
    saida: dict[int, dict] = {}
    for arquivo_id, pagina, idx, texto in con.execute(
            "SELECT arquivo_id, pagina, trecho_idx, texto FROM texto_fts"):
        saida.setdefault(int(arquivo_id), {}).setdefault(int(pagina or 0), []).append(
            (int(idx), texto))
    return saida


def paginas_do_indice_v4(por_pagina: dict, tamanho: int, sobreposicao: int):
    """Reconstroi o texto exato de cada pagina a partir dos trechos do v4.

    O chunk/1.0 e deterministico: sabendo o tamanho de cada trecho, sabe-se
    onde o seguinte comecou. Reconstruido, o texto e CORTADO DE NOVO pela
    mesma regra; so vale se sair trecho a trecho identico ao que estava no
    indice. Devolve (paginas, None) ou (None, motivo).
    """
    paginas = []
    for numero in sorted(por_pagina):
        trechos = [t for _, t in sorted(por_pagina[numero])]
        texto, inicio, fim = trechos[0], 0, len(trechos[0])
        for trecho in trechos[1:]:
            proximo = max(fim - sobreposicao, inicio + 1)
            texto += trecho[fim - proximo:]
            inicio, fim = proximo, proximo + len(trecho)
        if [p.texto for p in _cortar_legado(texto, tamanho, sobreposicao)] != trechos:
            return None, f"pagina {numero}: recorte nao reproduz o indice v4"
        paginas.append((numero, texto))
    return paginas, None


def paginas_do_derivado(texto: str, extrator: str | None):
    """Derivado gz -> paginas.

    Desde pdf/1.2, pptx/1.2 e xlsx/1.1 o extrator TERMINA cada pagina, slide
    ou aba com form feed (U+000C), na posicao do seu numero (unidade sem
    texto e vazia entre dois `\\f`). O `\\f` nunca aparece dentro da unidade
    (o extrator o troca por quebra de linha), entao separar ali e exato: sai
    a mesma lista `paginas_texto` que o extrator entregou.

    Derivado antigo, sem `\\f`: so a planilha guarda a fronteira
    (`[aba] nome`). PDF e PPTX antigos uniam as paginas por linha em branco,
    que tambem aparece DENTRO da pagina — separar ali seria inventar
    fronteira. Viram um segmento `documento`, honesto.

    So formato paginado (TIPO_POR_EXTRATOR) e separado: um `.txt` ou `.log`
    com form feed de impressora continua um documento so, como o extrator
    de texto o indexa.
    """
    if not texto or not texto.strip():
        return []
    paginado = (extrator or "").lower() in TIPO_POR_EXTRATOR
    if paginado and "\f" in texto:
        unidades = texto.split("\f")
        if unidades and unidades[-1] == "":
            unidades.pop()                        # o terminador da ultima unidade
        return [(i, u) for i, u in enumerate(unidades, start=1) if u.strip()]
    if (extrator or "").lower() == "xlsx" and texto.startswith("[aba] "):
        partes = texto.split("\n\n[aba] ")
        return [(i, p if i == 1 else "[aba] " + p) for i, p in enumerate(partes, start=1)]
    return [(0, texto)]


def mesmo_texto(paginas, derivado: str, extrator: str | None) -> bool:
    """O texto reconstruido do indice v4 e o mesmo do derivado?

    Derivado antigo junta as unidades com linha em branco; o novo (pdf/1.2,
    pptx/1.2, xlsx/1.1) termina cada unidade com `\\f`. Comparar so pela
    juncao antiga acusava "diverge do derivado" em todo derivado novo,
    embora o texto fosse o mesmo.
    """
    if "\n\n".join(t for _, t in paginas) == derivado:
        return True
    if "\f" in derivado:
        return ([t for _, t in paginas if t.strip()]
                == [t for _, t in paginas_do_derivado(derivado, extrator)])
    return False


def fonte_do_conteudo(conteudo, indice_v4: dict, arquivos: list[int],
                      tamanho_v4: int, sobreposicao_v4: int):
    """Escolhe o texto de um conteudo para (re)indexar sem o extrator.

    Ordem: o indice v4 reconstruido e conferido (preserva pagina/slide/aba)
    -> o derivado gz (perde a pagina, exceto em planilha) -> nada.
    Devolve (paginas, fonte, detalhe).
    """
    derivado = ler_derivado(conteudo["texto_path"])
    candidatos = [a for a in arquivos if a in indice_v4]
    motivos = []
    for arquivo_id in candidatos:
        paginas, motivo = paginas_do_indice_v4(indice_v4[arquivo_id], tamanho_v4,
                                               sobreposicao_v4)
        if paginas is None:
            motivos.append(f"arquivo {arquivo_id}: {motivo}")
            continue
        if derivado is not None and not mesmo_texto(paginas, derivado,
                                                     conteudo["extrator"]):
            motivos.append(f"arquivo {arquivo_id}: diverge do derivado")
            continue
        return paginas, "indice_v4", f"arquivo {arquivo_id}"
    if derivado is not None:
        return (paginas_do_derivado(derivado, conteudo["extrator"]), "derivado",
                "; ".join(motivos) or "sem trechos no indice v4")
    return None, None, "; ".join(motivos) or "sem derivado e sem indice v4"


# ======================================================================
# 9. BENCHMARK — reproduzivel, sobre uma COPIA temporaria do banco
# ======================================================================

POLITICAS_BENCH = [
    ("A  chunk/1.0 1500/150 (atual)", Politica(1500, 150, "legado")),
    ("B  chunk/2.0  800/100", Politica(800, 100, "janela")),
    ("C  chunk/2.0 1500/150", Politica(1500, 150, "janela")),
    ("D  chunk/2.0 2500/250", Politica(2500, 250, "janela")),
    ("E  paragrafo 1500/0", Politica(1500, 0, "paragrafo")),
]

# Consultas escritas A MAO a partir do vocabulario real da amostra (banco de
# 2026-10-04). (texto livre, conteudo_id alvo, frase-alvo presente no alvo).
# Se o banco mudar e a frase-alvo nao estiver mais no alvo, o bench acusa a
# consulta como invalida em vez de medir errado.
CONSULTAS_BENCH = [
    ("certidao negativa debitos tributarios", 1, "CERTIDÃO NEGATIVA DE DÉBITOS"),
    ("tubo de concreto data de aplicacao", 2, "TUBO DE CONCRETO"),
    ("aterro proctor cota projeto", 3, "Aterro (100% Proctor)"),
    ("faixa de dominio projetada RD-101 azimute", 4,
     "faixa de domínio projetada da Rodovia RD-101"),
    ("perfuracao material 3a categoria", 5, "Perfuração em material de 3ª categoria"),
    ("trecho sem execucao de balancim", 6, "Trecho sem Execução de Balancim"),
    ("gerenciamento de residuos efluentes condicionante", 7,
     "Gerenciamento de resíduos do sistema de efluentes"),
    ("memorial descritivo matricula Cidade Exemplo", 8, "Matrícula: 00.000"),
    ("certidao positiva efeito de negativa", 9, "certidão positiva tem efeito de negativa"),
    ("nao conformidades emitidas pelo Programa no periodo", 10,
     "Não conformidades emitidas pelo Programa no período"),
    ("sinalizacao horizontal em curvas", 11, "sinalização horizontal em curvas"),
    ("eventograma padrao criterios de pagamento", 12,
     "Eventograma Padrão e Critérios de Pagamento"),
    ("outorgas uso de recursos hidricos licenca ambiental", 14,
     "outorgas de uso de recursos hídricos"),
    ("parecer tecnico avaliacao de conformidade ambiental", 15,
     "Avaliação de conformidade ambiental"),
    ("atualizacao do eventograma consolidacao do projeto executivo", 16,
     "consolidação do Projeto Executivo"),
    ("rinex marco M-05", 18, "MARCO M-05"),
    ("preco unitario SICRO escavacao drenagem", 19, "preço unitário SICRO"),
    ("relatorio de estudos geotecnicos revisado", 20,
     "Relatório de Estudos Geotécnicos Revisado"),
    ("desgaste por abrasao los angeles", 21, "DESGASTE POR ABRASÃO LOS ANGELES"),
    ("sitio arqueologico FCA", 22, "Sítio Arqueológico"),
    ("certificado de calibracao", 23, "Certificado de Calibração"),
    ("boletim de sondagem jazida", 25, "BOLETIM DE SONDAGEM"),
    ("decreto de utilidade publica desapropriacao", 28, "Decreto de Utilidade Pública"),
    ("alteracao de tracado interferencias", 29, "alteração de traçado"),
    ("resina acrilica emulsionada", 30, "RESINA ACRÍLICA EMULSIONADA"),
]


def dobrar(texto: str) -> str:
    """Caixa e acento fora, espacos colapsados — para COMPARAR, nunca guardar."""
    t = unicodedata.normalize("NFKD", normalizar(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).lower().strip()


def _frases_partidas(con, palavras_por_frase: int = 6, passo: int = 40) -> tuple[int, int]:
    """Amostra sistematica: a cada `passo` palavras de cada segmento, uma frase
    de `palavras_por_frase` palavras. Conta as que NAO cabem inteiras em
    nenhum trecho daquele segmento (frase partida = busca de frase falha)."""
    total = partidas = 0
    for seg_id, in con.execute("SELECT id FROM segmento ORDER BY id").fetchall():
        trechos = con.execute("SELECT char_inicio, char_fim FROM trecho WHERE segmento_id=?"
                              " ORDER BY ordinal", (seg_id,)).fetchall()
        texto = _texto_segmento(con, seg_id)
        palavras = [m.span() for m in re.finditer(r"\S+", texto)]
        for i in range(0, max(0, len(palavras) - palavras_por_frase + 1), passo):
            a, b = palavras[i][0], palavras[i + palavras_por_frase - 1][1]
            total += 1
            if not any(ini <= a and b <= fim for ini, fim in trechos):
                partidas += 1
    return partidas, total


def _texto_segmento(con, segmento_id: int) -> str:
    linhas = con.execute("SELECT char_inicio, char_fim, texto FROM trecho"
                         " WHERE segmento_id=? ORDER BY ordinal", (segmento_id,)).fetchall()
    return reconstruir([Pedaco(i, a, b, t) for i, (a, b, t) in enumerate(linhas)])


def _remover_v5(con) -> None:
    for tipo, nome in (("TRIGGER", "trg_trecho_ai"), ("TRIGGER", "trg_trecho_ad"),
                       ("TRIGGER", "trg_trecho_au"), ("VIEW", "v_trecho_ocorrencia"),
                       ("VIEW", "v_busca_saude"), ("VIEW", "v_reindexar"),
                       ("VIEW", "v_busca_cobertura"), ("VIEW", "v_conteudo_sem_ocorrencia"),
                       ("TABLE", "busca_fts"), ("TABLE", "trecho"), ("TABLE", "segmento"),
                       ("TABLE", "indice_conteudo")):
        con.execute(f"DROP {tipo} IF EXISTS {nome}")


def amostra_do_banco(con, tamanho_v4: int = 1500, sobreposicao_v4: int = 150) -> list:
    """Os conteudos com texto e o texto de cada um (indice v4 > derivado)."""
    indice = carregar_indice_v4(con)
    amostra = []
    for c in con.execute("SELECT id, extrator, versao_extrator, texto_path, representante_id"
                         " FROM conteudo WHERE estado IN ('EXTRAIDO','PARCIAL')"
                         " ORDER BY id").fetchall():
        c = dict(zip(("id", "extrator", "versao_extrator", "texto_path",
                      "representante_id"), tuple(c)))
        arquivos = [c["representante_id"]] + [r[0] for r in con.execute(
            "SELECT id FROM arquivo WHERE conteudo_id=? AND id IS NOT ? ORDER BY id",
            (c["id"], c["representante_id"]))]
        paginas, fonte, _ = fonte_do_conteudo(c, indice, [a for a in arquivos if a],
                                              tamanho_v4, sobreposicao_v4)
        if paginas:
            amostra.append((c, paginas, fonte))
    return amostra


def bench(banco: Path, pasta: Path | None = None, saida_json: Path | None = None) -> list[dict]:
    pasta_tmp = Path(tempfile.mkdtemp(prefix="cerebro_bench_", dir=pasta))
    copia = pasta_tmp / "bench.db"
    try:
        origem = sqlite3.connect(f"file:{banco.as_posix()}?mode=ro", uri=True)
        destino = sqlite3.connect(str(copia))
        t0 = time.perf_counter()
        origem.backup(destino)
        origem.close()
        print(f"copia do banco: {copia} ({copia.stat().st_size / 1024 ** 2:.0f} MB,"
              f" {time.perf_counter() - t0:.1f} s)")
        destino.close()
        con = sqlite3.connect(str(copia), isolation_level=None)
        con.execute("PRAGMA foreign_keys=ON")

        amostra = amostra_do_banco(con)
        chars = sum(len(t) for _, pags, _ in amostra for _, t in pags)
        print(f"amostra: {len(amostra)} conteudos, {chars:,} caracteres brutos, fontes "
              f"{dict(Counter(f for _, _, f in amostra))}")

        textos = {c["id"]: dobrar("\n".join(t for _, t in pags)) for c, pags, _ in amostra}
        validas = [(q, alvo, frase) for q, alvo, frase in CONSULTAS_BENCH
                   if alvo in textos and dobrar(frase) in textos[alvo]]
        invalidas = [q for q, alvo, frase in CONSULTAS_BENCH
                     if (q, alvo, frase) not in validas]
        print(f"consultas validas: {len(validas)}/{len(CONSULTAS_BENCH)}"
              + (f"  invalidas: {invalidas}" if invalidas else ""))

        resultados = []
        for rotulo, politica in POLITICAS_BENCH:
            con.execute("BEGIN")
            _remover_v5(con)
            aplicar_ddl(con)
            gravar_versoes(con, politica=politica)
            con.execute("COMMIT")
            inicio = time.perf_counter()
            con.execute("BEGIN")
            for c, paginas, fonte in amostra:
                indexar_conteudo(con, c["id"], paginas, extrator=c["extrator"],
                                 versao_extrator=c["versao_extrator"], fonte_texto=fonte,
                                 politica=politica)
            con.execute("COMMIT")
            segundos = time.perf_counter() - inicio

            trechos = con.execute("SELECT COUNT(*) FROM trecho").fetchone()[0]
            guardado = con.execute("SELECT SUM(length(texto)) FROM trecho").fetchone()[0]
            normal = con.execute("SELECT SUM(caracteres) FROM segmento").fetchone()[0]
            maior = con.execute("SELECT MAX(length(texto)) FROM trecho").fetchone()[0]
            meio_palavra = 0
            for seg_id, in con.execute("SELECT id FROM segmento").fetchall():
                texto = _texto_segmento(con, seg_id)
                for (ini,) in con.execute("SELECT char_inicio FROM trecho WHERE"
                                          " segmento_id=? AND char_inicio>0", (seg_id,)):
                    if not texto[ini - 1].isspace() and not texto[ini].isspace():
                        meio_palavra += 1
            partidas, frases = _frases_partidas(con)

            a1 = a3 = trecho_certo = frase_ok = 0
            rr = 0.0
            ctx = []
            fora_do_topo = []
            for consulta, alvo, frase in validas:
                linhas = buscar(con, consulta_de_termos(consulta), limite=10)
                ordem = list(dict.fromkeys(l[1] for l in linhas))
                if alvo in ordem:
                    pos = ordem.index(alvo) + 1
                    rr += 1.0 / pos
                    a1 += pos == 1
                    a3 += pos <= 3
                    if pos > 1:
                        fora_do_topo.append(f"{consulta!r}: alvo {alvo} em {pos}o,"
                                            f" 1o foi {ordem[0]}")
                    tid = next(l[2] for l in linhas if l[1] == alvo)
                    texto = con.execute("SELECT texto FROM trecho WHERE id=?",
                                        (tid,)).fetchone()[0]
                    trecho_certo += dobrar(frase) in dobrar(texto)
                    ctx.append(len(texto))
                exata = buscar(con, '"' + normalizar_consulta(frase).replace('"', '""') + '"',
                               limite=50)
                frase_ok += alvo in {l[1] for l in exata}
            n = len(validas) or 1
            r = {
                "politica": rotulo, "versao": politica.versao, "trechos": trechos,
                "trechos_por_conteudo": round(trechos / len(amostra), 1),
                "maior_trecho": maior,
                "redundancia_pct": round(100.0 * (guardado - normal) / normal, 1),
                "indice_fts_kb": round(bytes_indice_fts(con) / 1024),
                "texto_trecho_kb": round(guardado / 1024),
                "segundos": round(segundos, 2),
                "inicio_meio_palavra": meio_palavra,
                "frases_partidas": f"{partidas}/{frases}",
                "frases_partidas_pct": round(100.0 * partidas / max(frases, 1), 2),
                "acerto_1": f"{a1}/{len(validas)}", "acerto_3": f"{a3}/{len(validas)}",
                "mrr": round(rr / n, 3),
                "trecho_certo": f"{trecho_certo}/{len(validas)}",
                "frase_exata": f"{frase_ok}/{len(validas)}",
                "contexto_medio": round(sum(ctx) / len(ctx)) if ctx else 0,
                "saude_ok": all(v == 0 for k, v in saude(con).items() if k != "a_reindexar"),
                "integridade_fts": integridade_fts(con),
                "fora_do_topo": fora_do_topo,
            }
            resultados.append(r)
            print(f"  {rotulo}: {trechos} trechos, {segundos:.2f} s")
            for falha in fora_do_topo:
                print(f"      fora do 1o lugar: {falha}")
        con.close()

        cab = ["politica", "trechos", "trechos_por_conteudo", "maior_trecho",
               "redundancia_pct", "indice_fts_kb", "texto_trecho_kb", "segundos",
               "inicio_meio_palavra", "frases_partidas", "acerto_1", "acerto_3", "mrr",
               "trecho_certo", "frase_exata", "contexto_medio", "saude_ok", "integridade_fts"]
        print("\n| " + " | ".join(cab) + " |")
        print("|" + "---|" * len(cab))
        for r in resultados:
            print("| " + " | ".join(str(r[k]) for k in cab) + " |")
        if saida_json:
            saida_json.write_text(json.dumps(resultados, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        return resultados
    finally:
        shutil.rmtree(pasta_tmp, ignore_errors=True)
        print(f"\ncopia temporaria apagada: {not pasta_tmp.exists()}")


# ======================================================================
# 10. LINHA DE COMANDO
# ======================================================================

def _abrir_ro(banco: Path):
    con = sqlite3.connect(f"file:{banco.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def main(argv=None) -> int:
    for saida in (sys.stdout, sys.stderr):
        try:
            saida.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Camada de busca (schema v5)")
    sub = ap.add_subparsers(dest="comando", required=True)
    b = sub.add_parser("buscar", help="consulta o indice (somente leitura)")
    b.add_argument("consulta")
    b.add_argument("--banco", required=True, type=Path)
    b.add_argument("--fts", action="store_true",
                   help="a consulta ja e sintaxe FTS5 (NEAR, OR, frase...)")
    b.add_argument("--limite", type=int, default=20)
    m = sub.add_parser("bench", help="mede as politicas de corte numa COPIA do banco")
    m.add_argument("--banco", required=True, type=Path,
                   help="banco de origem; aberto SOMENTE LEITURA e copiado para %%TEMP%%")
    m.add_argument("--tmp", type=Path, default=None, help="pasta para a copia temporaria")
    m.add_argument("--json", type=Path, default=None, help="grava os numeros neste arquivo")
    args = ap.parse_args(argv)

    if args.comando == "bench":
        bench(args.banco, args.tmp, args.json)
        return 0

    con = _abrir_ro(args.banco)
    if not v5_ativo(con):
        print("este banco ainda nao tem o schema v5 (scripts\\05_migrar_v5.py)")
        return 2
    expressao = args.consulta if args.fts else consulta_de_termos(args.consulta)
    atual = None
    for linha in buscar(con, expressao, limite=args.limite):
        if linha["conteudo_id"] != atual:
            atual = linha["conteudo_id"]
            print(f"\n[{atual}] {linha['segmento_rotulo']}  rank={linha['rank']:.2f}")
            print(f"    {(linha['contexto'] or '').replace(chr(10), ' / ')}")
        print(f"      P{linha['prioridade']} {linha['caminho_rel']}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
