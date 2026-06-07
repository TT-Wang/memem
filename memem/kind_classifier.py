"""Heuristic kind classifier for untagged memories (memem v1.13.0).

Classifies a memory dict into one of four kinds:
  - 'episodic' — a timestamped event or experience
  - 'skill'    — a reusable pattern, convention, or technique
  - 'case'     — a structured task narrative (problem → approach → result)
  - 'other'    — fallback when no signal matches strongly

The classifier:
  1. First checks tags — if any tag is 'type:episodic', 'type:skill', or
     'type:case', it returns that kind immediately (user tags always win).
  2. For untagged memories, runs additive heuristic scoring across the
     four categories; the category with the most signals wins.
  3. Writes the result to ``inferred_kind`` on the dict — it NEVER modifies
     tags or any on-disk field.

``inferred_kind`` is ephemeral — computed at recall time, never persisted.
"""
from __future__ import annotations

import re
from typing import Literal

# ---------------------------------------------------------------------------
# Compiled patterns (module-level for performance)
# ---------------------------------------------------------------------------

# ISO date (YYYY-MM-DD) anywhere in text
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# Past-tense verbs strongly associated with episodic events
_PAST_TENSE_RE = re.compile(
    r"\b(?:shipped|fixed|discovered|deployed|merged|broke|failed|"
    r"completed|resolved|encountered|happened|was|did|confirmed|"
    r"released|reverted|renamed|migrated|upgraded|downgraded)\b",
    re.IGNORECASE,
)

# Imperative/reusability signals for skills
_SKILL_KEYWORD_RE = re.compile(
    r"\b(?:always|must|never|convention|pattern|whenever|prefer|"
    r"avoid|use|run|check|ensure|remember|note|tip|warning|"
    r"gotcha|trick|idiom)\b",
    re.IGNORECASE,
)

# Titles that start with an imperative verb (skill signal)
_IMPERATIVE_TITLE_RE = re.compile(
    r"^(?:use|run|check|avoid|always|never|prefer|ensure|set|"
    r"call|add|remove|keep|make|put|pass|wrap|import|export|"
    r"enable|disable|configure|install|upgrade|deploy)\b",
    re.IGNORECASE,
)

# Numbered step structure (3+ contiguous numbered steps → case)
_NUMBERED_STEP_LINE_RE = re.compile(r"^\s*(\d+)[.)]\s+", re.MULTILINE)

# Explicit problem/approach/result structure (case signal)
_CASE_STRUCTURE_RE = re.compile(
    r"\b(?:tried|problem|issue|approach|result|solution|root cause|"
    r"fix|workaround|outcome|conclusion|summary)\b",
    re.IGNORECASE,
)

# "1. Tried:" pattern (EverMe-style case entry)
_TRIED_STEP_RE = re.compile(r"\b1\.\s+tried:", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Tag priority map
# ---------------------------------------------------------------------------

_TAG_KIND_MAP = {
    "type:episodic": "episodic",
    "type:skill": "skill",
    "type:case": "case",
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

Kind = Literal["episodic", "skill", "case", "other"]


def infer_kind(memory: dict) -> Kind:
    """Classify *memory* and set ``memory['inferred_kind']``.

    Returns the inferred kind string as well.

    The caller should use the return value OR read ``memory['inferred_kind']``
    — both are always consistent after this call.

    Priority:
      1. User-set ``type:*`` tag → direct mapping, no heuristics
      2. Additive heuristic scoring → highest signal count wins
      3. Fallback to 'other' when no category gets any signals

    This function is idempotent: calling it twice on the same dict yields
    the same result.
    """
    # 1. Tag-priority path: check tags list for type:* markers
    tags: list[str] = memory.get("tags") or memory.get("domain_tags") or []
    for tag in tags:
        tag_lower = tag.lower().strip()
        if tag_lower in _TAG_KIND_MAP:
            kind: Kind = _TAG_KIND_MAP[tag_lower]  # type: ignore[assignment]
            memory["inferred_kind"] = kind
            return kind

    # 2. Heuristic scoring on title + body
    title: str = memory.get("title", "") or ""
    body: str = (
        memory.get("full_record", "")
        or memory.get("essence", "")
        or memory.get("content", "")
        or ""
    )
    text = title + "\n" + body

    # --- Episodic signals ---
    episodic_score = 0
    if _ISO_DATE_RE.search(text):
        episodic_score += 1
    if _PAST_TENSE_RE.search(title):
        episodic_score += 1
    if _PAST_TENSE_RE.search(body):
        episodic_score += 1

    # --- Skill signals ---
    skill_score = 0
    if _IMPERATIVE_TITLE_RE.match(title.strip()):
        skill_score += 1
    skill_matches = len(_SKILL_KEYWORD_RE.findall(text))
    if skill_matches >= 2:
        skill_score += 2
    elif skill_matches == 1:
        skill_score += 1

    # --- Case signals ---
    case_score = 0
    step_numbers = [int(m) for m in _NUMBERED_STEP_LINE_RE.findall(body)]
    if len(step_numbers) >= 3:
        # Check for contiguous steps starting at 1
        step_numbers_sorted = sorted(set(step_numbers))
        if step_numbers_sorted[:3] == [1, 2, 3]:
            case_score += 2
        else:
            case_score += 1
    if _TRIED_STEP_RE.search(body):
        case_score += 2
    if _CASE_STRUCTURE_RE.search(body):
        case_score += 1

    # 3. Determine winner — require at least 1 signal; ties broken by priority order
    best_score = max(episodic_score, skill_score, case_score)
    if best_score == 0:
        kind = "other"
    elif episodic_score == best_score and episodic_score >= skill_score and episodic_score >= case_score:
        kind = "episodic"
    elif skill_score == best_score and skill_score >= case_score:
        kind = "skill"
    elif case_score == best_score:
        kind = "case"
    else:
        kind = "other"

    memory["inferred_kind"] = kind
    return kind
