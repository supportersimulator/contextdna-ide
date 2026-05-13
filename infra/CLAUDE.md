# Infrastructure — AWS, Terraform, Docker

Sub-project: **infrastructure** (infra/, system/)

## Rehydration
```bash
cd /Users/aarontjomsland/Documents/er-simulator-superrepo
PYTHONPATH=. .venv/bin/python3 memory/session_historian.py rehydrate --project infrastructure
```

## Architecture
- AWS: EC2, ECS, Lambda, NLB, Route53, CloudFront
- Terraform for IaC
- Docker Compose for local development

## Constraints
- GPU instances: use Internal NLB (private IP changes on ASG restart)
- NLB idle timeout 60s — WebSocket connections need keepalive
- Always sanitize secrets with ${PLACEHOLDER} before storing
