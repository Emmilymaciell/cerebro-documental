"""
fonte — de onde o worker tira os bytes de um documento.

Duas implementacoes, uma so interface:

  FonteGraph       producao. Baixa do SharePoint pelo Microsoft Graph para
                   `tmp/`, confere tamanho e quickXorHash, e o worker apaga
                   depois de usar. NUNCA le o disco sincronizado.

  FonteLocalTeste  **somente testes**. Le uma arvore de mentira montada pelo
                   proprio teste. Nao existe como fallback de producao: se o
                   Graph falhar, o item volta para a fila, ponto.

`obter()` nunca levanta excecao de rede para fora sem contexto: devolve um
dicionario com `ok`, `motivo` e `transitorio`, e quem decide o que fazer e o
worker (que sabe de estado, tentativa e backoff).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402
import graph_ro  # noqa: E402
import quickxor  # noqa: E402

PREFIXO_TMP = "e7_"


CODIGOS_AUSENTE = {"itemnotfound", "resourcenotfound", "notfound"}

# B4 — a credencial do APLICATIVO foi recusada. `falha_autenticacao` e o
# codigo que o `graph_ro.token()` da quando o login do Entra ID nao aceita o
# segredo (expirado, revogado, client id errado) — o status ai pode ser 400
# ou 401, por isso o codigo conta sozinho.
CODIGOS_CREDENCIAL = {"falha_autenticacao", "invalidauthenticationtoken",
                      "unauthenticated", "accessdenied"}


def _resultado(ok: bool, **campos) -> dict:
    base = {"ok": ok, "caminho": None, "bytes": 0, "quick_xor": None,
            "remover": False, "motivo": "", "transitorio": False, "ausente": False,
            "interrompido": False, "credencial": False, "status": None, "codigo": None}
    base.update(campos)
    return base


def _e_credencial(exc) -> bool:
    """401/403 (ou login recusado): problema de CREDENCIAL, nao do documento.

    Quem decide se e do aplicativo inteiro ou de um item so e o worker
    (`Worker._falha_credencial`), que sabe se a credencial ja funcionou nesta
    sessao. Aqui so se separa da falha do documento — e de rede: um 503 do
    endpoint de login continua sendo espera de rede.
    """
    if graph_ro.erro_transitorio(exc):
        return False
    return (getattr(exc, "status", 0) in (401, 403)
            or str(getattr(exc, "codigo", "")).lower() in CODIGOS_CREDENCIAL)


def _e_ausente(exc) -> bool:
    """404/410 do Graph = o item nao esta la. Nao e falha nossa nem da rede.

    Quem trata isso e o worker, marcando `MISSING` (DEC-029) em vez de gastar
    quatro tentativas para terminar em `ERROR_FINAL` — que seria rotular de
    "erro" uma pasta que simplesmente saiu do acervo.
    """
    return (getattr(exc, "status", 0) in (404, 410)
            or str(getattr(exc, "codigo", "")).lower() in CODIGOS_AUSENTE)


class FonteGraph:
    """Download temporario, validado, descartavel."""

    nome = "graph"

    def __init__(self, cfg: dict, graph=None, pasta_tmp: Path | None = None):
        self.cfg = cfg
        self.graph = graph or graph_ro.GraphRO(
            pausa_minima_s=float((cfg.get("nuvem") or {}).get("pausa_minima_s", 0.0)))
        self.tmp = Path(pasta_tmp or (cu.RAIZ_REPO / "tmp"))
        self.tmp.mkdir(parents=True, exist_ok=True)
        limites = cfg.get("worker") or {}
        self.tamanho_max = int(float(limites.get("tamanho_max_download_gb", 2))
                               * 1024 ** 3)
        # T-18: teto TOTAL de um download (tentativas e esperas somadas) e
        # maximo sem receber byte nenhum. 900 s cobre 300 MB (o teto de
        # extracao) a ~0,35 MB/s; o E7 mediu ~1,1 MB/s.
        self.prazo_s = float(limites.get("timeout_download_s", 900) or 0) or None
        self.timeout_leitura_s = float(limites.get("timeout_leitura_s", 120) or 120)
        # M-14: o worker pendura aqui a pergunta "pediram para parar?". Fica
        # None fora do worker (o download nunca e interrompido por ninguem).
        self.deve_parar = None
        self.bytes_baixados = 0

    # -- limpeza -------------------------------------------------------
    def limpar_sobras(self) -> int:
        """Restos de uma execucao interrompida. Roda na subida do worker."""
        removidos = 0
        for caminho in self.tmp.glob(f"{PREFIXO_TMP}*"):
            try:
                caminho.unlink()
                removidos += 1
            except OSError:
                pass
        return removidos

    def descartar(self, caminho) -> None:
        if not caminho:
            return
        try:
            Path(caminho).unlink(missing_ok=True)
        except OSError:
            pass

    # -- o download ----------------------------------------------------
    def obter(self, item) -> dict:
        drive_id, item_id = item["drive_id"], item["item_id"]
        if not drive_id or not item_id:
            return _resultado(False, motivo="sem drive_id/item_id: nao ha o que baixar")

        esperado = int(item["tamanho_bytes"] or 0)
        if esperado > self.tamanho_max:
            return _resultado(False, motivo=f"arquivo acima do teto de download "
                                            f"({esperado/1024**3:.2f} GB)")

        sufixo = Path(item["caminho_nuvem"] or item["caminho_rel"] or "").suffix
        destino = self.tmp / f"{PREFIXO_TMP}{item_id[:24]}{sufixo}"
        entregue = False
        try:
            try:
                baixados = self.graph.baixar(drive_id, item_id, destino,
                                             tamanho_max_bytes=self.tamanho_max,
                                             prazo_s=self.prazo_s,
                                             timeout_leitura_s=self.timeout_leitura_s,
                                             deve_parar=self.deve_parar)
            except graph_ro.ErroGraph as exc:
                motivo = f"{exc.status} {exc.codigo}: {exc.mensagem[:160]}"
                if exc.codigo == "interrompido":
                    # Pediram para parar: nao e falha de ninguem. O worker
                    # devolve o item ao estado de antes, sem gastar tentativa.
                    return _resultado(False, motivo=motivo, interrompido=True)
                if exc.codigo == "tempo_esgotado":
                    # Estourou o prazo duro do item (T-18). Conta tentativa do
                    # documento — ERROR_RETRYABLE e, na 4a, ERROR_FINAL — em vez
                    # de virar espera de rede: um download que pendura sempre
                    # nao pode custar 12 x 15 min.
                    return _resultado(False, motivo=motivo, transitorio=False)
                return _resultado(False, motivo=motivo,
                                  transitorio=graph_ro.erro_transitorio(exc),
                                  ausente=_e_ausente(exc),
                                  credencial=_e_credencial(exc),
                                  status=exc.status, codigo=exc.codigo)
            except OSError as exc:
                return _resultado(False, motivo=f"erro de disco: {exc}", transitorio=True)

            self.bytes_baixados += baixados

            # --- validacao: tamanho -------------------------------------
            if esperado and baixados != esperado:
                return _resultado(False, caminho=None, bytes=baixados,
                                  motivo=f"tamanho divergente: baixou {baixados}, "
                                         f"o Graph anunciou {esperado}",
                                  transitorio=True)

            # --- validacao: quickXorHash --------------------------------
            # R4: o hash e calculado SEMPRE. Antes so era calculado quando o
            # catalogo ja tinha um para comparar — e o item sem hash no
            # inventario saia sem `quick_xor`, logo sem `conteudo`, logo fora
            # da busca v5 (indice por conteudo). Com hash anunciado, ele valida;
            # sem, o calculado passa a ser o hash do item.
            anunciado = item["quick_xor"] if "quick_xor" in item.keys() else None
            try:
                calculado = quickxor.de_arquivo(destino)
            except OSError as exc:
                return _resultado(False, motivo=f"nao consegui ler o baixado: {exc}",
                                  transitorio=True)
            if anunciado and calculado != anunciado:
                return _resultado(False, bytes=baixados,
                                  motivo=f"quickXorHash divergente: calculei "
                                         f"{calculado}, o Graph anunciou {anunciado}")

            entregue = True
            return _resultado(True, caminho=destino, bytes=baixados,
                              quick_xor=calculado, remover=True)
        finally:
            # O temporario so sobrevive se foi ENTREGUE ao worker (que o apaga
            # depois de extrair). Qualquer outra saida — erro, prazo,
            # interrupcao, excecao inesperada, Ctrl+C — apaga aqui.
            if not entregue:
                self.descartar(destino)


class FonteLocalTeste:
    """Le a arvore local. **Somente para teste automatizado.**"""

    nome = "local"

    def __init__(self, cfg: dict, **_):
        self.cfg = cfg
        self.raiz = cfg["_raiz_path"]
        self.bytes_baixados = 0

    def limpar_sobras(self) -> int:
        return 0

    def descartar(self, caminho) -> None:
        """Nao apaga nada: o 'temporario' aqui e o proprio original."""
        return None

    def obter(self, item) -> dict:
        caminho = self.raiz / item["caminho_rel"]
        try:
            tamanho = os.path.getsize(cu.lp(caminho))
            # Calcula o mesmo hash que o servidor calcularia, para o teste
            # exercitar a deduplicacao igual a producao.
            quick = quickxor.de_arquivo(caminho) if tamanho else None
        except OSError as exc:
            return _resultado(False, motivo=f"nao achei no disco: {exc}",
                              transitorio=cu.erro_transitorio(exc))
        # `remover=False`: o worker NUNCA apaga o original.
        return _resultado(True, caminho=caminho, bytes=tamanho, quick_xor=quick,
                          remover=False)


def criar(nome: str, cfg: dict, **extra):
    if nome == "graph":
        return FonteGraph(cfg, **extra)
    if nome == "local":
        return FonteLocalTeste(cfg, **extra)
    raise ValueError(f"fonte desconhecida: {nome}")
