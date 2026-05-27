"""st-aws-setup - provision IAM Roles Anywhere + CloudFormation deploy roles."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import typer

from stratatools.util import info, run

app = typer.Typer(
    no_args_is_help=False,
    add_completion=False,
    help="Provision IAM Roles Anywhere trust anchor/profile and deployer IAM roles for AWS pusher usage.",
)

_AWS_PROFILE_OVERRIDE = ""
_AWS_DEFAULT_REGION_OVERRIDE = ""


def _aws(
    args: list[str],
    *,
    dry_run: bool,
    capture: bool = True,
    check: bool = True,
) -> str:
    cmd = ["aws"]
    if _AWS_PROFILE_OVERRIDE:
        cmd.extend(["--profile", _AWS_PROFILE_OVERRIDE])
    if _AWS_DEFAULT_REGION_OVERRIDE:
        cmd.extend(["--region", _AWS_DEFAULT_REGION_OVERRIDE])
    cmd.extend(args)
    result = run(cmd, dry_run=dry_run, capture=capture, check=check)
    if result is None:
        return ""
    return (result.stdout or "").strip()


def _aws_json(args: list[str], *, dry_run: bool, default: Any) -> Any:
    out = _aws(args, dry_run=dry_run, capture=True, check=False)
    if not out:
        return default
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return default


def _aws_text(args: list[str], *, dry_run: bool, default: str = "") -> str:
    out = _aws(args, dry_run=dry_run, capture=True, check=False)
    value = out.strip()
    if not value or value in ("None", "null"):
        return default
    return value


def _put_json_file(payload: dict[str, Any]) -> str:
    handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
    with handle:
        json.dump(payload, handle)
    return handle.name


def _ensure_root_ca_cert(root_ca_cert: Path, *, dry_run: bool) -> Path:
    cert_path = root_ca_cert.expanduser()
    if cert_path.exists():
        return cert_path

    ca_dir = cert_path.parent
    ca_key = ca_dir / "rootCA.key"
    ca_conf = ca_dir / "ca.conf"

    info(f"root CA not found at {cert_path}; generating local PKI assets")
    if dry_run:
        info(f"dry-run: would create {ca_dir}, {ca_conf}, {ca_key}, and {cert_path}")
        return cert_path

    ca_dir.mkdir(parents=True, exist_ok=True)

    ca_conf.write_text(
        """[ req ]
distinguished_name = req_distinguished_name
prompt = no
x509_extensions = v3_ca

[ req_distinguished_name ]
CN = MyLocalDeploymentCA

[ v3_ca ]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical, CA:true
keyUsage = critical, digitalSignature, cRLSign, keyCertSign
""",
        encoding="utf-8",
    )

    run(["openssl", "genrsa", "-out", str(ca_key), "2048"], check=True)
    run(
        [
            "openssl",
            "req",
            "-x509",
            "-new",
            "-nodes",
            "-key",
            str(ca_key),
            "-sha256",
            "-days",
            "3650",
            "-out",
            str(cert_path),
            "-config",
            str(ca_conf),
        ],
        check=True,
    )
    info(f"generated root CA certificate: {cert_path}")
    return cert_path


def _ensure_role(
    *,
    role_name: str,
    assume_policy: dict[str, Any],
    dry_run: bool,
) -> str:
    info(f"ensuring IAM role: {role_name}")
    role_arn = _aws_text(
        ["iam", "get-role", "--role-name", role_name, "--query", "Role.Arn", "--output", "text"],
        dry_run=dry_run,
    )
    assume_path = _put_json_file(assume_policy)

    if not role_arn:
        role_arn = _aws_text(
            [
                "iam",
                "create-role",
                "--role-name",
                role_name,
                "--assume-role-policy-document",
                f"file://{assume_path}",
                "--query",
                "Role.Arn",
                "--output",
                "text",
            ],
            dry_run=dry_run,
            default=f"arn:aws:iam::DRY_RUN:role/{role_name}",
        )
    else:
        _aws(
            [
                "iam",
                "update-assume-role-policy",
                "--role-name",
                role_name,
                "--policy-document",
                f"file://{assume_path}",
            ],
            dry_run=dry_run,
            capture=False,
            check=True,
        )
    return role_arn


def _ensure_managed_policy(
    *,
    policy_name: str,
    policy_document: dict[str, Any],
    dry_run: bool,
    account_id: str,
) -> str:
    info(f"ensuring managed policy: {policy_name}")
    policy_arn = _aws_text(
        [
            "iam",
            "list-policies",
            "--scope",
            "Local",
            "--query",
            f"Policies[?PolicyName=='{policy_name}'].Arn | [0]",
            "--output",
            "text",
        ],
        dry_run=dry_run,
    )
    policy_path = _put_json_file(policy_document)

    if not policy_arn:
        policy_arn = _aws_text(
            [
                "iam",
                "create-policy",
                "--policy-name",
                policy_name,
                "--policy-document",
                f"file://{policy_path}",
                "--query",
                "Policy.Arn",
                "--output",
                "text",
            ],
            dry_run=dry_run,
            default=f"arn:aws:iam::{account_id}:policy/{policy_name}",
        )
        return policy_arn

    versions = _aws_json(
        ["iam", "list-policy-versions", "--policy-arn", policy_arn, "--output", "json"],
        dry_run=dry_run,
        default={"Versions": []},
    ).get("Versions", [])
    if len(versions) >= 5:
        non_default = [v for v in versions if not v.get("IsDefaultVersion")]
        non_default.sort(key=lambda v: v.get("CreateDate", ""))
        if non_default:
            oldest = non_default[0]["VersionId"]
            _aws(
                ["iam", "delete-policy-version", "--policy-arn", policy_arn, "--version-id", oldest],
                dry_run=dry_run,
                capture=False,
                check=True,
            )

    _aws(
        [
            "iam",
            "create-policy-version",
            "--policy-arn",
            policy_arn,
            "--policy-document",
            f"file://{policy_path}",
            "--set-as-default",
        ],
        dry_run=dry_run,
        capture=False,
        check=True,
    )
    return policy_arn


def _ensure_trust_anchor(
    *,
    trust_anchor_name: str,
    trust_anchor_id: str,
    root_ca_cert: Path,
    region: str,
    account_id: str,
    dry_run: bool,
) -> tuple[str, str]:
    if trust_anchor_id:
        anchor_arn = f"arn:aws:rolesanywhere:{region}:{account_id}:trust-anchor/{trust_anchor_id}"
        return trust_anchor_id, anchor_arn

    existing_id = _aws_text(
        [
            "rolesanywhere",
            "list-trust-anchors",
            "--query",
            f"trustAnchors[?name=='{trust_anchor_name}'].trustAnchorId | [0]",
            "--output",
            "text",
        ],
        dry_run=dry_run,
    )
    if existing_id:
        anchor_arn = f"arn:aws:rolesanywhere:{region}:{account_id}:trust-anchor/{existing_id}"
        return existing_id, anchor_arn

    if dry_run and not root_ca_cert.exists():
        cert_data = "-----BEGIN CERTIFICATE-----\nDRY_RUN_PLACEHOLDER\n-----END CERTIFICATE-----"
    else:
        cert_data = root_ca_cert.read_text(encoding="utf-8")

    source_doc = {
        "sourceType": "CERTIFICATE_BUNDLE",
        "sourceData": {"x509CertificateData": cert_data},
    }
    source_path = _put_json_file(source_doc)
    created_id = _aws_text(
        [
            "rolesanywhere",
            "create-trust-anchor",
            "--name",
            trust_anchor_name,
            "--enabled",
            "--source",
            f"file://{source_path}",
            "--query",
            "trustAnchor.trustAnchorId",
            "--output",
            "text",
        ],
        dry_run=dry_run,
        default="DRY_RUN_TRUST_ANCHOR",
    )
    anchor_arn = f"arn:aws:rolesanywhere:{region}:{account_id}:trust-anchor/{created_id}"
    return created_id, anchor_arn


def _ensure_rolesanywhere_profile(
    *,
    profile_name: str,
    role_arn: str,
    dry_run: bool,
    account_id: str,
    region: str,
) -> str:
    profiles = _aws_json(
        ["rolesanywhere", "list-profiles", "--output", "json"],
        dry_run=dry_run,
        default={"profiles": []},
    ).get("profiles", [])
    existing = next((p for p in profiles if p.get("name") == profile_name), None)

    if existing:
        profile_id = existing["profileId"]
        _aws(
            [
                "rolesanywhere",
                "update-profile",
                "--profile-id",
                profile_id,
                "--enabled",
                "--role-arns",
                role_arn,
            ],
            dry_run=dry_run,
            capture=False,
            check=True,
        )
        return existing.get("profileArn", f"arn:aws:rolesanywhere:{region}:{account_id}:profile/{profile_id}")

    profile_arn = _aws_text(
        [
            "rolesanywhere",
            "create-profile",
            "--name",
            profile_name,
            "--enabled",
            "--role-arns",
            role_arn,
            "--query",
            "profile.profileArn",
            "--output",
            "text",
        ],
        dry_run=dry_run,
        default=f"arn:aws:rolesanywhere:{region}:{account_id}:profile/DRY_RUN_PROFILE",
    )
    return profile_arn


@app.callback(invoke_without_command=True)
def main(
    aws_profile: str = typer.Option("", "--aws-profile", help="AWS shared config profile name (equivalent to AWS_PROFILE)."),
    aws_default_region: str = typer.Option("", "--aws-default-region", help="AWS default region override for AWS CLI calls (equivalent to AWS_DEFAULT_REGION)."),
    region: str = typer.Option("us-east-1", "--region", help="AWS region for Roles Anywhere + CloudFormation."),
    root_ca_cert: Path = typer.Option(Path("~/local-pki/rootCA.pem"), "--root-ca-cert", file_okay=True, dir_okay=False, help="Path to root CA PEM (rootCA.pem). Auto-generated if missing."),
    trust_anchor_name: str = typer.Option("LocalDeploymentAnchor", "--trust-anchor-name"),
    trust_anchor_id: str = typer.Option("", "--trust-anchor-id", help="Reuse an existing trust anchor id instead of creating/finding by name."),
    cfn_execution_role_name: str = typer.Option("CloudFormationExecutionRole", "--cfn-execution-role-name"),
    local_deployer_role_name: str = typer.Option("LocalDeploymentEngineRole", "--local-deployer-role-name"),
    local_deployer_policy_name: str = typer.Option("LocalCloudFormationDeployerPolicy", "--local-deployer-policy-name"),
    rolesanywhere_profile_name: str = typer.Option("LocalDeploymentProfile", "--rolesanywhere-profile-name"),
    aws_profile_snippet_name: str = typer.Option("local-batch-worker", "--aws-profile-snippet-name"),
    attach_admin_access: bool = typer.Option(True, "--attach-admin-access/--no-attach-admin-access", help="Attach AdministratorAccess to CloudFormation execution role."),
    attach_cloudwatch_readonly: bool = typer.Option(True, "--attach-cloudwatch-readonly/--no-attach-cloudwatch-readonly", help="Attach CloudWatchReadOnlyAccess to local deployer role."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print AWS commands without executing."),
) -> None:
    """Create/update AWS IAM + Roles Anywhere resources used by local deploy runners."""
    global _AWS_PROFILE_OVERRIDE
    global _AWS_DEFAULT_REGION_OVERRIDE

    _AWS_PROFILE_OVERRIDE = aws_profile.strip()
    _AWS_DEFAULT_REGION_OVERRIDE = aws_default_region.strip()

    root_ca_cert = _ensure_root_ca_cert(root_ca_cert, dry_run=dry_run)

    account_id = _aws_text(
        ["sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        dry_run=dry_run,
        default="000000000000",
    )

    if _AWS_PROFILE_OVERRIDE:
        info(f"AWS_PROFILE: { _AWS_PROFILE_OVERRIDE }")
    if _AWS_DEFAULT_REGION_OVERRIDE:
        info(f"AWS_DEFAULT_REGION: { _AWS_DEFAULT_REGION_OVERRIDE }")
    info(f"account: {account_id}")
    info(f"region:  {region}")

    anchor_id, anchor_arn = _ensure_trust_anchor(
        trust_anchor_name=trust_anchor_name,
        trust_anchor_id=trust_anchor_id,
        root_ca_cert=root_ca_cert,
        region=region,
        account_id=account_id,
        dry_run=dry_run,
    )
    info(f"trust anchor id:  {anchor_id}")

    cfn_assume_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "cloudformation.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    cfn_role_arn = _ensure_role(
        role_name=cfn_execution_role_name,
        assume_policy=cfn_assume_policy,
        dry_run=dry_run,
    )
    if attach_admin_access:
        _aws(
            [
                "iam",
                "attach-role-policy",
                "--role-name",
                cfn_execution_role_name,
                "--policy-arn",
                "arn:aws:iam::aws:policy/AdministratorAccess",
            ],
            dry_run=dry_run,
            capture=False,
            check=True,
        )

    local_assume_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "rolesanywhere.amazonaws.com"},
                "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
                "Condition": {"ArnEquals": {"aws:SourceArn": anchor_arn}},
            }
        ],
    }
    local_role_arn = _ensure_role(
        role_name=local_deployer_role_name,
        assume_policy=local_assume_policy,
        dry_run=dry_run,
    )

    local_policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "CloudFormationManagement",
                "Effect": "Allow",
                "Action": [
                    "cloudformation:CreateStack",
                    "cloudformation:UpdateStack",
                    "cloudformation:DeleteStack",
                    "cloudformation:DescribeStacks",
                    "cloudformation:DescribeStackEvents",
                    "cloudformation:DescribeStackResources",
                    "cloudformation:ValidateTemplate",
                ],
                "Resource": "*",
            },
            {
                "Sid": "PassRoleToCloudFormation",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": f"arn:aws:iam::{account_id}:role/{cfn_execution_role_name}",
                "Condition": {"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}},
            },
        ],
    }
    local_policy_arn = _ensure_managed_policy(
        policy_name=local_deployer_policy_name,
        policy_document=local_policy_doc,
        dry_run=dry_run,
        account_id=account_id,
    )
    _aws(
        [
            "iam",
            "attach-role-policy",
            "--role-name",
            local_deployer_role_name,
            "--policy-arn",
            local_policy_arn,
        ],
        dry_run=dry_run,
        capture=False,
        check=True,
    )
    if attach_cloudwatch_readonly:
        _aws(
            [
                "iam",
                "attach-role-policy",
                "--role-name",
                local_deployer_role_name,
                "--policy-arn",
                "arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess",
            ],
            dry_run=dry_run,
            capture=False,
            check=True,
        )

    profile_arn = _ensure_rolesanywhere_profile(
        profile_name=rolesanywhere_profile_name,
        role_arn=local_role_arn,
        dry_run=dry_run,
        account_id=account_id,
        region=region,
    )

    info("")
    info("Setup complete.")
    info(f"Trust anchor ARN:    {anchor_arn}")
    info(f"Profile ARN:         {profile_arn}")
    info(f"Local role ARN:      {local_role_arn}")
    info(f"CFN execution role:  {cfn_role_arn}")
    info("")
    info("Add this to ~/.aws/config on the runner machine:")
    info(f"[profile {aws_profile_snippet_name}]")
    info(f"region = {region}")
    info(
        "credential_process = /usr/local/bin/aws_signing_helper credential-process "
        "--certificate /absolute/path/to/deployment_engine.crt "
        "--private-key /absolute/path/to/deployment_engine.key "
        f"--trust-anchor-arn {anchor_arn} "
        f"--profile-arn {profile_arn} "
        f"--role-arn {local_role_arn}"
    )
