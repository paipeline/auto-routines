<!-- Thanks for the PR. Keep it scoped — one concern per PR. -->

## What this changes

<!-- 1-2 sentences. -->

## Why

<!-- The user-facing motivation. -->

## Test plan

- [ ] `python3 -m pytest tests/` passes locally
- [ ] `python3 scripts/sanity-check.py templates/config.yaml` exits 0
- [ ] If schema changed: bumped `schema_version` and noted migration below
- [ ] If a new sanity check was added: included a failing-config test case
- [ ] Manually re-ran `/auto-routines init` against a temp repo

## Schema migration

<!-- Delete if not applicable. Otherwise: from / to / what users need to do. -->

## Screenshots / sample output

<!-- If user-visible behavior changed. -->
