# Implementation notes

Assumptions taken where the spec leaves a detail open. Deviations from the spec are ADRs in
`docs/adr/`, not notes.

- **Repository owner.** `OWNER` in the spec is `padobrik`; it appears in CODEOWNERS, URLs and the
  sandbox image name.
- **Code of Conduct contact.** The Contributor Covenant placeholder is filled with a private
  message to the maintainer's GitHub account rather than an e-mail address.
- **Coverage badge.** The README coverage badge uses Codecov with OIDC upload from one CI matrix
  cell. The upload does not gate CI; the 85% coverage gate is enforced by pytest-cov in every
  matrix cell. The badge stays "unknown" until the owner enables the repository on Codecov.
- **PR title check.** `pr-title.yml` checks the title with a shell regex instead of a third-party
  action, so the job needs no token.
- **Docker test skipping.** Docker tests skip locally when no daemon is reachable; CI sets
  `SADDLEFIT_REQUIRE_DOCKER=1`, which turns a missing daemon into a failure.
- **Line coverage.** "Line coverage" is measured without branch coverage.
- **Release PRs.** release-please opens its PR with the workflow token, which does not trigger
  CI; the PR is closed and reopened once by a maintainer to run the checks, instead of adding a
  personal access token.
- **Live smoke model.** Nightly live tests use `qwen3:0.6b` through the `ollama/ollama` service
  container.
- **max_steps vs. timeout.** "max_steps can never stretch a run past evaluation.timeout_seconds"
  is enforced by clamping `max_steps` to `timeout_seconds` (at least one second per step); the
  run's wall clock is also bounded by the sandbox's per-run deadline.
- **Allowlist paths.** Harness paths are judged on their raw segments: empty, `.` and `..`
  segments are refused rather than normalized.
- **Doctor size estimates.** Memory is 4-bit weights (0.5 byte per parameter, plus 10% for
  scales and embeddings) plus an fp16 KV cache for a 32k context, from the layer count, KV heads
  and key length Ollama reports. A mixture of experts counts `parameters × experts used ÷
  experts` as active, a rough estimate in the model's favour. A runner without that metadata
  gets tier "unknown" rather than a guess.
- **Doctor speed and exit codes.** Speed is measured after one unmeasured warm-up call, so model
  loading does not count. `doctor` exits 1 when the model is not compact or its tier is unknown,
  and 3 when Docker or the runner is unusable.
- **Contamination flag.** B10 does not name a field. `meta.yaml` stores the history commit's date
  as `committed`, and a task counts as contaminated when that date is older than the configured
  `student.release_date`. The flag is computed on every read, so `tasks list` follows a changed
  model or release date without re-mining. Without a release date nothing is flagged.
- **Task text for history tasks.** The spec does not say how a commit becomes `task.md`. It is
  an instruction: the commit subject without its Conventional Commit prefix, a sentence saying
  the change is not in the repository yet, and the commit body without pull-request template
  sections (`Linked issue`, `Checklist`, `Evaluation-integrity impact`, `Test plan`), checkbox
  lines, issue-closing lines and sign-off trailers. A pull request's description otherwise reads
  as a report that the work is done.
- **Visible check for history tasks.** Every history task gets one: the parent's versions of the
  commit's test files, else the directories of the new test files that already hold tests in the
  parent, else the whole suite, run with the same command as the hidden check
  (`PYTHONPATH=src:.`), so `finish_gate.run` is not needed for mined tasks. When the parent has no
  tests at all, pytest's "no tests collected" (exit 5) counts as a pass.
