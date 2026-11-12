"""Redact credentials/secrets from every string written into a system map or overlay (spec §11). Redact, don't drop."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # PEM private keys (whole block)
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.S),
     "-----BEGIN PRIVATE KEY-----" + REDACTED + "-----END PRIVATE KEY-----"),
    # GitHub tokens
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{20,}\b"), r"\1" + REDACTED),
    (re.compile(r"\b(github_pat_)[A-Za-z0-9_]{20,}\b"), r"\1" + REDACTED),
    # AWS access key ids
    (re.compile(r"\b((?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16})\b"), REDACTED),
    # Slack, Anthropic, OpenAI-style, Google API keys, Stripe
    (re.compile(r"\b(xox[abprs]-)[A-Za-z0-9-]{10,}"), r"\1" + REDACTED),
    (re.compile(r"\b(sk-ant-)[A-Za-z0-9_-]{10,}"), r"\1" + REDACTED),
    (re.compile(r"\b(sk-(?:proj|svcacct|admin)-)[A-Za-z0-9_-]{20,}"), r"\1" + REDACTED),
    (re.compile(r"\b(sk-)[A-Za-z0-9]{20,}\b"), r"\1" + REDACTED),
    (re.compile(r"\b(AIza)[0-9A-Za-z_-]{35}\b"), r"\1" + REDACTED),
    (re.compile(r"\b((?:sk|rk)_live_)[0-9A-Za-z]{10,}"), r"\1" + REDACTED),
    # GitLab personal access tokens, npm tokens, Hugging Face tokens
    (re.compile(r"\b(glpat-)[A-Za-z0-9_-]{20,}"), r"\1" + REDACTED),
    (re.compile(r"\b(npm_)[A-Za-z0-9]{36}\b"), r"\1" + REDACTED),
    (re.compile(r"\b(hf_)[A-Za-z0-9]{30,}\b"), r"\1" + REDACTED),
    # `Bearer <token>` (Authorization headers): a 16+ char credential-alphabet run with a digit spares prose
    (re.compile(r"(?i)(\bbearer\s+)(?=[A-Za-z0-9._~+/-]*\d)[A-Za-z0-9._~+/-]{16,}=*"), r"\1" + REDACTED),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # URLs with embedded credentials
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)"), r"\1" + REDACTED + r"\2"),
    # KEY="quoted value, possibly with spaces" — quotes/backticks are kept, only the value is redacted
    (re.compile(
        r"""(?ix)
        (\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth[_-]?key|auth[_-]?token)[\w.-]*
         ["']?\s*=\s*)
        (["'`])(?!\[REDACTED\])((?:(?!\2)[^\n])+)\2"""),
     r"\1\2" + REDACTED + r"\2"),
    # KEY=value (.env / shell / Python) whose key names a secret
    (re.compile(
        r"""(?ix)
        (\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth[_-]?key|auth[_-]?token)[\w.-]*
         ["']?\s*=\s*)
        (?!\[REDACTED\])(?![\w.]+[(\[])
        (["']?)([^\s"'`,;)}\]<>]{3,})\2"""),
     r"\1\2" + REDACTED + r"\2"),
    # "key": "value" / key: 'value with spaces' (YAML/JSON) — quoted values
    (re.compile(
        r"""(?ix)
        (\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)[\w.-]*
         ["']?\s*:\s*)
        (["'])(?!\[REDACTED\])((?:(?!\2)[^\n]){3,})\2"""),
     r"\1\2" + REDACTED + r"\2"),
    # key: value (unquoted YAML) — only a line-leading key whose value is one 8+ char token with a digit or symbol
    # running to end of line, to spare prose like "Token: see docs" / "password: required" and `${VAR}` references.
    # Ceiling: an all-letters unquoted YAML secret (`password: changeme`) is not caught.
    (re.compile(
        r"""(?imx)
        ^(\s*(?:-\s+)?[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)[\w.-]*
          \s*:[ \t]+)
        (?!\[REDACTED\])(?![$%{<])
        (?=[^\s"'#]*[\d/+=!@*^&-])[^\s"'#]{8,}
        (?=[ \t]*(?:\#.*)?$)"""),
     r"\1" + REDACTED),
]


# Pre-filter: every pattern above can only match text containing one of these literals, under the same case rules
# (case-sensitive literals stay case-sensitive; the `(?i)` patterns' keywords sit in a `(?i:…)` group, so Unicode
# case-folding such as "ſ" ~ "s" matches exactly as in the patterns). A string the trigger does not find is left
# unchanged by every pattern, so `scrub` returns it as is — the same result, without 22 regex passes. Adding a
# pattern means adding its required literal here (tests/unit/test_secret_scrubber.py checks each pattern's samples).
_TRIGGER = re.compile(
    r"-----BEGIN |gh[pousr]_|github_pat_|AKIA|ASIA|ABIA|ACCA|xox[abprs]-|sk-|AIza|[sr]k_live_|glpat-|npm_|hf_|eyJ|://"
    r"|(?i:bearer|password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential"
    r"|auth[_-]?key|auth[_-]?token)")


def _apply_patterns(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def scrub(text: str) -> str:
    if not text or not _TRIGGER.search(text):
        return text
    return _apply_patterns(text)


def scrub_tree(value: Any, skip_keys: frozenset[str] = frozenset()) -> Any:
    """Recursively scrub every string in a JSON-like structure. `scrub` is pure, so each distinct string is scrubbed
    once (a system map repeats IDs and paths: ~110k distinct of ~440k strings on a large repository)."""
    memo: dict[str, str] = {}

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            out = memo.get(v)
            if out is None:
                out = memo[v] = scrub(v)
            return out
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: (x if k in skip_keys else walk(x)) for k, x in v.items()}
        return v

    return walk(value)


class SecretScrubber:
    def scrub(self, text: str) -> str:
        return scrub(text)
