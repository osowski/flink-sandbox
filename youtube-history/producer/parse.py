import base64
from datetime import timezone
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from dateutil.parser import ParserError

_NARROW_NBSP = " "  # narrow no-break space; appears before AM/PM in some Google Takeout locales


def extract_entries(html_path: str) -> list[dict]:
    with open(html_path, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "lxml")

    entries = []
    for cell in soup.select("div.content-cell"):
        links = cell.find_all("a")
        if not links:
            continue

        video_link = links[0]
        url = video_link.get("href", "")
        title = video_link.get_text(strip=True)

        parsed = urlparse(url)
        if "youtube.com/watch" not in url or "tv.youtube.com" in parsed.netloc:
            continue

        qs = parse_qs(parsed.query)
        video_ids = qs.get("v", [])
        if not video_ids:
            continue
        video_id = video_ids[0]

        channel_name = ""
        channel_url = ""
        if len(links) > 1:
            channel_link = links[1]
            channel_name = channel_link.get_text(strip=True)
            channel_url = channel_link.get("href", "")

        if not channel_name:
            continue

        # Get the last non-empty text line from the cell — that's the timestamp.
        # Use strip() on each line; Google Takeout may include trailing whitespace nodes.
        lines = [line.strip() for line in cell.get_text(separator="\n").split("\n")]
        non_empty = [line for line in lines if line]
        raw_timestamp = non_empty[-1] if non_empty else ""
        raw_timestamp = raw_timestamp.replace(_NARROW_NBSP, " ")
        try:
            parsed_dt = dateutil_parser.parse(raw_timestamp)
            # Convert to UTC if timezone-aware; treat naive datetimes as UTC
            # (Google Takeout exports use UTC but dateutil may return a naive dt
            # for unrecognized locale abbreviations).
            if parsed_dt.tzinfo is not None:
                parsed_dt = parsed_dt.astimezone(timezone.utc)
            watched_at = parsed_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ParserError, ValueError, TypeError):
            continue

        entries.append({
            "video_id": video_id,
            "url": url,
            "title": title,
            "channel_name": channel_name,
            "channel_url": channel_url,
            "watched_at": watched_at,
        })

    return entries


def make_key(watched_at: str, video_id: str) -> str:
    raw = f"{watched_at}-{video_id}"
    return base64.b64encode(raw.encode()).decode()
