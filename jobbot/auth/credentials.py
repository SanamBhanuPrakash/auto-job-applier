"""Credential storage and retrieval (spec §26, §60).

The single rule this module exists to enforce:

    A password, session cookie, auth token or OTP secret never appears in
    an LLM prompt, a tool argument, a trace row, a log line, or an
    exception message.

That rule is why the agent cannot "just type the password" through the
generic `type` tool — `agent/policy.py` refuses credential-shaped fields
outright. The agent may only ask for `authenticate(domain)`, and this
subsystem performs it. The model receives a status, never a value.

Storage order, most to least secure:

1. **OS keyring** (macOS Keychain, GNOME Keyring, Windows Credential
   Manager) via the optional `keyring` package. Encrypted at rest by the
   OS, unlocked by the user's login.
2. **Environment variables** — `JOBBOT_CRED_<DOMAIN>_USER` / `_PASSWORD`.
   Convenient for CI; visible to any process that can read the
   environment, which is why it is second.

There is deliberately no third option. Storing passwords in a config file
in the repo directory is the mistake this ordering exists to avoid, and
offering it as a fallback is how it happens.

`Secret` wraps every value so that a stray f-string, `repr`, log call or
traceback prints `<redacted>` rather than the password. Getting the real
value takes an explicit `.reveal()`, which is greppable — you can audit
every place a credential is actually read.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)

_SERVICE = "jobbot"


class Secret:
    """A string that refuses to print itself.

    Not security in the cryptographic sense — the value is in memory and
    anyone with the process can read it. It is protection against the way
    credentials actually leak in practice: an f-string in a log line, a
    dataclass `repr` in a trace row, a variable caught in a traceback.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        """The actual value. Every call site is auditable by grepping."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __str__(self) -> str:
        return "<redacted>"

    __repr__ = __str__

    def __format__(self, _spec: str) -> str:
        return "<redacted>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and self._value == other._value

    def __hash__(self) -> int:  # pragma: no cover - not used as a key
        raise TypeError("Secret is not hashable; do not use one as a dict key")


@dataclass(frozen=True)
class Credential:
    domain: str
    username: str
    password: Secret

    def __repr__(self) -> str:
        return f"Credential(domain={self.domain!r}, username={self.username!r}, password=<redacted>)"


class CredentialSource(str, Enum):
    KEYRING = "KEYRING"
    ENVIRONMENT = "ENVIRONMENT"
    NONE = "NONE"


def normalize_domain(domain_or_url: str) -> str:
    """Credentials are keyed by registrable-ish host.

    `job-boards.greenhouse.io` and `boards.greenhouse.io` are the same
    account, and keying on the full host would silently ask for the
    password twice.
    """
    value = (domain_or_url or "").strip().lower()
    value = re.sub(r"^[a-z]+://", "", value)
    value = value.split("/")[0].split("?")[0]
    value = value.split("@")[-1].split(":")[0]
    parts = [p for p in value.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    # Keep the last three labels for known two-part public suffixes
    # (co.uk, com.au, co.in), else the last two.
    if parts[-2] in {"co", "com", "net", "org", "gov", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _env_key(domain: str, suffix: str) -> str:
    slug = re.sub(r"[^A-Z0-9]", "_", domain.upper())
    return f"JOBBOT_CRED_{slug}_{suffix}"


def _keyring():
    try:
        import keyring  # noqa: PLC0415 - optional dependency
    except ImportError:
        return None
    return keyring


class CredentialStore:
    """Reads and writes credentials. Never logs a value."""

    def __init__(self, *, allow_keyring: bool = True):
        self.allow_keyring = allow_keyring

    # -- read ---------------------------------------------------------------

    def get(self, domain_or_url: str) -> Credential | None:
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None

        env_user = os.environ.get(_env_key(domain, "USER"))
        env_pass = os.environ.get(_env_key(domain, "PASSWORD"))

        if self.allow_keyring:
            kr = _keyring()
            if kr is not None:
                try:
                    username = kr.get_password(_SERVICE, f"{domain}:username") or env_user
                    if username:
                        password = kr.get_password(_SERVICE, f"{domain}:{username}")
                        if password:
                            log.info("Using keyring credential for %s (user %s)", domain, username)
                            return Credential(domain, username, Secret(password))
                except Exception:  # noqa: BLE001 - a locked keyring is not fatal
                    log.debug("keyring lookup failed for %s", domain, exc_info=True)

        if env_user and env_pass:
            log.info("Using environment credential for %s (user %s)", domain, env_user)
            return Credential(domain, env_user, Secret(env_pass))
        return None

    def source_for(self, domain_or_url: str) -> CredentialSource:
        """Where a credential would come from, without reading the secret."""
        domain = normalize_domain(domain_or_url)
        if self.allow_keyring:
            kr = _keyring()
            if kr is not None:
                try:
                    if kr.get_password(_SERVICE, f"{domain}:username"):
                        return CredentialSource.KEYRING
                except Exception:  # noqa: BLE001
                    pass
        if os.environ.get(_env_key(domain, "USER")) and os.environ.get(_env_key(domain, "PASSWORD")):
            return CredentialSource.ENVIRONMENT
        return CredentialSource.NONE

    def has(self, domain_or_url: str) -> bool:
        return self.source_for(domain_or_url) is not CredentialSource.NONE

    # -- write --------------------------------------------------------------

    def store(self, domain_or_url: str, username: str, password: str) -> CredentialSource:
        """Save a credential to the keyring. Raises if none is available —
        never silently falls back to writing a password to disk."""
        domain = normalize_domain(domain_or_url)
        if not domain or not username or not password:
            raise ValueError("domain, username and password are all required")
        kr = _keyring()
        if kr is None:
            raise RuntimeError(
                "No OS keyring is available (`pip install keyring`). Refusing to write a "
                f"password to a file; set {_env_key(domain, 'USER')} and "
                f"{_env_key(domain, 'PASSWORD')} in your environment instead."
            )
        kr.set_password(_SERVICE, f"{domain}:username", username)
        kr.set_password(_SERVICE, f"{domain}:{username}", password)
        log.info("Stored credential for %s (user %s)", domain, username)
        return CredentialSource.KEYRING

    def forget(self, domain_or_url: str) -> bool:
        domain = normalize_domain(domain_or_url)
        kr = _keyring()
        if kr is None:
            return False
        try:
            username = kr.get_password(_SERVICE, f"{domain}:username")
            if username:
                kr.delete_password(_SERVICE, f"{domain}:{username}")
            kr.delete_password(_SERVICE, f"{domain}:username")
            return True
        except Exception:  # noqa: BLE001 - nothing stored is not an error
            return False


#: Patterns whose matches are scrubbed from anything headed for a log, a
#: trace row or an exception message.
_REDACTIONS = (
    re.compile(r"(password|passwd|pwd|secret|token|api[_-]?key|otp|code)"
               r"(\"?\s*[:=]\s*\"?)([^\s,;\"'}\]]+)", re.I),
    re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(r"\b[A-Za-z0-9._%+-]+:[^\s@/]{6,}@", re.I),  # user:pass@host
)


def redact(text: str, *, extra: tuple[str, ...] = ()) -> str:
    """Scrub credential-shaped substrings.

    Defence in depth for the paths where a value could reach a log despite
    `Secret` — a page's own HTML, a Playwright error quoting a selector
    with a value in it, a URL carrying a token.
    """
    if not text:
        return text
    out = str(text)
    for value in extra:
        if value and len(value) >= 4:
            out = out.replace(value, "<redacted>")
    out = _REDACTIONS[0].sub(r"\1\2<redacted>", out)
    out = _REDACTIONS[1].sub(r"\1 <redacted>", out)
    out = _REDACTIONS[2].sub("<redacted>@", out)
    return out
