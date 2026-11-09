"""tak_stack.py's clock: with --start-paused the drone waits at time zero for the TV's Start, and again after Reset."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tak"))  # the TAK add-on lives in tak/, beside code/
from tak_stack import HoldClock  # noqa: E402


def test_start_paused_waits_at_time_zero_and_reset_waits_again():
    clock = HoldClock(1, hold=True)
    runs, seen = [], []
    clock.on_run = lambda paused, started: runs.append((paused, started))
    clock.listeners.append(seen.append)
    view = clock.view()  # SimClock's view also carries flight, base and plan (Send out / Return to base)
    assert {k: view[k] for k in ("paused", "speed", "started")} == {"paused": True, "speed": 1.0, "started": False}
    assert clock.elapsed_ms() == 0
    assert clock.set({"paused": False}) == []
    assert seen[-1]["started"]  # the TVs hear "started" with the Start itself, so the Ready banner goes
    clock.set({"paused": True})
    assert runs == [(False, False), (True, True)]
    assert clock.set({"paused": "no"}) and clock.paused  # a bad request changes nothing
    clock.restart()
    assert clock.paused and not clock.started


def test_without_hold_it_runs_as_local_stack_does():
    clock = HoldClock(2)
    assert not clock.paused and clock.view()["started"]


class StubPod:
    flight = "docked"

    def launch(self):
        self.flight = "out"

    def go_home(self):
        self.flight = "returning"

    def plan(self):
        return []


def test_send_out_launches_the_pod_and_tells_the_phones():
    clock = HoldClock(1)
    runs = []
    clock.on_run = lambda paused, started: runs.append((paused, started))
    assert clock.set({"flight": "out"})  # no pod yet: refused, as local_stack's SimClock does
    clock.pod = StubPod()  # tak_stack.main() sets it, or the TV's Send out is refused and the drone never leaves
    assert clock.set({"flight": "out"}) == [] and clock.view()["flight"] == "out"
    assert runs == [(False, False)]  # the phones get "AFTERGLOW launched"
    assert clock.set({"flight": "home"}) == [] and clock.view()["flight"] == "returning" and runs == [(False, False)]
