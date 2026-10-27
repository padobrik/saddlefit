<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/saddlefit-dark.svg">
    <img alt="Saddlefit" src="docs/logo/saddlefit-light.svg" width="300">
  </picture>
</h1>

[![CI](https://github.com/padobrik/saddlefit/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/padobrik/saddlefit/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/padobrik/saddlefit/graph/badge.svg)](https://codecov.io/gh/padobrik/saddlefit)
[![Release](https://img.shields.io/github/v/release/padobrik/saddlefit?include_prereleases&sort=semver)](https://github.com/padobrik/saddlefit/releases)
[![License](https://img.shields.io/github/license/padobrik/saddlefit)](LICENSE)

Saddlefit fits an agent's harness to one compact model, on your own tasks. It adds what
fixes failures, drops what stops helping, and proves each change on tasks the tutor never saw.

The model's weights never change; only the files around it do: system prompt, skills, tool
guidance, tool scripts, loop settings, reasoning mode and an `EXPERIENCE.md` of lessons.

> **Status:** pre-alpha. Built milestone by milestone; see the
> [milestones](https://github.com/padobrik/saddlefit/milestones) and [the spec](docs/SPEC.md).

## Three loops

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/three-loops-dark.svg">
  <img alt="Three loops: the fast loop during a task, the middle loop overnight, the slow loop that improves the tutor itself" src="docs/diagrams/three-loops-light.svg" width="760">
</picture>

- **Fast — during a task.** When the task's own check fails, the student probes a few harness
  variants locally and keeps what passes; the finding goes on probation in a journal.
- **Middle — overnight.** A tutor diagnoses training failures and proposes one change at a
  time; it is accepted only if it wins on repeated runs over held-out tasks, and pruned by
  ablation once it stops paying for its context.
- **Slow — every few weeks.** Statistics on accepted changes become a ranked playbook, gated by
  an A/B test, until a compact local tutor can take over.

<details>
<summary>The fast loop in detail</summary>
<br>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/fast-loop-dark.svg">
  <img alt="Fast loop: a failed check triggers local probes, and a change that helps stays on probation in the journal" src="docs/diagrams/fast-loop-light.svg" width="760">
</picture>

</details>

<details>
<summary>The middle loop in detail</summary>
<br>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/middle-loop-dark.svg">
  <img alt="Middle loop: a change is accepted only if it wins on the training batch and again on held-out tasks" src="docs/diagrams/middle-loop-light.svg" width="760">
</picture>

</details>

<details>
<summary>The slow loop in detail</summary>
<br>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/slow-loop-dark.svg">
  <img alt="Slow loop: a new playbook must beat the old one in an A/B run before the tutor uses it, and a shadow tutor takes over once it is as good as the cloud one" src="docs/diagrams/slow-loop-light.svg" width="760">
</picture>

</details>

## What counts as a compact model

An open-weight model that runs on one consumer machine and meets all four conditions
(`saddlefit doctor` checks them):

1. **Memory** — at 4-bit, weights plus a 32k-token context fit in 24 GB or less.
2. **Active size** — at most about 10B parameters active per token (a 30B-A3B MoE qualifies).
3. **Speed** — roughly 20 tokens per second or more on the target machine.
4. **Tool use** — reliable tool calls, natively or through constrained JSON output.

| Tier | What it is | Typical hardware |
| --- | --- | --- |
| S | dense, 1–4B | 8–16 GB laptops, CPU only |
| M | dense, 4–10B, or a looped model | 16–18 GB Macs, GPUs with 8–12 GB |
| L | MoE, up to ~35B total and ~5B active | 32–36 GB Macs, 24 GB GPUs |

## Quickstart

Requires Python 3.12+, Docker and a local model runner such as [Ollama](https://ollama.com).
Ollama serves models with the context window set on the server, not the one a request asks
for, and its default depends on the version and the memory available. Start it with
`OLLAMA_CONTEXT_LENGTH=32768` (or what `student.context_length` says); `doctor` checks the
window it actually serves.

```bash
uv tool install saddlefit
cd your-python-project
saddlefit init --model qwen3:8b
saddlefit doctor
saddlefit tasks mine
saddlefit baseline
saddlefit fit
saddlefit report
```

Every command also accepts `--json`.

### More tasks than your history holds

A fit needs tasks the student solves sometimes. Two commands add them, both through the
generator model in `saddlefit.yaml`, within its `max_calls` and `max_usd` (fill in its prices
first):

```bash
saddlefit tasks generate --type feature --count 5   # or --type bugfix
saddlefit tasks mutate --count 5                     # injected bugs that break existing tests
```

A task is kept only if its hidden tests fail without the reference solution and pass with it
in a fresh container, and if the current student then passes it in one to four of five runs.
Generated and mutated tasks join the training and held-out splits, never control.

### Watching a fit

On a terminal `fit` shows one framed status line (round, candidate, stage, runs done,
spending); the frame turns amber when no run has finished for longer than a run may take.
From another shell, `saddlefit status` says whether a fit is running, where it is or where
it stopped, and what fits have cost.

### Work mode

```bash
saddlefit run "Add a --dry-run flag to the export command" --check "pytest -q tests/test_export.py"
```

The student works in a copy of your working tree. If the check fails, the fast loop probes a
few mechanics from the starter playbook, combines the ones that help and checks once more.
The answer is a patch in `.saddlefit/answers/`; your files are not touched. A change the fast
loop needed goes to `.saddlefit/journal/` on probation: it enters the harness only if a later
fit accepts it on held-out tasks. `run` exits 0 when the check passes and 4 when it still
fails.

## Where it comes from

The middle loop follows AutoSaddler; the fast loop and the improver that improves itself
follow Harness². What Saddlefit adds is the focus on one compact local model, measured on your
own tasks, with a harness that stays small.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/origins-dark.svg">
  <img alt="Ideas from AutoSaddler and Harness², and what Saddlefit adds" src="docs/diagrams/origins-light.svg" width="760">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/landscape-dark.svg">
  <img alt="Map of related approaches by when the harness improves and who executes; Saddlefit learns ahead of time for a small local model" src="docs/diagrams/landscape-light.svg" width="760">
</picture>

See [the spec, Part D](docs/SPEC.md#part-d--background) for sources and a fuller comparison.

## Documentation

- [Specification](docs/SPEC.md) — the build contract
- [Architecture](docs/architecture.md)
- [Architecture decision records](docs/adr/)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

[Apache-2.0](LICENSE)
