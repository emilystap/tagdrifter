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
  Artist Name Mismatch · Artist Folder Sanitization Mismatch · Artist Folder Letter Case
  Mismatch (the folder matches an accepted spelling except for case, e.g. `Charli xcx` for
  `Charli XCX`; only when `case_sensitive` is on).

## Source of truth

MusicBrainz is the ultimate authority. Until Tag Drifter consults it, a track's tags are trusted
over its path, letter case included, when it is **MBID-tagged**: it has well-formed
`musicbrainz_albumid` and `musicbrainz_trackid` values, which usually means Picard tagged it.
Every track- and album-level issue records this (an album counts when all its tracks do), so
issues on untrusted files can be told apart: `tagdrifter issues --mbid no`.

## Album Folder (`core/naming.py`)

- **Standard:** `{Album Title}`
- **With disambiguation:** `{Album Title} ({Disambiguation})`, from the first non-empty of:
  `albumversion`, `comment:MusicBrainz Album Comment`, `musicbrainz_albumcomment`,
  `releasecomment`, `comment:disambiguation`, `albumdisambiguation`, `discogs_release_notes`,
  `edition`.
- **Multi-release collisions:** when distinct releases (by `musicbrainz_albumid`, else by source
  folder) under one artist resolve to the same folder name (compared case-insensitively), the
  first suffix that makes them all unique is appended. A track without an album ID counts as part
  of its folder's release when its tagged siblings all share one ID:
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

Principle: **a name should read back as its tag.** Characters that Windows/SMB can't store become
visually similar Unicode substitutes instead of being dropped or replaced with something else.

| Input | Output | Example |
| :--- | :--- | :--- |
| `/` or `\` | `∕` (U+2215) | `AC/DC` → `AC∕DC` |
| `:` | `꞉` (U+A789) | `Medley: Pick Up…` → `Medley꞉ Pick Up…`, `4:44` → `4꞉44` |
| `*` | `∗` (U+2217) | `Work B**ch` → `Work B∗∗ch` |
| digit + `"` | `″` (U+2033) | `Blue Monday (12" Mix)` → `Blue Monday (12″ Mix)` |
| `"…"` / stray `"` | `“…”` / `”` | `"Heroes"` → `“Heroes”` |
| `\|` | `∣` (U+2223) | `A \| B` → `A ∣ B` |
| `?` | `？` (U+FF1F) | `Where Is My Mind?` → `Where Is My Mind？` |
| `<` `>` | `‹` `›` (U+2039/203A) | `u + me = <3` → `u + me = ‹3` |
| `.` at the start or end of a **folder or file name** | `․` (U+2024) | `DAMN.` → `DAMN․`, `.5: The Gray Chapter` → `․5꞉ The Gray Chapter` |
| leading/trailing spaces, repeated spaces | stripped / collapsed | `  padded  ` → `padded` |
| dots-only values | `(period)` | `.` → `(period)` |

Dots are only a problem at the edges of a whole name: Windows/SMB drop trailing dots, and a
leading dot hides the entry (and matches the default `.*` ignore pattern). Inside a track
filename they are safe, so `B.Y.O.B.` stays `…_B.Y.O.B..flac`, and a folder spelled `Lipps, Inc․`
gives filenames starting `Lipps, Inc._`. Values that already use typographic characters from
MusicBrainz (`…`, `’`, `‐`) pass through unchanged.

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
  test over ~9,000 randomized tracks found no other differences in the step-1 port.
- **Unicode NFC normalization**, as described above.
- **Untagged tracks don't split an album.** v1 treated tracks lacking `musicbrainz_albumid` as a
  separate release from their tagged siblings and proposed `(Year, N tracks)` folder splits.
  They now inherit the folder's single album ID; the missing tag is reported by
  `album.inconsistent_tags` / `tags.required` instead.
