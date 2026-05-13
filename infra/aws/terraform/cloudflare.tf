# ==============================================================================
# CLOUDFLARE DNS CONFIGURATION
# ==============================================================================
# Manages DNS records for ersimulator.com and contextdna.io domains.
#
# ER Simulator records:
# - admin.ersimulator.com -> Vercel (NASA Control Panel dashboard)
# - api.ersimulator.com   -> AWS ALB (backend API)
# - get.ersimulator.com   -> Lemon Squeezy (International checkout)
#
# Context DNA records:
# - contextdna.io         -> Vercel (main site)
# - www.contextdna.io     -> Vercel (redirect)
# - app.contextdna.io     -> Vercel (dashboard)
#
# Prerequisites:
# - Cloudflare API token with DNS edit permissions for both zones
# - Zone IDs for ersimulator.com and contextdna.io
#
# Add to terraform.tfvars.local:
#   secret_cloudflare_api_token     = "your-cloudflare-api-token"
#   secret_cloudflare_zone_id       = "your-ersimulator-zone-id"
#   secret_contextdna_zone_id       = "your-contextdna-zone-id"
#   vercel_admin_cname              = "cname.vercel-dns.com"
# ==============================================================================

# Conditionally configure Cloudflare provider only if credentials are set
provider "cloudflare" {
  api_token = var.secret_cloudflare_api_token != "PLACEHOLDER_NOT_SET" ? var.secret_cloudflare_api_token : null
}

# ==============================================================================
# VARIABLES
# ==============================================================================

variable "vercel_admin_cname" {
  description = "Vercel CNAME target for admin.ersimulator.com (typically cname.vercel-dns.com)"
  type        = string
  default     = "cname.vercel-dns.com"
}

variable "enable_cloudflare_dns" {
  description = "Enable Cloudflare DNS management (set to true when credentials are configured)"
  type        = bool
  default     = false
}

variable "lemon_squeezy_store" {
  description = "Lemon Squeezy store subdomain (e.g., ersimulator.lemonsqueezy.com)"
  type        = string
  default     = "ersimulator.lemonsqueezy.com"
}

variable "secret_contextdna_zone_id" {
  description = "Cloudflare Zone ID for contextdna.io"
  type        = string
  default     = "PLACEHOLDER_NOT_SET"
  sensitive   = true
}

# ==============================================================================
# DNS RECORDS
# ==============================================================================

# NASA Control Panel Dashboard - Points to Vercel
# This is where admin.ersimulator.com will be served from
resource "cloudflare_dns_record" "admin_dashboard" {
  count = var.enable_cloudflare_dns && var.secret_cloudflare_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_cloudflare_zone_id
  name    = "admin"
  content = var.vercel_admin_cname
  type    = "CNAME"
  ttl     = 1  # Auto TTL when proxied
  proxied = true

  comment = "NASA Control Panel dashboard (Vercel Next.js)"
}

# API subdomain - Points to AWS ALB
# api.ersimulator.com -> ALB for backend
resource "cloudflare_dns_record" "api_backend" {
  count = var.enable_cloudflare_dns && var.secret_cloudflare_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_cloudflare_zone_id
  name    = "api"
  content = aws_lb.app.dns_name
  type    = "CNAME"
  ttl     = 1  # Auto TTL when proxied
  proxied = true

  comment = "Backend API (AWS ALB)"
}

# Lemon Squeezy checkout - International payments
# get.ersimulator.com -> Lemon Squeezy store
# Geo-routing is handled client-side in the pricing page to avoid SEO issues
resource "cloudflare_dns_record" "lemon_squeezy_checkout" {
  count = var.enable_cloudflare_dns && var.secret_cloudflare_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_cloudflare_zone_id
  name    = "get"
  content = var.lemon_squeezy_store
  type    = "CNAME"
  ttl     = 1  # Auto TTL when proxied
  proxied = true

  comment = "Lemon Squeezy checkout (International payments)"
}

# LiveKit Server - WebRTC signaling and media
# livekit.ersimulator.com -> Dedicated c6i.large CPU instance
# IMPORTANT: NOT proxied - WebRTC requires direct connection for UDP
resource "cloudflare_dns_record" "livekit_server" {
  count = var.enable_cloudflare_dns && var.secret_cloudflare_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_cloudflare_zone_id
  name    = "livekit"
  content = aws_eip.livekit.public_ip
  type    = "A"
  ttl     = 300  # 5 minutes (NOT auto - not proxied)
  proxied = false  # CRITICAL: WebRTC needs direct UDP access, cannot go through Cloudflare proxy

  comment = "LiveKit WebRTC server (dedicated CPU instance)"
}

# Voice AI Toggle - GPU/LiveKit control panel
# voice.ersimulator.com -> API Gateway custom domain (Lambda)
# Note: Requires API Gateway custom domain mapping (d-g7wkng62a7.execute-api.us-west-2.amazonaws.com)
resource "cloudflare_dns_record" "voice_toggle" {
  count = var.enable_cloudflare_dns && var.secret_cloudflare_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_cloudflare_zone_id
  name    = "voice"
  content = "d-g7wkng62a7.execute-api.us-west-2.amazonaws.com"  # API Gateway custom domain endpoint
  type    = "CNAME"
  ttl     = 300  # 5 min TTL (not proxied)
  proxied = false  # Must be false - API Gateway handles TLS with ACM cert

  comment = "Voice AI GPU toggle control panel (API Gateway Lambda)"
}

# ==============================================================================
# CONTEXT DNA DNS RECORDS (contextdna.io)
# ==============================================================================

# Context DNA root domain - Points to Vercel
# contextdna.io -> Vercel dashboard
resource "cloudflare_dns_record" "contextdna_root" {
  count = var.enable_cloudflare_dns && var.secret_contextdna_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_contextdna_zone_id
  name    = "@"
  content = var.vercel_admin_cname
  type    = "CNAME"
  ttl     = 1  # Auto TTL
  proxied = false  # DNS only for Vercel SSL

  comment = "Context DNA main site (Vercel)"
}

# Context DNA www subdomain - Points to Vercel
# www.contextdna.io -> Vercel (redirects to root)
resource "cloudflare_dns_record" "contextdna_www" {
  count = var.enable_cloudflare_dns && var.secret_contextdna_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_contextdna_zone_id
  name    = "www"
  content = var.vercel_admin_cname
  type    = "CNAME"
  ttl     = 1  # Auto TTL
  proxied = false  # DNS only for Vercel SSL

  comment = "Context DNA www redirect (Vercel)"
}

# Context DNA app subdomain - Points to Vercel
# app.contextdna.io -> Vercel dashboard app
resource "cloudflare_dns_record" "contextdna_app" {
  count = var.enable_cloudflare_dns && var.secret_contextdna_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_contextdna_zone_id
  name    = "app"
  content = var.vercel_admin_cname
  type    = "CNAME"
  ttl     = 1  # Auto TTL
  proxied = false  # DNS only for Vercel SSL

  comment = "Context DNA dashboard app (Vercel)"
}

# Context DNA API subdomain - Points to AWS ALB (same backend as api.ersimulator.com)
# api.contextdna.io -> AWS ALB for Context DNA API endpoints
resource "cloudflare_dns_record" "contextdna_api" {
  count = var.enable_cloudflare_dns && var.secret_contextdna_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_contextdna_zone_id
  name    = "api"
  content = aws_lb.app.dns_name
  type    = "CNAME"
  ttl     = 1  # Auto TTL when proxied
  proxied = true

  comment = "Context DNA API (AWS ALB - shared backend)"
}

# Context DNA Admin Dashboard - Points to Vercel
# admin.contextdna.io -> Vercel (v0-sand dashboard for testing/development)
resource "cloudflare_dns_record" "contextdna_admin" {
  count = var.enable_cloudflare_dns && var.secret_contextdna_zone_id != "PLACEHOLDER_NOT_SET" ? 1 : 0

  zone_id = var.secret_contextdna_zone_id
  name    = "admin"
  content = var.vercel_admin_cname
  type    = "CNAME"
  ttl     = 1  # Auto TTL
  proxied = false  # DNS only for Vercel SSL

  comment = "Context DNA admin dashboard (Vercel - v0-sand testing)"
}

# ==============================================================================
# OUTPUTS
# ==============================================================================

output "admin_dashboard_url" {
  description = "NASA Control Panel dashboard URL"
  value       = var.enable_cloudflare_dns ? "https://admin.ersimulator.com" : "Not configured"
}

output "lemon_squeezy_checkout_url" {
  description = "Lemon Squeezy checkout URL (International)"
  value       = var.enable_cloudflare_dns ? "https://get.ersimulator.com" : "Not configured"
}

output "cloudflare_dns_enabled" {
  description = "Whether Cloudflare DNS management is enabled"
  value       = var.enable_cloudflare_dns
}

output "contextdna_url" {
  description = "Context DNA main site URL"
  value       = var.enable_cloudflare_dns && var.secret_contextdna_zone_id != "PLACEHOLDER_NOT_SET" ? "https://contextdna.io" : "Not configured"
}

output "contextdna_app_url" {
  description = "Context DNA dashboard app URL"
  value       = var.enable_cloudflare_dns && var.secret_contextdna_zone_id != "PLACEHOLDER_NOT_SET" ? "https://app.contextdna.io" : "Not configured"
}

output "contextdna_api_url" {
  description = "Context DNA API URL"
  value       = var.enable_cloudflare_dns && var.secret_contextdna_zone_id != "PLACEHOLDER_NOT_SET" ? "https://api.contextdna.io" : "Not configured"
}

output "contextdna_admin_url" {
  description = "Context DNA admin dashboard URL"
  value       = var.enable_cloudflare_dns && var.secret_contextdna_zone_id != "PLACEHOLDER_NOT_SET" ? "https://admin.contextdna.io" : "Not configured"
}
