#!/usr/bin/env python3
"""
git_blame_lookup.py
Fetch the last-committer name for a subdirectory in a local git repo.

Used by generate_dashboard.py to show "who last touched this Lambda function"
in the expanded alert detail view — a blame signal for triage.

Silent fallback: returns "" on any failure (missing repo, unknown path, timeout).
Dashboard render treats "" as "hide the field".
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=256)
def get_last_author(local_path: str, subdir: str) -> str:
    """
    Return the name of the author of the most recent commit touching `subdir/`
    inside the git repo at `local_path`. Empty string on any failure.
    """
    if not local_path or not subdir:
        return ""
    repo = Path(local_path)
    if not (repo / ".git").exists():
        return ""
    target = repo / subdir
    if not target.exists():
        return ""

    try:
        proc = subprocess.run(
            ["git", "log", "--format=%an", "-1", "--", f"{subdir}/"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python3 git_blame_lookup.py <local_path> <subdir>")
        sys.exit(1)
    print(get_last_author(sys.argv[1], sys.argv[2]))
