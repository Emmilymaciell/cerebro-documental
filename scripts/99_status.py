"""
99_status — fotografia completa do cerebro documental. Nao altera nada.

    python scripts\\99_status.py
    python scripts\\99_status.py --pastas 30
    python scripts\\99_status.py --erros
    python scripts\\99_status.py --du        # mede o tamanho real de derivados/
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

ROTULOS_P = {0: "P0", 1: "P1", 2: "P2", 3: "P3", 4: "P4"}


def pt(n) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "-"


def gb(n) -> str:
    return f"{(n or 0)/1024**3:,.2f} GB".replace(",", ".")


def tamanho_pasta(caminho: Path) -> int:
    total = 0
    for raiz, _dirs, arquivos in __import__("os").walk(caminho):
        for nome in arquivos:
            try:
                total += (Path(raiz) / nome).stat().st_size
            except OSError:
                pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--pastas", type=int, default=12)
    ap.add_argument("--erros", action="store_true")
    ap.add_argument("--du", action="store_true", help="mede derivados/ no disco (demora)")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    if not cfg["_banco_path"].exists():
        print(f"banco nao existe ainda: {cfg['_banco_path']}")
        print("Rode:  python scripts\\00_init_db.py")
        return 2
    con = cu.conectar(cfg["_banco_path"])

    total, bytes_total, nuvem, temp = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(tamanho_bytes),0), COALESCE(SUM(online),0), "
        "COALESCE(SUM(temporario),0) FROM arquivo WHERE presente=1").fetchone()
    ausentes = con.execute("SELECT COUNT(*) FROM arquivo WHERE presente=0").fetchone()[0]
    pastas = con.execute("SELECT COUNT(*) FROM pasta").fetchone()[0]

    print("=" * 72)
    print("CEREBRO DOCUMENTAL — status geral")
    print("=" * 72)
    print(f"  banco               {cfg['_banco_path']}")
    print(f"  pastas mapeadas     {pt(pastas)}")
    print(f"  arquivos presentes  {pt(total)}")
    print(f"  volume catalogado   {(bytes_total or 0)/1024**4:.3f} TB")
    print(f"  so na nuvem         {pt(nuvem)}  ({100*nuvem/total if total else 0:.1f}%)")
    print(f"  temporarios         {pt(temp)}")
    print(f"  ausentes (MISSING)  {pt(ausentes)}")

    # ---- worker -------------------------------------------------------
    c = cu.ler_controle(con)
    vivo = cu.processo_vivo(c["pid"])
    print(f"\n  WORKER              {c['estado']}"
          + (f"  (pid {c['pid']}, vivo)" if vivo else "  (sem processo)"))
    if c["motivo"]:
        print(f"    motivo            {c['motivo']}")
    print(f"    comando           {c['comando']}")
    print(f"    ultima atividade  {c['heartbeat'] or '-'}")
    if c["item_atual"]:
        print(f"    item atual        {c['item_atual'][:60]}")

    # ---- maquina de estados ------------------------------------------
    print("\n  ESTADOS")
    for linha in con.execute("SELECT * FROM v_progresso"):
        print(f"    {linha['estado']:<18} {pt(linha['arquivos']):>12}  {gb(linha['bytes']):>12}")
    # T-12 / A-2: a fila e a que o worker consome. Com a fonte na nuvem
    # (padrao, DEC-012) e a `v_fila_nuvem` (DEC-030); a `v_fila` inclui itens
    # sem identidade da nuvem ou fora do escopo (DEC-017) que o worker NUNCA
    # pega — e a previsao de horas abaixo sairia inflada.
    fonte = cfg.get("fonte", "graph")
    vista = "v_fila_nuvem" if fonte == "graph" else "v_fila"
    fila = con.execute(f"SELECT COUNT(*) FROM {vista}").fetchone()[0]
    print(f"    {'>> FILA PRONTA':<18} {pt(fila):>12}  ({vista}, fonte {fonte})")
    if vista != "v_fila":
        fora = con.execute("SELECT COUNT(*) FROM v_fila").fetchone()[0] - fila
        if fora:
            print(f"    {'   fora do alcance':<18} {pt(fora):>12}  (na v_fila, mas sem "
                  f"identidade/presenca na nuvem: o worker nao os pega)")

    # ---- prioridades --------------------------------------------------
    print("\n  POR PRIORIDADE (ordem de processamento)")
    print(f"    {'':<4} {'arquivos':>10} {'suportados':>11} {'concluidos':>11} {'na fila':>9} {'volume':>12}")
    for linha in con.execute("SELECT * FROM v_cobertura_prioridade"):
        rotulo = ROTULOS_P.get(linha["prioridade"], str(linha["prioridade"]))
        print(f"    {rotulo:<4} {pt(linha['arquivos']):>10} {pt(linha['suportados']):>11} "
              f"{pt(linha['concluidos']):>11} {pt(linha['na_fila']):>9} {gb(linha['bytes']):>12}")

    # ---- categorias ---------------------------------------------------
    print("\n  POR CATEGORIA DE ARQUIVO")
    print(f"    {'categoria':<12} {'arquivos':>10} {'volume':>12} {'nuvem':>9} "
          f"{'concl.':>8} {'ocr':>7} {'s/extr':>8} {'erro':>6}")
    for linha in con.execute("SELECT * FROM v_cobertura"):
        print(f"    {linha['categoria']:<12} {pt(linha['arquivos']):>10} "
              f"{gb(linha['bytes']):>12} {pt(linha['so_na_nuvem']):>9} "
              f"{pt(linha['concluidos']):>8} {pt(linha['aguardando_ocr']):>7} "
              f"{pt(linha['sem_extrator']):>8} {pt(linha['com_erro']):>6}")

    # ---- produtividade ------------------------------------------------
    print("\n  PROCESSAMENTO")
    ext = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(caracteres),0) chars, COALESCE(SUM(paginas),0) pags, "
        "COALESCE(SUM(bytes_texto),0) bytes_txt, COALESCE(SUM(trechos),0) trechos, "
        "COALESCE(AVG(duracao_ms),0) media_ms FROM extracao").fetchone()
    print(f"    extracoes         {pt(ext['n'])}")
    print(f"    paginas lidas     {pt(ext['pags'])}")
    print(f"    caracteres        {pt(ext['chars'])}")
    print(f"    trechos no indice {pt(ext['trechos'])}")
    print(f"    tempo medio/item  {ext['media_ms']/1000:.2f} s")
    bytes_proc = con.execute(
        "SELECT COALESCE(SUM(tamanho_bytes),0) FROM arquivo WHERE estado IN "
        "('COMPLETED','PARTIAL','OCR_REQUIRED','DUPLICATE')").fetchone()[0]
    print(f"    bytes processados {gb(bytes_proc)}")

    sessoes = con.execute(
        "SELECT COALESCE(SUM(duracao_s),0) s, COALESCE(SUM(arquivos_vistos),0) n "
        "FROM execucao WHERE etapa='worker' AND duracao_s IS NOT NULL").fetchone()
    if sessoes["s"] > 0 and sessoes["n"] > 0:
        taxa = sessoes["n"] / sessoes["s"]
        print(f"    velocidade media  {taxa*60:.1f} itens/min "
              f"({sessoes['n']} itens em {sessoes['s']/3600:.2f} h de worker)")
        if fila and taxa > 0:
            horas = fila / taxa / 3600
            print(f"    previsao da fila  ~{horas:.1f} h  "
                  f"({pt(fila)} da {vista} pela velocidade observada, nao e garantia)")

    # ---- armazenamento -------------------------------------------------
    print("\n  ARMAZENAMENTO")
    print(f"    espaco livre C:   {cu.espaco_livre_gb('C:\\\\'):.1f} GB")
    print(f"    banco             {cfg['_banco_path'].stat().st_size/1024**2:.1f} MB")
    print(f"    texto derivado    {(ext['bytes_txt'] or 0)/1024**2:.1f} MB (comprimido, "
          f"somado do banco)")
    if args.du:
        print(f"    derivados/ real   {tamanho_pasta(cfg['_derivados_path'])/1024**2:.1f} MB")

    # ---- duplicatas e versoes ------------------------------------------
    dup = con.execute("SELECT COUNT(*), COALESCE(SUM(copias-1),0) FROM v_duplicata").fetchone()
    fam = con.execute("SELECT COUNT(*) FROM v_familia_versao").fetchone()[0]
    print(f"\n  DUPLICATAS EXATAS   {pt(dup[0])} grupos / {pt(dup[1])} copias excedentes")
    print(f"  FAMILIAS DE VERSAO  {pt(fam)} grupos por nome normalizado")

    # ---- checkpoints ---------------------------------------------------
    ultimo = con.execute("SELECT * FROM checkpoint ORDER BY id DESC LIMIT 1").fetchone()
    if ultimo:
        print(f"\n  ULTIMO CHECKPOINT   {ultimo['em']} ({ultimo['estado_worker']})")
        if ultimo["ultimo_item"]:
            print(f"    ultimo item       {ultimo['ultimo_item'][:60]}")
        print(f"    concluidos {pt(ultimo['concluidos'])} | pendentes "
              f"{pt(ultimo['pendentes'])} | erros {pt(ultimo['erros'])}")

    print("\n  EXECUCOES (ultimas 6)")
    for e in con.execute(
        "SELECT id, etapa, status, iniciada_em, duracao_s, arquivos_vistos, erros "
        "FROM execucao ORDER BY id DESC LIMIT 6"
    ):
        dur = f"{(e['duracao_s'] or 0)/60:.1f} min"
        print(f"    #{e['id']:<4} {e['etapa']:<13} {e['status']:<13} {e['iniciada_em'][:19]}  "
              f"{pt(e['arquivos_vistos']):>9} it  {dur:>9}  {pt(e['erros'])} erros")

    print(f"\n  MAIORES PASTAS (top {args.pastas})")
    for d in con.execute(
        "SELECT p.caminho_rel, p.prioridade, p.arquivos, p.bytes FROM pasta p "
        "WHERE p.arquivos > 0 ORDER BY p.arquivos DESC LIMIT ?", (args.pastas,)
    ):
        nome = d["caminho_rel"] if len(d["caminho_rel"]) <= 62 else "..." + d["caminho_rel"][-59:]
        print(f"    {ROTULOS_P.get(d['prioridade'],'?')} {pt(d['arquivos']):>8} arq "
              f"{gb(d['bytes']):>11}  {nome}")

    if args.erros:
        print("\n  ULTIMOS ERROS")
        for linha in con.execute(
            "SELECT a.caminho_rel, a.estado, a.tentativas, a.ultimo_erro FROM arquivo a "
            "WHERE a.estado IN ('ERROR_FINAL','ERROR_RETRYABLE') "
            "ORDER BY a.id DESC LIMIT 20"
        ):
            print(f"    {linha['estado']:<16} t={linha['tentativas']} "
                  f"{(linha['ultimo_erro'] or '')[:60]}")
            print(f"        {linha['caminho_rel'][:80]}")

    print("=" * 72)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
