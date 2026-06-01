-- WebSocket 最近 N 条盘口变动（按 token + bid/ask 分队列，默认各 5 条）
-- 用于复盘：机会出现时前几跳盘口是怎么变的
CREATE TABLE IF NOT EXISTS opportunity_book_ticks (
    id              BIGSERIAL PRIMARY KEY,
    opportunity_id  BIGINT NOT NULL REFERENCES opportunities (id) ON DELETE CASCADE,
    token_id        VARCHAR(128) NOT NULL,
    side            VARCHAR(8) NOT NULL CHECK (side IN ('bid', 'ask')),
    tick_seq        SMALLINT NOT NULL CHECK (tick_seq BETWEEN 1 AND 50),
    event_type      VARCHAR(32) NOT NULL DEFAULT 'price_change',
    price           DOUBLE PRECISION NOT NULL,
    size            DOUBLE PRECISION NOT NULL,
    ws_ts           DOUBLE PRECISION NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (opportunity_id, token_id, side, tick_seq)
);

CREATE INDEX IF NOT EXISTS idx_opp_book_ticks_opp ON opportunity_book_ticks (opportunity_id);
