#!/usr/bin/env bash
#
# Make a release: tag the current commit vMAJOR.MINOR.PATCH, one more than the last release.
# The same script is in every cmo repo (cmo_ollama, cmo_client, cmo_server, cmo_vue_client).
#
# Usage: release.bash patch|minor|major [message]
#
#   patch  a fix, e.g. a prompt tweak                         1.3.0 -> 1.3.1
#   minor  something new: a setting, a label, an endpoint     1.3.0 -> 1.4.0
#   major  a change that breaks what reads it: the me_data.json
#          or me_ollama.json layout, the server API           1.3.0 -> 2.0.0
#
# The code reads its version from these tags (git describe), so a log line or a JSON file says
# e.g. cmo_ollama:1.3.0, or cmo_ollama:1.3.0+4 four commits after it, or 1.3.0+4.dirty with
# uncommitted changes. With a pom.xml (cmo_server) or package.json and package-lock.json
# (cmo_vue_client), their version is set to the new one and committed first, so it matches the tag.
#
# The tag is only made here; the script prints the command that pushes it.

set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
repo=$(basename "$PWD")

usage() {
  echo "Usage: $0 patch|minor|major [message]" >&2
  exit 1
}
[[ $# -ge 1 && "$1" =~ ^(patch|minor|major)$ ]] || usage
part=$1
message=${2:-}

if [ -n "$(git status --porcelain)" ]; then
  echo "error: $repo has uncommitted changes; commit them first" >&2
  exit 1
fi
if git describe --tags --exact-match --match 'v[0-9]*' HEAD > /dev/null 2>&1; then
  echo "error: this commit is already $(git describe --tags --exact-match --match 'v[0-9]*' HEAD)" >&2
  exit 1
fi

last=$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || echo v0.0.0)
IFS=. read -r major minor patch <<< "${last#v}"
case $part in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
esac
version="$major.$minor.$patch"

if [ -f pom.xml ]; then
  # the project's own <version>, the first one after its <artifactId>, not the parent's
  sed -i "0,/<artifactId>$repo<\/artifactId>/!{0,/<version>[^<]*<\/version>/s//<version>$version<\/version>/}" pom.xml
fi
if [ -f package.json ]; then
  sed -i "0,/\"version\": *\"[^\"]*\"/s//\"version\": \"$version\"/" package.json
fi
if [ -f package-lock.json ]; then
  # the lock file's own version, at the top and for the root package (""), as npm writes them
  jq --arg v "$version" --indent 2 '.version = $v | if .packages[""] then .packages[""].version = $v else . end' \
    package-lock.json > package-lock.json.tmp && mv package-lock.json.tmp package-lock.json
fi
if [ -n "$(git status --porcelain)" ]; then
  git commit -q -am "Release $version"
fi

git tag -a "v$version" -m "$repo $version${message:+: $message}"
echo "$repo: tagged v$version (was $last)"
echo "push it with: git -C $PWD push origin HEAD v$version"
