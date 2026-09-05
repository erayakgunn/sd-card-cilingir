import os, sys, re, json, subprocess, threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import ctypes

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
IMG = os.path.join(BASE, "imagectl.py")
FAT = os.path.join(BASE, "fatdump.py")
PROG_RE = re.compile(r"([\d.]+)\s*/\s*([\d.]+)\s*MB\s+([\d.]+)%")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated(path=None):
    path = path or os.path.abspath(__file__)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", PY, '"%s"' % path, BASE, 1)


class App:
    def __init__(self, root):
        self.root = root
        root.title("SD Kart Aracı - Yedek/Denetim")
        root.geometry("900x680")
        self.proc = None
        self.running = False
        self.drives = []

        self._build_ui()
        self.refresh_drives()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        self.admin_lbl = ttk.Label(top, text="", foreground="red")
        self.admin_lbl.pack(anchor="w", pady=(0, 4))

        row1 = ttk.Frame(top)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Hedef Disk:").pack(side="left")
        self.drive_var = tk.StringVar()
        self.drive_cb = ttk.Combobox(row1, textvariable=self.drive_var, width=52, state="readonly")
        self.drive_cb.pack(side="left", padx=6)
        ttk.Button(row1, text="Tara", command=self.refresh_drives).pack(side="left")
        ttk.Button(row1, text="Yönetici Olarak Yeniden Başlat", command=relaunch_elevated).pack(side="left", padx=6)

        row2 = ttk.Frame(top)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Imaj dosyası:").pack(side="left")
        self.img_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.img_var, width=52).pack(side="left", padx=6)
        ttk.Button(row2, text="Gözat", command=self.browse_img).pack(side="left")

        row3 = ttk.Frame(top)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="Çıkarma klasörü:").pack(side="left")
        self.outvar = tk.StringVar()
        ttk.Entry(row3, textvariable=self.outvar, width=52).pack(side="left", padx=6)
        ttk.Button(row3, text="Gözat", command=self.browse_out).pack(side="left")

        btns = ttk.Frame(top)
        btns.pack(fill="x", pady=8)
        self.b_img = ttk.Button(btns, text="Imaj Al (birebir yedek)", command=lambda: self.run("image"))
        self.b_cmp = ttk.Button(btns, text="Kartla Karşılaştır", command=lambda: self.run("compare"))
        self.b_hash = ttk.Button(btns, text="Hash Doğrula", command=lambda: self.run("hash"))
        self.b_list = ttk.Button(btns, text="Dosya Listele", command=lambda: self.run("list"))
        self.b_extract = ttk.Button(btns, text="Dosyaları Çıkar", command=lambda: self.run("extract"))
        for b in (self.b_img, self.b_cmp, self.b_hash, self.b_list, self.b_extract):
            b.pack(side="left", padx=3)
        self.b_stop = ttk.Button(btns, text="Durdur", command=self.stop, state="disabled")
        self.b_stop.pack(side="left", padx=3)

        self.prog = ttk.Progressbar(self.root, maximum=100)
        self.prog.pack(fill="x", padx=8)
        self.status = ttk.Label(self.root, text="Hazır")
        self.status.pack(anchor="w", padx=8)

        ttk.Label(self.root, text="CID/CSD (üretici/seri) USB okuyucu + Windows üzerinden OKUNAMAZ; onun için Pi/STM32 SPI kullanın.",
                  foreground="gray").pack(anchor="w", padx=8)

        self.log = scrolledtext.ScrolledText(self.root, state="disabled", wrap="word",
                                             font=("Consolas", 9), height=22)
        self.log.pack(fill="both", expand=True, padx=8, pady=4)

        self._set_admin_label()
        self._set_buttons(True)

    def _set_admin_label(self):
        if is_admin():
            self.admin_lbl.config(text="[Yönetici] Imaj/Karşılaştırma aktif.", foreground="green")
        else:
            self.admin_lbl.config(text="[UYARI] Yönetici değilsiniz. 'Imaj Al' ve 'Karşılaştır' için Yönetici olarak yeniden başlatın.", foreground="red")

    def _set_buttons(self, en):
        state = "normal" if en else "disabled"
        for b in (self.b_img, self.b_cmp, self.b_hash, self.b_list, self.b_extract):
            b.config(state=state)
        self.b_stop.config(state="disabled" if en else "normal")

    def browse_img(self):
        p = filedialog.asksaveasfilename(defaultextension=".img", filetypes=[("Image", "*.img"), ("All", "*.*")],
                                         initialfile="sd_card.img")
        if p:
            self.img_var.set(p)

    def browse_out(self):
        p = filedialog.askdirectory()
        if p:
            self.outvar.set(p)

    def refresh_drives(self):
        self.drives = []
        try:
            out = subprocess.run([PY, IMG, "list", "--json"], capture_output=True, text=True,
                                 cwd=BASE, timeout=60).stdout
            data = json.loads(out)
        except Exception as e:
            self.log_msg("Disk listesi alınamadı: %s" % e)
            return
        if isinstance(data, dict):
            data = [data]
        labels = []
        for d in data:
            idx = d.get("Index")
            model = d.get("Model")
            size = d.get("Size", 0) or 0
            media = d.get("MediaType", "")
            pnp = str(d.get("PNPDeviceID", ""))
            removable = "USBSTOR" in pnp or "Removable" in media
            tag = "  [REMOVABLE/SD]" if removable else ""
            labels.append((idx, "%s — %s (%d MB)%s" % (idx, model, size // (1024 * 1024), tag)))
            self.drives.append(d)
        self.drive_cb["values"] = [l for _, l in labels]
        if labels:
            self.drive_cb.current(0)
        self.log_msg("Diskler tarandı: %d adet." % len(labels))

    def selected_index(self):
        txt = self.drive_var.get()
        if not txt:
            return None
        try:
            return int(txt.split("—")[0].strip())
        except Exception:
            return None

    def device_path(self):
        i = self.selected_index()
        return r"\\.\PhysicalDrive%d" % i if i is not None else None

    def log_msg(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _set_realtime(self):
        self.root.update_idletasks()

    def run(self, kind):
        if self.running:
            return
        dev = self.device_path()
        img = self.img_var.get()
        if kind in ("image", "compare") and not dev:
            self.log_msg("Önce disk seç (Tara).")
            return
        if kind in ("image", "hash", "compare", "list", "extract") and not img:
            self.log_msg("Bu işlem için bir imaj dosyası seç.")
            return
        outdir = self.outvar.get()

        if kind == "image":
            cmd = [PY, IMG, "image", dev, img]
        elif kind == "compare":
            cmd = [PY, IMG, "compare", dev, img]
        elif kind == "hash":
            cmd = [PY, IMG, "hash", img]
        elif kind == "list":
            cmd = [PY, FAT, img, "--list"]
        elif kind == "extract":
            if not outdir:
                self.log_msg("Çıkarma klasörü seç.")
                return
            cmd = [PY, FAT, img, "--extract", outdir]
        else:
            return

        if kind in ("image", "compare") and not is_admin():
            self.log_msg("UYARI: '%s' yönetici gerektirir. Şu an sonuç hata dönebilir." % kind)

        self.running = True
        self._set_buttons(False)
        self.prog["value"] = 0
        self.log_msg(">>> %s  (%s)" % (kind, " ".join(cmd)))
        if kind == "image":
            self.status.config(text="Imaj alınıyor...")
        elif kind == "compare":
            self.status.config(text="Karşılaştırılıyor...")
        elif kind == "extract":
            self.status.config(text="Çıkarılıyor...")
        else:
            self.status.config(text="Çalışıyor...")

        t = threading.Thread(target=self._worker, args=(cmd, kind), daemon=True)
        t.start()

    def _worker(self, cmd, kind):
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         cwd=BASE)
            self._reader(self.proc, kind)
            rc = self.proc.wait()
            self.root.after(0, self._done, rc, kind)
        except Exception as e:
            self.root.after(0, self._done, -1, kind, str(e))

    def _reader(self, proc, kind):
        fd = proc.stdout.fileno()
        buf = b""
        while True:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.rstrip(b"\r").decode("utf-8", "replace")
                if line:
                    self.root.after(0, self.log_msg, line)
            seg = buf.rsplit(b"\r", 1)[-1].decode("utf-8", "replace")
            m = PROG_RE.search(seg)
            if m and kind in ("image", "compare"):
                pct = float(m.group(3))
                self.root.after(0, self._set_prog, pct, m.group(1), m.group(2))
        tail = buf.strip().decode("utf-8", "replace")
        if tail:
            self.root.after(0, self.log_msg, tail)

    def _set_prog(self, pct, cur, total):
        self.prog["value"] = pct
        self.status.config(text="Imaj: %s / %s MB" % (cur, total))

    def _done(self, rc, kind, err=""):
        self.running = False
        self._set_buttons(True)
        self.proc = None
        if err:
            self.log_msg("HATA: %s" % err)
        self.status.config(text="Tamamlandı (rc=%s)" % rc)
        self.prog["value"] = 100 if rc == 0 else 0
        self.log_msg("<<< %s işlemi bitti (rc=%s)" % (kind, rc))

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.log_msg("Durduruldu.")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
