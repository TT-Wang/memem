"""Non-mutating delta proposals for Active Memory Slice runs."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from memem.active_slice import ActiveMemorySlice


class DeltaProposal(TypedDict, total=False):
    delta_id: str
    delta_type: Literal[
        "save_new_memory",
        "deprecate_memory",
        "add_related_link",
        "add_open_tension_memory",
    ]
    target_memory_ids: list[str]
    reason: str
    evidence: dict
    confidence: float
    proposed_content: str
    proposed_title: str
    source_slice_id: str
    requires_user_confirmation: bool


def _delta_id(delta_type: str, payload: dict) -> str:
    encoded = json.dumps({"type": delta_type, **payload}, sort_keys=True, default=str)
    return f"delta_{hashlib.sha1(encoded.encode('utf-8')).hexdigest()[:12]}"


def propose_deltas_from_slice(slice_obj: ActiveMemorySlice) -> list[DeltaProposal]:
    """Propose safe memory changes without mutating the vault."""
    deltas: list[DeltaProposal] = []
    slice_id = slice_obj.get("slice_id", "")

    for tension in slice_obj.get("open_tensions", [])[:3]:
        description = tension.get("description", "")
        if not description:
            continue
        payload: dict[str, object] = {"description": description, "slice_id": slice_id}
        deltas.append({
            "delta_id": _delta_id("add_open_tension_memory", payload),
            "delta_type": "add_open_tension_memory",
            "target_memory_ids": tension.get("linked_memory_ids", []),
            "reason": "Open tension surfaced in active slice.",
            "evidence": {"tension": tension},
            "confidence": 0.55,
            "proposed_title": f"Open tension — {description[:80]}",
            "proposed_content": description,
            "source_slice_id": slice_id,
            "requires_user_confirmation": True,
        })

    # Relation deltas stay conservative: propose links between the strongest
    # selected memories in different sections when both are explicit memory IDs.
    selected: list[str] = []
    sections = (
        slice_obj.get("constraints", []),
        slice_obj.get("decisions", []),
        slice_obj.get("failure_patterns", []),
        slice_obj.get("active_background", []),
    )
    for section in sections:
        for item in section[:3]:
            mid = item.get("memory_id", "")
            if mid:
                selected.append(mid)
    if len(selected) >= 2:
        payload = {"ids": selected[:2], "slice_id": slice_id}
        deltas.append({
            "delta_id": _delta_id("add_related_link", payload),
            "delta_type": "add_related_link",
            "target_memory_ids": selected[:2],
            "reason": "Selected memories co-activated in the same active slice.",
            "evidence": {"slice_id": slice_id},
            "confidence": 0.5,
            "source_slice_id": slice_id,
            "requires_user_confirmation": True,
        })

    return deltas
