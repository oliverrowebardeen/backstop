import re
from collections import Counter

from data.build_questions import stratified_split
from data.load import load_all, load_questions, load_traps


def test_counts_and_split():
    qs = load_questions()
    assert len(qs) == 300
    assert Counter(q.split for q in qs) == {"learn": 100, "test": 200}
    assert len({q.id for q in qs + load_traps()}) == 330


def test_split_is_stratified():
    qs = load_questions()
    for cat in ("fact", "finance"):
        sub = [q for q in qs if q.category == cat]
        share = sum(q.split == "learn" for q in sub) / len(sub)
        assert abs(share - 1 / 3) < 0.02, (cat, share)


def test_traps_are_sourced_test_only():
    traps = load_traps()
    assert len(traps) == 30
    for t in traps:
        assert t.is_trap and t.category == "trap" and t.split == "test"
        assert re.search(r"https://\S+", t.trap_note)


def test_no_em_dashes_in_user_facing_text():
    for q in load_all():
        assert "—" not in q.question + " ".join(q.gold) + (q.trap_note or "")


def test_learn_filter_excludes_traps():
    assert all(q.split == "learn" and not q.is_trap for q in load_all(split="learn"))


def test_stratified_split_is_deterministic():
    import random
    rows = [{"id": f"q{i}", "category": "a" if i % 3 else "b"} for i in range(30)]
    a = [dict(r) for r in rows]
    b = [dict(r) for r in rows]
    stratified_split(a, 10, random.Random(14))
    stratified_split(b, 10, random.Random(14))
    assert [r["split"] for r in a] == [r["split"] for r in b]
    assert sum(r["split"] == "learn" for r in a) == 10
