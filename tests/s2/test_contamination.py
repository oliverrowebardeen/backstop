from evals.contamination import filter_results, is_benchmark_copy, question_overlap

Q = "What is the FY2018 capital expenditure amount (in USD millions) for 3M?"


def r(url, title="", snippet=""):
    return {"url": url, "title": title, "snippet": snippet}


def test_dataset_url_is_a_copy():
    assert is_benchmark_copy(Q, r("https://huggingface.co/datasets/PatronusAI/financebench/viewer"))


def test_verbatim_question_is_a_copy():
    assert is_benchmark_copy(Q, r("https://example.com/qa", snippet="Q12. " + Q + " Answer: $1577.00"))


def test_organic_page_is_kept():
    page = r("https://investors.3m.com/10k-2018", "3M 2018 Form 10-K",
             "Purchases of property, plant and equipment (PP&E) were $1,577 million in 2018.")
    assert not is_benchmark_copy(Q, page)
    assert question_overlap(Q, page["title"], page["snippet"]) < 0.2


def test_filter_preserves_order_and_counts(monkeypatch):
    rs = [r("https://a.com", snippet="unrelated"), r("https://huggingface.co/datasets/x"), r("https://b.com")]
    kept, dropped = filter_results(Q, rs)
    assert [x["url"] for x in kept] == ["https://a.com", "https://b.com"] and dropped == 1
    monkeypatch.setenv("SO_CONTAM_OFF", "1")
    assert filter_results(Q, rs) == (rs, 0)


def test_threshold_is_validated(monkeypatch):
    import pytest
    monkeypatch.setenv("SO_CONTAM_THRESHOLD", "0")
    with pytest.raises(ValueError):
        filter_results(Q, [])
