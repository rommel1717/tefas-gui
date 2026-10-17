# -*- coding: utf-8 -*-
"""
Portföyüm
=========
Kendi fon + hisse portföyünüzü (adet, maliyet) kaydedip güncel fiyatlarla izler:

  • Pozisyon tablosu : değer, K/Z, günlük / haftalık / aylık değişim, ağırlık
  • Dağılım grafikleri: tür, pozisyon, varlık sınıfı (fonların içine bakarak), kategori, kurucu
  • Performans & risk : portföy değeri serisi, Sharpe / volatilite / MaxDD, BIST100'e göre beta-alfa,
                        pozisyon korelasyonu, yoğunlaşma uyarıları
  • KAP bildirimleri  : hisse (ve eşleşirse fon) kodları için son bildirimler, yeni olanlar işaretlenir
  • Günlük kayıt      : sayfa her açıldığında o günün portföy değeri saklanır (gerçek değer geçmişi)

Saf fonksiyonlar Streamlit gerektirmez. render() Streamlit sayfasıdır.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "portfoyum.json"

KOLONLAR = ["tur", "kod", "adet", "maliyet", "alis_tarihi", "manuel_fiyat", "not"]
TURLER = ["Fon", "Hisse"]


# ═══════════════════════════════════════════════════════════════════════════
# Kayıt (JSON)
# ═══════════════════════════════════════════════════════════════════════════
def yukle(path: Optional[Path] = None) -> dict:
    path = Path(path) if path else PATH
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            d.setdefault("pozisyonlar", [])
            d.setdefault("kap_goruldu", [])
            d.setdefault("gunluk", {})
            return d
    except Exception:
        pass
    return {"pozisyonlar": [], "kap_goruldu": [], "gunluk": {}}


def kaydet(d: dict, path: Optional[Path] = None) -> None:
    Path(path or PATH).write_text(json.dumps(d, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def tur_tahmin(kod: str) -> str:
    """TEFAS fon kodları 3 harf; BIST hisseleri genellikle 4-5 harf."""
    k = re.sub(r"\.IS$", "", str(kod or "").strip().upper())
    return "Fon" if len(k) == 3 else "Hisse"


def pozisyon_df(kayit: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame(kayit or [], columns=KOLONLAR)
    if df.empty:
        return pd.DataFrame({
            "tur": pd.Series(dtype="object"), "kod": pd.Series(dtype="object"),
            "adet": pd.Series(dtype="float"), "maliyet": pd.Series(dtype="float"),
            "alis_tarihi": pd.Series(dtype="object"), "manuel_fiyat": pd.Series(dtype="float"),
            "not": pd.Series(dtype="object"),
        })
    return df


def temizle(df: pd.DataFrame) -> pd.DataFrame:
    """Kullanıcı girişini normalleştirir: kod büyük harf, tür tahmini, boş/geçersiz satırları atar."""
    d = df.copy()
    for c in KOLONLAR:
        if c not in d.columns:
            d[c] = None
    d["kod"] = d["kod"].astype(str).str.strip().str.upper().str.replace(r"\.IS$", "", regex=True)
    d = d[~d["kod"].isin(["", "NAN", "NONE"])]
    d["adet"] = pd.to_numeric(d["adet"], errors="coerce")
    d["maliyet"] = pd.to_numeric(d["maliyet"], errors="coerce")
    d["manuel_fiyat"] = pd.to_numeric(d["manuel_fiyat"], errors="coerce")
    d = d[d["adet"].fillna(0) > 0]
    eksik = ~d["tur"].isin(TURLER)
    d.loc[eksik, "tur"] = d.loc[eksik, "kod"].map(tur_tahmin)
    d["alis_tarihi"] = pd.to_datetime(d["alis_tarihi"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["alis_tarihi"] = d["alis_tarihi"].where(d["alis_tarihi"].notna(), None)
    return d[KOLONLAR].reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# Fiyat
# ═══════════════════════════════════════════════════════════════════════════
def fiyat_getir(analiz, core, pos: pd.DataFrame, ilerleme=None) -> Tuple[Dict[str, pd.DataFrame], Dict[str, str]]:
    """kod → (tarih, fiyat) tablosu ve kod → hata notu. Fon: TEFAS (cache'li), Hisse: Yahoo."""
    fiyat: Dict[str, pd.DataFrame] = {}
    hata: Dict[str, str] = {}
    n = len(pos)
    for i, r in enumerate(pos.itertuples(index=False), 1):
        kod, tur = r.kod, r.tur
        if ilerleme:
            ilerleme(i / max(n, 1), kod)
        try:
            if tur == "Fon":
                fd = analiz.api.fiyat(kod, periyod=12)
            else:
                fd = core.yahoo_fiyat_df(kod, "1y")
            if fd is None or fd.empty:
                hata[kod] = "fiyat bulunamadı"
                continue
            fd = fd.copy()
            fd["tarih"] = pd.to_datetime(fd["tarih"]).dt.normalize()
            fiyat[kod] = fd.drop_duplicates("tarih", keep="last").sort_values("tarih").reset_index(drop=True)
        except Exception as e:  # noqa: BLE001
            hata[kod] = f"{type(e).__name__}: {e}"
    return fiyat, hata


def _getiri(s: pd.Series, gun: int) -> Optional[float]:
    if s is None or len(s) < 2:
        return None
    t = s.index[-1]
    eski = s[s.index <= t - pd.Timedelta(days=gun)]
    if eski.empty:
        return None
    b = float(eski.iloc[-1])
    return (float(s.iloc[-1]) / b - 1) * 100 if b else None


def pozisyon_tablosu(pos: pd.DataFrame, fiyat: Dict[str, pd.DataFrame], isimler: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    rows = []
    bugun = pd.Timestamp(datetime.now().date())
    for r in pos.itertuples(index=False):
        fd = fiyat.get(r.kod)
        s = fd.set_index("tarih")["fiyat"] if fd is not None else None
        son = float(s.iloc[-1]) if s is not None and len(s) else None
        tarih = s.index[-1] if s is not None and len(s) else None
        kaynak = "piyasa"
        if (son is None) and pd.notna(r.manuel_fiyat):
            son, kaynak = float(r.manuel_fiyat), "manuel"
        elif son is None:
            kaynak = "yok"
        onceki = float(s.iloc[-2]) if s is not None and len(s) >= 2 else None
        deger = son * r.adet if son is not None else np.nan
        mal_top = r.maliyet * r.adet if pd.notna(r.maliyet) else np.nan
        kz = deger - mal_top if pd.notna(deger) and pd.notna(mal_top) else np.nan
        tutma = None
        yillik = np.nan
        if r.alis_tarihi:
            try:
                tutma = (bugun - pd.Timestamp(r.alis_tarihi)).days
                if tutma >= 30 and pd.notna(mal_top) and mal_top > 0 and pd.notna(deger) and deger > 0:
                    yillik = ((deger / mal_top) ** (365.0 / tutma) - 1) * 100
            except Exception:
                tutma = None
        rows.append({
            "tur": r.tur, "kod": r.kod, "ad": (isimler or {}).get(r.kod, r.kod),
            "adet": r.adet, "maliyet": r.maliyet, "fiyat": son, "fiyat_tarihi": tarih, "kaynak": kaynak,
            "deger": deger, "maliyet_toplam": mal_top, "kz_tl": kz,
            "kz_pct": (kz / mal_top * 100) if pd.notna(kz) and pd.notna(mal_top) and mal_top else np.nan,
            "gun_tl": (son - onceki) * r.adet if son is not None and onceki is not None else np.nan,
            "gun_pct": (son / onceki - 1) * 100 if son is not None and onceki else np.nan,
            "hafta_pct": _getiri(s, 7), "ay_pct": _getiri(s, 30),
            "tutma_gun": tutma, "yillik_kz_pct": yillik,
        })
    t = pd.DataFrame(rows)
    if t.empty:
        return t
    toplam = t["deger"].sum(skipna=True)
    t["agirlik_pct"] = t["deger"] / toplam * 100 if toplam else np.nan
    return t.sort_values("deger", ascending=False, na_position="last").reset_index(drop=True)


def ozet_kartlar(t: pd.DataFrame) -> dict:
    d = t["deger"].sum(skipna=True)
    m = t.loc[t["deger"].notna(), "maliyet_toplam"].sum(skipna=True)
    g = t["gun_tl"].sum(skipna=True)
    dun = d - g
    return {
        "deger": d, "maliyet": m, "kz_tl": d - m, "kz_pct": (d / m - 1) * 100 if m else np.nan,
        "gun_tl": g, "gun_pct": (g / dun * 100) if dun else np.nan,
        "n": len(t), "fiyatsiz": int(t["deger"].isna().sum()),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Seri, risk, korelasyon
# ═══════════════════════════════════════════════════════════════════════════
def portfoy_serisi(pos: pd.DataFrame, fiyat: Dict[str, pd.DataFrame]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Güncel adetlerle geriye dönük değer serisi (alış tarihini hesaba katmaz) ve pozisyon fiyat matrisi."""
    cols = {}
    for r in pos.itertuples(index=False):
        fd = fiyat.get(r.kod)
        if fd is not None and len(fd) >= 5:
            cols[r.kod] = fd.set_index("tarih")["fiyat"].astype(float)
    if not cols:
        return pd.DataFrame(), pd.DataFrame()
    M = pd.DataFrame(cols).sort_index().ffill().dropna()
    if M.empty:
        return pd.DataFrame(), M
    adet = pos.set_index("kod")["adet"].groupby(level=0).sum()
    deger = (M * adet.reindex(M.columns).values).sum(axis=1)
    return pd.DataFrame({"tarih": deger.index, "fiyat": deger.values}), M


def yogunlasma_uyarilari(t: pd.DataFrame, esik_tek: float = 25.0) -> Tuple[dict, List[str]]:
    w = (t["agirlik_pct"].dropna() / 100.0).sort_values(ascending=False)
    if w.empty:
        return {}, []
    hhi = float((w ** 2).sum())
    bilgi = {"en_buyuk": float(w.iloc[0] * 100), "ilk3": float(w.head(3).sum() * 100),
             "hhi": round(hhi * 100, 1), "etkin_n": round(1 / hhi, 1) if hhi else None}
    uyari: List[str] = []
    for r in t.itertuples():
        if pd.notna(r.agirlik_pct) and r.agirlik_pct > esik_tek:
            uyari.append(f"**{r.kod}** portföyün %{r.agirlik_pct:.0f}'ini oluşturuyor (eşik %{esik_tek:.0f}).")
        if pd.notna(r.kz_pct) and r.kz_pct <= -15:
            uyari.append(f"**{r.kod}** maliyetinin %{abs(r.kz_pct):.0f} altında.")
        if pd.notna(r.gun_pct) and abs(r.gun_pct) >= 5:
            uyari.append(f"**{r.kod}** son gün %{r.gun_pct:+.1f} değişti.")
        if r.fiyat_tarihi is not None and pd.notna(r.fiyat_tarihi) and \
                (pd.Timestamp(datetime.now().date()) - pd.Timestamp(r.fiyat_tarihi)).days > 5:
            uyari.append(f"**{r.kod}** fiyatı güncel değil (son: {pd.Timestamp(r.fiyat_tarihi).date()}).")
        if r.kaynak == "yok":
            uyari.append(f"**{r.kod}** için fiyat bulunamadı; değere katılmadı (manuel fiyat girebilirsiniz).")
    if bilgi["etkin_n"] is not None and bilgi["etkin_n"] < 3 and len(w) >= 3:
        uyari.append(f"Etkin çeşitlilik düşük: {len(w)} pozisyon var ama etkin sayı {bilgi['etkin_n']}.")
    return bilgi, uyari


def varlik_dagilimi(analiz, core, t: pd.DataFrame) -> pd.DataFrame:
    """Fonların içindeki varlık dağılımına bakarak portföyün efektif dağılımı (%). Hisse = %100 hisse."""
    etiket = {**core.KATEGORI_ACIKLAMA, "diger": "Diğer / Belirsiz"}
    toplam = t["deger"].sum(skipna=True)
    if not toplam:
        return pd.DataFrame(columns=["sinif", "pay"])
    acc: Dict[str, float] = {}
    try:
        dag = analiz.api.dagilimlar()
    except Exception:
        dag = pd.DataFrame()
    for r in t.itertuples():
        if pd.isna(r.deger):
            continue
        if r.tur == "Hisse":
            acc["hisse"] = acc.get("hisse", 0.0) + r.deger
            continue
        ozet = None
        if dag is not None and not dag.empty and "kod" in dag.columns:
            satir = dag[dag["kod"].astype(str).str.upper() == r.kod]
            if not satir.empty:
                try:
                    ozet = analiz.api.dagilim_ozet(satir.iloc[0])
                except Exception:
                    ozet = None
        if not ozet or sum(ozet.values()) <= 0:
            acc["diger"] = acc.get("diger", 0.0) + r.deger
            continue
        tp = sum(ozet.values())
        for k, v in ozet.items():
            acc[k] = acc.get(k, 0.0) + r.deger * v / tp
    out = pd.DataFrame({"sinif": [etiket.get(k, k) for k in acc], "pay": [v / toplam * 100 for v in acc.values()]})
    return out[out["pay"] > 0.05].sort_values("pay", ascending=False).reset_index(drop=True)


def kategori_kurucu(analiz, core, t: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for r in t.itertuples():
        if r.tur != "Fon" or pd.isna(r.deger):
            continue
        try:
            o = analiz.api.ozet(r.kod) or {}
        except Exception:
            o = {}
        rows.append({"kod": r.kod, "deger": r.deger, "kategori": o.get("fonKategori") or "Bilinmiyor",
                     "kurucu": core.kurucu_from_ad(r.ad) if r.ad and r.ad != r.kod else "Bilinmiyor"})
    return pd.DataFrame(rows)


def fon_adlari(analiz, pos: pd.DataFrame, fiyat: Dict[str, pd.DataFrame]) -> Dict[str, str]:
    ad = {}
    for r in pos.itertuples(index=False):
        fd = fiyat.get(r.kod)
        if r.tur == "Fon" and fd is not None and "fonUnvan" in fd.columns and len(fd):
            ad[r.kod] = str(fd["fonUnvan"].iloc[-1])
    return ad


# ═══════════════════════════════════════════════════════════════════════════
# KAP
# ═══════════════════════════════════════════════════════════════════════════
KAP_ETIKETLER = {
    "Temettü": ["kar payı", "kâr payı", "temettü", "nakit kar", "nakit kâr"],
    "Sermaye": ["bedelli", "bedelsiz", "sermaye artır", "sermaye azalt", "rüçhan"],
    "Geri alım": ["geri alım", "pay alım", "geri alın"],
    "Finansal rapor": ["finansal rapor", "faaliyet raporu", "bağımsız denetim"],
    "Yeni iş": ["yeni iş", "sözleşme", "ihale", "sipariş"],
    "Birleşme / satın alma": ["birleşme", "devralma", "satın alma", "pay devri", "iştirak"],
    "Genel kurul": ["genel kurul"],
    "Derecelendirme": ["derecelendirme"],
    "Dava / SPK": ["dava", "spk", "idari para", "soruşturma"],
    "Yönetim": ["yönetim kurulu", "istifa", "atama", "görevden"],
}


def kap_etiket(baslik: str) -> str:
    t = str(baslik or "").lower().replace("İ", "i").replace("I", "ı")
    bul = [k for k, kw in KAP_ETIKETLER.items() if any(w in t for w in kw)]
    return ", ".join(bul)


def _kap_satir(it: dict) -> dict:
    basic = it.get("basic") if isinstance(it.get("basic"), dict) else it
    pub = basic.get("publishDate") or basic.get("disclosureDate") or it.get("publishDate") or ""
    title = (basic.get("title") or basic.get("subject") or basic.get("disclosureTitle")
             or it.get("title") or it.get("subject") or it.get("summary") or "")
    stock = (basic.get("stockCodes") or basic.get("stockCode") or basic.get("memberCode")
             or it.get("stockCodes") or it.get("stockCode") or "")
    company = basic.get("companyName") or basic.get("memberTitle") or basic.get("memberName") or ""
    idx = basic.get("disclosureIndex") or basic.get("id") or it.get("disclosureIndex") or ""
    return {"tarih": str(pub), "kodlar": str(stock).upper(), "sirket": company, "baslik": title,
            "index": str(idx), "url": f"https://www.kap.org.tr/tr/Bildirim/{idx}" if idx else ""}


def kap_cek(gun: int = 7, timeout: int = 18) -> Tuple[pd.DataFrame, List[str]]:
    """KAP son N gün bildirimlerini en iyi çabayla çeker (resmi API yok; halka açık JSON uç noktaları).
    Dönen: (DataFrame, hata_notları). Hiçbiri çalışmazsa boş DataFrame + notlar."""
    import requests

    bitis = datetime.now().date()
    bas = bitis - timedelta(days=max(1, int(gun)))
    basliklar = {
        "User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json", "Origin": "https://www.kap.org.tr",
        "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    }
    s = requests.Session()
    rows: List[dict] = []
    notlar: List[str] = []

    def _liste(data):
        if isinstance(data, list):
            return data
        for k in ("data", "result", "disclosures"):
            if isinstance(data, dict) and isinstance(data.get(k), list):
                return data[k]
        return []

    try:
        r = s.get("https://www.kap.org.tr/tr/api/disclosures", headers=basliklar, timeout=timeout)
        if r.ok:
            rows += [_kap_satir(it) for it in _liste(r.json())]
        else:
            notlar.append(f"api/disclosures HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        notlar.append(f"api/disclosures: {type(e).__name__}")

    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        if len(rows) >= 50:
            break
        for url in ("https://www.kap.org.tr/tr/api/memberDisclosureQuery",
                    "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"):
            try:
                r = s.post(url, headers=basliklar, timeout=timeout,
                           json={"fromDate": bas.strftime(fmt), "toDate": bitis.strftime(fmt)})
                if not r.ok:
                    notlar.append(f"{url.split('/')[-1]} HTTP {r.status_code}")
                    continue
                items = _liste(r.json())
                rows += [_kap_satir(it) for it in items]
                if items:
                    break
            except Exception as e:  # noqa: BLE001
                notlar.append(f"{url.split('/')[-1]}: {type(e).__name__}")
    df = pd.DataFrame(rows)
    if df.empty:
        return df, notlar
    df = df[df["index"] != ""].drop_duplicates("index")
    df["_ts"] = pd.to_datetime(df["tarih"], dayfirst=True, errors="coerce")
    df = df.sort_values("_ts", ascending=False).drop(columns="_ts").reset_index(drop=True)
    return df, notlar


def kap_filtrele(df: pd.DataFrame, kodlar: List[str], goruldu: List[str]) -> pd.DataFrame:
    """Elimizdeki kodlara ait bildirimler; yeni (görülmemiş) olanlar işaretli."""
    if df is None or df.empty:
        return pd.DataFrame()
    kodlar = {k.upper() for k in kodlar}

    def esles(s):
        parcalar = {p.strip() for p in re.split(r"[,;/ ]+", str(s)) if p.strip()}
        return sorted(parcalar & kodlar)

    out = df.copy()
    out["eslesen"] = out["kodlar"].map(esles)
    out = out[out["eslesen"].map(len) > 0].copy()
    out["kod"] = out["eslesen"].map(lambda x: ", ".join(x))
    out["etiket"] = out["baslik"].map(kap_etiket)
    out["yeni"] = ~out["index"].isin(set(map(str, goruldu)))
    return out.drop(columns="eslesen").reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# Streamlit sayfası
# ═══════════════════════════════════════════════════════════════════════════
def _tl(v, d=0) -> str:
    try:
        return f"{float(v):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".") + " ₺"
    except (TypeError, ValueError):
        return "—"


def sidebar_rozet(st) -> None:
    """Menü altında KAP yeni bildirim sayısı (yalnızca önceden çekilmişse)."""
    n = st.session_state.get("pf_kap_yeni")
    if n:
        st.sidebar.warning(f"🔔 Portföyünüzde {n} yeni KAP bildirimi var")


def render(*, core, get_analiz) -> None:
    import altair as alt
    import streamlit as st

    S = st.session_state
    st.header("💼 Portföyüm")
    st.caption("Adet ve maliyetlerinizi girin; fiyatlar TEFAS (fon) ve Yahoo (hisse) üzerinden güncellenir. "
               "Kayıtlar yerel `portfoyum.json` dosyasında saklanır.")

    kayit = yukle()
    if "pf_df" not in S:
        S["pf_df"] = pozisyon_df(kayit["pozisyonlar"])

    tab_poz, tab_ozet, tab_dag, tab_risk, tab_kap = st.tabs(
        ["📝 Pozisyonlar", "📊 Özet & K/Z", "🥧 Dağılım", "📈 Performans & Risk", "📰 KAP"]
    )

    # ── pozisyonlar ─────────────────────────────────────────────────────────
    with tab_poz:
        st.caption(
            "İstediğiniz kadar **fon ve hisse** ekleyin (satır ekle / toplu kod). "
            "Tür boşsa koddan tahmin edilir (3 harf ≈ fon). "
            "Maliyet: birim alış ₺. Manuel fiyat yalnızca piyasa bulunamazsa."
        )
        _base_df = S["pf_df"] if "pf_df" in S else pozisyon_df([])
        # Toplu ekleme
        with st.expander("➕ Toplu fon / hisse ekle", expanded=False):
            tc1, tc2 = st.columns([3, 1])
            with tc1:
                toplu = st.text_area(
                    "Kodlar (virgül, boşluk veya satır)",
                    placeholder="AFO, TTE, AAL\nTHYAO, GARAN, EREGL",
                    key="pf_toplu_kod",
                    height=80,
                )
            with tc2:
                varsayilan_adet = st.number_input("Varsayılan adet", min_value=0.0, value=1.0, step=1.0, key="pf_var_adet")
                varsayilan_tur = st.selectbox("Tür (boş=otomatik)", ["", "Fon", "Hisse"], key="pf_var_tur")
            if st.button("Listeye ekle", key="pf_toplu_ekle") and toplu.strip():
                import re as _re
                kodlar = [x.strip().upper() for x in _re.split(r"[,;\s]+", toplu) if x.strip()]
                mevcut = set(str(x).upper() for x in (_base_df["kod"].tolist() if len(_base_df) and "kod" in _base_df.columns else []))
                yeni = []
                for k in kodlar:
                    if k in mevcut:
                        continue
                    tur = varsayilan_tur or tur_tahmin(k)
                    yeni.append({
                        "tur": tur, "kod": k, "adet": float(varsayilan_adet),
                        "maliyet": None, "alis_tarihi": None, "manuel_fiyat": None, "not": "toplu",
                    })
                    mevcut.add(k)
                if yeni:
                    S["pf_df"] = pd.concat([_base_df, pd.DataFrame(yeni, columns=KOLONLAR)], ignore_index=True)
                    S.pop("pf_editor", None)
                    st.success(f"{len(yeni)} pozisyon eklendi — kaydetmeyi unutmayın.")
                    st.rerun()
                else:
                    st.info("Eklenecek yeni kod yok (hepsi zaten listede).")

        duz = st.data_editor(
            S["pf_df"], num_rows="dynamic", use_container_width=True, key="pf_editor",
            column_config={
                "tur": st.column_config.SelectboxColumn("Tür", options=TURLER),
                "kod": st.column_config.TextColumn("Kod", help="Fon: AFO · Hisse: THYAO"),
                "adet": st.column_config.NumberColumn("Adet", min_value=0.0, format="%.4f"),
                "maliyet": st.column_config.NumberColumn("Birim maliyet ₺", min_value=0.0, format="%.4f"),
                "alis_tarihi": st.column_config.TextColumn("Alış tarihi (YYYY-AA-GG)"),
                "manuel_fiyat": st.column_config.NumberColumn("Manuel fiyat ₺", format="%.4f"),
                "not": st.column_config.TextColumn("Not"),
            },
        )
        c1, c2, c3, c4 = st.columns(4)
        if c1.button("💾 Kaydet", type="primary", key="pf_kaydet"):
            temiz = temizle(duz)
            kayit["pozisyonlar"] = temiz.to_dict("records")
            kaydet(kayit)
            S["pf_df"] = pozisyon_df(kayit["pozisyonlar"])
            S.pop("pf_editor", None)
            S.pop("pf_fiyat", None)
            st.success(f"{len(temiz)} pozisyon kaydedildi.")
            st.rerun()
        sepet = S.get("portfoy_sepet") or []
        if sepet and c2.button("🛒 Sepetteki fonları ekle", key="pf_sepet"):
            mevcut = set(temizle(duz)["kod"]) if len(duz) else set()
            yeni = [{"tur": "Fon", "kod": x["kod"], "adet": 1.0, "maliyet": None, "alis_tarihi": None,
                     "manuel_fiyat": None, "not": "sepetten"} for x in sepet if x["kod"] not in mevcut]
            S["pf_df"] = pd.concat([duz, pd.DataFrame(yeni, columns=KOLONLAR)], ignore_index=True)
            S.pop("pf_editor", None)
            st.rerun()
        up = c3.file_uploader("CSV / Excel içe aktar", type=["csv", "xlsx"], key="pf_up", label_visibility="collapsed")
        if up is not None and S.get("_pf_up_ad") != up.name:
            try:
                raw = pd.read_csv(up) if up.name.lower().endswith(".csv") else pd.read_excel(up)
                raw.columns = [str(c).strip().lower().replace(" ", "_") for c in raw.columns]
                S["pf_df"] = pd.concat([duz, temizle(raw)], ignore_index=True).drop_duplicates("kod", keep="last")
                S["_pf_up_ad"] = up.name
                S.pop("pf_editor", None)
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Dosya okunamadı: {e}")
        temiz_son = temizle(duz) if len(duz) else duz
        c4.download_button("⬇️ CSV indir", temiz_son.to_csv(index=False).encode("utf-8-sig"),
                           "portfoyum.csv", "text/csv", key="pf_csv")

    pos = temizle(S["pf_df"]) if len(S["pf_df"]) else S["pf_df"]
    pos = pos[pos["adet"].fillna(0) > 0] if len(pos) else pos
    if pos.empty:
        for t in (tab_ozet, tab_dag, tab_risk, tab_kap):
            with t:
                st.info("Önce 'Pozisyonlar' sekmesinde adet ve maliyet girip kaydedin.")
        return

    # ── fiyatlar ────────────────────────────────────────────────────────────
    analiz = get_analiz()
    anahtar = tuple(sorted(zip(pos["kod"], pos["tur"])))
    yenile = S.get("pf_anahtar") != anahtar or "pf_fiyat" not in S
    with tab_ozet:
        if st.button("🔄 Fiyatları güncelle", key="pf_fiyat_btn"):
            yenile = True
    if yenile:
        bar = st.progress(0.0, text="Fiyatlar alınıyor…")
        fiyat, hata = fiyat_getir(analiz, core, pos, lambda p, k: bar.progress(min(p, 1.0), text=k))
        bar.empty()
        S["pf_fiyat"], S["pf_hata"], S["pf_anahtar"] = fiyat, hata, anahtar
    fiyat, hata = S["pf_fiyat"], S.get("pf_hata", {})
    isimler = fon_adlari(analiz, pos, fiyat)
    t = pozisyon_tablosu(pos, fiyat, isimler)
    k = ozet_kartlar(t)

    # günlük kayıt
    try:
        if k["deger"] and not np.isnan(k["deger"]):
            kayit["gunluk"][datetime.now().strftime("%Y-%m-%d")] = {
                "deger": round(float(k["deger"]), 2), "maliyet": round(float(k["maliyet"]), 2)}
            kaydet(kayit)
    except Exception:
        pass

    # ── özet ────────────────────────────────────────────────────────────────
    with tab_ozet:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Toplam değer", _tl(k["deger"]))
        m2.metric("Toplam maliyet", _tl(k["maliyet"]))
        m3.metric("Kâr / Zarar", _tl(k["kz_tl"]), f"%{k['kz_pct']:+.2f}" if pd.notna(k["kz_pct"]) else None)
        m4.metric("Bugünkü değişim", _tl(k["gun_tl"]), f"%{k['gun_pct']:+.2f}" if pd.notna(k["gun_pct"]) else None)
        for kod, h in hata.items():
            st.warning(f"{kod}: {h}")
        bilgi, uyari = yogunlasma_uyarilari(t)
        if uyari:
            with st.expander(f"⚠️ Dikkat edilecekler ({len(uyari)})", expanded=True):
                for u in uyari:
                    st.markdown(f"- {u}")
        goster = t[["tur", "kod", "ad", "adet", "maliyet", "fiyat", "deger", "kz_tl", "kz_pct", "gun_pct",
                    "hafta_pct", "ay_pct", "agirlik_pct", "yillik_kz_pct", "kaynak"]].copy()
        st.dataframe(
            goster, use_container_width=True, hide_index=True,
            column_config={
                "tur": "Tür", "kod": "Kod", "ad": "Ad", "adet": st.column_config.NumberColumn("Adet", format="%.2f"),
                "maliyet": st.column_config.NumberColumn("Maliyet", format="%.4f"),
                "fiyat": st.column_config.NumberColumn("Fiyat", format="%.4f"),
                "deger": st.column_config.NumberColumn("Değer ₺", format="%.0f"),
                "kz_tl": st.column_config.NumberColumn("K/Z ₺", format="%.0f"),
                "kz_pct": st.column_config.NumberColumn("K/Z %", format="%.1f"),
                "gun_pct": st.column_config.NumberColumn("1G %", format="%.2f"),
                "hafta_pct": st.column_config.NumberColumn("1H %", format="%.2f"),
                "ay_pct": st.column_config.NumberColumn("1A %", format="%.2f"),
                "agirlik_pct": st.column_config.NumberColumn("Ağırlık %", format="%.1f"),
                "yillik_kz_pct": st.column_config.NumberColumn("Yıllık K/Z %", format="%.1f",
                                                               help="Alış tarihi girilen pozisyonlar için bileşik yıllıklandırma"),
                "kaynak": "Fiyat",
            },
        )
        kz = t.dropna(subset=["kz_pct"]).copy()
        if not kz.empty:
            kz["renk"] = np.where(kz["kz_pct"] >= 0, "Kâr", "Zarar")
            ch = alt.Chart(kz).mark_bar().encode(
                x=alt.X("kz_pct:Q", title="K/Z %"), y=alt.Y("kod:N", sort="-x", title=None),
                color=alt.Color("renk:N", scale=alt.Scale(domain=["Kâr", "Zarar"], range=["#2ca02c", "#d62728"]), legend=None),
                tooltip=["kod", alt.Tooltip("kz_pct:Q", format=".1f"), alt.Tooltip("kz_tl:Q", format=",.0f")],
            ).properties(height=max(120, 26 * len(kz)))
            st.altair_chart(ch, use_container_width=True)
        gun = kayit.get("gunluk") or {}
        if len(gun) >= 2:
            gd = pd.DataFrame([{"tarih": pd.Timestamp(d), "Değer": v["deger"], "Maliyet": v["maliyet"]}
                               for d, v in sorted(gun.items())]).melt("tarih", var_name="seri", value_name="₺")
            st.markdown("**Kayıtlı günlük portföy değeri** (sayfayı açtığınız günlerde otomatik kaydedilir)")
            st.altair_chart(alt.Chart(gd).mark_line(point=True).encode(
                x="tarih:T", y=alt.Y("₺:Q", scale=alt.Scale(zero=False)), color="seri:N",
                tooltip=["tarih:T", "seri:N", alt.Tooltip("₺:Q", format=",.0f")]), use_container_width=True)
        else:
            st.caption("Günlük değer geçmişi, sayfayı farklı günlerde açtıkça burada birikir.")

    # ── dağılım ─────────────────────────────────────────────────────────────
    def donut(df, kat, deger, baslik):
        st.markdown(f"**{baslik}**")
        if df is None or df.empty:
            st.caption("Veri yok")
            return
        c = alt.Chart(df).mark_arc(innerRadius=55).encode(
            theta=alt.Theta(f"{deger}:Q"), color=alt.Color(f"{kat}:N", legend=alt.Legend(orient="bottom", title=None)),
            tooltip=[kat, alt.Tooltip(f"{deger}:Q", format=",.1f")])
        st.altair_chart(c, use_container_width=True)

    with tab_dag:
        tv = t.dropna(subset=["deger"])
        d1, d2 = st.columns(2)
        with d1:
            donut(tv.groupby("tur", as_index=False)["deger"].sum(), "tur", "deger", "Fon / Hisse")
        with d2:
            donut(tv[["kod", "deger"]], "kod", "deger", "Pozisyon bazında")
        d3, d4 = st.columns(2)
        with d3:
            vd = varlik_dagilimi(analiz, core, t)
            donut(vd, "sinif", "pay", "Varlık sınıfı (fonların içine bakarak)")
        kk = kategori_kurucu(analiz, core, t)
        with d4:
            donut(kk.groupby("kategori", as_index=False)["deger"].sum() if not kk.empty else kk,
                  "kategori", "deger", "Fon kategorisi")
        if not kk.empty:
            donut(kk.groupby("kurucu", as_index=False)["deger"].sum(), "kurucu", "deger", "Fon kurucusu")
        if not vd.empty:
            st.caption("Varlık sınıfı grafiği, TEFAS'ın güncel portföy dağılım verisine göre fon değerleriyle ağırlıklandırılır; "
                       "hisse pozisyonları %100 hisse sayılır.")

    # ── performans & risk ───────────────────────────────────────────────────
    with tab_risk:
        seri, M = portfoy_serisi(pos, fiyat)
        if seri.empty:
            st.info("Seri oluşturmak için en az bir pozisyonun fiyat geçmişi gerekir.")
        else:
            # HTML + quantstats rapor
            st.markdown("**📄 Portföyüm HTML raporu** (Türkçe + quantstats)")
            if st.button("HTML rapor üret", key="pf_html_btn"):
                with st.spinner("Rapor yazılıyor…"):
                    try:
                        fm = core.full_metrics(seri.rename(columns={"fiyat": "fiyat"}), getattr(analiz, "rf", 0.40))
                        # korelasyon / çeşitlilik
                        div_sc = None
                        corr_m = None
                        if M.shape[1] >= 2:
                            R = M.pct_change().dropna()
                            if len(R) >= 30:
                                corr_m = R.corr()
                                mask = np.triu(np.ones(corr_m.shape), k=1).astype(bool)
                                avg_abs = float(np.abs(corr_m.where(mask)).stack().mean())
                                div_sc = round(max(0, min(100, (1 - avg_abs) * 100)), 1)
                        path = core.portfoy_html_raporu(
                            baslik="Portföyüm",
                            metrikler=fm or {},
                            tablo=t,
                            diversify_score=div_sc,
                            corr_df=corr_m,
                            oneriler=None,
                            ozet_md="",
                            fiyat_df=seri.rename(columns={"fiyat": "fiyat"}) if "fiyat" in seri.columns else seri,
                            quantstats=True,
                        )
                        if path:
                            S["pf_html_path"] = path
                            st.success(f"Rapor kaydedildi: {path}")
                        else:
                            st.warning("Rapor üretilemedi.")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Rapor hatası: {e}")
            hp = S.get("pf_html_path")
            if hp:
                from pathlib import Path as _P
                if _P(hp).exists():
                    st.download_button(
                        "⬇️ HTML indir", _P(hp).read_bytes(),
                        file_name=_P(hp).name, mime="text/html", key="pf_dl_html",
                    )
                    qs_files = sorted(_P(hp).parent.glob("qs_rapor_*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if qs_files:
                        st.download_button(
                            "⬇️ QuantStats HTML", qs_files[0].read_bytes(),
                            file_name=qs_files[0].name, mime="text/html", key="pf_dl_qs",
                        )
            st.caption("Not: Bu seri, **şu anki adetlerinizin** geçmiş fiyatlarla yeniden değerlenmesidir "
                       "(alış/satış zamanlamasını yansıtmaz). Gerçek geçmiş için 'Özet' sekmesindeki kayıtlı günlük değere bakın.")
            ref_bist = None
            try:
                ref_bist = core.yahoo_fiyat_df("XU100.IS", "1y")
            except Exception:
                ref_bist = None
            kars = seri.copy()
            kars["Portföyüm"] = kars["fiyat"] / kars["fiyat"].iloc[0] * 100
            kars = kars[["tarih", "Portföyüm"]]
            if ref_bist is not None and not ref_bist.empty:
                b = ref_bist.copy()
                b["tarih"] = pd.to_datetime(b["tarih"]).dt.normalize()
                b = b[b["tarih"] >= kars["tarih"].iloc[0]]
                if len(b):
                    kars = kars.merge(b.assign(BIST100=b["fiyat"] / b["fiyat"].iloc[0] * 100)[["tarih", "BIST100"]],
                                      on="tarih", how="left")
                    kars["BIST100"] = kars["BIST100"].ffill()
            long = kars.melt("tarih", var_name="seri", value_name="endeks (başlangıç=100)").dropna()
            st.altair_chart(alt.Chart(long).mark_line().encode(
                x="tarih:T", y=alt.Y("endeks (başlangıç=100):Q", scale=alt.Scale(zero=False)), color="seri:N",
                tooltip=["tarih:T", "seri:N", alt.Tooltip("endeks (başlangıç=100):Q", format=".1f")]),
                use_container_width=True)

            fm = core.full_metrics(seri.rename(columns={"fiyat": "fiyat"}), getattr(analiz, "rf", 0.40))
            bist_rets = None
            try:
                bist_rets = core.yahoo_daily_returns("XU100.IS")
            except Exception:
                pass
            import tefas_ek as TE
            ol, _ = TE.fiyat_olculeri(seri, bist_rets)
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("1 ay", f"%{fm.get('getiri_1a'):.1f}" if fm.get("getiri_1a") is not None else "—")
            r2.metric("3 ay", f"%{fm.get('getiri_3a'):.1f}" if fm.get("getiri_3a") is not None else "—")
            r3.metric("6 ay", f"%{fm.get('getiri_6a'):.1f}" if fm.get("getiri_6a") is not None else "—")
            r4.metric("1 yıl", f"%{fm.get('getiri_1y'):.1f}" if fm.get("getiri_1y") is not None else "—")
            q1, q2, q3, q4 = st.columns(4)
            q1.metric("Volatilite (yıllık)", f"%{fm.get('volatilite'):.1f}" if fm.get("volatilite") is not None else "—")
            q2.metric("Sharpe", f"{fm.get('sharpe'):.2f}" if fm.get("sharpe") is not None else "—")
            q3.metric("Max drawdown", f"%{fm.get('max_drawdown'):.1f}" if fm.get("max_drawdown") is not None else "—")
            q4.metric("Zirveden uzaklık", f"%{ol.get('tepeden_uzaklik'):.1f}" if ol.get("tepeden_uzaklik") is not None else "—")
            b1, b2, b3, b4 = st.columns(4)
            b1.metric("Beta (BIST100)", f"{ol['beta_bist']:.2f}" if ol.get("beta_bist") is not None else "—")
            b2.metric("Alfa (yıllık)", f"%{ol['alfa_bist']:.1f}" if ol.get("alfa_bist") is not None else "—")
            b3.metric("VaR %95 (günlük)", f"%{fm.get('var95'):.2f}" if fm.get("var95") is not None else "—")
            bilgi, _ = yogunlasma_uyarilari(t)
            b4.metric("Etkin pozisyon sayısı", f"{bilgi.get('etkin_n', '—')}",
                      help="1 / Σ(ağırlık²). Pozisyon sayısına yakınsa dengeli dağılmışsınız.")
            if bilgi:
                st.caption(f"En büyük pozisyon %{bilgi['en_buyuk']:.0f} · ilk 3 pozisyon %{bilgi['ilk3']:.0f} · "
                           f"yoğunlaşma endeksi {bilgi['hhi']}")

            if M.shape[1] >= 3:
                R = M.pct_change().dropna()
                if len(R) >= 30:
                    C = R.corr().round(2)
                    cl = C.reset_index().melt("index")
                    cl.columns = ["a", "b", "korelasyon"]
                    base = alt.Chart(cl).encode(x=alt.X("a:N", title=None), y=alt.Y("b:N", title=None))
                    st.markdown("**Pozisyonlar arası korelasyon** (günlük getiri)")
                    st.altair_chart(
                        base.mark_rect().encode(color=alt.Color("korelasyon:Q", scale=alt.Scale(scheme="redblue", domain=[-1, 1]))) +
                        base.mark_text(fontSize=11).encode(text=alt.Text("korelasyon:Q", format=".2f")),
                        use_container_width=True)
                    yuksek = [(a, b, c) for a, b, c in cl.itertuples(index=False) if a < b and c >= 0.85]
                    if yuksek:
                        st.warning("Çok yüksek korelasyon (≥0,85): " + ", ".join(f"{a}–{b} ({c:.2f})" for a, b, c in yuksek) +
                                   " — birlikte hareket ediyorlar, çeşitlendirme etkisi sınırlı.")

    # ── KAP ─────────────────────────────────────────────────────────────────
    with tab_kap:
        kodlar = pos["kod"].tolist()
        st.caption("KAP'ın resmi bir abonelik API'si yok; halka açık JSON uç noktaları en iyi çabayla sorgulanır. "
                   "Hisse kodları için en güvenilir sonucu verir; erişilemezse aşağıda neden gösterilir.")
        k1, k2, k3 = st.columns([1, 1, 2])
        gun = k1.number_input("Son kaç gün", 1, 30, 7, key="pf_kap_gun")
        yenile_kap = k2.button("🔄 KAP'ı kontrol et", key="pf_kap_btn")
        son = S.get("pf_kap_zaman")
        if yenile_kap or son is None or (datetime.now() - son).total_seconds() > 900:
            with st.spinner("KAP sorgulanıyor…"):
                df_kap, notlar = kap_cek(int(gun))
            S["pf_kap_ham"], S["pf_kap_not"], S["pf_kap_zaman"] = df_kap, notlar, datetime.now()
        df_kap, notlar = S.get("pf_kap_ham"), S.get("pf_kap_not", [])
        sonuc = kap_filtrele(df_kap, kodlar, kayit.get("kap_goruldu", []))
        S["pf_kap_yeni"] = int(sonuc["yeni"].sum()) if not sonuc.empty else 0
        k3.caption(f"Son kontrol: {S['pf_kap_zaman']:%H:%M} · taranan bildirim: {0 if df_kap is None else len(df_kap)}")
        if df_kap is None or df_kap.empty:
            st.error("KAP'tan veri alınamadı. " + (" | ".join(notlar[:3]) if notlar else "Ağ erişimi veya zaman aşımı."))
            st.link_button("KAP bildirim sorgusunu aç", "https://www.kap.org.tr/tr/bildirim-sorgu")
        elif sonuc.empty:
            st.success("Taranan bildirimler arasında portföyünüzdeki kodlara ait bildirim yok.")
            st.caption("Not: KAP'ın genel akışı yalnızca son birkaç yüz bildirimi içerebilir; uzun aralıklar için "
                       "şirket sayfasından bakın.")
        else:
            yeni_n = int(sonuc["yeni"].sum())
            (st.warning if yeni_n else st.info)(f"🔔 {yeni_n} yeni, toplam {len(sonuc)} bildirim")
            gosterilen = sonuc.assign(durum=np.where(sonuc["yeni"], "🆕 YENİ", ""))[
                ["durum", "tarih", "kod", "baslik", "etiket", "url"]]
            st.dataframe(gosterilen, use_container_width=True, hide_index=True, column_config={
                "durum": "", "tarih": "Tarih", "kod": "Kod", "baslik": "Başlık", "etiket": "Konu",
                "url": st.column_config.LinkColumn("Bildirim", display_text="Aç"),
            })
            if yeni_n and st.button("✅ Tümünü okundu işaretle", key="pf_kap_oku"):
                kayit["kap_goruldu"] = sorted(set(map(str, kayit.get("kap_goruldu", []))) | set(sonuc["index"]))[-2000:]
                kaydet(kayit)
                S["pf_kap_yeni"] = 0
                st.rerun()
        fon_kodlari = pos.loc[pos["tur"] == "Fon", "kod"].tolist()
        if fon_kodlari:
            st.caption("Fonlar için KAP bildirimi çoğunlukla fon kurucusu üzerinden yapılır; eşleşme yoksa "
                       "fon sayfasındaki 'KAP' bağlantısından bakabilirsiniz.")
