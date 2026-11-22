#!/usr/bin/env python3
"""What an ephemeral machine leaves in the raw store for one commit, and what it means.

A machine only adds objects, under <sha>/<machine>/ (KEY):

  attempts/<id>.json        one per attempt, written by that attempt alone: when it
                            began, the commit, the units the commit has, how it ended
  attempts/<id>.log         its log
  units/<unit>.tar.gz       one per recorded unit (benchmark suite), written once:
                            the result files and meta.json (exit code, result count)
  failed/<unit>.<id>.json   a unit an attempt ran that produced nothing

The status of the pair is derived from the listing, never stored: a unit is recorded
when its archive exists, an attempt was made when its file exists. So a restart runs
only what is missing, and the scheduler knows when to stop asking.

  run_state.py begin  URL KEY --attempt ID --file F --commit COMMIT.json [--reason R]
  run_state.py expect URL KEY --attempt ID --file F UNIT...   the units this commit has
  run_state.py todo   URL KEY UNIT...        of these, the ones to run, never-tried first
  run_state.py record URL KEY --attempt ID UNIT EXIT DIR      upload a unit's results
  run_state.py end    URL KEY --attempt ID --file F [--stage S]
  run_state.py status URL KEY                the derived status, as JSON

F is the attempt's file, kept locally between calls. States: running (an attempt is
under way, or died without saying so), finished (every expected unit is recorded),
partial (some are missing), failed (the last attempt stopped before any unit could
run, at --stage).
"""

import argparse
import json
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import remote  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_result(name: str) -> bool:
    return name.endswith(".json") and name != "meta.json" and "workorder-" not in name and not name.endswith("_load-average.json")


def recorded(unit: dict | None) -> bool:
    return bool(unit) and bool(unit.get("recorded"))


def todo(status: dict | None, units: list[str] | None = None) -> list[str]:
    """Units still to run: those never tried first, then those that produced nothing."""
    suites = (status or {}).get("suites") or {}
    units = (status or {}).get("expected") or [] if units is None else units
    missing = [u for u in units if not recorded(suites.get(u))]
    return [u for u in missing if u not in suites] + [u for u in missing if u in suites]


def complete(status: dict | None) -> bool:
    return bool(status) and bool(status.get("expected")) and not todo(status)


def wants_attempt(status: dict | None, max_attempts: int) -> bool:
    """Should the scheduler dispatch this commit and machine (again)?"""
    if status is None:
        return True
    return not complete(status) and int(status.get("attempts") or 0) < max_attempts


def derive(key: str, entries: list, attempts: list[dict]) -> dict:
    """The status of a pair from its listing and its attempt files."""
    sha, machine = key.split("/")
    names = [(e.key[len(key) + 1:].partition("/")[0], e.key[len(key) + 1:].partition("/")[2]) for e in entries]
    suites = {name.removesuffix(".json").partition(".")[0]: {"recorded": False} for kind, name in names if kind == "failed"}
    suites.update({name.removesuffix(".tar.gz"): {"recorded": True} for kind, name in names if kind == "units"})
    attempts = sorted(attempts, key=lambda a: a.get("started_at") or "")
    last = attempts[-1] if attempts else {}
    status = {
        "node": machine, "commit": sha, "reason": last.get("reason"),
        "attempts": len(attempts), "attempt_ids": [a.get("attempt") for a in attempts],
        "started_at": attempts[0].get("started_at") if attempts else None,
        "expected": next((a["expected"] for a in reversed(attempts) if a.get("expected")), []),
        "suites": suites,
        "suites_ok": sum(1 for u in suites.values() if recorded(u)),
        "suites_failed": sum(1 for u in suites.values() if not recorded(u)),
        # What this status was derived from; the compaction skips a pair whose
        # sources have not changed.
        "sources": {e.key[len(key) + 1:]: e.etag for e in entries},
    }
    ended = max((a["finished_at"] for a in attempts if a.get("finished_at")), default=None)
    if complete(status):
        status.update(state="finished", finished_at=ended)
    elif not last.get("finished_at"):
        status.update(state="running")
    elif last.get("stage"):
        status.update(state="failed", stage=last["stage"], finished_at=ended)
    else:
        status.update(state="partial", finished_at=ended)
    return status


def read_attempts(store, key: str, entries: list, scratch: Path) -> list[dict]:
    files = [e.key for e in entries if e.key.startswith(f"{key}/attempts/") and e.key.endswith(".json")]
    return [doc for doc in (remote.read_json(store, f, scratch) for f in files) if doc]


def read(store, key: str, scratch: Path) -> dict | None:
    """The derived status of <sha>/<machine>, or None when nothing was ever written."""
    entries = store.list(f"{key}/")
    return derive(key, entries, read_attempts(store, key, entries, scratch)) if entries else None


def record(store, key: str, attempt: str, unit: str, exit_code: int, directory: Path, scratch: Path) -> str:
    """Upload what a unit produced. Returns a line for the log."""
    files = sorted(p for p in directory.iterdir() if p.is_file()) if directory.is_dir() else []
    results = sum(1 for p in files if is_result(p.name))
    meta = Path(scratch) / "meta.json"
    meta.write_text(json.dumps({"unit": unit, "exit": exit_code, "results": results, "attempt": attempt}))
    if results == 0 and exit_code != 0:
        store.put(f"{key}/failed/{unit}.{attempt}.json", meta)
        return f"{unit}: exit {exit_code} and no results; not recorded"
    archive = Path(scratch) / f"{unit}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in files:
            if path.name != "meta.json":
                tar.add(path, arcname=path.name)
        tar.add(meta, arcname="meta.json")
    try:
        store.put(f"{key}/units/{unit}.tar.gz", archive, once=True)
    except remote.Exists:
        return f"{unit}: already recorded by another attempt; this one's results are dropped"
    return f"{unit}: {results} results recorded"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    def command(name, attempt=False):
        p = sub.add_parser(name)
        p.add_argument("url"), p.add_argument("key", help="<sha>/<machine>")
        if attempt:
            p.add_argument("--attempt", required=True)
        return p

    for name in ("begin", "expect", "end"):
        p = command(name, attempt=True)
        p.add_argument("--file", required=True, type=Path, help="the attempt's file, kept locally")
        if name == "begin":
            p.add_argument("--commit", required=True, type=Path), p.add_argument("--reason", default=None)
        elif name == "expect":
            p.add_argument("units", nargs="*")
        else:
            p.add_argument("--stage", default=None)
    command("todo").add_argument("units", nargs="*")
    r = command("record", attempt=True)
    r.add_argument("unit"), r.add_argument("exit", type=int), r.add_argument("directory", type=Path)
    command("status")
    args = ap.parse_args(argv)

    store = remote.remote(args.url)
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        if args.command == "status":
            print(json.dumps(read(store, args.key, scratch), indent=1))
            return 0
        if args.command == "todo":
            for unit in todo(read(store, args.key, scratch), args.units):
                print(unit)
            return 0
        if args.command == "record":
            print(record(store, args.key, args.attempt, args.unit, args.exit, args.directory, scratch))
            return 0
        sha, machine = args.key.split("/")
        if args.command == "begin":
            doc = {"attempt": args.attempt, "node": machine, "commit": sha, "reason": args.reason,
                   "started_at": now(), "commit_info": json.loads(args.commit.read_text())}
        else:
            doc = json.loads(args.file.read_text())
        if args.command == "expect":
            doc["expected"] = args.units
        elif args.command == "end":
            status = read(store, args.key, scratch)
            doc.update(finished_at=now(), state="failed" if args.stage else "finished" if complete(status) else "partial")
            if args.stage:
                doc["stage"] = args.stage
        args.file.write_text(json.dumps(doc, indent=1))
        store.put(f"{args.key}/attempts/{args.attempt}.json", args.file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
