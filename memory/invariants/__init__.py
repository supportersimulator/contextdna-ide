"""Storage invariants package — runtime read-after-write evidence checks."""

from .storage_invariants import (  # noqa: F401
    InvariantViolation,
    apply_invariants_to,
    describe,
    get_counters,
    invariant_check,
    reset_counters,
)
