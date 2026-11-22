#!/usr/bin/env python3
"""Dispatch the bench workflow from a comment.

A line of a comment on any issue or pull request of this repository:

  @github-actions bench <commit> [machine=<name>] [filter=<regex>] [budget=<minutes>]

<commit> is a sha (short or full), a tag or a branch of the upstream repository
(project.json). It must be on the upstream branch: every run is filed into that
branch's series, so a commit from elsewhere would be a point that does not belong.
machine is a name from machines.json (default: the first scheduled one, or the first listed),
filter a regex over benchmark units (default: all), budget the minutes after which no new
unit is started (default 240).

The comment is in COMMENT_BODY. The answer, what was dispatched or what was wrong
with the request, is posted as a comment on the same issue. Needs `gh` with a token
that may run workflows and comment.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from schedule import gh  # noqa: E402

COMMAND = re.compile(r"^@github-actions[ \t]+bench(?:mark)?(?:[ \t]+(.*))?$", re.M)
COMMIT = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]{0,99}")
USAGE = "`@github-actions bench <commit> [machine=<name>] [filter=<regex>] [budget=<minutes>]`"
# The schedule workflow terminates instances older than eight hours, and an
# instance lives for the budget plus up to two hours.
MAX_BUDGET = 360


def parse(body: str, machines: dict) -> tuple[dict | None, str | None]:
    """(request, None), (None, what is wrong), or (None, None) when the comment has no command."""
    match = COMMAND.search(body.replace("\r", ""))
    if not match:
        return None, None
    words = (match.group(1) or "").split()
    if not words or "=" in words[0]:
        return None, "no commit was named"
    commit = words[0]
    if not COMMIT.fullmatch(commit) or ".." in commit:
        return None, f"`{commit}` does not look like a commit"
    default_machine = next((name for name, m in machines.items() if m.get("scheduled")), next(iter(machines), None))
    request = {"commit": commit, "machine": default_machine,
               "suite_filter": ".", "budget_minutes": "240"}
    for word in words[1:]:
        key, equals, value = word.partition("=")
        if not equals or not value or key not in ("machine", "filter", "budget"):
            return None, f"`{word}` is not an option"
        request[{"filter": "suite_filter", "budget": "budget_minutes"}.get(key, key)] = value
    if request["machine"] not in machines:
        return None, f"`{request['machine']}` is not a machine; there are: {', '.join(f'`{m}`' for m in machines)}"
    if not re.fullmatch(r"[0-9]{1,4}", request["budget_minutes"]) or not 1 <= int(request["budget_minutes"]) <= MAX_BUDGET:
        return None, f"budget is a number of minutes from 1 to {MAX_BUDGET}"
    request["budget_minutes"] = str(int(request["budget_minutes"]))   # 030 would be octal to the shell that adds to it
    if len(request["suite_filter"]) > 200:
        return None, "the filter is too long"
    return request, None


def resolve(slug: str, branch: str, commit: str) -> tuple[str | None, str | None]:
    """(the full sha, None) when the commit is on the upstream branch, else (None, what is wrong)."""
    try:
        sha = gh("api", f"repos/{slug}/commits/{commit}", "--jq", ".sha").strip()
        # With the commit as the base, the branch is ahead of it or identical exactly
        # when the commit is one of the branch's.
        status = gh("api", f"repos/{slug}/compare/{sha}...{branch}", "--jq", ".status").strip()
    except subprocess.CalledProcessError:
        return None, f"`{commit}` is not a commit of {slug}"
    if status not in ("ahead", "identical"):
        return None, f"`{sha[:12]}` is not on `{branch}` of {slug}; only commits of that branch are measured"
    return sha, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="this repository, owner/name")
    ap.add_argument("--issue", required=True, help="the issue or pull request the comment is on")
    ap.add_argument("--root", type=Path, default=Path("."), help="project repository root containing project.json")
    ap.add_argument("--machines", type=Path, default=Path("machines.json"), help="path to machines.json (relative to --root if relative)")
    ap.add_argument("--dry-run", action="store_true", help="print the answer; dispatch and post nothing")
    args = ap.parse_args(argv)

    project = json.loads((args.root / "project.json").read_text())
    machines_path = args.machines if args.machines.is_absolute() else args.root / args.machines
    machines = json.loads(machines_path.read_text())
    slug = project["upstream"]["url"].removesuffix(".git").split("github.com/")[1]
    request, problem = parse(os.environ.get("COMMENT_BODY", ""), machines)
    if request is None and problem is None:
        print("command: no command in the comment")
        return 0
    sha = None
    if request:
        sha, problem = resolve(slug, project["upstream"]["branch"], request["commit"])
    if sha:
        if not args.dry_run:
            gh("workflow", "run", "bench.yml", "--repo", args.repo, "-f", f"sha={sha}", "-f", f"machine={request['machine']}",
               "-f", f"suite_filter={request['suite_filter']}", "-f", f"budget_minutes={request['budget_minutes']}")
        answer = (f"{'Would dispatch' if args.dry_run else 'Dispatched'}: {slug}@`{sha}` on `{request['machine']}`, filter `{request['suite_filter']}`, "
                  f"budget {request['budget_minutes']} minutes. "
                  f"[Runs](https://github.com/{args.repo}/actions/workflows/bench.yml)")
    else:
        answer = f"Not dispatched: {problem}.\n\nUsage: {USAGE}"
    print(answer)
    if not args.dry_run:
        gh("issue", "comment", args.issue, "--repo", args.repo, "--body", answer)
    return 0 if sha else 1


if __name__ == "__main__":
    sys.exit(main())
