---
name: memem
description: Show memem status for returning users, or the full welcome for brand-new users. Use when the user wants to check memem or learn about it.
allowed-tools: [Bash, Read]
---

Show memem's current state. Follow these steps in order.

**Step 1 — Detect user type.**
Run these commands:
```bash
[ -f ~/.memem/last-error.md ] && echo "HAS_ERROR" || echo "NO_ERROR"
[ -f ~/.memem/.welcome-shown ] && echo "WELCOME_SHOWN" || echo "WELCOME_NEW"
[ -f ~/.memem/.migrated_from_cortex ] && echo "LEGACY" || echo "NO_LEGACY"
find ~/obsidian-brain/memem/memories -maxdepth 1 -name "*.md" -type f 2>/dev/null | wc -l | tr -d ' '
```

Read the four outputs. The user is **brand new** only if ALL of these are true:
- `NO_ERROR`
- `WELCOME_NEW`
- `NO_LEGACY`
- Memory count is `0`

Otherwise the user is **returning** (even with 0 memories — if they migrated from cortex or dismissed the welcome, they're not brand new).

**Step 2 — If there is a bootstrap error, show it first** regardless of user type.
Run `cat ~/.memem/last-error.md` and display the output under a heading like `### ⚠️ memem bootstrap failed`. Then continue to the appropriate branch.

---

## Branch A — Brand new user

Show the full welcome:

**A1. Print the banner:**

```
  ███╗   ███╗███████╗███╗   ███╗███████╗███╗   ███╗
  ████╗ ████║██╔════╝████╗ ████║██╔════╝████╗ ████║
  ██╔████╔██║█████╗  ██╔████╔██║█████╗  ██╔████╔██║
  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║
  ██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║
  ╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝
  persistent memory for Claude Code
```

**A2. Write the one-sentence pitch:**
> memem gives Claude Code persistent memory across sessions. It extracts durable lessons (decisions, conventions, bug fixes, preferences) from your completed sessions into markdown files, then surfaces the relevant ones at the start of each new session.

**A3. Show the two mining options prominently:**

Check the past session count first:
```bash
find ~/.claude/projects/ -name "*.jsonl" ! -path "*/subagents/*" -size +5k 2>/dev/null | wc -l | tr -d ' '
```

Then show:

> ### 🟡 memem is installed — choose how to start
>
> memem won't mine anything until you start it. Two options:
>
> - **Mine only new sessions going forward.** Every completed Claude Code session will be extracted into durable memories from now on. No API cost for past history.
>   - Say **"start mining new sessions"** — or type `/memem-mine`
>
> - **Mine everything, including your past [N] Claude Code sessions.** Extracts memories from your full history plus all new sessions going forward. Uses Claude Haiku API credits; large histories may take up to an hour.
>   - Say **"start mining everything including history"** — or type `/memem-mine-history`
>
> Replace `[N]` with the session count. If the count is 0, drop the second option entirely.
>
> If the user sounds unsure, recommend the first option as the default.

**A4. Tail:**

> **Optional:** Memories are plain markdown at `~/obsidian-brain/memem/memories/`. Open that folder as an Obsidian vault for graph view. memem works identically without Obsidian.
>
> Commands: `/memem-status`, `/memem-doctor`, `/memem-mine`, `/memem-mine-history`

If useful, add one final plain-English line:

> If you just want the safe default, start with `/memem-mine`.

**A5. Mark welcome as shown so future `/memem` invocations use the compact view:**
```bash
mkdir -p ~/.memem && touch ~/.memem/.welcome-shown
```

---

## Branch B — Returning user

Show only current state. No pedagogy, no ASCII art, no tutorials.

**B1. Run the status command:**
```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m memem.server --status
```

Display the output in a code block.

**B2. Check the miner state:**
```bash
[ -f ~/.memem/.miner-opted-in ] && echo "OPTED_IN" || echo "NOT_OPTED_IN"
pgrep -f "memem.server --mine-all" > /dev/null && echo "MINING_HISTORY" || echo "NOT_MINING_HISTORY"
```

Then show a single one-line state summary matching what the signals show:

- If `OPTED_IN` + `MINING_HISTORY`: "⚙️ memem active — mining past history in background. New sessions will be mined automatically."
- If `OPTED_IN` + `NOT_MINING_HISTORY`: "✅ memem active — miner running, new sessions mined automatically."
- If `NOT_OPTED_IN`: "🟡 memem has N memories but the miner is stopped. Type `/memem-mine` to resume, or `/memem-welcome` to re-read the intro."

**B3. Tail (always, for returning users):**

> Commands: `/memem-status`, `/memem-doctor`, `/memem-mine`, `/memem-mine-history`, `/memem-welcome` (re-show intro)

**Do NOT** print the ASCII banner, "how memem works" explainer, Obsidian note, MCP tool list, or mining-option walkthrough. Returning users don't need them.
