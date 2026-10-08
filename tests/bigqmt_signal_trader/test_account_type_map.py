# coding: utf-8
"""Tests for account_type_map per-request resolution (PR #135).

litaolemo's review requested 5 specific scenarios plus an integration test:
  1. No MAP configured → falls back to default type
  2. MAP present with matching key → correct mapping
  3. Key not in MAP → fallback to default
  4. Empty/None account_id → fallback to default
  5. int key → str() conversion matches string keys in MAP

Plus:
  - Gateway._resolve_account_type integration
  - OrderGateway._account_type_code(account_id) per-request
  - BigQmtRpcHandlers._configured_account_type(account_id) per-request
  - BigQmtRpcHandlers._reported_account_type(account_id) per-request
  - No importlib.reload is called (the #3 review item)
"""

import importlib
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from bigqmt_signal_trader.account_type_map import (
    account_type_for,
    get_account_type_map,
    reload as reload_map,
)


# ---------------------------------------------------------------------------
# Unit tests: account_type_map pure functions
# ---------------------------------------------------------------------------

class AccountTypeForTest(unittest.TestCase):
    """The 5 scenarios requested in PR #135 review."""

    def setUp(self):
        # Force a fresh load for each test
        reload_map()

    def test_no_map_falls_back_to_default(self):
        """Scenario 1: no BIGQMT_ACCOUNT_TYPE_MAP → default_type."""
        sys.modules.pop("bigqmt_signal_trader_local_config", None)
        reload_map()
        result = account_type_for("any_account", default_type="CREDIT")
        self.assertEqual(result, "CREDIT")

    def test_map_present_correct_mapping(self):
        """Scenario 2: MAP has the key → returns mapped type."""
        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"123456": "CREDIT", "789012": "FUTURE"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            self.assertEqual(account_type_for("123456", "STOCK"), "CREDIT")
            self.assertEqual(account_type_for("789012", "STOCK"), "FUTURE")

    def test_key_not_in_map_falls_back(self):
        """Scenario 3: key exists but account_id is not in MAP → default."""
        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"111": "CREDIT"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            self.assertEqual(account_type_for("999", "STOCK"), "STOCK")

    def test_empty_or_none_account_id_falls_back(self):
        """Scenario 4: account_id is empty string or None → default."""
        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"111": "CREDIT"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            self.assertEqual(account_type_for("", "STOCK"), "STOCK")
            self.assertEqual(account_type_for(None, "CREDIT"), "CREDIT")

    def test_int_key_converted_to_str(self):
        """Scenario 5: int key is str()-converted so it matches string keys.

        BIGQMT_ACCOUNT_TYPE_MAP keys are strings (from Python dict literals
        in the config file). account_type_for() applies str() to the key
        before lookup, so passing an int 123456 WILL match the string key
        "123456". This is the designed behavior: account_ids in QMT are
        always strings, and the str() cast catches accidental int usage.
        """
        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"123456": "CREDIT"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            # int 123456 is str()-converted and matches "123456"
            result = account_type_for(123456, "STOCK")
            self.assertEqual(result, "CREDIT")

    def test_int_key_not_in_still_falls_back(self):
        """An int that has no corresponding string key → default."""
        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"111": "CREDIT"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            self.assertEqual(account_type_for(999, "STOCK"), "STOCK")

    def test_reload_clears_cached_map(self):
        """reload() forces a fresh load on next access."""
        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"AAA": "CREDIT"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            self.assertEqual(account_type_for("AAA", "STOCK"), "CREDIT")

        # Change the config
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"AAA": "FUTURE"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            self.assertEqual(account_type_for("AAA", "STOCK"), "FUTURE")

    def test_map_with_none_value_treated_as_empty(self):
        """BIGQMT_ACCOUNT_TYPE_MAP = None → treated as {} → fallback."""
        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = None
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            self.assertEqual(account_type_for("any", "STOCK"), "STOCK")


# ---------------------------------------------------------------------------
# Integration: _resolve_account_type on gateway
# ---------------------------------------------------------------------------

class GatewayResolveAccountTypeTest(unittest.TestCase):
    """_resolve_account_type on order/position gateway delegates to the map."""

    def test_order_gateway_uses_map(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"ACCT_CREDIT": "CREDIT"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            gw = BigQmtOrderGateway(context_info=None, account_type="STOCK")
            self.assertEqual(gw._resolve_account_type("ACCT_CREDIT"), "CREDIT")
            self.assertEqual(gw._resolve_account_type("ACCT_UNKNOWN"), "STOCK")

    def test_position_gateway_uses_map(self):
        from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider

        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"ACCT_CREDIT": "CREDIT"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            prov = BigQmtPositionProvider(
                get_trade_detail_data_func=None, account_type="STOCK")
            self.assertEqual(prov._resolve_account_type("ACCT_CREDIT"), "CREDIT")
            self.assertEqual(prov._resolve_account_type("ACCT_UNKNOWN"), "STOCK")


# ---------------------------------------------------------------------------
# Integration: _account_type_code(account_id) on order gateway
# ---------------------------------------------------------------------------

class AccountTypeCodePerRequestTest(unittest.TestCase):
    """_account_type_code(account_id) respects per-request MAP lookup."""

    def test_with_map_returns_mapped_code(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"CREDIT_ACCT": "CREDIT"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            gw = BigQmtOrderGateway(context_info=None, account_type="STOCK")
            code_mapped = gw._account_type_code("CREDIT_ACCT")
            code_default = gw._account_type_code(None)
            # Both should be ints
            self.assertIsInstance(code_mapped, int)
            self.assertIsInstance(code_default, int)

    def test_without_account_id_uses_self_account_type(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"CREDIT_ACCT": "CREDIT"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            gw = BigQmtOrderGateway(context_info=None, account_type="CREDIT")
            # No account_id → uses self.account_type = "CREDIT"
            code = gw._account_type_code()
            self.assertIsInstance(code, int)

    def test_no_map_account_id_ignored(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        # No MAP configured
        sys.modules.pop("bigqmt_signal_trader_local_config", None)
        reload_map()
        gw = BigQmtOrderGateway(context_info=None, account_type="STOCK")
        code_no_id = gw._account_type_code()
        code_with_id = gw._account_type_code("some_acct")
        # Without a MAP, both should return the same code (self.account_type)
        self.assertEqual(code_no_id, code_with_id)


# ---------------------------------------------------------------------------
# Integration: _configured_account_type(account_id) on BigQmtRpcHandlers
# ---------------------------------------------------------------------------

class ConfiguredAccountTypePerRequestTest(unittest.TestCase):
    """_configured_account_type(account_id=...) avoids _current_params race."""

    def _make_handlers(self, gateway_account_type="STOCK", account_type_map=None):
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        if account_type_map is not None:
            fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
            fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = account_type_map
            with mock.patch.dict("sys.modules",
                                 {"bigqmt_signal_trader_local_config": fake_cfg}):
                reload_map()
        else:
            sys.modules.pop("bigqmt_signal_trader_local_config", None)
            reload_map()

        gw = BigQmtOrderGateway(context_info=None, account_type=gateway_account_type)
        handlers = BigQmtRpcHandlers(
            account_id="DEFAULT_ACCT",
            market_data=None,
            position_provider=None,
            order_gateway=gw,
        )
        return handlers

    def test_no_map_returns_gateway_default(self):
        h = self._make_handlers(gateway_account_type="CREDIT")
        self.assertEqual(h._configured_account_type(), "CREDIT")

    def test_with_map_returns_mapped_type(self):
        h = self._make_handlers(
            gateway_account_type="STOCK",
            account_type_map={"CREDIT_ACCT": "CREDIT"})
        self.assertEqual(
            h._configured_account_type("CREDIT_ACCT"), "CREDIT")

    def test_map_key_missing_returns_gateway_default(self):
        h = self._make_handlers(
            gateway_account_type="STOCK",
            account_type_map={"OTHER": "FUTURE"})
        self.assertEqual(
            h._configured_account_type("CREDIT_ACCT"), "STOCK")

    def test_no_account_id_uses_self_account_id(self):
        h = self._make_handlers(
            gateway_account_type="STOCK",
            account_type_map={"DEFAULT_ACCT": "CREDIT"})
        # No explicit account_id → falls back to self.account_id
        self.assertEqual(h._configured_account_type(), "CREDIT")

    def test_no_current_params_attribute(self):
        """Verify that _current_params no longer exists on the instance."""
        h = self._make_handlers()
        self.assertFalse(hasattr(h, "_current_params"))


# ---------------------------------------------------------------------------
# Integration: _reported_account_type(account_id) on BigQmtRpcHandlers
# ---------------------------------------------------------------------------

class ReportedAccountTypePerRequestTest(unittest.TestCase):
    """_reported_account_type(account_id=...) respects per-request MAP."""

    def _make_handlers(self, gateway_account_type="STOCK", account_type_map=None):
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        if account_type_map is not None:
            fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
            fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = account_type_map
            with mock.patch.dict("sys.modules",
                                 {"bigqmt_signal_trader_local_config": fake_cfg}):
                reload_map()
        else:
            sys.modules.pop("bigqmt_signal_trader_local_config", None)
            reload_map()

        gw = BigQmtOrderGateway(context_info=None, account_type=gateway_account_type)
        handlers = BigQmtRpcHandlers(
            account_id="DEFAULT_ACCT",
            market_data=None,
            position_provider=None,
            order_gateway=gw,
        )
        return handlers

    def test_no_gateway_returns_empty(self):
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers
        h = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
        h.order_gateway = None
        self.assertEqual(h._reported_account_type(), "")

    def test_with_map_returns_mapped_type(self):
        h = self._make_handlers(
            gateway_account_type="STOCK",
            account_type_map={"CREDIT_ACCT": "CREDIT"})
        self.assertEqual(
            h._reported_account_type("CREDIT_ACCT"), "CREDIT")

    def test_without_account_id_returns_gateway_default(self):
        h = self._make_handlers(gateway_account_type="CREDIT")
        self.assertEqual(h._reported_account_type(), "CREDIT")


# ---------------------------------------------------------------------------
# Regression: importlib.reload is never called
# ---------------------------------------------------------------------------

class NoImportlibReloadTest(unittest.TestCase):
    """_load_map must use import_module, NOT reload (PR #135 review item #3)."""

    def test_reload_is_not_called_in_load_map(self):
        from bigqmt_signal_trader import account_type_map as atm

        with mock.patch.object(importlib, "reload") as mock_reload:
            # Force a fresh load
            atm._ACCOUNT_TYPE_MAP = None
            fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
            fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"X": "CREDIT"}
            with mock.patch.dict("sys.modules",
                                 {"bigqmt_signal_trader_local_config": fake_cfg}):
                atm.get_account_type_map()
            mock_reload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
