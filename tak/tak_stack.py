#!/usr/bin/env python3
"""The laptop stack with the TAK phones in it: code/tools/local_stack.py (fake pod -> tracker, geofence,
locate -> dashboard, no broker), plus tak_link.py both ways, the TV with the field teams, and a start at
time zero. local_stack.py and the dashboard are unchanged: this file builds on them. See tak/README.md.

    python tak/tak_stack.py --config code/park.toml                   # TV: http://localhost:8080
    python tak/tak_stack.py --config code/park.toml --port 8090 --start-paused
    python tak/tak_stack.py --config code/park.toml --no-tak          # the TV with the TAK panels, no phones

TAK: UDP multicast and plain TCP on [atak] tcp_port (8087, ATAK), and TLS on --tls-port (8089, iTAK)
once tak/tak_certs.py has made the certificates in --tls-dir (default tak/runs). Phones on the same
network (an iPhone hotspot works) add this laptop as a server; tak/README.md has the steps.

--start-paused: the drone waits on the pad at time zero until the ranger presses Start on the TV, and
again after Reset, so neither the TV nor the phones show a search that began before anyone looked.
"""
import argparse
import logging
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

TAK = Path(__file__).resolve().parent
import sys  # noqa: E402

for p in (TAK, TAK.parent / "code", TAK.parent / "code" / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import local_stack  # noqa: E402  (code/tools/local_stack.py, unchanged)
from afterglow import config as cfgmod  # noqa: E402
from afterglow.case import CaseStore  # noqa: E402
from afterglow.contracts import MODE, POSE, TARGET  # noqa: E402
from afterglow.dashboard import make_meta  # noqa: E402
from afterglow.parkmap import ParkMap  # noqa: E402
from tak_contracts import RESCUE  # noqa: E402
from tak_dashboard import TakCommands, TakState, make_tak_handler  # noqa: E402

log = logging.getLogger("tak_stack")


class HoldClock(local_stack.SimClock):
    """local_stack's SimClock, able to wait at time zero (hold) and to tell TAK when Start / Pause is pressed."""

    def __init__(self, speed=1.0, hold=False):
        super().__init__(speed)
        self.hold = hold
        self.paused = hold
        self.started = not hold  # has it run since time zero?
        self.on_run = None  # fn(paused, started_before): Start / Pause pressed

    def restart(self):
        super().restart()
        with self.lock:
            self.paused = self.hold
            self.started = not self.hold

    def view(self):
        return {**super().view(), "started": self.started}

    def set(self, body):
        if body.get("reset") is True:
            return super().set(body)
        if "flight" in body:  # the TV's "Send out" / "Return to base": the phones hear the launch too
            problems = super().set(body)
            if not problems and body["flight"] == "out" and self.on_run:
                self.on_run(False, False)
            return problems
        before, started = self.paused, self.started
        if body.get("paused", self.paused) is False:
            self.started = True  # before super().set(), whose listeners send view() to the TVs
        problems = super().set(body)
        if problems:
            self.started = started
            return problems
        if self.paused != before and self.on_run:
            self.on_run(self.paused, started)
        return problems


def start_tak(cfg, holder, chat, stop, tls_dir=None, tls_port=8089):
    """tak_link.py in this process, both ways: pings, the drone, every step's message and the rescue route
    go to the phones; their positions, chat and commands come back on the bus. Returns attach(bus), to
    call again after the sim's Reset (which makes a new bus), and the CotOut."""
    from tak_link import CotOut, TcpServer, UdpSender, tls_context
    acfg = cfg["atak"]
    senders = []
    co = CotOut(cfg, lambda data: [s(data) for s in senders], chat=chat,
                publish=lambda topic, msg: holder["bus"].publish(topic, msg))

    def hello(send_one):  # a phone joined (its own thread): take the bus lock, as every handler has it
        with holder["bus"].lock:
            co.hello(send_one)

    def phone(data):
        with holder["bus"].lock:
            co.from_phone(data)
    if acfg["send_to"]:
        senders.append(UdpSender(acfg["send_to"], acfg["iface"]))
    if acfg["tcp_port"]:
        senders.append(TcpServer(acfg["tcp_port"], on_connect=hello, on_event=phone))
    tls = bool(tls_dir) and (Path(tls_dir) / "server.pem").exists()
    if tls:
        senders.append(TcpServer(tls_port, ssl_ctx=tls_context(tls_dir), on_connect=hello, on_event=phone))

    def attach(bus):
        for topic in (TARGET, POSE, MODE, RESCUE):
            bus.subscribe(topic, co.feed)
    attach(holder["bus"])

    def ticker():  # repeat live pins every repeat_s, as lora_out does over LoRa
        while not stop.wait(1.0):
            with holder["bus"].lock:
                co.tick()
    threading.Thread(target=ticker, daemon=True, name="tak").start()
    log.info("TAK: %s%s%s, messages %s", ", ".join(acfg["send_to"]) or "no UDP",
             f" + TAK server on TCP {acfg['tcp_port']}" if acfg["tcp_port"] else "",
             f" + TLS on {tls_port} (iTAK)" if tls else " (no TLS: run tak/tak_certs.py for iTAK)",
             "on" if chat else "off")
    return attach, co


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="profile: afterglow.toml (default) or park.toml")
    ap.add_argument("--speed", type=float, default=1.0, help="fake pod time multiplier: 1, 2, 4 or 10 (the TV can change it)")
    ap.add_argument("--seed", type=int, default=2, help="fake pod random seed (as local_stack.py)")
    ap.add_argument("--replay", metavar="LOG", help="replay a mosquitto_sub -v log instead of the fake pod")
    ap.add_argument("--loop", action="store_true", help="with --replay: start again at the end")
    ap.add_argument("--host", default="127.0.0.1", help="address the TV is served on (0.0.0.0 = every interface)")
    ap.add_argument("--port", type=int, help="HTTP port (default: [http] port)")
    ap.add_argument("--start-paused", action="store_true",
                    help="fake pod: wait at time zero, drone on the pad, until the TV's Start (and again after Reset)")
    ap.add_argument("--feeds", action="store_true", help="also write fake camera and thermal pictures to live/ (the TV's panels)")
    ap.add_argument("--no-tak", action="store_true", help="no TAK servers (the TV still has the TAK panels)")
    ap.add_argument("--no-chat", action="store_true", help="TAK markers only, no chat messages")
    ap.add_argument("--tls-dir", default=str(TAK / "runs"), help="tak/tak_certs.py's folder (default tak/runs)")
    ap.add_argument("--tls-port", type=int, default=8089)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    cfg = cfgmod.load(a.config)
    holder = {"bus": local_stack.LocalBus()}  # replaced on a reset
    pm = ParkMap.load(cfg)
    meta = make_meta(cfg, pm)
    state = TakState(meta["trail_step_m"])
    local_stack.wire(cfg, holder["bus"], state, pm)
    cmds = TakCommands(lambda: holder["bus"], cfg["waypoints"])
    dirs = {k: cfgmod.resolve(cfg, cfg["http"][f"{k}_dir"]) for k in ("live", "snaps")}
    cases = CaseStore(cfg, pm)
    to_browsers = lambda view: state.broadcast("case", view)  # noqa: E731
    cases.listeners.append(to_browsers)
    stop = threading.Event()
    clock = None
    run = {}
    tak = None if a.no_tak else start_tak(cfg, holder, not a.no_chat, stop, a.tls_dir, a.tls_port)
    if a.replay:
        threading.Thread(target=lambda: local_stack.run_replay(a.replay, holder["bus"], a.speed, a.loop, stop),
                         daemon=True, name="source").start()
    else:
        clock = HoldClock(a.speed if a.speed in local_stack.SimClock.SPEEDS else 1, hold=a.start_paused)
        clock.listeners.append(lambda view: state.broadcast("sim", view))
        bx, by = cfg["sim"]["base_xy"]  # where the drone waits for "Send out", as local_stack.py
        clock.base = {"name": cfg["sim"]["base_name"], "x": bx, "y": by}
        if tak:
            def on_run(paused, started):
                with holder["bus"].lock:
                    tak[1].sim_state(paused, started)
            clock.on_run = on_run

        def start_pod():
            run["stop"] = threading.Event()
            pod = local_stack.make_pod(cfg, pm, holder["bus"], cases, a.seed)
            clock.pod = pod  # the clock drives "Send out" / "Return to base" on it
            run["thread"] = threading.Thread(target=local_stack.run_fake, args=(pod, holder["bus"], clock, run["stop"]),
                                             daemon=True, name="source")
            run["thread"].start()

        def reset():
            """Back to time zero, as local_stack.py's reset, and the phones lose this run's pins and routes."""
            run["stop"].set()
            run["thread"].join(timeout=3)
            holder["bus"] = local_stack.LocalBus()
            with state.lock:
                state.clear()
            local_stack.wire(cfg, holder["bus"], state, pm)
            if tak:
                attach, co = tak
                with holder["bus"].lock:
                    co.reset()
                attach(holder["bus"])
            cases.listeners[:] = [to_browsers]
            cases.close()
            clock.restart()
            start_pod()
            log.info("reset: back to time zero")
            state.broadcast("reset", {})
        clock.on_reset = reset
        start_pod()
    if a.feeds:  # as local_stack.py --feeds: fake camera and heat pictures that follow the sim
        dirs["live"].mkdir(parents=True, exist_ok=True)
        idle = {"hot": False, "person": False}
        local_stack.run_feeds(cfg, dirs["live"], stop,
                              view=(lambda: clock.pod.view if clock.pod else idle) if clock else None)

    server = ThreadingHTTPServer((a.host, a.port or cfg["http"]["port"]),
                                 make_tak_handler(state, cmds, meta, dirs, cases, clock))
    server.daemon_threads = True
    port = server.server_address[1]
    log.info("%s: %s -> tracker, geofence, locate -> dashboard + TAK, no broker", meta["name"],
             f"replay {a.replay}" if a.replay else "fake pod" + (", waiting for Start" if a.start_paused else ""))
    log.info("dashboard: open http://localhost:%d", port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if run.get("stop"):
            run["stop"].set()
        server.server_close()


if __name__ == "__main__":
    main()
