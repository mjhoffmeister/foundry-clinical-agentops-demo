# Chat UI: App Service (Linux, Python) behind Easy Auth (Entra ID). The
# site's system-assigned managed identity calls the hosted agent endpoint.

resource "azapi_resource" "plan" {
  type      = "Microsoft.Web/serverfarms@2024-11-01"
  name      = local.names.plan
  location  = var.web_location
  parent_id = azapi_resource.resource_group.id
  tags      = local.tags

  body = {
    kind = "linux"
    sku = {
      name = "B1"
      tier = "Basic"
    }
    properties = {
      reserved = true
    }
  }
}

resource "azapi_resource" "web" {
  type      = "Microsoft.Web/sites@2024-11-01"
  name      = local.names.web
  location  = var.web_location
  parent_id = azapi_resource.resource_group.id
  tags      = merge(local.tags, { "azd-service-name" = "web" })

  identity {
    type = "SystemAssigned"
  }

  body = {
    kind = "app,linux"
    properties = {
      serverFarmId = azapi_resource.plan.id
      httpsOnly    = true
      siteConfig = {
        linuxFxVersion  = "PYTHON|3.14"
        alwaysOn        = true
        ftpsState       = "Disabled"
        minTlsVersion   = "1.2"
        appCommandLine  = "python -m uvicorn app:app --host 0.0.0.0 --port 8000"
        healthCheckPath = "/healthz"
        appSettings = [
          { name = "SCM_DO_BUILD_DURING_DEPLOYMENT", value = "true" },
          { name = "WEBSITES_PORT", value = "8000" },
          { name = "FOUNDRY_PROJECT_ENDPOINT", value = local.project_endpoint },
          { name = "AGENT_NAME", value = var.agent_name },
          { name = "APPLICATIONINSIGHTS_CONNECTION_STRING", value = azapi_resource.app_insights.output.properties.ConnectionString },
          { name = "OTEL_SERVICE_NAME", value = "clinical-chat-web" },
        ]
      }
    }
  }

  schema_validation_enabled = false
  response_export_values    = ["identity.principalId", "properties.defaultHostName"]
}

locals {
  web_hostname     = azapi_resource.web.output.properties.defaultHostName
  project_endpoint = "https://${local.names.foundry_account}.services.ai.azure.com/api/projects/${local.names.foundry_project}"
}

resource "azuread_application" "web" {
  display_name     = "clinical-agentops-demo-web-${local.resource_token}"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azurerm_client_config.current.object_id]

  web {
    redirect_uris = ["https://${local.web_hostname}/.auth/login/aad/callback"]
    implicit_grant {
      id_token_issuance_enabled = true
    }
  }
}

resource "azuread_service_principal" "web" {
  client_id = azuread_application.web.client_id
  owners    = [data.azurerm_client_config.current.object_id]
}

# No client secret: Easy Auth uses the OIDC implicit (id_token) flow, so
# there is nothing to rotate or leak.
# authsettingsV2 is a singleton that exists on every site, so update it in place.
resource "azapi_update_resource" "web_auth" {
  type      = "Microsoft.Web/sites/config@2024-11-01"
  name      = "authsettingsV2"
  parent_id = azapi_resource.web.id

  body = {
    properties = {
      platform = { enabled = true }
      globalValidation = {
        requireAuthentication       = true
        unauthenticatedClientAction = "RedirectToLoginPage"
        redirectToProvider          = "azureactivedirectory"
        excludedPaths               = ["/healthz"]
      }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            clientId     = azuread_application.web.client_id
            openIdIssuer = "https://sts.windows.net/${data.azurerm_client_config.current.tenant_id}/v2.0"
          }
          validation = {
            allowedAudiences = ["api://${azuread_application.web.client_id}", azuread_application.web.client_id]
          }
        }
      }
      login = {
        tokenStore = { enabled = true }
      }
    }
  }

  depends_on = [azuread_service_principal.web]
}
