"""X.on EEG headset protocol: UUIDs, command values and notification parser.

Reverse-engineered from an HCI snoop of the official app, see CLAUDE.md.
Pure Python (stdlib only), no BLE dependency, so it can be tested on captured data
and serves as the reference for the ESP32 port.

The offline tools at the bottom work on xon_notify.csv and on xon_record.py recordings:
  python -m xonpy summary|decode|impedance FILE.csv
"""
import collections, csv, struct, sys
from dataclasses import dataclass

NAME_PREFIX = "X.on"

_SUFFIX = "-981d-405f-9d60-ea9518566b8b"
SERVICE = "e2951400" + _SUFFIX
# names from the 0x2901 descriptors
CHR_RATE = "e2951401" + _SUFFIX       # "Sample Rate", sint16 LE Hz. Power-on 125, app writes 250
CHR_MODE = "e2951402" + _SUFFIX       # "Recording Mode", MODE_* below
CHR_GAIN = "e2951403" + _SUFFIX       # "Amplifier gain", sint16 LE. Power-on 1, app writes 8
CHR_SCALES = "e2951404" + _SUFFIX     # "Amplifier resolutions", 3 x float32 LE: 2.384, 15.625, 0
CHR_HIGHPASS = "e2951405" + _SUFFIX   # "Amplifier highpass", float32 LE Hz. Power-on 0.1, app writes 0.5
CHR_OPTIONS = "e2951406" + _SUFFIX    # "Recording Options", app writes 00, reads back 03
CHR_SERIAL = "e2951200" + _SUFFIX     # "Serial Number"
CHR_VERSION = "e2951201" + _SUFFIX    # "FW+HW Version"
CHR_BATTERY = "e2951202" + _SUFFIX    # "Battery voltage", uint16 LE mV
CHR_RSSI = "e2951203" + _SUFFIX       # "Device RSSI", sint8 dBm
CHR_ENERGY = "e2951204" + _SUFFIX     # "Channel energy", float32 LE array
CHR_DATA = "e2952000" + _SUFFIX       # "ADC Values", notify stream

MODE_STOP, MODE_EEG, MODE_IMPEDANCE = 0, 1, 2
MODES = {"stop": MODE_STOP, "eeg": MODE_EEG, "impedance": MODE_IMPEDANCE}

CHANNEL_NAMES = ("F3", "F4", "C3", "Cz", "C4", "P3", "P4", "AUX")  # EEG order confirmed by touch test
RAIL = (32767, -32768)

TAG_SAMPLE, TAG_IMPEDANCE, TAG_TRAILER = 0x90, 0xA0, 0xB0


@dataclass
class Sample:
    seq: int        # u16, wraps
    status: int     # 0 in EEG mode. Impedance mode: 1 on the first record, then 2/3 alternating every 0.5 s
    ch: tuple       # 8 x int16 raw
    accel: tuple    # 3 x int8 raw, zero in impedance mode


@dataclass
class Trailer:
    t_ms: int       # +24 per packet at 250 Hz
    unk: int        # unknown, 0 on BlueZ, sometimes nonzero with the Android app
    last_seq: int
    rssi: int       # dBm, as seen by the headset
    battery_mv: int
    options: int    # always 3, matches the Recording Options readback


@dataclass
class Packet:
    samples: list
    trailer: "Trailer | None" = None
    impedance: "tuple | None" = None   # 10 x int16 from an a0 14 record
    unknown: tuple = ()                # (tag, payload) of unrecognised records


def parse(data: bytes) -> Packet:
    """Parse one notification. Records are TLV: tag u8, length u8, payload."""
    pkt = Packet(samples=[])
    unknown = []
    off = 0
    while off < len(data):
        if off + 2 > len(data):
            raise ValueError(f"truncated record header at {off}")
        tag, ln = data[off], data[off + 1]
        p = data[off + 2:off + 2 + ln]
        if len(p) != ln:
            raise ValueError(f"truncated record tag 0x{tag:02x} at {off}")
        if tag == TAG_SAMPLE and ln == 23:
            seq, status, *ch = struct.unpack_from("<HH8h", p)
            pkt.samples.append(Sample(seq, status, tuple(ch), struct.unpack_from("<3b", p, 20)))
        elif tag == TAG_TRAILER and ln == 14:
            pkt.trailer = Trailer(*struct.unpack_from("<IIHbHB", p))
        elif tag == TAG_IMPEDANCE and ln == 20:
            pkt.impedance = struct.unpack("<10h", p)
        else:
            unknown.append((tag, bytes(p)))
        off += 2 + ln
    pkt.unknown = tuple(unknown)
    return pkt


class SeqTracker:
    """Detects dropped samples from the u16 sample counter."""

    def __init__(self):
        self.last = None
        self.gaps = 0
        self.lost = 0

    def feed(self, seq):
        """Returns the number of samples lost just before this one."""
        lost = 0
        if self.last is not None:
            d = (seq - self.last) & 0xFFFF
            if d != 1:
                self.gaps += 1
                if 1 < d < 0x8000:
                    lost = d - 1
                    self.lost += lost
        self.last = seq
        return lost


def scales(raw: bytes):
    """Decode "Amplifier resolutions" (e2951404): (eeg_uv_per_lsb_at_gain_1, accel_mg_per_lsb, 0)."""
    return struct.unpack_from("<3f", raw)


def uv_per_lsb(resolution=2.384185791015625, gain=8):
    """EEG scale. The resolution value does not change with gain, the raw data does:
    blink and resting-EEG amplitudes fit resolution / gain (0.298 uV/LSB at gain 8)."""
    return resolution / gain


# --- offline tools -------------------------------------------------------

def read_rows(fn):
    """Yield (t, kind, data) from xon_notify.csv (t,handle,hex) or a recording (t,kind,data)."""
    with open(fn, newline="") as f:
        r = csv.DictReader(f)
        legacy = "hex" in r.fieldnames
        for row in r:
            if legacy:
                yield float(row["t"]), "notify", row["hex"]
            else:
                yield float(row["t"]), row["kind"], row["data"]


def cmd_summary(fn):
    n = bad = imp = 0
    lens, status = collections.Counter(), collections.Counter()
    unknown = collections.Counter()
    sat = [0] * 8
    nsamp = 0
    trk = SeqTracker()
    t0 = t1 = None
    for t, kind, data in read_rows(fn):
        if kind != "notify":
            print(f"{t:.3f} {kind}: {data}")
            continue
        b = bytes.fromhex(data)
        n += 1
        lens[len(b)] += 1
        t0 = t if t0 is None else t0
        t1 = t
        try:
            pkt = parse(b)
        except ValueError:
            bad += 1
            continue
        imp += pkt.impedance is not None
        for tag, _ in pkt.unknown:
            unknown[tag] += 1
        for s in pkt.samples:
            nsamp += 1
            trk.feed(s.seq)
            status[s.status] += 1
            for i, v in enumerate(s.ch):
                sat[i] += v in RAIL
    dur = (t1 - t0) if n > 1 else 0
    print(f"\n{n} notifications over {dur:.1f} s, lengths {dict(lens)}, unparseable {bad}")
    print(f"{nsamp} samples ({nsamp / dur if dur else 0:.1f}/s), status {dict(status)}, "
          f"seq gaps {trk.gaps} (lost {trk.lost}), impedance records {imp}")
    if unknown:
        print("unknown record tags:", {f"0x{k:02x}": v for k, v in unknown.items()})
    if nsamp:
        print("saturated:", "  ".join(f"{name} {100 * s / nsamp:.0f}%" for name, s in zip(CHANNEL_NAMES, sat)))


def cmd_decode(fn):
    w = csv.writer(sys.stdout)
    w.writerow(["t", "seq", "status", *CHANNEL_NAMES, "ax", "ay", "az", "label"])
    label = ""
    for t, kind, data in read_rows(fn):
        if kind == "marker":
            label = data
        elif kind == "notify":
            try:
                pkt = parse(bytes.fromhex(data))
            except ValueError:
                continue
            for s in pkt.samples:
                w.writerow([f"{t:.6f}", s.seq, s.status, *s.ch, *s.accel, label])


def cmd_impedance(fn):
    w = csv.writer(sys.stdout)
    w.writerow(["t", *(f"z{i}" for i in range(10)), "label"])
    label = ""
    for t, kind, data in read_rows(fn):
        if kind == "marker":
            label = data
        elif kind == "notify":
            try:
                pkt = parse(bytes.fromhex(data))
            except ValueError:
                continue
            if pkt.impedance:
                w.writerow([f"{t:.6f}", *pkt.impedance, label])
