#!/usr/bin/env python3
"""Sync config/lambda-ui-flows.json with the Lambda functions on disk.

For every Lambda directory in `beaconstac_lambda_functions/` that does not
already have an entry in `lambda-ui-flows.json`, this script appends a stub
entry with auto-detected fields (runtime, trigger type, handler) and
TODO placeholders for the human-friendly fields.

It will NEVER overwrite an existing entry — only add missing ones. Run it
whenever a new Lambda is added to the monorepo.

Usage:
    python3 scripts/sync_lambda_ui_flows.py [--lambdas-dir PATH] [--dry-run]

Output:
    - Modifies config/lambda-ui-flows.json in place (writes only if changed).
    - Prints a summary of additions / orphaned entries.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FLOWS_PATH = PROJECT_ROOT / "config" / "lambda-ui-flows.json"
DEFAULT_LAMBDAS_DIR = (
    PROJECT_ROOT.parent / "Work" / "beaconstac_lambda_functions"
)


def parse_serverless_yml(path: Path) -> dict[str, Any]:
    """Lightweight regex-based parse — avoids pulling in PyYAML.

    Extracts: runtime, handler entries, and trigger types (sqs/http/schedule/
    stream/sns/s3/cloudwatchEvent). Good enough for a stub entry.
    """
    if not path.is_file():
        return {}
    txt = path.read_text(errors="replace")
    info: dict[str, Any] = {"runtimes": [], "handlers": [], "triggers": set()}

    # runtime: python3.11 / nodejs18.x / etc.
    info["runtimes"] = sorted(set(re.findall(r"runtime:\s*([\w.]+)", txt)))

    # handler: foo.bar
    info["handlers"] = sorted(set(re.findall(r"handler:\s*([\w./@-]+)", txt)))

    # Triggers — match `- <type>:` under `events:`
    trigger_keywords = (
        "sqs", "http", "httpApi", "schedule", "stream", "sns", "s3",
        "cloudwatchEvent", "websocket", "eventBridge", "alb",
    )
    for kw in trigger_keywords:
        if re.search(rf"^\s*-\s*{re.escape(kw)}:", txt, re.MULTILINE):
            info["triggers"].add(kw)

    info["triggers"] = sorted(info["triggers"])
    return info


def stub_entry(fn_dir: str, sls_info: dict[str, Any]) -> dict[str, Any]:
    """Build a stub UI flow entry. Human fields are TODO placeholders."""
    triggers = sls_info.get("triggers") or []
    runtimes = sls_info.get("runtimes") or []
    handlers = sls_info.get("handlers") or []

    if "sqs" in triggers:
        trigger_desc = "Backend pushes a message to SQS that this Lambda consumes."
    elif "http" in triggers or "httpApi" in triggers or "alb" in triggers:
        trigger_desc = "External HTTP request hits this Lambda's URL."
    elif "schedule" in triggers:
        trigger_desc = "Scheduled cron — runs on a fixed cadence."
    elif "stream" in triggers:
        trigger_desc = "DynamoDB / Kinesis stream change triggers this Lambda."
    elif "sns" in triggers:
        trigger_desc = "SNS topic publishes a message."
    elif "s3" in triggers:
        trigger_desc = "S3 object event (upload/delete) fires this Lambda."
    else:
        trigger_desc = "TODO: trigger type not auto-detected — add manually."

    return {
        "_TODO": (
            "Auto-generated stub. Fill in the human-friendly fields below "
            f"(runtime={','.join(runtimes) or '?'}, "
            f"triggers={','.join(triggers) or 'none'}, "
            f"handlers={','.join(handlers) or '?'})"
        ),
        "what_it_does": "TODO: one sentence describing what this Lambda does in user-facing terms.",
        "trigger": trigger_desc,
        "ui_flow": [
            "TODO: 1. First click the user makes in Uniqode/Polo dashboard",
            "TODO: 2. Next step",
            "TODO: 3. Wait / submit / etc.",
        ],
        "how_to_verify": [
            "TODO: What the user sees in the UI to confirm success",
        ],
        "external_systems": [],
        "floci_tested": "Boot + trigger event parse + AWS SDK calls (Floci-emulated). Third-party HTTP NOT tested.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lambdas-dir",
        type=Path,
        default=DEFAULT_LAMBDAS_DIR,
        help=f"Path to beaconstac_lambda_functions repo (default: {DEFAULT_LAMBDAS_DIR})",
    )
    parser.add_argument(
        "--flows-file",
        type=Path,
        default=DEFAULT_FLOWS_PATH,
        help=f"Path to lambda-ui-flows.json (default: {DEFAULT_FLOWS_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be added without writing.",
    )
    args = parser.parse_args()

    lambdas_dir: Path = args.lambdas_dir
    flows_file: Path = args.flows_file

    if not lambdas_dir.is_dir():
        print(f"ERROR: Lambdas dir not found: {lambdas_dir}", file=sys.stderr)
        return 2
    if not flows_file.is_file():
        print(f"ERROR: Flows file not found: {flows_file}", file=sys.stderr)
        return 2

    flows: dict[str, Any] = json.loads(flows_file.read_text())

    # Discover Lambdas: any direct subdir with a serverless.yml
    on_disk: dict[str, dict[str, Any]] = {}
    for child in sorted(lambdas_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        sls = child / "serverless.yml"
        if sls.is_file():
            on_disk[child.name] = parse_serverless_yml(sls)

    documented = {k for k in flows.keys() if not k.startswith("_")}
    found = set(on_disk.keys())

    missing = sorted(found - documented)
    orphans = sorted(documented - found)

    print(f"Lambdas on disk:    {len(found)}")
    print(f"Documented entries: {len(documented)}")
    print(f"Missing (to add):   {len(missing)}")
    print(f"Orphans:            {len(orphans)}")
    print()

    if missing:
        print("== Adding stubs for: ==")
        for name in missing:
            print(f"  + {name}  (triggers={on_disk[name].get('triggers')})")
            flows[name] = stub_entry(name, on_disk[name])
    if orphans:
        print()
        print("== Orphan entries (in JSON, not on disk) — review manually: ==")
        for name in orphans:
            print(f"  ? {name}")

    if not missing:
        print("Nothing to add. lambda-ui-flows.json is up to date.")
        return 0

    if args.dry_run:
        print()
        print("--dry-run: not writing changes.")
        return 0

    # Sort top-level by key, but keep _meta first
    meta = flows.pop("_meta", None)
    sorted_flows: dict[str, Any] = {}
    if meta is not None:
        sorted_flows["_meta"] = meta
    for k in sorted(flows.keys()):
        sorted_flows[k] = flows[k]

    flows_file.write_text(json.dumps(sorted_flows, indent=2) + "\n")
    print()
    print(f"Wrote {flows_file} with {len(missing)} new stub(s).")
    print("Now edit those entries — replace the TODO fields with human-friendly text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
