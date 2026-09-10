# `badges` — machine-written badge documents

This branch holds nothing a human edits. `scripts/publish_coverage_badge.sh`
rewrites `coverage.json` from `.github/workflows/test.yml` on every push to
`main`, and `README.md` on `main` renders it through shields.io's `endpoint`
schema.

repository-baseline-policy.md §5.1: a badge whose value is written by a human
is forbidden — it renders identically to a real one and becomes false the
moment reality moves without anyone editing a file. `coverage.json` starts at
`pending` and is replaced by the first measured run; a hand-typed number here
would be the exact defect this branch exists to remove.
