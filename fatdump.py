import sys, os, struct, argparse, datetime, stat

GPT = [0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E]
ATTR_DIR = 0x10
ATTR_LFN = 0x0F
ATTR_VOL = 0x08
ATTR_LONG_MASK = 0x3F
CLUSTER_END_12 = 0xFF8
CLUSTER_END_16 = 0xFFF8
CLUSTER_END_32 = 0x0FFFFFF8


def decode_lfn(entries):
    name = []
    for e in sorted(entries, key=lambda x: x[1]):
        chars = []
        for off in (1, 3, 5, 7, 9):
            v = struct.unpack_from("<H", e[0], off)[0]
            if v != 0 and v != 0xFFFF:
                chars.append(v)
        for off in (14, 16, 18, 20, 22, 24):
            v = struct.unpack_from("<H", e[0], off)[0]
            if v != 0 and v != 0xFFFF:
                chars.append(v)
        for off in (28, 30):
            v = struct.unpack_from("<H", e[0], off)[0]
            if v != 0 and v != 0xFFFF:
                chars.append(v)
        name.extend(chars)
    return "".join(chr(c) for c in name)


def short_name(e):
    raw = e[0:8] + e[8:11]
    nm = raw[0:8].decode("ascii", "replace").rstrip()
    ext = raw[8:11].decode("ascii", "replace").rstrip()
    if not nm:
        return None
    if ext:
        return nm + "." + ext
    return nm


def decode_time(date_raw, time_raw):
    if date_raw == 0:
        return ""
    try:
        y = ((date_raw >> 9) & 0x7F) + 1980
        mo = (date_raw >> 5) & 0x0F
        d = date_raw & 0x1F
        h = (time_raw >> 11) & 0x1F
        mi = (time_raw >> 5) & 0x3F
        return "%04d-%02d-%02d %02d:%02d" % (y, mo, d, h, mi)
    except Exception:
        return ""


class FatReader:
    def __init__(self, path=None, backend=None, total_size=0):
        self.backend = backend
        if backend is None:
            with open(path, "rb") as f:
                self.data = f.read()
            self.raw = self.data
        else:
            self.total_size = total_size
        self.bps = 512
        self._init_geometry()

    def rsect(self, sector, count=1):
        if self.backend is not None:
            return self.backend.read_sectors(sector, count)
        off = sector * self.bps
        end = off + count * self.bps
        return self.raw[off:end]

    def _init_geometry(self):
        mbr = self.rsect(0, 1)
        part = None
        for i in range(4):
            ent = mbr[446 + i * 16:446 + i * 16 + 16]
            if len(ent) < 16:
                break
            if ent[4] in GPT and ent[4] != 0:
                part = struct.unpack_from("<II", ent, 8)
                break
        if part:
            self.part_start = part[0]
        else:
            self.part_start = 0
        bpb = self.rsect(self.part_start, 1)
        self.bps = struct.unpack_from("<H", bpb, 0x0B)[0]
        self.spc = bpb[0x0D]
        self.reserved = struct.unpack_from("<H", bpb, 0x0E)[0]
        self.numfats = bpb[0x10]
        self.rootent = struct.unpack_from("<H", bpb, 0x11)[0]
        self.fatsz16 = struct.unpack_from("<H", bpb, 0x16)[0]
        self.totsect = struct.unpack_from("<I", bpb, 0x20)[0] or struct.unpack_from("<H", bpb, 0x13)[0]
        rootdirs = (self.rootent * 32 + self.bps - 1) // self.bps
        self.fat_start = self.part_start + self.reserved
        fat_sectors = self.fatsz16
        if fat_sectors == 0:
            fat_sectors = struct.unpack_from("<I", bpb, 0x24)[0]
        self.fat_size = fat_sectors
        self.rootdir_start = self.fat_start + self.numfats * fat_sectors
        self.data_start = self.rootdir_start + rootdirs
        total_clusters = (self.totsect - (self.reserved + self.numfats * fat_sectors + rootdirs)) // self.spc
        if total_clusters < 4085:
            self.type = 12
        elif total_clusters < 65525:
            self.type = 16
        else:
            self.type = 32
        if self.type == 32:
            self.root_cluster = struct.unpack_from("<I", bpb, 0x2C)[0] & 0x0FFFFFFF
            self.data_start = self.rootdir_start
            self.rootdirs_sectors = 0
        else:
            self.root_cluster = None
            self.rootdir_len = rootdirs * self.bps

    def truncate_label(self, entry_attr, entry_name):
        return None

    def _fat_read(self, off, n):
        sec = self.fat_start + off // self.bps
        k = off % self.bps
        if k + n <= self.bps:
            return self.rsect(sec)[k:k + n]
        return self.rsect(sec, 2)[k:k + n]

    def get_fat(self, cluster):
        if self.type == 12:
            b = self._fat_read(cluster + cluster // 2, 2)
            v = struct.unpack("<H", b.ljust(2, b"\x00"))[0]
            if cluster & 1:
                return v >> 4
            return v & 0xFFF
        elif self.type == 16:
            return struct.unpack("<H", self._fat_read(cluster * 2, 2))[0]
        else:
            return struct.unpack("<I", self._fat_read(cluster * 4, 4))[0] & 0x0FFFFFFF

    def cluster_to_sector(self, cluster):
        return self.data_start + (cluster - 2) * self.spc

    def read_clusters(self, start_cluster):
        if start_cluster == 0:
            return b""
        out = bytearray()
        cluster = start_cluster
        guard = 0
        if self.type == 32:
            end = CLUSTER_END_32
        elif self.type == 16:
            end = CLUSTER_END_16
        else:
            end = CLUSTER_END_12
        seen = set()
        while cluster >= 2 and cluster < end and guard < 1_000_000 and cluster not in seen:
            seen.add(cluster)
            sec = self.cluster_to_sector(cluster)
            out.extend(self.rsect(sec, self.spc))
            cluster = self.get_fat(cluster)
            guard += 1
        return bytes(out)

    def _iter_dir(self, first_cluster, is_root):
        if is_root and self.type != 32:
            nsec = (self.rootdir_len + self.bps - 1) // self.bps
            return b"".join(self.rsect(self.rootdir_start + i) for i in range(nsec))
        return self.read_clusters(first_cluster)

    def walk(self):
        root_clusters = self.root_cluster if self.type == 32 else 0
        results = []
        self._walk_dir(root_clusters if self.type == 32 else None, "", results, is_root=True)
        return results

    def _read_dir_entries(self, first_cluster, is_root):
        data = self._iter_dir(first_cluster, is_root)
        entries = []
        i = 0
        lfn_pending = []
        while i + 32 <= len(data):
            e = data[i:i + 32]
            if e[0] == 0x00:
                break
            if e[0] == 0xE5:
                i += 32
                continue
            attr = e[0x0B]
            if attr == ATTR_LFN:
                order = e[0] & 0x1F
                lfn_pending.append((e, order))
                i += 32
                continue
            name = short_name(e)
            isdir = bool(attr & ATTR_DIR)
            isvol = bool(attr & ATTR_VOL)
            longname = decode_lfn(lfn_pending) if lfn_pending else ""
            lfn_pending = []
            if name and not isvol:
                if name not in (".", ".."):
                    if self.type == 32:
                        hi = struct.unpack_from("<H", e, 0x14)[0]
                        lo = struct.unpack_from("<H", e, 0x1A)[0]
                        cluster = ((hi << 16) | lo) & 0x0FFFFFFF
                    else:
                        cluster = struct.unpack_from("<H", e, 0x1A)[0]
                    size = struct.unpack_from("<I", e, 0x1C)[0]
                    dt = decode_time(struct.unpack_from("<H", e, 0x18)[0], struct.unpack_from("<H", e, 0x16)[0])
                    entries.append(dict(name=longname or name, short=name, isdir=isdir, size=size,
                                        cluster=cluster, mtime=dt, raw_e=e))
            i += 32
        return entries

    def _walk_dir(self, cluster, prefix, results, is_root):
        ents = self._read_dir_entries(cluster, is_root)
        for ent in ents:
            full = prefix + ent["name"]
            results.append((full, ent))
            if ent["isdir"]:
                self._walk_dir(ent["cluster"], full + "/", results, is_root=False)

    def read_file(self, cluster, size):
        data = self.read_clusters(cluster)
        return data[:size]


def main():
    import argparse
    p = argparse.ArgumentParser(prog="fatdump", description="List/extract FAT12/16/32 image files (for .img from imagectl).")
    p.add_argument("img")
    p.add_argument("--list", action="store_true", help="list files")
    p.add_argument("--files-only", action="store_true", help="list regular files only")
    p.add_argument("--extract", metavar="DIR", help="extract all files under DIR")
    p.add_argument("--file", metavar="PATH", help="extract a single path to stdout")
    args = p.parse_args()

    r = FatReader(args.img)
    print("# fs=%d-bit  bps=%d  spc=%d  part_start=%d" % (r.type, r.bps, r.spc, r.part_start), file=sys.stderr)
    entries = r.walk()
    if args.file:
        for full, ent in entries:
            if full == args.file and not ent["isdir"]:
                sys.stdout.buffer.write(r.read_file(ent["cluster"], ent["size"]))
                return
        print("not found", file=sys.stderr)
        sys.exit(1)
    if args.list:
        for full, ent in entries:
            if args.files_only and ent["isdir"]:
                continue
            t = "DIR " if ent["isdir"] else "FILE"
            print("%s  %s  %9d  %s" % (t, full, ent["size"], ent["mtime"]))
    if args.extract:
        import os as _os
        total = 0
        for full, ent in entries:
            if ent["isdir"]:
                _os.makedirs(_os.path.join(args.extract, full), exist_ok=True)
            else:
                dest = _os.path.join(args.extract, full)
                _os.makedirs(_os.path.dirname(dest), exist_ok=True)
                data = r.read_file(ent["cluster"], ent["size"])
                with open(dest, "wb") as f:
                    f.write(data)
                total += len(data)
                print("wrote %d bytes -> %s" % (len(data), dest))
        print("extracted %d files, %d bytes" % (sum(1 for _, e in entries if not e["isdir"]), total), file=sys.stderr)


if __name__ == "__main__":
    main()

