#!/usr/bin/env python3
"""
Test Batch Integration - Verify Phase 2 batch multiplexer works

This script tests:
1. Batch multiplexer can submit concurrent requests
2. Message builders work correctly
3. Webhook batch helper integrates properly
"""

import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_batch_multiplexer():
    """Test the basic batch multiplexer."""
    print("=" * 70)
    print("TEST 1: Batch Multiplexer (Async)")
    print("=" * 70)
    
    try:
        from memory.llm_batch_multiplexer import batch_llm_requests, Priority
        
        requests = [
            {
                "id": "simple_math",
                "priority": Priority.CRITICAL_USER_RESPONSE,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "What is 2+2? (Be very brief)"}
                ],
                "max_tokens": 50
            },
            {
                "id": "simple_greeting",
                "priority": Priority.VISIBLE_CONTEXT,
                "messages": [
                    {"role": "system", "content": "You are friendly."},
                    {"role": "user", "content": "Say hello briefly."}
                ],
                "max_tokens": 30
            }
        ]
        
        print("\n📤 Submitting 2 concurrent requests...")
        loop = asyncio.get_event_loop()
        results = loop.run_until_complete(batch_llm_requests(requests))
        
        print(f"\n✅ Received {len(results)} results:")
        for req_id, result in results.items():
            if result.get("error"):
                print(f"  ❌ {req_id}: {result['error']}")
            else:
                print(f"  ✓ {req_id}: {result.get('latency_ms', 0):.0f}ms, {result.get('tokens', 0)} tokens")
                print(f"    Content: {result.get('content', '')[:80]}...")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Batch multiplexer test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_message_builders():
    """Test Section 2 and Section 8 message builders."""
    print("\n" + "=" * 70)
    print("TEST 2: Message Builders")
    print("=" * 70)
    
    try:
        from memory.webhook_message_builders import prepare_section_2_messages, prepare_section_8_messages
        
        # Test Section 2
        print("\n📝 Building Section 2 messages...")
        s2_system, s2_user = prepare_section_2_messages("deploy Django to production")
        
        if s2_system and s2_user:
            print(f"  ✓ Section 2: system={len(s2_system)} chars, user={len(s2_user)} chars")
            print(f"    System excerpt: {s2_system[:100]}...")
            print(f"    User excerpt: {s2_user[:100]}...")
        else:
            print(f"  ⚠️  Section 2: LLM offline or messages failed")
        
        # Test Section 8
        print("\n📝 Building Section 8 messages...")
        s8_system, s8_user = prepare_section_8_messages("optimize vLLM performance")
        
        if s8_system and s8_user:
            print(f"  ✓ Section 8: system={len(s8_system)} chars, user={len(s8_user)} chars")
            print(f"    System excerpt: {s8_system[:100]}...")
            print(f"    User excerpt: {s8_user[:100]}...")
        else:
            print(f"  ⚠️  Section 8: Messages failed")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Message builder test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_webhook_batch_helper():
    """Test the synchronous webhook batch helper."""
    print("\n" + "=" * 70)
    print("TEST 3: Webhook Batch Helper (Sync Wrapper)")
    print("=" * 70)
    
    try:
        from memory.webhook_batch_helper import batch_section_2_and_8_llm_calls
        
        # Prepare simple test messages
        s2_messages = [
            {"role": "system", "content": "You are a professor. Be brief."},
            {"role": "user", "content": "Explain continuous batching in 2 sentences. /think"}
        ]
        
        s8_messages = [
            {"role": "system", "content": "You are Synaptic. Be brief."},
            {"role": "user", "content": "Share one insight about AI optimization. /think"}
        ]
        
        print("\n📤 Batching S2 + S8 together...")
        s2_content, s8_content = batch_section_2_and_8_llm_calls(
            section_2_messages=s2_messages,
            section_8_messages=s8_messages,
            section_2_max_tokens=200,
            section_8_max_tokens=200
        )
        
        if s2_content:
            print(f"\n  ✓ Section 2 received: {len(s2_content)} chars")
            print(f"    Content: {s2_content[:150]}...")
        else:
            print(f"\n  ❌ Section 2 failed")
        
        if s8_content:
            print(f"\n  ✓ Section 8 received: {len(s8_content)} chars")
            print(f"    Content: {s8_content[:150]}...")
        else:
            print(f"\n  ❌ Section 8 failed")
        
        return s2_content is not None and s8_content is not None
        
    except Exception as e:
        print(f"\n❌ Webhook batch helper test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("\n🧪 PHASE 2 BATCH INTEGRATION TEST SUITE")
    print("=" * 70)
    
    # Check vLLM availability first
    try:
        import requests
        resp = requests.get("http://127.0.0.1:5044/v1/models", timeout=3)
        if resp.ok:
            print("✅ Local LLM server available")
        else:
            print("❌ Local LLM server not responding")
            print("   Start it with: ./scripts/start-llm.sh")
            return False
    except Exception as e:
        print(f"❌ Local LLM server not reachable: {e}")
        print("   Start it with: ./scripts/start-llm.sh")
        return False
    
    results = []
    
    # Run tests
    results.append(("Batch Multiplexer", test_batch_multiplexer()))
    results.append(("Message Builders", test_message_builders()))
    results.append(("Webhook Batch Helper", test_webhook_batch_helper()))
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}: {name}")
    
    print(f"\n{'='*70}")
    print(f"Result: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! Phase 2 integration ready.")
        return True
    else:
        print("⚠️  Some tests failed. Review errors above.")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
