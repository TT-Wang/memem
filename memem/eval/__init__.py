# Re-export legacy scorecard so `from memem.eval import run_eval` still works.
from memem.eval.legacy_scorecard import run_eval as run_eval

__all__ = ["run_eval"]
