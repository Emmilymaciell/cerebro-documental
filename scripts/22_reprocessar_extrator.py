"""
22_reprocessar_extrator — devolve a fila o que foi extraido por versao
ANTERIOR do extrator (REGRA 9, T-17).

    python scripts\\22_reprocessar_extrator.py                     # simula
    python scripts\\22_reprocessar_extrator.py --aplicar           # grava
    python scripts\\22_reprocessar_extrator.py --categoria pdf --aplicar
    python scripts\\22_reprocessar_extrator.py --versao pdf/1.0
    python scripts\\22_reprocessar_extrator.py --estado PARTIAL --erro "adiada"

A REGRA 9 promete: "mudou a versao, aquele extrator reprocessa; os outros
nao". Ate aqui nada comparava `extracao.versao` com a versao que o extrator
produz hoje (`extratores.VERSOES`), e as correcoes de extrator nao
alcancavam nada ja processado.

Como decide (`cerebro_util.extracoes_desatualizadas`):
  - a unidade e o CONTEUDO: vale a extracao MAIS RECENTE de qualquer
    ocorrencia dele. Ja reextraido na versao atual (pelo novo representante,
    depois de uma reeleicao) nao volta, mesmo que o ex-representante MISSING
    guarde a linha velha;
  - padrao: versao de extrator (`pdf/1.0`) != versao atual da categoria;
  - MARCAS nao sao versao antiga: `'-'` (arquivo de 0 byte) e
    `'adiado/teto_tamanho'` (PARTIAL acima do teto, gravado pelo worker sem
    download). So entram com filtro explicito (`--erro adiada`,
    `--versao adiado/teto_tamanho`, `--estado`) ou `--sem-versao` — e assim
    que os PARTIAL adiados voltam quando houver como extrai-los;
  - UNSUPPORTED "sem leitor" (`SEM_LEITOR: ...`, extensao que continua sem
    biblioteca) tambem nao volta no padrao: reextrair daria o mesmo
    `nao_suportado`. So com `--erro SEM_LEITOR`;
  - UM item por conteudo: o REPRESENTANTE. As copias continuam cobertas;
  - COMPLETED de versao atual NUNCA volta.

Na gravacao, so conta o item cujo UPDATE casou linha (`rowcount`): se o
estado mudou entre o plano e o `--aplicar` (o worker pegou o item), ele e
pulado, sem evento.

O que muda no item escolhido: `estado='QUEUED'`, tentativas zeradas, e um
evento `reprocessar_versao_extrator` com o retrato da extracao anterior
(versao, status, texto_path, caracteres, trechos).

Trade-off declarado (a extracao anterior ate a nova ficar pronta):
  - ENQUANTO espera na fila, nada se perde: `extracao`, `conteudo`, o
    derivado `texto.gz` e os trechos continuam la e a busca segue
    respondendo com o texto da versao anterior;
  - QUANDO o worker reextrai com sucesso, a linha de `extracao`
    (UNIQUE arquivo_id+extrator), o derivado (mesmo caminho) e os trechos
    sao SUBSTITUIDOS na mesma transacao — a versao anterior sai, a nova
    entra, sem janela sem texto;
  - se a reextracao FALHAR, a linha de `extracao` passa a dizer `erro`
    (e assim que o worker grava), mas `conteudo` continua apontando para o
    derivado anterior e os trechos antigos ficam no indice; o retrato da
    versao anterior esta no evento. Guardar a extracao antiga numa tabela
    propria exigiria mudar o `worker.processar()` e o schema — fora do
    escopo desta etapa.

Sem `--aplicar`, nada e gravado.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

ESTADOS_FILA = ("QUEUED", "CHANGED", "READY", "WAITING_HYDRATION", "ERROR_RETRYABLE")


def lista(texto: str | None) -> list[str] | None:
    return [x.strip() for x in texto.split(",") if x.strip()] if texto else None


def planejar(con, args) -> tuple[list[dict], Counter]:
    """Quem volta para a fila, e quem fica de fora (e por que)."""
    candidatos = cu.extracoes_desatualizadas(
        con, categorias=lista(args.categoria), versoes=lista(args.versao),
        erro_contem=args.erro, estados=lista(args.estado),
        incluir_sem_versao=args.sem_versao)
    fora: Counter = Counter()
    plano = []
    for c in candidatos:
        alvo = con.execute("SELECT id, estado, presente, presente_nuvem, suportado, "
                           "drive_id, item_id FROM arquivo WHERE id=?",
                           (c["alvo_id"],)).fetchone()
        if alvo is None or not alvo["presente"]:
            fora["sem ocorrencia viva (so historico)"] += 1
            continue
        if alvo["estado"] in ESTADOS_FILA:
            fora["ja esta na fila"] += 1
            continue
        if not alvo["suportado"]:
            fora["categoria sem extrator ativo"] += 1
            continue
        if not (alvo["drive_id"] and alvo["item_id"] and alvo["presente_nuvem"]):
            fora["sem identidade da nuvem (worker nao pode baixar)"] += 1
            continue
        c["estado_alvo"] = alvo["estado"]
        plano.append(c)
        if args.limite and len(plano) >= args.limite:
            break
    return plano, fora


def aplicar(con, plano: list[dict], execucao_id: int) -> tuple[int, int]:
    """Devolve (feitos, pulados). Pulado = o UPDATE nao casou linha: o estado
    do alvo mudou depois do plano. Nao ganha evento — nada aconteceu com ele."""
    feitos = pulados = 0
    for c in plano:
        anterior = con.execute(
            "SELECT versao, status, texto_path, perfil_path, caracteres, trechos, "
            "executado_em FROM extracao WHERE arquivo_id=? AND extrator=?",
            (c["arquivo_id"], c["extrator"])).fetchone()
        cur = con.execute(
            "UPDATE arquivo SET estado='QUEUED', tentativas=0, ultimo_erro=NULL, "
            "proxima_tentativa_em=NULL, alterado_em=? WHERE id=? AND estado=?",
            (cu.agora_iso(), c["alvo_id"], c["estado_alvo"]))
        if cur.rowcount != 1:
            con.rollback()
            pulados += 1
            continue
        cu.registrar_evento(con, c["alvo_id"], execucao_id, "reprocessar_versao_extrator", {
            "estado_anterior": c["estado_alvo"], "conteudo_id": c["conteudo_id"],
            "extrator": c["extrator"], "versao_anterior": c["versao"],
            "versao_atual": c["versao_atual"],
            "extracao_anterior": dict(anterior) if anterior else None,
            "extraido_por_arquivo": c["arquivo_id"]})
        con.commit()                            # um item, uma transacao (REGRA 8)
        feitos += 1
    return feitos, pulados


def main() -> int:
    ap = argparse.ArgumentParser(description="Reprocessa o que foi extraido por versao antiga")
    ap.add_argument("--config", default=None)
    ap.add_argument("--aplicar", action="store_true", help="grava (sem isto, so simula)")
    ap.add_argument("--categoria", default=None, help="extrator(es): pdf,pptx,...")
    ap.add_argument("--versao", default=None, help="versao(oes) gravada(s): pdf/1.0,...")
    ap.add_argument("--erro", default=None,
                    help="trecho do erro gravado (ex.: 'adiada' para os PARTIAL acima do "
                         "teto de tamanho; 'SEM_LEITOR' para os UNSUPPORTED sem leitor)")
    ap.add_argument("--estado", default=None, help="estado(s) de quem extraiu: PARTIAL,...")
    ap.add_argument("--sem-versao", action="store_true",
                    help="inclui extracoes com marca em vez de versao ('-', "
                         "'adiado/teto_tamanho')")
    ap.add_argument("--limite", type=int, default=0, help="no maximo N itens")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    plano, fora = planejar(con, args)

    print("=" * 72)
    print(f"REPROCESSAMENTO POR VERSAO DE EXTRATOR — "
          f"{'APLICANDO' if args.aplicar else 'SIMULACAO'}")
    print("=" * 72)
    import extratores
    print("  versoes atuais: " + ", ".join(f"{k}={v}" for k, v in extratores.VERSOES.items()))
    grupos = Counter((c["extrator"], c["versao"], c["versao_atual"]) for c in plano)
    print(f"\n  voltam para a fila (1 por conteudo): {len(plano)}")
    for (extrator, de, para), n in sorted(grupos.items()):
        print(f"    {extrator:<8} {de:<12} -> {str(para):<12} {n:>6}")
    if fora:
        print("\n  ficam de fora:")
        for motivo, n in fora.most_common():
            print(f"    {motivo:<52} {n:>6}")

    if not args.aplicar:
        print("\n  SIMULACAO — nada foi gravado. Para gravar:  --aplicar")
        con.close()
        return 0

    execucao_id = cu.abrir_execucao(con, "reprocessar_extrator", "", vars(args))
    t0 = time.perf_counter()
    feitos = pulados = 0
    try:
        feitos, pulados = aplicar(con, plano, execucao_id)
    finally:
        con.rollback()
        completo = feitos + pulados == len(plano)
        cu.fechar_execucao(con, execucao_id, "concluida" if completo else "erro",
                           {"arquivos": feitos, "pulados": pulados},
                           time.perf_counter() - t0)
        con.close()
    print(f"\n  APLICADO: {feitos} item(ns) devolvidos a fila.")
    if pulados:
        print(f"  PULADOS: {pulados} item(ns) mudaram de estado depois do plano "
              f"(nada gravado para eles).")
    return 0 if completo else 1


if __name__ == "__main__":
    raise SystemExit(main())
