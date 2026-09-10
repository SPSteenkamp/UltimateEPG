# UltimateEPG

UltimateEPG builds a single XMLTV EPG file for the mapped live-TV channels.

## Output

After GitHub Pages is enabled and the workflow completes:

- XMLTV: `https://spsteenkamp.github.io/UltimateEPG/UltimateEPG.xml`
- Status: `https://spsteenkamp.github.io/UltimateEPG/status.json`

Use **only the XMLTV URL above** as the EPG URL in the IPTV player.

## How it works

1. `config/channels.csv` defines the exact EPG channel IDs to collect.
2. `scripts/build_epg.py` downloads the mapped public XMLTV feeds.
3. Only exact matching channel IDs and their programme records are copied.
4. The script refuses to publish an empty EPG.
5. GitHub Actions rebuilds every 6 hours and can also be run manually.
6. GitHub Pages publishes the resulting `site/` directory.

## Important

The EPG builder does not fabricate programme data. If a source does not contain an exact mapped channel ID, that channel is reported in `status.json` instead of being given false schedule data.

The Canada mappings use the current EPGShare CA2 feed and `.ca2` IDs.

Movie metadata/playlists are intentionally separate and are not part of this live-TV EPG build yet.
