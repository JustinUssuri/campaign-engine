"""公开地图的版本快照与候选索引；绝不登记本团实体、到访或里程。"""
import hashlib
import html
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from urllib import error, parse, request

from .store import ROOT, cached, locked, read_text

DIRECTORY = 'reference/maps/frwiki-faerun'
HOST = 'forgottenrealms.fandom.com'
TITLE = 'Map:Faerûn – Full Map'
MAP_URL = 'https://forgottenrealms.fandom.com/wiki/Map:Faer%C3%BBn_%E2%80%93_Full_Map'
MAP_PAGE_ID = 147186
IMAGE_PAGE_ID = 31276
PINNED_REVISION = 1071745
PINNED_IMAGE_REVISION = 1058020
MAX_RESPONSE = 20 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _directory():
    return ROOT / DIRECTORY


def _reader():
    return _directory() / 'index.json'


def load_index() -> dict:
    """仅查已导入快照；缺失时返回空索引，不联网、不写盘。"""
    path = _reader()
    def build():
        raw = read_text(path)
        if not raw:
            return {'source': {}, 'categories': [], 'candidates': []}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            raise SystemExit('地图候选索引损坏，请核对快照；未重新联网或覆盖') from None
        if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('candidates'), list):
            raise SystemExit('地图候选索引版本或结构不正确')
        unsigned = {key: val for key, val in data.items() if key not in ('content_sha256', 'snapshot', 'imported_at')}
        if data.get('content_sha256') != _hash(unsigned):
            raise SystemExit('地图候选索引摘要不符，请核对原始快照；未自动覆盖')
        return data
    return cached(('setting-map-index', str(path)), [path], build)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _revision(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9]\d*', str(value)):
        raise SystemExit('地图修订号须为正整数')
    return int(value)


def _supported_url(url):
    try:
        parsed = parse.urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname == HOST and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password
                 and parse.unquote(parsed.path).replace('_', ' ') == '/wiki/' + TITLE)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SystemExit('仅支持 Forgotten Realms Wiki 的 Faerûn – Full Map 公开地图网址')


def _fetch_api(params):
    """只请求固定公开 API；用户网址绝不成为任意网络请求地址。"""
    args = {'action': 'query', 'format': 'json', 'formatversion': 2, **params}
    url = f'https://{HOST}/api.php?' + parse.urlencode(args)
    req = request.Request(url, headers={'User-Agent': 'CampaignSettingIndex/1.0 (read-only public MediaWiki API)'})
    try:
        with request.urlopen(req, timeout=25) as response:
            if parse.urlsplit(response.geturl()).hostname != HOST:
                raise ValueError('接口跳转到不支持的域名')
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('接口响应过大')
            value = json.loads(raw)
    except (OSError, error.HTTPError, error.URLError, ValueError) as exc:
        raise SystemExit(f'地图未刷新，保留原快照：{exc}') from None
    query = value.get('query') if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get('error') or not isinstance(query, dict) or not isinstance(query.get('pages'), list):
        raise SystemExit('地图未刷新，保留原快照：公开接口没有返回有效页面')
    return value


def _pages(revision=None, refresh=False):
    args = {'prop': 'revisions|imageinfo', 'rvprop': 'ids|timestamp|content', 'rvslots': 'main',
            'iiprop': 'url|size|sha1|timestamp'}
    if refresh:
        args['pageids'] = f'{MAP_PAGE_ID}|{IMAGE_PAGE_ID}'
    else:
        args['revids'] = f'{_revision(revision or PINNED_REVISION)}|{PINNED_IMAGE_REVISION}'
    data = _fetch_api(args)
    pages = {page.get('pageid'): page for page in data['query']['pages'] if isinstance(page, dict)}
    if MAP_PAGE_ID not in pages or IMAGE_PAGE_ID not in pages:
        raise SystemExit('地图未刷新，保留原快照：地图或底图来源页面缺失')
    return pages[MAP_PAGE_ID], pages[IMAGE_PAGE_ID]


def _page_content(page):
    revisions = page.get('revisions')
    if not isinstance(revisions, list) or len(revisions) != 1 or not isinstance(revisions[0], dict):
        raise SystemExit('地图未刷新，保留原快照：修订数据不完整')
    rev = revisions[0]
    slots = rev.get('slots')
    main = slots.get('main') if isinstance(slots, dict) else None
    content = main.get('content') if isinstance(main, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise SystemExit('地图未刷新，保留原快照：修订正文缺失')
    _revision(rev.get('revid'))
    return rev, content


def _description(text):
    text = str(text or '')
    text = re.sub(r'<ref\b[^>]*>.*?</ref>|<ref\b[^>]*/>', '', text, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\[\[([^]|]+)\|([^]]+)\]\]', r'\2', text)
    text = re.sub(r'\[\[([^]]+)\]\]', r'\1', text)
    text = re.sub(r"'{2,}", '', text)
    return ' '.join(html.unescape(text).split())[:700]


def _article_url(link):
    if not isinstance(link, dict) or not isinstance(link.get('url'), str):
        return None
    value = link['url'].strip()
    parsed = parse.urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return value if parsed.scheme == 'https' and parsed.hostname == HOST and not parsed.username else None
    if not value or value.startswith(('/', '#')):
        return None
    return f'https://{HOST}/wiki/' + parse.quote(value.replace(' ', '_'), safe=':()_,–')


def normalize_map(map_page, image_page):
    """验证来源结构并保留原始坐标；标准 position 用左上原点，仅表示像素。"""
    if map_page.get('pageid') != MAP_PAGE_ID or image_page.get('pageid') != IMAGE_PAGE_ID:
        raise SystemExit('地图来源页面身份不正确')
    rev, raw = _page_content(map_page)
    image_rev, image_text = _page_content(image_page)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise SystemExit('地图未刷新，保留原快照：地图正文不是有效 JSON') from None
    if not isinstance(data, dict) or data.get('mapImage') != 'Map - Faerun - 3E.jpg':
        raise SystemExit('地图未刷新，保留原快照：底图身份不正确')
    bounds = data.get('mapBounds')
    if (not isinstance(bounds, list) or len(bounds) != 2 or bounds[0] != [0, 0]
            or not isinstance(bounds[1], list) or len(bounds[1]) != 2
            or not all(_number(n) and n > 0 for n in bounds[1])):
        raise SystemExit('地图未刷新，保留原快照：地图边界须为正的平面尺寸')
    width, height = bounds[1]
    if data.get('coordinateOrder') != 'xy' or data.get('origin') not in ('bottom-left', 'top-left'):
        raise SystemExit('地图未刷新，保留原快照：不支持的坐标顺序或原点')
    image = (image_page.get('imageinfo') or [{}])[0]
    if image.get('width') != width or image.get('height') != height:
        raise SystemExit('地图未刷新，保留原快照：底图尺寸与坐标边界不一致')
    categories = data.get('categories')
    markers = data.get('markers')
    if not isinstance(categories, list) or not isinstance(markers, list) or not markers:
        raise SystemExit('地图未刷新，保留原快照：分类或标记列表缺失')
    cats = {}
    for cat in categories:
        if (not isinstance(cat, dict) or not isinstance(cat.get('id'), str) or not cat['id']
                or not isinstance(cat.get('name'), str) or not cat['name'].strip() or cat['id'] in cats):
            raise SystemExit('地图未刷新，保留原快照：分类 ID 或名称不正确')
        cats[cat['id']] = cat['name'].strip()
    candidates = []
    ids = set()
    for marker in markers:
        if not isinstance(marker, dict):
            raise SystemExit('地图未刷新，保留原快照：标记须为对象')
        mid, cid = marker.get('id'), marker.get('categoryId')
        popup, pos = marker.get('popup'), marker.get('position')
        if (not isinstance(mid, str) or not mid or mid in ids or not isinstance(cid, str) or cid not in cats
                or not isinstance(popup, dict) or not isinstance(popup.get('title'), str) or not popup['title'].strip()
                or not isinstance(pos, list) or len(pos) != 2 or not all(_number(n) for n in pos)
                or not 0 <= pos[0] <= width or not 0 <= pos[1] <= height):
            raise SystemExit(f'地图未刷新，保留原快照：标记 {mid!r} 的身份、分类、标题或坐标不正确')
        ids.add(mid)
        x, y = pos
        candidates.append({'id': 'frwiki-faerun:' + mid, 'source_id': mid, 'title': popup['title'].strip(),
                           'category_id': cid, 'category': cats[cid], 'source_position': list(pos),
                           'position': [x, height - y if data['origin'] == 'bottom-left' else y],
                           'article_url': _article_url(popup.get('link')), 'description': _description(popup.get('description'))})
    candidates.sort(key=lambda row: (row['title'].casefold(), row['id']))
    source = {'id': 'frwiki-faerun', 'title': TITLE, 'url': MAP_URL, 'page_id': MAP_PAGE_ID,
              'revision': rev['revid'], 'revision_timestamp': rev.get('timestamp'),
              'permalink': f'https://{HOST}/index.php?oldid={rev["revid"]}',
              'map_image': data['mapImage'], 'image_page_id': IMAGE_PAGE_ID,
              'image_description_revision': image_rev['revid'], 'image_description_timestamp': image_rev.get('timestamp'),
              'image_url': image.get('url'), 'image_sha1': image.get('sha1'),
              'image_timestamp': image.get('timestamp'), 'width': width, 'height': height,
              'coordinate_order': 'xy', 'source_origin': data['origin'], 'index_origin': 'top-left',
              'coordinate_units': 'image_pixels', 'base_world_year': 1372, 'marker_era': 'mixed',
              'source_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
              'image_description_sha256': hashlib.sha256(image_text.encode('utf-8')).hexdigest(),
              'warning': '跨年代候选索引；不是1502年现状，不含经纬度、英里或可执行路线，不产生角色知情。'}
    result = {'schema_version': 1, 'source': source,
              'categories': [{'id': key, 'name': value} for key, value in cats.items()], 'candidates': candidates}
    result['content_sha256'] = _hash(result)
    return result


def _difference(old, new):
    before = {c['id']: c for c in old.get('candidates', [])}
    after = {c['id']: c for c in new['candidates']}
    return {'from_revision': old.get('source', {}).get('revision'), 'to_revision': new['source']['revision'],
            'added': sorted(after.keys() - before.keys()), 'removed': sorted(before.keys() - after.keys()),
            'changed': sorted(key for key in before.keys() & after.keys() if before[key] != after[key])}


def import_map(url, revision=None, *, refresh=False):
    """明确写入候选来源快照。固定修订可复读；刷新失败不会覆盖上次索引。"""
    from . import session, transaction
    if session.role() not in ('世界', '维护'):
        raise SystemExit('地图导入仅供世界或维护角色；候选查询不受此限制')
    _supported_url(url)
    if revision is not None:
        revision = _revision(revision)
    if refresh and revision is not None:
        raise SystemExit('指定修订号与 --refresh 不能同时使用')
    map_page, image_page = _pages(revision, refresh)
    index = normalize_map(map_page, image_page)
    expected = revision or PINNED_REVISION
    if not refresh and (index['source']['revision'] != expected
                        or index['source']['image_description_revision'] != PINNED_IMAGE_REVISION):
        raise SystemExit('地图未刷新，保留原快照：接口返回的修订与请求不一致')
    with locked():
        old = load_index()
        unchanged = old.get('content_sha256') == index['content_sha256']
        difference = _difference(old, index)
        if not unchanged:
            rev = index['source']['revision']
            image_rev = index['source']['image_description_revision']
            raw_data = {'map_page': map_page, 'image_page': image_page}
            snapshot = f'{DIRECTORY}/revision-{rev}-image-{image_rev}.json'
            path = ROOT / snapshot
            if path.exists():
                existing = json.loads(read_text(path))
                # API imageinfo 描述的是实际图像元数据，不把同一正文修订的后续图像元数据当正文变化。
                old_map_rev, old_map_raw = _page_content(existing['map_page'])
                _, new_map_raw = _page_content(map_page)
                _, old_image_raw = _page_content(existing['image_page'])
                _, new_image_raw = _page_content(image_page)
                if old_map_rev['revid'] != rev or old_map_raw != new_map_raw or old_image_raw != new_image_raw:
                    raise SystemExit('同修订号快照正文不一致，保留原资料供审核')
            index['snapshot'] = snapshot
            index['imported_at'] = datetime.now(timezone.utc).isoformat()
            source_note = (f'# Faerûn 候选地图来源\n\n'
                           f'- 地图：[Faerûn – Full Map]({MAP_URL})，修订 `{rev}`。\n'
                           f'- 底图：`Map - Faerun - 3E.jpg`，说明修订 `{image_rev}`；约1372 DR。\n'
                           f'- 标记：{len(index["candidates"])}；原图 {index["source"]["width"]}×{index["source"]["height"]}。\n'
                           '- 原始坐标按xy、左下原点保存；索引position转换为左上原点，单位仅为图像像素。\n'
                           '- 标记描述来自混合年代，必须逐项核对；地图索引不表示本团当前现状。\n'
                           '- 保留原始公开数据、来源及版本；未下载或重新发布底图图像。\n'
                           '- 本目录不登记本团实体、不修改玩家地图、不产生到访或知情，不提供英里或自动路线。\n')
            files = {f'{DIRECTORY}/index.json': _json(index), f'{DIRECTORY}/last-diff.json': _json(difference),
                     f'{DIRECTORY}/README.md': source_note}
            if not path.exists():
                files[snapshot] = _json(raw_data)
            transaction.mutate(files)
    return {'status': 'unchanged' if unchanged else 'imported', 'revision': index['source']['revision'],
            'image_revision': index['source']['image_description_revision'], 'count': len(index['candidates']),
            'sha256': index['content_sha256'], 'diff': difference,
            'message': '候选索引无变化' if unchanged else '已导入候选地图；未登记本团实体、到访、知情或路线'}


def _normal(value):
    return ' '.join(str(value).replace('_', ' ').casefold().split())


def search_candidates(query='', category=None, near=None, limit=20):
    """只读候选检索；near只表示图上像素邻近，不推断地理距离。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise SystemExit('候选查询 limit 须为1至200的整数')
    data = load_index()
    all_rows = data['candidates']
    needle = _normal(query)
    rows = [dict(c) for c in all_rows if (not needle or needle in _normal(c['title']) or needle == c['id'])
            and (category is None or _normal(category) in (_normal(c['category']), _normal(c['category_id'])))]
    if near is not None:
        anchor = near
        if isinstance(near, str):
            found = [c for c in all_rows if near == c['id'] or _normal(near) == _normal(c['title'])]
            if len(found) != 1:
                raise SystemExit('near 须唯一指定已导入候选的名称或ID')
            anchor = found[0]['position']
        if not isinstance(anchor, (list, tuple)) or len(anchor) != 2 or not all(_number(n) for n in anchor):
            raise SystemExit('near 须为候选名称、ID或两个有限像素坐标')
        for row in rows:
            row['pixel_distance'] = math.dist(row['position'], anchor)
        rows.sort(key=lambda c: (c['pixel_distance'], _normal(c['title']), c['id']))
    else:
        rows.sort(key=lambda c: (_normal(c['title']) != needle, _normal(c['title']), c['id']))
    return rows[:limit]


def show_map() -> dict:
    data = load_index()
    if not data['source']:
        return {'status': 'missing', 'message': '尚未导入候选地图；查询不自动联网或生成资料'}
    return {'status': 'available', 'source': data['source'], 'count': len(data['candidates']),
            'categories': dict(Counter(row['category'] for row in data['candidates'])),
            'snapshot': data.get('snapshot')}


def read_diff():
    path = _directory() / 'last-diff.json'
    return json.loads(read_text(path)) if path.exists() else {'message': '尚无地图导入差异'}


def add_parser(sub):
    parser = sub.add_parser('map', help='候选地图导入和只读检索')
    actions = parser.add_subparsers(dest='map_action', required=True)
    imp = actions.add_parser('import')
    imp.add_argument('map_url')
    imp.add_argument('--revision', dest='map_revision', type=int)
    imp.add_argument('--refresh', dest='map_refresh', action='store_true')
    search = actions.add_parser('search')
    search.add_argument('map_query', nargs='?', default='')
    search.add_argument('--category', dest='map_category')
    search.add_argument('--near', dest='map_near')
    search.add_argument('--limit', dest='map_limit', type=int, default=20)
    for p in (imp, search, actions.add_parser('show'), actions.add_parser('diff')):
        p.add_argument('--json', dest='map_json', action='store_true')
    return parser


def writes(args):
    return getattr(args, 'map_action', None) == 'import'


def cmd(state, args, payload=None):
    del state, payload
    if args.map_action == 'import':
        result = import_map(args.map_url, args.map_revision, refresh=args.map_refresh)
    elif args.map_action == 'search':
        result = search_candidates(args.map_query, args.map_category, args.map_near, args.map_limit)
    elif args.map_action == 'diff':
        result = read_diff()
    else:
        result = show_map()
    if getattr(args, 'map_json', False):
        return _json(result).rstrip()
    if isinstance(result, list):
        if not result:
            return '没有匹配的候选地图点；候选查询未登记本团实体'
        return '\n'.join(f'{r["id"]}｜{r["title"]}｜{r["category"]}｜图上像素{r["position"]}'
                         + (f'｜{r["article_url"]}' if r.get('article_url') else '') for r in result)
    if args.map_action == 'import':
        return f'{result["message"]}｜修订{result["revision"]}｜{result["count"]}个候选标记'
    if args.map_action == 'diff':
        if 'message' in result:
            return result['message']
        return (f'地图修订{result["from_revision"]}→{result["to_revision"]}｜'
                f'新增{len(result["added"])}、移除{len(result["removed"])}、改变{len(result["changed"])}；本团事实未覆盖')
    if result['status'] == 'missing':
        return result['message']
    source = result.get('source')
    if not isinstance(source, dict):
        raise SystemExit('地图来源摘要结构不正确')
    return (f'Faerûn候选索引｜修订{source["revision"]}｜{result["count"]}个标记\n'
            + str(source['warning']))
