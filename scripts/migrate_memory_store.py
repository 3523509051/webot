"""Read-only report by default; writes require --apply and a verified backup manifest."""
import argparse
import json
import hashlib
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from plugins.astrbot_plugin_group_activation.migration import migrate


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--data-dir',required=True,type=Path)
    mode=p.add_mutually_exclusive_group();mode.add_argument('--dry-run',action='store_true');mode.add_argument('--apply',action='store_true')
    p.add_argument('--backup-dir',type=Path)
    args=p.parse_args()
    if args.apply and (args.backup_dir is None or not (args.backup_dir/'manifest.json').is_file()):
        p.error('--apply requires --backup-dir with manifest.json')
    if args.apply:
        base=args.backup_dir.resolve()
        manifest=json.loads((base/'manifest.json').read_text(encoding='utf-8'))
        for name,expected in manifest.items():
            item=(base/name).resolve()
            if not item.is_relative_to(base) or not item.is_file() or hashlib.sha256(item.read_bytes()).hexdigest()!=expected:
                p.error('backup manifest verification failed')
    report=migrate(args.data_dir,dry_run=not args.apply)
    print(json.dumps(report,ensure_ascii=True,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
