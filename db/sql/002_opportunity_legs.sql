-- 机会对应的每条交易腿
CREATE TABLE IF NOT EXISTS opportunity_legs (
    id              BIGSERIAL PRIMARY KEY,
    opportunity_id  BIGINT NOT NULL REFERENCES opportunities (id) ON DELETE CASCADE,
    leg_index       SMALLINT NOT NULL,
    market_id       VARCHAR(64) NOT NULL DEFAULT '',
    market_question TEXT NOT NULL DEFAULT '',
    token_id        VARCHAR(128) NOT NULL,
    outcome         VARCHAR(128) NOT NULL DEFAULT '',
    side            VARCHAR(8) NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    size            DOUBLE PRECISION NOT NULL,
    available_depth DOUBLE PRECISION NOT NULL DEFAULT 0,
    notional        DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (opportunity_id, leg_index)
);

CREATE INDEX IF NOT EXISTS idx_opportunity_legs_opp_id ON opportunity_legs (opportunity_id);
