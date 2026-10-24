"""Brain Products X.on EEG headset over BLE, without the phone app.

    import asyncio, xonpy

    async def main():
        async with xonpy.XonClient() as headset:
            async for chunk in headset.stream():
                print(chunk.eeg.shape, chunk.eeg.mean(0))   # (6, 7) uV, F3 F4 C3 Cz C4 P3 P4

    asyncio.run(main())

LSL: xonpy.lsl.LslOutlet (needs pylsl), or `python -m xonpy lsl`.
"""
from .client import RATES, DeviceInfo, ReplaySource, XonClient, scan
from .data import EEG_NAMES, Chunk, ChunkBuilder

__all__ = ["XonClient", "ReplaySource", "DeviceInfo", "scan", "RATES",
           "Chunk", "ChunkBuilder", "EEG_NAMES"]
