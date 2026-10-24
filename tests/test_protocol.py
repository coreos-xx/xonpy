import struct
from pathlib import Path

import pytest

from xonpy import protocol as xp

CAPTURE = Path(__file__).parent / "data" / "xon_notify.csv"


def capture_packets():
    return [bytes.fromhex(d) for _, kind, d in xp.read_rows(CAPTURE) if kind == "notify"]


def test_capture_parses():
    pkts = [xp.parse(b) for b in capture_packets()]
    assert len(pkts) == 737
    data = [p for p in pkts if p.samples]
    imp = [p for p in pkts if p.impedance]
    assert len(data) == 727 and all(len(p.samples) == 6 and p.trailer for p in data)
    assert len(imp) == 10 and all(not p.samples for p in imp)
    assert not any(p.unknown for p in pkts)


def test_first_sample():
    s = xp.parse(capture_packets()[0]).samples[0]
    assert (s.seq, s.status) == (1, 1)
    assert s.ch == (-1306, -1301, -1160, -1116, -1173, -1283, -1236, -113)
    assert s.accel == (0, 0, 0)


def test_trailer():
    tr = xp.parse(capture_packets()[0]).trailer      # ... b0 0e 17000000 00000000 0600 e3 dc0f 03
    assert (tr.t_ms, tr.unk, tr.last_seq) == (0x17, 0, 6)
    assert (tr.rssi, tr.battery_mv, tr.options) == (-29, 0x0FDC, 3)


def test_truncated_and_unknown():
    with pytest.raises(ValueError):
        xp.parse(capture_packets()[0][:-3])
    p = xp.parse(b"\x42\x02\xaa\xbb")
    assert p.unknown == ((0x42, b"\xaa\xbb"),) and not p.samples


def test_seq_tracker():
    t = xp.SeqTracker()
    assert [t.feed(s) for s in (65534, 65535, 0, 1, 4)] == [0, 0, 0, 0, 2]
    assert (t.gaps, t.lost) == (1, 2)


def test_scales():
    raw = bytes.fromhex("8096184000007a4100000000")
    res = xp.scales(raw)
    assert res[0] == pytest.approx(2.384185791015625) and res[1] == 15.625
    assert xp.uv_per_lsb(res[0], 8) == pytest.approx(0.298023, rel=1e-5)
    assert struct.pack("<f", 0.5) == bytes.fromhex("0000003f")   # app's highpass write
