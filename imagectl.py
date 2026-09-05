import sys, os, struct, time, hashlib, argparse, ctypes, json, subprocess
import ctypes.wintypes as wt

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
INVALID = ctypes.c_void_p(0xFFFFFFFFFFFFFFFF).value

_k = ctypes.windll.kernel32
_k.CreateFileW.restype = ctypes.c_void_p
_k.CreateFileW.argtypes = [ctypes.c_wchar_p, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
_k.GetLastError.restype = wt.DWORD
_k.ReadFile.restype = wt.BOOL
_k.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k.CloseHandle.restype = wt.BOOL
_k.CloseHandle.argtypes = [ctypes.c_void_p]
_k.DeviceIoControl.restype = wt.BOOL
_k.DeviceIoControl.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]


def _last_err():
    return ctypes.get_last_error()


def open_raw(path):
    for access in (GENERIC_READ, GENERIC_READ | GENERIC_WRITE):
        h = _k.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if h is not None and h != INVALID:
            return h
    raise OSError(
        "Cannot open '%s' (Win32 error %d). "
        "Check: 1) run this as Administrator, 2) run 'imagectl list' to confirm the PhysicalDrive number, "
        "3) the card reader must be present." % (path, _last_err()))


def read_some(h, n):
    buf = ctypes.create_string_buffer(n)
    got = wt.DWORD(0)
    ok = _k.ReadFile(h, buf, n, ctypes.byref(got), None)
    if not ok:
        raise OSError("ReadFile failed: %s" % ctypes.get_last_error())
    return buf.raw[:got.value], got.value


def device_length(path):
    h = open_raw(path)
    try:
        out = ctypes.create_string_buffer(8)
        r = wt.DWORD(0)
        ret = _k.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0, out, 8, ctypes.byref(r), None)
        if not ret:
            raise OSError("DeviceIoControl failed: %s" % ctypes.get_last_error())
        return struct.unpack("<Q", out.raw)[0]
    finally:
        _k.CloseHandle(h)


def _fmt(n, chunk=1 << 20):
    h = open_raw(n)
    length = device_length(n)
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    done = 0
    start = time.time()
    with open(chunk, "wb") as f:
        while done < length:
            want = min(1 << 20, length - done)
            data, got = read_some(h, want)
            if got == 0:
                break
            got = min(got, want)
            f.write(data[:got])
            sha.update(data[:got])
            md5.update(data[:got])
            done += got
            sys.stdout.write("\r%12.2f / %12.2f MB  %6.2f%%" % (done / 1e6, length / 1e6, 100.0 * done / max(1, length)))
            sys.stdout.flush()
    _k.CloseHandle(h)
    el = time.time() - start
    sys.stdout.write("\n")
    return dict(size=done, device_size=length, sha256=sha.hexdigest(), md5=md5.hexdigest(), elapsed=el)


def file_hashes(path, chunk=1 << 20):
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            sha.update(b)
            md5.update(b)
            size += len(b)
    return dict(size=size, sha256=sha.hexdigest(), md5=md5.hexdigest())


def compare(device, imgfile, chunk=1 << 20):
    h = open_raw(device)
    length = device_length(device)
    total = min(length, os.path.getsize(imgfile))
    mism = 0
    done = 0
    first = None
    with open(imgfile, "rb") as f:
        while done < total:
            want = min(chunk, total - done)
            data, got = read_some(h, want)
            ref = f.read(got)
            if not ref:
                break
            n = min(got, len(ref))
            for i in range(n):
                if data[i] != ref[i]:
                    mism += 1
                    if first is None:
                        first = done + i
            done += n
            sys.stdout.write("\r%12.2f / %12.2f MB  diffs=%d" % (done / 1e6, total / 1e6, mism))
            sys.stdout.flush()
    _k.CloseHandle(h)
    sys.stdout.write("\n")
    return dict(compared=done, mismatches=mism, first_offset=first)


def get_drives():
    ps = (
        "Get-CimInstance Win32_DiskDrive | "
        "Select-Object Index,Model,Size,MediaType,PNPDeviceID | ConvertTo-Json -Compress"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    try:
        data = json.loads(out)
    except Exception:
        data = []
    if isinstance(data, dict):
        data = [data]
    return data


def list_drives(as_json=False):
    data = get_drives()
    if as_json:
        print(json.dumps(data))
        return
    print("Index  Size(bytes)     MediaType        Model / PNPID")
    print("-----  --------------  ---------------  ------------------------------")
    for d in data:
        keep = isinstance(d, dict)
        idx = d.get("Index") if keep else "?"
        size = d.get("Size") if keep else "?"
        media = d.get("MediaType") if keep else "?"
        model = d.get("Model") if keep else "?"
        pnp = d.get("PNPDeviceID") if keep else "?"
        if keep and pnp and "USBSTOR" in str(pnp):
            model = "%s    <== USB READER (SD card)" % model
        print("%-5s  %-14s  %-15s  %s" % (idx, size, media, model))
    print()
    print("SD kart icin 'Image 1' satirindaki Index degerini kullan, oRN: imagectl image \\\\.\\PhysicalDrive<Index> out.img")


def main():
    p = argparse.ArgumentParser(prog="imagectl", description="Raw disk imaging + verification for \\.\\PhysicalN (admin required).")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="list all physical drives and identify the SD card")
    lp.add_argument("--json", action="store_true", help="output JSON for tools/GUI")

    a = sub.add_parser("image", help="stream device to .img (sha256+md5)")
    a.add_argument("device")
    a.add_argument("outfile")
    a.add_argument("--force", action="store_true", help="allow imaging devices larger than 8 GB")

    b = sub.add_parser("info", help="print device length / size")
    b.add_argument("device")

    c = sub.add_parser("hash", help="hash an existing .img")
    c.add_argument("imgfile")

    d = sub.add_parser("compare", help="compare device to .img bitwise")
    d.add_argument("device")
    d.add_argument("imgfile")

    args = p.parse_args()
    if args.cmd == "list":
        list_drives(as_json=getattr(args, "json", False))
        return
    if args.cmd == "image":
        if not args.force:
            try:
                length = device_length(args.device)
            except OSError:
                length = 0
            if length > 8 * 1024 ** 3:
                print("ERROR: '%s' is %d bytes (> 8 GB). This is likely your main disk, not the SD card." % (args.device, length))
                print("Run 'imagectl list' to pick the correct PhysicalDrive, or use --force if you really mean it.")
                sys.exit(1)
        r = _fmt(args.device, args.outfile)
        print("IMAGE OK")
        print("  bytes        : %d" % r["size"])
        print("  device_bytes : %d" % r["device_size"])
        print("  md5          : %s" % r["md5"])
        print("  sha256       : %s" % r["sha256"])
        print("  elapsed_s    : %.1f" % r["elapsed"])
    elif args.cmd == "info":
        print("device_length_bytes: %d" % device_length(args.device))
    elif args.cmd == "hash":
        r = file_hashes(args.imgfile)
        print("  bytes   : %d" % r["size"])
        print("  md5     : %s" % r["md5"])
        print("  sha256  : %s" % r["sha256"])
    elif args.cmd == "compare":
        r = compare(args.device, args.imgfile)
        print("COMPARE %s" % ("OK" if r["mismatches"] == 0 else "MISMATCH"))
        print("  compared     : %d" % r["compared"])
        print("  mismatches   : %d" % r["mismatches"])
        print("  first_offset : %s" % r["first_offset"])


if __name__ == "__main__":
    main()
