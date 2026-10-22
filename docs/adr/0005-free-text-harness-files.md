# 5. Free-text harness files are content, not persisted formats

Date: 2026-10-22

## Status

Accepted

## Context

Hard rule 7, C7, ADR 0002 and ADR 0004 require a `schema_version` bump, a migration and a test
for every change to a released persisted format. The CodeRabbit "Schema versioning" check
enforces this.

B8 lists the persisted formats: each carries an integer `schema_version`, a Pydantic model is
its source of truth, and a JSON Schema is generated into `schemas/`. In the harness store these
are `harness.json` and `loop.yaml`.

The other harness files are free text that the tutor writes and the student reads:
`systemprompt.md`, `tool_guidance.md`, `EXPERIENCE.md` and the files under `skills/` and
`tools/`. Changing their content is what the tutor is for. They have no `schema_version`, no
model and no schema. Where Saddlefit reads a convention from them, such as one lesson per
hyphen-led list item in EXPERIENCE.md with a `(model: …)` label, the convention lives in
the reader's code.

On #99 the check failed a change that writes lessons to EXPERIENCE.md in exactly the shape the
reader released in 0.1.0 already parses, and asked for a `schema_version` and a migration for
EXPERIENCE.md. CodeRabbit agreed in a comment that this is no format change, but the check
stayed failed, as it did on #88.

## Decision

- Free-text harness files (`systemprompt.md`, `tool_guidance.md`, `EXPERIENCE.md`,
  `skills/**`, `tools/**`) are content, not persisted formats. Writing, changing or appending
  their content needs no `schema_version` bump or migration.
- A change to how Saddlefit *reads* such a file is a behaviour change of the reader. It must
  keep reading what released versions wrote, and a test must show that; it needs no version
  bump, since the file carries none.
- Everything else in ADR 0002 and ADR 0004 stands: a file with a `schema_version`, a model or
  a generated JSON Schema (`harness.json`, `loop.yaml`, run records, `saddlefit.yaml` and the
  rest) is a persisted format.

The CodeRabbit check and its copy in `docs/SPEC.md` (C6) are reworded to say this.

## Consequences

The tutor's ordinary work, changing harness text, no longer trips the check. A new reader
convention for a free-text file is still held to backward compatibility, through tests rather
than versions. Giving such a file real structure would mean making it a persisted format with
a `schema_version`, a model and a schema, and from then on the strict rule applies to it.
