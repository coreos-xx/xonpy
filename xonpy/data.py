"""Numpy view of the stream: packets -> chunks of samples in physical units."""
from dataclasses import dataclass

import numpy as np

from . import protocol as xp

EEG_NAMES = xp.CHANNEL_NAMES[:7]


@dataclass
class Chunk:
    """Samples from one notification (6 at 250 Hz)."""
    time: float              # host time.time() when the notification arrived
    seq: np.ndarray          # (n,) sample counter, u16, wraps
    eeg: np.ndarray          # (n, 7) float32 uV, order EEG_NAMES. Saturated samples stay at the rail value
    aux: np.ndarray          # (n,) int16 raw, scale unknown
    accel: np.ndarray        # (n, 3) float32 g
    saturated: np.ndarray    # (n, 8) bool, 7 EEG + AUX at the ADC rail
    status: np.ndarray       # (n,) 0 in EEG mode
    lost: int = 0            # samples missing (counter gap) just before this chunk
    battery_mv: "int | None" = None
    rssi: "int | None" = None


class ChunkBuilder:
    """Converts parsed packets to Chunks and tracks the sample counter."""

    def __init__(self, uv_per_lsb=xp.uv_per_lsb(), g_per_lsb=0.015625):
        self.uv_per_lsb = uv_per_lsb
        self.g_per_lsb = g_per_lsb
        self.seq = xp.SeqTracker()

    def reset(self):
        """Call after a restart, the headset counter starts again at 1."""
        self.seq = xp.SeqTracker()

    def __call__(self, pkt: xp.Packet, t: float) -> "Chunk | None":
        if not pkt.samples:
            return None
        lost = 0
        for s in pkt.samples:
            lost += self.seq.feed(s.seq)
        raw = np.array([s.ch for s in pkt.samples], dtype=np.int16)
        tr = pkt.trailer
        return Chunk(
            time=t,
            seq=np.array([s.seq for s in pkt.samples], dtype=np.uint16),
            eeg=(raw[:, :7].astype(np.float32) * np.float32(self.uv_per_lsb)),
            aux=raw[:, 7].copy(),
            accel=np.array([s.accel for s in pkt.samples], dtype=np.float32) * np.float32(self.g_per_lsb),
            saturated=(raw == 32767) | (raw == -32768),
            status=np.array([s.status for s in pkt.samples], dtype=np.uint16),
            lost=lost,
            battery_mv=tr.battery_mv if tr else None,
            rssi=tr.rssi if tr else None,
        )
