"""
cerebro_util — utilitarios comuns do Cerebro Documental.

Regras que este modulo protege:
  1. A raiz do SharePoint e READ-ONLY. `assert_escrita_permitida()` levanta
     PermissionError se alguem tentar escrever la dentro.
  2. Caminhos do SharePoint estouram MAX_PATH (260). Use sempre `lp()`.
  3. Arquivo "online" (OneDrive sob demanda) so pode ser hidratado dentro da
     politica: `pode_hidratar()` decide, `garantir_local()` executa,
     `liberar_espaco()` devolve para a nuvem SEM tocar no conteudo.

Python 3.12+, sem dependencia externa alem de PyYAML.
"""

from __future__ import annotations

import ctypes
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# ----------------------------------------------------------------------
# Caminhos do projeto
# ----------------------------------------------------------------------

RAIZ_REPO = Path(__file__).resolve().parents[1]
CONFIG_PADRAO = RAIZ_REPO / "config" / "projeto.yaml"


def importar_script(nome: str):
    """Importa um script numerado de `scripts/` como modulo.

    `import 02_classificar` nao existe em Python (o nome comeca com digito).
    Esta funcao carrega o arquivo pelo caminho e guarda em `sys.modules`, de
    modo que um script possa REUSAR a logica de outro em vez de reimplementa-la
    ou de chamar um subprocesso.
    """
    import importlib.util

    chave = f"cerebro_script_{nome}"
    if chave in sys.modules:
        return sys.modules[chave]
    caminho = RAIZ_REPO / "scripts" / f"{nome}.py"
    if not caminho.exists():
        raise FileNotFoundError(f"script inexistente: {caminho}")
    spec = importlib.util.spec_from_file_location(chave, caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[chave] = modulo
    try:
        spec.loader.exec_module(modulo)
    except BaseException:
        sys.modules.pop(chave, None)
        raise
    return modulo


def carregar_config(caminho: Path | str | None = None) -> dict:
    """Le config/projeto.yaml e devolve o dicionario ja validado no basico."""
    caminho = Path(caminho) if caminho else CONFIG_PADRAO
    with open(caminho, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    for obrigatorio in ("raiz", "banco", "derivados", "logs"):
        if not cfg.get(obrigatorio):
            raise ValueError(f"config sem a chave obrigatoria: {obrigatorio}")

    raiz = Path(cfg["raiz"])
    if not raiz.exists():
        raise FileNotFoundError(f"raiz configurada nao existe: {raiz}")

    cfg["_raiz_path"] = raiz
    cfg["_banco_path"] = Path(cfg["banco"])
    cfg["_derivados_path"] = Path(cfg["derivados"])
    cfg["_logs_path"] = Path(cfg["logs"])
    cfg["_saida_path"] = Path(cfg.get("saida") or (RAIZ_REPO / "output"))
    cfg["_config_path"] = Path(caminho)
    return cfg


# ----------------------------------------------------------------------
# Maquina de estados (documentada em sql/schema_v2.sql)
# ----------------------------------------------------------------------

ESTADOS = (
    "INVENTORIED", "QUEUED", "WAITING_HYDRATION", "HYDRATING", "READY",
    "EXTRACTING", "EXTRACTED", "COMPLETED", "PARTIAL", "OCR_REQUIRED",
    "UNSUPPORTED", "DUPLICATE", "SKIPPED", "CHANGED", "MISSING",
    "ERROR_RETRYABLE", "ERROR_FINAL",
)
# Estados que NAO precisam de mais nada do worker.
ESTADOS_TERMINAIS = ("COMPLETED", "DUPLICATE", "UNSUPPORTED", "SKIPPED",
                     "ERROR_FINAL", "MISSING", "OCR_REQUIRED", "PARTIAL")
# Estados que voltam para a fila.
ESTADOS_NA_FILA = ("QUEUED", "CHANGED", "READY", "WAITING_HYDRATION",
                   "ERROR_RETRYABLE")
# Se o processo morrer aqui, na proxima subida volta para a fila.
ESTADOS_ORFAOS = ("HYDRATING", "EXTRACTING", "EXTRACTED")


# ----------------------------------------------------------------------
# Long path (MAX_PATH)
# ----------------------------------------------------------------------

def lp(caminho: Path | str) -> str:
    """Caminho com prefixo extended-length do Windows (`\\\\?\\`)."""
    texto = str(caminho)
    if os.name != "nt":
        return texto
    if texto.startswith("\\\\?\\"):
        return texto
    texto = os.path.abspath(texto)
    if texto.startswith("\\\\"):
        return "\\\\?\\UNC\\" + texto.lstrip("\\")
    return "\\\\?\\" + texto


# ----------------------------------------------------------------------
# Guarda de escrita: nada e escrito dentro da raiz do SharePoint
# ----------------------------------------------------------------------

def assert_escrita_permitida(destino: Path | str, raiz_proibida: Path | str) -> None:
    """Levanta PermissionError se `destino` cair dentro de `raiz_proibida`."""
    d = Path(os.path.abspath(str(destino))).resolve(strict=False)
    r = Path(os.path.abspath(str(raiz_proibida))).resolve(strict=False)
    try:
        d.relative_to(r)
    except ValueError:
        return
    raise PermissionError(f"ESCRITA BLOQUEADA: {d} esta dentro da raiz read-only {r}")


# ----------------------------------------------------------------------
# Atributos / OneDrive Arquivos Sob Demanda
# ----------------------------------------------------------------------

FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def esta_online(atributos: int) -> bool:
    """True = conteudo so na nuvem. Abrir dispara download."""
    return bool(atributos & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


def esta_fixado(atributos: int) -> bool:
    return bool(atributos & FILE_ATTRIBUTE_PINNED)


def atributos_de(caminho: Path | str) -> int:
    """Le os atributos direto do Windows (funciona com caminho longo).

    `restype` explicito e obrigatorio: sem ele o ctypes devolve int COM SINAL,
    e o -1 de "arquivo nao existe" nunca seria igual a 0xFFFFFFFF. O erro
    passava despercebido e um arquivo inexistente era lido como se tivesse
    todos os atributos ligados — inclusive `online`.
    """
    if os.name != "nt":
        return 0
    funcao = ctypes.windll.kernel32.GetFileAttributesW
    funcao.argtypes = [ctypes.c_wchar_p]
    funcao.restype = ctypes.c_uint32
    valor = funcao(lp(caminho))
    if valor == _INVALID_FILE_ATTRIBUTES:
        raise FileNotFoundError(
            ctypes.get_last_error(), f"GetFileAttributesW falhou: {caminho}")
    return int(valor)


def _definir_atributos(caminho: Path | str, atributos: int) -> None:
    if os.name != "nt":
        return
    ok = ctypes.windll.kernel32.SetFileAttributesW(lp(caminho), ctypes.c_uint32(atributos))
    if not ok:
        raise OSError(ctypes.get_last_error(), f"SetFileAttributesW falhou: {caminho}")


def espaco_livre_gb(caminho: Path | str = "C:\\") -> float:
    total, usado, livre = shutil.disk_usage(str(caminho))
    return livre / (1024 ** 3)


# --- erros transitorios -------------------------------------------------
# Falha de nuvem/rede NAO e culpa do documento: e falta de disponibilidade.
CODIGOS_TRANSITORIOS = {
    21, 53, 59, 64, 67, 121, 170, 258, 995, 1223, 1231, 1232, 1450, 1453,
    # faixa de erros de cloud files (ERROR_CLOUD_FILE_*)
    *range(360, 405),
}


def erro_transitorio(exc: BaseException) -> bool:
    """True quando vale a pena tentar de novo mais tarde."""
    if isinstance(exc, (TimeoutError, ConnectionError, InterruptedError)):
        return True
    codigo = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if codigo in CODIGOS_TRANSITORIOS:
        return True
    texto = str(exc).lower()
    return any(p in texto for p in ("cloud", "nuvem", "not available", "nao esta disponivel",
                                    "não está disponível", "onedrive", "rede", "network"))


# --- hidratacao ---------------------------------------------------------

def pode_hidratar(cfg: dict, tamanho_bytes: int, bytes_hidratados_sessao: int) -> tuple[bool, str]:
    """Decide se o proximo arquivo pode ser baixado da nuvem.

    Tres travas independentes: politica, orcamento da sessao e disco livre.
    """
    nuvem = cfg.get("nuvem") or {}
    if not nuvem.get("permitir_hidratacao", False):
        return False, "hidratacao desligada em config (nuvem.permitir_hidratacao)"

    teto_gb = float(nuvem.get("max_hidratacao_gb", 15))
    if (bytes_hidratados_sessao + tamanho_bytes) > teto_gb * 1024 ** 3:
        return False, f"orcamento de hidratacao da sessao esgotado ({teto_gb} GB)"

    minimo = float(nuvem.get("espaco_livre_minimo_gb", 20))
    livre = espaco_livre_gb(cfg["_banco_path"].anchor or "C:\\")
    if livre - (tamanho_bytes / 1024 ** 3) < minimo:
        return False, f"espaco livre chegaria a menos de {minimo} GB (livre agora: {livre:.1f} GB)"

    maximo_arquivo = float(nuvem.get("tamanho_max_arquivo_gb", 2))
    if tamanho_bytes > maximo_arquivo * 1024 ** 3:
        return False, f"arquivo maior que o teto por arquivo ({maximo_arquivo} GB)"

    return True, ""


def garantir_local(caminho: Path | str, tempo_limite_s: float = 900.0) -> int:
    """Traz o conteudo para o disco lendo o arquivo (o OneDrive baixa sob demanda).

    Somente leitura: nao altera conteudo, nome, data nem local do original.
    Devolve os bytes lidos. Levanta OSError se a nuvem nao responder.
    """
    inicio = time.monotonic()
    lidos = 0
    with open(lp(caminho), "rb") as fh:
        while True:
            if time.monotonic() - inicio > tempo_limite_s:
                raise TimeoutError(f"hidratacao passou de {tempo_limite_s:.0f}s: {caminho}")
            pedaco = fh.read(4 * 1024 * 1024)
            if not pedaco:
                break
            lidos += len(pedaco)
    return lidos


def liberar_espaco(caminho: Path | str) -> bool:
    """Devolve o arquivo para 'somente na nuvem' — o mesmo que o Explorer faz
    em 'Liberar espaco'.

    NAO apaga, NAO move, NAO renomeia, NAO altera o conteudo: apenas marca o
    atributo UNPINNED e limpa PINNED. O OneDrive desidrata o cache local.
    Devolve True se o atributo foi aplicado.
    """
    if os.name != "nt":
        return False
    atuais = atributos_de(caminho)
    novos = (atuais | FILE_ATTRIBUTE_UNPINNED) & ~FILE_ATTRIBUTE_PINNED
    _definir_atributos(caminho, novos)
    return True


# ----------------------------------------------------------------------
# Normalizacao de nomes e classificacao
# ----------------------------------------------------------------------

_TOKENS_RUIDO = re.compile(
    r"\b("
    r"rev(?:isao|isão)?\s*\d*[a-z]?|r\d{1,2}|v\d{1,2}(?:\.\d+)?|"
    r"vers(?:ao|ão)\s*\d*|final(?:izado)?|copia|cópia|copy|"
    r"assinado|ass|novo|nova|atualizado|atualizada|ok|"
    r"\d{1,2}[-_]?\d{1,2}[-_]?\d{2,4}"
    r")\b"
)


def sem_acento(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalizar_texto(texto: str) -> str:
    """minusculo, sem acento, sem pontuacao — para comparar nomes de pasta."""
    base = sem_acento(str(texto)).lower()
    base = re.sub(r"[^a-z0-9]+", " ", base)
    return re.sub(r"\s+", " ", base).strip()


def normalizar_nome(nome_arquivo: str) -> str:
    """Nome canonico para agrupar variantes do mesmo documento.

    'P.1.1 - Relatório Rev 02 (cópia).pdf' -> 'p 1 1 relatorio'
    Heuristica: LEVANTA candidatos, nunca conclui.
    """
    base = Path(nome_arquivo).stem
    base = sem_acento(base).lower()
    base = re.sub(r"\(\s*\d+\s*\)", " ", base)
    base = re.sub(r"[^a-z0-9]+", " ", base)
    base = _TOKENS_RUIDO.sub(" ", base)
    return re.sub(r"\s+", " ", base).strip()


def categoria_de(ext: str, mapa_categorias: dict) -> str:
    ext = ext.lower()
    for categoria, extensoes in (mapa_categorias or {}).items():
        if ext in extensoes:
            return categoria
    return "outro"


def combina_padrao(nome: str, padroes: list[str]) -> bool:
    import fnmatch

    alvo = nome.lower()
    return any(fnmatch.fnmatch(alvo, p.lower()) for p in padroes or [])


# ----------------------------------------------------------------------
# Hash e derivados
# ----------------------------------------------------------------------

def sha256_arquivo(caminho: Path | str, bloco: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(lp(caminho), "rb") as fh:
        for pedaco in iter(lambda: fh.read(bloco), b""):
            h.update(pedaco)
    return h.hexdigest()


def caminho_derivado(cfg: dict, arquivo_id: int, tipo: str) -> Path:
    """derivados/texto/000/123.txt.gz  |  derivados/perfil/000/123.json

    Fragmenta em pastas de 1.000 para nao criar diretorio com milhoes de itens.
    """
    sufixo = {"texto": ".txt.gz", "perfil": ".json"}[tipo]
    pasta = cfg["_derivados_path"] / tipo / f"{arquivo_id // 1000:04d}"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta / f"{arquivo_id}{sufixo}"


def gravar_texto(caminho: Path, texto: str) -> int:
    """Grava comprimido. Devolve os bytes em disco."""
    with gzip.open(caminho, "wt", encoding="utf-8", compresslevel=6) as fh:
        fh.write(texto)
    return caminho.stat().st_size


def ler_texto(caminho: Path | str) -> str:
    with gzip.open(caminho, "rt", encoding="utf-8") as fh:
        return fh.read()


def gravar_perfil(caminho: Path, perfil: dict) -> int:
    caminho.write_text(json.dumps(perfil, ensure_ascii=False, indent=1), encoding="utf-8")
    return caminho.stat().st_size


def trechos_de(texto: str, tamanho: int = 1500, sobreposicao: int = 150):
    """Quebra o texto em trechos para o FTS, sem cortar palavra no meio."""
    texto = texto.strip()
    if not texto:
        return
    inicio = 0
    n = len(texto)
    while inicio < n:
        fim = min(inicio + tamanho, n)
        if fim < n:
            corte = texto.rfind(" ", inicio + tamanho // 2, fim)
            if corte > inicio:
                fim = corte
        yield texto[inicio:fim]
        if fim >= n:
            return
        inicio = max(fim - sobreposicao, inicio + 1)


# ----------------------------------------------------------------------
# Tempo
# ----------------------------------------------------------------------

def agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def agora_sql() -> str:
    """Formato comparavel com datetime('now') do SQLite (UTC, sem fuso)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def daqui_a_sql(segundos: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=segundos)).strftime(
        "%Y-%m-%d %H:%M:%S")


def ts_para_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def backoff_segundos(tentativa: int, base: float = 60.0, teto: float = 6 * 3600) -> float:
    """1a falha: 1 min. 2a: 4 min. 3a: 9 min... com teto de 6 h."""
    return min(base * (tentativa ** 2), teto)


# ----------------------------------------------------------------------
# Banco
# ----------------------------------------------------------------------

def conectar(caminho_banco: Path | str, raiz_proibida: Path | str | None = None) -> sqlite3.Connection:
    caminho_banco = Path(caminho_banco)
    if raiz_proibida:
        assert_escrita_permitida(caminho_banco, raiz_proibida)
    caminho_banco.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(caminho_banco), timeout=120.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")      # sobrevive a queda de energia
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA busy_timeout=120000")
    return con


def tem_fts5() -> bool:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def abrir_execucao(con: sqlite3.Connection, etapa: str, escopo: str, argumentos: dict) -> int:
    cur = con.execute(
        "INSERT INTO execucao (etapa, escopo, argumentos, iniciada_em, status, host) "
        "VALUES (?,?,?,?,'em_andamento',?)",
        (etapa, escopo, json.dumps(argumentos, ensure_ascii=False, default=str), agora_iso(),
         os.environ.get("COMPUTERNAME", "")),
    )
    con.commit()
    return int(cur.lastrowid)


def fechar_execucao(con: sqlite3.Connection, execucao_id: int, status: str,
                    contadores: dict, duracao_s: float, observacao: str = "") -> None:
    con.execute(
        "UPDATE execucao SET encerrada_em=?, status=?, diretorios_vistos=?, "
        "arquivos_vistos=?, arquivos_novos=?, arquivos_alterados=?, "
        "arquivos_sumidos=?, erros=?, bytes_processados=?, bytes_hidratados=?, "
        "duracao_s=?, observacao=? WHERE id=?",
        (agora_iso(), status,
         contadores.get("diretorios", 0), contadores.get("arquivos", 0),
         contadores.get("novos", 0), contadores.get("alterados", 0),
         contadores.get("sumidos", 0), contadores.get("erros", 0),
         contadores.get("bytes", 0), contadores.get("bytes_hidratados", 0),
         round(duracao_s, 2), observacao, execucao_id),
    )
    con.commit()


# ----------------------------------------------------------------------
# Conteudo x ocorrencia (schema v4) — a deduplicacao num lugar so
#
# Quem chama: worker (marcar_ausente, cobrir_por_conteudo, reeleicao na
# subida), 17_aplicar_delta (sumiu / voltou / alterou), 21_backfill_conteudo.
# Antes cada um tinha a sua copia — e a do 17 nao recontava nem reelegia
# (auditoria A-1, A-5). Tudo aqui roda DENTRO da transacao de quem chama
# (REGRA 8): nenhuma funcao desta secao faz commit.
# ----------------------------------------------------------------------

# A ordem da politica de duplicatas (docs/07_POLITICA_DUPLICATAS.md, DEC-027).
# `id` no fim so desempata linhas sem item_id — reproduzivel do mesmo jeito.
ORDEM_REPRESENTANTE = "prioridade ASC, criado_em_nuvem ASC, item_id ASC, id ASC"

# Estados de `conteudo` que ja cobrem uma ocorrencia sem baixar de novo.
CONTEUDO_COBRE = ("EXTRAIDO", "PARCIAL", "OCR_REQUIRED", "VAZIO")

# extracao.status -> conteudo.estado (o mesmo mapa de worker.gravar_conteudo)
CONTEUDO_DO_STATUS = {"ok": "EXTRAIDO", "parcial": "PARCIAL",
                      "ocr_requerido": "OCR_REQUIRED", "vazio": "VAZIO",
                      "nao_suportado": "NAO_SUPORTADO", "protegido": "ERRO",
                      "erro": "ERRO"}


def registrar_evento(con: sqlite3.Connection, arquivo_id: int, execucao_id: int | None,
                     evento: str, detalhe: dict | None = None) -> None:
    con.execute(
        "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, detalhe, em) "
        "VALUES (?,?,?,?,?)",
        (arquivo_id, execucao_id, evento,
         json.dumps(detalhe, ensure_ascii=False, default=str) if detalhe else None,
         agora_iso()))


def recontar_ocorrencias(con: sqlite3.Connection, conteudo_id: int | None) -> int | None:
    """`conteudo.ocorrencias` = ocorrencias VIVAS (`presente=1`).

    E a mesma definicao de `v_ocorrencia` e da consulta 3 de
    `sql/consultas_busca.sql` ("contador x realidade"). Contar tambem as
    ausentes deixava o contador acima da realidade toda vez que uma copia
    virava MISSING (DEC-032, auditoria A-1).
    """
    if not conteudo_id:
        return None
    vivas = con.execute("SELECT COUNT(*) FROM arquivo WHERE conteudo_id=? AND presente=1",
                        (conteudo_id,)).fetchone()[0]
    con.execute("UPDATE conteudo SET ocorrencias=?, atualizado_em=? WHERE id=?",
                (vivas, agora_iso(), conteudo_id))
    return int(vivas)


def mover_trechos(con: sqlite3.Connection, de_id: int, para_id: int,
                  caminho_rel: str) -> int:
    """Re-aponta os trechos do indice de uma ocorrencia para outra.

    O indice (`texto_fts`) e chaveado pelo `arquivo_id` do representante.
    Quando o representante sai, o texto continua valido — o conteudo e o
    mesmo — e passa a responder pelo caminho de quem assumiu. Nada e
    apagado: o trecho muda de dono, nao de texto.
    """
    n = con.execute("SELECT COUNT(*) FROM texto_fts WHERE arquivo_id=?",
                    (de_id,)).fetchone()[0]
    if n:
        con.execute("UPDATE texto_fts SET arquivo_id=?, caminho_rel=? WHERE arquivo_id=?",
                    (para_id, caminho_rel, de_id))
    return int(n)


def reeleger_conteudo(con: sqlite3.Connection, conteudo_id: int,
                      execucao_id: int | None = None, motivo: str = "") -> int | None:
    """Garante um representante VIVO e MEMBRO para o conteudo, sem reextrair.

    Representante orfao = nulo, ausente (`presente=0`) ou que nao pertence
    mais ao conteudo (bytes mudaram, A-5). Se existe ocorrencia viva do
    mesmo conteudo, a primeira pela ordem da politica assume:
      - `conteudo.representante_id` passa a ela (`reeleicoes+1`);
      - o `duplicata_de` das demais copias e re-apontado para ela, e o dela
        fica NULL (representante nao e duplicata de ninguem);
      - os trechos do indice mudam de dono (`mover_trechos`).
    Devolve o id do novo representante, ou None se nada mudou (ja estava
    bom, ou nao ha ninguem vivo — o conteudo fica como registro historico).
    """
    c = con.execute("SELECT id, representante_id FROM conteudo WHERE id=?",
                    (conteudo_id,)).fetchone()
    if c is None:
        return None
    anterior = c["representante_id"]
    if anterior is not None:
        rep = con.execute("SELECT presente, conteudo_id FROM arquivo WHERE id=?",
                          (anterior,)).fetchone()
        if rep is not None and rep["presente"] == 1 and rep["conteudo_id"] == conteudo_id:
            return None
    novo = con.execute(
        f"SELECT id, caminho_rel FROM arquivo WHERE conteudo_id=? AND presente=1 "
        f"ORDER BY {ORDEM_REPRESENTANTE} LIMIT 1", (conteudo_id,)).fetchone()
    if novo is None:
        return None
    con.execute("UPDATE conteudo SET representante_id=?, reeleicoes=reeleicoes+1, "
                "atualizado_em=? WHERE id=?", (novo["id"], agora_iso(), conteudo_id))
    con.execute("UPDATE arquivo SET duplicata_de=NULL WHERE id=?", (novo["id"],))
    con.execute("UPDATE arquivo SET duplicata_de=? WHERE conteudo_id=? AND id<>? "
                "AND duplicata_de IS NOT NULL", (novo["id"], conteudo_id, novo["id"]))
    movidos = mover_trechos(con, anterior, novo["id"], novo["caminho_rel"]) if anterior else 0
    registrar_evento(con, novo["id"], execucao_id, "virou_representante",
                     {"conteudo_id": conteudo_id, "representante_anterior": anterior,
                      "trechos_movidos": movidos, "motivo": motivo or None})
    return int(novo["id"])


def conteudos_orfaos_reelegiveis(con: sqlite3.Connection) -> list[int]:
    """Conteudos com representante orfao E alguma ocorrencia viva."""
    return [int(l[0]) for l in con.execute(
        "SELECT c.id FROM conteudo c LEFT JOIN arquivo r ON r.id = c.representante_id "
        "WHERE (r.id IS NULL OR r.presente = 0 OR r.conteudo_id IS NOT c.id) "
        "  AND EXISTS (SELECT 1 FROM arquivo a WHERE a.conteudo_id = c.id "
        "              AND a.presente = 1) ORDER BY c.id")]


def reeleger_orfaos(con: sqlite3.Connection, execucao_id: int | None = None,
                    motivo: str = "") -> int:
    """Varre e reelege todos os orfaos reelegiveis. Devolve quantos trocaram."""
    trocas = 0
    for conteudo_id in conteudos_orfaos_reelegiveis(con):
        if reeleger_conteudo(con, conteudo_id, execucao_id, motivo) is not None:
            trocas += 1
    return trocas


def ocorrencia_saiu(con: sqlite3.Connection, arquivo_id: int,
                    execucao_id: int | None = None, motivo: str = "") -> dict:
    """A ocorrencia acabou de ficar `presente=0` (MISSING). Chamar DEPOIS do
    UPDATE e na MESMA transacao (T-11 / auditoria A-1).

    Reconta o conteudo dela e, se ela era o representante, reelege — o
    conteudo continua pesquisavel pelo caminho de uma copia viva. O vinculo
    `conteudo_id` da ausente FICA: e o registro historico, e e o que permite
    restaurar tudo se ela reaparecer (DEC-029).
    """
    linha = con.execute("SELECT conteudo_id FROM arquivo WHERE id=?", (arquivo_id,)).fetchone()
    conteudo_id = linha["conteudo_id"] if linha else None
    if not conteudo_id:
        return {}
    recontar_ocorrencias(con, conteudo_id)
    novo = reeleger_conteudo(con, conteudo_id, execucao_id, motivo or "representante ausente")
    return {"conteudo_id": conteudo_id, "novo_representante": novo}


def ocorrencia_voltou(con: sqlite3.Connection, arquivo_id: int,
                      execucao_id: int | None = None) -> dict:
    """A ocorrencia voltou a `presente=1` (reapareceu no delta). Reconta, e se o
    conteudo estava sem representante vivo, ela (ou quem a ordem mandar) assume."""
    linha = con.execute("SELECT conteudo_id FROM arquivo WHERE id=?", (arquivo_id,)).fetchone()
    conteudo_id = linha["conteudo_id"] if linha else None
    if not conteudo_id:
        return {}
    recontar_ocorrencias(con, conteudo_id)
    novo = reeleger_conteudo(con, conteudo_id, execucao_id, "ocorrencia reapareceu")
    return {"conteudo_id": conteudo_id, "novo_representante": novo}


def desvincular_conteudo(con: sqlite3.Connection, arquivo_id: int,
                         execucao_id: int | None = None, motivo: str = "") -> dict:
    """Os bytes desta ocorrencia MUDARAM: ela nao carrega mais aquele conteudo
    (T-13 / auditoria A-5).

    Zera `conteudo_id` e `duplicata_de`, reconta o grupo que ela deixou e,
    se ela era o representante, reelege uma copia viva (os trechos vao
    junto). Sem copia viva, o conteudo fica como registro historico — com
    derivado, extracao e trechos intactos — ate o worker reextrair esta
    ocorrencia (`indexar` substitui os trechos dela). Nada e apagado.
    Devolve o que foi desfeito, para o evento de quem chamou.
    """
    linha = con.execute("SELECT conteudo_id, duplicata_de FROM arquivo WHERE id=?",
                        (arquivo_id,)).fetchone()
    if linha is None or not linha["conteudo_id"]:
        return {}
    conteudo_id = linha["conteudo_id"]
    con.execute("UPDATE arquivo SET conteudo_id=NULL, duplicata_de=NULL WHERE id=?",
                (arquivo_id,))
    recontar_ocorrencias(con, conteudo_id)
    novo = reeleger_conteudo(con, conteudo_id, execucao_id,
                             motivo or "representante mudou de conteudo")
    return {"conteudo_id_anterior": conteudo_id,
            "duplicata_de_anterior": linha["duplicata_de"],
            "novo_representante": novo}


# --- vinculo de legados (antes do v4) -------------------------------------

def grupos_legados(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Ocorrencias que EXTRAIRAM antes do v4 e nao tem linha em `conteudo`
    (auditoria M-4 / CR-2): tem `extracao`, tem (quick_xor, tamanho > 0) e
    estao num estado em que a extracao vale para os bytes que elas carregam.
    MISSING entra: a extracao continua valendo para as copias vivas.
    CHANGED/fila nao entram — os bytes mudaram ou nunca foram extraidos.
    """
    return con.execute(
        "SELECT a.id, a.estado, a.presente, a.quick_xor, a.tamanho_bytes, a.item_id, "
        "       e.extrator, e.versao, e.status, e.texto_path, e.perfil_path, e.paginas, "
        "       e.caracteres, e.trechos, e.bytes_texto, e.executado_em "
        "FROM arquivo a JOIN extracao e ON e.arquivo_id = a.id "
        "WHERE a.conteudo_id IS NULL AND a.quick_xor IS NOT NULL "
        "  AND COALESCE(a.tamanho_bytes, 0) > 0 "
        "  AND a.estado IN ('COMPLETED','PARTIAL','OCR_REQUIRED','MISSING') "
        "  AND e.status NOT IN ('erro','protegido','nao_suportado') "
        "ORDER BY a.id").fetchall()


def vincular_legado(con: sqlite3.Connection, legado: sqlite3.Row,
                    execucao_id: int | None = None) -> dict:
    """Cria (ou reaproveita) o `conteudo` de uma extracao legada e liga a ele
    a propria ocorrencia e as copias que apontam para ela por `duplicata_de`
    com o MESMO (quick_xor, tamanho). Copia com hash diferente nao e ligada:
    e devolvida em `divergentes`, para quem chamou decidir.
    """
    quick, tamanho = legado["quick_xor"], legado["tamanho_bytes"]
    existente = con.execute("SELECT id FROM conteudo WHERE quick_xor=? AND tamanho_bytes=?",
                            (quick, tamanho)).fetchone()
    criado = existente is None
    if criado:
        agora = agora_iso()
        cur = con.execute(
            "INSERT INTO conteudo (quick_xor, tamanho_bytes, evidencia, representante_id, "
            "extraido_de_item, estado, extrator, versao_extrator, texto_path, perfil_path, "
            "paginas, caracteres, trechos, bytes_texto, ocorrencias, primeira_vez_em, "
            "atualizado_em) VALUES (?,?,'FORTE',?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
            (quick, tamanho, legado["id"], legado["item_id"],
             CONTEUDO_DO_STATUS.get(legado["status"], "ERRO"), legado["extrator"],
             legado["versao"], legado["texto_path"], legado["perfil_path"],
             legado["paginas"], legado["caracteres"], legado["trechos"],
             legado["bytes_texto"], legado["executado_em"] or agora, agora))
        conteudo_id = int(cur.lastrowid)
    else:
        conteudo_id = int(existente["id"])

    ligados, divergentes = [legado["id"]], []
    con.execute("UPDATE arquivo SET conteudo_id=? WHERE id=?", (conteudo_id, legado["id"]))
    registrar_evento(con, legado["id"], execucao_id, "vinculado_a_conteudo",
                     {"conteudo_id": conteudo_id, "conteudo_criado": criado,
                      "origem": "backfill de extracao legada (M-4/CR-2)"})
    copias = con.execute(
        "SELECT id, quick_xor, tamanho_bytes, conteudo_id FROM arquivo "
        "WHERE duplicata_de=? AND id<>?", (legado["id"], legado["id"])).fetchall()
    for cp in copias:
        if cp["quick_xor"] == quick and cp["tamanho_bytes"] == tamanho:
            if cp["conteudo_id"] != conteudo_id:
                con.execute("UPDATE arquivo SET conteudo_id=? WHERE id=?",
                            (conteudo_id, cp["id"]))
                registrar_evento(con, cp["id"], execucao_id, "vinculado_a_conteudo",
                                 {"conteudo_id": conteudo_id, "via_duplicata_de": legado["id"],
                                  "origem": "backfill de duplicata legada (CR-2)"})
                ligados.append(cp["id"])
        else:
            divergentes.append(cp["id"])
    recontar_ocorrencias(con, conteudo_id)
    novo = reeleger_conteudo(con, conteudo_id, execucao_id, "backfill: representante ausente")
    if novo is None and not criado:
        # Conteudo ja existia com outro representante (vivo): as copias
        # ligadas agora passam a apontar para ele, como as demais do grupo.
        rep = con.execute("SELECT representante_id FROM conteudo WHERE id=?",
                          (conteudo_id,)).fetchone()[0]
        if rep:
            con.execute("UPDATE arquivo SET duplicata_de=? WHERE conteudo_id=? AND id<>? "
                        "AND duplicata_de IS NOT NULL", (rep, conteudo_id, rep))
    return {"conteudo_id": conteudo_id, "criado": criado, "ligados": ligados,
            "divergentes": divergentes, "novo_representante": novo}


# --- invariantes ------------------------------------------------------------

def checar_invariantes(con: sqlite3.Connection) -> dict[str, list[int]]:
    """As regras da deduplicacao, conferidas pelo banco. Lista VAZIA = ok.

    Cada chave devolve os ids (de `arquivo` ou de `conteudo`, conforme o nome)
    que violam a regra. Nao conserta nada — so mostra (REGRA 7).
    """
    def ids(sql: str) -> list[int]:
        return [int(l[0]) for l in con.execute(sql)]

    return {
        # CR-2: copia viva presa a um representante que sumiu
        "duplicata_com_representante_ausente": ids(
            "SELECT a.id FROM arquivo a LEFT JOIN arquivo b ON b.id = a.duplicata_de "
            "WHERE a.duplicata_de IS NOT NULL AND a.presente = 1 "
            "  AND (b.id IS NULL OR b.presente = 0) ORDER BY a.id"),
        # CR-2: copia legada sem ancora em `conteudo` (invisivel a reeleicao)
        "duplicata_sem_conteudo": ids(
            "SELECT id FROM arquivo WHERE duplicata_de IS NOT NULL AND conteudo_id IS NULL "
            "ORDER BY id"),
        # M-4: extraiu, tem hash, e nao tem conteudo (busca oficial nao acha)
        "extraido_sem_conteudo": ids(
            "SELECT a.id FROM arquivo a WHERE a.conteudo_id IS NULL "
            "  AND a.estado IN ('COMPLETED','PARTIAL','OCR_REQUIRED') "
            "  AND a.quick_xor IS NOT NULL AND COALESCE(a.tamanho_bytes,0) > 0 "
            "  AND EXISTS (SELECT 1 FROM extracao e WHERE e.arquivo_id = a.id) ORDER BY a.id"),
        # A-1: representante nulo/ausente/de outro conteudo, havendo copia viva
        "conteudo_orfao_reelegivel": conteudos_orfaos_reelegiveis(con),
        # DEC-032: contador x realidade (consulta 3 de consultas_busca.sql)
        "contador_divergente": ids(
            "SELECT c.id FROM conteudo c WHERE c.ocorrencias <> (SELECT COUNT(*) FROM "
            "arquivo a WHERE a.conteudo_id = c.id AND a.presente = 1) ORDER BY c.id"),
        # M-16: sem FK, ninguem mais pega ponteiro quebrado
        "conteudo_id_inexistente": ids(
            "SELECT a.id FROM arquivo a LEFT JOIN conteudo c ON c.id = a.conteudo_id "
            "WHERE a.conteudo_id IS NOT NULL AND c.id IS NULL ORDER BY a.id"),
        # A-5: ocorrencia diz carregar bytes que nao carrega mais
        "ocorrencia_com_hash_de_outro_conteudo": ids(
            "SELECT a.id FROM arquivo a JOIN conteudo c ON c.id = a.conteudo_id "
            "WHERE a.quick_xor IS NOT c.quick_xor OR a.tamanho_bytes IS NOT c.tamanho_bytes "
            "ORDER BY a.id"),
        # A-1: trecho sob um ausente quando o mesmo conteudo tem copia viva
        "trecho_em_ausente_com_copia_viva": ids(
            "SELECT DISTINCT a.id FROM texto_fts f JOIN arquivo a ON a.id = f.arquivo_id "
            "WHERE a.presente = 0 AND ("
            "  EXISTS (SELECT 1 FROM arquivo v WHERE a.conteudo_id IS NOT NULL "
            "          AND v.conteudo_id = a.conteudo_id AND v.presente = 1) "
            "  OR EXISTS (SELECT 1 FROM arquivo v WHERE a.conteudo_id IS NULL "
            "          AND a.quick_xor IS NOT NULL AND v.quick_xor = a.quick_xor "
            "          AND v.tamanho_bytes = a.tamanho_bytes AND v.presente = 1 "
            "          AND v.duplicata_de = a.id)) ORDER BY a.id"),
    }


def informativo_dedupe(con: sqlite3.Connection) -> dict[str, int]:
    """Numeros que NAO sao violacao, mas explicam o retrato (historico)."""
    def um(sql: str) -> int:
        return int(con.execute(sql).fetchone()[0])
    return {
        "conteudos": um("SELECT COUNT(*) FROM conteudo"),
        # Historico, nao defeito: os bytes nao existem mais em lugar nenhum do
        # escopo (todas as copias MISSING, ou a unica mudou de bytes). O
        # conteudo fica com derivado e extracao — e o que a REGRA 6 manda.
        "conteudo_sem_ocorrencia": um(
            "SELECT COUNT(*) FROM conteudo c WHERE NOT EXISTS "
            "(SELECT 1 FROM arquivo a WHERE a.conteudo_id = c.id)"),
        "conteudo_sem_ocorrencia_viva": um(
            "SELECT COUNT(*) FROM conteudo c WHERE NOT EXISTS (SELECT 1 FROM arquivo a "
            "WHERE a.conteudo_id = c.id AND a.presente = 1)"),
        "trechos_total": um("SELECT COUNT(*) FROM texto_fts"),
        "trechos_em_ausente": um(
            "SELECT COUNT(*) FROM texto_fts f JOIN arquivo a ON a.id = f.arquivo_id "
            "WHERE a.presente = 0"),
        "trechos_sem_conteudo": um(
            "SELECT COUNT(*) FROM texto_fts f JOIN arquivo a ON a.id = f.arquivo_id "
            "WHERE a.conteudo_id IS NULL"),
        "completed_sem_conteudo": um(
            "SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED' AND conteudo_id IS NULL"),
    }


def total_violacoes(violacoes: dict[str, list[int]]) -> int:
    return sum(len(v) for v in violacoes.values())


# --- reprocessamento por versao de extrator (REGRA 9, T-17) -----------------

def versao_atual_extrator(extrator: str) -> str | None:
    """A versao que o extrator desta categoria produz HOJE (`extratores.VERSOES`)."""
    import extratores  # tardio: so quem pergunta paga o import
    return extratores.VERSOES.get(extrator)


_NUMERO_DE_VERSAO = re.compile(r"\d+(?:\.\d+)*")


def eh_versao_de_extrator(extrator: str | None, versao: str | None) -> bool:
    """True se `versao` foi produzida por um extrator: `<extrator>/<n[.n...]>`.

    Tudo o mais e MARCA, nao versao: `'-'` (arquivo de 0 byte), `'?'`,
    `'adiado/teto_tamanho'` (`operacao.VERSAO_ADIADO_TETO`, gravada pelo
    worker no PARTIAL acima do teto, sem download) e qualquer outra marca de
    adiamento que venha a existir. Marca nao e "versao antiga": nenhuma
    versao de extrator a produziu, entao subir a versao nao a torna velha.
    """
    v = (versao or "").strip()
    prefixo = f"{extrator}/"
    return bool(extrator) and v.startswith(prefixo) and bool(
        _NUMERO_DE_VERSAO.fullmatch(v[len(prefixo):]))


def ainda_sem_leitor(extrator: str | None, status: str | None, ext: str | None) -> bool:
    """`nao_suportado` cuja extensao CONTINUA sem leitor (`.xls`, `.doc`...).

    E o UNSUPPORTED "sem leitor" do worker (L7, erro `SEM_LEITOR: ...`) e o
    mesmo `nao_suportado` decidido depois do download. Reextrair daria o
    mesmo resultado, qualquer que seja a versao do extrator da categoria —
    entao nao conta como versao antiga. No dia em que a extensao ganhar
    leitor (`extratores.motivo_sem_leitor` passa a devolver None), a regra
    se desliga sozinha e ele volta no padrao.
    """
    if status != "nao_suportado":
        return False
    import extratores  # tardio, como em versao_atual_extrator
    return extratores.motivo_sem_leitor(ext or "", extrator) is not None


def extracoes_desatualizadas(con: sqlite3.Connection, *, categorias=None, versoes=None,
                             erro_contem: str | None = None, estados=None,
                             incluir_sem_versao: bool = False) -> list[dict]:
    """Quem precisa reextrair, um item por CONTEUDO (o representante).

    A unidade de decisao e o CONTEUDO (R5b): de todas as linhas de `extracao`
    das ocorrencias daquele conteudo, vale a MAIS RECENTE (`executado_em`;
    empate -> a do representante, depois o maior `arquivo_id`). Se ela ja e
    da versao atual, o conteudo nao volta — mesmo que um ex-representante
    MISSING ainda guarde a linha velha (historico, REGRA 6). Sem isto, o
    conteudo reeleito voltava para a fila a cada passada do `22`.

    Criterio padrao: a extracao vigente tem versao DE EXTRATOR
    (`eh_versao_de_extrator`) diferente de `extratores.VERSOES[extrator]` —
    o derivado foi feito por versao anterior. Ficam de fora do padrao:
      - MARCAS (`'-'` do arquivo vazio, `'adiado/teto_tamanho'` do PARTIAL
        acima do teto, qualquer `adiado/...`): so entram com
        `incluir_sem_versao=True` ou por filtro explicito (R5a);
      - `nao_suportado` de extensao que continua sem leitor
        (`ainda_sem_leitor`; o UNSUPPORTED `SEM_LEITOR` do worker): idem.
    Com filtro explicito (`erro_contem`, `estados`, `versoes`) entra tudo o
    que casar, de qualquer versao — e assim que os PARTIAL adiados voltam
    (`--erro adiada` ou `--versao adiado/teto_tamanho`).

    Filtros (todos opcionais, combinados com E, aplicados a extracao
    vigente do conteudo): `categorias` (extrator), `versoes` (versao
    gravada), `erro_contem` (trecho de `extracao.erro` ou de
    `arquivo.ultimo_erro`), `estados` (estado da ocorrencia que extraiu).

    Garantia: extracao de versao ATUAL em estado COMPLETED nunca e devolvida.
    """
    linhas = con.execute(
        "SELECT e.arquivo_id, e.extrator, e.versao, e.status, e.erro, e.executado_em, "
        "       a.estado, a.presente, a.ultimo_erro, a.ext, a.caminho_rel, a.conteudo_id, "
        "       c.representante_id "
        "FROM extracao e JOIN arquivo a ON a.id = e.arquivo_id "
        "LEFT JOIN conteudo c ON c.id = a.conteudo_id ORDER BY e.arquivo_id").fetchall()

    # 1. a extracao VIGENTE de cada conteudo (ou de cada arquivo sem conteudo)
    vigente: dict = {}
    for l in linhas:
        chave = ("c", l["conteudo_id"]) if l["conteudo_id"] else ("a", l["arquivo_id"])
        ordem = (l["executado_em"] or "", l["arquivo_id"] == l["representante_id"],
                 l["arquivo_id"])
        if chave not in vigente or ordem > vigente[chave][0]:
            vigente[chave] = (ordem, l)

    # 2. o criterio, aplicado so a ela
    filtrado = erro_contem is not None or bool(estados) or bool(versoes)
    saida: list[dict] = []
    for _ordem, l in sorted(vigente.values(), key=lambda par: par[1]["arquivo_id"]):
        atual = versao_atual_extrator(l["extrator"])
        marca = not eh_versao_de_extrator(l["extrator"], l["versao"])
        sem_leitor = ainda_sem_leitor(l["extrator"], l["status"],
                                      l["ext"] or Path(l["caminho_rel"] or "").suffix)
        desatualizada = ((not marca) and (not sem_leitor) and atual is not None
                         and l["versao"] != atual)
        if categorias and l["extrator"] not in categorias:
            continue
        if versoes and l["versao"] not in versoes:
            continue
        if erro_contem is not None and erro_contem.lower() not in (
                f"{l['erro'] or ''} {l['ultimo_erro'] or ''}").lower():
            continue
        if estados and l["estado"] not in estados:
            continue
        # Filtro explicito (--erro/--estado/--versao) escolhe pelo que pediu,
        # mesmo de versao atual ou marca. O padrao escolhe so as desatualizadas.
        if not (desatualizada or filtrado or (marca and incluir_sem_versao)):
            continue
        if l["versao"] == atual and l["estado"] == "COMPLETED":
            continue                                  # versao atual: nunca
        saida.append({"arquivo_id": l["arquivo_id"],
                      "alvo_id": l["representante_id"] or l["arquivo_id"],
                      "conteudo_id": l["conteudo_id"], "extrator": l["extrator"],
                      "versao": l["versao"], "versao_atual": atual, "status": l["status"],
                      "estado": l["estado"], "erro": l["erro"]})
    return saida


# ----------------------------------------------------------------------
# Controle do worker (pause / resume / stop) — sem depender de IA
# ----------------------------------------------------------------------

def ler_controle(con: sqlite3.Connection) -> sqlite3.Row:
    linha = con.execute("SELECT * FROM controle WHERE id=1").fetchone()
    if linha is None:
        con.execute("INSERT INTO controle (id, comando, estado, atualizado_em) "
                    "VALUES (1,'RUN','PARADO',?)", (agora_sql(),))
        con.commit()
        linha = con.execute("SELECT * FROM controle WHERE id=1").fetchone()
    return linha


def escrever_controle(con: sqlite3.Connection, **campos) -> None:
    if not campos:
        return
    campos["atualizado_em"] = agora_sql()
    sets = ", ".join(f"{k}=?" for k in campos)
    con.execute(f"UPDATE controle SET {sets} WHERE id=1", list(campos.values()))
    con.commit()


def processo_vivo(pid: int | None) -> bool:
    """True se o PID existe (Windows). Usado para detectar worker zumbi."""
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


# ----------------------------------------------------------------------
# Log estruturado, com rotacao
# ----------------------------------------------------------------------

class Log:
    def __init__(self, pasta_logs: Path, etapa: str, con: sqlite3.Connection | None = None,
                 execucao_id: int | None = None, verboso: bool = True,
                 max_mb: float = 20.0, gravar_no_banco: bool = True):
        pasta_logs.mkdir(parents=True, exist_ok=True)
        self.pasta = pasta_logs
        self.etapa = etapa
        self.con = con
        self.execucao_id = execucao_id
        self.verboso = verboso
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.gravar_no_banco = gravar_no_banco
        self.arquivo = self._novo_arquivo()
        self._fh = open(self.arquivo, "a", encoding="utf-8")

    def _novo_arquivo(self) -> Path:
        carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.pasta / f"{self.etapa}_{carimbo}.jsonl"

    def _rotacionar_se_preciso(self):
        try:
            if self._fh.tell() > self.max_bytes:
                self._fh.close()
                self.arquivo = self._novo_arquivo()
                self._fh = open(self.arquivo, "a", encoding="utf-8")
        except Exception:
            pass

    def _escrever(self, nivel: str, mensagem: str, caminho_rel: str = "", **extra):
        registro = {"em": agora_iso(), "nivel": nivel, "etapa": self.etapa,
                    "caminho": caminho_rel, "msg": mensagem}
        registro.update(extra)
        try:
            self._fh.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
            self._rotacionar_se_preciso()
        except Exception:
            pass
        if self.con is not None and self.gravar_no_banco and nivel in ("aviso", "erro"):
            try:
                self.con.execute(
                    "INSERT INTO log (execucao_id, nivel, etapa, caminho_rel, mensagem, em) "
                    "VALUES (?,?,?,?,?,?)",
                    (self.execucao_id, nivel, self.etapa, caminho_rel, mensagem, registro["em"]),
                )
            except sqlite3.Error:
                pass
        if self.verboso and nivel in ("info", "aviso", "erro"):
            print(f"[{nivel}] {mensagem}",
                  file=sys.stderr if nivel == "erro" else sys.stdout, flush=True)

    def info(self, mensagem: str, caminho_rel: str = "", **extra):
        self._escrever("info", mensagem, caminho_rel, **extra)

    def aviso(self, mensagem: str, caminho_rel: str = "", **extra):
        self._escrever("aviso", mensagem, caminho_rel, **extra)

    def erro(self, mensagem: str, caminho_rel: str = "", **extra):
        self._escrever("erro", mensagem, caminho_rel, **extra)

    def debug(self, mensagem: str, caminho_rel: str = "", **extra):
        self._escrever("debug", mensagem, caminho_rel, **extra)

    def fechar(self):
        try:
            self._fh.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# Diagnostico do ambiente
# ----------------------------------------------------------------------

def diagnostico(cfg: dict) -> dict:
    raiz = cfg["_raiz_path"]
    return {
        "python": sys.version.split()[0],
        "raiz": str(raiz),
        "raiz_existe": raiz.exists(),
        "long_path_habilitado": _long_path_habilitado(),
        "fts5": tem_fts5(),
        "espaco_livre_gb": round(espaco_livre_gb(cfg["_banco_path"].anchor or "C:\\"), 1),
        "banco": str(cfg["_banco_path"]),
        "hidratacao_permitida": bool((cfg.get("nuvem") or {}).get("permitir_hidratacao", False)),
        "max_hidratacao_gb": (cfg.get("nuvem") or {}).get("max_hidratacao_gb"),
        "espaco_livre_minimo_gb": (cfg.get("nuvem") or {}).get("espaco_livre_minimo_gb"),
    }


def _long_path_habilitado() -> bool:
    if os.name != "nt":
        return True
    try:
        import winreg

        chave = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"SYSTEM\CurrentControlSet\Control\FileSystem")
        valor, _ = winreg.QueryValueEx(chave, "LongPathsEnabled")
        return bool(valor)
    except Exception:
        return False
