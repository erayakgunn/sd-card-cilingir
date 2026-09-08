"""sdbit: MH-SD modulu uzerinden 1-bit SD-bus bit-bang (libgpiod v2).

Kablo ayni: SCK=CLK, MOSI=CMD, MISO=DAT0, CS=DAT3 (yukukte), 3V3, GND.
Onemli: testten once karti cikar-tak (guç döngüsü) — mod, güç aninda secilir.

Kullanim:
  python3 sdbit.py probe --debug        # init + fabrika CID oku (CMD2)
  python3 sdbit.py program <hex16> [--fix-crc] [auto|samsung1|samsung2|direct]
                                           # vendor unlock -> CMD26 -> reset -> readback
  python3 sdbit.py restore [hex16] [--fix-crc] # kaynak CID'i geri yukle (varsayilan: SOURCE_CID)
  python3 sdbit.py cmd26probe --debug     # mevcut CID ile yalnizca dogrudan CMD26 sinamasi
  python3 sdbit.py cmd26writeprobe --debug # PSN+1 yaz/oku, sonra orijinali geri yukle
  python3 sdbit.py swissbitinfo --debug   # belgeli Swissbit CMD56 omur/firmware bilgisi
  python3 sdbit.py cmd26states --debug    # CMD26'yi guvenli erisilebilir durumlarda sinar
  python3 sdbit.py safe-runner [rapor.jsonl] [--debug]
                                           # salt-okunur durum/komut envanteri
"""

import sys
import time
import json

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
        # Host voltage window 2.7-3.6 V (OCR[23:15]=0xFF80) plus HCS.
        for hcs in (0x40FF8000, 0x00FF8000):
            for attempt in range(500):
                self.cmd(55, 0, total_bits=48)
                time.sleep(0.001)
                r3 = self.cmd(41, hcs, total_bits=48)
                r3b = self.bus.bits_to_bytes(r3) if r3 else None
                if self.debug and attempt < 2:
                    log("ACMD41(%#x) resp=%s" % (hcs, r3b.hex() if r3b else "yok"), True)
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
        raise RuntimeError("ACMD41 timeout")

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

    def _r1_status(self, raw):
        """Return the 32-bit status field from a native R1/R1b response."""
        if not raw or len(raw) < 6:
            return None
        return int.from_bytes(raw[1:5], "big")

    def _wait_r1b(self, label, timeout_s=1.0):
        """Clock DAT0 until an R1b command has completed.

        CMD7 is R1b in native SD mode.  Sending the following command before
        its DAT0 busy period has ended can make an otherwise healthy card look
        as though it gives no response to every vendor command.
        """
        b = self.bus
        deadline = time.time() + timeout_s
        busy_seen = False
        while time.time() < deadline:
            level = b.rx_bit_d0()
            if level == 0:
                busy_seen = True
            elif busy_seen:
                log("%s R1b busy bitti" % label, self.debug)
                return True
            elif not busy_seen:
                # A card is allowed to have no observable busy interval.
                return True
        raise RuntimeError("%s R1b DAT0 busy timeout" % label)

    def _select_transfer(self, label="", already_identified=False):
        """Enter TRAN state and verify it with CMD13; no vendor command here.

        CMD2 advances a card from READY to IDENT.  Do not send it again when
        the caller has just read CID and therefore already put the card in
        IDENT state.
        """
        if not already_identified:
            cid = self.read_cid()
            if cid is None:
                raise RuntimeError("CMD2_CID_READ_FAILED")
        r6 = self._r1(3, 0, "CMD3/R6 %s" % label)
        if not r6 or len(r6) < 6:
            raise RuntimeError("CMD3_NO_RESPONSE")
        rca = (r6[1] << 8) | r6[2]
        if rca == 0:
            raise RuntimeError("CMD3_INVALID_RCA")
        r1b = self._r1(7, rca << 16, "CMD7/R1b %s RCA=%04X" % (label, rca))
        if not r1b:
            raise RuntimeError("CMD7_NO_RESPONSE")
        self._wait_r1b("CMD7")
        status_raw = self._r1(13, rca << 16, "CMD13/R1 %s" % label)
        status = self._r1_status(status_raw)
        if status is None:
            raise RuntimeError("CMD13_NO_RESPONSE")
        state = (status >> 9) & 0x0F
        ready = bool(status & 0x00000100)
        log("CMD13 state=%d (%s), ready=%s, status=0x%08X" %
            (state, "TRAN" if state == 4 else "not-TRAN", ready, status), self.debug)
        if state != 4:
            raise RuntimeError("CARD_NOT_IN_TRAN_STATE (CMD13=0x%08X, state=%d)" %
                               (status, state))
        return rca

    def _read_data_block(self, length=512, timeout_s=1.0):
        """Read one native-SD DAT0 data block after an ADTC read command."""
        b = self.bus
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if b.rx_bit_d0() == 0:  # data start bit
                break
        else:
            raise RuntimeError("DATA_START_TIMEOUT")
        data_bits = [b.rx_bit_d0() for _ in range(length * 8)]
        crc_bits = [b.rx_bit_d0() for _ in range(16)]
        end = b.rx_bit_d0()
        if end != 1:
            raise RuntimeError("DATA_END_BIT_INVALID=%d" % end)
        data = b.bits_to_bytes(data_bits)
        got_crc = int("".join(str(v) for v in crc_bits), 2)
        want_crc = crc16_sd(data)
        if got_crc != want_crc:
            raise RuntimeError("DATA_CRC16_INVALID got=%04X expected=%04X" %
                               (got_crc, want_crc))
        return data

    def swissbit_info(self):
        """Read Swissbit's documented CMD56 lifetime-monitoring block."""
        self._select_transfer("swissbitinfo")
        raw = self._r1(56, 0x53420001, "CMD56 Swissbit lifetime")
        status = self._r1_status(raw)
        if status is None:
            raise RuntimeError("CMD56_NO_RESPONSE")
        if status & 0x00000004:
            raise RuntimeError("CMD56_ILLEGAL_COMMAND (R1_STATUS=%08X)" % status)
        data = self._read_data_block(512)
        return {
            "signature": data[:8],
            "cid": data[16:32],
            "firmware": data[32:48].split(b"\0", 1)[0].decode("ascii", "replace"),
            "rated_cycles": int.from_bytes(data[48:52], "big"),
            "max_cycles": int.from_bytes(data[52:56], "big"),
            "total_cycles": int.from_bytes(data[56:60], "big"),
            "average_cycles": int.from_bytes(data[60:64], "big"),
            "remaining_percent": data[80],
            "raw": data,
        }

    def cmd26_state_matrix(self):
        """Try CMD26 in safely reachable state-machine states.

        The CID payload is always the original value. DATA, RCV, and PRG are
        deliberately excluded: entering them requires a real read/write or
        register-program operation, so they are not safe diagnostic states.
        """
        original = self.read_cid()
        if original is None or not self._cid_crc_ok(original):
            raise RuntimeError("STATE_MATRIX_CID_READ_OR_CRC_FAILED")
        results = []

        def attempt(name, setup):
            self._reset_to_identification()  # returns to READY
            try:
                setup()
                self._cmd26(original, "CMD26 state=%s" % name)
                results.append((name, "ACCEPTED"))
            except Exception as e:
                results.append((name, "REJECTED: %s" % e))

        attempt("IDLE", lambda: (self.cmd(0, 0, crc=0x4A, expect=False),
                                  self.bus.clocks_idle(8)))
        attempt("READY", lambda: None)
        attempt("IDENT", lambda: self.read_cid())

        def standby():
            if self.read_cid() is None:
                raise RuntimeError("CMD2_NO_RESPONSE")
            if not self._r1(3, 0, "CMD3/R6 state=STBY"):
                raise RuntimeError("CMD3_NO_RESPONSE")
        attempt("STBY", standby)
        attempt("TRAN", lambda: self._select_transfer("state=TRAN"))

        self._reset_to_identification()
        final = self.read_cid()
        if final != original:
            raise RuntimeError("STATE_MATRIX_CID_CHANGED: before=%s after=%s" %
                               (original.hex().upper(), final.hex().upper() if final else "YOK"))
        return original, results

    def safe_runner(self):
        """Run a repeatable, non-mutating native-SD command inventory.

        Each case begins with CMD0/init. It intentionally excludes write,
        erase, lock, write-protect, CMD26, and undocumented vendor commands.
        The returned records contain no user-data blocks.
        """
        records = []

        def add_case(name, action):
            self._reset_to_identification()  # CMD0 + init -> READY
            started = time.time()
            try:
                detail = action()
                records.append({"case": name, "ok": True,
                                "elapsed_ms": round((time.time() - started) * 1000, 1),
                                "detail": detail})
            except Exception as e:
                records.append({"case": name, "ok": False,
                                "elapsed_ms": round((time.time() - started) * 1000, 1),
                                "error": str(e)})

        def ident_cid():
            cid = self.read_cid()
            if cid is None:
                raise RuntimeError("CMD2_NO_RESPONSE")
            return {"cid": cid.hex().upper(), "cid_crc_ok": self._cid_crc_ok(cid)}

        def standby_csd():
            if self.read_cid() is None:
                raise RuntimeError("CMD2_NO_RESPONSE")
            r6 = self._r1(3, 0, "CMD3/R6 runner")
            if not r6 or len(r6) < 6:
                raise RuntimeError("CMD3_NO_RESPONSE")
            rca = (r6[1] << 8) | r6[2]
            bits = self.cmd(9, rca << 16, total_bits=136)
            raw = self.bus.bits_to_bytes(bits) if bits else None
            if not raw or len(raw) < 17:
                raise RuntimeError("CMD9_NO_RESPONSE")
            return {"rca": "%04X" % rca, "csd": raw[1:17].hex().upper()}

        def transfer_status_cid():
            rca = self._select_transfer("runner")
            status_raw = self._r1(13, rca << 16, "CMD13/R1 runner")
            status = self._r1_status(status_raw)
            bits = self.cmd(10, rca << 16, total_bits=136)
            raw = self.bus.bits_to_bytes(bits) if bits else None
            if status is None or not raw or len(raw) < 17:
                raise RuntimeError("CMD13_OR_CMD10_NO_RESPONSE")
            return {"rca": "%04X" % rca, "status": "%08X" % status,
                    "cid": raw[1:17].hex().upper()}

        def transfer_scr():
            rca = self._select_transfer("runner")
            r55 = self._r1(55, rca << 16, "CMD55/R1 runner")
            r51 = self._r1(51, 0, "ACMD51/R1 runner")
            if not r55 or not r51:
                raise RuntimeError("CMD55_OR_ACMD51_NO_RESPONSE")
            return {"scr": self._read_data_block(8).hex().upper()}

        def swissbit_cmd56():
            self._select_transfer("runner")
            raw = self._r1(56, 0x53420001, "CMD56/R1 runner")
            status = self._r1_status(raw)
            if status is None or (status & 0x00000004):
                raise RuntimeError("CMD56_REJECTED")
            data = self._read_data_block(512)
            return {"status": "%08X" % status,
                    "signature": data[:8].hex().upper(),
                    "raw_prefix_32": data[:32].hex().upper()}

        add_case("IDENT/CMD2", ident_cid)
        add_case("STBY/CMD9", standby_csd)
        add_case("TRAN/CMD13+CMD10", transfer_status_cid)
        add_case("TRAN/ACMD51", transfer_scr)
        add_case("TRAN/CMD56-53420001", swissbit_cmd56)

        self._reset_to_identification()
        final_cid = self.read_cid()
        if final_cid is None:
            raise RuntimeError("RUNNER_FINAL_CID_READ_FAILED")
        return records, final_cid

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

        # Host must release DAT0 immediately after the CRC16.  The previous
        # implementation generated one extra clock while still driving DAT0=1;
        # that can consume/shift the first bit of the card's data-response
        # token in a 1-bit native SD implementation.
        b.d0_reconf(False)

        # Native SD data-response token is one byte: xxx0sss1.
        # sss=010 accepted, 101 CRC error, 110 write error.
        # Capture until a real token is found, then switch immediately to
        # DAT0-busy monitoring. Do NOT consume a fixed 32-bit window first.
        dr_bits = []
        token = None
        token_pos = None
        token_kind = None
        token_status = None

        for _ in range(32):
            dr_bits.append(b.rx_bit_d0())
            if len(dr_bits) >= 8:
                byte = 0
                for bit in dr_bits[-8:]:
                    byte = (byte << 1) | bit
                low5 = byte & 0x1F
                if low5 in (0x05, 0x0B, 0x0D):
                    token = byte
                    token_pos = len(dr_bits) - 8
                    token_status = (byte >> 1) & 0x07
                    token_kind = {
                        0x05: "accepted",
                        0x0B: "crc_error",
                        0x0D: "write_error",
                    }[low5]
                    break

        dr_raw = "".join(map(str, dr_bits))
        log("data-response raw bitleri: %s" % dr_raw, self.debug)
        log("data-response token: %s pos=%s kind=%s status=%s" %
            ((("0x%02X" % token) if token is not None else "YOK"),
             token_pos if token_pos is not None else "-",
             token_kind or "-",
             (("%03d" % token_status) if token_status is not None else "-")),
            self.debug)

        if token is None:
            raise RuntimeError("DATA_RESPONSE_INVALID raw=%s" % dr_raw)
        if token_kind != "accepted":
            raise RuntimeError("DATA_REJECTED token=0x%02X kind=%s raw=%s" %
                               (token, token_kind, dr_raw))

        # The token has now been consumed. Start busy timing immediately.
        # A CID program can legitimately take longer than the previous 3 s
        # diagnostic limit, so use 10 s here.
        t0 = time.time()
        busy_seen = False
        timeout_s = 10.0
        while time.time() - t0 < timeout_s:
            v = b.rx_bit_d0()
            if v == 0:
                busy_seen = True
            elif busy_seen:
                log("busy bitti elapsed=%.3fs" % (time.time() - t0), self.debug)
                return True, "accepted"

        raise RuntimeError("PROGRAM_TIMEOUT: DAT0 busy %.1fs+ (accepted=True)" % timeout_s)

    def _cmd26(self, cid, label="CMD26"):
        bits = self.cmd(26, 0, total_bits=48)
        r1 = self.bus.bits_to_bytes(bits) if bits else None
        log("%s resp: %s" % (label, r1.hex() if r1 else "yok"), self.debug)
        if not r1:
            raise RuntimeError("CMD26_NO_RESPONSE")
        # R1 is 48 bits: response header + 32-bit card status + CRC7/end.
        # The previous code incorrectly treated the final CRC byte as the
        # status byte. For example, raw=1a0000090085 means status=0x00000900
        # and CRC/end=0x85; it is NOT R1=0x85 and therefore does not indicate
        # ILLEGAL_COMMAND.
        if len(r1) < 6:
            raise RuntimeError("CMD26_BAD_R1_LEN=%d" % len(r1))
        status = int.from_bytes(r1[1:5], "big")
        log("%s R1 status=0x%08x" % (label, status), self.debug)
        if status & 0x00000004:
            raise RuntimeError("CMD26_ILLEGAL_COMMAND (R1_STATUS=%08X)" % status)
        state = (status >> 9) & 0x0F
        if state != 4:
            raise RuntimeError("CMD26_NOT_IN_TRAN_STATE (R1_STATUS=%08X, state=%d)" %
                               (status, state))
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

    def vendor_probe(self):
        """Vendor komutlarini sadece gozlemler; CMD26 gondermez."""
        # Vendor komutlari bazi kartlarda ancak secilmis/transfer state'te
        # cevap verir.  CMD7'nin R1b/DAT0 fazini tamamlayip CMD13 ile TRAN
        # durumunu dogrulamadan prob yapmayalim.
        try:
            self._select_transfer("vendorprobe")
            print("TRANSFER_STATE: OK (CMD13 ile dogrulandi)")
        except Exception as e:
            print("TRANSFER_STATE: FAILED: %s" % e)
            print("vendorprobe durduruldu; vendor komutu/CMD26 gonderilmedi.")
            return
        candidates = [
            (62, 0xEFAC62EC, "CMD62 enter Samsung/vendor"),
            (62, 0x0000EF50, "CMD62 unlock"),
            (62, 0x00CCED82, "CMD62 unlock-alt"),
            (60, 0, "CMD60 arg0"),
            (61, 0, "CMD61 arg0"),
            (63, 0, "CMD63 arg0"),
        ]
        for cmd, arg, label in candidates:
            try:
                raw = self._r1(cmd, arg, label)
                print("%s: %s" % (label, raw.hex().upper() if raw else "NO_RESPONSE"))
            except Exception as e:
                print("%s: ERROR %s" % (label, e))
        print("vendorprobe tamamlandi; CMD26 gonderilmedi.")

    def _reset_to_identification(self):
        """Reset the card and run initialization again for a real readback."""
        self.bus.d0_reconf(True)
        self.bus.clocks_idle(16)
        self.cmd(0, 0, crc=0x4A, expect=False)
        self.bus.clocks_idle(80)
        self.init()

    def program_cid(self, cid, candidate="auto", original_cid=None):
        cid = bytes(cid)
        if len(cid) != 16:
            raise ValueError("CID 16 bayt olmali")

        # A caller that has just issued CMD2 can pass that result here.  A
        # second CMD2 in IDENT state is invalid and produces no response.
        old_cid = bytes(original_cid) if original_cid is not None else self.read_cid()
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
        # old_cid was obtained with CMD2 immediately above, so the card is
        # already in IDENT state and the next valid command is CMD3.
        rca = self._select_transfer("program", already_identified=True)

        candidates = []
        if candidate in ("auto", "samsung1"):
            candidates.append(("samsung1", self._vendor_candidate_1))
        if candidate in ("auto", "samsung2"):
            candidates.append(("samsung2", self._vendor_candidate_2))
        if candidate in ("auto", "direct"):
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
                    # After CMD0/ACMD41 we are back in identification state.
                    # CMD3 is only valid after CMD2 has supplied the CID.
                    rca = self._select_transfer("retry")
                unlock()
                self._cmd26(cid, "%s CMD26" % name)
                # "direct" yolu kasitli olarak hicbir vendor komutu
                # gondermez; bu sayede CMD26'nin kendisini ayri sinayabiliriz.
                if name != "direct":
                    self._vendor_exit()
                print("PROGRAM COMMAND ACCEPTED: %s" % name)
                return rca, old_cid
            except Exception as e:
                msg = "%s: %s" % (name, e)
                failures.append(msg)
                print("FAIL: %s" % msg)
                log(msg, True)
                if name != "direct":
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
        elif args[0] == "vendorprobe":
            sd.vendor_probe()
        elif args[0] == "swissbitinfo":
            info = sd.swissbit_info()
            print("SWISSBIT CMD56: OK")
            print("  Signature        : %s" % info["signature"].hex().upper())
            if info["signature"] != b"Swissbit":
                print("  Note             : Bu CID/model CMD56 telemetri formatiyla eslesmiyor")
                print("  Raw[0:64]        : %s" % info["raw"][:64].hex().upper())
            print("  CID              : %s" % info["cid"].hex().upper())
            print("  Firmware         : %s" % info["firmware"])
            print("  Rated cycles     : %d" % info["rated_cycles"])
            print("  Max cycles       : %d" % info["max_cycles"])
            print("  Total cycles     : %d" % info["total_cycles"])
            print("  Average cycles   : %d" % info["average_cycles"])
            print("  Remaining life   : %d%%" % info["remaining_percent"])
        elif args[0] == "safe-runner":
            report_path = args[1] if len(args) > 1 else "sdbit-safe-runner.jsonl"
            records, final_cid = sd.safe_runner()
            with open(report_path, "w", encoding="utf-8", newline="\n") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            print("SAFE RUNNER tamamlandi: %d vaka" % len(records))
            for record in records:
                print("  %-24s %s" % (record["case"], "OK" if record["ok"] else "FAIL"))
            print("Final CID: %s" % final_cid.hex().upper())
            print("Rapor: %s" % report_path)
        elif args[0] == "cmd26states":
            original, results = sd.cmd26_state_matrix()
            print("CMD26 STATE MATRIX (payload her zaman mevcut CID): %s" % original.hex().upper())
            for name, result in results:
                print("  %-5s %s" % (name, result))
            print("DATA/RCV/PRG guvenlik nedeniyle denenmedi; final CID dogrulandi.")
        elif args[0] == "cmd26probe":
            # This is the least invasive CMD26 experiment: the payload is
            # exactly the CID read from this card moments earlier.  It does
            # not use CMD62 or any claimed vendor unlock sequence.
            before = sd.read_cid()
            if before is None:
                raise RuntimeError("PROBE_CID_READ_FAILED")
            if not sd._cid_crc_ok(before):
                raise RuntimeError("PROBE_CID_CRC_INVALID")
            print("CMD26 PROBE CID (degistirilmeyecek): %s" % before.hex().upper())
            sd.program_cid(before, candidate="direct", original_cid=before)
            print("[!] CMD26 probe sonrasi kart resetleniyor ve CID tekrar okunuyor...")
            sd._reset_to_identification()
            after = sd.read_cid()
            print("READBACK CID: %s" % (after.hex().upper() if after else "YOK"))
            if after != before:
                raise RuntimeError("CMD26_PROBE_READBACK_MISMATCH: before=%s after=%s" %
                                   (before.hex().upper(), after.hex().upper() if after else "YOK"))
            print("CMD26 PROBE VERIFY OK (CID degismedi)")
        elif args[0] == "cmd26writeprobe":
            # A same-value write only proves that the command/data path was
            # accepted.  Change one reversible field (PSN), verify it after a
            # reset, then restore the exact original CID and verify again.
            before = sd.read_cid()
            if before is None:
                raise RuntimeError("WRITE_PROBE_CID_READ_FAILED")
            if not sd._cid_crc_ok(before):
                raise RuntimeError("WRITE_PROBE_CID_CRC_INVALID")
            changed = bytearray(before)
            psn = (int.from_bytes(changed[9:13], "big") + 1) & 0xFFFFFFFF
            changed[9:13] = psn.to_bytes(4, "big")
            changed = sd._cid_with_valid_crc(changed)
            print("WRITE PROBE ORIGINAL: %s" % before.hex().upper())
            print("WRITE PROBE TARGET  : %s (yalnizca PSN +1)" % changed.hex().upper())
            sd.program_cid(changed, candidate="direct", original_cid=before)
            print("[!] Degisiklik sonrasi reset/readback...")
            sd._reset_to_identification()
            after = sd.read_cid()
            print("WRITE PROBE READBACK: %s" % (after.hex().upper() if after else "YOK"))
            if after != changed:
                if after == before:
                    print("CMD26 WRITE PROBE: kabul edildi ama CID commit etmedi.")
                    return
                raise RuntimeError("WRITE_PROBE_UNEXPECTED_CID: before=%s after=%s" %
                                   (before.hex().upper(), after.hex().upper() if after else "YOK"))
            print("CMD26 WRITE PROBE: CID commit etti; orijinal CID geri yukleniyor...")
            # after was read by CMD2, therefore the card is already IDENT.
            sd.program_cid(before, candidate="direct", original_cid=after)
            sd._reset_to_identification()
            restored = sd.read_cid()
            print("RESTORE READBACK: %s" % (restored.hex().upper() if restored else "YOK"))
            if restored != before:
                raise RuntimeError("WRITE_PROBE_RESTORE_FAILED: expected=%s got=%s" %
                                   (before.hex().upper(), restored.hex().upper() if restored else "YOK"))
            print("CMD26 WRITE PROBE VERIFY OK (orijinal CID geri yuklendi)")
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
