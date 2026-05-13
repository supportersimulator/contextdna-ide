#!/usr/bin/env python3
"""Markdown-aware chunk splitter for Discord replies.

Reads raw response text from stdin, emits NUL-separated chunks on stdout
that each fit within a configurable byte budget (default 1700) while
preserving:

  - Triple-backtick fence integrity: if a chunk would end inside a fenced
    code block, close the fence with ``` and reopen it on the next chunk
    with the same language tag.
  - Paragraph boundaries: prefer to split on the last \\n\\n within the
    final 300 chars of the budget so headings/lists stay intact.
  - Inline markdown links: avoid cutting inside ``[text](url)`` by
    deferring the split to before the unmatched ``[``.

Usage::

    printf '%s' "$reply" | python3 scripts/discord-split.py 1700 > chunks.bin

Chunks are separated by a single NUL byte (``\\0``) so callers can
use ``read -d ''`` safely — NUL is illegal in markdown content.
"""
from __future__ import annotations

import re
import sys
from typing import List, Tuple

FENCE_RE = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$", re.MULTILINE)
# Tail-window size for preferred paragraph breaks (chars from the end).
PARAGRAPH_LOOKBACK = 300


def _fence_state_at(text: str) -> Tuple[bool, str]:
    """Return (inside_fence, lang) after processing `text`.

    Scans lines for ```-opened fences; each bare ``` toggles off.
    """
    inside = False
    lang = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if inside:
                inside = False
                lang = ""
            else:
                inside = True
                lang = stripped[3:].strip()
    return inside, lang


def _last_unmatched_bracket(text: str) -> int:
    """Return index of a trailing ``[`` that has no matching ``)``.

    We walk backwards looking for the last ``[`` whose matching ``]``
    and ``(...)`` close AFTER the supplied text ends. If such a bracket
    exists, callers should split BEFORE it so the link stays whole.
    Returns -1 if no unmatched bracket found.
    """
    # Fast exit: no `[` at all.
    if "[" not in text:
        return -1
    depth_brack = 0
    depth_paren = 0
    # Scan right-to-left tracking balanced [...](...)
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch == ")":
            depth_paren += 1
        elif ch == "(":
            depth_paren -= 1
        elif ch == "]":
            depth_brack += 1
        elif ch == "[":
            if depth_brack <= 0:
                # Unmatched — check if this looks like start of a link.
                # Heuristic: an unmatched [ means link is still opening.
                return i
            depth_brack -= 1
    return -1


def _choose_split(text: str, budget: int) -> int:
    """Pick the best split index within [0, budget] for `text`.

    Preference order:
      1. Last ``\\n\\n`` in the final PARAGRAPH_LOOKBACK chars.
      2. Last ``\\n`` in the final PARAGRAPH_LOOKBACK chars.
      3. Before an unmatched ``[`` (avoid breaking a link).
      4. Hard cut at `budget`.
    """
    if len(text) <= budget:
        return len(text)

    window_start = max(0, budget - PARAGRAPH_LOOKBACK)
    window = text[window_start:budget]

    # Prefer paragraph break.
    para = window.rfind("\n\n")
    if para != -1:
        return window_start + para + 2  # include the blank line separator

    # Fall back to single newline.
    nl = window.rfind("\n")
    if nl != -1:
        return window_start + nl + 1

    # Avoid splitting inside a markdown link.
    head = text[:budget]
    bad_bracket = _last_unmatched_bracket(head)
    if bad_bracket > 0 and bad_bracket > budget - PARAGRAPH_LOOKBACK:
        return bad_bracket

    return budget


def split(text: str, budget: int) -> List[str]:
    """Split `text` into markdown-safe chunks each <= `budget` chars.

    If a chunk ends inside an open fence, close it with ``\\n``` and
    prepend ```<lang>`` to the next chunk so syntax highlighting
    resumes on the continuation message.
    """
    if budget <= 0:
        raise ValueError("budget must be positive")

    chunks: List[str] = []
    remainder = text
    carry_fence_lang: str | None = None  # set if previous chunk left a fence open
    # Worst-case suffix we may append to close an open fence: "\n```".
    FENCE_CLOSE_RESERVE = 4

    while remainder:
        # Account for the reopen header if we're mid-fence.
        reopen = ""
        if carry_fence_lang is not None:
            reopen = "```" + carry_fence_lang + "\n"

        # Reserve room for a potential fence-close suffix so the emitted
        # chunk (reopen + body + close) stays <= budget.
        effective_budget = budget - len(reopen) - FENCE_CLOSE_RESERVE
        if effective_budget <= 0:
            # Pathological: budget smaller than reopen+close. Fall back to
            # whatever room remains after the reopen header.
            effective_budget = max(1, budget - len(reopen))

        if len(remainder) <= effective_budget:
            split_at = len(remainder)
        else:
            split_at = _choose_split(remainder, effective_budget)
            if split_at <= 0:
                split_at = effective_budget

        body = remainder[:split_at]
        chunk = reopen + body

        # Determine fence state at end of chunk.
        inside, lang = _fence_state_at(chunk)
        if inside and split_at < len(remainder):
            # Close the open fence so Discord doesn't render ``` as literal text.
            # Ensure a newline before the closing fence.
            if not chunk.endswith("\n"):
                chunk += "\n"
            chunk += "```"
            carry_fence_lang = lang
        else:
            carry_fence_lang = None

        chunks.append(chunk)
        remainder = remainder[split_at:]

    return chunks


def main() -> int:
    budget = 1700
    if len(sys.argv) > 1:
        try:
            budget = int(sys.argv[1])
        except ValueError:
            print(f"invalid budget: {sys.argv[1]}", file=sys.stderr)
            return 2

    raw = sys.stdin.read()
    chunks = split(raw, budget)
    # Emit NUL-TERMINATED (not just separated) so bash `read -r -d ''`
    # sees a delimiter after every chunk including the last one. Without
    # a trailing NUL, `read -d ''` returns non-zero on the tail and the
    # final chunk is silently dropped under `set -e`.
    for c in chunks:
        sys.stdout.write(c)
        sys.stdout.write("\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
