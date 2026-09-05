"""sdtool: sdlib kullanan komut satiri arac (SSH/terminal icin)."""

import sys
import argparse
import sdlib


def hex_bytes(s):
    return bytes.fromhex(s.replace(" ", "").replace("0x", "").replace(",", ""))


PART_TYPES = {
    0x01: "FAT12",
    0x04: "FAT16 (<32MB)",
    0x06: "FAT16B",
    0x07: "NTFS/exFAT",
    0x0B: "FAT32",
    0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)",
}


def hexdump(data, base=0):
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hs = " ".join("%02X" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("%08X  %-47s  |%s|" % (base + off, hs, asc))


def find_fat_part(mbr):
    for i in range(4):
        e = mbr[446 + i * 16:446 + i * 16 + 16]
        if e[4] in PART_TYPES:
            return int.from_bytes(e[8:12], "little"), e[4]
    return 0, None


def cmd_mbr(sd):
    data = sd.read_block(0)
    print("MBR imzasi: %02X %02X %s" % (data[510], data[511],
          "(OK)" if (data[510], data[511]) == (0x55, 0xAA) else "(GECERSIZ!)"))
    found = False
    for i in range(4):
        e = data[446 + i * 16:446 + i * 16 + 16]
        if e[4] == 0:
            continue
        found = True
        lba = int.from_bytes(e[8:12], "little")
        cnt = int.from_bytes(e[12:16], "little")
        name = PART_TYPES.get(e[4], "bilinmeyen")
        print("Bolum %d: boot=%#02x  tip=%#02x (%s)  bas_LBA=%d  sektor=%d  boyut=%.1f MiB"
              % (i + 1, e[0], e[4], name, lba, cnt, cnt * 512 / 1048576))
    if not found:
        print("Bolum yok (superfloppy — tum kart tek FAT olabilir).")


def cmd_fatinfo(sd):
    mbr = sd.read_block(0)
    part_start, typ = find_fat_part(mbr)
    print("Bolum baslangic LBA: %d (tip %s)" % (part_start, PART_TYPES.get(typ, "?")))
    bs = sd.read_block(part_start)
    bps = int.from_bytes(bs[0x0B:0x0D], "little")
    spc = bs[0x0D]
    reserved = int.from_bytes(bs[0x0E:0x10], "little")
    nfats = bs[0x10]
    rootent = int.from_bytes(bs[0x11:0x13], "little")
    tot16 = int.from_bytes(bs[0x13:0x15], "little")
    fatsz16 = int.from_bytes(bs[0x16:0x18], "little")
    tot32 = int.from_bytes(bs[0x20:0x24], "little")
    fatsz32 = int.from_bytes(bs[0x24:0x28], "little")
    label = bs[0x2B:0x36].decode("ascii", "replace").strip()
    fstype = bs[0x36:0x42].decode("ascii", "replace").strip("\x00 ")
    print("OEM            : %r" % bs[3:11].decode("ascii", "replace"))
    print("bytes/sector   : %d" % bps)
    print("sectors/cluster: %d" % spc)
    print("reserved       : %d" % reserved)
    print("FAT sayisi     : %d" % nfats)
    print("root entries   : %d" % rootent)
    print("FAT size (sec) : %d" % (fatsz16 or fatsz32))
    print("total sectors  : %d" % (tot16 or tot32))
    print("volume label   : %r" % label)
    print("FS tipi        : %r" % fstype)
    print("imza           : %02X %02X %s" % (bs[510], bs[511],
          "(OK)" if (bs[510], bs[511]) == (0x55, 0xAA) else "(GECERSIZ!)"))


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
    sub.add_parser("mbr", help="bolumleme tablosunu coz")
    sub.add_parser("fatinfo", help="FAT boot sector bilgisi")
    hd = sub.add_parser("hexdump", help="bloklari okunabilir goster")
    hd.add_argument("addr", type=int)
    hd.add_argument("--count", type=int, default=1)

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
        elif args.cmd == "mbr":
            cmd_mbr(sd)
        elif args.cmd == "fatinfo":
            cmd_fatinfo(sd)
        elif args.cmd == "hexdump":
            for i in range(args.count):
                hexdump(sd.read_block(args.addr + i), base=(args.addr + i) * 512)
        else:
            print("bilinmeyen komut")
    except sdlib.SDError as e:
        print("HATA:", e)
        sys.exit(1)
    finally:
        sd.close()


if __name__ == "__main__":
    main()
