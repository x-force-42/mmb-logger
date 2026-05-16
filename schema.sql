-- mmb-logger schema v1
-- Modelo conceitual: 1 épico → N ciclos → N eventos.

CREATE TABLE IF NOT EXISTS epicos (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL,
  started_at TEXT NOT NULL,
  intencao TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('aberto', 'fechado')),
  closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_epicos_status ON epicos(status);
CREATE INDEX IF NOT EXISTS idx_epicos_started_at ON epicos(started_at);

CREATE TABLE IF NOT EXISTS ciclos (
  id TEXT PRIMARY KEY,
  epico_id TEXT NOT NULL REFERENCES epicos(id) ON DELETE CASCADE,
  project TEXT NOT NULL,
  planner_invoked_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('iniciado', 'planejado', 'pr_aberto', 'completo', 'abortado')),
  instruction TEXT NOT NULL,
  briefing_md TEXT,
  pr_url TEXT,
  pr_number INTEGER,
  closed_partial_at TEXT,
  closed_complete_at TEXT,
  merged_to_main INTEGER CHECK (merged_to_main IN (0, 1) OR merged_to_main IS NULL),
  assertiveness_score INTEGER CHECK
    (assertiveness_score BETWEEN 1 AND 5 OR assertiveness_score IS NULL),
  review_note TEXT,
  abort_at TEXT,
  abort_origin TEXT CHECK
    (abort_origin IN ('heartbeat', 'manual', 'self', 'master') OR abort_origin IS NULL),
  abort_reason TEXT,
  cost_usd REAL,
  tokens_input INTEGER,
  tokens_output INTEGER,
  diff_added INTEGER,
  diff_deleted INTEGER,
  diff_files INTEGER
);

CREATE INDEX IF NOT EXISTS idx_ciclos_epico ON ciclos(epico_id);
CREATE INDEX IF NOT EXISTS idx_ciclos_status ON ciclos(status);
CREATE INDEX IF NOT EXISTS idx_ciclos_project ON ciclos(project);
CREATE INDEX IF NOT EXISTS idx_ciclos_planner_invoked_at ON ciclos(planner_invoked_at);

CREATE TABLE IF NOT EXISTS eventos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ciclo_id TEXT REFERENCES ciclos(id) ON DELETE CASCADE,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  severity TEXT CHECK
    (severity IN ('info', 'warn', 'error', 'critical') OR severity IS NULL),
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_eventos_ciclo ON eventos(ciclo_id);
CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos(ts);
CREATE INDEX IF NOT EXISTS idx_eventos_kind ON eventos(kind);

CREATE TABLE IF NOT EXISTS projetos (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  repo_url TEXT,
  created_at TEXT NOT NULL
);

-- Estado da ingestão pra idempotência.
-- inbox: cada arquivo já processado registra path.
-- jsonl: por source, registra última linha lida.

CREATE TABLE IF NOT EXISTS processed_files (
  path TEXT PRIMARY KEY,
  processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jsonl_cursor (
  source TEXT PRIMARY KEY CHECK (source IN ('journal', 'agents')),
  last_offset INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
