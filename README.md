# xonpy

Stream EEG from a **Brain Products X.on** headset straight to your computer over Bluetooth Low Energy, without the phone app, and publish it to **Lab Streaming Layer (LSL)**.

Brain Products publishes no SDK or protocol for the X.on. xonpy is built on a reverse-engineered description of the BLE protocol ([docs/protocol.md](docs/protocol.md)), checked against wet-electrode recordings.

> **Unofficial.** Not affiliated with or endorsed by Brain Products GmbH. "X.on" and "Brain Products" are their trademarks. This is not a medical device: don't use it for diagnosis or anything safety-relevant.

## Features

- Connects to the X.on directly. No pairing needed, and the phone app isn't used.
- EEG in **µV** for F3, F4, C3, Cz, C4, P3 and P4, plus the accelerometer in **g**, at 125 or 250 Hz.
- **LSL output** (an EEG stream plus an accelerometer stream) with channel labels and units, ready for LabRecorder, OpenViBE, MNE-LSL, BCI pipelines and so on.
- Async Python API: `async for chunk in headset.stream()`, or callbacks.
- Automatic reconnect, dropped-sample detection (sample counter), battery and RSSI in every packet.
- Replay of recordings through the same API, so code can be tested without the headset.
- Tools: a live GUI (electrode map plus scrolling traces), a raw recorder, and a parser for Android Bluetooth HCI snoop logs.

## What is verified

| Item | Status |
|---|---|
| Connection, start/stop sequence, packet format | confirmed |
| Channel order F3, F4, C3, Cz, C4, P3, P4 | confirmed by touch tests |
| µV scale (raw × 2.384 / gain) | high confidence (blink and resting-EEG amplitudes, alpha with eyes closed) |
| Accelerometer scale | confirmed |
| Battery voltage, RSSI | confirmed |
| Electrode impedance values, AUX scale | **not decoded yet** |

Developed and tested on Ubuntu 24.04 (BlueZ 5.72, Python 3.12). bleak also supports macOS and Windows, but these haven't been tested.

## Prerequisites

- A Brain Products X.on headset, charged, with **wet (saline) electrodes**. With dry electrodes the channels are almost always saturated.
- A computer with Bluetooth Low Energy (Bluetooth 4.0 or newer).
  - Linux: BlueZ 5.55 or newer (`bluetoothctl --version`), and your user must be allowed to use Bluetooth (the default on desktop distributions).
- Python 3.10 or newer.
- **Close the X.on app on your phone** (or switch the phone's Bluetooth off). The headset accepts only one connection at a time.
- For the GUI: a desktop session (Qt).

## Installation

```bash
git clone https://github.com/coreos-xx/xonpy.git
cd xonpy
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[lsl]"          # library + LSL
# optional extras: [gui] for the live viewer, [dev] for the tests, or all: ".[lsl,gui,dev]"
```

With [uv](https://docs.astral.sh/uv/): `uv venv && uv pip install -e ".[lsl]"`.

The `lsl` extra installs `pylsl`, whose wheels ship the native `liblsl` library for common platforms. If `import pylsl` complains about a missing liblsl, install it from the [liblsl releases](https://github.com/sccn/liblsl/releases).

## Quick start

Switch the headset on, then:

```bash
xonpy scan           # list X.on headsets in range
xonpy info           # connect and show serial, battery, rate, gain, ...
xonpy lsl            # stream to LSL until Ctrl-C (prints rate, lost samples, battery, saturation every 5 s)
```

`python -m xonpy …` works the same way. Useful options for `lsl`:

| Option | Meaning |
|---|---|
| `--rate 125\|250` | sample rate (default 250) |
| `--gain N` | amplifier gain (default 8, as in the app) |
| `--highpass HZ` | hardware high-pass (default 0.5, as in the app) |
| `--address ADDR` | connect to one specific headset |
| `--aux` | add the AUX channel (raw units) to the EEG stream |
| `--no-accel` | don't create the accelerometer stream |
| `--replay FILE.csv [--speed N] [--loop]` | stream a recording instead of the headset |

### LSL streams

| Stream | Type | Channels | Unit |
|---|---|---|---|
| `X.on <serial>` | `EEG` | F3 F4 C3 Cz C4 P3 P4 (+ AUX with `--aux`) | microvolts |
| `X.on <serial> Accel` | `Accelerometer` | X Y Z | g |

The source IDs are `xon-<serial>` and `xon-<serial>-accel`. Timestamps are the arrival time of each BLE packet, applied to its last sample, with earlier samples spaced at the nominal rate. Enable dejitter on the receiving side (`pylsl.proc_dejitter`, which LabRecorder does by default).

To look at the stream, use for example `pip install mne-lsl` and then `mne-lsl viewer`, or record with [LabRecorder](https://github.com/labstreaminglayer/App-LabRecorder).

## Python API

```python
import asyncio
import xonpy

async def main():
    async with xonpy.XonClient() as headset:          # scan, connect, configure, start
        print(headset.info)                           # DeviceInfo: serial, battery_mv, rate, gain, ...
        async for chunk in headset.stream():
            print(chunk.eeg.mean(axis=0))             # (n, 7) float32, µV

asyncio.run(main())
```

Each `Chunk` holds the samples of one BLE packet (6 at 250 Hz):

| Field | Shape / type | Content |
|---|---|---|
| `eeg` | (n, 7) float32 | µV, order `xonpy.EEG_NAMES` = F3 F4 C3 Cz C4 P3 P4 |
| `aux` | (n,) int16 | AUX, raw |
| `accel` | (n, 3) float32 | X Y Z in g |
| `saturated` | (n, 8) bool | sample at the ADC rail (no contact / overload), 7 EEG + AUX |
| `seq` | (n,) uint16 | headset sample counter |
| `lost` | int | samples missing just before this chunk |
| `time` | float | host `time.time()` when the packet arrived |
| `battery_mv`, `rssi` | int | from the packet trailer |

Callbacks instead of the iterator, and LSL from your own code:

```python
from xonpy.lsl import LslOutlet

async def main():
    headset = xonpy.XonClient(rate=250, gain=8, reconnect=True)
    headset.on_event = print                          # connection state, writes, reconnects
    await headset.connect()
    await headset.start()
    outlet = LslOutlet(headset.info)
    headset.on_chunk = outlet.push                    # called for every packet
    try:
        await asyncio.Event().wait()                  # run until cancelled
    finally:
        await headset.close()                         # stop streaming and disconnect
```

`xonpy.ReplaySource("recording.csv", speed=1.0)` has the same interface as `XonClient` and plays back a recording made with `tools/xon_record.py`.

## Tools

| Tool | What it does | Needs |
|---|---|---|
| `tools/xon_gui.py` | Live viewer: top-view electrode map (green = good contact, grey = saturated), 8 scrolling traces with a display-only 50 Hz notch and 1 Hz highpass, guided touch test and head tasks (eyes closed, blinks) with markers, notes. Records raw data like `xon_record.py`. `--replay FILE.csv` plays back a recording. | `[gui]` |
| `tools/xon_record.py` | Raw recorder for protocol work: dumps every characteristic and descriptor, logs every packet, write, marker and note to `recordings/<time>_<label>.csv` plus `.json`. | – |
| `tools/xon_snoop.py` | Parses an Android `btsnoop_hci.log` (Bluetooth HCI snoop) and prints the X.on GATT traffic and a notification summary. | – |
| `xonpy summary\|decode\|impedance FILE.csv` | Offline: summarize a recording, or decode it to one CSV row per sample. | – |

Checking electrode contact before a session: run `python tools/xon_gui.py` and wait until every circle on the map is green. With the headset off your head, the touch test shows which channel belongs to which electrode. Hold the ear clip between wet fingers and touch each electrode with a wet finger of the other hand.

## Tests

```bash
pip install -e ".[lsl,dev]"
pytest
```

The tests run on a captured packet log (`tests/data/xon_notify.csv`) and need no headset. The LSL test needs a working network stack (localhost multicast).

## Troubleshooting

- **"X.on not found"**: the headset is off or out of range, or still connected to the phone app. Close the app, or switch off the phone's Bluetooth.
- **Everything saturated / grey in the GUI**: the electrodes are too dry or not touching the skin, or the ear clip isn't on. Wet the sponges and the ear clip with saline.
- **Strong 50/60 Hz hum**: normal for a mains environment. Filter in your processing (the GUI notch is display-only), and keep away from power supplies.
- **Connection fails right after a crash**: `bluetoothctl disconnect <address>` or toggle Bluetooth, then try again.

## How it was reverse-engineered

1. Captured the official app's Bluetooth traffic with Android's HCI snoop log (Developer options → *Enable Bluetooth HCI snoop log*, then `adb bugreport`) and parsed it with `tools/xon_snoop.py`.
2. Read the GATT user-description descriptors, which name every characteristic ("Sample Rate", "Amplifier gain", "Battery voltage", …).
3. Recorded wet-electrode sessions with `tools/xon_gui.py`: touch tests for the channel order, and blinks and eyes-closed alpha for the µV scale.

Details and the open questions are in [docs/protocol.md](docs/protocol.md). Contributions are welcome, especially captures of the impedance check with good electrode contact, and of other firmware versions.

## Roadmap

- ESP32 / Arduino library (NimBLE) for embedded devices, so they can receive the headset directly.
- Decode the electrode impedance values.

## License

[MIT](LICENSE)
