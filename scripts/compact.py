#!/usr/bin/env python3
"""Turn what ephemeral machines uploaded into the Parquet files `bx view` reads.

Machines leave raw objects under <raw>/<sha>/<machine>/ (scripts/run_state.py). For
every pair whose raw objects changed, this rebuilds, from all of them:

  <runs>/<sha>/<machine>/results.parquet   a benchx store of every recorded result
  <runs>/<sha>/<machine>/run.parquet       the dashboard's row about the run
  <runs>/<sha>/<machine>/status.json       the derived status, and what it was built from

It always rebuilds a pair whole and never deletes, so it may run at any time, also
while a machine is still uploading, and running it twice changes nothing. Raw
objects are expired by a bucket lifecycle rule; --days must stay well below that
age, or a pair would be rebuilt from half of its objects.
"""

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path

from benchx.views import write_run

sys.path.insert(0, str(Path(__file__).parent))
import remote  # noqa: E402
import run_state  # noqa: E402


def compact(raw, runs, key: str, entries: list, work: Path, bx: str) -> str:
    """Rebuild one pair. Returns a line for the summary."""
    scratch = work / "scratch"
    scratch.mkdir(parents=True)
    attempts = run_state.read_attempts(raw, key, entries, scratch)
    status = run_state.derive(key, entries, attempts)
    previous = remote.read_json(runs, f"{key}/status.json", scratch)
    if previous and previous.get("sources") == status["sources"]:
        return "unchanged"
    if not attempts:
        return "skipped: no attempt file"

    problems = 0
    for unit in sorted(u for u, s in status["suites"].items() if run_state.recorded(s)):
        archive, out = scratch / f"{unit}.tar.gz", work / "suites" / unit
        if not raw.get(f"{key}/units/{unit}.tar.gz", archive):
            continue   # listed, then expired: leave the unit out rather than fail
        out.mkdir(parents=True)
        with tarfile.open(archive) as tar:
            tar.extractall(out, filter="data")
        meta = json.loads((out / "meta.json").read_text())
        status["suites"][unit].update(exit=meta["exit"], results=meta["results"])
        (out / "meta.json").unlink()
        ingest = subprocess.run([bx, "ingest", str(out)], env={**os.environ, "BENCHX_HOME": str(work)}, capture_output=True, text=True)
        if ingest.returncode != 0:
            problems += 1
            print(f"compact: ingest problems in {key} {unit}:\n{(ingest.stdout + ingest.stderr)[-2000:]}", file=sys.stderr)

    attempts.sort(key=lambda a: a.get("started_at") or "")
    log = ""
    for attempt in attempts:
        dest = scratch / f"{attempt['attempt']}.log"
        if raw.get(f"{key}/attempts/{attempt['attempt']}.log", dest):
            log += dest.read_text(errors="replace")
    write_run(work / "filed", attempts[-1]["commit_info"], status, log, problems)
    (work / "filed" / "status.json").write_text(json.dumps(status, indent=1))

    # status.json last: it is what marks the pair as compacted.
    if (work / "store.parquet").is_file():
        runs.put(f"{key}/results.parquet", work / "store.parquet")
    runs.put(f"{key}/run.parquet", work / "filed" / "run.parquet")
    runs.put(f"{key}/status.json", work / "filed" / "status.json")
    return f"{status['state']}: {status['suites_ok']} of {len(status['expected'])} units, {sum(s.get('results', 0) for s in status['suites'].values())} results"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raw-url", required=True, help="where machines upload (s3://bucket/raw, or a directory)")
    ap.add_argument("--runs-url", required=True, help="where the Parquet files go (s3://bucket/runs, or a directory)")
    ap.add_argument("--days", type=float, default=3, help="look at pairs with an upload in the last DAYS days (0: all)")
    ap.add_argument("--bx", default=shutil.which("bx") or "bx")
    args = ap.parse_args(argv)

    raw, runs = remote.remote(args.raw_url), remote.remote(args.runs_url)
    pairs = defaultdict(list)
    for entry in raw.list():
        parts = entry.key.split("/")
        if len(parts) >= 4:
            pairs["/".join(parts[:2])].append(entry)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days) if args.days else None
    print("## compaction\n\n| commit | machine | |\n|---|---|---|")
    for key, entries in sorted(pairs.items()):
        if cutoff and max(e.modified for e in entries) < cutoff:
            continue
        with tempfile.TemporaryDirectory() as work:
            verdict = compact(raw, runs, key, entries, Path(work), args.bx)
        sha, machine = key.split("/")
        print(f"| `{sha[:12]}` | {machine} | {verdict} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
