from jobbot.matching.profile_select import best_profile_for_job
from jobbot.models import Job, ResumeProfile


def _job(title: str, description: str = "") -> Job:
    return Job(
        source="greenhouse", external_id="1", company="Acme", title=title,
        location="Remote", remote=True, url="https://example.com", description=description,
    )


def _profile_row(tag: str, skills: list[str], desired_titles: list[str]) -> ResumeProfile:
    return ResumeProfile(
        tag=tag,
        resume_path=f"/resumes/{tag}.pdf",
        profile_json={
            "name": "Jane Doe",
            "email": "jane@example.com",
            "skills": skills,
            "desired_titles": desired_titles,
            "experience": [{"title": desired_titles[0] if desired_titles else "", "company": "", "start": "", "end": "", "highlights": []}],
        },
    )


PYTHON_DEV = _profile_row("python-developer", ["Python", "Django", "PostgreSQL"], ["Python Developer", "Backend Engineer"])
FRONTEND = _profile_row("frontend", ["React", "TypeScript", "CSS"], ["Frontend Engineer", "UI Engineer"])
CLOUD = _profile_row("cloud-engineer", ["AWS", "Terraform", "Kubernetes"], ["Cloud Engineer", "DevOps Engineer"])

PROFILES = [PYTHON_DEV, FRONTEND, CLOUD]


def test_empty_profiles_returns_no_match():
    tag, score = best_profile_for_job(_job("Any Job"), [])
    assert tag == ""
    assert score == 0.0


def test_python_job_matches_python_developer_profile():
    job = _job("Senior Python Developer", "We use Django and PostgreSQL heavily.")
    tag, _score = best_profile_for_job(job, PROFILES)
    assert tag == "python-developer"


def test_frontend_job_matches_frontend_profile():
    job = _job("Frontend Engineer", "Building UIs in React and TypeScript.")
    tag, _score = best_profile_for_job(job, PROFILES)
    assert tag == "frontend"


def test_cloud_job_matches_cloud_engineer_profile():
    job = _job("Cloud Infrastructure Engineer", "AWS, Terraform, and Kubernetes at scale.")
    tag, _score = best_profile_for_job(job, PROFILES)
    assert tag == "cloud-engineer"


def test_single_profile_always_wins_regardless_of_fit():
    job = _job("Marketing Manager", "No engineering skills required.")
    tag, _score = best_profile_for_job(job, [PYTHON_DEV])
    assert tag == "python-developer"
