"""Command line for the xonpy library.

  python -m xonpy scan                               # list advertising headsets
  python -m xonpy info                               # connect and print device info
  python -m xonpy lsl [--aux] [--no-accel]           # stream EEG (uV) + accel (g) to LSL
  python -m xonpy lsl --replay recordings/X.csv      # same, from a recording (no headset needed)
  python -m xonpy summary|decode|impedance FILE.csv  # offline tools for raw recordings
"""
import argparse, asyncio, dataclasses, logging, sys, time

from . import protocol as xp
from .client import RATES, ReplaySource, XonClient, scan


def add_conn_args(ap):
    ap.add_argument("--address", help="connect to this address instead of scanning by name")
    ap.add_argument("--scan-timeout", type=float, default=15)


async def cmd_scan(args):
    found = await scan(args.scan_timeout)
    for dev, rssi in found:
        print(f"{dev.address}  {dev.name}  {rssi} dBm")
    if not found:
        print("no X.on found")


async def cmd_info(args):
    xon = XonClient(args.address, scan_timeout=args.scan_timeout)
    await xon.connect()
    try:
        for k, v in dataclasses.asdict(xon.info).items():
            print(f"{k:14s} {v}")
    finally:
        await xon.ble.disconnect()


async def cmd_lsl(args):
    from .lsl import LslOutlet

    if args.replay:
        src = ReplaySource(args.replay, speed=args.speed, loop=args.loop)
    else:
        src = XonClient(args.address, rate=args.rate, gain=args.gain, highpass=args.highpass,
                        scan_timeout=args.scan_timeout)
    src.on_event = lambda s: print(f"[{time.strftime('%H:%M:%S')}] {s}")
    async with src:
        outlet = LslOutlet(src.info, aux=args.aux, accel=not args.no_accel)
        print(f"LSL: '{outlet.eeg.get_info().name()}' {src.info.rate} Hz, "
              f"gain {src.info.gain}, highpass {src.info.highpass:g} Hz. Ctrl-C to stop")
        n = lost = 0
        sat = None
        last, t0 = None, time.monotonic()
        async for chunk in src.stream():
            outlet.push(chunk)
            n += len(chunk.seq)
            lost += chunk.lost
            s = chunk.saturated[:, :7].sum(0)
            sat = s if sat is None else sat + s
            last = chunk
            if time.monotonic() - t0 >= 5:
                dt = time.monotonic() - t0
                bad = ", ".join(f"{name} {100 * v / n:.0f}%" for name, v in zip(xp.CHANNEL_NAMES, sat) if v)
                print(f"{n / dt:6.1f} smp/s  lost {lost}  {last.battery_mv} mV  {last.rssi} dBm"
                      + (f"  saturated: {bad}" if bad else ""))
                n, sat, t0 = 0, None, time.monotonic()


def main():
    ap = argparse.ArgumentParser(prog="python -m xonpy", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    add_conn_args(sub.add_parser("scan", help="list advertising headsets"))
    add_conn_args(sub.add_parser("info", help="connect and print device info"))
    p = sub.add_parser("lsl", help="stream to Lab Streaming Layer")
    add_conn_args(p)
    p.add_argument("--rate", type=int, choices=RATES, default=250)
    p.add_argument("--gain", type=int, default=8)
    p.add_argument("--highpass", type=float, default=0.5)
    p.add_argument("--aux", action="store_true", help="add the AUX channel (raw) to the EEG stream")
    p.add_argument("--no-accel", action="store_true", help="no accelerometer stream")
    p.add_argument("--replay", help="stream a recording instead of the headset")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--loop", action="store_true", help="repeat the replay forever")
    for name in ("summary", "decode", "impedance"):
        sub.add_parser(name, help=f"offline: {name} a raw recording").add_argument("file")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    if args.cmd in ("summary", "decode", "impedance"):
        getattr(xp, f"cmd_{args.cmd}")(args.file)
        return
    try:
        asyncio.run({"scan": cmd_scan, "info": cmd_info, "lsl": cmd_lsl}[args.cmd](args))
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
