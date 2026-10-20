# 1. Record architecture decisions

Date: 2026-10-20

## Status

Accepted

## Context

`docs/SPEC.md` is the binding build contract. Implementations sometimes have to deviate from
it, and those deviations must stay visible and reviewable.

## Decision

Every deviation from the spec, and every significant architectural decision the spec leaves
open, is recorded as an Architecture Decision Record in `docs/adr/`:

- File name `NNNN-short-title.md`, numbered sequentially, never renumbered.
- Sections: Status, Context, Decision, Consequences.
- An ADR that changes the contract ships in the same pull request as an update to
  `docs/SPEC.md`.
- Superseded ADRs stay in place with status "Superseded by NNNN".
- Adding a runtime dependency not listed in B1 requires an ADR.

## Consequences

Reviewers can trace every departure from the spec to a decision with its reasons.
