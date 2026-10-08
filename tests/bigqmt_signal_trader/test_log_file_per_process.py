# coding: utf-8
"""Two bridges must not write the same log file (#144).

#139 fixed handlers piling up inside ONE process across restarts and reloads.
This is the other half, reported the next day: a user running two bridges at
once -- one live account, one simulated -- had both clients fall back to
~/.cache/bigqmt/logs/bigqmt.log, because _resolve_log_dir lands there for any
client not deployed inside a QMT directory.

Two OS handles on one file, from two processes. Windows then refuses the
rotation rename:

    PermissionError: [WinError 32] 另一个程序正在使用此文件，进程无法访问。:
      'bigqmt.log' -> 'bigqmt.log.2026-09-01'

raised on every write, and rotation never succeeding means backupCount pruning
never runs either -- the log grows without bound. No amount of in-process
handler bookkeeping reaches this: the other handle belongs to another process.

Two changes, in order of which one does the work:

  * the file name carries a per-process tag, so the two bridges stop sharing
  * the rotator tolerates a failed rename, so anyone who pins one name with
    BIGQMT_LOG_NAME gets a lost rotation rather than a traceback per write
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import logging_setup


class _Env(unittest.TestCase):
    KEYS = ("BIGQMT_LOG_NAME", "BIGQMT_ACCOUNT_ID")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FileNameTest(_Env):
    def test_the_account_id_scopes_the_file(self):
        """Stable across restarts, so a bridge keeps rolling the same file."""
        os.environ["BIGQMT_ACCOUNT_ID"] = "8886800503"

        self.assertEqual(logging_setup._log_file_name(), "bigqmt-8886800503.log")

    def test_two_accounts_get_two_files(self):
        """The whole point: the live bridge and the simulated one stop sharing."""
        os.environ["BIGQMT_ACCOUNT_ID"] = "8886800503"
        live = logging_setup._log_file_name()
        os.environ["BIGQMT_ACCOUNT_ID"] = "5556009168"
        sim = logging_setup._log_file_name()

        self.assertNotEqual(live, sim)

    def test_without_an_account_it_falls_back_to_the_pid(self):
        name = logging_setup._log_file_name()

        self.assertEqual(name, "bigqmt-pid%d.log" % os.getpid())

    def test_an_explicit_name_wins(self):
        """For anyone who wants the old single-file behaviour back."""
        os.environ["BIGQMT_LOG_NAME"] = "mybridge.log"
        os.environ["BIGQMT_ACCOUNT_ID"] = "8886800503"

        self.assertEqual(logging_setup._log_file_name(), "mybridge.log")

    def test_the_account_tag_is_stripped_to_alphanumerics(self):
        """It becomes a filename; separators and spaces have no business there."""
        os.environ["BIGQMT_ACCOUNT_ID"] = "888-680/05 03"

        name = logging_setup._log_file_name()

        self.assertEqual(name, "bigqmt-88868005 03.log".replace(" ", ""))

    def test_an_account_of_only_punctuation_falls_back_rather_than_emptying(self):
        os.environ["BIGQMT_ACCOUNT_ID"] = "///"

        self.assertEqual(logging_setup._log_file_name(),
                         "bigqmt-pid%d.log" % os.getpid())

    def test_it_is_still_a_dot_log_file(self):
        os.environ["BIGQMT_ACCOUNT_ID"] = "8886800503"

        self.assertTrue(logging_setup._log_file_name().endswith(".log"))


class TolerantRotatorTest(unittest.TestCase):
    """A failed rename must not become a traceback on every later write."""

    def setUp(self):
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="bigqmt-rotate-")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.dir, name)

    def test_it_renames_when_it_can(self):
        source, dest = self._path("a.log"), self._path("a.log.2026-09-01")
        with open(source, "w") as handle:
            handle.write("x")

        logging_setup._tolerant_rotator(source, dest)

        self.assertFalse(os.path.exists(source))
        self.assertTrue(os.path.exists(dest))

    def test_a_missing_source_is_not_an_error(self):
        logging_setup._tolerant_rotator(self._path("gone.log"),
                                        self._path("gone.log.1"))

    def test_a_failing_rename_is_swallowed(self):
        """os.replace onto a directory raises; the handler must not."""
        source = self._path("b.log")
        with open(source, "w") as handle:
            handle.write("x")
        blocked = self._path("blocked")
        os.makedirs(blocked)

        logging_setup._tolerant_rotator(source, blocked)

        self.assertTrue(os.path.exists(source), "source kept when rotation fails")

    def test_the_handler_actually_uses_it(self):
        import inspect

        source = inspect.getsource(logging_setup._setup)

        self.assertIn("file_handler.rotator = _tolerant_rotator", source)
        self.assertIn("_log_file_name()", source)


if __name__ == "__main__":
    unittest.main()
