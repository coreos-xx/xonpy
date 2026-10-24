"""Async sources of X.on data: the BLE headset (bleak) and a recording replay.

    async with XonClient() as xon:          # scan, connect, configure, start
        async for chunk in xon.stream():    # Chunk: eeg (n, 7) uV, accel (n, 3) g, ...
            print(chunk.eeg.mean(0))

Callbacks (on_chunk, on_packet, on_event, on_disconnect) are an alternative to stream().
"""
import asyncio, logging, struct, time
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner

from . import protocol as xp
from .data import Chunk, ChunkBuilder

log = logging.getLogger("xonpy")

RATES = (125, 250)   # what the official app offers


@dataclass
class DeviceInfo:
    name: str
    address: str
    serial: str = ""
    version: str = ""            # "FW+HW Version" bytes as hex
    battery_mv: int = 0
    rssi: int = 0
    resolution_uv: float = 2.384185791015625   # "Amplifier resolutions"[0], uV/LSB at gain 1
    accel_mg: float = 15.625
    rate: int = 0
    gain: int = 0
    highpass: float = 0.0


async def scan(timeout=5.0):
    """Returns [(BLEDevice, rssi)] for advertising X.on headsets."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return [(d, adv.rssi) for d, adv in found.values()
            if (adv.local_name or d.name or "").startswith(xp.NAME_PREFIX)]


class _Source:
    """Shared delivery: parse notifications, build chunks, feed callbacks and stream()."""

    def __init__(self):
        self.on_chunk = None        # callable(Chunk)
        self.on_packet = None       # callable(raw bytes, Packet or None), e.g. for raw logging
        self.on_event = None        # callable(str): connection state, writes, reads
        self.on_disconnect = None   # callable(), when the link drops (before any reconnect)
        self.info = None
        self._builder = ChunkBuilder()
        self._queues = []

    def _emit(self, text):
        log.info(text)
        if self.on_event:
            self.on_event(text)

    def _set_scale(self):
        self._builder = ChunkBuilder(xp.uv_per_lsb(self.info.resolution_uv, self.info.gain or 1),
                                     self.info.accel_mg / 1000)

    def _deliver(self, data: bytes, t: float):
        try:
            pkt = xp.parse(data)
        except ValueError:
            pkt = None
        if self.on_packet:
            self.on_packet(data, pkt)
        chunk = self._builder(pkt, t) if pkt else None
        if chunk is None:
            return
        if self.on_chunk:
            self.on_chunk(chunk)
        for q in self._queues:
            if q.full():          # slow consumer: drop the oldest
                q.get_nowait()
            q.put_nowait(chunk)

    def _end_streams(self):
        for q in self._queues:
            if q.full():
                q.get_nowait()
            q.put_nowait(None)

    async def stream(self, maxsize=2000):
        """Async iterator of Chunks until the source is closed."""
        q = asyncio.Queue(maxsize)
        self._queues.append(q)
        try:
            while (chunk := await q.get()) is not None:
                yield chunk
        finally:
            self._queues.remove(q)

    async def __aenter__(self):
        await self.connect()
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()


class XonClient(_Source):
    """BLE connection to one X.on headset."""

    def __init__(self, address=None, *, rate=250, gain=8, highpass=0.5, mode="eeg",
                 reconnect=True, scan_timeout=15.0, connect_timeout=20.0):
        super().__init__()
        if rate not in RATES:
            raise ValueError(f"rate must be one of {RATES}")
        if mode not in ("eeg", "impedance"):
            raise ValueError("mode must be 'eeg' or 'impedance'")
        self.address = address
        self.rate, self.gain, self.highpass, self.mode = rate, gain, highpass, mode
        self.reconnect = reconnect
        self.scan_timeout, self.connect_timeout = scan_timeout, connect_timeout
        self.ble = None          # the BleakClient, for raw GATT access
        self.device = None
        self._configure = True
        self._streaming = False
        self._closing = False
        self._reconnect_task = None

    @property
    def is_connected(self):
        return bool(self.ble and self.ble.is_connected)

    async def find(self):
        if self.address:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=self.scan_timeout)
        else:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (ad.local_name or d.name or "").startswith(xp.NAME_PREFIX),
                timeout=self.scan_timeout)
        if dev is None:
            raise RuntimeError("X.on not found. Is it switched on, and is the phone app closed?")
        return dev

    async def connect(self):
        self._closing = False
        self.device = self.device or await self.find()
        self._emit(f"connecting to {self.device.name} {self.device.address}")
        self.ble = BleakClient(self.device, disconnected_callback=self._on_ble_disconnect,
                               timeout=self.connect_timeout)
        await self.ble.connect()
        self._emit("connected")
        self.info = await self.read_info()

    async def read_info(self) -> DeviceInfo:
        rd = lambda uuid: self.ble.read_gatt_char(uuid)
        res = xp.scales(bytes(await rd(xp.CHR_SCALES)))
        return DeviceInfo(
            name=self.device.name, address=self.device.address,
            serial=bytes(await rd(xp.CHR_SERIAL)).decode("ascii", "replace"),
            version=bytes(await rd(xp.CHR_VERSION)).hex(),
            battery_mv=struct.unpack("<H", await rd(xp.CHR_BATTERY))[0],
            rssi=struct.unpack("<b", await rd(xp.CHR_RSSI))[0],
            resolution_uv=res[0], accel_mg=res[1],
            rate=struct.unpack("<h", await rd(xp.CHR_RATE))[0],
            gain=struct.unpack("<h", await rd(xp.CHR_GAIN))[0],
            highpass=struct.unpack("<f", await rd(xp.CHR_HIGHPASS))[0])

    async def _write(self, uuid, data):
        self._emit(f"write {uuid[:8]} {data.hex()}")
        await self.ble.write_gatt_char(uuid, data, response=True)

    async def _read(self, uuid, fmt):
        raw = bytes(await self.ble.read_gatt_char(uuid))
        self._emit(f"read {uuid[:8]} {raw.hex()}")
        return struct.unpack_from(fmt, raw)[0]

    async def start(self, configure=True):
        """Run the app's start sequence (CLAUDE.md). configure=False keeps the headset's settings."""
        self._configure = configure
        await self._write(xp.CHR_OPTIONS, b"\x00")
        if configure:
            await self._write(xp.CHR_RATE, struct.pack("<h", self.rate))
            await self._write(xp.CHR_GAIN, struct.pack("<h", self.gain))
            await self._write(xp.CHR_HIGHPASS, struct.pack("<f", self.highpass))
        self.info.rate = await self._read(xp.CHR_RATE, "<h")
        self.info.gain = await self._read(xp.CHR_GAIN, "<h")
        self.info.highpass = await self._read(xp.CHR_HIGHPASS, "<f")
        if configure and (self.info.rate, self.info.gain) != (self.rate, self.gain):
            raise RuntimeError(f"headset did not accept rate {self.rate} / gain {self.gain}, "
                               f"reads back {self.info.rate} / {self.info.gain}")
        self._set_scale()
        await self.ble.start_notify(xp.CHR_DATA, self._on_notify)
        self._emit("notify on")
        await self._write(xp.CHR_MODE, bytes([xp.MODES[self.mode]]))
        self._streaming = True

    async def stop(self):
        """Stop streaming, stay connected."""
        self._streaming = False
        if self.is_connected:
            await self.ble.stop_notify(xp.CHR_DATA)
            self._emit("notify off")
            await self._write(xp.CHR_MODE, bytes([xp.MODE_STOP]))

    async def close(self):
        """Stop, disconnect, end stream() iterators. No reconnect after this."""
        self._closing = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        try:
            await self.stop()
        except Exception as e:
            self._emit(f"stop sequence failed: {e}")
        if self.ble:
            await self.ble.disconnect()
        self._end_streams()

    def _on_notify(self, _, data):
        self._deliver(bytes(data), time.time())

    def _on_ble_disconnect(self, _):
        self._emit("disconnected")
        if self.on_disconnect:
            self.on_disconnect()
        if self._streaming and self.reconnect and not self._closing:
            self._reconnect_task = asyncio.ensure_future(self._reconnect())

    async def _reconnect(self):
        delay = 1.0
        while not self._closing:
            await asyncio.sleep(delay)
            try:
                await self.connect()
                self._builder.reset()
                await self.start(self._configure)
                self._emit("reconnected")
                return
            except Exception as e:
                self._emit(f"reconnect failed: {e}")
                if self.ble:
                    await self.ble.disconnect()
                self.device = None    # scan again next time
                delay = min(delay * 2, 30)


class ReplaySource(_Source):
    """Plays back an xon_record.py recording (or xon_notify.csv) with the original pacing."""

    def __init__(self, path, speed=1.0, loop=False):
        super().__init__()
        self.path, self.speed, self.loop = path, speed, loop
        self._task = None

    async def connect(self):
        self.rows = [(t, d) for t, kind, d in xp.read_rows(self.path) if kind == "notify"]
        if not self.rows:
            raise RuntimeError(f"no notifications in {self.path}")
        self.info = DeviceInfo(name="replay", address=self.path, serial="replay", rate=250, gain=8)
        for _, kind, data in xp.read_rows(self.path):
            if kind == "event" and data.startswith(("read e2951401", "read e2951403")):
                v = int.from_bytes(bytes.fromhex(data.split()[2]), "little", signed=True)
                if data.startswith("read e2951401"):
                    self.info.rate = v
                else:
                    self.info.gain = v
        self._emit(f"replaying {self.path} ({len(self.rows)} packets, {self.info.rate} Hz, gain {self.info.gain})")

    async def start(self, configure=True):
        self._set_scale()
        self._task = asyncio.ensure_future(self._run())

    async def _run(self):
        while True:
            t0, w0 = self.rows[0][0], time.monotonic()
            for t, data in self.rows:
                delay = (t - t0) / self.speed - (time.monotonic() - w0)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._deliver(bytes.fromhex(data), time.time())
            if not self.loop:
                break
            self._builder.reset()
        self._emit("replay finished")
        self._end_streams()

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def close(self):
        await self.stop()
        self._end_streams()
