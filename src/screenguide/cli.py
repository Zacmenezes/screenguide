"""The `screenguide` command."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from screenguide.capture import CaptureError, FileCapturer, PortalCapturer, ScreenCapturer
from screenguide.config import ConfigError, load_config
from screenguide.contract import InvalidModelResponse
from screenguide.detection import DetectionError, RapidOcrDetector
from screenguide.grounding import SetOfMarkGrounding
from screenguide.model import StubProvider
from screenguide.monitors import MonitorError, format_monitors, read_monitors
from screenguide.pricing import load_pricing
from screenguide.session import GuideSession, Step
from screenguide.usage import JsonlUsageTracker, read_usage_log, summarize


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
            provider=StubProvider(),
            tracker=JsonlUsageTracker(config.usage_log, load_pricing()),
        )
        step = session.next_step()
    except (OSError, ConfigError, CaptureError, MonitorError, DetectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except InvalidModelResponse as exc:
        print(f"error: invalid model response: {exc}", file=sys.stderr)
        return 1
    print(format_step(step))
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
    detect_parser = commands.add_parser("detect", help="find the text elements in an image")
    detect_parser.add_argument("--image", type=Path, required=True, help="image file to read")
    detect_parser.add_argument(
        "--output", type=Path, required=True, help="where to write the marked PNG"
    )
    commands.add_parser("usage", help="summarise usage by day, session, provider and model")
    commands.add_parser("monitors", help="list the monitors by connector name")

    args = parser.parse_args(argv)
    if args.command == "detect":
        return detect(args.image, args.output)
    if args.command == "usage":
        return usage()
    if args.command == "monitors":
        return monitors()
    if args.image and args.monitor:
        run_parser.error("--monitor applies only to --capture")
    return run(args.image, args.goal, args.monitor)
