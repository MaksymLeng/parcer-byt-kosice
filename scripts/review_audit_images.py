"""Make browser screenshots of image-only posts for human review (no OCR inference)."""
import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path

from flatwatch.facebook import _playwright, extract_post_id, parse_facebook_published


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output-dir', required=True)
    args = p.parse_args()
    rows = [r for line in Path(args.input).read_text().splitlines() for r in json.loads(line)['posts']]
    rows = [r for r in rows if not r['text'] and r.get('image')]
    rows = [r for r in rows if not parse_facebook_published(r['timestamp']) or
            datetime.now(timezone.utc) - datetime.fromisoformat(parse_facebook_published(r['timestamp'])) < timedelta(hours=49)]
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with _playwright()() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 1500})
        for start in range(0, len(rows), 6):
            cards = ''.join(f'<section><h3>{escape(extract_post_id(r["url"]) or "?")} · {escape(r["timestamp"])}</h3><img src="{escape(r["image"], quote=True)}"></section>' for r in rows[start:start+6])
            page.set_content('<style>body{margin:0;display:grid;grid-template-columns:1fr 1fr;font:16px sans-serif}section{height:490px;border:1px solid #777}h3{margin:8px}img{width:590px;height:435px;object-fit:contain}</style>'+cards)
            page.wait_for_timeout(1500)
            target = out / f'images-{start//6+1}.png'
            page.screenshot(path=str(target), full_page=True)
            print(target, flush=True)
        browser.close()


if __name__ == '__main__':
    main()
