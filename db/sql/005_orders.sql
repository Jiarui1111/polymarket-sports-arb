-- 下单记录（模拟或真实）
CREATE TABLE IF NOT EXISTS orders (
    id              BIGSERIAL PRIMARY KEY,
    opportunity_id  BIGINT REFERENCES opportunities (id) ON DELETE SET NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode            VARCHAR(16) NOT NULL,
    leg_index       SMALLINT,
    event_id        VARCHAR(64) NOT NULL DEFAULT '',
    market_id       VARCHAR(64) NOT NULL DEFAULT '',
    token_id        VARCHAR(128) NOT NULL,
    side            VARCHAR(8) NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    size            DOUBLE PRECISION NOT NULL,
    order_id        VARCHAR(128),
    status          VARCHAR(32) NOT NULL DEFAULT '',
    filled_size     DOUBLE PRECISION NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders (ts DESC);
CREATE INDEX IF NOT EXISTS idx_orders_opportunity_id ON orders (opportunity_id);
