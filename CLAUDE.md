# Cortex

Cortex is your persistent memory. It remembers knowledge across sessions and assembles relevant context for every task.

## How to use

Before starting work on any task, recall relevant memory:

```
mcp__cortex__context_assemble(goal="your task description")
```

This searches your memory + the web and returns the best context for your task.

After completing work successfully, save what you learned:

```
mcp__cortex__memory_save(content="key insight or pattern", title="short title", memory_type="lesson")
```

## Tools

- `context_assemble` — Assemble relevant context for a goal. Call this first on any task.
- `memory_save` — Save a piece of knowledge. Use after discovering patterns, conventions, or lessons.
- `memory_recall` — Search memory by keyword. Use when you need specific knowledge.
- `memory_list` — List all memories in a scope.
- `memory_feedback` — Report whether recalled memories were useful (approved/rejected). This strengthens or weakens memories over time.
- `memory_stats` — Show memory system statistics.
- `memory_promote` — Promote a candidate memory to learned status.

## When to save memory

Save when you discover:
- **Conventions**: "This project uses snake_case for API endpoints"
- **Lessons**: "RS256 is better than HS256 for production JWT"
- **Patterns**: "Always validate token expiry server-side"
- **Failures**: "Library X has a memory leak in version 2.3"
- **Preferences**: "User prefers concise responses without summaries"

Do NOT save trivial or obvious knowledge. Only save what would help a future session.

## Memory lifecycle

1. New knowledge starts as **candidate** (confidence 0.5)
2. When retrieved and the task succeeds → confidence increases
3. When retrieved and the task fails → confidence decreases
4. High-confidence memories are auto-promoted to **learned**
5. Learned memories are prioritized in future context assembly
