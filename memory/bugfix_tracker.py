"""
BUGFIX TRACKER - Status Tracking for Bug-Fix SOPs

Tracks passed/failed status for bugfix SOPs, enabling:
- Living documents that track fix validity over time
- Regression detection when a fix stops working
- Automatic alerts when a bugfix needs attention

STATUS DATA STRUCTURE:
{
    "bugfix_id": "async_boto3_blocking_001",
    "title": "[bug-fix SOP] Async blocking in LLM: sync_call → asyncio.to_thread()",
    "symptom": "LLM service hanging",
    "fix": "Wrap boto3 calls in asyncio.to_thread()",
    "status": "passed",  # or "failed"
    "passed_count": 5,
    "failed_count": 0,
    "last_passed": "2024-01-15T10:30:00",
    "last_failed": null,
    "failure_history": [],  # List of failure notes with timestamps
    "created": "2024-01-01T00:00:00",
    "updated": "2024-01-15T10:30:00"
}

WORKFLOW:
1. Bugfix recorded → status = "passed", (passed MM/DD/YY) prefix
2. User reports regression → status = "failed", (failed MM/DD/YY) prefix
3. System alerts Atlas about regression
4. Atlas investigates and fixes → status = "passed" again

REGRESSION SIGNALS:
- User says "that fix didn't work", "it's broken again", "same error"
- Same symptom reoccurs
- Manual `brain.py bugfix fail` command
"""

import json
import os
import re
import hashlib
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from pathlib import Path

# =============================================================================
# CONFIGURATION
# =============================================================================

BUGFIX_DB_PATH = os.path.expanduser("~/.context-dna/bugfix_tracker.json")


# =============================================================================
# BUGFIX DATA STRUCTURES
# =============================================================================

def create_bugfix_entry(
    title: str,
    symptom: str = "",
    fix: str = "",
    is_passed: bool = True
) -> Dict:
    """Create a new bugfix entry."""
    bugfix_id = f"bugfix_{hashlib.md5(title.encode()).hexdigest()[:12]}"
    now = datetime.now().isoformat()

    return {
        "bugfix_id": bugfix_id,
        "title": title,
        "symptom": symptom,
        "fix": fix,
        "status": "passed" if is_passed else "failed",
        "passed_count": 1 if is_passed else 0,
        "failed_count": 0 if is_passed else 1,
        "last_passed": now if is_passed else None,
        "last_failed": None if is_passed else now,
        "failure_history": [],
        "created": now,
        "updated": now
    }


def normalize_bugfix_id(title: str) -> str:
    """Convert title to normalized bugfix identifier."""
    # Remove [bug-fix SOP] prefix if present
    clean = re.sub(r'^\[bug-fix SOP\]\s*', '', title, flags=re.IGNORECASE)
    # Remove status prefix if present
    clean = re.sub(r'^\((?:passed|failed)\s+\d{1,2}/\d{1,2}/\d{2}\)\s*', '', clean)
    # Lowercase, replace spaces/special chars with underscore
    normalized = re.sub(r'[^a-z0-9]+', '_', clean.lower())
    # Remove leading/trailing underscores
    normalized = normalized.strip('_')
    # Truncate to reasonable length
    return normalized[:50]


# =============================================================================
# PERSISTENCE
# =============================================================================

def load_bugfix_db() -> Dict:
    """Load the bugfix database from disk."""
    if not os.path.exists(BUGFIX_DB_PATH):
        return {"bugfixes": {}}

    try:
        with open(BUGFIX_DB_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"bugfixes": {}}


def save_bugfix_db(db: Dict) -> None:
    """Save the bugfix database to disk."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(BUGFIX_DB_PATH), exist_ok=True)

    with open(BUGFIX_DB_PATH, 'w') as f:
        json.dump(db, f, indent=2)


def get_bugfix_entry(title: str) -> Optional[Dict]:
    """Get bugfix entry by title, or None if not found."""
    db = load_bugfix_db()
    bugfix_id = normalize_bugfix_id(title)
    return db["bugfixes"].get(bugfix_id)


def get_bugfix_by_symptom(symptom: str) -> Optional[Dict]:
    """Find bugfix by symptom text (for regression detection)."""
    db = load_bugfix_db()
    symptom_lower = symptom.lower()

    for bugfix in db["bugfixes"].values():
        if symptom_lower in bugfix.get("symptom", "").lower():
            return bugfix
        if symptom_lower in bugfix.get("title", "").lower():
            return bugfix

    return None


# =============================================================================
# STATUS TRACKING
# =============================================================================

def format_date_short(iso_date: str) -> str:
    """Convert ISO date to MM/DD/YY format."""
    if not iso_date:
        return ""
    try:
        dt = datetime.fromisoformat(iso_date.replace('Z', '+00:00'))
        return dt.strftime("%m/%d/%y")
    except (ValueError, AttributeError):
        return ""


def record_bugfix_passed(
    title: str,
    symptom: str = "",
    fix: str = ""
) -> Dict:
    """
    Record a bugfix as passing (fix is working).

    Args:
        title: The bugfix title
        symptom: The original symptom
        fix: The fix that was applied

    Returns:
        Updated bugfix entry
    """
    db = load_bugfix_db()
    bugfix_id = normalize_bugfix_id(title)
    now = datetime.now().isoformat()

    if bugfix_id in db["bugfixes"]:
        # Update existing entry
        entry = db["bugfixes"][bugfix_id]
        entry["status"] = "passed"
        entry["passed_count"] += 1
        entry["last_passed"] = now
        entry["updated"] = now
        if fix and not entry["fix"]:
            entry["fix"] = fix
        if symptom and not entry["symptom"]:
            entry["symptom"] = symptom
    else:
        # Create new entry
        entry = create_bugfix_entry(title, symptom, fix, is_passed=True)
        db["bugfixes"][bugfix_id] = entry

    save_bugfix_db(db)
    return entry


def record_bugfix_failed(
    title: str,
    failure_note: str = "",
    symptom: str = ""
) -> Tuple[Dict, bool]:
    """
    Record a bugfix as failing (regression detected).

    Args:
        title: The bugfix title
        failure_note: What went wrong this time
        symptom: The symptom that reoccurred

    Returns:
        Tuple of (updated bugfix entry, is_regression)
        is_regression = True if this was previously passing
    """
    db = load_bugfix_db()
    bugfix_id = normalize_bugfix_id(title)
    now = datetime.now().isoformat()

    is_regression = False

    if bugfix_id in db["bugfixes"]:
        entry = db["bugfixes"][bugfix_id]
        # Check if this is a regression (was passing, now failing)
        is_regression = entry["status"] == "passed"

        entry["status"] = "failed"
        entry["failed_count"] += 1
        entry["last_failed"] = now
        entry["updated"] = now

        # Add to failure history
        if failure_note:
            entry["failure_history"].append({
                "timestamp": now,
                "note": failure_note,
                "is_regression": is_regression
            })
    else:
        # Create new entry as failed (unusual but possible)
        entry = create_bugfix_entry(title, symptom, "", is_passed=False)
        if failure_note:
            entry["failure_history"].append({
                "timestamp": now,
                "note": failure_note,
                "is_regression": False
            })
        db["bugfixes"][bugfix_id] = entry

    save_bugfix_db(db)
    return entry, is_regression


# =============================================================================
# FORMATTING
# =============================================================================

def format_bugfix_status_prefix(entry: Dict) -> str:
    """
    Generate the status prefix for a bugfix SOP.

    Format: (passed MM/DD/YY) or (failed MM/DD/YY)
    """
    status = entry.get("status", "passed")
    if status == "passed":
        date_str = format_date_short(entry.get("last_passed", ""))
        return f"(passed {date_str})" if date_str else "(passed)"
    else:
        date_str = format_date_short(entry.get("last_failed", ""))
        return f"(failed {date_str})" if date_str else "(failed)"


def format_bugfix_with_status(title: str) -> str:
    """
    Format a bugfix SOP title with status prefix.

    Input:  "[bug-fix SOP] Async blocking: sync_call → asyncio.to_thread()"
    Output: "(passed 01/23/26) [bug-fix SOP] Async blocking: sync_call → asyncio.to_thread()"
    """
    entry = get_bugfix_entry(title)

    # Remove any existing status prefix
    clean_title = re.sub(r'^\((?:passed|failed)\s+\d{1,2}/\d{1,2}/\d{2}\)\s*', '', title)

    if entry:
        prefix = format_bugfix_status_prefix(entry)
        return f"{prefix} {clean_title}"
    else:
        # No entry yet - assume passed with today's date
        today = datetime.now().strftime("%m/%d/%y")
        return f"(passed {today}) {clean_title}"


def add_status_to_new_bugfix(title: str, symptom: str = "", fix: str = "") -> str:
    """
    Add status tracking to a new bugfix and return formatted title.

    Call this when a new bugfix SOP is created.
    """
    entry = record_bugfix_passed(title, symptom, fix)
    prefix = format_bugfix_status_prefix(entry)

    # Remove any existing status prefix from title
    clean_title = re.sub(r'^\((?:passed|failed)\s+\d{1,2}/\d{1,2}/\d{2}\)\s*', '', title)

    return f"{prefix} {clean_title}"


# =============================================================================
# REGRESSION DETECTION
# =============================================================================

def get_failed_bugfixes() -> List[Dict]:
    """
    Get all bugfixes that are currently in failed state.

    Returns list of bugfix entries that need attention.
    """
    db = load_bugfix_db()
    return [
        entry for entry in db["bugfixes"].values()
        if entry["status"] == "failed"
    ]


def get_regressions() -> List[Dict]:
    """
    Get bugfixes that regressed (were passing, now failing).

    These are the most critical - fixes that stopped working.
    """
    db = load_bugfix_db()
    regressions = []

    for entry in db["bugfixes"].values():
        if entry["status"] == "failed" and entry["passed_count"] > 0:
            regressions.append(entry)

    return regressions


def check_for_regression(symptom_text: str) -> Optional[Dict]:
    """
    Check if a symptom matches a known bugfix (potential regression).

    Call this when an error occurs to see if it matches a previous fix.

    Args:
        symptom_text: The error/symptom observed

    Returns:
        Matching bugfix entry if found, None otherwise
    """
    return get_bugfix_by_symptom(symptom_text)


def generate_regression_alert(entry: Dict) -> str:
    """
    Generate an alert message for a regression.

    This is what Atlas should show to the user.
    """
    title = entry.get("title", "Unknown bugfix")
    symptom = entry.get("symptom", "Unknown symptom")
    fix = entry.get("fix", "Unknown fix")
    last_passed = format_date_short(entry.get("last_passed", ""))
    fail_count = entry.get("failed_count", 1)

    # Get most recent failure note
    failure_history = entry.get("failure_history", [])
    recent_note = failure_history[-1]["note"] if failure_history else "No details"

    alert = f"""
REGRESSION DETECTED

Bugfix: {title}
Status: FAILED (was passing on {last_passed})
Failed {fail_count} time(s)

Original symptom: {symptom}
Previous fix: {fix}

Most recent failure: {recent_note}

This bugfix needs attention. The original fix may no longer be working.
"""
    return alert.strip()


# =============================================================================
# CLI INTERFACE
# =============================================================================

def main():
    """CLI for testing bugfix tracker."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python bugfix_tracker.py <command> [args]")
        print("")
        print("Commands:")
        print("  passed <title> [symptom] [fix]  - Mark bugfix as passing")
        print("  failed <title> [note]           - Mark bugfix as failed (regression)")
        print("  show <title>                    - Show bugfix status")
        print("  list                            - List all tracked bugfixes")
        print("  regressions                     - Show bugfixes that regressed")
        print("  check <symptom>                 - Check if symptom matches known bugfix")
        print("  test                            - Run test cases")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "passed":
        if len(sys.argv) < 3:
            print("Usage: bugfix_tracker.py passed <title> [symptom] [fix]")
            sys.exit(1)
        title = sys.argv[2]
        symptom = sys.argv[3] if len(sys.argv) > 3 else ""
        fix = sys.argv[4] if len(sys.argv) > 4 else ""

        entry = record_bugfix_passed(title, symptom, fix)
        formatted = format_bugfix_with_status(title)
        print(f"Marked as PASSED: {title}")
        print(f"  Passed count: {entry['passed_count']}")
        print(f"  Formatted: {formatted}")

    elif cmd == "failed":
        if len(sys.argv) < 3:
            print("Usage: bugfix_tracker.py failed <title> [failure_note]")
            sys.exit(1)
        title = sys.argv[2]
        note = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""

        entry, is_regression = record_bugfix_failed(title, note)
        formatted = format_bugfix_with_status(title)

        if is_regression:
            print("REGRESSION DETECTED!")
            print(generate_regression_alert(entry))
        else:
            print(f"Marked as FAILED: {title}")

        print(f"\nFormatted: {formatted}")

    elif cmd == "show":
        if len(sys.argv) < 3:
            print("Usage: bugfix_tracker.py show <title>")
            sys.exit(1)
        title = sys.argv[2]

        entry = get_bugfix_entry(title)
        if entry:
            formatted = format_bugfix_with_status(title)
            print(f"Title: {formatted}")
            print(f"Status: {entry['status'].upper()}")
            print(f"Passed: {entry['passed_count']} times")
            print(f"Failed: {entry['failed_count']} times")
            if entry['symptom']:
                print(f"Symptom: {entry['symptom']}")
            if entry['fix']:
                print(f"Fix: {entry['fix']}")
            if entry['failure_history']:
                print(f"Failure history:")
                for h in entry['failure_history'][-3:]:
                    date = format_date_short(h['timestamp'])
                    print(f"  [{date}] {h['note']}")
        else:
            print(f"No bugfix tracked for: {title}")

    elif cmd == "list":
        db = load_bugfix_db()
        if not db["bugfixes"]:
            print("No bugfixes tracked yet.")
        else:
            print("Tracked Bugfixes:")
            print("")
            for bugfix_id, entry in db["bugfixes"].items():
                status_emoji = "pass" if entry["status"] == "passed" else "FAIL"
                print(f"  [{status_emoji}] {entry['title'][:60]}...")
                print(f"       Passed: {entry['passed_count']}, Failed: {entry['failed_count']}")
                print("")

    elif cmd == "regressions":
        regressions = get_regressions()
        if not regressions:
            print("No regressions detected. All bugfixes are passing.")
        else:
            print(f"REGRESSIONS DETECTED: {len(regressions)} bugfix(es) need attention")
            print("")
            for entry in regressions:
                print(generate_regression_alert(entry))
                print("-" * 50)

    elif cmd == "check":
        if len(sys.argv) < 3:
            print("Usage: bugfix_tracker.py check <symptom_text>")
            sys.exit(1)
        symptom = " ".join(sys.argv[2:])

        match = check_for_regression(symptom)
        if match:
            print(f"MATCH FOUND - This may be a regression!")
            print(f"  Bugfix: {match['title']}")
            print(f"  Status: {match['status']}")
            print(f"  Fix: {match.get('fix', 'Unknown')}")
        else:
            print("No matching bugfix found for this symptom.")

    elif cmd == "test":
        print("=== BUGFIX TRACKER TESTS ===\n")

        # Test bugfix
        test_title = "[bug-fix SOP] Test async blocking (TEST)"

        # Record as passed
        print("1. Recording bugfix as PASSED...")
        entry = record_bugfix_passed(test_title, "LLM hanging", "Use asyncio.to_thread()")
        formatted = format_bugfix_with_status(test_title)
        print(f"   {formatted}")
        print(f"   Passed count: {entry['passed_count']}")

        # Record another pass
        print("\n2. Recording another PASS...")
        entry = record_bugfix_passed(test_title)
        print(f"   Passed count: {entry['passed_count']}")

        # Record a failure (regression)
        print("\n3. Recording FAILURE (regression)...")
        entry, is_regression = record_bugfix_failed(test_title, "Still hanging after update")
        formatted = format_bugfix_with_status(test_title)
        print(f"   Is regression: {is_regression}")
        print(f"   {formatted}")

        if is_regression:
            print("\n   ALERT:")
            print(generate_regression_alert(entry))

        # Record fix again
        print("\n4. Recording PASSED again (fixed the regression)...")
        entry = record_bugfix_passed(test_title)
        formatted = format_bugfix_with_status(test_title)
        print(f"   {formatted}")

        # Cleanup
        db = load_bugfix_db()
        test_id = normalize_bugfix_id(test_title)
        if test_id in db["bugfixes"]:
            del db["bugfixes"][test_id]
            save_bugfix_db(db)
            print("\n(Test data cleaned up)")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
