"""
test_graph_ro — garante que a porta para a nuvem e mesmo so de leitura,
e que ela funciona.

    python tests\\test_graph_ro.py              # tudo (a parte de rede exige .env)
    python tests\\test_graph_ro.py --sem-rede   # so as checagens locais

A prova tem tres camadas, e as duas primeiras rodam SEM rede e SEM .env —
a falta de credencial nao pode disfarcar cobertura perdida (T-15 / A-7):

  1. **Codigo (AST).** O `graph_ro.py` so pode sair para a rede por
     `urlopen(<Request montado por self._requisicao>)`; o unico `Request`
     com corpo e o login do Entra ID; nada de `http.client`, `requests`,
     `socket`, `subprocess`, `build_opener`, troca de `.method`/`.data`
     depois de montado, nem alias para `urlopen`. O verificador e testado
     contra fontes sinteticas com cada vetor conhecido — se ele deixar de
     pegar um, o teste falha (o verificador nao pode ser vacuo).
  2. **Comportamento (transporte falso).** `urllib.request.urlopen` e
     trocado por um gravador, e o socket e bloqueado. TODO metodo publico de
     `GraphRO` e exercitado; cada requisicao que nao seja o login tem de ser
     GET, sem corpo, para graph.microsoft.com ou para a URL de download que
     o proprio Graph devolveu. Metodo publico novo sem roteiro aqui = FAIL.
  3. **Rede** (opcional, exige `.env`): autentica, lista e baixa um arquivo
     pequeno para `tmp/` e apaga.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import shutil
import socket
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import RAIZ, checar, placar_final  # noqa: E402

import graph_ro  # noqa: E402

FONTE = RAIZ / "scripts" / "graph_ro.py"
LOGIN = "https://login.microsoftonline.com/"

# So a biblioteca padrao estritamente necessaria. Qualquer outro modulo de
# rede ou de processo (http.client, requests, socket, subprocess, ...) e
# vetor de escrita que o resto da prova nao enxergaria.
IMPORTS_PERMITIDOS = {"__future__", "json", "os", "time", "urllib.error", "urllib.parse",
                      "urllib.request", "pathlib"}
EXCECOES_HTTP_CLIENT = {"HTTPException", "IncompleteRead", "RemoteDisconnected"}
NOMES_PROIBIDOS = {"build_opener", "install_opener", "OpenerDirector", "HTTPHandler",
                   "HTTPSHandler", "HTTPConnection", "HTTPSConnection", "add_data",
                   "get_method", "__import__", "eval", "exec", "compile", "import_module"}
ENV_FALSO = {"GRAPH_TENANT_ID": "tenant-falso", "GRAPH_CLIENT_ID": "cliente-falso",
             "GRAPH_CLIENT_SECRET": "segredo-falso"}


# ----------------------------------------------------------------------
# 1. O codigo nao pode escrever (AST)
# ----------------------------------------------------------------------

def _nome_final(no: ast.AST) -> str:
    if isinstance(no, ast.Name):
        return no.id
    if isinstance(no, ast.Attribute):
        return no.attr
    return ""


def _e_chamada_requisicao(no: ast.AST) -> bool:
    """`self._requisicao(...)` — a unica fabrica de Request autorizada."""
    return (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
            and no.func.attr == "_requisicao"
            and isinstance(no.func.value, ast.Name) and no.func.value.id == "self")


def _so_requisicao(no: ast.AST) -> bool:
    """A expressao so pode produzir Request vindo de `self._requisicao`."""
    if _e_chamada_requisicao(no):
        return True
    if isinstance(no, ast.IfExp):
        return _so_requisicao(no.body) and _so_requisicao(no.orelse)
    return False


def _vem_da_guarda(expr: ast.AST, f, funcao_de, arvore, profundidade: int) -> bool:
    """O Request que chega ao urlopen nasceu em `self._requisicao`?

    Aceita: a chamada direta; um nome atribuido (so) a partir dela na mesma
    funcao; ou um PARAMETRO de um metodo auxiliar (ex.: `_abrir(self, req)`,
    caso alguem centralize o timeout), desde que TODA chamada a esse metodo
    passe, naquela posicao, algo que tambem venha da guarda.
    """
    if _so_requisicao(expr):
        return True
    if not isinstance(expr, ast.Name) or f is None:
        return False
    atribs = [a for a in ast.walk(f) if isinstance(a, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == expr.id for t in a.targets)]
    if atribs:
        return all(_so_requisicao(a.value) for a in atribs)
    parametros = [a.arg for a in f.args.args]
    if expr.id not in parametros or profundidade <= 0:
        return False
    posicao = parametros.index(expr.id) - (1 if parametros[:1] == ["self"] else 0)
    chamadas = [c for c in ast.walk(arvore) if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute) and c.func.attr == f.name]
    if not chamadas:
        return False
    for c in chamadas:
        kws = {k.arg: k.value for k in c.keywords}
        valor = kws.get(expr.id, c.args[posicao] if 0 <= posicao < len(c.args) else None)
        if valor is None or not _vem_da_guarda(valor, funcao_de(c), funcao_de, arvore,
                                               profundidade - 1):
            return False
    return True


def violacoes_de_escrita(fonte: str) -> list[str]:
    """Toda saida de rede do modulo tem de passar pela guarda de GET."""
    arvore = ast.parse(fonte)
    v: list[str] = []
    funcoes = [f for f in ast.walk(arvore) if isinstance(f, (ast.FunctionDef,
                                                               ast.AsyncFunctionDef))]

    def funcao_de(no: ast.AST):
        dono = None
        for f in funcoes:
            if any(n is no for n in ast.walk(f)):
                if dono is None or any(n is f for n in ast.walk(dono)):
                    dono = f          # a mais interna
        return dono

    # a) imports
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            for a in no.names:
                if a.name not in IMPORTS_PERMITIDOS:
                    v.append(f"import {a.name} (linha {no.lineno})")
                if a.asname and a.name.startswith("urllib"):
                    v.append(f"alias de {a.name} (linha {no.lineno})")
        elif isinstance(no, ast.ImportFrom):
            # De http.client so se importam CLASSES DE EXCECAO (para tratar
            # leitura interrompida). Conexao, nunca.
            if no.module == "http.client":
                for a in no.names:
                    if a.name not in EXCECOES_HTTP_CLIENT or a.asname:
                        v.append(f"from http.client import {a.name} (linha {no.lineno})")
                continue
            if (no.module or "") not in IMPORTS_PERMITIDOS:
                v.append(f"from {no.module} import ... (linha {no.lineno})")
            for a in no.names:
                if a.asname and a.name in ("urlopen", "Request"):
                    v.append(f"alias {a.asname} para {a.name} (linha {no.lineno})")

    # b) nomes que abrem outro caminho de rede, ou trocam o metodo
    for no in ast.walk(arvore):
        nome = _nome_final(no)
        if nome in NOMES_PROIBIDOS:
            v.append(f"uso de {nome} (linha {no.lineno})")
        if isinstance(no, ast.FunctionDef) and no.name in ("get_method",):
            v.append(f"def {no.name} sobrescreve o metodo HTTP (linha {no.lineno})")

    # c) ninguem troca metodo/corpo de um Request depois de montado
    for no in ast.walk(arvore):
        alvos = []
        if isinstance(no, ast.Assign):
            alvos = no.targets
        elif isinstance(no, (ast.AugAssign, ast.AnnAssign)):
            alvos = [no.target]
        for alvo in alvos:
            if isinstance(alvo, ast.Attribute) and alvo.attr in ("method", "data", "_data"):
                v.append(f"atribuicao a .{alvo.attr} (linha {no.lineno})")
        if isinstance(no, ast.Call) and _nome_final(no.func) == "setattr":
            v.append(f"setattr dinamico (linha {no.lineno})")

    # d) `urlopen` so como chamada direta — nada de guardar a funcao num nome
    chamadas_urlopen = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Call) and _nome_final(no.func) == "urlopen":
            chamadas_urlopen.add(id(no.func))
    for no in ast.walk(arvore):
        if _nome_final(no) == "urlopen" and isinstance(no, (ast.Name, ast.Attribute)) \
                and id(no) not in chamadas_urlopen:
            v.append(f"referencia solta a urlopen (alias?) (linha {no.lineno})")

    # e) cada urlopen: so `timeout=` como extra; o Request vem da guarda
    for no in ast.walk(arvore):
        if not (isinstance(no, ast.Call) and _nome_final(no.func) == "urlopen"):
            continue
        f = funcao_de(no)
        if len(no.args) != 1:
            v.append(f"urlopen com {len(no.args)} argumentos posicionais — o 2o e o corpo "
                     f"(linha {no.lineno})")
        extras = {k.arg for k in no.keywords} - {"timeout"}
        if extras:
            v.append(f"urlopen com {sorted(extras)} (linha {no.lineno})")
        if f is not None and f.name == "token":
            continue                  # o login: conferido em (f)
        if not no.args:
            continue
        arg = no.args[0]
        if _vem_da_guarda(arg, f, funcao_de, arvore, profundidade=2):
            continue
        v.append(f"urlopen fora da guarda: {ast.unparse(arg)[:60]} (linha {no.lineno})")

    # f) Request: so em `_requisicao` (GET, sem corpo) e no login
    for no in ast.walk(arvore):
        if not (isinstance(no, ast.Call) and _nome_final(no.func) == "Request"):
            continue
        f = funcao_de(no)
        kws = {k.arg: k.value for k in no.keywords}
        if f is not None and f.name == "_requisicao":
            metodo = kws.get("method")
            if metodo is None or ast.unparse(metodo) not in ("METODO_PERMITIDO", "'GET'",
                                                             '"GET"'):
                v.append(f"Request em _requisicao sem method=GET (linha {no.lineno})")
            if "data" in kws or len(no.args) > 1:
                v.append(f"Request em _requisicao com corpo (linha {no.lineno})")
            continue
        if f is not None and f.name == "token":
            url = no.args[0] if no.args else kws.get("url")
            ok = False
            if isinstance(url, ast.Name):
                atribs = [a for a in ast.walk(f) if isinstance(a, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == url.id
                                  for t in a.targets)]
                ok = bool(atribs) and all(
                    isinstance(a.value, ast.JoinedStr) and a.value.values
                    and isinstance(a.value.values[0], ast.Constant)
                    and str(a.value.values[0].value).startswith(LOGIN) for a in atribs)
            if not ok:
                v.append(f"Request no login nao aponta para {LOGIN} (linha {no.lineno})")
            continue
        v.append(f"Request fora de _requisicao/token (linha {no.lineno})")

    # g) a constante da guarda continua sendo GET, e so ela
    atribs = [n for n in ast.walk(arvore) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "METODO_PERMITIDO" for t in n.targets)]
    if len(atribs) != 1 or not (isinstance(atribs[0].value, ast.Constant)
                                and atribs[0].value.value == "GET"):
        v.append("METODO_PERMITIDO nao e exatamente uma atribuicao = 'GET'")
    return v


# Fontes sinteticas: o verificador TEM de pegar cada uma. Se uma passar, o
# verificador ficou vacuo e o teste falha — e assim que ele se prova.
_BASE = '''
import urllib.request
METODO_PERMITIDO = "GET"
class GraphRO:
    def _requisicao(self, url, metodo="GET"):
        return urllib.request.Request(url, headers={}, method=METODO_PERMITIDO)
    def get(self, url):
        with urllib.request.urlopen(self._requisicao(url), timeout=120) as r:
            return r.read()
'''
VETORES = {
    "urlopen(url, data=...)": "    def x(self, u):\n        return urllib.request.urlopen(u, data=b'x')\n",
    "urlopen(req, b'corpo') posicional":
        "    def x(self, u):\n        return urllib.request.urlopen(self._requisicao(u), b'x')\n",
    "urlopen(guarda, data=...)":
        "    def x(self, u):\n        return urllib.request.urlopen(self._requisicao(u), data=b'x')\n",
    "urlopen com string crua": "    def x(self, u):\n        return urllib.request.urlopen(u, timeout=5)\n",
    "Request(method='PUT')":
        "    def x(self, u):\n        return urllib.request.Request(u, method='PUT')\n",
    "req.method = 'POST' depois":
        "    def x(self, u):\n        r = self._requisicao(u)\n        r.method = 'POST'\n        return r\n",
    "req.data = corpo depois":
        "    def x(self, u):\n        r = self._requisicao(u)\n        r.data = b'x'\n        return r\n",
    "requests.post": "    def x(self, u):\n        import requests\n        return requests.post(u)\n",
    "http.client PUT":
        "    def x(self, u):\n        import http.client\n"
        "        return http.client.HTTPSConnection('h').request('PUT', u)\n",
    "subprocess curl -X DELETE":
        "    def x(self, u):\n        import subprocess\n"
        "        return subprocess.run(['curl', '-X', 'DELETE', u])\n",
    "socket cru": "    def x(self, u):\n        import socket\n        return socket.create_connection((u, 443))\n",
    "build_opener": "    def x(self, u):\n        return urllib.request.build_opener().open(u, b'x')\n",
    "alias de urlopen": "    def x(self, u):\n        abrir = urllib.request.urlopen\n        return abrir(u, b'x')\n",
    "from urllib.request import urlopen as abrir":
        "    def x(self, u):\n        from urllib.request import urlopen as abrir\n        return abrir(u)\n",
    "get_method sobrescrito":
        "    def x(self, u):\n        r = self._requisicao(u)\n"
        "        r.get_method = lambda: 'DELETE'\n        return r\n",
    "__import__ dinamico": "    def x(self, u):\n        return __import__('requests').put(u)\n",
    "auxiliar chamado com URL crua":
        "    def _abrir(self, req):\n        return urllib.request.urlopen(req, timeout=30)\n"
        "    def x(self, u):\n        return self._abrir(u)\n",
}
LEGITIMOS = {
    "timeout vindo de atributo (Agente A)":
        "    def y(self, u):\n        with urllib.request.urlopen(self._requisicao(u), "
        "timeout=self.timeout_s) as r:\n            return r.read()\n",
    "Request numa variavel, depois urlopen":
        "    def y(self, u, a):\n        req = (self._requisicao(u) if a else self._requisicao(u + 'x'))\n"
        "        with urllib.request.urlopen(req, timeout=600) as r:\n            return r.read()\n",
    "auxiliar que centraliza o timeout":
        "    def _abrir(self, req):\n        return urllib.request.urlopen(req, timeout=self.t)\n"
        "    def y(self, u):\n        return self._abrir(self._requisicao(u))\n",
}


# ----------------------------------------------------------------------
# 2. O comportamento nao pode escrever (transporte falso, sem rede)
# ----------------------------------------------------------------------

class Resposta:
    def __init__(self, corpo: bytes):
        self._corpo = corpo
        self._pos = 0
        # `baixar()` confere o tamanho anunciado contra o recebido (T-18).
        self.headers = {"Content-Length": str(len(corpo))}

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._corpo) - self._pos
        pedaco = self._corpo[self._pos:self._pos + n]
        self._pos += len(pedaco)
        return pedaco

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TransporteFalso:
    """Faz o papel de `urllib.request.urlopen`. Grava tudo, responde roteirizado."""

    def __init__(self):
        self.login: list[dict] = []
        self.requisicoes: list[dict] = []

    def __call__(self, req, data=None, timeout=None, **kw):
        if isinstance(req, str):                       # urlopen(url_crua)
            req = urllib.request.Request(req)
        if data is not None:                           # o que o urlopen faria
            req.data = data
        reg = {"url": req.full_url, "metodo": req.get_method(), "corpo": req.data,
               "extras": sorted(kw)}
        if req.full_url.startswith(LOGIN):
            self.login.append(reg)
            return Resposta(json.dumps({"access_token": "tok", "expires_in": 3600}).encode())
        self.requisicoes.append(reg)
        url = urllib.parse.unquote(req.full_url)
        if url.startswith("https://cdn.falso/"):
            return Resposta(b"conteudo-falso")
        if "downloadUrl" in url:
            return Resposta(json.dumps({"id": "i", "size": 14,
                                        "@microsoft.graph.downloadUrl":
                                            "https://cdn.falso/arquivo?sig=1"}).encode())
        if url.endswith("/pagina2"):
            return Resposta(json.dumps({"value": [{"id": "b"}],
                                        "@odata.deltaLink": "https://graph.microsoft.com"
                                                            "/v1.0/delta?token=fim"}).encode())
        if "/delta" in url or "/children" in url or url.rstrip("/").endswith(
                ("/drives", "/lists", "/versions", "/permissions")):
            return Resposta(json.dumps({"value": [{"id": "a"}], "@odata.nextLink":
                                        "https://graph.microsoft.com/v1.0/pagina2"}).encode())
        return Resposta(json.dumps({"id": "site,1,2", "value": []}).encode())


def roteiros(graph: graph_ro.GraphRO, tmp: Path) -> dict:
    """Uma chamada para CADA metodo publico. Metodo novo sem roteiro = FAIL."""
    return {
        "token": lambda: graph.token(),
        "get": lambda: graph.get("/sites/root"),
        "paginar": lambda: list(graph.paginar("/drives/d/root/children")),
        "site_id": lambda: graph.site_id(),
        "drives": lambda: graph.drives("site"),
        "listas": lambda: graph.listas("site"),
        "raiz_do_drive": lambda: graph.raiz_do_drive("d"),
        "filhos": lambda: list(graph.filhos("d", "pasta")),
        "item_por_caminho": lambda: graph.item_por_caminho("d", r"pasta\arq 20%A.pdf"),
        "delta": lambda: graph.delta("d"),
        "delta_paginado": lambda: list(graph.delta_paginado("d", "tok")),
        "versoes": lambda: graph.versoes("d", "i"),
        "permissoes_do_site": lambda: graph.permissoes_do_site("site"),
        "baixar": lambda: graph.baixar("d", "i", tmp / "baixado.bin"),
    }


def comportamento() -> None:
    print("\n--- comportamento: transporte falso, rede bloqueada ---")
    falso = TransporteFalso()
    original_urlopen = urllib.request.urlopen
    original_modulo = getattr(graph_ro, "urlopen", None)
    original_conectar = socket.socket.connect
    original_criar = socket.create_connection

    def sem_rede(*a, **k):
        raise AssertionError("tentativa de rede REAL durante o teste offline")

    tmp = Path(tempfile.mkdtemp(prefix="cerebro_graphro_"))
    erros: dict[str, str] = {}
    graph = graph_ro.GraphRO(dict(ENV_FALSO))
    publicos = sorted(n for n, f in inspect.getmembers(graph_ro.GraphRO, inspect.isfunction)
                      if not n.startswith("_"))
    roteiro = roteiros(graph, tmp)
    try:
        urllib.request.urlopen = falso
        if original_modulo is not None:
            graph_ro.urlopen = falso
        socket.socket.connect = sem_rede
        socket.create_connection = sem_rede
        for nome in publicos:
            if nome not in roteiro:
                continue
            try:
                roteiro[nome]()
            except Exception as exc:                  # noqa: BLE001
                erros[nome] = f"{type(exc).__name__}: {exc}"[:160]
    finally:
        urllib.request.urlopen = original_urlopen
        if original_modulo is not None:
            graph_ro.urlopen = original_modulo
        socket.socket.connect = original_conectar
        socket.create_connection = original_criar
        shutil.rmtree(tmp, ignore_errors=True)

    sem_roteiro = [n for n in publicos if n not in roteiro]
    checar(not sem_roteiro, "todo metodo publico de GraphRO tem roteiro neste teste",
           f"sem roteiro: {sem_roteiro}")
    checar(not erros, "todos os metodos publicos rodaram contra o transporte falso",
           str(erros))
    checar(len(falso.requisicoes) >= len(publicos),
           f"o transporte falso foi de fato usado ({len(falso.requisicoes)} requisicoes)")
    nao_get = [(r["metodo"], r["url"][:80]) for r in falso.requisicoes if r["metodo"] != "GET"]
    checar(not nao_get, "toda requisicao de documento e GET", str(nao_get))
    com_corpo = [r["url"][:80] for r in falso.requisicoes if r["corpo"] is not None]
    checar(not com_corpo, "nenhuma requisicao de documento leva corpo", str(com_corpo))
    destinos = {urllib.parse.urlsplit(r["url"]).netloc for r in falso.requisicoes}
    checar(destinos <= {"graph.microsoft.com", "cdn.falso"},
           "so fala com o Graph e com a URL de download que o Graph devolveu",
           str(destinos))
    checar(len(falso.login) == 1 and all(r["url"].startswith(LOGIN) for r in falso.login),
           "o unico pedido com corpo e o login do Entra ID (e so uma vez: token em cache)",
           str([(r["metodo"], r["url"][:60]) for r in falso.login]))
    checar(all(r["extras"] == [] for r in falso.requisicoes + falso.login),
           "urlopen recebe so request e timeout",
           str({tuple(r["extras"]) for r in falso.requisicoes}))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-rede", action="store_true")
    args = ap.parse_args()

    print("\n--- codigo: o verificador nao e vacuo ---")
    for rotulo, trecho in VETORES.items():
        achou = violacoes_de_escrita(_BASE + trecho)
        checar(bool(achou), f"o verificador pega: {rotulo}", "PASSOU SEM SER VISTO")
    for rotulo, trecho in LEGITIMOS.items():
        achou = violacoes_de_escrita(_BASE + trecho)
        checar(not achou, f"o verificador aceita: {rotulo}", "; ".join(achou))

    print("\n--- codigo: o graph_ro.py so sai pela guarda de GET ---")
    fonte = FONTE.read_text(encoding="utf-8")
    violacoes = violacoes_de_escrita(fonte)
    checar(not violacoes, "nenhuma saida de rede fora de urlopen(self._requisicao(...))",
           "; ".join(violacoes))
    for proibido in ("createUploadSession", "/content:", "copy()", "invite("):
        checar(proibido not in fonte, f"nao ha chamada de escrita '{proibido}'")
    cliente = graph_ro.GraphRO.__dict__
    escrita = [n for n in cliente if any(p in n.lower() for p in
               ("upload", "criar", "apagar", "excluir", "mover", "renomear", "escrever",
                "enviar", "post", "patch", "delete"))]
    checar(not escrita, "a classe nao expoe metodo de escrita", str(escrita))

    print("\n--- a guarda de metodo funciona (sem rede, sem .env) ---")
    graph = graph_ro.GraphRO(dict(ENV_FALSO))
    for metodo in ("POST", "PATCH", "PUT", "DELETE", "get", "HEAD"):
        try:
            graph._requisicao("https://graph.microsoft.com/v1.0/sites", metodo=metodo,
                              autenticar=False)
            checar(False, f"{metodo} e bloqueado em tempo de execucao")
        except PermissionError:
            checar(True, f"{metodo} e bloqueado em tempo de execucao")
    req = graph._requisicao("https://graph.microsoft.com/v1.0/sites", autenticar=False)
    checar(req.get_method() == "GET" and req.data is None,
           "a guarda monta GET sem corpo", f"{req.get_method()} {req.data!r}")

    comportamento()

    print("\n--- leitura de metadados (sem rede) ---")
    item = {"name": "notificacao 001.pdf", "size": 1234,
            "parentReference": {"path": "/drives/abc/root:/3. Notifica%C3%A7%C3%B5es/2026"},
            "file": {"hashes": {"quickXorHash": "ABC123=="}}}
    checar(graph_ro.caminho_do_item(item) == "3. Notificações\\2026\\notificacao 001.pdf",
           "caminho_do_item reconstroi o caminho com acento e '\\'",
           graph_ro.caminho_do_item(item))
    checar(graph_ro.hash_do_item(item) == "ABC123==", "hash_do_item le o quickXorHash")
    checar(graph_ro.hash_do_item({"file": {}}) is None, "item sem hash devolve None")
    checar(graph_ro.caminho_do_item({"name": "raiz.txt", "parentReference":
                                     {"path": "/drives/abc/root:"}}) == "raiz.txt",
           "arquivo na raiz do drive nao ganha barra sobrando")
    # Regressao: nome com '%' existe no acervo ("... 20%A.xlsx") e derrubava
    # a URL com HTTP 400 quando o '%' passava cru.
    urls = []
    graph.get = lambda caminho, **kw: urls.append(caminho) or {"id": "x"}
    graph.item_por_caminho("drive!1", r"pasta\sub\arq 20%A #2?.xlsx")
    del graph.get
    checar(urls and "%25A" in urls[0] and "%23" in urls[0] and "%3F" in urls[0],
           "caminho com % # ? e codificado segmento a segmento",
           urls[0][:120] if urls else "sem url")
    checar(urls and "/root:/pasta/sub/" in urls[0],
           "as barras de pasta nao sao codificadas", urls[0][:120] if urls else "")

    checar(graph_ro.erro_transitorio(graph_ro.ErroGraph(429, "activityLimitReached", "")),
           "429 e tratado como transitorio")
    checar(not graph_ro.erro_transitorio(graph_ro.ErroGraph(404, "itemNotFound", "")),
           "404 nao e transitorio")

    if args.sem_rede:
        return placar_final()

    print("\n--- rede: credencial ---")
    env = graph_ro.carregar_env()
    tem_credencial = all(env.get(c) for c in
                         ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"))
    checar(tem_credencial, ".env deste projeto tem as tres chaves obrigatorias",
           "sem .env: rode com --sem-rede para so as checagens locais")
    if not tem_credencial:
        return placar_final()
    graph = graph_ro.GraphRO(env)

    print("\n--- rede: o acesso funciona ---")
    try:
        site = graph.site_id()
        checar(bool(site) and "," in site, "autenticou e resolveu o site", site[:40])
        drives = graph.drives(site)
        checar(len(drives) >= 1, f"listou {len(drives)} drive(s)")
        alvo = next((d for d in drives if d.get("name") == "Programa"), drives[0])
        filhos = list(graph.filhos(alvo["id"]))
        checar(len(filhos) >= 1, f"listou {len(filhos)} itens na raiz de '{alvo['name']}'")
        com_hash = [f for f in filhos if graph_ro.hash_do_item(f)]
        print(f"    (raiz: {len(filhos)} itens, {len(com_hash)} arquivos com hash)")
    except graph_ro.ErroGraph as exc:
        checar(False, "acesso ao Graph", f"{exc} — {graph_ro.explicar(exc)}")
        return placar_final()

    print("\n--- rede: baixar para tmp e apagar ---")
    try:
        # Busca em largura, com teto de pastas visitadas. A versao anterior so
        # olhava os filhos DIRETOS do primeiro nivel e passou a nao achar nada
        # quando o acervo foi reorganizado — fixture fragil, nao defeito de
        # codigo. Agora desce ate encontrar, sem varrer o acervo inteiro.
        pequeno = None
        fila = [p["id"] for p in filhos if p.get("folder")]
        visitadas = 0
        while fila and pequeno is None and visitadas < 40:
            pasta_id = fila.pop(0)
            visitadas += 1
            for item in graph.filhos(alvo["id"], pasta_id):
                if item.get("file") and 0 < int(item.get("size") or 0) < 2 * 1024 ** 2:
                    pequeno = item
                    break
                if item.get("folder"):
                    fila.append(item["id"])
        if pequeno is None:
            checar(False, "achou um arquivo pequeno para testar o download",
                   f"{visitadas} pasta(s) visitada(s)")
            return placar_final()
        checar(True, f"achou um arquivo pequeno para testar o download "
                     f"({visitadas} pasta(s) visitada(s))")

        destino = RAIZ / "tmp" / f"teste_graph_{pequeno['id'][:12]}"
        gravados = graph.baixar(alvo["id"], pequeno["id"], destino)
        checar(gravados == int(pequeno["size"]),
               f"baixou o arquivo inteiro ({gravados} bytes)",
               f"{gravados} != {pequeno['size']}")
        checar(destino.exists(), "arquivo chegou em tmp/")
        destino.unlink(missing_ok=True)
        checar(not destino.exists(), "tmp/ ficou limpo depois do teste")
        print(f"    (chamadas ao Graph: {graph.chamadas}, esperas por limite: "
              f"{graph.esperas_429})")
    except graph_ro.ErroGraph as exc:
        checar(False, "download pelo Graph", f"{exc} — {graph_ro.explicar(exc)}")

    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
