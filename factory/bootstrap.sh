#!/usr/bin/env bash
# Set up the Marden build workspace next to this factory folder.
#
#   ./bootstrap.sh <handoff_dir> <marden_code.zip> <marden_data.zip>
#
# Creates (beside factory/):
#   marden/          the product repo: handoff/ (specs, screens, contracts) + legacy/ (existing code), on branch main
#   marden-seed/     legacy code + data unzipped together, read-only for agents (export MARDEN_SEED_DIR to this)
#   wt/              per-task worktrees (created by the factory)
# and writes factory/tasks.json from the handoff and the legacy code.
set -euo pipefail
HANDOFF=${1:?handoff dir}; CODE=${2:?code zip}; DATA=${3:?data zip}
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
REPO="$ROOT/marden"; SEED="$ROOT/marden-seed"

[ -e "$REPO" ] && { echo "$REPO already exists; move it aside first"; exit 1; }
mkdir -p "$REPO/handoff" "$REPO/legacy" "$SEED"
cp -R "$HANDOFF"/. "$REPO/handoff/"
unzip -q "$CODE" -d "$REPO/legacy"
unzip -q "$CODE" -d "$SEED"; unzip -q -o "$DATA" -d "$SEED"
# if a zip unpacked into one top-level folder, lift its contents up
flatten() { local d="$1"; local n; n=$(find "$d" -mindepth 1 -maxdepth 1 | wc -l)
  if [ "$n" -eq 1 ] && [ -d "$(find "$d" -mindepth 1 -maxdepth 1)" ]; then local sub; sub=$(find "$d" -mindepth 1 -maxdepth 1)
    (shopt -s dotglob; mv "$sub"/* "$d"/); rmdir "$sub"; fi; }
flatten "$REPO/legacy"; flatten "$SEED"
# keep big binaries and caches out of git
find "$REPO/legacy" \( -name "*.pkl" -o -name "*.parquet" -o -name "__pycache__" -o -name "node_modules" \) -prune -exec rm -rf {} + 2>/dev/null || true
chmod -R a-w "$SEED"

cd "$REPO"
cat > .gitignore <<'EOF'
node_modules/
.next/
dist/
__pycache__/
*.pyc
.venv/
.env
.env.*
!.env.example
.factory/
coverage/
playwright-report/
test-results/
EOF
cat > README.md <<'EOF'
# Marden

Product monorepo. `handoff/` is the source of truth (specs 00–22, screens, contracts, design system).
`legacy/` is the earlier working code to port. Built by the agent factory; see factory/RUNBOOK.md.
EOF
git init -q -b main
git add -A
git -c user.name="Marden Factory" -c user.email="factory@localhost" commit -qm "Import handoff and legacy code"
echo "repo ready: $REPO ($(git ls-files | wc -l | tr -d ' ') files)"

cd "$HERE"
python3 gen_tasks.py "$REPO/handoff" "$REPO/legacy" > tasks.json
echo "seed data: $SEED   ->   export MARDEN_SEED_DIR=$SEED"
echo "next: fill in .env (see PREREQS.md), then:  source .env && python3 factory.py preflight"
