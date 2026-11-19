"""
exportar_indice — fotografia legivel do indice da demonstracao.

    python examples/exportar_indice.py

Le `examples/demo_saida/demo.db` (gerado pelo pipeline da demo sobre o
mini-acervo ficticio) e grava `examples/indice_ficticio.json`: catalogo
(estado, prioridade P0..P4, obra, categoria, duplicata) e os trechos
indexados de cada conteudo. Serve para ver o que o indice guarda sem rodar
nada. Somente leitura no banco.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
BANCO = RAIZ / "examples" / "demo_saida" / "demo.db"
DESTINO = RAIZ / "examples" / "indice_ficticio.json"


def main() -> int:
    con = sqlite3.connect(f"file:{BANCO.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    arquivos = []
    for a in con.execute(
            "SELECT a.id, a.caminho_rel, a.ext, a.categoria, a.tamanho_bytes, a.prioridade, "
            "a.estado, a.duplicata_de, a.conteudo_id, p.obra, p.categoria_doc, p.motivo "
            "FROM arquivo a LEFT JOIN pasta p ON p.caminho_rel = a.pasta_rel ORDER BY a.id"):
        arquivos.append({
            "id": a["id"],
            "caminho": a["caminho_rel"].replace("\\", "/"),
            "extensao": a["ext"], "categoria": a["categoria"],
            "tamanho_bytes": a["tamanho_bytes"],
            "prioridade": f"P{a['prioridade']}", "motivo": a["motivo"],
            "obra": a["obra"], "categoria_documental": a["categoria_doc"],
            "estado": a["estado"], "duplicata_de": a["duplicata_de"],
            "conteudo_id": a["conteudo_id"],
        })
    conteudos = []
    for c in con.execute("SELECT id, extrator, caracteres, trechos FROM conteudo ORDER BY id"):
        trechos = [t["texto"] for t in con.execute(
            "SELECT texto FROM trecho WHERE conteudo_id=? ORDER BY ordinal", (c["id"],))]
        conteudos.append({"conteudo_id": c["id"], "extrator": c["extrator"],
                          "caracteres": c["caracteres"], "trechos": trechos})
    con.close()
    dados = {
        "aviso": "Dados ficticios. Gerado pela demo a partir de examples/acervo_ficticio.",
        "arquivos": arquivos,
        "conteudos": conteudos,
    }
    DESTINO.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                       newline="\n")
    print(DESTINO)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
