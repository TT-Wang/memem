# Cortex — Persistent Memory

You have access to a persistent memory system via Cortex. This memory persists across sessions and gets smarter over time.

Storage: Obsidian vault at `~/obsidian-brain/cortex/memories/` (markdown files, single source of truth).

## Auto-recall

The UserPromptSubmit hook automatically injects the memory index on your first message. Read the relevant Obsidian memory files directly when you need deeper context.

## Auto-save

When you complete significant work, save non-obvious lessons:

Call `mcp__plugin_cortex_cortex__memory_save` with:
- `content`: The key insight (one per save, keep it atomic)
- `title`: Short descriptive title
- `tags`: Comma-separated relevant tags

Examples of what to save:
- "This project uses Poetry instead of pip"
- "The auth module requires Redis for session storage"
- "Using mock.patch on the class not the instance fixed the test"

Do NOT save:
- Trivial or obvious facts
- Things already documented in the project
- Temporary state or in-progress work

## Mining chat exports

When the user asks to import chat history or conversation exports:

1. Call `mcp__plugin_cortex_cortex__memory_import` with the file/directory path
2. The tool returns raw conversation content
3. YOU extract the knowledge — read through and identify decisions, lessons, conventions, facts
4. For each piece of knowledge, call `mcp__plugin_cortex_cortex__memory_save` with atomic, self-contained content
5. Do NOT save raw conversation text. Extract the insight, discard the chat.

## Available tools

| Tool | What |
|------|------|
| `memory_save` | Store a lesson, pattern, or convention |
| `memory_recall` | Search memories by keyword |
| `memory_list` | List all memories |
| `memory_import` | Import from files, directories, or chat exports |
| `transcript_search` | Search raw Claude Code session logs |
