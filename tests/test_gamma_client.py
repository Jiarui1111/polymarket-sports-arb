import unittest

from data.polymarket_gamma_client import GammaClient


class GammaClientTest(unittest.TestCase):
    def test_keeps_low_liquidity_markets_inside_multi_outcome_event(self):
        client = GammaClient("https://gamma-api.polymarket.com", min_liquidity=500.0)
        market = client._parse_market({
            "id": "m-low",
            "question": "Will outcome happen?",
            "active": True,
            "closed": False,
            "enableOrderBook": True,
            "clobTokenIds": '["yes-token", "no-token"]',
            "outcomes": '["Yes", "No"]',
            "liquidityNum": "1.0",
            "negRiskMarketID": "neg-risk-set-1",
        })

        self.assertIsNotNone(market)
        self.assertEqual(market.yes_token_id, "yes-token")
        self.assertEqual(market.neg_risk_market_id, "neg-risk-set-1")


if __name__ == "__main__":
    unittest.main()
