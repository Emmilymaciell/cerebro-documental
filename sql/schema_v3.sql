-- ============================================================
-- CEREBRO DOCUMENTAL — esquema SQLite v3 (a nuvem)
--
-- v3 (2026-09-20): o SharePoint passa a ser lido pelo Microsoft Graph.
--   + `drive`          uma biblioteca de documentos do site
--   + `delta_token`    o checkpoint do delta, por drive
--   + `item_nuvem`     a VERDADE da nuvem, chaveada por (drive_id, item_id)
--   + `evento_nuvem`   historico: novo|renomeado|movido|alterado|removido
--   + `reconciliacao`  como cada linha antiga foi ligada a um item da nuvem,
--                      com metodo e confianca — e a fila de excecoes
--
-- IDENTIDADE CANONICA: (drive_id, item_id).
-- Caminho e nome sao ATRIBUTOS MUTAVEIS — nunca chave.
--
-- Aplicado por `scripts/03_migrar_v3.py`, que faz backup antes. As colunas
-- de nuvem em `arquivo` entram por ALTER TABLE no mesmo script.
--
-- Este arquivo foi extraido do banco vivo em 2026-09-20 e e a fonte
-- reproduzivel dessas estruturas: antes disso elas existiam no banco, mas
-- nenhum script do repositorio sabia recria-las.
-- ============================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS delta_token (
    drive_id         TEXT PRIMARY KEY REFERENCES drive(drive_id),
    delta_link       TEXT,          -- usado na proxima rodada incremental
    next_link        TEXT,          -- rodada interrompida no meio: continua daqui
    status           TEXT NOT NULL DEFAULT 'nunca_rodou',
                                    -- nunca_rodou|em_andamento|concluido|erro
    paginas          INTEGER NOT NULL DEFAULT 0,
    itens_ultima     INTEGER NOT NULL DEFAULT 0,
    iniciado_em      TEXT,
    atualizado_em    TEXT,
    erro             TEXT
);

CREATE TABLE IF NOT EXISTS drive (
    drive_id        TEXT PRIMARY KEY,
    nome            TEXT,
    tipo            TEXT,
    web_url         TEXT,
    site_id         TEXT,
    usado_bytes     INTEGER,
    no_escopo       INTEGER NOT NULL DEFAULT 1,
    observacao      TEXT,
    atualizado_em   TEXT
);

CREATE TABLE IF NOT EXISTS evento_nuvem (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id     TEXT NOT NULL,
    item_id      TEXT NOT NULL,
    evento       TEXT NOT NULL,   -- novo|renomeado|movido|alterado|removido|reapareceu|metadado
    detalhe      TEXT,
    execucao_id  INTEGER,
    em           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_nuvem (
    drive_id            TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    parent_id           TEXT,
    nome                TEXT,
    caminho_rel         TEXT,        -- relativo a raiz do drive, com '\'
    caminho_anterior    TEXT,        -- guardado quando o item se move/renomeia
    eh_pasta            INTEGER NOT NULL DEFAULT 0,
    ext                 TEXT,
    tamanho_bytes       INTEGER,
    quick_xor           TEXT,        -- hash calculado pelo SERVIDOR
    sha1                TEXT,
    etag                TEXT,
    ctag                TEXT,
    web_url             TEXT,
    criado_em           TEXT,
    modificado_em       TEXT,
    criado_por          TEXT,
    modificado_por      TEXT,
    presente            INTEGER NOT NULL DEFAULT 1,   -- 0 = deletado na nuvem
    primeira_vez_em     TEXT,
    ultima_vez_em       TEXT,
    removido_em         TEXT,
    execucao_id         INTEGER,
    PRIMARY KEY (drive_id, item_id)
);

CREATE TABLE IF NOT EXISTS reconciliacao (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    execucao_id     INTEGER,
    arquivo_id      INTEGER,          -- linha antiga (pode ser NULL: so na nuvem)
    drive_id        TEXT,
    item_id         TEXT,             -- item da nuvem (pode ser NULL: so local)
    caminho_local   TEXT,
    caminho_nuvem   TEXT,
    metodo          TEXT,             -- caminho_exato|caminho_e_tamanho|nome_tamanho_unico|...
    confianca       TEXT NOT NULL,    -- ALTA|MEDIA|AMBIGUO|SEM_CORRESPONDENCIA
    aplicado        INTEGER NOT NULL DEFAULT 0,
    conteudo_mudou  INTEGER NOT NULL DEFAULT 0,
    candidatos      INTEGER NOT NULL DEFAULT 1,
    detalhe         TEXT,
    em              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_evento_nuvem_item ON evento_nuvem(drive_id, item_id, em);

CREATE INDEX IF NOT EXISTS ix_evento_nuvem_tipo ON evento_nuvem(evento, em);

CREATE INDEX IF NOT EXISTS ix_item_caminho  ON item_nuvem(caminho_rel);

CREATE INDEX IF NOT EXISTS ix_item_hash     ON item_nuvem(quick_xor);
-- (quick_xor, tamanho) e a chave de CONTEUDO da politica de duplicatas
-- (DEC-025). Sem o indice composto, toda pergunta do tipo "este
-- conteudo existe em outro lugar?" varre as 160 mil linhas.
CREATE INDEX IF NOT EXISTS ix_item_hash_tam ON item_nuvem(quick_xor, tamanho_bytes);

CREATE INDEX IF NOT EXISTS ix_item_nome_tam ON item_nuvem(nome, tamanho_bytes);

CREATE INDEX IF NOT EXISTS ix_item_pai      ON item_nuvem(drive_id, parent_id);

CREATE INDEX IF NOT EXISTS ix_item_presente ON item_nuvem(presente, eh_pasta);

CREATE INDEX IF NOT EXISTS ix_nuvem_caminho  ON item_nuvem(drive_id, caminho_rel);

CREATE INDEX IF NOT EXISTS ix_reconc_arq   ON reconciliacao(arquivo_id);

CREATE INDEX IF NOT EXISTS ix_reconc_conf  ON reconciliacao(confianca, aplicado);

CREATE INDEX IF NOT EXISTS ix_reconc_item  ON reconciliacao(drive_id, item_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_arquivo_item ON arquivo(drive_id, item_id) WHERE item_id IS NOT NULL;

DROP VIEW IF EXISTS v_duplicata_nuvem;
CREATE VIEW v_duplicata_nuvem AS
SELECT quick_xor, COUNT(*) AS copias,
       SUM(tamanho_bytes) AS bytes_total,
       SUM(tamanho_bytes) - MIN(tamanho_bytes) AS bytes_redundantes,
       MIN(caminho_rel) AS exemplo
FROM item_nuvem
WHERE presente = 1 AND eh_pasta = 0 AND quick_xor IS NOT NULL AND tamanho_bytes > 0
GROUP BY quick_xor HAVING COUNT(*) > 1
ORDER BY bytes_redundantes DESC;

DROP VIEW IF EXISTS v_excecoes_reconciliacao;
CREATE VIEW v_excecoes_reconciliacao AS
SELECT confianca, metodo, caminho_local, caminho_nuvem, candidatos, detalhe, em
FROM reconciliacao
WHERE confianca IN ('AMBIGUO', 'SEM_CORRESPONDENCIA')
ORDER BY confianca, caminho_local;

DROP VIEW IF EXISTS v_migracao;
CREATE VIEW v_migracao AS
SELECT
  (SELECT COUNT(*) FROM arquivo WHERE presente = 1)                        AS linhas_antigas,
  (SELECT COUNT(*) FROM arquivo WHERE item_id IS NOT NULL)                 AS com_item_id,
  (SELECT COUNT(*) FROM arquivo WHERE item_id IS NULL AND presente = 1)    AS sem_item_id,
  (SELECT COUNT(*) FROM arquivo WHERE estado = 'COMPLETED')                AS completos,
  (SELECT COUNT(*) FROM item_nuvem WHERE eh_pasta = 0 AND presente = 1)    AS arquivos_na_nuvem;

DROP VIEW IF EXISTS v_nuvem_resumo;
CREATE VIEW v_nuvem_resumo AS
SELECT d.nome AS biblioteca,
       SUM(i.eh_pasta = 0 AND i.presente = 1)              AS arquivos,
       SUM(i.eh_pasta = 1 AND i.presente = 1)              AS pastas,
       SUM(CASE WHEN i.eh_pasta = 0 AND i.presente = 1
                THEN i.tamanho_bytes ELSE 0 END)           AS bytes,
       SUM(i.presente = 0)                                 AS removidos,
       SUM(i.eh_pasta = 0 AND i.presente = 1
           AND i.quick_xor IS NOT NULL)                    AS com_hash
FROM item_nuvem i LEFT JOIN drive d ON d.drive_id = i.drive_id
GROUP BY d.nome;

-- ------------------------------------------------------------
-- v_fila_nuvem: a fila que o worker da NUVEM consome.
-- Alem de tudo que a v_fila exige, o item tem de ter identidade
-- da nuvem — sem drive_id + item_id nao ha por onde baixar.
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
  AND estado IN ('QUEUED', 'CHANGED', 'READY', 'ERROR_RETRYABLE')
  AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= datetime('now'))
ORDER BY prioridade ASC, tamanho_bytes ASC;
