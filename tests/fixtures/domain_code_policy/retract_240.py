"""Retract the false-positive 'send' on draft 240 and quarantine prose-less rows.

Draft 240 (live, 'Re: Lars' -> office@diepsychiater.com) was a reply shell Lars
opened himself: own_text() of its body is empty. The scan then diffed "" against
the message he actually sent on 2026-09-24 and reported it as a 100% rewrite of
an Emma draft. Emma never wrote that draft, so there is no edit and no lesson.

This deletes the bogus sends row and marks the prose-less drafts 'notadraft' so
they can never be resolved or reported again. No lesson is recorded.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import draft_ledger as L

conn = L.connect()

cur = conn.execute("SELECT COUNT(*) FROM sends WHERE draft_row=240").fetchone()[0]
print("sends rows for draft 240 before:", cur)
conn.execute("DELETE FROM sends WHERE draft_row=240")

rows = conn.execute(
    "SELECT id, account, recipient, subject FROM drafts "
    "WHERE length(trim(body))=0").fetchall()
print("prose-less draft rows:", len(rows))
for r in rows:
    print(f"  quarantine {r['id']:4d} [{r['account']}] {r['subject'][:60]!r} -> {r['recipient'][:40]}")
conn.execute("UPDATE drafts SET status='notadraft' WHERE length(trim(body))=0")

les = conn.execute("SELECT COUNT(*) FROM lessons WHERE draft_row=240").fetchone()[0]
print("lessons attributed to draft 240 (expect 0):", les)

conn.commit()

print("\n--- after ---")
for r in conn.execute("SELECT account, status, COUNT(*) n FROM drafts "
                      "GROUP BY account, status ORDER BY account, status"):
    print(f"  {r['account']:8s} {r['status']:10s} {r['n']}")
for r in conn.execute("SELECT s.id, s.draft_row, s.verdict, d.recipient "
                      "FROM sends s JOIN drafts d ON d.id=s.draft_row"):
    print(f"  send {r['id']} draft {r['draft_row']} {r['verdict']} -> {r['recipient']}")
conn.close()
