"""sdtool: sdlib kullanan komut satiri arac (SSH/terminal icin)."""

import sys
import argparse
import sdlib


def hex_bytes(s):
    return bytes.fromhex(s.replace(" ", "").replace("0x", "").replace(",", ""))


def main():
    p = argparse.ArgumentParser(prog="sdtool", description="SD kart kontrol araci (Pi + SPI).")
    p.add_argument("--bus", type=int, default=0)
    p.add_argument("--dev", type=int, default=0)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="CID/CSD/SCR/RCA/OCR oku")
    sub.add_parser("status", help="kart durum (CMD13)")
    b = sub.add_parser("readblock", help="tek blok oku")
    b.add_argument("addr", type=int)
    b.add_argument("--file", help="blok icerigini dosyaya yaz")
    w = sub.add_parser("writeblock", help="tek blok yaz")
    w.add_argument("addr", type=int)
    w.add_argument("file", help="512 bayt iceren dosya")
    wc = sub.add_parser("writecid", help="CID yaz (CMD26)")
    wc.add_argument("hex")
    ws = sub.add_parser("writecsd", help="CSD yaz (CMD27)")
    ws.add_argument("hex")
    sp = sub.add_parser("setpass", help="sifre belirle (CMD42)")
    sp.add_argument("pwd")
    cp = sub.add_parser("clearpass", help="sifreyi temizle (CMD42)")
    cp.add_argument("pwd")
    ep = sub.add_parser("erasepass", help="sifreyi sil (kart zaten sifreliyken)")
    sw = sub.add_parser("setwp", help="yazma korumasini ac (CMD28)")
    sw.add_argument("addr", type=int)
    cw = sub.add_parser("clearwp", help="yazma korumasini kapat (CMD29)")
    cw.add_argument("addr", type=int)
    rw = sub.add_parser("readwp", help="yazma koruma durumunu oku (CMD30)")
    rw.add_argument("addr", type=int)
    er = sub.add_parser("erase", help="blok araligini sil (CMD32/33/38)")
    er.add_argument("start", type=int)
    er.add_argument("end", type=int)

    args = p.parse_args()

    sd = sdlib.SD(args.bus, args.dev)
    try:
        sd.init()
    except sdlib.SDError as e:
        print("INIT HATASI:", e)
        sd.close()
        sys.exit(1)

    try:
        if args.cmd == "info":
            try:
                cid = sd.cid()
                print("CID :", cid.hex().upper())
                d = sdlib.decode_cid(cid)
                print("  MID : 0x%02X" % d["mid"])
                print("  OID : %r" % d["oid"])
                print("  PNM : %r" % d["name"])
                print("  PRV : %s" % d["prv"])
                print("  PSN : %08X" % d["serial"])
                print("  MDT: %s (raw %s)" % (d["date_a"], d["date_raw"]))
            except sdlib.SDError as e:
                print("CID OKUNADI MI: %s" % e)
            try:
                csd = sd.csd()
                print("CSD :", csd.hex().upper())
                d = sdlib.decode_csd(csd)
                print("  type     : %s" % d["kind"])
                print("  capacity : %d bytes (%.1f MiB)" % (d["capacity"], d["capacity"] / 1048576))
                print("  read blk : %d" % (1 << d["read_bl_len"]))
            except sdlib.SDError as e:
                print("CSD OKUNADI MI: %s" % e)
            try:
                scr = sd.scr()
                print("SCR :", scr.hex().upper())
                d = sdlib.decode_scr(scr)
                print("  spec   : SD %s" % d["sd_spec"])
                print("  sec    : %s" % d["security"])
                print("  bus    : %s" % d["bus_widths"])
            except sdlib.SDError as e:
                print("SCR OKUNADI MI: %s" % e)
            rca = sd.rca()
            print("RCA : %s" % (("%04X" % rca) if rca is not None else "yok"))
            print("OCR : %s" % (sd.ocr.hex().upper() if sd.ocr else "yok"))
        elif args.cmd == "status":
            print("status r1 = %#x" % sd.status())
        elif args.cmd == "readblock":
            data = sd.read_block(args.addr)
            if args.file:
                with open(args.file, "wb") as f:
                    f.write(data)
                print("yazildi: %s (%d bayt)" % (args.file, len(data)))
            else:
                print(data.hex())
        elif args.cmd == "writeblock":
            with open(args.file, "rb") as f:
                data = f.read(512).ljust(512, b"\x00")
            sd.write_block(args.addr, data)
            print("blok yazildi: %d" % args.addr)
        elif args.cmd == "writecid":
            sd.write_cid(hex_bytes(args.hex))
            print("CID yazildi.")
        elif args.cmd == "writecsd":
            sd.write_csd(hex_bytes(args.hex))
            print("CSD yazildi.")
        elif args.cmd == "setpass":
            sd.set_password(args.pwd.encode())
            print("sifre belirlendi.")
        elif args.cmd == "clearpass":
            sd.clear_password(args.pwd.encode())
            print("sifre temizlendi.")
        elif args.cmd == "erasepass":
            sd.erase_password()
            print("sifre silindi.")
        elif args.cmd == "setwp":
            sd.set_write_protect(args.addr)
            print("yazma korumasi acildi: %d" % args.addr)
        elif args.cmd == "clearwp":
            sd.clear_write_protect(args.addr)
            print("yazma korumasi kapatildi: %d" % args.addr)
        elif args.cmd == "readwp":
            data = sd.read_write_protect(args.addr)
            print("wp durumu: %s" % data.hex().upper())
        elif args.cmd == "erase":
            sd.erase_blocks(args.start, args.end)
            print("silindi: %d..%d" % (args.start, args.end))
        else:
            print("bilinmeyen komut")
    except sdlib.SDError as e:
        print("HATA:", e)
        sys.exit(1)
    finally:
        sd.close()


if __name__ == "__main__":
    main()
