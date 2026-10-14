# pocketcasts_downloader

This script downloads all audio episodes from a Pocket Casts podcast page. Pocket Casts does not host the audio itself, it is a cache/aggregator that points at each podcast's own RSS feed. This script:
1. Extracts the podcast UUID from a pocketcasts.com / pca.st URL.
2. Fetches the full episode list from Pocket Casts' public cache API, which includes each episode's real audio URL.
3. Downloads every episode to a folder, oldest first, skipping any that are already present so you can re-run if needed.

If the cache API ever changes, pass --rss with the podcast's real RSS feed URL instead, the script will parse <enclosure> tags directly.

## Usage:
```bash
pip3 install requests
python3 pocketcasts_download.py "https://pocketcasts.com/podcast/<name>/<uuid>"
python3 pocketcasts_download.py "<url>" -o ./episodes
python3 pocketcasts_download.py --rss "https://example.com/feed.xml" -o ./episodes
```
