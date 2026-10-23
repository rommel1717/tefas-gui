#!/usr/bin/env python3
"""
TEFAS Fon Analiz — Streamlit Arayüz
====================================
Mevcut tefas_core motorunu kullanır.
Çalıştır: streamlit run tefas_gui.py
"""

from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

# ── Core motor ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import tefas_core as core  # noqa: E402
import tefas_filtre  # noqa: E402
import tefas_ek as TE  # noqa: E402
import tefas_portfoyum  # noqa: E402
import tefas_bist  # noqa: E402
import tefas_panel  # noqa: E402
# ── Sayfa ayarı ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="TEFAS Fon Analiz",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ─────────────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
  .block-container { padding-top: 1rem; padding-bottom: 2.5rem; max-width: 1200px; }
  div[data-testid="stMetricValue"] { font-size: 1.4rem; font-weight: 700; }
  div[data-testid="stMetricLabel"] { font-size: 0.82rem; color: #94a3b8; }
  .stDataFrame { font-size: 0.88rem; }
  h1 { font-size: 1.75rem !important; letter-spacing: -0.02em; }
  h2 { font-size: 1.22rem !important; margin-top: 0.7rem !important; }
  h3 { font-size: 1.05rem !important; }
  [data-testid="stSidebar"] { background: linear-gradient(180deg, #0f172a 0%, #1e293b 100%); }
  [data-testid="stSidebar"] * { color: #e2e8f0 !important; }
  [data-testid="stSidebar"] .stRadio label { font-size: 0.92rem; }
  .hero {
    background: linear-gradient(135deg, #0f2744, #1d4ed8);
    color: white; border-radius: 16px; padding: 22px 26px; margin-bottom: 18px;
  }
  .hero h1 { color: white !important; margin: 0 0 8px; }
  .hero p { margin: 0; opacity: 0.92; font-size: 0.95rem; }
  .flow-step {
    background: #1e293b; border: 1px solid #334155; border-radius: 12px;
    padding: 14px 16px; height: 100%; color: #e2e8f0;
  }
  .flow-step b { color: #60a5fa; }
  .badge {
    display:inline-block; padding:3px 10px; border-radius:999px;
    font-size:0.75rem; font-weight:600; margin-right:4px;
  }
  .badge-ok  { background:#064e3b; color:#6ee7b7; }
  .badge-warn{ background:#78350f; color:#fcd34d; }
  .badge-bad { background:#7f1d1d; color:#fca5a5; }
  /* Karanlık tema: grafik görselleri ve tablo çerçeveleri */
  [data-testid="stImage"] img { border-radius: 10px; }
  [data-testid="stExpander"] { border-color: #334155 !important; }
  .sepet-box {
    background: #1e293b; border: 1px solid #334155; border-radius: 10px;
    padding: 10px 12px; margin-top: 8px;
  }
</style>
""",
    unsafe_allow_html=True,
)

# ── Session state ───────────────────────────────────────────────────────────
def _init_state():
    defaults = {
        "cfg": core.load_config(),
        "fon_tipi": "YAT",
        "analiz": None,
        "son_tarama": None,
        "son_tarama_meta": {},
        "son_siralama": None,
        "son_portfoy": None,
        "son_karsilastir": None,
        "detay_sonuc": None,
        "opt_sonuc": None,
        "akilli_portfoy": None,
        "akilli_portfoy_sira": None,
        "hisse_sonuc": None,
        "karma_sonuc": None,
        "hisse_liste": "THYAO,GARAN,ASELS",
        "portfoy_sepet": [],
        "sepet_analiz": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


def get_analiz() -> core.FonAnaliz:
    a = st.session_state.analiz
    tip = st.session_state.fon_tipi
    cfg = st.session_state.cfg
    if a is None or a.api.fon_tipi != tip:
        st.session_state.analiz = core.FonAnaliz(fon_tipi=tip, cfg=cfg)
    else:
        st.session_state.analiz.cfg = cfg
        st.session_state.analiz.rf = float(cfg.get("risk_free", 0.40))
    return st.session_state.analiz



def sepet_ekle(kodlar, ad_map=None, kaynak="manuel"):
    """Fon kodlarını portföy sepetine ekler (tekrarsız)."""
    ad_map = ad_map or {}
    sepet = list(st.session_state.get("portfoy_sepet") or [])
    mevcut = {str(x.get("kod")).upper() for x in sepet}
    eklendi = 0
    for k in kodlar:
        k = str(k).strip().upper()
        if not k or k in mevcut:
            continue
        sepet.append({"kod": k, "ad": ad_map.get(k, k), "kaynak": kaynak})
        mevcut.add(k)
        eklendi += 1
    st.session_state.portfoy_sepet = sepet
    return eklendi


def sepet_analiz_et(weights: dict | None = None):
    """
    Sepet derin raporu:
    - fon bazlı metrik tablosu
    - eşit veya özel ağırlıklı birleşik portföy
    - korelasyon matrisi, çeşitlendirme skoru
    - kategori / risk dağılımı
    - en iyi / en zayıf fonlar
    - sade dilde genel değerlendirme
    """
    sepet = st.session_state.get("portfoy_sepet") or []
    if not sepet:
        return {"error": "Sepet boş"}
    analiz = get_analiz()
    rows = []
    series = {}
    dagilim_list = []

    for item in sepet:
        kod = item["kod"]
        try:
            d = analiz.fon_detay(kod)
            m = d["metrikler"]
            o, p = d["ozet"], d["profil"]
            ad = o.get("fonUnvan") or p.get("fonUnvan") or item.get("ad") or kod
            item["ad"] = ad
            row = {
                "kod": kod, "ad": (ad or "")[:55], "kaynak": item.get("kaynak"),
                "getiri_1a": m.get("getiri_1a"), "getiri_3a": m.get("getiri_3a"),
                "getiri_6a": m.get("getiri_6a"), "getiri_1y": m.get("getiri_1y"),
                "getiri_3y": m.get("getiri_3y"),
                "reel_1y": m.get("reel_1y"), "vs_mevduat": m.get("vs_mevduat"),
                "vs_bist100": m.get("vs_bist100"),
                "sharpe": m.get("sharpe"), "sortino": m.get("sortino"),
                "calmar": m.get("calmar"), "cagr": m.get("cagr"),
                "volatilite": m.get("volatilite"), "max_drawdown": m.get("max_drawdown"),
                "kazanma_orani": m.get("kazanma_orani"),
                "en_kotu_gun": m.get("en_kotu_gun"), "en_iyi_gun": m.get("en_iyi_gun"),
                "var95": m.get("var95"), "cvar95": m.get("cvar95"),
                "risk_degeri": p.get("riskDegeri"),
                "kategori": o.get("fonKategori") or p.get("fonKategori"),
                "fiyat": m.get("fiyat"),
            }
            # dağılım özeti
            try:
                dag = analiz.api.dagilimlar()
                if dag is not None and not dag.empty:
                    rr = dag[dag["kod"].astype(str).str.upper() == kod]
                    if not rr.empty:
                        oz = analiz.api.dagilim_ozet(rr.iloc[0])
                        for kk, vv in (oz or {}).items():
                            row[f"d_{kk}"] = vv
                        dagilim_list.append((kod, oz or {}))
            except Exception:
                pass
            rows.append(row)
            fd = d.get("fiyat_df")
            if fd is None or len(fd) < 60:
                try:
                    fd = analiz.api.fiyat(kod, periyod=36)
                except Exception:
                    fd = None
            if fd is not None and not fd.empty:
                s = fd.set_index("tarih")["fiyat"].astype(float).sort_index()
                s = s[~s.index.duplicated(keep="last")]
                series[kod] = s
        except Exception as e:
            rows.append({"kod": kod, "ad": item.get("ad"), "hata": str(e), "kaynak": item.get("kaynak")})

    df = pd.DataFrame(rows)
    if "hata" in df.columns:
        ok = df[df["hata"].isna()].copy()
    else:
        ok = df.copy()

    # Ağırlıklar
    kodlar_ok = ok["kod"].astype(str).tolist() if not ok.empty else []
    if weights:
        w_raw = {str(k).upper(): float(v) for k, v in weights.items() if str(k).upper() in kodlar_ok and float(v) > 0}
    else:
        w_raw = {k: 1.0 for k in kodlar_ok}
    tot = sum(w_raw.values()) or 1.0
    w_norm = {k: v / tot for k, v in w_raw.items()}

    # Birleşik portföy serisi
    port_m = None
    corr_df = None
    diversify_score = None
    port_ret = None
    if len(series) >= 1 and w_norm:
        use_cols = [c for c in series if c in w_norm]
        if use_cols:
            dfp = pd.DataFrame({c: series[c] for c in use_cols}).dropna(how="all").ffill().dropna()
            if len(dfp) >= 30:
                rets = dfp.pct_change().dropna()
                if len(use_cols) >= 2 and len(rets) >= 40:
                    try:
                        corr_df = rets[use_cols].corr()
                        # çeşitlendirme: ortalama |korelasyon| ne kadar düşükse o kadar iyi
                        mask = np.triu(np.ones(corr_df.shape), k=1).astype(bool)
                        avg_abs = float(np.abs(corr_df.where(mask)).stack().mean())
                        diversify_score = round(max(0, min(100, (1 - avg_abs) * 100)), 1)
                    except Exception:
                        corr_df = None
                ww = np.array([w_norm[c] for c in use_cols])
                ww = ww / ww.sum()
                port_ret = (rets[use_cols] * ww).sum(axis=1)
                port_px = (1 + port_ret).cumprod() * 100
                fd_port = pd.DataFrame({"tarih": port_px.index, "fiyat": port_px.values})
                port_m = core.full_metrics(fd_port, rf=analiz.rf)
                try:
                    port_m = core.attach_benchmark(port_m, core.fetch_benchmarks(st.session_state.cfg))
                except Exception:
                    pass
                for i, c in enumerate(use_cols):
                    w_norm[c] = float(ww[i])

    # Birleşik varlık dağılımı (ağırlıklı)
    birlesik_dag = {}
    if dagilim_list and w_norm:
        keys = set()
        for _, oz in dagilim_list:
            keys.update(oz.keys())
        for k in keys:
            s = 0.0
            for kod, oz in dagilim_list:
                s += float(oz.get(k) or 0) * float(w_norm.get(kod) or 0)
            if s > 0.05:
                birlesik_dag[k] = round(s, 1)

    # En iyi / en zayıf
    liderler = {}
    if not ok.empty:
        for col, label, asc in [
            ("getiri_1y", "en_yuksek_1y", False),
            ("sharpe", "en_iyi_sharpe", False),
            ("max_drawdown", "en_dusuk_dd", False),  # max_dd is negative, higher is better
            ("volatilite", "en_dusuk_vol", True),
        ]:
            if col in ok.columns and ok[col].notna().any():
                sub = ok.dropna(subset=[col])
                if sub.empty:
                    continue
                r = sub.sort_values(col, ascending=asc).iloc[0]
                liderler[label] = {"kod": r["kod"], "ad": r.get("ad"), "deger": r[col]}

    # Kategori dağılımı
    kat_dag = {}
    if not ok.empty and "kategori" in ok.columns:
        for _, r in ok.iterrows():
            k = str(r.get("kategori") or "Diğer")[:40]
            kat_dag[k] = kat_dag.get(k, 0) + float(w_norm.get(str(r["kod"]), 0)) * 100

    # Ağırlıklı ortalama tehlike + getiri (tüm portföy)
    def _wavg(col):
        if ok.empty or col not in ok.columns:
            return None
        s = pd.to_numeric(ok[col], errors="coerce")
        ww = ok["kod"].astype(str).map(lambda k: float(w_norm.get(k, 0)))
        mask = s.notna() & (ww > 0)
        if not mask.any():
            return None
        return float((s[mask] * ww[mask]).sum() / ww[mask].sum())

    def _simple_avg(col):
        if ok.empty or col not in ok.columns:
            return None
        s = pd.to_numeric(ok[col], errors="coerce")
        return float(s.mean()) if s.notna().any() else None

    ort_tehlike = _wavg("risk_degeri")  # SPK 1–7
    ort_getiri_1a = _wavg("getiri_1a")
    ort_getiri_3a = _wavg("getiri_3a")
    ort_getiri_6a = _wavg("getiri_6a")
    ort_getiri_1y = _wavg("getiri_1y")
    # Birleşik portföy getirisi varsa onu da tercih et
    if port_m:
        if port_m.get("getiri_1y") is not None:
            ort_getiri_1y = port_m.get("getiri_1y")
        if port_m.get("getiri_3a") is not None:
            ort_getiri_3a = port_m.get("getiri_3a")
        if port_m.get("getiri_1a") is not None:
            ort_getiri_1a = port_m.get("getiri_1a")
        if port_m.get("getiri_6a") is not None:
            ort_getiri_6a = port_m.get("getiri_6a")

    def _tehlike_yazi(v):
        if v is None:
            return "—"
        try:
            r = float(v)
        except Exception:
            return "—"
        etiket = (
            "Çok düşük" if r < 1.5 else
            "Düşük" if r < 2.5 else
            "Orta-düşük" if r < 3.5 else
            "Orta" if r < 4.5 else
            "Orta-yüksek" if r < 5.5 else
            "Yüksek" if r < 6.5 else
            "Çok yüksek"
        )
        return f"{r:.1f} · {etiket}"

    # Özet markdown
    lines = [f"### Portföy sepeti derin rapor ({len(sepet)} fon)", ""]
    lines.append("#### Tüm portföy — ortalama tehlike & getiri")
    lines.append(f"- **Ortalama tehlike seviyesi (ağırlıklı):** {_tehlike_yazi(ort_tehlike)} (SPK 1–7)")
    if ort_getiri_1a is not None:
        lines.append(f"- **Ortalama getiri 1A:** **%{ort_getiri_1a:.1f}**")
    if ort_getiri_3a is not None:
        lines.append(f"- **Ortalama getiri 3A:** **%{ort_getiri_3a:.1f}**")
    if ort_getiri_6a is not None:
        lines.append(f"- **Ortalama getiri 6A:** **%{ort_getiri_6a:.1f}**")
    if ort_getiri_1y is not None:
        lines.append(f"- **Ortalama getiri 1Y:** **%{ort_getiri_1y:.1f}**")
    lines.append("")
    if not ok.empty:
        a1y, ash, add, avol = _simple_avg("getiri_1y"), _simple_avg("sharpe"), _simple_avg("max_drawdown"), _simple_avg("volatilite")
        lines.append("#### Sepet ortalamaları (fon bazlı, eşit ağırlık)")
        if a1y is not None:
            lines.append(f"- Ortalama 1Y getiri: **%{a1y:.1f}**")
        if ash is not None:
            lines.append(f"- Ortalama Sharpe: **{ash:.2f}**")
        if add is not None:
            lines.append(f"- Ortalama MaxDD: **%{add:.1f}**")
        if avol is not None:
            lines.append(f"- Ortalama oynaklık: **%{avol:.1f}**")
        lines.append("")
    if liderler:
        lines.append("#### Öne çıkanlar")
        if "en_yuksek_1y" in liderler:
            x = liderler["en_yuksek_1y"]
            lines.append(f"- En yüksek 1Y: **{x['kod']}** ({x['ad']}) · %{x['deger']:.1f}")
        if "en_iyi_sharpe" in liderler:
            x = liderler["en_iyi_sharpe"]
            lines.append(f"- En iyi Sharpe: **{x['kod']}** · {x['deger']:.2f}")
        if "en_dusuk_dd" in liderler:
            x = liderler["en_dusuk_dd"]
            lines.append(f"- En sınırlı düşüş: **{x['kod']}** · %{x['deger']:.1f}")
        if "en_dusuk_vol" in liderler:
            x = liderler["en_dusuk_vol"]
            lines.append(f"- En düşük vol: **{x['kod']}** · %{x['deger']:.1f}")
        lines.append("")
    if diversify_score is not None:
        yorum = (
            "fonlar birbirinden görece bağımsız hareket ediyor"
            if diversify_score >= 55 else
            "fonlar orta düzeyde birlikte hareket ediyor"
            if diversify_score >= 35 else
            "fonlar yüksek korelasyonlu — çeşitlendirme zayıf olabilir"
        )
        lines.append(f"#### Çeşitlendirme skoru: **{diversify_score}/100**")
        lines.append(f"- {yorum} (ortalama mutlak korelasyona dayalı)")
        lines.append("")
    if kat_dag:
        lines.append("#### Kategori ağırlıkları")
        for k, v in sorted(kat_dag.items(), key=lambda x: -x[1]):
            lines.append(f"- {k}: **%{v:.0f}**")
        lines.append("")
    if birlesik_dag:
        lines.append("#### Birleşik varlık kırılımı (ağırlıklı)")
        labels = {"hisse": "Hisse", "tahvil": "Tahvil", "repo": "Repo", "mevduat": "Mevduat",
                  "altin": "Altın", "doviz": "Döviz", "fon": "Fon", "diger": "Diğer"}
        for k, v in sorted(birlesik_dag.items(), key=lambda x: -x[1]):
            lines.append(f"- {labels.get(k, k)}: **%{v:.1f}**")
        lines.append("")
    if port_m:
        lines.append("#### Birleşik portföy (seçilen ağırlıklar)")
        lines.append(
            core.turkce_degerlendirme(
                sharpe=port_m.get("sharpe"), sortino=port_m.get("sortino"),
                max_dd=port_m.get("max_drawdown"), vol=port_m.get("volatilite"),
                cagr=port_m.get("cagr"), calmar=port_m.get("calmar"),
                getiri_1y=port_m.get("getiri_1y"), reel_1y=port_m.get("reel_1y"),
                vs_mevduat=port_m.get("vs_mevduat"), vs_bist100=port_m.get("vs_bist100"),
                kazanma_orani=port_m.get("kazanma_orani"),
                en_kotu_gun=port_m.get("en_kotu_gun"),
                plain=True,
            )
        )
        lines.append("")
    lines.append("#### Fon listesi")
    for _, r in df.iterrows():
        if pd.notna(r.get("hata")) and str(r.get("hata")) not in ("", "nan", "None"):
            lines.append(f"- `{r['kod']}`: ⚠ {r['hata']}")
        else:
            wpct = w_norm.get(str(r["kod"]), 0) * 100
            lines.append(
                f"- `{r['kod']}` %{wpct:.0f} · {r.get('ad') or ''} — "
                f"1Y %{fmt_num(r.get('getiri_1y'))} · Sh {fmt_num(r.get('sharpe'), 2)} · "
                f"DD {fmt_num(r.get('max_drawdown'))}% · vol {fmt_num(r.get('volatilite'))}%"
            )
    lines.append("")
    lines.append("*Geçmiş performans geleceği garanti etmez; yatırım tavsiyesi değildir.*")

    # Portföy fiyat serisi (HTML / quantstats için)
    port_fd = None
    if port_ret is not None and len(port_ret) >= 20:
        try:
            port_px = (1 + port_ret).cumprod() * 100
            port_fd = pd.DataFrame({"tarih": port_px.index, "fiyat": port_px.values})
        except Exception:
            port_fd = None

    # Çeşitlendirme önerileri (aday = son tarama/sıralama)
    oneriler = []
    try:
        aday = None
        for key in ("son_siralama", "son_tarama"):
            cand = st.session_state.get(key)
            if cand is not None and hasattr(cand, "empty") and not cand.empty:
                aday = cand
                break
        if series and len(series) >= 1:
            oneriler = core.cesitlendirme_onerileri(
                analiz, series, aday_df=aday, max_aday=35, top_n=8,
                mevcut_kodlar=set(series.keys()),
            )
            if oneriler:
                lines.append("")
                lines.append("#### Çeşitlendirme önerileri (TEFAS açık · tehlike + getiri)")
                for o in oneriler[:6]:
                    lines.append(
                        f"- **{o['kod']}** — {o.get('ad','')} · "
                        f"tehlike: {o.get('tehlike_seviyesi') or '—'} · "
                        f"1Y %{o.get('getiri_1y') if o.get('getiri_1y') is not None else '—'} · "
                        f"3A %{o.get('getiri_3a') if o.get('getiri_3a') is not None else '—'} · "
                        f"ort.ρ {o.get('ortalama_korelasyon')} · "
                        f"çeşitl. {o.get('beklenen_cesitlilik')}/100 · {o.get('neden')}"
                    )
    except Exception as e:
        oneriler = []
        lines.append(f"\n_Çeşitlendirme önerisi hesaplanamadı: {e}_")

    return {
        "df": df,
        "ok": ok,
        "port_metrikler": port_m,
        "ozet_md": "\n".join(lines),
        "weights": w_norm,
        "corr": corr_df,
        "diversify_score": diversify_score,
        "liderler": liderler,
        "kategori_dag": kat_dag,
        "birlesik_dag": birlesik_dag,
        "port_ret": port_ret,
        "port_fd": port_fd,
        "series": series,
        "oneriler": oneriler,
        "n_fon": len(kodlar_ok),
        "ort_tehlike": ort_tehlike,
        "ort_tehlike_yazi": _tehlike_yazi(ort_tehlike),
        "ort_getiri_1a": ort_getiri_1a,
        "ort_getiri_3a": ort_getiri_3a,
        "ort_getiri_6a": ort_getiri_6a,
        "ort_getiri_1y": ort_getiri_1y,
    }



def fmt_num(v, digits=1, suffix=""):
    if v is None or (isinstance(v, float) and (np.isnan(v) or not np.isfinite(v))):
        return "—"
    try:
        return f"{float(v):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def df_to_excel_bytes(df: pd.DataFrame, meta: dict = None) -> bytes:
    buf = io.BytesIO()
    # core.excel_kaydet dosyaya yazar; burada belleğe yazıyoruz
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from openpyxl.formatting.rule import ColorScaleRule

        sutun_map = {
            "kod": "Fon Kodu", "ad": "Fon Adı", "skor": "Skor", "puan": "Puan",
            "sira": "Sıra", "kurucu": "Kurucu", "tefas_durum": "TEFAS",
            "risk_degeri": "Risk", "faiz": "Faiz", "kategori": "Kategori",
            "getiri_1a": "1A %", "getiri_3a": "3A %", "getiri_6a": "6A %",
            "getiri_1y": "1Y %", "getiri_3y": "3Y %",
            "reel_1y": "Reel 1Y %", "vs_mevduat": "vs Mevduat",
            "vs_bist100": "vs BIST100", "vs_altin": "vs Altın",
            "volatilite": "Vol %", "max_drawdown": "MaxDD %",
            "sharpe": "Sharpe", "sortino": "Sortino", "calmar": "Calmar",
            "cagr": "CAGR %", "kazanma_orani": "Kazanma %",
            "var95": "VaR %95", "cvar95": "cVaR %95",
            "risk_skor": "Risk skor", "likidite_skor": "Likidite skor",
            "likidite_seviye": "Likidite seviye", "risk_bayrak": "Risk bayrakları",
            "yatirim": "Portföy yatırımı", "baskin_tur": "Baskın Tür",
            "akran_grubu": "Akran Grubu", "getiri_z": "Getiri z-skor",
            "rapor_skor": "Rapor skor", "mesafe_skoru": "Dağılım Sapması",
            "d_hisse": "Hisse %", "d_tahvil": "Tahvil %", "d_repo": "Repo %",
            "d_mevduat": "Mevduat %", "d_altin": "Altın %", "d_doviz": "Döviz %",
            "buyukluk": "Büyüklük", "yatirimci": "Yatırımcı",
            "giris_kom": "Giriş Kom.", "cikis_kom": "Çıkış Kom.",
        }
        mevcut = [c for c in sutun_map if c in df.columns]
        out = df[mevcut].copy()
        for c in out.columns:
            if pd.api.types.is_numeric_dtype(out[c]):
                out[c] = out[c].replace([np.inf, -np.inf], np.nan)
        out = out.rename(columns={c: sutun_map[c] for c in mevcut})

        wb = Workbook()
        ws = wb.active
        ws.title = "Fonlar"
        NAVY, WHITE = "1B2838", "FFFFFF"
        header_fill = PatternFill("solid", fgColor=NAVY)
        header_font = Font(name="Calibri", bold=True, color=WHITE, size=11)
        thin = Border(
            left=Side(style="thin", color="E2E8F0"),
            right=Side(style="thin", color="E2E8F0"),
            top=Side(style="thin", color="E2E8F0"),
            bottom=Side(style="thin", color="E2E8F0"),
        )
        for j, col in enumerate(out.columns, 1):
            cell = ws.cell(1, j, col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin
        for i, row in enumerate(out.itertuples(index=False), 2):
            for j, val in enumerate(row, 1):
                cell = ws.cell(i, j, None if (isinstance(val, float) and val != val) else val)
                cell.border = thin
                cell.font = Font(name="Calibri", size=10)
                if isinstance(val, (int, float)) and val == val:
                    cell.number_format = "0.00"
        for j, col in enumerate(out.columns, 1):
            ws.column_dimensions[get_column_letter(j)].width = min(max(len(str(col)) + 3, 10), 36)
        ws.auto_filter.ref = f"A1:{get_column_letter(len(out.columns))}{len(out)+1}"
        ws.freeze_panes = "A2"

        if meta:
            ws2 = wb.create_sheet("Parametreler")
            ws2["A1"], ws2["B1"] = "Alan", "Değer"
            for cell in (ws2["A1"], ws2["B1"]):
                cell.fill = header_fill
                cell.font = header_font
            for i, (k, v) in enumerate(meta.items(), 2):
                ws2[f"A{i}"] = str(k)
                ws2[f"B{i}"] = str(v)
            ws2.column_dimensions["A"].width = 28
            ws2.column_dimensions["B"].width = 40

        wb.save(buf)
    except Exception:
        out2 = df.copy()
        out2.to_excel(buf, index=False)
    buf.seek(0)
    return buf.read()


def show_df(df: pd.DataFrame, cols: list, height: int = 420):
    if df is None or df.empty:
        st.info("Gösterilecek veri yok.")
        return
    use = [c for c in cols if c in df.columns]
    if not use:
        st.dataframe(df, use_container_width=True, height=height)
        return
    view = df[use].copy()
    pct_cols = {
        "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y", "getiri_3y",
        "reel_1y", "vs_mevduat", "vs_bist100", "vs_altin",
        "volatilite", "max_drawdown", "cagr", "kazanma_orani",
        "var95", "cvar95", "en_iyi_gun", "en_kotu_gun",
        "likidite_skor", "risk_skor", "secim_skor", "skor", "puan",
        "mesafe_skoru", "d_hisse", "d_tahvil", "d_repo", "d_mevduat",
        "d_altin", "d_doviz", "likit_varlik_pct",
    }
    ratio_cols = {"sharpe", "sortino", "calmar", "getiri_z", "rapor_skor", "bist_korelasyon"}
    for c in view.columns:
        if c in pct_cols and pd.api.types.is_numeric_dtype(view[c]):
            view[c] = pd.to_numeric(view[c], errors="coerce").round(2)
        elif c in ratio_cols and pd.api.types.is_numeric_dtype(view[c]):
            view[c] = pd.to_numeric(view[c], errors="coerce").round(2)
    col_cfg = {}
    try:
        for c in view.columns:
            if c in ("ad", "risk_bayrak", "yatirim"):
                col_cfg[c] = st.column_config.TextColumn(c, width="medium")
            elif c in pct_cols or c in ratio_cols:
                col_cfg[c] = st.column_config.NumberColumn(c, format="%.2f")
            elif c in ("kod", "sira"):
                col_cfg[c] = st.column_config.TextColumn(c, width="small")
    except Exception:
        col_cfg = None
    st.dataframe(
        view, use_container_width=True, height=height, hide_index=True,
        column_config=col_cfg if col_cfg else None,
    )


def save_excel_to_disk(df: pd.DataFrame, filename: str, meta: dict = None) -> Path | None:
    """Sonuçları programa ait klasöre Excel olarak yazar."""
    if df is None or df.empty:
        return None
    path = ROOT / filename
    try:
        # core.excel_kaydet varsa onu kullan (daha zengin format)
        core.excel_kaydet(df, str(path), meta=meta)
        return path
    except Exception:
        try:
            data = df_to_excel_bytes(df, meta)
            path.write_bytes(data)
            return path
        except Exception as e:
            st.warning(f"Excel diske yazılamadı: {e}")
            return None


def download_excel_button(df: pd.DataFrame, filename: str, meta: dict = None, key: str = "xl", auto_save: bool = True):
    """Hem indirme butonu gösterir hem (isteğe bağlı) diske kaydeder."""
    if df is None or df.empty:
        return
    if auto_save:
        saved = save_excel_to_disk(df, filename, meta)
        if saved:
            st.success(f"💾 Excel kaydedildi: `{saved}`")
    data = df_to_excel_bytes(df, meta)
    st.download_button(
        "📥 Excel indir",
        data=data,
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=key,
    )


# ── Kategori seçimi (grup + tam ad) ─────────────────────────────────────────
KATEGORI_GRUPLARI = {
    "Hisse": ["hisse", "yabanci hisse", "endeks"],
    "Borçlanma araçları": ["borclanma", "tahvil", "bono", "eurobond", "dis borclanma"],
    "Para piyasası / Likit": ["para piyasasi", "likit", "kisa vadeli"],
    "Altın / Kıymetli maden": ["altin", "kiymetli maden", "gumus"],
    "Karma / Değişken": ["karma", "degisken", "dengeli"],
    "Serbest": ["serbest"],
    "Fon sepeti": ["fon sepeti"],
    "Katılım (faizsiz)": ["katilim"],
    "Döviz / Yabancı": ["doviz", "yabanci", "dolar", "euro"],
    "Girişim / Gayrimenkul": ["girisim", "gayrimenkul", "gyf"],
}


def kategori_maske(seri: pd.Series, gruplar=None, tam_adlar=None) -> pd.Series:
    """Grup (anahtar kelime) VEYA tam ad seçimlerinden biri tutan satırları işaretler."""
    if not gruplar and not tam_adlar:
        return pd.Series(True, index=seri.index)
    norm = seri.astype(str).apply(lambda x: core._normalize_tr(x).lower())
    m = pd.Series(False, index=seri.index)
    for g in gruplar or []:
        for kw in KATEGORI_GRUPLARI.get(g, []):
            m |= norm.str.contains(kw, na=False, regex=False)
    if tam_adlar:
        m |= seri.astype(str).isin(tam_adlar)
    return m


def kategori_secici(df, key: str, container=None):
    """Kategori grubu + (varsa) tam TEFAS kategorisi çoklu seçimi. (gruplar, tam_adlar) döner."""
    c = container or st
    gruplar = c.multiselect(
        "Kategori grubu", list(KATEGORI_GRUPLARI.keys()), key=f"{key}_grp",
        help="Birden fazla seçilebilir; seçilenlerden herhangi biri eşleşen fon kalır. Boş = hepsi",
    )
    tam = []
    if df is not None and not df.empty and "kategori" in df.columns:
        vc = df["kategori"].dropna().astype(str).str.strip()
        vc = vc[vc != ""].value_counts()
        if len(vc):
            tam = c.multiselect(
                "TEFAS kategorisi (tam ad)", vc.index.tolist(), key=f"{key}_tam",
                format_func=lambda k: f"{k} ({vc[k]})",
            )
    return gruplar, tam


def risk_badge(seviye: str) -> str:
    s = (seviye or "").lower()
    if s == "yüksek":
        return '<span class="badge badge-ok">Likidite yüksek</span>'
    if s == "orta":
        return '<span class="badge badge-warn">Likidite orta</span>'
    if s == "düşük":
        return '<span class="badge badge-bad">Likidite düşük</span>'
    return ""


# ── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📊 TEFAS Analiz")
    st.caption("Fon tarama · risk · portföy · rapor")

    sayfa = st.radio(
        "Menü",
        [
            "🏠 Ana Sayfa",
            "🔍 Fon Tarama",
            "📋 Tüm Fon Sıralaması",
            "🧪 Filtre Stüdyosu",
            "⚖️ Karşılaştırma",
            "📁 Portföy",
            "💼 Portföyüm",
            "🎯 Optimizasyon",
            "🔎 Fon Detay",
            "📈 Hisse & Karma Portföy",
            "🔎 Hisse Detay",
            "🏦 BIST Değerleme",
            "⭐ Favoriler",
            "⚙️ Ayarlar",
        ],
        label_visibility="collapsed",
    )

    tefas_portfoyum.sidebar_rozet(st)
    st.divider()
    tip = st.selectbox("Fon tipi", ["YAT", "EMK", "BYF"], index=0)
    st.session_state.fon_tipi = tip

    cfg = st.session_state.cfg
    st.caption(
        f"Risksiz faiz: %{cfg.get('risk_free', 0.4)*100:.0f} · "
        f"TÜFE: %{cfg.get('tufe_yillik')} · "
        f"Mevduat: %{cfg.get('mevduat_yillik')}"
    )
    st.caption(f"Cache: `{core.CACHE_DIR.name}/`")
    sepet = st.session_state.get("portfoy_sepet") or []
    st.divider()
    st.markdown(f"### 🛒 Sepet · {len(sepet)}")
    if sepet:
        st.caption(", ".join(x["kod"] for x in sepet[:15]) + ("…" if len(sepet) > 15 else ""))
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Analiz et", key="sepet_analiz_side"):
                st.session_state["_goto_portfoy"] = True
                st.rerun()
        with b2:
            if st.button("Temizle", key="sepet_temiz_side"):
                st.session_state.portfoy_sepet = []
                st.session_state.sepet_analiz = None
                st.rerun()
    else:
        st.caption("Tarama / sıralama / detay → Portföye ekle")


# ═══════════════════════════════════════════════════════════════════════════
# ANA SAYFA
# ═══════════════════════════════════════════════════════════════════════════
if sayfa == "🏠 Ana Sayfa":
    st.markdown(
        """
<div class="hero">
  <h1>TEFAS Fon Analiz</h1>
  <p>Günlük özet · alarm · track record · net getiri · bilanço bildirimi</p>
</div>
""",
        unsafe_allow_html=True,
    )

    tefas_panel.render_ana_panel()

    sepet_n = len(st.session_state.get("portfoy_sepet") or [])
    sa = st.session_state.get("sepet_analiz") or {}
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Fon tipi", st.session_state.fon_tipi)
    c2.metric("Risksiz faiz", f"%{st.session_state.cfg.get('risk_free', 0.4)*100:.0f}")
    c3.metric("TÜFE (yıllık)", f"%{st.session_state.cfg.get('tufe_yillik', '—')}")
    c4.metric("Sepet", f"{sepet_n} fon")
    if sa.get("diversify_score") is not None:
        c5.metric("Çeşitlendirme", f"{sa.get('diversify_score')}/100")
    else:
        c5.metric("Çeşitlendirme", "—")

    # ── Günlük özet ────────────────────────────────────────────────────────
    st.subheader("📌 Bugünün özeti")
    o1, o2, o3 = st.columns(3)
    with o1:
        st.markdown("**Sepet**")
        if sepet_n == 0:
            st.caption("Sepet boş · Portföy sayfasından ekleyin")
        elif sa and sa.get("port_metrikler"):
            pm = sa["port_metrikler"]
            st.write(
                f"1Y **%{pm.get('getiri_1y') or sa.get('ort_getiri_1y') or '—'}** · "
                f"Sharpe **{pm.get('sharpe') or '—'}** · "
                f"MaxDD **%{pm.get('max_drawdown') or '—'}**"
            )
            if sa.get("ort_tehlike_yazi"):
                st.caption(f"Ort. tehlike: {sa.get('ort_tehlike_yazi')}")
        else:
            st.caption(f"{sepet_n} fon sepette · henüz analiz yok → Portföy’de «analiz et»")
    with o2:
        st.markdown("**Son tarama**")
        meta = st.session_state.get("son_tarama_meta") or {}
        df_t = st.session_state.get("son_tarama")
        if df_t is not None and hasattr(df_t, "empty") and not df_t.empty:
            st.write(f"**{len(df_t)}** fon · {meta.get('risk_paketi') or meta.get('mod') or 'tarama'}")
            if "getiri_1y" in df_t.columns:
                top = df_t.nlargest(3, "getiri_1y") if "getiri_1y" in df_t.columns else df_t.head(3)
                st.caption("1Y lider: " + ", ".join(
                    f"{r.get('kod')} %{r.get('getiri_1y'):.0f}" if pd.notna(r.get("getiri_1y")) else str(r.get("kod"))
                    for _, r in top.iterrows()
                ))
        else:
            st.caption("Henüz tarama yok")
    with o3:
        st.markdown("**Bilanço dönemi**")
        try:
            bil = core.bilanco_donem_durumu(probe=False)
        except Exception as e:
            bil = {"durum": "?", "mesaj": str(e)}
        if bil.get("durum") == "geldi":
            st.success(f"✅ {bil.get('yil')}/{bil.get('donem'):02d} — bilançolar gelmiş olmalı")
        elif bil.get("durum") == "bekleniyor":
            st.warning(f"⏳ {bil.get('mesaj', '')}")
        else:
            st.info(bil.get("mesaj") or "—")
        st.caption(bil.get("mesaj") or "")
        if st.button("Canlı doğrula (İş Yatırım probe)", key="btn_bilanco_probe"):
            with st.spinner("Probe hisseler kontrol ediliyor…"):
                bil2 = core.bilanco_donem_durumu(probe=True)
            if bil2.get("probe") is True:
                st.success(bil2.get("mesaj"))
            elif bil2.get("probe") is False:
                st.warning(bil2.get("mesaj"))
            else:
                st.info(bil2.get("mesaj") or "Probe doğrulanamadı (ağ)")

    # ── Alarmlar ───────────────────────────────────────────────────────────
    st.subheader("🔔 Alarmlar")
    alarm_msgs = core.sepet_alarmlari(sa, st.session_state.cfg)
    # Ayar alarmları son tarama üzerinde
    al_cfg = (st.session_state.cfg or {}).get("alarmlar") or {}
    df_t = st.session_state.get("son_tarama")
    if df_t is not None and hasattr(df_t, "empty") and not df_t.empty:
        for _, row in df_t.head(30).iterrows():
            kod = row.get("kod")
            if al_cfg.get("min_getiri_1y") is not None and row.get("getiri_1y") is not None:
                if float(row["getiri_1y"]) < float(al_cfg["min_getiri_1y"]):
                    alarm_msgs.append(f"{kod} 1Y %{row['getiri_1y']:.1f} < min")
            if al_cfg.get("min_sharpe") is not None and row.get("sharpe") is not None:
                if float(row["sharpe"]) < float(al_cfg["min_sharpe"]):
                    alarm_msgs.append(f"{kod} Sharpe {row['sharpe']:.2f} < min")
            if al_cfg.get("max_drawdown") is not None and row.get("max_drawdown") is not None:
                if float(row["max_drawdown"]) < float(al_cfg["max_drawdown"]):
                    alarm_msgs.append(f"{kod} MaxDD %{row['max_drawdown']:.1f}")
    if alarm_msgs:
        for m in alarm_msgs[:12]:
            st.error(m)
    else:
        st.caption("Aktif alarm yok. Eşikler: Ayarlar + sepet MaxDD / çeşitlendirme / tehlike.")

    # ── Track record ───────────────────────────────────────────────────────
    st.subheader("📈 Track record")
    tr1, tr2 = st.columns(2)
    with tr1:
        st.markdown("**Fon tarama arşivi**")
        tr_fon = core.evaluate_fon_tarama_track(analiz=None)
        st.caption(tr_fon.get("note") or "—")
        if st.button("Arşivdeki fonları bugünle karşılaştır", key="btn_tr_fon"):
            with st.spinner("Fon detayları çekiliyor…"):
                tr_fon = core.evaluate_fon_tarama_track(analiz=get_analiz())
            st.session_state["_tr_fon"] = tr_fon
        tr_fon = st.session_state.get("_tr_fon") or tr_fon
        if tr_fon.get("rows"):
            st.dataframe(pd.DataFrame(tr_fon["rows"]), hide_index=True, use_container_width=True)
    with tr2:
        st.markdown("**BIST skor arşivi**")
        tr_b = core.bist_track_record_ozet()
        st.caption(tr_b.get("note") or "—")
        if tr_b.get("perf_df") is not None:
            st.dataframe(tr_b["perf_df"], hide_index=True, use_container_width=True)
        elif tr_b.get("ok"):
            st.caption("Performans Takibi sayfası 6A+ birikim sonrası dolar.")

    # ── Net getiri (örnek sepet / son tarama) ───────────────────────────────
    st.subheader("💸 Net getiri (yönetim ücreti düşülmüş)")
    st.caption(
        "TEFAS profilde ücret alanı varsa brüt → net yaklaşık hesaplanır. "
        "Ücret yoksa satır atlanır. Formül: net ≈ (1+g)/(1+fee·dönem)−1"
    )
    net_rows = []
    try:
        analiz = get_analiz()
        kodlar_net = []
        if sepet_n:
            kodlar_net = [str(x.get("kod") or x).upper() for x in (st.session_state.get("portfoy_sepet") or [])][:8]
        elif df_t is not None and hasattr(df_t, "empty") and not df_t.empty:
            kodlar_net = [str(k).upper() for k in df_t["kod"].head(5).tolist()]
        if kodlar_net and st.button("Sepet/tarama için net getiri hesapla", key="btn_net_getiri"):
            with st.spinner("Profil + metrik…"):
                for kod in kodlar_net:
                    try:
                        det = analiz.fon_detay(kod)
                        p, o, m = det.get("profil") or {}, det.get("ozet") or {}, det.get("metrikler") or {}
                        fee = core.profil_yonetim_ucreti(p, o)
                        mn = core.metrik_net_getiriler(m, fee)
                        net_rows.append({
                            "kod": kod,
                            "ad": (o.get("fonUnvan") or p.get("fonUnvan") or kod)[:40],
                            "ücret_%": fee,
                            "1Y_brüt": m.get("getiri_1y"),
                            "1Y_net": mn.get("net_getiri_1y"),
                            "6A_net": mn.get("net_getiri_6a"),
                            "3A_net": mn.get("net_getiri_3a"),
                        })
                    except Exception:
                        continue
            st.session_state["_net_rows"] = net_rows
        net_rows = st.session_state.get("_net_rows") or []
        if net_rows:
            st.dataframe(pd.DataFrame(net_rows), hide_index=True, use_container_width=True)
        else:
            st.caption("Hesaplamak için butona basın (sepet veya son tarama gerekir).")
    except Exception as e:
        st.caption(f"Net getiri: {e}")

    with st.expander("Önerilen akış & modüller", expanded=False):
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            st.markdown("**1. Tarama** — aday havuzu")
        with f2:
            st.markdown("**2. Akıllı seçim** — skor")
        with f3:
            st.markdown("**3. Sepet** — analiz + HTML")
        with f4:
            st.markdown("**4. Detay** — rapor")
        st.markdown(
            """
| Modül | Ne işe yarar |
|-------|----------------|
| **Fon Tarama / Sıralama** | Aday havuzu + arşive kayıt (track record) |
| **Portföy** | Sepet, alarm girdileri, çeşitlendirme |
| **BIST Değerleme** | Skor arşivi + bilanço dönemi |
| **Ayarlar** | Alarm eşikleri (1Y, Sharpe, MaxDD, vol) |
"""
        )
        st.caption("Geçmiş performans geleceği garanti etmez. Yatırım tavsiyesi değildir.")

    favs = core.load_favs()
    if favs:
        st.caption(f"Favoriler ({len(favs)}): " + ", ".join(favs[:20]) + ("…" if len(favs) > 20 else ""))


elif sayfa == "🔍 Fon Tarama":
    st.header("🔍 Fon Tarama")
    st.caption("Hedef dağılım + risk + kalite kapıları → sıralı adaylar → akıllı seçim / portföy")

    # ── Hızlı senaryo ──────────────────────────────────────────────────────
    st.subheader("⚡ Hızlı senaryo")
    senaryo = st.radio(
        "Senaryo",
        [
            "Geniş tarama (çok fon)",
            "Dengeli",
            "Muhafazakâr",
            "Agresif büyüme",
            "Enflasyon üstü (reel getiri)",
            "Düşük risk / yüksek likidite",
            "Özel (aşağıdan ayarla)",
        ],
        horizontal=True,
        label_visibility="collapsed",
        index=0,
    )
    SENARYO_MAP = {
        # Geniş tarama: dağılım toleransı çok yüksek + neredeyse hiç kalite kapısı
        # → mümkün olduğunca çok fon bulmak için
        "Geniş tarama (çok fon)": {
            "paket": "0", "profil": "2", "gp": "2",
            "min_1y": None, "min_sharpe": None, "max_vol": None, "max_dd": None,
            "max_risk_skor": 40.0, "min_liq": 10.0, "pozitif_donem": False,
            "min_kazanma": None, "min_en_kotu": None, "max_var95": None, "max_cvar95": None,
            "max_fon": 50, "max_aday": 120, "tolerans": 80,
        },
        "Dengeli": {
            "paket": "2", "profil": "2", "gp": "2",
            "min_1y": 0.0, "min_sharpe": 0.0, "max_vol": 30.0, "max_dd": -25.0,
            "max_risk_skor": 22.0, "min_liq": 30.0, "pozitif_donem": False,
            "min_kazanma": 40.0, "min_en_kotu": -12.0, "max_var95": 6.0, "max_cvar95": 10.0,
            "max_fon": 40, "max_aday": 100, "tolerans": 55,
        },
        "Muhafazakâr": {
            "paket": "1", "profil": "3", "gp": "1",
            "min_1y": None, "min_sharpe": 0.0, "max_vol": 15.0, "max_dd": -10.0,
            "max_risk_skor": 15.0, "min_liq": 45.0, "pozitif_donem": False,
            "min_kazanma": 48.0, "min_en_kotu": -6.0, "max_var95": 3.5, "max_cvar95": 5.5,
            "min_sortino": 0.3,
            "max_fon": 25, "max_aday": 80, "tolerans": 45,
        },
        "Agresif büyüme": {
            "paket": "3", "profil": "1", "gp": "3",
            "min_1y": 5.0, "min_sharpe": None, "max_vol": 55.0, "max_dd": -45.0,
            "max_risk_skor": 30.0, "min_liq": 20.0, "pozitif_donem": False,
            "min_kazanma": 40.0, "min_en_kotu": -18.0, "max_var95": 8.0, "max_cvar95": 14.0,
            "max_fon": 40, "max_aday": 100, "tolerans": 60,
        },
        "Enflasyon üstü (reel getiri)": {
            "paket": "2", "profil": "2", "gp": "2",
            "min_1y": float(st.session_state.cfg.get("tufe_yillik") or 30),
            "min_sharpe": 0.0, "max_vol": 35.0, "max_dd": -30.0,
            "max_risk_skor": 22.0, "min_liq": 30.0, "pozitif_donem": False,
            "min_kazanma": 40.0, "min_en_kotu": -12.0, "max_var95": 6.0, "max_cvar95": 10.0,
            "max_fon": 35, "max_aday": 90, "tolerans": 55,
        },
        "Düşük risk / yüksek likidite": {
            "paket": "1", "profil": "4", "gp": "1",
            "min_1y": None, "min_sharpe": 0.0, "max_vol": 12.0, "max_dd": -8.0,
            "max_risk_skor": 12.0, "min_liq": 55.0, "pozitif_donem": False,
            "min_kazanma": 50.0, "min_en_kotu": -5.0, "max_var95": 3.0, "max_cvar95": 5.0,
            "min_sortino": 0.5,
            "max_fon": 25, "max_aday": 80, "tolerans": 45,
        },
        "Özel (aşağıdan ayarla)": None,
    }
    sc = SENARYO_MAP.get(senaryo) or {}

    with st.expander("① Profil & risk paketi", expanded=(senaryo == "Özel (aşağıdan ayarla)")):
        col_a, col_b = st.columns(2)
        with col_a:
            profil_default = sc.get("profil", "2")
            profil_keys = list(core.PROFILLER.keys())
            profil_sec = st.selectbox(
                "Dağılım profili",
                profil_keys,
                format_func=lambda k: core.PROFILLER[k][0],
                index=profil_keys.index(profil_default) if profil_default in profil_keys else 1,
            )
            if profil_sec == "6":
                st.markdown("**Özel dağılım (%)**")
                hedef = {}
                for kat, ac in core.KATEGORI_ACIKLAMA.items():
                    v = st.slider(ac, 0, 100, 0, key=f"h_{kat}")
                    if v > 0:
                        hedef[kat] = float(v)
                if not hedef:
                    hedef = core.PROFILLER["2"][1]
            else:
                hedef = core.PROFILLER[profil_sec][1]
                st.json(hedef)
        with col_b:
            paket_default = sc.get("paket", "2")
            paket_keys = list(core.RISK_PAKETLER.keys())
            paket_sec = st.selectbox(
                "Risk paketi",
                paket_keys,
                format_func=lambda k: f"{core.RISK_PAKETLER[k]['ad']} — {core.RISK_PAKETLER[k]['aciklama']}",
                index=paket_keys.index(paket_default) if paket_default in paket_keys else 1,
            )
            paket = core.RISK_PAKETLER[paket_sec]

    with st.expander("② Performans & risk filtreleri", expanded=(senaryo == "Özel (aşağıdan ayarla)")):
        c1, c2, c3 = st.columns(3)
        _m1y = sc.get("min_1y", None)
        _msh = sc.get("min_sharpe", paket.get("min_sharpe"))
        _mvol = sc.get("max_vol", paket.get("max_vol"))
        min_1y = c1.number_input("Min 1Y getiri %", value=float(_m1y) if _m1y is not None else None,
                                 step=1.0, format="%.1f", placeholder="yok")
        min_sharpe = c2.number_input("Min Sharpe",
            value=float(_msh) if _msh is not None else None, step=0.1, format="%.2f", placeholder="yok")
        max_vol = c3.number_input("Max volatilite %",
            value=float(_mvol) if _mvol is not None else None, step=1.0, format="%.1f", placeholder="yok")
        c4, c5, c6 = st.columns(3)
        _mdd = sc.get("max_dd", paket.get("max_dd"))
        max_dd = c4.number_input("Max DD % (örn -20)",
            value=float(_mdd) if _mdd is not None else None, step=1.0, format="%.1f", placeholder="yok")
        min_risk = c5.number_input("Min risk (1-7)", value=None, min_value=1, max_value=7, step=1, placeholder="yok")
        max_risk = c6.number_input("Max risk (1-7)",
            value=int(paket["max_risk"]) if paket.get("max_risk") is not None else None,
            min_value=1, max_value=7, step=1, placeholder="yok")
        c7, c8, c9 = st.columns(3)
        kurucu = c7.text_input("Kurucu metni", placeholder="örn AK PORTFÖY")
        faiz_sec = c8.selectbox("Faiz filtresi", ["Hepsi", "Faizli", "Katılım (faizsiz)"], index=0)
        tolerans = c9.slider(
            "Dağılım toleransı", 10, 100, int(sc.get("tolerans", 55)),
            help="Yüksek = neredeyse tüm fonlar geçer (80–100 ≈ serbest). Düşük = hedef profile yakın fonlar.",
        )
        c10, c11 = st.columns(2)
        max_fon = c10.slider("Kaç fon gösterilsin", 5, 100, int(sc.get("max_fon", 40)))
        max_aday = c11.slider("Aday havuzu", 20, 200, int(sc.get("max_aday", 100)))
        tur_sec = st.multiselect(
            "Fon türü (baskın varlık)",
            options=list(core.TUR_ACIKLAMA.keys()),
            format_func=lambda k: core.TUR_ACIKLAMA[k],
            default=[],
        )
        tur_filtre = tur_sec or None

    # Geniş tarama seçildiyse kalite kapılarını varsayılan kapalı tut
    _genis = senaryo.startswith("Geniş")
    with st.expander("③ Kalite kapıları & skor (seçimi iyileştirir)", expanded=not _genis):
        st.markdown(
            "Tarama sonrası **ikinci eleme**: anomali, likidite, dönem tutarlılığı, "
            "kazanma oranı, en kötü gün vb. **Çok fon istiyorsan bu bölümü gevşet veya kapalı tut.**"
        )
        k1, k2, k3, k4 = st.columns(4)
        _mrs = sc.get("max_risk_skor")
        max_risk_skor = k1.number_input(
            "Max risk_skor (anomali)",
            value=float(_mrs) if _mrs is not None else 40.0, step=1.0,
            help="Yüksek tut → daha az eleme (40 ≈ neredeyse kapalı)",
        )
        _ml = sc.get("min_liq")
        min_liq = k2.number_input(
            "Min likidite skor",
            value=float(_ml) if _ml is not None else 10.0, step=5.0,
            help="Düşük tut → daha az eleme (10 ≈ neredeyse kapalı)",
        )
        pozitif_donem = k3.checkbox(
            "1A/3A/6A/1Y hepsi ≥ 0",
            value=bool(sc.get("pozitif_donem", False)),
            help="Açık bırakırsanız daha fazla fon geçer. Piyasa düşüşlü dönemlerde çok eleme yapar.",
        )
        min_reel = k4.checkbox(
            "Reel getiri > 0 (TÜFE üstü)",
            value=(senaryo == "Enflasyon üstü (reel getiri)"),
        )

        st.markdown("**Ek risk / tutarlılık filtreleri** (boş = filtre yok)")
        e1, e2, e3, e4 = st.columns(4)
        _mk = sc.get("min_kazanma")
        min_kazanma = e1.number_input(
            "Min kazanma oranı %",
            value=float(_mk) if _mk is not None else None,
            min_value=0.0, max_value=100.0, step=1.0, placeholder="yok",
            help="Boş bırakırsan uygulanmaz. 50 ≈ yarıdan fazla gün yeşil.",
        )
        _mek = sc.get("min_en_kotu")
        min_en_kotu = e2.number_input(
            "En kötü gün alt sınırı %",
            value=float(_mek) if _mek is not None else None,
            step=0.5, format="%.1f", placeholder="yok",
            help="Boş = filtre yok. Örn. -5 → tek günde %5'ten fazla düşenler elenir.",
        )
        min_sortino = e3.number_input(
            "Min Sortino",
            value=float(sc.get("min_sortino")) if sc.get("min_sortino") is not None else None,
            step=0.1, format="%.2f", placeholder="yok",
            help="Sadece olumsuz oynaklığa göre risk-ayarlı getiri.",
        )
        _mvar = sc.get("max_var95")
        max_var95 = e4.number_input(
            "Max |VaR %95| (örn 3 = -3%)",
            value=float(_mvar) if _mvar is not None else None,
            min_value=0.5, max_value=20.0, step=0.5, placeholder="yok",
            help="Boş = filtre yok. Günlük kayıp üst sınırı.",
        )
        e5, e6, e7, e8 = st.columns(4)
        min_cagr = e5.number_input(
            "Min CAGR %", value=None, step=1.0, format="%.1f", placeholder="yok", key="min_cagr_kalite",
        )
        min_en_iyi = e6.number_input(
            "Min en iyi gün % (opsiyonel)", value=None, step=0.5, format="%.1f", placeholder="yok",
            help="Aşırı düşük tavan getirili fonları elemek için nadiren kullanılır.",
        )
        _mcvar = sc.get("max_cvar95")
        max_cvar95 = e7.number_input(
            "Max |cVaR %95| (örn 5)",
            value=float(_mcvar) if _mcvar is not None else None,
            min_value=0.5, max_value=30.0, step=0.5, placeholder="yok",
            help="Boş = filtre yok. En kötü %5 günlerin ortalaması.",
        )
        zorunlu_kazanma = e8.checkbox(
            "Kazanma oranı filtresini uygula",
            value=False if _genis else (min_kazanma is not None),
            help="Kapalıyken min kazanma oranı uygulanmaz → daha geniş sonuç.",
        )

        st.markdown("**Maliyet · büyüklük · likidite süresi · diğer**")
        x1, x2, x3, x4 = st.columns(4)
        max_toplam_kom = x1.number_input(
            "Max giriş+çıkış kom. %", value=None, step=0.1, format="%.2f", placeholder="yok",
            help="Giriş ve çıkış komisyonu toplamı üst sınırı",
            key="max_kom_kalite",
        )
        min_buyukluk_mn = x2.number_input(
            "Min portföy büyüklüğü (Mn TL)", value=None, step=10.0, format="%.0f", placeholder="yok",
            help="Örn. 100 = en az 100 milyon TL AUM",
            key="min_buyukluk_kalite",
        )
        min_yatirimci = x3.number_input(
            "Min yatırımcı sayısı", value=None, step=10, format="%d", placeholder="yok",
            key="min_yat_kalite",
        )
        max_valor = x4.number_input(
            "Max geri alım valör (gün)", value=None, min_value=0, max_value=30, step=1, placeholder="yok",
            help="T+0 / T+1 tercih için düşük tutun",
            key="max_valor_kalite",
        )
        x5, x6, x7, x8 = st.columns(4)
        min_calmar = x5.number_input(
            "Min Calmar", value=None, step=0.1, format="%.2f", placeholder="yok", key="min_calmar_kalite",
        )
        min_3y = x6.number_input(
            "Min 3Y getiri %", value=None, step=1.0, format="%.1f", placeholder="yok", key="min_3y_kalite",
        )
        min_6a = x7.number_input(
            "Min 6A getiri %", value=None, step=1.0, format="%.1f", placeholder="yok", key="min_6a_kalite",
        )
        min_vs_mev = x8.number_input(
            "Min vs mevduat (puan)", value=None, step=1.0, format="%.1f", placeholder="yok",
            help="1Y getiri − mevduat farkı",
            key="min_vs_mev_kalite",
        )
        x9, x10, x11 = st.columns(3)
        max_mesafe = x9.number_input(
            "Max dağılım sapması", value=None, step=5.0, format="%.0f", placeholder="yok",
            help="Hedef profile uzaklık; düşük = daha uyumlu",
            key="max_mesafe_kalite",
        )
        max_jump = x10.number_input(
            "Max sıçrama sayısı (>3σ)", value=None, step=1, format="%d", placeholder="yok",
            key="max_jump_kalite",
        )
        sadece_katilim = x11.checkbox("Sadece katılım (faizsiz eğilim)", value=False, key="sadece_katilim")

        st.markdown("**Kategori & BIST korelasyonu**")
        y1, y2, y3 = st.columns(3)
        kategori_filtre = y1.text_input(
            "Kategori içerir (örn Hisse, Borçlanma, Karma)",
            value="",
            help="TEFAS kategori adında geçen metin; boş = hepsi",
            key="kategori_filtre_txt",
        ).strip()
        kategori_gruplari = y1.multiselect(
            "Kategori grubu (çoklu)", list(KATEGORI_GRUPLARI.keys()),
            help="Seçilenlerden biri eşleşen fonlar kalır; boş = hepsi",
            key="kategori_grup_sec",
        )
        max_bist_korr = y2.number_input(
            "Max BIST100 korelasyonu",
            value=None, min_value=-1.0, max_value=1.0, step=0.05, format="%.2f",
            placeholder="yok",
            help="Örn. 0.70 → endekse çok bağlı fonları ele (çeşitlendirme)",
            key="max_bist_korr",
        )
        hesapla_bist = y3.checkbox(
            "BIST korelasyonunu hesapla",
            value=False if senaryo.startswith("Geniş") else True,
            help="Tarama sonrası her aday için BIST100 korelasyonu (biraz sürer). Geniş taramada varsayılan kapalı.",
        )

        skor_sistemi = st.radio(
            "Skor sistemi",
            ["gelismis", "klasik"],
            format_func=lambda x: "Gelişmiş (percentile + akran)" if x == "gelismis" else "Klasik",
            horizontal=True,
        )
        skor_agirlik = None
        if skor_sistemi == "gelismis":
            gp_default = sc.get("gp", "2")
            gp_keys = list(core.GELISMIS_PROFILLER.keys())
            gp = st.selectbox(
                "Gelişmiş skor profili",
                gp_keys,
                format_func=lambda k: core.GELISMIS_PROFILLER[k][0],
                index=gp_keys.index(gp_default) if gp_default in gp_keys else 1,
            )
            if gp == "4":
                skor_agirlik = {}
                for b, ac in core.GELISMIS_BILESEN_ACIKLAMA.items():
                    skor_agirlik[b] = st.slider(ac, 0, 100, 15, key=f"ga_{b}")
            else:
                skor_agirlik = core.GELISMIS_PROFILLER[gp][1]

    faiz_f = {"Faizli": "faiz", "Katılım (faizsiz)": "faizsiz"}.get(faiz_sec)

    if st.button("🚀 Taramayı başlat", type="primary", use_container_width=True):
        analiz = get_analiz()
        # Daha geniş havuz çek, kalite kapısını sonra uygula
        # Daha geniş havuz: max_fon'un en az 2 katı, max_aday kadar, üst sınır 250
        cek_fon = min(max(max_fon * 2, max_fon + 20, max_aday), 250)
        with st.spinner("Fonlar taranıyor (API + cache)… Birkaç dakika sürebilir."):
            try:
                df = analiz.tara(
                    hedef,
                    min_1y=min_1y,
                    min_sharpe=min_sharpe,
                    max_vol=max_vol,
                    max_dd=max_dd,
                    max_fon=cek_fon,
                    tolerans=tolerans,
                    max_aday=max_aday,
                    kurucu_filtre=kurucu or None,
                    faiz_filtre=faiz_f,
                    min_risk=int(min_risk) if min_risk else None,
                    max_risk=int(max_risk) if max_risk else None,
                    tur_filtre=tur_filtre,
                    skor_sistemi=skor_sistemi,
                    skor_agirlik=skor_agirlik,
                )
                st.session_state.son_tarama = df
                try:
                    core.save_tarama_snapshot(df, meta={
                        "kaynak": "tarama", "risk_paketi": paket.get("ad"), "senaryo": senaryo,
                    })
                except Exception:
                    pass
                st.session_state.son_tarama_meta = {
                    "hedef": str(hedef), "risk_paketi": paket["ad"],
                    "skor": skor_sistemi, "senaryo": senaryo,
                    "max_risk_skor": max_risk_skor, "min_liq": min_liq,
                    "pozitif_donem": pozitif_donem, "min_reel": min_reel,
                    "max_fon": max_fon,
                    "min_kazanma": min_kazanma if zorunlu_kazanma else None,
                    "min_en_kotu": min_en_kotu,
                    "min_sortino": min_sortino,
                    "max_var95": max_var95,
                    "max_cvar95": max_cvar95,
                    "min_cagr": min_cagr,
                    "min_en_iyi": min_en_iyi,
                    "max_toplam_kom": max_toplam_kom,
                    "min_buyukluk_mn": min_buyukluk_mn,
                    "min_yatirimci": min_yatirimci,
                    "max_valor": max_valor,
                    "min_calmar": min_calmar,
                    "min_3y": min_3y,
                    "min_6a": min_6a,
                    "min_vs_mev": min_vs_mev,
                    "max_mesafe": max_mesafe,
                    "max_jump": max_jump,
                    "sadece_katilim": sadece_katilim,
                    "kategori_filtre": kategori_filtre or None,
                    "kategori_gruplari": kategori_gruplari or None,
                    "max_bist_korr": max_bist_korr,
                    "hesapla_bist": hesapla_bist,
                }
            except Exception as e:
                st.error(f"Tarama hatası: {e}")
                st.session_state.son_tarama = pd.DataFrame()

    df_raw = st.session_state.son_tarama
    meta = st.session_state.get("son_tarama_meta") or {}

    def _kalite_filtre(df_in: pd.DataFrame) -> pd.DataFrame:
        if df_in is None or df_in.empty:
            return pd.DataFrame()
        w = df_in.copy()
        mrs = meta.get("max_risk_skor", max_risk_skor)
        ml = meta.get("min_liq", min_liq)
        if "risk_skor" in w.columns:
            w = w[pd.to_numeric(w["risk_skor"], errors="coerce").fillna(999) <= mrs]
        if "likidite_skor" in w.columns:
            w = w[pd.to_numeric(w["likidite_skor"], errors="coerce").fillna(0) >= ml]
        if meta.get("pozitif_donem", pozitif_donem):
            for col in ("getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y"):
                if col in w.columns:
                    w = w[pd.to_numeric(w[col], errors="coerce").fillna(-1) >= 0]
        if meta.get("min_reel", min_reel) and "reel_1y" in w.columns:
            w = w[pd.to_numeric(w["reel_1y"], errors="coerce").fillna(-999) > 0]
        # Kazanma oranı
        mkaz = meta.get("min_kazanma")
        if mkaz is not None and "kazanma_orani" in w.columns:
            w = w[pd.to_numeric(w["kazanma_orani"], errors="coerce").fillna(0) >= float(mkaz)]
        # En kötü gün (alt sınır: -5 → -8'likleri ele)
        mek = meta.get("min_en_kotu")
        if mek is not None and "en_kotu_gun" in w.columns:
            w = w[pd.to_numeric(w["en_kotu_gun"], errors="coerce").fillna(-999) >= float(mek)]
        # Sortino
        mso = meta.get("min_sortino")
        if mso is not None and "sortino" in w.columns:
            w = w[pd.to_numeric(w["sortino"], errors="coerce").fillna(-999) >= float(mso)]
        # VaR95: değer negatif; |VaR| <= max_var95 → var95 >= -max_var95
        mvar = meta.get("max_var95")
        if mvar is not None and "var95" in w.columns:
            w = w[pd.to_numeric(w["var95"], errors="coerce").fillna(-999) >= -abs(float(mvar))]
        # cVaR95
        mcvar = meta.get("max_cvar95")
        if mcvar is not None and "cvar95" in w.columns:
            w = w[pd.to_numeric(w["cvar95"], errors="coerce").fillna(-999) >= -abs(float(mcvar))]
        # CAGR
        mcagr = meta.get("min_cagr")
        if mcagr is not None and "cagr" in w.columns:
            w = w[pd.to_numeric(w["cagr"], errors="coerce").fillna(-999) >= float(mcagr)]
        # En iyi gün (opsiyonel tavan değil taban)
        mei = meta.get("min_en_iyi")
        if mei is not None and "en_iyi_gun" in w.columns:
            w = w[pd.to_numeric(w["en_iyi_gun"], errors="coerce").fillna(-999) >= float(mei)]
        # Komisyon toplamı
        mkom = meta.get("max_toplam_kom")
        if mkom is not None:
            g = pd.to_numeric(w.get("giris_kom"), errors="coerce").fillna(0) if "giris_kom" in w.columns else 0
            c = pd.to_numeric(w.get("cikis_kom"), errors="coerce").fillna(0) if "cikis_kom" in w.columns else 0
            try:
                w = w[(g + c) <= float(mkom)]
            except Exception:
                pass
        # Büyüklük (Mn TL)
        mbuy = meta.get("min_buyukluk_mn")
        if mbuy is not None and "buyukluk" in w.columns:
            w = w[pd.to_numeric(w["buyukluk"], errors="coerce").fillna(0) >= float(mbuy) * 1_000_000]
        # Yatırımcı
        myat = meta.get("min_yatirimci")
        if myat is not None and "yatirimci" in w.columns:
            w = w[pd.to_numeric(w["yatirimci"], errors="coerce").fillna(0) >= float(myat)]
        # Valör
        mval = meta.get("max_valor")
        if mval is not None and "valor_geri_alis" in w.columns:
            vv = pd.to_numeric(w["valor_geri_alis"], errors="coerce")
            w = w[vv.isna() | (vv <= float(mval))]
        # Calmar
        mcal = meta.get("min_calmar")
        if mcal is not None and "calmar" in w.columns:
            w = w[pd.to_numeric(w["calmar"], errors="coerce").fillna(-999) >= float(mcal)]
        # 3Y / 6A
        m3 = meta.get("min_3y")
        if m3 is not None and "getiri_3y" in w.columns:
            w = w[pd.to_numeric(w["getiri_3y"], errors="coerce").fillna(-999) >= float(m3)]
        m6 = meta.get("min_6a")
        if m6 is not None and "getiri_6a" in w.columns:
            w = w[pd.to_numeric(w["getiri_6a"], errors="coerce").fillna(-999) >= float(m6)]
        # vs mevduat
        mvm = meta.get("min_vs_mev")
        if mvm is not None and "vs_mevduat" in w.columns:
            w = w[pd.to_numeric(w["vs_mevduat"], errors="coerce").fillna(-999) >= float(mvm)]
        # dağılım sapması
        mmes = meta.get("max_mesafe")
        if mmes is not None and "mesafe_skoru" in w.columns:
            w = w[pd.to_numeric(w["mesafe_skoru"], errors="coerce").fillna(999) <= float(mmes)]
        # sıçrama
        mjump = meta.get("max_jump")
        if mjump is not None and "jump_sayisi" in w.columns:
            w = w[pd.to_numeric(w["jump_sayisi"], errors="coerce").fillna(999) <= float(mjump)]
        # katılım
        if meta.get("sadece_katilim"):
            if "faiz" in w.columns:
                fl = w["faiz"].astype(str).str.lower()
                w = w[fl.str.contains("katılım|katilim|faizsiz", na=False)]
            elif "ad" in w.columns:
                adl = w["ad"].astype(str).str.lower()
                w = w[adl.str.contains("katılım|katilim", na=False)]
        # kategori metin
        kfil = meta.get("kategori_filtre")
        if kfil and "kategori" in w.columns:
            nt = core._normalize_tr(kfil).lower()
            w = w[w["kategori"].astype(str).apply(
                lambda x: nt in core._normalize_tr(x).lower()
            )]
        # kategori grubu (çoklu)
        kgr = meta.get("kategori_gruplari")
        if kgr and "kategori" in w.columns:
            w = w[kategori_maske(w["kategori"], kgr, None)]
        # BIST korelasyon üst sınırı (sütun varsa)
        mbk = meta.get("max_bist_korr")
        if mbk is not None and "bist_korelasyon" in w.columns:
            bk = pd.to_numeric(w["bist_korelasyon"], errors="coerce")
            w = w[bk.isna() | (bk <= float(mbk))]
        # Rapor uyumlu skor ile yeniden sırala
        if not w.empty:
            w = w.copy()
            w["secim_skor"] = w.apply(core.rapor_uyumlu_skor, axis=1)
            sort_cols = [c for c in ["secim_skor", "skor", "sharpe", "getiri_1y"] if c in w.columns]
            w = w.sort_values(sort_cols, ascending=[False] * len(sort_cols))
            n = int(meta.get("max_fon", max_fon) or 20)
            w = w.head(n).reset_index(drop=True)
            if "sira" in w.columns:
                w = w.drop(columns=["sira"])
            w.insert(0, "sira", range(1, len(w) + 1))
        return w

    if df_raw is not None and not df_raw.empty:
        # BIST korelasyonu (kalite öncesi geniş havuzda, sonra filtre)
        if meta.get("hesapla_bist", True) and "bist_korelasyon" not in df_raw.columns:
            analiz_tmp = get_analiz()
            with st.spinner("BIST100 korelasyonları hesaplanıyor…"):
                try:
                    df_raw = core.df_bist_korelasyon_ekle(analiz_tmp, df_raw)
                    st.session_state.son_tarama = df_raw
                except Exception as e:
                    st.warning(f"BIST korelasyonu hesaplanamadı: {e}")

        df = _kalite_filtre(df_raw)
        elenen = len(df_raw) - len(df)
        st.success(f"Ham aday: **{len(df_raw)}** · Kalite sonrası: **{len(df)}**" + (f" · Elenen: {elenen}" if elenen else ""))

        if df.empty:
            st.warning("Kalite kapıları tüm adayları eledi — eşikleri gevşetin veya senaryoyu değiştirin.")
        else:
            # Özet metrikler
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Ort. 1Y %", fmt_num(df["getiri_1y"].mean() if "getiri_1y" in df else None))
            m2.metric("Ort. Sharpe", fmt_num(df["sharpe"].mean() if "sharpe" in df else None, 2))
            m3.metric("Ort. MaxDD %", fmt_num(df["max_drawdown"].mean() if "max_drawdown" in df else None))
            m4.metric("Ort. Likidite", fmt_num(df["likidite_skor"].mean() if "likidite_skor" in df else None, 0))
            m5.metric(
                "Ort. BIST korr.",
                fmt_num(df["bist_korelasyon"].mean() if "bist_korelasyon" in df else None, 2),
            )

            # ── Akıllı öneri (top 5) ──────────────────────────────────────
            st.subheader("🏆 Akıllı öneri (en iyi 5)")
            top5 = df.head(5)
            for _, row in top5.iterrows():
                nedenler = []
                if row.get("sharpe") is not None and float(row["sharpe"]) >= 1:
                    nedenler.append(f"Sharpe {float(row['sharpe']):.2f}")
                if row.get("reel_1y") is not None and float(row["reel_1y"]) > 0:
                    nedenler.append(f"reel +{float(row['reel_1y']):.1f}%")
                if row.get("likidite_seviye") == "yüksek":
                    nedenler.append("yüksek likidite")
                if row.get("max_drawdown") is not None and float(row["max_drawdown"]) >= -10:
                    nedenler.append(f"sınırlı DD {float(row['max_drawdown']):.1f}%")
                if row.get("vs_mevduat") is not None and float(row["vs_mevduat"]) > 0:
                    nedenler.append(f"mevduat üstü +{float(row['vs_mevduat']):.1f}")
                if row.get("risk_skor") is not None and float(row["risk_skor"]) < 10:
                    nedenler.append("düşük anomali")
                if row.get("bist_korelasyon") is not None:
                    try:
                        nedenler.append(f"BIST ρ={float(row['bist_korelasyon']):.2f}")
                    except (TypeError, ValueError):
                        pass
                if row.get("kategori"):
                    nedenler.append(str(row.get("kategori"))[:28])
                neden_txt = " · ".join(nedenler) if nedenler else "genel skor yüksek"
                with st.container():
                    cL, cR = st.columns([4, 1])
                    cL.markdown(
                        f"**{int(row.get('sira', 0))}. `{row.get('kod')}`** — {str(row.get('ad') or '')[:55]}  \n"
                        f"1Y **{fmt_num(row.get('getiri_1y'))}%** · Sharpe **{fmt_num(row.get('sharpe'), 2)}** · "
                        f"MaxDD **{fmt_num(row.get('max_drawdown'))}%** · Likidite **{fmt_num(row.get('likidite_skor'), 0)}** · "
                        f"Seçim skoru **{fmt_num(row.get('secim_skor'), 1)}**  \n"
                        f"<span style='color:#94a3b8;font-size:0.85rem'>Neden: {neden_txt}</span>",
                        unsafe_allow_html=True,
                    )
                    bayrak = str(row.get("risk_bayrak") or "")
                    if bayrak and bayrak not in ("", "nan"):
                        cR.caption(f"⚠ {bayrak[:60]}")

            # ── Akıllı 10 fon portföy ────────────────────────────────────
            st.subheader("🎯 Akıllı portföy (en iyi 10 fon)")
            st.caption(
                "Seçim skoru + tür çeşitliliği (aynı baskın türden en fazla 3) + "
                "opsiyonel düşük BIST korelasyonu → Max Sharpe / Risk Parity ağırlıkları."
            )
            with st.expander("📖 Akıllı seçim kriterleri — detay", expanded=False):
                st.markdown(core.AKILLI_SECIM_ACIKLAMA)
            ap1, ap2, ap3, ap4 = st.columns(4)
            n_akilli = ap1.slider("Fon sayısı", 5, 15, 10, key="n_akilli_port")
            ym_akilli = ap2.selectbox(
                "Ağırlık yöntemi",
                ["max_sharpe", "risk_parity", "score", "equal"],
                format_func=lambda x: {
                    "max_sharpe": "Max Sharpe", "risk_parity": "Risk Parity",
                    "score": "Skor orantılı", "equal": "Eşit",
                }[x],
                key="ym_akilli",
            )
            max_w_ak = ap3.slider("Tek fon max %", 10, 40, 20, key="maxw_akilli") / 100.0
            max_tur = ap4.slider("Aynı türden max fon", 1, 5, 3, key="max_tur_akilli")
            bt1, bt2 = st.columns(2)
            with bt1:
                btn_a10 = st.button("🚀 En iyi 10 fonluk portföyü oluştur", type="primary", key="btn_akilli10")
            with bt2:
                btn_kar_t = st.button("🔀 Karışık (Muhafazakâr+Dengeli+Agresif)", key="btn_karisik_tar")

            if btn_kar_t:
                kc1, kc2, kc3 = st.columns(3)
                n_muh = kc1.slider("Muhafazakâr", 1, 6, 3, key="n_muh_tar")
                n_den = kc2.slider("Dengeli", 1, 6, 4, key="n_den_tar")
                n_agr = kc3.slider("Agresif", 1, 6, 3, key="n_agr_tar")
                analiz = get_analiz()
                with st.spinner("Karışık portföy hesaplanıyor…"):
                    sonuc = core.akilli_karisik_portfoy(
                        analiz, df,
                        n_muhafazakar=n_muh, n_dengeli=n_den, n_agresif=n_agr,
                        yontem=ym_akilli, max_w=max_w_ak,
                    )
                if sonuc.get("error"):
                    st.error(sonuc["error"])
                else:
                    st.session_state.akilli_portfoy = sonuc

            if btn_a10:
                analiz = get_analiz()
                with st.spinner("Akıllı portföy hesaplanıyor…"):
                    sonuc = core.akilli_portfoy_sec(
                        analiz, df,
                        n_fon=n_akilli,
                        yontem=ym_akilli,
                        max_w=max_w_ak,
                        max_ayni_tur=max_tur,
                        max_bist_korr=meta.get("max_bist_korr"),
                        diversify=True,
                    )
                if sonuc.get("error"):
                    st.error(sonuc["error"])
                else:
                    st.session_state.akilli_portfoy = sonuc
            sonuc = st.session_state.get("akilli_portfoy")
            if sonuc and not sonuc.get("error") and sonuc.get("secili") is not None:
                secili = sonuc["secili"]
                st.success(
                    f"**{sonuc.get('n')} fon** seçildi · yöntem: `{sonuc.get('method')}` · "
                    f"tür dağılımı: {sonuc.get('tur_dagilim')}"
                )
                if sonuc.get("port_sharpe") is not None:
                    p1, p2, p3 = st.columns(3)
                    p1.metric("Beklenen μ", f"%{sonuc.get('port_mu')}")
                    p2.metric("Vol", f"%{sonuc.get('port_vol')}")
                    p3.metric("Sharpe", f"{sonuc.get('port_sharpe')}")
                if sonuc.get("ozet"):
                    with st.expander("Neden bu fonlar? (seçim gerekçeleri)", expanded=True):
                        for line in sonuc["ozet"]:
                            st.markdown(f"- {line}")
                wmap = sonuc.get("weights") or {}
                show_cols = [
                    c for c in [
                        "kod", "ad", "kategori", "baskin_tur", "akilli_skor",
                        "temel_skor", "bonus_kazanma", "bonus_en_kotu_gun",
                        "bonus_likidite", "bonus_bist", "ceza_anomali",
                        "getiri_1y", "sharpe", "sortino", "max_drawdown",
                        "bist_korelasyon", "likidite_skor", "risk_skor", "secim_gerekce",
                    ] if c in secili.columns
                ]
                view_p = secili[show_cols].copy() if show_cols else secili.copy()
                view_p["agirlik_%"] = view_p["kod"].astype(str).map(
                    lambda k: round(float(wmap.get(k, 0)) * 100, 1)
                )
                show_df(view_p, list(view_p.columns), height=400)
                with st.expander("Skor kırılımı (her fon)"):
                    for _, r in secili.iterrows():
                        st.caption(
                            f"**{r.get('kod')}** akilli={r.get('akilli_skor')} = "
                            f"temel {r.get('temel_skor')} + kazanma {r.get('bonus_kazanma')} + "
                            f"kötü gün {r.get('bonus_en_kotu_gun')} + likidite {r.get('bonus_likidite')} + "
                            f"BIST {r.get('bonus_bist')} + anomali {r.get('ceza_anomali')}"
                        )
                edf = secili.copy()
                edf["portfoy_agirlik_%"] = edf["kod"].astype(str).map(
                    lambda k: round(float(wmap.get(k, 0)) * 100, 2)
                )
                download_excel_button(
                    edf,
                    f"tefas_akilli_portfoy10_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    meta={
                        "mod": "akilli_portfoy",
                        "n": sonuc.get("n"),
                        "yontem": sonuc.get("method"),
                        "agirliklar": wmap,
                        "tur_dagilim": sonuc.get("tur_dagilim"),
                    },
                    key="xl_akilli10",
                )
                fa, fb = st.columns(2)
                with fa:
                    if st.button("⭐ Favorilere ekle", key="fav_akilli10"):
                        kodlar = secili["kod"].astype(str).tolist()
                        favs = list(set(core.load_favs()) | set(kodlar))
                        core.save_favs(favs)
                        st.success(f"{len(kodlar)} favori")
                with fb:
                    if st.button("🛒 Portföy sepetine ekle", key="sepet_akilli10"):
                        ad_map = {str(r["kod"]): str(r.get("ad") or r["kod"]) for _, r in secili.iterrows()}
                        n = sepet_ekle(secili["kod"].astype(str).tolist(), ad_map=ad_map, kaynak="tarama_akilli")
                        st.success(f"{n} fon sepete (toplam {len(st.session_state.portfoy_sepet)})")

            # ── Canlı filtre (sonuç üzerinde) ─────────────────────────────
            st.subheader("📋 Sonuç listesi")
            with st.expander("🔎 Liste üzerinde ek filtre", expanded=True):
                f1, f2, f3, f4 = st.columns(4)
                live_min_1y = f1.number_input("Min 1Y %", value=None, step=1.0, format="%.1f",
                                              placeholder="yok", key="live_min_1y")
                live_min_1a = f2.number_input("Min 1A %", value=None, step=0.5, format="%.1f",
                                              placeholder="yok", key="live_min_1a",
                                              help="Son 1 ay — kısa vade kazanan")
                live_min_3a = f3.number_input("Min 3A %", value=None, step=1.0, format="%.1f",
                                              placeholder="yok", key="live_min_3a")
                live_min_sh = f4.number_input("Min Sharpe", value=None, step=0.1, format="%.2f",
                                              placeholder="yok", key="live_min_sh")
                f5, f6, f7, f8 = st.columns(4)
                live_max_dd = f5.number_input("Max DD (örn -15)", value=None, step=1.0, format="%.1f",
                                              placeholder="yok", key="live_max_dd")
                live_min_liq = f6.number_input("Min likidite", value=None, step=5.0, format="%.0f",
                                               placeholder="yok", key="live_min_liq")
                live_max_vol = f7.number_input("Max vol %", value=None, step=1.0, format="%.1f",
                                               placeholder="yok", key="live_max_vol")
                live_max_rs = f8.number_input("Max risk_skor", value=None, step=1.0, format="%.1f",
                                              placeholder="yok", key="live_max_rs")
                f9, f10, f11, f12 = st.columns(4)
                live_min_kaz = f9.number_input("Min kazanma %", value=None, step=1.0, format="%.0f",
                                               placeholder="yok", key="live_min_kaz",
                                               help="Günlük pozitif gün oranı")
                live_min_ekotu = f10.number_input("En kötü gün ≥ %", value=None, step=0.5, format="%.1f",
                                                  placeholder="yok", key="live_min_ekotu",
                                                  help="Örn. -5 → tek günde %5'ten fazla düşenleri ele")
                live_min_so = f11.number_input("Min Sortino", value=None, step=0.1, format="%.2f",
                                               placeholder="yok", key="live_min_so")
                live_max_var = f12.number_input("Max |VaR95|", value=None, step=0.5, format="%.1f",
                                                placeholder="yok", key="live_max_var",
                                                help="3 yazarsanız VaR ≤ -3% olanlar elenir")
                f13a, f13b = st.columns(2)
                live_max_cvar = f13a.number_input("Max |cVaR95|", value=None, step=0.5, format="%.1f",
                                                  placeholder="yok", key="live_max_cvar")
                live_min_cagr = f13b.number_input("Min CAGR %", value=None, step=1.0, format="%.1f",
                                                  placeholder="yok", key="live_min_cagr")
                f13, f14, f15, f16 = st.columns(4)
                live_min_calmar = f13.number_input("Min Calmar", value=None, step=0.1, format="%.2f",
                                                   placeholder="yok", key="live_min_calmar")
                live_min_3y = f14.number_input("Min 3Y %", value=None, step=1.0, format="%.1f",
                                               placeholder="yok", key="live_min_3y")
                live_min_buy = f15.number_input("Min büyüklük Mn", value=None, step=10.0, format="%.0f",
                                                placeholder="yok", key="live_min_buy")
                live_max_kom = f16.number_input("Max kom. toplam %", value=None, step=0.1, format="%.2f",
                                                placeholder="yok", key="live_max_kom")
                f17, f18, f19 = st.columns(3)
                live_text = f17.text_input("Ad/kod/kurucu içerir", "", key="live_text")
                live_tur = f18.multiselect(
                    "Baskın tür",
                    options=sorted(df["baskin_tur"].dropna().unique().tolist()) if "baskin_tur" in df.columns else [],
                    key="live_tur",
                )
                live_sadece_temiz = f19.checkbox(
                    "Risk bayrağı olanları gizle",
                    value=False, key="live_temiz",
                )
                f20, f21 = st.columns(2)
                live_kat = f20.text_input("Kategori içerir", "", key="live_kat")
                live_kgr, live_ktam = kategori_secici(df, "live_kat_sec")
                live_max_bist = f21.number_input(
                    "Max BIST ρ", value=None, min_value=-1.0, max_value=1.0,
                    step=0.05, format="%.2f", placeholder="yok", key="live_max_bist",
                )

            df_goster = df.copy()
            if live_min_1y is not None and "getiri_1y" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["getiri_1y"], errors="coerce").fillna(-999) >= live_min_1y]
            if live_min_1a is not None and "getiri_1a" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["getiri_1a"], errors="coerce").fillna(-999) >= live_min_1a]
            if live_min_3a is not None and "getiri_3a" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["getiri_3a"], errors="coerce").fillna(-999) >= live_min_3a]
            if live_min_sh is not None and "sharpe" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["sharpe"], errors="coerce").fillna(-999) >= live_min_sh]
            if live_max_dd is not None and "max_drawdown" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["max_drawdown"], errors="coerce").fillna(-999) >= live_max_dd]
            if live_min_liq is not None and "likidite_skor" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["likidite_skor"], errors="coerce").fillna(0) >= live_min_liq]
            if live_max_vol is not None and "volatilite" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["volatilite"], errors="coerce").fillna(999) <= live_max_vol]
            if live_max_rs is not None and "risk_skor" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["risk_skor"], errors="coerce").fillna(999) <= live_max_rs]
            if live_min_kaz is not None and "kazanma_orani" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["kazanma_orani"], errors="coerce").fillna(0) >= live_min_kaz]
            if live_min_ekotu is not None and "en_kotu_gun" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["en_kotu_gun"], errors="coerce").fillna(-999) >= live_min_ekotu]
            if live_min_so is not None and "sortino" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["sortino"], errors="coerce").fillna(-999) >= live_min_so]
            if live_max_var is not None and "var95" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["var95"], errors="coerce").fillna(-999) >= -abs(live_max_var)]
            if live_max_cvar is not None and "cvar95" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["cvar95"], errors="coerce").fillna(-999) >= -abs(live_max_cvar)]
            if live_min_cagr is not None and "cagr" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["cagr"], errors="coerce").fillna(-999) >= live_min_cagr]
            if live_min_calmar is not None and "calmar" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["calmar"], errors="coerce").fillna(-999) >= live_min_calmar]
            if live_min_3y is not None and "getiri_3y" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["getiri_3y"], errors="coerce").fillna(-999) >= live_min_3y]
            if live_min_buy is not None and "buyukluk" in df_goster.columns:
                df_goster = df_goster[pd.to_numeric(df_goster["buyukluk"], errors="coerce").fillna(0) >= live_min_buy * 1_000_000]
            if live_max_kom is not None:
                g = pd.to_numeric(df_goster.get("giris_kom"), errors="coerce").fillna(0) if "giris_kom" in df_goster.columns else 0
                c = pd.to_numeric(df_goster.get("cikis_kom"), errors="coerce").fillna(0) if "cikis_kom" in df_goster.columns else 0
                try:
                    df_goster = df_goster[(g + c) <= live_max_kom]
                except Exception:
                    pass
            if live_text:
                nt = core._normalize_tr(live_text).lower()
                mask = (
                    df_goster.get("kod", pd.Series(dtype=str)).astype(str).str.upper().str.contains(live_text.upper(), na=False)
                    | df_goster.get("ad", pd.Series(dtype=str)).astype(str).apply(lambda x: nt in core._normalize_tr(x).lower())
                    | df_goster.get("kurucu", pd.Series(dtype=str)).astype(str).apply(lambda x: nt in core._normalize_tr(x).lower())
                )
                df_goster = df_goster[mask]
            if live_tur and "baskin_tur" in df_goster.columns:
                df_goster = df_goster[df_goster["baskin_tur"].isin(live_tur)]
            if live_sadece_temiz and "risk_bayrak" in df_goster.columns:
                rb = df_goster["risk_bayrak"].astype(str).str.lower()
                df_goster = df_goster[~rb.str.contains("aşırı|akran-dışı|sıçrama|yüksek fiyat", na=False)]
            if (live_kgr or live_ktam) and "kategori" in df_goster.columns:
                df_goster = df_goster[kategori_maske(df_goster["kategori"], live_kgr, live_ktam)]
            if live_kat and "kategori" in df_goster.columns:
                nt = core._normalize_tr(live_kat).lower()
                df_goster = df_goster[df_goster["kategori"].astype(str).apply(
                    lambda x: nt in core._normalize_tr(x).lower()
                )]
            if live_max_bist is not None and "bist_korelasyon" in df_goster.columns:
                bk = pd.to_numeric(df_goster["bist_korelasyon"], errors="coerce")
                df_goster = df_goster[bk.isna() | (bk <= live_max_bist)]

            df_goster = TE.panel(st, core, get_analiz, df_goster, "live")
            st.caption(f"Gösterilen: **{len(df_goster)}** / {len(df)} fon")
            cols = [
                "sira", "secim_skor", "skor", "kod", "ad",
                "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y", "getiri_3y",
                "reel_1y", "vs_mevduat", "vs_bist100",
                "sharpe", "sortino", "calmar", "cagr", "kazanma_orani",
                "en_kotu_gun", "en_iyi_gun", "var95", "cvar95",
                "volatilite", "max_drawdown",
                "risk_degeri", "risk_skor", "likidite_skor", "likidite_seviye",
                "buyukluk", "yatirimci", "giris_kom", "cikis_kom", "valor_geri_alis",
                "mesafe_skoru", "jump_sayisi", "bist_korelasyon", "kategori",
                "baskin_tur", "kurucu", "risk_bayrak",
            ]
            show_df(df_goster, cols + [c for c in st.session_state.get("live_ek_cols", []) if c not in cols], height=420)

            if skor_sistemi == "gelismis" or "pct_getiri" in df_goster.columns:
                with st.expander("Bileşen percentile'ları (akran grubu içinde 0–100)"):
                    pct_cols = [c for c in [
                        "kod", "pct_getiri", "pct_risk_ayarli", "pct_maxdd",
                        "pct_risk_anomali", "pct_maliyet", "pct_akran_sapma", "akran_grubu",
                    ] if c in df_goster.columns]
                    show_df(df_goster, pct_cols, height=280)

            # ── Seçimden portföy / favori ──────────────────────────────────
            st.subheader("✅ Seçim yap")
            kod_list = df_goster["kod"].astype(str).tolist() if "kod" in df_goster.columns else []
            labels = {
                str(r["kod"]): f"{r['kod']} — 1Y {fmt_num(r.get('getiri_1y'))}% · Sh {fmt_num(r.get('sharpe'), 2)}"
                for _, r in df_goster.iterrows()
            }
            secilen = st.multiselect(
                "Portföye / favoriye eklenecek fonlar",
                options=kod_list,
                default=kod_list[: min(5, len(kod_list))],
                format_func=lambda k: labels.get(k, k),
            )
            # Portföy için secili_df kaynağı
            df = df_goster
            a1, a2, a3 = st.columns(3)
            with a1:
                cfa, cfb = st.columns(2)
                with cfa:
                    if st.button("⭐ Favori") and secilen:
                        favs = list(set(core.load_favs()) | set(secilen))
                        core.save_favs(favs)
                        st.success(f"{len(secilen)} favori")
                with cfb:
                    if st.button("🛒 Portföye ekle", key="sepet_tarama") and secilen:
                        ad_map = {str(r["kod"]): str(r.get("ad") or r["kod"]) for _, r in df_goster.iterrows()}
                        n = sepet_ekle(secilen, ad_map=ad_map, kaynak="tarama")
                        st.success(f"{n} fon sepete eklendi (toplam {len(st.session_state.portfoy_sepet)})")
            with a2:
                yontem_p = st.selectbox(
                    "Portföy ağırlığı",
                    ["max_sharpe", "score", "equal", "risk_parity"],
                    format_func=lambda x: {
                        "max_sharpe": "Max Sharpe", "score": "Seçim skoru orantılı",
                        "equal": "Eşit", "risk_parity": "Risk Parity",
                    }[x],
                    key="tar_port_ym",
                )
            with a3:
                max_w_p = st.slider("Tek fon max %", 10, 100, 35, key="tar_maxw") / 100.0

            if st.button("📁 Seçilenlerden portföy kur", type="primary") and secilen:
                secili_df = df[df["kod"].astype(str).isin(secilen)].copy()
                if "rapor_skor" not in secili_df.columns and "secim_skor" in secili_df.columns:
                    secili_df["rapor_skor"] = secili_df["secim_skor"]
                analiz = get_analiz()
                with st.spinner("Ağırlıklar hesaplanıyor…"):
                    out = core.siralamadan_agirlik_hesapla(
                        analiz, secili_df, yontem=yontem_p, max_w=max_w_p,
                    )
                if out.get("error"):
                    st.error(out["error"])
                else:
                    st.markdown(f"**Ağırlıklar ({out.get('method')})**")
                    wdf = pd.DataFrame([
                        {"kod": k, "ağırlık %": round(v * 100, 1),
                         "risk katkısı %": out.get("risk_contrib_pct", {}).get(k, 0)}
                        for k, v in sorted(out.get("weights", {}).items(), key=lambda x: -x[1])
                        if v >= 0.001
                    ])
                    st.dataframe(wdf, hide_index=True, use_container_width=True)
                    if out.get("port_sharpe") is not None:
                        p1, p2, p3 = st.columns(3)
                        p1.metric("Beklenen μ", f"%{out.get('port_mu')}")
                        p2.metric("Vol", f"%{out.get('port_vol')}")
                        p3.metric("Sharpe", f"{out.get('port_sharpe')}")
                    # Excel
                    edf = secili_df.copy()
                    edf["portfoy_agirlik_%"] = edf["kod"].map(
                        lambda k: round(out.get("weights", {}).get(str(k), 0) * 100, 2)
                    )
                    download_excel_button(
                        edf,
                        f"tefas_tarama_portfoy_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                        meta={"mod": "tarama_portfoy", "yontem": yontem_p, "agirliklar": out.get("weights")},
                        key="xl_tar_port",
                    )
                    st.session_state.son_portfoy = out

            st.divider()
            cdl1, cdl2 = st.columns(2)
            with cdl1:
                download_excel_button(
                    df,
                    f"tefas_tarama_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    meta=meta,
                    key="xl_tarama",
                )
            with cdl2:
                if core.HAS_MPL and st.button("📊 PNG grafik rapor", key="gr_tarama"):
                    path = core.grafik_rapor(df, baslik=f"Tarama — {meta.get('risk_paketi', '')} | {senaryo}")
                    if path:
                        st.image(path)
                        with open(path, "rb") as f:
                            st.download_button(
                                "PNG indir", f, file_name=Path(path).name,
                                mime="image/png", key="dl_png_t",
                            )
    elif df_raw is not None and df_raw.empty:
        st.warning("Sonuç yok — filtreleri gevşetin veya senaryo değiştirin.")


# ═══════════════════════════════════════════════════════════════════════════
# TÜM FON SIRALAMASI
# ═══════════════════════════════════════════════════════════════════════════
elif sayfa == "📋 Tüm Fon Sıralaması":
    st.header("📋 Tüm Fonlar — Dönem Sıralaması")
    st.caption("Dağılım filtresi yok. 1A · 3A · 6A · 1Y · 3Y getiri + risk metrikleri")

    with st.expander("Parametreler", expanded=True):
        c1, c2, c3 = st.columns(3)
        poz = c1.checkbox("Son 1Y her dönem pozitif (1A,3A,6A,1Y)", value=True)
        poz3 = c2.checkbox("3Y de pozitif olsun", value=False)
        max_liste = c3.slider("Kaç fon listelensin", 10, 200, 40)
        lim = st.number_input(
            "En fazla kaç açık fon taransın? (0 = hepsi)",
            value=80, min_value=0, max_value=5000, step=10,
            help="İlk denemede 80–150 önerilir; tüm fonlar 10–40 dk sürebilir.",
        )
        max_tara = int(lim) if lim else None

        tur_sec = st.multiselect(
            "Fon türü filtresi",
            options=list(core.TUR_ACIKLAMA.keys()),
            format_func=lambda k: core.TUR_ACIKLAMA[k],
        )
        tur_filtre = tur_sec or None

        skor_sistemi = st.radio(
            "Skor sistemi", ["gelismis", "klasik"],
            format_func=lambda x: "Gelişmiş" if x == "gelismis" else "Klasik",
            horizontal=True, key="sira_skor",
        )
        skor_agirlik = core.GELISMIS_PROFILLER["2"][1] if skor_sistemi == "gelismis" else None
        if skor_sistemi == "gelismis":
            gp = st.selectbox(
                "Gelişmiş profil",
                list(core.GELISMIS_PROFILLER.keys()),
                format_func=lambda k: core.GELISMIS_PROFILLER[k][0],
                index=1, key="sira_gp",
            )
            if gp != "4":
                skor_agirlik = core.GELISMIS_PROFILLER[gp][1]

    st.info("⏳ Tüm fon taraması uzun sürebilir. Cache sonraki koşuları hızlandırır.")

    if st.button("🚀 Sıralamayı başlat", type="primary", use_container_width=True):
        analiz = get_analiz()
        with st.spinner("Açık fonlar taranıyor…"):
            try:
                df = analiz.tum_fon_siralama(
                    sadece_pozitif_1y=poz,
                    pozitif_3y=poz3,
                    max_liste=max_liste,
                    max_tara=max_tara,
                    tur_filtre=tur_filtre,
                    skor_sistemi=skor_sistemi,
                    skor_agirlik=skor_agirlik,
                )
                st.session_state.son_siralama = df
                try:
                    core.save_tarama_snapshot(df, meta={"kaynak": "siralama"})
                except Exception:
                    pass
            except Exception as e:
                st.error(f"Hata: {e}")
                df = pd.DataFrame()

    df = st.session_state.son_siralama
    if df is not None and not df.empty:
        # Akıllı skor + tutarlılık skoru ekle
        work = df.copy()
        if "secim_skor" not in work.columns:
            work["secim_skor"] = work.apply(core.rapor_uyumlu_skor, axis=1)
        if "tutarlilik_skor" not in work.columns:
            work = core.df_tutarlilik_skoru_ekle(work)

        st.success(f"{len(work)} fon listelendi")

        # ── Akıllı seçim kriterleri ──────────────────────────────────────
        st.subheader("🎯 Akıllı seçim kriterleri")
        with st.expander("📖 Kriterler nasıl çalışır? (detaylı açıklama)", expanded=False):
            st.markdown(core.AKILLI_SECIM_ACIKLAMA)

        preset_keys = list(core.AKILLI_FILTRE_PRESET.keys())
        preset_sec = st.radio(
            "Akıllı filtre profili",
            preset_keys,
            index=preset_keys.index("Dengeli") if "Dengeli" in preset_keys else 0,
            horizontal=True,
            format_func=lambda k: core.AKILLI_FILTRE_PRESET[k]["ad"].split("—")[0].strip(),
            key="sira_akilli_preset",
            help="Muhafazakâr / Dengeli / Agresif… tüm eşikleri otomatik doldurur",
        )
        pr = core.AKILLI_FILTRE_PRESET[preset_sec]
        st.caption(pr.get("ad", preset_sec))
        pk = preset_sec.replace(" ", "_").replace("/", "_")

        with st.expander("Kalite & risk filtreleri (profil eşikleri — ince ayar yapabilirsiniz)", expanded=(preset_sec == "Özel")):
            s1, s2, s3, s4 = st.columns(4)
            s_max_rs = s1.number_input("Max risk_skor", value=float(pr.get("max_risk_skor") or 25), step=1.0,
                                       key=f"sira_max_rs_{pk}", help="Anomali / ani sıçrama skoru üst sınırı")
            s_min_liq = s2.number_input("Min likidite", value=float(pr.get("min_liq") or 20), step=5.0,
                                        key=f"sira_min_liq_{pk}")
            _mkaz = pr.get("min_kazanma")
            s_min_kaz = s3.number_input(
                "Min kazanma %",
                value=float(_mkaz) if _mkaz is not None else None,
                step=1.0, placeholder="yok", key=f"sira_min_kaz_{pk}",
            )
            _mek = pr.get("min_en_kotu")
            s_min_ekotu = s4.number_input(
                "En kötü gün ≥ %",
                value=float(_mek) if _mek is not None else None,
                step=0.5, format="%.1f", placeholder="yok", key=f"sira_min_ekotu_{pk}",
            )
            s5, s6, s7, s8 = st.columns(4)
            _msh = pr.get("min_sharpe")
            s_min_sh = s5.number_input("Min Sharpe", value=float(_msh) if _msh is not None else None,
                                       step=0.1, format="%.2f", placeholder="yok", key=f"sira_min_sh_{pk}")
            _mso = pr.get("min_sortino")
            s_min_so = s6.number_input("Min Sortino", value=float(_mso) if _mso is not None else None,
                                       step=0.1, format="%.2f", placeholder="yok", key=f"sira_min_so_{pk}")
            s_max_dd = s7.number_input("Max DD %", value=float(pr.get("max_dd") if pr.get("max_dd") is not None else -40),
                                       step=1.0, key=f"sira_max_dd_{pk}")
            s_max_vol = s8.number_input("Max vol %", value=float(pr.get("max_vol") if pr.get("max_vol") is not None else 50),
                                        step=1.0, key=f"sira_max_vol_{pk}")
            s9, s10, s11, s12 = st.columns(4)
            _mvar = pr.get("max_var95")
            s_max_var = s9.number_input(
                "Max |VaR95|",
                value=float(_mvar) if _mvar is not None else None,
                step=0.5, placeholder="yok", key=f"sira_max_var_{pk}",
            )
            _mcvar = pr.get("max_cvar95")
            s_max_cvar = s10.number_input(
                "Max |cVaR95|",
                value=float(_mcvar) if _mcvar is not None else None,
                step=0.5, placeholder="yok", key=f"sira_max_cvar_{pk}",
            )
            _mcagr = pr.get("min_cagr")
            s_min_cagr = s11.number_input("Min CAGR %", value=float(_mcagr) if _mcagr is not None else None,
                                          step=1.0, format="%.1f", placeholder="yok", key=f"sira_min_cagr_{pk}")
            _mcal = pr.get("min_calmar")
            s_min_calmar = s12.number_input("Min Calmar", value=float(_mcal) if _mcal is not None else None,
                                            step=0.1, format="%.2f", placeholder="yok", key=f"sira_min_cal_{pk}")
            s13, s14, s15, s16 = st.columns(4)
            s_kategori = s13.text_input("Kategori içerir", "", key=f"sira_kat_{pk}")
            _mbist = pr.get("max_bist_korr")
            s_max_bist = s14.number_input(
                "Max BIST ρ",
                value=float(_mbist) if _mbist is not None else None,
                min_value=-1.0, max_value=1.0, step=0.05, format="%.2f",
                placeholder="yok", key=f"sira_max_bist_{pk}",
            )
            _rejim_preset = preset_sec in (
                "BIST düşünce artan", "Kriz dayanıklı", "Asimetrik (yükselir-korur)",
            ) or (pr.get("sirala") or "") in (
                "dususte_artan_skor", "kriz_dayanik_skor", "asimetri_skor", "bist_korelasyon_asc",
            )
            s_hesapla_bist = s15.checkbox(
                "BIST / rejim metrikleri hesapla",
                value=(_mbist is not None) or _rejim_preset or ("BIST" in preset_sec),
                key=f"sira_bist_hesap_{pk}",
                help="Korelasyon + upside/downside capture + kriz pencereleri. Biraz sürer.",
            )
            s_temiz = s16.checkbox(
                "Risk bayrağı olanları gizle",
                value=bool(pr.get("temiz_bayrak", True)),
                key=f"sira_temiz_{pk}",
            )
            s_min_reel = st.checkbox(
                "Reel getiri > 0 (TÜFE üstü)",
                value=bool(pr.get("min_reel", False)),
                key=f"sira_min_reel_{pk}",
            )
            st.markdown("**Kategoriye göre seçim**")
            kc1, kc2 = st.columns(2)
            s_kgr = kc1.multiselect(
                "Kategori grubu", list(KATEGORI_GRUPLARI.keys()), key=f"sira_kgr_{pk}",
                help="Birden fazla seçilebilir; boş = hepsi",
            )
            _vc = (work["kategori"].dropna().astype(str).str.strip().value_counts()
                   if "kategori" in work.columns else pd.Series(dtype=int))
            s_ktam = kc2.multiselect(
                "TEFAS kategorisi (tam ad)", _vc.index.tolist(), key=f"sira_ktam_{pk}",
                format_func=lambda k: f"{k} ({_vc[k]})",
            )
            st.markdown("**Kısa vade & rejim skor eşikleri**")
            kv1, kv2, kv3 = st.columns(3)
            _m1a = pr.get("min_1a")
            _m3a = pr.get("min_3a")
            s_min_1a = kv1.number_input(
                "Min 1A getiri %",
                value=float(_m1a) if _m1a is not None else None,
                step=0.5, format="%.1f", placeholder="yok",
                key=f"sira_min_1a_{pk}",
                help="Son 1 aylık getiri tabanı",
            )
            s_min_3a = kv2.number_input(
                "Min 3A getiri %",
                value=float(_m3a) if _m3a is not None else None,
                step=1.0, format="%.1f", placeholder="yok",
                key=f"sira_min_3a_{pk}",
                help="Son 3 aylık getiri tabanı",
            )
            _sirala_opts = [
                "akilli_skor", "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y",
                "sharpe", "bist_korelasyon", "bist_korelasyon_asc",
                "dususte_artan_skor", "kriz_dayanik_skor", "asimetri_skor",
                "upside_capture", "downside_capture", "capture_ratio", "kriz_getiri",
            ]
            _sirala_def = pr.get("sirala", "akilli_skor")
            if _sirala_def not in _sirala_opts:
                _sirala_opts = [_sirala_def] + _sirala_opts
            s_sirala = kv3.selectbox(
                "Sıralama ölçütü",
                _sirala_opts,
                index=_sirala_opts.index(_sirala_def) if _sirala_def in _sirala_opts else 0,
                format_func=lambda x: {
                    "akilli_skor": "Akıllı skor",
                    "getiri_1a": "1A getiri (kısa vade)",
                    "getiri_3a": "3A getiri",
                    "getiri_6a": "6A getiri",
                    "getiri_1y": "1Y getiri",
                    "sharpe": "Sharpe",
                    "bist_korelasyon": "BIST ρ (yüksek → düşük)",
                    "bist_korelasyon_asc": "BIST düşünce artan (düşük ρ önce)",
                    "dususte_artan_skor": "📉 Düşünce artan skor (0–100)",
                    "kriz_dayanik_skor": "🛡️ Kriz dayanıklılık skoru (0–100)",
                    "asimetri_skor": "⚖️ Asimetri skor (yükselir-korur)",
                    "upside_capture": "Upside capture %",
                    "downside_capture": "Downside capture % (düşük iyi)",
                    "capture_ratio": "Capture ratio (up/down)",
                    "kriz_getiri": "Kriz dönemi getiri %",
                }.get(x, x),
                key=f"sira_sirala_{pk}",
            )
            # Rejim skor min eşikleri (preset'ten)
            rr1, rr2, rr3 = st.columns(3)
            _mda = pr.get("min_dususte_artan")
            s_min_dususte = rr1.number_input(
                "Min düşünce-artan skor",
                value=float(_mda) if _mda is not None else None,
                min_value=0.0, max_value=100.0, step=5.0, placeholder="yok",
                key=f"sira_min_dususte_{pk}",
            )
            _mkd = pr.get("min_kriz_dayanik")
            s_min_kriz = rr2.number_input(
                "Min kriz-dayanıklılık skor",
                value=float(_mkd) if _mkd is not None else None,
                min_value=0.0, max_value=100.0, step=5.0, placeholder="yok",
                key=f"sira_min_kriz_{pk}",
            )
            _mas = pr.get("min_asimetri")
            s_min_asim = rr3.number_input(
                "Min asimetri skor",
                value=float(_mas) if _mas is not None else None,
                min_value=0.0, max_value=100.0, step=5.0, placeholder="yok",
                key=f"sira_min_asim_{pk}",
            )

        # BIST korelasyonu + rejim metrikleri (isteğe bağlı)
        _need_rejim = s_hesapla_bist or _rejim_preset or s_sirala in (
            "dususte_artan_skor", "kriz_dayanik_skor", "asimetri_skor",
            "upside_capture", "downside_capture", "capture_ratio", "kriz_getiri",
        )
        if _need_rejim and "dususte_artan_skor" not in work.columns:
            analiz_tmp = get_analiz()
            with st.spinner("BIST rejim metrikleri hesaplanıyor (korelasyon + kriz + capture)… Birkaç dk sürebilir."):
                try:
                    work = core.df_rejim_metrikleri_ekle(analiz_tmp, work, periyod=36)
                    st.session_state.son_siralama = work
                except Exception as e:
                    st.warning(f"Rejim metrikleri: {e}")
                    # fallback sadece korelasyon
                    if "bist_korelasyon" not in work.columns:
                        try:
                            work = core.df_bist_korelasyon_ekle(analiz_tmp, work)
                            st.session_state.son_siralama = work
                        except Exception as e2:
                            st.warning(f"BIST korelasyonu: {e2}")
        elif s_hesapla_bist and "bist_korelasyon" not in work.columns:
            analiz_tmp = get_analiz()
            with st.spinner("BIST100 korelasyonları hesaplanıyor…"):
                try:
                    work = core.df_bist_korelasyon_ekle(analiz_tmp, work)
                    st.session_state.son_siralama = work
                except Exception as e:
                    st.warning(f"BIST korelasyonu: {e}")

        filt = work.copy()
        if "risk_skor" in filt.columns and s_max_rs is not None:
            filt = filt[pd.to_numeric(filt["risk_skor"], errors="coerce").fillna(999) <= s_max_rs]
        if "likidite_skor" in filt.columns and s_min_liq is not None:
            filt = filt[pd.to_numeric(filt["likidite_skor"], errors="coerce").fillna(0) >= s_min_liq]
        if s_min_kaz is not None and "kazanma_orani" in filt.columns:
            filt = filt[pd.to_numeric(filt["kazanma_orani"], errors="coerce").fillna(0) >= s_min_kaz]
        if s_min_ekotu is not None and "en_kotu_gun" in filt.columns:
            filt = filt[pd.to_numeric(filt["en_kotu_gun"], errors="coerce").fillna(-999) >= s_min_ekotu]
        if s_min_sh is not None and "sharpe" in filt.columns:
            filt = filt[pd.to_numeric(filt["sharpe"], errors="coerce").fillna(-999) >= s_min_sh]
        if s_min_so is not None and "sortino" in filt.columns:
            filt = filt[pd.to_numeric(filt["sortino"], errors="coerce").fillna(-999) >= s_min_so]
        if s_max_dd is not None and "max_drawdown" in filt.columns:
            filt = filt[pd.to_numeric(filt["max_drawdown"], errors="coerce").fillna(-999) >= s_max_dd]
        if s_max_vol is not None and "volatilite" in filt.columns:
            filt = filt[pd.to_numeric(filt["volatilite"], errors="coerce").fillna(999) <= s_max_vol]
        if s_max_var is not None and "var95" in filt.columns:
            filt = filt[pd.to_numeric(filt["var95"], errors="coerce").fillna(-999) >= -abs(s_max_var)]
        if s_max_cvar is not None and "cvar95" in filt.columns:
            filt = filt[pd.to_numeric(filt["cvar95"], errors="coerce").fillna(-999) >= -abs(s_max_cvar)]
        if s_min_cagr is not None and "cagr" in filt.columns:
            filt = filt[pd.to_numeric(filt["cagr"], errors="coerce").fillna(-999) >= s_min_cagr]
        if s_min_calmar is not None and "calmar" in filt.columns:
            filt = filt[pd.to_numeric(filt["calmar"], errors="coerce").fillna(-999) >= s_min_calmar]
        if s_min_reel and "reel_1y" in filt.columns:
            filt = filt[pd.to_numeric(filt["reel_1y"], errors="coerce").fillna(-999) > 0]
        if (s_kgr or s_ktam) and "kategori" in filt.columns:
            filt = filt[kategori_maske(filt["kategori"], s_kgr, s_ktam)]
        if s_kategori and "kategori" in filt.columns:
            nt = core._normalize_tr(s_kategori).lower()
            filt = filt[filt["kategori"].astype(str).apply(lambda x: nt in core._normalize_tr(x).lower())]
        if s_max_bist is not None and "bist_korelasyon" in filt.columns:
            bk = pd.to_numeric(filt["bist_korelasyon"], errors="coerce")
            filt = filt[bk.isna() | (bk <= s_max_bist)]
        if s_temiz and "risk_bayrak" in filt.columns:
            rb = filt["risk_bayrak"].astype(str).str.lower()
            filt = filt[~rb.str.contains("aşırı|akran-dışı|sıçrama|yüksek fiyat", na=False)]
        if s_min_1a is not None and "getiri_1a" in filt.columns:
            filt = filt[pd.to_numeric(filt["getiri_1a"], errors="coerce").fillna(-999) >= s_min_1a]
        if s_min_3a is not None and "getiri_3a" in filt.columns:
            filt = filt[pd.to_numeric(filt["getiri_3a"], errors="coerce").fillna(-999) >= s_min_3a]
        # Rejim skor eşikleri
        if s_min_dususte is not None and "dususte_artan_skor" in filt.columns:
            filt = filt[pd.to_numeric(filt["dususte_artan_skor"], errors="coerce").fillna(0) >= s_min_dususte]
        if s_min_kriz is not None and "kriz_dayanik_skor" in filt.columns:
            filt = filt[pd.to_numeric(filt["kriz_dayanik_skor"], errors="coerce").fillna(0) >= s_min_kriz]
        if s_min_asim is not None and "asimetri_skor" in filt.columns:
            filt = filt[pd.to_numeric(filt["asimetri_skor"], errors="coerce").fillna(0) >= s_min_asim]
        # Ek filtreler (yaş, süreklilik, kategori içi sıra, alfa/beta, yoğunlaşma, akış, valör, maliyet)
        filt = TE.panel(st, core, get_analiz, filt, "sira")
        # Sıralama (kısa vade / skor / BIST / rejim)
        _sort_col = s_sirala
        _asc = False
        if s_sirala == "bist_korelasyon_asc":
            _sort_col = "bist_korelasyon"
            _asc = True  # düşük / negatif ρ önce = BIST düşünce artmaya aday
        elif s_sirala in ("downside_capture", "bist_korelasyon"):
            # düşük downside capture / düşük ρ daha iyi → küçükten büyüğe
            if s_sirala == "downside_capture":
                _asc = True
        if _sort_col in filt.columns and not filt.empty:
            filt = filt.copy()
            filt["_sort"] = pd.to_numeric(filt[_sort_col], errors="coerce")
            filt = filt.sort_values("_sort", ascending=_asc, na_position="last").drop(columns=["_sort"]).reset_index(drop=True)
            if "sira" in filt.columns:
                filt["sira"] = range(1, len(filt) + 1)
            else:
                filt.insert(0, "sira", range(1, len(filt) + 1))

        st.caption(
            f"Akıllı filtre sonrası: **{len(filt)}** / {len(work)} fon"
            + (f" · sıralama: `{s_sirala}`" if s_sirala else "")
        )

        cols = [
            "sira", "secim_skor", "tutarlilik_skor", "tutarlilik_n", "puan", "kod", "ad", "yatirim", "kategori",
            "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y", "getiri_3y",
            "reel_1y", "vs_mevduat", "vs_bist100",
            "sharpe", "sortino", "calmar", "cagr", "kazanma_orani",
            "en_kotu_gun", "en_iyi_gun", "var95", "cvar95",
            "volatilite", "max_drawdown",
            "risk_skor", "likidite_skor", "likidite_seviye",
            "bist_korelasyon", "beta", "beta_up", "beta_down",
            "upside_capture", "downside_capture", "capture_ratio",
            "bist_dusuk_gun_fon", "kriz_getiri",
            "dususte_artan_skor", "kriz_dayanik_skor", "asimetri_skor",
            "baskin_tur", "risk_bayrak",
        ]
        show_df(filt if not filt.empty else work, cols + [c for c in st.session_state.get("sira_ek_cols", []) if c not in cols], height=480)

        download_excel_button(
            filt if not filt.empty else work,
            f"tefas_donem_sira_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            meta={"mod": "tum_fon_siralama", "pozitif_1y": poz, "pozitif_3y": poz3},
            key="xl_sira",
        )

        # ── Akıllı portföy (10 fon) ──────────────────────────────────────
        st.subheader("🎯 Akıllı portföy (sıralamadan en iyi fonlar)")
        st.caption(
            f"Profil: **{preset_sec}** · çok kriterli skor + tür çeşitliliği. "
            "Yukarıdaki filtreler uygulanmış liste üzerinden seçer."
        )
        ap1, ap2, ap3, ap4 = st.columns(4)
        n_akilli_s = ap1.slider("Fon sayısı", 5, 15, int(pr.get("n_fon", 10)), key=f"n_akilli_sira_{pk}")
        _ym_opts = ["max_sharpe", "risk_parity", "score", "equal"]
        _ym_def = pr.get("yontem", "max_sharpe")
        ym_akilli_s = ap2.selectbox(
            "Ağırlık yöntemi",
            _ym_opts,
            index=_ym_opts.index(_ym_def) if _ym_def in _ym_opts else 0,
            format_func=lambda x: {
                "max_sharpe": "Max Sharpe", "risk_parity": "Risk Parity",
                "score": "Skor orantılı", "equal": "Eşit",
            }[x],
            key=f"ym_akilli_sira_{pk}",
        )
        max_w_s = ap3.slider(
            "Tek fon max %", 10, 40, int(round(float(pr.get("max_w", 0.2)) * 100)),
            key=f"maxw_akilli_sira_{pk}",
        ) / 100.0
        max_tur_s = ap4.slider("Aynı türden max fon", 1, 5, int(pr.get("max_ayni_tur", 3)), key=f"max_tur_sira_{pk}")

        b_col1, b_col2, b_col3 = st.columns(3)
        with b_col1:
            btn_tek = st.button("🚀 Akıllı seçim ile portföy oluştur", type="primary", key="btn_akilli_sira")
        with b_col2:
            btn_kar = st.button("🔀 Karışık (Muh+Den+Agr)", key="btn_karisik_sira")
        with b_col3:
            btn_cu = st.button("🏛️ Çekirdek–Uydu portföy", key="btn_cekirdek_uydu")

        if btn_cu:
            kaynak = filt if not filt.empty else work
            if kaynak.empty:
                st.warning("Filtrelenmiş liste boş.")
            else:
                analiz = get_analiz()
                with st.spinner("Çekirdek–uydu portföy hesaplanıyor…"):
                    sonuc = core.cekirdek_uydu_portfoy(
                        analiz, kaynak,
                        n_cekirdek=6, n_uydu=4,
                        cekirdek_agirlik=0.70,
                        yontem=ym_akilli_s,
                        max_w_cekirdek=max_w_s,
                        max_w_uydu=min(0.15, max_w_s),
                        max_ayni_tur=max_tur_s,
                    )
                if sonuc.get("error"):
                    st.error(sonuc["error"])
                else:
                    st.session_state.akilli_portfoy_sira = sonuc

        if btn_kar:
            with st.expander("Karışık portföy ayarları", expanded=True):
                kc1, kc2, kc3 = st.columns(3)
                n_muh = kc1.slider("Muhafazakâr fon", 1, 6, 3, key="n_muh_kar")
                n_den = kc2.slider("Dengeli fon", 1, 6, 4, key="n_den_kar")
                n_agr = kc3.slider("Agresif fon", 1, 6, 3, key="n_agr_kar")
            kaynak = filt if not filt.empty else work
            if kaynak.empty:
                st.warning("Filtrelenmiş liste boş.")
            else:
                # BIST düşünce artan için korelasyon gerekir
                analiz = get_analiz()
                with st.spinner("Karışık portföy (3 profil) hesaplanıyor…"):
                    sonuc = core.akilli_karisik_portfoy(
                        analiz, kaynak,
                        n_muhafazakar=n_muh,
                        n_dengeli=n_den,
                        n_agresif=n_agr,
                        yontem=ym_akilli_s,
                        max_w=max_w_s,
                    )
                if sonuc.get("error"):
                    st.error(sonuc["error"])
                else:
                    st.session_state.akilli_portfoy_sira = sonuc

        if btn_tek:
            kaynak = filt if not filt.empty else work
            if kaynak.empty:
                st.warning("Filtrelenmiş liste boş.")
            else:
                analiz = get_analiz()
                with st.spinner("Akıllı portföy hesaplanıyor…"):
                    sonuc = core.akilli_portfoy_sec(
                        analiz, kaynak,
                        n_fon=n_akilli_s,
                        yontem=ym_akilli_s,
                        max_w=max_w_s,
                        max_ayni_tur=max_tur_s,
                        max_bist_korr=s_max_bist,
                        diversify=True,
                    )
                if sonuc.get("error"):
                    st.error(sonuc["error"])
                else:
                    st.session_state.akilli_portfoy_sira = sonuc

        sonuc = st.session_state.get("akilli_portfoy_sira")
        if sonuc and not sonuc.get("error") and sonuc.get("secili") is not None:
            secili = sonuc["secili"]
            st.success(
                f"**{sonuc.get('n')} fon** seçildi · yöntem: `{sonuc.get('method')}` · "
                f"tür: {sonuc.get('tur_dagilim')}"
            )
            if sonuc.get("port_sharpe") is not None:
                p1, p2, p3 = st.columns(3)
                p1.metric("Beklenen μ", f"%{sonuc.get('port_mu')}")
                p2.metric("Vol", f"%{sonuc.get('port_vol')}")
                p3.metric("Sharpe", f"{sonuc.get('port_sharpe')}")
            if sonuc.get("ozet"):
                with st.expander("Neden bu fonlar?", expanded=True):
                    for line in sonuc["ozet"]:
                        st.markdown(f"- {line}")
            wmap = sonuc.get("weights") or {}
            show_cols = [
                c for c in [
                    "kod", "ad", "kategori", "baskin_tur", "akilli_skor",
                    "temel_skor", "bonus_kazanma", "bonus_en_kotu_gun",
                    "bonus_likidite", "bonus_bist", "ceza_anomali",
                    "getiri_1y", "sharpe", "sortino", "max_drawdown",
                    "bist_korelasyon", "likidite_skor", "profil_kaynak", "secim_gerekce",
                ] if c in secili.columns
            ]
            view_p = secili[show_cols].copy() if show_cols else secili.copy()
            view_p["agirlik_%"] = view_p["kod"].astype(str).map(
                lambda k: round(float(wmap.get(k, 0)) * 100, 1)
            )
            show_df(view_p, list(view_p.columns), height=400)
            with st.expander("Skor kırılımı (her fon)"):
                for _, r in secili.iterrows():
                    st.caption(
                        f"**{r.get('kod')}** akilli={r.get('akilli_skor')} = "
                        f"temel {r.get('temel_skor')} + kazanma {r.get('bonus_kazanma')} + "
                        f"kötü gün {r.get('bonus_en_kotu_gun')} + likidite {r.get('bonus_likidite')} + "
                        f"BIST {r.get('bonus_bist')} + anomali {r.get('ceza_anomali')}"
                    )
            edf = secili.copy()
            edf["portfoy_agirlik_%"] = edf["kod"].astype(str).map(
                lambda k: round(float(wmap.get(k, 0)) * 100, 2)
            )
            download_excel_button(
                edf,
                f"tefas_sira_akilli_portfoy_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                meta={"mod": "siralama_akilli_portfoy", "yontem": sonuc.get("method"), "agirliklar": wmap},
                key="xl_sira_akilli",
            )
            fc1, fc2 = st.columns(2)
            with fc1:
                if st.button("⭐ Favorilere ekle", key="fav_sira_akilli"):
                    kodlar = secili["kod"].astype(str).tolist()
                    favs = list(set(core.load_favs()) | set(kodlar))
                    core.save_favs(favs)
                    st.success(f"{len(kodlar)} favori")
            with fc2:
                if st.button("🛒 Portföy sepetine ekle", key="sepet_sira_akilli"):
                    ad_map = {str(r["kod"]): str(r.get("ad") or r["kod"]) for _, r in secili.iterrows()}
                    n = sepet_ekle(secili["kod"].astype(str).tolist(), ad_map=ad_map, kaynak="siralama")
                    st.success(f"{n} fon sepete eklendi (toplam {len(st.session_state.portfoy_sepet)})")

        if core.HAS_MPL and st.button("📊 PNG grafik rapor", key="gr_sira"):
            path = core.grafik_rapor(
                filt if not filt.empty else work,
                baslik=f"Dönem sıralaması | {datetime.now().strftime('%d.%m.%Y')}",
            )
            if path:
                st.image(path)
    elif df is not None and df.empty:
        st.warning("Kriterlere uyan fon yok.")


# ═══════════════════════════════════════════════════════════════════════════
# KARŞILAŞTIRMA
# ═══════════════════════════════════════════════════════════════════════════
elif sayfa == "🧪 Filtre Stüdyosu":
    tefas_filtre.render(
        core=core, get_analiz=get_analiz, sepet_ekle=sepet_ekle, show_df=show_df,
        download_excel_button=download_excel_button, fmt_num=fmt_num,
    )

elif sayfa == "⚖️ Karşılaştırma":
    st.header("⚖️ Fon Karşılaştırma")
    kodlar_str = st.text_input("Fon kodları (virgülle)", value="AAL,AFO")
    if st.button("Karşılaştır", type="primary"):
        analiz = get_analiz()
        rows = []
        with st.spinner("Veriler çekiliyor…"):
            for kod in [k.strip().upper() for k in kodlar_str.split(",") if k.strip()]:
                try:
                    d = analiz.fon_detay(kod)
                    m, o, p = d["metrikler"], d["ozet"], d["profil"]
                    rows.append({
                        "kod": kod,
                        "ad": (o.get("fonUnvan") or p.get("fonUnvan") or kod)[:50],
                        "risk": p.get("riskDegeri"),
                        "faiz": core.faiz_durum(p),
                        **m,
                    })
                except Exception as e:
                    st.warning(f"{kod}: {e}")
        st.session_state.son_karsilastir = pd.DataFrame(rows) if rows else pd.DataFrame()

    df = st.session_state.son_karsilastir
    if df is not None and not df.empty:
        cols = [
            "kod", "ad", "risk", "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y",
            "reel_1y", "vs_mevduat", "vs_bist100", "volatilite", "max_drawdown",
            "sharpe", "sortino", "calmar", "fiyat",
        ]
        show_df(df, cols, height=350)
        download_excel_button(df, f"tefas_karsilastir_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx", key="xl_kar")


# ═══════════════════════════════════════════════════════════════════════════
# PORTFÖY
# ═══════════════════════════════════════════════════════════════════════════
elif sayfa == "📁 Portföy":
    st.header("📁 Portföy Analizi")
    mod = st.radio(
        "Mod",
        ["🛒 Sepet (portföye ekle)", "Elle ağırlık", "Otomatik (en yüksek puan)"],
        horizontal=True,
    )

    if mod == "🛒 Sepet (portföye ekle)":
        st.caption(
            "Tarama, sıralama, akıllı portföy veya fon detaydan **Portföye ekle** ile biriktirdiğiniz fonlar. "
            "Burada hepsini analiz edip özet rapor alırsınız."
        )
        sepet = st.session_state.get("portfoy_sepet") or []
        st.markdown("**İstediğiniz kadar fon ekleyin** (virgül / satır; limit yok)")
        c1, c2, c3 = st.columns([3, 1, 1])
        with c1:
            manuel = st.text_area(
                "Kodlar",
                placeholder="AAL, TTE, AFO\nMAC, TTE",
                key="sepet_manuel",
                height=70,
                label_visibility="collapsed",
            )
        with c2:
            st.write("")
            if st.button("Sepete ekle", key="sepet_manuel_btn", use_container_width=True) and manuel:
                import re as _re
                kodlar = [x.strip() for x in _re.split(r"[,;\s]+", manuel) if x.strip()]
                n = sepet_ekle(kodlar, kaynak="manuel")
                st.success(f"{n} eklendi (toplam {len(st.session_state.get('portfoy_sepet') or [])})")
                st.rerun()
        with c3:
            st.write("")
            if st.button("Sepeti temizle", key="sepet_temiz_page", use_container_width=True):
                st.session_state.portfoy_sepet = []
                st.session_state.sepet_analiz = None
                st.rerun()

        if not sepet:
            st.info("Sepet boş. Tarama / sıralama sonuçlarından **🛒 Portföye ekle** kullanın veya yukarıya kod yazın.")
        else:
            st.markdown(f"**Sepetteki fonlar ({len(sepet)})**")
            sdf = pd.DataFrame(sepet)
            st.dataframe(sdf, hide_index=True, use_container_width=True)
            sil = st.multiselect("Sepetten çıkar", [x["kod"] for x in sepet], key="sepet_sil")
            if st.button("Seçilenleri çıkar") and sil:
                st.session_state.portfoy_sepet = [x for x in sepet if x["kod"] not in sil]
                st.rerun()

            st.subheader("Ağırlıklar")
            use_equal = st.checkbox("Eşit ağırlık", value=True, key="sepet_eq_w")
            custom_w = {}
            if not use_equal:
                cols_w = st.columns(min(len(sepet), 4))
                for i, item in enumerate(sepet):
                    with cols_w[i % len(cols_w)]:
                        custom_w[item["kod"]] = st.number_input(
                            f"{item['kod']} %",
                            value=round(100 / len(sepet)),
                            min_value=0, max_value=100,
                            key=f"sepet_w_{item['kod']}",
                        )
            else:
                custom_w = {x["kod"]: 1.0 for x in sepet}

            if st.button("📊 Sepeti analiz et + derin rapor", type="primary", key="btn_sepet_analiz"):
                with st.spinner("Fonlar, korelasyon ve birleşik metrikler hesaplanıyor…"):
                    st.session_state.sepet_analiz = sepet_analiz_et(weights=custom_w)

            sa = st.session_state.get("sepet_analiz")
            if sa and sa.get("error"):
                st.error(sa["error"])
            elif sa and sa.get("df") is not None:
                pm = sa.get("port_metrikler") or {}
                # KPI şeridi — risk/getiri + tehlike ortalaması
                k1, k2, k3, k4, k5, k6 = st.columns(6)
                k1.metric("Fon", sa.get("n_fon") or len(sa["df"]))
                k2.metric("Birleşik 1Y %", fmt_num(sa.get("ort_getiri_1y") if sa.get("ort_getiri_1y") is not None else pm.get("getiri_1y")))
                k3.metric("Sharpe", fmt_num(pm.get("sharpe"), 2))
                k4.metric("MaxDD %", fmt_num(pm.get("max_drawdown")))
                k5.metric("Vol %", fmt_num(pm.get("volatilite")))
                k6.metric("Çeşitlendirme", f"{sa.get('diversify_score') or '—'}/100")
                t1, t2, t3, t4 = st.columns(4)
                t1.metric(
                    "Ort. tehlike (1–7)",
                    sa.get("ort_tehlike_yazi") or "—",
                    help="Ağırlıklı ortalama SPK risk değeri (1=çok düşük, 7=çok yüksek)",
                )
                t2.metric("Ort. getiri 1A %", fmt_num(sa.get("ort_getiri_1a")))
                t3.metric("Ort. getiri 3A %", fmt_num(sa.get("ort_getiri_3a")))
                t4.metric("Ort. getiri 6A %", fmt_num(sa.get("ort_getiri_6a")))

                tab_o, tab_t, tab_k, tab_d, tab_g, tab_c, tab_r = st.tabs([
                    "Özet rapor", "Fon tablosu", "Korelasyon", "Dağılım", "Grafik",
                    "Çeşitlendirme önerisi", "HTML / QuantStats",
                ])
                with tab_o:
                    st.markdown(sa.get("ozet_md") or "")
                    if sa.get("liderler"):
                        st.caption("Öne çıkanlar yukarıdaki metinde listelenmiştir.")
                with tab_c:
                    oner = sa.get("oneriler") or []
                    st.markdown("**Çeşitlendirme skorunu yükseltmek için eklenebilecek fonlar**")
                    st.caption(
                        "Yalnızca **TEFAS’ta açık** (işlem gören) fonlar önerilir; "
                        "tasfiye / işleme kapalı / yasaklı kurucu elenir. "
                        "Aday kaynağı: son tarama/sıralama veya TEFAS açık evren."
                    )
                    if not oner:
                        st.info(
                            "Öneri yok. Önce **Tüm Fon Sıralaması** veya **Fon Tarama** çalıştırıp "
                            "yeniden analiz edin; en az 2 fon ve fiyat serisi gerekir. "
                            "Öneriler yalnızca TEFAS açık fonlardan seçilir."
                        )
                    else:
                        odf = pd.DataFrame(oner)
                        st.dataframe(odf, hide_index=True, use_container_width=True)
                        ekle_kod = st.multiselect(
                            "Sepete ekle",
                            [o["kod"] for o in oner],
                            default=[o["kod"] for o in oner[:3]],
                            key="sepet_oneri_ekle",
                        )
                        if st.button("🛒 Önerilenleri sepete ekle", key="btn_oneri_sepet") and ekle_kod:
                            ad_map = {o["kod"]: o.get("ad", o["kod"]) for o in oner}
                            n = sepet_ekle(ekle_kod, ad_map=ad_map, kaynak="cesitlendirme")
                            st.success(f"{n} fon sepete eklendi — yeniden analiz edin.")
                            st.rerun()
                with tab_r:
                    st.markdown("**Türkçe HTML portföy raporu** (+ quantstats varsa ayrı risk raporu)")
                    if st.button("📄 HTML rapor üret", type="primary", key="btn_sepet_html"):
                        with st.spinner("HTML rapor yazılıyor…"):
                            try:
                                _pm = dict(pm or {})
                                _pm["ort_tehlike"] = sa.get("ort_tehlike")
                                _pm["ort_tehlike_yazi"] = sa.get("ort_tehlike_yazi")
                                if sa.get("ort_getiri_1y") is not None:
                                    _pm["getiri_1y"] = sa.get("ort_getiri_1y")
                                path = core.portfoy_html_raporu(
                                    baslik=f"Sepet Portföy ({sa.get('n_fon')} fon)",
                                    metrikler=_pm,
                                    tablo=sa.get("df"),
                                    diversify_score=sa.get("diversify_score"),
                                    corr_df=sa.get("corr"),
                                    oneriler=sa.get("oneriler"),
                                    ozet_md=sa.get("ozet_md") or "",
                                    fiyat_df=sa.get("port_fd"),
                                    quantstats=True,
                                )
                                if path:
                                    st.session_state["sepet_html_path"] = path
                                    st.success(f"Rapor: {path}")
                                else:
                                    st.warning("Rapor üretilemedi.")
                            except Exception as e:
                                st.error(f"Rapor hatası: {e}")
                    hp = st.session_state.get("sepet_html_path")
                    if hp and Path(hp).exists():
                        data = Path(hp).read_bytes()
                        st.download_button(
                            "⬇️ HTML indir",
                            data=data,
                            file_name=Path(hp).name,
                            mime="text/html",
                            key="dl_sepet_html",
                        )
                        st.caption(str(hp))
                        # quantstats dosyası aynı klasörde qs_rapor_* olabilir
                        try:
                            qs_files = sorted(Path(hp).parent.glob("qs_rapor_*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
                            if qs_files:
                                qf = qs_files[0]
                                st.download_button(
                                    "⬇️ QuantStats HTML indir",
                                    data=qf.read_bytes(),
                                    file_name=qf.name,
                                    mime="text/html",
                                    key="dl_sepet_qs",
                                )
                        except Exception:
                            pass
                with tab_t:
                    show_df(sa["df"], list(sa["df"].columns), height=420)
                    # Ağırlık kolonu
                    wmap = sa.get("weights") or {}
                    if wmap:
                        wdf = pd.DataFrame([
                            {"kod": k, "ağırlık %": round(v * 100, 1)}
                            for k, v in sorted(wmap.items(), key=lambda x: -x[1])
                        ])
                        st.markdown("**Kullanılan ağırlıklar**")
                        st.dataframe(wdf, hide_index=True, use_container_width=True)
                with tab_k:
                    corr = sa.get("corr")
                    if corr is not None and not corr.empty:
                        st.caption("Günlük getiri korelasyonu — düşük değerler daha iyi çeşitlendirme anlamına gelir.")
                        st.dataframe(corr.round(2), use_container_width=True)
                        try:
                            st.dataframe(
                                corr.style.background_gradient(cmap="RdYlGn_r", vmin=-1, vmax=1),
                                use_container_width=True,
                            )
                        except Exception:
                            pass
                    else:
                        st.info("Korelasyon için en az 2 fon ve yeterli ortak fiyat serisi gerekir.")
                with tab_d:
                    bd = sa.get("birlesik_dag") or {}
                    kd = sa.get("kategori_dag") or {}
                    cda, cdb = st.columns(2)
                    with cda:
                        st.markdown("**Birleşik varlık kırılımı**")
                        if bd:
                            labels = {
                                "hisse": "Hisse", "tahvil": "Tahvil", "repo": "Repo",
                                "mevduat": "Mevduat", "altin": "Altın", "doviz": "Döviz",
                                "fon": "Fon", "diger": "Diğer",
                            }
                            ser = pd.Series({labels.get(k, k): v for k, v in bd.items()})
                            st.bar_chart(ser)
                        else:
                            st.caption("Dağılım verisi yok")
                    with cdb:
                        st.markdown("**Kategori ağırlıkları**")
                        if kd:
                            st.bar_chart(pd.Series(kd))
                        else:
                            st.caption("Kategori verisi yok")
                with tab_g:
                    pret = sa.get("port_ret")
                    if pret is not None and len(pret) > 5:
                        cum = (1 + pret).cumprod()
                        st.markdown("**Birleşik portföy kümülatif getiri (normalize)**")
                        st.line_chart(cum.rename("Portföy"))
                        # Drawdown
                        peak = cum.cummax()
                        dd = (cum / peak - 1.0) * 100
                        st.markdown("**Düşüş (drawdown) %**")
                        st.area_chart(dd.rename("DD %"))
                    else:
                        st.info("Grafik için yeterli birleşik seri yok.")

                download_excel_button(
                    sa["df"],
                    f"tefas_sepet_derin_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    meta={
                        "mod": "portfoy_sepet_derin",
                        "weights": sa.get("weights"),
                        "port_metrikler": pm,
                        "diversify_score": sa.get("diversify_score"),
                        "birlesik_dag": sa.get("birlesik_dag"),
                        "kategori_dag": sa.get("kategori_dag"),
                    },
                    key="xl_sepet",
                )
                if st.button("TEFAS portföy motoru (ağırlıklı)", key="sepet_equal_engine"):
                    w = sa.get("weights") or {}
                    if w:
                        analiz = get_analiz()
                        with st.spinner("portfoy_analiz…"):
                            s = analiz.portfoy_analiz(w)
                        st.session_state.son_portfoy = s
                        mm = s.get("metrikler") or {}
                        st.json({k: mm.get(k) for k in (
                            "getiri_1y", "reel_1y", "sharpe", "max_drawdown", "volatilite", "vs_bist100"
                        )})

    elif mod == "Elle ağırlık":
        n = st.slider("Kaç fon", 2, 8, 3)
        weights = {}
        cols = st.columns(min(n, 4))
        for i in range(n):
            with cols[i % len(cols)]:
                kod = st.text_input(f"Fon {i+1} kodu", key=f"pk_{i}").strip().upper()
                pct = st.number_input(f"Ağırlık %", value=round(100 / n), min_value=1, max_value=100, key=f"pw_{i}")
                if kod:
                    weights[kod] = float(pct)
        if st.button("Portföyü analiz et", type="primary") and weights:
            analiz = get_analiz()
            with st.spinner("Hesaplanıyor…"):
                s = analiz.portfoy_analiz(weights)
            m = s.get("metrikler") or {}
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("1Y %", fmt_num(m.get("getiri_1y")))
            c2.metric("Reel 1Y", fmt_num(m.get("reel_1y")))
            c3.metric("Sharpe", fmt_num(m.get("sharpe"), 2))
            c4.metric("MaxDD %", fmt_num(m.get("max_drawdown")))
            st.markdown("**Ağırlıklar**")
            st.json({k: f"%{v*100:.0f}" for k, v in (s.get("weights") or {}).items()})
            dag = {k: v for k, v in (s.get("dagilim") or {}).items() if v and v > 0.5}
            if dag:
                st.markdown("**Birleşik dağılım**")
                st.json(dag)
            st.session_state.son_portfoy = s
            # Excel: ağırlık + metrik özeti
            rows = []
            for k, v in (s.get("weights") or {}).items():
                rows.append({"kod": k, "agirlik_%": round(v * 100, 2), **{mk: m.get(mk) for mk in (
                    "getiri_1y", "reel_1y", "sharpe", "max_drawdown", "volatilite", "vs_mevduat", "vs_bist100"
                )}})
            if rows:
                edf = pd.DataFrame(rows)
                download_excel_button(
                    edf,
                    f"tefas_portfoy_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    meta={"mod": "elle_portfoy", "agirliklar": s.get("weights"), "metrikler": m},
                    key="xl_port_elle",
                )

    else:
        st.caption("Risk paketine göre tarama yapıp en yüksek skorlu fonlardan portföy kurar.")
        paket_sec = st.selectbox(
            "Risk paketi",
            list(core.RISK_PAKETLER.keys()),
            format_func=lambda k: core.RISK_PAKETLER[k]["ad"],
            index=1, key="oto_paket",
        )
        paket = core.RISK_PAKETLER[paket_sec]
        hedef = core.PROFILLER.get(paket.get("profil") or "2", core.PROFILLER["2"])[1]
        n = st.slider("Portföye kaç fon", 2, 10, 5)
        yontem = st.radio("Ağırlık", ["Puan-orantılı", "Eşit"], horizontal=True)
        max_w = st.slider("Tek fon max ağırlık", 0.1, 1.0, 0.5, 0.05)

        if st.button("Otomatik portföy oluştur", type="primary"):
            analiz = get_analiz()
            with st.spinner("En yüksek puanlı fonlar aranıyor…"):
                df = analiz.tara(
                    hedef,
                    min_sharpe=paket.get("min_sharpe"),
                    max_vol=paket.get("max_vol"),
                    max_dd=paket.get("max_dd"),
                    max_fon=n,
                    max_aday=max(40, n * 6),
                    max_risk=paket.get("max_risk"),
                )
            if df.empty:
                st.warning("Uygun fon bulunamadı.")
            else:
                kodlar = df["kod"].tolist()
                if yontem == "Eşit":
                    w = {k: 1.0 / len(kodlar) for k in kodlar}
                else:
                    skorlar = np.array([float(v) if v is not None else 0.0 for v in df["skor"].tolist()])
                    w_arr = core.optimize_score_proportional(skorlar, max_w=max_w)
                    w = {kodlar[i]: float(w_arr[i]) for i in range(len(kodlar))}
                show_df(df, ["skor", "kod", "ad", "getiri_1y", "sharpe", "volatilite", "max_drawdown", "risk_bayrak"], 300)
                st.markdown("**Ağırlıklar**")
                for k, v in sorted(w.items(), key=lambda x: -x[1]):
                    st.write(f"**{k}**: %{v*100:.1f}")
                s = analiz.portfoy_analiz(w)
                m = s.get("metrikler") or {}
                c1, c2, c3 = st.columns(3)
                c1.metric("1Y %", fmt_num(m.get("getiri_1y")))
                c2.metric("Sharpe", fmt_num(m.get("sharpe"), 2))
                c3.metric("MaxDD %", fmt_num(m.get("max_drawdown")))
                st.session_state.son_portfoy = s
                edf = df.copy()
                edf["portfoy_agirlik_%"] = edf["kod"].map(lambda k: round(w.get(k, 0) * 100, 2))
                download_excel_button(
                    edf,
                    f"tefas_oto_portfoy_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    meta={"mod": "otomatik_portfoy", "risk_paketi": paket["ad"], "agirliklar": w, "metrikler": m},
                    key="xl_port_oto",
                )


# ═══════════════════════════════════════════════════════════════════════════
# OPTİMİZASYON
# ═══════════════════════════════════════════════════════════════════════════
elif sayfa == "💼 Portföyüm":
    tefas_portfoyum.render(core=core, get_analiz=get_analiz)

elif sayfa == "🎯 Optimizasyon":
    st.header("🎯 Portföy Optimizasyonu")
    kodlar_str = st.text_input("Fon kodları (virgülle)", value="AAL,AFO,TTE", key="opt_kod")
    method = st.selectbox(
        "Yöntem",
        ["max_sharpe", "min_var", "risk_parity", "cvar", "equal", "score", "all"],
        format_func=lambda x: {
            "max_sharpe": "Max Sharpe",
            "min_var": "Min Variance",
            "risk_parity": "Risk Parity",
            "cvar": "Min CVaR (%5)",
            "equal": "Eşit ağırlık",
            "score": "Skor orantılı",
            "all": "Hepsi (karşılaştır)",
        }[x],
    )
    max_w = st.slider("Tek fon max ağırlık", 0.05, 1.0, 0.40, 0.05)
    max_risk = st.number_input("Max risk_skor filtresi (boş=yok)", value=None, step=1.0, placeholder="yok")

    if st.button("Optimize et", type="primary"):
        analiz = get_analiz()
        kodlar = [k.strip().upper() for k in kodlar_str.split(",") if k.strip()]
        series, scores = {}, {}
        with st.spinner("Fiyat serileri ve metrikler…"):
            for kod in kodlar:
                p = analiz.api.profil(kod)
                ad = p.get("fonUnvan") or kod
                ok, neden = core.fon_yatirima_uygun_mu(ad, p.get("tefasDurum") or "")
                if not ok:
                    st.warning(f"✗ {kod}: {neden}")
                    continue
                fd = analiz.api.fiyat(kod, periyod=12)
                if fd.empty:
                    st.warning(f"✗ {kod}: fiyat yok")
                    continue
                an = core.anomaly_from_prices(fd)
                o = analiz.api.ozet(kod)
                m = core.full_metrics(fd, analiz.rf)
                liq = core.likidite_risk(o, m, profil=p, anomaly=an)
                risk_skor = (an.get("anomaly_skor") or 0) + (liq.get("likidite_skor_risk") or 0)
                if max_risk is not None and risk_skor > max_risk:
                    st.warning(f"✗ {kod}: risk_skor={risk_skor:.1f}")
                    continue
                series[kod] = fd.set_index("tarih")["fiyat"]
                scores[kod] = float(m.get("sharpe") or 0) * 10 + float(m.get("getiri_1y") or 0) - risk_skor
                st.caption(f"✓ {kod}  risk_skor={risk_skor:.1f}  sharpe={m.get('sharpe')}")

        if len(series) < 2:
            st.error("En az 2 uygun fon gerekli.")
        else:
            methods = (
                ["max_sharpe", "min_var", "risk_parity", "cvar", "equal", "score"]
                if method == "all" else [method]
            )
            rows = []
            for met in methods:
                out = core.run_optimizer(met, series, rf=analiz.rf, max_w=max_w, scores=scores)
                if out.get("error"):
                    st.warning(out["error"])
                    continue
                st.subheader(met)
                c1, c2, c3 = st.columns(3)
                c1.metric("μ (yıllık)", f"%{out['port_mu']}")
                c2.metric("Vol", f"%{out['port_vol']}")
                c3.metric("Sharpe", f"{out['port_sharpe']}")
                wdf = pd.DataFrame([
                    {"kod": k, "ağırlık %": round(v * 100, 1),
                     "risk katkısı %": out["risk_contrib_pct"].get(k, 0)}
                    for k, v in sorted(out["weights"].items(), key=lambda x: -x[1]) if v >= 0.001
                ])
                st.dataframe(wdf, hide_index=True, use_container_width=True)
                hist = core.portfolio_metrics(out["weights"], series, rf=analiz.rf)
                if hist:
                    st.caption(
                        f"Tarihsel 1Y: %{hist.get('getiri_1y')} · "
                        f"MaxDD: %{hist.get('max_drawdown')} · Sharpe: {hist.get('sharpe')}"
                    )
                rows.append({"yontem": met, "mu_%": out["port_mu"], "vol_%": out["port_vol"],
                             "sharpe": out["port_sharpe"], **{f"w_{k}": v for k, v in out["weights"].items()}})
            if rows:
                st.session_state.opt_sonuc = pd.DataFrame(rows)
                download_excel_button(
                    st.session_state.opt_sonuc,
                    f"tefas_opt_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    key="xl_opt",
                )


# ═══════════════════════════════════════════════════════════════════════════
# FON DETAY (kod veya ad ile arama)
# ═══════════════════════════════════════════════════════════════════════════
elif sayfa == "🔎 Fon Detay":
    st.header("🔎 Fon Detay & Risk Raporu")
    st.caption("Fon **kodu** (örn. AAL) veya **adının bir kısmını** yazın (örn. Ak Portföy, altın, para piyasası)")

    arama = st.text_input(
        "Fon kodu veya adı",
        value=st.session_state.get("detay_arama", ""),
        placeholder="örn. AAL  veya  İş Portföy Hisse  veya  altın",
        key="detay_arama_input",
    ).strip()

    rapor_tur = st.radio(
        "HTML rapor türü",
        ["Türkçe (önerilen)", "Quantstats"] if core.HAS_QUANTSTATS else ["Türkçe (önerilen)"],
        horizontal=True,
    )
    bm_ok = st.checkbox("BIST100 ile karşılaştır", value=True)

    def _normalize_search(s: str) -> str:
        return core._normalize_tr(s or "").lower()

    def _fon_ara(analiz: core.FonAnaliz, q: str) -> pd.DataFrame:
        """Kod tam eşleşme veya unvan içinde arama."""
        q = (q or "").strip()
        if not q:
            return pd.DataFrame()
        q_up = q.upper().replace(" ", "")
        # Kısa ve alfanümerik → muhtemel kod
        if len(q) <= 6 and q_up.isalnum():
            try:
                p = analiz.api.profil(q_up)
                if p:
                    ad = p.get("fonUnvan") or q_up
                    return pd.DataFrame([{"kod": q_up, "ad": ad}])
            except Exception:
                pass
        # İsim / kısmi arama — dağılım listesi
        with st.spinner("Fon listesinde aranıyor…"):
            dag = analiz.api.dagilimlar()
        if dag is None or dag.empty:
            return pd.DataFrame()
        nq = _normalize_search(q)
        rows = []
        for _, r in dag.iterrows():
            kod = str(r.get("kod") or "").upper()
            ad = str(r.get("ad") or "")
            if nq in _normalize_search(ad) or nq in _normalize_search(kod) or q_up == kod:
                rows.append({"kod": kod, "ad": ad})
        if not rows:
            return pd.DataFrame()
        out = pd.DataFrame(rows).drop_duplicates("kod")
        # Daha iyi eşleşenler üste (başta geçen / daha kısa ad)
        def _rank(row):
            ad_n = _normalize_search(row["ad"])
            if ad_n.startswith(nq):
                return (0, len(ad_n))
            if nq in ad_n:
                return (1, len(ad_n))
            return (2, len(ad_n))
        out["_r"] = out.apply(_rank, axis=1)
        out = out.sort_values("_r").drop(columns=["_r"]).reset_index(drop=True)
        return out.head(40)

    def _detay_yukle(analiz: core.FonAnaliz, kod: str):
        d = analiz.fon_detay(kod)
        m, o, p = d["metrikler"], d["ozet"], d["profil"]
        ad = o.get("fonUnvan") or p.get("fonUnvan") or kod
        # Anomali + likidite özeti
        fd = d.get("fiyat_df")
        an = core.anomaly_from_prices(fd) if fd is not None else {}
        liq = core.likidite_risk(o, m, profil=p, anomaly=an)
        return {
            "d": d, "m": m, "o": o, "p": p, "ad": ad, "kod": kod,
            "an": an, "liq": liq,
        }

    col_ara, col_btn = st.columns([3, 1])
    with col_btn:
        st.write("")
        st.write("")
        ara_tik = st.button("🔍 Ara / Analiz et", type="primary", use_container_width=True)

    if ara_tik and arama:
        st.session_state.detay_arama = arama
        analiz = get_analiz()
        bulunan = _fon_ara(analiz, arama)
        st.session_state.detay_arama_sonuc = bulunan
        if bulunan is None or bulunan.empty:
            st.warning(f"«{arama}» için fon bulunamadı. Kodu veya adın bir parçasını deneyin.")
            st.session_state.detay_sonuc = None
        elif len(bulunan) == 1:
            kod = str(bulunan.iloc[0]["kod"])
            with st.spinner(f"{kod} analiz ediliyor…"):
                try:
                    st.session_state.detay_sonuc = _detay_yukle(analiz, kod)
                except Exception as e:
                    st.error(str(e))
                    st.session_state.detay_sonuc = None
        else:
            st.session_state.detay_sonuc = None  # seçim bekleniyor

    # Birden fazla sonuç → seçim
    bulunan = st.session_state.get("detay_arama_sonuc")
    if (
        st.session_state.get("detay_sonuc") is None
        and bulunan is not None
        and not getattr(bulunan, "empty", True)
        and len(bulunan) > 1
    ):
        st.info(f"**{len(bulunan)}** fon bulundu — analiz için birini seçin:")
        sec_etiket = [
            f"{r['kod']} — {str(r['ad'])[:70]}"
            for _, r in bulunan.iterrows()
        ]
        secim = st.radio("Fon seçin", sec_etiket, key="detay_secim_radio")
        if st.button("Seçileni analiz et", type="primary", key="detay_sec_analiz"):
            kod = secim.split(" — ")[0].strip().upper()
            analiz = get_analiz()
            with st.spinner(f"{kod} analiz ediliyor…"):
                try:
                    st.session_state.detay_sonuc = _detay_yukle(analiz, kod)
                except Exception as e:
                    st.error(str(e))
                    st.session_state.detay_sonuc = None

    det = st.session_state.detay_sonuc
    if det:
        m, o, p, ad, kod = det["m"], det["o"], det["p"], det["ad"], det["kod"]
        an, liq = det.get("an") or {}, det.get("liq") or {}
        st.subheader(f"`{kod}` — {ad}")
        st.caption(
            f"TEFAS: {p.get('tefasDurum')} · Risk: {p.get('riskDegeri')} · "
            f"Faiz: {core.faiz_durum(p, ad)} · Kategori: {o.get('fonKategori')} · "
            f"İşlem: {p.get('basIsSaat') or '?'}-{p.get('sonIsSaat') or '?'}"
        )

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("1Y %", fmt_num(m.get("getiri_1y")))
        c2.metric("Reel 1Y", fmt_num(m.get("reel_1y")))
        c3.metric("Sharpe", fmt_num(m.get("sharpe"), 2))
        c4.metric("Sortino", fmt_num(m.get("sortino"), 2))
        c5.metric("MaxDD %", fmt_num(m.get("max_drawdown")))
        c6.metric("Vol %", fmt_num(m.get("volatilite")))

        c7, c8, c9, c10, c11, c12 = st.columns(6)
        c7.metric("vs Mevduat", fmt_num(m.get("vs_mevduat")))
        c8.metric("vs BIST100", fmt_num(m.get("vs_bist100")))
        c9.metric("CAGR %", fmt_num(m.get("cagr")))
        c10.metric("Fiyat", fmt_num(m.get("fiyat"), 4))
        c11.metric("Likidite", fmt_num(liq.get("likidite_skor"), 0))
        c12.metric("Risk skor", fmt_num(an.get("anomaly_skor"), 1))

        if liq.get("likidite_seviye") or an.get("risk_bayrak") or liq.get("likidite_bayrak"):
            parts = []
            if liq.get("likidite_seviye"):
                parts.append(f"Likidite: {liq.get('likidite_seviye')}")
            if liq.get("likidite_bayrak"):
                parts.append(str(liq.get("likidite_bayrak")))
            if an.get("risk_bayrak"):
                parts.append(str(an.get("risk_bayrak")))
            st.caption(" · ".join(parts))

        more = {
            "1A %": m.get("getiri_1a"), "3A %": m.get("getiri_3a"), "6A %": m.get("getiri_6a"),
            "3Y %": m.get("getiri_3y"), "Calmar": m.get("calmar"),
            "VaR95": m.get("var95"), "cVaR95": m.get("cvar95"),
            "Kazanma %": m.get("kazanma_orani"),
            "En iyi gün %": m.get("en_iyi_gun"), "En kötü gün %": m.get("en_kotu_gun"),
            "Yatırımcı": o.get("yatirimciSayi"), "Büyüklük": o.get("portBuyukluk"),
            "Giriş kom.": p.get("girisKomisyonu"), "Çıkış kom.": p.get("cikisKomisyonu"),
        }
        st.json({k: v for k, v in more.items() if v is not None})

        # Portföy dağılımı (detay ekranı)
        try:
            analiz_d = get_analiz()
            dag = analiz_d.api.dagilimlar()
            if dag is not None and not dag.empty:
                row_d = dag[dag["kod"].astype(str).str.upper() == kod.upper()]
                if not row_d.empty:
                    oz = analiz_d.api.dagilim_ozet(row_d.iloc[0])
                    oz = {core.KATEGORI_ACIKLAMA.get(k, k): round(v, 1) for k, v in oz.items() if v and v >= 0.5}
                    if oz:
                        st.subheader("Portföy varlık dağılımı")
                        st.bar_chart(pd.Series(oz, name="Pay %"))
                        st.caption(" · ".join(f"{k}: %{v}" for k, v in sorted(oz.items(), key=lambda x: -x[1])))
        except Exception:
            pass

        # Sade Türkçe değerlendirme (plain markdown — Streamlit HTML'i bazen boş gösteriyordu)
        st.subheader("Sade dilde değerlendirme")
        metin_deg = core.turkce_degerlendirme(
            sharpe=m.get("sharpe"), sortino=m.get("sortino"),
            max_dd=m.get("max_drawdown"), vol=m.get("volatilite"),
            cagr=m.get("cagr"), calmar=m.get("calmar"),
            getiri_1y=m.get("getiri_1y"), reel_1y=m.get("reel_1y"),
            vs_mevduat=m.get("vs_mevduat"), vs_bist100=m.get("vs_bist100"),
            kazanma_orani=m.get("kazanma_orani"),
            en_kotu_gun=m.get("en_kotu_gun"),
            likidite_skor=(liq or {}).get("likidite_skor"),
            plain=True,
        )
        # BIST korelasyonu detayda ayrıca hesaplanmış olabilir
        try:
            if det.get("d") and det["d"].get("fiyat_df") is not None:
                bk = core.fon_bist_korelasyon(det["d"]["fiyat_df"])
                if bk is not None:
                    ekstra = core.turkce_degerlendirme(bist_korelasyon=bk, plain=True)
                    # sadece BIST satırını ekle
                    for line in ekstra.splitlines():
                        if "Korelasyon" in line or "korelasyon" in line:
                            metin_deg = metin_deg.rstrip() + "\n\n" + line + "\n"
        except Exception:
            pass
        st.markdown(metin_deg)
        # HTML kutu da (rapor stili) — yedek görünüm
        with st.expander("HTML kutu görünümü", expanded=False):
            st.components.v1.html(
                core.turkce_degerlendirme(
                    sharpe=m.get("sharpe"), sortino=m.get("sortino"),
                    max_dd=m.get("max_drawdown"), vol=m.get("volatilite"),
                    cagr=m.get("cagr"), calmar=m.get("calmar"),
                    getiri_1y=m.get("getiri_1y"), reel_1y=m.get("reel_1y"),
                    vs_mevduat=m.get("vs_mevduat"), vs_bist100=m.get("vs_bist100"),
                    kazanma_orani=m.get("kazanma_orani"),
                    en_kotu_gun=m.get("en_kotu_gun"),
                    likidite_skor=(liq or {}).get("likidite_skor"),
                    plain=False,
                ),
                height=420,
                scrolling=True,
            )

        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button("⭐ Favorilere ekle", key="detay_fav"):
                favs = list(set(core.load_favs()) | {kod})
                core.save_favs(favs)
                st.success(f"{kod} eklendi.")
            if st.button("🛒 Portföy sepetine ekle", key="detay_sepet"):
                n = sepet_ekle([kod], ad_map={kod: ad}, kaynak="detay")
                st.success(f"{kod} sepete eklendi" if n else f"{kod} zaten sepette")
        with b2:
            # Mini Excel
            row = {"kod": kod, "ad": ad, **{k: m.get(k) for k in (
                "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y", "getiri_3y",
                "reel_1y", "vs_mevduat", "vs_bist100", "volatilite", "max_drawdown",
                "sharpe", "sortino", "calmar", "cagr", "fiyat",
            )}}
            download_excel_button(
                pd.DataFrame([row]),
                f"tefas_detay_{kod}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                meta={"kod": kod, "ad": ad},
                key="xl_detay",
            )
        with b3:
            pass

        if st.button("📄 HTML risk raporu oluştur", type="primary"):
            analiz = get_analiz()
            fd = det["d"].get("fiyat_df")
            if fd is None or len(fd) < 100:
                fd = analiz.api.fiyat(kod, periyod=36)
            bm = core.yahoo_daily_returns("XU100.IS") if bm_ok else None
            # Portföy varlık dağılımı
            dagilim_fon = {}
            try:
                dag = analiz.api.dagilimlar()
                if dag is not None and not dag.empty:
                    row = dag[dag["kod"].astype(str).str.upper() == kod.upper()]
                    if not row.empty:
                        dagilim_fon = analiz.api.dagilim_ozet(row.iloc[0])
            except Exception:
                pass
            ek_m = dict(m)
            for k, v in (dagilim_fon or {}).items():
                ek_m[f"d_{k}"] = v
            with st.spinner("Rapor üretiliyor…"):
                if rapor_tur.startswith("Quant") and core.HAS_QUANTSTATS:
                    path = core.quantstats_rapor(fd, kod, ad=ad, benchmark_returns=bm, ekstra_metrikler=ek_m)
                else:
                    path = core.turkce_risk_raporu(
                        fd, kod, ad=ad, rf=analiz.rf,
                        benchmark_returns=bm, benchmark_ad="BIST100",
                        ekstra_metrikler=ek_m, dagilim=dagilim_fon,
                    )
            if path:
                st.success(f"Rapor: {path}")
                with open(path, "rb") as f:
                    st.download_button(
                        "HTML indir", f, file_name=Path(path).name,
                        mime="text/html", key="dl_html",
                    )
                try:
                    html = Path(path).read_text(encoding="utf-8")
                    st.components.v1.html(html, height=800, scrolling=True)
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════
# HİSSE & KARMA PORTFÖY
# ═══════════════════════════════════════════════════════════════════════════
elif sayfa == "📈 Hisse & Karma Portföy":
    st.header("📈 Hisse analizi & fon+hisse karma portföy")
    st.caption(
        "BIST hisseleri Yahoo Finance üzerinden çekilir (örn. THYAO, GARAN, ASELS). "
        "Fon kodları TEFAS; hisseler `.IS` soneki ile veya kısa kodla girilir."
    )

    tab_h, tab_k = st.tabs(["Hisse analizi", "Fon + Hisse portföy"])

    with tab_h:
        hisse_str = st.text_input(
            "Hisse kodları (virgülle)",
            value=st.session_state.get("hisse_liste", "THYAO,GARAN,ASELS"),
            placeholder="THYAO, GARAN, EREGL, TUPRS",
            key="hisse_input",
        )
        if st.button("Hisseleri analiz et", type="primary", key="btn_hisse_analiz"):
            semboller = [x.strip() for x in hisse_str.split(",") if x.strip()]
            st.session_state.hisse_liste = hisse_str
            analiz = get_analiz()
            with st.spinner("Hisse fiyatları ve metrikler hesaplanıyor…"):
                hdf = core.hisseleri_analiz_et(semboller, rf=analiz.rf, cfg=st.session_state.cfg)
            st.session_state.hisse_sonuc = hdf

        hdf = st.session_state.get("hisse_sonuc")
        if hdf is not None and not hdf.empty:
            if "hata" in hdf.columns and hdf["hata"].notna().any():
                for _, r in hdf[hdf["hata"].notna()].iterrows():
                    st.warning(f"{r.get('sembol')}: {r.get('hata')}")
            ok = hdf[hdf.get("hata", pd.Series(dtype=object)).isna()] if "hata" in hdf.columns else hdf
            if not ok.empty:
                st.success(f"{len(ok)} hisse analiz edildi")
                cols_h = [
                    c for c in [
                        "kod", "sembol", "fiyat",
                        "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y",
                        "reel_1y", "vs_mevduat", "vs_bist100",
                        "sharpe", "sortino", "calmar", "cagr",
                        "volatilite", "max_drawdown", "kazanma_orani",
                        "en_kotu_gun", "var95", "bist_korelasyon", "risk_skor",
                    ] if c in ok.columns
                ]
                show_df(ok, cols_h, height=360)
                download_excel_button(
                    ok,
                    f"tefas_hisse_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    meta={"mod": "hisse_analiz"},
                    key="xl_hisse",
                )
                # Tek hisse detay değerlendirme
                sec_h = st.selectbox("Sade dilde değerlendirme için hisse", ok["kod"].tolist(), key="hisse_det_sec")
                if sec_h:
                    row = ok[ok["kod"] == sec_h].iloc[0]
                    st.markdown(core.turkce_degerlendirme(
                        sharpe=row.get("sharpe"), sortino=row.get("sortino"),
                        max_dd=row.get("max_drawdown"), vol=row.get("volatilite"),
                        cagr=row.get("cagr"), calmar=row.get("calmar"),
                        getiri_1y=row.get("getiri_1y"), reel_1y=row.get("reel_1y"),
                        vs_mevduat=row.get("vs_mevduat"), vs_bist100=row.get("vs_bist100"),
                        kazanma_orani=row.get("kazanma_orani"),
                        en_kotu_gun=row.get("en_kotu_gun"),
                        bist_korelasyon=row.get("bist_korelasyon"),
                        plain=True,
                    ))
                    if st.button("HTML risk raporu (hisse)", key="hisse_html_rap"):
                        analiz = get_analiz()
                        det = core.hisse_analiz(str(row.get("sembol") or sec_h), rf=analiz.rf, cfg=st.session_state.cfg)
                        if det.get("error"):
                            st.error(det["error"])
                        else:
                            path = core.turkce_risk_raporu(
                                det["fiyat_df"], det["kod"], ad=det["sembol"],
                                rf=analiz.rf,
                                benchmark_returns=core.yahoo_daily_returns("XU100.IS"),
                                ekstra_metrikler=det["metrikler"],
                            )
                            if path:
                                st.success(path)
                                with open(path, "rb") as f:
                                    st.download_button("HTML indir", f, file_name=Path(path).name, mime="text/html", key="dl_hisse_html")

    with tab_k:
        st.markdown("Fon kodları + hisse kodlarını birlikte ağırlıklandırın.")
        n_var = st.slider("Kaç varlık", 2, 12, 4, key="karma_n")
        weights = {}
        cols_w = st.columns(min(n_var, 4))
        for i in range(n_var):
            with cols_w[i % len(cols_w)]:
                kod = st.text_input(
                    f"Varlık {i+1} (fon veya hisse)",
                    value=["AAL", "THYAO", "GARAN", "TTE"][i] if i < 4 else "",
                    key=f"karma_kod_{i}",
                    help="Fon: AAL · Hisse: THYAO veya THYAO.IS",
                ).strip().upper()
                pct = st.number_input(f"Ağırlık %", value=round(100 / n_var), min_value=1, max_value=100, key=f"karma_w_{i}")
                if kod:
                    # Hisse kısa kodlarını .IS ile işaretle eğer 5 harf ve TEFAS değil gibi
                    weights[kod] = float(pct)

        if st.button("Karma portföyü analiz et", type="primary", key="btn_karma"):
            if not weights:
                st.warning("En az bir varlık girin.")
            else:
                analiz = get_analiz()
                with st.spinner("Fon + hisse serileri birleştiriliyor…"):
                    out = core.portfoy_fon_hisse_analiz(analiz, weights)
                st.session_state.karma_sonuc = out

        out = st.session_state.get("karma_sonuc")
        if out and out.get("error"):
            st.error(out["error"])
            if out.get("meta"):
                st.json(out["meta"])
        elif out and out.get("metrikler"):
            m = out["metrikler"]
            st.success(f"Ortak seri: {out.get('n_gun')} gün")
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("1Y %", fmt_num(m.get("getiri_1y")))
            c2.metric("Sharpe", fmt_num(m.get("sharpe"), 2))
            c3.metric("MaxDD %", fmt_num(m.get("max_drawdown")))
            c4.metric("Vol %", fmt_num(m.get("volatilite")))
            c5.metric("vs BIST", fmt_num(m.get("vs_bist100")))
            st.markdown("**Normalize ağırlıklar**")
            wdf = pd.DataFrame([
                {"kod": k, "ağırlık %": round(v * 100, 1),
                 "tip": (out.get("meta") or {}).get(k, {}).get("tip", "?"),
                 "ad": (out.get("meta") or {}).get(k, {}).get("ad", k)}
                for k, v in sorted((out.get("weights") or {}).items(), key=lambda x: -x[1])
            ])
            st.dataframe(wdf, hide_index=True, use_container_width=True)
            st.markdown(core.turkce_degerlendirme(
                sharpe=m.get("sharpe"), sortino=m.get("sortino"),
                max_dd=m.get("max_drawdown"), vol=m.get("volatilite"),
                cagr=m.get("cagr"), calmar=m.get("calmar"),
                getiri_1y=m.get("getiri_1y"), reel_1y=m.get("reel_1y"),
                vs_mevduat=m.get("vs_mevduat"), vs_bist100=m.get("vs_bist100"),
                plain=True,
            ))
            download_excel_button(
                wdf.assign(**{k: m.get(k) for k in ("getiri_1y", "sharpe", "max_drawdown", "volatilite")}),
                f"tefas_karma_portfoy_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                meta={"mod": "karma_fon_hisse", "weights": out.get("weights"), "metrikler": m},
                key="xl_karma",
            )


# ═══════════════════════════════════════════════════════════════════════════
# FAVORİLER
# ═══════════════════════════════════════════════════════════════════════════
elif sayfa == "🏦 BIST Değerleme":
    tefas_bist.render(show_df=show_df, download_excel_button=download_excel_button)

elif sayfa == "🔎 Hisse Detay":
    st.info(
        "Hisse kodu/adı ile **tüm değerleme alanlarını** görmek için "
        "**🏦 BIST Değerleme → 🔎 Hisse Detay** sekmesini kullanın "
        "(rapor seç → kod veya şirket adı yazın)."
    )
    tefas_bist.render(show_df=show_df, download_excel_button=download_excel_button)

elif sayfa == "⭐ Favoriler":
    st.header("⭐ Favoriler")
    favs = core.load_favs()

    c1, c2 = st.columns([3, 1])
    with c1:
        yeni = st.text_input("Fon kodu ekle", placeholder="örn AAL").strip().upper()
    with c2:
        st.write("")
        st.write("")
        if st.button("Ekle") and yeni:
            favs = list(set(favs) | {yeni})
            core.save_favs(favs)
            st.rerun()

    if favs:
        st.write("Mevcut:", ", ".join(favs))
        cikar = st.multiselect("Çıkarılacaklar", favs)
        if st.button("Seçilenleri çıkar") and cikar:
            core.save_favs([f for f in favs if f not in cikar])
            st.rerun()

        if st.button("Favorileri güncelle / alarm kontrol", type="primary"):
            analiz = get_analiz()
            with st.spinner("Favoriler çekiliyor…"):
                df = analiz.favorileri_guncelle()
            if not df.empty:
                cols = [c for c in [
                    "kod", "ad", "getiri_1y", "reel_1y", "sharpe", "max_drawdown",
                    "volatilite", "tefas_durum", "risk_degeri",
                ] if c in df.columns]
                show_df(df, cols, 350)
                download_excel_button(
                    df, f"favoriler_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx", key="xl_fav",
                )
            if core.ALERT_LOG.exists():
                st.caption(f"Alarm log: {core.ALERT_LOG}")
                try:
                    log = core.ALERT_LOG.read_text(encoding="utf-8").strip().splitlines()[-15:]
                    st.code("\n".join(log) or "(boş)")
                except Exception:
                    pass
    else:
        st.info("Favori listesi boş.")


# ═══════════════════════════════════════════════════════════════════════════
# AYARLAR
# ═══════════════════════════════════════════════════════════════════════════
elif sayfa == "⚙️ Ayarlar":
    st.header("⚙️ Ayarlar")
    cfg = st.session_state.cfg

    c1, c2, c3 = st.columns(3)
    rf = c1.number_input("Risksiz faiz (0.40 = %40)", value=float(cfg.get("risk_free", 0.40)), min_value=0.0, max_value=1.0, step=0.01)
    tufe = c2.number_input("TÜFE yıllık %", value=float(cfg.get("tufe_yillik", 32.0)), min_value=0.0, step=0.5)
    mev = c3.number_input("Mevduat yıllık %", value=float(cfg.get("mevduat_yillik", 40.0)), min_value=0.0, step=0.5)

    st.subheader("Alarm eşikleri")
    al = cfg.get("alarmlar") or {}
    a1, a2, a3, a4 = st.columns(4)
    min_g = a1.number_input("Min 1Y % alarm", value=al.get("min_getiri_1y"), step=1.0, placeholder="yok")
    min_s = a2.number_input("Min Sharpe alarm", value=al.get("min_sharpe"), step=0.1, placeholder="yok")
    max_d = a3.number_input("Max DD % alarm", value=al.get("max_drawdown"), step=1.0, placeholder="yok")
    max_v = a4.number_input("Max vol % alarm", value=al.get("max_volatilite"), step=1.0, placeholder="yok")

    if st.button("Kaydet", type="primary"):
        cfg["risk_free"] = rf
        cfg["tufe_yillik"] = tufe
        cfg["mevduat_yillik"] = mev
        cfg["alarmlar"] = {
            "min_getiri_1y": min_g,
            "min_sharpe": min_s,
            "max_drawdown": max_d,
            "max_volatilite": max_v,
        }
        core.save_config(cfg)
        st.session_state.cfg = cfg
        if st.session_state.analiz is not None:
            st.session_state.analiz.cfg = cfg
            st.session_state.analiz.rf = float(rf)
            st.session_state.analiz.bench = {}
        st.success(f"Kaydedildi → {core.CONFIG_PATH}")

    st.divider()
    st.subheader("Cache")
    cache_files = list(core.CACHE_DIR.glob("*.json")) if core.CACHE_DIR.exists() else []
    st.caption(f"{len(cache_files)} cache dosyası · {core.CACHE_DIR}")
    if st.button("Cache temizle") and cache_files:
        for f in cache_files:
            try:
                f.unlink()
            except Exception:
                pass
        st.success("Cache silindi.")
        st.rerun()