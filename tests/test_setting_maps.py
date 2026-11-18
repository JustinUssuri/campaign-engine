"""地图导入只建立参考候选；网络用合成公开API回应，写入只在tmp目录。"""
import argparse
import copy
import json
from contextlib import nullcontext

import pytest

from campaign_engine import setting_maps as M

FETCH_API = M._fetch_api


def pages(markers=None, revision=M.PINNED_REVISION, origin='bottom-left'):
    data = {'mapImage': 'Map - Faerun - 3E.jpg', 'coordinateOrder': 'xy', 'origin': origin,
            'mapBounds': [[0, 0], [100, 60]], 'categories': [{'id': '1', 'name': 'Cities'}, {'id': '2', 'name': 'Forests'}],
            'markers': markers if markers is not None else [
                {'id': '1', 'categoryId': '1', 'position': [20, 10],
                 'popup': {'title': 'Alpha', 'description': "The '''old''' [[City|city]]<br/> &amp; port.",
                           'link': {'url': 'Alpha City'}}},
                {'id': '2', 'categoryId': '2', 'position': [25, 15],
                 'popup': {'title': 'Beta Wood', 'description': 'Some trees.', 'link': {'url': 'Beta Wood'}}}]}
    return ({'pageid': M.MAP_PAGE_ID, 'title': M.TITLE, 'revisions': [{'revid': revision, 'timestamp': '2026-01-01T00:00:00Z',
             'slots': {'main': {'content': json.dumps(data, ensure_ascii=False)}}}]},
            {'pageid': M.IMAGE_PAGE_ID, 'title': 'File:Map - Faerun - 3E.jpg',
             'revisions': [{'revid': M.PINNED_IMAGE_REVISION, 'timestamp': '2025-11-04T00:00:00Z',
                            'slots': {'main': {'content': 'A map circa [[1372 DR]].'}}}],
             'imageinfo': [{'width': 100, 'height': 60, 'url': 'https://static.wikia.nocookie.net/map.jpg',
                            'sha1': 'image-sha', 'timestamp': '2012-01-01T00:00:00Z'}]})


@pytest.fixture
def sample(tmp_path, monkeypatch):
    from campaign_engine import session, transaction
    monkeypatch.setattr(M, 'ROOT', tmp_path)
    monkeypatch.setattr(M, 'locked', nullcontext)
    monkeypatch.setattr(session, 'role', lambda: '维护')
    def mutate(files):
        for path, text in files.items():
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
    monkeypatch.setattr(transaction, 'mutate', mutate)
    mp, ip = pages()
    monkeypatch.setattr(M, '_fetch_api', lambda args: {'query': {'pages': [mp, ip]}})
    return tmp_path, mp, ip


def test_empty_reads_do_not_network_write_or_register(tmp_path, monkeypatch):
    monkeypatch.setattr(M, 'ROOT', tmp_path)
    monkeypatch.setattr(M, '_fetch_api', lambda args: pytest.fail('read must not access network'))
    assert M.search_candidates('No place') == []
    assert M.show_map()['status'] == 'missing'
    assert '尚无' in M.read_diff()['message']
    assert list(tmp_path.iterdir()) == []


def test_import_exact_revision_preserves_coordinates_source_and_raw_snapshot(sample):
    root, _, _ = sample
    before = sorted(root.rglob('*'))
    result = M.import_map(M.MAP_URL)
    assert result['status'] == 'imported' and result['count'] == 2
    data = M.load_index()
    assert data['source']['revision'] == M.PINNED_REVISION
    assert data['source']['image_description_revision'] == M.PINNED_IMAGE_REVISION
    assert data['source']['base_world_year'] == 1372 and data['source']['marker_era'] == 'mixed'
    row = M.search_candidates('Alpha')[0]
    assert row['source_position'] == [20, 10] and row['position'] == [20, 50]
    assert row['article_url'] == 'https://forgottenrealms.fandom.com/wiki/Alpha_City'
    assert row['description'] == 'The old city & port.'
    assert (root / data['snapshot']).exists()
    assert (root / M.DIRECTORY / 'README.md').exists()
    assert len(result['diff']['added']) == 2
    assert before == []
    assert not (root / 'state.yaml').exists() and not (root / 'sessions').exists()
    assert not (root / 'maps').exists() and not (root / 'gm').exists()


def test_repeated_import_is_byte_identical_and_keeps_initial_diff(sample):
    root, _, _ = sample
    M.import_map(M.MAP_URL)
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    second = M.import_map(M.MAP_URL, M.PINNED_REVISION)
    assert second['status'] == 'unchanged'
    after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert before == after
    assert len(M.read_diff()['added']) == 2


def test_explicit_refresh_records_add_remove_change_without_overwriting_prior_revision(sample, monkeypatch):
    root, mp, ip = sample
    M.import_map(M.MAP_URL)
    old_snapshot = root / M.load_index()['snapshot']
    original = old_snapshot.read_bytes()
    updated = json.loads(mp['revisions'][0]['slots']['main']['content'])
    updated['markers'][0]['popup']['title'] = 'Alpha Changed'
    updated['markers'].pop()
    updated['markers'].append({'id': '3', 'categoryId': '1', 'position': [50, 40], 'popup': {'title': 'Gamma'}})
    mp['revisions'][0]['slots']['main']['content'] = json.dumps(updated)
    mp['revisions'][0]['revid'] += 1
    captured = []
    monkeypatch.setattr(M, '_fetch_api', lambda args: (captured.append(args) or {'query': {'pages': [mp, ip]}}))
    result = M.import_map(M.MAP_URL, refresh=True)
    assert result['status'] == 'imported'
    assert result['diff']['added'] == ['frwiki-faerun:3']
    assert result['diff']['removed'] == ['frwiki-faerun:2']
    assert result['diff']['changed'] == ['frwiki-faerun:1']
    assert captured[0]['pageids'] == f'{M.MAP_PAGE_ID}|{M.IMAGE_PAGE_ID}'
    assert 'revids' not in captured[0]
    assert old_snapshot.read_bytes() == original
    assert M.search_candidates('Alpha')[0]['title'] == 'Alpha Changed'


def test_failed_refresh_preserves_every_reference_byte(sample, monkeypatch):
    root, _, _ = sample
    M.import_map(M.MAP_URL)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    def fail(args):
        raise SystemExit('地图未刷新，保留原快照：测试断网')
    monkeypatch.setattr(M, '_fetch_api', fail)
    with pytest.raises(SystemExit, match='未刷新'):
        M.import_map(M.MAP_URL, refresh=True)
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('url', [
    'http://forgottenrealms.fandom.com/wiki/Map:Faerûn_–_Full_Map',
    'https://evil.invalid/wiki/Map:Faerûn_–_Full_Map',
    'https://forgottenrealms.fandom.com.evil.invalid/wiki/Map:Faerûn_–_Full_Map',
    'https://user:password@forgottenrealms.fandom.com/wiki/Map:Faerûn_–_Full_Map',
    'https://forgottenrealms.fandom.com:9999/wiki/Map:Faerûn_–_Full_Map',
    'https://forgottenrealms.fandom.com/wiki/SomeOtherMap',
    'file:///etc/passwd', 'https://127.0.0.1/', 'not a url',
])
def test_unsupported_urls_fail_before_network_and_write(sample, monkeypatch, url):
    root, _, _ = sample
    monkeypatch.setattr(M, '_fetch_api', lambda args: pytest.fail('unsupported URL reached network'))
    with pytest.raises(SystemExit, match='仅支持'):
        M.import_map(url)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize('value', [0, -1, 'hello', 1.2, True])
def test_revision_validation(sample, value):
    with pytest.raises(SystemExit, match='修订号'):
        M.import_map(M.MAP_URL, revision=value)


def test_revision_identity_and_conflicting_options_are_checked(sample):
    root, mp, _ = sample
    with pytest.raises(SystemExit, match='不能同时'):
        M.import_map(M.MAP_URL, revision=123, refresh=True)
    with pytest.raises(SystemExit, match='修订与请求不一致'):
        M.import_map(M.MAP_URL, revision=123)
    assert list(root.iterdir()) == []
    assert mp['revisions'][0]['revid'] == M.PINNED_REVISION


@pytest.mark.parametrize('pos', [[-1, 0], [0, -1], [101, 0], [0, 61], [True, 1], [float('nan'), 1], [1], ['1', 1]])
def test_bad_coordinate_or_duplicate_marker_rejected_without_write(sample, pos):
    root, mp, _ = sample
    data = json.loads(mp['revisions'][0]['slots']['main']['content'])
    data['markers'][0]['position'] = pos
    mp['revisions'][0]['slots']['main']['content'] = json.dumps(data)
    with pytest.raises(SystemExit, match='坐标不正确'):
        M.import_map(M.MAP_URL)
    assert list(root.iterdir()) == []


def test_duplicate_ids_unknown_category_and_wrong_image_dimensions_fail(sample):
    root, mp, ip = sample
    original = mp['revisions'][0]['slots']['main']['content']
    for change in ('duplicate', 'category'):
        data = json.loads(original)
        if change == 'duplicate':
            data['markers'][1]['id'] = data['markers'][0]['id']
        else:
            data['markers'][0]['categoryId'] = 'missing'
        mp['revisions'][0]['slots']['main']['content'] = json.dumps(data)
        with pytest.raises(SystemExit, match='标记'):
            M.import_map(M.MAP_URL)
    mp['revisions'][0]['slots']['main']['content'] = original
    ip['imageinfo'][0]['width'] = 101
    with pytest.raises(SystemExit, match='尺寸'):
        M.import_map(M.MAP_URL)
    assert list(root.iterdir()) == []


def test_top_left_and_boundary_coordinates_are_supported():
    mp, ip = pages(markers=[{'id': '1', 'categoryId': '1', 'position': [100, 60], 'popup': {'title': 'Corner'}}], origin='top-left')
    data = M.normalize_map(mp, ip)
    assert data['candidates'][0]['position'] == [100, 60]
    assert data['candidates'][0]['source_position'] == [100, 60]


def test_search_exact_category_and_near_are_readonly_and_do_not_mutate_cache(sample):
    root, _, _ = sample
    M.import_map(M.MAP_URL)
    before = copy.deepcopy(M.load_index())
    bytes_before = M._reader().read_bytes()
    assert [x['title'] for x in M.search_candidates(category='Cities')] == ['Alpha']
    assert M.search_candidates('Alpha', near='frwiki-faerun:1')[0]['pixel_distance'] == 0
    near = M.search_candidates(near=[25, 45])
    assert [x['title'] for x in near] == ['Beta Wood', 'Alpha']
    assert M.load_index() == before and M._reader().read_bytes() == bytes_before
    assert not (root / 'sessions').exists()
    with pytest.raises(SystemExit, match='near'):
        M.search_candidates(near='missing')
    for limit in (0, 201, True, 1.1):
        with pytest.raises(SystemExit, match='limit'):
            M.search_candidates(limit=limit)


def test_import_role_guard_and_public_reads(sample, monkeypatch):
    from campaign_engine import session
    M.import_map(M.MAP_URL)
    monkeypatch.setattr(session, 'role', lambda: '主持')
    assert M.search_candidates('Alpha')
    with pytest.raises(SystemExit, match='世界或维护'):
        M.import_map(M.MAP_URL)


def test_pinned_request_does_not_fetch_latest(sample, monkeypatch):
    _, mp, ip = sample
    seen = []
    monkeypatch.setattr(M, '_fetch_api', lambda args: (seen.append(args) or {'query': {'pages': [mp, ip]}}))
    M.import_map(M.MAP_URL)
    assert seen[0]['revids'] == f'{M.PINNED_REVISION}|{M.PINNED_IMAGE_REVISION}'
    assert 'pageids' not in seen[0]


def test_same_revision_changed_body_cannot_overwrite_snapshot(sample):
    root, mp, _ = sample
    M.import_map(M.MAP_URL)
    before = M._reader().read_bytes()
    data = json.loads(mp['revisions'][0]['slots']['main']['content'])
    data['markers'][0]['popup']['title'] = 'Forged same revision'
    mp['revisions'][0]['slots']['main']['content'] = json.dumps(data)
    with pytest.raises(SystemExit, match='同修订号'):
        M.import_map(M.MAP_URL)
    assert M._reader().read_bytes() == before
    assert (root / M.DIRECTORY / f'revision-{M.PINNED_REVISION}-image-{M.PINNED_IMAGE_REVISION}.json').exists()


def test_network_error_contract_keeps_existing_index(sample, monkeypatch):
    from urllib.error import URLError
    M.import_map(M.MAP_URL)
    before = M._reader().read_bytes()
    def fail(req, timeout):
        raise URLError('offline')
    monkeypatch.setattr(M.request, 'urlopen', fail)
    with pytest.raises(SystemExit, match='未刷新.*保留原快照'):
        FETCH_API({})
    assert before == M._reader().read_bytes()


def test_index_digest_detects_unreviewed_candidate_edit(sample):
    M.import_map(M.MAP_URL)
    data = json.loads(M._reader().read_text())
    data['candidates'][0]['title'] = 'silently changed'
    M._reader().write_text(json.dumps(data))
    with pytest.raises(SystemExit, match='摘要不符'):
        M.load_index()


def test_parser_and_text_commands(sample):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    M.add_parser(sub)
    args = parser.parse_args(['map', 'import', M.MAP_URL, '--revision', str(M.PINNED_REVISION)])
    assert M.writes(args)
    assert '2个候选标记' in M.cmd({}, args)
    args = parser.parse_args(['map', 'search', 'Alpha', '--json'])
    assert not M.writes(args)
    assert json.loads(M.cmd({}, args))[0]['title'] == 'Alpha'
    args = parser.parse_args(['map', 'show'])
    assert '1372' not in M.cmd({}, args) or '1502' in M.cmd({}, args)
    assert '候选索引' in M.cmd({}, args)
    args = parser.parse_args(['map', 'diff'])
    assert '新增2' in M.cmd({}, args)
