#!/usr/bin/env bash
# Called only after all CI tests have passed on a push to main.
# No third-party release action or personal token is required.
set -Eeuo pipefail

: "${GITHUB_SHA:?Required: tested push SHA}"
: "${GH_TOKEN:?Required: workflow token with contents: write}"

git fetch origin main --tags
remote_head="$(git rev-parse refs/remotes/origin/main)"
git checkout -B main refs/remotes/origin/main

publish_if_missing() {
  local tag="$1"
  if gh release view "$tag" --json tagName >/dev/null 2>&1; then
    echo "Release $tag already exists; nothing to republish."
  else
    echo "Publishing release $tag from commit $(git rev-parse --short HEAD)"
    gh release create "$tag" --verify-tag --generate-notes --title "$tag"
  fi
}

tag_at_head="$(git tag --points-at HEAD --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)"

# Rerunning a completed workflow is idempotent.
if [[ "$remote_head" == "$GITHUB_SHA" && -n "$tag_at_head" ]]; then
  python3 scripts/release_version.py check
  expected="v$(python3 scripts/release_version.py current)"
  if [[ "$tag_at_head" != "$expected" ]]; then
    echo "::error::Tag at HEAD ($tag_at_head) does not match declared version ($expected)"
    exit 1
  fi
  publish_if_missing "$tag_at_head"
  exit 0
fi

# Recover an interrupted publish after the bot version commit was pushed.
if [[ "$remote_head" != "$GITHUB_SHA" ]]; then
  parent="$(git rev-parse HEAD^ 2>/dev/null || true)"
  subject="$(git log -1 --pretty=%s)"
  if [[ "$parent" == "$GITHUB_SHA" && "$subject" =~ ^chore\(release\):\ v[0-9]+\.[0-9]+\.[0-9]+\ \[skip\ ci\]$ ]]; then
    tag="v$(python3 scripts/release_version.py current)"
    if [[ "$subject" != "chore(release): $tag [skip ci]" ]]; then
      echo "::error::Release commit version does not match its message."
      exit 1
    fi
    if git show-ref --verify --quiet "refs/tags/$tag"; then
      [[ "$(git rev-list -n 1 "$tag")" == "$remote_head" ]] || {
        echo "::error::Release tag $tag points at a different commit."
        exit 1
      }
    else
      git config user.name "github-actions[bot]"
      git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
      git tag -a "$tag" -m "Release $tag"
      git push origin "refs/tags/$tag"
    fi
    publish_if_missing "$tag"
    exit 0
  fi
  # Never publish code that wasn't tested by this workflow.
  echo "::notice::main moved from $GITHUB_SHA to $remote_head; newer CI handles releases."
  exit 0
fi

python3 scripts/release_version.py check
version="$(python3 scripts/release_version.py prepare)"
tag="v$version"

if git show-ref --verify --quiet "refs/tags/$tag"; then
  echo "::error::Refusing to overwrite existing tag $tag"
  exit 1
fi

if ! git diff --quiet -- codex_auto_startstop.py .codex-plugin/plugin.json; then
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -- codex_auto_startstop.py .codex-plugin/plugin.json
  git commit -m "chore(release): $tag [skip ci]"
  # The workflow token does not trigger another push workflow: no recursion.
  # Non-fast-forward / protected branch failures safely abort publication.
  git push origin HEAD:refs/heads/main
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git tag -a "$tag" -m "Release $tag"
git push origin "refs/tags/$tag"
publish_if_missing "$tag"
