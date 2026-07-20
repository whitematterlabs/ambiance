"""Wake-loop edges (Linux only — inotify/timerfd/epoll are the point)."""

import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="v4 agent runtime is Linux-native"
)


@pytest.fixture
def loop(tmp_path):
    from agent.loop import WakeLoop

    loop = WakeLoop(tmp_path)
    yield loop
    loop.close()


def test_file_delivery_wakes(loop, tmp_path):
    (tmp_path / "msg-1").write_text("hello")
    wake = loop.wait(timeout=2)
    assert wake is not None
    assert [p.name for p in wake.inbox] == ["msg-1"]
    assert not wake.timer


def test_atomic_rename_delivery_wakes(loop, tmp_path, tmp_path_factory):
    staged = tmp_path_factory.mktemp("staging") / "msg-2"
    staged.write_text("hello")
    staged.rename(tmp_path / "msg-2")
    wake = loop.wait(timeout=2)
    assert wake is not None
    assert [p.name for p in wake.inbox] == ["msg-2"]


def test_timer_expiry_wakes(loop):
    loop.arm(time.time() + 0.1)
    wake = loop.wait(timeout=2)
    assert wake is not None
    assert wake.timer
    assert wake.inbox == []


def test_disarmed_timer_stays_silent(loop):
    loop.arm(time.time() + 0.1)
    loop.disarm()
    assert loop.wait(timeout=0.3) is None


def test_events_queue_between_waits(loop, tmp_path):
    (tmp_path / "a").write_text("1")
    (tmp_path / "b").write_text("2")
    time.sleep(0.05)
    wake = loop.wait(timeout=2)
    assert wake is not None
    assert sorted(p.name for p in wake.inbox) == ["a", "b"]
