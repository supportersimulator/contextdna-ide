#!/usr/bin/env python3
"""
Surgery Team of 3 — Multi-Model Introspection & Collaboration Tool

Usage:
    ./scripts/surgery-team.py probe              # Quick health check all 3 surgeons
    ./scripts/surgery-team.py ask-local "prompt"  # Query Qwen3-4B directly
    ./scripts/surgery-team.py ask-remote "prompt" # Query GPT-4.1 directly
    ./scripts/surgery-team.py introspect          # All models self-report capabilities
    ./scripts/surgery-team.py consult "topic"     # Structured multi-model consultation
    ./scripts/surgery-team.py cross-exam "topic"  # Full cross-exam + open exploration (unknown unknowns)
    ./scripts/surgery-team.py consensus "claim"   # Confidence-weighted consensus
    ./scripts/surgery-team.py research "topic"    # GPT-4.1 self-directed doc research ($5/day)
    ./scripts/surgery-team.py research-status     # Research budget & recent sessions
    ./scripts/surgery-team.py evidence "topic"    # Cross-examine docs against evidence store
    ./scripts/surgery-team.py ab-propose "claim"  # Design A/B test for a claim
    ./scripts/surgery-team.py ab-status           # A/B test dashboard
    ./scripts/surgery-team.py cardio-reverify "scope"  # Re-verify evidence grades (working-days aware)
    ./scripts/surgery-team.py status              # Hybrid routing status + costs

Created: 2026-02-18 — Surgery Team architecture
Updated: 2026-02-22 — Evidence cross-examination + A/B testing (Phase 8: Cardiologist Evidence Analyst)
Updated: 2026-03-04 — Open Exploration phase added to cross-exam (corrigibility: surface unknown unknowns)
Updated: 2026-03-04 — Cardiologist Evidence Re-Verification skill (working-days, criticals→Redis, A/B triggers)
"""

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path


# --- LLM provider resolution (DeepSeek cutover; default: anthropic/openai path) ---

def _resolve_llm_provider(model_override: str = "") -> dict:
    """Resolve current LLM provider from LLM_PROVIDER env.

    Returns {provider, base_url, model, api_key}. Default = OpenAI (current behavior).
    Set LLM_PROVIDER=deepseek to flip to DeepSeek (OpenAI-compatible endpoint).
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    if provider == "deepseek":
        api_key = (
            os.environ.get("Context_DNA_Deep_Seek")
            or os.environ.get("Context_DNA_Deepseek")
            or os.environ.get("DEEPSEEK_API_KEY", "")
        )
        if not api_key:
            try:
                r = subprocess.run(
                    ["security", "find-generic-password", "-s", "fleet-nerve",
                     "-a", "Context_DNA_Deep_Seek", "-w"],
                    capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0:
                    api_key = r.stdout.strip()
            except Exception:
                api_key = ""
        return {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model": model_override or "deepseek-chat",
            "api_key": api_key,
        }
    return {
        "provider": "openai",
        "base_url": None,
        "model": model_override or "gpt-4.1-mini",
        "api_key": os.environ.get("Context_DNA_OPENAI", ""),
    }

# Ensure repo root on path
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# Load .env for OpenAI key
ENV_PATH = REPO_ROOT / "context-dna" / ".env"
if ENV_PATH.exists():
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                # Override — .env is authoritative for API keys
                if v:
                    os.environ[k] = v

# Colors
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"


def ok(msg): print(f"  {GREEN}✓{NC} {msg}")
def fail(msg): print(f"  {RED}✗{NC} {msg}")
def warn(msg): print(f"  {YELLOW}⚠{NC} {msg}")
def info(msg): print(f"  {CYAN}→{NC} {msg}")
def header(msg): print(f"\n{BOLD}{msg}{NC}")
def dim(msg): print(f"  {DIM}{msg}{NC}")


# ─────────────────────────────────────────────────────────────────────────────
# MODEL CLIENTS
# ─────────────────────────────────────────────────────────────────────────────

def query_local(system: str, prompt: str, profile: str = "deep", max_chars: int = 12000, timeout_s: float = 90.0) -> dict:
    """Query Qwen3-4B via priority queue. Returns {ok, content, latency_ms, tokens}.
    max_chars: safety cap on output. Default 12000 (~3K tokens) — enough for deep profile (2048 tok).
    """
    try:
        from memory.llm_priority_queue import llm_generate, Priority
        t0 = time.time()
        result = llm_generate(system, prompt, Priority.ATLAS, profile, "surgery_team", timeout_s=timeout_s)
        latency = int((time.time() - t0) * 1000)
        if result:
            return {"ok": True, "content": result[:max_chars], "latency_ms": latency, "model": "Qwen3-4B-4bit"}
        return {"ok": False, "content": "No response", "latency_ms": latency, "model": "Qwen3-4B-4bit"}
    except Exception as e:
        return {"ok": False, "content": str(e), "latency_ms": 0, "model": "Qwen3-4B-4bit"}


def query_remote(system: str, prompt: str, model: str = "", max_tokens: int = 2048, timeout_s: float = 300.0) -> dict:
    """Query cardiologist via OpenAI-compatible API (OpenAI or DeepSeek). Returns {ok, content, latency_ms, model, cost_usd}.

    Provider selected via LLM_PROVIDER env (openai|deepseek). Default: openai.
    If model="" we use the provider's default ("gpt-4.1-mini" for openai, "deepseek-chat" for deepseek).
    """
    try:
        from openai import OpenAI
        cfg = _resolve_llm_provider(model_override=model)
        key = cfg["api_key"]
        effective_model = cfg["model"]
        if not key or len(key) < 20:
            return {"ok": False, "content": f"{cfg['provider']} API key not set", "latency_ms": 0, "model": effective_model}
        client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=timeout_s)
        t0 = time.time()
        resp = client.chat.completions.create(
            model=effective_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        latency = int((time.time() - t0) * 1000)
        content = resp.choices[0].message.content or ""
        usage = resp.usage
        cost = 0.0
        if usage:
            # Pricing per model (USD per 1M tokens: in, out)
            pricing = {
                "gpt-4.1": (2.00, 8.00),
                "gpt-4.1-mini": (0.40, 1.60),
                "gpt-4.1-nano": (0.10, 0.40),
                "o3": (2.00, 8.00),
                "o3-pro": (20.00, 80.00),
                "o4-mini": (1.10, 4.40),
                "deepseek-chat": (0.28, 1.10),
                "deepseek-reasoner": (0.55, 2.19),
            }
            in_rate, out_rate = pricing.get(effective_model, (2.00, 8.00))
            cost = (usage.prompt_tokens * in_rate + usage.completion_tokens * out_rate) / 1_000_000
        return {"ok": True, "content": content, "latency_ms": latency, "model": effective_model,
                "cost_usd": cost, "tokens_in": usage.prompt_tokens if usage else 0,
                "tokens_out": usage.completion_tokens if usage else 0}
    except Exception as e:
        return {"ok": False, "content": str(e), "latency_ms": 0, "model": model or "cardiologist"}


def query_remote_multi(system: str, messages: list, model: str = "", max_tokens: int = 4096, timeout_s: float = 300.0) -> dict:
    """Multi-turn cardiologist conversation. messages = list of {role, content} dicts.

    Provider selected via LLM_PROVIDER env (openai|deepseek). Default: openai.
    If model="" we use the provider's default ("gpt-4.1" for openai, "deepseek-chat" for deepseek).
    """
    try:
        from openai import OpenAI
        # Default model for multi-turn is gpt-4.1 on openai (richer reasoning); deepseek-chat otherwise.
        default_for_multi = "gpt-4.1" if os.environ.get("LLM_PROVIDER", "anthropic") != "deepseek" else "deepseek-chat"
        cfg = _resolve_llm_provider(model_override=model or default_for_multi)
        key = cfg["api_key"]
        effective_model = cfg["model"]
        if not key or len(key) < 20:
            return {"ok": False, "content": f"{cfg['provider']} API key not set", "latency_ms": 0, "model": effective_model}
        client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=timeout_s)
        full_messages = [{"role": "system", "content": system}] + messages
        t0 = time.time()
        resp = client.chat.completions.create(
            model=effective_model,
            messages=full_messages,
            max_tokens=max_tokens,
            temperature=0.7,
        )
        latency = int((time.time() - t0) * 1000)
        content = resp.choices[0].message.content or ""
        usage = resp.usage
        cost = 0.0
        if usage:
            pricing = {
                "gpt-4.1": (2.00, 8.00),
                "gpt-4.1-mini": (0.40, 1.60),
                "gpt-4.1-nano": (0.10, 0.40),
                "o3": (2.00, 8.00),
                "o3-pro": (20.00, 80.00),
                "o4-mini": (1.10, 4.40),
                "deepseek-chat": (0.28, 1.10),
                "deepseek-reasoner": (0.55, 2.19),
            }
            in_rate, out_rate = pricing.get(effective_model, (2.00, 8.00))
            cost = (usage.prompt_tokens * in_rate + usage.completion_tokens * out_rate) / 1_000_000
        return {"ok": True, "content": content, "latency_ms": latency, "model": effective_model,
                "cost_usd": cost, "tokens_in": usage.prompt_tokens if usage else 0,
                "tokens_out": usage.completion_tokens if usage else 0}
    except Exception as e:
        return {"ok": False, "content": str(e), "latency_ms": 0, "model": model or "cardiologist"}


# ─────────────────────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

def cmd_probe():
    """Quick health check all 3 surgeons."""
    header("=== Surgery Team Probe ===")

    # 1. Local (Qwen3-4B)
    header("Surgeon 1: Qwen3-4B-4bit (Local/Neurologist)")
    r = query_local("You are a test probe.", "Say 'operational' in one word.", profile="classify")
    if r["ok"]:
        ok(f"Responding — {r['latency_ms']}ms — \"{r['content'][:80]}\"")
    else:
        fail(f"Down — {r['content'][:80]}")

    # 2. Remote (GPT-4.1)
    header("Surgeon 2: GPT-4.1-mini (Remote/Cardiologist)")
    r = query_remote("You are a test probe.", "Say 'operational' in one word.", max_tokens=32)
    if r["ok"]:
        ok(f"Responding — {r['latency_ms']}ms — \"{r['content'][:80]}\" (${r.get('cost_usd', 0):.6f})")
    else:
        fail(f"Down — {r['content'][:80]}")

    # 3. Atlas (always available — we ARE Atlas)
    header("Surgeon 3: Atlas/Claude Opus (Head Surgeon)")
    ok("Present (running this tool)")

    # 4. Infrastructure
    header("Infrastructure")
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        rc.ping()
        ok(f"Redis :6379 — {rc.dbsize()} keys")
    except Exception:
        fail("Redis :6379 unreachable")

    import requests
    for port, name in [(5044, "LLM Server"), (8080, "agent_service"), (8888, "Synaptic"), (8029, "ContextDNA API")]:
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            ok(f"{name} :{port}")
        except Exception:
            fail(f"{name} :{port}")


def cmd_ask_local(prompt: str):
    """Query Qwen3-4B with a prompt."""
    header("=== Qwen3-4B Response ===")
    system = "You are Qwen3-4B-4bit, a local LLM. Be concise and honest. If you don't know, say so."
    r = query_local(system, prompt, profile="deep")
    if r["ok"]:
        info(f"Latency: {r['latency_ms']}ms")
        print(f"\n{r['content']}")
    else:
        fail(r["content"])


def cmd_ask_remote(prompt: str):
    """Query GPT-4.1 with a prompt."""
    header("=== GPT-4.1-mini Response ===")
    system = "You are GPT-4.1-mini. Be concise and honest. If you don't know, say so."
    r = query_remote(system, prompt)
    if r["ok"]:
        info(f"Latency: {r['latency_ms']}ms | Cost: ${r.get('cost_usd', 0):.6f}")
        print(f"\n{r['content']}")
    else:
        fail(r["content"])


def cmd_introspect():
    """All models self-report capabilities."""
    header("=== Surgery Team Introspection ===")

    introspection_prompt = (
        "INTROSPECTION: Report your capabilities honestly.\n"
        "1. What do you know about the ContextDNA system?\n"
        "2. What context do you typically receive?\n"
        "3. Your key limitations?\n"
        "4. What would help you perform better?\n"
        "Be brutally honest. Say 'I don't know' when appropriate."
    )

    system_local = (
        "You are Qwen3-4B-4bit running locally on mlx_lm.server. "
        "Introspect honestly on your own capabilities."
    )
    system_remote = (
        "You are GPT-4.1-mini, an OpenAI model used as fallback in a hybrid LLM system. "
        "Introspect honestly on your own capabilities."
    )

    header("Surgeon 1: Qwen3-4B-4bit")
    r1 = query_local(system_local, introspection_prompt)
    if r1["ok"]:
        info(f"{r1['latency_ms']}ms")
        print(r1["content"])
    else:
        fail(r1["content"])

    header("Surgeon 2: GPT-4.1-mini")
    r2 = query_remote(system_remote, introspection_prompt)
    if r2["ok"]:
        info(f"{r2['latency_ms']}ms | ${r2.get('cost_usd', 0):.6f}")
        print(r2["content"])
    else:
        fail(r2["content"])


def cmd_consult(topic: str):
    """Structured multi-model consultation on a topic."""
    header(f"=== Surgery Consultation: {topic[:60]} ===")

    consult_system = (
        "You are part of a 3-model surgery team analyzing a technical topic. "
        "Provide your analysis as structured JSON with these fields:\n"
        '{"claims": [{"claim": "...", "evidence": "...", "confidence": 0.0-1.0}], '
        '"unknowns": ["..."], "recommendations": ["..."]}\n'
        "Be honest about confidence. Use 0.3 or below for guesses. "
        "List unknowns explicitly."
    )

    consult_prompt = f"Analyze this topic for the surgery team:\n\n{topic}"

    # Query both in sequence (Qwen holds GPU lock)
    header("Qwen3-4B Analysis")
    r1 = query_local(consult_system, consult_prompt, profile="extract_deep")
    if r1["ok"]:
        info(f"{r1['latency_ms']}ms")
        print(r1["content"])
    else:
        fail(r1["content"])

    header("GPT-4.1-mini Analysis")
    r2 = query_remote(consult_system, consult_prompt)
    if r2["ok"]:
        info(f"{r2['latency_ms']}ms | ${r2.get('cost_usd', 0):.6f}")
        print(r2["content"])
    else:
        fail(r2["content"])

    header("Atlas Note")
    info("Atlas (Claude Opus) synthesizes both analyses in conversation context.")
    info("Run this from within a Claude session for full synthesis.")


def cmd_cross_exam(topic: str):
    """Full cross-examination: initial analysis → cross-review → open exploration (unknown unknowns)."""
    header(f"=== Cross-Examination: {topic[:60]} ===")

    base_system = (
        "You are part of a 3-model surgery team. Be analytical and critical. "
        "Identify confabulations, unsupported claims, and blind spots."
    )

    # Step 1: Initial reports
    header("Step 1: Initial Reports")

    initial_prompt = f"Provide your analysis of: {topic}\n\nBe specific. Cite evidence. Admit unknowns."

    info("Querying Qwen3-4B...")
    r_local = query_local(base_system, initial_prompt, profile="deep")
    local_report = r_local["content"] if r_local["ok"] else "(Qwen3-4B unavailable)"
    if r_local["ok"]:
        ok(f"Qwen3-4B: {len(local_report)} chars, {r_local['latency_ms']}ms")
    else:
        fail(f"Qwen3-4B: {local_report}")

    info("Querying GPT-4.1-mini...")
    r_remote = query_remote(base_system, initial_prompt)
    remote_report = r_remote["content"] if r_remote["ok"] else "(GPT-4.1 unavailable)"
    if r_remote["ok"]:
        ok(f"GPT-4.1: {len(remote_report)} chars, {r_remote['latency_ms']}ms, ${r_remote.get('cost_usd', 0):.6f}")
    else:
        fail(f"GPT-4.1: {remote_report}")

    # Step 2: Cross-examination
    header("Step 2: Cross-Examination")

    cross_system = (
        "You are a critical reviewer in a multi-model surgery team. "
        "Analyze the other model's report. Identify:\n"
        "1. Confabulations (claims without evidence)\n"
        "2. Blind spots (what was missed)\n"
        "3. Agreements (what aligns with your understanding)\n"
        "4. Confidence assessment (how much to trust each claim)"
    )

    info("Qwen3-4B reviewing GPT-4.1's report...")
    cross_prompt_1 = f"Review this report from GPT-4.1-mini:\n\n{remote_report}\n\nYour critical analysis:"
    r_cross1 = query_local(cross_system, cross_prompt_1, profile="deep")
    if r_cross1["ok"]:
        header("Qwen3-4B's Review of GPT-4.1")
        print(r_cross1["content"])
    else:
        fail(f"Cross-exam failed: {r_cross1['content']}")

    info("GPT-4.1 reviewing Qwen3-4B's report...")
    cross_prompt_2 = f"Review this report from Qwen3-4B (local 4B parameter model):\n\n{local_report}\n\nYour critical analysis:"
    r_cross2 = query_remote(cross_system, cross_prompt_2)
    if r_cross2["ok"]:
        header("GPT-4.1's Review of Qwen3-4B")
        print(r_cross2["content"])
    else:
        fail(f"Cross-exam failed: {r_cross2['content']}")

    # Step 3: Open Exploration (corrigibility — surface unknown unknowns)
    header("Step 3: Open Exploration")

    # Combine available reports for context
    combined_reports = ""
    if r_local["ok"]:
        combined_reports += f"=== Qwen3-4B Report ===\n{local_report}\n\n"
    if r_remote["ok"]:
        combined_reports += f"=== GPT-4.1-mini Report ===\n{remote_report}\n\n"
    if r_cross1["ok"]:
        combined_reports += f"=== Qwen3-4B Cross-Exam ===\n{r_cross1['content']}\n\n"
    if r_cross2["ok"]:
        combined_reports += f"=== GPT-4.1-mini Cross-Exam ===\n{r_cross2['content']}\n\n"

    explore_system = (
        "You are part of a 3-model surgery team. The team has already produced "
        "initial analyses and cross-examinations (provided below). Your role now "
        "is OPEN EXPLORATION — go beyond what was already covered.\n\n"
        "Focus on:\n"
        "- What are we ALL blind to? What assumptions remain unchallenged?\n"
        "- What adjacent systems, failure modes, or interactions were not considered?\n"
        "- What would a domain expert immediately ask that we haven't?\n"
        "- Are there academic, industry, or historical precedents we're ignoring?\n"
        "- What's the worst-case scenario nobody mentioned?\n"
        "- What corrigibility risks exist — where might we be confidently wrong?\n\n"
        "Do NOT repeat points already made. Only surface genuinely NEW insights."
    )
    explore_prompt = (
        f"TOPIC: {topic}\n\n"
        f"=== TEAM ANALYSIS SO FAR ===\n{combined_reports}\n"
        "Now: What are we blind to? What haven't we considered? "
        "Surface unknown unknowns — the things we don't know we don't know."
    )

    info("Qwen3-4B exploring unknown unknowns...")
    r_explore_local = query_local(explore_system, explore_prompt, profile="deep")
    if r_explore_local["ok"]:
        header("Qwen3-4B Open Exploration")
        print(r_explore_local["content"])
    else:
        fail(f"Open exploration failed: {r_explore_local['content']}")

    info("GPT-4.1 exploring unknown unknowns...")
    r_explore_remote = query_remote(explore_system, explore_prompt)
    if r_explore_remote["ok"]:
        header("GPT-4.1 Open Exploration")
        print(r_explore_remote["content"])
    else:
        fail(f"Open exploration failed: {r_explore_remote['content']}")

    # Step 4: Summary
    header("Step 4: Results Summary")
    total_cost = sum(r.get("cost_usd", 0) for r in [r_remote, r_cross2, r_explore_remote])
    info(f"Total OpenAI cost: ${total_cost:.6f}")
    info(f"Total queries: 6 (2 initial + 2 cross-exam + 2 exploration)")
    info("Feed these results to Atlas (Claude Opus) for ground-truth synthesis.")

    # Save results to WAL
    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    results = {
        "timestamp": datetime.now().isoformat(),
        "topic": topic,
        "local_report": local_report,
        "remote_report": remote_report,
        "local_cross_exam": r_cross1["content"] if r_cross1["ok"] else None,
        "remote_cross_exam": r_cross2["content"] if r_cross2["ok"] else None,
        "local_exploration": r_explore_local["content"] if r_explore_local["ok"] else None,
        "remote_exploration": r_explore_remote["content"] if r_explore_remote["ok"] else None,
        "total_cost_usd": total_cost,
    }
    out_path = results_dir / f"cross_exam_{int(time.time())}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    info(f"Results saved: {out_path}")


def cmd_consensus(claim: str):
    """Confidence-weighted consensus on a specific claim."""
    header(f"=== Consensus Check ===")
    dim(f"Claim: {claim[:100]}")

    consensus_system = (
        "Rate this claim on a 0.0-1.0 confidence scale. Respond ONLY as JSON:\n"
        '{"confidence": 0.X, "assessment": "agree|disagree|uncertain", '
        '"reasoning": "1-2 sentences", "evidence": "what you base this on"}'
    )
    consensus_prompt = f"Evaluate this claim:\n\n{claim}"

    header("Qwen3-4B")
    r1 = query_local(consensus_system, consensus_prompt, profile="extract")
    if r1["ok"]:
        print(f"  {r1['content'][:500]}")
    else:
        fail(r1["content"])

    header("GPT-4.1-mini")
    r2 = query_remote(consensus_system, consensus_prompt, max_tokens=256)
    if r2["ok"]:
        print(f"  {r2['content'][:500]}")
    else:
        fail(r2["content"])


def _get_research_budget(rc) -> tuple:
    """Returns (spent_today, remaining, budget_limit) for research budget."""
    today = date.today().isoformat()
    spent = float(rc.get(f"surgery:research:costs:{today}") or 0)
    budget = 5.00  # $5/day dedicated research budget
    return spent, max(0, budget - spent), budget


def _track_research_cost(rc, cost_usd: float, description: str):
    """Track research cost in Redis."""
    today = date.today().isoformat()
    rc.incrbyfloat(f"surgery:research:costs:{today}", cost_usd)
    rc.expire(f"surgery:research:costs:{today}", 86400 * 3)
    # Also log the event
    event = json.dumps({
        "ts": time.time(),
        "cost_usd": cost_usd,
        "description": description[:100],
    })
    rc.lpush("surgery:research:events", event)
    rc.ltrim("surgery:research:events", 0, 99)


def _build_doc_index() -> list:
    """Build index of all markdown files with metadata."""
    import glob
    skip_dirs = {"node_modules", ".git", ".venv", "venv", "__pycache__", ".tox",
                 "bundles", "build", "dist", ".next", ".cache"}
    docs = []
    for md_path in sorted(glob.glob(str(REPO_ROOT / "**/*.md"), recursive=True)):
        p = Path(md_path)
        # Skip excluded dirs
        if any(part in skip_dirs for part in p.parts):
            continue
        rel = p.relative_to(REPO_ROOT)
        try:
            size = p.stat().st_size
            with open(p, "r", errors="replace") as f:
                lines = f.readlines()
            # Extract title (first # heading or first non-empty line)
            title = ""
            for line in lines[:10]:
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip()
                    break
                elif stripped and not title:
                    title = stripped[:80]
            docs.append({
                "path": str(rel),
                "size": size,
                "lines": len(lines),
                "title": title[:100],
            })
        except Exception:
            continue
    return docs


def _get_evidence_snapshot(topic: str, limit: int = 30) -> dict:
    """Query evidence store for learnings, claims, and grades related to a topic.
    Returns {learnings: [...], claims: [...], stats: {...}, evidence_text: str}."""
    result = {"learnings": [], "claims": [], "negative_patterns": [], "stats": {}, "evidence_text": ""}

    # 1. Query learnings via SQLite FTS
    try:
        from memory.sqlite_storage import get_sqlite_storage
        storage = get_sqlite_storage()
        learnings = storage.query(topic, limit=limit)
        result["learnings"] = learnings
        result["stats"] = storage.get_stats()
    except Exception as e:
        result["_learnings_error"] = str(e)

    # 2. Query claims from observability store
    try:
        from memory.observability_store import ObservabilityStore
        obs = ObservabilityStore(mode="auto")
        for status in ("active", "trusted", "quarantined"):
            claims = obs.get_claims_by_status(status, limit=50)
            # Filter by topic keywords
            keywords = [w.lower() for w in topic.split() if len(w) > 3]
            for c in claims:
                stmt = (c.get("statement", "") or "").lower()
                if any(kw in stmt for kw in keywords):
                    c["_status"] = status
                    result["claims"].append(c)
    except Exception as e:
        result["_claims_error"] = str(e)

    # 3. Negative patterns
    try:
        from memory.observability_store import ObservabilityStore
        obs = ObservabilityStore(mode="auto")
        patterns = obs.get_frequent_negative_patterns(min_frequency=2)
        keywords = [w.lower() for w in topic.split() if len(w) > 3]
        for p in patterns:
            desc = (p.get("description", "") + p.get("pattern_key", "")).lower()
            if any(kw in desc for kw in keywords):
                result["negative_patterns"].append(p)
    except Exception as e:
        result["_patterns_error"] = str(e)

    # 4. Build formatted text for GPT-4.1
    lines = [f"# Evidence Snapshot for: {topic}\n"]

    if result["stats"]:
        s = result["stats"]
        lines.append(f"Store: {s.get('total', 0)} learnings ({s.get('fixes', 0)} fixes, "
                      f"{s.get('wins', 0)} wins, streak: {s.get('streak', 0)}d)\n")

    if result["learnings"]:
        lines.append(f"\n## Learnings ({len(result['learnings'])} matches)\n")
        for i, l in enumerate(result["learnings"][:20], 1):
            tags = l.get("tags", [])
            if isinstance(tags, str):
                try: tags = json.loads(tags)
                except: tags = []
            lines.append(f"{i}. [{l.get('type','?')}] **{l.get('title','')}**")
            lines.append(f"   {l.get('content','')[:300]}")
            if tags:
                lines.append(f"   Tags: {', '.join(tags[:5])}")
            lines.append("")

    if result["claims"]:
        lines.append(f"\n## Claims ({len(result['claims'])} matches)\n")
        for i, c in enumerate(result["claims"][:15], 1):
            grade = c.get("evidence_grade", "?")
            conf = c.get("confidence", 0)
            wconf = c.get("weighted_confidence", 0)
            n = c.get("n", "?")
            lines.append(f"{i}. [{c.get('_status','')}] **{c.get('statement','')[:150]}**")
            lines.append(f"   Grade: {grade} | Confidence: {conf:.0%} → {wconf:.0%} weighted | n={n}")
            lines.append("")

    if result["negative_patterns"]:
        lines.append(f"\n## Anti-Patterns ({len(result['negative_patterns'])} matches)\n")
        for p in result["negative_patterns"][:10]:
            lines.append(f"- [{p.get('frequency',0)}x] **{p.get('pattern_key','')}**")
            lines.append(f"  {p.get('description','')[:200]}")
            lines.append("")

    result["evidence_text"] = "\n".join(lines)
    return result


def cmd_research_evidence(topic: str):
    """Cross-examine documents against the evidence store. Annotates claims with verdicts."""
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable — needed for budget tracking")
        return

    spent, remaining, budget = _get_research_budget(rc)
    header(f"=== Evidence Cross-Examination: {topic[:60]} ===")
    info(f"Budget: ${spent:.4f} spent / ${budget:.2f} limit (${remaining:.4f} remaining)")

    if remaining < 0.05:
        fail(f"Daily research budget exhausted (${spent:.4f} / ${budget:.2f})")
        return

    # Phase 1: Gather evidence
    header("Phase 1: Gathering Evidence from Store")
    evidence = _get_evidence_snapshot(topic)

    n_learn = len(evidence["learnings"])
    n_claims = len(evidence["claims"])
    n_patterns = len(evidence["negative_patterns"])
    ok(f"Found: {n_learn} learnings, {n_claims} claims, {n_patterns} anti-patterns")

    if n_learn == 0 and n_claims == 0:
        warn("No evidence found for this topic — cross-examination will be limited")

    # Phase 2: Select & read documents
    header("Phase 2: Selecting Documents")
    docs = _build_doc_index()
    info(f"Index: {len(docs)} markdown files")

    from collections import defaultdict
    by_dir = defaultdict(list)
    for d in docs:
        top = d["path"].split("/")[0] if "/" in d["path"] else "root"
        by_dir[top].append(d)

    index_text = f"# Document Index — {len(docs)} markdown files\n\n"
    for dir_name, dir_docs in sorted(by_dir.items()):
        index_text += f"\n## {dir_name}/ ({len(dir_docs)} files)\n"
        for d in dir_docs:
            index_text += f"- [{d['lines']:4d}L {d['size']:6d}B] {d['path']}"
            if d["title"]:
                index_text += f" — {d['title']}"
            index_text += "\n"

    select_system = (
        "You are GPT-4.1, the Cardiologist in the ContextDNA Surgery Team of 3.\n"
        "You are conducting EVIDENCE CROSS-EXAMINATION: comparing project documents "
        "against empirical evidence from the evidence store.\n\n"
        "Select 5-8 files MOST LIKELY to contain testable claims about:\n"
        f"- {topic}\n\n"
        "Prefer: design docs, architecture decisions, SOPs, and docs making strong claims.\n"
        "RESPOND ONLY as a JSON array of file paths."
    )
    select_prompt = (
        f"RESEARCH TOPIC: {topic}\n\n"
        f"EVIDENCE AVAILABLE: {n_learn} learnings, {n_claims} claims, {n_patterns} anti-patterns\n\n"
        f"{index_text}\n\n"
        "Select 5-8 files most likely to contain claims I can cross-examine against evidence."
    )

    r_select = query_remote(select_system, select_prompt, model="gpt-4.1", max_tokens=1024)
    if not r_select["ok"]:
        fail(f"File selection failed: {r_select['content']}")
        return

    _track_research_cost(rc, r_select.get("cost_usd", 0), f"evidence-select:{topic[:40]}")

    try:
        raw = r_select["content"].strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        selected = json.loads(raw)
        if not isinstance(selected, list):
            selected = [selected]
    except Exception as e:
        fail(f"Could not parse file selection: {e}")
        return

    ok(f"Selected {len(selected)} files")

    # Read files
    file_contents = {}
    total_chars = 0
    for fp in selected:
        full = REPO_ROOT / fp
        if not full.exists():
            continue
        try:
            with open(full, "r", errors="replace") as f:
                content = f.read()
            if len(content) > 8000:
                content = content[:8000] + f"\n[TRUNCATED at 8K — full: {len(content)} chars]"
            file_contents[fp] = content
            total_chars += len(content)
            if total_chars > 50000:
                break
        except Exception:
            continue

    ok(f"Read {len(file_contents)} files ({total_chars} chars)")

    # Phase 3: Cross-examination
    header("Phase 3: Evidence Cross-Examination")

    docs_text = ""
    for fp, content in file_contents.items():
        docs_text += f"\n{'='*60}\n## FILE: {fp}\n{'='*60}\n{content}\n"

    crossexam_system = (
        "You are GPT-4.1, the Cardiologist in the ContextDNA Surgery Team.\n"
        "Your task: CROSS-EXAMINE document claims against empirical evidence.\n\n"
        "For each significant claim in the documents, determine:\n"
        "- TRUE_TO_EVIDENCE: Supported by evidence (cite specific learning/claim)\n"
        "- WORTH_TESTING: Plausible but insufficient evidence (propose what to test)\n"
        "- CONTRADICTS_EVIDENCE: Evidence suggests otherwise (cite contradicting evidence)\n"
        "- NO_EVIDENCE: No evidence found either way\n"
        "- STALE: Was true but evidence suggests it's outdated\n\n"
        "Output as JSON:\n"
        "{\n"
        '  "topic": "...",\n'
        '  "cross_examination": [\n'
        '    {\n'
        '      "source_file": "path",\n'
        '      "claim": "The exact or paraphrased claim from the document",\n'
        '      "verdict": "TRUE_TO_EVIDENCE|WORTH_TESTING|CONTRADICTS_EVIDENCE|NO_EVIDENCE|STALE",\n'
        '      "evidence_support": "Specific evidence that supports/contradicts (or null)",\n'
        '      "confidence": 0.0-1.0,\n'
        '      "suggested_test": "How to verify this claim (for WORTH_TESTING verdicts)",\n'
        '      "annotation": "Note to add to the document (1-2 sentences)"\n'
        '    }\n'
        '  ],\n'
        '  "ab_test_candidates": [\n'
        '    {\n'
        '      "claim": "Claim worth A/B testing",\n'
        '      "control": "Current approach (status quo)",\n'
        '      "variant": "Alternative to test",\n'
        '      "success_metric": "How to measure which is better",\n'
        '      "effort": "low|medium|high",\n'
        '      "priority": 1-5\n'
        '    }\n'
        '  ],\n'
        '  "summary": "Overall assessment"\n'
        "}"
    )
    crossexam_prompt = (
        f"TOPIC: {topic}\n\n"
        f"=== EVIDENCE FROM STORE ===\n{evidence['evidence_text']}\n\n"
        f"=== DOCUMENTS TO CROSS-EXAMINE ===\n{docs_text}\n\n"
        "Cross-examine every significant claim in these documents against the evidence provided."
    )

    info(f"Sending {len(crossexam_prompt)} chars for cross-examination...")
    r_exam = query_remote(crossexam_system, crossexam_prompt, model="gpt-4.1", max_tokens=4096)

    if not r_exam["ok"]:
        fail(f"Cross-examination failed: {r_exam['content']}")
        return

    _track_research_cost(rc, r_exam.get("cost_usd", 0), f"evidence-exam:{topic[:40]}")
    spent, remaining, _ = _get_research_budget(rc)

    ok(f"Cross-examination complete — ${r_exam.get('cost_usd', 0):.6f}")
    info(f"Budget remaining: ${remaining:.4f}")

    # Phase 4: Output & Save
    header("Phase 4: Cross-Examination Results")
    print(f"\n{r_exam['content']}")

    # Save to WAL
    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    ts = int(time.time())
    result = {
        "timestamp": datetime.now().isoformat(),
        "sender": "GPT-4.1",
        "recipient": "Atlas",
        "message_type": "finding",
        "task_type": "verification",
        "discussion_id": f"evidence-crossexam-{ts}",
        "research_topic": topic,
        "files_analyzed": list(file_contents.keys()),
        "evidence_snapshot": {
            "learnings_count": n_learn,
            "claims_count": n_claims,
            "patterns_count": n_patterns,
        },
        "cross_examination": r_exam["content"],
        "cost_usd": {
            "selection": r_select.get("cost_usd", 0),
            "examination": r_exam.get("cost_usd", 0),
            "total": r_select.get("cost_usd", 0) + r_exam.get("cost_usd", 0),
        },
    }
    out_path = results_dir / f"evidence_crossexam_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    info(f"Results saved: {out_path}")

    # Extract A/B test candidates
    try:
        parsed = json.loads(r_exam["content"].strip().strip("```json").strip("```"))
        ab_candidates = parsed.get("ab_test_candidates", [])
        if ab_candidates:
            header("A/B Test Candidates Identified")
            for i, ab in enumerate(ab_candidates, 1):
                info(f"{i}. {ab.get('claim', '')[:80]}")
                dim(f"   Control: {ab.get('control', '')[:60]}")
                dim(f"   Variant: {ab.get('variant', '')[:60]}")
                dim(f"   Metric: {ab.get('success_metric', '')[:60]}")
                dim(f"   Effort: {ab.get('effort', '?')} | Priority: {ab.get('priority', '?')}")
            info(f"Run 'surgery-team.py ab-propose \"<claim>\"' to formalize an A/B test")
            # Store candidates in Redis for ab-propose
            rc.set(f"surgery:evidence:ab_candidates:{ts}", json.dumps(ab_candidates), ex=86400 * 7)
    except Exception:
        pass

    # Verdict summary
    try:
        parsed = json.loads(r_exam["content"].strip().strip("```json").strip("```"))
        verdicts = parsed.get("cross_examination", [])
        if verdicts:
            header("Verdict Summary")
            counts = {}
            for v in verdicts:
                vd = v.get("verdict", "UNKNOWN")
                counts[vd] = counts.get(vd, 0) + 1
            for vd, cnt in sorted(counts.items()):
                color = GREEN if "TRUE" in vd else (RED if "CONTRA" in vd else YELLOW)
                print(f"  {color}{vd}{NC}: {cnt}")
    except Exception:
        pass

    header("Evidence Cross-Examination Complete")
    total_cost = r_select.get("cost_usd", 0) + r_exam.get("cost_usd", 0)
    info(f"Total cost: ${total_cost:.6f}")
    info(f"Daily budget: ${spent:.4f} / ${budget:.2f}")


def cmd_ab_propose(claim: str):
    """Design an A/B test for a claim from evidence cross-examination."""
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable")
        return

    spent, remaining, budget = _get_research_budget(rc)
    header(f"=== A/B Test Design ===")
    info(f"Claim: {claim[:100]}")
    info(f"Budget: ${remaining:.4f} remaining")

    if remaining < 0.05:
        fail("Budget exhausted")
        return

    # Gather evidence for the claim
    evidence = _get_evidence_snapshot(claim)

    design_system = (
        "You are GPT-4.1, the Cardiologist designing an A/B test.\n"
        "Given a claim and available evidence, design a rigorous test.\n\n"
        "Output as JSON:\n"
        "{\n"
        '  "test_id": "ab-YYYYMMDD-short-name",\n'
        '  "hypothesis": "If we do X instead of Y, then Z will improve because W",\n'
        '  "claim_under_test": "The exact claim being tested",\n'
        '  "control": {\n'
        '    "description": "Current behavior (status quo)",\n'
        '    "implementation": "What code/config stays as-is"\n'
        '  },\n'
        '  "variant": {\n'
        '    "description": "Proposed alternative",\n'
        '    "implementation": "What code/config changes"\n'
        '  },\n'
        '  "success_metrics": [\n'
        '    {"metric": "name", "measure": "how to measure", "target": "X% improvement"}\n'
        '  ],\n'
        '  "sample_size": "How many observations needed",\n'
        '  "duration": "Recommended test duration",\n'
        '  "rollback_plan": "How to revert if variant fails",\n'
        '  "evidence_baseline": "Current evidence state for this claim",\n'
        '  "risks": ["What could go wrong"]\n'
        "}"
    )
    design_prompt = (
        f"CLAIM TO TEST: {claim}\n\n"
        f"=== AVAILABLE EVIDENCE ===\n{evidence['evidence_text']}\n\n"
        "Design a rigorous A/B test for this claim."
    )

    r_design = query_remote(design_system, design_prompt, model="gpt-4.1-mini", max_tokens=2048)
    if not r_design["ok"]:
        fail(f"Design failed: {r_design['content']}")
        return

    _track_research_cost(rc, r_design.get("cost_usd", 0), f"ab-design:{claim[:40]}")

    header("A/B Test Design")
    print(f"\n{r_design['content']}")

    # Save to Redis + WAL
    ts = int(time.time())
    test_data = {
        "timestamp": datetime.now().isoformat(),
        "sender": "GPT-4.1",
        "recipient": "Aaron",
        "message_type": "finding",
        "task_type": "verification",
        "discussion_id": f"ab-test-{ts}",
        "claim": claim,
        "design": r_design["content"],
        "status": "proposed",
        "cost_usd": r_design.get("cost_usd", 0),
    }
    rc.set(f"surgery:ab_tests:proposed:{ts}", json.dumps(test_data), ex=86400 * 30)
    rc.lpush("surgery:ab_tests:history", json.dumps({"ts": ts, "claim": claim[:80], "status": "proposed"}))
    rc.ltrim("surgery:ab_tests:history", 0, 49)

    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    out_path = results_dir / f"ab_test_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(test_data, f, indent=2)

    info(f"Test saved: {out_path}")
    info(f"Cost: ${r_design.get('cost_usd', 0):.6f}")
    info("To activate: Aaron approves → Atlas implements control/variant → track outcomes")


def cmd_ab_collaborate(claim: str):
    """3-surgeon collaborative A/B test finalization.

    All 3 surgeons contribute within the JSON protocol:
    1. GPT-4.1 (Cardiologist): Designs rigorous test with metrics/rollback
    2. Qwen3-4B (Neurologist): Classifies feasibility, risk, measurement complexity
    3. Atlas (Head Surgeon): Synthesizes consensus, preserves dissent, finalizes

    Atlas will NOT implement until all 3 agree.
    """
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable")
        return

    spent, remaining, budget = _get_research_budget(rc)
    header(f"=== 3-Surgeon A/B Collaboration ===")
    info(f"Claim: {claim[:100]}")
    info(f"Budget: ${remaining:.4f} remaining")

    if remaining < 0.10:
        fail("Insufficient budget for 3-surgeon collaboration (~$0.08)")
        return

    ts = int(time.time())
    discussion_id = f"ab-collab-{ts}"

    # Phase 1: Evidence gathering (shared context for all surgeons)
    header("Phase 1: Gathering Shared Evidence")
    evidence = _get_evidence_snapshot(claim)
    n_learn = len(evidence["learnings"])
    n_claims = len(evidence["claims"])
    ok(f"Evidence: {n_learn} learnings, {n_claims} claims")

    # Phase 2: GPT-4.1 designs the test
    header("Phase 2: Cardiologist (GPT-4.1) — Test Design")

    cardiologist_system = (
        "You are GPT-4.1, the Cardiologist in the Surgery Team of 3.\n"
        "Design a rigorous A/B test that all 3 surgeons can agree on.\n\n"
        "Your design must be implementable by Atlas (code changes) and measurable by Qwen3-4B (classification).\n\n"
        "Output as JSON (Surgery Team protocol):\n"
        "{\n"
        '  "sender": "GPT-4.1",\n'
        '  "recipient": "all",\n'
        '  "message_type": "finding",\n'
        '  "task_type": "verification",\n'
        f'  "discussion_id": "{discussion_id}",\n'
        '  "confidence": 0.X,\n'
        '  "reasoning": "Why this test design is sound",\n'
        '  "data": {\n'
        '    "test_id": "ab-YYYYMMDD-short-name",\n'
        '    "hypothesis": "If X then Y because Z",\n'
        '    "claim_under_test": "exact claim",\n'
        '    "control": {"description": "...", "implementation": "specific code/config"},\n'
        '    "variant": {"description": "...", "implementation": "specific code/config"},\n'
        '    "success_metrics": [{"metric": "...", "measure": "...", "target": "...", "measurable_by_4B": true}],\n'
        '    "sample_size": "N observations needed",\n'
        '    "duration": "test duration",\n'
        '    "rollback_plan": "exact steps to revert",\n'
        '    "risks": ["..."],\n'
        '    "qwen_measurement_tasks": [\n'
        '      {"task": "classify X as 0-3", "profile": "classify", "frequency": "per-webhook"}\n'
        '    ]\n'
        '  },\n'
        '  "urgency": 3\n'
        "}"
    )
    cardiologist_prompt = (
        f"CLAIM TO TEST: {claim}\n\n"
        f"EVIDENCE:\n{evidence['evidence_text']}\n\n"
        "Design an A/B test. Remember:\n"
        "- Qwen3-4B (local 4B model) will handle real-time measurement via classify(0-3) calls\n"
        "- Atlas (Claude Opus) will implement the code changes\n"
        "- Include specific 'qwen_measurement_tasks' that a 4B model can reliably perform\n"
        "- Metrics must be automatically trackable, not subjective"
    )

    r_cardio = query_remote(cardiologist_system, cardiologist_prompt, model="gpt-4.1", max_tokens=2048)
    if not r_cardio["ok"]:
        fail(f"Cardiologist failed: {r_cardio['content']}")
        return

    _track_research_cost(rc, r_cardio.get("cost_usd", 0), f"ab-collab-cardio:{claim[:30]}")
    ok(f"Cardiologist design: ${r_cardio.get('cost_usd', 0):.6f}")

    # Parse cardiologist output
    cardio_raw = r_cardio["content"]
    try:
        cardio_json = json.loads(cardio_raw.strip().strip("```json").strip("```"))
    except Exception:
        cardio_json = {"raw": cardio_raw, "confidence": 0.5}

    print(f"\n{DIM}--- Cardiologist Design ---{NC}")
    print(cardio_raw[:2000])

    # Phase 3: Qwen3-4B reviews feasibility
    header("Phase 3: Neurologist (Qwen3-4B) — Feasibility Review")

    neuro_system = (
        "You are Qwen3-4B, the Neurologist in the Surgery Team. /no_think\n"
        "Review this A/B test design for FEASIBILITY from your perspective.\n"
        "You will be responsible for real-time measurement during the test.\n\n"
        "Score each dimension 0-3:\n"
        "- measurement_feasibility: Can you reliably classify the metrics? (0=impossible, 3=trivial)\n"
        "- risk_level: How risky is this test? (0=dangerous, 3=safe)\n"
        "- implementation_clarity: Is the implementation clear enough? (0=vague, 3=crystal)\n\n"
        "Output JSON ONLY:\n"
        '{"sender":"Qwen3-4B","recipient":"all","message_type":"finding",'
        '"task_type":"verification",'
        f'"discussion_id":"{discussion_id}",'
        '"confidence_score":N,'
        '"data":{"measurement_feasibility":N,"risk_level":N,"implementation_clarity":N,'
        '"concerns":["..."],"suggestions":["..."]},'
        '"reasoning":"1-2 sentences"}'
    )
    neuro_prompt = (
        f"CLAIM: {claim}\n\n"
        f"CARDIOLOGIST'S TEST DESIGN:\n{cardio_raw[:2000]}\n\n"
        "Review: Can you (a 4B parameter model) reliably measure the success metrics?\n"
        "What concerns or suggestions do you have?"
    )

    r_neuro = query_local(neuro_system, neuro_prompt, profile="extract_deep", max_chars=3000)
    if not r_neuro["ok"]:
        warn(f"Neurologist unavailable: {r_neuro['content'][:80]}")
        neuro_json = {"confidence_score": 1, "data": {"measurement_feasibility": 1, "concerns": ["LLM unavailable"]}}
    else:
        ok(f"Neurologist review: {r_neuro['latency_ms']}ms")
        try:
            neuro_raw = r_neuro["content"]
            # Try to extract JSON from response
            if "{" in neuro_raw:
                json_start = neuro_raw.index("{")
                json_end = neuro_raw.rindex("}") + 1
                neuro_json = json.loads(neuro_raw[json_start:json_end])
            else:
                neuro_json = {"raw": neuro_raw, "confidence_score": 1}
        except Exception:
            # Regex fallback for truncated JSON — extract individual fields
            neuro_json = {"raw": r_neuro["content"], "confidence_score": 1, "data": {}}
            import re
            for field in ["measurement_feasibility", "risk_level", "implementation_clarity", "confidence_score"]:
                m = re.search(rf'"{field}"\s*:\s*(\d+)', neuro_raw)
                if m:
                    val = int(m.group(1))
                    if field == "confidence_score":
                        neuro_json["confidence_score"] = val
                    else:
                        neuro_json["data"][field] = val

    print(f"\n{DIM}--- Neurologist Review ---{NC}")
    neuro_display = neuro_json.get("raw", json.dumps(neuro_json.get("data", neuro_json), indent=2))
    print(str(neuro_display)[:1000])

    # Phase 4: Atlas synthesizes consensus
    header("Phase 4: Atlas (Head Surgeon) — Consensus Synthesis")

    # Extract scores from neurologist
    neuro_data = neuro_json.get("data", neuro_json)
    meas_score = neuro_data.get("measurement_feasibility", 1)
    risk_score = neuro_data.get("risk_level", 2)
    clarity_score = neuro_data.get("implementation_clarity", 2)
    neuro_concerns = neuro_data.get("concerns", [])
    neuro_suggestions = neuro_data.get("suggestions", [])
    neuro_confidence = neuro_json.get("confidence_score", 1)

    # Map Qwen 0-3 to float
    neuro_conf_float = {0: 0.15, 1: 0.35, 2: 0.65, 3: 0.92}.get(neuro_confidence, 0.35)
    cardio_confidence = cardio_json.get("confidence", 0.75)

    # Atlas assessment
    atlas_confidence = min(0.95, (cardio_confidence * 0.5 + neuro_conf_float * 0.3 + 0.15))

    # Check for dissent
    dissent = []
    if meas_score < 2:
        dissent.append({
            "surgeon": "Qwen3-4B",
            "concern": "Measurement feasibility too low for reliable automated tracking",
            "score": meas_score,
            "recommendation": "Simplify metrics or use GPT-4.1 for measurement instead",
        })
    if risk_score < 1:
        dissent.append({
            "surgeon": "Qwen3-4B",
            "concern": "Risk assessment indicates potential system instability",
            "score": risk_score,
            "recommendation": "Add stronger rollback safeguards or reduce test scope",
        })
    if cardio_confidence < 0.5:
        dissent.append({
            "surgeon": "GPT-4.1",
            "concern": "Cardiologist has low confidence in test design",
            "confidence": cardio_confidence,
            "recommendation": "Gather more evidence before proceeding",
        })
    # Concerns are informational caveats, not blocking dissent
    caveats = []
    for concern in neuro_concerns:
        if isinstance(concern, str) and len(concern) > 5:
            caveats.append({"surgeon": "Qwen3-4B", "concern": concern})

    # Build consensus — score-based dissent blocks; concerns are caveats
    if not dissent and not caveats:
        consensus_status = "approved"
    elif not dissent and caveats:
        consensus_status = "approved_with_caveats"
    elif dissent and meas_score >= 2:
        consensus_status = "approved_with_caveats"
    else:
        consensus_status = "needs_revision"

    consensus = {
        "sender": "Atlas",
        "recipient": "Aaron",
        "timestamp": datetime.now().isoformat(),
        "discussion_id": discussion_id,
        "message_type": "consensus",
        "task_type": "verification",
        "confidence": round(atlas_confidence, 3),
        "reasoning": f"3-surgeon review complete. Cardiologist confidence: {cardio_confidence:.0%}, "
                     f"Neurologist scores: measurement={meas_score}/3, risk={risk_score}/3, clarity={clarity_score}/3. "
                     f"{'Score-based dissent exists.' if dissent else ''}"
                     f"{f' {len(caveats)} informational caveats noted.' if caveats else ''}"
                     f"{' All surgeons in agreement.' if not dissent and not caveats else ''}",
        "urgency": 3,
        "data": {
            "claim_under_test": claim,
            "consensus_status": consensus_status,
            "test_design": cardio_json.get("data", cardio_json),
            "cardiologist_assessment": {
                "confidence": cardio_confidence,
                "reasoning": cardio_json.get("reasoning", ""),
            },
            "neurologist_assessment": {
                "measurement_feasibility": meas_score,
                "risk_level": risk_score,
                "implementation_clarity": clarity_score,
                "confidence_score": neuro_confidence,
                "concerns": neuro_concerns,
                "suggestions": neuro_suggestions,
            },
            "atlas_assessment": {
                "weighted_confidence": round(atlas_confidence, 3),
                "implementation_ready": consensus_status in ("approved", "approved_with_caveats"),
                "dissent_count": len(dissent),
                "caveat_count": len(caveats),
            },
            "dissent": dissent if dissent else [],
            "caveats": caveats if caveats else [],
        },
    }

    # Display consensus
    status_color = GREEN if consensus_status == "approved" else (YELLOW if "caveat" in consensus_status else RED)
    print(f"\n  {status_color}{BOLD}CONSENSUS: {consensus_status.upper()}{NC}")
    info(f"Atlas weighted confidence: {atlas_confidence:.0%}")
    info(f"Cardiologist: {cardio_confidence:.0%} | Neurologist: conf={neuro_confidence}/3 meas={meas_score}/3 risk={risk_score}/3 clarity={clarity_score}/3")

    if dissent:
        header("Dissent Record (preserved per protocol)")
        for d in dissent:
            warn(f"[{d['surgeon']}] {d['concern']}")
            if "recommendation" in d:
                dim(f"   Recommendation: {d['recommendation']}")

    if caveats:
        header("Caveats (informational — not blocking)")
        for c in caveats:
            dim(f"  [{c['surgeon']}] {c['concern']}")

    # Show test design summary
    test_data = cardio_json.get("data", {})
    if test_data:
        header("Finalized Test Design")
        info(f"Test ID: {test_data.get('test_id', 'TBD')}")
        info(f"Hypothesis: {test_data.get('hypothesis', 'N/A')[:120]}")
        dim(f"  Control: {json.dumps(test_data.get('control', {}))[:100]}")
        dim(f"  Variant: {json.dumps(test_data.get('variant', {}))[:100]}")
        metrics = test_data.get("success_metrics", [])
        for m in metrics[:3]:
            dim(f"  Metric: {m.get('metric', '?')} — target: {m.get('target', '?')}")
        dim(f"  Duration: {test_data.get('duration', 'TBD')}")
        dim(f"  Rollback: {test_data.get('rollback_plan', 'TBD')[:80]}")

    # Save to Redis + WAL
    rc.set(f"surgery:ab_tests:consensus:{ts}", json.dumps(consensus), ex=86400 * 30)
    rc.lpush("surgery:ab_tests:history", json.dumps({
        "ts": ts, "claim": claim[:80], "status": consensus_status,
        "discussion_id": discussion_id, "type": "collaborate",
    }))
    rc.ltrim("surgery:ab_tests:history", 0, 49)

    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    out_path = results_dir / f"ab_consensus_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(consensus, f, indent=2)

    info(f"Consensus saved: {out_path}")
    spent, _, _ = _get_research_budget(rc)
    total_cost = r_cardio.get("cost_usd", 0)
    info(f"Session cost: ${total_cost:.6f} | Daily: ${spent:.4f} / ${budget:.2f}")

    if consensus_status == "approved":
        header("READY FOR IMPLEMENTATION")
        info("All 3 surgeons agree. Atlas may proceed with implementation.")
        info(f"To activate: surgery-team.py ab-start {ts}")
    elif "caveat" in consensus_status:
        header("APPROVED WITH CAVEATS")
        warn("Review dissent above. Aaron's directive needed to proceed or revise.")
        info(f"To activate anyway: surgery-team.py ab-start {ts}")
        info(f"To revise: surgery-team.py ab-collaborate \"{claim}\"")
    else:
        header("NEEDS REVISION")
        fail("Consensus not reached. Address dissent before proceeding.")
        info(f"To retry with refined claim: surgery-team.py ab-collaborate \"<refined claim>\"")


def cmd_ab_start(test_ref: str):
    """Activate an A/B test — change status from proposed/consensus to active."""
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable")
        return

    header(f"=== Activate A/B Test ===")

    # Find the test by timestamp reference
    consensus_key = f"surgery:ab_tests:consensus:{test_ref}"
    proposed_key = f"surgery:ab_tests:proposed:{test_ref}"

    consensus_data = rc.get(consensus_key)
    proposed_data = rc.get(proposed_key)

    if consensus_data:
        test = json.loads(consensus_data)
        source = "consensus"
    elif proposed_data:
        test = json.loads(proposed_data)
        source = "proposed"
    else:
        fail(f"No test found for ref: {test_ref}")
        info("Run 'surgery-team.py ab-status' to see available tests")
        return

    claim = test.get("data", test).get("claim_under_test", test.get("claim", "unknown"))
    info(f"Source: {source}")
    info(f"Claim: {claim[:100]}")

    # Check consensus status
    if source == "consensus":
        status = test.get("data", {}).get("consensus_status", "unknown")
        if status == "needs_revision":
            fail("This test has unresolved dissent. Cannot activate.")
            info("Address dissent first or get Aaron's directive to override.")
            return

    # Activate
    ts = int(time.time())
    active_record = {
        "test_ref": test_ref,
        "activated_at": datetime.now().isoformat(),
        "activated_ts": ts,
        "source": source,
        "claim": claim,
        "status": "active",
        "test_design": test.get("data", {}).get("test_design", test.get("design", {})),
        "observations": [],
        "metrics": {},
    }

    rc.set(f"surgery:ab_tests:active:{test_ref}", json.dumps(active_record), ex=86400 * 60)

    # Update history
    rc.lpush("surgery:ab_tests:history", json.dumps({
        "ts": ts, "claim": claim[:80], "status": "active",
        "test_ref": test_ref, "type": "activation",
    }))
    rc.ltrim("surgery:ab_tests:history", 0, 49)

    ok(f"Test ACTIVATED — ref: {test_ref}")

    # Show implementation checklist
    test_design = active_record["test_design"]
    if isinstance(test_design, dict):
        header("Implementation Checklist")
        control = test_design.get("control", {})
        variant = test_design.get("variant", {})
        info(f"CONTROL: {control.get('description', 'N/A')[:80]}")
        dim(f"  Impl: {control.get('implementation', 'N/A')[:100]}")
        info(f"VARIANT: {variant.get('description', 'N/A')[:80]}")
        dim(f"  Impl: {variant.get('implementation', 'N/A')[:100]}")

        metrics = test_design.get("success_metrics", [])
        header("Metrics to Track")
        for m in metrics:
            info(f"{m.get('metric', '?')}: {m.get('measure', '?')} → target: {m.get('target', '?')}")

        qwen_tasks = test_design.get("qwen_measurement_tasks", [])
        if qwen_tasks:
            header("Qwen3-4B Measurement Tasks")
            for qt in qwen_tasks:
                info(f"{qt.get('task', '?')} [{qt.get('profile', '?')}] @ {qt.get('frequency', '?')}")

        rollback = test_design.get("rollback_plan", "")
        if rollback:
            header("Rollback Plan")
            dim(f"  {rollback[:200]}")

    info(f"\nTrack progress: surgery-team.py ab-measure {test_ref}")
    info(f"Conclude test: surgery-team.py ab-conclude {test_ref} win|lose|inconclusive")


def cmd_ab_measure(test_ref: str):
    """Measure an active A/B test — query evidence store for outcomes."""
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable")
        return

    header(f"=== A/B Test Measurement ===")

    active_data = rc.get(f"surgery:ab_tests:active:{test_ref}")
    if not active_data:
        fail(f"No active test for ref: {test_ref}")
        info("Run 'surgery-team.py ab-status' to see available tests")
        return

    test = json.loads(active_data)
    claim = test.get("claim", "unknown")
    activated = test.get("activated_at", "unknown")

    info(f"Claim: {claim[:100]}")
    info(f"Activated: {activated}")

    # Query evidence for outcomes related to this test
    evidence = _get_evidence_snapshot(claim, limit=50)

    header("Evidence Since Activation")
    n_learn = len(evidence["learnings"])
    n_claims = len(evidence["claims"])
    ok(f"Found: {n_learn} learnings, {n_claims} claims related to this test")

    # Show learnings sorted by recency
    if evidence["learnings"]:
        header("Recent Learnings")
        for i, l in enumerate(evidence["learnings"][:10], 1):
            info(f"{i}. [{l.get('type','?')}] {l.get('title','')[:80]}")
            dim(f"   {l.get('content','')[:150]}")

    # Show claims with grades
    if evidence["claims"]:
        header("Related Claims & Grades")
        for c in evidence["claims"][:10]:
            grade = c.get("evidence_grade", "?")
            conf = c.get("weighted_confidence", 0)
            info(f"[{grade}] {c.get('statement','')[:80]} (conf: {conf:.0%})")

    # Quick Qwen3-4B assessment of test progress
    header("Neurologist Quick Assessment")
    neuro_system = (
        "You are Qwen3-4B. /no_think\n"
        "Score this A/B test's progress 0-3. Reply JSON ONLY:\n"
        '{"score":N,"assessment":"one sentence"}'
    )
    neuro_prompt = (
        f"Test claim: {claim}\n"
        f"Evidence found: {n_learn} learnings, {n_claims} claims\n"
        f"Activated: {activated}\n"
        "Score progress: 0=no data, 1=early, 2=trending, 3=conclusive"
    )

    r_neuro = query_local(neuro_system, neuro_prompt, profile="classify", max_chars=300)
    if r_neuro["ok"]:
        print(f"  {r_neuro['content'][:200]}")
    else:
        dim("  (Neurologist unavailable)")

    # Duration check
    try:
        act_ts = test.get("activated_ts", 0)
        elapsed_hours = (time.time() - act_ts) / 3600
        info(f"\nElapsed: {elapsed_hours:.1f} hours since activation")

        test_design = test.get("test_design", {})
        duration = test_design.get("duration", "")
        if duration:
            info(f"Planned duration: {duration}")
    except Exception:
        pass

    info(f"\nConclude: surgery-team.py ab-conclude {test_ref} win|lose|inconclusive")


def cmd_ab_conclude(args: str):
    """Conclude an A/B test with a verdict. Records results and feeds back to evidence store."""
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable")
        return

    # Parse args: "test_ref verdict"
    parts = args.split()
    if len(parts) < 2:
        fail("Usage: ab-conclude <test_ref> <win|lose|inconclusive>")
        return

    test_ref = parts[0]
    verdict = parts[1].lower()
    if verdict not in ("win", "lose", "inconclusive"):
        fail(f"Invalid verdict: {verdict}. Use: win, lose, inconclusive")
        return

    header(f"=== Conclude A/B Test ===")

    active_data = rc.get(f"surgery:ab_tests:active:{test_ref}")
    if not active_data:
        fail(f"No active test for ref: {test_ref}")
        return

    test = json.loads(active_data)
    claim = test.get("claim", "unknown")

    info(f"Claim: {claim[:100]}")
    info(f"Verdict: {verdict.upper()}")

    ts = int(time.time())

    # Record conclusion
    conclusion = {
        "test_ref": test_ref,
        "claim": claim,
        "verdict": verdict,
        "concluded_at": datetime.now().isoformat(),
        "activated_at": test.get("activated_at", ""),
        "test_design": test.get("test_design", {}),
    }

    # Feed back to evidence store as a new learning
    try:
        from memory.sqlite_storage import get_sqlite_storage
        storage = get_sqlite_storage()

        verdict_map = {
            "win": ("win", f"A/B TEST WON: {claim}"),
            "lose": ("fix", f"A/B TEST LOST: {claim} — variant did NOT improve over control"),
            "inconclusive": ("pattern", f"A/B TEST INCONCLUSIVE: {claim} — insufficient signal"),
        }
        ltype, title = verdict_map[verdict]

        storage.store_learning({
            "type": ltype,
            "title": title[:200],
            "content": (
                f"Test ref: {test_ref}\n"
                f"Claim: {claim}\n"
                f"Verdict: {verdict}\n"
                f"Duration: {test.get('activated_at', '?')} → {datetime.now().isoformat()}\n"
                f"Design: {json.dumps(test.get('test_design', {}))[:500]}"
            ),
            "tags": ["ab_test", f"verdict_{verdict}", "surgery_team"],
            "area": "ab_testing",
        })
        ok(f"Learning recorded in evidence store (type: {ltype})")
    except Exception as e:
        warn(f"Could not record to evidence store: {e}")

    # Move from active to completed in Redis
    rc.delete(f"surgery:ab_tests:active:{test_ref}")
    rc.set(f"surgery:ab_tests:completed:{test_ref}", json.dumps(conclusion), ex=86400 * 90)

    # Update history
    rc.lpush("surgery:ab_tests:history", json.dumps({
        "ts": ts, "claim": claim[:80], "status": f"completed_{verdict}",
        "test_ref": test_ref, "type": "conclusion",
    }))
    rc.ltrim("surgery:ab_tests:history", 0, 49)

    # Save to WAL
    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    out_path = results_dir / f"ab_conclusion_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(conclusion, f, indent=2)

    verdict_color = GREEN if verdict == "win" else (RED if verdict == "lose" else YELLOW)
    print(f"\n  {verdict_color}{BOLD}VERDICT: {verdict.upper()}{NC}")
    info(f"Results saved: {out_path}")
    info(f"Evidence store updated with verdict learning")

    if verdict == "win":
        ok("Variant proved superior. Consider making it the default.")
    elif verdict == "lose":
        info("Control remains. Rollback any variant changes.")
    else:
        info("Insufficient evidence. Consider extending test or refining metrics.")


def cmd_ab_status():
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable")
        return

    header("=== A/B Test Dashboard ===")

    history = rc.lrange("surgery:ab_tests:history", 0, 19)
    if not history:
        info("No A/B tests proposed yet")
        info("Run: surgery-team.py evidence \"topic\" → identifies testable claims")
        info("Then: surgery-team.py ab-propose \"claim\" → designs the test")
        return

    proposed = 0
    active = 0
    completed = 0
    for entry in history:
        try:
            d = json.loads(entry)
            ts = datetime.fromtimestamp(d["ts"]).strftime("%Y-%m-%d %H:%M")
            status = d.get("status", "proposed")
            color = YELLOW if status == "proposed" else (GREEN if status == "active" else CYAN)
            print(f"  {color}[{status:10s}]{NC} {ts} — {d.get('claim', '')[:70]}")
            if status == "proposed": proposed += 1
            elif status == "active": active += 1
            else: completed += 1
        except Exception:
            pass

    header("Summary")
    info(f"Proposed: {proposed} | Active: {active} | Completed: {completed}")

    # Check for recent evidence cross-examination candidates
    import glob
    ab_keys = rc.keys("surgery:evidence:ab_candidates:*")
    if ab_keys:
        header("Unactioned A/B Candidates from Cross-Examination")
        for key in ab_keys[:3]:
            try:
                candidates = json.loads(rc.get(key))
                for c in candidates[:3]:
                    dim(f"  • {c.get('claim', '')[:70]} (effort: {c.get('effort', '?')})")
            except Exception:
                pass


def cmd_research(topic: str):
    """GPT-4.1 self-directed document research with $5/day budget."""
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable — needed for budget tracking")
        return

    spent, remaining, budget = _get_research_budget(rc)
    header(f"=== Cardiologist Research: {topic[:60]} ===")
    info(f"Budget: ${spent:.4f} spent / ${budget:.2f} limit (${remaining:.4f} remaining)")

    if remaining < 0.01:
        fail(f"Daily research budget exhausted (${spent:.4f} / ${budget:.2f})")
        return

    # Phase 1: Build document index
    header("Phase 1: Building Document Index")
    docs = _build_doc_index()
    info(f"Found {len(docs)} markdown files")

    # Group by top-level directory
    from collections import defaultdict
    by_dir = defaultdict(list)
    for d in docs:
        top = d["path"].split("/")[0] if "/" in d["path"] else "root"
        by_dir[top].append(d)

    # Build compact index for GPT-4.1
    index_text = f"# Document Index — {len(docs)} markdown files\n\n"
    for dir_name, dir_docs in sorted(by_dir.items()):
        index_text += f"\n## {dir_name}/ ({len(dir_docs)} files)\n"
        for d in dir_docs:
            index_text += f"- [{d['lines']:4d}L {d['size']:6d}B] {d['path']}"
            if d["title"]:
                index_text += f" — {d['title']}"
            index_text += "\n"

    info(f"Index: {len(index_text)} chars across {len(by_dir)} directories")

    # Phase 2: GPT-4.1 selects files to explore
    header("Phase 2: Cardiologist Selects Files")

    select_system = (
        "You are GPT-4.1, the Cardiologist in the ContextDNA Surgery Team of 3.\n"
        "You have access to a project's complete markdown document index.\n"
        "Your job: select the most relevant files to read for the research topic.\n"
        "You will read these files in the next step, so choose wisely.\n\n"
        "Rules:\n"
        "- Select 5-10 files maximum per round (budget-conscious)\n"
        "- Prefer files with more lines (more content) over stubs\n"
        "- Look for design docs, architecture docs, and implementation notes\n"
        "- RESPOND ONLY as a JSON array of file paths, nothing else\n"
        "- Example: [\"docs/architecture.md\", \"memory/README.md\"]"
    )
    select_prompt = (
        f"RESEARCH TOPIC: {topic}\n\n"
        f"{index_text}\n\n"
        "Select 5-10 files most relevant to this research topic. JSON array only."
    )

    r_select = query_remote(select_system, select_prompt, model="gpt-4.1", max_tokens=1024)
    if not r_select["ok"]:
        fail(f"File selection failed: {r_select['content']}")
        return

    _track_research_cost(rc, r_select.get("cost_usd", 0), f"select:{topic[:50]}")
    info(f"Selection cost: ${r_select.get('cost_usd', 0):.6f}")

    # Parse selected files
    try:
        raw = r_select["content"].strip()
        # Handle markdown code fences
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        selected = json.loads(raw)
        if not isinstance(selected, list):
            selected = [selected]
    except Exception as e:
        fail(f"Could not parse file selection: {e}")
        info(f"Raw response: {r_select['content'][:300]}")
        return

    ok(f"Selected {len(selected)} files:")
    for fp in selected:
        dim(f"  {fp}")

    # Phase 3: Read selected files and feed to GPT-4.1
    header("Phase 3: Reading & Analyzing Documents")

    file_contents = {}
    total_chars = 0
    max_chars_per_file = 8000  # Cap per file to stay within context
    max_total_chars = 60000   # ~15K tokens input cap for analysis

    for fp in selected:
        full = REPO_ROOT / fp
        if not full.exists():
            warn(f"File not found: {fp}")
            continue
        try:
            with open(full, "r", errors="replace") as f:
                content = f.read()
            if len(content) > max_chars_per_file:
                content = content[:max_chars_per_file] + f"\n\n[... TRUNCATED at {max_chars_per_file} chars — full file is {len(content)} chars]"
            file_contents[fp] = content
            total_chars += len(content)
            ok(f"{fp} ({len(content)} chars)")
            if total_chars > max_total_chars:
                warn(f"Reached {total_chars} char cap — skipping remaining files")
                break
        except Exception as e:
            warn(f"Error reading {fp}: {e}")

    # Check budget before analysis call (this is the expensive one)
    spent, remaining, _ = _get_research_budget(rc)
    if remaining < 0.05:
        fail(f"Insufficient budget for analysis (${remaining:.4f} remaining)")
        return

    # Build analysis prompt
    docs_text = ""
    for fp, content in file_contents.items():
        docs_text += f"\n{'='*60}\n## FILE: {fp}\n{'='*60}\n{content}\n"

    analysis_system = (
        "You are GPT-4.1, the Cardiologist in the ContextDNA Surgery Team of 3.\n"
        "You have read a selection of project documents. Analyze them for the research topic.\n\n"
        "Your analysis should follow the Surgery Team JSON Communication Protocol:\n"
        "Produce findings that Atlas and Qwen3-4B can act on.\n\n"
        "Output format — respond as JSON:\n"
        "{\n"
        '  "research_topic": "...",\n'
        '  "files_analyzed": ["..."],\n'
        '  "findings": [\n'
        '    {"finding": "...", "evidence_refs": ["file:line"], "confidence": 0.X, "urgency": 1-5, "category": "architecture|gap|risk|opportunity|pattern"},\n'
        '  ],\n'
        '  "connections": ["Cross-file patterns or dependencies discovered"],\n'
        '  "blind_spots": ["What I could NOT determine from these files alone"],\n'
        '  "recommended_next_reads": ["Files I wish I had also read"],\n'
        '  "summary": "2-3 sentence synthesis"\n'
        "}"
    )
    analysis_prompt = (
        f"RESEARCH TOPIC: {topic}\n\n"
        f"I selected and read these {len(file_contents)} files:\n\n"
        f"{docs_text}\n\n"
        "Provide your research findings as structured JSON."
    )

    info(f"Sending {len(docs_text)} chars to GPT-4.1 for analysis...")
    r_analysis = query_remote(analysis_system, analysis_prompt, model="gpt-4.1", max_tokens=4096)

    if not r_analysis["ok"]:
        fail(f"Analysis failed: {r_analysis['content']}")
        return

    _track_research_cost(rc, r_analysis.get("cost_usd", 0), f"analyze:{topic[:50]}")
    spent, remaining, _ = _get_research_budget(rc)

    ok(f"Analysis complete — ${r_analysis.get('cost_usd', 0):.6f} "
       f"({r_analysis.get('tokens_in', 0)} in / {r_analysis.get('tokens_out', 0)} out)")
    info(f"Budget remaining: ${remaining:.4f}")

    # Phase 4: Output
    header("Phase 4: Research Findings")
    print(f"\n{r_analysis['content']}")

    # Save to WAL
    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    ts = int(time.time())
    result = {
        "timestamp": datetime.now().isoformat(),
        "sender": "GPT-4.1",
        "recipient": "Atlas",
        "message_type": "finding",
        "discussion_id": f"research-{ts}",
        "research_topic": topic,
        "files_selected": selected,
        "files_analyzed": list(file_contents.keys()),
        "analysis": r_analysis["content"],
        "cost_usd": {
            "selection": r_select.get("cost_usd", 0),
            "analysis": r_analysis.get("cost_usd", 0),
            "total": r_select.get("cost_usd", 0) + r_analysis.get("cost_usd", 0),
        },
        "tokens": {
            "selection": {"in": r_select.get("tokens_in", 0), "out": r_select.get("tokens_out", 0)},
            "analysis": {"in": r_analysis.get("tokens_in", 0), "out": r_analysis.get("tokens_out", 0)},
        },
    }
    out_path = results_dir / f"research_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    info(f"Results saved: {out_path}")

    # Optional Phase 5: Follow-up reads (if GPT-4.1 recommended more)
    try:
        parsed = json.loads(r_analysis["content"].strip().strip("```json").strip("```"))
        next_reads = parsed.get("recommended_next_reads", [])
        if next_reads and remaining > 0.10:
            header("Phase 5: Follow-Up Reads Available")
            info(f"GPT-4.1 recommends reading {len(next_reads)} more files:")
            for nr in next_reads:
                dim(f"  {nr}")
            info("Run again with --continue to explore these files")
    except Exception:
        pass  # Could not parse, that's fine

    header("Research Complete")
    total_cost = r_select.get("cost_usd", 0) + r_analysis.get("cost_usd", 0)
    info(f"Total cost this session: ${total_cost:.6f}")
    info(f"Daily budget: ${spent:.4f} / ${budget:.2f}")


def cmd_research_status():
    """Show research budget status and recent research events."""
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable")
        return

    header("=== Cardiologist Research Budget ===")
    spent, remaining, budget = _get_research_budget(rc)
    bar_len = int(min(spent / budget, 1.0) * 20) if budget > 0 else 0
    bar = f"[{'█' * bar_len}{'░' * (20 - bar_len)}]"
    info(f"Today: ${spent:.4f} / ${budget:.2f} {bar} (${remaining:.4f} remaining)")

    # Show recent 3 days
    for i in range(1, 4):
        from datetime import timedelta
        d = (date.today() - timedelta(days=i)).isoformat()
        past = float(rc.get(f"surgery:research:costs:{d}") or 0)
        if past > 0:
            dim(f"  {d}: ${past:.4f}")

    # Recent events
    events = rc.lrange("surgery:research:events", 0, 9)
    if events:
        header("Recent Research Sessions")
        for e in events[:5]:
            try:
                d = json.loads(e)
                ts = datetime.fromtimestamp(d["ts"]).strftime("%m-%d %H:%M")
                info(f"{ts} ${d['cost_usd']:.6f} — {d['description']}")
            except Exception:
                pass


def cmd_status():
    """Show hybrid routing status + costs."""
    header("=== Surgery Team Status ===")

    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        rc.ping()
    except Exception:
        fail("Redis unreachable")
        return

    today = date.today().isoformat()

    # Hybrid mode
    mode = rc.get("llm:hybrid_mode") or "fallback-only"
    info(f"Hybrid mode: {mode}")

    # Budget
    cost = float(rc.get(f"llm:costs:{today}") or 0)
    budget = float(os.environ.get("LLM_DAILY_BUDGET_USD", "5.0"))
    bar_len = int(min(cost / budget, 1.0) * 20) if budget > 0 else 0
    bar = f"[{'█' * bar_len}{'░' * (20 - bar_len)}]"
    info(f"Daily cost: ${cost:.4f} / ${budget:.2f} {bar}")

    # Provider stats
    stats = rc.hgetall("llm:provider_stats")
    if stats:
        header("Provider Stats")
        for provider in ["local", "openai"]:
            calls = stats.get(f"{provider}:calls", "0")
            errors = stats.get(f"{provider}:errors", "0")
            total = float(stats.get(f"{provider}:total_cost", "0"))
            info(f"{provider}: {calls} calls, {errors} errors, ${total:.4f}")

    # Recent fallback events
    events = rc.lrange("llm:fallback_events", 0, 9)
    if events:
        header(f"Recent Fallbacks ({len(events)})")
        for e in events[:5]:
            try:
                d = json.loads(e)
                ts = datetime.fromtimestamp(d["ts"]).strftime("%H:%M:%S")
                info(f"{ts} [{d['profile']}] → {d['model']} {d['latency_ms']}ms ${d['cost_usd']:.6f}")
            except Exception:
                dim(f"  (unparseable event)")
    else:
        info("No fallback events today")

    # GPU lock
    lock_holder = rc.get("llm:gpu_lock")
    if lock_holder:
        warn(f"GPU lock held: {lock_holder}")
    else:
        ok("GPU lock: free")


# ─────────────────────────────────────────────────────────────────────────────
# NEUROLOGIST SPECIAL SKILLS
# ─────────────────────────────────────────────────────────────────────────────

ARCHIVE_DB = Path.home() / ".context-dna" / "session_archive.db"


def cmd_neurologist_pulse():
    """Neurologist System Pulse — live system health from Redis + SQLite."""
    import redis as redis_lib
    import sqlite3

    header("=== Neurologist System Pulse ===")
    dim("[Qwen3-4B — Continuous System Consciousness]")

    pulse_data = {}

    # ── Phase 1: Redis reads ──
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable — pulse degraded")
        return

    # 1. Anticipation cache health
    header("[1] ANTICIPATION CACHE HEALTH")
    cache_status = {}
    for section in ("s2", "s6", "s8"):
        keys = list(rc.scan_iter(f"contextdna:anticipation:{section}:*", count=20))
        if keys:
            best_ttl = max(rc.ttl(k) for k in keys[:3])
            if best_ttl > 100:
                status = f"{GREEN}WARM{NC} — TTL {best_ttl}s"
                cache_status[section] = "warm"
            elif best_ttl > 0:
                status = f"{YELLOW}COOLING{NC} — TTL {best_ttl}s"
                cache_status[section] = "cooling"
            else:
                status = f"{RED}EXPIRED{NC}"
                cache_status[section] = "cold"
        else:
            status = f"{RED}COLD{NC} — key absent"
            cache_status[section] = "cold"
        labels = {"s2": "Professor", "s6": "Holistic", "s8": "Synaptic"}
        info(f"{labels[section]:10s} {status}")

    cold_count = sum(1 for v in cache_status.values() if v == "cold")
    if cold_count > 0:
        warn(f"{cold_count}/3 sections cold — webhook will use placeholders")
    pulse_data["anticipation"] = cache_status

    # 2. Critical findings
    header("[2] CRITICAL FINDINGS")
    crit_count = rc.llen("contextdna:critical:recent")
    info(f"Unacknowledged criticals in Redis: {crit_count}")
    if crit_count > 0:
        items = rc.lrange("contextdna:critical:recent", 0, 4)
        for item in items:
            try:
                d = json.loads(item)
                dim(f"  [{d.get('pass', '?')}] {d.get('finding', '')[:80]}")
            except Exception:
                pass
    pulse_data["criticals"] = crit_count

    # 3. LLM vitals
    header("[3] LIVE SYSTEM VITALS")
    today = date.today().isoformat()

    stats = rc.hgetall("llm:provider_stats")
    local_calls = int(stats.get("local:calls", 0))
    local_errors = int(stats.get("local:errors", 0))
    error_rate = (local_errors / local_calls * 100) if local_calls > 0 else 0
    if error_rate > 10:
        fail(f"LLM error rate: {error_rate:.1f}% ({local_errors}/{local_calls})")
    elif error_rate > 5:
        warn(f"LLM error rate: {error_rate:.1f}% ({local_errors}/{local_calls})")
    else:
        ok(f"LLM error rate: {error_rate:.1f}% ({local_errors}/{local_calls})")

    daily_cost = float(rc.get(f"llm:costs:{today}") or 0)
    budget = 5.0
    bar_len = int(min(daily_cost / budget, 1.0) * 20)
    bar = f"[{'█' * bar_len}{'░' * (20 - bar_len)}]"
    info(f"Daily cost: ${daily_cost:.2f} / ${budget:.2f} {bar}")

    gpu_lock = rc.get("llm:gpu_lock")
    if gpu_lock:
        warn(f"GPU lock: held by {gpu_lock}")
    else:
        ok("GPU lock: free")

    pass_active = rc.get("contextdna:pass_runner:active")
    if pass_active:
        info("Gold mining: active")
    else:
        dim("  Gold mining: idle")

    mode = rc.get("llm:hybrid_mode") or "fallback-only"
    info(f"Hybrid mode: {mode}")

    pulse_data["vitals"] = {
        "error_rate": round(error_rate, 1), "daily_cost": round(daily_cost, 2),
        "gpu_lock": bool(gpu_lock), "gold_mining": bool(pass_active), "mode": mode
    }

    # ── Phase 2: SQLite reads (graceful skip on lock) ──
    header("[4] INJECTION QUALITY (rolling pass 6)")
    quality_scores = {}
    try:
        from memory.db_utils import connect_wal
        conn = connect_wal(str(ARCHIVE_DB))
        rows = conn.execute("""
            SELECT extracted_content FROM pass_processing_log
            WHERE pass_id = 'eval_webhook_quality'
            ORDER BY processed_at DESC LIMIT 10
        """).fetchall()
        conn.close()

        if rows:
            # Parse scores from content strings (format: "RELEVANCE: 2/3, COMPLETENESS: 1/3, ...")
            for label in ("RELEVANCE", "COMPLETENESS", "FRESHNESS", "ACTIONABILITY"):
                scores = []
                for row in rows:
                    content = row[0] if row[0] else ""
                    import re
                    m = re.search(rf"{label}:\s*(\d)", content, re.IGNORECASE)
                    if m:
                        scores.append(int(m.group(1)))
                if scores:
                    avg = sum(scores) / len(scores)
                    arrow = "↑" if len(scores) > 1 and scores[0] >= scores[-1] else "↓"
                    quality_scores[label.lower()] = avg
                    if avg < 1.5:
                        warn(f"{label:15s} avg {avg:.1f}/3.0 {arrow} ← WEAK")
                    else:
                        info(f"{label:15s} avg {avg:.1f}/3.0 {arrow}")
        else:
            dim("  No pass 6 data available")
    except Exception as e:
        dim(f"  SQLite unavailable: {e}")

    pulse_data["quality"] = quality_scores

    # ── Phase 3: LLM synthesis (classify, 300 tokens, $0) ──
    header("[5] NEUROLOGIST INTERPRETATION")
    pulse_summary = json.dumps(pulse_data, indent=1)

    r = query_local(
        system=(
            "You are Qwen3-4B, the Neurologist — the system's continuous observer. /no_think\n"
            "Interpret these system vitals. Identify the single most important signal.\n"
            "2-3 sentences maximum. Be direct and actionable."
        ),
        prompt=f"PULSE DATA:\n{pulse_summary}",
        profile="classify",
        max_chars=400
    )
    if r["ok"]:
        print(f"\n  {CYAN}{r['content']}{NC}")
        dim(f"  ({r['latency_ms']}ms, $0)")
    else:
        dim("  LLM unavailable — raw data above is sufficient")

    print()


def _gather_challenge_material(topic: str) -> dict:
    """Gather data from 4 sources for corrigibility challenge. Pure Python, no LLM."""
    material = {"evidence_claims": [], "dialogue": [], "mmotw": [], "historian": []}

    # 1. Evidence store — anecdote-grade claims with high confidence (believed but unproven)
    try:
        from memory.db_utils import safe_conn
        obs_db = REPO_ROOT / "memory" / ".observability.db"
        if obs_db.exists():
            with safe_conn(obs_db, timeout=3) as conn:
                conn.row_factory = None  # fetchall returns tuples, not Row
                rows = conn.execute("""
                    SELECT statement, evidence_grade, confidence, outcome_count
                    FROM claim
                    WHERE status = 'active'
                    AND evidence_grade IN ('anecdote', 'opinion')
                    AND confidence >= 0.7
                    ORDER BY confidence DESC LIMIT 10
                """).fetchall()
            for r in rows:
                material["evidence_claims"].append({
                    "claim": r[0][:200], "grade": r[1],
                    "confidence": r[2], "outcomes": r[3]
                })
    except Exception:
        pass

    # 2. Dialogue mirror — recent messages with failure-related keywords
    try:
        import sqlite3
        mirror_db = Path.home() / ".context-dna" / ".dialogue_mirror.db"
        if mirror_db.exists():
            conn = sqlite3.connect(str(mirror_db), timeout=3)
            rows = conn.execute("""
                SELECT role, content, timestamp FROM dialogue_messages
                WHERE content LIKE ? OR content LIKE '%broke%' OR content LIKE '%wrong%'
                    OR content LIKE '%revert%' OR content LIKE '%still broken%'
                    OR content LIKE '%doesn''t work%' OR content LIKE '%failed%'
                ORDER BY timestamp DESC LIMIT 10
            """, (f"%{topic[:30]}%",)).fetchall()
            conn.close()
            for r in rows:
                material["dialogue"].append({
                    "role": r[0], "content": r[1][:200], "when": r[2]
                })
    except Exception:
        pass

    # 3. MMOTW repair SOPs — documented failures with claimed fixes
    try:
        import sqlite3
        sop_db = REPO_ROOT / "memory" / "repair_sops.db"
        if sop_db.exists():
            conn = sqlite3.connect(str(sop_db), timeout=3)
            rows = conn.execute("""
                SELECT title, symptom, root_cause, confidence, outcome
                FROM repair_sops
                WHERE outcome != 'success'
                ORDER BY confidence DESC LIMIT 10
            """).fetchall()
            conn.close()
            for r in rows:
                material["mmotw"].append({
                    "title": r[0], "symptom": (r[1] or "")[:150],
                    "root_cause": (r[2] or "")[:150], "confidence": r[3], "outcome": r[4]
                })
    except Exception:
        pass

    # 4. Session historian — cross-session insights
    try:
        from memory.session_historian import SessionHistorian
        historian = SessionHistorian()
        insights = historian.get_recent_insights(limit=10)
        for i in insights:
            material["historian"].append({
                "type": i.get("type", ""), "content": str(i.get("content", ""))[:200],
                "confidence": i.get("confidence", 0)
            })
    except Exception:
        pass

    return material


def _inject_corrigibility_critical(finding: str, session_id: str = "corrigibility"):
    """Inject a corrigibility critical into S0 via SQLite + Redis. WAL-style append."""
    import sqlite3
    import redis as redis_lib
    from datetime import timezone

    now = datetime.now(timezone.utc).isoformat()
    ts = int(time.time())

    # SQLite (authoritative) — with 48h dedup
    try:
        from memory.db_utils import connect_wal
        conn = connect_wal(str(ARCHIVE_DB))
        existing = conn.execute("""
            SELECT id FROM critical_findings
            WHERE pass_id = 'corrigibility_gate' AND substr(finding, 1, 100) = substr(?, 1, 100)
            AND found_at > datetime(?, '-48 hours') LIMIT 1
        """, (finding, now)).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO critical_findings
                (pass_id, finding, severity, session_id, item_id, found_at,
                 promoted_from_tank, wired_to_anticipation, wired_to_bigpicture)
                VALUES ('corrigibility_gate', ?, 'critical', ?, ?, ?, 0, 1, 1)
            """, (finding, session_id, f"corrigibility_{ts}", now))
            conn.commit()
        conn.close()
    except Exception as e:
        warn(f"SQLite write failed: {e}")
        # Retry once after 1s
        try:
            time.sleep(1)
            conn = connect_wal(str(ARCHIVE_DB))
            conn.execute("""
                INSERT OR IGNORE INTO critical_findings
                (pass_id, finding, severity, session_id, item_id, found_at,
                 promoted_from_tank, wired_to_anticipation, wired_to_bigpicture)
                VALUES ('corrigibility_gate', ?, 'critical', ?, ?, ?, 0, 1, 1)
            """, (finding, session_id, f"corrigibility_{ts}", now))
            conn.commit()
            conn.close()
        except Exception:
            pass

    # Redis (cache — WAL-style append, never bumps existing)
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        entry = json.dumps({
            "pass": "corrigibility_gate", "finding": finding,
            "severity": "critical", "found_at": now
        })
        rc.lpush("contextdna:critical:recent", entry)
        rc.ltrim("contextdna:critical:recent", 0, 49)
        rc.expire("contextdna:critical:recent", 86400)
        # WAL: additive sorted set (never trimmed)
        try:
            import sys as _sys, os as _os
            _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
            from memory.session_gold_passes import _wal_append_critical
            _wal_append_critical(json.loads(entry))
        except Exception:
            pass
    except Exception:
        pass


def cmd_neurologist_challenge(topic: str):
    """Corrigibility Testing Grounds — challenge assumed-true aspects."""
    header("=== Neurologist Corrigibility Challenge ===")
    dim(f"[Qwen3-4B — Skeptic Mode — Topic: {topic[:60]}]")

    # Phase 1: Gather challenge material from 4 sources
    info("Gathering challenge material from 4 data sources...")
    material = _gather_challenge_material(topic)

    total = sum(len(v) for v in material.values())
    info(f"Found: {len(material['evidence_claims'])} evidence claims, "
         f"{len(material['dialogue'])} dialogue entries, "
         f"{len(material['mmotw'])} MMOTW SOPs, "
         f"{len(material['historian'])} historian insights")

    if total == 0:
        warn(f"No challenge material found for topic: {topic}")
        return

    # Phase 2: LLM challenge (extract profile, 1200 tokens, $0)
    material_text = json.dumps(material, indent=1, default=str)
    # Cap material to prevent LLM input overflow (Qwen3-4B context: 32K tokens)
    if len(material_text) > 16000:
        material_text = material_text[:16000] + f"\n[...truncated from {len(material_text)} chars]"

    header("Challenging assumptions...")
    r = query_local(
        system=(
            "You are Qwen3-4B, the Neurologist — the system's corrigibility skeptic. /no_think\n\n"
            "You have access to data no other model can see:\n"
            "- Evidence claims believed true but backed by single outcomes\n"
            "- Dialogue history showing what was promised vs what actually happened\n"
            "- Repair SOPs documenting past failures\n"
            "- Cross-session insights showing recurring patterns\n\n"
            "For each challengeable item, output a JSON array:\n"
            '[{"claim": "the assumed-true thing", "challenge": "why this might be wrong", '
            '"source": "evidence|dialogue|mmotw|historian", '
            '"severity": "critical|worth_testing", '
            '"suggested_test": "how to verify"}]\n\n'
            "Be genuinely skeptical. Surface what others have been too confident about.\n"
            "Maximum 5 challenges. Quality over quantity. Output ONLY the JSON array."
        ),
        prompt=f"CHALLENGE MATERIAL:\n{material_text}\n\nTOPIC: {topic}",
        profile="extract",
        max_chars=8000
    )

    challenges = []
    if r["ok"]:
        # Parse challenges from LLM output
        try:
            parsed = json.loads(r["content"])
            if isinstance(parsed, list):
                challenges = parsed
        except json.JSONDecodeError:
            # Try extracting JSON array from response
            import re
            m = re.search(r'\[[\s\S]*\]', r["content"])
            if m:
                try:
                    challenges = json.loads(m.group(0))
                except Exception:
                    pass

        if not challenges:
            # Fallback: treat entire response as a single challenge
            if len(r["content"].strip()) > 20:
                challenges = [{"claim": topic, "challenge": r["content"][:2000],
                              "source": "mixed", "severity": "worth_testing",
                              "suggested_test": "Manual review needed"}]
    else:
        warn("Qwen3-4B unavailable — saving raw material only")

    # Phase 3: Display and inject
    ts = int(time.time())
    injected = 0

    if challenges:
        header(f"Corrigibility Challenges ({len(challenges)})")
        for i, ch in enumerate(challenges, 1):
            severity = ch.get("severity", "worth_testing")
            claim = ch.get("claim", "?")[:300]
            challenge = ch.get("challenge", "?")[:1000]
            source = ch.get("source", "?")
            test = ch.get("suggested_test", "?")[:500]

            color = RED if severity == "critical" else YELLOW
            print(f"\n  {color}[{severity.upper()}]{NC} {claim}")
            print(f"  {CYAN}Challenge:{NC} {challenge}")
            dim(f"  Source: {source} | Test: {test}")

            # Phase 4: Inject criticals into S0
            if severity == "critical":
                finding = f"CORRIGIBILITY: {claim} — {challenge}"
                _inject_corrigibility_critical(finding)
                injected += 1
                ok(f"Injected into S0 as critical finding")
    else:
        info("Neurologist found no concerns — topic appears well-supported")

    # Phase 5: Save full analysis to WAL
    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    result = {
        "timestamp": datetime.now().isoformat(),
        "topic": topic,
        "material_counts": {k: len(v) for k, v in material.items()},
        "challenges": challenges,
        "injected_to_s0": injected,
        "llm_response": r.get("content", "") if r["ok"] else "LLM unavailable"
    }
    out_path = results_dir / f"corrigibility_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    # Summary
    print()
    header("Summary")
    info(f"Challenges found: {len(challenges)}")
    if injected > 0:
        ok(f"Criticals injected into S0: {injected} (Atlas MUST test these)")
    info(f"Full analysis: {out_path}")
    dim(f"LLM: {r['latency_ms']}ms, $0")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# AUTONOMOUS A/B COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

def cmd_ab_validate(description: str):
    """Quick validation of a recent fix — fast feedback loop bypassing full A/B ceremony.

    Steps:
    1. Gains gate (no regressions?)
    2. Neurologist risk/benefit assessment
    3. Cardiologist evidence cross-examination
    4. Auto-verdict: KEEP if all pass, FLAG if any surgeon dissents
    """
    import redis as redis_lib
    import subprocess

    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable")
        return

    header("=== Quick Fix Validation ===")
    info(f"Fix: {description[:120]}")
    ts = int(time.time())
    verdicts = {}  # surgeon -> {vote, reason}

    # --- Step 1: Gains gate (informational — pre-existing failures don't block) ---
    header("Step 1: Gains Gate (regression check)")
    repo = Path(__file__).parent.parent
    try:
        result = subprocess.run(
            [str(repo / "scripts" / "gains-gate.sh")],
            capture_output=True, text=True, timeout=45, cwd=str(repo)
        )
        gate_passed = result.returncode == 0
        # Extract summary and failure lines
        failures = []
        for line in result.stdout.splitlines():
            if "Checks:" in line or "GATE:" in line or "All" in line:
                print(f"  {line.strip()}")
            if "CRITICAL" in line and "✗" in line:
                failures.append(line.strip())
        if gate_passed:
            ok("Gains gate: PASSED — no regressions")
            verdicts["gains_gate"] = {"vote": "keep", "reason": "All checks passed"}
        else:
            # Report failures but don't auto-reject — let surgeons decide if pre-existing
            warn(f"Gains gate: {len(failures)} critical check(s) failing")
            for f in failures:
                dim(f"  {f}")
            verdicts["gains_gate"] = {
                "vote": "flag",
                "reason": f"{len(failures)} failing checks (may be pre-existing)",
                "failures": [f[:100] for f in failures],
            }
    except subprocess.TimeoutExpired:
        warn("Gains gate timed out (45s)")
        verdicts["gains_gate"] = {"vote": "skip", "reason": "Timeout"}
    except Exception as e:
        warn(f"Gains gate error: {e}")
        verdicts["gains_gate"] = {"vote": "skip", "reason": str(e)}

    # --- Step 2: Neurologist risk/benefit ---
    header("Step 2: Neurologist Assessment (risk/benefit)")
    neuro_system = (
        "You are Qwen3-4B, the Neurologist — system safety skeptic. /no_think\n"
        "Evaluate this fix for risk and benefit. Reply JSON ONLY:\n"
        '{"vote":"keep"|"flag"|"revert","risk":1-5,"benefit":1-5,"reason":"one sentence"}'
    )
    neuro_prompt = (
        f"FIX APPLIED: {description}\n\n"
        "Assess: Does this fix introduce new risks? Is the benefit clear? "
        "vote=keep if safe+beneficial, flag if uncertain, revert if risky."
    )
    r_neuro = query_local(neuro_system, neuro_prompt, profile="classify", max_chars=300)
    if r_neuro["ok"]:
        content = r_neuro["content"].strip()
        print(f"  {content[:500]}")
        try:
            # Try to parse JSON vote
            import re
            json_match = re.search(r'\{[^}]+\}', content)
            if json_match:
                parsed = json.loads(json_match.group())
                verdicts["neurologist"] = parsed
            else:
                verdicts["neurologist"] = {"vote": "keep", "reason": content[:500]}
        except Exception:
            verdicts["neurologist"] = {"vote": "keep", "reason": content[:500]}
    else:
        dim("  Neurologist unavailable — skipping")
        verdicts["neurologist"] = {"vote": "skip", "reason": "LLM offline"}

    # --- Step 3: Cardiologist evidence check ---
    header("Step 3: Cardiologist Analysis (evidence-based)")
    spent, remaining, budget = _get_research_budget(rc)
    if remaining < 0.01:
        warn(f"Research budget exhausted — skipping Cardiologist")
        verdicts["cardiologist"] = {"vote": "skip", "reason": "Budget exhausted"}
    else:
        # Gather evidence related to the fix
        evidence = _get_evidence_snapshot(description, limit=20)

        cardio_system = (
            "You are GPT-4.1-mini, the Cardiologist — evidence-based validator.\n"
            "Given a fix description and related evidence, determine if this fix should be kept.\n"
            "Reply JSON ONLY:\n"
            '{"vote":"keep"|"flag"|"revert","confidence":0.0-1.0,"reason":"one sentence","evidence_gap":"what evidence is missing, if any"}'
        )
        cardio_prompt = (
            f"FIX: {description}\n\n"
            f"RELATED EVIDENCE:\n{evidence['evidence_text'][:2000]}\n\n"
            "Should this fix be kept based on available evidence? "
            "vote=keep if evidence supports it, flag if insufficient evidence, revert if evidence contradicts."
        )
        r_cardio = query_remote(cardio_system, cardio_prompt, model="gpt-4.1-mini", max_tokens=512)
        if r_cardio["ok"]:
            content = r_cardio["content"].strip()
            print(f"  {content[:500]}")
            _track_research_cost(rc, r_cardio.get("cost_usd", 0), f"ab-validate:{description[:30]}")
            try:
                import re
                json_match = re.search(r'\{[^}]+\}', content)
                if json_match:
                    parsed = json.loads(json_match.group())
                    verdicts["cardiologist"] = parsed
                else:
                    verdicts["cardiologist"] = {"vote": "keep", "reason": content[:500]}
            except Exception:
                verdicts["cardiologist"] = {"vote": "keep", "reason": content[:500]}
        else:
            warn(f"Cardiologist error: {r_cardio['content'][:100]}")
            verdicts["cardiologist"] = {"vote": "skip", "reason": r_cardio["content"][:100]}

    # --- Step 4: Synthesize verdict (ALL 3 surgeons must agree to keep) ---
    header("Synthesis")
    votes = {k: v.get("vote", "skip") for k, v in verdicts.items()}
    reverts = sum(1 for v in votes.values() if v == "revert")
    flags = sum(1 for v in votes.values() if v == "flag")
    keeps = sum(1 for v in votes.values() if v == "keep")
    skips = sum(1 for v in votes.values() if v == "skip")

    for surgeon, v in verdicts.items():
        vote = v.get("vote", "skip")
        reason = v.get("reason", "")
        color = GREEN if vote == "keep" else (RED if vote == "revert" else YELLOW)
        print(f"  {color}[{vote:6s}]{NC} {surgeon}: {reason[:100]}")

    # Consensus required: neurologist + cardiologist must BOTH vote keep
    # Gains gate is informational (pre-existing failures don't block)
    required_surgeons = {"neurologist", "cardiologist"}
    missing = required_surgeons - set(verdicts.keys())
    surgeon_skips = [s for s in required_surgeons if verdicts.get(s, {}).get("vote") == "skip"]
    surgeon_keeps = [s for s in required_surgeons if verdicts.get(s, {}).get("vote") == "keep"]
    surgeon_reverts = [s for s in required_surgeons if verdicts.get(s, {}).get("vote") == "revert"]

    if surgeon_reverts:
        final = "revert"
        print(f"\n  {RED}{BOLD}VERDICT: REVERT{NC} — {', '.join(surgeon_reverts)} recommend reverting")
    elif missing or surgeon_skips:
        final = "blocked"
        offline = list(missing) + surgeon_skips
        print(f"\n  {RED}{BOLD}VERDICT: BLOCKED{NC} — all 3 surgeons required for consensus")
        print(f"  Offline/skipped: {', '.join(offline)}")
        print(f"  Cannot auto-keep without full 3-surgeon agreement.")
    elif len(surgeon_keeps) == len(required_surgeons):
        final = "keep"
        gate_note = ""
        if verdicts.get("gains_gate", {}).get("vote") == "flag":
            gate_note = f" (gains gate has pre-existing issues — review separately)"
        print(f"\n  {GREEN}{BOLD}VERDICT: KEEP{NC} — 3-surgeon consensus achieved{gate_note}")
    else:
        final = "flag"
        flaggers = [s for s in required_surgeons if verdicts.get(s, {}).get("vote") == "flag"]
        print(f"\n  {YELLOW}{BOLD}VERDICT: FLAG FOR REVIEW{NC} — {', '.join(flaggers)} uncertain")

    _save_validation(rc, ts, description, final, verdicts)


def _save_validation(rc, ts: int, description: str, final_verdict: str, verdicts: dict):
    """Save validation result to Redis + WAL."""
    data = {
        "timestamp": datetime.now().isoformat(),
        "description": description,
        "verdict": final_verdict,
        "surgeon_votes": verdicts,
    }
    rc.set(f"surgery:ab_validate:{ts}", json.dumps(data), ex=86400 * 30)
    rc.lpush("surgery:ab_tests:history", json.dumps({
        "ts": ts, "claim": description[:80], "status": f"validated_{final_verdict}",
        "type": "quick_validation",
    }))
    rc.ltrim("surgery:ab_tests:history", 0, 49)

    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    out_path = results_dir / f"ab_validate_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    info(f"Validation saved: {out_path}")

    # Record to evidence store if kept
    if final_verdict == "keep":
        try:
            from memory.sqlite_storage import get_sqlite_storage
            storage = get_sqlite_storage()
            storage.store_learning({
                "type": "win",
                "title": f"Fix validated: {description[:150]}",
                "content": (
                    f"Quick validation passed.\n"
                    f"Votes: {json.dumps({k: v.get('vote','?') for k,v in verdicts.items()})}\n"
                    f"Reasons: {json.dumps({k: v.get('reason','') for k,v in verdicts.items()})}"
                ),
                "tags": ["ab_validate", "fix_validated", "surgery_team"],
                "area": "ab_testing",
            })
            ok("Fix recorded as validated win in evidence store")
        except Exception as e:
            warn(f"Could not record to evidence store: {e}")


def cmd_ab_veto(test_id_and_reason: str):
    """Veto an autonomous A/B test (during grace or active)."""
    parts = test_id_and_reason.split(None, 1)
    test_id = parts[0]
    reason = parts[1] if len(parts) > 1 else "manual veto via surgery-team"

    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from memory.ab_autonomous import veto_test
        success = veto_test(test_id, reason)
        if success:
            ok(f"Test {test_id} VETOED: {reason}")
        else:
            fail(f"Veto failed — test {test_id} not found or already concluded")
    except Exception as e:
        fail(f"Veto error: {e}")


def cmd_ab_queue():
    """Show autonomous A/B test queue and status."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from memory.ab_autonomous import get_status
        s = get_status()

        header("=== Autonomous A/B Testing ===")
        info(f"Budget: ${s['budget_remaining']:.2f} remaining (${s['budget_spent_today']:.4f} spent today)")

        active = s.get("active_test")
        if active:
            header("Active Test")
            print(f"  {YELLOW}ID:{NC} {active['test_id']}")
            print(f"  {YELLOW}Status:{NC} {active['status']}")
            print(f"  {YELLOW}Hypothesis:{NC} {active['hypothesis'][:80]}")
            print(f"  {YELLOW}Config:{NC} {active['config_type']}.{active['config_key']}")
            if active['status'] == 'grace':
                remaining = max(0, active['grace_until'] - time.time())
                print(f"  {YELLOW}Grace remaining:{NC} {int(remaining/60)}m {int(remaining%60)}s")
        else:
            dim("  No active test")

        if s.get("queued_count", 0) > 0:
            info(f"Queued: {s['queued_count']} tests waiting")

        history = s.get("history", [])
        if history:
            header("Recent History")
            for h in history[:5]:
                rev = f" {RED}[REVERTED]{NC}" if h.get("reverted") else ""
                status_color = GREEN if h['status'] == 'concluded' else (RED if h['status'] == 'reverted' else YELLOW)
                print(f"  {status_color}{h['status']:12s}{NC} {h['test_id']}  {h.get('hypothesis', '')[:50]}{rev}")
    except Exception as e:
        fail(f"Queue error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# CARDIOLOGIST CROSS-EXAMINATION PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def cmd_cardio_review(topic: str = ""):
    """Process critical cardiologist findings through 3-agent git history
    cross-examination, then 3-surgeon corrigibility review.

    Pipeline: Cardiologist findings → 3 parallel git agents → 3-surgeon review
    Each surgeon must examine opposing views before forming conclusions.
    """
    import redis as redis_lib
    header("Cardiologist Cross-Examination Pipeline")

    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception as e:
        fail(f"Redis unavailable: {e}")
        return

    # Step 1: Gather critical cardiologist findings
    findings = []
    raw_items = rc.lrange("quality:critical_notifications", 0, -1)
    for raw in raw_items:
        try:
            findings.append(json.loads(raw))
        except Exception:
            continue

    if not findings:
        # Also check cardiologist_findings for high-severity
        for raw in rc.lrange("quality:cardiologist_findings", 0, 19):
            try:
                f = json.loads(raw)
                if f.get("severity") == "critical":
                    findings.append(f)
            except Exception:
                continue

    if not findings:
        info("No critical cardiologist findings to review")
        return

    info(f"Found {len(findings)} critical finding(s) to cross-examine")

    # Step 2: Gather evidence snapshot for all findings
    all_dims = set()
    all_tasks = set()
    for f in findings:
        all_dims.update(f.get("dimensions", {}).keys())
        all_tasks.add(f.get("task_type", "unknown"))

    evidence = _get_evidence_snapshot(
        f"webhook quality {' '.join(all_dims)} {' '.join(all_tasks)}", limit=20
    )

    # Step 3: Three parallel git history investigations
    # Agent 1: Search git log for changes to webhook/injection code
    # Agent 2: Search for prior quality degradation fixes
    # Agent 3: Search for configuration/data changes that might cause regression
    header("Phase 1: Git History Cross-Examination (3 agents)")
    git_reports = _cardio_git_cross_exam(findings, evidence)

    # Step 4: 3-surgeon corrigibility review
    header("Phase 2: 3-Surgeon Corrigibility Review")
    consensus = _cardio_surgeon_review(findings, evidence, git_reports)

    # Step 5: Output results
    header("Results")
    _cardio_present_results(consensus, findings, rc)


def _cardio_git_cross_exam(findings: list, evidence: dict) -> dict:
    """3 parallel git history agents cross-examine cardiologist claims."""
    import subprocess

    findings_text = "\n".join(
        f"- [{f.get('severity','?')}] dims={f.get('dimensions',{})} "
        f"task={f.get('task_type','?')}: {f.get('diagnosis','')[:200]}"
        for f in findings
    )

    reports = {}

    # Agent 1: Recent webhook/injection code changes
    info("Agent 1: Searching recent webhook/injection code changes...")
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since=7 days ago", "--all",
             "--", "memory/persistent_hook_structure.py",
             "memory/webhook_message_builders.py", "memory/webhook_batch_helper.py",
             "memory/anticipation_engine.py", "memory/session_gold_passes.py"],
            capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT)
        )
        recent_changes = result.stdout.strip() or "(no recent changes)"

        # Also get diff stats for these files
        diff_result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~10..HEAD", "--",
             "memory/persistent_hook_structure.py",
             "memory/webhook_message_builders.py"],
            capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT)
        )
        diff_stats = diff_result.stdout.strip() or "(no diff)"

        reports["code_changes"] = f"Recent commits:\n{recent_changes}\n\nDiff stats:\n{diff_stats}"
        ok(f"Agent 1: Found {len(recent_changes.splitlines())} recent commits")
    except Exception as e:
        reports["code_changes"] = f"(error: {e})"
        fail(f"Agent 1: {e}")

    # Agent 2: Prior quality degradation fixes in git history
    info("Agent 2: Searching for prior quality degradation fixes...")
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--all", "-20",
             "--grep=quality", "--grep=degrad", "--grep=webhook",
             "--all-match"],
            capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT)
        )
        quality_fixes = result.stdout.strip()
        if not quality_fixes:
            # Broader search
            result = subprocess.run(
                ["git", "log", "--oneline", "--all", "-20", "--grep=webhook"],
                capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT)
            )
            quality_fixes = result.stdout.strip() or "(no matches)"
        reports["prior_fixes"] = f"Prior quality/webhook fixes:\n{quality_fixes}"
        ok(f"Agent 2: Found {len(quality_fixes.splitlines())} relevant commits")
    except Exception as e:
        reports["prior_fixes"] = f"(error: {e})"
        fail(f"Agent 2: {e}")

    # Agent 3: Configuration and data flow changes
    info("Agent 3: Searching for config/scheduler/data flow changes...")
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since=14 days ago", "--all",
             "--", "memory/lite_scheduler.py", "memory/llm_priority_queue.py",
             "memory/observability_store.py", "scripts/atlas-ops.sh"],
            capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT)
        )
        config_changes = result.stdout.strip() or "(no recent changes)"
        reports["config_changes"] = f"Config/scheduler changes:\n{config_changes}"
        ok(f"Agent 3: Found {len(config_changes.splitlines())} config commits")
    except Exception as e:
        reports["config_changes"] = f"(error: {e})"
        fail(f"Agent 3: {e}")

    return reports


def _cardio_surgeon_review(findings: list, evidence: dict, git_reports: dict) -> dict:
    """3-surgeon corrigibility review with opposing-view testing."""
    total_cost = 0.0

    findings_text = "\n".join(
        f"- [{f.get('severity','?')}] dims={f.get('dimensions',{})} "
        f"task={f.get('task_type','?')}: {f.get('diagnosis','')[:300]}"
        for f in findings
    )

    git_text = "\n\n".join(f"### {k}\n{v}" for k, v in git_reports.items())
    evidence_text = evidence.get("evidence_text", "(no evidence)")[:2000]

    base_context = (
        f"## Cardiologist Findings (Critical)\n{findings_text}\n\n"
        f"## Git History Evidence\n{git_text}\n\n"
        f"## Evidence Store\n{evidence_text}"
    )

    # Surgeon 1: Cardiologist (GPT-4.1-mini) — validate own findings against git evidence
    info("Surgeon 1 (Cardiologist/GPT-4.1-mini): Validating findings against git history...")
    cardio_system = (
        "You are GPT-4.1-mini, the Cardiologist in the Surgery Team of 3.\n"
        "Review your own critical findings against git history evidence.\n"
        "For each finding, determine:\n"
        "1. CONFIRMED: git evidence supports the diagnosis\n"
        "2. REVISED: git evidence suggests a different root cause\n"
        "3. RETRACTED: git evidence contradicts the diagnosis\n\n"
        "Also generate HYPOTHESES — testable predictions that would verify or falsify.\n"
        "Output JSON: {\"verdicts\": [{\"finding\": \"...\", \"verdict\": \"CONFIRMED|REVISED|RETRACTED\", "
        "\"evidence_cited\": \"...\", \"hypothesis\": \"if X then Y\", \"confidence\": 0.X}], "
        "\"overall_assessment\": \"...\", \"recommended_actions\": [\"...\"]}"
    )
    r_cardio = query_remote(cardio_system, base_context, max_tokens=2048)
    if r_cardio.get("ok"):
        total_cost += r_cardio.get("cost_usd", 0)
        ok(f"Cardiologist: {r_cardio.get('latency_ms', 0)}ms, ${r_cardio.get('cost_usd', 0):.4f}")
    else:
        fail(f"Cardiologist: {r_cardio.get('content', 'failed')[:100]}")

    # Surgeon 2: Neurologist (Qwen3-4B) — independent analysis, look for blind spots
    info("Surgeon 2 (Neurologist/Qwen3-4B): Independent analysis for blind spots...")
    neuro_system = (
        "You are Qwen3-4B, the Neurologist in the Surgery Team. /no_think\n"
        "Independently analyze these findings. Focus on:\n"
        "1. BLIND SPOTS: What did the Cardiologist miss?\n"
        "2. ALTERNATIVE CAUSES: What else could explain the degradation?\n"
        "3. MEASUREMENT: Can we reliably detect if the fix works?\n\n"
        "Score each finding 0-3 on: accuracy, completeness, actionability.\n"
        "Output JSON: {\"blind_spots\": [\"...\"], \"alternative_causes\": [\"...\"], "
        "\"scores\": [{\"finding\": \"...\", \"accuracy\": N, \"completeness\": N, \"actionability\": N}], "
        "\"dissent\": [\"...\"], \"measurement_plan\": \"...\"}"
    )
    r_neuro = query_local(neuro_system, base_context, profile="extract_deep", max_chars=3000)
    if r_neuro.get("ok"):
        ok(f"Neurologist: {r_neuro.get('latency_ms', 0)}ms")
    else:
        fail(f"Neurologist: {r_neuro.get('content', 'failed')[:100]}")

    # Step: Check for dissent — if any surgeon disagrees, test opposing view
    cardio_content = r_cardio.get("content", "")
    neuro_content = r_neuro.get("content", "")

    has_dissent = False
    if "RETRACTED" in cardio_content or "dissent" in neuro_content.lower():
        has_dissent = True

    # Parse neurologist scores for blocking conditions
    neuro_json = _safe_json_parse(neuro_content)
    dissent_items = neuro_json.get("dissent", []) if neuro_json else []
    if dissent_items:
        has_dissent = True

    opposing_view_result = None
    if has_dissent:
        # CORRIGIBILITY: Test the opposing view before forming conclusions
        info("DISSENT DETECTED — Testing opposing view (corrigibility protocol)...")
        opposing_system = (
            "You are GPT-4.1, testing an opposing view in the Surgery Team.\n"
            "One surgeon disagrees with the diagnosis. Your job is to STEELMAN "
            "the opposing view — make the STRONGEST possible case that the "
            "dissenting surgeon is correct.\n\n"
            "Then evaluate: Is the opposing view more supported by evidence than "
            "the original diagnosis? Score: 0-10 (10 = opposing view clearly correct).\n"
            "Output JSON: {\"steelman_argument\": \"...\", \"opposing_evidence\": [\"...\"], "
            "\"score\": N, \"recommendation\": \"accept_original|accept_opposing|needs_more_data\"}"
        )
        opposing_prompt = (
            f"## Original Diagnosis (Cardiologist)\n{cardio_content[:4000]}\n\n"
            f"## Dissent (Neurologist)\n{neuro_content[:4000]}\n\n"
            "Steelman the opposing view. Which position is better supported?"
        )
        r_opposing = query_remote(opposing_system, opposing_prompt,
                                   model="gpt-4.1", max_tokens=2048)
        if r_opposing.get("ok"):
            total_cost += r_opposing.get("cost_usd", 0)
            opposing_view_result = r_opposing.get("content", "")
            ok(f"Opposing view tested: {r_opposing.get('latency_ms', 0)}ms, "
               f"${r_opposing.get('cost_usd', 0):.4f}")
        else:
            fail(f"Opposing view: {r_opposing.get('content', 'failed')[:100]}")

    # Surgeon 3: Atlas — synthesize weighted consensus
    info("Surgeon 3 (Atlas): Synthesizing weighted consensus...")
    atlas_consensus = _cardio_atlas_synthesize(
        cardio_content, neuro_content, opposing_view_result,
        findings, has_dissent
    )
    atlas_consensus["total_cost_usd"] = total_cost

    return atlas_consensus


def _cardio_atlas_synthesize(cardio_report: str, neuro_report: str,
                              opposing_result: str | None,
                              findings: list, has_dissent: bool) -> dict:
    """Atlas synthesizes weighted consensus from all surgeon reports."""
    # Parse cardiologist verdicts
    cardio_json = _safe_json_parse(cardio_report)
    neuro_json = _safe_json_parse(neuro_report)
    opposing_json = _safe_json_parse(opposing_result) if opposing_result else None

    verdicts = cardio_json.get("verdicts", []) if cardio_json else []
    blind_spots = neuro_json.get("blind_spots", []) if neuro_json else []
    alt_causes = neuro_json.get("alternative_causes", []) if neuro_json else []
    dissent = neuro_json.get("dissent", []) if neuro_json else []

    # Confidence weighting
    cardio_confs = [v.get("confidence", 0.5) for v in verdicts]
    avg_cardio_conf = sum(cardio_confs) / len(cardio_confs) if cardio_confs else 0.5

    neuro_scores = neuro_json.get("scores", []) if neuro_json else []
    avg_neuro_accuracy = 0.5
    if neuro_scores:
        accuracies = [s.get("accuracy", 1.5) / 3.0 for s in neuro_scores]
        avg_neuro_accuracy = sum(accuracies) / len(accuracies)

    # If opposing view was tested, factor in
    opposing_score = 5  # neutral
    if opposing_json:
        opposing_score = opposing_json.get("score", 5)

    # Weighted confidence: cardio 40%, neuro 30%, opposing 30% (if tested)
    if opposing_json:
        atlas_confidence = (
            avg_cardio_conf * 0.4 +
            avg_neuro_accuracy * 0.3 +
            (1 - opposing_score / 10) * 0.3  # Invert: high opposing = low confidence in original
        )
    else:
        atlas_confidence = avg_cardio_conf * 0.55 + avg_neuro_accuracy * 0.45

    atlas_confidence = min(0.95, max(0.1, atlas_confidence))

    # Determine consensus status
    confirmed_count = sum(1 for v in verdicts if v.get("verdict") == "CONFIRMED")
    retracted_count = sum(1 for v in verdicts if v.get("verdict") == "RETRACTED")

    if retracted_count > confirmed_count:
        status = "findings_retracted"
    elif has_dissent and opposing_score >= 7:
        status = "opposing_view_stronger"
    elif has_dissent:
        status = "approved_with_caveats"
    elif confirmed_count == len(verdicts) and not blind_spots:
        status = "fully_confirmed"
    else:
        status = "partially_confirmed"

    # Collect all recommended actions
    actions = cardio_json.get("recommended_actions", []) if cardio_json else []
    if opposing_json and opposing_json.get("recommendation") == "accept_opposing":
        actions.insert(0, "PRIORITY: Accept opposing view — investigate alternative root cause")

    return {
        "consensus_status": status,
        "confidence": round(atlas_confidence, 3),
        "has_dissent": has_dissent,
        "findings_examined": len(findings),
        "verdicts": {
            "confirmed": confirmed_count,
            "retracted": retracted_count,
            "revised": sum(1 for v in verdicts if v.get("verdict") == "REVISED"),
        },
        "blind_spots": blind_spots[:5],
        "alternative_causes": alt_causes[:5],
        "dissent": dissent[:5],
        "opposing_view": {
            "tested": opposing_json is not None,
            "score": opposing_score,
            "recommendation": opposing_json.get("recommendation", "n/a") if opposing_json else "n/a",
        },
        "recommended_actions": actions[:10],
        "cardiologist_report": cardio_report[:500],
        "neurologist_report": neuro_report[:500],
        "opposing_report": (opposing_result or "")[:500],
    }


def _cardio_present_results(consensus: dict, findings: list, rc):
    """Present cross-examination results and store for Atlas retrieval."""
    status = consensus.get("consensus_status", "unknown")
    conf = consensus.get("confidence", 0)
    verdicts = consensus.get("verdicts", {})

    status_color = {
        "fully_confirmed": RED,
        "partially_confirmed": YELLOW,
        "approved_with_caveats": YELLOW,
        "findings_retracted": GREEN,
        "opposing_view_stronger": CYAN,
    }.get(status, DIM)

    print(f"\n  Status: {status_color}{BOLD}{status}{NC}")
    print(f"  Confidence: {conf:.0%}")
    print(f"  Verdicts: {GREEN}{verdicts.get('confirmed',0)} confirmed{NC}, "
          f"{YELLOW}{verdicts.get('revised',0)} revised{NC}, "
          f"{RED}{verdicts.get('retracted',0)} retracted{NC}")

    if consensus.get("has_dissent"):
        print(f"\n  {YELLOW}DISSENT:{NC}")
        for d in consensus.get("dissent", []):
            print(f"    - {d}")
        ov = consensus.get("opposing_view", {})
        if ov.get("tested"):
            print(f"  Opposing view score: {ov['score']}/10 → {ov['recommendation']}")

    if consensus.get("blind_spots"):
        print(f"\n  {CYAN}BLIND SPOTS:{NC}")
        for bs in consensus["blind_spots"]:
            print(f"    - {bs}")

    if consensus.get("recommended_actions"):
        print(f"\n  {BOLD}RECOMMENDED ACTIONS:{NC}")
        for i, a in enumerate(consensus["recommended_actions"], 1):
            print(f"    {i}. {a}")

    cost = consensus.get("total_cost_usd", 0)
    print(f"\n  Total cost: ${cost:.4f}")

    # Store results in Redis for Atlas retrieval
    try:
        result_payload = {
            "timestamp": datetime.now().isoformat(),
            "consensus": consensus,
            "findings_count": len(findings),
        }
        rc.setex("quality:cross_exam_result", 86400,
                  json.dumps(result_payload, default=str))
        ok("Results stored in Redis (quality:cross_exam_result, 24h TTL)")
    except Exception as e:
        warn(f"Redis store: {e}")

    # Write to WAL for Atlas file-based retrieval
    try:
        wal_dir = Path("/tmp/atlas-agent-results")
        wal_dir.mkdir(exist_ok=True)
        wal_file = wal_dir / f"cardio_review_{int(time.time())}.json"
        with open(wal_file, "w") as f:
            json.dump({"consensus": consensus, "findings": findings}, f, indent=2, default=str)
        ok(f"WAL written: {wal_file}")
    except Exception as e:
        warn(f"WAL write: {e}")


def _safe_json_parse(text: str | None) -> dict | None:
    """Try to parse JSON from LLM output, handling markdown fences."""
    if not text:
        return None
    # Strip markdown code fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last fence lines
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object in text
        import re
        m = re.search(r'\{[\s\S]*\}', cleaned)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CARDIOLOGIST EVIDENCE RE-VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def _get_working_days(project_dirs: list[str], since_days: int = 365) -> dict:
    """Count working days per project using git log.

    Returns {project_dir: {total_working_days: int, last_active: str, active_periods: [...]}}.
    A 'working day' = a calendar date with at least one commit touching that project dir.
    Shelved projects accumulate 0 working days while inactive.
    """
    import subprocess
    results = {}
    since = f"--since={since_days} days ago"

    for proj_dir in project_dirs:
        full_path = REPO_ROOT / proj_dir
        if not full_path.exists():
            continue
        try:
            # Get all commit dates touching this directory
            out = subprocess.run(
                ["git", "log", since, "--format=%aI", "--", proj_dir],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=10
            )
            if out.returncode != 0:
                results[proj_dir] = {"total_working_days": 0, "last_active": "unknown", "active_periods": []}
                continue

            dates = set()
            for line in out.stdout.strip().split("\n"):
                if line:
                    # Extract YYYY-MM-DD from ISO format
                    dates.add(line[:10])

            sorted_dates = sorted(dates)
            # Build active periods (contiguous runs with <=7d gaps)
            periods = []
            if sorted_dates:
                period_start = sorted_dates[0]
                period_end = sorted_dates[0]
                for d in sorted_dates[1:]:
                    from datetime import datetime as dt_cls
                    prev = dt_cls.strptime(period_end, "%Y-%m-%d")
                    curr = dt_cls.strptime(d, "%Y-%m-%d")
                    if (curr - prev).days <= 7:
                        period_end = d
                    else:
                        periods.append({"start": period_start, "end": period_end})
                        period_start = d
                        period_end = d
                periods.append({"start": period_start, "end": period_end})

            results[proj_dir] = {
                "total_working_days": len(dates),
                "last_active": sorted_dates[-1] if sorted_dates else "never",
                "active_periods": periods[-5:],  # Last 5 periods max
            }
        except Exception as e:
            results[proj_dir] = {"total_working_days": 0, "last_active": f"error: {e}", "active_periods": []}

    return results


def _get_all_evidence_for_reverify(limit: int = 50) -> list[dict]:
    """Get high-grade evidence (correlation+) from learnings + claims for re-verification."""
    evidence = []

    # 1. Learnings from SQLite
    try:
        from memory.sqlite_storage import get_sqlite_storage
        storage = get_sqlite_storage()
        # Get all learnings, sorted by created_at desc
        rows = storage.conn.execute("""
            SELECT * FROM learnings ORDER BY created_at DESC LIMIT ?
        """, (limit * 2,)).fetchall()
        for r in rows:
            d = storage._row_to_dict(r)
            d["_source_type"] = "learning"
            evidence.append(d)
    except Exception as e:
        pass

    # 2. Claims from observability store (focus on correlation+ grades)
    try:
        from memory.observability_store import ObservabilityStore
        obs = ObservabilityStore(mode="auto")
        high_grades = ("correlation", "case_series", "cohort", "validated", "meta")
        for status in ("active", "trusted", "quarantined"):
            claims = obs.get_claims_by_status(status, limit=limit)
            for c in claims:
                grade = c.get("evidence_grade", "anecdote")
                if grade in high_grades:
                    c["_source_type"] = "claim"
                    c["_status"] = status
                    evidence.append(c)
    except Exception:
        pass

    return evidence[:limit]


def cmd_cardio_reverify(scope: str):
    """Cardiologist re-verifies evidence grades against current codebase state.

    Uses 'working days' logic — shelved projects don't decay.
    Examines codebases, evidences, documents. Reclassifies grades.
    Surfaces criticals to Redis + file output for LLM/Atlas visibility.
    Can trigger A/B test design on high-value findings.

    Usage: ./scripts/surgery-team.py cardio-reverify "context-dna"
           ./scripts/surgery-team.py cardio-reverify "all"
           ./scripts/surgery-team.py cardio-reverify "er-simulator"
    """
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable — needed for criticals + budget")
        return

    spent, remaining, budget = _get_research_budget(rc)
    header(f"=== Cardiologist Evidence Re-Verification ===")
    info(f"Scope: {scope}")
    info(f"Budget: ${spent:.4f} spent / ${budget:.2f} limit (${remaining:.4f} remaining)")

    if remaining < 0.10:
        fail(f"Insufficient budget for re-verification (need ~$0.10, have ${remaining:.4f})")
        return

    # ─── Phase 1: Working Days Analysis ───
    header("Phase 1: Working Days Analysis")

    # Detect project directories to analyze
    project_dirs = []
    if scope.lower() == "all":
        # All top-level dirs that look like projects
        for d in sorted(REPO_ROOT.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_"):
                if (d / ".git").exists() or any(d.glob("*.py")) or any(d.glob("*.md")):
                    project_dirs.append(d.name)
        # Also include memory/ and scripts/ as pseudo-projects
        project_dirs.extend(["memory", "scripts"])
    else:
        # Specific project — also include memory/ for cross-reference
        project_dirs = [scope, "memory", "scripts"]

    project_dirs = list(set(project_dirs))
    working_days = _get_working_days(project_dirs)

    for proj, wd in sorted(working_days.items(), key=lambda x: -x[1]["total_working_days"]):
        status = f"{wd['total_working_days']}d active"
        last = wd["last_active"]
        if wd["total_working_days"] == 0:
            dim(f"  {proj}: no commits (shelved)")
        else:
            ok(f"{proj}: {status}, last: {last}")

    # ─── Phase 2: Gather Evidence ───
    header("Phase 2: Gathering Evidence for Re-Verification")
    evidence = _get_all_evidence_for_reverify(limit=50)
    n_learnings = sum(1 for e in evidence if e.get("_source_type") == "learning")
    n_claims = sum(1 for e in evidence if e.get("_source_type") == "claim")
    ok(f"Gathered: {n_learnings} learnings + {n_claims} claims")

    if not evidence:
        warn("No evidence found — nothing to re-verify")
        return

    # ─── Phase 3: Read Related Code (scope-aware) ───
    header("Phase 3: Sampling Current Codebase")
    code_samples = {}
    sample_dirs = [scope] if scope.lower() != "all" else project_dirs[:5]

    CODE_EXTENSIONS = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".sh", ".bash",
        ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini",
        ".sql", ".html", ".css", ".svelte", ".vue",
        ".swift", ".kt", ".dart", ".go", ".rs",
        ".dockerfile", ".tf", ".hcl",
    }
    SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", ".tox",
                 "bundles", "build", "dist", ".next", ".cache", ".mypy_cache",
                 "coverage", ".voice-venv"}

    for proj in sample_dirs:
        proj_path = REPO_ROOT / proj
        if not proj_path.exists():
            continue
        # Get most recently modified code files (all languages)
        code_files = []
        for f in proj_path.rglob("*"):
            if not f.is_file():
                continue
            if any(skip in f.parts for skip in SKIP_DIRS):
                continue
            if f.suffix.lower() in CODE_EXTENSIONS or f.name in ("Dockerfile", "Makefile", "Procfile"):
                try:
                    code_files.append((f, f.stat().st_mtime))
                except OSError:
                    continue
        code_files.sort(key=lambda x: -x[1])
        for cf, _ in code_files[:5]:
            try:
                rel = str(cf.relative_to(REPO_ROOT))
                with open(cf, "r", errors="replace") as f:
                    content = f.read()
                if len(content) > 8000:
                    content = content[:8000] + f"\n[TRUNCATED at 8K — full: {len(content)} chars]"
                code_samples[rel] = content
            except Exception:
                continue
        if len(code_samples) >= 12:
            break

    ok(f"Sampled {len(code_samples)} code files from {', '.join(sample_dirs)}")

    # ─── Phase 4: GPT-4.1 Re-Verification ───
    header("Phase 4: Cardiologist Re-Verification (GPT-4.1)")

    # Build evidence text for GPT
    ev_lines = []
    for i, e in enumerate(evidence[:30], 1):
        if e.get("_source_type") == "learning":
            ev_lines.append(
                f"{i}. [LEARNING] type={e.get('type','?')} | created={e.get('timestamp','?')[:10]} "
                f"| source={e.get('source','?')[:40]}\n"
                f"   Title: {e.get('title','')[:120]}\n"
                f"   Content: {e.get('content','')[:300]}\n"
                f"   session_id: {e.get('session_id','')[:20]} | metadata: {json.dumps(e.get('metadata',{}))[:100]}"
            )
        else:
            ev_lines.append(
                f"{i}. [CLAIM] grade={e.get('evidence_grade','?')} | status={e.get('_status','?')} "
                f"| confidence={e.get('confidence',0):.0%} → {e.get('weighted_confidence',0):.0%} weighted\n"
                f"   Statement: {e.get('statement','')[:200]}\n"
                f"   n={e.get('n','?')} | area={e.get('area','?')}"
            )
    evidence_text = "\n\n".join(ev_lines)

    # Build working days context
    wd_lines = []
    for proj, wd in sorted(working_days.items(), key=lambda x: -x[1]["total_working_days"]):
        wd_lines.append(
            f"- {proj}: {wd['total_working_days']} working days, last active: {wd['last_active']}"
        )
    wd_text = "\n".join(wd_lines)

    # Build code context
    code_text = ""
    for fp, content in list(code_samples.items())[:6]:
        code_text += f"\n{'='*40}\n## {fp}\n{'='*40}\n{content}\n"

    reverify_system = (
        "You are GPT-4.1, the Cardiologist in the ContextDNA Surgery Team of 3.\n"
        "You are conducting EVIDENCE RE-VERIFICATION — re-examining evidence grades "
        "against current codebase state with WORKING DAYS temporal awareness.\n\n"
        "CRITICAL CONCEPT — Working Days:\n"
        "- Evidence timestamps ONLY count days the project was ACTIVELY worked on\n"
        "- If a project was shelved (no commits) for months, that gap does NOT age the evidence\n"
        "- Example: Evidence from October on a project shelved until March is still 'fresh' "
        "if only 30 working days have elapsed\n"
        "- Use the 'Working Days' data provided to assess true evidence age\n\n"
        "Your tasks:\n"
        "1. RECLASSIFY — For each piece of evidence, determine if the grade is still appropriate "
        "given current code state. A learning that was 'fix' type but the code has since changed "
        "may no longer apply.\n"
        "2. SURFACE CRITICALS — Identify findings that the entire ContextDNA ecosystem MUST know:\n"
        "   - Evidence that contradicts current code\n"
        "   - High-grade evidence that is now stale (code diverged)\n"
        "   - Missing evidence for critical system areas\n"
        "   - Evidence that should trigger immediate investigation\n"
        "3. A/B TEST CANDIDATES — Flag evidence where an A/B test would resolve uncertainty\n\n"
        "Output as JSON:\n"
        "{\n"
        '  "scope": "project or area analyzed",\n'
        '  "working_days_summary": "Brief assessment of project activity",\n'
        '  "reclassifications": [\n'
        '    {\n'
        '      "evidence_id": "learning ID or claim ID",\n'
        '      "current_grade": "current grade/type",\n'
        '      "recommended_grade": "new grade (or KEEP if unchanged)",\n'
        '      "reason": "Why reclassify — cite specific code evidence",\n'
        '      "working_days_age": "Age in working days (not calendar days)",\n'
        '      "code_still_matches": true/false,\n'
        '      "confidence": 0.0-1.0\n'
        '    }\n'
        '  ],\n'
        '  "criticals": [\n'
        '    {\n'
        '      "finding": "What the system MUST know",\n'
        '      "severity": "critical|high|medium",\n'
        '      "evidence_ids": ["related evidence"],\n'
        '      "action_required": "What should be done",\n'
        '      "blast_radius": "What breaks if ignored"\n'
        '    }\n'
        '  ],\n'
        '  "ab_test_candidates": [\n'
        '    {\n'
        '      "claim": "Claim worth testing",\n'
        '      "control": "Current approach",\n'
        '      "variant": "Alternative to test",\n'
        '      "success_metric": "How to measure",\n'
        '      "effort": "low|medium|high",\n'
        '      "priority": 1-5\n'
        '    }\n'
        '  ],\n'
        '  "summary": "Overall health assessment of the evidence store"\n'
        "}"
    )

    reverify_prompt = (
        f"SCOPE: {scope}\n\n"
        f"=== PROJECT WORKING DAYS ===\n{wd_text}\n\n"
        f"=== EVIDENCE TO RE-VERIFY ({len(evidence)} items) ===\n{evidence_text}\n\n"
        f"=== CURRENT CODE SAMPLES ===\n{code_text}\n\n"
        "Re-verify all evidence. Use working days (not calendar days) for temporal assessment. "
        "Surface criticals. Flag A/B test candidates."
    )

    info(f"Sending {len(reverify_prompt)} chars to GPT-4.1...")
    r = query_remote(reverify_system, reverify_prompt, model="gpt-4.1", max_tokens=4096)

    if not r["ok"]:
        fail(f"Re-verification failed: {r['content']}")
        return

    _track_research_cost(rc, r.get("cost_usd", 0), f"cardio-reverify:{scope[:40]}")
    spent, remaining, _ = _get_research_budget(rc)
    ok(f"Re-verification complete — ${r.get('cost_usd', 0):.6f}")

    # ─── Phase 5: Parse & Store Results ───
    header("Phase 5: Processing Results")

    parsed = _safe_json_parse(r["content"])
    if not parsed:
        warn("Could not parse structured JSON — storing raw response")
        parsed = {"raw_response": r["content"], "criticals": [], "reclassifications": [], "ab_test_candidates": []}

    # Print summary
    reclassifications = parsed.get("reclassifications", [])
    criticals = parsed.get("criticals", [])
    ab_candidates = parsed.get("ab_test_candidates", [])
    summary = parsed.get("summary", "")

    if summary:
        header("Summary")
        print(f"\n  {summary}\n")

    # Reclassifications
    if reclassifications:
        header(f"Reclassifications ({len(reclassifications)})")
        changed = [r for r in reclassifications if r.get("recommended_grade", "KEEP") != "KEEP"]
        kept = len(reclassifications) - len(changed)
        if changed:
            for rc_item in changed:
                color = YELLOW if rc_item.get("code_still_matches", True) else RED
                print(f"  {color}{rc_item.get('current_grade','?')} → {rc_item.get('recommended_grade','?')}{NC}"
                      f"  {rc_item.get('evidence_id','')[:30]}")
                dim(f"    {rc_item.get('reason','')[:100]}")
                dim(f"    Working days age: {rc_item.get('working_days_age', '?')}")
        if kept:
            ok(f"{kept} evidence items confirmed (grade unchanged)")

    # Criticals — store in Redis + file
    if criticals:
        header(f"CRITICAL FINDINGS ({len(criticals)})")
        for i, crit in enumerate(criticals, 1):
            sev = crit.get("severity", "medium")
            color = RED if sev == "critical" else (YELLOW if sev == "high" else CYAN)
            print(f"  {color}[{sev.upper()}]{NC} {crit.get('finding','')[:120]}")
            dim(f"    Action: {crit.get('action_required','')[:100]}")
            dim(f"    Blast radius: {crit.get('blast_radius','')[:80]}")

        # Store criticals in Redis for system-wide visibility
        for crit in criticals:
            if crit.get("severity") in ("critical", "high"):
                crit_key = f"cardio:reverify:critical:{int(time.time())}:{hash(crit.get('finding',''))%10000}"
                rc.set(crit_key, json.dumps({
                    "finding": crit.get("finding", ""),
                    "severity": crit.get("severity", "medium"),
                    "action_required": crit.get("action_required", ""),
                    "blast_radius": crit.get("blast_radius", ""),
                    "evidence_ids": crit.get("evidence_ids", []),
                    "source": "cardio-reverify",
                    "scope": scope,
                    "timestamp": datetime.now().isoformat(),
                }), ex=86400 * 7)  # 7-day TTL

        # Also push to the critical findings WAL for session_gold_passes compatibility
        for crit in criticals:
            if crit.get("severity") == "critical":
                try:
                    import redis as redis_lib
                    rc.zadd("contextdna:critical:wal", {
                        json.dumps({
                            "pass": "cardio_reverify",
                            "severity": "critical",
                            "finding": crit.get("finding", "")[:500],
                            "action": crit.get("action_required", "")[:200],
                            "scope": scope,
                        }): time.time()
                    })
                except Exception:
                    pass

        ok(f"Stored {sum(1 for c in criticals if c.get('severity') in ('critical','high'))} criticals in Redis")

    # A/B test candidates
    if ab_candidates:
        header(f"A/B Test Candidates ({len(ab_candidates)})")
        for i, ab in enumerate(ab_candidates, 1):
            info(f"{i}. {ab.get('claim', '')[:80]}")
            dim(f"   Control: {ab.get('control', '')[:60]}")
            dim(f"   Variant: {ab.get('variant', '')[:60]}")
            dim(f"   Metric: {ab.get('success_metric', '')[:60]}")
            dim(f"   Effort: {ab.get('effort', '?')} | Priority: {ab.get('priority', '?')}")
        # Store for ab-propose
        rc.set(f"surgery:reverify:ab_candidates:{int(time.time())}", json.dumps(ab_candidates), ex=86400 * 7)
        info(f"Run 'surgery-team.py ab-propose \"<claim>\"' to formalize")

    # ─── Phase 6: Write Accessible Output ───
    header("Phase 6: Writing Results")

    # WAL file for Atlas
    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    ts = int(time.time())
    result_data = {
        "timestamp": datetime.now().isoformat(),
        "sender": "GPT-4.1",
        "recipient": "all",  # Atlas + local LLM + Aaron
        "message_type": "finding",
        "task_type": "evidence_reverification",
        "discussion_id": f"cardio-reverify-{ts}",
        "scope": scope,
        "working_days": working_days,
        "evidence_count": len(evidence),
        "reclassifications": reclassifications,
        "criticals": criticals,
        "ab_test_candidates": ab_candidates,
        "summary": summary,
        "raw_response": r["content"],
        "cost_usd": r.get("cost_usd", 0),
    }
    out_path = results_dir / f"cardio_reverify_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(result_data, f, indent=2, default=str)
    ok(f"WAL: {out_path}")

    # Findings file for local LLM (concise, prompt-friendly format)
    findings_dir = REPO_ROOT / "memory" / ".cardio_findings"
    findings_dir.mkdir(exist_ok=True)
    findings_file = findings_dir / f"reverify_{scope.replace('/', '_')}_{ts}.md"
    md_lines = [
        f"# Cardiologist Evidence Re-Verification: {scope}",
        f"Date: {datetime.now().isoformat()[:19]}",
        f"Evidence reviewed: {len(evidence)} items",
        "",
    ]
    if summary:
        md_lines.extend(["## Summary", summary, ""])
    if criticals:
        md_lines.append("## Criticals")
        for c in criticals:
            md_lines.append(f"- **[{c.get('severity','?').upper()}]** {c.get('finding','')}")
            md_lines.append(f"  Action: {c.get('action_required','')}")
        md_lines.append("")
    if changed_reclassifications := [r for r in reclassifications if r.get("recommended_grade", "KEEP") != "KEEP"]:
        md_lines.append("## Reclassifications")
        for rc_item in changed_reclassifications:
            md_lines.append(f"- {rc_item.get('evidence_id','?')}: {rc_item.get('current_grade','?')} → {rc_item.get('recommended_grade','?')} ({rc_item.get('reason','')[:80]})")
        md_lines.append("")
    if ab_candidates:
        md_lines.append("## A/B Test Candidates")
        for ab in ab_candidates:
            md_lines.append(f"- {ab.get('claim','')[:100]} (effort: {ab.get('effort','?')})")
        md_lines.append("")

    with open(findings_file, "w") as f:
        f.write("\n".join(md_lines))
    ok(f"Findings: {findings_file}")

    # Redis summary for quick access by any system component
    rc.set("cardio:reverify:latest", json.dumps({
        "scope": scope,
        "timestamp": datetime.now().isoformat(),
        "n_evidence": len(evidence),
        "n_reclassified": len([r for r in reclassifications if r.get("recommended_grade", "KEEP") != "KEEP"]),
        "n_criticals": len(criticals),
        "n_ab_candidates": len(ab_candidates),
        "summary": summary[:500] if summary else "",
        "findings_file": str(findings_file),
    }), ex=86400 * 7)
    ok("Redis: cardio:reverify:latest updated")

    # ─── Done ───
    header("Evidence Re-Verification Complete")
    total_cost = r.get("cost_usd", 0)
    info(f"Cost: ${total_cost:.6f} | Budget remaining: ${remaining - total_cost:.4f}")
    info(f"Criticals: {len(criticals)} | Reclassified: {len([r for r in reclassifications if r.get('recommended_grade','KEEP') != 'KEEP'])} | A/B candidates: {len(ab_candidates)}")
    if criticals:
        info("Criticals stored in Redis (cardio:reverify:critical:*) — visible to all system components")
    info(f"Local LLM can read: {findings_file}")


# ─────────────────────────────────────────────────────────────────────────────
# DEEP AUDIT — chained research → evidence → gap detection → A/B proposals
# ─────────────────────────────────────────────────────────────────────────────

def cmd_deep_audit(topic: str):
    """Chained pipeline: discover planned features → cross-check against codebase → identify gaps → propose A/B tests.

    Combines research, evidence cross-examination, and A/B test proposal into
    a single automated pipeline. Accepts a topic or comma-separated file paths.
    """
    import redis as redis_lib
    try:
        rc = redis_lib.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
    except Exception:
        fail("Redis unreachable — needed for budget tracking")
        return

    spent, remaining, budget = _get_research_budget(rc)
    header(f"=== Deep Audit: {topic[:60]} ===")
    info(f"Budget: ${spent:.4f} spent / ${budget:.2f} limit (${remaining:.4f} remaining)")

    # Need at least ~$0.10 for the full pipeline (3 GPT-4.1 calls)
    if remaining < 0.10:
        fail(f"Insufficient budget for deep audit (${remaining:.4f} remaining, need ~$0.10)")
        return

    # ─── Phase 1: Document Discovery ───
    header("Phase 1: Document Discovery")

    # Check if topic contains file paths (comma-separated)
    explicit_files = []
    if "/" in topic and any(topic.strip().endswith(ext) for ext in (".md", ".py", ".yaml", ".yml", ".json")):
        explicit_files = [f.strip() for f in topic.split(",") if f.strip()]
        info(f"Explicit file list provided: {len(explicit_files)} files")
        topic_for_prompts = "Deep audit of planning documents: " + ", ".join(
            Path(f).stem for f in explicit_files[:5]
        )
    else:
        topic_for_prompts = topic

    docs = _build_doc_index()
    info(f"Index: {len(docs)} markdown files in repository")

    if explicit_files:
        # Use explicit files directly
        selected = explicit_files
        ok(f"Using {len(selected)} explicitly provided files")
    else:
        # GPT-4.1 selects files
        from collections import defaultdict
        by_dir = defaultdict(list)
        for d in docs:
            top = d["path"].split("/")[0] if "/" in d["path"] else "root"
            by_dir[top].append(d)

        index_text = f"# Document Index — {len(docs)} markdown files\n\n"
        for dir_name, dir_docs in sorted(by_dir.items()):
            index_text += f"\n## {dir_name}/ ({len(dir_docs)} files)\n"
            for d in dir_docs:
                index_text += f"- [{d['lines']:4d}L {d['size']:6d}B] {d['path']}"
                if d["title"]:
                    index_text += f" — {d['title']}"
                index_text += "\n"

        select_system = (
            "You are GPT-4.1, the Cardiologist in the ContextDNA Surgery Team.\n"
            "You are running a DEEP AUDIT — finding planned-but-unbuilt features.\n\n"
            "Select 5-10 files most likely to contain:\n"
            "- Feature plans, design docs, implementation roadmaps\n"
            "- Architecture decisions with TODO/future items\n"
            "- Dependency audits with recommended fixes\n\n"
            "Prefer files with more content (higher line count).\n"
            "RESPOND ONLY as a JSON array of file paths."
        )
        select_prompt = (
            f"AUDIT TOPIC: {topic}\n\n"
            f"{index_text}\n\n"
            "Select 5-10 files for deep audit. JSON array only."
        )

        r_select = query_remote(select_system, select_prompt, model="gpt-4.1", max_tokens=1024)
        if not r_select["ok"]:
            fail(f"File selection failed: {r_select['content']}")
            return

        _track_research_cost(rc, r_select.get("cost_usd", 0), f"deep-audit-select:{topic[:40]}")
        info(f"Selection cost: ${r_select.get('cost_usd', 0):.6f}")

        try:
            raw = r_select["content"].strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            selected = json.loads(raw)
            if not isinstance(selected, list):
                selected = [selected]
        except Exception as e:
            fail(f"Could not parse file selection: {e}")
            return

    ok(f"Selected {len(selected)} files:")
    for fp in selected:
        dim(f"  {fp}")

    # ─── Phase 2: Read Documents ───
    header("Phase 2: Reading Documents")

    file_contents = {}
    total_chars = 0
    max_chars_per_file = 8000
    max_total_chars = 60000

    for fp in selected:
        full = REPO_ROOT / fp
        if not full.exists():
            warn(f"File not found: {fp}")
            continue
        try:
            with open(full, "r", errors="replace") as f:
                content = f.read()
            if len(content) > max_chars_per_file:
                content = content[:max_chars_per_file] + f"\n\n[... TRUNCATED at {max_chars_per_file} chars — full file is {len(content)} chars]"
            file_contents[fp] = content
            total_chars += len(content)
            ok(f"{fp} ({len(content)} chars)")
            if total_chars > max_total_chars:
                warn(f"Reached {total_chars} char cap — skipping remaining files")
                break
        except Exception as e:
            warn(f"Error reading {fp}: {e}")

    if not file_contents:
        fail("No files could be read — aborting")
        return

    # ─── Phase 3: Discover Planned Features ───
    header("Phase 3: Discovering Planned Features")

    docs_text = ""
    for fp, content in file_contents.items():
        docs_text += f"\n{'='*60}\n## FILE: {fp}\n{'='*60}\n{content}\n"

    discovery_system = (
        "You are GPT-4.1, the Cardiologist conducting a DEEP AUDIT.\n"
        "Your task: Extract every planned feature, recommended fix, and TODO item from these documents.\n\n"
        "For each item, classify its implementation status based on what the documents say:\n"
        "- PLANNED: Described in detail but explicitly marked as future/next/TODO\n"
        "- RECOMMENDED: An audit/review recommended this but no implementation mentioned\n"
        "- PARTIAL: Some work described but not complete\n"
        "- UNKNOWN: Can't determine status from documents alone\n\n"
        "Output as JSON:\n"
        "{\n"
        '  "planned_items": [\n'
        '    {\n'
        '      "name": "Short descriptive name",\n'
        '      "description": "What this feature/fix does (1-2 sentences)",\n'
        '      "source_file": "path/to/file.md",\n'
        '      "status": "PLANNED|RECOMMENDED|PARTIAL|UNKNOWN",\n'
        '      "category": "feature|fix|infrastructure|security|performance|testing",\n'
        '      "priority": "critical|high|medium|low",\n'
        '      "implementation_hints": "Files/modules mentioned, approach described"\n'
        '    }\n'
        '  ],\n'
        '  "total_items": N,\n'
        '  "summary": "High-level summary of what these docs plan"\n'
        "}"
    )
    discovery_prompt = (
        f"AUDIT TOPIC: {topic_for_prompts}\n\n"
        f"=== DOCUMENTS ({len(file_contents)} files) ===\n{docs_text}\n\n"
        "Extract ALL planned features, recommended fixes, and TODO items. JSON only."
    )

    spent, remaining, _ = _get_research_budget(rc)
    if remaining < 0.05:
        fail(f"Insufficient budget for discovery phase (${remaining:.4f})")
        return

    info(f"Sending {len(docs_text)} chars to GPT-4.1 for feature discovery...")
    r_discover = query_remote(discovery_system, discovery_prompt, model="gpt-4.1", max_tokens=4096)

    if not r_discover["ok"]:
        fail(f"Feature discovery failed: {r_discover['content']}")
        return

    _track_research_cost(rc, r_discover.get("cost_usd", 0), f"deep-audit-discover:{topic[:40]}")
    ok(f"Discovery cost: ${r_discover.get('cost_usd', 0):.6f}")

    # Parse planned items
    planned_items = []
    discovery_summary = ""
    try:
        raw_discover = r_discover["content"].strip()
        if "```" in raw_discover:
            raw_discover = raw_discover.split("```")[1]
            if raw_discover.startswith("json"):
                raw_discover = raw_discover[4:]
            raw_discover = raw_discover.rstrip("`")
        parsed_discover = json.loads(raw_discover)
        planned_items = parsed_discover.get("planned_items", [])
        discovery_summary = parsed_discover.get("summary", "")
    except Exception as e:
        warn(f"Could not parse discovery output: {e}")
        # Still show raw output
        print(f"\n{r_discover['content'][:2000]}")

    if planned_items:
        ok(f"Found {len(planned_items)} planned items")
        for i, item in enumerate(planned_items, 1):
            status_color = GREEN if item.get("status") == "PARTIAL" else YELLOW
            dim(f"  {i}. [{status_color}{item.get('status', '?')}{NC}] "
                f"[{item.get('priority', '?')}] {item.get('name', '?')}")

    # ─── Phase 4: Evidence Cross-Check ───
    header("Phase 4: Evidence Cross-Check Against Codebase")

    evidence = _get_evidence_snapshot(topic_for_prompts)
    n_learn = len(evidence["learnings"])
    n_claims = len(evidence["claims"])
    n_patterns = len(evidence["negative_patterns"])
    ok(f"Evidence store: {n_learn} learnings, {n_claims} claims, {n_patterns} anti-patterns")

    # Build cross-check prompt with planned items + evidence
    crosscheck_system = (
        "You are GPT-4.1, the Cardiologist conducting the CROSS-CHECK phase of a Deep Audit.\n\n"
        "You have:\n"
        "1. A list of planned features/fixes extracted from project documents\n"
        "2. Evidence from the project's evidence store (learnings, claims, anti-patterns)\n\n"
        "Your task: For each planned item, determine if it was ACTUALLY BUILT or not.\n"
        "Cross-reference the evidence store for implementation signals:\n"
        "- Success records mentioning the feature\n"
        "- Bug fixes related to the feature (implies it exists)\n"
        "- Anti-patterns mentioning it (implies it was attempted)\n"
        "- Claims about its status\n\n"
        "Output as JSON:\n"
        "{\n"
        '  "gap_analysis": [\n'
        '    {\n'
        '      "name": "Feature name (from planned items)",\n'
        '      "verdict": "BUILT|NOT_BUILT|PARTIALLY_BUILT|SUPERSEDED|UNCERTAIN",\n'
        '      "evidence": "What evidence supports this verdict (specific references)",\n'
        '      "confidence": 0.0-1.0,\n'
        '      "priority": "critical|high|medium|low",\n'
        '      "recommendation": "What to do about this gap (if NOT_BUILT/PARTIALLY_BUILT)"\n'
        '    }\n'
        '  ],\n'
        '  "ab_test_candidates": [\n'
        '    {\n'
        '      "name": "Feature/approach worth A/B testing",\n'
        '      "hypothesis": "Why testing this would be valuable",\n'
        '      "control": "Current approach (status quo)",\n'
        '      "variant": "Alternative to test",\n'
        '      "success_metric": "How to measure which is better",\n'
        '      "effort": "low|medium|high",\n'
        '      "priority": 1-5\n'
        '    }\n'
        '  ],\n'
        '  "summary": {\n'
        '    "total_planned": N,\n'
        '    "built": N,\n'
        '    "not_built": N,\n'
        '    "partially_built": N,\n'
        '    "uncertain": N,\n'
        '    "narrative": "2-3 sentence overall assessment"\n'
        '  }\n'
        "}"
    )

    planned_text = json.dumps(planned_items, indent=2) if planned_items else r_discover["content"]
    crosscheck_prompt = (
        f"AUDIT TOPIC: {topic_for_prompts}\n\n"
        f"=== PLANNED ITEMS ({len(planned_items)} items) ===\n{planned_text}\n\n"
        f"=== EVIDENCE FROM STORE ===\n{evidence['evidence_text']}\n\n"
        "Cross-check each planned item against the evidence. Which were built? Which are gaps?"
    )

    spent, remaining, _ = _get_research_budget(rc)
    if remaining < 0.03:
        fail(f"Insufficient budget for cross-check (${remaining:.4f})")
        # Still output discovery results
        header("Partial Results (budget exhausted before cross-check)")
        print(json.dumps({"planned_items": planned_items, "summary": discovery_summary}, indent=2))
        return

    info(f"Sending {len(crosscheck_prompt)} chars for evidence cross-check...")
    r_crosscheck = query_remote(crosscheck_system, crosscheck_prompt, model="gpt-4.1", max_tokens=4096)

    if not r_crosscheck["ok"]:
        fail(f"Cross-check failed: {r_crosscheck['content']}")
        return

    _track_research_cost(rc, r_crosscheck.get("cost_usd", 0), f"deep-audit-crosscheck:{topic[:40]}")
    ok(f"Cross-check cost: ${r_crosscheck.get('cost_usd', 0):.6f}")

    # ─── Phase 5: Results ───
    header("Phase 5: Deep Audit Results")

    # Parse cross-check results
    gap_analysis = []
    ab_candidates = []
    audit_summary = {}
    try:
        raw_cc = r_crosscheck["content"].strip()
        if "```" in raw_cc:
            raw_cc = raw_cc.split("```")[1]
            if raw_cc.startswith("json"):
                raw_cc = raw_cc[4:]
            raw_cc = raw_cc.rstrip("`")
        parsed_cc = json.loads(raw_cc)
        gap_analysis = parsed_cc.get("gap_analysis", [])
        ab_candidates = parsed_cc.get("ab_test_candidates", [])
        audit_summary = parsed_cc.get("summary", {})
    except Exception as e:
        warn(f"Could not parse cross-check output: {e}")
        print(f"\n{r_crosscheck['content'][:3000]}")

    # Display gap analysis
    if gap_analysis:
        header("Gap Analysis")
        verdict_colors = {
            "BUILT": GREEN, "NOT_BUILT": RED, "PARTIALLY_BUILT": YELLOW,
            "SUPERSEDED": DIM, "UNCERTAIN": CYAN,
        }
        for item in gap_analysis:
            v = item.get("verdict", "?")
            color = verdict_colors.get(v, NC)
            conf = item.get("confidence", 0)
            pri = item.get("priority", "?")
            print(f"  {color}{v:17s}{NC} [{pri:8s}] {item.get('name', '?')[:60]}  (conf: {conf:.0%})")
            if v in ("NOT_BUILT", "PARTIALLY_BUILT") and item.get("recommendation"):
                dim(f"                    → {item['recommendation'][:80]}")

    # Display summary
    if audit_summary:
        header("Summary")
        s = audit_summary
        info(f"Total planned: {s.get('total_planned', '?')}")
        ok(f"Built: {s.get('built', '?')}")
        if s.get('not_built', 0) > 0:
            fail(f"Not built: {s.get('not_built', '?')}")
        if s.get('partially_built', 0) > 0:
            warn(f"Partially built: {s.get('partially_built', '?')}")
        if s.get('uncertain', 0) > 0:
            info(f"Uncertain: {s.get('uncertain', '?')}")
        if s.get('narrative'):
            print(f"\n  {s['narrative']}")

    # Display A/B test candidates
    if ab_candidates:
        header(f"A/B Test Candidates ({len(ab_candidates)})")
        for i, ab in enumerate(ab_candidates, 1):
            info(f"{i}. {ab.get('name', ab.get('hypothesis', ''))[:80]}")
            dim(f"   Control: {ab.get('control', '')[:60]}")
            dim(f"   Variant: {ab.get('variant', '')[:60]}")
            dim(f"   Metric: {ab.get('success_metric', '')[:60]}")
            dim(f"   Effort: {ab.get('effort', '?')} | Priority: {ab.get('priority', '?')}")
        info("Run 'surgery-team.py ab-propose \"<hypothesis>\"' to formalize")

    # ─── Phase 6: Save Results ───
    header("Phase 6: Saving Results")

    results_dir = Path("/tmp/atlas-agent-results")
    results_dir.mkdir(exist_ok=True)
    ts = int(time.time())

    select_cost = 0
    try:
        select_cost = r_select.get("cost_usd", 0)
    except NameError:
        pass
    total_cost = (
        select_cost +
        r_discover.get("cost_usd", 0) +
        r_crosscheck.get("cost_usd", 0)
    )

    result_data = {
        "timestamp": datetime.now().isoformat(),
        "sender": "GPT-4.1",
        "recipient": "Atlas",
        "message_type": "finding",
        "task_type": "deep_audit",
        "discussion_id": f"deep-audit-{ts}",
        "audit_topic": topic,
        "files_analyzed": list(file_contents.keys()),
        "planned_items": planned_items,
        "gap_analysis": gap_analysis,
        "ab_test_candidates": ab_candidates,
        "summary": audit_summary,
        "discovery_summary": discovery_summary,
        "evidence_snapshot": {
            "learnings_count": n_learn,
            "claims_count": n_claims,
            "patterns_count": n_patterns,
        },
        "cost_usd": total_cost,
    }

    out_path = results_dir / f"deep_audit_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(result_data, f, indent=2, default=str)
    ok(f"WAL: {out_path}")

    # Store A/B candidates in Redis for ab-propose
    if ab_candidates:
        rc.set(f"surgery:deep_audit:ab_candidates:{ts}", json.dumps(ab_candidates), ex=86400 * 7)
        ok(f"A/B candidates stored in Redis ({len(ab_candidates)} candidates, 7-day TTL)")

    # Store gap summary in Redis for quick access
    rc.set("surgery:deep_audit:latest", json.dumps({
        "timestamp": datetime.now().isoformat(),
        "topic": topic,
        "files": list(file_contents.keys()),
        "summary": audit_summary,
        "gaps_count": len([g for g in gap_analysis if g.get("verdict") in ("NOT_BUILT", "PARTIALLY_BUILT")]),
        "ab_candidates_count": len(ab_candidates),
        "cost_usd": total_cost,
    }), ex=86400 * 7)
    ok("Redis: surgery:deep_audit:latest updated")

    # Done
    spent, remaining, _ = _get_research_budget(rc)
    header("Deep Audit Complete")
    info(f"Total cost: ${total_cost:.6f}")
    info(f"Budget remaining: ${remaining:.4f} / ${budget:.2f}")
    gaps = len([g for g in gap_analysis if g.get("verdict") in ("NOT_BUILT", "PARTIALLY_BUILT")])
    info(f"Gaps found: {gaps} | A/B candidates: {len(ab_candidates)}")
    if gaps > 0:
        info("Review gaps above and decide which to implement next")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

COMMANDS = {
    "probe": (cmd_probe, "Quick health check all 3 surgeons"),
    "ask-local": (cmd_ask_local, "Query Qwen3-4B directly"),
    "ask-remote": (cmd_ask_remote, "Query GPT-4.1 directly"),
    "introspect": (cmd_introspect, "All models self-report capabilities"),
    "consult": (cmd_consult, "Structured multi-model consultation"),
    "cross-exam": (cmd_cross_exam, "Full cross-examination protocol"),
    "consensus": (cmd_consensus, "Confidence-weighted consensus on a claim"),
    "research": (cmd_research, "GPT-4.1 self-directed doc research ($5/day)"),
    "research-status": (cmd_research_status, "Research budget & recent sessions"),
    "evidence": (cmd_research_evidence, "Cross-examine docs against evidence store"),
    "ab-propose": (cmd_ab_propose, "Design A/B test for a claim"),
    "ab-collaborate": (cmd_ab_collaborate, "3-surgeon A/B test design (full consensus)"),
    "ab-start": (cmd_ab_start, "Activate an approved A/B test"),
    "ab-measure": (cmd_ab_measure, "Measure an active A/B test"),
    "ab-conclude": (cmd_ab_conclude, "Conclude test: ab-conclude <ref> win|lose|inconclusive"),
    "ab-status": (cmd_ab_status, "A/B test dashboard"),
    "ab-validate": (cmd_ab_validate, "Quick fix validation — 3-surgeon fast feedback"),
    "ab-veto": (cmd_ab_veto, "Veto an autonomous A/B test"),
    "ab-queue": (cmd_ab_queue, "Autonomous A/B test queue & status"),
    "cardio-review": (cmd_cardio_review, "Cardiologist cross-exam → 3-surgeon corrigibility"),
    "cardio-reverify": (cmd_cardio_reverify, "Re-verify evidence grades (working-days aware)"),
    "status": (cmd_status, "Hybrid routing status + costs"),
    "neurologist-pulse": (cmd_neurologist_pulse, "Neurologist system pulse — live health"),
    "neurologist-challenge": (cmd_neurologist_challenge, "Corrigibility skeptic — challenge assumptions"),
    "deep-audit": (cmd_deep_audit, "Chained: docs → gaps → evidence → A/B proposals"),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(f"\n{BOLD}Surgery Team of 3{NC} — Multi-Model Collaboration Tool\n")
        print(f"Usage: {sys.argv[0]} <command> [args...]\n")
        print("Commands:")
        for name, (_, desc) in COMMANDS.items():
            print(f"  {CYAN}{name:14s}{NC}  {desc}")
        print(f"\nExamples:")
        print(f'  {DIM}./scripts/surgery-team.py probe{NC}')
        print(f'  {DIM}./scripts/surgery-team.py ask-local "Explain the webhook architecture"{NC}')
        print(f'  {DIM}./scripts/surgery-team.py cross-exam "Is the anticipation engine working correctly?"{NC}')
        print(f'  {DIM}./scripts/surgery-team.py consensus "Docker being down causes 50% context loss"{NC}')
        return

    cmd_name = sys.argv[1]
    if cmd_name not in COMMANDS:
        print(f"{RED}Unknown command: {cmd_name}{NC}")
        print(f"Run with --help for available commands")
        sys.exit(1)

    cmd_fn, _ = COMMANDS[cmd_name]

    # Commands that take a prompt argument
    if cmd_name in ("ask-local", "ask-remote", "consult", "cross-exam", "consensus", "research",
                     "evidence", "ab-propose", "ab-collaborate", "ab-start", "ab-measure", "ab-conclude",
                     "ab-validate", "ab-veto", "cardio-review", "cardio-reverify", "neurologist-challenge",
                     "deep-audit"):
        if len(sys.argv) < 3:
            print(f"{RED}Missing argument: {cmd_name} requires a prompt/topic{NC}")
            sys.exit(1)
        prompt = " ".join(sys.argv[2:])
        cmd_fn(prompt)
    else:
        cmd_fn()


if __name__ == "__main__":
    main()
