"""
05_migrar_v5 — aplica o schema v5 (camada de busca) e indexa por CONTEUDO.

    python scripts\\05_migrar_v5.py                    # SIMULACAO: so relatorio
    python scripts\\05_migrar_v5.py --aplicar          # backup + transacao + conferencias

Sem `--aplicar` o banco e aberto SOMENTE LEITURA e nada e escrito: o
relatorio diz o que seria feito, com as contagens de antes e os trechos que
sairiam, calculados de verdade (mesmo codigo, em memoria).

Com `--aplicar`, nesta ordem:

  1. toma o lock do worker (`operacao.TravaProcesso`): com worker rodando,
     recusa — migracao e worker nao escrevem juntos;
  2. backup pela API de backup do SQLite para `data/backup/` e `quick_check`
     NA COPIA antes de seguir;
  3. UMA transacao explicita (`isolation_level=None` + BEGIN IMMEDIATE): o
     driver em modo legado faria COMMIT implicito antes do DDL, e o DDL
     ficaria fora do ROLLBACK. Dentro dela: DDL v5, versoes em `meta`,
     `schema_versao='5'`, indexacao de tudo que esta em `v_reindexar`, e as
     conferencias. Qualquer conferencia que falhe => ROLLBACK, banco intacto;
  4. depois do COMMIT: `PRAGMA quick_check` do banco inteiro.

O TEXTO de cada conteudo vem, nesta ordem (docs/09_BUSCA.md, secao 10):
  a) do `texto_fts` v4, RECONSTRUIDO pagina a pagina e conferido (recortado
     de novo tem de dar os mesmos trechos, e unido tem de dar o derivado) —
     preserva pagina/slide/aba sem rede, sem download, sem extrator;
  b) do derivado `.txt.gz` (perde a pagina, exceto em planilha);
  c) sem fonte: o conteudo fica em `v_reindexar` e o relatorio diz qual.

Nao migra linha de `texto_fts` como esta: o trecho v4 nao tem conteudo_id,
posicao nem normalizacao. Tambem NAO apaga o `texto_fts` — aposentar o
indice antigo e passo separado, a mao, depois das conferencias.

Idempotente: rodar de novo nao reindexa o que nao mudou (trava do
`sha256_texto`) e, se nao houver nada a indexar, nem faz backup.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import busca  # noqa: E402
import cerebro_util as cu  # noqa: E402

VERSAO = "5"

# Contagens do v4 que a migracao NAO pode mudar.
TABELAS_INTOCAVEIS = ("arquivo", "conteudo", "extracao", "texto_fts", "evento_arquivo",
                      "item_nuvem")

# O que o DDL v5 usa do v4 — por NOME (o agente que mexe no v2..v4 pode mudar a
# ordem das colunas; isto nao pode quebrar).
REQUISITOS = {
    "meta": ("chave", "valor", "atualizado_em"),
    "conteudo": ("id", "quick_xor", "tamanho_bytes", "estado", "extrator",
                 "versao_extrator", "texto_path", "representante_id"),
    "arquivo": ("id", "conteudo_id", "presente", "prioridade", "temporario", "suportado",
                "ultima_vez_em"),
    "execucao": ("id", "etapa", "argumentos", "iniciada_em", "encerrada_em", "status",
                 "host", "observacao", "duracao_s"),
    "v_ocorrencia": ("conteudo_id", "arquivo_id", "eh_representante", "drive_id", "item_id",
                     "caminho_rel", "caminho_nuvem", "web_url", "prioridade",
                     "estado_ocorrencia", "criado_por", "modificado_por",
                     "modificado_em_nuvem"),
}


def _uma(con, sql, *args):
    linha = con.execute(sql, args).fetchone()
    return linha[0] if linha else None


def colunas(con, objeto: str) -> set[str]:
    try:
        return {d[0] for d in con.execute(f"SELECT * FROM {objeto} LIMIT 0").description}
    except sqlite3.DatabaseError:
        return set()


def conferir_requisitos(con) -> list[str]:
    faltas = []
    for objeto, exigidas in REQUISITOS.items():
        existentes = colunas(con, objeto)
        if not existentes:
            faltas.append(f"{objeto}: nao existe")
            continue
        faltam = [c for c in exigidas if c not in existentes]
        if faltam:
            faltas.append(f"{objeto}: faltam colunas {faltam}")
    return faltas


def contagens(con) -> dict:
    saida = {}
    for tabela in TABELAS_INTOCAVEIS:
        if colunas(con, tabela):
            saida[tabela] = _uma(con, f"SELECT COUNT(*) FROM {tabela}")
    return saida


def contagens_v5(con) -> dict:
    if not busca.v5_ativo(con):
        return {"segmento": 0, "trecho": 0, "busca_fts": 0, "indice_conteudo": 0}
    return {t: _uma(con, f"SELECT COUNT(*) FROM {t}")
            for t in ("segmento", "trecho", "busca_fts", "indice_conteudo")}


def elegiveis(con) -> list:
    """O que a migracao indexaria: `v_reindexar` se o v5 existe; senao, a
    mesma regra escrita a mao (texto extraido + ocorrencia presente)."""
    if busca.v5_ativo(con) and not busca.conferir_versoes(con):
        sql = "SELECT conteudo_id FROM v_reindexar"
    else:
        sql = ("SELECT c.id FROM conteudo c WHERE c.estado IN ('EXTRAIDO','PARCIAL')"
               " AND EXISTS (SELECT 1 FROM arquivo a WHERE a.conteudo_id=c.id"
               " AND a.presente=1) ORDER BY c.id")
    ids = [r[0] for r in con.execute(sql)]
    return [con.execute("SELECT * FROM conteudo WHERE id=?", (i,)).fetchone() for i in ids]


def arquivos_do_conteudo(con, conteudo) -> list[int]:
    rep = conteudo["representante_id"]
    outros = [r[0] for r in con.execute(
        "SELECT id FROM arquivo WHERE conteudo_id=? AND id IS NOT ? ORDER BY id",
        (conteudo["id"], rep))]
    return ([rep] if rep else []) + outros


def planejar(con, cfg) -> dict:
    """Decide a fonte de cada conteudo e calcula, EM MEMORIA, o que sairia."""
    limites = cfg.get("limites") or {}
    tam_v4 = int(limites.get("trecho_chars", 1500))
    sob_v4 = int(limites.get("trecho_sobreposicao", 150))
    indice_v4 = busca.carregar_indice_v4(con)
    plano = []
    for c in elegiveis(con):
        paginas, fonte, detalhe = busca.fonte_do_conteudo(
            c, indice_v4, arquivos_do_conteudo(con, c), tam_v4, sob_v4)
        segmentos, _ = busca.preparar(paginas or [], c["extrator"])
        trechos = sum(len(busca.POLITICA.cortar(s[4])) for s in segmentos)
        plano.append({"conteudo_id": c["id"], "extrator": c["extrator"],
                      "versao_extrator": c["versao_extrator"], "fonte": fonte,
                      "detalhe": detalhe, "paginas": paginas,
                      "segmentos": len(segmentos), "trechos": trechos})
    sem_conteudo = 0
    sem_conteudo_vivos = 0
    if indice_v4:
        ids = list(indice_v4)
        for i in range(0, len(ids), 500):
            lote = ids[i:i + 500]
            marcas = ",".join("?" * len(lote))
            sem_conteudo += _uma(con, f"SELECT COUNT(*) FROM arquivo WHERE id IN ({marcas})"
                                      " AND conteudo_id IS NULL", *lote)
            sem_conteudo_vivos += _uma(con, f"SELECT COUNT(*) FROM arquivo WHERE id IN"
                                            f" ({marcas}) AND conteudo_id IS NULL AND"
                                            " presente=1", *lote)
    return {"itens": plano, "arquivos_no_indice_v4": len(indice_v4),
            "indice_v4_sem_conteudo": sem_conteudo,
            "indice_v4_sem_conteudo_presentes": sem_conteudo_vivos,
            "tamanho_v4": tam_v4, "sobreposicao_v4": sob_v4}


def imprimir_plano(con, plano: dict) -> None:
    versao = _uma(con, "SELECT valor FROM meta WHERE chave='schema_versao'") or "?"
    print(f"  schema atual            v{versao}")
    print(f"  v5 presente             {'sim' if busca.v5_ativo(con) else 'nao'}")
    if busca.v5_ativo(con):
        for d in busca.conferir_versoes(con):
            print(f"  versao divergente       {d}")
    print(f"  politica de corte       {busca.VERSAO_CHUNKER}  normalizador "
          f"{busca.VERSAO_NORMALIZADOR}")
    for tabela, n in contagens(con).items():
        print(f"  {tabela:<23} {n}")
    for tabela, n in contagens_v5(con).items():
        print(f"  v5.{tabela:<20} {n}")
    itens = plano["itens"]
    fontes = Counter(i["fonte"] or "SEM_FONTE" for i in itens)
    print(f"\n  a indexar               {len(itens)} conteudos  {dict(fontes)}")
    print(f"  trechos previstos       {sum(i['trechos'] for i in itens)}"
          f"  (segmentos {sum(i['segmentos'] for i in itens)})")
    print(f"  indice v4               {plano['arquivos_no_indice_v4']} arquivos;"
          f" {plano['indice_v4_sem_conteudo']} sem conteudo_id"
          f" ({plano['indice_v4_sem_conteudo_presentes']} presentes) — ficam fora do v5"
          f" ate ganharem conteudo")
    for i in itens:
        if i["fonte"] != "indice_v4":
            print(f"    conteudo {i['conteudo_id']}: fonte {i['fonte'] or 'NENHUMA'}"
                  f" ({i['detalhe']})")


def fazer_backup(banco: Path, raiz_proibida) -> Path:
    """API de backup do SQLite, a partir de conexao somente leitura."""
    pasta = banco.parent / "backup"
    destino = pasta / f"{banco.stem}_pre_v5_{datetime.now():%Y%m%d_%H%M%S}.db"
    cu.assert_escrita_permitida(destino, raiz_proibida)
    pasta.mkdir(parents=True, exist_ok=True)
    origem = sqlite3.connect(f"file:{banco.as_posix()}?mode=ro", uri=True)
    copia = sqlite3.connect(str(destino))
    try:
        origem.backup(copia)
        ok = _uma(copia, "PRAGMA quick_check")
    finally:
        copia.close()
        origem.close()
    if ok != "ok":
        raise RuntimeError(f"backup {destino} reprovou no quick_check: {ok}")
    return destino


def aplicar(cfg, plano_ro: dict, args) -> int:
    import operacao as op

    banco = cfg["_banco_path"]
    trava = op.TravaProcesso(op.caminho_lock(cfg), rotulo="05_migrar_v5")
    if not trava.adquirir():
        print(f"\nRECUSADO: o lock do worker esta tomado ({trava.dono(trava.caminho)})."
              " Pare o worker e rode de novo.")
        return 3
    try:
        # "Nada a fazer" = nenhum conteudo COM FONTE na fila. Um conteudo sem
        # texto nenhum continua em v_reindexar a cada rodada, e nao pode
        # custar um backup de 1 GB toda vez.
        indexaveis = [i for i in plano_ro["itens"] if i["paginas"]]
        nada_a_fazer = (not indexaveis and plano_ro["v5_ativo"]
                        and plano_ro["schema_versao"] == VERSAO
                        and not plano_ro["divergencias"])
        if nada_a_fazer:
            print("\n  nada a indexar (com fonte) e o banco ja esta em v5: sem backup.")
        elif args.sem_backup:
            print("\n  backup: PULADO (--sem-backup, so teste)")
        else:
            t0 = time.perf_counter()
            destino = fazer_backup(banco, cfg["_raiz_path"])
            print(f"\n  backup: {destino} ({destino.stat().st_size / 1024 ** 2:.0f} MB,"
                  f" {time.perf_counter() - t0:.1f} s, quick_check ok)")

        con = sqlite3.connect(str(banco), isolation_level=None, timeout=120.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=120000")
        inicio = time.perf_counter()
        antes = contagens(con)
        v5_antes = contagens_v5(con)
        agora = cu.agora_iso()
        try:
            con.execute("BEGIN IMMEDIATE")
            execucao_id = con.execute(
                "INSERT INTO execucao (etapa, argumentos, iniciada_em, status, host)"
                " VALUES ('migrar_v5', ?, ?, 'em_andamento', ?)",
                (json.dumps({"aplicar": True, "chunker": busca.VERSAO_CHUNKER,
                             "normalizador": busca.VERSAO_NORMALIZADOR}),
                 agora, os.environ.get("COMPUTERNAME", ""))).lastrowid
            busca.aplicar_ddl(con)
            busca.gravar_versoes(con, agora)
            con.execute("INSERT INTO meta (chave, valor, atualizado_em) VALUES"
                        " ('schema_versao', ?, ?) ON CONFLICT(chave) DO UPDATE SET"
                        " valor=excluded.valor, atualizado_em=excluded.atualizado_em",
                        (VERSAO, agora))

            # Replaneja DENTRO da transacao: agora `v_reindexar` existe e e a
            # fila oficial (inclui EXTRATOR_NOVO/CHUNKER_NOVO numa re-execucao).
            plano = planejar(con, cfg)
            acoes = Counter()
            sem_fonte = []
            for n, item in enumerate(plano["itens"], start=1):
                if not item["paginas"]:
                    sem_fonte.append(item["conteudo_id"])
                    continue
                r = busca.indexar_conteudo(
                    con, item["conteudo_id"], item["paginas"], extrator=item["extrator"],
                    versao_extrator=item["versao_extrator"], execucao_id=execucao_id,
                    fonte_texto=item["fonte"], agora=agora)
                acoes[r["acao"]] += 1
                if args.simular_falha and n == 1:
                    raise RuntimeError("falha simulada depois do 1o conteudo (--simular-falha)")

            # ---- conferencias, ainda dentro da transacao -------------------
            problemas = []
            s = busca.saude(con)
            for chave, valor in s.items():
                if chave == "a_reindexar":
                    if valor != len(sem_fonte):
                        problemas.append(f"v_reindexar={valor}, esperado {len(sem_fonte)}"
                                         " (so os sem fonte)")
                elif valor != 0:
                    problemas.append(f"v_busca_saude.{chave}={valor}")
            if not busca.integridade_fts(con):
                problemas.append("FTS5 integrity-check (rank=1) reprovou")
            for tabela in ("segmento", "trecho", "indice_conteudo"):
                ruins = con.execute(f"PRAGMA foreign_key_check({tabela})").fetchall()
                if ruins:
                    problemas.append(f"foreign_key_check({tabela}): {len(ruins)} linha(s)")
            depois = contagens(con)
            depois.pop("execucao", None)
            if depois != antes:
                problemas.append(f"tabelas do v4 mudaram: {antes} -> {depois}")
            if busca.conferir_versoes(con):
                problemas.append(f"versoes: {busca.conferir_versoes(con)}")
            if problemas:
                raise RuntimeError("conferencia reprovou: " + "; ".join(problemas))

            segundos = time.perf_counter() - inicio
            con.execute("UPDATE execucao SET encerrada_em=?, status='ok', duracao_s=?,"
                        " observacao=? WHERE id=?",
                        (cu.agora_iso(), round(segundos, 2),
                         json.dumps({"acoes": dict(acoes), "sem_fonte": sem_fonte}),
                         execucao_id))
            con.execute("COMMIT")
        except BaseException as exc:
            if con.in_transaction:
                con.execute("ROLLBACK")
            print(f"\nROLLBACK — nada foi gravado. Motivo: {exc}")
            con.close()
            return 1

        v5_depois = contagens_v5(con)
        t_quick = time.perf_counter()
        quick = [r[0] for r in con.execute("PRAGMA quick_check")]
        t_quick = time.perf_counter() - t_quick
        con.close()
        print(f"\n  execucao {execucao_id}: {dict(acoes)}"
              + (f"; sem fonte (ficam em v_reindexar): {sem_fonte}" if sem_fonte else ""))
        for tabela in antes:
            marca = "OK " if antes[tabela] == depois[tabela] else "!! "
            print(f"    {marca}{tabela:<16} {antes[tabela]} -> {depois[tabela]}")
        for tabela in v5_depois:
            print(f"       v5.{tabela:<13} {v5_antes[tabela]} -> {v5_depois[tabela]}")
        print(f"  saude v5           todos os contadores de defeito = 0")
        print(f"  FTS integrity-check (rank=1) ok;  foreign_key_check ok")
        print(f"  quick_check        {', '.join(quick[:3])}  ({t_quick:.1f} s)")
        print(f"  tempo da transacao {segundos:.2f} s")
        print(f"  schema agora       v{VERSAO}")
        return 0 if quick == ["ok"] else 1
    finally:
        trava.liberar()


def main() -> int:
    for saida in (sys.stdout, sys.stderr):
        try:
            saida.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Migra o banco para o schema v5 (busca)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--aplicar", action="store_true",
                    help="escreve (sem isto: so simulacao, banco aberto somente leitura)")
    ap.add_argument("--sem-backup", action="store_true",
                    help="so para teste automatizado; nunca no banco real")
    ap.add_argument("--simular-falha", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    banco = cfg["_banco_path"]
    cu.assert_escrita_permitida(banco, cfg["_raiz_path"])
    if not banco.exists():
        print(f"banco nao existe: {banco}")
        return 2

    print(f"banco: {banco}")
    ro = sqlite3.connect(f"file:{banco.as_posix()}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    faltas = conferir_requisitos(ro)
    if faltas:
        print("\nRECUSADO: o banco nao tem o que o v5 exige do v4:")
        for f in faltas:
            print(f"  - {f}")
        ro.close()
        return 2
    t0 = time.perf_counter()
    plano = planejar(ro, cfg)
    imprimir_plano(ro, plano)
    plano["v5_ativo"] = busca.v5_ativo(ro)
    plano["divergencias"] = busca.conferir_versoes(ro) if plano["v5_ativo"] else ["sem v5"]
    plano["schema_versao"] = _uma(ro, "SELECT valor FROM meta WHERE chave='schema_versao'")
    ro.close()
    print(f"  planejamento em     {time.perf_counter() - t0:.2f} s")

    if not args.aplicar:
        print("\nSIMULACAO: nada foi escrito. Para aplicar: --aplicar")
        return 0
    return aplicar(cfg, plano, args)


if __name__ == "__main__":
    raise SystemExit(main())
