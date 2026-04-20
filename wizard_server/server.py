"""HTTP server — stdlib-only, serves dashboard + API routes."""

import json
import os
import socketserver
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import config_schema, repos, jobs as jobs_mod


# ─────────────────────────────────────────────────────────────
# GLOBALS (set in start_server)
# ─────────────────────────────────────────────────────────────
_config = None
_repos_info = None
_job_queue = None
_project_root = Path(__file__).parent.parent


def get_config():
    return _config


def get_repos_info():
    return _repos_info


def get_job_queue():
    return _job_queue


def reload_repos():
    global _repos_info
    _repos_info = repos.build_all_repos_info(_config)


# ─────────────────────────────────────────────────────────────
# REQUEST HANDLER
# ─────────────────────────────────────────────────────────────

class WizardHandler(SimpleHTTPRequestHandler):
    """Routes: / serves dashboard, /api/* delegates to API handlers."""

    def __init__(self, *args, **kwargs):
        # Serve static files from project root
        super().__init__(*args, directory=str(_project_root), **kwargs)

    # ── Routing ──────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "" or path == "/dashboard":
            self._serve_dashboard()
        elif path == "/api/config":
            self._handle_get_config()
        elif path.startswith("/api/status/"):
            job_id = path.split("/api/status/")[-1]
            self._handle_get_status(job_id)
        elif path == "/api/health":
            self._json_response(200, {"status": "ok", "version": "0.1.0"})
        else:
            # Fall through to static file serving
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        body = self._read_body()

        if path == "/api/analyze":
            self._handle_analyze(body)
        elif path == "/api/test-upgrade":
            self._handle_test_upgrade(body)
        elif path == "/api/config/repos":
            self._handle_add_repo(body)
        elif path.startswith("/api/cancel/"):
            job_id = path.split("/api/cancel/")[-1]
            self._handle_cancel(job_id)
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self._add_cors_headers()
        self.end_headers()

    # ── Dashboard ────────────────────────────────────────────

    def _serve_dashboard(self):
        dashboard = _project_root / "vulnerability-tracker" / "dashboard.html"
        if not dashboard.exists():
            self._json_response(404, {
                "error": "Dashboard not found. Run: ./scripts/refresh-dashboard.sh"
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(dashboard.read_bytes())

    # ── API: Config ──────────────────────────────────────────

    def _handle_get_config(self):
        result = {
            "server": _config.get("server", {}),
            "repos": [],
        }
        for ri in _repos_info:
            repo_summary = {
                "name": ri["name"],
                "path": ri["path"],
                "github": ri.get("github", ""),
                "type": ri.get("type", "unknown"),
                "valid": ri.get("valid", False),
                "validation_error": ri.get("validation_error"),
                "function_count": ri.get("function_count", 0),
                "functions_with_tests": ri.get("functions_with_tests", 0),
            }
            # Include function list for lambda monorepos
            if ri.get("type") == "lambda_monorepo" and ri.get("functions"):
                repo_summary["functions"] = {
                    name: {
                        "runtime": f.get("runtime"),
                        "language": f.get("language"),
                        "has_tests": f.get("has_tests", False),
                        "test_command": f.get("test_command"),
                        "deps_file": f.get("deps_file"),
                        "trigger_type": f.get("trigger_type"),
                        "services": f.get("services", []),
                    }
                    for name, f in ri["functions"].items()
                }
            result["repos"].append(repo_summary)

        self._json_response(200, result)

    def _handle_add_repo(self, body):
        if not body:
            self._json_response(400, {"error": "Request body required"})
            return

        required = ["name", "path", "github"]
        for field in required:
            if not body.get(field):
                self._json_response(400, {"error": f"Missing field: {field}"})
                return

        is_valid, err = config_schema.validate_repo(body)
        if not is_valid:
            self._json_response(400, {"error": err})
            return

        global _config
        _config = config_schema.add_repo(_config, body)
        reload_repos()

        # Find the newly added repo info
        for ri in _repos_info:
            if ri["name"] == body["name"]:
                self._json_response(200, {"status": "ok", "repo": ri})
                return

        self._json_response(200, {"status": "ok"})

    # ── API: Analyze ─────────────────────────────────────────

    def _handle_analyze(self, body):
        # Import here to avoid circular import
        from . import api
        result = api.handle_analyze(body, _repos_info, _config)
        self._json_response(result.pop("_status", 200), result)

    # ── API: Test Upgrade ────────────────────────────────────

    def _handle_test_upgrade(self, body):
        from . import api
        result = api.handle_test_upgrade(body, _repos_info, _config, _job_queue)
        self._json_response(result.pop("_status", 202), result)

    # ── API: Status ──────────────────────────────────────────

    def _handle_get_status(self, job_id):
        status = _job_queue.get_status(job_id)
        if not status:
            self._json_response(404, {
                "error": "Job not found. It may have expired (kept for 1 hour)."
            })
            return
        self._json_response(200, status)

    # ── API: Cancel ──────────────────────────────────────────

    def _handle_cancel(self, job_id):
        ok = _job_queue.cancel(job_id)
        if not ok:
            self._json_response(404, {"error": "Job not found"})
            return
        self._json_response(200, {"status": "cancelled", "job_id": job_id})

    # ── Helpers ──────────────────────────────────────────────

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _json_response(self, status_code, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _add_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format, *args):
        """Suppress default access logs for cleaner output. Only log errors."""
        if args and str(args[0]).startswith("4") or str(args[0]).startswith("5"):
            super().log_message(format, *args)


# ─────────────────────────────────────────────────────────────
# SERVER STARTUP
# ─────────────────────────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def start_server(host=None, port=None):
    """Load config, scan repos, start HTTP server."""
    global _config, _repos_info, _job_queue

    _config = config_schema.load_config()

    srv_cfg = _config.get("server", {})
    host = host or srv_cfg.get("host", "127.0.0.1")
    port = port or srv_cfg.get("port", 8787)

    _repos_info = repos.build_all_repos_info(_config)
    _job_queue = jobs_mod.JobQueue(
        max_queue_depth=srv_cfg.get("max_queue_depth", 3),
        job_ttl_seconds=srv_cfg.get("job_ttl_seconds", 3600),
    )

    # Print startup info
    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(" Upgrade Wizard Server")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    print(f"  Dashboard:  http://{host}:{port}/")
    print(f"  API:        http://{host}:{port}/api/")
    print(f"  Config:     {config_schema.CONFIG_PATH}")
    print()

    for ri in _repos_info:
        status = "✓" if ri.get("valid") else "✗"
        print(f"  {status} {ri['name']} ({ri.get('type', '?')})")
        if ri.get("type") == "lambda_monorepo":
            fc = ri.get("function_count", 0)
            ft = ri.get("functions_with_tests", 0)
            print(f"    {fc} functions, {ft} with tests")
        if not ri.get("valid"):
            print(f"    Error: {ri.get('validation_error')}")

    print()
    print("  Press Ctrl+C to stop.")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    server = ThreadedHTTPServer((host, port), WizardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        _job_queue.shutdown()
        server.shutdown()
