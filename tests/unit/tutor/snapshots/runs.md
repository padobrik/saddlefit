The student failed the 2 training runs below. Diagnose them and call `propose`
with one change to the harness.

## Run 01JB2Y8Q4V9N1K3D7M5R6T0W2X

- Verdict: fail, primary tag `gave_up`, tags: `gave_up`
- Diff: 1 files, +3 -1

### Task

Fix the ISO date parser.

### Last 9 events

1. model step 1: read_file
2. tool read_file: ok, 812 chars
3. model step 2: edit_file
4. tool edit_file: error, 40 chars
5. loop warning: edit_file x3
6. compaction: dropped 2 tool outputs
7. finish gate: visible check failed
8. model step 3: no tool calls, 1 malformed
9. error gave_up: finished without a passing check

### Visible check output, last 2000 characters

```
FAILED tests/test_dates.py::test_iso - ValueError
```

## Run 01JB2Y8Q4V9N1K3D7M5R6T0W2Y

- Verdict: fail, primary tag `gave_up`, tags: `gave_up`
- Diff: no changes

### Task

Fix the ISO date parser.

### Last 2 events

1. model step 1: read_file
2. tool read_file: ok, 812 chars

### Visible check output, last 2000 characters

```
(the student never ran it)
```
