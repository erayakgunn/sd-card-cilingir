"""sdbit: MH-SD modulu uzerinden 1-bit SD-bus bit-bang (libgpiod v2).

Kablo ayni: SCK=CLK, MOSI=CMD, MISO=DAT0, CS=DAT3 (yukukte), 3V3, GND.
Onemli: testten once karti cikar-tak (guç döngüsü) — mod, güç aninda secilir.

Kullanim:
  python3 sdbit.py probe --debug        # init + fabrika CID oku (CMD2)
  python3 sdbit.py program <hex16>      # CMD26 ile CID yaz + geri oku
"""

import sys
import time

import gpiod
from gpiod.line import Bias, Direction, Value

CLK, CMD, DAT0, DAT3 = 11, 10, 9, 8  # BCM (Pi header)

BIT_NS = 3000  # ~300 kHz hedef (sys overhead ile ~50-100 kHz)


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
        if self.cmd_out:
            self.req.set_value(CMD, Value.ACTIVE if cmd_v else Value.INACTIVE)
        if d0_v is not None and self.d0_out:
            self.req.set_value(DAT0, Value.ACTIVE if d0_v else Value.INACTIVE)
        self._spin(BIT_NS // 3)
        self.clk_hi()
        self._spin(2 * BIT_NS // 3)

    def rx_bit(self):
        """CLK low'da ornekle (kart falling edge'de veriyi kurar)."""
        self.clk_lo()
        self._spin(BIT_NS // 2)
        v = self.get_cmd()
        self.clk_hi()
        self._spin(BIT_NS // 2)
        return v

    def rx_bit_d0(self):
        self.clk_lo()
        self._spin(BIT_NS // 2)
        v = self.get_d0()
        self.clk_hi()
        self._spin(BIT_NS // 2)
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
            log("yanit yok (64 bit: %s)" % "".join(map(str, skipped)), self.debug)
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


def crc7_sd(data, mask):
    crc = 0
    for byte in data:
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            msb = crc >> 6
            crc = ((crc << 1) & 0x7F) | bit
            if msb:
                crc ^= mask
    return crc


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
        if not r7b and self.mask == 0x09:
            log("yanit yok -> CRC7 polinom 0x0D ile tekrar", self.debug)
            self.mask = 0x0D
            return self.init()
        for _ in range(500):
            self.cmd(55, 0, total_bits=48)
            r3 = self.cmd(41, 0x40000000, total_bits=48)
            r3b = self.bus.bits_to_bytes(r3) if r3 else None
            if r3b and len(r3b) >= 5 and (r3b[1] & 0x80):
                log("ACMD41 ready, OCR=%s" % r3b[1:5].hex(), self.debug)
                return True
            time.sleep(0.005)
        raise RuntimeError("ACMD41 timeout")

    def read_cid(self):
        # CMD2: R2 = 136 bit
        bits = self.cmd(2, 0, total_bits=136)
        if not bits:
            return None
        return self.bus.bits_to_bytes(bits)

    def program_cid(self, cid):
        b = self.bus
        bits = self.cmd(26, 0, total_bits=48)
        r1 = self.bus.bits_to_bytes(bits) if bits else None
        log("CMD26 resp: %s" % (r1.hex() if r1 else "yok"), self.debug)
        # veri: DAT0 uzerinden host surer
        b.d0_reconf(True)
        c16 = crc16_sd(cid)
        tok_bits = []
        tok = 0xFE
        for i in range(7, -1, -1):
            tok_bits.append((tok >> i) & 1)
        for byte in list(cid) + [(c16 >> 8) & 0xFF, c16 & 0xFF]:
            for i in range(7, -1, -1):
                tok_bits.append((byte >> i) & 1)
        for v in tok_bits:
            b.tx_bit(1, v)
        b.tx_bit(1, 1)  # end bit
        b.d0_reconf(False)
        # data response + busy: DAT0'u ornekleyerek clockla
        dr_bits = []
        for _ in range(16):
            dr_bits.append(b.rx_bit_d0())
        log("data-response bitleri: %s" % "".join(map(str, dr_bits)), self.debug)
        # busy: DAT0 low -> high bekle
        t0 = time.time()
        busy_seen = False
        while time.time() - t0 < 2.0:
            v = b.rx_bit_d0()
            if v == 0:
                busy_seen = True
            elif busy_seen:
                break
        log("busy bitti (busy_seen=%s)" % busy_seen, self.debug)
        return True


def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    args = [a for a in args if a != "--debug"]
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
        elif args[0] == "program" and len(args) > 1:
            target = bytes.fromhex(args[1])
            if len(target) != 16:
                print("CID 16 bayt olmali")
                return
            sd.program_cid(target)
            raw = sd.read_cid()
            print("readback (ham): %s" % (raw.hex().upper() if raw else "YOK"))
        else:
            print("bilinmeyen komut")
    finally:
        sd.bus.close()


if __name__ == "__main__":
    main()
