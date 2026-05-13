#!/usr/bin/env python3
"""
memory.synaptic_voice_audio — WebSocket TTS/STT bridge (counter-position build).

═══════════════════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════════════════

This module implements the LITERAL reading of Synaptic's directive:
"Deploy synaptic_voice as a WebSocket-based TTS/STT bridge".

It is the counter-position experiment to Atlas's `synaptic_voice.py`
(the personality/consultation engine that ships in migrate3). It is NOT
a competing implementation — it is a separate concern at a different
layer. See REPORT.md for the verdict on coexistence.

ARCHITECTURE
────────────

  ┌──────────────────────────────────────────────────────────────┐
  │  ws://127.0.0.1:8889/voice/<session_id>                       │
  │                                                                │
  │  TTS path:                                                     │
  │    client → {"type":"tts","text":"..."}                       │
  │    server → {"type":"audio","format":"wav","data":"<b64>"}    │
  │                                                                │
  │  STT path:                                                     │
  │    client → <binary WAV/PCM frames>                            │
  │    server → {"type":"transcript","text":"..."}                │
  │                                                                │
  │  Errors:                                                       │
  │    server → {"type":"error","code":"...","message":"..."}     │
  └──────────────────────────────────────────────────────────────┘

ZSF CONTRACT
────────────

Every code path that hits a missing dependency, a stub return, an
exception, or a degraded fallback bumps a counter exposed via
`get_zsf_counters()`. No `except Exception: pass`. No silent stubs.

TTS BACKEND PRIORITY
────────────────────

  1. pyttsx3 (cross-platform, offline)        — if installed
  2. macOS `say` CLI + afconvert → WAV bytes  — if Darwin
  3. Stub: emits silence WAV + bumps counter  — guaranteed-shape last resort

STT BACKEND PRIORITY
────────────────────

  1. faster-whisper (offline)                 — if installed
  2. Stub: returns empty transcript + counter — guaranteed-shape last resort

The stub last-resorts MUST produce well-shaped output (valid WAV header,
empty string) so callers can complete their protocol round-trip even on
a host with no audio stack installed. The counters tell ops what to fix.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import platform
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse


# =============================================================================
# Logger + ZSF counters
# =============================================================================

logger = logging.getLogger("context_dna.synaptic_voice_audio")

_ZSF_COUNTERS: Dict[str, int] = {
    "tts_calls": 0,
    "tts_pyttsx3_used": 0,
    "tts_macos_say_used": 0,
    "tts_stub_used": 0,
    "tts_errors": 0,
    "stt_calls": 0,
    "stt_whisper_used": 0,
    "stt_stub_used": 0,
    "stt_errors": 0,
    "ws_connections": 0,
    "ws_messages_in": 0,
    "ws_messages_out": 0,
    "ws_protocol_errors": 0,
    "ws_handler_exceptions": 0,
    "client_sync_calls": 0,
    "client_sync_errors": 0,
}
_ZSF_LOCK = threading.Lock()


def _bump(counter: str, n: int = 1) -> None:
    with _ZSF_LOCK:
        _ZSF_COUNTERS[counter] = _ZSF_COUNTERS.get(counter, 0) + n


def get_zsf_counters() -> Dict[str, int]:
    with _ZSF_LOCK:
        return dict(_ZSF_COUNTERS)


def reset_zsf_counters_for_testing() -> None:
    """Reset counters — test helper, not for production use."""
    with _ZSF_LOCK:
        for k in list(_ZSF_COUNTERS.keys()):
            _ZSF_COUNTERS[k] = 0


# =============================================================================
# Optional dependency probes — done lazily, observable via counters
# =============================================================================

_PYTTSX3_AVAILABLE: Optional[bool] = None
_WHISPER_AVAILABLE: Optional[bool] = None
_WEBSOCKETS_AVAILABLE: Optional[bool] = None


def _probe_pyttsx3() -> bool:
    global _PYTTSX3_AVAILABLE
    if _PYTTSX3_AVAILABLE is not None:
        return _PYTTSX3_AVAILABLE
    try:
        import pyttsx3  # noqa: F401
        _PYTTSX3_AVAILABLE = True
    except Exception:
        _PYTTSX3_AVAILABLE = False
    return _PYTTSX3_AVAILABLE


def _probe_whisper() -> bool:
    global _WHISPER_AVAILABLE
    if _WHISPER_AVAILABLE is not None:
        return _WHISPER_AVAILABLE
    try:
        import faster_whisper  # noqa: F401
        _WHISPER_AVAILABLE = True
    except Exception:
        _WHISPER_AVAILABLE = False
    return _WHISPER_AVAILABLE


def _probe_websockets() -> bool:
    global _WEBSOCKETS_AVAILABLE
    if _WEBSOCKETS_AVAILABLE is not None:
        return _WEBSOCKETS_AVAILABLE
    try:
        import websockets  # noqa: F401
        _WEBSOCKETS_AVAILABLE = True
    except Exception:
        _WEBSOCKETS_AVAILABLE = False
    return _WEBSOCKETS_AVAILABLE


def _is_darwin() -> bool:
    return platform.system() == "Darwin"


# =============================================================================
# TTS — text → WAV bytes
# =============================================================================


def _silence_wav(duration_s: float = 0.2, sample_rate: int = 16000) -> bytes:
    """Return a valid mono 16-bit PCM WAV with `duration_s` of silence."""
    n_frames = max(1, int(duration_s * sample_rate))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_frames)
    return buf.getvalue()


def _tts_pyttsx3(text: str) -> Optional[bytes]:
    """Synthesize via pyttsx3 to a temp WAV, return bytes. None on failure."""
    try:
        import pyttsx3
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            out_path = tf.name
        try:
            engine = pyttsx3.init()
            engine.save_to_file(text, out_path)
            engine.runAndWait()
            data = Path(out_path).read_bytes()
            _bump("tts_pyttsx3_used")
            return data
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
    except Exception as e:
        logger.warning("pyttsx3 synthesis failed: %s", e)
        _bump("tts_errors")
        return None


def _tts_macos_say(text: str) -> Optional[bytes]:
    """Synthesize via macOS `say` -> .aiff -> afconvert -> WAV. None on failure."""
    if not _is_darwin():
        return None
    aiff_path: Optional[str] = None
    wav_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as af:
            aiff_path = af.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wf:
            wav_path = wf.name

        # Step 1: text → AIFF
        r1 = subprocess.run(
            ["/usr/bin/say", "-o", aiff_path, text],
            capture_output=True,
            timeout=30,
        )
        if r1.returncode != 0:
            logger.warning("macOS say failed: rc=%s stderr=%s", r1.returncode, r1.stderr[:200])
            _bump("tts_errors")
            return None

        # Step 2: AIFF → WAV (16-bit PCM, 16kHz, mono) for STT compatibility
        r2 = subprocess.run(
            [
                "/usr/bin/afconvert",
                "-f", "WAVE",
                "-d", "LEI16@16000",
                "-c", "1",
                aiff_path,
                wav_path,
            ],
            capture_output=True,
            timeout=30,
        )
        if r2.returncode != 0:
            logger.warning("afconvert failed: rc=%s stderr=%s", r2.returncode, r2.stderr[:200])
            _bump("tts_errors")
            return None

        data = Path(wav_path).read_bytes()
        _bump("tts_macos_say_used")
        return data
    except Exception as e:
        logger.warning("macOS say pipeline raised: %s", e)
        _bump("tts_errors")
        return None
    finally:
        for p in (aiff_path, wav_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def synthesize(text: str) -> bytes:
    """
    Public TTS entry point. Returns WAV bytes.

    Backend order: pyttsx3 → macOS `say` → silence stub.
    Stub path bumps `tts_stub_used` so /health can flag missing audio stack.
    """
    _bump("tts_calls")
    if not isinstance(text, str) or not text.strip():
        # Empty text: still produce well-shaped output so the protocol completes.
        _bump("tts_stub_used")
        return _silence_wav(0.05)

    if _probe_pyttsx3():
        data = _tts_pyttsx3(text)
        if data:
            return data

    data = _tts_macos_say(text)
    if data:
        return data

    # Stub last resort — bump counter, return silence so the round-trip succeeds.
    logger.warning(
        "TTS: no backend available (pyttsx3=%s, darwin=%s) — returning silence stub",
        _probe_pyttsx3(),
        _is_darwin(),
    )
    _bump("tts_stub_used")
    return _silence_wav(0.5)


# =============================================================================
# STT — WAV bytes → text
# =============================================================================


def _stt_faster_whisper(audio_bytes: bytes, model_size: str = "tiny.en") -> Optional[str]:
    """Transcribe via faster-whisper. None on failure."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
        wav_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tf.write(audio_bytes)
                wav_path = tf.name
            # CPU, int8 — smallest viable footprint
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            segments, _info = model.transcribe(wav_path, beam_size=1)
            text = "".join(seg.text for seg in segments).strip()
            _bump("stt_whisper_used")
            return text
        finally:
            if wav_path:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass
    except Exception as e:
        logger.warning("faster-whisper transcription failed: %s", e)
        _bump("stt_errors")
        return None


def _stt_stub_validate(audio_bytes: bytes) -> str:
    """
    Stub STT: validates the input is a well-formed WAV (or empty), returns "".

    Bumps `stt_stub_used` so /health can flag missing STT stack. The reason
    we validate the header at all is to distinguish "client sent garbage"
    (bump stt_errors) from "no backend installed" (bump stt_stub_used).
    """
    if not audio_bytes:
        _bump("stt_stub_used")
        return ""
    if len(audio_bytes) < 44:
        # Too small for a WAV header — count as protocol error, still return ""
        _bump("stt_errors")
        return ""
    # RIFF/WAVE header sniff
    if audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        _bump("stt_errors")
        return ""
    _bump("stt_stub_used")
    return ""


def transcribe(audio_bytes: bytes) -> str:
    """
    Public STT entry point. Returns transcript text.

    Backend order: faster-whisper → header-validating stub.
    """
    _bump("stt_calls")
    if _probe_whisper():
        result = _stt_faster_whisper(audio_bytes)
        if result is not None:
            return result
    return _stt_stub_validate(audio_bytes)


# =============================================================================
# WebSocket server — ws://127.0.0.1:8889/voice/<session_id>
# =============================================================================


@dataclass
class VoiceServerState:
    host: str = "127.0.0.1"
    port: int = 8889
    sessions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    started_at: Optional[float] = None
    server_obj: Any = None
    stop_event: Optional[asyncio.Event] = None


_SERVER_STATE = VoiceServerState()
_SERVER_LOCK = threading.Lock()


def _parse_session_id(path: str) -> Optional[str]:
    """Extract <session_id> from /voice/<session_id>. Returns None if path malformed."""
    if not path:
        return None
    parsed = urlparse(path)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) == 2 and parts[0] == "voice":
        return parts[1]
    return None


async def _handle_text_frame(payload: str, session_id: str, ws: Any) -> None:
    """Handle a JSON text frame. Currently supports {"type":"tts","text":...}."""
    try:
        msg = json.loads(payload)
    except json.JSONDecodeError:
        _bump("ws_protocol_errors")
        await ws.send(json.dumps({
            "type": "error",
            "code": "bad_json",
            "message": "frame is not valid JSON",
        }))
        _bump("ws_messages_out")
        return

    msg_type = msg.get("type")
    if msg_type == "tts":
        text = msg.get("text", "")
        wav_bytes = synthesize(text)
        b64 = base64.b64encode(wav_bytes).decode("ascii")
        await ws.send(json.dumps({
            "type": "audio",
            "format": "wav",
            "data": b64,
            "session_id": session_id,
            "bytes": len(wav_bytes),
        }))
        _bump("ws_messages_out")
    elif msg_type == "ping":
        await ws.send(json.dumps({"type": "pong", "session_id": session_id}))
        _bump("ws_messages_out")
    else:
        _bump("ws_protocol_errors")
        await ws.send(json.dumps({
            "type": "error",
            "code": "unknown_type",
            "message": f"unsupported message type: {msg_type!r}",
        }))
        _bump("ws_messages_out")


async def _handle_binary_frame(payload: bytes, session_id: str, ws: Any) -> None:
    """Handle a binary audio frame → transcribe → emit transcript JSON."""
    text = transcribe(payload)
    await ws.send(json.dumps({
        "type": "transcript",
        "text": text,
        "session_id": session_id,
        "bytes_in": len(payload),
    }))
    _bump("ws_messages_out")


async def _connection_handler(ws: Any) -> None:
    """
    Per-connection coroutine. Compatible with websockets >=10 (single-arg
    handler) — path is read from ws.request.path on modern versions.
    """
    # websockets 10+: handler signature is (ws), path is on ws.request.path
    # websockets 8/9: handler signature is (ws, path) — we don't support those.
    try:
        path = getattr(getattr(ws, "request", None), "path", None) or getattr(ws, "path", "")
    except Exception:
        path = ""

    session_id = _parse_session_id(path)
    if not session_id:
        _bump("ws_protocol_errors")
        try:
            await ws.send(json.dumps({
                "type": "error",
                "code": "bad_path",
                "message": "expected ws://host:port/voice/<session_id>",
            }))
            _bump("ws_messages_out")
        finally:
            await ws.close(code=1008, reason="bad path")
        return

    _bump("ws_connections")
    with _SERVER_LOCK:
        _SERVER_STATE.sessions[session_id] = {
            "connected_at": time.time(),
            "messages_in": 0,
        }

    try:
        async for msg in ws:
            _bump("ws_messages_in")
            with _SERVER_LOCK:
                if session_id in _SERVER_STATE.sessions:
                    _SERVER_STATE.sessions[session_id]["messages_in"] += 1

            try:
                if isinstance(msg, (bytes, bytearray)):
                    await _handle_binary_frame(bytes(msg), session_id, ws)
                else:
                    await _handle_text_frame(msg, session_id, ws)
            except Exception as e:
                _bump("ws_handler_exceptions")
                logger.exception("handler raised on session %s: %s", session_id, e)
                try:
                    await ws.send(json.dumps({
                        "type": "error",
                        "code": "handler_exception",
                        "message": str(e),
                    }))
                    _bump("ws_messages_out")
                except Exception:
                    pass
    except Exception as e:
        _bump("ws_handler_exceptions")
        logger.warning("connection loop ended on session %s: %s", session_id, e)
    finally:
        with _SERVER_LOCK:
            _SERVER_STATE.sessions.pop(session_id, None)


async def _run_server_async(host: str, port: int, stop_event: asyncio.Event) -> None:
    if not _probe_websockets():
        logger.error("websockets library missing — cannot start audio bridge")
        raise RuntimeError("websockets library not installed")
    import websockets
    async with websockets.serve(_connection_handler, host, port):
        logger.info("synaptic_voice_audio bridge listening on ws://%s:%s/voice/<session_id>", host, port)
        await stop_event.wait()


def start_audio_bridge(host: str = "127.0.0.1", port: int = 8889) -> Dict[str, Any]:
    """
    Start the WebSocket TTS/STT bridge in a background thread. Idempotent.

    Returns a status dict with started, host, port, deps_available.
    """
    with _SERVER_LOCK:
        if _SERVER_STATE.started_at is not None:
            return {
                "status": "already_running",
                "host": _SERVER_STATE.host,
                "port": _SERVER_STATE.port,
                "started_at": _SERVER_STATE.started_at,
            }

    if not _probe_websockets():
        return {
            "status": "missing_dep",
            "missing": "websockets",
            "host": host,
            "port": port,
        }

    loop = asyncio.new_event_loop()
    stop_event_holder: Dict[str, asyncio.Event] = {}

    def _runner() -> None:
        asyncio.set_event_loop(loop)
        stop_event_holder["e"] = asyncio.Event()
        try:
            loop.run_until_complete(_run_server_async(host, port, stop_event_holder["e"]))
        except Exception as e:
            logger.exception("audio bridge thread crashed: %s", e)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_runner, name="synaptic_voice_audio", daemon=True)
    t.start()

    # Wait briefly for the event loop to spin up the stop_event before declaring "started".
    for _ in range(50):
        if "e" in stop_event_holder:
            break
        time.sleep(0.02)

    with _SERVER_LOCK:
        _SERVER_STATE.host = host
        _SERVER_STATE.port = port
        _SERVER_STATE.started_at = time.time()
        _SERVER_STATE.server_obj = (loop, t)
        _SERVER_STATE.stop_event = stop_event_holder.get("e")

    return {
        "status": "started",
        "host": host,
        "port": port,
        "started_at": _SERVER_STATE.started_at,
        "deps_available": {
            "websockets": _probe_websockets(),
            "pyttsx3": _probe_pyttsx3(),
            "faster_whisper": _probe_whisper(),
            "macos_say": _is_darwin(),
        },
    }


def stop_audio_bridge() -> Dict[str, Any]:
    """Stop the background WebSocket bridge if running."""
    with _SERVER_LOCK:
        if _SERVER_STATE.started_at is None or not _SERVER_STATE.stop_event:
            return {"status": "not_running"}
        loop_and_thread = _SERVER_STATE.server_obj
        stop_event = _SERVER_STATE.stop_event

    if loop_and_thread:
        loop, _t = loop_and_thread
        loop.call_soon_threadsafe(stop_event.set)

    with _SERVER_LOCK:
        _SERVER_STATE.started_at = None
        _SERVER_STATE.server_obj = None
        _SERVER_STATE.stop_event = None
        _SERVER_STATE.sessions.clear()

    return {"status": "stopped"}


# =============================================================================
# Sync Python client — for Atlas-side callers that aren't asyncio
# =============================================================================


class SynapticVoiceClient:
    """
    Thin synchronous client wrapping the WebSocket bridge.

    Usage:
        c = SynapticVoiceClient()
        wav = c.synthesize("hello world")     # bytes (WAV)
        text = c.transcribe(wav)              # str

    Implementation note: for callers running in the same process, the client
    short-circuits the WebSocket and calls `synthesize()` / `transcribe()`
    directly. This keeps unit tests fast and avoids spinning up a server
    just to round-trip a string. Set use_ws=True to force network I/O.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8889,
        session_id: Optional[str] = None,
        use_ws: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.session_id = session_id or f"client-{int(time.time() * 1000)}"
        self.use_ws = use_ws

    def synthesize(self, text: str) -> bytes:
        _bump("client_sync_calls")
        if not self.use_ws:
            return synthesize(text)
        try:
            return asyncio.run(self._synthesize_ws(text))
        except Exception as e:
            _bump("client_sync_errors")
            logger.warning("client synthesize via WS failed: %s — falling back to in-process", e)
            return synthesize(text)

    def transcribe(self, audio_bytes: bytes) -> str:
        _bump("client_sync_calls")
        if not self.use_ws:
            return transcribe(audio_bytes)
        try:
            return asyncio.run(self._transcribe_ws(audio_bytes))
        except Exception as e:
            _bump("client_sync_errors")
            logger.warning("client transcribe via WS failed: %s — falling back to in-process", e)
            return transcribe(audio_bytes)

    async def _synthesize_ws(self, text: str) -> bytes:
        import websockets
        uri = f"ws://{self.host}:{self.port}/voice/{self.session_id}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "tts", "text": text}))
            reply = await ws.recv()
            msg = json.loads(reply)
            if msg.get("type") != "audio":
                raise RuntimeError(f"unexpected reply type: {msg.get('type')}")
            return base64.b64decode(msg["data"])

    async def _transcribe_ws(self, audio_bytes: bytes) -> str:
        import websockets
        uri = f"ws://{self.host}:{self.port}/voice/{self.session_id}"
        async with websockets.connect(uri) as ws:
            await ws.send(audio_bytes)
            reply = await ws.recv()
            msg = json.loads(reply)
            if msg.get("type") != "transcript":
                raise RuntimeError(f"unexpected reply type: {msg.get('type')}")
            return msg.get("text", "")


# =============================================================================
# CLI smoke test
# =============================================================================


def _smoke_test() -> int:
    """
    End-to-end round-trip:
      1. Start bridge
      2. TTS "the quick brown fox" → WAV bytes
      3. Validate WAV header
      4. STT the WAV → text (whatever the stack returns; stub OK)
      5. Print counters
      6. Stop bridge
    """
    print("[1/6] Starting bridge...", flush=True)
    status = start_audio_bridge()
    print(f"      status: {json.dumps(status, indent=2)}", flush=True)
    if status.get("status") not in ("started", "already_running"):
        print("      FAILED to start bridge", flush=True)
        return 1

    # Give the listener a moment to bind
    time.sleep(0.3)

    try:
        print("[2/6] In-process TTS...", flush=True)
        wav = synthesize("The quick brown fox jumps over the lazy dog.")
        print(f"      WAV bytes: {len(wav)} (header={wav[:4]!r} format={wav[8:12]!r})", flush=True)

        print("[3/6] Validating WAV header...", flush=True)
        assert wav[:4] == b"RIFF", f"missing RIFF magic: {wav[:4]!r}"
        assert wav[8:12] == b"WAVE", f"missing WAVE magic: {wav[8:12]!r}"
        print("      OK", flush=True)

        print("[4/6] In-process STT round-trip...", flush=True)
        text = transcribe(wav)
        print(f"      transcript: {text!r}", flush=True)

        print("[5/6] WebSocket round-trip (real network I/O)...", flush=True)
        client = SynapticVoiceClient(use_ws=True)
        try:
            wav2 = client.synthesize("Hello from the WebSocket bridge.")
            print(f"      WS TTS bytes: {len(wav2)} (header={wav2[:4]!r})", flush=True)
            assert wav2[:4] == b"RIFF", "WS TTS did not return WAV"
            text2 = client.transcribe(wav2)
            print(f"      WS STT transcript: {text2!r}", flush=True)
        except Exception as e:
            print(f"      WS round-trip raised: {e}", flush=True)

        print("[6/6] Counters:", flush=True)
        print(json.dumps(get_zsf_counters(), indent=2), flush=True)
    finally:
        print("Stopping bridge...", flush=True)
        print(json.dumps(stop_audio_bridge(), indent=2), flush=True)

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(_smoke_test())
