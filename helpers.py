import re, zipfile
import pandas as pd

EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
INV_RE = re.compile(r"(\d{9})(?:\s*-\s*(\d{9}))?")
MAX_MAIL_BYTES = 18 * 1024 * 1024  # Gmail จำกัด 25MB เผื่อ overhead


def load_sheet(f):
    """อ่านรายงานภาษีขาย: หัวตารางแถว 2, B=เลขที่, N=ช่องทาง, O=อีเมล"""
    if f.name.lower().endswith("csv"):
        raw = pd.read_csv(f, header=1, dtype=str)
    else:
        raw = pd.read_excel(f, header=1, dtype=str)
    d = pd.DataFrame({
        "invoice_no": raw.iloc[:, 1],
        "branch": raw.iloc[:, 2],                 # คอลัมน์ C (สาขา เช่น T4)
        "customer": raw.iloc[:, 4],
        "channel": raw.iloc[:, 13],
        "email_raw": raw.iloc[:, 14],
    })
    d["invoice_no"] = (d["invoice_no"].astype(str).str.strip()
                       .str.replace(r"\.0$", "", regex=True))
    d = d[d["invoice_no"].str.fullmatch(r"\d{9}")]
    d = d.groupby("invoice_no", as_index=False).first()

    def get_email(x):
        m = EMAIL_RE.search(str(x))
        return m.group(0).lower() if m else None

    d["email"] = d["email_raw"].map(get_email)
    d["branch"] = d["branch"].fillna("").astype(str).str.strip().str.upper()
    ch = d["channel"].fillna("")
    # ส่งเข้าสาขา: "รับหน้าสาขา" และ "ส่งหน้าร้าน" (ไม่ส่งหาลูกค้าเด็ดขาด แม้ช่องอีเมลมีค่า)
    d["via_branch"] = ch.str.contains("รับหน้าสาขา|ส่งหน้าร้าน")
    d["via_email"] = ch.str.contains("อีเมล") & ~d["via_branch"]
    return d


def load_pdfs(zip_files):
    """รับ zip ได้หลายไฟล์ -> ({เลขที่: bytes}, [ชื่อไฟล์ช่วงหลายใบ])"""
    pdfs, ranges = {}, []
    for z in zip_files:
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if not n.lower().endswith(".pdf"):
                    continue
                base = n.split("/")[-1]
                m = INV_RE.search(base)
                if not m:
                    continue
                a, b = m.group(1), m.group(2)
                if b is None or a == b:      # ใบเดียว
                    pdfs[a] = zf.read(n)
                else:                        # ช่วงหลายใบ
                    ranges.append(base)
    return pdfs, ranges


def split_batches(inv_list, pdfs, limit=MAX_MAIL_BYTES):
    """แบ่งใบกำกับของลูกค้าคนเดียวเป็นหลายเมล ไม่ให้เกินขนาดที่ Gmail รับ"""
    batches, cur, size = [], [], 0
    for inv in inv_list:
        s = len(pdfs[inv])
        if cur and size + s > limit:
            batches.append(cur)
            cur, size = [], 0
        cur.append(inv)
        size += s
    if cur:
        batches.append(cur)
    return batches


# ---------- โควต้า / ทรัพยากร ----------
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Bangkok")
DAILY_LIMIT = 500          # โควต้าเมล/วัน ของ Gmail ส่วนตัว
UPLOAD_LIMIT_MB = 200      # ลิมิตของ Streamlit ต่อไฟล์


def now_th():
    return datetime.now(TZ)


def count_sent_today(prev_dfs):
    """นับจำนวน 'เมล' ที่ส่งสำเร็จวันนี้ จากไฟล์ผลการส่งที่อัปโหลด
    (1 เมล = 1 คู่ อีเมล+เวลาส่ง) ไฟล์เก่าที่ไม่มีคอลัมน์เวลาจะไม่ถูกนับ"""
    today = now_th().strftime("%Y-%m-%d")
    mails = set()
    for p in prev_dfs:
        if "ส่งเมื่อ" not in p.columns:
            continue
        ok = p[(p["สถานะ"] == "สำเร็จ") & p["ส่งเมื่อ"].fillna("").str.startswith(today)]
        mails |= set(zip(ok["email"], ok["ส่งเมื่อ"]))
    return len(mails)


def _read_int(path):
    try:
        v = open(path).read().strip()
        return None if v == "max" else int(v)
    except Exception:
        return None


def get_memory_mb():
    """คืน (ใช้ไป MB, ลิมิต MB หรือ None) อ่านจาก cgroup ของ container"""
    used = _read_int("/sys/fs/cgroup/memory.current") or _read_int(
        "/sys/fs/cgroup/memory/memory.usage_in_bytes")
    limit = _read_int("/sys/fs/cgroup/memory.max") or _read_int(
        "/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if limit and limit > 1 << 45:      # ค่า "ไม่จำกัด"
        limit = None
    if used is None:                   # สำรอง: RAM ของโปรเซสแอป
        try:
            for line in open("/proc/self/status"):
                if line.startswith("VmRSS"):
                    used = int(line.split()[1]) * 1024
        except Exception:
            pass
    to_mb = lambda b: None if b is None else b / 1024 / 1024
    return to_mb(used), to_mb(limit)


# ---------- เนื้อหาอีเมล (แก้ข้อความได้ตรงนี้) ----------
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
import html as _html

SIGNATURE_NAME = "Accounting officer"
SIGNATURE_COMPANY = "TRC motorsport"
SIGNATURE_TEL = "Tel.02-746-8989 Ext.610"


# หัวเรื่องเมลสาขาที่ "จำไว้" (สาขาที่ใช้หัวเรื่องต่างจากแบบมาตรฐาน ต้องคงไว้เพื่อให้ต่อกระทู้เดิม)
# สาขาอื่นๆ ใช้แบบมาตรฐาน: TRC Motorsport ใบกำกับภาษี / ใบเสร็จรับเงิน ( สาขา Tx )
BRANCH_SUBJECTS = {
    "T18": "TRC Motorsport ใบกำกับภาษี / ใบเสร็จรับเงิน (สาขา T18 - รับหน้าสาขา )",
}


def build_subject(customer, branch=None, to=None):
    if branch:
        return BRANCH_SUBJECTS.get(branch) or f"TRC Motorsport ใบกำกับภาษี / ใบเสร็จรับเงิน ( สาขา {branch} )"
    if customer:
        return f"TRC Motorsport ใบกำกับภาษี / ใบเสร็จรับเงิน ( {customer} )"
    # ไม่มีชื่อลูกค้า: ใส่อีเมลผู้รับต่อท้าย เพื่อให้ Gmail แยกเป็นกระทู้ของใครของมัน
    return f"TRC Motorsport ใบกำกับภาษี / ใบเสร็จรับเงิน ( {to} )" if to else "TRC Motorsport ใบกำกับภาษี / ใบเสร็จรับเงิน"


def build_bodies(sender_email, branch=None, items=None):
    """items = [(เลขที่ใบกำกับ, ชื่อลูกค้า)] ใช้กรณีส่งเข้าสาขา"""
    items = items or []
    list_text = ""
    list_html = ""
    if branch:
        list_text = f"สาขา {branch} รายการใบกำกับที่แนบ:\n" + "".join(
            f"  - {i}  {c}\n" for i, c in items) + "\n"
        rows = "".join(f"<li>{_html.escape(i)} &nbsp; {_html.escape(c)}</li>" for i, c in items)
        list_html = (f'<p style="color:#1a4f9c;">สาขา {_html.escape(branch)} '
                     f'รายการใบกำกับที่แนบ:</p><ul style="color:#1a4f9c;">{rows}</ul>')
    greet = f"สาขา {branch}" if branch else "บัญชี/การเงิน"
    text = (
        f"เรียน {greet}\n"
        "เรื่อง ใบเสร็จรับเงิน/ใบกำกับภาษี\n\n"
        "    ตามไฟล์ที่แนบมานี้ ค่ะ\n\n"
        + list_text +
        "Kindly Regards,\n"
        f"{SIGNATURE_NAME}\n{SIGNATURE_COMPANY}\n"
        f"Email : {sender_email}\n{SIGNATURE_TEL}\n"
    )
    e = _html.escape
    blue = "color:#1a4f9c;font-weight:bold;"
    html = f"""<div style="font-family:Tahoma,'Comic Sans MS',sans-serif;font-size:14px;">
<p style="{blue}">เรียน {e(greet)}<br>เรื่อง ใบเสร็จรับเงิน/ใบกำกับภาษี</p>
<p style="{blue}">&nbsp;&nbsp;&nbsp;&nbsp;ตามไฟล์ที่แนบมานี้ &nbsp; ค่ะ</p>
{list_html}<br>
<p style="margin:0;"><b>Kindly Regards,</b></p>
<p style="margin:0;color:#444;"><i>{e(SIGNATURE_NAME)}<br>{e(SIGNATURE_COMPANY)}</i></p>
<p style="color:#444;"><i>Email : <a href="mailto:{e(sender_email)}">{e(sender_email)}</a><br>{e(SIGNATURE_TEL)}</i></p>
</div>"""
    return text, html


def norm_subject(s):
    s = re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", str(s or ""), flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def plan_subject(customer, branch, to, reply_to_id=None, prev_subject=None):
    """คืน (หัวเรื่องที่จะใช้, reply_to_id ที่ใช้จริง) กติกาเดียวกับตอนสร้างเมล ใช้แสดงในตารางก่อนส่งด้วย"""
    subject = build_subject(customer, branch, to)
    # กันพลาด: เมลสาขาตอบกลับได้เฉพาะเมลที่หัวเรื่องเป็นของสาขานั้นเท่านั้น ห้ามต่อกับหัวเรื่องลูกค้าเด็ดขาด
    if reply_to_id and branch:
        if not prev_subject or not re.search(r"\(\s*สาขา\s+" + re.escape(branch) + r"\b", prev_subject, re.I):
            reply_to_id = None
    if reply_to_id:
        # เมลสาขาใช้หัวเรื่องที่จำไว้เสมอ เมลลูกค้าใช้หัวเรื่องเดิมของเมลก่อนหน้า (ให้ Gmail ต่อกระทู้เดิม)
        base = subject if branch else (norm_subject(prev_subject) if prev_subject else subject)
        subject = "Re: " + base
    return subject, reply_to_id


def build_message(sender_email, sender_name, to, customer, invoices, pdfs,
                  branch=None, cust_by_inv=None, reply_to_id=None, prev_subject=None, cc=None):
    m = EmailMessage()
    m["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    m["To"] = to
    if cc:                               # สำเนา (CC) ทั้งเมลสาขาและเมลลูกค้า
        m["Cc"] = cc
    m["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1])
    subject, reply_to_id = plan_subject(customer, branch, to, reply_to_id, prev_subject)
    if reply_to_id:                      # ตอบกลับเมลเดิมของผู้รับคนนี้
        m["In-Reply-To"] = reply_to_id
        m["References"] = reply_to_id
    m["Subject"] = subject
    items = [(i, (cust_by_inv or {}).get(i, "")) for i in invoices]
    text, html = build_bodies(sender_email, branch, items)
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    for inv in invoices:
        m.add_attachment(pdfs[inv], maintype="application", subtype="pdf",
                         filename=f"{inv}.pdf")
    return m


def last_message_ids(prev_dfs):
    """{อีเมลผู้รับ(ตัวเล็ก): Message-ID ของเมลล่าสุดที่ส่งสำเร็จ} จากไฟล์ผลการส่งเก่า"""
    best = {}
    for p in prev_dfs:
        if "message_id" not in p.columns or "ส่งเมื่อ" not in p.columns:
            continue
        ok = p[(p["สถานะ"] == "สำเร็จ") & p["message_id"].notna() & p["email"].notna()]
        for _, r in ok.iterrows():
            k = str(r["email"]).strip().lower()
            if k not in best or str(r["ส่งเมื่อ"]) > best[k][0]:
                best[k] = (str(r["ส่งเมื่อ"]), r["message_id"])
    return {k: v[1] for k, v in best.items()}


# ---------- ค้นเมลเดิมจากโฟลเดอร์ "ส่งแล้ว" ของ Gmail (IMAP) ----------
import imaplib, email as _email
from email.header import decode_header, make_header


def _sent_folder(imap):
    """หาชื่อโฟลเดอร์ส่งแล้ว จาก flag \\Sent (ชื่อเปลี่ยนตามภาษาของบัญชี)"""
    typ, boxes = imap.list()
    for b in boxes or []:
        line = b.decode("utf-8", "ignore") if isinstance(b, bytes) else str(b)
        if "\\Sent" in line:
            name = line.rsplit(' "/" ', 1)[-1].strip()
            return name
    return '"[Gmail]/Sent Mail"'


def imap_connect(user, app_password):
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(user, app_password)
    typ, _ = imap.select(_sent_folder(imap), readonly=True)
    if typ != "OK":
        raise RuntimeError("เปิดโฟลเดอร์ส่งแล้วไม่ได้")
    return imap


SUBJECT_PREFIX = "TRC Motorsport ใบกำกับภาษี / ใบเสร็จรับเงิน"


def _addr_set(s):
    from email.utils import getaddresses
    return {a.strip().lower() for _, a in getaddresses([str(s or "")]) if a.strip()}


def find_last_message(imap, to_addr, branch=None, prefix=SUBJECT_PREFIX, scan=60):
    """หาเมลล่าสุดที่เคยส่งหา to_addr (คืน (Message-ID, หัวเรื่อง) หรือ None)
    - ต้องส่งถึง 'ผู้รับชุดนี้เท่านั้น' (ช่อง To ตรงเป๊ะ ไม่มีคนอื่นปน)
    - branch = รหัสสาขา -> ต้องเป็นเมลสาขาของสาขานั้นเท่านั้น (หัวเรื่อง "(สาขา Tx - รับหน้าสาขา)")
    - branch = None -> เมลลูกค้า หัวเรื่องขึ้นต้น prefix และ *ไม่ใช่* เมลรับหน้าสาขา"""
    want_rec = _addr_set(to_addr)
    first = sorted(want_rec)[0] if want_rec else str(to_addr)
    q = f'"to:{first} subject:Motorsport"'      # ต้องครอบด้วย " ไม่งั้น IMAP อ่านคำสั่งไม่ออก
    typ, data = imap.search(None, "X-GM-RAW", q)
    if typ != "OK" or not data or not data[0]:
        return None
    ids = data[0].split()[-scan:]
    typ, msg = imap.fetch(b",".join(ids).decode(), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT TO)])")
    if typ != "OK" or not msg:
        return None
    want_cust = norm_subject(prefix)
    # หัวเรื่องเมลสาขา: "( สาขา T2 )" (รองรับรูปแบบเก่า "(สาขา T2 - รับหน้าสาขา)" ด้วย)
    branch_re = re.compile(r"\(\s*สาขา\s+" + re.escape(branch) + r"\b", re.I) if branch else None
    any_branch_re = re.compile(r"\(\s*สาขา\s")
    found = []
    for part in msg:
        if not isinstance(part, tuple):
            continue
        head = part[0].split()[0]
        hdr = _email.message_from_bytes(part[1])
        mid = (hdr.get("Message-ID") or "").strip()
        try:
            subj = str(make_header(decode_header(hdr.get("Subject") or "")))
        except Exception:
            subj = hdr.get("Subject") or ""
        if not mid or _addr_set(hdr.get("To")) != want_rec:
            continue
        ns = norm_subject(subj)
        if branch:
            ok = ns.startswith(want_cust) and bool(branch_re.search(ns))
        else:
            ok = ns.startswith(want_cust) and not any_branch_re.search(ns) and "รับหน้าสาขา" not in ns
        if ok:
            found.append((int(head), mid, subj))
    if not found:
        return None
    _, mid, subj = max(found)
    return mid, subj
