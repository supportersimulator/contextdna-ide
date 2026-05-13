"""Extras Installer for Context DNA.

Handles installation of optional visualization tools:
- xbar (macOS menu bar)
- VS Code extension
- Web dashboard
- Raycast extension
"""

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any


class ExtrasInstaller:
    """Installs Context DNA visualization extras."""

    EXTRAS = {
        "xbar": {
            "name": "xbar Menu Bar",
            "description": "macOS menu bar dashboard showing Context DNA stats",
            "platform": "macOS only",
            "platforms": ["Darwin"],
            "installer": "_install_xbar",
        },
        "vscode": {
            "name": "VS Code Extension",
            "description": "VS Code sidebar with Context DNA integration",
            "platform": "All platforms",
            "platforms": ["Darwin", "Linux", "Windows"],
            "installer": "_install_vscode",
        },
        "dashboard": {
            "name": "Web Dashboard",
            "description": "Next.js web dashboard for Context DNA",
            "platform": "All platforms",
            "platforms": ["Darwin", "Linux", "Windows"],
            "installer": "_install_dashboard",
        },
        "raycast": {
            "name": "Raycast Extension",
            "description": "Raycast commands for quick Context DNA access",
            "platform": "macOS only",
            "platforms": ["Darwin"],
            "installer": "_install_raycast",
        },
        "electron": {
            "name": "Electron Desktop App",
            "description": "Standalone desktop app wrapping the dashboard",
            "platform": "All platforms",
            "platforms": ["Darwin", "Linux", "Windows"],
            "installer": "_install_electron",
        },
    }

    def __init__(self):
        """Initialize installer."""
        self._templates_dir = Path(__file__).parent / "templates"

    def list_extras(self) -> Dict[str, Dict[str, Any]]:
        """List all available extras with their status."""
        system = platform.system()
        extras = {}

        for name, info in self.EXTRAS.items():
            available = system in info["platforms"]
            installed = self._check_installed(name)

            extras[name] = {
                **info,
                "available": available,
                "installed": installed,
                "reason": None if available else f"Only available on {', '.join(info['platforms'])}",
            }

        return extras

    def install(self, extra_name: str, force: bool = False) -> bool:
        """Install an extra.

        Args:
            extra_name: Name of extra to install
            force: Reinstall even if already installed

        Returns:
            True if installation successful
        """
        if extra_name not in self.EXTRAS:
            print(f"Unknown extra: {extra_name}")
            print(f"Available: {', '.join(self.EXTRAS.keys())}")
            return False

        info = self.EXTRAS[extra_name]
        system = platform.system()

        # Check platform
        if system not in info["platforms"]:
            print(f"'{extra_name}' is not available on {system}")
            print(f"Available on: {', '.join(info['platforms'])}")
            return False

        # Check if already installed
        if self._check_installed(extra_name) and not force:
            print(f"'{extra_name}' is already installed")
            print("Use --force to reinstall")
            return True

        # Run installer
        installer_method = getattr(self, info["installer"])
        return installer_method()

    def uninstall(self, extra_name: str) -> bool:
        """Uninstall an extra."""
        if extra_name == "xbar":
            return self._uninstall_xbar()
        elif extra_name == "vscode":
            return self._uninstall_vscode()
        elif extra_name == "dashboard":
            return self._uninstall_dashboard()
        elif extra_name == "raycast":
            return self._uninstall_raycast()
        elif extra_name == "electron":
            return self._uninstall_electron()
        return False

    def is_installed(self, extra_name: str) -> bool:
        """Check if an extra is installed."""
        return self._check_installed(extra_name)

    def _check_installed(self, extra_name: str) -> bool:
        """Check if an extra is installed."""
        if extra_name == "xbar":
            xbar_dir = Path.home() / "Library/Application Support/xbar/plugins"
            return (xbar_dir / "context-dna.5m.sh").exists()
        elif extra_name == "vscode":
            vscode_dir = Path.home() / ".vscode/extensions"
            return any(p.name.startswith("context-dna") for p in vscode_dir.glob("*")) if vscode_dir.exists() else False
        elif extra_name == "dashboard":
            return (Path.home() / ".context-dna/dashboard").exists()
        elif extra_name == "raycast":
            raycast_dir = Path.home() / ".config/raycast/extensions"
            return (raycast_dir / "context-dna").exists() if raycast_dir.exists() else False
        elif extra_name == "electron":
            return (Path.home() / ".context-dna/electron-app").exists()
        return False

    def _install_xbar(self) -> bool:
        """Install xbar plugin."""
        print("Installing xbar plugin...")

        # Check if xbar is installed
        xbar_plugins = Path.home() / "Library/Application Support/xbar/plugins"
        if not xbar_plugins.exists():
            print("xbar not found. Install from: https://xbarapp.com")
            print("Then run: context-dna extras install xbar")
            return False

        # Write plugin
        plugin_path = xbar_plugins / "context-dna.5m.sh"
        plugin_content = self._get_xbar_plugin()
        plugin_path.write_text(plugin_content)
        plugin_path.chmod(0o755)

        print(f"✓ Installed: {plugin_path}")
        print("  Refresh xbar to see Context DNA in your menu bar")
        return True

    def _get_xbar_plugin(self) -> str:
        """Generate xbar plugin script."""
        return '''#!/bin/bash
# <xbar.title>Context DNA</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.author>Context DNA</xbar.author>
# <xbar.author.github>context-dna</xbar.author.github>
# <xbar.desc>Context DNA status and quick actions</xbar.desc>
# <xbar.dependencies>python3,context-dna</xbar.dependencies>

# Check if context-dna is installed
if ! command -v context-dna &> /dev/null; then
    echo "🧬 Not Installed"
    echo "---"
    echo "Install Context DNA | bash='pip3 install context-dna' terminal=true"
    exit 0
fi

# Get status
STATUS_OUTPUT=$(context-dna status 2>/dev/null)

if [ $? -ne 0 ]; then
    echo "🧬 Not Initialized"
    echo "---"
    echo "Initialize | bash='context-dna init' terminal=true"
    exit 0
fi

# Parse status
TOTAL=$(echo "$STATUS_OUTPUT" | grep "Total:" | awk '{print $2}')
TODAY=$(echo "$STATUS_OUTPUT" | grep "Today:" | awk '{print $2}')
HEALTHY=$(echo "$STATUS_OUTPUT" | grep "Healthy:" | awk '{print $2}')

# Menu bar display
if [ "$HEALTHY" = "Yes" ]; then
    echo "🧬 $TOTAL"
else
    echo "🧬 ⚠️"
fi

echo "---"

# Stats section
echo "📊 Statistics | size=14"
echo "--Total Learnings $TOTAL | size=12"
echo "--Today $TODAY | size=12"
echo "--Status $HEALTHY | size=12"
echo "---"

# Quick actions
echo "⚡ Quick Actions | size=14"
echo "--Record Win | bash='context-dna win \"Quick win\" \"Details\"' terminal=true"
echo "--Record Fix | bash='context-dna fix \"Problem\" \"Solution\"' terminal=true"
echo "--Search | bash='context-dna query' terminal=true"
echo "--Consult | bash='context-dna consult' terminal=true"
echo "---"

# Recent learnings
echo "📝 Recent | size=14"
RECENT=$(context-dna recent --limit 5 2>/dev/null | grep "\\[" | head -5)
if [ -n "$RECENT" ]; then
    while IFS= read -r line; do
        echo "--$line | size=11"
    done <<< "$RECENT"
else
    echo "--No recent learnings | size=11"
fi
echo "---"

# System
echo "⚙️ System | size=14"
echo "--Open Dashboard | bash='context-dna dashboard' terminal=false"
echo "--View Providers | bash='context-dna providers' terminal=true"
echo "--Upgrade to Pro | bash='context-dna upgrade' terminal=true"
echo "---"
echo "Context DNA v1.0 | size=10 color=gray"
'''

    def _uninstall_xbar(self) -> bool:
        """Uninstall xbar plugin."""
        plugin_path = Path.home() / "Library/Application Support/xbar/plugins/context-dna.5m.sh"
        if plugin_path.exists():
            plugin_path.unlink()
            print("✓ Uninstalled xbar plugin")
            return True
        return False

    def _install_vscode(self) -> bool:
        """Install VS Code extension."""
        print("Installing VS Code extension...")

        # Create extension directory
        ext_dir = Path.home() / ".context-dna/vscode-extension"
        ext_dir.mkdir(parents=True, exist_ok=True)

        # Write package.json
        package_json = {
            "name": "context-dna",
            "displayName": "Context DNA",
            "description": "Autonomous learning for developers",
            "version": "1.0.0",
            "publisher": "context-dna",
            "engines": {"vscode": "^1.74.0"},
            "categories": ["Other"],
            "activationEvents": ["onStartupFinished"],
            "main": "./extension.js",
            "contributes": {
                "viewsContainers": {
                    "activitybar": [{
                        "id": "context-dna",
                        "title": "Context DNA",
                        "icon": "$(database)"
                    }]
                },
                "views": {
                    "context-dna": [{
                        "id": "contextDnaStats",
                        "name": "Statistics"
                    }, {
                        "id": "contextDnaRecent",
                        "name": "Recent Learnings"
                    }]
                },
                "commands": [{
                    "command": "context-dna.recordWin",
                    "title": "Context DNA: Record Win"
                }, {
                    "command": "context-dna.recordFix",
                    "title": "Context DNA: Record Fix"
                }, {
                    "command": "context-dna.consult",
                    "title": "Context DNA: Consult"
                }, {
                    "command": "context-dna.search",
                    "title": "Context DNA: Search"
                }]
            }
        }

        import json
        (ext_dir / "package.json").write_text(json.dumps(package_json, indent=2))

        # Write extension.js
        extension_js = '''
const vscode = require('vscode');
const { exec } = require('child_process');

function activate(context) {
    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('context-dna.recordWin', async () => {
            const title = await vscode.window.showInputBox({ prompt: 'What worked?' });
            if (!title) return;
            const details = await vscode.window.showInputBox({ prompt: 'How did it work?' });
            exec(`context-dna win "${title}" "${details || ''}"`, (err, stdout) => {
                if (err) vscode.window.showErrorMessage('Failed to record win');
                else vscode.window.showInformationMessage('Win recorded!');
            });
        }),

        vscode.commands.registerCommand('context-dna.recordFix', async () => {
            const problem = await vscode.window.showInputBox({ prompt: 'What was the problem?' });
            if (!problem) return;
            const solution = await vscode.window.showInputBox({ prompt: 'How did you fix it?' });
            if (!solution) return;
            exec(`context-dna fix "${problem}" "${solution}"`, (err, stdout) => {
                if (err) vscode.window.showErrorMessage('Failed to record fix');
                else vscode.window.showInformationMessage('Fix recorded!');
            });
        }),

        vscode.commands.registerCommand('context-dna.consult', async () => {
            const task = await vscode.window.showInputBox({ prompt: 'What are you about to do?' });
            if (!task) return;
            exec(`context-dna consult "${task}"`, (err, stdout) => {
                if (err) {
                    vscode.window.showErrorMessage('Failed to consult');
                } else {
                    const panel = vscode.window.createWebviewPanel(
                        'contextDnaConsult', 'Context DNA Context', vscode.ViewColumn.Two,
                        {}
                    );
                    panel.webview.html = `<pre style="padding: 20px; font-family: monospace;">${stdout}</pre>`;
                }
            });
        }),

        vscode.commands.registerCommand('context-dna.search', async () => {
            const query = await vscode.window.showInputBox({ prompt: 'Search learnings...' });
            if (!query) return;
            exec(`context-dna query "${query}"`, (err, stdout) => {
                if (err) {
                    vscode.window.showErrorMessage('Search failed');
                } else {
                    const panel = vscode.window.createWebviewPanel(
                        'contextDnaSearch', 'Context DNA Search', vscode.ViewColumn.Two,
                        {}
                    );
                    panel.webview.html = `<pre style="padding: 20px; font-family: monospace;">${stdout}</pre>`;
                }
            });
        })
    );

    vscode.window.showInformationMessage('Context DNA activated!');
}

function deactivate() {}

module.exports = { activate, deactivate };
'''
        (ext_dir / "extension.js").write_text(extension_js)

        print(f"✓ Extension created at: {ext_dir}")
        print()
        print("To install in VS Code:")
        print(f"  1. cd {ext_dir}")
        print("  2. npm install")
        print("  3. code --install-extension .")
        print()
        print("Or install from VS Code Marketplace (coming soon)")
        return True

    def _uninstall_vscode(self) -> bool:
        """Uninstall VS Code extension."""
        ext_dir = Path.home() / ".context-dna/vscode-extension"
        if ext_dir.exists():
            shutil.rmtree(ext_dir)
            print("✓ Removed VS Code extension source")
        # Also try to uninstall from VS Code
        subprocess.run(["code", "--uninstall-extension", "context-dna.context-dna"],
                      capture_output=True)
        return True

    def _install_dashboard(self) -> bool:
        """Install web dashboard."""
        print("Installing web dashboard...")

        dashboard_dir = Path.home() / ".context-dna/dashboard"
        dashboard_dir.mkdir(parents=True, exist_ok=True)

        # Write package.json
        package_json = {
            "name": "context-dna-dashboard",
            "version": "1.0.0",
            "private": True,
            "scripts": {
                "dev": "next dev -p 3456",
                "build": "next build",
                "start": "next start -p 3456"
            },
            "dependencies": {
                "next": "^14.0.0",
                "react": "^18.2.0",
                "react-dom": "^18.2.0"
            }
        }

        import json
        (dashboard_dir / "package.json").write_text(json.dumps(package_json, indent=2))

        # Create pages directory
        pages_dir = dashboard_dir / "pages"
        pages_dir.mkdir(exist_ok=True)

        # Write index page
        index_page = '''
import { useState, useEffect } from 'react';

export default function Home() {
  const [stats, setStats] = useState(null);
  const [recent, setRecent] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Fetch stats from API
    fetch('/api/stats')
      .then(res => res.json())
      .then(data => {
        setStats(data.stats);
        setRecent(data.recent);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading) return <div className="loading">Loading...</div>;

  return (
    <div className="dashboard">
      <header>
        <h1>🧬 Context DNA</h1>
        <p>Autonomous Learning Dashboard</p>
      </header>

      <div className="stats-grid">
        <div className="stat-card">
          <h3>Total Learnings</h3>
          <div className="stat-value">{stats?.total || 0}</div>
        </div>
        <div className="stat-card">
          <h3>Today</h3>
          <div className="stat-value">{stats?.today || 0}</div>
        </div>
        <div className="stat-card">
          <h3>Wins</h3>
          <div className="stat-value">{stats?.by_type?.win || 0}</div>
        </div>
        <div className="stat-card">
          <h3>Fixes</h3>
          <div className="stat-value">{stats?.by_type?.fix || 0}</div>
        </div>
      </div>

      <section className="recent">
        <h2>Recent Learnings</h2>
        <ul>
          {recent.map((item, i) => (
            <li key={i} className={`learning-${item.type}`}>
              <span className="type">[{item.type}]</span>
              <span className="title">{item.title}</span>
              <span className="date">{new Date(item.created_at).toLocaleDateString()}</span>
            </li>
          ))}
        </ul>
      </section>

      <style jsx>{`
        .dashboard { max-width: 1200px; margin: 0 auto; padding: 20px; font-family: system-ui; }
        header { text-align: center; margin-bottom: 40px; }
        header h1 { font-size: 2.5em; margin-bottom: 10px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 40px; }
        .stat-card { background: #f5f5f5; border-radius: 12px; padding: 20px; text-align: center; }
        .stat-card h3 { margin: 0 0 10px; color: #666; font-size: 0.9em; text-transform: uppercase; }
        .stat-value { font-size: 2.5em; font-weight: bold; color: #333; }
        .recent h2 { margin-bottom: 20px; }
        .recent ul { list-style: none; padding: 0; }
        .recent li { padding: 15px; border-bottom: 1px solid #eee; display: flex; gap: 15px; align-items: center; }
        .type { background: #e0e0e0; padding: 4px 8px; border-radius: 4px; font-size: 0.8em; }
        .learning-win .type { background: #c8e6c9; color: #2e7d32; }
        .learning-fix .type { background: #ffecb3; color: #f57c00; }
        .title { flex: 1; }
        .date { color: #999; font-size: 0.9em; }
        .loading { text-align: center; padding: 100px; font-size: 1.5em; color: #666; }
      `}</style>
    </div>
  );
}
'''
        (pages_dir / "index.js").write_text(index_page)

        # Create API directory
        api_dir = pages_dir / "api"
        api_dir.mkdir(exist_ok=True)

        # Write stats API
        stats_api = '''
import { exec } from 'child_process';
import { promisify } from 'util';

const execAsync = promisify(exec);

export default async function handler(req, res) {
  try {
    // Get stats
    const { stdout: statsOutput } = await execAsync('context-dna status --json 2>/dev/null || context-dna status');

    // Get recent
    const { stdout: recentOutput } = await execAsync('context-dna export 2>/dev/null');

    let stats = { total: 0, today: 0, by_type: {} };
    let recent = [];

    // Parse stats
    try {
      if (statsOutput.includes('{')) {
        stats = JSON.parse(statsOutput);
      } else {
        // Parse text output
        const lines = statsOutput.split('\\n');
        for (const line of lines) {
          if (line.includes('Total:')) stats.total = parseInt(line.split(':')[1]) || 0;
          if (line.includes('Today:')) stats.today = parseInt(line.split(':')[1]) || 0;
        }
      }
    } catch (e) {}

    // Parse recent
    try {
      const allLearnings = JSON.parse(recentOutput);
      recent = allLearnings.slice(-10).reverse();
    } catch (e) {}

    res.status(200).json({ stats, recent });
  } catch (error) {
    res.status(500).json({ error: error.message, stats: { total: 0, today: 0 }, recent: [] });
  }
}
'''
        (api_dir / "stats.js").write_text(stats_api)

        print(f"✓ Dashboard created at: {dashboard_dir}")
        print()
        print("To start the dashboard:")
        print(f"  cd {dashboard_dir}")
        print("  npm install")
        print("  npm run dev")
        print()
        print("Then open: http://localhost:3456")
        return True

    def _uninstall_dashboard(self) -> bool:
        """Uninstall dashboard."""
        dashboard_dir = Path.home() / ".context-dna/dashboard"
        if dashboard_dir.exists():
            shutil.rmtree(dashboard_dir)
            print("✓ Removed web dashboard")
            return True
        return False

    def _install_raycast(self) -> bool:
        """Install Raycast extension."""
        print("Installing Raycast extension...")

        raycast_dir = Path.home() / ".context-dna/raycast-extension"
        raycast_dir.mkdir(parents=True, exist_ok=True)

        # Write package.json
        package_json = {
            "name": "context-dna",
            "title": "Context DNA",
            "description": "Autonomous learning for developers",
            "icon": "dna-icon.png",
            "author": "context-dna",
            "license": "MIT",
            "commands": [
                {
                    "name": "search",
                    "title": "Search Learnings",
                    "description": "Search your Context DNA",
                    "mode": "view"
                },
                {
                    "name": "record-win",
                    "title": "Record Win",
                    "description": "Record something that worked",
                    "mode": "no-view"
                },
                {
                    "name": "record-fix",
                    "title": "Record Fix",
                    "description": "Record a problem and solution",
                    "mode": "no-view"
                },
                {
                    "name": "consult",
                    "title": "Consult Memory",
                    "description": "Get context before starting a task",
                    "mode": "view"
                },
                {
                    "name": "status",
                    "title": "Memory Status",
                    "description": "View Context DNA statistics",
                    "mode": "view"
                }
            ],
            "dependencies": {
                "@raycast/api": "^1.50.0"
            },
            "devDependencies": {
                "@raycast/eslint-config": "1.0.5",
                "@types/node": "18.8.3",
                "@types/react": "18.0.9",
                "eslint": "^7.32.0",
                "typescript": "^4.4.3"
            },
            "scripts": {
                "build": "ray build -e dist",
                "dev": "ray develop",
                "lint": "ray lint"
            }
        }

        import json
        (raycast_dir / "package.json").write_text(json.dumps(package_json, indent=2))

        # Create src directory
        src_dir = raycast_dir / "src"
        src_dir.mkdir(exist_ok=True)

        # Write search command
        search_tsx = '''
import { ActionPanel, Action, List, showToast, Toast } from "@raycast/api";
import { useState, useEffect } from "react";
import { exec } from "child_process";
import { promisify } from "util";

const execAsync = promisify(exec);

interface Learning {
  type: string;
  title: string;
  content: string;
  created_at: string;
}

export default function SearchLearnings() {
  const [searchText, setSearchText] = useState("");
  const [learnings, setLearnings] = useState<Learning[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (searchText.length < 2) {
      setLearnings([]);
      return;
    }

    setIsLoading(true);
    execAsync(`context-dna query "${searchText}" --limit 10`)
      .then(({ stdout }) => {
        // Parse output
        const results: Learning[] = [];
        const lines = stdout.split("\\n");
        let current: Partial<Learning> = {};

        for (const line of lines) {
          if (line.startsWith("[")) {
            if (current.title) results.push(current as Learning);
            const match = line.match(/\\[(.+?)\\] (.+)/);
            if (match) {
              current = { type: match[1], title: match[2] };
            }
          } else if (line.includes("ID:")) {
            const dateMatch = line.match(/\\| (.+)/);
            if (dateMatch) current.created_at = dateMatch[1];
          } else if (line.trim() && !line.startsWith("Found") && !line.startsWith("Tags:")) {
            current.content = line.trim();
          }
        }
        if (current.title) results.push(current as Learning);

        setLearnings(results);
        setIsLoading(false);
      })
      .catch(() => {
        setIsLoading(false);
        showToast({ style: Toast.Style.Failure, title: "Search failed" });
      });
  }, [searchText]);

  return (
    <List
      isLoading={isLoading}
      onSearchTextChange={setSearchText}
      searchBarPlaceholder="Search learnings..."
      throttle
    >
      {learnings.map((learning, index) => (
        <List.Item
          key={index}
          title={learning.title}
          subtitle={learning.content}
          accessories={[{ text: learning.type }]}
          actions={
            <ActionPanel>
              <Action.CopyToClipboard title="Copy" content={`${learning.title}\\n${learning.content}`} />
            </ActionPanel>
          }
        />
      ))}
    </List>
  );
}
'''
        (src_dir / "search.tsx").write_text(search_tsx)

        # Write record-win command
        record_win_tsx = '''
import { showToast, Toast, closeMainWindow } from "@raycast/api";
import { exec } from "child_process";
import { promisify } from "util";

const execAsync = promisify(exec);

export default async function RecordWin(props: { arguments: { title: string; details: string } }) {
  const { title, details } = props.arguments;

  try {
    await execAsync(`context-dna win "${title}" "${details || ""}"`);
    await showToast({ style: Toast.Style.Success, title: "Win recorded!" });
    await closeMainWindow();
  } catch (error) {
    await showToast({ style: Toast.Style.Failure, title: "Failed to record win" });
  }
}
'''
        (src_dir / "record-win.tsx").write_text(record_win_tsx)

        print(f"✓ Raycast extension created at: {raycast_dir}")
        print()
        print("To install in Raycast:")
        print(f"  1. cd {raycast_dir}")
        print("  2. npm install")
        print("  3. npm run dev")
        print()
        print("Or submit to Raycast Store for public distribution")
        return True

    def _uninstall_raycast(self) -> bool:
        """Uninstall Raycast extension."""
        raycast_dir = Path.home() / ".context-dna/raycast-extension"
        if raycast_dir.exists():
            shutil.rmtree(raycast_dir)
            print("✓ Removed Raycast extension source")
            return True
        return False

    def _install_electron(self) -> bool:
        """Install Electron desktop app."""
        print("Installing Electron desktop app...")

        app_dir = Path.home() / ".context-dna/electron-app"
        app_dir.mkdir(parents=True, exist_ok=True)

        import json

        # Write package.json
        package_json = {
            "name": "context-dna-app",
            "version": "0.1.0",
            "description": "Context DNA - Autonomous Learning Desktop App",
            "main": "main.js",
            "scripts": {
                "start": "electron .",
                "build": "electron-builder",
                "build:mac": "electron-builder --mac",
                "build:win": "electron-builder --win",
                "build:linux": "electron-builder --linux"
            },
            "dependencies": {
                "electron-store": "^8.1.0"
            },
            "devDependencies": {
                "electron": "^28.0.0",
                "electron-builder": "^24.0.0"
            },
            "build": {
                "appId": "dev.context-dna.app",
                "productName": "Context DNA",
                "mac": {
                    "category": "public.app-category.developer-tools"
                }
            }
        }
        (app_dir / "package.json").write_text(json.dumps(package_json, indent=2))

        # Write main.js
        main_js = '''const { app, BrowserWindow, Tray, Menu, shell } = require('electron');
const path = require('path');
const Store = require('electron-store');

const store = new Store({
    defaults: {
        apiUrl: 'http://127.0.0.1:3456',
        dashboardUrl: 'http://localhost:3457'
    }
});

let mainWindow = null;
let tray = null;

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        title: 'Context DNA',
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true
        }
    });

    mainWindow.loadURL(store.get('dashboardUrl')).catch(() => {
        mainWindow.loadURL(`data:text/html,
            <html>
                <body style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:system-ui;background:#1a1a2e;color:white;">
                    <div style="text-align:center;">
                        <h1>🧬 Dashboard Not Running</h1>
                        <p>Start with: context-dna serve</p>
                        <button onclick="location.reload()" style="padding:10px 20px;cursor:pointer;">Retry</button>
                    </div>
                </body>
            </html>
        `);
    });

    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        shell.openExternal(url);
        return { action: 'deny' };
    });
}

function createTray() {
    tray = new Tray(path.join(__dirname, 'icon.png'));
    const contextMenu = Menu.buildFromTemplate([
        { label: 'Open Dashboard', click: () => mainWindow?.show() },
        { type: 'separator' },
        { label: 'Quit', click: () => { app.isQuitting = true; app.quit(); } }
    ]);
    tray.setToolTip('Context DNA');
    tray.setContextMenu(contextMenu);
    tray.on('click', () => mainWindow?.show());
}

app.whenReady().then(() => {
    createWindow();
    createTray();
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
'''
        (app_dir / "main.js").write_text(main_js)

        # Create a simple icon placeholder
        # In production, this would be a real icon
        icon_svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <circle cx="16" cy="16" r="14" fill="#6366f1"/>
  <text x="16" y="22" text-anchor="middle" font-size="16" fill="white">🧬</text>
</svg>'''
        (app_dir / "icon.svg").write_text(icon_svg)

        print(f"✓ Electron app created at: {app_dir}")
        print()
        print("To build and run:")
        print(f"  cd {app_dir}")
        print("  npm install")
        print("  npm start")
        print()
        print("To build distributable:")
        print("  npm run build:mac  (or :win or :linux)")
        return True

    def _uninstall_electron(self) -> bool:
        """Uninstall Electron app."""
        app_dir = Path.home() / ".context-dna/electron-app"
        if app_dir.exists():
            shutil.rmtree(app_dir)
            print("✓ Removed Electron app")
            return True
        return False
