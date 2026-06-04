ALTER TABLE opportunity_book_levels
    DROP CONSTRAINT IF EXISTS opportunity_book_levels_level_rank_check;

ALTER TABLE opportunity_book_levels
    ADD CONSTRAINT opportunity_book_levels_level_rank_check
    CHECK (level_rank BETWEEN 1 AND 500);
