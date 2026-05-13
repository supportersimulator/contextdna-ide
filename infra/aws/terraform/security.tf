###############################################
# ALB SECURITY GROUP — allow HTTP from Internet
###############################################
resource "aws_security_group" "sg_alb" {
  name        = "${var.project_name}-${var.environment}-alb-sg"
  description = "ALB ingress from internet"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-alb-sg"
    Environment = var.environment
  }
}

#######################################################
# EC2 / APP SECURITY GROUP — SSH from world, HTTP from ALB
#######################################################
resource "aws_security_group" "sg_app" {
  name        = "${var.project_name}-${var.environment}-ec2-sg"
  description = "EC2/app instances behind ALB"
  vpc_id      = aws_vpc.main.id

  # SSH for admin access (can be tightened later)
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # HTTP from ALB only
  ingress {
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.sg_alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-ec2-sg"
    Environment = var.environment
  }
}

#######################################################
# DATABASE SECURITY GROUP — allow Postgres from EC2 SG
#######################################################
resource "aws_security_group" "sg_db" {
  name        = "${var.project_name}-${var.environment}-db-sg"
  description = "RDS access from app instances"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.sg_app.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-db-sg"
    Environment = var.environment
  }
}

#######################################################
# REDIS SECURITY GROUP — allow Redis port from EC2 SG
#######################################################
resource "aws_security_group" "sg_redis" {
  name        = "${var.project_name}-${var.environment}-redis-sg"
  description = "Redis access from app instances"
  vpc_id      = aws_vpc.main.id

  # Django app instances
  ingress {
    description     = "Redis from Django app"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.sg_app.id]
  }

  # LiveKit agent (for Django event stream pub/sub)
  ingress {
    description     = "Redis from LiveKit agent"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.sg_livekit.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-redis-sg"
    Environment = var.environment
  }
}

#######################################################
# LIVEKIT SECURITY GROUP — WebRTC media server
# Dedicated CPU instance for LiveKit + Agent
#######################################################
resource "aws_security_group" "sg_livekit" {
  name        = "${var.project_name}-${var.environment}-livekit-sg"
  description = "LiveKit WebRTC server - requires UDP for media"
  vpc_id      = aws_vpc.main.id

  # SSH for admin access
  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # LiveKit HTTP/WebSocket (signaling)
  ingress {
    description = "LiveKit WebSocket/HTTP"
    from_port   = 7880
    to_port     = 7880
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # LiveKit HTTPS/WSS (TLS signaling - browsers require this)
  ingress {
    description = "LiveKit WSS/HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # LiveKit TCP fallback (WebRTC over TCP)
  ingress {
    description = "LiveKit TCP fallback"
    from_port   = 7881
    to_port     = 7881
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # LiveKit WebRTC UDP media (RTP/RTCP)
  ingress {
    description = "LiveKit WebRTC UDP"
    from_port   = 50000
    to_port     = 60000
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # TURN UDP (for hospital networks behind strict NAT)
  ingress {
    description = "TURN UDP"
    from_port   = 3478
    to_port     = 3478
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # TURN TLS (for hospital networks blocking UDP)
  ingress {
    description = "TURN TLS"
    from_port   = 5349
    to_port     = 5349
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # All outbound (agent needs to reach GPU services)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-livekit-sg"
    Environment = var.environment
    Service     = "voice-ai"
    Component   = "livekit-server"
  }
}

#######################################################
# VOICE INFERENCE SECURITY GROUP
# For GPU instance running STT/TTS/LLM services
# Accessed via Internal NLB by LiveKit agent
#
# NOTE: This security group should be attached to the
# GPU ASG/ECS instances running voice inference services.
# The internal NLB routes VPC traffic to these ports.
#######################################################
resource "aws_security_group" "sg_voice_inference" {
  name        = "${var.project_name}-${var.environment}-voice-inference-sg"
  description = "GPU inference services (STT, TTS, LLM) - accessed via internal NLB"
  vpc_id      = aws_vpc.main.id

  # SSH for admin access
  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # STT (Faster-Whisper) - Port 8081
  # Allow from VPC CIDR (NLB is transparent, uses VPC IPs)
  ingress {
    description = "STT Whisper from VPC"
    from_port   = 8081
    to_port     = 8081
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # TTS HTTP (Kyutai) - Port 8000
  ingress {
    description = "TTS HTTP from VPC"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # TTS WebSocket (Kyutai) - Port 8089
  ingress {
    description = "TTS WebSocket from VPC"
    from_port   = 8089
    to_port     = 8089
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # LLM Proxy - Port 8090
  ingress {
    description = "LLM Proxy from VPC"
    from_port   = 8090
    to_port     = 8090
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # All outbound
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-voice-inference-sg"
    Environment = var.environment
    Service     = "voice-ai"
    Component   = "gpu-inference"
  }
}