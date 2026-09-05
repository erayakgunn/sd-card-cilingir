import sys, time, struct, spidev

DEBUG = False


def log(msg):
    if DEBUG:
        print("[SD] %s" % msg, file=sys.stderr)


class SD:
    def __init__(self, bus=0, dev=0, freq=400_000):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, dev)
        self.spi.max_speed_hz = freq
        self.spi.mode = 0
        self.bits = 8

    def clock(self, n):
        self.spi.xfer2([0xFF] * n)

    def _send_cmd(self, cmd, arg=0, crc=0x00):
        arg &= 0xFFFFFFFF
        return [((cmd & 0x7F) | 0x40),
                (arg >> 24) & 0xFF, (arg >> 16) & 0xFF,
                (arg >> 8) & 0xFF, arg & 0xFF,
                crc & 0xFF]

    def _r1_of(self, rx, start):
        for i in range(start, len(rx)):
            if rx[i] != 0xFF:
                return rx[i], i
        return None, None

    def cmd(self, cmd, arg=0, crc=0x00, pad=10):
        frame = self._send_cmd(cmd, arg, crc)
        rx = self.spi.xfer2(frame + [0xFF] * pad)
        r1, idx = self._r1_of(rx, len(frame))
        log("CMD%d arg=%#x -> r1=%s" % (cmd, arg, ("%#x" % r1) if r1 is not None else None))
        return r1, rx, idx

    def init(self):
        self.clock(20)
        self.clock(10)

        r1, _, _ = self.cmd(0, 0, 0x95)
        if r1 != 0x01:
            raise RuntimeError("CMD0 failed r1=%#x" % (r1 or 0))

        r1, rx, idx = self.cmd(8, 0x1AA, 0x87)
        if r1 not in (0x01, 0x05):
            raise RuntimeError("CMD8 failed r1=%s" % r1)
        echo = bytes(rx[idx + 1:idx + 5])
        if len(echo) < 4 or echo[2] != 0x01 or echo[3] != 0xAA:
            raise RuntimeError("CMD8 echo not 1AA: %s" % echo.hex())
        log("CMD8 echo OK")

        self.spi.max_speed_hz = 400_000

        ready = False
        for _ in range(1000):
            r55, _, _ = self.cmd(55, 0, 0x00)
            if r55 is None:
                time.sleep(0.005)
                continue
            r41, _, _ = self.cmd(41, 0x40000000)
            if r41 == 0x00:
                ready = True
                break
            if r41 != 0x01:
                raise RuntimeError("ACMD41 failed r1=%#x" % (r41 or 0))
            time.sleep(0.01)
        if not ready:
            raise RuntimeError("card init timeout")

        r1, rx, idx = self.cmd(58, 0, 0x00)
        if r1 == 0x00:
            self.ocr = bytes(rx[idx + 1:idx + 5])
            log("OCR: %s" % self.ocr.hex().upper())
        return True

    def read_register(self, cmd):
        frame = self._send_cmd(cmd, 0, 0x00)
        rx = self.spi.xfer2(frame + [0xFF] * 64)
        r1, idx = self._r1_of(rx, len(frame))
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
        # SPI modunda CID, CMD10 (SEND_CID) ile okunur; CMD2 (ALL_SEND_CID) SD-bus moduna aittir.
        return self.read_register(10)

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


def _csd_bit(csd, pos):
    return (csd[15 - (pos >> 3)] >> (pos & 7)) & 1


def _csd_bits(csd, hi, lo):
    v = 0
    for p in range(hi, lo - 1, -1):
        v = (v << 1) | _csd_bit(csd, p)
    return v


def decode_csd(csd):
    structure = _csd_bits(csd, 127, 126)
    read_bl_len = _csd_bits(csd, 83, 80)
    write_bl_len = _csd_bits(csd, 25, 22)
    trans_speed = _csd_bits(csd, 111, 104)
    if structure == 0:
        c_size = _csd_bits(csd, 73, 62)
        c_mult = _csd_bits(csd, 49, 47)
        capacity = (c_size + 1) * (1 << (c_mult + 2)) * (1 << read_bl_len)
        kind = "SDSC (standard capacity)"
    else:
        c_size = _csd_bits(csd, 69, 48)
        capacity = (c_size + 1) * (1 << 19)
        kind = "SDHC/SDXC"
    return dict(structure=structure, read_bl_len=read_bl_len,
                write_bl_len=write_bl_len, trans_speed=trans_speed,
                c_size=c_size, capacity=capacity, kind=kind)


def main():
    global DEBUG
    args = sys.argv[1:]
    if "--debug" in args:
        DEBUG = True
        args = [a for a in args if a != "--debug"]
    bus = int(args[0]) if len(args) > 0 else 0
    dev = int(args[1]) if len(args) > 1 else 0
    sd = SD(bus, dev)
    try:
        sd.init()
        cid = sd.cid()
        csd = sd.csd()
        print("CID  :", cid.hex().upper())
        print("CSD  :", csd.hex().upper())
        d2 = decode_csd(csd)
        print("CSD  type      : %s" % d2["kind"])
        print("CSD  capacity  : %d bytes (%.1f MiB / %.2f GiB)" % (
            d2["capacity"], d2["capacity"] / 1048576, d2["capacity"] / 1073741824))
        print("CSD  read blk  : %d bytes" % (1 << d2["read_bl_len"]))
        print("CSD  write blk : %d bytes" % (1 << d2["write_bl_len"]))
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
