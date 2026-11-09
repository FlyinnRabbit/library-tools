#!/usr/bin/env python3
"""
Rename helper for TV shows, anime and movies (companion of find_missing.py - keep
both files in the same folder, it reuses its name parsing, aliases and drive paths).

    python rename_media.py          preview, then asks before changing anything
    python rename_media.py --undo   revert the last batch

What it does
    Dark.S01E05.1080p.WEB.x264-GRP.mkv      ->  Dark - S01E05.mkv
    [Judas] Bleach - ... - S17E26v2.mkv     ->  Bleach - S17E26.mkv
    double episodes                          ->  Show - S03E01-E02.mkv
    files in a Specials / OVA folder         ->  Show - S00E01.mkv
    Scream.1996.1080p.BluRay.x264-GRP.mkv   ->  Scream (1996).mkv   (movies)
    subtitle files next to a video (.srt, .ass, .fr.srt ...) follow their video.

    Optionally (you are asked) it also moves everything into  Show/Season 01/ ,
    specials into  Show/Season 00/  - the layout Plex / Jellyfin / Kodi expect.

    Optionally (you are asked) it names movies and shows with their ENGLISH TMDB title:
    Le Roi Lion (1994).mkv  ->  The Lion King (1994).mkv      (needs your TMDB key, see find_missing.py)

Safety
    * Nothing changes until you type YES after the preview.
    * The full plan is saved to rename_plan.txt.
    * Files that would end up with the same name are NOT touched (listed as conflicts:
      they are usually duplicates - run find_missing.py, option d).
    * Every batch is logged in rename_log.json, --undo puts everything back.
    * Episode titles and quality tags (1080p, x265 ...) are dropped from the new
      names - undo restores them.
"""
import os
import re
import sys
import json
import time
import difflib
import urllib.error
import urllib.parse
from collections import Counter, defaultdict

import find_missing as fm

HERE = fm.app_dir()
PLAN_FILE = os.path.join(HERE, "rename_plan.txt")
LOG_FILE = os.path.join(HERE, "rename_log.json")

SIDECAR_EXTS = {'.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.sup', '.nfo'}
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ==========================================
# NAME HELPERS
# ==========================================
def safe(s):
    s = BAD_CHARS.sub(' ', s)
    return re.sub(r'\s+', ' ', s).strip(' .')


SMALL_WORDS = {'de', 'du', 'des', 'la', 'le', 'les', 'el', 'et', 'of', 'the', 'and',
               'a', 'an', 'in', 'on', 'at', 'to', 'von', 'van'}


def pretty(name):
    """ALL CAPS / all lowercase names -> Title Case ("Le Voyage de Chihiro");
    names with mixed case are kept as they are."""
    if not (name.isupper() or name.islower()):
        return name
    words = name.title().split(' ')
    return ' '.join(w.lower() if i and w.lower() in SMALL_WORDS else w
                    for i, w in enumerate(words))


SHOW_CUT = re.compile(r'\b(?:S\d{1,2}(?:\s*E\d+)?|S\s*\d{1,2}\s*-\s*\d{1,2}|seasons?\s*\d|saisons?\s*\d|'
                      r'complete|integrale|integral|intégrale|part\s*\d|\d{1,4}\s*~\s*\d{1,4})\b|'
                      r'\+\s*(?:ovas?|oavs?|specials?)\b', re.I)
# "24.Loki", "36 - Agatha All Along": collection order number in front of a show folder
SHOW_ORDER_PREFIX = re.compile(r'^\d{1,3}\s*(?:[.)]|\s-\s)\s*(?=[^\W\d_])(?!S\d{1,2}\b|Season)')


def clean_show_name(name):
    """Show name from a folder or file name: release tags, year, season markers and order
    numbers ('Bridgerton.S01.MULTi.1080p' -> 'Bridgerton', 'Downton Abbey iNTEGRALE' ->
    'Downton Abbey', '24.Loki' -> 'Loki')."""
    s = fm.cut_junk(fm.basic_clean(SHOW_ORDER_PREFIX.sub('', str(name), count=1)))
    m = next((m for m in SHOW_CUT.finditer(s) if m.start() > 0), None)
    if m:
        s = re.sub(r'\s+the$', '', s[:m.start()].strip(' -:+'), flags=re.I)   # "Game of Thrones The Complete"
    return s.strip(' -:+~')


clean_folder_name = clean_show_name


def is_structure_dir(name):
    c = fm.basic_clean(name)
    return bool(fm.SPECIAL_DIR.match(c) or fm.SEASON_DIR.match(c) or fm.NONSHOW_DIR.match(c))


def episode_tag(season, eps):
    tag = f"S{season:02d}E{eps[0]:02d}"
    if len(eps) > 1:
        tag += f"-E{eps[-1]:02d}"
    return tag


def folder_matches(folder, parsed_names):
    """True when the show folder really is that show (so its name can be trusted)."""
    f = fm.normalize_name(clean_folder_name(folder))
    if not f:
        return False
    for n in parsed_names:
        n = fm.normalize_name(n)
        if f in n or n in f or fm.canon_show(f) == fm.canon_show(n):
            continue
        return False
    return True


# ==========================================
# ENGLISH TITLES (optional)
# ==========================================
EN_CACHE_FILE = os.path.join(HERE, "english_titles_cache.json")
_en_cache = None


def _en_cache_get():
    global _en_cache
    if _en_cache is None:
        try:
            with open(EN_CACHE_FILE, encoding='utf-8') as f:
                _en_cache = json.load(f)
        except (OSError, ValueError):
            _en_cache = {}
    return _en_cache


def en_cache_save():
    if _en_cache:
        try:
            with open(EN_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(_en_cache, f, ensure_ascii=False, indent=0)
        except OSError:
            pass


def _tmdb_raw(path, **params):
    """Plain TMDB GET (uses the key loaded by find_missing). None on any problem."""
    key = fm._state.get('key')
    if not key or fm._state.get('auth_failed'):
        return None
    headers, q = {}, urllib.parse.urlencode(params)
    if key.startswith('eyJ'):
        headers['Authorization'] = f'Bearer {key}'
    else:
        q += f"&api_key={key}"
    try:
        return fm.http_json(f"https://api.themoviedb.org/3/{path}?{q}", headers=headers)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            fm._state['auth_failed'] = True
        return None
    except Exception:
        return None


def en_title(t):
    """File-name safe version of an English title: 'Star Wars: A New Hope' -> 'Star Wars - A New Hope'."""
    return safe(re.sub(r'\s*:\s+', ' - ', t))


def english_movie(name):
    """'Le Roi Lion (1994)' -> 'The Lion King (1994)'. None = keep the current name."""
    cache = _en_cache_get()
    ck = 'm|' + name
    if ck in cache:
        return cache[ck]
    res = None
    m = re.match(r'^(.*?)\s*\(((?:19|20)\d\d)\)$', name)
    year = int(m.group(2)) if m else None
    hit = fm.resolve_movie((name, ()))
    det = _tmdb_raw(f"movie/{hit['id']}", language='en-US') if hit else None
    if det and det.get('title'):
        ty = int((det.get('release_date') or '0000')[:4] or 0)
        if not year or not ty or abs(ty - year) <= 1:          # a different year = probably a wrong match
            new = en_title(det['title']) + (f" ({year or ty})" if (year or ty) else '')
            if fm.match_key(new) != fm.match_key(name):
                res = new
    cache[ck] = res
    return res


def english_show(display):
    """'Les Simpsons' -> 'The Simpsons'. None = keep the current name."""
    cache = _en_cache_get()
    ck = 's|' + display
    if ck in cache:
        return cache[ck]
    res = None
    k = fm.normalize_name(display)
    det = fm.resolve_show((k, {'display': display, 'name': display, 'base': k, 'year': None}))
    if det and det.get('name'):
        new = en_title(det['name'])
        if new and fm.match_key(new) != fm.match_key(display):
            res = new
    cache[ck] = res
    return res


def english_map(fn, names, label):
    """{name: english name} for the names that change (parallel + cached on disk)."""
    names = sorted(set(names))
    res = fm.run_parallel(fn, names, label, workers=8)
    fm.cache_save()
    en_cache_save()
    return {n: r for n, r in zip(names, res) if r}


# ==========================================
# BUILD THE PLAN
# ==========================================
def collect(root, episodic=True):
    items = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in fm.SKIP_DIRS and not d.startswith('$')]
        rel = os.path.relpath(dirpath, root)
        parents = () if rel == '.' else tuple(rel.replace('\\', '/').split('/'))
        for f in files:
            stem, ext = os.path.splitext(f)
            if ext.lower() in fm.VIDEO_EXTENSIONS and 'sample' not in stem.lower():
                items.append({'dir': dirpath, 'file': f, 'stem': stem, 'ext': ext,
                              'parents': parents,
                              'info': fm.episode_info(stem, parents) if episodic else None})
    return items


def build_plan(root, organize=False, english=False):
    """-> dict(ops=[...], unchanged=n, unparsed=[names], conflicts=[...], total=n)

    Show name: the TOP-MOST folder under `root` that is a show folder ("Reacher",
    "Bridgerton.S01.MULTi..." -> "Bridgerton"), so release / season / episode sub-folders
    never leak into the name. A folder is rejected only when the file names inside it
    clearly belong to several other shows (a category folder); then the name written in
    the file names is used."""
    items = collect(root)
    unparsed = []
    parsed_items = []
    for it in items:
        if not it['info']:
            unparsed.append(os.path.join(it['dir'], it['file']))
            continue
        it['clean'] = clean_show_name(it['info'][0]) or it['info'][0]
        parent_names = {fm.normalize_name(clean_show_name(p)) for p in it['parents']}
        # a file whose "show" is only a copy of one of its folder names is no evidence
        it['evidence'] = fm.normalize_name(it['clean']) not in parent_names
        parsed_items.append(it)

    by_prefix = defaultdict(list)
    for it in parsed_items:
        for d in range(1, len(it['parents']) + 1):
            by_prefix[it['parents'][:d]].append(it)
    memo = {}

    def folder_ok(prefix):
        if prefix in memo:
            return memo[prefix]
        f = fm.normalize_name(clean_show_name(prefix[-1]))
        res = bool(f) and not is_structure_dir(prefix[-1])
        if res:
            names = [fm.normalize_name(g['clean']) for g in by_prefix[prefix] if g['evidence']]
            if names:
                match = sum(1 for n in names
                            if n and (f in n or n in f or fm.canon_show(f) == fm.canon_show(n)))
                distinct = len({fm.canon_show(n) for n in names})
                res = match / len(names) >= 0.5 or distinct <= 2
        if res:
            # a category folder ("Apocalypse" holding several different series): no file name is
            # the folder name itself -> its sub-folders are the real shows
            grp = by_prefix[prefix]
            allnames = {fm.normalize_name(g['clean']) for g in grp}
            allnames.discard('')
            # ...and the files are named like their own sub-folder (not "Food Wars! [S01]-[S05]",
            # whose files carry different season titles but are ONE show)
            own_folder = sum(1 for g in grp if not g['evidence']) >= 0.8 * len(grp)
            plain = fm.normalize_name(prefix[-1]) == f       # no release/season tags: a bare category name
            if plain and own_folder and len({fm.canon_show(n) for n in allnames}) >= 3 and not any(
                    n == f or fm.canon_show(n) == fm.canon_show(f) for n in allnames):
                res = False
        memo[prefix] = res
        return res

    # most common spelling of each show over the whole library (for files outside a show folder)
    spelling = defaultdict(Counter)
    for it in parsed_items:
        spelling[fm.canon_show(fm.normalize_name(it['clean']))][it['clean']] += 1

    def folder_display(prefix):
        name = clean_show_name(prefix[-1])
        if name.isupper() or name.islower():          # BROOKLYN.NINE.NINE.INTEGRALE...
            f = fm.normalize_name(name)
            c = Counter(g['clean'] for g in by_prefix[prefix]
                        if fm.normalize_name(g['clean']) == f
                        and not (g['clean'].isupper() or g['clean'].islower()))
            if c:                                     # the file names spell it better: Nine-Nine
                name = c.most_common(1)[0][0]
        return safe(pretty(name))

    for it in parsed_items:
        chosen = next((it['parents'][:d] for d in range(1, len(it['parents']) + 1)
                       if folder_ok(it['parents'][:d])), None)
        if chosen:
            it['display'] = folder_display(chosen)
            it['base'] = os.path.join(root, *chosen)
        else:
            k = fm.canon_show(fm.normalize_name(it['clean']))
            it['display'] = safe(pretty(spelling[k].most_common(1)[0][0]))
            it['base'] = os.path.join(it['dir'], it['display']) if organize else None
    emap = {}
    if english:
        emap = english_map(english_show, [it['display'] for it in parsed_items if it.get('display')],
                           "English show titles")
        for it in parsed_items:
            if it.get('display') in emap:
                it['display'] = emap[it['display']]
    ops, unchanged, listing = [], 0, {}
    todo = []
    for it in items:
        if not it['info'] or not it.get('display'):
            continue
        _show, season, eps = it['info']
        new_stem = f"{it['display']} - {episode_tag(season, eps)}"
        if organize and it['base']:
            dst_dir = os.path.join(it['base'], f"Season {season:02d}")
        else:
            dst_dir = it['dir']
        todo.append([it, dst_dir, new_stem])
    mark_parts(todo)
    for it, dst_dir, new_stem in todo:
        op = make_op(it, dst_dir, new_stem, listing)
        if op:
            ops.append(op)
        else:
            unchanged += 1
    plan = finalize(ops, unchanged, unparsed, len(items))
    plan['english'] = emap
    return plan


# "Partie 1 / Partie 2", "1re partie", "S02E06.1 / S02E06.2", "S01E05.5": several files for ONE
# episode number are parts of that episode -> "Show - S02E06 - pt1", "... - pt2" (Plex/Jellyfin
# read this as a multi-part episode). Only when a file name says it is a part.
PART_RX = re.compile(r"(?:\bpart(?:ie)?\.?[ ._-]*(\d)|\bpt(\d)(?!\d)|(\d)(?:re|ere|e|eme|\u00e8me)?[ ._-]*partie"
                     r"|(premi[e\u00e8]re|1ere)[ ._-]+partie|(deuxi[e\u00e8]me|2eme)[ ._-]+partie"
                     r"|\bs\d{1,2}e\d{1,3}\.(\d)(?!\d))", re.I)


def part_number(stem):
    """1, 2, 5... when the file name marks a part, else 0."""
    m = PART_RX.search(stem)
    if not m:
        return 0
    g = m.groups()          # 1 part N | 2 ptN | 3 "N(re) partie" | 4 premiere | 5 deuxieme | 6 .N
    if g[3]:
        return 1
    if g[4]:
        return 2
    return int(g[0] or g[1] or g[2] or g[5])


def mark_parts(todo):
    """todo = [[item, dst_dir, new_stem]]: add ' - ptN' where parts would share one name."""
    groups = defaultdict(list)
    for row in todo:
        groups[(os.path.normcase(row[1]), row[2].lower())].append(row)
    for rows in groups.values():
        if len(rows) < 2 or len(rows) > 4:
            continue
        nums = [part_number(r[0]['stem']) for r in rows]
        if not any(nums):                       # plain duplicates, not parts: leave to conflicts
            continue
        for n, (rank, row) in enumerate(sorted(enumerate(rows), key=lambda x: (nums[x[0]], x[1][0]['file'])), 1):
            row[2] = f"{row[2]} - pt{n}"


def make_op(it, dst_dir, new_stem, listing):
    """One video (+ the subtitle files next to it) -> its new name. None if already named so."""
    src = os.path.join(it['dir'], it['file'])
    dst = os.path.join(dst_dir, new_stem + it['ext'].lower())
    if os.path.normcase(src) == os.path.normcase(dst):
        return None
    moves = [(src, dst)]
    if it['dir'] not in listing:
        try:
            listing[it['dir']] = os.listdir(it['dir'])
        except OSError:
            listing[it['dir']] = []
    for other in listing[it['dir']]:
        o_ext = os.path.splitext(other)[1].lower()
        if other.startswith(it['stem'] + '.') and o_ext in SIDECAR_EXTS and other != it['file']:
            suffix = other[len(it['stem']):]                  # ".fr.srt"
            moves.append((os.path.join(it['dir'], other), os.path.join(dst_dir, new_stem + suffix)))
    return {'moves': moves}


def finalize(ops, unchanged, unparsed, total):
    """Split the operations into safe ones and conflicts."""
    targets = defaultdict(list)
    for op in ops:
        targets[os.path.normcase(op['moves'][0][1])].append(op)
    sources = {os.path.normcase(m[0]) for op in ops for m in op['moves']}
    conflicts, good = [], []
    for op in ops:
        dst = os.path.normcase(op['moves'][0][1])
        if len(targets[dst]) > 1:
            conflicts.append((op['moves'][0][0], op['moves'][0][1], "another file gets the same name"))
        elif os.path.exists(op['moves'][0][1]) and dst not in sources:
            conflicts.append((op['moves'][0][0], op['moves'][0][1], "target already exists"))
        else:
            good.append(op)
    return {'ops': good, 'unchanged': unchanged, 'unparsed': unparsed,
            'conflicts': conflicts, 'total': total}


# ---- movies: "Title (Year).ext" -------------------------------------------
GENERIC_MOVIE = {'movie', 'film', 'video', 'title', 'sample'}


def movie_name(it):
    """'Scream (1996)' from the file name (or its folder when the file name says nothing)."""
    if re.search(r'(?<![A-Za-z0-9])(?:S\d{1,2}[ ._-]?E\d{1,3}|E\d{2,3})(?![A-Za-z0-9])',
                 it['stem'], re.I):                 # an episode of a series, not a movie
        return None
    cands = [c for c in fm.movie_candidates(it['stem'], it['parents'])
             if fm.normalize_name(c[0]) not in GENERIC_MOVIE]
    if not cands:
        return None
    title, year = cands[0]
    if year is None:
        k = fm.match_key(title)
        for t, y in cands[1:]:
            if y and fm.match_key(t) == k:
                year = y
                break
    name = re.sub(r'\bVol (\d)', r'Vol. \1', safe(pretty(title)))     # "Vol. 3"
    if not name:
        return None
    return f"{name} ({year})" if year else name


# Years you impose for titles without a year in their file name. They win over TMDB
# (useful when several films share a title: Hercules, Tarzan, Pinocchio...).
# Edit freely: "Title as it appears in the plan": year
YEAR_OVERRIDES = {
    # "Title as it appears in the plan": 1999,
}
YEAR_OVERRIDES.update(fm.load_config().get('year_overrides', {}))
YEAR_OVERRIDES_KEYED = {fm.match_key(k): v for k, v in YEAR_OVERRIDES.items()}


def _query_variants(name):
    """Search strings for a year-less title: as is, before ' - ', without a trailing number."""
    out = []
    nonum = re.sub(r'\s+\d{1,2}(?=\s+-\s+)', '', name)       # "Mission Impossible 4 - Protocole Fantome"
    for v in (name, nonum, re.split(r'\s+-\s+', name)[0]):
        v = v.strip()
        for w in (v, re.sub(r'\s+1$', '', v)):
            if w and w not in out:
                out.append(w)
    return out


def _year_of(r):
    d = r.get('release_date') or ''
    return int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None


def lookup_year(name):
    """TMDB release year for a movie title that has none.
    Returns {'year': int|None, 'candidates': [years]}; year is None when ambiguous/unknown."""
    found = {}
    for q in _query_variants(name):
        qk = fm.match_key(q)
        for lang in ('fr-FR', 'en-US'):
            data = fm.tmdb_get('search', 'search/movie', query=q, language=lang, include_adult='false')
            for r in (data or {}).get('results', [])[:8]:
                if not _year_of(r):
                    continue
                keys = [fm.match_key(r.get(k) or '') for k in ('title', 'original_title')]
                ok = qk in keys or (len(qk) >= 8 and any(
                    k and difflib.SequenceMatcher(None, qk, k).ratio() >= 0.9 for k in keys))
                if ok:
                    found[r['id']] = r
    if not found:
        return {'year': None, 'candidates': []}
    by_year = defaultdict(float)
    for r in found.values():
        by_year[_year_of(r)] = max(by_year[_year_of(r)], r.get('popularity') or 0)
    ranked = sorted(by_year.items(), key=lambda x: -x[1])
    cands = [y for y, _p in ranked]
    if len(ranked) == 1:
        return {'year': ranked[0][0], 'candidates': cands}
    if ranked[0][1] >= 3 * max(ranked[1][1], 0.01):
        return {'year': ranked[0][0], 'candidates': cands}
    return {'year': None, 'candidates': cands}


def lookup_years(names):
    """{name: lookup_year(name)} for all names, in parallel and cached on disk."""
    names = sorted(set(names))
    res = fm.run_parallel(lookup_year, names, "Looking up years", workers=8)
    fm.cache_save()
    return {n: (r or {'year': None, 'candidates': []}) for n, r in zip(names, res)}


def build_movie_plan(root, organize=False, years=None, english=False):
    items = collect(root, episodic=False)
    unparsed, named = [], []
    guessed, unresolved = {}, {}
    for it in items:
        it['name'] = movie_name(it)
        if it['name'] and not re.search(r'\((?:19|20)\d\d\)$', it['name']):
            info = years.get(it['name']) if years else None
            forced = YEAR_OVERRIDES_KEYED.get(fm.match_key(it['name']))
            if forced:                                   # your own list wins over TMDB
                guessed[it['name']] = forced
                it['name'] = f"{it['name']} ({forced})"
            elif info and info['year']:
                guessed[it['name']] = info['year']
                it['name'] = f"{it['name']} ({info['year']})"
            elif info is not None:
                unresolved[it['name']] = info['candidates']
        (named if it['name'] else unparsed).append(it)
    unparsed = [os.path.join(i['dir'], i['file']) for i in unparsed]
    emap = {}
    if english:
        emap = english_map(english_movie, [it['name'] for it in named], "English movie titles")
        for it in named:
            it['name'] = emap.get(it['name'], it['name'])
    names_in_dir = defaultdict(set)
    for it in named:
        names_in_dir[it['dir']].add(it['name'])
    ops, unchanged, listing = [], 0, {}
    for it in named:
        dst_dir = it['dir']
        # organize: a movie sharing its folder with other movies (or sitting in the library
        # root) gets its own "Title (Year)" folder; a folder holding only this movie is kept
        if organize and (os.path.normcase(it['dir']) == os.path.normcase(root)
                         or len(names_in_dir[it['dir']]) > 1):
            dst_dir = os.path.join(it['dir'], it['name'])
        op = make_op(it, dst_dir, it['name'], listing)
        if op:
            ops.append(op)
        else:
            unchanged += 1
    plan = finalize(ops, unchanged, unparsed, len(items))
    plan['guessed'], plan['unresolved'] = guessed, unresolved
    plan['english'] = emap
    return plan


# ==========================================
# APPLY / UNDO
# ==========================================
def load_log():
    try:
        with open(LOG_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_log(batches):
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(batches, f, ensure_ascii=False, indent=1)


def missing_dirs(d):
    chain = []
    while d and not os.path.isdir(d):
        chain.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return list(reversed(chain))


def apply_plan(ops, keep=()):
    batch = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'moves': [], 'created_dirs': []}
    failed = []
    try:
        for op in ops:
            for src, dst in op['moves']:
                try:
                    if os.path.exists(dst):
                        raise FileExistsError("target exists")
                    for d in missing_dirs(os.path.dirname(dst)):
                        os.mkdir(d)
                        batch['created_dirs'].append(d)
                    for attempt in range(3):             # a file briefly locked by a scanner/antivirus
                        try:
                            os.rename(src, dst)
                            break
                        except PermissionError:
                            if attempt == 2:
                                raise
                            time.sleep(1.5)
                    batch['moves'].append([src, dst])
                except OSError as e:
                    failed.append((src, str(e)))
    finally:
        if batch['moves']:
            batches = load_log()
            batches.append(batch)
            save_log(batches)
    # tidy: remove folders the moves left empty (rmdir only removes EMPTY folders)
    keep = {os.path.normcase(os.path.abspath(k)) for k in keep}
    for d in sorted({os.path.dirname(m[0]) for m in batch['moves']}, key=len, reverse=True):
        if os.path.normcase(os.path.abspath(d)) in keep:
            continue
        try:
            os.rmdir(d)
        except OSError:
            pass
    return batch, failed


def undo_last():
    batches = load_log()
    if not batches:
        print("Nothing to undo (no rename_log.json or it is empty).")
        return
    batch = batches[-1]
    print(f"Undoing the batch of {batch['time']} ({len(batch['moves'])} files)...")
    ok = bad = 0
    for src, dst in reversed(batch['moves']):
        try:
            if os.path.exists(dst) and not os.path.exists(src):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                os.rename(dst, src)
                ok += 1
            else:
                bad += 1
                print(f"   skipped (already moved or original name taken): {dst}")
        except OSError as e:
            bad += 1
            print(f"   failed: {dst}: {e}")
    for d in reversed(batch['created_dirs']):
        try:
            os.rmdir(d)
        except OSError:
            pass
    if bad == 0:
        batches.pop()
        save_log(batches)
    print(f"Restored {ok} files" + (f", {bad} problem(s) - the batch stays in the log." if bad else "."))


# ==========================================
# PREVIEW / MAIN
# ==========================================
def describe(plan, label, root, lines, console_limit=25):
    lines.append(f"\n=== {label} ({root}) ===")
    lines.append(f"{plan['total']} video files: {len(plan['ops'])} to rename, "
                 f"{plan['unchanged']} already fine, {len(plan['conflicts'])} conflicts, "
                 f"{len(plan['unparsed'])} not recognised.")
    for op in plan['ops']:
        src, dst = op['moves'][0]
        lines.append(f"  {os.path.relpath(src, root)}\n    -> {os.path.relpath(dst, root)}")
        for s, d in op['moves'][1:]:
            lines.append(f"    (+ {os.path.basename(s)} -> {os.path.basename(d)})")
    if plan.get('english'):
        lines.append(f"\nENGLISH TITLES USED ({len(plan['english'])}):")
        for a, b in sorted(plan['english'].items()):
            lines.append(f"  {a}  ->  {b}")
    if plan.get('guessed'):
        lines.append(f"\nYEARS ADDED (TMDB or YEAR_OVERRIDES) ({len(plan['guessed'])} titles):")
        for n, y in sorted(plan['guessed'].items()):
            lines.append(f"  {n} -> {y}")
    if plan.get('unresolved'):
        lines.append(f"\nNO YEAR ADDED ({len(plan['unresolved'])} titles, not found or ambiguous - rename them by hand if you want):")
        for n, c in sorted(plan['unresolved'].items()):
            lines.append(f"  {n}" + (f"   (candidate years: {', '.join(map(str, c))})" if c else "   (no match)"))
    if plan['conflicts']:
        lines.append("\nCONFLICTS (left untouched):")
        for src, dst, why in plan['conflicts']:
            lines.append(f"  {src}\n    would become {dst}  [{why}]")
    if plan['unparsed']:
        lines.append("\nNOT RECOGNISED (left untouched):")
        for p in plan['unparsed']:
            lines.append(f"  {p}")


def preview_text(plans, organize):
    lines = ["RENAME PLAN" + (" (with Season folders)" if organize else "")]
    for label, root, plan in plans:
        describe(plan, label, root, lines)
    return lines


def print_summary(plans):
    total = 0
    for label, root, plan in plans:
        total += len(plan['ops'])
        print(f"\n=== {label} ({root}) ===")
        print(f"{plan['total']} video files: {len(plan['ops'])} to rename, {plan['unchanged']} already fine, "
              f"{len(plan['conflicts'])} conflicts, {len(plan['unparsed'])} not recognised.")
        for op in plan['ops'][:15]:
            src, dst = op['moves'][0]
            print(f"  {os.path.relpath(src, root)}\n    -> {os.path.relpath(dst, root)}")
        if len(plan['ops']) > 15:
            print(f"  ... and {len(plan['ops']) - 15} more (all in rename_plan.txt)")
        if plan.get('english'):
            print(f"  🇬🇧 English titles used: {len(plan['english'])} (listed in rename_plan.txt)")
        for src, dst, why in plan['conflicts'][:5]:
            print(f"  ⚠️ conflict ({why}): {os.path.basename(src)}")
        if plan.get('guessed') is not None and (plan['guessed'] or plan.get('unresolved')):
            print(f"  📅 years added from TMDB: {len(plan['guessed'])}, "
                  f"left without year: {len(plan.get('unresolved', {}))} (details in rename_plan.txt)")
    return total


def fix_season(folder, season):
    """--season "folder" N : every 'S01E05' in the file names of that folder becomes 'S0NE05'
    (for a season that was numbered wrongly). Preview, then YES; undo with --undo."""
    ops, listing = [], {}
    for f in sorted(os.listdir(folder)):
        full = os.path.join(folder, f)
        m = re.search(r'\bS(\d{1,2})E(\d{1,3}(?:-E\d{1,3})?)', f)
        if not os.path.isfile(full) or not m or int(m.group(1)) == season:
            continue
        new = f[:m.start()] + f"S{season:02d}E{m.group(2)}" + f[m.end():]
        ops.append({'moves': [(full, os.path.join(folder, new))]})
    clash = [o for o in ops if os.path.exists(o['moves'][0][1])]
    if clash:
        print(f"⚠️ {len(clash)} target names already exist (e.g. {os.path.basename(clash[0]['moves'][0][1])}) - nothing changed.")
        return
    if not ops:
        print("Nothing to change in that folder.")
        return
    for o in ops[:8]:
        print(f"  {os.path.basename(o['moves'][0][0])}\n    -> {os.path.basename(o['moves'][0][1])}")
    if len(ops) > 8:
        print(f"  ... and {len(ops) - 8} more")
    if input(f"\nType YES to rename {len(ops)} files to season {season}: ").strip() != "YES":
        print("Cancelled - nothing was changed.")
        return
    batch, bad = apply_plan(ops, keep=[folder])
    print(f"✅ {len(batch['moves'])} files renamed" + (f", {len(bad)} failed" if bad else "") +
          ".  To revert: python rename_media.py --undo")


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if '--season' in sys.argv:
        try:
            i = sys.argv.index('--season')
            fix_season(sys.argv[i + 1], int(sys.argv[i + 2]))
        except (IndexError, ValueError, OSError) as e:
            print(f'Usage: python rename_media.py --season "D:/folder" 2   ({e})')
        input("\nPress Enter to exit...")
        return
    if '--undo' in sys.argv:
        undo_last()
        input("\nPress Enter to exit...")
        return
    if not fm.ensure_paths():
        input("\nPress Enter to exit...")
        return
    print("Which libraries do you want to rename?\n"
          "  1 = TV shows   2 = Anime   3 = Movies   (e.g. 13 = TV + movies, Enter = all)")
    choice = input("> ").strip() or "123"
    libs = []
    if '1' in choice:
        libs.append(("TV Shows", fm.TV_PATH, build_plan))
    if '2' in choice:
        libs.append(("Anime", fm.ANIME_PATH, build_plan))
    if '3' in choice:
        libs.append(("Movies", fm.MOVIE_PATH, build_movie_plan))
    ans = input("\nAlso organise into folders? (shows -> 'Show/Season 01/', specials -> 'Season 00',\n"
                "movies loose in a folder -> 'Title (Year)/')  [y/N] ")
    organize = ans.strip().lower() in ('y', 'yes', 'o', 'oui')

    a = input("\nName movies and shows with their ENGLISH title (from TMDB)? [y/N] ")
    english = a.strip().lower() in ('y', 'yes', 'o', 'oui')

    use_years = False
    if '3' in choice:
        a = input("\nMovies without a year in their name: look the year up on TMDB and add it? [Y/n] ")
        use_years = a.strip().lower() not in ('n', 'no', 'non')
    if use_years or english:
        key = fm.load_tmdb_key()
        if key:
            fm._state['key'] = key
            fm.cache_load()
        else:
            print("⚠️ No TMDB key found (tmdb_key.txt next to the script) - no years and no English titles.")
            use_years = english = False

    plans = []
    for label, paths, builder in libs:
        for root in fm.as_list(paths):          # a library can be several root folders
            if not os.path.isdir(root):
                print(f"⚠️ {root} not found - skipped.")
                continue
            print(f"Reading {root} ...")
            if builder is build_movie_plan and use_years:
                noyear = [n for n in (movie_name(i) for i in collect(root, episodic=False))
                          if n and not re.search(r'\((?:19|20)\d\d\)$', n)
                          and fm.match_key(n) not in YEAR_OVERRIDES_KEYED]
                years = lookup_years(noyear)
                if fm._state['auth_failed']:
                    print("⚠️ TMDB rejected the key - no years added.")
                    years = None
                plans.append((label, root, build_movie_plan(root, organize, years, english)))
            else:
                plans.append((label, root, builder(root, organize, english=english) if builder is build_plan
                              else builder(root, organize, english=english)))
    if not plans:
        print("No library folder found. Set MOVIE_PATH / TV_PATH / ANIME_PATH at the top of find_missing.py.")
        input("\nPress Enter to exit...")
        return
    total = print_summary(plans)
    with open(PLAN_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(preview_text(plans, organize)))
    print(f"\n📝 Full plan saved to {PLAN_FILE}")
    if not total:
        print("Nothing to rename.")
        return
    if input(f"\nType YES to rename {total} files (anything else cancels): ").strip() != "YES":
        print("Cancelled - nothing was changed.")
        return
    all_ops = [op for _l, _r, plan in plans for op in plan['ops']]
    batch, bad = apply_plan(all_ops, keep=[r for _l, r, _p in plans])
    done, failed = len(batch['moves']), len(bad)
    for src, why in bad[:10]:
        print(f"   ⚠️ {src}: {why}")
    print(f"\n✅ {done} files renamed" + (f", {failed} failed" if failed else "") +
          ".  To revert: python rename_media.py --undo")
    if failed:
        print("\n🔒 Files that failed are usually IN USE by another program: close your torrent client\n"
              "   (qBittorrent... still seeding them), Plex/Jellyfin/Kodi, a video player or Explorer's\n"
              "   preview pane, then run this script again - it only picks up what is left.")
    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
