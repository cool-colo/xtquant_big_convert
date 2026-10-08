"""account_type has to be discoverable, and visible once resolved (issue #92).

A credit account queried as STOCK does not error -- `get_trade_detail_data`
returns an all-zero asset row. So getting this setting wrong shows up as
"credit assets are all 0" with nothing in the logs pointing at the cause.

Three places looked plausible and only one worked:

  BIGQMT_ACCOUNT_TYPE in the local config      worked
  account_type inside BIGQMT_REDIS_CONFIG      silently ignored
  ACCOUNT_TYPE in the runtime module           silently overwritten, because
                                               the shipped example config sets
                                               BIGQMT_ACCOUNT_TYPE = "STOCK"

All three are honoured now, and the resolved value is printed with its source.
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


PROBE = (
    "import sys\n"
    "sys.path.insert(0, %r)\n"
    "import bigqmt_signal_trader_redis_rpc_runtime as rt\n"
    "print('RESOLVED|%%s|%%s' %% (rt.ACCOUNT_TYPE, rt.ACCOUNT_TYPE_SOURCE))\n"
) % SRC


def resolve(config_source):
    """Import the runtime in a fresh interpreter against a generated config."""
    workdir = tempfile.mkdtemp()
    with io.open(os.path.join(workdir, "bigqmt_signal_trader_local_config.py"),
                 "w", encoding="utf-8") as handle:
        handle.write(config_source)
    script = os.path.join(workdir, "probe.py")
    with io.open(script, "w", encoding="utf-8") as handle:
        handle.write(PROBE)
    completed = subprocess.run(
        [sys.executable, script], cwd=workdir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise AssertionError("probe failed:\n%s" % output)
    line = [row for row in output.splitlines() if row.startswith("RESOLVED|")]
    if not line:
        raise AssertionError("probe printed nothing usable:\n%s" % output)
    _tag, account_type, source = line[0].split("|", 2)
    return account_type, source, output


BASE = 'BIGQMT_ACCOUNT_ID = "acct"\n'


class AccountTypeSourcesTest(unittest.TestCase):
    def test_documented_variable_wins(self):
        account_type, source, _ = resolve(
            BASE + 'BIGQMT_ACCOUNT_TYPE = "CREDIT"\n'
                   'BIGQMT_REDIS_CONFIG = {"host": "127.0.0.1"}\n')

        self.assertEqual(account_type, "CREDIT")
        self.assertEqual(source, "BIGQMT_ACCOUNT_TYPE")

    def test_redis_config_key_is_honoured(self):
        """Where the reporter put it. It used to be read by nothing."""
        account_type, source, _ = resolve(
            BASE + 'BIGQMT_REDIS_CONFIG = {"host": "1", "account_type": "CREDIT"}\n')

        self.assertEqual(account_type, "CREDIT")
        self.assertIn("BIGQMT_REDIS_CONFIG", source)

    def test_nothing_set_falls_back_to_stock(self):
        account_type, source, _ = resolve(
            BASE + 'BIGQMT_REDIS_CONFIG = {"host": "127.0.0.1"}\n')

        self.assertEqual(account_type, "STOCK")
        self.assertEqual(source, "default")

    def test_value_is_normalised(self):
        account_type, _source, _ = resolve(
            BASE + 'BIGQMT_ACCOUNT_TYPE = "  credit  "\n'
                   'BIGQMT_REDIS_CONFIG = {}\n')

        self.assertEqual(account_type, "CREDIT")

    def test_a_config_without_the_key_keeps_the_account_id(self):
        """An older local config must not lose its account id just because it
        predates BIGQMT_ACCOUNT_TYPE."""
        _account_type, _source, output = resolve(
            BASE + 'BIGQMT_REDIS_CONFIG = {"host": "10.0.0.5"}\n')

        self.assertNotIn("Traceback", output)


class ResolutionIsVisibleTest(unittest.TestCase):
    """Silence is what turned a one-line setting into "assets are all 0"."""

    def test_the_resolved_type_and_its_source_are_printed(self):
        _account_type, _source, output = resolve(
            BASE + 'BIGQMT_ACCOUNT_TYPE = "CREDIT"\n'
                   'BIGQMT_REDIS_CONFIG = {}\n')

        self.assertIn("account_type=CREDIT", output)
        self.assertIn("BIGQMT_ACCOUNT_TYPE", output)

    def test_conflicting_settings_are_called_out(self):
        _account_type, _source, output = resolve(
            BASE + 'BIGQMT_ACCOUNT_TYPE = "CREDIT"\n'
                   'BIGQMT_REDIS_CONFIG = {"account_type": "STOCK"}\n')

        self.assertIn("ignored conflicting", output)
        self.assertIn("BIGQMT_REDIS_CONFIG", output)

    def test_the_shipped_default_is_not_reported_as_a_conflict(self):
        """ACCOUNT_TYPE ships as STOCK, so every credit setup would otherwise
        report a phantom conflict with it."""
        _account_type, _source, output = resolve(
            BASE + 'BIGQMT_ACCOUNT_TYPE = "CREDIT"\n'
                   'BIGQMT_REDIS_CONFIG = {}\n')

        self.assertNotIn("ignored conflicting", output)


class ReachesTheAssetQueryTest(unittest.TestCase):
    """The setting only matters if it arrives at get_trade_detail_data."""

    def test_configured_type_reaches_get_asset(self):
        import bigqmt_signal_trader_strategy as strategy
        from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider

        saved = dict(strategy._config)
        try:
            strategy.configure(mode="bigqmt", account_id="acct", account_type="CREDIT")
            config = strategy._build_config()
        finally:
            strategy._config.clear()
            strategy._config.update(saved)

        self.assertEqual(config.get("account_type"), "CREDIT")

        seen = []

        def query(account, account_type, detail_type, *args):
            seen.append((account_type, detail_type))
            return []

        provider = BigQmtPositionProvider(
            get_trade_detail_data_func=query,
            account_type=config.get("account_type", "STOCK"))
        provider.get_asset("acct")

        self.assertEqual(seen[0], ("CREDIT", "ACCOUNT"))

    def test_positions_use_the_same_type(self):
        from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider

        seen = []

        def query(account, account_type, detail_type, *args):
            seen.append((account_type, detail_type))
            return []

        provider = BigQmtPositionProvider(
            get_trade_detail_data_func=query, account_type="CREDIT")
        provider.get_positions("acct")

        self.assertEqual(seen[0][0], "CREDIT")


if __name__ == "__main__":
    unittest.main()
