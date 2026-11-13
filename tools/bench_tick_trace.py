"""Measure local trace CPU/encoding overhead with the real BigQMT parsers.

Run with the Nautilus virtualenv Python and --nautilus-repo pointing to its
checkout. Its HEAD data.py is the baseline (use a revision before tracing).
This deliberately excludes pipe/Redis I/O, event-loop waiting and strategy work.
"""

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
import types
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nautilus-repo", required=True, type=Path)
    parser.add_argument("--baseline-ref", default="HEAD")
    parser.add_argument("--samples", default=1000, type=int)
    args = parser.parse_args()
    if args.samples <= 0:
        parser.error("--samples must be positive")
    sys.path.insert(0, str(args.nautilus_repo))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

    from bigqmt_signal_trader.quote_push_channel import decode_push_payload
    from bigqmt_signal_trader.quote_push_channel import encode_push_payload
    from bigqmt_signal_trader.tick_trace import stamp_quote_batch
    from nautilus_trader.adapters.bigqmt.common import bigqmt_symbol_to_instrument_id
    from nautilus_trader.adapters.bigqmt.data import BigQMTDataClient
    from nautilus_trader.common.component import LiveClock
    from nautilus_trader.common.component import MessageBus
    from nautilus_trader.model.identifiers import TraderId

    source = subprocess.check_output([
        "git", "-C", str(args.nautilus_repo), "show",
        args.baseline_ref + ":nautilus_trader/adapters/bigqmt/data.py",
    ], text=True)
    baseline = types.ModuleType("nautilus_trader.adapters.bigqmt._trace_benchmark_baseline")
    exec(compile(source, "baseline_data.py", "exec"), baseline.__dict__)
    results = []
    for size in (1, 100):
        codes = ["%06d.SZ" % index for index in range(1, size + 1)]
        payload = {code: {
            "time": 1700000000000, "lastPrice": 10.0,
            "bidPrice": [10.0, 9.99, 9.98, 9.97, 9.96],
            "askPrice": [10.01, 10.02, 10.03, 10.04, 10.05],
            "bidVol": [200, 300, 400, 500, 600],
            "askVol": [150, 250, 350, 450, 550],
        } for code in codes}
        ids = {bigqmt_symbol_to_instrument_id(code) for code in codes}
        for mode in ("baseline", "timestamps_only", "timestamps_and_events"):
            adapter = baseline.BigQMTDataClient if mode == "baseline" else BigQMTDataClient
            methods = {name: getattr(adapter, name) for name in (
                "_on_quote_push", "_handle_quote_push", "_handle_full_tick_payload",
                "_publish_tick_traces", "_publish_tick_trace",
            ) if hasattr(adapter, name)}
            client = type("BenchmarkClient", (), methods)()
            client._config = types.SimpleNamespace(tick_trace_enabled=mode == "timestamps_and_events")
            client._clock = LiveClock()
            client._msgbus = MessageBus(TraderId("BENCH-001"), client._clock)
            client._msgbus.subscribe("bigqmt.tick_trace.*", lambda event: None)
            client._loop = types.SimpleNamespace(
                time=time.monotonic, call_soon_threadsafe=lambda callback, data: callback(data),
            )
            client._push_quote_ids = ids
            client._push_depth_ids = ids
            client._push_fallback_active = False
            client._handle_data = lambda data: None
            traced = mode != "baseline"

            def process():
                data = payload
                if traced:
                    data = stamp_quote_batch(data, "qmt_callback_ns", time.time_ns(), start=True)
                    data = stamp_quote_batch(data, "pipe_publish_ns", time.time_ns())
                pipe_bytes = json.dumps({"topic": "SZ", "data": data}).encode()
                received_ns = time.time_ns() if traced else None
                data = json.loads(pipe_bytes)["data"]
                if traced:
                    data = stamp_quote_batch(data, "proxy_pipe_received_ns", received_ns)
                    data = stamp_quote_batch(data, "redis_publish_ns", time.time_ns())
                redis_bytes = encode_push_payload({"combo_key": "SZ", "data": data}, encoding="json")
                received_ns = time.time_ns() if traced else None
                data = decode_push_payload(redis_bytes)["data"]
                if traced:
                    data = stamp_quote_batch(data, "adapter_redis_received_ns", received_ns)
                client._on_quote_push(data)
                return len(pipe_bytes), len(redis_bytes)

            for _ in range(100):
                process()
            samples = []
            for _ in range(args.samples):
                started = time.perf_counter_ns()
                pipe_size, redis_size = process()
                samples.append((time.perf_counter_ns() - started) / 1000)
            results.append({
                "mode": mode, "symbols_per_batch": size,
                "mean_batch_us": round(statistics.mean(samples), 3),
                "p95_batch_us": round(sorted(samples)[math.ceil(0.95 * len(samples)) - 1], 3),
                "symbols_per_second": round(size * 1e6 / statistics.mean(samples)),
                "pipe_bytes": pipe_size, "redis_bytes": redis_size,
            })
    print(json.dumps({
        "scope": "local CPU only; no transport I/O, scheduling or trading handlers",
        "baseline_ref": args.baseline_ref, "samples": args.samples, "results": results,
    }, indent=2))


if __name__ == "__main__":
    main()
