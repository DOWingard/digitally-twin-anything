"""Recognising a literal credential written into an address or a header.

Credentials belong in environment variables and appear in the manifest only as
`${NAME}` placeholders. These helpers describe what was found without ever
returning the offending value, so findings and logs stay free of secrets.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from ...log import Log

log = Log("manifest.checks.secrets")

_NAME = r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"
# Parameter and header names under which a value is a credential.
_CREDENTIAL_NAME = re.compile(
    r"pass(word|wd)?|pwd|secret|token|api[-_]?key|access[-_]?key|private[-_]?key|credential"
    r"|cookie|signature|authori[sz]ation|auth(?!or)|^sig$|^key$",
    re.IGNORECASE,
)
# Placeholders only, e.g. `${DB_PASSWORD}`.
_PLACEHOLDERS_ONLY = re.compile(rf"(?:{_NAME})+")
# Placeholders with an optional scheme word, e.g. `Bearer ${API_TOKEN}`.
_SCHEMED_PLACEHOLDER = re.compile(rf"\s*(?:[A-Za-z][A-Za-z0-9_-]*\s+)?(?:{_NAME})+\s*")
# Userinfo of a URL that cannot be parsed in full (for example a malformed IPv6 host).
_USERINFO_PASSWORD = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]*?:([^/@\s]*)@")


def credential_name(name: str) -> bool:
    return bool(_CREDENTIAL_NAME.search(name))


def literal_value(value: str) -> bool:
    """True when a credential value is written out instead of referenced."""
    return value.strip() != "" and not _SCHEMED_PLACEHOLDER.fullmatch(value)


def _literal_password(password: str | None) -> bool:
    return bool(password) and _PLACEHOLDERS_ONLY.fullmatch(password) is None


def url_problems(url: str) -> list[str]:
    """What literal credentials a URL carries, described without their values."""
    try:
        parts = urlsplit(url)
        password = parts.password
    except ValueError as exc:
        log.debug("address is not a parseable URL; checking userinfo only", reason=str(exc))
        match = _USERINFO_PASSWORD.match(url)
        if match and _literal_password(match.group(1)):
            return ["a literal password in the URL userinfo"]
        return []
    problems = []
    if _literal_password(password):
        problems.append("a literal password in the URL userinfo")
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if credential_name(name) and literal_value(value):
            problems.append(f"a literal value for query parameter '{name}'")
    return problems
