"""The `screenguide` command."""

import argparse
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from screenguide.capture import FileCapturer, PortalCapturer, ScreenCapturer, pinned
from screenguide.config import Config, ConfigError, load_config
from screenguide.contract import ModelRefusal
from screenguide.detection import DetectionError, ElementDetector, RapidOcrDetector
from screenguide.errors import GUIDE_ERRORS, describe_guide_error
from screenguide.evaluation import Case, CaseResult, git_commit, load_cases, open_run_file, run_eval
from screenguide.grounding import GroundingStrategy, SetOfMarkGrounding
from screenguide.instance import InstanceError, claim_or_send
from screenguide.model import AnthropicProvider, ModelProvider, ModelProviderError
from screenguide.monitors import MonitorChoice, MonitorError, format_monitors, read_monitors
from screenguide.pricing import PricingTable, load_pricing
from screenguide.session import GuideSession, Step
from screenguide.usage import JsonlUsageTracker, UsageTracker, read_usage_log, summarize

NEXT_PROMPT = "\nDo this step, then press Enter for the next one (q to quit): "


def format_step(step: Step) -> str:
    bbox = "null"
    if step.bbox:
        box = step.bbox
        bbox = f"x={box.x} y={box.y} width={box.width} height={box.height}"
    return "\n".join(
        [
            f"target_label: {step.target_label if step.target_label is not None else 'null'}",
            f"instruction: {step.instruction}",
            f"bbox: {bbox}",
            f"done: {str(step.done).lower()}",
            f"confidence: {step.confidence}",
        ]
    )


def run(image: Path | None, goal: str, monitor: str | None = None) -> int:
    def one_step(session: GuideSession) -> int:
        print(format_step(session.next_step()))
        return 0

    return _with_session(goal, image, monitor, one_step)


def guide(goal: str, monitor: str | None = None) -> int:
    def loop(session: GuideSession) -> int:
        print(f"session: {session.session_id[:8]}")
        for number, step in enumerate(session.steps(), 1):
            print(f"\nStep {number}\n{format_step(step)}")
            if not step.done and not _wait_for_next():
                break
        return 0

    try:
        return _with_session(goal, None, monitor, loop)
    except KeyboardInterrupt:
        print()
        return 130


def _wait_for_next() -> bool:
    """Whether the user asked for another step. Closed input counts as quitting."""
    try:
        return input(NEXT_PROMPT).strip().lower() != "q"
    except EOFError:
        return False


def _with_session(
    goal: str,
    image: Path | None,
    monitor: str | None,
    action: Callable[[GuideSession], int],
) -> int:
    """Build a session for the real screen, or for `image` if given, and report what fails."""
    try:
        session = _build_session(
            goal, image, monitor, notify=lambda message: print(message, file=sys.stderr)
        )
        return action(session)
    except GUIDE_ERRORS as exc:
        prefix = "" if isinstance(exc, ModelRefusal) else "error: "
        print(f"{prefix}{describe_guide_error(exc)}", file=sys.stderr)
        return 1


def _build_session(
    goal: str,
    image: Path | None,
    monitor: str | None,
    notify: Callable[[str], None],
    wrap_capturer: Callable[[ScreenCapturer], ScreenCapturer] = lambda capturer: capturer,
    choose_monitor: Callable[[], MonitorChoice] | None = None,
) -> GuideSession:
    """`choose_monitor` decides each capture's monitor; without it, `monitor` or the config does."""
    config = load_config()
    capturer: ScreenCapturer = (
        FileCapturer(image)
        if image
        else PortalCapturer(
            save_dir=config.screenshot_dir,
            choose_monitor=choose_monitor or pinned(monitor or config.monitor),
        )
    )
    layers = _layers(config)
    return GuideSession(
        goal=goal,
        capturer=wrap_capturer(capturer),
        detector=layers.detector,
        grounding=layers.grounding,
        provider=layers.provider,
        tracker=layers.tracker,
        notify=notify,
    )


@dataclass(frozen=True)
class _Layers:
    """Every layer of a session but the capturer, the same for the app and the evaluation."""

    detector: ElementDetector
    grounding: GroundingStrategy
    provider: ModelProvider
    tracker: JsonlUsageTracker
    pricing: PricingTable


def _layers(config: Config) -> _Layers:
    pricing = load_pricing()
    return _Layers(
        detector=RapidOcrDetector(),
        grounding=SetOfMarkGrounding(config.image_long_edge),
        provider=AnthropicProvider(config.model, config.effort),
        tracker=JsonlUsageTracker(config.usage_log, pricing),
        pricing=pricing,
    )


def evaluate(
    cases_file: Path,
    model: str | None = None,
    effort: str | None = None,
    image_long_edge: int | None = None,
    limit: int | None = None,
) -> int:
    """Run each case through the app's pipeline for one step and write a run file."""
    try:
        config = load_config()
        overrides = {"model": model, "effort": effort, "image_long_edge": image_long_edge}
        config = replace(config, **{k: v for k, v in overrides.items() if v is not None})
        cases, skipped = load_cases(cases_file)
        layers = _layers(config)
    except (OSError, UnicodeDecodeError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for line in skipped:
        print(f"{cases_file}:{line.line}: skipped: {line.reason}", file=sys.stderr)
    cases = cases[:limit] if limit is not None else cases
    if not cases:
        print("error: no cases to run", file=sys.stderr)
        return 1

    provider = layers.provider
    try:
        provider.check_credentials()
    except ModelProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if provider.external:
        print(
            f"Sending {len(cases)} screenshots, one per case plus any retry, to external "
            f"provider {provider.name} ({provider.model}).",
            file=sys.stderr,
        )

    session_id = uuid.uuid4().hex
    settings = {
        "session_id": session_id,
        "started_at": datetime.now(UTC).isoformat(),
        "provider": provider.name,
        "model": provider.model,
        "effort": config.effort,
        "grounding": layers.grounding.name,
        "image_long_edge": config.image_long_edge,
        "cases_file": str(cases_file.resolve()),
        "git_commit": git_commit(),
    }

    def new_session(case: Case, tracker: UsageTracker) -> GuideSession:
        return GuideSession(
            goal=case.goal,
            capturer=FileCapturer(case.image),
            detector=layers.detector,
            grounding=layers.grounding,
            provider=provider,
            tracker=tracker,
            history=case.history,
            session_id=session_id,
        )

    def progress(result: CaseResult) -> None:
        detail = f" ({result.error})" if result.error else ""
        print(f"line {result.line}: {result.outcome}{detail}", file=sys.stderr)

    try:
        path, run_file = open_run_file(cases_file.parent / "runs")
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with run_file:
        summary = run_eval(
            cases, new_session, layers.tracker, layers.pricing, settings, run_file, progress
        )
    print(f"{summary.format()}\nrun file: {path}")
    return 0


def detect(image: Path, output: Path) -> int:
    try:
        config = load_config()
        screenshot = FileCapturer(image).capture()
        elements = RapidOcrDetector().detect(screenshot)
        marked = SetOfMarkGrounding(config.image_long_edge).mark(screenshot, elements)
        output.write_bytes(marked.image.data)
    except (OSError, ConfigError, DetectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for element in elements:
        box = element.bbox
        print(
            f"{element.id}: {element.text!r} x={box.x} y={box.y} "
            f"width={box.width} height={box.height} confidence={element.confidence:.2f}"
        )
    return 0


def panel(monitor: str | None = None, command: str = "show") -> int:
    """Run the panel, or hand `command` to the one already running."""
    try:
        listener = claim_or_send(command)
    except InstanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if listener is None:
        if monitor:
            print("screenguide is already running; --monitor applies to a new one", file=sys.stderr)
        return 0
    with listener:
        try:
            starting_monitor = monitor or load_config().monitor
            # Imported here so that the other commands, and a launch that only forwards, start
            # without loading Qt.
            from screenguide.panel_guide import run_panel

            return run_panel(
                lambda goal, wrap_capturer, notify, choose_monitor: _build_session(
                    goal, None, None, notify, wrap_capturer, choose_monitor
                ),
                listener,
                read_monitors,
                starting_monitor,
            )
        except (ImportError, OSError, ConfigError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1


def toggle() -> int:
    return panel(command="toggle")


def panel_demo(image: Path) -> int:
    try:
        from screenguide.panel import run_demo

        return run_demo(FileCapturer(image).capture())
    except (ImportError, OSError, DetectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def usage() -> int:
    try:
        records, malformed = read_usage_log(load_config().usage_log)
    except (OSError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(summarize(records, malformed))
    return 0


def monitors() -> int:
    try:
        print(format_monitors(read_monitors()))
    except MonitorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive integer")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screenguide")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="produce one step for a screen and a goal")
    source = run_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path, help="image file to read")
    source.add_argument("--capture", action="store_true", help="capture the real screen")
    run_parser.add_argument("--goal", required=True, help="what the user wants to do")
    run_parser.add_argument("--monitor", help="connector name of the monitor to capture")
    guide_parser = commands.add_parser("guide", help="guide a task step by step on the real screen")
    guide_parser.add_argument("--goal", required=True, help="what the user wants to do")
    guide_parser.add_argument("--monitor", help="connector name of the monitor to capture")
    detect_parser = commands.add_parser("detect", help="find the text elements in an image")
    detect_parser.add_argument("--image", type=Path, required=True, help="image file to read")
    detect_parser.add_argument(
        "--output", type=Path, required=True, help="where to write the marked PNG"
    )
    panel_parser = commands.add_parser("panel", help="guide a task from the panel")
    panel_parser.add_argument("--monitor", help="connector name of the monitor to capture")
    panel_parser.add_argument("--image", type=Path, help="image file to crop, with --demo-step")
    panel_parser.add_argument(
        "--demo-step", action="store_true", help="show made-up steps instead of guiding a task"
    )
    commands.add_parser("toggle", help="show or hide the running panel, starting one if none runs")
    commands.add_parser("usage", help="summarise usage by day, session, provider and model")
    commands.add_parser("monitors", help="list the monitors by connector name")
    eval_parser = commands.add_parser(
        "eval", help="run saved cases through the pipeline and score the chosen elements"
    )
    eval_parser.add_argument("cases", type=Path, help="cases file, one JSON case per line")
    eval_parser.add_argument("--model", help="model for this run, instead of the config's")
    eval_parser.add_argument("--effort", help="effort for this run, instead of the config's")
    eval_parser.add_argument(
        "--image-long-edge",
        type=_positive_int,
        help="long edge of the image sent to the model, in pixels, for this run",
    )
    eval_parser.add_argument("--limit", type=_positive_int, help="run only the first N cases")

    args = parser.parse_args(argv)
    if args.command == "guide":
        return guide(args.goal, args.monitor)
    if args.command == "detect":
        return detect(args.image, args.output)
    if args.command == "panel":
        if bool(args.image) != args.demo_step:
            panel_parser.error("--image and --demo-step go together")
        if args.demo_step and args.monitor:
            panel_parser.error("--monitor does not apply to --demo-step")
        return panel_demo(args.image) if args.demo_step else panel(args.monitor)
    if args.command == "toggle":
        return toggle()
    if args.command == "usage":
        return usage()
    if args.command == "monitors":
        return monitors()
    if args.command == "eval":
        return evaluate(args.cases, args.model, args.effort, args.image_long_edge, args.limit)
    if args.image and args.monitor:
        run_parser.error("--monitor applies only to --capture")
    return run(args.image, args.goal, args.monitor)
