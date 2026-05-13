terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"  # Pinned to stable 5.x - v6.x has startup timeout issues
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"  # Upgraded to v5 - uses cloudflare_dns_record resource
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

provider "aws" {
  region = var.aws_region
}
