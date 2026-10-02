"""Run: python3 -m unittest discover -s tests   (no network: a local fake API)."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lanagent", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules["lanagent"] = pkg
spec.loader.exec_module(pkg)
from lanagent import plugin as P, cli as C  # noqa: E402

STATE = {"polls": 0}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("X-API-Key") != "gsk_test" and not self.path.startswith("/download/"):
            return self._json(401, {"success": False, "error": "Authentication required"})
        if self.path == "/credits/balance":
            return self._json(200, {"success": True, "credits": 42})
        if self.path == "/social/jobs/j1":
            STATE["polls"] += 1
            if STATE["polls"] < 2:
                return self._json(200, {"success": True, "status": "pending", "jobId": "j1"})
            return self._json(200, {"success": True, "status": "done", "jobId": "j1", "title": "Clip",
                                    "downloadUrl": "/download/agent1/tok123"})
        if self.path == "/social/jobs/bad":
            return self._json(200, {"success": False, "status": "failed", "error": "video unavailable", "creditsRefunded": 10})
        if self.path == "/download/agent1/tok123":
            data = b"MP4DATA" * 100
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", 'attachment; filename="clip.mp4"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json(404, {"success": False, "error": "no route"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.startswith("/mcp") and body.get("params", {}).get("name") == "spending_limit":
            return self._json(200, {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"success": True, "limit": 100, "spentToday": 7}}})
        self._json(404, {"success": False})


class PluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.home = tempfile.mkdtemp()
        os.environ["HERMES_HOME"] = cls.home
        os.environ["LANAGENT_API_URL"] = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        os.environ["LANAGENT_API_KEY"] = "gsk_test"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_wait_job_waits_then_saves_the_file(self):
        STATE["polls"] = 0
        r = json.loads(P.wait_job({"job_id": "j1", "timeout_seconds": 30}))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["status"], "done")
        f = r["files"][0]
        self.assertTrue(f["path"].endswith("clip.mp4"))
        self.assertEqual(Path(f["path"]).read_bytes(), b"MP4DATA" * 100)
        self.assertGreaterEqual(STATE["polls"], 2)

    def test_a_failed_job_says_why_and_reports_the_refund(self):
        r = json.loads(P.wait_job({"job_id": "bad"}))
        self.assertFalse(r["success"])
        self.assertIn("video unavailable", r["error"])
        self.assertEqual(r["refunded"], 10)

    def test_fetch_file_saves_a_result_link(self):
        r = json.loads(P.fetch_file({"url": "/download/agent1/tok123", "name": "mine.mp4"}))
        self.assertTrue(r["success"], r)
        self.assertTrue(r["path"].endswith("mine.mp4"))

    def test_spend_reports_balance_limit_and_session(self):
        P._session_spend.clear()
        P.on_post_tool_call(tool_name="mcp_lanagent_scrape", result="Charged 3 credit(s); balance 39.\n\n{}", session_id="s1")
        P.on_post_tool_call(tool_name="mcp_lanagent_call_tool", result={"content": [{"text": "Charged 2 credit(s)"}]}, session_id="s1")
        P.on_post_tool_call(tool_name="web_search", result="Charged 9 credit(s)", session_id="s1")
        r = json.loads(P.spend({}, session_id="s1"))
        self.assertEqual((r["credits"], r["daily_limit"], r["spent_today"], r["spent_this_session"]), (42, 100, 7, 5))
        self.assertIn("this session 5", P.slash_command())

    def test_download_links_finds_each_link_once(self):
        self.assertEqual(P.download_links({"a": "/download/x/1", "b": ["https://h/download/x/2", "/download/x/1"], "c": "no"}),
                         ["/download/x/1", "https://h/download/x/2"])

    def test_write_env_replaces_the_line_and_keeps_others(self):
        env = Path(self.home) / ".env"
        env.write_text("OTHER=1\nLANAGENT_API_KEY=old\n")
        C.write_env("LANAGENT_API_KEY", "gsk_new")
        self.assertEqual(env.read_text(), "OTHER=1\nLANAGENT_API_KEY=gsk_new\n")
        self.assertEqual(oct(env.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
