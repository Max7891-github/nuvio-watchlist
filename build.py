"""Builds Max's Nuvio "To Watch" addon from watchlist.json.

Runs on GitHub (see .github/workflows/build.yml). For every title it pulls the
plot, cast, backdrop, rating etc. from Cinemeta, then writes the addon into ./site.
"""
import json
import os
import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

OUT = "site"
CINEMETA = "https://v3-cinemeta.strem.io/meta/{type}/{id}.json"
KEEP = ["description", "releaseInfo", "imdbRating", "genres", "runtime",
        "cast", "director", "writer", "background", "logo", "poster",
        "country", "language", "awards", "year"]

ROWS = [  # (catalog id, row name, filter)
    ("upnext", "Up Next", lambda m: m["_status"] in ("upnext", "watching")),
    ("top", "Top Rated", lambda m: True),
    ("bollywood", "Bollywood & Indian", lambda m: m["_region"] == "bollywood"),
    ("hollywood", "Hollywood & More", lambda m: m["_region"] == "hollywood"),
    ("korean", "Korean", lambda m: m["_region"] == "korean"),
    ("anime", "Anime", lambda m: m["_region"] == "anime"),
    ("rewatch", "Rewatch", lambda m: m["_status"] == "rewatch"),
]


def fetch(item):
    if os.environ.get("OFFLINE"):
        return {}
    url = CINEMETA.format(**item)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "max-watchlist"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r).get("meta") or {}
        except Exception:
            time.sleep(1 + attempt * 2)
    return {}


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


def main():
    items = json.load(open("watchlist.json", encoding="utf-8"))
    with ThreadPoolExecutor(12) as pool:
        details = list(pool.map(fetch, items))

    metas = []
    for item, d in zip(items, details):
        m = {"id": item["id"], "type": item["type"], "name": d.get("name") or item["name"]}
        m.update({k: d[k] for k in KEEP if d.get(k)})
        m.setdefault("poster", f"https://images.metahub.space/poster/medium/{item['id']}/img")
        m.setdefault("background", f"https://images.metahub.space/background/medium/{item['id']}/img")
        m.setdefault("logo", f"https://images.metahub.space/logo/medium/{item['id']}/img")
        m["_region"], m["_status"] = item["region"], item["status"]
        metas.append(m)
    found = sum(1 for d in details if d)
    print(f"Got details for {found}/{len(items)} titles")

    shutil.rmtree(OUT, ignore_errors=True)
    catalogs = []
    for typ in ("movie", "series"):
        of_type = [m for m in metas if m["type"] == typ]
        for cid, name, keep in ROWS:
            rows = [m for m in of_type if keep(m)]
            if not rows:
                continue
            if cid != "upnext":
                rows.sort(key=rating, reverse=True)  # highest IMDb rating first
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

    write("manifest.json", {
        "id": "com.max.notionwatchlist",
        "version": "2.0.0",
        "name": "Max's To Watch",
        "description": "My to-watch list from Notion, with ratings, genres and Up Next.",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": catalogs,
    })
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    print(f"Built {len(catalogs)} rows")


if __name__ == "__main__":
    main()
