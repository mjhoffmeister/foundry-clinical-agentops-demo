# Azure Policy governance on the workload resource group (built-in definitions).
#  - DENY: Foundry model deployments must use an allow-listed model.
#  - AUDIT: deployments must keep the jailbreak prompt shield and
#           protected-material output filter enabled + blocking.
# These surface in Foundry Control Plane > Compliance.

locals {
  policy_definitions = {
    approved_models   = "/providers/Microsoft.Authorization/policyDefinitions/aafe3651-cb78-4f68-9f81-e7e41509110f"
    prompt_filter     = "/providers/Microsoft.Authorization/policyDefinitions/f3a9c2e0-7b4d-4d8f-9c3a-2e1f6b9a8d4e"
    completion_filter = "/providers/Microsoft.Authorization/policyDefinitions/af253d37-136a-42f8-a1fc-30010c083d41"
  }
}

resource "azapi_resource" "policy_approved_models" {
  type      = "Microsoft.Authorization/policyAssignments@2024-04-01"
  name      = "foundry-approved-models"
  parent_id = azapi_resource.resource_group.id

  body = {
    properties = {
      displayName        = "Foundry: only approved models may be deployed"
      description        = "Deny model deployments outside the clinical agent allow-list."
      policyDefinitionId = local.policy_definitions.approved_models
      enforcementMode    = "Default"
      nonComplianceMessages = [
        { message = "Model is not on the approved list for the clinical knowledge workload. Request an exception via the AI governance board." }
      ]
      parameters = {
        effect            = { value = "Deny" }
        allowedAssetIds   = { value = var.allowed_model_asset_ids }
        allowedPublishers = { value = [] }
      }
    }
  }

  # Assign after the approved deployments exist so first provision is unaffected.
  depends_on = [azapi_resource.judge_model]
}

resource "azapi_resource" "policy_jailbreak_shield" {
  type      = "Microsoft.Authorization/policyAssignments@2024-04-01"
  name      = "foundry-jailbreak-shield"
  parent_id = azapi_resource.resource_group.id

  body = {
    properties = {
      displayName        = "Foundry: jailbreak prompt shield must be enabled and blocking"
      policyDefinitionId = local.policy_definitions.prompt_filter
      parameters = {
        effect                   = { value = "Audit" }
        filterName               = { value = "Jailbreak" }
        allowedEnabledForPrompt  = { value = ["true"] }
        allowedBlockingForPrompt = { value = ["true"] }
      }
    }
  }
}

resource "azapi_resource" "policy_indirect_attack_shield" {
  type      = "Microsoft.Authorization/policyAssignments@2024-04-01"
  name      = "foundry-xpia-shield"
  parent_id = azapi_resource.resource_group.id

  body = {
    properties = {
      displayName        = "Foundry: indirect-attack prompt shield must be enabled and blocking"
      policyDefinitionId = local.policy_definitions.prompt_filter
      parameters = {
        effect                   = { value = "Audit" }
        filterName               = { value = "Indirect Attack" }
        allowedEnabledForPrompt  = { value = ["true"] }
        allowedBlockingForPrompt = { value = ["true"] }
      }
    }
  }
}

resource "azapi_resource" "policy_protected_material" {
  type      = "Microsoft.Authorization/policyAssignments@2024-04-01"
  name      = "foundry-protected-material"
  parent_id = azapi_resource.resource_group.id

  body = {
    properties = {
      displayName        = "Foundry: protected-material output filter must be enabled and blocking"
      policyDefinitionId = local.policy_definitions.completion_filter
      parameters = {
        effect                       = { value = "Audit" }
        filterName                   = { value = "Protected Material Text" }
        allowedEnabledForCompletion  = { value = ["true"] }
        allowedBlockingForCompletion = { value = ["true"] }
      }
    }
  }
}
