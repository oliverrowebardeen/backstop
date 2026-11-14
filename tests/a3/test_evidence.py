"""The evidence builder works on held data only: answer terms, snippets, agreement."""

from so.contracts import Result
from so.evidence import answer_terms, best_snippet, build_evidence, engines_agreeing, split_chunks


def result(snippet: str, engine: str) -> Result:
    return Result(rank=1, title="t", url=f"https://{engine}.example", snippet=snippet, engine=engine)


def test_quoted_answer_and_numbers():
    terms = answer_terms('Result 2 says "Jane Doe" became CEO in 2024.')
    assert terms.phrases == ("jane doe",)
    assert not terms.numbers


def test_query_years_are_not_answer_numbers():
    terms = answer_terms("Revenue was 4.2 billion in 2025.", exclude="acme revenue 2025")
    assert terms.numbers == (4.2e9,)


def test_numeric_match_is_normalized():
    terms = answer_terms('"$4.2 billion"')
    agreeing = engines_agreeing({
        "a": [result("4,200,000,000 in sales", "a")],
        "b": [result("about $4.2bn", "b")],
        "c": [result("4.3 billion", "c")],
    }, terms)
    assert agreeing == ["a", "b"]


def test_snippet_is_capped_and_centred_on_answer():
    text = "x " * 600 + "the answer is 987654 here " + "y " * 600
    snippet = best_snippet([text], answer_terms('"987654"'))
    assert len(snippet) <= 500 and "987654" in snippet


def test_split_chunks():
    assert split_chunks("a [...] b [...]  ") == ["a", "b"]
    assert split_chunks("plain") == ["plain"]


def test_build_evidence_shape():
    evidence = build_evidence(url="https://u", why="no answer terms", candidates=["chunk"], per_engine={})
    assert evidence == {"verified": True, "snippet": "chunk", "url": "https://u", "engines_agreeing": 0,
                        "agreeing_engines": [], "source": "search"}
