/**
 * 3-Surgeon Language Model Tools for VS Code Agent Mode
 *
 * Layer 3 adapter: thin shims (~150 lines) that register 3-surgeon
 * capabilities as VS Code Language Model Tools. AI agents (Copilot,
 * Claude, etc.) can invoke these tools during chat to get multi-LLM
 * cross-examination, consensus checks, and risk assessment.
 *
 * All logic lives in the Python core (Layer 1) behind the protocol
 * server (Layer 2). This file only does HTTP calls + VS Code glue.
 */

import * as vscode from 'vscode';

// ── Types ────────────────────────────────────────────────────────────

interface CrossExamInput {
  topic: string;
  mode?: 'single' | 'iterative' | 'continuous';
  depth?: 'full' | 'quick';
}

interface ConsultInput {
  topic: string;
}

interface ConsensusInput {
  claim: string;
}

// ── Server communication ─────────────────────────────────────────────

function getServerUrl(): string {
  return vscode.workspace
    .getConfiguration('context-dna')
    .get('apiUrl', 'http://127.0.0.1:3456');
}

async function callTool<T>(
  toolName: string,
  params: Record<string, unknown>,
  token?: vscode.CancellationToken,
  timeoutMs = 120_000,
): Promise<T> {
  const url = `${getServerUrl()}/tool/${toolName}`;

  // Timeout + VS Code cancellation → abort signal
  const signal = AbortSignal.timeout(timeoutMs);
  const cancelDisposable = token?.onCancellationRequested(() => {
    // token cancelled → throw to caller (signal.reason preserves cause)
    throw new vscode.CancellationError();
  });

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
      signal,
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(`3-Surgeons server error (${response.status}): ${text}`);
    }

    return (await response.json()) as T;
  } finally {
    cancelDisposable?.dispose();
  }
}

async function isServerRunning(): Promise<boolean> {
  try {
    const response = await fetch(`${getServerUrl()}/health`, { signal: AbortSignal.timeout(2000) });
    return response.ok;
  } catch {
    return false;
  }
}

function serverDownResult(): vscode.LanguageModelToolResult {
  return new vscode.LanguageModelToolResult([
    new vscode.LanguageModelTextPart(JSON.stringify({
      error: "3-Surgeons server not running. Start with: 3s serve",
      fallback: "Consider running the analysis manually."
    })),
  ]);
}

// ── Tool implementations ─────────────────────────────────────────────

class CrossExamineTool implements vscode.LanguageModelTool<CrossExamInput> {
  async prepareInvocation(
    options: vscode.LanguageModelToolInvocationPrepareOptions<CrossExamInput>,
    _token: vscode.CancellationToken
  ): Promise<vscode.PreparedToolInvocation> {
    const mode = options.input.mode ?? 'single';
    const modeLabel: Record<string, string> = {
      single: '1 iteration',
      iterative: 'up to 3 iterations',
      continuous: 'up to 5 iterations with open exploration',
    };
    return {
      invocationMessage: `Cross-examining "${options.input.topic}" with 3 surgeons (${modeLabel[mode] ?? mode})`,
    };
  }

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<CrossExamInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    if (!(await isServerRunning())) {
      return serverDownResult();
    }

    const result = await callTool<Record<string, unknown>>('cross_examine', {
      topic: options.input.topic,
      mode: options.input.mode ?? 'single',
      depth: options.input.depth ?? 'full',
    }, token);

    return new vscode.LanguageModelToolResult([
      new vscode.LanguageModelTextPart(JSON.stringify(result, null, 2)),
    ]);
  }
}

class ConsultTool implements vscode.LanguageModelTool<ConsultInput> {
  async prepareInvocation(
    options: vscode.LanguageModelToolInvocationPrepareOptions<ConsultInput>,
    _token: vscode.CancellationToken
  ): Promise<vscode.PreparedToolInvocation> {
    return {
      invocationMessage: `Consulting 3 surgeons on "${options.input.topic}"`,
    };
  }

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<ConsultInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    if (!(await isServerRunning())) {
      return serverDownResult();
    }

    const result = await callTool<Record<string, unknown>>('consult', {
      topic: options.input.topic,
    }, token);

    return new vscode.LanguageModelToolResult([
      new vscode.LanguageModelTextPart(JSON.stringify(result, null, 2)),
    ]);
  }
}

class ConsensusTool implements vscode.LanguageModelTool<ConsensusInput> {
  async prepareInvocation(
    options: vscode.LanguageModelToolInvocationPrepareOptions<ConsensusInput>,
    _token: vscode.CancellationToken
  ): Promise<vscode.PreparedToolInvocation> {
    return {
      invocationMessage: `Checking 3-surgeon consensus on "${options.input.claim}"`,
    };
  }

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<ConsensusInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    if (!(await isServerRunning())) {
      return serverDownResult();
    }

    const result = await callTool<Record<string, unknown>>('consensus', {
      claim: options.input.claim,
    }, token);

    return new vscode.LanguageModelToolResult([
      new vscode.LanguageModelTextPart(JSON.stringify(result, null, 2)),
    ]);
  }
}

class ProbeTool implements vscode.LanguageModelTool<Record<string, never>> {
  async prepareInvocation(
    _options: vscode.LanguageModelToolInvocationPrepareOptions<Record<string, never>>,
    _token: vscode.CancellationToken
  ): Promise<vscode.PreparedToolInvocation> {
    return {
      invocationMessage: 'Checking 3-surgeon system health',
    };
  }

  async invoke(
    _options: vscode.LanguageModelToolInvocationOptions<Record<string, never>>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    if (!(await isServerRunning())) {
      return serverDownResult();
    }

    const result = await callTool<Record<string, unknown>>('probe', {}, token);

    return new vscode.LanguageModelToolResult([
      new vscode.LanguageModelTextPart(JSON.stringify(result, null, 2)),
    ]);
  }
}

// ── Registration ─────────────────────────────────────────────────────

export function registerSurgeonTools(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.lm.registerTool('three-surgeons_cross-examine', new CrossExamineTool()),
    vscode.lm.registerTool('three-surgeons_consult', new ConsultTool()),
    vscode.lm.registerTool('three-surgeons_consensus', new ConsensusTool()),
    vscode.lm.registerTool('three-surgeons_probe', new ProbeTool())
  );

  // Log registration status
  isServerRunning().then((running) => {
    if (running) {
      console.log('3-Surgeons: tools registered, server running');
    } else {
      console.log('3-Surgeons: tools registered, server NOT running (tools will fail gracefully)');
    }
  });
}
