import collections
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import order_identity_journal as journal_module
from bigqmt_signal_trader.order_identity_journal import OrderIdentityJournal


NOW = 2000000.0


def make_store(tmp_path, account="acct", account_type="STOCK", **kwargs):
    return OrderIdentityJournal(account, account_type, directory=str(tmp_path),
                                clock=lambda: NOW, **kwargs)


def test_fresh_instance_recovers_durable_identity(tmp_path):
    first = make_store(tmp_path)
    assert first.remember("acct", "remark", NOW - 1, "alpha")
    second = make_store(tmp_path)
    assert first._segment != second._segment
    assert second.load() == collections.OrderedDict([
        (("acct", "remark"), (NOW - 1, "alpha")),
    ])
    assert not os.path.exists(first._segment + ".lock")


def test_multiple_writers_have_independent_segments_and_no_lost_entries(tmp_path):
    stores = [make_store(tmp_path) for _ in range(8)]

    def write(index):
        return stores[index].remember("acct", "order-%s" % index,
                                       NOW - index, "alpha")

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(write, range(8)))
    loaded = make_store(tmp_path).load()
    assert len(loaded) == 8
    assert len({store._segment for store in stores}) == 8
    assert all(("acct", "order-%s" % i) in loaded for i in range(8))


def test_separate_processes_recover_all_segments(tmp_path):
    code = "\n".join([
        "import importlib.util, sys",
        "spec = importlib.util.spec_from_file_location('journal_worker', sys.argv[1])",
        "module = importlib.util.module_from_spec(spec)",
        "spec.loader.exec_module(module)",
        "store = module.OrderIdentityJournal('acct', 'STOCK', directory=sys.argv[2])",
        "for number in range(10):",
        "    assert store.remember('acct', sys.argv[3] + '-' + str(number),",
        "                          2000000.0, 'alpha')",
    ])
    workers = [subprocess.Popen(
        [sys.executable, "-c", code, journal_module.__file__, str(tmp_path), str(i)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
    for worker in workers:
        out, err = worker.communicate(timeout=30)
        assert worker.returncode == 0, (out, err)
    loaded = make_store(tmp_path).load()
    assert len(loaded) == 40
    assert all(("acct", "%s-%s" % (i, j)) in loaded
               for i in range(4) for j in range(10))


def test_first_named_wins_in_timestamp_order_not_segment_order(tmp_path):
    later = make_store(tmp_path)
    earlier = make_store(tmp_path)
    assert later.remember("acct", "r", NOW - 2, "beta")
    assert earlier.remember("acct", "r", NOW - 5, "")
    assert earlier.remember("acct", "r", NOW - 4, "alpha")
    assert later.remember("acct", "r", NOW - 1, "")
    assert earlier.remember("acct", "r", NOW, "alpha")
    assert make_store(tmp_path).load()[("acct", "r")] == (NOW, "alpha")


def test_equal_timestamp_replay_is_deterministic(tmp_path):
    first, second = make_store(tmp_path), make_store(tmp_path)
    first.remember("acct", "r", NOW, "alpha")
    second.remember("acct", "r", NOW, "beta")
    expected = "alpha" if first._segment < second._segment else "beta"
    assert first.load()[("acct", "r")][1] == expected
    assert second.load() == first.load()


def test_ttl_and_limit_keep_latest_live_identities(tmp_path):
    store = make_store(tmp_path, ttl_seconds=10, limit=2)
    for remark, stamp in [("expired", NOW - 11), ("oldest", NOW - 10),
                           ("middle", NOW - 5), ("newest", NOW)]:
        assert store.remember("acct", remark, stamp, "alpha")
    assert list(store.load()) == [("acct", "middle"), ("acct", "newest")]


def test_ignores_corruption_partial_last_line_and_other_account(tmp_path):
    store = make_store(tmp_path)
    assert store.remember("acct", "good", NOW, "alpha")
    with open(store._segment, "ab") as handle:
        handle.write(b"broken json\n")
        handle.write(json.dumps({"account_id": "another", "account_type": "STOCK",
                                 "remark": "foreign", "ts": NOW,
                                 "strategy_name": "wrong"}).encode("utf-8") + b"\n")
        handle.write(b"\xff\n")
        handle.write(b'{"account_id":"acct","account_type":"STOCK",'
                     b'"remark":"partial","ts":2000000,"strategy_name":"wrong"}')
    assert list(store.load()) == [("acct", "good")]
    assert os.path.exists(store._segment)


def test_account_type_and_path_traversal_inputs_are_hashed(tmp_path):
    stock = make_store(tmp_path, account="../private/account", account_type="STOCK")
    credit = make_store(tmp_path, account="../private/account", account_type="CREDIT")
    assert stock.directory != credit.directory
    assert os.path.dirname(stock.directory) == str(tmp_path)
    assert len(os.path.basename(stock.directory)) == 64
    assert stock.remember("../private/account", "../outside", NOW, "alpha")
    assert not credit.load()
    assert stock.load()[("../private/account", "../outside")][1] == "alpha"


def test_env_directory_override_is_also_namespaced(tmp_path, monkeypatch):
    monkeypatch.setenv("BIGQMT_ORDER_IDENTITY_DIR", str(tmp_path))
    store = OrderIdentityJournal("acct", "STOCK", clock=lambda: NOW)
    assert os.path.dirname(store.directory) == str(tmp_path)


def test_default_path_requires_exact_python_component(tmp_path, monkeypatch):
    false_match = tmp_path / "python-not-a-deployment"
    deployment = tmp_path / "python"
    false_match.mkdir()
    deployment.mkdir()
    monkeypatch.delenv("BIGQMT_ORDER_IDENTITY_DIR", raising=False)
    monkeypatch.setattr(journal_module.sys, "path", [str(false_match), str(deployment)])
    store = OrderIdentityJournal("acct", "STOCK", clock=lambda: NOW)
    assert os.path.dirname(store.directory) == str(deployment / "state" / "order_identity")


def test_default_path_falls_back_to_user_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("BIGQMT_ORDER_IDENTITY_DIR", raising=False)
    monkeypatch.setattr(journal_module.sys, "path", [])
    monkeypatch.setattr(journal_module.os.path, "expanduser", lambda _: str(tmp_path))
    store = OrderIdentityJournal("acct", "STOCK", clock=lambda: NOW)
    assert os.path.dirname(store.directory) == str(tmp_path / ".cache" / "bigqmt" / "order_identity")


def test_only_fully_expired_segments_are_deleted(tmp_path):
    expired = make_store(tmp_path, ttl_seconds=10)
    active = make_store(tmp_path, ttl_seconds=10)
    expired.remember("acct", "old", NOW - 11, "alpha")
    active.remember("acct", "old-too", NOW - 11, "alpha")
    active.remember("acct", "live", NOW, "alpha")
    loaded = make_store(tmp_path, ttl_seconds=10).load()
    assert not os.path.exists(expired._segment)
    assert os.path.exists(active._segment)
    assert list(loaded) == [("acct", "live")]


def test_cleanup_skips_segment_while_writer_holds_lock(tmp_path):
    writer = make_store(tmp_path, ttl_seconds=10)
    writer.remember("acct", "old", NOW - 11, "alpha")
    lock = writer._acquire(writer._segment)
    try:
        assert not make_store(tmp_path, ttl_seconds=10).load()
        assert os.path.exists(writer._segment)
    finally:
        writer._release(lock)
    assert writer.remember("acct", "new", NOW, "alpha")
    assert make_store(tmp_path, ttl_seconds=10).load()[("acct", "new")][1] == "alpha"


def test_cleanup_rechecks_for_a_new_append_before_deleting(tmp_path, monkeypatch):
    writer = make_store(tmp_path, ttl_seconds=10)
    reader = make_store(tmp_path, ttl_seconds=10)
    writer.remember("acct", "old", NOW - 11, "alpha")
    original_acquire = reader._acquire

    def append_before_cleanup(path, wait=False):
        assert writer.remember("acct", "new", NOW, "alpha")
        return original_acquire(path, wait=wait)

    monkeypatch.setattr(reader, "_acquire", append_before_cleanup)
    reader.load()
    assert os.path.exists(writer._segment)
    assert reader.load()[("acct", "new")][1] == "alpha"


def test_fsync_failure_is_reported_without_raising_and_warning_is_throttled(
        tmp_path, monkeypatch, capsys):
    store = make_store(tmp_path)

    def fail(_):
        raise OSError("disk failure")

    monkeypatch.setattr(journal_module.os, "fsync", fail)
    assert not store.remember("acct", "r", NOW, "alpha")
    assert not store.remember("acct", "r2", NOW, "alpha")
    assert "disk failure" in store.error
    assert capsys.readouterr().out.count("restart recovery may be incomplete") == 1
    assert not os.path.exists(store._segment + ".lock")


def test_failed_partial_write_rolls_to_new_segment_and_does_not_hide_error(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    failed_segment = store._segment
    original_fsync = journal_module.os.fsync
    calls = []

    def truncate_first_write(fd):
        calls.append(fd)
        if len(calls) == 1:
            os.ftruncate(fd, 8)
            raise OSError("interrupted write")
        original_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", truncate_first_write)
    assert not store.remember("acct", "incomplete", NOW - 1, "alpha")
    assert store._segment != failed_segment
    assert store.remember("acct", "complete", NOW, "beta")
    assert "interrupted write" in store.error
    assert not os.path.exists(failed_segment + ".lock")
    assert list(make_store(tmp_path).load()) == [("acct", "complete")]


def test_append_success_preserves_startup_read_error(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.remember("acct", "before", NOW - 1, "alpha")
    original_listdir = journal_module.os.listdir

    def fail(_):
        raise OSError("startup directory unreadable")

    monkeypatch.setattr(journal_module.os, "listdir", fail)
    assert not store.load()
    monkeypatch.setattr(journal_module.os, "listdir", original_listdir)
    assert store.remember("acct", "after", NOW, "beta")
    assert "startup directory unreadable" in store.error


def test_append_flushes_before_fsync_and_closes_handle(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    original_fsync = journal_module.os.fsync
    checked = []

    def check(fd):
        with open(store._segment, "rb") as handle:
            checked.append(json.loads(handle.read().decode("utf-8"))["remark"])
        original_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", check)
    assert store.remember("acct", "durable", NOW, "alpha")
    assert checked == ["durable"]
    # No surviving Windows handle may block a rename or cleanup.
    os.rename(store._segment, store._segment + ".closed")


def test_rejects_mismatched_account_without_writing(tmp_path):
    store = make_store(tmp_path)
    assert not store.remember("foreign", "r", NOW, "alpha")
    assert "account does not match" in store.error
    assert not os.path.exists(store._segment)
