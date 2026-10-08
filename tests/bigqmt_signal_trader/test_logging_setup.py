import builtins
from pathlib import Path


def test_logging_fallback_writes_when_broker_python_omits_logging(monkeypatch, tmp_path):
    source_path = Path(__file__).parents[2] / "src" / "bigqmt_signal_trader" / "logging_setup.py"
    original_import = builtins.__import__

    def import_without_logging(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "logging" or name.startswith("logging."):
            raise ImportError("broker python omits logging")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BIGQMT_LOG_TO_STDOUT", "0")
    namespace = {
        "__name__": "bigqmt_logging_fallback_test",
        "__builtins__": dict(vars(builtins), __import__=import_without_logging),
    }
    exec(compile(source_path.read_bytes(), str(source_path), "exec"), namespace)

    logger = namespace["get_logger"]("rpc")
    logger.error("startup failed: %s", "missing module")

    log_path = Path(namespace["log_file_path"]())
    assert "[bigqmt.rpc] startup failed: missing module" in log_path.read_text()
