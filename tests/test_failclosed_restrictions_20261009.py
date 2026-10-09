"""
Fail-closed pin from the 2026-10-09 conformance audit (D11-08 / M-30).

'suspended' is the one restriction kind that refuses a buy, and it comes only
from the altered / halt feeds. When such a feed cannot be read, a name it does
not list must not read 'none': it reads 'unknown' (display only, never a
block) and the checker raises a warning (pinned in tests/test_result_checks.py,
test_altered_list_down_is_a_warning). ASCII only.

    python -m unittest tests.test_failclosed_restrictions_20261009 -v
"""
import unittest
from unittest import mock

import pandas as pd

import scanner.trade_restrictions as tr
from tests.test_trade_restrictions import (NoNetwork, SESSION, fake_get,
                                           no_ex_today, row)


def _info(altered_ok, altered=None):
    with mock.patch.object(tr, "_get_json", fake_get()), \
            mock.patch.object(tr, "load_ex_today", no_ex_today):
        info = tr.fetch_restrictions(SESSION)
    info["altered_ok"] = dict(altered_ok)
    info["altered"] = dict(altered or {})
    return info


def _kind(info, **kw):
    out = tr.annotate_restrictions(pd.DataFrame([row(**kw)]), info, SESSION)
    return out["Trade_Restriction"].iloc[0], out["Restriction_Flags"].iloc[0]


class AlteredFeedDownReadsUnknown(NoNetwork):
    def test_control_feed_up_a_quiet_name_is_none(self):
        info = _info({"OTC": True, "TSE": True})
        self.assertEqual(_kind(info, sid="9999", market="OTC")[0], "none")

    def test_feed_down_a_quiet_name_is_unknown_not_none(self):
        info = _info({"OTC": False, "TSE": True})
        self.assertEqual(_kind(info, sid="9999", market="OTC")[0], "unknown")
        # the other board is unaffected
        self.assertEqual(_kind(info, sid="9998", market="TSE")[0], "none")

    def test_feed_down_a_listed_suspension_is_still_suspended(self):
        info = _info({"OTC": False, "TSE": True}, {"9999": "suspended"})
        self.assertEqual(_kind(info, sid="9999", market="OTC")[0], "suspended")

    def test_a_known_disposition_outranks_unknown(self):
        info = _info({"OTC": False, "TSE": False})
        # 8227 is in disposition in the recorded fixture
        kind, flags = _kind(info, sid="8227", market="OTC")
        self.assertEqual(kind, "disposition")
        self.assertIn("unknown", flags)

    def test_unknown_never_blocks_the_buy(self):
        self.assertNotIn("unknown", tr.BLOCKING_RESTRICTIONS)

    def test_info_without_the_key_is_left_alone(self):
        # a hand-built info that never recorded altered_ok keeps the old reading
        info = _info({"OTC": True, "TSE": True})
        del info["altered_ok"]
        self.assertEqual(_kind(info, sid="9999", market="OTC")[0], "none")


if __name__ == "__main__":
    unittest.main()
