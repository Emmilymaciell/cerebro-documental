-- ============================================================
-- CEREBRO DOCUMENTAL — esquema SQLite v1
-- Aplicado por: scripts/00_init_db.py
-- Regra: este banco NUNCA fica dentro da arvore do SharePoint.
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- meta: versao do schema, raiz configurada, marcos
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    chave          TEXT PRIMARY KEY,
    valor          TEXT NOT NULL,
    atualizado_em  TEXT NOT NULL
);

-- ------------------------------------------------------------
-- execucao: uma linha por rodada de qualquer etapa (scan, hash,
-- extracao). E o ancoradouro da rastreabilidade e da retomada.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execucao (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    etapa               TEXT NOT NULL,                     -- scan | hash | extracao | ocr
    escopo              TEXT,                              -- prefixo relativo varrido ('' = tudo)
    argumentos          TEXT,                              -- linha de comando, JSON
    iniciada_em         TEXT NOT NULL,
    encerrada_em        TEXT,
    status              TEXT NOT NULL DEFAULT 'em_andamento', -- em_andamento|concluida|interrompida|erro
    diretorios_vistos   INTEGER NOT NULL DEFAULT 0,
    arquivos_vistos     INTEGER NOT NULL DEFAULT 0,
    arquivos_novos      INTEGER NOT NULL DEFAULT 0,
    arquivos_alterados  INTEGER NOT NULL DEFAULT 0,
    arquivos_sumidos    INTEGER NOT NULL DEFAULT 0,
    erros               INTEGER NOT NULL DEFAULT 0,
    duracao_s           REAL,
    host                TEXT,
    observacao          TEXT
);

-- ------------------------------------------------------------
-- arquivo: o inventario. Uma linha por caminho relativo.
-- Nesta fase NAO abre o arquivo: so metadados do sistema de
-- arquivos (nao hidrata arquivo que esta so na nuvem).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS arquivo (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    caminho_rel        TEXT NOT NULL UNIQUE,   -- relativo a RAIZ, com '\'
    biblioteca         TEXT,                   -- primeiro nivel (biblioteca do SharePoint)
    pasta_rel          TEXT NOT NULL,
    nome               TEXT NOT NULL,
    ext                TEXT,                   -- minuscula, com ponto ('.pdf')
    categoria          TEXT,                   -- pdf|docx|xlsx|pptx|imagem|cad|email|compactado|outro
    nome_normalizado   TEXT,                   -- sem acento/rev/copia — agrupa versoes
    profundidade       INTEGER,
    comprimento_caminho INTEGER,               -- diagnostico de MAX_PATH

    tamanho_bytes      INTEGER,
    mtime_utc          TEXT,
    ctime_utc          TEXT,

    atributos          INTEGER,                -- st_file_attributes bruto
    online             INTEGER NOT NULL DEFAULT 0,  -- 1 = so na nuvem; abrir = baixar
    fixado             INTEGER NOT NULL DEFAULT 0,  -- "sempre manter neste dispositivo"
    temporario         INTEGER NOT NULL DEFAULT 0,  -- ~$, .tmp, .~lock, desktop.ini

    prioridade         INTEGER NOT NULL DEFAULT 5,  -- 1 = maior
    estado             TEXT NOT NULL DEFAULT 'inventariado',
    -- inventariado | hash_ok | texto_ok | ocr_pendente | erro | ignorado

    sha256             TEXT,
    hash_em            TEXT,

    presente           INTEGER NOT NULL DEFAULT 1,  -- 0 = sumiu do disco na ultima varredura
    primeira_vez_em    TEXT NOT NULL,
    ultima_vez_em      TEXT NOT NULL,
    ultima_execucao_id INTEGER REFERENCES execucao(id),
    alterado_em        TEXT                          -- ultima vez que tamanho/mtime mudaram
);

CREATE INDEX IF NOT EXISTS ix_arquivo_estado     ON arquivo(estado, prioridade);
CREATE INDEX IF NOT EXISTS ix_arquivo_categoria  ON arquivo(categoria);
CREATE INDEX IF NOT EXISTS ix_arquivo_pasta      ON arquivo(pasta_rel);
CREATE INDEX IF NOT EXISTS ix_arquivo_biblioteca ON arquivo(biblioteca);
CREATE INDEX IF NOT EXISTS ix_arquivo_sha        ON arquivo(sha256);
CREATE INDEX IF NOT EXISTS ix_arquivo_norm       ON arquivo(nome_normalizado);
CREATE INDEX IF NOT EXISTS ix_arquivo_online     ON arquivo(online);
CREATE INDEX IF NOT EXISTS ix_arquivo_fila       ON arquivo(prioridade, estado, tamanho_bytes);

-- ------------------------------------------------------------
-- evento_arquivo: historico. Nada e sobrescrito em silencio.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evento_arquivo (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    arquivo_id   INTEGER NOT NULL REFERENCES arquivo(id),
    execucao_id  INTEGER REFERENCES execucao(id),
    evento       TEXT NOT NULL,   -- novo|alterado|sumiu|reapareceu|hidratado|desidratado|erro
    detalhe      TEXT,            -- JSON com antes/depois
    em           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evento_arquivo ON evento_arquivo(arquivo_id, em);
CREATE INDEX IF NOT EXISTS ix_evento_tipo    ON evento_arquivo(evento, em);

-- ------------------------------------------------------------
-- progresso_diretorio: retomada. Diretorio concluido nao e
-- reaberto na mesma execucao interrompida.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS progresso_diretorio (
    pasta_rel     TEXT PRIMARY KEY,
    execucao_id   INTEGER REFERENCES execucao(id),
    arquivos      INTEGER NOT NULL DEFAULT 0,
    subpastas     INTEGER NOT NULL DEFAULT 0,
    bytes         INTEGER NOT NULL DEFAULT 0,
    concluido_em  TEXT,
    erro          TEXT
);

-- ------------------------------------------------------------
-- extracao: uma linha por arquivo x extrator. Fase 2 em diante.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extracao (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    arquivo_id     INTEGER NOT NULL REFERENCES arquivo(id),
    execucao_id    INTEGER REFERENCES execucao(id),
    extrator       TEXT NOT NULL,       -- pdf_texto | docx | pptx | xlsx | ocr
    versao         TEXT NOT NULL,       -- versao do extrator: muda -> reprocessa
    sha256_fonte   TEXT,                -- hash do arquivo quando foi extraido
    status         TEXT NOT NULL,       -- ok | vazio | precisa_ocr | erro | protegido
    paginas        INTEGER,
    slides         INTEGER,
    abas           INTEGER,
    imagens        INTEGER,
    caracteres     INTEGER,
    palavras       INTEGER,
    densidade_texto REAL,               -- caracteres por pagina (detecta escaneado)
    texto_path     TEXT,                -- derivados/texto/...
    perfil_path    TEXT,                -- derivados/perfil/....json
    erro           TEXT,
    duracao_ms     INTEGER,
    executado_em   TEXT NOT NULL,
    UNIQUE(arquivo_id, extrator)
);
CREATE INDEX IF NOT EXISTS ix_extracao_status ON extracao(status);

-- ------------------------------------------------------------
-- texto_fts: indice de busca. Populado na fase de extracao.
-- ------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS texto_fts USING fts5(
    arquivo_id UNINDEXED,
    caminho_rel UNINDEXED,
    trecho_idx UNINDEXED,
    texto,
    tokenize = "unicode61 remove_diacritics 2"
);

-- ------------------------------------------------------------
-- log: eventos de execucao com nivel. Espelha logs/*.jsonl.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    execucao_id  INTEGER REFERENCES execucao(id),
    nivel        TEXT NOT NULL,     -- info | aviso | erro
    etapa        TEXT,
    caminho_rel  TEXT,
    mensagem     TEXT NOT NULL,
    em           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_log_nivel ON log(nivel, em);

-- ============================================================
-- VIEWS
-- ============================================================

-- Fila de trabalho: o que extrair em seguida, por prioridade.
DROP VIEW IF EXISTS v_fila;
CREATE VIEW v_fila AS
SELECT id, caminho_rel, categoria, prioridade, tamanho_bytes, online, estado
FROM arquivo
WHERE presente = 1
  AND temporario = 0
  AND estado IN ('inventariado', 'hash_ok')
ORDER BY prioridade ASC, online ASC, tamanho_bytes ASC;

-- Cobertura por categoria: quanto do acervo ja foi processado.
DROP VIEW IF EXISTS v_cobertura;
CREATE VIEW v_cobertura AS
SELECT categoria,
       COUNT(*)                                             AS arquivos,
       SUM(tamanho_bytes)                                   AS bytes,
       SUM(online)                                          AS so_na_nuvem,
       SUM(estado = 'texto_ok')                             AS com_texto,
       SUM(estado = 'ocr_pendente')                         AS aguardando_ocr,
       SUM(estado = 'erro')                                 AS com_erro,
       ROUND(100.0 * SUM(estado = 'texto_ok') / COUNT(*), 1) AS pct_texto
FROM arquivo
WHERE presente = 1 AND temporario = 0
GROUP BY categoria
ORDER BY arquivos DESC;

-- Duplicatas exatas (exige a fase de hash).
DROP VIEW IF EXISTS v_duplicata;
CREATE VIEW v_duplicata AS
SELECT sha256, COUNT(*) AS copias, SUM(tamanho_bytes) AS bytes_total,
       MIN(caminho_rel) AS exemplo
FROM arquivo
WHERE sha256 IS NOT NULL AND presente = 1
GROUP BY sha256
HAVING COUNT(*) > 1
ORDER BY copias DESC;

-- Candidatos a versoes do mesmo documento (nome normalizado igual,
-- conteudo diferente). Pista, nao conclusao.
DROP VIEW IF EXISTS v_familia_versao;
CREATE VIEW v_familia_versao AS
SELECT nome_normalizado,
       COUNT(*)            AS variantes,
       MAX(mtime_utc)      AS mais_recente,
       MIN(caminho_rel)    AS exemplo
FROM arquivo
WHERE presente = 1 AND temporario = 0 AND nome_normalizado <> ''
GROUP BY nome_normalizado
HAVING COUNT(*) > 1
ORDER BY variantes DESC;
