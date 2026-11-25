import itertools
import json
import random

import pytest

from app import content
from app.config import PROJECT_ROOT

CUSTOMER_VARIABLES = {"nps_level", "persona", "patience_turns", "scenario", "hidden_facts", "customer_name", "first_message"}
HELPDESK_VARIABLES = {"cx_level", "persona", "agent_name", "crm_data"}
COMBINATIONS = list(itertools.product(content.LEVELS, content.SCENARIOS))


def _assert_init(message: dict, expected_variables: set[str]):
    assert message["type"] == "conversation_initiation_client_data"
    variables = message["dynamic_variables"]
    assert set(variables) == expected_variables
    assert all(isinstance(v, str) and v.strip() for v in variables.values())
    override = message["conversation_config_override"]
    assert set(override) == {"tts", "conversation"}
    assert set(override["tts"]) == {"stability", "speed"}
    assert set(override["conversation"]) == {"max_duration_seconds"}
    assert override["conversation"]["max_duration_seconds"] == 420


@pytest.mark.parametrize("level,scenario_id", COMBINATIONS)
def test_customer_init_is_complete(level, scenario_id):
    _assert_init(content.build_customer_init(level, content.SCENARIOS[scenario_id], 420), CUSTOMER_VARIABLES)


@pytest.mark.parametrize("level,scenario_id", COMBINATIONS)
def test_helpdesk_init_is_complete(level, scenario_id):
    _assert_init(content.build_helpdesk_init(level, content.SCENARIOS[scenario_id], 420), HELPDESK_VARIABLES)


@pytest.mark.parametrize("level,variant", [(1, "low"), (3, "low"), (4, "mid"), (7, "mid"), (8, "high"), (10, "high")])
def test_first_message_variant_by_nps(level, variant):
    for scenario in content.SCENARIOS.values():
        init = content.build_customer_init(level, scenario, 420)
        assert init["dynamic_variables"]["first_message"] == scenario.first_messages[variant]


def test_expected_resolution_never_sent_to_agents():
    for level, scenario_id in COMBINATIONS:
        scenario = content.SCENARIOS[scenario_id]
        for init in (content.build_customer_init(level, scenario, 420), content.build_helpdesk_init(level, scenario, 420)):
            assert scenario.expected_resolution not in json.dumps(init, ensure_ascii=False)


def test_voice_settings_in_range():
    for table in (content.CUSTOMER_VOICE, content.HELPDESK_VOICE):
        assert set(table) == set(content.LEVELS)
        for stability, speed in table.values():
            assert 0 <= stability <= 1
            assert 0.7 <= speed <= 1.2


def test_hidden_facts_rendered_as_lines():
    init = content.build_customer_init(5, content.SCENARIOS["S1"], 420)
    lines = init["dynamic_variables"]["hidden_facts"].splitlines()
    assert lines == [f"- {fact}" for fact in content.SCENARIOS["S1"].hidden_facts]


def test_invalid_level_rejected():
    with pytest.raises(ValueError):
        content.build_customer_init(0, content.SCENARIOS["S1"], 420)
    with pytest.raises(ValueError):
        content.build_helpdesk_init(11, content.SCENARIOS["S1"], 420)


def test_pick_scenario():
    assert content.pick_scenario("S3").id == "S3"
    assert content.pick_scenario("random", random.Random(0)).id in content.SCENARIOS
    with pytest.raises(ValueError):
        content.pick_scenario("S9")


def test_customers_are_male():
    names = {s.id: s.customer_name for s in content.SCENARIOS.values()}
    assert names == {"S1": "Marek Nowak", "S2": "Andrzej Kowalski", "S3": "Piotr Wiśniewski", "S4": "Krzysztof Zieliński"}


def test_content_matches_markdown_sources():
    customer_md = (PROJECT_ROOT / "PROMPT_CUSTOMER.md").read_text(encoding="utf-8")
    helpdesk_md = (PROJECT_ROOT / "PROMPT_HELPDESK.md").read_text(encoding="utf-8")
    for level in content.LEVELS:
        assert f"| {level} | {content.CUSTOMER_PATIENCE_TURNS[level]} | {content.CUSTOMER_PERSONA_TEXTS[level]} |" in customer_md
        assert f"| {level} | {content.HELPDESK_PERSONA_TEXTS[level]} |" in helpdesk_md
    for scenario in content.SCENARIOS.values():
        assert f"### {scenario.id}. {scenario.title}" in customer_md
        assert scenario.scenario in customer_md
        assert scenario.expected_resolution in customer_md
        assert all(f"  - {fact}" in customer_md for fact in scenario.hidden_facts)
        assert all(f"„{message}”" in customer_md for message in scenario.first_messages.values())
        assert scenario.crm_data in helpdesk_md
