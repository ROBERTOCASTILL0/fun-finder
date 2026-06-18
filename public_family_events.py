#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    'times_of_sd': 'Times of San Diego Events',
    'tourism': 'San Diego Tourism Events',
    'eventbrite': 'Eventbrite San Diego',
    'meetup_general': 'Meetup San Diego',
    'county_parks': 'County Parks Events',
    'balboa': 'Balboa Park Events',
    'fleet': 'Fleet Science Center',
    'birch': 'Birch Aquarium',
    'museum_us': 'Museum of Us',
    'midway': 'USS Midway Events',
    'rady_shell': 'Rady Shell',
    'house_of_blues': 'House of Blues San Diego',
    'observatory': 'Observatory North Park',
    'music_box': 'Music Box San Diego',
    'humphreys': 'Humphreys Concerts',
    'petco_events': 'Petco Park Events',
    'resident_advisor': 'Resident Advisor',
    'edmtrain': 'EDMTrain',
    'discotech': 'Discotech',
    'nineteen_hz': '19hz Southern California',
    'padres': 'Padres Schedule',
    'sdfc': 'San Diego FC',
    'wave': 'San Diego Wave',
    'legion': 'San Diego Legion',
    'seals': 'San Diego Seals',
    'convention_center': 'Convention Center Calendar',
    'ucsd': 'UCSD Events',
    'sdsu': 'SDSU Events',
    'usd': 'USD Events',
    'point_loma': 'Point Loma Nazarene Events',
    'sdhumane': 'San Diego Humane Society',
    'meetup_dogs': 'Dog Meetup Search',
    'farm_bureau': 'San Diego Farmers Markets',
    'del_mar_fair': 'Del Mar Fair',
    'festival_listings': 'San Diego Festival Listings',
}
MAX_SOURCE_WORKERS = 8

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


def parse_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    text = clean_text(str(value))
    m = re.search(r'(20\d{2}-\d{2}-\d{2})', text)
    if m:
        return m.group(1)
    return parse_long_date(text) or parse_mmddyyyy(text)


def iter_nested_items(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from iter_nested_items(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_nested_items(value)


def is_event_type(value) -> bool:
    if isinstance(value, list):
        lowered = {str(x).lower() for x in value}
        return bool({'event', 'eventseries'} & lowered)
    return str(value).lower() in {'event', 'eventseries'}


def extract_location_text(value) -> str:
    if isinstance(value, list):
        parts = [extract_location_text(v) for v in value]
        return clean_text(', '.join(p for p in parts if p))
    if isinstance(value, dict):
        pieces = []
        if value.get('name'):
            pieces.append(str(value.get('name')))
        address = value.get('address')
        if isinstance(address, dict):
            for key in ('addressLocality', 'addressRegion', 'streetAddress'):
                piece = address.get(key)
                if piece:
                    pieces.append(str(piece))
        elif address:
            pieces.append(str(address))
        return clean_text(', '.join(dict.fromkeys(pieces)))
    return clean_text(str(value or ''))


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
    # Do not include the generic category label in the classification text; the
    # old default "Family outing" label caused all-ages/adult records to be
    # misclassified as kid/family events.
    text = clean_text(f'{title} {description} {venue}').lower()
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
    is_adult = audience == 'adult'
    metadata = {
        'audience': audience,
        'age_groups': sorted([g for g, ok in {
            'toddler': (not is_adult) and bool(re.search(toddler_re, text)),
            'kids': (not is_adult) and bool(re.search(kids_re, text)),
            'teens': (not is_adult) and bool(re.search(r'\b(teen|ages?\s*(1[0-9]|13\+))\b', text)),
            'adults': is_adult or bool(re.search(adult_re, text)),
        }.items() if ok]),
        'features': {
            'free': bool(is_free),
            'outdoor': outdoor,
            'indoor': indoor,
            # Only mark true when a source explicitly says dogs/pets are welcome.
            # A title like "Clifford the Big Red Dog" is not a dog-friendly venue signal.
            'dog_friendly': bool(re.search(r'\b(dog[- ]friendly|pet[- ]friendly|dogs? (are )?(welcome|allowed|permitted)|bring (your )?(dog|fido)|leashed dogs?|well[- ]behaved dogs|pup|puppy|pooch)\b', text)),
            'toddler_friendly': (not is_adult) and bool(re.search(toddler_re, text)),
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
    if metadata.get('audience') == 'adult':
        category = 'Adult outing'
    elif metadata.get('audience') == 'all_ages' and category == 'Family outing':
        category = 'All-ages event'
    elif metadata.get('audience') == 'young_children':
        category = 'Toddler-friendly'
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


def parse_jsonld_events(html: str, source_key: str, base_url: str) -> list[Event]:
    events: list[Event] = []
    scripts = re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I)
    for raw in scripts[:220]:
        raw = unescape(raw).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for item in iter_nested_items(data):
            if not isinstance(item, dict) or not is_event_type(item.get('@type')):
                continue
            title = clean_text(item.get('name') or item.get('headline') or '')
            date = parse_iso_date(item.get('startDate') or item.get('doorTime') or item.get('endDate'))
            if not title or not date:
                continue
            url = item.get('url') or base_url
            if isinstance(url, str) and url.startswith('/'):
                url = urljoin(base_url, url)
            venue = extract_location_text(item.get('location'))
            desc = item.get('description') or item.get('about') or ''
            if isinstance(desc, dict):
                desc = desc.get('name') or desc.get('description') or ''
            ev = normalize_event(
                title=title,
                date=date,
                url=str(url or base_url),
                source=source_key,
                description=clean_text(str(desc or '')),
                venue=venue,
                time_text=str(item.get('startDate') or item.get('doorTime') or ''),
            )
            if ev:
                events.append(ev)
    return events


def parse_tribe_events_html(html: str, source_key: str, base_url: str) -> list[Event]:
    events: list[Event] = []
    chunks = re.findall(
        r'<article[^>]*class="[^"]*tribe-events-calendar-list__event[^"]*"[\s\S]*?</article>',
        html,
        re.I,
    )
    for chunk in chunks[:180]:
        tm = re.search(
            r'tribe-events-calendar-list__event-title-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            chunk,
            re.S,
        )
        if not tm:
            continue
        title = clean_text(tm.group(2))
        date_match = re.search(r'tribe-events-calendar-list__event-datetime[^>]*datetime="([^"]+)"', chunk) or re.search(
            r'tribe-events-calendar-list__event-date-tag-datetime[^>]*datetime="([^"]+)"',
            chunk,
        )
        date = parse_iso_date(date_match.group(1) if date_match else None)
        if not title or not date:
            continue
        venue_match = re.search(r'tribe-events-calendar-list__event-venue-title[^>]*>(.*?)</', chunk, re.S)
        desc_match = re.search(r'tribe-events-calendar-list__event-description[^>]*>(.*?)</div>', chunk, re.S)
        time_match = re.search(r'tribe-events-calendar-list__event-datetime-wrapper[^>]*>(.*?)</div>', chunk, re.S)
        ev = normalize_event(
            title=title,
            date=date,
            url=urljoin(base_url, tm.group(1)),
            source=source_key,
            description=clean_text(desc_match.group(1)) if desc_match else '',
            venue=clean_text(venue_match.group(1)) if venue_match else '',
            time_text=clean_text(time_match.group(1)) if time_match else '',
        )
        if ev:
            events.append(ev)
    return events


def dedupe(events: list[Event]) -> list[Event]:
    chosen: dict[tuple[str, str], Event] = {}
    priority = {
        'family': 6,
        'kids': 6,
        'city': 5,
        'balboa': 5,
        'sdhumane': 4,
        'ucsd': 4,
        'meetup_dogs': 4,
        'reader': 3,
        'kpbs': 3,
        'meetup_general': 2,
    }
    for ev in sorted(events, key=lambda e: (e.date, -e.score, e.title.lower())):
        key = (re.sub(r'[^a-z0-9]+', ' ', ev.title.lower()).strip()[:56], ev.date)
        old = chosen.get(key)
        if old is None:
            chosen[key] = ev
            continue
        if (ev.score, priority.get(ev.source, 1)) > (old.score, priority.get(old.source, 1)):
            chosen[key] = ev
    return sorted(chosen.values(), key=lambda e: (e.date, -e.score, e.title.lower()))


def day_summary(events: list[Event]) -> str:
    if not events:
        return 'No strong San Diego matches yet.'
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


def build_source_definitions() -> list[dict]:
    return [
        {'key': 'reader', 'category': 'general_events', 'url': 'https://www.sandiegoreader.com/events/', 'parsers': ('reader',)},
        {'key': 'kpbs', 'category': 'general_events', 'url': 'https://www.kpbs.org/events/all', 'parsers': ('kpbs',)},
        {'key': 'times_of_sd', 'category': 'general_events', 'url': 'https://timesofsandiego.com/events/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'tourism', 'category': 'general_events', 'url': 'https://www.sandiego.org/explore/events.aspx', 'parsers': ('jsonld', 'tribe')},
        {'key': 'eventbrite', 'category': 'general_events', 'url': 'https://www.eventbrite.com/d/ca--san-diego/events/', 'parsers': ('jsonld',)},
        {'key': 'meetup_general', 'category': 'general_events', 'url': 'https://www.meetup.com/find/us--ca--san-diego/', 'parsers': ('jsonld',)},
        {'key': 'family', 'category': 'family_events', 'url': 'https://www.sandiegofamily.com/things-to-do/events-calendar', 'parsers': ('family',)},
        {'key': 'kids', 'category': 'family_events', 'url': 'https://sandiego.kidsoutandabout.com/', 'parsers': ('kids',)},
        {'key': 'city', 'category': 'family_events', 'url': 'https://www.sandiego.gov/events', 'parsers': ('city',)},
        {'key': 'county_parks', 'category': 'family_events', 'url': 'https://www.sdparks.org/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'balboa', 'category': 'museums', 'url': 'https://balboapark.org/events/', 'parsers': ('tribe', 'jsonld')},
        {'key': 'fleet', 'category': 'museums', 'url': 'https://www.fleetscience.org/events', 'parsers': ('jsonld', 'tribe')},
        {'key': 'birch', 'category': 'museums', 'url': 'https://aquarium.ucsd.edu/events', 'parsers': ('jsonld', 'tribe')},
        {'key': 'museum_us', 'category': 'museums', 'url': 'https://museumofus.org/events', 'parsers': ('jsonld', 'tribe')},
        {'key': 'midway', 'category': 'museums', 'url': 'https://www.midway.org/events/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'rady_shell', 'category': 'music_and_concerts', 'url': 'https://www.theshell.org/events/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'house_of_blues', 'category': 'music_and_concerts', 'url': 'https://www.houseofblues.com/sandiego/events', 'parsers': ('jsonld', 'tribe')},
        {'key': 'observatory', 'category': 'music_and_concerts', 'url': 'https://www.observatorysd.com/events', 'parsers': ('jsonld', 'tribe')},
        {'key': 'music_box', 'category': 'music_and_concerts', 'url': 'https://musicboxsd.com/events/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'humphreys', 'category': 'music_and_concerts', 'url': 'https://www.humphreysconcerts.com/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'petco_events', 'category': 'music_and_concerts', 'url': 'https://www.mlb.com/padres/tickets/events', 'parsers': ('jsonld',)},
        {'key': 'resident_advisor', 'category': 'nightlife', 'url': 'https://ra.co/events/us/sandiego', 'parsers': ('jsonld',)},
        {'key': 'edmtrain', 'category': 'nightlife', 'url': 'https://edmtrain.com/san-diego-ca', 'parsers': ('jsonld', 'tribe')},
        {'key': 'discotech', 'category': 'nightlife', 'url': 'https://discotech.me/san-diego/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'nineteen_hz', 'category': 'nightlife', 'url': 'https://19hz.info/eventlisting_LosAngeles.php', 'parsers': ('jsonld', 'tribe')},
        {'key': 'padres', 'category': 'sports', 'url': 'https://www.mlb.com/padres/schedule', 'parsers': ('jsonld',)},
        {'key': 'sdfc', 'category': 'sports', 'url': 'https://www.sandiegofc.com/schedule/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'wave', 'category': 'sports', 'url': 'https://sandiegowavefc.com/schedule/', 'parsers': ('jsonld',)},
        {'key': 'legion', 'category': 'sports', 'url': 'https://www.sdlegion.com/schedule/', 'parsers': ('jsonld',)},
        {'key': 'seals', 'category': 'sports', 'url': 'https://www.sealslax.com/schedule/', 'parsers': ('jsonld',)},
        {'key': 'convention_center', 'category': 'convention_center', 'url': 'https://www.visitsandiego.com/calendar', 'parsers': ('jsonld', 'tribe')},
        {'key': 'ucsd', 'category': 'universities', 'url': 'https://calendar.ucsd.edu/', 'parsers': ('jsonld',)},
        {'key': 'sdsu', 'category': 'universities', 'url': 'https://calendar.sdsu.edu/', 'parsers': ('jsonld',)},
        {'key': 'usd', 'category': 'universities', 'url': 'https://www.sandiego.edu/events/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'point_loma', 'category': 'universities', 'url': 'https://www.pointloma.edu/events', 'parsers': ('jsonld', 'tribe')},
        {'key': 'sdhumane', 'category': 'dog_friendly', 'url': 'https://www.sdhumane.org/events/', 'parsers': ('jsonld',)},
        {'key': 'meetup_dogs', 'category': 'dog_friendly', 'url': 'https://www.meetup.com/topics/dogs/us/ca/san_diego/', 'parsers': ('jsonld',)},
        {'key': 'farm_bureau', 'category': 'farmers_markets', 'url': 'https://www.sdfarmbureau.org/farmers-market/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'del_mar_fair', 'category': 'fairs_and_festivals', 'url': 'https://www.sdfair.com/', 'parsers': ('jsonld', 'tribe')},
        {'key': 'festival_listings', 'category': 'fairs_and_festivals', 'url': 'https://www.sandiego.org/explore/events/festivals.aspx', 'parsers': ('jsonld', 'tribe')},
    ]


def run_source_parser(parser_name: str, html: str, source_key: str, url: str) -> list[Event]:
    if parser_name == 'city':
        return parse_city_events(html)
    if parser_name == 'family':
        return parse_family_calendar(html)
    if parser_name == 'kids':
        return parse_kidsoutandabout(html)
    if parser_name == 'kpbs':
        return parse_kpbs(html)
    if parser_name == 'reader':
        return parse_reader_events(html)
    if parser_name == 'jsonld':
        return parse_jsonld_events(html, source_key, url)
    if parser_name == 'tribe':
        return parse_tribe_events_html(html, source_key, url)
    raise ValueError(f'unknown parser {parser_name!r}')


def fetch_source_result(source: dict) -> tuple[list[Event], dict]:
    key = source['key']
    url = source['url']
    status = {
        'key': key,
        'label': SOURCE_LABELS[key],
        'category': source['category'],
        'url': url,
        'status': 'unavailable',
        'count': 0,
    }
    try:
        html = fetch(url)
    except Exception as exc:
        status['detail'] = str(exc)[:160]
        return [], status

    last_detail = 'searched but no parseable upcoming events found'
    for parser_name in source['parsers']:
        try:
            parsed = run_source_parser(parser_name, html, key, url)
        except Exception as exc:
            last_detail = f'{parser_name}: {type(exc).__name__}: {exc}'
            continue
        if parsed:
            status.update({'status': 'loaded', 'count': len(parsed), 'parser': parser_name})
            return parsed, status
        last_detail = f'{parser_name}: no parseable upcoming events found'

    status.update({'status': 'no_events', 'detail': last_detail[:160], 'parser': source['parsers'][-1]})
    return [], status


def build_payload(events: list[Event], errors: list[str], fetched_at: str, source_status: list[dict] | None = None) -> dict:
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
    source_status = source_status or []
    loaded_sources = [s['label'] for s in source_status if s.get('status') == 'loaded' and s.get('count', 0) > 0]
    source_warnings = [s for s in source_status if s.get('status') != 'loaded']
    return {
        'ok': True,
        'generated_at': fetched_at,
        'sources': loaded_sources or sorted({e.source_label for e in events}),
        'errors': errors,
        'source_status': source_status,
        'source_warnings': source_warnings,
        'today': today_block,
        'calendar': calendar,
        'counts': {
            'total_events': len(events),
            'days': len(calendar),
            'free_events': sum(1 for e in events if e.is_free),
            'configured_sources': len(source_status),
            'loaded_sources': len(loaded_sources),
        },
        'top_categories': [{'name': k, 'count': v} for k, v in all_categories.most_common(8)],
    }


def refresh_cache() -> dict:
    fetched_at = NOW().isoformat(timespec='seconds')
    errors: list[str] = []
    source_defs = build_source_definitions()
    results_by_key: dict[str, tuple[list[Event], dict]] = {}

    with ThreadPoolExecutor(max_workers=min(MAX_SOURCE_WORKERS, len(source_defs))) as pool:
        future_map = {pool.submit(fetch_source_result, source): source for source in source_defs}
        for future in as_completed(future_map):
            source = future_map[future]
            try:
                results_by_key[source['key']] = future.result()
            except Exception as exc:
                results_by_key[source['key']] = (
                    [],
                    {
                        'key': source['key'],
                        'label': SOURCE_LABELS[source['key']],
                        'category': source['category'],
                        'url': source['url'],
                        'status': 'unavailable',
                        'count': 0,
                        'detail': f'{type(exc).__name__}: {exc}'[:160],
                    },
                )

    events: list[Event] = []
    source_status: list[dict] = []
    for source in source_defs:
        parsed, status = results_by_key.get(source['key'], ([], {
            'key': source['key'],
            'label': SOURCE_LABELS[source['key']],
            'category': source['category'],
            'url': source['url'],
            'status': 'unavailable',
            'count': 0,
            'detail': 'source result missing',
        }))
        events.extend(parsed)
        source_status.append(status)

    events = dedupe(events)
    payload = build_payload(events, errors, fetched_at, source_status)
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
