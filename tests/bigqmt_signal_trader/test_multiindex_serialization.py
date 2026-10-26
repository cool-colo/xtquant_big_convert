# coding: utf-8
"""Python 3.12 QMT terminals answer the multi-stock forms of
get_financial_data / get_top10_share_holder with a MultiIndex DataFrame
where pandas.Panel used to be (pandas removed Panel in 1.0; the 3.12
runtime ships pandas 3). The old serializer branch ran reset_index() on it
and flattened the tuple column labels into strings -- "('000002.SZ',
'close')" -- so the client rebuilt a frame it could not index by code
(measured live on 2.1.33, 2026-10-26). The fix slices by the first level
and reuses the Panel envelope, whose client rebuild is {code: DataFrame}.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pandas as pd  # noqa: E402

from bigqmt_signal_trader.redis_rpc import to_jsonable  # noqa: E402
from bigqmt_signal_trader.xtquant_compat import _restore_jsonable  # noqa: E402


def _financial_multiindex_frame():
    columns = pd.MultiIndex.from_tuples(
        [("601996.SH", "circulating_capital"), ("000002.SZ", "circulating_capital")]
    )
    frame = pd.DataFrame(
        [[7.0e9, 1.9e11], [7.1e9, 1.9e11]],
        index=["20260331", "20260630"],
        columns=columns,
    )
    return frame


def _top10_multiindex_frame():
    columns = pd.MultiIndex.from_tuples(
        [("000001.SZ", "holdName"), ("000001.SZ", "changeRatio")]
    )
    frame = pd.DataFrame(
        [[["holder-a", "holder-b"], [0.0, 0.0]]],
        index=["2026-06-30"],
        columns=columns,
    )
    return frame


class MultiIndexSerializationTest(unittest.TestCase):
    def test_financial_frame_becomes_panel_envelope(self):
        env = to_jsonable(_financial_multiindex_frame())
        self.assertEqual(env["__bigqmt_type__"], "Panel")
        self.assertEqual(env["items"], ["601996.SH", "000002.SZ"])
        self.assertEqual(env["major_axis"], ["20260331", "20260630"])
        self.assertEqual(env["minor_axis"], ["circulating_capital"])
        self.assertEqual(sorted(env["data"]), ["000002.SZ", "601996.SH"])
        sub = env["data"]["601996.SH"]
        self.assertEqual(sub["__bigqmt_type__"], "DataFrame")
        self.assertIn("circulating_capital", sub["columns"])

    def test_client_rebuilds_code_to_frame(self):
        env = to_jsonable(_financial_multiindex_frame())
        out = _restore_jsonable(env)
        self.assertIsInstance(out, dict)
        frame = out["601996.SH"]
        self.assertIsInstance(frame, pd.DataFrame)
        self.assertIn("circulating_capital", list(frame.columns))
        self.assertAlmostEqual(float(frame["circulating_capital"].iloc[0]), 7.0e9)

    def test_top10_list_cells_survive(self):
        out = _restore_jsonable(to_jsonable(_top10_multiindex_frame()))
        frame = out["000001.SZ"]
        self.assertEqual(list(frame["holdName"].iloc[0]), ["holder-a", "holder-b"])

    def test_plain_frame_still_uses_dataframe_envelope(self):
        frame = pd.DataFrame({"close": [1.0, 2.0]}, index=["20260101", "20260102"])
        env = to_jsonable(frame)
        self.assertEqual(env["__bigqmt_type__"], "DataFrame")
        self.assertIn("close", env["columns"])

    def test_mangled_columns_never_leak(self):
        """The pre-fix symptom: tuple column labels stringified on the wire."""
        env = to_jsonable(_financial_multiindex_frame())
        for column in env["data"]["601996.SH"]["columns"]:
            self.assertNotIn("(", str(column))
            self.assertNotIn(",", str(column))


if __name__ == "__main__":
    unittest.main()
