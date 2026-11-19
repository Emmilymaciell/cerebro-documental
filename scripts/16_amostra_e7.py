"""
16_amostra_e7 — escolhe (e depois mede) a amostra de prova do E7.

Dois comandos:

    python scripts\\16_amostra_e7.py selecionar
    python scripts\\16_amostra_e7.py relatar

`selecionar` monta uma amostra pequena e REPRODUTIVEL da fila da nuvem,
cobrindo os formatos e as situacoes que a prova precisa exercitar, grava a
composicao em `output/` e um retrato do estado ANTES em
`output/amostra_e7_antes.json`. Nao processa nada, nao toca na nuvem.

`relatar` compara o retrato com o estado de agora e imprime as metricas da
prova: baixados, evitados por deduplicacao, bytes previstos x baixados x
economizados, extracoes, falhas por categoria, linhas novas em `conteudo`,
ocorrencias que caíram em conteudo ja existente, e o antes/depois de cada
arquivo.

Por que determinista: a escolha e sempre `ORDER BY item_id` dentro de cada
cota, nunca `RANDOM()`. Rodar de novo no mesmo banco escolhe os mesmos
arquivos, e os ids ficam registrados em disco antes de qualquer download.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

KB = 1024
MB = 1024 ** 2

# Cada cota: (rotulo, predicado SQL, quantos). A ordem importa — as cotas do
# comeco escolhem primeiro, e nenhum arquivo entra duas vezes.
COTAS = [
    ("pdf P0 pequeno",      "categoria='pdf'  AND prioridade=0 AND tamanho_bytes BETWEEN 1 AND 204800", 5),
    ("pdf P0 medio",        "categoria='pdf'  AND prioridade=0 AND tamanho_bytes BETWEEN 1048576 AND 8388608", 3),
    ("pdf maior",           "categoria='pdf'  AND tamanho_bytes BETWEEN 8388608 AND :teto", 2),
    ("docx P0",             "categoria='docx' AND prioridade=0 AND tamanho_bytes BETWEEN 1 AND 2097152", 3),
    ("docx outra prio",     "categoria='docx' AND prioridade>0 AND tamanho_bytes BETWEEN 1 AND :teto", 2),
    ("xlsx P0",             "categoria='xlsx' AND prioridade=0 AND tamanho_bytes BETWEEN 1 AND 2097152", 3),
    ("xlsx outra prio",     "categoria='xlsx' AND prioridade>0 AND tamanho_bytes BETWEEN 1 AND :teto", 2),
    ("pptx",                "categoria='pptx' AND tamanho_bytes BETWEEN 1 AND :teto", 3),
    ("texto/csv",           "categoria IN ('texto','csv') AND tamanho_bytes BETWEEN 1 AND :teto", 2),
    ("P1",                  "prioridade=1 AND tamanho_bytes BETWEEN 1 AND 2097152", 1),
    ("P2",                  "prioridade=2 AND tamanho_bytes BETWEEN 1 AND 2097152", 1),
    ("P3",                  "prioridade=3 AND tamanho_bytes BETWEEN 1 AND 2097152", 1),
    ("P4",                  "prioridade=4 AND tamanho_bytes BETWEEN 1 AND 2097152", 1),
    ("arquivo de 0 byte",   "tamanho_bytes=0", 1),
]

# Situacoes que nao sao "formato": precisam de consulta propria.
GRUPOS_DUPLICADOS = 3      # grupos completos de 2 a 3 copias
UNICOS = 3                 # conteudo que so existe num lugar


def formatar(n: int) -> str:
    for unidade, divisor in (("GB", 1024 ** 3), ("MB", MB), ("KB", KB)):
        if n >= divisor:
            return f"{n/divisor:.1f} {unidade}"
    return f"{n} B"


class Amostra:
    def __init__(self, con, teto_bytes: int):
        self.con = con
        self.teto = teto_bytes
        self.escolhidos: dict[int, str] = {}   # arquivo_id -> rotulo da cota

    def _livres(self) -> str:
        if not self.escolhidos:
            return "1=1"
        return f"id NOT IN ({','.join(str(i) for i in self.escolhidos)})"

    def por_cota(self, rotulo: str, predicado: str, quantos: int) -> int:
        sql = (f"SELECT id FROM v_fila_nuvem WHERE {self._livres()} "
               f"AND {predicado.replace(':teto', str(self.teto))} "
               f"ORDER BY item_id LIMIT {int(quantos)}")
        achados = [linha[0] for linha in self.con.execute(sql)]
        for arquivo_id in achados:
            self.escolhidos[arquivo_id] = rotulo
        return len(achados)

    def grupos_duplicados(self, quantos: int) -> tuple[int, int]:
        """Grupos COMPLETOS de copias, para a deduplicacao aparecer de verdade.

        Exige que todas as copias do grupo estejam na fila: assim o relatorio
        consegue afirmar "baixei 1, evitei N-1" sem asterisco.
        """
        grupos = self.con.execute(
            "SELECT f.quick_xor, f.tamanho_bytes, COUNT(*) AS n, MIN(f.item_id) AS chave "
            "FROM v_fila_nuvem f "
            "WHERE f.quick_xor IS NOT NULL AND f.tamanho_bytes BETWEEN 1 AND ? "
            "GROUP BY f.quick_xor, f.tamanho_bytes "
            "HAVING COUNT(*) BETWEEN 2 AND 3 "
            "  AND COUNT(*) = (SELECT COUNT(*) FROM arquivo a "
            "                   WHERE a.quick_xor = f.quick_xor "
            "                     AND a.tamanho_bytes = f.tamanho_bytes "
            "                     AND a.presente = 1 AND a.presente_nuvem = 1) "
            "ORDER BY chave LIMIT ?", (self.teto, quantos)).fetchall()
        arquivos = 0
        for indice, grupo in enumerate(grupos, 1):
            membros = self.con.execute(
                "SELECT id FROM v_fila_nuvem WHERE quick_xor=? AND tamanho_bytes=? "
                "ORDER BY item_id", (grupo["quick_xor"], grupo["tamanho_bytes"])).fetchall()
            for linha in membros:
                self.escolhidos.setdefault(linha["id"], f"grupo duplicado {indice}")
                arquivos += 1
        return len(grupos), arquivos

    def unicos(self, quantos: int) -> int:
        return self.por_cota(
            "ocorrencia unica",
            "quick_xor IS NOT NULL AND tamanho_bytes BETWEEN 1024 AND 2097152 "
            "AND NOT EXISTS (SELECT 1 FROM arquivo a WHERE a.quick_xor=v_fila_nuvem.quick_xor "
            "AND a.tamanho_bytes=v_fila_nuvem.tamanho_bytes AND a.id<>v_fila_nuvem.id "
            "AND a.presente=1)", quantos)

    def detalhar(self) -> list[dict]:
        if not self.escolhidos:
            return []
        marcas = ",".join(str(i) for i in self.escolhidos)
        linhas = self.con.execute(
            f"SELECT a.id, a.item_id, a.drive_id, a.caminho_rel, a.caminho_nuvem, "
            f"a.categoria, a.ext, a.prioridade, a.tamanho_bytes, a.quick_xor, a.estado, "
            f"a.conteudo_id, "
            f"(SELECT COUNT(*) FROM arquivo b WHERE b.quick_xor=a.quick_xor "
            f" AND b.tamanho_bytes=a.tamanho_bytes AND b.presente=1 "
            f" AND a.quick_xor IS NOT NULL) AS ocorrencias_no_acervo "
            f"FROM arquivo a WHERE a.id IN ({marcas}) "
            f"ORDER BY a.prioridade, a.categoria, a.item_id").fetchall()
        saida = []
        for linha in linhas:
            registro = dict(linha)
            registro["cota"] = self.escolhidos[linha["id"]]
            saida.append(registro)
        return saida


# ----------------------------------------------------------------------
def selecionar(cfg, con, args) -> int:
    amostra = Amostra(con, args.teto_mb * MB)
    relato = []
    for rotulo, predicado, quantos in COTAS:
        achados = amostra.por_cota(rotulo, predicado, quantos)
        relato.append((rotulo, quantos, achados))
    grupos, arquivos_em_grupo = amostra.grupos_duplicados(GRUPOS_DUPLICADOS)
    relato.append((f"grupos duplicados ({grupos} grupos)", GRUPOS_DUPLICADOS,
                   arquivos_em_grupo))
    relato.append(("ocorrencia unica", UNICOS, amostra.unicos(UNICOS)))

    itens = amostra.detalhar()
    if not itens:
        print("a fila nao devolveu nenhum arquivo — nada a fazer.")
        return 1

    saida = cfg["_saida_path"]
    cu.assert_escrita_permitida(saida, cfg["_raiz_path"])
    saida.mkdir(parents=True, exist_ok=True)

    total_bytes = sum(i["tamanho_bytes"] or 0 for i in itens)
    # Bytes que uma execucao SEM deduplicacao baixaria x o que ela deve baixar.
    por_conteudo: dict = {}
    for item in itens:
        chave = (item["quick_xor"], item["tamanho_bytes"])
        por_conteudo.setdefault(chave, []).append(item)
    previsto_baixar = sum(membros[0]["tamanho_bytes"] or 0
                          for chave, membros in por_conteudo.items()
                          if chave[0] is not None)
    previsto_baixar += sum(i["tamanho_bytes"] or 0 for i in itens if i["quick_xor"] is None)
    copias = len(itens) - len([c for c in por_conteudo if c[0] is not None]) \
        - len([i for i in itens if i["quick_xor"] is None])

    print("=" * 74)
    print("AMOSTRA E7 — composicao")
    print("=" * 74)
    print(f"{'cota':<34}{'pedido':>8}{'achado':>8}")
    for rotulo, pedido, achado in relato:
        marca = "" if achado >= min(pedido, 1) else "   (fila nao tem)"
        print(f"  {rotulo:<32}{pedido:>8}{achado:>8}{marca}")
    print("-" * 74)
    print(f"  {'TOTAL DE ARQUIVOS':<32}{'':>8}{len(itens):>8}")
    print()

    def resumo(campo):
        contagem: dict = {}
        for item in itens:
            contagem[item[campo]] = contagem.get(item[campo], 0) + 1
        return ", ".join(f"{k}={v}" for k, v in sorted(contagem.items(),
                                                       key=lambda x: str(x[0])))

    print(f"  por categoria    {resumo('categoria')}")
    print(f"  por prioridade   P" + resumo('prioridade').replace(", ", ", P"))
    print(f"  por estado       {resumo('estado')}")
    print(f"  menor arquivo    {formatar(min(i['tamanho_bytes'] or 0 for i in itens))}")
    print(f"  maior arquivo    {formatar(max(i['tamanho_bytes'] or 0 for i in itens))}")
    print(f"  soma dos tamanhos          {formatar(total_bytes)}")
    print(f"  previsto baixar            {formatar(previsto_baixar)}")
    print(f"  previsto economizar        {formatar(total_bytes - previsto_baixar)} "
          f"({copias} copia(s) nao baixada(s))")
    print(f"  sem quick_xor no inventario {sum(1 for i in itens if not i['quick_xor'])}")
    print()

    # --- gravar a composicao ANTES de qualquer download -----------------
    csv_path = saida / "amostra_e7.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        escritor = csv.DictWriter(fh, fieldnames=list(itens[0].keys()), delimiter=";")
        escritor.writeheader()
        escritor.writerows(itens)

    antes = {
        "gerado_em": cu.agora_iso(),
        "teto_mb": args.teto_mb,
        "ids": [i["id"] for i in itens],
        "total_bytes": total_bytes,
        "previsto_baixar": previsto_baixar,
        "conteudos_antes": con.execute("SELECT COUNT(*) FROM conteudo").fetchone()[0],
        "completed_antes": con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0],
        "extracoes_antes": con.execute("SELECT COUNT(*) FROM extracao").fetchone()[0],
        "itens": {str(i["id"]): {"estado": i["estado"], "conteudo_id": i["conteudo_id"],
                                 "tamanho_bytes": i["tamanho_bytes"],
                                 "categoria": i["categoria"],
                                 "prioridade": i["prioridade"],
                                 "quick_xor": i["quick_xor"],
                                 "item_id": i["item_id"],
                                 "caminho_rel": i["caminho_rel"],
                                 "cota": i["cota"]} for i in itens},
    }
    json_path = saida / "amostra_e7_antes.json"
    json_path.write_text(json.dumps(antes, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [f"# Amostra E7 — {len(itens)} arquivos", "",
          f"Gerada em {antes['gerado_em']}. Selecao deterministica "
          f"(`ORDER BY item_id` dentro de cada cota, sem `RANDOM()`).", "",
          "| # | cota | prioridade | categoria | tamanho | copias no acervo | caminho |",
          "|---|---|---|---|---:|---:|---|"]
    for n, item in enumerate(itens, 1):
        md.append(f"| {n} | {item['cota']} | P{item['prioridade']} | {item['categoria']} "
                  f"| {formatar(item['tamanho_bytes'] or 0)} "
                  f"| {item['ocorrencias_no_acervo']} | `{item['caminho_rel']}` |")
    (saida / "amostra_e7.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"  composicao   {csv_path}")
    print(f"               {saida / 'amostra_e7.md'}")
    print(f"  retrato      {json_path}")
    print()
    print("Para processar SOMENTE esta amostra:")
    print(f"  python scripts\\worker.py run --ids {','.join(str(i['id']) for i in itens)}")
    print()
    print("Depois:  python scripts\\16_amostra_e7.py relatar")
    return 0


# ----------------------------------------------------------------------
def relatar(cfg, con, args) -> int:
    json_path = cfg["_saida_path"] / "amostra_e7_antes.json"
    if not json_path.exists():
        print(f"nao achei {json_path}. Rode antes:  16_amostra_e7.py selecionar")
        return 1
    antes = json.loads(json_path.read_text(encoding="utf-8"))
    ids = antes["ids"]
    marcas = ",".join(str(i) for i in ids)

    agora = {str(linha["id"]): dict(linha) for linha in con.execute(
        f"SELECT id, estado, conteudo_id, tamanho_bytes, tentativas, ultimo_erro, "
        f"processado_em, duplicata_de, quick_xor FROM arquivo WHERE id IN ({marcas})")}

    baixados, evitados, falhas = [], [], []
    bytes_baixados = bytes_economizados = bytes_nao_tentados = 0
    for chave, registro in antes["itens"].items():
        atual = agora.get(chave, {})
        estado = atual.get("estado")
        tamanho = registro["tamanho_bytes"] or 0
        if estado == "DUPLICATE":
            evitados.append(chave)
            bytes_economizados += tamanho
        elif estado in ("COMPLETED", "PARTIAL", "OCR_REQUIRED"):
            baixados.append(chave)
            bytes_baixados += tamanho
        else:
            falhas.append((chave, estado, (atual.get("ultimo_erro") or "")[:90]))
            bytes_nao_tentados += tamanho

    conteudos_novos = con.execute("SELECT COUNT(*) FROM conteudo").fetchone()[0] \
        - antes["conteudos_antes"]
    # Ocorrencia que caiu num conteudo extraido por OUTRA ocorrencia: e o que
    # a deduplicacao economiza. (Nao confundir com "conteudo ja existente no
    # banco antes da amostra" — aqui interessa quem nao precisou extrair.)
    reaproveitados = con.execute(
        f"SELECT COUNT(*) FROM arquivo a JOIN conteudo c ON c.id = a.conteudo_id "
        f"WHERE a.id IN ({marcas}) AND c.representante_id <> a.id").fetchone()[0]
    completed_depois = con.execute(
        "SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0]

    print("=" * 74)
    print("AMOSTRA E7 — resultado")
    print("=" * 74)
    print(f"  arquivos selecionados      {len(ids)}")
    print(f"  extraidos (baixados)       {len(baixados)}")
    print(f"  evitados por deduplicacao  {len(evitados)}")
    print(f"  falhas                     {len(falhas)}")
    print()
    print(f"  bytes previstos (soma)     {formatar(antes['total_bytes'])}")
    print(f"  bytes previstos p/ baixar  {formatar(antes['previsto_baixar'])}")
    print(f"  bytes efetivamente lidos   {formatar(bytes_baixados)}")
    print(f"  bytes economizados (dedup) {formatar(bytes_economizados)}")
    print(f"  bytes nao baixados (falha) {formatar(bytes_nao_tentados)}")
    print()
    print(f"  linhas novas em `conteudo` {conteudos_novos}")
    print(f"  ocorrencias apontando p/ conteudo de outra ocorrencia  {reaproveitados}")
    print(f"  COMPLETED no banco         {antes['completed_antes']} -> {completed_depois}")
    print(f"  extracoes no banco         {antes['extracoes_antes']} -> "
          f"{con.execute('SELECT COUNT(*) FROM extracao').fetchone()[0]}")
    print()

    if falhas:
        print("  falhas por categoria:")
        por_estado: dict = {}
        for chave, estado, erro in falhas:
            por_estado.setdefault(estado or "?", []).append((chave, erro))
        for estado, lista in sorted(por_estado.items()):
            print(f"    {estado:<20} {len(lista)}")
            for chave, erro in lista[:5]:
                print(f"      #{chave} {antes['itens'][chave]['caminho_rel'][:60]}")
                print(f"           {erro}")
        print()

    print("  antes -> depois, arquivo a arquivo:")
    print(f"    {'id':>7} {'cat':<6} {'prio':<5} {'antes':<10} -> {'depois':<12} "
          f"{'conteudo':>9}  caminho")
    for chave in sorted(antes["itens"], key=lambda k: int(k)):
        registro = antes["itens"][chave]
        atual = agora.get(chave, {})
        print(f"    {chave:>7} {registro['categoria']:<6} P{registro['prioridade']:<4} "
              f"{registro['estado']:<10} -> {str(atual.get('estado')):<12} "
              f"{str(atual.get('conteudo_id') or '-'):>9}  "
              f"{registro['caminho_rel'][:52]}")
    print()

    # A prova de que uma ocorrencia nao apaga as outras.
    print("  conteudos desta amostra com mais de uma ocorrencia:")
    multiplos = con.execute(
        f"SELECT conteudo_id, COUNT(*) n FROM arquivo WHERE id IN ({marcas}) "
        f"AND conteudo_id IS NOT NULL GROUP BY conteudo_id HAVING COUNT(*) > 1 "
        f"ORDER BY n DESC").fetchall()
    if not multiplos:
        print("    (nenhum)")
    for grupo in multiplos:
        print(f"    conteudo #{grupo['conteudo_id']}: {grupo['n']} ocorrencias")
        for oc in con.execute(
                "SELECT arquivo_id, eh_representante, item_id, caminho_rel "
                "FROM v_ocorrencia WHERE conteudo_id=? ORDER BY arquivo_id",
                (grupo["conteudo_id"],)):
            marca = "representante" if oc["eh_representante"] else "             "
            print(f"      #{oc['arquivo_id']:<7} {marca}  {oc['item_id'][:34]:<34} "
                  f"{oc['caminho_rel'][:46]}")
    sobras = sorted((cu.RAIZ_REPO / "tmp").glob("e7_*"))
    print()
    print(f"  temporarios restantes em tmp/  {len(sobras)} "
          f"{[p.name for p in sobras] if sobras else ''}")
    return 0


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Amostra controlada para a prova do E7")
    ap.add_argument("comando", nargs="?", default="selecionar",
                    choices=["selecionar", "relatar"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--teto-mb", type=int, default=40,
                    help="nao escolhe arquivo maior que isto (padrao 40 MB)")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    try:
        return selecionar(cfg, con, args) if args.comando == "selecionar" \
            else relatar(cfg, con, args)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
