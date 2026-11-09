"""Render only public guides, using a build dependency absent from the runtime."""
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ('User-Guide', 'Troubleshooting', 'Advanced-CLI', 'Source-Installation',
          'Cloud-Bot', 'Cloud-Relay', 'Release')
STYLE = 'body{max-width:1000px;margin:40px auto;padding:0 24px;font:18px/1.6 system-ui}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:8px;text-align:left}pre{overflow:auto;background:#eee;padding:16px}a{color:#164db5}'


def render():
    target = ROOT / 'build/guides'
    target.mkdir(parents=True, exist_ok=True)
    for name in GUIDES:
        body = markdown.markdown((ROOT / f'docs/{name}.md').read_text(encoding='utf-8'),
                                 extensions=['tables', 'fenced_code', 'toc'])
        body = re.sub(r'(href="[^"#]+)\.md(["#])', r'\1.html\2', body)
        # Source-only links remain explanatory text in the offline copy.
        body = re.sub(r'<a href="\.\./[^\"]+">(.*?)</a>', r'\1 (see source repository)', body)
        document = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width"><title>Outlook Triage Guide</title>'
                    f'<style>{STYLE}</style></head><body>{body}</body></html>')
        (target / f'{name}.html').write_text(document, encoding='utf-8')


if __name__ == '__main__':
    render()
