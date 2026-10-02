"""`hermes lanagent …`: connect this Hermes to the LANAgent API.

`hermes plugins install PortableDiag/lanagent-hermes --enable` puts the plugin in place;
`hermes lanagent setup` does the rest:

1. a key: `--key gsk_…`, or sign in with the 6-digit code emailed to you (a new email gets
   a free account), which mints a key named "Hermes: <host>" for this Hermes alone;
2. the key goes into Hermes' .env as LANAGENT_API_KEY (mode 600);
3. the MCP server goes into config.yaml through `hermes config set`, in discovery mode
   (a short tool list plus search_tools / call_tool, instead of ~100 schemas every turn);
4. optionally a server-enforced daily credit limit for the key.

`hermes lanagent status` checks the key, the balance and the MCP connection.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import plugin as P


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="lanagent_command", required=False)
    p = subs.add_parser("setup", help="Sign in (or use a key) and connect this Hermes to the LANAgent API")
    p.add_argument("--key", help="an existing gsk_ API key (otherwise sign in with an emailed code)")
    p.add_argument("--email", help="your email, to sign in with a code")
    p.add_argument("--daily-limit", type=int, help="server-enforced credit limit per UTC day for this key")
    p.add_argument("--full-tools", action="store_true",
                   help="list every tool (~100) instead of discovery mode")
    p.add_argument("--url", help=f"API base URL (default {P.DEFAULT_URL})")
    subs.add_parser("status", help="Is the key valid, what is the balance, does MCP answer?")
    parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    sub = getattr(args, "lanagent_command", None) or "status"
    return {"setup": _setup, "status": _status}.get(sub, _unknown)(args)


def _unknown(args) -> int:
    print("usage: hermes lanagent {setup,status}", file=sys.stderr)
    return 2


def _env_path() -> Path:
    return P._home() / ".env"


def write_env(name: str, value: str) -> None:
    """Set NAME=value in Hermes' .env, replacing an existing line; the file stays mode 600."""
    path = _env_path()
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{name}=")] + [f"{name}={value}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def _hermes_config_set(key: str, value: str) -> bool:
    """`hermes config set` edits config.yaml in place, keeping its comments."""
    r = subprocess.run(["hermes", "config", "set", key, value], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  could not set {key}: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
    return r.returncode == 0


def _sign_in(email: str | None) -> str | None:
    email = (email or input("Email for your LANAgent account (a new one is created if needed): ")).strip()
    if "@" not in email:
        print("lanagent: that is not an email address", file=sys.stderr)
        return None
    status, body = P.request("POST", "/portal/email-code", {"email": email}, key="")
    if status != 200 or body.get("success") is False:
        print(f"lanagent: could not send the code: {body.get('error') or f'HTTP {status}'}", file=sys.stderr)
        return None
    code = input(f"A 6-digit code was emailed to {email}. Code: ").strip()
    status, body = P.request("POST", "/portal/email-code/verify", {"email": email, "code": code}, key="")
    token = body.get("token")
    if status != 200 or not token:
        print(f"lanagent: sign-in failed: {body.get('error') or f'HTTP {status}'}", file=sys.stderr)
        return None
    name = f"Hermes: {socket.gethostname()}"[:60]
    req = urllib.request.Request(f"{P.base_url()}/portal/api-keys", method="POST",
                                   data=json.dumps({"name": name}).encode(),
                                   headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                            "User-Agent": P.UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            created = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"lanagent: could not create a key: HTTP {e.code} {e.read()[:200]!r}", file=sys.stderr)
        return None
    key = created.get("apiKey")
    if key:
        print(f"  created API key \"{created.get('name', name)}\" for this Hermes (revoke it in your dashboard to disconnect)")
    return key


def _setup(args: argparse.Namespace) -> int:
    if args.url:
        os.environ["LANAGENT_API_URL"] = args.url.rstrip("/")
    print(f"LANAgent API ({P.base_url()}): connecting this Hermes")
    key = (args.key or "").strip() or _sign_in(args.email)
    if not key or not key.startswith("gsk_"):
        print("lanagent: no API key (expected one starting with gsk_)", file=sys.stderr)
        return 1
    status, bal = P.request("GET", "/credits/balance", key=key)
    if status != 200:
        print(f"lanagent: that key does not work: {bal.get('error') or f'HTTP {status}'}", file=sys.stderr)
        return 1
    write_env("LANAGENT_API_KEY", key)
    if args.url:
        write_env("LANAGENT_API_URL", args.url.rstrip("/"))
    os.environ["LANAGENT_API_KEY"] = key
    print(f"  key saved to {_env_path()} (balance: {bal.get('credits')} credits)")

    mcp_url = f"{P.base_url()}/mcp" + ("" if args.full_tools else "?mode=discover")
    ok = (_hermes_config_set("mcp_servers.lanagent.url", mcp_url)
          and _hermes_config_set("mcp_servers.lanagent.headers.Authorization", "Bearer ${LANAGENT_API_KEY}")
          and _hermes_config_set("mcp_servers.lanagent.timeout", "600"))
    if not ok:
        return 1
    print(f"  MCP server 'lanagent' → {mcp_url}" + ("" if args.full_tools else
          "  (discovery mode: search_tools / describe_tool / call_tool reach every service)"))

    if args.daily_limit:
        r = P.mcp_call("spending_limit", {"credits_per_day": args.daily_limit})
        print(f"  daily limit: {r.get('limit')} credits" if r.get("success") else
              f"  daily limit not set: {r.get('error')}")
    print("Done. Restart Hermes (or start a new session) to load it. Check with: hermes lanagent status")
    return 0


def _status(args) -> int:
    if not P.api_key():
        print("lanagent: not set up yet. Run: hermes lanagent setup")
        return 1
    status, bal = P.request("GET", "/credits/balance", timeout=20)
    if status != 200:
        print(f"lanagent: the key does not work ({bal.get('error') or f'HTTP {status}'}). Run: hermes lanagent setup")
        return 1
    status, body = P.request("POST", "/mcp?mode=discover", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=30)
    tools = len(((body or {}).get("result") or {}).get("tools") or [])
    lim = P.mcp_call("spending_limit")
    limit = lim.get("limit")
    today = f"today {lim.get('spentToday') or 0}" + (f" of {limit}" if limit else "")
    mcp = "OK" if tools else f"not answering (HTTP {status})"
    print(f"LANAgent API {P.base_url()}: key OK · {bal.get('credits')} credits · {today} · MCP {mcp}")
    return 0 if tools else 1
