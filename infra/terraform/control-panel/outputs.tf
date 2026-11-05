output "spot_launch_template_id" {
  description = "The ID of the EC2 Spot Launch Template"
  value       = aws_launch_template.flow_spot_sandbox.id
}
