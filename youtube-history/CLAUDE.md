# CLAUDE.md — Project Context & Workflow

## Project Overview

This file exists to **onboard Claude into this codebase** at the start of every session. Only essential, universal context is included here.

- **Name:** youtube-history
- **Purpose:** Terraform module that provisions all Confluent Cloud and AWS infrastructure for a YouTube watch history Kafka + Flink pipeline (topics, Avro schemas, service accounts, API keys, Secrets Manager secrets, IRSA role for External Secrets Operator).
- **Tech stack:** Terraform >= 1.9, Confluent Cloud (Kafka + Schema Registry), AWS (Secrets Manager, IAM), `confluentinc/confluent ~> 2.0`, `hashicorp/aws ~> 5.0`

## Commands

```bash
cd terraform/
terraform fmt -recursive                # format all .tf files
terraform init                          # initialize providers and modules
terraform validate                      # syntax + type check (no credentials needed)
terraform plan -out=tfplan              # preview changes
terraform apply tfplan                  # apply previewed plan
terraform output                        # show outputs (secret ARNs, bootstrap servers, SR URL)
```

> Requires a populated `terraform/terraform.tfvars` — see Environment Setup below.

## Architecture

```
terraform/
├── main.tf        # provider config; managed CC env, Kafka cluster, SR cluster resources; Terraform SR service account
├── variables.tf   # all input variables
├── outputs.tf     # SM secret ARNs, ESO IAM role ARN, bootstrap servers, SR URL
├── topics.tf      # 4 confluent_kafka_topic resources
├── schemas.tf     # 4 confluent_schema resources (registered via tf_sr service account)
├── schemas/       # .avsc files: RawWatchEvent, VideoMetadata, EnrichedWatchEvent, MusicWatchCount
├── iam.tf         # Confluent service accounts, API keys, Kafka ACLs, SR RBAC; AWS IAM role + policy for ESO IRSA
└── secrets.tf     # 4 aws_secretsmanager_secret resources: producer, enricher, counter, youtube_api_key
```

The Confluent Cloud environment, Kafka cluster (Basic, single-zone, AWS), and Schema Registry cluster (ESSENTIALS) are all **created by Terraform** as managed resources.

## Environment Setup

Create `terraform/terraform.tfvars` (gitignored):

```hcl
confluent_cloud_api_key    = "..."   # Cloud-level API key (not Kafka-level)
confluent_cloud_api_secret = "..."
youtube_api_key            = "..."   # YouTube Data API v3 key
# confluent_environment_name   defaults to "yt-history"
# confluent_kafka_cluster_name defaults to "yt-kafka"
# aws_region                   defaults to "us-east-1"
# eks_oidc_issuer              defaults to placeholder — update before deploying ESO
# eso_k8s_namespace            defaults to "external-secrets"
# eso_k8s_service_account      defaults to "external-secrets"
```

AWS credentials must be configured separately (`aws configure` or env vars) — Terraform uses the standard credential chain.

## Key References

- **Spec:** `docs/superpowers/specs/2026-04-25-youtube-watch-history-kafka-flink-design.md` (Section 7 covers this module)
- **Plan:** `docs/superpowers/plans/2026-05-08-youtube-history-terraform.md`

## How Claude Should Work in This Project

Before doing work, Claude should follow this **standard workflow**:

### 1) Explore & Plan
- Investigate the relevant area of the codebase.
- Ask clarifying questions before writing or modifying code.
- Construct a short plan with steps before coding anything.

### 2) Code with Verification
- Implement minimal, necessary changes to solve the task.
- Run validation checks locally or via CLI commands.
- Apply security and defensive programming practices.

### 3) Pre-PR Review
- **MANDATORY: Run the `superpowers:requesting-code-review` skill before creating any PR.** This skill verifies work meets requirements and catches issues before merge.
- Verify all relevant documentation has been updated.
- Ensure PR description is accurate.
- Confirm branch naming follows `feature-<id>/` or `fix-<id>/` format.

### 4) Commit & Document
- Write a commit message that clearly states intent and outcome.
- Update any reference docs if you add new relevant context or conventions.

## Gotchas

- **Schema writes are Terraform-only** — pipeline service accounts have read-only SR access. Schema changes must go through `terraform apply`, not runtime.

## Constraints and Policies

These rules apply *in every session*:

### Design Specs — Local Only

Design specs (brainstorming outputs, implementation specs) written to `docs/superpowers/specs/` are **local working documents only** and must NEVER be committed to git. They are gitignored. Do not stage or commit any file under `docs/superpowers/`.

### Progressive Disclosure Policy

Only load task-specific docs when needed. This file is intentionally concise — reference external docs rather than duplicating their content here.

### Universal Work Rules

- Ask for clarification if requirements are unclear.
- Avoid doing anything that requires guessing missing context.
- If you need more context, ask for the relevant doc to read.
- Optimize for long-term maintainability over short-term ingenuity.

### Security — MUST FOLLOW

- NEVER expose API keys or tokens.
- ALWAYS manage secrets externally (not committed to this repository).
- NEVER commit `.env` or credential files.
- NEVER store secrets in plain text in committed files.

### Code Quality

- Apply defensive programming practices.
- Ensure idempotency where applicable — operations should be safe to re-run.
- Run `terraform validate` and `terraform fmt -check` before committing.

### Dependencies

- Minimize external dependencies where it makes sense.
- Always ask before importing any new external dependencies.

## Repository Etiquette

### Branching
- **RECOMMENDED: Use worktree isolation for feature work** — use `claude --worktree` or manual git worktrees.
- All branches must be associated with a GitHub Issue.
- Never commit directly to `main`.
- Branch names must follow format: `feature-<github-issue-id>/<description>` or `fix-<github-issue-id>/<description>`.

### Worktree Isolation — Recommended Practice

**Worktree isolation is strongly recommended for feature work.**

#### Using `claude --worktree` (Recommended)

```bash
claude --worktree                      # auto-creates isolated worktree
claude --worktree --name my-feature   # optionally name it
```

Rename the auto-generated branch early to follow convention (worktree sessions only):

```bash
git branch -m feature-<github-issue-id>/<feature-name>
```

#### Using git worktree (Manual Alternative)

```bash
git worktree add -b feature-123/my-feature ../youtube-history-my-feature
git worktree list
git worktree remove ../youtube-history-my-feature   # after PR merged
```

### Commits
- Write clear commit messages describing the changes.
- Keep commits focused on single changes.
- Avoid bleeding multiple streams of changes into a single commit.
- Run `git` commands directly — the session working directory is the repo root, so `git -C <path>` is never needed.

### Pull Requests
- Create PRs for all changes to `main`.
- NEVER force push to `main`.
- PR description must accurately reflect implementation.
- Include description of WHAT changed and WHY.
- Include explicit markdown link to GitHub Issue.

### GitHub Issues

Project status is tracked through GitHub Issues. Interaction with GitHub Issues and Pull Requests locally can be performed via the GitHub CLI.
