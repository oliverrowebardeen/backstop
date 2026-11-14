"""evals.finalize: dependency order, dry run, and stop-at-first-failure."""
import subprocess

from evals import finalize


def test_order_scores_before_analysis():
    names = [name for name, _ in finalize.STEPS]
    assert names[0] == "score the eval"
    assert names.index("frontier chart") < names.index("findings")


def test_dry_run_runs_nothing(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert finalize.main(["--dry-run"]) == 0
    assert "[1/5] score the eval" in capsys.readouterr().out


def test_stops_at_first_failure(monkeypatch, tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "x.jsonl").write_text("{}\n")
    monkeypatch.setattr(finalize, "OUT", tmp_path)
    calls = []

    class Done:
        def __init__(self, code):
            self.returncode = code

    def fake_run(cmd, cwd):
        calls.append(cmd)
        return Done(0 if len(calls) == 1 else 3)

    monkeypatch.setattr(finalize.subprocess, "run", fake_run)
    assert finalize.main([]) == 3
    assert len(calls) == 2  # score ran, frontier failed, nothing after


def test_no_logs_is_a_clear_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(finalize, "OUT", tmp_path)
    assert finalize.main([]) == 2
    assert "run python -m evals.run_eval first" in capsys.readouterr().err
