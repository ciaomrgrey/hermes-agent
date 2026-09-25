"""Build insert-articles.json from insert-articles.sql and lint the copy.

One source of truth: the SQL file. The JSON is derived, so the two cannot drift.
Lints applied to every consumer-visible string (title, h1, meta_description, body_md,
pillar_name): no em dash, no year, no "of the year", no "premium", no scarcity language.
"""
import json
import re
import sys
import pathlib

HERE = pathlib.Path(__file__).parent
sql = (HERE / 'insert-articles.sql').read_text()

# Split the VALUES tuples. Each row is "(\n  1,\n  'Awards', ... $md$...$md$\n)".
bodies = re.findall(r'\$md\$(.*?)\$md\$', sql, re.S)
assert len(bodies) == 2, f"expected 2 body_md blocks, found {len(bodies)}"

# Strip the bodies out so the scalar fields parse without markdown interference.
scrubbed = re.sub(r'\$md\$.*?\$md\$', '@@BODY@@', sql, flags=re.S)
tuples = re.findall(r'\(\s*1,\s*(.*?)@@BODY@@\s*\)', scrubbed, re.S)
assert len(tuples) == 2, f"expected 2 value tuples, found {len(tuples)}"

FIELDS = ['pillar_name', 'cluster', 'content_type', 'title', 'slug', 'h1',
          'meta_description', 'author_slug', 'status']

rows = []
for tup, body in zip(tuples, bodies):
    quoted = re.findall(r"'((?:[^']|'')*)'", tup)
    links_raw = quoted[-1]
    scalars = quoted[:-1]
    assert len(scalars) == len(FIELDS), f"got {len(scalars)} scalars, want {len(FIELDS)}: {scalars}"
    row = {'pillar': 1}
    row.update({k: v.replace("''", "'") for k, v in zip(FIELDS, scalars)})
    row['internal_links'] = json.loads(links_raw)
    row['body_md'] = body
    row['published_at'] = None
    rows.append(row)

(HERE / 'insert-articles.json').write_text(json.dumps(rows, indent=2, ensure_ascii=False) + '\n')

# ---------------------------------------------------------------- lint
BANNED = [
    (r'\u2014', 'em dash'),
    (r'\b(19|20)\d{2}\b', 'a year'),
    (r'of the year', '"of the year"'),
    (r'\bpremium\b', '"premium" on a consumer surface'),
    (r'\bsuper premium\b', '"super premium"'),
    (r'last chance|sells out|selling out|don\'t miss|limited time|hurry', 'scarcity / loss aversion'),
    (r'\bAthens\b', 'a city as the distilling location'),
    (r'Lost Lake', 'the producer named publicly'),
    (r'\bbase spirit\b', 'a base-spirit claim'),
]
VISIBLE = ['pillar_name', 'title', 'h1', 'meta_description', 'body_md']

failures = []
for i, row in enumerate(rows, 1):
    for field in VISIBLE:
        text = row[field]
        for pattern, label in BANNED:
            for m in re.finditer(pattern, text, re.I):
                failures.append(f"post {i} / {field}: {label} -> {text[max(0, m.start()-45):m.end()+45]!r}")

for i, row in enumerate(rows, 1):
    n = len(row['meta_description'])
    if n > 160:
        failures.append(f"post {i} / meta_description: {n} chars, over the 160 search-snippet budget")
    if not row['meta_description'].strip().endswith('.'):
        failures.append(f"post {i} / meta_description: does not end in a full stop")
    if row['status'] != 'draft':
        failures.append(f"post {i}: status is {row['status']!r}, must stay 'draft' until Lars approves")
    if row['published_at'] is not None:
        failures.append(f"post {i}: published_at must stay null before approval")

for i, row in enumerate(rows, 1):
    words = len(re.findall(r'\b\w+\b', row['body_md']))
    md_links = re.findall(r'\]\((/[^)]+)\)', row['body_md'])
    print(f"post {i}: slug={row['slug']}  status={row['status']}  published_at={row['published_at']}")
    print(f"         meta_description {len(row['meta_description'])} chars, body {words} words")
    print(f"         in-body internal links: {md_links}")

print()
if failures:
    print("LINT FAILURES:")
    for f in failures:
        print("  x", f)
    sys.exit(1)
print("LINT PASS: no em dash, no year, no 'of the year', no 'premium', no scarcity,")
print("           no city-as-origin, no producer named, no base-spirit claim.")
print("JSON written:", HERE / 'insert-articles.json')
