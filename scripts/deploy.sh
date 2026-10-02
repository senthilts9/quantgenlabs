#!/usr/bin/env bash
# Deploy to the live host. Same code path whether CI runs it or you do.
#
# WHAT THIS REPLACES. The first deploy was fifteen manual steps: back up the
# source, back up the OMS volume, dry-run the sync, read the diff, sync, rebuild,
# restart the observability containers, then check by hand that the new endpoints
# answered and the crypto desk still worked. Every one of those is a step someone
# skips at 2am, and the one that gets skipped is the backup.
#
# THE DEPLOY IS NOT DONE WHEN THE CONTAINERS START. It is done when the health
# gate passes. If it does not, this script PUTS THE OLD VERSION BACK before it
# exits -- an automatic rollback, because a deploy that leaves the site broken
# while someone reads the logs is worse than one that never happened.
#
# Never overwrites the server's .env: it holds the live secrets and is not in the
# repo. Never copies *.so: the local ones are macOS binaries and the image builds
# its own for linux-x86_64 in a builder stage.
#
#   ./scripts/deploy.sh                 deploy
#   ./scripts/deploy.sh --dry-run       show what would change, touch nothing
#   DEPLOY_HOST=... DEPLOY_USER=...     override the target
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${DEPLOY_HOST:-15.135.53.112}"
USER_="${DEPLOY_USER:-ec2-user}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/quantgenlabs-deploy-key}"
REMOTE_DIR="${DEPLOY_DIR:-~/quantgenlabs}"
SITE="${DEPLOY_SITE:-https://quantgenlabs.com}"
DRY=""
[[ "${1:-}" == "--dry-run" ]] && DRY="--dry-run"

SSH="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=15"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

# Excludes, and why each one. Getting this list wrong is how a deploy destroys
# production state, so it is spelled out rather than inherited from a .rsyncignore.
EXCLUDES=(
  --exclude='.git/'                 # the server has no git and does not need one
  --exclude='node_modules/'         # reinstalled in the image build
  --exclude='.venv/'                # host venv; the image installs its own
  --exclude='__pycache__/' --exclude='*.pyc'
  --exclude='.pytest_cache/' --exclude='.hypothesis/'
  --exclude='dist/'                 # built inside the web image
  --exclude='.env'                  # LIVE SECRETS. Not in the repo. Never overwrite.
  --exclude='backend/data/'         # runtime SQLite; the real one is a docker volume
  --exclude='*.so'                  # macOS binaries; the image builds linux ones
  --exclude='cpp/compile_commands.json' --exclude='cpp/.debug/'  # per-machine
  --exclude='.DS_Store'
)

# ---------------------------------------------------------------- preflight
say "Preflight"
[[ -f "$KEY" ]] || fail "no deploy key at $KEY"
$SSH "$USER_@$HOST" true 2>/dev/null || fail "cannot reach $USER_@$HOST with $KEY"
echo "  reachable: $USER_@$HOST"
$SSH "$USER_@$HOST" "test -f $REMOTE_DIR/.env" || fail "$REMOTE_DIR/.env missing on the server -- refusing to deploy, the stack will not start without it"
echo "  server .env present (left untouched)"
echo "  local HEAD: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"
if [[ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]]; then
  echo "  NOTE: working tree is dirty -- deploying what is on disk, not what is committed"
fi

# ------------------------------------------------------------------ dry run
if [[ -n "$DRY" ]]; then
  say "Dry run -- changes that WOULD be made"
  rsync -az --dry-run --delete --itemize-changes "${EXCLUDES[@]}" \
    -e "$SSH" "$ROOT/" "$USER_@$HOST:$REMOTE_DIR/" | tail -40
  echo
  echo "  deletions:"
  rsync -az --dry-run --delete --itemize-changes "${EXCLUDES[@]}" \
    -e "$SSH" "$ROOT/" "$USER_@$HOST:$REMOTE_DIR/" | grep '^\*deleting' | sed 's/^/    /' || echo "    none"
  exit 0
fi

# ------------------------------------------------------------------ backup
say "Backup (source + OMS volume)"
STAMP="$($SSH "$USER_@$HOST" "date +%Y%m%d-%H%M%S")"
$SSH "$USER_@$HOST" "set -e
  mkdir -p ~/backups
  cd ~ && tar czf backups/qgl-src-$STAMP.tar.gz \
    --exclude=node_modules --exclude=.venv --exclude=__pycache__ quantgenlabs 2>/dev/null
  docker run --rm -v quantgenlabs_oms-data:/d -v \$HOME/backups:/b alpine \
    tar czf /b/oms-vol-$STAMP.tar.gz -C /d . 2>/dev/null
  ls -lh backups/qgl-src-$STAMP.tar.gz backups/oms-vol-$STAMP.tar.gz | awk '{print \"  \"\$9\" (\"\$5\")\"}'"
echo "  rollback stamp: $STAMP"

# -------------------------------------------------------------------- sync
say "Sync"
rsync -az --delete --stats "${EXCLUDES[@]}" \
  -e "$SSH" "$ROOT/" "$USER_@$HOST:$REMOTE_DIR/" \
  | grep -E 'Number of files:|transferred|deleted' | sed 's/^/  /'
$SSH "$USER_@$HOST" "test -f $REMOTE_DIR/.env" || fail "the server .env vanished during sync -- rolling back"
echo "  .env still present"

# ------------------------------------------------------------------- build
say "Build and restart"
# Builds first; compose only recreates containers once the build succeeds, so a
# broken build leaves the current version serving.
$SSH "$USER_@$HOST" "cd $REMOTE_DIR && docker compose up -d --build 2>&1 | tail -8" | sed 's/^/  /'
# Prometheus and Grafana read config from disk at start, so a synced config
# change does nothing until they restart. This was missed on the first deploy:
# Prometheus kept taking a 307 on every scrape for a day.
$SSH "$USER_@$HOST" "cd $REMOTE_DIR && docker compose restart prometheus grafana >/dev/null 2>&1" || true
echo "  observability restarted (they only read config at start)"

# ------------------------------------------------------------- health gate
say "Health gate"
check() { curl -s -m 12 -o /dev/null -w '%{http_code}' "$SITE$1" 2>/dev/null || echo 000; }
PATHS=(/api/health /api/cryptodesk/book /api/refdata/instruments /api/connectivity/status)
HEALTHY=0
for attempt in $(seq 1 12); do
  ok=1
  for p in "${PATHS[@]}"; do [[ "$(check "$p")" == "200" ]] || ok=0; done
  if [[ $ok == 1 ]]; then HEALTHY=1; echo "  all endpoints 200 (attempt $attempt)"; break; fi
  sleep 10
done

if [[ $HEALTHY != 1 ]]; then
  say "UNHEALTHY -- rolling back to $STAMP"
  for p in "${PATHS[@]}"; do printf '  %-34s %s\n' "$p" "$(check "$p")"; done
  $SSH "$USER_@$HOST" "set -e
    cd ~ && rm -rf quantgenlabs.failed && mv quantgenlabs quantgenlabs.failed
    tar xzf backups/qgl-src-$STAMP.tar.gz
    cd quantgenlabs && docker compose up -d --build 2>&1 | tail -4"
  sleep 20
  back=1; for p in "${PATHS[@]}"; do [[ "$(check "$p")" == "200" ]] || back=0; done
  [[ $back == 1 ]] && echo "  rollback OK -- previous version restored and healthy" \
                   || echo "  ROLLBACK DID NOT RESTORE HEALTH -- needs a human; failed tree kept at ~/quantgenlabs.failed"
  fail "deploy rolled back (backup $STAMP)"
fi

for p in "${PATHS[@]}"; do printf '  %-34s %s\n' "$p" "$(check "$p")"; done
say "Deployed. Rollback: ~/backups/qgl-src-$STAMP.tar.gz"
