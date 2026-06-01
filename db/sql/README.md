# 数据库迁移

按文件名顺序执行本目录下所有 `.sql`：

```bash
pip install psycopg2-binary
# 配置 .env 中 PG_* 或 DATABASE_URL
python -m db.migrate
```

## 表说明

| 文件 | 表 | 说明 |
|------|-----|------|
| `001_opportunities.sql` | opportunities | 套利机会主表，含深度模拟利润 |
| `002_opportunity_legs.sql` | opportunity_legs | 每条腿 |
| `003_opportunity_book_levels.sql` | opportunity_book_levels | 发现时 bid/ask 各 N 档 |
| `004_opportunity_book_ticks.sql` | opportunity_book_ticks | WS 最近 N 条 tick |
| `005_orders.sql` | orders | 下单记录 |
