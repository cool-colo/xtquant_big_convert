# coding: utf-8
"""Log handlers must not accumulate across restarts and reloads (#139).

Observed on the live terminal after a day of restarts: one log line written to
bigqmt.log SIXTEEN times, and the same `ping breakdown` line in QMT's panel 373
times for a single strategy instance.

The cause is a mismatch in what survives a purge. _setup() guarded itself with
a module-level `_initialized`, but the entry drops every bigqmt_signal_trader
module from sys.modules on each start (_clear_local_modules; reload_deployment
does the same), so that flag is False again on the next import. Meanwhile
logging.getLogger("bigqmt") lives in the logging module's own registry, which
is never purged -- so the logger kept every handler the previous run added.

    module state resets  +  logger state does not  =  N handlers after N starts

Three consequences, in increasing order of how long they take to notice:

  1. every line written N times -- and this is the log you read when something
     else goes wrong. A 346-second stall once showed nothing but adjust
     heartbeats; drowning that log is expensive.
  2. N open handles on one file, so TimedRotatingFileHandler cannot rename it:
     PermissionError WinError 32, raised on every single write (68 times in one
     instance).
  3. rotation therefore never succeeds, so backupCount pruning never runs and
     BIGQMT_LOG_RETENTION_DAYS is inert. The live logs directory held exactly
     one bigqmt.log and no dated backups at all.
"""

import io
import logging
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import logging_setup


class _Counting(logging.Handler):
    """Records whether it was closed, so the test can see handle release."""

    def __init__(self):
        logging.Handler.__init__(self)
        self.closed_count = 0

    def emit(self, record):
        pass

    def close(self):
        self.closed_count += 1
        logging.Handler.close(self)


class _Angry(logging.Handler):
    def emit(self, record):
        pass

    def close(self):
        raise RuntimeError("this handler refuses to close")


class DetachTest(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("bigqmt_test_detach")
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)

    def tearDown(self):
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)

    def test_it_removes_what_a_previous_run_left(self):
        self.logger.addHandler(_Counting())
        self.logger.addHandler(_Counting())

        logging_setup._detach_existing_handlers(self.logger)

        self.assertEqual(self.logger.handlers, [])

    def test_it_closes_them_too(self):
        """removeHandler alone leaves the file handle open, and rotation then
        fails forever with WinError 32."""
        handler = _Counting()
        self.logger.addHandler(handler)

        logging_setup._detach_existing_handlers(self.logger)

        self.assertEqual(handler.closed_count, 1)

    def test_a_handler_that_will_not_close_does_not_stop_the_rest(self):
        angry, good = _Angry(), _Counting()
        self.logger.addHandler(angry)
        self.logger.addHandler(good)

        logging_setup._detach_existing_handlers(self.logger)

        self.assertEqual(self.logger.handlers, [])
        self.assertEqual(good.closed_count, 1)

    def test_an_empty_logger_is_fine(self):
        logging_setup._detach_existing_handlers(self.logger)

        self.assertEqual(self.logger.handlers, [])

    def test_something_without_handlers_at_all_is_fine(self):
        class Bare(object):
            pass

        logging_setup._detach_existing_handlers(Bare())


class NoAccumulationTest(unittest.TestCase):
    """The whole point: repeat _setup() the way a restart does."""

    def setUp(self):
        self.logger = logging.getLogger(logging_setup._LOGGER_NAME)
        self._saved = list(self.logger.handlers)
        self._initialized = logging_setup._initialized
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)

    def tearDown(self):
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
        for handler in self._saved:
            self.logger.addHandler(handler)
        logging_setup._initialized = self._initialized

    def _restart(self):
        """What a strategy start does: the module is fresh, the logger is not."""
        logging_setup._initialized = False
        logging_setup._setup()

    def test_one_start_installs_handlers(self):
        self._restart()

        self.assertGreater(len(self.logger.handlers), 0)

    def test_ten_restarts_do_not_multiply_them(self):
        self._restart()
        after_one = len(self.logger.handlers)

        for _ in range(9):
            self._restart()

        self.assertEqual(len(self.logger.handlers), after_one)

    def test_a_line_is_emitted_once_not_once_per_restart(self):
        """The symptom as the live terminal showed it: 16 copies of one line."""
        for _ in range(5):
            self._restart()

        counter = _Counting()
        emitted = []
        counter.emit = lambda record: emitted.append(record.getMessage())
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
        self.logger.addHandler(counter)

        logging_setup.get_logger("rpc").info("ping breakdown")

        self.assertEqual(emitted, ["ping breakdown"])

    def test_the_guard_is_not_the_module_flag_alone(self):
        """A module global cannot carry this: the entry purges the module on
        every start while the logger survives in logging's registry."""
        import inspect

        source = inspect.getsource(logging_setup._setup)

        self.assertIn("_detach_existing_handlers(logger)", source)


if __name__ == "__main__":
    unittest.main()
