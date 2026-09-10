#!/usr/bin/env python3

"""
UltimateEPG XMLTV Builder

Reads config/channels.csv
Downloads the mapped public XMLTV feeds
Matches channels by stable EPG ID first, then by channel name
Builds one merged XMLTV file:
    site/UltimateEPG.xml

The published XMLTV channel IDs are ALWAYS the UltimateEPG
canonical_stable_id values. Programme channel references are
rewritten to those same stable IDs.

Also creates:
    site/status.json

The builder does NOT fabricate programme data.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import shutil
import tempfile
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from difflib import SequenceMatcher


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "channels.csv"
SITE = ROOT / "site"

XML_OUT = SITE / "UltimateEPG.xml"
STATUS_OUT = SITE / "status.json"

USER_AGENT = "UltimateEPG/2.0"
TIMEOUT = 180
RETRIES = 3

# Name matching is deliberately conservative.
FUZZY_THRESHOLD = 0.78
FUZZY_MARGIN = 0.08


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def load_mapping() -> list[dict[str, str]]:
    if not CONFIG.exists():
        raise RuntimeError(f"Mapping file not found: {CONFIG}")

    with CONFIG.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    included = []
    for row in rows:
        include = row.get("include", "").strip().lower()
        if include in ("true", "1", "yes"):
            stable_id = row.get("canonical_stable_id", "").strip()
            name = row.get("canonical_channel_name", "").strip()
            source = row.get("epg_source_url", "").strip()
            epg_id = row.get("epg_id", "").strip()

            if stable_id and name and source and epg_id:
                included.append(row)

    if not included:
        raise RuntimeError("No complete included mappings found in config/channels.csv")

    return included


def download_file(url: str, destination: Path) -> None:
    last_error = None

    for attempt in range(1, RETRIES + 1):
        try:
            print(f"Downloading: {url}")

            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
            )

            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                with destination.open("wb") as output:
                    shutil.copyfileobj(response, output)

            if not destination.exists() or destination.stat().st_size == 0:
                raise RuntimeError("Downloaded file is empty")

            print(f"Downloaded {destination.stat().st_size:,} bytes")
            return

        except Exception as exc:
            last_error = exc
            print(f"Download attempt {attempt} failed: {exc}")

            if attempt < RETRIES:
                time.sleep(3 * attempt)

    raise RuntimeError(f"Failed to download {url}: {last_error}")


def open_xml_source(path: Path, url: str):
    raw = path.open("rb")
    header = raw.read(2)
    raw.seek(0)

    is_gzip = url.lower().endswith(".gz") or header == b"\x1f\x8b"

    if is_gzip:
        return gzip.GzipFile(fileobj=raw, mode="rb")

    return raw


def text_values(elem: ET.Element, wanted: str) -> list[str]:
    values = []

    for child in list(elem):
        if local_tag(child.tag) == wanted:
            value = "".join(child.itertext()).strip()
            if value:
                values.append(value)

    return values


def normalize_tokens(value: str) -> set[str]:
    """
    Normalize a channel name/ID into comparison tokens.

    Removes common EPG presentation words such as HD, network,
    channel, east/west and source country suffixes.
    """

    value = value or ""

    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")

    # Split camel-case names such as BabyFirst -> Baby First.
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)

    value = value.lower()
    value = value.replace("&", " and ")

    # Remove EPGShare source suffixes.
    value = re.sub(
        r"\b(?:us2|ca2|au|uk|ie|za|nz)\b",
        " ",
        value,
    )

    # Words that normally describe the feed rather than the channel.
    value = re.sub(
        r"\b(?:"
        r"hd|sd|network|channel|television|tv|feed|"
        r"east|west|pacific|central|national|international|"
        r"streaming|stream|satellite|outer|market|"
        r"premium|plus"
        r")\b",
        " ",
        value,
    )

    value = re.sub(r"[^a-z0-9]+", " ", value)

    tokens = []
    for token in value.split():
        # Mild singular/plural normalization.
        if len(token) > 4 and token.endswith("s"):
            token = token[:-1]
        tokens.append(token)

    return set(tokens)


def compact_name(value: str) -> str:
    """
    Compact comparison form. Useful for names such as:
      5StarMAX
      5.StarMAX.HD.East
    """

    value = value or ""

    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")

    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    value = value.lower()

    value = re.sub(
        r"\b(?:us2|ca2|au|uk|ie|za|nz|hd|sd|network|channel|"
        r"television|tv|feed|east|west|pacific|central|national|"
        r"international|streaming|stream|satellite|outer|market|"
        r"premium|plus)\b",
        "",
        value,
    )

    return re.sub(r"[^a-z0-9]+", "", value)


def name_score(left: str, right: str) -> float:
    left_tokens = normalize_tokens(left)
    right_tokens = normalize_tokens(right)

    if not left_tokens or not right_tokens:
        return 0.0

    token_score = (
        2.0 * len(left_tokens & right_tokens)
        / (len(left_tokens) + len(right_tokens))
    )

    left_compact = compact_name(left)
    right_compact = compact_name(right)

    compact_score = (
        SequenceMatcher(None, left_compact, right_compact).ratio()
        if left_compact and right_compact
        else 0.0
    )

    return max(token_score, compact_score)


def resolve_channel(
    row: dict[str, str],
    source_channels: dict[str, dict],
) -> tuple[str | None, str, float]:
    """
    Resolve one configured channel to a source channel ID.

    Priority:
      1. Exact configured EPG ID.
      2. Exact normalized channel name.
      3. Conservative fuzzy name match.
    """

    configured_id = row.get("epg_id", "").strip()
    canonical_name = row.get("canonical_channel_name", "").strip()

    # 1. Exact ID.
    if configured_id and configured_id in source_channels:
        return configured_id, "epg_id", 1.0

    # 2. Exact normalized name.
    canonical_compact = compact_name(canonical_name)
    canonical_tokens = normalize_tokens(canonical_name)

    exact_candidates = []

    for source_id, info in source_channels.items():
        names = info["names"]

        for source_name in names:
            if (
                canonical_compact
                and canonical_compact == compact_name(source_name)
            ):
                exact_candidates.append(source_id)
                break

            if (
                canonical_tokens
                and canonical_tokens == normalize_tokens(source_name)
            ):
                exact_candidates.append(source_id)
                break

    exact_candidates = list(dict.fromkeys(exact_candidates))

    if len(exact_candidates) == 1:
        return exact_candidates[0], "name_exact", 1.0

    # 3. Fuzzy match.
    scored = []

    for source_id, info in source_channels.items():
        best_name_score = 0.0

        for source_name in info["names"]:
            best_name_score = max(
                best_name_score,
                name_score(canonical_name, source_name),
            )

        # The configured EPG ID may come from an older EPGShare
        # naming revision. It is still useful for distinguishing
        # duplicate channel names such as ESPN2, MTV, MSG, etc.
        configured_id_score = 0.0

        if configured_id:
            configured_id_score = name_score(
                configured_id,
                source_id,
            )

        combined_score = max(
            best_name_score,
            configured_id_score,
        )

        scored.append((combined_score, source_id))

    scored.sort(reverse=True)

    if scored:
        best_score, best_id = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        if (
            best_score >= FUZZY_THRESHOLD
            and (best_score - second_score) >= FUZZY_MARGIN
        ):
            return best_id, "name_fuzzy", best_score

    return None, "unmatched", 0.0


def read_source_channels(
    downloaded: Path,
    url: str,
) -> dict[str, dict]:
    """
    First pass over the XMLTV source.

    Returns:
        source_id -> {
            "element": bytes,
            "names": [...]
        }
    """

    source_channels = {}

    source = open_xml_source(downloaded, url)

    try:
        context = ET.iterparse(source, events=("end",))

        for _, elem in context:
            if local_tag(elem.tag) != "channel":
                continue

            source_id = elem.attrib.get("id", "").strip()

            if not source_id:
                continue

            names = text_values(elem, "display-name")

            if not names:
                names = [source_id]

            source_channels[source_id] = {
                "element": ET.tostring(elem, encoding="utf-8"),
                "names": names,
            }

            elem.clear()

    finally:
        source.close()

    return source_channels


def make_stable_channel(
    source_xml: bytes,
    stable_id: str,
    canonical_name: str,
) -> bytes:
    elem = ET.fromstring(source_xml)

    elem.attrib["id"] = stable_id

    # Keep the source's display names, but ensure the canonical name
    # is available as the first display-name for player compatibility.
    display_names = [
        child
        for child in list(elem)
        if local_tag(child.tag) == "display-name"
    ]

    if display_names:
        display_names[0].text = canonical_name
    else:
        display = ET.Element("display-name")
        display.text = canonical_name
        elem.insert(0, display)

    return ET.tostring(elem, encoding="utf-8")


def parse_source(
    url: str,
    rows: list[dict[str, str]],
    workdir: Path,
):
    filename = "feed_" + str(abs(hash(url))) + ".xml"
    downloaded = workdir / filename

    download_file(url, downloaded)

    print("Reading source channel directory...")
    source_channels = read_source_channels(downloaded, url)

    print(f"Source contains {len(source_channels):,} channel IDs")

    resolved = {}
    resolution_stats = defaultdict(int)
    unresolved = []

    for row in rows:
        stable_id = row["canonical_stable_id"].strip()
        canonical_name = row["canonical_channel_name"].strip()

        source_id, method, score = resolve_channel(row, source_channels)

        if source_id is None:
            resolution_stats["unmatched"] += 1
            unresolved.append(
                {
                    "stable_id": stable_id,
                    "channel": canonical_name,
                    "configured_epg_id": row.get("epg_id", "").strip(),
                    "country": row.get("country", "").strip(),
                    "source": url,
                }
            )
            continue

        resolved[stable_id] = {
            "source_id": source_id,
            "stable_id": stable_id,
            "canonical_name": canonical_name,
            "method": method,
            "score": round(score, 4),
        }

        resolution_stats[method] += 1

    print(
        "Resolved:",
        f"ID={resolution_stats['epg_id']:,}",
        f"name_exact={resolution_stats['name_exact']:,}",
        f"name_fuzzy={resolution_stats['name_fuzzy']:,}",
        f"unmatched={resolution_stats['unmatched']:,}",
    )

    # Map source channel IDs to stable IDs. This also allows programmes
    # to be rewritten without changing the source programme data.
    source_to_stable = defaultdict(list)

    for stable_id, item in resolved.items():
        source_to_stable[item["source_id"]].append(stable_id)

    programme_file = (
        workdir
        / ("programmes_" + str(abs(hash(url))) + ".xmlparts")
    )

    programme_counts = defaultdict(int)

    source = open_xml_source(downloaded, url)

    try:
        with programme_file.open("wb") as output:
            context = ET.iterparse(source, events=("end",))

            for _, elem in context:
                if local_tag(elem.tag) != "programme":
                    continue

                source_channel_id = elem.attrib.get("channel", "").strip()
                stable_id = source_to_stable.get(source_channel_id)

                if stable_id:
                    elem.attrib["channel"] = stable_id

                    output.write(
                        ET.tostring(elem, encoding="utf-8")
                    )
                    output.write(b"\n")

                    programme_counts[stable_id] += 1

                elem.clear()

    finally:
        source.close()

    selected_channels = {}

    for stable_id, item in resolved.items():
        source_id = item["source_id"]
        source_info = source_channels.get(source_id)

        if not source_info:
            continue

        selected_channels[stable_id] = make_stable_channel(
            source_info["element"],
            stable_id,
            item["canonical_name"],
        )

    print(
        f"Published channel matches from this source: "
        f"{len(selected_channels):,}"
    )
    print(
        f"Programme records from this source: "
        f"{sum(programme_counts.values()):,}"
    )

    return (
        selected_channels,
        programme_file,
        programme_counts,
        resolved,
        unresolved,
        dict(resolution_stats),
    )


def write_xml(
    mapping,
    source_results,
    generated_at,
):
    SITE.mkdir(parents=True, exist_ok=True)

    selected_channels = {}
    programme_files = []
    programme_counts = {}

    resolutions = {}
    unresolved = []

    for url, result in source_results.items():
        (
            channels,
            programme_file,
            counts,
            resolved,
            source_unresolved,
            _stats,
        ) = result

        selected_channels.update(channels)
        programme_files.append(programme_file)

        resolutions.update(resolved)
        unresolved.extend(source_unresolved)

        for stable_id, count in counts.items():
            programme_counts[stable_id] = (
                programme_counts.get(stable_id, 0) + count
            )

    ordered_ids = []

    for row in mapping:
        stable_id = row["canonical_stable_id"].strip()

        if (
            stable_id in selected_channels
            and stable_id not in ordered_ids
        ):
            ordered_ids.append(stable_id)

    temporary_output = XML_OUT.with_suffix(".xml.tmp")

    with temporary_output.open("wb") as output:
        output.write(
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
        )

        output.write(
            (
                "<!-- "
                f"Generated by UltimateEPG at {generated_at}"
                " -->\n"
            ).encode("utf-8")
        )

        output.write(
            (
                '<tv '
                'generator-info-name="UltimateEPG" '
                'generator-info-url="https://github.com/SPSteenkamp/UltimateEPG">'
                "\n"
            ).encode("utf-8")
        )

        for stable_id in ordered_ids:
            output.write(selected_channels[stable_id])
            output.write(b"\n")

        for programme_file in programme_files:
            with programme_file.open("rb") as source:
                shutil.copyfileobj(source, output)

        output.write(b"</tv>\n")

    temporary_output.replace(XML_OUT)

    return ordered_ids, programme_counts, resolutions, unresolved


def main():
    print()
    print("========================================")
    print("        UltimateEPG XMLTV Builder")
    print("========================================")
    print()

    mapping = load_mapping()

    print(f"Loaded {len(mapping):,} channel mappings")

    channels_by_source = defaultdict(list)

    for row in mapping:
        source_url = row["epg_source_url"].strip()
        if source_url:
            channels_by_source[source_url].append(row)

    print(f"Using {len(channels_by_source):,} EPG sources")
    print()

    generated_at = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    source_results = {}
    source_status = {}

    with tempfile.TemporaryDirectory() as temporary_directory:
        workdir = Path(temporary_directory)

        for source_url, rows in channels_by_source.items():
            print("----------------------------------------")
            print(f"Source: {source_url}")
            print(f"Requested mappings: {len(rows):,}")

            try:
                result = parse_source(
                    source_url,
                    rows,
                    workdir,
                )

                source_results[source_url] = result

                (
                    channels,
                    _programme_file,
                    counts,
                    resolved,
                    unresolved,
                    stats,
                ) = result

                source_status[source_url] = {
                    "status": "ok",
                    "requested_mappings": len(rows),
                    "resolved_channels": len(resolved),
                    "published_channels": len(channels),
                    "channels_with_programmes": sum(
                        1
                        for stable_id in resolved
                        if counts.get(stable_id, 0) > 0
                    ),
                    "programme_records": sum(counts.values()),
                    "resolution": stats,
                    "unmatched": unresolved,
                }

            except Exception as exc:
                print(f"SOURCE FAILED: {exc}")

                source_status[source_url] = {
                    "status": "failed",
                    "requested_mappings": len(rows),
                    "resolved_channels": 0,
                    "published_channels": 0,
                    "channels_with_programmes": 0,
                    "programme_records": 0,
                    "resolution": {},
                    "unmatched": [],
                    "error": str(exc),
                }

    (
        published_ids,
        programme_counts,
        resolutions,
        unresolved,
    ) = write_xml(
        mapping,
        source_results,
        generated_at,
    )

    channels_without_programmes = []

    for row in mapping:
        stable_id = row["canonical_stable_id"].strip()

        if (
            stable_id in published_ids
            and programme_counts.get(stable_id, 0) == 0
        ):
            channels_without_programmes.append(
                {
                    "stable_id": stable_id,
                    "channel": row["canonical_channel_name"],
                    "country": row["country"],
                }
            )

    total_programmes = sum(programme_counts.values())

    published_by_country = defaultdict(int)
    programme_by_country = defaultdict(int)

    country_by_stable = {
        row["canonical_stable_id"].strip(): row["country"]
        for row in mapping
    }

    for stable_id in published_ids:
        country = country_by_stable.get(stable_id, "")
        published_by_country[country] += 1
        programme_by_country[country] += programme_counts.get(
            stable_id, 0
        )

    status = {
        "generated_at_utc": generated_at,
        "requested_mappings": len(mapping),
        "published_channels": len(published_ids),
        "channels_missing_from_source": len(unresolved),
        "channels_without_programmes": len(channels_without_programmes),
        "programme_records": total_programmes,
        "published_by_country": dict(sorted(published_by_country.items())),
        "programme_records_by_country": dict(sorted(programme_by_country.items())),
        "sources": source_status,
        "unmatched_channels": unresolved,
        "channels_without_programmes": channels_without_programmes,
        "xmltv_url": (
            "https://spsteenkamp.github.io/UltimateEPG/UltimateEPG.xml"
        ),
    }

    STATUS_OUT.write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print("========================================")
    print("              BUILD RESULT")
    print("========================================")
    print(f"Requested mappings:      {len(mapping):,}")
    print(f"Published channels:      {len(published_ids):,}")
    print(f"Missing channels:        {len(unresolved):,}")
    print(
        f"No programme data:       "
        f"{len(channels_without_programmes):,}"
    )
    print(f"Programme records:       {total_programmes:,}")
    print()

    if not published_ids:
        raise RuntimeError(
            "No mapped channels were resolved. Refusing to publish an empty EPG."
        )

    if total_programmes == 0:
        raise RuntimeError(
            "No programme records were found. Refusing to publish an empty EPG."
        )

    print("Validating generated XML...")

    try:
        ET.parse(XML_OUT)
    except Exception as exc:
        raise RuntimeError(
            f"Generated XMLTV is invalid: {exc}"
        )

    print("XML validation:          OK")
    print()
    print("========================================")
    print("UltimateEPG XMLTV build successful.")
    print(f"Output: {XML_OUT}")
    print("========================================")


if __name__ == "__main__":
    main()
