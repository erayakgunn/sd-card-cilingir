"""sdbit: MH-SD modulu uzerinden 1-bit SD-bus bit-bang (CID okuma/programlama).

Kablo ayni: SCK=CLK, MOSI=CMD, MISO=DAT0, CS=DAT3 (yukukte), 3V3, GND.
Kullanim:
  python3 sdbit.py probe           # init + fabrika CID oku (CMD2)
  python3 sdbit.py program <hex16> # CMD26 ile CID yaz + geri oku
"""

import sys
import time

import gpiod

CLK, CMD, DAT0, DAT3 = 11, 10, 9, 8  # BCM numaralari (Pi header)

BIT_NS = 3000  # bit periyodu ~3us (~300kHz hedef, Python ile daha yavas olur)


def find_chip():
    for p in ("/dev/gpiochip4", "/dev/gpiochip0"):
        try:
            c = gpiod.Chip(p)
            c.close()
            return p
        except Exception:
            continue
    raise RuntimeError("gpiochip bulunamadi")


class Bus:
    def __init__(self, chip_path=None, debug=False):
        self.debug = debug
        self.chip = gpiod.Chip(chip_path or find_chip())
        self.l_clk = self.chip.get_line(CLK)
        self.l_clk.request(consumer="sdbus-clk", type=gpiod.LINE_REQ_DIR_OUT,
                           default_vals=[0])
        self.l_cmd = self.chip.get_line(CMD)
        self.l_cmd.request(consumer="sdbus-cmd", type=gpiod.LINE_REQ_DIR_OUT,
                           default_vals=[1])
        self.l_d0 = self.chip.get_line(DAT0)
        self.l_d0.request(consumer="sdbus-d0", type=gpiod.LINE_REQ_DIR_OUT,
                          default_vals=[1])
        self.l_d3 = self.chip.get_line(DAT3)
        self.l_d3.request(consumer="sdbus-d3", type=gpiod.LINE_REQ_DIR_OUT,
                          default_vals=[1])
        self.tx_cmd = True
        self.tx_d0 = True

    def close(self):
        self.chip.close()

    # --- alt seviye ---

    def _spin(self, ns):
        end = time.perf_counter_ns() + ns
        while time.perf_counter_ns() < end:
            pass

    def _clk_out(self, cmd_bit, d0_bit):
        """Bir bit: CLK 0 -> hatlari kur -> CLK 1 -> bekle."""
        self.l_clk.set_value(0)
        self.l_cmd.set_value(cmd_bit)
        self.l_d0.set_value(d0_bit)
        self._spin(BIT_NS // 2)
        self.l_clk.set_value(1)
        self._spin(BIT_NS // 2)

    def _clk_in(self):
        """Bir bit: hatlar released (input-pullup varsayalim), ornek al."""
        self.l_clk.set_value(0)
        self._spin(BIT_NS // 3)
        self.l_clk.set_value(1)
        v = self.l_cmd.get_value()
        self._spin(BIT_NS // 3)
        return int(v)

    def cmd_tx(self, mode=True):
        self.tx_cmd = mode

    def clocks(self, n, sample=False):
        out = []
        for _ in range(n):
            if sample:
                self.l_clk.set_value(0)
                self._spin(BIT_NS // 3)
                self.l_clk.set_value(1)
                out.append(int(self.l_cmd.get_value()))
                self._spin(BIT_NS // 3)
            else:
                self._clk_out(1, 1)
        return out

    # --- komut katmani ---

    def send_command(self, cmd, arg, crc, expect_resp=True):
        """48-bit komut gonder. Donus: response bitleri (list) veya None."""
        frame = [0, 1]  # start, yon(host=1)
        v = cmd & 0x3F
        for i in range(5, -1, -1):
            frame.append((v >> i) & 1)
        a = arg & 0xFFFFFFFF
        for i in range(31, -1, -1):
            frame.append((a >> i) & 1)
        for i in range(6, -1, -1):
            frame.append((crc >> i) & 1)
        frame.append(1)  # end
        for b in frame:
            self._clk_out(b, 1)
        if not expect_resp:
            return None
        # Ncr: en fazla 8 bayt icinde start biti (0) bekle
        bits = []
        got = None
        for _ in range(64):
            b = self._clk_in()
            if b == 0:
                got = True
                break
            bits.append(b)
        if not got:
            return None
        resp = [0] + bits[-7:] if len(bits) >= 7 else [0] + [1] * (7 - len(bits)) + bits
        # start bitinden sonraki 7 biti zaten topladik; devaminda kalan bitler
        remaining = 48 - len(resp) if False else None
        # toplam 48-bit yanit icin kalan bitleri oku (R1: 8, R3: 40, R7: 40, R2: 136)
        return resp

    def read_bits(self, n):
        bits = []
        for _ in range(n):
            bits.append(self._clk_in())
        return bits

    def bits_to_bytes(self, bits):
        out = bytearray()
        for i in range(0, len(bits) - 7, 8):
            v = 0
            for b in bits[i:i + 8]:
                v = (v << 1) | b
            out.append(v)
        return bytes(out)

    def write_data_1bit(self, data, crc16v):
        """0xFE + veri + CRC16 (MSB first) — DAT0 uzerinden host surer."""
        bits = []
        tok = 0xFE
        for i in range(7, -1, -1):
            bits.append((tok >> i) & 1)
        for byte in data:
            for i in range(7, -1, -1):
                bits.append((byte >> i) & 1)
        for i in range(15, -1, -1):
            bits.append((crc16v >> i) & 1)
        for b in bits:
            self._clk_out(1, b)
        # 2 bit neck (end bit: DAT0 high)
        self._clk_out(1, 1)
        self._clk_out(1, 1)

    def wait_not_busy(self, timeout=1.0):
        self.l_d0.set_direction(gpiod.LINE_REQ_DIR_IN)
        t0 = time.time()
        ok = False
        while time.time() - t0 < timeout:
            self.l_clk.set_value(0)
            self._spin(BIT_NS // 3)
            self.l_clk.set_value(1)
            v = int(self.l_d0.get_value())
            self._spin(BIT_NS // 3)
            if v == 1:
                ok = True
                break
        self.tx_d0 = True
        return ok

    def set_dir_in(self, line):
        line.set_direction(gpiod.LINE_REQ_DIR_IN)

    def set_dir_out(self, line, val=1):
        line.set_direction(gpiod.LINE_REQ_DIR_OUT)
        line.set_value(val)


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
        self.bus = Bus(debug=debug)
        self.debug = debug
        self.mask = 0x09  # x7+x3+1; hata olursa 0x0D denenir

    def cmd(self, cmd, arg=0, crc=None, expect=True, nresp_bits=0):
        if crc is None:
            crc = crc7_sd([0x40 | (cmd & 0x3F),
                           (arg >> 24) & 0xFF, (arg >> 16) & 0xFF,
                           (arg >> 8) & 0xFF, arg & 0xFF], self.mask)
        self.bus.tx_cmd = True
        self.bus.set_dir_out(self.bus.l_cmd, 1)
        r = self.bus.send_command(cmd, arg, crc, expect)
        if not expect:
            return None
        if nresp_bits == 0:
            # R1: start+7 bit zaten send_command icinde
            b = self.bus.bits_to_bytes(r + self.bus.read_bits(0)) if r else None
            return b[0] if b else None
        bits = r + self.bus.read_bits(nresp_bits - len(r))
        return self.bus.bits_to_bytes(bits)

    def init(self):
        self.bus.l_d3.set_value(1)
        self.bus.clocks(80)
        # CMD0 (yanit yok)
        self.cmd(0, 0, crc=0x4A, expect=False)
        self.bus.clocks(8)
        # CMD8
        r7 = self.cmd(8, 0x1AA)
        if self.debug:
            print("[SD] CMD8 resp: %s" % (r7.hex() if r7 else None), file=sys.stderr)
        # ACMD41
        for _ in range(500):
            self.cmd(55, 0)
            r3 = self.cmd(41, 0x40000000, nresp_bits=40)
            if r3 and len(r3) >= 5 and (r3[1] & 0x80):
                return True
            time.sleep(0.005)
        raise RuntimeError("ACMD41 timeout")

    def read_cid(self):
        # CMD2: R2 = 136 bit (start+dir+127+stop)
        self.bus.set_dir_out(self.bus.l_cmd, 1)
        crc = crc7_sd([0x42, 0, 0, 0, 0], self.mask)
        self.bus.send_command(2, 0, crc, expect=True)
        bits = self.bus.read_bits(140)
        if self.debug:
            print("[SD] R2 bits: %s" % self.bus.bits_to_bytes(bits).hex(), file=sys.stderr)
        return bits

    def program_cid(self, cid):
        crc = crc7_sd([0x5A, 0, 0, 0, 0], self.mask)
        self.bus.set_dir_out(self.bus.l_cmd, 1)
        r = self.bus.send_command(26, 0, crc, expect=True)
        r1b = self.bus.bits_to_bytes(r + self.bus.read_bits(40 - len(r))) if r else None
        if self.debug:
            print("[SD] CMD26 resp: %s" % (r1b.hex() if r1b else None), file=sys.stderr)
        c16 = crc16_sd(cid)
        self.bus.write_data_1bit(cid, c16)
        # busy bekle (DAT0)
        self.bus.set_dir_in(self.bus.l_d0)
        ok = self.bus.wait_not_busy(2.0)
        self.bus.set_dir_out(self.bus.l_d0, 1)
        return ok


def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    args = [a for a in args if a != "--debug"]
    sd = SDCard(debug=debug)
    try:
        sd.init()
        print("init OK")
        if not args or args[0] == "probe":
            bits = sd.read_cid()
            print("R2 ham bitler (hex): %s" % sd.bus.bits_to_bytes(bits).hex().upper())
        elif args[0] == "program" and len(args) > 1:
            target = bytes.fromhex(args[1])
            if len(target) != 16:
                print("CID 16 bayt olmali"); return
            ok = sd.program_cid(target)
            print("program: busy=%s" % ok)
            bits = sd.read_cid()
            print("readback (hex): %s" % sd.bus.bits_to_bytes(bits).hex().upper())
        else:
            print("bilinmeyen komut")
    finally:
        sd.bus.close()


if __name__ == "__main__":
    main()
