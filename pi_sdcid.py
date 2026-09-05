import sys, time, struct, spidev

class SD:
    def __init__(self, bus=0, dev=0, freq=400_000):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, dev)
        self.spi.max_speed_hz = freq
        self.spi.mode = 0
        self.bits = 8

    def _tl(self, frame, pad, low_first=False):
        tx = list(frame) + [0xFF] * pad
        rx = self.spi.xfer2(tx)
        return rx

    def _send_raw(self, cmd, arg=0, crc=0x00):
        arg &= 0xFFFFFFFF
        return [((cmd & 0x7F) | 0x40), (arg >> 24) & 0xFF, (arg >> 16) & 0xFF,
                (arg >> 8) & 0xFF, arg & 0xFF, crc & 0xFF]

    def _r1(self, rx, start=6):
        for i in range(start, len(rx)):
            if rx[i] != 0xFF:
                return rx[i], i
        return None, None

    def clock(self, n):
        self.spi.xfer2([0xFF] * n)

    def init(self):
        self.clock(20)
        self.spi.xfer2([0xFF] * 12)
        r1, _ = self.acmd(0, 0, crc=0x95, idle=0x01)
        if r1 != 0x01:
            raise RuntimeError("CMD0 failed r1=%#x" % (r1 or 0))
        rx = self._tl(self._send_raw(8, 0x1AA, 0x87), 6)
        r1, idx = self._r1(rx)
        if r1 is None or r1 not in (0x01, 0x05):
            raise RuntimeError("CMD8 failed r1=%s" % r1)
        echo = bytes(rx[idx + 1:idx + 5])
        if len(echo) < 4 or echo[2] != 0x01 or echo[3] != 0xAA:
            raise RuntimeError("CMD8 echo not 1AA: %s" % echo.hex())
        self.spi.max_speed_hz = 800_000
        for _ in range(100):
            rx = self._tl(self._send_raw(55), 6)
            _, _ = self._r1(rx)
            rx = self._tl(self._send_raw(41, 0x40000000), 4)
            r41, _ = self._r1(rx, 6)
            if r41 == 0x00:
                return True
            if r41 != 0x01:
                raise RuntimeError("ACMD41 failed r1=%#x" % (r41 or 0))
            time.sleep(0.01)
        raise RuntimeError("card init timeout")

    def acmd(self, cmd, arg=0, crc=0x00, idle=None):
        rx = self._tl(self._send_raw(cmd, arg, crc), 6)
        r, _ = self._r1(rx)
        return r, rx

    def read_register(self, cmd):
        self.spi.xfer2([0xFF] * 2)
        frame = self._send_raw(cmd, 0, 0x00)
        rx = self._tl(frame, 44)
        r1, idx = self._r1(rx, 6)
        if r1 is None or r1 != 0x00:
            raise RuntimeError("CMD%d r1=%s" % (cmd, r1))
        i = idx + 1
        while i < len(rx) and rx[i] == 0xFF:
            i += 1
        if i >= len(rx) or rx[i] != 0xFE:
            raise RuntimeError("CMD%d no data token (%#x)" % (cmd, rx[i] if i < len(rx) else 0xFF))
        i += 1
        data = bytes(rx[i:i + 16])
        self.spi.xfer2([0xFF] * 2)
        return data

    def cid(self):
        return self.read_register(2)

    def csd(self):
        return self.read_register(9)


def decode_cid(cid):
    mid = cid[0]
    oid = cid[1:3].decode("ascii", "replace")
    name = cid[3:8].decode("ascii", "replace").rstrip("\x00 ")
    prv = cid[8]
    prv_str = "%d.%d" % (prv >> 4, prv & 0x0F)
    serial = struct.unpack(">I", cid[9:13])[0]
    date_bytes = cid[13:15]
    month_a = date_bytes[0] & 0x0F
    year_a = (date_bytes[0] >> 4) + 2000
    month_b = date_bytes[1] & 0x0F
    year_b = (date_bytes[1] >> 4) + 2000
    return dict(mid=mid, oid=oid, name=name, prv=prv_str, serial=serial,
                date_raw="%02X%02X" % (date_bytes[0], date_bytes[1]),
                date_a="%04d-%02d" % (year_a, month_a),
                date_b="%04d-%02d" % (year_b, month_b))


def main():
    bus = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    dev = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    sd = SD(bus, dev)
    try:
        sd.init()
        cid = sd.cid()
        csd = sd.csd()
        print("CID  :", cid.hex().upper())
        print("CSD  :", csd.hex().upper())
        d = decode_cid(cid)
        print("MID  (Manufacturer ID): 0x%02X" % d["mid"])
        print("OID  (OEM/App ID)     : %r" % d["oid"])
        print("PNM  (Product Name)   : %r" % d["name"])
        print("PRV  (Revision)       : %s" % d["prv"])
        print("PSN  (Serial No)      : %08X" % d["serial"])
        print("MDT  (raw)            : %s" % d["date_raw"])
        print("MDT  (variant A)      : %s" % d["date_a"])
        print("MDT  (variant B)      : %s  <-- do not activate Linux date field" % d["date_b"])
    finally:
        sd.spi.close()


if __name__ == "__main__":
    main()
