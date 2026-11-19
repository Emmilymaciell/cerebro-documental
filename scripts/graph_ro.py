"""
graph_ro — acesso ao SharePoint pelo Microsoft Graph, SOMENTE LEITURA.

Este modulo e a fronteira do Cerebro Documental com a nuvem. Ele nao tem —
e nao pode ganhar — nenhum verbo de escrita:

    * `_requisicao()` recusa qualquer metodo diferente de GET;
    * nao existe POST, PATCH, PUT, DELETE ou upload em lugar nenhum daqui;
    * `tests/test_graph_ro.py` le o proprio codigo-fonte e falha se aparecer.

Escrita, exclusao e reorganizacao do SharePoint sao de OUTRO projeto
(`PROG_Organizacao_SharePoint`). Os dois compartilham o registro de
aplicativo no Entra ID, mas nao compartilham codigo: este arquivo e uma
copia read-only, deliberadamente separada, com `.env` proprio.

Sem dependencia externa: so a biblioteca padrao.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException  # so a excecao; conexao nunca (T-15)
from pathlib import Path

RAIZ_REPO = Path(__file__).resolve().parents[1]
ENV = RAIZ_REPO / ".env"
GRAPH = "https://graph.microsoft.com/v1.0"

# Campos que o inventario precisa. Pedir so isto deixa a resposta pequena
# e reduz a chance de o Graph limitar a taxa (429).
CAMPOS_ITEM = ("id,name,size,file,folder,parentReference,eTag,cTag,webUrl,"
               "createdDateTime,lastModifiedDateTime,createdBy,lastModifiedBy,"
               "deleted")

METODO_PERMITIDO = "GET"


# ----------------------------------------------------------------------
# Credenciais
# ----------------------------------------------------------------------

def carregar_env(caminho: Path | str | None = None) -> dict:
    """Le o .env deste repositorio (chave=valor, # comenta).

    Variavel de ambiente vence o arquivo. O segredo nunca e impresso.
    """
    dados: dict[str, str] = {}
    caminho = Path(caminho) if caminho else ENV
    if caminho.exists():
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            dados[chave.strip()] = valor.strip()
    for chave in list(dados) + ["GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"]:
        if os.environ.get(chave):
            dados[chave] = os.environ[chave]
    return dados


class ErroGraph(Exception):
    """Erro devolvido pelo Graph, com codigo HTTP e mensagem legivel."""

    def __init__(self, status: int, codigo: str, mensagem: str, url: str = ""):
        self.status = status
        self.codigo = codigo
        self.mensagem = mensagem
        self.url = url
        super().__init__(f"HTTP {status} {codigo}: {mensagem}")


def erro_transitorio(exc: BaseException) -> bool:
    """Falha de rede/limite de taxa nao e culpa do documento: tentar depois."""
    if isinstance(exc, ErroGraph):
        return exc.status in (0, 429, 500, 502, 503, 504) or exc.codigo in (
            "sem_rede", "tentativas_esgotadas", "serviceNotAvailable",
            "activityLimitReached", "unknownError", "generalException")
    return isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError))


# ----------------------------------------------------------------------
# Prazo duro de um download (T-18) e parada pedida no meio (M-14)
# ----------------------------------------------------------------------

# Erros de socket que acontecem DEPOIS de a conexao abrir (no meio da
# leitura). `urlopen` so embrulha em URLError o que falha ao conectar.
ERROS_DE_LEITURA = (TimeoutError, ConnectionError, HTTPException)


class Prazo:
    """O relogio de UM download: prazo duro + pedido de parada.

    Sem isto, um download pendurado segurava o lock e o heartbeat do worker
    por tempo indefinido: o `timeout` do `urlopen` vale por operacao de
    socket, entao um servidor que pinga 1 byte a cada minuto nunca estoura,
    e as esperas de 429 (`Retry-After`, ate 120 s, 3 vezes) somavam por cima.

    - `segundos`: teto TOTAL do download, somando tentativas e esperas.
      Estourou -> `ErroGraph(0, 'tempo_esgotado')`.
    - `deve_parar()`: consultado no maximo 1x/s durante a leitura e a cada
      0,5 s durante as esperas. True -> `ErroGraph(0, 'interrompido')`.

    O pior caso fica limitado a `segundos + timeout de leitura`.
    """

    def __init__(self, segundos: float | None = None, deve_parar=None):
        self.segundos = float(segundos) if segundos else None
        self.fim = (time.monotonic() + self.segundos) if self.segundos else None
        self.deve_parar = deve_parar
        self._ultima_consulta = 0.0

    def restante(self) -> float | None:
        return None if self.fim is None else self.fim - time.monotonic()

    def checar(self, consultar_sempre: bool = False) -> None:
        restante = self.restante()
        if restante is not None and restante <= 0:
            raise ErroGraph(0, "tempo_esgotado",
                            f"download passou de {self.segundos:.0f}s (prazo duro por item)")
        if self.deve_parar is None:
            return
        agora = time.monotonic()
        if consultar_sempre or agora - self._ultima_consulta >= 1.0:
            self._ultima_consulta = agora
            if self.deve_parar():
                raise ErroGraph(0, "interrompido", "parada pedida durante o download")

    def timeout(self, padrao: float) -> float:
        """Timeout de socket que nao deixa a operacao passar muito do prazo."""
        restante = self.restante()
        return padrao if restante is None else max(0.5, min(padrao, restante))

    def cabe(self, segundos: float) -> bool:
        restante = self.restante()
        return restante is None or segundos < restante

    def dormir(self, segundos: float) -> None:
        """Dorme em fatias de 0,5 s, olhando o prazo e o pedido de parada."""
        fim = time.monotonic() + max(0.0, segundos)
        while True:
            self.checar(consultar_sempre=True)
            falta = fim - time.monotonic()
            if falta <= 0:
                return
            time.sleep(min(0.5, falta))


# ----------------------------------------------------------------------
# Cliente
# ----------------------------------------------------------------------

class GraphRO:
    def __init__(self, env: dict | None = None, pausa_minima_s: float = 0.0):
        self.env = env or carregar_env()
        faltando = [c for c in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET")
                    if not self.env.get(c)]
        if faltando:
            raise ValueError(f".env sem as chaves: {', '.join(faltando)}")
        self.host = self.env.get("SHAREPOINT_HOST", "contoso.sharepoint.com")
        self.site = self.env.get("SHAREPOINT_SITE", "/sites/exemplo")
        self.pausa_minima_s = pausa_minima_s      # ritmo, para nao levar 429
        self._token = None
        self._expira_em = 0.0
        self._ultima_chamada = 0.0
        self.chamadas = 0
        self.esperas_429 = 0
        self.bytes_baixados = 0

    # -- autenticacao ---------------------------------------------------
    def token(self) -> str:
        """Token de aplicativo (client credentials). Renova sozinho."""
        if self._token and time.time() < self._expira_em - 120:
            return self._token

        url = (f"https://login.microsoftonline.com/{self.env['GRAPH_TENANT_ID']}"
               f"/oauth2/v2.0/token")
        corpo = urllib.parse.urlencode({
            "client_id": self.env["GRAPH_CLIENT_ID"],
            "client_secret": self.env["GRAPH_CLIENT_SECRET"],
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode()
        # Unico POST do modulo, e ele nao toca em documento nenhum: e o
        # endpoint de login do Entra ID, que exige form-encoded.
        requisicao = urllib.request.Request(
            url, data=corpo,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(requisicao, timeout=60) as resposta:
                dados = json.loads(resposta.read())
        except urllib.error.HTTPError as exc:
            detalhe = exc.read().decode("utf-8", "replace")
            try:
                mensagem = json.loads(detalhe).get("error_description", detalhe)
            except json.JSONDecodeError:
                mensagem = detalhe
            raise ErroGraph(exc.code, "falha_autenticacao",
                            mensagem.split("\r")[0], url) from exc

        self._token = dados["access_token"]
        self._expira_em = time.time() + int(dados.get("expires_in", 3600))
        return self._token

    # -- a unica porta de saida -----------------------------------------
    def _requisicao(self, url: str, metodo: str = "GET", autenticar: bool = True,
                    aceitar: str = "application/json", token_no_redirect: bool = True):
        """Monta a requisicao. Recusa qualquer metodo que nao seja GET.

        `token_no_redirect=False` poe o token como cabecalho NAO
        redirecionavel: vai para o Graph, mas nao segue o 302 para o CDN de
        download (o `HTTPRedirectHandler` so copia `req.headers`).
        """
        if metodo != METODO_PERMITIDO:
            raise PermissionError(
                f"graph_ro e somente leitura: metodo {metodo} bloqueado. "
                f"Escrita no SharePoint e do projeto PROG_Organizacao_SharePoint.")
        requisicao = urllib.request.Request(url, headers={"Accept": aceitar},
                                            method=METODO_PERMITIDO)
        if autenticar:
            valor = f"Bearer {self.token()}"
            if token_no_redirect:
                requisicao.add_header("Authorization", valor)
            else:
                requisicao.add_unredirected_header("Authorization", valor)
        return requisicao

    def _ritmo(self):
        if self.pausa_minima_s <= 0:
            return
        espera = self.pausa_minima_s - (time.monotonic() - self._ultima_chamada)
        if espera > 0:
            time.sleep(espera)
        self._ultima_chamada = time.monotonic()

    def get(self, caminho: str, tentativas: int = 5, prazo: Prazo | None = None) -> dict:
        """GET no Graph. `caminho` relativo ('/sites/...') ou URL completa.

        `prazo` (opcional) limita o tempo total, esperas incluidas — e o que
        o download usa. Sem ele, o comportamento e o de sempre.
        """
        url = (caminho if caminho.startswith("http")
               else GRAPH + urllib.parse.quote(caminho, safe="/:?$&=,@!*'()+~%"))
        relogio = prazo or Prazo()
        for tentativa in range(1, tentativas + 1):
            relogio.checar()
            self._ritmo()
            try:
                with urllib.request.urlopen(self._requisicao(url),
                                            timeout=relogio.timeout(120)) as resposta:
                    self.chamadas += 1
                    return json.loads(resposta.read())
            except urllib.error.HTTPError as exc:
                bruto = exc.read().decode("utf-8", "replace")
                codigo, mensagem = _detalhar(bruto, exc)
                if exc.code in (429, 500, 502, 503, 504) and tentativa < tentativas:
                    espera = min(float(exc.headers.get("Retry-After", 5 * tentativa)), 120)
                    if relogio.cabe(espera):
                        self.esperas_429 += 1
                        relogio.dormir(espera)
                        continue
                raise ErroGraph(exc.code, codigo, mensagem, url) from exc
            except urllib.error.URLError as exc:
                if tentativa < tentativas and relogio.cabe(3 * tentativa):
                    relogio.dormir(3 * tentativa)
                    continue
                raise ErroGraph(0, "sem_rede", str(exc.reason), url) from exc
            except ERROS_DE_LEITURA as exc:
                if tentativa < tentativas and relogio.cabe(3 * tentativa):
                    relogio.dormir(3 * tentativa)
                    continue
                raise ErroGraph(0, "sem_rede", f"{type(exc).__name__}: {exc}", url) from exc
        raise ErroGraph(0, "tentativas_esgotadas", "", url)

    def paginar(self, caminho: str):
        """Percorre uma colecao inteira (@odata.nextLink)."""
        pagina = self.get(caminho)
        while True:
            for item in pagina.get("value", []):
                yield item
            proxima = pagina.get("@odata.nextLink")
            if not proxima:
                return
            pagina = self.get(proxima)

    # -- leitura do site -------------------------------------------------
    def site_id(self) -> str:
        return self.get(f"/sites/{self.host}:{self.site}")["id"]

    def drives(self, site_id: str) -> list[dict]:
        return list(self.paginar(f"/sites/{site_id}/drives"))

    def listas(self, site_id: str) -> list[dict]:
        return list(self.paginar(f"/sites/{site_id}/lists"))

    def raiz_do_drive(self, drive_id: str) -> dict:
        return self.get(f"/drives/{drive_id}/root")

    def filhos(self, drive_id: str, item_id: str = "root", pagina: int = 999):
        """Itens de uma pasta, paginando, so com os campos do inventario."""
        alvo = "root" if item_id == "root" else f"items/{item_id}"
        return self.paginar(
            f"/drives/{drive_id}/{alvo}/children?$top={pagina}&$select={CAMPOS_ITEM}")

    def item_por_caminho(self, drive_id: str, caminho_rel: str) -> dict:
        """Resolve um caminho ('pasta/sub/arquivo.pdf') para um driveItem.

        Codifica segmento por segmento com `safe=""`. Sem isso, nome de
        arquivo com '%', '#' ou '?' — que existem no acervo, ex.:
        'RD-103 PLANILHA 20%A.xlsx' — vira URL invalida (HTTP 400).
        """
        caminho = caminho_rel.replace("\\", "/").strip("/")
        if not caminho:
            return self.raiz_do_drive(drive_id)
        codificado = "/".join(urllib.parse.quote(parte, safe="")
                              for parte in caminho.split("/"))
        # URL completa: o `get()` nao recodifica o que ja vem pronto.
        return self.get(f"{GRAPH}/drives/{drive_id}/root:/{codificado}:"
                        f"?$select={CAMPOS_ITEM}")

    def delta(self, drive_id: str, token: str | None = None):
        """Delta query: devolve (itens, proximo_token).

        Sem token, faz a varredura inicial completa. Com token, traz so o
        que mudou desde a ultima vez — inclusive exclusoes (`deleted`).
        """
        caminho = (token if token and token.startswith("http")
                   else f"/drives/{drive_id}/root/delta"
                        + (f"?token={urllib.parse.quote(token)}" if token else ""))
        itens = []
        while True:
            pagina = self.get(caminho)
            itens.extend(pagina.get("value", []))
            if pagina.get("@odata.nextLink"):
                caminho = pagina["@odata.nextLink"]
                continue
            return itens, pagina.get("@odata.deltaLink", "")

    def delta_paginado(self, drive_id: str, token: str | None = None):
        """Igual ao delta, mas entrega pagina a pagina.

        Gera (itens_da_pagina, delta_link_ou_None). Serve para gravar o
        inventario a cada pagina, sem segurar 139 mil itens na memoria.
        """
        caminho = (token if token and token.startswith("http")
                   else f"/drives/{drive_id}/root/delta"
                        + (f"?token={urllib.parse.quote(token)}" if token else ""))
        while True:
            pagina = self.get(caminho)
            yield pagina.get("value", []), pagina.get("@odata.deltaLink")
            proxima = pagina.get("@odata.nextLink")
            if not proxima:
                return
            caminho = proxima

    def versoes(self, drive_id: str, item_id: str) -> list[dict]:
        """Historico de versoes do item (chamada extra: usar sob demanda)."""
        return list(self.paginar(f"/drives/{drive_id}/items/{item_id}/versions"))

    def permissoes_do_site(self, site_id: str) -> list[dict]:
        return list(self.paginar(f"/sites/{site_id}/permissions"))

    # -- conteudo ---------------------------------------------------------
    def baixar(self, drive_id: str, item_id: str, destino: Path | str,
               tamanho_max_bytes: int | None = None, tentativas: int = 4,
               prazo_s: float | None = None, timeout_leitura_s: float = 120.0,
               deve_parar=None) -> int:
        """Baixa o conteudo para `destino`. Devolve os bytes gravados.

        UMA chamada ao Graph por download (M-13): `GET .../content`, que
        responde 302 para a URL pre-autenticada do CDN. O token vai como
        cabecalho nao redirecionavel — chega ao Graph e NAO segue o 302,
        entao a credencial nao vaza para o CDN. Antes eram duas chamadas
        (`?$select=...downloadUrl` + o conteudo): ~122 mil em vez de ~61 mil
        para a fila, o dobro de quota e de chance de 429.

        Fallback: se o `/content` responder 401/403, pede os metadados com
        `@microsoft.graph.downloadUrl` (o caminho antigo) e baixa por ele,
        sem token. Tamanho e hash continuam conferidos pela `FonteGraph`
        contra o que o catalogo ja sabe; aqui, o `Content-Length` e o
        streaming respeitam `tamanho_max_bytes`. Continua sendo so GET.

        `prazo_s` e o teto TOTAL do download (tentativas e esperas somadas):
        estourou, `ErroGraph(0, 'tempo_esgotado')`. `timeout_leitura_s` e o
        maximo sem receber um byte. `deve_parar()` interrompe esperas e
        leitura com `ErroGraph(0, 'interrompido')`. O arquivo parcial fica em
        `destino` — quem chamou apaga (a `FonteGraph` apaga no `finally`).
        """
        destino = Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        relogio = Prazo(prazo_s, deve_parar)
        direto = GRAPH + f"/drives/{drive_id}/items/{item_id}/content"
        modo_metadados = False    # vira True se o /content responder 401/403
        url_cdn = None

        for tentativa in range(1, tentativas + 1):
            relogio.checar(consultar_sempre=True)
            if modo_metadados and not url_cdn:
                # O caminho antigo: metadados com a URL pre-autenticada.
                meta = self.get(f"/drives/{drive_id}/items/{item_id}"
                                f"?$select=id,size,@microsoft.graph.downloadUrl",
                                prazo=relogio)
                if tamanho_max_bytes and int(meta.get("size") or 0) > tamanho_max_bytes:
                    raise ErroGraph(0, "arquivo_grande",
                                    f"{meta.get('size')} bytes acima do teto "
                                    f"{tamanho_max_bytes}")
                url_cdn = meta.get("@microsoft.graph.downloadUrl")
                if not url_cdn:
                    raise ErroGraph(0, "sem_download_url",
                                    "o Graph nao devolveu @microsoft.graph.downloadUrl")
            self._ritmo()
            try:
                requisicao = (self._requisicao(url_cdn, autenticar=False, aceitar="*/*")
                              if url_cdn else
                              self._requisicao(direto, aceitar="*/*",
                                               token_no_redirect=False))
                with urllib.request.urlopen(
                        requisicao, timeout=relogio.timeout(timeout_leitura_s)) as resposta:
                    anunciado = int(resposta.headers.get("Content-Length") or 0)
                    if tamanho_max_bytes and anunciado > tamanho_max_bytes:
                        raise ErroGraph(0, "arquivo_grande",
                                        f"{anunciado} bytes acima do teto {tamanho_max_bytes}")
                    gravados = self._gravar_corpo(resposta, destino, relogio,
                                                  tamanho_max_bytes)
                    self.chamadas += 1
                    self.bytes_baixados += gravados
                    return gravados
            except urllib.error.HTTPError as exc:
                bruto = exc.read().decode("utf-8", "replace")
                codigo, mensagem = _detalhar(bruto, exc)
                if exc.code in (401, 403) and not modo_metadados and tentativa < tentativas:
                    modo_metadados = True     # tenta uma vez pelo caminho antigo
                    continue
                if exc.code in (429, 500, 502, 503, 504) and tentativa < tentativas:
                    espera = min(float(exc.headers.get("Retry-After", 5 * tentativa)), 120)
                    if relogio.cabe(espera):
                        self.esperas_429 += 1
                        relogio.dormir(espera)
                        url_cdn = None    # a URL pre-autenticada pode ter expirado
                        continue
                raise ErroGraph(exc.code, codigo, mensagem) from exc
            except urllib.error.URLError as exc:
                if tentativa < tentativas and relogio.cabe(3 * tentativa):
                    relogio.dormir(3 * tentativa)
                    continue
                raise ErroGraph(0, "sem_rede", str(exc.reason)) from exc
            except ERROS_DE_LEITURA as exc:
                # Conexao aberta e leitura parada (servidor mudo alem do
                # `timeout_leitura_s`) ou cortada no meio: e rede, nao disco.
                if tentativa < tentativas and relogio.cabe(3 * tentativa):
                    relogio.dormir(3 * tentativa)
                    continue
                raise ErroGraph(0, "sem_rede",
                                f"leitura interrompida: {type(exc).__name__}: {exc}") from exc
        raise ErroGraph(0, "tentativas_esgotadas", "download")

    @staticmethod
    def _gravar_corpo(resposta, destino: Path, relogio: Prazo,
                      tamanho_max_bytes: int | None) -> int:
        """Copia a resposta para o disco, conferindo prazo e parada a cada pedaco.

        `read1` devolve assim que chega QUALQUER coisa (ate 1 MB), entao o
        prazo e conferido mesmo quando o servidor pinga bytes devagar — com
        `read(4 MB)` a leitura so voltaria quando juntasse 4 MB.
        """
        gravados = 0
        ler = getattr(resposta, "read1", None) or resposta.read
        with open(_lp(destino), "wb") as fh:
            while True:
                relogio.checar()
                pedaco = ler(1024 * 1024)
                if not pedaco:
                    break
                fh.write(pedaco)
                gravados += len(pedaco)
                if tamanho_max_bytes and gravados > tamanho_max_bytes:
                    raise ErroGraph(0, "arquivo_grande",
                                    f"passou de {tamanho_max_bytes} bytes durante o download")
        return gravados


# ----------------------------------------------------------------------
# Auxiliares
# ----------------------------------------------------------------------

def _lp(caminho: Path | str) -> str:
    """Prefixo de caminho longo, para o tmp/ nao esbarrar no MAX_PATH."""
    texto = str(caminho)
    if os.name == "nt" and not texto.startswith("\\\\?\\"):
        texto = "\\\\?\\" + os.path.abspath(texto)
    return texto


def _detalhar(bruto: str, exc) -> tuple[str, str]:
    try:
        erro = json.loads(bruto).get("error", {})
        return erro.get("code", "desconhecido"), erro.get("message", bruto)[:400]
    except json.JSONDecodeError:
        return "desconhecido", (bruto or str(exc))[:400]


def caminho_do_item(item: dict) -> str:
    """Caminho relativo a raiz do drive, com '\\', como o banco guarda."""
    pai = (item.get("parentReference") or {}).get("path", "")
    # path vem como '/drives/<id>/root:/pasta/sub'
    pasta = pai.split("root:", 1)[1] if "root:" in pai else ""
    pasta = urllib.parse.unquote(pasta).strip("/")
    nome = item.get("name", "")
    inteiro = f"{pasta}/{nome}" if pasta else nome
    return inteiro.replace("/", "\\")


def hash_do_item(item: dict) -> str | None:
    """quickXorHash calculado pelo servidor. Duplicata sem baixar nada."""
    return ((item.get("file") or {}).get("hashes") or {}).get("quickXorHash")


def explicar(erro: ErroGraph) -> str:
    if erro.status == 401 or erro.codigo == "falha_autenticacao":
        return ("A credencial nao foi aceita. Conferir Tenant/Client ID e o segredo "
                "no .env — ou o segredo expirou/foi revogado.")
    if erro.status == 403:
        return ("Autenticou, mas o aplicativo nao tem acesso a este recurso. "
                "A TI precisa liberar o app no site (Sites.Selected).")
    if erro.status == 404:
        return "Autenticou, mas o site/caminho nao existe. Conferir host e site no .env."
    if erro.status == 429:
        return "O Graph limitou a taxa. Diminuir o ritmo (pausa_minima_s) e repetir."
    return erro.mensagem
