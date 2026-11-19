# Sanitização desta versão pública

Cópia sanitizada de um projeto em uso real. Nada aqui é dado de cliente ou de terceiro: o que aparece como exemplo é inventado. Detalhe completo da sanitização: relatório interno da autora.

## O que foi removido

Saídas geradas a partir do acervo real (`output/*.csv|md|json`), anexo `.docx` de configuração, arquivos de proveniência.

## O que foi trocado

`config/mapa_drives.yaml` reescrito com IDs zerados e URLs `contoso.sharepoint.com`; nomes do contrato → genéricos.

## O que é fictício

`examples/acervo_ficticio/` (9 documentos inventados) para a demonstração de indexação e busca.

## Como verificar

```bat
for %f in (tests\test_*.py) do py -3.12 %f      :: cada suíte é um script; graph_ro e quickxor com --sem-rede
:: test_hidratacao.py é teste manual contra o OneDrive real (exige --confirmar) e fica fora da rodada offline
```

Varreduras (2026-11-19): gitleaks `dir` só com falsos positivos justificados; busca por caminho local, e-mail, nomes do cliente e links privados sem ocorrências reais.
