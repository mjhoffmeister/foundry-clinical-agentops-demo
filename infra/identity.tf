# GitHub Actions workload identities (OIDC, no secrets). Split by duty:
#  - ci-candidate: PRs + main. Deploys candidate agent versions and runs evals.
#  - ci-promote:   only the `production` environment (required reviewer).
#                  Moves the production endpoint selector (compare-and-swap).

locals {
  github_issuer   = "https://token.actions.githubusercontent.com"
  github_audience = "api://AzureADTokenExchange"

  candidate_subjects = {
    "github-pull-request" = "${var.github_oidc_subject_prefix}:pull_request"
    "github-main"         = "${var.github_oidc_subject_prefix}:ref:refs/heads/main"
  }
}

resource "azapi_resource" "ci_candidate" {
  type      = "Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30"
  name      = local.names.ci_candidate
  location  = var.location
  parent_id = azapi_resource.resource_group.id
  tags      = local.tags

  response_export_values = ["properties.principalId", "properties.clientId"]
}

resource "azapi_resource" "ci_promote" {
  type      = "Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30"
  name      = local.names.ci_promote
  location  = var.location
  parent_id = azapi_resource.resource_group.id
  tags      = local.tags

  response_export_values = ["properties.principalId", "properties.clientId"]
}

# Federated credentials on one identity must be written sequentially.
resource "azapi_resource" "ci_candidate_fic_pr" {
  type      = "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30"
  name      = "github-pull-request"
  parent_id = azapi_resource.ci_candidate.id

  body = {
    properties = {
      issuer    = local.github_issuer
      subject   = local.candidate_subjects["github-pull-request"]
      audiences = [local.github_audience]
    }
  }
}

resource "azapi_resource" "ci_candidate_fic_main" {
  type      = "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30"
  name      = "github-main"
  parent_id = azapi_resource.ci_candidate.id

  body = {
    properties = {
      issuer    = local.github_issuer
      subject   = local.candidate_subjects["github-main"]
      audiences = [local.github_audience]
    }
  }

  depends_on = [azapi_resource.ci_candidate_fic_pr]
}

resource "azapi_resource" "ci_promote_fic_production" {
  type      = "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30"
  name      = "github-environment-production"
  parent_id = azapi_resource.ci_promote.id

  body = {
    properties = {
      issuer    = local.github_issuer
      subject   = "${var.github_oidc_subject_prefix}:environment:production"
      audiences = [local.github_audience]
    }
  }
}
