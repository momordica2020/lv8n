# -*- coding: utf-8 -*-
"""
绿坝娘维基(greendam.fandom.com/zh) 静态镜像脚本
=====================================================

功能：
  1. 通过 MediaWiki API 拉取全部页面清单（文章/分类/文件/模板/模块）及最后修改时间
  2. 渲染每个页面的正文 HTML，下载引用的图片到本地
  3. 将站内链接与图片改写为本地相对路径；重定向页自动生成跳转页，
     页面内指向重定向标题的链接直接改写为目标页面
  4. 每个页面顶部显示分类标签（tag）；移除指向 fandom.com/wikia.com 的外链
     （含编辑链接、图片外链包裹）
  5. 文件页(File:)渲染大图与文件信息（文件内容不在 parse 文本里，改用
     imageinfo 获取并下载原图）
  6. 生成主页 index.html（内嵌维基主页“Greendam Wiki”内容）、全页面索引
     allpages.html 与客户端搜索数据 search-data.json；每个页面顶部固定搜索栏
  7. 页脚显示镜像更新日期；通过 state.json 支持增量更新；通过
     title-map.json 记录标题->文件名映射，映射变化时自动全量重渲染
  8. 依赖传播：模板/模块页被编辑后自动重渲染引用它的页面（transcludedin）；
     文件被重新上传后自动重渲染使用它的页面（fileusage），刷新缩略图
  9. 孤儿清理：每次运行后删除不再被当前页面引用的页面/图片/媒体文件，
     并同步清理 state.json / image-map.json 中的失效记录

用法：
  python mirror_greendam.py            # 全量/增量运行（默认含模板页与孤儿清理）
  python mirror_greendam.py --force    # 强制重新渲染所有页面
  python mirror_greendam.py --limit 5  # 仅渲染前 5 个页面（用于测试）
  python mirror_greendam.py --no-templates       # 不镜像模板页与模块页
  python mirror_greendam.py --no-cleanup         # 跳过孤儿文件清理
"""
import argparse
import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

BASE = "https://greendam.fandom.com/zh"
API = BASE + "/api.php"
UA = "GreenDamWikiMirror/1.0 (static mirror script; contact: local)"

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki")
PAGES_DIR = os.path.join(OUT_DIR, "pages")
IMAGES_DIR = os.path.join(OUT_DIR, "images")
MEDIA_DIR = os.path.join(OUT_DIR, "media")
STATE_FILE = os.path.join(OUT_DIR, "state.json")
SEARCH_FILE = os.path.join(OUT_DIR, "search-data.json")
IMAGE_MAP_FILE = os.path.join(OUT_DIR, "image-map.json")
TITLE_MAP_FILE = os.path.join(OUT_DIR, "title-map.json")

NAMESPACES = [0, 14, 6]           # 文章 / 分类 / 文件
EXTRA_NAMESPACES = [10, 828]      # 模板 / 模块
MIRROR_VERSION = "7"              # 模板/功能版本，变更后自动全量重渲染

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})

# 运行时状态
image_map = {}
TITLE_MAP = {}
REDIRECT_MAP = {}
FILE_INFOS = {}
MAIN_PAGE = "Greendam Wiki"
UPDATE_TIME = ""

# 文件页链接标题里常见的“(207 KB)”等尺寸后缀
SIZE_SUFFIX_RE = re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:KB|MB|GB|B)\)\s*$")
FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def ensure_dirs():
    for d in (PAGES_DIR, IMAGES_DIR, MEDIA_DIR):
        os.makedirs(d, exist_ok=True)


def api_call(params, tries=6):
    params.setdefault("format", "json")
    for i in range(tries):
        try:
            r = SESSION.get(API, params=params, timeout=90)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if i == tries - 1:
                return {}
            time.sleep(3 * (i + 1))


def get_all_pages(namespaces=None):
    """返回 {title: lastmod_timestamp}，覆盖指定命名空间。"""
    result = {}
    for ns in (namespaces or NAMESPACES):
        cont = {}
        while True:
            params = {
                "action": "query",
                "generator": "allpages",
                "gapnamespace": ns,
                "gaplimit": 500,
                "prop": "info|revisions",
                "rvprop": "timestamp",
                "inprop": "url",
            }
            params.update(cont)
            data = api_call(params)
            pages = data.get("query", {}).get("pages", {})
            for p in pages.values():
                title = p.get("title", "")
                ts = ""
                revs = p.get("revisions") or []
                if revs:
                    ts = revs[0].get("timestamp", "")
                result[title] = ts
            if "continue" in data:
                cont = data["continue"]
            else:
                break
    return result


def get_main_page():
    """从 siteinfo 获取维基主页标题。"""
    data = api_call({"action": "query", "meta": "siteinfo", "siprop": "general"})
    return data.get("query", {}).get("general", {}).get("mainpage") or "Greendam Wiki"


def get_redirect_map(titles):
    """批量查询重定向，返回 {重定向标题: 最终目标标题}。"""
    raw = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        data = api_call({"action": "query", "redirects": "1", "titles": "|".join(chunk)})
        for r in data.get("query", {}).get("redirects", []):
            frm, to = r.get("from", ""), r.get("to", "")
            if frm and to:
                raw[frm] = to

    def final(t, seen=None):
        seen = seen or set()
        if t in seen or t not in raw:
            return t
        seen.add(t)
        return final(raw[t], seen)

    for k in list(raw):
        raw[k] = final(raw[k])
    return raw


def get_file_infos(titles):
    """批量获取 File: 页面的原始文件信息。"""
    infos = {}
    files = [t for t in titles if t.startswith("File:")]
    for i in range(0, len(files), 50):
        chunk = files[i:i + 50]
        data = api_call({
            "action": "query",
            "titles": "|".join(chunk),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|timestamp",
        })
        for p in data.get("query", {}).get("pages", {}).values():
            ii = (p.get("imageinfo") or [{}])[0]
            if ii:
                infos[p.get("title", "")] = ii
    return infos


def slug(title):
    """在文件名中安全地表示标题（保留 Unicode，替换 Windows 非法字符）。
    过长的标题（超出 Windows 路径限制）退化为哈希命名，保证确定性。"""
    safe = re.sub(r'[\\/:*?"<>|]', '_', title)
    safe = re.sub(r'\s+', '_', safe.strip())
    if len(safe.encode("utf-8")) <= 120:
        return safe
    return hashlib.md5(title.encode("utf-8")).hexdigest()[:24] + "-" + safe[-40:]


def build_title_map(all_titles):
    """为所有标题生成互不冲突（含大小写冲突）的确定性文件名。"""
    from collections import defaultdict
    groups = defaultdict(list)
    for t in all_titles:
        base = slug(t)
        groups[base.lower()].append((t, base))
    mapping = {}
    for key, items in groups.items():
        if len(items) == 1:
            mapping[items[0][0]] = items[0][1]
        else:
            for i, (t, base) in enumerate(sorted(items, key=lambda x: x[0])):
                mapping[t] = hashlib.md5(t.encode("utf-8")).hexdigest()[:16] + "-" + str(i)
    TITLE_MAP.clear()
    TITLE_MAP.update(mapping)


def fname(title):
    """返回标题对应的文件名（不含扩展名）。"""
    return TITLE_MAP.get(title, slug(title))


def load_title_map():
    if os.path.exists(TITLE_MAP_FILE):
        with open(TITLE_MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_title_map(m):
    with open(TITLE_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)


def page_file(title):
    return os.path.join(PAGES_DIR, fname(title) + ".html")


def download(url, dest):
    """下载 url 到 dest，返回是否成功。"""
    if url.startswith("//"):
        url = "https:" + url
    if os.path.exists(dest):
        return True
    try:
        r = SESSION.get(url, timeout=90)
        r.raise_for_status()
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, dest)
        return True
    except Exception:
        return False


MIME_EXT = {
    "audio/wav": ".wav", "audio/wave": ".wav", "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
    "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/x-flac": ".flac",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-msvideo": ".avi",
    "video/webm": ".webm", "video/ogg": ".ogv", "application/ogg": ".ogg",
    "image/webp": ".webp",
    "application/pdf": ".pdf", "application/x-pdf": ".pdf",
    "application/zip": ".zip", "application/x-zip-compressed": ".zip",
    "application/x-7z-compressed": ".7z", "application/x-rar-compressed": ".rar",
    "text/plain": ".txt",
    "application/rtf": ".rtf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/x-shockwave-flash": ".swf",
    "application/epub+zip": ".epub",
    "font/ttf": ".ttf", "application/x-font-ttf": ".ttf",
    "font/otf": ".otf", "application/x-font-otf": ".otf",
}


def ext_from_mime(mime):
    return MIME_EXT.get((mime or "").lower(), "")


def _asset_ext(url, mime):
    ext = ext_from_mime(mime)
    if ext:
        return ext
    m = re.search(r'\.([a-z0-9]{1,5})(?:/|$)', url, re.I)
    if m:
        e = "." + m.group(1).lower()
        if e in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico",
                 ".wav", ".mp3", ".mp4", ".ogg", ".flac", ".avi", ".mov", ".m4a", ".webm",
                 ".pdf", ".zip", ".7z", ".rar", ".txt", ".rtf", ".doc", ".docx",
                 ".xls", ".xlsx", ".ppt", ".pptx", ".swf", ".epub", ".ttf", ".otf"):
            return e
    return ""


def local_media(url, mime=None):
    """下载音频/视频到 media/ 并返回相对 pages/ 的路径，失败返回 None。"""
    if url.startswith("../media/") or url.startswith("media/"):
        return url
    key = url
    if key in image_map and os.path.exists(os.path.join(MEDIA_DIR, os.path.basename(image_map[key]))):
        return image_map[key]
    ext = _asset_ext(url, mime) or ".bin"
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    dest = os.path.join(MEDIA_DIR, digest + ext)
    if not download(url, dest):
        return None
    rel = os.path.relpath(dest, PAGES_DIR).replace("\\", "/")
    image_map[key] = rel
    return rel


def local_image(url, mime=None):
    """下载图片并返回本地相对路径（相对 pages/ 目录），失败返回 None。"""
    if url.startswith("../") or url.startswith("images/"):
        return url  # 已是本地路径
    key = url
    if key in image_map and os.path.exists(os.path.join(IMAGES_DIR, os.path.basename(image_map[key]))):
        return image_map[key]
    ext = ""
    m = re.search(r'\.(jpe?g|png|gif|svg|webp|bmp|ico)(?:/|$)', url, re.I)
    if m:
        ext = "." + m.group(1).lower()
    if not ext:
        ext = _asset_ext(url, mime)
    if not ext:
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico"):
        ext = ".img"
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    dest_name = digest + ext
    dest = os.path.join(IMAGES_DIR, dest_name)
    if not download(url, dest):
        return None
    rel = os.path.relpath(dest, PAGES_DIR).replace("\\", "/")
    image_map[key] = rel
    return rel


IMG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
MEDIA_TAG_RE = re.compile(r'<(audio|video|source)\b[^>]*>', re.IGNORECASE)
SRC_RE = re.compile(r'\bsrc="([^"]+)"', re.IGNORECASE)
SRCSET_RE = re.compile(r'\bsrcset="[^"]*"', re.IGNORECASE)
DATA_SRC_RE = re.compile(r'\sdata-src="[^"]*"', re.IGNORECASE)
LINK_RE = re.compile(r'href="(/zh)?/wiki/([^"#]*)(#[^"]*)?"', re.IGNORECASE)
FULL_LINK_RE = re.compile(r'href="https?://(?:greendam\.fandom\.com|www\.fandom\.com)?/?(zh)?/wiki/([^"#]*)(#[^"]*)?"', re.IGNORECASE)
A_TAG_RE = re.compile(r'<a\b[^>]*>', re.IGNORECASE)
A_FULL_RE = re.compile(r'<a\b[^>]*>.*?</a>', re.IGNORECASE | re.DOTALL)
HREF_ATTR_RE = re.compile(r'\bhref="([^"]*)"', re.IGNORECASE)
ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
EDITSECTION_RE = re.compile(r'<span\b[^>]*class="[^"]*\bmw-editsection\b[^"]*"[^>]*>.*?</span>', re.IGNORECASE | re.DOTALL)
FANDOM_HOST_RE = re.compile(r'(^|\.)(fandom\.com|wikia\.com|wikia\.nocookie\.net|fandom\.org)$', re.IGNORECASE)

# 页面 HTML 中对本地 images/media 的引用（src/href/poster/data-src），用于孤儿清理
ASSET_REF_RE = re.compile(
    r'\b(?:src|href|poster|data-src)="((?:\.\./)?(?:images|media)/([^"?]+))"',
    re.IGNORECASE,
)
# 图片下载失败时使用的 1x1 透明占位图，避免离线镜像残留 fandom 远程外链
PLACEHOLDER_IMG = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAAAAAAALAAAAAABAAEAAAIBRAA7"

SPECIAL_MAP = {
    "Special:所有页面": "../allpages.html",
    "Special:全部页面": "../allpages.html",
    "Special:AllPages": "../allpages.html",
    "Special:最近更改": "../allpages.html",
    "Special:搜索": "../allpages.html",
    "Special:文件列表": "../allpages.html",
    "Special:用户列表": "../allpages.html",
    "Special:页面分类": "../index.html",
    "Special:统计": "../index.html",
}


def special_target(title):
    if not title:
        return None
    t = title.strip()
    if t in SPECIAL_MAP:
        return SPECIAL_MAP[t]
    if t.startswith("Special:链入页面"):
        return "../allpages.html"
    return None


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def resolve_title(href_title, title_attr, all_titles_set):
    """在标题集合中解析真实标题；返回 None 表示未收录。"""
    for raw in (href_title, title_attr):
        if not raw:
            continue
        t = html_lib.unescape(raw).strip()
        t = t.split("?", 1)[0]
        seen = set()

        def add(v):
            v = v.strip()
            if v and v not in seen:
                seen.add(v)

        add(t)
        t2 = SIZE_SUFFIX_RE.sub("", t)
        add(t2)
        ts = t2.replace("_", " ")
        add(ts)
        for v in (t2, ts):
            if v and v[0].isalpha():
                add(v[0].upper() + v[1:])
        if FILE_EXT_RE.search(t2):
            if not t2.startswith("File:"):
                add("File:" + t2)
            else:
                add(t2[len("File:"):])
        for v in seen:
            if v in all_titles_set:
                return v
    return None


def strip_fandom_links(html):
    """移除指向 fandom.com / wikia.com 的外链与编辑链接。
    图片外链被解包（保留图片），文字外链被去链接化（保留文字）。"""
    html = EDITSECTION_RE.sub("", html)

    def a_sub(m):
        tag = m.group(0)
        hm = HREF_ATTR_RE.search(tag)
        if not hm:
            return tag
        url = html_lib.unescape(hm.group(1))
        mu = re.match(r'https?://([^/]+)', url)
        host = mu.group(1).lower() if mu else ""
        if host.startswith("www."):
            host = host[4:]
        if host and FANDOM_HOST_RE.search(host):
            inner = tag[tag.find(">") + 1: tag.rfind("</a>")]
            return inner
        return tag

    return A_FULL_RE.sub(a_sub, html)


def rewrite_html(title, html, all_titles_set):
    """改写图片与站内链接为本地相对路径，并清理外链。"""
    def img_sub(m):
        tag = m.group(0)
        attrs = dict(ATTR_RE.findall(tag))
        src = attrs.get("src", "")
        data_src = html_lib.unescape(attrs.get("data-src", ""))
        tag = SRCSET_RE.sub("", tag)
        tag = DATA_SRC_RE.sub("", tag)
        tag = tag.replace('class="lazyload"', "")

        def src_sub(sm):
            raw = sm.group(1)
            if raw.startswith("data:"):
                return sm.group(0)
            url = raw.replace("&amp;", "&")
            loc = local_image(url)
            if loc is None:
                return 'src="%s"' % PLACEHOLDER_IMG
            return 'src="%s"' % loc

        if data_src:
            loc = (
                data_src
                if data_src.startswith("../") or data_src.startswith("images/")
                else local_image(data_src)
            )
            if loc:
                if not src or src.startswith("data:"):
                    tag = SRC_RE.sub(lambda sm: 'src="%s"' % loc, tag, count=1)
                else:
                    tag = SRC_RE.sub(src_sub, tag, count=1)
                return re.sub(r"\s+>", ">", tag)
        return re.sub(r"\s+>", ">", SRC_RE.sub(src_sub, tag))

    html = IMG_RE.sub(img_sub, html)

    # 音频/视频：<audio>/<video>/<source> 的 src 本地化到 media/
    def media_sub(m):
        tag = m.group(0)

        def src_sub(sm):
            raw = sm.group(1)
            if raw.startswith(("data:", "blob:", "../", "media/")):
                return sm.group(0)
            url = html_lib.unescape(raw).replace("&amp;", "&")
            loc = local_media(url)
            if loc is None:
                return ""  # 下载失败：移除远程 src，避免离线镜像外链
            return 'src="%s"' % loc

        return SRC_RE.sub(src_sub, tag)

    html = MEDIA_TAG_RE.sub(media_sub, html)

    def a_sub(m):
        tag = m.group(0)
        open_end = tag.find(">")
        opening = tag[:open_end + 1]
        inner = tag[open_end + 1: tag.rfind("</a>")]
        hm = HREF_ATTR_RE.search(opening)
        if not hm:
            return tag
        lm = LINK_RE.search(opening) or FULL_LINK_RE.search(opening)
        if not lm:
            return tag
        href_title = urllib.parse.unquote(lm.group(2)).split("?", 1)[0]
        frag = lm.group(3) or ""
        attrs = dict(ATTR_RE.findall(opening))
        title_attr = html_lib.unescape(attrs.get("title", ""))
        canon = resolve_title(href_title, title_attr, all_titles_set)
        if canon is not None:
            canon = REDIRECT_MAP.get(canon, canon)  # 跟随重定向到最终目标
            if canon in all_titles_set:
                new_href = fname(canon) + ".html" + frag
                return HREF_ATTR_RE.sub('href="%s"' % new_href, opening, count=1) + inner + "</a>"
            # 重定向链的最终目标已删除：与红链一致，解链为纯文本
            return inner
        spec = special_target(title_attr) or special_target(href_title)
        if spec:
            return HREF_ATTR_RE.sub('href="%s"' % spec, opening, count=1) + inner + "</a>"
        if href_title in all_titles_set:
            # 未规范化匹配成功但确已收录：保留原链接
            new_href = fname(href_title) + ".html" + frag
            return HREF_ATTR_RE.sub('href="%s"' % new_href, opening, count=1) + inner + "</a>"
        # 未收录（Special/User/已删除页/红链）：解链为纯文本，避免死链
        return inner

    html = A_FULL_RE.sub(a_sub, html)
    return strip_fandom_links(html)


def render_page(title):
    """渲染单个页面，返回 (title, ok, text_html, categories)。"""
    data = api_call({
        "action": "parse",
        "page": title,
        "prop": "text|categories",
    })
    if "error" in data:
        return (title, False)
    parse = data.get("parse", {})
    text_html = parse.get("text", {}).get("*", "")
    text_html = re.sub(r'<div class="mw-parser-output">\s*', '', text_html)
    text_html = re.sub(r'</div>\s*$', '', text_html)
    text_html = re.sub(r'<!--.*?-->', '', text_html, flags=re.S)
    cats = [c.get("*", "") for c in parse.get("categories", []) if c.get("*")]
    return (title, True, text_html, cats)


def render_tags(cats, all_titles_set):
    """分类标签 -> 页面顶部的 tag 块；未收录（红链）分类显示为纯文本。"""
    if not cats:
        return ""
    items = []
    for c in cats:
        t = c if c.startswith("Category:") else "Category:" + c
        if t in all_titles_set:
            items.append('<a class="tag" href="%s">%s</a>' % (html_escape(fname(t) + ".html"), html_escape(c)))
        else:
            items.append('<span class="tag tag-missing">%s</span>' % html_escape(c))
    return '<div class="pagetags"><span class="tags-label">标签：</span>' + "".join(items) + "</div>"


def fmt_size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.1f KB" % (n / 1024)
    return "%.1f MB" % (n / 1048576)


def render_file_page(title, info, loc):
    """File 页正文：大图 + 文件信息表。"""
    name = title[len("File:"):] if title.startswith("File:") else title
    w = info.get("width", "")
    h = info.get("height", "")
    dim = ("%s × %s px" % (w, h)) if w and h else ""
    rows = [
        ("文件名", name),
        ("大小", fmt_size(info.get("size", ""))),
        ("尺寸", dim),
        ("类型", info.get("mime", "")),
    ]
    trs = "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (html_escape(k), html_escape(v))
        for k, v in rows if v
    )
    mime = info.get("mime", "")
    if mime.startswith("image/"):
        img = '<div class="filepage-img"><img src="%s" alt="%s"></div>' % (loc, html_escape(name))
    elif mime.startswith("audio/"):
        img = '<div class="filepage-img"><audio controls preload="metadata" src="%s"></audio></div>' % loc
    elif mime.startswith("video/"):
        img = '<div class="filepage-img"><video controls preload="metadata" src="%s" style="max-width:100%%"></video></div>' % loc
    else:
        img = (
            '<div class="filepage-img filepage-nonimage">该文件不是图片，无法预览。'
            '<br><a class="filepage-download" href="%s" download>下载原文件</a></div>'
        ) % loc
    return (
        '<div class="filepage">'
        + img
        + '<table class="filepage-meta">%s</table>'
        + '</div>'
    ) % trs


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - 绿坝娘维基镜像</title>
<link rel="stylesheet" href="../styles.css?v=20260807">
<script src="../theme.js?v=20260807"></script>
</head>
<body>
<header class="topbar">
  <a class="brand" href="../index.html">绿坝娘维基镜像</a>
  <div class="search-wrap">
    <input id="site-search" type="text" placeholder="搜索页面标题..." autocomplete="off">
    <div id="search-suggest" class="search-suggest"></div>
  </div>
  <button id="theme-toggle" class="theme-toggle" type="button" title="切换日/夜主题" aria-label="切换主题">🌙</button>
  <div class="crumb">{title}</div>
</header>
<main class="content">
<article class="article">
{content}
</article>
</main>
<footer class="foot">绿坝娘同好会项目工作组 · 静态镜像离线版 · 镜像更新于 {update}</footer>
<script src="../search.js?v=20260807" data-search="../search-data.json"></script>
</body>
</html>
"""

REDIRECT_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="0; url={target}">
<title>{title} - 绿坝娘维基镜像</title>
<link rel="stylesheet" href="../styles.css?v=20260807">
</head>
<body>
<header class="topbar">
  <a class="brand" href="../index.html">绿坝娘维基镜像</a>
  <div class="crumb">{title}（重定向）</div>
</header>
<main class="content">
<article class="article">
<p class="redirect-notice">这是一个重定向页面，正在跳转到 <a href="{target}">{shown}</a>…</p>
</article>
</main>
<footer class="foot">绿坝娘同好会项目工作组 · 静态镜像离线版 · 镜像更新于 {update}</footer>
<script>location.replace("{target}");</script>
</body>
</html>
"""

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>绿坝娘维基镜像</title>
<link rel="stylesheet" href="styles.css?v=20260807">
<script src="theme.js?v=20260807"></script>
</head>
<body>
<header class="topbar">
  <span class="brand">绿坝娘维基镜像</span>
  <div class="search-wrap">
    <input id="site-search" type="text" placeholder="搜索页面标题..." autocomplete="off">
    <div id="search-suggest" class="search-suggest"></div>
  </div>
  <button id="theme-toggle" class="theme-toggle" type="button" title="切换日/夜主题" aria-label="切换主题">🌙</button>
  <a class="allpages-link" href="allpages.html">全部页面</a>
</header>
<main class="content">
<article class="article">
{content}
</article>
</main>
<footer class="foot">绿坝娘同好会项目工作组 · 静态镜像离线版 · 镜像更新于 {update}</footer>
<script src="search.js?v=20260807" data-search="search-data.json"></script>
</body>
</html>
"""

ALLPAGES_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>全部页面 - 绿坝娘维基镜像</title>
<link rel="stylesheet" href="styles.css?v=20260807">
<script src="theme.js?v=20260807"></script>
</head>
<body>
<header class="topbar">
  <a class="brand" href="index.html">绿坝娘维基镜像</a>
  <div class="search-wrap">
    <input id="site-search" type="text" placeholder="搜索页面标题..." autocomplete="off">
    <div id="search-suggest" class="search-suggest"></div>
  </div>
  <button id="theme-toggle" class="theme-toggle" type="button" title="切换日/夜主题" aria-label="切换主题">🌙</button>
  <span class="crumb">全部页面 · 共 __LEN__ 个</span>
</header>
<main class="content">
  <div class="searchbox">
    <input id="list-filter" type="text" placeholder="在列表中筛选页面标题...">
    <span id="count"></span>
  </div>
  <ul class="pagelist" id="list"></ul>
</main>
<footer class="foot">绿坝娘同好会项目工作组 · 静态镜像离线版 · 镜像更新于 {update}</footer>
<script src="search.js" data-search="search-data.json"></script>
<script>
fetch('search-data.json').then(r=>r.json()).then(data=>{
  const list=document.getElementById('list');
  const inp=document.getElementById('list-filter');
  const count=document.getElementById('count');
  function render(f){
    const q=f.toLowerCase();
    const filtered=data.filter(x=>!q||x.title.toLowerCase().includes(q));
    list.innerHTML='';
    count.textContent='共 '+filtered.length+' 条';
    for(const x of filtered){
      const li=document.createElement('li');
      const a=document.createElement('a');
      a.href=x.url; a.textContent=x.title;
      li.appendChild(a); list.appendChild(li);
    }
  }
  inp.addEventListener('input',e=>render(e.target.value));
  render('');
});
</script>
</body>
</html>
"""


def write_redirect(title, target, all_titles_set):
    try:
        if target in all_titles_set:
            href = fname(target) + ".html"
            shown = target
        else:
            href = "../index.html"
            shown = target
        html = (
            REDIRECT_TEMPLATE
            .replace("{title}", html_escape(title))
            .replace("{target}", html_escape(href))
            .replace("{shown}", html_escape(shown))
            .replace("{update}", UPDATE_TIME)
        )
        with open(page_file(title), "w", encoding="utf-8") as f:
            f.write(html)
        return (title, True)
    except Exception as e:
        return (title, False, repr(e))


def write_page(title, all_titles_set):
    try:
        if title in REDIRECT_MAP:
            return write_redirect(title, REDIRECT_MAP[title], all_titles_set)
        res = render_page(title)
        if not res[1]:
            return (title, False)
        title, ok, text_html, cats = res
        # 文件页：parse 文本不含文件本体，用 imageinfo 渲染大图
        if title.startswith("File:"):
            info = FILE_INFOS.get(title) or {}
            url = info.get("url", "")
            mime = info.get("mime", "")
            if url:
                if mime.startswith("image/"):
                    loc = local_image(html_lib.unescape(url), mime)
                else:
                    loc = local_media(html_lib.unescape(url), mime)
                if loc:
                    text_html = render_file_page(title, info, loc) + text_html
        text_html = rewrite_html(title, text_html, all_titles_set)
        content = render_tags(cats, all_titles_set) + text_html
        html = (
            HTML_TEMPLATE
            .replace("{title}", html_escape(title))
            .replace("{content}", content)
            .replace("{update}", UPDATE_TIME)
        )
        with open(page_file(title), "w", encoding="utf-8") as f:
            f.write(html)
        return (title, True)
    except Exception as e:
        return (title, False, repr(e))


def read_main_article():
    """读取主页文章内容并改写为 wiki/ 根目录下的相对路径。"""
    fp = page_file(MAIN_PAGE)
    if not os.path.exists(fp):
        return "<p>主页内容暂不可用。</p>"
    with open(fp, encoding="utf-8") as f:
        c = f.read()
    m = re.search(r'<article class="article">(.*?)</article>', c, re.S)
    body = m.group(1) if m else ""
    body = re.sub(r'<div class="pagetags">.*?</div>', '', body, flags=re.S)
    body = re.sub(r'href="\.\./index\.html"', 'href="index.html"', body)
    body = re.sub(r'href="\.\./allpages\.html"', 'href="allpages.html"', body)
    body = re.sub(r'href="(?!#)(?![a-z][a-z0-9+.-]*:)(?!\.\.?/)([^"]+)"',
                  lambda mm: 'href="pages/%s"' % mm.group(1), body)
    body = re.sub(r'src="\.\./images/', 'src="images/', body)
    return body


def write_index(all_titles):
    items = []
    for t in sorted(all_titles, key=lambda x: x.lower()):
        items.append({
            "title": t,
            "url": "pages/" + fname(t) + ".html",
        })
    with open(SEARCH_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)

    html = ALLPAGES_TEMPLATE.replace("__LEN__", str(len(items))).replace("{update}", UPDATE_TIME)
    with open(os.path.join(OUT_DIR, "allpages.html"), "w", encoding="utf-8") as f:
        f.write(html)

    main_article = read_main_article()
    html = INDEX_TEMPLATE.replace("{content}", main_article).replace("{update}", UPDATE_TIME)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def load_image_map():
    global image_map
    if os.path.exists(IMAGE_MAP_FILE):
        with open(IMAGE_MAP_FILE, encoding="utf-8") as f:
            image_map = json.load(f)


def save_image_map():
    with open(IMAGE_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(image_map, f, ensure_ascii=False)


def get_transcluders(titles):
    """返回直接引用这些模板/模块页的页面标题集合（含各命名空间）。"""
    out = set()
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        cont = {}
        while True:
            params = {
                "action": "query",
                "titles": "|".join(chunk),
                "prop": "transcludedin",
                "tilimit": "max",
                "tinamespace": "0|6|10|14|828",
            }
            params.update(cont)
            data = api_call(params)
            for p in data.get("query", {}).get("pages", {}).values():
                for x in p.get("transcludedin") or []:
                    t = x.get("title", "")
                    if t:
                        out.add(t)
            if "continue" in data:
                cont = {k: v for k, v in (("ticontinue", data["continue"].get("ticontinue")),) if v}
            else:
                break
    return out


def get_file_usages(titles):
    """返回直接使用这些文件页的页面标题集合（含各命名空间）。"""
    out = set()
    files = [t for t in titles if t.startswith("File:")]
    for i in range(0, len(files), 50):
        chunk = files[i:i + 50]
        cont = {}
        while True:
            params = {
                "action": "query",
                "titles": "|".join(chunk),
                "prop": "fileusage",
                "fulimit": "max",
                "funamespace": "0|6|10|14|828",
            }
            params.update(cont)
            data = api_call(params)
            for p in data.get("query", {}).get("pages", {}).values():
                for x in p.get("fileusage") or []:
                    t = x.get("title", "")
                    if t:
                        out.add(t)
            if "continue" in data:
                cont = {k: v for k, v in (("fucontinue", data["continue"].get("fucontinue")),) if v}
            else:
                break
    return out


def page_changed(t, state, pages, file_infos):
    """判断页面是否需要重渲染：页面修订时间变化，或文件本体版本变化。"""
    cur = pages.get(t, "")
    prev = state.get(t)
    if isinstance(prev, dict):
        prev_page = prev.get("page")
        prev_file = prev.get("file")
    else:
        prev_page = prev_file = prev
    if prev_page != cur:
        return True
    if t.startswith("File:"):
        cur_file = (file_infos.get(t) or {}).get("timestamp", cur)
        return prev_file != cur_file
    return False


def state_value_for(t, pages, file_infos):
    """生成保存到 state.json 的值；File 页额外记录文件本体版本时间。"""
    cur = pages.get(t, "")
    if t.startswith("File:"):
        return {"page": cur, "file": (file_infos.get(t) or {}).get("timestamp", cur)}
    return cur


def propagate_dependencies(todo, all_titles_set, state, pages, file_infos):
    """把模板/文件变更传播到引用方（含模板链的传递闭包），返回新增标题集合。"""
    from collections import deque

    todo_set = set(todo)
    added = set()
    queue = deque(todo)
    checked_templates = set()
    checked_files = set()
    while queue:
        t = queue.popleft()
        if t.startswith(("Template:", "Module:")):
            if t in checked_templates:
                continue
            checked_templates.add(t)
            for u in get_transcluders([t]):
                if u in all_titles_set and u not in todo_set and u not in added:
                    added.add(u)
                    queue.append(u)
        elif t.startswith("File:"):
            if t in checked_files:
                continue
            checked_files.add(t)
            prev = state.get(t)
            prev_file = prev.get("file") if isinstance(prev, dict) else prev
            cur_file = (file_infos.get(t) or {}).get("timestamp", pages.get(t, ""))
            if prev_file != cur_file:
                for u in get_file_usages([t]):
                    if u in all_titles_set and u not in todo_set and u not in added:
                        added.add(u)
                        queue.append(u)
    return added


def collect_asset_refs():
    """扫描 wiki/ 下所有 HTML 引用的 images/media 文件名（只读）。"""
    refs = set()
    for root, _, files in os.walk(OUT_DIR):
        for fn in files:
            if not fn.lower().endswith(".html"):
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            for m in ASSET_REF_RE.finditer(content):
                refs.add(m.group(2))
    return refs


def cleanup_orphans(all_titles):
    """删除不再被当前页面引用的页面/图片/媒体文件，返回 {类别: [文件名]}。"""
    removed = {"pages": [], "images": [], "media": []}
    keep_pages = {fname(t) + ".html" for t in all_titles}
    for fn in os.listdir(PAGES_DIR):
        if fn.lower().endswith(".html") and fn not in keep_pages:
            try:
                os.remove(os.path.join(PAGES_DIR, fn))
                removed["pages"].append(fn)
            except OSError as e:
                print("      清理失败:", os.path.join(PAGES_DIR, fn), e)
    refs = collect_asset_refs()
    for d, key in ((IMAGES_DIR, "images"), (MEDIA_DIR, "media")):
        for fn in os.listdir(d):
            if fn not in refs:
                try:
                    os.remove(os.path.join(d, fn))
                    removed[key].append(fn)
                except OSError as e:
                    print("      清理失败:", os.path.join(d, fn), e)
    return removed


def prune_image_map():
    """删除 image-map 中指向已不存在文件的条目。"""
    global image_map
    drop = [
        k for k, rel in image_map.items()
        if not (
            os.path.exists(os.path.join(IMAGES_DIR, os.path.basename(rel)))
            or os.path.exists(os.path.join(MEDIA_DIR, os.path.basename(rel)))
        )
    ]
    for k in drop:
        del image_map[k]
    return len(drop)


def main():
    global UPDATE_TIME, MAIN_PAGE, REDIRECT_MAP, FILE_INFOS
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重新渲染所有页面")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 个页面（测试用）")
    ap.add_argument(
        "--include-templates",
        action="store_true",
        help="（已默认开启）同时镜像 Template/Module 命名空间页面",
    )
    ap.add_argument(
        "--no-templates",
        action="store_true",
        help="不镜像 Template/Module 命名空间页面（默认镜像）",
    )
    ap.add_argument(
        "--no-cleanup",
        action="store_true",
        help="跳过孤儿文件清理（默认每次运行后清理）",
    )
    args = ap.parse_args()

    ensure_dirs()
    load_image_map()
    UPDATE_TIME = datetime.now().strftime("%Y-%m-%d %H:%M")
    MAIN_PAGE = get_main_page()
    print("主页：%s" % MAIN_PAGE)

    print("[1/4] 拉取页面清单...")
    namespaces = list(NAMESPACES) + ([] if args.no_templates else EXTRA_NAMESPACES)
    pages = get_all_pages(namespaces)
    all_titles = list(pages.keys())
    all_titles_set = set(all_titles)
    print("      共 %d 个页面" % len(all_titles))
    if args.limit:
        all_titles = all_titles[:args.limit]
        all_titles_set = set(all_titles)
    build_title_map(list(pages.keys()))

    print("[2/4] 查询重定向与文件信息...")
    REDIRECT_MAP = get_redirect_map(all_titles) if not args.limit else {}
    FILE_INFOS = get_file_infos(all_titles)
    print("      重定向 %d 个，文件页 %d 个" % (len(REDIRECT_MAP), len(FILE_INFOS)))

    state = load_state()
    prev_title_map = load_title_map()
    map_changed = prev_title_map != TITLE_MAP
    version_changed = state.get("__mirror_version__") != MIRROR_VERSION
    if map_changed or version_changed:
        print("[!] 镜像映射/版本有变化，强制全量重渲染（保证旧链接与模板不失效）...")
        map_changed = True
    state["__mirror_version__"] = MIRROR_VERSION

    todo = []
    for t in all_titles:
        if args.force or map_changed or page_changed(t, state, pages, FILE_INFOS):
            todo.append(t)
    print("[3/4] 待渲染页面：%d 个" % len(todo))

    if not args.limit and not args.force and not map_changed and todo:
        extra = propagate_dependencies(todo, all_titles_set, state, pages, FILE_INFOS)
        if extra:
            todo = sorted(set(todo) | extra)
            print("      依赖传播新增 %d 个页面（模板/文件引用变化）" % len(extra))

    ok = 0
    fail = 0
    failed = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(write_page, t, all_titles_set): t for t in todo}
        done = 0
        for fut in as_completed(futs):
            res = fut.result()
            t = res[0]
            success = res[1]
            done += 1
            if success:
                ok += 1
                state[t] = state_value_for(t, pages, FILE_INFOS)
            else:
                fail += 1
                failed.append(res)
            if done % 100 == 0 or done == len(todo):
                print("      进度 %d/%d (成功 %d, 失败 %d)" % (done, len(todo), ok, fail))

    if args.limit:
        print("[4/4] --limit 测试模式，跳过索引/主页生成")
    else:
        print("[4/4] 生成索引与主页...")
        write_index(all_titles_set)
        if not args.no_cleanup:
            removed = cleanup_orphans(all_titles)
            total = sum(len(v) for v in removed.values())
            if total:
                print("      孤儿清理：删除页面 %d、图片 %d、媒体 %d" % (
                    len(removed["pages"]), len(removed["images"]), len(removed["media"])))
            else:
                print("      孤儿清理：无待清理文件")
            n_map = prune_image_map()
            if n_map:
                print("      image-map 失效条目清理：%d 个" % n_map)

        # 清理已删除页面的 state 记录
        stale_state = [k for k in state if not k.startswith("__") and k not in pages]
        for k in stale_state:
            del state[k]
        if stale_state:
            print("      state 中已删除页面记录清理：%d 个" % len(stale_state))

    save_state(state)
    save_image_map()
    save_title_map(TITLE_MAP)

    print("完成。成功 %d，失败 %d。输出目录：%s" % (ok, fail, OUT_DIR))
    for f in failed[:20]:
        print("  失败: %r" % (f,))
    if fail:
        print("提示：有页面渲染失败，可再次运行本脚本自动重试（已成功页面会跳过）。")
        sys.exit(1)


if __name__ == "__main__":
    main()
