# Library tools

Three small command-line tools to keep a home movie / TV / anime library tidy — the kind you serve with Jellyfin, Plex or Kodi.

- **`find_missing`** — tells you what is missing: gaps in seasons, missing episodes, missing films of a collection, duplicates.
- **`rename_media`** — gives every file a clean name (`Show - S01E05.mkv`, `Movie (1994).mkv`), optionally with titles in the language of your choice, with a preview first and a one-command undo.
- **`library_gallery`** — turns your library into a browsable poster gallery (one HTML page that works offline).

They only need Python (or the Windows `.exe`) and a free [TMDB](https://www.themoviedb.org) API key. Nothing is ever deleted, and nothing is renamed until you confirm.

> This product uses the TMDB API but is not endorsed or certified by TMDB.

---

## Quick start

### Windows (.exe)

1. Download `library-tools-windows.zip` from the [Releases](../../releases) page and unzip it.
2. Get a free TMDB API key: themoviedb.org → *Settings* → *API*. Both the short *API key* and the long *Read Access Token* work.
3. Double-click `find_missing.exe`. On first start it asks for your library folders and your TMDB key, and saves them next to the `.exe` (`config.json`, `tmdb_key.txt`).
4. Run `rename_media.exe` and `library_gallery.exe` the same way. Keep the three `.exe` files in the same folder so they share the settings.

Windows SmartScreen may warn that the app is unsigned: click *More info* → *Run anyway*. All the code is in [`src/`](src).

### From source (Windows, Linux, macOS)

Python 3.9 or newer, no extra packages needed:

```
python3 src/find_missing.py
python3 src/rename_media.py
python3 src/library_gallery.py
```

---

## The tools

### `find_missing` — what's missing in my library?

Scans your movie, TV and anime folders and prints a report (also saved to `media_report.txt`):

- missing seasons and episodes, missing sequels in numbered franchises;
- popular and currently airing titles you don't have yet.

After each scan, a small menu:

| Key | Action |
|---|---|
| `Enter` | Rescan (e.g. after adding files) |
| `c` | Online completeness check against TMDB: missing films of a collection, new or missing seasons, missing last episodes |
| `d` | Duplicate finder: same movie or episode stored twice, with the quality and size of each copy so you can pick which to keep (nothing is deleted) |
| `w` | Explain why a movie is not identified on TMDB |
| `q` | Quit |

### `rename_media` — clean file names

```
Dark.S01E05.1080p.WEB.x264-GRP.mkv     ->  Dark - S01E05.mkv
[Judas] Bleach - ... - S17E26v2.mkv    ->  Bleach - S17E26.mkv
double episodes                         ->  Show - S03E01-E02.mkv
files in a Specials / OVA folder        ->  Show - S00E01.mkv
Scream.1996.1080p.BluRay.x264-GRP.mkv  ->  Scream (1996).mkv
```

Subtitle and `.nfo` files next to a video are renamed along with it. You choose which libraries to process, then answer a few questions:

- **Organise into folders?** `Show/Season 01/`, specials in `Season 00/` — the layout Jellyfin, Plex and Kodi expect.
- **Rename titles in which language?** Type a language code (`en`, `fr`, `es`, `de`, `ja`, `pt-BR`…) to use the TMDB title in that language, or press Enter to keep the current names. With `en`: `Le Roi Lion (1994).mkv` → `The Lion King (1994).mkv`; with `fr`, the other way round.
- **Add missing years?** Movies without a year in their name get one from TMDB.

Safety:

- Nothing changes until you type `YES` after the preview. The full plan is written to `rename_plan.txt`.
- Files that would end up with the same name are left alone and listed as conflicts (usually duplicates — try `find_missing`, option `d`).
- Every batch is logged in `rename_log.json`.

Other commands:

```
python3 src/rename_media.py --undo                  # revert the last batch
python3 src/rename_media.py --season "D:/Show/S2" 2  # renumber a wrongly numbered season (S01E05 -> S02E05)
```

### `library_gallery` — poster gallery

Builds `gallery/index.html` and opens it in your browser: tabs for Movies / TV shows / Anime, filters by genre, unidentified titles or missing episodes, search by English or French title or year, posters, synopsis, genres, rating, and the files you have for each title. Posters are downloaded once into `gallery/posters/`, so the page works offline afterwards. Your library is only read, never modified.

It also writes `gallery/titles.txt`, a plain list of every title (English title, French title, year, type).

Options:

```
python3 src/library_gallery.py --no-open   # don't open the browser at the end
python3 src/library_gallery.py --offline   # no TMDB: titles from file names, no posters
```

The output folder can be changed with the `GALLERY_DIR` environment variable.

---

## Settings

### TMDB key

Looked up in this order: the `TMDB_API_KEY` environment variable, then a `tmdb_key.txt` file next to the script / `.exe`. If neither exists, the tool asks for it once and saves it. Without a key, the offline features still work.

### `config.json` (optional)

Created on first start; you can also write it yourself next to the scripts / `.exe`:

```json
{
  "movie_path": ["D:/Films"],
  "tv_path": ["E:/Series"],
  "anime_path": ["F:/Anime", "F:/Anime 2"],
  "ignore_shows": [],
  "ignore_collections": [],
  "year_overrides": {},
  "keep_names": []
}
```

| Key | Meaning |
|---|---|
| `movie_path`, `tv_path`, `anime_path` | One folder or a list of folders per library. Leave one empty to skip that library. |
| `ignore_shows` | Shows left out of every check. Compared lowercase without punctuation; `"doctor who"` covers all its versions. |
| `ignore_collections` | TMDB collections you don't want in the "missing films" report. |
| `year_overrides` | Force a year for a title, e.g. `{"Hercules": 1997}` — useful when several films share a name. |
| `keep_names` | Titles `rename_media` never translates, e.g. `["Le Chateau de Cagliostro"]` — for when TMDB gives an old or odd title. Year, accents and edition tags are ignored. |

Delete `config.json` and `tmdb_key.txt` to start over. More advanced tweaks (title aliases, episodes to ignore, franchise aliases) are commented at the top of [`src/find_missing.py`](src/find_missing.py).

---

## Building the .exe

The [GitHub Actions workflow](.github/workflows/build.yml) builds the three `.exe` files with PyInstaller. Pushing a tag such as `v1.0.0` builds them and attaches `library-tools-windows.zip` to the release; it can also be run by hand from the *Actions* tab.

## Privacy

Never publish `tmdb_key.txt`, `config.json` or `.env` files. They, along with the caches, reports and generated gallery, are already listed in [`.gitignore`](.gitignore).

## License

MIT — see [LICENSE](LICENSE).
