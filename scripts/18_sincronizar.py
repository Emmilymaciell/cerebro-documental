"""
18_sincronizar — uma rodada completa de sincronizacao, do inicio ao fim.

ONE-SHOT: roda, relata e sai. Nao tem laco infinito, nao fica residente e
nao processa documento nenhum. Foi escrito para o Agendador de Tarefas do
Windows chamar sozinho, de tempos em tempos.

    python scripts\\18_sincronizar.py --aplicar     # a rodada de verdade
    python scripts\\18_sincronizar.py --simular     # nao grava nada
    python scripts\\18_sincronizar.py               # = --simular

A ordem, e por que e essa:

  1. `12_scan_graph`   — o delta do Graph atualiza o espelho `item_nuvem`.
                         Checkpoint por pagina; `delta_link` so no fim da
                         rodada (`DEC-021`); HTTP 410 vira varredura completa
                         automatica (`DEC-035`). Falhou no meio: o token
                         valido anterior fica de pe e a rodada e retomavel.
  2. `17_aplicar_delta`— leva sumico, volta, rename, move e alteracao do
                         espelho para o catalogo `arquivo` (`DEC-038`).
  3. `19_ingerir_novos`— incorpora ao catalogo os arquivos que so existem no
                         espelho, com biblioteca, P0..P4 e estado inicial.
  4. relatorio com todos os numeros, em `logs/` e na tela.

Se o passo 1 nao chegar ao fim, os passos 2 e 3 NAO rodam e a saida e != 0.
Um espelho pela metade nao pode mandar no catalogo; o certo e rodar de novo,
que continua de onde parou.

O worker nao e tocado: este script nao le fila, nao baixa arquivo e nao
altera `controle`. Se o worker estava `PAUSADO`, continua `PAUSADO`.

Seguro rodar duas vezes seguidas: na segunda o delta nao traz nada, o passo 2
nao acha o que propagar e o passo 3 nao acha o que inserir.

Codigo de saida: 0 = rodada inteira concluida. != 0 = algo ficou pela metade
(o log e o relatorio dizem o que).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

scan_graph = cu.importar_script("12_scan_graph")
aplicar_delta = cu.importar_script("17_aplicar_delta")
ingerir_novos = cu.importar_script("19_ingerir_novos")
classificacao = cu.importar_script("02_classificar")

SAIDA_OK = 0
SAIDA_DELTA_INCOMPLETO = 3
SAIDA_PROPAGACAO_DIVERGENTE = 4
SAIDA_INGESTAO_DIVERGENTE = 5
SAIDA_ERRO = 1


# ----------------------------------------------------------------------
# Retrato do banco (o mesmo dos dois lados da rodada)
# ----------------------------------------------------------------------

def retrato(con) -> dict:
    dados = ingerir_novos.retrato(con)
    dados["espelho_arquivos"] = con.execute(
        "SELECT COUNT(*) FROM item_nuvem WHERE eh_pasta=0 AND presente=1").fetchone()[0]
    dados["espelho_pastas"] = con.execute(
        "SELECT COUNT(*) FROM item_nuvem WHERE eh_pasta=1 AND presente=1").fetchone()[0]
    return dados


def estado_do_worker(con) -> str:
    linha = con.execute("SELECT comando, estado FROM controle WHERE id=1").fetchone()
    if not linha:
        return "sem registro de controle"
    return f"{linha['estado']} (comando {linha['comando']})"


# ----------------------------------------------------------------------
# Passo 1 — o delta do Graph
# ----------------------------------------------------------------------

def passo_delta(con, execucao_id: int, log, modo: str, limite: int,
                drive_alvo: str | None) -> tuple[str, dict, int]:
    """Atualiza o espelho. Devolve (status, contadores, chamadas ao Graph)."""
    import graph_ro  # importado aqui: `--simular` nao precisa de rede

    graph = graph_ro.GraphRO(pausa_minima_s=0.02)
    inventario = scan_graph.InventarioNuvem(graph, con, execucao_id, log, limite)
    drives = inventario.registrar_drives(graph.site_id())
    alvos = [d for d in drives if not drive_alvo or d.get("name") == drive_alvo]
    if not alvos:
        raise RuntimeError(f"drive '{drive_alvo}' nao existe. Ha: "
                           f"{[d.get('name') for d in drives]}")
    status = "concluida"
    for d in alvos:
        resultado = inventario.sincronizar(d["id"], d.get("name", "?"), modo)
        if resultado != "concluida":
            status = resultado
            break
    return status, inventario.contadores, graph.chamadas


# ----------------------------------------------------------------------
# Passo 2 — do espelho para o catalogo
# ----------------------------------------------------------------------

def passo_propagacao(con, execucao_id: int, aplicar: bool,
                     cfg: dict | None = None) -> tuple[dict, bool]:
    p = aplicar_delta.Propagacao(con, execucao_id, aplicar, cfg=cfg)
    sumiram, voltaram = p.sumiram(), p.voltaram()
    recam, alter = p.recaminhados(), p.alterados()
    desal = p.desalinhados()
    p.novos_no_espelho()
    previsto = {"sumiram": len(sumiram), "voltaram": len(voltaram),
                "recaminhados": len(recam), "alterados": len(alter),
                "realinhados": len(desal)}
    if not aplicar:
        numeros = dict(p.contadores)
        numeros.update(previsto)
        return numeros, True
    p.marcar_sumidos(sumiram)
    p.restaurar(voltaram)
    p.recaminhar(recam)
    p.realinhar(desal)
    p.requeue(alter)
    con.commit()
    conferido = all(p.contadores[k] == v for k, v in previsto.items())
    return dict(p.contadores), conferido


# ----------------------------------------------------------------------
# Passo 3 — os arquivos novos
# ----------------------------------------------------------------------

def passo_ingestao(con, cfg: dict, execucao_id: int, log,
                   aplicar: bool) -> tuple[dict, bool]:
    regras = classificacao.carregar_regras(cfg.get("classificacao"))
    ing = ingerir_novos.Ingestao(con, cfg, regras, execucao_id, log)
    derivados = ing.preparar(ing.pendentes())
    if not aplicar:
        numeros = dict(ing.contadores)
        numeros["a_inserir"] = len(derivados)
        return numeros, True
    antes = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    inseridos = ing.inserir(derivados)
    ing.atualizar_pastas(derivados)
    depois = con.execute("SELECT COUNT(*) FROM arquivo").fetchone()[0]
    conferido = (inseridos == len(derivados) and depois - antes == len(derivados))
    numeros = dict(ing.contadores)
    numeros["a_inserir"] = len(derivados)
    return numeros, conferido


# ----------------------------------------------------------------------
# Relatorio
# ----------------------------------------------------------------------

def relatorio(cfg: dict, resumo: dict) -> Path:
    """Grava o relatorio da rodada em logs/ e devolve o caminho."""
    pasta = cfg["_logs_path"]
    pasta.mkdir(parents=True, exist_ok=True)
    # Ate o milissegundo: duas rodadas no mesmo segundo (acontece no teste, e
    # acontecera numa retentativa do Agendador) sobrescreviam o relatorio da
    # primeira, e o historico de execucao sumia sem aviso.
    carimbo = f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}"
    destino = pasta / f"sincronizacao_{carimbo}.json"
    cu.assert_escrita_permitida(destino, cfg["_raiz_path"])
    destino.write_text(json.dumps(resumo, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
    return destino


def imprimir(resumo: dict) -> None:
    largura = 70
    print()
    print("=" * largura)
    print(f"SINCRONIZACAO — {resumo['status']}  "
          f"({'APLICADO' if resumo['aplicar'] else 'SIMULACAO'})")
    print("=" * largura)
    print(f"  tempo total      {resumo['duracao_s'] / 60:.2f} min")
    print(f"  worker           {resumo['worker']}")
    print()

    print("  1. delta do Graph")
    delta = resumo["delta"]
    if delta.get("pulado"):
        print(f"     {delta['pulado']}")
    else:
        print(f"     status         {delta['status']}")
        for rotulo in ("itens", "novos", "alterados", "renomeados", "movidos",
                       "removidos", "reapareceram", "paginas", "resyncs", "erros"):
            print(f"     {rotulo:<14} {delta['contadores'].get(rotulo, 0):,}"
                  .replace(",", "."))
        print(f"     chamadas Graph {delta.get('chamadas', 0)}")
    print()

    print("  2. propagacao para o catalogo")
    prop = resumo["propagacao"]
    if prop.get("pulado"):
        print(f"     {prop['pulado']}")
    else:
        for rotulo in ("sumiram", "voltaram", "recaminhados", "alterados",
                       "realinhados", "novos_no_espelho", "reeleitos"):
            print(f"     {rotulo:<18} {prop['numeros'].get(rotulo, 0):,}"
                  .replace(",", "."))
        print(f"     conferido      {'SIM' if prop['conferido'] else 'NAO !!'}")
    print()

    print("  3. ingestao dos novos")
    ing = resumo["ingestao"]
    if ing.get("pulado"):
        print(f"     {ing['pulado']}")
    else:
        numeros = ing["numeros"]
        print(f"     {'pendentes':<18} {numeros.get('pendentes', 0):,}".replace(",", "."))
        print(f"     {'inseridos':<18} {numeros.get('inseridos', 0):,}".replace(",", "."))
        print(f"     {'a inserir':<18} {numeros.get('a_inserir', 0):,}".replace(",", "."))
        if numeros.get("por_estado"):
            print(f"     por estado     {numeros['por_estado']}")
        if numeros.get("por_prioridade"):
            print(f"     por prioridade {numeros['por_prioridade']}")
        print(f"     conferido      {'SIM' if ing['conferido'] else 'NAO !!'}")
    print()

    print("  o banco, antes e depois")
    antes, depois = resumo["antes"], resumo["depois"]
    print(f"  {'':<19}{'antes':>11}{'depois':>11}")
    for chave in antes:
        marca = " " if antes[chave] == depois[chave] else "*"
        print(f"  {marca}{chave:<18}{antes[chave]:>11}{depois[chave]:>11}")
    print()
    print(f"  relatorio: {resumo['relatorio']}")
    print(f"  saida: {resumo['codigo_saida']}")
    print("=" * largura)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Uma rodada completa de sincronizacao (one-shot)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--aplicar", action="store_true",
                    help="grava de verdade. Sem isto, so simula.")
    ap.add_argument("--simular", action="store_true",
                    help="explicitamente nao grava nada (e o padrao)")
    ap.add_argument("--sem-delta", action="store_true",
                    help="nao fala com o Graph; usa o espelho como esta")
    ap.add_argument("--completo", action="store_true",
                    help="ignora os tokens e varre a nuvem inteira")
    ap.add_argument("--incremental", action="store_true", help="so o que mudou")
    ap.add_argument("--limite", type=int, default=0,
                    help="para o delta depois de N itens (fica retomavel)")
    ap.add_argument("--drive", default=None, help="so esta biblioteca")
    ap.add_argument("--quieto", action="store_true")
    args = ap.parse_args(argv)

    aplicar = bool(args.aplicar) and not args.simular
    modo = "completo" if args.completo else ("incremental" if args.incremental
                                             else "auto")

    cfg = cu.carregar_config(args.config)
    cu.assert_escrita_permitida(cfg["_banco_path"], cfg["_raiz_path"])
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='item_nuvem'").fetchone():
        print("banco sem schema v3. Rode antes:  python scripts\\03_migrar_v3.py")
        return 2

    execucao_id = cu.abrir_execucao(con, "sincronizar", args.drive or "", vars(args))
    log = cu.Log(cfg["_logs_path"], "sincronizar", con, execucao_id,
                 verboso=not args.quieto)
    t0 = time.perf_counter()

    resumo = {
        "execucao_id": execucao_id, "aplicar": aplicar, "modo": modo,
        "iniciada_em": cu.agora_iso(), "status": "concluida",
        "worker": estado_do_worker(con),
        "antes": retrato(con), "depois": {},
        "delta": {}, "propagacao": {}, "ingestao": {},
        "codigo_saida": SAIDA_OK, "duracao_s": 0.0, "relatorio": "",
    }
    codigo = SAIDA_OK

    try:
        # --- 1. delta ---------------------------------------------------
        if args.sem_delta or not aplicar:
            motivo = ("pulado por --sem-delta" if args.sem_delta else
                      "pulado na simulacao: o delta sempre escreve no espelho")
            resumo["delta"] = {"pulado": motivo}
            log.info(f"passo 1 (delta) {motivo}")
        else:
            log.info("passo 1: delta do Graph")
            status, contadores, chamadas = passo_delta(
                con, execucao_id, log, modo, args.limite, args.drive)
            resumo["delta"] = {"status": status, "contadores": contadores,
                               "chamadas": chamadas}
            if status != "concluida":
                resumo["status"] = f"delta {status}"
                codigo = SAIDA_DELTA_INCOMPLETO
                log.aviso("o delta nao chegou ao fim; os passos 2 e 3 nao rodam. "
                          "O checkpoint esta salvo: rode de novo para continuar.")

        # --- 2. propagacao ----------------------------------------------
        if codigo == SAIDA_OK:
            log.info("passo 2: propagacao para o catalogo")
            numeros, conferido = passo_propagacao(con, execucao_id, aplicar, cfg)
            resumo["propagacao"] = {"numeros": numeros, "conferido": conferido}
            if not conferido:
                resumo["status"] = "propagacao divergente"
                codigo = SAIDA_PROPAGACAO_DIVERGENTE
                log.erro("o total propagado nao bateu com o anunciado")
        else:
            resumo["propagacao"] = {"pulado": "o delta nao chegou ao fim"}

        # --- 3. ingestao -------------------------------------------------
        if codigo == SAIDA_OK:
            log.info("passo 3: ingestao dos arquivos novos")
            numeros, conferido = passo_ingestao(con, cfg, execucao_id, log, aplicar)
            resumo["ingestao"] = {"numeros": numeros, "conferido": conferido}
            if not conferido:
                resumo["status"] = "ingestao divergente"
                codigo = SAIDA_INGESTAO_DIVERGENTE
                log.erro("o total inserido nao bateu com o anunciado")
        else:
            resumo["ingestao"] = {"pulado": "a rodada parou antes"}

    except KeyboardInterrupt:
        # Desfaz o que ficou pela metade (um item com estado sem evento, ou
        # um representante sumido sem reeleicao): o commit do `finally` so
        # pode gravar trabalho inteiro (REGRA 8). O checkpoint do delta ja
        # foi commitado pagina a pagina e nao e afetado.
        con.rollback()
        resumo["status"] = "interrompida pelo usuario"
        codigo = SAIDA_ERRO
        log.aviso("interrompida pelo usuario — o checkpoint da pagina esta salvo")
    except Exception as exc:                                   # noqa: BLE001
        con.rollback()
        resumo["status"] = f"erro: {type(exc).__name__}: {exc}"
        codigo = SAIDA_ERRO
        log.erro(f"a rodada parou: {type(exc).__name__}: {exc}")
    finally:
        con.commit()

    # --- 4. relatorio ---------------------------------------------------
    resumo["depois"] = retrato(con)
    resumo["worker"] = estado_do_worker(con)
    resumo["duracao_s"] = round(time.perf_counter() - t0, 2)
    resumo["codigo_saida"] = codigo
    resumo["relatorio"] = str(relatorio(cfg, resumo))

    if not args.quieto:
        imprimir(resumo)

    cu.fechar_execucao(
        con, execucao_id, "concluida" if codigo == SAIDA_OK else "erro",
        {"arquivos": (resumo["delta"].get("contadores") or {}).get("itens", 0),
         "novos": (resumo["ingestao"].get("numeros") or {}).get("inseridos", 0),
         "erros": 0 if codigo == SAIDA_OK else 1},
        resumo["duracao_s"], resumo["status"])
    log.fechar()
    con.close()
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
