#!/usr/bin/env bash
# Compose a release card body for caffeinated-whale-cli and print it to stdout.
#
# Usage: release-body.sh <version> <owner/repo>
#
# The card is two parts:
#   - The "What's Changed" copy, written by hand per release in
#     .github/release-notes/v<version>.md. It is advertising copy in the
#     reader's terms, which no template can generate, so this script requires
#     the file and refuses to invent a substitute.
#   - The footer (Installation, CHANGELOG link, Full Changelog), which is
#     entirely mechanical and is generated here so it cannot drift: the
#     CHANGELOG link is pinned to this release's tag, never a branch, and the
#     comparison runs from the previous version tag to this one.
set -euo pipefail

VERSION="${1:?usage: release-body.sh <version> <owner/repo>}"
REPO="${2:?usage: release-body.sh <version> <owner/repo>}"

TAG="v${VERSION}"
NOTES=".github/release-notes/${TAG}.md"

if [ ! -s "$NOTES" ]; then
  echo "error: ${NOTES} is missing or empty." >&2
  echo "The release card's What's Changed copy is written by hand per release." >&2
  echo "See .github/release-notes/README.md for the format." >&2
  exit 1
fi

if ! grep -q "^### What's Changed" "$NOTES"; then
  echo "error: ${NOTES} must open with a \"### What's Changed\" heading." >&2
  exit 1
fi

# The version tag directly below this one, in version order. Empty for the
# first release, which has nothing to compare against.
PREV_TAG=$(git tag --list 'v*.*.*' --sort=-v:refname \
  | awk -v cur="$TAG" 'found { print; exit } $0 == cur { found = 1 }')

cat "$NOTES"
cat <<EOF

### Installation

\`\`\`bash
# with uv (recommended)
uv tool install --upgrade caffeinated-whale-cli

# or with pip
pip install --upgrade caffeinated-whale-cli

# or run it once without installing
uvx --from caffeinated-whale-cli cwcli --version
\`\`\`

See the [CHANGELOG](https://github.com/${REPO}/blob/${TAG}/CHANGELOG.md) for the full detail.
EOF

if [ -n "$PREV_TAG" ]; then
  echo
  echo "**Full Changelog**: https://github.com/${REPO}/compare/${PREV_TAG}...${TAG}"
fi
