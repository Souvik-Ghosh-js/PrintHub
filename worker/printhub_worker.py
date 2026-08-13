"""
PrintHub Print Worker — vendor desktop app (spec §7).

The shop PC keeps this window open. The vendor logs in with their
auto-generated PrintHub credentials; the app then polls the server for
confirmed jobs, routes each to the right printer, prints, and reports back.

Printer configuration (spec §7):
  - Single Printer mode: one printer (handles B/W + colour) + preferred tray.
  - Multiple Printer mode (max 2): a B/W printer and a Colour printer, each
    with its own preferred tray. B/W jobs route to the B/W printer, colour
    jobs to the colour printer automatically.
  - "Save / Change Printer" can be used at any time without reinstalling;
    the config also syncs to the vendor dashboard on the server.

Printing/confirmation logic is ported from the working prototype
(printer/printer/worker_app.py). Package with PyInstaller (see build_exe.bat).
"""
import json
import os
import time
import threading
import queue
import tempfile
import subprocess

import requests
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import win32print
import win32api
import win32con

DEFAULT_SERVER = "https://mohiniprintshop.com/"
POLL_SECONDS = 10
PRINT_CONFIRM_TIMEOUT = 120
# After a genuine printer failure mid-print, wait this long before retrying
# that job, so a persistent fault can't spin the queue.
RETRY_COOLDOWN = 120
# When we haven't printed anything yet and are just waiting for the printer
# to come back (offline / out of paper / not selected), re-check often so the
# job goes out seconds after the printer is switched on.
WAIT_COOLDOWN = 15

AUTO_TRAY_LABEL = "Auto (printer default)"

# One build serves EVERY vendor: identity comes from the login, and this
# local file remembers the shop's server URL + session so the app reconnects
# by itself after a restart/reboot.
CONFIG_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                           "PrintHub", "worker.json")


def load_local_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_local_config(cfg: dict):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        print(f"[config] could not save {CONFIG_PATH}: {e}")


# ---------------------------------------------------------------------------
# Session state (set at login / config time; read by the worker thread)
# ---------------------------------------------------------------------------
class Session:
    def __init__(self):
        self.base_url = DEFAULT_SERVER
        self.token = None
        self.shop_name = ""
        self.lock = threading.Lock()
        # config: mode single|multi; printers/trays by role
        self.mode = "single"
        self.printer_single = None
        self.tray_single = None      # tray NAME or None (auto)
        self.printer_bw = None
        self.tray_bw = None
        self.printer_colour = None
        self.tray_colour = None

    def printer_for_job(self, job):
        """Print job routing (spec §7.3): pick (printer, tray_name) by mode."""
        with self.lock:
            if self.mode == "multi":
                if job.get("color_mode") == "color":
                    return self.printer_colour, self.tray_colour
                return self.printer_bw, self.tray_bw
            return self.printer_single, self.tray_single


SESSION = Session()


# ---------------------------------------------------------------------------
# Printer helpers (ported from the prototype)
# ---------------------------------------------------------------------------
def list_printers():
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags, None, 1)]


def list_trays(printer):
    """[(label, bin_id), ...] for a printer; [] if the driver reports none."""
    try:
        names = win32print.DeviceCapabilities(printer, "", win32con.DC_BINNAMES) or []
        ids = win32print.DeviceCapabilities(printer, "", win32con.DC_BINS) or []
    except Exception:
        return []
    trays = []
    for i, name in enumerate(names):
        bin_id = ids[i] if i < len(ids) else None
        if bin_id is not None:
            trays.append((str(name).strip(), int(bin_id)))
    return trays


def tray_bin_id(printer, tray_name):
    """Resolve a stored tray NAME to the driver's bin id (None = auto)."""
    if not tray_name or tray_name == AUTO_TRAY_LABEL:
        return None
    for name, bin_id in list_trays(printer):
        if name == tray_name:
            return bin_id
    return None


def printer_is_ready(printer):
    bad = {
        win32print.PRINTER_STATUS_OFFLINE: "offline",
        win32print.PRINTER_STATUS_ERROR: "error",
        win32print.PRINTER_STATUS_PAPER_JAM: "paper jam",
        win32print.PRINTER_STATUS_PAPER_OUT: "out of paper",
        win32print.PRINTER_STATUS_PAUSED: "paused",
        win32print.PRINTER_STATUS_NOT_AVAILABLE: "not available",
        win32print.PRINTER_STATUS_NO_TONER: "no toner",
        win32print.PRINTER_STATUS_DOOR_OPEN: "door open",
    }
    try:
        h = win32print.OpenPrinter(printer)
        try:
            info = win32print.GetPrinter(h, 2)
        finally:
            win32print.ClosePrinter(h)
    except Exception as e:
        return False, f"cannot open printer ({e})"
    status = info.get("Status", 0)
    for bit, reason in bad.items():
        if status & bit:
            return False, reason
    return True, "ready"


def configure_printer_settings(printer, job, tray_name):
    h = win32print.OpenPrinter(printer, {"DesiredAccess": win32print.PRINTER_ALL_ACCESS})
    props = win32print.GetPrinter(h, 2)
    devmode = props["pDevMode"]

    devmode.Color = 2 if job.get("color_mode") == "color" else 1
    devmode.Duplex = 2 if job.get("sides") == "double" else 1
    devmode.Orientation = 2 if job.get("orientation") == "landscape" else 1
    paper_map = {"A4": 9, "A3": 8, "Letter": 1}
    if job.get("paper_size") in paper_map:
        devmode.PaperSize = paper_map[job["paper_size"]]

    bin_id = tray_bin_id(printer, tray_name)
    if bin_id is not None:
        devmode.DefaultSource = bin_id

    props["pDevMode"] = devmode
    win32print.SetPrinter(h, 2, props, 0)
    win32print.ClosePrinter(h)


def _get_job_ids(printer):
    h = win32print.OpenPrinter(printer)
    try:
        jobs = win32print.EnumJobs(h, 0, 999, 1)
        return {j["JobId"] for j in jobs}, {j["JobId"]: j for j in jobs}
    finally:
        win32print.ClosePrinter(h)


def _find_acrobat():
    candidates = [
        r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
        r"C:\Program Files (x86)\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
        r"C:\Program Files (x86)\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
        r"C:\Program Files\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
        r"C:\Program Files (x86)\Adobe\Reader 11.0\Reader\AcroRd32.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _submit_print(file_path, printer):
    acro = _find_acrobat()
    if acro:
        subprocess.Popen([acro, "/t", file_path, printer],
                         creationflags=subprocess.CREATE_NO_WINDOW)
        return "acrobat"
    win32api.ShellExecute(0, "print", file_path, f'/d:"{printer}"', ".", 0)
    return "shell"


def print_file(file_path, job, printer, tray_name):
    """Print with confirmation on an explicit printer. Returns (status, msg)."""
    if not printer:
        return "failed", "no printer configured for this job type"

    ok, reason = printer_is_ready(printer)
    if not ok:
        return "failed", f"printer not ready: {reason}"

    try:
        configure_printer_settings(printer, job, tray_name)
    except Exception as e:
        return "failed", f"could not apply settings: {e}"

    before, _ = _get_job_ids(printer)
    try:
        _submit_print(file_path, printer)
    except Exception as e:
        return "failed", f"submit error: {e}"

    new_id = None
    for _ in range(60):  # up to ~30s for the spooler to register the job
        time.sleep(0.5)
        after, _ = _get_job_ids(printer)
        added = after - before
        if added:
            new_id = max(added)
            break
    if new_id is None:
        # The page may well have printed: a small job can enter AND leave the
        # spooler between two polls, so "never seen" is NOT proof of failure.
        # Treat it as done-but-unverified rather than retrying, because a
        # retry loop here reprints the document endlessly (and the customer
        # already paid for one copy).
        return "printed", ("submitted; spooler job completed too quickly to "
                           "track (not re-sent to avoid duplicates)")

    deadline = time.time() + PRINT_CONFIRM_TIMEOUT
    error_bits = {
        win32print.JOB_STATUS_ERROR: "job error",
        win32print.JOB_STATUS_OFFLINE: "printer offline",
        win32print.JOB_STATUS_PAPEROUT: "out of paper",
        win32print.JOB_STATUS_BLOCKED_DEVQ: "queue blocked",
        win32print.JOB_STATUS_DELETED: "job deleted",
    }
    while time.time() < deadline:
        ids, jobmap = _get_job_ids(printer)
        if new_id not in ids:
            return "printed", "completed"
        jstatus = jobmap[new_id].get("Status", 0)
        for bit, reason in error_bits.items():
            if jstatus & bit:
                return "failed", reason
        time.sleep(1)
    return "failed", "timed out waiting for the printer"


# ---------------------------------------------------------------------------
# Server API
# ---------------------------------------------------------------------------
def api_login(base_url, login_id, password):
    r = requests.post(f"{base_url}/worker/api/login",
                      json={"login_id": login_id, "password": password},
                      timeout=30)
    if r.status_code == 401:
        raise ValueError("Invalid login ID or password.")
    if r.status_code == 403:
        raise ValueError(r.json().get("error", "Subscription inactive."))
    r.raise_for_status()
    return r.json()


def api_fetch_jobs():
    r = requests.get(f"{SESSION.base_url}/worker/api/jobs",
                     params={"token": SESSION.token}, timeout=30)
    if r.status_code == 403:
        raise PermissionError(r.json().get("error", "access denied"))
    r.raise_for_status()
    return r.json().get("jobs", [])


def api_resume(base_url, token):
    """Validate a saved token and fetch the current session context
    (shop name + printer config) without re-entering the password."""
    r = requests.get(f"{base_url}/worker/api/jobs",
                     params={"token": token}, timeout=15)
    if r.status_code != 200:
        return None
    return r.json()


def api_mark_printed(job_id):
    r = requests.post(f"{SESSION.base_url}/worker/api/jobs/{job_id}/printed",
                      params={"token": SESSION.token}, timeout=30)
    r.raise_for_status()


def api_release_job(job_id, reason=""):
    """Hand a job back to the server because it could NOT be printed."""
    r = requests.post(f"{SESSION.base_url}/worker/api/jobs/{job_id}/release",
                      params={"token": SESSION.token},
                      json={"reason": reason[:200]}, timeout=30)
    r.raise_for_status()


def api_save_printer_config():
    with SESSION.lock:
        payload = {
            "printer_mode": SESSION.mode,
            "printer_single": SESSION.printer_single or "",
            "tray_single": SESSION.tray_single or "",
            "printer_bw": SESSION.printer_bw or "",
            "tray_bw": SESSION.tray_bw or "",
            "printer_colour": SESSION.printer_colour or "",
            "tray_colour": SESSION.tray_colour or "",
        }
    r = requests.post(f"{SESSION.base_url}/worker/api/printer-config",
                      params={"token": SESSION.token}, json=payload, timeout=30)
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Worker thread (ported: submit-once guard, 404 handling, retry on failure)
# ---------------------------------------------------------------------------
class WorkerThread(threading.Thread):
    def __init__(self, ui_queue, stop_event):
        super().__init__(daemon=True)
        self.ui_queue = ui_queue
        self.stop_event = stop_event
        # Jobs already sent to a printer this session — never sent twice.
        self.submitted = set()
        # Printed but the server didn't acknowledge; retried, never reprinted.
        self.unsynced = set()
        # job_id -> time of a genuine printer failure (for the retry cooldown).
        self.failed_at = {}
        # job_id -> how long to wait before retrying that particular job.
        self.cooldown_for = {}

    def log(self, msg):
        self.ui_queue.put(("log", msg))

    def record(self, job, status):
        self.ui_queue.put(("record", (job, status)))

    def _release(self, job_id, reason):
        """Tell the server we did not print this job, so it stays queued."""
        try:
            api_release_job(job_id, reason)
        except Exception as e:
            # Not fatal: the server also frees claims that go stale.
            self.log(f"⚠️ Could not requeue job {job_id} on the server ({e}); "
                     f"it will return automatically in a few minutes.")

    def _resync_printed(self):
        """Re-tell the server about jobs that printed but whose confirmation
        didn't get through. Never reprints — only re-sends the ack."""
        for job_id in list(self.unsynced):
            try:
                api_mark_printed(job_id)
                self.unsynced.discard(job_id)
                self.log(f"✅ Job {job_id} confirmed with the server.")
            except Exception:
                pass  # try again on the next cycle

    def run(self):
        self.log(f"Connected to {SESSION.base_url} as {SESSION.shop_name}")
        self.log("Watching for jobs...")
        while not self.stop_event.is_set():
            self._resync_printed()
            try:
                jobs = api_fetch_jobs()
            except PermissionError as e:
                self.log(f"⛔ {e} — printing paused. Renew the subscription "
                         f"or contact the admin.")
                self._sleep(30)
                continue
            except Exception as e:
                self.log(f"⚠️ Could not fetch jobs: {e}")
                self._sleep()
                continue

            for job in jobs:
                if self.stop_event.is_set():
                    break
                job_id = job["id"]
                # Already sent to a printer in this session — never again.
                if job_id in self.submitted or job_id in self.unsynced:
                    continue
                # A job that FAILED at the printer waits out its cooldown.
                # (Jobs merely waiting for the printer to come back online
                # use a much shorter wait — see WAIT_COOLDOWN.)
                failed = self.failed_at.get(job_id)
                cooldown = self.cooldown_for.get(job_id, RETRY_COOLDOWN)
                if failed and time.time() - failed < cooldown:
                    continue
                self._handle_job(job)
            self._sleep()
        self.log("Stopped.")

    def _handle_job(self, job):
        job_id = job["id"]
        filename = job.get("original_filename", "?")
        printer, tray = SESSION.printer_for_job(job)

        # Check the printer BEFORE downloading/printing: if it's off or has
        # no paper, hand the job straight back so it stays visibly queued
        # (and is picked up the moment the printer is switched on).
        if not printer:
            self.failed_at[job_id] = time.time()
            self.cooldown_for[job_id] = WAIT_COOLDOWN
            self._release(job_id, "no printer configured for this job type")
            self.log(f"⚠️ Job {job_id}: no printer configured for "
                     f"{job.get('color_mode')} — still queued.")
            self.record(job, "waiting: no printer set")
            return
        ready, reason = printer_is_ready(printer)
        if not ready:
            self.failed_at[job_id] = time.time()
            self.cooldown_for[job_id] = WAIT_COOLDOWN
            self._release(job_id, f"printer not ready: {reason}")
            self.log(f"⏸ Job {job_id}: {printer} is {reason} — still queued, "
                     f"will print when the printer is ready.")
            self.record(job, f"waiting: printer {reason}")
            return

        self.log(f"📥 Job {job_id}: {filename} "
                 f"({job.get('color_mode')}) → {printer}")
        self.record(job, "printing")
        self.submitted.add(job_id)

        try:
            resp = requests.get(job["file_url"], stream=True, timeout=60)
            resp.raise_for_status()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code == 404:
                self.log(f"⚠️ Job {job_id}: file missing (404) — skipping.")
                try:
                    api_mark_printed(job_id)
                except Exception:
                    pass
                self.record(job, "skipped (missing file)")
            else:
                self.submitted.discard(job_id)
                self._release(job_id, f"download failed ({code})")
                self.log(f"❌ Job {job_id}: download failed ({code}). Still queued.")
                self.record(job, "failed")
            return
        except Exception as e:
            self.submitted.discard(job_id)
            self._release(job_id, f"download error: {e}")
            self.log(f"❌ Job {job_id}: download error: {e}. Still queued.")
            self.record(job, "failed")
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name

        try:
            status, message = print_file(tmp_path, job, printer, tray)
        except Exception as e:
            self.submitted.discard(job_id)
            self._release(job_id, f"print error: {e}")
            self.log(f"❌ Job {job_id}: print error: {e}. Still queued.")
            self.record(job, "failed")
            return
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        if status == "printed":
            # Keep the job in `submitted` FOREVER (this session): paper has
            # come out, so it must never be re-sent even if the server call
            # below fails. Unsynced jobs are retried by _resync_printed().
            try:
                api_mark_printed(job_id)
                self.log(f"✅ Job {job_id} printed ({message}).")
                self.record(job, "printed")
            except Exception as e:
                self.unsynced.add(job_id)
                self.log(f"⚠️ Job {job_id} printed but the server was not "
                         f"updated ({e}) — will keep retrying, not reprinting.")
                self.record(job, "printed (syncing…)")
        else:
            # A genuine printer failure (offline, out of paper, ...). Nothing
            # came out, so hand the job BACK to the server: it must stay in
            # the queue and be retried — by this worker after a cooldown, or
            # by another/restarted worker immediately.
            self.failed_at[job_id] = time.time()
            self.submitted.discard(job_id)
            self._release(job_id, message)
            self.log(f"❌ Job {job_id} NOT printed: {message}. "
                     f"Still queued — retrying in {RETRY_COOLDOWN}s.")
            self.record(job, f"failed: {message}")

    def _sleep(self, seconds=POLL_SECONDS):
        for _ in range(int(seconds * 2)):
            if self.stop_event.is_set():
                return
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PrintHub Worker")
        self.geometry("880x640")

        self.ui_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None

        self.login_frame = ttk.Frame(self, padding=30)
        self.main_frame = ttk.Frame(self)
        self._build_login()
        self._build_main()

        self.after(200, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Try to resume the previous session (saved token) — the shop PC
        # reboots shouldn't require retyping anything.
        cfg = load_local_config()
        if cfg.get("server"):
            self.server_var.set(cfg["server"])
        if cfg.get("login_id"):
            self.login_var.set(cfg["login_id"])
        resumed = False
        if cfg.get("server") and cfg.get("token"):
            try:
                data = api_resume(cfg["server"], cfg["token"])
            except Exception:
                data = None
            if data is not None:
                data["token"] = cfg["token"]
                data.setdefault("shop_name", cfg.get("shop_name", ""))
                self._enter_main(cfg["server"], data, resumed=True)
                resumed = True
        if not resumed:
            self.login_frame.pack(fill="both", expand=True)

    # ---------------- login screen ----------------
    def _build_login(self):
        f = self.login_frame
        ttk.Label(f, text="PrintHub Worker — Vendor login",
                  font=("Segoe UI", 14, "bold")).grid(row=0, column=0,
                                                      columnspan=2, pady=(0, 18))
        ttk.Label(f, text="Server URL:").grid(row=1, column=0, sticky="e", pady=4)
        self.server_var = tk.StringVar(value=DEFAULT_SERVER)
        ttk.Entry(f, textvariable=self.server_var, width=38).grid(row=1, column=1, pady=4)
        ttk.Label(f, text="Login ID:").grid(row=2, column=0, sticky="e", pady=4)
        self.login_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.login_var, width=38).grid(row=2, column=1, pady=4)
        ttk.Label(f, text="Password:").grid(row=3, column=0, sticky="e", pady=4)
        self.pass_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.pass_var, width=38, show="•").grid(row=3, column=1, pady=4)
        self.login_btn = ttk.Button(f, text="Log in", command=self._do_login)
        self.login_btn.grid(row=4, column=1, sticky="e", pady=14)

    def _do_login(self):
        base = self.server_var.get().strip().rstrip("/")
        try:
            data = api_login(base, self.login_var.get().strip(), self.pass_var.get())
        except ValueError as e:
            messagebox.showerror("Login failed", str(e))
            return
        except Exception as e:
            messagebox.showerror("Login failed", f"Could not reach the server:\n{e}")
            return

        # Remember the session so the next launch reconnects automatically.
        save_local_config({"server": base,
                           "login_id": self.login_var.get().strip(),
                           "token": data["token"],
                           "shop_name": data.get("shop_name", "")})
        self._enter_main(base, data)

    def _enter_main(self, base, data, resumed=False):
        """Shared by fresh login and token resume."""
        SESSION.base_url = base
        SESSION.token = data["token"]
        SESSION.shop_name = data.get("shop_name", "")
        cfg = data.get("printer_config") or {}
        with SESSION.lock:
            SESSION.mode = cfg.get("printer_mode") or "single"
            SESSION.printer_single = cfg.get("printer_single")
            SESSION.tray_single = cfg.get("tray_single")
            SESSION.printer_bw = cfg.get("printer_bw")
            SESSION.tray_bw = cfg.get("tray_bw")
            SESSION.printer_colour = cfg.get("printer_colour")
            SESSION.tray_colour = cfg.get("tray_colour")

        self.title(f"PrintHub Worker — {SESSION.shop_name}")
        self.login_frame.pack_forget()
        self.main_frame.pack(fill="both", expand=True)
        self._load_printer_ui()
        self.status_lbl.config(text="●  stopped (click Start)", foreground="red")
        if resumed:
            self._append_log(f"Reconnected as {SESSION.shop_name} "
                             f"(saved session). Click Start.")
        else:
            self._append_log(f"Logged in as {SESSION.shop_name}. "
                             f"Configure printers, then click Start.")
        if data.get("status") == "grace":
            self._append_log("⚠️ Subscription is in the GRACE period — renew "
                             "soon to avoid suspension.")

    def _do_logout(self):
        """Forget the saved session and return to the login screen."""
        self.stop()
        cfg = load_local_config()
        cfg.pop("token", None)
        save_local_config(cfg)
        SESSION.token = None
        self.title("PrintHub Worker")
        self.main_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)

    # ---------------- main screen ----------------
    def _build_main(self):
        f = self.main_frame
        top = ttk.Frame(f, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="PrintHub Worker",
                  font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status_lbl = ttk.Label(top, text="●  stopped", foreground="red")
        self.status_lbl.pack(side="left", padx=15)
        self.start_btn = ttk.Button(top, text="Start", command=self.toggle)
        self.start_btn.pack(side="right")
        ttk.Button(top, text="Log out",
                   command=self._do_logout).pack(side="right", padx=8)

        # --- Printer configuration (spec §7) ---
        cfgf = ttk.LabelFrame(f, text="Printer configuration", padding=10)
        cfgf.pack(fill="x", padx=10, pady=(0, 8))

        self.mode_var = tk.StringVar(value="single")
        mrow = ttk.Frame(cfgf)
        mrow.pack(fill="x")
        ttk.Radiobutton(mrow, text="Single printer", value="single",
                        variable=self.mode_var,
                        command=self._on_mode_change).pack(side="left")
        ttk.Radiobutton(mrow, text="Multiple printers (B/W + Colour, max 2)",
                        value="multi", variable=self.mode_var,
                        command=self._on_mode_change).pack(side="left", padx=12)
        ttk.Button(mrow, text="Refresh printers",
                   command=self._load_printer_ui).pack(side="right")
        ttk.Button(mrow, text="Save / Change Printer",
                   command=self._save_config).pack(side="right", padx=8)

        # single row
        self.single_row = ttk.Frame(cfgf)
        self.sp_var, self.st_var = tk.StringVar(), tk.StringVar()
        self._printer_row(self.single_row, "Printer:", self.sp_var, self.st_var)

        # multi rows
        self.bw_row = ttk.Frame(cfgf)
        self.bp_var, self.bt_var = tk.StringVar(), tk.StringVar()
        self._printer_row(self.bw_row, "B/W printer:", self.bp_var, self.bt_var)
        self.col_row = ttk.Frame(cfgf)
        self.cp_var, self.ct_var = tk.StringVar(), tk.StringVar()
        self._printer_row(self.col_row, "Colour printer:", self.cp_var, self.ct_var)

        # records + log (ported layout)
        cols = ("id", "file", "color", "copies", "status", "time")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=10)
        widths = {"id": 50, "file": 320, "color": 70, "copies": 60,
                  "status": 120, "time": 110}
        for c in cols:
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        ttk.Label(f, text="Activity log", padding=(10, 0)).pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(f, height=8, state="disabled",
                                                 font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=False, padx=10, pady=(0, 10))

    def _printer_row(self, row, label, printer_var, tray_var):
        ttk.Label(row, text=label, width=14).pack(side="left")
        pc = ttk.Combobox(row, textvariable=printer_var, state="readonly", width=42)
        pc.pack(side="left", padx=6)
        ttk.Label(row, text="Tray:").pack(side="left", padx=(10, 0))
        tc = ttk.Combobox(row, textvariable=tray_var, state="readonly", width=26)
        tc.pack(side="left", padx=6)
        pc.bind("<<ComboboxSelected>>",
                lambda e: self._fill_trays(printer_var.get(), tc, tray_var))
        row.printer_combo, row.tray_combo = pc, tc

    def _fill_trays(self, printer, tray_combo, tray_var, keep=None):
        trays = list_trays(printer) if printer else []
        labels = [AUTO_TRAY_LABEL] + [t[0] for t in trays]
        tray_combo["values"] = labels
        tray_var.set(keep if keep in labels else AUTO_TRAY_LABEL)

    def _on_mode_change(self):
        if self.mode_var.get() == "multi":
            self.single_row.pack_forget()
            self.bw_row.pack(fill="x", pady=3)
            self.col_row.pack(fill="x", pady=3)
        else:
            self.bw_row.pack_forget()
            self.col_row.pack_forget()
            self.single_row.pack(fill="x", pady=3)

    def _load_printer_ui(self):
        """Populate dropdowns from installed printers + the saved config."""
        try:
            printers = list_printers()
        except Exception as e:
            printers = []
            self._append_log(f"⚠️ Could not list printers: {e}")
        try:
            default = win32print.GetDefaultPrinter()
        except Exception:
            default = printers[0] if printers else ""

        with SESSION.lock:
            mode = SESSION.mode
            saved = {"single": (SESSION.printer_single, SESSION.tray_single),
                     "bw": (SESSION.printer_bw, SESSION.tray_bw),
                     "colour": (SESSION.printer_colour, SESSION.tray_colour)}

        for row, var, tvar, key in ((self.single_row, self.sp_var, self.st_var, "single"),
                                    (self.bw_row, self.bp_var, self.bt_var, "bw"),
                                    (self.col_row, self.cp_var, self.ct_var, "colour")):
            row.printer_combo["values"] = printers
            saved_p, saved_t = saved[key]
            chosen = saved_p if saved_p in printers else default
            var.set(chosen or "")
            self._fill_trays(chosen, row.tray_combo, tvar, keep=saved_t)

        self.mode_var.set(mode)
        self._on_mode_change()

    def _save_config(self):
        """'Change Printer' (spec §7.5) — apply + sync to the server."""
        def tray_or_none(v):
            return None if v in ("", AUTO_TRAY_LABEL) else v
        with SESSION.lock:
            SESSION.mode = self.mode_var.get()
            SESSION.printer_single = self.sp_var.get() or None
            SESSION.tray_single = tray_or_none(self.st_var.get())
            SESSION.printer_bw = self.bp_var.get() or None
            SESSION.tray_bw = tray_or_none(self.bt_var.get())
            SESSION.printer_colour = self.cp_var.get() or None
            SESSION.tray_colour = tray_or_none(self.ct_var.get())
        try:
            api_save_printer_config()
            self._append_log("🖨 Printer configuration saved and synced to server.")
        except Exception as e:
            self._append_log(f"⚠️ Config saved locally but sync failed: {e}")
        if SESSION.mode == "multi":
            self._append_log(f"   B/W → {SESSION.printer_bw or '—'} "
                             f"(tray: {SESSION.tray_bw or 'auto'})")
            self._append_log(f"   Colour → {SESSION.printer_colour or '—'} "
                             f"(tray: {SESSION.tray_colour or 'auto'})")
        else:
            self._append_log(f"   Printer → {SESSION.printer_single or '—'} "
                             f"(tray: {SESSION.tray_single or 'auto'})")

    # ---------------- start/stop + queue ----------------
    def start(self):
        self.stop_event.clear()
        self.worker = WorkerThread(self.ui_queue, self.stop_event)
        self.worker.start()
        self.status_lbl.config(text="●  running", foreground="green")
        self.start_btn.config(text="Stop")

    def stop(self):
        self.stop_event.set()
        self.status_lbl.config(text="●  stopped", foreground="red")
        self.start_btn.config(text="Start")

    def toggle(self):
        if self.stop_event.is_set() or self.worker is None or not self.worker.is_alive():
            self.start()
        else:
            self.stop()

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "record":
                    self._add_record(*payload)
        except queue.Empty:
            pass
        self.after(200, self._drain_queue)

    def _append_log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _add_record(self, job, status):
        ts = time.strftime("%H:%M:%S")
        self.tree.insert("", 0, values=(
            job.get("id"), job.get("original_filename", ""),
            job.get("color_mode", ""), job.get("copies", 1), status, ts,
        ))

    def _on_close(self):
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
