#!/usr/bin/env python3
"""Fetch latest Windows 10/11 ESD download links and write them to README.md."""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

WORPROJECT = "https://worproject.com/dldserv/esd/getversions.php"
CAB_URLS = {
    "11": "https://go.microsoft.com/fwlink?linkid=2156292",
    "10": "https://go.microsoft.com/fwlink/?LinkId=841361",
}


def log(msg: str):
    print(f"  {msg}", file=sys.stderr)


def fetch(url: str) -> bytes:
    log(f"GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "esd-watch/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def get_cab_url(ver: str, mct_xml: bytes) -> str:
    """Extract latestCabLink from worproject XML; fall back to hardcoded URL."""
    root = ET.fromstring(mct_xml)
    for v in root.iter("version"):
        if v.get("number") == ver:
            link = v.find("latestCabLink")
            if link is not None and link.text:
                return link.text
    return CAB_URLS[ver]


def extract_cab(cab_data: bytes, dest: Path) -> Path:
    """Run cabextract and return path to products.xml."""
    cab_path = dest / "catalog.cab"
    cab_path.write_bytes(cab_data)

    subprocess.run(
        ["cabextract", "-d", str(dest), str(cab_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # products.xml may be in a subdirectory
    for found in dest.rglob("products.xml"):
        return found
    raise FileNotFoundError("products.xml not found after cabextract")


def parse_products(xml: bytes) -> list[dict]:
    """Parse products.xml → list of ESD file dicts with resolved localizations."""
    root = ET.fromstring(xml)

    # Build localization dict: { languageCode: { key: value } }
    # e.g. { "default": { "ARCH_64": "x64", "ENTERPRISE": "Enterprise", ... } }
    localizations: dict[str, dict[str, str]] = {}
    langs_el = root.find(".//PublishedMedia/Languages")
    if langs_el is not None:
        for lang_el in langs_el.findall("Language"):
            lc = lang_el.get("LanguageCode", "default")
            loc: dict[str, str] = {}
            for child in lang_el:
                tag = child.tag
                text = child.text or ""
                # Strip CDATA wrapper if present
                if text.startswith("<![CDATA[") and text.endswith("]]>"):
                    text = text[9:-3]
                text = text.strip()
                loc[tag] = text
            localizations[lc] = loc

    def localize(loc_key: str, lang_code: str) -> str:
        """Resolve a %KEY% localization key to its display value."""
        key = loc_key.strip("%")            # e.g. "%ARCH_64%" → "ARCH_64"
        # Try the file's language, then "default"
        for lc in (lang_code, "default"):
            if lc in localizations and key in localizations[lc]:
                val = localizations[lc][key]
                # Trim surrounding whitespace/CDATA
                return val.strip()
        return key  # fallback to the raw key

    files = []
    for f in root.iter("File"):
        def text(tag: str) -> str:
            e = f.find(tag)
            return e.text.strip() if e is not None and e.text else ""

        lang_code = text("LanguageCode")
        name = text("FileName")
        # extract build from filename, e.g. "26100.4349.250607-1500..." → "26100.4349"
        bm = re.match(r"(\d+\.\d+)", name)
        build = bm.group(1) if bm else ""
        files.append({
            "name":         name,
            "build":        build,
            "lang":         lang_code,
            "langName":     text("Language"),
            "edition":      text("Edition"),
            "editionName":  localize(text("Edition_Loc"), lang_code),
            "arch":         text("Architecture"),
            "archName":     localize(text("Architecture_Loc"), lang_code),
            "size":         text("Size"),
            "sha1":         text("Sha1"),
            "url":          text("FilePath"),
        })

    # dedupe by url, sort
    seen = set()
    uniq = []
    for f in files:
        if f["url"] and f["url"] not in seen:
            seen.add(f["url"])
            uniq.append(f)
    uniq.sort(key=lambda f: (f["arch"], f["edition"], f["lang"]))
    return uniq


def size_fmt(b: str) -> str:
    try:
        return f"{int(b) / (1024**3):.1f} GB"
    except (ValueError, TypeError):
        return b


def build_list(files: list[dict]) -> str:
    if not files:
        return "(no files found)\n"
    lines = []
    for f in files:
        label = f"{f['arch']} {f['editionName'] or f['edition']} {f['langName'] or f['lang']} {size_fmt(f['size'])}"
        lines.append(f"- [{label}]({f['url']})")
        lines.append(f"  - `{f['sha1']}`")
    return "\n".join(lines)


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_files = {}

    # step 1: fetch MCT catalogs
    log("Fetching MCT catalogs...")
    mct_xml = fetch(WORPROJECT)

    # step 2: for each Windows version, download CAB → extract → parse
    for ver in ("11", "10"):
        label = f"Windows {ver}"
        log(f"\n{'='*50}")
        log(f"Processing {label}")

        cab_url = get_cab_url(ver, mct_xml)
        log(f"CAB URL: {cab_url}")

        try:
            cab_data = fetch(cab_url)
        except Exception as e:
            log(f"ERROR downloading CAB: {e}")
            all_files[ver] = []
            continue

        with tempfile.TemporaryDirectory() as tmp:
            try:
                products_path = extract_cab(cab_data, Path(tmp))
            except Exception as e:
                log(f"ERROR extracting CAB: {e}")
                all_files[ver] = []
                continue

            xml = products_path.read_bytes()
            files = parse_products(xml)
            log(f"Found {len(files)} ESD files")
            all_files[ver] = files

    # step 3: generate README
    log(f"\n{'='*50}")
    log("Generating README.md...")

    def section_heading(ver: str) -> str:
        files = all_files.get(ver, [])
        build = files[0]["build"] if files and files[0]["build"] else ""
        return f"## Windows {ver}{f' (build {build})' if build else ''}"

    md = f"""# Windows ESD Links

> Auto-updated every day by [GitHub Actions](.github/workflows/update.yml).
> Last refresh: **{now}**

> **Note:** CDN only serves HTTP — use `curl -LO` or right-click "Save link as…" to download.
> Verify with `shasum -a 1 <file>` against the SHA1 shown after each link.

{section_heading('11')}

{build_list(all_files.get('11', []))}

{section_heading('10')}

{build_list(all_files.get('10', []))}

---
*Data sources: [worproject.com](https://worproject.com/dldserv/esd/getversions.php) → Microsoft Media Creation Tool catalogs*
"""

    Path("README.md").write_text(md)

    print(f"  Windows 11: {len(all_files.get('11', []))} files")
    print(f"  Windows 10: {len(all_files.get('10', []))} files")
    print(f"  README.md written")


if __name__ == "__main__":
    main()
