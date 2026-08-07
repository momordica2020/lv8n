# -*- coding: utf-8 -*-
"""
离线修复静态镜像：链接重写 + 懒加载图片水合
============================================

背景：mirror_greendam.py 在增量更新时，如果全局标题->文件名映射发生变化
（例如新增了 slug 冲突的标题，导致相关页面被改为哈希命名），已渲染但未
改动的页面里的链接仍指向旧文件名；另外 Fandom 部分链接的百分号编码是坏
的（解码后出现 U+FFFD 乱码），以及 `?redirect=no` 等查询串被并进了文件名。

本脚本无需联网，直接基于磁盘文件与 search-data.json 重写：
  1. 每个 <a href="..."> 按以下顺序解析目标：
     - 目标文件已存在（区分大小写，再按大小写不敏感兜底并修正大小写）
     - 锚点 title 属性（去 HTML 实体、去“(xxx KB)”尺寸后缀、File: 前后缀、
       下划线/空格、首字母大小写）对照当前页面清单
     - <figure> 内 <img data-image-name="...">（画廊 info 图标链接）
     - Special:所有页面 -> ../index.html（本地全页面列表）
  2. <img> 若 src 是 1px 占位图且 data-src 是本地路径，则把 data-src 换成
     src，并移除 data-src / srcset / lazyload 类，使图片离线可见。

用法：
  python repair_mirror.py            # 修复 wiki/pages 下所有页面
  python repair_mirror.py --dry-run  # 只统计，不写文件
"""
import argparse
import html as html_lib
import json
import os
import re

WIKI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki")
PAGES_DIR = os.path.join(WIKI, "pages")
SEARCH_FILE = os.path.join(WIKI, "search-data.json")

A_TAG_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
FIGURE_RE = re.compile(r"<figure\b[^>]*>.*?</figure>", re.IGNORECASE | re.DOTALL)
ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
HREF_RE = re.compile(r'\bhref="([^"]*)"', re.IGNORECASE)
SRC_RE = re.compile(r'\bsrc="([^"]*)"', re.IGNORECASE)
DATA_SRC_RE = re.compile(r'\sdata-src="[^"]*"', re.IGNORECASE)
SRCSET_RE = re.compile(r'\ssrcset="[^"]*"', re.IGNORECASE)
SIZE_SUFFIX_RE = re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:KB|MB|GB|B)\)\s*$")
EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def parse_attrs(tag):
    return dict(ATTR_RE.findall(tag))


def load_index():
    with open(SEARCH_FILE, encoding="utf-8") as f:
        items = json.load(f)
    title_to_file = {}
    for x in items:
        title_to_file[x["title"]] = os.path.basename(x["url"])
    return title_to_file


def title_candidates(raw):
    """根据锚点 title 属性生成候选标题，覆盖常见变体。"""
    if not raw:
        return
    t = html_lib.unescape(raw).strip()
    seen = set()

    def add(v):
        if v and v not in seen:
            seen.add(v)
            yield v

    yield from add(t)
    t_no_size = SIZE_SUFFIX_RE.sub("", t)
    yield from add(t_no_size)
    t_space = t_no_size.replace("_", " ")
    yield from add(t_space)
    # MediaWiki 标题首字母大小写不敏感
    for v in (t_no_size, t_space):
        if v and v[0].isalpha():
            cap = v[0].upper() + v[1:]
            yield from add(cap)
    # 文件页：补/去 File: 前缀
    if EXT_RE.search(t_no_size) or "File:" in t_no_size:
        if not t_no_size.startswith("File:"):
            yield from add("File:" + t_no_size)
        else:
            yield from add(t_no_size[len("File:"):])


def resolve_target(href, title_attr, fig_names, title_to_file, files, files_lower):
    """返回应写入的新 href 文件名/路径；无需修改返回 None。"""
    if not href:
        return None
    base = href.split("#", 1)[0]
    if (
        not base
        or base.startswith(("#", "data:", "../", "./", "//", "javascript:"))
        or "://" in base
    ):
        return None
    b = os.path.basename(base.split("?", 1)[0])
    if not b:
        return None
    if b in files:
        return None  # 已经正确
    canon = files_lower.get(b.lower())
    if canon is not None and canon != b:
        return canon  # 仅大小写不同：修正为磁盘上的真实文件名
    for t in title_candidates(title_attr):
        f = title_to_file.get(t)
        if f:
            return f
    for name in fig_names:
        t = name if name.startswith("File:") else "File:" + name
        f = title_to_file.get(t)
        if f:
            return f
    if title_attr and html_lib.unescape(title_attr).strip() in (
        "Special:所有页面",
        "Special:全部页面",
        "Special:AllPages",
    ):
        return "../index.html"
    return None


def figure_spans(content):
    """返回 [(start, end, [data-image-name, ...])]，供画廊 info 链接解析。"""
    out = []
    for m in FIGURE_RE.finditer(content):
        names = [
            html_lib.unescape(a.get("data-image-name", ""))
            for a in (parse_attrs(t) for t in IMG_TAG_RE.findall(m.group(0)))
            if a.get("data-image-name")
        ]
        if names:
            out.append((m.start(), m.end(), names))
    return out


def rewrite_page(content, title_to_file, files, files_lower, stats):
    figs = figure_spans(content)

    def fig_names(pos):
        for s, e, names in figs:
            if s < pos < e:
                return names
        return []

    edits = []  # (start, end, replacement)

    for m in A_TAG_RE.finditer(content):
        tag = m.group(0)
        attrs = parse_attrs(tag)
        href = attrs.get("href", "")
        new = resolve_target(
            href,
            attrs.get("title"),
            fig_names(m.start()),
            title_to_file,
            files,
            files_lower,
        )
        if new is None:
            continue
        new_tag = HREF_RE.sub(lambda sm: 'href="%s"' % new, tag, count=1)
        if new_tag != tag:
            edits.append((m.start(), m.end(), new_tag))
            stats["links_fixed"] += 1
            key = href.split("?", 1)[0]
            stats["links_from"][key] = stats["links_from"].get(key, 0) + 1

    for m in IMG_TAG_RE.finditer(content):
        tag = m.group(0)
        attrs = parse_attrs(tag)
        src = attrs.get("src", "")
        ds = attrs.get("data-src", "")
        if not ds or not src.startswith("data:"):
            continue
        if not (ds.startswith("../") or ds.startswith("images/")):
            continue  # data-src 仍是外部 URL，离线无法下载
        new_tag = SRC_RE.sub(lambda sm: 'src="%s"' % ds, tag, count=1)
        new_tag = DATA_SRC_RE.sub("", new_tag)
        new_tag = SRCSET_RE.sub("", new_tag)
        new_tag = new_tag.replace('class="lazyload"', "")
        if new_tag != tag:
            edits.append((m.start(), m.end(), new_tag))
            stats["images_hydrated"] += 1

    if not edits:
        return content
    edits.sort(key=lambda e: e[0], reverse=True)
    for s, e, repl in edits:
        content = content[:s] + repl + content[e:]
    return content


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args()

    title_to_file = load_index()
    files = {f for f in os.listdir(PAGES_DIR) if f.endswith(".html")}
    files_lower = {f.lower(): f for f in files}

    stats = {
        "links_fixed": 0,
        "images_hydrated": 0,
        "pages_touched": 0,
        "links_from": {},
    }

    for fn in sorted(os.listdir(PAGES_DIR)):
        if not fn.endswith(".html"):
            continue
        fp = os.path.join(PAGES_DIR, fn)
        with open(fp, encoding="utf-8", errors="replace") as f:
            content = f.read()
        new_content = rewrite_page(content, title_to_file, files, files_lower, stats)
        if new_content != content:
            stats["pages_touched"] += 1
            if not args.dry_run:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(new_content)

    print("pages touched :", stats["pages_touched"])
    print("links fixed   :", stats["links_fixed"])
    print("images hydrated:", stats["images_hydrated"])
    if args.dry_run:
        print("(dry run, 未写文件)")


if __name__ == "__main__":
    main()
