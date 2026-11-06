"""Builds Max's Nuvio "To Watch" addon straight from his Notion Library.

Runs on GitHub every 3 hours (see .github/workflows/build.yml). It reads the
Library through the Notion API (NOTION_TOKEN secret), pulls plot/cast/rating
from Cinemeta, then writes the addon into ./site.
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

OUT = "site"
DATA_SOURCE_ID = "5aced7bd-903d-4e6a-9347-1eb31f2d9ed1"   # Entertainment Hub > Library
DATABASE_ID = "08b96faeb31146f88c762040e4835c6c"
CINEMETA = "https://v3-cinemeta.strem.io/meta/{type}/{id}.json"
KEEP = ["description", "releaseInfo", "imdbRating", "genres", "runtime",
        "cast", "director", "writer", "background", "logo", "poster",
        "country", "language", "awards", "year"]
WANTED = {"Want": "want", "Later": "want", "Up next": "upnext",
          "In progress": "watching", "Rewatch": "rewatch"}
REGION_BY_CATEGORY = {"Bollywood / Indian": "bollywood", "Korean Films": "korean",
                      "K-Dramas": "korean", "Anime": "anime"}

ROWS = [  # (catalog id, row name, filter)
    ("upnext", "Up Next", lambda m: m["_status"] in ("upnext", "watching")),
    ("top", "Top Rated", lambda m: True),
    ("bollywood", "Bollywood & Indian", lambda m: m["_region"] == "bollywood"),
    ("hollywood", "Hollywood & More", lambda m: m["_region"] == "hollywood"),
    ("korean", "Korean", lambda m: m["_region"] == "korean"),
    ("anime", "Anime", lambda m: m["_region"] == "anime"),
    ("rewatch", "Rewatch", lambda m: m["_status"] == "rewatch"),
]


# ---------- Notion ----------
def notion_post(url, body, version):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
        "Notion-Version": version, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def notion_pages():
    body = {"page_size": 100, "filter": {"or": [
        {"property": "Status", "select": {"equals": s}} for s in WANTED]}}
    targets = [(f"https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}/query", "2025-09-03"),
               (f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", "2022-06-28")]
    last_err = None
    for url, version in targets:
        pages, cursor = [], None
        try:
            while True:
                if cursor:
                    body["start_cursor"] = cursor
                res = notion_post(url, body, version)
                pages += res["results"]
                if not res.get("has_more"):
                    return pages
                cursor = res["next_cursor"]
        except urllib.error.HTTPError as e:
            last_err = f"{e.code} {e.read().decode()[:300]}"
            body.pop("start_cursor", None)
    sys.exit("Couldn't read Notion. Check the NOTION_TOKEN secret and that the Library "
             f"is shared with your integration (••• → Connections).\n{last_err}")


def prop(page, name):
    p = page["properties"].get(name) or {}
    t = p.get("type")
    if t == "title":
        return "".join(x["plain_text"] for x in p["title"])
    if t == "select":
        return (p["select"] or {}).get("name")
    if t in ("url", "number"):
        return p[t]
    return None


# ---------- auto-find missing IMDb links ----------
def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower().replace("&", "and")).strip()


def imdb_suggest(query):
    q = norm(query)
    if not q:
        return []
    url = f"https://v2.sg.media-imdb.com/suggestion/{q[0]}/{urllib.parse.quote(q)}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("d", [])
    except Exception:
        return []


def find_imdb(name, typ, year):
    """Return a tt id only when there's exactly one sensible match."""
    m = re.search(r"\((\d{4})\)", name)
    year = year or (int(m.group(1)) if m else None)
    base = re.sub(r"\s*\((\d{4}|film|movie|show|series)\)", "", name, flags=re.I)
    base = re.sub(r"\s+S\d+$", "", base).strip()
    alts = [base] + re.findall(r"\(([^)]+)\)", base)
    base_clean = re.sub(r"\s*\([^)]*\)", "", base).strip()
    if base_clean not in alts:
        alts.insert(0, base_clean)
    kinds = {"Movie": ("feature", "TV movie"), "Show": ("TV series", "TV mini-series")}.get(
        typ, ("feature", "TV movie", "TV series", "TV mini-series", "TV special"))
    for alt in alts:
        titles = [h for h in imdb_suggest(alt) if h.get("id", "").startswith("tt")]
        hits = [h for h in titles if h.get("q") in kinds and norm(h.get("l", "")) == norm(alt)]
        if year:
            hits = [h for h in hits if h.get("y") and abs(h["y"] - year) <= 1]
        elif hits and titles and hits[0] is not titles[0]:
            hits = []  # no year and it isn't IMDb's top result: too risky, skip
        if len(hits) == 1:
            return hits[0]["id"]
        if len(hits) > 1:
            return None  # ambiguous: leave it for a human
    return None


def save_link(page_id, imdb):
    req = urllib.request.Request(
        f"https://api.notion.com/v1/pages/{page_id}", method="PATCH",
        data=json.dumps({"properties": {"Link": {"url": f"https://www.imdb.com/title/{imdb}/"}}}).encode(),
        headers={"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
                 "Notion-Version": "2022-06-28", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except urllib.error.HTTPError as e:
        print(f"  couldn't save link to Notion ({e.code}) — give the integration edit access")
        return False


def load_watchlist():
    items, skipped, found, seen = [], [], [], set()
    for page in notion_pages():
        typ, name = prop(page, "Type"), prop(page, "Name") or "?"
        if typ not in ("Movie", "Show", "Anime"):
            continue
        m = re.search(r"(tt\d+)", prop(page, "Link") or "")
        imdb = m.group(1) if m else find_imdb(name, typ, prop(page, "Year"))
        if not imdb:
            skipped.append(name)
            continue
        if not m:
            found.append(f"{name} → {imdb}")
            save_link(page["id"], imdb)
        if imdb in seen:
            continue
        seen.add(imdb)
        items.append({
            "id": imdb,
            "type": {"Movie": "movie", "Show": "series"}.get(typ, "anime"),
            "name": name,
            "region": "anime" if typ == "Anime" else REGION_BY_CATEGORY.get(prop(page, "Category")),
            "status": WANTED[prop(page, "Status")],
        })
    if found:
        print(f"Found and saved {len(found)} IMDb links:\n  " + "\n  ".join(found))
    print(f"Notion: {len(items)} titles" + (f", skipped {len(skipped)} with no IMDb link: "
          + ", ".join(skipped) if skipped else ""))
    return items


# ---------- Cinemeta ----------
def cinemeta(typ, imdb):
    url = CINEMETA.format(type=typ, id=imdb)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "max-watchlist"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r).get("meta") or {}
        except Exception:
            time.sleep(1 + attempt * 2)
    return {}


def fetch(item):
    if os.environ.get("OFFLINE"):
        return item.get("_fake", {})
    if item["type"] == "anime":  # anime could be a film or a series
        d = cinemeta("series", item["id"])
        if d:
            item["type"] = "series"
            return d
        item["type"] = "movie"
    return cinemeta(item["type"], item["id"])


def guess_region(item, d):
    if item["region"]:
        return item["region"]
    country = str(d.get("country") or "")
    if "India" in country:
        return "bollywood"
    if "Korea" in country:
        return "korean"
    return "hollywood"


# ---------- output ----------
def rating(m):
    try:
        return float(m.get("imdbRating") or 0)
    except ValueError:
        return 0.0


def write(path, data):
    path = os.path.join(OUT, path)
    os.makedirs(os.path.dirname(path) or OUT, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def main(items=None):
    items = items if items is not None else load_watchlist()
    with ThreadPoolExecutor(12) as pool:
        details = list(pool.map(fetch, items))

    metas = []
    for item, d in zip(items, details):
        if item["type"] == "anime":
            item["type"] = "series"
        m = {"id": item["id"], "type": item["type"], "name": d.get("name") or item["name"]}
        m.update({k: d[k] for k in KEEP if d.get(k)})
        for kind in ("poster", "background", "logo"):
            m.setdefault(kind, f"https://images.metahub.space/{kind}/medium/{item['id']}/img")
        m["_region"], m["_status"] = guess_region(item, d), item["status"]
        metas.append(m)
    print(f"Got details for {sum(1 for d in details if d)}/{len(items)} titles")

    shutil.rmtree(OUT, ignore_errors=True)
    catalogs = []
    for typ in ("movie", "series"):
        of_type = [m for m in metas if m["type"] == typ]
        for cid, name, keep in ROWS:
            rows = [m for m in of_type if keep(m)]
            if not rows:
                continue
            if cid != "upnext":
                rows.sort(key=rating, reverse=True)
            clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in rows]
            genres = sorted({g for m in clean for g in m.get("genres", [])})
            cat = {"type": typ, "id": cid, "name": name}
            if genres:
                cat["extra"] = [{"name": "genre", "options": genres, "isRequired": False}]
                for g in genres:
                    write(f"catalog/{typ}/{cid}/genre={g}.json",
                          {"metas": [m for m in clean if g in m.get("genres", [])]})
            write(f"catalog/{typ}/{cid}.json", {"metas": clean})
            catalogs.append(cat)

    # Your own links: links.json maps an IMDb id (or id:season:episode) to a URL
    links = json.load(open("links.json", encoding="utf-8")) if os.path.exists("links.json") else {}
    types = {m["id"]: m["type"] for m in metas}
    count = 0
    for key, value in links.items():
        if key.startswith("_") or not value:
            continue
        base = key.split(":")[0]
        typ = "series" if ":" in key else types.get(base, "movie")
        entries = value if isinstance(value, list) else [value]
        streams = []
        for n, e in enumerate(entries, 1):
            e = {"url": e} if isinstance(e, str) else e
            streams.append({"url": e["url"], "name": "My Server",
                            "title": e.get("name") or (f"Link {n}" if len(entries) > 1 else "My link")})
        write(f"stream/{typ}/{key}.json", {"streams": streams})
        count += 1
    print(f"Added your own links for {count} titles")

    write("manifest.json", {
        "id": "com.max.notionwatchlist",
        "version": "3.0.0",
        "name": "Max's To Watch",
        "description": "My to-watch list, synced live from Notion every 3 hours.",
        "resources": ["catalog", "stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": catalogs,
    })
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    print(f"Built {len(catalogs)} rows")


if __name__ == "__main__":
    main()
