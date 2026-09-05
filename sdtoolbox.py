"""sdtoolbox: SDToolBox benzeri tkinter GUI (Pi + SPI)."""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import sdlib
import fatdump


class App:
    def __init__(self, root):
        self.root = root
        root.title("SD ToolBox (Pi + SPI)")
        root.geometry("760x620")
        self.sd = None

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="SPI (bus/dev):").pack(side="left")
        self.bus_var = tk.StringVar(value="0")
        self.dev_var = tk.StringVar(value="0")
        ttk.Entry(top, textvariable=self.bus_var, width=5).pack(side="left", padx=2)
        ttk.Entry(top, textvariable=self.dev_var, width=5).pack(side="left", padx=2)
        self.b_conn = ttk.Button(top, text="Bağlan", command=self.connect)
        self.b_conn.pack(side="left", padx=6)
        self.conn_lbl = ttk.Label(top, text="Bağlı değil", foreground="red")
        self.conn_lbl.pack(side="left", padx=6)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_info = ttk.Frame(nb)
        self.tab_fat = ttk.Frame(nb)
        nb.add(self.tab_info, text="INFO")
        nb.add(self.tab_fat, text="FAT")
        self._build_info()
        self._build_fat()

    def _build_info(self):
        f = self.tab_info
        grid = ttk.Frame(f, padding=8)
        grid.pack(fill="x")
        rows = [("CID", "cid"), ("CSD", "csd"), ("RCA", "rca"), ("SCR", "scr")]
        self.vars = {}
        for i, (label, key) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar()
            self.vars[key] = v
            ttk.Entry(grid, textvariable=v, width=64, state="readonly").grid(row=i, column=1, padx=4, pady=3)
        ttk.Button(grid, text="Bilgileri Oku", command=self.refresh_info).grid(row=len(rows), column=1, sticky="w", pady=4)

        mid = ttk.Frame(f, padding=8)
        mid.pack(fill="x")
        # CID write
        cidbox = ttk.LabelFrame(mid, text="CID Yaz", padding=6)
        cidbox.pack(side="left", fill="both", expand=True, padx=4)
        ttk.Button(cidbox, text="CID Yaz", command=self.write_cid).pack(fill="x")

        # password
        pwbox = ttk.LabelFrame(mid, text="Şifre (CMD42)", padding=6)
        pwbox.pack(side="left", fill="both", expand=True, padx=4)
        self.pwd_var = tk.StringVar()
        ttk.Entry(pwbox, textvariable=self.pwd_var, width=16, show="*").pack(fill="x", pady=2)
        ttk.Button(pwbox, text="Set", command=self.set_pwd).pack(fill="x", pady=1)
        ttk.Button(pwbox, text="Clear", command=self.clear_pwd).pack(fill="x", pady=1)

        # write protection
        wpbox = ttk.LabelFrame(mid, text="Yazma Koruması", padding=6)
        wpbox.pack(side="left", fill="both", expand=True, padx=4)
        self.wp_var = tk.StringVar(value="0")
        ttk.Entry(wpbox, textvariable=self.wp_var, width=10).pack(fill="x", pady=2)
        ttk.Button(wpbox, text="Set", command=self.set_wp).pack(fill="x", pady=1)
        ttk.Button(wpbox, text="Clear", command=self.clear_wp).pack(fill="x", pady=1)

        # erase
        erbox = ttk.LabelFrame(mid, text="Zorla Silme", padding=6)
        erbox.pack(side="left", fill="both", expand=True, padx=4)
        ttk.Button(erbox, text="Tüm Kartı Sil", command=self.erase_card).pack(fill="x")

        self.log = scrolledtext.ScrolledText(f, state="disabled", wrap="word",
                                             font=("Consolas", 9), height=16)
        self.log.pack(fill="both", expand=True, padx=8, pady=4)

    def _build_fat(self):
        f = self.tab_fat
        top = ttk.Frame(f, padding=8)
        top.pack(fill="x")
        self.img_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.img_var, width=52).pack(side="left", padx=4)
        ttk.Button(top, text="Gözat", command=self.browse_img).pack(side="left")
        ttk.Button(top, text="Listele", command=self.list_files).pack(side="left", padx=4)
        self.out_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.out_var, width=30).pack(side="left", padx=4)
        ttk.Button(top, text="Çıkar", command=self.extract_files).pack(side="left")
        self.fat_log = scrolledtext.ScrolledText(f, state="disabled", wrap="word",
                                                 font=("Consolas", 9), height=20)
        self.fat_log.pack(fill="both", expand=True, padx=8, pady=4)

    def _log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _fat_log(self, text):
        self.fat_log.config(state="normal")
        self.fat_log.insert("end", text + "\n")
        self.fat_log.see("end")
        self.fat_log.config(state="disabled")

    def connect(self):
        bus = int(self.bus_var.get())
        dev = int(self.dev_var.get())
        try:
            if self.sd:
                self.sd.close()
            self.sd = sdlib.SD(bus, dev)
            self.sd.init()
            self.conn_lbl.config(text="Bağlı (bus %d dev %d)" % (bus, dev), foreground="green")
            self._log("Kart bağlandı: bus=%d dev=%d" % (bus, dev))
            self.refresh_info()
        except Exception as e:
            self.sd = None
            self.conn_lbl.config(text="Bağlı değil", foreground="red")
            self._log("BAĞLANTI HATASI: %s" % e)

    def refresh_info(self):
        if not self.sd:
            self._log("Önce Bağlan.")
            return
        try:
            cid = self.sd.cid()
            self.vars["cid"].set(cid.hex().upper())
            d = sdlib.decode_cid(cid)
            self._log("CID: MID=0x%02X PNM=%r PSN=%08X" % (d["mid"], d["name"], d["serial"]))
        except sdlib.SDError as e:
            self.vars["cid"].set("OKUNAMADI")
            self._log("CID: %s" % e)
        try:
            csd = self.sd.csd()
            self.vars["csd"].set(csd.hex().upper())
            d = sdlib.decode_csd(csd)
            self._log("CSD: %s %.1f MiB" % (d["kind"], d["capacity"] / 1048576))
        except sdlib.SDError as e:
            self.vars["csd"].set("OKUNAMADI")
            self._log("CSD: %s" % e)
        try:
            scr = self.sd.scr()
            self.vars["scr"].set(scr.hex().upper())
        except sdlib.SDError as e:
            self.vars["scr"].set("OKUNAMADI")
            self._log("SCR: %s" % e)
        rca = self.sd.rca()
        self.vars["rca"].set(("%04X" % rca) if rca is not None else "yok")

    def write_cid(self):
        if not self.sd:
            self._log("Önce Bağlan.")
            return
        val = self.vars["cid"].get()
        try:
            data = bytes.fromhex(val.replace(" ", ""))
        except ValueError:
            self._log("CID hex değil: %s" % val)
            return
        if len(data) != 16:
            self._log("CID 16 bayt olmalı.")
            return
        if not messagebox.askyesno("Dikkat", "CID kalıcı olarak değiştirilecek. Emin misin?"):
            return
        try:
            self.sd.write_cid(data)
            self._log("CID yazıldı.")
        except sdlib.SDError as e:
            self._log("CID YAZMA HATASI: %s" % e)

    def set_pwd(self):
        self._pwd_op("set", self.set_password)

    def clear_pwd(self):
        self._pwd_op("clear", self.clear_password)

    def _pwd_op(self, name, fn):
        if not self.sd:
            self._log("Önce Bağlan.")
            return
        pwd = self.pwd_var.get()
        if len(pwd) > 16:
            self._log("Şifre max 16 karakter.")
            return
        if not messagebox.askyesno("Dikkat", "%s şifresi (%d karakter)? Emin misin?" % (name, len(pwd))):
            return
        try:
            fn(pwd.encode())
            self._log("%s şifresi tamam." % name)
        except sdlib.SDError as e:
            self._log("%s HATASI: %s" % (name, e))

    def set_password(self, b):
        self.sd.set_password(b)

    def clear_password(self, b):
        self.sd.clear_password(b)

    def set_wp(self):
        self._wp_op("set", self.sd.set_write_protect)

    def clear_wp(self):
        self._wp_op("clear", self.sd.clear_write_protect)

    def _wp_op(self, name, fn):
        if not self.sd:
            self._log("Önce Bağlan.")
            return
        try:
            addr = int(self.wp_var.get())
        except ValueError:
            self._log("Blok adresi sayı olmalı.")
            return
        if not messagebox.askyesno("Dikkat", "Blok %d yazma koruması %s. Emin misin?" % (addr, name)):
            return
        try:
            fn(addr)
            self._log("Blok %d %s tamam." % (addr, name))
        except sdlib.SDError as e:
            self._log("%s HATASI: %s" % (name, e))

    def erase_card(self):
        if not self.sd:
            self._log("Önce Bağlan.")
            return
        if not messagebox.askyesno("ÇOK DİKKAT", "Tüm kart silinecek (CMD38). VERİ KAYBI. Emin misin?"):
            return
        try:
            csd = self.sd.csd()
            d = sdlib.decode_csd(csd)
            last = (d["capacity"] // 512) - 1
            self.sd.erase_blocks(0, last)
            self._log("Tüm kart silindi (0..%d)." % last)
        except sdlib.SDError as e:
            self._log("SİLME HATASI: %s" % e)

    def browse_img(self):
        p = filedialog.askopenfilename(filetypes=[("Image", "*.img"), ("All", "*.*")])
        if p:
            self.img_var.set(p)

    def list_files(self):
        img = self.img_var.get()
        if not img:
            self._fat_log("Önce imaj dosyası seç.")
            return
        try:
            r = fatdump.FatReader(img)
            self._fat_log("# fs=%d-bit  bps=%d  spc=%d  part_start=%d" % (r.type, r.bps, r.spc, r.part_start))
            for full, ent in r.walk():
                t = "DIR " if ent["isdir"] else "FILE"
                self._fat_log("%s  %s  %9d  %s" % (t, full, ent["size"], ent["mtime"]))
        except Exception as e:
            self._fat_log("HATA: %s" % e)

    def extract_files(self):
        img = self.img_var.get()
        out = self.out_var.get()
        if not img or not out:
            self._fat_log("İmaj dosyası ve çıkarma klasörü seç.")
            return
        try:
            r = fatdump.FatReader(img)
            total = 0
            for full, ent in r.walk():
                if ent["isdir"]:
                    import os
                    os.makedirs(os.path.join(out, full), exist_ok=True)
                else:
                    import os
                    dest = os.path.join(out, full)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    data = r.read_file(ent["cluster"], ent["size"])
                    with open(dest, "wb") as f:
                        f.write(data)
                    total += len(data)
            self._fat_log("Çıkarıldı: %d dosya, %d bayt -> %s" % (sum(1 for _, e in r.walk() if not e["isdir"]), total, out))
        except Exception as e:
            self._fat_log("HATA: %s" % e)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
