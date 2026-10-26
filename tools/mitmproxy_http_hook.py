"""Configurable HTTP response override and capture addon for mitmproxy.

Run from the repository root::

    mitmdump --listen-port 8888 -s tools/mitmproxy_http_hook.py

See docs/MITMPROXY_HTTP_HOOK.md for configuration and forwarding semantics.
"""

from __future__ import annotations

import asyncio
import http.client as http_client
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import ctx, exceptions, flow as mitmflow, http


g_pythonlib_url = "http://127.0.0.1:8080/download/update36.cfg"
DEFAULT_CONFIG = Path(__file__).with_name("mitmproxy_http_hook.json")
METADATA_KEY = "http_hook"
logger = logging.getLogger(__name__)


def load_targets(config_path: Path) -> list[dict]:
    """Validate configuration; resolve file paths relative to the JSON file."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("targets"), list):
        raise ValueError("configuration must contain a targets array")
    targets = []
    seen = set()
    for entry in config["targets"]:
        if not isinstance(entry, dict):
            raise ValueError("each target must be an object")
        host, port, path = (entry.get(key) for key in ("host", "port", "path"))
        if not isinstance(host, str) or not host or any(c in host for c in "/?#@"):
            raise ValueError("host must be a hostname or IP address without a scheme")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        if not isinstance(path, str) or not path.startswith("/") or any(c in path for c in "?#"):
            raise ValueError("path must start with / and contain no query or fragment")
        key = (host.lower(), port, path)
        if key in seen:
            raise ValueError("duplicate host/port/path target: %r" % (key,))
        seen.add(key)
        request_target = entry.get("request_target", False)
        if type(request_target) is not bool:
            raise ValueError("request_target must be true or false")
        response_file = entry.get("response_file")
        if response_file is not None:
            if not isinstance(response_file, str) or not response_file:
                raise ValueError("response_file must be a nonempty file path or null")
            response_file = str((config_path.parent / response_file).resolve())
            if not Path(response_file).is_file():
                raise ValueError("response_file does not exist: " + response_file)
        if not request_target and response_file is None:
            raise ValueError("a target needs response_file or request_target=true")
        status = entry.get("response_status", 200)
        if type(status) is not int or not 200 <= status <= 599:
            raise ValueError("response_status must be an integer between 200 and 599")
        headers = entry.get("response_headers", {"Content-Type": "application/octet-stream"})
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError("response_headers must map strings to strings")
        record_dir = entry.get("record_dir", "recordings")
        if not isinstance(record_dir, str) or not record_dir:
            raise ValueError("record_dir must be a nonempty directory path")
        targets.append({
            "host": host.lower(), "port": port, "path": path,
            "request_target": request_target, "response_file": response_file,
            "response_status": status, "response_headers": headers,
            "record_dir": str((config_path.parent / record_dir).resolve()),
        })
    return targets


class HttpHook:
    def __init__(self) -> None:
        self.targets = []

    def load(self, loader) -> None:
        loader.add_option(
            "http_hook_config", str, str(DEFAULT_CONFIG),
            "JSON configuration for exact host, port, and path HTTP hooks.",
        )

    def configure(self, updated: set[str]) -> None:
        if ctx.options.stream_large_bodies:
            raise exceptions.OptionsError("HTTP hook requires stream_large_bodies to be unset")
        if "http_hook_config" in updated:
            try:
                self.targets = load_targets(Path(ctx.options.http_hook_config).resolve())
            except (OSError, ValueError) as exc:
                raise exceptions.OptionsError("Invalid HTTP hook configuration: %s" % exc) from exc

    @staticmethod
    def predefined_response(target: dict):
        if target["response_file"] is None:
            return None
        try:
            body = Path(target["response_file"]).read_bytes()
        except OSError as exc:
            logger.error("Cannot read predefined response: %s", exc)
            return http.Response.make(500, b"Cannot read predefined response\n")
        response = http.Response.make(target["response_status"], b"", target["response_headers"])
        response.raw_content = body
        # The file contains the complete body, so mitmproxy computes its length.
        response.headers.pop("transfer-encoding", None)
        response.headers["content-length"] = str(len(body))
        return response

    def match_target(self, flow: http.HTTPFlow):
        request = flow.request
        # Strip only the query, preserving escaping and repeated slashes exactly.
        path = request.path.split("?", 1)[0]
        return next((target for target in self.targets if (
            request.host.lower() == target["host"]
            and request.port == target["port"]
            and path == target["path"]
        )), None)

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        if self.match_target(flow) is not None:
            # Decide whether to forward before any request body is sent upstream.
            flow.request.stream = False

    @staticmethod
    def fetch_upstream(flow: http.HTTPFlow) -> http.HTTPFlow:
        """Fetch independently so an upstream failure cannot prevent a predefined response."""
        captured = flow.copy()
        request = flow.request
        connection_class = http_client.HTTPSConnection if request.scheme == "https" else http_client.HTTPConnection
        connection = connection_class(request.host, request.port, timeout=30)
        try:
            connection.putrequest(request.method, request.path, skip_host=True, skip_accept_encoding=True)
            body = request.raw_content or b""
            # mitmproxy has already removed HTTP chunk framing from raw_content.
            for name, value in request.headers.items(multi=True):
                if name.lower() not in {"transfer-encoding", "content-length", "proxy-connection"}:
                    connection.putheader(name, value)
            if "host" not in request.headers:
                # HTTP/2 uses :authority, which is stored separately by mitmproxy.
                connection.putheader("Host", request.host_header or request.host)
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            captured.response = http.Response.make(response.status, b"")
            captured.response.reason = response.reason
            captured.response.headers = http.Headers([
                (name.encode("latin-1"), value.encode("latin-1")) for name, value in response.getheaders()
            ])
            captured.response.http_version = "HTTP/1.0" if response.version == 10 else "HTTP/1.1"
            captured.response.raw_content = response.read()
        except (OSError, http_client.HTTPException) as exc:
            captured.error = mitmflow.Error(str(exc))
        finally:
            connection.close()
        return captured

    async def request(self, flow: http.HTTPFlow) -> None:
        target = self.match_target(flow)
        if target is None:
            return
        if target["request_target"]:
            if target["response_file"] is not None:
                captured = await asyncio.to_thread(self.fetch_upstream, flow)
                self.record(captured, dict(target, original_url=flow.request.pretty_url))
                flow.response = self.predefined_response(target)
                return
            # Leaving flow.response unset lets mitmproxy forward the original request.
            # Store only serializable values so mitmproxy can still save the flow.
            flow.metadata[METADATA_KEY] = dict(target, original_url=flow.request.pretty_url)
        else:
            flow.response = self.predefined_response(target)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if METADATA_KEY in flow.metadata:
            # Captures require the complete body.
            flow.response.stream = False

    @staticmethod
    def record(flow: http.HTTPFlow, target: dict) -> None:
        directory = Path(target["record_dir"])
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            stem = stamp + "_" + flow.id
            body_name = stem + ".body"
            metadata = {
                "recorded_at": stamp,
                "request": {"method": flow.request.method, "url": target["original_url"]},
                "response": None,
                "error": flow.error.msg if flow.error else None,
            }
            if flow.response is not None:
                (directory / body_name).write_bytes(flow.response.raw_content or b"")
                metadata["response"] = {
                    "status_code": flow.response.status_code,
                    "reason": flow.response.reason,
                    "http_version": flow.response.http_version,
                    "headers": list(flow.response.headers.items(multi=True)),
                    "body_file": body_name,
                }
            (directory / (stem + ".json")).write_text(
                json.dumps(metadata, indent=2, ensure_ascii=True) + "\n", encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Cannot record HTTP hook response to %s: %s", directory, exc)

    def response(self, flow: http.HTTPFlow) -> None:
        target = flow.metadata.pop(METADATA_KEY, None)
        if target is None:
            return
        self.record(flow, target)
        predefined = self.predefined_response(target)
        if predefined is not None:
            flow.response = predefined

    def error(self, flow: http.HTTPFlow) -> None:
        target = flow.metadata.pop(METADATA_KEY, None)
        if target is None:
            return
        self.record(flow, target)


addons = [HttpHook()]
