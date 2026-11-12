"""Configuration loaded from environment / .env. Credentials are never hardcoded."""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_PROJECT = "TYN"
DEFAULT_SCOPE_LABEL = "tyn-release-assessment-v2"
DEFAULT_RELEASE = "R2.4"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "high"


class ConfigError(RuntimeError):
    pass


def load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(override=False)


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    email: str
    api_token: str
    issue_type: str = "Task"

    @classmethod
    def from_env(cls) -> "JiraConfig":
        load_dotenv_if_present()
        missing = [k for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN") if not os.environ.get(k)]
        if missing:
            raise ConfigError(f"Missing required settings: {', '.join(missing)} (set them in .env)")
        return cls(
            base_url=os.environ["JIRA_BASE_URL"].rstrip("/"),
            email=os.environ["JIRA_EMAIL"],
            api_token=os.environ["JIRA_API_TOKEN"],
            issue_type=os.environ.get("JIRA_ISSUE_TYPE", "Task"),
        )


def anthropic_settings() -> tuple[str, str, str]:
    """Return (api_key, model, effort)."""
    load_dotenv_if_present()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ConfigError("Missing ANTHROPIC_API_KEY (set it in .env)")
    effort = os.environ.get("ANTHROPIC_EFFORT", DEFAULT_EFFORT)
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError(f"ANTHROPIC_EFFORT must be low|medium|high|xhigh|max, got {effort!r}")
    return key, os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL), effort
