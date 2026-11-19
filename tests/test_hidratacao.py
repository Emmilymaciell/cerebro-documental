"""
test_hidratacao — prova, em arquivos REAIS do SharePoint, que o ciclo
"hidratar -> processar -> liberar espaco" nao altera o original.

    python tests\\test_hidratacao.py --confirmar
    python tests\\test_hidratacao.py --confirmar --arquivos 3 --max-mb 5

O que faz em cada arquivo escolhido (pequeno, e que esteja so na nuvem):

    1. fotografa tamanho, datas e atributos ANTES;
    2. hidrata (le o conteudo — e so leitura);
    3. calcula o sha256 com o arquivo local;
    4. libera espaco (atributo UNPINNED — o mesmo "Liberar espaco" do Explorer);
    5. confere: tamanho igual, mtime igual, ctime igual, arquivo existe;
    6. le de novo e confere que o sha256 e identico.

Nao apaga, nao move, nao renomeia e nao escreve conteudo. O unico atributo
tocado e o par PINNED/UNPINNED, que e o controle local de cache do OneDrive.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import checar, placar_final  # noqa: E402

import cerebro_util as cu  # noqa: E402


def achar_candidatos(raiz: Path, quantos: int, max_bytes: int, limite_varredura: int = 20000):
    """Primeiros arquivos pequenos, so na nuvem, que nao sejam temporarios."""
    achados = []
    vistos = 0
    pilha = [str(raiz)]
    while pilha and len(achados) < quantos and vistos < limite_varredura:
        atual = pilha.pop()
        try:
            with os.scandir(cu.lp(atual)) as it:
                for entrada in it:
                    try:
                        st = entrada.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    atributos = getattr(st, "st_file_attributes", 0)
                    if atributos & cu.FILE_ATTRIBUTE_DIRECTORY:
                        pilha.append(os.path.join(atual, entrada.name))
                        continue
                    vistos += 1
                    if entrada.name.startswith("~$") or entrada.name == "desktop.ini":
                        continue
                    if 1024 < st.st_size <= max_bytes and cu.esta_online(atributos):
                        achados.append(Path(atual) / entrada.name)
                        if len(achados) >= quantos:
                            break
        except OSError:
            continue
    return achados


def esperar_desidratar(caminho: Path, segundos: float = 90.0) -> bool:
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        if cu.esta_online(cu.atributos_de(caminho)):
            return True
        time.sleep(3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--confirmar", action="store_true",
                    help="obrigatorio: confirma que pode mexer no cache local de arquivos reais")
    ap.add_argument("--arquivos", type=int, default=2)
    ap.add_argument("--max-mb", type=float, default=3.0)
    args = ap.parse_args()

    if not args.confirmar:
        print(__doc__)
        print("Rode com --confirmar para executar.")
        return 2

    cfg = cu.carregar_config(args.config)
    raiz = cfg["_raiz_path"]
    print(f"raiz: {raiz}")
    print(f"espaco livre antes: {cu.espaco_livre_gb('C:\\\\'):.1f} GB\n")

    candidatos = achar_candidatos(raiz, args.arquivos, int(args.max_mb * 1024 ** 2))
    if not candidatos:
        print("nenhum arquivo pequeno e 'so na nuvem' encontrado na varredura inicial.")
        return 1

    for caminho in candidatos:
        rel = str(caminho.relative_to(raiz))
        print(f"\n--- {rel[:90]} ---")
        st_antes = os.stat(cu.lp(caminho))
        atr_antes = cu.atributos_de(caminho)
        checar(cu.esta_online(atr_antes), "arquivo comeca 'somente na nuvem'")

        try:
            lidos = cu.garantir_local(caminho)
        except Exception as exc:
            checar(False, "hidratacao concluida", str(exc))
            continue
        checar(lidos == st_antes.st_size, "hidratou o arquivo inteiro",
               f"{lidos} != {st_antes.st_size}")
        atr_hidratado = cu.atributos_de(caminho)
        checar(not cu.esta_online(atr_hidratado), "apos hidratar, o conteudo esta local")

        sha_1 = cu.sha256_arquivo(caminho)
        st_meio = os.stat(cu.lp(caminho))
        checar(st_meio.st_size == st_antes.st_size, "tamanho inalterado depois de ler")
        checar(int(st_meio.st_mtime) == int(st_antes.st_mtime),
               "data de modificacao inalterada depois de ler")

        checar(cu.liberar_espaco(caminho), "comando de liberar espaco aceito")
        desidratou = esperar_desidratar(caminho)
        print(f"    (desidratacao {'confirmada' if desidratou else 'ainda pendente'} "
              f"— o OneDrive faz isso em segundo plano)")

        st_depois = os.stat(cu.lp(caminho))
        checar(caminho.exists(), "o arquivo continua existindo")
        checar(st_depois.st_size == st_antes.st_size, "tamanho igual ao do inicio",
               f"{st_depois.st_size} != {st_antes.st_size}")
        checar(int(st_depois.st_mtime) == int(st_antes.st_mtime),
               "data de modificacao igual a do inicio")
        checar(int(st_depois.st_ctime) == int(st_antes.st_ctime),
               "data de criacao igual a do inicio")

        sha_2 = cu.sha256_arquivo(caminho)
        checar(sha_1 == sha_2, "conteudo identico depois do ciclo completo (sha256)")

    print(f"\nespaco livre depois: {cu.espaco_livre_gb('C:\\\\'):.1f} GB")
    print("\nObservacao: a verificacao acima e local. Ela prova que o arquivo nao")
    print("foi alterado, movido nem apagado — e, por isso, que nao ha nada novo")
    print("para o OneDrive sincronizar de volta para o SharePoint.")
    return placar_final()


if __name__ == "__main__":
    raise SystemExit(main())
