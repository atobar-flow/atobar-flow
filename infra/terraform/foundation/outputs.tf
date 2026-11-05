output "vpc_id" {
  description = "The ID of the VPC"
  value       = aws_vpc.main.id
}

output "public_subnet_id" {
  description = "The ID of the Public Subnet"
  value       = aws_subnet.public.id
}

output "spot_security_group_id" {
  description = "The ID of the Spot Security Group"
  value       = aws_security_group.spot.id
}

output "gha_dispatcher_role_arn" {
  description = "The ARN of the GitHub Actions OIDC role for supabase migrations"
  value       = aws_iam_role.gha_dispatcher.arn
}

output "gha_packer_role_arn" {
  description = "The ARN of the GitHub Actions Packer Build Role"
  value       = aws_iam_role.gha_packer.arn
}
