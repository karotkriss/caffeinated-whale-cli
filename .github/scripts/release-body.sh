#!/usr/bin/env bash
# Compose a release card body for caffeinated-whale-cli and print it to stdout.
#
# Usage: release-body.sh <version> <owner/repo>
#
# The card is two parts:
#   - The "What's New" or "What's Changed" copy, written by hand per release in
#     .github/release-notes/v<version>.md. It is advertising copy in the
#     reader's terms, which no template can generate, so this script requires
#     the file and refuses to invent a substitute.
#   - The footer (Installation, CHANGELOG link, Full Changelog), which is
#     entirely mechanical and is generated here so it cannot drift: the
#     CHANGELOG link is pinned to this release's tag, never a branch, and the
#     comparison runs from the previous version tag to this one.
#
# The change-list entries can name a flagship entry with a `<!-- flagship -->`
# marker directly above it (see .github/release-notes/README.md). This script
# is the one place that guarantees the marked entry leads: it refuses to
# publish (rather than rewrite hand-written Markdown) when the marker is
# buried, duplicated, or detached from an entry, and it strips the marker
# itself so it never reaches the published card.
set -euo pipefail

VERSION="${1:?usage: release-body.sh <version> <owner/repo>}"
REPO="${2:?usage: release-body.sh <version> <owner/repo>}"

TAG="v${VERSION}"
NOTES=".github/release-notes/${TAG}.md"

if [[ "$VERSION" =~ ^[0-9]+\.0\.0$ ]]; then
  EXPECTED_HEADING="### What's New"
  RELEASE_SIZE="major"
else
  EXPECTED_HEADING="### What's Changed"
  RELEASE_SIZE="smaller"
fi

if [ ! -s "$NOTES" ]; then
  echo "error: ${NOTES} is missing or empty." >&2
  echo "The release card's copy is written by hand per release." >&2
  echo "See .github/release-notes/README.md for the format." >&2
  exit 1
fi

FIRST_LINE=$(head -n 1 "$NOTES")
if [ "$FIRST_LINE" != "$EXPECTED_HEADING" ]; then
  echo "error: ${NOTES} must open with exactly \"${EXPECTED_HEADING}\" because ${VERSION} is a ${RELEASE_SIZE} release." >&2
  exit 1
fi

IS_MAJOR="false"
if [ "$RELEASE_SIZE" = "major" ]; then
  IS_MAJOR="true"
fi

# Validate and strip the optional `<!-- flagship -->` marker (see the header
# comment above and .github/release-notes/README.md for the authoring rule).
# A malformed/duplicated/detached marker, or one that does not lead the change
# list, stops the release rather than silently reordering hand-written prose;
# a major release with no marker at all also stops, since guessing a headline
# would be worse than requiring one. This is the ONE place the rule is
# enforced, so a later change to the format only needs to touch this program.
# shellcheck disable=SC2016
FLAGSHIP_AWK='
{ lines[NR] = $0 }
END {
  n = NR
  malformed = 0
  canon_count = 0
  canon_line = 0
  for (i = 1; i <= n; i++) {
    line = lines[i]
    canonical = line
    sub(/\r$/, "", canonical)
    if (canonical == "<!-- flagship -->") {
      canon_count++
      canon_line = i
      continue
    }
    low = tolower(line)
    if (index(low, "<!--") > 0 && index(low, "-->") > 0 && index(low, "flagship") > 0) {
      malformed++
      malformed_line = i
    }
  }

  if (malformed > 0) {
    print "error: malformed flagship marker on line " malformed_line " - it must be exactly \"<!-- flagship -->\" flush left on its own line" > "/dev/stderr"
    exit 1
  }
  if (canon_count > 1) {
    print "error: multiple <!-- flagship --> markers found - only one entry may be marked flagship" > "/dev/stderr"
    exit 1
  }
  if (canon_count == 0) {
    if (is_major == "true") {
      print "error: a major release requires an explicit <!-- flagship --> marker naming the headline entry - see .github/release-notes/README.md" > "/dev/stderr"
      exit 1
    }
    for (i = 1; i <= n; i++) print lines[i]
    exit 0
  }

  prev_blank = (lines[canon_line - 1] ~ /^[ \t]*$/)
  next_exists = (canon_line < n)
  next_blank = next_exists ? (lines[canon_line + 1] ~ /^[ \t]*$/) : 1
  if (!prev_blank) {
    print "error: the <!-- flagship --> marker on line " canon_line " must start its own paragraph - insert a blank line above it" > "/dev/stderr"
    exit 1
  }
  if (!next_exists || next_blank) {
    print "error: the <!-- flagship --> marker on line " canon_line " is detached from any entry - it must sit directly above the entry'"'"'s first line with no blank line between them" > "/dev/stderr"
    exit 1
  }
  if (lines[canon_line + 1] !~ /^\*\*[^*].*\*\*[ \t\r]*$/) {
    print "error: the <!-- flagship --> marker on line " canon_line " must sit directly above an entry that begins with a bold benefit sentence" > "/dev/stderr"
    exit 1
  }

  ngroups = 0
  in_group = 0
  for (i = 2; i <= n; i++) {
    blank = (lines[i] ~ /^[ \t]*$/)
    if (!blank && !in_group) {
      ngroups++
      group_start[ngroups] = i
      in_group = 1
    }
    if (blank) in_group = 0
  }

  marked_group = 0
  for (g = 1; g <= ngroups; g++) {
    if (group_start[g] == canon_line) { marked_group = g; break }
  }
  if (marked_group != 1) {
    print "error: the flagship entry (line " canon_line ") does not lead the change list - move it to be the first entry after the heading" > "/dev/stderr"
    exit 1
  }

  for (i = 1; i <= n; i++) {
    if (i == canon_line) continue
    print lines[i]
  }
}
'

if ! NOTES_BODY=$(awk -v is_major="$IS_MAJOR" "$FLAGSHIP_AWK" "$NOTES"); then
  exit 1
fi

# The version tag directly below this one, in version order. Empty for the
# first release, which has nothing to compare against.
PREV_TAG=$(git tag --list 'v*.*.*' --sort=-v:refname \
  | awk -v cur="$TAG" 'found { print; exit } $0 == cur { found = 1 }')

printf '%s\n' "$NOTES_BODY"
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
