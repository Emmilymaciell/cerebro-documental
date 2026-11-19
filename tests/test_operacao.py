"""
test_operacao — E9: o Cerebro rodando sozinho, por dias, sem sessao de IA.

    python tests\\test_operacao.py

Nao toca no SharePoint, nao chama o Graph, nao chama IA. Arvore de mentira,
banco de mentira, tudo em pasta temporaria apagada no fim.

O que este teste PROVA, e nao apenas exercita:

  A. lock       dois workers simultaneos sao impossiveis (dois processos de
                verdade, nao dois objetos) · o lock de um processo MORTO e
                recuperado · um `pid` vivo mas alheio (PID reciclado depois
                do reboot) NAO trava a fila para sempre
  B. orcamento  piso de disco pausa em PAUSA_SEGURA_DISCO · teto de banda
                encerra a sessao limpa em LIMITE_BANDA · teto de itens em
                LIMITE_SESSAO — e o que ja estava gravado continua gravado
  C. janela     fora do horario o worker nem abre sessao
  D. queda      shutdown gracioso conclui o item atual e grava · retomada
                devolve os orfaos (HYDRATING/EXTRACTING) para a fila
  E. metricas   ritmo e ETA saem de consulta ao banco, com numeros exatos
                conferidos na mao (REGRA 7) · erros agrupados por categoria
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import types
from datetime import datetime, time as _time, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import (RAIZ, area_temporaria, checar, escrever_config,  # noqa: E402
                   placar_final, rodar)

import cerebro_util as cu  # noqa: E402
import operacao as op  # noqa: E402
import worker as worker_mod  # noqa: E402

N_ARQUIVOS = 12

# Linha minima de `arquivo` para os testes sinteticos de metrica.
SQL_ARQUIVO = (
    "INSERT INTO arquivo (caminho_rel, pasta_rel, nome, ext, categoria, "
    "prioridade, suportado, estado, presente, tamanho_bytes, tentativas, "
    "primeira_vez_em, ultima_vez_em) "
    "VALUES (?,'.',?,'.txt','texto',0,1,'{estado}',1,1000,0,?,?)"
)

# Processo auxiliar: toma o lock e SEGURA ate ser morto. Precisa ser um
# processo separado de verdade — dois objetos no mesmo processo provariam
# menos do que o problema real (duas janelas, ou a tarefa agendada subindo
# junto com um clique da Emmily).
AJUDANTE = '''import sys, time
sys.path.insert(0, sys.argv[1])
import operacao as op
trava = op.TravaProcesso(sys.argv[2], rotulo="ajudante")
print("TOMEI" if trava.adquirir() else "NAO", flush=True)
while True:
    time.sleep(0.2)
'''


# ----------------------------------------------------------------------
# Andaime
# ----------------------------------------------------------------------
def montar_arvore(raiz: Path) -> None:
    (raiz / "RD103" / "P0").mkdir(parents=True)
    (raiz / "Outros").mkdir(parents=True)
    for n in range(N_ARQUIVOS):
        pasta = "RD103\\P0" if n % 2 == 0 else "Outros"
        # Conteudo distinto em cada um: nenhum vira DUPLICATE sem querer.
        (raiz / pasta / f"doc_{n:02d}.txt").write_text(
            f"medicao {n} da obra RD-103, servico de terraplenagem {n}.\n" * 20,
            encoding="utf-8")


def variante(area: Path, base: Path, nome: str, **over) -> Path:
    """Uma config irma da de teste, mudando so o que o cenario exige."""
    cfg = yaml.safe_load(base.read_text(encoding="utf-8"))
    for chave, valor in over.items():
        if isinstance(valor, dict) and isinstance(cfg.get(chave), dict):
            cfg[chave].update(valor)
        else:
            cfg[chave] = valor
    destino = area / f"config_{nome}.yaml"
    destino.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return destino


def estados(con) -> dict:
    return dict(con.execute("SELECT estado, COUNT(*) FROM arquivo GROUP BY estado"))


def contar(con, tabela: str, onde: str = "1=1", *params) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {tabela} WHERE {onde}", params).fetchone()[0]


def args_de_teste(**extra):
    base = dict(comando="run", config=None, limite=0, prioridade_max=None,
                sem_hidratar=False, so_nuvem=False, uma_vez=True, fonte="local",
                ids=None, manter_tmp=False, max_gb_sessao=None,
                ignorar_janela=False, quieto=True)
    base.update(extra)
    return types.SimpleNamespace(**base)


# ======================================================================
# A. LOCK DE PROCESSO
# ======================================================================
def parte_lock(area: Path, config: Path, con) -> None:
    print("\n--- A1. o lock e exclusivo dentro do processo ---")
    lock = area / "solo.lock"
    a, b = op.TravaProcesso(lock), op.TravaProcesso(lock)
    checar(a.adquirir() is True, "o primeiro toma o lock")
    checar(b.adquirir() is False, "o segundo NAO toma o mesmo lock")
    checar(op.TravaProcesso.ocupada(lock) is True, "a sonda ve o lock tomado")
    dono = op.TravaProcesso.dono(lock) or {}
    checar(dono.get("pid") == os.getpid() and dono.get("livre") is False,
           "o lock diz quem e o dono, mesmo tomado", str(dono))
    a.liberar()
    checar(op.TravaProcesso.ocupada(lock) is False, "liberou: a sonda ve livre")
    checar((op.TravaProcesso.dono(lock) or {}).get("livre") is True,
           "e o arquivo registra que foi devolvido")
    checar(b.adquirir() is True, "agora o segundo consegue")
    b.liberar()

    print("\n--- A2. dois workers simultaneos sao impossiveis ---")
    cfg = cu.carregar_config(config)
    lock_worker = op.caminho_lock(cfg)
    ajudante = area / "segura_lock.py"
    ajudante.write_text(AJUDANTE, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ajudante), str(RAIZ / "scripts"), str(lock_worker)],
        stdout=subprocess.PIPE, text=True, encoding="utf-8")
    checar((proc.stdout.readline() or "").strip() == "TOMEI",
           "um processo separado tomou o lock do worker")
    extracoes_antes = contar(con, "extracao")
    r = rodar("worker.py", config, "run", "--uma-vez", "--quieto")
    checar(r.returncode == 3, "o segundo worker RECUSA subir (codigo 3)",
           f"rc={r.returncode} {r.stdout[-200:]}")
    checar("ja existe um worker rodando" in r.stdout,
           "e explica por que, com o pid do dono", r.stdout[-200:])
    checar(contar(con, "extracao") == extracoes_antes,
           "nada foi processado pelo segundo worker")

    print("\n--- A3. lock de processo MORTO e recuperado ---")
    proc.kill()
    proc.wait(timeout=30)
    # A ordem importa: o conteudo do arquivo e lido ANTES da sonda, porque a
    # sonda toma e devolve o lock e, ao devolver, reescreve o dono.
    dono = op.TravaProcesso.dono(lock_worker) or {}
    checar(dono.get("livre") is False and dono.get("pid") == proc.pid,
           "o arquivo ainda diz 'tomado' — o morto nao teve chance de devolver",
           str(dono))
    checar(op.TravaProcesso.ocupada(lock_worker) is False,
           "e mesmo assim o lock esta LIVRE: quem devolve e o nucleo, nao o "
           "conteudo do arquivo nem o pid gravado")

    print("\n--- A4. pid vivo mas alheio nao trava a fila para sempre ---")
    # O caso que o `controle.pid` sozinho nao resolve: depois de um reboot o
    # Windows recicla os numeros, e o pid de um worker morto pode pertencer a
    # outro programa. Aqui usamos o pid do PROPRIO TESTE, que esta vivo e
    # obviamente nao e um worker.
    cu.escrever_controle(con, estado="RODANDO", pid=os.getpid(), comando="RUN")
    checar(cu.processo_vivo(os.getpid()) is True,
           "o pid gravado esta vivo (era o que bloqueava antes do E9)")
    r = rodar("worker.py", config, "run", "--uma-vez", "--limite", "1", "--quieto")
    checar(r.returncode == 0, "o worker sobe assim mesmo, porque o lock estava livre",
           f"rc={r.returncode} {r.stdout[-300:]}")
    checar("RODANDO" in r.stdout and "lock estava livre" in r.stdout,
           "e avisa que achou um RODANDO orfao no banco", r.stdout[-300:])
    checar(contar(con, "extracao") == extracoes_antes + 1,
           "processou exatamente 1 item (--limite 1)")
    checar(cu.ler_controle(con)["estado"] == "LIMITE_SESSAO",
           "e encerrou com o estado do teto de itens",
           cu.ler_controle(con)["estado"])
    checar(cu.ler_controle(con)["pid"] is None, "deixou o pid limpo ao encerrar")


# ======================================================================
# B. ORCAMENTO
# ======================================================================
def parte_orcamento(area: Path, config: Path, con) -> None:
    print("\n--- B1. o orcamento, unidade por unidade ---")
    cfg = cu.carregar_config(config)
    o = op.Orcamento.de_config(cfg)
    checar(o.piso_disco_gb == 25 and o.teto_derivados_gb == 60,
           "le piso de disco e teto de derivados da config", o.descricao())
    checar(o.teto_bytes_sessao == 0 and o.teto_itens_sessao == 0,
           "sem teto de banda nem de itens por padrao (config antiga vale)")
    checar(op.Orcamento.de_config(cfg, args_de_teste(limite=7)).teto_itens_sessao == 7,
           "--limite vira teto de itens da sessao")
    checar(op.Orcamento.de_config(
        cfg, args_de_teste(max_gb_sessao=0.5)).teto_bytes_sessao == int(0.5 * 1024 ** 3),
        "--max-gb-sessao vira teto de banda da sessao")

    impossivel = op.Orcamento(piso_disco_gb=10 ** 9, teto_derivados_gb=60)
    v = impossivel.checar(con)
    checar(v.ok is False and v.estado == "PAUSA_SEGURA_DISCO",
           "piso de disco -> PAUSA_SEGURA_DISCO", f"{v.estado}/{v.motivo}")
    v = op.Orcamento(piso_disco_gb=0, teto_bytes_sessao=100).checar(con, bytes_sessao=100)
    checar(v.ok is False and v.estado == "LIMITE_BANDA",
           "teto de banda -> LIMITE_BANDA", f"{v.estado}/{v.motivo}")
    v = op.Orcamento(piso_disco_gb=0, teto_itens_sessao=3).checar(con, itens_sessao=3)
    checar(v.ok is False and v.estado == "LIMITE_SESSAO",
           "teto de itens -> LIMITE_SESSAO", f"{v.estado}/{v.motivo}")
    v = op.Orcamento(piso_disco_gb=0, teto_derivados_gb=1e-12).checar(con)
    checar(v.ok is False and "derivados" in v.motivo,
           "teto de derivados -> PAUSA_SEGURA_DISCO", f"{v.estado}/{v.motivo}")
    checar(op.Orcamento(piso_disco_gb=0).checar(con).ok is True,
           "sem teto nenhum, o orcamento libera")

    print("\n--- B2. teto de banda encerra a SESSAO, nao o trabalho ---")
    feitos_antes = contar(con, "extracao")
    cfg_banda = variante(area, config, "banda",
                         worker={"max_bytes_sessao_gb": 1e-9})   # 1 byte
    r = rodar("worker.py", cfg_banda, "run", "--uma-vez", "--quieto")
    controle = cu.ler_controle(con)
    checar(r.returncode == 0, "encerrou limpo (codigo 0)", f"rc={r.returncode}")
    checar(controle["estado"] == "LIMITE_BANDA",
           "estado LIMITE_BANDA no controle", controle["estado"])
    checar(contar(con, "extracao") == feitos_antes + 1,
           "concluiu o item que ja tinha comecado, e parou ali",
           str(contar(con, "extracao")))
    ultimo = con.execute("SELECT * FROM checkpoint ORDER BY id DESC LIMIT 1").fetchone()
    checar(ultimo["estado_worker"] == "LIMITE_BANDA" and "teto de banda" in
           (ultimo["observacao"] or ""), "o checkpoint registra o motivo",
           str(ultimo["observacao"]))
    checar(contar(con, "arquivo", "estado IN ('HYDRATING','EXTRACTING','EXTRACTED')") == 0,
           "nenhum item ficou preso num estado de meio de caminho")
    checar(contar(con, "arquivo", "estado='QUEUED'") > 0,
           "o resto da fila continua QUEUED, esperando a proxima sessao")

    print("\n--- B3. piso de disco pausa com PAUSA_SEGURA_DISCO ---")
    feitos_antes = contar(con, "extracao")
    cfg_disco = variante(area, config, "disco",
                         nuvem={"espaco_livre_minimo_gb": 10 ** 9})
    r = rodar("worker.py", cfg_disco, "run", "--uma-vez", "--quieto")
    controle = cu.ler_controle(con)
    checar(r.returncode == 0, "encerrou limpo", f"rc={r.returncode}")
    checar(controle["estado"] == "PAUSA_SEGURA_DISCO",
           "estado PAUSA_SEGURA_DISCO", controle["estado"])
    checar("abaixo do minimo" in (controle["motivo"] or ""),
           "com o motivo legivel", str(controle["motivo"]))
    checar(contar(con, "extracao") == feitos_antes,
           "e NAO processou nada — parou antes de pegar item")
    checar(controle["estado"] in op.ESTADOS_WORKER_SEGUROS,
           "PAUSA_SEGURA_DISCO conta como estado seguro para desligar")


# ======================================================================
# C. JANELA DE EXECUCAO
# ======================================================================
def parte_janela(area: Path, config: Path, con) -> None:
    print("\n--- C1. a janela, hora a hora ---")
    dia = op.JanelaExecucao(ativa=True, inicio=_time(8, 0), fim=_time(18, 0))
    base = datetime(2026, 9, 21)          # segunda-feira
    checar(dia.dentro(base.replace(hour=9)) is True, "09h esta dentro de 08–18")
    checar(dia.dentro(base.replace(hour=7)) is False, "07h esta fora")
    checar(dia.dentro(base.replace(hour=18)) is False, "18h em ponto ja e fora (fim exclusivo)")

    noite = op.JanelaExecucao(ativa=True, inicio=_time(19, 0), fim=_time(7, 0))
    checar(noite.dentro(base.replace(hour=20)) is True, "20h esta dentro de 19–07")
    checar(noite.dentro(base.replace(hour=2)) is True,
           "02h tambem: a janela atravessa a meia-noite")
    checar(noite.dentro(base.replace(hour=12)) is False, "12h esta fora")

    uteis = op.JanelaExecucao(ativa=True, inicio=_time(19, 0), fim=_time(7, 0),
                              dias=(0, 1, 2, 3, 4))
    sexta, sabado = datetime(2026, 9, 25), datetime(2026, 9, 26)
    checar(uteis.dentro(sexta.replace(hour=22)) is True, "sexta 22h: dentro")
    checar(uteis.dentro(sabado.replace(hour=2)) is True,
           "sabado 02h ainda e a janela que ABRIU na sexta")
    checar(uteis.dentro(sabado.replace(hour=22)) is False,
           "sabado 22h: fora, porque sabado nao esta na lista de dias")
    checar(op.JanelaExecucao().dentro(base.replace(hour=3)) is True,
           "janela desligada: sempre dentro")

    proxima = dia.proxima_abertura(base.replace(hour=5))
    checar(proxima is not None and proxima.hour == 8 and proxima.day == 21,
           "proxima_abertura acha as 08h do mesmo dia", str(proxima))
    proxima = dia.proxima_abertura(base.replace(hour=19))
    checar(proxima is not None and proxima.hour == 8 and proxima.day == 22,
           "depois das 18h, a proxima abertura e no dia seguinte", str(proxima))

    print("\n--- C2. fora da janela o worker nem abre sessao ---")
    agora = datetime.now()
    inicio = (agora + timedelta(hours=2)).strftime("%H:%M")
    fim = (agora + timedelta(hours=3)).strftime("%H:%M")
    cfg_janela = variante(area, config, "janela",
                          worker={"janela": {"ativa": True, "inicio": inicio,
                                             "fim": fim, "dias": []}})
    execucoes_antes = contar(con, "execucao")
    feitos_antes = contar(con, "extracao")
    r = rodar("worker.py", cfg_janela, "run", "--uma-vez", "--quieto")
    controle = cu.ler_controle(con)
    checar(r.returncode == 0, "sai com codigo 0 — nao e erro, e horario",
           f"rc={r.returncode} {r.stdout[-200:]}")
    checar(controle["estado"] == "FORA_DA_JANELA", "estado FORA_DA_JANELA",
           controle["estado"])
    checar("volta em" in (controle["motivo"] or ""),
           "o motivo diz quando a janela reabre", str(controle["motivo"]))
    checar(contar(con, "execucao") == execucoes_antes,
           "nao poluiu o historico com uma sessao de zero item")
    checar(contar(con, "extracao") == feitos_antes, "e nao processou nada")

    print("\n--- C3. --ignorar-janela passa por cima ---")
    r = rodar("worker.py", cfg_janela, "run", "--uma-vez", "--limite", "1",
              "--ignorar-janela", "--quieto")
    checar(r.returncode == 0 and contar(con, "extracao") == feitos_antes + 1,
           "com --ignorar-janela, processa mesmo fora do horario",
           f"rc={r.returncode}")


# ======================================================================
# D. QUEDA E RETOMADA
# ======================================================================
def parte_queda(area: Path, config: Path, con) -> None:
    print("\n--- D1. shutdown gracioso conclui o item atual e grava ---")
    cfg = cu.carregar_config(config)
    cu.escrever_controle(con, comando="RUN", estado="PARADO", motivo="")
    args = args_de_teste(uma_vez=True)
    execucao_id = cu.abrir_execucao(con, "worker", "", {"teste": "gracioso"})
    log = cu.Log(cfg["_logs_path"], "operacao", con, execucao_id, verboso=False)
    w = worker_mod.Worker(cfg, con, log, execucao_id, args)

    # A pausa chega DEPOIS do primeiro item entrar em processamento — e esse
    # o caso que interessa: nao "parou antes de comecar", e sim "estava no
    # meio, terminou o item e so entao parou".
    original = w.processar
    vistos: list = []

    def espiao(item):
        desfecho = original(item)
        vistos.append((int(item["id"]), desfecho))
        cu.escrever_controle(con, comando="PAUSE")
        return desfecho

    w.processar = espiao
    fila_antes = contar(con, "v_fila")
    estado = w.rodar()
    log.fechar()

    checar(estado == "PAUSADO", "o worker encerrou em PAUSADO", estado)
    checar(len(vistos) == 1, f"exatamente 1 item foi processado (foram {len(vistos)})")
    alvo = vistos[0][0]
    linha = con.execute("SELECT estado FROM arquivo WHERE id=?", (alvo,)).fetchone()
    checar(linha["estado"] == "COMPLETED",
           "o item que estava em curso foi CONCLUIDO, nao abandonado", linha["estado"])
    extra = con.execute("SELECT texto_path, perfil_path FROM extracao WHERE arquivo_id=?",
                        (alvo,)).fetchone()
    checar(extra is not None and Path(extra["texto_path"]).exists()
           and Path(extra["perfil_path"]).exists(),
           "os derivados dele estao no disco — a transacao fechou (REGRA 8)")
    checar(contar(con, "texto_fts", "arquivo_id=?", alvo) > 0, "e o indice recebeu o texto")
    checar(contar(con, "v_fila") == fila_antes - 1,
           "a fila diminuiu exatamente 1 — nada a mais foi tocado")
    ultimo = con.execute("SELECT * FROM checkpoint ORDER BY id DESC LIMIT 1").fetchone()
    checar(ultimo["estado_worker"] == "PAUSADO", "checkpoint final gravado",
           str(ultimo["estado_worker"]))
    checar(cu.ler_controle(con)["pid"] is None,
           "o controle nao ficou com pid de processo que nao existe mais")

    print("\n--- D2. a pausa nao se desfaz sozinha ---")
    r = rodar("worker.py", config, "run", "--uma-vez", "--quieto")
    checar(r.returncode == 4, "worker recusa subir com PAUSE pendente (codigo 4)",
           f"rc={r.returncode}")
    r = rodar("control.py", config, "resume")
    checar(r.returncode == 0 and cu.ler_controle(con)["comando"] == "RUN",
           "resume libera a fila")

    print("\n--- D3. retomada apos queda devolve os orfaos ---")
    orfaos = [linha[0] for linha in con.execute(
        "SELECT id FROM arquivo WHERE estado='QUEUED' ORDER BY id LIMIT 2")]
    checar(len(orfaos) == 2, "ha 2 itens para simular a queda")
    con.execute("UPDATE arquivo SET estado='EXTRACTING' WHERE id=?", (orfaos[0],))
    con.execute("UPDATE arquivo SET estado='HYDRATING' WHERE id=?", (orfaos[1],))
    con.commit()
    checar(contar(con, "v_fila", "id IN (?,?)", *orfaos) == 0,
           "presos assim, eles estao FORA da fila")
    antes = contar(con, "evento_arquivo", "evento='retomado_apos_queda'")
    r = rodar("worker.py", config, "run", "--uma-vez", "--quieto")
    checar(r.returncode == 0, "o worker sobe normalmente depois da queda",
           f"rc={r.returncode} {r.stdout[-300:]}")
    checar(contar(con, "evento_arquivo", "evento='retomado_apos_queda'") == antes + 2,
           "os 2 orfaos voltaram para a fila, com evento no historico")
    finais = [con.execute("SELECT estado FROM arquivo WHERE id=?", (i,)).fetchone()[0]
              for i in orfaos]
    checar(all(e == "COMPLETED" for e in finais),
           "e foram ate o fim nesta sessao", str(finais))
    checar(cu.ler_controle(con)["estado"] == "FILA_VAZIA",
           "a fila terminou", cu.ler_controle(con)["estado"])
    checar(contar(con, "arquivo", "estado IN ('HYDRATING','EXTRACTING','EXTRACTED')") == 0,
           "nenhum estado de meio de caminho sobrou no banco")


# ======================================================================
# E. METRICAS, ETA E RELATORIOS
# ======================================================================
def parte_metricas(area: Path, config: Path, con) -> None:
    print("\n--- E1. as metricas saem do banco, nao do ar ---")
    m = op.medir(con, fonte="local")
    checar(m["vista_fila"] == "v_fila", "fonte local mede pela v_fila", m["vista_fila"])
    checar(m["fila"] == contar(con, "v_fila"),
           "a fila e a contagem da view, nao uma estimativa")
    checar(m["concluidos"] == sum(m["estados"].get(e, 0) for e in
                                  ("COMPLETED", "DUPLICATE", "PARTIAL", "OCR_REQUIRED")),
           "o 'ja coberto' e a soma dos estados terminais uteis")
    checar(m["itens_min"] > 0 and m["origem_ritmo"],
           f"mediu um ritmo e diz de onde veio: {m['origem_ritmo']}")
    checar(m["worker_estado"] == cu.ler_controle(con)["estado"],
           "o estado do worker vem da tabela controle")
    checar(m["derivados_gb"] == round(op.Orcamento.derivados_gb(con), 2),
           "derivados em GB conferem com a soma de bytes_texto")

    print("\n--- E2. ETA calculado a partir do banco, conferido na mao ---")
    # Banco limpo, numeros escolhidos: 11 eventos de 2 em 2 segundos = 10
    # intervalos em 20 s = 30 itens/min. Com 60 na fila, o ETA tem de dar
    # exatamente 2,0 minutos. Se der outra coisa, a conta esta errada.
    area2 = area / "eta"
    area2.mkdir()
    raiz2 = area2 / "vazio"
    raiz2.mkdir()
    cfg2 = escrever_config(area2, raiz2)
    r = rodar("00_init_db.py", cfg2)
    if r.returncode != 0:
        checar(False, "banco do teste de ETA criado", r.stderr[-400:])
        return
    con2 = cu.conectar(area2 / "teste.db")
    execucao = cu.abrir_execucao(con2, "worker", "", {"teste": "eta"})
    inicio = datetime(2026, 9, 21, 20, 0, 0)
    for n in range(11):
        con2.execute(SQL_ARQUIVO.format(estado="COMPLETED"),
                     (f"f{n}.txt", f"f{n}.txt", cu.agora_iso(), cu.agora_iso()))
        con2.execute(
            "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, em) "
            "VALUES (?,?,?,?)",
            (con2.execute("SELECT MAX(id) FROM arquivo").fetchone()[0], execucao,
             "processado", (inicio + timedelta(seconds=2 * n)).isoformat() + "+00:00"))
    for n in range(60):
        con2.execute(SQL_ARQUIVO.format(estado="QUEUED"),
                     (f"fila{n}.txt", f"fila{n}.txt", cu.agora_iso(), cu.agora_iso()))
    con2.commit()

    m2 = op.medir(con2, fonte="local")
    checar(m2["fila"] == 60, f"fila de 60 itens lida da view (leu {m2['fila']})")
    checar(m2["segundos_recente"] == 20.0,
           f"20 s de trabalho medidos (mediu {m2['segundos_recente']})")
    checar(m2["itens_recente"] == 10.0,
           f"10 itens no periodo (11 carimbos = 10 intervalos) — deu {m2['itens_recente']}")
    checar(m2["itens_min_recente"] == 30.0,
           f"30,0 itens/min (10 itens / 20 s) — deu {m2['itens_min_recente']}")
    checar(m2["eta_min"] == 2.0, f"ETA = 60/30 = 2,0 min — deu {m2['eta_min']}")
    checar(m2["termino_previsto"] is not None, "e vira uma data de termino")

    print("\n--- E3. noite de worker desligado nao vira lentidao ---")
    # Um evento 8 horas depois: se o calculo somasse o buraco, o ritmo
    # desabaria de 30 para ~0,02 itens/min e o ETA iria para meses.
    con2.execute(
        "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, em) "
        "VALUES (1,?,?,?)",
        (execucao, "processado", (inicio + timedelta(hours=8)).isoformat() + "+00:00"))
    con2.commit()
    m3 = op.medir(con2, fonte="local")
    checar(m3["segundos_recente"] == 20.0,
           f"as 8 h de ociosidade foram descartadas (mediu {m3['segundos_recente']} s)")
    checar(m3["itens_min_recente"] == 30.0,
           f"o ritmo continua 30,0 itens/min (deu {m3['itens_min_recente']})")

    print("\n--- E4. sessao de outra execucao nao se mistura ---")
    outra = cu.abrir_execucao(con2, "worker", "", {"teste": "outra"})
    con2.execute(
        "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, em) "
        "VALUES (1,?,?,?)",
        (outra, "processado", (inicio + timedelta(seconds=30)).isoformat() + "+00:00"))
    con2.commit()
    m4 = op.medir(con2, fonte="local")
    checar(m4["segundos_recente"] == 20.0,
           "o intervalo ENTRE duas sessoes nao entra na conta "
           f"(mediu {m4['segundos_recente']} s)")

    print("\n--- E5. sem medicao nenhuma, o ETA e 'nao sei' ---")
    con2.execute("DELETE FROM evento_arquivo")
    con2.commit()
    m5 = op.medir(con2, fonte="local")
    checar(m5["itens_min"] == 0 and m5["eta_min"] is None,
           "sem dado, nao inventa numero", f"{m5['itens_min']}/{m5['eta_min']}")
    checar("sem medicao" in m5["origem_ritmo"], "e diz isso na cara",
           m5["origem_ritmo"])
    checar(op.formatar_duracao(None) == "sem medicao",
           "a formatacao tambem nao inventa")
    con2.close()

    print("\n--- E6. erros agrupados por categoria ---")
    casos = [
        ("404 itemNotFound: The resource could not be found.", "nuvem: item nao existe"),
        ("503 serviceNotAvailable: queda", "nuvem: indisponivel"),
        ("quickXorHash divergente: calculei X, o Graph anunciou Y",
         "quickXorHash divergente"),
        ("tamanho divergente: baixou 10, o Graph anunciou 20", "tamanho divergente"),
        ("TimeoutError: hidratacao passou de 900s", "rede: tempo esgotado"),
        ("arquivo acima de 300 MB: extracao adiada", "acima do teto de tamanho"),
        ("", "sem mensagem"),
    ]
    for mensagem, esperado in casos:
        obtido = op.categoria_erro(mensagem)
        checar(obtido == esperado, f"'{(mensagem or '(vazio)')[:34]}' -> {esperado}",
               f"deu {obtido}")

    con.execute("UPDATE arquivo SET estado='ERROR_FINAL', "
                "ultimo_erro='404 itemNotFound: sumiu' WHERE id IN "
                "(SELECT id FROM arquivo ORDER BY id LIMIT 2)")
    con.execute("UPDATE arquivo SET estado='ERROR_RETRYABLE', "
                "ultimo_erro='503 serviceNotAvailable: queda' WHERE id IN "
                "(SELECT id FROM arquivo ORDER BY id DESC LIMIT 1)")
    con.commit()
    rel = op.relatorio_erros(con)
    checar(rel["total"] == 3, f"3 arquivos em erro (achou {rel['total']})")
    categorias = {g["categoria"]: g for g in rel["grupos"]}
    checar(categorias["nuvem: item nao existe"]["total"] == 2
           and categorias["nuvem: item nao existe"]["ERROR_FINAL"] == 2,
           "os dois 404 viraram UM grupo, contados como ERROR_FINAL")
    checar(categorias["nuvem: indisponivel"]["ERROR_RETRYABLE"] == 1,
           "o 503 e outro grupo, em ERROR_RETRYABLE")
    checar(rel["grupos"][0]["total"] >= rel["grupos"][-1]["total"],
           "os grupos saem do maior para o menor")
    checar(categorias["nuvem: item nao existe"]["exemplos"],
           "cada grupo traz exemplo com id e caminho, para ir olhar")
    texto = op.texto_relatorio_erros(rel)
    checar("ERROS POR CATEGORIA" in texto and "nuvem: item nao existe" in texto,
           "o relatorio de erros vira texto legivel")

    print("\n--- E7. relatorio de sessao ---")
    rel = op.relatorio_sessao(con)
    checar(rel["vazio"] is False and rel["execucao_id"] is not None,
           "acha a ultima sessao do worker sozinho")
    checar(rel["duracao_s"] >= 0 and rel["checkpoints"] >= 1,
           "traz duracao e checkpoints da sessao", str(rel["checkpoints"]))
    texto = op.texto_relatorio_sessao(rel)
    checar("SESSAO #" in texto and "ritmo medido" in texto,
           "e vira texto legivel, com o ritmo medido")

    print("\n--- E8. o comando unico responde ---")
    r = rodar("cerebro.py", config, "status")
    checar(r.returncode == 0 and "CEREBRO" in r.stdout and "lock" in r.stdout,
           "cerebro.py status", r.stdout[-300:] + r.stderr[-300:])
    r = rodar("cerebro.py", config, "relatorio")
    checar(r.returncode == 0 and "SESSAO #" in r.stdout and "ERROS" in r.stdout,
           "cerebro.py relatorio", r.stdout[-300:] + r.stderr[-300:])
    r = rodar("cerebro.py", config, "pausar")
    checar(r.returncode == 0 and "SEGURO" in r.stdout, "cerebro.py pausar",
           r.stdout[-300:])
    r = rodar("cerebro.py", config, "processar", "--uma-vez", "--quieto")
    checar(r.returncode == 4, "com pausa pendente, cerebro.py processar recusa (4)",
           f"rc={r.returncode}")
    r = rodar("cerebro.py", config, "retomar")
    checar(r.returncode == 0 and cu.ler_controle(con)["comando"] == "RUN",
           "cerebro.py retomar")
    r = rodar("cerebro.py", config, "processar", "--concorrencia", "2", "--quieto")
    checar(r.returncode == 2 and "concorrencia > 1" in r.stdout,
           "concorrencia > 1 e recusada, com o motivo escrito", r.stdout[-200:])


# ======================================================================
def parte_cobertura(area: Path, config: Path, con) -> None:
    print("\n--- cobertura: cada estado numa coluna, UNSUPPORTED fora do % ---")
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE arquivo (categoria TEXT, prioridade INTEGER, "
                "estado TEXT, presente INTEGER)")
    linhas = ([("pdf", 0, "COMPLETED", 1)] * 2 + [("pdf", 0, "DUPLICATE", 1),
              ("pdf", 1, "PARTIAL", 1), ("pdf", 1, "QUEUED", 1), ("pdf", 1, "ERROR_FINAL", 1),
              ("pdf", 0, "MISSING", 0)] + [("imagem", 2, "UNSUPPORTED", 1)] * 3)
    mem.executemany("INSERT INTO arquivo VALUES (?,?,?,?)", linhas)
    cob = {l["chave"]: l for l in op.cobertura(mem)}
    pdf, img, tot = cob["pdf"], cob["imagem"], cob["TOTAL"]
    checar(pdf["total"] == 6, "MISSING (presente=0) nao entra", str(pdf["total"]))
    checar((pdf["extraido"], pdf["parcial"], pdf["fila"], pdf["erro"]) == (3, 1, 1, 1),
           "pdf: extraido 3 (COMPLETED+DUPLICATE), parcial 1, fila 1, erro 1",
           str({k: pdf[k] for k in ("extraido", "parcial", "fila", "erro")}))
    checar(abs(pdf["pct"] - 100 * 4 / 6) < 1e-9, "pct = (extraido+parcial+ocr)/alvo",
           str(pdf["pct"]))
    checar(img["sem_extrator"] == 3 and img["pct"] is None,
           "so UNSUPPORTED: fora do denominador, pct indefinido (nao 0%)", str(img["pct"]))
    checar(tot["total"] == 9 and abs(tot["pct"] - 100 * 4 / 6) < 1e-9,
           "TOTAL soma as linhas e exclui UNSUPPORTED do %", f"{tot['total']}/{tot['pct']}")
    por_p = {l["chave"]: l for l in op.cobertura(mem, "prioridade")}
    checar(por_p[1]["total"] == 3 and por_p[2]["sem_extrator"] == 3,
           "cobertura por prioridade", str({k: v["total"] for k, v in por_p.items()}))
    mem.execute("INSERT INTO arquivo VALUES ('pdf', 0, 'ESTADO_INVENTADO', 1)")
    try:
        op.cobertura(mem)
        checar(False, "estado sem grupo levanta erro (nao some do relatorio)")
    except ValueError:
        checar(True, "estado sem grupo levanta erro (nao some do relatorio)")
    mem.close()
    checar(all(e in {x for g in op.GRUPOS_COBERTURA.values() for x in g}
               for e in cu.ESTADOS),
           "todo estado de cu.ESTADOS tem grupo de cobertura")

    print("\n--- cerebro.py: erros / cobertura / sync ---")
    antes = contar(con, "arquivo")
    r = rodar("cerebro.py", config, "cobertura")
    checar(r.returncode == 0 and "COBERTURA POR CATEGORIA" in r.stdout,
           "cerebro cobertura sai 0", r.stderr[-400:])
    r = rodar("cerebro.py", config, "cobertura", "--por", "prioridade")
    checar(r.returncode == 0 and "COBERTURA POR PRIORIDADE" in r.stdout,
           "cerebro cobertura --por prioridade sai 0", r.stderr[-400:])
    r = rodar("cerebro.py", config, "erros")
    checar(r.returncode == 0, "cerebro erros sai 0", r.stderr[-400:])
    r = rodar("cerebro.py", config, "sync", "--sem-delta", "--quieto")
    checar(r.returncode == 0, "cerebro sync (simulacao, sem Graph) sai 0",
           (r.stdout + r.stderr)[-600:])
    checar(contar(con, "arquivo") == antes, "sync sem --aplicar nao mexe no catalogo")


def main() -> int:
    area = area_temporaria("cerebro_operacao_")
    raiz_falsa = area / "acervo_falso"
    raiz_falsa.mkdir()
    montar_arvore(raiz_falsa)
    config = escrever_config(area, raiz_falsa)

    print("\n--- preparo ---")
    for script, extra in (("00_init_db.py", ()), ("01_scan_inventario.py", ("--quieto",)),
                          ("02_classificar.py", ())):
        r = rodar(script, config, *extra)
        if r.returncode != 0:
            print(r.stdout[-800:], r.stderr[-800:])
            checar(False, f"{script} rodou")
            return placar_final()
    con = cu.conectar(area / "teste.db")
    checar(contar(con, "arquivo") == N_ARQUIVOS,
           f"{N_ARQUIVOS} arquivos inventariados (achou {contar(con, 'arquivo')})")
    checar(contar(con, "v_fila") == N_ARQUIVOS, "todos na fila")

    parte_lock(area, config, con)
    parte_orcamento(area, config, con)
    parte_janela(area, config, con)
    parte_queda(area, config, con)
    parte_metricas(area, config, con)
    parte_cobertura(area, config, con)

    print("\n--- fecho: nada preso, nada perdido ---")
    checar(contar(con, "arquivo") == N_ARQUIVOS,
           "nenhuma linha sumiu do catalogo (REGRA 6)")
    checar(op.TravaProcesso.ocupada(op.caminho_lock(cu.carregar_config(config))) is False,
           "o lock ficou livre ao final de tudo")

    con.close()
    shutil.rmtree(area, ignore_errors=True)
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
