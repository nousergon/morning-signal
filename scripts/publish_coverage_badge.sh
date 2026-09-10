#!/usr/bin/env bash
# Publish the coverage figure CI just measured as a shields.io endpoint document.
#
# repository-baseline-policy.md §5.1: a badge whose value is written by a human
# is forbidden — it renders identically to a real one, so a reader cannot tell
# them apart, and it becomes false the moment reality moves without anyone
# editing a file. This repo carried a hand-set `coverage-96%` badge while the
# suite measured 92.55%.
#
# The badge in README.md renders whatever this script last wrote to
# `coverage.json` on the orphan `badges` branch. The number therefore comes
# from the same `.coverage` file the gate in pytest.ini exits non-zero on, and
# it moves on its own.
#
# Runs from .github/workflows/test.yml on pushes to main only. The `badges`
# branch already exists (seeded before the PR that added this script), so the
# contents API is enough and no branch is ever created here.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required to publish the badge document}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

BRANCH="badges"
DOC="coverage.json"

# The same measurement the gate uses — coverage.py's own totals over the whole
# `morning_signal` package, not a figure re-derived from parsed stdout.
python -m coverage json -o coverage-summary.json --quiet

PCT="$(python -c "import json;print(f\"{json.load(open('coverage-summary.json'))['totals']['percent_covered']:.2f}\")")"

# shields' own convention: red below 60, yellow below 80, green above 90.
COLOR="$(python -c "
pct = float('${PCT}')
print('brightgreen' if pct >= 90 else 'green' if pct >= 80 else 'yellow' if pct >= 60 else 'red')
")"

python - "$PCT" "$COLOR" <<'PY' > badge-endpoint.json
import json, sys
pct, color = sys.argv[1], sys.argv[2]
print(json.dumps({
    "schemaVersion": 1,
    "label": "coverage",
    "message": f"{pct}%",
    "color": color,
}, indent=2))
PY

echo "measured coverage: ${PCT}% (${COLOR})"

# The contents API needs the blob sha to replace an existing file. Its absence
# is a hard error rather than a create, because a missing document means the
# `badges` branch is gone — and silently recreating it would hide that the
# README badge has been rendering an error to every reader in the meantime.
SHA="$(gh api "repos/${GITHUB_REPOSITORY}/contents/${DOC}?ref=${BRANCH}" --jq '.sha')"

if [ -z "${SHA}" ]; then
  echo "::error::${DOC} not found on the ${BRANCH} branch — the README badge is broken" >&2
  exit 1
fi

if [ "$(gh api "repos/${GITHUB_REPOSITORY}/contents/${DOC}?ref=${BRANCH}" --jq '.content' | base64 --decode)" = "$(cat badge-endpoint.json)" ]; then
  echo "coverage unchanged at ${PCT}% — nothing to publish"
  exit 0
fi

gh api --method PUT "repos/${GITHUB_REPOSITORY}/contents/${DOC}" \
  -f message="chore(badges): coverage ${PCT}% from ${GITHUB_SHA:-HEAD}" \
  -f branch="${BRANCH}" \
  -f sha="${SHA}" \
  -f content="$(base64 < badge-endpoint.json | tr -d '\n')" \
  --jq '.commit.sha'
