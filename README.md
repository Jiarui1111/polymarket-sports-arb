# Polymarket Structural Arbitrage

Polymarket 多 outcome 结构套利监控与执行系统。当前默认 `dry_run`，只记录与模拟，不真实下单。

## 当前主线

只重点关注多个 outcome 互斥、最终恰好一个 outcome 结算为 Yes 的市场。

目标市场类型：

- 体育冠军类：NBA Champion、MLB Champion、World Cup Group Winner
- 区间类：Tesla deliveries、Inflation bracket、IPO market cap
- 加密价格区间类：BTC / ETH / SOL / XRP price range、above / below
- 候选人类：Presidential nominee、Election winner
- 排名类：AI model ranking、Top scorer nation
- 多候选人 / 多结果 winner market

这些市场适合程序化扫描 Yes / No 总价格与理论回收之间的偏差。

## 当前策略

基于真实订单簿深度计算，不只看 best ask。每个机会都会记录计划 size、可用深度、盘口前 N 档、估算成本、worst payout、profit 和 edge。

### 1. Yes Complete Set

买齐一个互斥多结果市场的所有 Yes。

```text
yes_sum = sum(avg_ask_yes_i)
profit_per_set = 1 - yes_sum
opportunity if yes_sum < 1 - min_edge
```

代码策略名：`buy_set`

### 2. Equal No Basket

等额买齐所有 outcome 的 No。N 个 outcome 中只有一个 No 会输，其余 N-1 个 No 赢。

```text
total_cost = q * sum(avg_ask_no_i)
worst_payout = q * (N - 1)
profit = worst_payout - total_cost
```

代码策略名：`sell_set`

### 3. Unequal No Basket

不等额买入各 outcome 的 No，用线性规划在订单簿深度中寻找更优组合。

```text
total_shares = sum(q_i)
worst_payout = total_shares - max(q_i)
profit = worst_payout - sum(cost_i)
```

代码策略名：`unequal_no_basket`

### 暂不做

`Complete Set + No`、conversion / merge、synthetic outcome、maker rebate assisted 暂时不作为当前主线。

单市场 `YES + NO < 1` 的 complement 策略仍保留在代码里，但默认关闭。

## 市场发现

流程：

```text
Gamma API 拉 active / open events
-> 过滤 neg-risk 多 outcome
-> 关键词过滤冠军、区间、候选人、排名、winner 类市场
-> 收集 YES / NO clob token id
-> REST 分批 seed 所有目标 token 的订单簿快照
-> WebSocket 订阅实时盘口变动
-> 策略读取本地 OrderBookCache 扫描机会
```

关键词过滤可以通过 `.env` 调整：

```text
TARGET_MARKET_FILTER_ENABLED=true
TARGET_MIN_OUTCOMES=3
TARGET_MARKET_KEYWORDS=champion,winner,group winner,market cap,deliveries,inflation,bracket,nominee,election,ranking,top scorer,best,most,which,how many,ipo,crypto,bitcoin,btc,ethereum,eth,solana,sol,xrp,price,range,above,below,presidential,nba,mlb,world cup,tesla,ai model
```

调试时可用 `--all-markets` 临时关闭目标市场过滤。

## 运行

```bash
pip install -r requirements.txt

python main.py --once
python main.py --once --max-events 200 --min-edge 0.002
python main.py --once --all-markets
```

真实下单必须显式确认：

```bash
python main.py --mode real --i-understand-real
```

## 结构

| 文件 | 职责 |
| --- | --- |
| `config.py` | 配置、市场过滤、策略开关 |
| `data/polymarket_gamma_client.py` | Gamma 市场发现 |
| `data/clob_client.py` | CLOB REST 行情、下单、撤单 |
| `ws/orderbook_ws.py` | WebSocket 实时订单簿 |
| `strategy/multi_outcome_arbitrage.py` | Yes complete set / No basket 扫描 |
| `execution/order_executor.py` | dry-run / real 执行与机会深度日志 |
| `storage/book_capture.py` | 机会触发时盘口快照与模拟成交 |
| `storage/db.py` | PostgreSQL 或空数据库实现 |
| `main.py` | 启动入口 |
