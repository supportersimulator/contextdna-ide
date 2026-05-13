# iam_key_rotator

AWS Lambda that rotates IAM access keys stored as Secrets Manager secrets.

## Secret schema

```json
{
  "access_key_id": "AKIA...",
  "secret_access_key": "...",
  "user": "iam-user-name",
  "rotated": "YYYY-MM-DD"
}
```

## Wired to

- `/ersim/prod/iam/terraform-ersim-prod` (30-day cadence)
- `/ersim/prod/iam/cursor-deploy` (30-day cadence)

## Deploy

```bash
./deploy.sh
```

See `deploy.sh` for zip/upload commands and execution-role setup.

## Manual test (dummy user)

```bash
scripts/test-rotation.sh
```

Creates a throwaway IAM user, wires a secret, runs one rotation, verifies
the new key works, tears everything down.
