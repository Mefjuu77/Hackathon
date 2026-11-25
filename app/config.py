import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    elevenlabs_api_key: str
    customer_agent_id: str
    helpdesk_agent_id: str
    elevenlabs_api_base: str
    max_conversation_seconds: float
    el_max_duration_seconds: int
    runs_dir: Path

    def require_elevenlabs(self) -> None:
        """Raise if the settings needed to talk to ElevenLabs are missing."""
        missing = [
            name
            for name, value in (
                ("ELEVENLABS_API_KEY", self.elevenlabs_api_key),
                ("CUSTOMER_AGENT_ID", self.customer_agent_id),
                ("HELPDESK_AGENT_ID", self.helpdesk_agent_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing settings in .env: {', '.join(missing)}")


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    runs_dir = Path(os.getenv("RUNS_DIR", "runs"))
    if not runs_dir.is_absolute():
        runs_dir = PROJECT_ROOT / runs_dir
    return Settings(
        elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        customer_agent_id=os.getenv("CUSTOMER_AGENT_ID", ""),
        helpdesk_agent_id=os.getenv("HELPDESK_AGENT_ID", ""),
        elevenlabs_api_base=os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io").rstrip("/"),
        max_conversation_seconds=float(os.getenv("MAX_CONVERSATION_SECONDS", "360")),
        el_max_duration_seconds=int(os.getenv("EL_MAX_DURATION_SECONDS", "420")),
        runs_dir=runs_dir,
    )
