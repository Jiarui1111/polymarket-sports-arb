# Polymarket Structural Arbitrage

面向 Polymarket 多结果 / neg-risk 市场（重点：Sports）的结构性套利引擎。

发现活跃市场，识别互斥结果组合，拉取实时订单簿，计算结构性价差，输出交易计划，经风控校验后进行模拟或真实下单。

> 开发中。默认 dry-run（只模拟、不下单）；私钥仅从 `.env` 读取。

## 策略

基于真实订单簿深度计算，扣除手续费与滑点缓冲后按 `min_edge` 过滤：

- **complement**：同一市场 `YES_ask + NO_ask < 1`，买入补集锁定 1 美元
- **buy_set**：互斥结果 YES 卖价之和小于 1，买入完整集
- **sell_set**：互斥结果 YES 之和大于 1，反向买入完整 NO 集

## 结构

| 文件 | 职责 |
| --- | --- |
| `config.py` / `logging_config.py` / `models.py` | 配置、日志、数据模型 |
| `data/polymarket_gamma_client.py` | 市场发现 |
| `data/clob_client.py` | 行情、下单、撤单 |
| `ws/orderbook_ws.py` | 实时订单簿 |
| `strategy/multi_outcome_arbitrage.py` | 套利检测 |
| `risk/risk_manager.py` | 风控 |
| `execution/order_executor.py` | 执行器 |
| `storage/db.py` | 持久化 |
| `main.py` | 入口 |

## 配置

- **本地**：`.env` 中 `DB_ENABLED=false`，只写 `logs/`，无需 PostgreSQL
- **服务器**：`cp .env.production.example .env`，`DB_ENABLED=true`，填 `PG_*` 后 `python -m db.migrate`

## 运行

```bash
pip install -r requirements.txt

python main.py --once --tags Sports
python main.py --tags Sports --min-edge 0.02
python main.py --mode real --i-understand-real
```
