#!/usr/bin/env python3
"""Screen captures for blocked work (stdlib only, macOS and Windows).

The Discord bridge asks for a capture of the device where something is blocked. On its own device it captures directly;
another device is asked with a request file in Queue/captures, which that device's runner or bridge answers. Results
wait outside the share (~/.switchboard/captures) when local. Files in Queue/captures exist only in transit: the bridge
deletes them on upload and anything older than 15 minutes is removed.

Capture fails with a short reason on a locked screen, Windows' secure desktop, a disconnected session or a Mac without
Screen Recording permission; the bridge then posts that as a text note.

  screencap.py test     capture this screen to the captures folder and print the size"""
import json, os, re, shutil, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import hostname, queue_root
import fleet

MAX_BYTES = 9_500_000      # Discord's limit here is 10 MB, and the upload adds its form around the file
SIZES = ((1920, 80), (1600, 70), (1280, 60), (960, 50), (640, 40))  # width, JPEG quality: tried in order until it fits
TTL = 900                  # seconds a capture or request may exist before cleanup removes it
LOCAL = Path.home() / ".switchboard" / "captures"

class CaptureError(RuntimeError): pass

def shared(): return queue_root() / "captures"

WINDOWS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class Dpi { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }'
[void][Dpi]::SetProcessDPIAware()
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
if ($b.Width -le 0 -or $b.Height -le 0) { exit 3 }
$bmp = [System.Drawing.Bitmap]::new($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$first = $bmp.GetPixel(0, 0).ToArgb(); $same = $true
foreach ($i in 1..23) { foreach ($j in 1..23) {
  if ($bmp.GetPixel([int]($b.Width * $i / 24), [int]($b.Height * $j / 24)).ToArgb() -ne $first) { $same = $false; break }
}; if (-not $same) { break } }
if ($same) { exit 3 }
$s = [Math]::Min(1.0, [double]@@WIDTH@@ / $b.Width)
$out = [System.Drawing.Bitmap]::new($bmp, [Math]::Max(1, [int]($b.Width * $s)), [Math]::Max(1, [int]($b.Height * $s)))
$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$ep = [System.Drawing.Imaging.EncoderParameters]::new(1)
$ep.Param[0] = [System.Drawing.Imaging.EncoderParameter]::new([System.Drawing.Imaging.Encoder]::Quality, [long]@@QUALITY@@)
$out.Save('@@PATH@@', $codec, $ep)
"""

def grab_windows(dest, width, quality):
    script = WINDOWS.replace("@@WIDTH@@", str(width)).replace("@@QUALITY@@", str(quality)).replace("@@PATH@@", str(dest).replace("'", "''"))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=45, creationflags=0x08000000)  # CREATE_NO_WINDOW: nothing appears in the picture
    except (OSError, subprocess.TimeoutExpired) as e: raise CaptureError(f"capture did not run ({type(e).__name__})")
    if r.returncode: raise CaptureError("the screen is not readable (locked, a secure desktop prompt, or no display attached)")

def grab_mac(dest, width, quality):
    import ctypes
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        allowed = cg.CGPreflightScreenCaptureAccess()
    except (OSError, AttributeError): allowed = True  # cannot tell on this macOS; screencapture decides
    if not allowed: raise CaptureError("Screen Recording permission is missing for this process's Python")
    try:
        io = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d1"], capture_output=True, text=True, errors="replace", timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired): io = ""
    if re.search(r'"(?:CGSSessionScreenIsLocked|IOConsoleLocked)"\s*=\s*(?:Yes|1)', io): raise CaptureError("the screen is locked")
    raw = dest.with_suffix(".raw.jpg")
    try:
        r = subprocess.run(["/usr/sbin/screencapture", "-x", "-t", "jpg", str(raw)], capture_output=True, text=True, errors="replace", timeout=45)
        if r.returncode or not raw.is_file(): raise CaptureError("screencapture could not read the display (locked or no permission)")
        r = subprocess.run(["/usr/bin/sips", "-Z", str(width), "-s", "format", "jpeg", "-s", "formatOptions", str(quality), str(raw), "--out", str(dest)],
                           capture_output=True, text=True, errors="replace", timeout=45)
        if r.returncode: raise CaptureError("could not shrink the capture")
    except (OSError, subprocess.TimeoutExpired) as e: raise CaptureError(f"capture did not run ({type(e).__name__})")
    finally:
        try: raw.unlink()
        except OSError: pass

def capture(dest):
    """Write a capture of this screen to dest, under the attachment limit, or raise CaptureError with a short reason."""
    dest = Path(dest); dest.parent.mkdir(parents=True, exist_ok=True)
    grab = grab_windows if os.name == "nt" else grab_mac if sys.platform == "darwin" else None
    if not grab: raise CaptureError(f"no capture method on {sys.platform}")
    for width, quality in SIZES:
        grab(dest, width, quality)
        if dest.is_file() and 0 < dest.stat().st_size <= MAX_BYTES: return dest
    try: dest.unlink()
    except OSError: pass
    raise CaptureError("the capture stayed over the attachment limit at every size")

def valid(sid): return bool(re.fullmatch(r"[0-9a-f]{8,40}", str(sid or "")))

def fulfil(sid, folder):
    """Capture for one request id into folder: <sid>.jpg, or <sid>.fail.json with the reason."""
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True); work = LOCAL / f"{sid}.work.jpg"
    try:
        capture(work)
        if folder.resolve() == LOCAL.resolve(): os.replace(work, folder / f"{sid}.jpg")
        else:  # into the share under a temporary name, so Syncthing never carries half a picture
            tmp = folder / f".{sid}.jpg.tmp"; shutil.copyfile(work, tmp); os.replace(tmp, folder / f"{sid}.jpg")
    except Exception as e:
        reason = str(e)[:200] if isinstance(e, CaptureError) else f"{type(e).__name__} during capture"
        fleet._write(folder / f"{sid}.fail.json", {"reason": reason})
    finally:
        try: work.unlink()
        except OSError: pass

def ask(sid, host):
    """Ask another device for a capture."""
    fleet._write(shared() / f"{sid}.req.json", {"id": sid, "host": host, "at": time.time()})

def serve_once():
    """Answer capture requests addressed to this device."""
    d, me = shared(), hostname().lower()
    for p in sorted(d.glob("*.req.json")) if d.is_dir() else []:
        try: r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        if str(r.get("host", "")).lower() != me: continue
        try: p.unlink()
        except OSError: continue  # another process here took it
        if valid(r.get("id")) and time.time() - float(r.get("at", 0)) < TTL: fulfil(r["id"], d)

def result(sid):
    """(jpeg path, None), (None, reason), or (None, None) while nothing has arrived."""
    for d in (LOCAL, shared()):
        if (d / f"{sid}.jpg").is_file(): return d / f"{sid}.jpg", None
        f = d / f"{sid}.fail.json"
        if f.is_file():
            try: return None, str(json.loads(f.read_text(encoding="utf-8")).get("reason") or "capture failed")
            except (OSError, ValueError): return None, None
    return None, None

def forget(sid):
    for d in (LOCAL, shared()):
        for name in (f"{sid}.jpg", f"{sid}.fail.json", f"{sid}.req.json"):
            try: (d / name).unlink()
            except OSError: pass

def clean():
    """Nothing lingers: remove whatever is older than TTL in both folders."""
    for d in (LOCAL, shared()):
        for p in d.glob("*") if d.is_dir() else []:
            try:
                if time.time() - p.stat().st_mtime > TTL: p.unlink()
            except OSError: pass

def start_service(say=None, every=5):
    """On a background thread, one process per device answers requests. The others retry so one can take over."""
    def loop():
        while not fleet.single_instance("screencap-service"): time.sleep(20)
        while True:
            try: serve_once(); clean()
            except Exception as e:
                if say: say(f"screencap error: {type(e).__name__}: {e}")
            time.sleep(every)
    t = threading.Thread(target=loop, daemon=True, name="screencap"); t.start(); return t

if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        p = LOCAL / "test.jpg"
        try: capture(p); print(f"ok {p.stat().st_size} bytes at {p}")
        except CaptureError as e: print("failed:", e); sys.exit(1)
