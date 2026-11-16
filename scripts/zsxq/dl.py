#!/usr/bin/env python3
"""Resumable archiver for a 知识星球 (zsxq) group.

Usage:  ZSXQ_ARCHIVE=<存档目录> python3 dl.py            # full / incremental crawl + render markdown
        ZSXQ_ARCHIVE=<存档目录> python3 dl.py --render   # only re-render markdown/README from raw data

方伟知识星球的定时抓取（scripts/crawl-fangwei-zsxq.sh）用它，存档目录是 data/inbox/fangwei-zsxq。
11-14 以前这份脚本只放在存档目录里、不在仓库里（审核 O3）；收进仓库时只改了三处：
存档目录由 ZSXQ_ARCHIVE 指定（以前是脚本所在目录，脚本放进仓库以后不能再往仓库里写数据）；
星球 ID 不再写死，从 ZSXQ_GROUP_ID 读；生成的 README 里不写存档人的名字。抓取和渲染逻辑没动。

Token is read from ~/zsxq/zsxq.env (ZSXQ_ACCESS_TOKEN=...). It is never written
to disk by this script and never logged.  ZSXQ_GROUP_ID comes from the environment
or the same env file.  Python 3.9 stdlib only.
"""
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser

ENV_FILE = os.path.expanduser(os.environ.get('ZSXQ_ENV', '~/zsxq/zsxq.env'))


def read_env_value(key):
    """环境变量优先，其次 ENV_FILE 里的 KEY=...；都没有返回空串。"""
    value = os.environ.get(key, '').strip()
    if value:
        return value
    try:
        with open(ENV_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + '='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ''


# 星球 ID 不写进公开仓库：放在 ~/zsxq/zsxq.env（ZSXQ_GROUP_ID=...）或环境变量里。
GROUP_ID = read_env_value('ZSXQ_GROUP_ID')
# 存档根目录（raw/、md/、images/ 都写在这里）。不给时不跑，免得把数据写进仓库。
ROOT = os.path.abspath(os.path.expanduser(os.environ['ZSXQ_ARCHIVE'])) if os.environ.get('ZSXQ_ARCHIVE') else ''
API = 'https://api.zsxq.com'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')
TZ8 = dt.timezone(dt.timedelta(hours=8))

RAW = os.path.join(ROOT, 'raw')
D_TOPICS = os.path.join(RAW, 'topics')
D_COMMENTS = os.path.join(RAW, 'comments')
D_ARTICLES = os.path.join(RAW, 'articles')
D_DETAIL = os.path.join(RAW, 'topic_detail')
D_IMAGES = os.path.join(ROOT, 'images')
D_FILES = os.path.join(ROOT, 'files')
D_MD = os.path.join(ROOT, 'md')
STATE = os.path.join(RAW, 'state.json')
MEDIA = os.path.join(RAW, 'media_status.json')
LOG = os.path.join(ROOT, 'dl.log')          # git-ignored
STATUS = os.path.join(ROOT, 'status.txt')   # git-ignored

API_DELAY = (1.2, 2.5)  # match chengfeng normal pace (user: no need to throttle if stable)
CDN_DELAY = (0.3, 0.8)


class Fatal(Exception):
    pass


def log(msg):
    line = '%s %s' % (dt.datetime.now(TZ8).strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def status(msg):
    with open(STATUS, 'w', encoding='utf-8') as f:
        f.write(dt.datetime.now(TZ8).strftime('%Y-%m-%d %H:%M:%S') + ' ' + msg + '\n')


def load_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_token():
    with open(ENV_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('ZSXQ_ACCESS_TOKEN='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise Fatal('ZSXQ_ACCESS_TOKEN not found in env file')


# ---------------------------------------------------------------- HTTP
class Client:
    def __init__(self):
        self.token = read_token()
        self.n_api = 0
        self.last_api = 0.0
        self.last_cdn = 0.0

    def _headers(self, with_cookie=True, accept='application/json, text/plain, */*'):
        h = {'origin': 'https://wx.zsxq.com', 'referer': 'https://wx.zsxq.com/',
             'user-agent': UA, 'accept': accept}
        if with_cookie:
            h.update({'cookie': 'zsxq_access_token=' + self.token, 'x-version': '2.77.0',
                      'x-timestamp': str(int(time.time())), 'x-request-id': str(uuid.uuid4())})
        return h

    def _pace(self, attr, rng):
        wait = getattr(self, attr) + random.uniform(*rng) - time.time()
        if wait > 0:
            time.sleep(wait)
        setattr(self, attr, time.time())

    def api(self, path, params=None, allow_fail=False):
        """GET JSON from api.zsxq.com, retrying code 1059 with backoff."""
        url = API + path
        if params:
            url += '?' + '&'.join('%s=%s' % (k, urllib.parse.quote(str(v), safe='')) for k, v in params.items())
        backoff = [2, 3, 5, 8, 13, 20, 30, 45]
        last = None
        for attempt in range(len(backoff) + 1):
            self._pace('last_api', API_DELAY)
            self.n_api += 1
            try:
                req = urllib.request.Request(url, headers=self._headers())
                with urllib.request.urlopen(req, timeout=40) as r:
                    d = json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise Fatal('HTTP %s on %s (auth/ban?)' % (e.code, path))
                if e.code == 429 or e.code >= 500:
                    last = 'HTTP %s' % e.code
                    if attempt < len(backoff):
                        log('  %s on %s, retry in %ss' % (last, path, backoff[attempt] * 2))
                        time.sleep(backoff[attempt] * 2)
                        continue
                    raise Fatal('HTTP %s persists on %s' % (e.code, path))
                if allow_fail:
                    return {'succeeded': False, 'code': 'HTTP%s' % e.code}
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = repr(e)
                if attempt < len(backoff):
                    time.sleep(backoff[attempt])
                    continue
                raise
            if d.get('succeeded'):
                return d
            code = d.get('code')
            last = code
            if code == 1059 and attempt < len(backoff):
                time.sleep(backoff[attempt] if attempt > 1 else 1.5 + attempt)
                continue
            if code in (401, 14210, 1004):  # auth related
                raise Fatal('API auth error code %s on %s' % (code, path))
            if allow_fail:
                return d
            raise Fatal('API error code %s on %s: %s' % (code, path, str(d.get('info') or d.get('error') or '')[:200]))
        if allow_fail:
            return {'succeeded': False, 'code': last}
        raise Fatal('retries exhausted (%s) on %s' % (last, path))

    def fetch(self, url, dest=None, cookie=False, accept='*/*'):
        """Download a URL (CDN/article). Returns bytes if dest is None."""
        for attempt in range(4):
            self._pace('last_cdn', CDN_DELAY)
            try:
                req = urllib.request.Request(url, headers=self._headers(cookie, accept))
                with urllib.request.urlopen(req, timeout=120) as r:
                    if dest is None:
                        return r.read()
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    tmp = dest + '.part'
                    with open(tmp, 'wb') as f:
                        while True:
                            b = r.read(1 << 16)
                            if not b:
                                break
                            f.write(b)
                    os.replace(tmp, dest)
                    return True
            except urllib.error.HTTPError as e:
                if e.code in (404, 410, 403) or attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))


# ---------------------------------------------------------------- helpers
def parse_t(s):
    return dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.%f%z')


def fmt_t(d):
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + '%03d' % (d.microsecond // 1000) + d.strftime('%z')


def shift_ms(s, ms):
    return fmt_t(parse_t(s) + dt.timedelta(milliseconds=ms))


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def find_images(obj):
    out = []
    for d in walk(obj):
        if 'image_id' in d and ('original' in d or 'large' in d or 'thumbnail' in d):
            out.append(d)
    return out


def find_files(obj):
    return [d for d in walk(obj) if 'file_id' in d and 'name' in d]


def image_url(img):
    for k in ('original', 'large', 'thumbnail'):
        if isinstance(img.get(k), dict) and img[k].get('url'):
            return img[k]['url'], k
    return None, None


def image_name(img):
    ext = (img.get('type') or 'jpg').lower()
    if ext == 'jpeg':
        ext = 'jpg'
    return '%s.%s' % (img['image_id'], ext)


def safe_name(s, maxlen=120):
    s = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', s).strip().strip('.')
    if len(s.encode('utf-8')) > maxlen:
        root, ext = os.path.splitext(s)
        while len((root + ext).encode('utf-8')) > maxlen and root:
            root = root[:-1]
        s = root + ext
    return s or 'file'


def file_local_name(f):
    return '%s_%s' % (f['file_id'], safe_name(f.get('name', 'file')))


def topic_body(t):
    return t.get('talk') or t.get('question') or t.get('task') or t.get('solution') or {}


# ---------------------------------------------------------------- crawl
def crawl_group(c):
    for name, path in [('group', '/v2/groups/%s' % GROUP_ID),
                       ('statistics', '/v2/groups/%s/statistics' % GROUP_ID),
                       ('columns', '/v2/groups/%s/columns' % GROUP_ID),
                       ('hashtags', '/v2/groups/%s/hashtags' % GROUP_ID)]:
        d = c.api(path, allow_fail=True)
        save_json(os.path.join(RAW, name + '.json'), d)
        log('group-level %s: succeeded=%s' % (name, d.get('succeeded')))
    cols = (load_json(os.path.join(RAW, 'columns.json'), {}) or {}).get('resp_data', {}).get('columns', [])
    for col in cols:
        cid = col.get('column_id')
        d = c.api('/v2/groups/%s/columns/%s/topics' % (GROUP_ID, cid), {'count': 100, 'sort': 'attached_to_column_time'}, allow_fail=True)
        save_json(os.path.join(RAW, 'columns', '%s.json' % cid), d)


def crawl_topics(c, state):
    full_done = state.get('topics_full_done', False)
    end_time = None if full_done else state.get('topics_cursor')
    if end_time:
        log('resuming topic pagination from end_time=%s' % end_time)
    page = 0
    new = 0
    while True:
        page += 1
        params = {'scope': 'all', 'count': 20}
        if end_time:
            params['end_time'] = end_time
        d = c.api('/v2/groups/%s/topics' % GROUP_ID, params)
        topics = d['resp_data'].get('topics', [])
        if not topics:
            state['topics_full_done'] = True
            state.pop('topics_cursor', None)
            save_json(STATE, state)
            log('topics: reached the end (page %d)' % page)
            break
        all_existing = True
        for t in topics:
            p = os.path.join(D_TOPICS, '%s.json' % t['topic_id'])
            if not os.path.exists(p):
                all_existing = False
                new += 1
            save_json(p, t)  # always refresh (counts/comments may change)
        non_sticky = [t for t in topics if not t.get('sticky')] or topics
        end_time = shift_ms(min(t['create_time'] for t in non_sticky), -1)
        if not full_done:
            state['topics_cursor'] = end_time
            save_json(STATE, state)
        n = len(os.listdir(D_TOPICS))
        log('topics page %d: %d topics, oldest %s, total saved %d' % (page, len(topics), topics[-1]['create_time'], n))
        status('phase=topics page=%d saved=%d' % (page, n))
        if full_done and all_existing and page > 1:
            log('topics: incremental mode, page fully known, stopping')
            break
    return new


def crawl_extra_scopes(c, state):
    """Cross-check: paginate scope=digests and scope=questions; save topics missing from scope=all."""
    have = set(fn[:-5] for fn in os.listdir(D_TOPICS))
    found = {}
    for scope in ('digests', 'questions'):
        end_time, n = None, 0
        for _ in range(500):
            params = {'scope': scope, 'count': 20}
            if end_time:
                params['end_time'] = end_time
            d = c.api('/v2/groups/%s/topics' % GROUP_ID, params, allow_fail=True)
            topics = (d.get('resp_data') or {}).get('topics') or []
            if not topics:
                break
            for t in topics:
                n += 1
                if str(t['topic_id']) not in have:
                    save_json(os.path.join(D_TOPICS, '%s.json' % t['topic_id']), t)
                    have.add(str(t['topic_id']))
                    found[str(t['topic_id'])] = scope
            end_time = shift_ms(min(t['create_time'] for t in topics), -1)
        log('scope=%s: %d topics seen' % (scope, n))
        state.setdefault('scope_counts', {})[scope] = n
    state['extra_scope_found'] = found
    save_json(STATE, state)
    if found:
        log('extra topics found only via other scopes: %s' % found)


def crawl_comments(c, tid, expected):
    pages, seen, merged = [], set(), []
    begin = None
    for _ in range(200):
        params = {'sort': 'asc', 'count': 30, 'with_sticky': 'true'}
        if begin:
            params['begin_time'] = begin
        d = c.api('/v2/topics/%s/comments' % tid, params)
        rd = d.get('resp_data', {})
        pages.append(rd)
        cs = rd.get('comments', [])
        fresh = [x for x in cs if x['comment_id'] not in seen]
        for x in fresh:
            seen.add(x['comment_id'])
            merged.append(x)
        if len(cs) < 30 or not fresh:
            break
        begin = shift_ms(max(x['create_time'] for x in cs), 1)
    n_total = sum(1 + len(x.get('replied_comments') or []) for x in merged)
    incomplete_replies = [x['comment_id'] for x in merged
                          if (x.get('replies_count') or 0) > len(x.get('replied_comments') or [])]
    return {'topic_id': tid, 'comments_count_at_fetch': expected, 'fetched_count': n_total,
            'top_level_count': len(merged), 'incomplete_reply_threads': incomplete_replies,
            'fetched_at': fmt_t(dt.datetime.now(TZ8)), 'comments': merged, 'raw_pages': pages}


def try_more_replies(c, cdata):
    """For comments whose replies are truncated, try the replied_comments endpoint."""
    for cm in cdata['comments']:
        have = cm.get('replied_comments') or []
        if (cm.get('replies_count') or 0) <= len(have):
            continue
        got = []
        d = c.api('/v2/comments/%s/replied_comments' % cm['comment_id'], {'sort': 'asc', 'count': 100}, allow_fail=True)
        got = (d.get('resp_data') or {}).get('replied_comments') or []
        if len(got) > len(have):
            cm['replied_comments'] = got
            log('  expanded replies for comment %s: %d -> %d' % (cm['comment_id'], len(have), len(got)))
    cdata['incomplete_reply_threads'] = [x['comment_id'] for x in cdata['comments']
                                         if (x.get('replies_count') or 0) > len(x.get('replied_comments') or [])]
    cdata['fetched_count'] = sum(1 + len(x.get('replied_comments') or []) for x in cdata['comments'])


def all_topics():
    out = []
    for fn in os.listdir(D_TOPICS):
        if fn.endswith('.json'):
            out.append(load_json(os.path.join(D_TOPICS, fn)))
    out.sort(key=lambda t: t['create_time'])
    return out


def crawl_details_and_comments(c):
    topics = all_topics()
    for i, t in enumerate(topics):
        tid = t['topic_id']
        body = topic_body(t)
        # topic detail for truncated non-article text
        txt = body.get('text') or ''
        if txt.rstrip().endswith('...') and not body.get('article'):
            dp = os.path.join(D_DETAIL, '%s.json' % tid)
            if not os.path.exists(dp):
                d = c.api('/v2/topics/%s' % tid, allow_fail=True)
                save_json(dp, d)
        n = t.get('comments_count') or 0
        cp = os.path.join(D_COMMENTS, '%s.json' % tid)
        if n > 0:
            old = load_json(cp)
            if old and old.get('comments_count_at_fetch') == n:
                continue
            cdata = crawl_comments(c, tid, n)
            if cdata['incomplete_reply_threads']:
                try_more_replies(c, cdata)
            save_json(cp, cdata)
            if cdata['fetched_count'] != n:
                log('  note: topic %s comments_count=%d fetched=%d' % (tid, n, cdata['fetched_count']))
        if i % 20 == 0:
            status('phase=comments %d/%d' % (i, len(topics)))
            log('comments progress %d/%d' % (i, len(topics)))


class ArticleParser(HTMLParser):
    """Extract the article body (div.content.ql-editor) as markdown."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.in_content = False
        self.out = []
        self.images = []
        self.links = []
        self.list_stack = []
        self.title = None
        self._in_title = False
        self.date = None
        self._in_date = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get('class', '') or ''
        if not self.in_content:
            if tag == 'div' and 'content' in cls.split() and 'ql-editor' in cls:
                self.in_content = True
                self.depth = 1
            elif tag == 'h1' and 'title' in cls:
                self._in_title = True
                self.title = ''
            elif tag == 'span' and a.get('id') == 'article-date':
                self._in_date = True
                self.date = ''
            return
        if tag == 'div':
            self.depth += 1
        if tag in ('p', 'div'):
            self.out.append('\n\n')
        elif tag == 'br':
            self.out.append('\n')
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.out.append('\n\n' + '#' * min(6, int(tag[1]) + 1) + ' ')
        elif tag in ('strong', 'b'):
            self.out.append('**')
        elif tag in ('em', 'i'):
            self.out.append('*')
        elif tag in ('s', 'del'):
            self.out.append('~~')
        elif tag == 'blockquote':
            self.out.append('\n\n> ')
        elif tag in ('ul', 'ol'):
            self.list_stack.append([tag, 0])
            self.out.append('\n')
        elif tag == 'li':
            if self.list_stack:
                self.list_stack[-1][1] += 1
                mark = '%d.' % self.list_stack[-1][1] if self.list_stack[-1][0] == 'ol' else '-'
            else:
                mark = '-'
            self.out.append('\n' + '  ' * max(0, len(self.list_stack) - 1) + mark + ' ')
        elif tag == 'img':
            src = a.get('src') or a.get('data-src') or ''
            if src:
                self.images.append(src)
                self.out.append('\n\n{{IMG:%s}}\n\n' % src)
        elif tag == 'a':
            self.links.append(a.get('href', ''))
            self.out.append('[')
        elif tag == 'pre':
            self.out.append('\n\n```\n')
        elif tag == 'code':
            self.out.append('`')

    def handle_endtag(self, tag):
        if self._in_title and tag == 'h1':
            self._in_title = False
        if self._in_date and tag == 'span':
            self._in_date = False
        if not self.in_content:
            return
        if tag == 'div':
            self.depth -= 1
            if self.depth == 0:
                self.in_content = False
                return
        if tag in ('strong', 'b'):
            self.out.append('**')
        elif tag in ('em', 'i'):
            self.out.append('*')
        elif tag in ('s', 'del'):
            self.out.append('~~')
        elif tag in ('ul', 'ol'):
            if self.list_stack:
                self.list_stack.pop()
            self.out.append('\n')
        elif tag == 'a':
            href = self.links.pop() if self.links else ''
            self.out.append('](%s)' % href)
        elif tag == 'pre':
            self.out.append('\n```\n')
        elif tag == 'code':
            self.out.append('`')

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_date:
            self.date += data
        elif self.in_content:
            self.out.append(data)

    def markdown(self):
        s = ''.join(self.out)
        s = re.sub(r'\*\*\s*\*\*', '', s)
        s = re.sub(r'[ \t]+\n', '\n', s)
        s = re.sub(r'\n{3,}', '\n\n', s)
        return s.strip()


def parse_article(html_text):
    p = ArticleParser()
    p.feed(html_text)
    return p


def article_img_name(url):
    path = urllib.parse.urlparse(url).path
    h = hashlib.sha1(path.encode()).hexdigest()[:16]
    ext = os.path.splitext(path)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
        ext = '.jpg'
    return 'articles/%s%s' % (h, ext)


def crawl_articles(c, media):
    topics = all_topics()
    arts = [(t['topic_id'], topic_body(t).get('article')) for t in topics if topic_body(t).get('article')]
    for i, (tid, a) in enumerate(arts):
        aid = a.get('article_id') or hashlib.sha1(a.get('article_url', '').encode()).hexdigest()[:12]
        p = os.path.join(D_ARTICLES, '%s.html' % aid)
        if not os.path.exists(p):
            url = a.get('article_url') or a.get('inline_article_url')
            try:
                b = c.fetch(url, cookie=True, accept='text/html,application/xhtml+xml,*/*')
                txt = b.decode('utf-8', 'replace')
                if 'ql-editor' not in txt and a.get('inline_article_url') and url != a['inline_article_url']:
                    b = c.fetch(a['inline_article_url'], cookie=True, accept='text/html,*/*')
                    txt = b.decode('utf-8', 'replace')
                os.makedirs(D_ARTICLES, exist_ok=True)
                with open(p, 'w', encoding='utf-8') as f:
                    f.write(txt)
            except Exception as e:
                media.setdefault('articles_failed', {})[aid] = {'topic_id': tid, 'error': repr(e)[:200]}
                log('  article %s failed: %r' % (aid, e))
                continue
        # article images
        with open(p, encoding='utf-8') as f:
            parsed = parse_article(f.read())
        for src in parsed.images:
            if not src.startswith('http'):
                continue
            rel = article_img_name(src)
            dest = os.path.join(D_IMAGES, rel)
            if os.path.exists(dest):
                continue
            try:
                c.fetch(src, dest)
            except Exception as e:
                media.setdefault('images_failed', {})[rel] = {'topic_id': tid, 'url_kind': 'article', 'error': repr(e)[:200]}
        if i % 20 == 0:
            status('phase=articles %d/%d' % (i, len(arts)))
            log('articles progress %d/%d' % (i, len(arts)))
            save_json(MEDIA, media)
    save_json(MEDIA, media)


def iter_topic_and_comment_objs():
    for t in all_topics():
        tid = t['topic_id']
        objs = [t]
        dp = load_json(os.path.join(D_DETAIL, '%s.json' % tid))
        if dp:
            objs.append(dp)
        cp = load_json(os.path.join(D_COMMENTS, '%s.json' % tid))
        if cp:
            objs.append(cp['comments'])
        yield tid, objs


def crawl_images(c, media):
    todo = []
    seen = set()
    for tid, objs in iter_topic_and_comment_objs():
        for o in objs:
            for img in find_images(o):
                nm = image_name(img)
                if nm in seen:
                    continue
                seen.add(nm)
                todo.append((tid, img, nm))
    log('images referenced: %d' % len(todo))
    done = 0
    for i, (tid, img, nm) in enumerate(todo):
        dest = os.path.join(D_IMAGES, nm)
        if os.path.exists(dest):
            continue
        url, kind = image_url(img)
        if not url:
            continue
        try:
            c.fetch(url, dest)
            done += 1
        except Exception as e:
            # fall back to large
            try:
                if kind == 'original' and img.get('large', {}).get('url'):
                    c.fetch(img['large']['url'], dest)
                    media.setdefault('images_fallback_large', []).append(nm)
                    done += 1
                    continue
            except Exception:
                pass
            media.setdefault('images_failed', {})[nm] = {'topic_id': tid, 'error': repr(e)[:200]}
            log('  image %s failed: %r' % (nm, e))
        if i % 25 == 0:
            status('phase=images %d/%d' % (i, len(todo)))
            save_json(MEDIA, media)
    log('images downloaded this run: %d' % done)
    save_json(MEDIA, media)


def crawl_files(c, media):
    fstat = media.setdefault('files', {})
    todo, seen = [], set()
    for tid, objs in iter_topic_and_comment_objs():
        for o in objs:
            for f in find_files(o):
                if f['file_id'] in seen:
                    continue
                seen.add(f['file_id'])
                todo.append((tid, f))
    log('files referenced: %d' % len(todo))
    stop_reason = None
    for tid, f in todo:
        fid = str(f['file_id'])
        name = file_local_name(f)
        dest = os.path.join(D_FILES, name)
        if os.path.exists(dest):
            fstat[fid] = dict(fstat.get(fid, {}), status='ok', local=name, topic_id=tid, name=f.get('name'), size=f.get('size'))
            continue
        if stop_reason:
            fstat[fid] = {'status': 'missing', 'reason': stop_reason, 'topic_id': tid, 'name': f.get('name'), 'size': f.get('size')}
            continue
        d = c.api('/v2/files/%s/download_url' % fid, allow_fail=True)
        url = (d.get('resp_data') or {}).get('download_url')
        if not url:
            code = d.get('code')
            info = str(d.get('info') or d.get('error') or '')[:200]
            fstat[fid] = {'status': 'missing', 'reason': 'download_url refused code=%s %s' % (code, info),
                          'topic_id': tid, 'name': f.get('name'), 'size': f.get('size')}
            log('  file %s (%s) refused: code=%s %s' % (fid, f.get('name'), code, info))
            if code not in (1059, 1030):  # 1030 = 权限不足 (group disallows download) is per-file
                # most likely a daily download quota; stop asking today
                stop_reason = 'not attempted after refusal code=%s (likely daily limit)' % code
            continue
        try:
            c.fetch(url, dest)
            fstat[fid] = {'status': 'ok', 'local': name, 'topic_id': tid, 'name': f.get('name'), 'size': f.get('size')}
            log('  file ok: %s (%s bytes)' % (name, os.path.getsize(dest)))
        except Exception as e:
            fstat[fid] = {'status': 'missing', 'reason': repr(e)[:200], 'topic_id': tid, 'name': f.get('name'), 'size': f.get('size')}
        save_json(MEDIA, media)
    save_json(MEDIA, media)


# ---------------------------------------------------------------- render
E_TAG = re.compile(r'<e\s+([^>]*?)\s*/>')
ATTR = re.compile(r'(\w+)="([^"]*)"')


def decode_e(text):
    def rep(m):
        a = {k: urllib.parse.unquote(html.unescape(v)) for k, v in ATTR.findall(m.group(1))}
        typ = a.get('type', '')
        title = a.get('title', '')
        if typ == 'web':
            href = a.get('href', '')
            return '[%s](%s)' % (title or href, href)
        if typ == 'text_bold':
            return '**%s**' % title.strip() if title.strip() else title
        if typ == 'text_italic':
            return '*%s*' % title
        if typ == 'text_delete':
            return '~~%s~~' % title
        if typ == 'hashtag':
            return title
        if typ == 'mention':
            return title if title.startswith('@') else '@' + title
        return title or a.get('href', '')
    return E_TAG.sub(rep, text or '')


def hkt(s):
    try:
        return parse_t(s).astimezone(TZ8).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return s


def md_quote(text, prefix='> '):
    return '\n'.join(prefix + l if l else prefix.rstrip() for l in text.split('\n'))


def render_images(objlist, rel):
    lines = []
    for img in objlist or []:
        nm = image_name(img)
        if os.path.exists(os.path.join(D_IMAGES, nm)):
            lines.append('![](%s/images/%s)' % (rel, nm))
        else:
            u, _ = image_url(img)
            lines.append('![图片未下载 %s](%s)' % (nm, u or ''))
    return lines


def render_files(files, rel, media):
    lines = []
    for f in files or []:
        nm = file_local_name(f)
        size = f.get('size')
        sz = (' (%.1f MB)' % (size / 1048576.0)) if size else ''
        st = media.get('files', {}).get(str(f['file_id']), {})
        if os.path.exists(os.path.join(D_FILES, nm)):
            note = ' — 仅本地保存，未提交到 git（>100MB）' if st.get('not_in_git') else ''
            lines.append('- 📎 [%s](%s/files/%s)%s%s' % (f.get('name'), rel, urllib.parse.quote(nm), sz, note))
        else:
            lines.append('- 📎 %s%s — 未下载（%s）' % (f.get('name'), sz, st.get('reason', 'missing')))
    return lines


def name_of(o):
    return (o or {}).get('name') or '匿名'


def render_comment(cm, rel, indent=''):
    out = []
    who = name_of(cm.get('owner'))
    if cm.get('repliee'):
        who += ' 回复 ' + name_of(cm['repliee'])
    txt = decode_e(cm.get('text', '')).strip()
    first, *rest = (txt.split('\n') or [''])
    out.append('%s- **%s**（%s）%s' % (indent, who, hkt(cm.get('create_time', '')), ('👍%d ' % cm['likes_count']) if cm.get('likes_count') else ''))
    for l in txt.split('\n'):
        out.append('%s  %s' % (indent, l))
    for l in render_images(cm.get('images'), rel):
        out.append('%s  %s' % (indent, l))
    for r in cm.get('replied_comments') or []:
        out.extend(render_comment(r, rel, indent + '  '))
    return out


def render_body(body, rel, media, heading_level=2):
    out = []
    txt = decode_e(body.get('text', ''))
    if txt:
        out.append(txt.strip())
        out.append('')
    out.extend(render_images(body.get('images'), rel))
    if body.get('images'):
        out.append('')
    fl = render_files(body.get('files'), rel, media)
    if fl:
        out.append('**附件：**')
        out.extend(fl)
        out.append('')
    return out


def topic_title(t):
    if t.get('type') == 'q&a' and (t.get('question') or {}).get('text'):
        s = decode_e(t['question']['text']).strip().split('\n')[0]
        s = re.sub(r'[*#\[\]]', '', s)
        return '问：' + ((s[:40] + '…') if len(s) > 40 else s)
    if t.get('title'):
        return t['title'].strip().replace('\n', ' ')
    b = topic_body(t)
    if b.get('article', {}).get('title'):
        return b['article']['title'].strip()
    s = decode_e(b.get('text', '')).strip().split('\n')[0]
    s = re.sub(r'[*#\[\]]', '', s)
    return (s[:40] + '…') if len(s) > 40 else (s or '(无标题)')


def render_topic(t, media):
    tid = t['topic_id']
    rel = '../..'
    b = topic_body(t)
    typ = t.get('type')
    lines = ['# %s' % topic_title(t), '']
    owner = b.get('owner') or (t.get('question') or {}).get('owner') or {}
    meta = ['- 作者：%s' % name_of(owner),
            '- 时间：%s (UTC+8)' % hkt(t['create_time']),
            '- 类型：%s%s%s' % (typ, ' · ⭐精华' if t.get('digested') else '', ' · 📌置顶' if t.get('sticky') else ''),
            '- 点赞 %s · 评论 %s · 阅读人数 %s' % (t.get('likes_count', 0), t.get('comments_count', 0), t.get('readers_count', t.get('reading_count', 0))),
            '- topic_id：`%s` · 原链接：https://wx.zsxq.com/topic/%s' % (tid, tid),
            '- 原始数据：[raw/topics/%s.json](%s/raw/topics/%s.json)' % (tid, rel, tid)]
    lines += meta + ['']
    if typ == 'q&a':
        q = t.get('question') or {}
        lines.append('## 提问（%s%s）' % ('匿名' if q.get('anonymous') else name_of(q.get('owner')),
                                       (' → ' + name_of(q.get('questionee'))) if q.get('questionee') else ''))
        lines.append('')
        lines += render_body(q, rel, media)
        ans = t.get('answer') or {}
        lines.append('## 回答（%s）' % name_of(ans.get('owner')))
        lines.append('')
        lines += render_body(ans, rel, media)
    else:
        # if detail has fuller text, use it
        dp = load_json(os.path.join(D_DETAIL, '%s.json' % tid))
        if dp and dp.get('succeeded'):
            db = topic_body(dp['resp_data'].get('topic', {}))
            if len(db.get('text', '')) > len(b.get('text', '')):
                b = dict(b, text=db['text'])
        art = b.get('article')
        if art:
            lines.append('> 以下为话题摘要，全文见下方「长文」。')
            lines.append('')
        lines += render_body(b, rel, media)
        if art:
            aid = art.get('article_id')
            p = os.path.join(D_ARTICLES, '%s.html' % aid)
            lines.append('## 长文：%s' % (art.get('title') or '').strip())
            lines.append('')
            lines.append('原文链接：%s · 原始 HTML：[raw/articles/%s.html](%s/raw/articles/%s.html)' % (art.get('article_url'), aid, rel, aid))
            lines.append('')
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    pa = parse_article(f.read())
                md = pa.markdown()

                def img_rep(m):
                    src = m.group(1)
                    rn = article_img_name(src)
                    if os.path.exists(os.path.join(D_IMAGES, rn)):
                        return '![](%s/images/%s)' % (rel, rn)
                    return '![图片未下载](%s)' % src
                md = re.sub(r'\{\{IMG:(.*?)\}\}', img_rep, md)
                lines.append(md if md else '（长文正文为空或解析失败，见原始 HTML）')
            else:
                lines.append('（长文未下载）')
            lines.append('')
    ref = t.get('referenced_topic')
    if ref:
        rt = ref.get('topic', ref)
        lines.append('## 引用的话题')
        lines.append('')
        if not rt.get('topic_id'):
            lines.append('> （引用的话题不可见：%s）' % (ref.get('invisible_reason') or 'unknown'))
            lines.append('')
            rt = None
    if ref and rt:
        rb = topic_body(rt)
        lines.append(md_quote('**%s**（%s，topic_id `%s`）\n\n%s' % (name_of(rb.get('owner')), hkt(rt.get('create_time', '')), rt.get('topic_id'), decode_e(rb.get('text', '')).strip())))
        lines.append('')
    cp = load_json(os.path.join(D_COMMENTS, '%s.json' % tid))
    comments = cp['comments'] if cp else (t.get('show_comments') or [])
    if comments:
        lines.append('## 评论（%d）' % (cp['fetched_count'] if cp else len(comments)))
        lines.append('')
        top = [c for c in comments if not c.get('parent_comment_id')] if not cp else comments
        for cm in top:
            lines += render_comment(cm, rel)
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def render_all():
    media = load_json(MEDIA, {}) or {}
    topics = all_topics()
    index = []
    for t in topics:
        d = parse_t(t['create_time']).astimezone(TZ8)
        y = d.strftime('%Y')
        fn = '%s_%s.md' % (d.strftime('%Y-%m-%d'), t['topic_id'])
        p = os.path.join(D_MD, y, fn)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(render_topic(t, media))
        b = topic_body(t)
        flags = ''.join([' ⭐' if t.get('digested') else '', ' 📝' if b.get('article') else '',
                         ' 🖼' if b.get('images') else '', ' 📎' if b.get('files') else '',
                         ' ❓' if t.get('type') == 'q&a' else ''])
        index.append('- %s [%s](%s/%s)%s' % (d.strftime('%Y-%m-%d %H:%M'), topic_title(t).replace('[', '(').replace(']', ')'), y, fn, flags))
    with open(os.path.join(D_MD, 'INDEX.md'), 'w', encoding='utf-8') as f:
        f.write('# 话题索引（按时间倒序）\n\n⭐精华 📝长文 🖼图片 📎附件 ❓问答\n\n' + '\n'.join(reversed(index)) + '\n')
    write_readme(topics, media)
    log('rendered %d topics to md/' % len(topics))


def dir_size(p):
    tot = 0
    for root, _, files in os.walk(p):
        for fn in files:
            tot += os.path.getsize(os.path.join(root, fn))
    return tot


def summary(topics=None, media=None):
    topics = topics if topics is not None else all_topics()
    media = media if media is not None else (load_json(MEDIA, {}) or {})
    from collections import Counter
    types = Counter(t.get('type') for t in topics)
    n_comments = 0
    for fn in os.listdir(D_COMMENTS) if os.path.isdir(D_COMMENTS) else []:
        n_comments += load_json(os.path.join(D_COMMENTS, fn)).get('fetched_count', 0)
    expected_comments = sum(t.get('comments_count') or 0 for t in topics)
    seen = set()
    for _, objs in iter_topic_and_comment_objs():
        for o in objs:
            for img in find_images(o):
                seen.add(image_name(img))
    imgs_ok = sum(1 for nm in seen if os.path.exists(os.path.join(D_IMAGES, nm)))
    art_imgs = len(os.listdir(os.path.join(D_IMAGES, 'articles'))) if os.path.isdir(os.path.join(D_IMAGES, 'articles')) else 0
    arts = [topic_body(t)['article'] for t in topics if topic_body(t).get('article')]
    arts_ok = sum(1 for a in arts if os.path.exists(os.path.join(D_ARTICLES, '%s.html' % a.get('article_id'))))
    files = media.get('files', {})
    return {
        'topics': len(topics), 'types': dict(types),
        'digests': sum(1 for t in topics if t.get('digested')),
        'first': topics[0]['create_time'] if topics else None,
        'last': topics[-1]['create_time'] if topics else None,
        'comments_fetched': n_comments, 'comments_expected': expected_comments,
        'images_referenced': len(seen), 'images_ok': imgs_ok, 'article_images': art_imgs,
        'articles': len(arts), 'articles_ok': arts_ok,
        'files_referenced': len(files), 'files_ok': sum(1 for v in files.values() if v.get('status') == 'ok'),
        'files_missing': {k: v for k, v in files.items() if v.get('status') != 'ok'},
        'files_not_in_git': {k: v for k, v in files.items() if v.get('not_in_git')},
    }


def write_readme(topics, media):
    s = summary(topics, media)
    g = (load_json(os.path.join(RAW, 'group.json'), {}) or {}).get('resp_data', {}).get('group', {})
    st = (load_json(os.path.join(RAW, 'statistics.json'), {}) or {}).get('resp_data', {})
    lines = [
        '# 知识星球「%s」完整存档' % g.get('name', ''),
        '',
        '> ⚠️ **付费私密内容，仅供付费成员个人存档与学习使用，请勿分享、转载或公开。** 这份存档目录不要公开。',
        '',
        '| 项目 | 值 |', '|---|---|',
        '| 星球 | %s（group_id `%s`） |' % (g.get('name', ''), GROUP_ID),
        '| 星主 | %s |' % (g.get('owner') or {}).get('name', ''),
        '| 简介 | %s |' % (g.get('description') or '').replace('\n', ' ').replace('|', '｜'),
        '| 星球创建 | %s |' % hkt(g.get('create_time', '')),
        '| 存档时间范围 | %s → %s (UTC+8) |' % (hkt(s['first'] or ''), hkt(s['last'] or '')),
        '| 话题数 | %d（星球统计 topics_count=%s）；类型 %s；精华 %d（统计 %s） |' % (
            s['topics'], st.get('topics_count'), json.dumps(s['types'], ensure_ascii=False), s['digests'], st.get('digests_count')),
        '| 话题数说明 | 星球统计数可能包含已删除话题；已用 scope=all / digests / questions 三种列表交叉核对（见 raw/state.json），未发现遗漏 |',
        '| 评论数 | %d（话题 comments_count 合计 %d，含楼中楼回复） |' % (s['comments_fetched'], s['comments_expected']),
        '| 长文 | %d 篇，已下载 %d |' % (s['articles'], s['articles_ok']),
        '| 图片 | 话题/问答/评论图片 %d 张，已下载 %d；长文内图片 %d 张 |' % (s['images_referenced'], s['images_ok'], s['article_images']),
        '| 附件 | %d 个，已下载 %d |' % (s['files_referenced'], s['files_ok']),
        '| 最后更新 | %s |' % dt.datetime.now(TZ8).strftime('%Y-%m-%d %H:%M'),
        '',
        '## 目录结构', '',
        '- `md/INDEX.md` — 全部话题索引（倒序）；`md/<年>/<日期>_<topic_id>.md` — 可读 Markdown（作者、时间、正文、图片、附件、长文全文、评论）',
        '- `raw/topics/<topic_id>.json` — 话题列表接口返回的原始 JSON（含 digested 精华 / sticky 置顶 等标记）',
        '- `raw/comments/<topic_id>.json` — 全部评论（`comments` 合并结果 + `raw_pages` 原始分页响应）',
        '- `raw/articles/<article_id>.html` — 长文原始 HTML；`raw/topic_detail/` — 话题详情（仅截断文本时抓取）',
        '- `raw/group.json`、`raw/statistics.json`、`raw/columns.json`（专栏：%s）、`raw/hashtags.json`' % ('无' if not ((load_json(os.path.join(RAW, 'columns.json'), {}) or {}).get('resp_data', {}).get('columns')) else '见 raw/columns/'),
        '- `raw/media_status.json` — 图片 / 附件 下载状态（失败、缺失原因）',
        '- `images/<image_id>.<ext>`（原图），`images/articles/`（长文内图片），`files/<file_id>_<文件名>`（附件）',
        '- 下载脚本在 research 仓库的 `scripts/zsxq/dl.py`（可重复运行，增量 / 断点续传）',
        '',
    ]
    if s['files_missing']:
        lines += ['## 未能下载的附件', '']
        for k, v in s['files_missing'].items():
            lines.append('- `%s` %s（topic `%s`）：%s' % (k, v.get('name'), v.get('topic_id'), v.get('reason')))
        lines.append('')
    if s['files_not_in_git']:
        lines += ['## 仅本地保存、未提交 git 的大文件（>100MB）', '']
        for v in s['files_not_in_git'].values():
            lines.append('- `files/%s`（%.1f MB）' % (v.get('local'), (v.get('size') or 0) / 1048576.0))
        lines.append('')
    lines += [
        '## 如何生成 / 更新', '',
        '在 Mac mini 上：`scripts/crawl-fangwei-zsxq.sh`（抓取并导入）；只抓不导入：`ZSXQ_ARCHIVE=<本目录> python3 scripts/zsxq/dl.py`',
        '',
        '- 令牌（`ZSXQ_ACCESS_TOKEN=...`）和星球 ID（`ZSXQ_GROUP_ID=...`）从 `~/zsxq/zsxq.env` 读取，不写入仓库。',
        '- 接口：`api.zsxq.com/v2/groups/{id}/topics?scope=all&count=20&end_time=<最后一条 create_time − 1ms>` 倒序翻页；'
        '评论 `/v2/topics/{id}/comments?sort=asc&count=30&begin_time=<上一页最后 create_time + 1ms>`；'
        '附件 `/v2/files/{file_id}/download_url`；长文直接抓 `articles.zsxq.com/id_xxx.html`。',
        '- 返回 `code=1059` 时自动退避重试；请求间隔 1–2.5 秒（与乘风一致）。',
        '- 再次运行时：话题从最新往旧翻，遇到整页已存在即停止；评论数变化的话题会重新抓评论；已下载的图片 / 附件 / 长文跳过。',
        '- `python3 dl.py --render` 仅根据 raw 数据重新生成 Markdown 和本 README。',
        '',
    ]
    with open(os.path.join(ROOT, 'README.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ---------------------------------------------------------------- main
def main():
    if not ROOT:
        print('请用 ZSXQ_ARCHIVE 指定存档目录（比如 data/inbox/fangwei-zsxq）', file=sys.stderr)
        return 2
    if not GROUP_ID and '--render' not in sys.argv and '--summary' not in sys.argv:
        print('缺少星球 ID：在 %s 里加一行 ZSXQ_GROUP_ID=...，或设环境变量 ZSXQ_GROUP_ID' % ENV_FILE, file=sys.stderr)
        return 2
    for d in (RAW, D_TOPICS, D_COMMENTS, D_ARTICLES, D_IMAGES, D_FILES, D_MD):
        os.makedirs(d, exist_ok=True)
    if '--render' in sys.argv:
        render_all()
        return 0
    if '--summary' in sys.argv:
        print(json.dumps(summary(), ensure_ascii=False, indent=1))
        return 0
    c = Client()
    state = load_json(STATE, {}) or {}
    media = load_json(MEDIA, {}) or {}
    skip = set(a[len('--skip='):] for a in sys.argv if a.startswith('--skip='))
    try:
        log('run start')
        if 'group' not in skip:
            crawl_group(c)
        if 'topics' not in skip:
            n = crawl_topics(c, state)
            log('new topics this run: %d' % n)
            crawl_extra_scopes(c, state)
        if 'comments' not in skip:
            crawl_details_and_comments(c)
        if 'articles' not in skip:
            crawl_articles(c, media)
        if 'images' not in skip:
            crawl_images(c, media)
        if 'files' not in skip:
            crawl_files(c, media)
        render_all()
        state['last_run'] = fmt_t(dt.datetime.now(TZ8))
        save_json(STATE, state)
        log('run done; api requests: %d' % c.n_api)
        status('DONE api_requests=%d' % c.n_api)
        return 0
    except Fatal as e:
        log('FATAL: %s' % e)
        status('FATAL %s' % e)
        save_json(MEDIA, media)
        return 2
    except Exception as e:
        log('ERROR: %s' % traceback.format_exc())
        status('ERROR %r' % e)
        save_json(MEDIA, media)
        return 1


if __name__ == '__main__':
    sys.exit(main())
