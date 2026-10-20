## Summary

<!-- What changes and why. One concern per pull request. -->

## Linked issue

Closes #

## Checklist

- [ ] Tests added or updated; `uv run pre-commit run --all-files` and `uv run pytest` pass locally
- [ ] Docs updated (README, `docs/`, ADR if the spec is deviated from)
- [ ] Persisted formats: `schema_version` bumped with a migration and tests, or no persisted format changed
- [ ] Schemas regenerated (`uv run python scripts/gen_schemas.py`) if models changed

## Evaluation-integrity impact

<!-- Can the student, the tutor, the generator or the fast loop now read anything sealed,
     or any held-out or control outcome? Can control results influence a decision? State "none" if not. -->
