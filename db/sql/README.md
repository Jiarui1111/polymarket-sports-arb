# Database Migrations

Run every `.sql` file in this directory in filename order:

```bash
pip install psycopg2-binary
python -m db.migrate
```

## Tables

| File | Table | Purpose |
|------|-------|---------|
| `001_opportunities.sql` | `opportunities` | One row per detected opportunity, including estimated and simulated profit |
| `002_opportunity_legs.sql` | `opportunity_legs` | Legs for each opportunity |
| `003_opportunity_book_levels.sql` | `opportunity_book_levels` | Bid/ask depth captured at opportunity time |
| `004_opportunity_book_ticks.sql` | `opportunity_book_ticks` | Recent WS ticks at opportunity time |
| `005_orders.sql` | `orders` | Dry-run or real order records |
| `006_widen_book_level_rank.sql` | constraint update | Widens stored depth rank range |
| `007_daily_opportunity_stats.sql` | `daily_opportunity_stats` | Daily opportunity count and summary stats by strategy |
