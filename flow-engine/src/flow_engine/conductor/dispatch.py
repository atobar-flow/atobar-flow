"""Role-aware Spot instance dispatch logic and CLI for atobar-flow."""

import argparse
import base64
import subprocess
import sys
from typing import Any, Callable, Optional


def build_user_data(
    role: str,
    tenant: str,
    pr: int,
    head_sha: str = "",
    label: str = "",
    engine_ref: str = "",
    run_id: str = "",
    run_cred: str = "",
    cp_url: str = "",
    agent_runtime: str = "nanobot",
    base_user_data: str = "",
) -> str:
    """Constructs the user data bash script injecting environment exports."""
    # Developer rework rounds resume from the evaluated SHA instead of discarding it
    target_ref = head_sha or "main"

    header_lines = [
        "#!/bin/bash",
        f'export FLOW_ROLE="{role}"',
        f'export FLOW_TENANT="{tenant}"',
        f'export FLOW_PR="{pr}"',
        f'export FLOW_HEAD_SHA="{head_sha}"',
        f'export TARGET_REPO="{tenant}"',
        f'export ISSUE_ID="{pr}"',
        f'export LABEL_NAME="{label}"',
        f'export ENGINE_REF="{engine_ref}"',
        f'export TARGET_REF="{target_ref}"',
        f'export FLOW_RUN_ID="{run_id}"',
        f'export FLOW_RUN_CREDENTIAL="{run_cred}"',
        f'export CONTROL_PANEL_URL="{cp_url}"',
        f'export FLOW_AGENT_RUNTIME="{agent_runtime}"',
        'export DISPLAY=":0"',
        "command -v xterm >/dev/null 2>&1 && command -v xdotool >/dev/null 2>&1 || (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xterm xdotool scrot fonts-dejavu-core >/dev/null 2>&1 || true)",
        "",
    ]

    base_lines = base_user_data.splitlines()
    if base_lines and base_lines[0].startswith("#!"):
        base_lines = base_lines[1:]

    return "\n".join(header_lines + base_lines) + "\n"


def build_run_instances_args(
    role: str,
    tenant: str,
    pr: int,
    head_sha: str = "",
    encoded_user_data: str = "",
    template_name: str = "flow-spot-sandbox-template",
) -> list[str]:
    """Generates the command arguments for 'aws ec2 run-instances'."""
    profile_name = "flow-spot-sandbox-profile"
    slug = tenant.replace("/", "__")
    tags = [
        f"{{Key=Role,Value={role}}}",
        f"{{Key=Tenant,Value={tenant}}}",
        f"{{Key=Pr,Value={pr}}}",
        f"{{Key=IssueId,Value={pr}}}",
        f"{{Key=Repository,Value={tenant}}}",
        f"{{Key=Name,Value=flow-{role}-{slug}-{pr}}}",
    ]
    if head_sha:
        tags.append(f"{{Key=HeadSha,Value={head_sha}}}")
    tag_spec = f"ResourceType=instance,Tags=[{','.join(tags)}]"

    return [
        "ec2",
        "run-instances",
        "--launch-template",
        f"LaunchTemplateName={template_name},Version=$Latest",
        "--iam-instance-profile",
        f"Name={profile_name}",
        "--instance-market-options",
        '{"MarketType":"spot"}',
        "--user-data",
        encoded_user_data,
        "--tag-specifications",
        tag_spec,
        "--query",
        "Instances[0].InstanceId",
        "--output",
        "text",
        "--no-cli-pager",
    ]


def dispatch_spot(
    role: str = "developer",
    tenant: str = "",
    pr: int = 0,
    head_sha: str = "",
    label: str = "",
    engine_ref: str = "",
    run_id: str = "",
    run_cred: str = "",
    cp_url: str = "",
    agent_runtime: str = "nanobot",
    dry_run: bool = False,
    template_name: str = "flow-spot-sandbox-template",
    runner: Optional[Callable[[list[str]], str]] = None,
) -> dict[str, Any]:
    """Prepares and launches or prints the role-aware Spot instance."""
    import re

    # Validate agent runtime
    valid_runtimes = {"native", "nanobot"}
    if agent_runtime not in valid_runtimes:
        raise ValueError(f"Invalid agent_runtime: {agent_runtime!r}. Must be one of {sorted(valid_runtimes)}")

    # Validate tenant format: owner/repo
    if not re.fullmatch(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", tenant):
        raise ValueError(f"Invalid tenant identifier: {tenant}")
    for part in tenant.split("/"):
        if part in (".", ".."):
            raise ValueError(f"Invalid tenant identifier: {tenant}")

    # Validate label
    if label:
        if not label.startswith("flow:"):
            raise ValueError(f"Invalid label: {label} (must start with flow:)")
        blocked_labels = {
            "flow:onboard", "flow:metrics", "flow:approved",
            "flow:escalated", "flow:in-progress",
            "flow:in-review", "flow:shipped", "flow:needs-approval"
        }
        if label in blocked_labels:
            raise ValueError(f"Invalid label: {label} is blocked from starting a spot instance")

    effective_role = role.strip().lower() if role else "developer"

    def default_runner(cmd: list[str]) -> str:
        proc = subprocess.run(
            ["aws"] + cmd,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Error dispatching Spot instance: Command {cmd} returned {proc.returncode}\n"
                f"Stderr: {proc.stderr}\nStdout: {proc.stdout}"
            )
        return proc.stdout.strip()

    run_cmd = runner or default_runner

    base_user_data = ""
    if not dry_run:
        # Fetch template's base user data
        describe_cmd = [
            "ec2",
            "describe-launch-template-versions",
            "--launch-template-name",
            template_name,
            "--versions",
            "$Latest",
            "--query",
            "LaunchTemplateVersions[0].LaunchTemplateData.UserData",
            "--output",
            "text",
        ]
        raw_b64 = run_cmd(describe_cmd)
        if raw_b64 and raw_b64 != "None":
            base_user_data = base64.b64decode(raw_b64).decode("utf-8")

    user_data_text = build_user_data(
        role=effective_role,
        tenant=tenant,
        pr=pr,
        head_sha=head_sha,
        label=label,
        engine_ref=engine_ref,
        run_id=run_id,
        run_cred=run_cred,
        cp_url=cp_url,
        agent_runtime=agent_runtime,
        base_user_data=base_user_data,
    )
    encoded_user_data = base64.b64encode(user_data_text.encode("utf-8")).decode("utf-8")

    args = build_run_instances_args(
        role=effective_role,
        tenant=tenant,
        pr=pr,
        head_sha=head_sha,
        encoded_user_data=encoded_user_data,
        template_name=template_name,
    )

    if dry_run:
        full_cmd_str = "aws " + " ".join(args)
        print(f"[DRY-RUN] {full_cmd_str}")
        return {"dry_run": True, "args": args}

    instance_id = run_cmd(args)
    print(f"Spawned Spot Instance: {instance_id} for {tenant}#{pr} (role: {effective_role})")
    return {"instance_id": instance_id, "args": args}


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch role-aware Spot instance")
    parser.add_argument("--role", default="developer", help="Swarm role (developer|evaluator|arbitrator)")
    parser.add_argument("--tenant", required=True, help="Target tenant repository (owner/repo)")
    parser.add_argument("--pr", type=int, required=True, help="Target issue or PR number")
    parser.add_argument("--head-sha", default="", help="PR head SHA")
    parser.add_argument("--label", default="", help="Trigger label")
    parser.add_argument("--engine-ref", default="", help="atobar-flow Git commit ref")
    parser.add_argument("--run-id", default="", help="Run ID for temporary credentials")
    parser.add_argument("--run-cred", default="", help="Temporary run credentials")
    parser.add_argument("--cp-url", default="", help="Control panel URL")
    parser.add_argument("--agent-runtime", default="nanobot", help="Agent execution runtime (nanobot)")
    parser.add_argument("--dry-run", action="store_true", help="Print run-instances command without executing")
    args = parser.parse_args()

    try:
        dispatch_spot(
            role=args.role,
            tenant=args.tenant,
            pr=args.pr,
            head_sha=args.head_sha,
            label=args.label,
            engine_ref=args.engine_ref,
            run_id=args.run_id,
            run_cred=args.run_cred,
            cp_url=args.cp_url,
            agent_runtime=args.agent_runtime,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"Error dispatching Spot instance: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
