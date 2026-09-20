"""
Parser tests for the market feeds (ingestion/price_volume_multi.py).

The 2026-09-20 audit's AUD-009: every test in this project sat above the
ingestion layer, so the code that turns an exchange response into stored
prices -- the layer where a wrong number becomes a wrong stop -- had none. It
is also the layer that produced the two real data defects this project has
hit: Yahoo's zero-volume holiday bars, and the placeholder bar for a session
that has not traded yet.

No network here: the HTTP call is mocked, so these run in CI and pin the
PARSING, which is what actually breaks when an endpoint changes shape.

    python -m unittest tests.test_ingestion_parsing -v
"""
import unittest
from unittest import mock

import pandas as pd

from ingestion.price_volume_multi import (
    _drop_synthetic_bars, _normalize_yf_single, _roc_to_iso, _tpex_one_month,
    _twse_one_month,
)


def twse_payload(data):
    return {"stat": "OK", "data": data}


class RocDates(unittest.TestCase):
    def test_roc_to_iso(self):
        self.assertEqual(_roc_to_iso("115/09/18"), "2026-09-18")

    def test_junk_is_none_not_an_exception(self):
        for bad in ("", "abc", "115/13", None):
            self.assertIsNone(_roc_to_iso(bad))


class TwseMonth(unittest.TestCase):
    """TWSE STOCK_DAY: ROC dates, thousands separators, share volume."""

    def call(self, payload):
        resp = mock.Mock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        with mock.patch("ingestion.price_volume_multi.requests.get",
                        return_value=resp) as get:
            df = _twse_one_month("2330", 2026, 9)
        return df, get

    def test_parses_a_normal_row(self):
        df, get = self.call(twse_payload([
            ["115/09/18", "40,892,688", "100,000,000", "2,460.00", "2,460.00",
             "2,435.00", "2,460.00", "+25.00", "60,123"],
        ]))
        self.assertEqual(len(df), 1)
        r = df.iloc[0]
        self.assertEqual(r["date"], "2026-09-18")
        self.assertAlmostEqual(r["open"], 2460.0)
        self.assertAlmostEqual(r["low"], 2435.0)
        self.assertAlmostEqual(r["volume_share"], 40892688.0)
        # verification must stay on -- the point of the 2026-09-20 change
        self.assertNotIn("verify", get.call_args.kwargs)

    def test_bad_rows_are_skipped_not_fatal(self):
        df, _ = self.call(twse_payload([
            ["115/09/17", "1,000", "1", "10.0", "11.0", "9.0", "10.5", "", ""],
            ["115/09/18", "x", "1", "n/a", "11.0", "9.0", "10.5", "", ""],
            ["too", "short"],
        ]))
        self.assertEqual(list(df["date"]), ["2026-09-17"])

    def test_not_ok_payload_is_empty(self):
        df, _ = self.call({"stat": "very sorry", "data": []})
        self.assertTrue(df.empty)

    def test_html_instead_of_json_is_empty(self):
        resp = mock.Mock()
        resp.json.side_effect = ValueError("not json")
        resp.raise_for_status.return_value = None
        with mock.patch("ingestion.price_volume_multi.requests.get", return_value=resp):
            self.assertTrue(_twse_one_month("2330", 2026, 9).empty)


class TpexMonth(unittest.TestCase):
    """TPEX reports volume in THOUSAND shares; the store keeps shares."""

    def test_volume_is_scaled_to_shares(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"tables": [{"data": [
            ["115/09/18", "15,864", "1,000", "925.00", "960.00", "901.00",
             "944.00", "+19.00", "1,234"],
        ]}]}
        with mock.patch("ingestion.price_volume_multi.requests.get", return_value=resp):
            df = _tpex_one_month("6488", 2026, 9)
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(df.iloc[0]["volume_share"], 15864000.0)
        self.assertAlmostEqual(df.iloc[0]["close"], 944.0)

    def test_empty_tables_is_empty_frame(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"tables": []}
        with mock.patch("ingestion.price_volume_multi.requests.get", return_value=resp):
            self.assertTrue(_tpex_one_month("6488", 2026, 9).empty)


class YfinanceNormalise(unittest.TestCase):
    def frame(self, rows):
        return pd.DataFrame(rows).set_index("Date")

    def test_zero_volume_closure_bar_is_dropped(self):
        # 2026-07-10 typhoon: Yahoo printed a flat bar on every ticker
        sub = self.frame([
            {"Date": pd.Timestamp("2026-07-09"), "Open": 10.0, "High": 11.0,
             "Low": 9.0, "Close": 10.5, "Volume": 1000},
            {"Date": pd.Timestamp("2026-07-10"), "Open": 10.5, "High": 10.5,
             "Low": 10.5, "Close": 10.5, "Volume": 0},
        ])
        out = _normalize_yf_single(sub, "1234")
        self.assertEqual(list(out["date"]), ["2026-07-09"])

    def test_placeholder_bar_with_volume_survives_here_by_design(self):
        """The 2026-09-20 Sunday bar carried real volume, so this layer keeps
        it -- one ticker cannot tell a placeholder from a thin session. It is
        scanner.data_integrity, which sees the whole market, that drops it;
        this test exists so the division of labour is explicit and a future
        reader does not "fix" it in the wrong place."""
        sub = self.frame([
            {"Date": pd.Timestamp("2026-09-18"), "Open": 217.5, "High": 217.5,
             "Low": 214.5, "Close": 216.5, "Volume": 2733461},
            {"Date": pd.Timestamp("2026-09-20"), "Open": 217.5, "High": 217.5,
             "Low": 214.5, "Close": 216.5, "Volume": 48692},
        ])
        out = _normalize_yf_single(sub, "2912")
        self.assertEqual(list(out["date"]), ["2026-09-18", "2026-09-20"])

        from scanner.data_integrity import nonsession_dates
        # the market-wide view, where the placeholder is one name in a thousand
        counts = [("2026-09-%02d" % d, 1900) for d in (14, 15, 16, 17, 18)]
        counts.append(("2026-09-20", 22))
        self.assertEqual(nonsession_dates(counts), ["2026-09-20"])

    def test_missing_columns_are_an_empty_frame_not_a_crash(self):
        sub = pd.DataFrame({"Close": [1.0]})
        self.assertTrue(_normalize_yf_single(sub, "1234").empty)

    def test_drop_synthetic_bars_tolerates_a_frame_without_volume(self):
        df = pd.DataFrame({"date": ["2026-09-18"], "close": [10.0]})
        self.assertEqual(len(_drop_synthetic_bars(df)), 1)


if __name__ == "__main__":
    unittest.main()
