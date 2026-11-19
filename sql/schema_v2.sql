-- ============================================================
-- CEREBRO DOCUMENTAL — esquema SQLite v2
--
-- v2 (2026-09-17): processador autonomo continuo.
--   + maquina de estados explicita (nunca inferencia obscura)
--   + tabela `controle` (pause/resume/stop sem sessao de IA)
--   + tabela `checkpoint` (retomada apos queda)
--   + tabela `pasta` (classificacao P0..P4 pela propria arvore)
--   + retry com backoff, duplicata, hidratacao rastreada
--
-- Aplicado por: scripts/00_init_db.py  (idempotente)
-- Regra: este banco NUNCA fica dentro da arvore do SharePoint.
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- meta
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    chave          TEXT PRIMARY KEY,
    valor          TEXT NOT NULL,
    atualizado_em  TEXT NOT NULL
);

-- ------------------------------------------------------------
-- execucao: uma rodada de qualquer etapa
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execucao (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    etapa               TEXT NOT NULL,     -- scan | classificacao | worker | ocr
    escopo              TEXT,
    argumentos          TEXT,
    iniciada_em         TEXT NOT NULL,
    encerrada_em        TEXT,
    status              TEXT NOT NULL DEFAULT 'em_andamento',
    diretorios_vistos   INTEGER NOT NULL DEFAULT 0,
    arquivos_vistos     INTEGER NOT NULL DEFAULT 0,
    arquivos_novos      INTEGER NOT NULL DEFAULT 0,
    arquivos_alterados  INTEGER NOT NULL DEFAULT 0,
    arquivos_sumidos    INTEGER NOT NULL DEFAULT 0,
    erros               INTEGER NOT NULL DEFAULT 0,
    bytes_processados   INTEGER NOT NULL DEFAULT 0,
    bytes_hidratados    INTEGER NOT NULL DEFAULT 0,
    duracao_s           REAL,
    host                TEXT,
    observacao          TEXT
);

-- ------------------------------------------------------------
-- pasta: a arvore, classificada. E o que produz P0..P4.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pasta (
    caminho_rel        TEXT PRIMARY KEY,
    biblioteca         TEXT,
    nome               TEXT,
    nivel              INTEGER,
    obra               TEXT,              -- RD101 | RD102A | RD103 | RD104 | NULL
    categoria_doc      TEXT,              -- contratos | notificacoes | medicoes | ...
    critica            INTEGER NOT NULL DEFAULT 0,
    prioridade         INTEGER NOT NULL DEFAULT 3,   -- 0=P0 ... 4=P4
    motivo             TEXT,              -- por que recebeu essa prioridade (auditavel)
    arquivos           INTEGER NOT NULL DEFAULT 0,
    arquivos_suportados INTEGER NOT NULL DEFAULT 0,
    bytes              INTEGER NOT NULL DEFAULT 0,
    classificado_em    TEXT
);
CREATE INDEX IF NOT EXISTS ix_pasta_prioridade ON pasta(prioridade);
CREATE INDEX IF NOT EXISTS ix_pasta_obra       ON pasta(obra, categoria_doc);

-- ------------------------------------------------------------
-- arquivo: o inventario + a maquina de estados
--
-- ESTADOS (arquivo.estado):
--   INVENTORIED        metadados capturados, ainda nao classificado
--   QUEUED             na fila de extracao
--   WAITING_HYDRATION  precisa vir da nuvem; OneDrive/rede indisponivel
--   HYDRATING          download em andamento
--   READY              conteudo local, pronto para extrair
--   EXTRACTING         extrator rodando (se ficar preso aqui = queda)
--   EXTRACTED          texto extraido, derivados ainda nao validados
--   COMPLETED          derivados gravados e conferidos  <-- fim feliz
--   PARTIAL            extraiu parte (ex.: PDF com paginas ilegiveis)
--   OCR_REQUIRED       sem camada de texto; vai para a fila de OCR
--   UNSUPPORTED        formato sem extrator hoje — catalogado, nao perdido
--   DUPLICATE          mesmo sha256 de outro ja COMPLETED
--   SKIPPED            temporario (~$, .tmp, desktop.ini)
--   CHANGED            mudou no disco; volta para a fila
--   MISSING            sumiu do disco (linha preservada)
--   ERROR_RETRYABLE    falha transitoria; tem proxima_tentativa_em
--   ERROR_FINAL        falhou N vezes; espera revisao humana
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS arquivo (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    -- SEM `UNIQUE`, de proposito (`DEC-015`): a identidade e
    -- `(drive_id, item_id)`, e o caminho e atributo mutavel. Um arquivo
    -- apagado e subido de novo no mesmo lugar e item NOVO — a linha velha
    -- fica `MISSING` com o historico dela, e as duas convivem com o mesmo
    -- `caminho_rel`. Ja aconteceu no acervo real. O UNIQUE ficou aqui por
    -- heranca do v2 e derrubava a insercao; quem garante unicidade e o
    -- indice `ux_arquivo_item` (ver `03_migrar_v3.INDICES`).
    caminho_rel        TEXT NOT NULL,
    biblioteca         TEXT,
    pasta_rel          TEXT NOT NULL,
    nome               TEXT NOT NULL,
    ext                TEXT,
    categoria          TEXT,
    nome_normalizado   TEXT,
    profundidade       INTEGER,
    comprimento_caminho INTEGER,

    tamanho_bytes      INTEGER,
    mtime_utc          TEXT,
    ctime_utc          TEXT,

    atributos          INTEGER,
    online             INTEGER NOT NULL DEFAULT 0,
    fixado             INTEGER NOT NULL DEFAULT 0,
    temporario         INTEGER NOT NULL DEFAULT 0,
    hidratado_por_nos  INTEGER NOT NULL DEFAULT 0,

    prioridade         INTEGER NOT NULL DEFAULT 3,
    suportado          INTEGER NOT NULL DEFAULT 0,
    estado             TEXT NOT NULL DEFAULT 'INVENTORIED',

    sha256             TEXT,
    hash_em            TEXT,
    duplicata_de       INTEGER REFERENCES arquivo(id),

    tentativas         INTEGER NOT NULL DEFAULT 0,
    ultimo_erro        TEXT,
    proxima_tentativa_em TEXT,

    presente           INTEGER NOT NULL DEFAULT 1,
    primeira_vez_em    TEXT NOT NULL,
    ultima_vez_em      TEXT NOT NULL,
    ultima_execucao_id INTEGER REFERENCES execucao(id),
    alterado_em        TEXT,
    processado_em      TEXT
);

CREATE INDEX IF NOT EXISTS ix_arquivo_estado     ON arquivo(estado);
CREATE INDEX IF NOT EXISTS ix_arquivo_fila       ON arquivo(estado, prioridade, online, tamanho_bytes);
CREATE INDEX IF NOT EXISTS ix_arquivo_categoria  ON arquivo(categoria);
CREATE INDEX IF NOT EXISTS ix_arquivo_pasta      ON arquivo(pasta_rel);
CREATE INDEX IF NOT EXISTS ix_arquivo_biblioteca ON arquivo(biblioteca);
CREATE INDEX IF NOT EXISTS ix_arquivo_sha        ON arquivo(sha256);
CREATE INDEX IF NOT EXISTS ix_arquivo_norm       ON arquivo(nome_normalizado);
CREATE INDEX IF NOT EXISTS ix_arquivo_retry      ON arquivo(estado, proxima_tentativa_em);

-- ------------------------------------------------------------
-- evento_arquivo: historico. Nada e sobrescrito em silencio.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evento_arquivo (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    arquivo_id   INTEGER NOT NULL REFERENCES arquivo(id),
    execucao_id  INTEGER REFERENCES execucao(id),
    evento       TEXT NOT NULL,
    detalhe      TEXT,
    em           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evento_arquivo ON evento_arquivo(arquivo_id, em);
CREATE INDEX IF NOT EXISTS ix_evento_tipo    ON evento_arquivo(evento, em);

-- ------------------------------------------------------------
-- progresso_diretorio: retomada da varredura
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
-- extracao: um arquivo x um extrator
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extracao (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    arquivo_id     INTEGER NOT NULL REFERENCES arquivo(id),
    execucao_id    INTEGER REFERENCES execucao(id),
    extrator       TEXT NOT NULL,
    versao         TEXT NOT NULL,
    sha256_fonte   TEXT,
    status         TEXT NOT NULL,      -- ok | parcial | ocr_requerido | vazio | erro | protegido | nao_suportado
    paginas        INTEGER,
    paginas_com_texto INTEGER,
    slides         INTEGER,
    abas           INTEGER,
    imagens        INTEGER,
    caracteres     INTEGER,
    palavras       INTEGER,
    densidade_texto REAL,
    texto_path     TEXT,
    perfil_path    TEXT,
    bytes_texto    INTEGER,
    trechos        INTEGER,
    erro           TEXT,
    tentativas     INTEGER NOT NULL DEFAULT 1,
    duracao_ms     INTEGER,
    executado_em   TEXT NOT NULL,
    UNIQUE(arquivo_id, extrator)
);
CREATE INDEX IF NOT EXISTS ix_extracao_status ON extracao(status);

-- ------------------------------------------------------------
-- texto_fts: busca. Alimentada pelo worker, trecho a trecho.
-- ------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS texto_fts USING fts5(
    arquivo_id UNINDEXED,
    caminho_rel UNINDEXED,
    pagina UNINDEXED,
    trecho_idx UNINDEXED,
    texto,
    tokenize = "unicode61 remove_diacritics 2"
);

-- ------------------------------------------------------------
-- controle: o worker obedece a esta linha. Uma so.
--   comando: RUN | PAUSE | STOP   (escrito por scripts/control.py)
--   estado : PARADO | RODANDO | PAUSADO | PAUSA_SEGURA_DISCO |
--            AGUARDANDO_REDE | FILA_VAZIA | ERRO
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS controle (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    comando             TEXT NOT NULL DEFAULT 'RUN',
    estado              TEXT NOT NULL DEFAULT 'PARADO',
    motivo              TEXT,
    pid                 INTEGER,
    host                TEXT,
    execucao_id         INTEGER,
    item_atual          TEXT,
    item_atual_id       INTEGER,
    iniciado_em         TEXT,
    heartbeat           TEXT,
    atualizado_em       TEXT,
    processados_sessao  INTEGER NOT NULL DEFAULT 0,
    bytes_sessao        INTEGER NOT NULL DEFAULT 0,
    erros_sessao        INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO controle (id, comando, estado, atualizado_em)
VALUES (1, 'RUN', 'PARADO', datetime('now'));

-- ------------------------------------------------------------
-- checkpoint: foto periodica do progresso (auditoria e retomada)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS checkpoint (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    execucao_id       INTEGER REFERENCES execucao(id),
    em                TEXT NOT NULL,
    estado_worker     TEXT,
    ultimo_item_id    INTEGER,
    ultimo_item       TEXT,
    concluidos        INTEGER,
    pendentes         INTEGER,
    erros             INTEGER,
    bytes_processados INTEGER,
    derivados_mb      REAL,
    espaco_livre_gb   REAL,
    observacao        TEXT
);
CREATE INDEX IF NOT EXISTS ix_checkpoint_em ON checkpoint(em);

-- ------------------------------------------------------------
-- log
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    execucao_id  INTEGER REFERENCES execucao(id),
    nivel        TEXT NOT NULL,
    etapa        TEXT,
    caminho_rel  TEXT,
    mensagem     TEXT NOT NULL,
    em           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_log_nivel ON log(nivel, em);

-- ============================================================
-- VIEWS
-- ============================================================

-- Fila de extracao. Ordem: prioridade, ja local antes de nuvem,
-- menor antes de maior (entrega utilidade mais cedo).
DROP VIEW IF EXISTS v_fila;
CREATE VIEW v_fila AS
SELECT id, caminho_rel, categoria, prioridade, tamanho_bytes, online, estado,
       tentativas, proxima_tentativa_em
FROM arquivo
WHERE presente = 1
  AND suportado = 1
  AND estado IN ('QUEUED', 'CHANGED', 'READY', 'WAITING_HYDRATION', 'ERROR_RETRYABLE')
  AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= datetime('now'))
ORDER BY prioridade ASC, online ASC, tamanho_bytes ASC;

-- Progresso por estado: a resposta para "quantos faltam?"
DROP VIEW IF EXISTS v_progresso;
CREATE VIEW v_progresso AS
SELECT estado, COUNT(*) AS arquivos, COALESCE(SUM(tamanho_bytes), 0) AS bytes
FROM arquivo WHERE presente = 1
GROUP BY estado ORDER BY arquivos DESC;

-- Cobertura por categoria de arquivo
DROP VIEW IF EXISTS v_cobertura;
CREATE VIEW v_cobertura AS
SELECT categoria,
       COUNT(*)                                  AS arquivos,
       COALESCE(SUM(tamanho_bytes), 0)           AS bytes,
       SUM(online)                               AS so_na_nuvem,
       SUM(estado = 'COMPLETED')                 AS concluidos,
       SUM(estado = 'OCR_REQUIRED')              AS aguardando_ocr,
       SUM(estado = 'UNSUPPORTED')               AS sem_extrator,
       SUM(estado IN ('ERROR_RETRYABLE','ERROR_FINAL')) AS com_erro,
       ROUND(100.0 * SUM(estado = 'COMPLETED') / COUNT(*), 1) AS pct_concluido
FROM arquivo
WHERE presente = 1 AND temporario = 0
GROUP BY categoria ORDER BY arquivos DESC;

-- Cobertura por prioridade (P0 primeiro)
DROP VIEW IF EXISTS v_cobertura_prioridade;
CREATE VIEW v_cobertura_prioridade AS
SELECT prioridade,
       COUNT(*)                                        AS arquivos,
       SUM(suportado)                                  AS suportados,
       SUM(estado = 'COMPLETED')                       AS concluidos,
       SUM(estado IN ('QUEUED','CHANGED','READY','WAITING_HYDRATION','ERROR_RETRYABLE')) AS na_fila,
       COALESCE(SUM(tamanho_bytes), 0)                 AS bytes
FROM arquivo
WHERE presente = 1 AND temporario = 0
GROUP BY prioridade ORDER BY prioridade;

DROP VIEW IF EXISTS v_duplicata;
CREATE VIEW v_duplicata AS
SELECT sha256, COUNT(*) AS copias, SUM(tamanho_bytes) AS bytes_total,
       MIN(caminho_rel) AS exemplo
FROM arquivo
WHERE sha256 IS NOT NULL AND presente = 1
GROUP BY sha256 HAVING COUNT(*) > 1
ORDER BY copias DESC;

DROP VIEW IF EXISTS v_familia_versao;
CREATE VIEW v_familia_versao AS
SELECT nome_normalizado,
       COUNT(*)         AS variantes,
       MAX(mtime_utc)   AS mais_recente,
       MIN(caminho_rel) AS exemplo
FROM arquivo
WHERE presente = 1 AND temporario = 0 AND nome_normalizado <> ''
GROUP BY nome_normalizado HAVING COUNT(*) > 1
ORDER BY variantes DESC;
