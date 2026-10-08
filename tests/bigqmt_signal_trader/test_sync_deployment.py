"""Pushing the package into a QMT deployment.

Deploying is a file copy that used to be manual, which is how a fix gets
confirmed locally and then debugged for an hour because it never reached the
terminal. The copy runs on the client, not inside QMT: a trading process
rewriting its own code mid-session would put whatever is in the source tree --
half-finished edits included -- straight onto the live terminal.

The properties that matter here are the ones about *not* writing: config files
hold the account id and credentials, and a deployment should not grow files it
never had.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import sync as sync_module
from bigqmt_signal_trader.sync import NEVER_OVERWRITE, sync_deployment


def _write(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.source = tempfile.mkdtemp()
        self.target = tempfile.mkdtemp()
        _write(os.path.join(self.source, "bigqmt_signal_trader", "__init__.py"), "new")
        _write(os.path.join(self.source, "bigqmt_signal_trader", "redis_rpc.py"), "new rpc")
        _write(os.path.join(self.source, "bigqmt_signal_trader_strategy.py"), "new strategy")
        _write(os.path.join(self.target, "bigqmt_signal_trader", "__init__.py"), "old")
        _write(os.path.join(self.target, "bigqmt_signal_trader", "redis_rpc.py"), "old rpc")

    def tearDown(self):
        for path in (self.source, self.target):
            shutil.rmtree(path, ignore_errors=True)

    def _sync(self, **kwargs):
        return sync_deployment(self.target, source_root=self.source, **kwargs)

    def test_stale_files_are_replaced(self):
        result = self._sync()

        self.assertEqual(
            _read(os.path.join(self.target, "bigqmt_signal_trader", "redis_rpc.py")),
            "new rpc")
        self.assertIn(os.path.join("bigqmt_signal_trader", "redis_rpc.py"),
                      result["updated"])

    def test_identical_files_are_left_alone(self):
        self._sync()
        result = self._sync()

        self.assertEqual(result["updated"], [])
        self.assertGreater(result["identical"], 0)

    def test_the_overwritten_file_is_backed_up(self):
        result = self._sync()
        backup = os.path.join(self.target, "bigqmt_signal_trader",
                              "redis_rpc.py" + result["backup_suffix"])

        self.assertTrue(os.path.exists(backup))
        self.assertEqual(_read(backup), "old rpc")

    def test_a_restart_is_always_reported_as_required(self):
        """QMT keeps modules across strategy re-runs -- a copy alone does
        nothing, and that is not obvious from the outside."""
        self.assertTrue(self._sync()["restart_required"])
        self.assertFalse(self._sync()["restart_required"])   # nothing changed


class ConfigFilesAreNeverWrittenTest(unittest.TestCase):
    """These hold the account id and Redis credentials."""

    def setUp(self):
        self.source = tempfile.mkdtemp()
        self.target = tempfile.mkdtemp()
        for name in NEVER_OVERWRITE:
            _write(os.path.join(self.source, name), "PACKAGED PLACEHOLDER")
            _write(os.path.join(self.target, name), "REAL ACCOUNT 8886800503")
        _write(os.path.join(self.source, "bigqmt_signal_trader", "__init__.py"), "new")
        _write(os.path.join(self.target, "bigqmt_signal_trader", "__init__.py"), "old")

    def tearDown(self):
        for path in (self.source, self.target):
            shutil.rmtree(path, ignore_errors=True)

    def test_they_survive_a_sync(self):
        sync_deployment(self.target, source_root=self.source)

        for name in NEVER_OVERWRITE:
            self.assertEqual(_read(os.path.join(self.target, name)),
                             "REAL ACCOUNT 8886800503", name)

    def test_they_are_reported_as_skipped(self):
        result = sync_deployment(self.target, source_root=self.source)

        for name in NEVER_OVERWRITE:
            self.assertIn(name, result["skipped_config"])

    def test_example_configs_are_not_config_files(self):
        """The .example.py files are documentation and should be refreshed."""
        for name in NEVER_OVERWRITE:
            self.assertNotIn(name.replace(".py", ".example.py"), NEVER_OVERWRITE)


class TraderSyncWrapperTest(unittest.TestCase):
    """xt_trader.sync_deployment() must work from the trader object.

    The wrapper called self.get_deployment_info(), which only exists on
    BigQmtXtData -- so the trader path died with AttributeError before
    copying a single byte (found while deploying #160: had to drive
    sync.sync_deployment by hand).
    """

    def _trader(self, target):
        import types

        from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader

        trader = BigQmtXtTrader(account_id="acct")
        calls = []

        def fake_call(method, params=None, **kwargs):
            calls.append(method)
            if method == "get_deployment_info":
                return {"qmt_python_dir": target, "version": "0.0.0-test"}
            return {}

        trader.client = types.SimpleNamespace(account_id="acct", call=fake_call)
        return trader, calls

    def test_trader_wrapper_completes(self):
        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target, True)
        trader, calls = self._trader(target)

        result = trader.sync_deployment()

        self.assertIn("get_deployment_info", calls)
        self.assertFalse(result.get("error"))
        self.assertTrue(os.path.isfile(
            os.path.join(target, "bigqmt_signal_trader", "version.py")))

    def test_trader_wrapper_reports_a_missing_dir_instead_of_raising(self):
        trader, _ = self._trader("")
        result = trader.sync_deployment()
        self.assertEqual(result.get("error"), "no qmt_python_dir reported")


class DoesNotGrowTheDeploymentTest(unittest.TestCase):
    def setUp(self):
        self.source = tempfile.mkdtemp()
        self.target = tempfile.mkdtemp()
        _write(os.path.join(self.source, "some_other_entry.py"), "not yours")
        _write(os.path.join(self.source, "bigqmt_signal_trader", "__init__.py"), "new")

    def tearDown(self):
        for path in (self.source, self.target):
            shutil.rmtree(path, ignore_errors=True)

    def test_a_top_level_module_the_target_lacks_is_not_pushed(self):
        sync_deployment(self.target, source_root=self.source)

        self.assertFalse(os.path.exists(os.path.join(self.target, "some_other_entry.py")))

    def test_the_strategy_entry_is_the_exception(self):
        """It is the one file a fresh deployment needs."""
        sync_deployment(self.target, source_root=self.source)
        _write(os.path.join(self.source, sync_module.STRATEGY_ENTRY), "entry")

        sync_deployment(self.target, source_root=self.source)

        self.assertTrue(os.path.exists(
            os.path.join(self.target, sync_module.STRATEGY_ENTRY)))

    def test_the_package_tree_is_created(self):
        sync_deployment(self.target, source_root=self.source)

        self.assertTrue(os.path.exists(
            os.path.join(self.target, "bigqmt_signal_trader", "__init__.py")))


class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.source = tempfile.mkdtemp()
        self.target = tempfile.mkdtemp()
        _write(os.path.join(self.source, "bigqmt_signal_trader", "__init__.py"), "new")
        _write(os.path.join(self.target, "bigqmt_signal_trader", "__init__.py"), "old")

    def tearDown(self):
        for path in (self.source, self.target):
            shutil.rmtree(path, ignore_errors=True)

    def test_it_writes_nothing(self):
        result = sync_deployment(self.target, source_root=self.source, dry_run=True)

        self.assertEqual(
            _read(os.path.join(self.target, "bigqmt_signal_trader", "__init__.py")),
            "old")
        self.assertTrue(result["updated"])   # still reports the plan


class MissingTargetTest(unittest.TestCase):
    def test_it_reports_rather_than_raises(self):
        result = sync_deployment("D:/nowhere/at/all")

        self.assertIn("error", result)
        self.assertEqual(result["updated"], [])


class AutoSyncIsOptInTest(unittest.TestCase):
    """Writing into a live trading terminal must not be a side effect of
    connecting."""

    def test_it_is_off_unless_asked_for(self):
        from bigqmt_signal_trader.xtquant_compat import auto_sync_enabled

        saved = os.environ.pop("BIGQMT_AUTO_SYNC", None)
        try:
            self.assertFalse(auto_sync_enabled())
            os.environ["BIGQMT_AUTO_SYNC"] = "1"
            self.assertTrue(auto_sync_enabled())
        finally:
            os.environ.pop("BIGQMT_AUTO_SYNC", None)
            if saved is not None:
                os.environ["BIGQMT_AUTO_SYNC"] = saved


if __name__ == "__main__":
    unittest.main()
