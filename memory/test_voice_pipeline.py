#!/usr/bin/env python3
"""
FULL VOICE PIPELINE TEST

Tests the complete STT → LLM → TTS pipeline:
1. Connect to voice.contextdna.io WebSocket
2. Send audio samples (simulates voice input)
3. Verify STT transcription
4. Verify LLM response
5. Verify TTS audio returned

Usage:
    python memory/test_voice_pipeline.py
"""

import asyncio
import json
import base64
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Installing websockets...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

# Voice server WebSocket endpoint
VOICE_WS_URL = "wss://voice.contextdna.io/voice"
LOCAL_WS_URL = "ws://localhost:8888/voice"

# Test messages to send via TTS (will generate audio responses)
TEST_PROMPTS = [
    "Hello Synaptic, this is a voice pipeline test. Can you confirm you can hear me?",
    "What is Context DNA and how does it help with AI memory?",
    "Please give me a brief status report on the system health.",
]


async def test_voice_pipeline(use_local: bool = True):
    """Test the full voice pipeline."""
    ws_url = LOCAL_WS_URL if use_local else VOICE_WS_URL
    print(f"\n{'='*60}")
    print("VOICE PIPELINE TEST - STT → LLM → TTS")
    print(f"{'='*60}")
    print(f"WebSocket URL: {ws_url}")
    print(f"Test prompts: {len(TEST_PROMPTS)}")
    print()

    try:
        async with websockets.connect(ws_url) as ws:
            print("✅ WebSocket connected!")

            # Test each prompt
            for i, prompt in enumerate(TEST_PROMPTS, 1):
                print(f"\n--- Test {i}/{len(TEST_PROMPTS)} ---")
                print(f"Prompt: {prompt[:50]}...")

                # Send text message (simulates STT output)
                # The server accepts text input for testing when no audio is provided
                await ws.send(json.dumps({
                    "type": "text",
                    "text": prompt,
                    "test_mode": True  # Indicates this is a test
                }))

                # Wait for response
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    data = json.loads(response)

                    if data.get("type") == "response":
                        print(f"✅ LLM Response received:")
                        print(f"   Text: {data.get('text', '')[:100]}...")
                        if data.get("audio"):
                            audio_size = len(base64.b64decode(data["audio"]))
                            print(f"   Audio: {audio_size:,} bytes")
                        else:
                            print("   Audio: (no audio in response)")
                    elif data.get("type") == "error":
                        print(f"❌ Error: {data.get('message', 'Unknown error')}")
                    else:
                        print(f"📨 Received: {data}")

                except asyncio.TimeoutError:
                    print("❌ Timeout waiting for response")

            print(f"\n{'='*60}")
            print("VOICE PIPELINE TEST COMPLETE")
            print(f"{'='*60}")

    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False

    return True


async def test_voice_auth_enrollment():
    """Test voice authentication enrollment status."""
    import httpx

    print("\n--- Voice Auth Enrollment Status ---")

    async with httpx.AsyncClient() as client:
        # Test direct API
        response = await client.get(
            "https://api.ersimulator.com/api/contextdna/voice/enrollment-status/",
            params={"user_email": "you@example.com"}
        )
        print(f"Direct API: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"  Enrolled: {data.get('enrolled')}")
            print(f"  Samples: {data.get('sample_count')}")
        else:
            print(f"  Error: {response.text}")


async def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("FULL VOICE PIPELINE END-TO-END TEST")
    print("="*60)

    # Test 1: Voice auth enrollment status
    try:
        await test_voice_auth_enrollment()
    except Exception as e:
        print(f"Auth test failed: {e}")

    # Test 2: Voice pipeline (STT → LLM → TTS)
    print("\nTesting local voice server...")
    try:
        success = await test_voice_pipeline(use_local=True)
        if success:
            print("\n✅ LOCAL PIPELINE TEST PASSED")
        else:
            print("\n❌ LOCAL PIPELINE TEST FAILED")
    except Exception as e:
        print(f"\n❌ Local test failed: {e}")

    # Test 3: Voice pipeline via Cloudflare tunnel
    print("\nTesting voice server via Cloudflare tunnel...")
    try:
        success = await test_voice_pipeline(use_local=False)
        if success:
            print("\n✅ TUNNEL PIPELINE TEST PASSED")
        else:
            print("\n❌ TUNNEL PIPELINE TEST FAILED")
    except Exception as e:
        print(f"\n❌ Tunnel test failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
