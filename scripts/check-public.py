#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail closed on common secrets/runtime files; print locations, never values."""
import argparse, io, ipaddress, os, pathlib, re, subprocess, sys
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--staged', action='store_true', help='scan the Git index, exactly as prepared for commit')
args = parser.parse_args()
root = pathlib.Path(__file__).resolve().parents[1]
result = subprocess.run(['git', '-C', str(root), 'ls-files', '--stage', '-z'], capture_output=True)
if result.returncode:
    sys.exit('Unable to inspect tracked files; run inside the Git repository.')
index_entries = {}
for record in result.stdout.split(b'\0'):
    if not record: continue
    header, name = record.split(b'\t', 1)
    mode, oid, _stage = header.split()
    rel = os.fsdecode(name)
    index_entries[rel] = (mode.decode(), oid.decode())
paths = [root / name for name in index_entries]
rules = {
    'provider credential': r'\b(?:sk-[A-Za-z0-9_-]{24,}|cfut_[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})\b',
    'dotted provider credential': r'\b[a-f0-9]{32}\.[A-Za-z0-9]{16,}\b',
    'private key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    # A relative application asset such as ./home/assets/ is not a user home.
    'personal absolute path': r'(?<!\.)/(?:Users|home)/[A-Za-z0-9_.-]+/|\b[A-Za-z]:\\(?:Users|个人)\\[^\\/\s]+\\|~/' + r'dafeiyu' + r'-test/',
    'private email': r'\b[\w.+-]+@(?:gmail|qq|hotmail|outlook)\.com\b',
    'control URL token': r'https?://127\.0\.0\.1:\d+/#\S{16,}',
}
authorized_notice_emails = {
    'chrisgao0312' + '@qq.com',
    '2117286908' + '@qq.com',
}
authorized_notice_files = {
    'integrations/wechat-pi/core/LICENSE',
    'integrations/wechat-pi/core/docs/NOTICE.md',
}
documentation_networks = tuple(ipaddress.ip_network(value) for value in (
    '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))
ipv4_pattern = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
private_email_pattern = rules['private email']
failures=[]
scan_paths=[]
for p in paths:
    rel=p.relative_to(root)
    if args.staged:
        if index_entries[rel.as_posix()][0] == '120000': failures.append((str(rel),0,'symlink'));continue
    elif p.is_symlink(): failures.append((str(rel),0,'symlink'));continue
    if any(x in {'.build','dist','config','private','local'} for x in rel.parts) or p.suffix in {'.log','.jsonl','.key','.pem','.p12','.db','.har'} or p.name in {'.env','qq-state.json','state.json'}:
        failures.append((str(rel),0,'runtime or credential file'));continue
    if p.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}: continue  # Human visual review is required separately.
    scan_paths.append(p)

staged_blobs = {}
if args.staged and scan_paths:
    oids = list(dict.fromkeys(index_entries[p.relative_to(root).as_posix()][1] for p in scan_paths))
    batch = subprocess.run(['git', '-C', str(root), 'cat-file', '--batch'],
                           input=('\n'.join(oids) + '\n').encode(), capture_output=True)
    if batch.returncode:
        sys.exit('Unable to read staged files from the Git index.')
    stream = io.BytesIO(batch.stdout)
    for requested_oid in oids:
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b'blob':
            sys.exit('Unable to read a staged source blob from the Git index.')
        size = int(header[2])
        blob = stream.read(size)
        if len(blob) != size or stream.read(1) != b'\n':
            sys.exit('Incomplete staged source blob from the Git index.')
        staged_blobs[requested_oid] = blob

for p in scan_paths:
    rel=p.relative_to(root)
    try:
        if args.staged:
            data = staged_blobs[index_entries[rel.as_posix()][1]]
        else:
            data=p.read_bytes()
    except FileNotFoundError: failures.append((str(rel),0,'tracked file missing'));continue
    try: text=data.decode('utf-8')
    except UnicodeDecodeError: failures.append((str(rel),0,'unreviewed binary'));continue
    for number,line in enumerate(text.splitlines(),1):
        for label,pattern in rules.items():
            if not re.search(pattern,line): continue
            if (label == 'private email' and rel.as_posix() in authorized_notice_files):
                emails = re.findall(private_email_pattern, line)
                author_row = (line.lstrip().startswith('@') if rel.name == 'LICENSE'
                              else line.lstrip().startswith('|'))
                if len(emails) == 1 and emails[0] in authorized_notice_emails and author_row:
                    continue
            failures.append((str(rel),number,label))
        if rel.parts[:2] == ('integrations', 'pi-console'):
            for match in ipv4_pattern.finditer(line):
                try:
                    address = ipaddress.ip_address(match.group(0))
                except ValueError:
                    failures.append((str(rel),number,'invalid IPv4 literal'))
                    continue
                if address.is_loopback or address.is_unspecified:
                    continue
                if any(address in network for network in documentation_networks):
                    continue
                failures.append((str(rel),number,'non-documentation IPv4 literal'))
for path,line,label in failures: print(f'{path}:{line}: {label}')
print(f'Checked {len(paths)} public files ({"index" if args.staged else "working tree"}); findings={len(failures)}. Images and commit metadata require separate review.')
sys.exit(bool(failures))
