Real MusicBrainz release lookups (`/ws/2/release/<mbid>?inc=recordings+artist-credits+release-groups+aliases&fmt=json`),
recorded once on 2026-10-30 so tests never call the API. Artist aliases other than English
ones were removed to keep the files small. MusicBrainz data is CC0.

- `maenarawanai.json`: ATARASHII GAKKO! – Maenarawanai (bbbc8eb5-c0ed-4497-8418-ba851f18d328),
  a Japanese artist credited by its English alias; track titles differ from recording titles.
- `brat.json`: Charli xcx – Brat and it’s the same but there’s three more songs so it’s not
  (b2176814-eccb-4652-9de2-d719e17f6952): two 12" vinyl media numbered 2 and 3, tracks
  numbered A1, B1, …
