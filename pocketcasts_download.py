#!/usr/bin/env python3
"""
python3 pocketcasts_download.py "https://pocketcasts.com/podcast/<name>/<uuid>"
python3 pocketcasts_download.py "<url>" -o ./episodes
python3 pocketcasts_download.py --rss "https://example.com/feed.xml" -o ./episodes
"""

import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

USER_AGENT = "Mozilla/5.0 (compatible; podcast-archiver/1.0)"

# Pocket Casts' public cache returns episodes with direct audio URLs, the exact path has shifted over time, so we try several known hosts/paths.
CACHE_HOSTS = [
    "https://podcast-api.pocketcasts.com",
    "https://cache.pocketcasts.com",
]
CACHE_PATHS = [
    "/podcast/full/{uuid}/0/3/2000",
    "/podcast/full/{uuid}",
]

EXT_BY_TYPE = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
}


def extract_uuid(url: str) -> str:
    """Pull the trailing UUID out of a Pocket Casts URL"""
    m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        url,
        re.I,
    )
    if not m:
        sys.exit(f"Could not find a podcast UUID in: {url}")
    return m.group(0)


def safe_name(text: str, maxlen: int = 120) -> str:
    """Turn an episode title into a filesystem-safe string"""
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:maxlen].rstrip(" .") or "episode"


def ext_for(audio_url: str, file_type: str | None) -> str:
    if file_type:
        ext = EXT_BY_TYPE.get(file_type.lower())
        if ext:
            return ext
    # fall back to the extension in the URL path (strip query string)
    path = audio_url.split("?", 1)[0]
    _, dot, tail = path.rpartition(".")
    if dot and 1 <= len(tail) <= 5:
        return "." + tail.lower()
    return ".mp3"


def fetch_episodes_from_cache(uuid: str) -> list[dict]:
    """Return a list of {title, url, published, file_type} from Pocket Casts."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    for host in CACHE_HOSTS:
        for path in CACHE_PATHS:
            api = host + path.format(uuid=uuid)
            for method in ("POST", "GET"):
                try:
                    if method == "POST":
                        r = session.post(api, json={}, timeout=30)
                    else:
                        r = session.get(api, timeout=30)
                except requests.RequestException:
                    continue
                if r.status_code != 200:
                    continue
                try:
                    data = r.json()
                except ValueError:
                    continue
                episodes = (data.get("podcast") or {}).get("episodes") or data.get(
                    "episodes"
                )
                if episodes:
                    print(f"Got {len(episodes)} episodes from {api} [{method}]")
                    return [
                        {
                            "title": e.get("title") or e.get("uuid") or "episode",
                            "url": e.get("url"),
                            "published": (e.get("published") or "")[:10],
                            "file_type": e.get("file_type"),
                        }
                        for e in episodes
                        if e.get("url")
                    ]
    return []


def fetch_episodes_from_rss(rss_url: str) -> list[dict]:
    """Parse <item><enclosure> entries from a podcast RSS feed."""
    r = requests.get(rss_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    episodes = []
    for item in root.iter("item"):
        title_el = item.find("title")
        enc = item.find("enclosure")
        pub = item.find("pubDate")
        if enc is None or not enc.get("url"):
            continue
        episodes.append(
            {
                "title": (title_el.text if title_el is not None else "") or "episode",
                "url": enc.get("url"),
                "published": (pub.text if pub is not None else "")[:16],
                "file_type": enc.get("type"),
            }
        )
    print(f"Got {len(episodes)} episodes from RSS feed")
    return episodes


def download(ep: dict, index: int, outdir: str, session: requests.Session) -> None:
    prefix = f"{index:03d}"
    if ep["published"]:
        prefix += f"_{ep['published']}"
    fname = f"{prefix}_{safe_name(ep['title'])}{ext_for(ep['url'], ep['file_type'])}"
    dest = os.path.join(outdir, fname)

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  skip (exists): {fname}")
        return

    tmp = dest + ".part"
    for attempt in range(1, 4):
        try:
            with session.get(ep["url"], stream=True, timeout=60) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                done = 0
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        fh.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            print(f"\r  {fname}: {pct}%", end="", flush=True)
            print(f"\r  saved: {fname}{' ' * 20}")
            os.replace(tmp, dest)
            return
        except (requests.RequestException, OSError) as e:
            print(f"\r  attempt {attempt} failed for {fname}: {e}")
            time.sleep(2 * attempt)
    print(f"  GAVE UP on {fname}")
    if os.path.exists(tmp):
        os.remove(tmp)


def main() -> None:
    ap = argparse.ArgumentParser(description="Download all audio from a Pocket Casts podcast.")
    ap.add_argument("url", nargs="?", help="Pocket Casts podcast URL")
    ap.add_argument("--rss", help="Use a podcast RSS feed URL directly instead of the cache API")
    ap.add_argument("-o", "--outdir", default="episodes", help="Output directory")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between downloads")
    args = ap.parse_args()

    if not args.url and not args.rss:
        ap.error("provide a Pocket Casts URL or --rss feed URL")

    os.makedirs(args.outdir, exist_ok=True)

    if args.rss:
        episodes = fetch_episodes_from_rss(args.rss)
    else:
        uuid = extract_uuid(args.url)
        print(f"Podcast UUID: {uuid}")
        episodes = fetch_episodes_from_cache(uuid)
        if not episodes:
            sys.exit(
                "Could not retrieve episodes from the Pocket Casts cache.\n"
                "Find the podcast's real RSS feed URL and re-run with --rss <feed>."
            )

    # Oldest first so the numeric prefix matches chronological order.
    episodes.reverse()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    print(f"Downloading {len(episodes)} episodes to {os.path.abspath(args.outdir)}\n")
    for i, ep in enumerate(episodes, start=1):
        download(ep, i, args.outdir, session)
        time.sleep(args.delay)
    print("\nDone.")


if __name__ == "__main__":
    main()
