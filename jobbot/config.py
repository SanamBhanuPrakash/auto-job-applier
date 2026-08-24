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

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    adzuna_country: str = "us"
    usajobs_api_key: str = ""
    usajobs_user_agent_email: str = ""

    jobbot_data_dir: Path = Path("./data")
    jobbot_resume_path: Path = Path("./data/resume.pdf")

    jobbot_headless: bool = False
    jobbot_auto_submit: bool = False

    @property
    def data_dir(self) -> Path:
        d = (REPO_ROOT / self.jobbot_data_dir).resolve()
        d.mkdir(parents=True, exist_ok=True)
        (d / "screenshots").mkdir(exist_ok=True)
        return d

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
