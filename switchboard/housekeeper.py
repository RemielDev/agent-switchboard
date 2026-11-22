#!/usr/bin/env python3
"""Keeps the worker light: measures memory, CPU and what the orchestration has running, and cleans up after it.
Stdlib only. Windows does the work; on other systems it exits.

  housekeeper.py [--poll 60]      loop (one per device)
  housekeeper.py --once           one pass, print the reading
  housekeeper.py --dry-run        never kill, rotate or change priority; say what would happen
  housekeeper.py --conflicts      clear the Syncthing conflict copies under Queue once, now (any system; with --dry-run: only say)

Each pass:
- writes Queue/resources/<host>.json (free memory, commit, CPU, browser renderers, agent runs).
  fleet.py reads it; a device marked `pressure` gets no new agent runs until it recovers.
- kills leftovers the orchestration owns whose parent process is gone: MCP helpers after 3 minutes, an agent run
  after 5 minutes. Nothing else is ever killed by this rule: not a browser, not an app.
- lowers agent runs to Below Normal priority so the desktop stays responsive.
- hourly, clears Syncthing conflict copies under Queue (fleet.resolve_conflicts): JSON state merged, .md and .jsonl
  lines united, copies inside attachments/*/before archived; every handled copy moves to Queue/archive/sync-conflicts,
  nothing is deleted. What it cannot handle goes to the owner as one line. Only Windows runs this pass: one resolver, one writer.
- rotates this device's worker logs over 5 MB (one old copy kept, only when no process holds the log), and removes run temp folders older than 3 hours.
- optional, only when the agy CLI (Gemini) is installed: the worker is assumed to run AI work only, with agents driving
  it all day, so nothing waits for the keyboard to go quiet. Every `judge_every_min` (sooner when free memory is under
  `pressure_free_gb`, at once on Clean now) a model (Gemini through agy, no Claude or Codex usage) gets the unfinished
  mail threads and every Chrome tab and app on the worker with its memory, and decides what to close. No fixed keep
  list: housekeeper-apps.json only gives it notes. Only the operating system and the orchestration itself are never
  offered. Apps close politely, then forced if still judged unneeded next round. Tabs close through UI Automation, no
  focus taken. Without agy this step is skipped.

Each pass is recorded in Queue/resources/<host>.json ("house"). Writing Queue/housekeeper/<host>-control.json pauses it
({"paused": true}) or asks for a judgment now ({"clean_at": <epoch>}).

Pressure starts under 2.5 GB free or over 97 percent commit, and clears above 4 GB free and under 94 percent."""
import argparse, base64, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet
from common import hostname, queue_root

SAMPLE = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$os = Get-CimInstance Win32_OperatingSystem
$cpu = (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average
$p = Get-CimInstance Win32_Process | ForEach-Object {
  $c = "$($_.CommandLine)"; if ($c.Length -gt 400) { $c = $c.Substring(0, 400) }
  $t = 0; if ($_.CreationDate) { $t = ([DateTimeOffset]$_.CreationDate).ToUnixTimeSeconds() }
  ,@([int]$_.ProcessId, [int]$_.ParentProcessId, $_.Name, [int]($_.WorkingSetSize / 1MB), [long]$t, $c, [int]$_.SessionId) }
$w = @{}; Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle } | ForEach-Object { $w["$($_.Id)"] = $_.MainWindowTitle }
@{ titles = $w; total = $os.TotalVisibleMemorySize; free = $os.FreePhysicalMemory; vtotal = $os.TotalVirtualMemorySize
   vfree = $os.FreeVirtualMemory; cpu = $cpu; procs = $p } | ConvertTo-Json -Depth 4 -Compress
"""
WINDOWS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -Namespace HK -Name U -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)] public struct LII { public uint cbSize; public uint dwTime; }
[DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LII p);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
[DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
'@
$l = New-Object HK.U+LII; $l.cbSize = 8; [void][HK.U]::GetLastInputInfo([ref]$l)
$idle = ([Environment]::TickCount - [int]$l.dwTime) / 1000
$out = @()
if ($true) {   # agents drive this PC all day: keyboard idle means nothing here
  $all = Get-CimInstance Win32_Process
  # The worker's own loops (HKCU Run entries: worker-tasks loops, Syncthing, Remote Control) run in hidden consoles
  # with no busy child between polls. Closing one ends Syncthing and the mail runners, so only a visible window
  # counts and a worker's console is never idle. Windows Terminal is left out: as
  # the default terminal it hosts consoles started elsewhere (logon Run entries included), which are not its child
  # processes, so it always looks idle and closing it ends every console it hosts.
  $keep = 'worker-tasks\.ps1|syncthing|remote-control|-WindowStyle Hidden|-w hidden'
  foreach ($p in (Get-Process pwsh,powershell,cmd -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and [HK.U]::IsWindowVisible($_.MainWindowHandle) -and $_.MainWindowTitle -notlike 'Administrator:*' })) {  # an elevated window ignores a close from here
    if ("$(($all | Where-Object { $_.ProcessId -eq $p.Id }).CommandLine)" -match $keep) { continue }
    $ids = @($p.Id); $work = 0
    for ($i = 0; $i -lt $ids.Count -and $ids.Count -lt 400; $i++) {
      foreach ($c in ($all | Where-Object { $_.ParentProcessId -eq $ids[$i] })) {
        $ids += [int]$c.ProcessId
        if ($c.Name -notmatch '^(conhost|OpenConsole|pwsh|powershell|cmd)\.exe$' -or "$($c.CommandLine)" -match $keep) { $work++ }
      }
    }
    $age = ((Get-Date) - $p.StartTime).TotalHours
    if ($work -eq 0 -and $age -gt 2) { try { if ($p.CloseMainWindow()) { $out += "closed idle terminal '$($p.MainWindowTitle)'" } } catch {} }
  }
}
ConvertTo-Json @($out) -Compress
"""
TABS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$close = @(($env:HK_TABS_CLOSE | ConvertFrom-Json))
$A = [System.Windows.Automation.AutomationElement]; $CT = [System.Windows.Automation.ControlType]
$TW = [System.Windows.Automation.TreeWalker]::ControlViewWalker
function Tabs($el, $depth) {   # the tab strip only: never descend into page content, whose own tabs are TabItems too
  if ($depth -gt 14) { return }
  $c = $TW.GetFirstChild($el)
  while ($c) {
    $t = $c.Current.ControlType
    if ($t -eq $CT::TabItem) { $c } elseif ($t -ne $CT::Document) { Tabs $c ($depth + 1) }
    $c = $TW.GetNextSibling($c)
  }
}
$pids = @(Get-Process chrome -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$cond = New-Object System.Windows.Automation.PropertyCondition ($A::ClassNameProperty, "Chrome_WidgetWin_1")
$out = @(); $closed = @()
foreach ($w in $A::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)) {
  if ($pids -notcontains $w.Current.ProcessId) { continue }
  foreach ($t in @(Tabs $w 0)) {
    $name = "$($t.Current.Name)"; $sel = $false
    try { $sel = $t.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Current.IsSelected } catch {}
    $mb = 0; if ($name -match ' - Memory usage - ([\d.,]+) (MB|GB)') { $mb = [double]($matches[1] -replace ',', ''); if ($matches[2] -eq 'GB') { $mb *= 1024 } }
    $group = ''; if ($name -match ' - Part of group (.+?)( - Memory usage|$)') { $group = $matches[1] }
    $title = ($name -replace ' - Memory usage - .*$', '') -replace ' - Part of group .*$', ''
    $key = "$($w.Current.NativeWindowHandle)|$title"
    if ($close -contains $key) {
      $btn = $t.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition ($A::ControlTypeProperty, $CT::Button)))
      if ($btn) { try { $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); $closed += $key; continue } catch {} }
    }
    $out += @{ key = $key; title = $title; sel = $sel; group = $group; mb = [int]$mb }
  }
}
@{ tabs = $out; closed = $closed } | ConvertTo-Json -Depth 4 -Compress
"""
APPS_CFG = Path(__file__).resolve().parent / "housekeeper-apps.json"
NO_WINDOW = 0x08000000
# (label, process name, command line, seconds without a parent before it is killed)
OWNED = [("mcp helper", r"^python", r"agent-mail-mcp\.py", 180),
         ("agent run", r"^claude", r"You are the mail agent", 300)]
RUNNER = r"(mail|queue)-runner\.py"
LOWER = r"^(claude|codex|python|node|cmd|pwsh|powershell|conhost)"

def sample():
    enc = base64.b64encode(SAMPLE.encode("utf-16-le")).decode()
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                         capture_output=True, timeout=90, creationflags=NO_WINDOW).stdout.decode("utf-8", "replace")
    d = json.loads(out[out.index("{"):])
    d["procs"] = [dict(zip(("pid", "ppid", "name", "mb", "born", "cmd", "session"), r)) for r in d["procs"]]
    return d

def orphan(p, by):
    """Its parent has exited (or the parent's pid now belongs to a younger process)."""
    par = by.get(p["ppid"])
    return par is None or par["born"] > p["born"]

def plan(procs, now):
    """Pure: what to kill and what to slow down. Returns (kills [(proc, label)], pids to lower)."""
    by = {p["pid"]: p for p in procs}
    kills = []
    for p in procs:
        for label, name, cmd, age in OWNED:
            if re.search(name, p["name"], re.I) and re.search(cmd, p["cmd"]) and p["born"] and now - p["born"] > age and orphan(p, by):
                kills.append((p, label)); break
    kids = {}
    for p in procs: kids.setdefault(p["ppid"], []).append(p)
    todo = [p for p in procs if re.search(r"^python", p["name"], re.I) and re.search(RUNNER, p["cmd"])]
    low, seen = [], {p["pid"] for p in todo}
    while todo:
        for c in kids.get(todo.pop()["pid"], []):
            if c["pid"] in seen or c["born"] < by[c["ppid"]]["born"]: continue
            seen.add(c["pid"]); todo.append(c)
            if re.search(LOWER, c["name"], re.I): low.append(c["pid"])
    return kills, low

def lower(pid):
    """Normal -> Below Normal. True when it changed."""
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x0200 | 0x1000, False, pid)
    if not h: return False
    try: return k.GetPriorityClass(h) == 0x20 and bool(k.SetPriorityClass(h, 0x4000))
    finally: k.CloseHandle(h)

def reading(d, was_pressed):
    procs, gb = d["procs"], 1024 * 1024
    free, commit = d["free"] / gb, 100 * (1 - d["vfree"] / d["vtotal"])
    why = []
    if free < (4.0 if was_pressed else 2.5): why.append(f"{free:.1f} GB free")
    if commit > (94 if was_pressed else 97): why.append(f"commit {commit:.0f}%")  # 92 held all agent mail with plenty of RAM free
    groups = {}
    for p in procs: groups[p["name"]] = groups.get(p["name"], 0) + p["mb"]
    return {"host": hostname(), "at": time.time(), "total_gb": round(d["total"] / gb, 1), "free_gb": round(free, 1),
            "commit_pct": round(commit), "cpu_pct": round(d["cpu"] or 0), "pressure": bool(why), "why": ", ".join(why),
            "browser_renderers": sum(1 for p in procs if p["name"].lower() in ("chrome.exe", "msedge.exe") and "--type=renderer" in p["cmd"]),
            "agent_runs": sum(1 for p in procs if re.search(r"^claude", p["name"], re.I) and "You are the mail agent" in p["cmd"]),
            "top": sorted(([k, v] for k, v in groups.items()), key=lambda x: -x[1])[:6]}

def tidy_files(dry):
    done = []
    for f in queue_root().glob(f"worker-*-{os.environ.get('COMPUTERNAME', hostname())}.log"):
        try:
            if f.stat().st_size > 5 * 1024 * 1024:
                # Rename, never truncate: a writer that holds the file open would keep its offset and refill it.
                # The rename fails while a wrapper holds the log, so that log rotates the next time it lets go.
                if not dry: os.replace(f, f.with_name(f.name + ".old"))
                done.append(f"rotated {f.name}")
        except OSError: pass
    for t in Path(tempfile.gettempdir()).glob("mail-runner-*"):
        try:
            if t.is_dir() and time.time() - t.stat().st_mtime > 3 * 3600:
                if not dry: shutil.rmtree(t, ignore_errors=True)
                done.append(f"removed temp {t.name}")
        except OSError: pass
    return done

def conflict_pass(dry, state):
    """Conflict copies under Queue merged or archived; what is left goes to the owner in one line, once per new list."""
    done, left = fleet.resolve_conflicts(dry=dry)
    if left and not dry and state.get("conflicts_left") != left:
        fleet.event("waiting", f"{len(left)} sync conflict cop{'y needs' if len(left) == 1 else 'ies need'} you: " + "; ".join(left)[:900])
    state["conflicts_left"] = left
    return done + ([f"conflict copies left for the owner: {'; '.join(left)}"] if left else [])

def tidy_windows(dry):
    """Visible terminal windows left on the desktop: close the ones that are only an idle prompt older than 2 hours. A
    window that runs something, a hidden console and a worker's own console are never closed."""
    if dry: return []
    try:
        enc = base64.b64encode(WINDOWS.encode("utf-16-le")).decode()
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                             capture_output=True, timeout=60, creationflags=NO_WINDOW).stdout.decode("utf-8", "replace")
        got = json.loads(out[out.index("["):]) if "[" in out else []
        return [str(x) for x in (got if isinstance(got, list) else [got])]
    except Exception as e: return [f"window tidy failed: {str(e)[:80]}"]

def apps_cfg():
    try: return json.loads(APPS_CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}

# The operating system and the orchestration itself: never offered to the judge. Chrome is offered per tab instead of
# as one app; claude.exe is both the Claude app and every agent run.
INFRA = set("""system idle registry smss csrss wininit winlogon services lsass lsaiso svchost fontdrvhost dwm explorer sihost ctfmon
taskhostw runtimebroker searchhost searchindexer searchprotocolhost searchfilterhost startmenuexperiencehost shellexperiencehost textinputhost
applicationframehost conhost openconsole dllhost wmiprvse audiodg spoolsv msmpeng nissrv securityhealthservice securityhealthsystray
memory compression secure system vmmem vmmemwsl wslservice wslhost wslrelay wsl python pythonw py pwsh powershell cmd windowsterminal
claude codex codex-code-mode-host node agy git ssh syncthing warp-svc nvcontainer
chrome msedgewebview2 smartscreen lockapp widgets widgetservice useroobebroker""".split()) | {"memory compression", "secure system"}

def candidates(d, tabs, state, now):
    """What the judge may close, each with an id, and the facts to judge it by."""
    procs, titles = d["procs"], d.get("titles") or {}
    out, apps = {}, {}
    for p in procs:
        n = re.sub(r"\.exe$", "", p["name"], flags=re.I)
        if n.lower() in INFRA or n.lower().startswith(("nv", "asus", "intel", "amd", "realtek")) or p.get("session", 1) == 0: continue  # session 0: services, not ours to stop
        a = apps.setdefault(n, {"kind": "app", "mb": 0, "procs": 0, "windows": [], "since": now})
        a["mb"] += p["mb"]; a["procs"] += 1; a["since"] = min(a["since"], p["born"] or now)
        if titles.get(str(p["pid"])): a["windows"].append(titles[str(p["pid"])][:80])
    for n, a in apps.items():
        if a["mb"] >= 40 or a["windows"]:
            out[f"app:{n}"] = {"kind": "app", "mb": a["mb"], "procs": a["procs"], "windows": a["windows"][:4], "open_min": int((now - a["since"]) / 60)}
    seen = state.setdefault("tabs_seen", {}); live = set()
    for x in tabs:
        k = x["key"]; live.add(k)
        if x.get("sel"): seen[k] = now
        seen.setdefault(k, now)
        out["tab:" + k] = {"kind": "Chrome tab", "title": x["title"][:100], "group": x.get("group") or "", "active_in_window": bool(x.get("sel")),
                           "mb": x.get("mb") or 0, "unseen_min": int((now - seen[k]) / 60)}
    for k in list(seen):
        if k not in live: seen.pop(k)
    for cid, c in out.items():
        if cid in state.get("asked", {}): c["asked_to_close_min_ago"] = int((now - state["asked"][cid]) / 60)
    return out

def work_context(now):
    """The unfinished mail threads on every device (running, queued, waiting for the owner), read from the mail folders."""
    return "THREADS (requests the agents work on; who holds each message and its state):\n" + (fleet.work_text(fleet.active_work()) or "- none")

JUDGE = """You keep memory free on a Windows worker that runs the owner's AI agents and nothing else. The owner is not at
it: every click and keystroke on it comes from agents. Decide which CANDIDATES to close right now.

Close a candidate when no running, queued or waiting work is likely to need it, or when it plainly has nothing to do with
the agents' work. Keep what a working, queued or waiting thread is likely to use (match repo and project names in thread
titles, window titles, tab titles and tab groups). Reopening costs something (an app may lose unsaved state; a closed
tab loses its page), so with plenty
of free memory close only what is clearly idle or unrelated, and with little free memory be readier to close. A tab unseen
for hours with no matching thread is a good candidate; so is an app no agent uses. Nothing is off limits by rule: weigh it.

Reply with the JSON object only: {"close": [{"id": "<candidate id>", "why": "<one short sentence>"}], "note": "<one sentence on the state of the PC>"}"""
SCHEMA = {"type": "object", "properties": {"close": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "why": {"type": "string"}},
          "required": ["id", "why"]}}, "note": {"type": "string"}}, "required": ["close", "note"]}

def agy():
    for c in (shutil.which("agy"), Path.home() / ".local/bin/agy", Path(os.environ.get("LOCALAPPDATA", "")) / "agy/bin/agy.exe"):
        if c and Path(c).is_file(): return str(c)

def judge(prompt, models):
    """(model, {close, note}) from the first model that answers."""
    exe = agy()
    if not exe: raise RuntimeError("agy not installed")
    work = Path(tempfile.mkdtemp(prefix="housekeeper-judge-"))  # empty folder: nothing to read or change
    err = ""
    try:
        for m in models:
            try:
                r = subprocess.run([exe, "-p", "Everything you need is in this message. Do not call any tool or read any file.\n\n" + prompt, "--model", m,
                                    "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--disable-slash-commands", "--print-timeout", "180s"],
                                   cwd=work, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=240, creationflags=NO_WINDOW)
                env = next((json.loads(l) for l in reversed(r.stdout.strip().splitlines()) if l.strip().startswith("{")), {})
                if env.get("status") != "SUCCESS": err = (r.stderr or r.stdout)[-160:]; continue
                body = env["response"].strip()
                return m, json.JSONDecoder().raw_decode(body[body.find("{"):])[0]
            except (subprocess.TimeoutExpired, ValueError, KeyError) as e: err = str(e)[:160]
        raise RuntimeError("no model answered: " + re.sub(r"\s+", " ", err))
    finally: shutil.rmtree(work, ignore_errors=True)

def judge_prompt(d, cands, now, cfg):
    gb = 1024 * 1024
    machine = (f"MACHINE: {d['free'] / gb:.1f} GB of {d['total'] / gb:.1f} GB RAM free, commit {100 * (1 - d['vfree'] / d['vtotal']):.0f}%, CPU {d['cpu']}%."
               f" Low memory starts under {cfg.get('pressure_free_gb', 6)} GB free.")
    notes = ("NOTES FROM OWNER:\n" + "\n".join("- " + n for n in cfg.get("notes", []))) if cfg.get("notes") else ""
    cl = "CANDIDATES (id: facts):\n" + "\n".join(f"- {cid}: {json.dumps(c, ensure_ascii=False)}" for cid, c in sorted(cands.items(), key=lambda x: -x[1]["mb"]))
    return "\n\n".join(x for x in (JUDGE, machine, notes, work_context(now), cl) if x)

def tidy_tabs(close, dry):
    enc = base64.b64encode(TABS.encode("utf-16-le")).decode()
    env = {**os.environ, "HK_TABS_CLOSE": json.dumps([] if dry else close)}
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], env=env,
                         capture_output=True, timeout=60, creationflags=NO_WINDOW).stdout.decode("utf-8", "replace")
    d = json.loads(out[out.index("{"):]) if "{" in out else {}
    tabs = d.get("tabs") or []
    return (tabs if isinstance(tabs, list) else [tabs]), list(d.get("closed") or [])

CONTROL = lambda: queue_root() / "housekeeper" / f"{hostname()}-control.json"
def control():
    try: return json.loads(CONTROL().read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}

def due(state, now, free_gb, cfg, force):
    """Time for a judgment: every judge_every_min, every 3 minutes while memory is low, at once on Clean now."""
    if force: return True
    every = float(cfg.get("judge_every_min", 10)) * 60
    if free_gb < float(cfg.get("pressure_free_gb", 6)): every = min(every, 180)
    return now - state.get("judged", 0) >= every

def carry_out(plan, cands, state, dry, now, acts):
    """Close what the judge named. Returns MB freed (estimated from what was closed)."""
    asked, freed, tabs = state.setdefault("asked", {}), 0, []
    for cid in list(asked):
        if cid not in cands: asked.pop(cid)
    for item in (plan.get("close") or [])[:12]:
        cid, why = str(item.get("id") or ""), str(item.get("why") or "")[:140]
        c = cands.get(cid)
        if not c: continue
        kind, _, ref = cid.partition(":")
        if kind == "tab": tabs.append((ref, c, why)); continue
        hard = cid in asked and now - asked[cid] > 45
        if not dry: subprocess.run(["taskkill", "/IM", ref + ".exe", "/T"] + (["/F"] if hard else []), capture_output=True, creationflags=NO_WINDOW)
        acts.append(f"{'force-closed' if hard else 'closed'} {ref} ({c['mb']} MB): {why}")
        if not dry: asked.setdefault(cid, now)
        freed += c["mb"]
    if tabs:
        _, closed = tidy_tabs([k for k, _, _ in tabs], dry)
        for k, c, why in tabs:
            if dry or k in closed: acts.append(f"closed tab '{c['title'][:60]}'" + (f" ({c['mb']} MB)" if c["mb"] else "") + f": {why}"); freed += c["mb"]
            else: acts.append(f"could not close tab '{c['title'][:60]}'")
    return freed

def follow_through(procs, titles, state, dry, now, acts):
    """An app the judge closed that ignored the polite close (tray apps do) is forced a pass later."""
    alive = {re.sub(r"\.exe$", "", p["name"], flags=re.I).lower() for p in procs}
    asked = state.setdefault("asked", {})
    for cid, t in list(asked.items()):
        kind, _, ref = cid.partition(":")
        if ref.lower() not in alive: asked.pop(cid); continue  # gone: if it comes back, only a new judgment may close it
        if now - t < 45: continue
        if kind == "app" and cid not in state.setdefault("forced", {}):
            if not dry: subprocess.run(["taskkill", "/IM", ref + ".exe", "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
            state["forced"][cid] = now; acts.append(f"force-closed {ref}: it ignored the close")
    for cid in list(state.get("forced", {})):
        if cid not in asked: state["forced"].pop(cid)

def cleanup(d, state, dry, force, now, acts):
    cfg = apps_cfg(); free = d["free"] / (1024 * 1024)
    follow_through(d["procs"], d.get("titles") or {}, state, dry, now, acts)
    tabs, _ = tidy_tabs([], True)
    cands = candidates(d, tabs, state, now)
    info = {"tabs": len(tabs), "tab_mb": sum(x.get("mb") or 0 for x in tabs), "candidates": len(cands), "freed_mb": 0}
    if agy() and due(state, now, free, cfg, force):  # the judge is optional: without agy, nothing is closed
        state["judged"] = now
        model, plan = judge(judge_prompt(d, cands, now, cfg), cfg.get("models") or ["gemini-4-carbon-low", "gemini-4-carbon-medium"])
        state.update(model=model, note=str(plan.get("note") or "")[:240], kept=len(cands) - len(plan.get("close") or []))
        info["freed_mb"] = carry_out(plan, cands, state, dry, now, acts)
        if dry: info["plan"] = plan
    return info

def one_pass(state, dry, force=False):
    d = sample(); now = time.time()
    ctl = control(); paused = bool(ctl.get("paused"))
    r = reading(d, state.get("pressed", False))
    # A commit-only spike must repeat on the next pass before it holds runs: most such spikes are gone a pass later with plenty of RAM free. Low free RAM still holds at once.
    if r["pressure"] and not state.get("pressed") and "GB free" not in r["why"] and not state.get("spike"):
        state["spike"] = True; r["pressure"] = False; r["why"] = ""
    else: state["spike"] = False
    kills, low = plan(d["procs"], now)
    acts = []
    if paused: kills = []
    for p, label in kills:
        if not dry: subprocess.run(["taskkill", "/PID", str(p["pid"]), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        acts.append(f"killed orphaned {label} {p['pid']} ({p['mb']} MB, {int((now - p['born']) / 60)} min old)")
    n = len(low) if dry else sum(1 for pid in low if lower(pid))
    if n: acts.append(f"lowered priority of {n} agent processes")
    if now - state.get("windows", 0) > 600:
        acts += tidy_windows(dry); state["windows"] = now
    if now - state.get("tidied", 0) > 900:
        acts += tidy_files(dry); state["tidied"] = now
    if now - state.get("conflicts", 0) > 3600:
        state["conflicts"] = now
        try: acts += conflict_pass(dry, state)
        except Exception as e: acts.append(f"conflict pass failed: {str(e)[:120]}")
    info = {}
    if not paused:
        try: info = cleanup(d, state, dry, force, now, acts)
        except Exception as e: acts.append(f"judgment failed: {str(e)[:120]}"); state["judged"] = now
    if dry: acts = ["would have " + a for a in acts]
    r["actions"] = acts
    day = time.strftime("%Y-%m-%d")
    if state.get("day") != day: state.update(day=day, freed_today=0)
    state["freed_today"] = state.get("freed_today", 0) + (0 if dry else info.get("freed_mb", 0))
    rec = state.setdefault("recent", [])
    rec += [[int(now), a] for a in acts if not a.startswith("lowered priority")]; del rec[:-30]
    cfg = apps_cfg()
    r["house"] = {"at": now, "paused": paused, "forced": force, "tabs": info.get("tabs"), "tab_mb": info.get("tab_mb"), "candidates": info.get("candidates"),
                  "judged": state.get("judged"), "every_min": cfg.get("judge_every_min", 10), "model": state.get("model"), "note": state.get("note"),
                  "kept": state.get("kept"), "freed_today_mb": state["freed_today"], "recent": rec[-15:]}
    if dry and info.get("plan"): r["plan"] = info["plan"]
    if not dry:
        fleet._write(queue_root() / "resources" / f"{hostname()}.json", r)
        if r["pressure"] != state.get("pressed", False):
            fleet.event("waiting" if r["pressure"] else "done", f"{hostname()} is low on memory ({r['why']}); new agent runs wait" if r["pressure"]
                        else f"{hostname()} memory recovered ({r['free_gb']} GB free); agent runs resume")
    state["pressed"] = r["pressure"]
    return r

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--poll", type=float, default=60); ap.add_argument("--once", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--conflicts", action="store_true")
    a = ap.parse_args()
    if a.conflicts:
        done, left = fleet.resolve_conflicts(dry=a.dry_run)
        for x in done: print(("would have " if a.dry_run else "") + x)
        for x in left: print("LEFT " + x)
        return
    if os.name != "nt": sys.exit("housekeeper.py only manages Windows workers")
    if a.once or a.dry_run:
        print(json.dumps(one_pass({}, a.dry_run), indent=1)); return
    if not fleet.single_instance("housekeeper"): sys.exit("housekeeper already running on this device")
    state = {}
    try:  # keep the pass history across restarts
        old = json.loads((queue_root() / "resources" / f"{hostname()}.json").read_text(encoding="utf-8")).get("house") or {}
        if old.get("recent"): state["recent"] = old["recent"]
        if time.strftime("%Y-%m-%d", time.localtime(old.get("at") or 0)) == time.strftime("%Y-%m-%d"): state.update(day=time.strftime("%Y-%m-%d"), freed_today=old.get("freed_today_mb") or 0)
    except (OSError, ValueError): pass
    state["clean_seen"] = float(control().get("clean_at") or 0)
    force = False
    while True:
        try:
            r = one_pass(state, False, force)
            for x in r["actions"]: print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {x}", flush=True)
        except Exception as e: print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pass failed: {e!r}", flush=True)
        force, t0, was = False, time.time(), control().get("paused")
        while time.time() - t0 < a.poll:  # a Clean now or Pause in the control file acts within seconds
            time.sleep(5); c = control()
            if float(c.get("clean_at") or 0) > state["clean_seen"]: state["clean_seen"] = float(c["clean_at"]); force = True; break
            if c.get("paused") != was: break

if __name__ == "__main__": main()
