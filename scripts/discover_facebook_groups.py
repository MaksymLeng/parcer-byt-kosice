from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from urllib.parse import quote, urlsplit, urlunsplit

from flatwatch.app import load_config
from flatwatch.facebook import _playwright


DEFAULT_QUERIES = (
    "prenájom byty Košice",
    "byty Košice",
    "Košice apartments rent",
    "ubytovanie Košice",
    "spolubývanie Košice",
    "аренда квартиры Кошице",
    "оренда квартири Кошице",
    "Українці в Кошице житло",
)


def canonical_group_url(url: str) -> str | None:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "groups":
        return None
    return urlunsplit(("https", "www.facebook.com", f"/groups/{parts[1]}/", "", ""))


def discover(profile_dir: str, queries: tuple[str, ...], scrolls: int) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    sync_playwright = _playwright()
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(profile_dir, headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        for query in queries:
            page.goto(
                f"https://www.facebook.com/search/groups/?q={quote(query)}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            time.sleep(5)
            for _ in range(max(1, scrolls)):
                rows = page.locator('a[href*="/groups/"]').evaluate_all(
                    r"""
                    links => links.map(link => {
                      const card = link.closest('div[role="article"]') || link.parentElement;
                      return {
                        url: link.href || '',
                        name: (link.innerText || '').trim(),
                        context: ((card && card.innerText) || '').trim().slice(0, 500)
                      };
                    })
                    """
                )
                for row in rows:
                    url = canonical_group_url(row.get("url", ""))
                    if not url:
                        continue
                    current = found.setdefault(
                        url,
                        {"url": url, "name": "", "context": "", "query": query},
                    )
                    if len(row.get("name", "")) > len(current["name"]):
                        current["name"] = row["name"]
                    if len(row.get("context", "")) > len(current["context"]):
                        current["context"] = row["context"]
                page.mouse.wheel(0, 1800)
                time.sleep(2)
        context.close()
    return sorted(found.values(), key=lambda row: (row["name"].casefold(), row["url"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--scrolls", type=int, default=3)
    args = parser.parse_args()
    config = load_config(args.config)
    facebook = config["sources"]["facebook"]
    configured = set(facebook.get("groups", []))
    rows = discover(facebook["profile_dir"], DEFAULT_QUERIES, args.scrolls)
    for row in rows:
        row["configured"] = "yes" if row["url"] in configured else "no"
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
