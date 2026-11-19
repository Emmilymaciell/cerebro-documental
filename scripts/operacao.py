"""
operacao — a infraestrutura que deixa o Cerebro rodar sozinho, por dias.

Este modulo NAO processa arquivo. Ele responde as perguntas que um operador
faz sem nenhuma sessao de IA aberta:

    - ja tem worker rodando?            -> TravaProcesso
    - posso baixar mais?                -> Orcamento (disco, banda, derivados)
    - e hora de trabalhar?              -> JanelaExecucao
    - quanto falta, e quando acaba?     -> Metricas (tudo lido do banco)
    - o que deu errado, e de que tipo?  -> relatorio_erros

Tudo aqui e deterministico e local. Nenhuma chamada de IA, nenhum byte de
documento sai da maquina (REGRA 5 / DEC-039).

REGRA 7 — estado explicito, nunca inferencia: toda metrica sai de uma
consulta ao banco (`execucao`, `checkpoint`, `evento_arquivo`, `extracao`,
`v_fila_nuvem`, `v_progresso`). Nada e estimado "no ar", e todo numero vem
acompanhado da fonte e do tamanho da amostra que o produziu.

Chaves novas de configuracao (todas opcionais, lidas com `cfg.get(...)`;
`config/projeto.yaml` NAO precisa mudar — os padroes abaixo valem):

    worker.max_bytes_sessao_gb    0      teto de download por sessao (0 = sem teto)
    worker.max_itens_sessao       0      teto de itens por sessao   (0 = sem teto)
    worker.lock_arquivo           ""     caminho do lock (padrao: <banco>.lock)
    worker.concorrencia           1      workers simultaneos — ver cerebro.py
    worker.janela.ativa           false  ligar a janela de execucao
    worker.janela.inicio          "19:00"
    worker.janela.fim             "07:00"
    worker.janela.dias            []     0=segunda .. 6=domingo; [] = todos
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as _time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402

# ----------------------------------------------------------------------
# Estados do WORKER (coluna `controle.estado`).
# Nao confundir com `cu.ESTADOS`, que sao os estados de cada ARQUIVO.
# ----------------------------------------------------------------------

ESTADOS_WORKER = (
    "RODANDO",              # tem worker vivo puxando a fila
    "PAUSADO",              # parou a pedido (control.py pause / Ctrl+C)
    "PARADO",               # parou a pedido (control.py stop)
    "FILA_VAZIA",           # nao ha mais nada pronto para processar
    "PAUSA_SEGURA_DISCO",   # piso de disco livre ou teto de derivados
    "AGUARDANDO_REDE",      # falhas seguidas de nuvem/rede
    "AGUARDANDO_CREDENCIAL",  # o Graph recusou a credencial do app N vezes seguidas
    "LIMITE_BANDA",         # teto de bytes da sessao atingido
    "LIMITE_SESSAO",        # teto de itens da sessao atingido
    "FORA_DA_JANELA",       # fora do horario permitido de execucao
)
# Estados em que e seguro fechar o processo e desligar a maquina.
ESTADOS_WORKER_SEGUROS = tuple(e for e in ESTADOS_WORKER if e != "RODANDO")


# ======================================================================
# 1. TRAVA DE PROCESSO
# ======================================================================
#
# Por que o `controle.pid` nao basta (e por que ele CONTINUA existindo):
#
#   a) PID e reciclado. Depois de um reboot o Windows reatribui os numeros
#      baixos. Um `pid` deixado em `controle` por um worker morto na queda de
#      energia pode, na subida, pertencer a um servico qualquer. Ai
#      `cu.processo_vivo()` diz "vivo", o worker se recusa a rodar e a fila
#      trava PARA SEMPRE — sem ninguem rodando.
#
#   b) Ha corrida entre ler e escrever. Dois `worker.py run` disparados no
#      mesmo segundo (tarefa agendada + clique da Emmily) leem `PARADO` os
#      dois, e os dois entram.
#
#   c) O `pid` so e limpo por quem encerra graciosamente. Queda de energia
#      nao limpa nada.
#
# O lock de ARQUIVO do sistema operacional resolve os tres de uma vez:
#
#   - e atomico (nao ha janela entre testar e adquirir);
#   - e liberado pelo NUCLEO quando o processo morre, de qualquer jeito —
#     Ctrl+C, kill, BSOD, queda de energia. Nao existe "lock preso";
#   - nao depende de comparar PID nenhum.
#
# Entao: o LOCK e a autoridade, o `controle.pid` continua sendo o diagnostico
# legivel ("quem esta segurando?"). O dono grava as suas credenciais no
# proprio arquivo de lock, a partir do byte 1 — o byte 0 e o unico travado,
# justamente para que outro processo possa LER o dono sem conseguir entrar.

_OFFSET_DADOS = 1          # byte 0 = o cadeado; do byte 1 em diante = quem e o dono
_TAMANHO_DADOS = 512


def _travar(fd: int) -> None:
    """Trava 1 byte, sem bloquear. Levanta OSError se ja estiver travado."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _destravar(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class TravaProcesso:
    """Um worker de cada vez, garantido pelo sistema operacional.

        with TravaProcesso(caminho) as trava:
            if not trava.adquirida:
                ...  # outro worker esta rodando: trava.dono() diz quem
    """

    def __init__(self, caminho: Path | str, rotulo: str = "worker"):
        self.caminho = Path(caminho)
        self.rotulo = rotulo
        self._fd: int | None = None
        self.adquirida = False

    # -- ciclo de vida -------------------------------------------------
    def adquirir(self) -> bool:
        if self.adquirida:
            return True
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.caminho), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        try:
            if os.fstat(fd).st_size < _OFFSET_DADOS:
                os.write(fd, b"\0" * _OFFSET_DADOS)
            _travar(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        self.adquirida = True
        self._gravar_dono(livre=False)
        return True

    def liberar(self) -> None:
        if self._fd is None:
            return
        try:
            self._gravar_dono(livre=True)
        except OSError:
            pass
        try:
            _destravar(self._fd)
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self.adquirida = False

    def __enter__(self) -> "TravaProcesso":
        self.adquirir()
        return self

    def __exit__(self, *_) -> None:
        self.liberar()

    # -- quem e o dono -------------------------------------------------
    def _gravar_dono(self, livre: bool) -> None:
        dados = {
            "rotulo": self.rotulo,
            "livre": livre,
            "pid": os.getpid(),
            "host": os.environ.get("COMPUTERNAME", "") or os.environ.get("HOSTNAME", ""),
            "desde": cu.agora_iso(),
        }
        bruto = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        bruto = bruto[:_TAMANHO_DADOS].ljust(_TAMANHO_DADOS, b" ")
        os.lseek(self._fd, _OFFSET_DADOS, os.SEEK_SET)
        os.write(self._fd, bruto)

    @staticmethod
    def dono(caminho: Path | str) -> dict | None:
        """Le quem gravou o lock. Funciona mesmo com o lock tomado: o byte
        travado e o 0, e os dados moram do byte 1 em diante."""
        caminho = Path(caminho)
        if not caminho.exists():
            return None
        try:
            with open(caminho, "rb") as fh:
                fh.seek(_OFFSET_DADOS)
                bruto = fh.read(_TAMANHO_DADOS).decode("utf-8", "replace").strip()
            return json.loads(bruto) if bruto else None
        except (OSError, ValueError):
            return None

    @classmethod
    def ocupada(cls, caminho: Path | str) -> bool:
        """True se HOJE, agora, alguem esta segurando o lock.

        Nao olha PID nenhum: tenta tomar. Se conseguiu, devolve na hora.
        """
        sonda = cls(caminho, rotulo="sonda")
        if sonda.adquirir():
            sonda.liberar()
            return False
        return True


def caminho_lock(cfg: dict) -> Path:
    """Onde mora o lock. Nunca dentro do SharePoint (REGRA 1)."""
    w = cfg.get("worker") or {}
    bruto = w.get("lock_arquivo") or ""
    destino = Path(bruto) if bruto else Path(str(cfg["_banco_path"]) + ".lock")
    cu.assert_escrita_permitida(destino, cfg["_raiz_path"])
    return destino


# ======================================================================
# 2. ORCAMENTO — disco, banda, derivados, itens
# ======================================================================

@dataclass
class Veredito:
    ok: bool
    estado: str = ""
    motivo: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Orcamento:
    """As travas que fazem o worker parar sozinho, com estado explicito."""

    piso_disco_gb: float = 25.0
    teto_derivados_gb: float = 60.0
    teto_bytes_sessao: int = 0      # 0 = sem teto
    teto_itens_sessao: int = 0      # 0 = sem teto
    ancora: str = "C:\\"

    @classmethod
    def de_config(cls, cfg: dict, args=None) -> "Orcamento":
        nuvem = cfg.get("nuvem") or {}
        w = cfg.get("worker") or {}
        teto_gb = float(w.get("max_bytes_sessao_gb", 0) or 0)
        teto_itens = int(w.get("max_itens_sessao", 0) or 0)
        # A linha de comando ganha da config: e assim que a Emmily faz uma
        # rodada curta sem editar arquivo nenhum.
        if args is not None:
            if getattr(args, "max_gb_sessao", None):
                teto_gb = float(args.max_gb_sessao)
            if getattr(args, "limite", 0):
                teto_itens = int(args.limite)
        return cls(
            piso_disco_gb=float(nuvem.get("espaco_livre_minimo_gb", 25)),
            teto_derivados_gb=float(w.get("derivados_max_gb", 60)),
            teto_bytes_sessao=int(teto_gb * 1024 ** 3),
            teto_itens_sessao=teto_itens,
            ancora=str(cfg["_banco_path"].anchor or "C:\\"),
        )

    # -- as quatro perguntas ------------------------------------------
    def checar(self, con=None, bytes_sessao: int = 0, itens_sessao: int = 0) -> Veredito:
        livre = cu.espaco_livre_gb(self.ancora)
        if livre < self.piso_disco_gb:
            return Veredito(False, "PAUSA_SEGURA_DISCO",
                            f"espaco livre {livre:.1f} GB abaixo do minimo "
                            f"{self.piso_disco_gb} GB")
        if con is not None and self.teto_derivados_gb > 0:
            derivados_gb = self.derivados_gb(con)
            if derivados_gb > self.teto_derivados_gb:
                return Veredito(False, "PAUSA_SEGURA_DISCO",
                                f"derivados com {derivados_gb:.1f} GB, acima do teto "
                                f"{self.teto_derivados_gb} GB")
        if self.teto_bytes_sessao and bytes_sessao >= self.teto_bytes_sessao:
            return Veredito(False, "LIMITE_BANDA",
                            f"teto de banda da sessao atingido: "
                            f"{bytes_sessao/1024**3:.2f} GB de "
                            f"{self.teto_bytes_sessao/1024**3:.2f} GB")
        if self.teto_itens_sessao and itens_sessao >= self.teto_itens_sessao:
            return Veredito(False, "LIMITE_SESSAO",
                            f"teto de {self.teto_itens_sessao} itens da sessao atingido")
        return Veredito(True)

    @staticmethod
    def derivados_gb(con) -> float:
        bruto = con.execute(
            "SELECT COALESCE(SUM(bytes_texto),0) FROM extracao").fetchone()[0] or 0
        return bruto / 1024 ** 3

    def descricao(self) -> str:
        banda = (f"{self.teto_bytes_sessao/1024**3:.1f} GB"
                 if self.teto_bytes_sessao else "sem teto")
        itens = str(self.teto_itens_sessao) if self.teto_itens_sessao else "sem teto"
        return (f"disco livre >= {self.piso_disco_gb:.0f} GB · "
                f"derivados <= {self.teto_derivados_gb:.0f} GB · "
                f"banda/sessao {banda} · itens/sessao {itens}")


# ======================================================================
# 3. JANELA DE EXECUCAO
# ======================================================================

_HORA = re.compile(r"^\s*(\d{1,2})\s*[:h]\s*(\d{1,2})?\s*$")


def _hora(texto, padrao: _time) -> _time:
    if isinstance(texto, _time):
        return texto
    m = _HORA.match(str(texto or ""))
    if not m:
        return padrao
    return _time(int(m.group(1)) % 24, int(m.group(2) or 0) % 60)


@dataclass
class JanelaExecucao:
    """'So rode entre 19h e 7h, de segunda a sexta.'

    A banda e dividida com o escritorio: fora da janela o worker nao fica
    dormindo segurando o lock — ele ENCERRA limpo, com estado
    `FORA_DA_JANELA`, e a tarefa agendada o traz de volta depois.
    """

    ativa: bool = False
    inicio: _time = field(default_factory=lambda: _time(19, 0))
    fim: _time = field(default_factory=lambda: _time(7, 0))
    dias: tuple = ()          # 0=segunda .. 6=domingo; vazio = todos

    @classmethod
    def de_config(cls, cfg: dict) -> "JanelaExecucao":
        bruto = ((cfg.get("worker") or {}).get("janela") or {})
        dias = tuple(sorted({int(d) % 7 for d in (bruto.get("dias") or [])}))
        return cls(ativa=bool(bruto.get("ativa", False)),
                   inicio=_hora(bruto.get("inicio"), _time(19, 0)),
                   fim=_hora(bruto.get("fim"), _time(7, 0)),
                   dias=dias)

    # -- a pergunta ----------------------------------------------------
    def dentro(self, agora: datetime | None = None) -> bool:
        if not self.ativa:
            return True
        agora = agora or datetime.now()
        t = agora.time()
        if self.inicio == self.fim:            # janela de 24 h
            return self._dia_ok(agora.weekday())
        if self.inicio < self.fim:             # 08:00 -> 18:00, no mesmo dia
            return self._dia_ok(agora.weekday()) and self.inicio <= t < self.fim
        # 19:00 -> 07:00, atravessa a meia-noite. O dia que conta e o dia em
        # que a janela ABRIU: as 2h de sabado ainda e a janela da sexta.
        if t >= self.inicio:
            return self._dia_ok(agora.weekday())
        if t < self.fim:
            return self._dia_ok((agora.weekday() - 1) % 7)
        return False

    def _dia_ok(self, dia: int) -> bool:
        return (not self.dias) or (dia in self.dias)

    def proxima_abertura(self, agora: datetime | None = None) -> datetime | None:
        """Quando a janela abre de novo. Deterministico: varre hora a hora."""
        if not self.ativa:
            return None
        agora = agora or datetime.now()
        candidato = agora.replace(minute=0, second=0, microsecond=0)
        for _ in range(24 * 8):
            candidato += timedelta(hours=1)
            if self.dentro(candidato):
                return candidato
        return None

    def descricao(self) -> str:
        if not self.ativa:
            return "sempre (janela desligada)"
        nomes = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")
        dias = ", ".join(nomes[d] for d in self.dias) if self.dias else "todos os dias"
        return (f"{self.inicio.strftime('%H:%M')}–{self.fim.strftime('%H:%M')} · {dias}")

    def checar(self, agora: datetime | None = None) -> Veredito:
        if self.dentro(agora):
            return Veredito(True)
        proxima = self.proxima_abertura(agora)
        quando = proxima.strftime("%d/%m %H:%M") if proxima else "?"
        return Veredito(False, "FORA_DA_JANELA",
                        f"fora da janela de execucao ({self.descricao()}); "
                        f"volta em {quando}")


# ======================================================================
# 4. METRICAS E ETA — tudo lido do banco (REGRA 7)
# ======================================================================

EVENTOS_DE_TRABALHO = ("processado", "coberto_por_conteudo", "ausente_na_nuvem")

# A marca da extracao adiada pelo teto de tamanho (T-10): o item NAO foi
# baixado. Fonte unica — o worker grava com ela, e a medida de banda abaixo a
# exclui (senao 300 MB "processados" em 20 ms inflariam o bytes/s).
VERSAO_ADIADO_TETO = "adiado/teto_tamanho"


def _para_datetime(texto) -> datetime | None:
    """Aceita os dois formatos do projeto: ISO com fuso e 'YYYY-MM-DD HH:MM:SS'."""
    if not texto:
        return None
    bruto = str(texto).strip().replace(" ", "T", 1)
    try:
        valor = datetime.fromisoformat(bruto)
    except ValueError:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


GAP_OCIOSO_S = 15 * 60      # acima disto, o worker estava DESLIGADO, nao lento


def _ritmo(carimbos: list, pesos: list | None = None, grupos: list | None = None,
           gap_s: float = GAP_OCIOSO_S) -> tuple[float, float, int]:
    """(segundos trabalhados, quantidade no periodo, tamanho da amostra).

    O detalhe que muda tudo: entre dois itens pode haver uma NOITE INTEIRA de
    worker desligado. Dividir 33 itens pelo intervalo do primeiro ao ultimo
    daria "0,02 item/min" e um ETA de meses — numero falso.

    Duas defesas, as duas deterministicas:

      1. `grupos[i]` (o `execucao_id`) impede somar o buraco ENTRE sessoes:
         so contam intervalos dentro da mesma sessao;
      2. `gap_s` descarta o intervalo grande DENTRO de uma sessao — o worker
         dormindo `espera_fila_vazia_s` ou esperando a rede nao e lentidao.

    O que sobra e "enquanto trabalha, anda a tanto" — a pergunta certa para
    o ETA de um worker que roda por janelas. `pesos[i]` e o que o item i
    entrega (bytes, por exemplo); sem pesos, cada item vale 1.
    """
    itens = []
    for i, bruto in enumerate(carimbos):
        quando = _para_datetime(bruto)
        if quando is None:
            continue
        itens.append((grupos[i] if grupos else 0, quando,
                      pesos[i] if pesos else 1.0))
    itens.sort(key=lambda x: (str(x[0]), x[1]))
    if len(itens) < 2:
        return 0.0, 0.0, len(itens)
    segundos = 0.0
    total = 0.0
    for (g0, t0, _), (g1, t1, peso) in zip(itens, itens[1:]):
        if g0 != g1:
            continue
        delta = (t1 - t0).total_seconds()
        if 0 <= delta <= gap_s:
            segundos += delta
            total += peso
    return segundos, total, len(itens)


def medir(con, *, fonte: str = "graph", amostra: int = 500) -> dict:
    """Foto do progresso, com a fonte de cada numero.

    Nao recebe nada do worker em memoria: tudo sai de consulta. Por isso
    funciona com o worker desligado, em outra maquina, dias depois.
    """
    vista_fila = "v_fila_nuvem" if fonte == "graph" else "v_fila"
    m: dict = {"fonte": fonte, "vista_fila": vista_fila, "medido_em": cu.agora_iso()}

    m["fila"] = con.execute(f"SELECT COUNT(*) FROM {vista_fila}").fetchone()[0]
    m["fila_bytes"] = con.execute(
        f"SELECT COALESCE(SUM(tamanho_bytes),0) FROM {vista_fila}").fetchone()[0] or 0
    m["estados"] = {linha["estado"]: linha["arquivos"]
                    for linha in con.execute("SELECT * FROM v_progresso")}
    m["total"] = con.execute(
        "SELECT COUNT(*) FROM arquivo WHERE presente=1").fetchone()[0]
    m["concluidos"] = (m["estados"].get("COMPLETED", 0)
                       + m["estados"].get("DUPLICATE", 0)
                       + m["estados"].get("PARTIAL", 0)
                       + m["estados"].get("OCR_REQUIRED", 0))

    # --- ritmo recente: os ultimos N eventos de trabalho DO WORKER -----
    # O `JOIN execucao ... etapa='worker'` nao e decoracao: o resync do E8
    # gravou 5.937 `ausente_na_nuvem` numa transacao so, todos com o mesmo
    # segundo. Sem o filtro, a amostra viraria "5.937 itens em 0 segundo" e o
    # ritmo desabaria para zero. Ritmo do worker se mede com trabalho do
    # worker.
    marcas = ",".join("?" * len(EVENTOS_DE_TRABALHO))
    eventos = con.execute(
        f"SELECT ev.em, ev.execucao_id FROM evento_arquivo ev "
        f"JOIN execucao ex ON ex.id = ev.execucao_id "
        f"WHERE ev.evento IN ({marcas}) AND ex.etapa = 'worker' "
        f"ORDER BY ev.id DESC LIMIT ?", (*EVENTOS_DE_TRABALHO, amostra)).fetchall()
    span, itens, n = _ritmo([e["em"] for e in eventos], None,
                            [e["execucao_id"] for e in eventos])
    m["amostra_recente"] = n
    m["segundos_recente"] = round(span, 1)
    m["itens_recente"] = itens
    m["itens_min_recente"] = round(itens / span * 60, 2) if span > 0 else 0.0

    # --- bytes/s recente: pelas extracoes gravadas ---------------------
    linhas = con.execute(
        "SELECT e.executado_em, e.execucao_id, COALESCE(a.tamanho_bytes,0) AS bytes "
        "FROM extracao e JOIN arquivo a ON a.id=e.arquivo_id "
        "WHERE e.versao <> ? "
        "ORDER BY e.executado_em DESC LIMIT ?", (VERSAO_ADIADO_TETO, amostra)).fetchall()
    span_b, bytes_janela, n_b = _ritmo([linha["executado_em"] for linha in linhas],
                                       [linha["bytes"] for linha in linhas],
                                       [linha["execucao_id"] for linha in linhas])
    m["amostra_bytes"] = n_b
    m["bytes_s_recente"] = round(bytes_janela / span_b, 1) if span_b > 0 else 0.0

    # --- historico: todas as sessoes ja fechadas do worker -------------
    hist = con.execute(
        "SELECT COALESCE(SUM(arquivos_vistos),0) AS itens, "
        "COALESCE(SUM(bytes_processados),0) AS bytes, "
        "COALESCE(SUM(duracao_s),0) AS segundos, COUNT(*) AS sessoes "
        "FROM execucao WHERE etapa='worker' AND duracao_s > 0 "
        "AND arquivos_vistos > 0").fetchone()
    m["sessoes_historicas"] = hist["sessoes"]
    m["itens_min_historico"] = (round(hist["itens"] / hist["segundos"] * 60, 2)
                                if hist["segundos"] else 0.0)
    m["bytes_s_historico"] = (round(hist["bytes"] / hist["segundos"], 1)
                              if hist["segundos"] else 0.0)

    # --- drenagem da fila: dois checkpoints seguidos -------------------
    # E a medida mais honesta para o ETA, porque conta o que de fato saiu da
    # fila (inclusive duplicata e 404, que nao viram extracao).
    # Os dois checkpoints TEM de ser da MESMA execucao e proximos no tempo:
    # entre a ultima sessao e a anterior o inventario pode ter mudado a fila
    # inteira (foi o que o resync do E8 fez), e a diferenca de `pendentes`
    # nao teria nada a ver com velocidade de processamento.
    m["drenagem_min"] = 0.0
    cps = con.execute(
        "SELECT em, pendentes, execucao_id FROM checkpoint "
        "WHERE pendentes IS NOT NULL AND execucao_id = "
        "  (SELECT execucao_id FROM checkpoint WHERE execucao_id IS NOT NULL "
        "   ORDER BY id DESC LIMIT 1) "
        "ORDER BY id DESC LIMIT 2").fetchall()
    if len(cps) == 2:
        novo, velho = _para_datetime(cps[0]["em"]), _para_datetime(cps[1]["em"])
        if novo and velho:
            segundos = (novo - velho).total_seconds()
            saiu = (cps[1]["pendentes"] or 0) - (cps[0]["pendentes"] or 0)
            if 0 < segundos <= GAP_OCIOSO_S and saiu > 0:
                m["drenagem_min"] = round(saiu / segundos * 60, 2)

    # --- ETA: a melhor medida disponivel, e ela diz de onde veio -------
    if m["itens_recente"] >= 10 and m["itens_min_recente"] > 0:
        ritmo, origem = (m["itens_min_recente"],
                         f"{m['itens_recente']:.0f} itens em "
                         f"{m['segundos_recente']/60:.1f} min de worker ligado")
    elif m["drenagem_min"] > 0:
        ritmo, origem = m["drenagem_min"], "drenagem entre os 2 ultimos checkpoints"
    elif m["itens_min_historico"] > 0:
        ritmo, origem = m["itens_min_historico"], f"{m['sessoes_historicas']} sessoes anteriores"
    else:
        ritmo, origem = 0.0, "sem medicao — nada processado ainda"
    m["itens_min"] = ritmo
    m["origem_ritmo"] = origem
    m["eta_min"] = round(m["fila"] / ritmo, 1) if ritmo > 0 else None
    m["termino_previsto"] = (
        (datetime.now() + timedelta(minutes=m["eta_min"])).strftime("%Y-%m-%d %H:%M")
        if m["eta_min"] is not None else None)

    controle = cu.ler_controle(con)
    m["worker_estado"] = controle["estado"]
    m["worker_comando"] = controle["comando"]
    m["worker_motivo"] = controle["motivo"] or ""
    m["worker_pid"] = controle["pid"]
    m["worker_heartbeat"] = controle["heartbeat"]
    m["worker_item"] = controle["item_atual"]
    m["espaco_livre_gb"] = round(cu.espaco_livre_gb("C:\\"), 1)
    m["derivados_gb"] = round(Orcamento.derivados_gb(con), 2)
    return m


def formatar_duracao(minutos: float | None) -> str:
    if minutos is None:
        return "sem medicao"
    if minutos < 90:
        return f"{minutos:.0f} min"
    horas = minutos / 60
    if horas < 48:
        return f"{horas:.1f} h"
    return f"{horas/24:.1f} dias"


def formatar_bytes(n: float) -> str:
    n = float(n or 0)
    for unidade in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unidade == "TB":
            return f"{n:.1f} {unidade}"
        n /= 1024
    return f"{n:.1f} TB"


def texto_metricas(m: dict) -> str:
    linhas = [
        "=" * 66,
        f"CEREBRO — {m['worker_estado']}  (comando pendente: {m['worker_comando']})",
        "=" * 66,
    ]
    if m.get("worker_motivo") and m["worker_estado"] != "RODANDO":
        linhas.append(f"  motivo                {str(m['worker_motivo'])[:200]}")
    if m["worker_estado"] == "AGUARDANDO_CREDENCIAL":
        # B4: o unico estado que pede gente. Dizer o que fazer, aqui mesmo.
        linhas += [
            "  ACAO: o Graph recusou a credencial do aplicativo. Renovar o segredo",
            "        (GRAPH_CLIENT_SECRET no .env) ou a permissao do app no site e",
            "        rodar `python scripts\\cerebro.py processar`. Nenhuma tentativa",
            "        de documento foi gasta.",
        ]
    linhas += [
        f"  arquivos no catalogo  {m['total']:,}".replace(",", "."),
        f"  ja cobertos           {m['concluidos']:,}".replace(",", "."),
        f"  fila                  {m['fila']:,}".replace(",", ".")
        + f"   ({formatar_bytes(m['fila_bytes'])})",
        "",
        f"  ritmo                 {m['itens_min']:.1f} itens/min   "
        f"[{m['origem_ritmo']}]",
        f"  banda                 {formatar_bytes(m['bytes_s_recente'])}/s recente · "
        f"{formatar_bytes(m['bytes_s_historico'])}/s historico",
        f"  falta                 {formatar_duracao(m['eta_min'])}"
        + (f"  ->  {m['termino_previsto']}" if m["termino_previsto"] else ""),
        "",
        f"  espaco livre          {m['espaco_livre_gb']:.1f} GB",
        f"  derivados             {m['derivados_gb']:.2f} GB",
        f"  ultimo heartbeat      {m['worker_heartbeat'] or '-'}",
    ]
    if m["worker_item"]:
        linhas.append(f"  item atual            {str(m['worker_item'])[:60]}")
    linhas.append("")
    for estado, quantos in sorted(m["estados"].items(), key=lambda x: -x[1]):
        linhas.append(f"    {estado:<20} {quantos:>10,}".replace(",", "."))
    linhas.append("=" * 66)
    return "\n".join(linhas)


# ======================================================================
# 5. RELATORIOS
# ======================================================================

# Classificacao de erro por REGRA, nao por IA (REGRA 5). Ordem importa:
# a primeira que casar vence.
_CATEGORIAS_ERRO = (
    # B3: pilula envenenada isolada por `Worker.recuperar_orfaos`
    ("derrubou o worker",         r"derrubou o worker"),
    ("quickXorHash divergente",   r"quickxorhash divergente"),
    ("tamanho divergente",        r"tamanho divergente"),
    ("nuvem: item nao existe",    r"\b(404|410)\b|itemnotfound|resourcenotfound"),
    ("nuvem: indisponivel",       r"\b(429|500|502|503|504)\b|servicenotavailable|"
                                  r"activitylimitreached|throttl"),
    ("nuvem: autenticacao",       r"\b(401|403)\b|unauthenticated|accessdenied"),
    ("rede: tempo esgotado",      r"timeout|timederror|tempo limite|passou de \d+s"),
    ("rede: conexao",             r"connectionerror|connection|conexao|rede"),
    ("disco",                     r"erro de disco|no space|disco cheio|oserror"),
    ("arquivo protegido",         r"protegid|senha|password|encrypted"),
    ("arquivo corrompido",        r"corromp|cannot open|damaged|nao e um|not a "),
    ("acima do teto de tamanho",  r"acima d[eo] (?:teto|\d)"),
    ("extrator",                  r"^[a-z_]*error:|extrac|extrator"),
)


def categoria_erro(texto: str | None) -> str:
    bruto = cu.sem_acento(str(texto or "")).lower()
    if not bruto.strip():
        return "sem mensagem"
    for rotulo, padrao in _CATEGORIAS_ERRO:
        if re.search(padrao, bruto):
            return rotulo
    return "outro"


def relatorio_erros(con, limite_exemplos: int = 2) -> dict:
    """Erros agrupados por CATEGORIA, com exemplo e contagem.

    A leitura correta e por categoria: 3.000 itens com '404 itemNotFound' sao
    UM fato operacional (uma biblioteca sumiu), nao 3.000 problemas.
    """
    linhas = con.execute(
        "SELECT id, caminho_rel, estado, tentativas, ultimo_erro "
        "FROM arquivo WHERE estado IN ('ERROR_RETRYABLE','ERROR_FINAL') "
        "ORDER BY id").fetchall()
    grupos: dict = {}
    for linha in linhas:
        chave = categoria_erro(linha["ultimo_erro"])
        g = grupos.setdefault(chave, {"categoria": chave, "total": 0,
                                      "ERROR_RETRYABLE": 0, "ERROR_FINAL": 0,
                                      "exemplos": []})
        g["total"] += 1
        g[linha["estado"]] += 1
        if len(g["exemplos"]) < limite_exemplos:
            g["exemplos"].append({"id": linha["id"], "caminho": linha["caminho_rel"],
                                  "erro": (linha["ultimo_erro"] or "")[:160],
                                  "tentativas": linha["tentativas"]})
    return {"total": len(linhas),
            "grupos": sorted(grupos.values(), key=lambda g: -g["total"])}


def texto_relatorio_erros(rel: dict) -> str:
    if not rel["total"]:
        return "Nenhum arquivo em ERROR_RETRYABLE ou ERROR_FINAL."
    linhas = ["=" * 66, f"ERROS POR CATEGORIA — {rel['total']} arquivo(s)", "=" * 66]
    for g in rel["grupos"]:
        linhas.append(f"\n  {g['categoria']}  —  {g['total']} "
                      f"(retry {g['ERROR_RETRYABLE']} · final {g['ERROR_FINAL']})")
        for ex in g["exemplos"]:
            linhas.append(f"      #{ex['id']} {str(ex['caminho'])[:56]}")
            linhas.append(f"         {ex['erro']}")
    linhas.append("=" * 66)
    return "\n".join(linhas)


def relatorio_sessao(con, execucao_id: int | None = None, contadores: dict | None = None,
                     estado: str = "", motivo: str = "") -> dict:
    """O que aconteceu nesta sessao. Le a `execucao` e os `checkpoint` dela."""
    if execucao_id is None:
        linha = con.execute("SELECT id FROM execucao WHERE etapa='worker' "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        execucao_id = int(linha["id"]) if linha else None
    if execucao_id is None:
        return {"execucao_id": None, "vazio": True}

    ex = con.execute("SELECT * FROM execucao WHERE id=?", (execucao_id,)).fetchone()
    cps = con.execute("SELECT * FROM checkpoint WHERE execucao_id=? ORDER BY id",
                      (execucao_id,)).fetchall()
    eventos = {linha[0]: linha[1] for linha in con.execute(
        "SELECT evento, COUNT(*) FROM evento_arquivo WHERE execucao_id=? "
        "GROUP BY evento", (execucao_id,))}
    duracao = float(ex["duracao_s"] or 0)
    itens = int(ex["arquivos_vistos"] or 0)
    return {
        "execucao_id": execucao_id,
        "vazio": False,
        "iniciada_em": ex["iniciada_em"],
        "encerrada_em": ex["encerrada_em"],
        "duracao_s": duracao,
        "itens": itens,
        "erros": int(ex["erros"] or 0),
        "bytes": int(ex["bytes_processados"] or 0),
        "itens_min": round(itens / duracao * 60, 2) if duracao > 0 else 0.0,
        "bytes_s": round(int(ex["bytes_processados"] or 0) / duracao, 1) if duracao > 0 else 0.0,
        "checkpoints": len(cps),
        "estado_final": estado or (cps[-1]["estado_worker"] if cps else ""),
        "motivo": motivo or (cps[-1]["observacao"] if cps else ""),
        "eventos": eventos,
        "contadores": contadores or {},
    }


def texto_relatorio_sessao(rel: dict) -> str:
    if rel.get("vazio"):
        return "Nenhuma sessao de worker registrada ainda."
    c = rel.get("contadores") or {}
    linhas = [
        "=" * 66,
        f"SESSAO #{rel['execucao_id']} — {rel['estado_final'] or '-'}",
        "=" * 66,
        f"  motivo              {rel['motivo'] or '-'}",
        f"  inicio              {rel['iniciada_em']}",
        f"  fim                 {rel['encerrada_em'] or '(em andamento)'}",
        f"  duracao             {formatar_duracao(rel['duracao_s']/60)}",
        f"  itens               {rel['itens']}",
        f"  erros               {rel['erros']}",
        f"  ritmo medido        {rel['itens_min']:.1f} itens/min · "
        f"{formatar_bytes(rel['bytes_s'])}/s",
        f"  checkpoints         {rel['checkpoints']}",
    ]
    if c:
        linhas += [
            f"    concluidos        {c.get('concluidos', 0)}",
            f"    duplicatas        {c.get('duplicatas', 0)} "
            f"({formatar_bytes(c.get('bytes_economizados', 0))} nao baixados)",
            f"    OCR necessario    {c.get('ocr', 0)}",
            f"    parciais          {c.get('parciais', 0)}"
            f" (adiados pelo teto, sem download: {c.get('adiados_tamanho', 0)})",
            f"    ausentes (404)    {c.get('ausentes', 0)}",
            f"    baixado           {formatar_bytes(c.get('bytes_baixados', 0))}",
        ]
    if rel["eventos"]:
        linhas.append("  eventos:")
        for evento, quantos in sorted(rel["eventos"].items(), key=lambda x: -x[1]):
            linhas.append(f"    {evento:<24} {quantos}")
    linhas.append("=" * 66)
    return "\n".join(linhas)


# ======================================================================
# Cobertura — quanto do acervo ja tem resposta, por categoria e prioridade
# ======================================================================
#
# Cada estado cai em UMA coluna (REGRA 7: estado explicito, nada inferido).
# `UNSUPPORTED` e decisao, nao pendencia (REGRA 6): sai do denominador do
# percentual, mas aparece na tabela. MISSING nao entra (presente=0).
GRUPOS_COBERTURA = {
    "extraido": ("COMPLETED", "DUPLICATE"),
    "parcial": ("PARTIAL",),
    "ocr": ("OCR_REQUIRED",),
    "fila": cu.ESTADOS_NA_FILA + cu.ESTADOS_ORFAOS + ("INVENTORIED",),
    "erro": ("ERROR_FINAL",),
    "sem_extrator": ("UNSUPPORTED",),
    "ignorado": ("SKIPPED",),
    # MISSING com presente=1 e incoerencia: aparece, nunca some (REGRA 6).
    "ausente": ("MISSING",),
}


def cobertura(con, por: str = "categoria") -> list[dict]:
    """Linhas {chave, total, <grupo>..., pct} para os arquivos presentes.

    `pct` = (extraido + parcial + ocr) / (total - sem_extrator - ignorado):
    quanto do que o projeto DECIDIU ler ja foi lido (ou classificado como
    precisando de OCR). Um estado fora dos grupos levanta erro — estado novo
    tem de ser classificado aqui de proposito.
    """
    if por not in ("categoria", "prioridade"):
        raise ValueError(f"cobertura por '{por}'?")
    grupo_de = {e: g for g, estados in GRUPOS_COBERTURA.items() for e in estados}
    linhas: dict = {}
    for r in con.execute(f"SELECT {por} AS chave, estado, COUNT(*) AS n FROM arquivo "
                         f"WHERE presente=1 GROUP BY {por}, estado"):
        grupo = grupo_de.get(r["estado"])
        if grupo is None:
            raise ValueError(f"estado {r['estado']} sem grupo de cobertura")
        chave = r["chave"] if r["chave"] is not None else "-"
        linha = linhas.setdefault(chave, {"chave": chave, "total": 0,
                                          **{g: 0 for g in GRUPOS_COBERTURA}})
        linha[grupo] += r["n"]
        linha["total"] += r["n"]
    saida = []
    total = {"chave": "TOTAL", "total": 0, **{g: 0 for g in GRUPOS_COBERTURA}}
    for chave in sorted(linhas, key=str):
        saida.append(linhas[chave])
        for k in ["total", *GRUPOS_COBERTURA]:
            total[k] += linhas[chave][k]
    saida.append(total)
    for linha in saida:
        alvo = (linha["total"] - linha["sem_extrator"] - linha["ignorado"]
                - linha["ausente"])
        lidos = linha["extraido"] + linha["parcial"] + linha["ocr"]
        linha["pct"] = (100.0 * lidos / alvo) if alvo else None
    return saida


def texto_cobertura(linhas: list[dict], por: str = "categoria") -> str:
    def n(v):
        return f"{int(v):,}".replace(",", ".")
    cab = (f"  {por:<12}{'total':>9}{'extraido':>10}{'parcial':>9}{'ocr':>7}"
           f"{'fila':>9}{'erro':>7}{'s/extr':>9}{'ignor':>7}{'ausen':>7}{'lido %':>8}")
    out = ["=" * 94, f"COBERTURA POR {por.upper()}  (arquivos presentes; s/extr e "
           f"decisao, fora do %)", "=" * 94, cab]
    for l in linhas:
        if l["chave"] == "TOTAL":
            out.append("  " + "-" * 92)
        pct = "-" if l["pct"] is None else f"{l['pct']:.1f}"
        out.append(f"  {str(l['chave']):<12}{n(l['total']):>9}{n(l['extraido']):>10}"
                   f"{n(l['parcial']):>9}{n(l['ocr']):>7}{n(l['fila']):>9}"
                   f"{n(l['erro']):>7}{n(l['sem_extrator']):>9}{n(l['ignorado']):>7}"
                   f"{n(l['ausente']):>7}"
                   f"{pct:>8}")
    out.append("=" * 94)
    return "\n".join(out)


# ======================================================================
# Linha de comando (diagnostico rapido; o comando de producao e cerebro.py)
# ======================================================================

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Infraestrutura de operacao autonoma")
    ap.add_argument("acao", choices=["metricas", "erros", "sessao", "trava", "janela"])
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    if args.acao == "janela":
        janela = JanelaExecucao.de_config(cfg)
        print(f"janela: {janela.descricao()}")
        print(f"agora dentro? {'sim' if janela.dentro() else 'nao'}")
        proxima = janela.proxima_abertura()
        if proxima:
            print(f"proxima abertura: {proxima:%d/%m %H:%M}")
        return 0
    if args.acao == "trava":
        caminho = caminho_lock(cfg)
        print(f"lock: {caminho}")
        print(f"ocupada: {'sim' if TravaProcesso.ocupada(caminho) else 'nao'}")
        print(f"ultimo dono: {TravaProcesso.dono(caminho)}")
        return 0

    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    try:
        if args.acao == "metricas":
            print(texto_metricas(medir(con, fonte=cfg.get("fonte", "graph"))))
        elif args.acao == "erros":
            print(texto_relatorio_erros(relatorio_erros(con)))
        else:
            print(texto_relatorio_sessao(relatorio_sessao(con)))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
