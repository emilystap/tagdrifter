Real MusicBrainz release lookups (`/ws/2/release/<mbid>?inc=recordings+artist-credits+release-groups+aliases&fmt=json`),
recorded once on 2026-10-30 so tests never call the API. Artist aliases other than English
ones were removed to keep the files small. MusicBrainz data is CC0.

- `maenarawanai.json`: ATARASHII GAKKO! – Maenarawanai (bbbc8eb5-c0ed-4497-8418-ba851f18d328),
  a Japanese artist credited by its English alias; track titles differ from recording titles.
- `brat.json`: Charli xcx – Brat and it’s the same but there’s three more songs so it’s not
  (b2176814-eccb-4652-9de2-d719e17f6952): two 12" vinyl media numbered 2 and 3, tracks
  numbered A1, B1, …
- `prolonging_the_magic_group.json`: a release group browse
  (`/ws/2/release?release-group=34cc7616-0e7e-34aa-a42d-bc9c17755f18&inc=recordings+media&limit=100&fmt=json`,
  recorded 2026-10-31), trimmed to 4 of CAKE – Prolonging the Magic's 13 releases: the 2023
  Deluxe Edition (15 tracks), the 1998 US CD whose lengths the files match, the US clean
  version and the 17-track Japanese CD.
- `let_me_go_recording.json`: a recording lookup for CAKE – Let Me Go
  (`/ws/2/recording/235efc48-16f8-4b48-8688-bf88010c1a32?inc=releases+media+isrcs&fmt=json`,
  recorded 2026-10-31; the app asks without `isrcs`): the 12 releases it's on with its
  track's length on each, including the 1999 single (3:55).
- `let_me_go_isrc.json`: an ISRC lookup (`/ws/2/isrc/USCA19886895?inc=releases&fmt=json`,
  recorded 2026-10-31; `inc=releases` adds nothing, so the app leaves it out): two
  recordings share the ISRC, the tagged one (3:56) and a 3:30 one.
