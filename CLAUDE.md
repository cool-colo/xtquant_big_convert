# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`xtquant-big-convert` is an RPC bridge that exposes a **Big QMT** (迅投大 QMT full trading
terminal) instance's built-in Python API — market data, trading, positions — as a
**remotely callable service**, plus a client-side adapter layer that mimics **MiniQMT**
(`xtquant`) method names so existing MiniQMT code runs unchanged against a Big QMT backend
without needing XtQuantServer permissions.

Two processes, one wire:
- **Server side** runs *inside* Big QMT's embedded Python interpreter (loaded as a QMT
  strategy). It is the only place that touches QMT runtime APIs.
- **Client side** is an ordinary Python program that calls the server over a pluggable
  transport (redis / zmq / mysql / shm).

## Commands

```bash
# Install (editable, with the redis transport extra)
pip install -e ".[redis,dev]"       # extras: redis, zmq (base), mysql, msgpack, dev

# Run the full offline test suite (grouped report)
python run_all_tests.py             # all offline tests
python run_all_tests.py -v          # verbose
python run_all_tests.py --group signal_trader   # one group: signal_trader | backtest | live_api
python run_all_tests.py --live      # also run live RPC tests (needs a running QMT + redis)

# Plain pytest (equivalent targets)
pytest tests/bigqmt_signal_trader        # signal_trader group
pytest tests/bigqmt_backtest             # backtest group
pytest tests/bigqmt_signal_trader/test_redis_rpc.py -q          # single file
pytest tests/bigqmt_signal_trader/test_redis_rpc.py::test_name  # single test

# Live end-to-end API check against a real QMT (skipped by default)
python test_all_apis.py
```

Tests are offline by default; anything needing a live QMT/redis is behind `--live` /
`test_all_apis.py` and skips otherwise. There is no lint/format tooling configured.

## Architecture

### The wire protocol and its two ends

The RPC contract lives in `src/bigqmt_signal_trader/redis_rpc.py` (despite the name, it is
transport-agnostic at the method layer). Key facts:

- **Method whitelist**: `READ_METHODS` in `redis_rpc.py` is the authoritative set of
  server-callable read-only methods (~117 methods + MiniQMT aliases). Order methods
  (`submit_order`/`cancel_order`) are gated behind `rpc_allow_order_methods` and **off by
  default** — turning them on is a deliberate, risk-reviewed change.
- The server can process most requests directly in the transport listener thread
  (`rpc_process_in_listener`); an in-memory queue + `drain_pending` remains as a fallback
  for methods that must run from a QMT strategy callback thread.

### Pluggable transports (`src/bigqmt_signal_trader/transports/`)

`build_transport(name, config)` in `transports/factory.py` picks a backend by the
`rpc.transport` config value. `KNOWN_TRANSPORTS = ("redis", "zmq", "mysql", "shm")`. Optional
deps (`zmq`, mysql driver) are imported lazily so a missing dependency only errors when that
transport is actually selected. `redis` is the production default; `zmq` is same-machine
low-latency; `shm` is a reserved stub (unimplemented). Switching transports is a one-field
config change.

### Client compatibility layer (`src/bigqmt_signal_trader/xtquant_compat.py`)

Turns MiniQMT-style calls (`xt_trader.query_stock_positions`, `xtdata.get_full_tick`, …) into
RPC calls. `configure()` mutates the already-imported `xt_trader` / `xtdata` singletons in
place, so `from ... import xt_trader` followed by `configure()` works. Async callbacks
(`XtQuantTraderCallback`) are delivered via redis pub/sub.

Async order event ordering (Issue #51) is a load-bearing subtlety: `on_order_stock_async_response`
arrives on a different channel than `on_stock_order`/`on_stock_trade`, and events routinely
arrive *before* the response. The client uses an `order_remark`-keyed barrier (10s timeout) to
buffer/release events in order. This delay is added **only** on the `order_stock_async` path.

### The `xtquant` shim (`src/xtquant/`)

A drop-in replacement package (`xtquant.xtconstant` / `xttype` / `xtdata` / `xttrader`) so
legacy `from xtquant import ...` code hits the RPC bridge with zero edits — but **only** when
this `src/` is placed before the real `xtquant` on `PYTHONPATH`.

**Import-cycle hazard**: `xtquant/__init__.py` imports `xtconstant`/`xttype` eagerly but resolves
`xtdata`/`xttrader` lazily via PEP 562 `__getattr__`, because those two reach back into
`xtquant_compat`, which does `from xtquant.xtconstant import *`. Eager import would create a
partially-initialized-module failure whose direction depends on which package the caller imports
first. Keep the lazy resolution — do not move `xtdata`/`xttrader` to eager imports.

### Backtest package (`src/bigqmt_backtest/`)

Deliberately **isolated** from `bigqmt_signal_trader` — it never imports it, so the live bridge
and both backtest backends (QMT-native and standalone local broker) have separate module state,
identities, and order gateways. QMT-native mode never uses the local broker.

### App orchestration (`src/bigqmt_signal_trader/`)

`SignalTradingApp` (`app.py`) is the strategy loop, wired from config by
`adapter_factory.build_app`. It composes injected providers: `signal_source`, `market_data`,
`position_provider`, `order_gateway`, `position_sync_sink`, `state_store`. The default factory
builds safe empty/dry-run implementations so QMT can load and schedule the strategy without
emitting real orders. `runner.py` (`init_app`/`tick_app`) is the reusable forwarding entry the
QMT strategy file calls.

### Whole-quote push (`subscribe_whole_quote`)

Real server-push (not one-shot snapshots), three channels: control-plane RPC (reuses the
transport), a one-way PUB/SUB data plane (`QuotePushChannel`, msgpack + json fallback), and the
Big QMT source (`QuoteSubscriptionManager`) which normalizes+dedupes subscriptions by a composite
key and ref-counts them across clients. Clients heartbeat via `quote_keepalive`; the server
auto-recovers subscriptions after restart.

## Server-side deployment entry files

These live at `src/` top level (registered as `py-modules` in `pyproject.toml`) and are copied
into Big QMT's `python/` directory to run as strategies. They **must stay ASCII-only** — QMT's
strategy editor may re-save with a local code page, corrupting non-ASCII (`BIGQMT_REDIS_DRYRUN.py`
is intentionally GBK). QMT loads strategy scripts via `exec`, so `__file__` may be undefined;
entry files probe candidate dirs to fix `sys.path`, and `bigqmt_signal_trader_strategy.py`
force-reloads `adapter_factory` on re-run because QMT caches modules across strategy restarts.

- `bigqmt_signal_trader_redis_rpc_runtime.py` — the RPC service entry (read-only + position sync;
  no signal consumption; order methods off by default).
- `bigqmt_signal_trader_strategy.py` — full ThinkTrader strategy entry.
- `BIGQMT_REDIS_DRYRUN.py` — QMT editor entry (GBK-encoded).
- Private config `bigqmt_signal_trader_local_config.py` (account/redis creds) is created on the
  QMT box and **not committed**. See `*_config.example.py` templates in `src/`.
- `bigqmt_no_redis/` — a self-contained ZMQ-only variant for QMT sandboxes whose broker whitelist
  blocks `import redis`.

## qmt-trader skill (`qmt-trader/`)

A standalone Claude Code / Cursor skill (`SKILL.md`) wrapping a deterministic CLI
(`scripts/qmt.py`, ~46 subcommands) that drives the deployed bridge for market data, positions,
orders, etc. Outputs JSON by default (`--table` for human-readable). Depends on the bridge being
deployed and running.

## Docs

`docs/` holds design/verification notes worth consulting before changing the corresponding
subsystem — notably `RPC_API_REFERENCE.md` (method catalog + Big QMT capability boundaries),
`XTQUANT_COMPAT_REPLACEMENT.md`, `SUBSCRIBE_WHOLE_QUOTE_PUSH.md`, `RPC_TRANSPORTS.md`,
`EXEC_EVENT_DUPLICATE_CALLBACK_FIX.md`, and `ZMQ_BACKTEST_BRIDGE.md`. `CHANGELOG.md` tracks
per-version behavior changes.
