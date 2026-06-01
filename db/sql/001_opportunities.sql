-- 套利机会主表：每次策略检测到结构机会时写入一行
CREATE TABLE IF NOT EXISTS opportunities (
    id              BIGSERIAL PRIMARY KEY,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy        VARCHAR(32) NOT NULL,
    event_id        VARCHAR(64) NOT NULL,
    event_title     TEXT NOT NULL DEFAULT '',
    edge            DOUBLE PRECISION NOT NULL,
    est_cost        DOUBLE PRECISION NOT NULL,
    est_profit      DOUBLE PRECISION NOT NULL,
    est_max_payout  DOUBLE PRECISION NOT NULL,
    slippage        DOUBLE PRECISION NOT NULL DEFAULT 0,
    fee_cost        DOUBLE PRECISION NOT NULL DEFAULT 0,
    min_depth       DOUBLE PRECISION NOT NULL DEFAULT 0,
    plan_size       DOUBLE PRECISION NOT NULL,
    -- 按订单簿深度模拟 plan_size 张的可成交成本与利润（比 mid 更贴近真实）
    sim_fill_cost   DOUBLE PRECISION,
    sim_fill_profit DOUBLE PRECISION,
    sim_fill_size   DOUBLE PRECISION,
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_opportunities_detected_at ON opportunities (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_event_id ON opportunities (event_id);
CREATE INDEX IF NOT EXISTS idx_opportunities_strategy ON opportunities (strategy);
