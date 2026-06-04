-- Daily aggregate opportunity counters.
-- One row per day and strategy. Query strategy='ALL' by summing rows when
-- a total daily count is needed.
CREATE TABLE IF NOT EXISTS daily_opportunity_stats (
    stat_date               DATE NOT NULL,
    strategy                VARCHAR(32) NOT NULL,
    opportunity_count       BIGINT NOT NULL DEFAULT 0,
    total_est_profit        DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_edge                DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_detected_at       TIMESTAMPTZ NOT NULL,
    last_detected_at        TIMESTAMPTZ NOT NULL,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (stat_date, strategy)
);

CREATE INDEX IF NOT EXISTS idx_daily_opportunity_stats_date
    ON daily_opportunity_stats (stat_date DESC);
