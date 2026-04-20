"""Test pipeline runner — executes upgrade + test steps for Angular and Lambda repos."""

import json
import os
import re
import subprocess
import time
from pathlib import Path


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

def run_pipeline(state):
    """Main pipeline runner. Called by JobQueue in a worker thread.

    state: jobs.JobState with meta dict containing all job parameters.
    """
    meta = state.meta
    repo_type = meta.get("repo_type", "unknown")

    if repo_type == "angular":
        _run_angular_pipeline(state)
    elif repo_type in ("lambda_monorepo", "lambda_serverless", "lambda_sam"):
        _run_lambda_pipeline(state)
    elif repo_type == "node_generic":
        _run_node_generic_pipeline(state)
    elif repo_type == "python_generic":
        _run_python_generic_pipeline(state)
    else:
        state.error = f"Unsupported repo type: {repo_type}"
        state.status = "completed"
        state.result = "error"
        return

    # Generate verdict after pipeline completes
    _generate_verdict(state)


# ─────────────────────────────────────────────────────────────
# ANGULAR PIPELINE
# ─────────────────────────────────────────────────────────────

def _run_angular_pipeline(state):
    """Angular: branch → upgrade → npm install → build → unit tests."""
    meta = state.meta
    repo_path = meta["repo_path"]
    pkg_dir = meta.get("function_dir") or "bac-app"
    work_dir = os.path.join(repo_path, pkg_dir)
    cmds = meta.get("test_commands", {})

    # Define steps
    steps = ["create_branch", "apply_upgrade", "npm_install", "build", "unit_tests"]
    for s in steps:
        state.add_step(s)

    # Step 1: Create branch
    if not _step_create_branch(state, repo_path):
        return

    # Step 2: Apply upgrade
    if not _step_apply_npm_upgrade(state, work_dir):
        return

    # Step 3: npm install
    if not _step_run_command(state, "npm_install", ["npm", "install"], work_dir, timeout=180):
        return

    # Step 4: Build
    build_cmd = cmds.get("build", "npx ng build --configuration production")
    if not _step_run_command(state, "build", build_cmd.split(), work_dir, timeout=600):
        return

    # Step 5: Unit tests
    test_cmd = cmds.get("unit_test", "npx ng test --watch=false --browsers=ChromeHeadless")
    _step_run_command(state, "unit_tests", test_cmd.split(), work_dir, timeout=600)


# ─────────────────────────────────────────────────────────────
# LAMBDA PIPELINE
# ─────────────────────────────────────────────────────────────

def _run_lambda_pipeline(state):
    """Lambda: branch → upgrade → install → serverless package → tests → integration."""
    meta = state.meta
    repo_path = meta["repo_path"]
    fn_dir = meta.get("function_dir", "")
    fn_info = meta.get("function_info", {})
    ecosystem = meta.get("ecosystem", "npm")

    if fn_dir:
        work_dir = os.path.join(repo_path, fn_dir)
    else:
        work_dir = repo_path

    has_tests = fn_info.get("has_tests", False)
    language = fn_info.get("language", "unknown")

    # Define steps based on test coverage
    step_names = ["create_branch", "apply_upgrade", "install_deps", "serverless_package"]
    if has_tests:
        step_names.append("run_tests")
    else:
        step_names.append("import_check")

    if meta.get("use_emulator"):
        step_names.append("integration_test")

    for s in step_names:
        state.add_step(s)

    # Step 1: Create branch
    if not _step_create_branch(state, repo_path):
        return

    # Step 2: Apply upgrade
    if ecosystem == "pip":
        if not _step_apply_pip_upgrade(state, work_dir):
            return
    else:
        if not _step_apply_npm_upgrade(state, work_dir):
            return

    # Step 3: Install deps
    if ecosystem == "pip":
        ok = _step_run_command(
            state, "install_deps",
            ["pip", "install", "-r", "requirements.txt"],
            work_dir, timeout=120,
        )
    else:
        ok = _step_run_command(state, "install_deps", ["npm", "install"], work_dir, timeout=120)
    if not ok:
        return

    # Step 4: Serverless package
    ok = _step_run_command(
        state, "serverless_package",
        ["npx", "serverless", "package", "--stage", "qa"],
        work_dir, timeout=180,
    )
    if not ok:
        return

    # Step 5: Tests or import check
    if has_tests:
        test_cmd = fn_info.get("test_command", "")
        if test_cmd:
            cmd_parts = test_cmd.split()
            ok = _step_run_command(state, "run_tests", cmd_parts, work_dir, timeout=180)
        else:
            # Fallback
            if language == "python":
                ok = _step_run_command(
                    state, "run_tests",
                    ["python", "-m", "pytest", "tests/", "-v"],
                    work_dir, timeout=180,
                )
            else:
                ok = _step_run_command(state, "run_tests", ["npm", "test"], work_dir, timeout=180)
        if not ok:
            return
    else:
        # Import check — verify handler loads without errors
        if language == "python":
            handler = fn_info.get("handler", "handler.main")
            module = handler.split(".")[0] if "." in handler else "handler"
            ok = _step_run_command(
                state, "import_check",
                ["python", "-c", f"import {module}"],
                work_dir, timeout=15,
            )
        else:
            # Node.js — try to require the compiled output
            handler = fn_info.get("handler", "dist/index.handler")
            module_path = handler.rsplit(".", 1)[0] if "." in handler else handler
            ok = _step_run_command(
                state, "import_check",
                ["node", "-e", f"require('./{module_path}')"],
                work_dir, timeout=15,
            )
        if not ok:
            return

    # Step 6: Integration test (if emulator requested)
    if meta.get("use_emulator"):
        _step_integration_test(state, work_dir, fn_info, meta)


# ─────────────────────────────────────────────────────────────
# GENERIC PIPELINES
# ─────────────────────────────────────────────────────────────

def _run_node_generic_pipeline(state):
    """Generic Node.js: branch → upgrade → install → build → test."""
    meta = state.meta
    work_dir = meta["repo_path"]
    cmds = meta.get("test_commands", {})

    steps = ["create_branch", "apply_upgrade", "npm_install", "build", "unit_tests"]
    for s in steps:
        state.add_step(s)

    if not _step_create_branch(state, work_dir):
        return
    if not _step_apply_npm_upgrade(state, work_dir):
        return
    if not _step_run_command(state, "npm_install", ["npm", "install"], work_dir, timeout=120):
        return

    build_cmd = cmds.get("build", "npm run build")
    if not _step_run_command(state, "build", build_cmd.split(), work_dir, timeout=300):
        return

    test_cmd = cmds.get("unit_test", "npm test")
    _step_run_command(state, "unit_tests", test_cmd.split(), work_dir, timeout=300)


def _run_python_generic_pipeline(state):
    """Generic Python: branch → upgrade → install → test."""
    meta = state.meta
    work_dir = meta["repo_path"]

    steps = ["create_branch", "apply_upgrade", "install_deps", "unit_tests"]
    for s in steps:
        state.add_step(s)

    if not _step_create_branch(state, work_dir):
        return
    if not _step_apply_pip_upgrade(state, work_dir):
        return
    if not _step_run_command(
        state, "install_deps",
        ["pip", "install", "-r", "requirements.txt"],
        work_dir, timeout=120,
    ):
        return

    _step_run_command(
        state, "unit_tests",
        ["python", "-m", "pytest", "-v"],
        work_dir, timeout=300,
    )


# ─────────────────────────────────────────────────────────────
# SHARED STEPS
# ─────────────────────────────────────────────────────────────

def _step_create_branch(state, repo_path):
    """Create a git branch for the upgrade. Returns True on success."""
    meta = state.meta
    pkg = meta["package"]
    epoch = int(time.time())
    fn = meta.get("function_dir", "")
    branch_suffix = f"{fn}-{pkg}" if fn else pkg
    # Sanitize branch name
    branch_suffix = re.sub(r"[^a-zA-Z0-9\-_]", "-", branch_suffix)
    branch_name = f"fix/upgrade-{branch_suffix}-{epoch}"
    state.branch = branch_name

    base = meta.get("base_branch", "develop")

    state.start_step("create_branch")
    try:
        # Fetch latest base
        subprocess.run(
            ["git", "fetch", "origin", base],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        # Create and checkout branch
        proc = subprocess.run(
            ["git", "checkout", "-b", branch_name, f"origin/{base}"],
            cwd=repo_path, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            # Try without origin/ prefix
            proc = subprocess.run(
                ["git", "checkout", "-b", branch_name, base],
                cwd=repo_path, capture_output=True, text=True, timeout=15,
            )

        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            state.finish_step("create_branch", False, output)
            state.skip_remaining()
            return False

        state.finish_step("create_branch", True, output)
        return True
    except Exception as e:
        state.finish_step("create_branch", False, str(e))
        state.skip_remaining()
        return False


def _step_apply_npm_upgrade(state, work_dir):
    """Apply npm upgrade based on strategy."""
    meta = state.meta
    strategy = meta["strategy"]
    package = meta["package"]

    state.start_step("apply_upgrade")
    try:
        if strategy.startswith("parent_upgrade"):
            parent = meta.get("parent_package", "")
            version = meta.get("target_version", "latest")
            if not parent:
                state.finish_step("apply_upgrade", False, "No parent_package specified")
                state.skip_remaining()
                return False
            proc = subprocess.run(
                ["npm", "install", f"{parent}@{version}"],
                cwd=work_dir, capture_output=True, text=True, timeout=120,
            )
            output = proc.stdout + proc.stderr
            state.finish_step("apply_upgrade", proc.returncode == 0, output)
            return proc.returncode == 0

        elif strategy == "overrides":
            spec = meta.get("override_spec", f">={meta.get('target_version', '')}")
            pkg_json_path = os.path.join(work_dir, "package.json")
            if not os.path.exists(pkg_json_path):
                state.finish_step("apply_upgrade", False, "package.json not found")
                state.skip_remaining()
                return False

            with open(pkg_json_path) as f:
                pkg = json.load(f)

            overrides = pkg.get("overrides", {})
            overrides[package] = spec
            pkg["overrides"] = overrides

            with open(pkg_json_path, "w") as f:
                json.dump(pkg, f, indent=2)

            state.finish_step(
                "apply_upgrade", True,
                f'Added overrides: {{"{package}": "{spec}"}}'
            )
            return True

        elif strategy == "direct_upgrade":
            version = meta.get("target_version", "latest")
            proc = subprocess.run(
                ["npm", "install", f"{package}@{version}"],
                cwd=work_dir, capture_output=True, text=True, timeout=120,
            )
            output = proc.stdout + proc.stderr
            state.finish_step("apply_upgrade", proc.returncode == 0, output)
            return proc.returncode == 0

        else:
            state.finish_step("apply_upgrade", False, f"Unknown strategy: {strategy}")
            state.skip_remaining()
            return False

    except Exception as e:
        state.finish_step("apply_upgrade", False, str(e))
        state.skip_remaining()
        return False


def _step_apply_pip_upgrade(state, work_dir):
    """Apply pip upgrade by editing requirements.txt."""
    meta = state.meta
    package = meta["package"]
    version = meta.get("target_version", "")
    strategy = meta["strategy"]

    state.start_step("apply_upgrade")
    try:
        req_path = os.path.join(work_dir, "requirements.txt")
        if not os.path.exists(req_path):
            state.finish_step("apply_upgrade", False, "requirements.txt not found")
            state.skip_remaining()
            return False

        with open(req_path) as f:
            lines = f.readlines()

        updated = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            # Match package name (case-insensitive, with or without version spec)
            pkg_pattern = re.escape(package)
            if re.match(rf"^{pkg_pattern}([=><!~\[]|$)", stripped, re.IGNORECASE):
                if strategy == "pin_range":
                    spec = meta.get("override_spec", f">={version}")
                    new_lines.append(f"{package}{spec}\n")
                else:
                    new_lines.append(f"{package}=={version}\n")
                updated = True
            else:
                new_lines.append(line)

        if not updated:
            # Package not in requirements.txt — append it
            new_lines.append(f"{package}=={version}\n")

        with open(req_path, "w") as f:
            f.writelines(new_lines)

        state.finish_step("apply_upgrade", True, f"Updated {package} to {version}")
        return True

    except Exception as e:
        state.finish_step("apply_upgrade", False, str(e))
        state.skip_remaining()
        return False


def _step_run_command(state, step_name, cmd, cwd, timeout=120):
    """Run a shell command as a pipeline step. Returns True if exit code 0."""
    if state.cancelled:
        state.skip_remaining()
        return False

    state.start_step(step_name)
    try:
        proc = subprocess.run(
            cmd, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
        output = proc.stdout + "\n" + proc.stderr
        passed = proc.returncode == 0
        state.finish_step(step_name, passed, output)

        if not passed:
            state.skip_remaining()
        return passed

    except subprocess.TimeoutExpired:
        state.finish_step(step_name, False, f"Command timed out after {timeout}s")
        state.skip_remaining()
        return False
    except FileNotFoundError as e:
        state.finish_step(step_name, False, f"Command not found: {e}")
        state.skip_remaining()
        return False
    except Exception as e:
        state.finish_step(step_name, False, str(e))
        state.skip_remaining()
        return False


def _step_integration_test(state, work_dir, fn_info, meta):
    """Run integration test via Floci/LocalStack emulator."""
    state.start_step("integration_test")

    # Check if emulator is running
    emulator_url = "http://localhost:4566"
    try:
        import urllib.request
        req = urllib.request.Request(f"{emulator_url}/_floci/health", method="GET")
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        try:
            req = urllib.request.Request(f"{emulator_url}/_localstack/health", method="GET")
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            state.finish_step(
                "integration_test", False,
                "Emulator not running. Start with: make emulator-up"
            )
            # Don't skip remaining — integration test failure without emulator is a soft fail
            return

    # Generate test event based on trigger type
    trigger = fn_info.get("trigger_type", "sqs")
    test_event = _generate_test_event(trigger, fn_info)
    event_path = os.path.join(work_dir, ".wizard-test-event.json")
    with open(event_path, "w") as f:
        json.dump(test_event, f, indent=2)

    try:
        # Try serverless invoke local first
        proc = subprocess.run(
            ["npx", "serverless", "invoke", "local",
             "--function", fn_info.get("handler", "").split(".")[0] or "handler",
             "--path", event_path,
             "--stage", "qa"],
            cwd=work_dir,
            capture_output=True, text=True, timeout=120,
        )
        output = proc.stdout + "\n" + proc.stderr

        # Check for errors in output
        has_error = (
            proc.returncode != 0
            or "errorMessage" in output
            or "Traceback" in output
            or "Error:" in output
        )

        state.finish_step("integration_test", not has_error, output)
    except Exception as e:
        state.finish_step("integration_test", False, str(e))
    finally:
        # Clean up temp event file
        if os.path.exists(event_path):
            os.remove(event_path)


# ─────────────────────────────────────────────────────────────
# TEST EVENT GENERATION
# ─────────────────────────────────────────────────────────────

def _generate_test_event(trigger_type, fn_info):
    """Generate a test event JSON matching the function's trigger type."""
    if trigger_type == "sqs":
        return {
            "Records": [{
                "messageId": "wizard-test-001",
                "receiptHandle": "wizard-test",
                "body": json.dumps({"action": "test", "source": "wizard"}),
                "attributes": {
                    "ApproximateReceiveCount": "1",
                    "SentTimestamp": str(int(time.time() * 1000)),
                },
                "messageAttributes": {},
                "md5OfBody": "test",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:000000000000:test-queue",
                "awsRegion": "us-east-1",
            }]
        }
    elif trigger_type == "s3":
        return {
            "Records": [{
                "eventVersion": "2.1",
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "test-bucket"},
                    "object": {"key": "test/wizard-test.txt", "size": 100},
                },
            }]
        }
    elif trigger_type == "http":
        return {
            "httpMethod": "POST",
            "path": "/api/test",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"source": "wizard", "action": "test"}),
            "queryStringParameters": {},
            "pathParameters": {},
            "requestContext": {"stage": "qa"},
        }
    elif trigger_type == "schedule":
        return {
            "source": "aws.events",
            "detail-type": "Scheduled Event",
            "detail": {},
            "time": "2026-04-17T00:00:00Z",
            "resources": ["arn:aws:events:us-east-1:000000000000:rule/wizard-test"],
        }
    elif trigger_type == "stream":
        return {
            "Records": [{
                "eventID": "wizard-test-001",
                "eventName": "MODIFY",
                "dynamodb": {
                    "Keys": {"id": {"S": "test-001"}},
                    "NewImage": {"id": {"S": "test-001"}, "status": {"S": "ACTIVE"}},
                    "OldImage": {"id": {"S": "test-001"}, "status": {"S": "PENDING"}},
                    "StreamViewType": "NEW_AND_OLD_IMAGES",
                },
                "eventSourceARN": "arn:aws:dynamodb:us-east-1:000000000000:table/test-table/stream/2026-04-17",
            }]
        }
    else:
        # Default: empty event
        return {"source": "wizard", "action": "test"}


# ─────────────────────────────────────────────────────────────
# VERDICT GENERATION
# ─────────────────────────────────────────────────────────────

def _generate_verdict(state):
    """Generate verdict based on pipeline results."""
    meta = state.meta
    fn_info = meta.get("function_info", {})
    has_tests = fn_info.get("has_tests", False)

    # For Angular repos, has_tests is always true (ng test exists)
    if meta.get("repo_type") == "angular":
        has_tests = True

    steps = state.steps
    failed = [s for s in steps if s["status"] == "failed"]
    passed_all = len(failed) == 0

    # Determine version jump type
    current_v = meta.get("current_version", "0.0.0")  # may not always be available
    target_v = meta.get("target_version", "latest")
    jump = _version_jump_type(current_v, target_v)

    if passed_all and has_tests:
        verdict = {
            "status": "safe",
            "icon": "check",
            "title": "SAFE TO UPGRADE",
            "message": f"All pipeline steps passed with existing test coverage.",
            "recommendation": "Merge Dependabot PR or push this branch.",
            "confidence": "high",
            "actions": ["push_pr", "dismiss_alert", "view_diff", "cleanup"],
        }
    elif passed_all and not has_tests:
        if jump == "patch":
            verdict = {
                "status": "likely_safe",
                "icon": "warning",
                "title": "LIKELY SAFE (LOW CONFIDENCE)",
                "message": "Package builds + imports successfully. No unit tests to verify logic.",
                "recommendation": "Merge is likely safe for this patch-level change. Manual smoke test recommended.",
                "confidence": "low",
                "actions": ["push_pr", "manual_test", "view_diff", "cleanup"],
            }
        elif jump == "minor":
            verdict = {
                "status": "likely_safe",
                "icon": "warning",
                "title": "LIKELY SAFE — REVIEW CHANGELOG",
                "message": "Minor version bump. Builds + imports OK, but no tests to catch behavior changes.",
                "recommendation": "Review changelog for behavior changes before merging.",
                "confidence": "low",
                "actions": ["push_pr", "manual_test", "view_diff", "cleanup"],
            }
        else:  # major or unknown
            verdict = {
                "status": "caution",
                "icon": "warning",
                "title": "CAUTION — MAJOR VERSION",
                "message": "Major version bump with no test coverage. Builds OK but behavior untested.",
                "recommendation": "Manual verification required. Consider adding tests first.",
                "confidence": "very_low",
                "actions": ["manual_test", "view_diff", "cleanup"],
            }
    else:
        # Failed
        failed_step = failed[0]["name"] if failed else "unknown"
        error_output = failed[0].get("output_tail", "") if failed else ""

        verdict = {
            "status": "breaks",
            "icon": "error",
            "title": "UPGRADE BREAKS FUNCTION",
            "message": f"Failed at: {failed_step}",
            "error_output": error_output,
            "recommendation": "Do not merge Dependabot suggestion as-is.",
            "confidence": "definitive",
            "failed_step": failed_step,
            "actions": ["try_version", "view_error", "cleanup"],
        }

        # Suggest intermediate version
        if target_v != "latest" and current_v:
            verdict["intermediate_suggestion"] = (
                f"Try an intermediate version between {current_v} and {target_v}. "
                f"Use 'Try Recommended Version' to binary-search for a safe version."
            )

    state.verdict = verdict


def _version_jump_type(from_v, to_v):
    """Determine if upgrade is patch/minor/major."""
    if to_v == "latest":
        return "unknown"
    try:
        fp = from_v.split(".")
        tp = to_v.split(".")
        if fp[0] != tp[0]:
            return "major"
        if len(fp) > 1 and len(tp) > 1 and fp[1] != tp[1]:
            return "minor"
        return "patch"
    except (IndexError, AttributeError):
        return "unknown"
