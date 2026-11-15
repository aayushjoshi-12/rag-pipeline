"""Run the code blocks of docs/api.md against a REAL running stack, so the documented examples cannot rot.

A block is executed when the line before its fence is ``<!-- verify:<name> -->``. Bash blocks run with
``bash``, Python blocks with this interpreter (needs ``httpx``: run through ``uv run`` from ``backend/``),
JavaScript blocks with ``node`` (>= 18). The blocks read ``API``, ``EMAIL`` and ``PASSWORD`` from the
environment; this script sets them from ``--api``, ``--email`` and ``$API_PASSWORD``.

    cd backend && API_PASSWORD=... uv run python ../scripts/verify_api_docs.py \
        --api http://localhost:8100 --email you@example.com [--chat]

``curl-chat`` calls the LLM (costs tokens), so it only runs with ``--chat``. Blocks create and delete their
own synthetic document / role / key; nothing else is touched. Exit status 0 only if every block passes.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "api.md"
BLOCK = re.compile(r"<!-- verify:([\w-]+) -->\n```(\w+)\n(.*?)\n```", re.S)
RUNNERS = {
    "bash": lambda code: ["bash", "-c", code],
    "python": lambda code: [sys.executable, "-c", code],
    "javascript": lambda code: ["node", "--input-type=module", "-e", code],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--email", required=True)
    ap.add_argument("--chat", action="store_true", help="also run the block that calls the LLM")
    ap.add_argument("--only", help="run just this block name")
    args = ap.parse_args()
    if "API_PASSWORD" not in os.environ:
        print("set API_PASSWORD", file=sys.stderr)
        return 2
    env = {
        **os.environ,
        "API": args.api.rstrip("/"),
        "EMAIL": args.email,
        "PASSWORD": os.environ["API_PASSWORD"],
    }

    blocks = BLOCK.findall(DOC.read_text(encoding="utf-8"))
    if not blocks:
        print("no verify blocks found in docs/api.md", file=sys.stderr)
        return 2
    results: list[tuple[str, str]] = []
    for name, lang, code in blocks:
        if args.only and name != args.only:
            continue
        if name == "curl-chat" and not args.chat:
            results.append((name, "SKIPPED (no --chat)"))
            continue
        if lang not in RUNNERS:
            results.append((name, f"FAIL (unknown language {lang})"))
            continue
        if lang == "bash" and not (shutil.which("curl") and shutil.which("jq")):
            results.append((name, "FAIL (curl and jq are required)"))
            continue
        if lang == "javascript" and not shutil.which("node"):
            results.append((name, "FAIL (node is required)"))
            continue
        print(f"--- {name} ({lang})", flush=True)
        proc = subprocess.run(
            RUNNERS[lang](code), env=env, capture_output=True, text=True, timeout=300
        )
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
        results.append((name, "PASS" if proc.returncode == 0 else f"FAIL (exit {proc.returncode})"))
    print("\n| Block | Result |\n|---|---|")
    for name, res in results:
        print(f"| {name} | {res} |")
    failed = [n for n, r in results if r.startswith("FAIL")]
    print("\nOverall:", "ALL PASS" if not failed else f"FAILED ({', '.join(failed)})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
