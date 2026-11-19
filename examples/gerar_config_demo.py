"""
gerar_config_demo — monta a config da demonstracao a partir de config/projeto.yaml.

    python examples/gerar_config_demo.py

Troca so os caminhos: a raiz passa a ser o mini-acervo ficticio
(`examples/acervo_ficticio`) e banco/derivados/logs/saida vao para
`examples/demo_saida/` (ignorada pelo Git). A fonte e `local` — nada de
SharePoint, nada de credencial. Grava `examples/demo_saida/config_demo.yaml`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[1]
SAIDA = RAIZ / "examples" / "demo_saida"


def main() -> int:
    base = yaml.safe_load((RAIZ / "config" / "projeto.yaml").read_text(encoding="utf-8"))
    SAIDA.mkdir(parents=True, exist_ok=True)
    base.update({
        "raiz": str(RAIZ / "examples" / "acervo_ficticio"),
        "banco": str(SAIDA / "demo.db"),
        "derivados": str(SAIDA / "derivados"),
        "logs": str(SAIDA / "logs"),
        "saida": str(SAIDA / "output"),
        "escopo": [],
        "fonte": "local",
    })
    # O mini-acervo tem poucos KB: o piso de disco da producao (25 GB) so
    # atrapalharia a demo numa maquina com pouco espaco livre.
    base.setdefault("nuvem", {})["espaco_livre_minimo_gb"] = 1
    destino = SAIDA / "config_demo.yaml"
    destino.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
    print(destino)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
