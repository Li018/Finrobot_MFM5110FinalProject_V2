"""Report calculations use the full daily low/high range, without API calls."""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import pandas as pd

from finrobot.data_source import CNStockUtils, FMPUtils, YFinanceUtils
from finrobot.functional.analyzer import ReportAnalysisUtils


class ReportPriceRangeTests(unittest.TestCase):
    def test_price_range_uses_low_min_and_high_max_for_both_markets(self):
        bars = pd.DataFrame(
            {"High": [12.0, 20.0], "Low": [8.0, 15.0],
             "Close": [10.0, 18.0], "Volume": [100.0, 200.0]},
            index=pd.to_datetime(["2025-01-01", "2025-01-02"]),
        )
        for symbol, price_source, financial_source, key in (
            ("600519", CNStockUtils, CNStockUtils, "52周价格区间（CNY）"),
            ("AAPL", YFinanceUtils, FMPUtils, "52 Week Price Range (USD)"),
        ):
            with self.subTest(symbol=symbol), ExitStack() as stack:
                for method, value in (
                    ("get_stock_data", bars),
                    ("get_stock_info", {"shortName": "Fixture", "currency": "USD"}),
                    ("get_analyst_recommendations", ("N/A", 0)),
                ):
                    stack.enter_context(patch.object(price_source, method, return_value=value))
                for method, value in (
                    ("get_target_price", "N/A"),
                    ("get_historical_market_cap", 1e8),
                    ("get_historical_bvps", 2.0),
                ):
                    stack.enter_context(patch.object(financial_source, method, return_value=value))
                result = ReportAnalysisUtils.get_key_data(symbol, "2025-01-03")
                self.assertEqual(result[key], "8.00 - 20.00")


if __name__ == "__main__":
    unittest.main()
