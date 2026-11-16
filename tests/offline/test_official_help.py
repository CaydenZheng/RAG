"""Offline regression checks for official search, URL reading and response replay."""

import json

import httpx
import pytest

from src.agent.official_help import OfficialHelp, canonical_help_url, extract_article

ARTICLE_URL = 'https://www.feishu.cn/hc/zh-CN/articles/123456'


def article_html():
    payload = {'_updateTime': 123, 'faqs': [
        {'question': 'Question one?', 'answer_txt': 'The answer.'},
        {'question': 'Question two?', 'answer_txt': 'The answer.'},
    ], 'body': [{'html': {'text': 'Article introduction.'}}]}
    return '<title>Article title</title><script>window._templateValue = ' + json.dumps(payload) + ';</script>'


@pytest.mark.parametrize('url', [
    'http://www.feishu.cn/hc/zh-CN/articles/123456',
    'https://www.feishu.cn.evil.example/hc/zh-CN/articles/123456',
    'https://user:password@www.feishu.cn/hc/zh-CN/articles/123456',
    'https://www.feishu.cn:8443/hc/zh-CN/articles/123456',
    'https://127.0.0.1/hc/zh-CN/articles/123456',
    'https://www.feishu.cn/api/private',
])
def test_reader_rejects_other_destinations(url, tmp_path):
    with pytest.raises(ValueError):
        canonical_help_url(url)
    reader = OfficialHelp(record_dir=tmp_path / 'records')
    assert reader.read({'url': url}).error_code == 'unsupported_document_url'


def test_canonical_url_and_question_answer_pairs():
    assert canonical_help_url(ARTICLE_URL + '-title?tracking=1#section') == ARTICLE_URL
    article = extract_article(article_html())
    assert article['sections'][:2] == ['Question one?\nThe answer.', 'Question two?\nThe answer.']
    assert article['source_updated_at'] == 123
    with pytest.raises(ValueError):
        extract_article('<html>login required</html>')


def test_search_records_actual_order_and_replays_exact_request(tmp_path):
    def handler(request):
        assert request.url.path == '/hc/api/article/search'
        assert request.url.params['query'] == 'storage'
        return httpx.Response(200, json={'code': 0, 'data': {'total': 1, 'list': [{
            'path': '/hc/zh-CN/articles/123456', 'title': {'name': 'Storage'},
            'content': {'abstract': 'Official snippet'}, 'update_time': 123,
        }]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reader = OfficialHelp(record_dir=tmp_path / 'records', client=client)
    response = reader.search({'query': 'storage'})
    assert response.success
    assert response.data['results'][0]['url'] == ARTICLE_URL
    assert response.data['results'][0]['rank'] == 1
    replay = OfficialHelp(record_dir=tmp_path / 'replayed', replay_dir=tmp_path / 'records')
    assert replay.search({'query': 'storage'}).data['results'] == response.data['results']
    assert replay.search({'query': 'unrecorded'}).error_code == 'official_replay_miss'


def test_read_pages_keep_source_ids_and_empty_match_is_explicit(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=article_html())))
    reader = OfficialHelp(record_dir=tmp_path / 'records', client=client)
    first = reader.read({'url': ARTICLE_URL, 'limit': 1})
    second = reader.read({'url': ARTICLE_URL, 'offset': first.data['next_offset'], 'limit': 1})
    assert first.data['contexts'][0]['document_id'] == 'hc-123456-p001'
    assert second.data['contexts'][0]['document_id'] == 'hc-123456-p002'
    assert second.data['contexts'][0]['url'] == ARTICLE_URL


def test_failures_are_preserved_and_faults_are_labeled(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403)))
    reader = OfficialHelp(record_dir=tmp_path / 'records', client=client)
    assert reader.read({'url': ARTICLE_URL}).error_code == 'official_http_403'
    assert len(list((tmp_path / 'records').glob('*.json'))) == 1
    reader = OfficialHelp(record_dir=tmp_path / 'fault', read_failure='official_timeout',
                          search_failure='official_timeout')
    assert reader.read({'url': ARTICLE_URL}).data['fault_injection']
    assert reader.search({'query': 'storage'}).data['fault_injection']


def test_query_read_preserves_full_article_when_no_keywords_match(tmp_path, monkeypatch):
    from src.utils.bm25_store import BM25Store

    monkeypatch.setattr(BM25Store, 'search', lambda *args, **kwargs: [])
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=article_html())))
    reader = OfficialHelp(record_dir=tmp_path / 'records', client=client)
    result = reader.read({'url': ARTICLE_URL, 'query': 'unmatched'})
    assert len(result.data['contexts']) == result.data['total_passages'] == 3
    assert result.data['next_offset'] is None
    assert result.data['paging_query'] == 'unmatched'


def test_corrupt_replay_is_rejected(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=article_html())))
    reader = OfficialHelp(record_dir=tmp_path / 'records', client=client)
    assert reader.read({'url': ARTICLE_URL}).success
    next((tmp_path / 'records').glob('*.body')).write_text('tampered', encoding='utf-8')
    replay = OfficialHelp(record_dir=tmp_path / 'replayed', replay_dir=tmp_path / 'records')
    assert replay.read({'url': ARTICLE_URL}).error_code == 'official_response_unreadable'


def test_article_slug_redirect_is_followed_but_external_redirect_is_rejected(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(301, headers={'location': ARTICLE_URL + '-title'})
        return httpx.Response(200, text=article_html())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reader = OfficialHelp(record_dir=tmp_path / 'records', client=client)
    assert reader.read({'url': ARTICLE_URL}).success
    assert calls == [ARTICLE_URL, ARTICLE_URL + '-title']
    client = httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(302, headers={'location': 'https://example.com/private'}),
    ))
    reader = OfficialHelp(record_dir=tmp_path / 'external', client=client)
    assert reader.read({'url': ARTICLE_URL}).error_code == 'official_response_unreadable'
