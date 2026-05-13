###############################################
# PRIMARY ASSETS BUCKET
###############################################
resource "aws_s3_bucket" "assets" {
  bucket = var.assets_bucket_name

  tags = {
    Name        = "${var.project_name}-${var.environment}-assets"
    Environment = var.environment
  }
}

resource "aws_s3_bucket_versioning" "assets" {
  bucket = aws_s3_bucket.assets.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket                  = aws_s3_bucket.assets.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

###############################################
# SECONDARY BUCKET (e.g., logs/backups)
###############################################
resource "aws_s3_bucket" "assets_2" {
  bucket = var.assets_bucket_name_2

  tags = {
    Name        = "${var.project_name}-${var.environment}-assets-2"
    Environment = var.environment
  }
}

resource "aws_s3_bucket_versioning" "assets_2" {
  bucket = aws_s3_bucket.assets_2.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "assets_2" {
  bucket                  = aws_s3_bucket.assets_2.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "assets_2" {
  bucket = aws_s3_bucket.assets_2.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
