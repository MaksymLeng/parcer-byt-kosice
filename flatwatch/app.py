from __future__ import annotations

import argparse
import getpass
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time

from .bazos import BazosClient
from .core import Listing, matches_filters
from .facebook import facebook_login, fetch_facebook_posts
from .history import collect_history, write_history_report
from .jozue import JozueClient
from .nehnutelnosti import NehnutelnostiClient
from .storage import ListingStore
from .telegram import TelegramNotifier


ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    database = Path(config["database"])
    if not database.is_absolute():
        config["database"] = str(ROOT / database)
    facebook = config["sources"].get("facebook", {})
    profile = Path(facebook.get("profile_dir", "browser-profile/facebook"))
    if not profile.is_absolute():
        facebook["profile_dir"] = str(ROOT / profile)
    return config


def notifier_from_config(config: dict, required: bool) -> TelegramNotifier | None:
    telegram = config["telegram"]
    token = os.getenv(telegram["bot_token_env"], "")
    chat_id = os.getenv(telegram["chat_id_env"], "")
    placeholders = {"", "replace_me"}
    if token in placeholders or chat_id in placeholders:
        if required:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return None
    return TelegramNotifier(token, chat_id)


def collect(
    config: dict,
    include_bazos: bool = True,
    include_facebook: bool = True,
    include_jozue: bool = True,
    include_nehnutelnosti: bool = True,
) -> list[Listing]:
    collected: list[Listing] = []
    sources = config["sources"]
    bazos = sources.get("bazos", {})
    if include_bazos and bazos.get("enabled", True):
        bazos_ids: set[str] = set()
        urls = bazos.get("urls") or [bazos["url"]]
        client = BazosClient(max_pages=int(bazos.get("max_pages", 15)))
        for url in urls:
            for listing in client.fetch(url):
                if listing.source_id not in bazos_ids:
                    collected.append(listing)
                    bazos_ids.add(listing.source_id)

    facebook = sources.get("facebook", {})
    if include_facebook and facebook.get("enabled", False):
        collected.extend(
            fetch_facebook_posts(
                facebook["profile_dir"],
                facebook.get("groups", []),
                bool(facebook.get("headless", False)),
                scroll_rounds=int(facebook.get('scroll_rounds', 4)),
                chronological=bool(facebook.get('chronological', True)),
            )
        )
    jozue = sources.get("jozue", {})
    if include_jozue and jozue.get("enabled", False):
        collected.extend(JozueClient(max_pages=int(jozue.get("max_pages", 10))).fetch(jozue["url"]))
    nehnutelnosti = sources.get("nehnutelnosti", {})
    if include_nehnutelnosti and nehnutelnosti.get("enabled", False):
        collected.extend(NehnutelnostiClient(max_pages=int(nehnutelnosti.get("max_pages", 15))).fetch(nehnutelnosti["url"]))
    return collected


def enabled_sources(config: dict, *, include_bazos: bool = True,
                    include_facebook: bool = True, include_jozue: bool = True,
                    include_nehnutelnosti: bool = True) -> set[str]:
    included = {"bazos": include_bazos, "facebook": include_facebook, "jozue": include_jozue,
                "nehnutelnosti": include_nehnutelnosti}
    return {source for source, include in included.items()
            if include and config["sources"].get(source, {}).get("enabled", source == "bazos")}


def run_check(
    config: dict,
    *,
    notify_existing: bool = False,
    no_telegram: bool = False,
    include_bazos: bool = True,
    include_facebook: bool = True,
    include_jozue: bool = True,
    include_nehnutelnosti: bool = True,
) -> dict:
    raw = collect(
        config,
        include_bazos=include_bazos,
        include_facebook=include_facebook,
        include_jozue=include_jozue,
        include_nehnutelnosti=include_nehnutelnosti,
    )
    store = ListingStore(config["database"])
    try:
        active_sources = enabled_sources(config, include_bazos=include_bazos,
                                         include_facebook=include_facebook, include_jozue=include_jozue,
                                         include_nehnutelnosti=include_nehnutelnosti)
        baseline_sources = {
            source for source in active_sources if not store.is_source_initialized(source)
        }
        if notify_existing:
            baseline_sources.clear()
        matched: list[Listing] = []
        rejected: dict[str, int] = {}
        rejected_items: list[tuple[Listing, str]] = []
        detail_client = BazosClient()
        for listing in raw:
            if listing.source == "bazos" and store.is_pending(listing):
                try:
                    listing = detail_client.enrich(listing)
                except Exception as exc:
                    print(f"Detail fetch failed for {listing.url}: {exc}", file=sys.stderr)
            accepted, reason, enriched = matches_filters(listing, config["filters"])
            if accepted:
                matched.append(enriched)
            else:
                rejected[reason] = rejected.get(reason, 0) + 1
                rejected_items.append((listing, reason))

        new_items: list[Listing] = []
        for listing in matched:
            existed = store.exists(listing)
            inserted, duplicate = store.add_if_unique(listing)
            if duplicate and not existed:
                rejected["duplicate"] = rejected.get("duplicate", 0) + 1
            if inserted:
                new_items.append(listing)
        for listing, reason in rejected_items:
            store.mark_filtered(listing.source, listing.source_id, reason)
        # Keep actual texts and decisions for the next human audit, including rejects.
        for source in active_sources:
            decisions = [{'reason': 'matched', 'listing': asdict(item)} for item in matched if item.source == source]
            decisions += [{'reason': reason, 'listing': asdict(item)} for item, reason in rejected_items if item.source == source]
            report = Path(config['database']).parent / f'latest-{source}-review.json'
            temporary = report.with_suffix('.tmp')
            temporary.write_text(json.dumps(decisions, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(report)
        if baseline_sources:
            for listing in new_items:
                if listing.source in baseline_sources:
                    store.mark_notified(listing, "BASELINE")
        for source in active_sources:
            store.mark_source_initialized(source)

        notifier = None if no_telegram else notifier_from_config(config, required=False)
        delivered = 0
        if notifier and active_sources:
            for listing in store.pending(active_sources):
                notifier.send_listing(listing)
                store.mark_notified(listing)
                delivered += 1

        preferred = sum(1 for listing in matched if listing.preferred_area)
        return {
            "fetched": len(raw),
            "matched": len(matched),
            "preferred": preferred,
            "new": len(new_items),
            "delivered": delivered,
            "baseline_created": bool(baseline_sources),
            "baseline_sources": sorted(baseline_sources),
            "telegram_configured": notifier is not None,
            "rejected": rejected,
            "matches": matched,
        }
    finally:
        store.close()


def print_result(result: dict) -> None:
    print(
        f"Fetched {result['fetched']}; matched {result['matched']}; "
        f"preferred {result['preferred']}; new {result['new']}; "
        f"sent {result['delivered']}."
    )
    if result["baseline_created"]:
        print(
            "Initial baseline saved for "
            + ", ".join(result["baseline_sources"])
            + "; existing ads were not sent."
        )
    if not result["telegram_configured"]:
        print("Telegram is not configured; collection still completed.")
    if result["rejected"]:
        print("Rejected:", json.dumps(result["rejected"], ensure_ascii=False, sort_keys=True))
    for listing in result["matches"]:
        star = "★" if listing.preferred_area else "·"
        print(f"{star} {listing.price_eur} € | {listing.title} | {listing.url}")


def telegram_setup(config: dict) -> int:
    telegram = config["telegram"]
    token = os.getenv(telegram["bot_token_env"], "")
    if token in {"", "replace_me"}:
        print("Set TELEGRAM_BOT_TOKEN in .env first.", file=sys.stderr)
        return 2
    temporary = TelegramNotifier(token, "0")
    bot = temporary.validate()
    print(f"Bot connected: @{bot.get('username', 'unknown')}")
    chats: dict[str, str] = {}
    for update in temporary.updates():
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat", {})
        if "id" in chat:
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or "chat"
            chats[str(chat["id"])] = label
    if not chats:
        print("Send /start to the bot, wait a few seconds, then run this command again.")
        return 1
    print("Available chats:")
    for chat_id, label in chats.items():
        print(f"  TELEGRAM_CHAT_ID={chat_id}  ({label})")
    return 0


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    if output and output[-1] != "":
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    path.chmod(0o600)


def telegram_configure(config: dict) -> int:
    token = getpass.getpass("Telegram bot token (input is hidden): ").strip()
    if not token:
        print("Token is empty.", file=sys.stderr)
        return 2
    temporary = TelegramNotifier(token, "0")
    bot = temporary.validate()
    print(f"Connected to @{bot.get('username', 'unknown')}.")
    input("Send /start to this bot in Telegram, then press Enter here: ")
    chats: dict[str, str] = {}
    for update in temporary.updates():
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat", {})
        if "id" in chat:
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or "chat"
            chats[str(chat["id"])] = label
    if not chats:
        print("No chat found. Send /start and run telegram-configure again.", file=sys.stderr)
        return 1
    chat_id, label = list(chats.items())[-1]
    telegram = config["telegram"]
    update_env_file(
        ROOT / ".env",
        {
            telegram["bot_token_env"]: token,
            telegram["chat_id_env"]: chat_id,
        },
    )
    print(f"Saved chat {label} ({chat_id}) to .env with mode 600.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Košice apartment watcher")
    parser.add_argument("--config", default="config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="collect once")
    check.add_argument("--notify-existing", action="store_true")
    check.add_argument("--no-telegram", action="store_true")
    check.add_argument("--no-bazos", action="store_true")
    check.add_argument("--no-facebook", action="store_true")
    check.add_argument("--no-jozue", action="store_true")
    check.add_argument("--no-nehnutelnosti", action="store_true")

    run = subparsers.add_parser("run", help="poll continuously")
    run.add_argument("--notify-existing", action="store_true")
    run.add_argument("--no-bazos", action="store_true")
    run.add_argument("--no-facebook", action="store_true")
    run.add_argument("--no-jozue", action="store_true")
    run.add_argument("--no-nehnutelnosti", action="store_true")
    run.add_argument("--interval", type=int)

    history = subparsers.add_parser(
        "history", help="export matching listings published during the last N hours"
    )
    history.add_argument("--hours", type=float, default=48)
    history.add_argument("--output", default="data/last-48h.md")
    history.add_argument("--facebook-scrolls", type=int, default=8)
    history.add_argument("--no-bazos", action="store_true")
    history.add_argument("--no-facebook", action="store_true")
    history.add_argument("--no-jozue", action="store_true")
    history.add_argument("--no-nehnutelnosti", action="store_true")

    subparsers.add_parser("telegram-setup", help="validate bot and discover chat id")
    subparsers.add_parser("telegram-configure", help="securely configure token and chat id")
    subparsers.add_parser("facebook-login", help="open a persistent Facebook login window")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    try:
        if args.command == "telegram-setup":
            return telegram_setup(config)
        if args.command == "telegram-configure":
            return telegram_configure(config)
        if args.command == "facebook-login":
            facebook_login(config["sources"]["facebook"]["profile_dir"])
            return 0
        if args.command == "history":
            listings, rejected, fetched = collect_history(
                config,
                hours=max(1, args.hours),
                include_bazos=not args.no_bazos,
                include_facebook=not args.no_facebook,
                include_jozue=not args.no_jozue,
                include_nehnutelnosti=not args.no_nehnutelnosti,
                facebook_scrolls=max(1, args.facebook_scrolls),
            )
            output_path = Path(args.output)
            if not output_path.is_absolute():
                output_path = ROOT / output_path
            write_history_report(
                listings,
                output_path,
                hours=max(1, args.hours),
                fetched=fetched,
                rejected=rejected,
            )
            print(
                f"Fetched {fetched}; matched {len(listings)}; "
                f"report saved to {output_path}."
            )
            for listing in listings:
                print(f"{listing.price_eur} € | {listing.title} | {listing.url}")
            return 0
        if args.command == "check":
            result = run_check(
                config,
                notify_existing=args.notify_existing,
                no_telegram=args.no_telegram,
                include_bazos=not args.no_bazos,
                include_facebook=not args.no_facebook,
                include_jozue=not args.no_jozue,
                include_nehnutelnosti=not args.no_nehnutelnosti,
            )
            print_result(result)
            return 0
        if args.command == "run":
            from .health import SourceHealth
            health = SourceHealth(config)
            active = enabled_sources(config, include_bazos=not args.no_bazos,
                                     include_facebook=not args.no_facebook, include_jozue=not args.no_jozue,
                                     include_nehnutelnosti=not args.no_nehnutelnosti)
            if not active:
                raise RuntimeError("No enabled sources selected")
            health_source = next(iter(active)) if len(active) == 1 else "all"
            first = True
            interval = max(60, int(args.interval or config.get("poll_interval_seconds", 300)))
            while True:
                try:
                    result = run_check(
                        config,
                        notify_existing=args.notify_existing if first else True,
                        include_bazos=not args.no_bazos,
                        include_facebook=not args.no_facebook,
                        include_jozue=not args.no_jozue,
                        include_nehnutelnosti=not args.no_nehnutelnosti,
                    )
                    print_result(result)
                    health.success(health_source)
                except Exception as exc:
                    print(f"Check failed: {exc}", file=sys.stderr)
                    health.failure(health_source, exc)
                first = False
                time.sleep(interval)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
