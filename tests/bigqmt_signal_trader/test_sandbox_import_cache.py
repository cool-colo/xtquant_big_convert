"""A `from X import Y` in the sandbox must not re-walk sys.path every time.

The sandbox replaces __import__ with its own hook. For `from pkg.mod import
NAME` the hook tried to load `pkg.mod.NAME` as a submodule -- because a
fromlist entry sometimes is one. Usually it is an attribute, and the lookup
then walked _SOURCE_ROOT plus every sys.path entry calling os.path.isfile
before raising ModuleNotFoundError, which was caught and discarded.

Nothing remembered the answer, so it ran again on every single call.

Measured in the live terminal, on a ping whose handler contains exactly one
such import (`from bigqmt_signal_trader.version import __version__`):

    handle=1493ms  to_jsonable=0ms  gap=0ms  publish=199ms     before
    handle=   0ms  to_jsonable=0ms  gap=0ms  publish=204ms     after

Round trip went from ~1800ms to ~95-200ms -- on every RPC method, since the
cost was in the import hook rather than in any handler.
"""

import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

ENTRIES = (("BIGQMT_REDIS_DRYRUN.py", "utf-8"),
           ("BIGQMT_ZMQ_BACKTEST.py", "gbk"))


class _FakeSys(object):
    """Just enough sys for the sliced import machinery to run harmlessly."""

    def __init__(self):
        self.modules = {}
        self.path = []


def _source(entry, encoding):
    with io.open(os.path.join(SRC, entry), encoding=encoding) as handle:
        return handle.read()


def _import_hook(entry, encoding):
    """Exec the sandbox's import machinery out of a standalone entry script.

    These scripts bootstrap the package, so the hook is inlined in each and
    cannot be imported. Exec'ing the real text keeps the test honest about
    what actually ships.
    """
    source = _source(entry, encoding)
    start = source.index("_MISSING_LOCAL_MODULES = set()")
    # The two entries name their helpers differently and _local_import is the
    # last function in one of them, so slice to the next top-level def or EOF.
    after = source.index("def _local_import(", start)
    end = source.find("\ndef ", after + 1)
    if end == -1:
        end = len(source)

    calls = []

    def fake_load(name):
        calls.append(name)
        if name.endswith(".NOT_A_MODULE"):
            raise ModuleNotFoundError("local source not found: %s" % name, name=name)
        return object()

    namespace = {
        "_load_local_module": fake_load,
        "_resolve_name": lambda name, g, level: name,
        # Only the fake names are "local". Answering True for everything let
        # the entry's own sys.modules cleanup run over the real interpreter
        # and delete modules pytest was using.
        "_is_local_module": lambda name: str(name).startswith("pkg"),
        "_is_local": lambda name: str(name).startswith("pkg"),
        "_ORIGINAL_IMPORT": lambda *a, **k: None,
        "ModuleNotFoundError": ModuleNotFoundError,
        # A stand-in interpreter: the sliced code clears sys.modules, and it
        # must not reach the real one.
        "sys": _FakeSys(),
        "os": os,
    }
    exec(compile(source[start:end], entry, "exec"), namespace)
    return namespace["_local_import"], calls, namespace


class NegativeCacheTest(unittest.TestCase):
    def test_a_missing_child_is_looked_up_once(self):
        """Behaviour is exercised against the live-bridge entry.

        The backtest entry carries the same code but its surrounding module
        setup resists being exec'd in isolation; that copy is checked
        textually instead (see ShippedInBothEntriesTest).
        """
        entry, encoding = ENTRIES[0]
        hook, calls, _ns = _import_hook(entry, encoding)

        for _ in range(5):
            hook("pkg.mod", {}, {}, ("NOT_A_MODULE",), 0)

        misses = [name for name in calls if name.endswith(".NOT_A_MODULE")]
        self.assertEqual(len(misses), 1,
                         "%s re-walked sys.path %d times" % (entry, len(misses)))

    def test_the_miss_is_remembered_by_full_name(self):
        hook, _calls, namespace = _import_hook(*ENTRIES[0])

        hook("pkg.mod", {}, {}, ("NOT_A_MODULE",), 0)

        self.assertIn("pkg.mod.NOT_A_MODULE", namespace["_MISSING_LOCAL_MODULES"])

    def test_two_different_misses_are_tracked_separately(self):
        hook, calls, _ns = _import_hook(*ENTRIES[0])

        hook("pkg.a", {}, {}, ("NOT_A_MODULE",), 0)
        hook("pkg.b", {}, {}, ("NOT_A_MODULE",), 0)

        self.assertEqual(len([n for n in calls if n.endswith(".NOT_A_MODULE")]), 2)


class StillImportsTest(unittest.TestCase):
    """Caching a miss must not stop a real submodule from loading."""

    def test_a_real_submodule_is_still_loaded_every_time(self):
        """Repeat calls must keep resolving it -- sys.modules does the real
        caching, and short-circuiting here would break a reload."""
        hook, calls, _ns = _import_hook(*ENTRIES[0])

        for _ in range(3):
            hook("pkg.mod", {}, {}, ("real_submodule",), 0)

        self.assertEqual(len([n for n in calls if n.endswith(".real_submodule")]), 3)

    def test_the_parent_module_is_still_loaded(self):
        hook, calls, _ns = _import_hook(*ENTRIES[0])

        hook("pkg.mod", {}, {}, ("NOT_A_MODULE",), 0)

        self.assertIn("pkg.mod", calls)

    def test_a_star_import_is_untouched(self):
        hook, calls, _ns = _import_hook(*ENTRIES[0])

        hook("pkg.mod", {}, {}, ("*",), 0)

        self.assertEqual([n for n in calls if n.endswith(".*")], [])


class ShippedInBothEntriesTest(unittest.TestCase):
    def test_both_loaders_carry_the_cache(self):
        for entry, encoding in ENTRIES:
            self.assertIn("_MISSING_LOCAL_MODULES", _source(entry, encoding), entry)

    def test_the_backtest_entry_stays_isolated(self):
        """It must not mention the live bridge -- pinned by test_qmt_runtime."""
        self.assertNotIn("bigqmt_signal_trader",
                         _source("BIGQMT_ZMQ_BACKTEST.py", "gbk"))


if __name__ == "__main__":
    unittest.main()
