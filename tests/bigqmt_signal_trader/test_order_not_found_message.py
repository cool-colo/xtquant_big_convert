"""A missing row after passorder must be reported as nonterminal (#152)."""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import io

SOURCE_PATH = os.path.join(ROOT, "src", "bigqmt_signal_trader", "redis_rpc.py")


def _message_block():
    with io.open(SOURCE_PATH, encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("passorder dispatched into QMT but no order row")
    return source[start:start + 500]


class NonterminalMessageTest(unittest.TestCase):
    def test_it_forbids_resubmission(self):
        self.assertIn("DO NOT RESUBMIT", _message_block())

    def test_it_names_callback_reconciliation(self):
        block = _message_block()
        self.assertIn("execution callback", block)
        self.assertIn("remark", block)

    def test_it_reports_the_lookup_count(self):
        self.assertIn("lookup(s)", _message_block())


if __name__ == "__main__":
    unittest.main()
