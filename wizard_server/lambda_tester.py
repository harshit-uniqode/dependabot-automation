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

import base64
import json
import os
import platform
import re
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
    """List JSON test events in lambda-test-events/."""
    p = Path(project_root) / "lambda-test-events"
    if not p.is_dir():
        return []
    return sorted([f.name for f in p.glob("*.json")])


# ─────────────────────────────────────────────────────────────
# BUILD + ZIP
# ─────────────────────────────────────────────────────────────

_ZIP_SKIP_DIRS = {
    ".git", ".idea", ".vscode", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".serverless", ".localtest_pkg",
    "tests", "test", "coverage", ".nyc_output", ".DS_Store",
}
_ZIP_SKIP_FILE_SUFFIXES = {".zip", ".log", ".pyc", ".pyo", ".swp"}


def _zip_dir(src_dir, zip_path, extra_dirs=None, jid=None):
    """Create a zip of src_dir at zip_path. Merges extra_dirs on top.

    CONTRACT: output zip_path MUST be excluded from the walk — critical when
    zip_path is inside src_dir (we zip the fn dir, and zip output is typically
    fn_dir/function.zip). Without this skip, the growing zip gets included in
    itself → 100GB+ runaway zips (happened with pdf_compressor on 2026-04-24).
    Also skip node_modules unless explicitly passed as extra_dir (so large
    TypeScript repos don't double-zip their deps).
    """
    src = Path(src_dir).resolve()
    zip_abs = Path(zip_path).resolve()

    extra_abs_set = {Path(e).resolve() for e in (extra_dirs or [])}

    def _should_skip_dir(path: Path) -> bool:
        if path.name in _ZIP_SKIP_DIRS:
            return True
        # Skip node_modules in main walk unless it's been passed as an extra_dir
        if path.name == "node_modules" and path.resolve() not in extra_abs_set:
            return True
        return False

    files_written = 0
    last_progress = time.time()

    def _progress_tick(current_path):
        nonlocal last_progress
        now = time.time()
        if jid and (now - last_progress) >= 2.0:
            size_mb = zip_abs.stat().st_size / (1024 * 1024) if zip_abs.exists() else 0
            _log(jid, f"  [zip] {files_written} files written, {size_mb:.1f} MB so far (current: {current_path.name})")
            last_progress = now

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if not _should_skip_dir(Path(root) / d)]
            for fname in files:
                abs_path = Path(root) / fname
                if abs_path.resolve() == zip_abs:
                    continue
                if abs_path.suffix in _ZIP_SKIP_FILE_SUFFIXES:
                    continue
                rel = abs_path.relative_to(src)
                zf.write(abs_path, rel.as_posix())
                files_written += 1
                _progress_tick(abs_path)
        for extra in extra_dirs or []:
            ex = Path(extra)
            if not ex.exists():
                continue
            base = ex.parent
            for root, dirs, files in os.walk(ex):
                dirs[:] = [d for d in dirs if d not in _ZIP_SKIP_DIRS]
                for fname in files:
                    abs_path = Path(root) / fname
                    if abs_path.resolve() == zip_abs:
                        continue
                    if abs_path.suffix in _ZIP_SKIP_FILE_SUFFIXES:
                        continue
                    rel = abs_path.relative_to(base)
                    zf.write(abs_path, rel.as_posix())
                    files_written += 1
                    _progress_tick(abs_path)

    if jid:
        final_size_mb = zip_abs.stat().st_size / (1024 * 1024) if zip_abs.exists() else 0
        _log(jid, f"  [zip] ✓ wrote {files_written} files, final size {final_size_mb:.1f} MB")


def _build_node(fn_dir, jid):
    """npm install + compile TypeScript if needed.

    Compile priority (first match wins):
      1. `npm run compile` if that script exists
      2. `npm run build` if that script exists
      3. `npx tsc` if tsconfig.json exists (even without a script)

    Reason for #3: some Lambdas (e.g. pdf_compressor) have a tsconfig with
    `outDir: dist` but no npm script. Real prod deploy uses serverless + TS
    plugin which auto-compiles. We replicate that here so handler `dist/index.X`
    actually resolves.
    """
    rc, _, _ = _run(["npm", "install", "--no-audit", "--no-fund"], cwd=fn_dir, timeout=300, jid=jid)
    if rc != 0:
        return False, "npm install failed"

    pkg = Path(fn_dir) / "package.json"
    compiled = False
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            scripts = data.get("scripts", {})
            if "compile" in scripts:
                rc, _, _ = _run(["npm", "run", "compile"], cwd=fn_dir, timeout=300, jid=jid)
                if rc != 0:
                    return False, "npm run compile failed"
                compiled = True
            elif "build" in scripts:
                rc, _, _ = _run(["npm", "run", "build"], cwd=fn_dir, timeout=300, jid=jid)
                if rc != 0:
                    return False, "npm run build failed"
                compiled = True
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: if we have tsconfig.json but no compile script ran, try npx tsc.
    if not compiled and (Path(fn_dir) / "tsconfig.json").exists():
        _log(jid, "tsconfig.json present but no compile/build script — running `npx tsc`")
        rc, _, _ = _run(["npx", "tsc"], cwd=fn_dir, timeout=300, jid=jid)
        if rc != 0:
            return False, "npx tsc failed"

    return True, "ok"


def _build_python(fn_dir, jid, runtime="python3.9"):
    """pip install -r requirements.txt -t pkg/ INSIDE Lambda Linux container.

    CONTRACT: Host pip install is broken for packages with native extensions
    (cryptography, pydantic-core, orjson, etc.) — they ship platform-specific
    .so files. Host-arch wheels won't load in the Lambda container. We must
    build deps using the same arch as the Lambda runtime container — and
    Floci runs Lambda containers matching the HOST arch (no cross-arch
    emulation by default). So the build platform must match host arch.
    This mirrors `serverless-python-requirements` `dockerizePip: true`.
    """
    pkg_dir = Path(fn_dir) / ".localtest_pkg"
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir, ignore_errors=True)
    pkg_dir.mkdir()

    req = Path(fn_dir) / "requirements.txt"
    if req.exists():
        # Map runtime (e.g. "python3.9") to Lambda base image
        py_ver = runtime.replace("python", "") if runtime.startswith("python") else "3.9"
        image = f"public.ecr.aws/lambda/python:{py_ver}"
        # Detect host arch — Floci Lambda containers run host-native arch
        host_arch = platform.machine().lower()
        docker_platform = "linux/arm64" if host_arch in ("arm64", "aarch64") else "linux/amd64"
        _log(jid, f"Building deps in {image} ({docker_platform}) — matches Floci Lambda container arch")
        rc, _, _ = _run(
            ["docker", "run", "--rm", "--platform", docker_platform,
             "--entrypoint", "/bin/sh",
             "-v", f"{Path(fn_dir).resolve()}:/var/task",
             image, "-c",
             f"pip install -r /var/task/requirements.txt -t /var/task/.localtest_pkg --quiet"],
            cwd=fn_dir, timeout=600, jid=jid,
        )
        if rc != 0:
            return False, "docker pip install failed"

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


def build_zip(fn_dir, language, jid, runtime="python3.9"):
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
        _zip_dir(src_root, zip_path, extra_dirs=extras, jid=jid)
        # Handler path adjustment: if we zipped dist/ contents at root,
        # strip leading "dist/" from handler so it resolves.
        if dist.exists() and src_root == dist:
            return True, {"zip": str(zip_path), "handler_prefix_strip": "dist/"}
        return True, {"zip": str(zip_path)}

    if language == "python":
        ok, pkg_dir = _build_python(fn_dir, jid, runtime=runtime)
        if not ok:
            return False, pkg_dir
        _log(jid, f"Zipping {pkg_dir} → {zip_path}")
        _zip_dir(pkg_dir, zip_path, jid=jid)
        return True, {"zip": str(zip_path)}

    return False, f"unsupported language: {language}"


# ─────────────────────────────────────────────────────────────
# DEPLOY + INVOKE
# ─────────────────────────────────────────────────────────────

def _load_env_vars(project_root, fn_name):
    """Load config/lambda-env-vars.json and return merged env dict.

    Merge order (later wins):
      1. Parameters block  — global defaults for all functions
      2. <fn_name> block   — per-function overrides keyed by Lambda dir name
      3. Hardcoded infra   — AWS creds/endpoint/stage always applied last

    fn_name is the raw directory name (e.g. "beaconstac_pdf_compressor").
    """
    env_file = Path(project_root) / "config" / "lambda-env-vars.json"
    merged = {}
    if env_file.exists():
        try:
            data = json.loads(env_file.read_text())
            merged.update(data.get("Parameters", {}))
            merged.update(data.get(fn_name, {}))
        except (json.JSONDecodeError, OSError):
            pass
    # Infra vars always win — Lambda must reach Floci endpoint
    merged.update({
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_ENDPOINT_URL": "http://floci:4566",
        "STAGE": "local",
        "NODE_OPTIONS": "--no-deprecation",
    })
    return merged


def _env_vars_str(env_dict):
    """Serialize env dict to AWS CLI --environment JSON shorthand.

    Use JSON form (not Variables=K=V,...) so values containing `,`, `=`,
    `{`, `}` or whitespace round-trip safely.
    """
    return json.dumps({"Variables": {str(k): str(v) for k, v in env_dict.items()}})


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

        _log(jid, f"Building {fn_meta['name']} ({language}, runtime={runtime})...")
        ok, result = build_zip(fn_dir, language, jid, runtime=runtime)
        if not ok:
            _finish(jid, "error", {"error": result})
            return

        zip_path = result["zip"]
        prefix_strip = result.get("handler_prefix_strip")
        if prefix_strip and handler.startswith(prefix_strip):
            original_handler = handler
            handler = handler[len(prefix_strip):]
            _log(jid, f"Handler adjusted: {original_handler} → {handler} (zip root is {prefix_strip.rstrip('/')}/)")

        # ── Upload path decision: inline vs S3 ──────────────────────
        # AWS Lambda CreateFunction has a 50MB limit on inline zip upload
        # (raw bytes via --zip-file), and Floci reports a 100MB JSON string
        # limit for the base64-encoded body. For large zips we upload to
        # Floci's S3 first and reference via Code.S3Bucket/S3Key.
        # Threshold: 50MB raw zip → safe headroom below all limits.
        zip_size = os.path.getsize(zip_path)
        zip_size_mb = zip_size / (1024 * 1024)
        USE_S3_THRESHOLD = 50 * 1024 * 1024  # 50 MB

        code_ref = None  # either ("inline",) or ("s3", bucket, key)
        if zip_size > USE_S3_THRESHOLD:
            s3_bucket = "floci-lambda-code"
            s3_key = f"{fn_name}-{int(time.time())}.zip"
            _log(jid, f"Zip is {zip_size_mb:.1f} MB — using S3 upload path (bucket={s3_bucket}, key={s3_key})")

            # Create bucket (idempotent — ignore BucketAlreadyOwnedByYou)
            _run(cli + ["s3api", "create-bucket", "--bucket", s3_bucket],
                 timeout=30, jid=jid)

            # Upload zip
            rc, _, stderr = _run(
                cli + ["s3", "cp", zip_path, f"s3://{s3_bucket}/{s3_key}"],
                timeout=600, jid=jid,
            )
            if rc != 0:
                _finish(jid, "error", {"error": f"s3 upload failed: {stderr[:500]}"})
                return
            code_ref = ("s3", s3_bucket, s3_key)
        else:
            _log(jid, f"Zip is {zip_size_mb:.1f} MB — using inline upload")
            code_ref = ("inline",)

        env_dict = _load_env_vars(project_root, fn_meta["name"])
        env_vars_str = _env_vars_str(env_dict)
        _log(jid, f"Env vars: {len(env_dict)} keys loaded")
        _log(jid, f"Deploying {fn_name} (runtime={runtime}, handler={handler})...")

        # Try update first; fallback to create
        if code_ref[0] == "s3":
            update_args = ["--s3-bucket", code_ref[1], "--s3-key", code_ref[2]]
        else:
            update_args = ["--zip-file", f"fileb://{zip_path}"]

        rc, _, stderr = _run(
            cli + ["lambda", "update-function-code",
                   "--function-name", fn_name] + update_args,
            timeout=120, jid=jid,
        )
        if rc == 0:
            # Function existed — also refresh env vars so any changes in
            # lambda-env-vars.json take effect (update-function-code does not touch env)
            _log(jid, "Function updated — refreshing env vars...")
            rc2, _, stderr2 = _run(
                cli + ["lambda", "update-function-configuration",
                       "--function-name", fn_name,
                       "--environment", env_vars_str],
                timeout=60, jid=jid,
            )
            if rc2 != 0:
                _finish(jid, "error", {"error": f"update-function-configuration failed: {stderr2[:500]}"})
                return
        else:
            _log(jid, "Function does not exist yet, creating...")
            # Code arg differs by path
            if code_ref[0] == "s3":
                code_args = ["--code", f"S3Bucket={code_ref[1]},S3Key={code_ref[2]}"]
            else:
                code_args = ["--zip-file", f"fileb://{zip_path}"]

            rc, _, stderr = _run(
                cli + ["lambda", "create-function",
                       "--function-name", fn_name,
                       "--runtime", runtime,
                       "--handler", handler,
                       "--role", "arn:aws:iam::000000000000:role/lambda-role"] +
                code_args +
                      ["--timeout", str(fn_meta.get("timeout", 60)),
                       "--memory-size", str(fn_meta.get("memory", 256)),
                       "--environment", env_vars_str],
                timeout=300, jid=jid,
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


# ─────────────────────────────────────────────────────────────
# SEED — auto-create AWS resources the Lambda expects
# ─────────────────────────────────────────────────────────────

def _detect_resources(fn_meta):
    """Parse serverless.yml + handler source to infer AWS resources needed.

    Returns dict with keys:
      s3_buckets   — list of bucket names
      sqs_queues   — list of queue names
      ddb_tables   — list of table names
      ses_identities — list of email addresses to verify
    """
    resources = {"s3_buckets": set(), "sqs_queues": set(), "ddb_tables": set(), "ses_identities": set()}
    fn_dir = Path(fn_meta["path"])

    # 1. serverless.yml parse
    sls_yml = fn_dir / "serverless.yml"
    if sls_yml.exists():
        try:
            txt = sls_yml.read_text()
            # Extract env vars like S3_BUCKET: <value>
            for m in re.finditer(r"(\w+_BUCKET|S3_BUCKET|BUCKET_NAME):\s*([^\s#]+)", txt):
                val = m.group(2).strip()
                # Skip template vars like ${param:s3Bucket}
                if not val.startswith("$"):
                    resources["s3_buckets"].add(val)
            # SQS queue resource refs: QueueName: foo
            for m in re.finditer(r"QueueName:\s*([^\s#]+)", txt):
                val = m.group(1).strip()
                if not val.startswith("$"):
                    resources["sqs_queues"].add(val)
            # DDB table resource refs: TableName: foo
            for m in re.finditer(r"TableName:\s*([^\s#]+)", txt):
                val = m.group(1).strip()
                if not val.startswith("$"):
                    resources["ddb_tables"].add(val)
        except OSError:
            pass

    # 2. Handler source scan — look for hardcoded defaults in code
    #    Pattern: process.env.X_BUCKET ? ... : 'literal-bucket-name'
    for src_dir in [fn_dir / "src", fn_dir]:
        if not src_dir.is_dir():
            continue
        for f in src_dir.rglob("*.ts"):
            if "node_modules" in str(f):
                continue
            try:
                txt = f.read_text(errors="ignore")
                # BUCKET fallback literals: 'beaconstac-content-debug'
                for m in re.finditer(r"process\.env\.\w*BUCKET\w*\s*\?\s*[^:]+:\s*['\"]([^'\"]+)['\"]", txt):
                    resources["s3_buckets"].add(m.group(1))
                # Direct S3 bucket literals passed to .getObject({ Bucket: 'x' }...)
                for m in re.finditer(r"Bucket:\s*['\"]([^'\"${}]+)['\"]", txt):
                    resources["s3_buckets"].add(m.group(1))
                # Direct ses sender identity: ses.sendEmail({ Source: 'a@b.com' })
                for m in re.finditer(r"(?:Source|From):\s*['\"]([^'\"]+@[^'\"]+)['\"]", txt):
                    resources["ses_identities"].add(m.group(1))
            except (UnicodeDecodeError, OSError):
                pass
        for f in src_dir.rglob("*.py"):
            try:
                txt = f.read_text(errors="ignore")
                for m in re.finditer(r"os\.environ\.get\(['\"]\w*BUCKET\w*['\"],\s*['\"]([^'\"]+)['\"]\)", txt):
                    resources["s3_buckets"].add(m.group(1))
                for m in re.finditer(r"Bucket=['\"]([^'\"${}]+)['\"]", txt):
                    resources["s3_buckets"].add(m.group(1))
            except (UnicodeDecodeError, OSError):
                pass

    # Convert sets to sorted lists for deterministic output
    return {k: sorted(v) for k, v in resources.items()}


def seed(fn_meta, project_root):
    """Create AWS resources detected from the Lambda's serverless.yml + source.

    Idempotent: create-bucket / create-queue return errors on duplicates, we ignore.
    Also uploads any fixtures found at <fn_dir>/tests/fixtures/* to the first
    detected S3 bucket under the same filename.
    """
    jid = _new_job()

    def _task():
        cli = _awslocal_or_aws()
        if not cli:
            _finish(jid, "error", {"error": "Neither awslocal nor aws CLI found."})
            return

        resources = _detect_resources(fn_meta)
        _log(jid, f"Detected resources: {json.dumps(resources)}")

        created = {"s3_buckets": [], "sqs_queues": [], "ddb_tables": [], "ses_identities": [], "fixtures_uploaded": []}

        # S3 buckets
        for bucket in resources["s3_buckets"]:
            _log(jid, f"Creating S3 bucket: {bucket}")
            rc, _, stderr = _run(
                cli + ["s3api", "create-bucket", "--bucket", bucket],
                timeout=30, jid=jid,
            )
            if rc == 0 or "BucketAlreadyOwnedByYou" in stderr:
                created["s3_buckets"].append(bucket)

        # SQS queues
        for queue in resources["sqs_queues"]:
            _log(jid, f"Creating SQS queue: {queue}")
            rc, _, stderr = _run(
                cli + ["sqs", "create-queue", "--queue-name", queue],
                timeout=30, jid=jid,
            )
            if rc == 0 or "QueueAlreadyExists" in stderr:
                created["sqs_queues"].append(queue)

        # DDB tables — minimal schema (primary key=id)
        for table in resources["ddb_tables"]:
            _log(jid, f"Creating DDB table: {table} (schema=id:S)")
            rc, _, stderr = _run(
                cli + ["dynamodb", "create-table",
                       "--table-name", table,
                       "--attribute-definitions", "AttributeName=id,AttributeType=S",
                       "--key-schema", "AttributeName=id,KeyType=HASH",
                       "--billing-mode", "PAY_PER_REQUEST"],
                timeout=30, jid=jid,
            )
            if rc == 0 or "ResourceInUseException" in stderr:
                created["ddb_tables"].append(table)

        # SES identities
        for email in resources["ses_identities"]:
            _log(jid, f"Verifying SES identity: {email}")
            rc, _, _ = _run(
                cli + ["ses", "verify-email-identity", "--email-address", email],
                timeout=30, jid=jid,
            )
            if rc == 0:
                created["ses_identities"].append(email)

        # Upload fixtures (if any) to first detected S3 bucket
        fn_dir = Path(fn_meta["path"])
        fixtures_dir = fn_dir / "tests" / "fixtures"
        if fixtures_dir.is_dir() and created["s3_buckets"]:
            target_bucket = created["s3_buckets"][0]
            for f in fixtures_dir.iterdir():
                if not f.is_file():
                    continue
                _log(jid, f"Uploading fixture: {f.name} → s3://{target_bucket}/{f.name}")
                rc, _, _ = _run(
                    cli + ["s3", "cp", str(f), f"s3://{target_bucket}/{f.name}"],
                    timeout=60, jid=jid,
                )
                if rc == 0:
                    created["fixtures_uploaded"].append(f"s3://{target_bucket}/{f.name}")

        _finish(jid, "done", {"created": created})

    threading.Thread(target=_task, daemon=True).start()
    return jid


def invoke(fn_meta, event_file, project_root):
    """Invoke deployed lambda with event JSON. Returns job_id (async)."""
    jid = _new_job()

    def _task():
        fn_name = "local-" + fn_meta["name"].replace("_", "-").lower()
        events_dir = (Path(project_root) / "lambda-test-events").resolve()
        try:
            event_path = (events_dir / event_file).resolve()
        except (OSError, ValueError):
            _finish(jid, "error", {"error": f"Invalid event file: {event_file}"})
            return
        if event_path.parent != events_dir or not event_path.is_file():
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
            log_b64 = invoke_meta.get("LogResult")
            if log_b64:
                try:
                    result["log_tail"] = base64.b64decode(log_b64).decode("utf-8", errors="replace")
                except (ValueError, UnicodeDecodeError):
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
    """Look up a Lambda function by its on-disk directory name.

    CONTRACT: `function_name` is the literal directory name as it exists on disk
    (e.g. `beaconstac_pdf_compressor` OR `forms-response-sync-service`).
    Directory naming is inconsistent — some use underscores, some hyphens.

    Do NOT translate hyphens ↔ underscores in callers. Pass the dir name verbatim.
    The Lambda runtime name (`local-<hyphenated>`) is derived here in deploy/invoke
    via `.replace("_", "-").lower()` — that conversion is wizard-internal only.
    """
    for ri in repos_info:
        if ri["name"] != repo_name:
            continue
        fn = ri.get("functions", {}).get(function_name)
        if not fn:
            return None
        return {"name": function_name, **fn}
    return None
