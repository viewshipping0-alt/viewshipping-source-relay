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
BRAZIL_HISTORY = f"https://www.marinha.mil.br/chm/dados-do-segnav-avradio-historico/historico-{datetime.now(timezone.utc).year}"

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



def brazil_direct_json_candidates() -> list[str]:
    now = datetime.now(timezone.utc)
    months = []
    for offset in range(5):
        total = now.year * 12 + (now.month - 1) - offset
        y, m0 = divmod(total, 12)
        months.append((y, m0 + 1))
    urls = []
    for y, m in months:
        ym = f"{y}-{m:02d}"
        folders = [f"{ym}-%5BDEV%5D", f"{ym}-[DEV]", ym]
        for folder in folders:
            base = f"https://www.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/{folder}/"
            for n in range(180, 0, -1):
                urls.append(f"{base}avradio_{n}.json")
    return urls


def probe_brazil_direct_json() -> tuple[str, bytes, int]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    candidates = brazil_direct_json_candidates()
    def attempt(url: str):
        try:
            r = SESSION.get(url, timeout=(6, 15), allow_redirects=True, headers={"Accept": "application/json,text/plain,*/*"})
            if r.status_code != 200 or len(r.content) < 100:
                return None
            _, count = validate_brazil_json(r.content)
            return url, r.content, count
        except Exception:
            return None
    for start in range(0, len(candidates), 120):
        batch = candidates[start:start+120]
        with ThreadPoolExecutor(max_workers=16) as pool:
            futs = {pool.submit(attempt, u): i for i, u in enumerate(batch)}
            found = []
            for f in as_completed(futs):
                result = f.result()
                if result:
                    found.append((futs[f], result))
            if found:
                found.sort(key=lambda x: x[0])
                return found[0][1]
    raise ValueError("No valid official CHM avradio JSON found in recent monthly directories")

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


def browser_html(url: str) -> tuple[str, str]:
    """Fetch a public page with Chromium when ordinary server requests are blocked."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, locale="en-GB")
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        content = page.content()
        final_url = page.url
        browser.close()
    if len(content) < 300:
        raise ValueError(f"Browser returned too little content from {url}")
    return content, final_url


def fetch_html_with_browser_fallback(url: str) -> tuple[str, str]:
    try:
        response = get(url, accept="text/html,application/xhtml+xml")
        return response.text, response.url
    except Exception:
        return browser_html(url)


def brazil_history_to_active_json(page_html: str) -> tuple[bytes, int]:
    """Build parser-compatible JSON from CHM's official current-year history page.

    CHM sometimes blocks data-centre requests to the live landing page. The official
    history page still publishes the warning text. We locate the latest NAVAREA V
    'messages in force' bulletin and retain only the warning references named in it.
    """
    text = BeautifulSoup(page_html, "lxml").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    # CHM pages separate notices with repeated stars. Fall back to reference boundaries.
    blocks = [normalise_space(x) for x in re.split(r"(?:\*\s*){3,}|(?=\n(?:NAVAREA\s+V\s+)?\d{4}/\d{2}\b)", text, flags=re.I) if normalise_space(x)]
    bulletins = []
    for idx, block in enumerate(blocks):
        upper = block.upper()
        if "NAVAREA V" in upper and re.search(r"AVISOS[- ]?RADIO.*EM VIGOR|MESSAGES? IN FORCE", upper):
            refs = []
            nav = re.search(r"NAVAREAS?\s*:\s*(.*?)(?:COSTEIROS?|LOCAIS?|SAR\s*:|$)", block, flags=re.I)
            if nav:
                refs = re.findall(r"\b\d{4}/\d{2}\b", nav.group(1))
            if refs:
                bulletin_ref = re.findall(r"\b\d{4}/\d{2}\b", block)
                bulletins.append((idx, bulletin_ref[0] if bulletin_ref else "", list(dict.fromkeys(refs))))
    if not bulletins:
        raise ValueError("Official Brazil history page contained no NAVAREA V in-force bulletin")

    _, bulletin_reference, active_refs = bulletins[-1]
    records = []
    for ref in active_refs:
        candidates = [b for b in blocks if re.search(rf"(?:^|\s){re.escape(ref)}(?:\s|$)", b) and "EM VIGOR" not in b.upper()]
        if not candidates:
            continue
        # Prefer the richest matching block; prepend source identity for the WP parser.
        body = max(candidates, key=len)
        records.append({"reference": ref, "source": "NAVAREA V", "text": f"NAVAREA V {ref} {body}"})
    if not records:
        raise ValueError("Brazil in-force bulletin was found but its active warning records were not reconstructed")
    payload = {
        "source": "Brazilian Navy Hydrographic Centre",
        "inventory": "NAVAREA V warnings in force",
        "bulletin_reference": bulletin_reference,
        "records": records,
    }
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    validate_brazil_json(raw)
    return raw, len(records)


def collect_brazil() -> Result:
    errors: list[str] = []
    try:
        candidate, raw, count = probe_brazil_direct_json()
        relay_html = f'<!doctype html><html><body><a class="vsi-relay-avradio" href="{html_lib.escape(candidate, quote=True)}">avradio.json</a></body></html>'
        page_bytes = relay_html.encode("utf-8")
        atomic_write(RELAY / "brazil/page.html", page_bytes)
        atomic_write(RELAY / "brazil/avradio.json", raw)
        return Result("brazil_chm", True, now_iso(), BRAZIL_PAGE, f"Validated official CHM JSON: {candidate}", {"page": "brazil/page.html", "json": "brazil/avradio.json"}, {"page": sha256_bytes(page_bytes), "json": sha256_bytes(raw)}, count)
    except Exception as exc:
        errors.append(f"direct JSON discovery: {exc}")
    page_html = ""
    page_url = BRAZIL_PAGE
    try:
        page_html, page_url = fetch_html_with_browser_fallback(BRAZIL_PAGE)
        candidates = extract_brazil_json_candidates(page_html, page_url)
        for candidate in candidates:
            try:
                try:
                    json_response = get(candidate, accept="application/json,text/plain,*/*")
                    raw = json_response.content
                except Exception:
                    # Chromium can retrieve the official JSON even where a data-centre HTTP
                    # client is rejected. Read the rendered body as text.
                    rendered, _ = browser_html(candidate)
                    body = BeautifulSoup(rendered, "lxml").get_text("", strip=True)
                    raw = body.encode("utf-8")
                _, count = validate_brazil_json(raw)
                relay_html = page_html
                if candidate not in relay_html:
                    relay_html += (
                        "\n<!-- ViewShipping validated official avradio link -->\n"
                        f'<a class="vsi-relay-avradio" href="{html_lib.escape(candidate, quote=True)}">avradio.json</a>\n'
                    )
                page_bytes = relay_html.encode("utf-8")
                atomic_write(RELAY / "brazil/page.html", page_bytes)
                atomic_write(RELAY / "brazil/avradio.json", raw)
                return Result("brazil_chm", True, now_iso(), BRAZIL_PAGE,
                              f"Validated official avradio JSON: {candidate}",
                              {"page": "brazil/page.html", "json": "brazil/avradio.json"},
                              {"page": sha256_bytes(page_bytes), "json": sha256_bytes(raw)}, count)
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
    except Exception as exc:
        errors.append(f"live page: {exc}")

    # Official current-year history fallback. This is not treated as the whole
    # historical archive: only references named by its latest in-force bulletin
    # are emitted into the snapshot.
    try:
        history_html, history_url = fetch_html_with_browser_fallback(BRAZIL_HISTORY)
        raw, count = brazil_history_to_active_json(history_html)
        relay_html = (
            history_html
            + "\n<!-- ViewShipping official-history active inventory -->\n"
            + '<a class="vsi-relay-avradio" href="avradio.json">avradio.json</a>\n'
        )
        page_bytes = relay_html.encode("utf-8")
        atomic_write(RELAY / "brazil/page.html", page_bytes)
        atomic_write(RELAY / "brazil/avradio.json", raw)
        return Result("brazil_chm", True, now_iso(), history_url,
                      f"Reconstructed {count} active NAVAREA V warnings from the latest official in-force bulletin",
                      {"page": "brazil/page.html", "json": "brazil/avradio.json"},
                      {"page": sha256_bytes(page_bytes), "json": sha256_bytes(raw)}, count)
    except Exception as exc:
        errors.append(f"history fallback: {exc}")
    raise RuntimeError("Brazil CHM live and official-history fallbacks failed. " + " | ".join(errors[-6:]))


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


DATE_RE = re.compile(r"\b(?:Published\s+)?(\d{1,2}\s+[A-Za-z]{3,9})(?:\s+(20\d{2}))?\b", re.I)
PMN_RE = re.compile(r"(?:PORT\s+MARINE\s+NOTICE\s+NO\.?|PMN)\s*[-:]?\s*(\d{1,3})\s+OF\s+(20\d{2})", re.I)


def normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_mpa_heading(value: str) -> tuple[str, str, str] | None:
    value = normalise_space(value)
    match = re.search(r"(?:PORT\s+MARINE\s+NOTICE\s+NO\.?|PMN)\s*[-:]?\s*(\d{1,3})\s+OF\s+(20\d{2})\s*[-–—:]?\s*(.*)", value, flags=re.I)
    if not match:
        return None
    number, year, subject = match.group(1), match.group(2), match.group(3).strip()
    title = f"PORT MARINE NOTICE NO. {int(number)} OF {year}"
    if subject:
        title += f" - {subject}"
    return title, number, year



def extract_mpa_detail_urls(page_html: str, base_url: str) -> list[str]:
    decoded = html_lib.unescape(page_html).replace('\\/', '/')
    urls = []
    soup = BeautifulSoup(decoded, 'lxml')
    for a in soup.find_all('a', href=True):
        href = urljoin(base_url, a['href'])
        if '/media-centre/details/' in href.lower():
            urls.append(href)
    pattern = r"(?:https?://www\.mpa\.gov\.sg)?(/media-centre/details/[A-Za-z0-9%_().~!$&*,;=:@+\-/]+)"
    for match in re.findall(pattern, decoded, flags=re.I):
        urls.append(urljoin(base_url, match.rstrip('\"<>),.;]')))
    return list(dict.fromkeys(urls))[:200]


def parse_mpa_detail_page(detail_html: str, url: str) -> dict[str, str] | None:
    soup = BeautifulSoup(detail_html, 'lxml')
    text = normalise_space(soup.get_text(' ', strip=True))
    heading = ''
    for tag in soup.find_all(['h1','h2','h3','title']):
        candidate = normalise_space(tag.get_text(' ', strip=True))
        if parse_mpa_heading(candidate):
            heading = candidate
            break
    if not heading:
        slug = url.rsplit('/',1)[-1].replace('-', ' ')
        if parse_mpa_heading(slug):
            heading = slug
    parsed = parse_mpa_heading(heading)
    if not parsed:
        m = PMN_RE.search(text)
        if not m:
            return None
        parsed = (f"PORT MARINE NOTICE NO. {int(m.group(1))} OF {m.group(2)}", m.group(1), m.group(2))
    title, _, year = parsed
    m = re.search(r'Published\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})', text, flags=re.I)
    if m:
        date = normalise_space(m.group(1))
    else:
        d = DATE_RE.search(text)
        date = f"{d.group(1)} {d.group(2) or year}" if d else ''
    return {'title': title, 'url': url, 'date': date} if date else None

def extract_mpa_notices(page_html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(page_html, "lxml")
    notices: list[dict[str, str]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"])
        labels = [
            anchor.get_text(" ", strip=True),
            anchor.get("aria-label") or "",
            anchor.get("title") or "",
        ]
        parsed = None
        for label in labels:
            parsed = parse_mpa_heading(label)
            if parsed:
                break
        if not parsed and "port-marine-notice" in href.lower():
            slug_label = href.rsplit("/", 1)[-1].replace("-", " ")
            parsed = parse_mpa_heading(slug_label)
        if not parsed or href in seen:
            continue
        title, _, year = parsed
        date = ""
        parent = anchor
        for _ in range(6):
            parent = parent.parent if parent else None
            if parent is None:
                break
            context = normalise_space(parent.get_text(" ", strip=True))
            for match in DATE_RE.finditer(context):
                day_month = match.group(1)
                explicit_year = match.group(2) or year
                # Avoid accidentally selecting a date embedded in the notice title.
                if day_month and explicit_year:
                    date = f"{day_month} {explicit_year}"
                    break
            if date:
                break
        notices.append({"title": title, "url": href, "date": date})
        seen.add(href)
    return notices


def fill_mpa_dates(notices: list[dict[str, str]], limit: int = 20) -> list[dict[str, str]]:
    for notice in notices[:limit]:
        if notice["date"]:
            continue
        try:
            detail_html, _ = fetch_html_with_browser_fallback(notice["url"])
            text = normalise_space(BeautifulSoup(detail_html, "lxml").get_text(" ", strip=True))
            year_match = PMN_RE.search(notice["title"])
            year = year_match.group(2) if year_match else str(datetime.now(timezone.utc).year)
            match = DATE_RE.search(text)
            if match:
                notice["date"] = f"{match.group(1)} {match.group(2) or year}"
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
    return fetch_html_with_browser_fallback(MPA_PAGE)


def collect_mpa() -> Result:
    source_html, final_url = fetch_mpa_html()
    notices = extract_mpa_notices(source_html, final_url)
    known = {item["url"] for item in notices}
    detail_urls = extract_mpa_detail_urls(source_html, final_url)
    for url in detail_urls:
        if url in known:
            continue
        try:
            detail_html, _ = fetch_html_with_browser_fallback(url)
            item = parse_mpa_detail_page(detail_html, url)
            if item:
                notices.append(item)
                known.add(url)
        except Exception:
            continue
    notices = fill_mpa_dates(notices, limit=60)
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
    if usable < 3:
        print(f"Collector incomplete: only {usable}/3 sources usable.", file=sys.stderr)
        return 1
    print("Collector completed with 3/3 usable sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
