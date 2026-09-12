# -*- coding: utf-8 -*-
"""Rebuild the Status tab: one screen answering "where am I right now?".

Four zones, conclusion first and evidence below:

  1 判定    verdict: is the race window open, what phase, this week's load
            target, the next checkpoint
  2 現在値  the current numbers: CTL / ATL / TSB, ACWR, VO2max, race
            predictions, lactate threshold, condition and HRV seven-day means
  3 週次    the last twelve weeks, newest first
  4 マクロ  the year by month, and where CTL sits in its own range

Everything is computed here and written as static values, with no live
spreadsheet formulas. The tab is rebuilt after every nightly refresh, so it is
never more than a day stale, and there is no formula-error surface at all.

Three rules this dashboard follows, each learned from being wrong once:

  * **Refuse to judge on incomplete data.** If any day inside the seven-day
    fatigue window was not imported, TSB is an estimate, and the verdict is
    withheld rather than guessed. A sync gap once opened the race window by
    itself.
  * **Deltas before absolutes** for the predictions. Garmin's race-time
    estimates can sit a long way from what an athlete actually runs, while
    their movement over four weeks is still informative.
  * **Say which band is doing the judging.** The condition score judges HRV
    against Garmin's own personal band, not against a standard deviation
    computed from the log. The two disagree, so both are shown and the one in
    use is named.

  python -m training_log.status_panel          # write (the nightly path)
  python -m training_log.status_panel --dry-run
"""
import argparse
import datetime as dt
import math
import sys

from . import config, garmin_fetch, log_io, pmc

TAB = config.STATUS_TAB
NCOL = 8

# 種別 values that are NOT a quality session.
EASY = {"", "jog", "off", "rest"}

# Columns that prove a day was actually imported: positive when real, empty
# otherwise. See pmc.day_fields for why columns where 0 is meaningful cannot
# serve as evidence.
SYNC_EVIDENCE = ("歩数", "睡眠h", "負荷")

STATUS_JP = {
    "RECOVERY": "回復期", "PRODUCTIVE": "好調(生産的)", "MAINTAINING": "維持",
    "OVERREACHING": "オーバーリーチ", "DETRAINING": "トレ不足",
    "UNPRODUCTIVE": "非生産的", "PEAKING": "ピーキング", "NO": "判定なし",
}


def rgb(hexs):
    return {"red": int(hexs[0:2], 16) / 255, "green": int(hexs[2:4], 16) / 255,
            "blue": int(hexs[4:6], 16) / 255}


B1, B4, NAVY = rgb("E8F0FE"), rgb("3D85C6"), rgb("0B5394")
BORDER, WHITE = rgb("D5E3F7"), rgb("FFFFFF")
AMBER, RED = rgb("9A6700"), rgb("A61C00")

fnum = pmc.fnum


# --------------------------------------------------------------------------- #
# data                                                                         #
# --------------------------------------------------------------------------- #
def read_log(sh):
    """Log rows -> {date: dict(race, kind, km, hrv, cond, acwr, load, ...)}.

    Columns are resolved by header NAME. Resolving them by position means that
    inserting a column silently starts reading its neighbour, and a dashboard
    reading the wrong column still renders perfectly.
    """
    grid = sh.worksheet(log_io.TAB).get_all_values()
    hdr = grid[0] if grid else []
    ix = {str(name).strip(): i for i, name in enumerate(hdr) if str(name).strip()}
    ev = [ix[k] for k in SYNC_EVIDENCE if k in ix]
    race_col = ix.get("試合", ix.get("予定"))
    days = {}
    for r in grid[1:]:
        if not r or not r[0].strip():
            continue
        try:
            d = dt.date.fromisoformat(r[0].strip())
        except ValueError:
            continue

        def g(i):
            return r[i] if (i is not None and i < len(r)) else ""

        load, load_unknown, synced = pmc.day_fields(
            g(ix.get("負荷")), g(ix.get("種別")), [g(i) for i in ev])
        days[d] = {"race": g(race_col).strip(),
                   "kind": g(ix.get("種別")).strip().lower(),
                   "km": fnum(g(ix.get("km"))) or 0.0,
                   "hrv": fnum(g(ix.get("HRV"))),
                   "cond": fnum(g(ix.get("体調点"))),
                   "acwr": fnum(g(ix.get("ACWR"))),
                   "load": load, "load_unknown": load_unknown, "synced": synced}
    return days


def last_allout(days, today, allout_load):
    """(date, days ago) of the most recent maximal effort, else (None, None).

    Decided from the session label, not from the hand-written race column: that
    column is where events of every kind get written, including ones that are
    not races at all, while the label column is produced by the classifier and
    means exactly one thing. Anaerobic sessions need a load floor as well, so a
    short set of strides is not counted as a maximal effort.
    """
    best = None
    for d, x in days.items():
        if d > today:
            continue
        kind = str(x.get("kind", ""))
        if "race" in kind or ("anaerobic" in kind
                              and (x.get("load") or 0.0) >= allout_load):
            if best is None or d > best:
                best = d
    return (best, (today - best).days) if best else (None, None)


def allout_note(days, today, allout_load, window):
    """The warning text while inside the window after a maximal effort.

    Every autonomic metric on this dashboard (TSB, readiness, HRV, resting
    heart rate, the condition score) is measuring the same recovery, so in
    the days after an all-out effort they agree with each other and are all
    wrong together. Five green lights on one morning are one green light.
    """
    d, n = last_allout(days, today, allout_load)
    if d is None or n > window:
        return None
    return (f"{d.month}/{d.day} の全力から{n}日 — この窓では TSB(体力−疲労)・準備度・"
            f"HRV・安静時脈・体調点を判定に使わない。5つとも同じ自律神経の回復を"
            f"測っていて、{window}日以内は揃って『万全』と誤る。代わりに"
            f"『全力から何日空いたか』と本人の脚の体感を見る。")


def weeks_of(days, bd, today, n=26):
    """The last n Monday-Sunday weeks, oldest first."""
    mon = today - dt.timedelta(days=today.weekday())
    out = []
    for i in range(n - 1, -1, -1):
        w0 = mon - dt.timedelta(weeks=i)
        w1 = min(w0 + dt.timedelta(days=6), today)
        span = [days.get(w0 + dt.timedelta(days=j), {})
                for j in range((w1 - w0).days + 1)]
        conds = [x["cond"] for x in span if x.get("cond") is not None]
        hrvs = [x["hrv"] for x in span if x.get("hrv") is not None]
        races = [x["race"] for x in span if x.get("race")]
        c, a = bd.get(w1, (None, None))
        out.append({
            "w0": w0, "cur": w0 == mon,
            "km": round(sum(x.get("km", 0) or 0 for x in span), 1),
            "load": round(sum(x.get("load", 0) or 0 for x in span)),
            "pts": sum(1 for x in span if x.get("kind", "") not in EASY),
            "cond": round(sum(conds) / len(conds)) if conds else None,
            "hrv": round(sum(hrvs) / len(hrvs)) if hrvs else None,
            "ctl": round(c) if c is not None else None,
            "tsb": round(c - a) if c is not None else None,
            "races": " / ".join(dict.fromkeys(races)),
        })
    return out


def months_of(days, bd, today):
    """This calendar year month by month, up to the current month."""
    out = []
    year = today.year
    for m in range(1, today.month + 1):
        m0 = dt.date(year, m, 1)
        last = (dt.date(year, m + 1, 1) - dt.timedelta(days=1)
                if m < 12 else dt.date(year, 12, 31))
        m1 = min(last, today)
        span = [(d, x) for d, x in days.items() if m0 <= d <= m1]
        races = [x["race"] for d, x in sorted(span) if x["race"]]
        c, _ = bd.get(m1, (None, None))
        out.append({"m": m, "end": m1,
                    "km": round(sum(x["km"] for _, x in span), 1),
                    "load": round(sum(x["load"] for _, x in span)),
                    "ctl": round(c) if c is not None else None,
                    "races": " / ".join(dict.fromkeys(races))})
    return out


def hrv_band(g, today, back=7):
    """Garmin's own HRV band (balancedLow, balancedUpper), or None.

    This is the band the condition score judges against. A mean and standard
    deviation computed from the log is a different statistic, and showing only
    that one lets two screens read the same HRV in opposite directions.
    """
    for i in range(back):
        d = (today - dt.timedelta(days=i)).isoformat()
        summary = (garmin_fetch._try(g.get_hrv_data, d) or {}).get("hrvSummary") or {}
        base = summary.get("baseline") or {}
        lo, hi = base.get("balancedLow"), base.get("balancedUpper")
        if lo and hi:
            return lo, hi
    return None


def vo2_at(g, iso):
    r = garmin_fetch._try(g.get_max_metrics, iso)
    for it in (r if isinstance(r, list) else [r] if r else []):
        if isinstance(it, dict):
            gen = it.get("generic") or {}
            v = gen.get("vo2MaxPreciseValue") or gen.get("vo2MaxValue")
            if v:
                return round(float(v), 1)
    return None


def race_pred_hist(g, d0, d1):
    """[{calendarDate, time5K, time10K}]; the API shape is version dependent."""
    for typ in ("daily", "monthly", None):
        r = garmin_fetch._try(g.get_race_predictions, d0.isoformat(),
                              d1.isoformat(), typ)
        if isinstance(r, list) and r and isinstance(r[0], dict):
            return [x for x in r if x.get("calendarDate")]
    return []


def hms(sec):
    if not sec:
        return ""
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def snum(v, nd=0):
    """A delta as a NUMBER. The leading '+' comes from the number format, not
    from the string: USER_ENTERED reads a leading '+' as a formula."""
    return "" if v is None else round(v, nd)


P0 = '+0;-0;0'                        # signed-integer display pattern


def status_jp(phrase):
    if not phrase:
        return "-"
    for k, v in STATUS_JP.items():
        if phrase.startswith(k):
            return v
    return phrase


def spark(vals, chart="line", ymin=None, ymax=None):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return ""
    if chart == "line":
        o = '"charttype","line";"color","#0B5394";"linewidth",2'
    else:
        o = '"charttype","column";"color","#3D85C6"'
    if ymin is not None:
        o += f';"ymin",{ymin}'
    if ymax is not None:
        o += f';"ymax",{ymax}'
    return "=SPARKLINE({" + ",".join(str(v) for v in vals) + "},{" + o + "})"


# --------------------------------------------------------------------------- #
# verdict (rule-based so it is reproducible; the thresholds are inferences)    #
# --------------------------------------------------------------------------- #
def verdict(ctl, tsb, pct, slope, open_pct, gaps7=()):
    if gaps7:
        ds = "・".join(f"{d.month}/{d.day}" for d in sorted(gaps7))
        return False, (f"判定保留 — 直近7日に取り込めていない日({ds})。TSB が推定値に"
                       f"なるので窓の開閉は言わない。時計の同期か refresh を確認")
    if pct >= open_pct and tsb > 0:
        return True, "開 — CTL上位・疲労も抜けている。出れば力が出る局面(推論)"
    if pct >= open_pct and tsb > -10:
        return False, "準備中 — 体力は高い。数日負荷を落とせば開く(推論)"
    if slope > 2:
        return False, "閉(構築中) — CTL回復中。窓はまだ先、焦らず積む(推論)"
    return False, "閉(再構築期) — まず慢性負荷を戻すのが先(推論)"


def hrv_note(band, hm, hs):
    """The band in use first; the log's own statistics after, named as such."""
    parts = []
    if band:
        parts.append(f"平常帯 {band[0]:.0f}〜{band[1]:.0f}(Garmin・体調点はこれで判定)")
    if hm and hs:
        parts.append(f"実測28日 {hm:.0f}±{hs:.0f}(ばらつき・帯ではない)")
    return " / ".join(parts)


def phase_text(pct, slope, since):
    trend = "上昇中" if slope > 2 else ("低下中" if slope < -2 else "横ばい")
    return (f"CTLは4週前比{slope:+.0f}で{trend}。"
            f"年内レンジの{pct*100:.0f}%点に位置({since}以降比)")


# --------------------------------------------------------------------------- #
# grid                                                                         #
# --------------------------------------------------------------------------- #
def build(days, bd, today, panel, extras, athlete, unknown=frozenset(), dry=False):
    st = athlete.section("status")
    _, _, _, pct_from = pmc.params(athlete)
    acwr_lo, acwr_hi = st["acwr_safe"]
    tgt_pct = float(st["week_load_target_pct"])
    allout_load, allout_window = st["all_out_load"], st["all_out_window_days"]

    weeks = weeks_of(days, bd, today)
    months = months_of(days, bd, today)
    ctl, atl = bd[today]
    tsb = ctl - atl
    c4, a4 = bd.get(today - dt.timedelta(days=28), (None, None))
    rng = [c for d, (c, _) in bd.items() if d >= pct_from] or [ctl]
    lo, hi = min(rng), max(rng)
    pct = (ctl - lo) / (hi - lo) if hi > lo else 0.0
    slope = ctl - c4 if c4 is not None else 0.0
    # A gap inside the 7-day fatigue window stops the verdict; a gap further
    # back only earns a note.
    gaps7 = {d for d in unknown if 0 <= (today - d).days <= 6}
    gaps42 = {d for d in unknown if 0 <= (today - d).days <= 41}
    is_open, vtext = verdict(ctl, tsb, pct, slope, float(st["ctl_percentile_open"]),
                             gaps7)

    def wavg(back0, back1, key):
        vs = [days.get(today - dt.timedelta(days=i), {}).get(key)
              for i in range(back0, back1 + 1)]
        vs = [v for v in vs if v is not None]
        return (sum(vs) / len(vs)) if vs else None

    cond7, cond7p = wavg(0, 6, "cond"), wavg(28, 34, "cond")
    hrv7, hrv7p = wavg(0, 6, "hrv"), wavg(28, 34, "hrv")
    h28 = [days.get(today - dt.timedelta(days=i), {}).get("hrv") for i in range(1, 29)]
    h28 = [v for v in h28 if v is not None]
    hm = sum(h28) / len(h28) if h28 else None
    hs = (math.sqrt(sum((v - hm) ** 2 for v in h28) / (len(h28) - 1))
          if h28 and len(h28) > 2 else None)
    acwr_now = next((days[today - dt.timedelta(days=i)]["acwr"] for i in range(0, 8)
                     if days.get(today - dt.timedelta(days=i), {}).get("acwr")
                     is not None), None)
    acwr_p = next((days[today - dt.timedelta(days=28 + i)]["acwr"] for i in range(0, 4)
                   if days.get(today - dt.timedelta(days=28 + i), {}).get("acwr")
                   is not None), None)

    wk_now = weeks[-1]
    tgt_lo = int(round(ctl * 7 * (1 - tgt_pct), -1))
    tgt_hi = int(round(ctl * 7 * (1 + tgt_pct), -1))
    nxt = next(((dt.date.fromisoformat(d), t) for d, t in athlete.races
                if dt.date.fromisoformat(d) >= today), None)

    rows, kind, cellfmt, numfmt = [], [], [], []

    def add(row, k):
        rows.append((row + [""] * NCOL)[:NCOL])
        kind.append(k)
        return len(rows)                  # 1-based sheet row

    add([f"Status — 分析ダッシュボード(更新 {today})"], "title")

    # -- zone 1: verdict ---------------------------------------------------- #
    add(["① 判定 — いま何をすべきか"], "band")
    r = add(["レース窓", vtext], "z1")
    if is_open:
        cellfmt.append((r - 1, 1, {"foregroundColor": NAVY, "bold": True}))
    if gaps42:
        ds = "・".join(f"{d.month}/{d.day}" for d in sorted(gaps42)[:8])
        more = f" 他{len(gaps42) - 8}日" if len(gaps42) > 8 else ""
        r = add(["データ欠測", f"{len(gaps42)}日 未同期({ds}{more})。CTL/ATL/TSB は"
                 f"その日を『典型的な1日』で仮置きした推定値"], "z1")
        cellfmt.append((r - 1, 1, {"foregroundColor": RED, "bold": True}))
    anote = allout_note(days, today, allout_load, allout_window)
    if anote:
        r = add(["全力の直後", anote], "z1")
        cellfmt.append((r - 1, 1, {"foregroundColor": AMBER, "bold": True}))
    add(["フェーズ", phase_text(pct, slope, pct_from)], "z1")
    gw = sum(1 for d in unknown if wk_now["w0"] <= d <= today)
    add(["今週の負荷", f"実績 {wk_now['load']}(月〜今日"
         + (f"・うち{gw}日未同期のため過少" if gw else "")
         + f") / 目標帯 {tgt_lo}〜{tgt_hi}(CTL×7の±{tgt_pct*100:.0f}%・推論)"], "z1")
    add(["次の測定", (f"{nxt[0].month}/{nxt[0].day} {nxt[1]} — あと"
                    f"{(nxt[0] - today).days}日" if nxt else "未設定")], "z1")

    # -- zone 2: now -------------------------------------------------------- #
    add(["② 現在値 — 主要指標(Δ=4週前比)"], "band")
    add(["指標", "値", "Δ4週", "推移(26週)", "", "", "補足"], "sub")
    wctl = [w["ctl"] for w in weeks]
    wtsb = [w["tsb"] for w in weeks]
    wcond = [w["cond"] for w in weeks]
    whrv = [w["hrv"] for w in weeks]
    p5, p5d, p10, p10d, vo2, vo2d, _, hband = extras
    SEC = '+0"秒";-0"秒";0'
    ctl_days, atl_days, _, _ = pmc.params(athlete)
    z2 = [  # label, value, value pattern, delta, delta pattern, spark, note, mark
        ("CTL(体力)", round(ctl), None, snum(slope), P0, spark(wctl, ymin=0),
         f"負荷の{ctl_days}日指数平均", None),
        ("ATL(疲労)", round(atl), None,
         snum(atl - a4) if a4 is not None else "", P0, "",
         f"負荷の{atl_days}日指数平均", None),
        ("TSB(フォーム)", snum(tsb), P0,
         snum(tsb - (c4 - a4)) if c4 is not None else "", P0,
         spark(wtsb, chart="column"), "正=疲労が抜けた状態", None),
        ("ACWR", acwr_now if acwr_now is not None else "", None,
         snum(acwr_now - acwr_p, 2) if None not in (acwr_now, acwr_p) else "",
         "+0.00;-0.00;0", "", f"{acwr_lo}〜{acwr_hi}が安全域",
         "out" if acwr_now is not None
         and not (acwr_lo <= acwr_now <= acwr_hi) else None),
        ("VO2max", vo2 if vo2 else "", None,
         snum(vo2d, 1) if vo2d is not None else "", "+0.0;-0.0;0", "", "", None),
        ("5k予測", "'" + hms(p5) if p5 else "", None, snum(p5d), SEC, "",
         "絶対値は参考外・Δのみ読む", None),
        ("10k予測", "'" + hms(p10) if p10 else "", None, snum(p10d), SEC, "",
         "同上(-=速くなった)", None),
        ("LT", (f"{panel.get('lt_hr')}bpm・{panel.get('lt_pace') or '-'}"
                if panel.get("lt_hr") else ""), None, "", None, "",
         "閾値走のアンカー", None),
        ("Garmin判定", status_jp(panel.get("train_status")), None, "", None, "",
         "参考程度", None),
        ("体調(7日平均)", round(cond7) if cond7 is not None else "", None,
         snum(cond7 - cond7p) if None not in (cond7, cond7p) else "", P0,
         spark(wcond, ymin=40, ymax=100), "90〜好調/66〜良好/50〜要観察", "cond"),
        ("HRV(7日平均)", round(hrv7) if hrv7 is not None else "", None,
         snum(hrv7 - hrv7p) if None not in (hrv7, hrv7p) else "", P0,
         spark(whrv), hrv_note(hband, hm, hs), None),
    ]
    if dry:
        # A dry run does not call Garmin, so these cells are empty because they
        # were not fetched -- not because there is no data.
        r = add(["※DRY実行", "VO2max・LT・5k/10k予測・Garmin判定は未取得。"
                 "下の空欄は『データ無し』ではなく『この実行では取りに行っていない』"],
                "z2")
        cellfmt.append((r - 1, 1, {"foregroundColor": AMBER, "italic": True}))
    for label, val, bpat, dlt, dpat, sp, note_txt, mark in z2:
        r = add([label, val, dlt, sp, "", "", note_txt], "z2")
        if bpat:
            numfmt.append((r - 1, 1, bpat))
        if dpat and dlt != "":
            numfmt.append((r - 1, 2, dpat))
        if mark == "out":
            cellfmt.append((r - 1, 1, {"foregroundColor": NAVY, "bold": True}))
        if mark == "cond" and cond7 is not None:
            if cond7 < 60:
                cellfmt.append((r - 1, 1, {"foregroundColor": RED, "bold": True}))
            elif cond7 < 80:
                cellfmt.append((r - 1, 1, {"foregroundColor": AMBER, "bold": True}))
            elif cond7 < 90:
                cellfmt.append((r - 1, 1, {"bold": True}))

    # -- zone 3: weekly ----------------------------------------------------- #
    add(["③ 週次推移 — 直近12週(新しい順)"], "band")
    add(["週", "km", "負荷", "CTL", "TSB", "体調", "P練", "試合・イベント"], "sub")
    for w in reversed(weeks[-12:]):
        lbl = f"{w['w0'].month}/{w['w0'].day}〜" + ("(今週)" if w["cur"] else "")
        r = add([lbl, w["km"], w["load"], w["ctl"] if w["ctl"] is not None else "",
                 w["tsb"] if w["tsb"] is not None else "",
                 w["cond"] if w["cond"] is not None else "", w["pts"],
                 w["races"]], "z3cur" if w["cur"] else "z3")
        if w["tsb"] is not None:
            numfmt.append((r - 1, 4, P0))
        if w["cond"] is not None and w["cond"] < 80:
            cellfmt.append((r - 1, 5, {"foregroundColor":
                                       RED if w["cond"] < 60 else AMBER,
                                       "bold": True}))

    # -- zone 4: macro ------------------------------------------------------ #
    add([f"④ マクロ — {today.year}年 月次"], "band")
    add(["月", "km", "負荷", "月末CTL", "VO2max", "", "レース"], "sub")
    mvo2 = extras[6] or {}
    for mo in months:
        add([f"{mo['m']}月", mo["km"], mo["load"],
             mo["ctl"] if mo["ctl"] is not None else "",
             mvo2.get(mo["m"], ""), "", mo["races"]], "z4")
    add([f"現在CTL {ctl:.0f} = 年内レンジ {lo:.0f}〜{hi:.0f} の {pct*100:.0f}%点"
         f"({pct_from} 以降)"], "pct")

    # -- footnotes ---------------------------------------------------------- #
    add([], "gap")
    for t in ("5k/10k予測: 絶対値は本人実測と乖離しうるため参考外。Δ(本人比の変化)のみ有効。",
              f"CTL=体力(負荷{ctl_days}日指数平均) / ATL=疲労({atl_days}日) / "
              f"TSB=CTL−ATL=フォーム。閾値は個人較正中の推論。",
              f"レース窓の条件(推論): CTLが年内{float(st['ctl_percentile_open'])*100:.0f}"
              f"%点以上 かつ TSB>0。週の負荷めやすは上の目標帯(CTL×7)。"):
        add([t], "foot")
    return rows, kind, cellfmt, numfmt


# --------------------------------------------------------------------------- #
# formatting                                                                   #
# --------------------------------------------------------------------------- #
def gr(sid, r1, r2, c1, c2):
    return {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}


def repeat(sid, r1, r2, c1, c2, fmt):
    fields = "userEnteredFormat(" + ",".join(sorted(fmt)) + ")"
    return {"repeatCell": {"range": gr(sid, r1, r2, c1, c2),
                           "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def note(sid, r, c, text):
    return {"updateCells": {"start": {"sheetId": sid, "rowIndex": r,
                                      "columnIndex": c},
                            "rows": [{"values": [{"note": text}]}],
                            "fields": "note"}}


def fmt_requests(sid, kind, cellfmt, numfmt, ctl_days, atl_days, season_start):
    n = len(kind)
    q = [{"updateSheetProperties": {"properties": {
        "sheetId": sid, "tabColor": B4,
        "gridProperties": {"frozenRowCount": 1, "hideGridlines": True}},
        "fields": "tabColor,gridProperties(frozenRowCount,hideGridlines)"}}]
    q.append(repeat(sid, 0, n, 0, NCOL, {"wrapStrategy": "CLIP",
                                         "textFormat": {"fontSize": 10}}))
    for i, k in enumerate(kind):
        if k == "title":
            q.append({"mergeCells": {"range": gr(sid, i, i + 1, 0, NCOL),
                                     "mergeType": "MERGE_ALL"}})
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "backgroundColor": B4, "verticalAlignment": "MIDDLE",
                "textFormat": {"foregroundColor": WHITE, "bold": True,
                               "fontSize": 12}}))
        elif k == "band":
            q.append({"mergeCells": {"range": gr(sid, i, i + 1, 0, NCOL),
                                     "mergeType": "MERGE_ALL"}})
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "backgroundColor": B4, "verticalAlignment": "MIDDLE",
                "textFormat": {"foregroundColor": WHITE, "bold": True,
                               "fontSize": 10}}))
        elif k == "sub":
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "backgroundColor": B1,
                "textFormat": {"foregroundColor": NAVY, "bold": True,
                               "fontSize": 10}}))
        elif k in ("z1", "pct"):
            q.append(repeat(sid, i, i + 1, 0, 1, {
                "textFormat": {"foregroundColor": NAVY, "bold": True,
                               "fontSize": 10}}))
        elif k == "z3cur":
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "backgroundColor": B1, "textFormat": {"fontSize": 10}}))
        elif k == "foot":
            q.append(repeat(sid, i, i + 1, 0, NCOL, {
                "textFormat": {"foregroundColor": NAVY, "italic": True,
                               "fontSize": 10}}))
        if k == "z2":
            q.append({"mergeCells": {"range": gr(sid, i, i + 1, 3, 6),
                                     "mergeType": "MERGE_ALL"}})
    z2r = [i for i, k in enumerate(kind) if k == "z2"]
    z3r = [i for i, k in enumerate(kind) if k in ("z3", "z3cur")]
    z4r = [i for i, k in enumerate(kind) if k == "z4"]
    for rr in (z2r, z3r, z4r):
        if rr:
            q.append(repeat(sid, rr[0], rr[-1] + 1, 0, 1, {
                "textFormat": {"foregroundColor": NAVY, "fontSize": 10}}))
    if z2r:
        q.append(repeat(sid, z2r[0], z2r[-1] + 1, 1, 3,
                        {"horizontalAlignment": "RIGHT"}))
    if z3r:
        q.append(repeat(sid, z3r[0], z3r[-1] + 1, 1, 2,
                        {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}))
        q.append({"updateBorders": {"range": gr(sid, z3r[0], z3r[-1] + 1, 0, NCOL),
                                    "innerHorizontal": {"style": "SOLID",
                                                        "color": BORDER}}})
    if z4r:
        q.append(repeat(sid, z4r[0], z4r[-1] + 1, 1, 2,
                        {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}))
        q.append(repeat(sid, z4r[0], z4r[-1] + 1, 4, 5,
                        {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}))
        q.append({"updateBorders": {"range": gr(sid, z4r[0], z4r[-1] + 1, 0, NCOL),
                                    "innerHorizontal": {"style": "SOLID",
                                                        "color": BORDER}}})
    for r0, c0, tf in cellfmt:
        q.append(repeat(sid, r0, r0 + 1, c0, c0 + 1, {"textFormat": tf}))
    for r0, c0, pat in numfmt:
        q.append(repeat(sid, r0, r0 + 1, c0, c0 + 1,
                        {"numberFormat": {"type": "NUMBER", "pattern": pat}}))
    lbl_note = {
        "CTL(体力)": f"Chronic Training Load: 日次負荷の{ctl_days}日指数平均。"
                     f"{season_start} を 0 で開始しているため、最初の数週はやや過小(推論)。",
        "TSB(フォーム)": "Training Stress Balance = CTL−ATL。正=体力が疲労を上回る"
                        "(レース向き)。絶対閾値は個人較正中(推論)。",
        "5k予測": "Garmin推定。選手によっては絶対値が実測と乖離するので参考外。"
                  "4週前との差(Δ)だけを傾向として読む。",
    }
    q.append(note(sid, 0, 0, "毎晩の自動更新で再生成。手書き列はこのタブには無い。"))
    widths = [118, 78, 78, 78, 78, 78, 130, 300]
    for i, w in enumerate(widths):
        q.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"}})
    q.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0,
                  "endIndex": 1},
        "properties": {"pixelSize": 34}, "fields": "pixelSize"}})
    return q, lbl_note


# --------------------------------------------------------------------------- #
def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m training_log.status_panel",
        description="Rebuild the Status dashboard tab from the Log tab.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned grid; touch neither Garmin nor the sheet")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    dry = args.dry_run
    today = dt.date.today()
    athlete = config.athlete()
    ctl_days, atl_days, season_start, _ = pmc.params(athlete)

    sh = config.spreadsheet()
    days = read_log(sh)
    bd, unknown = pmc.series(days, today, ctl_days, atl_days, start=season_start)

    panel, extras_vo2 = {}, {}
    p5 = p5d = p10 = p10d = vo2 = vo2d = hband = None
    if not dry:
        g = garmin_fetch._try(config.garmin_client)
        if g:
            for i in range(0, 8):
                d = (today - dt.timedelta(days=i)).isoformat()
                cand = garmin_fetch._try(garmin_fetch.fetch_status_panel, g, d) or {}
                if cand.get("vo2max") or cand.get("train_status"):
                    panel = cand
                    break
            p5, p10 = panel.get("race_5k"), panel.get("race_10k")
            hist = race_pred_hist(g, today - dt.timedelta(days=32), today)
            if hist:
                base = min(hist, key=lambda x: abs(
                    (dt.date.fromisoformat(x["calendarDate"])
                     - (today - dt.timedelta(days=28))).days))
                if p5 and base.get("time5K"):
                    p5d = p5 - base["time5K"]
                if p10 and base.get("time10K"):
                    p10d = p10 - base["time10K"]
            vo2 = panel.get("vo2max") or vo2_at(g, today.isoformat())
            v4 = vo2_at(g, (today - dt.timedelta(days=28)).isoformat())
            if vo2 and v4:
                vo2d = round(vo2 - v4, 1)
            for m in range(1, today.month + 1):
                last = ((dt.date(today.year, m + 1, 1) - dt.timedelta(days=1))
                        if m < 12 else dt.date(today.year, 12, 31))
                extras_vo2[m] = vo2_at(g, min(last, today).isoformat()) or ""
            if not extras_vo2.get(today.month) and vo2:
                extras_vo2[today.month] = vo2
            hband = hrv_band(g, today)

    rows, kind, cellfmt, numfmt = build(
        days, bd, today, panel,
        (p5, p5d, p10, p10d, vo2, vo2d, extras_vo2, hband),
        athlete, unknown, dry=dry)
    grid = [[("" if c is None else c) for c in r] for r in rows]
    for r in grid:
        print(" | ".join(str(c)[:30] for c in r))
    if dry:
        print(f"\n[dry run] {len(grid)} rows planned for '{TAB}'.")
        return 0

    old_idx = None
    try:
        old = sh.worksheet(TAB)
        old_idx = old._properties.get("index")
        sh.del_worksheet(old)
    except Exception:
        pass
    ws = sh.add_worksheet(title=TAB, rows=len(grid) + 5, cols=NCOL)
    sid = ws.id
    ws.update(values=grid, range_name=f"A1:H{len(grid)}",
              value_input_option="USER_ENTERED")
    q, lbl_note = fmt_requests(sid, kind, cellfmt, numfmt, ctl_days, atl_days,
                               season_start)
    for i, k in enumerate(kind):
        if k == "z2" and str(grid[i][0]) in lbl_note:
            q.append(note(sid, i, 0, lbl_note[str(grid[i][0])]))
    if old_idx is not None:
        q.append({"updateSheetProperties": {
            "properties": {"sheetId": sid, "index": old_idx}, "fields": "index"}})
    sh.batch_update({"requests": q})
    print(f"\nWrote '{TAB}': {len(grid)} rows, formatting applied (sheetId {sid}).")
    return 0


if __name__ == "__main__":
    sys.exit(config.run_cli(main))
