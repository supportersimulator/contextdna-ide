# `infra/backup/` — Making the Operational Invariance Promise Real

These three scripts are the *load-bearing* implementation behind [docs/operational-invariance.md §5](../../docs/operational-invariance.md). Without them, the promise *"if your laptop fell in the ocean today and your AWS account was deleted tomorrow, the mothership would still come back"* is aspirational. With them, it's verifiable.

## What's here

| Script | Cadence | What it does |
|--------|---------|--------------|
| `pg-dump.sh` | Daily (launchd / cron) | `pg_dump` of the evidence-ledger Postgres → gzip → age-encrypt → upload to S3-compatible bucket. 90-day retention sweep. |
| `jetstream-snapshot.sh` | Weekly | Snapshot of all KV buckets (`fleet_roster`, `evidence_chain`, etc.) + named streams → tar.gz → age-encrypt → upload. |
| `restore.sh` | On disaster | Symmetric inverse: list, download, decrypt, restore — for either / both. |

All three are **stdlib + standard CLI only**: `pg_dump`, `psql`, `nats`, `aws`, `age`, `tar`, `gzip`. No vendor lock-in. Works against AWS S3, Backblaze B2, Wasabi, or any S3-compatible target via `BACKUP_S3_ENDPOINT`.

## Setup (one-time, 10 minutes)

1. **Generate an age keypair.** This is the single secret that protects every backup. Keep the *private* key file (the `identity`) somewhere offline — a YubiKey, an encrypted thumb drive, a printed paper key. The public key (the `recipient`) goes in `.env` on every machine that runs backups.

   ```bash
   age-keygen -o ~/.ssh/contextdna-backup.age.key
   # Public line printed to stdout — copy it.
   grep '^# public key:' ~/.ssh/contextdna-backup.age.key
   ```

2. **Create the bucket.** Any S3-compatible bucket works. For B2 / Wasabi:

   ```bash
   aws s3 mb s3://contextdna-backups --endpoint-url https://s3.us-west-002.backblazeb2.com
   ```

3. **Set env vars.** Append to your existing `.env`:

   ```bash
   BACKUP_BUCKET=s3://contextdna-backups
   BACKUP_S3_ENDPOINT=https://s3.us-west-002.backblazeb2.com   # optional, omit for AWS
   BACKUP_AGE_PUBKEY=age1...                                    # from step 1
   BACKUP_AGE_KEYFILE=$HOME/.ssh/contextdna-backup.age.key      # only on restore machine
   BACKUP_RETENTION_DAYS=90
   ```

4. **Smoke test.**

   ```bash
   bash infra/backup/pg-dump.sh --dry-run
   bash infra/backup/jetstream-snapshot.sh --dry-run
   ```

   Both should print `OK` for every preflight check and exit 0.

5. **Schedule.** A sample launchd plist (macOS) and a sample cron line (Linux) live in [`../scripts/`](../scripts/). Daily at 03:00 for pg, weekly Sunday at 04:00 for JetStream.

## Restoring on a fresh machine

This is the full *"my AWS account was deleted"* recovery:

```bash
# 1. Clone
git clone git@github.com:supportersimulator/contextdna-ide.git
cd contextdna-ide

# 2. Bring back .env from your offline backup (1Password / age file / printed)
cp ~/Documents/contextdna-env-backup .env
source .env

# 3. Bring back the age identity file from offline backup
cp ~/Documents/contextdna-backup.age.key ~/.ssh/

# 4. List what's recoverable
bash infra/backup/restore.sh --list

# 5. Bring up the stack with empty DBs
docker compose -f docker-compose.lite.yml up -d

# 6. Restore the data
bash infra/backup/restore.sh --kind all --latest

# 7. Verify
curl -sf http://localhost:8855/health | jq '.evidence_count'
```

If step 7 returns the count you remember from yesterday, **the promise held**.

## ZSF guarantees

Every script:
- Exits non-zero on any failure.
- Prints `[<script>] FAIL: <reason>` to stderr on every failure path.
- Prints `[<script>] OK: <action>` on every success step.
- Cleans up temp files on exit (trap).
- Never silently swallows an error.

You can grep logs for `FAIL:` to find every problem. There is no `2>/dev/null` swallowing anywhere.

## Testing recovery (recommended quarterly)

The full *operational invariance* test:

```bash
# On a throwaway machine or VM:
git clone <repo>
cd contextdna-ide
# Pretend you have nothing else. Restore .env + age key from your offline source.
docker compose -f docker-compose.lite.yml up -d
bash infra/backup/restore.sh --kind all --latest
# Run the bootstrap verification script
bash scripts/bootstrap-verify.sh
# Expected: BOOTSTRAP-VERIFIED printed at the end.
```

If `BOOTSTRAP-VERIFIED` doesn't print, the promise isn't kept — investigate and fix.
