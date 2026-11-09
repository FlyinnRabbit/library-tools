#!/usr/bin/env python3
"""
Media library checker.

* Scans your 3 drives (movies / TV / anime)
* Finds holes: missing movie sequels, missing seasons, missing episodes
* Recommends popular / airing titles you don't have yet
* After each scan you get a small menu:
    Enter  rescan the drives (e.g. after adding files)
    c      online completeness check: movie collections, new / missing seasons,
           missing last episodes (compares your files with TMDB)
    d      duplicate finder: same movie / same episode stored twice
           (nothing is ever deleted - it only tells you what to keep)
    q      quit

TMDB key: set the TMDB_API_KEY environment variable, or put the key alone in a
file called tmdb_key.txt next to this script.
"""
import os
import re
import difflib
import sys
import json
import time
import threading
import unicodedata
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from datetime import date
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# CONFIGURATION
# ==========================================
# Folders to analyse. A single folder, or a list of folders if your library is spread
# over several root folders. Only these folders (and what is inside them) are scanned:
#   MOVIE_PATH = "D:/Films"
#   MOVIE_PATH = ["D:/Films", "D:/Polyglot_Movies/films"]
MOVIE_PATH = ""      # e.g. "D:/Films"
TV_PATH = ""         # e.g. "E:/Series"
ANIME_PATH = []      # e.g. ["F:/Anime", "F:/Anime 2"]

# Optional: a page where people can support this tool (Ko-fi, PayPal.me, GitHub Sponsors...).
# Leave "" to hide it. When set, the menu gets an extra [s] entry that opens this page.
DONATE_URL = ""

MAX_RECS = 10          # recommendations shown per category
TMDB_PAGES = 2         # 20 titles per page
MAX_MOVIE_FRANCHISE = 12   # ignore "gaps" in numbers above this (Apollo 13, etc.)

# Sequels whose title has no number. Format: normalized title phrase -> (franchise, part).
# Add your own lines here (lowercase, no punctuation).
MOVIE_ALIASES = {
    "jurassic park the lost world": ("Jurassic Park", 2),
    "the lost world jurassic park": ("Jurassic Park", 2),
    "jigsaw": ("Saw", 8),
    "spiral from the book of saw": ("Saw", 9),
    "saw x": ("Saw", 10),
}

# Episodes you DO have but the script can't see (e.g. a double episode stored in one
# file named only S03E01). Format: "normalized show name": [(season, episode)]
# or [(season, first_episode, last_episode)].
IGNORE_MISSING = {
    # "my show": [(3, 2)],
}

# TMDB collections you do not want in the "missing films" report (a huge or loose collection
# of which you own one film on purpose). Name as TMDB writes it, case and punctuation ignored.
IGNORE_COLLECTIONS = []

# Shows left out of every check (gaps, TMDB comparison, "not identified" list). Names are
# compared lowercase without punctuation; "doctor who" covers all its versions (1963/2005/2024).
IGNORE_SHOWS = []

# Same TV show / anime stored under different names. The first name is the one used
# in the report. Names are compared lowercase without punctuation.
SHOW_ALIASES = [
    ("la casa de papel", "money heist"),
    ("detective conan", "case closed", "meitantei conan"),
    ("bleach", "bleach thousand year blood war"),
    ("les simpsons", "the simpsons", "les simpson"),
    ("l attaque des titans", "attack on titan", "shingeki no kyojin"),
    ("les chevaliers du zodiaque", "saint seiya", "knights of the zodiac"),
    ("avatar le dernier maitre de l air", "avatar the last airbender"),
    ("a returner magic should be special", "a returner's magic should be special"),
    ("les animaux du bois de quat sous", "animals of farthing wood"),
    ("blue dragon tenkai no shichi ryuu", "blue dragon trials of the seven shadows"),
]

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv'}
SKIP_DIRS = {'$recycle.bin', 'system volume information', 'recycler',
             'sample', 'samples', 'extras', 'featurettes', 'trailers'}

REPORT = []
FILES = {}     # drive path -> full paths of the video files, same order as the scan entries
SCANS = {}     # kind -> (entries, files) from the latest scan


def out(line="", show=True):
    """Print a line and keep it for the saved report (show=False: report only)."""
    if show:
        print(line)
    REPORT.append(line)


# ==========================================
# NAME CLEANING / PARSING
# ==========================================
def normalize_name(name):
    """Lowercase, no accents, no punctuation, single spaces."""
    s = unicodedata.normalize('NFKD', str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace('&', ' and ')
    s = re.sub(r"['’`]", '', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return s.strip()


def basic_clean(raw):
    """Remove [group] (tags) {tags}, turn dots/underscores into spaces."""
    s = re.sub(r'\[[^\]]*\]|\([^)]*\)|\{[^}]*\}', ' ', str(raw))
    s = re.sub(r'[._]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


JUNK = re.compile(
    r'\b(2160p|1080p|1080i|720p|576p|480p|4k|uhd|bluray|blu ray|brrip|bdrip|'
    r'webrip|web dl|webdl|hdtv|dvdrip|x264|x265|h264|h265|hevc|avc|aac|ac3|'
    r'dts|atmos|hdr|10bit|remux|proper|repack|multi|vostfr|vff|truefrench|'
    r'extended|unrated|imax|hybrid|integrale|integral|intégrale|subfrench|(?:japanese|english|french|jap|jp) ver(?:sion)?|'
    r'french(?=\s+(?:\d{3,4}p|web|bluray|webrip|dvdrip|hdtv|x26\d|h26\d|multi))|'
    r'doc(?=\s+(?:french|multi|vff|\d{3,4}p)))\b', re.I)
YEAR = re.compile(r'\b(19\d\d|20[0-2]\d)\b')


def cut_junk(s):
    """Cut the title at the first release year / quality tag (never at pos 0)."""
    cut = len(s)
    for rx in (JUNK, YEAR):
        for m in rx.finditer(s):
            if m.start() > 0:
                cut = min(cut, m.start())
                break
    return s[:cut].strip(' -:')


ROMAN = re.compile(r'^(?=[IVX])X{0,2}(?:IX|IV|V?I{0,3})$')


def roman_to_int(s):
    vals = {'I': 1, 'V': 5, 'X': 10}
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        if ch not in vals:
            return None
        v = vals[ch]
        total += -v if v < prev else v
        prev = max(prev, v)
    return total or None


def to_number(tok):
    if tok.isdigit():
        return int(tok)
    return roman_to_int(tok) if ROMAN.match(tok.upper()) else None


def parse_numbered_title(title):
    """'Scream 2' / 'Scream VI' / 'Name Part 3' -> ('Scream', 2). Else (None, None)."""
    if re.fullmatch(r'saw 3d', title, re.I):
        return 'Saw', 7
    m = re.search(r'^(.+?)\s+(?:part|partie|pt|vol|volume|chapter|chapitre|episode)\.?\s*'
                  r'(\d{1,2}|[IVX]{1,4})\b', title, re.I)
    if m:
        n = to_number(m.group(2))
        name = m.group(1).strip(' -:')
        if n and name:
            return name, n
    tokens = title.split()
    for i, tok in enumerate(tokens):
        if i == 0:
            continue
        t = tok.strip(':,-')
        n = None
        if t.isdigit():
            n = int(t) if 1 <= int(t) <= 30 else None
        elif (len(t) > 1 or t == 'V') and ROMAN.match(t):   # uppercase roman only (lone X = "Project X", "Malcolm X")
            n = roman_to_int(t)
        if n:
            name = ' '.join(tokens[:i]).strip(' -:')
            if name:
                return name, n
    return None, None


SPECIAL_DIR = re.compile(r'^(?:specials?|sp|ovas?|oavs?|extras?|bonus)$', re.I)
SEASON_DIR = re.compile(r'^(?:season|saison|series|s)\s*0*(\d{1,2})\b', re.I)
NONSHOW_DIR = re.compile(r'^(?:season|saison|series|specials?|extras?|s\d{1,2})\b', re.I)
GENERIC = {'', 'episode', 'ep', 'season', 's', 'e', 'special', 'specials', 'vol', 'volume'}

EP_PATTERNS = [
    re.compile(r'^(?P<show>.*?)[\s-]*\bS(?P<s>\d{1,2})\s*E(?P<e>\d{1,4})(?!\d)', re.I),
    re.compile(r'^(?P<show>.*?)[\s-]*\b(?P<s>\d{1,2})x(?P<e>\d{2,3})(?!\d)', re.I),
    # "S05.01" / "S05 01" : season then bare episode number, no "E" (not "S01 720p")
    re.compile(r'^(?P<show>.*?)[\s-]*\bS(?P<s>\d{1,2})\s+(?P<e>\d{1,3})(?![\dA-Za-z])', re.I),
    re.compile(r'^(?P<show>.*?)\s+-\s+(?:E(?:p)?\.?\s*)?(?P<e>\d{1,4})(?:v\d)?\b', re.I),
    re.compile(r'^(?P<show>.*?)\s+(?:Ep(?:isode)?\.?|E)\s*(?P<e>\d{1,4})\b', re.I),
    # fallbacks (tried last): "Naruto 036 ''Title''", "Épisode 026", "11 - Title" / "08 Title"
    # (the last two have no show name: it comes from the folder)
    re.compile(r'^(?P<show>.*?)[\s-]*\b(?P<sp>SP|OAV|OVA|OAD)\s*(?P<e>\d{1,3})\b', re.I),   # S1 - SP05, OAV 3 (= specials)
    re.compile(r'^(?P<show>.*?)\s+(?P<e>\d{1,4})(?=\s+(?:final\s+)?[\'"‘’“”«])', re.I),
    re.compile(r'^(?P<show>.*?)\s+(?P<e>\d{3,4})\s+-\s+', re.I),                           # OP 542 - Title
    re.compile(r'^(?P<show>)(?:[EÉ]pisode|Ep)\s*(?P<e>\d{1,4})\b', re.I),
    re.compile(r'^(?P<show>)(?P<e>\d{1,3})(?:\s*-\s+|\s+)(?=[A-Za-z])', re.I),
    re.compile(r'^(?P<show>.*?)\s+(?P<e>\d{1,4})\s*$', re.I),                              # Saint Seiya 093
]


# What may follow the first episode number in a multi-episode file:
# E01E02, E01-E02, E01-02, E01 & 02, E01+E02, E01 et 02, E01-S01E02 ...
TAIL = re.compile(
    r'^\s*(?:(?:-|&|\+|,|/|and\b|et\b)\s*(?:S\d{1,2}\s*)?(?P<E1>E)?|(?P<E2>E))\s*(?P<n>\d{1,3})'
    r'(?!\d|(?![Ee]\d)[A-Za-z])', re.I)


def extend_episodes(first, tail):
    """Double/triple episodes: [first, first+1, ...]. Bare numbers must be consecutive
    (so '720p' or '- 10 Things' are never read as episodes); an explicit E-range
    like E01-E05 may span several episodes."""
    eps = [first]
    for _ in range(12):
        m = TAIL.match(tail)
        if not m:
            break
        n = int(m.group('n'))
        explicit = bool(m.group('E1') or m.group('E2'))
        last = eps[-1]
        if n == last + 1:
            eps.append(n)
        elif explicit and last < n <= last + 30:
            eps.extend(range(last + 1, n + 1))
        else:
            break
        tail = tail[m.end():]
    return eps


# "Blue Dragon - Saison 2", "Show Season 3": a season written at the end of a folder name
SEASON_TAIL = re.compile(r'(?:^|[\s._-])(?:saison|season)[\s._-]*0*(\d{1,2})$', re.I)


def episode_info(name, parents=()):
    """-> (show, season, [episode numbers]) or None."""
    raw = basic_clean(name)
    m = None
    for rx in EP_PATTERNS:
        m = rx.match(raw)
        if m:
            break
    if not m:
        return None
    gd = m.groupdict()
    e = int(gd['e'])
    eps = extend_episodes(e, raw[m.end():]) if 's' in gd else [e]
    season = gd.get('s')
    if season is None:
        for p in reversed(parents):
            c = basic_clean(p)
            if SPECIAL_DIR.match(c):          # files in a Specials folder = season 0
                season = 0
                break
            sm = SEASON_DIR.match(c) or SEASON_TAIL.search(c)
            if sm:
                season = sm.group(1)
                break
    season = int(season) if season is not None else 1
    if gd.get('sp'):                          # SP05 / OAV 3 / OVA 1 = special
        season = 0

    show = cut_junk(gd['show']).strip(' -:')
    show = re.sub(r'\s+(19\d\d|20[0-2]\d)$', '', show)
    if normalize_name(show) in GENERIC:
        show = ''
        for p in reversed(parents):
            c = basic_clean(p)
            if c and not NONSHOW_DIR.match(c):
                show = cut_junk(c)
                break
    if not normalize_name(show):
        return None
    return show, season, eps


def compress(nums):
    """[1,2,3,5] -> '1-3, 5'"""
    nums = sorted(nums)
    parts, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(parts)


# ==========================================
# SCANNING
# ==========================================
def scan_directory(path):
    """Returns [(filename_without_ext, (parent, folders...))] or None if missing."""
    if not os.path.isdir(path):
        out(f"⚠️ {path} does not exist or is not connected - skipped.")
        return None
    entries, paths = [], []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith('$')]
        rel = os.path.relpath(root, path)
        parents = () if rel == '.' else tuple(rel.replace('\\', '/').split('/'))
        for f in files:
            name, ext = os.path.splitext(f)
            if ext.lower() in VIDEO_EXTENSIONS and 'sample' not in name.lower():
                entries.append((name, parents))
                paths.append(os.path.join(root, f))
    FILES[path] = paths
    return entries


def as_list(p):
    if not p:
        return []
    return [p] if isinstance(p, str) else list(p)


def app_dir():
    """Folder holding the script, or the .exe when packaged. Settings and cache live here."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_FILE = os.path.join(app_dir(), "config.json")


def load_config():
    """config.json (next to the script / .exe) overrides the settings above, so the packaged
    .exe needs no editing. Keys: movie_path, tv_path, anime_path, donate_url, ignore_shows,
    ignore_collections, year_overrides."""
    global MOVIE_PATH, TV_PATH, ANIME_PATH, DONATE_URL, IGNORE_SHOWS, IGNORE_COLLECTIONS
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    MOVIE_PATH = cfg.get("movie_path", MOVIE_PATH)
    TV_PATH = cfg.get("tv_path", TV_PATH)
    ANIME_PATH = cfg.get("anime_path", ANIME_PATH)
    DONATE_URL = cfg.get("donate_url", DONATE_URL)
    IGNORE_SHOWS = list(cfg.get("ignore_shows", IGNORE_SHOWS))
    IGNORE_COLLECTIONS = list(cfg.get("ignore_collections", IGNORE_COLLECTIONS))
    return cfg


def ensure_paths():
    """If no library folder is set, ask for them once and save them in config.json."""
    global MOVIE_PATH, TV_PATH, ANIME_PATH
    if any(as_list(p) for p in (MOVIE_PATH, TV_PATH, ANIME_PATH)):
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print("No library folder is set (see config.json or the top of find_missing.py).")
        return False
    print("First start: tell me where your library is (Enter = skip; several folders: separate with ;)")
    def ask(label):
        v = input(f"  {label}: ").strip().strip('"')
        return [x.strip().strip('"') for x in v.split(';') if x.strip()]
    m, t, a = ask("Movies folder"), ask("TV shows folder"), ask("Anime folder")
    if not (m or t or a):
        print("No folder given.")
        return False
    MOVIE_PATH, TV_PATH, ANIME_PATH = m, t, a
    cfg = {}
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg.update(movie_path=m, tv_path=t, anime_path=a)
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        print(f"Saved to {CONFIG_FILE} (edit or delete it to change folders).\n")
    except OSError as e:
        print(f"(could not save {CONFIG_FILE}: {e})")
    return True



load_config()


def show_path(p):
    return ", ".join(as_list(p))


def scan_library(paths):
    """Scan one folder or a list of folders. -> (entries, files), or (None, []) if none exists.
    A file reachable through two overlapping folders is only counted once."""
    entries, files, seen, found = [], [], set(), False
    for p in as_list(paths):
        e = scan_directory(p)
        if e is None:
            continue
        found = True
        for entry, full in zip(e, FILES.get(p, [])):
            key = os.path.normcase(os.path.abspath(full))
            if key not in seen:
                seen.add(key)
                entries.append(entry)
                files.append(full)
    return (entries, files) if found else (None, [])


# ==========================================
# GAP DETECTION
# ==========================================
ORDER_PREFIX = re.compile(r'^(?:\d{1,2}\s*[.)]\s+|\d{1,2}\s+-\s+|0\d[\s._-]+)(?=\S)')


def strip_order_prefix(raw):
    """'01. Spider-Man', '24 - Spectre', '03 X-Men' -> collection order number removed.
    '21 Jump Street', '300', '8.Mile', '2012' are real titles and stay untouched."""
    return ORDER_PREFIX.sub('', str(raw), count=1)


def movie_title_year(raw):
    """(title, year) of a movie file/folder name. The title stops at the '(YYYY)' when
    there is one, so tags written after the year (Hybrid, IMAX, MULTi ...) never leak in."""
    raw = strip_order_prefix(raw)
    flat = re.sub(r'[._]+', ' ', raw)
    pm = re.search(r'\((19\d\d|20[0-2]\d)\)', flat)
    if pm:
        year = int(pm.group(1))
        head = basic_clean(flat[:pm.start()])
        title = cut_junk(head if head else basic_clean(raw))
    else:
        ys = YEAR.findall(flat)
        year = int(ys[-1]) if ys else None
        cleaned = basic_clean(raw)
        m = re.match(r'^(.+?)\s+-\s+(19\d\d|20[0-2]\d)\s+-\s+(.+)$', cleaned)   # James Bond 007 - 1967 - Casino Royale
        m2 = re.match(r'^(19\d\d|20[0-2]\d)\s+(.+)$', cleaned)                    # 1987.Predator
        rest = cut_junk(m2.group(2)) if m2 else ""
        if m:
            title, year = cut_junk(f"{m.group(1)} - {m.group(3)}"), int(m.group(2))
        elif m2 and len(ys) == 1 and re.search(r'[^\W\d_]{2,}', rest) and not JUNK.match(rest):
            title, year = rest, int(m2.group(1))
        else:
            title = cut_junk(cleaned)
    return title, year


def alias_lookup(title):
    n = f" {normalize_name(title)} "
    for phrase, val in MOVIE_ALIASES.items():
        if f" {phrase} " in n:
            return val
    return None


def movie_gaps(entries):
    franchises, bases = {}, set()
    for name, parents in entries:
        title = movie_title_year(name)[0]
        fname, num = alias_lookup(title) or parse_numbered_title(title)
        if fname:
            k = normalize_name(fname)
            if k:
                franchises.setdefault(k, {'display': fname, 'nums': set()})['nums'].add(num)
        else:
            bases.add(normalize_name(title))     # unnumbered = could be part 1
        # the movie's folder name can also reveal an unnumbered first film
        for p in parents:
            c = cut_junk(basic_clean(p))
            if (c and not NONSHOW_DIR.match(c) and parse_numbered_title(c)[0] is None
                    and not alias_lookup(c)):
                bases.add(normalize_name(c))
    bases.discard('')
    lines = []
    for k, d in sorted(franchises.items()):
        nums = set(d['nums'])
        # part 1 usually has no number and often a subtitle:
        # "Creed L'heritage de Rocky Balboa" counts as part 1 of "Creed"
        if any(b == k or b.startswith(k + " ") for b in bases):
            nums.add(1)
        if len(nums) < 2 or max(nums) > MAX_MOVIE_FRANCHISE:
            continue
        missing = sorted(set(range(1, max(nums) + 1)) - nums)
        if missing:
            lines.append(f"{d['display']}: missing part(s) {compress(missing)} "
                         f"(you have {compress(nums)})")
    return lines


def canon_show(key):
    for group in SHOW_ALIASES:
        names = [normalize_name(x) for x in group]
        if key in names:
            return names[0]
    return key


def is_ignored(show_key, season, ep):
    for rule in IGNORE_MISSING.get(show_key, ()):
        if rule[0] != season:
            continue
        lo = rule[1]
        hi = rule[2] if len(rule) > 2 else rule[1]
        if lo <= ep <= hi:
            return True
    return False


def episode_gaps(entries, anime=False):
    shows = collect_shows(entries)

    lines = []
    for k, d in sorted(shows.items()):
        seasons = d['seasons']
        real = sorted(s for s in seasons if s > 0)
        msgs = []
        if not anime and len(real) > 1:
            miss_s = [s for s in range(1, max(real) + 1) if s not in seasons]
            if miss_s:
                msgs.append(f"missing season(s) {compress(miss_s)}")
        for s in real:
            eps = seasons[s]
            lo = min(eps) if anime else 1
            missing = sorted(set(range(lo, max(eps) + 1)) - eps)
            if anime:
                # a jump of 100+ episodes is mixed numbering (per-season vs
                # absolute, e.g. One Piece), not a hole: ignore those gaps
                have = sorted(eps)
                skip = set()
                for a, b in zip(have, have[1:]):
                    if b - a > 100:
                        skip.update(range(a + 1, b))
                missing = [m for m in missing if m not in skip]
            missing = [m for m in missing if not is_ignored(k, s, m)]
            if missing:
                label = "" if anime and len(real) == 1 else f"S{s:02d} "
                msgs.append(f"{label}missing ep {compress(missing)}")
        if msgs:
            lines.append(f"{d['display']}: " + " | ".join(msgs))
    return lines


# ==========================================
# ONLINE DATA
# ==========================================
def http_json(url, headers=None, data=None, timeout=15):
    h = {'User-Agent': 'Mozilla/5.0'}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def fetch_airing_anime():
    print("🌐 Fetching currently airing anime from AniList...")
    query = '''
    query {
      Page(page: 1, perPage: 50) {
        media(status: RELEASING, type: ANIME, sort: POPULARITY_DESC, isAdult: false) {
          title { romaji english }
        }
      }
    }'''
    try:
        res = http_json("https://graphql.anilist.co",
                        headers={'Content-Type': 'application/json'},
                        data=json.dumps({'query': query}).encode('utf-8'))
        items = []
        for m in res['data']['Page']['media']:
            t = m.get('title') or {}
            names = [x for x in (t.get('english'), t.get('romaji')) if x]
            if names:
                items.append({'display': names[0], 'names': names})
        print(f"✅ {len(items)} airing anime fetched.")
        return items
    except Exception as e:
        print(f"⚠️ AniList unavailable: {e}")
        return []


def load_tmdb_key():
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if key:
        return key
    for folder in (app_dir(), os.getcwd()):
        for fname in ("tmdb_key.txt", "tmdb_key.txt.txt", "tmdb_key"):
            p = os.path.join(folder, fname)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding='utf-8-sig', errors='replace') as f:   # -sig: Notepad BOM
                    text = f.read()
            except OSError:
                continue
            # accept a bare key, "KEY=...", quotes, extra lines...
            m = re.search(r'eyJ[\w-]+\.[\w-]+\.[\w-]+|\b[0-9a-fA-F]{32}\b', text)
            key = m.group(0) if m else text.strip().strip('"\'')
            if key:
                print(f"🔑 TMDB key loaded from {p}")
                return key
    target = os.path.join(app_dir(), 'tmdb_key.txt')
    if sys.stdin and sys.stdin.isatty():
        k = input("No TMDB key yet. Paste your free TMDB API key (Enter = continue without): ").strip().strip('"\'')
        if k:
            try:
                with open(target, 'w', encoding='utf-8') as f:
                    f.write(k)
                print(f"Saved to {target}")
            except OSError as e:
                print(f"(could not save the key: {e})")
            return k
    print(f"⚠️ No TMDB key file found. Put it in {target} "
          "(check Windows isn't hiding a second .txt extension).")
    return ""


def fetch_trending_tmdb(media_type, key):
    if len(key) < 10:
        print(f"⚠️ No TMDB key found - skipping trending {media_type} recommendations.")
        return []
    print(f"🌐 Fetching trending {media_type} from TMDB...")
    headers, items = {}, []
    bearer = key.startswith("eyJ")           # v4 token vs v3 key
    if bearer:
        headers['Authorization'] = f'Bearer {key}'
    try:
        for page in range(1, TMDB_PAGES + 1):
            url = f"https://api.themoviedb.org/3/trending/{media_type}/week?page={page}&language=en-US"
            if not bearer:
                url += f"&api_key={key}"
            data = http_json(url, headers=headers)
            for r in data.get('results', []):
                names = [x for x in (r.get('title') or r.get('name'),
                                     r.get('original_title') or r.get('original_name')) if x]
                if names:
                    items.append({'display': names[0], 'names': names})
        print(f"✅ {len(items)} trending {media_type} titles fetched.")
        return items
    except urllib.error.HTTPError as e:
        print(f"⚠️ TMDB error {e.code}" + (" - the API key is invalid." if e.code == 401 else ""))
    except Exception as e:
        print(f"⚠️ TMDB unavailable: {e}")
    return []


# ==========================================
# RECOMMENDATIONS
# ==========================================
def local_titles(entries, episodic):
    titles = set()
    for name, parents in entries:
        t = None
        if episodic:
            info = episode_info(name, parents)
            if info:
                t = info[0]
        titles.add(normalize_name(t if t else cut_junk(basic_clean(name))))
        for p in parents:
            c = basic_clean(p)
            if c and not NONSHOW_DIR.match(c):
                titles.add(normalize_name(cut_junk(c)))
    for group in SHOW_ALIASES:                 # owning one name = owning them all
        names = [normalize_name(x) for x in group]
        if any(n in titles for n in names):
            titles.update(names)
    titles.discard('')
    return titles


def recommend(online, locals_, limit):
    blob = "\n".join(f" {t} " for t in locals_)
    long_locals = [t for t in locals_ if len(t) >= 6]
    result = []
    for item in online:
        owned = False
        for raw in item['names']:
            n = normalize_name(raw)
            if not n:
                continue
            if len(n) <= 4:
                owned = n in locals_                   # short titles: exact match only
            else:
                owned = (f" {n} " in blob) or any(f" {l} " in f" {n} " for l in long_locals)
            if owned:
                break
        if not owned:
            result.append(item['display'])
    return result[:limit]


# ==========================================
# TMDB CLIENT (cached on disk, parallel)
# ==========================================
HERE = app_dir()
CACHE_FILE = os.path.join(HERE, "tmdb_cache.json")
CACHE_TTL = {'search': 30 * 86400, 'movie': 30 * 86400, 'collection': 3 * 86400,
             'tv': 86400, 'alt': 30 * 86400}
_cache = {}
_lock = threading.Lock()
_save_lock = threading.Lock()
_state = {'key': '', 'auth_failed': False, 'errors': 0, 'dirty': 0, 'loaded': False}


def cache_load():
    if _state['loaded']:
        return
    _state['loaded'] = True
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            _cache.update(json.load(f))
    except (OSError, ValueError):
        pass


def cache_save():
    with _lock:
        snap = dict(_cache)
        _state['dirty'] = 0
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError as e:
        print(f"(could not save the TMDB cache: {e})")


def _pick(d, *keys):
    return {k: d.get(k) for k in keys}


def _trim_search(d):
    keys = ('id', 'title', 'original_title', 'name', 'original_name', 'release_date', 'first_air_date',
            'popularity')
    return {'results': [_pick(r, *keys) for r in d.get('results', [])[:8]]}


def _trim_movie(d):
    c = d.get('belongs_to_collection')
    return {'id': d.get('id'), 'collection': {'id': c['id'], 'name': c.get('name')} if c else None}


def _trim_collection(d):
    return {'name': d.get('name'),
            'parts': [_pick(p, 'id', 'title', 'release_date') for p in d.get('parts', [])]}


def _trim_tv(d):
    last, nxt = d.get('last_episode_to_air'), d.get('next_episode_to_air')
    return {'id': d.get('id'), 'name': d.get('name'), 'status': d.get('status'),
            'seasons': [_pick(s, 'season_number', 'episode_count', 'air_date') for s in d.get('seasons', [])],
            'last': _pick(last, 'season_number', 'episode_number') if last else None,
            'next': _pick(nxt, 'season_number', 'episode_number', 'air_date') if nxt else None}


def _trim_alt(d):
    return {'titles': [x.get('title') for x in d.get('results', []) if x.get('title')]}


TRIM = {'search': _trim_search, 'movie': _trim_movie, 'collection': _trim_collection,
        'tv': _trim_tv, 'alt': _trim_alt}


def tmdb_get(kind, path, **params):
    """Cached GET on api.themoviedb.org/3/<path>. `kind` picks the cache lifetime.
    Returns trimmed data, or None (not found / error)."""
    q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in sorted(params.items()))
    ck = f"{path}?{q}"
    with _lock:
        hit = _cache.get(ck)
    if hit and time.time() - hit['t'] < CACHE_TTL[kind]:
        return hit['d']
    key = _state['key']
    if _state['auth_failed'] or not key:
        return None
    headers, url = {}, f"https://api.themoviedb.org/3/{path}?{q}"
    if key.startswith('eyJ'):
        headers['Authorization'] = f'Bearer {key}'
    else:
        url += f"&api_key={key}"
    data, ok = None, False
    for attempt in range(4):
        try:
            data = TRIM[kind](http_json(url, headers=headers))
            ok = True
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                ok = True
                break
            if e.code == 401:
                _state['auth_failed'] = True
                return None
            time.sleep(1 + attempt * 2 if e.code == 429 else 0.5)
        except Exception:
            time.sleep(0.5)
    if not ok:
        with _lock:
            _state['errors'] += 1
        return None
    with _lock:
        _cache[ck] = {'t': time.time(), 'd': data}
        _state['dirty'] += 1
    return data


def run_parallel(fn, items, label, workers=8):
    total = len(items)
    results = [None] * total
    if not total:
        return results
    counter = [0]

    def job(i):
        try:
            results[i] = fn(items[i])
        except Exception:
            with _lock:
                _state['errors'] += 1
        with _lock:
            counter[0] += 1
            n = counter[0]
        if n % 25 == 0 or n == total:
            print(f"\r   {label}: {n}/{total}", end="", flush=True)
        if _state['dirty'] >= 400 and _save_lock.acquire(False):
            try:
                cache_save()
            finally:
                _save_lock.release()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(job, range(total)))
    print()
    return results


# ==========================================
# TITLE MATCHING AGAINST TMDB
# ==========================================
ROMAN_DIGIT = {'ii': '2', 'iii': '3', 'iv': '4', 'v': '5', 'vi': '6',
               'vii': '7', 'viii': '8', 'ix': '9', 'x': '10'}


WORD_DIGIT = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7',
              'eight': '8', 'nine': '9', 'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13'}


def match_key(s):
    """Comparison key: no accents/punctuation, leading 'the' dropped, Roman = digits."""
    toks = normalize_name(s).split()
    if toks and toks[0] == 'the':
        toks = toks[1:]
    toks = toks[:1] + [t for t in toks[1:] if t not in ('et', 'and')]   # "Tigre et dragon" = "Tigre & Dragon"
    if not toks:
        return ""
    return " ".join([toks[0]] + [ROMAN_DIGIT.get(t) or WORD_DIGIT.get(t, t) for t in toks[1:]])


def _fuzzy(q, k, same_year=False):
    """Same film under another title: one title inside the other (3+ words), or very similar.
    Only used together with a matching year."""
    if len(k) < 6 or len(q) < 6:
        return False
    a, b = q.split(), k.split()
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 2 and len(" ".join(short)) >= 10 and " ".join(short) in " ".join(long_):
        return True                              # "force awakens" in "star wars force awakens"
    nums = lambda t: sorted(w for w in t.split() if w.isdigit())
    if nums(q) != nums(k) and not same_year:     # "Scream" (1996) is not "Scream 2" (1997)
        return False
    return difflib.SequenceMatcher(None, q, k).ratio() >= 0.85


def pick(results, query, year, name_keys, date_key, loose):
    """Best search result for `query`: an exact title match (any of name_keys). With a
    year, results more than 1 year away are rejected, and (loose) a title that merely
    starts with the query is accepted as a second choice."""
    q = match_key(query)
    if not q:
        return None
    loose_hit = fuzzy_hit = None
    for r in (results or [])[:6]:
        if year:
            d = r.get(date_key) or ''
            if len(d) >= 4 and d[:4].isdigit() and abs(int(d[:4]) - year) > 1:
                continue
        keys = [match_key(r.get(k) or '') for k in name_keys]
        if q in keys:
            return r
        if (loose and year and loose_hit is None and
                any(len(k) >= 4 and (k.startswith(q + ' ') or q.startswith(k + ' ')) for k in keys)):
            loose_hit = r
        if loose and year and fuzzy_hit is None and any(_fuzzy(q, k, (r.get(date_key) or '')[:4] == str(year)) for k in keys):
            fuzzy_hit = r
    return loose_hit or fuzzy_hit


def movie_candidates(name, parents):
    """[(title, year)] guesses for one movie file: from the file name, then its folder."""
    cands = []
    for raw in (name, parents[-1] if parents else None):
        if not raw or NONSHOW_DIR.match(basic_clean(raw)):
            continue
        title, year = movie_title_year(raw)
        if len(normalize_name(title)) >= 2 and (title, year) not in cands:
            cands.append((title, year))
    return cands


EDITION_TAIL = re.compile(r"\s+(?:version longue|ultimate cut|uncut|dc|director'?s? cut|extended(?: cut| edition)?)$", re.I)
ROMAN_Q = {'ii': '2', 'iii': '3', 'iv': '4', 'v': '5', 'vi': '6', 'vii': '7', 'viii': '8', 'ix': '9'}


def title_variants(title):
    """Other ways to search the same film: without edition tags, either side of ' - ',
    Roman numerals as digits ('Police Academy III' -> 'Police Academy 3')."""
    base = [title]
    t2 = EDITION_TAIL.sub('', title).strip()
    if t2 and t2 not in base:
        base.append(t2)
    for t in list(base):
        for part in re.split(r'\s+-\s+', t)[1:] + re.split(r'\s+-\s+', t)[:1]:
            part = part.strip()
            if len(normalize_name(part)) >= 4 and part not in base:
                base.append(part)
    for t in list(base):                              # "Nausicaä" -> "Nausicaa", "Château" -> "Chateau"
        folded = unicodedata.normalize('NFKD', t).encode('ascii', 'ignore').decode()
        if folded and folded not in base:
            base.append(folded)
    out = []
    for t in base:
        for v in (t, " ".join([w if i == 0 else ROMAN_Q.get(w.lower(), w) for i, w in enumerate(t.split())])):
            if v and v not in out:
                out.append(v)
    return out


def franchise_base(title):
    """'Shrek 3' -> 'Shrek', 'Spy Kids 2 L Ile...' -> 'Spy Kids' (words before the first number)."""
    words = []
    for w in title.split():
        if w.isdigit() or w.lower() in ROMAN_Q or w.lower() == 'episode':
            break
        words.append(w)
    return " ".join(words)


def resolve_movie(item):
    name, parents = item
    for title, year in movie_candidates(name, parents):
        for tv in title_variants(title):
            hit = _search_movie(tv, year)
            if hit:
                return hit
        # last resort with a year: the franchise name + that exact year, if only ONE film fits
        # ("Shrek 3" (2007) is "Shrek the Third")
        base = franchise_base(title)
        bk = match_key(base)
        if year and len(bk) >= 4 and bk != match_key(title):
            for lang in ('en-US', 'fr-FR'):
                data = tmdb_get('search', 'search/movie', query=base, language=lang,
                                include_adult='false', year=year)
                fits = [r for r in (data or {}).get('results', [])
                        if (r.get('release_date') or '')[:4] == str(year) and
                        any((match_key(r.get(k) or '') + ' ').startswith(bk + ' ') for k in ('title', 'original_title'))]
                if len(fits) > 1:                # "Shrek the Third" vs the TV special "Shrek the Halls"
                    fits.sort(key=lambda r: -(r.get('popularity') or 0))
                    if (fits[0].get('popularity') or 0) < 3 * max(fits[1].get('popularity') or 0, 0.01):
                        fits = []
                if fits:
                    info = _movie_info(fits[0])
                    if info['collection']:       # a guess is only worth it when it gives a collection
                        return info
    return None


def _movie_info(hit):
    det = tmdb_get('movie', f"movie/{hit['id']}", language='en-US')
    return {'id': hit['id'], 'collection': (det or {}).get('collection')}


def _search_movie(title, year):
    attempts = [(year, year, True), (None, year, True), (None, None, False)] if year \
        else [(None, None, False)]
    # the most reliable attempt (title + year) in BOTH languages before any year-less one:
    # otherwise an unrelated film with the same title wins ("La Reine des neiges" vs Frozen)
    for param_year, verify_year, loose in attempts:
        for lang in ('en-US', 'fr-FR'):
            params = {'query': title, 'language': lang, 'include_adult': 'false'}
            if param_year:
                params['year'] = param_year
            data = tmdb_get('search', 'search/movie', **params)
            if not data:
                continue
            hit = pick(data['results'], title, verify_year,
                       ('title', 'original_title'), 'release_date', loose)
            if not hit and param_year and len(data['results']) == 1 and \
                    (data['results'][0].get('release_date') or '')[:4] == str(year):
                # TMDB found it through another title (Japanese, alternative...) and it is the
                # only film of that year: "Nausicaä de la Vallée du Vent" = "Warriors of the Wind"
                hit = data['results'][0]
            if hit:
                return _movie_info(hit)
    return None


def explain_movie(text):
    """What TMDB answers for one movie (menu option w) - to understand a wrong/missing match."""
    title, year = movie_title_year(text)
    print(f"Title searched: {title!r}   year: {year}")
    for tv in title_variants(title):
        for lang in ('en-US', 'fr-FR'):
            params = {'query': tv, 'language': lang, 'include_adult': 'false'}
            if year:
                params['year'] = year
            data = tmdb_get('search', 'search/movie', **params)
            res = (data or {}).get('results', [])[:5]
            print(f"  [{lang}] {tv!r}: " + ("; ".join(f"{r.get('title')} / {r.get('original_title')} ({(r.get('release_date') or '?')[:4]})"
                                                     for r in res) or "no result"))
    r = resolve_movie((text, ()))
    print("  => identified:", r if r else "NOT identified")


def resolve_show(item):
    k, d = item
    names = [d.get('name') or d['display']]
    for g in SHOW_ALIASES:
        if d.get('base', k) in [normalize_name(x) for x in g]:
            names += [x for x in g if x not in names]
    for nm in names:
        for lang in ('en-US', 'fr-FR'):
            data = tmdb_get('search', 'search/tv', query=nm, language=lang)
            hit = data and pick(data['results'], nm, d.get('year'), ('name', 'original_name'),
                                'first_air_date', False)
            if hit:
                return tmdb_get('tv', f"tv/{hit['id']}", language='en-US')
    # romaji / alternative titles ("Sousou no Frieren" is "Frieren: Beyond Journey's End")
    for nm in names[:2]:
        data = tmdb_get('search', 'search/tv', query=nm, language='en-US')
        for r in ((data or {}).get('results') or [])[:2]:
            fy = (r.get('first_air_date') or '')[:4]
            if d.get('year') and fy.isdigit() and abs(int(fy) - d['year']) > 1:
                continue
            alt = tmdb_get('alt', f"tv/{r['id']}/alternative_titles")
            keys = {match_key(t) for t in (alt or {}).get('titles', [])}
            if match_key(nm) in keys:
                return tmdb_get('tv', f"tv/{r['id']}", language='en-US')
    return None


def aired_counts(det):
    """{season: episodes already aired} from TMDB data (specials excluded)."""
    last = det.get('last') or {}
    ls, le = last.get('season_number'), last.get('episode_number')
    today = date.today().isoformat()
    counts = {}
    for s in det.get('seasons', []):
        n, c = s.get('season_number'), s.get('episode_count') or 0
        if not n:
            continue
        if ls is None:
            if s.get('air_date') and s['air_date'] <= today and c:
                counts[n] = c
        elif n < ls:
            counts[n] = c
        elif n == ls and le:
            counts[n] = min(c, le) if c else le       # anime: "last episode" is often absolute (S23E1180)
    return counts


# ==========================================
# ONLINE COMPLETENESS CHECK
# ==========================================
def libs():
    return [("Movies", MOVIE_PATH, 'movie'),
            ("TV Shows", TV_PATH, 'tv'),
            ("Anime", ANIME_PATH, 'anime')]


def get_scan(kind, path):
    """Entries + file paths from the latest scan (scans now if there is none)."""
    if kind not in SCANS:
        SCANS[kind] = scan_library(path)
    return SCANS[kind]


def show_year(name, parents):
    """Year that identifies a show ('Doctor Who (2005) - S01E04', folder 'Doctor Who 2005').
    Only a year ending the show title counts; release folders like 'Reacher S02 2024 MULTI'
    are ignored."""
    raw = basic_clean(name)
    for rx in EP_PATTERNS:
        m = rx.match(raw)
        if m:
            sm = re.search(r'\s(19\d\d|20[0-3]\d)$', cut_junk(m.group('show')).strip(' -:()[]'))
            if sm:
                return int(sm.group(1))
            break
    for p in parents:                                   # the show folder = first real folder
        c = basic_clean(p)
        if not c or SPECIAL_DIR.match(c) or SEASON_DIR.match(c) or NONSHOW_DIR.match(c):
            continue
        ym = re.search(r'^.+?\s[(\[]?(19\d\d|20[0-3]\d)[)\]]?$', c)
        return int(ym.group(1)) if ym else None
    return None


def collect_shows(entries):
    """{key: {'display', 'seasons', 'base', 'year'}}. Two different shows with the same title
    but different years (Doctor Who 1963 / 2005) are kept apart instead of being merged."""
    rows, years = [], defaultdict(set)
    for name, parents in entries:
        info = episode_info(name, parents)
        if not info:
            continue
        show, season, eps = info
        k = canon_show(normalize_name(show))
        y = show_year(name, parents)
        if y:
            years[k].add(y)
        rows.append((k, y, show, season, eps))
    shows = {}
    for k, y, show, season, eps in rows:
        split = y if (y and len(years[k]) >= 2) else None
        key = f"{k} {split}" if split else k
        d = shows.setdefault(key, {'display': f"{show} ({split})" if split else show,
                                   'seasons': defaultdict(set), 'base': k, 'year': split, 'name': show})
        d['seasons'][season].update(eps)
    skip = {normalize_name(x) for x in IGNORE_SHOWS}
    return {k: d for k, d in shows.items()
            if not ({normalize_name(d['name']), normalize_name(d['display']), d['base']} & skip)}


def check_movies(entries):
    results = run_parallel(resolve_movie, list(entries), "identifying movies")
    owned, unmatched = {}, []
    for (name, _p), r in zip(entries, results):
        if not r:
            unmatched.append(name)
            continue
        c = r['collection']
        if c:
            owned.setdefault(c['id'], {'name': c['name'], 'ids': set()})['ids'].add(r['id'])
    colls = list(owned.items())
    details = run_parallel(lambda cd: tmdb_get('collection', f"collection/{cd[0]}", language='en-US'),
                           colls, "movie collections")
    today = date.today().isoformat()
    missing_lines, upcoming_lines = [], []
    for (cid, d), det in zip(colls, details):
        if not det:
            continue
        parts = [p for p in det['parts'] if p.get('release_date')]
        released = [p for p in parts if p['release_date'] <= today]
        missing = [p for p in released if p['id'] not in d['ids']]
        upcoming = [p for p in parts if p['release_date'] > today and p['id'] not in d['ids']]
        nm = det.get('name') or d['name']
        if normalize_name(nm) in {normalize_name(x) for x in IGNORE_COLLECTIONS}:
            continue
        if missing:
            have = len([p for p in released if p['id'] in d['ids']])
            missing_lines.append((nm, have, len(released), [f"{p['title']} ({p['release_date'][:4]})"
                                  for p in sorted(missing, key=lambda p: p['release_date'])]))
        if upcoming:
            upcoming_lines.append((nm, [f"{p['title']} ({p['release_date']})"
                                        for p in sorted(upcoming, key=lambda p: p['release_date'])]))
    return sorted(missing_lines), sorted(upcoming_lines), unmatched, len(owned)


def check_shows(entries, anime):
    """-> (rows, unmatched). Each row is (title, status, [detail lines], missing_count) with status
    'exact' (episode numbers match TMDB) or 'approx' (anime numbered differently from TMDB)."""
    shows = collect_shows(entries)
    items = [(k, d) for k, d in sorted(shows.items())
             if any(s > 0 for s in d['seasons'])]
    dets = run_parallel(resolve_show, items, "identifying anime" if anime else "identifying shows")
    rows, unmatched = [], []
    for (k, d), det in zip(items, dets):
        if not det:
            unmatched.append(d['display'])
            continue
        counts = aired_counts(det)
        seasons = {s: eps for s, eps in d['seasons'].items() if s > 0}
        if not counts:
            continue
        top = max(seasons)
        details, missing_n, status = [], 0, 'exact'
        nxt = det.get('next') or {}
        later = (f"next episode: S{nxt['season_number']:02d}E{nxt['episode_number']:02d} on {nxt['air_date']}"
                 if nxt.get('air_date') and nxt.get('season_number') is not None and nxt.get('episode_number') is not None
                 else "")

        # Anime check: are the file numbers compatible with TMDB's seasons (same seasons, no number above the
        # season's episode count)? Then check episode by episode. Otherwise the numbering is absolute / split
        # differently and only the total can be compared.
        per_season = all(s in counts and max(eps) <= counts[s] for s, eps in seasons.items()) if anime else True
        if anime and not per_season:
            have = sum(len(eps) for eps in seasons.values())
            total = sum(counts.values())
            if have >= total:
                continue
            status, missing_n = 'approx', total - have
            highest = max(max(eps) for eps in seasons.values())
            details.append(f"you have {have} episodes (up to no. {highest}), TMDB lists {total} aired -> about {total - have} missing")
            details.append("numbering differs from TMDB: this is an estimate")
            if later:
                details.append(later)
            rows.append((d['display'], status, details, missing_n))
            continue

        missing_s = [s for s in counts if s not in seasons and s < top]
        new_s = [s for s in counts if s not in seasons and s > top]
        if missing_s:
            details.append(f"missing season(s) {compress(missing_s)}")
            missing_n += sum(counts[s] for s in missing_s)
        if new_s:
            details.append("newer season(s) out: " + ", ".join(f"S{s:02d} ({counts[s]} aired)" for s in sorted(new_s)))
            missing_n += sum(counts[s] for s in new_s)
        for s in sorted(seasons):
            if s not in counts:
                continue
            if anime:       # anime: every hole, not only the end of the season
                gone = [e for e in range(1, counts[s] + 1) if e not in seasons[s] and not is_ignored(k, s, e)]
                what = "missing ep"
            else:
                gone = [e for e in range(max(seasons[s]) + 1, counts[s] + 1) if not is_ignored(k, s, e)]
                what = "missing last ep"
            if gone:
                details.append(f"S{s:02d}: {what} {compress(gone)}")
                missing_n += len(gone)
        if details:
            if anime and later:
                details.append(later)
            rows.append((d['display'], status, details, missing_n))
    return rows, unmatched


def run_completeness():
    REPORT.clear()
    out("🌐 Online completeness check (TMDB)")
    if not _state['key']:
        out("⚠️ No TMDB key loaded - this check needs one (see tmdb_key.txt).")
        return
    cache_load()
    _state['errors'] = 0
    out("First run takes a few minutes (results are cached for next time).")
    for label, path, kind in libs():
        out(f"\n=== {label} ({show_path(path)}) ===")
        entries, _files = get_scan(kind, path)
        if entries is None:
            continue
        if kind == 'movie':
            missing, upcoming, unmatched, n_coll = check_movies(entries)
            out(f"{n_coll} movie collections recognised.")
            if missing:
                out(f"\n⚠️ Collections with films you don't have ({len(missing)}):")
                for nm, have, total, films in missing:
                    out(f"\n  {nm}  [{have}/{total}]")
                    for f in films:
                        out(f"      ✗ {f}")
            else:
                out("✅ No missing films in the collections you own.")
            if upcoming:
                out("\n📅 Not released yet:")
                for nm, films in upcoming:
                    out(f"\n  {nm}")
                    for f in films:
                        out(f"      • {f}")
        else:
            rows, unmatched = check_shows(entries, anime=(kind == 'anime'))
            exact = [r for r in rows if r[1] == 'exact']
            approx = [r for r in rows if r[1] == 'approx']
            if exact:
                out(f"\n⚠️ Missing episodes / seasons ({len(exact)}):")
                for title, _st, details, _n in exact:
                    out(f"\n  {title}")
                    for dline in details:
                        out(f"      ✗ {dline}")
            if approx:
                approx.sort(key=lambda r: -r[3])
                out(f"\n🟡 Probably behind - estimate only, the numbering differs from TMDB ({len(approx)}):")
                out("   (sorted by most episodes missing)")
                for title, _st, details, _n in approx:
                    out(f"\n  {title}")
                    for dline in details:
                        out(f"      ~ {dline}")
            if not rows:
                out("✅ Nothing missing compared with TMDB.")
        if unmatched:
            out(f"\n❓ {len(unmatched)} not identified online (unusual names?) - first ones:")
            for u in unmatched[:15]:
                out(f"   - {u}")
            for u in unmatched[15:400]:
                out(f"   - {u}", show=False)
        cache_save()
    if _state['auth_failed']:
        out("\n⚠️ TMDB refused the key (401) - check tmdb_key.txt.")
    elif _state['errors']:
        out(f"\n⚠️ {_state['errors']} online lookups failed (network?). Run again to retry them.")
    out("\nNote: TMDB sometimes splits or numbers seasons differently from your files; "
        "double-check before downloading. Add known quirks to IGNORE_MISSING / SHOW_ALIASES.")
    save_report("completeness_report.txt")


# ==========================================
# DUPLICATE FINDER
# ==========================================
def movie_dup_key(name):
    title, year = movie_title_year(name)
    return normalize_name(title), year


def quality_of(path):
    n = os.path.basename(path).lower()
    rank, label = 0, ""
    for tag, r in (('2160p', 4), ('4k', 4), ('uhd', 4), ('1080p', 3), ('1080i', 3),
                   ('720p', 2), ('576p', 1), ('480p', 1)):
        if re.search(rf'(?<![a-z0-9]){tag}(?![a-z0-9])', n) and r > rank:
            rank, label = r, tag
    codec = ("x265" if re.search(r'x265|h265|hevc', n) else
             "av1" if re.search(r'(?<![a-z0-9])av1(?![a-z0-9])', n) else
             "x264" if re.search(r'x264|h264|(?<![a-z0-9])avc(?![a-z0-9])', n) else "")
    return rank, " ".join(x for x in (label, codec) if x) or "?"


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def find_duplicates(entries, files, kind):
    """-> list of {'label', 'items': [{'path','size','rank','q'}], 'reclaim'} (best first)."""
    raw = []                                   # (label, [file indexes])
    if kind == 'movie':
        by = defaultdict(list)
        for i, (name, _p) in enumerate(entries):
            t, y = movie_dup_key(name)
            if t:
                by[(t, y)].append(i)
        years = defaultdict(set)
        for (t, y) in by:
            if y:
                years[t].add(y)
        merged = defaultdict(list)
        for (t, y), idx in by.items():
            if y is None and len(years[t]) == 1:    # file without year joins the only known year
                y = next(iter(years[t]))
            merged[(t, y)].extend(idx)
        for (t, y), idx in merged.items():
            if len(idx) > 1:
                nm = cut_junk(basic_clean(entries[idx[0]][0]))
                raw.append((f"{nm} ({y})" if y else nm, idx))
    else:
        cover, disp = defaultdict(set), {}
        for i, (name, parents) in enumerate(entries):
            info = episode_info(name, parents)
            if not info:
                continue
            show, season, eps = info
            k = canon_show(normalize_name(show))
            disp.setdefault(k, show)
            for e in eps:
                cover[(k, season, e)].add(i)
        sets = defaultdict(list)
        for (k, s, e), idx in cover.items():
            if len(idx) > 1:
                sets[(k, s, tuple(sorted(idx)))].append(e)
        for (k, s, idx), eps in sets.items():
            padded = re.sub(r'\d+', lambda m: m.group().zfill(2), compress(eps))
            raw.append((f"{disp[k]} S{s:02d}E{padded}", list(idx)))

    groups = []
    for label, idx in raw:
        items, ids = [], set()
        for i in idx:
            p = files[i]
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_ino:
                ids.add((st.st_dev, st.st_ino))
            rank, q = quality_of(p)
            items.append({'path': os.path.normpath(p), 'size': st.st_size, 'rank': rank, 'q': q})
        if len(items) < 2 or (ids and len(ids) == 1):      # hard links = same file
            continue
        items.sort(key=lambda it: (it['rank'], it['size']), reverse=True)
        groups.append({'label': label, 'items': items,
                       'reclaim': sum(it['size'] for it in items[1:])})
    groups.sort(key=lambda g: g['reclaim'], reverse=True)
    return groups


def run_duplicates():
    REPORT.clear()
    out("🔍 Duplicate finder - nothing is deleted or moved, this is only a list")
    grand = 0
    for label, path, kind in libs():
        out(f"\n=== {label} ({show_path(path)}) ===")
        entries, files = get_scan(kind, path)
        if not entries:
            continue
        groups = find_duplicates(entries, files, kind)
        if not groups:
            out("✅ No duplicates found.")
            continue
        total = sum(g['reclaim'] for g in groups)
        grand += total
        out(f"⚠️ {len(groups)} duplicated {'movies' if kind == 'movie' else 'episodes'}, "
            f"about {fmt_size(total)} could be freed.")
        out("   ★ = suggested keep (highest resolution, then biggest file)\n")
        for n, g in enumerate(groups):
            show = n < 25
            out(f"• {g['label']}  (frees {fmt_size(g['reclaim'])})", show=show)
            for j, it in enumerate(g['items']):
                out(f"   {'★' if j == 0 else ' '} [{it['q']}] {fmt_size(it['size'])}  {it['path']}", show=show)
        if len(groups) > 25:
            out(f"   ... {len(groups) - 25} more groups in the saved report.")
    out(f"\nTotal that could be freed: about {fmt_size(grand)}.")
    out("Check before deleting: different cuts (director's cut, extended) or languages are "
        "listed here too, and a file with no resolution tag in its name shows '?'.")
    save_report("duplicates_report.txt")


# ==========================================
# MAIN
# ==========================================
def save_report(filename="media_report.txt"):
    try:
        p = os.path.join(HERE, filename)
        with open(p, 'w', encoding='utf-8') as f:
            f.write("\n".join(REPORT))
        print(f"\n📝 Report saved to {p}")
    except Exception as e:
        print(f"(could not save report: {e})")


def run_scan(online, save=True):
    REPORT.clear()
    out("🚀 Scanning your libraries...")
    for label, path, kind in libs():
        out(f"\n=== {label} ({show_path(path)}) ===")
        entries, files = scan_library(path)
        if entries is None:
            SCANS.pop(kind, None)
            continue
        SCANS[kind] = (entries, files)
        out(f"{len(entries)} video files found.")

        gaps = movie_gaps(entries) if kind == 'movie' else episode_gaps(entries, anime=(kind == 'anime'))
        if gaps:
            out(f"\n⚠️ Possible holes ({len(gaps)}):")
            for g in gaps:
                out(f"   • {g}")
        else:
            out("✅ No holes detected.")

        if online.get(kind):
            recs = recommend(online[kind], local_titles(entries, kind != 'movie'), MAX_RECS)
            if recs:
                out(f"\n✨ Popular right now and not in your {label.lower()}:")
                for r in recs:
                    out(f"   🎬 {r}")
            else:
                out(f"\n🎉 You already have everything on the current {label.lower()} charts.")
        else:
            out("\n(No online recommendations - the online data could not be loaded; "
                "see the messages at the top of the window.)")
    out("\nNote: a 'missing part 1' can just be a different title (e.g. 'The Fast and the Furious').")
    if save:
        save_report()


def menu_text():
    return ("\n[Enter] rescan   [c] online completeness check (TMDB)   "
            "[d] find duplicates   [w] why is a movie not identified"
            + ("   [s] support this tool" if DONATE_URL.startswith("https://") else "") + "   [q] quit\n> ")


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # emoji on Windows consoles
    except Exception:
        pass
    if not ensure_paths():
        input("\nPress Enter to exit...")
        return
    key = load_tmdb_key()
    _state['key'] = key
    online = {
        'anime': fetch_airing_anime(),
        'movie': fetch_trending_tmdb("movie", key),
        'tv': fetch_trending_tmdb("tv", key),
    }
    while True:
        run_scan(online)
        while True:
            try:
                ans = input(menu_text()).strip().lower()
            except EOFError:
                return
            if ans in ('q', 'quit', 'exit'):
                return
            if ans == 'c':
                run_completeness()
            elif ans == 's' and DONATE_URL.startswith("https://"):
                import webbrowser
                print(f"Thank you! Opening {DONATE_URL}")
                webbrowser.open(DONATE_URL)
            elif ans == 'd':
                run_duplicates()
            elif ans == 'w':
                cache_load()
                try:
                    explain_movie(input("Movie file name or 'Title (Year)': ").strip())
                except EOFError:
                    return
            else:
                print()
                break


if __name__ == "__main__":
    main()
