import numpy as np
import pytest

from xonpy import protocol as xp
from xonpy.data import ChunkBuilder

from test_protocol import capture_packets


def eeg_packet():
    """First EEG-mode packet of the capture: ch1-7 at -32768, AUX at +32767, accel set."""
    for b in capture_packets():
        p = xp.parse(b)
        if p.samples and p.samples[0].status == 0:
            return p


def test_chunk_units():
    p = eeg_packet()
    c = ChunkBuilder(uv_per_lsb=0.25, g_per_lsb=0.015625)(p, 123.0)
    assert c.time == 123.0
    assert c.eeg.shape == (6, 7) and c.eeg.dtype == np.float32
    raw = np.array([s.ch for s in p.samples])
    np.testing.assert_allclose(c.eeg, raw[:, :7] * 0.25)
    np.testing.assert_array_equal(c.aux, raw[:, 7])
    np.testing.assert_allclose(c.accel, np.array([s.accel for s in p.samples]) * 0.015625)
    np.testing.assert_array_equal(c.saturated, np.isin(raw, xp.RAIL))
    assert c.battery_mv == p.trailer.battery_mv and c.rssi == p.trailer.rssi


def test_chunk_lost_and_reset():
    b = ChunkBuilder()
    pkts = [xp.parse(x) for x in capture_packets()]
    data = [p for p in pkts if p.samples and p.samples[0].status == 0]
    assert b(data[0], 0).lost == 0
    assert b(data[2], 0).lost == 6        # skipped one packet = 6 samples
    b.reset()
    assert b(data[0], 0).lost == 0


def test_default_scale():
    c = ChunkBuilder()(eeg_packet(), 0)
    assert c.eeg[0, 0] == pytest.approx(-32768 * 2.384185791015625 / 8)
