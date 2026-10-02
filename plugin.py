"""LANAgent API for Hermes: the parts MCP alone does not give an agent.

The API's MCP server (https://api.lanagent.net/mcp) already gives Hermes every service as a
tool. This plugin adds:

- lanagent_wait_job: downloads and audio extractions run as jobs. Instead of polling
  job_status turn after turn, wait here until the job is done and get the file saved
  locally (its path), ready to send or process.
- lanagent_fetch_file: save any result link (a generated image, a download link) locally.
- lanagent_spend: credits left, today's spend against the daily limit, and what this
  session has spent.
- A post_tool_call hook that adds up "Charged N credit(s)" from LANAgent tool results, and a
  /lanagent command that shows it.

File bytes never pass through the model: tools take and return local paths.
Standard library only (urllib), so the plugin installs with no dependencies.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://api.lanagent.net"
UA = "lanagent-hermes/0.1.0"
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024        # downloads can be long videos
CHARGED = re.compile(r"Charged (\d+) credit")

# Credits charged by LANAgent tools in this process, per Hermes session (post_tool_call hook).
_session_spend: dict[str, int] = {}


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def files_dir() -> Path:
    return _home() / "lanagent-files"


def _secret(name: str, default: str = "") -> str:
    try:
        from gateway.platforms._shared import get_scoped_secret
        value = get_scoped_secret(name, default)
        if value:
            return value
    except Exception:  # noqa: BLE001 — outside the gateway, or an older Hermes
        pass
    if os.environ.get(name):
        return os.environ[name]
    env = _home() / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def base_url() -> str:
    return (_secret("LANAGENT_API_URL", DEFAULT_URL) or DEFAULT_URL).rstrip("/")


def api_key() -> str:
    return _secret("LANAGENT_API_KEY", "").strip()


def _available() -> bool:
    return bool(api_key())


def request(method: str, path: str, body=None, timeout: float = 60.0, key: str | None = None) -> tuple[int, dict]:
    """One JSON call to the API. Returns (status, body); never raises for an HTTP error."""
    url = path if path.startswith("http") else f"{base_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    k = api_key() if key is None else key
    if k:
        headers["X-API-Key"] = k
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"success": False, "error": f"cannot reach {base_url()}: {getattr(e, 'reason', e)}"}
    try:
        return status, json.loads(raw.decode() or "{}")
    except ValueError:
        return status, {"success": status < 400, "raw": raw[:500].decode(errors="replace")}


def mcp_call(tool: str, arguments: dict | None = None, timeout: float = 60.0) -> dict:
    """Call a tool on the API's MCP server (used for the server-side daily limit)."""
    status, body = request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": tool, "arguments": arguments or {}}}, timeout)
    result = (body or {}).get("result") or {}
    return result.get("structuredContent") or {"success": False, "error": (body or {}).get("error", {}).get("message", f"HTTP {status}")}


def _ok(**kw) -> str:
    return json.dumps({"success": True, **kw})


def _err(msg: str, **kw) -> str:
    return json.dumps({"success": False, "error": msg, **kw})


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", name).strip(" .") or "file"
    return name[:150]


def save_url(url: str, dest_dir: Path, name: str | None = None) -> dict:
    """Download a result link to dest_dir. Gateway download links need the key; others do not."""
    full = url if url.startswith("http") else f"{base_url()}{url if url.startswith('/') else '/' + url}"
    headers = {"User-Agent": UA}
    if urllib.parse.urlparse(full).netloc == urllib.parse.urlparse(base_url()).netloc and api_key():
        headers["X-API-Key"] = api_key()
    req = urllib.request.Request(full, headers=headers)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=600) as r:
        cd = r.headers.get("content-disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
        fname = _safe_name(name or (urllib.parse.unquote(m.group(1)) if m else "") or
                           Path(urllib.parse.urlparse(full).path).name or "download")
        out = dest_dir / fname
        size = 0
        with open(out, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    f.close()
                    out.unlink(missing_ok=True)
                    raise ValueError("file is larger than 2 GB")
                f.write(chunk)
        return {"path": str(out), "bytes": size, "content_type": r.headers.get("content-type")}


def download_links(obj) -> list[str]:
    """Every gateway download link in a job result, in order, without repeats."""
    found: list[str] = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and re.search(r"(^|/)download/[^\s\"']+", v) and v not in found:
            found.append(v)

    walk(obj)
    return found


# ---------------------------------------------------------------------------------- tools

def wait_job(args: dict, **kw) -> str:
    job = str(args.get("job_id") or "").strip()
    if not job:
        return _err("job_id is required (the job_id a social_download / social_audio call returned)")
    limit = min(max(int(args.get("timeout_seconds") or 300), 10), 1800)
    deadline = time.monotonic() + limit
    delay = 3.0
    while True:
        status, body = request("GET", f"/social/jobs/{urllib.parse.quote(job)}", timeout=30)
        state = (body or {}).get("status")
        if status == 404:
            return _err("job not found or expired (jobs are kept for a limited time)", job_id=job)
        if status in (401, 403):
            return _err((body or {}).get("error") or f"HTTP {status}", job_id=job)
        if state == "failed":
            return _err(body.get("error") or "the job failed", job_id=job, upstreamStatus=body.get("upstreamStatus"),
                        refunded=body.get("creditsRefunded"))
        if state == "done":
            links = download_links(body)
            saved, errors = [], []
            if args.get("download", True):
                for link in links:
                    try:
                        saved.append(save_url(link, files_dir() / _safe_name(job)))
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{link}: {e}")
            keep = {k: v for k, v in body.items() if k not in ("success", "status")}
            return _ok(job_id=job, status="done", files=saved, links=links,
                       **({"download_errors": errors} if errors else {}), result=keep)
        if time.monotonic() + delay > deadline:
            return _ok(job_id=job, status=state or "pending", done=False,
                       note=f"still running after {limit}s; call lanagent_wait_job again to keep waiting")
        time.sleep(delay)
        delay = min(delay * 1.5, 15.0)


def fetch_file(args: dict, **kw) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return _err("url is required")
    if not (url.startswith("http") or "download/" in url):
        return _err("url must be an http(s) link or a /download/… link from a LANAgent result")
    try:
        return _ok(**save_url(url, files_dir() / "fetched", args.get("name")))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def spend(args: dict, **kw) -> str:
    status, bal = request("GET", "/credits/balance", timeout=30)
    if status != 200:
        return _err(bal.get("error") or f"HTTP {status}")
    lim = mcp_call("spending_limit")
    session = kw.get("session_id") or ""
    return _ok(credits=bal.get("credits"), subscription=bal.get("subscription"),
               daily_limit=lim.get("limit"), spent_today=lim.get("spentToday"),
               spent_this_session=_session_spend.get(session, 0) if session else sum(_session_spend.values()))


def _schema(name, description, properties, required=()):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}


WAIT = _schema(
    "lanagent_wait_job",
    "Wait for a LANAgent download or audio job (the job_id that social_download / social_audio returned) "
    f"to finish, and save its files locally under {files_dir()}. Returns the local file paths. "
    "Use this instead of polling job_status. Free.",
    {"job_id": {"type": "string", "description": "The job_id from social_download or social_audio"},
     "timeout_seconds": {"type": "integer", "description": "How long to wait (10–1800, default 300)"},
     "download": {"type": "boolean", "description": "Save the files locally (default true); false = just wait"}},
    ("job_id",))
FETCH = _schema(
    "lanagent_fetch_file",
    "Save a file from a LANAgent result link (a generated image, a download link) to a local path. Free.",
    {"url": {"type": "string", "description": "The link from a LANAgent tool result"},
     "name": {"type": "string", "description": "File name to save as (optional)"}},
    ("url",))
SPEND = _schema(
    "lanagent_spend",
    "Your LANAgent API credits: balance, today's spend against your daily limit, and what this "
    "session has spent. Free.",
    {})


# --------------------------------------------------------------------------- hook & command

def on_post_tool_call(tool_name: str = "", result=None, session_id: str = "", **kw) -> None:
    """Add up what LANAgent tools charged ("Charged N credit(s)" leads every paid result)."""
    if not str(tool_name).startswith("mcp_lanagent"):
        return
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    m = CHARGED.search(text or "")
    if m:
        key = session_id or "default"
        _session_spend[key] = _session_spend.get(key, 0) + int(m.group(1))


def slash_command(raw_args: str = "") -> str:
    if not _available():
        return "LANAgent API: not set up. Run: hermes lanagent setup"
    data = json.loads(spend({}))
    if not data.get("success"):
        return f"LANAgent API: {data.get('error')}"
    limit = data.get("daily_limit")
    return (f"LANAgent API: {data.get('credits')} credits left · today {data.get('spent_today') or 0}"
            f"{f' of {limit}' if limit else ''} · this session {data.get('spent_this_session') or 0}")


def register(ctx) -> None:
    from . import cli as _cli
    for name, schema, handler, emoji in (
        ("lanagent_wait_job", WAIT, wait_job, "⏳"),
        ("lanagent_fetch_file", FETCH, fetch_file, "📥"),
        ("lanagent_spend", SPEND, spend, "💳"),
    ):
        ctx.register_tool(name=name, toolset="lanagent", schema=schema, handler=handler,
                          check_fn=_available, emoji=emoji)
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("post_tool_call", on_post_tool_call)
    if hasattr(ctx, "register_command"):
        ctx.register_command("lanagent", slash_command, description="LANAgent API credits: balance, today, this session")
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(name="lanagent", help="Set up and check the LANAgent API connection",
                                 setup_fn=_cli.register_cli, handler_fn=_cli.dispatch)
