# Library tools

Three small tools to keep a movie / TV / anime library tidy. They use the free [TMDB](https://www.themoviedb.org) API.
This product uses the TMDB API but is not endorsed or certified by TMDB.

| Tool | What it does |
|---|---|
| `find_missing` | Finds holes in seasons, missing films of a collection, duplicates; compares with TMDB. |
| `rename_media` | Cleans file names (`Show - S01E05.mkv`, `Movie (Year).mkv`), optionally with English titles. Always shows a preview first; type `YES` to apply; `--undo` reverts. |
| `library_gallery` | Builds a browsable poster gallery (HTML) from your library. |

## Windows (.exe)
1. Download `library-tools-windows.zip` from the [Releases](../../releases) page and unzip it.
2. Get a free TMDB API key (themoviedb.org → Settings → API).
3. Double-click `find_missing.exe`. On first start it asks for your folders and your TMDB key and saves them next to the `.exe`
   (`config.json`, `tmdb_key.txt`). Delete those files to start over.
4. Same for `rename_media.exe` and `library_gallery.exe`. Keep the three `.exe` files in the same folder to share the settings.

Windows SmartScreen may warn about an unsigned app ("More info" → "Run anyway"). The code is all in `src/`.

## From source (Windows, Linux, macOS)
Python 3.9+, no extra packages:
```
python3 src/find_missing.py
python3 src/rename_media.py
python3 src/library_gallery.py
```

## Settings (`config.json`, optional)
```json
{
  "movie_path": ["D:/Films"],
  "tv_path": ["E:/Series"],
  "anime_path": ["F:/Anime"],
  "ignore_shows": [],
  "ignore_collections": [],
  "year_overrides": {}
}
```
Never publish `tmdb_key.txt`, `config.json` or `.env` files (they are in `.gitignore`).
