# Naming Conventions

The rules Tag Drifter checks a library against. Implemented in `src/tagdrifter/core/`.

```text
<Library Root>/<Artist Folder>/<Album Folder>/<Track Filename>.flac
```

## Artist Folder (`core/placement.py`)

- **Format:** `{Album Artist}` (sanitized), or `Various Artists` for compilations.
- Accepted spellings: the sanitized `albumartist`, the sanitized `albumartistsort`
  (falling back to `artistsort`), or `albumartist` with `$` → `s` (e.g. `Ke$ha` → `Kesha`).
- If `albumartist` is missing, `artist` is used.
- Issue types: Compilation inside Solo Artist Folder · Solo/Collab Album inside Various Artists ·
  Collab Album placed under Single Contributor · Solo Album placed under Collab Folder ·
  Artist Name Mismatch · Artist Folder Sanitization Mismatch.

## Album Folder (`core/naming.py`)

- **Standard:** `{Album Title}`
- **With disambiguation:** `{Album Title} ({Disambiguation})`, from the first non-empty of:
  `albumversion`, `comment:MusicBrainz Album Comment`, `musicbrainz_albumcomment`,
  `releasecomment`, `comment:disambiguation`, `albumdisambiguation`, `discogs_release_notes`,
  `edition`.
- **Multi-release collisions:** when distinct releases (by `musicbrainz_albumid`, else by source
  folder) under one artist resolve to the same folder name (compared case-insensitively), the
  first suffix that makes them all unique is appended:
  1. `({Year})`
  2. `({Year}, {N} tracks)`
  3. `({Year}, {Catalog# or Country})`, with `[mbid8]` appended if still identical

## Track Filename (`core/naming.py`)

- **Single disc** (`totaldiscs`/`disctotal` ≤ 1 and `discnumber` ≤ 1):
  `{Primary Track Artist}_{Album Title}_{track:02d}_{Track Title}.flac`
- **Multi-disc:** `{Primary Track Artist}_{Album Title}_{disc:02d}-{track:02d}_{Track Title}.flac`
- Sidecar lyrics (`.lrc`, `.txt`) should share the track's filename stem.

### Primary Track Artist

1. First entry of the multi-value `artists` tag (`"Adele; The Raconteurs"` → `Adele`); otherwise
   the `artist` tag with trailing `feat.`/`ft.`/`featuring`/`with` credits stripped.
2. If the artist folder is not Various Artists and the primary artist (or `artistsort`) matches the
   folder after normalization, the folder's spelling is used (`Ke$ha` inside `Kesha` → `Kesha`).
3. Otherwise, a non-inverted `artistsort` (no comma) that normalizes to the same name resolves a
   stylization; else the primary artist is used as-is (split albums keep each track's artist).

## Character Sanitization (`core/sanitize.py`)

| Input | Output | Example |
| :--- | :--- | :--- |
| `/` or `\` | `∕` (U+2215) | `AC/DC` → `AC∕DC` |
| `: ` (colon + space) | ` - ` | `Medley: Pick Up…` → `Medley - Pick Up…` |
| `:` | `-` | `4:44` → `4-44` |
| `*` | `∗` (U+2217) | `Work B**ch` → `Work B∗∗ch` |
| digit + `"` | `″` (U+2033) | `Blue Monday (12" Mix)` → `Blue Monday (12″ Mix)` |
| `"…"` / stray `"` | `“…”` / `”` | `"Heroes"` → `“Heroes”` |
| `\|` | `∣` (U+2223) | `A \| B` → `A ∣ B` |
| `?` `<` `>` | removed | `Where Is My Mind?` → `Where Is My Mind` |
| leading/trailing spaces and `.` | stripped | `Album...` → `Album` |
| symbol-only values | word in parentheses | `...` → `(period)`, `?` → `(question mark)` |

All text is normalized to Unicode NFC first, so names written decomposed (NFD, common from macOS
clients) compare equal to their tag values.

## Differences from v1 (`music_lib_scanner_v1`)

- **Case sensitivity is a setting.** v1 compared paths with `os.path.normcase`, which ignores case
  on Windows and nowhere else. Tag Drifter takes `case_sensitive` explicitly (default `True`, matching
  a case-sensitive ZFS dataset). When it is on, a case-only difference counts as a mismatch.
- **Case-only collisions are detected** (`find_case_collisions`): sibling names such as
  `Abbey Road` / `Abbey road` coexist on a case-sensitive dataset but collide for SMB clients.
- **Empty tag values are skipped.** v1 stopped at a tag that existed with an empty value (e.g.
  `ARTISTSORT=""`). Tag Drifter falls through to the next candidate key / fallback. A differential
  test over ~9,000 randomized tracks found no other differences.
- **Unicode NFC normalization**, as described above.
