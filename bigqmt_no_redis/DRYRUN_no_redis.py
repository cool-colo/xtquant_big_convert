#coding:gbk
"""QMT bridge entry (no-redis version).

Same file-loader pattern as BIGQMT_REDIS_DRYRUN, but the RPC transport is ZMQ
only -- no redis imports anywhere. This version loads the no-redis zmq transport
(bigqmt_no_redis/zmq_transport.py) which inlines all encoding helpers and drops
redis-based service discovery, so it loads cleanly in QMT sandboxes that reject
`import redis` or any redis-named module.

Use this when your QMT environment cannot import the redis package (e.g. broker
whitelist blocks it) or when you want zero redis dependency.

Config: set "transport": "zmq" in bigqmt_signal_trader_local_config.py (the
no-redis runtime forces zmq regardless). Redis config fields are ignored.
"""
import builtins as _builtins
import importlib as _importlib
import os
import sys
import types


_LOCAL_ROOTS = (
    "bigqmt_signal_trader",
    "bigqmt_signal_trader_strategy",
    "bigqmt_signal_trader_redis_rpc_runtime",
    "bigqmt_signal_trader_local_config",
    "bigqmt_no_redis",
)
_ORIGINAL_IMPORT = _builtins.__import__
_ORIGINAL_IMPORT_MODULE = _importlib.import_module
_ORIGINAL_RELOAD = _importlib.reload


def _known_qmt_python_dir():
    # Find the QMT python dir from sys.path instead of a hardcoded path, so
    # the bridge loads regardless of broker install location or launch mode.
    for p in sys.path:
        if p and r"\python" in p and os.path.isdir(p):
            return p
    return ""


try:
    _SOURCE_ROOT = os.path.dirname(os.path.abspath(__file__))
except Exception:
    _SOURCE_ROOT = _known_qmt_python_dir()
if not _SOURCE_ROOT:
    _SOURCE_ROOT = _known_qmt_python_dir()


def _is_local_module(name):
    return any(name == root or name.startswith(root + ".") for root in _LOCAL_ROOTS)


def _resolve_name(name, module_globals, level):
    if not level:
        return name
    package = (module_globals or {}).get("__package__") or (module_globals or {}).get("__name__", "")
    if not package:
        raise ImportError("relative import without package")
    for unused in range(level - 1):
        if "." not in package:
            raise ImportError("relative import beyond top-level package")
        package = package.rsplit(".", 1)[0]
    return package + ("." + name if name else "")


def _find_local_source(name):
    relative = name.replace(".", os.sep)
    dirs = []
    if _SOURCE_ROOT:
        dirs.append(_SOURCE_ROOT)
    for p in sys.path:
        if p and os.path.isdir(p) and p not in dirs:
            dirs.append(p)
    for d in dirs:
        package_init = os.path.join(d, relative, "__init__.py")
        if os.path.isfile(package_init):
            return package_init, True
        module_file = os.path.join(d, relative + ".py")
        if os.path.isfile(module_file):
            return module_file, False
    raise ModuleNotFoundError("local source not found: %s" % name, name=name)


def _set_parent_attribute(name, module):
    if "." not in name:
        return
    parent_name, child_name = name.rsplit(".", 1)
    parent = _load_local_module(parent_name)
    setattr(parent, child_name, module)


def _load_local_module(name):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    source_path, is_package = _find_local_source(name)
    if "." in name:
        _load_local_module(name.rsplit(".", 1)[0])
    module = types.ModuleType(name)
    module.__file__ = source_path
    module.__package__ = name if is_package else name.rpartition(".")[0]
    if is_package:
        module.__path__ = [os.path.dirname(source_path)]
    module_builtins = dict(_builtins.__dict__)
    module_builtins["__import__"] = _local_import
    module.__dict__["__builtins__"] = module_builtins
    module.__dict__["__bigqmt_load_local_module"] = _load_local_module
    sys.modules[name] = module
    if name == "bigqmt_signal_trader":
        return module
    try:
        with open(source_path, "rb") as source_file:
            source = source_file.read()
        exec(compile(source, source_path, "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    _set_parent_attribute(name, module)
    return module


def _local_import(name, module_globals=None, module_locals=None, fromlist=(), level=0):
    absolute_name = _resolve_name(name, module_globals, level)
    if not _is_local_module(absolute_name):
        return _ORIGINAL_IMPORT(name, module_globals, module_locals, fromlist, level)
    module = _load_local_module(absolute_name)
    for child in fromlist or ():
        if child != "*":
            try:
                _load_local_module(absolute_name + "." + child)
            except ModuleNotFoundError:
                pass
    if fromlist:
        return module
    return _load_local_module(absolute_name.split(".", 1)[0])


def _local_import_module(name, package=None):
    if _is_local_module(name):
        return _load_local_module(name)
    return _ORIGINAL_IMPORT_MODULE(name, package)


def _local_reload(module):
    if _is_local_module(getattr(module, "__name__", "")):
        return _load_local_module(module.__name__)
    return _ORIGINAL_RELOAD(module)


def _clear_local_modules():
    for name in list(sys.modules):
        if _is_local_module(name):
            sys.modules.pop(name, None)


def _stop_previous_rpc_service():
    """Release the previous QMT strategy's socket before clearing its module."""
    previous = sys.modules.get("bigqmt_signal_trader_strategy")
    reset = getattr(previous, "reset_app", None)
    if not callable(reset):
        return
    try:
        reset()
        print("[bigqmt_shell] previous rpc service stopped")
    except Exception as exc:
        print("[bigqmt_shell] previous rpc service stop failed: %s" % exc)


_stop_previous_rpc_service()
_clear_local_modules()
_importlib.import_module = _local_import_module
_importlib.reload = _local_reload
print("[bigqmt_shell] importlib entry source_root=%s" % _SOURCE_ROOT)


def _fallback_account_id():
    for name in ("BIGQMT_ACCOUNT_ID", "account", "account_id", "accountID"):
        value = globals().get(name)
        if value:
            return str(value)
    return ""


try:
    _local_import("bigqmt_signal_trader.adapters.market_bigqmt", globals(), fromlist=("*",))
    _local_import("bigqmt_signal_trader.adapters.order_bigqmt", globals(), fromlist=("*",))
    _local_import("bigqmt_signal_trader.adapters.position_bigqmt", globals(), fromlist=("*",))
    _strategy = _local_import("bigqmt_signal_trader_strategy", globals(), fromlist=("*",))
    _strategy.reset_app()
except Exception as bridge_preload_error:
    print("[bigqmt_shell] bridge preload failed: %s" % bridge_preload_error)

_runtime = _local_import("bigqmt_signal_trader_redis_rpc_runtime", globals(), fromlist=("*",))


def _load_local_config():
    return _local_import("bigqmt_signal_trader_local_config", globals(), fromlist=("*",))


try:
    _config = _load_local_config()
    BIGQMT_REDIS_CONFIG = getattr(_config, "BIGQMT_REDIS_CONFIG", {})
    # Force zmq transport (this is the no-redis version).
    BIGQMT_REDIS_CONFIG = dict(BIGQMT_REDIS_CONFIG or {})
    BIGQMT_REDIS_CONFIG["transport"] = "zmq"
    BIGQMT_REDIS_CONFIG["rpc_background_threads"] = True
    print("[bigqmt_shell] no-redis mode: transport=zmq background_threads=True")
    _runtime.configure_runtime_redis(BIGQMT_REDIS_CONFIG)
except Exception as redis_config_error:
    print("[bigqmt_shell] local redis config load failed: %s" % redis_config_error)

try:
    _config = _load_local_config()
    BIGQMT_ACCOUNT_ID = getattr(_config, "BIGQMT_ACCOUNT_ID", "")
    print("[bigqmt_shell] local account config loaded=%s" % bool(BIGQMT_ACCOUNT_ID))
    _runtime.configure_runtime_account(BIGQMT_ACCOUNT_ID)
except Exception as account_config_error:
    print("[bigqmt_shell] local account config load failed: %s" % account_config_error)
    account_id = _fallback_account_id()
    if account_id:
        _runtime.configure_runtime_account(account_id)

try:
    qmt_extra = {}
    for function_name in (
        "get_history_trade_detail_data", "get_value_by_order_id", "get_last_order_id",
        "get_ipo_data", "get_new_purchase_limit", "get_assure_contract",
        "get_enable_short_contract", "get_unclosed_compacts", "get_closed_compacts",
        "get_debt_contract", "get_option_subject_position", "get_comb_option",
        "get_hkt_exchange_rate", "down_history_data",
    ):
        if function_name in globals():
            qmt_extra[function_name] = globals()[function_name]
    print("[bigqmt_shell] down_history_data bound=%s" % ("down_history_data" in qmt_extra))
    _runtime.bind_runtime_api(
        passorder_func=globals().get("passorder"),
        cancel_func=globals().get("cancel"),
        get_trade_detail_data_func=globals().get("get_trade_detail_data"),
        extra_funcs=qmt_extra or None,
    )
except NameError:
    pass


init = _runtime.init
handlebar = _runtime.handlebar
adjust = _runtime.adjust
order_callback = _runtime.order_callback
deal_callback = _runtime.deal_callback
