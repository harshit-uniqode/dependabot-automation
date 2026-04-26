"""HTTP server — stdlib-only, serves dashboards + REST API.

Routes:
  GET  /                        → Angular dashboard
  GET  /api/config              → repo + server config
  GET  /api/health              → liveness check
  GET  /api/status/<job_id>     → pipeline job status
  POST /api/analyze             → analyse a package upgrade
  POST /api/test-upgrade        → run upgrade pipeline (async)
  POST /api/cancel/<job_id>     → cancel running pipeline
  POST /api/config/repos        → register a new repo

  Lambda local-testing API (see wizard_server/lambda_tester.py):
  GET  /api/lambda/emulator/status
  POST /api/lambda/emulator/{start,stop}
  GET  /api/lambda/list
  GET  /api/lambda/events
  POST /api/lambda/{deploy,invoke}
  GET  /api/lambda/job/<job_id>
"""

import json
import socketserver
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from . import config_schema, repos, jobs as jobs_mod, lambda_tester


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
        # ── Lambda local testing (GET) ────────────────────────
        elif path == "/api/lambda/emulator/status":
            self._json_response(200, lambda_tester.emulator_status())
        elif path == "/api/lambda/list":
            self._json_response(200, {"repos": lambda_tester.list_functions(_repos_info)})
        elif path == "/api/lambda/events":
            self._json_response(200, {"events": lambda_tester.list_events(_project_root)})
        elif path.startswith("/api/lambda/ui-flow/"):
            fn_dir = path.split("/api/lambda/ui-flow/")[-1]
            # Reject path traversal at the boundary
            if "/" in fn_dir or "\\" in fn_dir or fn_dir.startswith("."):
                self._json_response(400, {"error": "invalid function name"})
                return
            self._handle_get_ui_flow(fn_dir)
        elif path.startswith("/api/lambda/job/"):
            jid = path.split("/api/lambda/job/")[-1]
            job = lambda_tester.get_job(jid)
            if not job:
                self._json_response(404, {"error": "job not found"})
            else:
                self._json_response(200, job)
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
        # ── Lambda local testing (POST) ───────────────────────
        elif path == "/api/lambda/emulator/start":
            flavour = (body or {}).get("flavour", "floci")
            jid = lambda_tester.emulator_start(flavour, str(_project_root))
            self._json_response(202, {"job_id": jid, "status": "starting"})
        elif path == "/api/lambda/emulator/stop":
            jid = lambda_tester.emulator_stop(str(_project_root))
            self._json_response(202, {"job_id": jid, "status": "stopping"})
        elif path == "/api/lambda/deploy":
            self._handle_lambda_deploy(body or {})
        elif path == "/api/lambda/seed":
            self._handle_lambda_seed(body or {})
        elif path == "/api/lambda/invoke":
            self._handle_lambda_invoke(body or {})
        elif path == "/api/lambda/preflight":
            self._handle_lambda_preflight(body or {})
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self._add_cors_headers()
        self.end_headers()

    # ── Dashboard ────────────────────────────────────────────

    def _serve_dashboard(self):
        dashboard = _project_root / "vulnerability-dashboards" / "angular-dashboard.html"
        if not dashboard.exists():
            self._json_response(404, {
                "error": "Dashboard not found. Run: ./scripts/refresh-angular-dashboard.sh"
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

    # ── API: UI Flow ─────────────────────────────────────────

    def _handle_get_ui_flow(self, fn_dir):
        """Serve probable UI flow for a Lambda from config/lambda-ui-flows.json."""
        flows_file = _project_root / "config" / "lambda-ui-flows.json"
        if not flows_file.is_file():
            self._json_response(404, {"error": "lambda-ui-flows.json not found"})
            return
        try:
            data = json.loads(flows_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            self._json_response(500, {"error": f"failed to read flows: {e}"})
            return
        flow = data.get(fn_dir)
        if not flow:
            self._json_response(404, {
                "error": f"No UI flow documented for '{fn_dir}'. Run: make sync-ui-flows"
            })
            return
        self._json_response(200, {"function": fn_dir, "flow": flow})

    # ── API: Status ──────────────────────────────────────────

    def _handle_get_status(self, job_id):
        status = _job_queue.get_status(job_id)
        if not status:
            self._json_response(404, {
                "error": "Job not found. It may have expired (kept for 1 hour)."
            })
            return
        self._json_response(200, status)

    # ── API: Lambda local test ───────────────────────────────

    def _handle_lambda_deploy(self, body):
        repo_name = body.get("repo")
        fn_name = body.get("function")
        if not repo_name or not fn_name:
            self._json_response(400, {"error": "repo and function required"})
            return
        fn_meta = lambda_tester.find_function(_repos_info, repo_name, fn_name)
        if not fn_meta:
            self._json_response(404, {"error": f"function not found: {repo_name}/{fn_name}"})
            return
        jid = lambda_tester.deploy(fn_meta, str(_project_root))
        self._json_response(202, {"job_id": jid, "status": "deploying"})

    def _handle_lambda_seed(self, body):
        repo_name = body.get("repo")
        fn_name = body.get("function")
        if not repo_name or not fn_name:
            self._json_response(400, {"error": "repo and function required"})
            return
        fn_meta = lambda_tester.find_function(_repos_info, repo_name, fn_name)
        if not fn_meta:
            self._json_response(404, {"error": f"function not found: {repo_name}/{fn_name}"})
            return
        jid = lambda_tester.seed(fn_meta, str(_project_root))
        self._json_response(202, {"job_id": jid, "status": "seeding"})

    def _handle_lambda_invoke(self, body):
        repo_name = body.get("repo")
        fn_name = body.get("function")
        event_file = body.get("event")
        if not repo_name or not fn_name or not event_file:
            self._json_response(400, {"error": "repo, function, event required"})
            return
        # Reject path-traversal and absolute paths at the boundary
        if not isinstance(event_file, str) or "/" in event_file or "\\" in event_file or event_file.startswith("."):
            self._json_response(400, {"error": "invalid event filename"})
            return
        fn_meta = lambda_tester.find_function(_repos_info, repo_name, fn_name)
        if not fn_meta:
            self._json_response(404, {"error": f"function not found: {repo_name}/{fn_name}"})
            return
        jid = lambda_tester.invoke(fn_meta, event_file, str(_project_root))
        self._json_response(202, {"job_id": jid, "status": "invoking"})

    def _handle_lambda_preflight(self, body):
        repo_name = body.get("repo")
        fn_name = body.get("function")
        event_file = body.get("event")
        if not repo_name or not fn_name:
            self._json_response(400, {"error": "repo and function required"})
            return
        if event_file and (not isinstance(event_file, str) or "/" in event_file or "\\" in event_file or event_file.startswith(".")):
            self._json_response(400, {"error": "invalid event filename"})
            return
        fn_meta = lambda_tester.find_function(_repos_info, repo_name, fn_name)
        if not fn_meta:
            self._json_response(404, {"error": f"function not found: {repo_name}/{fn_name}"})
            return
        jid = lambda_tester.preflight(fn_meta, event_file or "", str(_project_root))
        self._json_response(202, {"job_id": jid, "status": "preflight"})

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
        """Suppress default access logs for cleaner output. Only log 4xx/5xx."""
        if args:
            code = str(args[0])
            if code.startswith("4") or code.startswith("5"):
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
