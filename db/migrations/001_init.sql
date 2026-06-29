-- Harness Testing Framework: initial schema

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    model_name  TEXT NOT NULL,
    model_url   TEXT NOT NULL,
    harness     TEXT NOT NULL,
    config_yaml TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS benchmark_runs (
    id           SERIAL PRIMARY KEY,
    run_id       TEXT REFERENCES runs(id) ON DELETE CASCADE,
    benchmark    TEXT NOT NULL,
    total_tasks  INT,
    completed    INT,
    accuracy     NUMERIC(6,4),
    grades       JSONB,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS task_outputs (
    id             SERIAL PRIMARY KEY,
    run_id         TEXT REFERENCES runs(id) ON DELETE CASCADE,
    benchmark      TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    final_output   TEXT,
    steps          JSONB,
    input_tokens   INT,
    output_tokens  INT,
    duration_sec   NUMERIC(8,2),
    error          TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(run_id, task_id)
);

CREATE TABLE IF NOT EXISTS eval_results (
    id            SERIAL PRIMARY KEY,
    run_id        TEXT REFERENCES runs(id) ON DELETE CASCADE,
    benchmark     TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    score         NUMERIC(6,4),
    grade         TEXT,
    explanation   TEXT,
    judge_model   TEXT,
    judge_input   TEXT,
    judge_output  TEXT,
    metadata      JSONB,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(run_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_task_outputs_run  ON task_outputs(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_run  ON eval_results(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_grade ON eval_results(grade);
