import re, smtplib, time
import pandas as pd
import streamlit as st
from helpers import (load_sheet, load_pdfs, split_batches, count_sent_today,
                     get_memory_mb, now_th, build_message, last_message_ids,
                     imap_connect, find_last_message, plan_subject, DAILY_LIMIT, UPLOAD_LIMIT_MB)

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
    st.divider()
    st.write("**โควต้าการส่งเมลวันนี้**")
    quota_box = st.container()
    if not (data_file and zip_files):
        quota_box.caption(f"อัปโหลดไฟล์ข้อ 1 และ 2 ก่อน จะแสดงโควต้า (เต็ม {DAILY_LIMIT} เมล/วัน)")

# CC ทุกเมล (สาขา + ลูกค้า): ตั้งใน Secrets ที่ CC_EMAIL (หรือ BRANCH_CC ชื่อเดิม)
CC = str(st.secrets.get("CC_EMAIL", st.secrets.get("BRANCH_CC", ""))).strip()
N_CC = len([x for x in CC.split(",") if x.strip()])
# เมลลูกค้ามี CC สาขาเพิ่มอีก 1 ผู้รับ จึงคิดโควต้าแบบกันพลาด: To + CC + สาขา
MAIL_LIMIT = DAILY_LIMIT // (2 + N_CC)        # Gmail นับตามจำนวนผู้รับ (To + CC)

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
            "inv_branch": dict(zip(g["invoice_no"], g["branch"].fillna(""))),
            "is_cust": kind == "ลูกค้า",
        }
        for batch in split_batches(list(g["invoice_no"]), pdfs):
            mails.append((email, batch, info))

    used_today = count_sent_today(prev_dfs)
    remaining = max(MAIL_LIMIT - used_today, 0)
    st.write(f"พร้อมส่ง {len(ready)} ใบ = **{len(mails)} เมล** / {ready['ส่งไปที่'].nunique()} อีเมล")
    with quota_box:
        st.progress(min(used_today / MAIL_LIMIT, 1.0))
        st.write(f"ส่งแล้ววันนี้: **{used_today}** / {MAIL_LIMIT} เมล")
        st.write(f"โควต้าเหลือ: **{remaining}**")
        st.write(f"หลังส่งรอบนี้เหลือ: **{max(remaining - len(mails), 0)}**")
        st.caption(f"Gmail จำกัด {DAILY_LIMIT} ผู้รับ/วัน (นับ To + CC) เมลลูกค้าละ {2 + N_CC} ผู้รับ (รวม CC สาขา) จึงคิดส่งได้ {MAIL_LIMIT} เมล "
                   "นับจากไฟล์ผลการส่งที่อัปโหลดในช่องที่ 3 เท่านั้น ไม่รวมเมลที่ส่งจากทางอื่นด้วยบัญชีเดียวกัน")
    if len(mails) > remaining:
        st.warning(f"เมลที่ต้องส่ง ({len(mails)}) เกินโควต้าที่เหลือ ({remaining}) "
                   f"ระบบจะส่งแค่ {remaining} เมลแรก ที่เหลือส่งพรุ่งนี้")

    def mail_cc(to, invs, info):
        """CC กลาง + (เมลลูกค้า) อีเมลสาขาของใบกำกับในเมลนั้น ตัดซ้ำกับผู้รับ"""
        parts = [x.strip() for x in CC.split(",") if x.strip()]
        if info.get("is_cust"):
            for b in sorted({info["inv_branch"].get(i, "") for i in invs}):
                be = branch_map.get(str(b).strip().upper())
                if be: parts.append(be)
        seen, out = {str(to).strip().lower()}, []
        for p in parts:
            addr = re.sub(r".*<([^>]+)>.*", r"\1", p).strip().lower()
            if addr not in seen:
                seen.add(addr); out.append(p)
        return ", ".join(out) or None

    use_thread = st.checkbox("ตอบกลับเมลเดิมของผู้รับ (ค้นจากโฟลเดอร์ 'ส่งแล้ว' ใน Gmail ไม่ต้องอัปโหลดข้อ 3)",
                             value=True)

    # cache ผลค้นเมลเดิม: {(อีเมลผู้รับ, สาขา|None): (Message-ID, หัวเรื่อง) หรือ None ถ้าไม่พบ}
    cache = st.session_state.setdefault("reply_cache", {})
    keys = sorted({(str(e).strip().lower(), inf["branch"]) for e, _, inf in mails},
                  key=lambda x: (x[0], x[1] or ""))
    if use_thread and mails and st.button("ตรวจหาเมลเดิมที่จะ Re: (ก่อนกดส่ง)"):
        try:
            with st.spinner("กำลังค้นเมลเดิมใน Gmail..."):
                imap = imap_connect(st.secrets["EMAIL"], st.secrets["EMAIL_APP_PASSWORD"])
                for addr, br in keys:
                    cache[(addr, br)] = find_last_message(imap, addr, branch=br)
                imap.logout()
        except Exception as ex:
            st.warning(f"ค้นเมลเดิมใน Gmail ไม่สำเร็จ ({ex})")
    checked = (not use_thread) or all(k in cache for k in keys)

    # สรุปปลายทางก่อนส่ง (กันพลาด) พร้อมบอกว่าจะ Re: หัวข้อไหน
    if mails:
        rows = []
        for e, invs, inf in mails:
            k = (str(e).strip().lower(), inf["branch"])
            if not use_thread:
                found, how = (None, None), "เมลใหม่ (ปิดการตอบกลับ)"
            elif k in cache:
                found = cache[k] or (None, None)
                how = None
            else:
                found, how = (None, None), "ยังไม่ตรวจ"
            subj, eff = plan_subject(inf["cust"], inf["branch"], e, found[0], found[1])
            if how is None:
                how = "Re: ต่อเมลเดิม" if eff else "เมลใหม่ (ไม่พบเมลเดิม)"
            rows.append({"ส่งไปที่": e, "ประเภท": "สาขา" if inf["branch"] else "ลูกค้า",
                         "สาขา": inf["branch"] or "", "จำนวนใบ": len(invs),
                         "การส่ง": how, "หัวเรื่องที่จะใช้": subj,
                         "เมลเดิมที่จะ Re:": (found[1] or "") if eff else ""})
        summary = pd.DataFrame(rows)
        with st.expander(f"สรุปปลายทางก่อนส่ง ({len(mails)} เมล: "
                         f"สาขา {int((summary['ประเภท'] == 'สาขา').sum())} / "
                         f"ลูกค้า {int((summary['ประเภท'] == 'ลูกค้า').sum())})", expanded=True):
            st.dataframe(summary, hide_index=True)
        if use_thread and not checked:
            st.info("กดปุ่ม 'ตรวจหาเมลเดิมที่จะ Re:' ก่อน เพื่อให้ตารางบอกว่าจะต่อหัวข้อเมลไหน แล้วจึงกดส่งได้")
    confirmed = st.checkbox("ตรวจรายการ ปลายทาง และหัวข้อที่จะ Re: แล้ว พร้อมส่ง", value=False)

    if st.button("เริ่มส่งอีเมล", disabled=not (confirmed and checked)) and len(ready):
        mails = mails[:remaining]
        if not mails:
            st.error("โควต้าวันนี้หมดแล้ว")
            st.stop()
        if use_thread:
            reply_ids = {k: v for k, v in cache.items() if v}      # ตามที่แสดงในตารางเท่านั้น
        else:                                                      # สำรองจากไฟล์ผลการส่ง (ถ้ามี)
            reply_ids = {(k, None): (v, None) for k, v in last_message_ids(prev_dfs).items()}
        log, bar = [], st.progress(0)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(st.secrets["EMAIL"], st.secrets["EMAIL_APP_PASSWORD"])
            for i, (email, invs, info) in enumerate(mails):
                try:
                    k = (str(email).strip().lower(), info["branch"])
                    rid, psub = reply_ids.get(k, (None, None))
                    m = build_message(st.secrets["EMAIL"],
                                      st.secrets.get("SENDER_NAME", ""),
                                      email, info["cust"], invs, pdfs,
                                      branch=info["branch"],
                                      cust_by_inv=info["cust_by_inv"],
                                      reply_to_id=rid, prev_subject=psub,
                                      cc=mail_cc(email, invs, info))
                    if info["branch"] and not re.search(
                            r"\(\s*สาขา\s+" + re.escape(info["branch"]) + r"\b", m["Subject"], re.I):
                        raise RuntimeError("หัวเรื่องเมลสาขาไม่ถูกต้อง ระบบไม่ส่งเพื่อกันพลาด")
                    s.send_message(m)
                    res = "สำเร็จ"
                    mid = m["Message-ID"]
                    reply_ids[k] = (mid, m["Subject"])
                except Exception as e:
                    res, mid = f"ล้มเหลว: {e}", ""
                t = now_th().strftime("%Y-%m-%d %H:%M:%S")
                for inv in invs:
                    log.append((inv, email, res, t, mid))
                bar.progress((i + 1) / len(mails))
                time.sleep(1)
        st.session_state["reply_cache"] = {}      # ส่งแล้ว ต้องค้นใหม่ครั้งหน้า
        out = pd.DataFrame(log, columns=["invoice_no", "email", "สถานะ", "ส่งเมื่อ", "message_id"])
        st.dataframe(out)
        st.download_button("ดาวน์โหลดผลการส่ง",
                           out.to_csv(index=False).encode("utf-8-sig"), f"result_{now_th():%Y-%m-%d_%H%M}.csv")
