"""Turn any document a customer uploads into a PDF the shop can print.

  * PDF                      -> used as is
  * Word / Excel / PowerPoint / OpenDocument / RTF / text / CSV
                             -> converted by LibreOffice (headless)
  * images (JPG, PNG, HEIC, ...) -> one A4 page each (multi-page TIFF:
                                   one page per frame)

Everything downstream (page count, page ranges, price, preview, printing)
then works on the PDF, so every file type behaves the same.

The server needs LibreOffice for office files:
    sudo apt-get install libreoffice-writer-nogui libreoffice-calc-nogui \
        libreoffice-impress-nogui
(deploy/setup.sh and deploy/redeploy.sh install it). Without it, office
files are refused with a clear message and PDFs/images still work.
"""
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageOps

try:                                   # iPhone photos (HEIC) — optional
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

OFFICE_EXTS = {
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".odt", ".rtf", ".txt",
    ".xls", ".xlsx", ".xlsm", ".ods", ".csv",
    ".ppt", ".pptx", ".pps", ".ppsx", ".odp",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
              ".tif", ".tiff", ".heic", ".heif"}

CONVERT_TIMEOUT = 90          # seconds; gunicorn's own timeout is 120
CACHE_SECONDS = 2 * 3600      # a converted file is reused for the preview + order
_WORK = Path(tempfile.gettempdir()) / "printhub-convert"


def kind_of(filename, data):
    """'pdf' | 'office' | 'image' | None (not printable)."""
    ext = os.path.splitext(filename or "")[1].lower()
    if data[:5].startswith(b"%PDF"):
        return "pdf"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in IMAGE_EXTS:
        return "image"
    try:                       # an image with a missing/odd extension
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        return "image"
    except Exception:
        return None


def to_pdf(data, filename):
    """PDF bytes for any supported upload. Raises ValueError with a message
    the customer can act on."""
    name = filename or "file"
    kind = kind_of(name, data)
    if kind == "pdf":
        return data
    if kind == "image":
        return image_to_pdf(data, name)
    if kind == "office":
        return office_to_pdf(data, os.path.splitext(name)[1].lower())
    raise ValueError(f"'{name}' is not a file we can print. Upload a PDF, "
                     f"Word, Excel, PowerPoint, text or image file.")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def image_to_pdf(data, name="image"):
    """Each image (or TIFF frame) on its own A4 sheet, landscape for a wide
    image, so the picture fills the paper."""
    DPI = 200
    A4_SHORT, A4_LONG = int(8.27 * DPI), int(11.69 * DPI)
    MARGIN = int(0.3 * DPI)
    try:
        src = Image.open(io.BytesIO(data))
    except Exception:
        raise ValueError(f"could not open the image '{name}'")
    pages = []
    try:
        for i in range(getattr(src, "n_frames", 1)):
            src.seek(i)
            img = ImageOps.exif_transpose(src.copy())     # phone rotation
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                flat = Image.new("RGB", img.size, "white")
                flat.paste(img, mask=img.split()[-1])
                img = flat
            else:
                img = img.convert("RGB")
            sheet_w, sheet_h = ((A4_LONG, A4_SHORT) if img.width > img.height
                                else (A4_SHORT, A4_LONG))
            k = min((sheet_w - 2 * MARGIN) / img.width,
                    (sheet_h - 2 * MARGIN) / img.height)
            img = img.resize((max(1, int(img.width * k)),
                              max(1, int(img.height * k))), Image.LANCZOS)
            page = Image.new("RGB", (sheet_w, sheet_h), "white")
            page.paste(img, ((sheet_w - img.width) // 2,
                             (sheet_h - img.height) // 2))
            pages.append(page)
            if i >= 99:                     # same cap as a PDF
                break
            if getattr(src, "format", "") == "GIF":
                break                       # an animated GIF prints once
    finally:
        src.close()
    out = io.BytesIO()
    pages[0].save(out, format="PDF", resolution=DPI,
                  save_all=True, append_images=pages[1:])
    return out.getvalue()


# ---------------------------------------------------------------------------
# Office documents (LibreOffice)
# ---------------------------------------------------------------------------
def soffice_path():
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for p in (r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if os.path.exists(p):
            return p
    return None


class _Lock:
    """One conversion at a time across all gunicorn workers: LibreOffice
    needs a few hundred MB, and two at once would push a small server into
    swap. No-op where fcntl is missing (Windows dev)."""

    def __enter__(self):
        _WORK.mkdir(parents=True, exist_ok=True)
        self.fh = open(_WORK / ".lock", "w")
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, *exc):
        self.fh.close()          # closing releases the flock


def _prune_cache(now):
    for p in _WORK.glob("*.pdf"):
        try:
            if now - p.stat().st_mtime > CACHE_SECONDS:
                p.unlink()
        except OSError:
            pass


def office_to_pdf(data, ext):
    exe = soffice_path()
    if not exe:
        raise ValueError("This shop cannot print Word/Excel/PowerPoint files "
                         "yet. Please save it as a PDF and upload that.")
    digest = hashlib.sha256(data).hexdigest()
    cached = _WORK / f"{digest}.pdf"
    if cached.exists():
        os.utime(cached)                         # keep it while in use
        return cached.read_bytes()

    with _Lock():
        if cached.exists():                      # converted while we waited
            return cached.read_bytes()
        job = Path(tempfile.mkdtemp(prefix="job-", dir=_WORK))
        try:
            src = job / f"document{ext}"
            src.write_bytes(data)
            profile = (_WORK / "lo-profile").resolve()
            cmd = [exe, f"-env:UserInstallation={profile.as_uri()}",
                   "--headless", "--norestore", "--nologo", "--nodefault",
                   "--convert-to", "pdf", "--outdir", str(job), str(src)]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=CONVERT_TIMEOUT,
                               check=False)
            except subprocess.TimeoutExpired:
                raise ValueError("this file took too long to convert. Please "
                                 "save it as a PDF and upload that.")
            out = job / "document.pdf"
            if not out.exists() or out.stat().st_size == 0:
                raise ValueError("could not open this file (is it "
                                 "password-protected or damaged?). Please "
                                 "save it as a PDF and upload that.")
            pdf = out.read_bytes()
            tmp = cached.with_suffix(".part")
            tmp.write_bytes(pdf)
            os.replace(tmp, cached)
            _prune_cache(time.time())
            return pdf
        finally:
            shutil.rmtree(job, ignore_errors=True)
