-- ============================================================
-- CEREBRO DOCUMENTAL — as consultas que provam o modelo v4
--
-- Pergunta de fundo: "o Cerebro extraiu UMA vez; ele ainda sabe TODOS os
-- lugares onde aquele documento aparece?"
--
-- Resposta em tres consultas. Rodar com:
--     python scripts\98_consultar.py --sql sql\consultas_busca.sql
-- ou abrindo data\cerebro.db em qualquer cliente SQLite.
-- ============================================================


-- ------------------------------------------------------------
-- 1. Um conteudo, varias ocorrencias.
--
-- Cada linha e uma ocorrencia documental inteira: seu proprio
-- (drive_id, item_id), seu caminho, sua prioridade, sua autoria e suas
-- datas. O que elas compartilham e o CONTEUDO — os bytes, o texto
-- extraido, o derivado. `eh_representante` marca de qual delas os bytes
-- foram lidos; as outras nao foram baixadas.
-- ------------------------------------------------------------
SELECT c.id                AS conteudo,
       c.estado            AS estado_conteudo,
       c.ocorrencias       AS quantas,
       o.arquivo_id,
       CASE o.eh_representante WHEN 1 THEN 'representante' ELSE '' END AS papel,
       o.item_id,
       o.drive_id,
       o.prioridade,
       o.estado_ocorrencia,
       o.criado_por,
       o.modificado_em_nuvem,
       o.caminho_rel
FROM conteudo c
JOIN v_ocorrencia o ON o.conteudo_id = c.id
WHERE c.ocorrencias > 1
ORDER BY c.ocorrencias DESC, c.id, o.eh_representante DESC, o.arquivo_id;


-- ------------------------------------------------------------
-- 2. Da busca textual de volta para TODOS os lugares.
--
-- O indice FTS guarda o trecho sob o `arquivo_id` de quem foi extraido —
-- o representante. Esta consulta vai do acerto no indice ate o conteudo,
-- e do conteudo ate cada ocorrencia. E por isso que procurar uma palavra
-- devolve as 10 pastas onde o documento esta, e nao so uma.
--
-- Trocar 'medicao' pelo termo procurado.
-- ------------------------------------------------------------
SELECT achado.trecho_idx,
       achado.pagina,
       c.id                AS conteudo,
       o.arquivo_id        AS onde_esta,
       CASE o.eh_representante WHEN 1 THEN 'representante' ELSE 'copia' END AS papel,
       o.item_id,
       o.caminho_rel,
       snippet(texto_fts, 4, '[', ']', '...', 12) AS contexto
FROM texto_fts AS achado
JOIN arquivo   AS extraido ON extraido.id = achado.arquivo_id
JOIN conteudo  AS c        ON c.id = extraido.conteudo_id
JOIN v_ocorrencia AS o     ON o.conteudo_id = c.id
WHERE texto_fts MATCH 'medicao'
ORDER BY c.id, o.eh_representante DESC
LIMIT 50;


-- ------------------------------------------------------------
-- 3. A contabilidade: o contador bate com a realidade?
--
-- `conteudo.ocorrencias` e um numero guardado; `v_ocorrencia` conta as
-- linhas de verdade. Se as duas colunas divergirem em qualquer linha, ha
-- deriva de contador — foi um bug real, corrigido no E7, e esta consulta
-- existe para que ele nao volte em silencio. O resultado esperado e
-- NENHUMA linha.
-- ------------------------------------------------------------
SELECT c.id,
       c.quick_xor,
       c.ocorrencias                                   AS contador,
       (SELECT COUNT(*) FROM arquivo a
         WHERE a.conteudo_id = c.id AND a.presente = 1) AS real_vivas
FROM conteudo c
WHERE c.ocorrencias <> (SELECT COUNT(*) FROM arquivo a
                         WHERE a.conteudo_id = c.id AND a.presente = 1);


-- ============================================================
-- v5 (camada de busca por CONTEUDO) — so depois de
-- `scripts/05_migrar_v5.py --aplicar`. A consulta 2 acima continua
-- valendo enquanto o `texto_fts` existir; a busca nova e
-- `python scripts\busca.py buscar "termo" --banco data\cerebro.db`.
-- ============================================================

-- 4. O indice invertido e a tabela de trechos contam a mesma historia?
--    Esperado: todos os contadores 0, exceto `a_reindexar` (fila).
SELECT * FROM v_busca_saude;

-- 5. Quanto falta indexar, e por que (REGRA 7: estado, nao estimativa).
SELECT motivo, COUNT(*) AS conteudos
FROM v_reindexar
GROUP BY motivo;

-- 6. De onde veio o texto de cada indexacao, e em que estado (REGRA 9).
SELECT fonte_texto, estado, COUNT(*) AS conteudos, SUM(trechos) AS trechos
FROM indice_conteudo
GROUP BY fonte_texto, estado;

-- 7. Quanto do acervo (em OCORRENCIAS) ja e pesquisavel, por prioridade.
SELECT * FROM v_busca_cobertura;
