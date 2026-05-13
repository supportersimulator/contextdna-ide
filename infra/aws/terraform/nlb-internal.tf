# ==============================================================================
# INTERNAL NLB FOR GPU VOICE INFERENCE SERVICES
# ==============================================================================
# Provides secure, VPC-only access to GPU inference services (STT, TTS, LLM)
# for the LiveKit agent running on the dedicated CPU instance.
#
# Benefits over direct public IP:
# - Security: No internet exposure for internal services
# - Reliability: Multi-AZ with health checks
# - Scalability: Auto target registration
# - DNS: Stable DNS name vs hardcoded IP
#
# Services:
# - STT (Faster-Whisper): Port 8081
# - TTS HTTP (Kyutai): Port 8000
# - TTS WebSocket (Kyutai): Port 8089
# - LLM Proxy: Port 8090
# ==============================================================================

# ==============================================================================
# DATA SOURCE: Look up GPU ASG instances for target registration
# ==============================================================================
# The GPU instance runs in an ASG named "ersim-voice-gpu-asg"
# We need its private IP for NLB target registration

data "aws_instances" "gpu_voice" {
  filter {
    name   = "tag:aws:autoscaling:groupName"
    values = ["ersim-voice-gpu-asg"]
  }

  filter {
    name   = "instance-state-name"
    values = ["running"]
  }
}

# ==============================================================================
# INTERNAL NLB
# ==============================================================================

resource "aws_lb" "voice_inference_internal" {
  name               = "${var.project_name}-${var.environment}-voice-nlb"
  internal           = true  # CRITICAL: VPC-only access
  load_balancer_type = "network"
  subnets            = aws_subnet.private[*].id

  enable_cross_zone_load_balancing = true

  tags = {
    Name        = "${var.project_name}-${var.environment}-voice-inference-nlb"
    Service     = "voice-ai"
    Environment = var.environment
    Component   = "internal-nlb"
  }
}

# ==============================================================================
# TARGET GROUPS
# ==============================================================================

# STT (Faster-Whisper) - Port 8081
resource "aws_lb_target_group" "voice_stt" {
  name        = "${var.project_name}-${var.environment}-stt-tg"
  port        = 8081
  protocol    = "TCP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    protocol            = "HTTP"
    path                = "/health"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 30
    timeout             = 10
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-stt-tg"
    Service     = "voice-ai"
    Component   = "stt-whisper"
    Environment = var.environment
  }
}

# TTS HTTP (Kyutai) - Port 8000
resource "aws_lb_target_group" "voice_tts_http" {
  name        = "${var.project_name}-${var.environment}-tts-http-tg"
  port        = 8000
  protocol    = "TCP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    protocol            = "HTTP"
    path                = "/health"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 30
    timeout             = 10
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-tts-http-tg"
    Service     = "voice-ai"
    Component   = "tts-kyutai-http"
    Environment = var.environment
  }
}

# TTS WebSocket (Kyutai) - Port 8089
resource "aws_lb_target_group" "voice_tts_ws" {
  name        = "${var.project_name}-${var.environment}-tts-ws-tg"
  port        = 8089
  protocol    = "TCP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  # WebSocket doesn't support HTTP health checks, use TCP
  health_check {
    enabled             = true
    protocol            = "TCP"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 30
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-tts-ws-tg"
    Service     = "voice-ai"
    Component   = "tts-kyutai-ws"
    Environment = var.environment
  }
}

# LLM Proxy - Port 8090
resource "aws_lb_target_group" "voice_llm" {
  name        = "${var.project_name}-${var.environment}-llm-tg"
  port        = 8090
  protocol    = "TCP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    protocol            = "HTTP"
    path                = "/health"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 30
    timeout             = 10
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-llm-tg"
    Service     = "voice-ai"
    Component   = "llm-proxy"
    Environment = var.environment
  }
}

# ==============================================================================
# TARGET GROUP ATTACHMENTS
# ==============================================================================
# Register GPU instance(s) to target groups
# Uses dynamic lookup from ASG instances

resource "aws_lb_target_group_attachment" "voice_stt" {
  count            = length(data.aws_instances.gpu_voice.private_ips)
  target_group_arn = aws_lb_target_group.voice_stt.arn
  target_id        = data.aws_instances.gpu_voice.private_ips[count.index]
  port             = 8081
}

resource "aws_lb_target_group_attachment" "voice_tts_http" {
  count            = length(data.aws_instances.gpu_voice.private_ips)
  target_group_arn = aws_lb_target_group.voice_tts_http.arn
  target_id        = data.aws_instances.gpu_voice.private_ips[count.index]
  port             = 8000
}

resource "aws_lb_target_group_attachment" "voice_tts_ws" {
  count            = length(data.aws_instances.gpu_voice.private_ips)
  target_group_arn = aws_lb_target_group.voice_tts_ws.arn
  target_id        = data.aws_instances.gpu_voice.private_ips[count.index]
  port             = 8089
}

resource "aws_lb_target_group_attachment" "voice_llm" {
  count            = length(data.aws_instances.gpu_voice.private_ips)
  target_group_arn = aws_lb_target_group.voice_llm.arn
  target_id        = data.aws_instances.gpu_voice.private_ips[count.index]
  port             = 8090
}

# ==============================================================================
# LISTENERS
# ==============================================================================

# STT Listener - Port 8081
resource "aws_lb_listener" "voice_stt" {
  load_balancer_arn = aws_lb.voice_inference_internal.arn
  port              = 8081
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.voice_stt.arn
  }

  tags = {
    Name    = "${var.project_name}-${var.environment}-stt-listener"
    Service = "voice-ai"
  }
}

# TTS HTTP Listener - Port 8000
resource "aws_lb_listener" "voice_tts_http" {
  load_balancer_arn = aws_lb.voice_inference_internal.arn
  port              = 8000
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.voice_tts_http.arn
  }

  tags = {
    Name    = "${var.project_name}-${var.environment}-tts-http-listener"
    Service = "voice-ai"
  }
}

# TTS WebSocket Listener - Port 8089
resource "aws_lb_listener" "voice_tts_ws" {
  load_balancer_arn = aws_lb.voice_inference_internal.arn
  port              = 8089
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.voice_tts_ws.arn
  }

  tags = {
    Name    = "${var.project_name}-${var.environment}-tts-ws-listener"
    Service = "voice-ai"
  }
}

# LLM Listener - Port 8090
resource "aws_lb_listener" "voice_llm" {
  load_balancer_arn = aws_lb.voice_inference_internal.arn
  port              = 8090
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.voice_llm.arn
  }

  tags = {
    Name    = "${var.project_name}-${var.environment}-llm-listener"
    Service = "voice-ai"
  }
}

# ==============================================================================
# OUTPUTS
# ==============================================================================

output "voice_inference_nlb_dns" {
  description = "Internal NLB DNS name for GPU voice inference services"
  value       = aws_lb.voice_inference_internal.dns_name
}

output "voice_inference_nlb_arn" {
  description = "Internal NLB ARN"
  value       = aws_lb.voice_inference_internal.arn
}

# Convenience outputs for agent configuration
output "voice_stt_url" {
  description = "STT service URL via internal NLB"
  value       = "http://${aws_lb.voice_inference_internal.dns_name}:8081"
}

output "voice_tts_http_url" {
  description = "TTS HTTP service URL via internal NLB"
  value       = "http://${aws_lb.voice_inference_internal.dns_name}:8000"
}

output "voice_tts_ws_url" {
  description = "TTS WebSocket URL via internal NLB"
  value       = "ws://${aws_lb.voice_inference_internal.dns_name}:8089/api/tts_streaming"
}

output "voice_llm_url" {
  description = "LLM Proxy URL via internal NLB"
  value       = "http://${aws_lb.voice_inference_internal.dns_name}:8090"
}
