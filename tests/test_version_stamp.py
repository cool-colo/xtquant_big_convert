"""The package's version stamp must match the release it ships in.

It said 0.2.0 for fifteen releases. That made it useless for the one job a
version stamp has here: telling a deployed QMT tree apart from the package it
was supposed to come from. Deploying into QMT is a file copy, and QMT keeps
modules in sys.modules across strategy re-runs, so "the copy never happened"
and "the copy happened but was not picked up" look identical from outside --
which is exactly what a version line resolves.
"""

import io
import os
import re
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader
from bigqmt_signal_trader import version as version_module


def _pyproject_version():
    with io.open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        match = re.search(r'^version = "([^"]+)"', handle.read(), re.M)
    assert match, "no version in pyproject.toml"
    return match.group(1)


class VersionStampTest(unittest.TestCase):
    def test_it_matches_pyproject(self):
        self.assertEqual(bigqmt_signal_trader.__version__, _pyproject_version())

    def test_it_looks_like_a_release(self):
        self.assertRegex(bigqmt_signal_trader.__version__, r"^\d+\.\d+\.\d+")

    def test_the_changelog_has_an_entry_for_it(self):
        """A stamp matching pyproject but with no changelog entry means the bump
        was made without writing down what changed."""
        with io.open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as handle:
            changelog = handle.read()

        self.assertIn("## [%s]" % bigqmt_signal_trader.__version__, changelog)


class DeploymentReportTest(unittest.TestCase):
    def test_it_reports_the_version_and_where_it_loaded_from(self):
        version, directory = bigqmt_signal_trader.deployment_report()

        self.assertEqual(version, bigqmt_signal_trader.__version__)
        self.assertTrue(directory.endswith("bigqmt_signal_trader"), directory)

    def test_a_caller_can_name_the_directory(self):
        _version, directory = bigqmt_signal_trader.deployment_report(
            package_dir="D:/somewhere/bigqmt_signal_trader")

        self.assertEqual(directory, "D:/somewhere/bigqmt_signal_trader")

    def test_it_never_raises(self):
        """This runs during strategy startup; an exception here would take the
        whole bridge down for the sake of a log line."""
        import os.path

        saved = os.path.abspath
        try:
            def boom(_path):
                raise OSError("no filesystem today")

            os.path.abspath = boom
            version, directory = bigqmt_signal_trader.deployment_report()
        finally:
            os.path.abspath = saved

        self.assertEqual(version, bigqmt_signal_trader.__version__)
        self.assertEqual(directory, "?")


class StartupReportingTest(unittest.TestCase):
    """The runtime has to actually print it, or none of the above helps."""

    def _runtime_source(self):
        path = os.path.join(ROOT, "src", "bigqmt_signal_trader_redis_rpc_runtime.py")
        with io.open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_runtime_reports_at_import(self):
        source = self._runtime_source()

        self.assertIn("def _report_deployment():", source)
        self.assertIn("\n_report_deployment()", source)

    def test_the_report_is_wrapped(self):
        """A missing or broken package must not stop the strategy starting."""
        source = self._runtime_source()
        start = source.index("def _report_deployment():")
        end = source.index("\ndef ", start + 1)

        self.assertIn("except Exception", source[start:end])


if __name__ == "__main__":
    unittest.main()


class SandboxVisibilityTest(unittest.TestCase):
    """The stamp has to exist inside QMT, which is the only place it matters.

    The QMT sandbox loader never executes the root package -- it returns an
    empty module, because the eager exports in __init__.py trip QMT's import
    allowlist. Anything defined there is invisible there. Found the hard way:
    the first version of this shipped in __init__.py and get_deployment_info
    came back with "module 'bigqmt_signal_trader' has no attribute
    'deployment_report'" from the live terminal.
    """

    def test_the_stamp_is_defined_in_a_submodule(self):
        self.assertEqual(version_module.__version__,
                         bigqmt_signal_trader.__version__)
        self.assertTrue(callable(version_module.deployment_report))

    def test_the_init_only_re_exports_it(self):
        path = os.path.join(ROOT, "src", "bigqmt_signal_trader", "__init__.py")
        with io.open(path, encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("from .version import", source)
        self.assertNotIn('__version__ = "', source)
        self.assertNotIn("def deployment_report", source)

    def test_callers_import_the_submodule_not_the_package(self):
        """Importing it off the root package works in an ordinary install and
        fails inside QMT -- the environment nobody tests by default."""
        for relative in ("bigqmt_signal_trader_redis_rpc_runtime.py",
                         "bigqmt_signal_trader/redis_rpc.py"):
            path = os.path.join(ROOT, "src", relative)
            with io.open(path, encoding="utf-8") as handle:
                source = handle.read()

            self.assertIn("from bigqmt_signal_trader.version import", source,
                          relative)
