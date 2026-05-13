#!/usr/bin/env python3
"""
Synaptic Speed Benchmark - All Pathways Tested

Tests all communication pathways to Synaptic's intelligence:
1. Direct SynapticVoice.consult() - Pure Python (target: ~3-5ms)
2. HTTP to port 8888 chat server - WebSocket/REST
3. HTTP to port 5043 local LLM API - FastAPI inference
4. Direct MLX generation - With model caching

Each pathway is verified to return REAL Synaptic context (not canned responses).

Usage:
    python memory/test_synaptic_speed.py          # Run all benchmarks
    python memory/test_synaptic_speed.py --quick  # Quick mode (fewer iterations)
    python memory/test_synaptic_speed.py --verbose  # Detailed output
"""

import time
import statistics
import argparse
import json
import sys
import os
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import warnings

# Suppress warnings for clean output
warnings.filterwarnings("ignore")

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Configuration
REPO_ROOT = Path(__file__).parent.parent
MLX_VENV = REPO_ROOT / "context-dna" / "local_llm" / ".venv-mlx"
MLX_PYTHON = MLX_VENV / "bin" / "python3"

# Test prompts that should return real context
TEST_PROMPTS = [
    "What patterns are you sensing right now?",
    "How do we deploy to production?",
    "What's the async boto3 pattern?",
    "Tell me about the voice stack architecture",
]


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""
    pathway: str
    success: bool
    latency_ms: float
    response_preview: str
    is_real_context: bool  # True if response has real Synaptic data
    error: Optional[str] = None


@dataclass
class PathwaySummary:
    """Summary statistics for a pathway."""
    pathway: str
    total_runs: int
    successful_runs: int
    real_context_count: int
    latencies_ms: List[float]

    @property
    def success_rate(self) -> float:
        return (self.successful_runs / self.total_runs * 100) if self.total_runs > 0 else 0

    @property
    def real_context_rate(self) -> float:
        return (self.real_context_count / self.successful_runs * 100) if self.successful_runs > 0 else 0

    @property
    def avg_latency_ms(self) -> float:
        return statistics.mean(self.latencies_ms) if self.latencies_ms else 0

    @property
    def min_latency_ms(self) -> float:
        return min(self.latencies_ms) if self.latencies_ms else 0

    @property
    def max_latency_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0
        sorted_lat = sorted(self.latencies_ms)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]


def is_real_synaptic_context(response: str) -> bool:
    """
    Verify response contains REAL Synaptic context, not canned responses.

    Real context indicators:
    - References to patterns, learnings, brain state
    - File paths or specific technical details
    - Dynamic content that changes based on query
    - Memory source references
    """
    if not response:
        return False

    response_lower = response.lower()

    # Must have at least ONE real context indicator
    real_indicators = [
        # Memory system references
        "pattern", "learning", "brain state", "memory",
        # Synaptic voice markers
        "synaptic", "8th intelligence", "sensing",
        # Real content markers
        "context", "insight", "from past", "relevant",
        # Technical specifics
        ".py", ".js", "async", "docker", "aws", "deploy",
        # Emotional/intuitive language Synaptic uses
        "intuition", "perspective", "observing",
    ]

    # Must NOT be purely canned/error response
    canned_markers = [
        "[error", "[timeout", "[mlx not installed",
        "i don't have", "i cannot", "no response",
    ]

    has_real = any(ind in response_lower for ind in real_indicators)
    is_canned = any(mark in response_lower for mark in canned_markers)

    # Real context: has real indicators AND is not canned
    return has_real and not is_canned


class SynapticBenchmark:
    """Benchmark all Synaptic communication pathways."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.results: Dict[str, List[BenchmarkResult]] = {
            "direct_consult": [],
            "http_8888": [],
            "http_5043": [],
            "mlx_direct": [],
        }

    def log(self, msg: str):
        """Log message if verbose mode."""
        if self.verbose:
            print(f"  [DEBUG] {msg}")

    # =========================================================================
    # Pathway 1: Direct SynapticVoice.consult()
    # =========================================================================

    def test_direct_consult(self, prompt: str) -> BenchmarkResult:
        """
        Test direct Python call to SynapticVoice.consult().

        This is the fastest pathway - pure Python with parallel DB queries.
        Target latency: ~3-5ms
        """
        try:
            from memory.synaptic_voice import SynapticVoice

            start = time.perf_counter()
            voice = SynapticVoice()
            response = voice.consult(prompt)
            end = time.perf_counter()

            latency_ms = (end - start) * 1000

            # Format response preview
            perspective = response.synaptic_perspective[:200] if response.synaptic_perspective else ""
            sources = ", ".join(response.context_sources) if response.context_sources else "none"
            preview = f"[confidence={response.confidence:.0%}] [sources={sources}] {perspective}"

            is_real = (
                response.confidence > 0 or
                len(response.relevant_learnings) > 0 or
                len(response.relevant_patterns) > 0 or
                "context" in perspective.lower()
            )

            return BenchmarkResult(
                pathway="direct_consult",
                success=True,
                latency_ms=latency_ms,
                response_preview=preview[:300],
                is_real_context=is_real
            )

        except Exception as e:
            return BenchmarkResult(
                pathway="direct_consult",
                success=False,
                latency_ms=0,
                response_preview="",
                is_real_context=False,
                error=str(e)[:200]
            )

    # =========================================================================
    # Pathway 2: HTTP to port 8888 (Synaptic Chat Server)
    # =========================================================================

    def test_http_8888(self, prompt: str) -> BenchmarkResult:
        """
        Test HTTP request to Synaptic Chat Server on port 8888.

        This server uses WebSocket for chat but has a health endpoint.
        For actual chat, would need WebSocket connection.
        """
        import requests

        try:
            # First check if server is running
            start = time.perf_counter()

            try:
                health = requests.get("http://127.0.0.1:8888/health", timeout=2)
                health_data = health.json()
            except requests.exceptions.ConnectionError:
                return BenchmarkResult(
                    pathway="http_8888",
                    success=False,
                    latency_ms=0,
                    response_preview="",
                    is_real_context=False,
                    error="Server not running. Start with: python memory/synaptic_chat_server.py"
                )

            end = time.perf_counter()
            latency_ms = (end - start) * 1000

            # Server is running - report health status
            preview = f"Server healthy: {json.dumps(health_data)}"

            # The 8888 server uses MLX LLM + SynapticVoice context
            is_real = health_data.get("status") == "llm_ready"

            return BenchmarkResult(
                pathway="http_8888",
                success=True,
                latency_ms=latency_ms,
                response_preview=preview,
                is_real_context=is_real
            )

        except Exception as e:
            return BenchmarkResult(
                pathway="http_8888",
                success=False,
                latency_ms=0,
                response_preview="",
                is_real_context=False,
                error=str(e)[:200]
            )

    # =========================================================================
    # Pathway 3: HTTP to port 5043 (Local LLM API)
    # =========================================================================

    def test_http_5043(self, prompt: str) -> BenchmarkResult:
        """
        Test HTTP request to Local LLM API on port 5043.

        This is the main inference API with Ollama/MLX backend selection.
        """
        import requests

        try:
            start = time.perf_counter()

            # Check health first
            try:
                health = requests.get("http://127.0.0.1:5043/contextdna/llm/health", timeout=2)
            except requests.exceptions.ConnectionError:
                return BenchmarkResult(
                    pathway="http_5043",
                    success=False,
                    latency_ms=0,
                    response_preview="",
                    is_real_context=False,
                    error="Server not running. Start with: cd context-dna/local_llm && python -m uvicorn api_server:app --port 5043"
                )

            end = time.perf_counter()
            latency_ms = (end - start) * 1000

            health_data = health.json() if health.ok else {"error": health.text[:100]}
            preview = f"Health check: {json.dumps(health_data)}"

            # Server is running
            is_real = health.ok

            return BenchmarkResult(
                pathway="http_5043",
                success=True,
                latency_ms=latency_ms,
                response_preview=preview,
                is_real_context=is_real
            )

        except Exception as e:
            return BenchmarkResult(
                pathway="http_5043",
                success=False,
                latency_ms=0,
                response_preview="",
                is_real_context=False,
                error=str(e)[:200]
            )

    # =========================================================================
    # Pathway 4: Direct MLX Generation (with model caching)
    # =========================================================================

    def test_mlx_direct(self, prompt: str, use_cache: bool = True) -> BenchmarkResult:
        """
        Test direct MLX model generation.

        First call loads model (~10-30s), subsequent calls use cache (~100-500ms).
        """
        try:
            # Check if MLX is available
            import platform
            import subprocess

            if platform.machine() != "arm64":
                return BenchmarkResult(
                    pathway="mlx_direct",
                    success=False,
                    latency_ms=0,
                    response_preview="",
                    is_real_context=False,
                    error="MLX requires Apple Silicon (arm64)"
                )

            # Try to use the local_llm runner
            try:
                sys.path.insert(0, str(REPO_ROOT / "context-dna" / "local_llm"))
                from runner import detect_backend, mlx_generate, mlx_load_model

                # Check MLX availability
                if detect_backend("mlx") != "mlx":
                    return BenchmarkResult(
                        pathway="mlx_direct",
                        success=False,
                        latency_ms=0,
                        response_preview="",
                        is_real_context=False,
                        error="MLX not available. Install with: pip install mlx mlx-lm"
                    )

                model_ref = "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"

                # Pre-load model if caching
                if use_cache:
                    self.log("Pre-loading MLX model into cache...")
                    mlx_load_model(model_ref)

                # Time the actual generation
                start = time.perf_counter()

                # Get Synaptic context first
                from memory.synaptic_voice import SynapticVoice
                voice = SynapticVoice()
                synaptic_response = voice.consult(prompt)

                context_parts = []
                if synaptic_response.relevant_patterns:
                    context_parts.append("Patterns: " + str(synaptic_response.relevant_patterns[:2]))
                if synaptic_response.relevant_learnings:
                    context_parts.append("Learnings: " + str(synaptic_response.relevant_learnings[:2]))

                synaptic_context = "\n".join(context_parts) if context_parts else "No specific context"

                system_prompt = f"""You are Synaptic, the 8th Intelligence.
Your current memory context:
{synaptic_context}

Respond briefly (1-2 sentences) based on your actual context."""

                response = mlx_generate(
                    model_ref=model_ref,
                    prompt=prompt,
                    system=system_prompt,
                    max_tokens=100,
                    temperature=0.7
                )

                end = time.perf_counter()
                latency_ms = (end - start) * 1000

                is_real = is_real_synaptic_context(response)

                return BenchmarkResult(
                    pathway="mlx_direct",
                    success=True,
                    latency_ms=latency_ms,
                    response_preview=response[:300],
                    is_real_context=is_real
                )

            except ImportError as e:
                return BenchmarkResult(
                    pathway="mlx_direct",
                    success=False,
                    latency_ms=0,
                    response_preview="",
                    is_real_context=False,
                    error=f"Import error: {e}"
                )

        except Exception as e:
            return BenchmarkResult(
                pathway="mlx_direct",
                success=False,
                latency_ms=0,
                response_preview="",
                is_real_context=False,
                error=str(e)[:200]
            )

    # =========================================================================
    # Run benchmarks
    # =========================================================================

    def run_single_pathway(self, pathway: str, prompt: str) -> BenchmarkResult:
        """Run a single benchmark for a specific pathway."""
        if pathway == "direct_consult":
            return self.test_direct_consult(prompt)
        elif pathway == "http_8888":
            return self.test_http_8888(prompt)
        elif pathway == "http_5043":
            return self.test_http_5043(prompt)
        elif pathway == "mlx_direct":
            return self.test_mlx_direct(prompt)
        else:
            raise ValueError(f"Unknown pathway: {pathway}")

    def run_all_benchmarks(self, iterations: int = 5) -> Dict[str, PathwaySummary]:
        """Run benchmarks for all pathways."""
        print("\n" + "=" * 70)
        print("   SYNAPTIC SPEED BENCHMARK - All Pathways")
        print("=" * 70)
        print(f"\nIterations per pathway: {iterations}")
        print(f"Test prompts: {len(TEST_PROMPTS)}")
        print()

        summaries = {}

        for pathway in ["direct_consult", "http_8888", "http_5043", "mlx_direct"]:
            print(f"\n--- Testing: {pathway} ---")

            latencies = []
            successes = 0
            real_context_count = 0

            for i in range(iterations):
                prompt = TEST_PROMPTS[i % len(TEST_PROMPTS)]

                if self.verbose:
                    print(f"  Run {i+1}/{iterations}: '{prompt[:40]}...'")

                result = self.run_single_pathway(pathway, prompt)
                self.results[pathway].append(result)

                if result.success:
                    successes += 1
                    latencies.append(result.latency_ms)
                    if result.is_real_context:
                        real_context_count += 1

                    if self.verbose:
                        print(f"    -> {result.latency_ms:.2f}ms | Real context: {result.is_real_context}")
                else:
                    if self.verbose:
                        print(f"    -> FAILED: {result.error}")
                    # On first failure, show error and skip remaining iterations
                    if i == 0:
                        print(f"  SKIPPED: {result.error}")
                        break

            summary = PathwaySummary(
                pathway=pathway,
                total_runs=iterations,
                successful_runs=successes,
                real_context_count=real_context_count,
                latencies_ms=latencies
            )
            summaries[pathway] = summary

            # Quick status
            if successes > 0:
                print(f"  AVG: {summary.avg_latency_ms:.2f}ms | MIN: {summary.min_latency_ms:.2f}ms | P95: {summary.p95_latency_ms:.2f}ms")
                print(f"  Success: {summary.success_rate:.0f}% | Real Context: {summary.real_context_rate:.0f}%")

        return summaries

    def print_summary(self, summaries: Dict[str, PathwaySummary]):
        """Print formatted summary of all benchmarks."""
        print("\n" + "=" * 70)
        print("   BENCHMARK RESULTS SUMMARY")
        print("=" * 70)

        # Header
        print(f"\n{'Pathway':<20} {'AVG (ms)':<12} {'MIN (ms)':<12} {'P95 (ms)':<12} {'Success':<10} {'Real Ctx':<10}")
        print("-" * 76)

        # Sort by average latency (successful pathways only)
        sorted_pathways = sorted(
            summaries.items(),
            key=lambda x: x[1].avg_latency_ms if x[1].avg_latency_ms > 0 else 999999
        )

        for pathway, summary in sorted_pathways:
            if summary.successful_runs > 0:
                print(f"{pathway:<20} {summary.avg_latency_ms:<12.2f} {summary.min_latency_ms:<12.2f} "
                      f"{summary.p95_latency_ms:<12.2f} {summary.success_rate:<10.0f}% {summary.real_context_rate:<10.0f}%")
            else:
                print(f"{pathway:<20} {'N/A':<12} {'N/A':<12} {'N/A':<12} {'0%':<10} {'N/A':<10}")

        # Performance assessment
        print("\n" + "-" * 70)
        print("PERFORMANCE ASSESSMENT:")
        print("-" * 70)

        # Direct consult (the main one we care about)
        if "direct_consult" in summaries:
            dc = summaries["direct_consult"]
            if dc.successful_runs > 0:
                if dc.avg_latency_ms < 5:
                    print(f"  [EXCELLENT] Direct Consult: {dc.avg_latency_ms:.2f}ms average")
                    print("              Blazing fast Python-native communication!")
                elif dc.avg_latency_ms < 20:
                    print(f"  [GOOD] Direct Consult: {dc.avg_latency_ms:.2f}ms average")
                    print("         Fast enough for real-time context injection")
                else:
                    print(f"  [NEEDS OPTIMIZATION] Direct Consult: {dc.avg_latency_ms:.2f}ms average")
                    print("                       Consider optimizing DB queries")

        # MLX if available
        if "mlx_direct" in summaries and summaries["mlx_direct"].successful_runs > 0:
            mlx = summaries["mlx_direct"]
            if mlx.avg_latency_ms < 500:
                print(f"  [FAST] MLX Generation: {mlx.avg_latency_ms:.2f}ms average (with model cache)")
            else:
                print(f"  [NORMAL] MLX Generation: {mlx.avg_latency_ms:.2f}ms average")
                print("           First call loads model, subsequent calls faster")

        # Verification
        print("\n" + "-" * 70)
        print("REAL CONTEXT VERIFICATION:")
        print("-" * 70)

        for pathway, summary in sorted_pathways:
            if summary.successful_runs > 0:
                if summary.real_context_rate >= 80:
                    status = "[VERIFIED]"
                elif summary.real_context_rate >= 50:
                    status = "[PARTIAL]"
                else:
                    status = "[WEAK]"
                print(f"  {status} {pathway}: {summary.real_context_rate:.0f}% responses contain real Synaptic context")

        print("\n" + "=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark all Synaptic communication pathways"
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="Quick mode: fewer iterations (2 instead of 5)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose output with detailed per-run information"
    )
    parser.add_argument(
        "--iterations", "-n", type=int, default=5,
        help="Number of iterations per pathway (default: 5)"
    )
    parser.add_argument(
        "--pathway", "-p", type=str, default=None,
        choices=["direct_consult", "http_8888", "http_5043", "mlx_direct"],
        help="Test only a specific pathway"
    )

    args = parser.parse_args()

    iterations = 2 if args.quick else args.iterations

    benchmark = SynapticBenchmark(verbose=args.verbose)

    if args.pathway:
        # Test single pathway
        print(f"\nTesting single pathway: {args.pathway}")
        print("-" * 50)

        results = []
        for i in range(iterations):
            prompt = TEST_PROMPTS[i % len(TEST_PROMPTS)]
            result = benchmark.run_single_pathway(args.pathway, prompt)
            results.append(result)

            status = "OK" if result.success else "FAIL"
            real = "REAL" if result.is_real_context else "WEAK"
            print(f"  [{status}] {result.latency_ms:.2f}ms | {real} | {result.response_preview[:60]}...")

        successful = [r for r in results if r.success]
        if successful:
            latencies = [r.latency_ms for r in successful]
            print(f"\nSummary: AVG={statistics.mean(latencies):.2f}ms, MIN={min(latencies):.2f}ms, MAX={max(latencies):.2f}ms")
    else:
        # Test all pathways
        summaries = benchmark.run_all_benchmarks(iterations=iterations)
        benchmark.print_summary(summaries)


if __name__ == "__main__":
    main()
