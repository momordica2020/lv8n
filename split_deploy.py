# -*- coding: utf-8 -*-
"""
静态镜像分仓部署脚本
====================

把 wiki/ 静态镜像拆分成多个可独立部署到 GitHub Pages 的仓库，解决单仓库
容量限制问题（GitHub Pages 每站点上限约 1GB、单仓库建议 <1GB）：

  lv8n-site       主站：HTML/CSS/JS/索引/搜索数据（约 40MB）
  lv8n-images-a   图片桶 A：文件名首字符 0-7（约 380MB）
  lv8n-images-b   图片桶 B：文件名首字符 8-f（约 380MB）
  lv8n-media      音频/视频（当前约 13MB，独立成仓便于增长）

页面中的图片/媒体相对路径会被改写为各仓库 GitHub Pages 的绝对 URL，用户
看到的仍是同一个完整站点。

用法：
  python split_deploy.py                 # 生成 deploy/ 目录
  python split_deploy.py --dry-run       # 只统计不改写

生成后按 deploy/README.md 里的命令推送并开启 Pages 即可。
"""
import argparse
import hashlib
import os
import re
import shutil

USER = "momordica2020"
SITE_REPO = "lv8n-site"
IMG_A_REPO = "lv8n-images-a"
IMG_B_REPO = "lv8n-images-b"
MEDIA_REPO = "lv8n-media"

ROOT = os.path.dirname(os.path.abspath(__file__))
WIKI = os.path.join(ROOT, "wiki")
PAGES_DIR = os.path.join(WIKI, "pages")
IMAGES_DIR = os.path.join(WIKI, "images")
MEDIA_DIR = os.path.join(WIKI, "media")
DEPLOY = os.path.join(ROOT, "deploy")

SITE_FILES = [
    "styles.css",
    "search.js",
    "theme.js",
    "search-data.json",
    "index.html",
    "allpages.html",
]

ASSET_RE = re.compile(r'\b(src|data-src|poster)="((?:\.\./)?(?:images|media)/[^"]+)"')


def bucket_repo(name):
    c = name[0].lower()
    if c in "01234567":
        return IMG_A_REPO
    return IMG_B_REPO


def img_url(name):
    return "https://%s.github.io/%s/images/%s" % (USER, bucket_repo(name), name)


def media_url(name):
    return "https://%s.github.io/%s/media/%s" % (USER, MEDIA_REPO, name)


README_TPL = """# {repo}

绿坝娘维基镜像的{desc}仓库。由 [split_deploy.py](split_deploy.py) 生成。

## 部署
1. 在 GitHub 创建同名仓库：https://github.com/{user}/{repo}
2. 推送：
   ```
   cd deploy/{repo}
   git init -b main
   git add .
   git commit -m "镜像部署"
   git remote add origin https://github.com/{user}/{repo}.git
   git push -u origin main
   ```
3. 仓库 Settings → Pages → Source 选择 `main` 分支 / root，保存。
   主站仓库必须保留 `.nojekyll`（否则下划线开头的页面文件会被忽略）。
"""


def safe_rmtree(p):
    if os.path.isdir(p):
        shutil.rmtree(p)


def copy_tree(src, dst, dry):
    n = 0
    for root, _, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = os.path.join(dst, rel)
        if not dry:
            os.makedirs(target, exist_ok=True)
        for f in files:
            if not dry:
                shutil.copy2(os.path.join(root, f), os.path.join(target, f))
            n += 1
    return n


def write_file(path, content, dry):
    if not dry:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


def rewrite_site(dry):
    """把 site 里的相对资源路径改写为各资源仓库的绝对 URL。"""
    files = [os.path.join(DEPLOY, SITE_REPO, "pages", f) for f in os.listdir(PAGES_DIR) if f.endswith(".html")]
    files += [os.path.join(DEPLOY, SITE_REPO, f) for f in ("index.html", "allpages.html")]
    total = 0

    def rep(m):
        attr, path = m.group(1), m.group(2)
        name = os.path.basename(path)
        if "/media/" in path or path.startswith("media/"):
            url = media_url(name)
        else:
            url = img_url(name)
        return '%s="%s"' % (attr, url)

    for fp in files:
        if not os.path.exists(fp):
            continue
        with open(fp, encoding="utf-8") as f:
            c = f.read()
        c2 = ASSET_RE.sub(rep, c)
        if c2 != c:
            total += len(ASSET_RE.findall(c))
            if not dry:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(c2)
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args()
    dry = args.dry_run

    if not dry:
        safe_rmtree(DEPLOY)
        os.makedirs(DEPLOY, exist_ok=True)

    site_dir = os.path.join(DEPLOY, SITE_REPO)
    if not dry:
        os.makedirs(site_dir, exist_ok=True)
        os.makedirs(os.path.join(site_dir, "pages"), exist_ok=True)
        os.makedirs(os.path.join(DEPLOY, IMG_A_REPO, "images"), exist_ok=True)
        os.makedirs(os.path.join(DEPLOY, IMG_B_REPO, "images"), exist_ok=True)
        os.makedirs(os.path.join(DEPLOY, MEDIA_REPO, "media"), exist_ok=True)

    # 站点仓库
    n_pages = copy_tree(PAGES_DIR, os.path.join(site_dir, "pages"), dry)
    for f in SITE_FILES:
        src = os.path.join(WIKI, f)
        if os.path.exists(src) and not dry:
            shutil.copy2(src, os.path.join(site_dir, f))

    # 图片分桶
    img_count = {IMG_A_REPO: 0, IMG_B_REPO: 0}
    img_bytes = {IMG_A_REPO: 0, IMG_B_REPO: 0}
    for f in os.listdir(IMAGES_DIR):
        repo = bucket_repo(f)
        img_count[repo] += 1
        img_bytes[repo] += os.path.getsize(os.path.join(IMAGES_DIR, f))
        if not dry:
            shutil.copy2(os.path.join(IMAGES_DIR, f), os.path.join(DEPLOY, repo, "images", f))

    # 媒体仓库
    n_media = 0
    media_bytes = 0
    if os.path.isdir(MEDIA_DIR):
        for f in os.listdir(MEDIA_DIR):
            n_media += 1
            media_bytes += os.path.getsize(os.path.join(MEDIA_DIR, f))
            if not dry:
                shutil.copy2(os.path.join(MEDIA_DIR, f), os.path.join(DEPLOY, MEDIA_REPO, "media", f))

    # 站点资源路径改写
    n_rewrite = rewrite_site(dry)

    # README / .nojekyll
    if not dry:
        for repo, desc in [
            (SITE_REPO, "主站"),
            (IMG_A_REPO, "图片桶 A"),
            (IMG_B_REPO, "图片桶 B"),
            (MEDIA_REPO, "媒体"),
        ]:
            rd = os.path.join(DEPLOY, repo)
            write_file(os.path.join(rd, "README.md"),
                       README_TPL.format(repo=repo, user=USER, desc=desc), dry)
            write_file(os.path.join(rd, ".nojekyll"), "", dry)

    if not dry:
        with open(os.path.join(DEPLOY, "README.md"), "w", encoding="utf-8") as f:
            f.write(
                "# 绿坝娘维基镜像 · 分仓部署\n\n"
                "共 4 个仓库：`lv8n-site`（主站）、`lv8n-images-a`/`lv8n-images-b`（图片分桶）、"
                "`lv8n-media`（音频/视频）。\n\n"
                "每个仓库的操作：Settings → Pages → Source 选 `main` / root 保存。\n\n"
                "主站 URL：https://%s.github.io/lv8n-site/\n" % USER
            )

    print("== 分仓统计 ==")
    print("lv8n-site      页面 %d 个" % n_pages)
    for repo in (IMG_A_REPO, IMG_B_REPO):
        print("%s  %d 个文件, %.1f MB" % (repo, img_count[repo], img_bytes[repo] / 1048576))
    print("lv8n-media     文件 %d 个, %.1f MB" % (n_media, media_bytes / 1048576))
    print("站点资源引用改写: %d 处" % n_rewrite)
    if dry:
        print("(dry run, 未生成文件)")
    else:
        print("输出目录: %s" % DEPLOY)


if __name__ == "__main__":
    main()
