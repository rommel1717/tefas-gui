# -*- coding: utf-8 -*-
"""
TEFAS Filtre Stüdyosu
=====================
Fon evrenini çok kriterli, şeffaf ve etkileşimli biçimde filtreleyen araç.

İki katman:
  1) Saf motor (Streamlit gerektirmez): kriter tanımları, maskeler, huni analizi,
     yakın kaçanlar, ağırlıklı yüzdelik skor, Pareto sınırı, çeşitlendirme, preset I/O.
  2) render(): Streamlit sayfası  →  tefas_gui.py içinden çağrılır.

Not: Boş bırakılan alt/üst sınır "sınır yok" demektir.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
PRESET_PATH = ROOT / "tefas_filtre_presetleri.json"

# ═══════════════════════════════════════════════════════════════════════════
# 1) KRİTER KAYDI
# ═══════════════════════════════════════════════════════════════════════════
# (anahtar, etiket, grup, adım, yön)  yön: True=yüksek iyi, False=düşük iyi, None=nötr
_CRIT = [
    ("getiri_1a", "1 Ay getiri %", "Getiri", 1.0, True),
    ("getiri_3a", "3 Ay getiri %", "Getiri", 1.0, True),
    ("getiri_6a", "6 Ay getiri %", "Getiri", 1.0, True),
    ("getiri_1y", "1 Yıl getiri %", "Getiri", 1.0, True),
    ("getiri_3y", "3 Yıl getiri %", "Getiri", 5.0, True),
    ("cagr", "CAGR %", "Getiri", 1.0, True),
    ("reel_1y", "Reel 1Y getiri %", "Getiri", 1.0, True),
    ("vs_mevduat", "Mevduata göre fark", "Getiri", 1.0, True),
    ("vs_bist100", "BIST100'e göre fark", "Getiri", 1.0, True),
    ("vs_altin", "Altına göre fark", "Getiri", 1.0, True),
    ("sharpe", "Sharpe", "Verimlilik", 0.1, True),
    ("sortino", "Sortino", "Verimlilik", 0.1, True),
    ("calmar", "Calmar", "Verimlilik", 0.1, True),
    ("kazanma_orani", "Kazanma oranı %", "Verimlilik", 1.0, True),
    ("volatilite", "Volatilite %", "Risk", 1.0, False),
    ("max_drawdown", "Max drawdown % (negatif)", "Risk", 1.0, True),
    ("var95", "VaR %95 (negatif)", "Risk", 0.5, True),
    ("cvar95", "cVaR %95 (negatif)", "Risk", 0.5, True),
    ("en_kotu_gun", "En kötü gün % (negatif)", "Risk", 0.5, True),
    ("en_iyi_gun", "En iyi gün %", "Risk", 0.5, None),
    ("risk_skor", "Risk skoru (anomali)", "Risk", 1.0, False),
    ("vol_oran", "Kısa/uzun vol oranı", "Risk", 0.1, False),
    ("jump_sayisi", "Fiyat sıçrama sayısı", "Risk", 1.0, False),
    ("risk_degeri", "TEFAS risk değeri (1-7)", "Risk", 1.0, False),
    ("likidite_skor", "Likidite skoru (0-100)", "Likidite & maliyet", 5.0, True),
    ("likit_varlik_pct", "Likit varlık %", "Likidite & maliyet", 5.0, True),
    ("giris_kom", "Giriş komisyonu", "Likidite & maliyet", 0.1, False),
    ("cikis_kom", "Çıkış komisyonu", "Likidite & maliyet", 0.1, False),
    ("buyukluk", "Fon büyüklüğü", "Ölçek", 1e6, None),
    ("yatirimci", "Yatırımcı sayısı", "Ölçek", 100.0, None),
    ("bist_korelasyon", "BIST korelasyonu (ρ)", "Korelasyon", 0.05, False),
    ("getiri_z", "Akran getiri z-skoru", "Korelasyon", 0.5, None),
    ("d_hisse", "Hisse %", "Portföy dağılımı", 5.0, None),
    ("d_tahvil", "Tahvil %", "Portföy dağılımı", 5.0, None),
    ("d_repo", "Repo %", "Portföy dağılımı", 5.0, None),
    ("d_mevduat", "Mevduat %", "Portföy dağılımı", 5.0, None),
    ("d_altin", "Altın %", "Portföy dağılımı", 5.0, None),
    ("d_doviz", "Döviz %", "Portföy dağılımı", 5.0, None),
    ("d_fon", "Fon %", "Portföy dağılımı", 5.0, None),
]
CRITERIA: Dict[str, dict] = {
    k: {"label": l, "group": g, "step": s, "better": b} for k, l, g, s, b in _CRIT
}
GROUP_ORDER = ["Getiri", "Verimlilik", "Risk", "Likidite & maliyet", "Korelasyon", "Ölçek", "Portföy dağılımı"]
CAT_COLS = {
    "baskin_tur": "Baskın varlık türü",
    "kurucu": "Kurucu",
    "kategori": "TEFAS kategorisi",
    "likidite_seviye": "Likidite seviyesi",
}

@dataclass
class Schema:
    """Filtre motorunun veri şeması: kriter kaydı, kategorik sütunlar, metin alanları, özel bayraklar."""
    criteria: dict
    group_order: list
    cat_cols: dict
    id_col: str = "kod"
    name_col: str = "ad"
    text_cols: tuple = ("kod", "ad", "kurucu")
    flags: dict = field(default_factory=dict)   # anahtar -> (etiket, fn(d, nan_pass) -> bool Series)

# Skor ağırlık profilleri (0 = kullanma). Yön CRITERIA["better"]'dan gelir.
WEIGHT_PROFILES = {
    "Dengeli": {"sharpe": 25, "sortino": 5, "getiri_1y": 15, "getiri_6a": 10, "getiri_3a": 5,
                "max_drawdown": 15, "volatilite": 5, "kazanma_orani": 10, "likidite_skor": 10},
    "Muhafazakâr": {"sharpe": 25, "sortino": 15, "getiri_1y": 5, "max_drawdown": 25, "volatilite": 10,
                    "kazanma_orani": 10, "likidite_skor": 10},
    "Getiri odaklı": {"getiri_1y": 30, "getiri_6a": 20, "getiri_3a": 15, "getiri_1a": 5, "sharpe": 15,
                      "max_drawdown": 5, "kazanma_orani": 5, "likidite_skor": 5},
    "Düşük maliyet & likit": {"likidite_skor": 25, "giris_kom": 15, "cikis_kom": 15, "sharpe": 15,
                              "max_drawdown": 15, "volatilite": 10, "getiri_1y": 5},
}
WEIGHT_COLS = [
    "sharpe", "sortino", "calmar", "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y", "getiri_3y",
    "reel_1y", "max_drawdown", "volatilite", "kazanma_orani", "likidite_skor", "risk_skor",
    "giris_kom", "cikis_kom", "bist_korelasyon",
]

# Excel çıktısındaki Türkçe başlıklar → iç anahtarlar (yüklenen dosyalar için)
HEADER_MAP = {
    "Fon Kodu": "kod", "Fon Adı": "ad", "Kurucu": "kurucu", "Risk": "risk_degeri", "Kategori": "kategori",
    "1A %": "getiri_1a", "3A %": "getiri_3a", "6A %": "getiri_6a", "1Y %": "getiri_1y", "3Y %": "getiri_3y",
    "Reel 1Y %": "reel_1y", "vs Mevduat": "vs_mevduat", "vs BIST100": "vs_bist100", "vs Altın": "vs_altin",
    "Vol %": "volatilite", "MaxDD %": "max_drawdown", "Sharpe": "sharpe", "Calmar": "calmar",
    "Hisse %": "d_hisse", "Tahvil %": "d_tahvil", "Repo %": "d_repo", "Mevduat %": "d_mevduat",
    "Altın %": "d_altin", "Döviz %": "d_doviz", "Fon %": "d_fon", "Yatırımcı": "yatirimci",
    "Büyüklük": "buyukluk", "Risk skor (anomali)": "risk_skor", "Likidite skor (0-100)": "likidite_skor",
    "Likidite seviye": "likidite_seviye", "Likit varlık %": "likit_varlik_pct",
    "Risk bayrakları": "risk_bayrak", "Max günlük %": "en_iyi_gun", "Min günlük %": "en_kotu_gun",
    "Sıçrama sayısı": "jump_sayisi", "Kısa vol %": "vol_kisa", "Vol oran (kısa/uzun)": "vol_oran",
    "Getiri z-skor (akran)": "getiri_z", "CAGR %": "cagr", "Kazanma %": "kazanma_orani",
    "VaR %95": "var95", "cVaR %95": "cvar95", "Puan": "puan", "Sıra": "sira",
}


# ═══════════════════════════════════════════════════════════════════════════
# 2) SAF MOTOR
# ═══════════════════════════════════════════════════════════════════════════
def fmt(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(x):
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def empty_spec() -> dict:
    return {"ranges": {}, "cats": {}, "cats_not": {}, "text": "", "text_not": "",
            "flags": {"temiz_bayrak": False, "her_donem_pozitif": False}, "nan_pass": False}


def prepare_universe(df: pd.DataFrame) -> pd.DataFrame:
    """Başlıkları normalle, kod tekilleştir, sayısal sütunları dönüştür."""
    d = df.copy()
    d = d.rename(columns={k: v for k, v in HEADER_MAP.items() if k in d.columns and v not in d.columns})
    if "kod" not in d.columns:
        raise ValueError("Veride 'kod' (Fon Kodu) sütunu yok.")
    d["kod"] = d["kod"].astype(str).str.strip().str.upper()
    d = d.drop_duplicates("kod").reset_index(drop=True)
    for c in CRITERIA:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    for c in ("ad", "kurucu", "kategori", "baskin_tur", "likidite_seviye", "risk_bayrak"):
        if c not in d.columns:
            d[c] = ""
    return d


def available_criteria(d: pd.DataFrame, schema: Optional["Schema"] = None) -> List[str]:
    sc = schema or FUND_SCHEMA
    return [k for k in sc.criteria if k in d.columns and d[k].notna().any()]


def preset_to_spec(pr: dict) -> dict:
    """core.AKILLI_FILTRE_PRESET girdisini filtre spesifikasyonuna çevirir."""
    sp = empty_spec()
    r = sp["ranges"]

    def put(col, lo=None, hi=None):
        if lo is not None or hi is not None:
            r[col] = (None if lo is None else float(lo), None if hi is None else float(hi))

    put("risk_skor", hi=pr.get("max_risk_skor"))
    put("likidite_skor", lo=pr.get("min_liq"))
    put("kazanma_orani", lo=pr.get("min_kazanma"))
    put("en_kotu_gun", lo=pr.get("min_en_kotu"))
    put("sharpe", lo=pr.get("min_sharpe"))
    put("sortino", lo=pr.get("min_sortino"))
    put("max_drawdown", lo=pr.get("max_dd"))
    put("volatilite", hi=pr.get("max_vol"))
    if pr.get("max_var95") is not None:
        put("var95", lo=-abs(float(pr["max_var95"])))
    if pr.get("max_cvar95") is not None:
        put("cvar95", lo=-abs(float(pr["max_cvar95"])))
    put("cagr", lo=pr.get("min_cagr"))
    put("calmar", lo=pr.get("min_calmar"))
    put("bist_korelasyon", hi=pr.get("max_bist_korr"))
    put("getiri_1a", lo=pr.get("min_1a"))
    put("getiri_3a", lo=pr.get("min_3a"))
    if pr.get("min_reel"):
        put("reel_1y", lo=0.0)
    sp["flags"]["temiz_bayrak"] = bool(pr.get("temiz_bayrak"))
    return sp


def _range_label(col: str, lo, hi, schema: Optional["Schema"] = None) -> str:
    name = (schema or FUND_SCHEMA).criteria.get(col, {}).get("label", col)
    if lo is not None and hi is not None:
        return f"{name}: {fmt(lo)} … {fmt(hi)}"
    if lo is not None:
        return f"{name} ≥ {fmt(lo)}"
    return f"{name} ≤ {fmt(hi)}"


def _flag_temiz(d: pd.DataFrame, nan_pass: bool) -> pd.Series:
    b = d["risk_bayrak"].fillna("").astype(str).str.strip().str.lower() if "risk_bayrak" in d.columns \
        else pd.Series("", index=d.index)
    return b.isin(["", "nan", "none", "-"])


def _flag_poz(d: pd.DataFrame, nan_pass: bool) -> pd.Series:
    ok = pd.Series(True, index=d.index)
    for c in ("getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y"):
        if c in d.columns:
            s = pd.to_numeric(d[c], errors="coerce")
            ok &= (s > 0) | (s.isna() if nan_pass else False)
    return ok


FUND_SCHEMA = Schema(
    criteria=CRITERIA, group_order=GROUP_ORDER, cat_cols=CAT_COLS,
    id_col="kod", name_col="ad", text_cols=("kod", "ad", "kurucu"),
    flags={"temiz_bayrak": ("Risk bayrağı yok", _flag_temiz),
           "her_donem_pozitif": ("1A·3A·6A·1Y hepsi pozitif", _flag_poz)},
)


def build_rules(d: pd.DataFrame, spec: dict, schema: Optional[Schema] = None) -> List[dict]:
    """Her aktif kriter için {id,label,mask,...} üretir."""
    sc = schema or FUND_SCHEMA
    rules: List[dict] = []
    nan_pass = bool(spec.get("nan_pass"))
    idx = d.index

    for col, (lo, hi) in (spec.get("ranges") or {}).items():
        if col not in d.columns or (lo is None and hi is None):
            continue
        s = pd.to_numeric(d[col], errors="coerce")
        ok = pd.Series(True, index=idx)
        if lo is not None:
            ok &= s >= lo
        if hi is not None:
            ok &= s <= hi
        ok = (ok | s.isna()) if nan_pass else (ok & s.notna())
        rules.append({"id": col, "kind": "range", "col": col, "lo": lo, "hi": hi,
                      "label": _range_label(col, lo, hi, sc), "mask": ok})

    for col, vals in (spec.get("cats") or {}).items():
        if col not in d.columns or not vals:
            continue
        ok = d[col].astype(str).isin([str(v) for v in vals])
        rules.append({"id": f"cat_{col}", "kind": "cat", "col": col,
                      "label": f"{sc.cat_cols.get(col, col)} ∈ {{{', '.join(map(str, vals))[:60]}}}", "mask": ok})

    for col, vals in (spec.get("cats_not") or {}).items():
        if col not in d.columns or not vals:
            continue
        ok = ~d[col].astype(str).isin([str(v) for v in vals])
        rules.append({"id": f"catnot_{col}", "kind": "cat", "col": col,
                      "label": f"{sc.cat_cols.get(col, col)} ∉ {{{', '.join(map(str, vals))[:60]}}}", "mask": ok})

    def _terms(t):
        return [x.strip().lower() for x in str(t or "").replace(";", ",").split(",") if x.strip()]

    parts = [d[c].astype(str) for c in sc.text_cols if c in d.columns]
    hay = parts[0] if parts else pd.Series("", index=idx)
    for extra in parts[1:]:
        hay = hay + " " + extra
    hay = hay.str.lower()
    inc = _terms(spec.get("text"))
    if inc:
        ok = pd.Series(False, index=idx)
        for t in inc:
            ok |= hay.str.contains(t, regex=False)
        rules.append({"id": "text", "kind": "text", "label": f"Metin içerir: {', '.join(inc)}", "mask": ok})
    exc = _terms(spec.get("text_not"))
    if exc:
        ok = pd.Series(True, index=idx)
        for t in exc:
            ok &= ~hay.str.contains(t, regex=False)
        rules.append({"id": "text_not", "kind": "text", "label": f"Metin içermez: {', '.join(exc)}", "mask": ok})

    for key, on in (spec.get("flags") or {}).items():
        if on and key in sc.flags:
            etiket, fn = sc.flags[key]
            rules.append({"id": f"flag_{key}", "kind": "flag", "label": etiket, "mask": fn(d, nan_pass)})
    return rules


def combine(d: pd.DataFrame, rules: List[dict]) -> pd.Series:
    ok = pd.Series(True, index=d.index)
    for r in rules:
        ok &= r["mask"]
    return ok


def funnel_table(rules: List[dict], n_total: int) -> pd.DataFrame:
    """Tek başına eleyen, yalnızca bu yüzden elenen ve sıralı huni."""
    if not rules:
        return pd.DataFrame(columns=["Kriter", "Tek başına eleyen", "Yalnızca bu yüzden elenen",
                                     "Sıralı kalan fon"])
    fails = pd.DataFrame({r["id"]: ~r["mask"] for r in rules})
    nfail = fails.sum(axis=1)
    rows = []
    for r in rules:
        f = fails[r["id"]]
        rows.append({"id": r["id"], "Kriter": r["label"], "Tek başına eleyen": int(f.sum()),
                     "Yalnızca bu yüzden elenen": int((f & (nfail == 1)).sum())})
    t = pd.DataFrame(rows).sort_values(["Tek başına eleyen", "Yalnızca bu yüzden elenen"], ascending=False)
    alive = pd.Series(True, index=fails.index)
    kalan = []
    for rid in t["id"]:
        alive &= ~fails[rid]
        kalan.append(int(alive.sum()))
    t["Sıralı kalan fon"] = kalan
    t["Eleme %"] = (t["Tek başına eleyen"] / max(n_total, 1) * 100).round(1)
    return t.drop(columns="id").reset_index(drop=True)


def near_misses(d: pd.DataFrame, rules: List[dict], max_fail: int = 1, schema: Optional[Schema] = None) -> pd.DataFrame:
    """En fazla `max_fail` kriteri kaçıran fonlar; eşiğe uzaklığa göre yakından uzağa."""
    sc = schema or FUND_SCHEMA
    if not rules:
        return pd.DataFrame()
    fails = pd.DataFrame({r["id"]: ~r["mask"] for r in rules})
    nfail = fails.sum(axis=1)
    idx = nfail[(nfail >= 1) & (nfail <= max_fail)].index
    if len(idx) == 0:
        return pd.DataFrame()
    spread = {r["col"]: float(pd.to_numeric(d[r["col"]], errors="coerce").std() or 1.0)
              for r in rules if r["kind"] == "range"}
    out_rows = []
    for i in idx:
        detay, gap = [], 0.0
        for r in rules:
            if r["mask"].loc[i]:
                continue
            if r["kind"] == "range":
                name = sc.criteria[r["col"]]["label"]
                v = d.at[i, r["col"]]
                if pd.isna(v):
                    detay.append(f"{name}: veri yok")
                    gap += 1.0
                    continue
                thr, yon = (r["lo"], "≥") if (r["lo"] is not None and v < r["lo"]) else (r["hi"], "≤")
                detay.append(f"{name} {fmt(v)} (gerekli {yon} {fmt(thr)})")
                gap += abs(float(v) - float(thr)) / (spread.get(r["col"]) or 1.0)
            else:
                detay.append(r["label"])
                gap += 1.0
        out_rows.append((i, "; ".join(detay), round(gap, 3), int(nfail.loc[i])))
    o = d.loc[[x[0] for x in out_rows]].copy()
    o["kaçırılan"] = [x[1] for x in out_rows]
    o["uzaklık"] = [x[2] for x in out_rows]
    o["kaçırılan_sayı"] = [x[3] for x in out_rows]
    return o.sort_values(["kaçırılan_sayı", "uzaklık"]).reset_index(drop=True)


def weighted_score(d: pd.DataFrame, weights: Dict[str, float], schema: Optional[Schema] = None) -> pd.Series:
    """Evren geneli yüzdelik sıra (0-100) ağırlıklı ortalaması. Eksik veri = 0 puan."""
    sc = schema or FUND_SCHEMA
    use = {c: float(w) for c, w in weights.items() if w and c in d.columns and d[c].notna().any()}
    tot = sum(use.values())
    if tot <= 0:
        return pd.Series(np.nan, index=d.index)
    acc = pd.Series(0.0, index=d.index)
    for c, w in use.items():
        s = pd.to_numeric(d[c], errors="coerce")
        pct = s.rank(pct=True, ascending=True)
        if sc.criteria.get(c, {}).get("better") is False:
            pct = 1.0 - pct + (1.0 / max(int(s.notna().sum()), 1))  # en düşük değer = en iyi
            pct = pct.clip(upper=1.0)
        acc += pct.fillna(0.0) * w
    return (acc / tot * 100).round(1)


def pareto_mask(d: pd.DataFrame, x: str, y: str, x_min: bool = True, y_max: bool = True) -> pd.Series:
    """x'i küçült/büyüt, y'yi büyüt/küçült — etkin (baskın olmayan) noktalar."""
    out = pd.Series(False, index=d.index)
    s = d[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    if s.empty:
        return out
    xs = s[x] if x_min else -s[x]
    ys = s[y] if y_max else -s[y]
    order = pd.DataFrame({"x": xs, "y": ys}).sort_values(["x", "y"], ascending=[True, False])
    best = -np.inf
    keep = []
    for i, row in order.iterrows():
        if row["y"] > best:
            keep.append(i)
            best = row["y"]
    out.loc[keep] = True
    return out


def diversified_pick(d_sorted: pd.DataFrame, n: int, max_per: Dict[str, int]) -> pd.DataFrame:
    """Sıralı listeden, her gruptan sınırlı sayıda fon seçer."""
    sayac: Dict[str, Dict[str, int]] = {c: {} for c in max_per}
    secilen = []
    for i, row in d_sorted.iterrows():
        if len(secilen) >= n:
            break
        ok = True
        for c, lim in max_per.items():
            if lim and c in d_sorted.columns:
                g = str(row.get(c))
                if sayac[c].get(g, 0) >= lim:
                    ok = False
                    break
        if not ok:
            continue
        for c in max_per:
            if c in d_sorted.columns:
                g = str(row.get(c))
                sayac[c][g] = sayac[c].get(g, 0) + 1
        secilen.append(i)
    return d_sorted.loc[secilen]


def explain_fund(d: pd.DataFrame, rules: List[dict], kod: str, schema: Optional[Schema] = None) -> pd.DataFrame:
    sc = schema or FUND_SCHEMA
    i = d.index[d[sc.id_col] == kod]
    if len(i) == 0:
        return pd.DataFrame()
    i = i[0]
    rows = []
    for r in rules:
        durum = "✅ geçti" if r["mask"].loc[i] else "❌ elendi"
        if r["kind"] == "range":
            v = d.at[i, r["col"]]
            esik = _range_label(r["col"], r["lo"], r["hi"], sc).split(":")[-1].strip()
            esik = esik.replace(sc.criteria[r["col"]]["label"], "").strip()
            rows.append({"Kriter": sc.criteria[r["col"]]["label"], "Fonun değeri": fmt(v), "Koşul": esik, "Durum": durum})
        else:
            rows.append({"Kriter": r["label"], "Fonun değeri": "", "Koşul": "", "Durum": durum})
    return pd.DataFrame(rows)


# ── Preset G/Ç ──────────────────────────────────────────────────────────────
def _spec_to_json(spec: dict) -> dict:
    s = json.loads(json.dumps(spec, default=lambda o: list(o) if isinstance(o, tuple) else str(o)))
    return s


def load_user_presets(path: Optional[Path] = None) -> Dict[str, dict]:
    path = Path(path) if path else PRESET_PATH
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_user_preset(name: str, spec: dict, path: Optional[Path] = None) -> None:
    path = Path(path) if path else PRESET_PATH
    p = load_user_presets(path)
    p[name] = _spec_to_json(spec)
    path.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_user_preset(name: str, path: Optional[Path] = None) -> None:
    path = Path(path) if path else PRESET_PATH
    p = load_user_presets(path)
    if name in p:
        del p[name]
        path.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_spec(sp: dict) -> dict:
    """JSON'dan gelen spesifikasyonu iç biçime çevirir."""
    s = empty_spec()
    s["ranges"] = {k: (v[0], v[1]) for k, v in (sp.get("ranges") or {}).items()}
    s["cats"] = {k: list(v) for k, v in (sp.get("cats") or {}).items()}
    s["cats_not"] = {k: list(v) for k, v in (sp.get("cats_not") or {}).items()}
    s["text"] = sp.get("text", "")
    s["text_not"] = sp.get("text_not", "")
    s["flags"].update(sp.get("flags") or {})
    s["nan_pass"] = bool(sp.get("nan_pass"))
    return s


# ═══════════════════════════════════════════════════════════════════════════
# 3) STREAMLIT SAYFASI
# ═══════════════════════════════════════════════════════════════════════════
DEFAULT_COLS = [
    "sira", "kod", "ad", "filtre_skor", "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y",
    "sharpe", "sortino", "max_drawdown", "volatilite", "kazanma_orani",
    "likidite_skor", "risk_skor", "baskin_tur", "kurucu", "bist_korelasyon",
]


def render(*, core, get_analiz, sepet_ekle, show_df, download_excel_button, fmt_num=None):
    import streamlit as st

    S = st.session_state

    # ── yardımcılar (oturum durumu) ─────────────────────────────────────────
    def _apply_spec(spec: dict, avail: List[str]):
        for k in list(S.keys()):
            if str(k).startswith(("flt_lo_", "flt_hi_", "flt_cat_")):
                del S[k]
        sel = [c for c in spec["ranges"] if c in avail]
        S["flt_sel"] = sel
        for c in sel:
            lo, hi = spec["ranges"][c]
            S[f"flt_lo_{c}"] = None if lo is None else float(lo)
            S[f"flt_hi_{c}"] = None if hi is None else float(hi)
        S["flt_cat_sel"] = list(spec["cats"].keys())
        for c, v in spec["cats"].items():
            S[f"flt_cat_{c}"] = list(v)
        S["flt_kurucu_haric"] = list((spec.get("cats_not") or {}).get("kurucu", []))
        S["flt_text"] = spec.get("text", "")
        S["flt_text_not"] = spec.get("text_not", "")
        S["flt_temiz"] = bool(spec["flags"].get("temiz_bayrak"))
        S["flt_poz"] = bool(spec["flags"].get("her_donem_pozitif"))
        S["flt_nan"] = bool(spec.get("nan_pass"))

    def _clear_all(avail):
        _apply_spec(empty_spec(), avail)

    def _apply_weights(profil: str):
        w = WEIGHT_PROFILES.get(profil, {})
        for c in WEIGHT_COLS:
            S[f"flt_w_{c}"] = int(w.get(c, 0))

    def _collect_spec() -> dict:
        sp = empty_spec()
        for c in S.get("flt_sel", []):
            sp["ranges"][c] = (S.get(f"flt_lo_{c}"), S.get(f"flt_hi_{c}"))
        for c in S.get("flt_cat_sel", []):
            v = S.get(f"flt_cat_{c}") or []
            if v:
                sp["cats"][c] = list(v)
        if S.get("flt_kurucu_haric"):
            sp["cats_not"]["kurucu"] = list(S["flt_kurucu_haric"])
        sp["text"] = S.get("flt_text", "")
        sp["text_not"] = S.get("flt_text_not", "")
        sp["flags"]["temiz_bayrak"] = bool(S.get("flt_temiz"))
        sp["flags"]["her_donem_pozitif"] = bool(S.get("flt_poz"))
        sp["nan_pass"] = bool(S.get("flt_nan"))
        return sp

    # ── başlık ──────────────────────────────────────────────────────────────
    st.header("🧪 Filtre Stüdyosu")
    st.caption(
        "Fon evrenini istediğiniz kriterlerle daraltın; her kriterin kaç fonu elediğini, "
        "hangi fonların eşiğe çok yakın kaçtığını ve sonucu risk-getiri haritasında görün. "
        "Boş bırakılan sınır = sınır yok."
    )

    # ── veri kaynağı ────────────────────────────────────────────────────────
    kaynaklar: Dict[str, pd.DataFrame] = {}
    for ad, key in (("Geniş evren (bu sayfadan yüklenen)", "filtre_evren"),
                    ("Son 'Tüm Fon Sıralaması' sonucu", "son_siralama"),
                    ("Son 'Fon Tarama' sonucu", "son_tarama"),
                    ("Yüklenen dosya", "filtre_upload")):
        v = S.get(key)
        if isinstance(v, pd.DataFrame) and not v.empty:
            kaynaklar[ad] = v

    with st.expander("📦 Veri kaynağı", expanded=not kaynaklar):
        up = st.file_uploader("CSV / Excel yükle (programın çıktısı olabilir)", type=["csv", "xlsx", "xls"],
                              key="flt_upload_file")
        if up is not None and S.get("_flt_up_name") != up.name:
            try:
                raw = pd.read_csv(up) if up.name.lower().endswith(".csv") else pd.read_excel(up)
                S["filtre_upload"] = prepare_universe(raw)
                S["_flt_up_name"] = up.name
                S["flt_kaynak"] = "Yüklenen dosya"
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Dosya okunamadı: {e}")
        st.markdown("**Geniş evreni canlı yükle** (pozitif-getiri şartı olmadan; filtreleme burada yapılır)")
        c1, c2 = st.columns([2, 1])
        lim = c1.number_input("En fazla kaç açık fon taransın? (0 = hepsi)", value=150, min_value=0,
                              max_value=5000, step=50, key="flt_lim",
                              help="Cache sonraki koşuları hızlandırır; tüm fonlar uzun sürebilir.")
        if c2.button("🚀 Evreni yükle", use_container_width=True, key="flt_evren_btn"):
            analiz = get_analiz()
            with st.spinner("Fonlar taranıyor…"):
                try:
                    ev = analiz.tum_fon_siralama(
                        sadece_pozitif_1y=False, pozitif_3y=False, max_liste=10000,
                        max_tara=int(lim) if lim else None, skor_sistemi="gelismis",
                        skor_agirlik=core.GELISMIS_PROFILLER["2"][1],
                    )
                    if ev is not None and not ev.empty:
                        S["filtre_evren"] = prepare_universe(ev)
                        S["flt_kaynak"] = "Geniş evren (bu sayfadan yüklenen)"
                        st.rerun()
                    else:
                        st.warning("Veri alınamadı.")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Hata: {e}")

    if not kaynaklar:
        st.info("Başlamak için bir veri kaynağı seçin: yukarıdan evreni yükleyin, dosya yükleyin "
                "ya da önce 'Tüm Fon Sıralaması' / 'Fon Tarama' sayfasında bir tarama çalıştırın.")
        return

    names = list(kaynaklar.keys())
    if S.get("flt_kaynak") not in names:
        S["flt_kaynak"] = names[0]
    kaynak = st.radio("Kaynak", names, key="flt_kaynak", horizontal=True, label_visibility="collapsed")
    try:
        d = prepare_universe(kaynaklar[kaynak])
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    import tefas_ek as TE
    with st.expander("📡 Ek metrikler (fon yaşı, süreklilik, alfa/beta, akış, valör, TER/stopaj…)"):
        _akis = TE.hesap_kontrolleri(st, core, get_analiz, d, "flt")
    d = TE.hazirla(d, S.get("ek_metrik"), S.get("ek_veri"), _akis, core)
    avail = available_criteria(d)
    avail_sorted = sorted(avail, key=lambda k: (GROUP_ORDER.index(CRITERIA[k]["group"]), list(CRITERIA).index(k)))

    # ── presetler ───────────────────────────────────────────────────────────
    st.subheader("1 · Hazır profil")
    pk = list(core.AKILLI_FILTRE_PRESET.keys())
    cols = st.columns(min(len(pk), 4) + 1)
    for i, k in enumerate(pk):
        with cols[i % 4]:
            st.button(k, key=f"flt_pr_{k}", use_container_width=True, help=core.AKILLI_FILTRE_PRESET[k].get("ad"),
                      on_click=_apply_spec, args=(preset_to_spec(core.AKILLI_FILTRE_PRESET[k]), avail))
    with cols[-1]:
        st.button("🧹 Hepsini temizle", key="flt_clear", use_container_width=True,
                  on_click=_clear_all, args=(avail,))

    up_presets = load_user_presets()
    pc1, pc2, pc3 = st.columns([2, 2, 2])
    with pc1:
        kayitli = st.selectbox("Kayıtlı filtrelerim", ["—"] + list(up_presets.keys()), key="flt_user_sel")
        b1, b2 = st.columns(2)
        if kayitli != "—":
            b1.button("📂 Yükle", key="flt_user_load", on_click=_apply_spec,
                      args=(normalize_spec(up_presets[kayitli]), avail))
            if b2.button("🗑️ Sil", key="flt_user_del"):
                delete_user_preset(kayitli)
                st.rerun()
    with pc2:
        yeni_ad = st.text_input("Mevcut filtreyi kaydet", placeholder="örn. Benim düşük riskli listem",
                                key="flt_save_name")
    with pc3:
        st.write("")
        st.write("")
        kaydet_btn = st.button("💾 Kaydet", key="flt_save_btn")

    # ── kriter seçimi ───────────────────────────────────────────────────────
    st.subheader("2 · Kriterler")
    st.multiselect(
        "Sayısal kriterler (ekledikçe aşağıda alt/üst sınır kutuları açılır)",
        options=avail_sorted,
        format_func=lambda k: f"{CRITERIA[k]['group']} · {CRITERIA[k]['label']}",
        key="flt_sel",
        placeholder="Kriter ekleyin (Sharpe, Max DD, Likidite…)",
    )
    for c in S.get("flt_sel", []):
        S.setdefault(f"flt_lo_{c}", None)
        S.setdefault(f"flt_hi_{c}", None)
        ser = pd.to_numeric(d[c], errors="coerce").dropna()
        r1, r2, r3, r4 = st.columns([2.4, 1, 1, 1.4])
        r1.markdown(f"**{CRITERIA[c]['label']}**")
        r1.caption(f"min {fmt(ser.min())} · medyan {fmt(ser.median())} · maks {fmt(ser.max())} "
                   f"· veri: {len(ser)}/{len(d)}")
        r2.number_input("Alt sınır", value=None, step=float(CRITERIA[c]["step"]), format="%g",
                        key=f"flt_lo_{c}", placeholder="yok", label_visibility="collapsed")
        r3.number_input("Üst sınır", value=None, step=float(CRITERIA[c]["step"]), format="%g",
                        key=f"flt_hi_{c}", placeholder="yok", label_visibility="collapsed")
        lo, hi = S.get(f"flt_lo_{c}"), S.get(f"flt_hi_{c}")
        if lo is not None or hi is not None:
            ok = pd.Series(True, index=ser.index)
            if lo is not None:
                ok &= ser >= lo
            if hi is not None:
                ok &= ser <= hi
            r4.caption(f"tek başına eler: **{int((~ok).sum())}**")
        else:
            r4.caption("henüz sınır yok")

    with st.expander("Kategori, metin ve diğer şartlar", expanded=bool(S.get("flt_cat_sel") or S.get("flt_text"))):
        cat_avail = [c for c in CAT_COLS if c in d.columns and d[c].astype(str).str.strip().ne("").any()]
        st.multiselect("Kategorik filtre ekle", cat_avail, format_func=lambda c: CAT_COLS[c], key="flt_cat_sel")
        for c in S.get("flt_cat_sel", []):
            vals = sorted(d[c].astype(str).replace("nan", "").loc[lambda s: s != ""].unique().tolist())
            fmt_fn = (lambda v: core.TUR_ACIKLAMA.get(v, v)) if c == "baskin_tur" else (lambda v: v)
            st.multiselect(CAT_COLS[c], vals, format_func=fmt_fn, key=f"flt_cat_{c}")
        _kur = sorted(x for x in d["kurucu"].astype(str).unique() if x and x != "nan")
        st.multiselect("Şu kurucuları HARİÇ tut", _kur, key="flt_kurucu_haric")
        t1, t2 = st.columns(2)
        t1.text_input("Kod / ad / kurucu içerir (virgülle ayır → biri yeter)", key="flt_text",
                      placeholder="katılım, altın")
        t2.text_input("Şunları içermesin", key="flt_text_not", placeholder="döviz, serbest")
        f1, f2, f3 = st.columns(3)
        f1.checkbox("Risk bayrağı olmayan fonlar", key="flt_temiz")
        f2.checkbox("1A·3A·6A·1Y hepsi pozitif", key="flt_poz")
        f3.checkbox("Verisi eksik fonlar elenmesin", key="flt_nan",
                    help="Kapalıyken, bir kriter için verisi olmayan fon elenir (daha temkinli).")

    spec = _collect_spec()
    if kaydet_btn:
        if yeni_ad.strip():
            save_user_preset(yeni_ad.strip(), spec)
            st.success(f"'{yeni_ad.strip()}' kaydedildi.")
        else:
            st.warning("Kaydetmek için bir ad yazın.")

    # ── skor / sıralama ─────────────────────────────────────────────────────
    st.subheader("3 · Sıralama")
    with st.expander("Özel skor ağırlıkları (evren genelinde yüzdelik sıra ortalaması)", expanded=False):
        wp = st.radio("Ağırlık profili", list(WEIGHT_PROFILES), horizontal=True, key="flt_wprof",
                      on_change=lambda: _apply_weights(S["flt_wprof"]))
        for c in WEIGHT_COLS:
            if f"flt_w_{c}" not in S:
                S[f"flt_w_{c}"] = int(WEIGHT_PROFILES[wp].get(c, 0))
        wcols = st.columns(4)
        w_av = [c for c in WEIGHT_COLS if c in d.columns and d[c].notna().any()]
        for i, c in enumerate(w_av):
            wcols[i % 4].slider(CRITERIA[c]["label"], 0, 40, key=f"flt_w_{c}")
        st.caption("Düşük-iyi ölçütler (volatilite, komisyon, risk skoru…) otomatik ters çevrilir. "
                   "Veri olmayan fon o ölçütten 0 puan alır.")
    d["filtre_skor"] = weighted_score(d, {c: S.get(f"flt_w_{c}", 0) for c in WEIGHT_COLS})

    sort_opts = ["filtre_skor"] + [c for c in ("puan", "puan_gelismis", "secim_skor", "akilli_skor")
                                   if c in d.columns and d[c].notna().any()] + avail_sorted
    sort_opts = list(dict.fromkeys(sort_opts))
    s1, s2 = st.columns([3, 1])
    sort_col = s1.selectbox("Sıralama ölçütü", sort_opts, key="flt_sort",
                            format_func=lambda k: {"filtre_skor": "Özel ağırlıklı skor (yeni)",
                                                   "puan": "Puan", "puan_gelismis": "Gelişmiş puan",
                                                   "secim_skor": "Seçim skoru",
                                                   "akilli_skor": "Akıllı skor"}.get(k, CRITERIA.get(k, {}).get("label", k)))
    varsayilan_artan = CRITERIA.get(sort_col, {}).get("better") is False
    ters = s2.checkbox("Küçükten büyüğe", value=varsayilan_artan, key=f"flt_asc_{sort_col}")

    # ── uygula ──────────────────────────────────────────────────────────────
    rules = build_rules(d, spec)
    ok = combine(d, rules)
    gecen = d[ok].sort_values(sort_col, ascending=ters, na_position="last").reset_index(drop=True)
    if "sira" in gecen.columns:
        gecen = gecen.drop(columns=["sira"])
    gecen.insert(0, "sira", range(1, len(gecen) + 1))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Evren", len(d))
    m2.metric("Filtreyi geçen", len(gecen))
    m3.metric("Geçme oranı", f"%{len(gecen) / max(len(d), 1) * 100:.0f}")
    if len(gecen) and "sharpe" in gecen.columns and gecen["sharpe"].notna().any():
        m4.metric("Medyan Sharpe (geçen)", fmt(gecen["sharpe"].median()))
    else:
        m4.metric("Aktif kriter", len(rules))

    ad_map = {str(r["kod"]): str(r.get("ad") or r["kod"]) for _, r in d.iterrows()}

    def _secim_araci(frame: pd.DataFrame, key: str, varsayilan: int = 5):
        kodlar = frame["kod"].astype(str).tolist()
        sec = st.multiselect("Eklenecek fonlar", kodlar, default=kodlar[:varsayilan], key=f"flt_pick_{key}",
                             format_func=lambda k: f"{k} — {ad_map.get(k, '')[:42]}")
        a, b = st.columns(2)
        if a.button("⭐ Favoriye ekle", key=f"flt_fav_{key}") and sec:
            core.save_favs(list(set(core.load_favs()) | set(sec)))
            st.success(f"{len(sec)} fon favorilere eklendi.")
        if b.button("🛒 Sepete ekle", key=f"flt_sepet_{key}") and sec:
            n = sepet_ekle(sec, ad_map=ad_map, kaynak="filtre")
            st.success(f"{n} fon sepete eklendi (toplam {len(S.portfoy_sepet)}).")

    t_son, t_huni, t_yakin, t_harita, t_cesit, t_mercek = st.tabs(
        ["📋 Sonuçlar", "🔻 Huni", "🎯 Yakın kaçanlar", "🗺️ Risk-getiri haritası",
         "🧩 Çeşitlendirilmiş seçim", "🔬 Fon mercek"]
    )

    # ── Sonuçlar ────────────────────────────────────────────────────────────
    with t_son:
        if not rules:
            st.info("Henüz kriter yok — tüm evren sıralanıyor. Hazır bir profil seçin veya kriter ekleyin.")
        if gecen.empty:
            st.warning("Hiçbir fon tüm kriterleri geçemedi.")
            ft = funnel_table(rules, len(d))
            if not ft.empty:
                en = ft.iloc[0]
                st.markdown(f"En kısıtlayıcı kriter: **{en['Kriter']}** (tek başına {en['Tek başına eleyen']} fonu eliyor). "
                            "'Huni' ve 'Yakın kaçanlar' sekmelerine bakın.")
        else:
            with st.expander("Gösterilecek sütunlar"):
                all_cols = [c for c in (["sira", "kod", "ad", "filtre_skor", "puan", "secim_skor"] + avail_sorted
                                        + ["baskin_tur", "kurucu", "kategori", "likidite_seviye", "risk_bayrak"])
                            if c in gecen.columns]
                goster = st.multiselect("Sütunlar", all_cols, default=[c for c in DEFAULT_COLS if c in all_cols],
                                        key="flt_cols", label_visibility="collapsed")
            show_df(gecen, goster or DEFAULT_COLS, height=480)
            _secim_araci(gecen, "son")
            e1, e2 = st.columns(2)
            with e1:
                download_excel_button(gecen[[c for c in (goster or DEFAULT_COLS) if c in gecen.columns]],
                                      "filtre_sonuc.xlsx", meta={"mod": "filtre_studyosu", "filtre": [r["label"] for r in rules]},
                                      key="flt_xl", auto_save=False)
            with e2:
                st.download_button("⬇️ CSV", gecen.to_csv(index=False).encode("utf-8-sig"),
                                   file_name="filtre_sonuc.csv", mime="text/csv", key="flt_csv")
        with st.expander("Aktif filtrenin özeti / paylaş (JSON)"):
            st.write("\n".join(f"- {r['label']}" for r in rules) or "Aktif kriter yok.")
            st.code(json.dumps(_spec_to_json(spec), ensure_ascii=False, indent=2), language="json")

    # ── Huni ────────────────────────────────────────────────────────────────
    with t_huni:
        ft = funnel_table(rules, len(d))
        if ft.empty:
            st.info("Huni için en az bir kriter ekleyin.")
        else:
            st.caption("**Tek başına eleyen**: kriterin kendi başına elediği fon. **Yalnızca bu yüzden elenen**: "
                       "sadece bu kriter yüzünden kaybedilen fon — gevşetirseniz geri gelir. "
                       "**Sıralı kalan**: en sert kriterden başlayarak kalan fon sayısı.")
            st.dataframe(ft, hide_index=True, use_container_width=True)
            st.bar_chart(ft.set_index("Kriter")[["Tek başına eleyen", "Yalnızca bu yüzden elenen"]])
            en = ft.sort_values("Yalnızca bu yüzden elenen", ascending=False).iloc[0]
            if en["Yalnızca bu yüzden elenen"] > 0:
                st.success(f"En çok fırsat: **{en['Kriter']}** kriterini gevşetirseniz "
                           f"**{en['Yalnızca bu yüzden elenen']}** fon daha geçer.")

    # ── Yakın kaçanlar ──────────────────────────────────────────────────────
    with t_yakin:
        if not rules:
            st.info("Önce kriter ekleyin.")
        else:
            mf = st.slider("En fazla kaç kriteri kaçırmış olsun?", 1, 3, 1, key="flt_mf")
            nm = near_misses(d, rules, max_fail=mf)
            if nm.empty:
                st.success("Yakın kaçan fon yok.")
            else:
                st.caption("Eşiği az farkla kaçıranlar önce. 'Uzaklık' = eşiğe mesafenin evren std. sapmasına oranı toplamı.")
                show_df(nm, ["kod", "ad", "kaçırılan", "uzaklık", "getiri_1y", "sharpe", "max_drawdown", "volatilite"], height=420)
                _secim_araci(nm.head(50), "yakin", varsayilan=0)

    # ── Harita ──────────────────────────────────────────────────────────────
    with t_harita:
        num_av = avail_sorted
        if len(num_av) < 2:
            st.info("Harita için en az iki sayısal sütun gerekir.")
        else:
            h1, h2, h3 = st.columns(3)
            xi = num_av.index("volatilite") if "volatilite" in num_av else 0
            yi = num_av.index("getiri_1y") if "getiri_1y" in num_av else min(1, len(num_av) - 1)
            xc = h1.selectbox("X ekseni", num_av, index=xi, key="flt_x", format_func=lambda k: CRITERIA[k]["label"])
            yc = h2.selectbox("Y ekseni", num_av, index=yi, key="flt_y", format_func=lambda k: CRITERIA[k]["label"])
            goster_elenen = h3.checkbox("Elenenleri de göster", value=True, key="flt_show_out")
            xmin = CRITERIA[xc]["better"] is not True
            ymax = CRITERIA[yc]["better"] is not False
            pm = pareto_mask(d[ok], xc, yc, x_min=xmin, y_max=ymax)
            pl = d[[xc, yc, "kod"]].copy()
            pl["Grup"] = np.where(~ok, "Elenen", np.where(pm.reindex(d.index, fill_value=False), "★ Etkin sınır", "Filtreyi geçen"))
            if not goster_elenen:
                pl = pl[pl["Grup"] != "Elenen"]
            pl = pl.dropna(subset=[xc, yc])
            st.scatter_chart(pl, x=xc, y=yc, color="Grup", height=420)
            st.caption(f"★ Etkin sınır: filtreyi geçenler arasında {CRITERIA[xc]['label']} "
                       f"{'düşük' if xmin else 'yüksek'}, {CRITERIA[yc]['label']} "
                       f"{'yüksek' if ymax else 'düşük'} yönünde başka bir fon tarafından geçilemeyenler.")
            pf = d[ok][pm.reindex(d[ok].index, fill_value=False)].sort_values(yc, ascending=not ymax)
            if not pf.empty:
                show_df(pf, ["kod", "ad", xc, yc, "sharpe", "max_drawdown", "likidite_skor", "baskin_tur"], height=300)

    # ── Çeşitlendirilmiş seçim ──────────────────────────────────────────────
    with t_cesit:
        if gecen.empty:
            st.info("Önce filtreyi geçen fon olmalı.")
        else:
            k1, k2, k3, k4 = st.columns(4)
            n_pick = k1.slider("Fon sayısı", 3, 20, 10, key="flt_npick")
            mt = k2.slider("Aynı türden max", 1, 6, 3, key="flt_mt")
            mk = k3.slider("Aynı kurucudan max", 1, 6, 2, key="flt_mk")
            yontem = k4.selectbox("Ağırlık yöntemi", ["max_sharpe", "risk_parity", "score", "equal"], key="flt_ym")
            sec = diversified_pick(gecen, n_pick, {"baskin_tur": mt, "kurucu": mk})
            st.caption(f"Sıralama: {sort_col} · sonuç listesinin en iyilerinden, tür/kurucu sınırlarıyla {len(sec)} fon.")
            show_df(sec, ["sira", "kod", "ad", "baskin_tur", "kurucu", "filtre_skor", "getiri_1y", "sharpe", "max_drawdown"], height=360)
            _secim_araci(sec, "cesit", varsayilan=len(sec))
            if st.button("⚖️ Ağırlıkları hesapla", key="flt_w_btn") and len(sec) >= 2:
                with st.spinner("Optimizasyon…"):
                    try:
                        res = core.akilli_portfoy_sec(get_analiz(), sec.copy(), n_fon=len(sec), yontem=yontem,
                                                      max_w=0.25, max_ayni_tur=mt, diversify=False)
                    except Exception as e:  # noqa: BLE001
                        res = {"error": str(e)}
                if res.get("error"):
                    st.error(res["error"])
                else:
                    w = res.get("weights") or {}
                    wt = pd.DataFrame({"Fon": list(w.keys()), "Ağırlık %": [round(v * 100, 1) for v in w.values()]})
                    st.dataframe(wt.sort_values("Ağırlık %", ascending=False), hide_index=True, use_container_width=True)
                    if res.get("port_sharpe") is not None:
                        a1, a2, a3 = st.columns(3)
                        a1.metric("Beklenen μ", f"%{res.get('port_mu')}")
                        a2.metric("Vol", f"%{res.get('port_vol')}")
                        a3.metric("Sharpe", f"{res.get('port_sharpe')}")

    # ── Mercek ──────────────────────────────────────────────────────────────
    with t_mercek:
        kod = st.selectbox("Fon", d["kod"].tolist(), key="flt_lens",
                           format_func=lambda k: f"{k} — {ad_map.get(k, '')[:50]}")
        i = d.index[d["kod"] == kod][0]
        st.markdown(f"**{kod}** · {ad_map.get(kod, '')}  →  "
                    f"{'✅ filtreyi geçiyor' if bool(ok.loc[i]) else '❌ filtreye takılıyor'}")
        ex = explain_fund(d, rules, kod)
        if ex.empty:
            st.info("Aktif kriter yok.")
        else:
            st.dataframe(ex, hide_index=True, use_container_width=True)
        pr_rows = []
        for c in avail_sorted:
            v = d.at[i, c]
            if pd.isna(v):
                continue
            pct = float(d[c].rank(pct=True).loc[i]) * 100
            iyi = CRITERIA[c]["better"]
            if iyi is False:
                pct = 100 - pct
            pr_rows.append({"Ölçüt": CRITERIA[c]["label"], "Değer": fmt(v),
                            "Evren içi konum": f"üst %{max(100 - pct, 1):.0f}" if iyi is not None else f"yüzdelik {pct:.0f}"})
        if pr_rows:
            with st.expander("Evren içindeki konumu", expanded=False):
                st.dataframe(pd.DataFrame(pr_rows), hide_index=True, use_container_width=True)


# Ek kriterleri kaydet (tefas_ek, bu modül yüklendikten sonra)
try:  # noqa: SIM105
    import tefas_ek  # noqa: F401
except Exception:  # pragma: no cover
    pass
