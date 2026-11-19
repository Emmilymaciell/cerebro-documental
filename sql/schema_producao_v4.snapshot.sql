# Retrato normalizado do schema — gerado por scripts/20_snapshot_schema.py
# fonte: teste.db (aberto em modo somente leitura) em 2026-10-04T13:11:04+00:00
# Linhas com '#' sao ignoradas na comparacao. NAO editar a mao: regenerar.
# tests/test_schema.py exige que a receita do repositorio produza EXATAMENTE isto.
# procedencia: copia de data/cerebro.db de 2026-10-04 + scripts/04_migrar_v4.py (remove o indice duplicado ix_arquivo_quickxor, M-12). Producao hoje = isto + ix_arquivo_quickxor.
TABLE arquivo
  COL id INTEGER PK1
  COL caminho_rel TEXT NOT NULL
  COL biblioteca TEXT
  COL pasta_rel TEXT NOT NULL
  COL nome TEXT NOT NULL
  COL ext TEXT
  COL categoria TEXT
  COL nome_normalizado TEXT
  COL profundidade INTEGER
  COL comprimento_caminho INTEGER
  COL tamanho_bytes INTEGER
  COL mtime_utc TEXT
  COL ctime_utc TEXT
  COL atributos INTEGER
  COL online INTEGER NOT NULL DEFAULT 0
  COL fixado INTEGER NOT NULL DEFAULT 0
  COL temporario INTEGER NOT NULL DEFAULT 0
  COL hidratado_por_nos INTEGER NOT NULL DEFAULT 0
  COL prioridade INTEGER NOT NULL DEFAULT 3
  COL suportado INTEGER NOT NULL DEFAULT 0
  COL estado TEXT NOT NULL DEFAULT 'INVENTORIED'
  COL sha256 TEXT
  COL hash_em TEXT
  COL duplicata_de INTEGER
  COL tentativas INTEGER NOT NULL DEFAULT 0
  COL ultimo_erro TEXT
  COL proxima_tentativa_em TEXT
  COL presente INTEGER NOT NULL DEFAULT 1
  COL primeira_vez_em TEXT NOT NULL
  COL ultima_vez_em TEXT NOT NULL
  COL ultima_execucao_id INTEGER
  COL alterado_em TEXT
  COL processado_em TEXT
  COL drive_id TEXT
  COL item_id TEXT
  COL parent_id TEXT
  COL etag TEXT
  COL ctag TEXT
  COL quick_xor TEXT
  COL web_url TEXT
  COL criado_por TEXT
  COL modificado_por TEXT
  COL criado_em_nuvem TEXT
  COL modificado_em_nuvem TEXT
  COL presente_nuvem INTEGER NOT NULL DEFAULT 0
  COL origem TEXT NOT NULL DEFAULT 'local'
  COL visto_nuvem_em TEXT
  COL caminho_anterior TEXT
  COL caminho_nuvem TEXT
  COL conteudo_id INTEGER
  FK ultima_execucao_id -> execucao(id) ON DELETE NO ACTION ON UPDATE NO ACTION
  FK duplicata_de -> arquivo(id) ON DELETE NO ACTION ON UPDATE NO ACTION
  AUTOINCREMENT
TABLE checkpoint
  COL id INTEGER PK1
  COL execucao_id INTEGER
  COL em TEXT NOT NULL
  COL estado_worker TEXT
  COL ultimo_item_id INTEGER
  COL ultimo_item TEXT
  COL concluidos INTEGER
  COL pendentes INTEGER
  COL erros INTEGER
  COL bytes_processados INTEGER
  COL derivados_mb REAL
  COL espaco_livre_gb REAL
  COL observacao TEXT
  FK execucao_id -> execucao(id) ON DELETE NO ACTION ON UPDATE NO ACTION
  AUTOINCREMENT
TABLE conteudo
  COL id INTEGER PK1
  COL quick_xor TEXT NOT NULL
  COL tamanho_bytes INTEGER NOT NULL
  COL evidencia TEXT NOT NULL DEFAULT 'FORTE'
  COL representante_id INTEGER
  COL extraido_de_item TEXT
  COL estado TEXT NOT NULL DEFAULT 'PENDENTE'
  COL extrator TEXT
  COL versao_extrator TEXT
  COL texto_path TEXT
  COL perfil_path TEXT
  COL paginas INTEGER
  COL caracteres INTEGER
  COL trechos INTEGER
  COL bytes_texto INTEGER
  COL ocorrencias INTEGER NOT NULL DEFAULT 1
  COL reeleicoes INTEGER NOT NULL DEFAULT 0
  COL primeira_vez_em TEXT
  COL atualizado_em TEXT
  FK representante_id -> arquivo(id) ON DELETE NO ACTION ON UPDATE NO ACTION
  UNIQUE (quick_xor, tamanho_bytes)
  AUTOINCREMENT
TABLE controle
  COL id INTEGER PK1
  COL comando TEXT NOT NULL DEFAULT 'RUN'
  COL estado TEXT NOT NULL DEFAULT 'PARADO'
  COL motivo TEXT
  COL pid INTEGER
  COL host TEXT
  COL execucao_id INTEGER
  COL item_atual TEXT
  COL item_atual_id INTEGER
  COL iniciado_em TEXT
  COL heartbeat TEXT
  COL atualizado_em TEXT
  COL processados_sessao INTEGER NOT NULL DEFAULT 0
  COL bytes_sessao INTEGER NOT NULL DEFAULT 0
  COL erros_sessao INTEGER NOT NULL DEFAULT 0
  CHECK (id = 1)
TABLE delta_token
  COL drive_id TEXT PK1
  COL delta_link TEXT
  COL next_link TEXT
  COL status TEXT NOT NULL DEFAULT 'nunca_rodou'
  COL paginas INTEGER NOT NULL DEFAULT 0
  COL itens_ultima INTEGER NOT NULL DEFAULT 0
  COL iniciado_em TEXT
  COL atualizado_em TEXT
  COL erro TEXT
  FK drive_id -> drive(drive_id) ON DELETE NO ACTION ON UPDATE NO ACTION
TABLE drive
  COL drive_id TEXT PK1
  COL nome TEXT
  COL tipo TEXT
  COL web_url TEXT
  COL site_id TEXT
  COL usado_bytes INTEGER
  COL no_escopo INTEGER NOT NULL DEFAULT 1
  COL observacao TEXT
  COL atualizado_em TEXT
TABLE evento_arquivo
  COL id INTEGER PK1
  COL arquivo_id INTEGER NOT NULL
  COL execucao_id INTEGER
  COL evento TEXT NOT NULL
  COL detalhe TEXT
  COL em TEXT NOT NULL
  FK execucao_id -> execucao(id) ON DELETE NO ACTION ON UPDATE NO ACTION
  FK arquivo_id -> arquivo(id) ON DELETE NO ACTION ON UPDATE NO ACTION
  AUTOINCREMENT
TABLE evento_nuvem
  COL id INTEGER PK1
  COL drive_id TEXT NOT NULL
  COL item_id TEXT NOT NULL
  COL evento TEXT NOT NULL
  COL detalhe TEXT
  COL execucao_id INTEGER
  COL em TEXT NOT NULL
  AUTOINCREMENT
TABLE execucao
  COL id INTEGER PK1
  COL etapa TEXT NOT NULL
  COL escopo TEXT
  COL argumentos TEXT
  COL iniciada_em TEXT NOT NULL
  COL encerrada_em TEXT
  COL status TEXT NOT NULL DEFAULT 'em_andamento'
  COL diretorios_vistos INTEGER NOT NULL DEFAULT 0
  COL arquivos_vistos INTEGER NOT NULL DEFAULT 0
  COL arquivos_novos INTEGER NOT NULL DEFAULT 0
  COL arquivos_alterados INTEGER NOT NULL DEFAULT 0
  COL arquivos_sumidos INTEGER NOT NULL DEFAULT 0
  COL erros INTEGER NOT NULL DEFAULT 0
  COL bytes_processados INTEGER NOT NULL DEFAULT 0
  COL bytes_hidratados INTEGER NOT NULL DEFAULT 0
  COL duracao_s REAL
  COL host TEXT
  COL observacao TEXT
  AUTOINCREMENT
TABLE extracao
  COL id INTEGER PK1
  COL arquivo_id INTEGER NOT NULL
  COL execucao_id INTEGER
  COL extrator TEXT NOT NULL
  COL versao TEXT NOT NULL
  COL sha256_fonte TEXT
  COL status TEXT NOT NULL
  COL paginas INTEGER
  COL paginas_com_texto INTEGER
  COL slides INTEGER
  COL abas INTEGER
  COL imagens INTEGER
  COL caracteres INTEGER
  COL palavras INTEGER
  COL densidade_texto REAL
  COL texto_path TEXT
  COL perfil_path TEXT
  COL bytes_texto INTEGER
  COL trechos INTEGER
  COL erro TEXT
  COL tentativas INTEGER NOT NULL DEFAULT 1
  COL duracao_ms INTEGER
  COL executado_em TEXT NOT NULL
  FK execucao_id -> execucao(id) ON DELETE NO ACTION ON UPDATE NO ACTION
  FK arquivo_id -> arquivo(id) ON DELETE NO ACTION ON UPDATE NO ACTION
  UNIQUE (arquivo_id, extrator)
  AUTOINCREMENT
TABLE item_nuvem
  COL drive_id TEXT NOT NULL PK1
  COL item_id TEXT NOT NULL PK2
  COL parent_id TEXT
  COL nome TEXT
  COL caminho_rel TEXT
  COL caminho_anterior TEXT
  COL eh_pasta INTEGER NOT NULL DEFAULT 0
  COL ext TEXT
  COL tamanho_bytes INTEGER
  COL quick_xor TEXT
  COL sha1 TEXT
  COL etag TEXT
  COL ctag TEXT
  COL web_url TEXT
  COL criado_em TEXT
  COL modificado_em TEXT
  COL criado_por TEXT
  COL modificado_por TEXT
  COL presente INTEGER NOT NULL DEFAULT 1
  COL primeira_vez_em TEXT
  COL ultima_vez_em TEXT
  COL removido_em TEXT
  COL execucao_id INTEGER
TABLE log
  COL id INTEGER PK1
  COL execucao_id INTEGER
  COL nivel TEXT NOT NULL
  COL etapa TEXT
  COL caminho_rel TEXT
  COL mensagem TEXT NOT NULL
  COL em TEXT NOT NULL
  FK execucao_id -> execucao(id) ON DELETE NO ACTION ON UPDATE NO ACTION
  AUTOINCREMENT
TABLE meta
  COL chave TEXT PK1
  COL valor TEXT NOT NULL
  COL atualizado_em TEXT NOT NULL
TABLE pasta
  COL caminho_rel TEXT PK1
  COL biblioteca TEXT
  COL nome TEXT
  COL nivel INTEGER
  COL obra TEXT
  COL categoria_doc TEXT
  COL critica INTEGER NOT NULL DEFAULT 0
  COL prioridade INTEGER NOT NULL DEFAULT 3
  COL motivo TEXT
  COL arquivos INTEGER NOT NULL DEFAULT 0
  COL arquivos_suportados INTEGER NOT NULL DEFAULT 0
  COL bytes INTEGER NOT NULL DEFAULT 0
  COL classificado_em TEXT
TABLE progresso_diretorio
  COL pasta_rel TEXT PK1
  COL execucao_id INTEGER
  COL arquivos INTEGER NOT NULL DEFAULT 0
  COL subpastas INTEGER NOT NULL DEFAULT 0
  COL bytes INTEGER NOT NULL DEFAULT 0
  COL concluido_em TEXT
  COL erro TEXT
  FK execucao_id -> execucao(id) ON DELETE NO ACTION ON UPDATE NO ACTION
TABLE reconciliacao
  COL id INTEGER PK1
  COL execucao_id INTEGER
  COL arquivo_id INTEGER
  COL drive_id TEXT
  COL item_id TEXT
  COL caminho_local TEXT
  COL caminho_nuvem TEXT
  COL metodo TEXT
  COL confianca TEXT NOT NULL
  COL aplicado INTEGER NOT NULL DEFAULT 0
  COL conteudo_mudou INTEGER NOT NULL DEFAULT 0
  COL candidatos INTEGER NOT NULL DEFAULT 1
  COL detalhe TEXT
  COL em TEXT NOT NULL
  AUTOINCREMENT
VIRTUAL texto_fts :: CREATE VIRTUAL TABLE texto_fts USING fts5(arquivo_id UNINDEXED, caminho_rel UNINDEXED, pagina UNINDEXED, trecho_idx UNINDEXED, texto, tokenize = unicode61 remove_diacritics 2)
TABLE texto_fts_config
  COL k  NOT NULL PK1
  COL v 
  WITHOUT ROWID
TABLE texto_fts_content
  COL id INTEGER PK1
  COL c0 
  COL c1 
  COL c2 
  COL c3 
  COL c4 
TABLE texto_fts_data
  COL id INTEGER PK1
  COL block BLOB
TABLE texto_fts_docsize
  COL id INTEGER PK1
  COL sz BLOB
TABLE texto_fts_idx
  COL segid  NOT NULL PK1
  COL term  NOT NULL PK2
  COL pgno 
  WITHOUT ROWID
INDEX ix_arquivo_biblioteca ON arquivo (biblioteca)
INDEX ix_arquivo_caminho ON arquivo (caminho_rel)
INDEX ix_arquivo_categoria ON arquivo (categoria)
INDEX ix_arquivo_conteudo ON arquivo (conteudo_id)
INDEX ix_arquivo_estado ON arquivo (estado)
INDEX ix_arquivo_fila ON arquivo (estado, prioridade, online, tamanho_bytes)
INDEX ix_arquivo_norm ON arquivo (nome_normalizado)
INDEX ix_arquivo_pasta ON arquivo (pasta_rel)
INDEX ix_arquivo_qxh ON arquivo (quick_xor)
INDEX ix_arquivo_retry ON arquivo (estado, proxima_tentativa_em)
INDEX ix_arquivo_sha ON arquivo (sha256)
INDEX ix_checkpoint_em ON checkpoint (em)
INDEX ix_conteudo_estado ON conteudo (estado)
INDEX ix_conteudo_repr ON conteudo (representante_id)
INDEX ix_evento_arquivo ON evento_arquivo (arquivo_id, em)
INDEX ix_evento_nuvem_item ON evento_nuvem (drive_id, item_id, em)
INDEX ix_evento_nuvem_tipo ON evento_nuvem (evento, em)
INDEX ix_evento_tipo ON evento_arquivo (evento, em)
INDEX ix_extracao_status ON extracao (status)
INDEX ix_item_caminho ON item_nuvem (caminho_rel)
INDEX ix_item_hash ON item_nuvem (quick_xor)
INDEX ix_item_hash_tam ON item_nuvem (quick_xor, tamanho_bytes)
INDEX ix_item_nome_tam ON item_nuvem (nome, tamanho_bytes)
INDEX ix_item_pai ON item_nuvem (drive_id, parent_id)
INDEX ix_item_presente ON item_nuvem (presente, eh_pasta)
INDEX ix_log_nivel ON log (nivel, em)
INDEX ix_nuvem_caminho ON item_nuvem (drive_id, caminho_rel)
INDEX ix_pasta_obra ON pasta (obra, categoria_doc)
INDEX ix_pasta_prioridade ON pasta (prioridade)
INDEX ix_reconc_arq ON reconciliacao (arquivo_id)
INDEX ix_reconc_conf ON reconciliacao (confianca, aplicado)
INDEX ix_reconc_item ON reconciliacao (drive_id, item_id)
INDEX ux_arquivo_item ON arquivo (drive_id, item_id) UNIQUE WHERE item_id IS NOT NULL
VIEW v_cobertura :: CREATE VIEW v_cobertura AS SELECT categoria, COUNT(*)AS arquivos, COALESCE(SUM(tamanho_bytes), 0)AS bytes, SUM(online)AS so_na_nuvem, SUM(estado = 'COMPLETED')AS concluidos, SUM(estado = 'OCR_REQUIRED')AS aguardando_ocr, SUM(estado = 'UNSUPPORTED')AS sem_extrator, SUM(estado IN('ERROR_RETRYABLE', 'ERROR_FINAL'))AS com_erro, ROUND(100.0 * SUM(estado = 'COMPLETED')/ COUNT(*), 1)AS pct_concluido FROM arquivo WHERE presente = 1 AND temporario = 0 GROUP BY categoria ORDER BY arquivos DESC
VIEW v_cobertura_conteudo :: CREATE VIEW v_cobertura_conteudo AS SELECT c.estado AS estado_conteudo, COUNT(DISTINCT c.id)AS conteudos, COUNT(a.id)AS ocorrencias, COALESCE(SUM(c.caracteres), 0)AS caracteres, COALESCE(SUM(c.trechos), 0)AS trechos FROM conteudo c LEFT JOIN arquivo a ON a.conteudo_id = c.id AND a.presente = 1 GROUP BY c.estado ORDER BY ocorrencias DESC
VIEW v_cobertura_prioridade :: CREATE VIEW v_cobertura_prioridade AS SELECT prioridade, COUNT(*)AS arquivos, SUM(suportado)AS suportados, SUM(estado = 'COMPLETED')AS concluidos, SUM(estado IN('QUEUED', 'CHANGED', 'READY', 'WAITING_HYDRATION', 'ERROR_RETRYABLE'))AS na_fila, COALESCE(SUM(tamanho_bytes), 0)AS bytes FROM arquivo WHERE presente = 1 AND temporario = 0 GROUP BY prioridade ORDER BY prioridade
VIEW v_conteudo_orfao :: CREATE VIEW v_conteudo_orfao AS SELECT c.id AS conteudo_id, c.quick_xor, c.estado, c.representante_id, (SELECT COUNT(*)FROM arquivo a WHERE a.conteudo_id = c.id AND a.presente = 1)AS ocorrencias_vivas FROM conteudo c LEFT JOIN arquivo r ON r.id = c.representante_id WHERE c.representante_id IS NULL OR r.presente = 0 OR r.id IS NULL
VIEW v_duplicata :: CREATE VIEW v_duplicata AS SELECT sha256, COUNT(*)AS copias, SUM(tamanho_bytes)AS bytes_total, MIN(caminho_rel)AS exemplo FROM arquivo WHERE sha256 IS NOT NULL AND presente = 1 GROUP BY sha256 HAVING COUNT(*)> 1 ORDER BY copias DESC
VIEW v_duplicata_nuvem :: CREATE VIEW v_duplicata_nuvem AS SELECT quick_xor, COUNT(*)AS copias, SUM(tamanho_bytes)AS bytes_total, SUM(tamanho_bytes)- MIN(tamanho_bytes)AS bytes_redundantes, MIN(caminho_rel)AS exemplo FROM item_nuvem WHERE presente = 1 AND eh_pasta = 0 AND quick_xor IS NOT NULL AND tamanho_bytes > 0 GROUP BY quick_xor HAVING COUNT(*)> 1 ORDER BY bytes_redundantes DESC
VIEW v_excecoes_reconciliacao :: CREATE VIEW v_excecoes_reconciliacao AS SELECT confianca, metodo, caminho_local, caminho_nuvem, candidatos, detalhe, em FROM reconciliacao WHERE confianca IN('AMBIGUO', 'SEM_CORRESPONDENCIA')ORDER BY confianca, caminho_local
VIEW v_familia_versao :: CREATE VIEW v_familia_versao AS SELECT nome_normalizado, COUNT(*)AS variantes, MAX(mtime_utc)AS mais_recente, MIN(caminho_rel)AS exemplo FROM arquivo WHERE presente = 1 AND temporario = 0 AND nome_normalizado <> '' GROUP BY nome_normalizado HAVING COUNT(*)> 1 ORDER BY variantes DESC
VIEW v_fila :: CREATE VIEW v_fila AS SELECT id, caminho_rel, categoria, prioridade, tamanho_bytes, online, estado, tentativas, proxima_tentativa_em, drive_id, item_id FROM arquivo WHERE presente = 1 AND suportado = 1 AND estado IN('QUEUED', 'CHANGED', 'READY', 'WAITING_HYDRATION', 'ERROR_RETRYABLE')AND(proxima_tentativa_em IS NULL OR proxima_tentativa_em <= datetime('now'))ORDER BY prioridade ASC, online ASC, tamanho_bytes ASC
VIEW v_fila_nuvem :: CREATE VIEW v_fila_nuvem AS SELECT id, caminho_rel, caminho_nuvem, categoria, prioridade, tamanho_bytes, estado, tentativas, drive_id, item_id, quick_xor FROM arquivo WHERE presente = 1 AND presente_nuvem = 1 AND suportado = 1 AND drive_id IS NOT NULL AND item_id IS NOT NULL AND estado IN('QUEUED', 'CHANGED', 'READY', 'ERROR_RETRYABLE', 'WAITING_HYDRATION')AND(proxima_tentativa_em IS NULL OR proxima_tentativa_em <= datetime('now'))ORDER BY prioridade ASC, tamanho_bytes ASC
VIEW v_migracao :: CREATE VIEW v_migracao AS SELECT(SELECT COUNT(*)FROM arquivo WHERE presente = 1)AS linhas_antigas, (SELECT COUNT(*)FROM arquivo WHERE item_id IS NOT NULL)AS com_item_id, (SELECT COUNT(*)FROM arquivo WHERE item_id IS NULL AND presente = 1)AS sem_item_id, (SELECT COUNT(*)FROM arquivo WHERE estado = 'COMPLETED')AS completos, (SELECT COUNT(*)FROM item_nuvem WHERE eh_pasta = 0 AND presente = 1)AS arquivos_na_nuvem
VIEW v_nuvem_resumo :: CREATE VIEW v_nuvem_resumo AS SELECT d.nome AS biblioteca, SUM(i.eh_pasta = 0 AND i.presente = 1)AS arquivos, SUM(i.eh_pasta = 1 AND i.presente = 1)AS pastas, SUM(CASE WHEN i.eh_pasta = 0 AND i.presente = 1 THEN i.tamanho_bytes ELSE 0 END)AS bytes, SUM(i.presente = 0)AS removidos, SUM(i.eh_pasta = 0 AND i.presente = 1 AND i.quick_xor IS NOT NULL)AS com_hash FROM item_nuvem i LEFT JOIN drive d ON d.drive_id = i.drive_id GROUP BY d.nome
VIEW v_ocorrencia :: CREATE VIEW v_ocorrencia AS SELECT c.id AS conteudo_id, c.quick_xor, c.estado AS estado_conteudo, c.texto_path, c.representante_id, a.id AS arquivo_id, (a.id = c.representante_id)AS eh_representante, a.drive_id, a.item_id, a.caminho_rel, a.caminho_nuvem, a.web_url, a.prioridade, a.estado AS estado_ocorrencia, a.criado_em_nuvem, a.modificado_em_nuvem, a.criado_por, a.modificado_por FROM conteudo c JOIN arquivo a ON a.conteudo_id = c.id WHERE a.presente = 1
VIEW v_progresso :: CREATE VIEW v_progresso AS SELECT estado, COUNT(*)AS arquivos, COALESCE(SUM(tamanho_bytes), 0)AS bytes FROM arquivo WHERE presente = 1 GROUP BY estado ORDER BY arquivos DESC
META schema_versao=4
