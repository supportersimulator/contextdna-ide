output "rds_endpoint" {
  description = "RDS endpoint hostname"
  value       = aws_db_instance.postgres.address
}

output "rds_database_url" {
  description = "PostgreSQL connection URL (fill in password manually if desired)"
  value       = "postgres://${var.db_username}:<DB_PASSWORD>@${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}/${var.db_name}"
}

output "redis_endpoint" {
  description = "ElastiCache Redis primary endpoint"
  value       = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "redis_url" {
  description = "Redis URL for application configuration"
  value       = "redis://${aws_elasticache_cluster.redis.cache_nodes[0].address}:6379/0"
}

output "assets_bucket_name" {
  description = "Primary S3 bucket name for core assets"
  value       = aws_s3_bucket.assets.bucket
}

output "assets_bucket_name_2" {
  description = "Secondary S3 bucket name"
  value       = aws_s3_bucket.assets_2.bucket
}

output "assets_bucket_region" {
  description = "Region of the assets S3 bucket"
  value       = var.aws_region
}

output "alb_dns_name" {
  description = "Application Load Balancer DNS name"
  value       = aws_lb.app.dns_name
}

output "ec2_public_ip" {
  description = "Public IP address of the app EC2 instance (via Elastic IP)"
  value       = aws_eip.app.public_ip
}

output "ssh_command" {
  description = "SSH command to connect to the EC2 instance"
  value       = "ssh -i ~/.ssh/id_rsa ec2-user@${aws_eip.app.public_ip}"
}

output "ec2_instance_role_arn" {
  description = "IAM role ARN for EC2 app instances"
  value       = aws_iam_role.ec2_instance_role.arn
}

output "deploy_role_arn" {
  description = "IAM role ARN for CI/CD deployments"
  value       = aws_iam_role.deploy.arn
}

output "livekit_public_ip" {
  description = "Public IP address of the LiveKit EC2 instance (via Elastic IP)"
  value       = aws_eip.livekit.public_ip
}

output "livekit_ssh_command" {
  description = "SSH command to connect to the LiveKit instance"
  value       = "ssh -i ~/.ssh/id_rsa ec2-user@${aws_eip.livekit.public_ip}"
}

output "livekit_url" {
  description = "LiveKit WebSocket URL for clients"
  value       = "wss://livekit.ersimulator.com"
}
