# LiveKit CPU Instance + Internal NLB for Voice Inference
# This is a targeted config that references existing infrastructure via data sources

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "5.70.0"  # Pinned to specific older version for stability
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Fetch Cloudflare credentials from AWS Secrets Manager
data "aws_secretsmanager_secret_version" "cloudflare_api_token" {
  secret_id = "/ersim/prod/future/CLOUDFLARE_API_TOKEN"
}

data "aws_secretsmanager_secret_version" "cloudflare_zone_id" {
  secret_id = "/ersim/prod/infra/CLOUDFLARE_ZONE_ID"
}

provider "cloudflare" {
  api_token = data.aws_secretsmanager_secret_version.cloudflare_api_token.secret_string
}

# Variables
variable "aws_region" {
  default = "us-west-2"
}

variable "environment" {
  default = "prod"
}

variable "project_name" {
  default = "ersim"
}

variable "livekit_api_key" {
  description = "LiveKit API key"
  sensitive   = true
}

variable "livekit_api_secret" {
  description = "LiveKit API secret"
  sensitive   = true
}

# Local for Cloudflare zone ID from Secrets Manager
locals {
  cloudflare_zone_id = data.aws_secretsmanager_secret_version.cloudflare_zone_id.secret_string
}

# Data sources - reference existing infrastructure
# Using VPC ID directly since there are duplicate VPC names
data "aws_vpc" "main" {
  id = "vpc-01e46e3b26b272a7e"  # The VPC with the GPU instance
}

data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.main.id]
  }
  filter {
    name   = "tag:Name"
    values = ["${var.project_name}-${var.environment}-public-*"]
  }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.main.id]
  }
  filter {
    name   = "tag:Name"
    values = ["${var.project_name}-${var.environment}-private-*"]
  }
}

data "aws_instances" "gpu_voice" {
  filter {
    name   = "tag:Name"
    values = ["ersim-voice-gpu"]  # Actual name of GPU instance
  }
  filter {
    name   = "instance-state-name"
    values = ["running"]
  }
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

data "aws_caller_identity" "current" {}
