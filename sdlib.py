"""sdlib: Raspberry Pi + SPI uzerinden SD kart kontrol kutuphanesi.

Tum SD SPI komutlari: CID/CSD/SCR/OCR/RCA okuma, blok okuma/yazma,
CID/CSD yazma, sifre (CMD42), yazma korumasi (CMD28-30), zorla silme (CMD32/33/38).
"""

import sys
import time
import struct
import collections
import spidev

DEBUG = False


def log(msg):
    if DEBUG:
        print("[SD] %s" % msg, file=sys.stderr)


class SDError(Exception):
    pass


class SDBackend:
    """fatdump.FatReader icin SPI blok okuma arayuzu (LRU cache'li)."""

    def __init__(self, sd, cache_size=128):
        self.sd = sd
        self.cache = collections.OrderedDict()
        self.cache_size = cache_size

    def read_sectors(self, lba, count):
        return b"".join(self._one(lba + i) for i in range(count))

    def _one(self, lba):
        if lba in self.cache:
            self.cache.move_to_end(lba)
            return self.cache[lba]
        data = self.sd.read_block(lba)
        self.cache[lba] = data
        while len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return data


class SD:
    def __init__(self, bus=0, dev=0, freq=400_000):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, dev)
        self.freq = freq
        self.spi.max_speed_hz = freq
        self.spi.mode = 0
        self.bits = 8
        self.ocr = None
        self.ccs = 0

    def close(self):
        self.spi.close()

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
        return r1, rx, idx
    def init(self):
        time.sleep(0.01)
        r1 = None
        for _ in range(10):
            self.clock(20)
            self.clock(10)
            r1, _, _ = self.cmd(0, 0, 0x95)
            if r1 == 0x01:
                break
            time.sleep(0.01)
        if r1 != 0x01:
            raise SDError("CMD0 failed r1=%#x (10 deneme)" % (r1 or 0))
        r1, rx, idx = self.cmd(8, 0x1AA, 0x87)
        if r1 not in (0x01, 0x05):
            raise SDError("CMD8 failed r1=%s" % r1)
        echo = bytes(rx[idx + 1:idx + 5])
        if len(echo) < 4 or echo[2] != 0x01 or echo[3] != 0xAA:
            raise SDError("CMD8 echo not 1AA")
        self.spi.max_speed_hz = 400_000
        for _ in range(1000):
            r55, _, _ = self.cmd(55, 0, 0)
            if r55 is None:
                time.sleep(0.005)
                continue
            r41, _, _ = self.cmd(41, 0x40000000)
            if r41 == 0x00:
                break
            if r41 != 0x01:
                raise SDError("ACMD41 failed r1=%#x" % (r41 or 0))
            time.sleep(0.01)
        else:
            raise SDError("card init timeout")
        r1, rx, idx = self.cmd(58, 0, 0)
        if r1 == 0x00:
            self.ocr = bytes(rx[idx + 1:idx + 5])
            # bit30 (CCS): 1=SDHC/SDXC (sektor adresi), 0=SDSC (bayt adresi)
            self.ccs = (self.ocr[0] >> 6) & 1
        else:
            self.ccs = 0
        if not self.ccs:
            # SDSC: blok uzunlugunu 512'ye sabitle
            self.cmd(16, 512, 0)
        # init tamamlandi; kullanici istedigi hiza cik (varsayilan 400 kHz)
        self.spi.max_speed_hz = max(400_000, self.freq)
        return True

    def _addr(self, sector):
        return sector if self.ccs else sector * 512

    def read_register(self, cmd, length=16, arg=0):
        frame = self._send_cmd(cmd, arg, 0x00)
        rx = self.spi.xfer2(frame + [0xFF] * 64)
        r1, idx = self._r1_of(rx, len(frame))
        if r1 != 0x00:
            raise SDError("CMD%d r1=%s" % (cmd, r1))
        i = idx + 1
        while i < len(rx) and rx[i] == 0xFF:
            i += 1
        if i >= len(rx) or rx[i] != 0xFE:
            raise SDError("CMD%d no data token (%#x)" % (cmd, rx[i] if i < len(rx) else 0xFF))
        i += 1
        data = bytes(rx[i:i + length])
        self.spi.xfer2([0xFF] * 2)
        return data

    def cid(self):
        # SPI modunda CID, CMD10 (SEND_CID) ile okunur.
        return self.read_register(10)

    def csd(self):
        return self.read_register(9)

    def scr(self):
        return self.read_register(51, length=8)

    def rca(self):
        r1, rx, idx = self.cmd(3, 0, 0)
        if r1 != 0x00:
            return None
        i = idx + 1
        while i < len(rx) and rx[i] == 0xFF:
            i += 1
        if i + 1 < len(rx):
            return int.from_bytes(bytes(rx[i:i + 2]), "big")
        return None

    def status(self):
        r1, _, _ = self.cmd(13, 0, 0)
        return r1

    def read_block(self, addr):
        frame = self._send_cmd(17, self._addr(addr))
        rx = self.spi.xfer2(frame + [0xFF] * (1 + 512 + 2 + 8))
        r1, idx = self._r1_of(rx, len(frame))
        if r1 != 0x00:
            raise SDError("CMD17 r1=%s" % r1)
        i = idx + 1
        while i < len(rx) and rx[i] == 0xFF:
            i += 1
        if i >= len(rx) or rx[i] != 0xFE:
            raise SDError("CMD17 no data token (%#x)" % (rx[i] if i < len(rx) else 0xFF))
        i += 1
        return bytes(rx[i:i + 512])

    def write_block(self, addr, data):
        if len(data) != 512:
            raise SDError("block must be 512 bytes")
        frame = self._send_cmd(24, self._addr(addr))
        rx = self.spi.xfer2(frame + [0xFF] * 8)
        r1, _ = self._r1_of(rx, len(frame))
        if r1 != 0x00:
            raise SDError("CMD24 r1=%s" % r1)
        self.spi.xfer2([0xFE] + list(data) + [0xFF, 0xFF])
        self._expect_data_response(24)

    def write_cid(self, cid_bytes):
        self._write_register(26, cid_bytes, "CID")

    def try_write_cid_idle(self, cid_bytes):
        """CMD26'yi idle durumda dener (fabrika programlama akisi).
        Donus: True = kabul edilip yazildi, False = reddedildi."""
        r1, _, _ = self.cmd(0, 0, 0x95)
        if r1 != 0x01:
            raise SDError("CMD0 reset failed r1=%#x" % (r1 or 0))
        frame = self._send_cmd(26, 0, 0x00)
        rx = self.spi.xfer2(frame + [0xFF] * 8)
        r1, _ = self._r1_of(rx, len(frame))
        # idle biti setli + illegal biti temiz = kabul
        accepted = r1 is not None and (r1 & 0x01) and not (r1 & 0x04)
        if accepted:
            # veri hemen gonder (gap veriyi kaydirabilir)
            self.spi.xfer2([0xFE] + list(cid_bytes) + [0xFF, 0xFF])
            rx = self.spi.xfer2([0xFF] * 32)
            log("CMD26 veri sonrasi: %s" % bytes(rx).hex())
            resp = None
            for b in rx:
                if (b & 0x1F) in (0x05, 0x0B, 0x0D):
                    resp = b & 0x1F
                    break
            if resp == 0x05:
                self._wait_busy()
                log("CMD26 veri: token 0x05 (kabul)")
            elif resp in (0x0B, 0x0D):
                raise SDError("CMD26 data error %#x" % resp)
            else:
                # token yok: kart dogrudan programlamaya gecmis olabilir, busy bekle
                log("CMD26 token yok (%#x), busy bekleniyor" % (resp if resp is not None else 0xFF))
                self._wait_busy(5.0)
                log("CMD26 busy bitti -> kabul varsayiliyor (dogrulama CID okumasiyla)")
        # programlama tamamlansin: bol saat + bekleme (CMD0'i erken atmayalim)
        t0 = time.time()
        while time.time() - t0 < 0.3:
            self.spi.xfer2([0xFF] * 16)
        time.sleep(0.2)
        self.init()
        return accepted

    def program_cid_idle(self, cid_bytes, target_hex):
        """Idle CMD26 varyantlarini dener, her seferinde okuma ile dogrular.
        Donus: (commit_edildi, varyant_adi)."""
        c = crc16(cid_bytes)
        blk_crc = [(c >> 8) & 0xFF, c & 0xFF]
        blk512 = bytes(cid_bytes) + b"\x00" * 496
        c512 = crc16(blk512)
        variants = ["token512", "prompt", "token"]
        for v in variants:
            r1, _, _ = self.cmd(0, 0, 0x95)
            if r1 != 0x01:
                raise SDError("CMD0 reset failed r1=%#x" % (r1 or 0))
            frame = self._send_cmd(26, 0, 0x00)
            rx = self.spi.xfer2(frame + [0xFF] * 8)
            r1, _ = self._r1_of(rx, len(frame))
            if r1 is None or not (r1 & 0x01) or (r1 & 0x04):
                log("CMD26 (%s) durumda reddedildi r1=%s" % (v, r1))
                continue
            if v == "token512":
                self.spi.xfer2([0xFE] + list(blk512) + [(c512 >> 8) & 0xFF, c512 & 0xFF])
                rxx = self.spi.xfer2([0xFF] * 32)
                log("token512-veri sonrasi: %s" % bytes(rxx[:16]).hex())
            elif v == "prompt":
                rxp = self.spi.xfer2([0xFF] * 32)
                log("prompt aramasi: %s" % bytes(rxp).hex())
                if 0xFE not in rxp:
                    log("prompt gelmedi, bu varyant atlandi")
                    self.init()
                    continue
                self.spi.xfer2(list(cid_bytes) + blk_crc)
                rxx = self.spi.xfer2([0xFF] * 32)
                log("prompt-veri sonrasi: %s" % bytes(rxx).hex())
            else:
                self.spi.xfer2([0xFE] + list(cid_bytes) + blk_crc)
                rxx = self.spi.xfer2([0xFF] * 32)
                log("token-veri sonrasi: %s" % bytes(rxx).hex())
            t0 = time.time()
            while time.time() - t0 < 0.3:
                self.spi.xfer2([0xFF] * 16)
            time.sleep(0.2)
            self.init()
            now = self.read_register(10)
            if now.hex().upper() == target_hex:
                return True, v
            log("varyant %s: commit yok, CID hala %s" % (v, now.hex().upper()))
        return False, None

    def write_csd(self, csd_bytes):
        self._write_register(27, csd_bytes, "CSD")

    def _write_register(self, cmd, data, name):
        if len(data) != 16:
            raise SDError("%s must be 16 bytes" % name)
        frame = self._send_cmd(cmd, 0, 0x00)
        rx = self.spi.xfer2(frame + [0xFF] * 8)
        r1, _ = self._r1_of(rx, len(frame))
        if r1 != 0x00:
            raise SDError("CMD%d r1=%s" % (cmd, r1))
        self.spi.xfer2([0xFE] + list(data) + [0xFF, 0xFF])
        self._expect_data_response(cmd)

    def _expect_data_response(self, cmd):
        rx = self.spi.xfer2([0xFF] * 16)
        resp = None
        for b in rx:
            if (b & 0x1F) in (0x05, 0x0B, 0x0D):
                resp = b & 0x1F
                break
        # 0x05 = kabul, 0x0B = CRC hatasi, 0x0D = yazma hatasi
        if resp != 0x05:
            raise SDError("CMD%d data error %#x" % (cmd, resp if resp is not None else 0xFF))
        self._wait_busy()

    def _wait_busy(self, timeout=2.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            rx = self.spi.xfer2([0xFF])
            if rx[0] == 0xFF:
                return
            time.sleep(0.002)
        raise SDError("card busy timeout")

    def set_password(self, pwd):
        self._lock_unlock(0x00, pwd)

    def clear_password(self, pwd):
        self._lock_unlock(0x01, pwd)

    def erase_password(self):
        self._lock_unlock(0x02, b"")

    def _lock_unlock(self, code, pwd):
        if len(pwd) > 16:
            raise SDError("password max 16 bytes")
        frame = self._send_cmd(42, 0, 0x00)
        rx = self.spi.xfer2(frame + [0xFF] * 8)
        r1, _ = self._r1_of(rx, len(frame))
        if r1 != 0x00:
            raise SDError("CMD42 r1=%s" % r1)
        block = bytes([code, len(pwd)]) + pwd
        block += b"\x00" * (512 - len(block))
        self.spi.xfer2([0xFE] + list(block) + [0xFF, 0xFF])
        self._expect_data_response(42)

    def set_write_protect(self, addr):
        r1, _, _ = self.cmd(28, self._addr(addr), 0)
        if r1 != 0x00:
            raise SDError("CMD28 r1=%s" % r1)

    def clear_write_protect(self, addr):
        r1, _, _ = self.cmd(29, self._addr(addr), 0)
        if r1 != 0x00:
            raise SDError("CMD29 r1=%s" % r1)

    def read_write_protect(self, addr):
        return self.read_register(30, length=4, arg=self._addr(addr))

    def erase_blocks(self, start, end):
        r1, _, _ = self.cmd(32, self._addr(start), 0)
        if r1 != 0x00:
            raise SDError("CMD32 r1=%s" % r1)
        r1, _, _ = self.cmd(33, self._addr(end), 0)
        if r1 != 0x00:
            raise SDError("CMD33 r1=%s" % r1)
        r1, _, _ = self.cmd(38, 0, 0)
        if r1 != 0x00:
            raise SDError("CMD38 r1=%s" % r1)
        self._wait_busy(60)


def crc16(data):
    """CRC16-CCITT (poly 0x1021, init 0, MSB-first) — SD veri bloklari."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


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


def decode_scr(scr):
    structure = scr[0] >> 4
    sd_spec = scr[0] & 0x0F
    erase_status = (scr[1] >> 7) & 1
    security = (scr[1] >> 4) & 0x07
    bus_widths = scr[1] & 0x0F
    erase = (scr[3] >> 4) & 0x03
    cmd_support = scr[3] & 0x07
    spec_map = {0: "1.0", 1: "1.10", 2: "2.00", 3: "3.00", 4: "4.00", 5: "4.10", 6: "5.00", 7: "5.10"}
    sec_map = {0: "none", 1: "SDSC", 2: "SDSC+", 3: "SDSC (ext)"}
    bw = []
    if bus_widths & 0x01:
        bw.append("1-bit")
    if bus_widths & 0x02:
        bw.append("4-bit")
    if bus_widths & 0x04:
        bw.append("8-bit")
    return dict(structure=structure, sd_spec=spec_map.get(sd_spec, "?"),
                sd_spec_raw=sd_spec, security=sec_map.get(security, "?"),
                security_raw=security, erase_status=erase_status,
                bus_widths=", ".join(bw) or "?", erase=erase,
                cmd_support=cmd_support)
