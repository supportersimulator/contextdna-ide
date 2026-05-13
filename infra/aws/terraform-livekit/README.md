# LiveKit CPU Infrastructure

Dedicated CPU instance for LiveKit Server + Voice Agent, separate from the GPU instance running STT/TTS/LLM services.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              AWS VPC                                     │
│                        (vpc-01e46e3b26b272a7e)                          │
│                                                                          │
│  ┌─────────────────────────┐      ┌─────────────────────────┐          │
│  │   LiveKit CPU Instance   │      │    GPU Voice Instance    │          │
│  │     (c6i.xlarge)         │      │      (g5.xlarge)         │          │
│  │   ersim-prod-livekit     │      │    ersim-voice-gpu       │          │
│  │                          │      │                          │          │
│  │  ┌──────────────────┐   │      │  ┌──────────────────┐   │          │
│  │  │  LiveKit Server  │   │      │  │  Faster Whisper  │   │          │
│  │  │    (port 7880)   │   │      │  │   STT (:8081)    │   │          │
│  │  └──────────────────┘   │      │  └──────────────────┘   │          │
│  │           │              │      │                          │          │
│  │  ┌──────────────────┐   │      │  ┌──────────────────┐   │          │
│  │  │   Voice Agent    │───┼──────┼─▶│   Kyutai TTS     │   │          │
│  │  │  (LiveKit SDK)   │   │      │  │  HTTP (:8000)    │   │          │
│  │  └──────────────────┘   │      │  │  WS (:8089)      │   │          │
│  │                          │      │  └──────────────────┘   │          │
│  │  Public: 35.161.30.189  │      │                          │          │
│  │  Private: 10.0.2.x      │      │  ┌──────────────────┐   │          │
│  └─────────────────────────┘      │  │    vLLM Qwen     │   │          │
│                                    │  │    LLM (:8090)   │   │          │
│                                    │  └──────────────────┘   │          │
│                                    │                          │          │
│                                    │  Private: 10.0.1.241    │          │
│                                    └─────────────────────────┘          │
└─────────────────────────────────────────────────────────────────────────┘

External Access:
  - livekit.ersimulator.com → 35.161.30.189 (NOT proxied - WebRTC requires direct)
  - WebSocket: wss://livekit.ersimulator.com (port 7880)
  - WebRTC UDP: ports 50000-60000
```

## Resources Created

| Resource | Name | Details |
|----------|------|---------|
| EC2 Instance | `ersim-prod-livekit` | c6i.xlarge (4 vCPU, 8GB RAM) |
| Elastic IP | `ersim-prod-livekit-eip` | 35.161.30.189 |
| Security Group | `ersim-prod-livekit-sg` | Ports 22, 7880, 7881, 50000-60000/UDP |
| IAM Role | `ersim-prod-livekit-role` | ECR, CloudWatch Logs, Secrets Manager |
| DNS Record | `livekit.ersimulator.com` | A record, NOT proxied |

## Instance Specifications

- **Instance Type**: c6i.xlarge
- **vCPU**: 4
- **RAM**: 8GB (per LiveKit agent recommendations)
- **Storage**: 30GB gp3 (encrypted)
- **AMI**: Amazon Linux 2023
- **Cost**: ~$124/month

## Services Running

### LiveKit Server
- **Image**: `livekit/livekit-server:latest`
- **Port**: 7880 (WebSocket), 7881 (RTC TCP), 50000-60000 (WebRTC UDP)
- **Config**: `/opt/livekit/livekit.yaml`

### Voice Agent
- **Source Code**: `ersim-voice-stack/services/agent/`
- **Image**: `831646886161.dkr.ecr.us-west-2.amazonaws.com/ersim-voice/livekit-agent:latest`
- **Config**: `/opt/agent/.env`
- **Connects to GPU services via private IP**

## Configuration Files

### LiveKit Config (`/opt/livekit/livekit.yaml`)
```yaml
port: 7880
rtc:
  port_range_start: 50000
  port_range_end: 60000
  tcp_port: 7881
  use_external_ip: true
redis:
  address: ""
keys:
  <API_KEY>: <API_SECRET>
logging:
  level: info
```

### Agent Environment (`/opt/agent/.env`)
```bash
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=<from-tfvars>
LIVEKIT_API_SECRET=<from-tfvars>
STT_BASE_URL=http://10.0.1.241:8081
TTS_BASE_URL=http://10.0.1.241:8000
LLM_BASE_URL=http://10.0.1.241:8090
KYUTAI_TTS_WS_URL=ws://10.0.1.241:8089/api/tts_streaming
```

## Credentials

### Cloudflare (from AWS Secrets Manager)
- API Token: `/ersim/prod/future/CLOUDFLARE_API_TOKEN`
- Zone ID: `/ersim/prod/infra/CLOUDFLARE_ZONE_ID`

### LiveKit (from terraform.tfvars)
- API Key: `livekit_api_key`
- API Secret: `livekit_api_secret`

## SSH Access

```bash
ssh -i ~/.ssh/ersim-keypair ec2-user@35.161.30.189
```

## Management Commands

### Check Services
```bash
# SSH into instance
ssh -i ~/.ssh/ersim-keypair ec2-user@35.161.30.189

# Check running containers
sudo docker ps

# View LiveKit logs
sudo docker logs livekit-server

# View Agent logs
sudo docker logs voice-agent

# Check cloud-init setup log
sudo cat /var/log/cloud-init-output.log
```

### Restart Services
```bash
cd /opt/livekit
sudo docker compose restart
```

### Update Agent
```bash
# Pull latest image
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin 831646886161.dkr.ecr.us-west-2.amazonaws.com
sudo docker pull 831646886161.dkr.ecr.us-west-2.amazonaws.com/ersim-voice/livekit-agent:latest

# Restart
cd /opt/livekit
sudo docker-compose up -d
```

## Terraform Commands

```bash
cd infra/aws/terraform-livekit

# Plan changes
terraform plan

# Apply changes
terraform apply

# Destroy (careful!)
terraform destroy

# Show outputs
terraform output
```

## Outputs

| Output | Description |
|--------|-------------|
| `livekit_public_ip` | Public IP of LiveKit instance |
| `livekit_url` | WebSocket URL (wss://livekit.ersimulator.com) |
| `livekit_ssh_command` | SSH command to connect |
| `gpu_private_ip` | GPU instance private IP |
| `voice_stt_url` | STT service URL |
| `voice_tts_http_url` | TTS HTTP URL |
| `voice_tts_ws_url` | TTS WebSocket URL |
| `voice_llm_url` | LLM service URL |

## Security

### Inbound Rules
| Port | Protocol | Source | Description |
|------|----------|--------|-------------|
| 22 | TCP | 0.0.0.0/0 | SSH |
| 7880 | TCP | 0.0.0.0/0 | LiveKit WebSocket |
| 7881 | TCP | 0.0.0.0/0 | LiveKit RTC TCP |
| 50000-60000 | UDP | 0.0.0.0/0 | WebRTC Media |

### IAM Permissions
- ECR: Pull container images
- CloudWatch Logs: Write logs
- Secrets Manager: Read `/ersim/*` secrets

## Troubleshooting

### Instance not responding
1. Check instance status in AWS Console
2. Check security group rules
3. Verify Elastic IP is attached

### LiveKit not starting
```bash
# Check Docker status
sudo systemctl status docker

# Check LiveKit config
sudo cat /opt/livekit/livekit.yaml

# Check Docker logs
sudo docker logs livekit-server
```

### Agent can't connect to GPU services
1. Verify GPU instance is running: `aws ec2 describe-instances --instance-ids i-0004495084cf78b61`
2. Check security group allows traffic from LiveKit instance
3. Test connectivity: `curl http://10.0.1.241:8081/health`

### DNS not resolving
1. Check Cloudflare dashboard for `livekit.ersimulator.com`
2. Verify record is NOT proxied (orange cloud off)
3. Wait for DNS propagation (up to 5 minutes)

## Related Documentation

- [AWS EC2 & ECS Architecture](../../docs/aws-ec2-ec2-ecs.md)
- [Full Stack Integration Spec](../../docs/aws-integration-spec-full-stack.md)
- [LiveKit CPU Migration Plan](../../ersim-voice-stack/LIVEKIT-CPU-MIGRATION.md)
- [Voice API Documentation](../../docs/api-voice.md)
