#!/usr/bin/env bash
# Compose a release card body for caffeinated-whale-cli and print it to stdout.
#
# Usage: release-body.sh <version> <owner/repo>
#
# The body is Chris's cross-project release-notes template (see the global
# agent instructions), not a cwcli-specific shape:
#
#   ## [<version>](<diff url>) (<date>)
#
#   > Description
#
#   ### Upgrade Steps
#   ### Breaking Changes
#   ### New Features
#   ### Bug Fixes
#   ### Performance Improvements
#   ### Other Changes
#
# The header (version link + release date) is mechanical and generated here
# so it cannot drift: the diff link runs from the previous release tag to
# this one and is empty for the first release, which has nothing to compare
# against. Everything after it is hand-written per release in
# .github/release-notes/v<version>.md - no template can write that copy, so
# this script requires the file and refuses to invent a substitute. A section
# that does not apply to a given release is omitted entirely (heading and
# all) rather than published empty.
set -euo pipefail

VERSION="${1:?usage: release-body.sh <version> <owner/repo>}"
REPO="${2:?usage: release-body.sh <version> <owner/repo>}"

TAG="v${VERSION}"
NOTES=".github/release-notes/${TAG}.md"

if [ ! -s "$NOTES" ]; then
  echo "error: ${NOTES} is missing or empty." >&2
  echo "The release card's copy is written by hand per release." >&2
  echo "See .github/release-notes/README.md for the format." >&2
  exit 1
fi

# Every "### " heading in the hand-written note must be one of the six
# sections the template allows, so a typo'd heading (e.g. "### Bugfixes")
# fails the release instead of publishing as an unrecognised, never-omitted
# section.
ALLOWED_HEADING_RE='^### (Upgrade Steps|Breaking Changes|New Features|Bug Fixes|Performance Improvements|Other Changes)$'
section_heading=
section_has_entry=0
while IFS= read -r line || [ -n "$line" ]; do
  if [[ "$line" == "###"* ]]; then
    if ! [[ "$line" =~ $ALLOWED_HEADING_RE ]]; then
      echo "error: ${NOTES} has an unrecognised heading: ${line}" >&2
      echo "Only these are valid: Upgrade Steps, Breaking Changes, New Features, Bug Fixes, Performance Improvements, Other Changes." >&2
      exit 1
    fi
    if [ -n "$section_heading" ] && [ "$section_has_entry" -eq 0 ]; then
      echo "error: ${NOTES} has an empty section: ${section_heading}" >&2
      exit 1
    fi
    section_heading="$line"
    section_has_entry=0
    continue
  fi
  if [ -n "$section_heading" ] && [[ "$line" =~ ^\*\ [^[:space:]].* ]]; then
    section_has_entry=1
    if [ "$section_heading" = "### Upgrade Steps" ] && [[ "$line" != *"[ACTION REQUIRED]"* ]]; then
      echo "error: ${NOTES} has an Upgrade Steps entry with no [ACTION REQUIRED] flag: ${line}" >&2
      exit 1
    fi
  fi
done <"$NOTES"

if [ -n "$section_heading" ] && [ "$section_has_entry" -eq 0 ]; then
  echo "error: ${NOTES} has an empty section: ${section_heading}" >&2
  exit 1
fi

# The version tag directly below this one, in version order. Empty for the
# first release, which has nothing to compare against.
PREV_TAG=$(git tag --list 'v*.*.*' --sort=-v:refname \
  | awk -v cur="$TAG" '$0 != cur { print; exit }')

DATE=$(date -u +%F)

if [ -n "$PREV_TAG" ]; then
  printf '## [%s](https://github.com/%s/compare/%s...%s) (%s)\n\n' \
    "$VERSION" "$REPO" "$PREV_TAG" "$TAG" "$DATE"
else
  printf '## [%s] (%s)\n\n' "$VERSION" "$DATE"
fi

cat "$NOTES"
