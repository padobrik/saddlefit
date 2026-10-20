# 2. Schema versions before the first release of a format

Date: 2026-10-20

## Status

Accepted

## Context

Hard rule 7 and C7 say every change to a persisted format bumps its `schema_version` and ships a migration with tests, and that readers accept the current and the previous version. The CodeRabbit "Schema versioning" pre-merge check enforces this.

While v0.1.0 was being built, the check failed pull requests that:

- introduced a brand-new format at `schema_version: 1` (`loop.yaml`, run records), where no previous version exists and no migration can be written;
- tightened validation of a format that is on `main` but in no release yet (`TaskMeta`, `SuiteLock`).

No reader of an earlier version of these formats can exist, so a version bump would only add a meaningless `v1 → v2` step before anything had shipped.

## Decision

- A persisted format is **released** once a `vX.Y.Z` tag contains it.
- Changes to a released format follow C7 unchanged: bump `schema_version`, ship a migration in `src/saddlefit/storage/migrations/`, and test it.
- A new format starts at `schema_version: 1`. A change to a format that no release has shipped yet keeps its version and needs no migration.
- Every persisted format still carries an integer `schema_version` from the start.

The CodeRabbit custom check and hard rule 7 in `docs/SPEC.md` are reworded to say exactly this.

## Consequences

The check keeps its purpose (no silent breaking change for anyone who has a released file) without blocking new formats. After `v0.1.0` ships, every format in it falls under the strict rule.
