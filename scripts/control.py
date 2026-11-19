"""
control — liga, pausa e retoma o worker SEM depender de nenhuma sessao de IA.

    python scripts\\control.py status
    python scripts\\control.py pause     # pausa graciosa: termina o item atual
    python scripts\\control.py resume    # libera a fila (depois: worker.py run)
    python scripts\\control.py stop      # encerra a sessao do worker
    python scripts\\control.py texto 123 # mostra o texto extraido do arquivo 123

`pause` so devolve o prompt quando o worker confirmou que parou num ponto
seguro — e diz explicitamente se ja da para fechar o computador.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402
import operacao as op  # noqa: E402


def pt(n) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "-"


# Fonte do processamento, lida da config em main(). Na nuvem a fila que o
# worker de fato pega e a `v_fila_nuvem`; a `v_fila` inclui itens fora do
# alcance da credencial (T-12 / DEC-017).
FONTE = "graph"


def contagens(con) -> dict:
    dados = {linha["estado"]: linha["arquivos"]
             for linha in con.execute("SELECT * FROM v_progresso")}
    vista = "v_fila_nuvem" if FONTE == "graph" else "v_fila"
    dados["_fila"] = con.execute(f"SELECT COUNT(*) FROM {vista}").fetchone()[0]
    dados["_total"] = con.execute(
        "SELECT COUNT(*) FROM arquivo WHERE presente=1").fetchone()[0]
    return dados


def mostrar_status(con, curto: bool = False, lock: Path | None = None) -> None:
    c = cu.ler_controle(con)
    vivo = (op.TravaProcesso.ocupada(lock) if lock else cu.processo_vivo(c["pid"]))
    dados = contagens(con)
    ultimo = con.execute("SELECT * FROM checkpoint ORDER BY id DESC LIMIT 1").fetchone()

    print("=" * 66)
    print(f"WORKER: {c['estado']}" + (f"  (pid {c['pid']}, vivo)" if vivo else "  (sem processo)"))
    print("=" * 66)
    if c["motivo"]:
        print(f"  motivo            {c['motivo']}")
    print(f"  comando pendente  {c['comando']}")
    print(f"  ultimo heartbeat  {c['heartbeat'] or '-'}")
    if c["item_atual"]:
        print(f"  item atual        {c['item_atual'][:70]}")
    print(f"  na sessao         {pt(c['processados_sessao'])} itens, "
          f"{pt(c['erros_sessao'])} erros")
    print()
    print(f"  arquivos no banco {pt(dados.get('_total'))}")
    print(f"  fila pronta       {pt(dados.get('_fila'))}")
    for estado in ("COMPLETED", "OCR_REQUIRED", "PARTIAL", "DUPLICATE", "UNSUPPORTED",
                   "QUEUED", "WAITING_HYDRATION", "ERROR_RETRYABLE", "ERROR_FINAL",
                   "INVENTORIED", "CHANGED", "MISSING", "SKIPPED"):
        if dados.get(estado):
            print(f"    {estado:<18} {pt(dados[estado]):>12}")
    if ultimo:
        print(f"\n  ultimo checkpoint {ultimo['em']}  ({ultimo['estado_worker']})")
        print(f"    concluidos {pt(ultimo['concluidos'])} | pendentes {pt(ultimo['pendentes'])}"
              f" | erros {pt(ultimo['erros'])}")
    print(f"  espaco livre      {cu.espaco_livre_gb('C:\\\\'):.1f} GB")
    if not curto:
        print("\n  detalhe completo:  python scripts\\99_status.py")
    print("=" * 66)


def pausar(con, espera_s: float = 180.0, lock: Path | None = None) -> int:
    c = cu.ler_controle(con)
    # Quem manda e o lock, nao o pid gravado (ver scripts/operacao.py). Sem o
    # lock, cai no `pid` — que e o que existia antes do E9.
    vivo = (op.TravaProcesso.ocupada(lock) if lock else cu.processo_vivo(c["pid"]))
    cu.escrever_controle(con, comando="PAUSE")
    if not vivo:
        cu.escrever_controle(con, estado="PAUSADO",
                             motivo="nenhum worker rodando quando a pausa foi pedida")
        print("Nao havia worker rodando. Fila travada em PAUSE.")
        print("SEGURO: pode fechar o processo e o computador.")
        mostrar_status(con, curto=True, lock=lock)
        return 0

    print(f"pausa pedida ao worker (pid {c['pid']}). Aguardando ele fechar o item atual...")
    limite = time.monotonic() + espera_s
    while time.monotonic() < limite:
        atual = cu.ler_controle(con)
        if atual["estado"] in op.ESTADOS_WORKER_SEGUROS:
            print(f"\nworker parou em estado seguro: {atual['estado']}")
            mostrar_status(con, curto=True, lock=lock)
            print("SEGURO: banco gravado, checkpoint registrado. "
                  "Pode fechar o processo e o computador.")
            return 0
        if (op.TravaProcesso.ocupada(lock) is False if lock
                else not cu.processo_vivo(atual["pid"])):
            print("\nprocesso do worker terminou.")
            mostrar_status(con, curto=True, lock=lock)
            print("SEGURO: pode fechar.")
            return 0
        time.sleep(2)

    print("\nATENCAO: o worker ainda nao confirmou a pausa em "
          f"{espera_s:.0f}s (pode estar num arquivo grande ou baixando da nuvem).")
    print("NAO e seguro desligar agora. Rode de novo:  python scripts\\control.py status")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Controle do worker")
    ap.add_argument("acao", choices=["status", "pause", "resume", "stop", "texto"])
    ap.add_argument("alvo", nargs="?", default=None, help="id do arquivo, para 'texto'")
    ap.add_argument("--config", default=None)
    ap.add_argument("--espera", type=float, default=180.0)
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    global FONTE
    FONTE = cfg.get("fonte", "graph")
    if not cfg["_banco_path"].exists():
        print(f"banco nao existe: {cfg['_banco_path']}\nRode:  python scripts\\00_init_db.py")
        return 2
    con = cu.conectar(cfg["_banco_path"])
    lock = op.caminho_lock(cfg)

    if args.acao == "status":
        mostrar_status(con, lock=lock)
    elif args.acao == "pause":
        return pausar(con, args.espera, lock=lock)
    elif args.acao == "resume":
        cu.escrever_controle(con, comando="RUN", motivo="")
        if op.TravaProcesso.ocupada(lock):
            print("comando RUN gravado; o worker que esta rodando volta a puxar a fila.")
        else:
            print("comando RUN gravado. Nao ha worker rodando — inicie com:")
            print("  python scripts\\worker.py run")
    elif args.acao == "stop":
        cu.escrever_controle(con, comando="STOP")
        print("comando STOP gravado. O worker encerra depois do item atual.")
    elif args.acao == "texto":
        if not args.alvo:
            print("informe o id:  python scripts\\control.py texto 123")
            return 2
        linha = con.execute(
            "SELECT a.caminho_rel, e.texto_path, e.status, e.paginas, e.caracteres "
            "FROM arquivo a LEFT JOIN extracao e ON e.arquivo_id=a.id WHERE a.id=?",
            (int(args.alvo),)).fetchone()
        if not linha or not linha["texto_path"]:
            print("sem texto extraido para esse id.")
            return 1
        print(f"# {linha['caminho_rel']}")
        print(f"# status={linha['status']} paginas={linha['paginas']} "
              f"caracteres={linha['caracteres']}\n")
        print(cu.ler_texto(linha["texto_path"])[:20000])

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
