"""Copy-on-write timestamps carried inside raw quote ticks, never exec events."""

import time


def unix_time_ns():
    """UNIX nanoseconds on both QMT Python 3.6.8 and newer client runtimes.

    Python 3.6 has no time_ns. Its float wall clock retains the same epoch/unit
    after conversion, with the precision of time.time(), not true ns precision.
    """
    try:
        return time.time_ns()
    except AttributeError:
        return int(time.time() * 1000000000)


def stamp_quote_batch(data, stage, timestamp_ns, start=False):
    """Stamp dict ticks, leaving legacy/untraced payloads and the input intact.

    A native QMT callback starts a new trace. Other stages only extend an
    existing trace, so snapshots and old publishers do not acquire fake stages.
    One timestamp belongs to the entire frame, even when it holds many symbols.
    """
    if not isinstance(data, dict):
        return data
    result = None
    for code, tick in data.items():
        if not isinstance(tick, dict) or not tick:
            continue
        trace = tick.get("_trace")
        if not start and not isinstance(trace, dict):
            continue
        if result is None:
            result = dict(data)
        traced_tick = dict(tick)
        traced_tick["_trace"] = {} if start else dict(trace)
        traced_tick["_trace"][stage] = timestamp_ns
        result[code] = traced_tick
    return data if result is None else result
