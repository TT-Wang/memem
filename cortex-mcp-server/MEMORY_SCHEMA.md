# Cortex Memory Schema Spec

This document defines the AI-native memory classification system for Cortex. It replaces the old `memory_type` field with a richer `why:` verb vocabulary, specifies the index line format, and describes the Obsidian frontmatter structure.

---

## 1. The `why:` Verb Vocabulary

Each memory is tagged with a single verb that answers *why this memory was saved*. The verb replaces the old `memory_type` field.

| Verb | Meaning | Old equivalent |
|------|---------|----------------|
| `decided` | A decision was made | knowledge |
| `learned` | A lesson from experience | lesson |
| `convention` | A pattern or rule we follow | convention |
| `prefers` | A user preference | preference |
| `failed` | Something that didn't work | failure |
| `shipped` | A feature or change was delivered | knowledge |
| `discovered` | Found something new | knowledge |
| `designed` | An architecture or design choice | knowledge |
| `blocked-by` | An obstacle or limitation | failure |

These 9 verbs are the complete vocabulary. Every memory must use exactly one.

---

## 2. Index Line Format

Each memory occupies exactly one line in the index:

```
- [MM-DD] verb: one-line description | #tag1 #tag2
```

**Rules:**
- Date is month-day only (`MM-DD`), no year
- Verb is one of the 9 verbs from the vocabulary above
- Description is a single line, kept short and scannable
- Tags are prefixed with `#` and separated by spaces
- The `|` separator must have a space on each side

**Example:**
```
- [04-09] decided: use Haiku to rewrite hook queries for vector DB | #retrieval #haiku
- [04-09] learned: embedding cold-start doubles first-query latency | #performance #embeddings
- [03-28] convention: all MCP tools return structured JSON with status field | #mcp #api
- [03-15] shipped: memory recall endpoint with cosine similarity scoring | #recall #search
```

---

## 3. Index File Structure (`_index.md`)

The index file groups memories by project. Project names are extracted from the `[project-name]` prefix in memory titles. Memories without a project prefix go under `## general`.

```markdown
# Cortex Memory Index
Updated: YYYY-MM-DD | Total: N memories

## project-name (N memories)
- [MM-DD] verb: description | #tags
- [MM-DD] verb: description | #tags

## another-project (N memories)
- [MM-DD] verb: description | #tags

## general (N memories)
- [MM-DD] verb: description | #tags
```

**Rules:**
- Projects are listed alphabetically, with `general` last
- Memories within each project are sorted newest-first (by date)
- The `Updated:` line reflects the date the index was last regenerated
- The `Total:` count includes all memories across all projects
- Each section header includes the memory count for that project

---

## 4. Memory File Frontmatter (Obsidian)

Every memory saved to the Obsidian vault uses this YAML frontmatter:

```yaml
---
title: Short descriptive title
who: project-name
why: verb
tags: [tag1, tag2, tag3]
confidence: 0.8
created: 2026-04-09
related: [other-memory-slug-id, another-memory-slug-id]
---
```

**Field definitions:**

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Short, human-readable title describing the memory |
| `who` | string | Project name (matches the prefix used in index grouping) |
| `why` | string | One verb from the vocabulary (e.g. `decided`, `learned`) |
| `tags` | list | Keyword tags for retrieval; no `#` prefix in frontmatter |
| `confidence` | float | LLM confidence in the memory's accuracy, 0.0–1.0 |
| `created` | date | ISO 8601 date the memory was created (`YYYY-MM-DD`) |
| `related` | list | Slug IDs of related memories (optional, may be empty list) |

**Notes:**
- `who` should be `general` when no project context is known
- `confidence` defaults to `0.8` when not explicitly set
- `related` is populated by the memory system when semantic neighbors are found
- The file slug (filename without extension) is derived from `title` lowercased and hyphenated

---

## 5. Mapping from Old Schema

When migrating memories from the old `memory_type` field to `why:`, apply these rules in order:

| Old `memory_type` | Condition on `essence` | New `why:` |
|-------------------|------------------------|------------|
| `lesson` | (any) | `learned` |
| `knowledge` | contains "decided", "chose", or "went with" | `decided` |
| `knowledge` | contains "shipped", "added", or "built" | `shipped` |
| `knowledge` | contains "found" or "discovered" | `discovered` |
| `knowledge` | contains "designed" or "architecture" | `designed` |
| `knowledge` | (default, no match) | `discovered` |
| `convention` | (any) | `convention` |
| `preference` | (any) | `prefers` |
| `failure` | contains "blocked", "can't", or "doesn't work" | `blocked-by` |
| `failure` | (default, no match) | `failed` |

**Implementation note:** Apply the `knowledge` rules with a case-insensitive substring match against the memory's `essence` field. The first matching rule wins; fall through to the default only when no condition matches.
