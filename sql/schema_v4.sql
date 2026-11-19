-- ============================================================
-- CEREBRO DOCUMENTAL — esquema SQLite v4
--
-- v4 (2026-09-20): separa OCORRENCIA DOCUMENTAL de CONTEUDO PROCESSADO.
--
--   ocorrencia = uma linha em `arquivo`: um (drive_id, item_id) com seu
--                caminho, datas, autoria e prioridade. NUNCA desaparece.
--   conteudo   = os bytes, identificados por (quick_xor, tamanho_bytes).
--                E o conteudo que e extraido, indexado e tem derivados.
--
-- Dez caminhos apontando para os mesmos bytes = 10 ocorrencias, 1 conteudo.
-- O worker extrai UMA vez; a busca responde os DEZ lugares.
--
-- Aplicado por `scripts/04_migrar_v4.py`, que faz backup antes.
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- conteudo: os bytes, uma vez so.
--
-- `evidencia` diz com que forca afirmamos que duas ocorrencias tem o mesmo
-- conteudo (docs/07_POLITICA_DUPLICATAS.md):
--   FORTE            quick_xor + tamanho iguais  (o padrao aqui)
--   CONFIRMADO_LOCAL hash criptografico calculado sobre os dois arquivos
--
-- `representante_id` e a ocorrencia de onde os bytes foram lidos. Se ela
-- sumir, outra ocorrencia assume SEM reextrair — o conteudo e o mesmo.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conteudo (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    quick_xor         TEXT NOT NULL,
    tamanho_bytes     INTEGER NOT NULL,
    evidencia         TEXT NOT NULL DEFAULT 'FORTE',
    representante_id  INTEGER REFERENCES arquivo(id),
    extraido_de_item  TEXT,          -- item_id de onde os bytes vieram
    estado            TEXT NOT NULL DEFAULT 'PENDENTE',
    -- PENDENTE | EXTRAIDO | PARCIAL | OCR_REQUIRED | VAZIO | ERRO | NAO_SUPORTADO
    extrator          TEXT,
    versao_extrator   TEXT,
    texto_path        TEXT,
    perfil_path       TEXT,
    paginas           INTEGER,
    caracteres        INTEGER,
    trechos           INTEGER,
    bytes_texto       INTEGER,
    ocorrencias       INTEGER NOT NULL DEFAULT 1,
    reeleicoes        INTEGER NOT NULL DEFAULT 0,
    primeira_vez_em   TEXT,
    atualizado_em     TEXT,
    UNIQUE (quick_xor, tamanho_bytes)
);
CREATE INDEX IF NOT EXISTS ix_conteudo_estado ON conteudo(estado);
CREATE INDEX IF NOT EXISTS ix_conteudo_repr   ON conteudo(representante_id);

-- M-12 (auditoria): `ix_arquivo_quickxor` era identico a `ix_arquivo_qxh`
-- (ambos `arquivo(quick_xor)`), criado no banco de producao por uma sessao
-- sem script (o mesmo tipo de deriva da DEC-024). Dois indices iguais so
-- dobram o custo de escrita. Fica o `ix_arquivo_qxh`, que e o que a receita
-- (`03_migrar_v3.INDICES`) cria. Reaplicar este arquivo
-- (`scripts/04_migrar_v4.py`) remove o duplicado; banco novo nunca o tem.
DROP INDEX IF EXISTS ix_arquivo_quickxor;

-- ------------------------------------------------------------
-- v_ocorrencia: "este conteudo existe em N lugares".
-- Toda ocorrencia aparece — inclusive a do representante.
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_ocorrencia;
CREATE VIEW v_ocorrencia AS
SELECT c.id                AS conteudo_id,
       c.quick_xor,
       c.estado            AS estado_conteudo,
       c.texto_path,
       c.representante_id,
       a.id                AS arquivo_id,
       (a.id = c.representante_id) AS eh_representante,
       a.drive_id, a.item_id, a.caminho_rel, a.caminho_nuvem, a.web_url,
       a.prioridade, a.estado AS estado_ocorrencia,
       a.criado_em_nuvem, a.modificado_em_nuvem, a.criado_por, a.modificado_por
FROM conteudo c
JOIN arquivo a ON a.conteudo_id = c.id
WHERE a.presente = 1;

-- ------------------------------------------------------------
-- v_cobertura_conteudo: quanto do acervo esta pesquisavel.
-- Conta OCORRENCIAS cobertas, nao so conteudos extraidos — e a pergunta
-- que interessa: "quantos documentos eu consigo achar hoje?"
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_cobertura_conteudo;
CREATE VIEW v_cobertura_conteudo AS
SELECT c.estado                                   AS estado_conteudo,
       COUNT(DISTINCT c.id)                       AS conteudos,
       COUNT(a.id)                                AS ocorrencias,
       COALESCE(SUM(c.caracteres), 0)             AS caracteres,
       COALESCE(SUM(c.trechos), 0)                AS trechos
FROM conteudo c
LEFT JOIN arquivo a ON a.conteudo_id = c.id AND a.presente = 1
GROUP BY c.estado
ORDER BY ocorrencias DESC;

-- ------------------------------------------------------------
-- v_conteudo_orfao: conteudo cujo representante sumiu ou nao esta
-- disponivel. Quem consome isto e a reeleicao do worker — o conteudo
-- continua valido, so precisa de outra ocorrencia para representa-lo.
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_conteudo_orfao;
CREATE VIEW v_conteudo_orfao AS
SELECT c.id AS conteudo_id, c.quick_xor, c.estado, c.representante_id,
       (SELECT COUNT(*) FROM arquivo a
         WHERE a.conteudo_id = c.id AND a.presente = 1) AS ocorrencias_vivas
FROM conteudo c
LEFT JOIN arquivo r ON r.id = c.representante_id
WHERE c.representante_id IS NULL
   OR r.presente = 0
   OR r.id IS NULL;

-- ------------------------------------------------------------
-- v_fila_nuvem (redefinida no v4): inclui WAITING_HYDRATION.
--
-- O nome do estado sobrou da era da hidratacao (DEC-013). Hoje ele quer
-- dizer "esperando a nuvem": e para onde `worker.falhar()` manda quem
-- tropecou em 429/503/timeout, com `proxima_tentativa_em` no futuro. Sem
-- ele nesta lista, uma queda de rede tirava o item da fila PARA SEMPRE —
-- e `restam_em_espera()` continuava contando o item, deixando o worker
-- dormindo a espera de algo que nunca voltaria.
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_fila_nuvem;
CREATE VIEW v_fila_nuvem AS
SELECT id, caminho_rel, caminho_nuvem, categoria, prioridade, tamanho_bytes,
       estado, tentativas, drive_id, item_id, quick_xor
FROM arquivo
WHERE presente = 1
  AND presente_nuvem = 1
  AND suportado = 1
  AND drive_id IS NOT NULL
  AND item_id IS NOT NULL
  AND estado IN ('QUEUED', 'CHANGED', 'READY', 'ERROR_RETRYABLE',
                 'WAITING_HYDRATION')
  AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= datetime('now'))
ORDER BY prioridade ASC, tamanho_bytes ASC;
