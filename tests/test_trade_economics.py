import unittest

from bot.strategies.enhanced import KellySizer


class TradeEconomicsTests(unittest.TestCase):
    def test_rejects_trade_below_min_expected_net_profit(self):
        sizer = KellySizer(
            fraction=0.5,
            max_bet_pct=0.1,
            fee_rate=0.07,
            min_position_size_usd=1.0,
            min_expected_net_profit_usd=0.10,
        )
        size = sizer.calculate(model_prob=0.55, market_price=0.50, bankroll=10)
        self.assertEqual(size, 0)

    def test_rejects_trade_below_min_position_size(self):
        sizer = KellySizer(
            fraction=0.25,
            max_bet_pct=0.05,
            fee_rate=0.07,
            min_position_size_usd=1.0,
            min_expected_net_profit_usd=0.0,
        )
        size = sizer.calculate(model_prob=0.90, market_price=0.10, bankroll=5)
        self.assertEqual(size, 0)


if __name__ == "__main__":
    unittest.main()
