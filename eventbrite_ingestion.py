from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BRAVE_SEARCH_URL = 'https://api.search.brave.com/res/v1/web/search'
EVENTBRITE_EVENT_URL_TEMPLATE = 'https://www.eventbriteapi.com/v3/events/{event_id}?expand=venue,organizer,category,subcategory,ticket_availability'
DEFAULT_SOURCE_URL = 'https://www.eventbrite.com/d/ca--san-diego/events/'
DEFAULT_CACHE_PATH = Path('/opt/data/roberto-ui/data/eventbrite_event_cache.json')
DISCOVERY_COUNT = 20
DISCOVERY_QUERY_LIMIT = 2
DISCOVERY_WINDOW_DAYS = 21
DEFAULT_DETAIL_BUDGET = 24
HARD_DETAIL_BUDGET = 24
DEFAULT_CACHE_HOURS = 24
DEFAULT_CACHE_RETENTION_HOURS = 168
DEFAULT_TIMEOUT = 12
USER_AGENT = 'SanDiegoFunFinder/1.0 (+https://eventbrite.com; read-only public event discovery)'
CACHE_VERSION = 1
MAX_CACHE_RECORDS = 256
EVENT_ID_RE = re.compile(r'https?://(?:www\.)?eventbrite\.com/e/[^?#]*-(\d+)(?:[/?#]|$)', re.I)
COUNTY_CITY_ALLOWLIST = {
    'carlsbad',
    'chula vista',
    'coronado',
    'del mar',
    'el cajon',
    'encinitas',
    'escondido',
    'imperial beach',
    'la mesa',
    'lemon grove',
    'national city',
    'oceanside',
    'poway',
    'san diego',
    'san marcos',
    'santee',
    'solana beach',
    'vista',
    'alpine',
    'bonita',
    'bonsall',
    'borrego springs',
    'camp pendleton',
    'casa de oro',
    'fallbrook',
    'crest',
    'del dios',
    'descanso',
    'dulzura',
    'eucalyptus hills',
    'fairbanks ranch',
    'granite hills',
    'harbison canyon',
    'hidden meadows',
    'jacumba',
    'jacumba hot springs',
    'julian',
    'jamul',
    'lakeside',
    'la presa',
    'mount laguna',
    'pala',
    'pauma valley',
    'pine valley',
    'potrero',
    'rainbow',
    'ramona',
    'rancho santa fe',
    'san diego country estates',
    'santa ysabel',
    'spring valley',
    'tecate',
    'valley center',
    'warner springs',
    'winter gardens',
    'rancho penasquitos',
    'rancho peñasquitos',
    'san ysidro',
    'otay mesa',
    'carmel valley',
    'rancho bernardo',
    'point loma',
    'la jolla',
    'pacific beach',
    'ocean beach',
    'mission beach',
    'clairemont',
    'mira mesa',
    'tierrasanta',
    'serra mesa',
    'hillcrest',
    'north park',
    'south park',
    'normal heights',
    'city heights',
    'kearny mesa',
    'scripps ranch',
    'encanto',
    'lincoln park',
    'logan heights',
    'shelter island',
}

EVENTBRITE_SOUTH_BAY_LOCALITIES = {
    'bonita',
    'chula vista',
    'imperial beach',
    'la presa',
    'national city',
    'otay mesa',
    'san ysidro',
}

EVENTBRITE_EAST_COUNTY_LOCALITIES = {
    'alpine',
    'borrego springs',
    'casa de oro',
    'crest',
    'descanso',
    'dulzura',
    'el cajon',
    'eucalyptus hills',
    'granite hills',
    'harbison canyon',
    'jacumba',
    'jacumba hot springs',
    'jamul',
    'julian',
    'la mesa',
    'lakeside',
    'lemon grove',
    'mount laguna',
    'pine valley',
    'potrero',
    'ramona',
    'san diego country estates',
    'santa ysabel',
    'santee',
    'spring valley',
    'winter gardens',
}

EVENTBRITE_NORTH_COUNTY_LOCALITIES = {
    'bonsall',
    'camp pendleton',
    'carlsbad',
    'del mar',
    'encinitas',
    'escondido',
    'fairbanks ranch',
    'fallbrook',
    'hidden meadows',
    'oceanside',
    'pala',
    'pauma valley',
    'poway',
    'rainbow',
    'rancho santa fe',
    'san marcos',
    'solana beach',
    'valley center',
    'vista',
    'warner springs',
}

EVENTBRITE_BEACH_LOCALITIES = {
    'coronado',
    'la jolla',
    'mission beach',
    'ocean beach',
    'pacific beach',
    'point loma',
    'shelter island',
}

EVENTBRITE_BALBOA_LOCALITIES = {
    'balboa park',
}

EVENTBRITE_DOWNTOWN_LOCALITIES = {
    'barrio logan',
    'downtown',
    'downtown san diego',
    'embarcadero',
    'gaslamp',
    'gaslamp quarter',
    'kettner',
    'little italy',
    'petco park',
}

EVENTBRITE_SPECIFIC_VENUE_ADDRESS_PHRASES: tuple[tuple[str, str], ...] = (
    ('mission bay', 'beach'),
    ('pacific beach', 'beach'),
    ('ocean beach', 'beach'),
    ('mission beach', 'beach'),
    ('la jolla', 'beach'),
    ('point loma', 'beach'),
    ('shelter island', 'beach'),
    ('balboa park', 'balboa'),
    ('downtown', 'downtown'),
    ('gaslamp', 'downtown'),
    ('little italy', 'downtown'),
    ('east village', 'downtown'),
    ('marina', 'downtown'),
)

EVENTBRITE_SAN_DIEGO_NEIGHBORHOODS = {
    'carmel valley',
    'city heights',
    'clairemont',
    'encanto',
    'hillcrest',
    'kearny mesa',
    'lincoln park',
    'logan heights',
    'mira mesa',
    'normal heights',
    'north park',
    'rancho bernardo',
    'rancho penasquitos',
    'rancho peñasquitos',
    'scripps ranch',
    'serra mesa',
    'south park',
    'tierrasanta',
}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def cache_path() -> Path:
    return Path(os.environ.get('EVENTBRITE_EVENT_CACHE_PATH') or DEFAULT_CACHE_PATH)


def cache_ttl_hours() -> int:
    return _env_int('EVENTBRITE_CACHE_HOURS', DEFAULT_CACHE_HOURS, minimum=6, maximum=168)


def cache_retention_hours() -> int:
    return _env_int('EVENTBRITE_CACHE_RETENTION_HOURS', DEFAULT_CACHE_RETENTION_HOURS, minimum=24, maximum=336)


def detail_budget() -> int:
    requested = _env_int('EVENTBRITE_DETAIL_MAX', DEFAULT_DETAIL_BUDGET, minimum=1, maximum=HARD_DETAIL_BUDGET)
    return min(requested, HARD_DETAIL_BUDGET)


def request_timeout() -> int:
    return _env_int('EVENTBRITE_TIMEOUT_SECONDS', DEFAULT_TIMEOUT, minimum=3, maximum=30)


def _canonical_month_year_terms(now: datetime) -> list[str]:
    months = []
    for offset in (0, DISCOVERY_WINDOW_DAYS - 1):
        month = (now + timedelta(days=offset)).strftime('%B %Y')
        if month not in months:
            months.append(month)
    return months


def build_discovery_queries(now: datetime) -> list[str]:
    month_years = ' OR '.join(f'"{month}"' for month in _canonical_month_year_terms(now))
    city_query = (
        'site:eventbrite.com/e/ '
        '("San Diego" OR "City of San Diego" OR "Balboa Park" OR "Mission Bay") '
        f'({month_years})'
    )
    county_query = (
        'site:eventbrite.com/e/ '
        '("San Diego County" OR "Chula Vista" OR "Oceanside" OR "Escondido" OR '
        '"Carlsbad" OR "La Mesa" OR "Encinitas" OR "National City") '
        f'({month_years})'
    )
    return [city_query, county_query]


def _read_json_response(request: Request, *, timeout: int) -> Any:
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()
        encoding = (response.headers.get('Content-Encoding', '') or '').lower()
        if encoding == 'gzip':
            payload = gzip.decompress(payload)
    return json.loads(payload.decode('utf-8'))


def extract_eventbrite_ids(payload: dict[str, Any]) -> list[str]:
    urls = []
    web = payload.get('web') if isinstance(payload, dict) else None
    results = web.get('results') if isinstance(web, dict) else []
    for item in results or []:
        if isinstance(item, dict):
            url = item.get('url') or item.get('profile') or ''
        else:
            url = ''
        if not isinstance(url, str):
            continue
        match = EVENT_ID_RE.search(url)
        if match:
            urls.append(match.group(1))
    return list(dict.fromkeys(urls))


def _http_json(url: str, *, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = Request(url, headers=headers)
    payload = _read_json_response(request, timeout=timeout)
    return payload if isinstance(payload, dict) else {}


def discover_event_ids(now: datetime, *, timeout: int) -> tuple[list[str], dict[str, int]]:
    api_key = os.environ.get('BRAVE_SEARCH_API_KEY')
    if not api_key:
        raise RuntimeError('missing_brave_key')
    headers = {
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip',
        'User-Agent': USER_AGENT,
        'X-Subscription-Token': api_key,
    }
    discovered: list[str] = []
    query_count = 0
    for query in build_discovery_queries(now)[:DISCOVERY_QUERY_LIMIT]:
        params = urlencode({'q': query, 'count': DISCOVERY_COUNT, 'text_decorations': 'false'})
        payload = _http_json(f'{BRAVE_SEARCH_URL}?{params}', headers=headers, timeout=timeout)
        query_count += 1
        discovered.extend(extract_eventbrite_ids(payload))
    return list(dict.fromkeys(discovered)), {'discovery_calls': query_count}


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    candidate = value.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _event_start(detail: dict[str, Any]) -> datetime | None:
    start = detail.get('start') if isinstance(detail, dict) else None
    if not isinstance(start, dict):
        return None
    return _parse_iso_datetime(start.get('local')) or _parse_iso_datetime(start.get('utc'))


def _normalized_text(value: Any) -> str:
    text = str(value or '').strip().lower()
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return text.strip()


def _is_listed_live(detail: dict[str, Any]) -> bool:
    if detail.get('status') == 'canceled':
        return False
    listed = detail.get('listed')
    if listed is False:
        return False
    return True


def _within_window(detail: dict[str, Any], now: datetime) -> bool:
    start = _event_start(detail)
    if start is None:
        return False
    day = start.date()
    return now.date() <= day <= (now.date() + timedelta(days=DISCOVERY_WINDOW_DAYS - 1))


def _coordinates_in_county(latitude: Any, longitude: Any) -> bool:
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return False
    return 32.45 <= lat <= 33.52 and -117.62 <= lon <= -116.08


def _address_locality_candidates(address: dict[str, Any]) -> set[str]:
    candidates: set[str] = set()
    for key in ('city', 'localized_area_display'):
        value = _normalized_text(address.get(key))
        if value:
            candidates.add(value)
    multi_line = address.get('localized_multi_line_address_display')
    if isinstance(multi_line, str):
        for raw_part in re.split(r'[\n,;|]+', multi_line):
            value = _normalized_text(raw_part)
            if value:
                candidates.add(value)
    display = address.get('localized_address_display')
    if isinstance(display, str):
        first_part = _normalized_text(display.split(',', 1)[0])
        if first_part:
            candidates.add(first_part)
    return candidates



def _address_locality_values(address: dict[str, Any]) -> list[str]:
    values: list[str] = []

    def add(raw: Any) -> None:
        value = _normalized_text(raw)
        if value and value not in values:
            values.append(value)

    add(address.get('city'))
    add(address.get('localized_area_display'))

    multi_line = address.get('localized_multi_line_address_display')
    if isinstance(multi_line, str):
        for raw_part in re.split(r'[\n,;|]+', multi_line):
            add(raw_part)

    display = address.get('localized_address_display')
    if isinstance(display, str):
        for raw_part in re.split(r'[,;|]+', display):
            add(raw_part)

    return values



def _eventbrite_venue_address_specific_texts(detail: dict[str, Any], address: dict[str, Any]) -> list[str]:
    values: list[str] = []

    def add(raw: Any) -> None:
        value = _normalized_text(raw)
        if value and value not in values:
            values.append(value)

    venue = detail.get('venue') if isinstance(detail.get('venue'), dict) else None
    if isinstance(venue, dict):
        add(venue.get('name'))

    add(address.get('localized_address_display'))
    add(address.get('localized_multi_line_address_display'))
    add(address.get('localized_area_display'))
    add(address.get('city'))

    return values



def _eventbrite_specific_area_from_venue_address(detail: dict[str, Any], address: dict[str, Any]) -> str | None:
    for value in _eventbrite_venue_address_specific_texts(detail, address):
        for phrase, area in EVENTBRITE_SPECIFIC_VENUE_ADDRESS_PHRASES:
            if phrase in value:
                return area
    return None



def _eventbrite_authoritative_area_from_address(detail: dict[str, Any]) -> str | None:
    venue = detail.get('venue') if isinstance(detail.get('venue'), dict) else None
    address = venue.get('address') if isinstance(venue, dict) and isinstance(venue.get('address'), dict) else None
    if not isinstance(address, dict):
        return None

    locality_values = _address_locality_values(address)
    city = _normalized_text(address.get('city'))

    for locality in locality_values:
        if locality in EVENTBRITE_SOUTH_BAY_LOCALITIES:
            return 'south-bay'
        if locality in EVENTBRITE_EAST_COUNTY_LOCALITIES:
            return 'east-county'
        if locality in EVENTBRITE_NORTH_COUNTY_LOCALITIES:
            return 'north-county'

    specific_area = _eventbrite_specific_area_from_venue_address(detail, address)
    if specific_area is not None:
        return specific_area

    for locality in locality_values:
        if locality in EVENTBRITE_BEACH_LOCALITIES:
            return 'beach'
        if locality in EVENTBRITE_BALBOA_LOCALITIES or 'balboa park' in locality:
            return 'balboa'
        if locality in EVENTBRITE_DOWNTOWN_LOCALITIES or 'downtown' in locality:
            return 'downtown'
        if city == 'san diego' and locality in EVENTBRITE_SAN_DIEGO_NEIGHBORHOODS:
            return 'central-san-diego'

    if city == 'san diego' or 'san diego' in locality_values:
        return 'central-san-diego'

    return None



def _address_is_california(address: dict[str, Any]) -> bool:
    region = _normalized_text(address.get('region'))
    if region in {'ca', 'california'}:
        return True
    for key in ('localized_address_display', 'localized_multi_line_address_display'):
        value = _normalized_text(address.get(key))
        if re.search(r'\b(ca|california)\b', value):
            return True
    return False



def is_allowed_san_diego_county_event(detail: dict[str, Any]) -> bool:
    if not _is_listed_live(detail):
        return False
    if detail.get('online_event'):
        return False
    venue = detail.get('venue')
    if not isinstance(venue, dict):
        return False
    address = venue.get('address')
    if not isinstance(address, dict):
        return False
    if not _address_is_california(address):
        return False
    return any(candidate in COUNTY_CITY_ALLOWLIST for candidate in _address_locality_candidates(address))


def _format_time_text(detail: dict[str, Any]) -> str:
    start = _event_start(detail)
    if start is None:
        return ''
    return start.strftime('%a %b %-d, %-I:%M %p')


def _venue_text(detail: dict[str, Any]) -> str:
    venue = detail.get('venue') if isinstance(detail, dict) else None
    if not isinstance(venue, dict):
        return ''
    address = venue.get('address') if isinstance(venue.get('address'), dict) else {}
    parts = [venue.get('name') or '']
    for key in ('city', 'region'):
        value = address.get(key)
        if value:
            parts.append(str(value))
    return ', '.join(part for part in parts if part)


def _detail_keywords(detail: dict[str, Any]) -> tuple[str, list[str]]:
    parts: list[str] = []
    tags: list[str] = []
    for key in ('category', 'subcategory'):
        value = detail.get(key)
        if isinstance(value, dict):
            name = value.get('name') or value.get('short_name') or ''
            if isinstance(name, str) and name.strip():
                parts.append(name.strip())
                tags.append(_normalized_text(name).replace(' ', '-'))
    organizer = detail.get('organizer') if isinstance(detail.get('organizer'), dict) else {}
    organizer_name = organizer.get('name') if isinstance(organizer, dict) else ''
    if organizer_name:
        parts.append(str(organizer_name))
    return ' '.join(parts), [tag for tag in tags if tag]



def _bounded_clean_text(value: Any, limit: int) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text[:limit]



def _eventbrite_authoritative_is_free(detail: dict[str, Any]) -> bool | None:
    if isinstance(detail.get('is_free'), bool):
        return detail['is_free']
    ticket_availability = detail.get('ticket_availability')
    if isinstance(ticket_availability, dict) and isinstance(ticket_availability.get('is_free'), bool):
        return ticket_availability['is_free']
    return None



def _apply_eventbrite_metadata_overrides(event: Any, detail: dict[str, Any]) -> Any:
    metadata = dict(getattr(event, 'metadata', {}) or {})
    metadata['eventbrite_id'] = str(detail.get('id') or '')[:64]
    organizer = detail.get('organizer') if isinstance(detail.get('organizer'), dict) else {}
    organizer_name = organizer.get('name') if isinstance(organizer, dict) else ''
    metadata['organizer_name'] = _bounded_clean_text(organizer_name, 160)
    for field, metadata_key in (('category', 'eventbrite_category'), ('subcategory', 'eventbrite_subcategory')):
        payload = detail.get(field) if isinstance(detail.get(field), dict) else {}
        value = payload.get('name') or payload.get('short_name') or ''
        metadata[metadata_key] = _bounded_clean_text(value, 160)
    logo = detail.get('logo') if isinstance(detail.get('logo'), dict) else {}
    original = logo.get('original') if isinstance(logo, dict) else {}
    metadata['image_url'] = _bounded_clean_text(original.get('url') if isinstance(original, dict) else '', 500)

    authoritative_is_free = _eventbrite_authoritative_is_free(detail)
    if authoritative_is_free is not None:
        event.is_free = authoritative_is_free
        tags = [tag for tag in getattr(event, 'tags', []) if tag != 'free']
        if authoritative_is_free:
            tags.append('free')
        event.tags = sorted(set(tags))
        metadata['features'] = dict(metadata.get('features') or {})
        metadata['features']['free'] = authoritative_is_free

    authoritative_area = _eventbrite_authoritative_area_from_address(detail)
    if authoritative_area is not None:
        metadata['area'] = authoritative_area

    native_category = _normalized_text(metadata.get('eventbrite_category'))
    native_subcategory = _normalized_text(metadata.get('eventbrite_subcategory'))
    if native_category == 'business professional' or native_subcategory == 'networking':
        metadata['audience'] = 'adult'
        age_groups = set(metadata.get('age_groups') or [])
        age_groups.add('adults')
        metadata['age_groups'] = sorted(age_groups)
        event.category = 'Adult outing'
    elif native_category == 'family education':
        if metadata.get('audience') == 'all_ages':
            metadata['audience'] = 'family'
        if event.category == 'All-ages event':
            event.category = 'Family outing'

    event.metadata = metadata
    return event



def normalize_eventbrite_detail(
    detail: dict[str, Any],
    *,
    normalizer: Callable[..., Any],
    source_label: str,
    now: datetime,
):
    if not _is_listed_live(detail):
        return None
    if not _within_window(detail, now):
        return None
    if not is_allowed_san_diego_county_event(detail):
        return None
    name = detail.get('name') if isinstance(detail.get('name'), dict) else {}
    title = (name.get('text') if isinstance(name, dict) else '') or ''
    url = detail.get('url') or ''
    start = _event_start(detail)
    if not title or not url or start is None:
        return None
    keyword_text, extra_tags = _detail_keywords(detail)
    summary = _bounded_clean_text(detail.get('summary') or '', 600)
    description_payload = detail.get('description') if isinstance(detail.get('description'), dict) else {}
    full_description = _bounded_clean_text(description_payload.get('text') if isinstance(description_payload, dict) else '', 3000)
    description = ' '.join(part for part in [summary, full_description, keyword_text] if part).strip()
    venue = _venue_text(detail)
    authoritative_is_free = _eventbrite_authoritative_is_free(detail)
    event = normalizer(
        title,
        start.date().isoformat(),
        str(url),
        'eventbrite',
        str(description),
        venue,
        _format_time_text(detail),
        is_free_override=authoritative_is_free,
        minimum_score=-20,
    )
    if event is None:
        return None
    event.source_label = source_label
    event.tags = sorted(set(event.tags + extra_tags + ['eventbrite']))
    return _apply_eventbrite_metadata_overrides(event, detail)


def _load_cache(now: datetime) -> dict[str, Any]:
    path = cache_path()
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict):
            return {'version': CACHE_VERSION, 'events': {}}
    except Exception:
        return {'version': CACHE_VERSION, 'events': {}}
    payload.setdefault('version', CACHE_VERSION)
    events = payload.get('events')
    payload['events'] = events if isinstance(events, dict) else {}
    pruned = prune_cache(payload, now)
    if pruned != payload:
        _save_cache(pruned)
    return pruned


def prune_cache(cache: dict[str, Any], now: datetime) -> dict[str, Any]:
    retention = timedelta(hours=cache_retention_hours())
    max_records = _env_int('EVENTBRITE_CACHE_MAX_RECORDS', MAX_CACHE_RECORDS, minimum=24, maximum=MAX_CACHE_RECORDS)
    kept: list[tuple[str, dict[str, Any], datetime]] = []
    for event_id, record in (cache.get('events') or {}).items():
        if not isinstance(record, dict):
            continue
        detail = record.get('detail')
        fetched_at = _parse_iso_datetime(record.get('fetched_at'))
        if not isinstance(detail, dict) or fetched_at is None:
            continue
        if fetched_at + retention < now:
            continue
        if not _is_listed_live(detail):
            continue
        if not _within_window(detail, now):
            continue
        kept.append((str(event_id), {'fetched_at': fetched_at.isoformat(), 'detail': detail}, fetched_at))
    kept.sort(key=lambda item: item[2], reverse=True)
    return {'version': CACHE_VERSION, 'events': {event_id: record for event_id, record, _ in kept[:max_records]}}


def _save_cache(cache: dict[str, Any]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(cache, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _fresh_cache_records(cache: dict[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
    ttl = timedelta(hours=cache_ttl_hours())
    fresh: dict[str, dict[str, Any]] = {}
    for event_id, record in (cache.get('events') or {}).items():
        if not isinstance(record, dict):
            continue
        fetched_at = _parse_iso_datetime(record.get('fetched_at'))
        detail = record.get('detail')
        if fetched_at is None or not isinstance(detail, dict):
            continue
        if fetched_at + ttl < now:
            continue
        fresh[str(event_id)] = record
    return fresh


def _fetch_event_detail(event_id: str, *, timeout: int) -> dict[str, Any]:
    token = _eventbrite_private_token()
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}',
        'User-Agent': USER_AGENT,
    }
    url = EVENTBRITE_EVENT_URL_TEMPLATE.format(event_id=event_id)
    return _http_json(url, headers=headers, timeout=timeout)


def fetch_eventbrite_source_result(
    source: dict[str, Any],
    *,
    normalizer: Callable[..., Any],
    source_label: str,
    now: datetime,
) -> tuple[list[Any], dict[str, Any]]:
    timeout = request_timeout()
    status: dict[str, Any] = {
        'key': 'eventbrite',
        'label': source_label,
        'category': source.get('category', 'general_events'),
        'url': source.get('url', DEFAULT_SOURCE_URL),
        'status': 'unavailable',
        'count': 0,
        'parser': 'eventbrite_api',
    }
    cache = _load_cache(now)
    retained_cache = dict(cache.get('events') or {})
    fresh_cache = _fresh_cache_records(cache, now)
    metrics = {
        'discovery_calls': 0,
        'discovered_ids': 0,
        'cache_hits': 0,
        'detail_calls': 0,
        'accepted': 0,
        'rate_limited': False,
        'config_error': '',
    }
    try:
        discovered_ids, discovery_metrics = discover_event_ids(now, timeout=timeout)
        metrics.update(discovery_metrics)
        metrics['discovered_ids'] = len(discovered_ids)
    except Exception as exc:
        cached_events = []
        for record in retained_cache.values():
            event = normalize_eventbrite_detail(record['detail'], normalizer=normalizer, source_label=source_label, now=now)
            if event is not None:
                cached_events.append(event)
        cached_events = _dedupe_by_source_id(cached_events)
        cache = _rewrite_cache_from_events(cache, retained_cache, now)
        _save_cache(cache)
        if cached_events:
            status.update({
                'status': 'loaded',
                'count': len(cached_events),
                'detail': _bounded_detail(f'cache_fallback discovery_error={type(exc).__name__} cache_hits={len(cached_events)}'),
            })
            return cached_events, status
        status['detail'] = _bounded_detail(f'discovery_error={type(exc).__name__}')
        return [], status

    accepted_events: list[Any] = []
    for event_id, record in retained_cache.items():
        event = normalize_eventbrite_detail(record['detail'], normalizer=normalizer, source_label=source_label, now=now)
        if event is not None:
            accepted_events.append(event)
            metrics['cache_hits'] += 1
    uncached_ids = [event_id for event_id in discovered_ids if event_id not in fresh_cache]
    if uncached_ids:
        try:
            _eventbrite_private_token()
        except RuntimeError as exc:
            metrics['config_error'] = str(exc)
            accepted_events = _dedupe_by_source_id(accepted_events)
            metrics['accepted'] = len(accepted_events)
            cache = _rewrite_cache_from_events(cache, retained_cache, now)
            _save_cache(cache)
            if accepted_events:
                detail = _bounded_detail(
                    ' '.join(
                        part
                        for part in [
                            f'discovery_calls={metrics["discovery_calls"]}',
                            f'discovered_ids={metrics["discovered_ids"]}',
                            f'cache_hits={metrics["cache_hits"]}',
                            f'detail_calls={metrics["detail_calls"]}',
                            f'accepted={metrics["accepted"]}',
                            f'config_error={metrics["config_error"]}',
                        ]
                        if part
                    )
                )
                status.update({'status': 'loaded', 'count': len(accepted_events), 'detail': detail})
                return accepted_events, status
            status.update({'status': 'unavailable', 'detail': _bounded_detail(str(exc))})
            return [], status
    for event_id in uncached_ids[:detail_budget()]:
        metrics['detail_calls'] += 1
        try:
            detail = _fetch_event_detail(event_id, timeout=timeout)
        except HTTPError as exc:
            if exc.code == 429:
                metrics['rate_limited'] = True
                break
            continue
        except Exception:
            continue
        retained_cache[event_id] = {'fetched_at': now.isoformat(), 'detail': detail}
        event = normalize_eventbrite_detail(detail, normalizer=normalizer, source_label=source_label, now=now)
        if event is not None:
            accepted_events.append(event)
    cache = _rewrite_cache_from_events(cache, retained_cache, now)
    _save_cache(cache)
    accepted_events = _dedupe_by_source_id(accepted_events)
    metrics['accepted'] = len(accepted_events)
    detail = _bounded_detail(
        ' '.join(
            part
            for part in [
                f'discovery_calls={metrics["discovery_calls"]}',
                f'discovered_ids={metrics["discovered_ids"]}',
                f'cache_hits={metrics["cache_hits"]}',
                f'detail_calls={metrics["detail_calls"]}',
                f'accepted={metrics["accepted"]}',
                'rate_limited=1' if metrics['rate_limited'] else '',
                f'config_error={metrics["config_error"]}' if metrics['config_error'] else '',
            ]
            if part
        )
    )
    if accepted_events:
        status.update({'status': 'loaded', 'count': len(accepted_events), 'detail': detail})
        return accepted_events, status
    status.update({'status': 'no_events', 'detail': detail})
    return [], status


def _rewrite_cache_from_events(cache: dict[str, Any], fresh_cache: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any]:
    merged = {'version': CACHE_VERSION, 'events': dict(cache.get('events') or {})}
    merged['events'].update(fresh_cache)
    return prune_cache(merged, now)


def _dedupe_by_source_id(events: list[Any]) -> list[Any]:
    chosen: dict[str, Any] = {}
    for event in events:
        metadata = getattr(event, 'metadata', {}) or {}
        key = str(metadata.get('eventbrite_id') or getattr(event, 'url', ''))
        chosen[key] = event
    return list(chosen.values())


def _bounded_detail(text: str) -> str:
    return text[:160]


def _eventbrite_private_token() -> str:
    token = os.environ.get('EVENTBRITE_PRIVATE_TOKEN')
    if not token:
        raise RuntimeError('missing_eventbrite_token')
    return token
