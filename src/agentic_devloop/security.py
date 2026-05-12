from __future__ import annotations

import re
from typing import Any


# Keep controller-generated branch names and run directory names compact and portable.
# The 64-character cap leaves room for prefixes like feature/, agent/, and timestamps.
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_INLINE_SECRET_RE = re.compile(
    r"(?im)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY)[A-Z0-9_]*)\b(\s*[:=]\s*)([^\s]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{12,}\b")
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b(https?://)([^/\s:@]+):([^@\s]+)@")
_TOKEN_LITERAL_RE = re.compile(
    r"\b("
    r"ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9\-_]{20,}|"
    r"sk-[A-Za-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z\-_]{35}"
    r")\b"
)


def validate_identifier(value: str, *, kind: str) -> str:
    if not SAFE_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{kind} must use only letters, digits, '.', '_' or '-' and may not contain path separators"
        )
    if value in {".", ".."}:
        raise ValueError(f"{kind} must not be '.' or '..'")
    return value


def redact_text(text: str) -> str:
    redacted = _INLINE_SECRET_RE.sub(r"\1\2[REDACTED]", text)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]:[REDACTED]@", redacted)
    redacted = _TOKEN_LITERAL_RE.sub("[REDACTED]", redacted)
    return redacted


def redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    return value
