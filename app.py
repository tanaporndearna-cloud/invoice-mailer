import smtplib, time
import pandas as pd
import streamlit as st
from helpers import (load_sheet, load_pdfs, split_batches, count_sent_today,
                     get_memory_mb, now_th, build_message, last_message_ids,
                     imap_connect, find_last_message_id, DAILY_LIMIT, UPLOAD_LIMIT_MB)

st.set_page_config(page_title="ส่งใบกำกับภาษี", page_icon="📧")
st.title("ส่งใบกำกับภาษีทางอีเมล")

if st.text_input("รหัสเข้าใช้งาน", type="password") != st.secrets["APP_PASSWORD"]:
    st.stop()

data_file = st.file_uploader("1) ไฟล์รายงานภาษีขาย (Excel/CSV)", type=["xlsx", "csv"])
zip_files = st.file_uploader("2) ไฟล์ zip รวม PDF (ใส่ได้หลายไฟล์)", type=["zip"],
                             accept_multiple_files=True)
prev_files = st.file_uploader("3) ผลการส่งครั้งก่อน (ไม่บังคับ ใส่ได้หลายไฟล์ ไว้กันส่งซ้ำ)",
                              type=["csv"], accept_multiple_files=True)

# ---------- แถบด้านข้าง: RAM และขนาดไฟล์ ----------
with st.sidebar:
    st.subheader("สถานะระบบ")
    used, limit = get_memory_mb()
    if used is not None:
        if limit:
            st.progress(min(used / limit, 1.0))
            st.write(f"RAM: ใช้ {used:,.0f} / {limit:,.0f} MB (เหลือ {limit - used:,.0f} MB)")
            if used / limit > 0.8:
                st.warning("RAM ใกล้เต็ม ควรแบ่งส่งเป็นรอบๆ")
        else:
            st.write(f"RAM: ใช้ {used:,.0f} MB (ไม่ทราบลิมิต)")
    else:
        st.write("RAM: อ่านค่าไม่ได้")
    st.divider()
    st.write(f"ลิมิตอัปโหลด: {UPLOAD_LIMIT_MB} MB **ต่อไฟล์**")
    if zip_files:
        total = 0
        for z in zip_files:
            mb = z.size / 1024 / 1024
            total += mb
            st.progress(min(mb / UPLOAD_LIMIT_MB, 1.0))
            st.caption(f"{z.name}: {mb:,.1f} MB (เหลือ {UPLOAD_LIMIT_MB - mb:,.1f} MB ในไฟล์นี้)")
        st.write(f"รวม zip ทั้งหมด: {total:,.1f} MB")
    else:
        st.caption("ยังไม่ได้อัปโหลด zip")

if data_file and zip_files:
    df = load_sheet(data_file)
    pdfs, ranges = load_pdfs(zip_files)

    if ranges:
        st.warning("ไฟล์เหล่านี้เป็นช่วงหลายใบ ระบบไม่ส่งให้ ต้องเช็กเอง: " + ", ".join(ranges))

    sent_before, prev_dfs = set(), []
    for pf in prev_files:
        p = pd.read_csv(pf, dtype=str)
        prev_dfs.append(p)
        sent_before |= set(p.loc[p["สถานะ"] == "สำเร็จ", "invoice_no"])
    if prev_files:
        st.caption(f"โหลดประวัติการส่งแล้ว {len(sent_before)} ใบ จาก {len(prev_files)} ไฟล์")

    # อีเมลสาขา อ่านจาก Secrets: [BRANCH_EMAILS]  T4 = "xxx@gmail.com"
    try:
        branch_map = {str(k).strip().upper(): str(v).strip()
                      for k, v in dict(st.secrets["BRANCH_EMAILS"]).items()}
    except Exception:
        branch_map = {}

    def target(r):
        if r["via_email"]:  return r["email"]
        if r["via_branch"]: return branch_map.get(r["branch"]) or None
        return None

    df["ส่งไปที่"] = df.apply(target, axis=1)
    df["ประเภท"] = df.apply(
        lambda r: "ลูกค้า" if r["via_email"] else ("สาขา" if r["via_branch"] else "-"), axis=1)

    def status(r):
        if not r["via_email"] and not r["via_branch"]:
            return "ช่องทางอื่น (ไม่ส่ง)"
        if r["via_branch"] and not r["via_email"] and not r["ส่งไปที่"]:
            return f"ไม่มีอีเมลสาขา ({r['branch'] or 'ว่าง'})"
        if not r["ส่งไปที่"]:              return "อีเมลไม่ถูกต้อง/ว่าง"
        if r["invoice_no"] in sent_before: return "ส่งไปแล้ว"
        if r["invoice_no"] not in pdfs:    return "ไม่พบ PDF"
        return "พร้อมส่ง"

    df["สถานะ"] = df.apply(status, axis=1)
    st.dataframe(df[["invoice_no", "channel", "branch", "ประเภท", "ส่งไปที่", "สถานะ"]])
    ready = df[df["สถานะ"] == "พร้อมส่ง"]

    # จำนวนเมลที่จะส่งจริง (รวมลูกค้าคนเดียวหลายใบ และแบ่งตามขนาด)
    mails = []
    for (email, kind), g in ready.groupby(["ส่งไปที่", "ประเภท"]):
        names = [str(n).strip() for n in g["customer"].dropna().unique()]
        info = {
            "cust": " / ".join(names),
            "branch": g["branch"].iloc[0] if kind == "สาขา" else None,
            "cust_by_inv": dict(zip(g["invoice_no"], g["customer"].fillna(""))),
        }
        for batch in split_batches(list(g["invoice_no"]), pdfs):
            mails.append((email, batch, info))

    used_today = count_sent_today(prev_dfs)
    remaining = max(DAILY_LIMIT - used_today, 0)
    st.write(f"พร้อมส่ง {len(ready)} ใบ = **{len(mails)} เมล** / {ready['ส่งไปที่'].nunique()} อีเมล")
    c1, c2, c3 = st.columns(3)
    c1.metric("ส่งแล้ววันนี้", used_today)
    c2.metric("โควต้าเหลือ", f"{remaining} / {DAILY_LIMIT}")
    c3.metric("หลังส่งรอบนี้เหลือ", max(remaining - len(mails), 0))
    st.caption("นับจากไฟล์ผลการส่งที่อัปโหลดในช่องที่ 3 เท่านั้น (เฉพาะไฟล์ที่ดาวน์โหลดจากแอปเวอร์ชันนี้) "
               "ไม่รวมเมลที่ส่งจากทางอื่นด้วยบัญชีเดียวกัน")
    if len(mails) > remaining:
        st.warning(f"เมลที่ต้องส่ง ({len(mails)}) เกินโควต้าที่เหลือ ({remaining}) "
                   f"ระบบจะส่งแค่ {remaining} เมลแรก ที่เหลือส่งพรุ่งนี้")

    use_thread = st.checkbox("ตอบกลับเมลเดิมของผู้รับ (ค้นจากโฟลเดอร์ 'ส่งแล้ว' ใน Gmail ไม่ต้องอัปโหลดข้อ 3)",
                             value=True)

    if st.button("เริ่มส่งอีเมล") and len(ready):
        mails = mails[:remaining]
        if not mails:
            st.error("โควต้าวันนี้หมดแล้ว")
            st.stop()
        reply_ids = last_message_ids(prev_dfs)      # สำรองจากไฟล์ผลการส่ง (ถ้ามี)
        if use_thread:
            try:
                with st.spinner("กำลังค้นเมลเดิมใน Gmail..."):
                    imap = imap_connect(st.secrets["EMAIL"], st.secrets["EMAIL_APP_PASSWORD"])
                    found = 0
                    for addr in sorted({str(e).strip().lower() for e, _, _ in mails}):
                        mid0 = find_last_message_id(imap, addr)
                        if mid0:
                            reply_ids[addr] = mid0
                            found += 1
                    imap.logout()
                st.info(f"พบเมลเดิมที่จะตอบกลับ {found} ราย จาก {len({str(e).lower() for e, _, _ in mails})} ราย "
                        "(รายอื่นจะเป็นเมลใหม่)")
            except Exception as ex:
                st.warning(f"ค้นเมลเดิมใน Gmail ไม่สำเร็จ ({ex}) จะส่งเป็นเมลใหม่ "
                           "(หรือใช้ข้อมูลจากไฟล์ผลการส่งถ้ามี)")
        log, bar = [], st.progress(0)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(st.secrets["EMAIL"], st.secrets["EMAIL_APP_PASSWORD"])
            for i, (email, invs, info) in enumerate(mails):
                try:
                    m = build_message(st.secrets["EMAIL"],
                                      st.secrets.get("SENDER_NAME", ""),
                                      email, info["cust"], invs, pdfs,
                                      branch=info["branch"],
                                      cust_by_inv=info["cust_by_inv"],
                                      reply_to_id=reply_ids.get(str(email).strip().lower()))
                    s.send_message(m)
                    res = "สำเร็จ"
                    mid = m["Message-ID"]
                    reply_ids[str(email).strip().lower()] = mid
                except Exception as e:
                    res, mid = f"ล้มเหลว: {e}", ""
                t = now_th().strftime("%Y-%m-%d %H:%M:%S")
                for inv in invs:
                    log.append((inv, email, res, t, mid))
                bar.progress((i + 1) / len(mails))
                time.sleep(1)
        out = pd.DataFrame(log, columns=["invoice_no", "email", "สถานะ", "ส่งเมื่อ", "message_id"])
        st.dataframe(out)
        st.download_button("ดาวน์โหลดผลการส่ง",
                           out.to_csv(index=False).encode("utf-8-sig"), f"result_{now_th():%Y-%m-%d_%H%M}.csv")
