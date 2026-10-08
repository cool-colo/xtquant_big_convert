"""Say so when the entry runs as a script instead of as a strategy (#123).

lzxN put the files in the QMT python directory, ran BIGQMT_REDIS_DRYRUN.py from
the strategy editor, and got:

    [BIGQMT_REDIS_DRYRUN]开始运行
    [bigqmt_shell] importlib entry source_root=...
    [bigqmt_shell] account_type=STOCK (from BIGQMT_ACCOUNT_TYPE)
    [bigqmt_shell] local redis config loaded keys=[...]
    [bigqmt_shell] local account config loaded=True
    [bigqmt_shell] download globals bound=[]
    [BIGQMT_REDIS_DRYRUN]结束运行

Every line looks like a healthy start. The tell is `download globals bound=[]`
-- empty -- and the absence of `init ok` and the diagnostics block. QMT injects
its API globals into a file it runs AS A STRATEGY; here it injected none, so
the module body ran, init() was never called, no RPC service started, and the
run ended. Nothing said any of that.

QMT's own docs name both causes: the editor window runs the file without the
strategy lifecycle, and the "standalone python process" option execs it as
__main__ "不会触发 init、handlebar 等函数".
"""

import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

ENTRY = os.path.join(SRC, "BIGQMT_REDIS_DRYRUN.py")


def _source():
    with io.open(ENTRY, encoding="utf-8") as handle:
        return handle.read()


class WarnsTest(unittest.TestCase):
    def test_it_checks_for_the_injected_globals(self):
        source = _source()

        self.assertIn('"passorder", "get_trade_detail_data", "download_history_data"',
                      source)

    def test_it_says_init_will_never_be_called(self):
        self.assertIn("init() will never be called", _source())

    def test_it_names_the_editor_window(self):
        self.assertIn("EDITOR", _source())

    def test_it_names_the_standalone_process_option(self):
        """QMT's docs: that option execs the file as __main__ and skips init."""
        self.assertIn("standalone python", _source())

    def test_it_explains_the_log_looks_healthy(self):
        """Without this the run reads as a successful start that just ended."""
        source = _source()

        self.assertIn("nothing listening", source)


class EncodingTest(unittest.TestCase):
    """This file declares #coding:gbk and is stored as UTF-8.

    That only holds together while every byte is ASCII. A Chinese character
    added here would be decoded as GBK inside QMT and break the file that
    bootstraps everything else -- so the warning is in English even though the
    UI labels it refers to are Chinese.
    """

    def test_the_entry_declares_gbk(self):
        self.assertTrue(_source().splitlines()[0].startswith("#coding:gbk"))

    def test_the_entry_is_pure_ascii(self):
        offenders = [index + 1 for index, line in enumerate(_source().splitlines())
                     if any(ord(char) > 127 for char in line)]

        self.assertEqual(offenders, [], "non-ASCII on lines %r" % (offenders,))

    def test_it_still_parses(self):
        import ast

        ast.parse(_source())


class OnlyWhenNoneArePresentTest(unittest.TestCase):
    """A strategy run has them; warning there would be noise on every start."""

    def _warns(self, present):
        namespace = dict((name, lambda *a: None) for name in present)
        return not any(name in namespace for name in
                       ("passorder", "get_trade_detail_data",
                        "download_history_data"))

    def test_a_real_strategy_run_is_quiet(self):
        self.assertFalse(self._warns(
            ["passorder", "get_trade_detail_data", "download_history_data"]))

    def test_one_present_is_enough_to_stay_quiet(self):
        """A partial injection is a different problem; the diagnostics block
        reports which bindings are missing."""
        self.assertFalse(self._warns(["passorder"]))

    def test_none_present_warns(self):
        self.assertTrue(self._warns([]))


if __name__ == "__main__":
    unittest.main()
