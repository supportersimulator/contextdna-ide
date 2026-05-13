#!/usr/bin/env python3
"""
Local LLM Architecture Analyzer

This module provides integration with locally-running LLMs (Ollama, LM Studio, etc.)
for zero-cost continuous architecture analysis.

WHY LOCAL LLM:
- Zero API costs - process unlimited text
- Full context - can analyze entire conversation/work logs
- Privacy - all data stays on your machine
- Speed - no network latency

SUPPORTED LOCAL LLM BACKENDS:
1. Ollama (recommended) - runs on port 11434
2. LM Studio - runs on port 1234
3. Text Generation WebUI - runs on port 5000
4. Custom endpoint - any OpenAI-compatible API

SETUP:
1. Install Ollama: brew install ollama
2. Pull a model: ollama pull llama3.1:8b  (or mistral, codellama, etc.)
3. Run Ollama: ollama serve
4. This module auto-detects and uses it

Usage:
    # Analyze all recent work with local LLM
    python local_llm_analyzer.py analyze

    # Generate architecture summary
    python local_llm_analyzer.py summarize

    # Extract patterns with full context
    python local_llm_analyzer.py patterns

    # Check local LLM status
    python local_llm_analyzer.py status

    # Run continuous analysis (background)
    python local_llm_analyzer.py background
"""

import os
import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import work log from architecture enhancer
try:
    from memory.architecture_enhancer import work_log, WorkDialogueLog
    WORK_LOG_AVAILABLE = True
except ImportError:
    WORK_LOG_AVAILABLE = False

try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False


# =============================================================================
# LOCAL LLM CONFIGURATION
# =============================================================================

@dataclass
class LLMEndpoint:
    """Configuration for a local LLM endpoint."""
    name: str
    url: str
    model: str
    api_format: str  # 'ollama', 'openai', 'tgwui'


# Common local LLM endpoints to try (ordered by preference: mlx_lm first)
LOCAL_ENDPOINTS = [
    LLMEndpoint("mlx_lm", "http://127.0.0.1:5044/v1/chat/completions", "mlx-community/Qwen3-4B-4bit", "openai"),
    LLMEndpoint("ollama", "http://localhost:11434/api/generate", "llama3.1:8b", "ollama"),
    LLMEndpoint("ollama-chat", "http://localhost:11434/api/chat", "llama3.1:8b", "ollama-chat"),
    LLMEndpoint("lm-studio", "http://localhost:1234/v1/chat/completions", "local-model", "openai"),
    LLMEndpoint("text-gen-webui", "http://localhost:5000/v1/chat/completions", "default", "openai"),
]

# State file for analyzer
ANALYZER_STATE_FILE = Path(__file__).parent / ".local_llm_state.json"


class LocalLLMClient:
    """
    Client for communicating with local LLMs.

    Auto-detects available endpoints and uses the first working one.
    """

    def __init__(self, preferred_endpoint: str = None):
        self.endpoint = None
        self.available = False
        self._detect_endpoint(preferred_endpoint)

    def _detect_endpoint(self, preferred: str = None):
        """Detect available local LLM endpoints.

        For mlx_lm: uses Redis health cache via priority queue (NO direct HTTP to port 5044).
        For other backends: direct health check to their respective ports.
        """
        endpoints_to_try = LOCAL_ENDPOINTS

        if preferred:
            # Put preferred endpoint first
            endpoints_to_try = [e for e in LOCAL_ENDPOINTS if e.name == preferred] + \
                               [e for e in LOCAL_ENDPOINTS if e.name != preferred]

        for endpoint in endpoints_to_try:
            try:
                if endpoint.name == "mlx_lm":
                    # Route through priority queue health cache — NO direct HTTP to 5044
                    from memory.llm_priority_queue import check_llm_health
                    if check_llm_health():
                        self.endpoint = endpoint
                        self.available = True
                        return
                elif endpoint.api_format == "ollama" or endpoint.api_format == "ollama-chat":
                    resp = requests.get("http://localhost:11434/api/tags", timeout=2)
                    if resp.status_code == 200:
                        self.endpoint = endpoint
                        self.available = True
                        return
                else:
                    # Non-mlx OpenAI-compatible endpoints (lm-studio, text-gen-webui)
                    resp = requests.get(endpoint.url.replace("/chat/completions", "/models"), timeout=5)
                    if resp.status_code == 200:
                        self.endpoint = endpoint
                        self.available = True
                        return
            except Exception as e:
                print(f"[WARN] LLM endpoint {endpoint.name} check failed: {e}")
                continue

        # No endpoint found
        self.available = False

    def generate(self, prompt: str, max_tokens: int = 2000) -> Optional[str]:
        """
        Generate text using the local LLM.

        For mlx_lm: routes through priority queue (NO direct HTTP to port 5044).
        For other backends: direct HTTP to their respective ports.

        Args:
            prompt: The prompt to send
            max_tokens: Maximum tokens in response

        Returns:
            Generated text or None if failed
        """
        if not self.available or not self.endpoint:
            return None

        try:
            if self.endpoint.name == "mlx_lm":
                # ALL mlx_lm access routes through priority queue
                from memory.llm_priority_queue import butler_query
                return butler_query("", prompt, profile="extract")

            elif self.endpoint.api_format == "ollama":
                # Ollama generate API
                response = requests.post(
                    self.endpoint.url,
                    json={
                        "model": self.endpoint.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "num_predict": max_tokens
                        }
                    },
                    timeout=120
                )
                if response.status_code == 200:
                    return response.json().get("response", "")

            elif self.endpoint.api_format == "ollama-chat":
                # Ollama chat API
                response = requests.post(
                    self.endpoint.url,
                    json={
                        "model": self.endpoint.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False
                    },
                    timeout=120
                )
                if response.status_code == 200:
                    return response.json().get("message", {}).get("content", "")

            else:
                # Non-mlx OpenAI-compatible API (lm-studio, text-gen-webui)
                response = requests.post(
                    self.endpoint.url,
                    json={
                        "model": self.endpoint.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens
                    },
                    timeout=120
                )
                if response.status_code == 200:
                    return response.json()["choices"][0]["message"]["content"]

        except Exception as e:
            print(f"LLM generation error: {e}")
            return None

        return None

    def get_status(self) -> Dict:
        """Get status of local LLM."""
        status = {
            "available": self.available,
            "endpoint": None,
            "models": []
        }

        if self.endpoint:
            status["endpoint"] = {
                "name": self.endpoint.name,
                "url": self.endpoint.url,
                "model": self.endpoint.model
            }

            # Try to list available models
            try:
                if "ollama" in self.endpoint.api_format:
                    resp = requests.get("http://localhost:11434/api/tags", timeout=2)
                    if resp.status_code == 200:
                        status["models"] = [m["name"] for m in resp.json().get("models", [])]
            except Exception as e:
                print(f"[WARN] Ollama model list fetch failed: {e}")

        return status


# Global LLM client
llm_client = LocalLLMClient()


# =============================================================================
# ANALYSIS PROMPTS
# =============================================================================

ARCHITECTURE_ANALYSIS_PROMPT = """You are an expert software architect analyzing a development work log.

The following is a log of commands, observations, successes, and dialogue from a software development session.
Your job is to extract architectural insights and patterns.

WORK LOG:
{work_log}

Please analyze this work log and provide:

1. **Architecture Patterns Detected**
   - List any recurring patterns (deployment, async, networking, etc.)
   - Note which parts of the system they affect

2. **Key Infrastructure Details**
   - Any IPs, instance IDs, service names, or endpoints mentioned
   - Configuration values that seem important

3. **Successful Procedures**
   - What operations completed successfully?
   - What sequence of steps led to success?

4. **Potential Improvements**
   - Any inefficiencies or repeated steps?
   - Suggestions for automation?

5. **Critical Warnings**
   - Any gotchas or issues to remember?
   - Things that failed before succeeding?

Be specific and actionable. Reference specific entries from the log when possible.
"""

PATTERN_EXTRACTION_PROMPT = """Analyze this development work log and extract reusable patterns.

WORK LOG:
{work_log}

For each pattern found, provide:
1. Pattern name
2. When to use it
3. Step-by-step procedure
4. Common gotchas

Focus on patterns that could be turned into Standard Operating Procedures (SOPs).
"""

SUMMARY_PROMPT = """Summarize the key architectural knowledge from this work session.

WORK LOG:
{work_log}

Provide a concise summary (under 500 words) that captures:
1. What was worked on
2. What was learned
3. What to remember for next time

This summary will be stored for future reference by other agents.
"""


# =============================================================================
# ANALYZER ENGINE
# =============================================================================

class LocalLLMAnalyzer:
    """
    Analyzes work logs using local LLM.

    Provides zero-cost, full-context analysis of all work.
    """

    def __init__(self):
        self.client = llm_client
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if ANALYZER_STATE_FILE.exists():
            try:
                with open(ANALYZER_STATE_FILE) as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] Analyzer state load failed: {e}")
        return {
            "last_analysis": None,
            "analyses_count": 0,
            "patterns_extracted": [],
            "summaries": []
        }

    def _save_state(self):
        with open(ANALYZER_STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    def _get_work_log_text(self, hours: int = 24) -> str:
        """Get work log as formatted text for LLM analysis."""
        if not WORK_LOG_AVAILABLE:
            return "Work log not available"

        entries = work_log.get_recent_entries(hours=hours)
        if not entries:
            return "No entries in work log"

        # Format entries for LLM
        text = ""
        for entry in entries:
            timestamp = entry.get("timestamp", "")[:19]  # Trim to datetime
            entry_type = entry.get("entry_type", "unknown")
            content = entry.get("content", "")
            source = entry.get("source", "")
            area = entry.get("area", "")

            text += f"[{timestamp}] [{entry_type.upper()}]"
            if area:
                text += f" [{area}]"
            if source:
                text += f" ({source})"
            text += f"\n{content}\n"

            if entry.get("metadata"):
                meta = entry["metadata"]
                if meta.get("details"):
                    text += f"  Details: {meta['details']}\n"
                if meta.get("output_preview"):
                    text += f"  Output: {meta['output_preview'][:200]}...\n"

            text += "\n"

        return text

    def analyze(self, hours: int = 24) -> Optional[str]:
        """
        Run full architecture analysis on work log.

        Returns analysis text or None if LLM not available.
        """
        if not self.client.available:
            return None

        work_text = self._get_work_log_text(hours)
        prompt = ARCHITECTURE_ANALYSIS_PROMPT.format(work_log=work_text)

        analysis = self.client.generate(prompt, max_tokens=3000)

        if analysis:
            self.state["last_analysis"] = datetime.now().isoformat()
            self.state["analyses_count"] += 1
            self._save_state()

            # Store in Context DNA if available
            if CONTEXT_DNA_AVAILABLE:
                try:
                    memory = ContextDNAClient()
                    memory.record_architecture_decision(
                        decision=f"[LOCAL-LLM] Architecture analysis",
                        rationale=analysis,
                        alternatives=None,
                        consequences="Generated by local LLM from work log"
                    )
                except Exception as e:
                    print(f"[WARN] Architecture decision recording failed: {e}")

        return analysis

    def extract_patterns(self, hours: int = 24) -> Optional[str]:
        """Extract reusable patterns from work log."""
        if not self.client.available:
            return None

        work_text = self._get_work_log_text(hours)
        prompt = PATTERN_EXTRACTION_PROMPT.format(work_log=work_text)

        patterns = self.client.generate(prompt, max_tokens=2000)

        if patterns:
            self.state["patterns_extracted"].append({
                "timestamp": datetime.now().isoformat(),
                "preview": patterns[:200]
            })
            self.state["patterns_extracted"] = self.state["patterns_extracted"][-20:]
            self._save_state()

        return patterns

    def summarize(self, hours: int = 24) -> Optional[str]:
        """Generate summary of work session."""
        if not self.client.available:
            return None

        work_text = self._get_work_log_text(hours)
        prompt = SUMMARY_PROMPT.format(work_log=work_text)

        summary = self.client.generate(prompt, max_tokens=1000)

        if summary:
            self.state["summaries"].append({
                "timestamp": datetime.now().isoformat(),
                "summary": summary[:500]
            })
            self.state["summaries"] = self.state["summaries"][-10:]
            self._save_state()

            # Store in Context DNA if available
            if CONTEXT_DNA_AVAILABLE:
                try:
                    memory = ContextDNAClient()
                    memory.record_agent_success(
                        task="Work session analysis",
                        approach="Local LLM analysis of work log",
                        result=summary,
                        agent_name="local-llm",
                        tags=["summary", "local-llm", "auto-generated"]
                    )
                except Exception as e:
                    print(f"[WARN] Work session recording failed: {e}")

        return summary

    def get_status(self) -> Dict:
        """Get analyzer status."""
        llm_status = self.client.get_status()
        return {
            "llm_available": llm_status["available"],
            "endpoint": llm_status.get("endpoint"),
            "models": llm_status.get("models", []),
            "last_analysis": self.state.get("last_analysis"),
            "analyses_count": self.state.get("analyses_count", 0),
            "patterns_extracted": len(self.state.get("patterns_extracted", [])),
            "summaries_generated": len(self.state.get("summaries", []))
        }


# Global analyzer
analyzer = LocalLLMAnalyzer()


def run_background_analysis(interval_hours: int = 4):
    """
    Run analysis in background mode.

    Analyzes work log every N hours.
    """
    import time

    print(f"Starting background analysis (interval: {interval_hours}h)")
    print("Press Ctrl+C to stop")

    while True:
        if analyzer.client.available:
            print(f"\n[{datetime.now().isoformat()}] Running local LLM analysis...")

            # Run summary first (lighter)
            summary = analyzer.summarize()
            if summary:
                print(f"  Summary generated ({len(summary)} chars)")

            # Run full analysis if enough time
            analysis = analyzer.analyze()
            if analysis:
                print(f"  Analysis complete ({len(analysis)} chars)")

        else:
            print(f"[{datetime.now().isoformat()}] Local LLM not available - skipping")

        # Wait for interval
        time.sleep(interval_hours * 3600)


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Local LLM Architecture Analyzer")
        print("")
        print("Uses locally-running LLMs (Ollama, LM Studio, etc.) for zero-cost analysis.")
        print("")
        print("Commands:")
        print("  analyze                - Run full architecture analysis")
        print("  patterns               - Extract reusable patterns")
        print("  summarize              - Generate work session summary")
        print("  status                 - Check local LLM status")
        print("  background             - Run continuous analysis")
        print("  view-log [hours]       - View raw work log")
        print("")
        print("Setup:")
        print("  1. Install Ollama: brew install ollama")
        print("  2. Pull a model: ollama pull llama3.1:8b")
        print("  3. Run Ollama: ollama serve")
        print("  4. Run analysis: python local_llm_analyzer.py analyze")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        status = analyzer.get_status()
        print("=== Local LLM Status ===")
        print(f"Available: {status['llm_available']}")
        if status.get("endpoint"):
            print(f"Endpoint: {status['endpoint']['name']} ({status['endpoint']['url']})")
            print(f"Model: {status['endpoint']['model']}")
        if status.get("models"):
            print(f"Available models: {', '.join(status['models'])}")
        print("")
        print("=== Analysis History ===")
        print(f"Last analysis: {status['last_analysis'] or 'Never'}")
        print(f"Total analyses: {status['analyses_count']}")
        print(f"Patterns extracted: {status['patterns_extracted']}")
        print(f"Summaries generated: {status['summaries_generated']}")

    elif cmd == "analyze":
        print("Running local LLM architecture analysis...")
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24

        if not analyzer.client.available:
            print("Error: No local LLM available")
            print("Start Ollama with: ollama serve")
            sys.exit(1)

        analysis = analyzer.analyze(hours=hours)
        if analysis:
            print("\n" + "=" * 60)
            print(analysis)
            print("=" * 60)
        else:
            print("Analysis failed")

    elif cmd == "patterns":
        print("Extracting patterns from work log...")
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24

        if not analyzer.client.available:
            print("Error: No local LLM available")
            sys.exit(1)

        patterns = analyzer.extract_patterns(hours=hours)
        if patterns:
            print("\n" + "=" * 60)
            print(patterns)
            print("=" * 60)
        else:
            print("Pattern extraction failed")

    elif cmd == "summarize":
        print("Generating work session summary...")
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24

        if not analyzer.client.available:
            print("Error: No local LLM available")
            sys.exit(1)

        summary = analyzer.summarize(hours=hours)
        if summary:
            print("\n" + "=" * 60)
            print(summary)
            print("=" * 60)
        else:
            print("Summary generation failed")

    elif cmd == "background":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 4
        run_background_analysis(interval_hours=interval)

    elif cmd == "view-log":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        print(f"=== Work Log (last {hours} hours) ===\n")
        print(analyzer._get_work_log_text(hours))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
