from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time

from flatwatch.app import load_config
from flatwatch.facebook import _playwright


CANDIDATES = (
    ("Bývanie a prenájom Košice", "https://www.facebook.com/groups/byvanieaprenajomKOSICE/"),
    ("Bývanie a prenájom Košice®️", "https://www.facebook.com/groups/byvanieprenajomkosice/"),
    ("Bývanie Košice - Byty, Domy, Pozemky", "https://www.facebook.com/groups/449027048994430/"),
    ("Flats for rent in Kosice AURUM", "https://www.facebook.com/groups/1504467486458300/"),
    ("Flats for rent Kosice", "https://www.facebook.com/groups/506547809420165/"),
    ("Kosice - Flats for rent", "https://www.facebook.com/groups/Kosice.Flats.For.Rent/"),
    ("Kosice apartments for rent", "https://www.facebook.com/groups/291188588728158/"),
    ("KOŠICE aktuálne / Podnájom / Prenájom", "https://www.facebook.com/groups/434046700580924/"),
    ("KOŠICE PODNAJOM / prenájom / ubytko", "https://www.facebook.com/groups/kosicebyty/"),
    ("Košice/podnájom/prenájom/byty", "https://www.facebook.com/groups/212789922252163/"),
    ("Rent Apartment & Room Košice", "https://www.facebook.com/groups/rentapartmentkosice/"),
    ("Košice a východné SR - prenájom", "https://www.facebook.com/groups/171065246825800/"),
    ("Nehnuteľnosti Košice mesto a okolie", "https://www.facebook.com/groups/1065008985593930/"),
    ("Украинцы в Кошице", "https://www.facebook.com/groups/748973053199583/"),
    ("Українці в Кошице - оголошення", "https://www.facebook.com/groups/253227908490242/"),
    ("Українці в Кошіце", "https://www.facebook.com/groups/1004268415045737/"),
    ("Українці Košice, Slovensko", "https://www.facebook.com/groups/ukrainianskosice/"),
)

JOIN_RE = re.compile(
    r"^(?:Присоединиться(?: к группе)?|Join group|Join|Pridať sa|Pripojiť sa|Приєднатися)$",
    re.I,
)
SUBMIT_RE = re.compile(r"^(?:Отправить|Submit|Odoslať|Подати|Potvrdiť)$", re.I)
QUESTIONS_RE = re.compile(
    r"^(?:Ответить на вопросы|Answer questions|Odpovedať na otázky|Відповісти на запитання)$",
    re.I,
)


def membership_state(page) -> str:
    button_texts = page.locator('[role="button"]').all_inner_texts()
    button_text = "\n".join(button_texts).casefold()
    if any(
        marker in button_text
        for marker in ("запрос отправлен", "отменить запрос", "pending", "žiadosť odoslaná")
    ):
        return "pending"
    if any(JOIN_RE.fullmatch(value.strip()) for value in button_texts):
        return "not_joined"
    if any(
        marker in button_text
        for marker in ("в группе", "вы присоединились", "joined", "členom", "člen skupiny")
    ):
        return "joined"
    return "unknown"


def complete_questions(page) -> str:
    question_buttons = page.get_by_role("button", name=QUESTIONS_RE)
    if question_buttons.count():
        question_buttons.first.click()
        time.sleep(1)
    dialogs = page.locator('div[role="dialog"]')
    if dialogs.count():
        dialog = dialogs.last
        answer = "I am looking for a long-term apartment rental in Košice."
        for selector in ("textarea", 'input[type="text"]'):
            fields = dialog.locator(selector)
            for index in range(fields.count()):
                field = fields.nth(index)
                if field.is_visible() and not field.input_value().strip():
                    field.fill(answer)
        checkboxes = dialog.get_by_role("checkbox")
        for index in range(checkboxes.count()):
            checkbox = checkboxes.nth(index)
            if checkbox.is_visible() and not checkbox.is_checked():
                checkbox.check()
        submits = dialog.get_by_role("button", name=SUBMIT_RE)
        if submits.count() and submits.last.is_enabled():
            submits.last.click()
            time.sleep(2)
        else:
            state = membership_state(page)
            return state if state == "pending" else "questions_need_manual_review"
    return membership_state(page)


def join_group(page) -> str:
    buttons = page.get_by_role("button", name=JOIN_RE)
    if not buttons.count():
        return "no_join_button"
    buttons.first.click()
    time.sleep(2)
    return complete_questions(page)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--join", action="store_true")
    parser.add_argument("--only", help="regular expression matched against name or URL")
    args = parser.parse_args()
    config = load_config(args.config)
    profile_dir = config["sources"]["facebook"]["profile_dir"]
    sync_playwright = _playwright()
    results = []
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(profile_dir, headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        candidates = CANDIDATES
        if args.only:
            only = re.compile(args.only, re.I)
            candidates = tuple(
                item for item in candidates if only.search(item[0]) or only.search(item[1])
            )
        for expected_name, url in candidates:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(4)
            state = membership_state(page)
            action = "none"
            if args.join and state == "not_joined":
                action = join_group(page)
                state = membership_state(page)
            elif args.join and state == "pending":
                action = complete_questions(page)
                state = membership_state(page)
            page.mouse.wheel(0, 1200)
            time.sleep(2)
            posts = page.locator('div[role="article"]')
            headings = page.locator("h1")
            visible_name = headings.first.inner_text().strip() if headings.count() else expected_name
            result = {
                "expected_name": expected_name,
                "visible_name": visible_name,
                "url": url,
                "state": state,
                "action": action,
                "visible_articles": posts.count(),
                "buttons": [
                    text.strip()
                    for text in page.locator('[role="button"]').all_inner_texts()
                    if text.strip()
                ][-20:],
            }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
