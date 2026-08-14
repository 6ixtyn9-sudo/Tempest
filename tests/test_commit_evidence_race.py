"""Regression test for the 2026-08-14 Paper Poll #80 failure.

Daily Capture #9 and Paper Poll #80 were both dispatched at 13:30:02Z. Both
appended rows to localdata/paper_status.csv. The capture pushed first; the
poll's `git pull --rebase` hit a content conflict on that file, the retry
loop then ran straight into "Pulling is not possible because you have
unmerged files" twice more and failed the run.

Guards here:
  * a concurrent append from two clones must not fail the losing run
  * both runs' rows must survive (union merge, not last-writer-wins)
  * rows must come out de-duplicated and in timestamp order
  * a failed rebase must never leave the tree with unmerged files
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "commit_evidence.sh"
ATTRIBUTES = REPO_ROOT / ".gitattributes"

HEADER = "ts,event,detail\n"
BASE_ROW = "2026-08-14T13:00:00,equity,100000.00\n"


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check,
        capture_output=True, text=True,
    )


def make_clone(origin, path):
    git("clone", str(origin), str(path), cwd=path.parent)
    git("config", "user.email", "t@example.com", cwd=path)
    git("config", "user.name", "tester", cwd=path)
    return path


def run_commit_evidence(clone, message):
    return subprocess.run(
        ["bash", str(clone / "scripts" / "commit_evidence.sh"), message],
        cwd=clone, capture_output=True, text=True, timeout=300,
    )


@pytest.fixture
def origin_and_clones(tmp_path):
    origin = tmp_path / "origin.git"
    git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    seed = make_clone(origin, tmp_path / "seed")
    (seed / "localdata").mkdir()
    (seed / "scripts").mkdir()
    (seed / "localdata" / "paper_status.csv").write_text(HEADER + BASE_ROW)
    (seed / "scripts" / "commit_evidence.sh").write_text(SCRIPT.read_text())
    (seed / "scripts" / "commit_evidence.sh").chmod(0o755)
    (seed / ".gitattributes").write_text(ATTRIBUTES.read_text())
    git("add", "-A", cwd=seed)
    git("commit", "-m", "base", cwd=seed)
    git("push", "origin", "main", cwd=seed)

    return origin, make_clone(origin, tmp_path / "a"), make_clone(origin, tmp_path / "b")


def test_concurrent_appends_both_survive(origin_and_clones):
    """The losing run must succeed and keep both runs' rows."""
    origin, capture, poll = origin_and_clones
    status = "localdata/paper_status.csv"

    with (capture / status).open("a") as fh:
        fh.write("2026-08-14T13:32:01,pass_done,capture-run\n")
    with (poll / status).open("a") as fh:
        fh.write("2026-08-14T13:32:03,pass_done,poll-run\n")

    first = run_commit_evidence(capture, "capture")
    assert first.returncode == 0, first.stdout + first.stderr

    # This is the call that failed in production.
    second = run_commit_evidence(poll, "poll")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "unmerged files" not in (second.stdout + second.stderr)

    git("pull", "--rebase", "origin", "main", cwd=capture)
    rows = (capture / status).read_text().splitlines()
    assert any("capture-run" in r for r in rows), "capture rows lost"
    assert any("poll-run" in r for r in rows), "poll rows lost"


def test_rows_are_sorted_and_deduplicated(origin_and_clones):
    """A union merge must not leave duplicate or out-of-order rows."""
    origin, capture, poll = origin_and_clones
    status = "localdata/paper_status.csv"

    # Overlapping identical row plus out-of-order timestamps on both sides.
    with (capture / status).open("a") as fh:
        fh.write("2026-08-14T13:40:00,equity,100000.00\n")
        fh.write("2026-08-14T13:20:00,client_ok,shared\n")
    with (poll / status).open("a") as fh:
        fh.write("2026-08-14T13:20:00,client_ok,shared\n")
        fh.write("2026-08-14T13:35:00,pass_done,poll\n")

    assert run_commit_evidence(capture, "capture").returncode == 0
    assert run_commit_evidence(poll, "poll").returncode == 0

    git("pull", "--rebase", "origin", "main", cwd=capture)
    body = (capture / status).read_text().splitlines()[1:]

    stamps = [r.split(",", 1)[0] for r in body]
    assert stamps == sorted(stamps), f"rows not chronological: {stamps}"
    assert len(body) == len(set(body)), "duplicate rows survived the merge"
    assert sum("client_ok,shared" in r for r in body) == 1


def test_no_unmerged_files_left_behind(origin_and_clones):
    """After a race the working tree must be clean, never mid-rebase."""
    origin, capture, poll = origin_and_clones
    status = "localdata/paper_status.csv"

    with (capture / status).open("a") as fh:
        fh.write("2026-08-14T13:32:01,pass_done,capture\n")
    with (poll / status).open("a") as fh:
        fh.write("2026-08-14T13:32:03,pass_done,poll\n")

    run_commit_evidence(capture, "capture")
    run_commit_evidence(poll, "poll")

    unmerged = git("diff", "--name-only", "--diff-filter=U", cwd=poll).stdout
    assert unmerged.strip() == "", f"unmerged files left: {unmerged}"
    assert not (poll / ".git" / "rebase-merge").exists()
    assert not (poll / ".git" / "rebase-apply").exists()


def test_noop_when_nothing_changed(origin_and_clones):
    """Idempotence: no changes must exit 0 without pushing."""
    origin, capture, _ = origin_and_clones
    result = run_commit_evidence(capture, "nothing")
    assert result.returncode == 0
    assert "No Tempest data changes" in result.stdout
