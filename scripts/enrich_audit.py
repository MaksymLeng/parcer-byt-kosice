"""Verify ambiguous housing posts from a saved audit on their own pages."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
from datetime import datetime, timedelta, timezone
from flatwatch.app import load_config
from flatwatch.facebook import _playwright, read_post_detail, extract_post_id, parse_facebook_published
from flatwatch.core import Listing, fold, clean_space, find_price, is_wanted_post


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--post-id', action='append', help='Verify only these post IDs')
    args = p.parse_args()
    cfg = load_config('config.json')
    raw_posts = [r for line in Path(args.input).read_text().splitlines() for r in json.loads(line)['posts']]
    seen = set()
    out = []
    with _playwright()() as p:
        c = p.chromium.launch_persistent_context(cfg['sources']['facebook']['profile_dir'], headless=True)
        c.set_default_timeout(2000)
        page = c.pages[0]
        for raw in raw_posts:
            post_id = extract_post_id(raw['url'])
            if not post_id or post_id in seen:
                continue
            if args.post_id and post_id not in args.post_id:
                continue
            seen.add(post_id)
            text = raw.get('text') or raw.get('raw_text', '')
            housing = re.search(r'\b(?:byt\w*|izb\w*|room\w*|flat|apartment|квартир\w*|кімнат\w*|комнат\w*|подселен\w*|підсел\w*)', fold(text))
            published = parse_facebook_published(raw.get('timestamp', ''))
            recent = not published or (datetime.now(timezone.utc) - datetime.fromisoformat(published)) < timedelta(hours=49)
            if housing and recent and not is_wanted_post(text):
                try:
                    raw = read_post_detail(page, raw)
                    print('Verified', post_id, raw['timestamp'], raw['text'][:90], flush=True)
                except Exception as exc:
                    if 'not logged in' in str(exc):
                        raise
                    raw['detail_error'] = type(exc).__name__
                    print('Detail failed', post_id, type(exc).__name__, flush=True)
            out.append(raw)
            Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        c.close()
    print('Saved', len(out), args.output)


if __name__ == '__main__':
    main()
