"""Historical report regressions using original tools and offline data fixtures."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from PIL import Image
from reportlab.platypus import SimpleDocTemplate, Table

from finrobot.data_source import CNStockUtils
from finrobot.data_source.akshare_utils import AkShareUtils
from finrobot.functional.analyzer import ReportAnalysisUtils
from finrobot.functional.reportlab import ReportLabUtils


def annual_source():
    common = {
        "REPORT_DATE": [f"{year}-12-31" for year in range(2021, 2025)],
        "NOTICE_DATE": [f"{year}-04-01" for year in range(2022, 2026)],
    }
    income = pd.DataFrame({
        **common, "TOTAL_OPERATE_INCOME": [1e8, 2e8, 3e8, 4e8],
        "OPERATE_COST": [5e7] * 4, "PARENT_NETPROFIT": [2e7] * 4,
        "NETPROFIT": [2e7] * 4, "BASIC_EPS": [1.0, 2.0, 3.0, 4.0],
    })
    balance = pd.DataFrame({
        **common, "TOTAL_ASSETS": [4e8] * 4,
        "TOTAL_LIABILITIES": [1e8] * 4, "TOTAL_EQUITY": [3e8] * 4,
    })
    cash_flow = pd.DataFrame({
        **common, "NETCASH_OPERATE": [3e7] * 4, "CONSTRUCT_LONG_ASSET": [1e7] * 4,
    })
    return Mock(
        stock_profit_sheet_by_report_em=Mock(return_value=income),
        stock_balance_sheet_by_report_em=Mock(return_value=balance),
        stock_cash_flow_sheet_by_report_em=Mock(return_value=cash_flow),
    )


class HistoricalFinancialMetricsTests(unittest.TestCase):
    def test_cutoff_uses_disclosure_date_before_selecting_recent_years(self):
        with patch("finrobot.data_source.akshare_utils._require_ak", return_value=annual_source()):
            early = CNStockUtils.get_financial_metrics("600519", years=1, as_of="2024-01-01")
            disclosed = CNStockUtils.get_financial_metrics("600519", years=1, as_of="2024-04-01")
            latest = CNStockUtils.get_financial_metrics("600519", years=2)
        self.assertEqual(list(early.columns), ["2022"])
        self.assertEqual(list(disclosed.columns), ["2023"])
        self.assertEqual(list(latest.columns), ["2023", "2024"])

    def test_unknown_disclosure_dates_are_not_assumed_available(self):
        for missing in ("column", "value"):
            with self.subTest(missing=missing):
                source = annual_source()
                income = source.stock_profit_sheet_by_report_em.return_value
                if missing == "column":
                    source.stock_profit_sheet_by_report_em.return_value = income.drop(columns="NOTICE_DATE")
                else:
                    income["NOTICE_DATE"] = None
                with patch("finrobot.data_source.akshare_utils._require_ak", return_value=source):
                    with self.assertRaisesRegex(ValueError, "截至 2024-01-01 已披露"):
                        AkShareUtils.get_financial_metrics("600519", as_of="2024-01-01")
                    self.assertIn("2024", AkShareUtils.get_financial_metrics("600519").columns)

    def test_all_three_statements_apply_the_cutoff(self):
        source = annual_source()
        source.stock_balance_sheet_by_report_em.return_value.loc[2, "NOTICE_DATE"] = "2025-01-01"
        source.stock_cash_flow_sheet_by_report_em.return_value.loc[2, "NOTICE_DATE"] = "2025-01-01"
        with patch("finrobot.data_source.akshare_utils._require_ak", return_value=source):
            metrics = AkShareUtils.get_financial_metrics("600519", as_of="2024-04-01")
        self.assertEqual(metrics.loc["EPS (元)", "2023"], 3.0)
        self.assertTrue(pd.isna(metrics.loc["Debt Ratio (%)", "2023"]))
        self.assertTrue(pd.isna(metrics.loc["Operating Cash Flow (亿元)", "2023"]))

    def test_no_eligible_report_fails_instead_of_returning_future_years(self):
        with patch("finrobot.data_source.akshare_utils._require_ak", return_value=annual_source()):
            with self.assertRaisesRegex(ValueError, "截至 2020-01-01 已披露"):
                AkShareUtils.get_financial_metrics("600519", as_of="2020-01-01")

    def test_pdf_financial_table_is_filtered_by_its_filing_date(self):
        financial_tables = []
        original_build = SimpleDocTemplate.build

        def capture_and_build(doc, story):
            financial_tables.extend(
                [row[:] for row in item._cellvalues]
                for item in story if isinstance(item, Table)
                and item._cellvalues[0][0] == "Financial Metrics"
            )
            return original_build(doc, story)

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image_path, report_path = folder / "chart.png", folder / "report.pdf"
            Image.new("RGB", (30, 20), "white").save(image_path)
            with patch("finrobot.data_source.akshare_utils._require_ak", return_value=annual_source()), \
                 patch.object(CNStockUtils, "get_stock_info", return_value={"shortName": "Fixture"}), \
                 patch.object(ReportAnalysisUtils, "get_key_data", return_value={"fixture": "ok"}), \
                 patch.object(SimpleDocTemplate, "build", autospec=True, side_effect=capture_and_build):
                result = ReportLabUtils.build_annual_report(
                    "600519", str(report_path), "Operating results", "Market position",
                    "Business overview", "Risks", "Competitors", str(image_path),
                    str(image_path), "2024-01-01",
                )
            self.assertEqual(result, "Annual report generated successfully.")
            self.assertTrue(report_path.read_bytes().startswith(b"%PDF"))
        self.assertEqual(len(financial_tables), 1)
        self.assertEqual(financial_tables[0][1][1:], ["2021", "2022"])


class HistoricalBVPSTests(unittest.TestCase):
    def test_future_only_values_are_not_used_for_a_historical_query(self):
        source = Mock(stock_financial_analysis_indicator=Mock(return_value=pd.DataFrame({
            "日期": ["2025-12-31"], "每股净资产_调整前(元)": [12.34],
        })))
        with patch("finrobot.data_source.akshare_utils._require_ak", return_value=source):
            self.assertIsNone(AkShareUtils.get_historical_bvps("600519", "2024-01-01"))
            self.assertEqual(AkShareUtils.get_historical_bvps("600519"), 12.34)

    def test_latest_period_at_or_before_target_is_selected(self):
        source = Mock(stock_financial_analysis_indicator=Mock(return_value=pd.DataFrame({
            "日期": ["2025-12-31", "2023-12-31", "2022-12-31"],
            "每股净资产_调整前(元)": [12.34, 8.0, 7.0],
        })))
        with patch("finrobot.data_source.akshare_utils._require_ak", return_value=source):
            self.assertEqual(AkShareUtils.get_historical_bvps("600519", "2023-12-31"), 8.0)
            self.assertEqual(AkShareUtils.get_historical_bvps("600519", "2023-06-01"), 7.0)

    def test_unparseable_report_periods_are_missing(self):
        source = Mock(stock_financial_analysis_indicator=Mock(return_value=pd.DataFrame({
            "日期": [None], "每股净资产_调整前(元)": [12.34],
        })))
        with patch("finrobot.data_source.akshare_utils._require_ak", return_value=source):
            self.assertIsNone(AkShareUtils.get_historical_bvps("600519", "2024-01-01"))


if __name__ == "__main__":
    unittest.main()
