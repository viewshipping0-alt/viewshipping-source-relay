#!/usr/bin/env python3
"""ViewShipping free official-source snapshot collector.

This collector runs on GitHub Actions and stores validated snapshots in /relay.
It never replaces a previously valid snapshot with an empty or malformed response.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "relay"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/126 Safari/537.36 ViewShipping-Official-Source-Relay/1.0"
)
TIMEOUT = (20, 60)

BRAZIL_PAGE = "https://www.marinha.mil.br/chm/dados-do-segnav-aviso-radio-nautico-tela/avisos-radio-nauticos-e-sar"
PANAMA_PAGE = "https://pancanal.com/en/advisories-to-shipping/"
MPA_PAGE = "https://www.mpa.gov.sg/home?type=port+marine+notices"

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-GB,en;q=0.9,pt-BR;q=0.8",
        "Cache-Control": "no-cache",
    }
)


@dataclass
class Result:
    key: str
    ok: bool
    fetched_at: str | None
    official_url: str
    message: str
    files: dict[str, str]
    sha256: dict[str, str]
    item_count: int | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, path)


def get(url: str, *, accept: str | None = None) -> requests.Response:
    headers = {"Accept": accept} if accept else None
    response = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True, headers=headers)
    response.raise_for_status()
    if not response.content:
        raise ValueError(f"Empty response from {url}")
    return response


def extract_brazil_json_candidates(page_html: str, base_url: str) -> list[str]:
    candidates: list[str] = []
    soup = BeautifulSoup(page_html, "lxml")
    for tag in soup.find_all(["a", "link", "script"]):
        value = tag.get("href") or tag.get("src")
        if value and "avradio" in value.lower() and ".json" in value.lower():
            candidates.append(urljoin(base_url, value.strip()))

    for match in re.findall(r"(?:https?://[^\s\"'<>]+|[^\s\"'<>]+)?avradio[^\s\"'<>]*?\.json(?:\?[^\s\"'<>]*)?", page_html, flags=re.I):
        candidates.append(urljoin(base_url, html_lib.unescape(match)))

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(candidates))


def validate_brazil_json(raw: bytes) -> tuple[Any, int]:
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, (list, dict)):
        raise ValueError("Brazil avradio payload is not a JSON object or array")
    text = json.dumps(payload, ensure_ascii=False)
    if not re.search(r"NAVAREA|AVISO|RADIO|SAR|\d{3,4}/\d{2}", text, flags=re.I):
        raise ValueError("Brazil JSON lacks expected warning markers")
    if isinstance(payload, list):
        count = len(payload)
    else:
        count = max((len(v) for v in payload.values() if isinstance(v, list)), default=len(payload))
    return payload, count


def collect_brazil() -> Result:
    page_response = get(BRAZIL_PAGE, accept="text/html,application/xhtml+xml")
    page_html = page_response.text
    candidates = extract_brazil_json_candidates(page_html, page_response.url)
    errors: list[str] = []

    for candidate in candidates:
        try:
            json_response = get(candidate, accept="application/json,text/plain,*/*")
            _, count = validate_brazil_json(json_response.content)

            # Ensure the snapshot page always exposes the exact validated official JSON link.
            relay_html = page_html
            if candidate not in relay_html:
                relay_html += (
                    "\n<!-- ViewShipping validated official avradio link -->\n"
                    f'<a class="vsi-relay-avradio" href="{html_lib.escape(candidate, quote=True)}">avradio.json</a>\n'
                )

            page_path = RELAY / "brazil/page.html"
            json_path = RELAY / "brazil/avradio.json"
            page_bytes = relay_html.encode("utf-8")
            atomic_write(page_path, page_bytes)
            atomic_write(json_path, json_response.content)
            fetched_at = now_iso()
            return Result(
                key="brazil_chm",
                ok=True,
                fetched_at=fetched_at,
                official_url=BRAZIL_PAGE,
                message=f"Validated official avradio JSON: {candidate}",
                files={"page": "brazil/page.html", "json": "brazil/avradio.json"},
                sha256={"page": sha256_bytes(page_bytes), "json": sha256_bytes(json_response.content)},
                item_count=count,
            )
        except Exception as exc:  # noqa: BLE001 - each candidate is isolated
            errors.append(f"{candidate}: {exc}")

    raise RuntimeError("No valid official avradio JSON discovered. " + " | ".join(errors[-5:]))


def validate_panama(html: str) -> int:
    refs = set(re.findall(r"\bA-\s*\d{1,3}\s*-\s*20\d{2}\b", html, flags=re.I))
    if not refs:
        raise ValueError("No Panama advisory references found")
    if "Fiscal Year" not in html and "Advisory to Shipping" not in html and "Advisories to shipping" not in html:
        raise ValueError("Panama advisory table markers missing")
    return len(refs)


def collect_panama() -> Result:
    response = get(PANAMA_PAGE, accept="text/html,application/xhtml+xml")
    count = validate_panama(response.text)
    data = response.content
    path = RELAY / "panama/advisories.html"
    atomic_write(path, data)
    return Result(
        key="panama_canal",
        ok=True,
        fetched_at=now_iso(),
        official_url=PANAMA_PAGE,
        message=f"Validated {count} advisory references from the official table",
        files={"page": "panama/advisories.html"},
        sha256={"page": sha256_bytes(data)},
        item_count=count,
    )


DATE_RE = re.compile(r"\b(?:Published\s+)?(\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})\b", re.I)
PMN_RE = re.compile(r"PORT\s+MARINE\s+NOTICE\s+NO\.?\s*[-:]?\s*\d{1,3}\s+OF\s+20\d{2}", re.I)


def normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_mpa_notices(page_html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(page_html, "lxml")
    notices: list[dict[str, str]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        title = normalise_space(anchor.get_text(" ", strip=True))
        href = urljoin(base_url, anchor["href"])
        combined = f"{title} {href}"
        if not PMN_RE.search(combined) and "port-marine-notice" not in href.lower():
            continue
        if not PMN_RE.search(title):
            # Some cards put the title in an aria-label/title attribute.
            title = normalise_space(anchor.get("aria-label") or anchor.get("title") or title)
        if not PMN_RE.search(title):
            continue
        if href in seen:
            continue

        date = ""
        parent = anchor
        for _ in range(5):
            parent = parent.parent if parent else None
            if parent is None:
                break
            match = DATE_RE.search(normalise_space(parent.get_text(" ", strip=True)))
            if match:
                date = match.group(1)
                break

        notices.append({"title": title, "url": href, "date": date})
        seen.add(href)

    return notices


def fill_mpa_dates(notices: list[dict[str, str]], limit: int = 15) -> list[dict[str, str]]:
    for notice in notices[:limit]:
        if notice["date"]:
            continue
        try:
            detail = get(notice["url"], accept="text/html,application/xhtml+xml")
            match = DATE_RE.search(normalise_space(BeautifulSoup(detail.text, "lxml").get_text(" ", strip=True)))
            if match:
                notice["date"] = match.group(1)
        except Exception:
            continue
    return notices


def render_mpa_markdown(notices: Iterable[dict[str, str]]) -> str:
    lines = [
        "# Maritime and Port Authority of Singapore — Port Marine Notices",
        "",
        f"Official listing: {MPA_PAGE}",
        "",
    ]
    for item in notices:
        lines.append(f"## [{item['title']}]({item['url']})")
        lines.append("")
        lines.append(f"Published {item['date']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_mpa_normalised_html(notices: Iterable[dict[str, str]]) -> str:
    cards = []
    for item in notices:
        cards.append(
            '<article class="port-marine-notice">'
            f'<h2><a href="{html_lib.escape(item["url"], quote=True)}">{html_lib.escape(item["title"])}</a></h2>'
            f'<p class="published">Published {html_lib.escape(item["date"])}</p>'
            "</article>"
        )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>MPA Port Marine Notices</title></head>"
        "<body><main><h1>Port Marine Notices</h1>"
        + "\n".join(cards)
        + "</main></body></html>"
    )


def fetch_mpa_html() -> tuple[str, str]:
    # Fast path: ordinary request. This frequently already contains the current cards.
    response = get(MPA_PAGE, accept="text/html,application/xhtml+xml")
    notices = extract_mpa_notices(response.text, response.url)
    if notices:
        return response.text, response.url

    # JavaScript fallback for client-rendered responses.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, locale="en-GB")
        page.goto(MPA_PAGE, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(4000)
        content = page.content()
        final_url = page.url
        browser.close()
    return content, final_url


def collect_mpa() -> Result:
    source_html, final_url = fetch_mpa_html()
    notices = extract_mpa_notices(source_html, final_url)
    notices = fill_mpa_dates(notices)
    notices = [item for item in notices if item["date"] and PMN_RE.search(item["title"])]
    if not notices:
        raise ValueError("No dated official Port Marine Notices found")

    # Use normalised snapshots so the WordPress parser receives stable, source-derived markup.
    html_text = render_mpa_normalised_html(notices)
    md_text = render_mpa_markdown(notices)
    html_bytes = html_text.encode("utf-8")
    md_bytes = md_text.encode("utf-8")
    html_path = RELAY / "mpa/notices.html"
    md_path = RELAY / "mpa/notices.md"
    atomic_write(html_path, html_bytes)
    atomic_write(md_path, md_bytes)
    return Result(
        key="mpa_singapore",
        ok=True,
        fetched_at=now_iso(),
        official_url=MPA_PAGE,
        message=f"Validated {len(notices)} dated official Port Marine Notices",
        files={"html": "mpa/notices.html", "markdown": "mpa/notices.md"},
        sha256={"html": sha256_bytes(html_bytes), "markdown": sha256_bytes(md_bytes)},
        item_count=len(notices),
    )


def previous_source_state(key: str) -> dict[str, Any]:
    manifest_path = RELAY / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        state = manifest.get("sources", {}).get(key, {})
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def main() -> int:
    RELAY.mkdir(parents=True, exist_ok=True)
    collectors = [collect_brazil, collect_panama, collect_mpa]
    source_states: dict[str, dict[str, Any]] = {}
    failures = 0

    for collector in collectors:
        name = collector.__name__.replace("collect_", "")
        try:
            result = collector()
            source_states[result.key] = {
                "status": "ok",
                "fetched_at": result.fetched_at,
                "official_url": result.official_url,
                "message": result.message,
                "item_count": result.item_count,
                "files": result.files,
                "sha256": result.sha256,
            }
            print(f"PASS {result.key}: {result.message}")
        except Exception as exc:  # noqa: BLE001 - fail closed and retain old snapshots
            failures += 1
            key_map = {"brazil": "brazil_chm", "panama": "panama_canal", "mpa": "mpa_singapore"}
            key = key_map[name]
            previous = previous_source_state(key)
            if previous:
                previous = dict(previous)
                previous["last_attempt_at"] = now_iso()
                previous["last_attempt_status"] = "failed"
                previous["last_attempt_error"] = str(exc)[:500]
                source_states[key] = previous
                print(f"RETAIN {key}: {exc}", file=sys.stderr)
            else:
                source_states[key] = {
                    "status": "unavailable",
                    "fetched_at": None,
                    "official_url": {
                        "brazil_chm": BRAZIL_PAGE,
                        "panama_canal": PANAMA_PAGE,
                        "mpa_singapore": MPA_PAGE,
                    }[key],
                    "message": str(exc)[:500],
                    "item_count": None,
                    "files": {},
                    "sha256": {},
                    "last_attempt_at": now_iso(),
                    "last_attempt_status": "failed",
                }
                print(f"FAIL {key}: {exc}", file=sys.stderr)

    manifest = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "purpose": "Validated official-source snapshots for the ViewShipping Intelligence Relay Bridge",
        "sources": source_states,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(RELAY / "manifest.json", manifest_bytes)

    # Do not fail the workflow merely because one authority is temporarily unavailable;
    # retained snapshots remain usable. Fail only when all three have never succeeded.
    usable = sum(1 for state in source_states.values() if state.get("status") == "ok" and state.get("fetched_at"))
    if usable == 0:
        print("No usable source snapshot exists yet.", file=sys.stderr)
        return 1
    print(f"Collector completed with {usable}/3 usable sources; {failures} attempt failure(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
