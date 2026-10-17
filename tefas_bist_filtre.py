# -*- coding: utf-8 -*-
"""
BIST Filtre Stüdyosu
====================
BIST Değerleme raporundaki (Ana Sıralama / Değerleme Özeti) hisseleri çok kriterli filtreler.
Motor: tefas_filtre (şema tabanlı) — burada BIST'e özel kriter kaydı, hazır profiller ve arayüz var.

Birimler: Raporda kesir olarak tutulan oran sütunları (ROE, ROIC, marjlar, getiriler, Adil Değer Marjı…)
burada ×100 yapılıp YÜZDE olarak gösterilir (ör. 0.15 → 15). Etiketlerde "%" yazar.
"""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import tefas_filtre as TF

ROOT = Path(__file__).resolve().parent
PRESET_PATH = ROOT / "bist_filtre_presetleri.json"

# Kesir olarak tutulan (görüntülemede ×100 → %) sütunlar — vbist_degerleme PCT_COL_NAMES + Temettü Verimi
PCT_COLS = {
    "Adil Değer Marjı", "Değerleme Uzlaşma Skoru", "ROE", "ROA", "ROIC", "Net Kar Marjı", "Faaliyet Kar Marjı",
    "FAVÖK Marjı", "FCF Marjı", "FCF/Net Kar", "Kazanç Getirisi", "FCF Getirisi", "Net Kâr Trend Tutarlılığı (8Ç)",
    "Kârlılık Skoru", "Değer Skoru", "Bilanço Skoru", "Nakit Akışı Skoru", "Veri Tamlık", "Brüt Kar Marjı",
    "Graham Kriter Geçiş Oranı", "Graham Güvenlik Marjı", "Net Nakit/Piyasa Değeri", "FCF Conversion Trend",
    "Büyüme Skoru", "Economic Quality Skoru", "ROIC-WACC Spread", "Accrual Kalitesi", "Brüt Marj İstikrarı",
    "Seyreltme Skoru", "Owner Earnings Getirisi", "Sloan Skoru", "Beneish Skoru", "ROIC Trend Skoru",
    "Brüt Marj Trend Skoru", "Reverse DCF Skoru", "WACC (ke)", "Reverse DCF Implied g", "RI Marjı", "EPV Marjı",
    "ROE-ke Spread", "RI/Defter", "Temettü Verimi",
}

# (sütun, grup, adım, yön)  yön: True=yüksek iyi, False=düşük iyi, None=nötr
_SEED = [
    # Model skorları
    ("Final Score", "Model skorları", 1.0, True), ("Adjusted Score", "Model skorları", 1.0, True),
    ("Güven Skoru", "Model skorları", 1.0, True), ("Risk Skoru", "Model skorları", 1.0, True),
    ("Risk Cezası", "Model skorları", 1.0, False), ("Peer Skor", "Model skorları", 1.0, True),
    ("Birleşik Skor", "Model skorları", 1.0, True), ("Risk Ayarlı Skor", "Model skorları", 1.0, True),
    ("Kârlılık Skoru", "Model skorları", 1.0, True), ("Değer Skoru", "Model skorları", 1.0, True),
    ("Bilanço Skoru", "Model skorları", 1.0, True), ("Nakit Akışı Skoru", "Model skorları", 1.0, True),
    ("Büyüme Skoru", "Model skorları", 1.0, True), ("Economic Quality Skoru", "Model skorları", 1.0, True),
    ("Veri Tamlık", "Model skorları", 5.0, True),
    # Değerleme
    ("Adil Değer Marjı", "Değerleme", 5.0, True), ("RI Marjı", "Değerleme", 5.0, True),
    ("EPV Marjı", "Değerleme", 5.0, True), ("Graham Güvenlik Marjı", "Değerleme", 5.0, True),
    ("Graham Kriter Geçiş Oranı", "Değerleme", 5.0, True), ("Graham Puan", "Değerleme", 1.0, True),
    ("Değerleme Uzlaşma Skoru", "Değerleme", 5.0, True),
    ("F/K", "Çarpanlar", 1.0, False), ("PD/DD", "Çarpanlar", 0.1, False),
    ("F/K (Forward)", "Çarpanlar", 1.0, False), ("PD/DD (Forward)", "Çarpanlar", 0.1, False),
    ("EV/EBIT", "Çarpanlar", 1.0, False), ("EV/FAVÖK", "Çarpanlar", 1.0, False),
    ("EV/Sales", "Çarpanlar", 0.1, False), ("Price/Sales", "Çarpanlar", 0.1, False),
    ("Kazanç Getirisi", "Çarpanlar", 1.0, True), ("FCF Getirisi", "Çarpanlar", 1.0, True),
    ("Owner Earnings Getirisi", "Çarpanlar", 1.0, True), ("Temettü Verimi", "Çarpanlar", 0.5, True),
    ("Net Nakit/Piyasa Değeri", "Çarpanlar", 5.0, True),
    # Tarihsel F/K & PD/DD (kendi μ/σ) — "Tarihsel Ucuz" sayfasından
    ("Tarihsel Ucuzluk Skoru", "Tarihsel çarpan", 0.1, True),
    ("FK z-skor", "Tarihsel çarpan", 0.1, False), ("PD/DD z-skor", "Tarihsel çarpan", 0.1, False),
    ("FK Ort (tarihsel)", "Tarihsel çarpan", 0.5, False), ("PD/DD Ort (tarihsel)", "Tarihsel çarpan", 0.1, False),
    ("FK SD (tarihsel)", "Tarihsel çarpan", 0.1, False), ("PD/DD SD (tarihsel)", "Tarihsel çarpan", 0.05, False),
    ("FK yüzdelik", "Tarihsel çarpan", 1.0, False), ("PD/DD yüzdelik", "Tarihsel çarpan", 1.0, False),
    # Karlılık
    ("ROE", "Karlılık", 1.0, True), ("ROA", "Karlılık", 1.0, True), ("ROIC", "Karlılık", 1.0, True),
    ("Net Kar Marjı", "Karlılık", 1.0, True), ("Faaliyet Kar Marjı", "Karlılık", 1.0, True),
    ("FAVÖK Marjı", "Karlılık", 1.0, True), ("FCF Marjı", "Karlılık", 1.0, True),
    ("Brüt Kar Marjı", "Karlılık", 1.0, True), ("ROE-ke Spread", "Karlılık", 1.0, True),
    ("ROIC-WACC Spread", "Karlılık", 1.0, True), ("RI/Defter", "Karlılık", 1.0, True),
    ("Net Kâr Trend Tutarlılığı (8Ç)", "Karlılık", 5.0, True), ("FCF/Net Kar", "Karlılık", 5.0, True),
    # Bilanço & kalite
    ("Cari Oran", "Bilanço & kalite", 0.1, True), ("Borç/Özkaynak", "Bilanço & kalite", 0.1, False),
    ("Net Borç/Özkaynak", "Bilanço & kalite", 0.1, False), ("Net Borç/FCF", "Bilanço & kalite", 0.5, False),
    ("Net Borç/FAVÖK", "Bilanço & kalite", 0.5, False), ("Kısa Vadeli Borç/Nakit", "Bilanço & kalite", 0.1, False),
    ("Faiz Karşılama", "Bilanço & kalite", 0.5, True), ("Altman Z", "Bilanço & kalite", 0.1, True),
    ("Piotroski F", "Bilanço & kalite", 1.0, True), ("Beneish M (Proxy)", "Bilanço & kalite", 0.1, False),
    ("Net YP/EBITDA", "Bilanço & kalite", 0.1, False), ("Temettü Ödeyen Çeyrek (8Ç)", "Bilanço & kalite", 1.0, True),
    # Teknik & pozisyon
    ("Vol Ann (σ)", "Teknik & pozisyon", 0.01, False), ("RSI(14)", "Teknik & pozisyon", 1.0, None),
    ("ADX(14)", "Teknik & pozisyon", 1.0, None), ("Bollinger %B", "Teknik & pozisyon", 0.05, None),
    ("Sharpe Proxy", "Teknik & pozisyon", 0.1, True), ("Önerilen Pay %", "Teknik & pozisyon", 0.5, True),
    ("Kelly Pay Proxy", "Teknik & pozisyon", 0.5, True), ("Shannon Güven", "Teknik & pozisyon", 1.0, True),
    # Piyasa
    ("Fiyat", "Piyasa", 1.0, None), ("Piyasa Değeri", "Piyasa", 1e9, None),
]
SEED = {c: {"group": g, "step": s, "better": b} for c, g, s, b in _SEED}
GROUP_ORDER = ["Model skorları", "Değerleme", "Çarpanlar", "Karlılık", "Bilanço & kalite",
               "Teknik & pozisyon", "Piyasa", "Diğer"]
CAT_SEED = ["Sektör", "Kova", "Yatırım Kararı", "Risk Sınıfı", "Değerleme Sınıfı", "Sınıf", "Nihai Sinyal",
            "Uyum Kadranı", "Altman Bölge", "Net-Net (NCAV) Durumu", "Marj Yönü", "Döviz Riski",
            "Değer Yatırımcılığı Sonucu", "Trend Durumu (SMA50)", "Supertrend Yön", "Hacim Teyidi",
            "Ters Sinyal", "Zarar Niteliği", "Kova Aksiyon"]

WEIGHT_PROFILES = {
    "Dengeli": {"Final Score": 25, "Adil Değer Marjı": 20, "ROIC": 15, "FCF Getirisi": 10, "Piotroski F": 10,
                "Güven Skoru": 10, "Risk Skoru": 10},
    "Değer": {"Adil Değer Marjı": 30, "F/K": 15, "PD/DD": 15, "FCF Getirisi": 15, "EPV Marjı": 10, "Final Score": 15},
    "Kalite": {"ROIC": 25, "ROE": 15, "Net Kar Marjı": 10, "Piotroski F": 15, "Altman Z": 10,
               "Net Kâr Trend Tutarlılığı (8Ç)": 10, "FCF Marjı": 15},
    "Temettü": {"Temettü Verimi": 35, "Temettü Ödeyen Çeyrek (8Ç)": 15, "FCF Getirisi": 15, "ROIC": 15,
                "Net Borç/Özkaynak": 10, "Piotroski F": 10},
}

# Hazır profiller: ("abs", sütun, alt, üst) · ("top", sütun, oran)  ·  ("flag", anahtar)
# "top": yön yüksek-iyi ise en iyi %oran, düşük-iyi ise en düşük %oran (evrene göre, birimden bağımsız).
BIST_PRESETS: Dict[str, dict] = {
    "Ucuz & kaliteli (Graham+Buffett)": {
        "ad": "Makul çarpan + sağlam bilanço + yeterli karlılık",
        "kurallar": [("abs", "F/K", 0.01, 15), ("abs", "PD/DD", None, 2.5), ("abs", "Piotroski F", 6, None),
                     ("abs", "ROE", 12, None), ("abs", "Adil Değer Marjı", 15, None),
                     ("abs", "Altman Z", 1.8, None), ("flag", "ana_siralama")],
    },
    "Adil değer iskontosu": {
        "ad": "Modelin adil değerine göre belirgin iskontolu, güveni yüksek",
        "kurallar": [("abs", "Adil Değer Marjı", 25, None), ("abs", "RI Marjı", 10, None),
                     ("abs", "EPV Marjı", 0, None), ("top", "Güven Skoru", 0.5), ("top", "Risk Skoru", 0.6),
                     ("flag", "ana_siralama")],
    },
    "Kaliteli büyüme": {
        "ad": "Yüksek getirili sermaye, tutarlı kâr, düşük borç",
        "kurallar": [("top", "ROIC", 0.3), ("abs", "ROE", 15, None), ("abs", "Net Kar Marjı", 8, None),
                     ("top", "Net Kâr Trend Tutarlılığı (8Ç)", 0.5), ("abs", "Net Borç/Özkaynak", None, 1.0),
                     ("abs", "FCF Marjı", 0, None), ("abs", "Piotroski F", 6, None)],
    },
    "Temettü & nakit": {
        "ad": "Düzenli temettü ödeyen, nakit üreten, borcu makul",
        "kurallar": [("abs", "Temettü Verimi", 3, None), ("abs", "Temettü Ödeyen Çeyrek (8Ç)", 4, None),
                     ("abs", "FCF Getirisi", 4, None), ("abs", "Net Borç/Özkaynak", None, 1.0),
                     ("top", "ROIC", 0.6)],
    },
    "Savunmacı (düşük risk)": {
        "ad": "Güçlü likidite ve faiz karşılama, düşük borç, yüksek risk skoru",
        "kurallar": [("abs", "Altman Z", 3, None), ("abs", "Cari Oran", 1.5, None),
                     ("abs", "Net Borç/Özkaynak", None, 0.5), ("abs", "Faiz Karşılama", 4, None),
                     ("top", "Risk Skoru", 0.4)],
    },
    "Model en iyileri": {
        "ad": "Final skoru evrenin en iyi %20'si, güven ve risk filtreli",
        "kurallar": [("top", "Final Score", 0.2), ("top", "Güven Skoru", 0.5), ("top", "Risk Skoru", 0.6),
                     ("flag", "uyari_yok")],
    },
    "Teknik teyitli": {
        "ad": "Temel skoru iyi + trend gücü var + aşırı alım değil",
        "kurallar": [("top", "Final Score", 0.4), ("abs", "RSI(14)", 35, 70), ("abs", "ADX(14)", 20, None)],
    },
    "Tarihsel en ucuz (F/K+PD/DD z)": {
        "ad": "Kendi ~3y F/K ve PD/DD ortalamasının altında (negatif z) — rapor 'Tarihsel Ucuz' sayfası sütunları varsa",
        "kurallar": [
            ("abs", "FK z-skor", None, 0.0),
            ("abs", "PD/DD z-skor", None, 0.0),
            ("top", "Tarihsel Ucuzluk Skoru", 0.15),
            ("flag", "ana_siralama"),
        ],
    },
    "Forward ucuz + kalite": {
        "ad": "Gelecek F/K ve PD/DD düşük + FCF pozitif + borç makul",
        "kurallar": [
            ("abs", "F/K (Forward)", 0.01, 14),
            ("abs", "PD/DD (Forward)", 0.01, 2.2),
            ("abs", "FCF Getirisi", 2, 40),
            ("abs", "Net Borç/Özkaynak", None, 1.2),
            ("abs", "Piotroski F", 5, None),
            ("flag", "ana_siralama"),
        ],
    },
    "Faktör dengeli (değer+kalite)": {
        "ad": "Ucuz trailing/forward çarpan + yüksek ROIC + düşük borç",
        "kurallar": [
            ("abs", "F/K", 0.01, 16),
            ("abs", "F/K (Forward)", 0.01, 15),
            ("abs", "PD/DD", None, 2.5),
            ("top", "ROIC", 0.4),
            ("abs", "Net Borç/Özkaynak", None, 1.0),
            ("flag", "ana_siralama"),
        ],
    },
}


# ── Şema ve veri hazırlama ──────────────────────────────────────────────────
def _auto_step(s: pd.Series) -> float:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return 1.0
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = float(q3 - q1) or float(s.std() or 1.0)
    raw = max(iqr / 20.0, 1e-6)
    order = 10 ** math.floor(math.log10(raw))
    return float(max(round(raw / order) * order, 1e-6))


def prepare_bist(df: pd.DataFrame) -> pd.DataFrame:
    """Hisse kodunu normalleştir, tekilleştir, kesir sütunlarını yüzdeye çevir."""
    if "Hisse" not in df.columns:
        raise ValueError("Veride 'Hisse' sütunu yok.")
    d = df.copy()
    d["Hisse"] = d["Hisse"].astype(str).str.strip().str.upper()
    d = d.drop_duplicates("Hisse").reset_index(drop=True)
    for c in PCT_COLS:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce") * 100.0
    return d


def _flag_ana(d: pd.DataFrame, nan_pass: bool) -> pd.Series:
    return d["Ana Sıralamada"].astype(str).str.startswith("Evet")


def _flag_uyari_yok(d: pd.DataFrame, nan_pass: bool) -> pd.Series:
    b = d["Model Uyarısı"].fillna("").astype(str).str.strip().str.lower()
    return b.isin(["", "nan", "none", "-", "yok"])


def build_schema(d: pd.DataFrame) -> TF.Schema:
    crit: Dict[str, dict] = {}
    for c, meta in SEED.items():
        if c in d.columns and pd.to_numeric(d[c], errors="coerce").notna().any():
            lab = c + (" %" if c in PCT_COLS else "")
            crit[c] = {"label": lab, **meta}
    for c in d.columns:
        if c in crit or str(c).startswith("_") or c == "Hisse":
            continue
        if pd.api.types.is_numeric_dtype(d[c]):
            s = pd.to_numeric(d[c], errors="coerce")
            if s.notna().mean() >= 0.05 and s.nunique() >= 3:
                crit[c] = {"label": c, "group": "Diğer", "step": _auto_step(s), "better": None}
    cats: Dict[str, str] = {}
    for c in CAT_SEED:
        if c in d.columns and 1 < d[c].astype(str).replace("nan", np.nan).nunique() <= 60:
            cats[c] = c
    for c in d.columns:
        if c in cats or c in crit or c in ("Hisse", "Ana Sıralamada") or d[c].dtype != object:
            continue
        v = d[c].dropna().astype(str)
        if 1 < v.nunique() <= 12 and v.str.len().mean() < 24:
            cats[c] = c
    flags = {}
    if "Ana Sıralamada" in d.columns:
        flags["ana_siralama"] = ("Ana sıralamada (finansal olmayan)", _flag_ana)
    if "Model Uyarısı" in d.columns:
        flags["uyari_yok"] = ("Model uyarısı yok", _flag_uyari_yok)
    return TF.Schema(criteria=crit, group_order=GROUP_ORDER, cat_cols=cats, id_col="Hisse",
                     name_col="Sektör", text_cols=("Hisse", "Sektör"), flags=flags)


def preset_spec(name: str, d: pd.DataFrame, schema: TF.Schema) -> Tuple[dict, List[str]]:
    """Hazır profili güncel veriye göre filtre spesifikasyonuna çevirir. (spec, atlanan sütunlar)"""
    sp = TF.empty_spec()
    atlanan: List[str] = []
    for kural in BIST_PRESETS[name]["kurallar"]:
        tip = kural[0]
        if tip == "flag":
            if kural[1] in schema.flags:
                sp["flags"][kural[1]] = True
            else:
                atlanan.append(kural[1])
            continue
        col = kural[1]
        if col not in schema.criteria:
            atlanan.append(col)
            continue
        if tip == "abs":
            sp["ranges"][col] = (kural[2], kural[3])
        elif tip == "top":
            s = pd.to_numeric(d[col], errors="coerce").dropna()
            better = schema.criteria[col]["better"]
            if s.empty or better is None:
                atlanan.append(col)
                continue
            q = float(kural[2])
            if better:
                sp["ranges"][col] = (round(float(s.quantile(1 - q)), 4), None)
            else:
                sp["ranges"][col] = (None, round(float(s.quantile(q)), 4))
    return sp, atlanan


# ── Streamlit arayüzü ───────────────────────────────────────────────────────
DEFAULT_COLS = ["sira", "Hisse", "Sektör", "filtre_skor", "Final Score", "Adil Değer Marjı", "F/K", "PD/DD",
                "ROE", "ROIC", "Piotroski F", "Altman Z", "Güven Skoru", "Risk Skoru", "Kova", "Nihai Sinyal",
                "Yatırım Kararı"]


def render_filter(d_raw: pd.DataFrame, *, send_cb: Callable[[List[str]], None], key: str = "bf"):
    import streamlit as st

    S = st.session_state
    try:
        d = prepare_bist(d_raw)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    sc = build_schema(d)
    avail = [k for k in sc.criteria]
    avail_sorted = sorted(avail, key=lambda k: (GROUP_ORDER.index(sc.criteria[k]["group"]), avail.index(k)))
    K = lambda n: f"{key}_{n}"  # noqa: E731

    # ── durum yardımcıları ──────────────────────────────────────────────────
    def _apply(spec: dict):
        for k in list(S.keys()):
            if str(k).startswith((K("lo_"), K("hi_"), K("cat_"))):
                del S[k]
        sel = [c for c in spec["ranges"] if c in avail]
        S[K("sel")] = sel
        for c in sel:
            lo, hi = spec["ranges"][c]
            S[K(f"lo_{c}")] = None if lo is None else float(lo)
            S[K(f"hi_{c}")] = None if hi is None else float(hi)
        S[K("catsel")] = [c for c in spec["cats"] if c in sc.cat_cols]
        for c, v in spec["cats"].items():
            S[K(f"cat_{c}")] = list(v)
        S[K("text")] = spec.get("text", "")
        S[K("text_not")] = spec.get("text_not", "")
        for fk in sc.flags:
            S[K(f"flag_{fk}")] = bool(spec["flags"].get(fk))
        S[K("nan")] = bool(spec.get("nan_pass"))

    def _apply_preset(name: str):
        sp, atl = preset_spec(name, d, sc)
        _apply(sp)
        S[K("atlanan")] = atl

    def _apply_weights(profil: str):
        w = WEIGHT_PROFILES.get(profil, {})
        for c in wcols_all:
            S[K(f"w_{c}")] = int(w.get(c, 0))

    def _collect() -> dict:
        sp = TF.empty_spec()
        for c in S.get(K("sel"), []):
            sp["ranges"][c] = (S.get(K(f"lo_{c}")), S.get(K(f"hi_{c}")))
        for c in S.get(K("catsel"), []):
            v = S.get(K(f"cat_{c}")) or []
            if v:
                sp["cats"][c] = list(v)
        sp["text"] = S.get(K("text"), "")
        sp["text_not"] = S.get(K("text_not"), "")
        for fk in sc.flags:
            sp["flags"][fk] = bool(S.get(K(f"flag_{fk}")))
        sp["nan_pass"] = bool(S.get(K("nan")))
        return sp

    st.caption(
        "Oran sütunları (ROE, ROIC, marjlar, getiriler, Adil Değer Marjı…) burada **yüzde** olarak gösterilir. "
        "Boş bırakılan sınır = sınır yok. Hazır profiller eşikleri güncel evrene göre doldurur."
    )

    # ── 1 · Hazır profil ────────────────────────────────────────────────────
    st.subheader("1 · Hazır profil")
    names = list(BIST_PRESETS)
    cols = st.columns(4)
    for i, n in enumerate(names):
        with cols[i % 4]:
            st.button(n, key=K(f"pr_{i}"), use_container_width=True, help=BIST_PRESETS[n]["ad"],
                      on_click=_apply_preset, args=(n,))
    with cols[len(names) % 4]:
        st.button("🧹 Hepsini temizle", key=K("clear"), use_container_width=True,
                  on_click=_apply, args=(TF.empty_spec(),))
    if S.get(K("atlanan")):
        st.caption("Bu raporda bulunmadığı için atlanan kriterler: " + ", ".join(S[K("atlanan")]))

    ups = TF.load_user_presets(PRESET_PATH)
    pc1, pc2, pc3 = st.columns([2, 2, 1])
    with pc1:
        kay = st.selectbox("Kayıtlı filtrelerim", ["—"] + list(ups), key=K("usersel"))
        b1, b2 = st.columns(2)
        if kay != "—":
            b1.button("📂 Yükle", key=K("userload"), on_click=_apply, args=(TF.normalize_spec(ups[kay]),))
            if b2.button("🗑️ Sil", key=K("userdel")):
                TF.delete_user_preset(kay, PRESET_PATH)
                st.rerun()
    with pc2:
        yeni = st.text_input("Mevcut filtreyi kaydet", placeholder="örn. Benim değer listem", key=K("savename"))
    with pc3:
        st.write("")
        st.write("")
        kaydet = st.button("💾 Kaydet", key=K("savebtn"))

    # ── 2 · Kriterler ───────────────────────────────────────────────────────
    st.subheader("2 · Kriterler")
    st.multiselect(
        "Sayısal kriterler (ekledikçe alt/üst sınır kutuları açılır)", avail_sorted,
        format_func=lambda k: f"{sc.criteria[k]['group']} · {sc.criteria[k]['label']}",
        key=K("sel"), placeholder="Kriter ekleyin (F/K, ROIC, Altman Z…)",
    )
    for c in S.get(K("sel"), []):
        S.setdefault(K(f"lo_{c}"), None)
        S.setdefault(K(f"hi_{c}"), None)
        ser = pd.to_numeric(d[c], errors="coerce").dropna()
        r1, r2, r3, r4 = st.columns([2.4, 1, 1, 1.4])
        r1.markdown(f"**{sc.criteria[c]['label']}**")
        r1.caption(f"min {TF.fmt(ser.min())} · medyan {TF.fmt(ser.median())} · maks {TF.fmt(ser.max())} "
                   f"· veri: {len(ser)}/{len(d)}")
        r2.number_input("Alt", value=None, step=float(sc.criteria[c]["step"]), format="%g", key=K(f"lo_{c}"),
                        placeholder="yok", label_visibility="collapsed")
        r3.number_input("Üst", value=None, step=float(sc.criteria[c]["step"]), format="%g", key=K(f"hi_{c}"),
                        placeholder="yok", label_visibility="collapsed")
        lo, hi = S.get(K(f"lo_{c}")), S.get(K(f"hi_{c}"))
        if lo is not None or hi is not None:
            ok = pd.Series(True, index=ser.index)
            if lo is not None:
                ok &= ser >= lo
            if hi is not None:
                ok &= ser <= hi
            r4.caption(f"tek başına eler: **{int((~ok).sum())}**")
        else:
            r4.caption("henüz sınır yok")

    with st.expander("Sektör / kova / metin ve diğer şartlar",
                     expanded=bool(S.get(K("catsel")) or S.get(K("text")))):
        st.multiselect("Kategorik filtre ekle", list(sc.cat_cols), key=K("catsel"))
        for c in S.get(K("catsel"), []):
            vals = sorted(d[c].dropna().astype(str).loc[lambda s: s.str.strip() != ""].unique().tolist())
            st.multiselect(c, vals, key=K(f"cat_{c}"))
        t1, t2 = st.columns(2)
        t1.text_input("Hisse / sektör içerir (virgülle → biri yeter)", key=K("text"), placeholder="THYAO, banka")
        t2.text_input("Şunları içermesin", key=K("text_not"), placeholder="gyo, holding")
        fl = st.columns(max(len(sc.flags), 1) + 1)
        for i, (fk, (etk, _fn)) in enumerate(sc.flags.items()):
            fl[i].checkbox(etk, key=K(f"flag_{fk}"))
        fl[-1].checkbox("Verisi eksik olanlar elenmesin", key=K("nan"),
                        help="Kapalıyken, bir kriter için verisi olmayan hisse elenir (daha temkinli).")

    spec = _collect()
    if kaydet:
        if yeni.strip():
            TF.save_user_preset(yeni.strip(), spec, PRESET_PATH)
            st.success(f"'{yeni.strip()}' kaydedildi.")
        else:
            st.warning("Kaydetmek için bir ad yazın.")

    # ── 3 · Sıralama ────────────────────────────────────────────────────────
    st.subheader("3 · Sıralama")
    wcols_all = [c for c in dict.fromkeys(c for p in WEIGHT_PROFILES.values() for c in p) if c in sc.criteria]
    with st.expander("Özel skor ağırlıkları (evren içi yüzdelik sıra ortalaması)"):
        wp = st.radio("Ağırlık profili", list(WEIGHT_PROFILES), horizontal=True, key=K("wprof"),
                      on_change=lambda: _apply_weights(S[K("wprof")]))
        for c in wcols_all:
            if K(f"w_{c}") not in S:
                S[K(f"w_{c}")] = int(WEIGHT_PROFILES[wp].get(c, 0))
        wc = st.columns(4)
        for i, c in enumerate(wcols_all):
            wc[i % 4].slider(sc.criteria[c]["label"], 0, 40, key=K(f"w_{c}"))
        st.caption("Düşük-iyi ölçütler (F/K, borç oranları…) otomatik ters çevrilir; eksik veri 0 puan alır.")
    d["filtre_skor"] = TF.weighted_score(d, {c: S.get(K(f"w_{c}"), 0) for c in wcols_all}, sc)

    sort_opts = ["filtre_skor"] + avail_sorted
    s1, s2 = st.columns([3, 1])
    sort_col = s1.selectbox("Sıralama ölçütü", sort_opts, key=K("sort"),
                            format_func=lambda k: "Özel ağırlıklı skor" if k == "filtre_skor" else sc.criteria[k]["label"])
    artan = s2.checkbox("Küçükten büyüğe", value=(sc.criteria.get(sort_col, {}).get("better") is False),
                        key=K(f"asc_{sort_col}"))

    # ── Uygula ──────────────────────────────────────────────────────────────
    rules = TF.build_rules(d, spec, sc)
    ok = TF.combine(d, rules)
    gecen = d[ok].sort_values(sort_col, ascending=artan, na_position="last").reset_index(drop=True)
    if "sira" in gecen.columns:
        gecen = gecen.drop(columns=["sira"])
    gecen.insert(0, "sira", range(1, len(gecen) + 1))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Evren", len(d))
    m2.metric("Filtreyi geçen", len(gecen))
    m3.metric("Geçme oranı", f"%{len(gecen) / max(len(d), 1) * 100:.0f}")
    if len(gecen) and "Final Score" in gecen.columns and gecen["Final Score"].notna().any():
        m4.metric("Medyan Final Score", TF.fmt(gecen["Final Score"].median()))
    else:
        m4.metric("Aktif kriter", len(rules))

    def _secim(frame: pd.DataFrame, kk: str, varsayilan: int = 5):
        kodlar = frame["Hisse"].astype(str).tolist()
        sec = st.multiselect("Gönderilecek hisseler", kodlar, default=kodlar[:varsayilan], key=K(f"pick_{kk}"))
        st.button("📈 'Hisse & Karma Portföy' sayfasına gönder", key=K(f"send_{kk}"), disabled=not sec,
                  on_click=send_cb, args=(sec,))
        if S.get("_bist_sent"):
            st.success(f"{S['_bist_sent']} hisse gönderildi — soldaki menüden '📈 Hisse & Karma Portföy' sayfasını açın.")

    def _yuvarla(df: pd.DataFrame) -> pd.DataFrame:
        v = df.copy()
        for c in v.columns:
            if pd.api.types.is_float_dtype(v[c]):
                v[c] = v[c].round(2)
        return v

    t_son, t_huni, t_yakin, t_harita, t_sektor, t_mercek = st.tabs(
        ["📋 Sonuçlar", "🔻 Huni", "🎯 Yakın kaçanlar", "🗺️ Harita", "🧩 Sektör dengeli seçim", "🔬 Hisse mercek"])

    with t_son:
        if not rules:
            st.info("Henüz kriter yok — tüm evren sıralanıyor. Hazır bir profil seçin veya kriter ekleyin.")
        if gecen.empty:
            st.warning("Hiçbir hisse tüm kriterleri geçemedi.")
            ft = TF.funnel_table(rules, len(d))
            if not ft.empty:
                st.markdown(f"En kısıtlayıcı kriter: **{ft.iloc[0]['Kriter']}** "
                            f"(tek başına {ft.iloc[0]['Tek başına eleyen']} hisseyi eliyor). "
                            "'Huni' ve 'Yakın kaçanlar' sekmelerine bakın.")
        else:
            with st.expander("Gösterilecek sütunlar"):
                tum = [c for c in (["sira", "Hisse", "Sektör", "filtre_skor"] + avail_sorted
                                   + [x for x in sc.cat_cols if x != "Sektör"]) if c in gecen.columns]
                gor = st.multiselect("Sütunlar", tum, default=[c for c in DEFAULT_COLS if c in tum],
                                     key=K("cols"), label_visibility="collapsed")
            gor = gor or [c for c in DEFAULT_COLS if c in gecen.columns]
            st.dataframe(_yuvarla(gecen[gor]), hide_index=True, use_container_width=True, height=480)
            _secim(gecen, "son")
            e1, e2 = st.columns(2)
            buf = io.BytesIO()
            gecen[gor].to_excel(buf, index=False, engine="openpyxl")
            e1.download_button("⬇️ Excel", buf.getvalue(), file_name="bist_filtre_sonuc.xlsx", key=K("xl"),
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            e2.download_button("⬇️ CSV", gecen[gor].to_csv(index=False).encode("utf-8-sig"),
                               file_name="bist_filtre_sonuc.csv", mime="text/csv", key=K("csv"))
        with st.expander("Aktif filtrenin özeti / paylaş (JSON)"):
            st.write("\n".join(f"- {r['label']}" for r in rules) or "Aktif kriter yok.")
            st.code(json.dumps(TF._spec_to_json(spec), ensure_ascii=False, indent=2), language="json")

    with t_huni:
        ft = TF.funnel_table(rules, len(d))
        if ft.empty:
            st.info("Huni için en az bir kriter ekleyin.")
        else:
            st.caption("**Tek başına eleyen**: kriterin kendi başına elediği hisse. **Yalnızca bu yüzden elenen**: "
                       "sadece bu kriter yüzünden kaybedilen hisse — gevşetirseniz geri gelir. "
                       "**Sıralı kalan**: en sert kriterden başlayarak kalan hisse sayısı.")
            st.dataframe(ft, hide_index=True, use_container_width=True)
            st.bar_chart(ft.set_index("Kriter")[["Tek başına eleyen", "Yalnızca bu yüzden elenen"]])
            en = ft.sort_values("Yalnızca bu yüzden elenen", ascending=False).iloc[0]
            if en["Yalnızca bu yüzden elenen"] > 0:
                st.success(f"En çok fırsat: **{en['Kriter']}** kriterini gevşetirseniz "
                           f"**{en['Yalnızca bu yüzden elenen']}** hisse daha geçer.")

    with t_yakin:
        if not rules:
            st.info("Önce kriter ekleyin.")
        else:
            mf = st.slider("En fazla kaç kriteri kaçırmış olsun?", 1, 3, 1, key=K("mf"))
            nm = TF.near_misses(d, rules, max_fail=mf, schema=sc)
            if nm.empty:
                st.success("Yakın kaçan hisse yok.")
            else:
                st.caption("Eşiği az farkla kaçıranlar önce.")
                kolon = [c for c in ["Hisse", "Sektör", "kaçırılan", "uzaklık", "Final Score", "Adil Değer Marjı",
                                     "F/K", "ROIC"] if c in nm.columns]
                st.dataframe(_yuvarla(nm[kolon]), hide_index=True, use_container_width=True, height=420)
                _secim(nm.head(50), "yakin", varsayilan=0)

    with t_harita:
        if len(avail_sorted) < 2:
            st.info("Harita için en az iki sayısal sütun gerekir.")
        else:
            h1, h2, h3, h4 = st.columns(4)
            xi = avail_sorted.index("F/K") if "F/K" in avail_sorted else 0
            yi = avail_sorted.index("ROIC") if "ROIC" in avail_sorted else 1
            xc = h1.selectbox("X ekseni", avail_sorted, index=xi, key=K("x"), format_func=lambda k: sc.criteria[k]["label"])
            yc = h2.selectbox("Y ekseni", avail_sorted, index=yi, key=K("y"), format_func=lambda k: sc.criteria[k]["label"])
            goster_elenen = h3.checkbox("Elenenleri göster", value=True, key=K("showout"))
            kirp = h4.checkbox("Uç değerleri kırp", value=True, key=K("clip"),
                               help="Grafikte %1–%99 aralığı dışındaki noktaları gizler (okunabilirlik).")
            xmin = sc.criteria[xc]["better"] is not True
            ymax = sc.criteria[yc]["better"] is not False
            pm = TF.pareto_mask(d[ok], xc, yc, x_min=xmin, y_max=ymax)
            pl = d[[xc, yc, "Hisse"]].copy()
            pl["Grup"] = np.where(~ok, "Elenen", np.where(pm.reindex(d.index, fill_value=False), "★ Etkin sınır", "Filtreyi geçen"))
            if not goster_elenen:
                pl = pl[pl["Grup"] != "Elenen"]
            pl = pl.dropna(subset=[xc, yc])
            if kirp and len(pl) > 20:
                for c in (xc, yc):
                    lo_q, hi_q = pl[c].quantile(0.01), pl[c].quantile(0.99)
                    pl = pl[(pl[c] >= lo_q) & (pl[c] <= hi_q)]
            st.scatter_chart(pl, x=xc, y=yc, color="Grup", height=420)
            st.caption(f"★ Etkin sınır: filtreyi geçenler arasında {sc.criteria[xc]['label']} "
                       f"{'düşük' if xmin else 'yüksek'}, {sc.criteria[yc]['label']} "
                       f"{'yüksek' if ymax else 'düşük'} yönünde başka bir hisse tarafından geçilemeyenler.")
            pf = d[ok][pm.reindex(d[ok].index, fill_value=False)].sort_values(yc, ascending=not ymax)
            if not pf.empty:
                kolon = [c for c in ["Hisse", "Sektör", xc, yc, "Final Score", "Adil Değer Marjı"] if c in pf.columns]
                st.dataframe(_yuvarla(pf[list(dict.fromkeys(kolon))]),
                             hide_index=True, use_container_width=True, height=300)

    with t_sektor:
        if gecen.empty:
            st.info("Önce filtreyi geçen hisse olmalı.")
        else:
            k1, k2 = st.columns(2)
            n_pick = k1.slider("Hisse sayısı", 3, 30, 15, key=K("npick"))
            mx = k2.slider("Aynı sektörden en fazla", 1, 6, 2, key=K("maxsek"))
            sec = TF.diversified_pick(gecen, n_pick, {"Sektör": mx} if "Sektör" in gecen.columns else {})
            st.caption(f"Sıralama: {('Özel skor' if sort_col == 'filtre_skor' else sc.criteria[sort_col]['label'])} · "
                       f"sektör sınırıyla {len(sec)} hisse.")
            kolon = [c for c in ["sira", "Hisse", "Sektör", "filtre_skor", "Final Score", "Adil Değer Marjı", "F/K",
                                 "ROIC"] if c in sec.columns]
            st.dataframe(_yuvarla(sec[kolon]), hide_index=True, use_container_width=True, height=360)
            _secim(sec, "sek", varsayilan=len(sec))

    with t_mercek:
        h = st.selectbox("Hisse", d["Hisse"].tolist(), key=K("lens"))
        i = d.index[d["Hisse"] == h][0]
        st.markdown(f"**{h}** · {d.at[i, 'Sektör'] if 'Sektör' in d.columns else ''}  →  "
                    f"{'✅ filtreyi geçiyor' if bool(ok.loc[i]) else '❌ filtreye takılıyor'}")
        ex = TF.explain_fund(d, rules, h, sc)
        if ex.empty:
            st.info("Aktif kriter yok.")
        else:
            ex = ex.rename(columns={"Fonun değeri": "Hissenin değeri"})
            st.dataframe(ex, hide_index=True, use_container_width=True)
        rows = []
        for c in avail_sorted:
            v = d.at[i, c]
            if pd.isna(v):
                continue
            pct = float(d[c].rank(pct=True).loc[i]) * 100
            iyi = sc.criteria[c]["better"]
            if iyi is False:
                pct = 100 - pct
            rows.append({"Ölçüt": sc.criteria[c]["label"], "Değer": TF.fmt(v),
                         "Evren içi konum": f"üst %{max(100 - pct, 1):.0f}" if iyi is not None else f"yüzdelik {pct:.0f}"})
        if rows:
            with st.expander("Evren içindeki konumu"):
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    S.pop("_bist_sent", None)
