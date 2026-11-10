# Offline Plan Explorer fixture: only the built-in terraform_data resource,
# so init/plan need no providers and no network.
variable "envs" {
  type    = set(string)
  default = ["dev", "prod"]
}
variable "version_tag" {
  type    = string
  default = "v1"
}

resource "terraform_data" "network" {
  input = { cidr = "10.40.0.0/16", tag = var.version_tag }
}

resource "terraform_data" "subnet" {
  count = 3
  input = { cidr = cidrsubnet("10.40.0.0/16", 8, count.index), vpc = terraform_data.network.id }
}

module "app" {
  source   = "./modules/app"
  for_each = var.envs
  env      = each.key
  network  = terraform_data.network.id
}

output "network_id" {
  value = terraform_data.network.id
}
