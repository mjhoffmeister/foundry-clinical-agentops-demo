variable "subscription_id" {
  description = "Azure subscription id."
  type        = string
}

variable "location" {
  description = "Azure region for all resources."
  type        = string
  default     = "eastus2"
}

variable "search_location" {
  description = "Region for Azure AI Search (separate because Search capacity is constrained in some regions)."
  type        = string
  default     = "centralus"
}

variable "web_location" {
  description = "Region for the App Service plan/site (separate because App Service VM quota varies by region)."
  type        = string
  default     = "centralus"
}

variable "resource_group_name" {
  description = "Resource group name. When empty, rg-<environment_name> is used."
  type        = string
  default     = ""
}

variable "environment_name" {
  description = "azd environment name."
  type        = string
}

variable "resource_token_salt" {
  description = "Optional salt to vary globally unique names across re-provisions."
  type        = string
  default     = ""
}

variable "principal_id" {
  description = "Object id of the person running azd provision (granted developer/data-plane roles)."
  type        = string
  default     = ""
}

variable "principal_type" {
  description = "Principal type of principal_id."
  type        = string
  default     = "User"
}

variable "presenter_principal_ids" {
  description = "Additional presenter object ids that need Foundry + Log Analytics read access."
  type        = list(string)
  default     = []
}

variable "agent_name" {
  description = "Hosted agent name (must match the azure.yaml service name)."
  type        = string
  default     = "clinical-agent"
}

variable "agent_model" {
  description = "Model deployment used by the hosted agent and the knowledge base."
  type = object({
    name     = string
    version  = string
    capacity = number
  })
  default = {
    name     = "gpt-5.4-mini"
    version  = "2026-03-17"
    capacity = 200
  }
}

variable "judge_model" {
  description = "Model deployment used as the LLM judge for evaluations."
  type = object({
    name     = string
    version  = string
    capacity = number
  })
  default = {
    name     = "gpt-5.4"
    version  = "2026-03-05"
    capacity = 150
  }
}

variable "allowed_model_asset_ids" {
  description = "Azure Policy allow-list (partial asset ids) for Foundry model deployments."
  type        = list(string)
  default = [
    "azureml://registries/azure-openai/models/gpt-5.4-mini/",
    "azureml://registries/azure-openai/models/gpt-5.4/",
  ]
}

variable "github_repository" {
  description = "GitHub owner/repo that is trusted for workload identity federation."
  type        = string
  default     = "mjhoffmeister/foundry-clinical-agentops-demo"
}

variable "github_oidc_subject_prefix" {
  description = "OIDC sub claim prefix for the repo (immutable-subject format: repo:<owner>@<ownerId>/<repo>@<repoId>)."
  type        = string
  default     = "repo:mjhoffmeister@15896550/foundry-clinical-agentops-demo@1405898390"
}
