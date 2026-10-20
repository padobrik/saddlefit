# Contributing to Saddlefit

Thanks for helping. [docs/SPEC.md](docs/SPEC.md) is the binding contract; read Part 0 and
Part B before changing behaviour.

## Development setup

```bash
git clone https://github.com/padobrik/saddlefit.git
cd saddlefit
uv sync                      # creates .venv from uv.lock
uv run pre-commit install
```

Docker is needed for the `docker` test layer. A local model runner is only needed for `live`
tests.

## Tests

```bash
uv run pytest -m "not docker and not live" --cov   # unit and integration, with the 85% gate
uv run pytest -m docker                            # sandbox tests (skipped without a daemon)
uv run pytest -m live                              # needs a running model runner
```

| Marker | Layer | Runs |
| --- | --- | --- |
| none | unit: pure logic | every PR |
| `integration` | loops end to end with the fake provider and the toy repository | every PR |
| `docker` | containers, limits, no network, tamper detection | every PR, Linux |
| `live` | a tiny model through Ollama; smoke runs only | nightly, non-blocking |

The network is blocked in tests by pytest-socket, except under `live`. Tests must be
deterministic.

Other checks CI runs:

```bash
uv run ruff check && uv run ruff format --check
uv run mypy
uv run python scripts/gen_schemas.py --check   # regenerate with no flag
```

## Commits and pull requests

- PR titles follow [Conventional Commits](https://www.conventionalcommits.org/), for example
  `feat(loops): add ablation`. Squash merges use the title, so it becomes the commit on `main`.
  Breaking changes use `feat!:` or a `BREAKING CHANGE:` footer.
- Branch names: `<type>/<issue>-<slug>`, for example `feat/12-held-out-gate`.
- Keep pull requests small (about 400 changed lines, excluding generated files and fixtures)
  and to one concern.
- Fill in the template, including the evaluation-integrity impact, and link the issue with
  `Closes #N`.
- Before pushing: `uv run pre-commit run --all-files` and `uv run pytest`.

A pull request merges only when every required check is green, every review thread is
resolved and CodeRabbit's latest review is an approval. Nobody merges with `--admin` or edits
rulesets to get a merge through. If CodeRabbit is unavailable, wait.

Release PRs opened by release-please do not trigger CI on their own, because they are created
with the workflow token. Close and reopen the release PR once to start the checks.

## Persisted formats

Every persisted format carries an integer `schema_version`, starting at 1. A change to a
**released** format (one that a `vX.Y.Z` tag contains) bumps it and ships a migration in
`src/saddlefit/storage/migrations/` with tests; readers accept the current and the previous
version. A format no release has shipped yet keeps its version and needs no migration
([ADR 0002](docs/adr/0002-schema-versions-before-release.md)).

## Diagrams

The README diagrams are generated. Edit the sources in `docs/diagrams/src/*.jsx`, then run
`node docs/diagrams/render.mjs` (Node 18+; it fetches a pinned esbuild through `npx`) and
commit both the source and the regenerated `*-light.svg` and `*-dark.svg` files.

## Architecture decisions

Deviations from the spec and significant decisions are recorded as ADRs in `docs/adr/`
(see [ADR 0001](docs/adr/0001-record-architecture-decisions.md)). A new runtime dependency
needs an ADR.
