"""
test_quickxor — o teste se valida sozinho contra o SharePoint.

    python tests\\test_quickxor.py              # 20 arquivos reais
    python tests\\test_quickxor.py --sem-rede   # so as invariantes locais
    python tests\\test_quickxor.py --arquivos 40 --max-mb 20

A prova nao e um valor que eu escrevi aqui: e o hash que o **servidor**
calculou. Baixamos o arquivo, calculamos do nosso lado e comparamos. Se a
implementacao estiver errada em um bit, o teste falha.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import RAIZ, checar, placar_final  # noqa: E402

import graph_ro  # noqa: E402
import quickxor  # noqa: E402


def coletar(graph: graph_ro.GraphRO, drive_id: str, quantos: int, max_bytes: int,
            teto_pastas: int = 400) -> list[dict]:
    """Varre a arvore em largura ate juntar N arquivos variados e pequenos."""
    achados: list[dict] = []
    fila = ["root"]
    vistas = 0
    extensoes: dict[str, int] = {}
    while fila and len(achados) < quantos and vistas < teto_pastas:
        atual = fila.pop(0)
        vistas += 1
        try:
            itens = list(graph.filhos(drive_id, atual))
        except graph_ro.ErroGraph:
            continue
        for item in itens:
            if item.get("folder"):
                fila.append(item["id"])
                continue
            if not item.get("file") or not graph_ro.hash_do_item(item):
                continue
            tamanho = int(item.get("size") or 0)
            if not (0 < tamanho <= max_bytes):
                continue
            ext = os.path.splitext(item.get("name", ""))[1].lower()
            # teto por extensao, para a amostra nao virar so .jpg;
            # afrouxa quando a varredura ja andou bastante sem completar
            teto_ext = 4 if vistas < teto_pastas // 2 else 12
            if extensoes.get(ext, 0) >= teto_ext:
                continue
            extensoes[ext] = extensoes.get(ext, 0) + 1
            achados.append(item)
            if len(achados) >= quantos:
                break
    return achados


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-rede", action="store_true")
    ap.add_argument("--arquivos", type=int, default=20)
    ap.add_argument("--max-mb", type=float, default=8.0)
    args = ap.parse_args()

    print("\n--- invariantes locais ---")
    vazio = quickxor.de_bytes(b"")
    checar(len(quickxor.QuickXorHash().digest()) == 20, "o hash tem 20 bytes (160 bits)")
    checar(isinstance(vazio, str) and vazio.endswith("="), "sai em base64", vazio)
    checar(quickxor.de_bytes(b"a") != quickxor.de_bytes(b"b"),
           "conteudos diferentes dao hashes diferentes")
    checar(quickxor.de_bytes(b"abc") == quickxor.de_bytes(b"abc"),
           "mesmo conteudo da o mesmo hash")
    # O tamanho entra no hash: mesma soma XOR, tamanhos diferentes.
    checar(quickxor.de_bytes(b"\x00") != quickxor.de_bytes(b"\x00\x00"),
           "o tamanho do arquivo faz parte do hash")

    random.seed(7)
    dados = bytes(random.getrandbits(8) for _ in range(quickxor.BLOCO_PADRAO + 12345))
    inteiro = quickxor.de_bytes(dados)
    parcial = quickxor.QuickXorHash()
    corte = quickxor.BLOCO_PADRAO
    parcial.update(dados[:corte]).update(dados[corte:])
    checar(parcial.base64() == inteiro,
           "hash em pedacos alinhados == hash de uma vez so")

    if args.sem_rede:
        return placar_final()

    print("\n--- contra o SharePoint: 20 arquivos reais ---")
    graph = graph_ro.GraphRO(pausa_minima_s=0.05)
    site = graph.site_id()
    drives = graph.drives(site)
    alvo = next((d for d in drives if d.get("name") == "Programa"), drives[0])

    amostra = coletar(graph, alvo["id"], args.arquivos, int(args.max_mb * 1024 ** 2))
    checar(len(amostra) >= args.arquivos,
           f"juntou {len(amostra)} arquivos com hash do servidor")
    if not amostra:
        return placar_final()

    tmp = RAIZ / "tmp"
    tmp.mkdir(exist_ok=True)
    acertos = 0
    bytes_totais = 0
    for item in amostra:
        destino = tmp / f"qx_{item['id'][:16]}"
        try:
            baixados = graph.baixar(alvo["id"], item["id"], destino)
            calculado = quickxor.de_arquivo(destino)
            esperado = graph_ro.hash_do_item(item)
            bate = calculado == esperado
            acertos += int(bate)
            bytes_totais += baixados
            if not bate:
                print(f"    DIVERGIU  {item['name'][:50]}")
                print(f"      servidor  {esperado}")
                print(f"      calculado {calculado}")
        except graph_ro.ErroGraph as exc:
            print(f"    erro ao baixar {item['name'][:40]}: {exc}")
        finally:
            destino.unlink(missing_ok=True)

    checar(acertos == len(amostra),
           f"{acertos}/{len(amostra)} hashes conferem com o servidor")
    checar(not list(tmp.glob("qx_*")), "tmp/ ficou limpo")
    extensoes = sorted({os.path.splitext(i["name"])[1].lower() or "(sem)" for i in amostra})
    print(f"    formatos na amostra: {', '.join(extensoes)}")
    print(f"    baixados {bytes_totais/1024**2:.1f} MB em {graph.chamadas} chamadas")

    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
