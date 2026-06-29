-- Harness Testing Framework: schema v2

-- Unique constraint on benchmark_runs to allow ON CONFLICT DO UPDATE
ALTER TABLE benchmark_runs
    ADD COLUMN IF NOT EXISTS categories JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'benchmark_runs_run_id_benchmark_key'
    ) THEN
        ALTER TABLE benchmark_runs ADD CONSTRAINT benchmark_runs_run_id_benchmark_key UNIQUE (run_id, benchmark);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE task_outputs
    ADD COLUMN IF NOT EXISTS status      TEXT DEFAULT 'done',
    ADD COLUMN IF NOT EXISTS agent_patch TEXT,
    ADD COLUMN IF NOT EXISTS eval_log    TEXT,
    ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE eval_results
    ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMPTZ DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_task_outputs_status    ON task_outputs(status);
CREATE INDEX IF NOT EXISTS idx_task_outputs_benchmark ON task_outputs(benchmark);
CREATE INDEX IF NOT EXISTS idx_eval_results_score     ON eval_results(score);
CREATE INDEX IF NOT EXISTS idx_benchmark_runs_bench   ON benchmark_runs(benchmark);
CREATE INDEX IF NOT EXISTS idx_runs_created           ON runs(created_at DESC);
