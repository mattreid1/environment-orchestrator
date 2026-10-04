# Shared AWS access for AIME

AWS CLI v2 is installed in SWE and frontend guest images. Authentication is
available only to workspaces bound to AIME (`8744dbdb-cdd7-4fee-8b46-3bd6ae6705fe`)
with an allocated `swe` or `frontend` profile. Knowledge profiles, other companies,
and unbound standalone workspaces cannot obtain credentials from this service.
The CLI executable itself is a common image package and is not a company boundary.

The default region is Canada Central (`ca-central-1`). The intended AWS role has
`AdministratorAccess`, including IAM management. This is a default region, not a
restriction on access to other regions. Organization policies and AWS root-only
operations still constrain an administrator role.

## Authenticate once on the host

Create a dedicated IAM user that can only assume the AIME administrator role.
The role trusts that user and has `arn:aws:iam::aws:policy/AdministratorAccess`.
The example CloudFormation template is `docs/aime-aws-iam.json`. It creates no
root access keys and no access keys in stack outputs. Create an access key for
the dedicated user, then enter it through hidden prompts on mbp-agent:

```sh
environment-aws setup --role-arn arn:aws:iam::YOUR_ACCOUNT_ID:role/AIMEAgentsAdmin
environment-aws check
```

Setup verifies that the source identity is an IAM user in the configured account
and that the role can be assumed. Source keys are kept in the owned mode-0600
host file `~/.config/environment-orchestrator/aws.json`, outside Git, Nix,
Paperclip, and guests. Root keys are rejected. New managed Codex and Claude runs
automatically provision the guest configuration; individual agent setup is not
required. The launcher writes the managed default profile in the guest's
`~/.aws/config`.

In an authorized guest:

```sh
aws --version
aws sts get-caller-identity
aws configure get region
```

## Credential boundary

The host `environment-aws` service listens on port 6092. The Nix firewall opens
that port only on workspace TAP interfaces. The guest's AWS `credential_process`
contacts its host-side gateway directly, without environment HTTP proxies.

Each request checks a private random workspace capability, the source guest IP,
the live Paperclip company binding and agent availability, and the allocated
workspace profile. Company and profile claims supplied by a caller are rejected.
The capability rotates on each managed launch. No request selects a role,
account, endpoint, command, or host path. Only after authorization does the host
call STS AssumeRole for the fixed configured role, returning one-hour temporary
credentials. The CLI can fetch new credentials for later commands without
human login. AWS errors are redacted and credential responses are not logged.

Authorized guest code can read its temporary credentials and use them outside
the CLI. Removing access blocks future issuance but does not invalidate already
issued credentials; they expire within an hour. The role can create or change
IAM identities because administrator access was explicitly requested. Host
operators running as `agent` can change the binding/configuration; the host is
the trusted boundary, not a mutually untrusted tenant platform.

CLI results can enter the agent's inference context. A VM snapshot can contain
temporary credentials. Keep workspace disks, snapshots, and host configuration
private. Deleting the source IAM access key stops new role issuance. Use AWS
role-session revocation when existing sessions must also be invalidated.
