"""
ROUTE TRACKER - Multi-Route Accumulation for Process SOPs

Tracks successful and failed routes for process SOPs, enabling:
- Living documents that grow with each documented route
- Preference ordering by success rate and first-try reliability
- Progressive detail on failed routes (phrase → context → full)

ROUTE DATA STRUCTURE:
{
    "sop_id": "deploy_django_production",  # Normalized SOP identifier
    "goal": "Deploy Django to production",
    "routes": [
        {
            "id": "route_systemctl_001",
            "description": "via systemctl restart gunicorn",
            "chain": "via (systemctl) → deploy → restart → healthy",
            "success_count": 5,
            "fail_count": 0,
            "first_try_success": 5,  # Succeeded on first attempt
            "first_try_total": 5,    # Total first attempts
            "last_success": "2024-01-15T10:30:00",
            "last_failure": null,
            "failure_notes": []      # Progressive detail on failures
        },
        {
            "id": "route_docker_002",
            "description": "via docker rebuild",
            "chain": "via (docker) → rebuild → deploy → healthy",
            "success_count": 2,
            "fail_count": 3,
            "first_try_success": 1,
            "first_try_total": 5,
            "last_success": "2024-01-10T14:20:00",
            "last_failure": "2024-01-14T09:15:00",
            "failure_notes": [
                "loses env vars",              # 1st fail: phrase
                "requires docker-compose up",  # 2nd fail: context
                "must recreate not restart - docker restart doesn't reload env"  # 3rd: full
            ]
        }
    ],
    "created": "2024-01-01T00:00:00",
    "updated": "2024-01-15T10:30:00"
}

PREFERENCE SCORING:
Routes are ordered by: first_try_success_rate * 0.6 + overall_success_rate * 0.4
This prioritizes routes that work on first attempt (reliability) while considering
overall success rate (robustness).
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

ROUTES_DB_PATH = os.path.expanduser("~/.context-dna/route_tracker.json")


# =============================================================================
# ROUTE DATA STRUCTURES
# =============================================================================

def create_route(
    description: str,
    chain: str = "",
    is_success: bool = True,
    failure_note: str = ""
) -> Dict:
    """Create a new route entry."""
    route_id = f"route_{hashlib.md5(description.encode()).hexdigest()[:8]}"
    now = datetime.now().isoformat()

    route = {
        "id": route_id,
        "description": description,
        "chain": chain,
        "success_count": 1 if is_success else 0,
        "fail_count": 0 if is_success else 1,
        "first_try_success": 1 if is_success else 0,
        "first_try_total": 1,
        "last_success": now if is_success else None,
        "last_failure": None if is_success else now,
        "failure_notes": [] if is_success else [failure_note] if failure_note else []
    }
    return route


def create_sop_entry(goal: str) -> Dict:
    """Create a new SOP entry with empty routes."""
    sop_id = normalize_sop_id(goal)
    now = datetime.now().isoformat()

    return {
        "sop_id": sop_id,
        "goal": goal,
        "routes": [],
        "created": now,
        "updated": now
    }


def normalize_sop_id(goal: str) -> str:
    """Convert goal to normalized SOP identifier."""
    # Lowercase, replace spaces/special chars with underscore
    normalized = re.sub(r'[^a-z0-9]+', '_', goal.lower())
    # Remove leading/trailing underscores
    normalized = normalized.strip('_')
    # Truncate to reasonable length
    return normalized[:50]


# =============================================================================
# PREFERENCE SCORING
# =============================================================================

def calculate_route_score(route: Dict) -> float:
    """
    Calculate preference score for a route.

    Score = first_try_success_rate * 0.6 + overall_success_rate * 0.4

    Higher scores = more preferred routes (reliable, high success rate)
    """
    total_attempts = route["success_count"] + route["fail_count"]
    if total_attempts == 0:
        return 0.0

    # Overall success rate
    overall_rate = route["success_count"] / total_attempts

    # First-try success rate (more important for preference)
    first_try_total = route.get("first_try_total", 1)
    first_try_success = route.get("first_try_success", route["success_count"])
    first_try_rate = first_try_success / first_try_total if first_try_total > 0 else 0

    # Weighted score: first-try reliability matters more
    score = (first_try_rate * 0.6) + (overall_rate * 0.4)

    return round(score * 100, 1)  # Return as percentage


def sort_routes_by_preference(routes: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Split routes into successful and failed, sort by preference.

    Returns:
        (successful_routes, failed_routes) - both sorted by score descending
    """
    successful = []
    failed = []

    for route in routes:
        if route["success_count"] > 0:
            successful.append(route)
        else:
            failed.append(route)

    # Sort successful by preference score (highest first)
    successful.sort(key=lambda r: calculate_route_score(r), reverse=True)

    # Sort failed by fail_count (most failures = most documented = at top)
    failed.sort(key=lambda r: r["fail_count"], reverse=True)

    return successful, failed


# =============================================================================
# PERSISTENCE
# =============================================================================

def load_routes_db() -> Dict:
    """Load the routes database from disk."""
    if not os.path.exists(ROUTES_DB_PATH):
        return {"sops": {}}

    try:
        with open(ROUTES_DB_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"sops": {}}


def save_routes_db(db: Dict) -> None:
    """Save the routes database to disk."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(ROUTES_DB_PATH), exist_ok=True)

    with open(ROUTES_DB_PATH, 'w') as f:
        json.dump(db, f, indent=2)


def get_sop_entry(goal: str) -> Optional[Dict]:
    """Get SOP entry by goal, or None if not found."""
    db = load_routes_db()
    sop_id = normalize_sop_id(goal)
    return db["sops"].get(sop_id)


def get_or_create_sop_entry(goal: str) -> Dict:
    """Get existing SOP entry or create new one."""
    db = load_routes_db()
    sop_id = normalize_sop_id(goal)

    if sop_id not in db["sops"]:
        db["sops"][sop_id] = create_sop_entry(goal)
        save_routes_db(db)

    return db["sops"][sop_id]


# =============================================================================
# ROUTE RECORDING
# =============================================================================

def record_route_success(
    goal: str,
    route_description: str,
    chain: str = "",
    is_first_try: bool = True
) -> Dict:
    """
    Record a successful route for a process SOP.

    Args:
        goal: The SOP goal (e.g., "Deploy Django to production")
        route_description: Brief description of the route (e.g., "via systemctl restart")
        chain: Full chain format (e.g., "via (systemctl) -> restart -> healthy")
        is_first_try: Whether this succeeded on first attempt

    Returns:
        Updated route entry
    """
    db = load_routes_db()
    sop_id = normalize_sop_id(goal)

    # Ensure SOP exists
    if sop_id not in db["sops"]:
        db["sops"][sop_id] = create_sop_entry(goal)

    sop = db["sops"][sop_id]
    now = datetime.now().isoformat()

    # Find existing route by description
    existing_route = None
    for route in sop["routes"]:
        if route["description"].lower() == route_description.lower():
            existing_route = route
            break

    if existing_route:
        # Update existing route
        existing_route["success_count"] += 1
        existing_route["last_success"] = now
        if chain and not existing_route["chain"]:
            existing_route["chain"] = chain
        if is_first_try:
            existing_route["first_try_success"] += 1
            existing_route["first_try_total"] += 1
        result = existing_route
    else:
        # Create new route
        new_route = create_route(route_description, chain, is_success=True)
        sop["routes"].append(new_route)
        result = new_route

    sop["updated"] = now
    save_routes_db(db)

    return result


def record_route_failure(
    goal: str,
    route_description: str,
    failure_note: str = "",
    chain: str = ""
) -> Dict:
    """
    Record a failed route for a process SOP.

    Progressive detail is automatically managed:
    - 1st failure: phrase only
    - 2nd failure: adds context
    - 3rd+ failure: full details

    Args:
        goal: The SOP goal
        route_description: Brief description of the route that failed
        failure_note: What went wrong (detail level adjusts automatically)
        chain: Full chain format if known

    Returns:
        Updated route entry
    """
    db = load_routes_db()
    sop_id = normalize_sop_id(goal)

    # Ensure SOP exists
    if sop_id not in db["sops"]:
        db["sops"][sop_id] = create_sop_entry(goal)

    sop = db["sops"][sop_id]
    now = datetime.now().isoformat()

    # Find existing route by description
    existing_route = None
    for route in sop["routes"]:
        if route["description"].lower() == route_description.lower():
            existing_route = route
            break

    if existing_route:
        # Update existing route
        existing_route["fail_count"] += 1
        existing_route["last_failure"] = now
        existing_route["first_try_total"] += 1

        # Add failure note with progressive detail
        if failure_note:
            fail_count = existing_route["fail_count"]
            # Truncate based on failure count (progressive detail)
            if fail_count == 1:
                # First failure: phrase only (max 30 chars)
                note = failure_note[:30].split('.')[0]
            elif fail_count == 2:
                # Second failure: add context (max 80 chars)
                note = failure_note[:80]
            else:
                # Third+ failure: full details
                note = failure_note

            if note not in existing_route["failure_notes"]:
                existing_route["failure_notes"].append(note)

        result = existing_route
    else:
        # Create new failed route
        new_route = create_route(route_description, chain, is_success=False, failure_note=failure_note[:30] if failure_note else "")
        sop["routes"].append(new_route)
        result = new_route

    sop["updated"] = now
    save_routes_db(db)

    return result


# =============================================================================
# ROUTE FORMATTING
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


def format_routes_for_sop(goal: str) -> str:
    """
    Format all routes for a process SOP in the multi-route display format.

    Output format:

    [process SOP] Deploy Django to production:
      (passed 01/23/26) Route 1 (95%): via (systemctl) -> restart -> healthy
      (passed 01/20/26) Route 2 (80%): via (docker) -> rebuild -> deploy -> healthy
      ----------------------------------------
      (failed 01/22/26) docker restart - loses env vars
      (failed 01/21/26) manual copy - permission issues, requires sudo

    Args:
        goal: The SOP goal to format routes for

    Returns:
        Formatted multi-route string, or empty string if no routes
    """
    sop = get_sop_entry(goal)
    if not sop or not sop["routes"]:
        return ""

    successful, failed = sort_routes_by_preference(sop["routes"])

    lines = []

    # === SUCCESSFUL ROUTES ===
    for i, route in enumerate(successful, 1):
        score = calculate_route_score(route)
        chain = route["chain"] if route["chain"] else route["description"]
        date_str = format_date_short(route.get("last_success", ""))
        date_prefix = f"(passed {date_str}) " if date_str else "(passed) "
        lines.append(f"  {date_prefix}Route {i} ({score:.0f}%): {chain}")

    # === SEPARATOR ===
    if successful and failed:
        lines.append("  " + "-" * 40)

    # === FAILED ROUTES ===
    for route in failed:
        desc = route["description"]
        fail_count = route["fail_count"]
        date_str = format_date_short(route.get("last_failure", ""))
        date_prefix = f"(failed {date_str}) " if date_str else "(failed) "

        # Progressive detail based on fail count
        if route["failure_notes"]:
            if fail_count == 1:
                # Single phrase
                detail = route["failure_notes"][0][:30]
            elif fail_count == 2:
                # With context
                detail = route["failure_notes"][-1][:80] if len(route["failure_notes"]) > 1 else route["failure_notes"][0]
            else:
                # Full details (join all notes)
                detail = "; ".join(route["failure_notes"])

            lines.append(f"  {date_prefix}{desc} - {detail}")
        else:
            lines.append(f"  {date_prefix}{desc}")

    return "\n".join(lines)


def format_sop_with_routes(goal: str, primary_chain: str = "") -> str:
    """
    Format complete process SOP with goal header and multi-route body.

    Args:
        goal: The SOP goal
        primary_chain: Optional chain for the main/current route

    Returns:
        Complete formatted SOP string
    """
    routes_section = format_routes_for_sop(goal)

    if routes_section:
        return f"[process SOP] {goal}:\n{routes_section}"
    elif primary_chain:
        return f"[process SOP] {goal}: {primary_chain}"
    else:
        return f"[process SOP] {goal}"


# =============================================================================
# CLI INTERFACE
# =============================================================================

def main():
    """CLI for testing route tracker."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python route_tracker.py <command> [args]")
        print("")
        print("Commands:")
        print("  success <goal> <route> [chain]   Record successful route")
        print("  fail <goal> <route> [note]       Record failed route")
        print("  show <goal>                      Show routes for SOP")
        print("  list                             List all tracked SOPs")
        print("  test                             Run test cases")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "success":
        if len(sys.argv) < 4:
            print("Usage: route_tracker.py success <goal> <route_description> [chain]")
            sys.exit(1)
        goal = sys.argv[2]
        route_desc = sys.argv[3]
        chain = sys.argv[4] if len(sys.argv) > 4 else ""

        result = record_route_success(goal, route_desc, chain)
        print(f"Recorded success: {route_desc}")
        print(f"  Success count: {result['success_count']}")
        print(f"  Score: {calculate_route_score(result)}%")

    elif cmd == "fail":
        if len(sys.argv) < 4:
            print("Usage: route_tracker.py fail <goal> <route_description> [failure_note]")
            sys.exit(1)
        goal = sys.argv[2]
        route_desc = sys.argv[3]
        note = sys.argv[4] if len(sys.argv) > 4 else ""

        result = record_route_failure(goal, route_desc, note)
        print(f"Recorded failure: {route_desc}")
        print(f"  Fail count: {result['fail_count']}")
        print(f"  Notes: {result['failure_notes']}")

    elif cmd == "show":
        if len(sys.argv) < 3:
            print("Usage: route_tracker.py show <goal>")
            sys.exit(1)
        goal = sys.argv[2]

        formatted = format_sop_with_routes(goal)
        if formatted:
            print(formatted)
        else:
            print(f"No routes tracked for: {goal}")

    elif cmd == "list":
        db = load_routes_db()
        if not db["sops"]:
            print("No SOPs tracked yet.")
        else:
            print("Tracked SOPs:")
            for sop_id, sop in db["sops"].items():
                route_count = len(sop["routes"])
                successful = sum(1 for r in sop["routes"] if r["success_count"] > 0)
                print(f"  {sop['goal']}")
                print(f"    Routes: {route_count} ({successful} successful)")

    elif cmd == "test":
        print("=== ROUTE TRACKER TESTS ===\n")

        # Test goal
        test_goal = "Deploy Django to production (TEST)"

        # Record some successes
        print("Recording successes...")
        record_route_success(test_goal, "via systemctl restart", "via (systemctl) -> restart -> healthy")
        record_route_success(test_goal, "via systemctl restart", "via (systemctl) -> restart -> healthy")
        record_route_success(test_goal, "via systemctl restart", "via (systemctl) -> restart -> healthy")
        record_route_success(test_goal, "via docker rebuild", "via (docker) -> rebuild -> deploy -> healthy")

        # Record some failures
        print("Recording failures...")
        record_route_failure(test_goal, "docker restart", "loses env vars")
        record_route_failure(test_goal, "docker restart", "requires docker-compose up instead")
        record_route_failure(test_goal, "docker restart", "must recreate not restart - docker restart doesn't reload .env file contents")
        record_route_failure(test_goal, "manual file copy", "permission denied")

        # Show result
        print("\n" + "=" * 50)
        print(format_sop_with_routes(test_goal))
        print("=" * 50)

        # Cleanup test data
        db = load_routes_db()
        test_sop_id = normalize_sop_id(test_goal)
        if test_sop_id in db["sops"]:
            del db["sops"][test_sop_id]
            save_routes_db(db)
            print("\n(Test data cleaned up)")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
