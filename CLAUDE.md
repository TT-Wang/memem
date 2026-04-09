# Cortex — Persistent Memory

You have access to a persistent memory system via Cortex. This memory persists across sessions and gets smarter over time.

## MANDATORY: Auto-recall on every session

At the START of every conversation, BEFORE doing any work, you MUST:

1. Look at the user's first message to understand what they're working on
2. Call `mcp__plugin_cortex_cortex__context_assemble` with a goal derived from their message
3. Use the returned context to inform your work — it contains lessons, patterns, and knowledge from prior sessions

Do this silently. Do not tell the user you're recalling memory unless they ask.

## MANDATORY: Auto-save on completion

When you complete a significant piece of work (not trivial tasks), you MUST save what you learned:

Call `mcp__plugin_cortex_cortex__memory_save` with:
- `content`: The key insight, pattern, or lesson (one per save, keep it atomic)
- `title`: Short descriptive title
- `memory_type`: One of: `lesson`, `knowledge`, `convention`, `failure`, `preference`
- `tags`: Comma-separated relevant tags

Examples of what to save:
- "This project uses Poetry instead of pip" (convention)
- "The auth module requires Redis for session storage" (knowledge)
- "Using mock.patch on the class not the instance fixed the test" (lesson)
- "User prefers short responses without code explanations" (preference)
- "Library X v2.3 has a race condition in async mode" (failure)

Do NOT save:
- Trivial or obvious facts
- Things already documented in the project
- Temporary state or in-progress work

## Auto-feedback

When you notice that recalled context was helpful for the current task, call `mcp__plugin_cortex_cortex__memory_feedback` with `approved=true` and the relevant memory IDs.

When recalled context was wrong or misleading, call `mcp__plugin_cortex_cortex__memory_feedback` with `approved=false`.

This strengthens useful memories and weakens unhelpful ones over time.

## Mining chat exports

When the user asks to import chat history or conversation exports:

1. Call `mcp__plugin_cortex_cortex__memory_import` with the file/directory path
2. The tool returns raw conversation content
3. YOU extract the knowledge — read through the conversations and identify:
   - **Decisions**: "We decided to use X because Y"
   - **Lessons**: "This approach failed because Z"
   - **Conventions**: "The team uses X pattern for Y"
   - **Facts**: "Service X depends on service Y"
   - **Preferences**: "The user prefers X over Y"
4. For each piece of knowledge, call `mcp__plugin_cortex_cortex__memory_save` with:
   - Atomic, self-contained content (one fact per save)
   - Descriptive title
   - Appropriate memory_type and tags
5. Do NOT save raw conversation text. Extract the insight, discard the chat.

Example: If a conversation discusses JWT auth at length, save:
- "Use RS256 for production JWT signing" (lesson, tags: auth,jwt)
- "Refresh tokens stored in Redis with 7-day expiry" (convention, tags: auth,redis)

NOT: "The assistant explained that JWT tokens should be..." (raw text)
