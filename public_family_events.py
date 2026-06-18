#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / 'public_family_events_cache.json'
PT = ZoneInfo('America/Los_Angeles') if ZoneInfo else None
NOW = lambda: datetime.now(PT) if PT else datetime.utcnow()
CACHE_HOURS = 6
DAY_WINDOW = 21
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; SanDiegoFunFinder/1.0; public event discovery)'}
TIMEOUT = 18

SOURCE_LABELS = {
    'city': 'City of San Diego',
    'family': 'San Diego Family',
    'kids': 'Kids Out And About',
    'kpbs': 'KPBS',
    'reader': 'San Diego Reader',
}

POSITIVE_KEYWORDS = {
    'toddler': 4,
    'toddlers': 4,
    'preschool': 4,
    'preschooler': 4,
    'family': 2,
    'kids': 2,
    'children': 2,
    'child': 2,
    'ages 0': 5,
    'ages 1': 5,
    'ages 2': 5,
    'ages 3': 5,
    'ages 4': 4,
    'ages 5': 4,
    'baby': 4,
    'babies': 4,
    'storytime': 5,
    'story time': 5,
    'puppet': 4,
    'sensory': 4,
    'craft': 3,
    'crafts': 3,
    'lego': 3,
    'museum': 3,
    'aquarium': 3,
    'zoo': 3,
    'park': 2,
    'nature': 2,
    'free': 2,
    'farmers market': 3,
    'farmers\' market': 3,
    'movie': 2,
    'beach': 2,
    'music': 1,
    'festival': 2,
    'steam': 3,
    'science': 2,
    'play': 3,
    'playtime': 4,
}

NEGATIVE_KEYWORDS = {
    '21+': -8,
    'adults only': -8,
    'cocktail': -5,
    'beer': -4,
    'wine': -4,
    'networking': -8,
    'professional': -5,
    'career': -6,
    'gala': -5,
    'fundraiser': -4,
    'marathon': -3,
    '5k': -3,
    'politics': -3,
}

CATEGORY_RULES = [
    ('Farmers market', ['farmers market', 'mercato', 'market']),
    ('Museum / zoo', ['museum', 'aquarium', 'zoo', 'balboa park']),
    ('Story / reading', ['storytime', 'story time', 'book', 'read aloud']),
    ('Craft / STEAM', ['craft', 'lego', 'steam', 'science', 'maker']),
    ('Outdoor / nature', ['park', 'beach', 'nature', 'hike', 'garden', 'outdoor']),
    ('Performance / movie', ['movie', 'music', 'concert', 'puppet', 'theater', 'show']),
    ('Festival / community', ['festival', 'fair', 'community', 'celebration', 'free day']),
]


@dataclass
class Event:
    title: str
    date: str
    url: str
    source: str
    source_label: str
    venue: str = ''
    description: str = ''
    time_text: str = ''
    category: str = ''
    is_free: bool = False
    score: int = 0
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def clean_text(value: str) -> str:
    value = unescape(value or '')
    value = re.sub(r'<br\s*/?>', ' ', value, flags=re.I)
    value = re.sub(r'<[^>]+>', ' ', value)
    value = value.replace('\xa0', ' ')
    value = re.sub(r'\s+', ' ', value)
    return value.strip(' \n\t-')


def fetch(url: str) -> str:
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode('utf-8', errors='replace')


def parse_mmddyyyy(value: str) -> str | None:
    for pat in ('%m/%d/%Y', '%m-%d-%Y'):
        try:
            return datetime.strptime(value, pat).date().isoformat()
        except Exception:
            pass
    return None


def parse_long_date(value: str) -> str | None:
    value = clean_text(value)
    patterns = [
        '%A, %B %d, %Y',
        '%B %d, %Y',
    ]
    if ' from ' in value:
        value = value.split(' from ', 1)[0]
    if ', ' in value and re.search(r'\d{4},', value):
        value = value.rsplit(',', 1)[0] if value.count(',') > 2 else value
    for pat in patterns:
        try:
            return datetime.strptime(value, pat).date().isoformat()
        except Exception:
            pass
    m = re.search(r'([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})', value)
    if m:
        try:
            return datetime.strptime(m.group(1), '%A, %B %d, %Y').date().isoformat()
        except Exception:
            pass
    m = re.search(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})', value)
    if m:
        try:
            return datetime.strptime(m.group(1), '%B %d, %Y').date().isoformat()
        except Exception:
            pass
    return None


def score_event(title: str, description: str, venue: str = '', source: str = '') -> tuple[int, bool, list[str], str]:
    text = f'{title} {description} {venue}'.lower()
    score = 0
    tags: list[str] = []
    for key, val in POSITIVE_KEYWORDS.items():
        if key in text:
            score += val
            if key in {'free', 'storytime', 'story time', 'craft', 'lego', 'museum', 'aquarium', 'zoo', 'farmers market', 'park', 'beach', 'playtime'}:
                tags.append(key.replace("farmers\' market", 'farmers market'))
    for key, val in NEGATIVE_KEYWORDS.items():
        if key in text:
            score += val
    if source == 'family':
        score += 2
    elif source == 'kids':
        score += 2
    elif source == 'city':
        score += 1
    is_free = 'free' in text or '$0' in text or 'no cost' in text
    category = 'Family outing'
    for label, keys in CATEGORY_RULES:
        if any(k in text for k in keys):
            category = label
            break
    return score, is_free, sorted(set(tags)), category



def detect_area(text: str) -> str:
    t = text.lower()
    areas = [
        ('beach', ['beach', 'ocean', 'coast', 'del mar', 'coronado', 'mission beach', 'la jolla', 'ocean beach', 'pacific beach', 'shelter island', 'mission bay']),
        ('downtown', ['downtown', 'gaslamp', 'little italy', 'embarcadero', 'barrio logan', 'petco', 'harbor', 'kettner']),
        ('balboa', ['balboa', 'san diego zoo', 'fleet science', 'natural history museum', 'spreckels', 'botanical']),
        ('north-county', ['north county', 'carlsbad', 'encinitas', 'escondido', 'oceanside', 'solana beach', 'vista', 'san marcos', 'rancho santa fe', 'legoland', 'bonsall']),
        ('east-county', ['east county', 'el cajon', 'santee', 'lakeside', 'alpine', 'lemon grove', 'spring valley', 'la mesa']),
        ('south-bay', ['south bay', 'chula vista', 'national city', 'bonita', 'imperial beach', 'otay']),
    ]
    for area, keys in areas:
        if any(k in t for k in keys):
            return area
    return 'central-san-diego' if 'san diego' in t else 'unknown'


def detect_time_period(time_text: str, text: str = '') -> str:
    t = f'{time_text} {text}'.lower()
    if re.search(r'\b(6|7|8|9|10|11)\s*(a\.?m\.?|am)\b|\bmorning\b', t):
        return 'morning'
    if re.search(r'\b(12|1|2|3|4|5)\s*(p\.?m\.?|pm)\b|\b(noon|afternoon)\b', t):
        return 'afternoon'
    if re.search(r'\b(6|7|8|9|10|11)\s*(p\.?m\.?|pm)\b|\b(evening|night|sunset)\b', t):
        return 'evening'
    return 'unknown'


def classify_metadata(title: str, description: str, venue: str, source: str, time_text: str = '', is_free: bool = False, category: str = '') -> dict:
    text = clean_text(f'{title} {description} {venue} {category}').lower()
    kids_re = r'\b(kids?|children|child|family|families|toddler|baby|babies|preschool|camp|storytime|story time|teen|ages?\s*\d|lego|puppet)\b'
    adult_re = r'\b(21\+|adult|adults only|cocktail|beer|wine|bar|brewery|winemaker|networking|professional|career|trivia night|karaoke|open mic)\b'
    toddler_re = r'\b(toddler|baby|infant|preschool|storytime|story time|sensory|ages?\s*[0-5])\b'
    audience = 'all_ages'
    # Explicit adult/21+ language wins over generic words like "storytime"
    # so Adult Storytime does not leak into kid/toddler filters.
    if re.search(adult_re, text):
        audience = 'adult'
    elif re.search(toddler_re, text):
        audience = 'young_children'
    elif re.search(kids_re, text) or source in {'family', 'kids'}:
        audience = 'family'
    indoor = bool(re.search(r'\b(indoor|museum|library|gallery|center|theatre|theater|aquarium|school|studio|hotel|restaurant|bar|brewery)\b', text))
    outdoor = bool(re.search(r'\b(outdoor|park|beach|garden|nature|hike|trail|market|fair|festival|farmers|amphitheatre|walk|bay|waterfront)\b', text))
    metadata = {
        'audience': audience,
        'age_groups': sorted([g for g, ok in {
            'toddler': bool(re.search(toddler_re, text)),
            'kids': bool(re.search(kids_re, text)),
            'teens': bool(re.search(r'\b(teen|ages?\s*(1[0-9]|13\+))\b', text)),
            'adults': audience == 'adult' or bool(re.search(adult_re, text)),
        }.items() if ok]),
        'features': {
            'free': bool(is_free),
            'outdoor': outdoor,
            'indoor': indoor,
            'dog_friendly': bool(re.search(r'\b(dog|dogs|pet|leash)\b', text)),
            'toddler_friendly': bool(re.search(toddler_re, text)),
            'stroller_friendly': bool(re.search(r'\b(stroller|paved|flat|accessible|wheelchair)\b', text)),
            'low_walking': bool(re.search(r'\b(accessible|seated|easy|short walk|wheelchair|bench)\b', text)),
            'shade': bool(re.search(r'\b(shade|shaded|covered|indoor)\b', text)),
            'bathrooms': bool(re.search(r'\b(restroom|bathroom|facilities|park|center|museum|library|hotel)\b', text)),
            'food_nearby': bool(re.search(r'\b(food|restaurant|snack|vendor|market|concession|cafe|dining)\b', text)),
        },
        'area': detect_area(text),
        'time_period': detect_time_period(time_text, text),
        'source_key': source,
        'metadata_version': 2,
    }
    return metadata

def normalize_event(title: str, date: str | None, url: str, source: str, description: str = '', venue: str = '', time_text: str = '') -> Event | None:
    if not title or not date:
        return None
    score, is_free, tags, category = score_event(title, description, venue, source)
    metadata = classify_metadata(title, description, venue, source, time_text, is_free, category)
    # Keep a broader set for adult/general San Diego browsing while still dropping
    # strongly irrelevant family-unfriendly items from family-first sources.
    if score < -6:
        return None
    return Event(
        title=clean_text(title),
        date=date,
        url=url,
        source=source,
        source_label=SOURCE_LABELS[source],
        venue=clean_text(venue),
        description=clean_text(description),
        time_text=clean_text(time_text),
        category=category,
        is_free=is_free,
        score=score,
        tags=tags,
        metadata=metadata,
    )


def parse_city_events(html: str) -> list[Event]:
    events: list[Event] = []
    chunks = html.split('<div class="grid-x grid-margin-x event-listing-large">')[1:]
    for chunk in chunks:
        title = clean_text(re.search(r'<h2[^>]*>(.*?)</h2>', chunk, re.S).group(1)) if re.search(r'<h2[^>]*>(.*?)</h2>', chunk, re.S) else ''
        dt_text = clean_text(re.search(r'<p class="post__date[^"]*"><strong>(.*?)</strong>', chunk, re.S).group(1)) if re.search(r'<p class="post__date[^"]*"><strong>(.*?)</strong>', chunk, re.S) else ''
        date = parse_long_date(dt_text)
        href = re.search(r'<p><a href="([^"]+)"', chunk)
        desc = ''
        dm = re.search(r'</p>(.*?)<p><a href=', chunk, re.S)
        if dm:
            desc = clean_text(dm.group(1))
        ev = normalize_event(
            title=title,
            date=date,
            url=urljoin('https://www.sandiego.gov', href.group(1) if href else '/events'),
            source='city',
            description=desc,
            time_text=dt_text,
        )
        if ev:
            events.append(ev)
    return events


def parse_family_calendar(html: str) -> list[Event]:
    events: list[Event] = []
    item_pat = re.compile(
        r'<li class="event".*?<a\s+href="(?P<href>[^"]+)"[^>]*data-bs-content="(?P<tooltip>.*?)"[^>]*title="(?P<title>[^"]+)"',
        re.S,
    )
    parts = re.split(r'(?=<td[^>]*has-events[^>]*>)', html)
    for part in parts:
        if 'has-events' not in part:
            continue
        chunk = part.split('</td>', 1)[0]
        dm = re.search(r'/things-to-do/events-calendar/day/([0-9\-]+)', chunk)
        if not dm:
            continue
        date = parse_mmddyyyy(dm.group(1))
        for im in item_pat.finditer(chunk):
            tooltip = unescape(im.group('tooltip'))
            desc_match = re.search(r'rsepro-calendar-tooltip-description[^>]*>(.*?)</div>', tooltip, re.S)
            desc = clean_text(desc_match.group(1)) if desc_match else ''
            ev = normalize_event(
                title=im.group('title'),
                date=date,
                url=urljoin('https://www.sandiegofamily.com', im.group('href')),
                source='family',
                description=desc,
            )
            if ev:
                events.append(ev)
    return events


def parse_kidsoutandabout(html: str) -> list[Event]:
    events: list[Event] = []
    pattern = re.compile(
        r'<h2><a href="(?P<href>/content/[^"]+)">(?P<title>[^<]+)</a></h2>'
        r'.*?<div class="field field-name-field-short-description[^"]*">.*?<div class="field-item even">(?P<desc>.*?)</div>'
        r'.*?<div class="field field-type-datetime field-field-activity-dates">.*?(?P<dates>(?:<div class="field-item even"><span class="date-display-single">[^<]+</span></div>)+)',
        re.S,
    )
    for m in pattern.finditer(html):
        dates = re.findall(r'<span class="date-display-single">([^<]+)</span>', m.group('dates'))
        for d in dates[:8]:
            ev = normalize_event(
                title=m.group('title'),
                date=parse_mmddyyyy(clean_text(d)),
                url=urljoin('https://sandiego.kidsoutandabout.com', m.group('href')),
                source='kids',
                description=m.group('desc'),
            )
            if ev:
                events.append(ev)
    return events


def parse_kpbs(html: str) -> list[Event]:
    events: list[Event] = []
    chunks = re.findall(r'<ps-promo class="EventPromoC"[\s\S]*?</ps-promo>', html)
    for chunk in chunks[:120]:
        tm = re.search(r'<h3 class="EventPromoC-title"\s*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not tm:
            continue
        time_text = clean_text(re.search(r'<span class="EventPromoC-time-text">(.*?)</span>', chunk, re.S).group(1)) if re.search(r'<span class="EventPromoC-time-text">(.*?)</span>', chunk, re.S) else ''
        date = parse_long_date(time_text)
        venue = clean_text(re.search(r'<div class="EventPromoC-venue[^"]*">.*?</svg>\s*(.*?)\s*</div>', chunk, re.S).group(1)) if re.search(r'<div class="EventPromoC-venue[^"]*">.*?</svg>\s*(.*?)\s*</div>', chunk, re.S) else ''
        categories = [clean_text(x) for x in re.findall(r'EventPromoC-categories-item">.*?>(.*?)</a>', chunk, re.S)]
        desc = ''
        gm = re.search(r'data-platform="google-calendar" href="([^"]+)"', chunk)
        if gm:
            qs = parse_qs(urlparse(unescape(gm.group(1))).query)
            desc = unquote(qs.get('details', [''])[0])
        ev = normalize_event(
            title=tm.group(2),
            date=date,
            url=tm.group(1),
            source='kpbs',
            description=' '.join(categories + [desc]),
            venue=venue,
            time_text=time_text,
        )
        if ev and ev.score >= 2:
            events.append(ev)
    return events



def parse_reader_events(html: str) -> list[Event]:
    events: list[Event] = []
    scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S | re.I)
    for raw in scripts[:180]:
        raw = unescape(raw).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, list):
            items = data
        else:
            items = [data]
        for item in items:
            if not isinstance(item, dict) or item.get('@type') != 'Event':
                continue
            name = item.get('name') or ''
            start = str(item.get('startDate') or '')[:10]
            if not re.match(r'\d{4}-\d{2}-\d{2}', start):
                continue
            loc = item.get('location') or {}
            if isinstance(loc, dict):
                venue = clean_text(loc.get('name') or '')
                addr = loc.get('address') or {}
                if isinstance(addr, dict):
                    city = addr.get('addressLocality') or ''
                    if city and city.lower() not in venue.lower():
                        venue = clean_text(f'{venue}, {city}')
            else:
                venue = ''
            desc = item.get('description') or ''
            url = item.get('url') or ''
            if url and url.startswith('/'):
                url = urljoin('https://www.sandiegoreader.com', url)
            ev = normalize_event(
                title=name,
                date=start,
                url=url or 'https://www.sandiegoreader.com/events/',
                source='reader',
                description=desc,
                venue=venue,
                time_text=str(item.get('startDate') or ''),
            )
            if ev:
                events.append(ev)
    return events

def dedupe(events: list[Event]) -> list[Event]:
    chosen: dict[tuple[str, str], Event] = {}
    priority = {'family': 5, 'kids': 5, 'city': 4, 'reader': 3, 'kpbs': 2}
    for ev in sorted(events, key=lambda e: (e.date, -e.score, e.title.lower())):
        key = (re.sub(r'[^a-z0-9]+', ' ', ev.title.lower()).strip()[:56], ev.date)
        old = chosen.get(key)
        if old is None:
            chosen[key] = ev
            continue
        if (ev.score, priority.get(ev.source, 0)) > (old.score, priority.get(old.source, 0)):
            chosen[key] = ev
    return sorted(chosen.values(), key=lambda e: (e.date, -e.score, e.title.lower()))


def day_summary(events: list[Event]) -> str:
    if not events:
        return 'No strong family matches yet.'
    free_n = sum(1 for e in events if e.is_free)
    cats = Counter(e.category for e in events if e.category)
    top_titles = ', '.join(e.title for e in events[:2])
    pieces = [f'{len(events)} ideas']
    if free_n:
        pieces.append(f'{free_n} free')
    if cats:
        label, count = cats.most_common(1)[0]
        pieces.append(f'{count} {label.lower()}')
    if top_titles:
        pieces.append(top_titles)
    return ' · '.join(pieces)


def build_payload(events: list[Event], errors: list[str], fetched_at: str) -> dict:
    today = NOW().date()
    days: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        try:
            d = datetime.fromisoformat(ev.date).date()
        except Exception:
            continue
        if d < today or d > today + timedelta(days=DAY_WINDOW - 1):
            continue
        days[ev.date].append(ev)
    calendar = []
    for offset in range(DAY_WINDOW):
        d = today + timedelta(days=offset)
        iso = d.isoformat()
        entries = sorted(days.get(iso, []), key=lambda e: (-e.score, e.title.lower()))
        calendar.append({
            'date': iso,
            'weekday': d.strftime('%a'),
            'label': d.strftime('%b %-d') if hasattr(d, 'strftime') else iso,
            'is_today': offset == 0,
            'count': len(entries),
            'summary': day_summary(entries),
            'events': [asdict(e) for e in entries[:12]],
        })
    today_block = calendar[0] if calendar else {'events': [], 'summary': 'No data'}
    all_categories = Counter(e.category for e in events if e.category)
    return {
        'ok': True,
        'generated_at': fetched_at,
        'sources': [SOURCE_LABELS[s] for s in SOURCE_LABELS],
        'errors': errors,
        'today': today_block,
        'calendar': calendar,
        'counts': {
            'total_events': len(events),
            'days': len(calendar),
            'free_events': sum(1 for e in events if e.is_free),
        },
        'top_categories': [{'name': k, 'count': v} for k, v in all_categories.most_common(6)],
    }


def refresh_cache() -> dict:
    fetched_at = NOW().isoformat(timespec='seconds')
    errors: list[str] = []
    events: list[Event] = []
    sources = [
        ('city', 'https://www.sandiego.gov/events', parse_city_events),
        ('family', 'https://www.sandiegofamily.com/things-to-do/events-calendar', parse_family_calendar),
        ('kids', 'https://sandiego.kidsoutandabout.com/', parse_kidsoutandabout),
        ('kpbs', 'https://www.kpbs.org/events/all', parse_kpbs),
        ('reader', 'https://www.sandiegoreader.com/events/', parse_reader_events),
    ]
    for key, url, parser in sources:
        try:
            html = fetch(url)
            events.extend(parser(html))
        except Exception as e:
            errors.append(f'{SOURCE_LABELS[key]}: {e}')
    events = dedupe(events)
    payload = build_payload(events, errors, fetched_at)
    CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return payload


def read_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding='utf-8'))
    except Exception:
        return None


def is_stale(payload: dict | None) -> bool:
    if not payload:
        return True
    ts = payload.get('generated_at')
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PT)
    except Exception:
        return True
    return NOW() - dt > timedelta(hours=CACHE_HOURS)


def public_events_payload(force: bool = False) -> dict:
    cached = read_cache()
    if force or is_stale(cached):
        try:
            return refresh_cache()
        except Exception as e:
            if cached:
                cached = dict(cached)
                cached['ok'] = True
                cached.setdefault('errors', []).append(f'Live refresh failed: {e}')
                cached['stale'] = True
                return cached
            return {'ok': False, 'error': f'family events refresh failed: {e}'}
    return cached or {'ok': False, 'error': 'family events cache missing'}


if __name__ == '__main__':
    import sys
    data = public_events_payload(force='refresh' in sys.argv)
    if '--json' in sys.argv or 'refresh' in sys.argv or len(sys.argv) == 1:
        print(json.dumps(data, indent=2))
