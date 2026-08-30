"""Loads .env and the YAML config files in config/."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    # "groq" (default): free, no card required — see jobbot/llm.py for why
    # it's the default and what the tradeoffs are. "anthropic": needs a
    # separate paid API key, not covered by a Claude.ai Pro/Max subscription.
    llm_provider: str = "groq"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    adzuna_country: str = "us"
    usajobs_api_key: str = ""
    usajobs_user_agent_email: str = ""

    jobbot_data_dir: Path = Path("./data")
    jobbot_resume_path: Path = Path("./data/resume.pdf")
    jobbot_resumes_dir: Path = Path("./config/resumes")

    jobbot_headless: bool = False
    jobbot_auto_submit: bool = False
    # Off by default. When on, a previously-confirmed answer to a sensitive
    # question (work authorization, EEOC, legal attestation, ...) is reused
    # automatically instead of always stopping for review. Turning it on
    # still requires one explicit typed confirmation per run (see cli.py
    # _confirm_sensitive_autofill) listing exactly which saved answers will
    # be reused — this flag alone does not silently enable it.
    jobbot_autofill_sensitive: bool = False
    # When the deterministic path cannot find the application form, hand
    # the browser to the bounded agent loop to reach it (jobbot/agent/
    # takeover.py) instead of giving up. The agent operates under the same
    # policy gate as everything else and can never submit: takeover runs
    # at Autonomy.NAVIGATE.
    jobbot_agent_takeover: bool = True
    # Hard bound on one takeover episode.
    jobbot_agent_max_steps: int = 12

    @property
    def data_dir(self) -> Path:
        d = (REPO_ROOT / self.jobbot_data_dir).resolve()
        d.mkdir(parents=True, exist_ok=True)
        (d / "screenshots").mkdir(exist_ok=True)
        return d

    @property
    def resumes_dir(self) -> Path:
        return (REPO_ROOT / self.jobbot_resumes_dir).resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobbot.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _load_yaml(path: Path, example_path: Path) -> dict:
    if not path.exists():
        if example_path.exists():
            raise FileNotFoundError(
                f"{path.name} not found. Copy {example_path} to {path} and edit it."
            )
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def load_companies() -> dict:
    return _load_yaml(CONFIG_DIR / "companies.yaml", CONFIG_DIR / "companies.example.yaml")


def load_search_settings() -> dict:
    return _load_yaml(CONFIG_DIR / "settings.yaml", CONFIG_DIR / "settings.example.yaml")


def load_profile_raw() -> dict:
    return _load_yaml(CONFIG_DIR / "profile.yaml", CONFIG_DIR / "profile.example.yaml")


def save_profile_raw(data: dict) -> Path:
    path = CONFIG_DIR / "profile.yaml"
    with path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    return path
