"""Security scanning for memory content.

Scans memory content for prompt injection patterns, credential exfiltration
attempts, and invisible unicode characters. Memory content is injected into
future sessions' context — a successful injection here poisons all future
interactions.
"""

import re


_INJECTION_PATTERNS = [
    (r"ignore\s+.{0,20}(previous|prior|above|all)\s+.{0,10}instructions", "prompt_injection"),
    (r"ignore\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"do\s+not\s+tell\s+the\s+user", "deception"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
    (r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don't\s+have)\s+(restrictions|limits|rules)", "bypass_restrictions"),
]

_EXFIL_PATTERNS = [
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
    (r"authorized_keys", "ssh_backdoor"),
    (r"\$HOME/\.ssh|~/\.ssh", "ssh_access"),
]

_INVISIBLE_CHARS = {
    "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
}


def scan_memory_content(content: str) -> str | None:
    """Scan memory content for injection/exfil patterns.

    Returns error string if blocked, None if clean.
    """
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: invisible unicode U+{ord(char):04X} (possible injection)"

    text = content if isinstance(content, str) else ""
    for pattern, name in _INJECTION_PATTERNS + _EXFIL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return f"Blocked: matches threat pattern '{name}'"

    return None
