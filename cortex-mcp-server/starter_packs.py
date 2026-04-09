"""
Starter packs — built-in universal knowledge for new users.

Each pack is a list of (title, content, memory_type, tags) tuples.
These are loaded on first use to eliminate the cold start problem.
"""
import json
import os

# ─── Universal (always loaded) ────────────────────────────────────

UNIVERSAL = [
    # Software engineering fundamentals
    ("Write tests before refactoring",
     "Before changing existing code, write tests that verify current behavior. Run them after each change. If tests pass, the refactor is safe. If they fail, you know exactly what broke.",
     "lesson", ["engineering", "testing", "refactoring"]),

    ("Small commits, single purpose",
     "Each commit should do one thing. 'Fix auth bug' not 'Fix auth bug and refactor database and update docs'. Small commits are easier to review, revert, and bisect.",
     "convention", ["git", "workflow"]),

    ("Read the error message fully",
     "Before searching for a solution, read the entire error message including the stack trace. The fix is often described in the error itself. Pay attention to line numbers, file paths, and the actual exception type.",
     "lesson", ["debugging"]),

    ("Check what changed when things break",
     "If something worked yesterday but not today: git diff, check dependency updates, check environment changes, check config changes. The cause is almost always in what changed, not in what stayed the same.",
     "procedure", ["debugging", "troubleshooting"]),

    ("Never commit secrets",
     "API keys, passwords, tokens, .env files, credentials.json — never commit these. Use environment variables or secret managers. If accidentally committed, rotate the secret immediately — git history preserves it forever.",
     "lesson", ["security", "git"]),

    ("Validate at boundaries, trust internally",
     "Validate user input, API responses, and file contents at system boundaries. Once data is inside your system and validated, don't re-validate at every layer — it adds complexity without safety.",
     "lesson", ["architecture", "security"]),

    ("Prefer explicit over implicit",
     "Name variables clearly, make dependencies visible, document non-obvious decisions. Code is read 10x more than it's written. Future-you is a different person who won't remember why.",
     "convention", ["code-quality"]),

    ("Don't optimize prematurely",
     "Write correct code first, then measure, then optimize the bottleneck. Most performance intuitions are wrong. Profile before optimizing. The slow part is rarely where you think it is.",
     "lesson", ["performance", "engineering"]),

    # Version control
    ("Use conventional commit messages",
     "Format: type(scope): description. Types: feat, fix, docs, refactor, test, chore. Examples: 'feat(auth): add JWT refresh tokens', 'fix(api): handle null response'. Makes git log readable and enables automated changelogs.",
     "convention", ["git", "workflow"]),

    ("Branch from main, merge to main",
     "Feature branches should be short-lived (hours to days, not weeks). The longer a branch lives, the harder the merge. Rebase on main regularly to stay current.",
     "convention", ["git", "workflow"]),

    # Code review
    ("Review your own code first",
     "Before requesting review, read your own diff as if you're the reviewer. You'll catch obvious issues, typos, leftover debug code, and missing edge cases. This saves reviewer time and improves quality.",
     "procedure", ["code-review", "workflow"]),

    # Error handling
    ("Handle errors at the right level",
     "Don't catch exceptions where you can't handle them meaningfully. Let them propagate to a level that can log, retry, or notify. Catching and ignoring (except: pass) hides bugs.",
     "lesson", ["error-handling", "engineering"]),

    ("Log context, not just the error",
     "Bad: 'Error occurred'. Good: 'Failed to create user: email=x@y.com, error=unique constraint violated'. Include what you were trying to do, with what inputs, and what went wrong.",
     "lesson", ["logging", "debugging"]),

    # API design
    ("Use consistent naming in APIs",
     "Pick one convention and stick to it. snake_case or camelCase, not both. Plural endpoints (/users not /user). Consistent verbs (GET=read, POST=create, PUT=replace, PATCH=update, DELETE=remove).",
     "convention", ["api", "design"]),

    ("Return meaningful HTTP status codes",
     "200=OK, 201=Created, 400=Bad Request (client error), 401=Unauthorized (not logged in), 403=Forbidden (no permission), 404=Not Found, 409=Conflict, 422=Validation Error, 500=Server Error. Don't use 200 for errors.",
     "knowledge", ["api", "http"]),

    # Testing
    ("Test behavior, not implementation",
     "Test what the code does, not how it does it. If you refactor internals, tests should still pass. Tests that break on every refactor are testing the wrong thing.",
     "lesson", ["testing"]),

    ("Each test should test one thing",
     "A test named 'test_user_creation' should only test user creation. If it also checks email sending, profile creation, and logging, split it up. When a test fails, the name should tell you what broke.",
     "convention", ["testing"]),

    ("Use factories, not fixtures for test data",
     "Shared fixtures create hidden dependencies between tests. Factory functions that create fresh data per test are more explicit and prevent test-order-dependent failures.",
     "lesson", ["testing"]),

    # Database
    ("Always use migrations, never alter tables directly",
     "Schema changes go through migration files (Alembic, Flyway, etc.). Direct ALTER TABLE commands are untrackable, unreproducible, and will cause deployment failures.",
     "convention", ["database", "deployment"]),

    ("Index columns you query by",
     "If you WHERE, JOIN, or ORDER BY a column frequently, it should have an index. But don't index everything — each index slows writes. Check EXPLAIN before adding indexes.",
     "lesson", ["database", "performance"]),

    # Dependency management
    ("Pin dependency versions in production",
     "Use exact versions (==3.2.1) or tight ranges (>=3.2,<3.3) in production. Unpinned deps (>=3.0) can break your app when a new version releases with breaking changes.",
     "lesson", ["dependencies", "deployment"]),

    ("Audit dependencies before adding them",
     "Check: Is it maintained? How many downloads? Any open security issues? When was last release? A dependency with 10 stars and no updates in 2 years is a liability, not a shortcut.",
     "procedure", ["dependencies", "security"]),

    # Documentation
    ("Document why, not what",
     "Code shows what it does. Comments should explain why it does it that way. 'Sort by date descending' is useless — the code says that. 'Sort by date descending because the billing API requires chronological order' is useful.",
     "convention", ["documentation", "code-quality"]),

    # Deployment
    ("Make deployments boring",
     "Deployments should be one command, repeatable, reversible. If deploying requires a checklist of manual steps, automate it. If it requires courage, the process is broken.",
     "lesson", ["deployment", "devops"]),

    ("Monitor before you need to",
     "Set up logging, error tracking, and basic metrics before the first production incident. You can't debug what you can't observe. Structured logs (JSON) are searchable; print statements are not.",
     "lesson", ["monitoring", "devops"]),

    # Async / concurrency
    ("Async doesn't mean faster",
     "Async helps when waiting for I/O (network, disk, database). It doesn't help CPU-bound work. If your code is crunching numbers, async adds overhead with no benefit. Use threads or multiprocessing for CPU work.",
     "knowledge", ["async", "performance"]),

    # Security
    ("Never trust client input",
     "Everything from the client — form data, headers, cookies, URL params, file uploads — is attacker-controlled. Validate, sanitize, and escape before using it in queries, commands, or HTML.",
     "lesson", ["security"]),

    ("Use parameterized queries, never string concatenation",
     "SQL: cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,)) — safe. cursor.execute(f'SELECT * FROM users WHERE id = {user_id}') — SQL injection. Same applies to ORMs with raw queries.",
     "lesson", ["security", "database"]),

    # Architecture
    ("Start with a monolith",
     "Don't start with microservices. Start with a well-structured monolith. Extract services only when you have a clear reason (team scaling, deployment independence, language boundary). Premature microservices add complexity without benefits.",
     "lesson", ["architecture"]),

    ("Separate configuration from code",
     "Database URLs, API keys, feature flags, timeouts — these belong in environment variables or config files, not hardcoded. Different environments (dev, staging, prod) use different config, same code.",
     "convention", ["architecture", "deployment"]),
]


# ─── Language-specific packs ─────────────────────────────────────

PYTHON = [
    ("Use virtual environments always",
     "Every Python project gets its own venv. Never install packages globally. python -m venv .venv && source .venv/bin/activate. This prevents version conflicts between projects.",
     "convention", ["python", "setup"]),

    ("Use type hints",
     "def process(data: list[dict]) -> bool: — Type hints catch bugs before runtime, enable IDE autocomplete, and serve as documentation. Use from __future__ import annotations for forward references.",
     "convention", ["python", "code-quality"]),

    ("Use pathlib over os.path",
     "Path('src') / 'models' / 'user.py' is cleaner than os.path.join('src', 'models', 'user.py'). pathlib handles cross-platform paths, has .read_text(), .exists(), .glob(), and is more Pythonic.",
     "convention", ["python"]),

    ("Use f-strings for formatting",
     "f'User {name} has {count} items' — faster and more readable than .format() or % formatting. For complex expressions, assign to a variable first: total = calculate(); f'Total: {total}'",
     "convention", ["python"]),

    ("Use dataclasses for data containers",
     "@dataclass over manual __init__. Gives you __repr__, __eq__, type hints, and defaults for free. Use frozen=True for immutable data. Use field(default_factory=list) for mutable defaults.",
     "convention", ["python"]),

    ("Prefer list/dict comprehensions over loops",
     "[x.name for x in users if x.active] is faster and more readable than building a list with append in a loop. But don't nest more than 2 levels — then a regular loop is clearer.",
     "lesson", ["python", "performance"]),

    ("Use context managers for resources",
     "with open('file.txt') as f: — guarantees cleanup. Works for files, database connections, locks, temporary directories. Write your own with @contextmanager for custom resource management.",
     "convention", ["python"]),

    ("Use pytest over unittest",
     "pytest: simple assertions (assert x == y), fixtures, parametrize, better output. No need for self.assertEqual, setUp/tearDown boilerplate. Run with pytest -v --tb=short for readable output.",
     "convention", ["python", "testing"]),
]

JAVASCRIPT = [
    ("Use const by default",
     "const for everything. let only when you need to reassign. Never var — it has function scope and hoisting, which cause bugs. const doesn't mean immutable — objects/arrays can still be modified.",
     "convention", ["javascript"]),

    ("Use optional chaining and nullish coalescing",
     "user?.address?.city instead of user && user.address && user.address.city. value ?? 'default' instead of value || 'default' (|| treats 0 and '' as falsy, ?? only treats null/undefined).",
     "convention", ["javascript"]),

    ("Use async/await over .then chains",
     "const data = await fetch(url).then(r => r.json()) is readable. But always wrap in try/catch or the promise will silently fail. Unhandled promise rejections crash Node.js.",
     "convention", ["javascript", "async"]),

    ("Check bundle size before adding npm packages",
     "Run 'npx bundlephobia package-name' before npm install. A 2KB utility function wrapped in a 200KB package is not worth it. Check alternatives on bundlephobia.com.",
     "procedure", ["javascript", "dependencies"]),

    ("Use TypeScript for anything beyond scripts",
     "TypeScript catches bugs at compile time, enables refactoring, and serves as documentation. The overhead of setting it up is paid back within the first week. Use strict mode.",
     "lesson", ["javascript", "typescript"]),

    ("Use ESM over CommonJS",
     "import/export over require/module.exports. ESM is the standard, supports tree shaking, and works in browsers. Set \"type\": \"module\" in package.json. Use .mjs extension if needed.",
     "convention", ["javascript", "node"]),
]

REACT = [
    ("Components should do one thing",
     "If a component fetches data, processes it, and renders a complex UI, split it. Container component for logic, presentational component for UI. Or use custom hooks for data logic.",
     "convention", ["react", "architecture"]),

    ("Use custom hooks for reusable logic",
     "useAuth(), useDebounce(), useFetch() — extract stateful logic into hooks. Components become simpler. Hooks are testable independently. Name them use* to follow the convention.",
     "convention", ["react"]),

    ("Avoid prop drilling with context or state management",
     "If you're passing props through 3+ levels, use React Context or a state library (Zustand, Jotai). But don't overuse global state — local state is simpler and faster for component-specific data.",
     "lesson", ["react", "architecture"]),

    ("Keys must be stable and unique",
     "Never use array index as key in lists that can reorder. Use a stable ID from the data. Bad keys cause subtle bugs — wrong items update, animations break, form inputs lose values.",
     "lesson", ["react"]),

    ("Memoize expensive computations, not everything",
     "useMemo and useCallback have overhead. Only use them for genuinely expensive calculations or to prevent child re-renders with React.memo. Premature memoization makes code harder to read.",
     "lesson", ["react", "performance"]),
]

RUST = [
    ("Use Result and Option, not unwrap",
     "unwrap() panics on error — fine for prototyping, not for production. Use ? operator to propagate errors. Match on Result/Option for explicit handling. Use anyhow for application errors, thiserror for libraries.",
     "convention", ["rust", "error-handling"]),

    ("Prefer iterators over indexing",
     "items.iter().filter(|x| x.active).map(|x| x.name).collect::<Vec<_>>() is idiomatic and often faster than manual loops with indexing. The compiler optimizes iterator chains aggressively.",
     "convention", ["rust", "performance"]),

    ("Clone is not evil, premature optimization is",
     "Start with clone(), make it correct, then optimize with references and lifetimes where profiling shows it matters. Fighting the borrow checker for zero-cost abstractions on day one kills productivity.",
     "lesson", ["rust"]),

    ("Use cargo clippy and cargo fmt",
     "Run cargo clippy before committing — it catches common mistakes, anti-patterns, and performance issues. Run cargo fmt for consistent formatting. Both are zero-config and fast.",
     "convention", ["rust", "tooling"]),
]

GO = [
    ("Handle errors immediately",
     "if err != nil { return fmt.Errorf('doing X: %w', err) } — wrap errors with context. Never ignore errors with _ = doSomething(). Use %w to wrap so callers can inspect with errors.Is/As.",
     "convention", ["go", "error-handling"]),

    ("Accept interfaces, return structs",
     "Functions should accept interface parameters (io.Reader, not *os.File) and return concrete types. This makes code flexible for callers and concrete for users.",
     "convention", ["go", "architecture"]),

    ("Use table-driven tests",
     "tests := []struct{ name string; input int; want int }{{...}, {...}}; for _, tt := range tests { t.Run(tt.name, func(t *testing.T) {...}) } — clean, extensible, easy to add cases.",
     "convention", ["go", "testing"]),

    ("Use context for cancellation and timeouts",
     "Pass context.Context as the first parameter. Use context.WithTimeout for network calls. Check ctx.Done() in long loops. Never store context in structs.",
     "convention", ["go", "async"]),
]

# ─── Pack registry ───────────────────────────────────────────────

PACKS = {
    "universal": UNIVERSAL,
    "python": PYTHON,
    "javascript": JAVASCRIPT,
    "react": REACT,
    "rust": RUST,
    "go": GO,
}

# Map file indicators to packs
PACK_DETECTORS = {
    "pyproject.toml": ["python"],
    "requirements.txt": ["python"],
    "setup.py": ["python"],
    "Pipfile": ["python"],
    "package.json": ["javascript"],
    "tsconfig.json": ["javascript"],
    "next.config.js": ["javascript", "react"],
    "next.config.ts": ["javascript", "react"],
    "vite.config.ts": ["javascript", "react"],
    "Cargo.toml": ["rust"],
    "go.mod": ["go"],
}


def detect_packs(project_dir: str) -> list[str]:
    """Detect which starter packs apply based on project files."""
    detected = {"universal"}  # always include

    for filename, packs in PACK_DETECTORS.items():
        if os.path.exists(os.path.join(project_dir, filename)):
            detected.update(packs)

    # Check for React in package.json dependencies
    pkg_json = os.path.join(project_dir, "package.json")
    if os.path.exists(pkg_json):
        try:
            data = json.loads(open(pkg_json).read())
            all_deps = list(data.get("dependencies", {}).keys()) + list(data.get("devDependencies", {}).keys())
            if any("react" in d for d in all_deps):
                detected.add("react")
        except (OSError, json.JSONDecodeError):
            pass

    return sorted(detected)


def get_pack_memories(pack_names: list[str]) -> list[tuple[str, str, str, list[str]]]:
    """Get all memories from the requested packs. Returns list of (title, content, type, tags)."""
    memories = []
    seen_titles = set()

    for name in pack_names:
        pack = PACKS.get(name, [])
        for title, content, mem_type, tags in pack:
            if title not in seen_titles:
                memories.append((title, content, mem_type, tags))
                seen_titles.add(title)

    return memories
