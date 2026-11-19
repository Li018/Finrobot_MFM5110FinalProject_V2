"""Synthetic-price and announcement fixtures; no external requests."""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from finrobot.data_source.akshare_utils import AkShareUtils, _select_annual_report
from finrobot.functional.factors import compute_price_factors, get_a_share_factors


class PriceFactorTests(unittest.TestCase):
    def setUp(self):
        dates = pd.bdate_range("2024-01-01", periods=90)
        close = 10 * 1.01 ** np.arange(90)
        self.bars = pd.DataFrame({"Close": close, "Volume": np.arange(90) + 100}, index=dates)

    def test_momentum_volatility_and_volume_formulas(self):
        result = compute_price_factors(self.bars)
        self.assertAlmostEqual(result.iloc[-1].momentum_20d, 1.01 ** 20 - 1)
        self.assertAlmostEqual(result.iloc[-1].momentum_60d, 1.01 ** 60 - 1)
        self.assertAlmostEqual(result.iloc[-1].volatility_20d, 0)
        self.assertAlmostEqual(result.iloc[-1].avg_volume_20d, self.bars.Volume.tail(20).mean())

    def test_future_data_does_not_change_prefix(self):
        pd.testing.assert_frame_equal(compute_price_factors(self.bars).iloc[:65],
                                      compute_price_factors(self.bars.iloc[:65]))

    def test_missing_volume_and_incomplete_prices_are_not_filled(self):
        result = compute_price_factors(self.bars.drop(columns="Volume"))
        self.assertTrue(result.avg_volume_20d.isna().all())
        bars = self.bars.copy()
        bars.iloc[-1, 0] = np.nan
        self.assertTrue(pd.isna(compute_price_factors(bars).iloc[-1].momentum_20d))

    def test_duplicate_dates_are_rejected(self):
        with self.assertRaises(ValueError):
            compute_price_factors(pd.concat([self.bars, self.bars.iloc[-1:]]))

    def test_public_tool_excludes_bars_after_cutoff(self):
        import json
        with patch("finrobot.functional.factors.CNStockUtils.get_stock_data", return_value=self.bars):
            answer = json.loads(get_a_share_factors("000001", "2024-03-29"))
        self.assertLessEqual(answer["data_date"], "2024-03-29")
        self.assertEqual(answer["factors"]["avg_volume_20d"]["unit"], "lots/day")


class AnnualReportTests(unittest.TestCase):
    def announcements(self, titles, dates=None):
        return pd.DataFrame({"公告标题": titles, "公告时间": dates or ["2025-04-01"] * len(titles),
                             "公告链接": [f"fixture-{i}" for i in range(len(titles))]})

    def test_report_year_is_not_disclosure_year(self):
        frame = self.announcements(["2023年年度报告", "2024年年度报告"])
        self.assertEqual(_select_annual_report(frame, "2024")["公告标题"], "2024年年度报告")

    def test_summary_english_cancelled_and_correction_are_excluded(self):
        for suffix in ("摘要", "英文版", "取消公告", "更正公告"):
            with self.subTest(suffix=suffix):
                self.assertIsNone(_select_annual_report(self.announcements(["2024年年度报告" + suffix]), "2024"))

    def test_latest_ignores_newer_summary(self):
        frame = self.announcements(["2024年年度报告", "2024年年度报告摘要"], ["2025-04-01", "2025-05-01"])
        self.assertEqual(_select_annual_report(frame)["公告标题"], "2024年年度报告")

    def test_missing_disclosure_date_does_not_claim_latest(self):
        frame = self.announcements(["2024年年度报告"]).drop(columns="公告时间")
        self.assertIsNone(_select_annual_report(frame))

    def test_tool_returns_not_found_when_only_summary_exists(self):
        with patch.object(AkShareUtils, "get_announcements", return_value=self.announcements(["2024年年度报告摘要"])):
            self.assertIn("未找到", AkShareUtils.get_sec_report("000001", "2024"))


if __name__ == "__main__":
    unittest.main()
