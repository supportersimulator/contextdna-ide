#!/usr/bin/env python3
"""
Seed ContextDNA with learnings from ER Simulator documentation.

Run this once to populate the initial knowledge base with:
- Bug fixes from BENCHMARK-RECORD.md
- Architecture decisions from LIVEKIT-CPU-MIGRATION.md
- Performance lessons from optimization work

Usage:
    python memory/seed_context_dna.py
"""

import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.context_dna_client import ContextDNAClient


def seed_context_dna():
    """Seed ContextDNA with known learnings."""
    print("Seeding ContextDNA with ER Simulator learnings...\n")

    memory = ContextDNAClient()

    if not memory.ping():
        print("Error: ContextDNA server not reachable. Start it with: context_dna docker up")
        sys.exit(1)

    print(f"Connected to ContextDNA (Space: {memory.space_id})\n")

    # =========================================================================
    # BUG FIXES
    # =========================================================================
    print("Recording bug fixes...")

    # boto3 async fix (CRITICAL)
    memory.record_bug_fix(
        symptom="6-concurrent LLM requests taking 14.56s total (should be ~1s)",
        root_cause="Synchronous boto3.converse() call blocks the asyncio event loop, causing sequential processing instead of parallel",
        fix="Wrap boto3 calls in asyncio.to_thread(): response = await asyncio.to_thread(bedrock_client.converse, **params)",
        tags=["llm", "async", "boto3", "bedrock", "performance", "critical"],
        file_path="ersim-voice-stack/services/llm/app/main.py:212",
        additional_context="This single fix improved LLM latency by 92% (14.56s → 0.52s). Always use asyncio.to_thread() for synchronous AWS SDK calls in async code."
    )
    print("  ✓ boto3 async fix")

    # Whisper async fix
    memory.record_bug_fix(
        symptom="STT requests blocking other concurrent requests",
        root_cause="faster_whisper model.transcribe() is synchronous and blocks the asyncio event loop",
        fix="Wrap in asyncio.to_thread(): segments, info = await asyncio.to_thread(_transcribe)",
        tags=["stt", "async", "whisper", "performance"],
        file_path="ersim-voice-stack/services/stt/app/main.py:49"
    )
    print("  ✓ Whisper async fix")

    # Soundfile async fix
    memory.record_bug_fix(
        symptom="WAV encoding/decoding blocking audio pipeline",
        root_cause="soundfile sf.read() and sf.write() are blocking I/O operations that block the event loop",
        fix="Wrap soundfile operations in asyncio.to_thread() for both reading and writing",
        tags=["audio", "async", "soundfile", "wav", "io"],
        file_path="ersim-voice-stack/services/agent/app/main.py:562"
    )
    print("  ✓ Soundfile async fix")

    # TTS sample rate mismatch
    memory.record_bug_fix(
        symptom="InvalidState error - sample_rate and num_channels don't match",
        root_cause="Agent initializes LiveKit playout at 48000 Hz but KYUTAI_TTS_SAMPLE_RATE env var was 24000",
        fix="Set KYUTAI_TTS_SAMPLE_RATE=48000 in agent .env (WebRTC standard is 48kHz)",
        tags=["tts", "audio", "sample-rate", "webrtc", "livekit", "kyutai"]
    )
    print("  ✓ TTS sample rate fix")

    # GPU IP change on restart
    memory.record_bug_fix(
        symptom="Agent can't connect to GPU services after GPU restart",
        root_cause="GPU instance gets new private IP when ASG launches new instance. Agent .env has hardcoded IP.",
        fix="Use 'Refresh Agent Config' button in voice toggle, or update agent .env with new GPU IP via SSM. Better: use Internal NLB for stable DNS.",
        tags=["gpu", "networking", "ecs", "asg", "agent", "nlb"]
    )
    print("  ✓ GPU IP change fix")

    # Docker restart doesn't reload env
    memory.record_bug_fix(
        symptom="Docker restart doesn't pick up new environment variables",
        root_cause="docker restart only restarts the container, doesn't reload --env-file. Container keeps old env.",
        fix="Use docker stop && docker rm && docker run (recreate container) to reload env vars from --env-file",
        tags=["docker", "env", "configuration"]
    )
    print("  ✓ Docker env reload fix")

    # ECS task not starting
    memory.record_bug_fix(
        symptom="ECS voice-inference service stuck at 0 running tasks after ASG scale-up",
        root_cause="GPU instance not registered with ECS cluster after ASG launch. ECS agent takes ~60s to register.",
        fix="Wait for ECS agent to register (~60s), then force new deployment: aws ecs update-service --force-new-deployment",
        tags=["ecs", "gpu", "asg", "deployment"]
    )
    print("  ✓ ECS registration fix")

    # Cloudflare proxy breaks API Gateway
    memory.record_bug_fix(
        symptom="API Gateway custom domain returns 403 or SSL errors through Cloudflare proxy",
        root_cause="Cloudflare proxy rewrites requests and breaks API Gateway's TLS termination with ACM certificate",
        fix="Set proxied=false for API Gateway DNS records. Let API Gateway handle TLS directly with ACM cert.",
        tags=["cloudflare", "api-gateway", "dns", "tls", "ssl"]
    )
    print("  ✓ Cloudflare proxy fix")

    # Lambda timing too fast
    memory.record_bug_fix(
        symptom="Lambda GPU toggle times out waiting for GPU to be ready",
        root_cause="GPU startup takes 3-5 minutes total: ASG launch (~60s) + ECS agent (~30s) + image pull (~60s) + container start (~30s)",
        fix="Use 300s max wait with 15s intervals (20 attempts) instead of 120s/10s",
        tags=["lambda", "gpu", "timing", "asg"],
        file_path="ersim-voice-stack/infra/lambda/gpu_toggle.py"
    )
    print("  ✓ Lambda timing fix")

    # =========================================================================
    # ARCHITECTURE DECISIONS
    # =========================================================================
    print("\nRecording architecture decisions...")

    memory.record_architecture_decision(
        decision="Separate LiveKit/Agent onto CPU instance (c6i.xlarge), keep STT/TTS/LLM on GPU (g5.xlarge)",
        rationale="WebRTC competes with CUDA workloads causing latency spikes and WebSocket connection issues. CPU-bound WebRTC shouldn't share GPU resources with ML inference.",
        alternatives=[
            "All services on GPU - causes resource contention",
            "Kubernetes with resource limits - complex for our scale",
            "ECS Fargate for LiveKit - limited networking control"
        ],
        consequences="Adds ~$80/month but eliminates resource contention. Clear separation of concerns."
    )
    print("  ✓ CPU/GPU separation decision")

    memory.record_architecture_decision(
        decision="Use hybrid LLM routing: direct models for low load (≤2 concurrent), cross-region for high load (≥3)",
        rationale="Direct regional models have lowest latency (~0.5s) but limited throughput. Cross-region provides spillover capacity with AWS availability-based routing.",
        alternatives=[
            "Single model - hits quota limits under load",
            "Random rotation - wastes low-latency direct models",
            "All cross-region - higher latency for single users"
        ],
        consequences="Best latency for single users, automatic scaling for concurrent users."
    )
    print("  ✓ Hybrid LLM routing decision")

    memory.record_architecture_decision(
        decision="Use Internal NLB for GPU services instead of hardcoded private IPs",
        rationale="GPU IP changes on ASG restart. NLB provides stable DNS name and automatic target registration when new instances launch.",
        alternatives=[
            "Hardcoded IP with manual updates - error-prone",
            "Service discovery with ECS - complex setup",
            "Route53 private hosted zone - doesn't auto-update"
        ]
    )
    print("  ✓ Internal NLB decision")

    memory.record_architecture_decision(
        decision="Agent connects to LiveKit on localhost (ws://localhost:7880), not public URL",
        rationale="Agent runs on same instance as LiveKit server. Localhost avoids TLS overhead, external routing, and DNS resolution.",
        alternatives=[
            "wss://livekit.ersimulator.com - adds TLS overhead",
            "Internal DNS - unnecessary indirection"
        ]
    )
    print("  ✓ Localhost LiveKit decision")

    memory.record_architecture_decision(
        decision="Use Cloudflare DNS NOT proxied for LiveKit (WebRTC needs direct UDP access)",
        rationale="WebRTC requires direct UDP connections for media. Cloudflare proxy only supports HTTP/HTTPS - it would break UDP media streams.",
        alternatives=["Cloudflare proxied (doesn't work for WebRTC)"],
        consequences="LiveKit DNS record must have orange cloud OFF. No DDoS protection for WebRTC endpoint."
    )
    print("  ✓ Cloudflare WebRTC decision")

    # =========================================================================
    # PERFORMANCE LESSONS
    # =========================================================================
    print("\nRecording performance lessons...")

    memory.record_performance_lesson(
        metric="6-concurrent LLM latency",
        before="14.56s average",
        after="0.52s average",
        technique="asyncio.to_thread() wrapper for synchronous boto3 calls. This is the single most impactful optimization.",
        file_path="ersim-voice-stack/services/llm/app/main.py:216",
        tags=["llm", "async", "boto3", "critical"]
    )
    print("  ✓ LLM async improvement")

    memory.record_performance_lesson(
        metric="Full pipeline (STT→LLM→TTS) at 6-concurrent",
        before="15.23s average",
        after="1.17s average",
        technique="Applied async fixes to all three services: boto3 for LLM, faster_whisper for STT, soundfile for audio I/O.",
        tags=["pipeline", "async", "performance"]
    )
    print("  ✓ Full pipeline improvement")

    memory.record_performance_lesson(
        metric="20-concurrent LLM latency",
        before="3.41s with 26s outliers",
        after="1.00s with no outliers",
        technique="Expanded from 5 to 9 Bedrock models (3 direct + 3 cross-region Haiku variants). More models = more parallel capacity.",
        file_path="ersim-voice-stack/services/llm/app/main.py:35-54",
        tags=["llm", "bedrock", "scaling", "throughput"]
    )
    print("  ✓ Model pool expansion")

    memory.record_performance_lesson(
        metric="LLM response length",
        before="Default 300 tokens",
        after="80 tokens for simulation phase",
        technique="Phase-based max_tokens: intro=200, simulation=80, debrief=250. Shorter responses in simulation phase reduce latency.",
        file_path="ersim-voice-stack/services/agent/app/pipeline.py:40-45",
        tags=["llm", "tokens", "latency"]
    )
    print("  ✓ Phase-based tokens")

    memory.record_performance_lesson(
        metric="LLM streaming TTFB",
        before="Expected faster with streaming",
        after="12.8s streaming vs 1.3s non-streaming (WORSE!)",
        technique="ABANDONED: boto3 streaming is synchronous per-chunk. Non-streaming with asyncio.to_thread() is optimal for our use case.",
        tags=["llm", "streaming", "abandoned"]
    )
    print("  ✓ Streaming lesson (abandoned approach)")

    # =========================================================================
    # SUMMARY
    # =========================================================================
    print("\n" + "=" * 60)
    print("ContextDNA seeded successfully!")
    print("=" * 60)
    print(f"\nSpace ID: {memory.space_id}")
    print("\nTo query learnings:")
    print("  from memory.context_dna_client import ContextDNAClient")
    print("  memory = ContextDNAClient()")
    print("  lessons = memory.get_relevant_learnings('async boto3')")
    print("\nTo get prompt context:")
    print("  context = memory.get_prompt_context(area='llm performance')")


if __name__ == "__main__":
    seed_context_dna()
