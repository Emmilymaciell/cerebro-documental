"""
01_scan_inventario — varredura SOMENTE LEITURA de TODA a arvore.

O que faz:
  - percorre a raiz inteira (ou os escopos pedidos);
  - grava/atualiza uma linha por arquivo na tabela `arquivo`;
  - estados: novo -> INVENTORIED | SKIPPED (temporario)
             mudou -> CHANGED (volta para a fila)
             sumiu -> MISSING (linha preservada, nunca apagada)
  - registra eventos (novo, alterado, sumiu, hidratado, desidratado);
  - marca progresso por diretorio, para retomar de onde parou;
  - produz log JSONL em logs/ e linhas na tabela `log`.

O que NAO faz — de proposito:
  - nao abre nenhum arquivo (nao calcula hash, nao extrai texto);
  - por isso NAO baixa nada da nuvem;
  - nao escreve nada dentro da raiz do SharePoint;
  - nao decide prioridade (isso e do 02_classificar.py).

Uso:
    python scripts\\01_scan_inventario.py                 # arvore inteira (retomando)
    python scripts\\01_scan_inventario.py --limite 5000   # benchmark
    python scripts\\01_scan_inventario.py --escopo "Programa - 4. Produtos"
    python scripts\\01_scan_inventario.py --reiniciar     # ignora progresso anterior
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cerebro_util as cu  # noqa: E402


class Scanner:
    def __init__(self, cfg: dict, con, log: cu.Log, execucao_id: int, args):
        self.cfg = cfg
        self.con = con
        self.log = log
        self.execucao_id = execucao_id
        self.args = args
        self.raiz = cfg["_raiz_path"]
        self.categorias = cfg.get("categorias") or {}
        self.suportadas = set(cfg.get("extratores_ativos") or [])
        self.pastas_ignoradas = {p.lower() for p in (cfg.get("pastas_ignoradas") or [])}
        self.padroes_temp = cfg.get("arquivos_temporarios") or []
        self.commit_a_cada = int((cfg.get("limites") or {}).get("commit_a_cada", 1000))
        self.contadores = {"diretorios": 0, "arquivos": 0, "novos": 0,
                           "alterados": 0, "sumidos": 0, "erros": 0, "ignorados": 0,
                           "bytes": 0}
        self.desde_commit = 0
        self.reparse_vistos: set[str] = set()
        self.concluidos = self._carregar_concluidos()
        self.t0 = time.perf_counter()

    # -- retomada -------------------------------------------------------
    def _carregar_concluidos(self) -> set[str]:
        linhas = self.con.execute(
            "SELECT pasta_rel FROM progresso_diretorio WHERE concluido_em IS NOT NULL"
        ).fetchall()
        return {linha[0] for linha in linhas}

    # -- utilitarios ----------------------------------------------------
    def _rel(self, caminho_abs: str) -> str:
        return os.path.relpath(caminho_abs, str(self.raiz))

    def _existentes(self, pasta_rel: str) -> dict:
        linhas = self.con.execute(
            "SELECT id, caminho_rel, tamanho_bytes, mtime_utc, online, estado, presente "
            "FROM arquivo WHERE pasta_rel = ?", (pasta_rel,)
        ).fetchall()
        return {linha["caminho_rel"]: linha for linha in linhas}

    def _evento(self, arquivo_id: int, evento: str, detalhe: dict | None = None):
        self.con.execute(
            "INSERT INTO evento_arquivo (arquivo_id, execucao_id, evento, detalhe, em) "
            "VALUES (?,?,?,?,?)",
            (arquivo_id, self.execucao_id, evento,
             json.dumps(detalhe, ensure_ascii=False) if detalhe else None, cu.agora_iso()),
        )

    def _talvez_commit(self, forcar: bool = False):
        self.desde_commit += 1
        if forcar or self.desde_commit >= self.commit_a_cada:
            self.con.commit()
            self.desde_commit = 0

    # -- varredura ------------------------------------------------------
    def varrer(self, escopos: list[str]) -> str:
        for escopo in escopos:
            rel = escopo.strip("\\")
            abs_dir = str(self.raiz / rel) if rel else str(self.raiz)
            if not os.path.isdir(cu.lp(abs_dir)):
                self.log.erro(f"escopo inexistente, ignorado: {rel or '.'}")
                self.contadores["erros"] += 1
                continue
            self.log.info(f"escopo: {rel or '<raiz inteira>'}")
            status = self._percorrer(abs_dir)
            if status != "ok":
                return status
        return "concluida"

    def _percorrer(self, dir_inicial: str) -> str:
        pilha = [dir_inicial]
        while pilha:
            atual = pilha.pop()
            rel_atual = self._rel(atual)
            if rel_atual == ".":
                rel_atual = ""
            # Pasta ja concluida numa execucao interrompida: nao reprocessa os
            # arquivos, mas ainda precisa ser lida para alcancar as subpastas.
            ja_concluida = rel_atual in self.concluidos

            try:
                subdirs = self._processar_diretorio(atual, rel_atual, ja_concluida)
            except KeyboardInterrupt:
                self.con.commit()
                return "interrompida"
            except OSError as exc:
                self.contadores["erros"] += 1
                self.log.erro(f"falha ao ler diretorio: {exc}", rel_atual)
                self.con.execute(
                    "INSERT INTO progresso_diretorio (pasta_rel, execucao_id, erro, concluido_em) "
                    "VALUES (?,?,?,?) ON CONFLICT(pasta_rel) DO UPDATE SET erro=excluded.erro",
                    (rel_atual, self.execucao_id, str(exc), None),
                )
                self._talvez_commit()
                continue

            pilha.extend(subdirs)

            if self.args.limite and self.contadores["arquivos"] >= self.args.limite:
                self.con.commit()
                self.log.aviso(f"limite de {self.args.limite} arquivos atingido")
                return "interrompida"
        return "ok"

    def _processar_diretorio(self, abs_dir: str, rel_dir: str,
                             ja_concluida: bool = False) -> list[str]:
        subdirs: list[str] = []
        arquivos_aqui = 0
        bytes_aqui = 0
        parcial = False
        existentes = {} if ja_concluida else self._existentes(rel_dir)

        with os.scandir(cu.lp(abs_dir)) as it:
            for entrada in it:
                try:
                    st = entrada.stat(follow_symlinks=False)
                    atributos = getattr(st, "st_file_attributes", 0)
                    eh_dir = bool(atributos & cu.FILE_ATTRIBUTE_DIRECTORY) or entrada.is_dir()
                except OSError as exc:
                    self.contadores["erros"] += 1
                    self.log.erro(f"stat falhou: {exc}", os.path.join(rel_dir, entrada.name))
                    continue

                if eh_dir:
                    if entrada.name.lower() in self.pastas_ignoradas:
                        continue
                    caminho_sub = os.path.join(abs_dir, entrada.name)
                    # Junction/symlink pode criar ciclo. Resolver o alvo custa caro,
                    # entao so resolvemos quando o diretorio e reparse point.
                    if atributos & cu.FILE_ATTRIBUTE_REPARSE_POINT:
                        alvo = os.path.realpath(caminho_sub).lower()
                        if alvo in self.reparse_vistos:
                            self.log.aviso("ciclo de diretorio evitado",
                                           os.path.join(rel_dir, entrada.name))
                            continue
                        self.reparse_vistos.add(alvo)
                    subdirs.append(caminho_sub)
                    continue

                if ja_concluida:
                    continue

                self._registrar_arquivo(rel_dir, entrada.name, st, atributos, existentes)
                arquivos_aqui += 1
                bytes_aqui += st.st_size
                self._talvez_commit()

                if self.args.limite and self.contadores["arquivos"] >= self.args.limite:
                    parcial = True
                    break

        if ja_concluida:
            return subdirs

        self.contadores["diretorios"] += 1
        self.contadores["bytes"] += bytes_aqui
        # Diretorio interrompido pelo --limite NAO e marcado como concluido:
        # na retomada ele precisa ser lido inteiro de novo.
        self.con.execute(
            "INSERT INTO progresso_diretorio "
            "(pasta_rel, execucao_id, arquivos, subpastas, bytes, concluido_em) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(pasta_rel) DO UPDATE SET "
            "execucao_id=excluded.execucao_id, arquivos=excluded.arquivos, "
            "subpastas=excluded.subpastas, bytes=excluded.bytes, "
            "concluido_em=excluded.concluido_em",
            (rel_dir, self.execucao_id, arquivos_aqui, len(subdirs), bytes_aqui,
             None if parcial else cu.agora_iso()),
        )
        if not parcial:
            self.concluidos.add(rel_dir)
        self._talvez_commit()

        if self.contadores["diretorios"] % 500 == 0:
            self._progresso()
        return subdirs

    def _registrar_arquivo(self, rel_dir: str, nome: str, st, atributos: int, existentes: dict):
        caminho_rel = os.path.join(rel_dir, nome) if rel_dir else nome
        ext = os.path.splitext(nome)[1].lower()
        categoria = cu.categoria_de(ext, self.categorias)
        online = 1 if cu.esta_online(atributos) else 0
        temporario = 1 if cu.combina_padrao(nome, self.padroes_temp) else 0
        suportado = 0 if temporario else int(categoria in self.suportadas)
        biblioteca = caminho_rel.split("\\", 1)[0] if "\\" in caminho_rel else ""
        agora = cu.agora_iso()
        mtime = cu.ts_para_iso(st.st_mtime)

        antigo = existentes.get(caminho_rel)
        if antigo is None:
            cur = self.con.execute(
                "INSERT INTO arquivo (caminho_rel, biblioteca, pasta_rel, nome, ext, categoria, "
                "nome_normalizado, profundidade, comprimento_caminho, tamanho_bytes, mtime_utc, "
                "ctime_utc, atributos, online, fixado, temporario, suportado, estado, presente, "
                "primeira_vez_em, ultima_vez_em, ultima_execucao_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (caminho_rel, biblioteca, rel_dir, nome, ext, categoria,
                 cu.normalizar_nome(nome), caminho_rel.count("\\") + 1,
                 len(str(self.raiz)) + 1 + len(caminho_rel),
                 st.st_size, mtime, cu.ts_para_iso(st.st_ctime), atributos, online,
                 1 if cu.esta_fixado(atributos) else 0, temporario, suportado,
                 "SKIPPED" if temporario else "INVENTORIED", agora, agora, self.execucao_id),
            )
            self._evento(int(cur.lastrowid), "novo")
            self.contadores["novos"] += 1
        else:
            mudou = (antigo["tamanho_bytes"] != st.st_size) or (antigo["mtime_utc"] != mtime)
            mudou_nuvem = antigo["online"] != online
            campos = ("UPDATE arquivo SET tamanho_bytes=?, mtime_utc=?, ctime_utc=?, "
                      "atributos=?, online=?, fixado=?, presente=1, ultima_vez_em=?, "
                      "ultima_execucao_id=?")
            valores = [st.st_size, mtime, cu.ts_para_iso(st.st_ctime), atributos, online,
                       1 if cu.esta_fixado(atributos) else 0, agora, self.execucao_id]
            if mudou:
                # conteudo mudou: o que ja tinha sido extraido deixou de valer
                campos += (", alterado_em=?, estado=?, sha256=NULL, hash_em=NULL, "
                           "tentativas=0, ultimo_erro=NULL, proxima_tentativa_em=NULL")
                valores += [agora, "SKIPPED" if temporario else "CHANGED"]
            elif antigo["estado"] == "MISSING":
                campos += ", estado=?"
                valores += ["SKIPPED" if temporario else "CHANGED"]
            campos += " WHERE id=?"
            valores.append(antigo["id"])
            self.con.execute(campos, valores)

            if mudou:
                self._evento(antigo["id"], "alterado",
                             {"tamanho_antes": antigo["tamanho_bytes"],
                              "tamanho_depois": st.st_size,
                              "mtime_antes": antigo["mtime_utc"], "mtime_depois": mtime})
                self.contadores["alterados"] += 1
            if mudou_nuvem:
                self._evento(antigo["id"], "hidratado" if online == 0 else "desidratado")
            if not antigo["presente"]:
                self._evento(antigo["id"], "reapareceu")

        self.contadores["arquivos"] += 1
        if temporario:
            self.contadores["ignorados"] += 1

    # -- fechamento -----------------------------------------------------
    def marcar_sumidos(self, escopos: list[str]):
        """Estava no banco, dentro do escopo varrido, e nao apareceu -> MISSING."""
        for escopo in escopos:
            rel = escopo.strip("\\")
            if rel:
                filtro = "AND (caminho_rel = ? OR caminho_rel LIKE ?)"
                params = (self.execucao_id, rel, rel + "\\%")
            else:
                filtro = ""
                params = (self.execucao_id,)
            linhas = self.con.execute(
                f"SELECT id FROM arquivo WHERE presente=1 "
                f"AND (ultima_execucao_id IS NULL OR ultima_execucao_id <> ?) {filtro}",
                params,
            ).fetchall()
            for linha in linhas:
                self.con.execute(
                    "UPDATE arquivo SET presente=0, estado='MISSING' WHERE id=?", (linha["id"],))
                self._evento(linha["id"], "sumiu")
                self.contadores["sumidos"] += 1
        self.con.commit()

    def _progresso(self):
        decorrido = time.perf_counter() - self.t0
        taxa = self.contadores["arquivos"] / decorrido if decorrido > 0 else 0
        print(f"  ... {self.contadores['arquivos']:,} arquivos | "
              f"{self.contadores['diretorios']:,} pastas | "
              f"{taxa:,.0f} arq/s | {decorrido/60:.1f} min".replace(",", "."), flush=True)


# ----------------------------------------------------------------------
# Relatorio final
# ----------------------------------------------------------------------

def resumo(con, execucao_id: int, decorrido: float, contadores: dict):
    print()
    print("=" * 66)
    print("RESUMO DA VARREDURA")
    print("=" * 66)
    taxa = contadores["arquivos"] / decorrido if decorrido > 0 else 0
    print(f"  execucao            {execucao_id}")
    print(f"  tempo               {decorrido/60:.2f} min ({decorrido:.1f} s)")
    for rotulo, chave in (("arquivos vistos", "arquivos"), ("novos", "novos"),
                          ("alterados", "alterados"), ("sumidos", "sumidos"),
                          ("pastas", "diretorios"), ("erros", "erros")):
        print(f"  {rotulo:<19} {contadores[chave]:,}".replace(",", "."))
    print(f"  velocidade          {taxa:,.0f} arquivos/s".replace(",", "."))
    print()
    print("  por categoria (presentes, nao temporarios):")
    for linha in con.execute(
        "SELECT categoria, COUNT(*) n, SUM(tamanho_bytes) b, SUM(online) nuvem "
        "FROM arquivo WHERE presente=1 AND temporario=0 "
        "GROUP BY categoria ORDER BY n DESC"
    ):
        gb = (linha["b"] or 0) / (1024 ** 3)
        print(f"    {linha['categoria']:<12} {linha['n']:>9,} arq  {gb:>9.2f} GB  "
              f"{linha['nuvem']:>9,} so na nuvem".replace(",", "."))
    if taxa > 0:
        print("\n  projecao nesta velocidade:")
        for alvo in (100_000, 500_000, 1_000_000):
            seg = alvo / taxa
            tempo = f"{seg/60:.1f} min" if seg < 3600 else f"{seg/3600:.1f} h"
            print(f"    {alvo:>9,} arquivos -> {tempo}".replace(",", "."))
    print("=" * 66)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Inventario somente leitura do acervo")
    ap.add_argument("--config", default=None)
    ap.add_argument("--escopo", action="append", default=None,
                    help="subpasta relativa a raiz; pode repetir. Padrao: arvore inteira")
    ap.add_argument("--limite", type=int, default=0,
                    help="para depois de N arquivos (benchmark); a execucao fica retomavel")
    ap.add_argument("--reiniciar", action="store_true",
                    help="descarta o progresso por diretorio e varre tudo de novo")
    ap.add_argument("--sem-sumidos", action="store_true",
                    help="nao marca como MISSING o que nao apareceu nesta varredura")
    ap.add_argument("--quieto", action="store_true")
    args = ap.parse_args()

    cfg = cu.carregar_config(args.config)
    cu.assert_escrita_permitida(cfg["_banco_path"], cfg["_raiz_path"])

    con = cu.conectar(cfg["_banco_path"], cfg["_raiz_path"])
    if not con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='arquivo'"
    ).fetchone():
        print("banco sem schema. Rode antes:  python scripts\\00_init_db.py")
        return 2

    escopos = args.escopo if args.escopo is not None else (cfg.get("escopo") or [])
    if not escopos:
        escopos = [""]

    if args.reiniciar:
        con.execute("DELETE FROM progresso_diretorio")
        con.commit()

    pendente = con.execute(
        "SELECT * FROM execucao WHERE etapa='scan' AND status IN ('em_andamento','interrompida') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if pendente and not args.reiniciar:
        execucao_id = pendente["id"]
        con.execute("UPDATE execucao SET status='em_andamento' WHERE id=?", (execucao_id,))
        con.commit()
        retomando = True
    else:
        con.execute("DELETE FROM progresso_diretorio")
        execucao_id = cu.abrir_execucao(con, "scan", "|".join(escopos), vars(args))
        retomando = False

    log = cu.Log(cfg["_logs_path"], "scan", con, execucao_id, verboso=not args.quieto)
    log.info(f"raiz: {cfg['_raiz_path']}")
    log.info(f"escopos: {escopos}")
    if retomando:
        ja = con.execute("SELECT COUNT(*) FROM progresso_diretorio "
                         "WHERE concluido_em IS NOT NULL").fetchone()[0]
        log.info(f"retomando execucao {execucao_id}: {ja} pastas ja concluidas")

    scanner = Scanner(cfg, con, log, execucao_id, args)
    t0 = time.perf_counter()
    status = "concluida"
    try:
        status = scanner.varrer(escopos)
    except KeyboardInterrupt:
        status = "interrompida"
        log.aviso("interrompido pelo usuario (Ctrl+C)")
    finally:
        con.commit()

    if status == "concluida" and not args.sem_sumidos and not args.limite:
        scanner.marcar_sumidos(escopos)

    decorrido = time.perf_counter() - t0
    cu.fechar_execucao(con, execucao_id, status, scanner.contadores, decorrido)
    con.commit()

    resumo(con, execucao_id, decorrido, scanner.contadores)
    if status != "concluida":
        print("\nvarredura INTERROMPIDA. Para continuar de onde parou, rode o mesmo comando")
        print("sem --limite:  python scripts\\01_scan_inventario.py")
    else:
        print("\nproximo passo:  python scripts\\02_classificar.py")
    log.fechar()
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
