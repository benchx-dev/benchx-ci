# benchx-ci

Reusable GitHub Actions + AWS EC2/S3 CI workbench for [benchx](https://github.com/benchx-dev/benchx).

`benchx-ci` implements the CI workbench layer described in [`ci-workbench.md`](https://github.com/benchx-dev/benchx/blob/main/docs/design/ci-workbench.md):

- **Ephemeral EC2 Spot/on-demand runners** launched per `(commit, machine)` with 3-AZ failover and automatic teardown.
- **Write-once S3 raw staging & forward-only restarts** (`scripts/run_state.py`, `scripts/bench-run.sh`): each benchmark unit uploads immediately so timeouts or Spot interruptions lose at most the unit in flight.
- **Idempotent compaction & publishing** (`scripts/compact.py`, `bx view`): compacts raw uploads into `runs/<sha>/<machine>/{results,run}.parquet` and builds a static dashboard for GitHub Pages.
- **Stateless scheduler & comment commands** (`scripts/schedule.py`, `scripts/command.py`): tracks upstream commits and handles `@github-actions bench <commit>` requests with zero database.

## Using `benchx-ci` in a project repository

A project benchmark repository (such as [`benchx-dev/arrow`](https://github.com/benchx-dev/arrow)) only needs:

1. **`project.json`** — project name, title, upstream repo/branch/commit URL, and optional `skip_commit_tags`.
2. **`benchx.conf.json`** — your target provider command and harness adapter.
3. **`machines.json`** — EC2 instance specs keyed by logical machine name.
4. **Provider & discovery scripts** (e.g. `provider/`):
   - target provider (`prepare` / `describe`),
   - optional `setup-env.sh` and `run-in-env.sh` wrapper,
   - `discover.sh <build_dir>` printing one runnable suite per line (`<suite>` or `<unit>\t<suite>`).
5. **Four thin caller workflows** under `.github/workflows/` invoking `benchx-dev/benchx-ci/.github/workflows/{bench,publish,schedule,command}.yml@main`.

## AWS setup

Deploy [`aws/setup.yaml`](aws/setup.yaml) once per project repository and copy its outputs into GitHub repository variables:

```bash
aws cloudformation deploy --region us-east-2 --stack-name benchx-<project> \
  --template-file aws/setup.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubRepository=<owner>/<repo>

aws cloudformation describe-stacks --region us-east-2 --stack-name benchx-<project> \
  --query 'Stacks[0].Outputs[].[Description,OutputValue]' --output text |
  while read -r name value; do gh variable set "$name" --repo <owner>/<repo> --body "$value"; done
```

Also set:
- Repository secret `GH_RUNNER_TOKEN` (fine-grained PAT with **Administration: Read and write** on the benchmark repo for ephemeral runner registration).
- Repository variable `PAGES_ENABLED=true` once GitHub Pages (source: GitHub Actions) is enabled.
- Repository variable `SCHEDULE_ENABLED=true` when ready for automated hourly scheduling.
