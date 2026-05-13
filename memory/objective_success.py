#!/usr/bin/env python3
"""
Objective Success Detector - Extract REAL Successes from Work Stream

The problem with agent-reported "success":
- Agents often claim success prematurely
- "I'll deploy this" is not success
- "The deploy should work" is not success
- "Done!" followed by "wait, there's an error" is NOT success

OBJECTIVE SUCCESS is when:
1. User explicitly confirms ("that worked", "perfect", "yes")
2. System returns success signals (exit 0, "healthy", "200 OK")
3. State persists (git commit, file saved, deploy completed)
4. No reversal occurs (we didn't undo, retry, or fix it after)

This module analyzes the FULL conversation/work stream and extracts
only the things that OBJECTIVELY succeeded - not premature claims.

ARCHITECTURE:
    Full Work Stream (dialogue + commands + outputs)
                    ↓
    ┌─────────────────────────────────────────┐
    │     OBJECTIVE SUCCESS DETECTOR          │
    │                                         │
    │  1. User Confirmation Detection         │
    │     - "that worked" → SUCCESS           │
    │     - "perfect" → SUCCESS               │
    │     - "no wait" → CANCEL PREVIOUS       │
    │                                         │
    │  2. System Confirmation Detection       │
    │     - exit_code == 0 → POTENTIAL        │
    │     - "healthy" in output → POTENTIAL   │
    │     - followed by error → CANCEL        │
    │                                         │
    │  3. State Persistence Detection         │
    │     - git commit succeeded → SUCCESS    │
    │     - file written + no rewrite → OK    │
    │                                         │
    │  4. Reversal Detection                  │
    │     - retry same thing → CANCEL PREV    │
    │     - "fix" after "done" → CANCEL       │
    │     - rollback → CANCEL                 │
    │                                         │
    └─────────────────────────────────────────┘
                    ↓
    ONLY Objective Successes → Architecture Brain

Usage:
    from memory.objective_success import analyze_for_successes

    # Analyze work stream and extract objective successes
    successes = analyze_for_successes(entries)

    # Each success has confidence score and evidence
    for s in successes:
        print(f"{s['task']} (confidence: {s['confidence']})")
        print(f"  Evidence: {s['evidence']}")
"""

import re
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict

# Ensure parent directory is in path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# =============================================================================
# SUCCESS DETECTION PATTERNS
# =============================================================================

# User confirmation phrases (STRONG success signal)
USER_CONFIRMATION_PATTERNS = [
    (r"\bsuccess\b", 1.0, "user_confirmed"),  # "success!", "success"
    (r"\bthat worked\b", 1.0, "user_confirmed"),
    (r"\bperfect\b", 0.9, "user_confirmed"),
    (r"\bexcellent\b", 0.9, "user_confirmed"),
    (r"\bgood job\b", 0.9, "user_confirmed"),
    (r"\bnice\b", 0.8, "user_confirmed"),  # "nice!", "nice work"
    (r"\bawesome\b", 0.8, "user_confirmed"),
    (r"\blooks good\b", 0.8, "user_confirmed"),
    (r"\bworks\b", 0.7, "user_confirmed"),
    (r"\byes\b(?!\s*(but|however|wait))", 0.6, "user_confirmed"),  # "yes" not followed by "but"
    (r"\bok\b(?!\s*(but|however|wait|so))", 0.5, "user_acknowledged"),
    (r"\b(thanks|thank you)\b", 0.5, "user_acknowledged"),
]

# User rejection/correction phrases (CANCEL previous success)
USER_REJECTION_PATTERNS = [
    (r"\bno wait\b", "cancel_previous"),
    (r"\bactually\b", "cancel_previous"),
    (r"\bthat('s| is)? not (right|correct|working)\b", "cancel_previous"),
    (r"\bthat didn'?t work\b", "cancel_previous"),
    (r"\bstill (broken|not working|failing)\b", "cancel_previous"),
    (r"\btry again\b", "cancel_previous"),
    (r"\bundo\b", "cancel_previous"),
    (r"\brollback\b", "cancel_previous"),
    (r"\brevert\b", "cancel_previous"),
]

# System success signals (POTENTIAL success, needs validation)
# These are OBJECTIVE - no human confirmation needed if confidence is high enough
# =============================================================================
# EXPANDED OBJECTIVE SUCCESS PATTERNS
# =============================================================================
# These patterns detect IRREFUTABLE system-level successes that don't need
# human confirmation. The system learns more patterns over time.

SYSTEM_SUCCESS_PATTERNS = [
    # =========================================================================
    # HTTP/API SUCCESS CODES (Irrefutable network success)
    # =========================================================================
    (r"\b200\s*OK\b", 0.85, "http_200_ok"),
    (r"\b201\s*(Created)?\b", 0.85, "http_201_created"),
    (r"\b202\s*(Accepted)?\b", 0.8, "http_202_accepted"),
    (r"\b204\s*(No Content)?\b", 0.8, "http_204_no_content"),
    (r'"status":\s*200', 0.85, "json_status_200"),
    (r'"statusCode":\s*200', 0.85, "json_status_code_200"),
    (r'"code":\s*200', 0.8, "json_code_200"),
    (r"HTTP/\d\.\d\s+2\d\d", 0.85, "http_2xx_response"),
    (r"response.*\b2\d\d\b", 0.75, "response_2xx"),

    # =========================================================================
    # EXIT CODES (System process completion)
    # =========================================================================
    (r"exit[_ ]?code[:\s]*0\b", 0.8, "exit_zero"),
    (r"exited with code 0", 0.85, "exited_code_0"),
    (r"return[ed]?\s+0\b", 0.7, "returned_zero"),
    (r"status[:\s]+0\b", 0.75, "status_zero"),
    (r"\$\?\s*=\s*0", 0.8, "bash_success"),

    # =========================================================================
    # DOCKER/CONTAINER SUCCESS
    # =========================================================================
    (r"\bhealthy\b", 0.85, "container_healthy"),
    (r"\bactive\s*\(running\)\b", 0.85, "service_running"),
    (r"container.*started", 0.8, "container_started"),
    (r"container.*created", 0.75, "container_created"),
    (r"Successfully built", 0.85, "docker_built"),
    (r"Successfully tagged", 0.85, "docker_tagged"),
    (r"Up \d+\s*(seconds?|minutes?|hours?)", 0.8, "container_up"),
    (r"Pulling.*done", 0.8, "docker_pull_done"),
    (r"Image is up to date", 0.8, "image_current"),
    (r"Starting.*done", 0.75, "service_started"),
    (r"Recreated", 0.75, "container_recreated"),

    # =========================================================================
    # GIT SUCCESS
    # =========================================================================
    (r"\bcommitted\b", 0.8, "git_committed"),
    (r"\bpushed\b", 0.8, "git_pushed"),
    (r"create mode \d+", 0.75, "git_file_created"),
    (r"\d+ files? changed", 0.75, "git_changes_staged"),
    (r"insertions?\(\+\)", 0.7, "git_insertions"),
    (r"Fast-forward", 0.8, "git_fast_forward"),
    (r"Already up to date", 0.75, "git_up_to_date"),
    (r"Branch .* set up to track", 0.8, "git_branch_tracking"),
    (r"Merge made by", 0.85, "git_merge_success"),
    (r"rebase.*successfully", 0.85, "git_rebase_success"),

    # =========================================================================
    # BUILD/COMPILE SUCCESS
    # =========================================================================
    (r"Build succeeded", 0.9, "build_succeeded"),
    (r"BUILD SUCCESS", 0.9, "maven_build_success"),
    (r"compiled successfully", 0.9, "compile_success"),
    (r"Compilation complete", 0.85, "compilation_complete"),
    (r"webpack.*compiled", 0.8, "webpack_compiled"),
    (r"vite.*built", 0.8, "vite_built"),
    (r"esbuild.*done", 0.8, "esbuild_done"),
    (r"tsc.*complete", 0.8, "typescript_complete"),
    (r"no errors", 0.75, "no_errors"),
    (r"0 error", 0.8, "zero_errors"),
    (r"✓.*passed", 0.8, "check_passed"),
    (r"✔", 0.7, "checkmark_success"),

    # =========================================================================
    # TEST SUCCESS
    # =========================================================================
    (r"\bpassed\b", 0.8, "tests_passed"),
    (r"tests? passed", 0.85, "tests_passed_explicit"),
    (r"\d+ passed", 0.85, "n_tests_passed"),
    (r"OK \(\d+ tests?\)", 0.85, "pytest_ok"),
    (r"PASSED", 0.85, "test_passed_caps"),
    (r"All tests passed", 0.9, "all_tests_passed"),
    (r"100%.*passed", 0.9, "all_tests_passed_pct"),
    (r"Coverage.*\d+%", 0.7, "coverage_reported"),
    (r"Ran \d+ tests? in", 0.75, "tests_ran"),
    (r"✓ \d+ tests?", 0.85, "mocha_tests_passed"),

    # =========================================================================
    # DEPLOYMENT SUCCESS
    # =========================================================================
    (r"\bdeployed\b", 0.8, "deployed"),
    (r"deployment.*complete", 0.85, "deployment_complete"),
    (r"deployment.*success", 0.9, "deployment_success"),
    (r"deployed to production", 0.9, "deployed_production"),
    (r"rollout.*complete", 0.85, "rollout_complete"),
    (r"release.*published", 0.85, "release_published"),
    (r"live at", 0.8, "live_at_url"),
    (r"available at http", 0.8, "available_at_url"),
    (r"Vercel.*Ready", 0.85, "vercel_ready"),
    (r"Netlify.*Published", 0.85, "netlify_published"),
    (r"AWS.*deployed", 0.85, "aws_deployed"),

    # =========================================================================
    # DATABASE SUCCESS
    # =========================================================================
    (r"migration.*complete", 0.85, "migration_complete"),
    (r"migrated", 0.8, "migrated"),
    (r"seeded", 0.8, "database_seeded"),
    (r"\d+ rows? (inserted|affected|updated)", 0.8, "rows_affected"),
    (r"INSERT.*\d+", 0.7, "insert_success"),
    (r"Query OK", 0.85, "mysql_query_ok"),
    (r"CREATE TABLE", 0.75, "table_created"),
    (r"ALTER TABLE.*success", 0.8, "alter_success"),

    # =========================================================================
    # FILE/FILESYSTEM SUCCESS
    # =========================================================================
    (r"\bcreated\b.*\bfile\b", 0.75, "file_created"),
    (r"File.*written", 0.8, "file_written"),
    (r"Saved", 0.7, "file_saved"),
    (r"copied", 0.7, "file_copied"),
    (r"moved", 0.7, "file_moved"),
    (r"chmod.*success", 0.75, "chmod_success"),
    (r"chown.*success", 0.75, "chown_success"),
    (r"directory created", 0.8, "directory_created"),

    # =========================================================================
    # PACKAGE MANAGER SUCCESS
    # =========================================================================
    (r"npm.*added \d+ packages?", 0.85, "npm_installed"),
    (r"pip.*Successfully installed", 0.85, "pip_installed"),
    (r"cargo.*Compiling.*Finished", 0.85, "cargo_built"),
    (r"go.*mod.*tidy", 0.75, "go_mod_tidy"),
    (r"yarn.*Done", 0.8, "yarn_done"),
    (r"pnpm.*Done", 0.8, "pnpm_done"),
    (r"packages? installed", 0.8, "packages_installed"),
    (r"dependencies.*installed", 0.8, "deps_installed"),

    # =========================================================================
    # CLOUD/INFRASTRUCTURE SUCCESS
    # =========================================================================
    (r"terraform.*Apply complete", 0.9, "terraform_applied"),
    (r"terraform.*Plan:.*to add", 0.75, "terraform_planned"),
    (r"Resources:.*created", 0.85, "resources_created"),
    (r"aws.*success", 0.8, "aws_success"),
    (r"gcloud.*success", 0.8, "gcloud_success"),
    (r"az.*success", 0.8, "azure_success"),
    (r"kubectl.*created", 0.8, "k8s_created"),
    (r"kubectl.*configured", 0.8, "k8s_configured"),
    (r"pod.*Running", 0.85, "pod_running"),
    (r"service.*LoadBalancer", 0.8, "lb_created"),

    # =========================================================================
    # SSL/SECURITY SUCCESS
    # =========================================================================
    (r"certificate.*issued", 0.85, "cert_issued"),
    (r"SSL.*valid", 0.85, "ssl_valid"),
    (r"https.*secure", 0.8, "https_secure"),
    (r"certbot.*success", 0.85, "certbot_success"),
    (r"key.*generated", 0.75, "key_generated"),

    # =========================================================================
    # MEMORY SYSTEM SUCCESS (Context DNA specific)
    # =========================================================================
    (r"Successfully", 0.75, "success_keyword"),
    (r"\bsuccessfully\b", 0.7, "successfully_keyword"),
    (r"Recorded SOP:", 0.9, "sop_recorded"),
    (r"Recorded Gotcha:", 0.9, "gotcha_recorded"),
    (r"Recorded Pattern:", 0.9, "pattern_recorded"),
    (r"Agent success recorded", 0.9, "agent_success_recorded"),
    (r"SOP extraction triggered", 0.85, "sop_extraction"),
    (r"brain.*100%", 0.9, "brain_100_percent"),
    (r"automation.*level.*100%", 0.9, "full_automation"),
    (r"5/5 core systems active", 0.9, "all_systems_active"),
    (r"Wins.*\d+", 0.75, "wins_recorded"),
    (r"Captured win", 0.85, "win_captured"),
    (r"Pattern learned", 0.85, "pattern_learned"),

    # =========================================================================
    # GENERIC SUCCESS INDICATORS
    # =========================================================================
    (r"\bcompleted\b", 0.65, "completed_keyword"),
    (r"\bupdated\b.*\bsuccessfully\b", 0.8, "update_success"),
    (r"Done\.", 0.65, "done_period"),
    (r"Finished", 0.7, "finished"),
    (r"Ready", 0.65, "ready"),
    (r"OK$", 0.7, "ok_terminal"),
    (r"✅", 0.8, "emoji_success"),
    (r"🎉", 0.75, "emoji_celebration"),
    (r"🚀", 0.7, "emoji_rocket"),
]

# System failure signals (CANCEL potential success)
SYSTEM_FAILURE_PATTERNS = [
    (r"\berror\b", "error_found"),
    (r"\bfailed\b", "failed_found"),
    (r"\bexception\b", "exception_found"),
    (r"\btimeout\b", "timeout_found"),
    (r"\bdenied\b", "denied_found"),
    (r"\brefused\b", "refused_found"),
    (r"exit[_ ]?code[:\s]*[1-9]", "nonzero_exit"),
    (r"\b(500|502|503|504)\b", "http_error"),
    (r"\bunhealthy\b", "unhealthy"),
    (r"\bcrash\b", "crashed"),
]

# Retry/fix indicators (suggest previous attempt failed)
RETRY_PATTERNS = [
    (r"\btry(ing)? again\b", "retry_detected"),
    (r"\blet me fix\b", "fix_detected"),
    (r"\blet me try\b", "retry_detected"),
    (r"\bone more time\b", "retry_detected"),
    (r"\bstill not\b", "still_failing"),
    (r"\bsame error\b", "same_error"),
]


# =============================================================================
# OBJECTIVE SUCCESS DATACLASS
# =============================================================================

@dataclass
class ObjectiveSuccess:
    """A verified objective success."""
    task: str
    confidence: float  # 0.0 to 1.0
    evidence: List[str]
    timestamp: str
    area: Optional[str] = None
    source: str = "detected"  # 'detected', 'user_confirmed', 'system_confirmed'


# =============================================================================
# SUCCESS DETECTION ENGINE
# =============================================================================

class ObjectiveSuccessDetector:
    """
    Analyzes work stream to extract ONLY objective successes.

    Key insight: We look at the SEQUENCE of events.
    - "Success!" followed by "Error" → NOT a success
    - "Done" followed by user "perfect" → SUCCESS
    - Exit 0 followed by retry → NOT a success
    """

    def __init__(self):
        self.pending_successes: List[Dict] = []
        self.confirmed_successes: List[ObjectiveSuccess] = []
        self.cancelled_tasks: set = set()

    def analyze_entries(self, entries: List[Dict]) -> List[ObjectiveSuccess]:
        """
        Analyze a sequence of work log entries and extract objective successes.

        This processes entries in ORDER to detect:
        - Potential successes
        - Confirmations (promote to confirmed)
        - Cancellations (remove from pending)
        """
        self.pending_successes = []
        self.confirmed_successes = []
        self.cancelled_tasks = set()

        for i, entry in enumerate(entries):
            self._process_entry(entry, entries, i)

        # Return only confirmed successes
        return self.confirmed_successes

    def _process_entry(self, entry: Dict, all_entries: List[Dict], index: int):
        """Process a single entry in context of the full stream."""
        content = entry.get("content", "").lower()
        entry_type = entry.get("entry_type", "")
        source = entry.get("source", "atlas")
        timestamp = entry.get("timestamp", "")

        # 1. Check for user confirmations (promotes pending to confirmed)
        if source == "user":
            self._check_user_confirmation(content, timestamp)
            self._check_user_rejection(content)

        # 2. Check for system success signals
        if entry_type in ("command", "observation"):
            self._check_system_success(entry, content, timestamp)

        # 3. Check for system failure signals (cancels pending)
        self._check_system_failure(content)

        # 4. Check for retry patterns (cancels recent pending)
        self._check_retry_patterns(content)

        # 5. Check for explicit success entries
        if entry_type == "success":
            self._add_pending_success(entry, content, timestamp, 0.5)

    def _check_user_confirmation(self, content: str, timestamp: str):
        """Check if user is confirming previous work."""
        for pattern, confidence, evidence_type in USER_CONFIRMATION_PATTERNS:
            if re.search(pattern, content, re.I):
                # Promote most recent pending success to confirmed
                if self.pending_successes:
                    pending = self.pending_successes.pop()
                    confirmed = ObjectiveSuccess(
                        task=pending["task"],
                        confidence=min(1.0, pending["confidence"] + confidence),
                        evidence=pending["evidence"] + [evidence_type],
                        timestamp=timestamp,
                        area=pending.get("area"),
                        source="user_confirmed"
                    )
                    self.confirmed_successes.append(confirmed)
                break

    def _check_user_rejection(self, content: str):
        """Check if user is rejecting previous work."""
        for pattern, _ in USER_REJECTION_PATTERNS:
            if re.search(pattern, content, re.I):
                # Cancel most recent pending success
                if self.pending_successes:
                    cancelled = self.pending_successes.pop()
                    self.cancelled_tasks.add(cancelled["task"][:50])
                    # Wire negative signal to evidence pipeline
                    try:
                        from memory.auto_capture import capture_failure
                        capture_failure(
                            task=cancelled.get('task', 'unknown'),
                            error=f'User rejection detected: {content[:100]}',
                            area=cancelled.get('area', 'general'),
                            root_cause='user_rejection'
                        )
                    except Exception:
                        pass
                break

    def _check_system_success(self, entry: Dict, content: str, timestamp: str):
        """Check for system success signals."""
        for pattern, confidence, evidence_type in SYSTEM_SUCCESS_PATTERNS:
            if re.search(pattern, content, re.I):
                # Extract task description
                task = self._extract_task_description(entry, content)
                if task:
                    self._add_pending_success(entry, task, timestamp, confidence, evidence_type)
                break

    def _check_system_failure(self, content: str):
        """Check for system failure signals."""
        for pattern, failure_reason in SYSTEM_FAILURE_PATTERNS:
            if re.search(pattern, content, re.I):
                # Cancel most recent pending success
                if self.pending_successes:
                    cancelled = self.pending_successes.pop()
                    self.cancelled_tasks.add(cancelled["task"][:50])
                    # Wire negative signal to evidence pipeline
                    try:
                        from memory.auto_capture import capture_failure
                        capture_failure(
                            task=cancelled.get('task', 'unknown'),
                            error=f'Detected: {failure_reason}',
                            area=cancelled.get('area', 'general'),
                            root_cause=failure_reason
                        )
                    except Exception:
                        pass
                break

    def _check_retry_patterns(self, content: str):
        """Check for retry patterns that indicate previous failure."""
        for pattern, _ in RETRY_PATTERNS:
            if re.search(pattern, content, re.I):
                # Cancel all recent pending (last 2)
                while self.pending_successes and len(self.pending_successes) > 0:
                    cancelled = self.pending_successes.pop()
                    self.cancelled_tasks.add(cancelled["task"][:50])
                    if len(self.pending_successes) <= 1:
                        break
                break

    def _add_pending_success(self, entry: Dict, task: str, timestamp: str,
                             confidence: float, evidence: str = "detected"):
        """Add a potential success to pending list."""
        # Don't add if already cancelled
        if task[:50] in self.cancelled_tasks:
            return

        # Don't add duplicates
        for pending in self.pending_successes:
            if pending["task"][:50] == task[:50]:
                # Update confidence if higher
                if confidence > pending["confidence"]:
                    pending["confidence"] = confidence
                    pending["evidence"].append(evidence)
                return

        self.pending_successes.append({
            "task": task,
            "confidence": confidence,
            "evidence": [evidence],
            "timestamp": timestamp,
            "area": entry.get("area")
        })

    def _extract_task_description(self, entry: Dict, content: str) -> Optional[str]:
        """Extract a meaningful task description from entry."""
        # Use entry content if short enough
        if len(content) < 100:
            return content

        # Try to extract first meaningful sentence
        sentences = content.split(".")
        if sentences:
            return sentences[0][:100]

        return content[:100]

    def get_high_confidence_successes(self, min_confidence: float = 0.7) -> List[ObjectiveSuccess]:
        """Get only high-confidence successes."""
        return [s for s in self.confirmed_successes if s.confidence >= min_confidence]

    def get_objective_successes_without_user(self, min_confidence: float = 0.7) -> List[ObjectiveSuccess]:
        """
        Get successes that are OBJECTIVELY confirmed by system signals,
        without requiring user confirmation.

        These are the "irrefutable evidence" wins - things like:
        - "Recorded SOP:" appearing in output
        - "5/5 core systems active"
        - "SOP extraction triggered"
        - Exit code 0 + no subsequent errors

        These should be auto-captured without waiting for user to say "worked".
        """
        objective_wins = []

        # Include pending successes with very high confidence
        for pending in self.pending_successes:
            if pending["confidence"] >= min_confidence:
                # Check if evidence is from system (not user patterns)
                evidence = pending.get("evidence", [])
                system_evidence = [e for e in evidence if e not in ["user_confirmed", "user_acknowledged"]]
                if system_evidence:
                    objective_wins.append(ObjectiveSuccess(
                        task=pending["task"],
                        confidence=pending["confidence"],
                        evidence=system_evidence,
                        timestamp=pending.get("timestamp", ""),
                        area=pending.get("area"),
                        source="system_confirmed"
                    ))

        return objective_wins


# =============================================================================
# INTEGRATION WITH WORK LOG
# =============================================================================

def analyze_for_successes(entries: List[Dict]) -> List[ObjectiveSuccess]:
    """
    Convenience function to analyze entries for objective successes.

    Args:
        entries: List of work log entries (from work_log.get_recent_entries())

    Returns:
        List of ObjectiveSuccess objects, sorted by confidence
    """
    detector = ObjectiveSuccessDetector()
    successes = detector.analyze_entries(entries)
    return sorted(successes, key=lambda s: s.confidence, reverse=True)


def get_objective_successes(hours: int = 24) -> List[ObjectiveSuccess]:
    """
    Get objective successes from the work log.

    This is the main entry point - call this to get verified successes.
    """
    try:
        from memory.architecture_enhancer import work_log
        entries = work_log.get_recent_entries(hours=hours, include_processed=True)
        return analyze_for_successes(entries)
    except ImportError:
        return []


def record_objective_successes():
    """
    Analyze work log and record objective successes to the brain.

    This should be called periodically to extract and record verified wins.
    """
    successes = get_objective_successes(hours=24)

    if not successes:
        return 0

    try:
        from memory.brain import brain
        recorded = 0

        for success in successes:
            if success.confidence >= 0.7:  # Only high-confidence
                brain.capture_win(
                    task=success.task,
                    details=f"Evidence: {', '.join(success.evidence)}",
                    area=success.area
                )
                recorded += 1

        return recorded
    except ImportError:
        return 0


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Objective Success Detector")
        print("")
        print("Extracts ONLY verified successes from work stream.")
        print("Filters out premature claims, cancelled attempts, and failures.")
        print("")
        print("Commands:")
        print("  analyze [hours]    - Analyze work log for objective successes")
        print("  record             - Record high-confidence successes to brain")
        print("  test               - Run detection on sample data")
        print("")
        print("What counts as objective success:")
        print("  - User confirms ('that worked', 'perfect')")
        print("  - System confirms (exit 0, healthy, 200 OK)")
        print("  - No subsequent failure, retry, or rollback")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "analyze":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        successes = get_objective_successes(hours)

        if not successes:
            print(f"No objective successes detected in last {hours} hours")
            sys.exit(0)

        print(f"=== Objective Successes (last {hours}h) ===\n")
        for s in successes:
            confidence_bar = "█" * int(s.confidence * 10) + "░" * (10 - int(s.confidence * 10))
            print(f"[{confidence_bar}] {s.confidence:.0%}")
            print(f"  Task: {s.task[:80]}")
            print(f"  Evidence: {', '.join(s.evidence)}")
            print(f"  Source: {s.source}")
            if s.area:
                print(f"  Area: {s.area}")
            print()

    elif cmd == "record":
        recorded = record_objective_successes()
        print(f"Recorded {recorded} high-confidence successes to brain")

    elif cmd == "test":
        # Test with sample data
        test_entries = [
            {"entry_type": "command", "content": "docker-compose up -d", "source": "atlas", "timestamp": "2024-01-15T10:00:00"},
            {"entry_type": "observation", "content": "Containers starting...", "source": "atlas", "timestamp": "2024-01-15T10:00:05"},
            {"entry_type": "observation", "content": "Error: port already in use", "source": "system", "timestamp": "2024-01-15T10:00:10"},
            {"entry_type": "command", "content": "docker-compose down && docker-compose up -d", "source": "atlas", "timestamp": "2024-01-15T10:01:00"},
            {"entry_type": "observation", "content": "All containers healthy", "source": "system", "timestamp": "2024-01-15T10:01:30"},
            {"entry_type": "dialogue", "content": "that worked perfectly", "source": "user", "timestamp": "2024-01-15T10:02:00"},
        ]

        print("=== Test Analysis ===\n")
        print("Input entries:")
        for e in test_entries:
            print(f"  [{e['entry_type']}] {e['content'][:50]}...")

        print("\nAnalysis:")
        successes = analyze_for_successes(test_entries)

        if successes:
            for s in successes:
                print(f"  ✓ {s.task} (confidence: {s.confidence:.0%})")
                print(f"    Evidence: {', '.join(s.evidence)}")
        else:
            print("  No objective successes detected")

        print("\nExpected: docker-compose up should be detected as success")
        print("         (first attempt cancelled by error, second confirmed by user)")

    elif cmd == "patterns":
        # Show all current patterns
        print("=== OBJECTIVE SUCCESS PATTERNS ===\n")
        print(f"Total patterns: {len(SYSTEM_SUCCESS_PATTERNS)}")
        print("\nBy category:")

        categories = {}
        for pattern, confidence, evidence in SYSTEM_SUCCESS_PATTERNS:
            # Extract category from evidence type
            category = evidence.split("_")[0] if "_" in evidence else "generic"
            if category not in categories:
                categories[category] = []
            categories[category].append((pattern, confidence, evidence))

        for cat, patterns in sorted(categories.items()):
            print(f"\n  {cat.upper()} ({len(patterns)} patterns):")
            for p, c, e in patterns[:5]:  # Show first 5
                print(f"    [{c:.2f}] {e}: {p[:40]}...")
            if len(patterns) > 5:
                print(f"    ... and {len(patterns) - 5} more")

    elif cmd == "discover":
        # Discover potential new patterns from work log
        print("=== PATTERN DISCOVERY ===\n")
        print("Analyzing work log for potential new success patterns...")

        try:
            from memory.architecture_enhancer import work_log
            entries = work_log.get_recent_entries(hours=168, include_processed=True)  # 1 week

            # Find entries that look like success but aren't in patterns
            potential_patterns = discover_new_patterns(entries)

            if potential_patterns:
                print(f"\nFound {len(potential_patterns)} potential new patterns:\n")
                for pattern_info in potential_patterns[:20]:
                    print(f"  Candidate: \"{pattern_info['text'][:60]}...\"")
                    print(f"  Frequency: {pattern_info['count']} occurrences")
                    print(f"  Suggested pattern: {pattern_info['suggested_regex']}")
                    print()
            else:
                print("No new patterns discovered")

        except ImportError:
            print("Work log not available")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


# =============================================================================
# AUTO-LEARNING: Discover New Objective Success Patterns
# =============================================================================

def discover_new_patterns(entries: List[Dict], min_frequency: int = 2) -> List[Dict]:
    """
    Discover potential new objective success patterns from work log entries.

    This analyzes entries that appear to indicate success but don't match
    existing patterns, finding repeated phrases that could be new patterns.

    Args:
        entries: Work log entries to analyze
        min_frequency: Minimum times a phrase must appear to be considered

    Returns:
        List of potential patterns with suggested regex
    """
    # Common success-indicating words/phrases to look for
    success_indicators = [
        r'\b(success|succeed|worked|complete|done|finished|ready|active|running|healthy)\b',
        r'\b(created|built|deployed|installed|configured|enabled|started)\b',
        r'\b(passed|approved|verified|validated|confirmed)\b',
        r'\b\d{3}\b',  # HTTP-like status codes
        r'✓|✅|🎉|👍|🚀',  # Emoji indicators
    ]

    # Track potential patterns
    phrase_counts = {}

    for entry in entries:
        content = entry.get("content", "")
        entry_type = entry.get("entry_type", "")

        # Skip user dialogue (those are handled separately)
        if entry.get("source") == "user":
            continue

        # Look for success-indicating content
        for indicator_pattern in success_indicators:
            if re.search(indicator_pattern, content, re.I):
                # Extract the relevant phrase (surrounding context)
                matches = list(re.finditer(indicator_pattern, content, re.I))
                for match in matches:
                    # Get surrounding context (10 chars before and after)
                    start = max(0, match.start() - 15)
                    end = min(len(content), match.end() + 15)
                    phrase = content[start:end].strip()

                    # Normalize the phrase
                    normalized = _normalize_for_pattern(phrase)

                    if normalized and len(normalized) > 5:
                        if normalized not in phrase_counts:
                            phrase_counts[normalized] = {
                                "text": phrase,
                                "count": 0,
                                "examples": []
                            }
                        phrase_counts[normalized]["count"] += 1
                        if len(phrase_counts[normalized]["examples"]) < 3:
                            phrase_counts[normalized]["examples"].append(content[:100])

    # Filter to patterns that appear frequently enough
    potential_patterns = []
    for normalized, info in phrase_counts.items():
        if info["count"] >= min_frequency:
            # Check if it matches existing patterns
            if not _matches_existing_pattern(info["text"]):
                suggested = _generate_regex(normalized)
                potential_patterns.append({
                    "text": info["text"],
                    "normalized": normalized,
                    "count": info["count"],
                    "examples": info["examples"],
                    "suggested_regex": suggested
                })

    # Sort by frequency
    potential_patterns.sort(key=lambda x: x["count"], reverse=True)

    return potential_patterns


def _normalize_for_pattern(phrase: str) -> str:
    """Normalize a phrase for pattern matching."""
    # Remove variable parts (numbers, hashes, IDs)
    normalized = re.sub(r'\b[a-f0-9]{8,}\b', '<HASH>', phrase)  # Git hashes, IDs
    normalized = re.sub(r'\b\d+\.\d+\.\d+\b', '<VERSION>', normalized)  # Versions
    normalized = re.sub(r'\b\d+\b', '<NUM>', normalized)  # Numbers
    normalized = re.sub(r'\s+', ' ', normalized)  # Collapse whitespace
    return normalized.strip().lower()


def _matches_existing_pattern(text: str) -> bool:
    """Check if text matches any existing pattern."""
    text_lower = text.lower()
    for pattern, _, _ in SYSTEM_SUCCESS_PATTERNS:
        try:
            if re.search(pattern, text_lower, re.I):
                return True
        except re.error:
            continue
    return False


def _generate_regex(normalized: str) -> str:
    """Generate a suggested regex from a normalized phrase."""
    # Escape special regex characters
    escaped = re.escape(normalized)

    # Replace placeholders with regex patterns
    escaped = escaped.replace(r'\<HASH\>', r'[a-f0-9]+')
    escaped = escaped.replace(r'\<VERSION\>', r'\d+\.\d+\.\d+')
    escaped = escaped.replace(r'\<NUM\>', r'\d+')

    # Add word boundaries
    escaped = r'\b' + escaped + r'\b'

    return escaped


def learn_pattern_from_success(success: ObjectiveSuccess) -> Optional[str]:
    """
    Learn a new pattern from a confirmed success.

    When a success is confirmed by user or has very high confidence,
    extract the pattern and suggest adding it to SYSTEM_SUCCESS_PATTERNS.

    Returns the suggested pattern or None if already covered.
    """
    task_lower = success.task.lower()

    # Check if already matched by existing pattern
    if _matches_existing_pattern(success.task):
        return None

    # Generate pattern from the success
    normalized = _normalize_for_pattern(success.task)
    suggested = _generate_regex(normalized)

    return suggested


# Export for use by pattern_registry.py
__all__ = [
    'ObjectiveSuccessDetector',
    'ObjectiveSuccess',
    'SYSTEM_SUCCESS_PATTERNS',
    'USER_CONFIRMATION_PATTERNS',
    'analyze_for_successes',
    'get_objective_successes',
    'discover_new_patterns',
    'learn_pattern_from_success',
]
