variable "aws_region" {
  description = "AWS region to deploy resources into"
  type        = string
  default     = "us-west-2"
}

variable "project_name" {
  description = "Short name used for tagging and naming resources"
  type        = string
  default     = "ersim"
}

variable "environment" {
  description = "Deployment environment (e.g., dev, staging, prod)"
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets"
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "public_subnet_azs" {
  description = "Availability zones for public subnets"
  type        = list(string)
  default     = ["us-west-2a", "us-west-2b"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets"
  type        = list(string)
  default     = ["10.0.11.0/24", "10.0.12.0/24"]
}

variable "private_subnet_azs" {
  description = "Availability zones for private subnets"
  type        = list(string)
  default     = ["us-west-2a", "us-west-2b"]
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "ersim_prod"
}

variable "db_username" {
  description = "PostgreSQL master username"
  type        = string
  default     = "ersim_app"
}

variable "db_password" {
  description = "PostgreSQL master password (set via tfvars or environment)"
  type        = string
  sensitive   = true
}

variable "ec2_instance_type" {
  description = "EC2 instance type for app server"
  type        = string
  default     = "t3.medium"
}

variable "assets_bucket_name" {
  description = "Primary S3 bucket name for core app assets"
  type        = string
}

variable "assets_bucket_name_2" {
  description = "Secondary S3 bucket name (e.g., logs or backups)"
  type        = string
}

variable "public_ssh_key" {
  description = "SSH public key content for ersim-keypair"
  type        = string
}

############################################
# SECRETS VARIABLES (values in terraform.tfvars.local)
# These are used by secrets.tf to provision AWS Secrets Manager
############################################

# --- Backend-Used Secrets (10) ---

variable "secret_django_secret_key" {
  description = "Django secret key"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_database_url" {
  description = "PostgreSQL DATABASE_URL for production"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_redis_url" {
  description = "Redis URL for production"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_openai_api_key" {
  description = "OpenAI API key"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_whisper_api_key" {
  description = "Whisper API key (usually same as OpenAI)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_elevenlabs_api_key" {
  description = "ElevenLabs API key"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_livekit_api_key" {
  description = "LiveKit API key for self-hosted voice stack"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_livekit_api_secret" {
  description = "LiveKit API secret for self-hosted voice stack"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_supabase_anon_key" {
  description = "Supabase anonymous key"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_supabase_jwt_secret" {
  description = "Supabase JWT secret"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_ersim_app_jwt_secret" {
  description = "ERSIM app JWT secret for authbridge"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_ersim_ai_tester_openai_api_key" {
  description = "OpenAI API key for AI tester"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

# --- Future Secrets (15) ---

variable "secret_stripe_secret_key" {
  description = "Stripe secret key (future)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_stripe_webhook_secret" {
  description = "Stripe webhook secret (future)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_revenuecat_api_key" {
  description = "RevenueCat API key (future)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_revenuecat_webhook_secret" {
  description = "RevenueCat webhook secret (future)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_sentry_dsn" {
  description = "Sentry DSN (future)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_sendgrid_api_key" {
  description = "SendGrid API key"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_cloudflare_api_token" {
  description = "Cloudflare API token"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_google_client_secret" {
  description = "Google OAuth client secret"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_google_translate_api_key" {
  description = "Google Translate API key"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_aws_access_key_id" {
  description = "AWS access key ID (for non-IAM services)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_aws_secret_access_key" {
  description = "AWS secret access key (for non-IAM services)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_supabase_service_role_key" {
  description = "Supabase service role key"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_supabase_database_url" {
  description = "Supabase direct database URL"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_openai_realtime_api_key" {
  description = "OpenAI Realtime API key (web-app)"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_realtime_api_key" {
  description = "Legacy realtime API key"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

# --- Infrastructure Secrets (19) ---

variable "secret_supabase_url" {
  description = "Supabase project URL"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_sendgrid_from_email" {
  description = "SendGrid sender email address"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_cloudflare_zone_id" {
  description = "Cloudflare zone ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_cloudflare_account_id" {
  description = "Cloudflare account ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_cloudflare_email" {
  description = "Cloudflare account email"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_google_client_id" {
  description = "Google OAuth client ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_google_translate_project_id" {
  description = "Google Cloud project ID for translation"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_firebase_api_key" {
  description = "Firebase API key"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_firebase_auth_domain" {
  description = "Firebase auth domain"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_firebase_project_id" {
  description = "Firebase project ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_firebase_storage_bucket" {
  description = "Firebase storage bucket"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_firebase_messaging_sender_id" {
  description = "Firebase messaging sender ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_firebase_app_id" {
  description = "Firebase app ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_firebase_measurement_id" {
  description = "Firebase measurement/analytics ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_google_sheet_id" {
  description = "Google Sheet ID for sim-monitor"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_apps_script_id" {
  description = "Google Apps Script project ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_atsr_script_id" {
  description = "ATSR Apps Script ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_apps_script_deployment_id" {
  description = "Apps Script deployment ID"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

variable "secret_apps_script_web_app_url" {
  description = "Apps Script web app URL"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}

# ============================================================================
# TURN CAPTURE API VARIABLES
# ============================================================================

variable "turn_capture_backend_url" {
  description = "Backend URL for turn capture Lambda to forward to (e.g., https://api.ersimulator.com)"
  type        = string
  default     = "https://api.ersimulator.com"
}

variable "turn_capture_internal_api_key" {
  description = "Internal API key for Lambda-to-Backend communication"
  type        = string
  sensitive   = true
  default     = "PLACEHOLDER_NOT_SET"
}
