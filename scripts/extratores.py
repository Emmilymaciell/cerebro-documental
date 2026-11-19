"""
extratores — leitura determinista de conteudo, por formato.

Contrato unico: `extrair(caminho, categoria, cfg) -> dict`.

    status: ok | parcial | ocr_requerido | vazio | protegido |
            nao_suportado | erro
    paginas_texto: [(numero, texto), ...]   -> vira trecho no indice
    + metricas (paginas, slides, abas, imagens, caracteres, palavras)
    + extras opcionais (metadados, tabelas, paginas_ocr, ocr_tipo) — ver
      CHAVES_EXTRAS; quem nao as conhece simplesmente nao as le.

Nenhum extrator levanta excecao para fora: erro vira status. Um arquivo
ruim nunca pode derrubar o worker.

Formato sem extrator devolve `nao_suportado` — o arquivo continua no
catalogo, com o tipo registrado, esperando implementacao futura. Quem
decide isso e `motivo_sem_leitor()`, e o worker a consulta ANTES de baixar
(L7): o que nenhuma biblioteca le nao gasta download.

Formatos paginados (pdf, pptx, xlsx) gravam o `texto` com cada unidade
estrutural (pagina, slide, aba) TERMINADA por form feed (U+000C), na
posicao do seu numero — unidade sem texto e uma string vazia entre dois
`\\f`. E a convencao do `pdftotext`. Assim o derivado `.txt.gz` guarda onde
cada pagina comeca e `busca.paginas_do_derivado()` reconstroi os segmentos
sem o extrator. Ver docs/08_EXTRATORES.md.
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from pathlib import Path

VERSOES = {
    "pdf": "pdf/1.3",
    "docx": "docx/1.1",
    "pptx": "pptx/1.2",
    "xlsx": "xlsx/1.1",
    "csv": "csv/1.1",
    "texto": "texto/1.1",
}

# Formatos antigos (binarios OLE) que as bibliotecas locais nao leem.
EXT_LEGADO = {".doc", ".xls", ".ppt", ".pst", ".dwg", ".dgn"}

# Formatos que caem na mesma categoria do yaml mas nenhuma biblioteca
# instalada abre. Sem isto eles viram `erro` e gastam as 4 tentativas do
# worker antes de parar em ERROR_FINAL — quando o certo e UNSUPPORTED de
# primeira (REGRA 6). `.xlsb` e o binario do Excel 2007+: openpyxl nao le.
EXT_SEM_BIBLIOTECA = {".rtf", ".odt", ".ods", ".odp", ".xlsb"}

# Categorias que leem QUALQUER byte como texto: nenhuma extensao e recusada.
CATEGORIAS_TEXTO_LIVRE = {"csv", "texto"}

# Separador/terminador de unidade estrutural no `texto` dos formatos paginados.
SEPARADOR_UNIDADE = "\f"

# Chaves alem das 14 do contrato. Opcionais: o worker as copia para o perfil
# JSON quando existem; quem nao as conhece nao quebra.
CHAVES_EXTRAS = ("metadados", "tabelas", "paginas_ocr", "ocr_tipo", "ocr_metricas")

# Limites padrao (todos sobrescreviveis em `worker:` do projeto.yaml; o
# padrao mora aqui para um yaml antigo continuar valendo).
PADRAO_PAGINAS_MAX_PDF = 3000
PADRAO_MIN_CARACTERES_POR_PAGINA = 80
PADRAO_OCR_COBERTURA_IMAGEM = 0.5
PADRAO_XLSX_MAX_LINHAS_ABA = 5000
PADRAO_XLSX_MAX_CARACTERES = 4_000_000
PADRAO_MAX_CARACTERES_DOCUMENTO = 20_000_000      # docx e pptx
# Cobertura de imagem so e medida em pagina com imagem e menos caracteres que
# isto (medir custa ~70% do tempo de extrair o texto da pagina). E o teto ate
# onde `min_caracteres_por_pagina` pode ser recalibrado SEM baixar de novo.
PADRAO_OCR_METRICA_MAX_CARACTERES = 1000

# Formato de `ocr_metricas["paginas"]` (pdf/1.3). Uma pagina por item,
# separadas por ';', na ordem (item i = pagina i+1), campos por ',':
#   c        caracteres do texto da pagina (o mesmo `c` da regra de OCR)
#   cob_mil  cobertura de imagem em milesimos (0..1000); vazio = nao medida
#            (pagina com >= ocr_metrica_max_caracteres caracteres)
#   d        1 se a pagina tem conteudo desenhado (fluxo de conteudo nao
#            vazio: vetor, imagem ou texto); 0 = fluxo vazio
#   img      numero de imagens da pagina (`get_images`)
# Pagina que nao carregou: '-'. Uma string so, e nao lista de listas, porque
# o perfil e gravado com `indent=1` e uma lista vira um numero por linha
# (~45 bytes/pagina contra ~9). Ver docs/08_EXTRATORES.md.
FORMATO_OCR_METRICAS = "c,cob_mil,d,img;v1"


def _vazio(extrator: str, status: str, erro: str = "") -> dict:
    return {"status": status, "extrator": extrator, "versao": VERSOES.get(extrator, "?"),
            "texto": "", "paginas_texto": [], "paginas": None, "paginas_com_texto": None,
            "slides": None, "abas": None, "imagens": None, "caracteres": 0, "palavras": 0,
            "densidade_texto": None, "erro": erro,
            "metadados": None, "tabelas": None, "paginas_ocr": None, "ocr_tipo": None,
            "ocr_metricas": None}


def _lp(caminho) -> str:
    texto = str(caminho)
    if os.name == "nt" and not texto.startswith("\\\\?\\"):
        texto = "\\\\?\\" + os.path.abspath(texto)
    return texto


def _limite(cfg: dict, chave: str, padrao):
    valor = (cfg.get("worker") or {}).get(chave, padrao)
    try:
        return type(padrao)(valor)
    except (TypeError, ValueError):
        return padrao


# ----------------------------------------------------------------------
# Quem tem leitor — decisao pura, pela extensao, ANTES do download (L7)
# ----------------------------------------------------------------------

def extensao(caminho) -> str:
    """Extensao em minusculas, com ponto, de um caminho com `\\` ou `/`."""
    nome = re.split(r"[\\/]", str(caminho or ""))[-1]
    ponto = nome.rfind(".")
    return nome[ponto:].lower() if ponto > 0 else ""


def motivo_sem_leitor(ext: str, categoria: str | None) -> str | None:
    """None se o extrator da categoria vai tentar ler esta extensao; senao, o motivo.

    Funcao pura: nao abre arquivo. E a MESMA regra que os extratores aplicam
    depois do download — o worker a usa antes, para nao baixar o que vai
    terminar `nao_suportado` de qualquer jeito.
    """
    if categoria not in EXTRATORES:
        return f"sem extrator para a categoria '{categoria}'"
    if categoria in CATEGORIAS_TEXTO_LIVRE:
        return None
    ext = (ext or "").lower()
    if ext in EXT_LEGADO:
        return f"formato legado {ext}"
    if ext in EXT_SEM_BIBLIOTECA:
        return f"sem biblioteca para {ext}"
    return None


def suporta(ext: str, categoria: str | None) -> bool:
    return motivo_sem_leitor(ext, categoria) is None


def resultado_sem_leitor(ext: str, categoria: str | None, sufixo_erro: str = "") -> dict:
    """O resultado que o extrator daria — sem precisar dos bytes."""
    motivo = motivo_sem_leitor(ext, categoria) or "sem leitor"
    return _vazio(categoria or "?", "nao_suportado", motivo + sufixo_erro)


# ----------------------------------------------------------------------
# Auxiliares comuns
# ----------------------------------------------------------------------

def _limpar_unidade(texto: str) -> str:
    """Texto de uma unidade estrutural: sem nulo e sem o separador de unidade."""
    return (texto or "").replace("\x00", "").replace(SEPARADOR_UNIDADE, "\n").strip()


def _juntar_unidades(unidades: list[str]) -> str:
    """Unidades na ordem do numero (indice 0 = unidade 1), cada uma terminada por \\f.

    Unidade vazia continua ocupando a sua posicao (string vazia entre dois
    \\f) para que o numero sobreviva no derivado; as vazias do FIM sao
    cortadas. Nenhuma unidade com texto -> "".
    """
    ultimo = max((i for i, u in enumerate(unidades) if u), default=-1)
    if ultimo < 0:
        return ""
    return "".join(u + SEPARADOR_UNIDADE for u in unidades[:ultimo + 1])


def _paginas_de_unidades(unidades: list[str]) -> list[tuple[int, str]]:
    return [(i, u) for i, u in enumerate(unidades, start=1) if u]


def _faixas(numeros: list[int], limite: int = 40) -> str:
    """[1,2,3,7,9,10] -> '1-3, 7, 9-10' (cortado em `limite` faixas)."""
    faixas, inicio, anterior = [], None, None
    for n in numeros:
        if inicio is None:
            inicio = anterior = n
        elif n == anterior + 1:
            anterior = n
        else:
            faixas.append((inicio, anterior))
            inicio = anterior = n
    if inicio is not None:
        faixas.append((inicio, anterior))
    texto = ", ".join(f"{a}-{b}" if a != b else f"{a}" for a, b in faixas[:limite])
    return texto + (", ..." if len(faixas) > limite else "")


def _iso(valor) -> str | None:
    if valor is None or valor == "":
        return None
    iso = getattr(valor, "isoformat", None)
    return iso() if iso else str(valor)


def _so_preenchidos(campos: dict) -> dict:
    return {k: v for k, v in campos.items() if v not in (None, "", [], {})}


_DATA_PDF = re.compile(r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
                       r"(?:(Z)|([+\-])(\d{2})'?(\d{2})?'?)?")


def _data_pdf(bruto) -> str | None:
    """'D:20240315143000-03'00'' -> '2024-03-15T14:30:00-03:00'. Sem padrao: o bruto."""
    if not bruto:
        return None
    m = _DATA_PDF.match(str(bruto).strip())
    if not m:
        return str(bruto)
    ano, mes, dia, hora, minuto, seg, zulu, sinal, tzh, tzm = m.groups()
    texto = f"{ano}-{mes or '01'}-{dia or '01'}T{hora or '00'}:{minuto or '00'}:{seg or '00'}"
    if zulu:
        texto += "+00:00"
    elif sinal:
        texto += f"{sinal}{tzh}:{tzm or '00'}"
    return texto


def _tem_core_properties(pacote) -> bool:
    """python-docx/python-pptx INVENTAM propriedades (autor 'python-docx',
    modificado = agora) quando o pacote nao tem docProps/core.xml. So lemos
    as propriedades se a relacao existir de verdade."""
    rels = getattr(pacote, "rels", None)
    if rels is None:
        rels = getattr(pacote, "_rels", None)
    if rels is None:
        return False
    try:
        return any(str(getattr(r, "reltype", "")).endswith("/metadata/core-properties")
                   for r in rels.values())
    except Exception:
        return False


def _metadados_core(objeto) -> dict:
    """core_properties de python-docx / python-pptx -> dict so com o preenchido."""
    try:
        if not _tem_core_properties(objeto.part.package):
            return {}
        cp = objeto.core_properties
        return _so_preenchidos({
            "titulo": cp.title, "autor": cp.author, "assunto": cp.subject,
            "palavras_chave": cp.keywords, "ultimo_autor": cp.last_modified_by,
            "criado_em": _iso(cp.created), "modificado_em": _iso(cp.modified),
            "revisao": cp.revision if cp.revision else None, "categoria": cp.category,
        })
    except Exception:
        return {}


# --- pacote OOXML: cifrado, binario renomeado, modelo e macro --------------

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MARCA_CIFRADO = "EncryptedPackage".encode("utf-16-le")


def _assinatura(caminho) -> bytes:
    with open(_lp(caminho), "rb") as fh:
        return fh.read(8)


def _ole_cifrado(caminho) -> bool:
    """Documento Office com senha de abertura e um conteiner OLE com o fluxo
    `EncryptedPackage`. Procura o nome do fluxo (UTF-16) no arquivo inteiro,
    em blocos — nao carrega o arquivo na memoria."""
    resto = b""
    with open(_lp(caminho), "rb") as fh:
        while True:
            bloco = fh.read(1 << 20)
            if not bloco:
                return False
            janela = resto + bloco
            if _MARCA_CIFRADO in janela:
                return True
            resto = janela[-len(_MARCA_CIFRADO):]


def _checar_ooxml(caminho, extrator: str) -> dict | None:
    """Um .docx/.xlsx/.pptx que na verdade e OLE: com senha ou legado renomeado.

    Sem isto, as duas coisas viravam `erro` ("File is not a zip file") e
    gastavam as 4 tentativas do worker num arquivo que nunca vai abrir.
    """
    if _assinatura(caminho) != _OLE:
        return None
    if _ole_cifrado(caminho):
        return _vazio(extrator, "protegido", "documento Office cifrado com senha "
                                             "(OLE EncryptedPackage)")
    return _vazio(extrator, "nao_suportado", "binario OLE com extensao OOXML "
                                             "(formato legado renomeado)")


_CT_PRINCIPAL = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
_CT_EQUIVALENTE = {
    "docx": ("application/vnd.ms-word.document.macroEnabled.main+xml",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
             "application/vnd.ms-word.template.macroEnabledTemplate.main+xml"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
             "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
             "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
             "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml"),
}


def _pacote_equivalente(caminho, formato: str) -> io.BytesIO | None:
    """.docm/.dotx/.potx: mesma estrutura do .docx/.pptx, outro content type.

    python-docx e python-pptx recusam o pacote so pelo content type da parte
    principal. Montamos uma copia EM MEMORIA com o content type trocado — o
    original nao e tocado e nada vai para o disco. Macro nenhuma e executada:
    as bibliotecas so leem XML. None se nao ha o que trocar.
    """
    with zipfile.ZipFile(_lp(caminho)) as origem:
        tipos = origem.read("[Content_Types].xml")
        novo = tipos
        for antigo in _CT_EQUIVALENTE[formato]:
            novo = novo.replace(antigo.encode("ascii"), _CT_PRINCIPAL[formato].encode("ascii"))
        if novo == tipos:
            return None
        saida = io.BytesIO()
        with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as destino:
            for info in origem.infolist():
                dados = novo if info.filename == "[Content_Types].xml" else origem.read(info)
                destino.writestr(info, dados)
    saida.seek(0)
    return saida


def _abrir_ooxml(caminho, formato: str, abrir):
    """Abre com a biblioteca; se ela recusar so pelo content type, abre a copia
    equivalente em memoria. Excecao sobe para quem chamou."""
    try:
        return abrir(_lp(caminho))
    except ValueError:
        equivalente = _pacote_equivalente(caminho, formato)
        if equivalente is None:
            raise
        return abrir(equivalente)


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------

def _cobertura_imagem(pagina) -> float:
    """Fracao da area da pagina coberta por imagens (0..1, soma das caixas)."""
    area = abs(pagina.rect.width * pagina.rect.height) or 1.0
    coberta = 0.0
    for info in pagina.get_image_info():
        caixa = pagina.rect & info.get("bbox", (0, 0, 0, 0))
        if not caixa.is_empty:
            coberta += abs(caixa.width * caixa.height)
    return min(coberta / area, 1.0)


def _pagina_em_branco(pagina) -> bool:
    try:
        return not pagina.read_contents().strip()
    except Exception:
        return False                # na duvida, nao e branca: vai para OCR


def decidir_ocr_paginas(metricas: list, cfg: dict) -> dict:
    """A regra de OCR por pagina (DEC-043) — funcao PURA das metricas.

    `metricas`: uma entrada por pagina lida, na ordem; cada uma e
    `(c, cob_mil, d, img)` (ver FORMATO_OCR_METRICAS) ou None (pagina que nao
    carregou). E a MESMA funcao que `extrair_pdf` usa: recalibrar
    `min_caracteres_por_pagina` / `ocr_cobertura_imagem` depois e so chama-la
    de novo sobre `ocr_metricas` do perfil — sem baixar o PDF.

        pede OCR  <=>  c < min_cpp  E  ( img > 0 e cob_mil/1000 >= min_cobertura
                                         OU c == 0 e (d ou img) )
        branca    <=>  c == 0, d == 0, img == 0          (nao conta)

    Devolve {"paginas_ocr": [n, ...], "brancas": int, "indeterminadas": [n, ...]}.
    Indeterminada = a decisao precisava da cobertura e ela nao foi medida
    (c >= ocr_metrica_max_caracteres na extracao, mas min_cpp foi recalibrado
    acima disso); ela NAO entra em `paginas_ocr`.
    """
    min_cpp = _limite(cfg, "min_caracteres_por_pagina", float(PADRAO_MIN_CARACTERES_POR_PAGINA))
    min_cobertura = _limite(cfg, "ocr_cobertura_imagem", PADRAO_OCR_COBERTURA_IMAGEM)
    ocr: list[int] = []
    indeterminadas: list[int] = []
    brancas = 0
    for numero, m in enumerate(metricas, start=1):
        if m is None:
            continue
        c, cob_mil, d, img = m
        if c >= min_cpp:
            continue
        if img and cob_mil is None:
            indeterminadas.append(numero)
        elif img and cob_mil / 1000 >= min_cobertura:
            ocr.append(numero)
        elif c == 0:
            if not d and not img:
                brancas += 1
            else:
                ocr.append(numero)
    return {"paginas_ocr": ocr, "brancas": brancas, "indeterminadas": indeterminadas}


def metricas_ocr_para_texto(metricas: list) -> str:
    """[(c, cob_mil, d, img) | None, ...] -> 'c,cob,d,img;...' (FORMATO_OCR_METRICAS)."""
    return ";".join("-" if m is None else
                    f"{m[0]},{'' if m[1] is None else m[1]},{m[2]},{m[3]}" for m in metricas)


def metricas_ocr_de_texto(texto: str) -> list:
    """Inverso de `metricas_ocr_para_texto` — o que um recalibrador le do perfil."""
    saida: list = []
    for item in (texto.split(";") if texto else []):
        if item == "-":
            saida.append(None)
            continue
        c, cob, d, img = item.split(",")
        saida.append((int(c), None if cob == "" else int(cob), int(d), int(img)))
    return saida


def _metricas_da_pagina(pagina, texto: str, min_cpp: float, teto_cobertura: float) -> tuple:
    """(c, cob_mil, d, img) de uma pagina ja carregada.

    Excecao sobe (a pagina conta como "nao carregou") so quando a medida
    decide o OCR de hoje (c < min_cpp) — como antes do pdf/1.3. Acima disso a
    cobertura e so metrica para recalibrar: se falhar, fica "nao medida".
    """
    c = len(texto)
    img = len(pagina.get_images(full=False))
    if not img:
        cob_mil = 0
    elif c < min_cpp:
        cob_mil = int(round(_cobertura_imagem(pagina) * 1000))
    elif c < teto_cobertura:
        try:
            cob_mil = int(round(_cobertura_imagem(pagina) * 1000))
        except Exception:
            cob_mil = None
    else:
        cob_mil = None                            # nao medida: c alto demais
    d = 1 if c else int(not _pagina_em_branco(pagina))
    return (c, cob_mil, d, img)


def extrair_pdf(caminho: Path, cfg: dict) -> dict:
    """PDF por pagina, com a decisao de OCR PAGINA A PAGINA (L2).

    Uma pagina precisa de OCR quando tem menos de `min_caracteres_por_pagina`
    caracteres E (imagens cobrem >= `ocr_cobertura_imagem` da pagina, OU ela
    nao tem texto nenhum mas tem conteudo desenhado). Pagina sem texto e sem
    conteudo e branca: nao conta. Basta UMA pagina assim para o documento
    ser `ocr_requerido` — o texto das outras paginas e extraido e indexado do
    mesmo jeito, e `paginas_ocr` diz exatamente quais faltam.

    pdf/1.3: a decisao sai de `decidir_ocr_paginas` sobre as metricas de cada
    pagina, e as metricas vao para o perfil (`ocr_metricas`) — recalibrar a
    regra depois nao exige baixar o acervo de novo.
    """
    import fitz  # pymupdf

    max_paginas = _limite(cfg, "paginas_max_pdf", PADRAO_PAGINAS_MAX_PDF)
    min_cpp = _limite(cfg, "min_caracteres_por_pagina", float(PADRAO_MIN_CARACTERES_POR_PAGINA))
    # a cobertura e medida ate o teto, e o teto nunca fica abaixo do limiar
    # em uso: a decisao de hoje nunca depende de cobertura nao medida.
    teto_cobertura = max(_limite(cfg, "ocr_metrica_max_caracteres",
                                 float(PADRAO_OCR_METRICA_MAX_CARACTERES)), min_cpp)

    sem_leitor = motivo_sem_leitor(Path(caminho).suffix, "pdf")
    if sem_leitor:
        return _vazio("pdf", "nao_suportado", sem_leitor)

    resultado = _vazio("pdf", "ok")
    try:
        doc = fitz.open(_lp(caminho))
    except Exception as exc:
        return _vazio("pdf", "erro", f"nao abriu: {exc}")

    try:
        if doc.needs_pass:
            return _vazio("pdf", "protegido", "PDF com senha")

        total = doc.page_count
        lidas = min(total, max_paginas)
        truncado = total > max_paginas
        unidades: list[str] = []
        imagens = com_texto = 0
        metricas: list = []
        falharam: list[int] = []

        for indice in range(lidas):
            numero = indice + 1
            try:
                pagina = doc.load_page(indice)
                texto = _limpar_unidade(pagina.get_text("text") or "")
                m = _metricas_da_pagina(pagina, texto, min_cpp, teto_cobertura)
            except Exception:
                falharam.append(numero)
                unidades.append("")
                metricas.append(None)
                continue
            metricas.append(m)
            imagens += m[3]
            if texto:
                com_texto += 1
            unidades.append(texto)

        decisao = decidir_ocr_paginas(metricas, cfg)
        ocr, brancas = decisao["paginas_ocr"], decisao["brancas"]

        paginas_texto = _paginas_de_unidades(unidades)
        inteiro = _juntar_unidades(unidades)
        uteis = sum(len(t) for _, t in paginas_texto)
        # A densidade e SOBRE AS PAGINAS LIDAS (B1) e conta so texto, nao o
        # separador de pagina. E metrica: a decisao de OCR e por pagina.
        densidade = (uteis / lidas) if lidas else 0.0
        meta = doc.metadata or {}
        resultado.update({
            "paginas": total, "paginas_texto": paginas_texto, "texto": inteiro,
            "imagens": imagens, "paginas_com_texto": com_texto,
            "caracteres": len(inteiro), "palavras": len(inteiro.split()),
            "densidade_texto": round(densidade, 1),
            "metadados": _so_preenchidos({
                "titulo": meta.get("title"), "autor": meta.get("author"),
                "assunto": meta.get("subject"), "palavras_chave": meta.get("keywords"),
                "aplicativo": meta.get("creator"), "produtor": meta.get("producer"),
                "criado_em": _data_pdf(meta.get("creationDate")),
                "modificado_em": _data_pdf(meta.get("modDate")),
            }),
            "paginas_ocr": ocr,
            "ocr_metricas": ({"formato": FORMATO_OCR_METRICAS,
                              "paginas": metricas_ocr_para_texto(metricas)}
                             if metricas else None),
        })

        avisos = []
        if truncado:
            avisos.append(f"LIMITE: lidas {lidas} de {total} paginas (paginas_max_pdf)")
        if falharam:
            avisos.append(f"{len(falharam)} pagina(s) nao carregaram: {_faixas(falharam)}")

        if ocr:
            # Escaneado ou misto. Nao e erro, e nada do que ja foi lido se perde.
            nao_brancas = lidas - brancas - len(falharam)
            tipo = "escaneado" if len(ocr) >= nao_brancas else "misto"
            resultado["ocr_tipo"] = tipo
            resultado["status"] = "ocr_requerido"
            avisos.insert(0, f"OCR ({tipo}): {len(ocr)} de {lidas} pagina(s) lidas sem "
                             f"camada de texto: {_faixas(ocr)}")
        elif truncado or falharam:
            resultado["status"] = "parcial"
        elif not inteiro:
            resultado["status"] = "vazio"
        resultado["erro"] = "; ".join(avisos)
        return resultado
    except Exception as exc:
        return _vazio("pdf", "erro", f"{type(exc).__name__}: {exc}")
    finally:
        try:
            doc.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# DOCX
# ----------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_W_IGNORAR = {_W + "pPr", _W + "rPr", _W + "delText", _W + "instrText", _W + "sectPr",
              _MC_FALLBACK}


def _texto_do_paragrafo(no, partes: list, caixas: list) -> None:
    """Texto de um w:p na ordem dos runs. Caixas de texto ancoradas no
    paragrafo (w:txbxContent) sao separadas em `caixas` — viram linhas
    proprias. `mc:Fallback` e pulado: e a copia VML da mesma caixa, que o
    Word grava junto da versao DrawingML (sem isto a caixa sairia duas vezes).
    """
    for filho in no:
        tag = filho.tag
        if not isinstance(tag, str) or tag in _W_IGNORAR:
            continue
        if tag == _W + "txbxContent":
            caixas.append(filho)
        elif tag == _W + "t":
            partes.append(filho.text or "")
        elif tag == _W + "tab":
            partes.append("\t")
        elif tag in (_W + "br", _W + "cr"):
            partes.append("\n")
        elif tag == _W + "noBreakHyphen":
            partes.append("-")
        else:
            _texto_do_paragrafo(filho, partes, caixas)


def _celulas(linha):
    for filho in linha:
        if filho.tag == _W + "tc":
            yield filho
        elif filho.tag == _W + "sdt":
            conteudo = filho.find(_W + "sdtContent")
            if conteudo is not None:
                yield from _celulas(conteudo)


def _linhas_tabela(tabela):
    for filho in tabela:
        if filho.tag == _W + "tr":
            yield filho
        elif filho.tag == _W + "sdt":
            conteudo = filho.find(_W + "sdtContent")
            if conteudo is not None:
                yield from _linhas_tabela(conteudo)


def _blocos_docx(container, linhas: list, contagem: dict, profundidade: int = 0) -> None:
    """Paragrafos e tabelas NA ORDEM DO DOCUMENTO (corpo, cabecalho, rodape ou
    caixa de texto — e o mesmo XML). Tabela: uma linha por linha da tabela,
    celulas unidas por ` | `; tabela aninhada entra dentro da celula."""
    if profundidade > 16:                       # cinto contra aninhamento absurdo
        return
    for filho in container:
        tag = filho.tag
        if tag == _W + "p":
            partes, caixas = [], []
            _texto_do_paragrafo(filho, partes, caixas)
            texto = "".join(partes).strip()
            if texto:
                linhas.append(texto)
            for caixa in caixas:
                contagem["caixas"] += 1
                _blocos_docx(caixa, linhas, contagem, profundidade + 1)
        elif tag == _W + "tbl":
            contagem["tabelas"] += 1
            for linha in _linhas_tabela(filho):
                celulas = []
                for celula in _celulas(linha):
                    sub: list = []
                    _blocos_docx(celula, sub, contagem, profundidade + 1)
                    texto = "\n".join(sub).strip()
                    if texto:
                        celulas.append(texto)
                if celulas:
                    linhas.append(" | ".join(celulas))
        elif tag == _W + "sdt":
            conteudo = filho.find(_W + "sdtContent")
            if conteudo is not None:
                _blocos_docx(conteudo, linhas, contagem, profundidade + 1)
        elif tag in (_W + "customXml", _W + "smartTag", _W + "ins"):
            _blocos_docx(filho, linhas, contagem, profundidade + 1)


def _partes_por_tipo(pacote, sufixo_ct: str) -> list:
    partes = [p for p in pacote.iter_parts()
              if str(getattr(p, "content_type", "")).endswith(sufixo_ct)]
    return sorted(partes, key=lambda p: str(p.partname))


def _elemento(parte):
    elemento = getattr(parte, "element", None)
    return elemento if elemento is not None else getattr(parte, "_element", None)


def extrair_docx(caminho: Path, cfg: dict) -> dict:
    sem_leitor = motivo_sem_leitor(Path(caminho).suffix, "docx")
    if sem_leitor:
        return _vazio("docx", "nao_suportado", sem_leitor)
    try:
        import docx
    except ImportError:
        return _vazio("docx", "nao_suportado", "python-docx nao instalado")

    max_chars = _limite(cfg, "max_caracteres_documento", PADRAO_MAX_CARACTERES_DOCUMENTO)
    try:
        ole = _checar_ooxml(caminho, "docx")
        if ole is not None:
            return ole
        documento = _abrir_ooxml(caminho, "docx", docx.Document)
    except Exception as exc:
        return _vazio("docx", "erro", f"nao abriu: {exc}")

    try:
        pacote = documento.part.package
        contagem = {"tabelas": 0, "caixas": 0}
        avisos = []

        def secoes(sufixo_ct: str, rotulo: str) -> list[str]:
            saida, vistos = [], set()
            for parte in _partes_por_tipo(pacote, sufixo_ct):
                try:
                    sub: list = []
                    _blocos_docx(_elemento(parte), sub, {"tabelas": 0, "caixas": 0})
                except Exception as exc:
                    avisos.append(f"{parte.partname}: {type(exc).__name__}")
                    continue
                texto = "\n".join(sub).strip()
                if texto and texto not in vistos:  # 1a pagina/par/impar repetem
                    vistos.add(texto)
                    saida.append(f"[{rotulo}] {texto}")
            return saida

        cabecalhos = secoes("header+xml", "cabecalho")
        corpo: list = []
        _blocos_docx(documento.element.body, corpo, contagem)
        rodapes = secoes("footer+xml", "rodape")

        texto = "\n".join(cabecalhos + corpo + rodapes).strip()
        # Imagens do documento (corpo, cabecalho, rodape). A miniatura do
        # pacote (/docProps/thumbnail.*) nao e conteudo.
        imagens = len({str(p.partname) for p in pacote.iter_parts()
                       if str(getattr(p, "content_type", "")).startswith("image/")
                       and str(p.partname).startswith("/word/")})
        status = "ok" if texto else ("ocr_requerido" if imagens else "vazio")
        if len(texto) > max_chars:
            texto = texto[:max_chars]
            status = "parcial"
            avisos.insert(0, f"LIMITE: texto cortado em {max_chars} caracteres "
                             f"(max_caracteres_documento)")
        elif avisos and status == "ok":
            status = "parcial"
        if status == "ocr_requerido":
            avisos.insert(0, f"OCR: documento sem texto, com {imagens} imagem(ns)")

        resultado = _vazio("docx", status, "; ".join(avisos))
        resultado.update({"texto": texto, "paginas_texto": [(0, texto)] if texto else [],
                          "imagens": imagens, "caracteres": len(texto),
                          "palavras": len(texto.split()), "tabelas": contagem["tabelas"],
                          "metadados": _metadados_core(documento)})
        return resultado
    except Exception as exc:
        return _vazio("docx", "erro", f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# PPTX
# ----------------------------------------------------------------------

def _formas_pptx(formas, partes: list, contagem: dict, profundidade: int = 0) -> None:
    """Percorre as formas de um slide e junta o texto.

    Precisa ser recursivo: uma forma agrupada nao tem text_frame, e o texto
    das caixas de dentro se perderia. Tabela tambem nao tem text_frame — o
    texto mora nas celulas. Forma que falha e CONTADA (vira `parcial`), nao
    engolida em silencio.
    """
    if profundidade > 8:                       # cinto contra grupo circular
        return
    for forma in formas:
        try:
            tipo = getattr(forma, "shape_type", None)
            # 13 = MSO_SHAPE_TYPE.PICTURE; imagem em placeholder tem outro tipo
            if tipo == 13 or type(forma).__name__ == "PlaceholderPicture":
                contagem["imagens"] += 1
            if getattr(forma, "has_table", False):
                contagem["tabelas"] += 1
                for linha in forma.table.rows:
                    celulas = [c.text.strip() for c in linha.cells if c.text and c.text.strip()]
                    if celulas:
                        partes.append(" | ".join(celulas))
                continue
            if tipo == 6 or hasattr(forma, "shapes"):    # MSO_SHAPE_TYPE.GROUP
                _formas_pptx(forma.shapes, partes, contagem, profundidade + 1)
                continue
            if getattr(forma, "has_text_frame", False) and forma.text_frame.text.strip():
                partes.append(forma.text_frame.text.strip())
        except Exception:
            contagem["falhas"] += 1             # uma forma ruim nao mata o slide


def extrair_pptx(caminho: Path, cfg: dict) -> dict:
    sem_leitor = motivo_sem_leitor(Path(caminho).suffix, "pptx")
    if sem_leitor:
        return _vazio("pptx", "nao_suportado", sem_leitor)
    try:
        from pptx import Presentation
    except ImportError:
        return _vazio("pptx", "nao_suportado", "python-pptx nao instalado")

    max_chars = _limite(cfg, "max_caracteres_documento", PADRAO_MAX_CARACTERES_DOCUMENTO)
    try:
        ole = _checar_ooxml(caminho, "pptx")
        if ole is not None:
            return ole
        apresentacao = _abrir_ooxml(caminho, "pptx", Presentation)
    except Exception as exc:
        return _vazio("pptx", "erro", f"nao abriu: {exc}")

    try:
        unidades: list[str] = []
        contagem = {"imagens": 0, "tabelas": 0, "falhas": 0}
        total_chars = 0
        cortado = False
        total_slides = len(apresentacao.slides)
        for slide in apresentacao.slides:
            partes: list = []
            _formas_pptx(slide.shapes, partes, contagem)
            try:
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                    partes.append("[notas] " + slide.notes_slide.notes_text_frame.text.strip())
            except Exception:
                contagem["falhas"] += 1
            unidade = _limpar_unidade("\n".join(partes))
            if total_chars + len(unidade) > max_chars:
                unidades.append(unidade[:max(0, max_chars - total_chars)].strip())
                cortado = True
                break
            total_chars += len(unidade)
            unidades.append(unidade)

        inteiro = _juntar_unidades(unidades)
        avisos = []
        if cortado:
            status = "parcial"
            avisos.append(f"LIMITE: texto cortado em {max_chars} caracteres no slide "
                          f"{len(unidades)} de {total_slides} (max_caracteres_documento)")
        elif contagem["falhas"]:
            status = "parcial"
        elif inteiro:
            status = "ok"
        else:
            status = "ocr_requerido" if contagem["imagens"] else "vazio"
        if contagem["falhas"]:
            avisos.append(f"{contagem['falhas']} forma(s)/nota(s) nao lidas")
        if status == "ocr_requerido":
            avisos.insert(0, f"OCR: apresentacao sem texto, com {contagem['imagens']} "
                             f"imagem(ns)")

        resultado = _vazio("pptx", status, "; ".join(avisos))
        resultado.update({"texto": inteiro, "paginas_texto": _paginas_de_unidades(unidades),
                          "slides": total_slides, "imagens": contagem["imagens"],
                          "tabelas": contagem["tabelas"], "caracteres": len(inteiro),
                          "palavras": len(inteiro.split()),
                          "metadados": _metadados_core(apresentacao)})
        return resultado
    except Exception as exc:
        return _vazio("pptx", "erro", f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# XLSX / XLSM
# ----------------------------------------------------------------------

def extrair_xlsx(caminho: Path, cfg: dict) -> dict:
    """Valores de celula, aba por aba, com o nome da aba.

    `data_only=True`: formula vale o VALOR EM CACHE gravado por quem salvou
    (o Excel sempre grava). Formula sem cache (planilha gerada por programa e
    nunca aberta no Excel) chega como celula vazia — perda silenciosa,
    documentada em docs/08_EXTRATORES.md. `keep_vba=False` + `read_only`:
    macro de .xlsm nunca e carregada, muito menos executada.
    """
    sem_leitor = motivo_sem_leitor(Path(caminho).suffix, "xlsx")
    if sem_leitor:
        return _vazio("xlsx", "nao_suportado", sem_leitor)
    try:
        import openpyxl
    except ImportError:
        return _vazio("xlsx", "nao_suportado", "openpyxl nao instalado")

    max_linhas = _limite(cfg, "xlsx_max_linhas_aba", PADRAO_XLSX_MAX_LINHAS_ABA)
    max_chars = _limite(cfg, "xlsx_max_caracteres", PADRAO_XLSX_MAX_CARACTERES)
    try:
        ole = _checar_ooxml(caminho, "xlsx")
        if ole is not None:
            return ole
        livro = openpyxl.load_workbook(_lp(caminho), read_only=True, data_only=True,
                                       keep_vba=False)
    except Exception as exc:
        return _vazio("xlsx", "erro", f"nao abriu: {exc}")

    try:
        unidades: list[str] = []
        avisos: list[str] = []
        total_chars = 0
        estourou = False
        for nome_aba in livro.sheetnames:
            aba = livro[nome_aba]
            if not hasattr(aba, "iter_rows"):
                # Aba de grafico (chartsheet): nao tem celula. Antes, isto
                # levantava AttributeError e a planilha inteira virava `erro`.
                unidades.append("")
                continue
            linhas_texto = []
            for i, linha in enumerate(aba.iter_rows(values_only=True)):
                if i >= max_linhas:
                    avisos.append(f"LIMITE: aba '{nome_aba}' cortada em {max_linhas} "
                                  f"linhas (xlsx_max_linhas_aba)")
                    break
                valores = [str(v).strip() for v in linha if v is not None and str(v).strip()]
                if valores:
                    linhas_texto.append(" | ".join(valores))
                total_chars += sum(len(v) for v in valores)
                if total_chars > max_chars:
                    estourou = True
                    break
            unidades.append(_limpar_unidade(f"[aba] {nome_aba}\n" + "\n".join(linhas_texto))
                            if linhas_texto else "")
            if estourou:
                lidas = len(unidades)
                avisos.append(f"LIMITE: {max_chars} caracteres atingidos na aba "
                              f"'{nome_aba}' ({lidas} de {len(livro.sheetnames)} abas lidas; "
                              f"xlsx_max_caracteres)")
                break

        inteiro = _juntar_unidades(unidades)
        status = "parcial" if avisos else ("ok" if inteiro else "vazio")
        imagens = macros = None
        try:
            with zipfile.ZipFile(_lp(caminho)) as pacote:
                nomes = pacote.namelist()
            imagens = sum(1 for n in nomes if n.startswith("xl/media/"))
            macros = any(n.lower().endswith("vbaproject.bin") for n in nomes)
        except Exception:
            pass
        propriedades = getattr(livro, "properties", None)
        metadados = {}
        if propriedades is not None:
            metadados = _so_preenchidos({
                "titulo": propriedades.title, "autor": propriedades.creator,
                "assunto": propriedades.subject, "palavras_chave": propriedades.keywords,
                "ultimo_autor": propriedades.lastModifiedBy,
                "criado_em": _iso(propriedades.created),
                "modificado_em": _iso(propriedades.modified),
            })
        if macros:
            metadados["macros"] = True          # registrado, nunca executado

        resultado = _vazio("xlsx", status, "; ".join(avisos))
        resultado.update({"texto": inteiro, "paginas_texto": _paginas_de_unidades(unidades),
                          "abas": len(livro.sheetnames), "imagens": imagens,
                          "caracteres": len(inteiro), "palavras": len(inteiro.split()),
                          "metadados": metadados})
        return resultado
    except Exception as exc:
        return _vazio("xlsx", "erro", f"{type(exc).__name__}: {exc}")
    finally:
        try:
            livro.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# CSV e texto puro
# ----------------------------------------------------------------------

def _ler_texto(caminho: Path, limite_bytes: int) -> tuple[str, bool]:
    with open(_lp(caminho), "rb") as fh:
        bruto = fh.read(limite_bytes + 1)
    truncado = len(bruto) > limite_bytes
    bruto = bruto[:limite_bytes]

    # BOM primeiro. Sem isto um arquivo UTF-16 (comum em exportacao do
    # Windows) e aceito pelo cp1252 e vira lixo com bytes nulos no indice.
    codecs = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    if bruto.startswith(b"\xff\xfe\x00\x00"):
        codecs.insert(0, "utf-32-le")
    elif bruto.startswith(b"\xff\xfe"):
        codecs.insert(0, "utf-16")
    elif bruto.startswith(b"\xfe\xff"):
        codecs.insert(0, "utf-16")
    elif b"\x00" in bruto[:4096]:
        # UTF-16 sem BOM: o lado nulo diz qual.
        pares = bruto[:4096]
        codecs.insert(0, "utf-16-le" if pares[1::2].count(0) > pares[0::2].count(0)
                      else "utf-16-be")

    for codec in codecs:
        try:
            texto = bruto.decode(codec)
        except (UnicodeDecodeError, UnicodeError, LookupError):
            continue
        # Byte nulo nunca e conteudo: trunca a coluna do FTS e polui o texto
        # derivado. Se sobrou, a decodificacao esta errada — tenta a proxima.
        if "\x00" in texto:
            if codec == codecs[-1]:
                texto = texto.replace("\x00", "")
            else:
                continue
        return texto, truncado
    return bruto.decode("latin-1", errors="replace").replace("\x00", ""), truncado


def _folga_campo_csv(tamanho: int) -> None:
    """Deixa o modulo csv aceitar um campo do tamanho do proprio arquivo."""
    alvo = max(tamanho, 131072)
    while True:
        try:
            if csv.field_size_limit() < alvo:
                csv.field_size_limit(alvo)
            return
        except OverflowError:                  # limite do C long nesta build
            alvo //= 2
            if alvo <= 131072:
                return


def extrair_csv(caminho: Path, cfg: dict) -> dict:
    try:
        bruto, truncado = _ler_texto(caminho, 20 * 1024 * 1024)
        amostra = bruto[:8192]
        try:
            dialeto = csv.Sniffer().sniff(amostra, delimiters=";,\t|")
            separador = dialeto.delimiter
        except Exception:
            separador = ";" if amostra.count(";") > amostra.count(",") else ","
        # O limite padrao do modulo csv (128 KB por campo) estoura em
        # planilha exportada com uma celula de texto longo, e ali isso vira
        # `erro` -> 4 tentativas do worker por um arquivo que esta intacto.
        _folga_campo_csv(len(bruto))
        linhas = []
        for i, campos in enumerate(csv.reader(io.StringIO(bruto), delimiter=separador)):
            if i >= 20000:
                truncado = True
                break
            valores = [c.strip() for c in campos if c and c.strip()]
            if valores:
                linhas.append(" | ".join(valores))
        texto = "\n".join(linhas).strip()
        resultado = _vazio("csv", "parcial" if truncado else ("ok" if texto else "vazio"))
        resultado.update({"texto": texto, "paginas_texto": [(0, texto)] if texto else [],
                          "caracteres": len(texto), "palavras": len(texto.split())})
        return resultado
    except Exception as exc:
        return _vazio("csv", "erro", f"{type(exc).__name__}: {exc}")


def extrair_texto(caminho: Path, cfg: dict) -> dict:
    try:
        texto, truncado = _ler_texto(caminho, 20 * 1024 * 1024)
        texto = texto.strip()
        resultado = _vazio("texto", "parcial" if truncado else ("ok" if texto else "vazio"))
        resultado.update({"texto": texto, "paginas_texto": [(0, texto)] if texto else [],
                          "caracteres": len(texto), "palavras": len(texto.split())})
        return resultado
    except Exception as exc:
        return _vazio("texto", "erro", f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# Despacho
# ----------------------------------------------------------------------

EXTRATORES = {
    "pdf": extrair_pdf,
    "docx": extrair_docx,
    "pptx": extrair_pptx,
    "xlsx": extrair_xlsx,
    "csv": extrair_csv,
    "texto": extrair_texto,
}


def extrair(caminho: Path, categoria: str, cfg: dict) -> dict:
    """Ponto unico de entrada. Nunca levanta excecao."""
    funcao = EXTRATORES.get(categoria)
    if funcao is None:
        return _vazio(categoria or "?", "nao_suportado", f"sem extrator para '{categoria}'")
    try:
        return funcao(Path(caminho), cfg)
    except Exception as exc:                                  # cinto de seguranca
        return _vazio(categoria, "erro", f"{type(exc).__name__}: {exc}")
