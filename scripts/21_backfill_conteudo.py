"""
21_backfill_conteudo — conserta a deduplicacao que ja esta no banco.

    python scripts\\21_backfill_conteudo.py            # SIMULA (padrao)
    python scripts\\21_backfill_conteudo.py --aplicar  # backup + grava

O que corrige (docs/11_QA_AUDITORIA.md):

  M-4   extracoes anteriores ao v4 sem linha em `conteudo` (os COMPLETED
        legados): cria o `conteudo` a partir da `extracao` existente, pela
        chave (quick_xor, tamanho), e liga a ocorrencia. Nada e baixado nem
        reextraido. Arquivo de 0 byte nao ganha conteudo (DEC-011).
  CR-2  duplicatas legadas presas a um representante que sumiu: entram no
        mesmo `conteudo` (mesmo quick_xor e tamanho), e a reeleicao escolhe
        uma copia viva — os trechos do indice vao junto. A que nao tiver
        como ser ligada (representante sem extracao, hash diferente) volta
        para a fila com evento: nunca fica presa num estado terminal.
  A-5   ocorrencias que mudaram de bytes mas ainda apontam para o conteudo
        antigo (ex.: #128398): o vinculo e desfeito, com o conteudo antigo
        no evento; se ela era o representante e ha copia viva, reelege.
  A-1   conteudos orfaos com copia viva e contadores divergentes: reelege e
        reconta tudo.

Garantias:
  - SIMULACAO de verdade: tudo roda dentro de uma transacao que termina em
    ROLLBACK. Os numeros mostrados sao os que o --aplicar produziria.
  - --aplicar faz BACKUP antes (API de backup do SQLite, consistente com
    WAL) em `data/backup/`, e grava UM ITEM POR TRANSACAO (REGRA 8).
  - Idempotente: rodar de novo nao acha mais nada a fazer.
  - Nada some (REGRA 6): nenhum DELETE. Cada mudanca vira evento.
  - Saida 0 so se, no fim, `cu.checar_invariantes` nao tiver violacao.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402


def fazer_backup(con: sqlite3.Connection, banco: Path, raiz: Path) -> Path:
    pasta = banco.parent / "backup"
    destino = pasta / f"{banco.stem}_pre_backfill_{datetime.now():%Y%m%d_%H%M%S}.db"
    cu.assert_escrita_permitida(destino, raiz)
    pasta.mkdir(parents=True, exist_ok=True)
    alvo = sqlite3.connect(str(destino))
    try:
        con.backup(alvo)
    finally:
        alvo.close()
    return destino


def contagens(con) -> dict:
    def um(sql):
        return con.execute(sql).fetchone()[0]
    return {
        "arquivos": um("SELECT COUNT(*) FROM arquivo"),
        "conteudos": um("SELECT COUNT(*) FROM conteudo"),
        "extracoes": um("SELECT COUNT(*) FROM extracao"),
        "trechos": um("SELECT COUNT(*) FROM texto_fts"),
        "eventos": um("SELECT COUNT(*) FROM evento_arquivo"),
        "COMPLETED": um("SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'"),
        "DUPLICATE": um("SELECT COUNT(*) FROM arquivo WHERE estado='DUPLICATE'"),
        "na_fila": um("SELECT COUNT(*) FROM arquivo WHERE presente=1 AND estado IN "
                      "('QUEUED','CHANGED','READY','WAITING_HYDRATION','ERROR_RETRYABLE')"),
    }


class Backfill:
    def __init__(self, con, execucao_id, aplicar: bool):
        self.con = con
        self.execucao_id = execucao_id
        self.aplicar = aplicar
        self.n = {"legados_vinculados": 0, "conteudos_criados": 0,
                  "conteudos_reaproveitados": 0, "copias_ligadas": 0,
                  "copias_divergentes": 0, "desvinculados_bytes_novos": 0,
                  "devolvidas_a_fila": 0, "reeleicoes": 0, "contadores_corrigidos": 0}

    def _fim_do_item(self):
        """REGRA 8: no --aplicar, cada item e uma transacao. Na simulacao
        nada e commitado — o ROLLBACK do fim desfaz tudo."""
        if self.aplicar:
            self.con.commit()

    # 1. M-4 + CR-2: extracoes legadas ganham conteudo; copias entram junto
    def vincular_legados(self):
        for legado in cu.grupos_legados(self.con):
            r = cu.vincular_legado(self.con, legado, self.execucao_id)
            self.n["legados_vinculados"] += 1
            self.n["conteudos_criados" if r["criado"] else "conteudos_reaproveitados"] += 1
            self.n["copias_ligadas"] += len(r["ligados"]) - 1
            self.n["copias_divergentes"] += len(r["divergentes"])
            self.n["reeleicoes"] += 1 if r["novo_representante"] else 0
            self._fim_do_item()

    # 2. A-5: ocorrencia com bytes novos apontando para o conteudo antigo
    def desvincular_bytes_novos(self):
        for arquivo_id in cu.checar_invariantes(self.con)["ocorrencia_com_hash_de_outro_conteudo"]:
            r = cu.desvincular_conteudo(self.con, arquivo_id, self.execucao_id,
                                        "backfill: bytes mudaram (A-5)")
            cu.registrar_evento(self.con, arquivo_id, self.execucao_id,
                                "desvinculado_de_conteudo",
                                {"motivo": "o hash da ocorrencia nao e o do conteudo (A-5)",
                                 **r})
            self.n["desvinculados_bytes_novos"] += 1
            self.n["reeleicoes"] += 1 if r.get("novo_representante") else 0
            self._fim_do_item()

    # 3. CR-2: copia viva presa a representante ausente, sem como ligar
    def devolver_orfas(self):
        for arquivo_id in cu.checar_invariantes(self.con)["duplicata_com_representante_ausente"]:
            a = self.con.execute("SELECT id, estado, duplicata_de, conteudo_id, suportado "
                                 "FROM arquivo WHERE id=?", (arquivo_id,)).fetchone()
            if a["conteudo_id"]:
                # Tem conteudo: a reeleicao resolve (passo 4). Nao e orfa.
                continue
            novo_estado = "QUEUED" if a["suportado"] else "UNSUPPORTED"
            self.con.execute(
                "UPDATE arquivo SET estado=?, duplicata_de=NULL, tentativas=0, "
                "proxima_tentativa_em=NULL, alterado_em=? WHERE id=?",
                (novo_estado, cu.agora_iso(), arquivo_id))
            cu.registrar_evento(self.con, arquivo_id, self.execucao_id,
                                "duplicata_orfa_devolvida",
                                {"estado_anterior": a["estado"],
                                 "duplicata_de_anterior": a["duplicata_de"],
                                 "estado_novo": novo_estado,
                                 "motivo": "representante ausente e sem conteudo para "
                                           "religar (CR-2)"})
            self.n["devolvidas_a_fila"] += 1
            self._fim_do_item()

    # 4. A-1: orfaos reelegiveis e contadores
    def reeleger_e_recontar(self):
        for conteudo_id in cu.conteudos_orfaos_reelegiveis(self.con):
            if cu.reeleger_conteudo(self.con, conteudo_id, self.execucao_id,
                                    "backfill: representante orfao"):
                self.n["reeleicoes"] += 1
            self._fim_do_item()
        for conteudo_id in cu.checar_invariantes(self.con)["contador_divergente"]:
            cu.recontar_ocorrencias(self.con, conteudo_id)
            self.n["contadores_corrigidos"] += 1
            self._fim_do_item()

    def rodar(self):
        self.vincular_legados()
        self.desvincular_bytes_novos()
        self.devolver_orfas()
        self.reeleger_e_recontar()


def imprimir_invariantes(titulo: str, violacoes: dict, info: dict) -> None:
    print(f"  {titulo}")
    for nome, ids in violacoes.items():
        marca = "  " if not ids else "!!"
        exemplo = f"  ex.: {ids[:5]}" if ids else ""
        print(f"    {marca} {nome:<40} {len(ids):>6}{exemplo}")
    print("     (informativo) " + ", ".join(f"{k}={v}" for k, v in info.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill de conteudo e consertos de dedupe")
    ap.add_argument("--config", default=None)
    ap.add_argument("--aplicar", action="store_true",
                    help="faz backup e grava (sem isto, simula com ROLLBACK)")
    ap.add_argument("--sem-backup", action="store_true",
                    help="so para teste automatizado; nunca usar no banco real")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    banco = cfg["_banco_path"]
    con = cu.conectar(banco, cfg["_raiz_path"])
    t0 = time.perf_counter()

    antes = contagens(con)
    viol_antes = cu.checar_invariantes(con)
    print("=" * 72)
    print(f"BACKFILL DE CONTEUDO — {'APLICANDO' if args.aplicar else 'SIMULACAO (rollback)'}")
    print("=" * 72)
    imprimir_invariantes("invariantes ANTES", viol_antes, cu.informativo_dedupe(con))

    execucao_id = None
    if args.aplicar:
        if not args.sem_backup:
            destino = fazer_backup(con, banco, cfg["_raiz_path"])
            print(f"\n  backup: {destino} ({destino.stat().st_size / 1024 ** 2:.0f} MB)")
        execucao_id = cu.abrir_execucao(con, "backfill_conteudo", "", vars(args))

    b = Backfill(con, execucao_id, args.aplicar)
    status = "erro"
    try:
        b.rodar()
        depois = contagens(con)
        viol_depois = cu.checar_invariantes(con)
        info_depois = cu.informativo_dedupe(con)
        status = "concluida" if args.aplicar else "simulacao"
    finally:
        if not args.aplicar:
            con.rollback()                      # a simulacao nao deixa rastro
        else:
            con.commit()
            if execucao_id:
                cu.fechar_execucao(con, execucao_id, status,
                                   {"arquivos": b.n["legados_vinculados"],
                                    "alterados": b.n["copias_ligadas"]},
                                   time.perf_counter() - t0, str(b.n))

    print("\n  o que foi feito" + ("" if args.aplicar else " (e DESFEITO — simulacao)"))
    for chave, valor in b.n.items():
        print(f"    {chave:<28} {valor:>8}")
    print(f"\n  {'':<12}{'antes':>10}{'depois':>10}")
    for chave in antes:
        marca = " " if antes[chave] == depois[chave] else "*"
        print(f"  {marca}{chave:<11}{antes[chave]:>10}{depois[chave]:>10}")
    intactos = all(antes[k] == depois[k] for k in ("arquivos", "extracoes", "trechos"))
    print(f"\n  nada apagado (arquivos, extracoes, trechos iguais): {'SIM' if intactos else 'NAO !!'}")
    print()
    imprimir_invariantes("invariantes DEPOIS", viol_depois, info_depois)
    if not args.aplicar:
        agora = contagens(con)
        print(f"\n  SIMULACAO: banco intocado ({'SIM' if agora == antes else 'NAO !!'}). "
              f"Para gravar:  --aplicar")
    con.close()
    return 0 if intactos and cu.total_violacoes(viol_depois) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
