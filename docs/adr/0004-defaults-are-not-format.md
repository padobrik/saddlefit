# 4. A new default is not a format change

Date: 2026-10-21

## Status

Accepted

## Context

Hard rule 7, C7 and ADR 0002 require a `schema_version` bump, a migration and a test for every change to a released persisted format. The CodeRabbit "Schema versioning" check enforces this.

Saddlefit 0.1.0 and 0.1.1 shipped with the default `sandbox.image` pinned to `ghcr.io/padobrik/saddlefit-sandbox:0.1.0`. That image exists for amd64 only. #88 makes the default the image published with the installed version. The check failed the change and asked for `schema_version: 2` with a migration that writes the old default into files that omit the field.

Nothing about the format changes:

- the keys, types, constraints and required fields are the same;
- every file accepted before is accepted now, with the same values;
- `dump_config` writes every field, so `init` puts the image into `saddlefit.yaml` and a project keeps it across releases.

Only the value used for an omitted field changes. A migration that preserved the old value would keep the image the change moves away from. Every release with a new sandbox image would need another bump.

## Decision

- A new default for an optional field is not a format change when the serializer writes that field out. The same holds for removing a JSON Schema `default` annotation, which documents and does not validate.
- Everything else in ADR 0002 stands: a change to the keys, types, constraints, required fields, serializer or file layout of a released format needs a bump, a migration and a test.

The CodeRabbit check and the copy of it in `docs/SPEC.md` (C6) are reworded to say this.

## Consequences

Defaults can follow releases without empty version bumps. A format whose serializer leaves defaults out would not be covered by this exception, because files written by it would change meaning; such a change still needs a bump.
