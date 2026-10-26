"""Redis-free attribution must survive a new handler and a new process."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from bigqmt_signal_trader.models import OrderSnapshot
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


ACCOUNT = "issue393-test"
REMARK = "XXX_T4_D1@600036@20261015#0756"
NAME = "XXX_T4_D1"


class Gateway(DryRunOrderGateway):
    account_type = "STOCK"

    def query_orders(self, account_id, strategy_name):
        return [OrderSnapshot(
            order_sys_id="native-order", user_order_id=REMARK,
            stock_code="600036.SH", action="BUY", volume=100,
            traded_volume=0, status="50", strategy_name="QMT_BRIDGE")]


def handlers(directory):
    instance = BigQmtRpcHandlers(
        ACCOUNT, SimpleNamespace(), SimpleNamespace(), order_gateway=Gateway(),
        allow_order_methods=True)
    instance.enable_order_identity_persistence(
        directory=str(directory), account_types={ACCOUNT: "STOCK"})
    return instance


def test_pipe_without_redis_recovers_name_and_preserves_remark_after_restart(tmp_path):
    before = handlers(tmp_path)
    assert before._identity_redis() is None
    before._handle_submit_order({
        "stock_code": "600036.SH", "action": "BUY", "volume": 100,
        "price": 10, "remark": REMARK, "strategy_name": NAME,
        "wait_settlement": False})
    after = handlers(tmp_path)
    row = after._handle_query_orders({})[0]
    assert row.strategy_name == NAME
    assert row.user_order_id == REMARK
    probe = after._handle_probe_order_identity({"remark": REMARK})
    assert probe["local_persistence_enabled"] is True
    assert probe["local_hit"] is True
    assert probe["local_persistence_error"] == ""
    assert probe["identity_redis_wired"] is False


def test_a_new_python_process_restores_the_saved_name(tmp_path):
    before = handlers(tmp_path)
    before._remember_order_identity_local(ACCOUNT, REMARK, NAME)
    script = """
import json
import sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers
account, remark, directory = sys.argv[2:]
h = BigQmtRpcHandlers(account, None, None, order_gateway=SimpleNamespace(account_type='STOCK'))
h.enable_order_identity_persistence(directory, {account: 'STOCK'})
row = SimpleNamespace(user_order_id=remark, strategy_name='QMT_BRIDGE')
print(json.dumps({'name': h._attribute_to_strategies(account, [row])[0].strategy_name,
                  'remark': row.user_order_id}))
"""
    env = dict(os.environ, BIGQMT_LOG_ENABLED="0", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(ROOT / "src"),
         ACCOUNT, REMARK, str(tmp_path)],
        env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"name": NAME, "remark": REMARK}


def test_first_named_ownership_is_loaded_before_a_reused_remark_is_submitted(tmp_path):
    handlers(tmp_path)._remember_order_identity_local(ACCOUNT, REMARK, NAME)
    restarted = handlers(tmp_path)
    restarted._remember_order_identity_local(ACCOUNT, REMARK, "other-strategy")
    assert restarted._handle_query_orders({})[0].strategy_name == NAME
    assert handlers(tmp_path)._handle_query_orders({})[0].strategy_name == NAME


def test_secondary_account_is_persisted_and_loaded_in_its_own_namespace(tmp_path):
    before = handlers(tmp_path)
    before._remember_order_identity_local("secondary-account", REMARK, "secondary-name")
    after = handlers(tmp_path)
    rows = [SimpleNamespace(user_order_id=REMARK, strategy_name="QMT_BRIDGE")]
    assert after._attribute_to_strategies("secondary-account", rows)[0].strategy_name == "secondary-name"
    primary_rows = [SimpleNamespace(user_order_id=REMARK, strategy_name="QMT_BRIDGE")]
    assert after._attribute_to_strategies(ACCOUNT, primary_rows)[0].strategy_name == "QMT_BRIDGE"


def test_disk_failure_is_visible_without_changing_the_submit_result(tmp_path):
    blocked_directory = tmp_path / "not-a-directory"
    blocked_directory.write_text("occupied", encoding="utf-8")
    instance = handlers(blocked_directory)
    result = instance._handle_submit_order({
        "stock_code": "600036.SH", "action": "BUY", "volume": 100,
        "price": 10, "remark": REMARK, "strategy_name": NAME,
        "wait_settlement": False})
    assert result.status == "DRY_RUN"
    assert instance.order_gateway.submitted[-1].remark == REMARK
    probe = instance._handle_probe_order_identity({"remark": REMARK})
    assert probe["local_persistence_error"]
    assert probe["local_hit"] is True


def test_real_account_type_lists_share_the_scalar_type_namespace(tmp_path, monkeypatch):
    from bigqmt_signal_trader import account_type_map

    monkeypatch.setattr(account_type_map, "get_account_type_map",
                        lambda: {ACCOUNT: ["STOCK", "HUGANGTONG"]})
    before = BigQmtRpcHandlers(ACCOUNT, None, None, order_gateway=Gateway())
    before.enable_order_identity_persistence(directory=str(tmp_path))
    before._remember_order_identity_local(ACCOUNT, REMARK, NAME)
    assert handlers(tmp_path)._handle_query_orders({})[0].strategy_name == NAME


def test_hot_reload_keeps_newly_recovered_entries_from_another_process(tmp_path, monkeypatch):
    import bigqmt_signal_trader_strategy as strategy

    old = handlers(tmp_path)
    old._remember_order_identity_local(ACCOUNT, "first-remark", "first-name")
    handlers(tmp_path)._remember_order_identity_local(ACCOUNT, REMARK, NAME)
    restarted = handlers(tmp_path)
    monkeypatch.setattr(strategy, "_rpc_service", SimpleNamespace(handlers=old))
    monkeypatch.setattr(strategy, "_reload_request", {"pending": True, "by": "test"})
    monkeypatch.setattr(strategy, "_reload_result", {})
    monkeypatch.setattr(strategy, "_wait_for_responses_to_flush", lambda: True)
    monkeypatch.setattr(strategy, "_purge_package_modules", lambda: [])
    monkeypatch.setattr(strategy, "_rebind_module_level_imports", lambda: None)
    monkeypatch.setattr(strategy, "reset_app", lambda: None)
    monkeypatch.setattr(strategy, "init", lambda ctx: setattr(
        strategy, "_rpc_service", SimpleNamespace(handlers=restarted)))
    assert strategy._perform_reload(None)["ok"] is True
    assert restarted._handle_query_orders({})[0].strategy_name == NAME
    assert restarted._order_identity_local[(ACCOUNT, "first-remark")][1] == "first-name"
