# ER Simulator AWS Terraform Infrastructure

This Terraform project provisions the core AWS backend infrastructure for the ER Simulator backend.

It creates:

- A VPC with public and private subnets, NAT gateway, and routing
- A public EC2 instance (Amazon Linux 2023) behind an Application Load Balancer
- A private S3 bucket (and a secondary bucket) for app assets / logs (versioned, encrypted, private)
- A production PostgreSQL RDS instance
- A Redis (ElastiCache) cluster
- IAM roles for backend compute and future CI/CD

## 1. Prerequisites

- Terraform v1.6.0 or later
- AWS CLI or environment credentials configured for an account that can create VPC, EC2, RDS, ElastiCache, IAM, and S3 resources
- An S3 bucket name that is globally unique (configurable via `terraform.tfvars`)

## 2. Directory

From the repo root:

```bash
cd infra/aws/terraform
```

## 3. Configure Variables

Copy the example tfvars file and edit it:

```bash
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and at minimum set:

- `db_password` – a strong Postgres password (or override in `terraform.tfvars.local`)
- `assets_bucket_name` / `assets_bucket_name_2` – globally unique bucket names
- Optionally adjust:
  - `project_name`
  - `ec2_instance_type`
  - `vpc_cidr` / subnet CIDRs

For **local, uncommitted overrides** (e.g. real passwords and SSH keys), create `terraform.tfvars.local` and use:

```hcl
# terraform.tfvars.local (do NOT commit)
db_password        = "REAL_STRONG_PASSWORD"
assets_bucket_name = "ersimulator-core-assets-yourname-2025"
assets_bucket_name_2 = "ersimulator-core-assets-logs-yourname-2025"
public_ssh_key     = "ssh-rsa AAAA... your real key ..."
```

Then run plan/apply with:

```bash
terraform plan  -var-file="terraform.tfvars.local"
terraform apply -var-file="terraform.tfvars.local"
```

## 4. Initialize and Apply

Initialize Terraform:

```bash
terraform init
```

Review the plan (with your local overrides):

```bash
terraform plan -var-file="terraform.tfvars.local"
```

Apply the infrastructure:

```bash
terraform apply -var-file="terraform.tfvars.local"
```

Provisioning EC2/RDS/ElastiCache may take several minutes.

## 5. Outputs and Environment Variables

After `terraform apply` completes, Terraform will print several outputs.

Key ones to copy into your backend `.env` are:

### RDS Postgres

- **Output:** `rds_database_url`
- **Example format:**

  ```text
  postgres://ersim_app:YOUR_PASSWORD@ersim-prod-db.<RDS-ENDPOINT-HASH>.us-west-2.rds.amazonaws.com:5432/ersim_prod
  ```

Paste this into `DATABASE_URL` in `backend/.env`.

### Redis (ElastiCache)

- **Output:** `redis_url`
- **Example format:**

  ```text
  redis://ersim-prod-redis.<ELASTICACHE-ENDPOINT-HASH>.us-west-2.cache.amazonaws.com:6379/0
  ```

Paste this into `REDIS_URL` in `backend/.env`.

### S3 Assets Buckets

- **Outputs:**
  - `assets_bucket_name`
  - `assets_bucket_name_2`
  - `assets_bucket_region`

Paste primary values into:

- `AWS_S3_BUCKET` = `assets_bucket_name`
- `AWS_S3_REGION` = `assets_bucket_region`

Use the secondary bucket for logs/backups as needed.

### EC2 / ALB

- **Outputs:**
  - `ec2_public_ip`
  - `alb_dns_name`
  - `ssh_command`

Use `ssh_command` to connect for debugging, and `alb_dns_name` as the primary HTTP endpoint.

### IAM Roles

- **Outputs:**
  - `ec2_instance_role_arn`
  - `deploy_role_arn`

These can be used later in CI/CD or when moving towards ECS.

## 6. Notes and Safety

- This Terraform configuration **does not** store or expose API keys or secrets.
- All secrets should remain in your backend `.env` file (for local/dev) or in a secret manager (e.g., SSM Parameter Store/Secrets Manager) for production.
- The generated RDS and Redis URLs contain hostnames only; you are responsible for keeping actual passwords and API keys private.

## 7. Next Steps

Once infrastructure is provisioned and backend `.env` is updated:

1. Confirm the backend is reachable via the ALB DNS.
2. SSH to the EC2 instance using the `ssh_command` output if you need to debug.
3. When ready, you can introduce CI/CD and, later, an ECS task/service that reuses this VPC, ALB, and IAM layout.

## 8. Turn Capture API (Voice Sessions)

The Turn Capture API provides a permanent, non-expiring authentication method for capturing voice session conversation turns. This is implemented via AWS API Gateway + Lambda.

### What It Does

- Frontend sends conversation turns (learner speech, AI responses) to AWS API Gateway
- Lambda validates and forwards to Django backend's internal endpoint
- Uses API key authentication instead of user JWT tokens
- Works for all users on all device types without auth expiration issues

### Variables Required

Add these to your `terraform.tfvars.local`:

```hcl
# Turn Capture API
turn_capture_backend_url       = "https://api.ersimulator.com"
turn_capture_internal_api_key  = "your-random-secret-key-here"  # Generate with: openssl rand -hex 32
```

### Outputs After Apply

- **`turn_capture_api_url`**: The API Gateway URL for frontend to call (e.g., `https://abc123.execute-api.us-west-2.amazonaws.com/turn`)
- **`turn_capture_api_key_ssm_path`**: SSM path where API key is stored

### Retrieving the API Key

After `terraform apply`, retrieve the API key from SSM:

```bash
aws ssm get-parameter \
  --name "/ersim/prod/turn-capture-api-key" \
  --with-decryption \
  --query "Parameter.Value" \
  --output text
```

### Frontend Environment Variables

Add these to your web-app `.env.local` or production environment:

```bash
NEXT_PUBLIC_TURN_CAPTURE_API_URL=<turn_capture_api_url output>
NEXT_PUBLIC_TURN_CAPTURE_API_KEY=<api key from SSM>
```

### Backend Environment Variables

Add this to your Django `.env`:

```bash
TURN_CAPTURE_INTERNAL_API_KEY=<same value as turn_capture_internal_api_key>
```

### Architecture

```
Frontend (web-app/mobile-app)
    │
    │ POST /turn (with x-api-key header)
    ▼
AWS API Gateway (HTTP API)
    │
    │ Invoke
    ▼
AWS Lambda (turn-capture)
    │
    │ POST /api/voice/realtime/internal-save-turn/
    │ (with X-Internal-API-Key header)
    ▼
Django Backend
    │
    │ Store in ConversationTurn table
    ▼
PostgreSQL
```

### Fallback Behavior

If `NEXT_PUBLIC_TURN_CAPTURE_API_URL` and `NEXT_PUBLIC_TURN_CAPTURE_API_KEY` are not set, the frontend automatically falls back to the direct Django endpoint using Supabase JWT authentication.
