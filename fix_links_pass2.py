# -*- coding: utf-8 -*-
"""第二轮：URL 解码所有残留的 %-编码 .html 链接。"""
import os, re, urllib.parse

PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki", "pages")

link_re = re.compile(r'href="([^"#:/]*%[^"#:/]*\.html)(#[^"]*)?"')

updated = 0
for fn in os.listdir(PAGES_DIR):
    if not fn.endswith(".html"):
        continue
    fp = os.path.join(PAGES_DIR, fn)
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()
    original = content

    def replacer(m):
        encoded = m.group(1)
        frag = m.group(2) or ""
        decoded = urllib.parse.unquote(encoded)
        decoded = re.sub(r'[\\/:*?"<>|]', '_', decoded)
        return 'href="' + decoded + frag + '"'

    content = link_re.sub(replacer, content)
    if content != original:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        updated += 1

print("Second pass: updated %d files" % updated)
