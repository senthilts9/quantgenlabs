# Deploying

The live host is an EC2 instance running **docker compose** — not Kubernetes.
`deploy/k8s/` is a reference design that has never been applied; see the note in
[`deployment.md`](deployment.md).

## One command

```bash
./scripts/deploy.sh --dry-run   # what would change; touches nothing
./scripts/deploy.sh             # deploy
```

Or in CI: push to `main`. The `deploy` workflow runs **only after `ci` reports
success**, or manually via Actions → deploy → Run workflow.

## What it does, and why each step is there

| Step | Why |
|---|---|
| Preflight | Refuses to start if the server's `.env` is missing — the stack will not boot without it, and finding that out after the sync means a broken site plus a rollback. |
| Backup source + OMS volume | Timestamped into `~/backups/`. This is the step a manual deploy skips. |
| rsync `--delete` | Removes files deleted from the repo. The excludes are listed in the script with a reason each; the two that matter are `.env` (live secrets, not in the repo) and `*.so` (macOS binaries — the image builds its own for linux-x86_64). |
| `docker compose up -d --build` | Builds first. Compose only recreates containers once the build succeeds, so a broken build leaves the current version serving. |
| Restart Prometheus + Grafana | They read config from disk at start. A synced config change does nothing until they restart — this was missed on the first deploy and Prometheus took a 307 on every scrape for a day. |
| Health gate | Four endpoints, retried for two minutes: health, crypto desk, reference data, connectivity. |
| **Automatic rollback** | If the gate fails, the previous version is restored and rebuilt before the script exits. A deploy that leaves the site broken while someone reads the logs is worse than one that never happened. The failed tree is kept at `~/quantgenlabs.failed`. |

## First-time CI setup

The workflow needs one secret and three variables. **Add them yourself — do not
paste the private key into a chat or a file in this repo.**

```bash
# Secret: the private key, read straight from disk into the secret
gh secret set DEPLOY_SSH_KEY --repo senthilts9/quantgenlabs \
  < ~/.ssh/quantgenlabs-deploy-key

# Variables
gh variable set DEPLOY_HOST --repo senthilts9/quantgenlabs --body "15.135.53.112"
gh variable set DEPLOY_USER --repo senthilts9/quantgenlabs --body "ec2-user"
gh variable set DEPLOY_SITE --repo senthilts9/quantgenlabs --body "https://quantgenlabs.com"

# The pinned host key. The workflow REFUSES to deploy without this rather than
# falling back to StrictHostKeyChecking=no: accepting any key means a deploy
# would push this platform's source and secrets to whatever answers on that
# address.
gh variable set DEPLOY_HOST_KEY --repo senthilts9/quantgenlabs \
  --body "$(ssh-keyscan -t ed25519 15.135.53.112 2>/dev/null)"
```

Expected fingerprint for that host, to check against what `ssh-keyscan` returns:

```
256 SHA256:04ciI1vbDWHnxIhNrfUPeZjgcEbJ8dz8stp6mFvt0xI (ED25519)
```

## Rolling back by hand

```bash
ssh -i ~/.ssh/quantgenlabs-deploy-key ec2-user@15.135.53.112
ls ~/backups/                      # pick a qgl-src-<stamp>.tar.gz
cd ~ && mv quantgenlabs quantgenlabs.bad && tar xzf backups/qgl-src-<stamp>.tar.gz
cd quantgenlabs && docker compose up -d --build
```

The OMS volume is backed up separately as `oms-vol-<stamp>.tar.gz`; restoring the
source does not restore it, because rolling back code and silently rolling back
the trade blotter are very different decisions.
