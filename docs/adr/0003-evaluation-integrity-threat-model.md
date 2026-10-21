# 3. What "the student reads sealed data" means for evaluation

Date: 2026-10-21

## Status

Accepted

## Context

B10 lists who may see sealed data (hidden tests, `check.sh`, reference solutions, hidden-check
output). The student, the tutor, the generator and the fast loop may not. B10 mechanism 2 also
fixes how evaluation works: the evaluator builds a fresh container from the base snapshot, applies
the student's diff, adds the hidden tests and runs `check.sh`.

The CodeRabbit "Evaluation integrity" pre-merge check fails a change that "lets the student ...
read sealed task data". On the evaluator PR (#75) it reads "the student" as including the code the
student wrote. During evaluation that code runs inside the same pytest process as the hidden tests,
so it could read them. No in-process boundary can prevent that: pytest imports the code under test
from the test modules.

What the evaluator does guarantee:

- the diff is final before any sealed file exists where the student can reach it; the hidden files
  are copied in only after `git apply`, in a fresh container that is discarded afterwards;
- the container has no network, and its only outputs are the check's exit code and stdout; the
  stdout goes to the sealed store only, and the verdict carries no output (required test 13);
- a diff that touches protected paths, hidden-test paths, the evaluator's own files or anything in
  their way is `tampered` and never runs;
- every sealed file carries a canary, and a canary in any prompt aborts the run (required test 2).

So code under test can read every hidden test copied into the container, and could change its
behaviour at run time based on what it reads. It cannot send anything back: neither the hidden
tests' content nor the check's output can travel to the model, the tutor or a report. The only
channel out is the verdict's exit code, duration and timeout flag, plus the aggregates B10 allows
in reports. Such code has to be written before its author has seen any hidden test, so it can only
be a generic attempt to game tests. B10 defends against that with held-out and control sets and
repeated runs (B10 known traps, B12).

## Decision

"The student" in B10 and in the pre-merge check means the student model and everything that
reaches it: prompts, tool results, run records and reports. Code under test may read the hidden
test it is being judged by, inside the evaluation container. That is inherent to running tests, and
B10 mechanism 2 prescribes it.

The pre-merge check is reworded to say this explicitly, keeping every other condition:

> Fail if the change lets the student model (its prompts, tool results, run records or reports),
> the tutor, the generator or the fast loop read sealed task data or held-out or control outcomes,
> or lets control results influence any decision. Code under test reading the hidden tests inside
> the evaluation container, as B10 mechanism 2 prescribes, is not such a read, provided the
> check's output goes only to the sealed store and nothing but the verdict's exit code, duration
> and timeout flag reaches the student model, the tutor, the generator or the fast loop. Reports
> may carry what B10 allows them: held-out aggregates, and control outcomes only after
> `report --final`. Pass otherwise.

## Consequences

The check keeps its purpose: any path from sealed data to the model, the tutor, the generator or
the fast loop still fails, and reports stay limited to what B10's table allows them.

The verdict fields that do leave evaluation, exit code, duration and timeout flag, are what the
run record keeps for the hidden check. The duration is a timing side channel: it can tell how long
the hidden tests ran, nothing about what they contain. That risk is accepted. The evaluator PR can merge with the B10 design. Gaming hidden
tests blindly stays a known risk, and the held-out and control sets measure it.
