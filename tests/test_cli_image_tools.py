"""Notebook tools must also work in the documented plain-Python CLI."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from finrobot.functional.coding import IPythonUtils


class CliImageTests(unittest.TestCase):
    def test_plain_python_preserves_cells_and_returns_image_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyst's chart.png"
            Image.new("RGB", (2, 2)).save(path)
            code = """
import sys
from finrobot.functional.coding import IPythonUtils
assert IPythonUtils.exec_python('value = 21') == ''
assert IPythonUtils.exec_python('value * 2') == '42'
assert IPythonUtils.display_image(sys.argv[1]) == 'Image available at: ' + sys.argv[1]
"""
            proc = subprocess.run([sys.executable, "-c", code, str(path.resolve())],
                                  text=True, capture_output=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_notebook_display_uses_path_as_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyst's chart.png"
            Image.new("RGB", (2, 2)).save(path)
            with patch("finrobot.functional.coding.get_ipython", return_value=object()), \
                    patch("IPython.display.display") as display:
                result = IPythonUtils.display_image(str(path))
            self.assertIn(str(path.resolve()), result)
            self.assertIn("displayed successfully", result)
            display.assert_called_once()

    def test_missing_image_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                IPythonUtils.display_image(str(Path(directory) / "missing.png"))

    def test_cell_error_is_returned(self):
        with patch("finrobot.functional.coding.get_ipython", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = IPythonUtils.exec_python("1 / 0")
        self.assertIn("division by zero", result)


if __name__ == "__main__":
    unittest.main()
