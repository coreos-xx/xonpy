"""Lab Streaming Layer output: an EEG stream (uV) and an accelerometer stream (g)."""
import time

import numpy as np
import pylsl

from .client import DeviceInfo
from .data import EEG_NAMES, Chunk


def _channels(desc, channels):
    """channels: [(label, unit, type)]"""
    chans = desc.append_child("channels")
    for label, unit, kind in channels:
        c = chans.append_child("channel")
        c.append_child_value("label", label)
        c.append_child_value("unit", unit)
        c.append_child_value("type", kind)


class LslOutlet:
    """Pushes Chunks to LSL. Use as source.on_chunk = outlet.push.

    EEG stream: type "EEG", 7 channels F3 F4 C3 Cz C4 P3 P4 in microvolts (+ AUX raw if aux=True).
    Accel stream: type "Accelerometer", X Y Z in g, same rate.
    Timestamps: arrival time of each notification for its last sample, earlier samples
    back-filled at the nominal rate by liblsl. Inlets should enable dejitter (proc_dejitter).
    """

    def __init__(self, info: DeviceInfo, aux=False, accel=True, name=None):
        self.aux = aux
        sid = info.serial or info.address
        name = name or f"X.on {sid}"
        channels = [(n, "microvolts", "EEG") for n in EEG_NAMES] + ([("AUX", "raw", "AUX")] if aux else [])
        si = pylsl.StreamInfo(name, "EEG", len(channels), info.rate, pylsl.cf_float32, f"xon-{sid}")
        d = si.desc()
        d.append_child_value("manufacturer", "Brain Products")
        d.append_child_value("model", "X.on")
        d.append_child_value("serial_number", info.serial)
        _channels(d, channels)
        ref = d.append_child("reference")
        ref.append_child_value("label", "ear clip")
        acq = d.append_child("acquisition")
        acq.append_child_value("gain", str(info.gain))
        acq.append_child_value("highpass_hz", f"{info.highpass:g}")
        acq.append_child_value("resolution_uv_at_gain_1", f"{info.resolution_uv:g}")
        self.eeg = pylsl.StreamOutlet(si)

        self.acc = None
        if accel:
            si = pylsl.StreamInfo(f"{name} Accel", "Accelerometer", 3, info.rate, pylsl.cf_float32,
                                  f"xon-{sid}-accel")
            _channels(si.desc(), [(a, "g", "Accelerometer") for a in "XYZ"])
            self.acc = pylsl.StreamOutlet(si)

    def push(self, chunk: Chunk):
        ts = pylsl.local_clock() - (time.time() - chunk.time)   # arrival time on the LSL clock
        eeg = chunk.eeg
        if self.aux:
            eeg = np.column_stack([eeg, chunk.aux.astype(np.float32)])
        self.eeg.push_chunk(eeg.tolist(), ts)
        if self.acc:
            self.acc.push_chunk(chunk.accel.tolist(), ts)
