############################################
# AWS SECRETS MANAGER - ERSIM SECRETS
############################################
# This file provisions secrets in AWS Secrets Manager.
# Actual secret values come from terraform.tfvars.local (git-ignored).
#
# NO-OP GUARANTEE:
#   - Does NOT modify EC2, ALB, or any existing resources
#   - Does NOT introduce ECS
#   - Only creates Secrets Manager entries
#
# NAMING CONVENTION:
#   /ersim/{environment}/backend/{SECRET_NAME}  - Backend-used secrets
#   /ersim/{environment}/future/{SECRET_NAME}   - Future secrets
############################################

locals {
  secrets_prefix       = "/${var.project_name}/${var.environment}"
  backend_secrets_path = "${local.secrets_prefix}/backend"
  future_secrets_path  = "${local.secrets_prefix}/future"
  infra_secrets_path   = "${local.secrets_prefix}/infra"
}

############################################
# BACKEND-USED SECRETS (Required for ECS)
# Total: 10 secrets
############################################

resource "aws_secretsmanager_secret" "django_secret_key" {
  name        = "${local.backend_secrets_path}/DJANGO_SECRET_KEY"
  description = "Django secret key for CSRF, sessions, etc."

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "django_secret_key" {
  secret_id     = aws_secretsmanager_secret.django_secret_key.id
  secret_string = var.secret_django_secret_key
}

resource "aws_secretsmanager_secret" "database_url" {
  name        = "${local.backend_secrets_path}/DATABASE_URL"
  description = "PostgreSQL connection string (RDS)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = var.secret_database_url
}

resource "aws_secretsmanager_secret" "redis_url" {
  name        = "${local.backend_secrets_path}/REDIS_URL"
  description = "Redis connection string (ElastiCache)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id     = aws_secretsmanager_secret.redis_url.id
  secret_string = var.secret_redis_url
}

resource "aws_secretsmanager_secret" "openai_api_key" {
  name        = "${local.backend_secrets_path}/OPENAI_API_KEY"
  description = "OpenAI API key for AI completions"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "openai_api_key" {
  secret_id     = aws_secretsmanager_secret.openai_api_key.id
  secret_string = var.secret_openai_api_key
}

resource "aws_secretsmanager_secret" "whisper_api_key" {
  name        = "${local.backend_secrets_path}/WHISPER_API_KEY"
  description = "Whisper API key for speech-to-text"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "whisper_api_key" {
  secret_id     = aws_secretsmanager_secret.whisper_api_key.id
  secret_string = var.secret_whisper_api_key
}

resource "aws_secretsmanager_secret" "elevenlabs_api_key" {
  name        = "${local.backend_secrets_path}/ELEVENLABS_API_KEY"
  description = "ElevenLabs API key for text-to-speech"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "elevenlabs_api_key" {
  secret_id     = aws_secretsmanager_secret.elevenlabs_api_key.id
  secret_string = var.secret_elevenlabs_api_key
}

# LiveKit credentials for self-hosted voice stack
resource "aws_secretsmanager_secret" "livekit_api_key" {
  name        = "${local.backend_secrets_path}/LIVEKIT_API_KEY"
  description = "LiveKit API key for self-hosted voice stack"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "livekit_api_key" {
  secret_id     = aws_secretsmanager_secret.livekit_api_key.id
  secret_string = var.secret_livekit_api_key
}

resource "aws_secretsmanager_secret" "livekit_api_secret" {
  name        = "${local.backend_secrets_path}/LIVEKIT_API_SECRET"
  description = "LiveKit API secret for self-hosted voice stack"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "livekit_api_secret" {
  secret_id     = aws_secretsmanager_secret.livekit_api_secret.id
  secret_string = var.secret_livekit_api_secret
}

resource "aws_secretsmanager_secret" "supabase_anon_key" {
  name        = "${local.backend_secrets_path}/SUPABASE_ANON_KEY"
  description = "Supabase anonymous/public key"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "supabase_anon_key" {
  secret_id     = aws_secretsmanager_secret.supabase_anon_key.id
  secret_string = var.secret_supabase_anon_key
}

resource "aws_secretsmanager_secret" "supabase_jwt_secret" {
  name        = "${local.backend_secrets_path}/SUPABASE_JWT_SECRET"
  description = "Supabase JWT secret for token validation"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "supabase_jwt_secret" {
  secret_id     = aws_secretsmanager_secret.supabase_jwt_secret.id
  secret_string = var.secret_supabase_jwt_secret
}

resource "aws_secretsmanager_secret" "ersim_app_jwt_secret" {
  name        = "${local.backend_secrets_path}/ERSIM_APP_JWT_SECRET"
  description = "ERSIM app JWT secret for authbridge token exchange"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "ersim_app_jwt_secret" {
  secret_id     = aws_secretsmanager_secret.ersim_app_jwt_secret.id
  secret_string = var.secret_ersim_app_jwt_secret
}

resource "aws_secretsmanager_secret" "ersim_ai_tester_openai_api_key" {
  name        = "${local.backend_secrets_path}/ERSIM_AI_TESTER_OPENAI_API_KEY"
  description = "OpenAI API key for AI tester harness (dev-only feature)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "backend"
  }
}

resource "aws_secretsmanager_secret_version" "ersim_ai_tester_openai_api_key" {
  secret_id     = aws_secretsmanager_secret.ersim_ai_tester_openai_api_key.id
  secret_string = var.secret_ersim_ai_tester_openai_api_key
}

############################################
# FUTURE SECRETS (Provision now, wire later)
# Total: 15 secrets
############################################

resource "aws_secretsmanager_secret" "stripe_secret_key" {
  name        = "${local.future_secrets_path}/STRIPE_SECRET_KEY"
  description = "Stripe secret key (future payment integration)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "stripe_secret_key" {
  secret_id     = aws_secretsmanager_secret.stripe_secret_key.id
  secret_string = var.secret_stripe_secret_key
}

resource "aws_secretsmanager_secret" "stripe_webhook_secret" {
  name        = "${local.future_secrets_path}/STRIPE_WEBHOOK_SECRET"
  description = "Stripe webhook secret (future payment integration)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "stripe_webhook_secret" {
  secret_id     = aws_secretsmanager_secret.stripe_webhook_secret.id
  secret_string = var.secret_stripe_webhook_secret
}

resource "aws_secretsmanager_secret" "revenuecat_api_key" {
  name        = "${local.future_secrets_path}/REVENUECAT_API_KEY"
  description = "RevenueCat API key (future mobile IAP)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "revenuecat_api_key" {
  secret_id     = aws_secretsmanager_secret.revenuecat_api_key.id
  secret_string = var.secret_revenuecat_api_key
}

resource "aws_secretsmanager_secret" "revenuecat_webhook_secret" {
  name        = "${local.future_secrets_path}/REVENUECAT_WEBHOOK_SECRET"
  description = "RevenueCat webhook secret (future mobile IAP)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "revenuecat_webhook_secret" {
  secret_id     = aws_secretsmanager_secret.revenuecat_webhook_secret.id
  secret_string = var.secret_revenuecat_webhook_secret
}

resource "aws_secretsmanager_secret" "sentry_dsn" {
  name        = "${local.future_secrets_path}/SENTRY_DSN"
  description = "Sentry DSN for error tracking"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "sentry_dsn" {
  secret_id     = aws_secretsmanager_secret.sentry_dsn.id
  secret_string = var.secret_sentry_dsn
}

resource "aws_secretsmanager_secret" "sendgrid_api_key" {
  name        = "${local.future_secrets_path}/SENDGRID_API_KEY"
  description = "SendGrid API key for transactional emails"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "sendgrid_api_key" {
  secret_id     = aws_secretsmanager_secret.sendgrid_api_key.id
  secret_string = var.secret_sendgrid_api_key
}

resource "aws_secretsmanager_secret" "cloudflare_api_token" {
  name        = "${local.future_secrets_path}/CLOUDFLARE_API_TOKEN"
  description = "Cloudflare API token for DNS/CDN management"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_api_token" {
  secret_id     = aws_secretsmanager_secret.cloudflare_api_token.id
  secret_string = var.secret_cloudflare_api_token
}

resource "aws_secretsmanager_secret" "google_client_secret" {
  name        = "${local.future_secrets_path}/GOOGLE_CLIENT_SECRET"
  description = "Google OAuth client secret"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "google_client_secret" {
  secret_id     = aws_secretsmanager_secret.google_client_secret.id
  secret_string = var.secret_google_client_secret
}

resource "aws_secretsmanager_secret" "google_translate_api_key" {
  name        = "${local.future_secrets_path}/GOOGLE_TRANSLATE_API_KEY"
  description = "Google Translate API key"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "google_translate_api_key" {
  secret_id     = aws_secretsmanager_secret.google_translate_api_key.id
  secret_string = var.secret_google_translate_api_key
}

resource "aws_secretsmanager_secret" "aws_access_key_id" {
  name        = "${local.future_secrets_path}/AWS_ACCESS_KEY_ID"
  description = "AWS access key (for services that can't use IAM roles)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "aws_access_key_id" {
  secret_id     = aws_secretsmanager_secret.aws_access_key_id.id
  secret_string = var.secret_aws_access_key_id
}

resource "aws_secretsmanager_secret" "aws_secret_access_key" {
  name        = "${local.future_secrets_path}/AWS_SECRET_ACCESS_KEY"
  description = "AWS secret access key (for services that can't use IAM roles)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "aws_secret_access_key" {
  secret_id     = aws_secretsmanager_secret.aws_secret_access_key.id
  secret_string = var.secret_aws_secret_access_key
}

resource "aws_secretsmanager_secret" "supabase_service_role_key" {
  name        = "${local.future_secrets_path}/SUPABASE_SERVICE_ROLE_KEY"
  description = "Supabase service role key (admin operations)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "supabase_service_role_key" {
  secret_id     = aws_secretsmanager_secret.supabase_service_role_key.id
  secret_string = var.secret_supabase_service_role_key
}

resource "aws_secretsmanager_secret" "supabase_database_url" {
  name        = "${local.future_secrets_path}/SUPABASE_DATABASE_URL"
  description = "Supabase direct database connection URL"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "supabase_database_url" {
  secret_id     = aws_secretsmanager_secret.supabase_database_url.id
  secret_string = var.secret_supabase_database_url
}

resource "aws_secretsmanager_secret" "openai_realtime_api_key" {
  name        = "${local.future_secrets_path}/OPENAI_REALTIME_API_KEY"
  description = "OpenAI Realtime API key (web-app voice)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "openai_realtime_api_key" {
  secret_id     = aws_secretsmanager_secret.openai_realtime_api_key.id
  secret_string = var.secret_openai_realtime_api_key
}

resource "aws_secretsmanager_secret" "realtime_api_key" {
  name        = "${local.future_secrets_path}/REALTIME_API_KEY"
  description = "Legacy realtime API key (alias)"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "future"
  }
}

resource "aws_secretsmanager_secret_version" "realtime_api_key" {
  secret_id     = aws_secretsmanager_secret.realtime_api_key.id
  secret_string = var.secret_realtime_api_key
}

############################################
# INFRASTRUCTURE SECRETS (Infra/Integration)
# Total: 19 secrets
############################################

# --- Supabase ---

resource "aws_secretsmanager_secret" "supabase_url" {
  name        = "${local.infra_secrets_path}/SUPABASE_URL"
  description = "Supabase project URL"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "supabase_url" {
  secret_id     = aws_secretsmanager_secret.supabase_url.id
  secret_string = var.secret_supabase_url
}

# --- SendGrid ---

resource "aws_secretsmanager_secret" "sendgrid_from_email" {
  name        = "${local.infra_secrets_path}/SENDGRID_FROM_EMAIL"
  description = "SendGrid sender email address"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "sendgrid_from_email" {
  secret_id     = aws_secretsmanager_secret.sendgrid_from_email.id
  secret_string = var.secret_sendgrid_from_email
}

# --- Cloudflare ---

resource "aws_secretsmanager_secret" "cloudflare_zone_id" {
  name        = "${local.infra_secrets_path}/CLOUDFLARE_ZONE_ID"
  description = "Cloudflare zone ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_zone_id" {
  secret_id     = aws_secretsmanager_secret.cloudflare_zone_id.id
  secret_string = var.secret_cloudflare_zone_id
}

resource "aws_secretsmanager_secret" "cloudflare_account_id" {
  name        = "${local.infra_secrets_path}/CLOUDFLARE_ACCOUNT_ID"
  description = "Cloudflare account ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_account_id" {
  secret_id     = aws_secretsmanager_secret.cloudflare_account_id.id
  secret_string = var.secret_cloudflare_account_id
}

resource "aws_secretsmanager_secret" "cloudflare_email" {
  name        = "${local.infra_secrets_path}/CLOUDFLARE_EMAIL"
  description = "Cloudflare account email"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_email" {
  secret_id     = aws_secretsmanager_secret.cloudflare_email.id
  secret_string = var.secret_cloudflare_email
}

# --- Google ---

resource "aws_secretsmanager_secret" "google_client_id" {
  name        = "${local.infra_secrets_path}/GOOGLE_CLIENT_ID"
  description = "Google OAuth client ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "google_client_id" {
  secret_id     = aws_secretsmanager_secret.google_client_id.id
  secret_string = var.secret_google_client_id
}

resource "aws_secretsmanager_secret" "google_translate_project_id" {
  name        = "${local.infra_secrets_path}/GOOGLE_TRANSLATE_PROJECT_ID"
  description = "Google Cloud project ID for translation"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "google_translate_project_id" {
  secret_id     = aws_secretsmanager_secret.google_translate_project_id.id
  secret_string = var.secret_google_translate_project_id
}

# --- Firebase ---

resource "aws_secretsmanager_secret" "firebase_api_key" {
  name        = "${local.infra_secrets_path}/FIREBASE_API_KEY"
  description = "Firebase API key"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "firebase_api_key" {
  secret_id     = aws_secretsmanager_secret.firebase_api_key.id
  secret_string = var.secret_firebase_api_key
}

resource "aws_secretsmanager_secret" "firebase_auth_domain" {
  name        = "${local.infra_secrets_path}/FIREBASE_AUTH_DOMAIN"
  description = "Firebase auth domain"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "firebase_auth_domain" {
  secret_id     = aws_secretsmanager_secret.firebase_auth_domain.id
  secret_string = var.secret_firebase_auth_domain
}

resource "aws_secretsmanager_secret" "firebase_project_id" {
  name        = "${local.infra_secrets_path}/FIREBASE_PROJECT_ID"
  description = "Firebase project ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "firebase_project_id" {
  secret_id     = aws_secretsmanager_secret.firebase_project_id.id
  secret_string = var.secret_firebase_project_id
}

resource "aws_secretsmanager_secret" "firebase_storage_bucket" {
  name        = "${local.infra_secrets_path}/FIREBASE_STORAGE_BUCKET"
  description = "Firebase storage bucket"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "firebase_storage_bucket" {
  secret_id     = aws_secretsmanager_secret.firebase_storage_bucket.id
  secret_string = var.secret_firebase_storage_bucket
}

resource "aws_secretsmanager_secret" "firebase_messaging_sender_id" {
  name        = "${local.infra_secrets_path}/FIREBASE_MESSAGING_SENDER_ID"
  description = "Firebase messaging sender ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "firebase_messaging_sender_id" {
  secret_id     = aws_secretsmanager_secret.firebase_messaging_sender_id.id
  secret_string = var.secret_firebase_messaging_sender_id
}

resource "aws_secretsmanager_secret" "firebase_app_id" {
  name        = "${local.infra_secrets_path}/FIREBASE_APP_ID"
  description = "Firebase app ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "firebase_app_id" {
  secret_id     = aws_secretsmanager_secret.firebase_app_id.id
  secret_string = var.secret_firebase_app_id
}

resource "aws_secretsmanager_secret" "firebase_measurement_id" {
  name        = "${local.infra_secrets_path}/FIREBASE_MEASUREMENT_ID"
  description = "Firebase measurement/analytics ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "firebase_measurement_id" {
  secret_id     = aws_secretsmanager_secret.firebase_measurement_id.id
  secret_string = var.secret_firebase_measurement_id
}

# --- Google Sheets / Apps Script ---

resource "aws_secretsmanager_secret" "google_sheet_id" {
  name        = "${local.infra_secrets_path}/GOOGLE_SHEET_ID"
  description = "Google Sheet ID for sim-monitor"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "google_sheet_id" {
  secret_id     = aws_secretsmanager_secret.google_sheet_id.id
  secret_string = var.secret_google_sheet_id
}

resource "aws_secretsmanager_secret" "apps_script_id" {
  name        = "${local.infra_secrets_path}/APPS_SCRIPT_ID"
  description = "Google Apps Script project ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "apps_script_id" {
  secret_id     = aws_secretsmanager_secret.apps_script_id.id
  secret_string = var.secret_apps_script_id
}

resource "aws_secretsmanager_secret" "atsr_script_id" {
  name        = "${local.infra_secrets_path}/ATSR_SCRIPT_ID"
  description = "ATSR Apps Script ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "atsr_script_id" {
  secret_id     = aws_secretsmanager_secret.atsr_script_id.id
  secret_string = var.secret_atsr_script_id
}

resource "aws_secretsmanager_secret" "apps_script_deployment_id" {
  name        = "${local.infra_secrets_path}/DEPLOYMENT_ID"
  description = "Apps Script deployment ID"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "apps_script_deployment_id" {
  secret_id     = aws_secretsmanager_secret.apps_script_deployment_id.id
  secret_string = var.secret_apps_script_deployment_id
}

resource "aws_secretsmanager_secret" "apps_script_web_app_url" {
  name        = "${local.infra_secrets_path}/WEB_APP_URL"
  description = "Apps Script web app URL"

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Category    = "infra"
  }
}

resource "aws_secretsmanager_secret_version" "apps_script_web_app_url" {
  secret_id     = aws_secretsmanager_secret.apps_script_web_app_url.id
  secret_string = var.secret_apps_script_web_app_url
}
