# youtube-history

A Kafka + Flink pipeline that processes a Google Takeout `watch-history.html` file, enriches each video entry via the YouTube Data API v3, classifies music videos, and maintains a running all-time watch count queryable by Grafana.

## Architecture

```
watch-history.html
      |
      v
Python Producer
  Parses HTML, produces one Avro RawWatchEvent per entry
  to yt.raw.watch.events (compacted, keyed, idempotent)
      |
      v                         Confluent Cloud
Flink Job 1 -- Enricher + Classifier
  Reads yt.raw.watch.events (unbounded)
  Bootstraps from yt.video.metadata (bounded, at startup)
  Calls YouTube Data API v3 in batches of 50
  Classifies music vs. non-music per video
  Writes to yt.enriched.watch.events and yt.video.metadata
      |
      v
Flink Job 2 -- Counter
  Reads yt.enriched.watch.events
  Aggregates watch count per music video
  Writes to yt.music.watch.counts (Kafka) and PostgreSQL
      |
      v
Grafana
  Queries PostgreSQL for top music videos and search
```

## Topics

| Topic | Type | Retention | Purpose |
|---|---|---|---|
| `yt.raw.watch.events` | Compacted | Infinite | Canonical deduped watch history |
| `yt.video.metadata` | Compacted | Infinite | Unified fact table: API data + music classification |
| `yt.enriched.watch.events` | Compacted | Infinite | API-enriched + classified watch events |
| `yt.music.watch.counts` | Compacted | Infinite | All-time per-video music watch counts |
| `yt.raw.watch.events.dlq` | Delete | 30 days | Dead letter queue: unenrichable events (deleted/private/region-restricted videos) |

## Requirements

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.9
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager for the producer)
- [Java 21 JDK](https://adoptium.net/) (for building Flink jobs)
- [Maven 3.9+](https://maven.apache.org/download.cgi) (for building the Flink multi-module project)
- [Docker](https://docs.docker.com/get-docker/) (for building the custom Flink image)
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) configured with credentials (`aws configure`)
- A Confluent Cloud account with an existing environment and Kafka cluster
- A Confluent Cloud Schema Registry cluster associated with that environment
- A Confluent Cloud Cloud-level API key (Settings > API keys > Cloud resource management scope)
- A YouTube Data API v3 key
- An EKS cluster with an OIDC provider (for External Secrets Operator IRSA)
- Confluent Platform for Kubernetes Flink operator installed on the cluster (for the `cmf.confluent.io/v1` CRD)
- External Secrets Operator with a `ClusterSecretStore` named `aws-secrets-manager`
- MinIO or S3-compatible storage for Flink checkpoint state

## Setup

### Pre-step: Create a Confluent Cloud API key for Terraform

Terraform authenticates to Confluent Cloud using a Cloud-level API key (not a Kafka-level key). This must be created manually once before running Terraform.

1. In Confluent Cloud, navigate to **Settings > API keys > Add key**
2. Select scope **Cloud resource management** (not "Granular access" / Kafka cluster)
3. Save the key ID and secret — these become `confluent_cloud_api_key` and `confluent_cloud_api_secret` in `terraform.tfvars`

Everything else — service accounts, Kafka API keys, Schema Registry API keys, ACLs, and AWS Secrets Manager secrets — is provisioned by Terraform with least-privilege access. You do not need to create any additional API keys manually.

### Step 1: Provision infrastructure with Terraform

The `terraform/` directory provisions all Confluent Cloud and AWS resources required by the pipeline.

#### 1.1 Configure variables

Copy the example file and fill in the three required values:

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Open `terraform/terraform.tfvars` and set:

| Variable | Where to find it |
|----------|-----------------|
| `confluent_cloud_api_key` | Confluent Cloud → Settings → API keys (Cloud resource management scope) |
| `confluent_cloud_api_secret` | Saved when you created the key in the Pre-step |
| `youtube_api_key` | Google Cloud Console → APIs & Services → Credentials |
| `common_tags` | Set all keys including `cflt_keep_until` (expiry date, e.g. `"2027-01-01"`). Must be provided as a complete map — see `terraform.tfvars.example` |

Optional overrides (only set if you need to change them):

```hcl
# confluent_environment_name   = "yt-history"
# confluent_kafka_cluster_name = "yt-kafka"
# aws_region                   = "us-east-1"
# eks_oidc_issuer              = "placeholder.example.com/id/PLACEHOLDER"
# eso_k8s_namespace            = "external-secrets"
# eso_k8s_service_account      = "external-secrets"
```

`common_tags` must be overridden as a complete map — partial overrides will drop the omitted keys. The full set of keys is shown in `terraform.tfvars.example`.

> `terraform.tfvars` is gitignored and will never be committed.

#### 1.2 Initialize and apply

```bash
cd terraform/
terraform init
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
```

#### 1.3 Capture outputs

After a successful apply, capture the outputs for use in later steps:

```bash
terraform output -json
```

Key outputs:

| Output | Used by |
|---|---|
| `sm_secret_arns.producer` | GitOps ExternalSecret CR for the Python producer |
| `sm_secret_arns.enricher` | GitOps ExternalSecret CR for the Flink enricher job |
| `sm_secret_arns.counter` | GitOps ExternalSecret CR for the Flink counter job |
| `sm_secret_arns.youtube_api_key` | GitOps ExternalSecret CR for the Flink enricher job |
| `eso_iam_role_arn` | IRSA annotation on the External Secrets Operator ServiceAccount |
| `bootstrap_servers` | Kubernetes ConfigMap for all pipeline components |
| `schema_registry_url` | Kubernetes ConfigMap for all pipeline components |

### Step 2: Run the Python producer

The producer reads your Google Takeout `watch-history.html` and produces one Avro `RawWatchEvent` per entry to `yt.raw.watch.events`. Credentials are fetched from AWS Secrets Manager at startup — Terraform already populated these in Step 1.

#### 2.1 Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed
- AWS CLI configured with credentials that can read from Secrets Manager (`aws sts get-caller-identity` should succeed)
- Step 1 complete (Terraform applied, SM secrets populated)
- Your Google Takeout `watch-history.html` file available locally

#### 2.2 Install dependencies

```bash
cd producer/
uv sync
```

#### 2.3 Configure environment

Copy the example env file and fill in the values from `terraform output`:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# From: terraform output bootstrap_servers
BOOTSTRAP_SERVERS=pkc-xxxxx.us-east-1.aws.confluent.cloud:9092

# From: terraform output schema_registry_url
SCHEMA_REGISTRY_URL=https://psrc-xxxxx.us-east-2.aws.confluent.cloud

# Path to your Google Takeout watch history file
INPUT_FILE=./watch-history.html

# Secret path — matches what Terraform created; leave as default
SM_SECRET_PATH=/yt-pipeline/confluent/producer
AWS_REGION=us-east-1
```

> `.env` is gitignored. Never commit it.

#### 2.4 Place your watch history file

Copy or move your Google Takeout file into the producer directory:

```bash
cp /path/to/Takeout/YouTube\ and\ YouTube\ Music/history/watch-history.html producer/watch-history.html
```

#### 2.5 Run the producer

```bash
cd producer/
uv run python main.py
```

Expected output (last lines):

```
... INFO Producing 22761 events to yt.raw.watch.events
... INFO Done. 22761 events produced.
```

Verify in the Confluent Cloud Console that `yt.raw.watch.events` contains messages with Base64-encoded keys.

#### 2.6 What Terraform provisioned for the producer

The producer runs as service account `sa-yt-producer` with the following least-privilege grants, all created automatically by Terraform:

| Resource | Permission | Purpose |
|---|---|---|
| `yt.raw.watch.events` | WRITE, DESCRIBE | Produce and resolve topic metadata |
| Schema Registry (all subjects) | DeveloperRead | Look up existing schema IDs |

The Kafka API key and Schema Registry API key for this service account are stored in AWS Secrets Manager at `/yt-pipeline/confluent/producer` by Terraform. The producer never holds credentials in plaintext on disk.

### Step 3: Build and deploy the Flink enricher job

The enricher job (`flink/yt-enricher/`) reads `yt.raw.watch.events`, calls the YouTube Data API in batches of 50, classifies music videos, and writes to `yt.enriched.watch.events` and `yt.video.metadata`. It runs on a Kubernetes cluster managed by the Confluent Platform for Kubernetes (CPK) Flink operator.

#### 3.1 Prerequisites

- **Java 21 JDK** — required to compile the Maven project
- **Maven 3.9+** — builds the multi-module project (`flink/pom.xml`)
- **Docker** — builds the custom Flink image
- A container registry you can push to (default image: `quay.io/osowski/yt-enricher:latest`)
- A Kubernetes cluster with:
  - [Confluent Platform for Kubernetes](https://docs.confluent.io/operator/current/co-deploy-flink.html) Flink operator installed (provides the `cmf.confluent.io/v1` CRD)
  - [External Secrets Operator](https://external-secrets.io/) installed with a `ClusterSecretStore` named `aws-secrets-manager` pointing to the same AWS region used in Step 1
  - Argo CD installed (optional — you can apply manifests manually instead)
  - MinIO or S3-compatible storage for Flink checkpoints

#### 3.2 Build the fat JAR

```bash
cd flink/
mvn clean package -pl common,yt-enricher --also-make
```

The shaded JAR is written to `flink/yt-enricher/target/yt-enricher-1.0-SNAPSHOT.jar`. The build generates Avro classes from `.avsc` files in `flink/common/src/main/avro/` and bundles all runtime dependencies except Flink itself (which is provided by the base image).

#### 3.3 Build and push the Docker image

```bash
# Run from the repo root so the COPY path resolves correctly
docker build -t quay.io/osowski/yt-enricher:latest -f flink/Dockerfile flink/
docker push quay.io/osowski/yt-enricher:latest
```

The `Dockerfile` is at `flink/Dockerfile` and is based on `confluentinc/cp-flink:2.1.1-cp2-java21`.

> If you use a different registry or tag, update the `spec.image` field in `flink/k8s/yt-enricher-app.yaml` before deploying.

#### 3.4 Fill in ConfigMap placeholders

`gitops/enricher/configmap.yaml` holds non-secret endpoint config. Replace both values with the outputs from Step 1:

```bash
# Get the values
terraform -chdir=terraform output -raw bootstrap_servers
terraform -chdir=terraform output -raw schema_registry_url
```

Edit `gitops/enricher/configmap.yaml`:

```yaml
data:
  BOOTSTRAP_SERVERS: "pkc-xxxxx.us-east-1.aws.confluent.cloud:9092"
  SCHEMA_REGISTRY_URL: "https://psrc-xxxxx.us-east-2.aws.confluent.cloud"
```

#### 3.5 Fill in FlinkApplication checkpoint placeholders

`flink/k8s/yt-enricher-app.yaml` has three `REPLACE-*` placeholders for the MinIO/S3 endpoint. Edit the file and set:

| Placeholder | Value |
|---|---|
| `REPLACE-MINIO-BUCKET` | MinIO bucket name for Flink state (e.g. `flink-checkpoints`) |
| `REPLACE-MINIO-SERVICE` | MinIO Kubernetes Service name |
| `REPLACE-NAMESPACE` | Kubernetes namespace where MinIO runs |

MinIO credentials are **not** stored in the manifest. They are pulled from AWS Secrets Manager via the `yt-minio-creds` ExternalSecret (deployed alongside the job in `gitops/enricher/`). Before deploying, store the credentials in Secrets Manager:

```bash
aws secretsmanager create-secret \
  --name /yt-pipeline/minio/credentials \
  --secret-string '{"access_key":"<your-minio-access-key>","secret_key":"<your-minio-secret-key>"}'
```

The External Secrets Operator will inject them as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` environment variables, which the Flink S3 plugin picks up automatically via the AWS SDK credential chain.

#### 3.6 Deploy via Argo CD (recommended)

Apply the two Argo CD `Application` resources once. Argo CD will sync the enricher config (ExternalSecrets + ConfigMap) in wave 0 before starting the FlinkApplication in wave 1:

```bash
kubectl apply -f gitops/argo/yt-enricher-application.yaml
```

Verify the Applications become `Healthy` and `Synced`:

```bash
kubectl get applications -n argocd yt-enricher-config yt-enricher-job
```

#### 3.7 Deploy manually (alternative)

If you are not using Argo CD, apply the manifests in order:

```bash
# 1. ExternalSecrets + ConfigMap (must be ready before the job starts)
kubectl apply -f gitops/enricher/

# 2. Wait for ExternalSecrets to pull credentials from Secrets Manager
kubectl wait --for=condition=Ready externalsecret -n flink --all --timeout=120s

# 3. FlinkApplication
kubectl apply -f flink/k8s/yt-enricher-app.yaml
```

#### 3.8 Design note: enricher starts from earliest offset

The Flink enricher is configured with `OffsetsInitializer.earliest()`, meaning it reads `yt.raw.watch.events` from offset 0 on first deployment (when no checkpoint exists). This is intentional for the following reasons:

- **All topics are fact tables.** `yt.raw.watch.events` is compacted and keyed by video ID + timestamp; replaying it is idempotent. `yt.video.metadata` is also compacted; re-emitting known video metadata is safe.
- **The metadata cache eliminates redundant API calls.** At startup, `MetadataBootstrap` pre-loads all entries from `yt.video.metadata` into an in-memory cache. Any video already enriched in a prior run is served from the cache without hitting the YouTube Data API v3, preserving quota.
- **Watch history is bounded and historical.** Google Takeout exports are finite snapshots; the full replay completes in a single bounded pass. Subsequent runs process only new events from the offset where the previous checkpoint left off.

If you need to replay from a specific point (e.g., after schema changes), delete the Flink checkpoint from MinIO and redeploy. The job will replay from offset 0 and rebuild the enrichment state.

#### 3.9 What Terraform provisioned for the enricher

Terraform created service account `sa-yt-enricher` with the following least-privilege grants:

| Resource | Permission | Purpose |
|---|---|---|
| `yt.raw.watch.events` | READ, DESCRIBE | Consume raw watch events |
| `yt.video.metadata` | READ, WRITE, DESCRIBE | Bootstrap cached metadata at startup; write new metadata |
| `yt.enriched.watch.events` | WRITE, DESCRIBE | Produce enriched events |
| `yt.raw.watch.events.dlq` | WRITE, DESCRIBE | Produce unenrichable events to dead letter queue |
| Schema Registry (all subjects) | DeveloperRead | Look up Avro schema IDs at runtime |

The Kafka API key and Schema Registry API key for `sa-yt-enricher` are stored in AWS Secrets Manager at `/yt-pipeline/confluent/enricher`. The YouTube API key is stored at `/yt-pipeline/youtube/api-key`. External Secrets Operator pulls both into Kubernetes Secrets in the `flink` namespace — the job never holds credentials in plaintext on disk.

<!-- Step 4: Flink counter deployment (coming soon) -->
<!-- Step 5: PostgreSQL schema and Grafana setup (coming soon) -->
