#!/usr/bin/env python3
"""Read-only verification for t_7bb6be2e.

1. Does the ENT draft still exist in Lars's live.com Drafts, still unsent?
2. Has ANYTHING inbound arrived from orlpraxen.com (any address, any folder)?
3. Has the "Nose" conversation gained any message since 2026-09-18?

No writes, no sends. Paginated, error-counted: a zero is only reported when
zero errors occurred.
"""
import sys
sys.path.insert(0, "/Users/claudia/hermes/home/profiles/emma/scripts")
import m365_auth

ACCT = "live"
CONV_TAIL = "MAoAEAAwOIDc_WFOBb4oKkzuuuFj"
DRAFT_TAIL = "RIxFVssZmv57AAkA-4-uAAAA"

errors = []


def page(path, params):
    """Follow @odata.nextLink, returning all values."""
    out = []
    data, tok = m365_auth.graph(ACCT, path, params=params)
    while True:
        out.extend(data.get("value", []))
        nxt = data.get("@odata.nextLink")
        if not nxt:
            break
        import urllib.request, json as _json
        req = urllib.request.Request(nxt, headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = _json.loads(r.read().decode())
    return out


# --- 1. the draft ---------------------------------------------------------
print("== 1. Drafts folder, orlpraxen recipients ==")
try:
    drafts = page(
        "/me/mailFolders/drafts/messages",
        {"$select": "id,subject,toRecipients,conversationId,lastModifiedDateTime,isDraft",
         "$top": "100"},
    )
    print("total drafts:", len(drafts))
    hit = 0
    for m in drafts:
        to = ",".join(r["emailAddress"]["address"] for r in m.get("toRecipients", []))
        if "orlpraxen" in to.lower():
            hit += 1
            print("  DRAFT id-tail:", m["id"][-24:])
            print("    subject:", m.get("subject"))
            print("    to:", to)
            print("    isDraft:", m.get("isDraft"))
            print("    lastModified:", m.get("lastModifiedDateTime"))
            print("    conv tail:", m.get("conversationId", "")[-28:])
            print("    matches recorded draft id:", m["id"].endswith(DRAFT_TAIL))
    print("orlpraxen drafts found:", hit)
except Exception as e:
    errors.append("drafts: %r" % (e,))
    print("ERROR", e)

# --- 2. any inbound from orlpraxen, all folders ---------------------------
print()
print("== 2. All folders, any message from *orlpraxen.com ==")
try:
    msgs = page(
        "/me/messages",
        {"$select": "id,subject,from,sender,receivedDateTime,parentFolderId,isDraft",
         "$search": '"from:orlpraxen.com"',
         "$top": "100"},
    )
    print("search hits:", len(msgs))
    for m in msgs:
        frm = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
        print("  ", m.get("receivedDateTime"), frm, "|", (m.get("subject") or "")[:60],
              "| isDraft", m.get("isDraft"))
except Exception as e:
    errors.append("search: %r" % (e,))
    print("ERROR", e)

# --- 3. the Nose conversation, everything in it ---------------------------
print()
print("== 3. Whole 'Nose' conversation, newest first ==")
try:
    conv = page(
        "/me/messages",
        {"$select": "id,subject,from,receivedDateTime,sentDateTime,isDraft,parentFolderId",
         "$filter": "conversationId eq '%s'" % CONV_TAIL,
         "$orderby": "receivedDateTime desc",
         "$top": "50"},
    )
    print("messages in conversation (filter on tail):", len(conv))
    for m in conv:
        frm = (m.get("from") or {}).get("emailAddress", {}).get("address", "(none)")
        print("  ", m.get("receivedDateTime"), frm, "| isDraft", m.get("isDraft"),
              "|", (m.get("subject") or "")[:50], "| id-tail", m["id"][-24:])
except Exception as e:
    errors.append("conversation: %r" % (e,))
    print("ERROR", e)

# --- 4. Sent Items: was it ever sent? ------------------------------------
print()
print("== 4. Sent Items, anything to orlpraxen in 2026 ==")
try:
    sent = page(
        "/me/mailFolders/sentitems/messages",
        {"$select": "id,subject,toRecipients,sentDateTime",
         "$top": "100", "$orderby": "sentDateTime desc"},
    )
    print("sent items scanned:", len(sent))
    n = 0
    for m in sent:
        to = ",".join(r["emailAddress"]["address"] for r in m.get("toRecipients", []))
        if "orlpraxen" in to.lower():
            n += 1
            print("  SENT", m.get("sentDateTime"), "->", to, "|", (m.get("subject") or "")[:50])
    print("sends to orlpraxen in scanned window:", n)
except Exception as e:
    errors.append("sent: %r" % (e,))
    print("ERROR", e)

print()
print("ERRORS:", len(errors))
for e in errors:
    print("  ", e)
