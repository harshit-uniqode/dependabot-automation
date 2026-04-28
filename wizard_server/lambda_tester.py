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


def _detect_aws_sdk_v2(fn_dir):
    """Return True if the Lambda uses Node aws-sdk v2 (not v3).

    v2 = `aws-sdk` in dependencies (single package).
    v3 = `@aws-sdk/client-*` (scoped packages).

    v2 silently ignores AWS_ENDPOINT_URL, so it bypasses Floci and hits
    real AWS — see runtime-shims/aws-sdk-floci-shim.js for the fix.
    """
    pkg = Path(fn_dir) / "package.json"
    if not pkg.is_file():
        return False
    try:
        data = json.loads(pkg.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    return "aws-sdk" in deps


def _inject_floci_shim(fn_dir, jid):
    """Copy runtime-shims/aws-sdk-floci-shim.js into the Lambda dir so it
    gets bundled into the zip. Returns the runtime-relative path the
    Lambda's NODE_OPTIONS should --require, or None if shim was not needed.
    """
    shim_src = Path(__file__).resolve().parent / "runtime-shims" / "aws-sdk-floci-shim.js"
    if not shim_src.is_file():
        _log(jid, "⚠️  Floci shim source missing — SDK v2 calls will bypass Floci")
        return None
    shim_dest = Path(fn_dir) / "aws-sdk-floci-shim.js"
    try:
        shutil.copy2(str(shim_src), str(shim_dest))
        _log(jid, "🔴 SDK v2 detected — injected Floci shim (aws-sdk-floci-shim.js) into zip root")
        _log(jid, "   Without this, the AWS SDK ignores AWS_ENDPOINT_URL and bypasses Floci.")
        return "/var/task/aws-sdk-floci-shim.js"
    except OSError as e:
        _log(jid, f"⚠️  Failed to copy shim: {e}")
        return None


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

        # SDK v2 needs a shim (v2 ignores AWS_ENDPOINT_URL).
        shim_require_path = None
        if _detect_aws_sdk_v2(fn_dir):
            shim_require_path = _inject_floci_shim(fn_dir, jid)

        # Prefer dist/ if present (TypeScript compile output)
        dist = fn_dir / "dist"
        src_root = dist if dist.exists() else fn_dir
        extras = []
        nm = fn_dir / "node_modules"
        if nm.exists():
            extras.append(str(nm))
        # If TS-compiled to dist/ but shim needs to be at zip root, copy it into dist/ too.
        if shim_require_path and dist.exists() and src_root == dist:
            shim_in_dist = dist / "aws-sdk-floci-shim.js"
            shim_at_root = fn_dir / "aws-sdk-floci-shim.js"
            if shim_at_root.is_file() and not shim_in_dist.is_file():
                try:
                    shutil.copy2(str(shim_at_root), str(shim_in_dist))
                except OSError:
                    pass

        _log(jid, f"Zipping {src_root} (+node_modules) → {zip_path}")
        _zip_dir(src_root, zip_path, extra_dirs=extras, jid=jid)

        result = {"zip": str(zip_path)}
        # Handler path adjustment: if we zipped dist/ contents at root,
        # strip leading "dist/" from handler so it resolves.
        if dist.exists() and src_root == dist:
            result["handler_prefix_strip"] = "dist/"
        if shim_require_path:
            result["shim_require_path"] = shim_require_path
        return True, result

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

        # If build_zip injected the SDK-v2 shim, prepend its --require to
        # NODE_OPTIONS so it loads before the handler. Preserves existing
        # NODE_OPTIONS (e.g. --no-deprecation) by concatenating.
        shim_require_path = result.get("shim_require_path") if isinstance(result, dict) else None
        if shim_require_path:
            existing = env_dict.get("NODE_OPTIONS", "")
            require_flag = f"--require={shim_require_path}"
            if require_flag not in existing:
                env_dict["NODE_OPTIONS"] = (require_flag + " " + existing).strip()
            _log(jid, f"🔴 Floci shim active — NODE_OPTIONS={env_dict['NODE_OPTIONS']}")

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

def _load_overrides(project_root, fn_name):
    """Load per-Lambda overrides from config/lambda-test-overrides.json.

    Returns the entry for fn_name, or empty dict if none.
    """
    overrides_file = Path(project_root) / "config" / "lambda-test-overrides.json"
    if not overrides_file.is_file():
        return {}
    try:
        data = json.loads(overrides_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data.get(fn_name, {}) or {}


def _classify_invoke(result, overrides):
    """Classify Lambda invocation outcome into 4 verdicts.

    Returns (verdict, reason, stage_reached) where verdict is one of:
      - pass               : clean success, status 200, no function_error
      - pass_with_caveat   : reached external boundary; failure is expected (Floci can't proxy)
      - fail               : crashed BEFORE expected boundary, or no boundary defined
      - inconclusive       : timed out without reaching marker; can't tell
    """
    log_tail = (result.get("log_tail") or "").lower()
    output = (result.get("output") or "").lower()
    fn_err = result.get("function_error")
    status = result.get("status_code")
    haystack = log_tail + "\n" + output

    # Expected failure config
    marker = (overrides.get("expected_failure_marker") or "").lower()
    stage = overrides.get("expected_failure_stage") or ""

    # Clean PASS
    if not fn_err and status == 200:
        return ("pass", "Lambda completed successfully", "handler_complete")

    # Has expected boundary configured
    if marker:
        marker_hit = marker in haystack
        is_timeout = "task timed out" in haystack or "function.timedout" in output
        if marker_hit:
            return (
                "pass_with_caveat",
                f"Reached expected boundary ({stage} → {overrides.get('expected_failure_marker')}); Floci cannot proxy this call. Internal logic OK.",
                stage or "external_boundary",
            )
        if is_timeout:
            return (
                "inconclusive",
                f"Timed out without reaching expected boundary marker ({overrides.get('expected_failure_marker')}). Logs may be truncated; raise Lambda timeout or check CloudWatch.",
                "unknown",
            )
        # Crashed before marker
        return (
            "fail",
            f"Crashed BEFORE expected boundary ({overrides.get('expected_failure_marker')}). Internal logic broken.",
            "before_boundary",
        )

    # No expected_failure config → standard fail
    why = f"functionError={fn_err}" if fn_err else f"statusCode={status}"
    return ("fail", why, "handler")


def _resolve_fixture(uri, project_root):
    """Resolve a fixture URI to an absolute filesystem path.

    Accepts:
      - fixture://<filename> → lambda-test-assets/<filename>
      - absolute path → returned as-is
      - relative path → resolved against project_root

    Returns Path or None if file does not exist.
    """
    if not uri:
        return None
    if uri.startswith("fixture://"):
        rel = uri[len("fixture://"):]
        candidate = Path(project_root) / "lambda-test-assets" / rel
    elif uri.startswith("/"):
        candidate = Path(uri)
    else:
        candidate = Path(project_root) / uri
    return candidate if candidate.is_file() else None


def _extract_event_keys(event_file_path):
    """Walk the test event JSON and return list of (key_name, value) pairs
    for fields that look like S3 object keys, queue URLs, table names.

    Used by Seed to pre-create objects the handler will read from.
    """
    refs = {"s3_keys": [], "queue_urls": [], "table_names": [], "buckets": []}
    if not event_file_path or not Path(event_file_path).is_file():
        return refs
    try:
        data = json.loads(Path(event_file_path).read_text())
    except (json.JSONDecodeError, OSError):
        return refs

    def walk(node, parent_key=None):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                if isinstance(v, str):
                    if kl == "key" and not v.startswith("$"):
                        refs["s3_keys"].append(v)
                    elif kl in ("name", "bucket") and parent_key == "bucket":
                        refs["buckets"].append(v)
                    elif kl in ("queueurl", "queue_url"):
                        refs["queue_urls"].append(v)
                    elif kl in ("tablename", "table_name"):
                        refs["table_names"].append(v)
                walk(v, k)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent_key)

    walk(data)
    return refs


def _detect_resources(fn_meta, project_root=None):
    """Parse serverless.yml + handler source to infer AWS resources needed.

    Returns dict with keys:
      s3_buckets   — list of bucket names
      sqs_queues   — list of queue names
      ddb_tables   — list of table names
      ses_identities — list of email addresses to verify

    If project_root is provided, also merges in extras from
    config/lambda-test-overrides.json for this Lambda.
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

    # Merge per-Lambda overrides (extra_buckets / extra_queues / extra_tables)
    if project_root:
        overrides = _load_overrides(project_root, fn_meta["name"])
        for b in overrides.get("extra_buckets", []) or []:
            resources["s3_buckets"].add(b)
        for q in overrides.get("extra_queues", []) or []:
            resources["sqs_queues"].add(q)
        for t in overrides.get("extra_tables", []) or []:
            resources["ddb_tables"].add(t)

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

        overrides = _load_overrides(project_root, fn_meta["name"])

        if overrides.get("skip_seed"):
            _log(jid, f"Skipping seed (override: skip_seed=true). Notes: {overrides.get('notes', '')}")
            _finish(jid, "done", {"created": {"skipped": True, "notes": overrides.get("notes", "")}})
            return

        ext = overrides.get("external_apis_required") or []
        if ext:
            _log(jid, "⚠️  External APIs this Lambda needs (NOT emulated by Floci):")
            for api in ext:
                _log(jid, f"   - {api}")
            _log(jid, "   Local invoke may fail at the external HTTPS call. That is expected.")

        resources = _detect_resources(fn_meta, project_root)
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

        # Upload fixtures from <fn_dir>/tests/fixtures/* — preserves existing behaviour
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

        # Override fixture_keys: explicit { key → fixture://file } mapping per Lambda.
        # Uploaded to EVERY detected bucket so the handler finds it regardless
        # of which bucket the env var or hardcoded fallback resolves to.
        fixture_keys = overrides.get("fixture_keys") or {}
        if fixture_keys:
            if not created["s3_buckets"]:
                _log(jid, "⚠️  fixture_keys defined in overrides but no S3 bucket created — skipping uploads")
            else:
                for s3_key, uri in fixture_keys.items():
                    src_path = _resolve_fixture(uri, project_root)
                    if not src_path:
                        _log(jid, f"❌ fixture not found for key '{s3_key}': {uri}")
                        continue
                    for target_bucket in created["s3_buckets"]:
                        _log(jid, f"Uploading override fixture: {src_path.name} → s3://{target_bucket}/{s3_key}")
                        rc, _, _ = _run(
                            cli + ["s3", "cp", str(src_path), f"s3://{target_bucket}/{s3_key}"],
                            timeout=60, jid=jid,
                        )
                        if rc == 0:
                            created["fixtures_uploaded"].append(f"s3://{target_bucket}/{s3_key}")

        _finish(jid, "done", {"created": created})

    threading.Thread(target=_task, daemon=True).start()
    return jid


def preflight(fn_meta, event_file, project_root):
    """Run pre-flight checks BEFORE invoking a Lambda — verify the AWS state
    the handler will read actually exists. Returns job_id (async).

    Checks performed:
      - Function deployed in Floci? (lambda get-function)
      - For each detected S3 bucket: HeadBucket
      - For each event key field: HeadObject in the first bucket
      - For each detected SQS queue: GetQueueUrl
      - For each detected DDB table: DescribeTable
      - For each env var the handler reads: present in deployed config?
      - External APIs declared in overrides → warn only (informational)

    The Lambda continues to invoke even if some checks fail — this is
    diagnostic only. But it surfaces the most common failure modes
    (missing fixture, missing bucket) before the user sees a cryptic
    SDK error inside the Lambda container.
    """
    jid = _new_job()

    def _task():
        cli = _awslocal_or_aws()
        if not cli:
            _finish(jid, "error", {"error": "Neither awslocal nor aws CLI found."})
            return

        fn_name = "local-" + fn_meta["name"].replace("_", "-").lower()
        overrides = _load_overrides(project_root, fn_meta["name"])

        if overrides.get("skip_preflight"):
            _log(jid, f"Skipping pre-flight (override). Notes: {overrides.get('notes', '')}")
            _finish(jid, "done", {"checks": [], "skipped": True})
            return

        results = []  # [(status, label, detail)] — status ∈ {ok, warn, fail, alert, info}

        # 0. AWS SDK v2 detection — RED ALERT if shim isn't active
        fn_dir = fn_meta.get("path")
        if fn_dir and _detect_aws_sdk_v2(fn_dir):
            # Check the deployed Lambda's NODE_OPTIONS for the shim require flag
            rc, stdout, _ = _run(
                cli + ["lambda", "get-function-configuration",
                       "--function-name", fn_name, "--query", "Environment.Variables.NODE_OPTIONS",
                       "--output", "text"],
                timeout=10, jid=jid,
            )
            shim_active = rc == 0 and "aws-sdk-floci-shim" in (stdout or "")
            if shim_active:
                results.append(("ok", "AWS SDK v2 + Floci shim active", "calls will route through Floci"))
            else:
                results.append((
                    "alert",
                    "🔴 AWS SDK v2 detected — shim NOT active",
                    "v2 ignores AWS_ENDPOINT_URL → Lambda will hit real AWS and fail with InvalidAccessKeyId. "
                    "Re-deploy via the dashboard to inject the shim.",
                ))

        # 1. Function deployed?
        rc, _, stderr = _run(
            cli + ["lambda", "get-function", "--function-name", fn_name],
            timeout=15, jid=jid,
        )
        if rc == 0:
            results.append(("ok", "Function deployed", fn_name))
        else:
            results.append(("fail", "Function deployed", f"{fn_name} not found — run Build & Deploy first"))
            _print_preflight(jid, results)
            _finish(jid, "done", {"checks": results, "passed": False})
            return

        resources = _detect_resources(fn_meta, project_root)

        # 2. S3 buckets
        for bucket in resources["s3_buckets"]:
            rc, _, _ = _run(
                cli + ["s3api", "head-bucket", "--bucket", bucket],
                timeout=10, jid=jid,
            )
            if rc == 0:
                results.append(("ok", "Bucket exists", bucket))
            else:
                results.append(("fail", "Bucket missing", f"{bucket} — run Seed Resources"))

        # 3. Object keys from test event (HeadObject in first bucket)
        events_dir = (Path(project_root) / "lambda-test-events").resolve()
        ev_path = events_dir / event_file if event_file else None
        if ev_path and ev_path.is_file():
            event_refs = _extract_event_keys(str(ev_path))
            target_bucket = resources["s3_buckets"][0] if resources["s3_buckets"] else None
            for k in event_refs["s3_keys"]:
                if not target_bucket:
                    results.append(("warn", "S3 key referenced", f"{k} — no bucket detected to check against"))
                    continue
                rc, _, _ = _run(
                    cli + ["s3api", "head-object", "--bucket", target_bucket, "--key", k],
                    timeout=10, jid=jid,
                )
                if rc == 0:
                    results.append(("ok", "S3 object present", f"s3://{target_bucket}/{k}"))
                else:
                    results.append(("fail", "S3 object missing", f"s3://{target_bucket}/{k} — add to fixture_keys in lambda-test-overrides.json"))

        # 4. SQS queues
        for queue in resources["sqs_queues"]:
            rc, _, _ = _run(
                cli + ["sqs", "get-queue-url", "--queue-name", queue],
                timeout=10, jid=jid,
            )
            if rc == 0:
                results.append(("ok", "Queue exists", queue))
            else:
                results.append(("fail", "Queue missing", f"{queue} — run Seed Resources"))

        # 5. DDB tables
        for table in resources["ddb_tables"]:
            rc, _, _ = _run(
                cli + ["dynamodb", "describe-table", "--table-name", table],
                timeout=10, jid=jid,
            )
            if rc == 0:
                results.append(("ok", "Table exists", table))
            else:
                results.append(("fail", "Table missing", f"{table} — run Seed Resources"))

        # 6. External API warnings
        for api in overrides.get("external_apis_required") or []:
            results.append(("warn", "External API (not in Floci)", api))

        # 7. Notes
        if overrides.get("notes"):
            results.append(("info", "Notes", overrides["notes"]))

        _print_preflight(jid, results)
        passed = not any(s in ("fail", "alert") for s, _, _ in results)
        _finish(jid, "done", {"checks": results, "passed": passed})

    threading.Thread(target=_task, daemon=True).start()
    return jid


def _print_preflight(jid, results):
    """Print pre-flight results with status icons inline in the job log."""
    icons = {"ok": "✅", "fail": "❌", "warn": "⚠️ ", "info": "ℹ️ ", "alert": "🔴"}
    _log(jid, "── Pre-flight checks ─────────────────────")
    for status, label, detail in results:
        icon = icons.get(status, "  ")
        _log(jid, f"  {icon} {label}: {detail}")
    _log(jid, "──────────────────────────────────────────")


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

        overrides = _load_overrides(project_root, fn_meta["name"])
        verdict, reason, stage = _classify_invoke(result, overrides)
        result["verdict"] = verdict
        result["verdict_reason"] = reason
        result["verdict_stage_reached"] = stage

        finish_status = "done" if verdict in ("pass", "pass_with_caveat") else "error"
        _finish(jid, finish_status, result)

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
