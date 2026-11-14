"""Agent answer format: prose followed by a trailing JSON object, and the one repair turn."""
import json
from unittest.mock import AsyncMock

import pytest

from so import agent
from tests.o3.test_agent import URL, events, final_response, response, search_call  # noqa: F401
from tests.o3.test_agent import agent_settings, question, wrappers  # noqa: F401  (fixtures)

BODY = json.dumps({"final": "Ada is the CEO.", "status": "ANSWERED", "citations": [URL]})


def test_trailing_unfenced_json_after_prose_is_accepted():
    text = f"The sources agree Ada runs it, see the profile {{and the filing}}.\n\n{BODY}"
    answer = agent._answer(text, {URL}, 1)
    assert answer.final == "Ada is the CEO." and answer.citations == [URL]


def test_json_that_does_not_end_the_reply_is_rejected():
    with pytest.raises(ValueError, match="JSON object or NOT FOUND"):
        agent._answer(f"{BODY}\nThat is my answer.", {URL}, 1)


async def test_format_slip_gets_one_repair_turn(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(search_call()), response(text="Ada is the CEO, per the profile."),
                                     final_response()]
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.status == "ANSWERED"
    repair_prompt = wrappers.complete.call_args_list[-1].args[2][-1]["content"]
    assert "did not follow the required answer format" in repair_prompt
    llm_events = [e for e in events(tmp_path) if e["type"] == "llm"]
    assert len(llm_events) == 3  # search turn, slipped answer, repaired answer: all costed


async def test_second_format_slip_still_raises(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(search_call()), response(text="Ada."), response(text="Still Ada.")]
    with pytest.raises(ValueError, match="Invalid agent answer"):
        await agent.run_question(question, "single", log_dir=tmp_path)
