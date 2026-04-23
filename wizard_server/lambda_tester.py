"""Local Lambda testing — emulator lifecycle, build + deploy to Floci/LocalStack, invoke.

Endpoints expected by lambda-dashboard.html:
  GET  /api/lambda/emulator/status     → {"running": bool, "endpoint": str}
  POST /api/lambda/emulator/start      → {"status":"starting","logs":[...]} (async)
  POST /api/lambda/emulator/stop       → {"status":"stopped"}
  GET  /api/lambda/list                → {"repos":[{"name":..,"functions":[...]}]}
  GET  /api/lambda/events              → {"events":["sqs-event.json",...]}
  POST /api/lambda/deploy              → {"job_id":..} (async, poll job status)
  POST /api/lambda/invoke              → {"job_id":..} (async)
  GET  /api/lambda/job/<id>            → {"status":"done|running|error","logs":[...],"result":{...}}
"""

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path

EMULATOR_ENDPOINT = "http://localhost:4566"
_jobs = {}  # job_id → { status, logs, result, started_at, finished_at }
_jobs_lock = threading.Lock()
_JOB_TTL_SECONDS = 3600


# ─────────────────────────────────────────────────────────────
# JOB TRACKING
# ─────────────────────────────────────────────────────────────

def _new_job():
    jid = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[jid] = {
            "status": "running",
            "logs": [],
            "result": None,
            "started_at": time.time(),
            "finished_at": None,
        }
    return jid


def _log(jid, line):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]["logs"].append(line)


def _finish(jid, status, result=None):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]["status"] = status
            _jobs[jid]["result"] = result
            _jobs[jid]["finished_at"] = time.time()


def _gc_jobs():
    cutoff = time.time() - _JOB_TTL_SECONDS
    with _jobs_lock:
        stale = [k for k, v in _jobs.items()
                 if v.get("finished_at") and v["finished_at"] < cutoff]
        for k in stale:
            _jobs.pop(k, None)


def get_job(jid):
    _gc_jobs()
    with _jobs_lock:
        job = _jobs.get(jid)
        if not job:
            return None
        return dict(job)


# ─────────────────────────────────────────────────────────────
# SHELL HELPERS
# ─────────────────────────────────────────────────────────────

def _run(cmd, cwd=None, env=None, timeout=120, jid=None):
    """Run a shell command, stream lines to job log if jid given. Return (rc, stdout, stderr)."""
    if jid:
        _log(jid, f"$ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=isinstance(cmd, str),
        )
        if jid:
            for ln in (proc.stdout or "").splitlines():
                _log(jid, ln)
            for ln in (proc.stderr or "").splitlines():
                _log(jid, f"[stderr] {ln}")
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        if jid:
            _log(jid, f"[error] timeout after {timeout}s")
        return 124, "", str(e)
    except FileNotFoundError as e:
        if jid:
            _log(jid, f"[error] command not found: {e}")
        return 127, "", str(e)


def _tool_available(name):
    return shutil.which(name) is not None


# ─────────────────────────────────────────────────────────────
# EMULATOR
# ─────────────────────────────────────────────────────────────

def emulator_status():
    """Check if Floci/LocalStack is reachable at :4566."""
    for path in ("/_floci/health", "/_localstack/health"):
        rc, out, _ = _run(
            ["curl", "-sf", f"{EMULATOR_ENDPOINT}{path}"],
            timeout=3,
        )
        if rc == 0:
            flavour = "floci" if "floci" in path else "localstack"
            return {"running": True, "endpoint": EMULATOR_ENDPOINT, "flavour": flavour}
    return {"running": False, "endpoint": EMULATOR_ENDPOINT, "flavour": None}


def emulator_start(flavour, project_root):
    """Start emulator via `make emulator-up` (Floci) or `make emulator-up-localstack`.
    Returns job_id. Runs async."""
    jid = _new_job()
    target = "emulator-up-localstack" if flavour == "localstack" else "emulator-up"

    def _task():
        _log(jid, f"Starting emulator ({flavour})...")
        env = {}
        if flavour == "localstack" and not os.environ.get("LOCALSTACK_AUTH_TOKEN"):
            _log(jid, "[warn] LOCALSTACK_AUTH_TOKEN not set — Community tier only")
        rc, _, _ = _run(["make", target], cwd=project_root, timeout=180, jid=jid)
        if rc != 0:
            _finish(jid, "error", {"error": f"make {target} failed (rc={rc})"})
            return
        _log(jid, "Emulator started.")
        _finish(jid, "done", {"flavour": flavour})

    threading.Thread(target=_task, daemon=True).start()
    return jid


def emulator_stop(project_root):
    jid = _new_job()

    def _task():
        _log(jid, "Stopping emulator...")
        rc, _, _ = _run(["make", "emulator-down"], cwd=project_root, timeout=60, jid=jid)
        _finish(jid, "done" if rc == 0 else "error", {"rc": rc})

    threading.Thread(target=_task, daemon=True).start()
    return jid


# ─────────────────────────────────────────────────────────────
# LAMBDA DISCOVERY
# ─────────────────────────────────────────────────────────────

def list_functions(repos_info):
    """Return flat list of deployable functions from all lambda_monorepo repos."""
    repos = []
    for ri in repos_info:
        if ri.get("type") != "lambda_monorepo" or not ri.get("valid"):
            continue
        fns = []
        for name, meta in ri.get("functions", {}).items():
            fns.append({
                "name": name,
                "path": meta.get("path"),
                "runtime": meta.get("runtime"),
                "language": meta.get("language"),
                "handler": meta.get("handler"),
                "trigger_type": meta.get("trigger_type"),
                "services": meta.get("services", []),
                "timeout": meta.get("timeout", 30),
                "memory": meta.get("memory", 128),
            })
        repos.append({
            "name": ri["name"],
            "path": ri["path"],
            "functions": sorted(fns, key=lambda f: f["name"]),
        })
    return repos


def list_events(project_root):
    """List JSON test events in test-events/."""
    p = Path(project_root) / "test-events"
    if not p.is_dir():
        return []
    return sorted([f.name for f in p.glob("*.json")])


# ─────────────────────────────────────────────────────────────
# BUILD + ZIP
# ─────────────────────────────────────────────────────────────

def _zip_dir(src_dir, zip_path, extra_dirs=None):
    """Create a zip of src_dir at zip_path. Merges extra_dirs on top."""
    src = Path(src_dir)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src):
            for fname in files:
                abs_path = Path(root) / fname
                rel = abs_path.relative_to(src)
                zf.write(abs_path, rel.as_posix())
        for extra in extra_dirs or []:
            ex = Path(extra)
            if not ex.exists():
                continue
            base = ex.parent
            for root, _, files in os.walk(ex):
                for fname in files:
                    abs_path = Path(root) / fname
                    rel = abs_path.relative_to(base)
                    zf.write(abs_path, rel.as_posix())


def _build_node(fn_dir, jid):
    """npm install + compile if tsc config / compile script present."""
    rc, _, _ = _run(["npm", "install", "--no-audit", "--no-fund"], cwd=fn_dir, timeout=300, jid=jid)
    if rc != 0:
        return False, "npm install failed"

    pkg = Path(fn_dir) / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            scripts = data.get("scripts", {})
            if "compile" in scripts:
                rc, _, _ = _run(["npm", "run", "compile"], cwd=fn_dir, timeout=180, jid=jid)
                if rc != 0:
                    return False, "npm run compile failed"
            elif "build" in scripts:
                rc, _, _ = _run(["npm", "run", "build"], cwd=fn_dir, timeout=180, jid=jid)
                if rc != 0:
                    return False, "npm run build failed"
        except (json.JSONDecodeError, OSError):
            pass
    return True, "ok"


def _build_python(fn_dir, jid):
    """pip install -r requirements.txt -t pkg/."""
    pkg_dir = Path(fn_dir) / ".localtest_pkg"
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir, ignore_errors=True)
    pkg_dir.mkdir()

    req = Path(fn_dir) / "requirements.txt"
    if req.exists():
        rc, _, _ = _run(
            ["pip3", "install", "-r", str(req), "-t", str(pkg_dir), "--quiet"],
            cwd=fn_dir, timeout=300, jid=jid,
        )
        if rc != 0:
            return False, "pip install failed"

    # Copy handler source files into pkg dir
    for item in Path(fn_dir).iterdir():
        if item.name in (".localtest_pkg", ".git", "tests", "__pycache__"):
            continue
        if item.is_file() and item.suffix == ".py":
            shutil.copy2(item, pkg_dir / item.name)
        elif item.is_dir() and not item.name.startswith("."):
            # Copy nested module dirs (e.g. src/)
            target = pkg_dir / item.name
            if target.exists():
                continue
            shutil.copytree(item, target, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "tests", "test_*", ".localtest_pkg"
            ))
    return True, str(pkg_dir)


def build_zip(fn_dir, language, jid):
    """Build artifact and produce function.zip in fn_dir. Return (ok, zip_path_or_error)."""
    fn_dir = Path(fn_dir)
    zip_path = fn_dir / "function.zip"
    if zip_path.exists():
        zip_path.unlink()

    if language == "nodejs":
        ok, msg = _build_node(fn_dir, jid)
        if not ok:
            return False, msg
        # Prefer dist/ if present (TypeScript compile output)
        dist = fn_dir / "dist"
        src_root = dist if dist.exists() else fn_dir
        extras = []
        nm = fn_dir / "node_modules"
        if nm.exists():
            extras.append(str(nm))
        _log(jid, f"Zipping {src_root} (+node_modules) → {zip_path}")
        _zip_dir(src_root, zip_path, extra_dirs=extras)
        # Handler path adjustment: if we zipped dist/ contents at root,
        # strip leading "dist/" from handler so it resolves.
        if dist.exists() and src_root == dist:
            return True, {"zip": str(zip_path), "handler_prefix_strip": "dist/"}
        return True, {"zip": str(zip_path)}

    if language == "python":
        ok, pkg_dir = _build_python(fn_dir, jid)
        if not ok:
            return False, pkg_dir
        _log(jid, f"Zipping {pkg_dir} → {zip_path}")
        _zip_dir(pkg_dir, zip_path)
        return True, {"zip": str(zip_path)}

    return False, f"unsupported language: {language}"


# ─────────────────────────────────────────────────────────────
# DEPLOY + INVOKE
# ─────────────────────────────────────────────────────────────

def _awslocal_or_aws():
    """Prefer awslocal; fallback to aws with --endpoint-url."""
    if _tool_available("awslocal"):
        return ["awslocal"]
    if _tool_available("aws"):
        return ["aws", "--endpoint-url", EMULATOR_ENDPOINT, "--region", "us-east-1",
                "--no-cli-pager"]
    return None


def _runtime_for(language, serverless_runtime):
    """Pick a runtime the emulator will accept."""
    if serverless_runtime and serverless_runtime != "unknown":
        return serverless_runtime
    return "nodejs18.x" if language == "nodejs" else "python3.11"


def deploy(fn_meta, project_root):
    """Build + create-or-update Lambda on emulator. Returns job_id (async)."""
    jid = _new_job()

    def _task():
        fn_name = "local-" + fn_meta["name"].replace("_", "-").lower()
        fn_dir = fn_meta["path"]
        language = fn_meta.get("language", "nodejs")
        handler = fn_meta.get("handler") or ("index.handler" if language == "nodejs" else "handler.lambda_handler")
        runtime = _runtime_for(language, fn_meta.get("runtime"))

        status = emulator_status()
        if not status["running"]:
            _finish(jid, "error", {"error": "Emulator not running. Start it first."})
            return

        cli = _awslocal_or_aws()
        if not cli:
            _finish(jid, "error", {"error": "Neither awslocal nor aws CLI found. Install with: pip install awscli-local"})
            return

        _log(jid, f"Building {fn_meta['name']} ({language})...")
        ok, result = build_zip(fn_dir, language, jid)
        if not ok:
            _finish(jid, "error", {"error": result})
            return

        zip_path = result["zip"]
        prefix_strip = result.get("handler_prefix_strip")
        if prefix_strip and handler.startswith(prefix_strip):
            original_handler = handler
            handler = handler[len(prefix_strip):]
            _log(jid, f"Handler adjusted: {original_handler} → {handler} (zip root is {prefix_strip.rstrip('/')}/)")

        _log(jid, f"Deploying {fn_name} (runtime={runtime}, handler={handler})...")

        # Try update first; fallback to create
        rc, _, stderr = _run(
            cli + ["lambda", "update-function-code",
                   "--function-name", fn_name,
                   "--zip-file", f"fileb://{zip_path}"],
            timeout=60, jid=jid,
        )
        if rc != 0:
            _log(jid, "Function does not exist yet, creating...")
            # Pass dummy AWS creds + endpoint so handlers that use aws-sdk
            # can find credentials inside the Lambda container. Floci proxies
            # AWS calls made to http://floci:4566 back to its own services.
            env_vars = (
                "Variables={"
                "AWS_ACCESS_KEY_ID=test,"
                "AWS_SECRET_ACCESS_KEY=test,"
                "AWS_DEFAULT_REGION=us-east-1,"
                "AWS_REGION=us-east-1,"
                "AWS_ENDPOINT_URL=http://floci:4566,"
                "STAGE=local,"
                "NODE_OPTIONS=--no-deprecation"
                "}"
            )
            rc, _, stderr = _run(
                cli + ["lambda", "create-function",
                       "--function-name", fn_name,
                       "--runtime", runtime,
                       "--handler", handler,
                       "--role", "arn:aws:iam::000000000000:role/lambda-role",
                       "--zip-file", f"fileb://{zip_path}",
                       "--timeout", str(fn_meta.get("timeout", 60)),
                       "--memory-size", str(fn_meta.get("memory", 256)),
                       "--environment", env_vars],
                timeout=120, jid=jid,
            )
            if rc != 0:
                _finish(jid, "error", {"error": f"create-function failed: {stderr[:500]}"})
                return

        # Wait for active state
        _log(jid, "Waiting for function to be active...")
        _run(cli + ["lambda", "wait", "function-active-v2",
                    "--function-name", fn_name],
             timeout=60, jid=jid)

        _finish(jid, "done", {
            "function_name": fn_name,
            "runtime": runtime,
            "handler": handler,
            "zip_size_bytes": os.path.getsize(zip_path),
        })

    threading.Thread(target=_task, daemon=True).start()
    return jid


def invoke(fn_meta, event_file, project_root):
    """Invoke deployed lambda with event JSON. Returns job_id (async)."""
    jid = _new_job()

    def _task():
        fn_name = "local-" + fn_meta["name"].replace("_", "-").lower()
        event_path = Path(project_root) / "test-events" / event_file
        if not event_path.exists():
            _finish(jid, "error", {"error": f"Event file not found: {event_file}"})
            return

        cli = _awslocal_or_aws()
        if not cli:
            _finish(jid, "error", {"error": "Neither awslocal nor aws CLI found."})
            return

        out_file = Path(project_root) / ".localtest_out" / f"{jid}.json"
        out_file.parent.mkdir(exist_ok=True)

        _log(jid, f"Invoking {fn_name} with {event_file}...")
        rc, stdout, stderr = _run(
            cli + ["lambda", "invoke",
                   "--function-name", fn_name,
                   "--payload", f"fileb://{event_path}",
                   "--cli-binary-format", "raw-in-base64-out",
                   "--log-type", "Tail",
                   str(out_file)],
            timeout=120, jid=jid,
        )
        if rc != 0:
            _finish(jid, "error", {"error": f"invoke failed: {stderr[:500]}"})
            return

        result = {"function_name": fn_name, "event": event_file}
        try:
            invoke_meta = json.loads(stdout) if stdout.strip() else {}
            result["status_code"] = invoke_meta.get("StatusCode")
            result["function_error"] = invoke_meta.get("FunctionError")
            # Decode base64 log tail
            log_b64 = invoke_meta.get("LogResult")
            if log_b64:
                import base64
                try:
                    result["log_tail"] = base64.b64decode(log_b64).decode("utf-8", errors="replace")
                except Exception:
                    result["log_tail"] = log_b64
        except json.JSONDecodeError:
            pass

        try:
            result["output"] = out_file.read_text()
        except OSError:
            result["output"] = ""

        _finish(jid, "done" if not result.get("function_error") else "error", result)

    threading.Thread(target=_task, daemon=True).start()
    return jid


# ─────────────────────────────────────────────────────────────
# FIND FUNCTION META BY NAME
# ─────────────────────────────────────────────────────────────

def find_function(repos_info, repo_name, function_name):
    for ri in repos_info:
        if ri["name"] != repo_name:
            continue
        fn = ri.get("functions", {}).get(function_name)
        if not fn:
            return None
        return {"name": function_name, **fn}
    return None
