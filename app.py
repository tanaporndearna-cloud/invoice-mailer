import smtplib, time
import pandas as pd
import streamlit as st
from email.message import EmailMessage
from helpers import load_sheet, load_pdfs, split_batches

st.set_page_config(page_title="ส่งใบกำกับภาษี", page_icon="📧")
st.title("ส่งใบกำกับภาษีทางอีเมล")

if st.text_input("รหัสเข้าใช้งาน", type="password") != st.secrets["APP_PASSWORD"]:
    st.stop()

data_file = st.file_uploader("1) ไฟล์รายงานภาษีขาย (Excel/CSV)", type=["xlsx", "csv"])
zip_files = st.file_uploader("2) ไฟล์ zip รวม PDF (ใส่ได้หลายไฟล์)", type=["zip"],
                             accept_multiple_files=True)
prev_file = st.file_uploader("3) ผลการส่งครั้งก่อน (ไม่บังคับ ไว้กันส่งซ้ำ)", type=["csv"])

if data_file and zip_files:
    df = load_sheet(data_file)
    pdfs, ranges = load_pdfs(zip_files)

    if ranges:
        st.warning("ไฟล์เหล่านี้เป็นช่วงหลายใบ ระบบไม่ส่งให้ ต้องเช็กเอง: " + ", ".join(ranges))

    sent_before = set()
    if prev_file:
        p = pd.read_csv(prev_file, dtype=str)
        sent_before = set(p.loc[p["สถานะ"] == "สำเร็จ", "invoice_no"])

    def status(r):
        if not r["via_email"]:             return "ช่องทางอื่น (ไม่ส่ง)"
        if not r["email"]:                 return "อีเมลไม่ถูกต้อง/ว่าง"
        if r["invoice_no"] in sent_before: return "ส่งไปแล้ว"
        if r["invoice_no"] not in pdfs:    return "ไม่พบ PDF"
        return "พร้อมส่ง"

    df["สถานะ"] = df.apply(status, axis=1)
    st.dataframe(df[["invoice_no", "channel", "email", "สถานะ"]])
    ready = df[df["สถานะ"] == "พร้อมส่ง"]
    st.write(f"พร้อมส่ง {len(ready)} ใบ / {ready['email'].nunique()} อีเมล")

    if st.button("เริ่มส่งอีเมล") and len(ready):
        mails = []
        for email, g in ready.groupby("email"):
            for batch in split_batches(list(g["invoice_no"]), pdfs):
                mails.append((email, batch))

        log, bar = [], st.progress(0)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(st.secrets["EMAIL"], st.secrets["EMAIL_APP_PASSWORD"])
            for i, (email, invs) in enumerate(mails):
                try:
                    m = EmailMessage()
                    m["From"] = st.secrets["EMAIL"]
                    m["To"] = email
                    m["Subject"] = "ใบกำกับภาษีเลขที่ " + ", ".join(invs)
                    m.set_content("เรียนลูกค้า\n\nแนบใบกำกับภาษีมาพร้อมอีเมลนี้ค่ะ\n\nขอบคุณค่ะ")
                    for inv in invs:
                        m.add_attachment(pdfs[inv], maintype="application",
                                         subtype="pdf", filename=f"{inv}.pdf")
                    s.send_message(m)
                    res = "สำเร็จ"
                except Exception as e:
                    res = f"ล้มเหลว: {e}"
                for inv in invs:
                    log.append((inv, email, res))
                bar.progress((i + 1) / len(mails))
                time.sleep(1)
        out = pd.DataFrame(log, columns=["invoice_no", "email", "สถานะ"])
        st.dataframe(out)
        st.download_button("ดาวน์โหลดผลการส่ง",
                           out.to_csv(index=False).encode("utf-8-sig"), "result.csv")
