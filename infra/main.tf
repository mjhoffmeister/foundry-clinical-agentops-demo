# Core workload: resource group, observability, Foundry (account, project,
# model deployments, guardrail), and Azure AI Search for the Foundry IQ KB.

data "azurerm_client_config" "current" {}

locals {
  resource_group_name = var.resource_group_name != "" ? var.resource_group_name : "rg-${var.environment_name}"

  resource_token = substr(sha1(join("-", compact([
    var.subscription_id,
    local.resource_group_name,
    var.location,
    var.resource_token_salt,
  ]))), 0, 10)

  names = {
    foundry_account = "aif-cka-${local.resource_token}"
    foundry_project = "proj-clinical-agent"
    search          = "srch-cka-${local.resource_token}"
    log_analytics   = "log-cka-${local.resource_token}"
    app_insights    = "appi-cka-${local.resource_token}"
    plan            = "asp-cka-${local.resource_token}"
    web             = "app-cka-${local.resource_token}"
    ci_candidate    = "id-ci-candidate-${local.resource_token}"
    ci_promote      = "id-ci-promote-${local.resource_token}"
  }

  tags = {
    "azd-env-name"  = var.environment_name
    application     = "clinical-agentops-demo"
    workload        = "demo"
    SecurityControl = "Ignore"
  }

  guardrail_name = "clinical-guardrail"
}

resource "azapi_resource" "resource_group" {
  type      = "Microsoft.Resources/resourceGroups@2025-04-01"
  name      = local.resource_group_name
  location  = var.location
  parent_id = "/subscriptions/${var.subscription_id}"
  tags      = local.tags
}

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
resource "azapi_resource" "log_analytics" {
  type      = "Microsoft.OperationalInsights/workspaces@2025-02-01"
  name      = local.names.log_analytics
  location  = var.location
  parent_id = azapi_resource.resource_group.id
  tags      = local.tags

  body = {
    properties = {
      retentionInDays = 30
      sku             = { name = "PerGB2018" }
    }
  }
}

resource "azapi_resource" "app_insights" {
  type      = "Microsoft.Insights/components@2020-02-02"
  name      = local.names.app_insights
  location  = var.location
  parent_id = azapi_resource.resource_group.id
  tags      = local.tags

  body = {
    kind = "web"
    properties = {
      Application_Type    = "web"
      WorkspaceResourceId = azapi_resource.log_analytics.id
    }
  }

  response_export_values = ["properties.ConnectionString"]
}

# ---------------------------------------------------------------------------
# Azure AI Search (Foundry IQ knowledge base backing store)
# ---------------------------------------------------------------------------
resource "azapi_resource" "search" {
  type      = "Microsoft.Search/searchServices@2026-03-01-preview"
  name      = local.names.search
  location  = var.search_location
  parent_id = azapi_resource.resource_group.id
  tags      = local.tags

  identity {
    type = "SystemAssigned"
  }

  body = {
    sku = { name = "basic" }
    properties = {
      disableLocalAuth    = true
      hostingMode         = "Default"
      knowledgeRetrieval  = "standard"
      partitionCount      = 1
      replicaCount        = 1
      publicNetworkAccess = "Enabled"
      semanticSearch      = "free"
    }
  }

  schema_validation_enabled = false
  response_export_values    = ["identity.principalId"]
}

# ---------------------------------------------------------------------------
# Foundry account, guardrail, model deployments, project
# ---------------------------------------------------------------------------
resource "azapi_resource" "foundry_account" {
  type      = "Microsoft.CognitiveServices/accounts@2025-06-01"
  name      = local.names.foundry_account
  location  = var.location
  parent_id = azapi_resource.resource_group.id
  tags      = local.tags

  identity {
    type = "SystemAssigned"
  }

  body = {
    kind = "AIServices"
    sku  = { name = "S0" }
    properties = {
      allowProjectManagement = true
      customSubDomainName    = local.names.foundry_account
      disableLocalAuth       = true
      publicNetworkAccess    = "Enabled"
      networkAcls = {
        defaultAction       = "Allow"
        virtualNetworkRules = []
        ipRules             = []
      }
    }
  }

  schema_validation_enabled = false
}

# Guardrail (RAI policy) applied to every model deployment: harm categories
# blocked at Medium on both input and output, jailbreak + indirect-attack
# (XPIA) shields on input, protected-material detection on output.
resource "azapi_resource" "guardrail" {
  type      = "Microsoft.CognitiveServices/accounts/raiPolicies@2025-06-01"
  name      = local.guardrail_name
  parent_id = azapi_resource.foundry_account.id

  body = {
    properties = {
      basePolicyName = "Microsoft.DefaultV2"
      mode           = "Default"
      contentFilters = concat(
        flatten([
          for source in ["Prompt", "Completion"] : [
            for category in ["Hate", "Sexual", "Violence", "Selfharm"] : {
              name              = category
              enabled           = true
              blocking          = true
              severityThreshold = "Medium"
              source            = source
            }
          ]
        ]),
        [
          { name = "Jailbreak", enabled = true, blocking = true, source = "Prompt" },
          { name = "Indirect Attack", enabled = true, blocking = true, source = "Prompt" },
          { name = "Protected Material Text", enabled = true, blocking = true, source = "Completion" },
          { name = "Protected Material Code", enabled = true, blocking = false, source = "Completion" },
        ]
      )
    }
  }

  schema_validation_enabled = false
}

resource "azapi_resource" "agent_model" {
  type      = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  name      = var.agent_model.name
  parent_id = azapi_resource.foundry_account.id

  body = {
    sku = {
      name     = "GlobalStandard"
      capacity = var.agent_model.capacity
    }
    properties = {
      model = {
        format  = "OpenAI"
        name    = var.agent_model.name
        version = var.agent_model.version
      }
      raiPolicyName        = azapi_resource.guardrail.name
      versionUpgradeOption = "NoAutoUpgrade"
    }
  }

  schema_validation_enabled = false
}

# ARM throttles concurrent deployments on one account, so chain them.
resource "azapi_resource" "judge_model" {
  type      = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  name      = var.judge_model.name
  parent_id = azapi_resource.foundry_account.id

  body = {
    sku = {
      name     = "GlobalStandard"
      capacity = var.judge_model.capacity
    }
    properties = {
      model = {
        format  = "OpenAI"
        name    = var.judge_model.name
        version = var.judge_model.version
      }
      raiPolicyName        = azapi_resource.guardrail.name
      versionUpgradeOption = "NoAutoUpgrade"
    }
  }

  schema_validation_enabled = false
  depends_on                = [azapi_resource.agent_model]
}

resource "azapi_resource" "project" {
  type      = "Microsoft.CognitiveServices/accounts/projects@2025-06-01"
  name      = local.names.foundry_project
  location  = var.location
  parent_id = azapi_resource.foundry_account.id
  tags      = local.tags

  identity {
    type = "SystemAssigned"
  }

  body = {
    properties = {
      displayName = "Clinical Knowledge Agent"
      description = "Clinical knowledge hosted agent (MedQuAD via Foundry IQ) with eval-gated CI/CD."
    }
  }

  schema_validation_enabled = false
  response_export_values    = ["identity.principalId"]
  depends_on                = [azapi_resource.judge_model]
}

# App Insights connections (account + project) light up Foundry tracing,
# the agent monitoring dashboard and continuous evaluation.
resource "azapi_resource" "account_appinsights_connection" {
  type      = "Microsoft.CognitiveServices/accounts/connections@2025-06-01"
  name      = "${local.names.foundry_account}-appinsights"
  parent_id = azapi_resource.foundry_account.id

  body = {
    properties = {
      category      = "AppInsights"
      target        = azapi_resource.app_insights.id
      authType      = "ApiKey"
      isSharedToAll = true
      credentials = {
        key = azapi_resource.app_insights.output.properties.ConnectionString
      }
      metadata = {
        ApiType    = "Azure"
        ResourceId = azapi_resource.app_insights.id
      }
    }
  }

  schema_validation_enabled = false
  depends_on                = [azapi_resource.project]
}

resource "azapi_resource" "project_appinsights_connection" {
  type      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01"
  name      = "appinsights"
  parent_id = azapi_resource.project.id

  body = {
    properties = {
      category      = "AppInsights"
      target        = azapi_resource.app_insights.id
      authType      = "ApiKey"
      isSharedToAll = true
      credentials = {
        key = azapi_resource.app_insights.output.properties.ConnectionString
      }
      metadata = {
        ApiType    = "Azure"
        ResourceId = azapi_resource.app_insights.id
      }
    }
  }

  schema_validation_enabled = false
  depends_on                = [azapi_resource.account_appinsights_connection]
}
