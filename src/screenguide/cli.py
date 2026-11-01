"""The `screenguide` command."""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from screenguide.capture import CaptureError, FileCapturer, PortalCapturer, ScreenCapturer
from screenguide.config import ConfigError, load_config
from screenguide.contract import InvalidModelResponse, ModelRefusal
from screenguide.detection import DetectionError, RapidOcrDetector
from screenguide.grounding import SetOfMarkGrounding
from screenguide.model import AnthropicProvider, ModelProviderError
from screenguide.monitors import MonitorError, format_monitors, read_monitors
from screenguide.pricing import load_pricing
from screenguide.session import GuideSession, Step
from screenguide.usage import JsonlUsageTracker, read_usage_log, summarize

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
        config = load_config()
        capturer: ScreenCapturer = (
            FileCapturer(image)
            if image
            else PortalCapturer(save_dir=config.screenshot_dir, monitor=monitor or config.monitor)
        )
        session = GuideSession(
            goal=goal,
            capturer=capturer,
            detector=RapidOcrDetector(),
            grounding=SetOfMarkGrounding(config.image_long_edge),
            provider=AnthropicProvider(config.model, config.effort),
            tracker=JsonlUsageTracker(config.usage_log, load_pricing()),
            notify=lambda message: print(message, file=sys.stderr),
        )
        return action(session)
    except (
        OSError,
        ConfigError,
        CaptureError,
        MonitorError,
        DetectionError,
        ModelProviderError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except InvalidModelResponse as exc:
        print(f"error: invalid model response after one retry: {exc}", file=sys.stderr)
        return 1
    except ModelRefusal as exc:
        print(
            f"The model declined this request: {exc}\n"
            "Try another model: set `model` in ~/.config/screenguide/config.toml "
            "or SCREENGUIDE_MODEL.",
            file=sys.stderr,
        )
        return 1


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
    commands.add_parser("usage", help="summarise usage by day, session, provider and model")
    commands.add_parser("monitors", help="list the monitors by connector name")

    args = parser.parse_args(argv)
    if args.command == "guide":
        return guide(args.goal, args.monitor)
    if args.command == "detect":
        return detect(args.image, args.output)
    if args.command == "usage":
        return usage()
    if args.command == "monitors":
        return monitors()
    if args.image and args.monitor:
        run_parser.error("--monitor applies only to --capture")
    return run(args.image, args.goal, args.monitor)
