"""production modules never import test code or switch on an environment flag for fakes."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _modules():
    for pkg in ("evals", "analysis"):
        yield from sorted((ROOT / pkg).glob("*.py"))


def test_no_production_module_imports_tests():
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any(n == "tests" or n.startswith("tests.") for n in names), f"{path.name} imports {names}"


def test_no_production_module_mentions_so_fake():
    assert [p.name for p in _modules() if "SO_FAKE" in p.read_text()] == []


def test_fixture_injection_is_in_place(eval_fakes, tmp_path):
    from evals import run_eval
    from tests.s2 import fakes
    from tests.s4 import fake_agent

    assert eval_fakes.llm is fakes and eval_fakes.telem_client is fakes
    assert run_eval.load_agent() is fake_agent
    assert eval_fakes.OUT.is_relative_to(tmp_path) and eval_fakes.JUDGE_CACHE.is_relative_to(tmp_path)
