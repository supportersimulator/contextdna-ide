# Outputs

output "livekit_public_ip" {
  description = "Public IP of the LiveKit instance"
  value       = aws_eip.livekit.public_ip
}

output "livekit_url" {
  description = "LiveKit WebSocket URL"
  value       = "wss://livekit.ersimulator.com"
}

output "livekit_ssh_command" {
  description = "SSH command to connect to LiveKit instance"
  value       = "ssh -i ~/.ssh/ersim-keypair ec2-user@${aws_eip.livekit.public_ip}"
}

output "gpu_private_ip" {
  description = "GPU instance private IP for voice inference services"
  value       = data.aws_instances.gpu_voice.private_ips[0]
}

output "voice_stt_url" {
  description = "STT service URL (direct to GPU)"
  value       = "http://${data.aws_instances.gpu_voice.private_ips[0]}:8081"
}

output "voice_tts_http_url" {
  description = "TTS HTTP service URL (direct to GPU)"
  value       = "http://${data.aws_instances.gpu_voice.private_ips[0]}:8000"
}

output "voice_tts_ws_url" {
  description = "TTS WebSocket service URL (direct to GPU)"
  value       = "ws://${data.aws_instances.gpu_voice.private_ips[0]}:8089"
}

output "voice_llm_url" {
  description = "LLM service URL (direct to GPU)"
  value       = "http://${data.aws_instances.gpu_voice.private_ips[0]}:8090"
}

output "agent_env_vars" {
  description = "Environment variables for the Voice Agent"
  value = <<-EOF
    # Voice Agent Environment Variables
    LIVEKIT_URL=ws://localhost:7880
    LIVEKIT_API_KEY=<from-tfvars>
    LIVEKIT_API_SECRET=<from-tfvars>
    STT_BASE_URL=http://${data.aws_instances.gpu_voice.private_ips[0]}:8081
    TTS_BASE_URL=http://${data.aws_instances.gpu_voice.private_ips[0]}:8000
    LLM_BASE_URL=http://${data.aws_instances.gpu_voice.private_ips[0]}:8090
    KYUTAI_TTS_WS_URL=ws://${data.aws_instances.gpu_voice.private_ips[0]}:8089/api/tts_streaming
  EOF
}
