# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub:
[open a private security advisory](https://github.com/padobrik/saddlefit/security/advisories/new).
Do not open a public issue.

You will get an acknowledgement within 7 days. Please include the Saddlefit version, the
steps to reproduce and the impact you observed.

## Scope

In scope:

- **Sandbox escapes**: anything that lets code run by the student, by a tutor-proposed tool
  script or by a task check reach the network, the host filesystem beyond the run workspace,
  host environment variables beyond the allowlist, or run as root.
- **Evaluation-integrity bypasses**: anything that lets the student, the tutor, the generator
  or the fast loop read sealed task data (hidden tests, `check.sh`, reference solutions,
  hidden-check output) or held-out and control outcomes, or lets control results influence a
  decision.
- **Secret exposure**: API keys reaching logs, prompts, run records or the sandbox.
- **Harness store bypasses**: proposals that change files outside the allowlist or change
  budgets and evaluation settings.

Out of scope: weaknesses of the model runner or the Docker engine themselves, and results
on tasks you control.

## Supported versions

Only the latest released minor version receives fixes while Saddlefit is 0.x.
