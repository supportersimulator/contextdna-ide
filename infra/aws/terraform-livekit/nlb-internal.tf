# Reference existing Internal NLB for GPU Voice Inference Services
# The NLB already exists: ersim-prod-voice-nlb

data "aws_lb" "voice_inference_internal" {
  name = "ersim-prod-voice-nlb"
}

# Note: Target groups exist in wrong VPC (vpc-0fd42a9b95ba38647)
# but GPU is in vpc-01e46e3b26b272a7e.
# For now, we'll use the GPU's private IP directly instead of NLB.
# This can be updated later to fix the target group VPC issue.
