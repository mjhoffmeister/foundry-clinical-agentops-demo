# Outputs are written into the azd environment; names must stay stable.

output "AZURE_RESOURCE_GROUP" {
  value = azapi_resource.resource_group.name
}

output "AZURE_FOUNDRY_RESOURCE_GROUP" {
  value = azapi_resource.resource_group.name
}

output "AZURE_TENANT_ID" {
  value = data.azurerm_client_config.current.tenant_id
}

output "AZURE_AI_ACCOUNT_NAME" {
  value = azapi_resource.foundry_account.name
}

output "AZURE_AI_PROJECT_NAME" {
  value = azapi_resource.project.name
}

output "AZURE_AI_PROJECT_ID" {
  value = azapi_resource.project.id
}

output "AZURE_OPENAI_ENDPOINT" {
  value = "https://${azapi_resource.foundry_account.name}.openai.azure.com/"
}

output "FOUNDRY_PROJECT_ENDPOINT" {
  value = local.project_endpoint
}

output "AZURE_AI_MODEL_DEPLOYMENT_NAME" {
  value = azapi_resource.agent_model.name
}

output "AZURE_AI_JUDGE_DEPLOYMENT_NAME" {
  value = azapi_resource.judge_model.name
}

output "AZURE_SEARCH_NAME" {
  value = azapi_resource.search.name
}

output "AZURE_SEARCH_ENDPOINT" {
  value = "https://${azapi_resource.search.name}.search.windows.net"
}

output "AZURE_SEARCH_ID" {
  value = azapi_resource.search.id
}

output "APPLICATIONINSIGHTS_CONNECTION_STRING" {
  value     = azapi_resource.app_insights.output.properties.ConnectionString
  sensitive = true
}

output "AZURE_APP_INSIGHTS_ID" {
  value = azapi_resource.app_insights.id
}

output "AZURE_LOG_ANALYTICS_WORKSPACE_ID" {
  value = azapi_resource.log_analytics.id
}

output "AGENT_NAME" {
  value = var.agent_name
}

output "WEB_APP_NAME" {
  value = azapi_resource.web.name
}

output "WEB_APP_URL" {
  value = "https://${local.web_hostname}"
}

output "CI_CANDIDATE_CLIENT_ID" {
  value = azapi_resource.ci_candidate.output.properties.clientId
}

output "CI_PROMOTE_CLIENT_ID" {
  value = azapi_resource.ci_promote.output.properties.clientId
}

output "GUARDRAIL_NAME" {
  value = azapi_resource.guardrail.name
}
