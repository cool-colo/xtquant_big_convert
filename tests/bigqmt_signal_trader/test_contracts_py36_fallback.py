# coding: utf-8
"""contracts.py must import on a py3.6 embedded Python with no
typing_extensions (PR #159, @karlthas007).

QMT terminals ship python36.dll; typing.Protocol is 3.8+. The module falls
back to a plain stand-in there -- Protocol is only used as a structural-
typing base class in this file, and plain subclassing is all it needs to
support. This test simulates that environment by blocking both imports and
reloading the module.
"""

import builtins
import importlib
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))


class ContractsPy36FallbackTest(unittest.TestCase):
    def test_imports_and_subclasses_without_typing_protocol(self):
        import bigqmt_signal_trader.contracts as contracts

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            # py3.6: typing exists (Dict/List are fine), only Protocol is
            # absent (3.8+); typing_extensions is not installed at all.
            fromlist = args[2] if len(args) > 2 else ()
            if name == "typing" and fromlist and "Protocol" in fromlist:
                raise ImportError("cannot import name 'Protocol'")
            if name.startswith("typing_extensions"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", side_effect=blocked):
            reloaded = importlib.reload(contracts)
        try:
            self.assertNotEqual(reloaded.Protocol.__module__, "typing")

            class MyGateway(reloaded.Protocol):
                pass

            self.assertTrue(issubclass(MyGateway, reloaded.Protocol))
            # The structural contracts still defined fine on the stand-in.
            self.assertTrue(hasattr(reloaded, "OrderGateway"))
        finally:
            # Restore the real module for every later test in this process.
            importlib.reload(contracts)


if __name__ == "__main__":
    unittest.main()
