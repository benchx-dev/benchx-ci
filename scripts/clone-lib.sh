# Shared by bench-run.sh and project providers: reads project.json from ROOT
# (default: $PWD) and manages the local bare-ish upstream git clone. Sourced,
# not executed.
ROOT=${ROOT:-$PWD}

# project FIELD: one value from project.json, by dotted path (upstream.url).
project() {
  python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): d = d[k]
print(d)' "$ROOT/project.json" "$1"
}

PROJECT=$(project name)
UPSTREAM_URL=${UPSTREAM_URL:-$(project upstream.url)}
UPSTREAM_BRANCH=${UPSTREAM_BRANCH:-$(project upstream.branch)}
UPSTREAM_REFERENCE=${UPSTREAM_REFERENCE:-}
WORK=${BENCHX_WORK:-$HOME/.benchx-$PROJECT}
CLONE=$WORK/$PROJECT

ensure_clone() {
  if [[ ! -d $CLONE/.git ]]; then
    mkdir -p "$WORK"
    local ref=()
    [[ -n $UPSTREAM_REFERENCE ]] && ref=(--reference-if-able "$UPSTREAM_REFERENCE")
    echo "clone: $UPSTREAM_URL -> $CLONE" >&2
    git clone -q --no-checkout "${ref[@]}" "$UPSTREAM_URL" "$CLONE" >&2
  fi
}

fetch_main() {
  git -C "$CLONE" fetch -q origin "+refs/heads/$UPSTREAM_BRANCH:refs/remotes/origin/$UPSTREAM_BRANCH" >&2
}

# resolve_commit REF: the full sha, fetching it from origin when it is unknown locally.
resolve_commit() {
  local sha
  if ! sha=$(git -C "$CLONE" rev-parse --verify -q "$1^{commit}"); then
    git -C "$CLONE" fetch -q origin "$1" >&2 || return 1
    sha=$(git -C "$CLONE" rev-parse --verify -q "$1^{commit}") || return 1
  fi
  echo "$sha"
}
