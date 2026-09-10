#!/usr/bin/env python3

"""
UltimateEPG XMLTV Builder

Reads config/channels.csv
Downloads the mapped public XMLTV feeds
Extracts only the requested channel IDs
Builds one merged XMLTV file:
    site/UltimateEPG.xml

Also creates:
    site/status.json

The builder does NOT fabricate programme data.
"""

from __future__ import annotations

import csv
import gzip
import json
import shutil
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

CONFIG = ROOT / "config" / "channels.csv"
SITE = ROOT / "site"

XML_OUT = SITE / "UltimateEPG.xml"
STATUS_OUT = SITE / "status.json"

USER_AGENT = "UltimateEPG/1.0"
TIMEOUT = 180
RETRIES = 3


# ============================================================
# HELPERS
# ============================================================

def local_tag(tag: str) -> str:
    """
    Removes an XML namespace from a tag.

    Example:
        {http://example.com}channel
    becomes:
        channel
    """
    return tag.rsplit("}", 1)[-1]


def load_mapping() -> list[dict[str, str]]:
    """
    Load the channel mapping CSV.
    Only rows with include=true/1/yes are used.
    """

    if not CONFIG.exists():
        raise RuntimeError(
            f"Mapping file not found: {CONFIG}"
        )

    with CONFIG.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as f:

        rows = list(csv.DictReader(f))

    included = []

    for row in rows:

        include = row.get(
            "include",
            ""
        ).strip().lower()

        if include in (
            "true",
            "1",
            "yes"
        ):
            included.append(row)

    if not included:
        raise RuntimeError(
            "No included channels found in config/channels.csv"
        )

    return included


def download_file(
    url: str,
    destination: Path
) -> None:

    last_error = None

    for attempt in range(
        1,
        RETRIES + 1
    ):

        try:

            print(
                f"Downloading: {url}"
            )

            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=TIMEOUT
            ) as response:

                with destination.open(
                    "wb"
                ) as output:

                    shutil.copyfileobj(
                        response,
                        output
                    )

            if not destination.exists():
                raise RuntimeError(
                    "Download did not create a file"
                )

            if destination.stat().st_size == 0:
                raise RuntimeError(
                    "Downloaded file is empty"
                )

            print(
                f"Downloaded {destination.stat().st_size:,} bytes"
            )

            return

        except Exception as exc:

            last_error = exc

            print(
                f"Download attempt {attempt} failed: {exc}"
            )

            if attempt < RETRIES:

                time.sleep(
                    3 * attempt
                )

    raise RuntimeError(
        f"Failed to download {url}: {last_error}"
    )


def open_xml_source(
    path: Path,
    url: str
):
    """
    Open either XML or gzip-compressed XML.
    """

    raw = path.open(
        "rb"
    )

    header = raw.read(2)

    raw.seek(0)

    is_gzip = (
        url.lower().endswith(".gz")
        or header == b"\x1f\x8b"
    )

    if is_gzip:

        return gzip.GzipFile(
            fileobj=raw,
            mode="rb"
        )

    return raw


# ============================================================
# PARSE SOURCE
# ============================================================

def parse_source(
    url: str,
    target_ids: set[str],
    workdir: Path
):
    """
    Download and parse one XMLTV source.

    Returns:

        channels
        programme_file
        programme_counts
    """

    filename = (
        "feed_"
        + str(abs(hash(url)))
        + ".xml"
    )

    downloaded = workdir / filename

    download_file(
        url,
        downloaded
    )

    channels: dict[str, bytes] = {}

    programme_file = (
        workdir
        / (
            "programmes_"
            + str(abs(hash(url)))
            + ".xmlparts"
        )
    )

    programme_counts = defaultdict(int)

    print(
        f"Parsing source: {url}"
    )

    with programme_file.open(
        "wb"
    ) as programme_output:

        source = open_xml_source(
            downloaded,
            url
        )

        try:

            # IMPORTANT:
            # We deliberately do NOT call elem.clear()
            # while parsing individual child elements.
            #
            # Clearing children before their parent <channel>
            # or <programme> is completed can destroy:
            #   <display-name>
            #   <title>
            #   <desc>
            #   etc.
            #
            # This version keeps the complete XML elements.

            context = ET.iterparse(
                source,
                events=("end",)
            )

            for _, elem in context:

                element_type = local_tag(
                    elem.tag
                )

                # --------------------------------------------
                # CHANNEL
                # --------------------------------------------

                if element_type == "channel":

                    channel_id = elem.attrib.get(
                        "id",
                        ""
                    )

                    if channel_id in target_ids:

                        channels[channel_id] = (
                            ET.tostring(
                                elem,
                                encoding="utf-8"
                            )
                        )

                # --------------------------------------------
                # PROGRAMME
                # --------------------------------------------

                elif element_type == "programme":

                    channel_id = elem.attrib.get(
                        "channel",
                        ""
                    )

                    if channel_id in target_ids:

                        programme_output.write(
                            ET.tostring(
                                elem,
                                encoding="utf-8"
                            )
                        )

                        programme_output.write(
                            b"\n"
                        )

                        programme_counts[
                            channel_id
                        ] += 1

        finally:

            source.close()

    print(
        f"Matched {len(channels):,} channels"
    )

    print(
        f"Matched {sum(programme_counts.values()):,} programme records"
    )

    return (
        channels,
        programme_file,
        programme_counts
    )


# ============================================================
# WRITE XMLTV
# ============================================================

def write_xml(
    mapping,
    source_results,
    generated_at
):

    SITE.mkdir(
        parents=True,
        exist_ok=True
    )

    selected_channels = {}

    programme_files = []

    programme_counts = {}

    # --------------------------------------------------------
    # Combine results
    # --------------------------------------------------------

    for url, result in source_results.items():

        channels, programme_file, counts = result

        selected_channels.update(
            channels
        )

        for channel_id, count in counts.items():

            programme_counts[
                channel_id
            ] = (
                programme_counts.get(
                    channel_id,
                    0
                )
                + count
            )

        if programme_file.exists():

            programme_files.append(
                programme_file
            )

    # --------------------------------------------------------
    # Stable channel order
    # --------------------------------------------------------

    ordered_ids = []

    seen = set()

    for row in mapping:

        channel_id = row.get(
            "epg_id",
            ""
        ).strip()

        if not channel_id:
            continue

        if (
            channel_id in selected_channels
            and channel_id not in seen
        ):

            ordered_ids.append(
                channel_id
            )

            seen.add(
                channel_id
            )

    # --------------------------------------------------------
    # Temporary output
    # --------------------------------------------------------

    temporary_output = (
        XML_OUT.with_suffix(
            ".xml.tmp"
        )
    )

    with temporary_output.open(
        "wb"
    ) as output:

        output.write(
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
        )

        output.write(
            (
                "<!-- "
                f"Generated by UltimateEPG at {generated_at}"
                " -->\n"
            ).encode(
                "utf-8"
            )
        )

        output.write(
            (
                '<tv '
                'generator-info-name="UltimateEPG" '
                'generator-info-url="https://github.com/SPSteenkamp/UltimateEPG">'
                "\n"
            ).encode(
                "utf-8"
            )
        )

        # ----------------------------------------------------
        # Channels
        # ----------------------------------------------------

        for channel_id in ordered_ids:

            output.write(
                selected_channels[
                    channel_id
                ]
            )

            output.write(
                b"\n"
            )

        # ----------------------------------------------------
        # Programmes
        # ----------------------------------------------------

        for programme_file in programme_files:

            with programme_file.open(
                "rb"
            ) as programme_input:

                shutil.copyfileobj(
                    programme_input,
                    output
                )

        # ----------------------------------------------------
        # Close XMLTV
        # ----------------------------------------------------

        output.write(
            b"</tv>\n"
        )

    temporary_output.replace(
        XML_OUT
    )

    return (
        ordered_ids,
        programme_counts
    )


# ============================================================
# MAIN BUILD
# ============================================================

def main():

    print()
    print(
        "========================================"
    )
    print(
        "        UltimateEPG XMLTV Builder"
    )
    print(
        "========================================"
    )
    print()

    # --------------------------------------------------------
    # Load mapping
    # --------------------------------------------------------

    mapping = load_mapping()

    print(
        f"Loaded {len(mapping):,} channel mappings"
    )

    # --------------------------------------------------------
    # Group channels by source
    # --------------------------------------------------------

    channels_by_source = defaultdict(set)

    for row in mapping:

        epg_id = row.get(
            "epg_id",
            ""
        ).strip()

        source_url = row.get(
            "epg_source_url",
            ""
        ).strip()

        if epg_id and source_url:

            channels_by_source[
                source_url
            ].add(
                epg_id
            )

    print(
        f"Using {len(channels_by_source):,} EPG sources"
    )

    print()

    # --------------------------------------------------------
    # Timestamp
    # --------------------------------------------------------

    generated_at = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    source_results = {}

    source_status = {}

    # --------------------------------------------------------
    # Process sources
    # --------------------------------------------------------

    with tempfile.TemporaryDirectory() as temporary_directory:

        workdir = Path(
            temporary_directory
        )

        for source_url, target_ids in channels_by_source.items():

            print(
                "----------------------------------------"
            )

            print(
                f"Source: {source_url}"
            )

            print(
                f"Requested IDs: {len(target_ids):,}"
            )

            try:

                result = parse_source(
                    source_url,
                    target_ids,
                    workdir
                )

                source_results[
                    source_url
                ] = result

                channels, _, counts = result

                source_status[
                    source_url
                ] = {

                    "status": "ok",

                    "requested_channels":
                        len(target_ids),

                    "matched_channels":
                        len(channels),

                    "channels_with_programmes":
                        sum(
                            1
                            for channel_id in channels
                            if counts.get(
                                channel_id,
                                0
                            ) > 0
                        ),

                    "programme_records":
                        sum(
                            counts.values()
                        )
                }

            except Exception as exc:

                print(
                    f"SOURCE FAILED: {exc}"
                )

                source_status[
                    source_url
                ] = {

                    "status": "failed",

                    "requested_channels":
                        len(target_ids),

                    "matched_channels":
                        0,

                    "channels_with_programmes":
                        0,

                    "programme_records":
                        0,

                    "error":
                        str(exc)
                }

    # --------------------------------------------------------
    # Build final XML
    # --------------------------------------------------------

    matched_ids, programme_counts = write_xml(
        mapping,
        source_results,
        generated_at
    )

    # --------------------------------------------------------
    # Find missing channels
    # --------------------------------------------------------

    missing_channels = []

    channels_without_programmes = []

    for row in mapping:

        channel_id = row.get(
            "epg_id",
            ""
        ).strip()

        if not channel_id:
            continue

        if channel_id not in matched_ids:

            missing_channels.append(
                {
                    "epg_id":
                        channel_id,

                    "channel":
                        row.get(
                            "canonical_channel_name",
                            ""
                        ),

                    "country":
                        row.get(
                            "country",
                            ""
                        ),

                    "source":
                        row.get(
                            "epg_source_url",
                            ""
                        )
                }
            )

        elif programme_counts.get(
            channel_id,
            0
        ) == 0:

            channels_without_programmes.append(
                {
                    "epg_id":
                        channel_id,

                    "channel":
                        row.get(
                            "canonical_channel_name",
                            ""
                        ),

                    "country":
                        row.get(
                            "country",
                            ""
                        )
                }
            )

    total_programmes = sum(
        programme_counts.values()
    )

    # --------------------------------------------------------
    # Status report
    # --------------------------------------------------------

    status = {

        "generated_at_utc":
            generated_at,

        "requested_mappings":
            len(mapping),

        "published_channels":
            len(matched_ids),

        "channels_missing_from_source":
            len(missing_channels),

        "channels_without_programmes":
            len(channels_without_programmes),

        "programme_records":
            total_programmes,

        "sources":
            source_status,

        "missing_channels":
            missing_channels,

        "channels_without_programmes":
            channels_without_programmes,

        "xmltv_url":
            "https://spsteenkamp.github.io/UltimateEPG/UltimateEPG.xml"
    }

    STATUS_OUT.write_text(
        json.dumps(
            status,
            indent=2,
            ensure_ascii=False
        )
        + "\n",
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    print()
    print(
        "========================================"
    )
    print(
        "              BUILD RESULT"
    )
    print(
        "========================================"
    )

    print(
        f"Requested mappings:      {len(mapping):,}"
    )

    print(
        f"Published channels:      {len(matched_ids):,}"
    )

    print(
        f"Missing channels:        {len(missing_channels):,}"
    )

    print(
        f"No programme data:       {len(channels_without_programmes):,}"
    )

    print(
        f"Programme records:       {total_programmes:,}"
    )

    print()

    # --------------------------------------------------------
    # Never publish an empty EPG
    # --------------------------------------------------------

    if not matched_ids:

        raise RuntimeError(
            "No mapped channels were found in any EPG source. "
            "Refusing to publish an empty EPG."
        )

    if total_programmes == 0:

        raise RuntimeError(
            "No programme records were found. "
            "Refusing to publish an empty EPG."
        )

    # --------------------------------------------------------
    # Validate resulting XML
    # --------------------------------------------------------

    print(
        "Validating generated XML..."
    )

    try:

        ET.parse(
            XML_OUT
        )

    except Exception as exc:

        raise RuntimeError(
            f"Generated XMLTV is invalid: {exc}"
        )

    print(
        "XML validation:          OK"
    )

    print()

    print(
        "========================================"
    )

    print(
        "UltimateEPG XMLTV build successful."
    )

    print(
        f"Output: {XML_OUT}"
    )

    print(
        "========================================"
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
