# mitmproxy HTTP hook

Install mitmproxy in a separate environment using a Python version supported by
mitmproxy, then start the addon from the repository root:

```bash
python -m pip install mitmproxy
mitmdump --listen-port 8888 -s tools/mitmproxy_http_hook.py
```

Configure the HTTP client to use `http://127.0.0.1:8888` as its proxy. The proxy
listens on 8888 so the original application can keep listening on 8080. Traffic
must pass through mitmproxy for the addon to see it. To exercise the default hook:

```bash
curl --noproxy '' --proxy http://127.0.0.1:8888 http://127.0.0.1:8080/download/update36.cfg
```

The default target is `g_pythonlib_url = "http://127.0.0.1:8080/download/update36.cfg"`.
The separate file `tools/responses/update36.cfg` contains the exact 187-byte body
downloaded from `http://download.thinktrader.net:8800/download/update36.cfg` on
2026-10-16. The remote URL supplies the predefined body; forwarding goes to the
original matching target, `127.0.0.1:8080`.

Edit `tools/mitmproxy_http_hook.json` to add or change targets, or select a custom
file with:

```bash
mitmdump --listen-port 8888 -s tools/mitmproxy_http_hook.py --set http_hook_config=/absolute/path/hooks.json
```

Each entry in `targets` has these settings:

| Setting | Meaning |
| --- | --- |
| `host`, `port`, `path` | All three must match. Hostnames are case insensitive. Port and path are exact; paths are case sensitive and are not URL decoded. Query parameters are ignored. |
| `response_file` | Separate raw response body file, or `null` to use the upstream response. File bytes are reread for every response, so editing the body does not require restarting the proxy. |
| `response_status` | Predefined response status, default 200. |
| `response_headers` | Predefined headers, default `Content-Type: application/octet-stream`. Content length is computed from the file. |
| `request_target` | Boolean, default false. When true, let mitmproxy forward the original request and record the upstream response before applying the predefined response. |
| `record_dir` | Capture directory, default `recordings`. |

File and directory paths are relative to the JSON configuration file. Restart
mitmproxy after editing the JSON configuration. Unmatched requests pass through.
Duplicate target definitions and missing configured body files are rejected.

| Predefined file | `request_target` | Result |
| --- | --- | --- |
| Set | false | Return the file immediately, without contacting the target. |
| Set | true | Forward and record the upstream response, then return the file. |
| `null` | true | Forward, record, and return the upstream response. |
| `null` | false | Invalid configuration. |

Forwarding preserves the method, URL, headers, and body. With both forwarding and
a predefined file enabled, the addon fetches the original target using Python's
HTTP client in a worker thread, records the result, and then returns the predefined
file, including when the upstream attempt fails. This fetch uses a 30-second socket
timeout and does not follow redirects. HTTP transfer framing is reconstructed.
With forwarding alone, mitmproxy performs the request and handles connection errors.
The addon buffers bodies and requires `stream_large_bodies` to be unset.

Each capture gets a unique timestamp/flow-ID filename: `.body` contains the raw
upstream bytes (including any content encoding), and `.json` contains the request
URL and method, response status, headers (including duplicates), and body filename.
Connection failures are recorded as JSON with an `error` field. Captures are
written before replacement, so they contain the actual upstream response.

To refresh the predefined body from its source:

```bash
curl --fail --location --output tools/responses/update36.cfg http://download.thinktrader.net:8800/download/update36.cfg
```
