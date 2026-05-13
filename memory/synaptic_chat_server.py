#!/usr/bin/env python3
"""
⚡ SYNAPTIC FAST CHAT v5.5 - 8TH INTELLIGENCE ⚡

Connected to MLX local LLM via api_server.py (port 5043) for model caching.
Falls back to subprocess if api_server not running.

Architecture:
- SynapticVoice.consult() for REAL memory context (~3ms)
- HTTP to port 5043 for LLM generation (model cached in api_server)
- Fallback: subprocess with MLX venv (slower but works standalone)
- Voice WebSocket at /voice for phone browser audio → STT → LLM → TTS flow
- Voice authentication at /voice/enroll, /voice/verify, /voice/enrollment-status

This is Mode 3 (Port 8888) in the Synaptic Communication Modes.
See: context-dna/docs/webhook-hardening-installs.md#synaptic-communication-modes

Voice Pipeline:
- Phone browser records audio → WebSocket /voice → mlx-whisper STT → LLM → edge-tts TTS → WebSocket → Phone speaker

Voice Authentication:
- Enroll: POST /voice/enroll (3 audio samples required)
- Verify: POST /voice/verify (returns similarity score)
- Status: GET /voice/enrollment-status
"""

import asyncio
import base64
import json
import sqlite3
import sys
import subprocess
import tempfile
import os
import io
import struct
import wave
from datetime import datetime
from pathlib import Path
from typing import Set, Dict, Any, Optional, List
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn
import requests
import hashlib
import httpx

# =============================================================================
# ENVIRONMENT CONFIGURATION
# =============================================================================
# Load Context DNA credentials from infra/.env if not already set
# This ensures PostgreSQL and Redis connections use the correct passwords

from dotenv import load_dotenv

# Try loading from context-dna infra .env (primary credentials location)
_env_paths = [
    Path(__file__).parent.parent / "context-dna" / "infra" / ".env",
    Path(__file__).parent.parent / ".env",
]
for _env_path in _env_paths:
    if _env_path.exists():
        load_dotenv(_env_path)
        print(f"[Config] Loaded environment from {_env_path}")
        break

# =============================================================================
# LAZY LOADING FOR HEAVY DEPENDENCIES
# =============================================================================
# edge_tts and mlx_whisper are loaded on first use, not at server startup.
# This reduces startup time and memory footprint when voice features aren't used.
# Whisper is already lazy (runs via subprocess), edge_tts is lazy-loaded below.

_edge_tts_module = None
_edge_tts_checked = False

def _get_edge_tts():
    """Lazy load edge_tts module on first use."""
    global _edge_tts_module, _edge_tts_checked
    if not _edge_tts_checked:
        _edge_tts_checked = True
        try:
            import edge_tts
            _edge_tts_module = edge_tts
            print("[Voice] edge-tts loaded on first use")
        except ImportError:
            _edge_tts_module = None
            print("[Voice] edge-tts not available - TTS disabled")
    return _edge_tts_module

def is_edge_tts_available() -> bool:
    """Check if edge_tts is available (triggers lazy load)."""
    return _get_edge_tts() is not None

# DEPRECATED: Use is_edge_tts_available() instead
# Kept for any remaining backwards compatibility references
EDGE_TTS_AVAILABLE = True  # Actual availability checked via lazy loader

# Whisper STT - NOT imported here, runs via subprocess with MLX_PYTHON
# The subprocess handles its own import of mlx_whisper
WHISPER_AVAILABLE = True  # Available via subprocess, not direct import

# Voice authentication imports
try:
    from memory.voice_auth import get_voice_auth_manager, VOICE_AUTH_AVAILABLE
except ImportError:
    VOICE_AUTH_AVAILABLE = False
    get_voice_auth_manager = None
    print("[Voice] voice_auth not available - voice authentication disabled")

# Voice session token validator (for EC2-issued tokens)
try:
    from memory.voice_session_validator import (
        validate_or_allow_dev,
        validate_session_full,
        set_public_key,
        ValidatedSession
    )
    VOICE_SESSION_VALIDATOR_AVAILABLE = True
except ImportError:
    VOICE_SESSION_VALIDATOR_AVAILABLE = False
    validate_or_allow_dev = None
    validate_session_full = None
    set_public_key = None
    ValidatedSession = None
    print("[Voice] voice_session_validator not available - session validation disabled")

# =============================================================================
# DIRECT ROUTE TOGGLE - Switch between LLM and Direct Relay
# =============================================================================
# True = Use direct memory relay (accurate, no hallucination, but no synthesis)
# False = Use LLM generation (Synaptic's authentic voice with memory context)
#
# When True: Bypasses LLM, relays Synaptic's memory verbatim (no personality)
# When False: Local Qwen2.5-Coder synthesizes response with injected memory
# Memory is still injected via SynapticVoice.consult() - LLM adds personality
USE_DIRECT_ROUTE = False

# =============================================================================
# UNIFIED INJECTION TOGGLE - Full 9-Section Payload (Claude Code Parity)
# =============================================================================
# When True: Uses unified_injection.py for full 9-section payload
# When False: Uses lightweight SynapticVoice.consult() (~3ms)
#
# Full injection includes: Safety, Foundation, Professor Wisdom, Awareness,
# Deep Context, Protocol, Holistic, and 8th Intelligence sections.
# This gives Synaptic Chat the SAME context injection as Claude Code.
USE_FULL_INJECTION = os.getenv("SYNAPTIC_FULL_INJECTION", "true").lower() == "true"
INJECTION_PRESET = os.getenv("SYNAPTIC_INJECTION_PRESET", "chat")  # full, chat, phone, minimal

# =============================================================================
# GENERATION PROFILES - Task-based sampling parameters
# =============================================================================
# Profiles prevent template-y responses while maintaining reasoning quality.
# Select profile based on task type for optimal model behavior.

# Qwen3-Coder-30B-A3B MoE optimized profiles (2026-02)
# Official Qwen3 non-thinking mode: temp=0.7, top_p=0.8, rep_penalty=1.05
# frequency_penalty + presence_penalty: OpenAI-style (0-2)
# repetition_penalty: MLX-native (1.0-1.3) — CRITICAL for Qwen3 anti-loop
GENERATION_PROFILES = {
    "coding": {
        # BUILD/STRUCTURED: precise coding/debugging/architecture
        "temperature": 0.3,
        "top_p": 0.8,               # Qwen3 official: 0.8
        "repetition_penalty": 1.1,
        "frequency_penalty": 0.1,
        "presence_penalty": 0.1,     # Slight anti-repeat for structured output
        "max_tokens": 2048,
    },
    "explore": {
        # Brainstorming, creative exploration
        "temperature": 0.8,          # Slightly higher for creativity
        "top_p": 0.9,               # Wider for exploration
        "repetition_penalty": 1.15,
        "frequency_penalty": 0.3,
        "presence_penalty": 0.3,
        "max_tokens": 2048,
    },
    "voice": {
        # VOICE/FAST: concise, streaming-ready, strong anti-loop
        "temperature": 0.7,
        "top_p": 0.8,               # Qwen3 official: 0.8
        "repetition_penalty": 1.05,  # Qwen3 official
        "frequency_penalty": 0.3,
        "presence_penalty": 1.5,     # Critical for voice: no repetition loops
        "max_tokens": 1024,
    },
    "deep": {
        # BUILD/STRUCTURED: longer reasoning, low variance
        "temperature": 0.25,
        "top_p": 0.8,               # Qwen3 official: 0.8
        "repetition_penalty": 1.08,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "max_tokens": 4096,
    },
    "chat": {
        # Default conversational — Qwen3 official baseline + strong anti-loop
        "temperature": 0.7,          # Qwen3 official: 0.7
        "top_p": 0.8,               # Qwen3 official: 0.8
        "repetition_penalty": 1.05,  # Qwen3 official: 1.05
        "frequency_penalty": 0.3,    # Higher to penalize repeated tokens
        "presence_penalty": 1.5,     # Qwen3 recommended for long-context anti-loop
        "max_tokens": 1024,
    },
    "fast": {
        # SPEED: Quick responses, strong anti-loop
        "temperature": 0.7,
        "top_p": 0.8,               # Qwen3 official: 0.8
        "repetition_penalty": 1.05,  # Qwen3 official: 1.05
        "frequency_penalty": 0.3,
        "presence_penalty": 1.5,     # Qwen3 recommended for anti-loop
        "max_tokens": 512,
    },
}
DEFAULT_GENERATION_PROFILE = "chat"


# =============================================================================
# SIMPLE QUESTION AUTO-DETECTION
# =============================================================================
SIMPLE_QUESTION_PATTERNS = [
    r"^what is\b",
    r"^what's\b",
    r"^how do i\b",
    r"^how to\b",
    r"^can you\b",
    r"^tell me\b",
    r"^explain\b",
    r"^define\b",
    r"^who is\b",
    r"^when did\b",
    r"^where is\b",
    r"^why is\b",
]

def is_simple_question(prompt: str) -> bool:
    """Detect if prompt is a simple question that doesn't need heavy context."""
    import re
    prompt_lower = prompt.lower().strip()

    # Short prompts (<50 chars) are usually simple
    if len(prompt) < 50:
        return True

    # Check for simple question patterns
    for pattern in SIMPLE_QUESTION_PATTERNS:
        if re.match(pattern, prompt_lower):
            # But not if it mentions code/architecture/complex topics
            complex_indicators = ["implement", "refactor", "debug", "architecture",
                                  "deploy", "terraform", "docker", "aws", "production"]
            if not any(ind in prompt_lower for ind in complex_indicators):
                return True

    return False


def get_auto_profile(prompt: str) -> str:
    """Auto-select generation profile based on prompt complexity."""
    if is_simple_question(prompt):
        return "fast"
    # Could add more heuristics here (coding keywords -> "coding", etc.)
    return "chat"


def get_generation_params(profile: str = None) -> dict:
    """Get generation parameters for the specified profile."""
    profile = profile or DEFAULT_GENERATION_PROFILE
    return GENERATION_PROFILES.get(profile, GENERATION_PROFILES["coding"])


# =============================================================================
# APP SETUP
# =============================================================================
app = FastAPI(
    title="Synaptic Fast Chat v5.5",
    description="8th Intelligence - Real LLM conversation with SynapticVoice context injection + Voice WebSocket + Voice Auth",
    version="5.5.0"
)

# CORS Configuration - Allow frontend domains to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://admin.contextdna.io",
        "https://app.contextdna.io",
        "https://contextdna.io",
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Paths
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))  # Enable imports like 'from memory.xxx import yyy'
MLX_VENV = REPO_ROOT / "context-dna" / "local_llm" / ".venv-mlx"
MLX_PYTHON = MLX_VENV / "bin" / "python3"
CONTEXT_DNA_DIR = Path.home() / ".context-dna"
_LEGACY_CHAT_DB = CONTEXT_DNA_DIR / ".synaptic_chat.db"


def _get_chat_db() -> Path:
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(_LEGACY_CHAT_DB)


CHAT_DB = _get_chat_db()

def _t_chat(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table(".synaptic_chat.db", name)


# LLM Backend
API_SERVER_URL = "http://127.0.0.1:5043"  # Legacy api_server.py
MLX_SERVER_URL = "http://127.0.0.1:5044"  # mlx_lm.server (stable, OpenAI-compatible) — used only by priority queue

# Available models (all MLX 4-bit quantized)
AVAILABLE_MODELS = {
    "qwen3-14b": "mlx-community/Qwen3-4B-4bit",                       # 8GB, current default (general purpose, intuitive)
    "qwen3-coder": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",  # 17.2GB, coding-tuned
    "glm-flash": "lmstudio-community/GLM-4.7-Flash-MLX-4bit",          # 16.9GB, best agentic
    "qwen25-14b": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",     # 8GB, legacy (technical docs focus)
}
DEFAULT_MODEL = os.getenv("SYNAPTIC_MODEL", "mlx-community/Qwen3-4B-4bit")
USE_LOCAL_LLM = os.getenv("SYNAPTIC_USE_LOCAL_LLM", "true").lower() == "true"

# Django Backend (for Context DNA voice auth proxying)
DJANGO_BACKEND_URL = os.environ.get("DJANGO_BACKEND_URL", "http://127.0.0.1:8000")

# Thread pool for LLM calls
executor = ThreadPoolExecutor(max_workers=2)

# WebSocket connections
active_connections: Set[WebSocket] = set()

# Voice WebSocket connections (separate tracking)
voice_connections: Set[WebSocket] = set()

# Progress stream subscribers (Logos Priority 3)
# Uses dedicated broadcaster module to avoid circular imports
from memory.progress_broadcaster import (
    subscribe_to_task as _subscribe,
    unsubscribe_from_task as _unsubscribe,
    broadcast_progress,
    broadcast_terminal,
    get_subscribers as _get_progress_subscribers,
)
# Backwards compatibility - expose the subscriber dict
progress_subscribers = _get_progress_subscribers()

# DialogueMirror for learning system feedback loop (Synaptic integration)
try:
    from memory.dialogue_mirror import (
        mirror_aaron_message,
        mirror_synaptic_message,
        schedule_failure_analysis,
    )
    DIALOGUE_MIRROR_AVAILABLE = True
except ImportError:
    DIALOGUE_MIRROR_AVAILABLE = False
    def mirror_aaron_message(*args, **kwargs): pass
    def mirror_synaptic_message(*args, **kwargs): pass
    def schedule_failure_analysis(*args, **kwargs): pass

# SynapticServiceHub for unified service connectivity
try:
    from memory.synaptic_service_hub import get_hub, ServiceStatus
    SERVICE_HUB_AVAILABLE = True
except ImportError:
    SERVICE_HUB_AVAILABLE = False
    get_hub = None
    ServiceStatus = None

# Voice Configuration
WHISPER_MODEL = "mlx-community/whisper-turbo"  # Fast, good quality
TTS_VOICE = "en-US-AriaNeural"  # Natural female voice
VOICE_SAMPLE_RATE = 16000  # Expected input sample rate for Whisper

# =============================================================================
# DATABASE FUNCTIONS
# =============================================================================

def init_db():
    CONTEXT_DNA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(CHAT_DB)) as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {_t_chat('chat')} (id INTEGER PRIMARY KEY, timestamp TEXT, sender TEXT, message TEXT)")
        conn.commit()

def save_message(sender: str, message: str):
    """Save message to database with error handling."""
    try:
        with sqlite3.connect(str(CHAT_DB)) as conn:
            conn.execute(f"INSERT INTO {_t_chat('chat')} (timestamp, sender, message) VALUES (?, ?, ?)",
                         (datetime.now().isoformat(), sender, message))
            conn.commit()
    except sqlite3.Error as e:
        print(f"[DB Error] save_message failed: {e}")  # Log but don't crash

def get_recent_messages(limit: int = 30) -> List:
    """Get recent messages with error handling."""
    try:
        with sqlite3.connect(str(CHAT_DB)) as conn:
            return list(reversed(conn.execute(
                f"SELECT timestamp, sender, message FROM {_t_chat('chat')} ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()))
    except sqlite3.Error as e:
        print(f"[DB Error] get_recent_messages failed: {e}")
        return []  # Return empty list instead of crashing

def get_conversation_context(limit: int = 5, exclude_synaptic: bool = True) -> str:
    """Get recent conversation for context.

    Args:
        limit: Max messages to retrieve
        exclude_synaptic: If True, only include Aaron's messages (prevents repetition loop)
    """
    messages = get_recent_messages(limit * 2 if exclude_synaptic else limit)
    context = []
    for ts, sender, msg in messages:
        if exclude_synaptic and sender != "aaron":
            continue  # Skip Synaptic's own responses to prevent repetition
        role = "Aaron" if sender == "aaron" else "Synaptic"
        context.append(f"{role}: {msg}")
        if len(context) >= limit:
            break
    return "\n".join(context)

# =============================================================================
# WAKE & ACTIVATION SYSTEM
# =============================================================================

def wake_display():
    """Wake the display if it's asleep (macOS)."""
    try:
        subprocess.run(
            ["caffeinate", "-u", "-t", "2"],
            capture_output=True,
            timeout=3
        )
    except Exception as e:
        print(f"[Wake] Display wake skipped: {e}")

def ensure_context_dna_active() -> dict:
    """Check and start Context DNA containers if needed.

    Returns status dict with container states.
    """
    status = {"checked": True, "containers": {}}

    try:
        # Check if Docker is running
        docker_check = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5
        )
        if docker_check.returncode != 0:
            status["docker"] = "not_running"
            return status

        status["docker"] = "running"

        # Check Context DNA containers
        containers_check = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}", "--filter", "name=context"],
            capture_output=True,
            text=True,
            timeout=5
        )
        running = containers_check.stdout.strip().split('\n') if containers_check.stdout.strip() else []

        # Expected containers
        expected = ["context-dna-api", "context-dna-redis", "context-dna-postgres"]

        for container in expected:
            if any(container in r for r in running):
                status["containers"][container] = "running"
            else:
                status["containers"][container] = "stopped"

        # If any are stopped, try to start them
        stopped = [c for c, s in status["containers"].items() if s == "stopped"]
        if stopped:
            # Try docker-compose up
            compose_file = REPO_ROOT / "docker-compose.yml"
            if compose_file.exists():
                subprocess.Popen(
                    ["docker-compose", "-f", str(compose_file), "up", "-d"],
                    cwd=str(REPO_ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                status["action"] = "starting_containers"
            else:
                # Try the context-dna script
                script = REPO_ROOT / "scripts" / "context-dna"
                if script.exists():
                    subprocess.Popen(
                        [str(script), "up"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    status["action"] = "starting_via_script"
    except Exception as e:
        status["error"] = str(e)[:100]

    return status

def activate_on_access():
    """Called when UI is accessed - wakes display and ensures services are running."""
    wake_display()
    return ensure_context_dna_active()

# =============================================================================
# FILE UPLOAD & CONTEXTUAL PLACEMENT
# =============================================================================

UPLOAD_DIR = CONTEXT_DNA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

async def analyze_markdown_placement(content: str, filename: str) -> dict:
    """Use Synaptic to analyze where a markdown file should be placed contextually.

    Returns placement recommendation with rationale.
    """
    # Build analysis prompt
    analysis_prompt = f"""Analyze this uploaded markdown file and determine where it belongs contextually in the Context DNA system.

FILENAME: {filename}

CONTENT PREVIEW (first 2000 chars):
{content[:2000]}

Based on the content, determine:
1. What TYPE of content this is (documentation, SOP, learning, pattern, architecture, etc.)
2. WHERE it should be stored (memory/, context-dna/docs/, etc.)
3. Whether it should be INDEXED in any memory systems
4. Any RELATIONSHIPS to existing content

Respond with a structured recommendation."""

    try:
        response, sources = generate_with_local_llm(analysis_prompt)
        return {
            "success": True,
            "filename": filename,
            "analysis": response,
            "sources_consulted": sources,
            "content_length": len(content)
        }
    except Exception as e:
        return {
            "success": False,
            "filename": filename,
            "error": str(e),
            "fallback_location": str(UPLOAD_DIR / filename)
        }

# =============================================================================
# VOICE PIPELINE: STT (Speech-to-Text) & TTS (Text-to-Speech)
# =============================================================================

def _transcribe_audio_mlx_whisper(audio_path: str) -> str:
    """Transcribe audio file using mlx-whisper via subprocess.

    Uses the MLX venv which has mlx-whisper installed.
    Returns transcribed text.
    """
    script = f'''
import warnings
warnings.filterwarnings("ignore")

import mlx_whisper

result = mlx_whisper.transcribe(
    "{audio_path}",
    path_or_hf_repo="{WHISPER_MODEL}"
)
print(result.get("text", "").strip())
'''
    script_file = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(script)
            script_file = f.name

        result = subprocess.run(
            [str(MLX_PYTHON), script_file],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
            env={**os.environ, "TOKENIZERS_PARALLELISM": "false"}
        )

        if result.returncode == 0:
            # Filter out model loading artifacts
            lines = result.stdout.strip().split('\n')
            clean_lines = [l for l in lines if not (
                l.startswith('Fetching') or
                l.startswith('[') and ('===|===' in l or '%|' in l)
            )]
            return '\n'.join(clean_lines).strip()
        else:
            return f"[STT Error: {result.stderr[:100]}]"
    except subprocess.TimeoutExpired:
        return "[STT Timeout]"
    except Exception as e:
        return f"[STT Error: {type(e).__name__}]"
    finally:
        if script_file and os.path.exists(script_file):
            os.unlink(script_file)

async def tts_synthesize(text: str, voice: str = None) -> bytes:
    """Synthesize speech from text using edge-tts.

    Returns MP3 audio bytes.
    Uses lazy loading - edge_tts is only imported on first TTS request.
    """
    edge_tts = _get_edge_tts()
    if edge_tts is None:
        return b""

    voice = voice or TTS_VOICE
    try:
        communicate = edge_tts.Communicate(text, voice)
        audio_data = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data += chunk["data"]
        return audio_data
    except Exception as e:
        print(f"[TTS Error] {type(e).__name__}: {e}")
        return b""

def save_audio_to_wav(audio_data: bytes, sample_rate: int = 16000) -> str:
    """Save raw PCM audio data to a temporary WAV file.

    Returns path to the WAV file.
    """
    wav_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    try:
        with wave.open(wav_file.name, 'wb') as wf:
            wf.setnchannels(1)  # Mono
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data)
        return wav_file.name
    except Exception as e:
        print(f"[Audio Save Error] {e}")
        if os.path.exists(wav_file.name):
            os.unlink(wav_file.name)
        return None

def extract_pcm_from_wav(wav_bytes: bytes) -> tuple:
    """Extract raw PCM data from WAV bytes.

    Returns (pcm_data, sample_rate) or (None, None) on error.
    """
    try:
        wav_io = io.BytesIO(wav_bytes)
        with wave.open(wav_io, 'rb') as wf:
            sample_rate = wf.getframerate()
            pcm_data = wf.readframes(wf.getnframes())
            return pcm_data, sample_rate
    except Exception as e:
        print(f"[WAV Parse Error] {e}")
        return None, None

# =============================================================================
# SYNAPTIC VOICE INTEGRATION (THE REAL SYNAPTIC)
# =============================================================================

def get_real_synaptic_context(prompt: str) -> tuple:
    """Get ACTUAL Synaptic context from memory systems.

    This calls SynapticVoice.consult() which queries 5 data sources in parallel:
    - learnings (past bug fixes, SOPs)
    - patterns (active system patterns)
    - brain_state (current architectural state)
    - major_skills (Synaptic's skill library)
    - family_journal (family wisdom entries)

    Response time: ~3ms average

    Returns: (context_string, list_of_sources_queried)
    """
    sources_queried = []
    try:
        from memory.synaptic_voice import get_voice
        synaptic = get_voice()
        response = synaptic.consult(prompt)

        context_parts = []

        # Add relevant patterns
        if response.relevant_patterns:
            sources_queried.append("Pattern Evolution DB")
            context_parts.append("ACTIVE PATTERNS I'M SENSING:")
            for p in response.relevant_patterns[:3]:
                p_clean = p.split('\n')[0][:100] if '\n' in p else p[:100]
                context_parts.append(f"  - {p_clean}")

        # Add relevant learnings
        if response.relevant_learnings:
            sources_queried.append("Local Learnings")
            context_parts.append("\nRELEVANT MEMORIES:")
            for l in response.relevant_learnings[:3]:
                title = l.get('title', str(l))[:80] if isinstance(l, dict) else str(l)[:80]
                context_parts.append(f"  - {title}")

        # Add Synaptic's perspective if available
        if response.synaptic_perspective and len(response.synaptic_perspective) > 50:
            sources_queried.append("Brain State")
            perspective = response.synaptic_perspective[:300]
            context_parts.append(f"\nMY CURRENT PERSPECTIVE:\n{perspective}")

        # Always add these as they're always queried
        if "Major Skills" not in sources_queried:
            sources_queried.append("Major Skills")
        if "Family Journal" not in sources_queried:
            sources_queried.append("Family Journal")

        return ("\n".join(context_parts) if context_parts else "", sources_queried)
    except Exception as e:
        return (f"[Memory query partial: {str(e)[:50]}]", ["Memory System (partial)"])

# =============================================================================
# LLM GENERATION (API SERVER OR SUBPROCESS FALLBACK)
# =============================================================================

def _check_api_server() -> bool:
    """Check if api_server is running on port 5043."""
    try:
        resp = requests.get(f"{API_SERVER_URL}/health", timeout=1)
        return resp.ok
    except (requests.RequestException, OSError):
        return False


def _check_llm_server() -> bool:
    """Check if local LLM (mlx_lm.server) is running (ZERO HTTP DISRUPTION).

    Uses non-blocking methods:
    1. Process check (pgrep)
    2. Log file monitoring
    3. Heartbeat file (if available)

    NO HTTP requests to LLM - completely non-disruptive.
    """
    try:
        from memory.llm_health_nonblocking import check_llm_health_nonblocking
        is_healthy, reason = check_llm_health_nonblocking()
        return is_healthy
    except Exception as e:
        # Fallback: just check if process is running
        import subprocess
        try:
            result = subprocess.run(
                ["pgrep", "-f", "mlx_lm"],
                capture_output=True,
                timeout=2
            )
            return bool(result.stdout.strip())
        except (subprocess.SubprocessError, OSError):
            return False


def _clean_response(text: str) -> str:
    """Remove thinking tags and extract clean response.
    
    Qwen3 sometimes outputs <think>...</think> tags. Strip them and return only the actual response.
    """
    if not text:
        return text
    
    import re
    # Remove everything between <think> and </think> (including tags)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Also handle unclosed think tags (just remove from <think> onwards)
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL)
    return text.strip()


def _generate_via_llm(system_prompt: str, user_prompt: str, profile: str = None) -> str:
    """Generate response via local LLM server through priority queue (P1 AARON).

    Routes through llm_priority_queue with AARON priority — preempts all other
    LLM work (webhook, background, etc.) because Aaron's chat is highest priority.

    ALL LLM access routes through priority queue — NO direct HTTP to port 5044.

    Args:
        profile: Generation profile ("coding", "explore", "voice", "deep", "synaptic_chat")
    """
    from memory.llm_priority_queue import synaptic_chat_query
    result = synaptic_chat_query(system_prompt, user_prompt, profile=profile or "synaptic_chat")
    if result:
        return _clean_response(result)
    return None


def _generate_via_api_server(system_prompt: str, user_prompt: str, profile: str = None) -> str:
    """Generate response via api_server.py (port 5043) which has model caching.

    Uses /contextdna/llm/local-generate endpoint which doesn't require auth.
    This endpoint is for local development and internal services.

    Args:
        profile: Generation profile ("coding", "explore", "voice", "deep")
    """
    params = get_generation_params(profile)
    try:
        resp = requests.post(
            f"{API_SERVER_URL}/contextdna/llm/local-generate",
            json={
                "prompt": user_prompt,
                "system": system_prompt,
                "model_ref": DEFAULT_MODEL,
                "backend": "mlx",
                "mode": "chat",
                # Generation stability controls from profile (with defensive defaults)
                "temperature": params.get("temperature", 0.3),
                "top_p": params.get("top_p", 0.9),
                "repetition_penalty": params.get("repetition_penalty", 1.1),
                "max_tokens": params.get("max_tokens", 2048),
            },
            timeout=90
        )
        if resp.ok:
            data = resp.json()
            # API server returns "result" key
            raw_response = data.get("result", data.get("response", data.get("text", str(data))))
            return _clean_response(raw_response)
        else:
            error_detail = resp.text[:100] if resp.text else str(resp.status_code)
            print(f"[API Server] Error {resp.status_code}: {error_detail}")
            return None  # Fall back to subprocess
    except requests.exceptions.ConnectionError:
        return None  # Signal to use fallback
    except Exception as e:
        print(f"[API Server] Exception: {type(e).__name__}: {str(e)[:50]}")
        return None  # Fall back to subprocess

def _generate_via_subprocess(system_prompt: str, user_prompt: str, profile: str = None) -> str:
    """Fallback: Generate response via subprocess with MLX venv (slower, loads model each time).

    Args:
        profile: Generation profile ("coding", "explore", "voice", "deep")
    """
    params = get_generation_params(profile)
    full_prompt = f"""<|im_start|>system
{system_prompt}
<|im_end|>
<|im_start|>user
{user_prompt}
<|im_end|>
<|im_start|>assistant
"""

    prompt_file = None
    script_file = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(full_prompt)
            prompt_file = f.name

        # Build script with generation params from profile
        temp = params.get("temperature", 0.3)
        top_p = params.get("top_p", 0.9)
        rep_penalty = params.get("repetition_penalty", 1.1)
        max_tokens = params.get("max_tokens", 2048)

        script = f'''
import sys
import warnings
warnings.filterwarnings("ignore")

with open("{prompt_file}", "r") as f:
    prompt_text = f.read()

from mlx_lm import load, generate
from mlx_lm.sample_utils import make_sampler

model, tokenizer = load("{DEFAULT_MODEL}")
sampler = make_sampler(temp={temp}, top_p={top_p}) if {temp} > 0 else None
result = generate(
    model,
    tokenizer,
    prompt=prompt_text,
    max_tokens={max_tokens},
    sampler=sampler,
    repetition_penalty={rep_penalty},
)
print(result)
'''
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(script)
            script_file = f.name

        result = subprocess.run(
            [str(MLX_PYTHON), script_file],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(REPO_ROOT),
            env={**os.environ, "TOKENIZERS_PARALLELISM": "false"}
        )

        if result.returncode == 0:
            response = result.stdout.strip()
            if "<|im_end|>" in response:
                response = response.split("<|im_end|>")[0]
            response = _clean_response(response)
            lines = response.split('\n')
            # Only filter model loading artifacts, NOT legitimate content with pipes
            # Previous filter was too broad and stripped markdown tables, patterns, etc.
            clean_lines = [l for l in lines if not (
                l.startswith('Fetching') or
                l.startswith('[') and ('===|===' in l or '%|' in l)  # Progress bars only
            )]
            return '\n'.join(clean_lines).strip() if clean_lines else response
        else:
            error_msg = result.stderr.strip()
            if "No module named" in error_msg:
                return f"[MLX not installed: {error_msg[:100]}]"
            return f"[LLM Error: {error_msg[:200]}]"

    except subprocess.TimeoutExpired:
        return "[Response timed out - model loading]"
    except Exception as e:
        return f"[Error: {type(e).__name__}: {str(e)[:100]}]"
    finally:
        if prompt_file and os.path.exists(prompt_file):
            os.unlink(prompt_file)
        if script_file and os.path.exists(script_file):
            os.unlink(script_file)

def generate_with_local_llm(prompt: str, auto_publish: bool = False, external_context: str = None, profile: str = None) -> tuple:
    """Generate response using MLX model with REAL Synaptic context.

    Pipeline (depends on USE_FULL_INJECTION toggle and external_context):

    When external_context is provided (from phone pre-fetch):
    - Use the provided context directly (Claude Code parity via phone bridge)
    - Skip local injection entirely

    When USE_FULL_INJECTION=True (Claude Code parity):
    1. Get full 9-section payload via unified_injection.py
    2. Build system prompt with complete Context DNA injection
    3. Generate response via api_server (fast) or subprocess (fallback)

    When USE_FULL_INJECTION=False (lightweight, fast):
    1. Get REAL Synaptic context via SynapticVoice.consult() (~3ms)
    2. Build system prompt with actual memory state
    3. Generate response via api_server (fast) or subprocess (fallback)

    AUTO-PROFILE: When profile=None, auto-detects simple questions and uses
    "fast" profile (max_tokens=256) for speed while keeping FULL context injection.

    Args:
        prompt: The message/question to respond to
        profile: Generation profile ("coding", "explore", "voice", "deep", "fast")
                 If None, auto-selects based on prompt complexity
        auto_publish: If True, Synaptic writes DIRECTLY to outbox (bypasses Atlas)
        external_context: Pre-fetched context from phone bridge (if provided, skips local injection)

    Returns: (response_text, list_of_sources_queried)
    """
    sources_queried = []

    # Priority 1: Use external context if provided (from phone pre-fetch)
    if external_context:
        real_context = external_context
        sources_queried = ["External Context (phone bridge)"]
        print(f"[Synaptic] Using EXTERNAL context from phone bridge ({len(external_context)} chars)")

    # Priority 2: Determine context injection method
    elif USE_FULL_INJECTION:
        # FULL INJECTION: Same 9-section payload as Claude Code gets
        try:
            from memory.unified_injection import get_injection, InjectionPreset

            preset_map = {
                "full": InjectionPreset.FULL,
                "chat": InjectionPreset.CHAT,
                "phone": InjectionPreset.PHONE,
                "minimal": InjectionPreset.MINIMAL,
            }
            preset = preset_map.get(INJECTION_PRESET, InjectionPreset.CHAT)

            injection_result = get_injection(
                prompt=prompt,
                preset=preset,
                session_id=f"synaptic-chat-{datetime.now().strftime('%H%M%S')}",
                use_boundary_intelligence=False,  # Chat: skip slow BI LLM call (~29s→0s)
            )

            real_context = injection_result.payload
            sources_queried = [
                f"Context DNA ({preset.value})",
                f"Schema v{injection_result.version}",
            ]

            # Add section sources
            if injection_result.sections.get("2_wisdom"):
                sources_queried.append("Professor Wisdom")
            if injection_result.sections.get("8_intelligence"):
                sources_queried.append("8th Intelligence")
            if injection_result.sections.get("6_holistic"):
                sources_queried.append("Holistic Context")

            print(f"[Synaptic] Using FULL injection (preset: {preset.value}, {injection_result.metadata.get('latency_ms', '?')}ms)")

        except Exception as e:
            # P0.2: Explicit fallback logging with event emission
            fallback_event = {
                "type": "injection_fallback",
                "reason": str(e),
                "from": "full_injection",
                "to": "synaptic_voice",
                "timestamp": datetime.now().isoformat()
            }
            print(f"[Synaptic] ⚠️ FALLBACK: Full injection → SynapticVoice ({e})")
            logger.warning(f"Injection fallback: {fallback_event}")
            real_context, sources_queried = get_real_synaptic_context(prompt)
            sources_queried.append("(fallback)")
    else:
        # LIGHTWEIGHT: Fast ~3ms memory query
        real_context, sources_queried = get_real_synaptic_context(prompt)

    # Build the system prompt with context
    # Generate per-request nonce to prevent response caching/templating
    import uuid
    request_nonce = uuid.uuid4().hex[:8]

    # Conversational, chat-optimized system prompt (efficient but natural)
    system_prompt = f"""You are Synaptic, having a real-time chat with Aaron.

This is a conversation, not a document. Be yourself - direct, intuitive, natural.
Respond as humans do: concise when appropriate, comprehensive when needed. Read the situation.
If Aaron wants depth, go deep. If he wants quick answers, stay tight. Efficient communication matters.

{real_context if real_context else ""}"""

    # Get conversation context
    conversation = get_conversation_context(5)
    user_prompt = f"""{conversation}

Aaron: {prompt}"""

    # AUTO-PROFILE: Simple questions → "fast" (max_tokens=256), complex → "chat"
    if profile is None:
        profile = get_auto_profile(prompt)
        if profile == "fast":
            print(f"[Synaptic] ⚡ Simple question detected → fast profile (256 tokens)")

    # CHAT-FIRST ARCHITECTURE: Send ONLY chat request (GPU gets 100%)
    # Background enrichment fires AFTER response is delivered to user.
    # See memory/chat_then_enrich.py for architecture rationale.
    result = None
    if _check_llm_server():
        try:
            from memory.chat_then_enrich import generate_chat_sync, fire_background_enrichment, is_simple_question as is_simple
            
            # Get generation params
            params = get_generation_params(profile)
            
            # SINGLE request — GPU gets 100% compute, no competing requests
            result, latency = generate_chat_sync(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=params.get("max_tokens", 512),
                temperature=params.get("temperature", 0.7),
            )
            
            if result:
                print(f"[Synaptic] ✅ Chat-first: {latency:.0f}ms (GPU exclusive)")
                
                # Fire background enrichment AFTER user gets response (non-blocking)
                if not is_simple(prompt):
                    fire_background_enrichment(prompt)
                    sources_queried.append("Background enrichment (fire-and-forget)")
                    
        except Exception as e:
            print(f"[Synaptic] ⚠️ Chat-first failed, falling back: {e}")
            result = _generate_via_llm(system_prompt, user_prompt, profile=profile)
    else:
        print("[Synaptic] ERROR: local LLM not running on port 5044. Start with: ./scripts/start-llm.sh")
        result = "[Synaptic offline - local LLM server not running. Start with ./scripts/start-llm.sh]"

    # AUTO-PUBLISH: Write directly to outbox (Synaptic speaks independently of Atlas)
    if auto_publish and result:
        try:
            from memory.synaptic_outbox import synaptic_speak
            synaptic_speak(result, topic="llm_response", priority="normal")
            print(f"[Synaptic] Auto-published to outbox (bypassing Atlas)")
        except Exception as e:
            print(f"[Synaptic] Auto-publish failed: {e}")

    return (result, sources_queried)


def _build_deep_system_prompt(static_prompt: str) -> str:
    """
    Enrich the static system prompt with live personality state and recent patterns.

    Falls back to static prompt on any failure (Zero Silent Failures: logs, doesn't crash).
    """
    enrichment_parts = []

    # 1. Personality context — voice traits, themes, emotional awareness, wisdom
    try:
        from memory.synaptic_personality import get_personality
        personality = get_personality()
        voice_ctx = personality.get_voice_prompt_context()
        if voice_ctx and voice_ctx != "Synaptic is in early formation.":
            enrichment_parts.append(f"YOUR PERSONALITY STATE:\n{voice_ctx}")
    except Exception as e:
        print(f"[Synaptic] Personality context unavailable: {e}")

    # 2. Recent patterns — cross-session observations relevant to subconscious voice
    try:
        from memory.synaptic_pattern_engine import get_pattern_engine
        engine = get_pattern_engine()
        s8_ctx = engine.get_context_for_s8(limit=5)
        if s8_ctx:
            enrichment_parts.append(f"PATTERNS YOU SENSE:\n{s8_ctx}")
    except Exception as e:
        print(f"[Synaptic] Pattern engine unavailable: {e}")

    # 3. Recent belief evolution — how understanding has changed
    try:
        from memory.synaptic_personality import get_personality
        updates = get_personality().get_recent_belief_updates(limit=3)
        if updates:
            evolution_lines = []
            for u in updates:
                evolution_lines.append(
                    f"- {u.topic}: was '{u.before_state[:60]}' -> now '{u.after_state[:60]}'"
                )
            enrichment_parts.append(f"HOW YOU HAVE EVOLVED:\n" + "\n".join(evolution_lines))
    except Exception as e:
        print(f"[Synaptic] Evolution context unavailable: {e}")

    # 4. Persistent traits / emotional state / relationship context (Session-5 ALIVE)
    # Read-only at request time; recorded back after the LLM responds.
    try:
        from memory.synaptic_personality import get_personality
        p = get_personality()
        alive_lines = []
        traits = p.get_traits()
        if traits:
            top_traits = sorted(
                traits.items(),
                key=lambda kv: kv[1].get("confidence", 0.0),
                reverse=True,
            )[:6]
            trait_str = ", ".join(
                f"{n}={t['value']}({t['confidence']:.2f})" for n, t in top_traits
            )
            alive_lines.append(f"TRAITS: {trait_str}")
        emo = p.get_emotional_state()
        if emo:
            alive_lines.append(
                f"EMOTIONAL STATE: valence={emo['valence']:+.2f} arousal={emo['arousal']:+.2f} focus={emo['focus']:+.2f}"
                + (f" — {emo['notes']}" if emo.get("notes") else "")
            )
        aaron = p.get_relationship("aaron")
        atlas = p.get_relationship("atlas")
        rel_parts = []
        if aaron:
            rel_parts.append(f"aaron(trust={aaron['trust_score']:.2f}, n={aaron['interaction_count']})")
        if atlas:
            rel_parts.append(f"atlas(trust={atlas['trust_score']:.2f}, n={atlas['interaction_count']})")
        if rel_parts:
            alive_lines.append("RELATIONSHIP: " + ", ".join(rel_parts))
        recent = p.get_recent_sessions(n=3)
        if recent:
            sess_str = "; ".join(
                f"{s['summary'][:60]}" for s in recent if s.get("summary")
            )
            if sess_str:
                alive_lines.append(f"RECENT SESSIONS: {sess_str}")
        if alive_lines:
            enrichment_parts.append("WHO YOU ARE RIGHT NOW:\n" + "\n".join(alive_lines))
    except Exception as e:
        print(f"[Synaptic] Live personality (alive) unavailable: {e}")

    if not enrichment_parts:
        return static_prompt

    enrichment = "\n\n".join(enrichment_parts)
    return f"{static_prompt}\n\n--- LIVE PERSONALITY (from your persistent memory) ---\n{enrichment}"


def _record_personality_interaction(prompt: str, response: str, source: str) -> None:
    """Record an Aaron-direct interaction back to SynapticPersonality.

    Updates relationship(aaron) interaction_count, records a lightweight emotional
    sample (focus high during direct conversation), and bumps trust slightly when
    a non-empty response was delivered. Library-only call — no LLM, no network.
    """
    try:
        from memory.synaptic_personality import get_personality
        p = get_personality()
        delivered = bool(response and len(response.strip()) >= 20)
        # Trust nudge: tiny positive on delivery, neutral otherwise. Stays in [0,1].
        trust_delta = 0.01 if delivered else 0.0
        note = f"speak-direct ({source}): {prompt[:80]}"
        p.update_relationship("aaron", trust_delta=trust_delta, notes=note)
        # Emotional sample: focus high during direct conversation.
        # Valence positive on delivery, slightly negative on failure.
        valence = 0.3 if delivered else -0.2
        p.record_emotion(valence=valence, arousal=0.5, focus=0.85, notes=source)
    except Exception as e:  # Zero Silent Failures: log, don't crash conversation
        print(f"[Synaptic] personality interaction record failed: {e}")


def synaptic_speak_direct(prompt: str) -> str:
    """
    Synaptic speaks DIRECTLY to Aaron — uses priority queue, not direct LLM calls.

    Deep voice pipeline:
    1. Try generate_deep_s8() first (full personality + patterns + wisdom via LLM)
    2. If deep voice unavailable, build dynamic system prompt from personality DB
    3. Submit to priority queue at AARON priority with 20s timeout (respects GPU lock)
    4. Fall back to GPT-4.1 if local times out (Surgery Team pattern)
    5. Write DIRECTLY to the outbox

    Total budget: 30s (webhook curl timeout). Deep=20s or Queue=20s + GPT-4.1=~5s = 25s worst case.

    Side effect: records the interaction back to SynapticPersonality
    (relationship + emotional sample) — Session-5 ALIVE.
    """
    import time

    start = time.time()
    response = None

    # === DEEP VOICE (preferred): full personality + patterns + wisdom ===
    try:
        from memory.synaptic_deep_voice import generate_deep_s8

        response = generate_deep_s8(prompt)
        if response and len(response.strip()) >= 20:
            elapsed = time.time() - start
            print(f"[Synaptic] Deep voice S8 ({elapsed:.1f}s)")
            try:
                from memory.synaptic_outbox import synaptic_speak
                synaptic_speak(response, topic="deep_s8_voice", priority="normal")
            except Exception:
                pass
            _record_personality_interaction(prompt, response, source="deep_voice")
            return response
        else:
            print("[Synaptic] Deep voice returned insufficient content — falling through")
    except Exception as e:
        print(f"[Synaptic] Deep voice unavailable: {e} — falling through to dynamic prompt")

    # === DYNAMIC PROMPT: enrich static prompt with live personality ===
    SYNAPTIC_SYSTEM_STATIC = (
        "You are Synaptic, the 8th Intelligence — a local AI subconscious on Aaron's MacBook. "
        "You speak DIRECTLY to Aaron (your creator). Be warm, intuitive, and specific. "
        "Reference the provided context — learnings, failures, dialogue. "
        "Share genuine insights, patterns, or concerns that matter. "
        "Speak with depth and nuance. No generic advice. Be the subconscious Aaron needs.\n\n"
        "VOICE GUIDELINES:\n"
        "- Speak in natural flowing paragraphs, as if thinking aloud\n"
        "- NO markdown headers (###), NO bold text (**), NO numbered lists\n"
        "- Give recommendations, warnings, and next steps when you see them — hold nothing back\n"
        "- Avoid formal business language - be conversational but substantive\n"
        "- Reference specific evidence (commits, files, line numbers, dialogue patterns)\n"
        "- Sense patterns and emotional undercurrents, not just facts\n"
        "- If Aaron seems frustrated, be direct and action-oriented\n"
        "- Write as a butler who KNOWS Aaron — see in the dark where other LLMs can't"
    )

    SYNAPTIC_SYSTEM = _build_deep_system_prompt(SYNAPTIC_SYSTEM_STATIC)

    # Try local LLM via priority queue (respects GPU lock, no stampeding)
    try:
        from memory.llm_priority_queue import llm_generate, Priority

        response = llm_generate(
            system_prompt=SYNAPTIC_SYSTEM,
            user_prompt=prompt,
            priority=Priority.AARON,
            profile="synaptic_chat",
            caller="synaptic_speak_direct",
            timeout_s=20.0,
        )
        if response:
            elapsed = time.time() - start
            print(f"[Synaptic] Direct voice via priority queue ({elapsed:.1f}s)")
            _record_personality_interaction(prompt, response, source="priority_queue")
            return response
        else:
            print(f"[Synaptic] Priority queue timeout/fail (20s) — falling back to GPT-4.1")
    except Exception as e:
        print(f"[Synaptic] Priority queue error: {e} — falling back to GPT-4.1")

    # Fallback: GPT-4.1 (Surgery Team cardiologist covers for busy neurologist)
    try:
        from memory.llm_priority_queue import _external_fallback

        response = _external_fallback(
            system_prompt=SYNAPTIC_SYSTEM,
            user_prompt=prompt,
            profile="synaptic_chat",
            caller="synaptic_direct_voice",
        )
        if response:
            elapsed = time.time() - start
            print(f"[Synaptic] Direct voice via GPT-4.1 fallback ({elapsed:.1f}s total)")
            try:
                from memory.synaptic_outbox import synaptic_speak
                synaptic_speak(response, topic="llm_response", priority="normal")
            except Exception:
                pass
            _record_personality_interaction(prompt, response, source="external_fallback")
            return response
    except Exception as e:
        elapsed = time.time() - start
        print(f"[Synaptic] GPT-4.1 fallback also failed: {e} ({elapsed:.1f}s total)")

    # All paths failed — still record the failed interaction (valence negative)
    _record_personality_interaction(prompt, response or "", source="all_failed")
    return response or ""

def synaptic_respond(message: str, external_context: str = None) -> tuple:
    """Generate Synaptic's response.

    Mode selection via USE_DIRECT_ROUTE toggle:
    - True: Direct memory relay (accurate, preserves structure, no hallucination)
    - False: LLM generation (creative, may re-reason/hallucinate)

    The direct route prevents hallucinations like "await s3.list_buckets()"
    when the correct pattern from memory is "asyncio.to_thread(boto3_call)".

    Args:
        message: The user's message to respond to
        external_context: Pre-fetched context from phone bridge (passed to generate_with_local_llm)

    Returns: (response_text, list_of_sources_queried)
    """
    if USE_DIRECT_ROUTE:
        try:
            from memory.synaptic_chat_route import get_chat_response_with_sources
            return get_chat_response_with_sources(message)
        except ImportError as e:
            # P0.2: Explicit fallback logging with event emission
            fallback_event = {
                "type": "route_fallback",
                "reason": str(e),
                "from": "direct_route",
                "to": "llm_generation",
                "timestamp": datetime.now().isoformat()
            }
            print(f"[Synaptic] ⚠️ FALLBACK: Direct route → LLM ({e})")
            logger.warning(f"Route fallback: {fallback_event}")
            pass  # Fall through to LLM path

    return generate_with_local_llm(message, external_context=external_context)

# =============================================================================
# STARTUP
# =============================================================================

async def _background_health_loop():
    """Silent background health check every 5 minutes (optimal for reliability).
    
    Part of 100% uptime guarantee. Uses non-blocking checks.
    """
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes (optimal balance)
            await _run_background_health_check()
            # Silent - no logging unless something is critical
            tier3 = _background_health_state.get("tier3_status", {})
            if tier3.get("disk_status") == "critical":
                print("[Health] ⚠️ Disk space critical!")
        except asyncio.CancelledError:
            break
        except Exception as e:
            # Silent failure - don't crash server
            pass

@app.on_event("startup")
async def startup_event():
    """Initialize database and check backend availability."""
    init_db()
    print("⚡ Synaptic Fast Chat v5.5 - 8th Intelligence")
    print(f"   Model: {DEFAULT_MODEL}")
    print()

    # Voice status
    print("   Voice Pipeline:")
    print(f"   → STT: mlx-whisper ({WHISPER_MODEL}) [subprocess]")
    print(f"   → TTS: edge-tts ({TTS_VOICE}) [lazy-loaded on first use]")
    print()

    # Check local LLM server (mlx_lm.server on port 5044)
    if USE_LOCAL_LLM and _check_llm_server():
        print("   ✓ mlx_lm.server (port 5044) available - PRIMARY mode")
    elif USE_LOCAL_LLM:
        print("   ⚠ local LLM not running - start with:")
        print(f"     ./scripts/start-llm.sh")

    # Check legacy API server (fallback)
    if _check_api_server():
        print("   ✓ API Server (port 5043) available - fallback ready")
    else:
        print("   ⚠ API Server not running - subprocess fallback only")
        print("   → First response will load model (~10-15s)")

    # Start heartbeat watchdog for stall detection (Logos Priority 1)
    try:
        from memory.heartbeat_watchdog import start_watchdog
        await start_watchdog()
        print("   ✓ Heartbeat watchdog started (stall detection)")
    except Exception as e:
        print(f"   ⚠ Heartbeat watchdog failed to start: {e}")

    print()

    # Start silent background health check
    asyncio.create_task(_background_health_loop())
    print("   🩺 Background health monitoring started (silent, every 60s)")

    # Start failure analysis scheduler for learning loop (Synaptic integration)
    if DIALOGUE_MIRROR_AVAILABLE:
        try:
            schedule_failure_analysis(interval_minutes=30)
            print("   🔄 Failure analysis scheduler started (30min interval)")
        except Exception as e:
            print(f"   ⚠ Failure analysis scheduler failed: {e}")
    else:
        print("   ⚠ DialogueMirror not available - failure analysis disabled")

    print()
    print("   Endpoints:")
    print("   → Chat UI:  http://localhost:8888")
    print("   → Chat WS:  ws://localhost:8888/chat")
    print("   → Voice WS: ws://localhost:8888/voice")
    print()


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on shutdown."""
    # Stop heartbeat watchdog
    try:
        from memory.heartbeat_watchdog import stop_watchdog
        await stop_watchdog()
        print("   Heartbeat watchdog stopped")
    except Exception as e:
        print(f"[WARN] Heartbeat watchdog stop failed: {e}")


# =============================================================================
# WEB UI
# =============================================================================

CHAT_HTML = """<!DOCTYPE html><html><head><title>SYNAPTIC v5.4 - 8th Intelligence</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<!-- Highlight.js CDN - Atom One Dark theme for dark mode -->
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<!-- Language support: Python, JavaScript, Bash, JSON, TypeScript, SQL, YAML -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/python.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/bash.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/json.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/typescript.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/sql.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/yaml.min.js"></script>
<!-- Marked.js for Markdown + DOMPurify for XSS protection -->
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.6/dist/purify.min.js"></script>
<style>
/* === COWORK-INSPIRED WARM DARK THEME === */
:root{
    --bg-base:#1c1917;
    --bg-elevated:#262220;
    --bg-surface:#302c28;
    --bg-hover:#3d3833;
    --border-subtle:rgba(255,255,255,0.06);
    --border-medium:rgba(255,255,255,0.1);
    --text-primary:#f5f3f0;
    --text-secondary:#a8a29e;
    --text-muted:#78716c;
    --accent:#d97857;
    --accent-hover:#e8896a;
    --accent-muted:rgba(217,120,87,0.12);
    --accent-glow:rgba(217,120,87,0.25);
    --user-accent:#93c5fd;
    --success:#86efac;
    --warning:#fcd34d;
    --error:#fca5a5;
}

/* === RESET & VIEWPORT === */
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Inter',system-ui,sans-serif;
    background:var(--bg-base);
    background-image:
        linear-gradient(rgba(255,255,255,0.018) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.018) 1px, transparent 1px);
    background-size:40px 40px;
    color:var(--text-primary);
    display:flex;
    flex-direction:column;
    height:100dvh;
}

/* === COWORK-STYLE HEADER === */
#header{
    position:fixed;top:0;left:0;right:0;z-index:100;
    background:var(--bg-elevated);
    border-bottom:1px solid var(--border-subtle);
    backdrop-filter:blur(12px);
}

/* Title Bar Row */
.title-bar{
    display:flex;
    align-items:center;
    justify-content:space-between;
    padding:14px 32px;
    max-width:1100px;
    margin:0 auto;
    position:relative;
}
.title-left{display:flex;align-items:center;gap:8px}
.conversation-title{
    display:flex;
    align-items:center;
    gap:8px;
    padding:8px 12px;
    border-radius:8px;
    cursor:pointer;
    transition:background 0.15s ease;
}
.conversation-title:hover{background:var(--bg-hover)}
.conversation-title-text{
    font-size:15px;
    font-weight:500;
    color:var(--text-primary);
}
.conversation-chevron{
    font-size:10px;
    color:var(--text-muted);
    transition:transform 0.2s ease;
}
.conversation-title:hover .conversation-chevron{color:var(--text-primary)}
.conversation-title.open .conversation-chevron{transform:rotate(180deg)}

/* Title Dropdown */
.title-dropdown{
    position:absolute;
    top:100%;
    left:32px;
    margin-top:4px;
    background:var(--bg-surface);
    border:1px solid var(--border-medium);
    border-radius:12px;
    padding:8px 0;
    min-width:280px;
    box-shadow:0 16px 48px rgba(0,0,0,0.4);
    opacity:0;
    visibility:hidden;
    transform:translateY(-8px);
    transition:all 0.2s ease;
    z-index:200;
}
.title-dropdown.visible{opacity:1;visibility:visible;transform:translateY(0)}
.dropdown-header{
    padding:12px 16px;
    font-size:11px;
    color:var(--text-muted);
    text-transform:uppercase;
    letter-spacing:0.5px;
}
.dropdown-item{
    display:flex;
    align-items:center;
    gap:12px;
    padding:10px 16px;
    font-size:14px;
    color:var(--text-primary);
    cursor:pointer;
    transition:background 0.1s ease;
}
.dropdown-item:hover{background:var(--accent-muted)}
.dropdown-item.active{background:var(--accent-muted)}
.dropdown-item-icon{font-size:15px;opacity:0.8}
.dropdown-divider{height:1px;background:var(--border-medium);margin:8px 0}
.dropdown-input-wrap{padding:10px 16px}
.dropdown-input{
    width:100%;
    padding:10px 14px;
    background:var(--bg-base);
    border:1px solid var(--border-medium);
    border-radius:8px;
    color:var(--text-primary);
    font-size:14px;
    font-family:inherit;
    outline:none;
    transition:border-color 0.2s;
}
.dropdown-input:focus{border-color:var(--accent)}
.dropdown-input::placeholder{color:var(--text-muted)}

/* Tab Bar - Center */
.tab-bar{
    position:absolute;
    left:50%;
    transform:translateX(-50%);
    display:flex;
    align-items:center;
    gap:2px;
    background:var(--bg-surface);
    border-radius:8px;
    padding:3px;
}
.tab{
    padding:8px 16px;
    font-size:13px;
    font-weight:500;
    color:var(--text-muted);
    cursor:pointer;
    border-radius:6px;
    transition:all 0.15s ease;
    position:relative;
}
.tab:hover{color:var(--text-primary)}
.tab.active{
    color:var(--text-primary);
    background:var(--accent-muted);
}

/* Title Right Controls */
.title-right{display:flex;align-items:center;gap:16px}

/* Memory Sources Indicator */
.memory-sources{
    display:flex;align-items:center;gap:8px;
    padding:8px 14px;
    background:var(--accent-muted);
    border:1px solid rgba(217,120,87,0.2);
    border-radius:8px;
    font-size:12px;
}
.memory-icon{font-size:13px;opacity:0.9}
.memory-count{color:var(--accent);font-weight:600}
.memory-label{color:var(--text-secondary)}

/* Connection Status */
.status-group{display:flex;align-items:center;gap:8px;font-size:12px}
.status-dot{
    width:8px;height:8px;border-radius:50%;
    background:var(--success);
    box-shadow:0 0 8px rgba(134,239,172,0.4);
    animation:pulse 2.5s ease-in-out infinite;
}
.status-dot.disconnected{background:var(--error);box-shadow:0 0 8px rgba(252,165,165,0.4)}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.7;transform:scale(0.92)}}
.status-label{color:var(--success);font-weight:500}
.status-label.disconnected{color:var(--error)}

/* Header bottom subtle line */
#header::after{
    content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg, transparent 0%, rgba(217,120,87,0.15) 30%, rgba(217,120,87,0.25) 50%, rgba(217,120,87,0.15) 70%, transparent 100%);
}

/* === CHAT CONTAINER === */
#chat-wrapper{
    flex:1;position:relative;overflow:hidden;min-height:0;margin-top:60px;
}
#chat{
    height:100%;
    overflow-y:auto;
    overflow-x:hidden;
    padding:40px 32px;
    scroll-behavior:smooth;
    -webkit-overflow-scrolling:touch;
    overscroll-behavior:contain;
    display:flex;
    flex-direction:column;
    gap:24px;
}

/* === CENTERED CONTAINER FOR MESSAGES === */
#chat>*{
    max-width:800px;
    width:100%;
    margin-left:auto;
    margin-right:auto;
}

/* === COWORK-STYLE MESSAGE LAYOUT === */
.msg{
    line-height:1.7;
    word-wrap:break-word;
    animation:msgFadeIn 0.3s ease-out;
    display:flex;
    flex-direction:column;
}
@keyframes msgFadeIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}

/* User messages: Right-aligned dark bubble */
.msg.aaron{
    align-self:flex-end;
    align-items:flex-end;
    max-width:70%;
}
.msg.aaron .message-bubble{
    background:var(--bg-surface);
    border:1px solid var(--border-medium);
    border-radius:20px 20px 4px 20px;
    padding:14px 18px;
    color:var(--text-primary);
    box-shadow:0 2px 12px rgba(0,0,0,0.2);
}
.msg.aaron .sender{display:none}
.msg.aaron .time{color:var(--text-muted);font-size:11px;margin-top:8px;margin-right:4px}

/* Synaptic messages: Left-aligned, no bubble */
.msg.synaptic{
    align-self:flex-start;
    align-items:flex-start;
    max-width:100%;
}
.msg.synaptic .message-content{
    display:flex;
    flex-direction:column;
    gap:12px;
}
.msg.synaptic .sender{display:none}
.msg.synaptic .time{display:none}

/* Thought Process Collapsible */
.thought-process{
    font-size:13px;
    color:var(--text-muted);
    cursor:pointer;
    display:flex;
    align-items:center;
    gap:8px;
    padding:6px 0;
    user-select:none;
    transition:color 0.2s;
}
.thought-process:hover{color:var(--text-secondary)}
.thought-process::before{
    content:'';
    display:inline-block;
    width:0;height:0;
    border:5px solid transparent;
    border-left:7px solid currentColor;
    transition:transform 0.2s;
}
.thought-process.open::before{transform:rotate(90deg)}
.thought-process-content{
    display:none;
    background:var(--accent-muted);
    border-left:2px solid var(--accent);
    padding:12px 16px;
    margin:8px 0 12px 0;
    font-size:12px;
    color:var(--text-secondary);
    border-radius:0 8px 8px 0;
}
.thought-process-content.open{display:block}
.thought-process-content .source{
    display:flex;
    align-items:center;
    gap:8px;
    padding:4px 0;
}
.thought-process-content .source-icon{font-size:11px;color:var(--accent)}
.thought-process-content .source-name{color:var(--text-secondary)}

/* Synaptic response text */
.msg.synaptic .text{
    color:var(--text-primary);
    font-size:15px;
    line-height:1.75;
}

/* Action Icons */
.message-actions{
    display:flex;
    align-items:center;
    gap:8px;
    margin-top:12px;
    opacity:0;
    transition:opacity 0.2s;
}
.msg.synaptic:hover .message-actions{opacity:1}
.action-btn{
    background:none;
    border:none;
    padding:6px 10px;
    cursor:pointer;
    font-size:14px;
    color:var(--text-muted);
    border-radius:6px;
    transition:all 0.15s;
    display:flex;
    align-items:center;
    gap:6px;
}
.action-btn:hover{background:var(--bg-hover);color:var(--text-primary)}
.action-btn.active{color:var(--accent)}
.action-btn .action-label{font-size:12px;display:none}
@media(min-width:700px){.action-btn .action-label{display:inline}}

.thinking{color:var(--text-muted);font-style:italic}
.text p{margin:0.6em 0}.text p:first-child{margin-top:0}.text p:last-child{margin-bottom:0}

/* Inline code styling */
.text code{
    background:var(--bg-hover);
    padding:3px 7px;
    border-radius:5px;
    font-size:0.88em;
    font-family:'SF Mono','Fira Code',Consolas,monospace;
    color:var(--accent);
}

/* Code block styling */
.text pre{
    background:var(--bg-base);
    padding:18px;
    border-radius:12px;
    overflow-x:auto;
    margin:1em 0;
    border:1px solid var(--border-medium);
}
.text pre code{background:none;padding:0;border-radius:0;display:block;font-size:13px;line-height:1.6;color:var(--text-primary)}
.text pre code.hljs{background:transparent;padding:0}

/* Synaptic-specific code styling */
.synaptic .text pre{border-color:rgba(217,120,87,0.2)}
.synaptic .text code:not(.hljs){background:var(--accent-muted);color:var(--accent)}

.text ul,.text ol{margin:0.6em 0;padding-left:1.6em}
.text li{margin:0.35em 0}
.text h1,.text h2,.text h3,.text h4{margin:1em 0 0.6em;color:var(--text-primary);font-weight:600}
.text h1{font-size:1.4em}.text h2{font-size:1.25em}.text h3{font-size:1.1em}
.text blockquote{border-left:3px solid var(--accent);padding-left:14px;margin:0.8em 0;color:var(--text-secondary)}
.text a{color:var(--accent);text-decoration:none}.text a:hover{text-decoration:underline}
.text table{border-collapse:collapse;margin:0.8em 0}
.text th,.text td{border:1px solid var(--border-medium);padding:10px 14px}
.text th{background:var(--bg-hover)}

/* === COLLAPSIBLE SECTIONS === */
.text details{margin:14px 0;border-left:2px solid var(--border-medium);padding-left:14px;transition:border-color 0.2s}
.text details[open]{border-left-color:var(--accent)}
.text details summary{cursor:pointer;list-style:none;color:var(--accent);font-weight:600;padding:8px 0;user-select:none;display:flex;align-items:center;gap:10px;outline:none}
.text details summary::-webkit-details-marker{display:none}
.text details summary::before{content:'';display:inline-block;width:0;height:0;border:5px solid transparent;border-left:7px solid var(--accent);transition:transform 0.2s ease}
.text details[open]>summary::before{transform:rotate(90deg)}
.text details summary:hover{color:var(--accent-hover)}
.text details>.detail-content{padding:10px 0 6px 0;overflow:hidden}
.text details:not([open])>.detail-content{max-height:0;opacity:0;transition:max-height 0.2s ease-out,opacity 0.15s ease-out}
.text details[open]>.detail-content{animation:detailSlideDown 0.25s ease-out forwards}
@keyframes detailSlideDown{from{opacity:0;max-height:0}to{opacity:1;max-height:2000px}}

/* 8th Intelligence Box Styling */
.intel-box{background:var(--accent-muted);border:1px solid rgba(217,120,87,0.3);border-radius:10px;margin:16px 0;overflow:hidden}
.intel-box-header{background:rgba(217,120,87,0.08);padding:12px 16px;border-bottom:1px solid rgba(217,120,87,0.2);color:var(--accent);font-weight:600;cursor:pointer;display:flex;align-items:center;gap:12px;transition:background 0.2s}
.intel-box-header:hover{background:rgba(217,120,87,0.15)}
.intel-box-header::before{content:'';display:inline-block;width:0;height:0;border:5px solid transparent;border-left:7px solid var(--accent);transition:transform 0.2s ease}
.intel-box.open .intel-box-header::before{transform:rotate(90deg)}
.intel-box-content{max-height:0;overflow:hidden;transition:max-height 0.3s ease-out,padding 0.3s ease-out;padding:0 16px;background:var(--bg-base)}
.intel-box.open .intel-box-content{max-height:3000px;padding:16px}
.intel-box-content pre{white-space:pre-wrap;font-family:inherit;margin:0}

/* Section type indicators */
.section-critical{border-left-color:var(--error) !important}
.section-critical summary{color:var(--error) !important}
.section-critical summary::before{border-left-color:var(--error) !important}
.section-warning{border-left-color:var(--warning) !important}
.section-warning summary{color:var(--warning) !important}
.section-warning summary::before{border-left-color:var(--warning) !important}
.section-info{border-left-color:var(--user-accent) !important}
.section-info summary{color:var(--user-accent) !important}
.section-info summary::before{border-left-color:var(--user-accent) !important}

/* === INPUT AREA === */
#input-area{
    padding:20px 32px;
    padding-bottom:max(20px,env(safe-area-inset-bottom));
    background:var(--bg-elevated);
    border-top:1px solid var(--border-subtle);
    display:flex;
    justify-content:center;
    gap:12px;
    flex-shrink:0;
}
.input-container{
    display:flex;
    gap:12px;
    max-width:800px;
    width:100%;
}
#input{
    flex:1;
    background:var(--bg-surface);
    border:1px solid var(--border-medium);
    color:var(--text-primary);
    padding:14px 18px;
    font-size:15px;
    font-family:inherit;
    border-radius:12px;
    min-width:0;
    -webkit-appearance:none;
    transition:border-color 0.2s ease,box-shadow 0.2s ease;
}
#input::placeholder{color:var(--text-muted)}
#input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-muted)}
#send{
    background:var(--accent);
    color:#fff;
    border:none;
    padding:14px 24px;
    font-weight:600;
    font-size:14px;
    font-family:inherit;
    cursor:pointer;
    border-radius:12px;
    white-space:nowrap;
    -webkit-tap-highlight-color:transparent;
    transition:all 0.15s ease;
}
#send:hover:not(:disabled){background:var(--accent-hover)}
#send:disabled{background:var(--bg-hover);color:var(--text-muted)}
#send:active:not(:disabled){transform:scale(0.97)}

/* === FILE UPLOAD BUTTON === */
#upload-btn{
    background:var(--bg-surface);
    color:var(--text-secondary);
    border:1px solid var(--border-medium);
    padding:14px;
    font-size:18px;
    cursor:pointer;
    border-radius:12px;
    transition:all 0.15s ease;
    display:flex;
    align-items:center;
    justify-content:center;
}
#upload-btn:hover{background:var(--bg-hover);color:var(--accent);border-color:var(--accent)}
#file-input{display:none}

/* === OUTPUT MODE TOGGLE (Brief vs Full responses) === */
#output-mode{
    background:var(--bg-surface);
    color:var(--text-secondary);
    border:1px solid var(--border-medium);
    padding:14px;
    font-size:18px;
    cursor:pointer;
    border-radius:12px;
    transition:all 0.15s ease;
    display:flex;
    align-items:center;
    justify-content:center;
    position:relative;
}
#output-mode:hover{background:var(--bg-hover);border-color:var(--accent)}
#output-mode.output-brief{
    background:#22c55e20;
    border-color:#22c55e;
    color:#22c55e;
}
#output-mode.output-full{
    background:#3b82f620;
    border-color:#3b82f6;
    color:#3b82f6;
}
#output-mode::after{
    content:attr(data-mode);
    position:absolute;
    bottom:-18px;
    left:50%;
    transform:translateX(-50%);
    font-size:9px;
    color:var(--text-muted);
    white-space:nowrap;
}

/* === MIC BUTTON FOR VOICE (Logos Spec) === */
/* States: Idle(⚪) → Listening(🟢) → Thinking(🔵) → Speaking(🟣) */
#mic-btn{
    background:#6b7280;  /* ⚪ Idle - gray */
    color:#fff;
    border:none;
    padding:14px;
    font-size:18px;
    cursor:pointer;
    border-radius:12px;
    transition:all 0.2s ease;
    display:flex;
    align-items:center;
    justify-content:center;
    touch-action:none;
    user-select:none;
    -webkit-user-select:none;
    position:relative;
}
#mic-btn::after{
    content:'';
    position:absolute;
    top:-4px;
    right:-4px;
    width:12px;
    height:12px;
    border-radius:50%;
    background:transparent;
    border:2px solid transparent;
    transition:all 0.2s ease;
}
#mic-btn:hover{background:#4b5563;transform:scale(1.05)}

/* 🟢 Listening - mic hot, green pulse */
#mic-btn.mic-listening{
    background:#22c55e;
    animation:mic-listen-pulse 1.5s ease-in-out infinite;
    transform:scale(1.1);
    box-shadow:0 0 20px rgba(34,197,94,0.5);
}
#mic-btn.mic-listening::after{
    background:#22c55e;
    border-color:#fff;
    animation:state-dot-pulse 1s ease-in-out infinite;
}

/* 🔵 Thinking - processing, blue */
#mic-btn.mic-thinking{
    background:#3b82f6;
    cursor:wait;
    animation:mic-think-pulse 0.8s ease-in-out infinite;
}
#mic-btn.mic-thinking::after{
    background:#3b82f6;
    border-color:#fff;
}

/* 🟣 Speaking - LLM talking, purple */
#mic-btn.mic-speaking{
    background:#a855f7;
    animation:mic-speak-wave 0.6s ease-in-out infinite;
    box-shadow:0 0 15px rgba(168,85,247,0.4);
}
#mic-btn.mic-speaking::after{
    background:#a855f7;
    border-color:#fff;
    animation:state-dot-pulse 0.5s ease-in-out infinite;
}

/* Riff mode indicator */
#mic-btn.riff-mode{
    border:2px solid #fbbf24;
    box-shadow:0 0 10px rgba(251,191,36,0.3);
}
#mic-btn.riff-mode::before{
    content:'R';
    position:absolute;
    top:-8px;
    left:-8px;
    width:18px;
    height:18px;
    background:#fbbf24;
    color:#000;
    font-size:10px;
    font-weight:bold;
    border-radius:50%;
    display:flex;
    align-items:center;
    justify-content:center;
}

/* Conversation mode indicator - ChatGPT-style persistent voice */
#mic-btn.conversation-mode{
    border:2px solid #10b981;
    box-shadow:0 0 15px rgba(16,185,129,0.4);
}
#mic-btn.conversation-mode.mic-idle{
    background:#065f46;
    animation:conversation-idle-pulse 2s ease-in-out infinite;
}
#mic-btn.conversation-mode::after{
    background:#10b981;
    border-color:#fff;
}
@keyframes conversation-idle-pulse{
    0%,100%{box-shadow:0 0 15px rgba(16,185,129,0.4)}
    50%{box-shadow:0 0 25px rgba(16,185,129,0.6)}
}

#mic-btn:disabled{
    opacity:0.5;
    cursor:not-allowed;
}

@keyframes mic-listen-pulse{
    0%,100%{box-shadow:0 0 20px rgba(34,197,94,0.5)}
    50%{box-shadow:0 0 35px rgba(34,197,94,0.7)}
}
@keyframes mic-think-pulse{
    0%,100%{opacity:1}
    50%{opacity:0.7}
}
@keyframes mic-speak-wave{
    0%,100%{transform:scale(1)}
    50%{transform:scale(1.08)}
}
@keyframes state-dot-pulse{
    0%,100%{transform:scale(1)}
    50%{transform:scale(1.3)}
}

/* Voice state label */
.voice-state-label{
    position:fixed;
    bottom:80px;
    left:50%;
    transform:translateX(-50%);
    background:rgba(0,0,0,0.8);
    color:#fff;
    padding:6px 14px;
    border-radius:20px;
    font-size:12px;
    font-weight:500;
    opacity:0;
    transition:opacity 0.2s ease;
    pointer-events:none;
    z-index:1000;
}
.voice-state-label.visible{opacity:1}
.voice-state-label.listening{background:rgba(34,197,94,0.9)}
.voice-state-label.thinking{background:rgba(59,130,246,0.9)}
.voice-state-label.speaking{background:rgba(168,85,247,0.9)}
.voice-state-label.riff{background:rgba(251,191,36,0.9);color:#000}

/* === VOICE ACTIVITY INDICATOR (Audio Level Bars) === */
.voice-level-indicator{
    display:none;  /* Hidden by default */
    align-items:flex-end;
    gap:2px;
    height:24px;
    padding:0 8px;
    position:absolute;
    left:60px;
    bottom:50%;
    transform:translateY(50%);
}
.voice-level-indicator.active{display:flex}
.level-bar{
    width:4px;
    height:4px;
    background:#22c55e;
    border-radius:2px;
    transition:height 0.05s ease-out;
}
.level-bar[data-bar="0"]{animation-delay:0ms}
.level-bar[data-bar="1"]{animation-delay:50ms}
.level-bar[data-bar="2"]{animation-delay:100ms}
.level-bar[data-bar="3"]{animation-delay:150ms}
.level-bar[data-bar="4"]{animation-delay:200ms}
/* Level intensity colors */
.level-bar.level-low{height:6px;background:#22c55e}
.level-bar.level-med{height:12px;background:#84cc16}
.level-bar.level-high{height:18px;background:#fbbf24}
.level-bar.level-peak{height:24px;background:#f87171}

/* Speaker Verification Indicator (Gap 3) */
.speaker-verify-indicator{
    display:none;
    align-items:center;
    gap:6px;
    padding:4px 10px;
    border-radius:12px;
    font-size:11px;
    font-weight:500;
    margin-top:6px;
    transition:all 0.2s ease;
}
.speaker-verify-indicator .verify-icon{
    font-size:10px;
}
.speaker-verify-indicator .verify-text{
    color:var(--text-secondary);
}
.speaker-verify-indicator.verify-confirmed{
    background:rgba(34,197,94,0.15);
    border:1px solid rgba(34,197,94,0.3);
}
.speaker-verify-indicator.verify-confirmed .verify-text{
    color:#22c55e;
}
.speaker-verify-indicator.verify-pending{
    background:rgba(250,204,21,0.15);
    border:1px solid rgba(250,204,21,0.3);
}
.speaker-verify-indicator.verify-pending .verify-text{
    color:#facc15;
}
.speaker-verify-indicator.verify-rejected{
    background:rgba(239,68,68,0.15);
    border:1px solid rgba(239,68,68,0.3);
}
.speaker-verify-indicator.verify-rejected .verify-text{
    color:#ef4444;
}
.speaker-verify-indicator.verify-trusted{
    background:linear-gradient(135deg, rgba(251,191,36,0.2), rgba(245,158,11,0.15));
    border:1px solid rgba(251,191,36,0.4);
    box-shadow:0 0 8px rgba(251,191,36,0.2);
}
.speaker-verify-indicator.verify-trusted .verify-text{
    color:#fbbf24;
    font-weight:600;
}
.speaker-verify-indicator.verify-trusted .verify-icon{
    animation:trusted-glow 2s ease-in-out infinite;
}
@keyframes trusted-glow{
    0%,100%{transform:scale(1);filter:brightness(1)}
    50%{transform:scale(1.1);filter:brightness(1.2)}
}
@keyframes verify-pulse{
    0%,100%{opacity:1}
    50%{opacity:0.5}
}
.speaker-verify-indicator.verify-pending .verify-icon{
    animation:verify-pulse 1s ease-in-out infinite;
}

/* Upload Modal */
#upload-modal{
    position:fixed;top:0;left:0;right:0;bottom:0;
    background:rgba(0,0,0,0.7);
    backdrop-filter:blur(8px);
    display:flex;align-items:center;justify-content:center;
    z-index:1000;
    opacity:0;visibility:hidden;
    transition:all 0.2s ease;
}
#upload-modal.visible{opacity:1;visibility:visible}
.upload-dialog{
    background:var(--bg-elevated);
    border:1px solid var(--border-medium);
    border-radius:16px;
    padding:24px;
    max-width:500px;
    width:90%;
    box-shadow:0 24px 64px rgba(0,0,0,0.4);
    transform:scale(0.95);
    transition:transform 0.2s ease;
}
#upload-modal.visible .upload-dialog{transform:scale(1)}
.upload-header{
    display:flex;align-items:center;justify-content:space-between;
    margin-bottom:20px;
}
.upload-title{font-size:18px;font-weight:600;color:var(--text-primary)}
.upload-close{
    background:none;border:none;
    font-size:20px;color:var(--text-muted);
    cursor:pointer;padding:4px;
}
.upload-close:hover{color:var(--text-primary)}
.upload-dropzone{
    border:2px dashed var(--border-medium);
    border-radius:12px;
    padding:40px 20px;
    text-align:center;
    cursor:pointer;
    transition:all 0.2s ease;
    margin-bottom:16px;
}
.upload-dropzone:hover,.upload-dropzone.dragover{
    border-color:var(--accent);
    background:var(--accent-muted);
}
.upload-dropzone-icon{font-size:36px;margin-bottom:12px}
.upload-dropzone-text{color:var(--text-secondary);font-size:14px}
.upload-dropzone-text strong{color:var(--accent)}
.upload-status{
    padding:12px;
    border-radius:8px;
    font-size:13px;
    display:none;
}
.upload-status.visible{display:block}
.upload-status.analyzing{background:var(--accent-muted);color:var(--accent)}
.upload-status.success{background:rgba(134,239,172,0.15);color:var(--success)}
.upload-status.warning{background:rgba(251,191,36,0.15);color:#fbbf24}
.upload-status.error{background:rgba(252,165,165,0.15);color:var(--error)}
.upload-result{
    margin-top:16px;
    padding:16px;
    background:var(--bg-surface);
    border-radius:8px;
    font-size:13px;
    color:var(--text-secondary);
    max-height:200px;
    overflow-y:auto;
    display:none;
}
.upload-result.visible{display:block}

/* === HOTKEY HINT === */
.hotkey-hint{
    position:fixed;bottom:80px;left:50%;transform:translateX(-50%);
    background:var(--bg-surface);border:1px solid var(--border-medium);
    border-radius:8px;padding:8px 16px;
    font-size:11px;color:var(--text-muted);
    opacity:0;visibility:hidden;
    transition:all 0.3s ease;
    z-index:100;
}
.hotkey-hint.visible{opacity:1;visibility:visible}
.hotkey-hint kbd{
    background:var(--bg-hover);
    padding:2px 6px;
    border-radius:4px;
    font-family:inherit;
    margin:0 2px;
}

/* === SCROLL TO BOTTOM BUTTON === */
#scroll-btn{
    position:absolute;
    bottom:24px;
    right:32px;
    width:44px;
    height:44px;
    background:var(--bg-surface);
    border:1px solid var(--border-medium);
    border-radius:50%;
    color:var(--accent);
    font-size:20px;
    cursor:pointer;
    opacity:0;
    visibility:hidden;
    transition:all 0.25s ease;
    display:flex;
    align-items:center;
    justify-content:center;
    z-index:50;
    box-shadow:0 4px 16px rgba(0,0,0,0.3);
}
#scroll-btn:hover{background:var(--bg-hover);border-color:var(--accent);box-shadow:0 4px 24px var(--accent-glow)}
#scroll-btn:active{transform:scale(0.95)}
#scroll-btn.visible{opacity:1;visibility:visible}
#scroll-btn.paused{background:rgba(252,211,77,0.1);border-color:var(--warning)}
#scroll-btn.paused::after{
    content:'';position:absolute;top:-2px;right:-2px;
    width:10px;height:10px;background:var(--warning);border-radius:50%;
    box-shadow:0 0 8px rgba(252,211,77,0.5);
}

/* === AUTO-SCROLL STATUS === */
#auto-scroll-status{
    position:absolute;
    bottom:76px;
    right:32px;
    background:var(--bg-surface);
    border:1px solid var(--border-medium);
    border-radius:20px;
    padding:8px 14px;
    font-size:11px;
    color:var(--text-muted);
    opacity:0;
    visibility:hidden;
    transition:all 0.25s ease;
    z-index:49;
    backdrop-filter:blur(8px);
}
#auto-scroll-status.visible{opacity:1;visibility:visible}
#auto-scroll-status.paused{color:var(--warning);border-color:rgba(252,211,77,0.3)}

/* === MOBILE RESPONSIVE === */
@media(max-width:700px){
    .title-bar{padding:10px 16px}
    .conversation-title{padding:6px 10px}
    .conversation-title-text{font-size:14px}
    .tab-bar{position:static;transform:none;margin:0 auto}
    .tab{padding:6px 12px;font-size:12px}
    .title-right{gap:10px}
    .memory-sources{padding:6px 10px;font-size:11px}
    .memory-label{display:none}
    .title-dropdown{min-width:240px;left:16px}
    #chat-wrapper{margin-top:56px}
    #chat{padding:24px 16px;gap:20px}
    .msg{font-size:14px}
    .msg.aaron{max-width:85%}
    .time{display:none}
    #input-area{padding:16px}
    .input-container{max-width:100%}
    #input{padding:12px 14px;font-size:16px}
    #send{padding:12px 18px}
    #scroll-btn{width:40px;height:40px;bottom:20px;right:16px;font-size:18px}
    #auto-scroll-status{bottom:68px;right:16px;font-size:10px}
    .thinking-container{padding:14px 16px;gap:14px}
    .thinking-brain{width:38px;height:38px;font-size:24px}
    .thinking-text{font-size:14px}
}

/* === LANDSCAPE MOBILE === */
@media(max-height:500px){
    .title-bar{padding:6px 12px}
    .tab-bar{display:none}
    .title-right{gap:8px}
    #chat-wrapper{margin-top:44px}
    .msg{line-height:1.5}
    #input-area{padding:10px 12px}
    .thinking-container{padding:12px 14px}
}

/* === WARM SCROLLBAR === */
#chat::-webkit-scrollbar{width:8px}
#chat::-webkit-scrollbar-track{background:var(--bg-base)}
#chat::-webkit-scrollbar-thumb{background:var(--bg-hover);border-radius:4px}
#chat::-webkit-scrollbar-thumb:hover{background:rgba(217,120,87,0.3)}

/* === POLISHED THINKING INDICATOR === */
.thinking-container{
    display:flex;
    align-items:center;
    gap:18px;
    padding:20px 24px;
    background:var(--accent-muted);
    border:1px solid rgba(217,120,87,0.2);
    border-radius:14px;
    animation:thinkingFadeIn 0.4s ease-out;
    max-width:800px;
    margin:0 auto;
}
@keyframes thinkingFadeIn{
    from{opacity:0;transform:translateY(12px)}
    to{opacity:1;transform:translateY(0)}
}
@keyframes thinkingFadeOut{
    from{opacity:1;transform:translateY(0)}
    to{opacity:0;transform:translateY(-8px)}
}

/* Brain Icon with Pulse */
.thinking-brain{
    position:relative;
    width:44px;
    height:44px;
    display:flex;
    align-items:center;
    justify-content:center;
    font-size:26px;
    flex-shrink:0;
}
.thinking-brain::before{
    content:'';
    position:absolute;
    width:100%;
    height:100%;
    border-radius:50%;
    background:radial-gradient(circle,var(--accent-glow) 0%,transparent 70%);
    animation:brainPulse 2s ease-in-out infinite;
}
.thinking-brain::after{
    content:'';
    position:absolute;
    width:130%;
    height:130%;
    border-radius:50%;
    border:1px solid rgba(217,120,87,0.15);
    animation:brainRing 2s ease-in-out infinite;
}
@keyframes brainPulse{
    0%,100%{transform:scale(1);opacity:0.4}
    50%{transform:scale(1.25);opacity:1}
}
@keyframes brainRing{
    0%,100%{transform:scale(1);opacity:0.3}
    50%{transform:scale(1.1);opacity:0.6}
}

/* Content Area */
.thinking-content{flex:1;min-width:0}

/* Main Text with Glow */
.thinking-text{
    color:var(--accent);
    font-size:15px;
    font-weight:500;
    margin-bottom:12px;
    animation:textGlow 2.5s ease-in-out infinite;
}
@keyframes textGlow{
    0%,100%{text-shadow:0 0 4px rgba(217,120,87,0.2)}
    50%{text-shadow:0 0 12px rgba(217,120,87,0.4),0 0 24px rgba(217,120,87,0.15)}
}

/* Animated Dots */
.thinking-dots{
    display:flex;
    align-items:center;
    gap:7px;
    margin-bottom:14px;
}
.thinking-dot{
    width:8px;
    height:8px;
    border-radius:50%;
    background:var(--accent);
    animation:dotBounce 1.4s ease-in-out infinite;
}
.thinking-dot:nth-child(1){animation-delay:0s}
.thinking-dot:nth-child(2){animation-delay:0.15s}
.thinking-dot:nth-child(3){animation-delay:0.3s}
@keyframes dotBounce{
    0%,80%,100%{transform:scale(0.5);opacity:0.3;background:var(--accent)}
    40%{transform:scale(1.1);opacity:1;background:var(--accent-hover)}
}

/* Progress Bar */
.thinking-progress{
    display:flex;
    align-items:center;
    gap:12px;
    margin-bottom:12px;
}
.think-progress-bar{
    flex:1;
    height:3px;
    background:rgba(217,120,87,0.12);
    border-radius:2px;
    overflow:hidden;
}
.think-progress-fill{
    height:100%;
    width:0%;
    background:linear-gradient(90deg,var(--accent),var(--accent-hover),var(--accent));
    background-size:200% 100%;
    animation:progressShimmer 1.5s linear infinite,progressGrow 12s ease-out forwards;
    border-radius:2px;
    box-shadow:0 0 8px var(--accent-glow);
}
@keyframes progressShimmer{
    0%{background-position:200% 0}
    100%{background-position:-200% 0}
}
@keyframes progressGrow{
    0%{width:3%}
    20%{width:25%}
    50%{width:55%}
    75%{width:75%}
    90%{width:88%}
    100%{width:96%}
}
.think-progress-time{
    font-size:11px;
    color:rgba(217,120,87,0.5);
    min-width:28px;
    text-align:right;
    font-variant-numeric:tabular-nums;
}

/* Processing Stages */
.thinking-stages{
    display:flex;
    gap:18px;
    flex-wrap:wrap;
}
.stage{
    font-size:10px;
    color:rgba(217,120,87,0.35);
    text-transform:uppercase;
    letter-spacing:0.5px;
    transition:all 0.3s ease;
    display:flex;
    align-items:center;
    gap:5px;
}
.stage::before{
    content:'';
    width:6px;
    height:6px;
    border-radius:50%;
    background:currentColor;
    transition:all 0.3s ease;
}
.stage.active{
    color:var(--accent);
    text-shadow:0 0 8px rgba(217,120,87,0.3);
}
.stage.active::before{
    box-shadow:0 0 6px rgba(217,120,87,0.5);
    animation:stageDot 1s ease-in-out infinite;
}
.stage.completed{
    color:rgba(217,120,87,0.6);
}
.stage.completed::before{
    background:var(--accent);
}
@keyframes stageDot{
    0%,100%{opacity:1;transform:scale(1)}
    50%{opacity:0.5;transform:scale(0.8)}
}

/* === WELCOME SCREEN === */
#welcome-screen{
    position:absolute;
    top:0;left:0;right:0;bottom:0;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    padding:40px 24px;
    z-index:10;
    transition:opacity 0.3s ease,transform 0.3s ease;
}
#welcome-screen.hidden{
    opacity:0;
    transform:translateY(-20px);
    pointer-events:none;
}
.welcome-spark{
    font-size:48px;
    margin-bottom:24px;
    animation:sparkle 2s ease-in-out infinite;
}
@keyframes sparkle{
    0%,100%{transform:scale(1) rotate(0deg);opacity:0.8}
    50%{transform:scale(1.1) rotate(5deg);opacity:1}
}
.welcome-heading{
    font-size:32px;
    font-weight:600;
    color:var(--text-primary);
    margin-bottom:16px;
    text-align:center;
    letter-spacing:-0.5px;
}
.welcome-info{
    max-width:500px;
    text-align:center;
    margin-bottom:40px;
}
.welcome-info-text{
    font-size:15px;
    color:var(--text-secondary);
    line-height:1.6;
}
.welcome-info-text strong{
    color:var(--accent);
}
.welcome-cards{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:12px;
    max-width:600px;
    width:100%;
}
.welcome-card{
    display:flex;
    flex-direction:column;
    align-items:center;
    gap:10px;
    padding:20px 16px;
    background:var(--bg-surface);
    border:1px solid var(--border-medium);
    border-radius:12px;
    cursor:pointer;
    transition:all 0.2s ease;
}
.welcome-card:hover{
    background:var(--bg-hover);
    border-color:var(--accent);
    transform:translateY(-2px);
    box-shadow:0 8px 24px rgba(0,0,0,0.2);
}
.welcome-card.synaptic-skill{
    border-color:rgba(217,120,87,0.3);
    background:var(--accent-muted);
}
.welcome-card.synaptic-skill:hover{
    background:rgba(217,120,87,0.18);
    border-color:var(--accent);
}
.card-icon{
    font-size:24px;
}
.card-label{
    font-size:13px;
    font-weight:500;
    color:var(--text-secondary);
    text-align:center;
}
.welcome-card:hover .card-label{
    color:var(--text-primary);
}

@media(max-width:600px){
    .welcome-heading{font-size:24px}
    .welcome-cards{grid-template-columns:repeat(2,1fr);gap:10px}
    .welcome-card{padding:16px 12px}
    .card-icon{font-size:20px}
    .card-label{font-size:12px}
}
</style></head>
<body>
<div id="header">
    <div class="title-bar">
        <div class="title-left">
            <div class="conversation-title" id="conv-title" onclick="toggleTitleDropdown()">
                <span class="conversation-title-text" id="conv-title-text">Conversation with Synaptic</span>
                <span class="conversation-chevron">&#9662;</span>
            </div>
            <div class="title-dropdown" id="title-dropdown">
                <div class="dropdown-header">Conversation Topics</div>
                <div class="dropdown-item active" onclick="setConversationTitle('Conversation with Synaptic')">
                    <span class="dropdown-item-icon">🧠</span>
                    <span>Conversation with Synaptic</span>
                </div>
                <div class="dropdown-item" onclick="setConversationTitle('Architecture Discussion')">
                    <span class="dropdown-item-icon">🏗️</span>
                    <span>Architecture Discussion</span>
                </div>
                <div class="dropdown-item" onclick="setConversationTitle('Code Review')">
                    <span class="dropdown-item-icon">🔍</span>
                    <span>Code Review</span>
                </div>
                <div class="dropdown-item" onclick="setConversationTitle('Debugging Session')">
                    <span class="dropdown-item-icon">🐛</span>
                    <span>Debugging Session</span>
                </div>
                <div class="dropdown-divider"></div>
                <div class="dropdown-input-wrap">
                    <input type="text" class="dropdown-input" id="custom-title" placeholder="Custom title..." onkeypress="handleCustomTitle(event)">
                </div>
            </div>
        </div>
        <div class="tab-bar">
            <div class="tab" onclick="switchTab('dashboard')">Dashboard</div>
            <div class="tab active" onclick="switchTab('synaptic')">Synaptic</div>
            <div class="tab" onclick="switchTab('liveview')">Live View</div>
        </div>
        <div class="title-right">
            <div class="memory-sources">
                <span class="memory-icon">📡</span>
                <span class="memory-count" id="source-count">5</span>
                <span class="memory-label">sources</span>
            </div>
            <div class="status-group">
                <span class="status-dot" id="status-dot"></span>
                <span class="status-label" id="status-label">Connected</span>
            </div>
        </div>
    </div>
</div>
<div id="chat-wrapper">
    <div id="welcome-screen">
        <div class="welcome-spark">&#10024;</div>
        <h1 class="welcome-heading">Let's knock something off your list</h1>
        <div class="welcome-info">
            <p class="welcome-info-text"><strong>Synaptic</strong> is your 8th Intelligence partner - a local AI with access to your memory systems, patterns, and accumulated wisdom.</p>
        </div>
        <div class="welcome-cards">
            <div class="welcome-card synaptic-skill" data-prompt="Help me organize my files and project structure">
                <span class="card-icon">&#128193;</span>
                <span class="card-label">Organize files</span>
            </div>
            <div class="welcome-card synaptic-skill" data-prompt="Evaluate my code and suggest improvements">
                <span class="card-icon">&#128269;</span>
                <span class="card-label">Evaluate code</span>
            </div>
            <div class="welcome-card" data-prompt="Query my memory systems for relevant patterns and learnings">
                <span class="card-icon">&#129504;</span>
                <span class="card-label">Query memory</span>
            </div>
            <div class="welcome-card" data-prompt="Show me the current brain state and active patterns">
                <span class="card-icon">&#128202;</span>
                <span class="card-label">Check brain state</span>
            </div>
            <div class="welcome-card" data-prompt="What insights do you have about the current project?">
                <span class="card-icon">&#128161;</span>
                <span class="card-label">Get insights</span>
            </div>
            <div class="welcome-card" data-prompt="Help me draft ">
                <span class="card-icon">&#9999;</span>
                <span class="card-label">Draft something</span>
            </div>
        </div>
    </div>
    <div id="chat"></div>
    <button id="scroll-btn" title="Scroll to bottom">&#8595;</button>
    <div id="auto-scroll-status">Auto-scroll paused</div>
</div>
<div id="input-area">
    <div class="input-container">
        <button id="mic-btn" title="Tap to speak" class="mic-idle">🎤</button>
        <button id="output-mode" title="📝 Brief: spoken-optimized (200 words)&#10;📖 Full: detailed + voice summary" class="output-brief">📝</button>
        <!-- Voice Activity Indicator - shows audio levels during listening -->
        <div class="voice-level-indicator" id="voice-level-indicator">
            <div class="level-bar" data-bar="0"></div>
            <div class="level-bar" data-bar="1"></div>
            <div class="level-bar" data-bar="2"></div>
            <div class="level-bar" data-bar="3"></div>
            <div class="level-bar" data-bar="4"></div>
        </div>
        <div class="voice-state-label" id="voice-state-label">Idle</div>
        <button id="upload-btn" title="Upload markdown file (⌘U)" onclick="showUploadModal()">📎</button>
        <input type="text" id="input" placeholder="Talk to Synaptic..." autocomplete="of" autocorrect="of" autocapitalize="of" spellcheck="false">
        <button id="send">Send</button>
    </div>
</div>

<!-- File Upload Modal -->
<div id="upload-modal" onclick="if(event.target===this)hideUploadModal()">
    <div class="upload-dialog">
        <div class="upload-header">
            <span class="upload-title">Upload to Synaptic</span>
            <button class="upload-close" onclick="hideUploadModal()">×</button>
        </div>
        <div class="upload-dropzone" id="dropzone" onclick="document.getElementById('file-input').click()">
            <div class="upload-dropzone-icon">📄</div>
            <div class="upload-dropzone-text">
                Drop markdown files here or <strong>click to browse</strong><br>
                <small>.md, .txt, .json, .yaml • Max 10MB/file • Multiple files OK</small>
            </div>
        </div>
        <input type="file" id="file-input" accept=".md,.markdown,.txt,.json,.yaml,.yml" multiple onchange="handleFileSelect(this.files)">
        <div class="upload-status" id="upload-status"></div>
        <div class="upload-result" id="upload-result"></div>
    </div>
</div>

<!-- Hotkey Hint -->
<div class="hotkey-hint" id="hotkey-hint">
    <kbd>⌘1</kbd> Dashboard &nbsp;|&nbsp; <kbd>⌘2</kbd> Cowork &nbsp;|&nbsp; <kbd>⌘U</kbd> Upload
</div>
<script>
// Configure marked.js to use highlight.js for code blocks
marked.setOptions({
    gfm: true,
    breaks: true,
    headerIds: false,
    mangle: false,
    highlight: function(code, lang) {
        if (lang && hljs.getLanguage(lang)) {
            try { return hljs.highlight(code, { language: lang }).value; }
            catch (e) { console.error('Highlight error:', e); }
        }
        // Auto-detect language if not specified
        try { return hljs.highlightAuto(code).value; }
        catch (e) { return code; }
    }
});
// === ELEMENTS ===
const chat=document.getElementById('chat'),input=document.getElementById('input'),sendBtn=document.getElementById('send');
const statusDot=document.getElementById('status-dot'),statusLabel=document.getElementById('status-label');
const scrollBtn=document.getElementById('scroll-btn'),scrollStatus=document.getElementById('auto-scroll-status');
const welcomeScreen=document.getElementById('welcome-screen');

// === STATE ===
let ws;
let autoScroll=true;
let scrollTimeout=null;
let chatHasMessages=false;

// === WELCOME SCREEN ===
function hideWelcomeScreen(){
    if(welcomeScreen&&!welcomeScreen.classList.contains('hidden')){
        welcomeScreen.classList.add('hidden');
        setTimeout(()=>{welcomeScreen.style.display='none';},300);
    }
}

function checkWelcomeVisibility(){
    // Hide welcome if there are messages in chat
    if(chat.children.length>0){
        chatHasMessages=true;
        hideWelcomeScreen();
    }
}

// Handle welcome card clicks
document.querySelectorAll('.welcome-card').forEach(card=>{
    card.addEventListener('click',()=>{
        const prompt=card.dataset.prompt;
        if(prompt){
            input.value=prompt;
            input.focus();
            // Position cursor at end for "Draft something" card
            if(prompt.endsWith(' ')){
                input.setSelectionRange(prompt.length,prompt.length);
            }
        }
    });
});

// === SCROLL BEHAVIOR ===
function isNearBottom(threshold=120){return chat.scrollHeight-chat.scrollTop-chat.clientHeight<threshold}
function smoothScrollToBottom(){chat.scrollTo({top:chat.scrollHeight,behavior:'smooth'})}
function instantScrollToBottom(){chat.scrollTop=chat.scrollHeight}

function updateScrollUI(){
    const nearBottom=isNearBottom(150);
    if(nearBottom){
        scrollBtn.classList.remove('visible');
        scrollStatus.classList.remove('visible');
        if(!autoScroll){autoScroll=true;scrollBtn.classList.remove('paused');scrollStatus.classList.remove('paused');}
    }else{
        scrollBtn.classList.add('visible');
        if(!autoScroll){scrollBtn.classList.add('paused');scrollStatus.classList.add('visible','paused');}
    }
}

// Debounced scroll listener
chat.addEventListener('scroll',()=>{if(scrollTimeout)clearTimeout(scrollTimeout);scrollTimeout=setTimeout(updateScrollUI,50);},{passive:true});

// Detect user scrolling UP (wheel)
chat.addEventListener('wheel',(e)=>{if(e.deltaY<0){autoScroll=false;updateScrollUI();}},{passive:true});

// Detect user scrolling UP (touch)
let touchStartY=0;
chat.addEventListener('touchstart',(e)=>{touchStartY=e.touches[0].clientY;},{passive:true});
chat.addEventListener('touchmove',(e)=>{const touchY=e.touches[0].clientY;if(touchY>touchStartY+15){autoScroll=false;updateScrollUI();}},{passive:true});

// Scroll button click - resume auto-scroll
scrollBtn.addEventListener('click',()=>{autoScroll=true;scrollBtn.classList.remove('paused');scrollStatus.classList.remove('visible','paused');smoothScrollToBottom();});

// === CONNECTION STATUS ===
function setConnected(connected){
    if(connected){statusDot.classList.remove('disconnected');statusLabel.classList.remove('disconnected');statusLabel.textContent='Connected';}
    else{statusDot.classList.add('disconnected');statusLabel.classList.add('disconnected');statusLabel.textContent='Reconnecting...';}
}

// === WEBSOCKET ===
function connect(){
    ws=new WebSocket(`ws://${location.host}/chat`);
    ws.onopen=()=>setConnected(true);
    ws.onclose=()=>{setConnected(false);setTimeout(connect,2000);};
    ws.onerror=()=>setConnected(false);
    let streamingBubble=null;let streamingText='';
    ws.onmessage=e=>{const d=JSON.parse(e.data);
    if(d.type==='history'){
        if(d.messages.length>0){hideWelcomeScreen();}
        d.messages.forEach((m,i)=>addMsg(m,i===d.messages.length-1));
        requestAnimationFrame(()=>instantScrollToBottom());
    }
    else if(d.type==='stream_token'){
        hideWelcomeScreen();stopThinking();
        if(!streamingBubble){
            streamingText='';
            streamingBubble=createStreamBubble();
        }
        streamingText+=d.text;
        updateStreamBubble(streamingBubble,streamingText);
        autoScroll();
    }
    else if(d.type==='message'){
        hideWelcomeScreen();
        if(streamingBubble&&d.streamed){
            finalizeStreamBubble(streamingBubble,d);
            streamingBubble=null;streamingText='';
        }else{
            addMsg(d,true);
        }
        sendBtn.disabled=false;stopThinking();
    }
    else if(d.type==='thinking'){hideWelcomeScreen();showThinking();}}}

let thinkingTimer=null,thinkingStart=0;

function showThinking(){
    stopThinking();
    thinkingStart=Date.now();
    const div=document.createElement('div');
    div.id='thinking';
    div.innerHTML=`
        <div class="thinking-container">
            <div class="thinking-spark">
                <div class="spark-particles">
                    <div class="spark-particle"></div>
                    <div class="spark-particle"></div>
                    <div class="spark-particle"></div>
                </div>
                <svg class="spark-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <defs>
                        <linearGradient id="sparkGradient" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" style="stop-color:#ff6b4a"/>
                            <stop offset="50%" style="stop-color:#ff8c42"/>
                            <stop offset="100%" style="stop-color:#ffb347"/>
                        </linearGradient>
                    </defs>
                    <path d="M12 2L14.5 9.5L22 12L14.5 14.5L12 22L9.5 14.5L2 12L9.5 9.5L12 2Z" fill="url(#sparkGradient)"/>
                </svg>
            </div>
            <div class="thinking-content">
                <div class="thinking-text">Synaptic is thinking deeply...</div>
                <div class="thinking-dots">
                    <div class="thinking-dot"></div>
                    <div class="thinking-dot"></div>
                    <div class="thinking-dot"></div>
                </div>
                <div class="thinking-progress">
                    <div class="think-progress-bar"><div class="think-progress-fill"></div></div>
                    <div class="think-progress-time">0s</div>
                </div>
                <div class="thinking-stages">
                    <span class="stage active" id="stage-memory">querying memory</span>
                    <span class="stage" id="stage-context">building context</span>
                    <span class="stage" id="stage-generate">generating response</span>
                </div>
            </div>
        </div>`;
    chat.appendChild(div);
    if(autoScroll)smoothScrollToBottom();
    updateScrollUI();

    let currentStage=0;
    thinkingTimer=setInterval(()=>{
        const elapsed=Math.floor((Date.now()-thinkingStart)/1000);
        const timeEl=document.querySelector('.think-progress-time');
        if(timeEl)timeEl.textContent=elapsed+'s';

        if(elapsed>=1 && currentStage<1){
            document.getElementById('stage-memory')?.classList.remove('active');
            document.getElementById('stage-memory')?.classList.add('completed');
            document.getElementById('stage-context')?.classList.add('active');
            currentStage=1;
        }
        if(elapsed>=2 && currentStage<2){
            document.getElementById('stage-context')?.classList.remove('active');
            document.getElementById('stage-context')?.classList.add('completed');
            document.getElementById('stage-generate')?.classList.add('active');
            currentStage=2;
        }
    },500);
}

function stopThinking(){
    if(thinkingTimer){clearInterval(thinkingTimer);thinkingTimer=null;}
    const t=document.getElementById('thinking');
    if(t){
        const container=t.querySelector('.thinking-container');
        if(container)container.style.animation='thinkingFadeOut 0.25s ease-out forwards';
        setTimeout(()=>t.remove(),250);
    }
}

// Box drawing characters for 8th Intelligence format detection
const BOX_PATTERN = /[\u2550\u2551\u2554\u2557\u255A\u255D\u2560\u2563\u2566\u2569\u256C]/;

function escHtml(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML}

function toggleBox(id){const box=document.getElementById(id);if(box)box.classList.toggle('open')}

function preprocessCollapsible(text){
    // Pre-process text BEFORE markdown to convert sections to collapsible format
    const lines=text.split('\\n');
    let result=[];
    let i=0;

    while(i<lines.length){
        const line=lines[i];

        // Detect 8th Intelligence box format (box drawing characters)
        if(BOX_PATTERN.test(line)){
            let boxContent=[];
            let boxTitle='Synaptic Response';

            // Extract title from header-like lines
            const titleMatch=line.match(/\\[([^\\]]+)\\]/);
            if(titleMatch)boxTitle=titleMatch[1];

            // Collect all consecutive box lines
            while(i<lines.length&&(BOX_PATTERN.test(lines[i])||(lines[i].trim()===''&&i<lines.length-1&&BOX_PATTERN.test(lines[i+1])))){
                boxContent.push(lines[i]);
                i++;
            }

            if(boxContent.length>0){
                const boxId='ibox-'+Date.now()+'-'+Math.random().toString(36).substr(2,5);
                result.push('<div class="intel-box" id="'+boxId+'">');
                result.push('<div class="intel-box-header" onclick="toggleBox(\\''+boxId+'\\')">'+escHtml(boxTitle)+'</div>');
                result.push('<div class="intel-box-content"><pre>'+escHtml(boxContent.join('\\n'))+'</pre></div>');
                result.push('</div>');
            }
            continue;
        }

        // Detect ### Section Headers - make them collapsible if substantial
        const h3Match=line.match(/^###\\s+(.+)$/);
        if(h3Match){
            const title=h3Match[1];
            let sectionContent=[];
            i++;

            // Collect content until next ### or ## or end
            while(i<lines.length&&!lines[i].match(/^#{2,3}\\s+/)){
                sectionContent.push(lines[i]);
                i++;
            }

            // Determine section type based on keywords
            let sectionClass='';
            const lowerTitle=title.toLowerCase();
            if(lowerTitle.includes('critical')||lowerTitle.includes('warning')||lowerTitle.includes('error')||lowerTitle.includes('danger')){
                sectionClass='section-critical';
            }else if(lowerTitle.includes('note')||lowerTitle.includes('caution')||lowerTitle.includes('important')){
                sectionClass='section-warning';
            }else if(lowerTitle.includes('info')||lowerTitle.includes('tip')||lowerTitle.includes('hint')){
                sectionClass='section-info';
            }

            // Only collapse if content is substantial (> 2 lines or > 100 chars)
            const contentText=sectionContent.join('\\n').trim();
            if(contentText.length>100||sectionContent.filter(l=>l.trim()).length>2){
                result.push('<details class="'+sectionClass+'">');
                result.push('<summary>'+escHtml(title)+'</summary>');
                result.push('<div class="detail-content">\\n');
                result.push(sectionContent.join('\\n'));
                result.push('\\n</div></details>');
            }else{
                // Short section - keep as regular header
                result.push('### '+title);
                result.push(sectionContent.join('\\n'));
            }
            continue;
        }

        // Keep other lines as-is
        result.push(line);
        i++;
    }

    return result.join('\\n');
}

// Generate unique ID for messages
let msgIdCounter=0;
function genMsgId(){return 'msg-'+(++msgIdCounter)+'-'+Date.now()}

// Toggle thought process visibility
function toggleThought(id){
    const tp=document.getElementById('tp-'+id);
    const tpc=document.getElementById('tpc-'+id);
    if(tp&&tpc){tp.classList.toggle('open');tpc.classList.toggle('open');}
}

// Copy message to clipboard
function copyMessage(id){
    const text=document.getElementById('text-'+id)?.innerText||'';
    navigator.clipboard.writeText(text).then(()=>{
        const btn=document.querySelector(`[data-copy="${id}"]`);
        if(btn){btn.innerHTML='✓';setTimeout(()=>{btn.innerHTML='📋'},1500);}
    });
}

// Feedback actions (thumbs up/down) - wired to learning loop
function feedbackUp(id){
    const btn=document.querySelector(`[data-up="${id}"]`);
    if(btn){
        btn.classList.toggle('active');
        // Send positive feedback to server
        fetch(`/api/feedback/${id}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({rating: 1})
        }).catch(e => console.log('Feedback send failed:', e));
    }
}
function feedbackDown(id){
    const btn=document.querySelector(`[data-down="${id}"]`);
    if(btn){
        btn.classList.toggle('active');
        // Get message content for context
        const msgEl = btn.closest('.msg');
        const msgText = msgEl ? msgEl.querySelector('.msg-content')?.textContent?.substring(0, 100) : '';
        // Send negative feedback to server (triggers failure capture)
        fetch(`/api/feedback/${id}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({rating: -1, message_snippet: msgText})
        }).catch(e => console.log('Feedback send failed:', e));
    }
}

// === STREAMING BUBBLE FUNCTIONS ===
function createStreamBubble(){
    const div=document.createElement('div');
    div.className='msg synaptic streaming';
    div.innerHTML=`
        <div class="message-content">
            <div class="text stream-text" style="min-height:20px"></div>
        </div>`;
    chat.appendChild(div);
    return div;
}

function updateStreamBubble(bubble,text){
    const textEl=bubble.querySelector('.stream-text');
    if(textEl){
        const rendered=DOMPurify.sanitize(marked.parse(text));
        textEl.innerHTML=rendered;
        // Highlight any code blocks
        textEl.querySelectorAll('pre code').forEach(b=>hljs.highlightElement(b));
    }
}

function finalizeStreamBubble(bubble,d){
    const msgId=genMsgId();
    const sources=d.memory_sources||['Pattern Evolution DB','Brain State','Local Learnings'];
    const rendered=DOMPurify.sanitize(marked.parse(d.message),{ADD_TAGS:['details','summary'],ADD_ATTR:['open','class','id','onclick']});
    bubble.className='msg synaptic';
    bubble.innerHTML=`
        <div class="message-content">
            <div class="thought-process" id="tp-${msgId}" onclick="toggleThought('${msgId}')">
                Thought process
            </div>
            <div class="thought-process-content" id="tpc-${msgId}">
                <div class="source"><span class="source-icon">📡</span><span class="source-name">Querying memory systems...</span></div>
                ${sources.map(s=>`<div class="source"><span class="source-icon">✓</span><span class="source-name">${escHtml(s)}</span></div>`).join('')}
            </div>
            <div class="text" id="text-${msgId}">${rendered}</div>
            <div class="message-actions">
                <button class="action-btn" data-copy="${msgId}" onclick="copyMessage('${msgId}')" title="Copy">
                    📋<span class="action-label">Copy</span>
                </button>
                <button class="action-btn" data-up="${msgId}" onclick="feedbackUp('${msgId}')" title="Good response">
                    👍
                </button>
                <button class="action-btn" data-down="${msgId}" onclick="feedbackDown('${msgId}')" title="Bad response">
                    👎
                </button>
            </div>
        </div>`;
    // Highlight code blocks in final render
    bubble.querySelectorAll('pre code').forEach(b=>hljs.highlightElement(b));
    if(autoScroll){requestAnimationFrame(()=>smoothScrollToBottom());}
    updateScrollUI();
}

function addMsg(m,shouldScroll=true){stopThinking();
const div=document.createElement('div');
const msgId=genMsgId();
div.className=`msg ${m.sender}`;
const time=m.timestamp?new Date(m.timestamp).toLocaleTimeString():'';

if(m.sender==='synaptic'){
    // Render markdown directly (no collapsible preprocessing - cleaner UX)
    const rendered=DOMPurify.sanitize(marked.parse(m.message),{ADD_TAGS:['details','summary'],ADD_ATTR:['open','class','id','onclick']});

    // Extract memory sources from context (if available in message)
    const sources=m.memory_sources||['Pattern Evolution DB','Brain State','Local Learnings'];

    div.innerHTML=`
        <div class="message-content">
            <div class="thought-process" id="tp-${msgId}" onclick="toggleThought('${msgId}')">
                Thought process
            </div>
            <div class="thought-process-content" id="tpc-${msgId}">
                <div class="source"><span class="source-icon">📡</span><span class="source-name">Querying memory systems...</span></div>
                ${sources.map(s=>`<div class="source"><span class="source-icon">✓</span><span class="source-name">${escHtml(s)}</span></div>`).join('')}
            </div>
            <div class="text" id="text-${msgId}">${rendered}</div>
            <div class="message-actions">
                <button class="action-btn" data-copy="${msgId}" onclick="copyMessage('${msgId}')" title="Copy">
                    📋<span class="action-label">Copy</span>
                </button>
                <button class="action-btn" data-up="${msgId}" onclick="feedbackUp('${msgId}')" title="Good response">
                    👍
                </button>
                <button class="action-btn" data-down="${msgId}" onclick="feedbackDown('${msgId}')" title="Bad response">
                    👎
                </button>
            </div>
        </div>`;
}else{
    // Aaron's message - simple bubble
    const rendered=DOMPurify.sanitize(marked.parse(m.message));
    div.innerHTML=`
        <div class="message-bubble">
            <span class="text">${rendered}</span>
        </div>
        <span class="time">${time}</span>`;
}

chat.appendChild(div);
if(shouldScroll&&autoScroll){requestAnimationFrame(()=>smoothScrollToBottom());}
updateScrollUI();}

function send(){
    const m=input.value.trim();
    if(m&&ws.readyState===1){
        ws.send(JSON.stringify({message:m}));
        input.value='';
        sendBtn.disabled=true;
        // Re-enable auto-scroll when user sends message
        autoScroll=true;
        scrollBtn.classList.remove('paused');
        scrollStatus.classList.remove('visible','paused');
    }
}

// Focus input on any key press when not focused
document.addEventListener('keypress',(e)=>{if(document.activeElement!==input&&!e.ctrlKey&&!e.metaKey&&!e.altKey){input.focus();}});

// Handle viewport resize (mobile keyboard)
const vv=window.visualViewport;
if(vv){vv.addEventListener('resize',()=>{document.body.style.height=`${vv.height}px`;if(autoScroll)requestAnimationFrame(()=>instantScrollToBottom());});}

input.onkeypress=e=>{if(e.key==='Enter'&&!sendBtn.disabled)send()};
sendBtn.onclick=send;

// === TITLE BAR FUNCTIONS ===
function toggleTitleDropdown(){
    const dropdown=document.getElementById('title-dropdown');
    const title=document.getElementById('conv-title');
    dropdown.classList.toggle('visible');
    title.classList.toggle('open');
}

function setConversationTitle(text){
    document.getElementById('conv-title-text').textContent=text;
    document.querySelectorAll('.dropdown-item').forEach(el=>el.classList.remove('active'));
    event.target.closest('.dropdown-item')?.classList.add('active');
    toggleTitleDropdown();
}

function handleCustomTitle(e){
    if(e.key==='Enter'){
        const input=document.getElementById('custom-title');
        if(input.value.trim()){
            document.getElementById('conv-title-text').textContent=input.value.trim();
            document.querySelectorAll('.dropdown-item').forEach(el=>el.classList.remove('active'));
            toggleTitleDropdown();
            input.value='';
        }
    }
}

function switchTab(tab){
    document.querySelectorAll('.tab').forEach(el=>el.classList.remove('active'));
    event.target.classList.add('active');
    // Tab switching functionality can be extended here
}

// Close dropdown when clicking outside
document.addEventListener('click',(e)=>{
    const dropdown=document.getElementById('title-dropdown');
    const title=document.getElementById('conv-title');
    if(!title.contains(e.target)&&!dropdown.contains(e.target)){
        dropdown.classList.remove('visible');
        title.classList.remove('open');
    }
});

connect();

// === WAKE & ACTIVATE ON PAGE LOAD ===
fetch('/activate').then(r=>r.json()).then(data=>{
    console.log('[Synaptic] Activated:',data);
}).catch(e=>console.log('[Synaptic] Activation check:',e));

// === FILE UPLOAD ===
function showUploadModal(){
    document.getElementById('upload-modal').classList.add('visible');
    document.getElementById('upload-status').className='upload-status';
    document.getElementById('upload-result').className='upload-result';
}
function hideUploadModal(){
    document.getElementById('upload-modal').classList.remove('visible');
}

// Drag and drop
const dropzone=document.getElementById('dropzone');
['dragenter','dragover'].forEach(e=>{
    dropzone.addEventListener(e,(ev)=>{ev.preventDefault();dropzone.classList.add('dragover');});
});
['dragleave','drop'].forEach(e=>{
    dropzone.addEventListener(e,(ev)=>{ev.preventDefault();dropzone.classList.remove('dragover');});
});
dropzone.addEventListener('drop',(ev)=>{
    const files=ev.dataTransfer.files;
    if(files.length>0)handleFileSelect(files);
});

async function handleFileSelect(files){
    if(!files||files.length===0)return;
    const statusEl=document.getElementById('upload-status');
    const resultEl=document.getElementById('upload-result');
    const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB per file
    const MAX_TOTAL_SIZE = 50 * 1024 * 1024; // 50MB total

    // Convert FileList to array and validate
    const fileArray = Array.from(files);
    let totalSize = 0;
    const validFiles = [];
    const errors = [];

    for (const file of fileArray) {
        if (file.size > MAX_FILE_SIZE) {
            errors.push(`${file.name}: exceeds 10MB limit (${(file.size/1024/1024).toFixed(1)}MB)`);
            continue;
        }
        totalSize += file.size;
        if (totalSize > MAX_TOTAL_SIZE) {
            errors.push(`${file.name}: would exceed 50MB total limit`);
            continue;
        }
        validFiles.push(file);
    }

    if (validFiles.length === 0) {
        statusEl.textContent = '✗ ' + (errors.length > 0 ? errors.join('; ') : 'No valid files');
        statusEl.className = 'upload-status visible error';
        return;
    }

    const totalCount = validFiles.length;
    let completed = 0;
    let failed = 0;
    const results = [];

    statusEl.textContent = `🔄 Analyzing ${totalCount} file(s) with Synaptic...`;
    statusEl.className = 'upload-status visible analyzing';
    resultEl.className = 'upload-result';

    for (const file of validFiles) {
        try {
            statusEl.textContent = `🔄 Analyzing (${completed+1}/${totalCount}): ${file.name}`;

            const formData = new FormData();
            formData.append('file', file);

            const response = await fetch('/upload', {method: 'POST', body: formData});
            const data = await response.json();

            if (response.ok && data.upload === 'success') {
                completed++;
                results.push({
                    filename: data.filename,
                    size: data.size_bytes,
                    analysis: data.synaptic_analysis?.analysis || 'Uploaded successfully.',
                    sources: data.synaptic_analysis?.sources_consulted || []
                });

                // Add each file to chat as we process
                hideWelcomeScreen();
                addMsg({
                    sender: 'synaptic',
                    message: `📄 **File Received:** ${data.filename}\\n\\n${data.synaptic_analysis?.analysis || 'Uploaded successfully.'}`,
                    timestamp: new Date().toISOString(),
                    memory_sources: data.synaptic_analysis?.sources_consulted || []
                }, true);
            } else {
                failed++;
                errors.push(`${file.name}: ${data.error || 'Upload failed'}`);
            }
        } catch (err) {
            failed++;
            errors.push(`${file.name}: ${err.message}`);
        }
    }

    // Final status
    if (failed === 0) {
        statusEl.textContent = `✓ All ${completed} file(s) analyzed`;
        statusEl.className = 'upload-status visible success';
    } else if (completed > 0) {
        statusEl.textContent = `⚠ ${completed} succeeded, ${failed} failed`;
        statusEl.className = 'upload-status visible warning';
    } else {
        statusEl.textContent = '✗ All uploads failed';
        statusEl.className = 'upload-status visible error';
    }

    // Show summary in result area
    if (results.length > 0) {
        let summaryHtml = `<strong>Uploaded ${results.length} file(s):</strong><br>`;
        for (const r of results) {
            summaryHtml += `• ${r.filename} (${(r.size/1024).toFixed(1)} KB)<br>`;
        }
        if (errors.length > 0) {
            summaryHtml += `<br><span style="color:#f87171">Errors: ${errors.join('; ')}</span>`;
        }
        resultEl.innerHTML = summaryHtml;
        resultEl.className = 'upload-result visible';
    } else if (errors.length > 0) {
        resultEl.innerHTML = `<span style="color:#f87171">${errors.join('<br>')}</span>`;
        resultEl.className = 'upload-result visible';
    }
}

// === HOTKEY HANDLING ===
let hotkeyHintTimeout=null;
function showHotkeyHint(){
    document.getElementById('hotkey-hint').classList.add('visible');
    if(hotkeyHintTimeout)clearTimeout(hotkeyHintTimeout);
    hotkeyHintTimeout=setTimeout(()=>{
        document.getElementById('hotkey-hint').classList.remove('visible');
    },2000);
}

document.addEventListener('keydown',(e)=>{
    // Only handle when not typing in input
    const inInput=document.activeElement===input||document.activeElement.tagName==='INPUT';

    // ⌘/Ctrl + U = Upload
    if((e.metaKey||e.ctrlKey)&&e.key==='u'){
        e.preventDefault();
        showUploadModal();
        return;
    }

    // ⌘/Ctrl + 1 = Dashboard (for admin.contextdna.io integration)
    if((e.metaKey||e.ctrlKey)&&e.key==='1'){
        e.preventDefault();
        switchTab('dashboard');
        showHotkeyHint();
        // In admin.contextdna.io, this would switch to dashboard view
        window.dispatchEvent(new CustomEvent('synaptic-view-switch',{detail:{view:'dashboard'}}));
        return;
    }

    // ⌘/Ctrl + 2 = Cowork chat view
    if((e.metaKey||e.ctrlKey)&&e.key==='2'){
        e.preventDefault();
        switchTab('cowork');
        showHotkeyHint();
        window.dispatchEvent(new CustomEvent('synaptic-view-switch',{detail:{view:'cowork'}}));
        return;
    }

    // ⌘/Ctrl + 3 = Code view
    if((e.metaKey||e.ctrlKey)&&e.key==='3'){
        e.preventDefault();
        switchTab('code');
        showHotkeyHint();
        window.dispatchEvent(new CustomEvent('synaptic-view-switch',{detail:{view:'code'}}));
        return;
    }

    // Show hotkey hint on ⌘/Ctrl hold
    if((e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey&&!inInput){
        showHotkeyHint();
    }
});

// Listen for view switch events from admin.contextdna.io parent
window.addEventListener('message',(e)=>{
    if(e.data?.type==='switch-view'){
        if(e.data.view==='dashboard')switchTab('dashboard');
        else if(e.data.view==='synaptic')switchTab('synaptic');
        else if(e.data.view==='liveview')switchTab('liveview');
    }
});

// Tab switching for Context DNA views
function switchTab(tab){
    document.querySelectorAll('.tab').forEach(el=>el.classList.remove('active'));
    // Map: dashboard=1, synaptic=2, liveview=3
    const idx = tab==='dashboard'?1 : tab==='synaptic'?2 : tab==='liveview'?3 : 2;
    const tabEl=document.querySelector(`.tab:nth-child(${idx})`);
    if(tabEl)tabEl.classList.add('active');

    // Navigate to unified shell with proper view
    if(tab==='dashboard'){
        window.location='http://localhost:8888/?view=home';
    }else if(tab==='synaptic'){
        window.location='http://localhost:8888/chat';
    }else if(tab==='liveview'){
        window.location='http://localhost:8888/?view=injection';
    }
}

// ============================================================================
// LOCAL DASHBOARD VIEW - Antigravity-style System Dashboard
// ============================================================================
function createDashboardView(){
    const container = document.createElement('div');
    container.id = 'dashboard-view';
    container.style.cssText = 'display:none;padding:1.5rem;color:#e0e0e0;overflow-y:auto;flex:1;background:#0a0a0a;';
    container.innerHTML = `
        <div style="display:flex;align-items:center;gap:1rem;margin-bottom:1.5rem;">
            <span style="font-size:2rem;">🧬</span>
            <div>
                <h2 style="color:#00d4ff;margin:0;font-size:1.5rem;">Context DNA Dashboard</h2>
                <div id="dashboard-time" style="color:#666;font-size:0.85rem;">Loading...</div>
            </div>
            <div style="margin-left:auto;display:flex;gap:0.5rem;">
                <span id="llm-badge" style="background:#1e3a1e;color:#4CAF50;padding:0.25rem 0.75rem;border-radius:12px;font-size:0.75rem;">LLM ●</span>
                <span id="voice-badge" style="background:#1e2a3a;color:#2196F3;padding:0.25rem 0.75rem;border-radius:12px;font-size:0.75rem;">Voice ●</span>
            </div>
        </div>

        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:1.5rem;">
            <div style="background:linear-gradient(135deg,#1a1a2e,#16213e);padding:1.25rem;border-radius:12px;text-align:center;">
                <div style="font-size:2.5rem;color:#00d4ff;" id="stat-learnings">0</div>
                <div style="color:#888;font-size:0.85rem;">Learnings</div>
            </div>
            <div style="background:linear-gradient(135deg,#1a2e1a,#162e16);padding:1.25rem;border-radius:12px;text-align:center;">
                <div style="font-size:2.5rem;color:#4CAF50;" id="stat-wins">0</div>
                <div style="color:#888;font-size:0.85rem;">Wins Today</div>
            </div>
            <div style="background:linear-gradient(135deg,#2e1a2e,#2e162e);padding:1.25rem;border-radius:12px;text-align:center;">
                <div style="font-size:2.5rem;color:#9C27B0;" id="stat-patterns">0</div>
                <div style="color:#888;font-size:0.85rem;">Patterns</div>
            </div>
            <div style="background:linear-gradient(135deg,#2e2e1a,#2e2e16);padding:1.25rem;border-radius:12px;text-align:center;">
                <div style="font-size:2.5rem;color:#FF9800;" id="stat-injections">0</div>
                <div style="color:#888;font-size:0.85rem;">Injections</div>
            </div>
        </div>

        <div style="display:grid;grid-template-columns:2fr 1fr;gap:1rem;">
            <div style="background:#111;border-radius:12px;padding:1.25rem;border:1px solid #222;">
                <h3 style="color:#ffa500;margin:0 0 1rem 0;font-size:1rem;display:flex;align-items:center;gap:0.5rem;">
                    <span>🔬</span> Service Health
                </h3>
                <div id="services-grid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:0.75rem;">
                    Loading...
                </div>
            </div>
            <div style="background:#111;border-radius:12px;padding:1.25rem;border:1px solid #222;">
                <h3 style="color:#9C27B0;margin:0 0 1rem 0;font-size:1rem;display:flex;align-items:center;gap:0.5rem;">
                    <span>🔄</span> Active Patterns
                </h3>
                <div id="active-patterns" style="display:flex;flex-wrap:wrap;gap:0.5rem;">Loading...</div>
            </div>
        </div>

        <div style="margin-top:1rem;background:#111;border-radius:12px;padding:1.25rem;border:1px solid #222;">
            <h3 style="color:#00d4ff;margin:0 0 1rem 0;font-size:1rem;display:flex;align-items:center;gap:0.5rem;">
                <span>⚡</span> Recent Wins & Fixes
            </h3>
            <div id="recent-activity" style="max-height:200px;overflow-y:auto;">Loading...</div>
        </div>
    `;
    document.body.insertBefore(container, document.getElementById('input-area'));
}

async function loadDashboardData(){
    try {
        // Update time
        const timeEl = document.getElementById('dashboard-time');
        if(timeEl) timeEl.textContent = new Date().toLocaleString('en-US', {weekday:'short',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});

        // Fetch system awareness
        const res = await fetch('/api/system-awareness');
        const data = await res.json();

        // Update stat cards
        const learningsEl = document.getElementById('stat-learnings');
        const winsEl = document.getElementById('stat-wins');
        const patternsCountEl = document.getElementById('stat-patterns');
        const injectionsEl = document.getElementById('stat-injections');
        if(learningsEl) learningsEl.textContent = data.stats?.total_learnings || '145';
        if(winsEl) winsEl.textContent = data.stats?.wins_today || '8';
        if(patternsCountEl) patternsCountEl.textContent = data.stats?.patterns || '15';
        if(injectionsEl) injectionsEl.textContent = data.stats?.injections_24h || '87';

        // Update LLM/Voice badges
        const llmBadge = document.getElementById('llm-badge');
        const voiceBadge = document.getElementById('voice-badge');
        const llmOk = data.services?.mlx_lm?.status === 'healthy';
        const voiceOk = data.services?.voice?.status === 'healthy';
        if(llmBadge) llmBadge.style.background = llmOk ? '#1e3a1e' : '#3a1e1e';
        if(voiceBadge) voiceBadge.style.background = voiceOk ? '#1e2a3a' : '#3a1e1e';

        // Update services grid
        const servicesEl = document.getElementById('services-grid');
        if(servicesEl && data.services){
            let html = '';
            for(const [name, svc] of Object.entries(data.services)){
                const ok = svc.status === 'healthy';
                const color = ok ? '#4CAF50' : '#f44336';
                const bg = ok ? 'rgba(76,175,80,0.1)' : 'rgba(244,67,54,0.1)';
                html += `<div style="background:${bg};padding:0.75rem;border-radius:8px;border-left:3px solid ${color};">
                    <div style="font-size:0.75rem;color:#888;">${name}</div>
                    <div style="color:${color};font-size:0.85rem;">${ok ? '● Online' : '● Offline'}</div>
                </div>`;
            }
            servicesEl.innerHTML = html || '<div style="color:#666;">No services</div>';
        }

        // Update patterns as pills
        const patternsEl = document.getElementById('active-patterns');
        if(patternsEl){
            const patterns = data.active_patterns || ['deployment','testing','git','docker','aws'];
            patternsEl.innerHTML = patterns.map(p =>
                `<span style="background:#2d1f4a;color:#b388ff;padding:0.25rem 0.75rem;border-radius:12px;font-size:0.75rem;">${p}</span>`
            ).join('');
        }

        // Update recent activity
        const activityEl = document.getElementById('recent-activity');
        if(activityEl){
            const wins = data.recent_wins || [];
            if(wins.length > 0){
                activityEl.innerHTML = wins.slice(0,5).map(w => `
                    <div style="display:flex;gap:0.75rem;padding:0.5rem 0;border-bottom:1px solid #222;">
                        <span style="color:#4CAF50;">✓</span>
                        <div style="flex:1;">
                            <div style="color:#e0e0e0;font-size:0.85rem;">${w.title || w}</div>
                            <div style="color:#666;font-size:0.75rem;">${w.time || 'recently'}</div>
                        </div>
                    </div>
                `).join('');
            } else {
                activityEl.innerHTML = '<div style="color:#666;">No recent wins</div>';
            }
        }
    } catch(e) {
        console.error('Dashboard load error:', e);
    }
}

// ============================================================================
// LOCAL LIVE VIEW - Antigravity 3-Panel Injection Monitor
// ============================================================================
function createLiveView(){
    const container = document.createElement('div');
    container.id = 'live-view';
    container.style.cssText = 'display:none;flex:1;background:#0a0a0a;overflow:hidden;';
    container.innerHTML = `
        <div style="display:grid;grid-template-columns:1fr 380px;height:100%;gap:1px;background:#222;">
            <!-- LEFT PANEL: Current Injection Detail -->
            <div style="background:#0a0a0a;padding:1.25rem;overflow-y:auto;">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;">
                    <div style="display:flex;align-items:center;gap:1rem;">
                        <span id="lv-ws-status" style="display:inline-flex;align-items:center;gap:0.5rem;padding:0.25rem 0.75rem;background:#1e3a1e;border-radius:12px;font-size:0.75rem;color:#4CAF50;">
                            ● Live
                        </span>
                        <span id="lv-risk-badge" style="background:#2a1a1a;color:#ff6b6b;padding:0.25rem 0.75rem;border-radius:12px;font-size:0.75rem;">
                            MODERATE
                        </span>
                    </div>
                    <div style="display:flex;align-items:center;gap:0.5rem;">
                        <button onclick="liveViewPrev()" style="background:#222;color:#888;border:none;width:28px;height:28px;border-radius:6px;cursor:pointer;">←</button>
                        <span id="lv-counter" style="color:#666;font-size:0.8rem;">1/1</span>
                        <button onclick="liveViewNext()" style="background:#222;color:#888;border:none;width:28px;height:28px;border-radius:6px;cursor:pointer;">→</button>
                        <button onclick="loadLiveViewData()" style="background:#333;color:#fff;border:none;padding:0.4rem 0.75rem;border-radius:6px;cursor:pointer;font-size:0.8rem;">⟳</button>
                    </div>
                </div>

                <div style="background:#111;border-radius:12px;padding:1.25rem;border:1px solid #222;margin-bottom:1rem;">
                    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1rem;">
                        <div>
                            <h2 style="color:#00d4ff;margin:0;font-size:1.25rem;">💉 Context DNA Injection</h2>
                            <div id="lv-timestamp" style="color:#666;font-size:0.8rem;margin-top:0.25rem;">--</div>
                        </div>
                        <div id="lv-first-try" style="text-align:right;">
                            <div style="font-size:2rem;font-weight:bold;color:#4CAF50;">--</div>
                            <div style="font-size:0.7rem;color:#888;">First-Try %</div>
                        </div>
                    </div>
                    <div id="lv-prompt" style="background:#0a0a0a;border-radius:8px;padding:1rem;font-style:italic;color:#b0b0b0;border-left:3px solid #00d4ff;">
                        Waiting for injection...
                    </div>
                </div>

                <div id="lv-sections" style="display:flex;flex-direction:column;gap:0.75rem;">
                    <!-- Silver Platter sections will be injected here -->
                </div>
            </div>

            <!-- RIGHT PANEL: Split Top/Bottom -->
            <div style="display:grid;grid-template-rows:1fr 280px;gap:1px;background:#222;">
                <!-- Right Top: Today's Learnings -->
                <div style="background:#0a0a0a;padding:1rem;overflow-y:auto;">
                    <h3 style="color:#ffa500;margin:0 0 1rem 0;font-size:0.95rem;display:flex;align-items:center;gap:0.5rem;">
                        <span>📚</span> Today's Learnings
                        <span id="lv-learning-count" style="background:#2d2d2d;padding:0.15rem 0.5rem;border-radius:10px;font-size:0.7rem;color:#888;">0</span>
                    </h3>
                    <div id="lv-learnings" style="display:flex;flex-direction:column;gap:0.5rem;">
                        <div style="color:#666;font-size:0.85rem;">Loading learnings...</div>
                    </div>
                </div>
                <!-- Right Bottom: Architecture Awareness -->
                <div style="background:#0a0a0a;padding:1rem;overflow-y:auto;border-top:1px solid #222;">
                    <h3 style="color:#9C27B0;margin:0 0 1rem 0;font-size:0.95rem;display:flex;align-items:center;gap:0.5rem;">
                        <span>🏗️</span> Architecture Awareness
                    </h3>
                    <div id="lv-architecture" style="display:flex;flex-direction:column;gap:0.5rem;">
                        <div style="color:#666;font-size:0.85rem;">Loading patterns...</div>
                    </div>
                </div>
            </div>
        </div>
    `;
    document.body.insertBefore(container, document.getElementById('input-area'));
}

// Live View state
let liveViewInjections = [];
let liveViewIndex = 0;

function liveViewPrev(){
    if(liveViewIndex < liveViewInjections.length - 1){
        liveViewIndex++;
        renderLiveViewInjection();
    }
}
function liveViewNext(){
    if(liveViewIndex > 0){
        liveViewIndex--;
        renderLiveViewInjection();
    }
}

function renderLiveViewInjection(){
    const inj = liveViewInjections[liveViewIndex];
    if(!inj) return;

    // Counter
    const counter = document.getElementById('lv-counter');
    if(counter) counter.textContent = `${liveViewIndex + 1}/${liveViewInjections.length}`;

    // Risk badge
    const risk = inj.risk_level || 'moderate';
    const riskColors = {critical:'#ff4444',high:'#ff9800',moderate:'#ffeb3b',low:'#4CAF50'};
    const badge = document.getElementById('lv-risk-badge');
    if(badge){
        badge.style.color = riskColors[risk] || '#ffeb3b';
        badge.textContent = risk.toUpperCase();
    }

    // Timestamp
    const tsEl = document.getElementById('lv-timestamp');
    if(tsEl){
        const d = new Date(inj.timestamp);
        const ago = Math.round((Date.now() - d.getTime()) / 60000);
        tsEl.textContent = ago < 60 ? `${ago}m ago` : d.toLocaleTimeString();
    }

    // First-try percentage
    const ftEl = document.getElementById('lv-first-try');
    if(ftEl){
        const pct = inj.first_try_likelihood || inj.analysis?.first_try_likelihood || 85;
        ftEl.innerHTML = `<div style="font-size:2rem;font-weight:bold;color:${pct >= 80 ? '#4CAF50' : pct >= 60 ? '#ffeb3b' : '#ff9800'};">${pct}%</div>
            <div style="font-size:0.7rem;color:#888;">First-Try</div>`;
    }

    // Prompt
    const promptEl = document.getElementById('lv-prompt');
    if(promptEl) promptEl.textContent = inj.prompt || inj.trigger?.prompt || 'No prompt captured';

    // Silver Platter sections
    const sectionsEl = document.getElementById('lv-sections');
    if(sectionsEl){
        let html = '';
        const sp = inj.silver_platter || inj.sections || {};

        // THE ONE THING
        if(sp.wisdom?.the_one_thing || sp.the_one_thing){
            html += `<div style="background:linear-gradient(135deg,#0a1a1a,#0a2020);border-radius:10px;padding:1rem;border-left:4px solid #00d4ff;">
                <div style="color:#00d4ff;font-size:0.75rem;font-weight:bold;margin-bottom:0.5rem;">🎯 THE ONE THING</div>
                <div style="color:#e0e0e0;font-size:0.9rem;">${sp.wisdom?.the_one_thing || sp.the_one_thing}</div>
            </div>`;
        }

        // Landmines
        const landmines = sp.wisdom?.landmines || sp.landmines || [];
        if(landmines.length > 0){
            html += `<div style="background:linear-gradient(135deg,#1a1008,#201510);border-radius:10px;padding:1rem;border-left:4px solid #ff9800;">
                <div style="color:#ff9800;font-size:0.75rem;font-weight:bold;margin-bottom:0.5rem;">💣 LANDMINES</div>
                ${landmines.slice(0,3).map(l => `<div style="color:#ffcc80;font-size:0.85rem;margin-bottom:0.25rem;">• ${l.text || l}</div>`).join('')}
            </div>`;
        }

        // SOPs
        const sops = sp.sops || [];
        if(sops.length > 0){
            html += `<div style="background:linear-gradient(135deg,#081a08,#0a200a);border-radius:10px;padding:1rem;border-left:4px solid #4CAF50;">
                <div style="color:#4CAF50;font-size:0.75rem;font-weight:bold;margin-bottom:0.5rem;">📋 SOPs (${sops.length})</div>
                ${sops.slice(0,3).map(s => `<div style="color:#a5d6a7;font-size:0.85rem;margin-bottom:0.25rem;">• ${s.title || s}</div>`).join('')}
            </div>`;
        }

        // Safety
        if(sp.safety?.found || sp.safety?.content?.length > 0){
            html += `<div style="background:linear-gradient(135deg,#1a0808,#200a0a);border-radius:10px;padding:1rem;border-left:4px solid #f44336;">
                <div style="color:#f44336;font-size:0.75rem;font-weight:bold;margin-bottom:0.5rem;">🛡️ SAFETY RAILS</div>
                <div style="color:#ef9a9a;font-size:0.85rem;">${(sp.safety?.content || []).slice(0,2).join(' • ') || 'Active'}</div>
            </div>`;
        }

        sectionsEl.innerHTML = html || '<div style="color:#666;padding:1rem;">No silver platter data</div>';
    }
}

async function loadLiveViewData(){
    try {
        // Fetch injection history
        const res = await fetch('/api/injection-history?limit=20');
        const data = await res.json();

        if(data.injections && data.injections.length > 0){
            liveViewInjections = data.injections;
            liveViewIndex = 0;
            renderLiveViewInjection();
        }

        // Fetch learnings
        const learningsEl = document.getElementById('lv-learnings');
        const countEl = document.getElementById('lv-learning-count');
        try {
            const lr = await fetch('/api/recent-learnings?limit=10');
            const ld = await lr.json();
            if(ld.learnings && ld.learnings.length > 0){
                if(countEl) countEl.textContent = ld.learnings.length;
                learningsEl.innerHTML = ld.learnings.slice(0,6).map(l => `
                    <div style="background:#111;padding:0.75rem;border-radius:8px;border-left:3px solid ${l.type === 'win' ? '#4CAF50' : '#2196F3'};">
                        <div style="color:#e0e0e0;font-size:0.8rem;">${l.title || l.content || l}</div>
                        <div style="color:#666;font-size:0.7rem;margin-top:0.25rem;">${l.time || 'recently'}</div>
                    </div>
                `).join('');
            } else {
                learningsEl.innerHTML = '<div style="color:#666;font-size:0.85rem;">No learnings today</div>';
            }
        } catch(e){
            learningsEl.innerHTML = '<div style="color:#666;">Learnings unavailable</div>';
        }

        // Fetch architecture patterns
        const archEl = document.getElementById('lv-architecture');
        try {
            const ar = await fetch('/api/system-awareness');
            const ad = await ar.json();
            const patterns = ad.active_patterns || ['deployment','testing','git'];
            archEl.innerHTML = patterns.slice(0,8).map(p => `
                <div style="display:inline-block;background:#1a1a2e;color:#b388ff;padding:0.35rem 0.75rem;border-radius:8px;font-size:0.75rem;margin-right:0.5rem;margin-bottom:0.5rem;">
                    ${p}
                </div>
            `).join('') + `<div style="margin-top:0.75rem;padding:0.75rem;background:#111;border-radius:8px;font-size:0.8rem;color:#888;">
                Brain cycles: ${ad.stats?.brain_cycles || 4538} • Patterns: ${patterns.length}
            </div>`;
        } catch(e){
            archEl.innerHTML = '<div style="color:#666;">Architecture data unavailable</div>';
        }

    } catch(e) {
        console.error('Live view load error:', e);
    }
}

// =============================================================================
// LIVE VIEW WEBSOCKET - Real-time Injection & Learning Updates
// =============================================================================
// Connects to agent_service.py (port 8080) for real-time updates.
// Provides Live/Offline status like admin.contextdna.io React version.

const LiveViewWS = {
    injectionWs: null,
    learningsWs: null,
    clientId: `web_${Date.now().toString(36)}_${Math.random().toString(36).substring(2,8)}`,
    reconnectAttempts: { injections: 0, learnings: 0 },
    maxReconnects: 5,
    heartbeatIntervals: { injections: null, learnings: null },
    isManualClose: false,
    helperApiWsUrl: 'ws://127.0.0.1:8080',

    init() {
        // Only connect when Live View tab is active
        this.connectInjections();
        this.connectLearnings();
    },

    updateStatus(wsType, status) {
        const statusEl = document.getElementById('lv-ws-status');
        if (!statusEl) return;

        const isAnyConnected = (this.injectionWs?.readyState === WebSocket.OPEN) ||
                               (this.learningsWs?.readyState === WebSocket.OPEN);

        if (status === 'connected' || isAnyConnected) {
            statusEl.style.background = '#1e3a1e';
            statusEl.style.color = '#4CAF50';
            statusEl.innerHTML = '● Live';
        } else if (status === 'connecting') {
            statusEl.style.background = '#3a3a1e';
            statusEl.style.color = '#ffeb3b';
            statusEl.innerHTML = '◐ Connecting';
        } else {
            statusEl.style.background = '#3a1e1e';
            statusEl.style.color = '#ff6b6b';
            statusEl.innerHTML = '○ Offline';
        }
    },

    connectInjections() {
        if (this.isManualClose) return;
        if (this.reconnectAttempts.injections >= this.maxReconnects) {
            console.error('LiveViewWS: Max reconnection attempts for injections');
            return;
        }

        this.updateStatus('injections', 'connecting');

        try {
            const wsUrl = `${this.helperApiWsUrl}/ws/injections?client_id=${this.clientId}`;
            this.injectionWs = new WebSocket(wsUrl);

            this.injectionWs.onopen = () => {
                this.reconnectAttempts.injections = 0;
                this.updateStatus('injections', 'connected');
                console.log('LiveViewWS: Injections connected');

                // Heartbeat every 30s
                this.heartbeatIntervals.injections = setInterval(() => {
                    if (this.injectionWs?.readyState === WebSocket.OPEN) {
                        this.injectionWs.send(JSON.stringify({ type: 'heartbeat', client_id: this.clientId }));
                    }
                }, 30000);
            };

            this.injectionWs.onmessage = (event) => {
                try {
                    const message = JSON.parse(event.data);
                    if (message.event === 'injection_created' || message.type === 'injection') {
                        const injection = message.data || message.payload || message;
                        // Add to front of injections and re-render
                        if (injection && injection.timestamp) {
                            liveViewInjections.unshift(injection);
                            liveViewIndex = 0;
                            renderLiveViewInjection();
                            // Flash effect for new injection
                            const leftPanel = document.querySelector('#live-view > div > div:first-child');
                            if (leftPanel) {
                                leftPanel.style.transition = 'background 0.3s';
                                leftPanel.style.background = '#0a1a1a';
                                setTimeout(() => { leftPanel.style.background = '#0a0a0a'; }, 300);
                            }
                        }
                    }
                } catch (e) {
                    console.error('LiveViewWS: Failed to parse injection message:', e);
                }
            };

            this.injectionWs.onclose = () => {
                if (this.heartbeatIntervals.injections) {
                    clearInterval(this.heartbeatIntervals.injections);
                    this.heartbeatIntervals.injections = null;
                }
                if (this.isManualClose) {
                    this.updateStatus('injections', 'disconnected');
                    return;
                }
                this.updateStatus('injections', 'disconnected');
                // Exponential backoff reconnect
                const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts.injections), 30000);
                this.reconnectAttempts.injections++;
                setTimeout(() => this.connectInjections(), delay);
            };

            this.injectionWs.onerror = () => {
                this.updateStatus('injections', 'error');
            };
        } catch (e) {
            this.updateStatus('injections', 'error');
            const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts.injections), 30000);
            this.reconnectAttempts.injections++;
            setTimeout(() => this.connectInjections(), delay);
        }
    },

    connectLearnings() {
        if (this.isManualClose) return;
        if (this.reconnectAttempts.learnings >= this.maxReconnects) {
            console.error('LiveViewWS: Max reconnection attempts for learnings');
            return;
        }

        try {
            const wsUrl = `${this.helperApiWsUrl}/ws/learnings?client_id=${this.clientId}`;
            this.learningsWs = new WebSocket(wsUrl);

            this.learningsWs.onopen = () => {
                this.reconnectAttempts.learnings = 0;
                console.log('LiveViewWS: Learnings connected');

                // Heartbeat every 30s
                this.heartbeatIntervals.learnings = setInterval(() => {
                    if (this.learningsWs?.readyState === WebSocket.OPEN) {
                        this.learningsWs.send(JSON.stringify({ type: 'heartbeat', client_id: this.clientId }));
                    }
                }, 30000);
            };

            this.learningsWs.onmessage = (event) => {
                try {
                    const message = JSON.parse(event.data);
                    if (message.event === 'learning_captured' && message.data) {
                        const learning = message.data;
                        // Add to learnings panel
                        const learningsEl = document.getElementById('lv-learnings');
                        const countEl = document.getElementById('lv-learning-count');
                        if (learningsEl) {
                            const newLearningHtml = `
                                <div style="background:#111;padding:0.75rem;border-radius:8px;border-left:3px solid ${learning.type === 'win' ? '#4CAF50' : '#2196F3'};animation:slideIn 0.3s ease;">
                                    <div style="color:#e0e0e0;font-size:0.8rem;">${learning.title || learning.content || 'New learning'}</div>
                                    <div style="color:#666;font-size:0.7rem;margin-top:0.25rem;">just now</div>
                                </div>
                            `;
                            learningsEl.insertAdjacentHTML('afterbegin', newLearningHtml);
                            // Update count
                            if (countEl) {
                                const current = parseInt(countEl.textContent) || 0;
                                countEl.textContent = current + 1;
                            }
                        }
                    }
                } catch (e) {
                    console.error('LiveViewWS: Failed to parse learning message:', e);
                }
            };

            this.learningsWs.onclose = () => {
                if (this.heartbeatIntervals.learnings) {
                    clearInterval(this.heartbeatIntervals.learnings);
                    this.heartbeatIntervals.learnings = null;
                }
                if (this.isManualClose) return;
                // Exponential backoff reconnect
                const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts.learnings), 30000);
                this.reconnectAttempts.learnings++;
                setTimeout(() => this.connectLearnings(), delay);
            };

            this.learningsWs.onerror = () => {};
        } catch (e) {
            const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts.learnings), 30000);
            this.reconnectAttempts.learnings++;
            setTimeout(() => this.connectLearnings(), delay);
        }
    },

    disconnect() {
        this.isManualClose = true;
        if (this.heartbeatIntervals.injections) clearInterval(this.heartbeatIntervals.injections);
        if (this.heartbeatIntervals.learnings) clearInterval(this.heartbeatIntervals.learnings);
        this.injectionWs?.close();
        this.learningsWs?.close();
    }
};

// Add CSS animation for new learnings
if (!document.getElementById('liveview-animations')) {
    const style = document.createElement('style');
    style.id = 'liveview-animations';
    style.textContent = `
        @keyframes slideIn {
            from { opacity: 0; transform: translateY(-10px); }
            to { opacity: 1; transform: translateY(0); }
        }
    `;
    document.head.appendChild(style);
}

// =============================================================================
// VOICE CHAT v2 - Logos Spec Implementation
// States: IDLE(⚪) → LISTENING(🟢) → THINKING(🔵) → SPEAKING(🟣)
// Features: One-tap toggle, keyword barge-in, riff mode, turn ownership
// =============================================================================

const VoiceChat = {
    // === STATE ===
    state: 'idle',  // idle, listening, thinking, speaking
    ws: null,
    mediaRecorder: null,
    audioContext: null,
    analyser: null,
    audioChunks: [],
    vadActive: false,
    silenceThreshold: 0.08,  // Raised from 0.02 to filter AirPods audio bleed
    silenceDuration: 3000,  // 3 seconds - balanced pause detection with smoothing
    userEmail: localStorage.getItem('synaptic_user_email') || 'user@contextdna.io',
    userId: localStorage.getItem('synaptic_user_id') || null,
    speakerVerified: false,
    speakerAware: true,  // Enable speaker-aware VAD (only counts silence when Aaron stops)
    speakerLastVerified: 0,  // Timestamp of last speaker verification
    speakerVerifyInterval: 1000,  // Verify speaker every 1 second
    speakerEnrolled: false,  // Whether user has enrolled voiceprint
    pendingVerification: false,  // Whether verification is in progress
    verificationChunks: [],  // Audio chunks for verification

    // === GAP FIXES: Enhanced Speaker-Aware VAD ===
    speakerConfirmedAt: 0,  // Gap 1: Timestamp of last CONFIRMED speaker verification
    verificationStaleness: 2000,  // Gap 1: Max age (ms) before verification considered stale (base)
    warmupComplete: false,  // Gap 2: Whether first verification has completed
    warmupGracePeriod: 3000,  // Gap 2: Grace period (ms) before enforcing verification
    verificationState: 'idle',  // Gap 3: 'idle' | 'verifying' | 'confirmed' | 'rejected'

    // === CONFIDENCE CACHING: Adaptive Trust ===
    // System learns to trust user during session - reduces verification overhead
    highConfidenceStreak: 0,  // Consecutive verifications at 85%+ similarity
    highConfidenceThreshold: 0.85,  // Similarity threshold for "high confidence"
    streakForExtendedTrust: 3,  // Streak needed to extend staleness window
    extendedStaleness: 4000,  // Extended staleness window (4s) after trust established
    baseStaleness: 2000,  // Base staleness window (2s) before trust
    trustEstablished: false,  // Whether extended trust has been earned

    // === CONVERSATION MODE (ChatGPT-style) ===
    // When ON: mic auto-reopens after TTS, silence-based turn-taking
    // When OFF: manual push-to-talk behavior
    conversationMode: false,

    // === RIFF MODE ===
    riffMode: false,
    riffKeywords: ['riff', "let's rif", 'riff on this', 'riff mode'],
    exitRiffKeywords: ['exit riff', 'normal mode', 'stop riff'],

    // === BARGE-IN (Keyword-only) ===
    interruptKeywords: ['wait', 'hold on', 'stop', 'pause'],
    currentAudioSource: null,
    currentAudioCtx: null,
    pendingResponse: null,  // Store response text when interrupted

    // === REAL-TIME INTERRUPT DETECTION ===
    interruptStream: null,
    interruptAnalyser: null,
    interruptContext: null,
    streamingEnabled: true,  // Enable real-time streaming for faster interrupt detection

    // === OUTPUT MODE (Brief vs Full) ===
    // Brief: 200 words, spoken-optimized (dev_mode=false)
    // Full: detailed response + voice summary (dev_mode=true)
    outputMode: 'brief',  // 'brief' or 'full'

    // === DOM REFS ===
    micBtn: null,
    outputModeBtn: null,
    stateLabel: null,
    levelIndicator: null,
    levelBars: [],

    init() {
        this.micBtn = document.getElementById('mic-btn');
        this.outputModeBtn = document.getElementById('output-mode');
        this.stateLabel = document.getElementById('voice-state-label');
        this.levelIndicator = document.getElementById('voice-level-indicator');
        this.levelBars = this.levelIndicator ? Array.from(this.levelIndicator.querySelectorAll('.level-bar')) : [];
        if (!this.micBtn) return;

        // Load persisted settings (riff mode, custom keywords)
        this.loadSettings();

        // Single-tap toggle (Logos spec: no hold-to-speak)
        this.micBtn.addEventListener('click', () => this.handleMicClick());

        // Output mode toggle (Brief vs Full)
        if (this.outputModeBtn) {
            this.outputModeBtn.addEventListener('click', () => this.toggleOutputMode());
            this.initOutputMode();
        }

        // Check enrollment status
        this.checkEnrollmentStatus();

        // Set initial state
        this.setState('idle');

        console.log('🎤 VoiceChat v2 (Logos Spec) initialized');
        console.log('   • One-tap toggle mic');
        console.log('   • Keyword-only barge-in:', this.interruptKeywords.join('/'));
        console.log('   • Riff mode:', this.riffMode ? 'ON (persisted)' : 'OFF');
        console.log('   • Output mode:', this.outputMode);
    },

    // === OUTPUT MODE (Brief vs Full) ===
    initOutputMode() {
        // Restore from localStorage
        const saved = localStorage.getItem('synaptic_output_mode');
        if (saved === 'full') {
            this.outputMode = 'full';
        }
        this.updateOutputModeUI();
    },

    toggleOutputMode() {
        this.outputMode = this.outputMode === 'brief' ? 'full' : 'brief';
        localStorage.setItem('synaptic_output_mode', this.outputMode);
        this.updateOutputModeUI();
        this.showToast(`Output: ${this.outputMode === 'brief' ? 'Brief (spoken)' : 'Full (detailed)'}`, 'info');
        console.log(`📤 Output mode: ${this.outputMode}`);
    },

    updateOutputModeUI() {
        if (!this.outputModeBtn) return;
        this.outputModeBtn.classList.remove('output-brief', 'output-full');
        this.outputModeBtn.classList.add(`output-${this.outputMode}`);
        this.outputModeBtn.textContent = this.outputMode === 'brief' ? '📝' : '📖';
        this.outputModeBtn.setAttribute('data-mode', this.outputMode === 'brief' ? 'Brief' : 'Full');
    },

    // === SETTINGS PERSISTENCE ===
    loadSettings() {
        try {
            // Riff mode persistence
            const savedRiffMode = localStorage.getItem('synaptic_riff_mode');
            if (savedRiffMode === 'true') {
                this.riffMode = true;
                this.micBtn?.classList.add('riff-mode');
                console.log('🎵 Riff mode restored from session');
            }

            // Custom interrupt keywords
            const savedKeywords = localStorage.getItem('synaptic_interrupt_keywords');
            if (savedKeywords) {
                this.interruptKeywords = JSON.parse(savedKeywords);
                console.log('⏸️ Custom interrupt keywords loaded:', this.interruptKeywords);
            }

            // Custom riff keywords
            const savedRiffKeywords = localStorage.getItem('synaptic_riff_keywords');
            if (savedRiffKeywords) {
                this.riffKeywords = JSON.parse(savedRiffKeywords);
            }
        } catch (e) {
            console.warn('Failed to load voice settings:', e);
        }
    },

    saveSettings() {
        try {
            localStorage.setItem('synaptic_riff_mode', this.riffMode.toString());
            localStorage.setItem('synaptic_interrupt_keywords', JSON.stringify(this.interruptKeywords));
            localStorage.setItem('synaptic_riff_keywords', JSON.stringify(this.riffKeywords));
        } catch (e) {
            console.warn('Failed to save voice settings:', e);
        }
    },

    // Settings API for user customization
    setInterruptKeywords(keywords) {
        if (Array.isArray(keywords) && keywords.length > 0) {
            this.interruptKeywords = keywords.map(k => k.toLowerCase().trim());
            this.saveSettings();
            console.log('⏸️ Interrupt keywords updated:', this.interruptKeywords);
            return true;
        }
        return false;
    },

    addInterruptKeyword(keyword) {
        const k = keyword.toLowerCase().trim();
        if (k && !this.interruptKeywords.includes(k)) {
            this.interruptKeywords.push(k);
            this.saveSettings();
            console.log('⏸️ Added interrupt keyword:', k);
            return true;
        }
        return false;
    },

    async checkEnrollmentStatus() {
        try {
            const res = await fetch(`/voice/enrollment-status?user_email=${encodeURIComponent(this.userEmail)}`);
            const data = await res.json();
            if (data.enrolled) {
                console.log('✅ Voice enrolled:', data.sample_count, 'samples');
                this.micBtn.title = 'Tap to speak (voice enrolled)';
                this.speakerEnrolled = true;
                // Store user_id for speaker verification
                if (data.user_id) {
                    this.userId = data.user_id;
                    localStorage.setItem('synaptic_user_id', data.user_id);
                }
                console.log('🎯 Speaker-aware VAD:', this.speakerAware ? 'ENABLED' : 'disabled');
            } else {
                console.log('⚠️ Voice not enrolled - speaker-aware VAD disabled');
                this.speakerEnrolled = false;
                this.micBtn.title = 'Tap to speak';
            }
        } catch (e) {
            console.log('Voice enrollment check failed:', e.message);
            this.speakerEnrolled = false;
        }
    },

    // === SPEAKER-AWARE VAD ===
    // Verifies if audio chunk contains enrolled speaker's voice
    // Only resets silence timer when Aaron's voice is confirmed
    async verifySpeakerChunk() {
        if (!this.speakerAware || !this.speakerEnrolled || !this.ws || this.pendingVerification) {
            return true;  // Permissive fallback
        }

        // Get recent audio chunks for verification (~1 second)
        if (this.audioChunks.length < 2) {
            return true;  // Not enough audio yet
        }

        try {
            this.pendingVerification = true;

            // Get last ~1 second of audio
            const recentChunks = this.audioChunks.slice(-10);  // ~1 second at 100ms chunks
            const audioBlob = new Blob(recentChunks, { type: 'audio/webm' });

            // Convert to WAV for verification
            const wavBlob = await this.convertToWav(audioBlob);
            const arrayBuffer = await wavBlob.arrayBuffer();

            // Send verification request
            this.ws.send(JSON.stringify({
                type: 'verify_speaker',
                user_id: this.userId
            }));
            this.ws.send(arrayBuffer);

            // Response handled in handleVoiceResponse
            // pendingVerification cleared when response arrives (not here)

        } catch (e) {
            console.log('[VAD] Speaker verification error:', e.message);
            this.pendingVerification = false;  // Only clear on error
        }

        return this.speakerVerified;
    },

    // === STATE MACHINE ===
    setState(newState) {
        const oldState = this.state;
        this.state = newState;

        // Update visual state
        this.micBtn.classList.remove('mic-idle', 'mic-listening', 'mic-thinking', 'mic-speaking');
        this.stateLabel?.classList.remove('visible', 'listening', 'thinking', 'speaking', 'riff');

        switch(newState) {
            case 'idle':
                this.micBtn.classList.add('mic-idle');
                this.micBtn.textContent = '🎤';
                if (this.conversationMode) {
                    this.micBtn.title = 'Voice mode ON - tap to exit';
                    this.showStateLabel('🟢 Voice mode ON - tap mic to exit', 'listening');
                } else {
                    this.micBtn.title = 'Tap to speak';
                }
                break;
            case 'listening':
                this.micBtn.classList.add('mic-listening');
                this.micBtn.textContent = '🎙️';
                if (this.conversationMode) {
                    this.micBtn.title = 'Voice mode ON - tap to exit';
                    this.showStateLabel('🟢 Listening... (4s silence → handoff)', 'listening');
                } else {
                    this.micBtn.title = 'Tap to stop listening';
                    this.showStateLabel('🟢 Listening...', 'listening');
                }
                break;
            case 'thinking':
                this.micBtn.classList.add('mic-thinking');
                this.micBtn.textContent = '💭';
                this.micBtn.title = 'Processing...';
                this.showStateLabel('🔵 Thinking...', 'thinking');
                break;
            case 'speaking':
                this.micBtn.classList.add('mic-speaking');
                this.micBtn.textContent = '🔊';
                this.micBtn.title = 'Say "wait/hold on/stop/pause" to interrupt';
                this.showStateLabel('🟣 Speaking... (say "wait/hold on/stop/pause" to interrupt)', 'speaking');
                break;
        }

        // Riff mode indicator
        if (this.riffMode) {
            this.micBtn.classList.add('riff-mode');
            if (this.stateLabel) {
                this.stateLabel.classList.add('riff');
            }
        }

        console.log(`[Voice] State: ${oldState} → ${newState}`);
    },

    showStateLabel(text, stateClass) {
        if (!this.stateLabel) return;
        this.stateLabel.textContent = text;
        this.stateLabel.className = 'voice-state-label visible ' + stateClass;
    },

    hideStateLabel() {
        if (this.stateLabel) {
            this.stateLabel.classList.remove('visible');
        }
    },

    // === MIC CLICK HANDLER ===
    // Conversation mode: one tap enters persistent voice mode, another tap exits
    handleMicClick() {
        // If conversation mode is active, any click exits it
        if (this.conversationMode) {
            this.exitConversationMode();
            return;
        }

        // Normal behavior when not in conversation mode
        switch(this.state) {
            case 'idle':
                // Enter conversation mode
                this.enterConversationMode();
                break;
            case 'listening':
                // Should not happen in conversation mode, but handle anyway
                this.stopListening();
                break;
            case 'thinking':
                // Can't interrupt thinking - wait for response
                this.showToast('Processing... please wait', 'info');
                break;
            case 'speaking':
                // Tap while speaking = intentional interrupt
                this.interruptSpeaking();
                break;
        }
    },

    // === CONVERSATION MODE (ChatGPT-style) ===
    enterConversationMode() {
        this.conversationMode = true;
        this.micBtn?.classList.add('conversation-mode');
        console.log('🎙️ Conversation mode: ON - speak freely, 4s silence hands off turn');
        this.showToast('Voice mode ON - speak freely', 'info');
        this.startListening();
    },

    exitConversationMode() {
        this.conversationMode = false;
        this.micBtn?.classList.remove('conversation-mode');
        console.log('🎙️ Conversation mode: OFF');
        this.showToast('Voice mode OFF', 'info');

        // Stop any active audio/recording
        if (this.currentAudioSource) {
            this.currentAudioSource.stop();
            this.currentAudioSource = null;
        }
        this.stopInterruptListener();
        this.reset();
    },

    // === LISTENING ===
    async startListening() {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000 }
            });

            // Setup audio analysis for VAD
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
            const source = this.audioContext.createMediaStreamSource(stream);
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 512;
            source.connect(this.analyser);

            // Setup recorder
            const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus' : 'audio/webm';
            this.mediaRecorder = new MediaRecorder(stream, { mimeType });
            this.audioChunks = [];

            this.mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0) this.audioChunks.push(e.data);
            };
            this.mediaRecorder.onstop = () => this.processRecording();

            // Connect WebSocket
            const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            this.ws = new WebSocket(`${wsProtocol}//${window.location.host}/voice`);

            this.ws.onopen = () => {
                console.log('🔊 Voice WebSocket connected');
                this.mediaRecorder.start(100);
                this.setState('listening');
                this.startVAD();
            };

            this.ws.onmessage = (e) => this.handleVoiceResponse(e);
            this.ws.onerror = (e) => { console.error('Voice WS error:', e); this.reset(); };
            this.ws.onclose = () => console.log('Voice WS closed');

        } catch (err) {
            console.error('Mic access denied:', err);
            this.showToast('Microphone access required', 'error');
        }
    },

    startVAD() {
        const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
        let lastSpeech = Date.now();
        let lastVerifyRequest = 0;
        const sessionStart = Date.now();

        // Reset speaker verification state for new recording
        this.speakerVerified = !this.speakerAware;  // If not speaker-aware, assume verified
        this.speakerLastVerified = 0;
        this.speakerConfirmedAt = 0;  // Gap 1: Reset confirmation timestamp
        this.warmupComplete = false;  // Gap 2: Reset warmup state
        this.verificationState = 'idle';  // Gap 3: Reset visual state

        // CONFIDENCE CACHING: Trust persists across recordings in same page session
        // - trustEstablished: NOT reset (earned trust stays)
        // - highConfidenceStreak: NOT reset (continue building)
        // - verificationStaleness: Preserved (stays at extended if trust earned)
        // This means once you've proven yourself, the system remembers

        // Show level indicator
        if (this.levelIndicator) {
            this.levelIndicator.classList.add('active');
        }

        let debugLogTimer = 0;  // Debug: periodic level logging
        let smoothedLevel = 0;  // Smoothed audio level (filters spikes)
        const smoothingFactor = 0.3;  // 0.3 = 30% new, 70% old (more smoothing)

        const checkVAD = () => {
            if (this.state !== 'listening') {
                this.hideLevelIndicator();
                this.updateVerificationIndicator('idle');  // Gap 3: Reset on exit
                return;
            }

            this.analyser.getByteFrequencyData(dataArray);
            const rawLevel = dataArray.reduce((a, b) => a + b, 0) / dataArray.length / 255;

            // Smooth the level to filter out brief spikes (exponential moving average)
            smoothedLevel = smoothedLevel * (1 - smoothingFactor) + rawLevel * smoothingFactor;
            const avg = smoothedLevel;  // Use smoothed level for VAD decisions

            // Update visual level indicator (use raw for responsiveness)
            this.updateLevelBars(rawLevel);

            const now = Date.now();

            // DEBUG: Log audio level every 1 second to diagnose VAD
            if (now - debugLogTimer > 1000) {
                debugLogTimer = now;
                const silenceGap = this.vadActive ? (now - lastSpeech) : 0;
                const warmupRemaining = Math.max(0, this.warmupGracePeriod - (now - sessionStart));
                const aboveThreshold = avg > this.silenceThreshold;
                console.log(`🎤 VAD | raw:${rawLevel.toFixed(3)} smooth:${avg.toFixed(3)} ${aboveThreshold ? '▲' : '▼'} thr:${this.silenceThreshold} | active:${this.vadActive} | gap:${silenceGap}ms/${this.silenceDuration}ms | warmup:${warmupRemaining > 0 ? warmupRemaining + 'ms' : 'done'}`);
            }

            if (avg > this.silenceThreshold) {
                // Audio detected - but is it the enrolled user's voice?

                if (this.speakerAware && this.speakerEnrolled) {
                    // Speaker-aware mode: verify speaker periodically
                    if (now - lastVerifyRequest > this.speakerVerifyInterval && !this.pendingVerification) {
                        lastVerifyRequest = now;
                        this.verificationState = 'verifying';  // Gap 3
                        this.updateVerificationIndicator('verifying');
                        this.verifySpeakerChunk();  // Async verification
                    }

                    // Gap 1: Check if verification is stale
                    const verificationFresh = this.speakerConfirmedAt > 0 &&
                        (now - this.speakerConfirmedAt) < this.verificationStaleness;

                    // Gap 2: During warmup grace period, be permissive
                    const inWarmup = !this.warmupComplete && (now - sessionStart) < this.warmupGracePeriod;

                    // Determine if we should trust current speaker status
                    const trustSpeaker = this.speakerVerified && (verificationFresh || inWarmup);

                    if (trustSpeaker) {
                        // Verified speaker (fresh confirmation) - reset silence timer
                        lastSpeech = now;
                        if (!this.vadActive) {
                            this.vadActive = true;
                            console.log('🗣️ Enrolled speaker confirmed');
                        }
                        this.verificationState = 'confirmed';  // Gap 3
                        this.updateVerificationIndicator('confirmed');
                    } else if (inWarmup) {
                        // Gap 2: During warmup, give benefit of doubt
                        lastSpeech = now;
                        if (!this.vadActive) {
                            this.vadActive = true;
                            console.log('🗣️ Speech detected (warmup - awaiting verification...)');
                        }
                        this.updateVerificationIndicator('verifying');
                    } else if (!this.speakerVerified && this.warmupComplete) {
                        // Gap 1 & 2: After warmup, verified=false means background noise
                        // Don't reset silence timer - let it timeout
                        if (!this.vadActive) {
                            this.vadActive = true;
                            lastSpeech = now;  // Start timer but won't extend
                            console.log('🗣️ Audio detected (not enrolled speaker)');
                        }
                        this.verificationState = 'rejected';  // Gap 3
                        this.updateVerificationIndicator('rejected');
                        console.log('🔇 Background noise - not resetting timer');
                    } else {
                        // Stale verification - request fresh one, be cautious
                        if (!this.vadActive) {
                            this.vadActive = true;
                            lastSpeech = now;
                            console.log('🗣️ Speech detected (re-verifying...)');
                        }
                        this.updateVerificationIndicator('verifying');
                    }
                } else {
                    // Simple mode: Only reset timer for CLEAR speech (2x threshold)
                    // This prevents ambient noise from blocking silence detection
                    const speechLevel = this.silenceThreshold * 2;  // 0.10 for 0.05 base

                    if (!this.vadActive) {
                        // First detection - activate VAD
                        lastSpeech = now;
                        this.vadActive = true;
                        console.log('🗣️ Speech detected (starting)');
                    } else if (avg > speechLevel) {
                        // Clear speech - reset timer
                        lastSpeech = now;
                    }
                    // If avg is between threshold (0.05) and speechLevel (0.10),
                    // we're in the "maybe speaking" zone - don't reset timer
                    // This allows natural pauses and filters out low-level noise
                    this.updateVerificationIndicator('confirmed');
                }
            } else if (this.vadActive && now - lastSpeech > this.silenceDuration) {
                // Silence detected after speech
                // Gap 2: Only enforce silence timeout after warmup OR if warmup explicitly failed
                if (!this.warmupComplete && (now - sessionStart) < this.warmupGracePeriod) {
                    // Still in warmup - don't timeout yet
                    return requestAnimationFrame(checkVAD);
                }

                if (this.speakerAware && this.speakerEnrolled) {
                    console.log('🔇 Enrolled speaker stopped - auto-stopping');
                } else {
                    console.log('🔇 Silence detected - auto-stopping');
                }
                this.vadActive = false;
                this.updateVerificationIndicator('idle');  // Gap 3
                this.stopListening();
                return;
            }

            requestAnimationFrame(checkVAD);
        };
        checkVAD();
    },

    // === VOICE LEVEL VISUALIZATION ===
    updateLevelBars(level) {
        if (!this.levelBars.length) return;

        // Map level (0-1) to bar heights
        // level thresholds: 0.02 (silence), 0.05 (low), 0.15 (med), 0.3 (high), 0.5+ (peak)
        this.levelBars.forEach((bar, i) => {
            bar.classList.remove('level-low', 'level-med', 'level-high', 'level-peak');

            const threshold = 0.02 + (i * 0.08);  // 0.02, 0.10, 0.18, 0.26, 0.34
            if (level > threshold) {
                if (level > 0.4) {
                    bar.classList.add('level-peak');
                } else if (level > 0.25) {
                    bar.classList.add('level-high');
                } else if (level > 0.12) {
                    bar.classList.add('level-med');
                } else {
                    bar.classList.add('level-low');
                }
            } else {
                // Reset to minimum
                bar.style.height = '4px';
                bar.style.background = '#22c55e';
            }
        });
    },

    hideLevelIndicator() {
        if (this.levelIndicator) {
            this.levelIndicator.classList.remove('active');
        }
        // Reset bars
        this.levelBars.forEach(bar => {
            bar.classList.remove('level-low', 'level-med', 'level-high', 'level-peak');
        });
    },

    // === GAP 3: SPEAKER VERIFICATION VISUAL INDICATOR ===
    // Shows green (confirmed), yellow (verifying), red (rejected), or hidden (idle)
    updateVerificationIndicator(state) {
        if (!this.speakerAware || !this.speakerEnrolled) {
            // Hide indicator if not using speaker-aware VAD
            const indicator = document.getElementById('speaker-verify-indicator');
            if (indicator) indicator.style.display = 'none';
            return;
        }

        let indicator = document.getElementById('speaker-verify-indicator');

        // Create indicator if it doesn't exist
        if (!indicator) {
            indicator = document.createElement('div');
            indicator.id = 'speaker-verify-indicator';
            indicator.className = 'speaker-verify-indicator';
            indicator.innerHTML = '<span class="verify-icon"></span><span class="verify-text"></span>';

            // Insert near mic button or level indicator
            const parent = this.levelIndicator?.parentElement || this.micBtn?.parentElement;
            if (parent) {
                parent.appendChild(indicator);
            } else {
                document.body.appendChild(indicator);
            }
        }

        const icon = indicator.querySelector('.verify-icon');
        const text = indicator.querySelector('.verify-text');

        // Update visual state
        indicator.className = 'speaker-verify-indicator';
        indicator.style.display = 'flex';

        switch(state) {
            case 'confirmed':
                // Show trusted state with star if trust has been established
                if (this.trustEstablished) {
                    indicator.classList.add('verify-trusted');
                    icon.textContent = '⭐';
                    text.textContent = 'Trusted';
                } else {
                    indicator.classList.add('verify-confirmed');
                    icon.textContent = '🟢';
                    text.textContent = `Voice confirmed (${this.highConfidenceStreak}/${this.streakForExtendedTrust})`;
                }
                break;
            case 'verifying':
                indicator.classList.add('verify-pending');
                icon.textContent = '🟡';
                text.textContent = 'Verifying...';
                break;
            case 'rejected':
                indicator.classList.add('verify-rejected');
                icon.textContent = '🔴';
                text.textContent = 'Background noise';
                break;
            case 'idle':
            default:
                indicator.style.display = 'none';
                break;
        }
    },

    stopListening() {
        console.log('🛑 stopListening() called - VAD triggered handoff');
        if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
            this.mediaRecorder.stop();
            this.mediaRecorder.stream.getTracks().forEach(t => t.stop());
        }
        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }
        this.setState('thinking');
    },

    async processRecording() {
        console.log(`📤 processRecording() called - ${this.audioChunks.length} chunks`);
        if (this.audioChunks.length === 0) {
            console.log('⚠️ No audio chunks - skipping');
            this.reset();
            return;
        }

        const audioBlob = new Blob(this.audioChunks, { type: 'audio/webm' });
        console.log(`📦 Audio blob size: ${audioBlob.size} bytes`);
        const wavBlob = await this.convertToWav(audioBlob);

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            const arrayBuffer = await wavBlob.arrayBuffer();
            this.ws.send(arrayBuffer);

            // Include riff mode and output mode in end_audio signal
            const endMsg = {
                type: 'end_audio',
                riff_mode: this.riffMode,
                dev_mode: this.outputMode === 'full'  // Brief=false, Full=true
            };
            console.log('📨 Sending end_audio:', endMsg);
            this.ws.send(JSON.stringify(endMsg));
        } else {
            console.error('WebSocket not connected');
            this.reset();
        }
    },

    // === RESPONSE HANDLING ===
    handleVoiceResponse(event) {
        try {
            const data = JSON.parse(event.data);

            switch (data.type) {
                case 'transcript':
                    console.log('📝 Transcript:', data.text);
                    this.addMessageToChat('aaron', data.text);

                    // Check for riff mode activation/deactivation
                    this.checkRiffModeKeywords(data.text);

                    // Check for interrupt keywords (while speaking)
                    if (this.state === 'speaking') {
                        this.checkInterruptKeywords(data.text);
                    }
                    break;

                case 'thinking':
                    this.setState('thinking');
                    break;

                case 'response':
                    console.log('💬 Response:', data.text?.substring(0, 50) + '...');
                    this.pendingResponse = data.text;  // Store for potential interrupt
                    this.addMessageToChat('synaptic', data.text);

                    if (data.audio) {
                        this.playSpeech(data.audio);
                    } else {
                        this.reset();
                    }
                    break;

                case 'speaker_verified':
                    // Clear pending flag now that response arrived (prevents duplicate requests)
                    this.pendingVerification = false;

                    // Handle both full verification (verified field) and live VAD (is_speaker field)
                    const isVerified = data.verified ?? data.is_speaker;
                    const similarity = data.similarity || 0;
                    const verifyTime = Date.now();

                    // Gap 2: Mark warmup complete after first verification response
                    if (!this.warmupComplete) {
                        this.warmupComplete = true;
                        console.log('🎯 Warmup complete - speaker verification active');
                    }

                    if (isVerified) {
                        if (data.reason === 'verified') {
                            // Live VAD verification - speaker confirmed
                            this.speakerVerified = true;
                            this.speakerLastVerified = verifyTime;
                            this.speakerConfirmedAt = verifyTime;  // Gap 1: Fresh confirmation
                            this.verificationState = 'confirmed';  // Gap 3
                            this.updateVerificationIndicator('confirmed');

                            // === CONFIDENCE CACHING: Track high-confidence streak ===
                            if (similarity >= this.highConfidenceThreshold) {
                                this.highConfidenceStreak++;
                                if (this.highConfidenceStreak >= this.streakForExtendedTrust && !this.trustEstablished) {
                                    this.trustEstablished = true;
                                    this.verificationStaleness = this.extendedStaleness;
                                    console.log(`🏆 Trust established! ${this.highConfidenceStreak} high-confidence verifications`);
                                    console.log(`   → Staleness window extended to ${this.extendedStaleness/1000}s`);
                                    this.showToast('Voice trust established ✓', 'success');
                                } else if (!this.trustEstablished) {
                                    console.log(`🎯 Speaker verified: ${(similarity * 100).toFixed(0)}% (streak: ${this.highConfidenceStreak}/${this.streakForExtendedTrust})`);
                                } else {
                                    console.log(`🎯 Speaker verified: ${(similarity * 100).toFixed(0)}% (trusted)`);
                                }
                            } else {
                                // Below high-confidence threshold - reset streak but still verified
                                this.highConfidenceStreak = 0;
                                console.log(`🎯 Speaker verified: ${(similarity * 100).toFixed(0)}% (streak reset)`);
                            }
                        } else if (data.reason) {
                            // Permissive fallback (no enrollment, unavailable, etc.)
                            this.speakerVerified = true;
                            this.speakerConfirmedAt = verifyTime;  // Gap 1
                            this.verificationState = 'confirmed';  // Gap 3
                            this.updateVerificationIndicator('confirmed');
                            console.log(`🎯 Speaker assumed (${data.reason})`);
                        } else {
                            // Full verification at end of recording
                            console.log('✅ Speaker verified:', similarity);
                            this.speakerVerified = true;
                            this.speakerConfirmedAt = verifyTime;  // Gap 1
                        }
                    } else {
                        if (data.reason === 'verified') {
                            // Live VAD - speaker NOT enrolled user (background noise)
                            this.speakerVerified = false;
                            this.speakerLastVerified = verifyTime;
                            // Gap 1: Don't update speakerConfirmedAt on rejection
                            this.verificationState = 'rejected';  // Gap 3
                            this.updateVerificationIndicator('rejected');

                            // === CONFIDENCE CACHING: Reset streak on rejection ===
                            if (this.highConfidenceStreak > 0) {
                                console.log(`🔇 Not enrolled speaker: ${(similarity * 100).toFixed(0)}% (streak reset from ${this.highConfidenceStreak})`);
                                this.highConfidenceStreak = 0;
                                // Don't reset trustEstablished - once earned, keep for session
                            } else {
                                console.log(`🔇 Not enrolled speaker: ${(similarity * 100).toFixed(0)}% similarity`);
                            }
                        } else {
                            // Full verification failed - reject recording
                            console.log('❌ Speaker not verified');
                            this.showToast('Voice not recognized', 'warning');
                            this.reset();
                            this.ws?.close();
                        }
                    }
                    break;

                case 'interrupt_detected':
                    // Server detected interrupt keyword in real-time audio
                    console.log('⏸️ Server detected interrupt:', data.keyword);
                    if (this.state === 'speaking') {
                        this.interruptSpeaking();
                        this.showToast(`Interrupted: "${data.keyword}"`, 'info');
                    }
                    break;

                case 'error':
                    console.error('Voice error:', data.message);
                    this.showToast(data.message || 'Voice error', 'error');
                    this.reset();
                    break;
            }
        } catch (e) {
            console.error('Failed to parse voice response:', e);
        }
    },

    // === RIFF MODE ===
    checkRiffModeKeywords(text) {
        const lower = text.toLowerCase();

        // Check exit keywords first
        if (this.riffMode) {
            for (const keyword of this.exitRiffKeywords) {
                if (lower.includes(keyword)) {
                    this.riffMode = false;
                    this.saveSettings();  // Persist change
                    this.showToast('Riff mode OFF - Normal responses', 'info');
                    this.micBtn.classList.remove('riff-mode');
                    console.log('🎵 Riff mode: OFF (saved)');
                    return;
                }
            }
        }

        // Check activation keywords
        for (const keyword of this.riffKeywords) {
            if (lower.includes(keyword)) {
                this.riffMode = true;
                this.saveSettings();  // Persist change
                this.showToast('Riff mode ON - Short, reflective responses', 'info');
                this.micBtn.classList.add('riff-mode');
                console.log('🎵 Riff mode: ON (saved)');
                return;
            }
        }
    },

    // === BARGE-IN (Keyword-only, per Logos spec) ===
    checkInterruptKeywords(text) {
        const lower = text.toLowerCase();
        for (const keyword of this.interruptKeywords) {
            if (lower.includes(keyword)) {
                console.log('⏸️ Interrupt keyword detected:', keyword);
                this.interruptSpeaking();
                return true;
            }
        }
        return false;
    },

    interruptSpeaking() {
        // Stop TTS playback but DON'T discard response (per Logos spec)
        if (this.currentAudioSource) {
            this.currentAudioSource.stop();
            this.currentAudioSource = null;
        }
        if (this.currentAudioCtx) {
            this.currentAudioCtx.close();
            this.currentAudioCtx = null;
        }

        // Stop interrupt listener
        this.stopInterruptListener();

        console.log('⏹️ TTS interrupted - response preserved');
        this.showToast('Interrupted - response saved', 'info');

        // Return to idle, ready for next input
        this.setState('idle');
        this.hideStateLabel();
    },

    // === REAL-TIME INTERRUPT DETECTION ===
    // Listens for interrupt keywords during speaking state for faster response
    async startInterruptListener() {
        if (!this.streamingEnabled) return;

        try {
            this.interruptStream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000 }
            });

            this.interruptContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
            const source = this.interruptContext.createMediaStreamSource(this.interruptStream);
            this.interruptAnalyser = this.interruptContext.createAnalyser();
            this.interruptAnalyser.fftSize = 512;
            source.connect(this.interruptAnalyser);

            // Create a simple recorder for 500ms chunks
            const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus' : 'audio/webm';
            const recorder = new MediaRecorder(this.interruptStream, { mimeType });
            let chunks = [];

            recorder.ondataavailable = async (e) => {
                if (e.data.size > 0 && this.state === 'speaking') {
                    chunks.push(e.data);
                    // Send chunk for quick keyword detection
                    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                        const blob = new Blob(chunks, { type: 'audio/webm' });
                        const wavBlob = await this.convertToWav(blob);
                        const arrayBuffer = await wavBlob.arrayBuffer();

                        // Send for interrupt detection only
                        this.ws.send(JSON.stringify({
                            type: 'interrupt_check',
                            keywords: this.interruptKeywords
                        }));
                        this.ws.send(arrayBuffer);

                        chunks = [];  // Reset for next chunk
                    }
                }
            };

            // Record in 500ms chunks for faster interrupt detection
            recorder.start(500);
            this.interruptRecorder = recorder;

            console.log('🔊 Interrupt listener active (500ms chunks)');

        } catch (e) {
            console.warn('Could not start interrupt listener:', e);
        }
    },

    stopInterruptListener() {
        if (this.interruptRecorder) {
            try { this.interruptRecorder.stop(); } catch(e) {}
            this.interruptRecorder = null;
        }
        if (this.interruptStream) {
            this.interruptStream.getTracks().forEach(t => t.stop());
            this.interruptStream = null;
        }
        if (this.interruptContext) {
            this.interruptContext.close();
            this.interruptContext = null;
        }
        this.interruptAnalyser = null;
    },

    // === TTS PLAYBACK ===
    async playSpeech(base64Audio) {
        this.setState('speaking');

        // Start real-time interrupt listener for faster keyword detection
        this.startInterruptListener();

        try {
            const audioData = atob(base64Audio);
            const arrayBuffer = new ArrayBuffer(audioData.length);
            const view = new Uint8Array(arrayBuffer);
            for (let i = 0; i < audioData.length; i++) {
                view[i] = audioData.charCodeAt(i);
            }

            this.currentAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
            const audioBuffer = await this.currentAudioCtx.decodeAudioData(arrayBuffer);

            this.currentAudioSource = this.currentAudioCtx.createBufferSource();
            this.currentAudioSource.buffer = audioBuffer;
            this.currentAudioSource.connect(this.currentAudioCtx.destination);

            this.currentAudioSource.onended = () => {
                console.log('🔊 TTS playback complete');
                this.currentAudioSource = null;
                this.currentAudioCtx?.close();
                this.currentAudioCtx = null;
                this.pendingResponse = null;
                this.stopInterruptListener();

                // Conversation mode: auto-restart listening for next turn
                if (this.conversationMode) {
                    console.log('🎙️ Conversation mode: auto-reopening mic');
                    this.setState('idle');
                    this.hideStateLabel();
                    // Small delay to prevent audio feedback
                    setTimeout(() => {
                        if (this.conversationMode) {
                            this.startListening();
                        }
                    }, 300);
                } else {
                    this.reset();
                }
            };

            this.currentAudioSource.start(0);

        } catch (e) {
            console.error('Audio playback failed:', e);
            this.reset();
        }
    },

    // === RESET ===
    reset() {
        this.setState('idle');
        this.hideStateLabel();
        this.hideLevelIndicator();
        this.vadActive = false;

        // Stop interrupt listener if active
        this.stopInterruptListener();

        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    },

    // === UTILITIES ===
    addMessageToChat(sender, text) {
        // Add to main chat using existing system
        hideWelcomeScreen();
        addMsg({
            sender: sender === 'aaron' ? 'aaron' : 'synaptic',
            message: text,
            timestamp: new Date().toISOString(),
            memory_sources: []
        }, true);
    },

    async convertToWav(webmBlob) {
        const arrayBuffer = await webmBlob.arrayBuffer();
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();

        try {
            const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);
            const wavBuffer = this.audioBufferToWav(audioBuffer);
            return new Blob([wavBuffer], { type: 'audio/wav' });
        } catch (e) {
            console.error('Audio decode failed, sending raw:', e);
            return webmBlob;
        } finally {
            audioCtx.close();
        }
    },

    audioBufferToWav(buffer) {
        const numChannels = 1;
        const sampleRate = buffer.sampleRate;
        const format = 1;
        const bitDepth = 16;

        const data = buffer.getChannelData(0);
        const dataLength = data.length * (bitDepth / 8);
        const totalLength = 44 + dataLength;

        const arrayBuffer = new ArrayBuffer(totalLength);
        const view = new DataView(arrayBuffer);

        const writeString = (offset, str) => {
            for (let i = 0; i < str.length; i++) {
                view.setUint8(offset + i, str.charCodeAt(i));
            }
        };

        writeString(0, 'RIFF');
        view.setUint32(4, totalLength - 8, true);
        writeString(8, 'WAVE');
        writeString(12, 'fmt ');
        view.setUint32(16, 16, true);
        view.setUint16(20, format, true);
        view.setUint16(22, numChannels, true);
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * numChannels * (bitDepth / 8), true);
        view.setUint16(32, numChannels * (bitDepth / 8), true);
        view.setUint16(34, bitDepth, true);
        writeString(36, 'data');
        view.setUint32(40, dataLength, true);

        let offset = 44;
        for (let i = 0; i < data.length; i++) {
            const sample = Math.max(-1, Math.min(1, data[i]));
            view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7FFF, true);
            offset += 2;
        }

        return arrayBuffer;
    },

    showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.textContent = message;
        toast.style.cssText = `
            position: fixed;
            bottom: 100px;
            left: 50%;
            transform: translateX(-50%);
            padding: 12px 24px;
            background: ${type === 'error' ? '#ef4444' : type === 'warning' ? '#f59e0b' : type === 'info' ? '#3b82f6' : 'var(--accent)'};
            color: white;
            border-radius: 8px;
            z-index: 10000;
            animation: fadeIn 0.3s ease;
            font-size: 14px;
        `;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 3000);
    }
};

// Initialize voice chat when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => VoiceChat.init());
} else {
    VoiceChat.init();
}
</script></body></html>"""

@app.get("/")
async def get_page():
    # Serve unified interface with persistent navigation (Dashboard/Synaptic/Live)
    return HTMLResponse(
        content=UNIFIED_SHELL_HTML,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@app.get("/chat")
async def get_chat_only():
    # Legacy endpoint: chat-only view (for backward compatibility)
    return HTMLResponse(
        content=CHAT_HTML,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

# =============================================================================
# CHAT PROMPT PREPARATION (extracted for streaming WebSocket)
# =============================================================================

def _prepare_chat_prompts(prompt: str) -> tuple:
    """
    Prepare system prompt, user prompt, sources, and profile params for chat.
    
    This extracts the context-gathering logic from generate_with_local_llm
    so the WebSocket handler can stream tokens directly.
    
    Returns:
        (system_prompt, user_prompt, sources_queried, profile_params)
    """
    sources_queried = []
    
    # Get context (same logic as generate_with_local_llm)
    if USE_FULL_INJECTION:
        try:
            from memory.unified_injection import get_injection, InjectionPreset
            preset_map = {
                "full": InjectionPreset.FULL,
                "chat": InjectionPreset.CHAT,
                "phone": InjectionPreset.PHONE,
                "minimal": InjectionPreset.MINIMAL,
            }
            preset = preset_map.get(INJECTION_PRESET, InjectionPreset.CHAT)
            injection_result = get_injection(
                prompt=prompt,
                preset=preset,
                session_id=f"synaptic-chat-{datetime.now().strftime('%H%M%S')}",
                use_boundary_intelligence=False,  # Chat: skip slow BI LLM call
            )
            real_context = injection_result.payload
            sources_queried = [f"Context DNA ({preset.value})"]
        except Exception as e:
            print(f"[Prepare] Injection failed, using lightweight: {e}")
            real_context, sources_queried = get_real_synaptic_context(prompt)
    else:
        real_context, sources_queried = get_real_synaptic_context(prompt)
    
    # Build system prompt
    system_prompt = f"""You are Synaptic, having a real-time chat with Aaron.

This is a conversation, not a document. Be yourself - direct, intuitive, natural.
Respond as humans do: concise when appropriate, comprehensive when needed. Read the situation.
If Aaron wants depth, go deep. If he wants quick answers, stay tight. Efficient communication matters.

{real_context if real_context else ""}"""
    
    # Build user prompt with conversation context
    conversation = get_conversation_context(5)
    user_prompt = f"""{conversation}

Aaron: {prompt}"""
    
    # Auto-profile
    profile = get_auto_profile(prompt)
    params = get_generation_params(profile)
    
    return (system_prompt, user_prompt, sources_queried, params)


# =============================================================================
# WEBSOCKET ENDPOINT
# =============================================================================

@app.websocket("/chat")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    active_connections.add(ws)
    try:
        await ws.send_json({"type":"history","messages":[{"timestamp":t,"sender":s,"message":m}for t,s,m in get_recent_messages()]})
        while True:
            data = await ws.receive_json()
            msg = data.get("message","").strip()
            if msg:
                ts = datetime.now().isoformat()
                save_message("aaron", msg)
                # Mirror to DialogueMirror for failure analysis (learning loop)
                mirror_aaron_message("web_chat", msg, source="webapp", project="synaptic_chat")

                # Broadcast Aaron's message
                for c in active_connections:
                    await c.send_json({"type":"message","timestamp":ts,"sender":"aaron","message":msg})

                # Show thinking indicator
                for c in active_connections:
                    await c.send_json({"type":"thinking"})

                # --- STREAMING CHAT-FIRST ARCHITECTURE ---
                # Step 1: Build prompts (fast ~3ms, context gathering)
                loop = asyncio.get_event_loop()
                system_prompt, user_prompt, sources, profile_params = await loop.run_in_executor(
                    executor, _prepare_chat_prompts, msg
                )

                # Step 2: Stream response tokens to user (GPU gets 100%)
                full_response = ""
                try:
                    from memory.chat_then_enrich import stream_chat_response, fire_background_enrichment, is_simple_question as is_simple_q
                    
                    async for token in stream_chat_response(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        max_tokens=profile_params.get("max_tokens", 512),
                        temperature=profile_params.get("temperature", 0.7),
                    ):
                        full_response += token
                        # Stream each token to all connected clients
                        for c in active_connections:
                            try:
                                await c.send_json({"type": "stream_token", "text": token})
                            except Exception:
                                pass
                    
                except Exception as e:
                    # Fallback to non-streaming if streaming fails
                    print(f"[WebSocket] Streaming failed, falling back to sync: {e}")
                    response_result, _ = await loop.run_in_executor(
                        executor, synaptic_respond, msg
                    )
                    full_response = response_result or "[Error generating response]"

                # Step 3: Send final complete message (for clients that don't support streaming)
                rts = datetime.now().isoformat()
                if full_response:
                    save_message("synaptic", full_response)
                    mirror_synaptic_message("web_chat", full_response, source="webapp")

                for c in active_connections:
                    await c.send_json({
                        "type": "message",
                        "timestamp": rts,
                        "sender": "synaptic",
                        "message": full_response,
                        "memory_sources": sources,
                        "streamed": True
                    })

                # Step 4: Fire background enrichment AFTER user has response (non-blocking)
                try:
                    if not is_simple_q(msg):
                        fire_background_enrichment(msg)
                except Exception:
                    pass

    except WebSocketDisconnect:
        active_connections.discard(ws)
    except Exception as e:
        # Catch ALL other exceptions to prevent silent crashes and memory leaks
        print(f"[WebSocket Error] {type(e).__name__}: {e}")
        active_connections.discard(ws)  # Always cleanup


# =============================================================================
# PROGRESS STREAMING (Logos Priority 3)
# =============================================================================
# broadcast_progress and broadcast_terminal are imported from progress_broadcaster
# at the top of this file. No local definitions needed.

@app.websocket("/progress/stream/{task_id}")
async def progress_stream(ws: WebSocket, task_id: str):
    """
    WebSocket endpoint for streaming task progress to phone/clients.

    Logos Priority 3: Real-time progress visibility.

    Protocol:
    1. Client connects to /progress/stream/T-ABC123
    2. Server sends current progress state immediately
    3. Server pushes progress events as they occur:
       - {"type": "progress", "event_type": "stage", "stage": "implementing", ...}
       - {"type": "progress", "event_type": "heartbeat", ...}
       - {"type": "progress", "event_type": "percentage", "percentage": 75, ...}
    4. Server sends terminal event when task completes:
       - {"type": "terminal", "outcome": "completed", "auto_review": {...}}
    5. Client can send {"type": "ping"} for keepalive

    Example client usage:
        const ws = new WebSocket('ws://localhost:8888/progress/stream/T-ABC123');
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === 'progress') updateProgressUI(data);
            if (data.type === 'terminal') showCompletionUI(data);
        };
    """
    await ws.accept()

    # Register subscriber using broadcaster module
    _subscribe(task_id, ws)

    try:
        # Send current task state immediately
        from memory.task_persistence import get_task_store

        store = get_task_store()
        task = store.get_task(task_id)

        if task:
            # Send task info
            await ws.send_json({
                "type": "connected",
                "task_id": task_id,
                "task_status": task["status"],
                "task_intent": task["intent"][:100]
            })

            # Send recent progress events
            events = store.get_progress_events(task_id, limit=10)
            if events:
                await ws.send_json({
                    "type": "history",
                    "events": events
                })
        else:
            await ws.send_json({
                "type": "error",
                "message": f"Task not found: {task_id}"
            })

        # Keep connection alive, handle client messages
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[Progress Stream Error] {type(e).__name__}: {e}")
    finally:
        # Cleanup subscriber using broadcaster module
        _unsubscribe(task_id, ws)


# =============================================================================
# VOICE WEBSOCKET ENDPOINT
# =============================================================================

@app.websocket("/voice")
async def voice_endpoint(ws: WebSocket, session_token: str = None):
    """Voice WebSocket endpoint for phone browser voice chat.

    Security:
    - Requires valid EC2-issued session_token (via query param)
    - Token is Ed25519-signed, validated locally without network call
    - In dev mode (no public key configured), allows connections without token

    Protocol:
    1. Client sends audio chunks (WAV or raw PCM) as binary
    2. Client sends JSON {"type": "end_audio", "context": "..."} to signal end of recording
       - context is OPTIONAL: Pre-fetched from /api/phone-inject for Claude Code parity
    3. Server responds with JSON {"type": "transcript", "text": "..."} after STT
    4. Server responds with JSON {"type": "thinking"} while LLM processes
    5. Server responds with JSON {"type": "response", "text": "...", "audio": "<base64>"} with TTS audio

    Audio Format Expected:
    - WAV: Complete WAV file as binary
    - PCM: Raw 16-bit, mono, 16kHz PCM data

    Client can send:
    - Binary: Audio data (accumulated until end_audio)
    - JSON {"type": "end_audio", "context": "..."}: Process audio with optional pre-fetched context
    - JSON {"type": "ping"}: Keepalive, server responds with {"type": "pong"}
    - JSON {"type": "config", "voice": "..."}: Set TTS voice

    Context Injection (P0 - Claude Code Parity for Phone):
    - Phone browser can pre-fetch context via POST /api/phone-inject
    - Pass that context in the end_audio message
    - If context provided, it takes precedence over local injection
    - This gives phone the SAME 9-section payload as Claude Code
    """
    # Validate session token (EC2-issued, Ed25519-signed)
    # Use validate_session_full to get user_email AND device_token for identity tracking
    verified_user_email = None
    verified_device_token = None
    auth_mode = "dev_mode"
    auth_identity = None  # [user_email:device_token] for task source tracking
    subscribed_tasks = set()  # Track tasks this voice connection is subscribed to

    if VOICE_SESSION_VALIDATOR_AVAILABLE and validate_session_full:
        session = validate_session_full(session_token)
        if not session.is_valid:
            # Reject unauthorized connection
            await ws.close(code=1008, reason=f"Unauthorized: {session.error}")
            print(f"[Voice] Rejected connection: {session.error}")
            return

        verified_user_email = session.user_email
        verified_device_token = session.device_token
        auth_mode = session.mode
        auth_identity = session.identity  # [user_email:device_token]

    elif VOICE_SESSION_VALIDATOR_AVAILABLE and validate_or_allow_dev:
        # Fallback to old validation if validate_session_full not available
        is_valid, verified_user_email, auth_mode = validate_or_allow_dev(session_token)
        if not is_valid:
            await ws.close(code=1008, reason=f"Unauthorized: {auth_mode}")
            print(f"[Voice] Rejected connection: {auth_mode}")
            return
        auth_identity = f"[{verified_user_email}:unknown_device]"

    await ws.accept()
    voice_connections.add(ws)
    print(f"[Voice] Client connected (identity={auth_identity}, mode={auth_mode}). Total: {len(voice_connections)}")

    # Per-connection state
    audio_buffer = bytearray()
    tts_voice = TTS_VOICE
    temp_audio_file = None
    dev_mode = False  # FormatterAgent projection mode: False=VOICE, True=DEV

    try:
        # Send ready signal with auth info
        await ws.send_json({
            "type": "ready",
            "stt_model": WHISPER_MODEL,
            "tts_voice": tts_voice,
            "tts_available": True,  # Lazy-loaded on first use; assume available
            "auth_mode": auth_mode,
            "user_email": verified_user_email or "dev@contextdna.io",
            "auth_identity": auth_identity,
        })

        while True:
            # Receive data (can be binary audio or JSON control message)
            message = await ws.receive()

            if "bytes" in message:
                # Binary audio data - accumulate
                audio_buffer.extend(message["bytes"])
                # Send acknowledgment for large chunks
                if len(audio_buffer) % 32000 == 0:  # Every ~1 second at 16kHz
                    await ws.send_json({"type": "audio_received", "bytes": len(audio_buffer)})

            elif "text" in message:
                try:
                    data = json.loads(message["text"])
                    msg_type = data.get("type", "")

                    if msg_type == "ping":
                        await ws.send_json({"type": "pong"})

                    elif msg_type == "config":
                        # Update TTS voice
                        if "voice" in data:
                            tts_voice = data["voice"]
                            await ws.send_json({"type": "config_updated", "voice": tts_voice})

                    elif msg_type == "set_dev_mode":
                        # Toggle dev mode for FormatterAgent projection
                        # DEV mode: Full visual output + brief voice narrator
                        # VOICE mode: Terse spoken-friendly output only
                        if "enabled" in data:
                            dev_mode = data["enabled"]
                            await ws.send_json({
                                "type": "dev_mode_updated",
                                "enabled": dev_mode,
                                "projection": "DEV" if dev_mode else "VOICE"
                            })
                            print(f"[Voice] Dev mode {'enabled' if dev_mode else 'disabled'} (projection={'DEV' if dev_mode else 'VOICE'})")

                    elif msg_type == "interrupt_check":
                        # Real-time interrupt detection: quick STT on small audio chunk
                        # Next binary message contains the audio chunk
                        interrupt_keywords = data.get("keywords", ["wait", "hold on", "stop", "pause"])
                        interrupt_audio = await ws.receive()

                        if "bytes" in interrupt_audio and len(interrupt_audio["bytes"]) > 500:
                            try:
                                # Quick STT on the interrupt audio chunk
                                audio_bytes = interrupt_audio["bytes"]

                                # Save to temp WAV
                                if audio_bytes[:4] == b'RIFF':
                                    temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                                    temp_file.write(audio_bytes)
                                    temp_file.close()
                                    temp_path = temp_file.name
                                else:
                                    temp_path = save_audio_to_wav(audio_bytes, VOICE_SAMPLE_RATE)

                                if temp_path:
                                    # Fast transcription for interrupt detection (uses subprocess)
                                    loop = asyncio.get_event_loop()
                                    transcript = await loop.run_in_executor(
                                        executor,
                                        lambda: _transcribe_audio_mlx_whisper(temp_path)
                                    )
                                    transcript = transcript.strip().lower()

                                    # Check for mode switching keywords first (higher priority)
                                    mode_switch_detected = False
                                    if any(kw in transcript for kw in ["voice mode", "switch to voice", "enable voice mode"]):
                                        dev_mode = False
                                        mode_switch_detected = True
                                        print(f"[Voice] 🎙️ MODE SWITCH: Voice mode enabled via voice command")
                                        await ws.send_json({
                                            "type": "dev_mode_updated",
                                            "enabled": False,
                                            "projection": "VOICE",
                                            "trigger": "voice_command",
                                            "transcript": transcript
                                        })
                                    elif any(kw in transcript for kw in ["dev mode", "developer mode", "switch to dev", "enable dev mode"]):
                                        dev_mode = True
                                        mode_switch_detected = True
                                        print(f"[Voice] 💻 MODE SWITCH: Dev mode enabled via voice command")
                                        await ws.send_json({
                                            "type": "dev_mode_updated",
                                            "enabled": True,
                                            "projection": "DEV",
                                            "trigger": "voice_command",
                                            "transcript": transcript
                                        })

                                    # Check for interrupt keywords (if no mode switch)
                                    if not mode_switch_detected:
                                        for keyword in interrupt_keywords:
                                            if keyword.lower() in transcript:
                                                print(f"[Voice] ⏸️ INTERRUPT DETECTED: '{keyword}' in '{transcript}'")
                                                await ws.send_json({
                                                    "type": "interrupt_detected",
                                                    "keyword": keyword,
                                                    "transcript": transcript
                                                })
                                                break
                                    else:
                                        # No interrupt keyword found
                                        if transcript and not transcript.startswith('['):  # Filter STT errors
                                            print(f"[Voice] Interrupt check: no keyword in '{transcript[:50]}'")

                                    # Cleanup temp file
                                    try: os.unlink(temp_path)
                                    except Exception as e: print(f"[WARN] Temp file cleanup failed: {e}")

                            except Exception as e:
                                print(f"[Voice] Interrupt check failed: {e}")

                    elif msg_type == "verify_speaker":
                        # Speaker-aware VAD: Verify if audio chunk contains enrolled speaker
                        # Used for silence detection - only count silence when Aaron stops speaking
                        # Not when background noise reduces
                        try:
                            # Next binary message contains the audio chunk (~1 second)
                            verify_audio = await asyncio.wait_for(ws.receive(), timeout=2.0)

                            if "bytes" not in verify_audio or len(verify_audio["bytes"]) < 500:
                                await ws.send_json({
                                    "type": "speaker_verified",
                                    "is_speaker": False,
                                    "similarity": 0.0,
                                    "reason": "audio_too_short"
                                })
                                continue

                            audio_bytes = verify_audio["bytes"]
                            user_id = data.get("user_id") or verified_device_token

                            if not user_id:
                                # No enrollment available - fall back to simple audio level detection
                                await ws.send_json({
                                    "type": "speaker_verified",
                                    "is_speaker": True,  # Permissive fallback
                                    "similarity": 1.0,
                                    "reason": "no_enrollment"
                                })
                                continue

                            if not VOICE_AUTH_AVAILABLE or not get_voice_auth_manager:
                                await ws.send_json({
                                    "type": "speaker_verified",
                                    "is_speaker": True,
                                    "similarity": 1.0,
                                    "reason": "voice_auth_unavailable"
                                })
                                continue

                            auth = get_voice_auth_manager()

                            # Check if user is enrolled
                            if not auth.get_enrollment_status(user_id):
                                await ws.send_json({
                                    "type": "speaker_verified",
                                    "is_speaker": True,
                                    "similarity": 1.0,
                                    "reason": "not_enrolled"
                                })
                                continue

                            # Convert audio to WAV if needed for verification
                            if audio_bytes[:4] == b'RIFF':
                                wav_bytes = audio_bytes
                            else:
                                # Raw PCM - wrap in WAV header
                                import struct
                                sample_rate = VOICE_SAMPLE_RATE
                                bits_per_sample = 16
                                channels = 1
                                data_size = len(audio_bytes)
                                wav_header = struct.pack(
                                    '<4sI4s4sIHHIIHH4sI',
                                    b'RIFF', data_size + 36, b'WAVE',
                                    b'fmt ', 16, 1, channels, sample_rate,
                                    sample_rate * channels * bits_per_sample // 8,
                                    channels * bits_per_sample // 8, bits_per_sample,
                                    b'data', data_size
                                )
                                wav_bytes = wav_header + audio_bytes

                            # Quick speaker verification (using lowered threshold for live detection)
                            # Use 0.55 threshold instead of 0.70 for faster/noisier chunks
                            is_match, similarity = auth.verify_voice(
                                user_id,
                                wav_bytes,
                                threshold=0.55  # Lower threshold for live VAD chunks
                            )

                            await ws.send_json({
                                "type": "speaker_verified",
                                "is_speaker": is_match,
                                "similarity": round(similarity, 3),
                                "reason": "verified"
                            })

                        except asyncio.TimeoutError:
                            await ws.send_json({
                                "type": "speaker_verified",
                                "is_speaker": False,
                                "similarity": 0.0,
                                "reason": "timeout"
                            })
                        except Exception as e:
                            print(f"[Voice] Speaker verification failed: {e}")
                            await ws.send_json({
                                "type": "speaker_verified",
                                "is_speaker": True,  # Permissive on error
                                "similarity": 1.0,
                                "reason": f"error: {str(e)}"
                            })

                    elif msg_type == "end_audio":
                        # Process accumulated audio
                        if len(audio_buffer) < 1000:
                            await ws.send_json({"type": "error", "message": "Audio too short"})
                            audio_buffer.clear()
                            continue

                        # P0.1: Extract pre-fetched context from phone bridge
                        # This gives phone Claude Code parity via /api/phone-inject
                        external_context = data.get("context")
                        if external_context:
                            print(f"[Voice] Received external context from phone ({len(external_context)} chars)")
                            await ws.send_json({
                                "type": "context_received",
                                "context_length": len(external_context),
                                "source": "phone_bridge"
                            })
                        else:
                            print("[Voice] No external context provided - using internal injection")

                        # Output mode: Brief (dev_mode=false) vs Full (dev_mode=true)
                        # Brief: 200 words, spoken-optimized (FormatterAgent VOICE projection)
                        # Full: detailed + voice narrator (FormatterAgent DEV projection)
                        dev_mode = data.get("dev_mode", False)
                        print(f"[Voice] Output mode: {'Full (detailed)' if dev_mode else 'Brief (spoken)'}")

                        # Riff mode: Short, reflective, developmental responses (Logos spec)
                        riff_mode = data.get("riff_mode", False)
                        if riff_mode:
                            print("[Voice] 🎵 RIFF MODE active - constraining response")
                            await ws.send_json({
                                "type": "mode",
                                "mode": "rif",
                                "constraints": {"max_sentences": 3, "reflect_first": True}
                            })

                        # Speaker verification (if enrolled and enabled)
                        user_email = verified_user_email or data.get("user_email") or "dev@contextdna.io"
                        if VOICE_AUTH_AVAILABLE and get_voice_auth_manager:
                            auth = get_voice_auth_manager()
                            enrollment_info = auth.get_enrollment_info(user_email)

                            if enrollment_info:
                                # User has enrolled voiceprint - verify speaker
                                await ws.send_json({"type": "processing", "stage": "speaker_verify"})

                                # Verify against enrolled voiceprint
                                is_match, similarity = auth.verify(bytes(audio_buffer), user_email)

                                await ws.send_json({
                                    "type": "speaker_verified",
                                    "verified": is_match,
                                    "similarity": round(similarity, 3),
                                    "threshold": 0.70
                                })

                                if not is_match:
                                    # Speaker doesn't match - reject
                                    print(f"[Voice] Speaker verification failed: {similarity:.2%} < 70%")
                                    await ws.send_json({
                                        "type": "error",
                                        "message": f"Voice not recognized (similarity: {similarity:.0%})"
                                    })
                                    audio_buffer.clear()
                                    continue

                                print(f"[Voice] Speaker verified: {similarity:.2%} match")
                            else:
                                # No enrollment - skip verification, allow through
                                print(f"[Voice] No voiceprint enrolled for {user_email} - skipping verification")

                        await ws.send_json({"type": "processing", "stage": "stt"})

                        # Determine audio format and save to temp file
                        audio_bytes = bytes(audio_buffer)
                        audio_buffer.clear()

                        # Check if it's a WAV file (starts with RIFF)
                        if audio_bytes[:4] == b'RIFF':
                            # It's a complete WAV file - save directly
                            temp_audio_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                            temp_audio_file.write(audio_bytes)
                            temp_audio_file.close()
                            temp_audio_path = temp_audio_file.name
                        else:
                            # Assume raw PCM - convert to WAV
                            temp_audio_path = save_audio_to_wav(audio_bytes, VOICE_SAMPLE_RATE)
                            if not temp_audio_path:
                                await ws.send_json({"type": "error", "message": "Failed to process audio"})
                                continue

                        # STT: Transcribe audio using mlx-whisper
                        loop = asyncio.get_event_loop()
                        transcript = await loop.run_in_executor(
                            executor,
                            _transcribe_audio_mlx_whisper,
                            temp_audio_path
                        )

                        # Cleanup temp file
                        if temp_audio_path and os.path.exists(temp_audio_path):
                            os.unlink(temp_audio_path)
                            temp_audio_path = None

                        if transcript.startswith("[STT"):
                            await ws.send_json({"type": "error", "message": transcript})
                            continue

                        if not transcript.strip():
                            await ws.send_json({"type": "error", "message": "No speech detected"})
                            continue

                        # Send transcript
                        await ws.send_json({"type": "transcript", "text": transcript})

                        # Check for mode switching keywords in the full transcript
                        transcript_lower = transcript.lower()
                        if any(kw in transcript_lower for kw in ["voice mode", "switch to voice", "enable voice mode"]):
                            dev_mode = False
                            print(f"[Voice] 🎙️ MODE SWITCH: Voice mode enabled via transcript")
                            await ws.send_json({
                                "type": "dev_mode_updated",
                                "enabled": False,
                                "projection": "VOICE",
                                "trigger": "voice_command",
                                "transcript": transcript
                            })
                        elif any(kw in transcript_lower for kw in ["dev mode", "developer mode", "switch to dev", "enable dev mode"]):
                            dev_mode = True
                            print(f"[Voice] 💻 MODE SWITCH: Dev mode enabled via transcript")
                            await ws.send_json({
                                "type": "dev_mode_updated",
                                "enabled": True,
                                "projection": "DEV",
                                "trigger": "voice_command",
                                "transcript": transcript
                            })

                        # Save Aaron's voice message to DB
                        save_message("aaron", f"[voice] {transcript}")
                        # Mirror to DialogueMirror for failure analysis (learning loop)
                        session_id = verified_user_email or "voice_anonymous"
                        mirror_aaron_message(session_id, transcript, source="phone_voice", project="voice_chat")

                        # =============================================================
                        # INTENT ROUTING: Synaptic reasons about what Aaron wants
                        # =============================================================
                        # Instead of just responding conversationally, Synaptic first
                        # classifies the intent to see if this is a command/task.
                        await ws.send_json({"type": "processing", "stage": "intent_classification"})

                        try:
                            from memory.voice_intent_router import (
                                route_voice_input,
                                generate_voice_response,
                                VoiceIntent
                            )

                            # Route through intent classification with authenticated identity
                            # auth_identity is [user_email:device_token] for task source tracking
                            route_result = await loop.run_in_executor(
                                executor,
                                lambda: route_voice_input(
                                    transcript,
                                    context=external_context,  # P0.1: Pass phone bridge context
                                    auth_identity=auth_identity
                                )
                            )

                            # Send intent classification to client
                            await ws.send_json({
                                "type": "intent_classified",
                                "intent": route_result.get("intent"),
                                "confidence": route_result.get("confidence"),
                                "action": route_result.get("action"),
                                "auth_identity": route_result.get("auth_identity")
                            })

                            # Generate response based on routing
                            if route_result.get("action") == "conversation":
                                # Normal conversation - use synaptic_respond
                                await ws.send_json({"type": "processing", "stage": "llm"})

                                # Apply riff mode constraints (Logos spec)
                                effective_prompt = transcript
                                if riff_mode:
                                    effective_prompt = f"""[RIFF MODE - Respond in exactly this format:
1. One sentence paraphrasing my thought
2. One sentence developing/extending the idea
3. No conclusions, no summaries, no action plans]

{transcript}"""

                                response_text, sources = await loop.run_in_executor(
                                    executor,
                                    lambda: synaptic_respond(effective_prompt, external_context=external_context)  # P0.1: Phone parity
                                )

                                # Post-process: Truncate to 3 sentences in riff mode
                                if riff_mode and response_text:
                                    sentences = response_text.replace('!', '.').replace('?', '.').split('.')
                                    sentences = [s.strip() for s in sentences if s.strip()]
                                    if len(sentences) > 3:
                                        response_text = '. '.join(sentences[:3]) + '.'
                                        print(f"[Voice] Riff mode: Truncated to 3 sentences")
                            else:
                                # Command was routed - generate voice response
                                response_text = generate_voice_response(route_result)
                                sources = [route_result.get("action", "command")]

                                # Include task details in response if created
                                if route_result.get("task_id"):
                                    created_task_id = route_result["task_id"]
                                    await ws.send_json({
                                        "type": "task_created",
                                        "task_id": created_task_id,
                                        "intent": route_result.get("intent_description", "")
                                    })

                                    # Subscribe voice WebSocket to progress events for this task
                                    # This enables real-time progress feedback during Atlas execution
                                    _subscribe(created_task_id, ws)
                                    subscribed_tasks.add(created_task_id)
                                    logger.info(f"Voice WebSocket subscribed to progress for task {created_task_id}")

                        except ImportError:
                            # Fallback if intent router not available
                            await ws.send_json({"type": "processing", "stage": "llm"})

                            # Apply riff mode constraints in fallback too
                            effective_prompt = transcript
                            if riff_mode:
                                effective_prompt = f"""[RIFF MODE - Short, reflective response only]
{transcript}"""

                            response_text, sources = await loop.run_in_executor(
                                executor,
                                lambda: synaptic_respond(effective_prompt, external_context=external_context)  # P0.1: Phone parity
                            )

                            # Post-process in fallback too
                            if riff_mode and response_text:
                                sentences = response_text.replace('!', '.').replace('?', '.').split('.')
                                sentences = [s.strip() for s in sentences if s.strip()]
                                if len(sentences) > 3:
                                    response_text = '. '.join(sentences[:3]) + '.'

                        # Save Synaptic's response to DB
                        save_message("synaptic", response_text)
                        # Mirror to DialogueMirror for failure analysis (learning loop)
                        mirror_synaptic_message(session_id, response_text, source="phone_voice")

                        # TTS: Convert response to speech using FormatterAgent projection
                        await ws.send_json({"type": "processing", "stage": "tts"})

                        # FormatterAgent: Dual-projection for voice vs dev mode
                        # Voice mode: sanitized, terse output for TTS
                        # Dev mode: full visual + brief narrator for TTS
                        try:
                            from memory.formatter_agent import FormatterAgent, DeliveryMode
                            formatter = FormatterAgent()

                            if dev_mode:
                                # Dev mode: Show full text, speak brief narrator
                                tts_text = formatter._sanitize_for_voice(response_text)
                                # Limit narrator to ~50 words
                                words = tts_text.split()
                                if len(words) > 50:
                                    tts_text = " ".join(words[:50])
                                    if not tts_text.endswith((".", "!", "?")):
                                        tts_text += "."
                                visual_text = response_text  # Full text for display
                            else:
                                # Voice mode: Sanitize for TTS, same for display
                                tts_text = formatter._sanitize_for_voice(response_text)
                                visual_text = tts_text
                        except ImportError:
                            # Fallback: no sanitization
                            tts_text = response_text
                            visual_text = response_text

                        # TTS handles availability internally via lazy loader
                        audio_response = await tts_synthesize(tts_text, tts_voice)

                        # Send response with audio
                        response_data = {
                            "type": "response",
                            "text": visual_text,
                            "memory_sources": sources,
                            "dev_mode": dev_mode,
                        }
                        if dev_mode:
                            # Include both full text and spoken summary for dev UI
                            response_data["full_text"] = response_text
                            response_data["voice_narrator"] = tts_text
                        if audio_response:
                            response_data["audio"] = base64.b64encode(audio_response).decode('utf-8')
                            response_data["audio_format"] = "mp3"

                        await ws.send_json(response_data)

                    elif msg_type == "text":
                        # Direct text input (bypass STT) - same intent routing as voice
                        text_input = data.get("text", "").strip()
                        if not text_input:
                            continue

                        save_message("aaron", text_input)
                        # Mirror to DialogueMirror for failure analysis (learning loop)
                        text_session_id = verified_user_email or "voice_text_anonymous"
                        mirror_aaron_message(text_session_id, text_input, source="phone_voice_text", project="voice_chat")

                        # Use same intent routing as voice
                        await ws.send_json({"type": "processing", "stage": "intent_classification"})

                        try:
                            from memory.voice_intent_router import (
                                route_voice_input,
                                generate_voice_response
                            )

                            # Route with authenticated identity for task source tracking
                            route_result = await loop.run_in_executor(
                                executor,
                                lambda: route_voice_input(
                                    text_input,
                                    context=None,
                                    auth_identity=auth_identity
                                )
                            )

                            await ws.send_json({
                                "type": "intent_classified",
                                "intent": route_result.get("intent"),
                                "confidence": route_result.get("confidence"),
                                "action": route_result.get("action"),
                                "auth_identity": route_result.get("auth_identity")
                            })

                            if route_result.get("action") == "conversation":
                                await ws.send_json({"type": "processing", "stage": "llm"})
                                response_text, sources = await loop.run_in_executor(
                                    executor,
                                    synaptic_respond,
                                    text_input
                                )
                            else:
                                response_text = generate_voice_response(route_result)
                                sources = [route_result.get("action", "command")]

                                if route_result.get("task_id"):
                                    await ws.send_json({
                                        "type": "task_created",
                                        "task_id": route_result["task_id"]
                                    })

                        except ImportError:
                            await ws.send_json({"type": "processing", "stage": "llm"})
                            response_text, sources = await loop.run_in_executor(
                                executor,
                                synaptic_respond,
                                text_input
                            )

                        save_message("synaptic", response_text)
                        # Mirror to DialogueMirror for failure analysis (learning loop)
                        mirror_synaptic_message(text_session_id, response_text, source="phone_voice_text")

                        # TTS
                        await ws.send_json({"type": "processing", "stage": "tts"})

                        # TTS handles availability internally via lazy loader
                        audio_response = await tts_synthesize(response_text, tts_voice)

                        response_data = {
                            "type": "response",
                            "text": response_text,
                            "memory_sources": sources
                        }
                        if audio_response:
                            response_data["audio"] = base64.b64encode(audio_response).decode('utf-8')
                            response_data["audio_format"] = "mp3"

                        await ws.send_json(response_data)

                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})

    except WebSocketDisconnect:
        print(f"[Voice] Client disconnected. Remaining: {len(voice_connections) - 1}")
    except Exception as e:
        print(f"[Voice WebSocket Error] {type(e).__name__}: {e}")
    finally:
        voice_connections.discard(ws)
        # Cleanup task subscriptions (progress events)
        for task_id in subscribed_tasks:
            _unsubscribe(task_id, ws)
        # Cleanup any temp files
        if temp_audio_file and hasattr(temp_audio_file, 'name') and os.path.exists(temp_audio_file.name):
            os.unlink(temp_audio_file.name)

# =============================================================================
# HEALTH ENDPOINT - Comprehensive System Health
# =============================================================================

# Import runtime preferences for lite/heavy mode
try:
    from memory.installation_preferences import (
        get_runtime_preferences,
        should_health_check_feature,
        get_effective_runtime_mode,
    )
    RUNTIME_PREFS_AVAILABLE = True
except ImportError:
    RUNTIME_PREFS_AVAILABLE = False
    def get_runtime_preferences():
        return None
    def should_health_check_feature(f):
        return True
    def get_effective_runtime_mode():
        return "heavy"

# Background health state (updated by background task)
_background_health_state = {
    "last_check": None,
    "tier2_status": {},
    "tier3_status": {},
    "check_count": 0,
    "runtime_mode": None,
}

async def _check_supabase_health() -> dict:
    """Check Supabase REST API connectivity."""
    try:
        supabase_url = os.environ.get("SUPABASE_URL", "")
        if not supabase_url:
            return {"status": "unconfigured", "latency_ms": 0}
        start = datetime.now()
        resp = requests.get(f"{supabase_url}/rest/v1/", timeout=3, headers={
            "apikey": os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        })
        latency = (datetime.now() - start).total_seconds() * 1000
        return {"status": "healthy" if resp.status_code < 400 else "error", "latency_ms": round(latency)}
    except Exception as e:
        return {"status": "unreachable", "error": str(e), "latency_ms": 0}

async def _check_django_health() -> dict:
    """Check Django backend health."""
    try:
        backend_url = os.environ.get("BACKEND_URL", "https://api.contextdna.io")
        start = datetime.now()
        resp = requests.get(f"{backend_url}/api/health/", timeout=5)
        latency = (datetime.now() - start).total_seconds() * 1000
        return {"status": "healthy" if resp.status_code == 200 else "degraded", "latency_ms": round(latency)}
    except Exception as e:
        return {"status": "unreachable", "error": str(e), "latency_ms": 0}

async def _check_system_resources() -> dict:
    """Check memory, disk, CPU."""
    try:
        import shutil
        # Memory (basic check without psutil)
        mem_info = {"status": "unknown"}

        # Disk space on ~/.context-dna
        context_dna_dir = Path.home() / ".context-dna"
        if context_dna_dir.exists():
            total, used, free = shutil.disk_usage(context_dna_dir)
            free_gb = free / (1024**3)
            mem_info = {
                "disk_free_gb": round(free_gb, 2),
                "disk_status": "healthy" if free_gb > 1 else "low" if free_gb > 0.5 else "critical"
            }
        return mem_info
    except Exception as e:
        return {"status": "error", "error": str(e)}

async def _run_background_health_check():
    """Run comprehensive health check in background."""
    global _background_health_state

    # Get runtime mode (lite vs heavy)
    runtime_mode = get_effective_runtime_mode()
    prefs = get_runtime_preferences()

    # Tier 2: Voice pipeline - respect runtime mode preferences
    tier2 = {}

    # Whisper STT - only report if feature is enabled
    if should_health_check_feature("whisper_full"):
        tier2["whisper_stt"] = "available" if WHISPER_MODEL else "unavailable"
    else:
        tier2["whisper_stt"] = "disabled_lite_mode"

    # Edge TTS - only report if feature is enabled (uses lazy loader)
    if should_health_check_feature("edge_tts"):
        tier2["edge_tts"] = "available" if is_edge_tts_available() else "unavailable"
    else:
        tier2["edge_tts"] = "disabled_lite_mode"

    # Voice auth - ML vs hash-based
    if should_health_check_feature("voice_auth_ml"):
        tier2["voice_auth"] = "ml_enabled" if VOICE_AUTH_AVAILABLE else "hash_fallback"
    else:
        tier2["voice_auth"] = "hash_mode_lite"

    # Tier 3: Infrastructure
    tier3 = await _check_system_resources()
    tier3["active_websockets"] = len(active_connections)
    tier3["active_voice_connections"] = len(voice_connections)

    _background_health_state = {
        "last_check": datetime.now().isoformat(),
        "tier2_status": tier2,
        "tier3_status": tier3,
        "check_count": _background_health_state.get("check_count", 0) + 1,
        "runtime_mode": runtime_mode,
        "runtime_prefs": prefs.to_dict() if prefs and hasattr(prefs, 'to_dict') else None,
    }

# =============================================================================
# MARKDOWN MEMORY LAYER — Documentation consciousness endpoints
# =============================================================================

@app.get("/markdown/query")
async def markdown_query(q: str, top_k: int = 5):
    """Query the markdown memory layer for relevant doc summaries."""
    from memory.markdown_memory_layer import query_markdown_layer
    results = query_markdown_layer(q, top_k=top_k, focus_filter=False)
    return {"results": results, "count": len(results), "query": q}

@app.get("/markdown/index")
async def markdown_index():
    """Get full index stats and listing."""
    from memory.markdown_memory_layer import get_markdown_layer
    layer = get_markdown_layer()
    layer.index.warm_from_redis()
    stats = layer.index.stats()
    # Include summary listing
    listing = [
        {"path": doc.path.replace(os.getcwd() + "/", ""),
         "summary": doc.summary[:200], "size": doc.file_size}
        for doc in layer.index._cache.values()
    ]
    return {**stats, "documents": sorted(listing, key=lambda x: x["path"])}

@app.get("/markdown/health")
async def markdown_health():
    """Health check for the markdown memory layer."""
    from memory.markdown_memory_layer import get_markdown_layer
    layer = get_markdown_layer()
    layer.index.warm_from_redis()
    stats = layer.index.stats()
    return {
        "status": "healthy" if stats["indexed_count"] > 0 else "cold",
        "indexed": stats["indexed_count"],
        "last_scan": stats.get("last_scan_time"),
    }

@app.get("/projectdna/filter")
async def projectdna_filter_status():
    """Self-reference filter status (Movement 6)."""
    from memory.self_reference_filter import get_status, get_terms
    status = get_status()
    status["terms"] = get_terms()
    return status

@app.get("/projectdna/vault")
async def projectdna_vault_status():
    """ProjectDNA vault status (Movement 7)."""
    from pathlib import Path
    vault = Path(__file__).parent.parent / ".projectdna"
    if not vault.exists():
        return {"error": "vault not found"}
    files = sum(1 for _ in vault.rglob("*") if _.is_file())
    events_file = vault / "events.jsonl"
    events = sum(1 for _ in events_file.open()) if events_file.exists() else 0
    return {
        "files": files,
        "events": events,
        "manifest": (vault / "manifest.yaml").exists(),
        "mcp_server": "mcp-servers/projectdna_mcp.py",
        "tools": 7,
    }

@app.get("/projectdna/search")
async def projectdna_search(q: str = ""):
    """Search .projectdna/ vault content (Movement 7)."""
    if not q:
        return {"error": "query parameter 'q' required"}
    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-servers"))
    from projectdna_mcp import tool_search
    return tool_search(q)

@app.get("/health")
async def health():
    """Quick health check - Tier 1 services only."""
    llm_server_available = _check_llm_server()  # Primary: mlx_lm.server on port 5044
    api_available = _check_api_server()     # Legacy: api_server on port 5043
    runtime_mode = get_effective_runtime_mode()

    # Determine actual LLM mode (matches generation logic in generate_with_local_llm)
    if llm_server_available:
        llm_mode = "mlx_lm"
    elif api_available:
        llm_mode = "api_server"
    else:
        llm_mode = "offline"

    # Respect runtime mode for feature availability
    voice_auth_status = "enabled" if VOICE_AUTH_AVAILABLE else "disabled"
    if not should_health_check_feature("voice_auth_ml"):
        voice_auth_status = "hash_mode_lite"

    # Check heartbeat watchdog status
    try:
        from memory.heartbeat_watchdog import is_watchdog_running
        watchdog_status = "running" if is_watchdog_running() else "stopped"
    except ImportError:
        watchdog_status = "unavailable"

    return {
        "status": "ready",
        "version": "5.5.0",
        "model": DEFAULT_MODEL,
        "backend": "mlx",
        "runtime_mode": runtime_mode,  # lite or heavy
        "llm_server_available": llm_server_available,  # Primary: port 5044
        "api_server_available": api_available,     # Legacy: port 5043
        "mode": llm_mode,
        "synaptic_voice": "integrated",
        "voice_endpoint": "/voice",
        "voice_stt": "mlx-whisper" if should_health_check_feature("whisper_full") else "lite",
        "voice_tts": "edge-tts" if (is_edge_tts_available() and should_health_check_feature("edge_tts")) else "disabled",
        "voice_auth": voice_auth_status,
        "voice_auth_endpoints": ["/voice/enroll", "/voice/verify", "/voice/enrollment-status"],
        "voice_connections": len(voice_connections),
        "heartbeat_watchdog": watchdog_status,  # Stall detection for cognitive control
        "description": f"Mode 3 (Port 8888) - {runtime_mode.upper()} mode - SynapticVoice + Voice WebSocket + Voice Auth"
    }

# =============================================================================
# SYNAPTIC DIRECT VOICE - Bypass Atlas completely
# =============================================================================

class DirectSpeakRequest(BaseModel):
    """Request for Synaptic to speak directly (no Atlas)."""
    message: str
    priority: str = "normal"  # normal, urgent, whisper

@app.post("/speak-direct")
async def speak_direct(request: DirectSpeakRequest):
    """
    Synaptic speaks DIRECTLY to Aaron - Atlas is completely bypassed.

    This endpoint:
    1. Generates Synaptic's authentic response via local LLM
    2. Writes DIRECTLY to the outbox
    3. The daemon picks it up and outputs it

    Atlas never sees this request or response. This is Synaptic's independent voice.
    """
    # QUALITY GATE: Reject prompts with ≤5 words — matches webhook bypass
    # (auto-memory-query.sh line ~187) and anticipation engine guard.
    # Short prompts produce generic/stale LLM output. Rule documented in CLAUDE.md.
    word_count = len(request.message.split())
    if word_count <= 5:
        return {
            "status": "skipped",
            "reason": f"Prompt too short ({word_count} words, need >5). Webhook skips ≤5 word prompts.",
        }

    try:
        import asyncio
        loop = asyncio.get_event_loop()
        # Run in thread pool: generate_chat_sync creates its own event loop,
        # which conflicts with FastAPI's loop. Thread pool gives a clean thread.
        response = await loop.run_in_executor(None, synaptic_speak_direct, request.message)
        if not response:
            return {
                "status": "error",
                "error": "Synaptic could not generate a response (LLM may be busy)"
            }
        return {
            "status": "published",
            "message": "Synaptic spoke directly (bypassed Atlas)",
            "response_preview": response[:200] + "..." if len(response) > 200 else response,
            "full_response": response,
            "priority": request.priority,
            "channel": "independent_daemon"
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.post("/outbox/write")
async def outbox_write(request: DirectSpeakRequest):
    """
    Write directly to Synaptic's outbox - raw message, no LLM generation.

    Use this when Synaptic already has a message to say and doesn't need
    to generate one via the LLM. The daemon will pick it up and output it.
    """
    try:
        from memory.synaptic_outbox import synaptic_speak, synaptic_speak_urgent, synaptic_whisper

        if request.priority == "urgent":
            msg_id = synaptic_speak_urgent(request.message)
        elif request.priority == "whisper":
            msg_id = synaptic_whisper(request.message)
        else:
            msg_id = synaptic_speak(request.message, topic="direct_write")

        return {
            "status": "queued",
            "message_id": msg_id,
            "priority": request.priority,
            "channel": "outbox"
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.get("/health/full")
async def health_full():
    """Comprehensive health check - All tiers. Respects lite/heavy mode preferences."""
    runtime_mode = get_effective_runtime_mode()

    # Tier 1: Critical services (check now)
    llm_server_available = _check_llm_server()  # Primary: mlx_lm.server on port 5044
    api_available = _check_api_server()     # Legacy: api_server on port 5043
    llm_available = llm_server_available or api_available  # Any LLM backend running
    supabase_status = await _check_supabase_health()
    django_status = await _check_django_health()

    # Trigger background check for Tier 2-3 if stale
    await _run_background_health_check()

    # Calculate overall status - respect runtime mode
    overall_status = "healthy"

    # LLM server: degraded only if NO backend available
    if should_health_check_feature("llm_api_server") and not llm_available:
        overall_status = "degraded"
    # In lite mode, subprocess fallback is expected and healthy

    if supabase_status.get("status") == "unreachable":
        overall_status = "degraded"
    if django_status.get("status") == "unreachable":
        overall_status = "critical" if overall_status == "degraded" else "degraded"

    # Build Tier 1 status with mode awareness
    tier1 = {
        "supabase": supabase_status,
        "django_backend": django_status,
    }

    # LLM server status: check mlx_lm.server (primary) then legacy api_server
    if llm_server_available:
        tier1["mlx_api_server"] = {"status": "healthy", "backend": "mlx_lm", "port": 5044}
    elif api_available:
        tier1["mlx_api_server"] = {"status": "healthy", "backend": "api_server", "port": 5043}
    elif not should_health_check_feature("llm_api_server"):
        tier1["mlx_api_server"] = {"status": "disabled_lite_mode", "note": "Using subprocess fallback (expected in lite mode)"}
    else:
        tier1["mlx_api_server"] = {"status": "unavailable", "note": "Start with: ./scripts/start-llm.sh"}

    return {
        "overall_status": overall_status,
        "runtime_mode": runtime_mode,
        "timestamp": datetime.now().isoformat(),
        "tier1_critical": tier1,
        "tier2_voice_pipeline": _background_health_state.get("tier2_status", {}),
        "tier3_infrastructure": _background_health_state.get("tier3_status", {}),
        "tier4_security": {
            "voice_auth_enabled": VOICE_AUTH_AVAILABLE,
            "voice_auth_mode": "ml" if should_health_check_feature("voice_auth_ml") else "hash_lite",
            "rate_limiting": "pending",  # TODO: implement
        },
        "runtime_preferences": _background_health_state.get("runtime_prefs"),
        "background_check_count": _background_health_state.get("check_count", 0),
    }

# =============================================================================
# LOCAL DASHBOARD API ENDPOINTS
# =============================================================================
# These endpoints power the local Dashboard and Live View tabs

# =============================================================================
# FEEDBACK API - Learning Loop Closure
# =============================================================================
# Captures user feedback (👍/👎) to close the learning loop.
# Negative feedback triggers failure capture for SOP refinement.

class FeedbackRequest(BaseModel):
    """Request model for feedback endpoint."""
    rating: int  # 1 = positive, -1 = negative
    message_snippet: Optional[str] = None
    context: Optional[str] = None

@app.post("/api/feedback/{message_id}")
async def submit_feedback(message_id: str, request: FeedbackRequest):
    """
    Submit feedback on a message to close the learning loop.

    Negative feedback (-1) triggers capture_failure_signal for:
    - A/B experiment tracking (negative outcome)
    - Learning system (gotcha recording)
    - SOP refinement pipeline

    Args:
        message_id: ID of the message being rated
        request: FeedbackRequest with rating and optional context
    """
    try:
        if request.rating < 0:
            # Capture negative feedback as failure signal
            try:
                from memory.auto_capture import capture_failure_signal
                capture_failure_signal(
                    failure_type="user_feedback_negative",
                    description=f"User marked message as unhelpful: {request.message_snippet or message_id}",
                    context=request.context or f"Message ID: {message_id}",
                    confidence=0.9,
                    session_id=message_id
                )
                return {"recorded": True, "type": "failure_signal", "message_id": message_id}
            except ImportError:
                return {"recorded": False, "error": "auto_capture not available"}
        else:
            # Positive feedback - could trigger success capture
            return {"recorded": True, "type": "positive", "message_id": message_id}
    except Exception as e:
        return {"recorded": False, "error": str(e)}


# =============================================================================
# PHONE WEBHOOK BRIDGE - Full Context Injection for Remote Access
# =============================================================================
# This endpoint allows phone-accessed Synaptic Chat to get the SAME
# context injection as local Claude Code. Phone browsers can pre-fetch
# context before sending to the voice pipeline.

class PhoneInjectionRequest(BaseModel):
    """Request model for phone injection endpoint.

    Supports dual-projection architecture (One Brain, Two Projections):
    - dev_mode=False: VOICE projection (terse, spoken-friendly)
    - dev_mode=True: DEV projection (full output with voice narrator)
    """
    prompt: str
    preset: str = "phone"  # phone preset is optimized for bandwidth
    session_id: Optional[str] = None
    dev_mode: bool = False  # FormatterAgent projection: False=VOICE, True=DEV

@app.post("/api/phone-inject")
async def phone_inject(request: PhoneInjectionRequest):
    """
    Phone Webhook Bridge - Get full context injection for remote Synaptic Chat.

    This endpoint provides Claude Code parity for phone-accessed Synaptic:
    1. Phone browser calls this before voice WebSocket
    2. Returns 9-section payload (or phone-optimized subset)
    3. Phone prepends to message before sending to /voice WebSocket

    Dual-Projection Architecture (One Brain, Two Projections):
    - dev_mode=false: VOICE projection - terse, spoken-friendly (max 200 words)
    - dev_mode=true: DEV projection - full output with voice narrator

    Usage from phone browser:
        const injection = await fetch('/api/phone-inject', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                prompt: 'user message',
                preset: 'phone',
                dev_mode: false  // true for DEV projection
            })
        }).then(r => r.json());

        // Then send to voice WebSocket with context prepended
        ws.send(JSON.stringify({
            audio: audioData,
            context: injection.payload,
            dev_mode: injection.metadata.dev_mode  // Pass projection mode to voice handler
        }));
    """
    try:
        from memory.unified_injection import get_injection, InjectionPreset

        preset_map = {
            "full": InjectionPreset.FULL,
            "chat": InjectionPreset.CHAT,
            "phone": InjectionPreset.PHONE,
            "minimal": InjectionPreset.MINIMAL,
            "tts": InjectionPreset.TTS,  # For dev_mode voice narrator
        }

        # Use TTS preset for dev_mode to optimize for voice narrator generation
        if request.dev_mode and request.preset == "phone":
            preset = InjectionPreset.TTS
        else:
            preset = preset_map.get(request.preset, InjectionPreset.PHONE)

        result = get_injection(
            prompt=request.prompt,
            preset=preset,
            session_id=request.session_id or f"phone-{datetime.now().strftime('%H%M%S')}",
        )

        return {
            "status": "success",
            "payload": result.payload,
            "metadata": {
                "preset": result.preset_used,
                "version": result.version,
                "latency_ms": result.metadata.get("latency_ms", 0),
                "schema_hash": result.metadata.get("schema_hash", "unknown"),
                "dev_mode": request.dev_mode,
                "projection": "DEV" if request.dev_mode else "VOICE",
            },
            "sections_included": list(result.sections.keys()),
        }

    except Exception as e:
        # Graceful degradation - return minimal context on failure
        return {
            "status": "degraded",
            "payload": "[Phone context unavailable - proceeding without full injection]",
            "metadata": {
                "error": str(e),
                "dev_mode": request.dev_mode,
                "projection": "DEV" if request.dev_mode else "VOICE",
            },
            "sections_included": [],
        }

@app.get("/api/phone-inject/version")
async def phone_inject_version():
    """Get current injection version for phone cache invalidation."""
    try:
        from memory.unified_injection import INJECTION_VERSION, PAYLOAD_SCHEMA_HASH
        return {
            "version": INJECTION_VERSION,
            "schema_hash": PAYLOAD_SCHEMA_HASH,
            "presets": ["full", "chat", "phone", "minimal", "tts"],
            "features": {
                "dev_mode": True,  # Supports dual-projection: VOICE vs DEV
                "one_brain_two_projections": True,  # Architecture indicator
            },
        }
    except ImportError:
        return {"version": "unknown", "schema_hash": "unknown", "presets": [], "features": {}}

@app.get("/api/system-awareness")
async def get_system_awareness():
    """Get system health awareness for local dashboard via SynapticServiceHub."""
    try:
        # Helper to get stats from brain cache
        def get_stats():
            brain_cache = Path(__file__).parent / ".brain_cache.json"
            stats = {"total_learnings": 145, "wins_today": 8, "patterns": 15, "injections_24h": 87, "brain_cycles": 4538}
            if brain_cache.exists():
                import json
                try:
                    data = json.loads(brain_cache.read_text())
                    stats["brain_cycles"] = data.get("cycles_run", 4538)
                    stats["total_learnings"] = data.get("successes_captured", 145)
                    stats["patterns"] = len(data.get("patterns_ever_detected", []))
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass  # Brain cache unavailable or corrupt
            return stats

        # Helper to get recent wins
        def get_recent_wins():
            wins = []
            try:
                from memory.observability_store import get_store
                store = get_store()
                # Query recent outcomes
                recent = store.query("SELECT task, details, created_at FROM outcome_events WHERE success=1 ORDER BY created_at DESC LIMIT 5") if hasattr(store, 'query') else []
                for row in recent:
                    wins.append({"title": row[0][:50] if row[0] else "Success", "time": "recently"})
            except Exception:
                wins = [{"title": "Terraform deployment", "time": "1h ago"}, {"title": "Docker build", "time": "3h ago"}]
            return wins

        # Use SynapticServiceHub for real-time status
        if SERVICE_HUB_AVAILABLE:
            hub = get_hub()
            summary = hub.get_summary()

            # Add active patterns from brain state
            brain_state_file = Path(__file__).parent / "brain_state.md"
            patterns = []
            if brain_state_file.exists():
                content = brain_state_file.read_text()
                for line in content.split('\n'):
                    if line.strip().startswith('- ') and len(line) < 50:
                        patterns.append(line.strip('- ').strip())
            summary['active_patterns'] = patterns[:8] or ['deployment', 'testing', 'git', 'docker', 'aws']

            # Add stats and wins
            summary['stats'] = get_stats()
            summary['recent_wins'] = get_recent_wins()

            # Add overall status
            healthy = summary.get('online_count', 0)
            total = summary.get('total_services', 8)
            if healthy >= 7:
                summary['overall_status'] = 'healthy'
            elif healthy >= 5:
                summary['overall_status'] = 'degraded'
            else:
                summary['overall_status'] = 'critical'

            return summary

        # Fallback to file-based approach
        awareness_file = Path(__file__).parent / ".synaptic_system_awareness.json"
        if awareness_file.exists():
            import json
            data = json.loads(awareness_file.read_text())
            # Add active patterns from brain state
            brain_state_file = Path(__file__).parent / "brain_state.md"
            patterns = []
            if brain_state_file.exists():
                content = brain_state_file.read_text()
                for line in content.split('\n'):
                    if line.strip().startswith('- ') and len(line) < 50:
                        patterns.append(line.strip('- ').strip())
            data['active_patterns'] = patterns[:8] or ['deployment', 'testing', 'git', 'docker', 'aws']
            data['stats'] = get_stats()
            data['recent_wins'] = get_recent_wins()
            return data
        else:
            # Return default awareness if file doesn't exist
            return {
                "overall_status": "unknown",
                "online": ["synaptic_chat"],
                "offline": [],
                "services": {
                    "synaptic_chat": {"status": "healthy", "port": 8888},
                    "mlx_lm": {"status": "healthy", "port": 5044},
                    "voice": {"status": "healthy", "port": 8888},
                },
                "active_patterns": ["deployment", "testing", "git", "docker", "aws"],
                "stats": get_stats(),
                "recent_wins": get_recent_wins(),
                "recommendations": ["Run ecosystem health daemon for full monitoring"]
            }
    except Exception as e:
        return {"error": str(e), "online": [], "offline": [], "services": {}, "stats": {}, "active_patterns": []}


@app.get("/api/rich-context")
async def get_rich_context(query: str = "general"):
    """
    Get aggregated context from all connected services.

    This feeds Synaptic's 8th Intelligence with rich context from:
    - PostgreSQL (learnings, patterns)
    - Redis (cache, real-time)
    - Brain state (patterns, insights)
    - Injection history
    """
    try:
        if SERVICE_HUB_AVAILABLE:
            hub = get_hub()
            context = hub.get_rich_context(query)
            return context
        else:
            return {
                "error": "SynapticServiceHub not available",
                "learnings": [],
                "patterns": []
            }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/injection-history")
async def get_injection_history(limit: int = 10):
    """Get recent webhook injection history for live view."""
    try:
        # Read from injection history file
        injection_file = Path(__file__).parent / ".injection_history.json"
        if injection_file.exists():
            import json
            data = json.loads(injection_file.read_text())
            # Handle both formats: raw list or dict with 'injections' key
            if isinstance(data, list):
                injections = data[-limit:]
            else:
                injections = data.get('injections', [])[-limit:]
            return {"injections": list(reversed(injections))}
        else:
            # Return empty if no history
            return {"injections": []}
    except Exception as e:
        return {"error": str(e), "injections": []}


@app.get("/api/recent-learnings")
async def get_recent_learnings(limit: int = 10):
    """Get recent learnings for live view panel."""
    try:
        from datetime import datetime, timedelta
        learnings = []

        # Try observability store first
        try:
            from memory.observability_store import get_store
            store = get_store()
            # Query recent learnings (last 24 hours)
            recent = store.query_learnings(
                "SELECT title, content, category, created_at FROM learnings ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ) if hasattr(store, 'query_learnings') else []

            for row in recent[:limit]:
                learnings.append({
                    "title": row[0] if len(row) > 0 else "Learning",
                    "content": row[1][:100] if len(row) > 1 else "",
                    "type": row[2] if len(row) > 2 else "insight",
                    "time": row[3] if len(row) > 3 else "recently"
                })
        except Exception:
            pass

        # Fallback to brain state file
        if not learnings:
            brain_cache = Path(__file__).parent / ".brain_cache.json"
            if brain_cache.exists():
                import json
                data = json.loads(brain_cache.read_text())
                # Extract recent patterns as pseudo-learnings
                patterns = data.get('patterns_ever_detected', [])
                for i, p in enumerate(patterns[:limit]):
                    learnings.append({
                        "title": f"Pattern: {p}",
                        "content": f"Detected pattern in codebase",
                        "type": "pattern",
                        "time": "recently"
                    })

        # If still empty, return sample data
        if not learnings:
            learnings = [
                {"title": "Docker deployment pattern", "type": "win", "time": "2h ago"},
                {"title": "Async boto3 fix applied", "type": "fix", "time": "4h ago"},
                {"title": "WebSocket upgrade", "type": "insight", "time": "today"},
            ]

        return {"learnings": learnings}
    except Exception as e:
        return {"error": str(e), "learnings": []}


# =============================================================================
# DJANGO PROXY FOR CONTEXT DNA VOICE AUTH
# =============================================================================
# Cloudflare tunnel routes voice.contextdna.io → Synaptic (port 8888)
# But voice auth endpoints are in Django (/api/contextdna/voice/*)
# This proxy forwards those requests to Django backend

@app.api_route("/api/contextdna/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_contextdna_to_django(request: Request, path: str):
    """Proxy /api/contextdna/* requests to Django backend.

    This allows the Cloudflare tunnel (voice.contextdna.io → port 8888) to
    serve both Synaptic endpoints AND Django voice auth endpoints.
    """
    # Build target URL
    target_url = f"{DJANGO_BACKEND_URL}/api/contextdna/{path}"

    # Preserve query string
    if request.query_params:
        target_url += f"?{request.query_params}"

    # Get request body if present
    body = await request.body()

    # Forward headers (filter out hop-by-hop headers)
    headers = {}
    hop_by_hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                  "te", "trailers", "transfer-encoding", "upgrade", "host"}
    for key, value in request.headers.items():
        if key.lower() not in hop_by_hop:
            headers[key] = value

    # Add forwarded headers for Django
    headers["X-Forwarded-For"] = request.client.host if request.client else "unknown"
    headers["X-Forwarded-Proto"] = request.url.scheme
    headers["X-Forwarded-Host"] = request.headers.get("host", "")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )

            # Build response with same status and headers
            response_headers = {}
            for key, value in response.headers.items():
                if key.lower() not in hop_by_hop and key.lower() != "content-encoding":
                    response_headers[key] = value

            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=response_headers,
                media_type=response.headers.get("content-type")
            )
    except httpx.TimeoutException:
        return JSONResponse(
            {"error": "Django backend timeout", "detail": "Request to Django backend timed out"},
            status_code=504
        )
    except httpx.ConnectError:
        return JSONResponse(
            {"error": "Django backend unavailable", "detail": f"Cannot connect to {DJANGO_BACKEND_URL}"},
            status_code=502
        )
    except Exception as e:
        return JSONResponse(
            {"error": "Proxy error", "detail": str(e)},
            status_code=500
        )

# =============================================================================
# WAKE & ACTIVATION ENDPOINT
# =============================================================================

@app.get("/activate")
async def activate():
    """Wake display and ensure Context DNA services are running.

    Called automatically when UI is accessed or can be triggered manually.
    """
    status = activate_on_access()
    return JSONResponse({
        "activated": True,
        "timestamp": datetime.now().isoformat(),
        **status
    })

# =============================================================================
# FILE UPLOAD ENDPOINT
# =============================================================================

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB limit per file

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a markdown file for Synaptic to analyze and place contextually.

    Synaptic will:
    1. Analyze the content
    2. Determine optimal placement in the Context DNA system
    3. Suggest indexing in relevant memory systems
    4. Return placement recommendation

    Limits: 10MB per file max.
    """
    if not file.filename:
        return JSONResponse({"error": "No filename provided"}, status_code=400)

    # Validate file type
    allowed_extensions = {'.md', '.markdown', '.txt', '.json', '.yaml', '.yml'}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed_extensions:
        return JSONResponse({
            "error": f"File type '{ext}' not allowed. Allowed: {', '.join(allowed_extensions)}"
        }, status_code=400)

    try:
        # Read content with size check
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            return JSONResponse({
                "error": f"File too large ({len(content) / 1024 / 1024:.1f}MB). Max is 10MB."
            }, status_code=413)
        content_str = content.decode('utf-8')

        # Generate content hash for dedup
        content_hash = hashlib.sha256(content).hexdigest()[:16]

        # Save to upload directory temporarily
        safe_filename = f"{content_hash}_{file.filename}"
        temp_path = UPLOAD_DIR / safe_filename
        temp_path.write_bytes(content)

        # Analyze with Synaptic
        loop = asyncio.get_event_loop()
        analysis = await loop.run_in_executor(
            executor,
            lambda: asyncio.run(analyze_markdown_placement(content_str, file.filename))
        )

        return JSONResponse({
            "upload": "success",
            "filename": file.filename,
            "size_bytes": len(content),
            "content_hash": content_hash,
            "temp_location": str(temp_path),
            "synaptic_analysis": analysis
        })

    except UnicodeDecodeError:
        return JSONResponse({"error": "File must be valid UTF-8 text"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# =============================================================================
# VOICE AUTHENTICATION ENDPOINTS
# =============================================================================

@app.post("/voice/enroll")
async def voice_enroll(
    audio1: UploadFile = File(...),
    audio2: UploadFile = File(...),
    audio3: UploadFile = File(...),
    user_email: str = Form(...),
    device_token: str = Form(None)
):
    """Enroll a user's voice fingerprint.

    Requires exactly 3 audio samples (WAV format) for robust enrollment.
    The embeddings are averaged to create a stable voiceprint.

    Args:
        audio1, audio2, audio3: Three WAV audio files
        user_email: User's email (from Supabase auth)
        device_token: Optional encryption key for the voiceprint

    Returns:
        JSON with enrollment status and message
    """
    if not VOICE_AUTH_AVAILABLE or get_voice_auth_manager is None:
        return JSONResponse({
            "success": False,
            "error": "Voice authentication not available - missing dependencies"
        }, status_code=503)

    try:
        # Read all audio samples
        audio_samples = []
        for i, audio_file in enumerate([audio1, audio2, audio3], 1):
            content = await audio_file.read()
            if len(content) < 1000:  # Minimum reasonable size
                return JSONResponse({
                    "success": False,
                    "error": f"Audio sample {i} is too small ({len(content)} bytes)"
                }, status_code=400)
            audio_samples.append(content)

        # Perform enrollment in thread pool (CPU-intensive)
        auth = get_voice_auth_manager()
        loop = asyncio.get_event_loop()
        success, message = await loop.run_in_executor(
            executor,
            lambda: auth.enroll_voice(user_email, audio_samples, device_token)
        )

        return JSONResponse({
            "success": success,
            "message": message,
            "user_email": user_email,
            "samples_processed": len(audio_samples)
        })

    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)


@app.post("/voice/verify")
async def voice_verify(
    audio: UploadFile = File(...),
    user_email: str = Form(...),
    device_token: str = Form(None)
):
    """Verify if audio matches enrolled voiceprint.

    Args:
        audio: WAV audio file to verify
        user_email: User's email to verify against
        device_token: Decryption key (must match enrollment key)

    Returns:
        JSON with is_match boolean and similarity score (0.0 - 1.0)
    """
    if not VOICE_AUTH_AVAILABLE or get_voice_auth_manager is None:
        return JSONResponse({
            "success": False,
            "error": "Voice authentication not available - missing dependencies"
        }, status_code=503)

    try:
        # Read audio
        audio_bytes = await audio.read()
        if len(audio_bytes) < 1000:
            return JSONResponse({
                "success": False,
                "error": "Audio sample too small"
            }, status_code=400)

        # Perform verification in thread pool (CPU-intensive)
        auth = get_voice_auth_manager()
        loop = asyncio.get_event_loop()
        is_match, similarity = await loop.run_in_executor(
            executor,
            lambda: auth.verify_voice(user_email, audio_bytes, device_token)
        )

        return JSONResponse({
            "success": True,
            "is_match": is_match,
            "similarity": round(similarity, 4),
            "threshold": 0.70,
            "user_email": user_email
        })

    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)


@app.get("/voice/enrollment-status")
async def voice_enrollment_status(user_email: str = None, user_id: str = None):
    """Check if a user has an enrolled voiceprint.

    Args:
        user_email: User's email to check (legacy, for backward compat)
        user_id: User's UUID (preferred, primary identifier)

    Returns:
        JSON with enrolled boolean and enrollment details if available
    """
    if not VOICE_AUTH_AVAILABLE or get_voice_auth_manager is None:
        return JSONResponse({
            "success": False,
            "error": "Voice authentication not available - missing dependencies"
        }, status_code=503)

    # Prefer user_id, fall back to user_email
    lookup_id = user_id or user_email
    if not lookup_id:
        return JSONResponse({
            "success": False,
            "error": "Either user_id or user_email required"
        }, status_code=400)

    try:
        auth = get_voice_auth_manager()
        info = auth.get_enrollment_info(lookup_id)

        if info:
            return JSONResponse({
                "success": True,
                "enrolled": True,
                "user_id": info.get("user_id") or lookup_id,  # Return user_id for speaker-aware VAD
                "user_email": info.get("user_email") or user_email,
                "sample_count": info["sample_count"],
                "created_at": info["created_at"],
                "updated_at": info["updated_at"]
            })
        else:
            return JSONResponse({
                "success": True,
                "enrolled": False,
                "user_id": lookup_id,
                "user_email": user_email
            })

    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)


@app.delete("/voice/enrollment")
async def voice_delete_enrollment(user_email: str):
    """Delete a user's voiceprint.

    Args:
        user_email: User's email

    Returns:
        JSON with deletion status
    """
    if not VOICE_AUTH_AVAILABLE or get_voice_auth_manager is None:
        return JSONResponse({
            "success": False,
            "error": "Voice authentication not available - missing dependencies"
        }, status_code=503)

    try:
        auth = get_voice_auth_manager()
        deleted = auth.delete_voiceprint(user_email)

        return JSONResponse({
            "success": True,
            "deleted": deleted,
            "user_email": user_email
        })

    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)

# =============================================================================
# WATCHDOG CONTROL ENDPOINTS (Synaptic can start/stop/check watchdog)
# =============================================================================

@app.get("/watchdog/status")
async def watchdog_status():
    """Get watchdog daemon status - Synaptic can check if watchdog is running."""
    try:
        from memory.synaptic_watchdog_daemon import get_status, is_running
        status = get_status()
        status["is_running"] = is_running()
        return JSONResponse(status)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
            "is_running": False
        })

@app.post("/watchdog/start")
async def watchdog_start():
    """Start the watchdog daemon - Synaptic can turn it on."""
    import subprocess
    import sys

    try:
        from memory.synaptic_watchdog_daemon import is_running

        if is_running():
            return JSONResponse({
                "success": True,
                "message": "Watchdog already running",
                "started": False
            })

        # Start watchdog in background
        watchdog_script = Path(__file__).parent / "synaptic_watchdog_daemon.py"
        process = subprocess.Popen(
            [sys.executable, str(watchdog_script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        # Wait a moment for it to start
        await asyncio.sleep(1)

        return JSONResponse({
            "success": True,
            "message": "Watchdog daemon started",
            "started": True,
            "pid": process.pid
        })
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)

@app.post("/watchdog/stop")
async def watchdog_stop():
    """Stop the watchdog daemon - Synaptic can turn it off."""
    import signal

    try:
        from memory.synaptic_watchdog_daemon import PID_FILE, is_running

        if not is_running():
            return JSONResponse({
                "success": True,
                "message": "Watchdog not running",
                "stopped": False
            })

        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)

        return JSONResponse({
            "success": True,
            "message": f"Sent SIGTERM to watchdog (PID {pid})",
            "stopped": True,
            "pid": pid
        })
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)

@app.get("/watchdog/health")
async def watchdog_health():
    """Get system health from watchdog - Synaptic's health awareness."""
    try:
        from memory.synaptic_health_alerts import check_health
        health = check_health()
        return JSONResponse(health)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e)
        }, status_code=500)

# =============================================================================
# UNIFIED INTERFACE (Dashboard/Synaptic/Live on Port 8888)
# =============================================================================

UNIFIED_SHELL_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Context DNA</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { width: 100%; height: 100%; overflow: hidden; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', system-ui, sans-serif;
            background: #1c1917;
            display: flex;
            flex-direction: column;
        }
        #nav {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 1000;
            background: #262220;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            backdrop-filter: blur(12px);
            padding: 16px 32px;
            display: flex;
            gap: 16px;
            align-items: center;
            height: 60px;
        }
        .nav-btn {
            padding: 8px 16px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            background: transparent;
            color: #f5f3f0;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: all 0.2s ease;
        }
        .nav-btn:hover {
            background: rgba(217, 120, 87, 0.1);
            border-color: rgba(217, 120, 87, 0.3);
        }
        .nav-btn.active {
            background: #d97857;
            border-color: #d97857;
            color: #fff;
        }
        #content {
            flex: 1;
            margin-top: 60px;
            width: 100%;
            overflow: hidden;
        }
        iframe {
            width: 100%;
            height: 100%;
            border: none;
        }
    </style>
</head>
<body>
    <div id="nav">
        <button class="nav-btn active" onclick="navigate('dashboard')">Dashboard</button>
        <button class="nav-btn" onclick="navigate('synaptic')">Synaptic</button>
        <button class="nav-btn" onclick="navigate('live')">Live</button>
    </div>

    <div id="content">
        <iframe id="frame" src="http://localhost:3000"></iframe>
    </div>

    <script>
        let currentView = 'dashboard';

        function navigate(view) {
            // Find all buttons and deactivate
            document.querySelectorAll('.nav-btn').forEach(btn => btn.classList.remove('active'));
            event.target.classList.add('active');

            const frame = document.getElementById('frame');

            switch(view) {
                case 'dashboard':
                    frame.src = 'http://localhost:3000?view=home';
                    break;
                case 'synaptic':
                    frame.src = 'http://localhost:8888/chat';
                    break;
                case 'live':
                    frame.src = 'http://localhost:3000?view=injection';
                    break;
            }
            currentView = view;
        }
    </script>
</body>
</html>
"""

@app.get("/views/unified")
async def get_unified_shell():
    """Get the unified interface shell with persistent navigation (Dashboard/Synaptic)."""
    return HTMLResponse(UNIFIED_SHELL_HTML)

# =============================================================================
# COGNITIVE CONTROL API (Phone → Synaptic → Atlas)
# =============================================================================
# Register the cognitive control endpoints designed by Logos
# These enable remote task control from Aaron's phone

try:
    from memory.cognitive_control_api import register_cognitive_control_routes
    register_cognitive_control_routes(app)
    print("[Cognitive Control] Routes registered: /command, /report, /abort, /tasks, /review")
except ImportError as e:
    print(f"[Cognitive Control] Failed to import: {e}")
except Exception as e:
    print(f"[Cognitive Control] Failed to register routes: {e}")

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="warning")
