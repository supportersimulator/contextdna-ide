###############################################
# FIND LATEST AMAZON LINUX 2023 AMI
###############################################
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

###############################################
# SSH KEY PAIR (public key only)
###############################################
resource "aws_key_pair" "ersim_key" {
  key_name   = "ersim-keypair"
  public_key = var.public_ssh_key
}

###############################################
# APP EC2 INSTANCE
###############################################
resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.ec2_instance_type
  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.sg_app.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_instance_profile.name
  key_name               = aws_key_pair.ersim_key.key_name

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
  }

  user_data = <<-EOF
#!/bin/bash
set -euxo pipefail

############################################
# Update System & Install Dependencies
############################################
dnf update -y
dnf install -y git nginx python3.11 python3.11-pip

############################################
# Install and enable SSM Agent (AL2023 compatible)
############################################
dnf install -y https://s3.amazonaws.com/ec2-downloads-windows/SSMAgent/latest/linux_amd64/amazon-ssm-agent.rpm
systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

############################################
# Create Application Directory
############################################
mkdir -p /var/www/ersim
cd /var/www/ersim

############################################
# Clone Superrepo (Backend lives in /backend)
############################################
if [ ! -d app ]; then
  git clone https://github.com/supportersimulator/er-simulator-superrepo.git app
fi

cd /var/www/ersim/app/backend

############################################
# Python Dependencies
############################################
python3.11 -m pip install --upgrade pip
python3.11 -m pip install -r requirements.txt

############################################
# Django Setup
############################################
python3.11 manage.py migrate --noinput || true
python3.11 manage.py collectstatic --noinput || true

############################################
# Gunicorn Systemd Service
############################################
cat > /etc/systemd/system/gunicorn.service << 'UNIT'
[Unit]
Description=Gunicorn daemon for ERSIM Backend
After=network.target

[Service]
User=ec2-user
Group=nginx
WorkingDirectory=/var/www/ersim/app/backend
ExecStart=/usr/bin/python3.11 -m gunicorn ersim_backend.wsgi:application \
  --workers 3 \
  --bind unix:/run/gunicorn.sock
Restart=always
Environment=DJANGO_SETTINGS_MODULE=ersim_backend.settings.production
Environment=DJANGO_ENV=production
# Voice backend: use self-hosted LiveKit instead of OpenAI Realtime
Environment=VOICE_BACKEND=livekit
# LiveKit on dedicated CPU instance (livekit.ersimulator.com)
Environment=LIVEKIT_URL=wss://livekit.ersimulator.com
EnvironmentFile=-/var/www/ersim/app/backend/.env

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p /run

############################################
# Nginx Configuration
############################################
cat > /etc/nginx/conf.d/ersim.conf << 'NGINXCONF'
server {
    listen 80;
    server_name _;

    location = /favicon.ico { access_log off; log_not_found off; }

    location /static/ {
        alias /var/www/ersim/app/backend/static/;
    }

    location / {
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_pass http://unix:/run/gunicorn.sock;
    }
}
NGINXCONF

rm -f /etc/nginx/conf.d/default.conf || true

############################################
# Start Services
############################################
systemctl daemon-reload
systemctl enable gunicorn
systemctl restart gunicorn
systemctl enable nginx
systemctl restart nginx

EOF

  tags = {
    Name        = "${var.project_name}-${var.environment}-app-instance"
    Environment = var.environment
  }
}

###############################################
# Elastic IP for EC2 instance
###############################################
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = {
    Name        = "${var.project_name}-${var.environment}-app-eip"
    Environment = var.environment
  }
}

###############################################
# Attach instance to ALB target group
###############################################
resource "aws_lb_target_group_attachment" "app" {
  target_group_arn = aws_lb_target_group.app.arn
  target_id        = aws_instance.app.id
  port             = 80
}

###############################################
# LIVEKIT EC2 INSTANCE (Dedicated CPU)
# Compute-optimized c6i.large for WebRTC media
# Runs LiveKit Server + Voice Agent
# ~$62/month on-demand
###############################################
resource "aws_instance" "livekit" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = "c6i.large"  # 2 vCPU, 4GB RAM - optimal for LiveKit
  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.sg_livekit.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_instance_profile.name
  key_name               = aws_key_pair.ersim_key.key_name

  root_block_device {
    volume_type = "gp3"
    volume_size = 30
  }

  user_data = <<-EOF
#!/bin/bash
set -euxo pipefail

############################################
# Update System & Install Dependencies
############################################
dnf update -y
dnf install -y docker jq curl python3 python3-pip

############################################
# Install and enable SSM Agent
############################################
dnf install -y https://s3.amazonaws.com/ec2-downloads-windows/SSMAgent/latest/linux_amd64/amazon-ssm-agent.rpm
systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

############################################
# Start Docker
############################################
systemctl enable docker
systemctl start docker
usermod -aG docker ec2-user

############################################
# Create LiveKit config directory
############################################
mkdir -p /opt/livekit
mkdir -p /opt/livekit/certs
mkdir -p /opt/agent

############################################
# Fetch LiveKit secrets from Secrets Manager
############################################
export AWS_DEFAULT_REGION=us-west-2

LIVEKIT_API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id /ersim/prod/voice/LIVEKIT_API_KEY \
  --query SecretString --output text 2>/dev/null || echo "devkey")

LIVEKIT_API_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id /ersim/prod/voice/LIVEKIT_API_SECRET \
  --query SecretString --output text 2>/dev/null || echo "secret")

############################################
# Install certbot and obtain TLS certificate
# Uses Cloudflare DNS-01 challenge (no port 80 needed)
############################################
pip3 install certbot certbot-dns-cloudflare

# Get Cloudflare API token from Secrets Manager
CLOUDFLARE_API_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id /ersim/prod/future/CLOUDFLARE_API_TOKEN \
  --query SecretString --output text 2>/dev/null || echo "")

if [ -n "$CLOUDFLARE_API_TOKEN" ]; then
  # Create Cloudflare credentials file
  mkdir -p /root/.secrets
  chmod 700 /root/.secrets
  cat > /root/.secrets/cloudflare.ini << CFCREDS
dns_cloudflare_api_token = $CLOUDFLARE_API_TOKEN
CFCREDS
  chmod 600 /root/.secrets/cloudflare.ini

  # Get certificate using DNS-01 challenge
  certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /root/.secrets/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    -d livekit.ersimulator.com \
    --non-interactive \
    --agree-tos \
    --email support@ersimulator.com \
    --cert-path /opt/livekit/certs/cert.pem \
    --key-path /opt/livekit/certs/privkey.pem \
    --fullchain-path /opt/livekit/certs/fullchain.pem \
    || echo "Certificate generation failed, continuing without TLS"

  # Copy certs to LiveKit directory with correct permissions
  if [ -f /etc/letsencrypt/live/livekit.ersimulator.com/fullchain.pem ]; then
    cp /etc/letsencrypt/live/livekit.ersimulator.com/fullchain.pem /opt/livekit/certs/
    cp /etc/letsencrypt/live/livekit.ersimulator.com/privkey.pem /opt/livekit/certs/
    chmod 644 /opt/livekit/certs/fullchain.pem
    chmod 600 /opt/livekit/certs/privkey.pem
    echo "TLS certificates installed successfully"
    TLS_ENABLED=true
  else
    TLS_ENABLED=false
  fi

  # Setup auto-renewal cron job
  echo "0 3 * * * root certbot renew --quiet && cp /etc/letsencrypt/live/livekit.ersimulator.com/*.pem /opt/livekit/certs/ && systemctl restart livekit" > /etc/cron.d/certbot-renew
else
  echo "No Cloudflare API token found, skipping TLS setup"
  TLS_ENABLED=false
fi

############################################
# Create LiveKit config file
# NOTE: LiveKit does NOT support native TLS - we use Nginx for TLS termination
# LiveKit listens on port 7880 (HTTP), Nginx proxies 443 (HTTPS) -> 7880
############################################
cat > /opt/livekit/livekit.yaml << LIVEKITCONF
port: 7880
bind_addresses:
  - ""

rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true
  use_ice_lite: true

# TURN disabled - requires separate TLS setup
# For hospital networks, use TURN server or configure ICE properly
turn:
  enabled: false

# API keys
keys:
  $LIVEKIT_API_KEY: $LIVEKIT_API_SECRET

logging:
  level: info
  json: true

# Room configuration
room:
  auto_create: true
  empty_timeout: 300
LIVEKITCONF

############################################
# Install Nginx for TLS termination (more efficient than Caddy)
############################################
dnf install -y nginx

cat > /etc/nginx/conf.d/livekit.conf << 'NGINXCONF'
# LiveKit WSS/HTTPS reverse proxy
# Optimized for low-latency WebSocket connections

upstream livekit_backend {
    server 127.0.0.1:7880;
    keepalive 256;
}

server {
    listen 443 ssl;
    http2 on;
    server_name livekit.ersimulator.com;

    ssl_certificate /opt/livekit/certs/fullchain.pem;
    ssl_certificate_key /opt/livekit/certs/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    proxy_buffer_size 128k;
    proxy_buffers 4 256k;
    proxy_busy_buffers_size 256k;

    location / {
        proxy_pass http://livekit_backend;
        proxy_http_version 1.1;

        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }
}
NGINXCONF

rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true

############################################
# Create LiveKit systemd service
# Mounts certs directory for TLS support
############################################
cat > /etc/systemd/system/livekit.service << 'UNIT'
[Unit]
Description=LiveKit Server
After=docker.service
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker stop livekit
ExecStartPre=-/usr/bin/docker rm livekit
ExecStart=/usr/bin/docker run --rm --name livekit \
  --network host \
  -v /opt/livekit/livekit.yaml:/etc/livekit.yaml:ro \
  -v /opt/livekit/certs:/certs:ro \
  livekit/livekit-server:latest \
  --config /etc/livekit.yaml
ExecStop=/usr/bin/docker stop livekit

[Install]
WantedBy=multi-user.target
UNIT

############################################
# Create Agent environment file
# Agent connects to GPU box for STT/TTS/LLM
# Uses internal NLB for secure VPC-only access
############################################
cat > /opt/agent/.env << AGENTENV
# LiveKit connection (local server on same host)
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=$LIVEKIT_API_KEY
LIVEKIT_API_SECRET=$LIVEKIT_API_SECRET

# GPU services via Internal NLB (secure, VPC-only)
# NLB DNS: ${aws_lb.voice_inference_internal.dns_name}
STT_BASE_URL=http://${aws_lb.voice_inference_internal.dns_name}:8081
TTS_BASE_URL=http://${aws_lb.voice_inference_internal.dns_name}:8000
LLM_BASE_URL=http://${aws_lb.voice_inference_internal.dns_name}:8090

# Kyutai TTS streaming via Internal NLB
KYUTAI_TTS_WS_URL=ws://${aws_lb.voice_inference_internal.dns_name}:8089/api/tts_streaming
KYUTAI_TTS_AUTH_TOKEN=ersim_internal
KYUTAI_TTS_AUTH_QUERY_PARAM=auth_id
KYUTAI_TTS_VOICE=vctk/p225_023.wav
KYUTAI_TTS_SAMPLE_RATE=24000
KYUTAI_TTS_NUM_CHANNELS=1

# Django API
ERSIM_API_URL=https://api.ersimulator.com

# Redis for Django event stream (pub/sub)
REDIS_URL=redis://${aws_elasticache_cluster.redis.cache_nodes[0].address}:6379/0
AGENTENV

############################################
# Fetch agent API key from Secrets Manager
############################################
LIVEKIT_AGENT_API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id /ersim/prod/voice/LIVEKIT_AGENT_API_KEY \
  --query SecretString --output text 2>/dev/null || echo "")

if [ -n "$LIVEKIT_AGENT_API_KEY" ]; then
  echo "LIVEKIT_AGENT_API_KEY=$LIVEKIT_AGENT_API_KEY" >> /opt/agent/.env
fi

############################################
# Create Agent systemd service
############################################
cat > /etc/systemd/system/agent.service << 'UNIT'
[Unit]
Description=LiveKit Voice Agent
After=docker.service livekit.service
Requires=docker.service
Wants=livekit.service

[Service]
Type=simple
Restart=always
RestartSec=10
EnvironmentFile=/opt/agent/.env
ExecStartPre=-/usr/bin/docker stop agent
ExecStartPre=-/usr/bin/docker rm agent
ExecStartPre=/usr/bin/docker pull 831646886161.dkr.ecr.us-west-2.amazonaws.com/ersim-voice/livekit-agent:latest
ExecStart=/usr/bin/docker run --rm --name agent \
  --network host \
  --env-file /opt/agent/.env \
  831646886161.dkr.ecr.us-west-2.amazonaws.com/ersim-voice/livekit-agent:latest
ExecStop=/usr/bin/docker stop agent

[Install]
WantedBy=multi-user.target
UNIT

############################################
# Login to ECR for agent image
############################################
aws ecr get-login-password --region us-west-2 | \
  docker login --username AWS --password-stdin 831646886161.dkr.ecr.us-west-2.amazonaws.com

############################################
# Start Services
############################################
systemctl daemon-reload

# Start LiveKit first
systemctl enable livekit
systemctl start livekit

# Start Nginx TLS proxy (depends on LiveKit being up)
sleep 5
systemctl enable nginx
systemctl start nginx

# Wait for LiveKit to be healthy before starting agent
sleep 5
systemctl enable agent
systemctl start agent

############################################
# Log completion
############################################
echo "LiveKit + Nginx + Agent setup complete" | logger -t livekit-setup

EOF

  tags = {
    Name        = "${var.project_name}-${var.environment}-livekit"
    Environment = var.environment
    Service     = "voice-ai"
    Component   = "livekit-server"
  }
}

###############################################
# Elastic IP for LiveKit instance
###############################################
resource "aws_eip" "livekit" {
  domain   = "vpc"
  instance = aws_instance.livekit.id

  tags = {
    Name        = "${var.project_name}-${var.environment}-livekit-eip"
    Environment = var.environment
    Service     = "voice-ai"
  }
}
