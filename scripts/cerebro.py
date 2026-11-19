"""
cerebro — o comando unico de producao. Um so verbo por vez, sem IA nenhuma.

    python scripts\\cerebro.py processar      # <- e isto que o Agendador chama
    python scripts\\cerebro.py status
    python scripts\\cerebro.py pausar
    python scripts\\cerebro.py retomar
    python scripts\\cerebro.py relatorio
    python scripts\\cerebro.py erros          # erros por causa, com exemplos
    python scripts\\cerebro.py cobertura      # quanto ja foi lido (--por prioridade)
    python scripts\\cerebro.py sync           # delta -> propagacao -> ingestao (simula;
                                              #   --aplicar grava)

Ele nao reimplementa nada: orquestra `worker.py` (o processamento),
`operacao.py` (lock, orcamento, janela, metricas, cobertura), `control.py`
(pausa) e `18_sincronizar.py` (sync).

Codigos de saida — e assim que o Agendador de Tarefas sabe o que houve:

    0  rodou e encerrou limpo (fila vazia, janela fechada, teto atingido...)
    2  banco sem schema
    3  ja existe worker rodando (o lock esta tomado) — nada foi feito
    4  ha PAUSE/STOP pendente: a Emmily mandou parar e isso NAO se desfaz
       sozinho, nem depois de reiniciar o Windows
    5  busca v5 com versao divergente do codigo (rode 05_migrar_v5.py)
    6  AGUARDANDO_CREDENCIAL: o Graph recusou a credencial em serie (401/403
       sistemico). Nenhuma tentativa de documento foi gasta; renove o segredo
       do aplicativo e rode de novo (docs/10_OPERACAO_AUTONOMA.md)

`sync` devolve os codigos do 18_sincronizar.py.

Manual completo: docs/10_OPERACAO_AUTONOMA.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402
import control  # noqa: E402
import operacao as op  # noqa: E402
import worker as worker_mod  # noqa: E402


# ----------------------------------------------------------------------
# CONCORRENCIA — por que 1, e o que faltaria para mudar
# ----------------------------------------------------------------------
#
# O parametro existe, o padrao e 1, e valor > 1 e RECUSADO de proposito.
# Nao e limitacao de codigo; e a REGRA "nada de arquitetura sofisticada sem
# medicao que justifique":
#
#   1. Nao ha benchmark. A unica medicao real do projeto (amostra E7, 39
#      arquivos) e de um worker so: ~23 itens/min, ~1,1 MB/s. Nao sabemos se
#      o gargalo e a banda, o Graph ou o extrator. Com banda saturada, dois
#      workers entregam o mesmo total e dobram a chance de erro.
#   2. O Graph tem rate limit (429 / `activityLimitReached`) por aplicativo,
#      nao por processo. Paralelizar aumenta o 429, e o backoff devolve o
#      ganho.
#   3. SQLite em WAL aceita varios leitores, mas UM escritor. Cada item e uma
#      transacao de escrita (REGRA 8). Dois workers disputariam o `busy_timeout`
#      no ponto mais caro do ciclo.
#   4. O lock de processo (operacao.TravaProcesso) e exclusivo por construcao.
#      Concorrencia real exigiria particionar a fila (por `prioridade` ou por
#      `id % N`) e um lock por particao — desenho novo, nao um parametro.
#
# O benchmark que decide: rodar 500 itens com 1 worker e 500 com 2, medindo
# itens/min, MB/s, taxa de 429 e `busy` no SQLite. Enquanto isso nao existir,
# a resposta honesta e 1.
MOTIVO_CONCORRENCIA_1 = (
    "concorrencia > 1 nao esta liberada: nao ha benchmark que a justifique e o "
    "Graph tem rate limit por aplicativo. Ver o comentario no topo de "
    "scripts/cerebro.py e docs/10_OPERACAO_AUTONOMA.md."
)


def _conectar(cfg):
    return cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])


# ----------------------------------------------------------------------
# processar
# ----------------------------------------------------------------------

def acao_processar(args) -> int:
    if int(getattr(args, "concorrencia", 1) or 1) != 1:
        print(MOTIVO_CONCORRENCIA_1)
        return 2
    codigo, estado = worker_mod.executar(args)
    if not args.quieto and estado:
        print(f"\ncerebro: sessao encerrada em {estado} (codigo {codigo})")
    return codigo


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------

def acao_status(args) -> int:
    cfg = cu.carregar_config(args.config)
    lock = op.caminho_lock(cfg)
    con = _conectar(cfg)
    try:
        m = op.medir(con, fonte=cfg.get("fonte", "graph"))
        print(op.texto_metricas(m))
        ocupada = op.TravaProcesso.ocupada(lock)
        dono = op.TravaProcesso.dono(lock) or {}
        print(f"  lock                  {'TOMADO' if ocupada else 'livre'}  ({lock.name})")
        if dono:
            print(f"    ultimo dono         pid {dono.get('pid')} em "
                  f"{dono.get('host')} desde {dono.get('desde')}")
        print(f"  orcamento             {op.Orcamento.de_config(cfg).descricao()}")
        janela = op.JanelaExecucao.de_config(cfg)
        print(f"  janela                {janela.descricao()}"
              f"  [{'aberta' if janela.dentro() else 'fechada'}]")
        # O diagnostico mais util: "esta travado?" respondido por fato, nao
        # por impressao. RODANDO sem lock = sessao morta sem encerrar.
        if m["worker_estado"] == "RODANDO" and not ocupada:
            print("\n  ATENCAO: o banco diz RODANDO mas nao ha lock tomado — a sessao"
                  "\n  anterior morreu sem encerrar. O proximo `processar` assume e"
                  "\n  devolve os orfaos para a fila. Nada a fazer.")
        elif ocupada:
            print("\n  Ha worker vivo. Para parar com seguranca: "
                  "python scripts\\cerebro.py pausar")
        print("=" * 66)
    finally:
        con.close()
    return 0


# ----------------------------------------------------------------------
# pausar / retomar
# ----------------------------------------------------------------------

def acao_pausar(args) -> int:
    cfg = cu.carregar_config(args.config)
    con = _conectar(cfg)
    try:
        return control.pausar(con, args.espera, lock=op.caminho_lock(cfg))
    finally:
        con.close()


def acao_retomar(args) -> int:
    cfg = cu.carregar_config(args.config)
    lock = op.caminho_lock(cfg)
    con = _conectar(cfg)
    try:
        cu.escrever_controle(con, comando="RUN", motivo="")
        if op.TravaProcesso.ocupada(lock):
            print("comando RUN gravado; o worker que esta rodando volta a puxar a fila.")
        else:
            print("comando RUN gravado. Nao ha worker rodando — inicie com:")
            print("  python scripts\\cerebro.py processar")
    finally:
        con.close()
    return 0


def acao_parar(args) -> int:
    cfg = cu.carregar_config(args.config)
    con = _conectar(cfg)
    try:
        cu.escrever_controle(con, comando="STOP")
        print("comando STOP gravado. O worker encerra depois do item atual.")
    finally:
        con.close()
    return 0


# ----------------------------------------------------------------------
# relatorio
# ----------------------------------------------------------------------

def acao_relatorio(args) -> int:
    cfg = cu.carregar_config(args.config)
    con = _conectar(cfg)
    try:
        so_erros = getattr(args, "erros", False)
        so_sessao = getattr(args, "sessao", False)
        tudo = not (so_erros or so_sessao)
        if tudo or so_sessao:
            print(op.texto_relatorio_sessao(
                op.relatorio_sessao(con, getattr(args, "execucao", None))))
            print()
        if tudo or so_erros:
            print(op.texto_relatorio_erros(op.relatorio_erros(con)))
    finally:
        con.close()
    return 0


# ----------------------------------------------------------------------
# erros / cobertura / sync
# ----------------------------------------------------------------------

def acao_erros(args) -> int:
    cfg = cu.carregar_config(args.config)
    con = _conectar(cfg)
    try:
        print(op.texto_relatorio_erros(op.relatorio_erros(con)))
    finally:
        con.close()
    return 0


def acao_cobertura(args) -> int:
    cfg = cu.carregar_config(args.config)
    con = _conectar(cfg)
    try:
        print(op.texto_cobertura(op.cobertura(con, args.por), args.por))
    finally:
        con.close()
    return 0


def acao_sync(args) -> int:
    """Uma rodada de sincronizacao: delta do Graph -> propagacao -> ingestao.

    Nao reimplementa nada: delega ao `18_sincronizar.py`, com os mesmos
    codigos de saida. Sem `--aplicar` so simula (padrao do 18).
    """
    import importlib
    sincronizar = importlib.import_module("18_sincronizar")
    argv = ["--config", args.config] if args.config else []
    for opcao in ("aplicar", "completo", "incremental", "sem_delta", "quieto"):
        if getattr(args, opcao):
            argv.append("--" + opcao.replace("_", "-"))
    if args.limite:
        argv += ["--limite", str(args.limite)]
    if args.drive:
        argv += ["--drive", args.drive]
    return sincronizar.main(argv)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="cerebro", description="Comando unico de operacao do Cerebro Documental")
    subs = ap.add_subparsers(dest="acao", required=True)

    p = subs.add_parser("processar", help="processa a fila ate acabar (ou ate uma trava)")
    worker_mod.adicionar_opcoes(p)
    p.add_argument("--concorrencia", type=int, default=1,
                   help="workers simultaneos. HOJE so 1 e aceito — sem benchmark "
                        "e com rate limit no Graph, paralelizar seria chute.")
    p.set_defaults(func=acao_processar)

    p = subs.add_parser("status", help="onde tudo esta, com ETA medido do banco")
    p.add_argument("--config", default=None)
    p.set_defaults(func=acao_status)

    p = subs.add_parser("pausar", help="pausa graciosa: termina o item atual e grava")
    p.add_argument("--config", default=None)
    p.add_argument("--espera", type=float, default=180.0)
    p.set_defaults(func=acao_pausar)

    p = subs.add_parser("retomar", help="desfaz a pausa (a fila volta a andar)")
    p.add_argument("--config", default=None)
    p.set_defaults(func=acao_retomar)

    p = subs.add_parser("parar", help="encerra a sessao do worker depois do item atual")
    p.add_argument("--config", default=None)
    p.set_defaults(func=acao_parar)

    p = subs.add_parser("relatorio", help="relatorio da sessao e dos erros por categoria")
    p.add_argument("--config", default=None)
    p.add_argument("--erros", action="store_true", help="so os erros")
    p.add_argument("--sessao", action="store_true", help="so a sessao")
    p.add_argument("--execucao", type=int, default=None, help="id de uma sessao especifica")
    p.set_defaults(func=acao_relatorio)

    p = subs.add_parser("erros", help="erros agrupados por causa, com exemplos")
    p.add_argument("--config", default=None)
    p.set_defaults(func=acao_erros)

    p = subs.add_parser("cobertura", help="quanto do acervo ja foi lido, por categoria "
                                          "ou prioridade")
    p.add_argument("--config", default=None)
    p.add_argument("--por", choices=["categoria", "prioridade"], default="categoria")
    p.set_defaults(func=acao_cobertura)

    p = subs.add_parser("sync", help="uma rodada de sincronizacao com a nuvem "
                                     "(simula; --aplicar grava)")
    p.add_argument("--config", default=None)
    p.add_argument("--aplicar", action="store_true", help="grava de verdade")
    p.add_argument("--completo", action="store_true", help="varre a nuvem inteira")
    p.add_argument("--incremental", action="store_true", help="so o que mudou")
    p.add_argument("--sem-delta", action="store_true", help="nao fala com o Graph")
    p.add_argument("--limite", type=int, default=0)
    p.add_argument("--drive", default=None)
    p.add_argument("--quieto", action="store_true")
    p.set_defaults(func=acao_sync)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
