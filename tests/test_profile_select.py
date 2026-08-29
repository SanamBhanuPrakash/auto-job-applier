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


# --- regression coverage for a real bug: resumes written by the same person
# for different role variants share a large common tech stack (FastAPI,
# Docker, PostgreSQL, ...). Verified live on 9 real resumes that raw
# skill-list matching let that shared boilerplate dominate and one profile
# won almost every job regardless of fit (winning margins of 2-5 points out
# of 100 — noise, not signal). These use profiles that deliberately share
# most of their skills, the way real ones did, to make sure that stays fixed.

SHARED_STACK = ["FastAPI", "Docker", "PostgreSQL", "Git", "REST API Design", "AWS"]

OVERLAPPING_FRONTEND = _profile_row(
    "frontend-engineer", SHARED_STACK + ["React", "CSS", "Tailwind"], []
)
OVERLAPPING_BACKEND = _profile_row(
    "backend-engineer", SHARED_STACK + ["SQLAlchemy", "Microservices"], []
)
OVERLAPPING_CLOUD = _profile_row(
    "cloud-engineer", SHARED_STACK + ["Terraform", "Kubernetes"], []
)
OVERLAPPING_ML = _profile_row(
    "ai-ml-engineer", SHARED_STACK + ["PyTorch", "Neural Networks", "Backpropagation"], []
)
OVERLAPPING_PROFILES = [OVERLAPPING_FRONTEND, OVERLAPPING_BACKEND, OVERLAPPING_CLOUD, OVERLAPPING_ML]


def test_shared_boilerplate_skills_dont_dominate_the_match():
    """The old bug: a job with none of the *distinctive* ML terms in it
    still matched ai-ml-engineer because the shared stack (FastAPI, Docker,
    ...) outweighed everything else. This should go to frontend now."""
    job = _job("Frontend Engineer", "Build accessible UIs with React and CSS, styled with Tailwind.")
    tag, _score = best_profile_for_job(job, OVERLAPPING_PROFILES)
    assert tag == "frontend-engineer"


def test_distinctive_ml_terms_still_win_their_own_job_despite_shared_stack():
    job = _job("Machine Learning Engineer", "PyTorch, neural networks, and backpropagation from scratch.")
    tag, _score = best_profile_for_job(job, OVERLAPPING_PROFILES)
    assert tag == "ai-ml-engineer"


def test_tag_title_overlap_breaks_ties_correctly():
    """Exact regression case: "Full Stack Engineer" must not lose to
    "frontend-engineer" just because frontend's distinctive skill list
    happens to overlap the job description more — the tag itself matching
    the title is the stronger signal."""
    full_stack = _profile_row("full-stack-engineer", SHARED_STACK + ["React", "Node.js"], [])
    profiles = [OVERLAPPING_FRONTEND, full_stack]
    job = _job("Full Stack Engineer", "React frontend and Node backend.")
    tag, _score = best_profile_for_job(job, profiles)
    assert tag == "full-stack-engineer"
