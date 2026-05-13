"""
First Experience for Context DNA Vibe Coder Launch

Provides a guided first experience after setup:
- Opens VS Code in the configured workspace
- Verifies webhook is active
- Shows guided first prompt suggestions
- Celebrates success

Created: January 29, 2026
Part of: Vibe Coder Launch Initiative
"""

import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class FirstExperienceResult:
    """Result of the first experience flow."""

    vscode_opened: bool = False
    webhook_verified: bool = False
    first_prompt_suggested: bool = False
    success_shown: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def fully_successful(self) -> bool:
        return self.vscode_opened and self.webhook_verified and not self.errors

    def to_dict(self) -> Dict:
        return {
            "vscode_opened": self.vscode_opened,
            "webhook_verified": self.webhook_verified,
            "first_prompt_suggested": self.first_prompt_suggested,
            "success_shown": self.success_shown,
            "fully_successful": self.fully_successful,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class TutorialStep:
    """A step in the first experience tutorial."""

    step_number: int
    title: str
    instruction: str
    expected_result: str
    keyboard_shortcut: Optional[str] = None
    visual_hint: Optional[str] = None


class FirstExperience:
    """
    Guide users through their first Context DNA experience.

    Handles:
    - Opening VS Code with the project
    - Verifying webhook hooks are active
    - Providing guided first prompts
    - Celebrating successful setup
    """

    FIRST_PROMPTS = [
        {
            "category": "exploration",
            "prompt": "What is this project about?",
            "description": "Get an overview of your new project structure",
        },
        {
            "category": "learning",
            "prompt": "Explain how this main file works",
            "description": "Understand the starter code",
        },
        {
            "category": "creation",
            "prompt": "Create a simple hello world function",
            "description": "Generate your first piece of code",
        },
        {
            "category": "enhancement",
            "prompt": "Add a function to calculate the sum of two numbers",
            "description": "Extend the project with new functionality",
        },
        {
            "category": "testing",
            "prompt": "Write a test for the main function",
            "description": "Create your first test",
        },
    ]

    def __init__(self, project_path: Optional[Path] = None):
        self.project_path = project_path or Path.cwd()
        self.system = platform.system()

    # =========================================================================
    # VS Code Operations
    # =========================================================================

    def open_vscode_with_project(self, project_path: Optional[Path] = None) -> bool:
        """
        Open VS Code with the specified project.

        Returns True if VS Code opened successfully.
        """
        path = project_path or self.project_path

        try:
            # Try to find VS Code command
            vscode_cmd = self._find_vscode_command()

            if not vscode_cmd:
                return False

            # Open VS Code with the project
            subprocess.Popen(
                [vscode_cmd, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            return True

        except Exception:
            return False

    def _find_vscode_command(self) -> Optional[str]:
        """Find the VS Code command for this system."""
        commands_to_try = ["code"]

        if self.system == "Darwin":
            # macOS - try common locations
            app_paths = [
                "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
                str(Path.home() / "Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"),
            ]
            commands_to_try.extend(app_paths)

        elif self.system == "Windows":
            # Windows - try common locations
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            program_files = os.environ.get("PROGRAMFILES", "C:\\Program Files")
            commands_to_try.extend([
                os.path.join(local_app_data, "Programs", "Microsoft VS Code", "bin", "code.cmd"),
                os.path.join(program_files, "Microsoft VS Code", "bin", "code.cmd"),
            ])

        # Try each command
        for cmd in commands_to_try:
            try:
                result = subprocess.run(
                    [cmd, "--version"],
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return cmd
            except Exception:
                continue

        return None

    def is_vscode_installed(self) -> bool:
        """Check if VS Code is installed and accessible."""
        return self._find_vscode_command() is not None

    # =========================================================================
    # Webhook Verification
    # =========================================================================

    def verify_webhook_active(self) -> Dict[str, Any]:
        """
        Verify that Context DNA webhook is properly configured.

        Returns dict with verification status and details.
        """
        result = {
            "verified": False,
            "checks": {},
            "message": "",
        }

        checks = []

        # Check 1: Hook configuration file exists
        hook_file = Path.home() / ".claude" / "settings.local.json"
        hook_file_exists = hook_file.exists()
        checks.append(("hook_file_exists", hook_file_exists))

        # Check 2: Hook file has valid JSON
        hook_valid = False
        hook_command_present = False
        if hook_file_exists:
            try:
                with open(hook_file) as f:
                    config = json.load(f)
                hook_valid = True

                # Check 3: UserPromptSubmit hook is configured
                hooks = config.get("hooks", {})
                submit_hooks = hooks.get("UserPromptSubmit", [])
                if submit_hooks:
                    hook_command_present = True
            except Exception as e:
                print(f"[WARN] Hook config validation failed: {e}")

        checks.append(("hook_syntax_valid", hook_valid))
        checks.append(("hook_command_present", hook_command_present))

        # Check 4: Memory module exists
        memory_dir = Path(__file__).parent
        memory_module_exists = (memory_dir / "auto_context.py").exists() or \
                               (memory_dir / "context.py").exists()
        checks.append(("memory_module_exists", memory_module_exists))

        # Check 5: Scripts directory exists
        scripts_dir = Path(__file__).parent.parent / "scripts"
        auto_memory_script = scripts_dir / "auto-memory-query.sh"
        script_exists = auto_memory_script.exists()
        checks.append(("auto_memory_script_exists", script_exists))

        # Check 6: Script is executable
        script_executable = False
        if script_exists:
            script_executable = os.access(auto_memory_script, os.X_OK)
        checks.append(("script_executable", script_executable))

        # Store all checks
        result["checks"] = {name: passed for name, passed in checks}

        # Determine overall status
        critical_checks = ["hook_file_exists", "hook_command_present"]
        critical_passed = all(result["checks"].get(c, False) for c in critical_checks)

        all_passed = all(passed for _, passed in checks)

        if all_passed:
            result["verified"] = True
            result["message"] = "All webhook checks passed!"
        elif critical_passed:
            result["verified"] = True
            result["message"] = "Webhook configured (some optional checks failed)"
        else:
            result["verified"] = False
            failed = [name for name, passed in checks if not passed]
            result["message"] = f"Webhook verification failed: {', '.join(failed[:3])}"

        return result

    # =========================================================================
    # Tutorial & Guidance
    # =========================================================================

    def get_tutorial_steps(self) -> List[TutorialStep]:
        """Get the guided tutorial steps for first experience."""
        return [
            TutorialStep(
                step_number=1,
                title="Open Claude Code",
                instruction="Open the Claude Code panel in VS Code",
                expected_result="Claude Code chat panel opens on the right",
                keyboard_shortcut="Cmd+Shift+P → 'Claude: Open'",
                visual_hint="Look for the Claude icon in the activity bar",
            ),
            TutorialStep(
                step_number=2,
                title="Send Your First Prompt",
                instruction="Type a simple prompt to test the integration",
                expected_result="Claude responds AND you see Context DNA injection in the prompt",
                keyboard_shortcut=None,
                visual_hint="Look for '🧬 CONTEXT DNA BLUEPRINT' in the injected context",
            ),
            TutorialStep(
                step_number=3,
                title="Verify Context Injection",
                instruction="Check that Context DNA is injecting wisdom into your prompts",
                expected_result="You should see sections like SAFETY, FOUNDATION, WISDOM",
                keyboard_shortcut=None,
                visual_hint="The injection appears BEFORE your prompt text",
            ),
            TutorialStep(
                step_number=4,
                title="Explore Your Project",
                instruction="Ask Claude about your project structure",
                expected_result="Claude explains your starter project with full context",
                keyboard_shortcut=None,
                visual_hint="Try: 'What files are in this project?'",
            ),
        ]

    def get_first_prompts(self, project_type: Optional[str] = None) -> List[Dict]:
        """Get suggested first prompts, optionally filtered by project type."""
        prompts = self.FIRST_PROMPTS.copy()

        # Add project-specific prompts
        if project_type:
            if project_type == "fastapi":
                prompts.insert(0, {
                    "category": "testing",
                    "prompt": "Run the FastAPI server and show me the docs",
                    "description": "Start your API and see auto-generated docs",
                })
            elif project_type == "nextjs":
                prompts.insert(0, {
                    "category": "development",
                    "prompt": "Start the Next.js development server",
                    "description": "Launch your web app locally",
                })
            elif project_type == "ml_python":
                prompts.insert(0, {
                    "category": "exploration",
                    "prompt": "Open the Jupyter notebook and explain what I can do",
                    "description": "Get started with data exploration",
                })
            elif project_type == "expo":
                prompts.insert(0, {
                    "category": "development",
                    "prompt": "Start the Expo development server",
                    "description": "Launch your mobile app",
                })

        return prompts

    def get_guided_first_prompt(self, project_type: Optional[str] = None) -> str:
        """Get the best first prompt suggestion."""
        prompts = self.get_first_prompts(project_type)
        if prompts:
            return prompts[0]["prompt"]
        return "What is this project about?"

    # =========================================================================
    # Success Messages
    # =========================================================================

    def get_success_message(self) -> str:
        """Get the success celebration message."""
        return '''
╔══════════════════════════════════════════════════════════════╗
║  🎉 SETUP COMPLETE! Context DNA is Ready!                    ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Your development environment is now supercharged:           ║
║                                                              ║
║  ✅ VS Code opened with your project                         ║
║  ✅ Context DNA webhooks are active                          ║
║  ✅ Memory system initialized                                ║
║  ✅ Ready for your first AI-assisted coding session!         ║
║                                                              ║
║  ┌─────────────────────────────────────────────────────────┐ ║
║  │ 💡 TRY YOUR FIRST PROMPT:                               │ ║
║  │                                                          │ ║
║  │    "What is this project about?"                        │ ║
║  │                                                          │ ║
║  │ You'll see Context DNA inject wisdom automatically!      │ ║
║  └─────────────────────────────────────────────────────────┘ ║
║                                                              ║
║  📚 Resources:                                               ║
║  • Context DNA docs: context-dna/docs/                       ║
║  • Memory queries: python memory/query.py "topic"            ║
║  • Brain context: python memory/brain.py context "task"      ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
'''

    def get_partial_success_message(self, result: FirstExperienceResult) -> str:
        """Get a message for partial success scenarios."""
        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║  ⚠️ SETUP MOSTLY COMPLETE - Some Issues Detected             ║",
            "╠══════════════════════════════════════════════════════════════╣",
        ]

        if result.vscode_opened:
            lines.append("║  ✅ VS Code opened successfully                                ║")
        else:
            lines.append("║  ❌ VS Code could not be opened automatically                 ║")
            lines.append("║     → Open VS Code manually and open your project folder      ║")

        if result.webhook_verified:
            lines.append("║  ✅ Webhook hooks verified                                     ║")
        else:
            lines.append("║  ❌ Webhook verification had issues                           ║")
            lines.append("║     → Run: python memory/webhook_verifier.py --fix            ║")

        if result.warnings:
            lines.append("║                                                              ║")
            lines.append("║  Warnings:                                                   ║")
            for warning in result.warnings[:3]:
                lines.append(f"║  • {warning[:54]:<54} ║")

        lines.extend([
            "║                                                              ║",
            "║  Despite the issues, you can still start using Claude!      ║",
            "║  Context DNA will do its best to inject helpful context.    ║",
            "╚══════════════════════════════════════════════════════════════╝",
        ])

        return "\n".join(lines)

    # =========================================================================
    # Main Flow
    # =========================================================================

    def run_first_experience(
        self,
        project_path: Optional[Path] = None,
        project_type: Optional[str] = None,
        open_vscode: bool = True,
        show_tutorial: bool = True,
    ) -> FirstExperienceResult:
        """
        Run the complete first experience flow.

        Args:
            project_path: Path to the project to open
            project_type: Type of project (for customized prompts)
            open_vscode: Whether to open VS Code
            show_tutorial: Whether to display tutorial steps

        Returns:
            FirstExperienceResult with status details
        """
        result = FirstExperienceResult()
        path = project_path or self.project_path

        # Step 1: Verify webhook is configured
        webhook_result = self.verify_webhook_active()
        result.webhook_verified = webhook_result["verified"]
        if not webhook_result["verified"]:
            result.warnings.append(webhook_result["message"])

        # Step 2: Open VS Code (if requested)
        if open_vscode:
            if self.is_vscode_installed():
                result.vscode_opened = self.open_vscode_with_project(path)
                if not result.vscode_opened:
                    result.warnings.append("VS Code found but failed to open project")
            else:
                result.warnings.append("VS Code not found in PATH")
                result.vscode_opened = False

        # Step 3: Prepare first prompt
        result.first_prompt_suggested = True

        # Step 4: Show success message
        result.success_shown = True

        return result

    def print_first_experience(
        self,
        result: FirstExperienceResult,
        project_type: Optional[str] = None,
    ):
        """Print the first experience output to console."""
        if result.fully_successful:
            print(self.get_success_message())
        else:
            print(self.get_partial_success_message(result))

        # Show suggested first prompts
        print("\n💡 Suggested First Prompts:")
        print("-" * 40)
        for i, prompt in enumerate(self.get_first_prompts(project_type)[:4], 1):
            print(f"  {i}. \"{prompt['prompt']}\"")
            print(f"     → {prompt['description']}")
        print()


# =============================================================================
# Module-Level Convenience Functions
# =============================================================================

def launch_first_experience(
    project_path: Path,
    project_type: Optional[str] = None,
) -> FirstExperienceResult:
    """Launch the complete first experience flow."""
    fe = FirstExperience(project_path)
    result = fe.run_first_experience(project_type=project_type)
    fe.print_first_experience(result, project_type)
    return result


def verify_webhook() -> Dict[str, Any]:
    """Quick webhook verification."""
    return FirstExperience().verify_webhook_active()


def open_vscode(project_path: Path) -> bool:
    """Open VS Code with project."""
    return FirstExperience().open_vscode_with_project(project_path)


# =============================================================================
# CLI Interface
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="First Experience - Guided onboarding for Context DNA"
    )
    parser.add_argument("--project", type=Path, help="Project path to open")
    parser.add_argument("--project-type", type=str, help="Project type (python_starter, fastapi, etc.)")
    parser.add_argument("--verify", action="store_true", help="Only verify webhook status")
    parser.add_argument("--tutorial", action="store_true", help="Show tutorial steps")
    parser.add_argument("--prompts", action="store_true", help="Show suggested first prompts")
    parser.add_argument("--no-vscode", action="store_true", help="Don't open VS Code")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    fe = FirstExperience(args.project)

    if args.verify:
        result = fe.verify_webhook_active()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Webhook Verification")
            print("=" * 40)
            for check, passed in result["checks"].items():
                icon = "✅" if passed else "❌"
                print(f"  {icon} {check}")
            print()
            icon = "✅" if result["verified"] else "❌"
            print(f"{icon} {result['message']}")

    elif args.tutorial:
        steps = fe.get_tutorial_steps()
        if args.json:
            print(json.dumps([{
                "step": s.step_number,
                "title": s.title,
                "instruction": s.instruction,
                "expected_result": s.expected_result,
                "keyboard_shortcut": s.keyboard_shortcut,
            } for s in steps], indent=2))
        else:
            print("Tutorial Steps")
            print("=" * 50)
            for step in steps:
                print(f"\nStep {step.step_number}: {step.title}")
                print(f"  → {step.instruction}")
                if step.keyboard_shortcut:
                    print(f"  ⌨️ {step.keyboard_shortcut}")
                print(f"  ✓ Expected: {step.expected_result}")

    elif args.prompts:
        prompts = fe.get_first_prompts(args.project_type)
        if args.json:
            print(json.dumps(prompts, indent=2))
        else:
            print("Suggested First Prompts")
            print("=" * 50)
            for i, p in enumerate(prompts, 1):
                print(f"\n{i}. \"{p['prompt']}\"")
                print(f"   [{p['category']}] {p['description']}")

    else:
        # Run full first experience
        result = fe.run_first_experience(
            project_path=args.project,
            project_type=args.project_type,
            open_vscode=not args.no_vscode,
        )

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            fe.print_first_experience(result, args.project_type)
