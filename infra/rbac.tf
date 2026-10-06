# Least-privilege RBAC for every identity in the solution. The hosted agent's
# own identity only exists after the first deploy; the azd postdeploy hook
# grants it Search Index Data Reader (see scripts/postdeploy.ps1).

locals {
  role_ids = {
    azure_ai_user                     = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
    cognitive_services_user           = "a97b65f3-24c7-4388-baec-2e87135dc908"
    cognitive_services_openai_user    = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
    search_service_contributor        = "7ca78c08-252a-4471-8644-bb5ff32d4ba0"
    search_index_data_contributor     = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
    search_index_data_reader          = "1407120a-92aa-4202-b7e9-c0e197c71c8f"
    log_analytics_reader              = "73c42c96-874c-492b-b04d-ab87d138a893"
    privileged_monitoring_data_reader = "dbc9c667-e97f-4491-aee6-90b9cf960190"
    reader                            = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
    website_contributor               = "de139f84-1756-47ae-9be6-808fbbe84772"
  }

  service_assignments = {
    # Foundry IQ knowledge base calls the model with the Search service identity.
    search-cs-user     = { principal = azapi_resource.search.identity[0].principal_id, role = "cognitive_services_user", scope = azapi_resource.foundry_account.id }
    search-openai-user = { principal = azapi_resource.search.identity[0].principal_id, role = "cognitive_services_openai_user", scope = azapi_resource.foundry_account.id }

    # Project identity: cloud evaluations (judge model) + reading traces for scheduled trace
    # evaluation. Reader on App Insights is required; without it trace runs fail/hang.
    project-cs-user        = { principal = azapi_resource.project.identity[0].principal_id, role = "cognitive_services_user", scope = azapi_resource.foundry_account.id }
    project-ai-user        = { principal = azapi_resource.project.identity[0].principal_id, role = "azure_ai_user", scope = azapi_resource.foundry_account.id }
    project-appi-reader    = { principal = azapi_resource.project.identity[0].principal_id, role = "reader", scope = azapi_resource.app_insights.id }
    project-appi-la-reader = { principal = azapi_resource.project.identity[0].principal_id, role = "log_analytics_reader", scope = azapi_resource.app_insights.id }
    project-appi-priv-mon  = { principal = azapi_resource.project.identity[0].principal_id, role = "privileged_monitoring_data_reader", scope = azapi_resource.app_insights.id }
    project-law-la-reader  = { principal = azapi_resource.project.identity[0].principal_id, role = "log_analytics_reader", scope = azapi_resource.log_analytics.id }

    # Chat UI invokes the agent endpoint with its managed identity.
    web-ai-user = { principal = azapi_resource.web.identity[0].principal_id, role = "azure_ai_user", scope = azapi_resource.project.id }

    # CI candidate: create agent versions, run evals, look up KB passages for groundedness context.
    # Cloud evaluators call the judge model as the principal that submits the eval run,
    # so the candidate identity needs OpenAI data-plane access on the account.
    ci-candidate-ai-user       = { principal = azapi_resource.ci_candidate.output.properties.principalId, role = "azure_ai_user", scope = azapi_resource.project.id }
    ci-candidate-openai-user   = { principal = azapi_resource.ci_candidate.output.properties.principalId, role = "cognitive_services_openai_user", scope = azapi_resource.foundry_account.id }
    ci-candidate-reader        = { principal = azapi_resource.ci_candidate.output.properties.principalId, role = "reader", scope = azapi_resource.resource_group.id }
    ci-candidate-search-reader = { principal = azapi_resource.ci_candidate.output.properties.principalId, role = "search_index_data_reader", scope = azapi_resource.search.id }
    ci-candidate-la-reader     = { principal = azapi_resource.ci_candidate.output.properties.principalId, role = "log_analytics_reader", scope = azapi_resource.log_analytics.id }

    # CI promote: move the production endpoint selector and deploy the chat UI.
    ci-promote-ai-user = { principal = azapi_resource.ci_promote.output.properties.principalId, role = "azure_ai_user", scope = azapi_resource.project.id }
    ci-promote-reader  = { principal = azapi_resource.ci_promote.output.properties.principalId, role = "reader", scope = azapi_resource.resource_group.id }
    ci-promote-web     = { principal = azapi_resource.ci_promote.output.properties.principalId, role = "website_contributor", scope = azapi_resource.web.id }
  }

  deployer_assignments = var.principal_id == "" ? {} : {
    deployer-ai-user     = { principal = var.principal_id, role = "azure_ai_user", scope = azapi_resource.project.id }
    deployer-openai-user = { principal = var.principal_id, role = "cognitive_services_openai_user", scope = azapi_resource.foundry_account.id }
    deployer-search-svc  = { principal = var.principal_id, role = "search_service_contributor", scope = azapi_resource.search.id }
    deployer-search-data = { principal = var.principal_id, role = "search_index_data_contributor", scope = azapi_resource.search.id }
    deployer-law-reader  = { principal = var.principal_id, role = "log_analytics_reader", scope = azapi_resource.log_analytics.id }
  }

  presenter_assignments = merge([
    for idx, id in var.presenter_principal_ids : {
      "presenter-${idx}-ai-user"    = { principal = id, role = "azure_ai_user", scope = azapi_resource.project.id }
      "presenter-${idx}-law-reader" = { principal = id, role = "log_analytics_reader", scope = azapi_resource.log_analytics.id }
    }
  ]...)
}

resource "azapi_resource" "service_role" {
  for_each = local.service_assignments

  type      = "Microsoft.Authorization/roleAssignments@2022-04-01"
  name      = uuidv5("url", "${each.value.scope}|${each.key}|${local.role_ids[each.value.role]}")
  parent_id = each.value.scope

  body = {
    properties = {
      principalId      = each.value.principal
      principalType    = "ServicePrincipal"
      roleDefinitionId = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${local.role_ids[each.value.role]}"
    }
  }
}

resource "azapi_resource" "user_role" {
  for_each = merge(local.deployer_assignments, local.presenter_assignments)

  type      = "Microsoft.Authorization/roleAssignments@2022-04-01"
  name      = uuidv5("url", "${each.value.scope}|${each.value.principal}|${local.role_ids[each.value.role]}")
  parent_id = each.value.scope

  body = {
    properties = {
      principalId      = each.value.principal
      principalType    = var.principal_type
      roleDefinitionId = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${local.role_ids[each.value.role]}"
    }
  }
}
