#!/bin/sh
# Publish a branch (default: main) to a public Git remote with a privacy-safe commit identity.
#
# Every commit's author and committer email is replaced by the given GitHub "noreply" address in a
# THROWAWAY CLONE, so this repository, its branches and its worktrees are never modified. The rewrite is
# deterministic (same input commits give the same output hashes), so later runs push as a plain
# fast-forward. The script never force-pushes.
#
# Usage: scripts/publish_public.sh <noreply-email> <remote-url> [branch]
# Example: scripts/publish_public.sh 12345+user@users.noreply.github.com https://github.com/user/repo.git
set -eu

NEW="${1:?usage: publish_public.sh <noreply-email> <remote-url> [branch]}"
REMOTE="${2:?usage: publish_public.sh <noreply-email> <remote-url> [branch]}"
BRANCH="${3:-main}"

case "$NEW" in
  *@users.noreply.github.com) ;;
  *) echo "Refusing: the email must end in @users.noreply.github.com" >&2; exit 2 ;;
esac

SRC="$(git rev-parse --show-toplevel)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone -q --no-local --branch "$BRANCH" --single-branch "$SRC" "$WORK/repo"
cd "$WORK/repo"
NAME="$(git log -1 --format=%an)"
export NEW NAME
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --env-filter '
  export GIT_AUTHOR_EMAIL="$NEW"; export GIT_COMMITTER_EMAIL="$NEW"
  export GIT_AUTHOR_NAME="$NAME"; export GIT_COMMITTER_NAME="$NAME"
' -- --all >/dev/null 2>&1
rm -rf .git/refs/original
git reflog expire --expire=now --all
git gc -q --prune=now

# Verify before pushing: only the noreply identity may appear in any commit.
others="$(git log --all --format='%ae%n%ce' | sort -u | grep -v -x -F "$NEW" || true)"
if [ -n "$others" ]; then
  echo "Refusing to push: found other identities in the rewritten history." >&2
  exit 3
fi
echo "Rewrote $(git rev-list --count HEAD) commits; all use $NEW"
git push "$REMOTE" "$BRANCH:$BRANCH"
