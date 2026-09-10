## What & why

<!-- What does this change and why? Link any related issue. -->

## Checklist

- [ ] Tests added/updated for the behavior change
- [ ] `pytest` passes locally — the coverage floor in `pytest.ini` is a ratchet, raised as coverage improves and never lowered to make a change pass
- [ ] `CHANGELOG.md` updated
- [ ] No secrets or proprietary prompt content committed (use `prompt.example.md` / `.example` config)
- [ ] Change stays within the public engine's scope (no multi-tenant / billing / hosted-service code)
- [ ] Fail-loud preserved — no new silent `except: pass` swallows

## Test plan

<!-- How you verified this works. -->

---

**Prepared by:** _<!-- your name, or the model if AI-assisted -->_
<!-- Required by pull-request-policy.md. A template in THIS repo shadows the
     org default in `nousergon/.github`, which carries this field — so a local
     template without it silently removes the attribution from every PR here. -->
