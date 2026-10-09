"""
Fail-closed pin from the 2026-10-09 conformance audit (D7-01).

History backfill used to label every "00"-prefixed code as TSE, so the ~117
ETFs that list on the OTC board were fetched with the .TW suffix and never got
history. The board now comes from today's own snapshot. ASCII only.

    python -m unittest tests.test_failclosed_backfill_20261009 -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import scanner.market_snapshot as ms
from tests.test_ingestion_parsing import BackfillBudget


class BoardsComeFromTheSnapshot(BackfillBudget):
    def _wanted(self, boards):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "pv.db")
            self._db(db, [("00679B", 1, 500.0), ("0050", 1, 400.0),
                          ("00858", 1, 300.0), ("1111", 1, 200.0)])
            return ms.stocks_needing_history(db, min_bars=60, limit=10,
                                             boards=boards)

    def test_an_otc_etf_is_labelled_otc(self):
        got = self._wanted({"00679B": "OTC", "00858": "OTC", "0050": "TSE",
                            "1111": "TSE"})
        self.assertEqual(got["00679B"], "OTC")
        self.assertEqual(got["00858"], "OTC")
        self.assertEqual(got["0050"], "TSE")
        self.assertEqual(got["1111"], "TSE")

    def test_no_00_prefix_shortcut(self):
        got = self._wanted(None)
        self.assertIsNone(got["00679B"])          # the fetcher probes both
        self.assertIsNone(got["0050"])

    def test_snapshot_health_carries_the_board_map(self):
        tse = pd.DataFrame({"stock_id": ["0050", "2330"]})
        otc = pd.DataFrame({"stock_id": ["00679B", "8069"]})
        with mock.patch.object(ms, "parse_tse", lambda raw: tse), \
                mock.patch.object(ms, "parse_otc", lambda raw: otc), \
                mock.patch("scanner.market_filter._fetch_json", lambda *a, **k: []), \
                mock.patch.dict(ms.MIN_ROWS, {"TSE": 1, "OTC": 1}):
            _df, health = ms.fetch_snapshot(log=lambda *a, **k: None)
        self.assertEqual(health["boards"], {"0050": "TSE", "2330": "TSE",
                                            "00679B": "OTC", "8069": "OTC"})


if __name__ == "__main__":
    unittest.main()
