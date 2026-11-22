#!/usr/bin/env python3
"""Load scoped MEMORY.md files through Claude Code's command hooks.

The memory lives in one Markdown vault (any folder of .md files). A workspace folder gets the MEMORY.md files that
apply to it: the vault-root MEMORY.md, every MEMORY.md on the folder's own path (a real file or a link into the
vault), each one mapped to the folder in the registry, and the MEMORY.md files above each of those inside the vault.
Wider scopes come first; narrower ones take precedence.

SessionStart sends every scoped file. UserPromptSubmit sends again only the files that changed since the last
complete send. PreToolUse (Read/Edit/Write) sends the files of a scope the session has not seen yet and blocks an
edit until they are read. Unattended runner jobs (AGENT_MAIL_AGENT set) may skip device-note sections listed in
<base>/runner-memory.json; nothing else is ever filtered. When the files need more chunks than there are hook
slots, the device note's tail is left out first and the last slot says what to read.
Any error in this path falls back to the previous loader logic, and the hook always exits 0.

Files (base is $AI_MEMORY_BASE, default ~/.switchboard):
  <base>/registry.json       {"vault": "<vault folder>",
                              "device_note": "<optional: this device's note, absolute or relative to the vault>",
                              "scopes": [{"root": "<workspace folder>", "canonical": "<vault>/.../MEMORY.md"}, ...]}
  <base>/MEMORY.md           optional. When it is the vault-root MEMORY.md itself (a link), Claude Code is assumed to
                             load it already (for example `@~/.switchboard/MEMORY.md` in ~/.claude/CLAUDE.md) and the
                             hook leaves it out; when it is a copy or absent, the hook sends the vault-root file.
  <base>/runner-memory.json  optional: {"<folder>": {"<device note file name>": ["<## heading>", ...]}}
  <base>/.cache/claude-memory-loader/   per-session state and errors.log

Hook usage: python3 claude-memory-loader.py --chunk N --chunks 8, one hook entry per slot N = 0..7 for SessionStart
and UserPromptSubmit (each output stays under Claude Code's preview limit), and one entry (--chunk 0) for PreToolUse."""
from pathlib import Path
import argparse, contextlib, hashlib, io, json, os, re, sys, time
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

BASE = Path(os.environ.get('AI_MEMORY_BASE', str(Path.home() / '.switchboard')))
REGISTRY = BASE / 'registry.json'
POLICY = BASE / 'runner-memory.json'
def registry():
    return json.loads(REGISTRY.read_text()) if REGISTRY.is_file() else {}
def vault_root():
    vault = registry().get('vault')
    return Path(vault).expanduser().resolve() if vault else None
def device_file():
    """This device's own note (paths, tools, limits), from the registry's optional device_note; None when not set."""
    data = registry()
    note, vault = data.get('device_note'), data.get('vault')
    if not note: return None
    note = Path(note).expanduser()
    return note if note.is_absolute() or not vault else Path(vault).expanduser() / note
GLOBAL = BASE / 'MEMORY.md'

def lock_file(handle):
    if fcntl: fcntl.flock(handle, fcntl.LOCK_EX)
    else:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

def routes():
    # Windows has no workspace MEMORY.md links, so map folders through the registry.
    if not REGISTRY.is_file(): return {}
    data = json.loads(REGISTRY.read_text())
    return {os.path.normcase(str(Path(s['root']))): s['canonical'] for s in data.get('scopes', []) if s.get('root') and s.get('canonical')}

def scope_files(directory):
    directory = Path(directory).expanduser().resolve()
    if directory.is_file(): directory = directory.parent
    paths = []
    device = device_file()
    if device and device.is_file(): paths.append(device)
    seen = {GLOBAL.resolve(), *[p.resolve() for p in paths]}
    vault = vault_root()
    routed = routes()
    for folder in [*reversed(directory.parents), directory]:
        p = folder / 'MEMORY.md'
        if not p.is_file():
            target = routed.get(os.path.normcase(str(folder)))
            if not target or not Path(target).is_file(): continue
            p = Path(target)
        canonical = p.resolve()
        # Native workspace ancestry and canonical vault ancestry can differ: also send the vault folders above it.
        if vault and vault in canonical.parents:
            for ancestor in reversed(canonical.parent.parents):
                if ancestor != vault and vault not in ancestor.parents: continue
                shared = ancestor / 'MEMORY.md'
                if shared.is_file() and shared.resolve() not in seen:
                    seen.add(shared.resolve()); paths.append(shared)
        if canonical not in seen:
            seen.add(canonical); paths.append(p)
    return paths

def scope_label(p):
    if p == device_file(): return 'This execution device only, across workspaces'
    vault = vault_root()
    if vault and vault in p.parents:
        return 'Matching mapped workspace and its descendants (' + str(p.parent.relative_to(vault)) + ')'
    return str(p.parent)

def signature(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def context(paths):
    chunks = ['Shared memory loaded from canonical files. Each file applies only within the folder named below and its descendants; narrower files take precedence. Write to the resolved canonical vault path, never replace a local discovery link or create CLAUDE.md or AGENTS.md copies.']
    for p in paths:
        chunks.append('\n---\nScope: ' + scope_label(p) + '\nSource: ' + str(p.resolve()) + '\n\n' + p.read_text())
    return '\n'.join(chunks)

def context_chunks(paths):
    chunks = []
    for p in paths:
        body = p.read_text()
        parts = [body[i:i + 6000] for i in range(0, len(body), 6000)] or ['']
        for i, part in enumerate(parts):
            chunks.append('Canonical memory. Write to the resolved Source path. Scope: ' + scope_label(p) + '\nSource: ' + str(p.resolve()) + '\nPart ' + str(i + 1) + '/' + str(len(parts)) + '. Applies only within this scope; narrower scope instructions take precedence.\n\n' + part)
    return chunks

def run_legacy(event, chunk=0, chunk_count=8):
    # The previous loader, kept as it was: run() falls back to it on any error. PreToolUse always uses it.
    name = event.get('hook_event_name', 'SessionStart')
    cwd = Path(event.get('cwd', os.getcwd())).resolve()
    directory = cwd
    tool = event.get('tool_name', '')
    tool_input = event.get('tool_input') or {}
    target = tool_input.get('file_path') or tool_input.get('path')
    if name == 'PreToolUse' and isinstance(target, str):
        p = Path(target).expanduser()
        if not p.is_absolute(): p = cwd / p
        directory = p.parent
    paths = scope_files(directory)
    cache = BASE / '.cache/claude-memory-loader'
    cache.mkdir(parents=True, exist_ok=True); os.chmod(cache, 0o700)
    key = hashlib.sha256(str(event.get('session_id', 'no-session')).encode()).hexdigest()
    state_file = cache / (key + '.json')
    lock = cache / (key + '.lock')
    with lock.open('a') as handle:
        lock_file(handle)
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        seen = state.setdefault('seen', {})
        signatures = {str(p.resolve()): signature(p) for p in paths}
        if name == 'PreToolUse':
            changed = [p for p in paths if seen.get(str(p.resolve())) != signatures[str(p.resolve())]]
            if not changed: return
            text = context(changed)
            loaded = changed
            output = {'hookEventName': name}
            if len(text) <= 8500:
                output['additionalContext'] = text
            else:
                # Claude redirects additionalContext over 10,000 characters to a
                # preview file. Require explicit reads instead of hiding rules.
                loaded = []
                output['additionalContext'] = 'Applicable canonical instructions must be read before editing:\n' + '\n'.join(str(p) for p in changed)
                if tool == 'Read' and target:
                    read_path = Path(target).expanduser()
                    if not read_path.is_absolute(): read_path = cwd / read_path
                    loaded = [p for p in changed if p.resolve() == read_path.resolve()]
            if tool in ['Edit', 'Write', 'MultiEdit']:
                output['permissionDecision'] = 'deny'
                output['permissionDecisionReason'] = 'New or changed scoped MEMORY.md instructions are now available. Read the listed instructions, apply them, then retry this edit. This needs no user approval.'
            print(json.dumps({'hookSpecificOutput': output}))
            for p in loaded: seen[str(p.resolve())] = signatures[str(p.resolve())]
        else:
            # The installed Claude also redirects long plain stdout to a preview.
            # Separate bounded hook outputs preserve the full text in context.
            chunks = context_chunks(paths)
            if len(chunks) > chunk_count:
                raise RuntimeError('MEMORY.md chain needs more configured hook chunks: ' + str(len(chunks)))
            fingerprint = hashlib.sha256(json.dumps(signatures, sort_keys=True).encode()).hexdigest()
            slots = state.setdefault('slots', {})
            if name == 'UserPromptSubmit' and slots.get(str(chunk)) == fingerprint: return
            if chunk < len(chunks):
                print(json.dumps({'hookSpecificOutput': {'hookEventName': name, 'additionalContext': chunks[chunk]}}))
            slots[str(chunk)] = fingerprint
            if all(slots.get(str(i)) == fingerprint for i in range(len(chunks))): seen.update(signatures)
        tmp = state_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(state)); os.chmod(tmp, 0o600); os.replace(tmp, state_file)

def runner_skips(directory):
    """Device-note headings an unattended runner job skips in DIRECTORY: the longest matching policy root wins.
    Policy shape: {"<folder>": {"<device note file name>": ["<## heading>", ...]}}. Interactive sessions get []."""
    if not os.environ.get('AGENT_MAIL_AGENT') or not POLICY.is_file(): return []
    policy = json.loads(POLICY.read_text(encoding='utf-8'))
    device = device_file()
    if not isinstance(policy, dict) or not device: return []
    here = os.path.normcase(str(Path(directory).resolve()))
    best, heads = -1, []
    for root, notes in policy.items():
        r = os.path.normcase(str(Path(root).expanduser().resolve()))
        if (here == r or here.startswith(r.rstrip('\\/') + os.sep)) and len(r) > best:
            best, heads = len(r), (notes or {}).get(device.name, [])
    if not isinstance(heads, list) or not all(isinstance(h, str) for h in heads): raise ValueError('bad runner-memory policy')
    return [h[3:].strip() if h.startswith('## ') else h.strip() for h in heads]

def drop_sections(text, heads):
    """TEXT without the '## ' sections named in HEADS (exact match, outside code fences), and the names dropped."""
    out, dropped, skip, fence = [], [], False, False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(('```', '~~~')): fence = not fence
        elif not fence and re.match(r'#{1,2} ', line):
            name = line[3:].strip() if line.startswith('## ') else None
            skip = name in heads
            if skip: dropped.append(name)
        if not skip: out.append(line)
    return ''.join(out), dropped

def note_text(p, skips):
    body = p.read_text()
    if not skips or p != device_file(): return body
    body, dropped = drop_sections(body, skips)
    if dropped:
        body = body.rstrip('\n') + '\n\nUnattended runner jobs on this device are not sent these sections of this note: ' + '; '.join(dropped) + '. Read them in ' + str(p.resolve()) + ' if this job needs one.\n'
    return body

def file_chunks(p, skips):
    # Same text, split and headers as context_chunks, so an unfiltered send is byte-identical.
    body = note_text(p, skips)
    parts = [body[i:i + 6000] for i in range(0, len(body), 6000)] or ['']
    head = 'Canonical memory. Write to the resolved Source path. Scope: ' + scope_label(p) + '\nSource: ' + str(p.resolve()) + '\nPart '
    return [head + str(i + 1) + '/' + str(len(parts)) + '. Applies only within this scope; narrower scope instructions take precedence.\n\n' + part for i, part in enumerate(parts)], parts

def fit(paths, chunk_count, skips):
    """At most CHUNK_COUNT chunks. On overflow MEMORY.md files keep their slots, the device note's tail goes first,
    and the last slot names what was left out and where to read it."""
    per = [(p, *file_chunks(p, skips)) for p in paths]
    if sum(len(c) for _, c, _ in per) <= chunk_count: return [x for _, c, _ in per for x in c]
    device, room, allow = device_file(), chunk_count - 1, {}
    for p, c, _ in sorted(per, key=lambda x: x[0] == device):  # MEMORY.md files first, then the device note
        allow[p] = min(len(c), max(room, 0)); room -= allow[p]
    out, left = [], []
    for p, c, parts in per:
        out += c[:allow[p]]
        if allow[p] < len(c):
            cut = sum(map(len, parts[:allow[p]]))
            heads = [m.group(1) for m in re.finditer(r'(?m)^## (.+)$', ''.join(parts)) if m.start() <= cut]
            where = ('from the section "' + heads[-1].strip() + '" on' if heads else 'from the start') + ' (parts ' + str(allow[p] + 1) + '-' + str(len(c)) + ' of ' + str(len(c)) + ')'
            left.append(str(p.resolve()) + ' ' + where)
    out.append('Canonical memory index. Too long for the hook slots, so not injected; read these before acting: ' + '; '.join(left) + '.')
    return out

def run(event, chunk=0, chunk_count=8):
    name = event.get('hook_event_name', 'SessionStart')
    if name == 'PreToolUse': return run_legacy(event, chunk, chunk_count)  # unchanged
    cwd = Path(event.get('cwd', os.getcwd())).resolve()
    paths = scope_files(cwd)
    skips = runner_skips(cwd)
    cache = BASE / '.cache/claude-memory-loader'
    cache.mkdir(parents=True, exist_ok=True); os.chmod(cache, 0o700)
    key = hashlib.sha256(str(event.get('session_id', 'no-session')).encode()).hexdigest()
    state_file = cache / (key + '.json')
    lock = cache / (key + '.lock')
    with lock.open('a') as handle:
        lock_file(handle)
        try: state = json.loads(state_file.read_text()) if state_file.exists() else {}
        except ValueError: state = {}  # a torn state file means "nothing sent yet"
        if not isinstance(state, dict): state = {}
        seen = state.setdefault('seen', {})
        signatures = {str(p.resolve()): signature(p) for p in paths}
        if name == 'UserPromptSubmit':
            # Every slot process sees the same seen set until the last slot of this send records it.
            paths = [p for p in paths if seen.get(str(p.resolve())) != signatures[str(p.resolve())]]
            if not paths: return
            signatures = {str(p.resolve()): signatures[str(p.resolve())] for p in paths}
        # The installed Claude also redirects long plain stdout to a preview.
        # Separate bounded hook outputs preserve the full text in context.
        chunks = fit(paths, chunk_count, skips)
        fingerprint = hashlib.sha256(json.dumps(signatures, sort_keys=True).encode()).hexdigest()
        slots = state.setdefault('slots', {})
        if name == 'UserPromptSubmit' and slots.get(str(chunk)) == fingerprint: return
        if chunk < len(chunks):
            print(json.dumps({'hookSpecificOutput': {'hookEventName': name, 'additionalContext': chunks[chunk]}}))
        slots[str(chunk)] = fingerprint
        if all(slots.get(str(i)) == fingerprint for i in range(len(chunks))): seen.update(signatures)
        tmp = state_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(state)); os.chmod(tmp, 0o600); os.replace(tmp, state_file)

def captured(fn, *args):
    """FN's stdout, released only if FN finishes, so a failed attempt never leaves half an answer."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer): fn(*args)
    return buffer.getvalue()

def last_resort(event, chunk, chunk_count, error):
    # Both paths failed. SessionStart still gets every rule, without state; other events stay silent (fail open).
    if event.get('hook_event_name', 'SessionStart') != 'SessionStart': return ''
    try:
        chunks = fit(scope_files(Path(event.get('cwd', os.getcwd())).resolve()), chunk_count, [])
        text = chunks[chunk] if chunk < len(chunks) else ''
    except Exception:
        text = ('Shared MEMORY.md loader failed (' + type(error).__name__ + '). Before acting, read this device\'s note (if any) and every MEMORY.md on this folder\'s path in the vault named in ' + str(REGISTRY) + '.') if chunk == 0 else ''
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}) + '\n' if text else ''

def note_error(stage, error):
    try:
        log = BASE / '.cache/claude-memory-loader/errors.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + stage + ' ' + type(error).__name__ + ': ' + str(error)[:300] + '\n')
    except Exception: pass
    print('Shared MEMORY.md loader ' + stage + ': ' + str(error), file=sys.stderr)

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--chunk', type=int, default=0)
    parser.add_argument('--chunks', type=int, default=8)
    args = parser.parse_args(argv)
    event = {}
    try:
        event = json.load(sys.stdin)
        out = captured(run, event, args.chunk, args.chunks)
    except Exception as error:
        note_error('fell back to the previous logic', error)
        try: out = captured(run_legacy, event, args.chunk, args.chunks) if isinstance(event, dict) and event else ''
        except Exception as again:
            note_error('previous logic failed too', again)
            out = last_resort(event if isinstance(event, dict) else {}, args.chunk, args.chunks, again)
    sys.stdout.write(out)
    return 0

if __name__ == '__main__':
    try: main()
    except BaseException as error:  # a memory hook must never fail the session
        print('Shared MEMORY.md loader failed: ' + str(error), file=sys.stderr)
    sys.exit(0)
