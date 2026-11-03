"""Single instance and its IPC, on real sockets, without Qt."""

import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from screenguide import instance
from screenguide.instance import InstanceError, claim_or_send, receive


@pytest.fixture
def running():
    """The instance a test runs against, released at the end like a process that exits."""
    listener = claim_or_send("show")
    assert listener is not None
    yield listener
    listener.close()


def receive_soon(listener, count=1, seconds=2.0):
    commands = []
    deadline = time.monotonic() + seconds
    while len(commands) < count:
        assert time.monotonic() < deadline, f"got only {commands}"
        commands += receive(listener)
        time.sleep(0.005)
    return commands


def raw_client(address, payload):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(address)
    if payload is not None:
        client.sendall(payload)
    return client


def test_first_launch_becomes_the_instance_and_hears_nothing_yet(running):
    assert running.getblocking() is False
    assert receive(running) == []


def test_second_launch_hands_its_command_to_the_first_and_does_not_claim(running):
    assert claim_or_send("toggle") is None
    assert claim_or_send("show") is None

    assert receive_soon(running, 2) == ["toggle", "show"]


def test_commands_sent_before_the_instance_reads_wait_for_it(running):
    """The kernel queues them while the instance is still loading Qt."""
    for _ in range(5):
        assert claim_or_send("toggle") is None

    assert receive_soon(running, 5) == ["toggle"] * 5


def test_instance_that_ended_leaves_nothing_behind(running):
    running.close()

    listener = claim_or_send("toggle")

    assert listener is not None
    listener.close()


def test_instance_that_was_killed_leaves_nothing_behind(instance_address):
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from screenguide import instance\n"
            f"instance.ADDRESS = {instance_address!r}\n"
            "listener = instance.claim_or_send('show')\n"
            "print(listener is not None, flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "True"
        assert instance._claim() is None
    finally:
        holder.kill()
        holder.wait()

    listener = claim_or_send("show")

    assert listener is not None
    listener.close()


def test_launches_at_the_same_time_make_exactly_one_instance():
    results = []
    start = threading.Barrier(8)

    def launch():
        start.wait()
        results.append(claim_or_send("toggle"))

    threads = [threading.Thread(target=launch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    listeners = [result for result in results if result is not None]
    assert len(listeners) == 1
    assert receive_soon(listeners[0], 7) == ["toggle"] * 7
    listeners[0].close()


def test_instance_ending_between_bind_and_connect_is_replaced(monkeypatch):
    claims = iter([None, "listener"])
    sends = []

    def refuse(command):
        sends.append(command)
        raise ConnectionRefusedError

    monkeypatch.setattr(instance, "_claim", lambda: next(claims))
    monkeypatch.setattr(instance, "_send", refuse)

    assert claim_or_send("toggle") == "listener"
    assert sends == ["toggle"]


def test_gives_up_with_a_clear_error_when_it_can_neither_claim_nor_reach(monkeypatch):
    monkeypatch.setattr(instance, "_claim", lambda: None)

    def refuse(command):
        raise ConnectionRefusedError

    monkeypatch.setattr(instance, "_send", refuse)

    with pytest.raises(InstanceError, match="try again"):
        claim_or_send("toggle")


def test_instance_that_does_not_answer_is_reported(monkeypatch):
    monkeypatch.setattr(instance, "_claim", lambda: None)

    def hang(command):
        raise TimeoutError("timed out")

    monkeypatch.setattr(instance, "_send", hang)

    with pytest.raises(InstanceError, match="cannot reach the running screenguide"):
        claim_or_send("show")


def test_unknown_command_is_not_sent():
    with pytest.raises(ValueError):
        claim_or_send("quit")


def receive_until(listener, last, seconds=2.0):
    """Every command up to and including `last`."""
    commands = []
    deadline = time.monotonic() + seconds
    while last not in commands:
        assert time.monotonic() < deadline, f"got only {commands}"
        commands += receive(listener)
        time.sleep(0.005)
    return commands


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"quit\n", []),
        (b"", []),
        (b"x" * 500, []),
        (b"\xff\xfe\n", []),
        # A command missing its newline still counts once the client hangs up.
        (b"toggle", ["toggle"]),
    ],
    ids=["unknown", "empty", "too-long", "not-utf8", "no-newline"],
)
def test_instance_ignores_what_is_not_a_command(running, instance_address, payload, expected):
    with raw_client(instance_address, payload):
        pass
    claim_or_send("show")

    assert receive_until(running, "show") == [*expected, "show"]


def test_client_that_sends_nothing_holds_the_instance_only_briefly(
    running, instance_address, monkeypatch
):
    monkeypatch.setattr(instance, "TIMEOUT_SECONDS", 0.1)
    with raw_client(instance_address, None):
        claim_or_send("toggle")
        started = time.monotonic()

        commands = receive_until(running, "toggle")

        assert time.monotonic() - started < 1.0
    assert commands == ["toggle"]


def test_commands_from_another_user_are_refused(running, monkeypatch):
    claim_or_send("toggle")
    real_uid = os.getuid()
    monkeypatch.setattr(instance.os, "getuid", lambda: real_uid + 1)

    time.sleep(0.05)
    assert receive(running) == []


def test_socket_of_another_user_gets_no_command(running, monkeypatch):
    real_uid = os.getuid()
    with monkeypatch.context() as patched:
        patched.setattr(instance.os, "getuid", lambda: real_uid + 1)
        with pytest.raises(InstanceError, match="another user"):
            claim_or_send("toggle")

    time.sleep(0.05)
    assert receive(running) == []


@pytest.mark.parametrize("command", ["toggle", "next"])
def test_shortcut_command_reaches_the_running_panel_without_loading_the_app(
    running, instance_address, command
):
    """The shortcut's launch skips the imports that take most of a second."""
    script = (
        "import sys\n"
        "from screenguide import instance\n"
        f"instance.ADDRESS = {instance_address!r}\n"
        "from screenguide.launcher import main\n"
        f"code = main([{command!r}])\n"
        "print(code, 'screenguide.cli' in sys.modules, 'anthropic' in sys.modules)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )

    assert done.stdout.split() == ["0", "False", "False"], done.stderr
    assert receive_soon(running) == [command]


def test_forward_reports_when_no_instance_answers():
    assert instance.forward("toggle") is False


@pytest.mark.parametrize(
    ("args", "with_instance"),
    [(["toggle"], False), (["next"], False), (["panel"], True), (["toggle", "--help"], True)],
    ids=["toggle-with-none-running", "next-with-none-running", "panel", "other-arguments"],
)
def test_launcher_hands_everything_else_to_the_cli(monkeypatch, args, with_instance):
    from screenguide import cli, launcher

    calls = []
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(argv) or 7)
    listener = claim_or_send("show") if with_instance else None
    try:
        assert launcher.main(args) == 7
        assert calls == [args]
        if listener is not None:
            assert receive(listener) == []
    finally:
        if listener is not None:
            listener.close()
