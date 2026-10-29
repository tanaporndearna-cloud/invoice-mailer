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
    d["via_email"] = d["channel"].fillna("").str.contains("อีเมล")
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
