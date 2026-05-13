# cli.js Null-Safe Patch — Survives `npm i -g @anthropic-ai/claude-code`

Patches the globally-installed Claude Code `cli.js` to make `em1().alwaysThinking`
null-safe. Without this patch, the multifleet/MCP bridge intermittently crashes
with:

```
TypeError: Cannot read properties of null (reading 'alwaysThinking')
```

…which kills Claude Code sessions mid-task.

## What it does

In `/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js`, rewrites:

```js
em1().alwaysThinking
```

to:

```js
(em1()||{}).alwaysThinking
```

…at every occurrence (currently 2 sites). Idempotent — re-running detects the
already-patched state and exits cleanly.

## Why it lives in this repo (not /tmp)

The original `/tmp/cli.js.bak` was wiped by macOS `/tmp` purge. Every
`npm i -g @anthropic-ai/claude-code` reinstall resets the file. We need a
durable, version-controlled re-application path.

We do **not** check in the 9 MB `cli.js` blob. Instead the script:
1. Reads the live cli.js
2. SHA256-fingerprints the pre-patch content
3. Backs up to `tools/cli-js-patches/.cli.js.<sha16>.bak` (sha-pinned, gitignored)
4. Applies the substring replacement
5. Logs to `applied.log`

## Usage

```bash
bash tools/cli-js-patches/apply.sh           # apply (default)
bash tools/cli-js-patches/apply.sh --check   # report state, no changes
bash tools/cli-js-patches/apply.sh --revert  # restore latest backup
```

## When to re-run

Re-run after **any** of these:

- `npm i -g @anthropic-ai/claude-code` (manual upgrade)
- `npm update -g` (catch-all upgrade)
- `brew upgrade node` (rarely re-installs node_modules but occasionally does)
- Claude Code self-updates (if/when it ships an in-place updater)

## Triggering automatically (recommended)

Pick **one** of these — manual is fine if you rarely upgrade.

### Option A: zsh post-install hook (lightest)

Add to `~/.zshrc`:

```sh
# Re-apply Claude Code cli.js null-safe patch after global npm installs.
claude_code_postinstall() {
  npm "$@"
  if [[ "$*" == *"@anthropic-ai/claude-code"* ]]; then
    bash "$HOME/dev/er-simulator-superrepo/tools/cli-js-patches/apply.sh" || true
  fi
}
alias npm=claude_code_postinstall
```

Trade-off: aliasing `npm` is intrusive. Acceptable on a single-user dev box;
skip on shared machines.

### Option B: launchd file-watcher (most robust)

Create `~/Library/LaunchAgents/com.ersim.cli-js-patch.plist` watching
`/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js` for `WatchPaths`
mtime changes; on fire, run `apply.sh`. Loads at login, fires automatically
when npm rewrites the file.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ersim.cli-js-patch</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>/Users/aarontjomsland/dev/er-simulator-superrepo/tools/cli-js-patches/apply.sh</string>
  </array>
  <key>WatchPaths</key><array>
    <string>/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js</string>
  </array>
  <key>StandardOutPath</key><string>/tmp/cli-js-patch.log</string>
  <key>StandardErrorPath</key><string>/tmp/cli-js-patch.err</string>
</dict></plist>
```

Load with `launchctl load ~/Library/LaunchAgents/com.ersim.cli-js-patch.plist`.

### Option C: manual (current default)

After every claude-code upgrade, run:

```bash
bash tools/cli-js-patches/apply.sh
```

## Files

- `apply.py` — patcher (check / apply / revert)
- `apply.sh` — bash wrapper
- `applied.log` — append-only audit log of patch/revert events (gitignored)
- `.cli.js.<sha16>.bak` — sha-pinned pre-patch backups (gitignored)

## Caveat

If upstream cli.js refactors `em1()` away, `apply.py --check` will report
`pattern not found — upstream may have refactored em1()` and exit 3. At that
point investigate whether the upstream fix landed and retire this patch.
