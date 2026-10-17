# -*- coding: utf-8 -*-
"""
TEFAS ek filtreler
==================
Mevcut fiyat geçmişi / profil / özet verisinden türetilen ek ölçütler:

  Süreklilik      : fon yaşı, pozitif ay oranı, kategori medyanını geçen ay oranı,
                    MaxDD sonrası toparlanma süresi, zirveden uzaklık
  Piyasa ilişkisi : BIST100'e göre alfa / beta
  Kategori içi    : Sharpe, Sortino, getiri, drawdown, volatilite yüzdelik sırası
  Yoğunlaşma      : en büyük varlık payı, yoğunlaşma endeksi, repo+mevduat payı
  Akış            : fon büyüklüğü / yatırımcı sayısı değişimi (günlük anlık görüntüden)
  İşlem           : geri alım valörü, son işlem saati
  Maliyet         : TER, stopaj (TEFAS API'sinde yok → isteğe bağlı CSV ile)
  Vergi grubu     : varlık dağılımından yaklaşık sınıflama (oran içermez)

Saf fonksiyonlar Streamlit gerektirmez. panel() Streamlit arayüzüdür.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import tefas_filtre as TF

# ═══════════════════════════════════════════════════════════════════════════
# Kriter kaydı (tefas_filtre.CRITERIA'ya eklenir)
# ═══════════════════════════════════════════════════════════════════════════
EK_GRUPLAR = ["Süreklilik", "Piyasa ilişkisi", "Kategori içi sıra", "Yoğunlaşma",
              "Akış", "İşlem", "Maliyet (ek veri)"]

EK_CRIT = [
    ("fon_yasi_ay", "Fiyat geçmişi / fon yaşı (ay, en çok 36)", "Süreklilik", 3.0, True),
    ("pozitif_ay_orani", "Pozitif ay oranı % (son 12 ay)", "Süreklilik", 5.0, True),
    ("ay_ustu_orani", "Kategori medyanını geçen ay % (son 12 ay)", "Süreklilik", 5.0, True),
    ("toparlanma_gun", "MaxDD sonrası toparlanma (gün)", "Süreklilik", 10.0, False),
    ("tepeden_uzaklik", "Zirveden uzaklık % (negatif)", "Süreklilik", 1.0, True),
    ("alfa_bist", "Alfa vs BIST100 (yıllık %)", "Piyasa ilişkisi", 1.0, True),
    ("beta_bist", "Beta vs BIST100", "Piyasa ilişkisi", 0.1, None),
    ("kyz_sharpe", "Kategori içi Sharpe yüzdeliği (100 = en iyi)", "Kategori içi sıra", 5.0, True),
    ("kyz_sortino", "Kategori içi Sortino yüzdeliği", "Kategori içi sıra", 5.0, True),
    ("kyz_getiri_6a", "Kategori içi 6A getiri yüzdeliği", "Kategori içi sıra", 5.0, True),
    ("kyz_getiri_1y", "Kategori içi 1Y getiri yüzdeliği", "Kategori içi sıra", 5.0, True),
    ("kyz_max_drawdown", "Kategori içi drawdown yüzdeliği", "Kategori içi sıra", 5.0, True),
    ("kyz_volatilite", "Kategori içi düşük volatilite yüzdeliği", "Kategori içi sıra", 5.0, True),
    ("en_buyuk_varlik_pct", "En büyük varlık payı %", "Yoğunlaşma", 5.0, None),
    ("hhi_varlik", "Varlık yoğunlaşma endeksi (0-100)", "Yoğunlaşma", 5.0, None),
    ("repo_mevduat_pct", "Repo + mevduat %", "Yoğunlaşma", 5.0, None),
    ("buyukluk_degisim_pct", "Fon büyüklüğü değişimi % (anlık görüntü)", "Akış", 5.0, True),
    ("yatirimci_degisim_pct", "Yatırımcı sayısı değişimi %", "Akış", 1.0, True),
    ("valor_geri_alis", "Geri alım valörü (T+gün)", "İşlem", 1.0, False),
    ("son_islem_saat", "Son işlem saati (13.5 = 13:30)", "İşlem", 0.5, True),
    ("ter", "Toplam gider oranı % (ek veri)", "Maliyet (ek veri)", 0.1, False),
    ("stopaj", "Stopaj % (ek veri)", "Maliyet (ek veri)", 1.0, False),
]
EK_KOLON_LISTESI = [k for k, *_ in EK_CRIT]
# Ağdan hesaplanan (Hesapla düğmesi gereken) sütunlar
AG_KOLONLARI = ["fon_yasi_ay", "pozitif_ay_orani", "ay_ustu_orani", "toparlanma_gun",
                "tepeden_uzaklik", "alfa_bist", "beta_bist", "son_islem_saat"]
EK_CAT = {"vergi_grubu": "Vergi grubu (yaklaşık)"}


def kaydet_kriterler() -> None:
    """Ek kriterleri filtre motoruna kaydeder (idempotent)."""
    for g in EK_GRUPLAR:
        if g not in TF.GROUP_ORDER:
            TF.GROUP_ORDER.append(g)
    for k, l, g, s, b in EK_CRIT:
        TF.CRITERIA.setdefault(k, {"label": l, "group": g, "step": s, "better": b})
    for k, v in EK_CAT.items():
        TF.CAT_COLS.setdefault(k, v)
    for c in ("kyz_sharpe", "kyz_getiri_1y", "kyz_max_drawdown", "pozitif_ay_orani",
              "ay_ustu_orani", "kyz_sortino", "kyz_volatilite", "kyz_getiri_6a"):
        if c not in TF.WEIGHT_COLS:
            TF.WEIGHT_COLS.append(c)


kaydet_kriterler()

# ═══════════════════════════════════════════════════════════════════════════
# Saf hesaplar (ağ gerektirmez)
# ═══════════════════════════════════════════════════════════════════════════
def _tr_low(x) -> str:
    s = str(x or "")
    tr = str.maketrans({"İ": "i", "I": "ı", "ı": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                        "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})
    return s.translate(tr).lower()


def _grup_kolonu(d: pd.DataFrame) -> pd.Series:
    k = d["kategori"].astype(str).str.strip() if "kategori" in d.columns else pd.Series("", index=d.index)
    b = d["baskin_tur"].astype(str).str.strip() if "baskin_tur" in d.columns else pd.Series("", index=d.index)
    k = k.where(~k.isin(["", "nan", "None"]), b)
    return k.where(~k.isin(["", "nan", "None"]), "Bilinmiyor")


KYZ_ESLEME = {  # sütun → (kaynak, yüksek_iyi)
    "kyz_sharpe": ("sharpe", True), "kyz_sortino": ("sortino", True),
    "kyz_getiri_6a": ("getiri_6a", True), "kyz_getiri_1y": ("getiri_1y", True),
    "kyz_max_drawdown": ("max_drawdown", True), "kyz_volatilite": ("volatilite", False),
}


def kategori_ici_yuzdelik(d: pd.DataFrame, min_grup: int = 5) -> pd.DataFrame:
    """Her fonun kendi kategorisi içindeki yüzdelik sırası (100 = kategorisinin en iyisi)."""
    out = d.copy()
    g = _grup_kolonu(out)
    n = g.map(g.value_counts())
    for yeni, (kaynak, yuksek) in KYZ_ESLEME.items():
        if kaynak not in out.columns:
            continue
        s = pd.to_numeric(out[kaynak], errors="coerce")
        pct = s.groupby(g).rank(pct=True, ascending=yuksek) * 100
        out[yeni] = pct.where(n >= min_grup).round(1)
    return out


def yogunlasma(d: pd.DataFrame) -> pd.DataFrame:
    """d_* varlık paylarından yoğunlaşma ölçüleri."""
    out = d.copy()
    cols = [c for c in out.columns if str(c).startswith("d_") and
            pd.to_numeric(out[c], errors="coerce").notna().any()]
    if not cols:
        return out
    m = out[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0).clip(lower=0)
    toplam = m.sum(axis=1).replace(0, np.nan)
    out["en_buyuk_varlik_pct"] = m.max(axis=1).where(toplam.notna()).round(1)
    out["hhi_varlik"] = (((m.div(toplam, axis=0)) ** 2).sum(axis=1) * 100).round(1)
    rm = [c for c in ("d_repo", "d_mevduat") if c in out.columns]
    if rm:
        out["repo_mevduat_pct"] = (out[rm].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)
                                   .where(toplam.notna())).round(1)
    return out


def vergi_grubu(d: pd.DataFrame) -> pd.Series:
    """Varlık dağılımına göre YAKLAŞIK vergi grubu etiketi (resmi koşul/oran değildir)."""
    kat = (d["kategori"].astype(str) if "kategori" in d.columns else pd.Series("", index=d.index)).map(_tr_low)

    def col(c):
        return pd.to_numeric(d[c], errors="coerce").fillna(0) if c in d.columns else pd.Series(0.0, index=d.index)

    hisse = col("d_hisse")
    borc = col("d_tahvil") + col("d_repo") + col("d_mevduat")
    etiket = pd.Series("Diğer (karma / değişken / fon sepeti)", index=d.index)
    etiket[kat.str.contains("serbest", na=False)] = "Serbest"
    etiket[kat.str.contains("altin|kiymetli", na=False)] = "Altın / kıymetli maden"
    etiket[borc >= 80] = "Borçlanma / mevduat ağırlıklı (≥%80)"
    etiket[kat.str.contains("para piyasasi|likit", na=False)] = "Para piyasası / likit"
    etiket[hisse >= 80] = "Hisse yoğun (≥%80 hisse)"
    return etiket


def saat_ondalik(s) -> Optional[float]:
    """'13:30' / '13.30' / '1330' → 13.5"""
    try:
        t = str(s).strip().replace(".", ":")
        if not t or t in ("?", "None", "nan"):
            return None
        if ":" in t:
            h, m = t.split(":")[:2]
        elif len(t) >= 3:
            h, m = t[:-2], t[-2:]
        else:
            h, m = t, "0"
        return round(int(h) + int(m) / 60.0, 2)
    except Exception:
        return None


def temel_ekle(d: pd.DataFrame) -> pd.DataFrame:
    """Ağ gerektirmeyen tüm türetilmiş sütunlar (prepare_universe sonrasında çağrılır)."""
    out = yogunlasma(d)
    out = kategori_ici_yuzdelik(out)
    out["vergi_grubu"] = vergi_grubu(out)
    if "islem_saat" in out.columns and "son_islem_saat" not in out.columns:
        out["son_islem_saat"] = out["islem_saat"].astype(str).str.split("-").str[-1].map(saat_ondalik)
    return out


# ── Fiyat serisi ölçüleri ──────────────────────────────────────────────────
def _seri(fd: pd.DataFrame) -> Optional[pd.Series]:
    if fd is None or fd.empty:
        return None
    s = fd.set_index("tarih")["fiyat"].sort_index().astype(float)
    s = s[~s.index.duplicated(keep="last")].dropna()
    if getattr(s.index, "tz", None) is not None:
        s.index = s.index.tz_localize(None)
    return s if len(s) >= 5 else None


def aylik_getiri(s: pd.Series) -> pd.Series:
    m = s.groupby(s.index.to_period("M")).last().pct_change().dropna() * 100
    return m.tail(12)


def fiyat_olculeri(fd: pd.DataFrame, bist_rets: Optional[pd.Series] = None) -> Tuple[dict, Optional[pd.Series]]:
    out = {k: None for k in ("fon_yasi_ay", "pozitif_ay_orani", "toparlanma_gun",
                             "tepeden_uzaklik", "alfa_bist", "beta_bist")}
    s = _seri(fd)
    if s is None:
        return out, None
    out["fon_yasi_ay"] = round((s.index[-1] - s.index[0]).days / 30.44, 1)
    am = aylik_getiri(s)
    if len(am) >= 6:
        out["pozitif_ay_orani"] = round(float((am > 0).mean() * 100), 1)
    zirve = s.cummax()
    dd = s / zirve - 1
    out["tepeden_uzaklik"] = round(float(dd.iloc[-1] * 100), 2)
    if float(dd.min()) > -0.005:
        out["toparlanma_gun"] = 0.0
    else:
        dip = dd.idxmin()
        hedef = float(zirve.loc[dip])
        sonra = s.loc[dip:]
        ok = sonra[sonra >= hedef]
        out["toparlanma_gun"] = float((ok.index[0] - dip).days) if len(ok) else None  # None = henüz toparlamadı
    if bist_rets is not None and len(bist_rets) > 60:
        try:
            fr = s.pct_change().dropna()
            bm = bist_rets.copy()
            if getattr(bm.index, "tz", None) is not None:
                bm.index = bm.index.tz_localize(None)
            ortak = fr.index.intersection(bm.index)
            if len(ortak) >= 60:
                x, y = bm.loc[ortak].values, fr.loc[ortak].values
                var = np.var(x, ddof=1)
                if var > 0:
                    beta = float(np.cov(x, y, ddof=1)[0, 1] / var)
                    alfa = float((y.mean() - beta * x.mean()) * 252 * 100)
                    out["beta_bist"], out["alfa_bist"] = round(beta, 3), round(alfa, 2)
        except Exception:
            pass
    return out, am


def ay_ustu_orani(aylik: Dict[str, pd.Series], grup: Dict[str, str], min_grup: int = 5) -> Dict[str, Optional[float]]:
    """Fonun aylık getirisi kendi kategorisinin aylık medyanından yüksek olan ay yüzdesi."""
    if not aylik:
        return {}
    M = pd.DataFrame(aylik)
    sonuc: Dict[str, Optional[float]] = {k: None for k in aylik}
    g = pd.Series(grup).reindex(M.columns)
    for ad, kodlar in g.groupby(g).groups.items():
        kodlar = list(kodlar)
        if len(kodlar) < min_grup:
            continue
        sub = M[kodlar]
        med = sub.median(axis=1)
        for k in kodlar:
            ok = sub[k].notna()
            if ok.sum() >= 6:
                sonuc[k] = round(float((sub[k][ok] > med[ok]).mean() * 100), 1)
    return sonuc


# ── Anlık görüntü (akış) ───────────────────────────────────────────────────
def _snap_yolu(core=None) -> Path:
    base = Path(getattr(core, "CACHE_DIR", ".")) if core is not None else Path(".")
    base.mkdir(parents=True, exist_ok=True)
    return base / "fon_snapshot.csv"


def snapshot_kaydet(d: pd.DataFrame, core=None) -> int:
    """Bugünün büyüklük / yatırımcı değerlerini saklar (günde bir kez). Eklenen satır sayısı."""
    if d is None or d.empty or "kod" not in d.columns:
        return 0
    if "buyukluk" not in d.columns and "yatirimci" not in d.columns:
        return 0
    p = _snap_yolu(core)
    bugun = datetime.now().strftime("%Y-%m-%d")
    yeni = pd.DataFrame({
        "tarih": bugun, "kod": d["kod"].astype(str).str.upper(),
        "buyukluk": pd.to_numeric(d.get("buyukluk"), errors="coerce"),
        "yatirimci": pd.to_numeric(d.get("yatirimci"), errors="coerce"),
    }).dropna(subset=["buyukluk", "yatirimci"], how="all")
    try:
        eski = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=yeni.columns)
        eski = eski[~((eski["tarih"] == bugun) & eski["kod"].isin(yeni["kod"]))]
        pd.concat([eski, yeni], ignore_index=True).to_csv(p, index=False)
        return len(yeni)
    except Exception:
        return 0


def akis_ekle(d: pd.DataFrame, gun: int = 90, core=None, min_gun: int = 14) -> pd.DataFrame:
    """Kayıtlı anlık görüntülerden büyüklük / yatırımcı değişimi (%). Yeterince eski kayıt yoksa boş kalır."""
    out = d.copy()
    p = _snap_yolu(core)
    for c in ("buyukluk_degisim_pct", "yatirimci_degisim_pct", "akis_gun"):
        out[c] = np.nan
    if not p.exists():
        return out
    try:
        sn = pd.read_csv(p, parse_dates=["tarih"])
    except Exception:
        return out
    hedef = pd.Timestamp(datetime.now() - timedelta(days=gun))
    sinir = pd.Timestamp(datetime.now() - timedelta(days=min_gun))
    sn = sn[sn["tarih"] <= sinir]
    if sn.empty:
        return out
    sn = sn.assign(fark=(sn["tarih"] - hedef).abs()).sort_values("fark").drop_duplicates("kod")
    ref = sn.set_index("kod")
    kod = out["kod"].astype(str).str.upper()
    for src, dst in (("buyukluk", "buyukluk_degisim_pct"), ("yatirimci", "yatirimci_degisim_pct")):
        if src in out.columns and src in ref.columns:
            now = pd.to_numeric(out[src], errors="coerce")
            old = kod.map(ref[src])
            out[dst] = ((now / old - 1) * 100).where(old > 0).round(2)
    out["akis_gun"] = (pd.Timestamp(datetime.now()) - kod.map(ref["tarih"])).dt.days
    return out


# ── Ek veri (TER / stopaj) ─────────────────────────────────────────────────
def ek_veri_birlestir(d: pd.DataFrame, ek: pd.DataFrame) -> pd.DataFrame:
    """kod + (ter, stopaj, ...) içeren tabloyu birleştirir."""
    ek = ek.copy()
    ek.columns = [_tr_low(c).strip().replace(" ", "_") for c in ek.columns]
    ad_esle = {"fon_kodu": "kod", "toplam_gider_orani": "ter", "gider_orani": "ter", "stopaj_orani": "stopaj"}
    ek = ek.rename(columns={k: v for k, v in ad_esle.items() if k in ek.columns})
    if "kod" not in ek.columns:
        raise ValueError("Ek veride 'kod' sütunu yok.")
    ek["kod"] = ek["kod"].astype(str).str.strip().str.upper()
    ek = ek.drop_duplicates("kod").set_index("kod")
    out = d.copy()
    kod = out["kod"].astype(str).str.upper()
    for c in ek.columns:
        if c in ("ter", "stopaj"):
            out[c] = pd.to_numeric(kod.map(pd.to_numeric(ek[c], errors="coerce")), errors="coerce")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Ağ gerektiren zenginleştirme
# ═══════════════════════════════════════════════════════════════════════════
def zenginlestir(analiz, core, d: pd.DataFrame, bist: bool = True,
                 ilerleme: Optional[Callable[[float, str], None]] = None) -> pd.DataFrame:
    """kod → ek metrikler tablosu (fiyat geçmişi 36 ay + profil). Cache'li çağrılar kullanılır."""
    kodlar = d["kod"].astype(str).str.upper().tolist()
    bist_rets = None
    if bist:
        try:
            bist_rets = core.yahoo_daily_returns("XU100.IS")
        except Exception:
            bist_rets = None
    grup = _grup_kolonu(d)
    grup.index = kodlar
    satirlar: Dict[str, dict] = {}
    aylik: Dict[str, pd.Series] = {}
    for i, kod in enumerate(kodlar, 1):
        if ilerleme:
            ilerleme(i / max(len(kodlar), 1), kod)
        try:
            fd = analiz.api.fiyat(kod, periyod=36)
            o, am = fiyat_olculeri(fd, bist_rets)
        except Exception:
            o, am = {}, None
        try:
            p = analiz.api.profil(kod) or {}
            o["son_islem_saat"] = saat_ondalik(p.get("sonIsSaat"))
            v = p.get("fonGeriAlisValor")
            if v is not None:
                o["valor_geri_alis"] = float(v)
        except Exception:
            pass
        satirlar[kod] = o
        if am is not None and len(am) >= 6:
            aylik[kod] = am
    ust = ay_ustu_orani(aylik, grup.to_dict())
    for k, v in ust.items():
        satirlar.setdefault(k, {})["ay_ustu_orani"] = v
    return pd.DataFrame.from_dict(satirlar, orient="index").rename_axis("kod")


def ek_uygula(d: pd.DataFrame, ek: Optional[pd.DataFrame]) -> pd.DataFrame:
    """zenginlestir() çıktısını d'ye ekler (d'de dolu olan değerler korunur)."""
    out = d.copy()
    if ek is None or ek.empty:
        return out
    kod = out["kod"].astype(str).str.upper()
    for c in ek.columns:
        yeni = pd.to_numeric(kod.map(ek[c]), errors="coerce")
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").where(lambda s: s.notna(), yeni)
        else:
            out[c] = yeni
    return out


def hazirla(d: pd.DataFrame, ek: Optional[pd.DataFrame] = None, ek_veri: Optional[pd.DataFrame] = None,
            akis_gun: int = 90, core=None) -> pd.DataFrame:
    """prepare_universe edilmiş tablodan tüm ek sütunları üretir."""
    out = ek_uygula(d, ek)
    out = temel_ekle(out)
    out = akis_ekle(out, akis_gun, core)
    if ek_veri is not None and not ek_veri.empty:
        out = ek_veri_birlestir(out, ek_veri)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Streamlit paneli (Fon Tarama / Sıralama / Stüdyo'da yeniden kullanılır)
# ═══════════════════════════════════════════════════════════════════════════
PANEL_KRITERLER = [k for k in EK_KOLON_LISTESI if k not in ("buyukluk_degisim_pct",)] + ["buyukluk_degisim_pct"]


def hesap_kontrolleri(st, core, get_analiz, df: pd.DataFrame, key: str) -> int:
    """Hesapla düğmesi + TER/stopaj CSV yükleme + akış aralığı. Akış gün sayısını döner."""
    S = st.session_state
    b1, b2 = st.columns([2, 3])
    if b1.button(f"📡 Ek metrikleri hesapla ({len(df)} fon)", key=f"{key}_ek_hesapla",
                 help="36 aylık fiyat geçmişi ve profil bilgisi çekilir (cache'li). Fon sayısı fazlaysa uzun sürebilir."):
        bar = st.progress(0.0, text="Hesaplanıyor…")
        try:
            yeni = zenginlestir(get_analiz(), core, df,
                                ilerleme=lambda p, k: bar.progress(min(p, 1.0), text=f"{k}"))
            eski = S.get("ek_metrik")
            S["ek_metrik"] = yeni if eski is None else pd.concat([eski[~eski.index.isin(yeni.index)], yeni])
            n = snapshot_kaydet(df, core)
            bar.empty()
            st.success(f"{len(yeni)} fon için hesaplandı. Anlık görüntü kaydı: {n} fon.")
        except Exception as e:  # noqa: BLE001
            bar.empty()
            st.error(f"Hesaplanamadı: {e}")
    up = b2.file_uploader("İsteğe bağlı: TER / stopaj CSV (kod, ter, stopaj)", type=["csv"], key=f"{key}_ek_csv")
    if up is not None and S.get("_ek_csv_ad") != up.name:
        try:
            S["ek_veri"] = pd.read_csv(up)
            S["_ek_csv_ad"] = up.name
        except Exception as e:  # noqa: BLE001
            st.error(f"CSV okunamadı: {e}")
    akis_gun = st.slider("Akış karşılaştırma aralığı (gün)", 30, 365, 90, 30, key=f"{key}_akis")

    return akis_gun


def panel(st, core, get_analiz, df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Ek filtre paneli; filtrelenmiş df döner. Hesaplanan ek metrikler tüm sayfalarda ortak tutulur."""
    S = st.session_state
    if df is None or df.empty or "kod" not in df.columns:
        return df
    with st.expander("➕ Ek filtreler (yaş · süreklilik · kategori içi sıra · alfa/beta · yoğunlaşma · akış · valör · maliyet)"):
        akis_gun = hesap_kontrolleri(st, core, get_analiz, df, key)

        d = TF.prepare_universe(df)
        d = hazirla(d, S.get("ek_metrik"), S.get("ek_veri"), akis_gun, core)
        avail = [k for k in EK_KOLON_LISTESI if k in d.columns and d[k].notna().any()]
        bos = [k for k in EK_KOLON_LISTESI if k not in avail]
        if bos:
            st.caption("Verisi henüz yok: " + ", ".join(TF.CRITERIA[k]["label"] for k in bos) +
                       " — ağ metrikleri için hesapla düğmesi, akış için en az 2 hafta anlık görüntü, "
                       "TER/stopaj için CSV gerekir.")
        sec = st.multiselect("Ek kriterler", avail, key=f"{key}_ek_sec",
                             format_func=lambda k: f"{TF.CRITERIA[k]['group']} · {TF.CRITERIA[k]['label']}")
        spec = TF.empty_spec()
        for c in sec:
            ser = pd.to_numeric(d[c], errors="coerce").dropna()
            r1, r2, r3 = st.columns([2.6, 1, 1])
            r1.markdown(f"**{TF.CRITERIA[c]['label']}**")
            r1.caption(f"min {TF.fmt(ser.min())} · medyan {TF.fmt(ser.median())} · maks {TF.fmt(ser.max())} "
                       f"· veri: {len(ser)}/{len(d)}")
            lo = r2.number_input("Alt", value=None, step=float(TF.CRITERIA[c]["step"]), format="%g",
                                 key=f"{key}_ek_lo_{c}", placeholder="yok", label_visibility="collapsed")
            hi = r3.number_input("Üst", value=None, step=float(TF.CRITERIA[c]["step"]), format="%g",
                                 key=f"{key}_ek_hi_{c}", placeholder="yok", label_visibility="collapsed")
            spec["ranges"][c] = (lo, hi)
        v1, v2 = st.columns(2)
        vg = v1.multiselect("Vergi grubu (yaklaşık)", sorted(d["vergi_grubu"].dropna().unique()),
                            key=f"{key}_ek_vg",
                            help="Varlık dağılımından türetilen etiket; resmi vergi koşulları için KAP/fon izahnamesini doğrulayın.")
        if vg:
            spec["cats"]["vergi_grubu"] = vg
        kur = sorted(x for x in d["kurucu"].astype(str).unique() if x and x != "nan")
        kh = v2.multiselect("Şu kurucuları HARİÇ tut", kur, key=f"{key}_ek_kh")
        if kh:
            spec["cats_not"] = {"kurucu": kh}
        spec["nan_pass"] = st.checkbox("Verisi eksik fonlar elenmesin", key=f"{key}_ek_nan")
        rules = TF.build_rules(d, spec)
        S[f"{key}_ek_cols"] = []
        if not rules:
            return df
        ok = TF.combine(d, rules)
        st.caption(f"Ek filtrelerden geçen: **{int(ok.sum())} / {len(d)}**")
        S[f"{key}_ek_cols"] = list(sec) + (["vergi_grubu"] if vg else [])
        sonuc = d[ok].reset_index(drop=True)
    return sonuc
