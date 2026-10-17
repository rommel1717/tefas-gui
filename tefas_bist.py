# -*- coding: utf-8 -*-
"""
BIST Değerleme sayfası (vbist_degerleme_v193.py entegrasyonu)
=============================================================
- Değerleme motoru AYRI bir süreçte çalışır (global ayarlar arayüzü kirletmez, arayüz donmaz).
- Canlı günlük + ilerleme çubuğu, durdurma düğmesi.
- Üretilen Excel raporları `bist_raporlar/` klasöründe saklanır; geçmiş raporları açabilirsiniz.
- Sonuç sayfaları (Ana Sıralama, Aday Listesi, Portföy, Due Diligence, …) filtrelenebilir tablolar olarak gösterilir.
- Aday hisseleri tek tıkla "Hisse & Karma Portföy" sayfasına gönderebilirsiniz.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

import tefas_bist_filtre as BF

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "vbist_degerleme_v193.py"
OUT_DIR = ROOT / "bist_raporlar"
HIST_DIR = ROOT / "bist_history"

# Çalıştırma parametreleri: (anahtar, CLI bayrağı, etiket, adım, yardım)
PARAMS = [
    ("ke", "--ke", "Özkaynak maliyeti (ke)", 0.01, "Örn. 0.24 — boşsa model kendisi hesaplar (rf + ERP)."),
    ("rf", "--rf", "Risksiz faiz (TR 10Y)", 0.01, "Örn. 0.28"),
    ("erp", "--erp", "Hisse risk primi (ERP)", 0.005, "Örn. 0.06"),
    ("infl_now", "--infl-now", "Güncel enflasyon beklentisi", 0.01, "Örn. 0.30"),
    ("infl_lt", "--infl-lt", "Uzun vade enflasyon (terminal)", 0.01, "Örn. 0.15"),
    ("real_g", "--real-g", "Terminal reel büyüme", 0.005, "Örn. 0.02"),
    ("min_liq", "--min-liq", "Min. günlük hacim (milyon TL)", 1.0, "Likidite alt sınırı (varsayılan 10)."),
    ("real_ke", "--real-ke", "Reel özkaynak maliyeti tabanı", 0.01, "Varsayılan 0.12"),
]

QUICK_COLS = [
    "Hisse", "Sektör", "Final Score", "Adjusted Score", "Güven Skoru", "Risk Skoru", "Risk Sınıfı",
    "Değerleme Sınıfı", "Yatırım Kararı", "Adil Değer Marjı", "Kova", "Nihai Sinyal",
]


# ── Çalıştırma yönetimi ─────────────────────────────────────────────────────
def build_command(values: dict, output: Path, equity_basis: str, mos: str, no_backtest: bool, walk_forward: bool) -> List[str]:
    cmd = [sys.executable, "-u", str(SCRIPT), "--output", str(output), "--history-dir", str(HIST_DIR)]
    for key, flag, *_ in PARAMS:
        v = values.get(key)
        if v is not None:
            cmd += [flag, str(v)]
    if equity_basis in ("real", "nominal"):
        cmd += ["--equity-basis", equity_basis]
    if mos and mos.strip():
        cmd += ["--mos", mos.strip()]
    if no_backtest:
        cmd.append("--no-backtest")
    if walk_forward:
        cmd.append("--walk-forward")
    return cmd


def start_run(cmd: List[str], output: Path) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", BIST_OUTPUT=str(output))
    env.pop("DISPLAY", None) if sys.platform != "win32" else None
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", cwd=str(ROOT), env=env, bufsize=1,
    )
    run = {"proc": proc, "lines": [], "output": str(output), "t0": time.time(), "cmd": cmd, "rc": None}

    def _reader():
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                run["lines"].append(line.rstrip("\n"))
        finally:
            run["rc"] = proc.wait()

    threading.Thread(target=_reader, daemon=True).start()
    return run


def progress_of(lines: List[str]) -> tuple[int, int]:
    """Son '[12/480]' kalıbından (yapılan, toplam)."""
    for ln in reversed(lines[-400:]):
        m = re.match(r"\s*\[(\d+)/(\d+)\]", ln)
        if m:
            return int(m.group(1)), int(m.group(2))
    return 0, 0


def list_reports() -> List[Path]:
    if not OUT_DIR.exists():
        return []
    fs = [p for p in OUT_DIR.glob("*.xlsx") if "__olusturuluyor__" not in p.name]
    return sorted(fs, key=lambda p: p.stat().st_mtime, reverse=True)


def read_sheet(path: str, sheet: str, mtime: float) -> pd.DataFrame:  # mtime cache anahtarı
    return pd.read_excel(path, sheet_name=sheet)


# ── Sayfa ───────────────────────────────────────────────────────────────────
def render(*, show_df=None, download_excel_button=None):
    import streamlit as st

    S = st.session_state

    cached_read = st.cache_data(show_spinner=False)(read_sheet)

    @st.cache_data(show_spinner=False)
    def _sheet_names(path: str, mtime: float) -> List[str]:
        return pd.ExcelFile(path).sheet_names

    st.header("🏦 BIST Değerleme")
    st.caption(
        "Graham + Buffett ∩ Teknik AL · EPV · Residual Income · Due Diligence · Shannon/Kelly. "
        "Motor ayrı süreçte çalışır; tüm BIST taraması uzun sürebilir (rapor Excel olarak saklanır). "
        "Yatırım tavsiyesi değildir."
    )

    if not SCRIPT.exists():
        st.error(f"`{SCRIPT.name}` bulunamadı. Dosyayı `tefas_gui.py` ile aynı klasöre koyun.")
        return

    tab_run, tab_res, tab_det, tab_sek = st.tabs([
        "🚀 Çalıştır", "📊 Rapor görüntüleyici", "🔎 Hisse Detay", "🏷️ Sektör eşle"
    ])

    # ═══ Çalıştır ═══════════════════════════════════════════════════════════
    with tab_run:
        run = S.get("bist_run")
        aktif = bool(run) and run["rc"] is None

        with st.expander("Model parametreleri (boş = motorun varsayılanı)", expanded=not aktif):
            cols = st.columns(4)
            vals: Dict[str, Optional[float]] = {}
            for i, (key, _flag, label, step, hlp) in enumerate(PARAMS):
                vals[key] = cols[i % 4].number_input(label, value=None, step=step, format="%g",
                                                     placeholder="varsayılan", help=hlp, key=f"bist_p_{key}")
            c1, c2, c3, c4 = st.columns(4)
            eq = c1.selectbox("İskonto tabanı", ["varsayılan", "real", "nominal"], key="bist_eq")
            mos = c2.text_input("Güvenlik marjı A,B,C", placeholder="0.30,0.20,0.15", key="bist_mos",
                                help="A ≥ B ≥ C olmalı.")
            nobt = c3.checkbox("Backtest kapalı (hızlı)", key="bist_nobt")
            wf = c4.checkbox("Walk-forward paneli", key="bist_wf")

        b1, b2, _ = st.columns([1, 1, 3])
        if b1.button("▶️ Değerlemeyi başlat", type="primary", disabled=aktif, key="bist_start"):
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            out = OUT_DIR / f"BIST_Degerleme_{datetime.now():%Y%m%d_%H%M}.xlsx"
            cmd = build_command({k: v for k, v in vals.items()}, out, "" if eq == "varsayılan" else eq,
                                mos, nobt, wf)
            S["bist_run"] = start_run(cmd, out)
            st.rerun()
        if b2.button("⏹️ Durdur", disabled=not aktif, key="bist_stop"):
            try:
                run["proc"].terminate()
            except Exception:  # noqa: BLE001
                pass

        def _canli():
            r = S.get("bist_run")
            if not r:
                st.info("Henüz çalıştırma yok. Parametreleri gözden geçirip başlatın; "
                        "veya 'Rapor görüntüleyici' sekmesinden eski bir raporu açın.")
                return
            done, total = progress_of(r["lines"])
            gecen = int(time.time() - r["t0"])
            if r["rc"] is None:
                st.progress(done / total if total else 0.0,
                            text=f"Çalışıyor · {done}/{total or '?'} hisse · {gecen // 60} dk {gecen % 60} sn")
            elif r["rc"] == 0:
                st.success(f"Tamamlandı ({gecen // 60} dk {gecen % 60} sn) → {Path(r['output']).name}")
            else:
                st.error(f"Süreç hata ile bitti (kod {r['rc']}). Günlüğün sonuna bakın.")
            st.code("\n".join(r["lines"][-25:]) or "(henüz çıktı yok)", language="text")
            if r["rc"] is not None and not S.get("_bist_finished_" + r["output"]):
                S["_bist_finished_" + r["output"]] = True
                if r["rc"] == 0 and Path(r["output"]).exists():
                    S["bist_secili_rapor"] = r["output"]
                st.rerun()

        if hasattr(st, "fragment"):
            st.fragment(run_every=2 if aktif else None)(_canli)()
        else:
            _canli()
            if aktif:
                st.button("🔄 Yenile", key="bist_refresh")
        r = S.get("bist_run")
        if r and r["lines"]:
            with st.expander("Tüm günlük"):
                st.code("\n".join(r["lines"]), language="text")

    # ═══ Rapor görüntüleyici ════════════════════════════════════════════════
    with tab_res:
        raporlar = list_reports()
        up = st.file_uploader("Veya bir BIST Değerleme Excel raporu yükle", type=["xlsx"], key="bist_upload")
        if up is not None and S.get("_bist_up_name") != up.name:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            dest = OUT_DIR / f"yuklenen_{up.name}"
            dest.write_bytes(up.getvalue())
            S["_bist_up_name"] = up.name
            S["bist_secili_rapor"] = str(dest)
            st.rerun()
        raporlar = list_reports()
        if not raporlar:
            st.info("Henüz rapor yok. 'Çalıştır' sekmesinden bir değerleme başlatın ya da Excel raporu yükleyin.")
            return
        yollar = [str(p) for p in raporlar]
        if S.get("bist_secili_rapor") not in yollar:
            S["bist_secili_rapor"] = yollar[0]
        path = st.selectbox(
            "Rapor", yollar, key="bist_secili_rapor",
            format_func=lambda p: f"{Path(p).name}  ·  {datetime.fromtimestamp(Path(p).stat().st_mtime):%d.%m.%Y %H:%M}",
        )
        mt = Path(path).stat().st_mtime
        try:
            sheets = _sheet_names(path, mt)
        except Exception as e:  # noqa: BLE001
            st.error(f"Rapor okunamadı: {e}")
            return

        def _get(sheet: str) -> pd.DataFrame:
            try:
                return cached_read(path, sheet, mt)
            except Exception:  # noqa: BLE001
                return pd.DataFrame()

        t1, t5, t2, t3, t4 = st.tabs(["🏆 Sıralama", "🧪 Filtre Stüdyosu", "🎯 Aday & Portföy", "🗂️ Tüm sayfalar", "📥 İndir"])

        # Sıralama
        with t1:
            ana = "Ana Sıralama" if "Ana Sıralama" in sheets else ("Değerleme Özeti" if "Değerleme Özeti" in sheets else None)
            if not ana:
                st.info("Sıralama sayfası bulunamadı.")
            else:
                df = _get(ana)
                if df.empty:
                    st.info("Sayfa boş.")
                else:
                    f1, f2, f3 = st.columns([2, 2, 2])
                    ara = f1.text_input("Hisse ara", placeholder="THYAO, EREGL", key="bist_ara")
                    sek = f2.multiselect("Sektör", sorted(df["Sektör"].dropna().astype(str).unique())
                                         if "Sektör" in df.columns else [], key="bist_sek")
                    kov = f3.multiselect("Kova", sorted(df["Kova"].dropna().astype(str).unique())
                                         if "Kova" in df.columns else [], key="bist_kova")
                    g1, g2, g3 = st.columns(3)
                    ekstra = {}
                    for (col_adi, anahtar, ad), gc in zip((("Yatırım Kararı", "bist_karar", "Yatırım kararı"),
                                                           ("Risk Sınıfı", "bist_risk", "Risk sınıfı"),
                                                           ("Değerleme Sınıfı", "bist_deg", "Değerleme sınıfı")),
                                                          (g1, g2, g3)):
                        if col_adi in df.columns:
                            ekstra[col_adi] = gc.multiselect(ad, sorted(df[col_adi].dropna().astype(str).unique()), key=anahtar)
                    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
                    h1, h2, h3 = st.columns([2, 1, 1])
                    min_c = h1.selectbox("Sayısal alt sınır uygula", ["—"] + num_cols, key="bist_numc")
                    min_v = h2.number_input("≥", value=None, placeholder="min", key="bist_numv", format="%g")
                    max_v = h3.number_input("≤", value=None, placeholder="maks", key="bist_numx", format="%g")

                    w = df.copy()
                    if ara.strip():
                        terms = [t.strip().upper() for t in ara.replace(";", ",").split(",") if t.strip()]
                        w = w[w["Hisse"].astype(str).str.upper().apply(lambda x: any(t in x for t in terms))]
                    if sek:
                        w = w[w["Sektör"].astype(str).isin(sek)]
                    if kov:
                        w = w[w["Kova"].astype(str).isin(kov)]
                    for col_adi, secili in ekstra.items():
                        if secili:
                            w = w[w[col_adi].astype(str).isin(secili)]
                    if min_c != "—":
                        s = pd.to_numeric(w[min_c], errors="coerce")
                        if min_v is not None:
                            w = w[s >= min_v]
                            s = pd.to_numeric(w[min_c], errors="coerce")
                        if max_v is not None:
                            w = w[s <= max_v]

                    sirala_opts = [c for c in (["Final Score", "Adjusted Score", "Adil Değer Marjı", "Güven Skoru"] + num_cols)
                                   if c in w.columns]
                    sirala_opts = list(dict.fromkeys(sirala_opts))
                    s1, s2 = st.columns([3, 1])
                    sc = s1.selectbox("Sırala", sirala_opts or ["—"], key="bist_sort")
                    asc = s2.checkbox("Artan", value=False, key="bist_asc")
                    if sc in w.columns:
                        w = w.sort_values(sc, ascending=asc, na_position="last")
                    with st.expander("Sütunlar"):
                        gor = st.multiselect("Gösterilecek", list(df.columns),
                                             default=[c for c in QUICK_COLS if c in df.columns] or list(df.columns)[:12],
                                             key="bist_cols", label_visibility="collapsed")
                    st.caption(f"{len(w)} / {len(df)} hisse")
                    st.dataframe(w[gor] if gor else w, hide_index=True, use_container_width=True, height=480)
                    kodlar = w["Hisse"].astype(str).tolist() if "Hisse" in w.columns else []
                    sec = st.multiselect("Karma portföy analizine gönderilecek hisseler", kodlar,
                                         default=kodlar[:5], key="bist_pick_rank")
                    st.button("📈 'Hisse & Karma Portföy' sayfasına gönder", key="bist_send_rank",
                              disabled=not sec, on_click=_send_to_hisse, args=(sec,))
                    if S.get("_bist_sent"):
                        st.success(f"{S['_bist_sent']} hisse gönderildi — soldaki menüden '📈 Hisse & Karma Portföy' sayfasını açın.")

        # Filtre Stüdyosu
        with t5:
            evrenler = [x for x in ("Ana Sıralama", "Değerleme Özeti") if x in sheets]
            if not evrenler:
                st.info("Bu raporda 'Ana Sıralama' veya 'Değerleme Özeti' sayfası yok.")
            else:
                ev = st.radio("Evren", evrenler, horizontal=True, key="bist_flt_evren",
                              help="Ana Sıralama: finansal olmayan şirketler. Değerleme Özeti: tüm taranan hisseler.")
                dfe = _get(ev)
                if dfe.empty:
                    st.info("Sayfa boş.")
                else:
                    BF.render_filter(dfe, send_cb=_send_to_hisse, key=f"bf{evrenler.index(ev)}")

        # Aday & Portföy
        with t2:
            cand = _get("Aday Listesi") if "Aday Listesi" in sheets else pd.DataFrame()
            if cand.empty or "Hisse" not in cand.columns:
                st.info("Bu raporda aday listesi yok ya da boş (filtreden geçen aday çıkmamış olabilir).")
            else:
                st.subheader(f"Aday Listesi · {len(cand)} hisse")
                gor = [c for c in ["Aday Sıra", "Hisse", "Sektör", "Nihai Sinyal", "Uyum Kadranı", "Final Score",
                                   "Aday Skoru", "DD Aksiyon", "DD Özet", "Fiyat Sinyali Özeti", "Önerilen Pay %",
                                   "Rejim Ayarlı Pay %", "Stop Loss %"] if c in cand.columns]
                st.dataframe(cand[gor] if gor else cand, hide_index=True, use_container_width=True)
                kodlar = cand["Hisse"].astype(str).tolist()
                st.button(f"📈 {len(kodlar)} adayı 'Hisse & Karma Portföy' sayfasına gönder", key="bist_send_cand",
                          on_click=_send_to_hisse, args=(kodlar,))
                if S.get("_bist_sent"):
                    st.success(f"{S['_bist_sent']} hisse gönderildi — '📈 Hisse & Karma Portföy' sayfasını açın.")
                with st.expander("Tüm sütunlar"):
                    st.dataframe(cand, hide_index=True, use_container_width=True)
            for sh in ("Portföy", "Portföy Özet", "Değer Yatırımı", "Konsensüs"):
                if sh in sheets:
                    dd = _get(sh)
                    if not dd.empty:
                        with st.expander(sh, expanded=(sh == "Portföy")):
                            st.dataframe(dd, hide_index=True, use_container_width=True)

        # Tüm sayfalar
        with t3:
            sh = st.selectbox("Sayfa", sheets, key="bist_sheet")
            dd = _get(sh)
            if dd.empty:
                st.info("Sayfa boş ya da okunamadı.")
            else:
                st.caption(f"{len(dd)} satır × {len(dd.columns)} sütun")
                st.dataframe(dd, hide_index=True, use_container_width=True, height=520)

        # İndir
        with t4:
            st.write(f"**{Path(path).name}** · {Path(path).stat().st_size / 1e6:.1f} MB")
            st.download_button("⬇️ Excel raporunu indir", Path(path).read_bytes(), file_name=Path(path).name,
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               key="bist_dl")
            st.caption(f"Rapor klasörü: `{OUT_DIR}`  ·  Skor arşivi: `{HIST_DIR}` "
                       "(track record birikmesi için aynı klasör kullanılır).")
            tv = Path(path).with_name(Path(path).stem + "_TV_Watchlist_kodlar.txt")
            if tv.exists():
                st.download_button("⬇️ TradingView kod listesi", tv.read_bytes(), file_name=tv.name, key="bist_dl_tv")
        S.pop("_bist_sent", None)

    # ═══ Hisse Detay — tüm sütunlar + ad/kod arama ══════════════════════════
    with tab_det:
        st.subheader("🔎 Hisse detay analizi")
        st.caption(
            "Değerleme raporundan hisse **kodu veya adı** ile arayın; tüm metrikler, skorlar, "
            "değerleme, risk, teknik ve due diligence alanları tek sayfada gösterilir."
        )
        reports = list_reports()
        if not reports:
            st.info("Önce bir değerleme raporu üretin veya `bist_raporlar/` klasörüne Excel koyun.")
        else:
            rpath = st.selectbox(
                "Rapor",
                reports,
                format_func=lambda p: f"{p.name}  ({datetime.fromtimestamp(p.stat().st_mtime):%d.%m.%Y %H:%M})",
                key="bist_det_rapor",
            )
            mt = rpath.stat().st_mtime
            sheets = _sheet_names(str(rpath), mt)
            src_sheet = "Ana Sıralama" if "Ana Sıralama" in sheets else (
                "Değerleme Özeti" if "Değerleme Özeti" in sheets else sheets[0]
            )
            # Tercih: Değerleme Özeti (daha geniş) varsa kullan
            if "Değerleme Özeti" in sheets:
                src_sheet = st.selectbox(
                    "Kaynak sayfa",
                    [s for s in ("Değerleme Özeti", "Ana Sıralama", "Aday Listesi") if s in sheets]
                    or sheets[:5],
                    key="bist_det_sheet",
                )
            df_all = cached_read(str(rpath), src_sheet, mt)
            if df_all is None or df_all.empty:
                st.warning("Sayfa boş.")
            else:
                # Arama: kod veya unvan/ad
                ad_col = next((c for c in df_all.columns if str(c).lower() in (
                    "unvan", "ad", "şirket", "sirket", "name", "hisse adı", "hisse adi"
                )), None)
                kod_col = "Hisse" if "Hisse" in df_all.columns else df_all.columns[0]
                q = st.text_input(
                    "Hisse kodu veya adı",
                    placeholder="THYAO  veya  Türk Hava  veya  GARAN",
                    key="bist_det_q",
                )
                # Liste seçimi
                opts = []
                for _, row in df_all.iterrows():
                    k = str(row.get(kod_col) or "").strip().upper()
                    a = str(row.get(ad_col) or "").strip() if ad_col else ""
                    label = f"{k} — {a}" if a and a.lower() not in ("nan", "none") else k
                    if k:
                        opts.append((k, label, row))
                if q and q.strip():
                    ql = q.strip().lower()
                    opts_f = [o for o in opts if ql in o[0].lower() or ql in o[1].lower()]
                else:
                    opts_f = opts
                if not opts_f:
                    st.warning("Eşleşen hisse yok.")
                else:
                    labels = [o[1] for o in opts_f[:400]]
                    sec = st.selectbox("Hisse seç", labels, key="bist_det_sec")
                    row = next(o[2] for o in opts_f if o[1] == sec)
                    kod = str(row.get(kod_col) or "").upper()
                    st.markdown(f"### {kod}" + (f" · {row.get(ad_col)}" if ad_col and pd.notna(row.get(ad_col)) else ""))
                    # KPI üst şerit
                    def _g(*names):
                        for n in names:
                            if n in row.index and pd.notna(row.get(n)):
                                return row.get(n)
                        return None
                    k1, k2, k3, k4, k5, k6 = st.columns(6)
                    k1.metric("Sektör", str(_g("Sektör") or "—")[:22])
                    k2.metric("Final Score", f"{float(_g('Final Score') or 0):.1f}" if _g("Final Score") is not None else "—")
                    k3.metric("Adil Marj %", f"{float(_g('Adil Değer Marjı') or 0):.1f}" if _g("Adil Değer Marjı") is not None else "—")
                    k4.metric("F/K", f"{float(_g('F/K') or 0):.1f}" if _g("F/K") is not None else "—")
                    k5.metric("PD/DD", f"{float(_g('PD/DD') or 0):.2f}" if _g("PD/DD") is not None else "—")
                    k6.metric("Risk", str(_g("Risk Sınıfı") or "—")[:18])
                    # Gruplu tüm alanlar
                    groups = {
                        "Kimlik / Sınıf": ["Hisse", "Sektör", "Finansal Grup", "Not", "Yatırım Kararı", "Değerleme Sınıfı",
                                           "Risk Sınıfı", "Kova", "Nihai Sinyal", "Uyum Kadranı", "Ana Sıralamada"],
                        "Fiyat / Çarpan": ["Fiyat", "Piyasa Değeri", "F/K", "PD/DD", "F/K (Forward)", "PD/DD (Forward)",
                                          "Δ F/K (Fwd−TTM)", "Δ PD/DD (Fwd−TTM)", "EV/EBIT", "EV/FAVÖK",
                                          "Kazanç Getirisi", "FCF Getirisi", "Owner Earnings Getirisi"],
                        "Değerleme": ["Adil Değer", "Adil Değer Marjı", "EPV", "Residual Income", "Graham Sayı",
                                      "Final Score", "Adjusted Score", "Güven Skoru", "Risk Skoru", "Araştırma Skoru"],
                        "Kalite / Bilanço": ["ROIC", "ROE", "Piotroski F", "Altman Z", "Altman Bölge",
                                             "Net Borç/Özkaynak", "Net Borç/FCF", "Cari Oran", "CCC"],
                        "Getiri / Risk": ["Getiri 6A", "Getiri 12A", "Vol Ann (σ)", "Max DD", "Sharpe",
                                          "Backtest 6A İleri Getiri", "Backtest 12A İleri Getiri"],
                        "Teknik": ["Teknik Skor", "RSI(14)", "ADX(14)", "MACD", "Supertrend", "Teknik Sinyal"],
                        "Portföy / Kelly": ["Önerilen Pay %", "Rejim Ayarlı Pay %", "Stop Loss %", "Kelly %"],
                        "Due Diligence": ["DD Aksiyon", "DD Özet", "Aday Skoru", "Fiyat Sinyali Özeti"],
                    }
                    # Gerçekten var olan sütunları göster
                    for gname, cols in groups.items():
                        present = [c for c in cols if c in row.index]
                        if not present:
                            continue
                        with st.expander(gname, expanded=(gname in ("Kimlik / Sınıf", "Fiyat / Çarpan", "Değerleme"))):
                            data = []
                            for c in present:
                                v = row.get(c)
                                if pd.isna(v):
                                    v = "—"
                                elif isinstance(v, float):
                                    v = f"{v:.4g}"
                                data.append({"Alan": c, "Değer": v})
                            st.dataframe(pd.DataFrame(data), hide_index=True, use_container_width=True)
                    # Kalan tüm sütunlar
                    used = set()
                    for cols in groups.values():
                        used.update(cols)
                    rest = [c for c in row.index if c not in used]
                    if rest:
                        with st.expander(f"Diğer tüm alanlar ({len(rest)})", expanded=False):
                            data = []
                            for c in rest:
                                v = row.get(c)
                                if pd.isna(v):
                                    v = "—"
                                elif isinstance(v, float):
                                    v = f"{v:.4g}"
                                data.append({"Alan": c, "Değer": v})
                            st.dataframe(pd.DataFrame(data), hide_index=True, use_container_width=True)
                    # Tek satır Excel indir
                    one = pd.DataFrame([row])
                    if download_excel_button:
                        download_excel_button(
                            one,
                            f"hisse_detay_{kod}_{datetime.now():%Y%m%d_%H%M}.xlsx",
                            meta={"hisse": kod, "kaynak": str(rpath.name)},
                            key=f"xl_hisse_det_{kod}",
                        )
                    else:
                        st.download_button(
                            "⬇️ Bu hisseyi Excel indir",
                            one.to_csv(index=False).encode("utf-8-sig"),
                            file_name=f"hisse_detay_{kod}.csv",
                            mime="text/csv",
                            key=f"csv_hisse_det_{kod}",
                        )

    # ═══ Sektör eşlemesini yeniden çalıştır ═════════════════════════════════
    with tab_sek:
        st.subheader("🏷️ Sektör eşlemesini yeniden çalıştır")
        st.caption(
            "Mevcut Excel raporundaki **Diğer / boş** sektörleri statik BIST haritası + "
            "kod kalıbı (GYO) + Yahoo ile günceller. Yeni dosya: `*_sektor_guncel.xlsx`"
        )
        reports = list_reports()
        if not reports:
            st.info("`bist_raporlar/` klasöründe Excel yok. Önce değerleme çalıştırın.")
        else:
            rpath = st.selectbox(
                "Rapor seç",
                reports,
                format_func=lambda p: f"{p.name}  ({datetime.fromtimestamp(p.stat().st_mtime):%d.%m.%Y %H:%M})",
                key="bist_sek_rapor",
            )
            force = st.checkbox("Tüm sektörleri yeniden yaz (yalnız Diğer değil)", value=True, key="bist_sek_force")
            if st.button("▶️ Sektör eşlemesini çalıştır", type="primary", key="bist_sek_run"):
                try:
                    import importlib.util
                    spec = importlib.util.spec_from_file_location("vbist", SCRIPT)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)  # type: ignore
                    with st.spinner("Sektörler güncelleniyor…"):
                        outp = mod.remap_sectors_in_excel(str(rpath), force_all=force)
                    st.success(f"Tamam: `{outp}`")
                    if outp and Path(outp).exists():
                        st.download_button(
                            "⬇️ Güncel Excel indir",
                            Path(outp).read_bytes(),
                            file_name=Path(outp).name,
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            key="bist_sek_dl",
                        )
                except Exception as e:  # noqa: BLE001
                    st.error(f"Sektör eşleme hatası: {e}")


def _send_to_hisse(kodlar: List[str]):
    """Seçilen hisseleri 'Hisse & Karma Portföy' sayfasındaki giriş kutusuna yazar."""
    import streamlit as st
    s = ", ".join(dict.fromkeys(str(k).strip().upper() for k in kodlar if str(k).strip()))
    st.session_state["hisse_liste"] = s
    st.session_state["hisse_input"] = s
    st.session_state["_bist_sent"] = len(kodlar)
