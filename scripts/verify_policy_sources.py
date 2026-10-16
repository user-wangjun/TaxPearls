"""Read official policy sources and verify reviewed clauses; no tax execution."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.parse import urlsplit

import pypdfium2 as pdfium
import yaml

CATALOG = Path(__file__).resolve().parents[1] / "docs" / "企业政策适用目录.yaml"


class Text(HTMLParser):
    def __init__(self, target):
        super().__init__()
        self.target = target
        self.depth = 0
        self.matches = 0
        self.blocks = []
        self.block_start = 0
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div':
            if self.depth:
                self.depth += 1
            elif self.target in attrs.get('class', '').split() or attrs.get('id') == self.target:
                self.depth = 1
                self.matches += 1
                self.block_start = len(self.parts)
        if tag in {"script", "style"}:
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag == 'div' and self.depth:
            self.depth -= 1
            if not self.depth:
                self.blocks.append(''.join(self.parts[self.block_start:]).strip())
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data):
        if self.depth and not self.ignored:
            self.parts.append(data)


def official_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {
            'www.gdpc.gov.cn', 'www.gd.gov.cn', 'szs.mof.gov.cn', 'fgk.chinatax.gov.cn'}
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise ValueError('source URL must be an approved official HTTPS host')


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verify(source):
    try:
        official_url(source['url'])
        request = Request(source["url"], headers={"User-Agent": "Mozilla/5.0"})
        with build_opener(OfficialRedirect()).open(request, timeout=20) as response:
            official_url(response.geturl())
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("source exceeds 2 MB")
        if source["format"] == "pdf":
            if not raw.startswith(b'%PDF-'):
                raise ValueError('source is not a PDF')
            parts = []
            with pdfium.PdfDocument(io.BytesIO(raw)) as pdf:
                pages = [47, 48] if source["id"] == "gd-deed" else range(len(pdf))
                for i in pages:
                    page = pdf[i]
                    textpage = page.get_textpage()
                    parts.append(textpage.get_text_range())
                    textpage.close()
                    page.close()
            text = '\n'.join(parts)
        else:
            target = ('article-content' if source['id'] == 'gd-revision' else
                      'TRS_Editor' if source['id'] == 'vat-2026' else 'arc_cont')
            parser = Text(target)
            parser.feed(raw.decode("utf-8"))
            blocks = [b for b in parser.blocks if b]
            if len(blocks) != 1 or parser.depth or len(blocks[0]) < 50:
                raise ValueError('official article container missing or ambiguous')
            text = blocks[0]
        clean = re.sub(r"\s+", "", text)
        missing = [v for v in source["required_text"] if re.sub(r"\s+", "", v) not in clean]
        return {"id": source["id"], "url": source["url"], "ok": not missing,
                "missing": missing, "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "text_sha256": hashlib.sha256(clean.encode()).hexdigest()}
    except Exception as exc:
        return {"id": source["id"], "url": source["url"], "ok": False, "error": str(exc)}


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--source", action="append", default=[])
    selected = args.parse_args().source
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    sources = [s for s in catalog["sources"] if not selected or s["id"] in selected]
    if selected and set(selected) - {s["id"] for s in sources}:
        raise SystemExit("unknown source")
    # PDFium is not thread-safe. Parse the two PDFs sequentially; only network
    # reads / HTML parsing run concurrently.
    verified = [verify(s) for s in sources if s['format'] == 'pdf']
    with ThreadPoolExecutor(max_workers=4) as pool:
        verified.extend(pool.map(verify, [s for s in sources if s['format'] != 'pdf']))
    by_id = {r['id']: r for r in verified}
    results = [by_id[s['id']] for s in sources]
    print(json.dumps({"catalog_version": catalog["version"], "sources": results}, ensure_ascii=False, indent=2))
    return 0 if results and all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
