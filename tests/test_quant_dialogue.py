"""Offline tests for user-parameterized dialogue tools; prices are test fixtures."""
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
os.environ["DEBUG"] = "false"
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")
from finrobot.functional import quant_dialogue as dialogue
from finrobot.functional.quant_dialogue import QuantDialogueTools as Tools


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / "tmp/dialogue_tests" / datetime.now().strftime("%Y%m%d_%H%M%S%f")
        self.folder.mkdir(parents=True)
        dates = pd.bdate_range("2023-01-02", periods=160)
        prices = 10 + np.sin(np.arange(160) / 7)
        self.frame = pd.DataFrame({"Open": prices, "High": prices + .2, "Low": prices - .2,
                                   "Close": prices, "Volume": 100000.0}, index=dates)
        source = {"cached": True, "path": "test-fixture-only", "metadata": {"clean_sha256": "fixture"}}
        self.data_patch = patch.object(dialogue, "DATA_ROOT", self.folder / "data")
        self.out_patch = patch.object(dialogue, "OUT_ROOT", self.folder / "results")
        self.source_patch = patch.object(dialogue, "get_market_snapshot", return_value=(self.frame, source))
        for item in (self.data_patch, self.out_patch, self.source_patch):
            item.start()
            self.addCleanup(item.stop)

    def prepare(self, **kwargs):
        config = {"symbol": "000651", "start_date": "2023-03-01", "end_date": "2023-07-31",
                  "fast": 5, "slow": 20, "initial_cash": 50000.0, "max_position": .6, **kwargs}
        return json.loads(Tools.prepare_quant_experiment(**config))

    def test_user_parameters_are_preserved(self):
        answer = self.prepare()
        cfg = answer["config"]
        self.assertEqual((cfg["symbol"], cfg["fast"], cfg["slow"], cfg["initial_cash"], cfg["max_position"]),
                         ("000651", 5, 20, 50000.0, .6))
        self.assertEqual(answer["warmup_rows"], 20)
        self.assertEqual(answer["actual_start"], "2023-03-01")

    def test_parameter_change_creates_another_experiment(self):
        first, second = self.prepare(), self.prepare(symbol="600031", fast=10, slow=30)
        self.assertNotEqual(first["experiment_id"], second["experiment_id"])
        _, saved = dialogue.load_experiment(first["experiment_id"])
        self.assertEqual(saved["config"]["symbol"], "000651")
        self.assertEqual(saved["config"]["fast"], 5)

    def test_compare_and_independent_risk(self):
        experiment = self.prepare()
        identifier = experiment["experiment_id"]
        compared = json.loads(Tools.compare_quant_strategies(identifier))
        risk = json.loads(Tools.check_quant_risk(identifier, "sma"))
        self.assertEqual(set(compared["metrics"]), {"sma", "buy_hold"})
        self.assertTrue(risk["ok"])
        self.assertFalse(risk["continuous_position_cap_enforced"])
        self.assertTrue(Path(compared["plot_path"]).is_file())
        self.assertEqual(risk["metrics"], compared["metrics"]["sma"])
        self.assertEqual(risk["metrics"]["initial_cash"], 50000.0)

    def test_data_tampering_rejected(self):
        identifier = self.prepare()["experiment_id"]
        path = dialogue.DATA_ROOT / identifier / "prices.csv"
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            dialogue.load_experiment(identifier)

    def test_unsafe_code_is_saved_but_not_executed(self):
        identifier = self.prepare()["experiment_id"]
        with patch.object(dialogue.subprocess, "run") as runner:
            result = json.loads(Tools.execute_quant_code(identifier, "import os\nos.remove('data.csv')"))
        self.assertFalse(result["ok"])
        runner.assert_not_called()
        self.assertTrue(Path(result["code_path"]).is_file())

    def test_paths_cannot_escape_experiment_store(self):
        with self.assertRaises(ValueError):
            dialogue.load_experiment("../../OAI_CONFIG_LIST")
        identifier = self.prepare()["experiment_id"]
        with self.assertRaises(ValueError):
            Tools.check_quant_risk(identifier, "../../secret")

    def test_invalid_parameters_rejected_before_market_request(self):
        for kwargs in ({"fast": 20, "slow": 10}, {"max_position": 1.2}, {"initial_cash": -1},
                       {"symbol": "invalid"}, {"end_date": "2099-01-01"}):
            with self.assertRaises(ValueError):
                self.prepare(**kwargs)
        dialogue.get_market_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
