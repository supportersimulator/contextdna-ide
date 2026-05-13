/**
 * @deprecated — This variant (core/clients/vscode) has been merged into
 * the canonical extension at context-dna/clients/vscode/. That version
 * now includes surgeonTools, fleet status bar, WebSocket EventBus,
 * FleetDashboardPanel, and all features from both variants.
 *
 * DO NOT develop here. Use context-dna/clients/vscode/ instead.
 *
 * Context DNA VS Code Extension (LEGACY — kept for reference only)
 *
 * THIN CLIENT that consumes the Context DNA API.
 * All logic lives in the server - this just provides VS Code UI.
 */

import * as vscode from 'vscode';
import { registerSurgeonTools } from './surgeonTools';

const API_URL = () =>
  vscode.workspace.getConfiguration('context-dna').get('apiUrl', 'http://127.0.0.1:3456');

const DASHBOARD_URL = () =>
  vscode.workspace.getConfiguration('context-dna').get('dashboardUrl', 'http://localhost:3457');

const FLEET_URL = () =>
  vscode.workspace.getConfiguration('context-dna').get('fleetUrl', 'http://127.0.0.1:8855');

/**
 * API request helper
 */
async function apiRequest<T>(
  endpoint: string,
  method: 'GET' | 'POST' = 'GET',
  body?: object
): Promise<T | null> {
  try {
    const response = await fetch(`${API_URL()}${endpoint}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return (await response.json()) as T;
  } catch (error) {
    console.error('Context DNA API error:', error);
    return null;
  }
}

/**
 * Stats interface
 */
interface Stats {
  total: number;
  wins: number;
  fixes: number;
  patterns: number;
  today: number;
  streak: number;
}

/**
 * Learning interface
 */
interface Learning {
  id: string;
  type: string;
  title: string;
  content: string;
  tags: string[];
  created_at: string | null;
}

/**
 * Stats provider for sidebar
 */
class StatsProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private _onDidChangeTreeData = new vscode.EventEmitter<vscode.TreeItem | undefined>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  refresh(): void {
    this._onDidChangeTreeData.fire(undefined);
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  async getChildren(): Promise<vscode.TreeItem[]> {
    const stats = await apiRequest<Stats>('/api/stats');

    if (!stats) {
      const item = new vscode.TreeItem('Server not running');
      item.description = 'Run: context-dna serve';
      item.iconPath = new vscode.ThemeIcon('warning');
      return [item];
    }

    return [
      this.createStatItem('Total', stats.total, 'book'),
      this.createStatItem('Wins', stats.wins, 'star'),
      this.createStatItem('Fixes', stats.fixes, 'wrench'),
      this.createStatItem('Today', stats.today, 'calendar'),
      this.createStatItem('Streak', stats.streak, 'flame'),
    ];
  }

  private createStatItem(label: string, value: number, icon: string): vscode.TreeItem {
    const item = new vscode.TreeItem(`${label}: ${value}`);
    item.iconPath = new vscode.ThemeIcon(icon);
    return item;
  }
}

/**
 * Recent learnings provider for sidebar
 */
class RecentProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private _onDidChangeTreeData = new vscode.EventEmitter<vscode.TreeItem | undefined>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  refresh(): void {
    this._onDidChangeTreeData.fire(undefined);
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  async getChildren(): Promise<vscode.TreeItem[]> {
    const response = await apiRequest<{ recent: Learning[] }>('/api/recent?limit=10');

    if (!response || !response.recent) {
      const item = new vscode.TreeItem('No learnings yet');
      item.iconPath = new vscode.ThemeIcon('info');
      return [item];
    }

    return response.recent.map((learning) => {
      const emoji = { win: '🏆', fix: '🔧', pattern: '🔄' }[learning.type] || '📝';
      const item = new vscode.TreeItem(`${emoji} ${learning.title}`);
      item.description = learning.content?.slice(0, 50) || '';
      item.tooltip = new vscode.MarkdownString(
        `**${learning.title}**\n\n${learning.content || ''}\n\n*${learning.created_at || ''}*`
      );
      return item;
    });
  }
}

/**
 * Extension activation
 */
export function activate(context: vscode.ExtensionContext) {
  console.log('Context DNA extension activated');

  // Register 3-Surgeon Language Model Tools (Layer 3 adapter)
  registerSurgeonTools(context);

  // Create providers
  const statsProvider = new StatsProvider();
  const recentProvider = new RecentProvider();

  // Register tree views
  vscode.window.registerTreeDataProvider('context-dna.stats', statsProvider);
  vscode.window.registerTreeDataProvider('context-dna.recent', recentProvider);

  // Register commands
  context.subscriptions.push(
    // Record Win
    vscode.commands.registerCommand('context-dna.recordWin', async () => {
      const title = await vscode.window.showInputBox({
        prompt: 'What win do you want to record?',
        placeHolder: 'e.g., Fixed async bug in API handler',
      });

      if (!title) return;

      const content = await vscode.window.showInputBox({
        prompt: 'How did you do it? (optional)',
        placeHolder: 'e.g., Used asyncio.to_thread() wrapper',
      });

      const result = await apiRequest<{ success: boolean }>('/api/win', 'POST', {
        title,
        content: content || '',
      });

      if (result?.success) {
        vscode.window.showInformationMessage(`🏆 Win recorded: ${title}`);
        statsProvider.refresh();
        recentProvider.refresh();
      } else {
        vscode.window.showErrorMessage('Failed to record win. Is the server running?');
      }
    }),

    // Record Fix
    vscode.commands.registerCommand('context-dna.recordFix', async () => {
      const title = await vscode.window.showInputBox({
        prompt: 'What problem did you fix?',
        placeHolder: 'e.g., Docker container not starting',
      });

      if (!title) return;

      const content = await vscode.window.showInputBox({
        prompt: 'What was the solution? (optional)',
        placeHolder: 'e.g., HOME=/root was missing in Dockerfile',
      });

      const result = await apiRequest<{ success: boolean }>('/api/fix', 'POST', {
        title,
        content: content || '',
      });

      if (result?.success) {
        vscode.window.showInformationMessage(`🔧 Fix recorded: ${title}`);
        statsProvider.refresh();
        recentProvider.refresh();
      } else {
        vscode.window.showErrorMessage('Failed to record fix. Is the server running?');
      }
    }),

    // Search
    vscode.commands.registerCommand('context-dna.search', async () => {
      const query = await vscode.window.showInputBox({
        prompt: 'Search learnings',
        placeHolder: 'e.g., async python',
      });

      if (!query) return;

      const result = await apiRequest<{ results: Learning[] }>('/api/query', 'POST', {
        query,
        limit: 10,
      });

      if (!result || result.results.length === 0) {
        vscode.window.showInformationMessage(`No results found for "${query}"`);
        return;
      }

      // Show quick pick with results
      const items = result.results.map((learning) => ({
        label: `${
          { win: '🏆', fix: '🔧', pattern: '🔄' }[learning.type] || '📝'
        } ${learning.title}`,
        description: learning.content?.slice(0, 50) || '',
        detail: learning.tags?.join(', ') || '',
        learning,
      }));

      const selected = await vscode.window.showQuickPick(items, {
        placeHolder: `${result.results.length} results for "${query}"`,
      });

      if (selected) {
        // Show full learning in new document
        const doc = await vscode.workspace.openTextDocument({
          content: `# ${selected.learning.title}\n\nType: ${selected.learning.type}\nTags: ${
            selected.learning.tags?.join(', ') || 'none'
          }\n\n${selected.learning.content || ''}`,
          language: 'markdown',
        });
        vscode.window.showTextDocument(doc);
      }
    }),

    // Consult
    vscode.commands.registerCommand('context-dna.consult', async () => {
      const task = await vscode.window.showInputBox({
        prompt: 'What task are you about to work on?',
        placeHolder: 'e.g., implement user authentication',
      });

      if (!task) return;

      const result = await apiRequest<{ context: string }>('/api/consult', 'POST', { task });

      if (result?.context) {
        // Show context in new document
        const doc = await vscode.workspace.openTextDocument({
          content: `# Context for: ${task}\n\n${result.context}`,
          language: 'markdown',
        });
        vscode.window.showTextDocument(doc);
      } else {
        vscode.window.showInformationMessage('No relevant context found for this task.');
      }
    }),

    // Open Dashboard
    vscode.commands.registerCommand('context-dna.openDashboard', () => {
      vscode.env.openExternal(vscode.Uri.parse(DASHBOARD_URL()));
    }),

    // Show Stats
    vscode.commands.registerCommand('context-dna.showStats', async () => {
      const stats = await apiRequest<Stats>('/api/stats');

      if (stats) {
        vscode.window.showInformationMessage(
          `🧬 Context DNA: ${stats.total} learnings | 🏆 ${stats.wins} wins | 🔧 ${stats.fixes} fixes | 🔥 ${stats.streak} day streak`
        );
      } else {
        vscode.window.showErrorMessage('Could not fetch stats. Is the server running?');
      }
    })
  );

  // Auto-refresh every 30 seconds
  const refreshInterval = setInterval(() => {
    statsProvider.refresh();
    recentProvider.refresh();
  }, 30000);

  context.subscriptions.push({ dispose: () => clearInterval(refreshInterval) });

  // Status bar item
  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.command = 'context-dna.showStats';
  statusBar.text = '$(book) Context DNA';
  statusBar.tooltip = 'Click to show Context DNA stats';
  statusBar.show();
  context.subscriptions.push(statusBar);

  // Update status bar with stats
  async function updateStatusBar() {
    const stats = await apiRequest<Stats>('/api/stats');
    if (stats) {
      statusBar.text = `$(book) ${stats.total} | 🔥${stats.streak}`;
      statusBar.tooltip = `Context DNA: ${stats.total} learnings, ${stats.streak} day streak`;
    } else {
      statusBar.text = '$(book) Context DNA ⚠️';
      statusBar.tooltip = 'Context DNA server not running';
    }
  }

  updateStatusBar();
  setInterval(updateStatusBar, 30000);

  // ── Fleet Status Bar ──
  const fleetBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 99);
  fleetBar.command = 'context-dna.showFleetStatus';
  fleetBar.text = '$(shield) Fleet: ?';
  fleetBar.tooltip = 'Multi-Fleet status — click for details';
  fleetBar.show();
  context.subscriptions.push(fleetBar);

  interface FleetHealth {
    nodeId: string;
    status: string;
    transport: string;
    uptime_s: number;
    activeSessions: number;
    peers: Record<string, { lastSeen: number; sessions: number; vscode: string }>;
    stats: { sent: number; received: number; broadcasts: number; errors: number };
  }

  async function updateFleetBar() {
    try {
      const response = await fetch(`${FLEET_URL()}/health`);
      if (!response.ok) { throw new Error(`HTTP ${response.status}`); }
      const health = (await response.json()) as FleetHealth;

      const peerCount = Object.keys(health.peers).length;
      const totalNodes = peerCount + 1; // self + peers
      const allHealthy = Object.values(health.peers).every(p => p.lastSeen < 120);

      if (allHealthy && peerCount > 0) {
        fleetBar.text = `$(shield) F:${totalNodes}`;
        fleetBar.backgroundColor = undefined;
      } else if (peerCount > 0) {
        fleetBar.text = `$(shield) F:${totalNodes}!`;
        fleetBar.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
      } else {
        fleetBar.text = `$(shield) F:1`;
        fleetBar.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
      }

      const peerNames = Object.keys(health.peers).join(', ');
      fleetBar.tooltip = `Fleet: ${health.nodeId} | ${peerCount} peers (${peerNames}) | ${health.activeSessions} sessions | ${health.stats.sent} sent`;
    } catch {
      fleetBar.text = '$(shield) Fleet: off';
      fleetBar.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
      fleetBar.tooltip = 'Fleet daemon not running';
    }
  }

  // Fleet status command — show detailed info
  context.subscriptions.push(
    vscode.commands.registerCommand('context-dna.showFleetStatus', async () => {
      try {
        const response = await fetch(`${FLEET_URL()}/health`);
        if (!response.ok) { throw new Error(`HTTP ${response.status}`); }
        const health = (await response.json()) as FleetHealth;

        const lines = [
          `Node: ${health.nodeId} (${health.status})`,
          `Transport: ${health.transport}`,
          `Uptime: ${Math.floor(health.uptime_s / 3600)}h ${Math.floor((health.uptime_s % 3600) / 60)}m`,
          `Sessions: ${health.activeSessions}`,
          `Messages: ${health.stats.sent} sent, ${health.stats.received} recv, ${health.stats.errors} errors`,
          '',
          'Peers:',
          ...Object.entries(health.peers).map(([name, p]) =>
            `  ${p.lastSeen < 60 ? '●' : p.lastSeen < 300 ? '◐' : '○'} ${name}: ${p.sessions} sessions, seen ${p.lastSeen}s ago`
          ),
        ];

        const action = await vscode.window.showInformationMessage(
          lines.join('\n'),
          { modal: true },
          'Run Doctor', 'Open Dashboard'
        );

        if (action === 'Run Doctor') {
          const terminal = vscode.window.createTerminal('Fleet Doctor');
          terminal.sendText('MULTIFLEET_NODE_ID=$(hostname -s | tr "[:upper:]" "[:lower:]") PYTHONPATH=multi-fleet python3 -m multifleet.doctor');
          terminal.show();
        } else if (action === 'Open Dashboard') {
          const terminal = vscode.window.createTerminal('Fleet Dashboard');
          terminal.sendText('curl -sf http://127.0.0.1:8855/dashboard');
          terminal.show();
        }
      } catch {
        vscode.window.showErrorMessage('Fleet daemon not running. Start with: launchctl load ~/Library/LaunchAgents/io.contextdna.fleet-nats.plist');
      }
    })
  );

  updateFleetBar();
  const fleetInterval = setInterval(updateFleetBar, 15000); // Every 15s for fleet
  context.subscriptions.push({ dispose: () => clearInterval(fleetInterval) });
}

export function deactivate() {
  console.log('Context DNA extension deactivated');
}
