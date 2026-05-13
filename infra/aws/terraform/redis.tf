###############################################
# ELASTICACHE REDIS SUBNET GROUP
###############################################
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.project_name}-redis-subnets"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = "${var.project_name}-redis-subnets"
  }
}

###############################################
# ELASTICACHE REDIS CLUSTER
###############################################
resource "aws_elasticache_cluster" "redis" {
  cluster_id      = "${var.project_name}-prod-redis"
  engine          = "redis"
  engine_version  = "7.1"
  node_type       = "cache.t3.micro"
  num_cache_nodes = 1

  subnet_group_name  = aws_elasticache_subnet_group.redis.name

  # IMPORTANT — reference imported SG from security.tf
  security_group_ids = [aws_security_group.sg_redis.id]

  tags = {
    Name        = "${var.project_name}-prod-redis"
    Environment = "production"
  }
}