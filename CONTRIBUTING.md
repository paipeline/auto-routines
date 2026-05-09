# Contributing to auto-routines

Thanks for your interest. `auto-routines` is a small, sharp tool — contributions that keep it that way are very welcome.

## Ways to help

- **File a bug.** Open an issue with a reproduction. The smaller the repro, the faster the fix.
- **Propose a routine.** If you've thought of an automation that would be broadly useful, open an issue with the trigger, the purpose, and a sketch of the prompt. Don't ship it as a default until the community wants it.
- **Improve the sanity check.** `scripts/sanity-check.py` is the deterministic gate. New checks that catch real misconfigurations are always welcome — include a failing-config test case.
- **Polish the docs.** README, SKILL.md, examples — clearer beats clever.
- **Test on real repos.** The validation matrix in commit history was run against a temp repo. Real-world reports across stacks (Rails, Go, Python, Rust, monorepos) help us harden the install path.

## Ground rules

1. **TDD or it doesn't ship.** This project is developed test-first. Every guardrail in `SKILL.md` has a failing test in `tests/` *before* the corresponding check in `scripts/sanity-check.py`. PRs that add a check without a test (or a test without a corresponding behavior) will be sent back.
2. **Keep the core minimal.** The skill should stay readable end-to-end. Resist the urge to add layers.
3. **Sanity check first.** Any new field in `config.yaml` needs a corresponding check in `scripts/sanity-check.py` *and* a test case in `tests/test_sanity_check.py`.
4. **No silent breaking changes.** If you change the schema, bump `schema_version` in `templates/config.yaml` and document the migration in the PR.
5. **Real on-commit triggers stay as `git-hook`.** Do not invent a fake "PostCommit" Claude hook event — Claude Code does not emit one.
6. **Routines never push to main.** They commit on `routines/<id>` branches and open PRs. This is non-negotiable.

## TDD workflow

```bash
pip install pyyaml pytest

# 1. write a failing test
$EDITOR tests/test_sanity_check.py
pytest -q                              # red

# 2. add the minimum check to make it pass
$EDITOR scripts/sanity-check.py
pytest -q                              # green

# 3. refactor — keep it green
pytest -q
```

CI runs `pytest -q` against Python 3.9–3.12 on every push and PR.

## Dev loop

```bash
# clone fork
git clone https://github.com/<you>/auto-routines ~/.claude/skills/auto-routines
cd ~/.claude/skills/auto-routines

# always run the test suite before pushing
pip install pyyaml pytest
pytest -q

# end-to-end smoke test against a temp repo
cd /tmp && mkdir test-repo && cd test-repo && git init

# in another shell
claude --dangerously-skip-permissions
> /auto-routines

# after changes to the skill, re-run /auto-routines init in the test repo
# verify sanity-check still exits 0:
python3 ~/.claude/skills/auto-routines/scripts/sanity-check.py .iteration/config.yaml
```

## Pull requests

- One concern per PR. Easier to review, easier to revert.
- Update README/SKILL.md if behavior changes.
- Add a line to the PR description for any new `config.yaml` field.
- Be patient — review can take a few days.

## Code of conduct

Be kind. Disagree about ideas, not people. We follow the spirit of the [Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
