"""Vendored commodity utilities from nousergon-lib (MIT-era, pre-0.60.0).

WHY THIS EXISTS
---------------
nousergon-lib was relicensed to AGPL-3.0-only at v0.60.0 (it is the internal
NE/Crucible fleet dedup layer). morning-signal is an independent MIT product
and must not carry an AGPL dependency, so the three commodity utilities it used
are vendored here instead. The vendored code is the MIT-era nousergon-lib
source (<= 0.59.8), which remains under the MIT License.

CONTENTS (all generic, no NE edge):
- anthropic_payload.py  — Anthropic API request-payload validation
- cost.py               — LLM cost telemetry / `record_anthropic_call`
- model_metadata.py     — `ModelMetadata` (extracted from decision_capture)
- model_pricing.yaml    — packaged Anthropic rate card (loaded by cost.py)
- trading_calendar.py   — market-calendar helpers (`is_trading_day`)

STRIP PLAN (when the public MIT `nousergon-core` lib ships — config#<core>):
1. delete this `_vendor/` directory,
2. add `nousergon-core` to pyproject dependencies,
3. repoint imports `morning_signal._vendor.nousergon.X` -> `nousergon_core.X`.
Kept import-compatible (same module + symbol names) so the swap is mechanical.
"""
