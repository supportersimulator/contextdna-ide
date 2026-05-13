"""Chain engine — segment registration, state, and execution.

Matches macbook1's orchestration layer design (63ca4eab).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from memory.chain_requirements import CommandRequirements, RuntimeContext, check_requirements, GateResult


@dataclass
class ChainSegment:
    """Atomic unit of a chain — declares requirements, executes work."""
    name: str
    fn: Callable[[Any, dict], dict]
    requires: CommandRequirements
    tags: list[str] = field(default_factory=list)
    learned_deps: set[str] = field(default_factory=set)
    learned_synergies: set[str] = field(default_factory=set)


SEGMENT_REGISTRY: dict[str, ChainSegment] = {}


def clear_registry() -> None:
    """Clear all registered segments (for testing)."""
    SEGMENT_REGISTRY.clear()


def segment(
    name: str,
    requires: CommandRequirements,
    tags: list[str] | None = None,
) -> Callable:
    """Decorator to register a function as a chain segment."""
    def decorator(fn: Callable) -> Callable:
        if name in SEGMENT_REGISTRY:
            raise ValueError(f"Segment '{name}' already registered")
        seg = ChainSegment(
            name=name,
            fn=fn,
            requires=requires,
            tags=tags or [],
        )
        SEGMENT_REGISTRY[name] = seg
        return fn
    return decorator


@dataclass
class ChainState:
    """Accumulator passed through chain execution."""
    data: dict = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    degraded: list[tuple[str, list[str]]] = field(default_factory=list)
    segment_results: dict[str, dict] = field(default_factory=dict)
    segment_times_ns: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""


class ChainExecutor:
    """Runs ordered segment lists with gate-aware execution."""

    def __init__(self, halt_on_error: bool = True):
        self.halt_on_error = halt_on_error

    def run(
        self,
        segment_names: list[str],
        ctx: RuntimeContext,
        initial_data: dict | None = None,
    ) -> ChainState:
        state = ChainState(data=dict(initial_data or {}))

        for name in segment_names:
            if state.halted:
                break

            seg = SEGMENT_REGISTRY.get(name)
            if seg is None:
                state.skipped.append((name, f"Segment '{name}' not registered"))
                continue

            # Gate check
            result = check_requirements(seg.requires, ctx)
            if result.gate == GateResult.BLOCKED:
                state.skipped.append((name, result.reason))
                continue
            if result.gate == GateResult.DEGRADED:
                state.degraded.append((name, result.notes))

            # Execute
            t0 = time.perf_counter_ns()
            try:
                seg_result = seg.fn(ctx, state.data)
                if isinstance(seg_result, dict):
                    state.segment_results[name] = seg_result
                    state.data.update(seg_result)
            except Exception as e:
                err_msg = f"{name}: {e}"
                state.errors.append(err_msg)
                if self.halt_on_error:
                    state.halted = True
                    state.halt_reason = err_msg
            finally:
                state.segment_times_ns[name] = time.perf_counter_ns() - t0

        return state
