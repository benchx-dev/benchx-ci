#!/usr/bin/env python3
"""Dispatch the bench workflow for commits a machine still has to measure.

For the newest commits on the upstream branch (project.json) and every scheduled
machine (machines.json), derive the pair's status from what the machine left in
the raw store (scripts/run_state.py). A pair is dispatched when nothing was ever
uploaded for it, or it is incomplete with attempts left, and no bench run for it is
queued or under way. No more than --max-parallel bench runs are under way at once.
The store is the only state.

Prints a Markdown summary. Needs `gh` with a token that may run workflows.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import remote  # noqa: E402
import run_state  # noqa: E402

ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def upstream_commits(project: dict, lookback: int) -> list[tuple[str, str]]:
    """(sha, title) of the newest commits on the upstream branch, newest first."""
    slug = project["upstream"]["url"].removesuffix(".git").split("github.com/")[1]
    out = gh("api", f"repos/{slug}/commits?sha={project['upstream']['branch']}&per_page={lookback}",
             "--jq", ".[] | [.sha, (.commit.message | split(\"\\n\")[0])] | @tsv")
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if line]


def active_runs(repo: str) -> set[str]:
    """Titles of bench runs that are queued or running: 'bench <sha> <machine>'."""
    try:
        runs = json.loads(gh("run", "list", "--repo", repo, "--workflow", "bench.yml", "--limit", "200", "--json", "displayTitle,status"))
    except subprocess.CalledProcessError as error:
        if "actions/workflows/bench.yml" not in error.stderr or "404" not in error.stderr:
            raise
        print("schedule: bench.yml is not on the default branch yet; nothing can be running", file=sys.stderr)
        return set()
    return {run["displayTitle"] for run in runs if run["status"] in ACTIVE}


def skipped(title: str, skip_tags: list[str] | tuple[str, ...] = ()) -> bool:
    return any(tag.lower() in title.lower() for tag in skip_tags)


def plan(commits, machines, status_of, active, max_attempts, max_parallel, skip_tags=()):
    """The (sha, machine) pairs to dispatch, and a line about every pair looked at.

    Every bench run that is queued or under way, whoever started it, is an
    instance that counts toward max_parallel.
    """
    chosen, lines = [], []
    for sha, title in commits:
        if skipped(title, skip_tags):
            lines.append((sha, "-", "skipped by title", title))
            continue
        for machine in machines:
            status = status_of(sha, machine)
            if f"bench {sha} {machine}" in active:
                verdict = "running"
            elif not run_state.wants_attempt(status, max_attempts):
                verdict = "complete" if run_state.complete(status) else f"gave up after {status.get('attempts')} attempts"
            elif len(active) + len(chosen) >= max_parallel:
                verdict = "waiting (parallel limit)"
            else:
                chosen.append((sha, machine))
                verdict = "dispatched" if status is None else f"dispatched again (attempt {int(status.get('attempts') or 0) + 1})"
            lines.append((sha, machine, verdict, title))
    return chosen, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raw-url", required=True, help="where machines upload (s3://bucket/raw, or a directory)")
    ap.add_argument("--repo", required=True, help="this repository, owner/name")
    ap.add_argument("--root", type=Path, default=Path("."), help="project repository root containing project.json")
    ap.add_argument("--machines", type=Path, default=Path("machines.json"), help="path to machines.json (relative to --root if relative)")
    ap.add_argument("--lookback", type=int, default=10, help="how many upstream commits to consider")
    ap.add_argument("--max-attempts", type=int, default=2)
    ap.add_argument("--max-parallel", type=int, default=4, help="bench runs, and so instances, under way at once")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    project = json.loads((args.root / "project.json").read_text())
    machines_path = args.machines if args.machines.is_absolute() else args.root / args.machines
    machines = [name for name, m in json.loads(machines_path.read_text()).items() if m.get("scheduled")]
    skip_tags = project.get("skip_commit_tags") or []
    store = remote.remote(args.raw_url)
    with tempfile.TemporaryDirectory() as scratch:
        chosen, lines = plan(
            upstream_commits(project, args.lookback), machines,
            lambda sha, machine: run_state.read(store, f"{sha}/{machine}", Path(scratch)),
            active_runs(args.repo), args.max_attempts, args.max_parallel, skip_tags=skip_tags)
    for sha, machine in chosen:
        if not args.dry_run:
            gh("workflow", "run", "bench.yml", "--repo", args.repo, "-f", f"sha={sha}", "-f", f"machine={machine}")
    print(f"## schedule{' (dry run)' if args.dry_run else ''}\n\n| commit | machine | | title |\n|---|---|---|---|")
    for sha, machine, verdict, title in lines:
        print(f"| `{sha[:12]}` | {machine} | {verdict} | {title.replace('|', '/')} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
