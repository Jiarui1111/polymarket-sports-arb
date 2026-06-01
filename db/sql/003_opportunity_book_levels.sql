-- 发现机会瞬间的订单簿档位快照：每个 token 各存 best N 档 bid / ask（默认 5）
CREATE TABLE IF NOT EXISTS opportunity_book_levels (
    id              BIGSERIAL PRIMARY KEY,
    opportunity_id  BIGINT NOT NULL REFERENCES opportunities (id) ON DELETE CASCADE,
    token_id        VARCHAR(128) NOT NULL,
    side            VARCHAR(8) NOT NULL CHECK (side IN ('bid', 'ask')),
    level_rank      SMALLINT NOT NULL CHECK (level_rank BETWEEN 1 AND 20),
    price           DOUBLE PRECISION NOT NULL,
    size            DOUBLE PRECISION NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (opportunity_id, token_id, side, level_rank)
);

CREATE INDEX IF NOT EXISTS idx_opp_book_levels_opp ON opportunity_book_levels (opportunity_id);
CREATE INDEX IF NOT EXISTS idx_opp_book_levels_token ON opportunity_book_levels (token_id);
