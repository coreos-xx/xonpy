import asyncio
import uuid

import pytest

from xonpy import ReplaySource

from test_protocol import CAPTURE


def test_replay_stream_and_callbacks():
    async def run():
        src = ReplaySource(CAPTURE, speed=1000)
        packets, events = [], []
        src.on_packet = lambda raw, pkt: packets.append(pkt)
        src.on_event = events.append
        async with src:
            chunks = [c async for c in src.stream()]
        return chunks, packets, events

    chunks, packets, events = asyncio.run(run())
    assert len(packets) == 737          # every notification, incl. impedance-only ones
    assert len(chunks) == 727           # only packets with samples
    assert sum(len(c.seq) for c in chunks) == 4362
    assert events[-1] == "replay finished"


def test_lsl_roundtrip():
    pylsl = pytest.importorskip("pylsl")
    from xonpy import DeviceInfo
    from xonpy.data import ChunkBuilder
    from xonpy.lsl import LslOutlet
    from xonpy import protocol as xp
    from test_data import eeg_packet

    serial = f"test-{uuid.uuid4().hex[:8]}"
    out = LslOutlet(DeviceInfo(name="t", address="t", serial=serial, rate=250, gain=8))
    found = pylsl.resolve_byprop("source_id", f"xon-{serial}", timeout=5)
    assert found, "LSL stream not visible (multicast blocked?)"
    inlet = pylsl.StreamInlet(found[0])
    info = inlet.info(timeout=5)
    ch = info.desc().child("channels").child("channel")
    assert ch.child_value("label") == "F3" and ch.child_value("unit") == "microvolts"
    inlet.open_stream(timeout=5)

    chunk = ChunkBuilder()(eeg_packet(), 0)
    import time
    chunk.time = time.time()
    out.push(chunk)
    data, ts = inlet.pull_chunk(timeout=5, max_samples=6)
    assert len(data) == 6
    assert data[0] == pytest.approx(chunk.eeg[0].tolist())
    assert ts[1] - ts[0] == pytest.approx(1 / 250, abs=1e-6)
