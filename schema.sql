-- mmb-logger schema v1
-- Modelo conceitual: 1 épico → N ciclos → N eventos.

CREATE TABLE IF NOT EXISTS epicos (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL,
  started_at TEXT NOT NULL,
  intencao TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('aberto', 'fechado')),
  closed_at TEXT,
  andaime_version TEXT
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
    (abort_origin IN ('heartbeat', 'manual', 'self', 'master',
                      'worker-exit', 'worker-timeout', 'stale')
     OR abort_origin IS NULL),
  abort_reason TEXT,
  cost_usd REAL,
  tokens_input INTEGER,
  tokens_output INTEGER,
  diff_added INTEGER,
  diff_deleted INTEGER,
  diff_files INTEGER,
  andaime_version TEXT,
  model TEXT
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
  payload_json TEXT NOT NULL DEFAULT '{}',
  -- v3 (fase 3): rastreio de origem do evento pra dedup idempotente
  -- em runs repetidos do reconciler. NULL pra eventos legacy do regime
  -- antigo (inference.py); NOT NULL pra eventos derivados pelo reconcile.
  source_key TEXT
);

CREATE INDEX IF NOT EXISTS idx_eventos_ciclo ON eventos(ciclo_id);
CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos(ts);
CREATE INDEX IF NOT EXISTS idx_eventos_kind ON eventos(kind);
-- NOTA: idx_eventos_source_key (UNIQUE parcial) é criado em db.py:init_db
-- após o ALTER TABLE garantir a coluna em DBs migrados. Em DB novo, esta
-- mesma migração roda também (idempotente via IF NOT EXISTS).

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

-- ─────────────────────────────────────────────────────────────────────────
-- agent_sessions (v2026-05-20+): camada retrospectiva de sessões Claude Code.
--
-- Granularidade: 1 linha por arquivo .jsonl em ~/.claude/projects/<encoded>/.
-- Tabela é populada pelo comando opt-in `mmb-logger backfill-agent-sessions`
-- (não pelo reconcile padrão). Independente de ciclos: ORPHAN é estado
-- válido ("sem ciclo MMB único confiável"), não erro.
--
-- Custo é estimativa operacional (cost_usd_estimated), NÃO substitui
-- ciclos.cost_usd. Pricing version carimbado por linha. duration_api_ms
-- fica NULL retroativo (proxy de transcript é fraca; só preenche quando
-- vier de fonte canônica como `claude -p --output-format json`).
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_sessions (
  -- Identidade
  session_id              TEXT PRIMARY KEY,
  transcript_path         TEXT NOT NULL,
  project_encoded_dir     TEXT,
  cwd                     TEXT,
  git_branch              TEXT,

  -- Classificação
  role                    TEXT NOT NULL CHECK (role IN
    ('master','manual','atomic','orchestrator','unknown')),
  link_confidence         TEXT NOT NULL CHECK (link_confidence IN
    ('HIGH','MEDIUM','LOW','ORPHAN')),
  link_reason             TEXT,
  evidence_json           TEXT,

  -- Linkagem MMB (ciclo_id/epico_id nullable; ORPHAN ingere com NULL)
  ciclo_id                TEXT REFERENCES ciclos(id) ON DELETE SET NULL,
  epico_id                TEXT REFERENCES epicos(id) ON DELETE SET NULL,
  project                 TEXT,
  task_id_raw             TEXT,
  task_id_normalized      TEXT,
  slug                    TEXT,
  candidate_issue_number  INTEGER,
  candidate_pr_number     INTEGER,
  candidate_pr_url        TEXT,

  -- Tempo (3 visões)
  started_at              TEXT,
  ended_at                TEXT,
  duration_wall_ms        INTEGER,
  active_interaction_ms   INTEGER,
  duration_api_ms         INTEGER,   -- NULL em backfill retroativo

  -- Modelo/custo/tokens
  model_resolved          TEXT,
  models_json             TEXT,
  permission_mode         TEXT,
  claude_version          TEXT,
  has_synthetic_turns     INTEGER NOT NULL DEFAULT 0
                            CHECK (has_synthetic_turns IN (0,1)),
  input_tokens            INTEGER,
  output_tokens           INTEGER,
  cache_creation_tokens   INTEGER,
  cache_read_tokens       INTEGER,
  cost_usd_estimated      REAL,
  cost_pricing_version    TEXT,
  cost_confidence         TEXT CHECK (cost_confidence IN
    ('ok_unvalidated','mixed_models','has_synthetic','unknown_model','no_model')
    OR cost_confidence IS NULL),

  -- Atividade
  num_turns               INTEGER,
  tool_call_count_total   INTEGER,
  tool_calls_by_name_json TEXT,
  files_read_json         TEXT,
  files_edited_json       TEXT,
  bash_commands_count     INTEGER,

  -- Controle
  ingested_at             TEXT NOT NULL,
  source                  TEXT NOT NULL DEFAULT 'claude_transcript_backfill'
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_ciclo
  ON agent_sessions(ciclo_id);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_epico
  ON agent_sessions(epico_id);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_role
  ON agent_sessions(role);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_link
  ON agent_sessions(link_confidence);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_started
  ON agent_sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_pr
  ON agent_sessions(candidate_pr_number);
