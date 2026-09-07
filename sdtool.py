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
    if fatsz16:
        fstype = bs[0x36:0x3E].decode("ascii", "replace").strip("\x00 ")
    else:
        fstype = bs[0x52:0x5A].decode("ascii", "replace").strip("\x00 ")
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
    p.add_argument("--freq", type=int, default=400_000,
                   help="SPI hizi (Hz), init sonrasi uygulanir (orn. 4000000)")
    p.add_argument("--debug", action="store_true", help="ham SPI diagnostik cikti")
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
    wc.add_argument("--idle", action="store_true",
                    help="idle durumunda yaz (baz kartlar sadece bu durumda kabul eder)")
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
    sub.add_parser("ls", help="kart dosyalarini listele (SPI uzerinden)")
    ex = sub.add_parser("extract", help="kart dosyalarini klasore cikar (SPI uzerinden)")
    ex.add_argument("outdir")
    spd = sub.add_parser("speed", help="okuma hizi testi")
    spd.add_argument("--start", type=int, default=235)
    spd.add_argument("--count", type=int, default=256)
    im = sub.add_parser("image", help="tum karti .img dosyasina aktar (SPI)")
    im.add_argument("outfile")
    im.add_argument("--start", type=int, default=0)
    im.add_argument("--count", type=int, default=0, help="0 = kapasite sonuna kadar")
    sub.add_parser("probe", help="CID/CSD yazilabilirlik testi (CMD26/CMD27, guvenli: kendi degerini geri yazar)")
    ex2 = sub.add_parser("export", help="kart bilgilerini JSON olarak kaydet")
    ex2.add_argument("outfile")
    wi = sub.add_parser("writeimage", help="img dosyasini karta yaz (SPI, yavas)")
    wi.add_argument("imgfile")
    wi.add_argument("--start", type=int, default=0)
    wi.add_argument("--verify", action="store_true", help="yazilan bloklari geri okuyup karsilastir")
    sub.add_parser("csdtest", help="CSD yazma commit testi (TMP_WRITE_PROTECT bitini yaz/geri al)")
    ch = sub.add_parser("cidhunt", help="CID commit yontem arayisi (CRC-on + komut varyant matrisi)")
    ch.add_argument("hex")
    vb = sub.add_parser("vendorcid", help="Samsung/Evo CMD62 backdoor + CMD26 denemesi")
    vb.add_argument("hex")

    args = p.parse_args()

    sdlib.DEBUG = args.debug
    sd = sdlib.SD(args.bus, args.dev, args.freq)
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
            data = hex_bytes(args.hex)
            if len(data) != 16:
                print("HATA: CID 16 bayt olmali.")
                sys.exit(1)
            if args.idle:
                ok, variant = sd.program_cid_idle(data, args.hex.upper())
                if not ok:
                    print("HATA: hicbir varyant commit etmedi (kart CID yazmayi geri çevirdi).")
                    sys.exit(1)
                print("CID commit edildi (varyant: %s)." % variant)
            else:
                sd.write_cid(data)
            print("Mevcut CID: %s" % sd.cid().hex().upper())
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
        elif args.cmd == "ls":
            import fatdump
            be = sdlib.SDBackend(sd)
            cap = sdlib.decode_csd(sd.csd())["capacity"]
            r = fatdump.FatReader(backend=be, total_size=cap)
            for full, ent in r.walk():
                t = "DIR " if ent["isdir"] else "FILE"
                print("%s  %s  %9d  %s" % (t, full, ent["size"], ent["mtime"]))
        elif args.cmd == "extract":
            import os
            import fatdump
            be = sdlib.SDBackend(sd)
            cap = sdlib.decode_csd(sd.csd())["capacity"]
            r = fatdump.FatReader(backend=be, total_size=cap)
            entries = r.walk()
            total = 0
            for full, ent in entries:
                if ent["isdir"]:
                    os.makedirs(os.path.join(args.outdir, full), exist_ok=True)
                else:
                    dest = os.path.join(args.outdir, full)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    data = r.read_file(ent["cluster"], ent["size"])
                    with open(dest, "wb") as f:
                        f.write(data)
                    total += len(data)
            nfiles = sum(1 for _, e in entries if not e["isdir"])
            print("cikarildi: %d dosya, %d bayt -> %s" % (nfiles, total, args.outdir))
        elif args.cmd == "speed":
            import time as _t
            t0 = _t.time()
            for i in range(args.count):
                sd.read_block(args.start + i)
            dt = _t.time() - t0
            kb = args.count * 512 / 1024
            print("%d blok / %.2f sn = %.0f KB/s (%.2f MB/s)" % (args.count, dt, kb / dt, kb / 1024 / dt))
        elif args.cmd == "image":
            import time as _t
            import hashlib
            cap = sdlib.decode_csd(sd.csd())["capacity"]
            total_blocks = args.count if args.count else cap // 512
            md5 = hashlib.md5()
            sha = hashlib.sha256()
            t0 = _t.time()
            done = 0
            with open(args.outfile, "wb") as f:
                for i in range(args.start, args.start + total_blocks):
                    data = sd.read_block(i)
                    f.write(data)
                    md5.update(data)
                    sha.update(data)
                    done += 1
                    if done % 256 == 0 or done == total_blocks:
                        el = _t.time() - t0
                        pct = 100.0 * done / total_blocks
                        eta = el / done * (total_blocks - done)
                        sys.stdout.write("\r%8d / %d blok  %5.1f%%  %.0f KB/s  ETA %.0f sn   "
                                         % (done, total_blocks, pct, (done * 512 / 1024) / el, eta))
                        sys.stdout.flush()
            el = _t.time() - t0
            print("\nIMAGE OK: %s" % args.outfile)
            print("  blocks : %d (%d bayt)" % (done, done * 512))
            print("  sure   : %.1f dk" % (el / 60))
            print("  md5    : %s" % md5.hexdigest())
            print("  sha256 : %s" % sha.hexdigest())
        elif args.cmd == "probe":
            cid = sd.cid()
            print("Mevcut CID : %s" % cid.hex().upper())
            try:
                sd.write_cid(cid)
                print("CMD26 (tran) : DESTEKLIYOR (dogrulandi)")
            except sdlib.SDError as e:
                print("CMD26 (tran) : desteklenmiyor (%s)" % e)
            try:
                mod = bytearray(cid)
                psn = int.from_bytes(mod[9:13], "big")
                mod[9:13] = ((psn + 1) & 0xFFFFFFFF).to_bytes(4, "big")
                ok, variant = sd.program_cid_idle(bytes(mod), bytes(mod).hex().upper())
                if ok:
                    print("CMD26 (idle) : COMMIT EDIYOR! (varyant: %s)" % variant)
                    ok2, v2 = sd.program_cid_idle(cid, cid.hex().upper())
                    print("  -> orijinal CID geri yazildi: %s (mevcut: %s)"
                          % ("OK" if ok2 else "MANUEL GEREKLI", sd.cid().hex().upper()))
                else:
                    print("CMD26 (idle) : kabul ediyor ama COMMIT ETMIYOR (sahte kabul)")
            except sdlib.SDError as e:
                print("CMD26 (idle) : hata (%s)" % e)
                try:
                    sd.init()
                    print("  -> kartin su anki CID: %s %s"
                          % (sd.cid().hex().upper(),
                             "(DEGISTI!)" if sd.cid() != cid else "(ayni, degismedi)"))
                except sdlib.SDError as e2:
                    print("  -> kart hazirlanamadi: %s" % e2)
            try:
                csd = sd.csd()
                sd.write_csd(csd)
                print("CMD27 (CSD)  : DESTEKLIYOR (kendi CSD'i geri yazildi)")
            except sdlib.SDError as e:
                print("CMD27 (CSD)  : desteklenmiyor (%s)" % e)
        elif args.cmd == "export":
            import json
            import datetime
            info = {"zaman": datetime.datetime.now().isoformat()}
            cid = sd.cid()
            info["cid"] = cid.hex().upper()
            info["cid_decoded"] = sdlib.decode_cid(cid)
            csd = sd.csd()
            info["csd"] = csd.hex().upper()
            info["csd_decoded"] = sdlib.decode_csd(csd)
            try:
                info["scr"] = sd.scr().hex().upper()
            except sdlib.SDError:
                info["scr"] = None
            rca = sd.rca()
            info["rca"] = "%04X" % rca if rca is not None else None
            info["ocr"] = sd.ocr.hex().upper() if sd.ocr else None
            with open(args.outfile, "w") as f:
                json.dump(info, f, indent=2, ensure_ascii=False)
            print("kaydedildi: %s" % args.outfile)
        elif args.cmd == "writeimage":
            import os
            import time as _t
            cap = sdlib.decode_csd(sd.csd())["capacity"]
            max_blocks = cap // 512
            total = os.path.getsize(args.imgfile) // 512
            if args.start + total > max_blocks:
                print("HATA: imaj karttan buyuk (kart %d blok, imaj %d blok)" % (max_blocks, total))
                sys.exit(1)
            with open(args.imgfile, "rb") as f:
                f.seek(args.start * 512)
                t0 = _t.time()
                done = 0
                while True:
                    data = f.read(512)
                    if not data:
                        break
                    sd.write_block(args.start + done, data.ljust(512, b"\x00"))
                    if args.verify and sd.read_block(args.start + done) != data.ljust(512, b"\x00"):
                        print("\nHATA: dogrulama hatasi blok %d" % (args.start + done))
                        sys.exit(1)
                    done += 1
                    if done % 64 == 0 or done == total:
                        el = _t.time() - t0
                        pct = 100.0 * done / total
                        eta = el / done * (total - done)
                        sys.stdout.write("\r%8d / %d blok  %5.1f%%  %.0f KB/s  ETA %.0f sn   "
                                         % (done, total, pct, (done * 512 / 1024) / el, eta))
                        sys.stdout.flush()
            print("\nWRITE OK: %d blok, %.1f dk" % (done, (_t.time() - t0) / 60))
        elif args.cmd == "csdtest":
            csd = sd.csd()
            mod = bytearray(csd)
            mod[14] |= 0x10  # TMP_WRITE_PROTECT = 1 (gecici, geri alinabilir)
            try:
                sd.write_csd(bytes(mod))
            except sdlib.SDError as e:
                print("CMD27 yazma hatasi: %s" % e)
                sys.exit(1)
            now = sd.csd()
            # byte15 CRC'dir, kart kendisi yeniden hesaplar — karsilastirma disi
            if bytes(now[:15]) == bytes(mod[:15]):
                print("CSD commit EDI (byte14=%02X okundu). Geri aliniyor..." % now[14])
                sd.write_csd(csd)
                print("geri alindi: %s" % sd.csd().hex().upper())
                print("SONUC: CSD yazma CALISIYOR.")
            else:
                print("CSD commit YOK (okunan: %s)" % now.hex().upper())
                print("SONUC: CSD yazma da sessizce diskarde ediliyor.")
        elif args.cmd == "cidhunt":
            data = hex_bytes(args.hex)
            if len(data) != 16:
                print("HATA: CID 16 bayt olmali.")
                sys.exit(1)
            ok, how = sd.hunt_cid_commit(data, args.hex.upper())
            if ok:
                print("COMMIT BASARILI: %s" % how)
            else:
                print("Hicbir yontem commit etmedi.")
            print("Mevcut CID: %s" % sd.cid().hex().upper())
        elif args.cmd == "vendorcid":
            data = hex_bytes(args.hex)
            if len(data) != 16:
                print("HATA: CID 16 bayt olmali.")
                sys.exit(1)
            sd.samsung_backdoor_cid(data)
            print("Vendor backdoor/CMD26 tamamlandi.")
            print("Mevcut CID: %s" % sd.cid().hex().upper())
        else:
            print("bilinmeyen komut")
    except sdlib.SDError as e:
        print("HATA:", e)
        sys.exit(1)
    finally:
        sd.close()


if __name__ == "__main__":
    main()
