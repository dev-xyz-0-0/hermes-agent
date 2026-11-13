#!/usr/bin/env python3
"""Runner for backfill_relationships.py — loads API key then calls it."""
import os, re, sys

# Find DEEPSEEK_API_KEY from common config locations
for p in ['~/.bash_rtk', '~/.env', '~/.hermes/.env', '~/.hermes/config.yaml']:
    fp = os.path.expanduser(p)
    if os.path.exists(fp):
        with open(fp, encoding='utf-8', errors='ignore') as f:
            content = f.read()
        m = re.search(r'DEEPSEEK_API_KEY[=: ]+"?([A-Za-z0-9_\-]+)"?', content)
        if m:
            os.environ['DEEPSEEK_API_KEY'] = m.group(1)
            print(f"Key loaded from {p}")
            break

if not os.environ.get('DEEPSEEK_API_KEY'):
    print("ERROR: Could not find DEEPSEEK_API_KEY")
    sys.exit(1)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.backfill_relationships import main

# Allow limiting via env or pass through args
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--limit', type=int, default=0)
parser.add_argument('--dry-run', action='store_true')
args, _ = parser.parse_known_args()

sys.argv = ['backfill']
if args.limit:
    sys.argv.extend(['--limit', str(args.limit)])
if args.dry_run:
    sys.argv.append('--dry-run')

main()
