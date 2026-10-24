#!/usr/bin/env python3
"""Dump BLE GATT traffic for the X.on headset from an Android btsnoop_hci.log.

Usage:  python tools/xon_snoop.py btsnoop_hci.log > xon_dump.txt   (also writes xon_notify.csv)
"""
import struct, sys, collections

TARGET_NAME = b"X.on"

def main(fn):
    d = open(fn, "rb").read()
    if d[:8] != b"btsnoop\x00":
        sys.exit("Not a btsnoop file (header %r)" % d[:8])
    ver, dl = struct.unpack(">II", d[8:16])
    off, recs = 16, []
    while off + 24 <= len(d):
        ol, il, fl, dr, ts = struct.unpack(">IIIIq", d[off:off + 24]); off += 24
        recs.append((ts, fl, d[off:off + il])); off += il
    print(f"# btsnoop v{ver} datalink {dl}, {len(recs)} records")

    # --- find X.on address from advertising reports, and its connection handles
    xon_addrs = set()
    for ts, fl, p in recs:
        if p and p[0] == 4 and len(p) > 3 and p[1] == 0x3E and TARGET_NAME in p:
            # legacy adv report: sub 0x02; extended: 0x0D. Addr follows event type.
            sub = p[3]
            if sub == 0x02:
                xon_addrs.add(bytes(p[7:13]))
            elif sub == 0x0D:
                xon_addrs.add(bytes(p[8:14]))
    conns = {}
    for ts, fl, p in recs:
        if p and p[0] == 4 and len(p) > 14 and p[1] == 0x3E and p[3] in (0x01, 0x0A):
            st = p[4]; h = struct.unpack("<H", p[5:7])[0]; addr = bytes(p[9:15])
            a = ":".join("%02X" % b for b in addr[::-1])
            print(f"# LE connection complete: status={st} handle=0x{h:03x} peer={a}"
                  + ("  <-- X.on" if addr in xon_addrs else ""))
            if st == 0:
                conns[h] = (addr in xon_addrs) or not xon_addrs
    print(f"# X.on addresses: {[':'.join('%02X'%b for b in a[::-1]) for a in xon_addrs]}")

    # --- reassemble ACL -> L2CAP, keep ATT (CID 4) on X.on handles
    t0 = None
    bufs = {}
    att = []  # (t, dir, handle, pdu)
    for ts, fl, p in recs:
        if not p or p[0] != 2 or len(p) < 5:
            continue
        hf, ln = struct.unpack("<HH", p[1:5])
        h, pb = hf & 0x0FFF, (hf >> 12) & 3
        if h not in conns or not conns[h]:
            continue
        direc = "RX" if fl & 1 else "TX"   # TX = phone -> headset
        data = p[5:5 + ln]
        key = (h, direc)
        if pb in (0, 2):
            bufs[key] = bytearray(data)
        else:
            if key not in bufs:
                continue
            bufs[key] += data
        b = bufs[key]
        if len(b) >= 4:
            l2len, cid = struct.unpack("<HH", b[:4])
            if len(b) >= 4 + l2len:
                if cid == 4:
                    if t0 is None: t0 = ts
                    att.append(((ts - t0) / 1e6, direc, h, bytes(b[4:4 + l2len])))
                del bufs[key]

    print(f"# ATT PDUs on X.on: {len(att)}")
    OPS = {0x01:"ErrorRsp",0x02:"MTUReq",0x03:"MTURsp",0x04:"FindInfoReq",0x05:"FindInfoRsp",
           0x08:"ReadByTypeReq",0x09:"ReadByTypeRsp",0x0A:"ReadReq",0x0B:"ReadRsp",
           0x10:"ReadByGroupReq",0x11:"ReadByGroupRsp",0x12:"WriteReq",0x13:"WriteRsp",
           0x1B:"Notify",0x1D:"Indicate",0x1E:"Confirm",0x52:"WriteCmd"}

    def uuid(b):
        if len(b) == 2: return "%04x" % struct.unpack("<H", b)[0]
        u = b[::-1].hex()
        return f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}"

    # --- GATT map
    print("\n## GATT DISCOVERY")
    for t, dr, h, pdu in att:
        op = pdu[0]
        if op == 0x11:
            n = pdu[1]
            for i in range(2, len(pdu), n):
                e = pdu[i:i + n]
                s, en = struct.unpack("<HH", e[:4])
                print(f"SERVICE   0x{s:04x}-0x{en:04x}  {uuid(e[4:])}")
        elif op == 0x09:
            n = pdu[1]
            for i in range(2, len(pdu), n):
                e = pdu[i:i + n]
                ah = struct.unpack("<H", e[:2])[0]
                if n in (7, 21):
                    props, vh = e[2], struct.unpack("<H", e[3:5])[0]
                    pr = [nm for bit, nm in [(2,"READ"),(4,"WRITE_NR"),(8,"WRITE"),(16,"NOTIFY"),(32,"INDICATE")] if props & bit]
                    print(f"  CHAR    decl 0x{ah:04x} value 0x{vh:04x}  {uuid(e[5:])}  {'|'.join(pr)}")
                else:
                    print(f"  RBT     0x{ah:04x}  {e[2:].hex()}")
        elif op == 0x05:
            fmt = pdu[1]; n = 4 if fmt == 1 else 18
            for i in range(2, len(pdu), n):
                e = pdu[i:i + n]
                print(f"    DESC  0x{struct.unpack('<H', e[:2])[0]:04x}  {uuid(e[2:])}")

    # --- full log except notifications (summarised below)
    print("\n## ATT LOG (notifications collapsed)")
    last_note = None; run = 0
    def flush():
        nonlocal run
        if run:
            print(f"      ... {run} notifications on 0x{last_note:04x}")
        run = 0
    for t, dr, h, pdu in att:
        op = pdu[0]
        if op == 0x1B:
            hh = struct.unpack("<H", pdu[1:3])[0]
            if run and hh == last_note:
                run += 1; continue
            flush(); last_note = hh; run = 1
            print(f"{t:9.3f} {dr} Notify 0x{hh:04x} len={len(pdu)-3} {pdu[3:].hex()}")
            continue
        flush()
        name = OPS.get(op, "op%02x" % op)
        if op in (0x12, 0x52, 0x0A, 0x0B, 0x1D):
            hh = struct.unpack("<H", pdu[1:3])[0] if op != 0x0B else None
            body = pdu[3:] if op != 0x0B else pdu[1:]
            hs = f"0x{hh:04x}" if hh is not None else "      "
            txt = "".join(chr(c) if 32 <= c < 127 else "." for c in body)
            print(f"{t:9.3f} {dr} {name:12s} {hs} {body.hex()}   |{txt}|")
        else:
            print(f"{t:9.3f} {dr} {name:12s} {pdu[1:].hex()}")
    flush()

    # --- notification statistics + samples
    print("\n## NOTIFICATION STREAMS")
    by = collections.defaultdict(list)
    for t, dr, h, pdu in att:
        if pdu[0] == 0x1B:
            by[struct.unpack("<H", pdu[1:3])[0]].append((t, pdu[3:]))
    for hh, lst in by.items():
        lens = collections.Counter(len(v) for _, v in lst)
        dur = lst[-1][0] - lst[0][0]
        rate = (len(lst) - 1) / dur if dur > 0 else 0
        print(f"handle 0x{hh:04x}: {len(lst)} pkts, {lst[0][0]:.2f}s..{lst[-1][0]:.2f}s, "
              f"{rate:.1f} pkt/s, lengths {dict(lens)}")
    # write every notification to CSV for offline decoding
    with open("xon_notify.csv", "w") as f:
        f.write("t,handle,hex\n")
        for hh, lst in by.items():
            for t, v in lst:
                f.write(f"{t:.6f},0x{hh:04x},{v.hex()}\n")
    print("# all notifications written to xon_notify.csv")
    for hh, lst in by.items():
        print(f"\n### first 40 notifications on 0x{hh:04x}")
        for t, v in lst[:40]:
            print(f"{t:9.3f} {v.hex()}")

if __name__ == "__main__":
    main(sys.argv[1])
