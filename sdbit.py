"""sdbit: MH-SD modulu uzerinden 1-bit SD-bus bit-bang (libgpiod v2).

Kablo ayni: SCK=CLK, MOSI=CMD, MISO=DAT0, CS=DAT3 (yukukte), 3V3, GND.
Onemli: testten once karti cikar-tak (guç döngüsü) — mod, güç aninda secilir.

Kullanim:
  python3 sdbit.py probe --debug        # init + fabrika CID oku (CMD2)
  python3 sdbit.py program <hex16> [--fix-crc] [auto|samsung1|samsung2|direct]
                                           # vendor unlock -> CMD26 -> reset -> readback
  python3 sdbit.py restore [hex16] [--fix-crc] # kaynak CID'i geri yukle (varsayilan: SOURCE_CID)
"""

import sys
import time

import gpiod
from gpiod.line import Bias, Direction, Value

CLK, CMD, DAT0, DAT3 = 11, 10, 9, 8  # BCM (Pi header)

BIT_NS = 3000  # ~300 kHz hedef (sys overhead ile ~50-100 kHz)

# Original card CID supplied for this project.  Used by `restore` as a
# convenience default; it is never silently written without verification.
SOURCE_CID = bytes.fromhex("5D53424C31424E31061D0E354501571D")


def find_chip():
    for p in ("/dev/gpiochip4", "/dev/gpiochip0"):
        try:
            c = gpiod.Chip(p)
            c.close()
            return p
        except Exception:
            continue
    raise RuntimeError("gpiochip bulunamadi")


def log(msg, debug=True):
    if debug:
        print("[SD] %s" % msg, file=sys.stderr)


class Bus:
    def __init__(self, chip_path=None, debug=False):
        self.debug = debug
        self.cmd_out = False
        self.d0_out = False
        self.req = gpiod.request_lines(
            chip_path or find_chip(),
            consumer="sdbus",
            config={
                CLK: gpiod.LineSettings(direction=Direction.OUTPUT,
                                        output_value=Value.INACTIVE),
                CMD: gpiod.LineSettings(direction=Direction.INPUT,
                                        bias=Bias.PULL_UP),
                DAT0: gpiod.LineSettings(direction=Direction.INPUT,
                                         bias=Bias.PULL_UP),
                DAT3: gpiod.LineSettings(direction=Direction.OUTPUT,
                                         output_value=Value.ACTIVE),
            },
        )

    def close(self):
        self.req.release()

    def _spin(self, ns):
        end = time.perf_counter_ns() + ns
        while time.perf_counter_ns() < end:
            pass

    def clk_hi(self):
        self.req.set_value(CLK, Value.ACTIVE)

    def clk_lo(self):
        self.req.set_value(CLK, Value.INACTIVE)

    def cmd_reconf(self, out):
        self.cmd_out = out
        s = (gpiod.LineSettings(direction=Direction.OUTPUT, bias=Bias.PULL_UP,
                                output_value=Value.ACTIVE) if out
             else gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP))
        self.req.reconfigure_lines({CMD: s})

    def d0_reconf(self, out):
        self.d0_out = out
        s = (gpiod.LineSettings(direction=Direction.OUTPUT, bias=Bias.PULL_UP,
                                output_value=Value.ACTIVE) if out
             else gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP))
        self.req.reconfigure_lines({DAT0: s})

    def get_cmd(self):
        return 1 if self.req.get_value(CMD) == Value.ACTIVE else 0

    def get_d0(self):
        return 1 if self.req.get_value(DAT0) == Value.ACTIVE else 0

    # --- bit katmani ---

    def tx_bit(self, cmd_v, d0_v=None):
        self.clk_lo()
        if cmd_v:
            # SD-bus CMD open-drain: 1 biti icin hatti birak.
            if self.cmd_out:
                self.cmd_reconf(False)
        else:
            # 0 biti icin hatti low sur.
            if not self.cmd_out:
                self.cmd_reconf(True)
            self.req.set_value(CMD, Value.INACTIVE)
        if d0_v is not None and self.d0_out:
            self.req.set_value(DAT0, Value.ACTIVE if d0_v else Value.INACTIVE)
        self._spin(BIT_NS // 3)
        self.clk_hi()
        self._spin(2 * BIT_NS // 3)

    def rx_bit(self):
        """SD-bus verisini rising edge sonrasinda ornekle."""
        self.clk_lo()
        self._spin(BIT_NS // 2)
        self.clk_hi()
        self._spin(BIT_NS // 3)
        v = self.get_cmd()
        self._spin(BIT_NS // 6)
        return v

    def rx_bit_d0(self):
        self.clk_lo()
        self._spin(BIT_NS // 2)
        self.clk_hi()
        self._spin(BIT_NS // 3)
        v = self.get_d0()
        self._spin(BIT_NS // 6)
        return v

    def clocks_idle(self, n):
        for _ in range(n):
            self.tx_bit(1, 1)

    def bits_to_bytes(self, bits):
        out = bytearray()
        for i in range(0, len(bits) - 7, 8):
            v = 0
            for b in bits[i:i + 8]:
                v = (v << 1) | b
            out.append(v)
        return bytes(out)

    # --- komut katmani ---

    def send_command(self, cmd, arg, crc, rx_bits=0):
        """Komut gonder + yanit bitlerini topla. Donus: bit listesi (None olmaz)."""
        # SD-bus: ardışık komutlar arasında Ncs için en az 8 boş clock.
        self.clocks_idle(8)
        frame = [0, 1]
        v = cmd & 0x3F
        for i in range(5, -1, -1):
            frame.append((v >> i) & 1)
        a = arg & 0xFFFFFFFF
        for i in range(31, -1, -1):
            frame.append((a >> i) & 1)
        for i in range(6, -1, -1):
            frame.append((crc >> i) & 1)
        frame.append(1)
        self.cmd_reconf(True)
        self.d0_reconf(False)  # DAT0 released
        for b in frame:
            self.tx_bit(b, 1)
        self.cmd_reconf(False)
        # Ncr: start biti (0) bekle (en fazla 64 bit)
        skipped = []
        start = None
        for _ in range(64):
            v = self.rx_bit()
            if v == 0:
                start = True
                break
            skipped.append(v)
        if not start:
            return None
        bits = [0] + self.rx_bits_inner(rx_bits - 1) if rx_bits else [0]
        return bits

    def rx_bits_inner(self, n):
        return [self.rx_bit() for _ in range(n)]

    def r1_byte(self, bits):
        if not bits:
            return None
        b8 = bits[:8]
        while len(b8) < 8:
            b8.append(1)
        return self.bits_to_bytes(b8 + [0])[0] & 0xFF


def crc7_sd(data, mask=0x09):
    """SD komut CRC7 (dogrulanmis: CMD0->0x4A, CMD8->0x43)."""
    crc = 0
    for b in data:
        d = b
        for _ in range(8):
            crc = (crc << 1) & 0xFF
            if ((d & 0x80) != 0) != ((crc & 0x80) != 0):
                crc ^= 0x09
            d = (d << 1) & 0xFF
    return crc & 0x7F


def crc16_sd(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


class SDCard:
    def __init__(self, debug=False):
        self.debug = debug
        self.bus = Bus(debug=debug)
        self.mask = 0x09

    def cmd(self, cmd, arg=0, crc=None, expect=True, total_bits=48):
        if crc is None:
            crc = crc7_sd([0x40 | (cmd & 0x3F),
                           (arg >> 24) & 0xFF, (arg >> 16) & 0xFF,
                           (arg >> 8) & 0xFF, arg & 0xFF], self.mask)
        bits = self.bus.send_command(cmd, arg, crc, rx_bits=total_bits)
        if not expect:
            return None
        return bits

    def init(self):
        b = self.bus
        b.cmd_reconf(True)
        b.d0_reconf(True)
        b.clocks_idle(80)
        # CMD0: yanit yok
        self.cmd(0, 0, crc=0x4A, expect=False)
        b.clocks_idle(8)
        r7 = self.cmd(8, 0x1AA, total_bits=48)
        r7b = self.bus.bits_to_bytes(r7) if r7 else None
        log("CMD8 resp: %s" % (r7b.hex() if r7b else "yok"), self.debug)
        time.sleep(0.01)
        # ACMD41 argument must include the host voltage window.  For a
        # 3.3 V host (and after CMD8=0x1AA), request 2.7-3.6 V:
        #   OCR[23:15] = 0xFF80, HCS = bit 30.
        # The previous code sent only HCS (0x40000000), which made the card
        # return its voltage-window OCR (00FF8000) with power-up bit 0 and
        # was then incorrectly treated as an initialization timeout.
        for hcs in (0x40000000, 0x00000000):
            acmd41_arg = 0x00FF8000 | hcs
            for attempt in range(500):
                self.cmd(55, 0, total_bits=48)
                time.sleep(0.001)
                r3 = self.cmd(41, acmd41_arg, total_bits=48)
                r3b = self.bus.bits_to_bytes(r3) if r3 else None
                if self.debug and attempt < 2:
                    log("ACMD41(%#x) resp=%s" % (acmd41_arg, r3b.hex() if r3b else "yok"), True)
                ocr = self._find_ready_ocr(r3b)
                if ocr is not None:
                    log("ACMD41 ready, OCR=%08x" % ocr, self.debug)
                    return True
                time.sleep(0.005)
            # Bir sonraki arguman icin karti yeniden idle'a al.
            self.cmd(0, 0, crc=0x4A, expect=False)
            self.bus.clocks_idle(8)
        # Bazi eski/standart-disi denetleyiciler SD-bus'ta ACMD41 yerine CMD1 kullanir.
        for attempt in range(500):
            r1 = self.cmd(1, 0, total_bits=48)
            r1b = self.bus.bits_to_bytes(r1) if r1 else None
            if self.debug and attempt < 2:
                log("CMD1 resp=%s" % (r1b.hex() if r1b else "yok"), True)
            ocr = self._find_ready_ocr(r1b)
            if ocr is not None:
                log("CMD1 ready, OCR=%08x" % ocr, self.debug)
                return True
            time.sleep(0.005)
        raise RuntimeError("ACMD41/CMD1 timeout: kart initialization ready biti setmedi")

    def _find_ready_ocr(self, response):
        """R3: ilk byte response header, sonraki 4 byte OCR."""
        if not response or len(response) < 5:
            return None
        value = int.from_bytes(response[1:5], "big")
        return value if value & 0x80000000 else None

    def read_cid(self):
        # CMD2: R2 = 136 bit
        bits = self.cmd(2, 0, total_bits=136)
        if not bits:
            return None
        raw = self.bus.bits_to_bytes(bits)
        # R2'nin ilk bayti response header; CID son 16 bayttir.
        return raw[1:17] if len(raw) >= 17 else raw

    def read_cid_selected(self, rca):
        # Secilmis/transfer state'te CID: CMD10 + RCA.
        bits = self.cmd(10, rca << 16, total_bits=136)
        if not bits:
            return None
        raw = self.bus.bits_to_bytes(bits)
        return raw[1:17] if len(raw) >= 17 else raw

    def _r1(self, cmd, arg=0, label=None):
        bits = self.cmd(cmd, arg, total_bits=48)
        raw = self.bus.bits_to_bytes(bits) if bits else None
        if label:
            log("%s: %s" % (label, raw.hex() if raw else "yok"), self.debug)
        return raw

    def _cmd62(self, arg, label=None):
        # CMD62 is reserved by the SD specification.  These values are
        # documented for some Samsung controllers; they are NOT claimed to
        # be a Swissbit command.  Keep every candidate visible in --debug.
        return self._r1(62, arg, label or ("CMD62 %#010x" % arg))

    def _cid_crc_ok(self, cid):
        if len(cid) != 16:
            return False
        # CID CRC7 covers the first 15 CID bytes.  The final CID byte is
        # CRC7[6:0] << 1 | 1.
        got = cid[15]
        calc = crc7_sd(cid[:15])
        return got == ((calc << 1) | 1)

    def _cid_with_valid_crc(self, cid):
        cid = bytes(cid)
        if len(cid) != 16:
            raise ValueError("CID 16 bayt olmali")
        calc = crc7_sd(cid[:15])
        return cid[:15] + bytes([(calc << 1) | 1])

    def _send_cid_data(self, cid):
        """Send the 16-byte CID data packet used by the existing CMD26 path."""
        b = self.bus
        b.d0_reconf(True)
        c16 = crc16_sd(cid)
        bits = []
        for byte in bytes([0xFE]) + bytes(cid) + bytes([(c16 >> 8) & 0xFF, c16 & 0xFF]):
            for i in range(7, -1, -1):
                bits.append((byte >> i) & 1)
        for v in bits:
            b.tx_bit(1, v)
        b.tx_bit(1, 1)
        b.d0_reconf(False)

        dr_bits = [b.rx_bit_d0() for _ in range(16)]
        log("data-response bitleri: %s" % "".join(map(str, dr_bits)), self.debug)
        # SD data response is xxx010status.  010 = accepted, 101 = CRC
        # error, 110 = write error.  With bit-bang timing we retain the raw
        # bits and explicitly classify the common patterns.
        dr = "".join(map(str, dr_bits))
        accepted = any(dr[i:i+3] == "010" for i in range(max(0, len(dr)-2)))
        crc_error = any(dr[i:i+3] == "101" for i in range(max(0, len(dr)-2)))
        write_error = any(dr[i:i+3] == "110" for i in range(max(0, len(dr)-2)))
        log("data-response classify: accepted=%s crc_error=%s write_error=%s" %
            (accepted, crc_error, write_error), self.debug)

        t0 = time.time()
        busy_seen = False
        while time.time() - t0 < 3.0:
            v = b.rx_bit_d0()
            if v == 0:
                busy_seen = True
            elif busy_seen:
                break
        if time.time() - t0 >= 3.0:
            raise RuntimeError("PROGRAM_TIMEOUT: DAT0 busy 3s+ (accepted=%s)" % accepted)
        log("busy bitti (busy_seen=%s)" % busy_seen, self.debug)
        return accepted, dr

    def _cmd26(self, cid, label="CMD26"):
        bits = self.cmd(26, 0, total_bits=48)
        r1 = self.bus.bits_to_bytes(bits) if bits else None
        log("%s resp: %s" % (label, r1.hex() if r1 else "yok"), self.debug)
        if not r1:
            raise RuntimeError("CMD26_NO_RESPONSE")
        # For an R1 response, bit 2 is ILLEGAL_COMMAND and bit 3 is CRC_ERROR.
        status = r1[-1]
        if status & 0x04:
            raise RuntimeError("CMD26_ILLEGAL_COMMAND (R1=%02X)" % status)
        accepted, dr = self._send_cid_data(cid)
        if not accepted:
            raise RuntimeError("DATA_REJECTED (response=%s)" % dr)
        return r1

    def _vendor_candidate_1(self):
        """Samsung-documented CMD62 candidate: EFAC62EC -> 00CCED82."""
        self._cmd62(0xEFAC62EC, "vendor #1 unlock A")
        self._cmd62(0x00CCED82, "vendor #1 unlock B")
        self._r1(16, 16, "CMD16/16-byte")

    def _vendor_candidate_2(self):
        """Older Samsung/Arduino candidate: EFAC62EC -> EF50 -> CMD17."""
        self._cmd62(0xEFAC62EC, "vendor #2 unlock A")
        self._cmd62(0x0000EF50, "vendor #2 unlock B")
        # CMD17 is part of the published candidate sequence.  We deliberately
        # only issue the command and clock a short observation window; reading
        # an arbitrary 512-byte block here would risk desynchronising the
        # single-wire bit-bang state if the controller actually accepts it.
        r = self._r1(17, 0, "CMD17/probe")
        if r is None:
            log("CMD17/probe: no R1; continuing to CMD26 candidate", self.debug)

    def _vendor_exit(self):
        try:
            self._cmd62(0x00DECCEE, "vendor exit")
        except Exception as e:
            log("vendor exit hata: %s" % e, self.debug)

    def _reset_to_identification(self):
        """Reset the card and run initialization again for a real readback."""
        self.bus.d0_reconf(True)
        self.bus.clocks_idle(16)
        self.cmd(0, 0, crc=0x4A, expect=False)
        self.bus.clocks_idle(80)
        self.init()

    def program_cid(self, cid, candidate="auto"):
        cid = bytes(cid)
        if len(cid) != 16:
            raise ValueError("CID 16 bayt olmali")

        old_cid = self.read_cid()
        if old_cid is None:
            raise RuntimeError("ORIGINAL_CID_READ_FAILED")
        print("ORIGINAL CID: %s" % old_cid.hex().upper())
        log("CMD2 onceki CID ham: %s" % old_cid.hex(), self.debug)

        # A CID is 128 bits, with the last byte containing CRC7 + end bit.
        # The CID supplied in the original project has 00 as its final byte,
        # which is not a valid CID CRC byte.  Fail closed unless --fix-crc is
        # requested by main(); callers pass the already-normalised value here.
        if not self._cid_crc_ok(cid):
            raise ValueError("TARGET_CID_CRC_INVALID: son byte=%02X beklenen=%02X" %
                             (cid[15], self._cid_with_valid_crc(cid)[15]))

        # CMD3 -> RCA, then CMD7 -> selected state.  Keep this before every
        # vendor candidate because controllers differ in which state they
        # expect for reserved commands.
        r6b = self._r1(3, 0, "CMD3/R6")
        rca = None
        if r6b and len(r6b) >= 3:
            rca = (r6b[1] << 8) | r6b[2]
            self._r1(7, rca << 16, "CMD7/R1 (RCA=%04X)" % rca)

        candidates = []
        if candidate in ("auto", "samsung1"):
            candidates.append(("samsung1", self._vendor_candidate_1))
        if candidate in ("auto", "samsung2"):
            candidates.append(("samsung2", self._vendor_candidate_2))
        if candidate == "direct":
            candidates.append(("direct", lambda: None))

        failures = []
        for name, unlock in candidates:
            print("TRY %s: vendor unlock -> CMD26" % name)
            try:
                # Re-enter a clean selected state between destructive
                # candidates.  If the controller latched the previous
                # candidate, CMD0/init is the safest recovery we have.
                if name != candidates[0][0]:
                    self._reset_to_identification()
                    r6b = self._r1(3, 0, "CMD3/R6 retry")
                    if r6b and len(r6b) >= 3:
                        rca = (r6b[1] << 8) | r6b[2]
                        self._r1(7, rca << 16, "CMD7/R1 retry (RCA=%04X)" % rca)
                unlock()
                self._cmd26(cid, "%s CMD26" % name)
                self._vendor_exit()
                print("PROGRAM COMMAND ACCEPTED: %s" % name)
                return rca, old_cid
            except Exception as e:
                msg = "%s: %s" % (name, e)
                failures.append(msg)
                print("FAIL: %s" % msg)
                log(msg, True)
                self._vendor_exit()

        raise RuntimeError("ALL_CID_PROGRAM_METHODS_FAILED: " + " | ".join(failures))


def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    args = [a for a in args if a != "--debug"]

    if args and args[0] == "readtest":
        # init YOK — sadece hat testi
        bus = Bus(debug=debug)
        try:
            print("10 sn: CMD/DAT0 izleniyor. MOSI pinine GND'ye dokun -> CMD=0 gormeliyiz.")
            for i in range(50):
                c = bus.get_cmd()
                d = bus.get_d0()
                print("CMD=%d DAT0=%d" % (c, d))
                time.sleep(0.2)
        finally:
            bus.close()
        return

    sd = SDCard(debug=debug)
    try:
        print("[!] DAT3 yukukte. Karti SIMDI cikarip tekrar tak (5 sn):")
        for i in range(5, 0, -1):
            print("    %d..." % i)
            time.sleep(1)
        sd.init()
        print("init OK")
        if not args or args[0] == "probe":
            raw = sd.read_cid()
            print("R2 (136 bit, ham): %s" % (raw.hex().upper() if raw else "YOK"))
        elif args and args[0] in ("program", "restore") and (len(args) > 1 or args[0] == "restore"):
            requested = SOURCE_CID if args[0] == "restore" and len(args) == 1 else bytes.fromhex(args[1])
            if len(requested) != 16:
                print("CID 16 bayt olmali")
                return
            target = requested
            if not sd._cid_crc_ok(target):
                fixed = sd._cid_with_valid_crc(target)
                print("[!] Hedef CID CRC7 gecersiz.")
                print("    verilen : %s" % target.hex().upper())
                print("    duzeltilmis: %s" % fixed.hex().upper())
                if "--fix-crc" not in args:
                    print("ABORT: --fix-crc olmadan yazma yapilmadi.")
                    return
                target = fixed
            candidate = "auto"
            for a in args[2:]:
                if a in ("auto", "samsung1", "samsung2", "direct"):
                    candidate = a
            print("TARGET CID: %s" % target.hex().upper())
            if args[0] == "restore":
                print("Restore explicit CID: kaynak CID'i arguman olarak verildi.")
            rca, old_cid = sd.program_cid(target, candidate=candidate)
            print("[!] Program sonrasi kart resetleniyor ve CID tekrar okunuyor...")
            sd._reset_to_identification()
            raw = sd.read_cid()
            print("READBACK CID: %s" % (raw.hex().upper() if raw else "YOK"))
            if raw != target:
                raise RuntimeError("READBACK_MISMATCH: expected=%s got=%s" %
                                   (target.hex().upper(), raw.hex().upper() if raw else "YOK"))
            print("CID VERIFY OK")
        elif args[0] == "readtest":
            # CMD ve DAT0 okuma yolu testi: 10 sn boyunca seviyeleri goster.
            # MOSI pinini bir jumper ile GND'ye dokunursan CMD=0 gormeliyiz.
            print("10 sn boyunca CMD/DAT0 okunuyor. MOSI hattina GND'ye dokun ve CMD=0 gorup gormedigimize bak.")
            for i in range(100):
                c = sd.bus.get_cmd()
                d = sd.bus.get_d0()
                if i % 10 == 0:
                    print("CMD=%d DAT0=%d" % (c, d))
                time.sleep(0.1)
        else:
            print("bilinmeyen komut")
    finally:
        sd.bus.close()


if __name__ == "__main__":
    main()
