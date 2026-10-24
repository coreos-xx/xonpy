#!/usr/bin/env python3
"""Record raw data from the X.on headset over BLE (bleak / BlueZ).

  python tools/xon_record.py --info                     # dump GATT + descriptors + values, no streaming
  python tools/xon_record.py                            # stream EEG until Ctrl-C
  python tools/xon_record.py --protocol wet -l wet1     # guided session: touch test + on-head tasks
  python tools/xon_record.py --protocol head --gain 1   # on-head tasks only, at gain 1
  python tools/xon_record.py --mode impedance -d 30     # impedance mode for 30 s
  python tools/xon_record.py --skip-config -d 20        # test defaults (skip rate/gain/highpass writes)

While recording, type text + Enter to add a note to the log.
Close the X.on phone app first, the headset accepts only one central.

Writes recordings/<time>_<label>.csv (t,kind,data) and a .json with device info and settings.
Decode afterwards with: python -m xonpy summary|decode|impedance FILE.csv
"""
import argparse, asyncio, csv, json, os, signal, struct, sys, time

from xonpy import protocol as xp
from xonpy.client import RATES, XonClient

# GATT Characteristic Presentation Format (0x2904) format codes
FORMATS = {0x01: "bool", 0x04: "uint8", 0x06: "uint16", 0x08: "uint32", 0x0C: "sint8",
           0x0E: "sint16", 0x10: "sint32", 0x14: "float32", 0x15: "float64",
           0x19: "utf8", 0x1B: "struct"}
UNITS = {0x2700: "unitless", 0x2703: "s", 0x2713: "m/s2", 0x2722: "Hz", 0x2728: "V",
         0x272A: "ohm", 0x27AD: "%"}


# Electrode positions as seen by the wearer (10-20 system): odd = left, even = right, z = midline.
POSITIONS = {"F3": "front-left", "F4": "front-right", "C3": "top-left", "Cz": "top-centre",
             "C4": "top-right", "P3": "back-left", "P4": "back-right"}


def touch_protocol():
    """Steps (label, seconds or None = wait for Enter, prompt)."""
    steps = [("prep_touch", None, "Headset OFF your head, saline-wet. Hold the ear clip in one hand. "
                                  "Left/right = as if you were wearing it. Press ENTER to start")]
    names = xp.CHANNEL_NAMES[:7]
    steps.append(("rest", 5, "Rest, touch nothing"))
    for i, e in enumerate(names):
        nxt = f"{names[i + 1]} ({POSITIONS[names[i + 1]]})" if i + 1 < len(names) else "nothing"
        steps.append((f"touch_{e}", 3, f"TOUCH {e} ({POSITIONS[e]}) with a finger"))
        steps.append(("rest", 3, f"Release. Next: {nxt}"))
    return steps


def head_protocol():
    steps = [("prep_head", None, "Put the headset ON, clip on the ear. Press ENTER when settled")]
    steps.append(("baseline", 10, "Sit still, eyes open, relax"))
    steps.append(("eyes_closed", 15, "CLOSE your eyes and keep them closed, relax (beep at the end)"))
    steps.append(("eyes_open", 5, "Open your eyes"))
    for _ in range(5):
        steps.append(("blink", 1, "BLINK hard"))
        steps.append(("rest", 2, "Rest"))
    for _ in range(3):
        steps.append(("look_left", 2, "LOOK LEFT (eyes only)"))
        steps.append(("look_center", 2, "Look straight"))
        steps.append(("look_right", 2, "LOOK RIGHT (eyes only)"))
        steps.append(("look_center", 2, "Look straight"))
    steps.append(("end", 3, "Done, sit still"))
    return steps


PROTOCOLS = {"touch": touch_protocol, "head": head_protocol,
             "wet": lambda: touch_protocol() + head_protocol()}


def describe_2904(v):
    if len(v) < 7:
        return v.hex()
    fmt, exp, unit, ns, desc = struct.unpack("<BbHBH", v[:7])
    return (f"format={FORMATS.get(fmt, hex(fmt))} exp={exp} "
            f"unit={UNITS.get(unit, hex(unit))} ns={ns} desc=0x{desc:04x}")


def printable(v):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in v)


async def read_info(client):
    """Read every readable characteristic and descriptor. Returns a JSON-able dict."""
    info = []
    for svc in client.services:
        print(f"SERVICE {svc.uuid}")
        s = {"uuid": svc.uuid, "chars": []}
        for ch in svc.characteristics:
            c = {"uuid": ch.uuid, "handle": ch.handle, "props": ch.properties, "descriptors": []}
            line = f"  {ch.uuid[:8]} h=0x{ch.handle:04x} {','.join(ch.properties)}"
            if "read" in ch.properties:
                try:
                    v = bytes(await client.read_gatt_char(ch))
                    c["value"] = v.hex()
                    line += f"  = {v.hex()} |{printable(v)}|"
                except Exception as e:
                    c["error"] = str(e)
                    line += f"  read failed: {e}"
            print(line)
            for d in ch.descriptors:
                e = {"uuid": d.uuid, "handle": d.handle}
                try:
                    v = bytes(await client.read_gatt_descriptor(d.handle))
                    e["value"] = v.hex()
                    if d.uuid.startswith("00002901"):
                        e["text"] = v.decode("utf-8", "replace")
                        shown = repr(e["text"])
                    elif d.uuid.startswith("00002904"):
                        e["text"] = shown = describe_2904(v)
                    else:
                        shown = v.hex()
                except Exception as ex:
                    e["error"] = shown = f"read failed: {ex}"
                print(f"      {d.uuid[4:8]} {shown}")
                c["descriptors"].append(e)
            s["chars"].append(c)
        info.append(s)
    return info


class Stats:
    """Per-second live view: packet rate, counter gaps, saturation, signal spread."""

    def __init__(self):
        self.seq = xp.SeqTracker()
        self.total_pkts = self.total_samples = self.bad = self.impedance = 0
        self.lens = {}
        self._reset()

    def _reset(self):
        self.pkts = 0
        self.vals = [[] for _ in range(8)]
        self.sat = [0] * 8
        self.n = 0
        self.accel = None
        self.status = None
        self.trailer = None

    def feed(self, data):
        """Parse one notification and update the counters. Returns the Packet or None."""
        self.pkts += 1
        self.total_pkts += 1
        self.lens[len(data)] = self.lens.get(len(data), 0) + 1
        try:
            pkt = xp.parse(data)
        except ValueError:
            self.bad += 1
            return None
        self.impedance += pkt.impedance is not None
        self.trailer = pkt.trailer or self.trailer
        for s in pkt.samples:
            self.seq.feed(s.seq)
            self.n += 1
            self.total_samples += 1
            self.accel, self.status = s.accel, s.status
            for i, v in enumerate(s.ch):
                if v in xp.RAIL:
                    self.sat[i] += 1
                else:
                    self.vals[i].append(v)
        return pkt

    def line(self, dt):
        chans = []
        for name, sat, vals in zip(xp.CHANNEL_NAMES, self.sat, self.vals):
            if self.n and sat > 0.9 * self.n:
                chans.append(f"{name}:SAT")
            elif vals:
                chans.append(f"{name}:{max(vals) - min(vals)}")
            else:
                chans.append(f"{name}:-")
        s = (f"{self.pkts / dt:4.1f} pkt/s {self.n / dt:5.1f} smp/s gaps {self.seq.gaps} "
             f"st={self.status} acc={self.accel} | " + " ".join(chans))
        if self.trailer:
            s += f" | {self.trailer.battery_mv} mV {self.trailer.rssi} dBm"
        if self.bad or self.impedance:
            s += f" | bad {self.bad} imp {self.impedance}"
        self._reset()
        return s


class ConsoleUI:
    """Terminal front end: prints prompts and status, stdin for notes and Enter, Ctrl-C stops."""

    def started(self, rec):
        loop = asyncio.get_running_loop()
        loop.add_reader(sys.stdin.fileno(), self._stdin, rec)
        loop.add_signal_handler(signal.SIGINT, rec.stop)
        print("(Ctrl-C to stop, type text + Enter for a note)\n")

    def stopped(self, rec):
        loop = asyncio.get_running_loop()
        loop.remove_reader(sys.stdin.fileno())
        loop.remove_signal_handler(signal.SIGINT)

    def _stdin(self, rec):
        line = sys.stdin.readline()
        if not line:
            asyncio.get_running_loop().remove_reader(sys.stdin.fileno())
            return
        rec.proceed(line.strip())

    def step(self, i, steps):
        if i is None:   # protocol finished
            return
        label, secs, prompt = steps[i]
        print(f"\n>>> {prompt}" + (f"  ({secs} s)" if secs else "") + "\a")

    def status(self, line):
        print("   " + line)

    def packet(self, pkt):
        pass

    def message(self, text):
        print(text)


class Recorder:
    """Connects, configures, streams and logs. Front ends call proceed/note/stop/start_protocol."""

    def __init__(self, args, ui):
        self.args, self.ui = args, ui
        self.stats = Stats()
        self.rate = args.rate
        self.done = None
        self.waiter = None
        self.writer = None
        self.protocol_task = None
        self.stop_after_protocol = True

    def log(self, kind, data):
        if self.writer:
            self.writer.writerow([f"{time.time():.6f}", kind, data])

    def note(self, text):
        self.log("note", text)
        self.ui.message(f"note: {text}")

    def proceed(self, text=""):
        """Continue a protocol step that waits for Enter, otherwise treat text as a note."""
        if self.waiter and not self.waiter.done():
            self.waiter.set_result(text)
        elif text:
            self.note(text)

    def stop(self):
        if self.done:
            self.done.set()

    def start_protocol(self, name):
        if self.protocol_task and not self.protocol_task.done():
            self.ui.message("a protocol is already running")
            return
        self.protocol_task = asyncio.create_task(self._run_protocol(PROTOCOLS[name]()))

    async def _run_protocol(self, steps):
        loop = asyncio.get_running_loop()
        for i, (label, secs, prompt) in enumerate(steps):
            self.log("marker", label)
            self.ui.step(i, steps)
            if secs is None:
                self.waiter = loop.create_future()
                await self.waiter
            else:
                await asyncio.sleep(secs)
        self.log("marker", "protocol_end")
        self.ui.step(None, steps)
        self.ui.message("protocol finished")
        if self.stop_after_protocol:
            self.stop()

    async def _ticker(self, f):
        last = time.monotonic()
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            self.ui.status(self.stats.line(now - last))
            last = now
            f.flush()

    async def run(self):
        args = self.args
        os.makedirs(args.out, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.join(args.out, stamp + (f"_{args.label}" if args.label else ""))
        loop = asyncio.get_running_loop()
        self.done = asyncio.Event()

        xon = XonClient(args.address, rate=args.rate, gain=args.gain, highpass=args.highpass,
                        mode=args.mode, reconnect=False, scan_timeout=args.scan_timeout,
                        connect_timeout=args.connect_timeout)

        def on_event(text):
            self.log("event", text)
            if not text.startswith(("write ", "read ")):
                self.ui.message(text)

        def on_packet(raw, _):
            self.log("notify", raw.hex())
            pkt = self.stats.feed(raw)
            if pkt:
                self.ui.packet(pkt)

        xon.on_event, xon.on_packet, xon.on_disconnect = on_event, on_packet, self.done.set
        self.ui.message("scanning ...")
        await xon.connect()
        f = None
        try:
            self.ui.message("reading device info")
            meta = {"device": {"name": xon.device.name, "address": xon.device.address}, "started": stamp,
                    "args": vars(args), "gatt": await read_info(xon.ble)}
            if args.info:
                with open(base + "_info.json", "w") as jf:
                    json.dump(meta, jf, indent=1)
                self.ui.message(f"wrote {base}_info.json")
                return

            f = open(base + ".csv", "w", newline="")
            self.writer = csv.writer(f)
            self.writer.writerow(["t", "kind", "data"])
            await xon.start(configure=not args.skip_config)
            info = xon.info
            self.rate = info.rate
            meta["config"] = {"rate": info.rate, "gain": info.gain, "highpass": info.highpass}
            meta["scales"] = [info.resolution_uv, info.accel_mg]
            self.ui.message(f"config: rate {info.rate}, gain {info.gain}, highpass {info.highpass:g}, "
                            f"battery {info.battery_mv} mV")
            self.ui.message(f"streaming {args.mode} -> {base}.csv")

            self.ui.started(self)
            tasks = [asyncio.create_task(self._ticker(f))]
            if args.protocol:
                self.start_protocol(args.protocol)
            if args.duration:
                loop.call_later(args.duration, self.done.set)
            try:
                await self.done.wait()
            finally:
                for t in tasks + [self.protocol_task]:
                    if t:
                        t.cancel()
                self.ui.stopped(self)
                if xon.is_connected:
                    self.ui.message("stopping ...")
                await xon.close()
                st = self.stats
                meta["totals"] = {"packets": st.total_pkts, "samples": st.total_samples,
                                  "lengths": st.lens, "unparseable": st.bad,
                                  "seq_gaps": st.seq.gaps, "seq_lost": st.seq.lost,
                                  "impedance_records": st.impedance}
                with open(base + ".json", "w") as jf:
                    json.dump(meta, jf, indent=1)
                self.ui.message(f"{st.total_pkts} packets, {st.total_samples} samples, "
                                f"{st.seq.gaps} gaps ({st.seq.lost} lost). Wrote {base}.csv and .json")
        finally:
            await xon.close()
            if f:
                f.close()
            self.writer = None


def make_parser(description=__doc__):
    ap = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--info", action="store_true", help="only dump GATT info, don't stream")
    ap.add_argument("--address", help="connect to this address instead of scanning by name")
    ap.add_argument("--mode", choices=["eeg", "impedance"], default="eeg")
    ap.add_argument("--rate", type=int, choices=RATES, default=250, help="sample rate in Hz")
    ap.add_argument("--gain", type=int, default=8, help="amplifier gain (power-on 1, app uses 8)")
    ap.add_argument("--highpass", type=float, default=0.5, help="highpass in Hz (power-on 0.1, app uses 0.5)")
    ap.add_argument("--skip-config", action="store_true", help="skip the rate/gain/highpass writes to test defaults")
    ap.add_argument("--protocol", choices=sorted(PROTOCOLS), help="run a guided session with markers")
    ap.add_argument("-d", "--duration", type=float, help="stop after this many seconds")
    ap.add_argument("-l", "--label", default="", help="added to the output file name")
    ap.add_argument("--out", default="recordings", help="output directory")
    ap.add_argument("--scan-timeout", type=float, default=15)
    ap.add_argument("--connect-timeout", type=float, default=20)
    return ap


def main():
    args = make_parser().parse_args()
    try:
        asyncio.run(Recorder(args, ConsoleUI()).run())
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
