"""Search and read Feishu Help Center through its public website requests."""

import hashlib
import json
import re
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from src.core.agent_runtime import ToolResult

HELP_ORIGIN = 'https://www.feishu.cn'
SEARCH_ENDPOINT = HELP_ORIGIN + '/hc/api/article/search'


def canonical_help_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'www.feishu.cn'
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise ValueError('Expected a Feishu Help Center HTTPS article URL')
    match = re.fullmatch(r'/hc/zh-CN/articles/(\d+)(?:[-/].*)?', parsed.path)
    if not match:
        raise ValueError('Expected a Feishu Help Center article path')
    return HELP_ORIGIN + '/hc/zh-CN/articles/' + match[1]


def extract_article(html: str) -> dict:
    """Read text payloads without executing page JavaScript or losing FAQ questions."""
    marker = re.search(r'window\._templateValue\s*=\s*', html)
    if not marker:
        raise ValueError('Help Center article payload missing')
    data, _ = json.JSONDecoder().raw_decode(html[marker.end():])
    sections = []

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            if isinstance(value.get('answer_txt'), str):
                sections.append((value.get('question', '') + '\n' + value['answer_txt']).strip())
                return
            body = value.get('html')
            if isinstance(body, dict) and isinstance(body.get('text'), str) and body['text'].strip():
                sections.append(body['text'].strip())
            for key, child in value.items():
                if key != 'html':
                    visit(child)

    visit(data)
    if not sections:
        raise ValueError('Help Center article text missing')
    title_match = re.search(r'<title>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    title = unescape(title_match[1]).strip() if title_match else 'Feishu Help Center'
    return {'title': title, 'sections': sections, 'source_updated_at': data.get('_updateTime'),
            'extraction': 'embedded_article_text_and_faq; images_and_table_layout_not_extracted'}


class OfficialHelp:
    """One official entry point with captured HTTP responses and exact-call replay."""

    def __init__(self, *, record_dir: Path, proxy: str | None = None,
                 replay_dir: Path | None = None, client=None, read_failure: str | None = None,
                 search_failure: str | None = None):
        self.record_dir = record_dir
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.replay_dir = replay_dir
        self.client = client
        self.proxy = proxy
        self.read_failure = read_failure
        self.search_failure = search_failure
        self.documents: dict[str, dict] = {}

    def fetch(self, url: str) -> str:
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()
        if self.replay_dir:
            metadata = json.loads((self.replay_dir / (key + '.json')).read_text(encoding='utf-8'))
            body = (self.replay_dir / metadata['body_file']).read_bytes()
            if hashlib.sha256(body).hexdigest() != metadata['body_sha256']:
                raise ValueError('Replay response checksum mismatch')
            if metadata['status_code'] != 200:
                raise httpx.HTTPStatusError('Recorded HTTP failure', request=httpx.Request('GET', url),
                                            response=httpx.Response(metadata['status_code']))
            return body.decode('utf-8')
        redirects = []

        def request(client):
            current_url = url
            for _ in range(4):
                response = client.get(current_url)
                if not response.is_redirect:
                    return response
                destination = urljoin(current_url, response.headers.get('location', ''))
                if canonical_help_url(destination) != canonical_help_url(url):
                    raise ValueError('Redirect left the requested official article')
                redirects.append({'url': current_url, 'status_code': response.status_code,
                                  'destination': destination})
                current_url = destination
            raise ValueError('Official article redirect limit reached')

        if self.client is None:
            with httpx.Client(proxy=self.proxy, timeout=30, follow_redirects=False, trust_env=False) as client:
                response = request(client)
        else:
            response = request(self.client)
        body = response.content
        if len(body) > 5_000_000:
            raise ValueError('Help Center response exceeds size limit')
        body_hash = hashlib.sha256(body).hexdigest()
        body_file = key + '-' + body_hash[:16] + '.body'
        (self.record_dir / body_file).write_bytes(body)
        metadata = {
            'url': url, 'status_code': response.status_code, 'body_file': body_file,
            'body_sha256': body_hash,
            'fetched_at': datetime.now(UTC).isoformat(),
            'redirects': redirects,
        }
        (self.record_dir / (key + '.json')).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8',
        )
        with (self.record_dir / 'requests.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(metadata, ensure_ascii=False) + '\n')
        response.raise_for_status()
        return body.decode('utf-8')

    @staticmethod
    def failure(error: Exception) -> ToolResult:
        if isinstance(error, FileNotFoundError):
            code = 'official_replay_miss'
        elif isinstance(error, httpx.TimeoutException):
            code = 'official_timeout'
        elif isinstance(error, httpx.HTTPStatusError):
            code = 'official_http_' + str(error.response.status_code)
        elif isinstance(error, httpx.RequestError):
            code = 'official_network_error'
        else:
            code = 'official_response_unreadable'
        return ToolResult(success=False, error_code=code,
                          error='Official evidence could not be obtained for this request.')

    def search(self, params: dict) -> ToolResult:
        if self.search_failure:
            return ToolResult(success=False, error_code=self.search_failure,
                              error='Official search is unavailable in this controlled failure scenario.',
                              data={'fault_injection': True})
        query = params['query'].strip()
        limit = params.get('limit', 5)
        url = SEARCH_ENDPOINT + '?' + urlencode({
            'query': query, 'language': 'zh-CN', 'offset': 0, 'limit': limit, 'page': 1,
        })
        try:
            response = json.loads(self.fetch(url))
            if response.get('code') != 0 or not isinstance(response.get('data', {}).get('list'), list):
                raise ValueError('Unexpected official search response')
            results = []
            for rank, entry in enumerate(response['data']['list'][:limit], 1):
                results.append({
                    'rank': rank, 'title': entry['title']['name'],
                    'url': canonical_help_url(HELP_ORIGIN + entry['path']),
                    'snippet': entry['content']['abstract'], 'source_updated_at': entry.get('update_time'),
                })
            return ToolResult(success=True, data={
                'query': query, 'results': results, 'total': response['data']['total'],
                'search_url': HELP_ORIGIN + '/hc/zh-CN/search?' + urlencode({'query': query, 'page': 1}),
                'evidence_kind': 'search_snippets',
                'retrieval_mode': 'http_response_replay' if self.replay_dir else 'live_website',
            })
        except (httpx.HTTPError, ValueError, KeyError, TypeError, FileNotFoundError) as error:
            return self.failure(error)

    def read(self, params: dict) -> ToolResult:
        try:
            url = canonical_help_url(params['url'])
        except (ValueError, TypeError):
            return ToolResult(success=False, error_code='unsupported_document_url',
                              error='This reader accepts Feishu Help Center article URLs.')
        if self.read_failure:
            return ToolResult(success=False, error_code=self.read_failure,
                              error='The article body is unavailable in this controlled failure scenario.',
                              data={'fault_injection': True, 'url': url})
        try:
            if url not in self.documents:
                article = extract_article(self.fetch(url))
                passages = []
                for section in article['sections']:
                    passages.extend(section[i:i + 1600] for i in range(0, len(section), 1600))
                article['passages'] = passages
                self.documents[url] = article
            article = self.documents[url]
            query = params.get('query', '').strip()
            offset = params.get('offset', 0)
            limit = params.get('limit', 3)
            indices = list(range(len(article['passages'])))
            if query:
                # Rank within the requested article while keeping each returned passage intact.
                from src.utils.bm25_store import BM25Store

                index = BM25Store()
                index.build(article['passages'], [str(i) for i in indices])
                ranked = [int(i) for i, _ in index.search(query, top_k=len(indices))]
                matched = set(ranked)
                indices = ranked + [i for i in indices if i not in matched]
            selected = indices[offset:offset + limit]
            article_id = url.rsplit('/', 1)[-1]
            contexts = [{
                'document_id': f'hc-{article_id}-p{i + 1:03d}', 'source_document_id': article_id,
                'title': article['title'], 'text': article['passages'][i], 'score': 1.0,
                'url': url, 'source_type': 'official', 'source_updated_at': article['source_updated_at'],
            } for i in selected]
            return ToolResult(success=True, data={
                'contexts': contexts, 'url': url, 'extraction': article['extraction'],
                'retrieval_mode': 'http_response_replay' if self.replay_dir else 'live_website',
                'next_offset': offset + limit if offset + limit < len(indices) else None,
                'total_passages': len(article['passages']),
                'paging_query': query,
                'pagination': 'Keep the same query while paging. When changing query, restart at offset 0. '
                              'Query ranks passages first; remaining passages follow in document order.',
            })
        except (httpx.HTTPError, ValueError, KeyError, TypeError, FileNotFoundError) as error:
            return self.failure(error)
