"""Context DNA Extras - Optional Visualization Tools.

Available extras:
- xbar: macOS menu bar dashboard
- vscode: VS Code extension
- dashboard: Web dashboard (Next.js)
- raycast: Raycast extension

Install with:
    context-dna extras install xbar
    context-dna extras install vscode
    context-dna extras install dashboard
    context-dna extras install raycast
"""

from context_dna.extras.installer import ExtrasInstaller

__all__ = ["ExtrasInstaller"]
