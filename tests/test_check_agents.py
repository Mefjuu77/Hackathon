import copy

import pytest

from scripts.check_agents import FAIL, MANUAL, PASS, REQUIRED_CLIENT_EVENTS, check_agent_config

REPORT_MOOD_TOOL = {
    "id": "tool_1",
    "tool_config": {
        "type": "client",
        "name": "report_mood",
        "expects_response": False,
        "parameters": {"type": "object", "properties": {"score": {"type": "integer"}, "reason": {"type": "string"}}},
    },
}


def make_config(role: str) -> dict:
    return {
        "conversation_config": {
            "agent": {
                "language": "pl",
                "first_message": "{{first_message}}" if role == "customer" else "",
                "prompt": {"built_in_tools": {"end_call": {"name": "end_call"}}, "tool_ids": ["tool_1"]},
            },
            "asr": {"user_input_audio_format": "pcm_16000"},
            "tts": {"agent_output_audio_format": "pcm_16000"},
            "turn": {"turn_timeout": 10, "silence_end_call_timeout": -1},
            "conversation": {"client_events": sorted(REQUIRED_CLIENT_EVENTS | {"vad_score"})},
        },
        "platform_settings": {
            "auth": {"enable_auth": True},
            "overrides": {
                "conversation_config_override": {
                    "tts": {"stability": True, "speed": True},
                    "conversation": {"max_duration_seconds": True},
                }
            },
        },
    }


def statuses(results) -> dict[str, str]:
    return {r.check_id: r.status for r in results}


@pytest.mark.parametrize("role", ["customer", "helpdesk"])
def test_valid_config_passes(role):
    results = check_agent_config(role, make_config(role), [REPORT_MOOD_TOOL])
    assert all(r.status == PASS for r in results), results
    assert ("A8" in statuses(results)) == (role == "customer")


def _set(config: dict, path: str, value):
    *parents, last = path.split(".")
    node = config
    for key in parents:
        node = node[key]
    node[last] = value


VIOLATIONS = [
    ("A1", "conversation_config.agent.language", "en"),
    ("A3", "conversation_config.agent.first_message", "Dzień dobry"),
    ("A5", "conversation_config.asr.user_input_audio_format", "ulaw_8000"),
    ("A6", "conversation_config.tts.agent_output_audio_format", "pcm_44100"),
    ("A7", "conversation_config.agent.prompt.built_in_tools.end_call", None),
    ("A9", "platform_settings.overrides.conversation_config_override.tts.speed", False),
    ("A9", "platform_settings.overrides.conversation_config_override.conversation.max_duration_seconds", False),
    ("A10", "platform_settings.auth.enable_auth", False),
    ("A11", "conversation_config.conversation.client_events", ["audio", "ping"]),
    ("A12", "conversation_config.turn.turn_timeout", 7),
    ("A13", "conversation_config.turn.silence_end_call_timeout", 20),
]


@pytest.mark.parametrize("role", ["customer", "helpdesk"])
@pytest.mark.parametrize("check_id,path,value", VIOLATIONS)
def test_violation_reported(role, check_id, path, value):
    config = copy.deepcopy(make_config(role))
    _set(config, path, value)
    result = statuses(check_agent_config(role, config, [REPORT_MOOD_TOOL]))
    assert result[check_id] == FAIL
    assert [cid for cid, status in result.items() if status == FAIL] == [check_id]


def test_helpdesk_first_message_must_be_empty():
    config = make_config("helpdesk")
    _set(config, "conversation_config.agent.first_message", "{{first_message}}")
    assert statuses(check_agent_config("helpdesk", config, []))["A3"] == FAIL


def test_report_mood_missing_fails():
    assert statuses(check_agent_config("customer", make_config("customer"), []))["A8"] == FAIL


def test_report_mood_waiting_for_response_fails():
    tool = copy.deepcopy(REPORT_MOOD_TOOL)
    tool["tool_config"]["expects_response"] = True
    assert statuses(check_agent_config("customer", make_config("customer"), [tool]))["A8"] == FAIL


def test_unresolvable_tools_reported_manual():
    assert statuses(check_agent_config("customer", make_config("customer"), None))["A8"] == MANUAL
