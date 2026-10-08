"""Two failures that only appear in the single-file QMT sandbox build (#76).

Both were reported by heimo88 against a deployed v0.2.10 with a live repro on
2026-08-27, and neither is visible in a normal package deployment -- which is
exactly why the whole existing suite stayed green through both.

1. ``from xtquant.xtconstant import *`` (introduced by #73) is a SyntaxError in
   the single-file builds, which exec each module inside a function body.
   Nothing in a normal install compiles that way, so nothing caught it.

2. exec_events was imported from inside the order/deal callback. QMT runs those
   on a C++ thread entered through PyGILState_Ensure, where the first exec of a
   not-yet-imported module fails in the C layer without setting a Python
   exception -- SystemError "error return without exception set". A normal
   deployment preloads the module during init, so the lazy import hits the
   sys.modules cache and never execs anything on that thread.
"""

import io
import os
import sys
import textwrap
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


def _module_source(relative_path):
    with io.open(os.path.join(SRC, relative_path), encoding="utf-8") as handle:
        return handle.read()


class FunctionScopeCompileTest(unittest.TestCase):
    """A single-file build execs each module inside a function body."""

    def _compile_in_function(self, relative_path):
        source = _module_source(relative_path)
        wrapped = "def _single_file_module():\n" + textwrap.indent(source, "    ")
        compile(wrapped, "<single-file-build:%s>" % relative_path, "exec")

    def test_xtquant_compat_compiles_inside_a_function_body(self):
        # Before the fix this raised: "import * only allowed at module level".
        self._compile_in_function("bigqmt_signal_trader/xtquant_compat.py")

    def test_no_star_import_anywhere_in_the_bundled_package(self):
        """One star import is all it takes to break the build again."""
        offenders = []
        for directory, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                # Generated single-file builds are artifacts, not sources, and
                # the flat one is written as GBK. tests/test_single_file_build.py
                # scans those with the right encoding.
                if not name.endswith(".py") or name.endswith("_ALL_IN_ONE.py"):
                    continue
                path = os.path.join(directory, name)
                with io.open(path, encoding="utf-8", errors="replace") as handle:
                    for lineno, line in enumerate(handle, 1):
                        if line.lstrip().startswith("from ") and line.rstrip().endswith(
                            "import *"
                        ):
                            offenders.append(
                                "%s:%d" % (os.path.relpath(path, SRC), lineno)
                            )
        self.assertEqual(offenders, [], "star imports break single-file builds")


class ConstantReExportTest(unittest.TestCase):
    """``import *`` was also the only thing keeping these names on the module.

    docs/XTQUANT_COMPAT_REPLACEMENT.md documents
    ``from bigqmt_signal_trader import xtquant_compat as xtconstant``, so the
    obvious fix -- importing just the three names the module uses -- would have
    silently dropped 536 constants out from under that documented path.
    """

    def _both(self):
        from xtquant import xtconstant
        from bigqmt_signal_trader import xtquant_compat

        return xtconstant, xtquant_compat

    def test_every_public_constant_is_still_reachable(self):
        xtconstant, xtquant_compat = self._both()
        exported = [n for n in dir(xtconstant) if not n.startswith("_")]
        missing = [n for n in exported if not hasattr(xtquant_compat, n)]

        self.assertEqual(missing, [])
        self.assertGreater(len(exported), 500)

    def test_values_match_the_source_of_truth(self):
        xtconstant, xtquant_compat = self._both()
        for name in dir(xtconstant):
            if name.startswith("_"):
                continue
            self.assertIs(
                getattr(xtquant_compat, name),
                getattr(xtconstant, name),
                "%s diverged from xtquant.xtconstant" % name,
            )

    def test_mixed_case_constants_survive(self):
        """A .isupper() filter would look right and drop these four."""
        _, xtquant_compat = self._both()
        for name in (
            "EESO_ActiveFirst",
            "EESO_ActiveFirstFull",
            "EESO_ConcurrentlyOrder",
            # sic -- the native SDK spells it "ClOSE"; we mirror it verbatim.
            "OFFSET_FLAG_ClOSEYESTERDAY",
        ):
            self.assertTrue(hasattr(xtquant_compat, name), name)

    def test_the_documented_migration_path_still_reads_constants(self):
        from bigqmt_signal_trader import xtquant_compat as xtconstant

        self.assertEqual(xtconstant.STOCK_BUY, 23)
        self.assertEqual(xtconstant.STOCK_SELL, 24)
        self.assertEqual(xtconstant.FIX_PRICE, 11)
        self.assertEqual(xtconstant.ORDER_SUCCEEDED, 56)

    def test_backfill_does_not_shadow_the_module_s_own_names(self):
        """setdefault, not update: xtquant_compat's own definitions win."""
        from bigqmt_signal_trader import xtquant_compat

        self.assertEqual(xtquant_compat.DEFAULT_MARKET_DATA_CHUNK, 100)
        self.assertTrue(hasattr(xtquant_compat, "BigQmtXtTrader"))


class ExecEventsPreloadTest(unittest.TestCase):
    """The callback path must not be where exec_events first gets exec'd."""

    def test_module_is_loaded_at_import_time(self):
        import bigqmt_signal_trader_strategy as strategy

        self.assertIsNotNone(
            strategy._exec_events,
            "exec_events must be resolved at module load, not in the callback",
        )
        self.assertTrue(hasattr(strategy._exec_events, "normalize_order_event"))

    def test_publish_path_contains_no_import_statement(self):
        """An import here runs on QMT's PyGILState_Ensure callback thread."""
        source = _module_source("bigqmt_signal_trader_strategy.py")
        start = source.index("def _publish_exec_event(")
        end = source.index("\ndef ", start + 1)
        body = source[start:end]

        self.assertNotIn("import exec_events", body)
        self.assertNotIn("import_module", body)

    def test_publish_is_a_no_op_when_the_module_failed_to_load(self):
        """Load failure disables pushing; it must not raise into the callback.

        A raise here would reach QMT's callback and stop the strategy.
        """
        import bigqmt_signal_trader_strategy as strategy

        saved = strategy._exec_events
        strategy._exec_events = None
        try:
            self.assertIsNone(strategy._publish_exec_event("order", object()))
        finally:
            strategy._exec_events = saved

    def test_publish_failures_log_the_exception_type_and_traceback(self):
        """str(exc) alone reads "error return without exception set" and stops
        there -- that is what made this take a day to diagnose."""
        import bigqmt_signal_trader_strategy as strategy

        logged = []

        class _Boom(object):
            def __getattr__(self, name):
                raise SystemError("error return without exception set")

        class _Events(object):
            def normalize_order_event(self, obj, account_id):
                raise SystemError("error return without exception set")

        saved = {
            name: getattr(strategy, name)
            for name in ("_log_err", "_exec_event_sink", "_exec_events", "_build_config")
        }
        strategy._log_err = lambda name, msg: logged.append(msg)
        strategy._exec_event_sink = lambda config: object()
        strategy._exec_events = _Events()
        strategy._build_config = lambda: {"account_id": "acct", "exec_events": {"enabled": True}}
        try:
            strategy._publish_exec_event("order", _Boom())
        finally:
            for name, value in saved.items():
                setattr(strategy, name, value)

        self.assertEqual(len(logged), 1, logged)
        message = logged[0]
        self.assertIn("SystemError", message)
        self.assertIn("error return without exception set", message)
        self.assertIn("Traceback", message)


if __name__ == "__main__":
    unittest.main()
