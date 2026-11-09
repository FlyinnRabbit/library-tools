#!/usr/bin/env python3
"""
library_gallery.py - turns your 3 libraries into a browsable poster gallery (one HTML page).

    python library_gallery.py            -> builds gallery/index.html and opens it

Needs find_missing.py in the same folder (same folders, same title matching, same TMDB cache)
and the TMDB key (TMDB_API_KEY variable or tmdb_key.txt, exactly like find_missing.py).
Posters are downloaded once into gallery/posters, so the gallery works offline afterwards.
Nothing is renamed or modified in your libraries: it only reads them.

Options:
    --no-open     do not open the page in the browser when done
    --offline     no TMDB at all: titles come from the file names, no posters
"""
import json
import os
import sys
import time
import urllib.error
import webbrowser
from collections import defaultdict
from datetime import datetime

import find_missing as fm

# ==========================================
# CONFIGURATION
# ==========================================
BOT_PREFIX = "/"                  # "/" = slash command (/play query:<title>); "$" = the self-bot text prefix
# Where the gallery is written. Put the bot's gallery folder here so nothing has to be copied, e.g.
# BOT_GALLERY_DIR = r"C:\path\to\your-bot\gallery"
BOT_GALLERY_DIR = ""
OUT_DIR = BOT_GALLERY_DIR or os.environ.get("GALLERY_DIR") or os.path.join(fm.HERE, "gallery")
POSTER_DIR = os.path.join(OUT_DIR, "posters")
DETAIL_CACHE_FILE = os.path.join(fm.HERE, "gallery_cache.json")
DETAIL_TTL = 14 * 86400
POSTER_SIZE = "w342"
LANG = "en-US"                    # language of titles / synopsis / genres in the gallery
ALT_LANG = "fr-FR"                # other title, kept only so French searches still work

_dcache = {}
_dkey = {'key': ''}


# ==========================================
# TMDB DETAILS (title, poster, overview...) - cached
# ==========================================
def dcache_load():
    try:
        with open(DETAIL_CACHE_FILE, encoding='utf-8') as f:
            _dcache.update(json.load(f))
    except (OSError, ValueError):
        pass


def dcache_save():
    try:
        tmp = DETAIL_CACHE_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_dcache, f, ensure_ascii=False)
        os.replace(tmp, DETAIL_CACHE_FILE)
    except OSError as e:
        print(f"(could not save {DETAIL_CACHE_FILE}: {e})")


def _api(path, lang):
    key = _dkey['key']
    url = f"https://api.themoviedb.org/3/{path}?language={lang}"
    headers = {}
    if key.startswith('eyJ'):
        headers['Authorization'] = f'Bearer {key}'
    else:
        url += f"&api_key={key}"
    for attempt in range(4):
        try:
            return fm.http_json(url, headers=headers)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 401:
                _dkey['key'] = ''
                return None
            time.sleep(1 + attempt * 2 if e.code == 429 else 0.5)
        except Exception:
            time.sleep(0.5)
    return None


def details(kind, tid):
    """kind 'movie' | 'tv'. -> dict(title, alt, original, year, rating, genres, overview, poster) or None.
    Everything is shown in LANG (English by default); the other-language title is kept in 'alt' for searching."""
    ck = f"{kind}/{tid}|{LANG}"
    hit = _dcache.get(ck)
    if hit and time.time() - hit['t'] < DETAIL_TTL:
        return hit['d']
    if not _dkey['key']:
        return None
    main = _api(f"{kind}/{tid}", LANG)
    if not main:
        return None
    other = _api(f"{kind}/{tid}", ALT_LANG) or {}
    overview = main.get('overview') or other.get('overview') or ''   # not translated: use the other language
    date = main.get('release_date') or main.get('first_air_date') or ''
    title = main.get('title') or main.get('name') or ''
    alt = other.get('title') or other.get('name') or ''
    d = {'title': title, 'alt': alt if alt and alt != title else '',
         'original': main.get('original_title') or main.get('original_name') or '',
         'year': int(date[:4]) if date[:4].isdigit() else None,
         'rating': round(main.get('vote_average') or 0, 1),
         'genres': [g.get('name') for g in main.get('genres', []) if g.get('name')],
         'overview': overview,
         'poster': main.get('poster_path') or ''}
    _dcache[ck] = {'t': time.time(), 'd': d}
    return d


def download_poster(poster):
    """'/abc.jpg' -> local file name 'abc.jpg' (downloaded once). '' when unavailable."""
    if not poster:
        return ''
    name = poster.lstrip('/')
    dst = os.path.join(POSTER_DIR, name)
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return name
    import urllib.request
    try:
        req = urllib.request.Request(f"https://image.tmdb.org/t/p/{POSTER_SIZE}{poster}",
                                     headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        with open(dst, 'wb') as f:
            f.write(data)
        return name
    except Exception:
        return ''


# ==========================================
# SCANNING -> ITEMS
# ==========================================
def file_info(paths):
    size, added = 0, 0
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        size += st.st_size
        added = max(added, int(st.st_mtime))
    return size, added


def movie_items(entries, files, label, online):
    groups = defaultdict(list)           # tmdb id (or fallback key) -> [(name, parents, path)]
    rows = list(zip(entries, files))
    infos = fm.run_parallel(lambda r: fm.resolve_movie(r[0]), rows, "identifying films") \
        if online else [None] * len(rows)
    meta = {}
    for ((name, parents), path), info in zip(rows, infos):
        cand = (fm.movie_candidates(name, parents) or [(name, None)])[0]
        if info:
            key = ('id', info['id'])
        else:
            key = ('name', fm.normalize_name(cand[0]), cand[1])
        groups[key].append(path)
        meta[key] = (cand, info)
    items = []
    todo = [k for k in groups if k[0] == 'id']
    det = dict(zip(todo, fm.run_parallel(lambda k: details('movie', k[1]), todo, "film details"))) if online else {}
    for key, paths in groups.items():
        (title, year), _info = meta[key]
        d = det.get(key)
        size, added = file_info(paths)
        quality = max((fm.quality_of(p) for p in paths), default=(0, "?"))[1]
        items.append({
            'kind': 'movie', 'lib': label,
            'title': (d or {}).get('title') or title, 'alt': (d or {}).get('alt') or '', 'original': (d or {}).get('original') or '',
            'year': (d or {}).get('year') or year, 'rating': (d or {}).get('rating') or 0,
            'genres': (d or {}).get('genres') or [], 'overview': (d or {}).get('overview') or '',
            'poster': (d or {}).get('poster') or '', 'known': bool(d),
            'files': [os.path.basename(p) for p in paths], 'dirs': sorted({os.path.dirname(p) for p in paths}),
            'size': size, 'added': added, 'quality': quality, 'seasons': [],
            'play': title})
    return items


def show_items(entries, files, label, kind, online):
    saved = list(fm.IGNORE_SHOWS)
    fm.IGNORE_SHOWS[:] = []                 # a gallery shows everything, Doctor Who included
    try:
        shows = fm.collect_shows(entries)
    finally:
        fm.IGNORE_SHOWS[:] = saved
    paths = defaultdict(list)
    keys = set(shows)
    for (name, parents), path in zip(entries, files):
        info = fm.episode_info(name, parents)
        if not info:
            continue
        k = fm.canon_show(fm.normalize_name(info[0]))
        y = fm.show_year(name, parents)
        paths[f"{k} {y}" if f"{k} {y}" in keys else k].append(path)
    order = list(shows.items())
    res = fm.run_parallel(lambda kd: fm.resolve_show(kd), order, f"identifying {label}") \
        if online else [None] * len(order)
    ids = [r['id'] if r else None for r in res]
    dets = fm.run_parallel(lambda i: details('tv', i) if i else None, ids, f"{label} details") if online else [None] * len(ids)
    items = []
    for (k, d), det in zip(order, dets):
        size, added = file_info(paths.get(k, []))
        seasons = []
        for s in sorted(d['seasons']):
            eps = sorted(d['seasons'][s])
            top = max(eps) if eps else 0
            missing = [e for e in range(1, top + 1) if e not in d['seasons'][s] and not fm.is_ignored(d['base'], s, e)]
            seasons.append({'n': s, 'count': len(eps), 'eps': fm.compress(eps),
                            'missing': fm.compress(missing) if missing and s > 0 else ''})
        items.append({
            'kind': kind, 'lib': label,
            'title': (det or {}).get('title') or d['display'], 'alt': (det or {}).get('alt') or '', 'original': (det or {}).get('original') or '',
            'year': (det or {}).get('year') or d.get('year'), 'rating': (det or {}).get('rating') or 0,
            'genres': (det or {}).get('genres') or [], 'overview': (det or {}).get('overview') or '',
            'poster': (det or {}).get('poster') or '', 'known': bool(det),
            'files': [], 'dirs': sorted({os.path.dirname(p) for p in paths.get(k, [])})[:3],
            'size': size, 'added': added, 'quality': '', 'seasons': seasons,
            'episodes': sum(x['count'] for x in seasons if x['n'] > 0),
            'play': d['name']})
    return items


# ==========================================
# POSTER SHEETS (pages of 12 posters for the Discord bot)
# ==========================================
SHEET_COLS, SHEET_ROWS = 4, 3
CELL_W, CELL_H, CAPTION_H, GAP = 230, 345, 56, 12


def _font(size):
    from PIL import ImageFont
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, width, lines=2):
    out, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
    out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        while out[-1] and draw.textlength(out[-1] + "…", font=font) > width:
            out[-1] = out[-1][:-1]
        out[-1] += "…"
    return out


def _sort_key(title):
    import unicodedata
    return unicodedata.normalize('NFD', title).encode('ascii', 'ignore').decode().lower()


def make_sheets(items):
    """gallery/pages/<kind>_<n>.jpg (4x3 numbered posters) + gallery/pages.json
    {kind: [[index in library.json, ...] per page]}. Needs Pillow (pip install pillow)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("\nℹ️ Pillow is not installed: no poster pages for Discord "
              "(run: pip install pillow, then this script again).")
        return False
    pages_dir = os.path.join(OUT_DIR, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    for f in os.listdir(pages_dir):                          # old pages may be longer than the new ones
        if f.endswith(".jpg"):
            os.remove(os.path.join(pages_dir, f))
    per_page = SHEET_COLS * SHEET_ROWS
    font, small, badge = _font(15), _font(13), _font(18)
    layout = {}
    for kind in ('movie', 'tv', 'anime'):
        idxs = sorted((i for i, it in enumerate(items) if it['kind'] == kind), key=lambda i: _sort_key(items[i]['title']))
        pages = [idxs[k:k + per_page] for k in range(0, len(idxs), per_page)]
        layout[kind] = pages
        for n, page in enumerate(pages, 1):
            W = SHEET_COLS * CELL_W + (SHEET_COLS + 1) * GAP
            H = SHEET_ROWS * (CELL_H + CAPTION_H) + (SHEET_ROWS + 1) * GAP
            img = Image.new('RGB', (W, H), (23, 25, 34))
            d = ImageDraw.Draw(img)
            for pos, i in enumerate(page):
                it = items[i]
                x = GAP + (pos % SHEET_COLS) * (CELL_W + GAP)
                y = GAP + (pos // SHEET_COLS) * (CELL_H + CAPTION_H + GAP)
                pf = os.path.join(POSTER_DIR, it['poster'].lstrip('/')) if it.get('poster') else ''
                try:
                    img.paste(Image.open(pf).convert('RGB').resize((CELL_W, CELL_H)), (x, y))
                except Exception:
                    d.rectangle([x, y, x + CELL_W, y + CELL_H], fill=(36, 40, 66))
                    for k, line in enumerate(_wrap(d, it['title'], font, CELL_W - 24, 5)):
                        d.text((x + 12, y + CELL_H // 2 - 40 + k * 20), line, fill=(180, 184, 200), font=font)
                d.ellipse([x + 6, y + 6, x + 36, y + 36], fill=(229, 160, 13))
                label = str(pos + 1)
                d.text((x + 21 - d.textlength(label, font=badge) / 2, y + 12), label, fill=(17, 17, 17), font=badge)
                for k, line in enumerate(_wrap(d, it['title'], font, CELL_W)):
                    d.text((x, y + CELL_H + 4 + k * 18), line, fill=(232, 233, 238), font=font)
                sub = " · ".join(str(v) for v in (it.get('year'), ("★ %s" % it['rating']) if it.get('rating') else None) if v)
                d.text((x, y + CELL_H + 4 + 36), sub, fill=(139, 144, 163), font=small)
            img.save(os.path.join(pages_dir, f"{kind}_{n}.jpg"), quality=82)
    with open(os.path.join(OUT_DIR, "pages.json"), 'w', encoding='utf-8') as f:
        json.dump({'per_page': per_page, 'pages': layout}, f)
    print(f"\n🖼️ Discord poster pages: {sum(len(v) for v in layout.values())} pages in {pages_dir}")
    return True


# ==========================================
# HTML
# ==========================================
HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Library</title>
<style>
:root{--bg:#0e0f13;--panel:#171922;--panel2:#1f2230;--txt:#e8e9ee;--mut:#8b90a3;--acc:#e5a00d;--ok:#4cc38a;--bad:#ef6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:rgba(14,15,19,.94);backdrop-filter:blur(8px);border-bottom:1px solid #23263a;padding:12px 18px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:18px;margin:0 8px 0 0}
.tabs{display:flex;gap:6px}.tab{background:var(--panel);border:1px solid #2a2e42;color:var(--txt);padding:7px 13px;border-radius:999px;cursor:pointer;font:inherit}
.tab.on{background:var(--acc);color:#111;border-color:var(--acc);font-weight:600}.tab small{opacity:.7;margin-left:4px}
input,select{background:var(--panel);border:1px solid #2a2e42;color:var(--txt);padding:8px 11px;border-radius:8px;font:inherit}
#q{flex:1;min-width:180px;max-width:360px}label.chk{color:var(--mut);display:flex;gap:5px;align-items:center;cursor:pointer}
#count{color:var(--mut);margin-left:auto}
main{padding:18px;display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:16px}
.card{cursor:pointer;border-radius:10px;overflow:hidden;background:var(--panel);transition:transform .12s;content-visibility:auto;contain-intrinsic-size:260px}
.card:hover{transform:translateY(-3px);outline:2px solid var(--acc)}
.poster{aspect-ratio:2/3;width:100%;display:block;object-fit:cover;background:var(--panel2)}
.ph{aspect-ratio:2/3;display:flex;align-items:center;justify-content:center;text-align:center;padding:12px;color:var(--mut);background:linear-gradient(160deg,#242842,#171922);font-weight:600}
.meta{padding:8px 10px 10px}.t{font-weight:600;font-size:13px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.s{color:var(--mut);font-size:12px;margin-top:2px;display:flex;justify-content:space-between}.star{color:var(--acc)}
.warn{color:var(--bad)}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;justify-content:center;z-index:10;padding:18px}
#modal.on{display:flex}.box{background:var(--panel);border-radius:14px;max-width:880px;width:100%;max-height:92vh;overflow:auto;display:grid;grid-template-columns:240px 1fr;gap:20px;padding:20px;position:relative}
.box img,.box .ph{width:100%;border-radius:10px}.box h2{margin:0 0 4px}.x{position:absolute;right:12px;top:8px;background:none;border:0;color:var(--mut);font-size:26px;cursor:pointer}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.chip{background:var(--panel2);border-radius:999px;padding:3px 10px;font-size:12px;color:var(--mut)}
.ov{color:#cfd2de;margin:10px 0}.btn{background:var(--acc);color:#111;border:0;padding:8px 13px;border-radius:8px;font-weight:600;cursor:pointer;margin:4px 6px 4px 0}
.btn.alt{background:var(--panel2);color:var(--txt)}table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}td,th{padding:5px 8px;border-bottom:1px solid #262a3d;text-align:left}th{color:var(--mut);font-weight:500}
.path{color:var(--mut);font-size:12px;word-break:break-all}
.pick{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}.pick select{background:var(--panel2);color:var(--txt);border:1px solid #2a2e42;border-radius:8px;padding:6px}
#player{position:fixed;inset:0;background:#000;z-index:20;display:none;flex-direction:column}#player.on{display:flex}
.pbar{display:flex;gap:8px;align-items:center;padding:8px 10px;background:#111;flex-wrap:wrap}.pbar b{flex:1;min-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pbar select{background:var(--panel2);color:var(--txt);border:1px solid #2a2e42;border-radius:8px;padding:5px;max-width:42vw}
#pv{flex:1;width:100%;min-height:0;background:#000}#pmsg{position:absolute;left:0;right:0;top:50%;text-align:center;color:#ccc;pointer-events:none;padding:0 20px}
@media(max-width:640px){.box{grid-template-columns:1fr}main{grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px}}
</style></head><body>
<header>
 <h1>📚 Library</h1>
 <div class="tabs" id="tabs"></div>
 <input id="q" type="search" placeholder="Search (title in French or English, year…)" autofocus>
 <select id="sort">
  <option value="az">A → Z</option><option value="added">Recently added</option>
  <option value="year">Year (newest)</option><option value="rating">Rating</option><option value="size">Size</option>
 </select>
 <select id="genre"><option value="">All genres</option></select>
 <label class="chk"><input type="checkbox" id="unk"> Unidentified</label>
 <label class="chk"><input type="checkbox" id="gaps"> Missing episodes</label>
 <input id="prefix" size="3" title="Bot prefix" style="width:46px">
 <span id="count"></span>
 __LOGOUT__
</header>
<main id="grid"></main>
<div id="modal"><div class="box" id="box"></div></div>
<div id="player"><div class="pbar"><b id="ptitle"></b><select id="paud" title="Audio"></select><select id="psub" title="Subtitles"></select><button class="btn alt" id="pclose">✕ Close</button></div><video id="pv" controls playsinline></video><div id="pmsg"></div></div>
<script>
const DATA=__DATA__;
const $=s=>document.querySelector(s);
const fold=s=>(s||'').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g,'');
const TABS=[['all','All'],['movie','Movies'],['tv','TV Shows'],['anime','Anime']];
let tab='all';
DATA.forEach(d=>{d._s=fold([d.title,d.alt,d.original,d.year,d.files.join(' ')].join(' '));
  d._gaps=d.seasons.some(s=>s.missing)});
function fmtSize(n){const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i>1?1:0)+' '+u[i]}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function drawTabs(){$('#tabs').innerHTML=TABS.map(([k,l])=>{const n=k=='all'?DATA.length:DATA.filter(d=>d.kind==k).length;
 return `<button class="tab ${k==tab?'on':''}" data-k="${k}">${l}<small>${n}</small></button>`}).join('');
 document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{tab=b.dataset.k;drawTabs();render()})}
const genres=[...new Set(DATA.flatMap(d=>d.genres))].sort((a,b)=>a.localeCompare(b,'fr'));
$('#genre').innerHTML+=genres.map(g=>`<option>${esc(g)}</option>`).join('');
$('#prefix').value=localStorage.getItem('prefix2')||'__PREFIX__';
$('#prefix').oninput=e=>localStorage.setItem('prefix2',e.target.value);
function card(d,i){
 const img=d.poster?`<img class="poster" loading="lazy" src="posters/${esc(d.poster.replace(/^\//,''))}" alt="">`:`<div class="ph">${esc(d.title)}</div>`;
 const info=d.kind=='movie'?(d.quality||''):`${d.episodes||0} ep.`;
 return `<div class="card" data-i="${i}">${img}<div class="meta"><div class="t">${esc(d.title)}</div>
 <div class="s"><span>${d.year||''}</span><span>${d.rating?'<span class="star">★</span> '+d.rating:''}${d._gaps?' <span class="warn" title="Missing episodes">⚠</span>':''}</span></div></div></div>`}
function render(){
 const q=fold($('#q').value).split(/\s+/).filter(Boolean),g=$('#genre').value,unk=$('#unk').checked,gp=$('#gaps').checked,sort=$('#sort').value;
 let L=DATA.map((d,i)=>[d,i]).filter(([d])=>(tab=='all'||d.kind==tab)&&(!g||d.genres.includes(g))&&(!unk||!d.known)&&(!gp||d._gaps)&&q.every(w=>d._s.includes(w)));
 const cmp={az:(a,b)=>fold(a.title).localeCompare(fold(b.title)),added:(a,b)=>b.added-a.added,year:(a,b)=>(b.year||0)-(a.year||0),
  rating:(a,b)=>b.rating-a.rating,size:(a,b)=>b.size-a.size}[sort];
 L.sort((x,y)=>cmp(x[0],y[0]));
 $('#grid').innerHTML=L.map(([d,i])=>card(d,i)).join('');
 $('#count').textContent=L.length+' title'+(L.length>1?'s':'');
 document.querySelectorAll('.card').forEach(c=>c.onclick=()=>open(+c.dataset.i))}
function qFor(d,s,e){return d.kind=='movie'?d.play:`${d.play} s${String(s||1).padStart(2,'0')}e${String(e||1).padStart(2,'0')}`}
function epsOf(sea){const o=[];(sea.eps||'').split(',').forEach(p=>{const m=p.trim().match(/^(\d+)(?:-(\d+))?$/);if(m)for(let n=+m[1];n<=+(m[2]||m[1]);n++)o.push(n)});return o}
function cmdFor(d,s,e){const p=$('#prefix').value||'/';return p=='/'?`/play query:${qFor(d,s,e)}`:`${p}play ${qFor(d,s,e)}`}
function open(i){const d=DATA[i];
 const img=d.poster?`<img src="posters/${esc(d.poster.replace(/^\//,''))}" alt="">`:`<div class="ph" style="min-height:300px">${esc(d.title)}</div>`;
 const chips=[d.year,d.kind=='movie'?'Movie':d.kind=='anime'?'Anime':'TV Show',d.rating?'★ '+d.rating:'',d.quality,d.size?fmtSize(d.size):'',...d.genres].filter(Boolean).map(c=>`<span class="chip">${esc(c)}</span>`).join('');
 let body='';
 const real=d.kind=='movie'?[]:d.seasons.filter(x=>x.n>0&&epsOf(x).length);let selS=real.length?real[0].n:1,selE=real.length?epsOf(real[0])[0]:1;
 const pickHtml=real.length?`<div class="pick"><select id="ssel">${real.map(x=>`<option value="${x.n}">Season ${x.n}</option>`).join('')}</select><select id="esel"></select></div>`:'';
 if(d.seasons.length){body=`<table><tr><th>Season</th><th>Episodes</th><th>Present</th><th>Missing</th></tr>`+d.seasons.map(s=>
  `<tr><td>${s.n?('S'+String(s.n).padStart(2,'0')):'Specials'}</td><td>${s.count}</td><td>${esc(s.eps)}</td><td class="${s.missing?'warn':''}">${esc(s.missing)||'—'}</td></tr>`).join('')+'</table>'}
 else body=`<div class="path">${d.files.map(esc).join('<br>')}</div>`;
 $('#box').innerHTML=`<button class="x" onclick="closeM()">×</button><div>${img}</div><div><h2>${esc(d.title)}</h2>
  ${d.original&&d.original!=d.title?`<div class="path">${esc(d.original)}</div>`:''}<div class="chips">${chips}</div>
  <div class="ov">${esc(d.overview)||'<i>No synopsis (unidentified title or TMDB unavailable).</i>'}</div>
  ${pickHtml}${window.WATCH?'<button class="btn" id="wt">▶ Watch here</button>':''}<button class="btn alt" id="cp">📋 Copy the bot command</button><button class="btn alt" id="cp2">Copy title only</button>${d.dirs.map(x=>`<div class="path">${esc(x)}</div>`).join('')}${body}</div>`;
 const copy=(t,b)=>{(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).catch(()=>prompt('Copy:',t));b.textContent='✔ '+t};
 const fillEps=()=>{const sea=real.find(x=>x.n==selS);$('#esel').innerHTML=epsOf(sea).map(n=>`<option value="${n}">Episode ${n}</option>`).join('');$('#esel').value=selE};
 if(real.length){fillEps();$('#ssel').onchange=ev=>{selS=+ev.target.value;selE=epsOf(real.find(x=>x.n==selS))[0];fillEps()};$('#esel').onchange=ev=>{selE=+ev.target.value}}
 $('#cp').onclick=()=>copy(cmdFor(d,selS,selE),$('#cp'));$('#cp2').onclick=()=>copy(qFor(d,selS,selE),$('#cp2'));
 if($('#wt'))$('#wt').onclick=()=>{closeM();watch(d,selS,selE)};
 $('#modal').classList.add('on')}
function closeM(){$('#modal').classList.remove('on')}
let hls=null;
function closeP(){const v=$('#pv');v.pause();if(hls){hls.destroy();hls=null}v.removeAttribute('src');v.load();$('#player').classList.remove('on')}
async function watch(d,s,e,a,sub,t0){
 const msg=$('#pmsg'),v=$('#pv');$('#player').classList.add('on');msg.textContent='Preparing the stream… (it can take 10-20 seconds)';
 if(hls){hls.destroy();hls=null}
 try{
  let u=`/api/watch?kind=${d.kind}&title=${encodeURIComponent(d.play)}`+(d.kind=='movie'?'':`&season=${s}&episode=${e}`)+(a!=null?`&a=${a}`:'')+(sub!=null?`&s=${sub}`:'');
  const r=await fetch(u,{credentials:'same-origin'});let j={};try{j=await r.json()}catch(_){}
  if(!r.ok){msg.textContent=j.error||('Not available ('+r.status+')');return}
  $('#ptitle').textContent=j.title;
  const fill=(id,list,cur,none)=>{$(id).innerHTML=(none?'<option value="-1">'+none+'</option>':'')+list.map(x=>`<option value="${x.i}">${esc(x.label)}</option>`).join('');$(id).value=cur;$(id).style.display=list.length||none?'':'none'};
  fill('#paud',j.audios,j.audio,'');fill('#psub',j.subs,j.sub,'No subtitles');
  const reload=()=>watch(d,s,e,$('#paud').value,$('#psub').value,v.currentTime||0);
  $('#paud').onchange=reload;$('#psub').onchange=reload;
  const start=()=>{msg.textContent='';v.play().catch(()=>{})};
  if(window.Hls&&Hls.isSupported()){hls=new Hls({startPosition:t0||-1,maxBufferLength:40});hls.loadSource(j.url);hls.attachMedia(v);
   hls.on(Hls.Events.MANIFEST_PARSED,start);
   hls.on(Hls.Events.ERROR,(_,x)=>{if(x.fatal)msg.textContent='Playback error ('+x.details+'). Try again in a moment.'})}
  else{v.src=j.url;v.onloadedmetadata=()=>{if(t0)v.currentTime=t0;start()};v.onerror=()=>{msg.textContent='Playback error.'}}
 }catch(err){msg.textContent='Could not reach the server.'}}
$('#pclose').onclick=closeP;
$('#modal').onclick=e=>{if(e.target.id=='modal')closeM()};document.onkeydown=e=>{if(e.key=='Escape'){if($('#player').classList.contains('on'))closeP();else closeM()}};
['q','sort','genre','unk','gaps'].forEach(id=>$('#'+id).oninput=render);
drawTabs();render();
</script></body></html>
"""


def build_html(items, prefix, share=False):
    """share=True: no file names, no folder paths, no sizes (safe to give to other people)."""
    if share:
        items = [dict(i, files=[], dirs=[], size=0) for i in items]
    data = json.dumps(items, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    logout = ('<form method="post" action="/logout" style="margin:0"><button class="tab" style="font:inherit">Log out</button></form>'
              if share else '')
    return HTML.replace('__DATA__', data).replace('__PREFIX__', prefix).replace('__LOGOUT__', logout)


# ==========================================
# MAIN
# ==========================================
def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if not fm.ensure_paths():
        input("\nPress Enter to exit...")
        return
    offline = '--offline' in sys.argv
    key = '' if offline else fm.load_tmdb_key()
    fm._state['key'] = key
    _dkey['key'] = key
    online = bool(key)
    if not online:
        print("⚠️ No TMDB key (or --offline): titles come from file names, no posters.")
    fm.cache_load()
    dcache_load()
    os.makedirs(POSTER_DIR, exist_ok=True)

    items = []
    for label, path, kind in fm.libs():
        print(f"\n=== {label} ({fm.show_path(path)}) ===")
        entries, files = fm.scan_library(path)
        if entries is None:
            continue
        print(f"{len(entries)} video files")
        if kind == 'movie':
            items += movie_items(entries, files, label, online)
        else:
            items += show_items(entries, files, label, kind, online)
    fm.cache_save()
    dcache_save()

    seen = defaultdict(list)             # same title twice (remakes): add the year to the bot command
    for it in items:
        seen[(it['kind'] == 'movie', fm.normalize_name(it['play']))].append(it)
    for group in seen.values():
        if len(group) > 1:
            for it in group:
                if it['year'] and it['kind'] == 'movie':
                    it['play'] = f"{it['play']} {it['year']}"

    if online:
        posters = sorted({i['poster'] for i in items if i['poster']})
        todo = [p for p in posters if not os.path.isfile(os.path.join(POSTER_DIR, p.lstrip('/')))]
        if todo:
            print(f"\nDownloading {len(todo)} posters...")
            fm.run_parallel(download_poster, todo, "posters")
        for it in items:                         # no file -> placeholder instead of a broken image
            if it['poster'] and not os.path.isfile(os.path.join(POSTER_DIR, it['poster'].lstrip('/'))):
                it['poster'] = ''

    out = os.path.join(OUT_DIR, "index.html")
    with open(out, 'w', encoding='utf-8') as f:
        f.write(build_html(items, BOT_PREFIX))
    os.makedirs(os.path.join(OUT_DIR, "share"), exist_ok=True)       # copy without file names/paths, for the web server
    with open(os.path.join(OUT_DIR, "share", "index.html"), 'w', encoding='utf-8') as f:
        f.write(build_html(items, BOT_PREFIX, share=True))
    # same data for the Discord bot's $galerie command (no folder paths in it)
    keep = ('kind', 'title', 'alt', 'original', 'year', 'rating', 'genres', 'overview', 'poster', 'known',
            'quality', 'seasons', 'episodes', 'play')
    with open(os.path.join(OUT_DIR, "library.json"), 'w', encoding='utf-8') as f:
        json.dump([{k: i.get(k) for k in keep} for i in items], f, ensure_ascii=False)
    # plain-text list next to the gallery: English title | French title | year | type | name to use with /play
    names = {'movie': 'Movie', 'tv': 'TV show', 'anime': 'Anime'}
    rows = sorted(items, key=lambda i: (i['kind'], _sort_key(i['title'])))
    with open(os.path.join(OUT_DIR, "titles.txt"), 'w', encoding='utf-8') as f:
        f.write("English title | French title | Year | Type | /play name\n")
        for i in rows:
            f.write(f"{i['title']} | {i.get('alt') or ''} | {i.get('year') or ''} | {names.get(i['kind'], i['kind'])} | {i['play']}\n")
    make_sheets(items)
    known = sum(1 for i in items if i['known'])
    print(f"\n✅ {len(items)} titles ({known} identified on TMDB) -> {out}")
    print(f"   Discord bot data: {os.path.join(OUT_DIR, 'library.json')} (+ the posters folder)")
    if len(items) - known:
        print(f"   {len(items) - known} not identified: tick 'Non identifiés' in the page to see them.")
    if '--no-open' not in sys.argv:
        webbrowser.open('file:///' + out.replace('\\', '/'))


if __name__ == "__main__":
    main()
