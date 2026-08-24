from __future__ import annotations

from pydantic import BaseModel, Field


class Experience(BaseModel):
    company: str = ""
    title: str = ""
    start: str = ""
    end: str = ""
    highlights: list[str] = Field(default_factory=list)


class Education(BaseModel):
    school: str = ""
    degree: str = ""
    start: str = ""
    end: str = ""


class Links(BaseModel):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Profile(BaseModel):
    """The single source of truth for every fact the fill-planner is allowed to use.

    Anything not represented here (or left null) must never be invented by the
    LLM when filling an application form — see jobbot/submit/fill_planner.py.
    """

    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: Links = Field(default_factory=Links)
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)

    desired_titles: list[str] = Field(default_factory=list)
    desired_locations: list[str] = Field(default_factory=list)
    salary_expectation_usd: int | None = None
    willing_to_relocate: bool | None = None

    # Deliberately left nullable and excluded from auto-fill by default.
    # See config/profile.example.yaml for why.
    work_authorization: str | None = None
    requires_sponsorship: bool | None = None
    veteran_status: str | None = None
    disability_status: str | None = None
    gender: str | None = None
    race_ethnicity: str | None = None

    def facts_json_for_llm(self) -> str:
        """Serialize only the fields the fill-planner is permitted to draw on."""
        return self.model_dump_json(exclude_none=True, indent=2)
