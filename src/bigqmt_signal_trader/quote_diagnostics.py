"""Bounded, rate-limited diagnostics for quote delivery boundaries."""

import threading
import time


class QuoteDiagnostics(object):
    def __init__(self, log=None, prefix="[bigqmt_quote]"):
        self.counts = {}
        self._last_report = {}
        self._lock = threading.Lock()
        self._log = log or print
        self._prefix = prefix

    def increment(self, reason, amount=1):
        with self._lock:
            self.counts[reason] = self.counts.get(reason, 0) + amount
            return self.counts[reason]

    def report(self, reason, subject="", error=None):
        now = time.monotonic()
        with self._lock:
            count = self.counts.get(reason, 0) + 1
            self.counts[reason] = count
            if count > 3 and now - self._last_report.get(reason, now) < 60.0:
                return
            self._last_report[reason] = now
        detail = " %s: %s" % (type(error).__name__, error) if error is not None else ""
        try:
            self._log("%s reason=%s subject=%s count=%d%s" %
                      (self._prefix, reason, subject, count, detail))
        except Exception:
            # Reporting must not interrupt quote delivery.
            pass
