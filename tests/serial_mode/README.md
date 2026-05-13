# `migrate4/test_mode` — `SERIAL_MODE=1` operator guide

A single environment variable, `SERIAL_MODE=1`, gives operators a
deterministic, single-threaded execution path through code that
normally runs through async queues and semaphore-throttled background
workers.

## When to use

| Scenario | Why `SERIAL_MODE=1` helps |
| --- | --- |
| **CI smoke tests** | No async timing noise, no flaky scheduler races, no LLM-network dependency. |
| **`scripts/bootstrap-verify.sh`** | Verifies module wiring without spinning up the LLM priority queue / NATS / Redis. |
| **Debugger sessions** | Step-through works because there are no background threads or worker loops to break out of. |
| **Reproducible bug reports** | Same input → same output. The `_serial_llm_response` stub is keyed on a SHA-256 of the prompt, so a captured prompt yields a captured response forever. |
| **Local laptop work without API keys** | The deterministic stub returns instantly; no DeepSeek / OpenAI / MLX call is attempted. |

## When NOT to use

| Scenario | Why to leave it unset |
| --- | --- |
| **Production runtime** | The whole point is to bypass the real LLM. A production deploy with `SERIAL_MODE=1` returns gibberish to users. |
| **Performance benchmarks** | Serial execution masks queue contention, GPU lock waits, and external-API latency — exactly the things benchmarks need to surface. |
| **Concurrency / race-condition testing** | `@serial_safe` strips out the very async behavior these tests are designed to stress. |
| **Integration tests against a real model** | Stub responses will fail any test that asserts on actual LLM content (medical accuracy, JSON schema, tool calls, etc.). |

## How tests opt in

```bash
# Run the whole pytest suite in serial mode
SERIAL_MODE=1 pytest tests/

# Run bootstrap verification without queues
SERIAL_MODE=1 bash scripts/bootstrap-verify.sh

# One-off Python repro of a captured bug
SERIAL_MODE=1 python -m my_module.repro
```

Inside Python:

```python
from migrate4.test_mode import is_serial_mode, get_serial_counters

if is_serial_mode():
    print("running deterministic; counters:", get_serial_counters())
```

## Decorators

```python
from migrate4.test_mode import serial_safe, bypass_queue_in_serial

@serial_safe
async def fetch_context(node_id: str) -> dict:
    ...

@bypass_queue_in_serial
def call_llm(user_prompt: str, **kwargs) -> str:
    return llm_generate(user_prompt=user_prompt, **kwargs)
```

Call sites are identical in both modes:

```python
result = await fetch_context("mac1")          # awaitable either way
text   = call_llm(user_prompt="hello world")  # sync call either way
```

When `SERIAL_MODE=1`:

* `fetch_context` runs synchronously inside the wrapper and the awaited
  result is the same value the coroutine would have produced.
* `call_llm` never reaches `llm_generate`; it returns a deterministic
  stub of the form `[SERIAL_MODE_STUB:<hex16>] echo(<len>)`.

## Backpressure interaction (Agent 4)

`anticipation_backpressure.py` (Agent 4's work) throttles anticipation
loops with `asyncio.Semaphore` objects.  Those semaphores are expected
to short-circuit to `contextlib.nullcontext()` when `is_serial_mode()`
is `True`, because there is nothing to throttle when every call runs
inline.  See the inline note at the top of `serial_mode.py`.

## ZSF counters

Two counters are exposed via `get_serial_counters()`:

| Counter | Meaning |
| --- | --- |
| `serial_mode_calls_total` | Number of times a decorated wrapper observed `SERIAL_MODE=1` and took the serial branch. |
| `serial_mode_fallbacks_total` | Number of times the serial branch itself raised and we fell back to the real async/queue path. Should be **0** in a healthy CI run. |

A non-zero `serial_mode_fallbacks_total` in CI is the first signal that
a wrapper is mis-applied (e.g. decorating a non-coroutine, or a queue
caller whose signature lacks `user_prompt`).

## Transition from test to production — addressing the Synaptic concern

Synaptic raised this in the original design review:

> "There is no transition from test mode to production mode."

The transition is intentionally a **no-op**: `SERIAL_MODE` is read at
every decorator entry from `os.environ`.  Nothing is cached, nothing is
monkey-patched, no module is mutated.  Removing the variable (or
setting it to anything other than `"1"`) restores normal behavior on
the very next call.

Production guard pattern:

```python
# top of main.py / wsgi entrypoint
import os, sys
if os.environ.get("SERIAL_MODE", "0") == "1":
    sys.exit("FATAL: SERIAL_MODE is for tests only — refusing to start.")
```

This guard is cheap, explicit, and makes a misconfigured deploy
fail-loud at startup rather than fail-silent at runtime.

## "Tested but not production-tested" blind spot

A reasonable 3-surgeon objection: code paths that *only* run when
`SERIAL_MODE=1` are not exercised by production traffic and can rot.
Mitigations:

1. **Decorator wrappers are thin.** All real logic lives in the wrapped
   function; the decorator only chooses which branch to call.
2. **CI runs the test suite in both modes.**  The matrix should
   include one job with `SERIAL_MODE=0` (default) and one with
   `SERIAL_MODE=1`, so behavior parity is enforced.
3. **The stub is distinguishable.**  `_serial_llm_response` returns a
   string starting with `[SERIAL_MODE_STUB:` — any log scrape in
   production that sees this prefix is a smoke alarm.
4. **Counters are exposed.**  `serial_mode_calls_total > 0` in
   production telemetry is itself a critical finding.
