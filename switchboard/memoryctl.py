#!/usr/bin/env python3
"""Maintain local routes to one authoritative Markdown memory vault (stdlib only).

  memoryctl.py check          verify every registered scope: its MEMORY.md link into the vault, its recall folder and
                              Claude Code's autoMemoryDirectory setting; also that the vault holds no symlinks
  memoryctl.py lint           report note problems (broken [[links]], device paths, tags); never edits, exits 0
  memoryctl.py init ROOT      route a workspace folder to a new MEMORY.md in the vault (default <vault>/Projects/<name>)

The registry (--registry, default ~/.switchboard/registry.json) is the same file claude-memory-loader.py reads:
{"vault": "<folder>", "device_note": "<optional>", "scopes": [{"root", "canonical", "recall", "kind"}, ...]}."""
from pathlib import Path
import argparse
import datetime
import json
import os
import re
import stat
import sys
import tempfile

BASE = Path.home() / '.switchboard'
UNSET = object()

def atomic(path, body, expected=UNSET):
    # Resolve the destination BEFORE replacing it. Replacing a discovery symlink
    # would create a second independently editable memory source.
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_bytes() if path.is_file() else None
    if expected is not UNSET and previous != expected:
        raise RuntimeError('Concurrent edit: ' + str(path))
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if (path.read_bytes() if path.is_file() else None) != previous:
            raise RuntimeError('Concurrent edit: ' + str(path))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def inside(path, root):
    return path == root or root in path.parents

def check(registry):
    data = json.loads(registry.read_text())
    vault = Path(data['vault']).expanduser().resolve()
    errors, roots = [], set()
    for entry in data['scopes']:
        root, canonical = Path(entry['root']), Path(entry['canonical'])
        if str(root) in roots: errors.append('Duplicate scope ' + str(root))
        roots.add(str(root))
        if canonical.name != 'MEMORY.md' or not canonical.is_file():
            errors.append('Missing canonical ' + str(canonical))
            continue
        if not inside(canonical.resolve(), vault): errors.append('Canonical outside vault ' + str(canonical))
        route = root / 'MEMORY.md'
        if not route.is_file() or route.resolve() != canonical.resolve():
            errors.append('Discovery mismatch ' + str(route))
        for name in ['CLAUDE.md', 'AGENTS.md', '.claude/CLAUDE.md']:
            path = root / name
            if path.exists() or path.is_symlink(): errors.append('Redundant project entry ' + str(path))
        recall = Path(entry['recall'])
        index = recall / 'MEMORY.md'
        if not inside(recall.resolve(), vault): errors.append('Recall outside vault ' + str(recall))
        if not index.is_file(): errors.append('Missing recall entry ' + str(index))
        elif len(index.read_text().splitlines()) > 200: errors.append('Recall entry exceeds 200 lines ' + str(index))
        settings = root / '.claude/settings.local.json'
        if settings.is_file():
            configured = json.loads(settings.read_text()).get('autoMemoryDirectory')
            if configured != str(recall): errors.append('Claude write route mismatch ' + str(settings))
    for entry in data.get('stores', []):
        path = Path(entry['legacy'])
        if not path.is_dir() or path.resolve() != Path(entry['canonical']).resolve():
            errors.append('Legacy recall mismatch ' + str(path))
    for path in vault.rglob('*'):
        if path.is_symlink(): errors.append('Symlink inside vault ' + str(path))
    print(json.dumps({'scopes': len(data['scopes']), 'vault': str(vault), 'errors': errors}, indent=2))
    return bool(errors)

# ---- lint: report-only checks on note content. Never edits a note; always exits 0. ----
# Checks: links that resolve to no file in the vault, notes in Claude Code's auto-memory format, device-specific
# paths in shared notes, placeholders in memory notes, and the tag scheme (opt-in: a note that carries a memory/* or
# area/* tag needs exactly one memory/* tag and at most one area/* tag). Folders whose name starts with a dot
# (app settings, trash, .git) and the folder of the registry's device_note are skipped.
DEVICE = re.compile(r'/Users/|/home/\w|(?<![A-Za-z])[A-Z]:[\\/]|(?<![\w.])~/|\blocalhost:\d+|\b127\.0\.0\.1\b')
PLACEHOLDER = re.compile(r'\bonce it exists\b|\bnot built yet\b|\bTODO\b')
LINK = re.compile(r'!?\[\[([^\]]+?)\]\]')

def _frontmatter(text):
    """Return (lines of the YAML block, top-level tags or None), or (None, None) if there is no block."""
    lines = text.split('\n')
    if not lines or lines[0].strip() != '---': return None, None
    try: end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
    except StopIteration: return None, None
    block, tags = lines[1:end], None
    for i, line in enumerate(block):
        if not line.startswith('tags:'): continue
        rest = line[5:].strip()
        if rest.startswith('['): tags = [t.strip().strip('\'"') for t in rest.strip('[]').split(',') if t.strip()]
        elif rest: tags = [rest.strip('\'"')]
        else:
            tags = []
            for item in block[i + 1:]:
                if not item.strip().startswith('- '): break
                tags.append(item.strip()[2:].strip().strip('\'"'))
    return block, tags

def _code_free(lines):
    """Yield (line number, text) outside fenced code blocks, with inline code removed."""
    fence = False
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith(('```', '~~~')): fence = not fence; continue
        if not fence: yield n, re.sub(r'`[^`]*`', '', line)

def _resolver(vault, files):
    paths = {f.relative_to(vault).as_posix().lower() for f in files}
    def resolve(target):
        t = target.strip().replace('\\', '').lower()
        for cand in (t, t + '.md'):
            if cand in paths: return cand
            hit = next((p for p in paths if p.endswith('/' + cand)), None)
            if hit: return hit
        return None
    return resolve

def device_note(reg, vault):
    """The registry's optional device_note (this device's own note, never shared) as a resolved path, or None."""
    note = reg.get('device_note')
    if not note: return None
    note = Path(note).expanduser()
    return (note if note.is_absolute() else vault / note).resolve()

def local_folder(reg, vault):
    """The folder holding the device note, which stays out of shared memory; None when unset or the vault root."""
    note = device_note(reg, vault)
    return note.parent if note and note.parent != vault else None

def lint(registry):
    data = json.loads(Path(registry).read_text(encoding='utf-8'))
    vault = Path(data['vault']).expanduser().resolve()
    note, local = device_note(data, vault), local_folder(data, vault)
    def skipped(p):
        dotted = any(part.startswith('.') for part in p.relative_to(vault).parts[:-1])
        return dotted or p.resolve() == note or (local is not None and inside(p.resolve(), local))
    files = [p for p in vault.rglob('*') if p.is_file() and not skipped(p)]
    local_rel = local.relative_to(vault).as_posix().lower() + '/' if local is not None and inside(local, vault) else None
    notes = sorted(p for p in files if p.suffix == '.md' and not p.name.endswith('.excalidraw.md'))
    resolve = _resolver(vault, files)
    out = []
    def report(label, n, check, msg): out.append(f'{label}:{n}: {check}: {msg}')
    for path in notes:
        rel = path.relative_to(vault).as_posix()
        text = path.read_text(encoding='utf-8', errors='replace')
        lines = text.split('\n')
        block, tags = _frontmatter(text)
        if block is not None:
            for i, line in enumerate(block, 2):
                if line.startswith('metadata:') or 'originSessionId' in line or re.match(r'name:\s*(""|\'\')\s*$', line):
                    report(rel, i, 'frontmatter', 'auto-memory format: ' + line.strip())
        if tags and any(t.startswith(('memory/', 'area/')) for t in tags):
            kinds, areas = [t for t in tags if t.startswith('memory/')], [t for t in tags if t.startswith('area/')]
            if len(kinds) != 1: report(rel, 1, 'tags', f'needs exactly one memory/* tag, has {kinds}')
            if len(areas) > 1: report(rel, 1, 'tags', f'at most one area/* tag, has {areas}')
        tagged = bool(tags) and any(t.startswith('memory/') for t in tags)
        for n, line in _code_free(lines):
            for m in LINK.finditer(line):
                target = m.group(1).split('|')[0].split('#')[0].rstrip('\\').strip()
                if not target or (local_rel and target.lower().startswith(local_rel)): continue  # device-local, not synced
                if not resolve(target): report(rel, n, 'link', '[[' + target + ']] does not resolve')
            if tagged and PLACEHOLDER.search(line): report(rel, n, 'placeholder', PLACEHOLDER.search(line).group(0))
        for n, line in enumerate(lines, 1):
            line = re.sub(r'\]\(file://[^)]*\)', ']()', line)
            if DEVICE.search(line): report(rel, n, 'device-path', line.strip()[:120])
    for line in out: print(line)
    print(f'{len(out)} findings', file=sys.stderr)
    return 0

def init(root, notes, registry):
    root = root.expanduser().resolve()
    registry_bytes = registry.read_bytes()
    reg = json.loads(registry_bytes)
    vault = Path(reg['vault']).expanduser().resolve()
    if not root.is_dir(): raise RuntimeError('Project root does not exist')
    if not (vault / 'MEMORY.md').is_file(): raise RuntimeError('Configured memory vault is unavailable')
    if notes:
        notes = notes.expanduser()
        if not notes.is_absolute(): notes = vault / notes
        notes = notes.resolve()
    else:
        notes = vault / 'Projects' / root.name
    local = local_folder(reg, vault)
    if not inside(notes, vault) or notes == vault or (local is not None and inside(notes, local)):
        raise RuntimeError('Shared project memory must be a folder inside the vault, outside the device note\'s folder')
    for entry in reg['scopes']:
        if Path(entry['root']) == root:
            if Path(entry['recall']) != notes: raise RuntimeError('Scope already has a different memory route')
        elif Path(entry['recall']) == notes:
            raise RuntimeError('Memory folder belongs to another scope; select a distinct --memory-dir')
    canonical = notes / 'MEMORY.md'
    discovery = root / 'MEMORY.md'
    for name in ['CLAUDE.md', 'AGENTS.md', '.claude/CLAUDE.md']:
        path = root / name
        if path.exists() or path.is_symlink(): raise RuntimeError('Consolidate existing instructions first: ' + str(path))
    if discovery.exists() or discovery.is_symlink():
        if not discovery.is_symlink() or discovery.resolve() != canonical:
            raise RuntimeError('Existing instructions need reviewed migration: ' + str(discovery))
    settings = root / '.claude/settings.local.json'
    existing = settings.read_bytes() if settings.exists() else None
    data = json.loads(existing) if existing else {}
    if data.get('autoMemoryDirectory') and Path(data['autoMemoryDirectory']).expanduser().resolve() != notes:
        raise RuntimeError('Existing recall needs reviewed migration')
    # Settings can contain sensitive values. Keep recovery files local and private.
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = registry.parent / 'backups' / 'initializer' / stamp
    backup.mkdir(parents=True, mode=0o700)
    (backup / 'registry.json').write_bytes(registry_bytes)
    os.chmod(backup / 'registry.json', 0o600)
    if existing:
        (backup / 'settings.local.json').write_bytes(existing)
        os.chmod(backup / 'settings.local.json', 0o600)
    (backup / 'routes.json').write_text(json.dumps({'root':str(root),'notes':str(notes)}))
    if not canonical.exists():
        atomic(canonical, '---\ntags: [memory/rules]\n---\n\n# ' + root.name + '\n\nApply the vault-root MEMORY.md and applicable parent rules. Keep verified facts and durable decisions in this folder. Update existing notes; keep task logs and deliverables in the project workspace.\n')
    if not discovery.is_symlink(): discovery.symlink_to(canonical)
    data['autoMemoryDirectory'] = str(notes)
    body = json.dumps(data, indent=2) + '\n'
    if existing != body.encode(): atomic(settings, body, existing)
    if not any(Path(e['root']) == root for e in reg['scopes']):
        reg['scopes'].append({'root':str(root),'canonical':str(canonical),'kind':'project','recall':str(notes)})
        atomic(registry, json.dumps(reg,indent=2)+'\n', registry_bytes)
    print('Memory route: ' + str(discovery) + ' -> ' + str(canonical))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry',type=Path,default=BASE/'registry.json')
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('check')
    sub.add_parser('lint', help='Report note problems; never edits, always exits 0')
    create = sub.add_parser('init')
    create.add_argument('root',type=Path)
    create.add_argument('--memory-dir',type=Path,help='Vault-relative folder, or absolute folder inside the vault')
    args = parser.parse_args()
    try:
        if args.command == 'check': return check(args.registry)
        if args.command == 'lint':
            try: sys.stdout.reconfigure(errors='replace')
            except Exception: pass
            try: return lint(args.registry)
            except Exception as error:
                print('memoryctl lint: could not run: ' + str(error))
                return 0
        init(args.root,args.memory_dir,args.registry)
        return 0
    except Exception as error:
        print(str(error),file=sys.stderr)
        return 1

if __name__ == '__main__': sys.exit(main())
