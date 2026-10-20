# Saddlefit

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

- **Fast — during a task.** When the task's own check fails, the student probes a few harness
  variants locally and keeps what passes; the finding goes on probation in a journal.
- **Middle — overnight.** A tutor diagnoses training failures and proposes one change at a
  time; it is accepted only if it wins on repeated runs over held-out tasks, and pruned by
  ablation once it stops paying for its context.
- **Slow — every few weeks.** Statistics on accepted changes become a ranked playbook, gated by
  an A/B test, until a compact local tutor can take over.

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

## Documentation

- [Specification](docs/SPEC.md) — the build contract
- [Architecture](docs/architecture.md)
- [Architecture decision records](docs/adr/)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

[Apache-2.0](LICENSE)
