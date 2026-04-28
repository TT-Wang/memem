"""Miner error taxonomy.

Two classes with explicit semantics:

- TransientError: known to be transient (will likely succeed on retry).
  Examples: TRANSIENT_EXIT_CODE from the server subprocess; network blip.
  Daemon retries, subject to per-session attempt cap.

- PermanentError: anything else, including unclassified errors.
  Examples: auth failure, hung subprocess, malformed input, unknown
  non-zero exit code.
  Daemon stops (SystemExit FATAL_EXIT_CODE) so the wrapper does not
  restart it. User must intervene.

The DEFAULT classification is PermanentError. This is the single most
important invariant in the miner: an unknown error class can never
trigger a retry loop, by construction. v0.11.1 fixed a specific case
of misclassified-as-transient (auth failures); this taxonomy prevents
the entire bug class.
"""


class TransientError(RuntimeError):
    """Subprocess result is known to be transient — retry is safe."""


class PermanentError(RuntimeError):
    """Subprocess result is permanent (default) — stop the daemon."""
