from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import os
import random
import re
import time
from urllib.parse import urlsplit, urlunsplit

from .core import Listing, clean_space, find_price, fold, is_wanted_post


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAYWRIGHT_BROWSER_DIR = PROJECT_ROOT / "browser-profile" / "ms-playwright"
BODY_SELECTOR = '[data-ad-preview="message"], [data-ad-comet-preview="message"], [data-ad-rendering-role="story_message"]'
MORE_RE = re.compile(r'^(Ещё|Еще|See more|Zobraziť viac|Показати більше|Докладніше)$', re.I)


def expand_post_bodies(page):
    # Facebook uses both data attributes and plain dir=auto spans, especially for translations.
    for button in page.query_selector_all('[role="article"] [role="button"]'):
        try:
            if not MORE_RE.fullmatch(button.inner_text().strip()):
                continue
            top_level = button.evaluate('b => { const a=b.closest("[role=article]"); return a && !a.parentElement.closest("[role=article]"); }')
            if top_level:
                button.click(timeout=1500)
        except Exception:
            pass


def read_post_detail(page, raw: dict) -> dict:
    """Open an ambiguous post itself, away from comments in the group feed."""
    raw = dict(raw)
    page.goto(raw['url'].split('?')[0], wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(2500)
    if 'login' in page.url or 'checkpoint' in page.url or page.locator('input[name="email"]').count():
        raise RuntimeError('Facebook session is not logged in; run facebook-login first')
    bodies = page.locator(BODY_SELECTOR)
    if bodies.count():
        body = bodies.first
        more = body.get_by_role('button', name=MORE_RE)
        if more.count():
            try:
                more.first.click(timeout=1500)
                page.wait_for_timeout(300)
            except Exception:
                pass
        raw['text'] = body.inner_text()
    post_id = extract_post_id(raw['url'])
    for anchor in page.query_selector_all('a[href*="/posts/"], a[href*="/permalink/"]'):
        try:
            href = anchor.get_attribute('href') or ''
            if 'comment_id=' in href or extract_post_id(href) != post_id:
                continue
            label = anchor.get_attribute('aria-label') or anchor.inner_text()
            if not parse_facebook_published(label):
                continue
            anchor.hover(timeout=1500)
            page.wait_for_timeout(900)
            tooltip = ' '.join(page.locator('[role="tooltip"]').all_inner_texts())
            raw['timestamp'] = tooltip if parse_facebook_published(tooltip) else (anchor.get_attribute('aria-label') or label)
            break
        except Exception:
            pass
    raw['detail_checked'] = True
    return raw


def extract_post_id(url: str) -> str | None:
    patterns = (
        r"/groups/[^/]+/(?:posts|permalink)/(\d+)",
        r"[?&]story_fbid=(\d+)",
        r"[?&]multi_permalinks=(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def parse_facebook_published(text: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now(timezone.utc)
    prefix = fold(clean_space(text[:220]))
    months = (
        ('januar', 'january', 'январ', 'січ'), ('februar', 'february', 'феврал', 'лют'),
        ('marec', 'marca', 'march', 'март', 'берез'), ('april', 'апрел', 'квіт'),
        ('maj', 'may', 'мая', 'май', 'трав'), ('jun', 'june', 'июн', 'черв'),
        ('jul', 'july', 'июл', 'лип'), ('august', 'август', 'серп'),
        ('september', 'septembra', 'сен', 'сентябр', 'сентябрь', 'верес'),
        ('oktober', 'oktobra', 'october', 'октябр', 'жовт'),
        ('november', 'novembra', 'ноябр', 'листоп'), ('december', 'decembra', 'декабр', 'груд'),
    )
    for month, names in enumerate(months, 1):
        name = '(?:' + '|'.join(names) + r')\w*'
        match = re.search(r'(?<!\d)(\d{1,2})\.?\s+' + name + r'(?:\s+(20\d{2}))?', prefix)
        english = re.search(name + r'\s+(\d{1,2})(?:,?\s+(20\d{2}))?', prefix)
        match = match or english
        if match:
            clock = re.search(r'(\d{1,2}):(\d{2})(?:\s*(am|pm))?', prefix)
            hour = int(clock[1]) if clock else 0
            if clock and clock[3]:
                hour = hour % 12 + (12 if clock[3] == 'pm' else 0)
            try:
                dt = datetime(int(match[2] or now.year), month, int(match[1]), hour,
                              int(clock[2]) if clock else 0, tzinfo=ZoneInfo('Europe/Bratislava'))
                if not match[2] and dt > now + timedelta(days=1):
                    dt = dt.replace(year=dt.year - 1)
                return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
            except ValueError:
                return None
    if any(word in prefix for word in ("только что", "just now", "práve teraz", "щойно")):
        return now.isoformat(timespec="seconds")
    relative = re.search(
        r"(?<!\w)(\d+)\s*(мин\.?|хв\.?|min\.?|ч\.?|год\.?|h|дн\.?|д\.?|d|нед\.?|тиж\.?|w)(?!\w)",
        prefix,
        re.I,
    )
    if relative:
        value = int(relative.group(1))
        unit = relative.group(2).rstrip(".")
        if unit in {"мин", "хв", "min"}:
            delta = timedelta(minutes=value)
        elif unit in {"ч", "год", "h"}:
            delta = timedelta(hours=value)
        elif unit in {"дн", "д", "d"}:
            delta = timedelta(days=value)
        else:
            delta = timedelta(weeks=value)
        return (now - delta).isoformat(timespec="seconds")
    if any(word in prefix for word in ('вчера', 'учора', 'yesterday', 'vcera', 'сегодня', 'today', 'dnes', 'сьогодні')):
        days = 1 if any(word in prefix for word in ('вчера', 'учора', 'yesterday', 'vcera')) else 0
        dt = now.astimezone(ZoneInfo('Europe/Bratislava')) - timedelta(days=days)
        clock = re.search(r'(\d{1,2}):(\d{2})', prefix)
        if clock:
            dt = dt.replace(hour=int(clock[1]), minute=int(clock[2]), second=0)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    year = re.search(r"\b(20\d{2})\s*(?:г\.)?", prefix)
    if year and int(year.group(1)) < now.year:
        return f"{year.group(1)}-01-01T00:00:00+00:00"
    return None


def _playwright():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PLAYWRIGHT_BROWSER_DIR))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Facebook support is not installed. Run: "
            "python3 -m pip install -r requirements-facebook.txt && "
            "PLAYWRIGHT_BROWSERS_PATH=browser-profile/ms-playwright "
            "python3 -m playwright install chromium"
        ) from exc
    return sync_playwright


def facebook_login(profile_dir: str) -> None:
    sync_playwright = _playwright()
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(profile_dir, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
        input("Войди в Facebook в открытом окне, затем нажми Enter здесь: ")
        context.close()


def fetch_facebook_posts(
    profile_dir: str,
    groups: list[str],
    headless: bool = False,
    *,
    max_posts_per_group: int = 25,
    scroll_rounds: int = 1,
    chronological: bool = False,
    audit_path: str | None = None,
) -> list[Listing]:
    sync_playwright = _playwright()
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    listings: list[Listing] = []
    seen_post_ids: set[str] = set()
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(profile_dir, headless=headless)
        context.set_default_timeout(2000)
        page = context.pages[0] if context.pages else context.new_page()
        detail_page = None
        for group_index, group_url in enumerate(groups, 1):
            target_url = group_url
            if chronological:
                separator = "&" if "?" in target_url else "?"
                target_url = f"{target_url}{separator}sorting_setting=CHRONOLOGICAL"
            page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(random.uniform(4.0, 7.0))
            if "login" in page.url or 'checkpoint' in page.url or page.locator('input[name="email"]').count():
                context.close()
                raise RuntimeError("Facebook session is not logged in; run facebook-login first")
            group_posts = 0
            audit_posts = []
            for round_index in range(max(1, scroll_rounds)):
                # Only expand the post body, never comments or unrelated UI buttons.
                expand_post_bodies(page)
                bodies = page.locator('[data-ad-preview="message"], [data-ad-comet-preview="message"], [data-ad-rendering-role="story_message"]')
                for body in bodies.all():
                    more = body.get_by_role('button', name=re.compile(r'^(Ещё|Еще|See more|Zobraziť viac|Показати більше|Докладніше)$', re.I))
                    try:
                        if more.count():
                            more.first.click(timeout=1500)
                    except Exception:
                        pass
                # Hover coarse timestamps to reveal the publication's exact date.
                for anchor in page.query_selector_all('a[href*="/posts/"], a[href*="/permalink/"]'):
                    try:
                        href = anchor.get_attribute('href') or ''
                        if 'comment_id=' in href or extract_post_id(href) in seen_post_ids:
                            continue
                        label = anchor.inner_text().strip()
                        if label and not re.search(r'\d+\s*(?:ч\.?|h|мин\.?|min)(?!\w)', label):
                            anchor.hover(timeout=1000)
                            page.wait_for_timeout(800)
                    except Exception:
                        pass
                raw_posts = page.locator('div[role="article"]').evaluate_all(
                    r"""
                    (articles, limit) => articles.filter(a => !a.parentElement.closest('[role="article"]')).slice(0, limit).map(article => {
                      const links = [...article.querySelectorAll('a[href]')];
                      const permalink = links.find(a =>
                        !/[?&](comment_id|reply_comment_id)=/.test(a.href) &&
                        (/\/groups\/[^/]+\/(posts|permalink)\//.test(a.href) ||
                        /[?&](story_fbid|multi_permalinks)=/.test(a.href))
                      );
                      const body = article.querySelector('[data-ad-preview="message"], [data-ad-comet-preview="message"], [data-ad-rendering-role="story_message"]') ||
                        [...article.querySelectorAll('[dir="auto"]')].find(e => e.innerText.length > 40 && e.closest('[role="article"]') === article && !e.closest('[role="textbox"], a'));
                      const image = [...article.querySelectorAll('img[src]')].find(i => i.width >= 200);
                      return {
                        text: body ? body.innerText : '',
                        raw_text: article.innerText || '',
                        timestamp: permalink ? (permalink.getAttribute('aria-label') || permalink.innerText) : '',
                        url: permalink ? permalink.href : '',
                        image: image ? image.src : ''
                      };
                    })
                    """,
                    max_posts_per_group,
                )
                for raw in raw_posts:
                    url = raw.get("url", "")
                    post_id = extract_post_id(url)
                    text = clean_space(raw.get("text", ""))
                    if not post_id or post_id in seen_post_ids:
                        continue
                    raw_text = text or raw.get('raw_text', '')
                    published = parse_facebook_published(raw.get('timestamp', ''))
                    recent = not published or (datetime.now(timezone.utc) - datetime.fromisoformat(published)) < timedelta(hours=49)
                    housing = re.search(r'\b(?:byt\w*|izb\w*|room\w*|flat|apartment|квартир\w*|кімнат\w*|комнат\w*|подселен\w*|підсел\w*)', fold(raw_text))
                    incomplete = not text or re.search(r'(?:Ещё|Еще|See more|Zobraziť viac|Показати більше)\s*$', text)
                    if housing and recent and incomplete and not is_wanted_post(raw_text):
                        if detail_page is None:
                            detail_page = context.new_page()
                        raw = read_post_detail(detail_page, raw)
                        text = clean_space(raw.get('text', ''))
                    audit_posts.append(raw)
                    seen_post_ids.add(post_id)
                    if not text:
                        continue
                    group_posts += 1
                    parsed_url = urlsplit(url)
                    canonical_url = urlunsplit(
                        (parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", "")
                    )
                    listings.append(
                        Listing(
                            source="facebook",
                            source_id=post_id,
                            url=canonical_url,
                            title=text[:140],
                            description=text,
                            price_eur=find_price(text),
                            location="Košice",
                            published_at=parse_facebook_published(raw.get('timestamp', '')),
                            image_url=raw.get("image") or None,
                        )
                    )
                if group_posts >= max_posts_per_group or round_index + 1 >= scroll_rounds:
                    break
                page.mouse.wheel(0, 1800)
                time.sleep(random.uniform(1.5, 2.5))
            print(f'Facebook group {group_index}/{len(groups)}: {group_url} posts={group_posts}', flush=True)
            if audit_path:
                path = Path(audit_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps({'group': group_url, 'url': page.url, 'posts': audit_posts,
                                             'page_summary': page.locator('body').inner_text()[:1200]}, ensure_ascii=False) + '\n')
            time.sleep(random.uniform(2.0, 4.0))
        context.close()
    return listings
