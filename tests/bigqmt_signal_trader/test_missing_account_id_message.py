"""The missing-account_id error has to say what was searched (issue #90).

"Big QMT account_id is required" was the whole message. It did not say which
modules were looked for, so a config file sitting one sys.path away from the
running interpreter produced exactly the same text as no config at all -- and
the reporter had in fact placed the file, just where their interpreter could
not import it.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat


class MissingAccountIdMessageTest(unittest.TestCase):
    def setUp(self):
        self._saved = {name: os.environ.get(name)
                       for name in ("BIGQMT_ACCOUNT_ID",
                                    xtquant_compat.CLIENT_CONFIG_MODULE_ENV)}
        for name in self._saved:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_it_names_the_modules_it_looked_for(self):
        message = xtquant_compat._missing_account_id_message()
        for module_name in xtquant_compat.DEFAULT_CLIENT_CONFIG_MODULES:
            self.assertIn(module_name, message)

    def test_it_offers_every_working_remedy(self):
        """All three are verified paths; the message must not omit one."""
        message = xtquant_compat._missing_account_id_message()

        self.assertIn("sys.path", message)
        self.assertIn("BIGQMT_ACCOUNT_ID", message)
        self.assertIn("configure(account_id=", message)
        self.assertIn("bigqmt-init", message)

    def test_it_mentions_the_import_time_timing_trap(self):
        """configure() runs at import, so a config placed afterwards is
        silently ignored until configure() is called again."""
        message = xtquant_compat._missing_account_id_message()
        self.assertIn("import time", message)

    def test_a_selected_module_from_the_environment_is_listed_first(self):
        os.environ[xtquant_compat.CLIENT_CONFIG_MODULE_ENV] = "my_own_config"
        message = xtquant_compat._missing_account_id_message()

        self.assertIn("my_own_config", message)
        self.assertLess(message.index("my_own_config"),
                        message.index("bigqmt_signal_trader_client_config"))

    def test_the_call_path_actually_raises_it(self):
        client = xtquant_compat.BigQmtRpcClient(account_id="")
        with self.assertRaises(ValueError) as caught:
            client.call("ping", {})

        message = str(caught.exception)
        self.assertIn("Big QMT account_id is required", message)
        self.assertIn("bigqmt_signal_trader_client_config", message)

    def test_a_config_that_imports_but_lacks_the_key_says_so(self):
        """Different cause, different message: the file was found, the key was
        not -- telling the reader to check sys.path would send them the wrong
        way."""
        module = type(sys)("fake_client_config")
        module.BIGQMT_REDIS_CONFIG = {"host": "127.0.0.1"}
        sys.modules["fake_client_config"] = module
        os.environ[xtquant_compat.CLIENT_CONFIG_MODULE_ENV] = "fake_client_config"
        try:
            message = xtquant_compat._missing_account_id_message()
        finally:
            sys.modules.pop("fake_client_config", None)

        self.assertIn("fake_client_config", message)
        self.assertIn("defines no BIGQMT_ACCOUNT_ID", message)

    def test_it_never_raises_while_building_the_message(self):
        """This runs on an error path; an exception here would replace a bad
        error message with a worse one."""
        saved = xtquant_compat.load_client_config
        xtquant_compat.load_client_config = lambda *a, **k: 1 / 0
        try:
            message = xtquant_compat._missing_account_id_message()
        finally:
            xtquant_compat.load_client_config = saved

        self.assertIn("Big QMT account_id is required", message)


if __name__ == "__main__":
    unittest.main()
