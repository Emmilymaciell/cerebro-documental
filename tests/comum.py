"""
comum — infraestrutura dos testes. Arvore falsa, config isolada, placar.

Nenhum teste toca no SharePoint real.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "scripts"))

PLACAR = {"pass": 0, "fail": 0}


def checar(condicao: bool, rotulo: str, detalhe: str = "") -> bool:
    if condicao:
        PLACAR["pass"] += 1
        print(f"  PASS  {rotulo}")
    else:
        PLACAR["fail"] += 1
        print(f"  FAIL  {rotulo}  {detalhe}")
    return bool(condicao)


def placar_final() -> int:
    print("\n" + "=" * 52)
    print(f"  {PLACAR['pass']} PASS / {PLACAR['fail']} FAIL")
    print("=" * 52)
    return 1 if PLACAR["fail"] else 0


def area_temporaria(prefixo: str = "cerebro_teste_") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefixo))


def escrever_config(area: Path, raiz_falsa: Path, **sobrescrever) -> Path:
    base = yaml.safe_load((RAIZ / "config" / "projeto.yaml").read_text(encoding="utf-8"))
    base["raiz"] = str(raiz_falsa)
    base["banco"] = str(area / "teste.db")
    base["derivados"] = str(area / "derivados")
    base["logs"] = str(area / "logs")
    base["saida"] = str(area / "output")
    base["escopo"] = []
    # Config de teste le a arvore de mentira montada pelo proprio teste.
    # Em producao a fonte e sempre a nuvem (DEC-012).
    base["fonte"] = "local"
    for chave, valor in sobrescrever.items():
        if isinstance(valor, dict) and isinstance(base.get(chave), dict):
            base[chave].update(valor)
        else:
            base[chave] = valor
    destino = area / "config_teste.yaml"
    destino.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    return destino


def rodar(script: str, config: Path, *extra) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(RAIZ / "scripts" / script), *extra, "--config", str(config)]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def foto_da_arvore(raiz: Path) -> dict:
    """(caminho -> tamanho, mtime) — para provar que nada foi tocado."""
    foto = {}
    for caminho in sorted(raiz.rglob("*")):
        if caminho.is_file():
            st = caminho.stat()
            foto[str(caminho.relative_to(raiz))] = (st.st_size, int(st.st_mtime))
    return foto


def foto_exata(raiz: Path) -> dict:
    """(caminho -> tamanho, mtime_ns, sha256) — a prova forte da REGRA 1.

    `foto_da_arvore` trunca o mtime para o segundo: um toque no mesmo segundo
    da escrita do teste passa despercebido. Aqui o mtime vai em nanossegundos
    e o conteudo entra pelo sha256 — reescrever o mesmo tamanho tambem aparece.
    """
    import hashlib

    foto = {}
    for caminho in sorted(raiz.rglob("*")):
        if caminho.is_file():
            st = caminho.stat()
            foto[str(caminho.relative_to(raiz))] = (
                st.st_size, st.st_mtime_ns, hashlib.sha256(caminho.read_bytes()).hexdigest())
    return foto
