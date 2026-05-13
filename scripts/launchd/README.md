# Cloudflare Tunnel LaunchDaemon

Auto-starts cloudflared tunnel on machine boot/login.

## Installation

```bash
# Copy plist to LaunchAgents
cp scripts/launchd/com.cloudflare.tunnel.plist ~/Library/LaunchAgents/

# Load immediately
launchctl load ~/Library/LaunchAgents/com.cloudflare.tunnel.plist

# Verify running
launchctl list | grep cloudflare
```

## Management

```bash
# Stop tunnel
launchctl unload ~/Library/LaunchAgents/com.cloudflare.tunnel.plist

# Start tunnel
launchctl load ~/Library/LaunchAgents/com.cloudflare.tunnel.plist

# Check status
launchctl list | grep cloudflare
```

## Logs

- stdout: `~/.cloudflared/tunnel.log`
- stderr: `~/.cloudflared/tunnel.error.log`

```bash
# View logs
tail -f ~/.cloudflared/tunnel.log
```

## Tunnel Details

- Tunnel ID: `8d30fb13-b9d5-4ac0-9f7d-10785989484c`
- Hostname: `voice.contextdna.io`
- Target: `http://localhost:8888`
