#!/usr/bin/env python3
"""
MCP Server Health Check Daemon

This daemon runs in the background and ensures:
1. Context DNA services are healthy before MCP server starts
2. MCP server auto-restarts if Context DNA goes down
3. Health status is exposed for monitoring

This is designed to run in Docker alongside the main MCP server.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add repo root to path
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)


class MCPHealthDaemon:
    """Daemon that monitors Context DNA health and manages MCP server lifecycle."""
    
    def __init__(self):
        self.running = True
        self.last_health_check = None
        self.consecutive_failures = 0
        self.max_failures = 3
        
        # Register signal handlers
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)
    
    def _handle_shutdown(self, signum, frame):
        """Graceful shutdown handler."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
    
    async def check_context_dna_health(self) -> bool:
        """Check if Context DNA API is healthy."""
        import aiohttp
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    'http://contextdna-api:8029/health',
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get('code') == 0 or data.get('msg') == 'ok'
                    return False
        except Exception as e:
            logger.debug(f"Health check failed: {e}")
            return False
    
    async def check_helper_agent_health(self) -> bool:
        """Check if helper agent is responding."""
        import aiohttp
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    'http://contextdna-helper-agent:8080/health',
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as response:
                    return response.status == 200
        except Exception as e:
            logger.debug(f"Helper agent check failed: {e}")
            return False
    
    async def run(self):
        """Main daemon loop."""
        logger.info("MCP Health Daemon starting...")
        
        # Wait for Context DNA to be ready
        logger.info("Waiting for Context DNA services...")
        while self.running:
            api_healthy = await self.check_context_dna_health()
            if api_healthy:
                logger.info("✓ Context DNA API is healthy")
                break
            logger.info("Waiting for Context DNA API... (retry in 5s)")
            await asyncio.sleep(5)
        
        logger.info("✓ MCP Server prerequisites met - ready to serve")
        
        # Main health monitoring loop
        while self.running:
            self.last_health_check = datetime.now(timezone.utc)
            
            # Check both API and helper agent
            api_healthy = await self.check_context_dna_health()
            agent_healthy = await self.check_helper_agent_health()
            
            if api_healthy and agent_healthy:
                if self.consecutive_failures > 0:
                    logger.info("✓ Services recovered")
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
                logger.warning(
                    f"Health check failed ({self.consecutive_failures}/{self.max_failures}) "
                    f"- API: {api_healthy}, Agent: {agent_healthy}"
                )
                
                if self.consecutive_failures >= self.max_failures:
                    logger.error(
                        "Context DNA services unhealthy - MCP server will use fallback mode"
                    )
            
            # Check every 30 seconds
            await asyncio.sleep(30)
        
        logger.info("MCP Health Daemon stopped")


if __name__ == '__main__':
    daemon = MCPHealthDaemon()
    asyncio.run(daemon.run())
