"""
quickxor — o mesmo hash que o SharePoint calcula (quickXorHash).

Por que isto existe: o Graph ja devolve `file.hashes.quickXorHash` de cada
arquivo, de graca, na listagem. Se soubermos calcular o mesmo hash aqui,
ganhamos duas coisas:

  1. **duplicata exata sem baixar nada** — basta comparar o hash que veio
     do servidor (hoje seria preciso baixar 1 TB para calcular sha256);
  2. **conferencia de download** — baixou, calcula, compara: se bater, o
     arquivo chegou inteiro e sem corrupcao.

Implementacao portada da referencia da Microsoft (QuickXorHash). E um XOR
com deslocamento circular de 11 bits sobre 160 bits, e no fim o tamanho do
arquivo entra por XOR nos bytes menos significativos.

Nao e hash criptografico: serve para identidade e integridade de transporte,
nao para seguranca.
"""

from __future__ import annotations

import base64
from pathlib import Path

LARGURA_BITS = 160
DESLOCAMENTO = 11
BITS_ULTIMA_CELULA = 32
CELULAS = (LARGURA_BITS - 1) // 64 + 1      # 3 celulas: 64 + 64 + 32 bits
MASCARA64 = (1 << 64) - 1

# Blocos multiplos de 160 bytes mantem o deslocamento alinhado entre chamadas.
BLOCO_PADRAO = LARGURA_BITS * 26_000        # ~3,97 MB


class QuickXorHash:
    """Acumulador de hash. Use `update()` quantas vezes quiser."""

    def __init__(self):
        self._dados = [0] * CELULAS
        self._deslocamento = 0
        self._tamanho = 0

    def update(self, bloco: bytes) -> "QuickXorHash":
        if not bloco:
            return self
        n = len(bloco)
        indice = self._deslocamento // 64
        offset = self._deslocamento % 64

        for i in range(min(n, LARGURA_BITS)):
            ultima = indice == CELULAS - 1
            bits_da_celula = BITS_ULTIMA_CELULA if ultima else 64

            # XOR de todos os bytes desta "faixa" (passo de 160 bytes)
            combinado = 0
            for j in range(i, n, LARGURA_BITS):
                combinado ^= bloco[j]

            if offset <= bits_da_celula - 8:
                self._dados[indice] ^= (combinado << offset) & MASCARA64
            else:
                # O byte cai entre duas celulas: parte aqui, parte na seguinte.
                seguinte = 0 if ultima else indice + 1
                resto = bits_da_celula - offset
                self._dados[indice] ^= (combinado << offset) & MASCARA64
                self._dados[seguinte] ^= (combinado >> resto) & MASCARA64

            offset += DESLOCAMENTO
            while offset >= bits_da_celula:
                indice = 0 if ultima else indice + 1
                offset -= bits_da_celula

        self._deslocamento = (self._deslocamento
                              + DESLOCAMENTO * (n % LARGURA_BITS)) % LARGURA_BITS
        self._tamanho += n
        return self

    def digest(self) -> bytes:
        rgb = bytearray((LARGURA_BITS - 1) // 8 + 1)          # 20 bytes
        for i in range(CELULAS - 1):
            rgb[i * 8:(i + 1) * 8] = (self._dados[i] & MASCARA64).to_bytes(8, "little")
        sobra = len(rgb) - (CELULAS - 1) * 8                   # 4 bytes
        rgb[(CELULAS - 1) * 8:] = (self._dados[-1] & MASCARA64).to_bytes(8, "little")[:sobra]

        # O tamanho do arquivo entra por XOR nos 8 ultimos bytes.
        comprimento = (self._tamanho & MASCARA64).to_bytes(8, "little")
        inicio = LARGURA_BITS // 8 - 8                         # 12
        for i, byte in enumerate(comprimento):
            rgb[inicio + i] ^= byte
        return bytes(rgb)

    def base64(self) -> str:
        """Formato em que o Graph devolve o hash."""
        return base64.b64encode(self.digest()).decode("ascii")


def de_bytes(dados: bytes) -> str:
    return QuickXorHash().update(dados).base64()


def de_arquivo(caminho: Path | str, bloco: int = BLOCO_PADRAO) -> str:
    """Hash de um arquivo em disco, lendo em pedacos."""
    h = QuickXorHash()
    with open(_lp(caminho), "rb") as fh:
        while True:
            pedaco = fh.read(bloco)
            if not pedaco:
                break
            h.update(pedaco)
    return h.base64()


def _lp(caminho: Path | str) -> str:
    import os

    texto = str(caminho)
    if os.name == "nt" and not texto.startswith("\\\\?\\"):
        texto = "\\\\?\\" + os.path.abspath(texto)
    return texto
