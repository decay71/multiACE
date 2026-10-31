"""index.html must keep its tags balanced: one unclosed element nests every
later tab inside the wrong v-show section and those tabs render empty (the
print queue merge left the favorites card open, 2026-10-31)."""
import os
from html.parser import HTMLParser

INDEX = os.path.join(os.path.dirname(__file__), '..', '..', 'multiace', 'web',
                     'frontend', 'index.html')
VOID = {'br', 'img', 'input', 'meta', 'link', 'hr', 'source', 'col', 'area',
        'base', 'wbr', 'path', 'circle', 'rect', 'line', 'polyline',
        'polygon', 'use', 'stop', 'ellipse'}


class _Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
            return
        self.errors.append('</%s> at line %d, open: %s'
                           % (tag, self.getpos()[0],
                              self.stack[-1] if self.stack else None))
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def test_index_html_tags_balanced():
    p = _Balance()
    with open(INDEX, encoding='utf-8') as f:
        p.feed(f.read())
    assert p.errors == [] and p.stack == [], (p.errors[:5], p.stack[-5:])
