"""Offline behavior tests using mitmproxy's actual HTTP flow types."""

import asyncio
import gzip
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("mitmproxy", reason="standalone HTTP hook requires optional mitmproxy")
from mitmproxy import flow as mitmflow, http
from mitmproxy.test import tflow


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mitmproxy_http_hook", ROOT / "tools/mitmproxy_http_hook.py")
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def setup_hook(tmp_path, *, request_target=True, predefined=True):
    (tmp_path / "response.cfg").write_bytes(b"predefined\r\n")
    config = {
        "targets": [{
            "host": "example.test", "port": 8080, "path": "/download/update36.cfg",
            "request_target": request_target,
            "response_file": "response.cfg" if predefined else None,
            "response_headers": {"Content-Type": "text/plain"},
        }],
    }
    config_path = tmp_path / "hooks.json"
    config_path.write_text(json.dumps(config))
    addon = hook.HttpHook()
    addon.targets = hook.load_targets(config_path)
    return addon


def make_flow(url="http://example.test:8080/download/update36.cfg", method="GET", body=b""):
    flow = tflow.tflow()
    flow.request = http.Request.make(method, url, body)
    return flow


@pytest.mark.parametrize("url", [
    "http://other.test:8080/download/update36.cfg",
    "http://example.test:8081/download/update36.cfg",
    "http://example.test/download/update36.cfg",
    "http://example.test:8080/download/other.cfg",
    "http://example.test:8080/download/update36.cfg/",
    "http://example.test:8080/Download/update36.cfg",
    "http://example.test:8080/download/%75pdate36.cfg",
    "http://example.test:8080//download/update36.cfg",
])
def test_nonmatching_requests_pass_through(tmp_path, url):
    addon = setup_hook(tmp_path, request_target=False)
    flow = make_flow(url)
    asyncio.run(addon.request(flow))
    assert flow.response is None
    assert hook.METADATA_KEY not in flow.metadata


def test_predefined_only_never_forwards_or_records(tmp_path):
    addon = setup_hook(tmp_path, request_target=False)
    flow = make_flow("http://EXAMPLE.TEST:8080/download/update36.cfg?version=1")
    asyncio.run(addon.request(flow))
    assert flow.response.status_code == 200
    assert flow.response.raw_content == b"predefined\r\n"
    addon.response(flow)
    assert not (tmp_path / "recordings").exists()


@pytest.mark.parametrize("predefined", [True, False])
def test_forward_preserves_request_and_captures_original_response(tmp_path, predefined, monkeypatch):
    addon = setup_hook(tmp_path, predefined=predefined)
    flow = make_flow(method="POST", body=b"original request")
    original_request = flow.request.get_state()
    # Exercise native forwarding; predefined fetch is covered separately.
    if predefined:
        addon.targets[0]["response_file"] = None
    asyncio.run(addon.request(flow))
    if predefined:
        flow.metadata[hook.METADATA_KEY]["response_file"] = str(tmp_path / "response.cfg")
    assert flow.response is None  # mitmproxy performs forwarding
    assert flow.request.get_state() == original_request
    json.dumps(flow.metadata)  # remains serializable for saved mitmproxy flows
    original_body = gzip.compress(b"actual upstream body")
    flow.response = http.Response.make(404, b"")
    flow.response.headers = http.Headers([
        (b"Content-Encoding", b"gzip"),
        (b"Set-Cookie", b"one=1"), (b"Set-Cookie", b"two=2"),
    ])
    flow.response.raw_content = original_body
    flow.response.stream = True
    addon.responseheaders(flow)
    assert flow.response.stream is False
    addon.response(flow)
    captures = list((tmp_path / "recordings").glob("*.json"))
    assert len(captures) == 1
    recorded = json.loads(captures[0].read_text())
    assert recorded["response"]["status_code"] == 404
    assert recorded["request"]["method"] == "POST"
    assert recorded["response"]["headers"].count(["Set-Cookie", "one=1"]) == 1
    assert recorded["response"]["headers"].count(["Set-Cookie", "two=2"]) == 1
    assert (captures[0].parent / recorded["response"]["body_file"]).read_bytes() == original_body
    assert flow.response.raw_content == (b"predefined\r\n" if predefined else original_body)
    addon.response(flow)
    assert len(list((tmp_path / "recordings").glob("*.json"))) == 1


def test_concurrent_flows_get_separate_captures(tmp_path):
    addon = setup_hook(tmp_path, predefined=False)
    first, second = make_flow(), make_flow()
    asyncio.run(addon.request(first))
    asyncio.run(addon.request(second))
    for flow, body in [(second, b"second"), (first, b"first")]:
        flow.response = http.Response.make(200, body)
        addon.response(flow)
    bodies = {path.read_bytes() for path in (tmp_path / "recordings").glob("*.body")}
    assert bodies == {b"first", b"second"}


@pytest.mark.parametrize("predefined", [True, False])
def test_upstream_error_is_recorded(tmp_path, predefined):
    addon = setup_hook(tmp_path, predefined=False)
    flow = make_flow()
    asyncio.run(addon.request(flow))
    flow.error = mitmflow.Error("Connection refused")
    addon.error(flow)
    capture = next((tmp_path / "recordings").glob("*.json"))
    recorded = json.loads(capture.read_text())
    assert recorded["error"] == "Connection refused"
    assert recorded["response"] is None
    assert flow.response is None
    assert flow.error.msg == "Connection refused"


def test_matching_request_body_is_buffered_before_forwarding_decision(tmp_path):
    addon = setup_hook(tmp_path, request_target=False)
    flow = make_flow(method="POST", body=b"original request")
    flow.request.stream = True
    addon.requestheaders(flow)
    assert flow.request.stream is False
    asyncio.run(addon.request(flow))
    assert flow.response.raw_content == b"predefined\r\n"


def test_body_file_can_be_edited_without_reloading(tmp_path):
    addon = setup_hook(tmp_path, request_target=False)
    (tmp_path / "response.cfg").write_bytes(b"new contents")
    flow = make_flow()
    asyncio.run(addon.request(flow))
    assert flow.response.raw_content == b"new contents"
    assert flow.response.headers["content-length"] == str(len(b"new contents"))


def test_predefined_encoded_body_is_not_encoded_twice(tmp_path):
    addon = setup_hook(tmp_path, request_target=False)
    raw_body = gzip.compress(b"predefined")
    (tmp_path / "response.cfg").write_bytes(raw_body)
    addon.targets[0]["response_headers"] = {"Content-Encoding": "gzip", "Transfer-Encoding": "chunked"}
    flow = make_flow()
    asyncio.run(addon.request(flow))
    assert flow.response.raw_content == raw_body
    assert flow.response.content == b"predefined"
    assert flow.response.headers["content-length"] == str(len(raw_body))
    assert "transfer-encoding" not in flow.response.headers


@pytest.mark.parametrize("change", [
    {"port": "8080"}, {"port": True}, {"port": 0}, {"path": "download/update36.cfg"},
    {"host": "http://example.test"}, {"path": "/download/update36.cfg?x=1"},
    {"request_target": "false"}, {"response_file": "missing.cfg"},
    {"request_target": False, "response_file": None},
])
def test_invalid_configuration_is_rejected(tmp_path, change):
    setup_hook(tmp_path)
    path = tmp_path / "hooks.json"
    config = json.loads(path.read_text())
    config["targets"][0].update(change)
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        hook.load_targets(path)


def test_default_configuration_has_requested_target_and_saved_response():
    target, = hook.load_targets(hook.DEFAULT_CONFIG)
    assert (target["host"], target["port"], target["path"]) == (
        "127.0.0.1", 8080, "/download/update36.cfg",
    )
    assert target["request_target"] is True
    body = Path(target["response_file"]).read_bytes()
    assert body.startswith(b"[python]\r\n")
    assert len(body) == 187


@pytest.mark.parametrize("failed", [False, True])
def test_predefined_response_survives_upstream_fetch_failure(tmp_path, monkeypatch, failed):
    addon = setup_hook(tmp_path)
    flow = make_flow()
    def fetch(original):
        captured = original.copy()
        if failed:
            captured.error = mitmflow.Error("Connection refused")
        else:
            captured.response = http.Response.make(404, b"actual response")
        return captured
    monkeypatch.setattr(addon, "fetch_upstream", fetch)
    asyncio.run(addon.request(flow))
    assert flow.response.raw_content == b"predefined\r\n"
    recorded = json.loads(next((tmp_path / "recordings").glob("*.json")).read_text())
    assert bool(recorded["error"]) == failed
    if not failed:
        assert recorded["response"]["status_code"] == 404
