# -*- coding: utf-8 -*-
"""
Piyasa Paneli (ön sayfa)
========================
Günlük olarak güncellenen:
  • 🆕 Yeni bilançolar   — KAP "Finansal Rapor" bildirimleri + İş Yatırım MaliTablo dönem yoklaması
  • 📈 Hisseler          — günlük / haftalık / aylık en çok artan ve düşen (Yahoo Finance)
  • 💰 Fonlar            — günlük / haftalık / aylık en çok artan ve düşen (TEFAS)
Yeni bilanço gelince BIST değerlemesinin yenilenmesi gerektiği işaretlenir; istenirse değerleme otomatik
(veya tek tıkla) yeniden çalıştırılır.

Çalışma biçimleri
  1) Uygulama içi: arka plan zamanlayıcı (st.cache_resource) — 10:30 ve 19:00'dan (TSİ, hafta içi) sonra
     veri eskiyse kendiliğinden yeniler.
  2) Dışarıdan: `python panel_guncelle.py` (cron / GitHub Actions) → panel_data/*.json günceller.
Veri kaynağına ulaşılamazsa panel sahte veri üretmez; "veri alınamadı" gösterir ve son başarılı veriyi korur.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

try:  # Windows'ta tzdata gerekir (requirements.txt)
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Istanbul")
except Exception:  # noqa: BLE001
    TZ = None

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "panel_data"
HEDEF_SAATLER = [(10, 30), (19, 0)]          # hafta içi güncelleme hedefleri (TSİ)
RETRY_BEKLEME_DK = 60                         # başarısız denemeden sonra yeniden deneme aralığı
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36"}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}.IS"

_RUN_LOCK = threading.Lock()
_STATE = {"running": False, "part": "", "done": 0, "total": 0}
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── yardımcılar ─────────────────────────────────────────────────────────────
def now() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or now()).strftime("%Y-%m-%d %H:%M")


def _parse(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        return d.replace(tzinfo=TZ) if TZ else d
    except Exception:  # noqa: BLE001
        return None


def load(name: str, default=None):
    p = DATA_DIR / f"{name}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {} if default is None else default


def save(name: str, obj) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / f"{name}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def _fold(s: str) -> str:
    return str(s or "").translate(str.maketrans("ıİşŞğĞüÜöÖçÇ", "iisSgGuUoOcC")).lower()


def _core():
    import tefas_core
    return tefas_core


def _vbist():
    import vbist_degerleme_v193 as V
    return V


def hedef_zamani(t: Optional[datetime] = None) -> datetime:
    """Şu andan önceki en yakın hafta-içi güncelleme hedefi."""
    t = t or now()
    for gun in range(0, 8):
        d = (t - timedelta(days=gun)).date()
        if d.weekday() >= 5:
            continue
        for h, m in sorted(HEDEF_SAATLER, reverse=True):
            dt = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ) if TZ else datetime(d.year, d.month, d.day, h, m)
            if dt <= t:
                return dt
    return t


def due(meta: dict, t: Optional[datetime] = None) -> bool:
    t = t or now()
    deneme = _parse(meta.get("son_deneme"))
    if deneme and (t - deneme) < timedelta(minutes=RETRY_BEKLEME_DK):
        return False
    son = _parse(meta.get("son_guncelleme"))
    return son is None or son < hedef_zamani(t)


def _degisim(s: pd.Series) -> Optional[dict]:
    """Fiyat serisinden (tarih indeksli) 1g / ~1 hafta / ~1 ay % değişim."""
    s = pd.to_numeric(s, errors="coerce").dropna()
    s = s[s > 0].sort_index()
    if len(s) < 2:
        return None
    son, t = float(s.iloc[-1]), s.index[-1]

    def _at(gun: int) -> Optional[float]:
        sub = s[s.index <= t - pd.Timedelta(days=gun)]
        return float(sub.iloc[-1]) if len(sub) else None

    h, a = _at(7), _at(30)
    return {"tarih": t.strftime("%Y-%m-%d"), "fiyat": round(son, 6),
            "g1": round((son / float(s.iloc[-2]) - 1) * 100, 3),
            "h1": round((son / h - 1) * 100, 3) if h else None,
            "a1": round((son / a - 1) * 100, 3) if a else None}


# ── Fonlar ──────────────────────────────────────────────────────────────────
def guncelle_fonlar(workers: int = 8, tipler: Tuple[str, ...] = ("YAT",), max_fon: int = 0) -> dict:
    core = _core()
    kodlar: List[Tuple[str, str, str]] = []
    for tip in tipler:
        dag = core.TefasAPI(fon_tipi=tip, sleep=0.0).dagilimlar()
        for _, r in dag.iterrows():
            ad = str(r.get("ad") or "")
            if core.kurucu_yasakli_mi(ad) or core.unvan_tasfiye_mi(ad):
                continue
            kodlar.append((str(r["kod"]), ad, tip))
    if not kodlar:
        raise RuntimeError("TEFAS fon listesi alınamadı.")
    if max_fon:
        kodlar = kodlar[:max_fon]
    local = threading.local()
    _STATE.update(part="fon", done=0, total=len(kodlar))

    def work(it):
        kod, ad, tip = it
        api = getattr(local, "api", None)
        if api is None:
            api = local.api = core.TefasAPI(fon_tipi=tip, sleep=0.05)
        rows = []
        for _ in range(2):
            rows = api._post("/api/funds/fonFiyatBilgiGetir", {"fonKodu": kod.upper(), "dil": "TR", "periyod": 1},
                             ckey=None)
            if rows:
                break
            time.sleep(0.6)
        if not rows:
            return None
        df = pd.DataFrame(rows)
        if "tarih" not in df.columns or "fiyat" not in df.columns:
            return None
        df["tarih"] = pd.to_datetime(df["tarih"], errors="coerce")
        s = df.dropna(subset=["tarih"]).drop_duplicates("tarih").set_index("tarih")["fiyat"]
        d = _degisim(s)
        return None if d is None else {"kod": kod, "ad": ad, "tip": tip, **d}

    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(work, k) for k in kodlar]):
            _STATE["done"] += 1
            try:
                r = f.result()
            except Exception:  # noqa: BLE001
                r = None
            if r:
                out.append(r)
    if len(out) < max(5, len(kodlar) * 0.1):
        raise RuntimeError(f"Yeterli fon fiyatı alınamadı ({len(out)}/{len(kodlar)}).")
    veri_tarihi = max(r["tarih"] for r in out)
    obj = {"guncelleme": _iso(), "veri_tarihi": veri_tarihi, "taranan": len(kodlar), "kayitlar": out}
    save("fon", obj)
    return obj


# ── Hisseler ────────────────────────────────────────────────────────────────
def bist_evren() -> Tuple[List[str], Dict[str, dict]]:
    """Hisse kodları (+ piyasa değeri bilgisi). Önce canlı İş Yatırım tablosu, olmazsa önceki kayıt."""
    try:
        V = _vbist()
        ratios = V.get_ratios(requests.Session())
        kodlar = sorted(k for k in ratios if re.fullmatch(r"[A-Z0-9]{3,6}", k))
        if len(kodlar) >= 50:
            return kodlar, {k: {"cap": ratios[k].get("cap")} for k in kodlar}
    except Exception:  # noqa: BLE001
        pass
    eski = load("hisse").get("kayitlar") or []
    if eski:
        return [r["kod"] for r in eski], {r["kod"]: {"cap": r.get("cap")} for r in eski}
    raise RuntimeError("BIST hisse listesi alınamadı (İş Yatırım erişimi yok, önceki kayıt da yok).")


def _yahoo_hisse(code: str, sess: requests.Session) -> Optional[dict]:
    for deneme in range(3):
        try:
            r = sess.get(YAHOO.format(sym=code), params={"range": "3mo", "interval": "1d"}, headers=UA, timeout=20)
            if r.status_code == 429:
                time.sleep(1.5 * (deneme + 1))
                continue
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            idx = pd.to_datetime(res["timestamp"], unit="s", utc=True)
            idx = (idx.tz_convert("Europe/Istanbul") if TZ else idx).tz_localize(None).normalize()
            close = pd.Series(q["close"], index=idx, dtype="float64")
            vol = pd.Series(q.get("volume") or [np.nan] * len(idx), index=idx, dtype="float64")
            ok = close.notna() & ~close.index.duplicated(keep="last")
            close, vol = close[ok], vol[ok]
            d = _degisim(close)
            if d is None:
                return None
            ciro = float((close * vol).tail(20).mean()) if vol.notna().any() else None
            return {"kod": code, **d, "ciro": None if ciro is None or np.isnan(ciro) else round(ciro, 0)}
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return None


def guncelle_hisseler(workers: int = 10) -> dict:
    kodlar, bilgi = bist_evren()
    _STATE.update(part="hisse", done=0, total=len(kodlar))
    sess = requests.Session()
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(_yahoo_hisse, k, sess) for k in kodlar]):
            _STATE["done"] += 1
            try:
                r = f.result()
            except Exception:  # noqa: BLE001
                r = None
            if r:
                r["cap"] = (bilgi.get(r["kod"]) or {}).get("cap")
                out.append(r)
    if len(out) < max(10, len(kodlar) * 0.2):
        raise RuntimeError(f"Yeterli hisse fiyatı alınamadı ({len(out)}/{len(kodlar)}).")
    obj = {"guncelleme": _iso(), "veri_tarihi": max(r["tarih"] for r in out), "taranan": len(kodlar), "kayitlar": out}
    save("hisse", obj)
    return obj


# ── Bilançolar ──────────────────────────────────────────────────────────────
def son_biten_donem(bugun: date, gecikme: int = 3) -> Tuple[int, int]:
    for yil in (bugun.year, bugun.year - 1):
        for p, (m, d) in ((12, (12, 31)), (9, (9, 30)), (6, (6, 30)), (3, (3, 31))):
            if date(yil, m, d) + timedelta(days=gecikme) <= bugun:
                return yil, p
    return bugun.year - 1, 12


def _prev_q(y: int, p: int) -> Tuple[int, int]:
    return (y, p - 3) if p > 3 else (y - 1, 12)


def _donem_str(y: int, p: int) -> str:
    return f"{y}/{p:02d}"


def _donem_var_mi(sess: requests.Session, V, code: str, y: int, p: int, gruplar=("XI_29", "UFRS_K", "UFRS")) -> Optional[str]:
    """Dönem İş Yatırım'da dolu mu? Doluysa bulunan finansal grup kodunu döndürür; yoksa None."""
    qs = [(y, p)]
    for _ in range(3):
        qs.append(_prev_q(*qs[-1]))
    for g in gruplar:
        url = V.FIN_URL_TMPL.replace("financialGroup=XI_29", f"financialGroup={g}").format(
            code=code, y1=qs[0][0], p1=qs[0][1], y2=qs[1][0], p2=qs[1][1],
            y3=qs[2][0], p3=qs[2][1], y4=qs[3][0], p4=qs[3][1])
        r = sess.get(url, headers=V.HEAD, timeout=30)
        r.raise_for_status()
        rows = r.json().get("value") or []
        if sum(1 for it in rows if pd.notna(V.num(it.get("value1")))) >= 10:
            return g
    return None


def _kap_finansal_rapor(sess: requests.Session, V, gun: int = 3) -> List[dict]:
    try:
        df, _note = V.fetch_kap_disclosures_recent(sess, days=gun)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for _, r in df.iterrows():
        if r.get("Kaynak") == "UYARI" or not r.get("Hisse"):
            continue
        if "finansal rapor" in _fold(r.get("Başlık")):
            _skip = {"Hisse", "Tarih", "URL", "KAP Index", "Kaynak", "Başlık"}
            ek = [str(r[c]).strip() for c in r.index if c not in _skip and pd.notna(r[c])
                  and str(r[c]).strip() not in ("", "nan", "None", "NaT")]
            ozet = " · ".join(ek[:3])[:120]
            out.append({"kod": str(r["Hisse"]).upper(), "tarih": str(r.get("Tarih") or ""),
                        "url": r.get("URL") or "", "kap": str(r.get("KAP Index") or ""),
                        "baslik": str(r.get("Başlık") or ""), "ozet": ozet})
    return out


def guncelle_bilanco(workers: int = 12, evren: Optional[List[str]] = None) -> dict:
    V = _vbist()
    sess = requests.Session()
    st_ = load("bilanco")
    durum: Dict[str, dict] = st_.get("durum") or {}
    yeni: List[dict] = st_.get("yeni") or []
    ilk = not durum
    bugun = now().date()
    y, p = son_biten_donem(bugun)
    donem = _donem_str(y, p)
    bitis = {3: (3, 31), 6: (6, 30), 9: (9, 30), 12: (12, 31)}[p]
    pencere_icinde = bugun <= date(y, *bitis) + timedelta(days=85)

    if evren is None:
        evren = [r["kod"] for r in (load("hisse").get("kayitlar") or [])]
        if not evren:
            evren, _ = bist_evren()
    kap = _kap_finansal_rapor(sess, V)
    kap_kodlar = {k["kod"] for k in kap}

    # Yoklanacak hisseler: bu dönemi henüz kaydetmemiş olanlar (pencere içinde) + KAP'ta görünenler
    aday = [k for k in evren if (durum.get(k, {}).get("son") or "") < donem and (pencere_icinde or k in kap_kodlar)]
    for k in kap_kodlar:
        if k not in aday and (durum.get(k, {}).get("son") or "") < donem:
            aday.append(k)
    _STATE.update(part="bilanço", done=0, total=len(aday))

    def work(code):
        gr = durum.get(code, {}).get("grup")
        return code, _donem_var_mi(sess, V, code, y, p, gruplar=(gr,) if gr else ("XI_29", "UFRS_K", "UFRS"))

    bulunan, hata = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(work, c) for c in aday]):
            _STATE["done"] += 1
            try:
                code, grup = f.result()
            except Exception:  # noqa: BLE001
                hata += 1
                continue
            if grup:
                bulunan.append((code, grup))
    if aday and hata == len(aday):
        raise RuntimeError("İş Yatırım MaliTablo erişilemedi (tüm yoklamalar hata verdi).")

    simdi = _iso()
    for code, grup in bulunan:
        onceki = durum.get(code, {}).get("son")
        durum[code] = {"son": donem, "grup": grup, "tespit": simdi}
        if ilk or onceki is None:
            continue   # ilk kurulumda / evrene yeni giren hissede mevcut durumu kaydet, "yeni" sayma
        mevcut = next((e for e in yeni if e["kod"] == code and not e.get("donem")), None)
        if mevcut:
            mevcut.update(donem=donem, tespit=simdi, kaynak=mevcut["kaynak"] + " + İş Yatırım")
        else:
            yeni.append({"kod": code, "donem": donem, "onceki": onceki, "tespit": simdi, "kaynak": "İş Yatırım"})
    # Bulunamayanlar için "önceki dönem" varsayımı (ilk çalıştırmada yeni tespit edilebilsin)
    if ilk:
        for k in evren:
            durum.setdefault(k, {"son": _donem_str(*_prev_q(y, p)), "grup": None, "tespit": None})

    # KAP bildirimleri (bilgi amaçlı; İş Yatırım henüz yansıtmamış olabilir)
    for k in kap:
        nitelik = " · ".join(x for x in (k.get("baslik"), k.get("ozet")) if x)
        mevcut_kap = next((e for e in yeni if e.get("kap") == k["kap"] and k["kap"]), None)
        if mevcut_kap:
            mevcut_kap["kap_nitelik"] = nitelik or mevcut_kap.get("kap_nitelik") or ""
            continue
        var = next((e for e in yeni if e["kod"] == k["kod"] and _gun_farki(e.get("tespit")) <= 10), None)
        if var:
            var.update(kap=k["kap"], url=k["url"], kap_nitelik=nitelik)
            continue
        yeni.append({"kod": k["kod"], "donem": "", "onceki": durum.get(k["kod"], {}).get("son"), "tespit": simdi,
                     "kaynak": "KAP", "url": k["url"], "kap": k["kap"], "kap_tarih": k["tarih"],
                     "kap_nitelik": nitelik})

    esik = (now() - timedelta(days=45)).strftime("%Y-%m-%d")
    yeni = sorted([e for e in yeni if (e.get("tespit") or "")[:10] >= esik], key=lambda e: e.get("tespit") or "", reverse=True)
    try:
        _eksik_yas = [_gun_farki(e.get("tespit")) for e in yeni if not e.get("kap")]
        if _eksik_yas:
            _kap_geri_esle(sess, V, yeni, int(min(45, max(3, max(_eksik_yas) + 1))))
    except Exception:  # noqa: BLE001
        pass
    try:
        _notlari_tamamla(sess, V, yeni, durum, donem)
    except Exception:  # noqa: BLE001  (not üretilemese de bilanço yoklaması başarısız sayılmaz)
        pass
    obj = {"guncelleme": _iso(), "aranan_donem": donem, "pencere": pencere_icinde, "yoklanan": len(aday),
           "yeni_bulunan": 0 if ilk else len(bulunan), "ilk_kurulum": ilk, "durum": durum, "yeni": yeni}
    save("bilanco", obj)
    return obj


def _kap_geri_esle(sess: requests.Session, V, yeni: List[dict], gun: int) -> int:
    """KAP bağlantısı olmayan kayıtlar (ör. İş Yatırım'dan tespit edilmiş) için son `gun` günün KAP
    'Finansal Rapor' bildirimlerinde eşleşme arar; bulursa KAP linkini ve bildirim niteliğini yazar."""
    eksik = [e for e in yeni if not e.get("kap")]
    if not eksik:
        return 0
    by: Dict[str, dict] = {}
    for k in _kap_finansal_rapor(sess, V, gun):
        by.setdefault(k["kod"], k)
    n = 0
    for e in eksik:
        k = by.get(e["kod"])
        if not k:
            continue
        e.update(kap=k["kap"], kap_tarih=k["tarih"],
                 kap_nitelik=" · ".join(x for x in (k.get("baslik"), k.get("ozet")) if x))
        if not e.get("url"):
            e["url"] = k["url"]
        if "KAP" not in (e.get("kaynak") or ""):
            e["kaynak"] = (e.get("kaynak") or "") + " + KAP"
        n += 1
    return n


def kap_eslestir(gun: int = 45) -> dict:
    """Arayüz düğmesi: KAP bağlantısı eksik kayıtları yeniden eşleştirir."""
    if not _RUN_LOCK.acquire(blocking=False):
        return {"hata": "Güncelleme zaten çalışıyor."}
    try:
        bil = load("bilanco")
        yeni = bil.get("yeni") or []
        n = _kap_geri_esle(requests.Session(), _vbist(), yeni, gun)
        save("bilanco", bil)
        return {"eslesen": n, "kalan": sum(1 for e in yeni if not e.get("kap"))}
    finally:
        _RUN_LOCK.release()


def _gun_farki(iso: Optional[str]) -> float:
    d = _parse(iso)
    return 999.0 if not d else (now() - d).total_seconds() / 86400


# ── Bilanço değerlendirme notu ──────────────────────────────────────────────
_NOT_KODLARI = {
    "sat": ("3C", "3A", "3AA"),      # hasılat
    "faal": ("3DF", "3H", "3E"),     # esas faaliyet kârı
    "net": ("3L", "3J", "3I"),       # net dönem kârı
}


def _tr(x: float, d: int = 0) -> str:
    s = f"{abs(x):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if x < 0 else "") + s


def _yoy_txt(g: float) -> str:
    return f"{'+' if g > 0 else ('-' if g < 0 else '')}%{_tr(abs(g), 0)}"


def _mali_kalemler(sess: requests.Session, V, code: str, y: int, p: int, gruplar) -> Optional[dict]:
    """(güncel, geçen yıl aynı dönem) çiftleri: satış / faaliyet kârı / net kâr."""
    qs = [(y, p), (y - 1, p), _prev_q(y, p), _prev_q(y - 1, p)]
    for g in gruplar:
        if not g:
            continue
        url = V.FIN_URL_TMPL.replace("financialGroup=XI_29", f"financialGroup={g}").format(
            code=code, y1=qs[0][0], p1=qs[0][1], y2=qs[1][0], p2=qs[1][1],
            y3=qs[2][0], p3=qs[2][1], y4=qs[3][0], p4=qs[3][1])
        r = sess.get(url, headers=V.HEAD, timeout=30)
        r.raise_for_status()
        by = {str(it.get("itemCode") or ""): it for it in (r.json().get("value") or [])}
        out: dict = {}
        for key, kodlar in _NOT_KODLARI.items():
            for c in kodlar:
                it = by.get(c)
                if not it:
                    continue
                a, b = V.num(it.get("value1")), V.num(it.get("value2"))
                if pd.notna(a):
                    out[key] = (float(a), None if pd.isna(b) else float(b))
                    break
        if "sat" in out or "net" in out:
            out["grup"] = g
            return out
    return None


def _buyume(cur: Optional[float], prev: Optional[float]) -> Tuple[Optional[float], str]:
    """(% değişim, durum). durum: kara | zarara | zarar_azaldi | zarar_buyudu | ''"""
    if cur is None or prev is None:
        return None, ""
    if prev > 0:
        return ((cur / prev - 1) * 100, "zarara" if cur < 0 else "")
    if prev < 0:
        if cur > 0:
            return None, "kara"
        return None, "zarar_azaldi" if cur > prev else "zarar_buyudu"
    return None, ""


def _gui_otomatik_yorum_ekle(kod: str, donem: str = "") -> str:
    """tefas_gui mali_tablo_otomatik_yorum varsa kısa özet ekler."""
    try:
        import sys
        gui = sys.modules.get("tefas_gui") or sys.modules.get("__main__")
        if gui is None or not hasattr(gui, "mali_tablo_otomatik_yorum"):
            return ""
        isy = {}
        if hasattr(gui, "isyatirim_mali_tablo"):
            isy = gui.isyatirim_mali_tablo(kod, force=False) or {}
        deg = {}
        if hasattr(gui, "deg_from_isyatirim"):
            try:
                deg = gui.deg_from_isyatirim(kod) or {}
                if deg.get("_error"):
                    deg = {}
            except Exception:
                deg = {}
        y = gui.mali_tablo_otomatik_yorum(isy if isinstance(isy, dict) else {}, deg)
        bits = []
        if y.get("sonuc"):
            bits.append(str(y["sonuc"]).replace("**", "")[:90])
        if y.get("olumlu"):
            bits.append("+" + str(y["olumlu"][0]).replace("**", "")[:40])
        if y.get("olumsuz"):
            bits.append("−" + str(y["olumsuz"][0]).replace("**", "")[:40])
        return " · ".join(bits)[:160]
    except Exception:
        return ""


def _not_uret(k: dict) -> Tuple[str, Optional[int]]:
    """
    Yeni bilanço final skor (0–100) + kurumsal not.
    Satış/faaliyet/net kâr YoY + marj + isteğe bağlı gui paket.
    Dönüş: (not_metni, puan_ham) — puan_ham geriye uyumluluk; final skor not içinde.
    """
    parca, puan = [], 0
    sat, faal, net = k.get("sat"), k.get("faal"), k.get("net")
    kod = str(k.get("kod") or "")
    donem = str(k.get("donem") or "")

    if sat:
        g, _ = _buyume(*sat)
        if g is not None:
            parca.append(f"Satış {_yoy_txt(g)}")
            puan += 2 if g >= 20 else (1 if g >= 10 else (-1 if g < 0 else 0))
    if faal:
        g, d = _buyume(*faal)
        if g is not None:
            parca.append(f"Faaliyet {_yoy_txt(g)}")
            puan += 2 if g >= 20 else (1 if g >= 10 else (-1 if g < 0 else 0))
        elif d == "kara":
            parca.append("Faaliyet zarardan kâra"); puan += 2
        elif d == "zarar_buyudu":
            parca.append("Faaliyet zararı büyüdü"); puan -= 2
    if net:
        g, d = _buyume(*net)
        if g is not None and d != "zarara":
            parca.append(f"Net kâr {_yoy_txt(g)}")
            puan += 3 if g >= 25 else (2 if g >= 10 else (1 if g >= 0 else -2))
        elif d == "kara":
            parca.append("Net kâr zarardan kâra"); puan += 3
        elif d == "zarara":
            parca.append("Net kâr zarara döndü"); puan -= 3
        elif d == "zarar_azaldi":
            parca.append("Net zarar azaldı"); puan += 1
        elif d == "zarar_buyudu":
            parca.append("Net zarar büyüdü"); puan -= 3
    marj0 = None
    if sat and net and sat[0]:
        marj0 = net[0] / sat[0] * 100
        txt = f"Net marj %{_tr(marj0, 1)}"
        if sat[1] and net[1] is not None:
            m1 = net[1] / sat[1] * 100
            if marj0 - m1 >= 0.5:
                txt += " ↑"; puan += 1
            elif marj0 - m1 <= -0.5:
                txt += " ↓"; puan -= 1
        parca.append(txt)
        if marj0 >= 12:
            puan += 1
        elif marj0 is not None and marj0 < 3:
            puan -= 1

    # 0–100 final skor (ham puan -6..+12 → ölçek)
    final = int(max(0, min(100, round(55 + puan * 6))))

    # Gui paket: kalite / sinyal ile harman
    gui_bits = []
    try:
        import sys
        gui = sys.modules.get("tefas_gui") or sys.modules.get("__main__")
        if gui and hasattr(gui, "_hisse_paket"):
            pack = gui._hisse_paket(kod, force=False)
            if isinstance(pack, dict):
                kal = (pack.get("kalite") or {}).get("skor")
                sig = (pack.get("yorum") or {})
                if kal is not None:
                    try:
                        final = int(round(0.55 * final + 0.45 * float(kal)))
                        gui_bits.append(f"kalite {float(kal):.0f}")
                    except (TypeError, ValueError):
                        pass
                if sig.get("sonuc"):
                    gui_bits.append(str(sig["sonuc"]).replace("**", "")[:70])
        ekstra = _gui_otomatik_yorum_ekle(kod, donem)
        if ekstra:
            gui_bits.append(ekstra[:80])
    except Exception:
        pass

    if final >= 75:
        etiket = "🟢 Güçlü"
    elif final >= 60:
        etiket = "🟡 İyi"
    elif final >= 45:
        etiket = "🟠 Karışık"
    else:
        etiket = "🔴 Zayıf"

    # Kurumsal not metni
    ozet = " · ".join(parca) if parca else "Kalem verisi sınırlı"
    if gui_bits:
        ozet = ozet + " · " + " · ".join(gui_bits[:2])
    not_metni = (
        f"{etiket} · Final skor {final}/100"
        f" · bileşen satış/faaliyet/net/marj"
        f" · {ozet}"
    )

    k["_final_skor"] = final
    k["_etiket"] = etiket
    return not_metni[:320], puan



def _notlari_tamamla(sess: requests.Session, V, yeni: List[dict], durum: Dict[str, dict],
                     aranan_donem: str, workers: int = 8, yenile: bool = False) -> int:
    """Yeni bilanço kayıtlarına 'not' (değerlendirme notu) yazar. Dönemi henüz bilinmeyen (yalnız KAP) kayıtlar
    için aranan dönem İş Yatırım'da denenir; veri yoksa kayıt 'bekliyor' kalır ve sonraki yenilemede tekrar denenir."""
    adaylar = [e for e in yeni if yenile or not e.get("not") or e.get("not_bekliyor") or e.get("final_skor") is None]

    def work(e):
        kod = e["kod"]
        d = e.get("donem") or aranan_donem or ""
        m = re.fullmatch(r"(\d{4})/(\d{1,2})", d)
        if not m:
            return e, None, False
        y, p = int(m.group(1)), int(m.group(2))
        gr = (durum.get(kod) or {}).get("grup")
        gruplar = ((gr,) if gr else ()) + tuple(g for g in ("XI_29", "UFRS_K", "UFRS") if g != gr)
        try:
            return e, _mali_kalemler(sess, V, kod, y, p, gruplar), False
        except Exception:  # noqa: BLE001
            return e, None, True

    n = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(work, e) for e in adaylar]):
            e, k, hata = f.result()
            if hata:
                continue
            kap_only = not e.get("donem")
            if k is None:
                if kap_only:
                    e["not"], e["not_bekliyor"] = "⏳ İş Yatırım'da rakamlar henüz yok (KAP bildirimi alındı)", True
                else:
                    e["not"], e["not_bekliyor"] = "Kalem verisi okunamadı", False
            else:
                txt, puan = _not_uret(k)
                e["not"] = (f"({aranan_donem}) " if kap_only else "") + txt
                e["not_puan"], e["not_bekliyor"] = puan, False
                e["final_skor"] = k.get("_final_skor")
                e["etiket"] = k.get("_etiket")
            n += 1
    return n


def notlari_guncelle(yenile: bool = False) -> dict:
    """Arayüzdeki 'notları hesapla' düğmesi: tam yoklama yapmadan yalnız değerlendirme notlarını doldurur."""
    if not _RUN_LOCK.acquire(blocking=False):
        return {"hata": "Güncelleme zaten çalışıyor."}
    try:
        bil = load("bilanco")
        if not (bil.get("yeni") or []):
            return {"guncellenen": 0}
        n = _notlari_tamamla(requests.Session(), _vbist(), bil["yeni"], bil.get("durum") or {},
                             bil.get("aranan_donem") or "", yenile=yenile)
        save("bilanco", bil)
        return {"guncellenen": n}
    finally:
        _RUN_LOCK.release()


# ── Değerleme yenileme ──────────────────────────────────────────────────────
def son_rapor_zamani() -> Optional[datetime]:
    try:
        import tefas_bist as B
        r = B.list_reports()
        if r:
            return datetime.fromtimestamp(r[0].stat().st_mtime, TZ)
    except Exception:  # noqa: BLE001
        pass
    return None


def bekleyen_degerleme() -> List[dict]:
    """Son BIST değerleme raporundan SONRA bilançosu gelen şirketler."""
    yeni = (load("bilanco").get("yeni") or [])
    rz = son_rapor_zamani()
    if rz is None:
        return yeni
    return [e for e in yeni if (_parse(e.get("tespit")) or rz) > rz]


def degerleme_baslat(wait: bool = False) -> Optional[dict]:
    """vbist_degerleme motorunu (ayrı süreçte) yeniden çalıştırır."""
    import tefas_bist as B
    B.OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = B.OUT_DIR / f"BIST_Degerleme_{now():%Y%m%d_%H%M}.xlsx"
    run = B.start_run(B.build_command({}, out, "", "", False, False), out)
    if wait:
        while run["rc"] is None:
            time.sleep(5)
    return run


def ayarlar() -> dict:
    return {"auto_degerleme": False, "min_yeni": 3, **load("ayarlar")}


# ── Orkestrasyon ────────────────────────────────────────────────────────────
def refresh_all(parts=("hisse", "bilanco", "fon"), wait_degerleme: bool = False, auto_degerleme: Optional[bool] = None) -> dict:
    if not _RUN_LOCK.acquire(blocking=False):
        return {"hata": "Güncelleme zaten çalışıyor."}
    try:
        _STATE["running"] = True
        meta = load("meta")
        meta["son_deneme"] = _iso()
        save("meta", meta)
        hatalar, basarili = {}, []
        for part in parts:
            try:
                {"fon": guncelle_fonlar, "hisse": guncelle_hisseler, "bilanco": guncelle_bilanco}[part]()
                basarili.append(part)
            except Exception as e:  # noqa: BLE001
                hatalar[part] = f"{type(e).__name__}: {e}"[:300]
        meta = load("meta")
        meta["hatalar"] = hatalar
        meta["parcalar"] = {**(meta.get("parcalar") or {}), **{p: _iso() for p in basarili}}
        if basarili:
            meta["son_guncelleme"] = _iso()
        ay = ayarlar()
        otomatik = ay["auto_degerleme"] if auto_degerleme is None else auto_degerleme
        if otomatik and "bilanco" in basarili:
            bek = bekleyen_degerleme()
            son = _parse(meta.get("degerleme_baslangic"))
            if len(bek) >= int(ay["min_yeni"]) and (son is None or (now() - son) > timedelta(hours=20)):
                try:
                    run = degerleme_baslat(wait=wait_degerleme)
                    meta["degerleme_baslangic"] = _iso()
                    meta["degerleme_cikti"] = run["output"]
                except Exception as e:  # noqa: BLE001
                    hatalar["degerleme"] = f"{type(e).__name__}: {e}"[:300]
        save("meta", meta)
        return meta
    finally:
        _STATE.update(running=False, part="", done=0, total=0)
        _RUN_LOCK.release()


def arka_planda_baslat(parts=("hisse", "bilanco", "fon")) -> bool:
    if _STATE["running"]:
        return False
    threading.Thread(target=refresh_all, kwargs={"parts": parts}, daemon=True, name="panel-refresh").start()
    return True


# ── Arayüz ──────────────────────────────────────────────────────────────────
def _renkle(df: pd.DataFrame, kolon: str):
    def f(v):
        try:
            return "color:#34d399" if float(v) > 0 else ("color:#f87171" if float(v) < 0 else "")
        except (TypeError, ValueError):
            return ""
    sty = df.style.format({kolon: "{:+.2f}"}, na_rep="—")
    return (getattr(sty, "map", None) or sty.applymap)(f, subset=[kolon])


def _liste(kayitlar: List[dict], anahtar: str, n: int, yukari: bool, min_ciro: float = 0.0, cap: float = 0.0) -> pd.DataFrame:
    df = pd.DataFrame(kayitlar)
    if df.empty or anahtar not in df.columns:
        return pd.DataFrame()
    df = df.dropna(subset=[anahtar])
    if min_ciro and "ciro" in df.columns:
        df = df[df["ciro"].fillna(0) >= min_ciro]
    if cap:
        df = df[df[anahtar].abs() <= cap]
    df = df.sort_values(anahtar, ascending=not yukari).head(n)
    return df


_PN_CSS = """<style>
.pn-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:4px 0 10px}
.pn-card{container-type:inline-size;overflow:hidden;min-width:0;background:linear-gradient(160deg,#1e293bcc,#0f172acc);border:1px solid #334155;border-radius:10px;padding:8px 12px}
.pn-l{color:#94a3b8;white-space:nowrap}
.pn-v{font-weight:700;color:#f1f5f9;line-height:1.15;white-space:nowrap;margin:2px 0 4px}
.pn-d{display:inline-block;font-weight:600;padding:1px 8px;border-radius:999px;white-space:nowrap;max-width:100%}
.pn-up{background:#0f3d32;color:#3ddc97}.pn-dn{background:#3d1a1a;color:#ff6b6b}.pn-nt{background:#1e293b;color:#94a3b8}
.pn-hero{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 45%,#0f766e 100%);border:1px solid #334155;border-radius:14px;padding:14px 18px;margin:8px 0 12px;box-shadow:0 8px 24px #00000040}
.pn-hero h3{margin:0 0 4px;font-size:1.05rem;color:#f8fafc;font-weight:700}
.pn-hero p{margin:0;color:#cbd5e1;font-size:0.82rem;line-height:1.4}
.pn-chip-row{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px;align-items:center}
.pn-chip{background:#1e293b;border:1px solid #475569;color:#e2e8f0;padding:4px 10px;border-radius:999px;font-size:0.78rem;font-weight:600}
.pn-chip-on{background:linear-gradient(90deg,#0d9488,#2563eb);border-color:transparent;color:#fff}
.pn-chip-arrow{color:#64748b;font-size:0.75rem}
.pn-sec{background:#0f172a99;border:1px solid #334155;border-radius:12px;padding:12px 14px;margin:10px 0 14px}
.pn-sec-title{font-size:0.9rem;font-weight:700;color:#e2e8f0;margin:0 0 8px}
.pn-table-wrap{overflow-x:auto;border-radius:10px;border:1px solid #334155}
.pn-table-wrap table{width:100%;border-collapse:collapse;font-size:0.82rem}
.pn-table-wrap th{background:#1e293b;color:#94a3b8;text-align:left;padding:8px 10px;font-weight:600;white-space:nowrap}
.pn-table-wrap td{padding:7px 10px;border-top:1px solid #1e293b;color:#e2e8f0;white-space:nowrap}
.pn-table-wrap tr:hover td{background:#1e293b66}
</style>"""


def _fit(metin: str, max_rem: float, char_w: float = 0.6) -> str:
    """Kart genişliğine göre yazıyı küçültür (container query); desteklemeyen tarayıcıda max boyut."""
    kk = 100.0 / (char_w * max(len(str(metin)), 1))
    return f"font-size:{max_rem}rem;font-size:min({max_rem}rem,{kk:.1f}cqw)"


def _kpi_html(kartlar: List[Tuple[str, str, str, str]]) -> str:
    """kartlar: (etiket, değer, alt metin, ton[up|dn|nt])"""
    parca = []
    for et, val, alt, ton in kartlar:
        d = (f'<span class="pn-d pn-{ton}" style="{_fit(alt, 0.7, 0.62)}">{alt}</span>' if alt else "")
        parca.append(f'<div class="pn-card"><div class="pn-l" style="{_fit(et, 0.72, 0.55)}">{et}</div>'
                     f'<div class="pn-v" style="{_fit(val, 1.35, 0.62)}">{val}</div>{d}</div>')
    return '<div class="pn-grid">' + "".join(parca) + "</div>"


def _ton(v) -> str:
    try:
        return "up" if float(v) > 0 else ("dn" if float(v) < 0 else "nt")
    except (TypeError, ValueError):
        return "nt"


def render_ana_panel(*, key: str = "panel") -> None:
    """Ön sayfa paneli. Hata olursa ana sayfayı bozmaz."""
    import streamlit as st

    try:
        _render(st, key)
    except Exception as e:  # noqa: BLE001
        st.warning(f"Piyasa paneli yüklenemedi: {type(e).__name__}: {e}")


def _scheduler():
    import streamlit as st

    @st.cache_resource
    def _start():
        def loop():
            while True:
                try:
                    if not _STATE["running"] and due(load("meta")):
                        refresh_all()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(900)
        t = threading.Thread(target=loop, daemon=True, name="panel-scheduler")
        t.start()
        return t

    return _start()


def _render(st, key: str) -> None:
    _scheduler()
    S = st.session_state
    meta, fon, his, bil = load("meta"), load("fon"), load("hisse"), load("bilanco")
    ay = ayarlar()

    # Başlık + durum
    h1, h2 = st.columns([4, 1])
    h1.subheader("📊 Piyasa Paneli")
    son = meta.get("son_guncelleme")

    def _durum():
        if _STATE["running"]:
            d, t = _STATE["done"], _STATE["total"]
            st.progress(d / t if t else 0.0, text=f"Güncelleniyor: {_STATE['part']} {d}/{t or '?'}")
        else:
            if S.get(f"{key}_was_running"):
                S[f"{key}_was_running"] = False
                st.rerun()
        S[f"{key}_was_running"] = bool(_STATE["running"])

    if h2.button("🔄 Şimdi güncelle", key=f"{key}_refresh", disabled=_STATE["running"], use_container_width=True):
        arka_planda_baslat()
        st.rerun()
    if hasattr(st, "fragment"):
        st.fragment(run_every=3 if _STATE["running"] else None)(_durum)()
    else:
        _durum()
    cap = f"Son güncelleme: **{son or 'henüz yok'}** (TSİ)"
    if his.get("veri_tarihi"):
        cap += f" · hisse verisi {his['veri_tarihi']}"
    if fon.get("veri_tarihi"):
        cap += f" · fon verisi {fon['veri_tarihi']}"
    st.caption(cap + " · her hafta içi gün 10:30 ve 19:00'da otomatik yenilenir.")
    for part, msg in (meta.get("hatalar") or {}).items():
        st.warning(f"{part} güncellenemedi: {msg} — son başarılı veri gösteriliyor.")
    if not (fon or his or bil):
        st.info("Henüz veri yok. İlk güncelleme arka planda başlatılır; birkaç dakika sonra sayfayı yenileyin "
                "veya '🔄 Şimdi güncelle'ye basın.")
        return

    # Özet kutuları
    hk = pd.DataFrame(his.get("kayitlar") or [])
    fk = pd.DataFrame(fon.get("kayitlar") or [])
    bek = bekleyen_degerleme() if bil else []
    kartlar = []
    if not hk.empty:
        ort = hk["g1"].mean()
        kartlar.append(("BIST yükselen / düşen", f"{int((hk['g1'] > 0).sum())} / {int((hk['g1'] < 0).sum())}",
                        f"ort. %{ort:+.2f}", _ton(ort)))
    if not fk.empty:
        med = fk["g1"].median()
        kartlar.append(("Fon medyan günlük", f"%{med:+.2f}", f"{int((fk['g1'] > 0).sum())} fon yükseldi", _ton(med)))
    kartlar.append(("Yeni bilanço (45 gün)", str(len(bil.get("yeni") or [])), "", "nt"))
    kartlar.append(("Değerlemesi bekleyen", str(len(bek)), "", "nt"))
    st.markdown(_PN_CSS + _kpi_html(kartlar), unsafe_allow_html=True)

    st.caption("🌐 **Genel Değerlendirme** (4 çeyrek + makro gündem) ayrı sayfada.")

    t_bil, t_his, t_fon = st.tabs(["🆕 Yeni bilançolar", "📈 Hisseler", "💰 Fonlar"])

    # Yeni bilançolar
    with t_bil:
        yeni = bil.get("yeni") or []
        if bil.get("ilk_kurulum"):
            st.info("İlk kurulum tamamlandı: mevcut bilanço durumu kaydedildi. Bundan sonra gelen yeni bilançolar burada listelenir.")
        if not yeni:
            st.write("Son 45 günde tespit edilen yeni bilanço yok."
                     + (f" (Aranan dönem: {bil.get('aranan_donem')}, yoklanan hisse: {bil.get('yoklanan')})" if bil else ""))
        else:
            gunler = sorted({(e.get("tespit") or "")[:10] for e in yeni if e.get("tespit")}, reverse=True)
            sayi = {g: sum(1 for e in yeni if (e.get("tespit") or "")[:10] == g) for g in gunler}
            sec = st.selectbox("Tarih", ["Tümü"] + gunler, key=f"{key}_btarih",
                               format_func=lambda g: g if g == "Tümü" else f"{g} ({sayi[g]})")
            gor = [e for e in yeni if sec == "Tümü" or (e.get("tespit") or "")[:10] == sec]
            # Ana görünüm: 20 satır + dikey kaydırma; tüm satırlar Excel'de
            n_goster = st.slider("Ana görünüm satır sayısı", 10, 50, 20, 5, key=f"{key}_bil_n")
            st.caption(f"Toplam **{len(gor)}** kayıt · ekranda ilk **{min(n_goster, len(gor))}** (aşağı kaydırın).")
            rows = []
            rz = son_rapor_zamani()
            for e in gor:
                bekler = (rz is None) or ((_parse(e.get("tespit")) or rz) > rz)
                # Final skor nottan veya alandan
                fs = e.get("final_skor")
                if fs is None and e.get("not"):
                    import re as _re
                    mm = _re.search(r"Final(?:\s+skor)?\s+(\d+)/100", str(e.get("not")))
                    if mm:
                        fs = int(mm.group(1))
                rows.append({
                    "Hisse": e["kod"],
                    "Dönem": e.get("donem") or "—",
                    "Final skor": fs if fs is not None else "—",
                    "Tespit": e.get("tespit"),
                    "Kaynak": e.get("kaynak"),
                    "KAP bildirimi": e.get("kap_nitelik") or ("KAP'ta eşleşmedi" if not e.get("kap") else "—"),
                    "Değerleme": "⏳ yenilenmeli" if bekler else "✅ güncel",
                    "Final not": e.get("not") or "—",
                    "KAP": e.get("url") or "",
                })
            df = pd.DataFrame(rows)
            cfg = None
            if hasattr(st, "column_config"):
                C = st.column_config
                cfg = {
                    "Hisse": C.TextColumn(width="small"),
                    "Dönem": C.TextColumn(width="small"),
                    "Final skor": C.NumberColumn(width="small", format="%d"),
                    "Tespit": C.TextColumn(width="small"),
                    "Kaynak": C.TextColumn(width="small"),
                    "KAP bildirimi": C.TextColumn(width="medium"),
                    "Değerleme": C.TextColumn(width="small"),
                    "Final not": C.TextColumn(width="medium"),
                    "KAP": C.LinkColumn("KAP", display_text="aç", width="small"),
                }
            # 20 satır yüksekliği ~ sabit panel; fazla satır dikey kayar. Genişlik sığdırılır.
            _nvis = min(len(df), int(st.session_state.get(f"{key}_bil_n") or 20))
            _row_h = 36
            _hdr = 38
            _height = _hdr + _row_h * max(_nvis, 1) + 8  # tam 20 satır sığsın, içeride kaydırma
            # Final notu kısalt — yatay taşmayı azalt
            if "Final not" in df.columns:
                df = df.copy()
                df["Final not"] = df["Final not"].astype(str).str.slice(0, 90)
            st.dataframe(
                df,
                hide_index=True,
                use_container_width=True,
                column_config=cfg,
                height=_height,
            )

            # Excel: tüm satırlar + KAP odaklı
            try:
                import io as _io
                buf = _io.BytesIO()
                with pd.ExcelWriter(buf, engine="openpyxl") as xl:
                    df.to_excel(xl, sheet_name="Yeni_bilanco", index=False)
                    kap_rows = []
                    for e in (bil.get("yeni") or []):
                        if e.get("kap") or e.get("url") or e.get("kap_nitelik"):
                            kap_rows.append({
                                "Hisse": e.get("kod"),
                                "Dönem": e.get("donem"),
                                "KAP": e.get("kap"),
                                "KAP tarih": e.get("kap_tarih"),
                                "Bildirim": e.get("kap_nitelik"),
                                "URL": e.get("url"),
                                "Final skor": e.get("final_skor"),
                                "Not": e.get("not"),
                            })
                    if kap_rows:
                        pd.DataFrame(kap_rows).to_excel(xl, sheet_name="KAP_bildirimleri", index=False)
                _xb = buf.getvalue()
                try:
                    _rd = Path(__file__).resolve().parent / "tefas_raporlar"
                    _rd.mkdir(parents=True, exist_ok=True)
                    _xp = _rd / f"kap_bilanco_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
                    _xp.write_bytes(_xb)
                    st.caption(f"💾 Kaydedildi: `{_xp}`")
                except Exception:
                    pass
                st.download_button(
                    "📥 Excel (bilanço + KAP bildirimleri)",
                    data=_xb,
                    file_name=f"kap_bilanco_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"{key}_kap_excel",
                )
            except Exception as _xe:
                st.caption(f"Excel: {_xe}")

            # Final skor özet kartları (en yüksek 6)
            try:
                skorlu = [e for e in gor if e.get("final_skor") is not None]
                if not skorlu:
                    skorlu = []
                    for e in gor:
                        import re as _re
                        mm = _re.search(r"Final(?:\s+skor)?\s+(\d+)/100", str(e.get("not") or ""))
                        if mm:
                            skorlu.append({**e, "final_skor": int(mm.group(1))})
                skorlu = sorted(skorlu, key=lambda x: int(x.get("final_skor") or 0), reverse=True)[:6]
                if skorlu:
                    st.markdown("##### Final skor notları")
                    st.caption("Yeni bilanço · kural tabanlı final skor (0–100). Yatırım tavsiyesi değildir.")
                    cols = st.columns(min(3, len(skorlu)))
                    for i, e in enumerate(skorlu):
                        fs = int(e.get("final_skor") or 0)
                        badge = "🟢" if fs >= 75 else ("🟡" if fs >= 60 else ("🟠" if fs >= 45 else "🔴"))
                        with cols[i % len(cols)]:
                            st.markdown(
                                f"""
<div style="background:linear-gradient(160deg,#1e293bcc,#0f172acc);border:1px solid #334155;
border-radius:12px;padding:12px 14px;margin-bottom:8px;min-height:110px;">
<div style="font-size:0.75rem;color:#94a3b8;">{badge} {e.get('kod')} · {e.get('donem') or '—'}</div>
<div style="font-size:1.6rem;font-weight:700;color:#f1f5f9;">{fs}<span style="font-size:0.85rem;color:#94a3b8">/100</span></div>
<div style="font-size:0.78rem;color:#cbd5e1;line-height:1.35;">{(str(e.get('not') or '')[:120])}</div>
</div>
""",
                                unsafe_allow_html=True,
                            )
            except Exception:
                pass
            kap_eksik = sum(1 for e in yeni if not e.get("kap"))
            if kap_eksik:
                q1, q2 = st.columns([2, 3])
                if q1.button("🔗 KAP bildirimlerini eşleştir", key=f"{key}_kapes", disabled=_STATE["running"],
                             help="KAP bağlantısı olmayan kayıtlar için son 45 günün KAP 'Finansal Rapor' bildirimlerine bakar."):
                    with st.spinner("KAP bildirimleri aranıyor…"):
                        sonuc = kap_eslestir()
                    if sonuc.get("hata"):
                        st.warning(sonuc["hata"])
                    else:
                        st.session_state[f"{key}_kapmsg"] = (f"{sonuc['eslesen']} kayıt eşleşti, "
                                                             f"{sonuc['kalan']} kayıt için KAP bildirimi bulunamadı.")
                        st.rerun()
                q2.caption(st.session_state.pop(f"{key}_kapmsg", None) or f"{kap_eksik} kayıtta KAP bağlantısı yok.")
            eksik = sum(1 for e in yeni if not e.get("not") or e.get("not_bekliyor"))
            if eksik:
                n1, n2 = st.columns([2, 3])
                if n1.button("📝 Final skor & notları hesapla", key=f"{key}_notlar", disabled=_STATE["running"],
                             help="İş Yatırım mali tablo YoY + final skor 0–100."):
                    with st.spinner("Final skorlar hesaplanıyor…"):
                        sonuc = notlari_guncelle(yenile=True)
                    if sonuc.get("hata"):
                        st.warning(sonuc["hata"])
                    else:
                        st.success(f"Güncellenen: {sonuc.get('guncellenen', 0)}")
                        st.rerun()
                n2.caption(f"{eksik} kayıtta not yok / veri bekleniyor.")
                if st.button("🤖 Otomatik yorum zenginleştir (gui)", key=f"{key}_auto_y",
                             help="tefas_gui mali_tablo_otomatik_yorum ile notları genişletir."):
                    with st.spinner("Otomatik yorum…"):
                        bil2 = load("bilanco")
                        yeni2 = bil2.get("yeni") or []
                        n_upd = 0
                        for e in yeni2:
                            kod = e.get("kod")
                            if not kod:
                                continue
                            ekstra = _gui_otomatik_yorum_ekle(kod, e.get("donem") or "")
                            if not ekstra:
                                continue
                            oldn = e.get("not") or ""
                            if ekstra in oldn:
                                continue
                            e["not"] = ((oldn + " · " if oldn and oldn != "—" else "") + ekstra)[:320]
                            e["not_bekliyor"] = False
                            n_upd += 1
                        bil2["yeni"] = yeni2
                        save("bilanco", bil2)
                        st.success(f"{n_upd} kayıtta yorum güncellendi.")
                        st.rerun()
            st.caption("Not = kural tabanlı özet (satış / faaliyet / net kâr YoY + marj) + isteğe bağlı gui otomatik yorum. Yatırım tavsiyesi değildir.")
        a1, a2 = st.columns([2, 3])
        if a1.button("♻️ Yeni bilançoları değerlemeye yansıt", key=f"{key}_deger", disabled=not bek,
                     help="BIST değerleme motorunu yeniden çalıştırır (tüm evreni baştan hesaplar; uzun sürer)."):
            try:
                S["bist_run"] = degerleme_baslat()
                st.success("Değerleme başlatıldı. İlerlemeyi 🏦 BIST Değerleme → Çalıştır sekmesinden izleyin.")
            except Exception as e:  # noqa: BLE001
                st.error(f"Başlatılamadı: {e}")
        with a2.expander("Otomatik değerleme yenileme"):
            oto = st.checkbox("Yeni bilanço gelince değerlemeyi kendiliğinden yenile (en fazla 20 saatte bir)",
                              value=bool(ay["auto_degerleme"]), key=f"{key}_oto")
            mn = st.number_input("En az kaç şirketin bilançosu gelince?", 1, 50, int(ay["min_yeni"]), key=f"{key}_min")
            if (oto, int(mn)) != (bool(ay["auto_degerleme"]), int(ay["min_yeni"])):
                save("ayarlar", {"auto_degerleme": bool(oto), "min_yeni": int(mn)})
                st.caption("Ayar kaydedildi.")
            st.caption("Motor tüm BIST'i yeniden tarar. Streamlit Cloud gibi kısıtlı ortamlarda değil, kendi bilgisayarında/sunucunda "
                       "çalışan kurulumda kullanın.")

    # Hisseler
    with t_his:
        if hk.empty:
            st.info("Hisse verisi yok.")
        else:
            p, m, n = st.columns([2, 2, 1])
            per = p.radio("Dönem", ["Günlük", "Haftalık", "Aylık"], horizontal=True, key=f"{key}_hp")
            ciro = m.number_input("Min. ort. günlük ciro (milyon TL)", 0.0, 10000.0, 5.0, 1.0, key=f"{key}_hciro",
                                  help="Düşük hacimli hisselerin gürültüsünü ele (son 20 gün ort. fiyat×hacim).")
            nn = n.number_input("Kaç satır", 5, 40, 20, key=f"{key}_hn")
            ak = {"Günlük": "g1", "Haftalık": "h1", "Aylık": "a1"}[per]
            yenikodlar = {e["kod"] for e in (bil.get("yeni") or [])}
            ca, cb = st.columns(2)
            for col, yukari, baslik in ((ca, True, "🟢 En çok artan"), (cb, False, "🔴 En çok düşen")):
                with col:
                    st.markdown(f"**{baslik}**")
                    d = _liste(his["kayitlar"], ak, int(nn), yukari, min_ciro=ciro * 1e6)
                    if d.empty:
                        st.caption("Veri yok.")
                        continue
                    v = pd.DataFrame({"Hisse": [("🆕 " if k in yenikodlar else "") + k for k in d["kod"]],
                                      "Fiyat": d["fiyat"].round(2).values, "Değişim %": d[ak].values,
                                      "Ciro (mn ₺)": (d["ciro"] / 1e6).round(1).values if "ciro" in d else None})
                    st.dataframe(_renkle(v, "Değişim %"), hide_index=True, use_container_width=True)
            st.caption("🆕 = son 45 günde yeni bilançosu gelen şirket. Yatırım tavsiyesi değildir.")

    # Fonlar
    with t_fon:
        if fk.empty:
            st.info("Fon verisi yok.")
        else:
            p, m, n = st.columns([2, 2, 1])
            per = p.radio("Dönem", ["Günlük", "Haftalık", "Aylık"], horizontal=True, key=f"{key}_fp")
            gizle = m.checkbox("Aykırı değerleri gizle", value=True, key=f"{key}_fcap",
                               help="Günlük ±25%, haftalık ±50%, aylık ±100% dışı (çoğunlukla veri hatası/ilk fiyat).")
            nn = n.number_input("Kaç satır", 5, 40, 20, key=f"{key}_fn")
            ak = {"Günlük": "g1", "Haftalık": "h1", "Aylık": "a1"}[per]
            capv = {"g1": 25.0, "h1": 50.0, "a1": 100.0}[ak] if gizle else 0.0
            ca, cb = st.columns(2)
            for col, yukari, baslik in ((ca, True, "🟢 En çok artan"), (cb, False, "🔴 En çok düşen")):
                with col:
                    st.markdown(f"**{baslik}**")
                    d = _liste(fon["kayitlar"], ak, int(nn), yukari, cap=capv)
                    if d.empty:
                        st.caption("Veri yok.")
                        continue
                    v = pd.DataFrame({"Fon": d["kod"].values, "Ad": d["ad"].str.slice(0, 38).values,
                                      "Değişim %": d[ak].values, "Tarih": d["tarih"].values})
                    st.dataframe(_renkle(v, "Değişim %"), hide_index=True, use_container_width=True)
            st.caption(f"{fon.get('taranan', '?')} fon tarandı; fon fiyatları genellikle ertesi sabah netleşir. "
                       "Yatırım tavsiyesi değildir.")



def render_bilanco_4c_analiz(*, key: str = "b4c") -> None:
    """Ayrı sayfa: BIST 100 otomatik 4 çeyrek özeti (ana sayfadan taşındı)."""
    import streamlit as st
    try:
        bil = load("bilanco")
    except Exception:
        bil = {"yeni": [], "aranan_donem": None, "aktif_donem": {}}
    # ── Dönem özeti: toplam satış / kâr / nakit + kâr-zarar adedi ──
    st.markdown(
        _PN_CSS
        + """
<div class="pn-hero">
  <h3>📊 Bilanço dönemi özeti</h3>
  <p>Yalnız <b>BIST 100</b> · İş Yatırım · Toplam satış / brüt / faaliyet / net kâr / nakit ·
  satışa oranlar · kâr–zarar adedi · son 4 çeyrek. Yatırım tavsiyesi değildir.</p>
</div>
""",
        unsafe_allow_html=True,
    )
    # Referans dönem: veriden otomatik + dropdown (son 4 çeyrek seçili zincir)
    from collections import Counter
    _don_auto = bil.get("aranan_donem") or (bil.get("aktif_donem") or {}).get("etiket") or ""
    _don_counts = Counter(
        str(e.get("donem") or "") for e in (bil.get("yeni") or []) if e.get("donem")
    )
    if not _don_auto and _don_counts:
        _don_auto = _don_counts.most_common(1)[0][0]

    # Dropdown seçenekleri: otomatik + yeni listeden dönemler + son 8 çeyrek adayı
    _don_opts = []
    if _don_auto:
        _don_opts.append(_don_auto)
    for d, _n in _don_counts.most_common():
        if d and d not in _don_opts:
            _don_opts.append(d)
    try:
        import sys
        _tg0 = sys.modules.get("tefas_gui") or sys.modules.get("__main__")
        if _tg0 and hasattr(_tg0, "bilanco_onceki_ceyrekler") and _don_auto:
            for d in _tg0.bilanco_onceki_ceyrekler(_don_auto, 8):
                if d not in _don_opts:
                    _don_opts.append(d)
    except Exception:
        pass
    if not _don_opts:
        _don_opts = ["2026/9", "2026/6", "2026/3", "2025/12"]

    c_oz1, c_oz2, c_oz3 = st.columns([2, 2, 1])
    with c_oz1:
        _don_oz = st.selectbox(
            "Son bilanço dönemi (otomatik 4 çeyrek bu döneme göre)",
            options=_don_opts,
            index=0,
            key=f"{key}_donem_sec",
            help="Sadece son dönemi seçin; sistem otomatik 4 çeyrek üretir: örn. 2026/9 → 2026/6 → 2026/3 → 2025/12.",
        )
    with c_oz2:
        try:
            import sys
            _tg1 = sys.modules.get("tefas_gui") or sys.modules.get("__main__")
            _zincir = (
                _tg1.bilanco_onceki_ceyrekler(_don_oz, 4)
                if _tg1 and hasattr(_tg1, "bilanco_onceki_ceyrekler")
                else [_don_oz]
            )
        except Exception:
            _zincir = [_don_oz]
        _chips = []
        for _i, _d in enumerate(_zincir):
            _cls = "pn-chip pn-chip-on" if _i == 0 else "pn-chip"
            _chips.append(f'<span class="{_cls}">{_d}</span>')
            if _i < len(_zincir) - 1:
                _chips.append('<span class="pn-chip-arrow">→</span>')
        st.markdown(
            '<div class="pn-chip-row">' + "".join(_chips) + "</div>"
            + f'<p style="color:#94a3b8;font-size:0.8rem;margin:4px 0 0">Kaynak şirket (yeni liste): '
            + f"<b>{len(bil.get('yeni') or [])}</b></p>",
            unsafe_allow_html=True,
        )
    with c_oz3:
        _run_oz = st.button("4 çeyrek özet (otomatik)", key=f"{key}_donem_ozet", type="primary")
    st.caption(
        "Dropdown’dan referans seçin; özet **otomatik son 4 çeyreği** hesaplar "
        "(2026/9→6→3→2025/12 · 2027/9→6→3→2026/12)."
    )
    _cache_don = st.session_state.get(f"{key}_ozet_donem")
    if _run_oz or (st.session_state.get(f"{key}_ozet4_cache") and _cache_don == _don_oz):
        if _run_oz or not st.session_state.get(f"{key}_ozet4_cache") or _cache_don != _don_oz:
            with st.spinner("Tüm BIST 100 hisseleri · 4 çeyrek…"):
                try:
                    import sys
                    _tg = sys.modules.get("tefas_gui") or sys.modules.get("__main__")
                    if _tg is None or not hasattr(_tg, "bilanco_son_4_ceyrek_arsivli"):
                        raise RuntimeError("bilanco_son_4_ceyrek_arsivli bulunamadı")
                    # Tüm BIST 100 (yeni bilanço listesiyle sınırlama)
                    _oz4 = _tg.bilanco_son_4_ceyrek_arsivli(
                        donem=_don_oz or None, kodlar=None, max_n=150
                    )
                    st.session_state[f"{key}_ozet4_cache"] = _oz4
                    st.session_state[f"{key}_ozet4_donem"] = _don_oz
                    # güncel dilim = ilk özet
                    _ozet = (_oz4.get("ozetler") or [{}])[0] if _oz4 else {}
                    st.session_state[f"{key}_ozet_cache"] = _ozet
                    st.session_state[f"{key}_ozet_donem"] = _don_oz
                except Exception as _oe:
                    st.warning(f"Özet: {_oe}")
                    _ozet = {}
                    _oz4 = {}
        else:
            _ozet = st.session_state.get(f"{key}_ozet_cache") or {}
            _oz4 = st.session_state.get(f"{key}_ozet4_cache") or {}

        def _tlfmt(x):
            try:
                import sys
                _tg = sys.modules.get("tefas_gui") or sys.modules.get("__main__")
                if _tg and hasattr(_tg, "_fmt_tl") and x is not None:
                    return _tg._fmt_tl(x)
            except Exception:
                pass
            try:
                return f"{float(x):,.0f}".replace(",", ".") if x is not None else "—"
            except Exception:
                return "—"

        if _ozet and _ozet.get("n"):
            st.markdown('<div class="pn-sec"><div class="pn-sec-title">Seçili dönem — özet KPI</div>', unsafe_allow_html=True)
            _kar_n = int(_ozet.get("kar_n") or 0)
            _zar_n = int(_ozet.get("zarar_n") or 0)
            st.markdown(
                _kpi_html([
                    ("Şirket", str(_ozet.get("n") or 0), _ozet.get("donem") or "", "nt"),
                    ("Kâr açıklayan", str(_kar_n), f"%{100*_kar_n/max(int(_ozet.get('n') or 1),1):.0f}", "up"),
                    ("Zarar açıklayan", str(_zar_n), f"%{100*_zar_n/max(int(_ozet.get('n') or 1),1):.0f}", "dn"),
                    ("Toplam satış", _tlfmt(_ozet.get("toplam_satis")), "", "nt"),
                    ("Toplam net kâr", _tlfmt(_ozet.get("toplam_net")), "", "up" if (_ozet.get("toplam_net") or 0) >= 0 else "dn"),
                ]),
                unsafe_allow_html=True,
            )
            st.markdown(
                _kpi_html([
                    ("Brüt kâr", _tlfmt(_ozet.get("toplam_brut")), "", "nt"),
                    ("Faaliyet kârı", _tlfmt(_ozet.get("toplam_faaliyet")), "", "nt"),
                    ("Dönem sonu nakit", _tlfmt(_ozet.get("toplam_nakit")), "", "nt"),
                    ("Toplam NPP", _tlfmt(_ozet.get("toplam_npp")), "enflasyon mh.", "dn" if (_ozet.get("toplam_npp") or 0) < 0 else "nt"),
                ]),
                unsafe_allow_html=True,
            )
            st.markdown(
                _kpi_html([
                    ("Brüt / satış", f"{_ozet.get('oran_brut_satis_%') if _ozet.get('oran_brut_satis_%') is not None else '—'}%", "", "nt"),
                    ("Faaliyet / satış", f"{_ozet.get('oran_faaliyet_satis_%') if _ozet.get('oran_faaliyet_satis_%') is not None else '—'}%", "", "nt"),
                    ("Net / satış", f"{_ozet.get('oran_net_satis_%') if _ozet.get('oran_net_satis_%') is not None else '—'}%", "", "nt"),
                    ("Nakit / satış", f"{_ozet.get('oran_nakit_satis_%') if _ozet.get('oran_nakit_satis_%') is not None else '—'}%", "", "nt"),
                ]),
                unsafe_allow_html=True,
            )
            st.markdown("</div>", unsafe_allow_html=True)

            # Yapay zekâ
            try:
                import sys
                _tg_ai = sys.modules.get("tefas_gui") or sys.modules.get("__main__")
                if _tg_ai and hasattr(_tg_ai, "bilanco_ozet_yapay_zeka_yorum"):
                    _aiy = _tg_ai.bilanco_ozet_yapay_zeka_yorum(
                        _ozet, karsilastirma=(_oz4 or {}).get("karsilastirma")
                    )
                    st.markdown(
                        '<div class="pn-sec"><div class="pn-sec-title">🤖 Yapay zekâ ile dönem analizi</div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(_aiy.get("ozet_paragraf") or "")
                    _c1, _c2 = st.columns(2)
                    with _c1:
                        if _aiy.get("olumlu"):
                            st.success("**Olumlu**\n\n" + "\n".join(f"- {x}" for x in _aiy["olumlu"][:6]))
                        if _aiy.get("notr"):
                            st.info("**Not**\n\n" + "\n".join(f"- {x}" for x in _aiy["notr"][:4]))
                    with _c2:
                        if _aiy.get("olumsuz"):
                            st.warning("**Risk / temkin**\n\n" + "\n".join(f"- {x}" for x in _aiy["olumsuz"][:6]))
                        if _aiy.get("aksiyon"):
                            st.caption(" · ".join(_aiy.get("aksiyon") or []))
                    st.caption(
                        f"Model skor: **{_aiy.get('skor')}/100** · {_aiy.get('etiket')} · {_aiy.get('disclaimer')}"
                    )
                    st.markdown("</div>", unsafe_allow_html=True)
            except Exception as _aie:
                st.caption(f"YZ analiz: {_aie}")

            try:
                import pandas as _pd
                _sdf = _pd.DataFrame(_ozet.get("satirlar") or [])
                if not _sdf.empty and "PD/DD" in _sdf.columns:
                    _pdd = _pd.to_numeric(_sdf["PD/DD"], errors="coerce").dropna()
                    if len(_pdd):
                        st.caption(
                            f"PD/DD medyan **{_pdd.median():.2f}** · ortalama **{_pdd.mean():.2f}** "
                            f"({len(_pdd)} şirket)."
                        )
                if not _sdf.empty:
                    with st.expander("Şirket detay (dönem özeti)", expanded=False):
                        _show = _sdf.copy()
                        for c in ("satis", "brut", "faaliyet", "net", "nakit", "npp"):
                            if c in _show.columns:
                                _show[c] = _show[c].map(lambda v: _tlfmt(v) if v is not None else "—")
                        st.dataframe(_show, hide_index=True, use_container_width=True, height=280)
            except Exception:
                pass
        elif _ozet.get("hata"):
            st.caption(_ozet["hata"])

        # Otomatik 4 çeyrek tablosu
        if _oz4.get("karsilastirma"):
            _z4 = _oz4.get("ceyrekler") or []
            _ch4 = []
            for _i, _d in enumerate(_z4):
                _ch4.append(f'<span class="pn-chip{" pn-chip-on" if _i==0 else ""}">{_d}</span>')
                if _i < len(_z4) - 1:
                    _ch4.append('<span class="pn-chip-arrow">→</span>')
            st.markdown(
                '<div class="pn-sec"><div class="pn-sec-title">Otomatik son 4 çeyrek</div>'
                + f'<div class="pn-chip-row">{"".join(_ch4)}</div>',
                unsafe_allow_html=True,
            )
            try:
                import pandas as _pd
                import math
                _cdf = _pd.DataFrame(_oz4["karsilastirma"])
                # veri yok satırlarını çıkar
                if "Şirket" in _cdf.columns:
                    _cdf = _cdf[_cdf["Şirket"].fillna(0).astype(float) > 0].copy()
                def _cell_money(v):
                    if v is None or (isinstance(v, float) and (math.isnan(v) or v == 0 and False)):
                        return "—"
                    if v is None:
                        return "—"
                    try:
                        if isinstance(v, float) and math.isnan(v):
                            return "—"
                    except Exception:
                        pass
                    return _tlfmt(v)
                def _cell_pct(v):
                    if v is None:
                        return "—"
                    try:
                        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                            return "—"
                        return f"{float(v):.2f}".replace(".", ",")
                    except Exception:
                        return "—"
                for col in ("Toplam satış", "Brüt kâr", "Faaliyet kârı", "Net kâr", "Dönem sonu nakit"):
                    if col in _cdf.columns:
                        _cdf[col] = _cdf[col].map(_cell_money)
                for col in ("Brüt/satış %", "Faaliyet/satış %", "Net/satış %", "Nakit/satış %"):
                    if col in _cdf.columns:
                        _cdf[col] = _cdf[col].map(_cell_pct)
                st.markdown(
                    '<div class="pn-table-wrap">',
                    unsafe_allow_html=True,
                )
                st.dataframe(
                    _cdf,
                    hide_index=True,
                    use_container_width=True,
                    height=min(280, 56 + 36 * max(len(_cdf), 1)),
                )
                st.markdown("</div></div>", unsafe_allow_html=True)
                # Sektör + GSYH
                _sek = _oz4.get("sektor_analiz") or {}
                if _sek.get("sektor_tablosu") or _sek.get("gsyh"):
                    st.markdown(
                        '<div class="pn-sec"><div class="pn-sec-title">🏭 Sektörel dağılım · PD/DD · GSYH oranı</div>',
                        unsafe_allow_html=True,
                    )
                    if _sek.get("notlar"):
                        for _n in _sek["notlar"][:5]:
                            st.caption(_n)
                    if _sek.get("sektor_tablosu"):
                        _sdf = _pd.DataFrame(_sek["sektor_tablosu"])
                        for c in ("Satış", "Net kâr", "Nakit"):
                            if c in _sdf.columns:
                                _sdf[c] = _sdf[c].map(_cell_money)
                        if "Net/satış %" in _sdf.columns:
                            _sdf["Net/satış %"] = _sdf["Net/satış %"].map(_cell_pct)
                        st.markdown("**Sektör kırılımı**")
                        st.dataframe(_sdf, hide_index=True, use_container_width=True, height=260)
                    if _sek.get("pd_dd_iliski"):
                        st.markdown("**Satış / kârlılık · PD/DD ilişkisi**")
                        _pdf = _pd.DataFrame(_sek["pd_dd_iliski"])
                        st.dataframe(_pdf, hide_index=True, use_container_width=True)
                    if _sek.get("gsyh"):
                        st.markdown("**Örneklem / Türkiye GSYH (yaklaşık çeyreklik)**")
                        _gdf = _pd.DataFrame(_sek["gsyh"])
                        st.dataframe(_gdf, hide_index=True, use_container_width=True)
                        st.caption(_sek.get("disclaimer") or "")
                    st.markdown("</div>", unsafe_allow_html=True)
            except Exception as _ce:
                st.caption(str(_ce))





def render_genel_degerlendirme(*, key: str = "gd") -> None:
    """Genel Değerlendirme: 4Ç BIST100 + Türkiye/dünya ekonomi & enflasyon & teknoloji."""
    import streamlit as st

    t1, t2 = st.tabs(["📊 BIST Özet", "🌍 Ekonomi & gündem"])

    with t1:
        try:
            render_bilanco_4c_analiz(key=f"{key}_4c")
        except Exception as e:
            st.warning(f"4Ç: {e}")

    with t2:
        st.markdown(
            _PN_CSS
            + """
<div class="pn-hero">
  <h3>🌍 Türkiye & dünya ekonomisi · enflasyon · teknoloji</h3>
  <p>Güncel haber başlıkları (RSS) + kural tabanlı makro notlar. Yatırım tavsiyesi değildir.</p>
</div>
""",
            unsafe_allow_html=True,
        )
        if st.button("Gündemi yenile", key=f"{key}_news_refresh"):
            st.session_state.pop(f"{key}_news", None)
        news = st.session_state.get(f"{key}_news")
        if not news:
            with st.spinner("Ekonomi gündemi çekiliyor…"):
                try:
                    import sys
                    _tg = sys.modules.get("tefas_gui") or sys.modules.get("__main__")
                    if _tg and hasattr(_tg, "ekonomi_gundem_ozeti"):
                        news = _tg.ekonomi_gundem_ozeti(max_items=10)
                    else:
                        news = {"analiz": ["Modül yüklenemedi."], "turkiye": [], "dunya": [], "enflasyon": [], "teknoloji": []}
                    st.session_state[f"{key}_news"] = news
                except Exception as e:
                    news = {"analiz": [str(e)], "turkiye": [], "dunya": [], "enflasyon": [], "teknoloji": []}
                    st.session_state[f"{key}_news"] = news

        if news.get("analiz"):
            st.markdown('<div class="pn-sec"><div class="pn-sec-title">📌 Makro çerçeve</div>', unsafe_allow_html=True)
            for a in news["analiz"]:
                st.markdown(f"- {a}")
            st.markdown("</div>", unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("#### 🇹🇷 Türkiye")
            for r in (news.get("turkiye") or [])[:8]:
                title = r.get("baslik") or ""
                link = r.get("link") or ""
                if link:
                    st.markdown(f"- [{title}]({link})")
                else:
                    st.markdown(f"- {title}")
            st.markdown("#### 📉 Enflasyon / faiz")
            for r in (news.get("enflasyon") or [])[:8]:
                title = r.get("baslik") or ""
                link = r.get("link") or ""
                if link:
                    st.markdown(f"- [{title}]({link})")
                else:
                    st.markdown(f"- {title}")
            if not (news.get("enflasyon") or news.get("turkiye")):
                st.caption("RSS boş — ağ veya kaynak kısıtı. Makro notlara bakın.")
        with c2:
            st.markdown("#### 🌐 Dünya")
            for r in (news.get("dunya") or [])[:8]:
                title = r.get("baslik") or ""
                link = r.get("link") or ""
                if link:
                    st.markdown(f"- [{title}]({link})")
                else:
                    st.markdown(f"- {title}")
            st.markdown("#### 💻 Teknoloji / yapay zekâ")
            for r in (news.get("teknoloji") or [])[:8]:
                title = r.get("baslik") or ""
                link = r.get("link") or ""
                if link:
                    st.markdown(f"- [{title}]({link})")
                else:
                    st.markdown(f"- {title}")
            if not (news.get("dunya") or news.get("teknoloji")):
                st.caption("Teknoloji/dünya RSS boş olabilir.")

        st.caption(
            f"Güncelleme: {news.get('guncelleme') or '—'} · Kaynaklar: AA / Bloomberg HT RSS (erişilebilirse)."
        )
