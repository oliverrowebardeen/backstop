from __future__ import annotations

import json
from pathlib import Path

import pytest

from so import config, router
from so.contracts import MatrixRow, RescueTable


@pytest.mark.parametrize("query,goal,expected", [
    ("What is the FY2024 operating margin?", "", "finance"),
    ("Which companies were acquired?", "Use the 10-K filing", "finance"),
    ("What was the amount?", "Find the revolving credit agreement", "finance"),
    ("What is quarterly revenue growth?", "", "finance"),
    ("Show the free cash flow", "", "finance"),
    ("What is the current stock price?", "", "finance"),
    ("How big was the latest funding round?", "", "finance"),
    ("Find the balance\nsheet liabilities", "", "finance"),
    ("Read the １０Ｋ", "", "finance"),
    ("Who founded a bank?", "Find the founder", "fact"),
    ("Who bought the artwork for $100 million USD?", "Find the buyer", "fact"),
    ("When did the capital city change?", "", "fact"),
    ("Who played Bond?", "Find the actor", "fact"),
    ("A revenue-neutral proposal", "Identify its author", "finance"),
    ("The words capexpert and valuationary", "", "fact"),
    ("", "", "fact"),
])
def test_classify(query, goal, expected):
    assert router.classify(query, goal) == expected


def test_classifier_uses_configured_vocabulary_without_file_access(monkeypatch):
    monkeypatch.setattr(config, "FIRST_ENGINE_FINANCE_PATTERN", r"\bfinancials\b")
    def fail_read(*args, **kwargs):
        pytest.fail("classify must not read any file")
    monkeypatch.setattr(Path, "open", fail_read)
    assert router.classify("Read financials", "") == "finance"
    assert router.classify("Revenue", "") == "fact"


def test_build_computes_counts_and_winners(learn_data):
    questions, rows = learn_data
    table = router.build(rows, questions)
    assert table.routing == {"fact": "perplexity", "finance": "exa"}
    assert table.n_questions == 3
    assert table.counts["fact"]["exa"].model_dump() == {"n_questions": 2, "hits": 1, "hit_rate": 0.5}
    assert router.FirstEngineTable.model_validate_json(table.model_dump_json()) == table


def test_price_tie_and_engine_name_tie_are_deterministic(learn_data, monkeypatch):
    questions, rows = learn_data
    tied = [row.model_copy(update={"hit": True}) for row in rows]
    monkeypatch.setattr(config, "PRICE_USD", {"tavily": 0.02, "exa": 0.01, "perplexity": 0.005})
    assert set(router.build(tied, questions).routing.values()) == {"perplexity"}
    monkeypatch.setattr(config, "PRICE_USD", {})
    assert set(router.build(tied, questions).routing.values()) == {"exa"}


@pytest.mark.parametrize("changes", [{"split": "test"}, {"is_trap": True}, {"category": "trap"}])
def test_builder_refuses_non_learn_questions(learn_data, changes):
    questions, rows = learn_data
    questions[0] = questions[0].model_copy(update=changes)
    with pytest.raises(router.LearnOnlyError):
        router.build(rows, questions)


@pytest.mark.parametrize("changes", [{"split": "test"}, {"is_trap": True}, {"category": "trap"}])
def test_builder_refuses_explicit_non_learn_row_metadata(learn_data, changes):
    questions, rows = learn_data
    raw = [row.model_dump() for row in rows]
    raw[0].update(changes)
    with pytest.raises(router.LearnOnlyError):
        router.build(raw, questions)


def test_builder_rejects_unknown_and_duplicate_rows(learn_data):
    questions, rows = learn_data
    with pytest.raises(router.LearnOnlyError):
        router.build([*rows, MatrixRow(question_id="unrecognized", engine="exa", hit=True)], questions)
    with pytest.raises(ValueError, match="Duplicate"):
        router.build([*rows, rows[0]], questions)
    with pytest.raises(ValueError, match="Duplicate"):
        router.build(rows, [*questions, questions[0]])


def test_builder_refuses_incomplete_cohorts_and_missing_categories(learn_data):
    questions, rows = learn_data
    with pytest.raises(ValueError, match="1 missing"):
        router.build(rows[1:], questions)
    with pytest.raises(ValueError, match="No learn questions for category finance"):
        router.build([row for row in rows if row.question_id.startswith("fact")], questions[:2])
    with pytest.raises(ValueError, match="No learn questions"):
        router.build([], [])


@pytest.mark.parametrize("price", [-1, float("nan"), float("inf")])
def test_builder_rejects_invalid_prices(price, learn_data, monkeypatch):
    questions, rows = learn_data
    monkeypatch.setattr(config, "PRICE_USD", {"exa": price})
    with pytest.raises(ValueError, match="prices"):
        router.build(rows, questions)


def test_routing_on_off_missing_and_configuration(learn_data, monkeypatch):
    questions, rows = learn_data
    assert router.first_engine("query", "goal") == "tavily"
    table = router.build(rows, questions)
    router.write_table(table, config.FIRST_ENGINE_TABLE_PATH)
    assert router.first_engine("query", "goal") == "perplexity"
    assert router.first_engine("query", "Find earnings") == "exa"
    monkeypatch.setattr(config, "FIRST_ENGINE_ROUTING", "off")
    monkeypatch.setattr(config, "FIRST_ENGINE", "custom-engine")
    config.FIRST_ENGINE_TABLE_PATH.write_text("malformed")
    assert router.first_engine("revenue", "goal") == "custom-engine"


def test_routing_reloads_replaced_table(learn_data):
    questions, rows = learn_data
    router.write_table(router.build(rows, questions), config.FIRST_ENGINE_TABLE_PATH)
    assert router.first_engine("Who built it?", "") == "perplexity"
    changed = [row.model_copy(update={"hit": row.engine == "tavily"}) for row in rows]
    router.write_table(router.build(changed, questions), config.FIRST_ENGINE_TABLE_PATH)
    assert router.first_engine("Who built it?", "") == "tavily"


@pytest.mark.parametrize("damage", ["json", "test", "split", "choice", "counts", "category", "engine"])
def test_invalid_existing_table_never_silently_falls_back(damage, learn_data, monkeypatch):
    questions, rows = learn_data
    raw = router.build(rows, questions).model_dump()
    if damage == "test":
        raw["built_on_split"] = "test"
    elif damage == "split":
        del raw["built_on_split"]
    elif damage == "choice":
        raw["routing"]["fact"] = "tavily"
    elif damage == "counts":
        raw["counts"]["fact"]["exa"]["hits"] = 10
    elif damage == "category":
        del raw["counts"]["finance"]
    elif damage == "engine":
        monkeypatch.setattr(config, "ENGINES", ["tavily", "exa"])
    config.FIRST_ENGINE_TABLE_PATH.write_text("not JSON" if damage == "json" else json.dumps(raw))
    with pytest.raises(router.RoutingTableError):
        router.first_engine("query", "goal")


def test_file_read_errors_are_explicit(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(router.RoutingTableError, match="Cannot read"):
        router.first_engine("query", "goal")


def write_inputs(questions, rows):
    config.FIRST_ENGINE_QUESTIONS_PATH.write_text("\n".join(q.model_dump_json() for q in questions))
    config.FIRST_ENGINE_TRAPS_PATH.write_text("")
    config.FIRST_ENGINE_MATRIX_PATH.write_text("\n".join(row.model_dump_json() for row in rows))


def test_loader_filters_held_out_payloads_before_validation(learn_data):
    questions, rows = learn_data
    write_inputs(questions, rows)
    # Excluded records deliberately lack text/gold and have unusable hit labels.
    with config.FIRST_ENGINE_QUESTIONS_PATH.open("a") as output:
        output.write('\n{"id":"held-out","split":"test"}\n')
    config.FIRST_ENGINE_TRAPS_PATH.write_text('{"id":"trap","split":"test","is_trap":true}')
    with config.FIRST_ENGINE_MATRIX_PATH.open("a") as output:
        output.write('\n{"question_id":"held-out","hit":{"unusable":true}}\n')
        output.write('{"question_id":"trap","hit":{"unusable":true}}\n')
    actual_questions, actual_rows = router.load_learn_inputs()
    assert actual_questions == questions
    assert actual_rows == rows


def test_loader_refuses_unknown_ids_and_malformed_json(learn_data):
    questions, rows = learn_data
    write_inputs(questions, rows)
    with config.FIRST_ENGINE_MATRIX_PATH.open("a") as output:
        output.write('\n{"question_id":"unknown","engine":"exa","hit":true}')
    with pytest.raises(router.LearnOnlyError, match="unknown"):
        router.load_learn_inputs()
    config.FIRST_ENGINE_MATRIX_PATH.write_text("not json")
    with pytest.raises(ValueError, match="Invalid JSON"):
        router.load_learn_inputs()


def test_measure_uses_predictions_and_honors_rescue_cap(learn_data, monkeypatch):
    questions, rows = learn_data
    table = router.build(rows, questions)
    rescue = RescueTable(order_after={"tavily": ["exa"], "exa": ["perplexity"]})
    report = router.measure(rows, questions, table, rescue)
    overall = report["categories"]["overall"]
    assert overall["classifier_accuracy"] == 1
    assert overall["baseline"]["first_call_hits"] == 0
    assert overall["baseline"]["simulated_engine_calls"] == 7
    assert overall["routed"]["first_call_hits"] == 3
    assert overall["routed"]["simulated_engine_calls"] == 3
    monkeypatch.setattr(router, "classify", lambda query, goal: "fact")
    monkeypatch.setattr(config, "MAX_RESCUES", 0)
    report = router.measure(rows, questions, table, rescue)
    overall = report["categories"]["overall"]
    assert overall["classifier_accuracy"] == 2 / 3
    assert overall["routed"]["first_call_hits"] == 2
    assert overall["baseline"]["simulated_engine_calls"] == 3


def test_measure_refuses_unobserved_rescue_engine(learn_data):
    questions, rows = learn_data
    with pytest.raises(ValueError, match="lacks an observed result"):
        router.measure(rows, questions, router.build(rows, questions), RescueTable(fallback_order=["unknown"]))


def test_atomic_write_preserves_existing_table_on_failure(learn_data, monkeypatch):
    questions, rows = learn_data
    table = router.build(rows, questions)
    target = config.FIRST_ENGINE_TABLE_PATH
    target.write_text("previous")
    def fail_replace(*args, **kwargs):
        raise OSError("cannot replace")
    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="cannot replace"):
        router.write_table(table, target)
    assert target.read_text() == "previous"
    assert list(target.parent.iterdir()) == [target]


def test_cli_build_and_measure(learn_data, monkeypatch, capsys):
    questions, rows = learn_data
    write_inputs(questions, rows)
    monkeypatch.setattr("sys.argv", ["router", "build"])
    router.main()
    assert json.loads(capsys.readouterr().out)["n_questions"] == 3
    before = config.FIRST_ENGINE_TABLE_PATH.read_text()
    monkeypatch.setattr("sys.argv", ["router", "measure"])
    router.main()
    assert json.loads(capsys.readouterr().out)["split"] == "learn"
    assert config.FIRST_ENGINE_TABLE_PATH.read_text() == before


def test_cli_rejects_test_split(monkeypatch):
    monkeypatch.setattr("sys.argv", ["router", "build", "--split", "test"])
    with pytest.raises(SystemExit) as exc:
        router.main()
    assert exc.value.code == 2
