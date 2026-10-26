-- Claude Usage Tracker — central store (SQLite)
-- One database holds both accounts. Every row carries `account` so views can be
-- scoped per account or joined for cross-account comparison.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
  account      TEXT PRIMARY KEY,          -- 'work' | 'personal'
  label        TEXT,
  plan         TEXT,                       -- 'pro' | 'max' | 'team' ...
  created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS machines (
  machine      TEXT PRIMARY KEY,          -- hostname
  account      TEXT NOT NULL REFERENCES accounts(account),
  last_seen    TEXT,
  collector_version TEXT
);

-- One row per Claude Code session (one JSONL file).
CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  account      TEXT NOT NULL REFERENCES accounts(account),
  machine      TEXT,
  project_dir  TEXT,                       -- cwd of the session
  entrypoint   TEXT,                       -- cli | vscode | remote_cowork | scheduled ...
  cc_version   TEXT,
  git_branch   TEXT,
  first_ts     TEXT,
  last_ts      TEXT,
  turns        INTEGER DEFAULT 0,          -- user prompts
  first_prompt TEXT,                       -- first 300 chars of first user prompt
  source_file  TEXT                        -- transcript path on the machine
);
CREATE INDEX IF NOT EXISTS ix_sessions_account_ts ON sessions(account, first_ts);

-- One row per assistant API response. Dedupe key = message_id + request_id
-- (the same message can appear in a file more than once when streamed).
CREATE TABLE IF NOT EXISTS messages (
  message_id        TEXT NOT NULL,
  request_id        TEXT,
  session_id        TEXT NOT NULL REFERENCES sessions(session_id),
  account           TEXT NOT NULL,
  ts                TEXT NOT NULL,
  model             TEXT,                  -- model that actually answered
  requested_model   TEXT,                  -- model the user/session asked for
  effort            TEXT,
  input_tokens      INTEGER DEFAULT 0,
  cache_create_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  output_tokens     INTEGER DEFAULT 0,
  thinking_tokens   INTEGER DEFAULT 0,
  stop_reason       TEXT,
  is_sidechain      INTEGER DEFAULT 0,     -- subagent traffic
  parent_uuid       TEXT,
  PRIMARY KEY (message_id, request_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_account_ts ON messages(account, ts);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS ix_messages_model ON messages(account, model);

-- Tool and MCP calls, one row per tool_use block.
CREATE TABLE IF NOT EXISTS tool_calls (
  tool_use_id   TEXT PRIMARY KEY,
  message_id    TEXT NOT NULL,
  session_id    TEXT NOT NULL,
  account       TEXT NOT NULL,
  ts            TEXT,
  tool_name     TEXT NOT NULL,             -- full name, e.g. mcp__memory__memory_read
  mcp_server    TEXT,                      -- 'memory' for mcp__memory__*, NULL for built-ins
  input_chars   INTEGER DEFAULT 0,         -- size of the tool input
  result_chars  INTEGER                    -- size of the tool_result that came back (if seen)
);
CREATE INDEX IF NOT EXISTS ix_tool_calls_account ON tool_calls(account, tool_name);

-- User prompts, for turn counts and repeated-prompt detection.
CREATE TABLE IF NOT EXISTS prompts (
  prompt_uuid   TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  account       TEXT NOT NULL,
  ts            TEXT,
  chars         INTEGER DEFAULT 0,
  is_slash      INTEGER DEFAULT 0,         -- starts with '/'
  slash_name    TEXT,
  prompt_hash   TEXT,                      -- sha1 of normalized text, for repeats
  excerpt       TEXT                       -- first 200 chars
);

-- Snapshots of instruction files: CLAUDE.md, commands, skills, scheduled-task prompts.
-- Content is stored so the duplicate-instruction analyzer can diff across jobs.
CREATE TABLE IF NOT EXISTS context_files (
  id            INTEGER PRIMARY KEY,
  account       TEXT NOT NULL,
  machine       TEXT,
  kind          TEXT NOT NULL,             -- claude_md | command | skill | scheduled_task | agent | settings
  path          TEXT NOT NULL,
  scope         TEXT,                      -- global | project:<dir> | cloud
  content_hash  TEXT NOT NULL,
  chars         INTEGER,
  est_tokens    INTEGER,                   -- chars/4 heuristic unless measured
  content       TEXT,
  snapshot_ts   TEXT NOT NULL,
  UNIQUE(account, path, content_hash)
);

-- Scheduled tasks as configured (cloud). Filled by `collector tasks` export or by hand.
CREATE TABLE IF NOT EXISTS scheduled_tasks (
  task_id       TEXT PRIMARY KEY,
  account       TEXT NOT NULL,
  name          TEXT,
  cron          TEXT,
  model         TEXT,
  prompt        TEXT,
  permission_mode TEXT,
  enabled       INTEGER DEFAULT 1,
  updated_ts    TEXT
);

-- One row per scheduled-task run (shipped by the run itself via the relay).
CREATE TABLE IF NOT EXISTS task_runs (
  run_id        TEXT PRIMARY KEY,
  task_id       TEXT,
  account       TEXT NOT NULL,
  started_ts    TEXT,
  ended_ts      TEXT,
  model         TEXT,
  input_tokens  INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_create_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  turns         INTEGER,
  outcome       TEXT,                      -- 'noteworthy' | 'nothing_new' | 'error'
  summary       TEXT
);

-- Daily meter readings for surfaces with no per-message data (claude.ai chat).
CREATE TABLE IF NOT EXISTS meter_snapshots (
  account       TEXT NOT NULL,
  date          TEXT NOT NULL,             -- YYYY-MM-DD
  surface       TEXT NOT NULL,             -- claude_ai | claude_code | scheduled
  metric        TEXT NOT NULL,             -- pct_used | messages | resets_at
  value         TEXT,
  PRIMARY KEY (account, date, surface, metric)
);

-- Output of the analyzers. Dashboard reads this; you mark findings resolved.
CREATE TABLE IF NOT EXISTS findings (
  id            INTEGER PRIMARY KEY,
  account       TEXT NOT NULL,             -- or 'shared' for cross-account findings
  analyzer      TEXT NOT NULL,
  severity      TEXT NOT NULL,             -- high | medium | low | info
  title         TEXT NOT NULL,
  detail        TEXT,                      -- markdown
  evidence      TEXT,                      -- JSON
  est_tokens_wasted INTEGER,
  recommendation TEXT,
  fingerprint   TEXT UNIQUE,               -- analyzer+subject, so reruns update not duplicate
  created_ts    TEXT DEFAULT (datetime('now')),
  updated_ts    TEXT,
  resolved      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_findings_open ON findings(account, resolved, severity);

-- Collector bookkeeping: how far each transcript file has been read.
CREATE TABLE IF NOT EXISTS ingest_log (
  account       TEXT NOT NULL,
  machine       TEXT NOT NULL,
  source_file   TEXT NOT NULL,
  byte_offset   INTEGER DEFAULT 0,
  last_ingest   TEXT,
  PRIMARY KEY (account, machine, source_file)
);

-- Convenience views -----------------------------------------------------------

CREATE VIEW IF NOT EXISTS v_daily AS
SELECT account, substr(ts,1,10) AS day, model,
       COUNT(*) AS calls,
       SUM(input_tokens) AS input_tokens,
       SUM(cache_create_tokens) AS cache_create_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(input_tokens + cache_create_tokens + cache_read_tokens) AS context_tokens
FROM messages GROUP BY account, day, model;

CREATE VIEW IF NOT EXISTS v_session_totals AS
SELECT s.session_id, s.account, s.project_dir, s.entrypoint, s.first_ts, s.last_ts, s.turns, s.first_prompt,
       COUNT(m.message_id) AS calls,
       SUM(m.input_tokens) AS input_tokens,
       SUM(m.cache_create_tokens) AS cache_create_tokens,
       SUM(m.cache_read_tokens) AS cache_read_tokens,
       SUM(m.output_tokens) AS output_tokens,
       SUM(m.input_tokens + m.cache_create_tokens + m.cache_read_tokens) AS context_tokens,
       MAX(m.input_tokens + m.cache_create_tokens + m.cache_read_tokens) AS peak_context,
       CASE WHEN SUM(m.input_tokens + m.cache_create_tokens + m.cache_read_tokens) > 0
            THEN 1.0 * SUM(m.cache_read_tokens) / SUM(m.input_tokens + m.cache_create_tokens + m.cache_read_tokens)
            ELSE 0 END AS cache_hit_ratio,
       GROUP_CONCAT(DISTINCT m.model) AS models
FROM sessions s LEFT JOIN messages m ON m.session_id = s.session_id
GROUP BY s.session_id;

CREATE VIEW IF NOT EXISTS v_tool_usage AS
SELECT account, COALESCE(mcp_server, 'builtin') AS server, tool_name,
       COUNT(*) AS calls, SUM(input_chars) AS input_chars, SUM(COALESCE(result_chars,0)) AS result_chars
FROM tool_calls GROUP BY account, server, tool_name;
