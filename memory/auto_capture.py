#!/usr/bin/env python3
"""
Automatic Architecture Capture - Learn from ALL Agent Activity

This module captures architecture knowledge from agent activity in real-time,
not just git commits. It hooks into common operations and extracts learnings.

CAPTURE SOURCES:
1. Bash command execution (SSH, AWS CLI, Docker, Terraform)
2. File modifications (infrastructure configs)
3. API responses (AWS, cloud providers)
4. Successful task completions
5. Error resolutions

INTEGRATION:
This module is designed to be called automatically by the agent framework.
It inspects commands/outputs and extracts architecture details.

Usage (called automatically by agent):
    from memory.auto_capture import capture_command, capture_file_change, capture_success, capture_failure

    # After running a bash command
    capture_command("ssh ubuntu@10.0.1.5 'systemctl status gunicorn'", output, exit_code)

    # After modifying infrastructure
    capture_file_change("infra/aws/terraform/main.tf", old_content, new_content)

    # After successful task
    capture_success("Deployed Django update", details="Restarted gunicorn, verified /api/health")
"""

import os
import sys
import re
import json
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import memory components
try:
    from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
    CONTEXT_DNA_AVAILABLE = True
except ImportError:
    CONTEXT_DNA_AVAILABLE = False

try:
    from memory.bugfix_sop_enhancer import (
        enhance_capture, extract_key_insight, generate_bugfix_sop_title,
        SOPDeduplicator, ArtifactDeduplicator
    )
    BUGFIX_ENHANCER_AVAILABLE = True
except ImportError:
    BUGFIX_ENHANCER_AVAILABLE = False

try:
    from memory.process_sop_enhancer import generate_process_sop_title
    PROCESS_ENHANCER_AVAILABLE = True
except ImportError:
    PROCESS_ENHANCER_AVAILABLE = False

try:
    from memory.sop_title_router import generate_sop_title
    SOP_ROUTER_AVAILABLE = True
except ImportError:
    SOP_ROUTER_AVAILABLE = False

# Combined flag for backwards compatibility
SOP_ENHANCER_AVAILABLE = BUGFIX_ENHANCER_AVAILABLE

try:
    from memory.artifact_store import ArtifactStore, sanitize_secrets
    ARTIFACT_STORE_AVAILABLE = True
except ImportError:
    ARTIFACT_STORE_AVAILABLE = False

try:
    from memory.route_tracker import record_route_success, record_route_failure
    ROUTE_TRACKER_AVAILABLE = True
except ImportError:
    ROUTE_TRACKER_AVAILABLE = False

try:
    from memory.knowledge_graph import KnowledgeGraph
    KNOWLEDGE_GRAPH_AVAILABLE = True
except ImportError:
    KNOWLEDGE_GRAPH_AVAILABLE = False

# Import work log for mirroring all activity
try:
    from memory.architecture_enhancer import work_log
    WORK_LOG_AVAILABLE = True
except ImportError:
    WORK_LOG_AVAILABLE = False


# =============================================================================
# FAILURE TRACKING - Health-monitored error recording for silent failure points
# =============================================================================

def _resolve_hook_variant_id(session_id: str) -> str:
    """
    Resolve the actual hook variant_id from hook_firings for a session.

    Returns actual variant_id or 'userpromptsubmit_default' as fallback.
    """
    try:
        from memory.hook_evolution import get_hook_evolution_engine
        engine = get_hook_evolution_engine()
        cursor = engine.db.execute(
            "SELECT variant_id FROM hook_firings WHERE session_id = ? ORDER BY fired_at DESC LIMIT 1",
            (session_id,))
        row = cursor.fetchone()
        if row:
            return row[0]
        variant, _ = engine.get_active_variant("UserPromptSubmit", session_id)
        if variant:
            return variant.variant_id
    except Exception:
        pass
    return "userpromptsubmit_default"


_FAILURE_STATE_FILE = Path(__file__).parent / ".auto_capture_failures.json"
_FAILURE_THRESHOLD = 5          # failures per component in window before notifying
_FAILURE_WINDOW_SECONDS = 600   # 10-minute window
_NOTIFICATION_COOLDOWN = 900    # 15-minute cooldown between notifications


def _record_capture_failure(component: str, error: str):
    """Record a non-blocking capture failure with threshold-based macOS notification."""
    try:
        now = datetime.now()
        now_ts = now.timestamp()
        state = {}
        if _FAILURE_STATE_FILE.exists():
            try:
                with open(_FAILURE_STATE_FILE) as f:
                    state = json.load(f)
            except Exception:
                state = {}

        if component not in state:
            state[component] = {"failures": [], "last_notification_ts": 0, "total_failures": 0}

        comp = state[component]
        comp["failures"].append({"ts": now_ts, "error": error[:200], "at": now.isoformat()})
        comp["total_failures"] = comp.get("total_failures", 0) + 1

        # Prune outside window
        window_start = now_ts - _FAILURE_WINDOW_SECONDS
        comp["failures"] = [f for f in comp["failures"] if f["ts"] >= window_start]

        recent_count = len(comp["failures"])
        cooldown_elapsed = (now_ts - comp.get("last_notification_ts", 0)) >= _NOTIFICATION_COOLDOWN

        if recent_count >= _FAILURE_THRESHOLD and cooldown_elapsed:
            comp["last_notification_ts"] = now_ts
            try:
                msg = f"{component}: {recent_count} failures in {_FAILURE_WINDOW_SECONDS // 60}min"
                subprocess.run(
                    ["osascript", "-e",
                     f'display notification "{msg}" with title "Context DNA: auto_capture"'],
                    timeout=5, capture_output=True,
                )
            except Exception as e:
                print(f"[WARN] Notification for capture failure failed: {e}")

        with open(_FAILURE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f"[WARN] _record_capture_failure itself failed (meta-safety): {e}")


# =============================================================================
# PATTERN DETECTION
# =============================================================================

# Commands that reveal architecture
ARCHITECTURE_COMMANDS = [
    # AWS
    (r"aws\s+ec2\s+describe", "aws", "EC2 instance details"),
    (r"aws\s+ecs\s+", "ecs", "ECS service/task details"),
    (r"aws\s+lambda\s+", "lambda", "Lambda function details"),
    (r"aws\s+rds\s+", "rds", "Database details"),
    (r"aws\s+elb|aws\s+elbv2", "networking", "Load balancer details"),

    # SSH/Remote
    (r"ssh\s+", "deployment", "Server access pattern"),
    (r"scp\s+", "deployment", "File transfer pattern"),

    # Docker
    (r"docker\s+ps", "docker", "Running containers"),
    (r"docker\s+logs", "docker", "Container logs"),
    (r"docker\s+inspect", "docker", "Container details"),
    (r"docker-compose|docker\s+compose", "docker", "Compose configuration"),

    # Terraform
    (r"terraform\s+plan", "terraform", "Infrastructure plan"),
    (r"terraform\s+apply", "terraform", "Infrastructure change"),
    (r"terraform\s+state", "terraform", "State details"),

    # System
    (r"systemctl\s+status", "deployment", "Service status"),
    (r"systemctl\s+restart", "deployment", "Service restart"),
    (r"journalctl", "deployment", "Service logs"),
    (r"nginx\s+-t|nginx.*reload", "networking", "Nginx configuration"),

    # Kubernetes
    (r"kubectl\s+get", "kubernetes", "K8s resources"),
    (r"kubectl\s+describe", "kubernetes", "K8s details"),
]

# Patterns that indicate successful operations
SUCCESS_PATTERNS = [
    r"active \(running\)",  # systemctl
    r"healthy",  # health checks
    r"running",  # docker ps
    r"Apply complete",  # terraform
    r"Successfully",
    r"deployed",
    r"restarted",
    r"200 OK",
    r"PASSED",
]

# Patterns that indicate architecture details in output
ARCHITECTURE_PATTERNS = [
    (r"i-[0-9a-f]{8,17}", "instance_id", "EC2 Instance ID"),
    (r"arn:aws:[^\s]+", "arn", "AWS Resource ARN"),
    (r"vpc-[0-9a-f]+", "vpc_id", "VPC ID"),
    (r"subnet-[0-9a-f]+", "subnet_id", "Subnet ID"),
    (r"sg-[0-9a-f]+", "security_group", "Security Group"),
    (r"ami-[0-9a-f]+", "ami_id", "AMI ID"),
    (r"(\d{1,3}\.){3}\d{1,3}:\d+", "endpoint", "Service Endpoint"),
    (r"port\s*[=:]\s*(\d+)", "port", "Port Configuration"),
]


# =============================================================================
# CAPTURE STATE
# =============================================================================

class CaptureState:
    """Tracks capture state across operations."""

    STATE_FILE = Path(__file__).parent / ".auto_capture_state.json"

    def __init__(self):
        self.state = self._load()

    def _load(self) -> dict:
        if self.STATE_FILE.exists():
            try:
                with open(self.STATE_FILE) as f:
                    return json.load(f)
            except Exception as e:
                _record_capture_failure("state_load", str(e))
        return {
            "captures_today": 0,
            "last_capture": None,
            "recent_commands": [],
            "discovered_resources": {}
        }

    def _save(self):
        with open(self.STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    def record_capture(self, capture_type: str, details: str):
        self.state["captures_today"] += 1
        self.state["last_capture"] = datetime.now().isoformat()
        self.state["recent_commands"].append({
            "type": capture_type,
            "details": details[:100],
            "at": datetime.now().isoformat()
        })
        # Keep only last 50
        self.state["recent_commands"] = self.state["recent_commands"][-50:]
        self._save()

    def record_resource(self, resource_type: str, resource_id: str, details: str):
        if resource_type not in self.state["discovered_resources"]:
            self.state["discovered_resources"][resource_type] = {}
        self.state["discovered_resources"][resource_type][resource_id] = {
            "details": details,
            "discovered_at": datetime.now().isoformat()
        }
        self._save()


_state = CaptureState()


# =============================================================================
# CAPTURE FUNCTIONS
# =============================================================================

def capture_command(command: str, output: str, exit_code: int = 0) -> Optional[str]:
    """
    Capture architecture knowledge from a bash command execution.

    Called automatically after bash commands that may reveal architecture.

    Args:
        command: The command that was executed
        output: stdout/stderr from the command
        exit_code: Command exit code

    Returns:
        Session ID if captured, None otherwise
    """
    if not CONTEXT_DNA_AVAILABLE:
        return None

    # Check if this command reveals architecture
    cmd_lower = command.lower()
    matched_area = None
    matched_desc = None

    for pattern, area, desc in ARCHITECTURE_COMMANDS:
        if re.search(pattern, cmd_lower):
            matched_area = area
            matched_desc = desc
            break

    if not matched_area:
        return None

    # Check if successful
    is_success = exit_code == 0 or any(re.search(p, output, re.I) for p in SUCCESS_PATTERNS)

    if not is_success:
        return None

    # Extract architecture details from output
    discovered = []
    for pattern, resource_type, desc in ARCHITECTURE_PATTERNS:
        matches = re.findall(pattern, output)
        for match in matches[:3]:  # Limit per pattern
            discovered.append({
                "type": resource_type,
                "value": match if isinstance(match, str) else match[0],
                "description": desc
            })
            _state.record_resource(resource_type, match if isinstance(match, str) else match[0], desc)

    # Build learning content
    content = f"""## Command Execution: {matched_desc}

**Command:** `{sanitize_secrets(command) if ARTIFACT_STORE_AVAILABLE else command}`
**Area:** {matched_area}
**Status:** {'Success' if is_success else 'Failed'}
**Captured:** {datetime.now().isoformat()}

"""
    if discovered:
        content += "### Discovered Resources:\n"
        for d in discovered:
            content += f"- {d['description']}: `{d['value']}`\n"

    # Sanitize and add relevant output
    safe_output = sanitize_secrets(output[:500]) if ARTIFACT_STORE_AVAILABLE else output[:500]
    if safe_output.strip():
        content += f"\n### Output Preview:\n```\n{safe_output}\n```\n"

    # ALSO log to work dialogue (for local LLM analysis)
    if WORK_LOG_AVAILABLE:
        try:
            work_log.log_command(
                command=sanitize_secrets(command) if ARTIFACT_STORE_AVAILABLE else command,
                output=safe_output,
                exit_code=exit_code
            )
        except Exception as e:
            _record_capture_failure("work_log", str(e))

    # Record to Context DNA
    try:
        memory = ContextDNAClient()
        session_id = memory.record_architecture_decision(
            decision=f"[AUTO-CAPTURE] {matched_desc}",
            rationale=content,
            alternatives=None,
            consequences=f"Area: {matched_area}, Auto-captured from command execution"
        )
        _state.record_capture("command", f"{matched_area}: {command[:50]}")
        return session_id
    except Exception as e:
        _record_capture_failure("context_dna", str(e))
        return None


def capture_file_change(file_path: str, old_content: str, new_content: str) -> Optional[str]:
    """
    Capture architecture knowledge from file modifications.

    Called automatically when infrastructure files are modified.

    Args:
        file_path: Path to the modified file
        old_content: Content before modification (can be empty for new files)
        new_content: Content after modification

    Returns:
        Session ID if captured, None otherwise
    """
    if not CONTEXT_DNA_AVAILABLE:
        return None

    # Check if this is an infrastructure file
    file_lower = file_path.lower()
    infra_patterns = [
        (".tf", "terraform"),
        ("dockerfile", "docker"),
        ("docker-compose", "docker"),
        (".yml", "config"),
        (".yaml", "config"),
        ("nginx", "networking"),
        (".service", "systemd"),
        ("gunicorn", "deployment"),
    ]

    matched_area = None
    for pattern, area in infra_patterns:
        if pattern in file_lower:
            matched_area = area
            break

    if not matched_area:
        return None

    # Build diff summary
    old_lines = set(old_content.split('\n')) if old_content else set()
    new_lines = set(new_content.split('\n'))
    added = new_lines - old_lines
    removed = old_lines - new_lines

    content = f"""## File Change: {file_path}

**Area:** {matched_area}
**Changed:** {datetime.now().isoformat()}

### Summary:
- Lines added: {len(added)}
- Lines removed: {len(removed)}

"""
    if added:
        safe_added = [sanitize_secrets(l) if ARTIFACT_STORE_AVAILABLE else l for l in list(added)[:10]]
        content += "### Added:\n```\n" + "\n".join(safe_added) + "\n```\n"

    # Store artifact if available
    disk_id = None
    if ARTIFACT_STORE_AVAILABLE:
        try:
            store = ArtifactStore()
            disk_id = store.store_with_artifacts(
                session_id=f"file-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                artifacts={file_path: new_content},
                area=matched_area,
                sanitize=True
            )
            content += f"\n[Artifact stored: {disk_id}]\n"
        except Exception as e:
            _record_capture_failure("artifact_store", str(e))

    # ALSO log to work dialogue (for local LLM analysis)
    if WORK_LOG_AVAILABLE:
        try:
            work_log.log_observation(
                f"File changed: {file_path} (+{len(added)} lines, -{len(removed)} lines)",
                area=matched_area
            )
        except Exception as e:
            _record_capture_failure("work_log", str(e))

    # Record to Context DNA
    try:
        memory = ContextDNAClient()
        session_id = memory.record_architecture_decision(
            decision=f"[AUTO-CAPTURE] File change: {Path(file_path).name}",
            rationale=content,
            alternatives=None,
            consequences=f"Area: {matched_area}, Disk: {disk_id or 'N/A'}"
        )
        _state.record_capture("file_change", f"{matched_area}: {file_path}")
        return session_id
    except Exception as e:
        _record_capture_failure("context_dna", str(e))
        return None


def _select_evidence_grade(task: str, details: str = None, area: str = None) -> tuple:
    """
    Select appropriate EBM grade based on evidence strength signals.

    Expands beyond the original 2-grade system (case_series/anecdotal) to use
    more of the 8-level EBM pyramid when evidence warrants it.

    Returns:
        Tuple of (evidence_grade_string, base_confidence)
    """
    combined = (f"{task} {details or ''}").lower()

    # COHORT (0.7): Repeated, verified patterns with multiple data points
    cohort_signals = [
        'consistently', 'always', 'every time', 'proven pattern',
        'verified across', 'multiple instances', 'repeated success',
        'regression test', 'load test', 'benchmark',
    ]
    if any(sig in combined for sig in cohort_signals):
        return "cohort", 0.8

    # CASE_CONTROL (0.6): Compared approaches / before-after evidence
    case_control_signals = [
        'compared to', 'versus', 'before and after', 'a/b test',
        'outperformed', 'faster than', 'improvement over',
        'replaced with', 'switched from',
    ]
    if any(sig in combined for sig in case_control_signals):
        return "case_control", 0.7

    # CASE_SERIES (0.5): Standard documented fix or procedure with detail
    if _is_simple_win(task, details):
        return "case_series", 0.7

    # EXPERT_OPINION (0.4): Theoretical or opinion-based without direct verification
    expert_signals = [
        'should', 'recommend', 'best practice', 'convention',
        'standard approach', 'common pattern', 'typically',
    ]
    if any(sig in combined for sig in expert_signals):
        return "expert_opinion", 0.5

    # ANECDOTAL (0.3): Single observation, minimal detail
    if not details or len(details) < 30:
        return "anecdotal", 0.4

    # Default: CASE_SERIES for substantive captures with details
    return "case_series", 0.6


def _is_simple_win(task: str, details: str = None) -> bool:
    """
    Detect objective success signals that should NOT get SOP formatting.

    Simple wins are acknowledgments, not procedures:
    - "Git commit succeeded"
    - "Tests passed"
    - "Docker containers healthy"
    - "Deploy completed"

    These feed the learning pipeline (DNA, Pattern Registry, Consolidation)
    but get CLEAN titles, not verbose SOP formatting.
    """
    combined = f"{task} {details or ''}".lower()

    # Simple win patterns (should NOT become SOPs)
    simple_patterns = [
        # Git operations
        'commit succeeded', 'pushed to', 'merged', 'pull request',
        # Test results
        'tests passed', 'test passed', 'all tests', 'tests green',
        # Docker/Container status
        'container healthy', 'containers healthy', 'docker healthy',
        'service running', 'service started', 'service up',
        # Deploy status
        'deploy completed', 'deployment completed', 'deployed successfully',
        # Build results
        'build succeeded', 'build completed', 'compilation succeeded',
        # Generic success signals
        'exit 0', '200 ok', 'health check passed',
    ]

    # If task matches simple pattern, it's a simple win
    for pattern in simple_patterns:
        if pattern in combined:
            return True

    # Short tasks without procedural content are simple wins
    if len(task) < 50 and not any(k in combined for k in [
        'fix', 'bug', 'error', 'issue', 'problem', 'solve',
        'step', 'process', 'procedure', 'how to', 'guide'
    ]):
        return True

    return False


def capture_success(task: str, details: str = None, area: str = None) -> Optional[str]:
    """
    Capture a successful task completion with TYPE-FIRST classification.

    SIMPLE WINS (objective success signals):
    - Clean titles: "Git commit succeeded", "Tests passed", "Docker healthy"
    - No SOP formatting applied
    - Still feed learning pipeline (DNA, Pattern Registry, Consolidation)

    POTENTIAL SOPs (procedural content):
    - Uses SOP Title Router for full 6-zone format
    - Bug-fix SOPs: "[bug-fix SOP] HEART: bad_sign → fix → outcome"
    - Process SOPs: "[process SOP] GOAL: via (tools) → steps → ✓ verification"

    Both types are captured and displayed in Today's Learnings.
    The difference is title formatting, not learning value.

    Args:
        task: What was accomplished
        details: Additional details about how it was done
        area: Architecture area (auto-detected if not provided)

    Returns:
        Session ID if captured, None otherwise
    """
    if not CONTEXT_DNA_AVAILABLE:
        return None

    # === TYPE-FIRST CLASSIFICATION ===
    # Detect if this is a simple win BEFORE applying any SOP formatting
    enhanced_title = task
    key_insight = details

    if _is_simple_win(task, details):
        # SIMPLE WIN: Keep title clean, no SOP formatting
        # Still feeds learning pipeline (DNA → Pattern Registry → Consolidation)
        enhanced_title = task
        key_insight = details
    elif SOP_ROUTER_AVAILABLE:
        # POTENTIAL SOP: Use the router for proper formatting
        try:
            enhanced_title = generate_sop_title(task, details)

            if BUGFIX_ENHANCER_AVAILABLE:
                key_insight = extract_key_insight(task, details)

            # Dedup check for SOPs only
            if BUGFIX_ENHANCER_AVAILABLE:
                dedup = SOPDeduplicator()
                content_for_check = f"{task} {details or ''}"
                is_duplicate, existing_id = dedup.is_duplicate(enhanced_title, content_for_check)

                if is_duplicate:
                    _state.record_capture("skipped_duplicate", f"{task[:40]}... (dup of {existing_id})")
                    return None
        except Exception as e:
            # Fallback to clean title on error
            enhanced_title = task
            key_insight = details
    elif BUGFIX_ENHANCER_AVAILABLE:
        # Fallback: try individual enhancers if router unavailable
        try:
            enhanced_title = generate_bugfix_sop_title(task, details)
            if enhanced_title is None and PROCESS_ENHANCER_AVAILABLE:
                enhanced_title = generate_process_sop_title(task, details)
            elif enhanced_title is None:
                enhanced_title = f"[process SOP] {task}"
            key_insight = extract_key_insight(task, details)
        except Exception as e:
            enhanced_title = task
            key_insight = details

    # Auto-detect area if not provided
    if not area:
        if KNOWLEDGE_GRAPH_AVAILABLE:
            try:
                kg = KnowledgeGraph()
                area = kg.categorize(f"{task} {details or ''}")
            except Exception as e:
                print(f"[WARN] knowledge_graph categorize: {e}")
                _record_capture_failure("knowledge_graph", str(e))
                area = "general"
        else:
            area = "general"

    # CLEAN content - just the key insight, NO verbose markdown template
    content = key_insight.strip() if key_insight else enhanced_title

    # Sanitize secrets if available
    if ARTIFACT_STORE_AVAILABLE:
        if content:
            content = sanitize_secrets(content)
        if enhanced_title:
            enhanced_title = sanitize_secrets(enhanced_title)

    # Record DIRECTLY to learning store API (NO "Agent Success:" prefix)
    try:
        memory = ContextDNAClient()
        learning = {
            "type": "win",
            "title": enhanced_title,  # Optimized SOP title
            "content": content,        # Clean content (no verbose markdown)
            "tags": [area, "auto-captured"],
        }
        resp = memory._http_post("/api/learnings", learning)
        # Response may nest ID under resp["learning"]["id"]
        if isinstance(resp.get("learning"), dict):
            session_id = resp["learning"].get("id", resp.get("id", ""))
        else:
            session_id = resp.get("id", resp.get("learning_id", ""))

        _state.record_capture("success", f"{area}: {enhanced_title[:50]}")

        # Register for future dedup
        if SOP_ENHANCER_AVAILABLE and session_id:
            try:
                dedup = SOPDeduplicator()
                dedup.register_sop(session_id, enhanced_title, content)
            except Exception as e:
                _record_capture_failure("sop_dedup", str(e))

        # === WIRE EVIDENCE GRADE TO OBSERVABILITY STORE ===
        # Connects capture_success to the EBM grading system.
        # record_claim_with_evidence() now routes through quarantine internally:
        # claims start as status='quarantined' and get promoted by lite_scheduler.
        claim_id = None
        base_confidence = 0.6
        try:
            from memory.observability_store import get_observability_store
            obs_store = get_observability_store()
            evidence_grade, base_confidence = _select_evidence_grade(task, details, area)
            claim_id = obs_store.record_claim_with_evidence(
                claim_text=enhanced_title,
                evidence_grade=evidence_grade,
                source="capture_success",
                confidence=base_confidence,
                tags=[area or "general", "auto-captured"],
                area=area or "general",
            )
        except Exception as e:
            print(f"[WARN] evidence_store: {e}")
            _record_capture_failure("evidence_store", str(e))

        # === WIRE DIRECT CLAIM OUTCOME FOR QUARANTINE PROMOTION ===
        # Records outcome directly to claim (bypasses injection_claim chain).
        # This is the PRIMARY path for quarantine promotion - fires on EVERY
        # capture_success regardless of injection_info availability.
        if claim_id:
            try:
                from memory.observability_store import get_observability_store
                dco_store = get_observability_store()
                dco_store.record_direct_claim_outcome(
                    claim_id=claim_id,
                    success=True,
                    reward=base_confidence if base_confidence else 0.7,
                    source="capture_success",
                    notes=f"{enhanced_title[:100]}",
                )
            except Exception as dco_err:
                _record_capture_failure("direct_claim_outcome", str(dco_err))

        # === Gap 2: WIRE LEARNING OUTCOME ATTRIBUTION ===
        # Records which learning appeared in injection → links to outcome
        if claim_id and session_id:
            try:
                from memory.observability_store import get_observability_store
                ilo_store = get_observability_store()
                import os
                _inj_session = os.environ.get("CLAUDE_SESSION_ID", "")
                # Get most recent injection_id for attribution
                _inj_row = ilo_store._sqlite_conn.execute(
                    "SELECT injection_id FROM injection_event ORDER BY timestamp_utc DESC LIMIT 1"
                ).fetchone()
                if _inj_row:
                    ilo_store.record_learning_outcome(
                        injection_id=_inj_row["injection_id"],
                        learning_id=str(session_id),
                        section_id="s2",
                        outcome_success=True,
                        outcome_reward=base_confidence if base_confidence else 0.7,
                    )
            except Exception:
                pass  # Non-critical

        # === Gap 3: WIRE SOP OUTCOME TRACKING ===
        # When an SOP-formatted success is captured, record outcome for effectiveness
        if claim_id and ("[bug-fix SOP]" in enhanced_title or "[process SOP]" in enhanced_title):
            try:
                from memory.observability_store import get_observability_store
                sop_store = get_observability_store()
                _inj_row = sop_store._sqlite_conn.execute(
                    "SELECT injection_id FROM injection_event ORDER BY timestamp_utc DESC LIMIT 1"
                ).fetchone()
                sop_store.record_sop_outcome(
                    sop_id=claim_id,
                    injection_id=_inj_row["injection_id"] if _inj_row else None,
                    outcome_success=True,
                    outcome_reward=base_confidence if base_confidence else 0.7,
                )
            except Exception:
                pass  # Non-critical

        # === WIRE ROUTE RECORDING FOR MULTI-ROUTE SOPs ===
        # When a process SOP is captured, record the route for accumulation.
        # Closes the loop: successful tasks auto-accumulate as preferred routes.
        if ROUTE_TRACKER_AVAILABLE and enhanced_title.startswith("[process SOP]"):
            try:
                from memory.process_sop_enhancer import generate_goal_heart
                goal_heart = generate_goal_heart(task)
                route_desc = details[:100] if details else task[:100]
                record_route_success(goal_heart, route_desc, enhanced_title)
            except Exception as e:
                _record_capture_failure("route_tracker", str(e))

        # === WIRE OUTCOME TO A/B EXPERIMENT TRACKING ===
        # This closes the learning loop: injection → outcome → variant performance
        try:
            import os
            current_session = os.environ.get("CLAUDE_SESSION_ID", "")
            injection_info = None
            injection_id = None

            # Try session-based lookup first
            if current_session:
                from memory.injection_store import get_injection_store
                store = get_injection_store()
                injection_info = store.get_session_injection(current_session)

            # Fallback 1: use most recent injection from observability store
            if not injection_info:
                try:
                    from memory.observability_store import get_observability_store
                    fallback_store = get_observability_store()
                    row = fallback_store._sqlite_conn.execute(
                        "SELECT injection_id, session_id FROM injection_event ORDER BY timestamp_utc DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        injection_id = row["injection_id"]
                        if not current_session:
                            current_session = row["session_id"] or "no_session"
                        injection_info = {"injection_id": injection_id, "ab_variant": "control"}
                except Exception:
                    pass  # Best-effort fallback

            # Fallback 2: use most recent boundary injection (bi_ prefix)
            # This is critical because boundary_feedback.record_feedback() only
            # works with injection_ids that exist in boundary_injections table.
            # Without this, feedback from inj_ IDs silently fails.
            boundary_injection_id = None
            if injection_info:
                try:
                    from memory.boundary_feedback import get_boundary_learner
                    bl = get_boundary_learner()
                    row = bl.db.execute(
                        "SELECT injection_id FROM boundary_injections "
                        "WHERE feedback_recorded = 0 ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        boundary_injection_id = row[0]
                except Exception:
                    pass  # Best-effort
            else:
                # No injection_info at all - get boundary injection directly
                try:
                    from memory.boundary_feedback import get_boundary_learner
                    bl = get_boundary_learner()
                    row = bl.db.execute(
                        "SELECT injection_id FROM boundary_injections "
                        "WHERE feedback_recorded = 0 ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        boundary_injection_id = row[0]
                        injection_id = boundary_injection_id
                        injection_info = {"injection_id": boundary_injection_id, "ab_variant": "control"}
                except Exception:
                    pass  # Best-effort

            if injection_info:
                if not injection_id:
                    injection_id = injection_info.get("injection_id")
                ab_variant = injection_info.get("ab_variant", "control")
                if not current_session:
                    current_session = "no_session"

                # Record positive outcome to hook evolution (A/B variant tracking)
                try:
                    from memory.hook_evolution import get_hook_evolution_engine
                    engine = get_hook_evolution_engine()
                    engine.record_outcome(
                        variant_id=_resolve_hook_variant_id(current_session),
                        session_id=current_session,
                        outcome="positive",
                        signals=["capture_success", area or "general"],
                        task_completed=True,
                        confidence=0.8
                    )
                except Exception:
                    pass  # Non-blocking

                # Record positive feedback to boundary learner
                # Use boundary_injection_id (bi_ prefix) when available,
                # since record_feedback() looks up in boundary_injections table.
                feedback_target_id = boundary_injection_id or injection_id
                try:
                    from memory.boundary_feedback import get_boundary_learner
                    learner = get_boundary_learner()
                    learner.record_feedback(
                        injection_id=feedback_target_id,
                        was_helpful=True,
                        project_was_correct=True,
                        confidence=0.8,
                        signals=["task_success", enhanced_title[:50]]
                    )
                except Exception:
                    pass  # Non-blocking

                # === WIRE OUTCOME_EVENT: Close the evidence feedback loop ===
                # This populates outcome_event which compute_claim_outcome_rollup
                # depends on for quarantine promotion (n>=10, success_rate>=0.7).
                # Without this, the feedback loop is broken: claims quarantine
                # forever because there are no outcomes to evaluate.
                try:
                    from memory.observability_store import get_observability_store
                    oe_store = get_observability_store()
                    oe_store.record_outcome_event(
                        session_id=current_session,
                        outcome_type="task_success",
                        success=True,
                        reward=base_confidence,
                        injection_id=injection_id,
                        notes=f"capture_success: {enhanced_title[:100]}",
                    )
                except Exception as oe_err:
                    _record_capture_failure("outcome_event", str(oe_err))

                # === WIRE INJECTION_CLAIM: Link claims to their injections ===
                # This populates the injection_claim table which is the JOIN key
                # between claims and outcome_events in compute_claim_outcome_rollup.
                # Without this, rollups are always empty even if outcomes exist.
                if claim_id and injection_id:
                    try:
                        from memory.observability_store import get_observability_store
                        lnk_store = get_observability_store()
                        lnk_store.link_claim_to_injection(
                            injection_id=injection_id,
                            claim_id=claim_id,
                            section_id="capture_success",
                        )
                    except Exception as lnk_err:
                        _record_capture_failure("injection_claim_link", str(lnk_err))

                # === WIRE SOP→OUTCOME: Link SOPs to outcome events (P6 enhanced) ===
                # Two-pronged approach:
                # 1. Title-based: Link if title contains SOP-like keywords
                # 2. Context-based: Search active injection for SOPs mentioned in context
                # This bootstraps the evidence loop — SOPs get outcome attribution.
                try:
                    import hashlib
                    sop_store = get_observability_store()
                    # Get the outcome_id we just created
                    last_oe = sop_store._sqlite_conn.execute(
                        "SELECT outcome_id FROM outcome_event WHERE session_id=? ORDER BY timestamp_utc DESC LIMIT 1",
                        (current_session,)
                    ).fetchone()
                    if last_oe:
                        linked = 0
                        # Prong 1: Title-based SOP linking (original)
                        title_lower = (enhanced_title or "").lower()
                        if any(tag in title_lower for tag in ['sop', 'fix', 'pattern', 'gotcha', 'procedure', 'workaround']):
                            sop_id = f"SOP-{hashlib.md5(enhanced_title.encode()).hexdigest()[:12].upper()}"
                            sop_store.link_sop_to_outcome(
                                sop_id=sop_id,
                                sop_title=enhanced_title[:200],
                                outcome_event_id=last_oe[0],
                                session_id=current_session,
                            )
                            linked += 1

                        # Prong 2: Search quarantine for SOPs active in this injection
                        if injection_id and linked < 5:
                            try:
                                sop_candidates = sop_store._sqlite_conn.execute("""
                                    SELECT item_id, notes FROM knowledge_quarantine
                                    WHERE item_type IN ('sop', 'session_insight', 'cross_session_pattern')
                                    AND status = 'quarantined'
                                    ORDER BY created_at_utc DESC LIMIT 10
                                """).fetchall()
                                for sop_item_id, sop_stmt in sop_candidates:
                                    if linked >= 5:
                                        break
                                    # Check relevance: 2+ word overlap with task
                                    task_words = set(title_lower.split())
                                    sop_words = set((sop_stmt or "").lower().split())
                                    if len(task_words & sop_words) >= 2:
                                        sop_id = f"SOP-{hashlib.md5(sop_item_id.encode()).hexdigest()[:12].upper()}"
                                        sop_store.link_sop_to_outcome(
                                            sop_id=sop_id,
                                            sop_title=(sop_stmt or "")[:200],
                                            outcome_event_id=last_oe[0],
                                            session_id=current_session,
                                        )
                                        linked += 1
                            except Exception:
                                pass
                except Exception:
                    pass  # Non-blocking

        except Exception:
            pass  # Never fail capture due to A/B tracking

        # === WIRE PATTERN REGISTRY: Learn coordination patterns from wins ===
        # Feeds EvolvingPatternRegistry so successful patterns propagate
        # across fleet nodes and future success detection improves.
        try:
            from memory.pattern_registry import EvolvingPatternRegistry
            _pattern_reg = EvolvingPatternRegistry()
            _pattern_reg.learn_from_confirmed(
                success_task=enhanced_title[:200],
                success_details=content or "",
                context_entries=[{"content": details or ""}] if details else [],
            )
        except Exception:
            pass  # Non-blocking — pattern learning is additive

        return session_id
    except Exception as e:
        _record_capture_failure("context_dna", str(e))
        return None


def capture_failure(task: str, error: str = None, area: str = None, root_cause: str = None) -> Optional[str]:
    """
    Capture a failed task to eliminate survivorship bias.

    Mirrors capture_success() but records losses, not wins.
    Uses CASE_SERIES evidence grade (lower than COHORT used by success path)
    to reflect that failures are valuable but less validated signals.

    Both successes and failures feed the learning pipeline.
    Without failure capture, the system only learns from wins = survivorship bias.

    Args:
        task: What was attempted
        error: Error message or description of what went wrong
        area: Architecture area (auto-detected if not provided)
        root_cause: Known root cause (if diagnosed)

    Returns:
        Claim ID if recorded, None otherwise
    """
    if not CONTEXT_DNA_AVAILABLE:
        return None

    # Auto-detect area if not provided
    if not area:
        if KNOWLEDGE_GRAPH_AVAILABLE:
            try:
                kg = KnowledgeGraph()
                area = kg.categorize(f"{task} {error or ''} {root_cause or ''}")
            except Exception as e:
                print(f"[WARN] knowledge_graph categorize: {e}")
                _record_capture_failure("knowledge_graph", str(e))
                area = "general"
        else:
            area = "general"

    # Build statement
    root_cause_str = root_cause or "unknown"
    statement = f"FAILURE: {task} | Root cause: {root_cause_str}"

    # Build content for learning store
    content = f"Task: {task}"
    if error:
        content += f"\nError: {error}"
    if root_cause:
        content += f"\nRoot cause: {root_cause}"

    # Sanitize secrets
    if ARTIFACT_STORE_AVAILABLE:
        statement = sanitize_secrets(statement)
        content = sanitize_secrets(content)

    # Record to learning store as 'gotcha' (failure learnings)
    try:
        memory = ContextDNAClient()
        learning = {
            "type": "gotcha",
            "title": statement,
            "content": content,
            "tags": ["failure", "learning", area, "auto-captured"],
        }
        resp = memory._http_post("/api/learnings", learning)
        learning_id = resp.get("id", resp.get("learning_id", ""))

        _state.record_capture("failure", f"{area}: {task[:50]}")

        # === WIRE EVIDENCE GRADE TO OBSERVABILITY STORE ===
        # Failures use CASE_SERIES (0.5 weight) - lower than success COHORT (0.7)
        # to reflect that failure signals are valuable but less validated.
        try:
            from memory.observability_store import get_observability_store
            obs_store = get_observability_store()
            claim_id = obs_store.record_claim_with_evidence(
                claim_text=statement,
                evidence_grade="case_series",
                source="capture_failure",
                confidence=0.4,
                tags=["failure", "learning", area or "general"],
                area=area or "general",
            )
        except Exception as e:
            _record_capture_failure("evidence_store", str(e))
            claim_id = None

        # === WIRE DIRECT CLAIM OUTCOME FOR QUARANTINE PROMOTION ===
        # Failure outcomes are equally important for promotion evaluation.
        # compute_claim_outcome_rollup needs BOTH successes AND failures
        # to compute meaningful success_rate ratios.
        if claim_id:
            try:
                from memory.observability_store import get_observability_store
                dco_store = get_observability_store()
                dco_store.record_direct_claim_outcome(
                    claim_id=claim_id,
                    success=False,
                    reward=-0.3,
                    source="capture_failure",
                    notes=f"{task[:100]}",
                )
            except Exception as dco_err:
                _record_capture_failure("direct_claim_outcome", str(dco_err))

        # === WIRE ROUTE FAILURE FOR MULTI-ROUTE SOPs ===
        # Record failed approaches so the system avoids them in future.
        if ROUTE_TRACKER_AVAILABLE:
            try:
                from memory.process_sop_enhancer import generate_goal_heart
                goal_heart = generate_goal_heart(task)
                failure_note = root_cause or error or 'unknown'
                record_route_failure(goal_heart, task[:100], failure_note[:200])
            except Exception as e:
                _record_capture_failure("route_tracker", str(e))

        # === WIRE OUTCOME_EVENT: UNCONDITIONAL negative signal ===
        # P1.6 FIX: outcome_event MUST fire regardless of injection_info.
        # Previously gated behind `if injection_info:` which silently
        # dropped ALL negative outcomes when no injection context existed.
        # The evidence pipeline needs BOTH successes AND failures.
        # injection_id is best-effort: included when available for attribution.
        try:
            import os
            current_session = os.environ.get("CLAUDE_SESSION_ID", "") or "no_session"
            # Best-effort injection_id for outcome attribution
            _neg_injection_id = None
            try:
                from memory.observability_store import get_observability_store
                _neg_obs = get_observability_store()
                _neg_row = _neg_obs._sqlite_conn.execute(
                    "SELECT injection_id FROM injection_event ORDER BY timestamp_utc DESC LIMIT 1"
                ).fetchone()
                if _neg_row:
                    _neg_injection_id = _neg_row["injection_id"]
            except Exception:
                pass
            from memory.observability_store import get_observability_store
            oe_store = get_observability_store()
            oe_store.record_outcome_event(
                session_id=current_session,
                outcome_type="task_failure",
                success=False,
                reward=-0.3,
                injection_id=_neg_injection_id,
                notes=f"capture_failure: {task[:100]}",
            )
        except Exception as oe_err:
            _record_capture_failure("outcome_event", str(oe_err))

        # === WIRE NEGATIVE OUTCOME TO A/B EXPERIMENT TRACKING ===
        # A/B tracking legitimately requires injection_info context.
        try:
            import os
            current_session = os.environ.get("CLAUDE_SESSION_ID", "")
            injection_info = None
            injection_id = None
            boundary_injection_id = None

            # Try session-based lookup first
            if current_session:
                from memory.injection_store import get_injection_store
                store = get_injection_store()
                injection_info = store.get_session_injection(current_session)

            # Fallback: find most recent boundary injection for feedback
            if not injection_info:
                try:
                    from memory.boundary_feedback import get_boundary_learner
                    bl = get_boundary_learner()
                    row = bl.db.execute(
                        "SELECT injection_id FROM boundary_injections "
                        "WHERE feedback_recorded = 0 ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        boundary_injection_id = row[0]
                        injection_id = boundary_injection_id
                        injection_info = {"injection_id": boundary_injection_id, "ab_variant": "control"}
                except Exception:
                    pass

            if injection_info:
                if not injection_id:
                    injection_id = injection_info.get("injection_id")
                ab_variant = injection_info.get("ab_variant", "control")
                if not current_session:
                    current_session = "no_session"

                # Also resolve boundary_injection_id if not yet found
                if not boundary_injection_id:
                    try:
                        from memory.boundary_feedback import get_boundary_learner
                        bl = get_boundary_learner()
                        row = bl.db.execute(
                            "SELECT injection_id FROM boundary_injections "
                            "WHERE feedback_recorded = 0 ORDER BY timestamp DESC LIMIT 1"
                        ).fetchone()
                        if row:
                            boundary_injection_id = row[0]
                    except Exception:
                        pass

                try:
                    from memory.hook_evolution import get_hook_evolution_engine
                    engine = get_hook_evolution_engine()
                    engine.record_outcome(
                        variant_id=_resolve_hook_variant_id(current_session),
                        session_id=current_session,
                        outcome="negative",
                        signals=["capture_failure", area or "general", root_cause_str],
                        task_completed=False,
                        confidence=0.6
                    )
                except Exception:
                    pass  # Non-blocking

                # Record negative feedback to boundary learner
                # Use boundary_injection_id (bi_ prefix) since record_feedback()
                # looks up in boundary_injections table.
                feedback_target_id = boundary_injection_id or injection_id
                try:
                    from memory.boundary_feedback import get_boundary_learner
                    learner = get_boundary_learner()
                    learner.record_feedback(
                        injection_id=feedback_target_id,
                        was_helpful=False,
                        project_was_correct=True,
                        confidence=0.6,
                        signals=["task_failure", task[:50]]
                    )
                except Exception:
                    pass  # Non-blocking

                # === WIRE INJECTION_CLAIM: Link claims to their injections ===
                if claim_id and injection_id:
                    try:
                        from memory.observability_store import get_observability_store
                        lnk_store = get_observability_store()
                        lnk_store.link_claim_to_injection(
                            injection_id=injection_id,
                            claim_id=claim_id,
                            section_id="capture_failure",
                        )
                    except Exception as lnk_err:
                        _record_capture_failure("injection_claim_link", str(lnk_err))
        except Exception:
            pass  # Never fail capture due to A/B tracking

        return claim_id or learning_id
    except Exception as e:
        _record_capture_failure("context_dna", str(e))
        return None


def capture_error_resolution(error: str, resolution: str, area: str = None) -> Optional[str]:
    """
    Capture an error that was resolved.

    This is valuable learning - understanding what went wrong and how to fix it.

    Args:
        error: The error that occurred
        resolution: How it was fixed
        area: Architecture area

    Returns:
        Session ID if captured, None otherwise
    """
    if not CONTEXT_DNA_AVAILABLE:
        return None

    # Auto-detect area
    if not area:
        if KNOWLEDGE_GRAPH_AVAILABLE:
            try:
                kg = KnowledgeGraph()
                area = kg.categorize(f"{error} {resolution}")
            except Exception as e:
                print(f"[WARN] knowledge_graph categorize: {e}")
                _record_capture_failure("knowledge_graph", str(e))
                area = "general"
        else:
            area = "general"

    # Record as bug fix
    try:
        memory = ContextDNAClient()
        session_id = memory.record_bug_fix(
            symptom=sanitize_secrets(error) if ARTIFACT_STORE_AVAILABLE else error,
            root_cause="See error details",
            fix=sanitize_secrets(resolution) if ARTIFACT_STORE_AVAILABLE else resolution,
            tags=[area, "auto-captured", "error-resolution"]
        )
        _state.record_capture("error_resolution", f"{area}: {error[:30]}")
        return session_id
    except Exception as e:
        _record_capture_failure("context_dna", str(e))
        return None


def capture_failure_signal(
    failure_type: str,
    description: str,
    context: str = None,
    confidence: float = 0.7,
    session_id: str = None
) -> Optional[str]:
    """
    Capture a failure signal for learning system feedback.

    This is called by dialogue_mirror when frustration signals, retry patterns,
    or implicit failures are detected. These feed back into:
    - Learning system (recorded as 'gotcha' type)
    - A/B experiment tracking (negative outcomes)
    - SOP refinement pipeline

    Args:
        failure_type: Type of failure (frustration, retry_request, abandonment, etc.)
        description: What was detected
        context: Additional context about the failure
        confidence: Confidence level (0.0-1.0)
        session_id: Optional session ID for A/B tracking

    Returns:
        Learning ID if captured, None otherwise
    """
    if not CONTEXT_DNA_AVAILABLE:
        return None

    try:
        memory = ContextDNAClient()

        # Record as 'gotcha' type learning for future prevention
        title = f"[failure signal] {failure_type}: {description[:50]}"
        content = f"Type: {failure_type}\nDescription: {description}\nConfidence: {confidence}"
        if context:
            content += f"\nContext: {context}"

        learning = {
            "type": "gotcha",
            "title": title,
            "content": content,
            "tags": ["failure-signal", failure_type, "dialogue-mirror", "auto-captured"],
        }
        resp = memory._http_post("/api/learnings", learning)
        learning_id = resp.get("id", resp.get("learning_id", ""))

        _state.record_capture("failure_signal", f"{failure_type}: {description[:30]}")

        # === WIRE OUTCOME_EVENT: UNCONDITIONAL negative signal ===
        # P1.6 FIX: outcome_event MUST fire regardless of injection_info.
        # Failure signals from dialogue_mirror are strong negative outcomes.
        # injection_id is best-effort: included when available for attribution.
        try:
            import os
            current_session = session_id or os.environ.get("CLAUDE_SESSION_ID", "") or "no_session"
            # Best-effort injection_id for outcome attribution
            _neg_injection_id = None
            try:
                from memory.observability_store import get_observability_store
                _neg_obs = get_observability_store()
                _neg_row = _neg_obs._sqlite_conn.execute(
                    "SELECT injection_id FROM injection_event ORDER BY timestamp_utc DESC LIMIT 1"
                ).fetchone()
                if _neg_row:
                    _neg_injection_id = _neg_row["injection_id"]
            except Exception:
                pass
            from memory.observability_store import get_observability_store
            oe_store = get_observability_store()
            oe_store.record_outcome_event(
                session_id=current_session,
                outcome_type="failure_signal",
                success=False,
                reward=-0.5,
                injection_id=_neg_injection_id,
                notes=f"dialogue_mirror: {failure_type}: {description[:80]}",
            )
        except Exception as oe_err:
            _record_capture_failure("outcome_event", str(oe_err))

        # === WIRE NEGATIVE OUTCOME TO A/B EXPERIMENT TRACKING ===
        # A/B tracking legitimately requires injection_info context.
        try:
            import os
            current_session = session_id or os.environ.get("CLAUDE_SESSION_ID", "")
            injection_info = None
            injection_id = None
            boundary_injection_id = None

            # Try session-based lookup first
            if current_session:
                from memory.injection_store import get_injection_store
                store = get_injection_store()
                injection_info = store.get_session_injection(current_session)

            # Fallback: find most recent boundary injection for feedback
            if not injection_info:
                try:
                    from memory.boundary_feedback import get_boundary_learner
                    bl = get_boundary_learner()
                    row = bl.db.execute(
                        "SELECT injection_id FROM boundary_injections "
                        "WHERE feedback_recorded = 0 ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        boundary_injection_id = row[0]
                        injection_id = boundary_injection_id
                        injection_info = {"injection_id": boundary_injection_id, "ab_variant": "control"}
                except Exception:
                    pass

            if injection_info:
                if not injection_id:
                    injection_id = injection_info.get("injection_id")
                ab_variant = injection_info.get("ab_variant", "control")
                if not current_session:
                    current_session = "no_session"

                # Also resolve boundary_injection_id if not yet found
                if not boundary_injection_id:
                    try:
                        from memory.boundary_feedback import get_boundary_learner
                        bl = get_boundary_learner()
                        row = bl.db.execute(
                            "SELECT injection_id FROM boundary_injections "
                            "WHERE feedback_recorded = 0 ORDER BY timestamp DESC LIMIT 1"
                        ).fetchone()
                        if row:
                            boundary_injection_id = row[0]
                    except Exception:
                        pass

                # Record negative outcome to hook evolution
                try:
                    from memory.hook_evolution import get_hook_evolution_engine
                    engine = get_hook_evolution_engine()
                    engine.record_outcome(
                        variant_id=_resolve_hook_variant_id(current_session),
                        session_id=current_session,
                        outcome="negative",
                        signals=[failure_type, "dialogue_mirror_detected"],
                        task_completed=False,
                        confidence=confidence
                    )
                except Exception as e:
                    print(f"[WARN] A/B negative outcome recording failed: {e}")

                # Record negative feedback to boundary learner
                # Use boundary_injection_id (bi_ prefix) for proper lookup.
                feedback_target_id = boundary_injection_id or injection_id
                try:
                    from memory.boundary_feedback import get_boundary_learner
                    learner = get_boundary_learner()
                    learner.record_feedback(
                        injection_id=feedback_target_id,
                        was_helpful=False,
                        project_was_correct=True,
                        confidence=confidence,
                        signals=[failure_type, description[:50]]
                    )
                except Exception as e:
                    print(f"[WARN] A/B signal recording failed: {e}")
        except Exception as e:
            print(f"[WARN] A/B tracking during capture failed: {e}")

        return learning_id
    except Exception as e:
        _record_capture_failure("capture_bugfix", str(e))
        return None


def get_capture_stats() -> dict:
    """Get statistics about auto-capture activity."""
    return {
        "captures_today": _state.state["captures_today"],
        "last_capture": _state.state["last_capture"],
        "recent_count": len(_state.state["recent_commands"]),
        "discovered_resources": len(_state.state.get("discovered_resources", {}))
    }


# =============================================================================
# INTEGRATION HELPERS
# =============================================================================

def scan_for_changes(hours: int = 4) -> int:
    """
    Scan recent git commits for infrastructure-relevant file changes.

    Called by lite_scheduler._run_scan_project() on a recurring basis.
    Checks commits within the last N hours and counts infrastructure-relevant
    file changes (terraform, docker, scripts, config, memory system, etc.).

    Args:
        hours: How far back to look (default 4 hours).

    Returns:
        Number of infrastructure-relevant file changes detected.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--since=" + str(hours) + " hours ago",
             "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent)
        )
        if result.returncode != 0:
            return 0

        files = [f.strip() for f in result.stdout.strip().splitlines()
                 if f.strip()]
        if not files:
            return 0

        # Count infrastructure-relevant files
        infra_patterns = [
            ".tf", "dockerfile", "docker-compose", ".yml", ".yaml",
            "nginx", ".service", "gunicorn", "scripts/", "memory/",
            ".env", "package.json", "requirements", "tsconfig",
            "deploy", "infra/", ".github/", "config/",
        ]
        count = 0
        for f in files:
            f_lower = f.lower()
            if any(p in f_lower for p in infra_patterns):
                count += 1

        return count
    except Exception:
        return 0


def should_capture_command(command: str) -> bool:
    """Check if a command should trigger capture."""
    cmd_lower = command.lower()
    return any(re.search(p, cmd_lower) for p, _, _ in ARCHITECTURE_COMMANDS)


def should_capture_file(file_path: str) -> bool:
    """Check if a file modification should trigger capture."""
    file_lower = file_path.lower()
    patterns = [".tf", "dockerfile", "docker-compose", "nginx", ".service", "gunicorn"]
    return any(p in file_lower for p in patterns)


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Auto-Capture CLI")
        print("")
        print("Commands:")
        print("  stats                     - Show capture statistics")
        print("  resources                 - Show discovered resources")
        print("  test-command <cmd>        - Test if command would be captured")
        print("")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "stats":
        stats = get_capture_stats()
        print(f"Captures today: {stats['captures_today']}")
        print(f"Last capture: {stats['last_capture'] or 'Never'}")
        print(f"Recent commands: {stats['recent_count']}")
        print(f"Discovered resources: {stats['discovered_resources']}")

    elif cmd == "resources":
        resources = _state.state.get("discovered_resources", {})
        if resources:
            for rtype, items in resources.items():
                print(f"\n{rtype}:")
                for rid, info in items.items():
                    print(f"  {rid}: {info.get('details', '')}")
        else:
            print("No resources discovered yet")

    elif cmd == "test-command":
        if len(sys.argv) < 3:
            print("Usage: test-command <command>")
            sys.exit(1)
        test_cmd = " ".join(sys.argv[2:])
        if should_capture_command(test_cmd):
            print(f"✅ Would capture: {test_cmd}")
            for pattern, area, desc in ARCHITECTURE_COMMANDS:
                if re.search(pattern, test_cmd.lower()):
                    print(f"   Area: {area}")
                    print(f"   Description: {desc}")
                    break
        else:
            print(f"❌ Would NOT capture: {test_cmd}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
