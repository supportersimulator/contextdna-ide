# Context DNA Installer S3 Bucket
# This bucket hosts the cross-platform installers (DMG, EXE, AppImage)

# S3 Bucket for installers
resource "aws_s3_bucket" "installer" {
  bucket = "contextdna-installer"

  tags = {
    Name        = "Context DNA Installer"
    Environment = "production"
    Purpose     = "Electron app distribution"
  }
}

# Bucket versioning (keeps old versions for rollback)
resource "aws_s3_bucket_versioning" "installer" {
  bucket = aws_s3_bucket.installer.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Static website hosting
resource "aws_s3_bucket_website_configuration" "installer" {
  bucket = aws_s3_bucket.installer.id

  index_document {
    suffix = "install.html"
  }

  error_document {
    key = "install.html"
  }
}

# Public access settings
resource "aws_s3_bucket_public_access_block" "installer" {
  bucket = aws_s3_bucket.installer.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

# Bucket policy for public read access
resource "aws_s3_bucket_policy" "installer" {
  bucket = aws_s3_bucket.installer.id

  depends_on = [aws_s3_bucket_public_access_block.installer]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadGetObject"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.installer.arn}/*"
      }
    ]
  })
}

# CORS configuration for download from any origin
resource "aws_s3_bucket_cors_configuration" "installer" {
  bucket = aws_s3_bucket.installer.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    expose_headers  = ["ETag", "Content-Length", "Content-Type"]
    max_age_seconds = 3600
  }
}

# IAM user for GitHub Actions deployment
resource "aws_iam_user" "github_actions_installer" {
  name = "github-actions-installer"
  path = "/service-accounts/"

  tags = {
    Purpose = "GitHub Actions CI/CD for installer deployment"
  }
}

# IAM policy for S3 access
resource "aws_iam_user_policy" "github_actions_installer" {
  name = "installer-s3-access"
  user = aws_iam_user.github_actions_installer.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = [
          aws_s3_bucket.installer.arn,
          "${aws_s3_bucket.installer.arn}/*"
        ]
      }
    ]
  })
}

# Access key for GitHub Actions (store in GitHub Secrets)
resource "aws_iam_access_key" "github_actions_installer" {
  user = aws_iam_user.github_actions_installer.name
}

# CloudFront Distribution (optional - for custom domain + HTTPS)
resource "aws_cloudfront_distribution" "installer" {
  count = var.enable_cloudfront ? 1 : 0

  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "install.html"
  comment             = "Context DNA Installer CDN"
  price_class         = "PriceClass_100" # North America + Europe

  aliases = var.installer_domain != "" ? [var.installer_domain] : []

  origin {
    domain_name = aws_s3_bucket_website_configuration.installer.website_endpoint
    origin_id   = "S3-installer"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD", "OPTIONS"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "S3-installer"

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    viewer_protocol_policy = "redirect-to-https"
    min_ttl                = 0
    default_ttl            = 3600
    max_ttl                = 86400
    compress               = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = var.installer_domain == "" ? true : false
    acm_certificate_arn           = var.installer_domain != "" ? var.acm_certificate_arn : null
    ssl_support_method            = var.installer_domain != "" ? "sni-only" : null
    minimum_protocol_version      = "TLSv1.2_2021"
  }

  tags = {
    Name        = "Context DNA Installer CDN"
    Environment = "production"
  }
}

# Variables
variable "enable_cloudfront" {
  description = "Enable CloudFront CDN for installer distribution"
  type        = bool
  default     = false
}

variable "installer_domain" {
  description = "Custom domain for installer (e.g., install.contextdna.io)"
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "ACM certificate ARN for custom domain (must be in us-east-1)"
  type        = string
  default     = ""
}

# Outputs
output "installer_bucket_name" {
  description = "S3 bucket name for installers"
  value       = aws_s3_bucket.installer.id
}

output "installer_bucket_website_url" {
  description = "S3 static website URL"
  value       = "http://${aws_s3_bucket_website_configuration.installer.website_endpoint}"
}

output "installer_bucket_s3_url" {
  description = "S3 URL for direct access"
  value       = "https://${aws_s3_bucket.installer.bucket_regional_domain_name}"
}

output "cloudfront_distribution_domain" {
  description = "CloudFront distribution domain name"
  value       = var.enable_cloudfront ? aws_cloudfront_distribution.installer[0].domain_name : null
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for cache invalidation)"
  value       = var.enable_cloudfront ? aws_cloudfront_distribution.installer[0].id : null
}

output "github_actions_access_key_id" {
  description = "Access key ID for GitHub Actions (add to GitHub Secrets)"
  value       = aws_iam_access_key.github_actions_installer.id
  sensitive   = true
}

output "github_actions_secret_access_key" {
  description = "Secret access key for GitHub Actions (add to GitHub Secrets)"
  value       = aws_iam_access_key.github_actions_installer.secret
  sensitive   = true
}
