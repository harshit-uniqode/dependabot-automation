"""Repo manager — auto-detection of repo type, Lambda function scanning, command resolution."""

import json
import os
import re
from pathlib import Path


# ─────────────────────────────────────────────────────────────
# REPO TYPE AUTO-DETECTION
# ─────────────────────────────────────────────────────────────

def detect_repo_type(repo_path):
    """Detect repo type from sentinel files. Returns type string."""
    p = Path(repo_path)

    # Check for Angular
    if (p / "angular.json").exists() or (p / "bac-app" / "angular.json").exists():
        return "angular"

    # Check for SAM Lambda
    if (p / "template.yaml").exists() or (p / "template.yml").exists():
        return "lambda_sam"

    # Check for Serverless Framework monorepo (multiple dirs with serverless.yml)
    sls_count = sum(
        1 for d in p.iterdir()
        if d.is_dir() and not d.name.startswith(".") and (d / "serverless.yml").exists()
    )
    if sls_count >= 2:
        return "lambda_monorepo"

    # Single Serverless function
    if (p / "serverless.yml").exists() or (p / "serverless.ts").exists():
        return "lambda_serverless"

    # Generic Node
    if (p / "package.json").exists():
        return "node_generic"

    # Generic Python
    if (p / "requirements.txt").exists() or (p / "pyproject.toml").exists():
        return "python_generic"

    return "unknown"


# ─────────────────────────────────────────────────────────────
# DEFAULT TEST COMMANDS PER REPO TYPE
# ─────────────────────────────────────────────────────────────

DEFAULT_COMMANDS = {
    "angular": {
        "build": "npx ng build --configuration production",
        "unit_test": "npx ng test --watch=false --browsers=ChromeHeadless",
    },
    "lambda_monorepo": {
        # Per-function; resolved dynamically
    },
    "lambda_sam": {
        "build": "sam build",
        "unit_test": "npm test",
    },
    "lambda_serverless": {
        "build": "npx serverless package --stage qa",
        "unit_test": "npm test",
    },
    "node_generic": {
        "build": "npm run build",
        "unit_test": "npm test",
    },
    "python_generic": {
        "build": None,
        "unit_test": "python -m pytest",
    },
}


# ─────────────────────────────────────────────────────────────
# LAMBDA MONOREPO SCANNER
# ─────────────────────────────────────────────────────────────

def scan_lambda_monorepo(repo_path):
    """Scan a Lambda monorepo and return a dict of function metadata.

    Returns:
        {
            "function_dir_name": {
                "path": "/abs/path/to/function",
                "runtime": "python3.12",
                "language": "python" | "nodejs",
                "has_tests": True/False,
                "test_command": "pytest tests/ -v" | "npm test" | None,
                "deps_file": "requirements.txt" | "package.json",
                "trigger_type": "sqs" | "s3" | "http" | "schedule" | "stream" | "unknown",
                "services": ["sqs", "ses", ...],
                "handler": "handler.main",
                "memory": 256,
                "timeout": 60,
            },
            ...
        }
    """
    repo = Path(repo_path)
    functions = {}

    for d in sorted(repo.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name == "node_modules":
            continue

        sls_file = d / "serverless.yml"
        if not sls_file.exists():
            continue

        fn_meta = _parse_serverless_yml(d, sls_file)
        fn_meta["path"] = str(d)
        fn_meta.update(_detect_tests(d, fn_meta.get("language", "unknown")))
        fn_meta.update(_detect_deps_file(d))
        functions[d.name] = fn_meta

    return functions


def _parse_serverless_yml(fn_dir, sls_file):
    """Parse serverless.yml for runtime, triggers, services. Uses regex (no yaml dep)."""
    text = sls_file.read_text()
    meta = {
        "runtime": "unknown",
        "language": "unknown",
        "trigger_type": "unknown",
        "services": [],
        "handler": "",
        "memory": 128,
        "timeout": 30,
    }

    # Runtime
    rt_match = re.search(r"runtime:\s*(\S+)", text)
    if rt_match:
        runtime = rt_match.group(1)
        meta["runtime"] = runtime
        if "python" in runtime:
            meta["language"] = "python"
        elif "node" in runtime:
            meta["language"] = "nodejs"

    # Handler
    h_match = re.search(r"handler:\s*(\S+)", text)
    if h_match:
        meta["handler"] = h_match.group(1)

    # Memory
    m_match = re.search(r"memorySize:\s*(\d+)", text)
    if m_match:
        meta["memory"] = int(m_match.group(1))

    # Timeout
    t_match = re.search(r"timeout:\s*(\d+)", text)
    if t_match:
        meta["timeout"] = int(t_match.group(1))

    # Trigger type — check events section
    if re.search(r"- sqs:", text):
        meta["trigger_type"] = "sqs"
    elif re.search(r"- s3:", text):
        meta["trigger_type"] = "s3"
    elif re.search(r"- http:", text) or re.search(r"- httpApi:", text):
        meta["trigger_type"] = "http"
    elif re.search(r"- schedule:", text):
        meta["trigger_type"] = "schedule"
    elif re.search(r"- stream:", text):
        meta["trigger_type"] = "stream"

    # Services used — scan for AWS service references
    svc_patterns = {
        "sqs": r"sqs[:\.]|SQS|Queue",
        "s3": r"s3[:\.]|S3|Bucket",
        "dynamodb": r"dynamodb[:\.]|DynamoDB|Table",
        "ses": r"ses[:\.]|SES|SendEmail",
        "sns": r"sns[:\.]|SNS|Topic",
        "secretsmanager": r"secretsmanager|SecretsManager|GetSecretValue",
        "kinesis": r"kinesis|Kinesis|Firehose",
        "cloudfront": r"cloudfront|CloudFront",
        "ecr": r"ecr[:\.]|ECR",
        "eventbridge": r"eventbridge|EventBridge",
    }
    for svc, pattern in svc_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            meta["services"].append(svc)

    return meta


def _detect_tests(fn_dir, language):
    """Detect if function has tests and what command runs them."""
    result = {"has_tests": False, "test_command": None}
    d = Path(fn_dir)

    # Check for tests/ directory
    tests_dir = d / "tests"
    if not tests_dir.exists():
        # Check nested — e.g., services/tests/ in polo_email_service
        for sub in d.rglob("tests"):
            if sub.is_dir():
                tests_dir = sub
                break

    has_test_files = False
    if tests_dir.exists() and tests_dir.is_dir():
        test_files = list(tests_dir.rglob("test_*.py")) + list(tests_dir.rglob("*_test.py"))
        test_files += list(tests_dir.rglob("*.test.js")) + list(tests_dir.rglob("*.test.ts"))
        has_test_files = len(test_files) > 0

    if not has_test_files:
        return result

    result["has_tests"] = True

    # Check for Makefile with test target
    makefile = d / "Makefile"
    if makefile.exists():
        mk_text = makefile.read_text()
        if re.search(r"^test:", mk_text, re.MULTILINE):
            result["test_command"] = "make test"
            return result

    # Check for package.json test script
    pkg_json = d / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            if pkg.get("scripts", {}).get("test"):
                result["test_command"] = "npm test"
                return result
        except (json.JSONDecodeError, KeyError):
            pass

    # Default by language
    if language == "python":
        result["test_command"] = "python -m pytest tests/ -v"
    elif language == "nodejs":
        result["test_command"] = "npm test"

    return result


def _detect_deps_file(fn_dir):
    """Detect the primary dependency file for a function."""
    d = Path(fn_dir)
    if (d / "requirements.txt").exists():
        return {"deps_file": "requirements.txt"}
    if (d / "package.json").exists():
        return {"deps_file": "package.json"}
    if (d / "pyproject.toml").exists():
        return {"deps_file": "pyproject.toml"}
    return {"deps_file": None}


# ─────────────────────────────────────────────────────────────
# BUILD FULL REPO INFO
# ─────────────────────────────────────────────────────────────

def build_repo_info(repo_cfg):
    """Build complete repo info with auto-detection and function scanning.

    Takes a repo entry from wizard-config.json, enriches it with
    auto-detected fields, and returns the complete info dict.
    """
    path = repo_cfg["path"]
    info = dict(repo_cfg)

    # Auto-detect type if not set
    if not info.get("type"):
        info["type"] = detect_repo_type(path)

    # Validate path
    if not Path(path).exists():
        info["valid"] = False
        info["validation_error"] = f"Path does not exist: {path}"
        return info

    info["valid"] = True
    info["validation_error"] = None

    # Merge default test commands if not explicitly set
    if not info.get("test_commands"):
        info["test_commands"] = DEFAULT_COMMANDS.get(info["type"], {}).copy()

    # Scan Lambda monorepo for function metadata
    if info["type"] == "lambda_monorepo":
        info["functions"] = scan_lambda_monorepo(path)
        info["function_count"] = len(info["functions"])
        info["functions_with_tests"] = sum(
            1 for f in info["functions"].values() if f.get("has_tests")
        )
    else:
        info["functions"] = {}
        info["function_count"] = 0
        info["functions_with_tests"] = 0

    return info


def build_all_repos_info(config):
    """Build info for all repos in config. Returns list of enriched repo dicts."""
    return [build_repo_info(r) for r in config.get("repos", [])]
