"""
Fleet Nerve Discovery — mDNS/DNS-SD peer discovery.

Registers this node as _fleet-nerve._tcp.local via zeroconf (if available).
Continuously browses for other fleet nodes. Merges with static config peers.
Graceful degradation: if zeroconf not installed, logs warning and skips mDNS.

SECURITY: Never log IP addresses with credentials. Only log node IDs.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("fleet.discovery")

REPO_ROOT = Path(__file__).parent.parent
DISCOVERY_CACHE = REPO_ROOT / ".multifleet" / "discovered_peers.json"
SERVICE_TYPE = "_fleet-nerve._tcp.local."
EVICTION_TIMEOUT_S = 30 * 60  # 30 minutes


class FleetDiscovery:
    """mDNS/DNS-SD peer discovery for fleet nodes.

    On startup: registers this node via mDNS, browses for peers.
    Maintains discovered_peers dict merged with static config.
    Falls back to config-only mode if zeroconf unavailable.
    """

    def __init__(self, node_id: str, port: int = 8855, ip: Optional[str] = None):
        self.node_id = node_id
        self.port = port
        self.ip = ip or self._get_local_ip()
        self.discovered_peers: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._zeroconf = None
        self._browser = None
        self._service_info = None
        self._running = False
        self._eviction_thread: Optional[threading.Thread] = None
        self._mdns_available = False

        # Load cached discoveries from prior runs
        self._load_cache()

    def start(self):
        """Start mDNS registration and browsing. No-op if zeroconf unavailable."""
        self._running = True

        # Start eviction thread regardless of mDNS availability
        self._eviction_thread = threading.Thread(
            target=self._eviction_loop, daemon=True, name="fleet-discovery-evict"
        )
        self._eviction_thread.start()

        try:
            from zeroconf import Zeroconf, ServiceBrowser, ServiceInfo
            self._mdns_available = True
        except ImportError:
            logger.warning(
                "zeroconf not installed — mDNS discovery disabled. "
                "Install with: pip install zeroconf. Config-only mode active."
            )
            return

        try:
            import socket
            self._zeroconf = Zeroconf()

            # Register this node
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                f"{self.node_id}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(self.ip)] if self.ip else [],
                port=self.port,
                properties={
                    b"node_id": self.node_id.encode(),
                    b"version": b"1",
                },
            )
            self._zeroconf.register_service(self._service_info)
            logger.info(f"mDNS: registered node {self.node_id} on port {self.port}")

            # Browse for peers
            self._browser = ServiceBrowser(
                self._zeroconf, SERVICE_TYPE, handlers=[self._on_service_change]
            )
            logger.info("mDNS: browsing for fleet peers")

        except Exception as e:
            logger.error(f"mDNS startup failed: {e} — falling back to config-only")
            self._mdns_available = False
            self._cleanup_zeroconf()

    def stop(self):
        """Unregister mDNS and stop browsing."""
        self._running = False
        self._cleanup_zeroconf()
        self._save_cache()
        logger.info("Discovery stopped")

    def _cleanup_zeroconf(self):
        """Safely tear down zeroconf resources."""
        if self._zeroconf:
            try:
                if self._service_info:
                    self._zeroconf.unregister_service(self._service_info)
                self._zeroconf.close()
            except Exception as e:
                logger.warning(f"mDNS cleanup error: {e}")
            self._zeroconf = None
            self._browser = None
            self._service_info = None

    def _on_service_change(self, zeroconf, service_type, name, state_change):
        """Callback for mDNS service changes (add/remove/update)."""
        try:
            from zeroconf import ServiceStateChange
        except ImportError:
            return

        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            info = zeroconf.get_service_info(service_type, name)
            if not info:
                return

            props = {
                k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v
                for k, v in (info.properties or {}).items()
            }
            peer_node_id = props.get("node_id", "")
            if not peer_node_id or peer_node_id == self.node_id:
                return

            import socket
            addresses = info.parsed_addresses()
            peer_ip = addresses[0] if addresses else ""
            peer_port = info.port or 8855

            with self._lock:
                existing = self.discovered_peers.get(peer_node_id, {})
                # Don't overwrite config-sourced entries
                if existing.get("source") == "config":
                    # Just refresh last_seen
                    existing["last_seen"] = time.time()
                    existing["mdns_seen"] = time.time()
                else:
                    self.discovered_peers[peer_node_id] = {
                        "ip": peer_ip,
                        "port": peer_port,
                        "last_seen": time.time(),
                        "source": "mdns",
                        "mdns_seen": time.time(),
                    }
            logger.info(f"mDNS: discovered peer {peer_node_id}")

        elif state_change == ServiceStateChange.Removed:
            # Extract node_id from service name: "{node_id}._fleet-nerve._tcp.local."
            peer_node_id = name.replace(f".{SERVICE_TYPE}", "").strip(".")
            if peer_node_id and peer_node_id != self.node_id:
                with self._lock:
                    existing = self.discovered_peers.get(peer_node_id)
                    if existing and existing.get("source") == "mdns":
                        # Don't remove immediately — let eviction handle it
                        # Just note that mDNS no longer sees it
                        existing.pop("mdns_seen", None)
                logger.info(f"mDNS: peer {peer_node_id} service removed")

    def add_nats_peer(self, node_id: str, ip: str = "", port: int = 8855):
        """Add or refresh a peer discovered via NATS heartbeat."""
        if node_id == self.node_id:
            return
        with self._lock:
            existing = self.discovered_peers.get(node_id)
            if existing:
                existing["last_seen"] = time.time()
                existing["nats_seen"] = time.time()
                # Fill in IP/port if we didn't have them
                if ip and not existing.get("ip"):
                    existing["ip"] = ip
                if port and existing.get("port", 8855) == 8855:
                    existing["port"] = port
            else:
                self.discovered_peers[node_id] = {
                    "ip": ip,
                    "port": port,
                    "last_seen": time.time(),
                    "source": "nats",
                    "nats_seen": time.time(),
                }
            logger.debug(f"NATS peer refreshed: {node_id}")

    def merge_config_peers(self, config_peers: dict):
        """Merge static config peers into discovered_peers. Config takes priority."""
        with self._lock:
            for node_id, peer_info in config_peers.items():
                if node_id == self.node_id:
                    continue
                existing = self.discovered_peers.get(node_id, {})
                self.discovered_peers[node_id] = {
                    "ip": peer_info.get("ip", existing.get("ip", "")),
                    "port": peer_info.get("port", existing.get("port", 8855)),
                    "user": peer_info.get("user", existing.get("user", "")),
                    "role": peer_info.get("role", existing.get("role", "")),
                    "mac_address": peer_info.get("mac_address", existing.get("mac_address", "")),
                    "last_seen": time.time(),
                    "source": "config",
                    # Preserve sub-source timestamps if they existed
                    **({"mdns_seen": existing["mdns_seen"]} if "mdns_seen" in existing else {}),
                    **({"nats_seen": existing["nats_seen"]} if "nats_seen" in existing else {}),
                }

    def get_all_peers(self) -> dict:
        """Return all discovered peers (thread-safe copy)."""
        with self._lock:
            return {k: dict(v) for k, v in self.discovered_peers.items()}

    def get_peers_for_config(self) -> dict:
        """Return peers in config-compatible format (ip, port, user)."""
        with self._lock:
            result = {}
            for node_id, info in self.discovered_peers.items():
                result[node_id] = {
                    "ip": info.get("ip", ""),
                    "port": info.get("port", 8855),
                    "user": info.get("user", ""),
                    "role": info.get("role", ""),
                    "mac_address": info.get("mac_address", ""),
                }
            return result

    def _eviction_loop(self):
        """Evict peers not seen via any source for >30 minutes."""
        while self._running:
            time.sleep(60)  # Check every minute
            now = time.time()
            evicted = []
            with self._lock:
                for node_id, info in list(self.discovered_peers.items()):
                    # Config peers are never evicted
                    if info.get("source") == "config":
                        continue
                    last_seen = info.get("last_seen", 0)
                    if now - last_seen > EVICTION_TIMEOUT_S:
                        evicted.append(node_id)
                        del self.discovered_peers[node_id]

            for nid in evicted:
                logger.info(f"Evicted stale peer: {nid} (not seen for >{EVICTION_TIMEOUT_S // 60}min)")

            # Periodically save cache
            if evicted or int(now) % 300 < 60:  # ~every 5 min
                self._save_cache()

    def _save_cache(self):
        """Persist discovered peers to disk for faster startup."""
        try:
            DISCOVERY_CACHE.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                # Only cache non-config peers (config reloads from its own source)
                cacheable = {
                    nid: info for nid, info in self.discovered_peers.items()
                    if info.get("source") != "config"
                }
            DISCOVERY_CACHE.write_text(json.dumps(cacheable, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save discovery cache: {e}")

    def _load_cache(self):
        """Load previously discovered peers from cache."""
        if not DISCOVERY_CACHE.exists():
            return
        try:
            cached = json.loads(DISCOVERY_CACHE.read_text())
            now = time.time()
            loaded = 0
            for node_id, info in cached.items():
                last_seen = info.get("last_seen", 0)
                # Only load peers seen within eviction window
                if now - last_seen <= EVICTION_TIMEOUT_S:
                    self.discovered_peers[node_id] = info
                    loaded += 1
            if loaded:
                logger.info(f"Loaded {loaded} cached peers from prior discovery")
        except Exception as e:
            logger.warning(f"Failed to load discovery cache: {e}")

    @staticmethod
    def _get_local_ip() -> Optional[str]:
        """Get the machine's LAN IP."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception as e:
            logger.warning(f"Failed to detect local IP: {e}")
            return None

    @property
    def is_mdns_active(self) -> bool:
        return self._mdns_available and self._zeroconf is not None
