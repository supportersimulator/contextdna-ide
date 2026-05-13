# LiveKit CPU Instance - Dedicated c6i.xlarge for LiveKit Server + Voice Agent
# Upgraded to 4 vCPU, 8GB RAM per LiveKit agent recommendations

# Security Group for LiveKit
resource "aws_security_group" "livekit" {
  name        = "${var.project_name}-${var.environment}-livekit-sg"
  description = "LiveKit WebRTC server security group"
  vpc_id      = data.aws_vpc.main.id

  # SSH
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "SSH access"
  }

  # LiveKit WebSocket (client connections)
  ingress {
    from_port   = 7880
    to_port     = 7880
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "LiveKit WebSocket"
  }

  # LiveKit RTC over TCP (fallback)
  ingress {
    from_port   = 7881
    to_port     = 7881
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "LiveKit RTC TCP"
  }

  # WebRTC UDP ports
  ingress {
    from_port   = 50000
    to_port     = 60000
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "WebRTC UDP media"
  }

  # Allow all outbound
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound traffic"
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-livekit-sg"
    Environment = var.environment
    Project     = var.project_name
  }
}

# Use existing SSH key pair
data "aws_key_pair" "existing" {
  key_name = "ersim-keypair"
}

# IAM Role for LiveKit instance
resource "aws_iam_role" "livekit" {
  name = "${var.project_name}-${var.environment}-livekit-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
  })

  tags = {
    Name        = "${var.project_name}-${var.environment}-livekit-role"
    Environment = var.environment
    Project     = var.project_name
  }
}

resource "aws_iam_role_policy" "livekit_ecr" {
  name = "ecr-access"
  role = aws_iam_role.livekit.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:CreateLogGroup"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:/ersim/*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "livekit" {
  name = "${var.project_name}-${var.environment}-livekit-profile"
  role = aws_iam_role.livekit.name
}

# LiveKit EC2 Instance
resource "aws_instance" "livekit" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = "c6i.xlarge"  # 4 vCPU, 8GB RAM - recommended for LiveKit + Agent
  key_name               = data.aws_key_pair.existing.key_name
  vpc_security_group_ids = [aws_security_group.livekit.id]
  subnet_id              = tolist(data.aws_subnets.public.ids)[0]
  iam_instance_profile   = aws_iam_instance_profile.livekit.name

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = base64encode(<<-EOF
    #!/bin/bash
    set -ex

    # Update system
    dnf update -y
    dnf install -y docker git

    # Start Docker
    systemctl enable docker
    systemctl start docker
    usermod -aG docker ec2-user

    # Create directories
    mkdir -p /opt/livekit /opt/agent

    # Write LiveKit config
    cat > /opt/livekit/livekit.yaml << 'LKCONFIG'
    port: 7880
    rtc:
      port_range_start: 50000
      port_range_end: 60000
      tcp_port: 7881
      use_external_ip: true
    redis:
      address: ""
    keys:
      ${var.livekit_api_key}: ${var.livekit_api_secret}
    logging:
      level: info
    LKCONFIG

    # Write agent environment
    # Using GPU private IP directly (NLB target groups in wrong VPC)
    GPU_IP="${data.aws_instances.gpu_voice.private_ips[0]}"

    # Get Redis endpoint from ElastiCache (for Django event stream)
    REDIS_HOST=$(aws elasticache describe-cache-clusters \
      --cache-cluster-id ersim-prod-redis \
      --show-cache-node-info \
      --region ${var.aws_region} \
      --query 'CacheClusters[0].CacheNodes[0].Endpoint.Address' \
      --output text 2>/dev/null || echo "localhost")

    cat > /opt/agent/.env << AGENTENV
    LIVEKIT_URL=ws://localhost:7880
    LIVEKIT_API_KEY=${var.livekit_api_key}
    LIVEKIT_API_SECRET=${var.livekit_api_secret}
    STT_BASE_URL=http://$GPU_IP:8081
    TTS_BASE_URL=http://$GPU_IP:8000
    LLM_BASE_URL=http://$GPU_IP:8090
    KYUTAI_TTS_WS_URL=ws://$GPU_IP:8089/api/tts_streaming
    ERSIM_API_URL=https://api.ersimulator.com
    REDIS_URL=redis://$REDIS_HOST:6379/0
    AGENTENV

    # Create docker-compose for LiveKit + Agent (both on same instance)
    cat > /opt/livekit/docker-compose.yml << 'DCOMPOSE'
    version: '3.8'
    services:
      livekit:
        image: livekit/livekit-server:latest
        container_name: livekit-server
        restart: unless-stopped
        network_mode: host
        volumes:
          - ./livekit.yaml:/livekit.yaml
        command: --config /livekit.yaml

      agent:
        image: ${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/ersim-voice/livekit-agent:latest
        container_name: voice-agent
        restart: unless-stopped
        network_mode: host
        env_file:
          - /opt/agent/.env
        depends_on:
          - livekit
    DCOMPOSE

    # Login to ECR and pull agent image
    aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com

    # Start LiveKit + Agent
    cd /opt/livekit && docker compose up -d

    echo "LiveKit + Agent setup complete"
  EOF
  )

  tags = {
    Name        = "${var.project_name}-${var.environment}-livekit"
    Environment = var.environment
    Project     = var.project_name
    Service     = "livekit"
  }

  lifecycle {
    ignore_changes = [ami]
  }
}

# Elastic IP for LiveKit (WebRTC needs stable public IP)
resource "aws_eip" "livekit" {
  instance = aws_instance.livekit.id
  domain   = "vpc"

  tags = {
    Name        = "${var.project_name}-${var.environment}-livekit-eip"
    Environment = var.environment
    Project     = var.project_name
  }
}

# Cloudflare DNS for LiveKit (NOT proxied - WebRTC needs direct connection)
resource "cloudflare_dns_record" "livekit_server" {
  zone_id = local.cloudflare_zone_id
  name    = "livekit"
  content = aws_eip.livekit.public_ip
  type    = "A"
  ttl     = 300
  proxied = false  # CRITICAL: WebRTC requires direct connection
  comment = "LiveKit WebRTC server (dedicated CPU instance)"
}
