#!/usr/bin/env bash
# One attempt at measuring one upstream commit on an ephemeral machine. The
# machine keeps nothing and only ever adds objects to a raw store (RAW_URL: an
# S3 prefix, or a local directory) under <sha>/<machine>/:
#
#   attempts/<id>.json     this attempt: the commit, the units it has, how it ended
#   attempts/<id>.log      its log
#   units/<unit>.tar.gz    the result files of one unit (benchmark suite)
#
# A unit is uploaded as soon as it is measured, so a run that dies loses at most
# the unit in flight. A restart is the same call again: it lists the store and
# runs only the units that have no archive. Nothing is read back, merged or
# replaced here; scripts/compact.py turns the raw objects into Parquet elsewhere.
# scripts/run_state.py documents the layout.
#
# Required env:
#   SHA MACHINE RAW_URL
#   DISCOVER_SCRIPT        executable printing one suite per line (either
#                          "<suite>" or "<unit>\t<suite>") given BUILD_DIR
#
# Optional env:
#   ROOT                   project repository root (default: $PWD)
#   CONFIGURATION          target provider configuration (e.g. "cpp"; passed as
#                          @<cfg>=<sha> when set, else <sha>)
#   ATTEMPT_ID             names this attempt (the workflow run)
#   BUDGET_MIN             start no new unit after this many minutes (default 240)
#   SUITE_FILTER           regex matched against unit names (default ".")
#   REPETITIONS            bx bench --repetitions (default 3)
#   CASE_TIMEOUT           bx bench --case-timeout (default 600)
#
# Exit: 0 when every unit that matches SUITE_FILTER is recorded, 1 otherwise.
set -euo pipefail
: "${SHA:?}" "${MACHINE:?}" "${RAW_URL:?}" "${DISCOVER_SCRIPT:?}"
CI_SCRIPTS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "${ROOT:-$PWD}" && pwd)
ATTEMPT_ID=${ATTEMPT_ID:-local-$(date +%s)}
BUDGET_MIN=${BUDGET_MIN:-240}
SUITE_FILTER=${SUITE_FILTER:-.}
REPETITIONS=${REPETITIONS:-3}
CASE_TIMEOUT=${CASE_TIMEOUT:-600}
CONFIGURATION=${CONFIGURATION:-}
source "$CI_SCRIPTS/clone-lib.sh"
BX=${BX:-$(command -v bx)}
PYTHON=${PYTHON:-$(command -v python3)}
DEADLINE=$(( $(date +%s) + BUDGET_MIN * 60 ))

cd "$ROOT"
ensure_clone
SHA=$(resolve_commit "$SHA") || { echo "bench-run: unknown commit" >&2; exit 2; }
KEY=$SHA/$MACHINE
RUN=$WORK/raw/$SHA/$MACHINE
rm -rf "$RUN"; mkdir -p "$RUN"
ATTEMPT=$RUN/attempt.json
exec > >(tee -a "$RUN/log.txt") 2>&1
echo "== $(date -u +%FT%TZ) machine=$MACHINE commit=$SHA attempt=$ATTEMPT_ID"

# ---- the raw store -----------------------------------------------------------
state() { local command=$1; shift; "$PYTHON" "$CI_SCRIPTS/run_state.py" "$command" "$RAW_URL" "$KEY" "$@"; }
upload_log() { "$PYTHON" "$CI_SCRIPTS/remote.py" put "$RAW_URL" "$KEY/attempts/$ATTEMPT_ID.log" "$RUN/log.txt" || echo "bench-run: the log upload failed" >&2; }

git -C "$CLONE" show -s --format='%H%n%P%n%aI%n%cI%n%s' "$SHA" | "$PYTHON" -c '
import json, sys
sha, parents, author_date, committer_date, subject = sys.stdin.read().split("\n", 4)
print(json.dumps({"sha": sha, "parents": parents.split(), "author_date": author_date,
                  "committer_date": committer_date, "subject": subject.rstrip("\n"),
                  "branch": sys.argv[1]}, indent=1))' "$UPSTREAM_BRANCH" >"$RUN/commit.json"

# The attempt is counted from here on, even if the machine dies during the build.
state begin --attempt "$ATTEMPT_ID" --file "$ATTEMPT" --commit "$RUN/commit.json" --reason "$UPSTREAM_BRANCH"
ENDED=
finish() {  # finish [STAGE]: close the attempt once, whatever ended it
  [[ -n $ENDED ]] && return
  ENDED=1
  state end --attempt "$ATTEMPT_ID" --file "$ATTEMPT" ${1:+--stage "$1"} || echo "bench-run: the final upload failed" >&2
  upload_log
}
trap 'finish' EXIT
trap 'echo "bench-run: interrupted"; exit 1' INT TERM

# ---- build -------------------------------------------------------------------
TARGET_SPEC=${CONFIGURATION:+@$CONFIGURATION=}$SHA
if ! DESCRIPTION=$("$BX" target prepare "$TARGET_SPEC"); then
  echo "bench-run: the provider failed"
  finish build
  exit 1
fi
BUILD=$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["target"]["path"])' <<<"$DESCRIPTION")
ACTIVATION=$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("activation") or "")' <<<"$DESCRIPTION")
if [[ -n $ACTIVATION ]]; then
  eval "$ACTIVATION"
fi

# ---- discover & measure ------------------------------------------------------
# The commit's units are all suites reported by DISCOVER_SCRIPT, whatever this
# attempt is asked to run: an attempt with a SUITE_FILTER leaves the pair
# partial, not done. Each line is "<suite>" (unit = basename) or "<unit>\t<suite>".
declare -A SUITE_ARG=()
ALL=()
while IFS= read -r line; do
  [[ -n $line ]] || continue
  if [[ $line == *$'\t'* ]]; then
    unit=${line%%$'\t'*}
    suite_arg=${line#*$'\t'}
  else
    suite_arg=$line
    unit=${line##*/}
  fi
  ALL+=("$unit")
  SUITE_ARG["$unit"]=$suite_arg
done < <("$DISCOVER_SCRIPT" "$BUILD")

if (( ${#ALL[@]} > 0 )); then
  mapfile -t SUITES < <(printf '%s\n' "${ALL[@]}" | grep -E -- "$SUITE_FILTER" || true)
else
  SUITES=()
fi
if [[ ${#SUITES[@]} == 0 ]]; then
  echo "bench-run: no benchmark unit matches SUITE_FILTER='$SUITE_FILTER'"
  finish discover
  exit 1
fi
state expect --attempt "$ATTEMPT_ID" --file "$ATTEMPT" "${ALL[@]}"
if ! PENDING=$(state todo "${SUITES[@]}"); then
  echo "bench-run: the store could not be listed"
  finish list
  exit 1
fi
mapfile -t TODO < <(printf '%s' "$PENDING")
echo "units: ${#ALL[@]} in this commit, ${#SUITES[@]} match SUITE_FILTER='$SUITE_FILTER', ${#TODO[@]} to run"

for unit in "${TODO[@]}"; do
  [[ -n $unit ]] || continue
  if (( $(date +%s) >= DEADLINE )); then
    echo "bench-run: the $BUDGET_MIN minute budget is spent; the rest is left for a restart"
    break
  fi
  suite_arg=${SUITE_ARG[$unit]}
  out=$RUN/suites/$unit
  mkdir -p "$out"
  echo "-- $unit"
  if "$BX" bench "$BUILD" --suite "$suite_arg" \
       --project "$PROJECT" --run-key "$UPSTREAM_BRANCH/$SHA/$MACHINE" --out "$out" \
       --repetitions "$REPETITIONS" --case-timeout "$CASE_TIMEOUT" --no-ingest >"$out/table.txt" 2>&1; then
    rc=0
  else
    rc=$?; echo "   exit $rc"
    if [[ -s $out/table.txt ]]; then
      tail -n 40 "$out/table.txt" | sed 's/^/   /' || true
    fi
  fi
  # A failed upload leaves the unit unrecorded, for a restart.
  state record --attempt "$ATTEMPT_ID" "$unit" "$rc" "$out" || echo "bench-run: the upload of $unit failed"
  upload_log
done

finish
if ! PENDING=$(state todo "${SUITES[@]}"); then
  echo "== done, but the store could not be listed: what is recorded is unknown"
  exit 1
fi
LEFT=$(printf '%s' "$PENDING" | grep -c . || true)
echo "== done: $(( ${#SUITES[@]} - LEFT )) of ${#SUITES[@]} units recorded"
[[ $LEFT == 0 ]]
