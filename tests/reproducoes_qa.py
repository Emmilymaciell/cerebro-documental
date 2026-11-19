"""
reproducoes_qa — defeitos de PRODUCAO achados pela auditoria de vacuidade
(sprint de convergencia, Agente C), em reproducao minima.

    python tests\\reproducoes_qa.py

NAO e suite e NAO entra na contagem do CLAUDE.md. Cada reproducao imprime
REPRODUZIDO (o defeito existe no codigo atual) ou CORRIGIDO. Saida 1 se
algum defeito ainda reproduz, 0 se todos foram corrigidos — quando chegar a
0, a checagem correspondente pode migrar para a suite de origem (indicada
em cada caso) e este arquivo encolhe.

Tudo roda em banco temporario (config de `comum.escrever_config`). Nada de
rede, nada no banco de producao, nada no SharePoint.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import area_temporaria, escrever_config, rodar  # noqa: E402

import cerebro_util as cu  # noqa: E402

RESULTADOS: dict[str, bool] = {}


def relatar(codigo: str, reproduziu: bool, detalhe: str) -> None:
    RESULTADOS[codigo] = reproduziu
    print(f"  {'REPRODUZIDO' if reproduziu else 'CORRIGIDO  '}  {codigo}  {detalhe}")


def r1_origem_muda_na_segunda_passada() -> None:
    """R1 — `13_reconciliar --aplicar` nao e idempotente em `arquivo.origem`.

    A 1a passada cria as linhas que so existem na nuvem com origem='graph'.
    A 2a passada as encontra por caminho exato e `vincular()` grava
    origem='ambos' — como se tivessem existido no disco local. Nenhuma
    logica le `origem` hoje (so informativo), por isso a gravidade e baixa;
    mas a 2a passada contradiz a 1a e a checagem "as 4 linhas novas vieram
    da nuvem" deixa de valer depois de qualquer re-execucao.
    Destino quando corrigido: `foto_catalogo()` de test_reconciliacao.py
    volta a incluir `origem`.
    """
    import test_reconciliacao as tr

    area = area_temporaria("cerebro_repro_r1_")
    raiz = area / "acervo_falso"
    raiz.mkdir()
    config = escrever_config(area, raiz)
    rodar("00_init_db.py", config)
    tr.montar(area, config)
    rodar("13_reconciliar.py", config, "--aplicar")
    con = sqlite3.connect(area / "teste.db")
    primeira = con.execute("SELECT origem FROM arquivo WHERE item_id='i9'").fetchone()[0]
    con.close()
    rodar("13_reconciliar.py", config, "--aplicar")
    con = sqlite3.connect(area / "teste.db")
    segunda = con.execute("SELECT origem FROM arquivo WHERE item_id='i9'").fetchone()[0]
    con.close()
    shutil.rmtree(area, ignore_errors=True)
    relatar("R1", primeira != segunda,
            f"origem do item so-na-nuvem: 1a passada={primeira!r}, 2a passada={segunda!r}")


def r2_v_fila_sem_item_id_no_banco_recriado() -> None:
    """R2 — banco recriado pelo repositorio tem `v_fila` SEM drive_id/item_id.

    `00_init_db.py` reaproveita `COLUNAS_NUVEM` e `INDICES` do
    `03_migrar_v3.py`, mas nao o bloco `VIEWS`: a `v_fila` fica a do
    `schema_v2.sql`. Em producao ela expoe `drive_id, item_id`. O
    `15_marcar_ausentes.py --aplicar` consulta `v_fila WHERE item_id IS NULL`
    DEPOIS de gravar os MISSING: num banco recriado ele marca, faz commit e
    morre com OperationalError, sem o relatorio de preservacao.
    (CR-1, "consequencia colateral"; o schema e do Agente B.)
    """
    area = area_temporaria("cerebro_repro_r2_")
    raiz = area / "acervo_falso"
    raiz.mkdir()
    config = escrever_config(area, raiz)
    rodar("00_init_db.py", config)
    con = cu.conectar(area / "teste.db")
    agora = cu.agora_iso()
    con.execute("INSERT INTO arquivo (id, caminho_rel, pasta_rel, nome, estado, presente, "
                "primeira_vez_em, ultima_vez_em) VALUES (1,'A\\x.pdf','A','x.pdf','QUEUED',1,?,?)",
                (agora, agora))
    execucao = cu.abrir_execucao(con, "reconciliacao", "", {})
    con.execute("INSERT INTO reconciliacao (execucao_id, arquivo_id, metodo, confianca, "
                "aplicado, em) VALUES (?,1,'nenhum','SEM_CORRESPONDENCIA',0,?)",
                (execucao, agora))
    con.commit()
    colunas = [c[1] for c in con.execute("PRAGMA table_info(v_fila)")]
    con.close()
    r = rodar("15_marcar_ausentes.py", config, "--aplicar", "--quieto")
    con = sqlite3.connect(area / "teste.db")
    estado = con.execute("SELECT estado FROM arquivo WHERE id=1").fetchone()[0]
    con.close()
    shutil.rmtree(area, ignore_errors=True)
    quebrou = r.returncode != 0 and "item_id" in r.stderr
    relatar("R2", "item_id" not in colunas or quebrou,
            f"v_fila={colunas}; 15_marcar_ausentes rc={r.returncode}, "
            f"linha ficou {estado} ({'morreu depois do commit' if quebrou else 'ok'})")


def main() -> int:
    print("\n--- reproducoes de defeitos de producao (fora da contagem) ---")
    r1_origem_muda_na_segunda_passada()
    r2_v_fila_sem_item_id_no_banco_recriado()
    abertos = [c for c, v in RESULTADOS.items() if v]
    print(f"\n  {len(abertos)} defeito(s) ainda reproduz(em): {', '.join(abertos) or '-'}")
    return 1 if abertos else 0


if __name__ == "__main__":
    raise SystemExit(main())
