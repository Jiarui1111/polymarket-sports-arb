import unittest

from config import Config
from models import Event, Market, OrderBook, PriceLevel
from strategy.multi_outcome_arbitrage import MultiOutcomeArbitrage


class UnequalNoBasketTest(unittest.TestCase):
    def test_yes_complete_set_uses_max_profitable_depth(self):
        cfg = Config()
        cfg.fee_rate = 0.0
        cfg.min_edge = 0.001
        cfg.risk_min_edge = 0.001
        cfg.risk_max_slippage = 1.0
        cfg.slippage_buffer = 0.0

        markets = []
        books = {}
        for i, price in enumerate([0.30, 0.30, 0.30]):
            yes_token = f"yes-max-{i}"
            no_token = f"no-max-{i}"
            markets.append(
                Market(
                    market_id=str(i),
                    question=f"Outcome {i}",
                    group_item_title=f"O{i}",
                    outcomes=["Yes", "No"],
                    clob_token_ids=[yes_token, no_token],
                )
            )
            books[yes_token] = OrderBook(yes_token, asks=[PriceLevel(price, 100.0)])
            books[no_token] = OrderBook(no_token, asks=[PriceLevel(0.8, 100.0)])

        event = Event(event_id="event-max", title="Max Event", neg_risk=True, markets=markets)
        plans = MultiOutcomeArbitrage(cfg).scan_event(event, books.get)
        buy_set = [p for p in plans if p.strategy == "buy_set"]

        self.assertEqual(len(buy_set), 1)
        self.assertEqual(buy_set[0].legs[0].size, 100.0)
        self.assertAlmostEqual(buy_set[0].est_cost, 90.0)
        self.assertAlmostEqual(buy_set[0].est_profit, 10.0)

    def test_uses_unequal_depth_when_it_improves_worst_profit(self):
        cfg = Config()
        cfg.fee_rate = 0.0
        cfg.min_edge = 0.001
        cfg.risk_min_edge = 0.001
        cfg.risk_max_slippage = 1.0
        cfg.slippage_buffer = 0.0

        markets = []
        books = {}
        no_depths = [20.0, 20.0, 10.0]
        for i, depth in enumerate(no_depths):
            yes_token = f"yes-{i}"
            no_token = f"no-{i}"
            markets.append(
                Market(
                    market_id=str(i),
                    question=f"Outcome {i}",
                    group_item_title=f"O{i}",
                    outcomes=["Yes", "No"],
                    clob_token_ids=[yes_token, no_token],
                )
            )
            books[yes_token] = OrderBook(yes_token, asks=[PriceLevel(0.9, 20.0)])
            books[no_token] = OrderBook(no_token, asks=[PriceLevel(0.3, depth)])

        event = Event(event_id="event-1", title="Test Event", neg_risk=True, markets=markets)
        plans = MultiOutcomeArbitrage(cfg).scan_event(event, books.get)
        unequal = [p for p in plans if p.strategy == "unequal_no_basket"]

        self.assertEqual(len(unequal), 1)
        plan = unequal[0]
        self.assertEqual([leg.size for leg in plan.legs], [20.0, 20.0, 10.0])
        self.assertAlmostEqual(plan.est_cost, 15.0)
        self.assertAlmostEqual(plan.est_max_payout, 30.0)
        self.assertAlmostEqual(plan.est_profit, 15.0)


if __name__ == "__main__":
    unittest.main()
