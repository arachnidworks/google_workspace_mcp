#!/usr/bin/env bash
# Merge the newest upstream release tag into sync/upstream and open or update a
# PR into main, review requested from chad-aw. Never pushes main, never merges.
# DRY_RUN=1 does the merge locally and skips every push and gh call.
set -euo pipefail

UPSTREAM_URL=${UPSTREAM_URL:-https://github.com/taylorwilsdon/google_workspace_mcp.git}
BRANCH=sync/upstream
REVIEWER=chad-aw
DRY_RUN=${DRY_RUN:-}

git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM_URL"
git fetch -q upstream --tags
git fetch -q origin main "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" 2>/dev/null || git fetch -q origin main

# Newest final release (vX.Y.Z only: no rc or dev tags).
TAG=$(git tag -l 'v[0-9]*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
[ -n "${FORCE_TAG:-}" ] && TAG=$FORCE_TAG
[ -n "$TAG" ] || { echo "no upstream release tag found"; exit 1; }

if git merge-base --is-ancestor "$TAG" origin/main; then
  echo "$TAG is already in main, nothing to do"; exit 0
fi
if git merge-base --is-ancestor "$TAG" "origin/$BRANCH" 2>/dev/null; then
  echo "$TAG is already on $BRANCH (PR open or merged elsewhere), nothing to do"; exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

# The branch is bot-owned: rebuild it from main so it never carries stale merges.
git checkout -q -B "$BRANCH" origin/main
CONFLICT=0
git merge --no-edit -m "chore(sync): merge upstream $TAG" "$TAG" || CONFLICT=1
if [ "$CONFLICT" = 1 ]; then
  FILES=$(git diff --name-only --diff-filter=U)
  # Commit the markers so the PR exists and the review email goes out.
  git add -A
  git commit -q -m "chore(sync): merge upstream $TAG (CONFLICTS, resolve before merging)"
fi

if [ "$CONFLICT" = 1 ]; then
  BODY="Upstream release \`$TAG\` conflicts with AW changes. Conflict markers are committed on this branch and must be resolved before merging.

Conflicted files:
$(printf '%s\n' "$FILES" | sed 's/^/- `/; s/$/`/')

Resolve: check out \`$BRANCH\`, fix the files, run \`uv run --frozen --extra test pytest\`, push. Do not merge as is."
  TITLE="Sync upstream $TAG (CONFLICTS)"
else
  BODY="Merges upstream release \`$TAG\` into main. CI runs on this PR. Merging does not deploy: Cloud Run deploys stay manual (\`deploy.sh\`)."
  TITLE="Sync upstream $TAG"
fi

if [ -n "$DRY_RUN" ]; then
  echo "DRY RUN: tag=$TAG conflict=$CONFLICT"; echo "$TITLE"; echo "$BODY"; exit 0
fi

git push --force origin "$BRANCH"
if PR=$(gh pr list --head "$BRANCH" --base main --state open --json number --jq '.[0].number') && [ -n "$PR" ]; then
  gh pr edit "$PR" --title "$TITLE" --body "$BODY"
  gh pr edit "$PR" --add-reviewer "$REVIEWER"
else
  gh pr create --base main --head "$BRANCH" --title "$TITLE" --body "$BODY" --reviewer "$REVIEWER"
fi
