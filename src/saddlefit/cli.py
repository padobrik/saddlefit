"""Command-line interface."""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.live import Live

from saddlefit import __version__
from saddlefit.config import Config, ConfigError, SandboxConfig, StudentConfig, load_config
from saddlefit.doctor import doctor_for, render
from saddlefit.errors import EnvironmentFailure, SaddlefitError, UserError
from saddlefit.loops.ablation import AblationResult
from saddlefit.loops.baseline import RunOutcome
from saddlefit.loops.generate import GenerationReport
from saddlefit.loops.middle import RoundReport
from saddlefit.orbit import OrbitStatus
from saddlefit.project import Project
from saddlefit.providers.anthropic import AnthropicClient
from saddlefit.providers.base import ModelClient, ModelInfo, ProviderError
from saddlefit.providers.openai_compat import OpenAICompatibleClient, context_window_problem
from saddlefit.storage.progress import ProgressRecord
from saddlefit.tasks.generator import TASK_KINDS
from saddlefit.tasks.mining import Runner, SandboxRunner


class ExitCode(IntEnum):
    """Process exit codes (B14)."""

    OK = 0
    USER_ERROR = 1
    INTEGRITY_VIOLATION = 2
    ENVIRONMENT_ERROR = 3
    NOT_SOLVED = 4  # run: the check still fails; the answer is the best attempt


app = typer.Typer(
    name="saddlefit",
    help="Fit an agent's harness to one compact model, on your own tasks.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON output.")
    ] = False,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    """Saddlefit command group."""
    ctx.obj = {"json": json_output}
    if version:
        if json_output:
            typer.echo(json.dumps({"version": __version__}))
        else:
            typer.echo(f"saddlefit {__version__}")
        raise typer.Exit(ExitCode.OK)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


DEFAULT_CONFIG = Path(".saddlefit") / "saddlefit.yaml"

tasks_app = typer.Typer(help="Mine, list and show tasks.", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")


def _emit(ctx: typer.Context, data: object, text: str) -> None:
    """Print ``data`` as JSON under ``--json``, else the human-readable ``text``."""
    root_ctx = ctx.find_root()
    if root_ctx.obj and root_ctx.obj.get("json"):
        typer.echo(json.dumps(data, default=str))
    else:
        typer.echo(text)


def _project(repo: Path | None = None) -> Project:
    return Project((repo or Path.cwd()).resolve())


def _docker() -> Any:
    import docker  # noqa: PLC0415 - only the commands that run containers need the SDK

    try:
        client = docker.from_env()
    except docker.errors.DockerException as exc:
        raise EnvironmentFailure(f"Docker is not available: {exc}") from exc
    try:
        client.ping()
    except docker.errors.DockerException as exc:
        client.close()
        raise EnvironmentFailure(f"Docker is not available: {exc}") from exc
    return client


def mining_runner(project: Project) -> Runner:
    """Candidates' tests run in the sandbox, never on the host (B11)."""
    config = project.config()
    docker_client = _docker()
    # the candidates' tests need the repository's dependencies, which the sandbox cannot fetch
    image = project.environment_image(docker_client)
    return SandboxRunner(docker_client, image, config.sandbox)


@app.command()
def init(
    ctx: typer.Context,
    model: Annotated[str | None, typer.Option(help="Student model, e.g. qwen3:8b.")] = None,
    repo: Annotated[Path | None, typer.Option(help="Repository root (default: here).")] = None,
) -> None:
    """Create .saddlefit/: configuration, harness store and the split salt."""
    result = _project(repo).init(model)
    _emit(ctx, result, f"initialised {result['state']} for {result['model']}")


@tasks_app.command("mine")
def tasks_mine(
    ctx: typer.Context,
    limit: Annotated[int | None, typer.Option(help="Scan at most N commits.")] = None,
    since: Annotated[str | None, typer.Option(help="Only commits since YYYY-MM-DD.")] = None,
) -> None:
    """Turn git history into tasks: tests that fail before a commit and pass after."""
    try:
        since_date = date.fromisoformat(since) if since else None
    except ValueError as exc:
        raise UserError(f"--since must be YYYY-MM-DD: {since}") from exc
    project = _project()
    report, metas = project.mine(mining_runner(project), limit=limit, since=since_date)
    data = {**report.summary(), "tasks": [m.id for m in metas]}
    _emit(
        ctx,
        data,
        f"scanned {report.scanned} commits, {len(metas)} tasks; rejected: {dict(report.rejected)}",
    )


def _generator_ready(config: Config, command: str) -> None:
    """Refuse before any spending: the generator must be real and priced."""
    if config.generator.provider != "anthropic":
        raise UserError(
            f"{command} needs the anthropic generator provider, not {config.generator.provider!r}"
        )
    prices = config.generator.prices_usd_per_mtok
    if not (prices.input and prices.output):
        # without prices, generator.max_usd could not stop anything
        raise UserError(
            "generator prices are not set; fill in generator.prices_usd_per_mtok "
            "(input and output) from your price list"
        )


def _generation_output(ctx: typer.Context, report: GenerationReport, count: int) -> None:
    data = {
        "kept": report.kept,
        "attempts": [vars(a) for a in report.attempts],
        "calls": report.calls,
        "usd": round(report.usd, 4),
        "stopped": report.stopped,
    }
    lines = [
        f"{'kept' if a.kept else 'dropped'} {a.task_id or '-'}: {a.reason}" for a in report.attempts
    ]
    lines.append(f"kept {len(report.kept)} of {count}; {report.calls} calls, ${report.usd:.2f}")
    if report.stopped:
        lines.append(f"stopped: {report.stopped}")
    _emit(ctx, data, "\n".join(lines))


@tasks_app.command("mutate")
def tasks_mutate(
    ctx: typer.Context,
    count: Annotated[int, typer.Option(min=1, help="How many tasks to keep.")] = 1,
) -> None:
    """Inject bugs that break existing tests; keep those the student solves in 20-80% of 5 runs."""
    project = _project()
    config = project.config()
    _generator_ready(config, "tasks mutate")
    model_info, model_client = _student(project)
    report = project.mutate_tasks(
        count=count,
        docker_client=_docker(),
        generator_client=AnthropicClient(config.generator.model),
        model_client=model_client,
        model_info=model_info,
        runner=mining_runner(project),
    )
    _generation_output(ctx, report, count)


@tasks_app.command("generate")
def tasks_generate(
    ctx: typer.Context,
    type_: Annotated[str, typer.Option("--type", help="Task type: feature or bugfix.")],
    count: Annotated[int, typer.Option(min=1, help="How many tasks to keep.")] = 1,
) -> None:
    """Write tasks with the generator; keep those the student solves in 20-80% of 5 runs."""
    if type_ not in TASK_KINDS:
        raise UserError(f"unknown task type {type_!r}; known: {', '.join(sorted(TASK_KINDS))}")
    project = _project()
    config = project.config()
    _generator_ready(config, "tasks generate")
    model_info, model_client = _student(project)
    report = project.generate_tasks(
        kind=type_,
        count=count,
        docker_client=_docker(),
        generator_client=AnthropicClient(config.generator.model),
        model_client=model_client,
        model_info=model_info,
    )
    _generation_output(ctx, report, count)


@tasks_app.command("list")
def tasks_list(ctx: typer.Context) -> None:
    """List tasks with their type and split."""
    project = _project()
    metas = project.tasks()
    # judged against the configured model on every read, so a new release date applies at once
    release = project.config().student.release_date
    rows = [
        {"id": m.id, "type": m.type, "split": m.split, "contaminated": m.contaminated(release)}
        for m in metas
    ]
    lines = [
        f"{m.split:8} {m.type:8} {m.id}{'  (contaminated)' if m.contaminated(release) else ''}"
        for m in metas
    ]
    _emit(ctx, rows, "\n".join(lines) or "no tasks")


@tasks_app.command("show")
def tasks_show(ctx: typer.Context, task_id: str) -> None:
    """Show a task's public text and metadata (never its sealed part)."""
    project = _project()
    text = project.task_text(task_id)
    meta = next(m for m in project.tasks() if m.id == task_id)
    _emit(ctx, {"meta": meta.model_dump(mode="json"), "task": text}, text)


@app.command()
def doctor(
    ctx: typer.Context,
    config: Annotated[
        Path, typer.Option(help="Project config; without one, the defaults are checked.")
    ] = DEFAULT_CONFIG,
) -> None:
    """Check Docker, the runner, speed, tool calling and the compact-model tier (A3)."""
    try:
        loaded = load_config(config) if config.exists() else None
    except ConfigError as exc:
        raise UserError(str(exc)) from exc
    student = loaded.student if loaded else StudentConfig()
    sandbox = loaded.sandbox if loaded else SandboxConfig()
    report = doctor_for(student, sandbox)
    # the report comes first, so a failed write below never hides the checks
    if ctx.obj["json"]:
        typer.echo(json.dumps(report.as_json(), indent=2))
    else:
        typer.echo(render(report))
    if loaded is not None and report.record is not None:
        # B5: the probe result is stored with the model digest, for tool_mode "auto"
        record = config.parent / "doctor.json"
        try:
            record.write_text(report.record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise EnvironmentFailure(f"cannot write {record}: {exc}") from exc
    if not report.environment_ok:
        raise EnvironmentFailure("Docker or the model runner is not usable; see the checks above")
    if not report.compact:
        raise typer.Exit(ExitCode.USER_ERROR)


def progress_line(run: RunOutcome, number: int) -> str:
    """One line per finished run; held-out runs never show their task or outcome (B10)."""
    if run.split == "held_out":
        return f"run {number:3} held_out  (outcome kept for the report's aggregates)"
    return f"run {number:3} {run.split:9} {run.classification.primary:20} {run.task_id}"


@app.command()
def baseline(
    ctx: typer.Context,
    limit: Annotated[int | None, typer.Option(help="Run at most N tasks.")] = None,
) -> None:
    """Measure the student on training and held-out tasks; control is never run."""
    project = _project()
    model_info, model_client = _student(project)
    finished = 0

    def progress(run: RunOutcome) -> None:
        nonlocal finished
        finished += 1
        if not (ctx.find_root().obj or {}).get("json"):
            typer.echo(progress_line(run, finished))

    report, markdown = project.baseline(
        docker_client=_docker(),
        model_client=model_client,
        model_info=model_info,
        limit=limit,
        on_run=progress,
    )
    _emit(ctx, report, markdown.read_text(encoding="utf-8"))


def _student(project: Project) -> tuple[ModelInfo, Callable[[], ModelClient]]:
    """The student model as seen once, and a factory for its clients."""
    config = project.config()
    # the running model's digest decides whether doctor's measurements still apply
    probe = OpenAICompatibleClient(base_url=config.student.base_url, model=config.student.model)
    model_info = probe.info()  # read once: timeout, tool mode and report use the same model
    try:
        problem = context_window_problem(
            probe.served_context_length(), config.student.context_length
        )
    except ProviderError as exc:
        raise EnvironmentFailure(f"the student model could not be loaded: {exc}") from exc
    if problem is not None:
        raise EnvironmentFailure(problem)
    timeout_s = project.student_timeout(model_info.digest)

    def model_client() -> ModelClient:
        return OpenAICompatibleClient(
            base_url=config.student.base_url,
            model=config.student.model,
            context_length=config.student.context_length,
            timeout_s=timeout_s,
        )

    return model_info, model_client


def _framed(quiet: bool) -> bool:
    """The animated frame only on a terminal, never with --json."""
    return not quiet and sys.stdout.isatty()


def _shown(record: ProgressRecord) -> tuple[object, ...]:
    # what earns a plain line: a stage change or a finished run, not a batch's size alone
    return (record.round, record.stage, record.candidate, record.done)


def progress_change(previous: ProgressRecord | None, record: ProgressRecord) -> tuple[bool, bool]:
    """(a run finished, something a plain line shows changed) from one update to the next."""
    if previous is None:
        return False, True
    # only a finished run resets the stall timer; a stage change alone is no progress
    return record.done > previous.done, _shown(previous) != _shown(record)


def progress_status(record: ProgressRecord) -> str:
    """``round 1/2 · candidate 1/3 · held-out 4/6 · $0.84``, the fit's place in one line."""
    parts = [f"round {record.round}/{record.rounds}"]
    if record.candidate is not None:
        parts.append(f"candidate {record.candidate}/{record.candidates}")
    counter = f" {record.done}/{record.total}" if record.total else ""
    parts.append(f"{record.stage}{counter}")
    parts.append(f"${record.tutor_usd:.2f}")
    return " · ".join(parts)


def fit_progress_line(record: ProgressRecord) -> str:
    """The plain line written when output is not a terminal: a time, then the status."""
    return f"{record.updated_at.astimezone().strftime('%H:%M:%S')} {progress_status(record)}"


def round_line(number: int, report: RoundReport) -> str:
    """One round, as the console shows it."""
    solved = f"{report.train_before}/{len(report.batch)}"
    outcome = f"accepted {report.accepted}" if report.accepted else "nothing accepted"
    return (
        f"round {number}: batch solved {solved}; {len(report.records)} candidates tried, "
        f"{report.refused} refused; {outcome}; {report.lessons} lessons; "
        f"tutor {report.tutor_calls} calls, ${report.tutor_usd:.2f}"
    ) + "".join(f"\n  refused {_printable(reason[:300])}" for reason in report.refusals)


def _printable(text: str) -> str:
    """Escape what a terminal would act on: a reason can quote the tutor or a linter."""
    return "".join(
        c if c.isprintable() else (f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"\\u{ord(c):04x}")
        for c in text
    )


def round_data(report: RoundReport) -> dict[str, Any]:
    """One round, as ``--json`` gives it."""
    return {
        "batch": list(report.batch),
        "train_before": report.train_before,
        "accepted": report.accepted,
        "records": [rec.model_dump(mode="json") for rec in report.records],
        "refused": report.refused,
        "refusals": list(report.refusals),  # "<kind>: <reason>", one per refusal
        "lessons": report.lessons,
        "tutor_calls": report.tutor_calls,
        "tutor_usd": report.tutor_usd,
    }


@app.command()
def fit(
    ctx: typer.Context,
    rounds: Annotated[int, typer.Option(min=1, help="Middle-loop rounds to run.")] = 1,
) -> None:
    """Fit the harness to the student: the middle loop on training and held-out tasks."""
    project = _project()
    config = project.config()
    if config.tutor.provider != "anthropic":
        raise UserError(f"fit needs the anthropic tutor provider, not {config.tutor.provider!r}")
    prices = config.tutor.prices_usd_per_mtok.model_dump()
    # the kinds of tokens a tutor call is billed for; 5-minute cache writes are never made
    unset = [k for k in ("input", "output", "cache_write_1h", "cache_read") if not prices[k]]
    if unset:
        typer.echo(
            f"warning: tutor prices not set ({', '.join(unset)}), so tutor.max_usd_per_round "
            "undercounts spending; fill in tutor.prices_usd_per_mtok from your price list",
            err=True,
        )
    model_info, model_client = _student(project)
    quiet = bool((ctx.find_root().obj or {}).get("json"))
    done = 0

    last: list[ProgressRecord] = []
    orbit = OrbitStatus(stall_after_s=config.evaluation.timeout_seconds)
    live = Live(orbit, refresh_per_second=30, transient=True) if _framed(quiet) else None

    def on_progress(record: ProgressRecord) -> None:
        progressed, changed = progress_change(last[-1] if last else None, record)
        last[:] = [record]
        if live is not None:
            orbit.update(progress_status(record), progressed=progressed)
        elif changed and not quiet:
            # one line per stage change and finished run; spending alone changes nothing shown
            typer.echo(fit_progress_line(record))

    def fit_with(echo: Callable[[str], object]) -> list[RoundReport]:
        def on_round(report: RoundReport) -> None:
            nonlocal done
            done += 1
            if not quiet:
                echo(round_line(done, report))

        return project.fit(
            docker_client=_docker(),
            model_client=model_client,
            tutor_client=AnthropicClient(config.tutor.model),
            model_info=model_info,
            rounds=rounds,
            on_round=on_round,
            on_progress=on_progress,
        )

    if live is None:
        reports = fit_with(typer.echo)
    else:
        with live:  # round lines print above the frame
            # plain text: a refusal reason must not be read as Rich markup
            reports = fit_with(lambda s: live.console.print(s, markup=False, highlight=False))
    data = [round_data(r) for r in reports]
    _emit(ctx, data, f"{len(reports)} rounds; {sum(bool(r.accepted) for r in reports)} accepted")


@app.command()
def run(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="What the student should do.")],
    check: Annotated[
        str | None,
        typer.Option(help="Command that passes when the task is done; loop.yaml's gate if unset."),
    ] = None,
) -> None:
    """Work mode: the student does TASK in a copy of the working tree, with the fast loop."""
    project = _project()
    loop, _ = project.effective_loop()
    command = check or loop.finish_gate.run
    if not command:
        raise UserError("run needs --check: loop.yaml has no finish_gate.run to fall back on")
    model_info, model_client = _student(project)
    work = project.work(
        task_text=task,
        check=command,
        docker_client=_docker(),
        model_client=model_client,
        model_info=model_info,
    )
    result = work.result
    data = {
        "passed": result.passed,
        "answer": str(work.answer),
        "probes": [
            {"run_id": p.run_id, "passed": p.passed, "tags": list(p.tags)} for p in result.probes
        ],
        "variants": [e.id for e in result.helped],
        "journal": str(work.journal) if work.journal else None,
        "stopped": result.stopped,
    }
    verdict = "passed" if result.passed else "FAILED (best attempt)"
    lines = [f"{verdict}: `{command}` after {len(result.probes)} runs"]
    if result.helped:
        lines.append(f"with playbook variants: {', '.join(e.id for e in result.helped)}")
    if work.journal:
        status = "on probation" if result.passed else "rejected: the composed change failed"
        lines.append(f"journal ({status}): {work.journal}")
    apply = f"git apply {shlex.quote(str(work.answer))}"
    lines.append(f"answer: {work.answer}  (apply with: {apply})")
    _emit(ctx, data, "\n".join(lines))
    if not result.passed:
        raise typer.Exit(ExitCode.NOT_SOLVED)


@app.command()
def status(ctx: typer.Context) -> None:
    """Show where a running or interrupted fit is, and what fits have cost so far."""
    data = _project().status()
    _emit(ctx, data, status_text(data, datetime.now(UTC)))


def _ago(then: str, now: datetime) -> str:
    minutes = int((now - datetime.fromisoformat(then)).total_seconds() // 60)
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


def status_text(data: dict[str, Any], now: datetime) -> str:
    """``saddlefit status`` for people; ``--json`` gives ``data`` itself."""
    totals = (
        f"all fits: {data['rounds_done']} rounds, {data['tutor_calls']} tutor calls, "
        f"${data['tutor_usd']:.2f}"
    )
    if data["state"] == "idle":
        return f"idle; {totals}"
    record = ProgressRecord.model_validate(data["progress"])
    raw = data["progress"]
    when = f"started {_ago(raw['started_at'], now)}, last update {_ago(raw['updated_at'], now)}"
    this = f"tutor this fit: {record.tutor_calls} calls, ${record.tutor_usd:.2f}; {totals}"
    if data["state"] == "running":
        return f"fit running (pid {record.pid}), {progress_status(record)}\n{when}\n{this}"
    return (
        f"fit interrupted (pid {record.pid} is gone) at {progress_status(record)}\n{when}\n"
        f"{this}\nif no fit runs, .saddlefit/fit.lock can be removed"
    )


def ablation_line(result: AblationResult) -> str:
    """One tested change, as the console shows it."""
    if result.skipped is not None:
        return f"skipped {result.proposal_id}: {result.skipped}"
    verdict = "pruned" if result.pruned else "kept"
    train = result.train.net if result.train else 0
    held_out = result.held_out.net if result.held_out else 0
    return (
        f"{verdict} {result.proposal_id} "
        f"(without it: training net {train:+d}, held-out net {held_out:+d})"
    )


@app.command()
def prune(ctx: typer.Context) -> None:
    """Ablate now: test every accepted change and drop those that do not help."""
    project = _project()
    if project.nothing_to_prune():
        _emit(ctx, [], "no accepted changes to test")  # no model runner or Docker needed
        return
    model_info, model_client = _student(project)
    results = project.prune(
        docker_client=_docker(), model_client=model_client, model_info=model_info
    )
    lines = [ablation_line(r) for r in results] or ["no accepted changes to test"]
    data = [
        {
            "proposal_id": r.proposal_id,
            "pruned": r.pruned,
            "skipped": r.skipped,
            "train_net": r.train.net if r.train else None,
            "held_out_net": r.held_out.net if r.held_out else None,
        }
        for r in results
    ]
    _emit(ctx, data, "\n".join(lines))


@app.command(name="export")
def export_command(
    ctx: typer.Context,
    format: Annotated[str, typer.Option("--format", help="Export format: skill.")] = "skill",
    out: Annotated[Path | None, typer.Option(help="Directory to create.")] = None,
    ref: Annotated[str, typer.Option(help="Harness commit or tag to export.")] = "main",
) -> None:
    """Export the fitted harness for other agents; `skill` writes Agent Skills."""
    if format != "skill":
        raise UserError(f"export format {format!r} is not available yet; use --format skill")
    folders = _project().export_skill(out, ref)
    data = {"format": format, "ref": ref, "skills": [str(f) for f in folders]}
    _emit(ctx, data, "\n".join(f"wrote {f}" for f in folders))


@app.command()
def report(
    ctx: typer.Context,
    final: Annotated[
        bool,
        typer.Option(
            "--final",
            help="Freeze the harness and evaluate control once; logged and counted.",
        ),
    ] = False,
) -> None:
    """Report progress: the baseline harness against the current one, training and
    held-out aggregates only; control appears only in ``report --final``."""
    project = _project()
    if final:
        model_info, model_client = _student(project)
        data, markdown = project.final_report(
            docker_client=_docker(), model_client=model_client, model_info=model_info
        )
    else:
        data, markdown = project.report()
    _emit(ctx, data, markdown.read_text(encoding="utf-8"))


def main() -> None:
    """Console-script entry point; maps Saddlefit errors onto exit codes 1, 2 and 3."""
    try:
        app()
    except SaddlefitError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise SystemExit(exc.exit_code) from exc
