###############################################
# RDS SUBNET GROUP (must span 2 AZs)
###############################################
resource "aws_db_subnet_group" "db" {
  name       = "${var.project_name}-db-subnets"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name        = "${var.project_name}-db-subnets"
    Environment = var.environment
  }
}

###############################################
# RDS POSTGRES INSTANCE
###############################################
resource "aws_db_instance" "postgres" {
  identifier              = "${var.project_name}-${var.environment}-db"
  allocated_storage       = 20
  max_allocated_storage   = 30
  storage_type            = "gp3"
  engine                  = "postgres"
  engine_version          = "15.12"
  instance_class          = "db.t3.micro"
  db_name                 = var.db_name
  username                = var.db_username
  password                = var.db_password
  port                    = 5432
  db_subnet_group_name    = aws_db_subnet_group.db.name
  vpc_security_group_ids  = [aws_security_group.sg_db.id]
  multi_az                = false
  publicly_accessible     = false
  backup_retention_period = 7
  skip_final_snapshot     = true
  storage_encrypted       = true

  tags = {
    Name        = "${var.project_name}-${var.environment}-db"
    Environment = var.environment
  }
}