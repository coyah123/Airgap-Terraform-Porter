variable "env" { type = string }
variable "network" { type = string }

resource "terraform_data" "service" {
  input            = { env = var.env, network = var.network }
  triggers_replace = [var.env]
}

resource "terraform_data" "secret" {
  input = sensitive("s3cr3t-${var.env}")
}
