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
- **Release PRs and publishing.** release-please works with the workflow token, and events
  from that token start no other workflow. Its release PR is therefore closed and reopened once
  by a maintainer to run the checks, instead of adding a personal access token. For the same
  reason a Release it creates does not trigger `publish.yml` by itself: release-please dispatches
  `publish.yml` for the new tag (`workflow_dispatch` is the one event that token may start), and
  a maintainer can dispatch it again for an existing tag. A dispatch must name a tag with a
  published Release and checks out exactly `refs/tags/<tag>`. PyPI never replaces a file, so a
  repeated dispatch skips files already uploaded (`skip-existing`) and re-pushes only the sandbox
  image; a broken package needs a new version. Tags are plain `vX.Y.Z`, matching the protected
  `refs/tags/v*` ruleset.
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
- **Student call timeout.** The spec's local default of 120 s per call is too short for a
  4,096-token reply from a model writing about 24 tokens a second. `baseline` derives the
  timeout from `doctor.json`: a full reply at the measured speed, times 1.5, never below 120 s.
  Without a record for the configured model the default stays, so run `saddlefit doctor` first.
- **Environment images.** B11 asks for dependencies to be installed into a derived image in a
  separate build step but names no command, so `tasks mine` and `baseline` build it on demand.
  The dependencies come from `uv.lock` (exported with all groups) when it exists, else the
  `requirements*.txt` files, else `pyproject.toml` (compiled with all extras). They are installed
  as root on top of the sandbox image, and the image ends as the sandbox user. The tag is a hash
  of the base image and those files, so an unchanged repository reuses its image, and the result
  is recorded in `.saddlefit/environment.json`, which is removed when the repository no longer
  has dependency files. The build sends no registry credentials: the base image is local and
  nothing is pulled. Installing the dependencies runs their build hooks with network access, as
  installing the project by hand would; the files are the repository owner's, and the student
  never takes part in this step. The build gets the sandbox's memory limit and a 30-minute HTTP
  timeout. The Docker API has no CPU cap or daemon-side deadline for a build, so neither is set.
- **Default sandbox image.** `sandbox.image` defaults to
  `ghcr.io/padobrik/saddlefit-sandbox:<installed version>`, so it never needs a hand edit: every
  release publishes the image under the same tag in the same run. `init` writes the value into
  `saddlefit.yaml`, so an existing project keeps the image it started with until its owner
  changes it. The generated schema shows no default for it.
- **Prompt-cache prices.** Prompt caching is not in the spec. The tutor's repeated parts (system
  prompt, harness files) are worth caching, and cache writes and reads are priced apart from
  plain input, with different prices for 5-minute and 1-hour writes. So
  `tutor.prices_usd_per_mtok` also carries `cache_write_5m`, `cache_write_1h` and `cache_read`,
  defaulting to 0.0 like the other prices. They arrived in `saddlefit.yaml` v2; a v1 file is
  migrated by its version alone, and its cache prices stay 0.0 until the user fills them in.
- **Inconclusive tasks in the gates.** A task whose environment errors left too few runs to
  decide it (as in `baseline`) is not solved for the batch gate, and neither a win nor a loss
  for the held-out gate, but it still counts in the held-out n, which can only raise the
  margin a candidate needs.
- **Gate settings.** B12 makes every default configurable. The held-out margin (0.1) and loss
  limit (1) are parameters of the decision functions; they move into `saddlefit.yaml` when a
  command first needs them, as a format change of their own.
- **Earlier proposal.** The tie-break's "earlier proposal" is the order in which the tutor
  produced the candidates in a round, not the proposal ID.
- **Anthropic provider.** Current Claude models refuse a forced `tool_choice`, so the tutor's
  proposal tool is offered with `tool_choice: auto` and `strict: true`, and the prompt asks for
  the call. Sampling settings (`temperature`, `seed`) are not sent; `reasoning` maps to
  `output_config.effort` (`off` → `low`, since thinking cannot be disabled), and no setting
  leaves the API's default. The system prompt is cached with the 1-hour TTL: batch reruns
  separate a round's tutor calls by more than five minutes. Refused calls fall back on the
  server (`fallbacks: "default"`); the reply's `raw.model` names the model that answered.
  Retries are Saddlefit's (B5), so the SDK makes one attempt per call.
- **Tool script linting.** B8 asks new tool scripts to pass ruff (Python) or shellcheck (shell)
  and to run only inside the sandbox, so the sandbox image ships both linters and the scripts
  are linted there, in a container with no network, with ruff's `--isolated` so no file in the
  workspace can switch rules off. Every `.py` and `.sh` file a proposal adds or changes is
  linted, in `skills/` too; `tools/` may hold nothing else.
- **Tutor prompt.** The stable part (instructions, constraints, harness files, lessons and
  playbook) is the system prompt, which the provider caches; the failed runs are the user
  message. A run's events are rendered one line each from the run record, which stores tool
  output sizes, not tool output; the visible check output is the last finish-gate output of
  the student's own run. A proposal citing a run it was not shown is refused.
- **Patch format.** Proposals must be git-style diffs (`diff --git` headers): the store finds
  a patch's paths from those headers, and a plain unified diff was refused as changing no
  files. Patches apply with `git apply --recount`, because models miscount hunk lines; the
  context lines must still match exactly. Both came from a live tutor call.
- **Lessons.** A lesson is labelled with the student model, since the student's context keeps
  the lessons for its own model (`(model: …)`), and it is stored as one `- ` line: the
  context takes each such line as one lesson, so a second physical line would be lost. The
  tutor may write up to two lines; they are joined. A lesson reaches EXPERIENCE.md as a
  validated proposal that is accepted at once, with no gates, since B13 makes reflection
  part of every decision. Its evidence must be among the decided proposal's evidence runs,
  and the reflection prompt carries no held-out results: B10 lets the tutor see held-out
  aggregates, but a lesson lands in EXPERIENCE.md, which the student reads, and the student
  may see no held-out data. So lessons cite training numbers only, unlike the example in A9.
- **Change records.** One record per candidate that reached the training batch; a proposal
  refused by validation never became a candidate and has none. `harness_commit` is the
  candidate's commit, and `held_out` is empty when the batch gate already rejected it.
  Records hold held-out aggregates only, never per-task outcomes.
- **Middle loop round.** A7 asks for batches of 5–10 training tasks; a round takes 6, as a
  window over the sorted training tasks that moves on every round. Up to 3 candidates per
  round, each from its own tutor call on the same failures, within the round's call and spend
  budget; reflection calls count toward it. The tie-break's "added context tokens" is the
  proposal's own estimate (`context_tokens_delta`). The loop reaches the student through
  `Practice`, which returns training outcomes per task and held-out results only as one
  aggregate per comparison, and has no way to run control tasks.
- **`fit`.** Each round runs the student through the baseline's runner with the harness at a
  given commit exported and its `loop.yaml` clamped, so candidates run exactly as `main`
  would. The current harness's held-out results are kept per commit, so each comparison
  reruns only the candidate. Proposed scripts are linted in `sandbox.image`, which ships the
  linters, not in the project's environment image. `fit` needs the `anthropic` tutor and
  warns when tutor prices are unset, since `max_usd_per_round` cannot stop a round then.
- **Fit state.** `.saddlefit/fit.json` counts finished middle-loop rounds, so each `fit`
  continues with the next training batch instead of starting again at the first; a round
  that fails is not counted.
- **One fit at a time.** B3 says parallel fitting sessions never share mutable state. The
  minimal reading: `fit` holds an exclusive `.saddlefit/fit.lock` for its whole run, so two
  sessions never run at once on a project. A crash can leave the lock behind; the error names
  the file to remove. `fit` also refuses a suite without held-out tasks, where no candidate
  could pass the held-out gate.
- **Ablation.** Each active change (accepted, not pruned since) is tested newest first on a
  branch that reverts its accept merge from `main`, against `main` on every training task and
  on the held-out tasks. A change that a later accepted change builds on cannot be reverted
  alone; it is skipped and stays until the later one goes. A pruned change is reverted on
  `main` with the reason in the commit message and gets a `pruned` record whose training
  counts are with (before) and without (after) it. The trigger state, accepted changes since
  the last ablation and the student model and digest of the last fit, lives in `fit.json`;
  a new digest under the same model name counts as a model change when both are known.
- **`prune`.** Ablation on demand, as `fit` would run it: only the student runs, so `prune`
  needs no tutor and no API key. It holds the fit lock and resets the count of accepted
  changes since the last ablation; the round count and student model stay as they are.
- **Statistics.** The paired bootstrap resamples tasks with replacement, keeping each task's
  before and after outcomes together, and takes the 2.5th and 97.5th percentiles of the
  10,000 mean differences; the seed is fixed (20261021). It lives in `saddlefit.eval`, since
  it needs per-task outcomes; callers get only the interval.
- **Progress report.** `report` compares the harness the latest baseline used with the current
  `main`. Runs are grouped by the git tree of their harness commit, since a candidate and the
  merge that accepted it hold the same files; tasks are decided as in the baseline and
  compared only where decided under both, the rest counted as not measured. The current
  harness's failure profile comes from its runs. Tutor spending accumulates in `fit.json`,
  failed rounds included. A task run more than once under a harness is judged on its latest
  `runs_per_task` runs; every suite task not decided under both counts as not measured.
  Only runs with the baseline's student model, digest and suite hash count, and `report`
  refuses when the model, the suite or the evaluation settings changed since the baseline.
  Control evaluations stay 0 until `report --final` exists.
- **Control log.** `.saddlefit/control.json` logs each `report --final`: the frozen tag, its
  commit, the baseline harness commit it was compared with, the time and the number of
  control tasks. The count every report prints is the number of entries. Appends hold
  `control.lock` across the read and the atomic replace, so no evaluation is lost; times
  are timezone-aware UTC. `BaselineRunner`
  runs control only when its caller passes the split explicitly, which only `report --final`
  does.
- **`report --final`.** The only path that runs control: under the fit lock it rereads the
  fit state, runs the control tasks under the baseline's harness and under the current `main`
  (once if both hold the same files), then tags that commit `fit-<yyyy-mm-dd>-<n>`, so a
  failed evaluation leaves no tag, logs the evaluation, and writes the progress
  report plus control before and after, the delegation map and the context efficiency. The
  baseline never ran control, so its control numbers come from this run. Control runs go
  through `ControlRunner`, separate from the `Practice` that `fit` uses.
- **Skill export.** The spec names the goal ("the fit as SKILL.md for other agents") but not
  the layout. `export --format skill` writes Agent Skills: `saddlefit-<project>/SKILL.md`
  with the system prompt, the tool guidance, the finish-gate rule from `loop.yaml` and the
  lessons for the student model, its tool scripts in `scripts/`, and each harness skill
  copied as it is; the generated skill takes the next free name if a harness skill has its
  name. `export.json` (`schema_version: 1`) records the project, model, harness commit and
  skills. The export is scanned for canaries before it is kept (a hit exits 2 and
  writes nothing), and it never overwrites a directory. `agents-md` and `hooks` come later.
- **Interrupted runs.** A crash or power loss can leave a run record without its summary
  line, or with its last line cut off (every complete line ends in a newline). Reading one
  raises `UnfinishedRun`; `report` leaves such runs out, since they have no outcome. Any
  other malformed record still fails the read.
- **Served context window.** Ollama's OpenAI endpoint ignores a requested window, so the
  server's `OLLAMA_CONTEXT_LENGTH` decides it, with a default that depends on the Ollama
  version and the memory available; longer prompts are cut without an error. `doctor` and
  every command that starts the student read the loaded model's window from `/api/ps`
  (loading it with a one-token reply when needed) and exit 3 when it is smaller than
  `student.context_length`. Runners that do not report it pass unchecked.
- **Tool call arguments in run records.** Run records v2 keep each tool call's arguments, so
  a failed `edit_file` can be read back. Strings are redacted like log lines; strings longer
  than 300 characters keep their head and tail with a note on the cut; numbers, booleans and
  nulls are kept as they are, and anything else is written as JSON text. The tool's output
  stays out, as before: only its size is recorded. The tutor's digest of a run does not show
  the arguments yet.
- **Generator budget.** Config v3 gives the generator `max_calls` (10), `max_usd` (5.0) and
  `prices_usd_per_mtok`, all for one `tasks generate` command, which stops before a call that
  could pass either limit. A v2 file reads as v3 with these defaults; as for every format,
  only the current and the previous version are read, so a v1 file is refused.
- **`tasks generate`.** One attempt is one generator call: the task is sealed, must fail
  without its reference patch and pass with it, then runs five times with the current student
  and is kept only at one to four passes (20-80%). The generator sees the file list, the
  README, a random sample of non-protected text files within 40,000 characters and up to
  three training task texts. The command refuses to start without generator input and output
  prices, since `generator.max_usd` could not stop anything; it stops before a call whose
  worst case (the prompt at four characters a token plus a full 8,192-token reply) could pass
  it. Calibration runs go to `.saddlefit/calibration/`, outside baseline and fit records.
  Generated tasks start from the repository's current `HEAD`.
- **Fit spending and refusals (#113).** `run_rounds` saves `fit.json` after every paid tutor
  call, so a crash or power loss loses at most the call in flight. Each refused attempt keeps
  its reason as `tutor: …` (the call failed or its answer was unusable) or `proposal: …` (a
  disallowed file, a failed linter, a patch that does not apply); `fit` prints them under the
  round line, `fit.json` v2 counts them by kind across fits, and the progress report shows
  the counts.
