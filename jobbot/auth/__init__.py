"""Authentication subsystem (spec §25-§32)."""
from jobbot.auth.credentials import Credential, CredentialStore, Secret, redact
from jobbot.auth.detect import detect_auth_state, is_authenticated
from jobbot.auth.orchestrator import AuthOrchestrator, AuthResult, VerificationChannel
from jobbot.auth.states import AuthOutcome, AuthState, VerificationChannelState

__all__ = [
    "AuthOrchestrator", "AuthOutcome", "AuthResult", "AuthState", "Credential",
    "CredentialStore", "Secret", "VerificationChannel", "VerificationChannelState",
    "detect_auth_state", "is_authenticated", "redact",
]
