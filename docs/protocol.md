# X.on BLE protocol

Reverse-engineered description of how the Brain Products X.on EEG headset talks over Bluetooth Low Energy. Brain Products publishes no SDK or protocol, so everything here comes from:
- an Android HCI snoop log of the official X.on app,
- reading the GATT descriptors directly,
- recording sessions with wet electrodes (touch tests, blinks, eyes closed).

Each statement says how well it is confirmed. This document is the reference for `xonpy/protocol.py` and for ports to other platforms (e.g. ESP32).

## Device

- 7 EEG channels: F3, F4, C3, Cz, C4, P3, P4 (10-20 positions). Ear clip for reference and ground, one AUX input, 3-axis accelerometer.
- Advertises as `X.on <serial>`, so scan for the name prefix `X.on`. The advertisement carries no service UUIDs.
- The address is a **random static** address. On stacks where you pick the address type (NimBLE, Zephyr…), use *random*.
- **No pairing or encryption.** The official app never bonds.
- The headset requests **MTU 247** right after connecting. Notifications are 166 bytes, so the default MTU of 23 is not enough.
- Only one central at a time. Close the phone app before connecting from anything else.

## GATT

One custom service: **`e2951400-981d-405f-9d60-ea9518566b8b`**. All custom UUIDs share the suffix `-981d-405f-9d60-ea9518566b8b`. Look characteristics up by UUID; handles may differ between firmware versions.

The names in quotes are the devices' own 0x2901 user descriptions, and the formats come from the 0x2904 descriptors.

| UUID prefix | Props | Name | Format / meaning |
|---|---|---|---|
| `e2951401` | R/W | "Sample Rate" | sint16 LE, Hz. Power-on 125. The app offers 125 and 250 (`fa 00`) |
| `e2951402` | R/W | "Recording Mode" | uint8: `00` stop, `01` EEG, `02` impedance check |
| `e2951403` | R/W | "Amplifier gain" | sint16 LE. Power-on 1, the app writes 8 |
| `e2951404` | R | "Amplifier resolutions" | 3 × float32 LE: **2.384** (µV/LSB at gain 1), **15.625** (accelerometer mg/LSB), 0 |
| `e2951405` | R/W | "Amplifier highpass" | float32 LE, Hz. Power-on 0.1, the app writes 0.5 (`00 00 00 3f`) |
| `e2951406` | R/W | "Recording Options" | uint8. The app writes `00`, reads back `03 00 00 00 00 00 00 00`. Meaning unknown |
| `e2951200` | R | "Serial Number" | ASCII |
| `e2951201` | R | "FW+HW Version" | 4 bytes |
| `e2951202` | R | "Battery voltage" | uint16 LE, mV |
| `e2951203` | R | "Device RSSI" | sint8, dBm (as seen by the headset) |
| `e2951204` | R | "Channel energy" | float32 LE array, values around −70 (dB?). Not decoded |
| `e2952000` | R/W/Notify | "ADC Values" | **the data stream**, see below |

## Start and stop

What the official app does, in order. All writes are Write Requests (with response).

**Start**
1. Connect. The headset requests an MTU of 247.
2. `00` → `e2951406` (Recording Options)
3. `fa 00` → `e2951401` (250 Hz). The app reads it back.
4. `08 00` → `e2951403` (gain 8)
5. `00 00 00 3f` → `e2951405` (highpass 0.5 Hz)
6. Optional: read `e2951404` (resolutions)
7. Enable notifications on `e2952000`. The app writes `00 00` and then `01 00` to its CCCD. Writing `01 00` alone works too, and BlueZ does this for you.
8. `01` → `e2951402` (start EEG). `02` starts the impedance check instead.

**Stop**
1. Disable notifications on `e2952000` (`00 00` → CCCD)
2. `00` → `e2951402`

If the link drops, run the full start sequence again after reconnecting. The sample counter restarts at 1.

## Notification format (`e2952000`)

A notification is a list of TLV records: `tag (u8)`, `length (u8)`, `payload`.

| Tag | Length | Record |
|---|---|---|
| `0x90` | 23 | sample |
| `0xB0` | 14 | trailer |
| `0xA0` | 20 | impedance (impedance mode only) |

In EEG mode each notification is **166 bytes = 6 samples (6 × 25) + trailer (16)**. At 250 Hz that is 41.7 notifications/s, about 7 kB/s.

### Sample record (25 bytes including tag and length)

| Offset | Type | Meaning | Confidence |
|---|---|---|---|
| 0 | u8 | `0x90` | confirmed |
| 1 | u8 | `0x17` (23) | confirmed |
| 2–3 | u16 LE | sample counter, +1 per sample, starts at 1, wraps at 65535 | confirmed, no gaps seen on a good link |
| 4–5 | u16 LE | status: `0` in EEG mode. In impedance mode: `1` on the first record, then alternating `2`/`3` every 0.5 s | observed |
| 6–19 | 7 × i16 LE | EEG: **F3, F4, C3, Cz, C4, P3, P4** | confirmed (touch tests) |
| 20–21 | i16 LE | AUX | scale unknown |
| 22–24 | 3 × i8 | accelerometer X, Y, Z | confirmed |

`0x7FFF` / `0x8000` mean the channel is at the ADC rail: saturated, or the electrode has no contact. Dry electrodes are saturated almost all the time, so use wet (saline) electrodes.

### Trailer (16 bytes including tag and length)

```
b0 0e | u32 LE time_ms | u32 LE unknown | u16 LE last sample counter | i8 RSSI | u16 LE battery mV | u8 options
```

- `time_ms` rises by 24 per packet at 250 Hz (6 samples × 4 ms).
- The unknown u32 is always 0 on Linux/BlueZ, and sometimes close to `time_ms` with the Android app.
- RSSI and battery match the `e2951203` / `e2951202` characteristics. `options` was always 3.

### Impedance mode

- The headset injects a **10 Hz test signal** into all channels, so impedance-mode samples are not usable as EEG.
- Occasional 22-byte notifications carry a single `a0 14` record with 10 × i16 LE, probably per-electrode impedances. **Not decoded**: every value read 32767 in all tests so far.

## Units

| Quantity | Conversion | Confidence |
|---|---|---|
| EEG | **µV = raw × resolution / gain**, i.e. 2.384 / 8 = **0.298 µV/LSB** at the app's gain 8 | high (see below) |
| Accelerometer | g = raw × 15.625 / 1000 (±2 g in an int8) | confirmed, reads ≈ 1 g at rest |
| AUX | unknown | – |

The resolution characteristic reads the same at gain 1 and gain 8, but the raw data grows with gain. Converted with resolution / gain, eye blinks come out at +90–114 µV on F3/F4, decreasing towards the back of the head, and resting EEG is 4–10 µV RMS. Both are textbook values, and blinks are positive at frontal sites, so the polarity is not inverted. With eyes closed there is a clear alpha peak around 10–11 Hz over the central and parietal channels.

At gain 8 the 16-bit range covers about ±9.8 mV. Gain 1 has a noticeably higher noise floor in µV, so stay with gain 8.

## Open questions

- The `a0 14` impedance records and the "Channel energy" characteristic.
- The "Recording Options" bits, and the unknown trailer u32.
- The AUX scale.
- What the app's "extended range" option writes (probably a different gain).
