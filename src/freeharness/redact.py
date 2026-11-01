"""
Secret redaction for what a run writes to disk (transcripts, supervisor.jsonl, calls.jsonl, stage logs).

`redact(text)` is a pure string -> string function. Every pattern is compiled once at import and none of them matches a
quote, a backslash or a comma inside its secret, so running it over a line of JSON keeps the line valid JSON.
Replacements are `***` (a secret) or `<email>`; surrounding names (`API_KEY=`) are kept so a log still reads.
"""
from __future__ import annotations

import re

_VAL = r"[A-Za-z0-9+/=_.~-]"
_Q = r"[\\\"']*"          # a quote, or the escaped quote of a JSON string, between a name and its value

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
                          re.S)
_AUTH = re.compile(rf"(?i)(\bauthorization{_Q}\s*[:=]\s*{_Q})(?:(?:bearer|basic|token|digest)\s+)?[^\s\"'\\,;]+")
_TOKENS = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
                     r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
                     r"(?:npm|pypi)-[A-Za-z0-9]{30,})")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_ASSIGN = re.compile(rf"(?i)(\b[A-Za-z0-9_.-]*(?:key|token|secret)\b{_Q}\s*[:=]\s*{_Q})({_VAL}{{16,}})")
_PASSWORD = re.compile(rf"(?i)(\b[A-Za-z0-9_.-]*(?:password|passwd)\b{_Q}\s*[:=]\s*{_Q})([^\s\"'\\,;]{{8,}})")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def redact(text: str) -> str:
    if not text:
        return text
    if "-----BEGIN" in text:
        text = _PRIVATE_KEY.sub("***", text)
    low = text.lower()
    if "authorization" in low:
        text = _AUTH.sub(r"\1***", text)
    if "bearer" in low:
        text = _BEARER.sub("Bearer ***", text)
    text = _TOKENS.sub("***", text)
    if "key" in low or "token" in low or "secret" in low:
        text = _ASSIGN.sub(r"\1***", text)
    if "pass" in low:
        text = _PASSWORD.sub(r"\1***", text)
    if "@" in text:
        text = _EMAIL.sub("<email>", text)
    return text
