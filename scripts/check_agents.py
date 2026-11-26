"""Verify the ElevenLabs agent configuration against the SPEC §3 checklist.

Usage: uv run python scripts/check_agents.py
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402

PASS, FAIL, MANUAL = "PASS", "FAIL", "MANUAL"

# `ping` and `client_tool_call` are not selectable in the dashboard; they are always sent.
REQUIRED_CLIENT_EVENTS = {
    "audio",
    "agent_response",
    "agent_response_correction",
    "user_transcript",
    "interruption",
    "agent_tool_response",
}
EXPECTED_FIRST_MESSAGE = {"customer": "{{first_message}}", "helpdesk": ""}


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: str
    detail: str


def _get(data: dict, path: str):
    """Follow a dotted path; return None if any step is missing."""
    for key in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _check(check_id: str, ok: bool, detail: str) -> CheckResult:
    return CheckResult(check_id, PASS if ok else FAIL, detail)


def _find_report_mood(tools: list[dict]) -> dict | None:
    for tool in tools:
        cfg = tool.get("tool_config", tool)
        if cfg.get("name") == "report_mood":
            return cfg
    return None


def check_agent_config(role: str, config: dict, tools: list[dict] | None) -> list[CheckResult]:
    """Check one agent's GET /v1/convai/agents/{id} response.

    `tools` are the resolved tool definitions from `prompt.tool_ids`, or None if they
    could not be fetched (A8 is then reported as MANUAL).
    """
    cc = config.get("conversation_config", {})
    overrides = _get(config, "platform_settings.overrides.conversation_config_override") or {}
    results = []

    language = _get(cc, "agent.language")
    results.append(_check("A1", language == "pl", f"language={language!r}"))

    first_message = _get(cc, "agent.first_message") or ""
    expected = EXPECTED_FIRST_MESSAGE[role]
    results.append(_check("A3", first_message == expected, f"first_message={first_message!r}, expected {expected!r}"))

    input_format = _get(cc, "asr.user_input_audio_format")
    results.append(_check("A5", input_format == "pcm_16000", f"user_input_audio_format={input_format!r}"))
    output_format = _get(cc, "tts.agent_output_audio_format")
    results.append(_check("A6", output_format == "pcm_16000", f"agent_output_audio_format={output_format!r}"))

    end_call = _get(cc, "agent.prompt.built_in_tools.end_call")
    results.append(_check("A7", end_call is not None, "end_call enabled" if end_call else "end_call missing"))

    if role == "customer":
        if tools is None:
            results.append(CheckResult("A8", MANUAL, "could not fetch tool definitions; verify report_mood in the panel"))
        else:
            tool = _find_report_mood(tools)
            if tool is None:
                results.append(_check("A8", False, "report_mood tool not attached"))
            else:
                params = set((_get(tool, "parameters.properties") or {}).keys())
                ok = tool.get("type") == "client" and {"score", "reason"} <= params and not tool.get("expects_response")
                results.append(_check(
                    "A8", ok,
                    f"type={tool.get('type')!r}, params={sorted(params)}, expects_response={tool.get('expects_response')!r}",
                ))

    missing_overrides = [
        path
        for path in ("tts.stability", "tts.speed", "conversation.max_duration_seconds")
        if _get(overrides, path) is not True
    ]
    results.append(_check("A9", not missing_overrides, f"overrides not enabled: {missing_overrides}" if missing_overrides else "overrides enabled"))

    enable_auth = _get(config, "platform_settings.auth.enable_auth")
    results.append(_check("A10", enable_auth is True, f"enable_auth={enable_auth!r}"))

    client_events = set(_get(cc, "conversation.client_events") or [])
    missing_events = sorted(REQUIRED_CLIENT_EVENTS - client_events)
    results.append(_check("A11", not missing_events, f"missing client_events: {missing_events}" if missing_events else "client_events ok"))

    turn_timeout = _get(cc, "turn.turn_timeout")
    results.append(_check("A12", turn_timeout == 10, f"turn_timeout={turn_timeout!r}"))

    silence_timeout = _get(cc, "turn.silence_end_call_timeout")
    results.append(_check("A13", silence_timeout in (None, -1), f"silence_end_call_timeout={silence_timeout!r}"))

    return results


def fetch_tools(client: httpx.Client, config: dict) -> list[dict] | None:
    tool_ids = _get(config, "conversation_config.agent.prompt.tool_ids") or []
    tools = list(_get(config, "conversation_config.agent.prompt.tools") or [])  # deprecated inline tools
    try:
        for tool_id in tool_ids:
            response = client.get(f"/v1/convai/tools/{tool_id}")
            response.raise_for_status()
            tools.append(response.json())
    except httpx.HTTPError:
        return None
    return tools


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    settings = load_settings()
    try:
        settings.require_elevenlabs()
    except RuntimeError as error:
        print(error)
        return 2
    agents = {"customer": settings.customer_agent_id, "helpdesk": settings.helpdesk_agent_id}
    failed = False
    with httpx.Client(base_url=settings.elevenlabs_api_base, headers={"xi-api-key": settings.elevenlabs_api_key}, timeout=15) as client:
        for role, agent_id in agents.items():
            response = client.get(f"/v1/convai/agents/{agent_id}")
            response.raise_for_status()
            config = response.json()
            tools = fetch_tools(client, config) if role == "customer" else []
            print(f"\n== {role} ({agent_id}) ==")
            for result in check_agent_config(role, config, tools):
                print(f"  {result.check_id:<4} {result.status:<6} {result.detail}")
                failed |= result.status == FAIL
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
