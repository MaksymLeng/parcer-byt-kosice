"""Read-only collection for human review; never sends Telegram messages."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from flatwatch.app import load_config
from flatwatch.facebook import fetch_facebook_posts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--scrolls', type=int, default=8)
    parser.add_argument('--limit-groups', type=int)
    args = parser.parse_args()
    config = load_config('config.json')['sources']['facebook']
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.with_suffix('.jsonl').exists():
        raise SystemExit('Refusing to overwrite an existing audit')
    posts = fetch_facebook_posts(
        config['profile_dir'], config['groups'][:args.limit_groups], True,
        max_posts_per_group=80, scroll_rounds=args.scrolls, chronological=True,
        audit_path=str(path.with_suffix('.jsonl')),
    )
    path.write_text(json.dumps([asdict(p) for p in posts], ensure_ascii=False, indent=2))
    print(f'Audit saved: {len(posts)} posts, {path}', flush=True)


if __name__ == '__main__':
    main()
