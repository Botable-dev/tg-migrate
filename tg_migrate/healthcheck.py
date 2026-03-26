"""Health check — static analysis for known migration anti-patterns.

Based on 9 real edge cases from the Verbatica March 2026 migration.
See: Documentation/migration_runbook.md §4
"""

import re
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def _strip_comments(text: str) -> str:
    """Strip Python # comments from code text (preserves strings)."""
    # Simple heuristic: remove everything after # that is not inside quotes
    lines = []
    for line in text.split("\n"):
        # Find # that's not inside a string
        in_single = False
        in_double = False
        result = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == '#' and not in_single and not in_double:
                break
            result.append(ch)
            i += 1
        lines.append("".join(result))
    return "\n".join(lines)


@dataclass
class Finding:
    check: str
    severity: str    # critical | warning | info
    file: str
    line: int
    message: str
    fix: str


# ── Pattern definitions ──

CHECKS = [
    {
        "name": "infinite_retry",
        "description": "Chat not found without marking sent/permanent",
        "severity": "critical",
        "pattern": r"(?:Chat not found|chat not found)",
        "anti_pattern": r"(?:permanent|unreachable|sent\s*=\s*True|is_permanent)",
        "fix": (
            "When a send fails with 'Chat not found', mark the notification as "
            "sent=True or the user as unreachable. Without this, cron jobs will "
            "retry every cycle, creating an infinite spam loop."
        ),
    },
    {
        "name": "recipient_loop",
        "description": "Broadcast loop without per-recipient error handling",
        "severity": "critical",
        "pattern": r"for\s+\w+\s+in\s+(?:recipients|admins|users|chat_ids)",
        "anti_pattern": r"try\s*:",
        "context_lines": 10,
        "fix": (
            "Wrap each send in a try/except inside the loop. One dead chat_id "
            "will kill the entire loop, preventing delivery to remaining recipients "
            "and causing dedup to miss — leading to spam on next cycle."
        ),
    },
    {
        "name": "null_dedup",
        "description": "SQL IN() on nullable column (NULL != NULL)",
        "severity": "warning",
        "pattern": r"\.in_\(\s*['\"]record_id['\"]",
        "anti_pattern": r"is_\(\s*['\"]record_id['\"].*null",
        "fix": (
            "SQL IN(NULL) always returns FALSE. If record_id can be NULL, "
            "add a separate query: .is_('record_id', 'null'). "
            "Without this, NULL-record events are never deduped → infinite reprocessing."
        ),
    },
    {
        "name": "stale_callback",
        "description": "Missing 'Message is not modified' error suppression",
        "severity": "warning",
        "pattern": r"error_handler|def\s+\w*error\w*",
        "anti_pattern": r"[Mm]essage is not modified",
        "fix": (
            "When users double-click inline buttons, Telegram throws "
            "'Message is not modified'. If the error handler shows a generic "
            "error to the user, they think the bot is broken. Silently ignore this error."
        ),
    },
    {
        "name": "session_kill",
        "description": "Session data destroyed on validation failure",
        "severity": "warning",
        "pattern": r"\.pop\(\s*['\"](?:hw_session|session|state)['\"]",
        "anti_pattern": r"(?:show_alert|continue|return\s+None)",
        "context_lines": 5,
        "fix": (
            "Don't pop/destroy session data when validation fails (e.g., "
            "'no files uploaded'). Show an alert instead and keep the session alive. "
            "Otherwise the user can't recover without starting over."
        ),
    },
    {
        "name": "double_answer",
        "description": "Multiple query.answer() in same callback handler",
        "severity": "info",
        "pattern": r"query\.answer\(",
        "count_threshold": 2,
        "fix": (
            "Telegram allows only ONE query.answer() per callback. "
            "Multiple calls will throw BadRequest. Move answer() into each "
            "code branch instead of calling at the start + in error handling."
        ),
    },
]


def scan_file(filepath: Path) -> list[Finding]:
    """Scan a single Python file for known anti-patterns."""
    findings = []

    try:
        content = filepath.read_text(encoding="utf-8")
        lines = content.split("\n")
    except Exception as e:
        log.warning(f"Could not read {filepath}: {e}")
        return findings

    for check in CHECKS:
        name = check["name"]
        pattern = re.compile(check["pattern"], re.IGNORECASE)
        anti_pattern = re.compile(check.get("anti_pattern", ""), re.IGNORECASE) if check.get("anti_pattern") else None
        ctx = check.get("context_lines", 20)
        count_threshold = check.get("count_threshold")

        # Count-based check (e.g., double query.answer)
        if count_threshold:
            matches = [(i, line) for i, line in enumerate(lines) if pattern.search(line)]
            # Group by function (simple heuristic: same def block)
            func_matches = {}
            current_func = "<module>"
            for i, line in enumerate(lines):
                if re.match(r"^(?:async\s+)?def\s+(\w+)", line):
                    current_func = re.match(r"^(?:async\s+)?def\s+(\w+)", line).group(1)
                if pattern.search(line):
                    func_matches.setdefault(current_func, []).append(i)

            for func, idxs in func_matches.items():
                if len(idxs) >= count_threshold:
                    findings.append(Finding(
                        check=name,
                        severity=check["severity"],
                        file=str(filepath),
                        line=idxs[0] + 1,
                        message=f"{check['description']} in function '{func}' ({len(idxs)} calls)",
                        fix=check["fix"],
                    ))
            continue

        # Pattern + anti-pattern check
        for i, line in enumerate(lines):
            if not pattern.search(line):
                continue

            # Check if anti-pattern exists nearby (context window)
            start = max(0, i - ctx)
            end = min(len(lines), i + ctx)
            context_block = "\n".join(lines[start:end])
            # Strip comments so that TODOs/notes don't suppress findings
            context_code = _strip_comments(context_block)

            if anti_pattern and anti_pattern.search(context_code):
                continue

            findings.append(Finding(
                check=name,
                severity=check["severity"],
                file=str(filepath),
                line=i + 1,
                message=check["description"],
                fix=check["fix"],
            ))
            # One finding per check per file is enough
            break

    return findings


def scan_directory(path: str, extensions: tuple = (".py",)) -> list[Finding]:
    """Scan all Python files in a directory for anti-patterns."""
    root = Path(path)
    all_findings = []

    if root.is_file():
        return scan_file(root)

    for py_file in sorted(root.rglob("*")):
        if py_file.suffix in extensions and not any(
            part.startswith(".") or part in (
                "__pycache__", "node_modules", "venv", ".venv",
                "site-packages", ".git", ".tox", ".mypy_cache",
            )
            for part in py_file.parts
        ):
            all_findings.extend(scan_file(py_file))

    return all_findings
