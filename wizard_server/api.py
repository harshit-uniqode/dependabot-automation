"""API endpoint handlers — analyze, test-upgrade, config."""

import json
import os
import re
import subprocess
from pathlib import Path


# ─────────────────────────────────────────────────────────────
# ANALYZE
# ─────────────────────────────────────────────────────────────

def handle_analyze(body, repos_info, config):
    """Handle POST /api/analyze. Returns response dict with _status key."""
    if not body:
        return {"_status": 400, "error": "Request body required"}

    repo_name = body.get("repo", "")
    package = body.get("package", "")
    ecosystem = body.get("ecosystem", "npm")
    function_dir = body.get("function_dir", "")
    current_version = body.get("current_version", "")
    patched_version = body.get("patched_version", "")

    if not repo_name or not package:
        return {"_status": 400, "error": "Missing required fields: repo, package"}

    # Find repo info
    repo_info = None
    for ri in repos_info:
        if ri["name"] == repo_name:
            repo_info = ri
            break

    if not repo_info:
        return {"_status": 400, "error": f"Repo '{repo_name}' not found in config"}

    if not repo_info.get("valid"):
        return {"_status": 400, "error": f"Repo invalid: {repo_info.get('validation_error')}"}

    repo_path = repo_info["path"]
    repo_type = repo_info.get("type", "unknown")

    # Determine working directory
    if repo_type == "lambda_monorepo" and function_dir:
        work_dir = os.path.join(repo_path, function_dir)
        if not os.path.isdir(work_dir):
            return {"_status": 400, "error": f"Function dir not found: {function_dir}"}
    elif repo_info.get("package_dir"):
        work_dir = os.path.join(repo_path, repo_info["package_dir"])
    else:
        work_dir = repo_path

    # Get function info for Lambda monorepos
    function_info = None
    if repo_type == "lambda_monorepo" and function_dir:
        functions = repo_info.get("functions", {})
        function_info = functions.get(function_dir)

    # Run analysis based on ecosystem
    if ecosystem == "pip":
        analysis = _analyze_pip(work_dir, package, current_version, patched_version)
    else:
        analysis = _analyze_npm(work_dir, package, current_version, patched_version)

    analysis["_status"] = 200
    analysis["package"] = package
    analysis["current_version"] = current_version
    analysis["patched_version"] = patched_version
    analysis["repo"] = repo_name
    analysis["function_dir"] = function_dir

    if function_info:
        analysis["function_info"] = {
            "runtime": function_info.get("runtime"),
            "language": function_info.get("language"),
            "has_tests": function_info.get("has_tests", False),
            "test_command": function_info.get("test_command"),
            "trigger_type": function_info.get("trigger_type"),
            "services": function_info.get("services", []),
        }

    return analysis


def _analyze_npm(work_dir, package, current_version, patched_version):
    """Run npm why and build upgrade options for an npm package."""
    result = {
        "dep_tree": "",
        "parent_deps": [],
        "changelog_url": f"https://www.npmjs.com/package/{package}?activeTab=versions",
        "npm_url": f"https://www.npmjs.com/package/{package}",
        "upgrade_options": [],
    }

    # Run npm why
    try:
        proc = subprocess.run(
            ["npm", "why", package],
            cwd=work_dir,
            capture_output=True, text=True, timeout=30,
        )
        result["dep_tree"] = proc.stdout.strip() or proc.stderr.strip()

        # Parse parent deps from npm why output
        result["parent_deps"] = _parse_npm_why(proc.stdout)
    except FileNotFoundError:
        result["dep_tree"] = "Error: npm not found in PATH"
    except subprocess.TimeoutExpired:
        result["dep_tree"] = "Error: npm why timed out after 30s"

    # Build upgrade options
    if result["parent_deps"]:
        for parent in result["parent_deps"]:
            result["upgrade_options"].append({
                "id": f"parent_upgrade_{parent['name']}",
                "label": f"Upgrade parent: {parent['name']}",
                "description": f"npm install {parent['name']}@latest",
                "command": f"npm install {parent['name']}@latest",
                "risk": _assess_version_risk(parent.get("version", ""), "latest"),
                "risk_reason": "Check changelog for breaking changes in parent package",
            })

    # Always offer overrides option for sub-deps
    result["upgrade_options"].append({
        "id": "overrides",
        "label": f"Add npm overrides for {package}",
        "description": f'Add "overrides": {{"{package}": ">={patched_version}"}} to package.json',
        "command": f'"overrides": {{"{package}": ">={patched_version}"}}',
        "risk": "low" if _is_patch_bump(current_version, patched_version) else "medium",
        "risk_reason": "Forces minimum version globally. May break parent if incompatible.",
    })

    # Try to get better changelog URL
    try:
        proc = subprocess.run(
            ["npm", "view", package, "repository.url"],
            capture_output=True, text=True, timeout=10,
        )
        repo_url = proc.stdout.strip()
        if repo_url:
            # Clean up git+https://... or git://... URLs
            repo_url = re.sub(r"^git\+", "", repo_url)
            repo_url = re.sub(r"\.git$", "", repo_url)
            repo_url = re.sub(r"^git://", "https://", repo_url)
            if "github.com" in repo_url:
                result["changelog_url"] = repo_url + "/releases"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return result


def _analyze_pip(work_dir, package, current_version, patched_version):
    """Analyze a pip package in requirements.txt."""
    result = {
        "dep_tree": "",
        "parent_deps": [],
        "changelog_url": f"https://pypi.org/project/{package}/#history",
        "pypi_url": f"https://pypi.org/project/{package}/",
        "upgrade_options": [],
    }

    # Check if package is in requirements.txt
    req_file = os.path.join(work_dir, "requirements.txt")
    if os.path.exists(req_file):
        with open(req_file) as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.lower().startswith(package.lower()):
                result["dep_tree"] = f"{stripped} (requirements.txt, line {i + 1})"
                break

    # pip show for more details
    try:
        proc = subprocess.run(
            ["pip", "show", package],
            cwd=work_dir,
            capture_output=True, text=True, timeout=15,
        )
        if proc.stdout:
            result["dep_tree"] += "\n\n" + proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # For pip: direct upgrade is usually the right option
    target = patched_version or "latest"
    result["upgrade_options"].append({
        "id": "direct_upgrade",
        "label": f"Upgrade {package} to {target}",
        "description": f"Change requirements.txt: {package}=={target}",
        "command": f"{package}=={target}",
        "risk": _assess_version_risk(current_version, target),
        "risk_reason": "Review changelog for breaking changes",
    })

    # Also offer pinning a range
    if patched_version:
        result["upgrade_options"].append({
            "id": "pin_range",
            "label": f"Pin range: {package}>={patched_version}",
            "description": f"Allows future compatible versions",
            "command": f"{package}>={patched_version}",
            "risk": "medium",
            "risk_reason": "Future versions may introduce breaking changes",
        })

    return result


def _parse_npm_why(output):
    """Parse npm why output to extract parent dependency chain."""
    parents = []
    if not output:
        return parents

    # npm why output format varies. Look for patterns like:
    # package@version from PackageName@version
    seen = set()
    for line in output.split("\n"):
        # Match: "from @scope/pkg@version" or "from pkg@version"
        matches = re.findall(r'from\s+(@?[\w\-./]+)@(\S+)', line)
        for name, version in matches:
            if name not in seen:
                seen.add(name)
                parents.append({
                    "name": name,
                    "version": version,
                    "is_direct": False,  # Will be enriched by caller if needed
                    "required_range": "",
                })

    return parents


def _is_patch_bump(from_v, to_v):
    """Check if version change is a patch bump (x.y.Z)."""
    try:
        fp = from_v.split(".")
        tp = to_v.split(".")
        return fp[0] == tp[0] and fp[1] == tp[1]
    except (IndexError, AttributeError):
        return False


def _assess_version_risk(from_v, to_v):
    """Assess risk level of a version upgrade."""
    if to_v == "latest":
        return "medium"
    try:
        fp = from_v.split(".")
        tp = to_v.split(".")
        if fp[0] != tp[0]:
            return "high"  # Major version bump
        if fp[1] != tp[1]:
            return "medium"  # Minor version bump
        return "low"  # Patch
    except (IndexError, AttributeError):
        return "medium"


# ─────────────────────────────────────────────────────────────
# TEST UPGRADE
# ─────────────────────────────────────────────────────────────

def handle_test_upgrade(body, repos_info, config, job_queue):
    """Handle POST /api/test-upgrade. Returns response dict."""
    if not body:
        return {"_status": 400, "error": "Request body required"}

    repo_name = body.get("repo", "")
    package = body.get("package", "")
    strategy = body.get("strategy", "")

    if not repo_name or not package or not strategy:
        return {"_status": 400, "error": "Missing: repo, package, strategy"}

    # Find repo
    repo_info = None
    for ri in repos_info:
        if ri["name"] == repo_name:
            repo_info = ri
            break

    if not repo_info:
        return {"_status": 400, "error": f"Repo '{repo_name}' not found"}

    if not repo_info.get("valid"):
        return {"_status": 400, "error": f"Repo invalid: {repo_info.get('validation_error')}"}

    # Check working tree is clean
    repo_path = repo_info["path"]
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        if proc.stdout.strip():
            return {
                "_status": 400,
                "error": "Working tree has uncommitted changes. Commit or stash first.",
            }
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # If git not available, proceed anyway

    # Build job metadata
    job_meta = {
        "repo": repo_name,
        "repo_path": repo_path,
        "repo_type": repo_info.get("type"),
        "package": package,
        "strategy": strategy,
        "target_version": body.get("target_version", "latest"),
        "parent_package": body.get("parent_package"),
        "override_spec": body.get("override_spec"),
        "base_branch": body.get("base_branch", repo_info.get("default_base_branch", "develop")),
        "function_dir": body.get("function_dir", ""),
        "use_emulator": body.get("use_emulator", False),
        "ecosystem": body.get("ecosystem", "npm"),
        "test_commands": repo_info.get("test_commands", {}),
    }

    # Get function-specific info for Lambda monorepos
    if repo_info.get("type") == "lambda_monorepo" and body.get("function_dir"):
        fn_info = repo_info.get("functions", {}).get(body["function_dir"], {})
        job_meta["function_info"] = fn_info

    # Import pipeline runner
    from . import pipelines

    # Submit job
    job_id, err = job_queue.submit(pipelines.run_pipeline, job_meta)
    if err:
        return {"_status": 409, "error": err}

    return {
        "_status": 202,
        "job_id": job_id,
        "status": "queued",
        "message": f"Upgrade job queued. Poll GET /api/status/{job_id} for progress.",
    }
