"""Tests for the benchx-ci workbench scripts: remote, run_state, compact, schedule, command."""

import json
import shutil
import sys
import tarfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import command  # noqa: E402
import compact  # noqa: E402
import remote  # noqa: E402
import run_state  # noqa: E402
import schedule  # noqa: E402

KEY = "abc/m"
COMMIT = {"sha": "abc", "parents": ["p"], "author_date": "2026-01-01T00:00:00Z", "committer_date": "2026-01-01T00:00:00Z",
          "subject": "s", "branch": "main"}


def test_a_key_is_written_once(tmp_path):
    store, src = remote.DirRemote(tmp_path / "store"), tmp_path / "file"
    assert not store.get("k/a", tmp_path / "out") and store.list() == []
    src.write_text("one")
    store.put("k/a", src, once=True)
    src.write_text("two")
    with pytest.raises(remote.Exists):
        store.put("k/a", src, once=True)
    assert store.get("k/a", tmp_path / "out") and (tmp_path / "out").read_text() == "one"
    store.put("k/a", src)                         # without once, it is replaced
    store.put("k/b", src)
    assert [e.key for e in store.list("k/")] == ["k/a", "k/b"] and store.list("x/") == []


class Machine:
    """What scripts/bench-run.sh calls, against a directory."""

    def __init__(self, tmp_path):
        self.tmp, self.url = tmp_path, str(tmp_path / "raw")
        self.store = remote.remote(self.url)
        (tmp_path / "commit.json").write_text(json.dumps(COMMIT))

    def state(self, cmd, attempt, *args):
        file = ["--file", str(self.tmp / f"{attempt}.json")] if cmd != "record" else []
        assert run_state.main([cmd, self.url, KEY, "--attempt", attempt, *file, *args]) == 0

    def begin(self, attempt, *units):
        self.state("begin", attempt, "--commit", str(self.tmp / "commit.json"))
        if units:
            self.state("expect", attempt, *units)

    def record(self, attempt, unit, exit_code, results):
        out = self.tmp / "out" / attempt / unit
        out.mkdir(parents=True)
        for i in range(results):
            (out / f"case{i}.json").write_text("{}")
        (out / "workorder-1.json").write_text("{}")   # not a result
        self.state("record", attempt, unit, str(exit_code), str(out))

    def status(self):
        return run_state.read(self.store, KEY, self.tmp)


def test_a_restart_runs_only_what_is_missing(tmp_path, capsys):
    m = Machine(tmp_path)
    assert m.status() is None and run_state.wants_attempt(None, 1)
    m.begin("1", "a", "b", "c")
    m.record("1", "a", 0, 10)                     # recorded
    m.record("1", "b", 1, 0)                      # produced nothing: tried, not recorded
    status = m.status()                           # the machine died: no end
    assert status["state"] == "running" and (status["suites_ok"], status["suites_failed"]) == (1, 1)
    assert run_state.todo(status) == ["c", "b"]   # never tried first
    assert run_state.wants_attempt(status, 2) and not run_state.wants_attempt(status, 1)

    m.begin("2", "a", "b", "c")
    m.record("2", "c", 0, 5)
    m.record("2", "a", 0, 99)                     # a unit is written once: the second copy is dropped
    m.state("end", "2")
    status = m.status()
    assert status["state"] == "partial" and status["attempts"] == 2 and status["attempt_ids"] == ["1", "2"]
    with tarfile.open(tmp_path / "raw" / KEY / "units" / "a.tar.gz") as tar:
        assert json.load(tar.extractfile("meta.json")) == {"unit": "a", "exit": 0, "results": 10, "attempt": "1"}

    m.begin("3", "a", "b", "c")
    m.record("3", "b", 1, 3)                      # failed cases, but it produced results
    m.state("end", "3")
    status = m.status()
    assert status["state"] == "finished" and run_state.complete(status) and not run_state.wants_attempt(status, 99)
    capsys.readouterr()
    run_state.main(["todo", m.url, KEY, "a", "b", "c"])
    assert capsys.readouterr().out == ""


def test_a_build_failure_is_failed_and_retried(tmp_path):
    m = Machine(tmp_path)
    m.begin("1")
    m.state("end", "1", "--stage", "build")
    status = m.status()
    assert status["state"] == "failed" and status["stage"] == "build" and not run_state.complete(status)
    assert run_state.wants_attempt(status, 2) and not run_state.wants_attempt(status, 1)
    m.begin("2")
    assert m.status()["state"] == "running" and "stage" not in m.status()


def test_compaction_rebuilds_from_everything_and_only_on_change(tmp_path, capsys):
    m = Machine(tmp_path)
    runs = tmp_path / "runs"
    bx = tmp_path / "bx"                          # stands in for `bx ingest DIR`: one row per result file
    bx.write_text(f"#!{sys.executable}\n" + """import os, sys, pathlib, pyarrow as pa, pyarrow.parquet as pq
store = pathlib.Path(os.environ["BENCHX_HOME"]) / "store.parquet"
rows = pq.read_table(store).column("f").to_pylist() if store.exists() else []
d = pathlib.Path(sys.argv[2])
assert not (d / "meta.json").exists()
rows += [f"{d.name}/{p.name}" for p in sorted(d.glob("case*.json"))]
pq.write_table(pa.table({"f": rows}), store)
""")
    bx.chmod(0o755)

    def run():
        capsys.readouterr()
        assert compact.main(["--raw-url", m.url, "--runs-url", str(runs), "--bx", str(bx)]) == 0
        return capsys.readouterr().out.splitlines()[-1]

    m.begin("1", "a", "b")
    m.record("1", "a", 0, 2)
    (tmp_path / "log").write_text("first\n")
    m.store.put(f"{KEY}/attempts/1.log", tmp_path / "log")
    assert "running: 1 of 2 units, 2 results" in run()          # mid-run: what there is
    assert "unchanged" in run()
    row = pq.read_table(runs / KEY / "run.parquet").to_pylist()[0]
    assert (row["state"], row["suites_ok"], row["results"], row["log"], row["sha"]) == ("running", 1, 2, "first\n", "abc")

    m.begin("2", "a", "b")
    m.record("2", "b", 0, 3)
    m.state("end", "2")
    assert "finished: 2 of 2 units, 5 results" in run()
    assert pq.read_table(runs / KEY / "results.parquet").column("f").to_pylist() == [
        "a/case0.json", "a/case1.json", "b/case0.json", "b/case1.json", "b/case2.json"]
    assert json.loads((runs / KEY / "status.json").read_text())["attempts"] == 2
    assert "unchanged" in run()

    shutil.rmtree(runs)                           # runs/ is derived: it can be rebuilt
    assert "finished: 2 of 2 units, 5 results" in run()
    assert compact.main(["--raw-url", m.url, "--runs-url", str(runs), "--bx", str(bx), "--days", "0.0000001"]) == 0
    assert "abc" not in capsys.readouterr().out.replace("## compaction", "")   # too old to look at


def test_the_plan():
    done = {"expected": ["a"], "suites": {"a": {"recorded": True}}, "attempts": 1}
    partial = {"expected": ["a", "b"], "suites": {"a": {"recorded": True}}, "attempts": 1}
    spent = dict(partial, attempts=2)
    statuses = {("s1", "m"): done, ("s2", "m"): partial, ("s3", "m"): spent, ("s6", "m"): None}
    commits = [("s0", "GH-1: [Docs] words"), ("s1", "x"), ("s2", "x"), ("s3", "x"), ("s4", "x"), ("s5", "x"), ("s6", "x")]
    # Two runs are under way, one of them for a commit outside this list: both count.
    active = {"bench s4 m", "bench old m"}
    chosen, lines = schedule.plan(
        commits, ["m"], lambda sha, m: statuses.get((sha, m)), active,
        max_attempts=2, max_parallel=4, skip_tags=["[Docs]"])
    assert chosen == [("s2", "m"), ("s5", "m")]
    assert [verdict for _, _, verdict, _ in lines] == [
        "skipped by title", "complete", "dispatched again (attempt 2)", "gave up after 2 attempts",
        "running", "dispatched", "waiting (parallel limit)"]
    assert schedule.plan(commits, ["m"], lambda sha, m: statuses.get((sha, m)), active, max_attempts=2, max_parallel=2, skip_tags=["[Docs]"])[0] == []
    # Without skip_tags, s0 is not skipped.
    assert not schedule.skipped("GH-1: [Docs] words", [])


def test_a_filtered_attempt_leaves_the_pair_partial(tmp_path):
    m = Machine(tmp_path)
    m.begin("1", "a", "b", "c")                   # the commit has three units; the attempt runs one
    m.record("1", "a", 0, 4)
    m.state("end", "1")
    status = m.status()
    assert status["state"] == "partial" and not run_state.complete(status) and run_state.wants_attempt(status, 2)
    assert run_state.todo(status) == ["b", "c"] and run_state.todo(status, ["a"]) == []


def test_a_comment_is_parsed_into_a_request():
    machines = {"big": {"scheduled": False}, "small": {"scheduled": True}}
    parse = lambda body: command.parse(body, machines)  # noqa: E731
    assert parse("thanks, @github-actions bench abc") == (None, None)          # not at the start of a line
    assert parse("> @github-actions bench abc") == (None, None)                # a quoted command
    assert parse("@github-actions benchmarks abc") == (None, None)
    assert parse("please:\r\n@github-actions bench abc1234\r\nthanks")[0] == {
        "commit": "abc1234", "machine": "small", "suite_filter": ".", "budget_minutes": "240"}
    assert command.parse("@github-actions bench abc1234", {"big": {"scheduled": False}, "small": {"scheduled": False}})[0] == {
        "commit": "abc1234", "machine": "big", "suite_filter": ".", "budget_minutes": "240"}
    assert parse("@github-actions benchmark main machine=big filter=bit-util|csv budget=30")[0] == {
        "commit": "main", "machine": "big", "suite_filter": "bit-util|csv", "budget_minutes": "30"}
    assert parse("@github-actions bench abc budget=030")[0]["budget_minutes"] == "30"
    for body, problem in (("@github-actions bench", "no commit"), ("@github-actions bench filter=x", "no commit"),
                          ("@github-actions bench a..b", "does not look like a commit"),
                          ("@github-actions bench $(id)", "does not look like a commit"),
                          ("@github-actions bench abc machine=huge", "is not a machine"),
                          ("@github-actions bench abc budget=0", "budget is"), ("@github-actions bench abc budget=361", "budget is"),
                          ("@github-actions bench abc budget=1e3", "budget is"), ("@github-actions bench abc budget=\u00b2", "budget is"), ("@github-actions bench abc verbose", "is not an option"),
                          ("@github-actions bench abc sha=def", "is not an option"), ("@github-actions bench abc filter=", "is not an option")):
        request, said = parse(body)
        assert request is None and problem in said, body
