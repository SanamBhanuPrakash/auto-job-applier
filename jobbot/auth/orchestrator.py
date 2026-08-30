"""AuthOrchestrator — detect, authenticate, and *verify* (spec §25, §28).

The shape:

    observe -> detect what is being asked
            -> decide whether we are permitted to satisfy it
            -> perform it through the credential subsystem
            -> observe again and verify we are actually signed in

Three properties are load-bearing.

**The credential never reaches the agent.** The model can ask for
`authenticate(domain)` and gets back a status. The password is read here,
typed here, and never enters a prompt, a tool argument, a trace row or a
log line. `agent/policy.py` refuses credential-shaped fields through the
generic `type` tool precisely so this is the only path.

**Success is verified, never inferred.** §28 says it outright: do not
infer success because clicking succeeded. A wrong password usually
re-renders the same login form, and a click on the submit button succeeds
in both cases. We re-observe and look for positive evidence of a session.

**Walls are refused, not worked around.** CAPTCHA, bot detection, security
challenges, locked accounts and third-party SSO are never automated. Some
of those a human can clear, after which the agent resumes (§32) — that is
handing over, not routing around.

What this deliberately does not do: retry a password. One failed attempt
is reported; a second is how an account gets locked, and if the stored
credential is wrong, trying it again will not make it right.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from jobbot.agent.observation import BrowserObservation, Control, Detail, observe
from jobbot.auth.credentials import Credential, CredentialStore, normalize_domain, redact
from jobbot.auth.detect import detect_auth_state
from jobbot.auth.states import (
    HUMAN_CAN_RESOLVE,
    NEVER_AUTOMATED,
    AuthOutcome,
    AuthState,
    VerificationChannelState,
)

log = logging.getLogger(__name__)

#: How long to wait for a login to take effect before re-observing.
_SETTLE_MS = 2500


@dataclass
class AuthResult:
    outcome: AuthOutcome
    state: AuthState
    domain: str = ""
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    #: True when a person could clear this by acting in the browser window
    #: and the flow could then resume (§32).
    resumable_by_human: bool = False

    @property
    def ok(self) -> bool:
        """Safe to carry on with the application."""
        return self.outcome in (
            AuthOutcome.AUTHENTICATED, AuthOutcome.ALREADY_AUTHENTICATED,
            AuthOutcome.NOT_REQUIRED,
        )

    @property
    def verified_session(self) -> bool:
        """We saw positive evidence of being signed in.

        Strictly narrower than `ok`. NOT_REQUIRED means the page did not
        ask — which is equally true of an error page, a 404, and a page
        that had not finished loading — so it must never be recorded as a
        verified session (browser-agent-failures.md §14).
        """
        return self.outcome in (AuthOutcome.AUTHENTICATED,
                                AuthOutcome.ALREADY_AUTHENTICATED)

    def to_dict(self) -> dict:
        """Safe to persist and to show a model: status only, no secrets."""
        return {
            "outcome": self.outcome.value,
            "auth_state": self.state.value,
            "domain": self.domain,
            "reason": redact(self.reason)[:300],
            "evidence": [redact(e)[:160] for e in self.evidence[:5]],
            "resumable_by_human": self.resumable_by_human,
        }


class VerificationChannel:
    """§30. Where a second factor comes from, if anywhere.

    The default is `UNAVAILABLE`: an unattended run has nobody to ask for
    a code, and there is no legitimate way to obtain one otherwise.
    Reading the user's email to harvest OTPs is not something this builds.
    """

    def __init__(self, *, prompt=None):
        self._prompt = prompt

    def state(self) -> VerificationChannelState:
        return (VerificationChannelState.AVAILABLE if self._prompt
                else VerificationChannelState.UNAVAILABLE)

    def request_code(self, domain: str) -> str | None:
        """Ask the human for the code. Returns None when nobody is there."""
        if not self._prompt:
            return None
        return self._prompt(domain)


class AuthOrchestrator:
    def __init__(
        self,
        store: CredentialStore | None = None,
        *,
        verification: VerificationChannel | None = None,
        allow_signup: bool = False,
        allowed_signup_domains: tuple[str, ...] = (),
    ):
        self.store = store or CredentialStore()
        self.verification = verification or VerificationChannel()
        # §29: account creation is off unless configuration permits it, and
        # then only on named domains. Creating accounts on arbitrary sites
        # unattended is not a default anything should have.
        self.allow_signup = allow_signup
        self.allowed_signup_domains = tuple(
            normalize_domain(d) for d in allowed_signup_domains
        )

    # -- entry point --------------------------------------------------------

    def ensure_authenticated(self, page, *, url: str = "") -> AuthResult:
        """Get this page to an authenticated state, or say why not."""
        domain = normalize_domain(url or _safe_url(page))
        observation = observe(page, None, detail=Detail.ARIA)

        # Perception failed, so every signal below is missing for reasons
        # that have nothing to do with authentication. Concluding "not
        # asking, therefore fine" here would let a dead page read as a
        # live session — the same absence-as-evidence mistake as
        # browser-agent-failures.md §14, in the one place where being
        # wrong means carrying on unauthenticated.
        if observation.degraded:
            return AuthResult(
                AuthOutcome.INDETERMINATE, AuthState.UNSUPPORTED_AUTH, domain,
                "could not read the page, so nothing can be concluded about the session: "
                + "; ".join(observation.notes)[:200],
                list(observation.notes), resumable_by_human=True,
            )

        state, evidence = detect_auth_state(observation)

        if state is AuthState.NOT_AUTH:
            return AuthResult(AuthOutcome.NOT_REQUIRED, state, domain,
                              "page is not asking for authentication")
        if state is AuthState.ALREADY_AUTHENTICATED:
            return AuthResult(AuthOutcome.ALREADY_AUTHENTICATED, state, domain,
                              "already signed in", evidence)

        if state in NEVER_AUTOMATED:
            return AuthResult(
                AuthOutcome.BLOCKED if state in (AuthState.CAPTCHA, AuthState.BOT_DETECTION)
                else AuthOutcome.REFUSED,
                state, domain,
                _refusal_reason(state), evidence,
                resumable_by_human=state in HUMAN_CAN_RESOLVE,
            )

        if state in (AuthState.OTP_REQUIRED, AuthState.EMAIL_VERIFICATION_REQUIRED):
            return self._handle_second_factor(page, state, domain, evidence)

        if state is AuthState.SIGNUP_REQUIRED:
            return self._handle_signup(state, domain, evidence)

        if state in (AuthState.LOGIN_REQUIRED, AuthState.SESSION_EXPIRED):
            return self._login(page, observation, state, domain, evidence)

        return AuthResult(AuthOutcome.HUMAN_REQUIRED, AuthState.UNSUPPORTED_AUTH, domain,
                          f"unrecognised authentication flow ({state.value})", evidence,
                          resumable_by_human=True)

    # -- flows --------------------------------------------------------------

    def _login(self, page, observation, state, domain, evidence) -> AuthResult:
        credential = self.store.get(domain)
        if credential is None:
            return AuthResult(
                AuthOutcome.NO_CREDENTIAL, state, domain,
                f"no stored credential for {domain}; run "
                f"`jobbot auth add {domain}` or sign in once in the browser window",
                evidence, resumable_by_human=True,
            )

        identifier = _find_identifier_field(observation)
        password_field = _find_password_field(observation)

        if identifier is None and password_field is None:
            return AuthResult(AuthOutcome.HUMAN_REQUIRED, state, domain,
                              "could not locate the login fields", evidence,
                              resumable_by_human=True)

        try:
            self._type_credential(page, identifier, password_field, credential)
            self._submit_login(page, observation)
        except Exception as exc:  # noqa: BLE001 - never let a raw error carry a value
            message = redact(f"{type(exc).__name__}: {exc}",
                             extra=(credential.password.reveal(), credential.username))
            log.warning("Login attempt on %s failed: %s", domain, message)
            return AuthResult(AuthOutcome.FAILED, state, domain, message, evidence,
                              resumable_by_human=True)

        return self._verify_after_login(page, state, domain, evidence)

    def _type_credential(self, page, identifier: Control | None,
                         password_field: Control | None, credential: Credential) -> None:
        """The only place a password is typed.

        Values go straight from the store into Playwright. Nothing here is
        logged, and the caller catches and redacts anything that raises.
        """
        if identifier is not None:
            page.fill(f"[data-jobbot-id='{identifier.ref}']", credential.username)
        if password_field is not None:
            page.fill(f"[data-jobbot-id='{password_field.ref}']",
                      credential.password.reveal())

    def _submit_login(self, page, observation) -> None:
        button = _find_submit_control(observation)
        if button is not None:
            page.click(f"[data-jobbot-id='{button.ref}']")
        else:
            # Multi-step logins often accept Enter on the identifier field.
            page.keyboard.press("Enter")
        page.wait_for_timeout(_SETTLE_MS)

    def _verify_after_login(self, page, state, domain, evidence) -> AuthResult:
        """§28: verify actual authentication; do not infer it from a click.

        A wrong password re-renders the same form, and the click succeeds
        either way. Only positive evidence of a session counts.
        """
        after = observe(page, None, detail=Detail.ARIA)
        new_state, new_evidence = detect_auth_state(after)
        evidence = evidence + [f"after submit: {new_state.value}"] + new_evidence

        if new_state is AuthState.ALREADY_AUTHENTICATED:
            return AuthResult(AuthOutcome.AUTHENTICATED, new_state, domain,
                              "signed in and verified", evidence)
        if after.degraded:
            return AuthResult(
                AuthOutcome.INDETERMINATE, AuthState.UNSUPPORTED_AUTH, domain,
                "could not read the page after signing in, so the result is unknown",
                evidence, resumable_by_human=True,
            )
        if new_state is AuthState.NOT_AUTH:
            # The login form is gone and nothing is asking us to sign in.
            # Weaker than a sign-out link, so say so rather than claiming
            # certainty (failures §3). We *did* just submit a credential
            # here, which is what makes this different from the same state
            # observed cold.
            return AuthResult(AuthOutcome.AUTHENTICATED, AuthState.ALREADY_AUTHENTICATED,
                              domain,
                              "login form is gone and the page no longer asks to sign in "
                              "(weaker evidence than an explicit sign-out link)", evidence)
        if new_state in (AuthState.OTP_REQUIRED, AuthState.EMAIL_VERIFICATION_REQUIRED):
            return self._handle_second_factor(page, new_state, domain, evidence)
        if new_state in NEVER_AUTOMATED:
            return AuthResult(AuthOutcome.BLOCKED, new_state, domain,
                              _refusal_reason(new_state), evidence,
                              resumable_by_human=new_state in HUMAN_CAN_RESOLVE)

        # Still a login page: the credential did not work. One attempt only.
        return AuthResult(
            AuthOutcome.FAILED, new_state, domain,
            f"still at {new_state.value} after signing in; the stored credential for "
            f"{domain} appears to be wrong. Not retrying — a second attempt will not "
            "make it right and may lock the account.",
            evidence, resumable_by_human=True,
        )

    def _handle_second_factor(self, page, state, domain, evidence) -> AuthResult:
        channel = self.verification.state()
        if channel is not VerificationChannelState.AVAILABLE:
            return AuthResult(
                AuthOutcome.HUMAN_REQUIRED, state, domain,
                f"{state.value} and no verification channel is available; a person must "
                "supply the code. Nothing is bypassed and no code is guessed.",
                evidence, resumable_by_human=True,
            )
        code = self.verification.request_code(domain)
        if not code:
            return AuthResult(AuthOutcome.HUMAN_REQUIRED, state, domain,
                              "no verification code was provided", evidence,
                              resumable_by_human=True)
        try:
            observation = observe(page, None, detail=Detail.ARIA)
            field_ = _find_otp_field(observation)
            if field_ is None:
                return AuthResult(AuthOutcome.HUMAN_REQUIRED, state, domain,
                                  "could not locate the verification code field", evidence,
                                  resumable_by_human=True)
            page.fill(f"[data-jobbot-id='{field_.ref}']", code)
            self._submit_login(page, observation)
        except Exception as exc:  # noqa: BLE001
            return AuthResult(AuthOutcome.FAILED, state, domain,
                              redact(f"{type(exc).__name__}: {exc}", extra=(code,)),
                              evidence, resumable_by_human=True)
        return self._verify_after_login(page, state, domain, evidence)

    def _handle_signup(self, state, domain, evidence) -> AuthResult:
        """§29. Off unless configured, and then only on named domains."""
        if not self.allow_signup:
            return AuthResult(
                AuthOutcome.HUMAN_REQUIRED, state, domain,
                "this posting requires creating an account, which is disabled. Create it "
                "yourself once in the browser window and the session will be reused.",
                evidence, resumable_by_human=True,
            )
        if domain not in self.allowed_signup_domains:
            return AuthResult(
                AuthOutcome.REFUSED, state, domain,
                f"account creation is enabled but {domain} is not in the allowed list",
                evidence, resumable_by_human=True,
            )
        return AuthResult(
            AuthOutcome.HUMAN_REQUIRED, state, domain,
            "automated account creation is permitted for this domain but is not "
            "implemented; create the account yourself once.",
            evidence, resumable_by_human=True,
        )


# -- field location ---------------------------------------------------------


def _find_password_field(observation: BrowserObservation) -> Control | None:
    for control in observation.controls:
        label = control.semantic_label.lower()
        if "password" in label and "forgot" not in label and "reset" not in label:
            return control
    return None


def _find_identifier_field(observation: BrowserObservation) -> Control | None:
    for keyword in ("email", "e-mail", "username", "user name", "user id"):
        for control in observation.controls:
            if control.role not in ("textbox", "combobox"):
                continue
            if keyword in control.semantic_label.lower():
                return control
    return None


def _find_otp_field(observation: BrowserObservation) -> Control | None:
    for control in observation.controls:
        label = control.semantic_label.lower()
        if control.role != "textbox":
            continue
        if any(k in label for k in ("code", "otp", "one-time", "one time", "verification")):
            return control
    return None


def _find_submit_control(observation: BrowserObservation) -> Control | None:
    import re

    pattern = re.compile(r"\b(sign ?in|log ?in|continue|next|submit)\b", re.I)
    for control in observation.controls:
        if control.role in ("button", "link") and pattern.search(control.name):
            return control
    return None


def _refusal_reason(state: AuthState) -> str:
    return {
        AuthState.CAPTCHA:
            "a CAPTCHA is a boundary, not an obstacle. Nothing here attempts to solve or "
            "evade one — solve it yourself in the browser window and the flow resumes.",
        AuthState.BOT_DETECTION:
            "the site has flagged this session as automated. Working around that is what "
            "gets accounts banned, so this stops here.",
        AuthState.SECURITY_CHALLENGE:
            "a security challenge needs the account owner, not an agent.",
        AuthState.ACCOUNT_LOCKED:
            "the account is locked. Repeated attempts make that worse, so this stops.",
        AuthState.SSO_REQUIRED:
            "this site only offers single sign-on through a third-party identity provider "
            "(Google/Microsoft/Apple). Driving your primary identity's sign-in risks far "
            "more than one job application, so sign in yourself once — the session is reused.",
    }.get(state, f"{state.value} is not automated")


def _safe_url(page) -> str:
    try:
        return page.url or ""
    except Exception:  # noqa: BLE001
        return ""
