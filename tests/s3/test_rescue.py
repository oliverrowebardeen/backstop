import pytest

from analysis.rescue_table import LearnOnlyError, build
from tests.s2.fake_contracts import Question


def q(i, split="learn", trap=False):
    return Question(id=i, question="q", gold=["g"], category="trap" if trap else "fact", source="t",
                    split=split, is_trap=trap, trap_note="u" if trap else None)


def row(qid, engine, hit):
    return {"question_id": qid, "engine": engine, "hit": hit}


ENG = ["tavily", "exa", "brave"]


def test_refuses_test_and_trap_rows():
    qs = [q("a"), q("b", "test"), q("c", "test", True)]
    for bad in ("b", "c"):
        rows = [row("a", "tavily", False), row(bad, "tavily", False)]
        with pytest.raises(LearnOnlyError):
            build(rows, qs, ENG)


def test_rescue_count_and_order():
    qs = [q("a"), q("b"), q("c"), q("d")]
    rows = []
    # tavily misses a,b,c; exa hits a,b; brave hits a
    for qid, t, e, b in [("a", 0, 1, 1), ("b", 0, 1, 0), ("c", 0, 0, 0), ("d", 1, 1, 1)]:
        rows += [row(qid, "tavily", bool(t)), row(qid, "exa", bool(e)), row(qid, "brave", bool(b))]
    t = build(rows, qs, ENG)
    assert t["miss_counts"]["tavily"] == 3
    assert t["rescue"]["tavily"]["exa"] == {"rescued": 2, "misses": 3, "rate": round(2 / 3, 4)}
    assert t["fallback_order"] == ["exa", "brave"]
