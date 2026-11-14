"""score(so_log_dir=...): headline second_opinion from an extra pass, main-eval one as the comparison."""
import asyncio

from data.load import load_questions, load_traps
from evals import run_eval, score


def test_extra_pass_becomes_headline_with_comparison(eval_fakes, tmp_path):
    test = sorted(load_questions(split="test"), key=lambda q: q.id)[:4]
    traps = load_traps()[:1]
    main_dir = run_eval.default_log_dir()
    extra_dir = tmp_path / "out" / "logs_extra"
    run_eval.write_manifest(test, traps, list(run_eval.SETUPS), None, None, False)
    asyncio.run(run_eval.run(test, traps, list(run_eval.SETUPS), main_dir, 4))
    asyncio.run(run_eval.run(test, traps, ["second_opinion"], extra_dir, 4))
    assert run_eval.manifest_path(extra_dir) != run_eval.manifest_path(main_dir)

    r = asyncio.run(score.score(so_log_dir=extra_dir, so_label="cascade at 0.7"))
    assert set(r["setups"]) == {"single", "telem_default", "second_opinion"}
    assert r["headline"]["second_opinion"] == "cascade at 0.7"
    assert score.COMPARE in r["comparison"] and "checker" in r["comparison"]
    assert f"second_opinion_vs_{score.COMPARE}" in r["paired"]
    assert r["setups"]["second_opinion"]["n"] == r["comparison"][score.COMPARE]["n"] == 4
    md = score.render_md(r)
    assert "Headline second_opinion: cascade at 0.7" in md and "second_opinion_llm_checker" in md


def test_without_extra_pass_nothing_changes(eval_fakes):
    test = sorted(load_questions(split="test"), key=lambda q: q.id)[:3]
    run_eval.write_manifest(test, [], list(run_eval.SETUPS), None, None, False)
    asyncio.run(run_eval.run(test, [], list(run_eval.SETUPS), run_eval.default_log_dir(), 3))
    r = asyncio.run(score.score())
    assert "comparison" not in r and "headline" not in r
    assert set(r["paired"]) == {"second_opinion_vs_single", "second_opinion_vs_telem_default"}
