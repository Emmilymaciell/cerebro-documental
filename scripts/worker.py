"""
worker — o processador autonomo. Roda sozinho, por horas ou dias.

    python scripts\\worker.py run          # processa ate acabar a fila
    python scripts\\worker.py run --limite 20
    python scripts\\worker.py run --prioridade-max 1   # so P0 e P1
    python scripts\\worker.py run --sem-hidratar       # so o que ja esta local

Nao precisa de sessao de IA aberta. Quem manda parar e o
`scripts\\control.py` (pause / resume / stop), pela tabela `controle`.

Ciclo de cada item — e uma transacao, e a transacao e o checkpoint:

    QUEUED -> [hidratar se estiver so na nuvem] -> READY -> EXTRACTING
           -> grava texto.gz + perfil.json + trechos no indice
           -> COMPLETED | PARTIAL | OCR_REQUIRED | DUPLICATE
           -> [libera espaco: devolve para a nuvem]  -> proximo

Garantias:
  - o original no SharePoint nunca e alterado, movido ou apagado;
  - um arquivo ruim vira ERROR_* e o worker segue;
  - falha de nuvem/rede e transitoria: volta para a fila com backoff;
  - queda de energia: quem estava EXTRACTING volta para a fila na subida —
    mas o item que ja derrubou o worker `max_quedas_por_item` vezes (padrao
    2) vai para ERROR_FINAL, para nao travar a fila (B3);
  - credencial recusada (401/403) nao gasta tentativa do documento; N
    seguidas encerram a sessao em AGUARDANDO_CREDENCIAL, codigo 6 (B4).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import busca  # noqa: E402
import cerebro_util as cu  # noqa: E402
import extratores  # noqa: E402
import fonte  # noqa: E402
import operacao as op  # noqa: E402

PARAR = {"pedido": False}

# ----------------------------------------------------------------------
# T-10 / A-3 — item acima do teto de extracao NAO e baixado.
#
# Ele termina em `PARTIAL` (estado que ja existe: nenhum estado novo), mas
# com uma extracao que se distingue de qualquer outra pelo `versao`. E por
# esta marca que, no dia em que existir extrator de arquivo grande, se
# acham os itens para retomar — consulta documentada em
# docs/05_OPERACAO.md ("Itens adiados pelo teto de extracao"):
#
#     SELECT a.id, a.caminho_rel, a.tamanho_bytes, a.categoria
#       FROM arquivo a JOIN extracao e ON e.arquivo_id = a.id
#      WHERE a.estado = 'PARTIAL' AND e.versao = 'adiado/teto_tamanho';
# ----------------------------------------------------------------------
VERSAO_ADIADO_TETO = op.VERSAO_ADIADO_TETO          # "adiado/teto_tamanho"
PREFIXO_ERRO_ADIADO_TETO = "ADIADO_TETO_TAMANHO"
# L7 — formato sem leitor (extratores.motivo_sem_leitor) NAO e baixado.
PREFIXO_ERRO_SEM_LEITOR = "SEM_LEITOR"
# B4 — codigo de saida de `executar()` quando a sessao encerra em
# AGUARDANDO_CREDENCIAL (docs/10_OPERACAO_AUTONOMA.md, "Codigos de saida").
CODIGO_AGUARDANDO_CREDENCIAL = 6


def resultado_adiado_por_tamanho(item, teto_mb: float) -> dict:
    """O 'resultado de extracao' de um item que nem foi baixado.

    Mesmo formato do que `extratores.extrair()` devolve, para seguir pelo
    MESMO caminho de gravacao (extracao, perfil, conteudo, estado, evento)
    numa transacao so (REGRA 8).
    """
    tamanho_mb = int(item["tamanho_bytes"] or 0) / 1024 ** 2
    return {"status": "parcial", "extrator": item["categoria"],
            "versao": VERSAO_ADIADO_TETO, "texto": "", "paginas_texto": [],
            "erro": (f"{PREFIXO_ERRO_ADIADO_TETO}: arquivo de {tamanho_mb:.0f} MB acima "
                     f"do teto de {teto_mb:.0f} MB; extracao adiada, nada foi baixado"),
            "paginas": None, "paginas_com_texto": None, "slides": None,
            "abas": None, "imagens": None, "caracteres": 0, "palavras": 0,
            "densidade_texto": None}


def _tratar_sinal(signum, frame):
    PARAR["pedido"] = True
    print("\n[aviso] pedido de parada recebido — concluindo o item atual...", flush=True)


class Worker:
    def __init__(self, cfg: dict, con, log: cu.Log, execucao_id: int, args,
                 fonte_pronta=None):
        self.cfg = cfg
        self.con = con
        self.log = log
        self.execucao_id = execucao_id
        self.args = args
        self.raiz = cfg["_raiz_path"]
        self.w = cfg.get("worker") or {}
        self.nuvem = cfg.get("nuvem") or {}
        self.limites = cfg.get("limites") or {}
        self.max_tentativas = int(self.w.get("max_tentativas", 4))
        self.checkpoint_a_cada = int(self.w.get("checkpoint_a_cada", 50))
        self.permitir_hidratar = (bool(self.nuvem.get("permitir_hidratacao", False))
                                  and not args.sem_hidratar)
        self.contadores = {"arquivos": 0, "novos": 0, "alterados": 0, "sumidos": 0,
                           "erros": 0, "bytes": 0, "bytes_hidratados": 0,
                           "concluidos": 0, "duplicatas": 0, "ocr": 0, "parciais": 0,
                           "bytes_baixados": 0, "bytes_economizados": 0,
                           "validacoes_falhas": 0, "reeleicoes": 0, "ausentes": 0,
                           "adiados_tamanho": 0, "interrompidos": 0}
        self.falhas_seguidas = 0
        # B3 — pilula envenenada: quantas vezes um MESMO item pode ser achado
        # orfao (o processo morreu com ele no meio) antes de ser isolado.
        self.max_quedas_por_item = max(1, int(self.w.get("max_quedas_por_item", 2)))
        # B4 — disjuntor de credencial (ver `_falha_credencial`).
        self.falhas_credencial_para_parar = max(
            1, int(self.w.get("falhas_credencial_para_parar", 3)))
        self.falhas_credencial_seguidas = 0
        self.credencial_comprovada = False    # o Graph ja entregou algo nesta sessao
        self.ultima_falha_credencial = ""
        self.t0 = time.perf_counter()
        self.ultimo_heartbeat = 0.0
        # As travas de operacao autonoma (scripts/operacao.py). Todas saem de
        # `cfg.get(...)` com padrao, entao um `projeto.yaml` antigo continua
        # valendo: orcamento com os mesmos limites de antes e janela desligada.
        self.orcamento = op.Orcamento.de_config(cfg, args)
        self.janela = op.JanelaExecucao.de_config(cfg)
        if getattr(args, "ignorar_janela", False):
            self.janela.ativa = False
        # De onde vem o conteudo. 'graph' e o padrao; 'local' e so de teste.
        # `fonte_pronta` existe para o teste automatizado injetar um Graph
        # falso: em producao ninguem passa esse argumento.
        self.fonte = fonte_pronta or fonte.criar(
            getattr(args, "fonte", None) or cfg.get("fonte", "graph"), cfg)
        if self.fonte.nome != "graph":
            self.log.aviso(f"FONTE '{self.fonte.nome}' — modo de teste. "
                           f"Em producao a fonte e a nuvem (DEC-012).")
        # M-14: o download pergunta, nas esperas e entre pedacos, se pediram
        # para parar. Sem isto a pausa graciosa esperava o fim de uma
        # tempestade de 429 (ate ~6 min) e o `control.py pause` desistia antes.
        # Busca v5 (indice por conteudo, docs/09_BUSCA.md): ativa so depois
        # de `05_migrar_v5.py --aplicar`. Antes disso, o indice v4
        # (`texto_fts`, por arquivo) continua sendo alimentado.
        self.busca_v5 = busca.v5_ativo(con)
        try:
            # R9: a mesma pergunta, feita ~1x/s pelo download, tambem bate o
            # heartbeat (no maximo a cada `heartbeat_s`).
            self.fonte.deve_parar = self._durante_download
        except AttributeError:
            pass

    def pedido_de_parada(self) -> bool:
        """Ctrl+C no terminal, ou PAUSE/STOP gravado pelo control.py."""
        return PARAR["pedido"] or self.comando_atual() in ("PAUSE", "STOP")

    def _durante_download(self) -> bool:
        """Chamada pelo `graph_ro.baixar` (via `FonteGraph.deve_parar`) no maximo
        1x/s durante a leitura e a cada 0,5 s nas esperas de 429.

        R9: sem isto, um download de 300 MB a ~1 MB/s deixava o heartbeat
        parado por 5 min e o `status` nao distinguia "baixando" de "travado".
        """
        self.pulso()
        return self.pedido_de_parada()

    def pulso(self) -> None:
        """Bate SO o heartbeat, sem mexer em item atual nem contadores, e no
        maximo uma vez a cada `heartbeat_s`. Fora de transacao: o download
        acontece depois do commit do HYDRATING."""
        agora = time.monotonic()
        if agora - self.ultimo_heartbeat < float(self.w.get("heartbeat_s", 20)):
            return
        self.ultimo_heartbeat = agora
        cu.escrever_controle(self.con, heartbeat=cu.agora_sql())

    # ------------------------------------------------------------------
    # Retomada: quem morreu no meio volta para a fila
    # ------------------------------------------------------------------
    def recuperar_orfaos(self) -> int:
        marcador = ",".join("?" * len(cu.ESTADOS_ORFAOS))
        filtro, params = "", list(cu.ESTADOS_ORFAOS)
        if getattr(self.args, "ids", None):
            # B-9: numa amostra controlada (--ids) so os itens da amostra
            # sao ressuscitados — nada fora do escopo declarado (DEC-034).
            filtro = f" AND id IN ({','.join('?' * len(self.args.ids))})"
            params.extend(self.args.ids)
        linhas = self.con.execute(
            f"SELECT id, caminho_rel, estado, tentativas FROM arquivo "
            f"WHERE estado IN ({marcador})" + filtro, params).fetchall()
        isolados = 0
        for linha in linhas:
            # B3 — pilula envenenada. O item que estava no meio quando o
            # processo morreu (crash nativo do extrator, falta de memoria,
            # janela fechada com a extracao travada) e, pela ordem da fila, o
            # PRIMEIRO a ser pego de novo. Sem limite, ele derrubava o worker
            # a cada disparo do Agendador e a fila nao andava nunca mais.
            quedas = self.quedas_do_item(linha["id"]) + 1
            if quedas >= self.max_quedas_por_item:
                self._isolar_por_queda(linha, quedas)
                isolados += 1
                continue
            self.con.execute("UPDATE arquivo SET estado='QUEUED' WHERE id=?", (linha["id"],))
            self.con.execute(
                "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, detalhe, em) "
                "VALUES (?,?,?,?,?)",
                (linha["id"], self.execucao_id, "retomado_apos_queda",
                 json.dumps({"estado_anterior": linha["estado"], "queda": quedas,
                             "limite": self.max_quedas_por_item}), cu.agora_iso()))
        self.con.commit()
        if len(linhas) - isolados:
            self.log.aviso(f"{len(linhas) - isolados} item(ns) interrompidos na execucao "
                           f"anterior voltaram para a fila")
        return len(linhas)

    def quedas_do_item(self, arquivo_id: int) -> int:
        """Quantas vezes este item ja foi achado orfao desde o ultimo desfecho.

        Sai do historico (`evento_arquivo`), como o contador de rede (M-6):
        sem coluna nova. Zera quando o item e processado, coberto por outro
        conteudo ou isolado — um item que mudou de bytes e voltou para a fila
        depois de isolado ganha o orcamento inteiro de novo.
        """
        return self.con.execute(
            "SELECT COUNT(*) FROM evento_arquivo WHERE arquivo_id=? "
            "AND evento='retomado_apos_queda' AND id > COALESCE(("
            "  SELECT MAX(id) FROM evento_arquivo WHERE arquivo_id=? "
            "  AND evento IN ('processado', 'coberto_por_conteudo', "
            "                 'quarentena_apos_queda')), 0)",
            (arquivo_id, arquivo_id)).fetchone()[0]

    def _isolar_por_queda(self, linha, quedas: int) -> None:
        """ERROR_FINAL com motivo explicito. Estado que ja existe (REGRA 7):
        o item nao some (REGRA 6), sai da fila e o relatorio de erros o mostra
        na categoria 'derrubou o worker'. Para dar outra chance depois de
        investigar: docs/10_OPERACAO_AUTONOMA.md, secao 5."""
        motivo = (f"derrubou o worker {quedas} vezes (a ultima com o item em "
                  f"{linha['estado']}); isolado em ERROR_FINAL para nao travar a fila")
        self.mudar_estado(linha["id"], "ERROR_FINAL",
                          tentativas=int(linha["tentativas"] or 0) + 1,
                          ultimo_erro=motivo, proxima_tentativa_em=None)
        self.evento(linha["id"], "quarentena_apos_queda",
                    {"estado_anterior": linha["estado"], "quedas": quedas,
                     "limite": self.max_quedas_por_item, "estado": "ERROR_FINAL"})
        self.contadores["erros"] += 1
        self.contadores["isolados_por_queda"] = (
            self.contadores.get("isolados_por_queda", 0) + 1)
        self.log.erro(f"ERROR_FINAL: {motivo}", linha["caminho_rel"])

    # ------------------------------------------------------------------
    # Travas de seguranca
    # ------------------------------------------------------------------
    def checar_orcamento(self) -> op.Veredito:
        """Disco livre, teto de derivados, teto de banda e teto de itens.

        Um veredito so, com o ESTADO que o encerramento deve registrar —
        `PAUSA_SEGURA_DISCO`, `LIMITE_BANDA` ou `LIMITE_SESSAO`. Nada de
        "parou e nao sei por que" (REGRA 7).
        """
        return self.orcamento.checar(
            self.con,
            bytes_sessao=self.contadores["bytes_baixados"],
            itens_sessao=self.contadores["arquivos"])

    def checar_disco(self) -> tuple[bool, str]:
        """Compatibilidade: a trava de disco isolada, como antes do E9."""
        v = self.orcamento.checar(self.con)
        return v.ok, v.motivo

    def comando_atual(self) -> str:
        return cu.ler_controle(self.con)["comando"]

    def heartbeat(self, item: str = "", item_id: int | None = None, estado: str = "RODANDO"):
        agora = time.monotonic()
        if agora - self.ultimo_heartbeat < float(self.w.get("heartbeat_s", 20)) and item == "":
            return
        self.ultimo_heartbeat = agora
        cu.escrever_controle(
            self.con, estado=estado, heartbeat=cu.agora_sql(), item_atual=item,
            item_atual_id=item_id, processados_sessao=self.contadores["arquivos"],
            bytes_sessao=self.contadores["bytes"], erros_sessao=self.contadores["erros"])

    def checkpoint(self, item_id=None, item="", estado="RODANDO", observacao=""):
        concluidos = self.con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado='COMPLETED'").fetchone()[0]
        # T-12 / A-2 / M-7: `pendentes` e a fila que ESTE worker consome. Na
        # nuvem e a `v_fila_nuvem` (DEC-030) — a `v_fila` inclui os 592 fora
        # de escopo (DEC-017) que o worker nunca pega, e o checkpoint gravava
        # 65.384 pendentes ao lado de um FILA_VAZIA.
        pendentes = self.con.execute(
            f"SELECT COUNT(*) FROM {self.vista_fila()}").fetchone()[0]
        erros = self.con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE estado IN ('ERROR_FINAL','ERROR_RETRYABLE')"
        ).fetchone()[0]
        derivados_mb = (self.con.execute(
            "SELECT COALESCE(SUM(bytes_texto),0) FROM extracao").fetchone()[0] or 0) / 1024 ** 2
        self.con.execute(
            "INSERT INTO checkpoint (execucao_id, em, estado_worker, ultimo_item_id, ultimo_item,"
            " concluidos, pendentes, erros, bytes_processados, derivados_mb, espaco_livre_gb,"
            " observacao) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.execucao_id, cu.agora_sql(), estado, item_id, item, concluidos, pendentes,
             erros, self.contadores["bytes"], round(derivados_mb, 2),
             round(cu.espaco_livre_gb(self.cfg["_banco_path"].anchor or "C:\\"), 2), observacao))
        self.con.commit()

    # ------------------------------------------------------------------
    # Fila
    # ------------------------------------------------------------------
    def proximo(self):
        filtro = ""
        params: list = []
        if self.args.prioridade_max is not None:
            filtro = " AND prioridade <= ?"
            params.append(int(self.args.prioridade_max))
        if getattr(self.args, "ids", None):
            marcas = ",".join("?" * len(self.args.ids))
            filtro += f" AND id IN ({marcas})"
            params.extend(self.args.ids)

        if self.fonte.nome == "graph":
            # DEC-030: sem (drive_id, item_id) nao ha por onde baixar. E nao
            # existe fallback para o disco sincronizado.
            return self.con.execute(
                "SELECT id, caminho_rel, caminho_nuvem, categoria, tamanho_bytes, estado, "
                "tentativas, prioridade, drive_id, item_id, quick_xor, conteudo_id "
                "FROM arquivo WHERE presente=1 AND presente_nuvem=1 AND suportado=1 "
                "AND drive_id IS NOT NULL AND item_id IS NOT NULL "
                # WAITING_HYDRATION aqui quer dizer "esperando a nuvem", nao
                # "esperando hidratacao" (o nome sobrou da DEC-013). E onde
                # `falhar()` poe quem tropecou na rede — sem este estado na
                # lista, um 503 tiraria o item da fila para sempre.
                "AND estado IN ('QUEUED','CHANGED','READY','ERROR_RETRYABLE',"
                "'WAITING_HYDRATION') "
                "AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= ?)"
                + filtro +
                " ORDER BY prioridade ASC, tamanho_bytes ASC LIMIT 1",
                [cu.agora_sql()] + params).fetchone()

        return self.con.execute(
            "SELECT id, caminho_rel, caminho_nuvem, categoria, tamanho_bytes, online, "
            "estado, tentativas, prioridade, sha256, quick_xor, drive_id, item_id, "
            "conteudo_id FROM arquivo WHERE presente=1 AND suportado=1 "
            "AND estado IN ('QUEUED','CHANGED','READY','WAITING_HYDRATION','ERROR_RETRYABLE') "
            "AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= ?)"
            + filtro +
            " ORDER BY prioridade ASC, online ASC, tamanho_bytes ASC LIMIT 1",
            [cu.agora_sql()] + params).fetchone()

    def vista_fila(self) -> str:
        """A view da fila que este worker de fato consome (mesma regra de
        `operacao.medir()`)."""
        return "v_fila_nuvem" if self.fonte.nome == "graph" else "v_fila"

    def restam_em_espera(self) -> int:
        # Na nuvem, so conta quem `proximo()` poderia pegar depois do backoff.
        # Sem o filtro, um item em espera e fora do escopo da nuvem deixaria o
        # worker dormindo `espera_fila_vazia_s` atras de algo que nunca vem.
        nuvem = (" AND presente_nuvem=1 AND drive_id IS NOT NULL AND item_id IS NOT NULL"
                 if self.fonte.nome == "graph" else "")
        return self.con.execute(
            "SELECT COUNT(*) FROM arquivo WHERE presente=1 AND suportado=1 "
            "AND estado IN ('WAITING_HYDRATION','ERROR_RETRYABLE')" + nuvem).fetchone()[0]

    # ------------------------------------------------------------------
    # Estados
    # ------------------------------------------------------------------
    def mudar_estado(self, arquivo_id: int, estado: str, **campos):
        sets = ["estado=?"]
        valores: list = [estado]
        for chave, valor in campos.items():
            sets.append(f"{chave}=?")
            valores.append(valor)
        valores.append(arquivo_id)
        self.con.execute(f"UPDATE arquivo SET {', '.join(sets)} WHERE id=?", valores)

    def evento(self, arquivo_id: int, evento: str, detalhe: dict | None = None):
        self.con.execute(
            "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, detalhe, em) "
            "VALUES (?,?,?,?,?)",
            (arquivo_id, self.execucao_id, evento,
             json.dumps(detalhe, ensure_ascii=False, default=str) if detalhe else None,
             cu.agora_iso()))

    def marcar_ausente(self, item, motivo: str):
        """O Graph respondeu 404: o item nao esta mais la (DEC-029).

        Nao e erro de processamento — e o acervo que mudou. A linha fica
        inteira, com classificacao, extracoes, derivados e vinculos; o
        `estado_anterior` vai no evento, e a reconciliacao devolve o item ao
        estado de antes se ele reaparecer no delta. Nunca `DELETE`, nunca
        `ERROR_FINAL` (REGRA 6).
        """
        # `presente=0` e `presente_nuvem=0` sao a convencao da REGRA 6 e da
        # DEC-029 — e o que o `15_marcar_ausentes.py` do E6 gravou nos 1.457.
        # Sem `presente=0` o item ficaria contado como presente no
        # `99_status.py` e o representante ausente nunca seria reeleito.
        self.mudar_estado(item["id"], "MISSING", presente=0, presente_nuvem=0,
                          ultimo_erro=motivo[:500], proxima_tentativa_em=None)
        # T-11 (auditoria A-1): na MESMA transacao, o conteudo desta
        # ocorrencia e recontado e, se ela era o representante, uma copia viva
        # assume — com os trechos do indice. Antes isso so acontecia na
        # proxima subida do worker, e ate la a busca respondia com um arquivo
        # que nao existe mais.
        dedupe = cu.ocorrencia_saiu(self.con, item["id"], self.execucao_id,
                                    "representante ausente na nuvem (404)")
        self.evento(item["id"], "ausente_na_nuvem",
                    {"estado_anterior": item["estado"], "motivo": motivo[:300],
                     "detectado_por": "worker", "item_id": item["item_id"]
                     if "item_id" in item.keys() else None, **dedupe})
        if dedupe.get("novo_representante"):
            self.contadores["reeleicoes"] += 1
        self.contadores["ausentes"] += 1
        self.log.aviso(f"MISSING: o Graph nao encontra mais este item ({motivo[:80]})",
                       item["caminho_rel"])

    def falhas_de_rede(self, arquivo_id: int) -> int:
        """Falhas de nuvem/rede deste item desde o ultimo processamento.

        Contador PROPRIO da rede (M-6), separado de `arquivo.tentativas`, que
        e do documento. Sai do historico (`evento_arquivo`, `detalhe.rede=1`),
        que ja e gravado a cada falha — sem coluna nova no schema. Zera quando
        o item e processado ou coberto por conteudo.
        """
        return self.con.execute(
            "SELECT COUNT(*) FROM evento_arquivo WHERE arquivo_id=? AND evento='falha' "
            "AND json_extract(detalhe, '$.rede') = 1 AND id > COALESCE(("
            "  SELECT MAX(id) FROM evento_arquivo WHERE arquivo_id=? "
            "  AND evento IN ('processado', 'coberto_por_conteudo')), 0)",
            (arquivo_id, arquivo_id)).fetchone()[0]

    def falhar(self, item, mensagem: str, transitorio: bool):
        tentativas = int(item["tentativas"])
        rede = 0
        if transitorio:
            rede = self.falhas_de_rede(item["id"]) + 1
        if transitorio and rede <= self.max_tentativas * 3:
            # Nuvem/rede fora do ar NAO gasta as tentativas do documento (M-6):
            # `tentativas` fica como esta. Antes o contador era um so, e um
            # item com 3 quedas de rede chegava ao primeiro erro real com
            # tentativas=4 e ia direto para ERROR_FINAL.
            estado = "WAITING_HYDRATION"
            espera = cu.backoff_segundos(rede, base=120.0)
        else:
            # Erro do documento — ou rede que esgotou o seu proprio orcamento
            # (max_tentativas x 3): a partir dai cada falha conta para o item.
            tentativas += 1
            if tentativas >= self.max_tentativas:
                estado = "ERROR_FINAL"
                espera = None
            else:
                estado = "ERROR_RETRYABLE"
                espera = cu.backoff_segundos(tentativas)
        self.mudar_estado(item["id"], estado, tentativas=tentativas, ultimo_erro=mensagem[:500],
                          proxima_tentativa_em=cu.daqui_a_sql(espera) if espera else None)
        self.evento(item["id"], "falha", {"estado": estado, "tentativa": tentativas,
                                          "rede": 1 if transitorio else 0,
                                          "tentativa_rede": rede or None,
                                          "erro": mensagem[:500]})
        self.contadores["erros"] += 1
        contagem = (f"rede {rede}/{self.max_tentativas * 3}" if estado == "WAITING_HYDRATION"
                    else f"{tentativas}/{self.max_tentativas}")
        self.log.erro(f"{estado} ({contagem}): {mensagem[:200]}", item["caminho_rel"])

    # ------------------------------------------------------------------
    # B4 — disjuntor de credencial
    # ------------------------------------------------------------------
    def _credencial_ok(self) -> None:
        """O Graph respondeu autenticado (bytes ou 404): a credencial vale."""
        self.credencial_comprovada = True
        self.falhas_credencial_seguidas = 0

    def falhas_credencial_do_item(self, arquivo_id: int) -> int:
        """Falhas de credencial deste item desde o ultimo processamento — so
        para dimensionar a espera dele (mesma forma de `falhas_de_rede`)."""
        return self.con.execute(
            "SELECT COUNT(*) FROM evento_arquivo WHERE arquivo_id=? "
            "AND evento='falha_credencial' AND id > COALESCE(("
            "  SELECT MAX(id) FROM evento_arquivo WHERE arquivo_id=? "
            "  AND evento IN ('processado', 'coberto_por_conteudo')), 0)",
            (arquivo_id, arquivo_id)).fetchone()[0]

    def _falha_credencial(self, item, obtido: dict) -> str:
        """401/403/login recusado no download. Uma transacao (REGRA 8).

        Por que nao e falha do documento: com o segredo do aplicativo
        expirado, TODO download responde 401. Tratado como erro do item, cada
        um gastava tentativa ate ERROR_FINAL e o worker varreria a fila
        inteira (~62 mil) para ERROR_FINAL numa noite.

        A regra, deterministica:

        * 401 / `falha_autenticacao` -> SEMPRE sistemico. Token e do
          aplicativo, nao do item: nao existe "401 deste arquivo".
        * 403 -> o Graph usa o mesmo `accessDenied` para "o app perdeu acesso
          ao site" e para "este item tem permissao propria". Nao da para
          distinguir pela resposta. Distingue-se pelo contexto: se a
          credencial JA FUNCIONOU nesta sessao e nao ha outra falha de
          credencial em sequencia, o 403 e do item (`isolado`) e gasta
          tentativa dele, como erro do documento — um item proibido termina
          ERROR_FINAL e nao para o worker. Antes de qualquer prova (inicio da
          sessao) ou em sequencia, e sistemico.

        Sistemico: o item volta ao estado de antes, SEM gastar tentativa, com
        espera propria (para o proximo da fila ser tentado e servir de
        prova), e evento `falha_credencial`. O laco (`rodar`) encerra em
        AGUARDANDO_CREDENCIAL depois de `falhas_credencial_para_parar`
        seguidas (padrao 3) — no pior caso, uma revogacao no meio da sessao
        custa 1 tentativa a 1 item.
        """
        arquivo_id = int(item["id"])
        status, codigo = obtido.get("status"), obtido.get("codigo")
        motivo = obtido["motivo"]
        isolado = (status == 403 and self.credencial_comprovada
                   and self.falhas_credencial_seguidas == 0)
        self.falhas_credencial_seguidas += 1
        self.ultima_falha_credencial = motivo
        self.contadores["falhas_credencial"] = self.contadores.get("falhas_credencial", 0) + 1
        detalhe = {"estado_anterior": item["estado"], "status": status, "codigo": codigo,
                   "motivo": motivo[:300], "seguidas": self.falhas_credencial_seguidas,
                   "isolado": 1 if isolado else 0}
        if isolado:
            self.evento(arquivo_id, "falha_credencial", {**detalhe, "gasta_tentativa": 1})
            self.falhar(item, motivo, False)
            self.con.commit()
            return "credencial_item"

        n = self.falhas_credencial_do_item(arquivo_id) + 1
        espera = cu.backoff_segundos(n, base=float(self.w.get("espera_credencial_item_s", 300)))
        self.mudar_estado(arquivo_id, item["estado"], ultimo_erro=motivo[:500],
                          proxima_tentativa_em=cu.daqui_a_sql(espera))
        self.evento(arquivo_id, "falha_credencial",
                    {**detalhe, "gasta_tentativa": 0, "espera_s": espera})
        self.con.commit()
        self.log.erro(f"credencial recusada ({self.falhas_credencial_seguidas}/"
                      f"{self.falhas_credencial_para_parar} seguidas), sem gastar tentativa "
                      f"do documento: {motivo[:160]}", item["caminho_rel"])
        return "credencial"

    # ------------------------------------------------------------------
    # Conteudo x ocorrencia (schema v4)
    # ------------------------------------------------------------------
    def conteudo_de(self, item):
        """O conteudo destes bytes ja foi extraido por outra ocorrencia?

        Chave: (quick_xor, tamanho). E a evidencia FORTE da politica —
        `docs/07_POLITICA_DUPLICATAS.md`. Sem hash do servidor, nao ha grupo:
        o arquivo e tratado como unico.
        """
        quick = item["quick_xor"] if "quick_xor" in item.keys() else None
        if not quick or not item["tamanho_bytes"]:
            return None
        return self.con.execute(
            "SELECT * FROM conteudo WHERE quick_xor=? AND tamanho_bytes=?",
            (quick, item["tamanho_bytes"])).fetchone()

    def recontar_ocorrencias(self, conteudo_id) -> None:
        """Reconta quantas ocorrencias apontam para este conteudo.

        Chamada dos DOIS lados de uma troca: quem recebeu a ocorrencia e
        quem a perdeu. Sem o segundo lado, um arquivo que muda de conteudo
        deixa o contador antigo inflado para sempre — `v_ocorrencia` diria
        uma coisa e `conteudo.ocorrencias` outra.
        """
        # Uma definicao so de "ocorrencia" (as VIVAS, presente=1): a de
        # `cu.recontar_ocorrencias`, a mesma de `v_ocorrencia` (A-1).
        cu.recontar_ocorrencias(self.con, conteudo_id)

    @staticmethod
    def _conteudo_anterior(item):
        return item["conteudo_id"] if "conteudo_id" in item.keys() else None

    def cobrir_por_conteudo(self, item, conteudo) -> None:
        """Liga a ocorrencia a um conteudo ja extraido. Nao baixa nada.

        A ocorrencia continua inteira — caminho, item_id, datas, autoria e
        prioridade ficam como estao. `DUPLICATE` aqui quer dizer "coberta para
        busca, encerrada para processamento", nunca "ignorada".
        """
        anterior = self._conteudo_anterior(item)
        self.mudar_estado(item["id"], "DUPLICATE",
                          conteudo_id=conteudo["id"],
                          duplicata_de=conteudo["representante_id"],
                          processado_em=cu.agora_iso(),
                          ultimo_erro=None, proxima_tentativa_em=None)
        cu.recontar_ocorrencias(self.con, conteudo["id"])
        if anterior and anterior != conteudo["id"]:
            # Saiu do grupo antigo: reconta la e, se era o representante,
            # passa o posto (e os trechos) para uma copia viva (DEC-032).
            cu.recontar_ocorrencias(self.con, anterior)
            cu.reeleger_conteudo(self.con, anterior, self.execucao_id,
                                 "representante mudou de conteudo")
        self.evento(item["id"], "coberto_por_conteudo",
                    {"conteudo_id": conteudo["id"],
                     "representante": conteudo["representante_id"],
                     "evidencia": conteudo["evidencia"],
                     "bytes_economizados": int(item["tamanho_bytes"] or 0)})
        self.contadores["duplicatas"] += 1
        self.contadores["bytes_economizados"] += int(item["tamanho_bytes"] or 0)

    def gravar_conteudo(self, item, resultado: dict, quick: str | None,
                        texto_path, perfil_path, trechos: int, bytes_texto: int) -> int | None:
        """Cria/atualiza a linha de `conteudo` desta extracao."""
        if not quick or not item["tamanho_bytes"]:
            return None
        mapa = {"ok": "EXTRAIDO", "parcial": "PARCIAL", "ocr_requerido": "OCR_REQUIRED",
                "vazio": "VAZIO", "nao_suportado": "NAO_SUPORTADO", "protegido": "ERRO",
                "erro": "ERRO"}
        agora = cu.agora_iso()
        self.con.execute(
            "INSERT INTO conteudo (quick_xor, tamanho_bytes, evidencia, representante_id, "
            "extraido_de_item, estado, extrator, versao_extrator, texto_path, perfil_path, "
            "paginas, caracteres, trechos, bytes_texto, ocorrencias, primeira_vez_em, "
            "atualizado_em) VALUES (?,?,'FORTE',?,?,?,?,?,?,?,?,?,?,?,1,?,?) "
            "ON CONFLICT(quick_xor, tamanho_bytes) DO UPDATE SET "
            "representante_id=excluded.representante_id, estado=excluded.estado, "
            "extrator=excluded.extrator, versao_extrator=excluded.versao_extrator, "
            "texto_path=excluded.texto_path, perfil_path=excluded.perfil_path, "
            "paginas=excluded.paginas, caracteres=excluded.caracteres, "
            "trechos=excluded.trechos, bytes_texto=excluded.bytes_texto, "
            "extraido_de_item=excluded.extraido_de_item, atualizado_em=excluded.atualizado_em",
            (quick, item["tamanho_bytes"], item["id"], item["item_id"] if "item_id" in
             item.keys() else None, mapa.get(resultado.get("status"), "ERRO"),
             resultado.get("extrator"), resultado.get("versao"),
             str(texto_path) if texto_path else None,
             str(perfil_path) if perfil_path else None,
             resultado.get("paginas"), resultado.get("caracteres"), trechos, bytes_texto,
             agora, agora))
        linha = self.con.execute(
            "SELECT id FROM conteudo WHERE quick_xor=? AND tamanho_bytes=?",
            (quick, item["tamanho_bytes"])).fetchone()
        conteudo_id = int(linha["id"]) if linha else None
        if conteudo_id:
            anterior = self._conteudo_anterior(item)
            self.con.execute("UPDATE arquivo SET conteudo_id=? WHERE id=?",
                             (conteudo_id, item["id"]))
            self.recontar_ocorrencias(conteudo_id)
            if anterior and anterior != conteudo_id:
                # A copia mudou de bytes: nao pertence mais ao grupo antigo.
                # Se era o representante de la, o grupo antigo precisa de outro.
                self.recontar_ocorrencias(anterior)
                cu.reeleger_conteudo(self.con, anterior, self.execucao_id,
                                     "representante mudou de conteudo")
        return conteudo_id

    def reeleger_representantes(self) -> int:
        """Representante sumiu? Outra ocorrencia assume — SEM reextrair.

        O conteudo e o mesmo; o que muda e de quem o lemos. Os derivados e o
        indice continuam valendo, e nada volta para a fila por causa disto.
        """
        # A logica mora em cerebro_util (uma copia so, a mesma do
        # 17_aplicar_delta). Ela tambem re-aponta `duplicata_de` e muda os
        # trechos do indice de dono — antes ficavam sob o representante morto.
        trocas = cu.reeleger_orfaos(self.con, self.execucao_id,
                                    "reeleicao na subida do worker")
        if trocas:
            self.con.commit()
            self.log.aviso(f"{trocas} conteudo(s) trocaram de representante "
                           f"sem reextracao")
        self.contadores["reeleicoes"] += trocas
        return trocas

    # ------------------------------------------------------------------
    # Processamento de um item = uma transacao = um checkpoint
    # ------------------------------------------------------------------
    def processar(self, item) -> str:
        arquivo_id = int(item["id"])
        caminho_rel = item["caminho_rel"]
        inicio = time.perf_counter()
        obtido = None

        self.heartbeat(caminho_rel, arquivo_id)

        # --- 1. o conteudo ja foi extraido por outra ocorrencia? ---------
        ja = self.conteudo_de(item)
        # Cuidado: se ESTE arquivo ja e o representante do conteudo, ele nao e
        # duplicata de si mesmo — esta sendo reprocessado, e deve reextrair.
        if (ja is not None and ja["representante_id"] != arquivo_id
                and ja["estado"] in ("EXTRAIDO", "PARCIAL", "OCR_REQUIRED", "VAZIO")):
            self.cobrir_por_conteudo(item, ja)
            self.contadores["arquivos"] += 1
            self.con.commit()
            return "duplicata"

        try:
            # --- 1a. formato sem leitor: nem baixa (L7) -------------------
            # `.xls`, `.xlsb`, `.doc`, `.rtf`... caem numa categoria com
            # extrator, mas nenhuma biblioteca instalada os le: eram baixados
            # so para o extrator devolver `nao_suportado`. A decisao e pela
            # extensao — a mesma regra que o extrator aplica depois do
            # download (`extratores.motivo_sem_leitor`) — e o item segue pelo
            # MESMO caminho de gravacao, terminando em UNSUPPORTED (REGRA 6),
            # sem estado novo. Vem antes do teto: formato sem leitor e
            # UNSUPPORTED de qualquer tamanho.
            ext = extratores.extensao(
                (item["caminho_nuvem"] if "caminho_nuvem" in item.keys() else None)
                or caminho_rel)
            if not extratores.suporta(ext, item["categoria"]):
                quick = item["quick_xor"] if "quick_xor" in item.keys() else None
                resultado = extratores.resultado_sem_leitor(ext, item["categoria"])
                resultado["erro"] = (f"{PREFIXO_ERRO_SEM_LEITOR}: {resultado['erro']}; "
                                     f"nada foi baixado")
                return self._registrar(item, resultado, quick, inicio, baixado=False,
                                       adiado="sem_leitor")

            # --- 1b. acima do teto de extracao: nem baixa (T-10 / A-3) ----
            # Antes, o teto era conferido DEPOIS do download: 74 itens / 39 GB
            # baixados inteiros so para virar PARTIAL. Agora a decisao sai do
            # tamanho que o inventario ja conhece, e o item segue pelo mesmo
            # caminho de gravacao de qualquer PARTIAL — com o `quick_xor` do
            # inventario, para que as copias do mesmo arquivo grande sejam
            # cobertas no passo 1 sem download nenhum.
            teto_mb = float(self.w.get("tamanho_max_extracao_mb", 300))
            if int(item["tamanho_bytes"] or 0) > teto_mb * 1024 ** 2:
                quick = item["quick_xor"] if "quick_xor" in item.keys() else None
                return self._registrar(item, resultado_adiado_por_tamanho(item, teto_mb),
                                       quick, inicio, baixado=False)

            # --- 2. obter os bytes ---------------------------------------
            self.mudar_estado(arquivo_id, "HYDRATING")
            self.con.commit()
            obtido = self.fonte.obter(item)
            if not obtido["ok"]:
                if obtido.get("interrompido"):
                    # Pausa/parada pedida no meio do download (M-14). Nao e
                    # falha: o item volta exatamente ao estado de antes, sem
                    # gastar tentativa, e o laco encerra na proxima volta.
                    self.mudar_estado(arquivo_id, item["estado"])
                    self.evento(arquivo_id, "download_interrompido",
                                {"estado_anterior": item["estado"],
                                 "motivo": obtido["motivo"][:300]})
                    self.contadores["interrompidos"] += 1
                    self.con.commit()
                    self.log.aviso("download interrompido por pedido de parada; "
                                   "o item volta para a fila", caminho_rel)
                    return "interrompido"
                if obtido.get("ausente"):
                    # 404 e resposta autenticada: a credencial funciona.
                    self._credencial_ok()
                    self.marcar_ausente(item, obtido["motivo"])
                    self.con.commit()
                    return "ausente"
                if obtido.get("credencial"):
                    return self._falha_credencial(item, obtido)
                if "divergente" in obtido["motivo"]:
                    self.contadores["validacoes_falhas"] += 1
                    self.evento(arquivo_id, "validacao_falhou",
                                {"motivo": obtido["motivo"]})
                self.falhar(item, obtido["motivo"], obtido["transitorio"])
                self.con.commit()
                return "transitorio" if obtido["transitorio"] else "erro"

            self._credencial_ok()
            self.contadores["bytes_baixados"] += obtido["bytes"]
            caminho_local = obtido["caminho"]
            quick = obtido["quick_xor"] or (item["quick_xor"] if "quick_xor" in item.keys()
                                            else None)

            # 2b. Segunda checagem de conteudo, agora com o hash dos bytes que
            # chegaram. Pega o que o inventario nao sabia (item sem hash do
            # servidor) e e a rede de seguranca da politica de duplicatas:
            # baixou, mas nao extrai duas vezes o mesmo conteudo.
            if quick and quick != (item["quick_xor"] if "quick_xor" in item.keys()
                                   else None):
                self.con.execute("UPDATE arquivo SET quick_xor=? WHERE id=?",
                                 (quick, arquivo_id))
            ja = self.conteudo_de({"quick_xor": quick,
                                   "tamanho_bytes": item["tamanho_bytes"]})
            if (ja is not None and ja["representante_id"] != arquivo_id
                    and ja["estado"] in ("EXTRAIDO", "PARCIAL", "OCR_REQUIRED",
                                         "VAZIO")):
                self.cobrir_por_conteudo(item, ja)
                self.contadores["arquivos"] += 1
                self.con.commit()
                return "duplicata"

            # --- 3. extrair ----------------------------------------------
            self.mudar_estado(arquivo_id, "EXTRACTING", quick_xor=quick)
            self.con.commit()

            # (o teto de extracao ja foi conferido no passo 1b, ANTES do
            # download: aqui so chega quem esta dentro dele)
            vazio = int(item["tamanho_bytes"] or 0) == 0
            if vazio:
                resultado = {"status": "vazio", "extrator": item["categoria"],
                             "versao": "-", "texto": "", "paginas_texto": [],
                             "erro": "arquivo com 0 bytes", "paginas": None,
                             "paginas_com_texto": None, "slides": None, "abas": None,
                             "imagens": None, "caracteres": 0, "palavras": 0,
                             "densidade_texto": None}
            else:
                resultado = extratores.extrair(caminho_local, item["categoria"], self.cfg)

            return self._registrar(item, resultado, quick, inicio)

        except Exception as exc:
            self.con.rollback()
            self.falhar(item, f"{type(exc).__name__}: {exc}", cu.erro_transitorio(exc))
            self.con.commit()
            return "erro"
        finally:
            # O temporario morre aqui, deu certo ou nao. `remover=False` na
            # fonte local garante que o original nunca e tocado.
            if obtido and obtido.get("caminho") and obtido.get("remover"):
                if getattr(self.args, "manter_tmp", False):
                    self.log.aviso(f"--manter-tmp: {obtido['caminho']} preservado")
                else:
                    self.fonte.descartar(obtido["caminho"])

    def _registrar(self, item, resultado: dict, quick, inicio: float,
                   baixado: bool = True, adiado: str = "teto_tamanho") -> str:
        """Passos 4 e 5 de `processar()`: derivados + registro, UMA transacao.

        Usado pelo caminho normal (depois do download e da extracao) e pelo
        caminho do teto de extracao (T-10), que nao baixa nada. Um caminho de
        gravacao so: extracao, perfil, conteudo, estado e evento sempre
        juntos, com um commit no fim (REGRA 8). Excecao aqui sobe para o
        `try` de `processar()`, que faz rollback e `falhar()`.
        """
        arquivo_id = int(item["id"])
        caminho_rel = item["caminho_rel"]

        # --- 4. derivados --------------------------------------------
        texto_path = perfil_path = None
        bytes_texto = trechos = 0
        if resultado.get("texto"):
            destino = cu.caminho_derivado(self.cfg, arquivo_id, "texto")
            cu.assert_escrita_permitida(destino, self.raiz)
            bytes_texto = cu.gravar_texto(destino, resultado["texto"])
            if bytes_texto <= 0 or not destino.exists():
                raise OSError("derivado de texto nao foi gravado")
            texto_path = destino
            if not self.busca_v5:
                # v4: indice por arquivo. Na v5 o indice e por conteudo e so
                # pode ser gravado depois de `gravar_conteudo()` (passo 5).
                trechos = self.indexar(arquivo_id, caminho_rel, resultado)

        perfil = {
            "arquivo_id": arquivo_id, "caminho_rel": caminho_rel,
            "caminho_nuvem": item["caminho_nuvem"] if "caminho_nuvem" in item.keys()
                             else None,
            "drive_id": item["drive_id"] if "drive_id" in item.keys() else None,
            "item_id": item["item_id"] if "item_id" in item.keys() else None,
            "quick_xor": quick, "tamanho_bytes": item["tamanho_bytes"],
            "categoria": item["categoria"], "prioridade": item["prioridade"],
            "extrator": resultado.get("extrator"), "versao": resultado.get("versao"),
            "status": resultado.get("status"), "paginas": resultado.get("paginas"),
            "slides": resultado.get("slides"), "abas": resultado.get("abas"),
            "imagens": resultado.get("imagens"),
            "paginas_com_texto": resultado.get("paginas_com_texto"),
            "caracteres": resultado.get("caracteres"),
            "palavras": resultado.get("palavras"),
            "densidade_texto": resultado.get("densidade_texto"),
            "trechos_indexados": trechos, "erro": resultado.get("erro"),
            "extraido_em": cu.agora_iso(),
            # metadados, tabelas e paginas que pedem OCR (E9): so existem no
            # resultado do extrator; o perfil e o lugar deles.
            **{chave: resultado[chave] for chave in extratores.CHAVES_EXTRAS
               if resultado.get(chave) not in (None, {}, [])},
        }
        destino_perfil = cu.caminho_derivado(self.cfg, arquivo_id, "perfil")
        cu.assert_escrita_permitida(destino_perfil, self.raiz)
        cu.gravar_perfil(destino_perfil, perfil)
        perfil_path = destino_perfil

        # --- 5. registrar --------------------------------------------
        mapa = {"ok": "COMPLETED", "parcial": "PARTIAL",
                "ocr_requerido": "OCR_REQUIRED", "vazio": "COMPLETED",
                "protegido": "ERROR_FINAL", "nao_suportado": "UNSUPPORTED",
                "erro": None}
        status = resultado.get("status", "erro")
        estado = mapa.get(status)
        duracao_ms = int((time.perf_counter() - inicio) * 1000)

        self.con.execute(
            "INSERT INTO extracao (arquivo_id, execucao_id, extrator, versao, "
            "sha256_fonte, status, paginas, paginas_com_texto, slides, abas, imagens, "
            "caracteres, palavras, densidade_texto, texto_path, perfil_path, "
            "bytes_texto, trechos, erro, tentativas, duracao_ms, executado_em) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(arquivo_id, extrator) DO UPDATE SET "
            "execucao_id=excluded.execucao_id, versao=excluded.versao, "
            "sha256_fonte=excluded.sha256_fonte, status=excluded.status, "
            "paginas=excluded.paginas, paginas_com_texto=excluded.paginas_com_texto, "
            "slides=excluded.slides, abas=excluded.abas, imagens=excluded.imagens, "
            "caracteres=excluded.caracteres, palavras=excluded.palavras, "
            "densidade_texto=excluded.densidade_texto, texto_path=excluded.texto_path, "
            "perfil_path=excluded.perfil_path, bytes_texto=excluded.bytes_texto, "
            "trechos=excluded.trechos, erro=excluded.erro, "
            "tentativas=extracao.tentativas+1, duracao_ms=excluded.duracao_ms, "
            "executado_em=excluded.executado_em",
            (arquivo_id, self.execucao_id, resultado.get("extrator"),
             resultado.get("versao"), quick, status, resultado.get("paginas"),
             resultado.get("paginas_com_texto"), resultado.get("slides"),
             resultado.get("abas"), resultado.get("imagens"),
             resultado.get("caracteres"), resultado.get("palavras"),
             resultado.get("densidade_texto"),
             str(texto_path) if texto_path else None,
             str(perfil_path) if perfil_path else None, bytes_texto, trechos,
             (resultado.get("erro") or "")[:500], 1, duracao_ms, cu.agora_iso()))

        if estado is None:
            self.falhar(item, resultado.get("erro") or "erro na extracao", False)
            self.con.commit()
            return "erro"

        conteudo_id = self.gravar_conteudo(item, resultado, quick, texto_path,
                                           perfil_path, trechos, bytes_texto)
        if self.busca_v5 and conteudo_id is not None:
            # Mesma transacao do item (REGRA 8): indexar_resultado nao commita.
            r = busca.indexar_resultado(self.con, conteudo_id, resultado,
                                        execucao_id=self.execucao_id)
            trechos = int(r["trechos"])
            self.con.execute("UPDATE conteudo SET trechos=? WHERE id=?",
                             (trechos, conteudo_id))
            self.con.execute("UPDATE extracao SET trechos=? WHERE arquivo_id=? "
                             "AND extrator=?",
                             (trechos, arquivo_id, resultado.get("extrator")))
            perfil["trechos_indexados"] = trechos
            cu.gravar_perfil(destino_perfil, perfil)
        self.mudar_estado(arquivo_id, estado, processado_em=cu.agora_iso(),
                          ultimo_erro=(resultado.get("erro") or None),
                          proxima_tentativa_em=None, quick_xor=quick,
                          conteudo_id=conteudo_id)
        self.evento(arquivo_id, "processado",
                    {"estado": estado, "status": status, "trechos": trechos,
                     "ms": duracao_ms, "conteudo_id": conteudo_id,
                     "fonte": self.fonte.nome, "baixado": baixado,
                     **({} if baixado else {"adiado": adiado})})

        self.contadores["arquivos"] += 1
        if baixado:
            # bytes que de fato passaram pelo extrator — o adiado nao conta.
            self.contadores["bytes"] += int(item["tamanho_bytes"] or 0)
        elif adiado == "teto_tamanho":
            self.contadores["adiados_tamanho"] += 1
        else:
            self.contadores[f"adiados_{adiado}"] = (
                self.contadores.get(f"adiados_{adiado}", 0) + 1)
        if estado == "COMPLETED":
            self.contadores["concluidos"] += 1
        elif estado == "OCR_REQUIRED":
            self.contadores["ocr"] += 1
        elif estado == "PARTIAL":
            self.contadores["parciais"] += 1
        self.con.commit()
        return "ok"

    def indexar(self, arquivo_id: int, caminho_rel: str, resultado: dict) -> int:
        """Alimenta o indice de busca, trecho a trecho, com a pagina de origem."""
        tamanho = int(self.limites.get("trecho_chars", 1500))
        sobreposicao = int(self.limites.get("trecho_sobreposicao", 150))
        self.con.execute("DELETE FROM texto_fts WHERE arquivo_id=?", (arquivo_id,))
        total = 0
        paginas = resultado.get("paginas_texto") or []
        if not paginas and resultado.get("texto"):
            paginas = [(0, resultado["texto"])]
        for pagina, texto in paginas:
            for indice, trecho in enumerate(cu.trechos_de(texto, tamanho, sobreposicao)):
                self.con.execute(
                    "INSERT INTO texto_fts (arquivo_id, caminho_rel, pagina, trecho_idx, texto) "
                    "VALUES (?,?,?,?,?)", (arquivo_id, caminho_rel, pagina, indice, trecho))
                total += 1
        return total

    def liberar(self, caminho_abs: Path, arquivo_id: int, hidratamos: bool):
        """Devolve o arquivo para 'somente na nuvem'. Nao apaga, nao move, nao altera."""
        if not self.nuvem.get("liberar_apos_processar", False):
            return
        if self.nuvem.get("liberar_somente_o_que_hidratamos", True) and not hidratamos:
            return
        try:
            cu.liberar_espaco(caminho_abs)
            self.con.execute(
                "UPDATE arquivo SET online=1, hidratado_por_nos=0, atributos=? WHERE id=?",
                (cu.atributos_de(caminho_abs), arquivo_id))
            self.evento(arquivo_id, "espaco_liberado")
        except Exception as exc:
            self.log.aviso(f"nao consegui liberar espaco: {exc}", str(caminho_abs))

    # ------------------------------------------------------------------
    # Laco principal
    # ------------------------------------------------------------------
    def rodar(self) -> str:
        cu.escrever_controle(self.con, estado="RODANDO", motivo="", pid=os.getpid(),
                             host=os.environ.get("COMPUTERNAME", ""),
                             execucao_id=self.execucao_id, iniciado_em=cu.agora_sql(),
                             heartbeat=cu.agora_sql(), processados_sessao=0, bytes_sessao=0,
                             erros_sessao=0, item_atual="", item_atual_id=None)
        self.recuperar_orfaos()
        sobras = self.fonte.limpar_sobras()
        if sobras:
            self.log.aviso(f"{sobras} temporario(s) de uma execucao interrompida "
                           f"foram apagados")
        self.reeleger_representantes()
        self.checkpoint(estado="RODANDO", observacao="inicio da sessao")

        desde_checkpoint = 0
        while True:
            if PARAR["pedido"]:
                return self._encerrar("PAUSADO", "parada pedida no terminal (Ctrl+C)")

            comando = self.comando_atual()
            if comando == "PAUSE":
                return self._encerrar("PAUSADO", "pausa pedida via control.py")
            if comando == "STOP":
                return self._encerrar("PARADO", "parada pedida via control.py")

            veredito = self.checar_orcamento()
            if not veredito.ok:
                return self._encerrar(veredito.estado, veredito.motivo)

            # A banda e dividida com o escritorio: fora da janela o worker
            # ENCERRA limpo em vez de dormir segurando o lock. Quem o traz de
            # volta e a tarefa agendada (docs/10_OPERACAO_AUTONOMA.md).
            veredito = self.janela.checar()
            if not veredito.ok:
                return self._encerrar(veredito.estado, veredito.motivo)

            item = self.proximo()
            if item is None:
                em_espera = self.restam_em_espera()
                if em_espera and not self.args.uma_vez:
                    espera = float(self.w.get("espera_fila_vazia_s", 300))
                    self.log.info(f"nada pronto agora; {em_espera} item(ns) em espera. "
                                  f"Dormindo {espera:.0f}s.")
                    if self._dormir(espera):
                        continue
                    return self._encerrar("PAUSADO", "parada durante a espera")
                return self._encerrar("FILA_VAZIA", "nao ha mais nada para processar")

            try:
                desfecho = self.processar(item)
            except KeyboardInterrupt:
                self.con.commit()
                return self._encerrar("PAUSADO", "Ctrl+C durante o processamento")
            except Exception as exc:                      # nunca derruba o worker
                self.con.rollback()
                self.falhar(item, f"{type(exc).__name__}: {exc}", cu.erro_transitorio(exc))
                self.con.commit()
                desfecho = "erro"

            if desfecho in ("credencial", "credencial_item"):
                # B4: parar e esperar gente. Insistir nao conserta segredo
                # expirado — so queimaria a fila. Encerramento limpo, estado
                # proprio e codigo de saida 6 (`executar`).
                if self.falhas_credencial_seguidas >= self.falhas_credencial_para_parar:
                    return self._encerrar(
                        "AGUARDANDO_CREDENCIAL",
                        f"{self.falhas_credencial_seguidas} falhas seguidas de credencial "
                        f"no Graph (ultima: {self.ultima_falha_credencial[:120]}). "
                        f"Renovar GRAPH_CLIENT_SECRET no .env ou a permissao do app no "
                        f"site; nenhuma tentativa de documento foi gasta")
            elif desfecho == "transitorio":
                self.falhas_seguidas += 1
                if self.falhas_seguidas >= int(self.w.get("falhas_seguidas_para_pausar", 25)):
                    return self._encerrar("AGUARDANDO_REDE",
                                          f"{self.falhas_seguidas} falhas seguidas de nuvem/rede")
                if self.falhas_seguidas % 5 == 0:
                    espera = float(self.w.get("espera_rede_s", 120))
                    self.log.aviso(f"{self.falhas_seguidas} falhas seguidas; "
                                   f"esperando {espera:.0f}s pela nuvem")
                    if not self._dormir(espera):
                        return self._encerrar("PAUSADO", "parada durante a espera de rede")
            elif desfecho == "sem_hidratacao":
                pass
            else:
                self.falhas_seguidas = 0

            desde_checkpoint += 1
            if desde_checkpoint >= self.checkpoint_a_cada:
                self.checkpoint(item["id"], item["caminho_rel"])
                desde_checkpoint = 0
                self._progresso()
            self.heartbeat()

    def _dormir(self, segundos: float) -> bool:
        """Dorme em fatias, olhando pedido de pausa. False = precisa parar."""
        fim = time.monotonic() + segundos
        while time.monotonic() < fim:
            if self.pedido_de_parada():
                return False
            time.sleep(min(2.0, max(0.1, fim - time.monotonic())))
        return True

    def _encerrar(self, estado: str, motivo: str) -> str:
        self.con.commit()
        self.checkpoint(estado=estado, observacao=motivo)
        cu.escrever_controle(self.con, estado=estado, motivo=motivo, item_atual="",
                             item_atual_id=None, heartbeat=cu.agora_sql(), pid=None)
        self.log.info(f"encerrando: {estado} — {motivo}")
        return estado

    def _progresso(self):
        decorrido = max(time.perf_counter() - self.t0, 0.001)
        taxa = self.contadores["arquivos"] / decorrido
        print(f"  ... {self.contadores['arquivos']} itens | "
              f"{self.contadores['concluidos']} ok | {self.contadores['ocr']} ocr | "
              f"{self.contadores['duplicatas']} dup | {self.contadores['erros']} erro | "
              f"{taxa*60:.1f} itens/min", flush=True)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def montar_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Processador autonomo do cerebro documental")
    ap.add_argument("comando", nargs="?", default="run", choices=["run"])
    adicionar_opcoes(ap)
    return ap


def adicionar_opcoes(ap: argparse.ArgumentParser) -> None:
    """As opcoes do processamento. `cerebro.py processar` usa as MESMAS —
    um lugar so para nao existirem dois conjuntos de flags divergindo."""
    ap.add_argument("--config", default=None)
    ap.add_argument("--limite", type=int, default=0, help="processa no maximo N itens")
    ap.add_argument("--prioridade-max", type=int, default=None,
                    help="so processa prioridade <= N (0=P0, 1=P1...)")
    ap.add_argument("--sem-hidratar", action="store_true",
                    help="so processa o que ja esta local; nao baixa nada")
    ap.add_argument("--so-nuvem", action="store_true",
                    help="so processa o que esta na nuvem (exercita hidratacao + liberacao)")
    ap.add_argument("--uma-vez", action="store_true",
                    help="nao dorme esperando a fila; encerra quando nao houver item pronto")
    ap.add_argument("--fonte", choices=["graph", "local"], default=None,
                    help="de onde vem o conteudo. Padrao: graph (a nuvem). "
                         "'local' e SO para teste automatizado.")
    ap.add_argument("--ids", default=None,
                    help="processa somente estes ids (lista separada por virgula) — "
                         "para amostras controladas")
    ap.add_argument("--manter-tmp", action="store_true",
                    help="modo diagnostico: nao apaga o temporario baixado")
    ap.add_argument("--max-gb-sessao", type=float, default=None,
                    help="teto de download desta sessao, em GB. Ao atingir, "
                         "encerra limpo em LIMITE_BANDA.")
    ap.add_argument("--ignorar-janela", action="store_true",
                    help="roda mesmo fora da janela de execucao configurada")
    ap.add_argument("--quieto", action="store_true")


def executar(args) -> tuple[int, str]:
    """Roda uma sessao de processamento. Devolve (codigo de saida, estado).

    Codigos: 0 ok · 2 banco sem schema · 3 ja tem worker (lock tomado) ·
             4 pausa/parada pendente · 5 busca v5 com versao divergente ·
             6 AGUARDANDO_CREDENCIAL (o Graph recusou a credencial do app
               `falhas_credencial_para_parar` vezes seguidas; B4).
    """
    args.ids = ([int(x) for x in args.ids.split(",") if str(x).strip()]
                if isinstance(args.ids, str) else args.ids)

    signal.signal(signal.SIGINT, _tratar_sinal)
    try:
        signal.signal(signal.SIGTERM, _tratar_sinal)
    except (AttributeError, ValueError):
        pass

    cfg = cu.carregar_config(args.config)
    cu.assert_escrita_permitida(cfg["_banco_path"], cfg["_raiz_path"])
    cu.assert_escrita_permitida(cfg["_derivados_path"], cfg["_raiz_path"])

    # ------------------------------------------------------------------
    # UM WORKER DE CADA VEZ — o lock do sistema operacional e a autoridade.
    # O `controle.pid` continua sendo o diagnostico legivel, mas NAO decide:
    # PID e reciclado depois de um reboot, e um pid morto que caiu em cima de
    # outro processo travaria a fila para sempre. O lock, ao contrario, e
    # devolvido pelo nucleo quando o processo morre — inclusive na queda de
    # energia. Detalhes em scripts/operacao.py.
    # ------------------------------------------------------------------
    caminho = op.caminho_lock(cfg)
    trava = op.TravaProcesso(caminho, rotulo="worker")
    if not trava.adquirir():
        dono = op.TravaProcesso.dono(caminho) or {}
        print(f"ja existe um worker rodando (pid {dono.get('pid', '?')} em "
              f"{dono.get('host', '?')}, desde {dono.get('desde', '?')}).")
        print(r"Use:  python scripts\cerebro.py status")
        return 3, "RODANDO"

    con = None
    try:
        con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
        if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name='controle'").fetchone():
            print("banco sem schema v2. Rode antes:  python scripts\\00_init_db.py")
            return 2, ""

        controle = cu.ler_controle(con)
        if controle["estado"] == "RODANDO":
            # Pegamos o lock, logo NAO ha worker vivo: este RODANDO ficou de
            # uma queda. Anotar e seguir — nunca travar a fila por causa dele.
            print(f"[aviso] o banco dizia RODANDO (pid {controle['pid']}), mas o lock "
                  f"estava livre: sessao anterior morreu sem encerrar. Seguindo.")
        if controle["comando"] in ("PAUSE", "STOP"):
            # Pausa e uma decisao da usuaria: nao se desfaz sozinha, nem quando
            # o Windows reinicia e a tarefa agendada sobe o worker de novo.
            print(f"o worker esta com comando {controle['comando']} pendente "
                  f"(motivo: {controle['motivo'] or '-'}).")
            print(r"Para voltar a processar:  python scripts\control.py resume")
            return 4, controle["estado"]

        # Janela de execucao: antes de abrir sessao, para nao poluir o
        # historico com sessoes de zero item.
        janela = op.JanelaExecucao.de_config(cfg)
        if getattr(args, "ignorar_janela", False):
            janela.ativa = False
        veredito = janela.checar()
        if not veredito.ok:
            cu.escrever_controle(con, estado=veredito.estado, motivo=veredito.motivo,
                                 pid=None, item_atual="", item_atual_id=None)
            print(veredito.motivo)
            return 0, veredito.estado

        # Indexar com versao de normalizador/corte diferente da carimbada
        # poria cada item novo direto em `v_reindexar`: recusar subir.
        if busca.v5_ativo(con):
            divergencias = busca.conferir_versoes(con)
            if divergencias:
                print("busca v5: o banco foi indexado com outra versao do codigo:")
                for linha in divergencias:
                    print(f"  {linha}")
                print(r"Rode:  python scripts\05_migrar_v5.py")
                return 5, controle["estado"]

        execucao_id = cu.abrir_execucao(con, "worker", "", vars(args))
        log = cu.Log(cfg["_logs_path"], "worker", con, execucao_id,
                     verboso=not args.quieto)
        worker = Worker(cfg, con, log, execucao_id, args)
        log.info(f"worker iniciado (pid {os.getpid()}) | lock {caminho.name} | "
                 f"orcamento: {worker.orcamento.descricao()} | "
                 f"janela: {worker.janela.descricao()}")

        t0 = time.perf_counter()
        try:
            estado = worker.rodar()
        finally:
            cu.fechar_execucao(con, execucao_id, "concluida", worker.contadores,
                               time.perf_counter() - t0)

        if not args.quieto:
            print()
            print(op.texto_relatorio_sessao(op.relatorio_sessao(
                con, execucao_id, worker.contadores, estado,
                cu.ler_controle(con)["motivo"] or "")))
            sobrando = len(list((cu.RAIZ_REPO / "tmp").glob("e7_*")))
            print(f"  temporarios em tmp/ {sobrando}")
            print(op.texto_metricas(op.medir(con, fonte=worker.fonte.nome)))
            print("  detalhe:  python scripts\\cerebro.py relatorio")
        log.fechar()
        # 6: o Agendador mostra "ultimo resultado 0x6" — um sinal visivel de
        # que ha algo para a Emmily fazer (renovar o segredo), ao contrario dos
        # encerramentos limpos (0). A proxima disparada tenta de novo sozinha.
        return (CODIGO_AGUARDANDO_CREDENCIAL if estado == "AGUARDANDO_CREDENCIAL"
                else 0), estado
    finally:
        if con is not None:
            con.close()
        trava.liberar()


def main() -> int:
    args = montar_parser().parse_args()
    codigo, _ = executar(args)
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
