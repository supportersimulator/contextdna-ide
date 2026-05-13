#!/usr/bin/env python3
"""Context DNA CLI - Autonomous learning for developers.

Usage:
    context-dna init                    Initialize for this project
    context-dna setup                   Start Docker infrastructure
    context-dna serve                   Start the API server
    context-dna dashboard               Open the dashboard

    CAPTURE:
    context-dna win "title" "details"   Record a win (something that worked)
    context-dna fix "problem" "solution" Record a gotcha (something that broke)
    context-dna pattern "name" "desc"   Record a reusable pattern
    context-dna sop "title" "steps"     Record a Standard Operating Procedure

    QUERY:
    context-dna query "search terms"    Search learnings
    context-dna consult "task"          Get wisdom BEFORE starting work
    context-dna recent                  Show recent learnings
    context-dna status                  Show brain status and statistics

    AUTOMATION:
    context-dna cycle                   Run full brain cycle (auto-detect successes)
    context-dna detect                  Debug: show detected objective successes

    HOOKS:
    context-dna hooks install <ide>     Install IDE hooks (claude, cursor, git, all)
    context-dna hooks uninstall <ide>   Uninstall IDE hooks
    context-dna hooks status            Show hook installation status

    ADVANCED:
    context-dna upgrade                 Upgrade to Pro tier
    context-dna models                  List/manage LLM models
    context-dna providers               Show available LLM providers
    context-dna export                  Export learnings as JSON
    context-dna import <file>           Import learnings from JSON
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from context_dna import __version__
from context_dna.brain import Brain


def cmd_init(args, brain: Brain) -> int:
    """Initialize Context DNA."""
    print(f"Initializing Context DNA in {brain.project_dir}...")

    config = brain.init(
        backend=args.backend,
        project_name=args.name,
    )

    print(f"Created .context-dna/ directory")
    print(f"Project: {config['project']}")
    print(f"Backend: {config['storage']['backend']}")
    print()
    print("Context DNA is ready! Try:")
    print('  context-dna win "First learning" "This is how it started"')
    print('  context-dna query "learning"')

    return 0


def cmd_win(args, brain: Brain) -> int:
    """Record a win."""
    learning_id = brain.win(
        title=args.title,
        details=args.details or "",
        tags=args.tags.split(",") if args.tags else None,
    )

    print(f"Recorded win [{learning_id}]: {args.title}")
    return 0


def cmd_fix(args, brain: Brain) -> int:
    """Record a fix/gotcha."""
    learning_id = brain.fix(
        problem=args.problem,
        solution=args.solution,
        tags=args.tags.split(",") if args.tags else None,
    )

    print(f"Recorded fix [{learning_id}]: {args.problem}")
    return 0


def cmd_pattern(args, brain: Brain) -> int:
    """Record a pattern."""
    learning_id = brain.pattern(
        name=args.name,
        description=args.description,
        example=args.example or "",
        tags=args.tags.split(",") if args.tags else None,
    )

    print(f"Recorded pattern [{learning_id}]: {args.name}")
    return 0


def cmd_query(args, brain: Brain) -> int:
    """Search learnings."""
    results = brain.query(
        search=args.search,
        limit=args.limit,
        learning_type=args.type,
    )

    if not results:
        print(f"No results for: {args.search}")
        return 0

    print(f"Found {len(results)} result(s):\n")

    for r in results:
        print(f"[{r.type.value}] {r.title}")
        print(f"  ID: {r.id} | {r.created_at.strftime('%Y-%m-%d %H:%M')}")
        if r.content and r.content != r.title:
            # Show first 100 chars of content
            preview = r.content.replace("\n", " ")[:100]
            print(f"  {preview}...")
        if r.tags:
            print(f"  Tags: {', '.join(r.tags)}")
        print()

    return 0


def cmd_consult(args, brain: Brain) -> int:
    """Get wisdom before starting a task."""
    wisdom = brain.consult(args.task)
    print(wisdom)
    return 0


def cmd_status(args, brain: Brain) -> int:
    """Show brain status."""
    stats = brain.status()

    print("Context DNA Status")
    print("=" * 40)
    print(f"Project:     {stats['project']}")
    print(f"Backend:     {stats['backend']}")
    print(f"Healthy:     {'Yes' if stats['healthy'] else 'No'}")
    print()
    print(f"Total:       {stats['total']} learnings")
    print(f"Today:       {stats['today']}")
    print(f"Last:        {stats['last_capture'] or 'never'}")
    print()
    print("By Type:")
    for type_name, count in stats.get("by_type", {}).items():
        print(f"  {type_name}: {count}")

    return 0


def cmd_recent(args, brain: Brain) -> int:
    """Show recent learnings."""
    learnings = brain.recent(hours=args.hours, limit=args.limit)

    if not learnings:
        print(f"No learnings in the last {args.hours} hours")
        return 0

    print(f"Recent learnings ({len(learnings)}):\n")

    for r in learnings:
        print(f"[{r.type.value}] {r.title}")
        print(f"  {r.created_at.strftime('%Y-%m-%d %H:%M')}")
        print()

    return 0


def cmd_export(args, brain: Brain) -> int:
    """Export learnings as JSON."""
    json_data = brain.export()

    if args.output:
        Path(args.output).write_text(json_data)
        print(f"Exported to {args.output}")
    else:
        print(json_data)

    return 0


def cmd_import(args, brain: Brain) -> int:
    """Import learnings from JSON."""
    json_data = Path(args.file).read_text()
    count = brain.import_learnings(json_data)
    print(f"Imported {count} learnings")
    return 0


def cmd_hooks(args, brain: Brain) -> int:
    """Manage IDE hooks."""
    # Use the new HookInstaller
    try:
        from context_dna.hooks import HookInstaller
        installer = HookInstaller(str(brain.project_dir))
    except ImportError:
        # Fall back to built-in implementation
        if args.action == "install":
            return install_hooks_builtin(args.ide, brain)
        elif args.action == "status":
            return show_hooks_status(brain)
        else:
            print(f"Unknown hooks action: {args.action}")
            return 1

    if args.action == "install":
        if args.ide == "all":
            # Install all hooks
            results = installer.install_all()
            for hook, result in results.items():
                status = "OK" if result["success"] else "FAIL"
                print(f"[{status}] {hook}: {result['message']}")
                if result.get("path"):
                    print(f"       Path: {result['path']}")
            return 0 if all(r["success"] for r in results.values()) else 1
        elif args.ide:
            result = installer.install(args.ide)
            print(result["message"])
            if result.get("path"):
                print(f"Path: {result['path']}")
            return 0 if result["success"] else 1
        else:
            print("Specify hook to install: context-dna hooks install <claude|cursor|git|all>")
            return 1

    elif args.action == "uninstall":
        if args.ide:
            result = installer.uninstall(args.ide)
            print(result["message"])
            return 0 if result["success"] else 1
        else:
            print("Specify hook to uninstall: context-dna hooks uninstall <claude|cursor|git>")
            return 1

    elif args.action == "status":
        status = installer.status()
        print("=== Context DNA Hook Status ===")
        print(f"Project: {status['project_dir']}")
        print()

        for hook in ["claude", "cursor", "git"]:
            hook_status = status.get(hook, {})
            installed = hook_status.get("installed", False)
            print(f"{hook.title():8} {'[INSTALLED]' if installed else '[not installed]'}")
            if hook_status.get("path"):
                print(f"         Path: {hook_status['path']}")

        print()
        installed_list = installer.get_installed_hooks()
        print(f"Installed: {', '.join(installed_list) if installed_list else 'none'}")

        # Show detected IDEs
        detected = installer.detect_ide()
        if detected:
            print(f"Detected IDEs: {', '.join(detected)}")

        return 0

    else:
        print(f"Unknown hooks action: {args.action}")
        return 1


def install_hooks_builtin(ide: str, brain: Brain) -> int:
    """Install hooks for a specific IDE (built-in fallback)."""
    templates_dir = Path(__file__).parent.parent.parent.parent / "templates"

    if ide == "claude":
        return install_claude_hooks(brain, templates_dir)
    elif ide == "cursor":
        return install_cursor_hooks(brain, templates_dir)
    elif ide == "git":
        return install_git_hooks(brain, templates_dir)
    else:
        print(f"Unknown IDE: {ide}")
        print("Supported: claude, cursor, git")
        return 1


def install_claude_hooks(brain: Brain, templates_dir: Path) -> int:
    """Install Claude Code hooks."""
    claude_dir = brain.project_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)

    # Create settings.local.json
    settings_file = claude_dir / "settings.local.json"

    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "context-dna consult \"$PROMPT\" 2>/dev/null || true"
                        }
                    ]
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "context-dna _capture-bash \"$TOOL_INPUT\" \"$TOOL_RESPONSE\" 2>/dev/null &"
                        }
                    ]
                }
            ]
        }
    }

    # Merge with existing settings if present
    if settings_file.exists():
        existing = json.loads(settings_file.read_text())
        existing.setdefault("hooks", {}).update(settings["hooks"])
        settings = existing

    settings_file.write_text(json.dumps(settings, indent=2))

    print("Installed Claude Code hooks")
    print(f"  Created: {settings_file}")
    print()
    print("Restart Claude Code to activate hooks.")

    # Update config
    brain.config["hooks"]["claude"] = True
    with open(brain.config_file, "w") as f:
        json.dump(brain.config, f, indent=2)

    return 0


def install_cursor_hooks(brain: Brain, templates_dir: Path) -> int:
    """Install Cursor rules."""
    cursor_file = brain.project_dir / ".cursorrules"

    rules = """# Context DNA Integration

Before starting ANY task, consult Context DNA for relevant learnings:

```bash
context-dna consult "your task description"
```

After completing a task successfully, record it:

```bash
context-dna win "what you did" "how you did it"
```

After encountering and fixing a bug, record the gotcha:

```bash
context-dna fix "what went wrong" "how to fix it"
```

This project uses Context DNA for autonomous learning.
All learnings are stored in .context-dna/ (gitignored).
"""

    # Append to existing rules
    existing = ""
    if cursor_file.exists():
        existing = cursor_file.read_text()
        if "Context DNA" in existing:
            print("Cursor rules already contain Context DNA section")
            return 0

    with open(cursor_file, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n" + rules)

    print("Installed Cursor rules")
    print(f"  Updated: {cursor_file}")

    # Update config
    brain.config["hooks"]["cursor"] = True
    with open(brain.config_file, "w") as f:
        json.dump(brain.config, f, indent=2)

    return 0


def install_git_hooks(brain: Brain, templates_dir: Path) -> int:
    """Install git hooks."""
    git_dir = brain.project_dir / ".git"
    if not git_dir.exists():
        print("Not a git repository. Run 'git init' first.")
        return 1

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)

    # Post-commit hook
    post_commit = hooks_dir / "post-commit"
    hook_content = """#!/bin/bash
# Context DNA: Auto-capture git commits
context-dna _capture-commit 2>/dev/null &
"""

    # Append if exists, create if not
    if post_commit.exists():
        existing = post_commit.read_text()
        if "context-dna" in existing:
            print("Git post-commit hook already contains Context DNA")
        else:
            with open(post_commit, "a") as f:
                f.write("\n" + hook_content)
            print("Updated git post-commit hook")
    else:
        post_commit.write_text(hook_content)
        print("Created git post-commit hook")

    post_commit.chmod(0o755)

    # Update config
    brain.config["hooks"]["git"] = True
    with open(brain.config_file, "w") as f:
        json.dump(brain.config, f, indent=2)

    return 0


def show_hooks_status(brain: Brain) -> int:
    """Show hooks installation status."""
    print("Hook Status:")
    print()

    hooks = brain.config.get("hooks", {})
    for hook_name, installed in hooks.items():
        status = "Installed" if installed else "Not installed"
        print(f"  {hook_name}: {status}")

    print()
    print("Install hooks with: context-dna hooks install <ide>")
    print("Supported: claude, cursor, git")

    return 0


def cmd_capture_commit(args, brain: Brain) -> int:
    """Internal: Capture git commit (called by git hook)."""
    import subprocess

    try:
        # Get last commit info
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s|%b"],
            capture_output=True, text=True, cwd=brain.project_dir
        )

        if result.returncode != 0:
            return 1

        parts = result.stdout.split("|", 1)
        title = parts[0]
        body = parts[1] if len(parts) > 1 else ""

        # Record as win
        brain.win(
            title=f"Commit: {title}",
            details=body,
            metadata={"source": "git-commit"},
        )

    except Exception:
        pass  # Silently fail in hook

    return 0


def cmd_capture_bash(args, brain: Brain) -> int:
    """Internal: Capture bash command result (called by hook)."""
    # This is called by the PostToolUse hook
    # Capture command and output to work log for objective success detection
    try:
        cmd = args.tool_input or ""
        output = args.tool_response or ""

        if cmd:
            # Detect exit code from output if present
            exit_code = 0
            if "exit code" in output.lower():
                import re
                match = re.search(r'exit[_ ]?code[:\s]*(\d+)', output, re.IGNORECASE)
                if match:
                    exit_code = int(match.group(1))

            # Detect area from command
            area = None
            cmd_lower = cmd.lower()
            if any(x in cmd_lower for x in ["docker", "compose"]):
                area = "docker"
            elif any(x in cmd_lower for x in ["terraform", "tfstate"]):
                area = "terraform"
            elif any(x in cmd_lower for x in ["git ", "git-"]):
                area = "git"
            elif any(x in cmd_lower for x in ["npm", "yarn", "pnpm"]):
                area = "node"
            elif any(x in cmd_lower for x in ["pip", "python", "poetry"]):
                area = "python"
            elif any(x in cmd_lower for x in ["aws ", "ec2", "ecs", "lambda"]):
                area = "aws"

            # Capture to work log
            brain.capture_command(cmd, output, exit_code, area)

    except Exception:
        pass  # Silently fail in hook

    return 0


def cmd_cycle(args, brain: Brain) -> int:
    """Run full brain cycle (detect successes, consolidate, update state)."""
    print("Running Context DNA brain cycle...")
    print()

    result = brain.cycle()

    if result.get("objective_successes"):
        print(f"Objective Successes Detected: {len(result['objective_successes'])}")
        for s in result["objective_successes"][:5]:
            conf = s["confidence"]
            print(f"  [{conf:.0%}] {s['task'][:60]}")
        print()

    if result.get("successes_recorded", 0) > 0:
        print(f"Auto-recorded: {result['successes_recorded']} high-confidence successes")
        print()

    if result.get("consolidation"):
        c = result["consolidation"]
        print("Consolidation:")
        print(f"  Entries processed: {c.get('entries_processed', 0)}")
        print(f"  Patterns detected: {c.get('patterns_detected', 0)}")
        print(f"  Entries cleaned: {c.get('entries_cleaned', 0)}")
        print()

    if result.get("state_file"):
        print(f"State file updated: {result['state_file']}")

    if result.get("success"):
        print()
        print("Brain cycle complete.")
    else:
        print()
        print(f"Cycle completed with errors: {result.get('error')}")
        return 1

    return 0


def cmd_sop(args, brain: Brain) -> int:
    """Record a Standard Operating Procedure."""
    # Parse steps from comma-separated or numbered format
    steps_str = args.steps
    if "," in steps_str:
        steps = [s.strip() for s in steps_str.split(",")]
    else:
        # Try to parse numbered list
        import re
        steps = re.split(r'\d+\.\s*', steps_str)
        steps = [s.strip() for s in steps if s.strip()]

    if not steps:
        steps = [steps_str]

    warnings = args.warnings.split(",") if args.warnings else None
    tags = args.tags.split(",") if args.tags else None

    learning_id = brain.sop(
        title=args.title,
        steps=steps,
        when_to_use=args.when or "",
        warnings=warnings,
        tags=tags,
    )

    print(f"Recorded SOP [{learning_id}]: {args.title}")
    print(f"  Steps: {len(steps)}")
    if warnings:
        print(f"  Warnings: {len(warnings)}")
    return 0


def cmd_auto_learn(args, brain: Brain) -> int:
    """Internal: Auto-learn from git commits (called by git hook)."""
    import subprocess

    try:
        # Get last commit info
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%H|%s|%b|%an|%ae"],
            capture_output=True, text=True, cwd=brain.project_dir
        )

        if result.returncode != 0:
            return 1

        parts = result.stdout.split("|", 4)
        if len(parts) < 2:
            return 0

        commit_hash = parts[0]
        title = parts[1]
        body = parts[2] if len(parts) > 2 else ""
        author = parts[3] if len(parts) > 3 else ""

        # Detect commit type
        title_lower = title.lower()
        is_fix = title_lower.startswith(("fix:", "fix(", "bugfix:", "hotfix:"))
        is_feat = title_lower.startswith(("feat:", "feat(", "feature:"))
        is_perf = title_lower.startswith(("perf:", "perf(", "performance:"))
        is_refactor = title_lower.startswith(("refactor:", "refactor("))

        # Get files changed
        files_result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            capture_output=True, text=True, cwd=brain.project_dir
        )
        files = files_result.stdout.strip().split("\n") if files_result.stdout else []

        # Detect area from files
        area = None
        for f in files:
            if "docker" in f.lower() or "compose" in f.lower():
                area = "docker"
                break
            elif "terraform" in f.lower() or f.endswith(".tf"):
                area = "terraform"
                break
            elif "lambda" in f.lower():
                area = "aws"
                break

        # Record based on type
        if is_fix:
            # Extract problem from title, solution from body or diff
            problem = title.replace("fix:", "").replace("fix(", "").strip()
            solution = body or f"Commit {commit_hash[:8]}"
            brain.fix(problem, solution, area=area)
            print(f"Auto-learned fix: {problem[:50]}...")

        elif is_feat or is_perf or is_refactor:
            # Record as win
            brain.win(
                title=f"Commit: {title}",
                details=body or f"Changed files: {', '.join(files[:5])}",
                area=area,
                metadata={"commit": commit_hash, "files": files[:10]}
            )
            print(f"Auto-learned: {title[:50]}...")

        else:
            # Regular commit - still capture for context
            brain.win(
                title=f"Commit: {title}",
                details=body,
                metadata={"commit": commit_hash, "source": "git-auto"}
            )

    except Exception as e:
        # Fail gracefully in hook, but log for debugging
        print(f"[WARN] Git hook auto-capture failed: {e}")

    return 0


def cmd_detect_successes(args, brain: Brain) -> int:
    """Detect objective successes from work log (debugging command)."""
    hours = args.hours or 24
    min_conf = args.min_confidence or 0.5

    print(f"Analyzing work log for objective successes (last {hours}h)...")
    print()

    successes = brain.detect_objective_successes(hours=hours)

    if not successes:
        print("No successes detected in work log.")
        print()
        print("Make sure:")
        print("  1. Work log is capturing activity (check .context-dna/logs/)")
        print("  2. User confirmations are being recorded")
        print("  3. System outputs contain success indicators")
        return 0

    print(f"Found {len(successes)} potential success(es):\n")

    for s in successes:
        conf = s["confidence"]
        marker = "✓" if conf >= 0.7 else "?"
        print(f"  {marker} [{conf:.0%}] {s['task'][:60]}")
        if s.get("evidence"):
            print(f"      Evidence: {', '.join(s['evidence'][:3])}")
        print()

    # Summary
    high_conf = [s for s in successes if s["confidence"] >= 0.7]
    if high_conf:
        print(f"High-confidence ({len(high_conf)}): Will be auto-recorded by 'context-dna cycle'")
    else:
        print("No high-confidence successes. Threshold is 0.7.")

    return 0


def cmd_notify(args, brain: Brain) -> int:
    """Send system notification."""
    from context_dna.setup.notifications import notify

    if args.action == "test":
        success = notify(
            title="Context DNA",
            message="Notifications are working!",
            subtitle="Test notification",
            sound=True,
        )
        if success:
            print("Test notification sent successfully")
        else:
            print("Failed to send notification (check system permissions)")
        return 0 if success else 1

    elif args.action == "enable":
        # Update config to enable notifications
        brain.config["notifications"] = {"enabled": True}
        import json
        with open(brain.config_file, "w") as f:
            json.dump(brain.config, f, indent=2)
        print("Notifications enabled")
        return 0

    elif args.action == "disable":
        brain.config["notifications"] = {"enabled": False}
        import json
        with open(brain.config_file, "w") as f:
            json.dump(brain.config, f, indent=2)
        print("Notifications disabled")
        return 0

    return 1


def cmd_setup(args, brain: Brain) -> int:
    """Start Docker infrastructure or run setup wizard."""
    # Handle setup wizard
    if args.wizard:
        from context_dna.setup.wizard import SetupWizard
        wizard = SetupWizard()
        success = wizard.run()
        return 0 if success else 1

    # Handle setup status check
    if args.status:
        from context_dna.setup.checker import SetupChecker
        checker = SetupChecker()
        status = checker.check_all()

        print("Context DNA Setup Status")
        print("=" * 50)
        print()

        if status.is_complete:
            print("✅ All systems configured and ready!")
        else:
            print("⚠️  Setup incomplete")

        print()
        print("Components:")
        for item in status.items:
            if item.status.value == "ok":
                icon = "✅"
            elif item.status.value == "missing":
                icon = "❌"
            elif item.status.value == "partial":
                icon = "⚠️ "
            else:
                icon = "❓"

            print(f"  {icon} {item.name}: {item.message}")
            if item.fix_hint:
                print(f"      Fix: {item.fix_hint}")

        if status.issues:
            print()
            print("Issues:")
            for issue in status.issues:
                print(f"  ❌ {issue}")

        if status.warnings:
            print()
            print("Warnings:")
            for warning in status.warnings:
                print(f"  ⚠️  {warning}")

        return 0

    # Handle API key configuration
    if args.key == "key" and args.provider:
        from context_dna.setup.notifications import store_api_key_secure, get_api_key_setup_instructions

        provider = args.provider.lower()

        # Show security warning
        print()
        print("=" * 60)
        print("🔐 SECURITY WARNING")
        print("=" * 60)
        print(get_api_key_setup_instructions(provider))
        print("=" * 60)
        print()

        # Prompt for key
        import getpass
        try:
            key = getpass.getpass(f"Enter {provider.upper()} API key (will be hidden): ")
            if key:
                success = store_api_key_secure(provider, key)
                if success:
                    print(f"✅ {provider.upper()} key stored securely in system keychain")
                else:
                    print(f"❌ Failed to store key. Try setting {provider.upper()}_API_KEY environment variable instead.")
                return 0 if success else 1
            else:
                print("No key entered, aborting")
                return 1
        except KeyboardInterrupt:
            print("\nAborted")
            return 1

    # Default: Start Docker infrastructure
    print("Setting up Context DNA infrastructure...")
    print()

    # Find docker-compose.yml
    package_dir = Path(__file__).parent.parent.parent.parent
    compose_file = package_dir / "docker-compose.yml"

    if not compose_file.exists():
        print("Error: docker-compose.yml not found")
        print(f"Expected at: {compose_file}")
        return 1

    # Check if Docker is running
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            print("Error: Docker is not running. Please start Docker first.")
            return 1
    except FileNotFoundError:
        print("Error: Docker not found. Please install Docker first.")
        return 1

    # Start services
    services = ["postgres", "redis", "seaweedfs"]
    if args.with_ollama:
        services.append("ollama")

    print(f"Starting services: {', '.join(services)}")

    try:
        result = subprocess.run(
            ["docker-compose", "-f", str(compose_file), "up", "-d"] + services,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            print(f"Error starting services: {result.stderr}")
            return 1

        print("✓ Services started")

    except subprocess.TimeoutExpired:
        print("Error: Timeout starting services")
        return 1

    # Wait for services to be healthy
    print("Waiting for services to be healthy...")
    import time
    time.sleep(5)

    # Check health
    healthy = True
    for service in services:
        result = subprocess.run(
            ["docker-compose", "-f", str(compose_file), "ps", "-q", service],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print(f"  ✓ {service}")
        else:
            print(f"  ✗ {service}")
            healthy = False

    if healthy:
        print()
        print("Context DNA infrastructure is ready!")
        print()
        print("Next steps:")
        print("  1. Initialize in your project: context-dna init --backend pgvector")
        print("  2. Install hooks: context-dna hooks install claude")
        print("  3. Start learning: context-dna win 'First win' 'This worked!'")
    else:
        print()
        print("Some services failed to start. Check: docker-compose logs")

    return 0 if healthy else 1


def cmd_upgrade(args, brain: Brain) -> int:
    """Upgrade to Pro tier."""
    from context_dna.upgrade import UpgradeManager

    manager = UpgradeManager()

    if args.status:
        # Show upgrade status
        status = manager.check_status()
        print(f"Tier: {status['tier'].upper()}")
        if status['license_key']:
            print(f"License: {status['license_key'][:9]}...")
            print(f"Activated: {status['activated_at']}")
        print(f"Hardware: {status['hardware']['os']} {status['hardware']['arch']}")
        print(f"RAM: {status['hardware']['ram_gb']}GB")
        if status['hardware']['gpu']:
            print(f"GPU: {status['hardware']['gpu']}")
        if status['installed_models']:
            print(f"Models: {', '.join(status['installed_models'][:3])}")
        return 0

    # Run upgrade
    success = manager.upgrade(license_key=args.license)
    return 0 if success else 1


def cmd_models(args, brain: Brain) -> int:
    """Manage LLM models."""
    try:
        from context_dna.llm.ollama_provider import OllamaProvider
        ollama = OllamaProvider()
    except Exception as e:
        print(f"Error connecting to Ollama: {e}")
        print("Make sure Ollama is running: context-dna setup --with-ollama")
        return 1

    if args.action == "list":
        models = ollama.list_models()
        if models:
            print("Installed models:")
            for model in models:
                print(f"  - {model}")
        else:
            print("No models installed")
        return 0

    elif args.action == "pull":
        if not args.model:
            print("Specify model to pull: context-dna models pull llama3.1:8b")
            return 1

        # Check Pro tier for certain models
        from context_dna.upgrade import LicenseManager
        if not LicenseManager().is_pro():
            print("⭐ Model downloading requires Context DNA Pro")
            print("   Run: context-dna upgrade")
            return 1

        print(f"Pulling {args.model}...")
        for progress in ollama.pull_model(args.model):
            if progress.get("status") == "downloading":
                completed = progress.get("completed", 0)
                total = progress.get("total", 1)
                pct = (completed / total) * 100 if total else 0
                print(f"\r  Progress: {pct:.1f}%", end="", flush=True)
            elif progress.get("status") == "success":
                print(f"\r✓ {args.model} downloaded successfully")
            elif progress.get("status") == "error":
                print(f"\rError: {progress.get('error')}")
                return 1
        return 0

    elif args.action == "info":
        if not args.model:
            print("Specify model: context-dna models info llama3.1:8b")
            return 1

        info = ollama.model_info(args.model)
        if info:
            print(f"Model: {args.model}")
            print(f"Parameters: {info.get('parameters', 'unknown')}")
            print(f"Size: {info.get('size', 'unknown')}")
        else:
            print(f"Model not found: {args.model}")
            return 1
        return 0

    return 0


def cmd_providers(args, brain: Brain) -> int:
    """Show available LLM providers."""
    from context_dna.llm.manager import ProviderManager

    manager = ProviderManager()
    health = manager.health_check_all()

    print("LLM Providers:")
    print()

    from context_dna.llm.providers import PROVIDERS

    for name, config in PROVIDERS.items():
        status = "✓ Available" if health.get(name) else "✗ Unavailable"
        cost = "Free" if config.cost_per_1k_tokens == 0 else f"${config.cost_per_1k_tokens}/1K tokens"

        print(f"  {name}")
        print(f"    Model: {config.model}")
        print(f"    Status: {status}")
        print(f"    Cost: {cost}")
        if config.embedding_model:
            print(f"    Embeddings: {config.embedding_model}")
        print()

    # Show recommendation
    available = manager.list_available()
    if available:
        if "ollama" in available:
            print("Recommended: ollama (local, free)")
        elif "openai" in available:
            print("Recommended: openai (fast, affordable)")
    else:
        print("No providers available. Run:")
        print("  - context-dna setup --with-ollama  (for local LLM)")
        print("  - Set Context_DNA_OPENAI  (for cloud LLM)")

    return 0


def cmd_serve(args, brain: Brain) -> int:
    """Start the Context DNA API server."""
    from context_dna.server import run_server

    host = args.host or "127.0.0.1"
    port = args.port or 3456

    print("Starting Context DNA API server...")
    print()
    print("All visualization clients connect to this server:")
    print("  - Next.js Dashboard (http://localhost:3457)")
    print("  - xbar menu bar widget")
    print("  - VS Code extension")
    print("  - Raycast extension")
    print("  - Electron app")
    print()

    run_server(host=host, port=port, brain=brain)
    return 0


def cmd_dashboard(args, brain: Brain) -> int:
    """Open the Context DNA dashboard."""
    import webbrowser

    dashboard_url = args.url or "http://localhost:3457"
    api_url = "http://127.0.0.1:3456"

    # Check if API server is running
    try:
        import urllib.request
        urllib.request.urlopen(f"{api_url}/api/health", timeout=2)
    except Exception:
        print("Warning: API server not running.")
        print("Start it with: context-dna serve")
        print()

    if args.start_server:
        # Start both API server and dashboard
        print("Starting services...")

        # Start API server in background
        import threading
        from context_dna.server import run_server

        server_thread = threading.Thread(
            target=run_server,
            kwargs={"host": "127.0.0.1", "port": 3456, "brain": brain, "daemon": True},
            daemon=True,
        )
        server_thread.start()
        print("  API server starting on http://127.0.0.1:3456")

        # Check for Next.js dashboard
        dashboard_dir = Path(__file__).parent.parent.parent.parent / "dashboard"
        if dashboard_dir.exists():
            try:
                # Start Next.js in background
                subprocess.Popen(
                    ["npm", "run", "dev"],
                    cwd=dashboard_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print("  Dashboard starting on http://localhost:3457")
            except Exception as e:
                print(f"  Dashboard not started: {e}")
                print("  Run manually: cd dashboard && npm run dev")

        # Wait a moment for servers to start
        import time
        time.sleep(2)

    # Open browser
    print(f"Opening dashboard: {dashboard_url}")
    webbrowser.open(dashboard_url)

    return 0


def cmd_extras(args, brain: Brain) -> int:
    """Install visualization extras (xbar, VS Code, Raycast, Electron)."""
    from context_dna.extras.installer import ExtrasInstaller

    installer = ExtrasInstaller()

    if args.action == "list":
        print("Available extras:")
        print()
        for name, info in installer.EXTRAS.items():
            installed = installer.is_installed(name)
            status = "✓ Installed" if installed else "Not installed"
            print(f"  {name}: {info['description']}")
            print(f"    Status: {status}")
            print(f"    Platform: {info['platform']}")
            print()

        print("Install with: context-dna extras install <name>")
        return 0

    elif args.action == "install":
        if not args.extra:
            print("Specify extra to install: context-dna extras install xbar")
            print("Available: xbar, vscode, raycast, dashboard, electron")
            return 1

        success = installer.install(args.extra, force=args.force)
        return 0 if success else 1

    return 0


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="context-dna",
        description="Autonomous learning for developers",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"context-dna {__version__}"
    )
    parser.add_argument(
        "--project-dir",
        help="Project directory (default: current)",
        default=None,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # init
    p_init = subparsers.add_parser("init", help="Initialize Context DNA")
    p_init.add_argument("--backend", choices=["sqlite", "acontext"], default="sqlite")
    p_init.add_argument("--name", help="Project name")

    # win
    p_win = subparsers.add_parser("win", help="Record a win")
    p_win.add_argument("title", help="What worked")
    p_win.add_argument("details", nargs="?", help="How it worked")
    p_win.add_argument("--tags", help="Comma-separated tags")

    # fix
    p_fix = subparsers.add_parser("fix", help="Record a fix/gotcha")
    p_fix.add_argument("problem", help="What went wrong")
    p_fix.add_argument("solution", help="How to fix it")
    p_fix.add_argument("--tags", help="Comma-separated tags")

    # pattern
    p_pattern = subparsers.add_parser("pattern", help="Record a pattern")
    p_pattern.add_argument("name", help="Pattern name")
    p_pattern.add_argument("description", help="When/how to use")
    p_pattern.add_argument("--example", help="Code example")
    p_pattern.add_argument("--tags", help="Comma-separated tags")

    # query
    p_query = subparsers.add_parser("query", help="Search learnings")
    p_query.add_argument("search", help="Search terms")
    p_query.add_argument("--limit", type=int, default=10)
    p_query.add_argument("--type", choices=["win", "fix", "pattern", "insight", "sop"])

    # consult
    p_consult = subparsers.add_parser("consult", help="Get wisdom before work")
    p_consult.add_argument("task", help="What you're about to do")

    # status
    subparsers.add_parser("status", help="Show statistics")

    # recent
    p_recent = subparsers.add_parser("recent", help="Show recent learnings")
    p_recent.add_argument("--hours", type=int, default=24)
    p_recent.add_argument("--limit", type=int, default=20)

    # export
    p_export = subparsers.add_parser("export", help="Export learnings as JSON")
    p_export.add_argument("--output", "-o", help="Output file")

    # import
    p_import = subparsers.add_parser("import", help="Import learnings from JSON")
    p_import.add_argument("file", help="JSON file to import")

    # hooks
    p_hooks = subparsers.add_parser("hooks", help="Manage IDE hooks")
    p_hooks.add_argument("action", choices=["install", "uninstall", "status"])
    p_hooks.add_argument("ide", nargs="?", choices=["claude", "cursor", "git", "all"])

    # setup
    p_setup = subparsers.add_parser("setup", help="Start Docker infrastructure or run setup wizard")
    p_setup.add_argument("--with-ollama", action="store_true", help="Include Ollama for local LLM")
    p_setup.add_argument("--wizard", action="store_true", help="Run AI-assisted setup wizard")
    p_setup.add_argument("--status", action="store_true", help="Check setup status")
    p_setup.add_argument("key", nargs="?", help="Configure API key (openai, anthropic)")
    p_setup.add_argument("provider", nargs="?", help="Provider name when using 'key' subcommand")

    # notify
    p_notify = subparsers.add_parser("notify", help="Send system notification")
    p_notify.add_argument("action", choices=["test", "enable", "disable"], help="Notification action")

    # upgrade
    p_upgrade = subparsers.add_parser("upgrade", help="Upgrade to Pro tier")
    p_upgrade.add_argument("--license", help="License key (CDNA-XXXX-XXXX-XXXX-XXXX)")
    p_upgrade.add_argument("--status", action="store_true", help="Check upgrade status")

    # models
    p_models = subparsers.add_parser("models", help="Manage LLM models")
    p_models.add_argument("action", choices=["list", "pull", "info"], nargs="?", default="list")
    p_models.add_argument("model", nargs="?", help="Model name for pull/info")

    # providers
    subparsers.add_parser("providers", help="Show available LLM providers")

    # serve
    p_serve = subparsers.add_parser("serve", help="Start the API server")
    p_serve.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    p_serve.add_argument("--port", type=int, default=3456, help="Port to listen on")

    # dashboard
    p_dashboard = subparsers.add_parser("dashboard", help="Open the dashboard")
    p_dashboard.add_argument("--url", help="Dashboard URL (default: http://localhost:3457)")
    p_dashboard.add_argument("--start-server", action="store_true", help="Also start the API server")

    # extras
    p_extras = subparsers.add_parser("extras", help="Install visualization extras")
    p_extras.add_argument("action", choices=["list", "install"], nargs="?", default="list")
    p_extras.add_argument("extra", nargs="?", help="Extra to install (xbar, vscode, raycast, electron)")
    p_extras.add_argument("--force", action="store_true", help="Force reinstall")

    # cycle
    p_cycle = subparsers.add_parser("cycle", help="Run full brain cycle (auto-detect successes)")

    # sop
    p_sop = subparsers.add_parser("sop", help="Record a Standard Operating Procedure")
    p_sop.add_argument("title", help="SOP title")
    p_sop.add_argument("steps", help="Comma-separated steps or numbered list")
    p_sop.add_argument("--when", help="When to use this SOP")
    p_sop.add_argument("--warnings", help="Comma-separated warnings")
    p_sop.add_argument("--tags", help="Comma-separated tags")

    # detect (debugging)
    p_detect = subparsers.add_parser("detect", help="Detect objective successes (debugging)")
    p_detect.add_argument("--hours", type=int, default=24)
    p_detect.add_argument("--min-confidence", type=float, default=0.5)

    # Internal commands (used by hooks)
    p_cap_commit = subparsers.add_parser("_capture-commit")
    p_cap_bash = subparsers.add_parser("_capture-bash")
    p_cap_bash.add_argument("tool_input", nargs="?")
    p_cap_bash.add_argument("tool_response", nargs="?")

    # Internal: auto-learn from git
    p_auto_learn = subparsers.add_parser("auto-learn")
    p_auto_learn.add_argument("source", nargs="?", default="git-commit")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    # Create brain instance
    brain = Brain(project_dir=args.project_dir)

    # Route to command handler
    commands = {
        "init": cmd_init,
        "setup": cmd_setup,
        "win": cmd_win,
        "fix": cmd_fix,
        "pattern": cmd_pattern,
        "sop": cmd_sop,
        "query": cmd_query,
        "consult": cmd_consult,
        "status": cmd_status,
        "recent": cmd_recent,
        "cycle": cmd_cycle,
        "detect": cmd_detect_successes,
        "export": cmd_export,
        "import": cmd_import,
        "hooks": cmd_hooks,
        "upgrade": cmd_upgrade,
        "models": cmd_models,
        "providers": cmd_providers,
        "serve": cmd_serve,
        "dashboard": cmd_dashboard,
        "extras": cmd_extras,
        "_capture-commit": cmd_capture_commit,
        "_capture-bash": cmd_capture_bash,
        "auto-learn": cmd_auto_learn,
        "notify": cmd_notify,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args, brain)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
