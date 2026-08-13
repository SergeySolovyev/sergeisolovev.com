#!/usr/bin/env python3
"""news_publisher — news.json is the source of truth; this renders it to the site.

Replaces the droplet script of the same name, lost when the DigitalOcean box went
away. Contract is unchanged and still documented in news.json's own _meta block:

    news.json  ->  index.html  (newest N, injected between the NEWS markers)
               ->  news.html   (all items, fully static + JSON-LD ItemList)

Run it from anywhere; paths resolve relative to this file's parent repo:

    python tools/news_publisher.py            # write
    python tools/news_publisher.py --check    # validate only, touch nothing

Images are optional. An item may carry:

    "images": [{"src": "news/assets/x.jpg", "alt": "...", "caption": "..."}]

news.html renders them as a dependency-free scroll-snap carousel. The homepage
still renders news through the React component in index.html, which does not yet
know about images — the data is injected regardless, so wiring the homepage
carousel is a separate index.html edit, not a change here.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEWS_JSON = ROOT / "news.json"
INDEX = ROOT / "index.html"
NEWS_PAGE = ROOT / "news.html"

HOMEPAGE_LIMIT = 5
SITE = "https://sergeisolovev.com"

NEWS_START = "<!--NEWS:START-->"
NEWS_END = "<!--NEWS:END-->"


# ---------------------------------------------------------------- validation

PLACEHOLDERS = ("xx", "todo", "tbd", "fixme", "lorem", "{{", "}}", "<placeholder>", "n/a")

SCHEMA_KEYS = {"date", "display", "title", "desc", "source", "url", "images", "pinned"}
IMAGE_KEYS = {"src", "alt", "caption"}


def _walk_strings(value, path="item"):
    """Yield (path, string) for every string anywhere in the item."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, f"{path}.{k}" if path != "item" else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings(v, f"{path}[{i}]")

SLUG_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}[-_]|\.html?$", re.IGNORECASE)
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def validate(item: dict, index: int) -> tuple[list[str], list[str]]:
    """Check one item. Returns (blocking problems, non-blocking warnings).

    This is the gate the old pipeline never really had. It shipped a post titled
    with a raw filename and another carrying the literal placeholder
    "XX (e.g., lightsec, ANUBIS)" in its <title>. Both are mechanically
    detectable — which is the whole point: an evaluator is code, not an agent.
    The previous system delegated exactly these checks to a 10 KB LLM critic
    prompt and it caught neither.

    Split rationale: block only where a false positive is nearly impossible and
    the cost of being wrong is public embarrassment. Everything softer prints a
    warning and still publishes, because a gate that cries wolf gets bypassed.
    """
    problems: list[str] = []
    warnings: list[str] = []

    for field in ("date", "title", "desc"):
        if not str(item.get(field, "")).strip():
            problems.append(f"missing required field: {field}")

    title = str(item.get("title", "")).strip()
    desc = str(item.get("desc", "")).strip()

    # --- blocking ---

    haystack = f"{title} {desc}".lower()
    for token in PLACEHOLDERS:
        if token in ("xx", "n/a"):
            # avoid matching inside ordinary words: require a standalone token
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", haystack):
                problems.append(f"unfilled placeholder in text: {token!r}")
        elif token in haystack:
            problems.append(f"unfilled placeholder in text: {token!r}")

    if title and (SLUG_LIKE.search(title) or " " not in title):
        problems.append("title looks like a slug or filename, not a headline")

    # The site is English-only. Russian organisation names get transliterated
    # ("Мособлбанк" -> "Mosoblbank"), not pasted in Cyrillic — an international
    # reader cannot even pronounce the string, let alone search for it.
    #
    # Walk EVERY string in the item, not a hand-listed set of fields. An earlier
    # version checked only title/desc/source/alt/caption, so a stray field could
    # carry Cyrillic straight to the page unnoticed — the check has to be blind to
    # which keys exist, or it silently stops covering the ones added later.
    for path, value in _walk_strings(item):
        if CYRILLIC.search(value):
            problems.append(f"Cyrillic in {path} — the site is English-only")

    # Anything outside the published schema is almost always a leaked working note
    # (a local Windows path, an internal id). Those must not reach the page.
    unknown = set(item) - SCHEMA_KEYS
    if unknown:
        problems.append(f"fields not in the news schema: {', '.join(sorted(unknown))}")

    raw_date = str(item.get("date", "")).strip()
    if raw_date:
        try:
            parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            problems.append(f"date is not YYYY-MM-DD: {raw_date!r}")
        else:
            if parsed > date.today():
                problems.append(f"date is in the future: {raw_date}")

    for img in item.get("images", []) or []:
        if not str(img.get("src", "")).strip():
            problems.append("image entry without src")
        elif not (ROOT / img["src"]).exists():
            problems.append(f"image file not found: {img['src']}")
        if not str(img.get("alt", "")).strip():
            problems.append(f"image without alt text: {img.get('src', '?')}")

    # --- warnings ---

    if desc and len(desc) < 40:
        warnings.append(f"desc is only {len(desc)} chars — thin for a news item")

    if not str(item.get("source", "")).strip():
        warnings.append("no source — the card will show only a date")

    return problems, warnings


# ------------------------------------------------------------------- loading

def load_items() -> list[dict]:
    data = json.loads(NEWS_JSON.read_text(encoding="utf-8"))
    items = data.get("items", [])
    if not isinstance(items, list):
        sys.exit("news.json: 'items' must be a list")
    return items


def normalise(item: dict) -> dict:
    """Fill in what can be derived, so hand-edited entries stay minimal."""
    out = dict(item)
    raw = str(out.get("date", "")).strip()
    if raw and not str(out.get("display", "")).strip():
        try:
            out["display"] = datetime.strptime(raw, "%Y-%m-%d").strftime("%b %-d, %Y")
        except ValueError:
            pass
        except Exception:
            # Windows strftime has no %-d
            out["display"] = datetime.strptime(raw, "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
    out.setdefault("source", "")
    out.setdefault("url", "")
    out.setdefault("images", [])
    return out


def sort_newest_first(items: list[dict]) -> list[dict]:
    """Sort by date descending, so file order can never silently break the page.

    _meta tells a human to add newest to the top; trusting that by hand is how
    lists drift. Sorting here makes the instruction a convenience, not a contract.
    """
    return sorted(items, key=lambda i: str(i.get("date", "")), reverse=True)


def homepage_selection(items: list[dict]) -> list[dict]:
    """Newest items first, pinned items appended at the END, capped at HOMEPAGE_LIMIT.

    A purely chronological feed buries the thing you most want a visitor to see:
    the ACI eFX Summit moderation is from December 2025 and had already fallen off
    the homepage entirely by the time nine newer entries were added. Pinning keeps
    it on the homepage permanently.

    Pinned goes LAST, not first: at the top, a December 2025 item reads as "the
    latest news is from last year" — exactly the wrong signal. At the bottom it
    reads as a standing highlight below the fresh entries, and the top of the block
    keeps showing the newest date.

    The archive at /news.html stays strictly chronological — pinning is a homepage
    editorial choice, not a claim that the event is recent.
    """
    pinned = [i for i in items if i.get("pinned")]
    rest = [i for i in items if not i.get("pinned")]
    if len(pinned) >= HOMEPAGE_LIMIT:
        return pinned[:HOMEPAGE_LIMIT]
    return rest[: HOMEPAGE_LIMIT - len(pinned)] + pinned


# ------------------------------------------------------------------ renderers

def render_home_cards(items: list[dict]) -> str:
    """Homepage news, as plain HTML.

    This used to inject a `const NEWS = [...]` array for a React component to
    render. That meant the news existed only after JavaScript ran — invisible to
    search engines and to answer engines, which is the opposite of what a news
    feed is for. The page is static now, so the publisher writes the markup.
    """
    e = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    out = []
    for it in items:
        meta = e(it.get("display", ""))
        if it.get("source"):
            meta += " &middot; " + e(it["source"])

        parts = [
            '          <article class="glass news-item">',
            f'            <div class="label">{meta}</div>',
            f'            <h3>{e(it["title"])}</h3>',
            f'            <p>{e(it["desc"])}</p>',
        ]

        images = it.get("images") or []
        if images:
            parts.append('            <div class="shots">')
            for img in images:
                cap = e(img.get("caption", ""))
                parts.append(
                    "              <figure>"
                    f'<img src="/{e(img["src"])}" alt="{e(img.get("alt", ""))}" loading="lazy" />'
                    + (f"<figcaption>{cap}</figcaption>" if cap else "")
                    + "</figure>"
                )
            parts.append("            </div>")

        if it.get("url"):
            parts.append(
                f'            <a class="more" href="{e(it["url"])}" rel="noopener">Details &rarr;</a>'
            )

        parts.append("          </article>")
        out.append("\n".join(parts))
    return "\n".join(out)


def inject_index(items: list[dict]) -> bool:
    src = INDEX.read_text(encoding="utf-8")
    if NEWS_START not in src or NEWS_END not in src:
        sys.exit(f"index.html: markers {NEWS_START} / {NEWS_END} not found")

    block = f"{NEWS_START}\n{render_home_cards(homepage_selection(items))}\n          {NEWS_END}"
    pattern = re.escape(NEWS_START) + r".*?" + re.escape(NEWS_END)
    updated = re.sub(pattern, lambda _m: block, src, count=1, flags=re.DOTALL)

    if updated == src:
        return False
    INDEX.write_text(updated, encoding="utf-8")
    return True


def render_cards(items: list[dict]) -> str:
    chunks = []
    for it in items:
        e = lambda s: html.escape(str(s), quote=True)  # noqa: E731
        meta = e(it.get("display", ""))
        if it.get("source"):
            meta += " &middot; " + e(it["source"])

        parts = [
            '    <article class="card">',
            f'      <div class="meta">{meta}</div>',
            f'      <h2>{e(it["title"])}</h2>',
            f'      <p>{e(it["desc"])}</p>',
        ]

        images = it.get("images") or []
        if images:
            parts.append('      <div class="shots">')
            for img in images:
                cap = e(img.get("caption", ""))
                parts.append(
                    '        <figure>'
                    f'<img src="/{e(img["src"])}" alt="{e(img.get("alt", ""))}" loading="lazy" />'
                    + (f'<figcaption>{cap}</figcaption>' if cap else "")
                    + '</figure>'
                )
            parts.append("      </div>")

        if it.get("url"):
            parts.append(f'      <a href="{e(it["url"])}" rel="noopener">Details &rarr;</a>')

        parts.append("    </article>")
        chunks.append("\n".join(parts))
    return "\n".join(chunks)


def render_jsonld(items: list[dict]) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "News & Appearances - Sergei Solovev",
        "url": f"{SITE}/news.html",
        "numberOfItems": len(items),
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": n,
                "item": {
                    "@type": "NewsArticle",
                    "headline": it["title"],
                    "datePublished": it["date"],
                    "author": {
                        "@type": "Person",
                        "@id": f"{SITE}/#person",
                        "name": "Sergei Solovev",
                    },
                },
            }
            for n, it in enumerate(items, start=1)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>News &amp; Appearances | Sergei Solovev</title>
  <meta name="description" content="Talks, panels and appearances by Sergei Solovev - researcher at the intersection of TradFi, AI and DeFi." />
  <link rel="canonical" href="https://sergeisolovev.com/news.html" />
  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="Sergei Solovev" />
  <meta property="og:title" content="News &amp; Appearances | Sergei Solovev" />
  <meta property="og:description" content="Talks, panels and appearances by Sergei Solovev (TradFi to AI to DeFi)." />
  <meta property="og:url" content="https://sergeisolovev.com/news.html" />
  <meta property="og:image" content="__OGIMAGE__" />
  <meta name="twitter:card" content="summary_large_image" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Manrope:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <script type="application/ld+json">
__JSONLD__
  </script>
  <style>
    :root{--bg:#f4f3ef;--text:#111827;--dim:#374151;--muted:#6b7280;--tradfi:#334155;--ai:#b38a5a;--defi:#1f2937;--card:rgba(255,255,255,.7);--stroke:rgba(15,23,42,.12)}
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--text);font-family:"Manrope",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.55}
    .wrap{max-width:820px;margin:0 auto;padding:32px 20px 64px}
    .top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:8px}
    .brand{font-family:"Instrument Serif",serif;font-size:1.9rem;color:var(--defi);text-decoration:none}
    nav a{font-family:"JetBrains Mono",monospace;font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);text-decoration:none;margin-left:18px}
    nav a:hover{color:var(--defi)}
    .eyebrow{font-family:"JetBrains Mono",monospace;font-size:.7rem;letter-spacing:.2em;text-transform:uppercase;color:var(--muted);margin-top:28px}
    h1{font-family:"Instrument Serif",serif;font-size:2.9rem;line-height:1.05;margin:.2rem 0 1.6rem}
    .card{background:var(--card);border:1px solid var(--stroke);border-radius:18px;padding:22px 24px;margin-bottom:16px;backdrop-filter:blur(8px)}
    .meta{font-family:"JetBrains Mono",monospace;font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
    .card h2{font-size:1.2rem;line-height:1.3;margin:.5rem 0 .4rem}
    .card p{color:var(--dim);font-size:.96rem;margin:.3rem 0 0}
    .card a{display:inline-block;margin-top:.7rem;font-family:"JetBrains Mono",monospace;font-size:.74rem;color:var(--tradfi);text-decoration:none}
    .card a:hover{color:var(--defi)}
    .shots{display:flex;gap:12px;overflow-x:auto;scroll-snap-type:x mandatory;margin:16px -4px 0;padding:0 4px 6px;-webkit-overflow-scrolling:touch}
    .shots figure{flex:0 0 82%;scroll-snap-align:center;margin:0}
    .shots img{width:100%;height:auto;display:block;border-radius:12px;border:1px solid var(--stroke)}
    .shots figcaption{font-size:.8rem;color:var(--muted);margin-top:6px;line-height:1.4}
    @media (min-width:700px){.shots figure{flex:0 0 48%}}
    footer{margin-top:40px;border-top:1px solid var(--stroke);padding-top:18px;font-family:"JetBrains Mono",monospace;font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
    footer a{color:var(--tradfi);text-decoration:none}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <a class="brand" href="/">Sergei Solovev</a>
      <nav><a href="/">Home</a><a href="/blog/">Blog</a><a href="/publications.html">Publications</a></nav>
    </div>
    <div class="eyebrow">Latest</div>
    <h1>News &amp; Appearances</h1>
__CARDS__
    <footer>&copy; __YEAR__ Sergei Solovev &middot; TradFi &rarr; AI &rarr; DeFi &middot; <a href="/">Home</a></footer>
  </div>
</body>
</html>
"""


def render_news_page(items: list[dict]) -> bool:
    og = f"{SITE}/sergey%20solovyev.png"
    for it in items:
        if it.get("images"):
            og = f"{SITE}/" + it["images"][0]["src"].replace(" ", "%20")
            break

    page = (
        PAGE.replace("__JSONLD__", render_jsonld(items))
        .replace("__CARDS__", render_cards(items))
        .replace("__OGIMAGE__", og)
        .replace("__YEAR__", str(date.today().year))
    )
    if NEWS_PAGE.exists() and NEWS_PAGE.read_text(encoding="utf-8") == page:
        return False
    NEWS_PAGE.write_text(page, encoding="utf-8")
    return True


# ----------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    args = ap.parse_args()

    items = [normalise(i) for i in load_items()]

    blocked = False
    for n, item in enumerate(items):
        problems, warnings = validate(item, n)
        label = f"[{item.get('date', '?')}] {str(item.get('title', ''))[:56]!r}"
        if problems:
            blocked = True
            print(f"BLOCKED  {label}")
            for p in problems:
                print(f"         - {p}")
        for w in warnings:
            print(f"WARN     {label}\n         - {w}")

    if blocked:
        print("\nNothing written. Fix the items above, or relax the rules in validate().")
        return 1

    items = sort_newest_first(items)
    home = homepage_selection(items)
    pinned = [i for i in home if i.get("pinned")]
    print(f"OK  {len(items)} item(s); {len(home)} on the homepage")
    for it in home:
        mark = "PIN " if it.get("pinned") else "    "
        pics = f"  [{len(it.get('images', []))} img]" if it.get("images") else ""
        print(f"    {mark}{it['date']}  {it['title'][:58]}{pics}")
    if not pinned:
        print("    (nothing pinned — the homepage is purely chronological)")

    if args.check:
        print("--check: no files written")
        return 0

    changed_index = inject_index(items)
    changed_page = render_news_page(items)

    print(f"index.html  {'updated' if changed_index else 'unchanged'}")
    print(f"news.html   {'updated' if changed_page else 'unchanged'}")
    if changed_index or changed_page:
        print("\nReview the diff, then commit and push — Actions deploys to Pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
