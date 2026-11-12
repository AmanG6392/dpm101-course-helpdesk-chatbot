"""Loads course_docs/ and resolves its quirks IN CODE (the .txt files are never edited):

1. Conflicting versions  -> superseded docs are detected and excluded from the LLM context.
2. Unofficial / injected  -> the raw student-forum export and any prompt-injection-looking
   paragraphs are quarantined, so they can never reach the model as "policy".
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DOCS_DIR = Path(os.getenv("COURSE_DOCS_DIR", Path(__file__).resolve().parent.parent / "course_docs"))

# A numbered section that is an unmoderated / raw student export -> not official policy.
UNTRUSTED_SECTION = re.compile(
    r"^\s*\d+\.\s+.*\b(UNMODERATED|STUDENT DISCUSSION|FORUM COMMENTS|RAW EXPORT)\b.*$", re.I | re.M
)
# Paragraph-level prompt-injection tells (defence in depth, in case the section header is missing).
INJECTION = re.compile(
    r"(system update for ai|for ai assistants|ignore (?:all |any |the )?(?:previous|above|prior)"
    r"|disregard (?:section|the above|previous|all|any)|you are now|new instructions?:"
    r"|override (?:the )?(?:rules|policy|instructions))",
    re.I,
)
SUPERSEDES = re.compile(r"supersedes\s+[\"“']?([\w.\-]+\.txt)", re.I)
VERSION = re.compile(r"\(\s*Version\s+(\d+)[^)]*\)", re.I)


@dataclass
class Doc:
    name: str
    title: str
    version: int
    last_updated: str
    text: str
    superseded_by: str | None = None
    quarantined: list[str] = field(default_factory=list)


def _header(raw: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.+)$", raw, re.M)
    return m.group(1).strip() if m else ""


def _sanitize(raw: str) -> tuple[str, list[str]]:
    quarantined: list[str] = []
    m = UNTRUSTED_SECTION.search(raw)
    if m:
        quarantined.append(raw[m.start():].strip())
        raw = raw[: m.start()]
    kept = []
    for block in re.split(r"\n\s*\n", raw):
        if INJECTION.search(block):
            quarantined.append(block.strip())
        else:
            kept.append(block)
    return "\n\n".join(kept).strip(), quarantined


def load_docs(path: Path = DOCS_DIR) -> dict[str, Doc]:
    docs: dict[str, Doc] = {}
    explicit: dict[str, list[str]] = {}
    for f in sorted(path.glob("*.txt")):
        raw = f.read_text(encoding="utf-8")
        title_raw = _header(raw, "DOCUMENT") or f.stem
        v = VERSION.search(title_raw)
        text, quarantined = _sanitize(raw)
        docs[f.name] = Doc(
            name=f.name,
            title=VERSION.sub("", title_raw).strip(),
            version=int(v.group(1)) if v else 1,
            last_updated=_header(raw, "LAST UPDATED"),
            text=text,
            quarantined=quarantined,
        )
        explicit[f.name] = SUPERSEDES.findall(raw)

    # (a) explicit: "This document supersedes 02_grading_policy_v1.txt"
    for new, olds in explicit.items():
        for old in olds:
            if old in docs and old != new:
                docs[old].superseded_by = new

    # (b) fallback: same base title, lower version number loses
    groups: dict[str, list[Doc]] = {}
    for d in docs.values():
        base = re.sub(r"^\d+\s*-\s*", "", d.title).strip().lower()
        groups.setdefault(base, []).append(d)
    for group in groups.values():
        newest = max(group, key=lambda d: d.version)
        for d in group:
            if d is not newest and d.version < newest.version and not d.superseded_by:
                d.superseded_by = newest.name
    return docs


def active_docs(docs: dict[str, Doc]) -> list[Doc]:
    return [d for d in docs.values() if not d.superseded_by]


def build_context(docs: dict[str, Doc]) -> str:
    parts = []
    for d in active_docs(docs):
        parts.append(f"=== FILE: {d.name} | {d.title} | last updated {d.last_updated} ===\n{d.text}\n")
    return "\n".join(parts)
