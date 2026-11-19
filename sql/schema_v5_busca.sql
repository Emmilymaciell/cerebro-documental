-- ============================================================
-- CEREBRO DOCUMENTAL — esquema SQLite v5 (a camada de busca)
--
-- PROPOSTO em 2026-09-27 pelo desenho de `docs/09_BUSCA.md`.
-- FECHADO em 2026-10-04 (sprint de convergencia): politica de corte medida
-- e decidida (`chunk/2.0:1500:150`), normalizador `norm/1.1`, estado
-- REMOVIDO, coluna `fonte_texto`.
--
-- Aplicado SO por `scripts/05_migrar_v5.py` (backup, transacao unica,
-- conferencias). Nao usar `executescript()` com este arquivo dentro de uma
-- transacao: o Python faz COMMIT antes. `busca.instrucoes_ddl()` o divide
-- em instrucoes. Aditivo e idempotente (IF NOT EXISTS / DROP VIEW).
--
-- DEPENDE do v4 apenas por NOME de coluna, nunca por ordem:
--   meta(chave, valor, atualizado_em)
--   conteudo(id, quick_xor, tamanho_bytes, estado, extrator,
--            versao_extrator, texto_path)
--   arquivo(conteudo_id, presente, prioridade, temporario, suportado,
--           ultima_vez_em)
--   execucao(id)
--   v_ocorrencia(conteudo_id, arquivo_id, eh_representante, drive_id,
--                item_id, caminho_rel, caminho_nuvem, web_url, prioridade,
--                estado_ocorrencia, criado_por, modificado_por,
--                modificado_em_nuvem)
-- `05_migrar_v5.py` confere cada um antes de tocar no banco.
--
-- v5 acrescenta a camada que responde "onde esta escrito isto?":
--
--   + `segmento`         pagina | slide | aba — a unidade de ORIGEM do texto
--   + `trecho`           o pedaco indexavel, com posicao e segmento de origem
--   + `busca_fts`        FTS5 com CONTENT EXTERNO apontando para `trecho`
--   + `indice_conteudo`  o que ja esta indexado, com que versoes e que texto
--   + views de busca, de reindexacao e de saude do indice
--
-- A DECISAO CENTRAL: o indice e por CONTEUDO, nao por ocorrencia.
--
--   v4 separou OCORRENCIA (`arquivo`: um (drive_id, item_id) com caminho,
--   datas, autoria e prioridade proprios) de CONTEUDO (`conteudo`: os bytes,
--   por (quick_xor, tamanho_bytes)). O indice atual `texto_fts` ficou do lado
--   errado dessa linha: ele e chaveado por `arquivo_id` — o representante.
--
--   Consequencias do modelo antigo, todas reais:
--     - se o representante for reeleito (worker.reeleger_representantes()),
--       o indice fica apontando para um `arquivo_id` que nao e mais o
--       representante, e ninguem limpa a linha velha;
--     - se a ocorrencia que carrega o indice virar MISSING, o conteudo
--       continua vivo nas outras 9 copias mas SOME da busca;
--     - reprocessar duas ocorrencias do mesmo conteudo escreve o mesmo texto
--       duas vezes no indice.
--
--   Em v5 `trecho.conteudo_id` e a chave. Um conteudo = uma indexacao.
--   A busca chega nas N ocorrencias fisicas pela `v_ocorrencia` do v4.
--
-- Medido no banco em 2026-09-27 (leitura, sem escrita):
--   61.427 ocorrencias suportadas, vivas, com hash
--   35.872 conteudos distintos entre elas
--   => 25.555 indexacoes (41,6%) que o modelo por conteudo NAO faz.
--
-- ============================================================

PRAGMA foreign_keys = ON;


-- ============================================================
-- 1. METADADOS DE VERSAO
--
-- As versoes do normalizador e do chunker ficam em `meta` porque sao a
-- entrada da decisao "isto precisa reindexar?" (`v_reindexar`). Mudou a
-- versao, aquilo reindexa; o resto nao. Mesmo principio da REGRA 9, que ja
-- vale para os extratores.
--
-- O numero de versao inclui os parametros: trocar `trecho_chars` de 1500
-- para 1200 E UMA MUDANCA DE VERSAO, porque muda o conteudo do indice.
-- Se ficar so no `config/projeto.yaml`, o banco nao tem como saber que o
-- que esta la dentro foi produzido por outra regra.
--
-- Aqui e so a SEMENTE (INSERT OR IGNORE). Quem carimba de verdade e
-- `busca.gravar_versoes()`, chamado pela migracao: o codigo e a fonte das
-- versoes, e o worker recusa subir se o banco divergir
-- (`busca.conferir_versoes()`).
-- ============================================================

INSERT OR IGNORE INTO meta (chave, valor, atualizado_em) VALUES
    ('busca.versao_esquema',      '5',                 datetime('now')),
    ('busca.versao_normalizador', 'norm/1.1',          datetime('now')),
    ('busca.versao_chunker',      'chunk/2.0:1500:150',datetime('now')),
    ('busca.tokenizer',           'unicode61 remove_diacritics 2', datetime('now'));


-- ============================================================
-- 2. SEGMENTO — a unidade de ORIGEM
--
-- O extrator ja devolve `paginas_texto: [(numero, texto), ...]`
-- (`scripts/extratores.py`). Cada par vira uma linha aqui. E o que permite
-- que o resultado diga "pagina 12" ou "aba Medicoes 2025" em vez de
-- "em algum lugar deste arquivo".
--
-- `tipo` vem do extrator, nao de adivinhacao:
--     pdf   -> pagina  (numero = pagina real, 1-based)
--     pptx  -> slide   (numero = slide, 1-based)
--     xlsx  -> aba     (numero = ordem da aba; rotulo = nome da aba)
--     docx  -> documento (numero = 0; o extrator nao pagina, e nao vamos
--                         inventar paginacao que o arquivo nao tem)
--     csv   -> documento
--     texto -> documento
--
-- `rotulo` e o que se mostra para a pessoa. `numero` e o que se usa para
-- ordenar e para abrir o documento na posicao certa.
--
-- `caracteres` e do texto JA NORMALIZADO — e por isso pode ser menor que o
-- que o extrator contou (cabecalho repetido removido, espacos colapsados).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS segmento (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    conteudo_id   INTEGER NOT NULL REFERENCES conteudo(id),
    ordem         INTEGER NOT NULL,   -- 1..N, ordem de aparicao no documento
    tipo          TEXT    NOT NULL,   -- pagina | slide | aba | documento
    numero        INTEGER,            -- numero de origem (0 = nao paginado)
    rotulo        TEXT,               -- "p. 12", "slide 3", "Medicoes 2025"
    caracteres    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (conteudo_id, ordem)
);

CREATE INDEX IF NOT EXISTS ix_segmento_conteudo ON segmento(conteudo_id);


-- ============================================================
-- 3. TRECHO — o pedaco indexavel
--
-- POR QUE UMA TABELA REAL, e nao so o FTS:
--
--   a) o FTS5 desta maquina e SQLite 3.42.0, que NAO tem
--      `contentless_delete` (chegou no 3.43). Uma tabela FTS5 sem conteudo
--      (`content=''`) nao consegue apagar linha de forma confiavel, e
--      apagar com exatidao e requisito (REGRA 6: nada vira lixo silencioso;
--      REGRA 8: reindexar e uma transacao inteira ou nenhuma).
--      Verificado: `create virtual table u using fts5(a, content='',
--      contentless_delete=1)` -> "unrecognized option".
--
--   b) com CONTENT EXTERNO (`content='trecho'`) o texto existe UMA vez —
--      nesta tabela. O FTS5 guarda so o indice invertido e le o texto daqui
--      quando precisa (inclusive para `snippet()` e `highlight()`).
--      Contentless economizaria a coluna `texto`, mas custaria o snippet e
--      a exclusao correta. Nao compensa.
--
--   c) posicao e origem sao dados de negocio (rastreabilidade, REGRA 9),
--      nao detalhe do motor de busca. Se um dia trocarmos o FTS5, `trecho`
--      continua valendo.
--
-- `char_inicio` / `char_fim` sao deslocamentos no TEXTO NORMALIZADO DO
-- SEGMENTO (nao do documento inteiro, nao do texto bruto). Servem para:
--   - reabrir o derivado e mostrar o entorno sem depender do FTS;
--   - conferir que dois chunkings produziram o mesmo recorte;
--   - ordenar os trechos de uma mesma pagina de forma estavel.
--
-- `texto` e o texto normalizado do recorte, COM acento e COM maiuscula.
-- O dobramento de caixa e de acento e do tokenizador, na hora da consulta —
-- nao do armazenamento. Assim o trecho exibido diz o que o documento diz.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trecho (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- = busca_fts.rowid
    conteudo_id   INTEGER NOT NULL REFERENCES conteudo(id),
    segmento_id   INTEGER NOT NULL REFERENCES segmento(id),
    ordinal       INTEGER NOT NULL,   -- 0..N-1 dentro do segmento
    char_inicio   INTEGER NOT NULL,
    char_fim      INTEGER NOT NULL,
    texto         TEXT    NOT NULL,
    UNIQUE (segmento_id, ordinal)
);

-- "todos os trechos deste conteudo" e a consulta mais quente da camada:
-- e o que a reindexacao apaga e o que a busca junta.
CREATE INDEX IF NOT EXISTS ix_trecho_conteudo ON trecho(conteudo_id);
CREATE INDEX IF NOT EXISTS ix_trecho_segmento ON trecho(segmento_id);


-- ============================================================
-- 4. busca_fts — o indice invertido, com conteudo externo
--
-- `tokenize = "unicode61 remove_diacritics 2"` e o mesmo do `texto_fts`
-- atual, e continua correto:
--   - `remove_diacritics 2` dobra acento em TODO o intervalo Unicode
--     (o `1` deixa de fora caracteres fora do Latin-1 Supplement);
--   - dobra caixa por padrao.
--   => `MEDIÇÃO`, `Medicao` e `medição` sao o mesmo token.
--
-- SEM `tokenchars`: de proposito. Se `-` e `/` virassem caracteres de
-- token, "RD-101" seria UM token e procurar "101" sozinho falharia. Sem
-- eles, "RD-101" vira os tokens "rd" e "101", e o codigo inteiro se acha
-- com a consulta de frase: MATCH '"rd 101"'.
--
-- SEM indice de prefixo (`prefix='2 3'`): de proposito, por ora. Ele
-- aumenta o indice em algo entre 30% e 40% para acelerar `termo*`. FTS5
-- responde `termo*` sem ele, so que mais devagar. O gatilho para criar
-- esta em `docs/09_BUSCA.md`, secao 9 — e e um numero medido, nao um
-- palpite.
--
-- A lista de colunas do FTS5 tem de casar por NOME com colunas da tabela
-- de conteudo. Declaramos so `texto`: o resto se obtem pelo JOIN com
-- `trecho` por rowid, que e chave primaria e portanto gratuito.
-- ------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS busca_fts USING fts5(
    texto,
    content      = 'trecho',
    content_rowid= 'id',
    tokenize     = "unicode61 remove_diacritics 2"
);


-- ------------------------------------------------------------
-- 4b. Sincronizacao por TRIGGER — nao por disciplina do programador
--
-- Com conteudo externo o FTS5 nao se atualiza sozinho. Os tres gatilhos
-- abaixo fazem com que NENHUM caminho de codigo consiga escrever em
-- `trecho` e esquecer o indice. Isso e o que sustenta a promessa de
-- "reindexar sem deixar lixo": o `DELETE FROM trecho WHERE conteudo_id=?`
-- da reindexacao ja limpa o invertido, na mesma transacao.
--
-- A forma `INSERT INTO busca_fts(busca_fts, rowid, texto) VALUES('delete',
-- ...)` e a unica maneira correta de remover de uma tabela de conteudo
-- externo: o FTS5 precisa do texto ANTIGO para descontar os tokens.
--
-- NAO ha ON DELETE CASCADE de `segmento` para `trecho`, de proposito:
-- a acao de chave estrangeira pode nao disparar o gatilho de DELETE
-- dependendo de `PRAGMA recursive_triggers`, e o indice ficaria com lixo
-- sem ninguem perceber. Com a FK sem cascata, apagar o segmento antes do
-- trecho da erro — que e exatamente o comportamento desejado. A ordem da
-- reindexacao e: trecho, depois segmento.
-- ------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_trecho_ai AFTER INSERT ON trecho BEGIN
    INSERT INTO busca_fts(rowid, texto) VALUES (new.id, new.texto);
END;

CREATE TRIGGER IF NOT EXISTS trg_trecho_ad AFTER DELETE ON trecho BEGIN
    INSERT INTO busca_fts(busca_fts, rowid, texto)
    VALUES ('delete', old.id, old.texto);
END;

CREATE TRIGGER IF NOT EXISTS trg_trecho_au AFTER UPDATE ON trecho BEGIN
    INSERT INTO busca_fts(busca_fts, rowid, texto)
    VALUES ('delete', old.id, old.texto);
    INSERT INTO busca_fts(rowid, texto) VALUES (new.id, new.texto);
END;

-- A conferencia periodica do invertido contra `trecho` (operacao, nao laco
-- do worker) e:
--
--     INSERT INTO busca_fts(busca_fts, rank) VALUES('integrity-check', 1);
--
-- O `rank = 1` NAO e opcional: sem ele o FTS5 so verifica a coerencia
-- interna do proprio indice e NAO percebe que ele divergiu da tabela de
-- conteudo. Medido nesta maquina (SQLite 3.42.0): apagando uma linha de
-- `trecho` com o gatilho desligado, a forma sem argumento passa e a forma
-- com `rank = 1` levanta "database disk image is malformed".


-- ============================================================
-- 5. indice_conteudo — a contabilidade da indexacao
--
-- Uma linha por conteudo indexado. Responde, sem inferencia (REGRA 7):
--   - isto ja foi indexado?
--   - com que versao de extrator, normalizador e chunker?
--   - o texto normalizado e o mesmo de antes? (`sha256_texto`)
--
-- `sha256_texto` e a trava fina contra reindexacao inutil. Subir a versao
-- de um extrator costuma mudar o texto de ALGUNS arquivos, nao de todos.
-- Quem produziu texto identico so tem os carimbos de versao atualizados:
-- nenhuma linha de `trecho` e tocada, nenhum token e reescrito.
--
-- `estado`:
--   INDEXADO   ha trechos no indice
--   SEM_TEXTO  o conteudo foi considerado e nao rendeu texto (0 byte,
--              PDF escaneado sem OCR, planilha vazia). Fica registrado
--              para NAO ser reavaliado a cada rodada — REGRA 6: nada some,
--              nem mesmo o nada.
--   REMOVIDO   os trechos foram tirados por `busca.desindexar_conteudo()`
--              porque nenhuma ocorrencia estava presente (limpeza MANUAL,
--              com carencia). A linha fica; se uma ocorrencia voltar, o
--              conteudo reentra em `v_reindexar` com motivo REMOVIDO.
--
-- `fonte_texto` diz de onde veio o texto indexado (REGRA 9):
--   extrator   o `paginas_texto` do extrator, pelo worker
--   indice_v4  reconstruido do `texto_fts` v4 e conferido (migracao)
--   derivado   o .txt.gz de `derivados/` (perde a pagina, exceto planilha)
--
-- `cabecalho_removido` guarda, em JSON, as linhas que a normalizacao
-- identificou como cabecalho/rodape corrido e retirou. Nada e descartado em
-- silencio: da para auditar a decisao e desfaze-la subindo a versao do
-- normalizador.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS indice_conteudo (
    conteudo_id         INTEGER PRIMARY KEY REFERENCES conteudo(id),
    estado              TEXT NOT NULL DEFAULT 'INDEXADO',  -- INDEXADO | SEM_TEXTO | REMOVIDO
    versao_extrator     TEXT,
    versao_normalizador TEXT NOT NULL,
    versao_chunker      TEXT NOT NULL,
    sha256_texto        TEXT,      -- do texto normalizado, segmento a segmento
    segmentos           INTEGER NOT NULL DEFAULT 0,
    trechos             INTEGER NOT NULL DEFAULT 0,
    caracteres          INTEGER NOT NULL DEFAULT 0,
    cabecalho_removido  TEXT,      -- JSON: linhas retiradas por repeticao
    fonte_texto         TEXT,      -- extrator | indice_v4 | derivado
    execucao_id         INTEGER REFERENCES execucao(id),
    indexado_em         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_indice_estado ON indice_conteudo(estado);


-- ------------------------------------------------------------
-- 5b. Indice de apoio na `arquivo`
--
-- `ix_arquivo_conteudo(conteudo_id)` ja existe desde o v4. O parcial
-- abaixo serve a consulta que a busca faz o tempo todo — "as ocorrencias
-- VIVAS deste conteudo, em ordem de prioridade" — e evita ler as 7.400
-- linhas MISSING para descartar depois.
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_arquivo_conteudo_vivo
    ON arquivo(conteudo_id, prioridade) WHERE presente = 1;


-- ============================================================
-- 6. VIEWS
-- ============================================================

-- ------------------------------------------------------------
-- v_trecho_ocorrencia: de um trecho para TODOS os lugares fisicos.
--
-- Esta e a view que cumpre a promessa da `DEC-027`: um conteudo foi
-- indexado uma vez e a busca devolve as N ocorrencias, cada uma com seu
-- `drive_id`, `item_id`, caminho, prioridade e autoria.
--
-- Nao ha MATCH aqui de proposito: uma view nao pode receber MATCH. A
-- consulta canonica (secao 8) junta esta view ao resultado do FTS por
-- `trecho_id`.
--
-- `v_ocorrencia` (v4) ja filtra `presente = 1`. E por isso que uma
-- ocorrencia que vira MISSING desaparece do resultado SEM que nada seja
-- apagado do indice — e as outras copias continuam achaveis.
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_trecho_ocorrencia;
CREATE VIEW v_trecho_ocorrencia AS
SELECT t.id            AS trecho_id,
       t.conteudo_id,
       t.ordinal,
       t.char_inicio,
       t.char_fim,
       s.tipo          AS segmento_tipo,
       s.numero        AS segmento_numero,
       s.rotulo        AS segmento_rotulo,
       s.ordem         AS segmento_ordem,
       o.arquivo_id,
       o.eh_representante,
       o.drive_id,
       o.item_id,
       o.caminho_rel,
       o.caminho_nuvem,
       o.web_url,
       o.prioridade,
       o.estado_ocorrencia,
       o.criado_por,
       o.modificado_por,
       o.modificado_em_nuvem
FROM trecho t
JOIN segmento     s ON s.id = t.segmento_id
JOIN v_ocorrencia o ON o.conteudo_id = t.conteudo_id;


-- ------------------------------------------------------------
-- v_reindexar: o que precisa ser (re)indexado, e por que.
--
-- A resposta para "quanto falta indexar?" e `SELECT COUNT(*) FROM
-- v_reindexar` — estado explicito, nunca estimativa (REGRA 7).
--
-- So entra conteudo que (a) tem texto extraido e (b) tem pelo menos uma
-- ocorrencia viva. Indexar conteudo sem ocorrencia viva seria trabalho
-- para um resultado que a `v_ocorrencia` nunca mostraria.
--
-- `motivo` e ordenado da causa mais forte para a mais fraca, para que o
-- relatorio diga uma coisa so por linha.
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_reindexar;
CREATE VIEW v_reindexar AS
SELECT c.id                AS conteudo_id,
       c.quick_xor,
       c.tamanho_bytes,
       c.estado            AS estado_conteudo,
       c.extrator,
       c.versao_extrator,
       c.texto_path,
       i.versao_extrator     AS indexado_com_extrator,
       i.versao_normalizador AS indexado_com_normalizador,
       i.versao_chunker      AS indexado_com_chunker,
       CASE
         WHEN i.conteudo_id IS NULL THEN 'NUNCA_INDEXADO'
         WHEN i.estado = 'REMOVIDO' THEN 'REMOVIDO'
         WHEN i.versao_extrator IS NOT c.versao_extrator THEN 'EXTRATOR_NOVO'
         WHEN i.versao_normalizador <>
              (SELECT valor FROM meta WHERE chave = 'busca.versao_normalizador')
              THEN 'NORMALIZADOR_NOVO'
         ELSE 'CHUNKER_NOVO'
       END                 AS motivo,
       (SELECT MIN(a.prioridade) FROM arquivo a
         WHERE a.conteudo_id = c.id AND a.presente = 1) AS prioridade,
       (SELECT COUNT(*) FROM arquivo a
         WHERE a.conteudo_id = c.id AND a.presente = 1) AS ocorrencias_vivas
FROM conteudo c
LEFT JOIN indice_conteudo i ON i.conteudo_id = c.id
WHERE c.estado IN ('EXTRAIDO', 'PARCIAL')
  AND EXISTS (SELECT 1 FROM arquivo a
               WHERE a.conteudo_id = c.id AND a.presente = 1)
  AND ( i.conteudo_id IS NULL
     OR i.estado = 'REMOVIDO'
     OR i.versao_extrator IS NOT c.versao_extrator
     OR i.versao_normalizador <>
        (SELECT valor FROM meta WHERE chave = 'busca.versao_normalizador')
     OR i.versao_chunker <>
        (SELECT valor FROM meta WHERE chave = 'busca.versao_chunker') )
ORDER BY prioridade ASC, c.id ASC;


-- ------------------------------------------------------------
-- v_busca_saude: os contadores que TEM de ser zero.
--
-- Mesma ideia da consulta 3 de `sql/consultas_busca.sql`, que existe para
-- que a deriva de contador nao volte em silencio. Aqui a pergunta e:
-- "o indice invertido e a tabela de trechos contam a mesma historia?"
--
-- `a_reindexar` e o unico numero que pode ser diferente de zero — e ele e
-- fila, nao defeito.
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_busca_saude;
CREATE VIEW v_busca_saude AS
SELECT
  (SELECT COUNT(*) FROM busca_fts) - (SELECT COUNT(*) FROM trecho)
      AS fts_menos_trecho,          -- 0: o invertido nao tem sobra nem falta
  (SELECT COUNT(*) FROM trecho t
    WHERE NOT EXISTS (SELECT 1 FROM segmento s WHERE s.id = t.segmento_id))
      AS trecho_sem_segmento,       -- 0
  (SELECT COUNT(*) FROM trecho t
    WHERE NOT EXISTS (SELECT 1 FROM conteudo c WHERE c.id = t.conteudo_id))
      AS trecho_sem_conteudo,       -- 0
  (SELECT COUNT(*) FROM segmento s
    WHERE NOT EXISTS (SELECT 1 FROM conteudo c WHERE c.id = s.conteudo_id))
      AS segmento_sem_conteudo,     -- 0
  (SELECT COUNT(*) FROM trecho t
    WHERE NOT EXISTS (SELECT 1 FROM indice_conteudo i
                       WHERE i.conteudo_id = t.conteudo_id))
      AS trecho_sem_indice,         -- 0: todo trecho tem contabilidade
  (SELECT COUNT(*) FROM indice_conteudo i
    WHERE i.estado = 'INDEXADO'
      AND i.trechos <> (SELECT COUNT(*) FROM trecho t
                         WHERE t.conteudo_id = i.conteudo_id))
      AS contador_trechos_divergente,  -- 0
  (SELECT COUNT(*) FROM indice_conteudo i
    WHERE i.estado <> 'INDEXADO'
      AND EXISTS (SELECT 1 FROM trecho t WHERE t.conteudo_id = i.conteudo_id))
      AS trecho_de_nao_indexado,    -- 0: SEM_TEXTO/REMOVIDO nao tem trecho
  (SELECT COUNT(*) FROM v_reindexar)
      AS a_reindexar;               -- fila, pode ser > 0


-- ------------------------------------------------------------
-- v_busca_cobertura: "quanto do acervo eu consigo achar hoje?"
--
-- Conta OCORRENCIAS, nao conteudos — que e a pergunta de quem procura um
-- documento. Por prioridade, porque P0 achavel vale mais que P4 achavel.
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_busca_cobertura;
CREATE VIEW v_busca_cobertura AS
SELECT a.prioridade,
       COUNT(*)                                          AS ocorrencias_vivas,
       SUM(a.suportado)                                  AS suportadas,
       SUM(CASE WHEN i.estado = 'INDEXADO' THEN 1 ELSE 0 END)
                                                         AS pesquisaveis,
       SUM(CASE WHEN i.estado = 'SEM_TEXTO' THEN 1 ELSE 0 END)
                                                         AS sem_texto,
       ROUND(100.0 * SUM(CASE WHEN i.estado = 'INDEXADO' THEN 1 ELSE 0 END)
             / COUNT(*), 1)                              AS pct_pesquisavel
FROM arquivo a
LEFT JOIN indice_conteudo i ON i.conteudo_id = a.conteudo_id
WHERE a.presente = 1 AND a.temporario = 0
GROUP BY a.prioridade
ORDER BY a.prioridade;


-- ------------------------------------------------------------
-- v_conteudo_sem_ocorrencia: indexado, mas hoje sem nenhum lugar fisico.
--
-- Acontece quando a ULTIMA ocorrencia de um conteudo vira MISSING. Pela
-- REGRA 6 isto NAO e apagado automaticamente: o item pode reaparecer no
-- delta (`DEC-029`) e, se reaparecer, o indice ja esta pronto — nada
-- reextrai, nada reindexa.
--
-- Esta view e a fila de uma eventual limpeza MANUAL, decidida pela Emmily,
-- com janela de carencia. Nunca um job automatico.
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_conteudo_sem_ocorrencia;
CREATE VIEW v_conteudo_sem_ocorrencia AS
SELECT i.conteudo_id,
       c.quick_xor,
       c.tamanho_bytes,
       i.trechos,
       i.caracteres,
       i.indexado_em,
       (SELECT COUNT(*) FROM arquivo a WHERE a.conteudo_id = i.conteudo_id)
           AS ocorrencias_no_catalogo,
       (SELECT MAX(a.ultima_vez_em) FROM arquivo a
         WHERE a.conteudo_id = i.conteudo_id) AS vista_pela_ultima_vez
FROM indice_conteudo i
JOIN conteudo c ON c.id = i.conteudo_id
WHERE i.estado = 'INDEXADO'
  AND NOT EXISTS (SELECT 1 FROM arquivo a
                   WHERE a.conteudo_id = i.conteudo_id AND a.presente = 1)
ORDER BY vista_pela_ultima_vez;


-- ============================================================
-- 7. MIGRACAO DO `texto_fts` ATUAL
--
-- SITUACAO MEDIDA em 2026-09-27 (leitura do banco real):
--     texto_fts            4.361 linhas
--     arquivo_id distintos    89
--     conteudo               30 linhas (27 EXTRAIDO, 2 PARCIAL, 1 OCR)
--     conteudo.trechos    4.200 no total
--
-- CORRIGIDO em 2026-10-04: as 161 linhas "a mais" NAO sao lixo. Sao trechos
-- de 60 arquivos extraidos na era local (2026-09-18) que nunca ganharam
-- `conteudo_id` — 56 presentes e COMPLETED. Ficam fora do v5 ate ganharem
-- `conteudo` (docs/09_BUSCA.md, secao 13).
--
-- DECISAO: nao se copia linha de `texto_fts` para `trecho` (sem
-- `conteudo_id`, sem posicao, sem normalizacao). O texto de cada pagina e
-- RECONSTRUIDO dos trechos v4 (o corte 1.0 e deterministico), CONFERIDO
-- (recortado de novo da os mesmos trechos; unido da o derivado byte a byte)
-- e indexado de novo pelo corte 2.0, com pagina/slide/aba preservados.
-- Sem a conferencia, cai para o derivado (que perde a pagina, exceto em
-- planilha). Ensaio na copia do banco real: 29 de 29 reconstruidos.
--
-- PROCEDIMENTO: `scripts/05_migrar_v5.py` (simulacao; `--aplicar` com lock
-- do worker, backup, transacao unica e conferencias). O `texto_fts` FICA NO
-- BANCO; aposentar e passo separado, a mao, depois da troca do
-- `worker.indexar()` (docs/09_BUSCA.md, secao 11) e de uma rodada limpa.
--
-- NAO DESCOMENTAR SEM A CONFERENCIA DO PASSO 4:
-- DROP TABLE IF EXISTS texto_fts;
-- ============================================================


-- ============================================================
-- 8. A CONSULTA CANONICA DA BUSCA
--
-- Nao vira view: `MATCH` nao atravessa view. Fica aqui como contrato.
-- A implementacao que vale e `busca.SQL_BUSCA` (scripts/busca.py), que
-- alem do desenho abaixo descarta, ANTES do LIMIT, o conteudo sem
-- ocorrencia presente — senao ele ocuparia vaga no top-N e sumiria no JOIN.
--
-- Duas etapas, sempre:
--   (A) o FTS5 decide QUAIS trechos, e em que ordem (bm25, via `rank`);
--   (B) o SQL comum expande cada trecho nas ocorrencias fisicas.
--
-- O LIMIT vai na etapa (A), sobre CONTEUDOS. Se ficasse na etapa (B), um
-- documento copiado em 30 pastas ocuparia a primeira pagina inteira
-- sozinho. A unidade do resultado e o CONTEUDO; as ocorrencias sao um
-- atributo dele.
--
--   WITH acerto AS (
--       SELECT rowid AS trecho_id, rank
--       FROM busca_fts
--       WHERE busca_fts MATCH :consulta
--       ORDER BY rank
--       LIMIT 500
--   ),
--   melhor AS (                        -- o melhor trecho de cada conteudo
--       SELECT t.conteudo_id,
--              MIN(a.rank)                                      AS rank,
--              (SELECT a2.trecho_id FROM acerto a2
--                 JOIN trecho t2 ON t2.id = a2.trecho_id
--                WHERE t2.conteudo_id = t.conteudo_id
--                ORDER BY a2.rank LIMIT 1)                      AS trecho_id
--       FROM acerto a JOIN trecho t ON t.id = a.trecho_id
--       GROUP BY t.conteudo_id
--       ORDER BY rank
--       LIMIT 50
--   )
--   SELECT m.rank,
--          v.conteudo_id,
--          v.segmento_tipo, v.segmento_numero, v.segmento_rotulo,
--          v.char_inicio, v.char_fim,
--          v.arquivo_id, v.item_id, v.drive_id,
--          v.caminho_rel, v.web_url, v.prioridade,
--          v.modificado_por, v.modificado_em_nuvem,
--          snippet(busca_fts, 0, '[', ']', ' ... ', 14) AS contexto
--   FROM melhor m
--   JOIN v_trecho_ocorrencia v ON v.trecho_id = m.trecho_id
--   JOIN busca_fts ON busca_fts.rowid = m.trecho_id
--   WHERE busca_fts MATCH :consulta
--   ORDER BY m.rank, v.prioridade ASC, v.caminho_rel;
--
-- Cada linha do resultado carrega, sem ambiguidade:
--   onde no documento  -> segmento_tipo + segmento_numero + segmento_rotulo
--                         + char_inicio/char_fim
--   qual documento     -> conteudo_id (os bytes)
--   onde no SharePoint -> drive_id + item_id + caminho_rel + web_url
--   o que dizer disso  -> snippet, com o termo destacado
--
-- E isso e o `caminho_rel` + identidade + data + origem que a REGRA 9 pede.
-- ============================================================
