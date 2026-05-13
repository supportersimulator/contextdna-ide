#!/usr/bin/env python3
"""
Test all 4 MCP resources for Context DNA webhook integration.

Tests:
1. All 4 resources generate correctly
2. Fresh generation on each read (timestamps differ)
3. Fallback behavior when services offline
4. Content validation for each resource

Resources tested:
- contextdna://webhook (full 9-section payload)
- contextdna://session-recovery (rehydration only)
- contextdna://professor (wisdom only)
- contextdna://8th-intelligence (Synaptic only)
"""

import asyncio
import sys
import time
import json
from pathlib import Path
from datetime import datetime

# Add repo root to path
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "mcp-servers"))

# Import the MCP server
from contextdna_webhook_mcp import ContextDNAWebhookMCP


class MCPResourceTester:
    """Test all MCP resources."""
    
    def __init__(self):
        self.server = ContextDNAWebhookMCP()
        self.results = {
            "passed": 0,
            "failed": 0,
            "warnings": 0,
            "tests": []
        }
    
    def log_test(self, name: str, status: str, details: str = ""):
        """Log a test result."""
        symbols = {
            "pass": "✅",
            "fail": "❌",
            "warn": "⚠️"
        }
        
        symbol = symbols.get(status, "❓")
        print(f"{symbol} {status.upper()}: {name}")
        if details:
            print(f"   {details}")
        
        self.results["tests"].append({
            "name": name,
            "status": status,
            "details": details,
            "timestamp": datetime.now().isoformat()
        })
        
        if status == "pass":
            self.results["passed"] += 1
        elif status == "fail":
            self.results["failed"] += 1
        elif status == "warn":
            self.results["warnings"] += 1
    
    async def test_resource_list(self):
        """Test that all 4 resources are listed."""
        print("\n━━━ Test 1: Resource Listing ━━━\n")
        
        try:
            resources = await self.server.list_resources()
            resource_list = resources.get("resources", [])
            
            expected_uris = [
                "contextdna://webhook",
                "contextdna://session-recovery",
                "contextdna://professor",
                "contextdna://8th-intelligence"
            ]
            
            found_uris = [r["uri"] for r in resource_list]
            
            for uri in expected_uris:
                if uri in found_uris:
                    self.log_test(f"Resource listed: {uri}", "pass")
                else:
                    self.log_test(f"Resource listed: {uri}", "fail", "Not in resource list")
            
            # Check total count
            if len(resource_list) == 4:
                self.log_test("All 4 resources present", "pass")
            else:
                self.log_test("All 4 resources present", "fail", 
                            f"Found {len(resource_list)}, expected 4")
        
        except Exception as e:
            self.log_test("Resource listing", "fail", str(e))
    
    async def test_webhook_resource(self):
        """Test full webhook resource."""
        print("\n━━━ Test 2: Full Webhook Resource ━━━\n")
        
        try:
            result = await self.server.read_resource("contextdna://webhook")
            
            if "error" in result:
                self.log_test("Webhook generation", "fail", result["error"]["message"])
                return
            
            content = result["contents"][0]["text"]
            
            # Check content exists and has reasonable size
            if len(content) > 100:
                self.log_test("Webhook content generated", "pass", 
                            f"{len(content)} characters")
            else:
                self.log_test("Webhook content generated", "fail", 
                            f"Only {len(content)} characters")
            
            # Check for key sections (if not in fallback mode)
            if "Fallback Mode" in content:
                self.log_test("Webhook sections", "warn", 
                            "Running in fallback mode")
            else:
                # Check for section markers
                section_markers = [
                    "SECTION 0: SAFETY",
                    "SECTION 1: FOUNDATION",
                    "SECTION 8: 8TH INTELLIGENCE"
                ]
                
                found_sections = sum(1 for marker in section_markers if marker in content)
                
                if found_sections >= 2:
                    self.log_test("Webhook sections present", "pass", 
                                f"{found_sections}/3 key sections found")
                else:
                    self.log_test("Webhook sections present", "warn", 
                                f"Only {found_sections}/3 key sections found")
        
        except Exception as e:
            self.log_test("Webhook resource", "fail", str(e))
    
    async def test_session_recovery(self):
        """Test session recovery resource."""
        print("\n━━━ Test 3: Session Recovery Resource ━━━\n")
        
        try:
            result = await self.server.read_resource("contextdna://session-recovery")
            
            if "error" in result:
                self.log_test("Session recovery generation", "fail", 
                            result["error"]["message"])
                return
            
            content = result["contents"][0]["text"]
            
            # Check content exists
            if len(content) > 50:
                self.log_test("Session recovery content", "pass", 
                            f"{len(content)} characters")
            else:
                self.log_test("Session recovery content", "fail", 
                            f"Only {len(content)} characters")
            
            # Check for expected markers
            if "SESSION CRASH RECOVERY" in content or "No archived sessions" in content:
                self.log_test("Session recovery format", "pass")
            else:
                self.log_test("Session recovery format", "fail", 
                            "Missing expected headers")
            
            # Check for rehydration command
            if "session_historian.py rehydrate" in content:
                self.log_test("Rehydration command present", "pass")
            else:
                self.log_test("Rehydration command present", "warn", 
                            "Command not found in content")
        
        except Exception as e:
            self.log_test("Session recovery resource", "fail", str(e))
    
    async def test_professor_wisdom(self):
        """Test Professor wisdom resource."""
        print("\n━━━ Test 4: Professor Wisdom Resource ━━━\n")
        
        try:
            result = await self.server.read_resource("contextdna://professor")
            
            if "error" in result:
                self.log_test("Professor wisdom generation", "fail", 
                            result["error"]["message"])
                return
            
            content = result["contents"][0]["text"]
            
            # Check content exists
            if len(content) > 20:
                self.log_test("Professor wisdom content", "pass", 
                            f"{len(content)} characters")
            else:
                self.log_test("Professor wisdom content", "fail", 
                            f"Only {len(content)} characters")
            
            # Check if Professor is available or unavailable
            if "unavailable" in content.lower():
                self.log_test("Professor service status", "warn", 
                            "Professor service offline (expected if not running)")
            else:
                self.log_test("Professor service status", "pass", 
                            "Professor generated wisdom")
        
        except Exception as e:
            self.log_test("Professor wisdom resource", "fail", str(e))
    
    async def test_8th_intelligence(self):
        """Test 8th Intelligence resource."""
        print("\n━━━ Test 5: 8th Intelligence Resource ━━━\n")
        
        try:
            result = await self.server.read_resource("contextdna://8th-intelligence")
            
            if "error" in result:
                self.log_test("8th Intelligence generation", "fail", 
                            result["error"]["message"])
                return
            
            content = result["contents"][0]["text"]
            
            # Check content exists
            if len(content) > 20:
                self.log_test("8th Intelligence content", "pass", 
                            f"{len(content)} characters")
            else:
                self.log_test("8th Intelligence content", "fail", 
                            f"Only {len(content)} characters")
            
            # Check for Synaptic markers
            if "[START: Synaptic to Aaron]" in content or "8th Intelligence" in content:
                self.log_test("8th Intelligence format", "pass", 
                            "Synaptic voice markers present")
            else:
                self.log_test("8th Intelligence format", "warn", 
                            "Missing expected Synaptic markers")
            
            # Check if service is available
            if "unavailable" in content.lower() or "listening" in content.lower():
                self.log_test("Synaptic service status", "warn", 
                            "Synaptic in passive mode (normal if no active context)")
            else:
                self.log_test("Synaptic service status", "pass", 
                            "Synaptic generated perspective")
        
        except Exception as e:
            self.log_test("8th Intelligence resource", "fail", str(e))
    
    async def test_freshness(self):
        """Test that resources generate fresh content each time."""
        print("\n━━━ Test 6: Freshness Verification ━━━\n")
        
        for uri in ["contextdna://webhook", "contextdna://session-recovery"]:
            try:
                # Read same resource 3 times with delays
                contents = []
                timestamps = []
                
                for i in range(3):
                    result = await self.server.read_resource(uri)
                    
                    if "error" not in result:
                        content = result["contents"][0]["text"]
                        contents.append(content)
                        timestamps.append(time.time())
                    
                    if i < 2:  # Don't sleep after last iteration
                        await asyncio.sleep(1)
                
                if len(contents) == 3:
                    # Check if timestamps in content differ
                    # (Look for "Generated:" or timestamp patterns)
                    unique_contents = len(set(contents))
                    
                    if unique_contents >= 2:
                        self.log_test(f"Freshness: {uri}", "pass", 
                                    f"{unique_contents}/3 unique generations")
                    else:
                        self.log_test(f"Freshness: {uri}", "warn", 
                                    "Content appears identical (may be cached)")
                    
                    # Check time deltas
                    time_delta = timestamps[-1] - timestamps[0]
                    self.log_test(f"Generation time: {uri}", "pass", 
                                f"{time_delta:.1f}s for 3 generations")
                else:
                    self.log_test(f"Freshness: {uri}", "fail", 
                                f"Only generated {len(contents)}/3 times")
            
            except Exception as e:
                self.log_test(f"Freshness: {uri}", "fail", str(e))
    
    async def test_fallback_behavior(self):
        """Test fallback when services are offline."""
        print("\n━━━ Test 7: Fallback Behavior ━━━\n")
        
        # Test with invalid URI (should return error)
        try:
            result = await self.server.read_resource("contextdna://invalid")
            
            if "error" in result:
                if result["error"]["code"] == -32602:
                    self.log_test("Invalid URI handling", "pass", 
                                "Proper error code returned")
                else:
                    self.log_test("Invalid URI handling", "warn", 
                                f"Error code: {result['error']['code']}")
            else:
                self.log_test("Invalid URI handling", "fail", 
                            "Should have returned error for invalid URI")
        
        except Exception as e:
            self.log_test("Invalid URI handling", "fail", str(e))
        
        # Test webhook fallback (when full generation fails)
        try:
            fallback = await self.server._generate_fallback_context()
            
            if len(fallback) > 50:
                self.log_test("Fallback context generation", "pass", 
                            f"{len(fallback)} characters")
            else:
                self.log_test("Fallback context generation", "fail", 
                            "Fallback too short")
            
            # Check fallback has essential info
            if "session_historian.py rehydrate" in fallback:
                self.log_test("Fallback has recovery command", "pass")
            else:
                self.log_test("Fallback has recovery command", "fail")
        
        except Exception as e:
            self.log_test("Fallback generation", "fail", str(e))
    
    async def test_mcp_protocol(self):
        """Test MCP protocol message handling."""
        print("\n━━━ Test 8: MCP Protocol Compliance ━━━\n")
        
        # Test initialize message
        try:
            init_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {}
            }
            
            response = await self.server.handle_message(init_msg)
            
            if response.get("result", {}).get("protocolVersion"):
                self.log_test("MCP initialize", "pass", 
                            f"Protocol: {response['result']['protocolVersion']}")
            else:
                self.log_test("MCP initialize", "fail", 
                            "No protocol version in response")
            
            # Check server info
            server_info = response.get("result", {}).get("serverInfo", {})
            if server_info.get("name") and server_info.get("version"):
                self.log_test("MCP server info", "pass", 
                            f"{server_info['name']} v{server_info['version']}")
            else:
                self.log_test("MCP server info", "fail", 
                            "Missing name or version")
        
        except Exception as e:
            self.log_test("MCP protocol", "fail", str(e))
        
        # Test resources/list
        try:
            list_msg = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/list",
                "params": {}
            }
            
            response = await self.server.handle_message(list_msg)
            
            resources = response.get("result", {}).get("resources", [])
            if len(resources) == 4:
                self.log_test("MCP resources/list", "pass", 
                            f"{len(resources)} resources")
            else:
                self.log_test("MCP resources/list", "fail", 
                            f"Expected 4, got {len(resources)}")
        
        except Exception as e:
            self.log_test("MCP resources/list", "fail", str(e))
        
        # Test resources/read
        try:
            read_msg = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": "contextdna://webhook"}
            }
            
            response = await self.server.handle_message(read_msg)
            
            if "result" in response and "contents" in response["result"]:
                self.log_test("MCP resources/read", "pass", 
                            "Successfully read resource")
            else:
                self.log_test("MCP resources/read", "fail", 
                            "No contents in response")
        
        except Exception as e:
            self.log_test("MCP resources/read", "fail", str(e))
    
    async def run_all_tests(self):
        """Run all validation tests."""
        print("🧬 Context DNA MCP Resource Validation")
        print("=" * 60)
        print()
        
        start_time = time.time()
        
        # Run all test suites
        await self.test_resource_list()
        await self.test_webhook_resource()
        await self.test_session_recovery()
        await self.test_professor_wisdom()
        await self.test_8th_intelligence()
        await self.test_freshness()
        await self.test_fallback_behavior()
        await self.test_mcp_protocol()
        
        elapsed = time.time() - start_time
        
        # Print summary
        print("\n" + "=" * 60)
        print("\n📊 TEST SUMMARY")
        print(f"\n  ✅ Passed:   {self.results['passed']}")
        print(f"  ❌ Failed:   {self.results['failed']}")
        print(f"  ⚠️  Warnings: {self.results['warnings']}")
        print(f"\n  ⏱️  Duration: {elapsed:.1f}s")
        print()
        
        # Save detailed results
        results_file = REPO_ROOT / "scripts" / "mcp-test-results.json"
        with open(results_file, "w") as f:
            json.dump(self.results, f, indent=2)
        
        print(f"📄 Detailed results: {results_file}")
        print()
        
        # Exit code
        if self.results['failed'] == 0:
            print("✅ All tests passed!")
            return 0
        else:
            print("❌ Some tests failed. Review output above.")
            return 1


async def main():
    """Main test runner."""
    tester = MCPResourceTester()
    exit_code = await tester.run_all_tests()
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
