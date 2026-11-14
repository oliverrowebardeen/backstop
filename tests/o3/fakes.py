"""Offline wrapper stand-ins used only while the foundation lane is absent."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from so.contracts import Event


async def unavailable(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Test must provide this wrapper's response")


class FakeEventLog:
    def __init__(self, run_id: str, question_id: str, setup: str, *, log_dir="out/logs") -> None:
        self.run_id, self.question_id, self.setup = run_id, question_id, setup
        self.path = Path(log_dir) / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq = 0

    def emit(self, event_type: str, **data: Any) -> Event:
        self.seq += 1
        event = Event(ts=datetime.now(timezone.utc).isoformat(), run_id=self.run_id,
                      question_id=self.question_id, setup=self.setup,
                      seq=self.seq, type=event_type, data=data)
        with self.path.open("a") as stream:
            stream.write(event.model_dump_json() + "\n")
        return event


def install_missing_wrappers() -> None:
    modules = {
        "config": {"FIRST_ENGINE": "tavily", "AGENT_MODEL": "test-agent"},
        "telem_client": {name: unavailable for name in (
            "search_engines", "search_default", "fetch_page")},
        "llm": {"complete": unavailable},
        "eventlog": {"EventLog": FakeEventLog},
    }
    for name, members in modules.items():
        qualified = f"so.{name}"
        if qualified not in sys.modules and importlib.util.find_spec(qualified) is None:
            module = ModuleType(qualified)
            module.__dict__.update(members)
            sys.modules[qualified] = module
