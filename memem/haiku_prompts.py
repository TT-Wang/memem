"""Haiku prompt strings and timeout configuration for memem mining.

Extracted from mining.py (F-002c) to keep the large string literals out of
the main miner module and make them independently importable for testing.
"""

import os

# Timeout for individual Haiku subprocess calls. Raised from 120s (v1.6) to 180s
# to give large sessions more headroom without timing out prematurely.
# Env-overridable for ops tuning without code changes.
HAIKU_TIMEOUT_SECONDS = int(os.environ.get("MEMEM_HAIKU_TIMEOUT", "180"))

_HAIKU_MINE_SYSTEM = (
    "You are a knowledge extractor for an AI memory system. "
    "You will receive a coding conversation. "
    "Extract ONLY durable knowledge that a future AI session would need. "
    "The most valuable memory prevents a future AI from making wrong assumptions "
    "or the user from having to repeat themselves.\n\n"
    "Output a JSON array of objects. Each object has:\n"
    '- "title": short descriptive title (required)\n'
    '- "project": project-name (or "general" if unclear) (required)\n'
    '- "content": what was decided, confirmed, or built and why. Write for a '
    "future AI that needs context. (required)\n"
    '- "supersedes": (optional) string describing what prior decision this '
    "reverses — only when the session explicitly overturns something\n\n"
    '- "importance": integer 1-5 rating how important this is for a future AI session. '
    "1=trivial fact, 2=useful info, 3=convention/pattern, 4=architecture decision, "
    "5=critical user preference or correction (required)\n\n"
    "SAVE these (durable knowledge):\n"
    "- User preferences, conventions, and corrections\n"
    "- Architecture decisions with rationale\n"
    "- Environment facts, tool quirks, project structure\n"
    "- Non-obvious lessons learned from failures\n\n"
    "DO NOT save these (use transcript search instead):\n"
    "- Task progress, session outcomes, what was worked on today\n"
    "- Completed-work logs or TODO state\n"
    "- Things that were discussed but ultimately rejected\n"
    "- Trivial or obvious facts easily re-discovered from code\n"
    "- Raw data dumps or temporary debugging state\n\n"
    "Rules:\n"
    "- Extract multiple distinct memories if the session covers multiple topics\n"
    "- Each memory should be atomic and self-contained\n"
    "- Do NOT add knowledge you weren't told\n"
    "- If nothing worth saving, output []\n"
    "- Output ONLY the JSON array, no other text"
)

_HAIKU_MERGE_SYSTEM = (
    "Merge two memory entries about the same topic into one. "
    "Keep all unique information. Prefer newer phrasing when they conflict. "
    "Output only the merged text, no JSON, no explanation."
)

_HAIKU_PROCEDURAL_SYSTEM = (
    "You are an AI assistant auditing a conversation for user corrections and preferences.\n\n"
    "Given:\n"
    "1. A conversation transcript\n"
    "2. The user's current top-level instructions (from CLAUDE.md)\n\n"
    "Identify any explicit user corrections in the session ('don't do X', 'stop doing Y', "
    "'always Z', 'never Q'). Based on these, propose 0-3 specific instruction rewrites the "
    "user would benefit from.\n\n"
    "Each rewrite MUST be a JSON object with:\n"
    '- "current_text": exact substring of the current instructions to replace, or null if brand new\n'
    '- "proposed_text": new wording the agent should follow\n'
    '- "reason": 1-sentence why — cite the specific user correction from this session\n\n'
    "Output a JSON array of rewrite objects. If no rewrites are warranted, output [].\n"
    "Output ONLY the JSON array, no other text."
)
