#!/usr/bin/env python3
"""
Extract valuable conversation content from Claude Code session JSONL files.
Produces a summarized markdown document of all sessions, preserving key insights,
decisions, and outcomes while discarding tool results and file snapshots.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude/projects/-Users-aarontjomsland-Documents-er-simulator-superrepo"
OUTPUT_DIR = Path.home() / "dev/er-simulator-superrepo/session-archive"

# Sessions currently active (don't archive these)
ACTIVE_SESSIONS = set()

def get_active_session_ids():
    """Find sessions currently in use by running Claude processes."""
    import subprocess
    result = subprocess.run(
        ["ps", "aux"], capture_output=True, text=True
    )
    active = set()
    for line in result.stdout.split("\n"):
        if "--resume" in line:
            parts = line.split("--resume ")
            if len(parts) > 1:
                session_id = parts[1].split()[0].strip()
                active.add(session_id)
    return active


def extract_session_gold(jsonl_path, session_id):
    """Extract user messages and assistant text responses from a session."""
    messages = []
    msg_count = 0
    user_count = 0
    assistant_count = 0
    first_ts = None
    last_ts = None

    try:
        with open(jsonl_path, 'r', errors='replace') as f:
            for line_num, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get('type', '')

                # Skip non-message types
                if msg_type not in ('user', 'assistant'):
                    continue

                msg = obj.get('message', {})
                role = msg.get('role', '')
                content = msg.get('content', '')

                # Track timestamps
                ts = obj.get('timestamp')
                if ts:
                    if not first_ts:
                        first_ts = ts
                    last_ts = ts

                msg_count += 1

                # Extract text content
                text_parts = []
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if item.get('type') == 'text':
                                text = item.get('text', '')
                                # Skip system reminders and webhook injections (huge)
                                if '<system-reminder>' in text or len(text) > 5000:
                                    text_parts.append(text[:500] + "\n[... truncated ...]")
                                else:
                                    text_parts.append(text)
                            elif item.get('type') == 'tool_use':
                                tool_name = item.get('name', 'unknown')
                                tool_input = item.get('input', {})
                                # Just note what tool was used, not full input
                                if tool_name in ('Edit', 'Write'):
                                    file_path = tool_input.get('file_path', '?')
                                    text_parts.append(f"[Tool: {tool_name} → {file_path}]")
                                elif tool_name == 'Bash':
                                    cmd = tool_input.get('command', '?')[:200]
                                    text_parts.append(f"[Tool: Bash → {cmd}]")
                                elif tool_name == 'TodoWrite':
                                    todos = tool_input.get('todos', [])
                                    todo_text = "; ".join(t.get('content', '')[:80] for t in todos[:5])
                                    text_parts.append(f"[Todos: {todo_text}]")
                                else:
                                    text_parts.append(f"[Tool: {tool_name}]")

                combined = "\n".join(text_parts).strip()
                if not combined:
                    continue

                if role == 'user':
                    user_count += 1
                    messages.append(f"\n### USER ({user_count})\n{combined}")
                elif role == 'assistant':
                    assistant_count += 1
                    # Limit assistant messages to reasonable size
                    if len(combined) > 3000:
                        combined = combined[:3000] + "\n[... truncated ...]"
                    messages.append(f"\n### ATLAS ({assistant_count})\n{combined}")

    except Exception as e:
        return None, f"Error reading {jsonl_path}: {e}"

    if not messages:
        return None, "Empty session"

    # Build session summary
    file_size = os.path.getsize(jsonl_path)
    size_mb = file_size / (1024 * 1024)
    mod_time = datetime.fromtimestamp(os.path.getmtime(jsonl_path))

    header = f"""
## Session: {session_id[:8]}
- **Date**: {mod_time.strftime('%Y-%m-%d %H:%M')}
- **Size**: {size_mb:.1f}MB
- **Messages**: {user_count} user / {assistant_count} assistant
- **File**: {jsonl_path.name}

"""
    return header + "\n".join(messages), None


def count_subagents(session_id):
    """Count subagent files for a session."""
    subagent_dir = SESSIONS_DIR / session_id / "subagents"
    if subagent_dir.exists():
        return len(list(subagent_dir.glob("*.jsonl")))
    return 0


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    active_ids = get_active_session_ids()
    print(f"Active sessions (won't archive): {active_ids}")

    # Find all session files
    session_files = sorted(
        SESSIONS_DIR.glob("*.jsonl"),
        key=lambda p: os.path.getmtime(p),
        reverse=True
    )

    # Filter out agent files (subagent results at root level)
    session_files = [f for f in session_files if not f.stem.startswith("agent-")]

    print(f"Found {len(session_files)} sessions")

    # Categorize
    today = datetime.now().date()
    active = []
    recent = []  # last 2 days
    archivable = []

    for sf in session_files:
        session_id = sf.stem
        mod_date = datetime.fromtimestamp(os.path.getmtime(sf)).date()
        days_old = (today - mod_date).days
        size_mb = os.path.getsize(sf) / (1024 * 1024)
        subagents = count_subagents(session_id)

        info = {
            'path': sf,
            'id': session_id,
            'date': mod_date,
            'days_old': days_old,
            'size_mb': size_mb,
            'subagents': subagents,
        }

        if session_id in active_ids:
            active.append(info)
        elif days_old <= 1:
            recent.append(info)
        else:
            archivable.append(info)

    print(f"\nActive: {len(active)}")
    print(f"Recent (≤1 day): {len(recent)}")
    print(f"Archivable: {len(archivable)}")

    total_archivable_mb = sum(s['size_mb'] for s in archivable)
    print(f"Archivable size: {total_archivable_mb:.0f}MB")

    # Extract gold from archivable sessions (only ones >10KB — skip empty/trivial)
    gold_sessions = [s for s in archivable if s['size_mb'] > 0.01]
    gold_sessions.sort(key=lambda s: s['size_mb'], reverse=True)

    print(f"\nExtracting gold from {len(gold_sessions)} sessions...")

    # Write to markdown
    output_file = OUTPUT_DIR / f"session-gold-{today.isoformat()}.md"

    with open(output_file, 'w') as out:
        out.write(f"# Claude Code Session Archive\n")
        out.write(f"Extracted: {datetime.now().isoformat()}\n")
        out.write(f"Sessions: {len(gold_sessions)}\n")
        out.write(f"Total raw size: {total_archivable_mb:.0f}MB\n\n")
        out.write("---\n\n")

        # Index
        out.write("## Index\n\n")
        out.write("| # | Session | Date | Size | Msgs | Subagents |\n")
        out.write("|---|---------|------|------|------|-----------|\n")

        for i, s in enumerate(gold_sessions, 1):
            out.write(f"| {i} | {s['id'][:8]} | {s['date']} | {s['size_mb']:.1f}MB | - | {s['subagents']} |\n")

        out.write("\n---\n\n")

        # Extract each session
        for i, s in enumerate(gold_sessions, 1):
            print(f"  [{i}/{len(gold_sessions)}] {s['id'][:8]} ({s['size_mb']:.1f}MB, {s['subagents']} subagents)...", end=" ")

            gold, error = extract_session_gold(s['path'], s['id'])
            if error:
                print(f"SKIP: {error}")
                continue

            out.write(gold)
            out.write("\n\n---\n\n")
            print("OK")

    output_size = os.path.getsize(output_file) / (1024 * 1024)
    print(f"\nGold extracted to: {output_file}")
    print(f"Output size: {output_size:.1f}MB (from {total_archivable_mb:.0f}MB raw)")
    print(f"Compression: {(1 - output_size/max(total_archivable_mb, 0.1))*100:.0f}% reduction")

    # Also write a manifest for cleanup
    manifest_file = OUTPUT_DIR / f"cleanup-manifest-{today.isoformat()}.json"
    manifest = {
        'extracted': datetime.now().isoformat(),
        'active_sessions': list(active_ids),
        'recent_sessions': [s['id'] for s in recent],
        'archivable_sessions': [
            {
                'id': s['id'],
                'date': s['date'].isoformat(),
                'size_mb': round(s['size_mb'], 1),
                'subagents': s['subagents'],
                'path': str(s['path']),
            }
            for s in archivable
        ],
        'total_archivable_mb': round(total_archivable_mb, 1),
    }

    with open(manifest_file, 'w') as f:
        json.dump(manifest, f, indent=2)

    print(f"Cleanup manifest: {manifest_file}")
    print(f"\nNext steps:")
    print(f"  1. Review {output_file}")
    print(f"  2. Archive raw data: tar czf ~/session-archive.tar.gz -C ~/.claude/projects/ .")
    print(f"  3. Delete old sessions (see manifest)")


if __name__ == "__main__":
    main()
