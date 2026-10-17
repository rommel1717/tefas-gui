#!/usr/bin/env python3
"""
TEFAS Fon Analiz — TEK DOSYA (tam özellik)
==========================================
• TEFAS'ta açık fonlar
• Dağılım + getiri + risk (Sharpe, vol, max DD, Calmar)
• Benchmark: BIST100, Altın, Mevduat, TÜFE → reel / fazla getiri
• Filtre: faiz, kurucu, risk seviyesi
• Favoriler + alarm eşikleri
• Karşılaştırma, portföy, detay, Excel
• Kendi ürettiğimiz, uçtan uca %100 Türkçe HTML risk/getiri raporu (matplotlib, harici bağımlılık yok)
• Quantstats HTML risk/getiri raporu (opsiyonel, alternatif — grafik başlıkları İngilizce kalır)
• Disk cache

Kurulum:
  pip install requests pandas numpy openpyxl matplotlib scipy
  pip install quantstats   # HTML rapor için (opsiyonel)

Çalıştır: python tefas_fon_analiz_tek.py
"""

from __future__ import annotations

import base64
import json
import re
import time
import warnings
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore", category=RuntimeWarning, module="pandas")
warnings.filterwarnings("ignore", category=RuntimeWarning, module="numpy")

try:
    import quantstats as qs
    HAS_QUANTSTATS = True
except ImportError:
    qs = None
    HAS_QUANTSTATS = False

# ═══════════════════════════════════════════════════════════
BASE = "https://www.tefas.gov.tr"
HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://www.tefas.gov.tr",
    "Referer": "https://www.tefas.gov.tr/tr/fon-verileri",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}
ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "tefas_cache"
CACHE_DIR.mkdir(exist_ok=True)
RAPORLAR_DIR = ROOT / "raporlar"
try:
    RAPORLAR_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
CONFIG_PATH = ROOT / "tefas_config.json"
FAV_PATH = ROOT / "tefas_favoriler.json"
ALERT_LOG = ROOT / "tefas_alarmlar.log"


def rapor_yolu(prefix: str, ext: str = "html") -> Path:
    """Tüm HTML/PNG raporları `raporlar/` klasörüne yazar."""
    try:
        RAPORLAR_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    safe = re.sub(r"[^\w\-]+", "_", str(prefix or "rapor")[:48])
    return RAPORLAR_DIR / f"{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext.lstrip('.')}"

ASSET_MAP = {
    "hisse": ["hs", "yhs", "byf"],
    "tahvil": ["dt", "ost", "hb", "fb", "db", "osdb", "dot"],
    "repo": ["tr", "tpp", "r"],
    "mevduat": ["vmtl", "vm", "vmd", "kh", "khtl", "khd", "md"],
    "altin": ["km", "kmbyf", "kmkba"],
    "doviz": ["yba", "ybkb", "eut", "ybosb"],
    "fon": ["yyf", "ybyf"],
}
KATEGORI_ACIKLAMA = {
    "hisse": "Hisse senedi", "tahvil": "Tahvil / bono", "repo": "Repo / PP",
    "mevduat": "Mevduat / katılma", "altin": "Altın", "doviz": "Döviz", "fon": "Diğer fon",
}
# "diger" dahil, tür filtresi + akran grubu (peer group) etiketleri için
TUR_ACIKLAMA = {**KATEGORI_ACIKLAMA, "diger": "Diğer / Belirsiz"}

# ─── Gelişmiş (percentile tabanlı) skor: ağırlık profilleri ───
# Bileşenler: risk_ayarli_getiri, donem_getiri, max_dusus, risk_anomali, maliyet, akran_sapma
# Her satırın toplamı 100'e normalize edilir (elle yazarken tam 100 tutmasan da otomatik ölçeklenir).
GELISMIS_PROFILLER = {
    "1": ("Muhafazakâr", {
        "risk_ayarli_getiri": 20, "donem_getiri": 15, "max_dusus": 30,
        "risk_anomali": 20, "maliyet": 10, "akran_sapma": 5,
    }),
    "2": ("Dengeli", {
        "risk_ayarli_getiri": 30, "donem_getiri": 25, "max_dusus": 15,
        "risk_anomali": 15, "maliyet": 10, "akran_sapma": 5,
    }),
    "3": ("Agresif", {
        "risk_ayarli_getiri": 35, "donem_getiri": 35, "max_dusus": 8,
        "risk_anomali": 12, "maliyet": 5, "akran_sapma": 5,
    }),
    "4": ("Özel", None),
}
GELISMIS_BILESEN_ACIKLAMA = {
    "risk_ayarli_getiri": "Risk-ayarlı getiri (Sharpe+Sortino ort.)",
    "donem_getiri": "Çok dönemli getiri (1A/3A/6A/1Y/3Y ağırlıklı)",
    "max_dusus": "Maksimum düşüş (max drawdown)",
    "risk_anomali": "Anomali + likidite riski",
    "maliyet": "Maliyet (giriş+çıkış komisyonu)",
    "akran_sapma": "Akran grubuna göre getiri sapması",
}
MIN_AKRAN_GRUP = 8  # bir akran grubunda (fon türü) bu sayının altında fon varsa, o bileşen tüm havuz üzerinden hesaplanır
PROFILLER = {
    "1": ("Agresif", {"hisse": 70, "tahvil": 10, "altin": 10, "doviz": 10}),
    "2": ("Dengeli", {"hisse": 40, "tahvil": 25, "repo": 15, "altin": 10, "doviz": 10}),
    "3": ("Muhafazakâr", {"hisse": 15, "tahvil": 30, "repo": 30, "mevduat": 15, "altin": 10}),
    "4": ("Para piyasası", {"repo": 40, "mevduat": 40, "tahvil": 20}),
    "5": ("Altın", {"altin": 60, "hisse": 20, "doviz": 20}),
    "6": ("Özel", None),
}

# SPK (17.09.2026 civarı) TEFAS'ta işleme kapatılan / tasfiye sürecindeki kurucular
YASAKLI_KURUCULAR = [
    "TERA PORTFÖY", "TERA PORTFOY",
    "PUSULA PORTFÖY", "PUSULA PORTFOY",
    "HEDEF PORTFÖY", "HEDEF PORTFOY",
    "ATLAS PORTFÖY", "ATLAS PORTFOY",
    "A1 PORTFÖY", "A1 PORTFOY", "A1 CAPITAL",
    "PARDUS PORTFÖY", "PARDUS PORTFOY",
    "BULLS PORTFÖY", "BULLS PORTFOY",
]
TASFIYE_KELIMELER = [
    "TASFİYE", "TASFIYE", "TASFİYE HALİNDE", "TASFIYE HALINDE",
    "SONA ERDİR", "SONA ERDIR", "LİKİDASYON", "LIKIDASYON",
]


def _normalize_tr(s: str) -> str:
    if not s:
        return ""
    t = str(s).upper()
    for a, b in [("İ", "I"), ("Ş", "S"), ("Ğ", "G"), ("Ü", "U"), ("Ö", "O"), ("Ç", "C")]:
        t = t.replace(a, b)
    return t


def kurucu_yasakli_mi(ad: str) -> bool:
    n = _normalize_tr(ad or "")
    for k in YASAKLI_KURUCULAR:
        if _normalize_tr(k) in n:
            return True
    return False


def unvan_tasfiye_mi(ad: str) -> bool:
    n = _normalize_tr(ad or "")
    for k in TASFIYE_KELIMELER:
        if _normalize_tr(k) in n:
            return True
    return False


def fon_yatirima_uygun_mu(ad: str, tefas_durum: str, sadece_acik: bool = True) -> tuple:
    """Tasfiye / işleme kapalı / yasaklı kurucu elemesi. (uygun_mu, neden)"""
    if unvan_tasfiye_mi(ad):
        return False, "unvanda tasfiye ifadesi"
    if kurucu_yasakli_mi(ad):
        return False, "yasaklı/işleme kapatılan kurucu"
    if sadece_acik:
        d = (tefas_durum or "").lower()
        if not d:
            return False, "TEFAS durum yok"
        if "işlem görüyor" not in d:
            return False, f"TEFAS kapalı: {tefas_durum}"
        if "görmüyor" in d:
            return False, f"TEFAS kapalı: {tefas_durum}"
    return True, "ok"


# Risk paketi: tek tuş eşikler (max_dd negatif; daha kötü düşüş elenir)
RISK_PAKETLER = {
    "1": {
        "ad": "Muhafazakâr",
        "max_risk": 3,
        "max_vol": 12.0,
        "max_dd": -8.0,
        "min_sharpe": None,
        "profil": "3",
        "aciklama": "Risk≤3, vol≤%12, max DD≥−8",
    },
    "2": {
        "ad": "Dengeli",
        "max_risk": 5,
        "max_vol": 25.0,
        "max_dd": -20.0,
        "min_sharpe": 0.0,
        "profil": "2",
        "aciklama": "Risk≤5, vol≤%25, max DD≥−20, Sharpe≥0",
    },
    "3": {
        "ad": "Agresif",
        "max_risk": 7,
        "max_vol": 50.0,
        "max_dd": -40.0,
        "min_sharpe": None,
        "profil": "1",
        "aciklama": "Risk≤7, vol≤%50, max DD≥−40",
    },
    "0": {
        "ad": "Özel / yok",
        "max_risk": None,
        "max_vol": None,
        "max_dd": None,
        "min_sharpe": None,
        "profil": None,
        "aciklama": "Elle filtre",
    },
}

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    plt = None
    HAS_MPL = False

DEFAULT_CONFIG = {
    "risk_free": 0.40,
    "tufe_yillik": 32.0,
    "mevduat_yillik": 40.0,
    "alarmlar": {
        "min_getiri_1y": None,
        "min_sharpe": None,
        "max_drawdown": None,
        "max_volatilite": None,
        "sepet_max_dd": -25.0,       # sepet MaxDD % altı alarm
        "sepet_min_cesit": 35.0,     # çeşitlendirme skoru altı
        "sepet_max_tehlike": 5.5,    # ort. tehlike üstü
    },
}

TARAMA_HISTORY_PATH = ROOT / "tefas_tarama_history.json"
BILANCO_CACHE_PATH = ROOT / "tefas_bilanco_durum.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            base = DEFAULT_CONFIG.copy()
            base.update({k: v for k, v in cfg.items() if k != "alarmlar"})
            base["alarmlar"] = {**DEFAULT_CONFIG["alarmlar"], **(cfg.get("alarmlar") or {})}
            return base
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_favs() -> List[str]:
    if FAV_PATH.exists():
        try:
            return json.loads(FAV_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_favs(favs: List[str]):
    FAV_PATH.write_text(json.dumps(sorted(set(favs)), ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_fee_pct(val) -> Optional[float]:
    """Yönetim ücreti / TER benzeri alanı yüzdeye çevirir (örn. 2.1 veya '%2,1')."""
    if val is None or (isinstance(val, float) and not np.isfinite(val)):
        return None
    try:
        if isinstance(val, (int, float)):
            v = float(val)
            # 0.021 gibi oran gelirse yüzdeye çevir
            if 0 < v < 0.5:
                v *= 100.0
            return v if 0 <= v < 30 else None
        s = str(val).strip().replace("%", "").replace(",", ".")
        if not s or s.lower() in {"nan", "none", "-"}:
            return None
        v = float(s)
        if 0 < v < 0.5:
            v *= 100.0
        return v if 0 <= v < 30 else None
    except Exception:
        return None


def profil_yonetim_ucreti(profil: dict, ozet: dict = None) -> Optional[float]:
    """TEFAS profil/özetten yıllık yönetim ücreti % (bulunursa)."""
    sources = [profil or {}, ozet or {}]
    keys = (
        "yonetimUcreti", "yonetimUcretiOrani", "yonetimUcretOrani",
        "toplamGiderOrani", "toplamGider", "giderOrani", "TER", "ter",
        "fonToplamGiderOrani", "yillikYonetimUcreti", "managementFee",
    )
    for src in sources:
        for k in keys:
            if k in src:
                fee = _parse_fee_pct(src.get(k))
                if fee is not None:
                    return fee
        # anahtar adı içinde geçsin
        for k, v in src.items():
            kl = str(k).lower()
            if any(x in kl for x in ("yonetimucret", "toplamgider", "gideroran", "managementfee", "expense")):
                fee = _parse_fee_pct(v)
                if fee is not None:
                    return fee
    return None


def net_getiri_hesapla(getiri_pct, yonetim_ucreti_yillik_pct, donem_yil: float = 1.0) -> Optional[float]:
    """Brüt getiri % → yaklaşık net getiri % (ücret dönemle orantılı düşülür).

    Basit model: net ≈ (1 + g/100) / (1 + fee*donem/100) - 1
    Ücret bilinmiyorsa None.
    """
    if getiri_pct is None or yonetim_ucreti_yillik_pct is None:
        return None
    try:
        g = float(getiri_pct) / 100.0
        f = float(yonetim_ucreti_yillik_pct) / 100.0 * float(donem_yil)
        if f <= -0.99:
            return None
        return ((1.0 + g) / (1.0 + f) - 1.0) * 100.0
    except Exception:
        return None


def metrik_net_getiriler(metrikler: dict, yonetim_ucreti_yillik_pct: Optional[float]) -> dict:
    """getiri_1a/3a/6a/1y alanlarına net_* ekler."""
    m = dict(metrikler or {})
    fee = yonetim_ucreti_yillik_pct
    m["yonetim_ucreti"] = fee
    periods = {"getiri_1a": 1 / 12, "getiri_3a": 0.25, "getiri_6a": 0.5, "getiri_1y": 1.0, "cagr": 1.0}
    for k, y in periods.items():
        if k in m and m[k] is not None:
            m[f"net_{k}"] = net_getiri_hesapla(m[k], fee, y)
    return m


def save_tarama_snapshot(df: pd.DataFrame, meta: dict = None, max_keep: int = 30) -> None:
    """Fon tarama/sıralama anlık görüntüsünü track record için saklar."""
    if df is None or getattr(df, "empty", True):
        return
    try:
        rows = []
        for _, r in df.head(50).iterrows():
            rows.append({
                "kod": str(r.get("kod") or ""),
                "ad": str(r.get("ad") or "")[:60],
                "getiri_1y": r.get("getiri_1y"),
                "sharpe": r.get("sharpe"),
                "skor": r.get("skor") or r.get("secim_skor") or r.get("rapor_skor"),
                "max_drawdown": r.get("max_drawdown"),
            })
        snap = {
            "tarih": datetime.now().isoformat(timespec="seconds"),
            "n": len(rows),
            "meta": meta or {},
            "fonlar": rows,
        }
        hist = []
        if TARAMA_HISTORY_PATH.exists():
            hist = json.loads(TARAMA_HISTORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(hist, list):
                hist = []
        hist.append(snap)
        hist = hist[-max_keep:]
        TARAMA_HISTORY_PATH.write_text(json.dumps(hist, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        print(f"tarama history yazılamadı: {e}", flush=True)


def load_tarama_history() -> list:
    try:
        if TARAMA_HISTORY_PATH.exists():
            h = json.loads(TARAMA_HISTORY_PATH.read_text(encoding="utf-8"))
            return h if isinstance(h, list) else []
    except Exception:
        pass
    return []


def evaluate_fon_tarama_track(analiz=None, max_snaps: int = 5) -> dict:
    """Eski tarama snapshot'larındaki fonların bugünkü getirisi (basit track record)."""
    hist = load_tarama_history()
    if len(hist) < 1:
        return {"ok": False, "note": "Henüz tarama arşivi yok. Bir tarama/sıralama çalıştırın."}
    # En eski uygun snap (en az 7 gün önce tercih)
    today = datetime.now()
    candidates = []
    for snap in hist:
        try:
            t = datetime.fromisoformat(str(snap.get("tarih", ""))[:19])
        except Exception:
            continue
        age = (today - t).days
        candidates.append((age, t, snap))
    if not candidates:
        return {"ok": False, "note": "Geçerli snapshot yok."}
    candidates.sort(key=lambda x: -x[0])
    age, t0, snap = candidates[0]
    fonlar = snap.get("fonlar") or []
    rows = []
    if analiz is not None and age >= 1:
        for f in fonlar[:15]:
            kod = str(f.get("kod") or "").upper()
            if not kod:
                continue
            try:
                det = analiz.fon_detay(kod)
                m = det.get("metrikler") or {}
                rows.append({
                    "kod": kod,
                    "ad": f.get("ad") or kod,
                    "eski_1y": f.get("getiri_1y"),
                    "bugun_1y": m.get("getiri_1y"),
                    "bugun_1a": m.get("getiri_1a"),
                    "bugun_sharpe": m.get("sharpe"),
                    "eski_skor": f.get("skor"),
                })
            except Exception:
                continue
    return {
        "ok": True,
        "snapshot_tarih": t0.strftime("%Y-%m-%d"),
        "gun_gecmis": age,
        "n_fon": len(fonlar),
        "rows": rows,
        "note": (
            f"{t0.strftime('%d.%m.%Y')} taraması · {age} gün geçti · {len(fonlar)} fon arşivde"
            + (f" · {len(rows)} güncellendi" if rows else " · detay için analiz nesnesi gerekir")
        ),
        "n_snap": len(hist),
    }


def bilanco_donem_durumu(session=None, probe: bool = False) -> dict:
    """Şirket bilançolarının yayımlanma durumu (takvim + isteğe bağlı canlı probe).

    Ana sayfada 'bilançolar geldi' bildirimi için.
    """
    from datetime import timedelta
    # Lag: bilanço dönem sonundan sonra tipik yayımlanma gecikmesi (gün)
    lag = {3: 45, 6: 70, 9: 45, 12: 75}
    month_end = {3: 31, 6: 30, 9: 30, 12: 31}
    today = datetime.now()
    # Aday dönem
    y, p = today.year, 12
    for _ in range(12):
        q_end = datetime(y, p, month_end[p])
        if today >= q_end + timedelta(days=lag[p]):
            break
        # önceki çeyrek
        if p == 3:
            y, p = y - 1, 12
        else:
            p -= 3
    q_end = datetime(y, p, month_end[p])
    publish_est = q_end + timedelta(days=lag[p])
    gun_kaldi = (publish_est.date() - today.date()).days
    donem_ad = {3: "1. Çeyrek (Ocak–Mart)", 6: "2. Çeyrek (Nisan–Haziran)",
                9: "3. Çeyrek (Temmuz–Eylül)", 12: "Yıllık / 4. Çeyrek"}[p]
    # Bir sonraki beklenen
    if p == 12:
        ny, np_ = y + 1, 3
    else:
        ny, np_ = y, p + 3
    next_end = datetime(ny, np_, month_end[np_])
    next_pub = next_end + timedelta(days=lag[np_])

    durum = "geldi" if gun_kaldi <= 0 else "bekleniyor"
    msg = (
        f"{y} {donem_ad} bilançoları yayımlanmış olmalı (tahmini ≥ {publish_est.strftime('%d.%m.%Y')})."
        if durum == "geldi" else
        f"{y} {donem_ad} için tahmini yayımlanma ~{publish_est.strftime('%d.%m.%Y')} "
        f"(~{gun_kaldi} gün)."
    )
    # Canlı probe (yavaş) — cache'li
    probe_ok = None
    if probe:
        try:
            import requests as _req
            sess = session or _req.Session()
            # vbist fonksiyonunu dene
            try:
                import vbist_degerleme_v193 as _vb
                probe_ok = _vb._anchor_has_data(sess, y, p)
            except Exception:
                probe_ok = None
        except Exception:
            probe_ok = None
        if probe_ok is True:
            durum = "geldi"
            msg = f"{y}/{p:02d} bilançoları İş Yatırım’da doğrulandı (probe hisseler dolu)."
        elif probe_ok is False:
            durum = "bekleniyor"
            msg = f"{y}/{p:02d} henüz probe hisselerde dolu değil — yayımlanma sürüyor olabilir."

    out = {
        "yil": y,
        "donem": p,
        "donem_ad": donem_ad,
        "durum": durum,
        "mesaj": msg,
        "tahmini_yayim": publish_est.strftime("%Y-%m-%d"),
        "sonraki_tahmini": next_pub.strftime("%Y-%m-%d"),
        "gun_kaldi": gun_kaldi,
        "probe": probe_ok,
    }
    try:
        BILANCO_CACHE_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return out


def sepet_alarmlari(sepet_analiz: dict, cfg: dict = None) -> List[str]:
    """Portföy sepeti alarm metinleri."""
    al = (cfg or load_config()).get("alarmlar") or {}
    msgs = []
    if not sepet_analiz:
        return msgs
    pm = sepet_analiz.get("port_metrikler") or {}
    dd = pm.get("max_drawdown")
    thr_dd = al.get("sepet_max_dd")
    if dd is not None and thr_dd is not None and float(dd) < float(thr_dd):
        msgs.append(f"Sepet MaxDD %{float(dd):.1f} eşik %{float(thr_dd):.0f} altında")
    ds = sepet_analiz.get("diversify_score")
    thr_c = al.get("sepet_min_cesit")
    if ds is not None and thr_c is not None and float(ds) < float(thr_c):
        msgs.append(f"Çeşitlendirme {float(ds):.0f}/100 · eşik {float(thr_c):.0f}")
    th = sepet_analiz.get("ort_tehlike")
    thr_t = al.get("sepet_max_tehlike")
    if th is not None and thr_t is not None and float(th) > float(thr_t):
        msgs.append(f"Ort. tehlike {float(th):.1f} · eşik {float(thr_t):.1f} üstü")
    g1y = sepet_analiz.get("ort_getiri_1y") or pm.get("getiri_1y")
    if al.get("min_getiri_1y") is not None and g1y is not None and float(g1y) < float(al["min_getiri_1y"]):
        msgs.append(f"Sepet 1Y %{float(g1y):.1f} < min %{float(al['min_getiri_1y']):.0f}")
    return msgs


def bist_track_record_ozet(history_dir=None) -> dict:
    """bist_score_history / Performans Takibi özeti (varsa)."""
    roots = []
    if history_dir:
        roots.append(Path(history_dir))
    roots += [
        ROOT / "bist_history",
        ROOT / "bist_raporlar",
        Path.cwd() / "bist_history",
    ]
    hist_path = None
    for r in roots:
        p = r / "bist_score_history.csv"
        if p.exists():
            hist_path = p
            break
        # alternatif isim
        for cand in r.glob("*score_history*.csv") if r.exists() else []:
            hist_path = cand
            break
        if hist_path:
            break
    if not hist_path:
        return {"ok": False, "note": "BIST skor arşivi yok — en az bir değerleme çalıştırılmalı."}
    try:
        h = pd.read_csv(hist_path)
        n = len(h)
        dates = pd.to_datetime(h.get("snapshot_date"), errors="coerce") if "snapshot_date" in h.columns else None
        first = str(dates.min().date()) if dates is not None and dates.notna().any() else "?"
        last = str(dates.max().date()) if dates is not None and dates.notna().any() else "?"
        # Excel performans sayfası
        perf_note = None
        for r in roots:
            if not r.exists():
                continue
            for x in sorted(r.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
                try:
                    xl = pd.ExcelFile(x)
                    if "Performans Takibi" in xl.sheet_names:
                        pdf = pd.read_excel(xl, sheet_name="Performans Takibi")
                        if not pdf.empty and len(pdf.columns) > 1:
                            return {
                                "ok": True,
                                "note": f"Arşiv {n} satır ({first} → {last})",
                                "path": str(hist_path),
                                "perf_df": pdf.head(30),
                                "source": x.name,
                            }
                except Exception:
                    continue
        return {"ok": True, "note": f"Arşiv {n} satır ({first} → {last}) — performans sayfası henüz yok (6A birikmeli)", "path": str(hist_path), "perf_df": None}
    except Exception as e:
        return {"ok": False, "note": f"Arşiv okunamadı: {e}"}


def log_alarm(msg: str):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | {msg}\n"
    with open(ALERT_LOG, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"  ⚠ ALARM: {msg}")


# ─── Benchmark ───
def yahoo_return(symbol: str, days: int = 365) -> Optional[float]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "1y" if days >= 300 else "6mo", "interval": "1d"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        ts = result["timestamp"]
        pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
        if len(pairs) < 5:
            return None
        end_p = pairs[-1][1]
        hedef = pairs[-1][0] - days * 86400
        eski = [p for p in pairs if p[0] <= hedef] or pairs[:1]
        start_p = eski[-1][1]
        if not start_p:
            return None
        return round((end_p / start_p - 1) * 100, 2)
    except Exception as e:
        print(f"  [benchmark] {symbol}: {e}")
        return None


def normalize_hisse_sembol(kod: str) -> str:
    """THYAO / thyao.IS → THYAO.IS"""
    k = (kod or "").strip().upper().replace(" ", "")
    if not k:
        return ""
    if not k.endswith(".IS") and not k.endswith(".COM") and "=" not in k:
        k = k + ".IS"
    return k


def yahoo_fiyat_df(symbol: str, range_: str = "2y") -> Optional[pd.DataFrame]:
    """Yahoo Finance günlük kapanış → TEFAS uyumlu fiyat_df (tarih, fiyat)."""
    symbol = normalize_hisse_sembol(symbol)
    if not symbol:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = requests.get(
            url,
            params={"range": range_, "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=25,
        )
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        ts = result["timestamp"]
        rows = [
            {"tarih": pd.Timestamp(t, unit="s"), "fiyat": float(c)}
            for t, c in zip(ts, closes) if c is not None
        ]
        if len(rows) < 20:
            return None
        df = pd.DataFrame(rows).sort_values("tarih").drop_duplicates("tarih")
        if getattr(df["tarih"].dt, "tz", None) is not None:
            df["tarih"] = df["tarih"].dt.tz_localize(None)
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"  [hisse] {symbol}: {e}")
        return None


def hisse_analiz(sembol: str, rf: float = 0.40, cfg: Optional[dict] = None) -> dict:
    """
    BIST (veya Yahoo) hisse analizi: full_metrics + vs BIST100 + sade değerlendirme verisi.
    sembol: THYAO, GARAN, ASELS veya THYAO.IS
    """
    sembol = normalize_hisse_sembol(sembol)
    fd = yahoo_fiyat_df(sembol, range_="2y")
    if fd is None or fd.empty:
        return {"error": f"{sembol} fiyat verisi alınamadı", "sembol": sembol}
    m = full_metrics(fd, rf=rf)
    cfg = cfg or load_config()
    bench = {
        "bist100_1y": yahoo_return("XU100.IS", 365),
        "bist100_6a": yahoo_return("XU100.IS", 180),
        "altin_1y": yahoo_return("GC=F", 365),
        "tufe_yillik": cfg.get("tufe_yillik"),
        "mevduat_yillik": cfg.get("mevduat_yillik"),
    }
    m = attach_benchmark(m, bench)
    bist_rets = yahoo_daily_returns("XU100.IS")
    corr = fon_bist_korelasyon(fd, bist_rets)
    an = anomaly_from_prices(fd)
    return {
        "sembol": sembol,
        "kod": sembol.replace(".IS", ""),
        "tip": "hisse",
        "metrikler": m,
        "fiyat_df": fd,
        "bist_korelasyon": corr,
        "anomaly": an,
        "benchmark": bench,
    }


def hisseleri_analiz_et(semboller: list, rf: float = 0.40, cfg: Optional[dict] = None) -> pd.DataFrame:
    """Birden fazla hisseyi analiz edip tablo döner."""
    rows = []
    for s in semboller:
        s = (s or "").strip()
        if not s:
            continue
        r = hisse_analiz(s, rf=rf, cfg=cfg)
        if r.get("error"):
            rows.append({"kod": normalize_hisse_sembol(s).replace(".IS", ""), "sembol": normalize_hisse_sembol(s),
                         "hata": r["error"]})
            continue
        m = r["metrikler"]
        rows.append({
            "kod": r["kod"],
            "sembol": r["sembol"],
            "tip": "hisse",
            "fiyat": m.get("fiyat"),
            "getiri_1a": m.get("getiri_1a"),
            "getiri_3a": m.get("getiri_3a"),
            "getiri_6a": m.get("getiri_6a"),
            "getiri_1y": m.get("getiri_1y"),
            "getiri_3y": m.get("getiri_3y"),
            "reel_1y": m.get("reel_1y"),
            "vs_mevduat": m.get("vs_mevduat"),
            "vs_bist100": m.get("vs_bist100"),
            "volatilite": m.get("volatilite"),
            "max_drawdown": m.get("max_drawdown"),
            "sharpe": m.get("sharpe"),
            "sortino": m.get("sortino"),
            "calmar": m.get("calmar"),
            "cagr": m.get("cagr"),
            "kazanma_orani": m.get("kazanma_orani"),
            "en_kotu_gun": m.get("en_kotu_gun"),
            "en_iyi_gun": m.get("en_iyi_gun"),
            "var95": m.get("var95"),
            "cvar95": m.get("cvar95"),
            "bist_korelasyon": r.get("bist_korelasyon"),
            "risk_skor": (r.get("anomaly") or {}).get("anomaly_skor"),
        })
    return pd.DataFrame(rows)


def portfoy_fon_hisse_analiz(
    analiz: "FonAnaliz",
    weights: dict,
) -> dict:
    """
    weights: {'AAL': 0.3, 'THYAO.IS': 0.2, ...}
    Fon kodları TEFAS, .IS veya 4-5 harfli BIST hisseleri Yahoo'dan.
    """
    if not weights:
        return {"error": "Ağırlık yok"}
    # normalize weights
    tot = sum(float(v) for v in weights.values() if v)
    if tot <= 0:
        return {"error": "Toplam ağırlık 0"}
    w = {str(k).strip().upper(): float(v) / tot for k, v in weights.items() if float(v) > 0}

    series = {}
    meta = {}
    # Ağırlık anahtarlarını işlerken kopya kullan (pop güvenli)
    w_items = list(w.items())
    w = {}
    for kod0, wt in w_items:
        kod = kod0.replace(".IS", "").replace(".is", "")
        force_hisse = kod0.upper().endswith(".IS") or len(kod) >= 5
        profil = None
        is_fon = False
        if not force_hisse:
            try:
                profil = analiz.api.profil(kod)
                is_fon = bool(profil)
            except Exception:
                is_fon = False
        if is_fon:
            try:
                fd = analiz.api.fiyat(kod, periyod=36)
                if fd is not None and not fd.empty:
                    s = fd.set_index("tarih")["fiyat"].astype(float).sort_index()
                    s = s[~s.index.duplicated(keep="last")]
                    series[kod] = s
                    w[kod] = wt
                    meta[kod] = {"tip": "fon", "ad": (profil or {}).get("fonUnvan") or kod}
                    continue
            except Exception as e:
                meta[kod] = {"tip": "fon", "hata": str(e)}
        sym = normalize_hisse_sembol(kod0)
        fd = yahoo_fiyat_df(sym)
        if fd is not None and not fd.empty:
            s = fd.set_index("tarih")["fiyat"].astype(float).sort_index()
            s = s[~s.index.duplicated(keep="last")]
            series[sym] = s
            w[sym] = wt
            meta[sym] = {"tip": "hisse", "ad": sym}
        else:
            meta[kod0] = {"tip": "hisse", "hata": "fiyat yok"}

    if len(series) < 1:
        return {"error": "Hiçbir varlık için fiyat alınamadı", "meta": meta}

    # Align and portfolio returns
    dfp = pd.DataFrame(series).dropna(how="all").ffill().dropna()
    if dfp.empty or len(dfp) < 30:
        return {"error": "Ortak fiyat serisi yetersiz", "meta": meta}
    rets = dfp.pct_change().dropna()
    cols = [c for c in rets.columns if c in w]
    if not cols:
        return {"error": "Ağırlık-seri eşleşmedi", "meta": meta}
    ww = np.array([w[c] for c in cols])
    ww = ww / ww.sum()
    port_ret = (rets[cols] * ww).sum(axis=1)
    # Fake fiyat_df for full_metrics
    port_px = (1 + port_ret).cumprod() * 100
    fd_port = pd.DataFrame({"tarih": port_px.index, "fiyat": port_px.values})
    m = full_metrics(fd_port, rf=analiz.rf)
    cfg = load_config()
    bench = fetch_benchmarks(cfg)
    m = attach_benchmark(m, bench)
    return {
        "weights": {c: float(ww[i]) for i, c in enumerate(cols)},
        "metrikler": m,
        "meta": meta,
        "n_gun": len(port_ret),
        "port_series": port_ret,
    }


def fetch_benchmarks(cfg: dict) -> dict:
    print("Benchmark çekiliyor...")
    out = {
        "bist100_1y": yahoo_return("XU100.IS", 365),
        "bist100_6a": yahoo_return("XU100.IS", 180),
        "altin_1y": yahoo_return("GC=F", 365),
        "tufe_yillik": cfg.get("tufe_yillik"),
        "mevduat_yillik": cfg.get("mevduat_yillik"),
    }
    print(f"  BIST100 1Y: {out['bist100_1y']}% | Altın USD 1Y: {out['altin_1y']}%")
    print(f"  TÜFE: %{out['tufe_yillik']} | Mevduat: %{out['mevduat_yillik']}")
    return out


def attach_benchmark(m: dict, bench: dict) -> dict:
    g1y = m.get("getiri_1y")
    if g1y is None or not np.isfinite(float(g1y)):
        return m
    g1y = float(g1y)
    if bench.get("tufe_yillik") is not None:
        m["reel_1y"] = round(g1y - float(bench["tufe_yillik"]), 2)
    if bench.get("mevduat_yillik") is not None:
        m["vs_mevduat"] = round(g1y - float(bench["mevduat_yillik"]), 2)
    if bench.get("bist100_1y") is not None:
        m["vs_bist100"] = round(g1y - float(bench["bist100_1y"]), 2)
    if bench.get("altin_1y") is not None:
        m["vs_altin"] = round(g1y - float(bench["altin_1y"]), 2)
    return m


# ─── API ───
class TefasAPI:
    def __init__(self, fon_tipi: str = "YAT", sleep: float = 0.3):
        self.fon_tipi = fon_tipi.upper()
        self.sleep = sleep
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _cpath(self, key: str) -> Path:
        return CACHE_DIR / f"{key.replace('/', '_').replace(':', '_')}.json"

    def _cget(self, key: str, hours: float = 12) -> Optional[list]:
        p = self._cpath(key)
        if not p.exists() or time.time() - p.stat().st_mtime > hours * 3600:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _cset(self, key: str, data: list):
        self._cpath(key).write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")

    def _post(self, endpoint: str, payload: dict, ckey: str = None, hours: float = 12) -> list:
        if ckey:
            c = self._cget(ckey, hours)
            if c is not None:
                return c
        try:
            r = self.session.post(f"{BASE}{endpoint}", json=payload, timeout=45)
            r.raise_for_status()
            rows = (r.json()).get("resultList") or []
            if ckey and rows:
                self._cset(ckey, rows)
            time.sleep(self.sleep)
            return rows
        except Exception as e:
            print(f"  [API] {endpoint}: {e}")
            return []

    def _tarih(self, d: int = 0) -> str:
        return (datetime.now() - timedelta(days=d)).strftime("%Y%m%d")

    def dagilimlar(self) -> pd.DataFrame:
        rows = self._post(
            "/api/funds/dagilimSiraliGetirT",
            {"dil": "TR", "fonTipi": self.fon_tipi, "basTarih": self._tarih(5),
             "bitTarih": self._tarih(0), "basSira": 1, "bitSira": 5000},
            ckey=f"dagilim_{self.fon_tipi}_{self._tarih(0)}", hours=6,
        )
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).rename(columns={"fonKodu": "kod", "fonUnvan": "ad", "tarih": "tarih"})
        return df.drop_duplicates(subset=["kod"], keep="last")

    def profil(self, kod: str) -> dict:
        rows = self._post("/api/funds/fonProfilBilgiGetir", {"dil": "TR", "fonKodu": kod.upper()},
                          ckey=f"profil_{kod}", hours=24)
        return rows[0] if rows else {}

    def tefas_acik_mi(self, kod: str) -> bool:
        p = self.profil(kod)
        d = (p.get("tefasDurum") or "").lower()
        if "işlem görüyor" not in d or "görmüyor" in d:
            return False
        ad = p.get("fonUnvan") or ""
        ok, _ = fon_yatirima_uygun_mu(ad, p.get("tefasDurum") or "", sadece_acik=True)
        return ok

    def fon_uygun_mu(self, kod: str, ad: str = "") -> tuple:
        """(uygun, neden, profil_dict)"""
        if ad and (kurucu_yasakli_mi(ad) or unvan_tasfiye_mi(ad)):
            return False, "yasaklı kurucu veya tasfiye unvanı", {}
        p = self.profil(kod)
        ad2 = ad or p.get("fonUnvan") or ""
        ok, neden = fon_yatirima_uygun_mu(ad2, p.get("tefasDurum") or "", sadece_acik=True)
        return ok, neden, p

    def ozet(self, kod: str) -> dict:
        rows = self._post("/api/funds/fonBilgiGetir", {"dil": "TR", "fonKodu": kod.upper()},
                          ckey=f"ozet_{kod}", hours=6)
        return rows[0] if rows else {}

    def fiyat(self, kod: str, periyod: int = 12) -> pd.DataFrame:
        rows = self._post("/api/funds/fonFiyatBilgiGetir",
                          {"fonKodu": kod.upper(), "dil": "TR", "periyod": periyod},
                          ckey=f"fiyat_{kod}_{periyod}", hours=12)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["tarih"] = pd.to_datetime(df["tarih"], errors="coerce")
        df["fiyat"] = pd.to_numeric(df["fiyat"], errors="coerce")
        df = df.dropna(subset=["tarih", "fiyat"]).sort_values("tarih")
        return df[["tarih", "fiyat", "fonKodu", "fonUnvan"]].reset_index(drop=True)

    def dagilim_ozet(self, row: pd.Series) -> Dict[str, float]:
        ozet = {k: 0.0 for k in ASSET_MAP}
        ozet["diger"] = 0.0
        for col, val in row.items():
            if col in ("kod", "ad", "tarih", "bilFiyat", "rn", "fonKodu", "fonUnvan"):
                continue
            try:
                pct = float(val) if pd.notna(val) else 0.0
            except (TypeError, ValueError):
                continue
            if pct <= 0:
                continue
            col_l = str(col).lower()
            hit = False
            for kat, kodlar in ASSET_MAP.items():
                if col_l in kodlar:
                    ozet[kat] += pct
                    hit = True
                    break
            if not hit:
                ozet["diger"] += pct
        return ozet


def kurucu_from_ad(ad: str) -> str:
    if not ad:
        return ""
    m = re.match(r"^(.+?PORTF[ÖO]Y)\b", ad.upper())
    return m.group(1).title() if m else " ".join(ad.split()[:2])


def faiz_durum(profil: dict, ad: str = "") -> str:
    f = profil.get("faizIcerigi")
    if f:
        return str(f)
    ad_l = (ad or "").lower()
    if "katılım" in ad_l or "katilim" in ad_l:
        return "Katılım (faizsiz eğilim)"
    return "Bilinmiyor"


# ─── Metrikler ───
def _dr(s: pd.Series) -> pd.Series:
    return s.pct_change().dropna()


def period_returns(s: pd.Series) -> dict:
    """1A, 3A, 6A, 1Y, 3Y getiri (%)"""
    empty = {
        "getiri_1a": None, "getiri_3a": None, "getiri_6a": None,
        "getiri_1y": None, "getiri_3y": None,
    }
    if s is None or len(s.dropna()) < 5:
        return empty
    s = s.dropna().sort_index()
    son, t = s.iloc[-1], s.index[-1]

    def ret(g):
        e = s[s.index <= t - pd.Timedelta(days=g)]
        if e.empty:
            return None
        base = float(e.iloc[-1])
        if base == 0 or not np.isfinite(base) or not np.isfinite(float(son)):
            return None
        val = (float(son) / base - 1) * 100
        if not np.isfinite(val) or abs(val) > 1e6:
            return None
        return round(val, 2)

    return {
        "getiri_1a": ret(30),
        "getiri_3a": ret(90),
        "getiri_6a": ret(180),
        "getiri_1y": ret(365),
        "getiri_3y": ret(365 * 3),
    }


def full_metrics(fd: pd.DataFrame, rf: float = 0.40) -> dict:
    """
    Menü 4 → 1 (Türkçe HTML rapor) ile aynı temel metrik seti:
    dönem getirileri + Sharpe/Sortino/Calmar + vol/MaxDD +
    CAGR, kazanma oranı, VaR95, cVaR95, en iyi/kötü gün.
    """
    if fd is None or fd.empty:
        return {}
    s = fd.set_index("tarih")["fiyat"].sort_index().astype(float)
    s = s[~s.index.duplicated(keep="last")].dropna()
    out = period_returns(s)
    r = _dr(s)
    out["volatilite"] = round(float(r.std() * np.sqrt(252) * 100), 2) if len(r) >= 10 else None
    out["max_drawdown"] = round(float((s / s.cummax() - 1).min() * 100), 2) if len(s) >= 5 else None
    out["sharpe"] = out["calmar"] = out["sortino"] = None
    out["cagr"] = out["kazanma_orani"] = out["var95"] = out["cvar95"] = None
    out["en_iyi_gun"] = out["en_kotu_gun"] = None

    if len(r) >= 10:
        # CAGR (rapor ile aynı formül)
        kumulatif = (1 + r).cumprod()
        yil = max(len(r) / 252.0, 0.05)
        if float(kumulatif.iloc[-1]) > 0:
            out["cagr"] = round((float(kumulatif.iloc[-1]) ** (1 / yil) - 1) * 100, 2)
        out["kazanma_orani"] = round(float((r > 0).mean() * 100), 1)
        out["en_iyi_gun"] = round(float(r.max() * 100), 2)
        out["en_kotu_gun"] = round(float(r.min() * 100), 2)
        out["var95"] = round(float(np.percentile(r, 5) * 100), 2)
        kuyruk = r[r <= np.percentile(r, 5)]
        if len(kuyruk):
            out["cvar95"] = round(float(kuyruk.mean() * 100), 2)

    if len(r) >= 20 and r.std() and not np.isnan(r.std()):
        out["sharpe"] = round(float((r.mean() * 252 - rf) / (r.std() * np.sqrt(252))), 2)
        downside = r[r < 0]
        if len(downside) >= 5 and downside.std() and not np.isnan(downside.std()):
            dd_std = float(downside.std() * np.sqrt(252))
            out["sortino"] = round(float((r.mean() * 252 - rf) / dd_std), 2) if dd_std > 1e-9 else None
        mdd = out["max_drawdown"]
        out["calmar"] = round(float(r.mean() * 252) / abs(mdd / 100), 2) if mdd else None

    out["fiyat"] = round(float(s.iloc[-1]), 6)
    return out


def donem_puani(m: dict) -> float:
    """
    Dönemsel performans puanı (dağılım kriteri yok).
    Son 1 yıldaki dönemler + 3Y + Sharpe ağırlıklı.
    """
    def clip(v, lo, hi):
        if v is None:
            return 0.0
        return max(lo, min(float(v), hi))

    return round(
        clip(m.get("getiri_1a"), -20, 40) * 0.08
        + clip(m.get("getiri_3a"), -30, 80) * 0.12
        + clip(m.get("getiri_6a"), -40, 120) * 0.15
        + clip(m.get("getiri_1y"), -50, 200) * 0.22
        + clip(m.get("getiri_3y"), -80, 400) * 0.10
        + clip(m.get("sharpe"), -2, 5) * 10
        + clip(m.get("sortino"), -2, 6) * 6
        + clip(m.get("cagr"), -30, 80) * 0.12
        + clip(m.get("kazanma_orani"), 30, 70) * 0.08
        + (5 if (m.get("max_drawdown") is not None and m["max_drawdown"] >= -10) else 0)
        - (8 if (m.get("var95") is not None and m["var95"] < -4) else 0),
        2,
    )


def her_donem_pozitif_1y(m: dict) -> bool:
    """Son 1 yılda 1A, 3A, 6A, 1Y hepsi pozitif mi?"""
    for k in ("getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y"):
        v = m.get(k)
        if v is None or v <= 0:
            return False
    return True


def anomaly_from_prices(fd: pd.DataFrame) -> dict:
    """
    Fiyat serisinden anormal hareket.
    TEFAS fiyat API'sinde işlem hacmi yok → günlük getiri sıçramaları + vol rejimi.
    """
    out = {
        "max_gunluk_getiri": None,
        "min_gunluk_getiri": None,
        "jump_sayisi": 0,
        "vol_kisa": None,
        "vol_oran": None,
        "anomaly_skor": 0.0,
        "risk_bayrak": "",
    }
    if fd is None or fd.empty or len(fd) < 15:
        return out
    s = fd.set_index("tarih")["fiyat"].sort_index().astype(float)
    s = s[~s.index.duplicated(keep="last")].dropna()
    r = s.pct_change().dropna()
    if len(r) < 10:
        return out

    max_d = float(r.max() * 100)
    min_d = float(r.min() * 100)
    out["max_gunluk_getiri"] = round(max_d, 2)
    out["min_gunluk_getiri"] = round(min_d, 2)

    mu, sigma = float(r.mean()), float(r.std())
    jumps = 0
    if sigma > 0 and not np.isnan(sigma):
        jumps = int((np.abs(r - mu) > 3 * sigma).sum())
        out["jump_sayisi"] = jumps

    r_short = r.tail(21)
    vol_l = float(r.std() * np.sqrt(252) * 100) if r.std() and not np.isnan(r.std()) else 0.0
    if len(r_short) >= 10 and r_short.std() and not np.isnan(r_short.std()):
        vol_s = float(r_short.std() * np.sqrt(252) * 100)
        out["vol_kisa"] = round(vol_s, 2)
        if vol_l > 1e-9:
            out["vol_oran"] = round(vol_s / vol_l, 2)

    bayraklar = []
    skor = 0.0
    if max_d >= 8:
        bayraklar.append(f"aşırı+gün %{max_d:.1f}")
        skor += min(max_d / 2, 25)
    if min_d <= -8:
        bayraklar.append(f"aşırı-gün %{min_d:.1f}")
        skor += min(abs(min_d) / 2, 25)
    if jumps >= 5:
        bayraklar.append(f"{jumps} sıçrama(>3σ)")
        skor += jumps * 2
    if out["vol_oran"] is not None and out["vol_oran"] >= 2.0:
        bayraklar.append(f"vol artışı x{out['vol_oran']}")
        skor += min((out["vol_oran"] - 1) * 10, 20)
    if out["vol_oran"] is not None and out["vol_oran"] <= 0.4 and vol_l > 5:
        bayraklar.append(f"vol çöküşü x{out['vol_oran']}")
        skor += 5

    out["anomaly_skor"] = round(skor, 1)
    out["risk_bayrak"] = " | ".join(bayraklar)
    return out


def likidite_risk(
    ozet: dict,
    metrik: dict,
    dagilim: dict = None,
    profil: dict = None,
    anomaly: dict = None,
) -> dict:
    """
    Fon likidite analizi (0–100 skor; yüksek = daha likit).

    Bileşenler:
      • AUM (portföy büyüklüğü)
      • Yatırımcı sayısı
      • Likit varlık payı (repo + mevduat + tahvil)
      • Valör (geri alım günü)
      • Anomali cezası (fiyat sıçraması)
      • Yüksek getiri + küçük fon cezası

    TEFAS API'de işlem hacmi yok; dolaylı göstergeler kullanılır.
    """
    bayrak = []
    dagilim = dagilim or {}
    profil = profil or {}
    anomaly = anomaly or {}

    def _f(x, default=None):
        try:
            if x is None or (isinstance(x, float) and (x != x)):
                return default
            return float(x)
        except (TypeError, ValueError):
            return default

    buyukluk = _f(ozet.get("portBuyukluk"))
    yat = _f(ozet.get("yatirimciSayi"))
    if yat is not None:
        yat = int(yat)

    # --- AUM puanı (0–30) ---
    if buyukluk is None:
        aum_p = 12.0
    elif buyukluk >= 5_000_000_000:
        aum_p = 30.0
    elif buyukluk >= 1_000_000_000:
        aum_p = 26.0
    elif buyukluk >= 500_000_000:
        aum_p = 22.0
    elif buyukluk >= 100_000_000:
        aum_p = 16.0
    elif buyukluk >= 50_000_000:
        aum_p = 10.0
        bayrak.append(f"küçük AUM ({buyukluk/1e6:.1f}Mn)")
    elif buyukluk >= 10_000_000:
        aum_p = 5.0
        bayrak.append(f"küçük AUM ({buyukluk/1e6:.1f}Mn)")
    else:
        aum_p = 1.0
        bayrak.append("çok küçük AUM")

    # --- Yatırımcı puanı (0–20) ---
    if yat is None:
        yat_p = 8.0
    elif yat >= 10000:
        yat_p = 20.0
    elif yat >= 1000:
        yat_p = 16.0
    elif yat >= 300:
        yat_p = 12.0
    elif yat >= 100:
        yat_p = 8.0
    elif yat >= 20:
        yat_p = 4.0
        bayrak.append(f"az yatırımcı ({yat})")
    else:
        yat_p = 1.0
        bayrak.append(f"çok az yatırımcı ({yat})")

    # --- Likit varlık payı (0–25): repo + mevduat + tahvil ---
    # dagilim keys: d_repo / repo etc.
    def _dget(*keys):
        for k in keys:
            v = dagilim.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return 0.0

    repo = _dget("d_repo", "repo")
    mev = _dget("d_mevduat", "mevduat")
    tahvil = _dget("d_tahvil", "tahvil")
    hisse = _dget("d_hisse", "hisse")
    likit_pct = repo + mev + tahvil
    if likit_pct >= 80:
        varlik_p = 25.0
    elif likit_pct >= 60:
        varlik_p = 20.0
    elif likit_pct >= 40:
        varlik_p = 14.0
    elif likit_pct >= 20:
        varlik_p = 8.0
    else:
        varlik_p = 3.0
        if hisse >= 50:
            bayrak.append(f"hisse ağır ({hisse:.0f}%)")

    # --- Valör (0–15): geri alım ne kadar geç ---
    valor = profil.get("fonGeriAlisValor")
    try:
        valor = int(float(valor)) if valor is not None else None
    except (TypeError, ValueError):
        valor = None
    if valor is None:
        valor_p = 8.0
    elif valor <= 0:
        valor_p = 15.0
    elif valor == 1:
        valor_p = 12.0
    elif valor == 2:
        valor_p = 8.0
    else:
        valor_p = 4.0
        bayrak.append(f"valör T+{valor}")

    # --- Anomali / şüpheli yapı cezası (0–10 düşüş) ---
    ceza = 0.0
    an_skor = _f(anomaly.get("anomaly_skor"), 0) or 0
    if an_skor >= 20:
        ceza += 6
        bayrak.append("yüksek fiyat anomalisi")
    elif an_skor >= 10:
        ceza += 3

    g1y = _f(metrik.get("getiri_1y"))
    if g1y is not None and buyukluk is not None:
        if g1y > 80 and buyukluk < 100_000_000:
            ceza += 8
            bayrak.append("yüksek getiri+küçük fon")
        if g1y > 150:
            ceza += 4
            bayrak.append(f"aşırı 1Y %{g1y:.0f}")

    ham = aum_p + yat_p + varlik_p + valor_p - ceza
    likidite_skor = max(0.0, min(100.0, round(ham, 1)))

    if likidite_skor >= 70:
        seviye = "yüksek"
    elif likidite_skor >= 45:
        seviye = "orta"
    else:
        seviye = "düşük"
        if "düşük likidite" not in " ".join(bayrak):
            bayrak.append("düşük likidite")

    # Eski risk_skor bileşeniyle uyum (yüksek = daha riskli)
    # 100 - likidite_skor ölçeğinin bir kısmı
    likidite_risk_skor = round(max(0.0, (70 - likidite_skor) * 0.35), 1)

    return {
        "likidite_skor": likidite_skor,          # 0–100, yüksek=iyi
        "likidite_seviye": seviye,               # yüksek / orta / düşük
        "likidite_skor_risk": likidite_risk_skor,  # anomali risk_skor'a ek
        "likidite_bayrak": " | ".join(bayrak),
        "likit_varlik_pct": round(likit_pct, 1),
        "valor_geri_alis": valor,
        "buyukluk": buyukluk,
        "yatirimci": yat,
        # geriye uyumluluk
        "likidite_skor_eski": likidite_risk_skor,
    }


def birlestir_risk_bayrak(*parts: str) -> str:
    items = []
    for p in parts:
        if p:
            items.extend([x.strip() for x in str(p).split("|") if x.strip()])
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return " | ".join(out)


def peer_return_zscore(df: pd.DataFrame, col: str = "getiri_1y") -> pd.Series:
    if col not in df.columns or df.empty:
        return pd.Series([None] * len(df), index=df.index)
    s = pd.to_numeric(df[col], errors="coerce")
    mu, sig = s.mean(), s.std()
    if sig is None or not sig or (isinstance(sig, float) and np.isnan(sig)):
        return pd.Series([0.0] * len(df), index=df.index)
    return ((s - mu) / sig).round(2)


def baskin_varlik_turu(ozet: Dict[str, float]) -> str:
    """Bir fonun dağılım özetinden (d_hisse, d_tahvil, ...) baskın varlık türünü döndürür.
    Bu, 'fon türü' filtresi ve gelişmiş skorun akran grubu (peer group) anahtarıdır."""
    if not ozet:
        return "diger"
    best_k, best_v = "diger", -1.0
    for k in list(ASSET_MAP.keys()) + ["diger"]:
        v = ozet.get(k, 0.0) or 0.0
        if v > best_v:
            best_v, best_k = v, k
    return best_k


def _grup_percentile(s: pd.Series, grup: pd.Series, tersine: bool = False,
                      min_grup: int = MIN_AKRAN_GRUP) -> pd.Series:
    """Seriyi akran grubu (aynı fon türü) içinde 0-100 percentile'a çevirir.
    Bir grubun boyutu min_grup'un altındaysa, o gruptaki satırlar tüm havuz
    ('_TUM_') üzerinden hesaplanır — küçük gruplarda percentile anlamsızlaşmasın diye.
    tersine=True: düşük ham değer daha iyi demektir (maliyet, risk_skor gibi)."""
    s = pd.to_numeric(s, errors="coerce")
    grup = grup.fillna("diger").astype(str)
    boyutlar = grup.value_counts()
    eff_grup = grup.where(grup.map(boyutlar) >= min_grup, "_TUM_")
    pct = s.groupby(eff_grup).rank(pct=True, method="average") * 100
    if tersine:
        pct = 100 - pct
    return pct


def _bilesik_getiri(row) -> Optional[float]:
    """1A/3A/6A/1Y/3Y getirilerinin ağırlıklı ortalaması; eksik dönemler
    ağırlıkları yeniden normalize ederek dışarıda bırakılır (0'a çekilmez)."""
    agirliklar = [
        ("getiri_1a", 0.10), ("getiri_3a", 0.15), ("getiri_6a", 0.20),
        ("getiri_1y", 0.30), ("getiri_3y", 0.25),
    ]
    top_w, top_v = 0.0, 0.0
    for k, w in agirliklar:
        v = row.get(k)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            top_v += float(v) * w
            top_w += w
    if top_w <= 0:
        return None
    return top_v / top_w


def gelismis_skor_hesapla(
    df: pd.DataFrame, agirlik: dict, min_grup: int = MIN_AKRAN_GRUP,
    grup_kolon: str = "baskin_tur", cikti_kolon: str = "skor_gelismis",
) -> pd.DataFrame:
    """
    Percentile tabanlı, akran grubuna (fon türü) göre normalize edilmiş,
    yapılandırılabilir ağırlıklı skor. Sabit "clip" eşikleri yerine, her
    bileşen taranan fon havuzu (ve mümkünse aynı fon türü) içindeki göreli
    sırasına göre 0-100 puana çevrilir; böylece piyasa rejiminden bağımsız
    çalışır ve tek bir aşırı değer skoru domine edemez.

    Bileşenler (GELISMIS_BILESEN_ACIKLAMA):
      risk_ayarli_getiri, donem_getiri, max_dusus, risk_anomali, maliyet, akran_sapma
    """
    if df is None or df.empty:
        return df
    df = df.reset_index(drop=True)
    if grup_kolon not in df.columns:
        df[grup_kolon] = "diger"
    grup = df[grup_kolon].fillna("diger").astype(str)

    sharpe = pd.to_numeric(df.get("sharpe"), errors="coerce")
    sortino = pd.to_numeric(df.get("sortino"), errors="coerce")
    risk_ayarli = pd.concat([sharpe, sortino], axis=1).mean(axis=1, skipna=True)

    donem = df.apply(_bilesik_getiri, axis=1)
    maxdd = pd.to_numeric(df.get("max_drawdown"), errors="coerce")
    risk_sk = pd.to_numeric(df.get("risk_skor"), errors="coerce")

    maliyet_var = ("giris_kom" in df.columns) or ("cikis_kom" in df.columns)
    giris = pd.to_numeric(df.get("giris_kom"), errors="coerce").fillna(0) if maliyet_var else None
    cikis = pd.to_numeric(df.get("cikis_kom"), errors="coerce").fillna(0) if maliyet_var else None
    maliyet = (giris + cikis) if maliyet_var else None

    if "getiri_z" in df.columns:
        akran_sapma = pd.to_numeric(df["getiri_z"], errors="coerce").abs()
    else:
        akran_sapma = pd.Series([np.nan] * len(df))

    df["pct_getiri"] = _grup_percentile(donem, grup, tersine=False, min_grup=min_grup).round(1)
    df["pct_risk_ayarli"] = _grup_percentile(risk_ayarli, grup, tersine=False, min_grup=min_grup).round(1)
    df["pct_maxdd"] = _grup_percentile(maxdd, grup, tersine=False, min_grup=min_grup).round(1)
    df["pct_risk_anomali"] = _grup_percentile(risk_sk, grup, tersine=True, min_grup=min_grup).round(1)
    df["pct_maliyet"] = (
        _grup_percentile(maliyet, grup, tersine=True, min_grup=min_grup).round(1)
        if maliyet_var else pd.Series([50.0] * len(df))
    )
    df["pct_akran_sapma"] = _grup_percentile(akran_sapma, grup, tersine=True, min_grup=min_grup).round(1)

    bilesenler = {
        "risk_ayarli_getiri": df["pct_risk_ayarli"],
        "donem_getiri": df["pct_getiri"],
        "max_dusus": df["pct_maxdd"],
        "risk_anomali": df["pct_risk_anomali"],
        "maliyet": df["pct_maliyet"],
        "akran_sapma": df["pct_akran_sapma"],
    }
    toplam_agirlik = sum(max(agirlik.get(k, 0), 0) for k in bilesenler) or 1.0
    skor = pd.Series(0.0, index=df.index)
    for k, seri in bilesenler.items():
        w = max(agirlik.get(k, 0), 0) / toplam_agirlik
        # eksik bileşen → nötr 50 puan (ne ödüllendirir ne cezalandırır)
        skor = skor + seri.fillna(50.0) * w
    df[cikti_kolon] = skor.round(2)

    boyutlar = grup.value_counts()
    eff = grup.where(grup.map(boyutlar) >= min_grup, "_TUM_")
    df["akran_grubu"] = eff.map(lambda x: "Genel havuz" if x == "_TUM_" else TUR_ACIKLAMA.get(x, x))
    return df


def portfolio_metrics(weights: dict, series: dict, rf: float = 0.40) -> dict:
    common, norms = None, {}
    for kod in weights:
        if kod not in series or series[kod].empty:
            continue
        s = series[kod].dropna().sort_index()
        norms[kod] = s
        common = s.index if common is None else common.intersection(s.index)
    if common is None or len(common) < 20:
        return {}
    common = common.sort_values()
    port = None
    for kod, s in norms.items():
        a = s.reindex(common).ffill()
        if not a.iloc[0] or pd.isna(a.iloc[0]):
            continue
        c = (a / a.iloc[0]) * weights[kod]
        port = c if port is None else port + c
    if port is None:
        return {}
    return full_metrics(pd.DataFrame({"tarih": port.index, "fiyat": port.values}), rf)


try:
    from scipy.optimize import minimize
    HAS_SCIPY = True
except ImportError:
    minimize = None
    HAS_SCIPY = False


def _returns_matrix(series: Dict[str, pd.Series]) -> tuple:
    common = None
    clean = {}
    for kod, s in series.items():
        if s is None or s.empty:
            continue
        s = s.dropna().sort_index()
        s = s[~s.index.duplicated(keep="last")]
        clean[kod] = s
        common = s.index if common is None else common.intersection(s.index)
    if common is None or len(common) < 30 or len(clean) < 2:
        return [], pd.DataFrame()
    common = common.sort_values()
    cols = {kod: s.reindex(common).ffill().pct_change() for kod, s in clean.items()}
    R = pd.DataFrame(cols).dropna(how="any")
    if len(R) < 20:
        return [], pd.DataFrame()
    return list(R.columns), R


def _mu_cov(R: pd.DataFrame):
    mu = R.mean().values * 252
    cov = R.cov().values * 252 + np.eye(R.shape[1]) * 1e-8
    return mu, cov


def optimize_equal(n: int) -> np.ndarray:
    return np.ones(n) / n


def optimize_min_variance(mu, cov, max_w: float = 1.0) -> np.ndarray:
    n = len(mu)
    if not HAS_SCIPY:
        try:
            w = np.linalg.pinv(cov).sum(axis=1)
            w = np.maximum(w, 0)
            if w.sum() <= 0:
                return optimize_equal(n)
            w = w / w.sum()
            w = np.minimum(w, max_w)
            return w / w.sum()
        except Exception:
            return optimize_equal(n)

    def obj(w):
        return float(w @ cov @ w)

    cons = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    bounds = [(0.0, max_w)] * n
    res = minimize(obj, optimize_equal(n), method="SLSQP", bounds=bounds, constraints=cons,
                   options={"maxiter": 500, "ftol": 1e-12})
    w = np.maximum(res.x if res.success else optimize_equal(n), 0)
    return w / w.sum()


def optimize_max_sharpe(mu, cov, rf: float = 0.40, max_w: float = 1.0) -> np.ndarray:
    n = len(mu)
    if not HAS_SCIPY:
        try:
            w = np.linalg.pinv(cov) @ (mu - rf)
            w = np.maximum(w, 0)
            if w.sum() <= 0:
                return optimize_equal(n)
            w = np.minimum(w / w.sum(), max_w)
            return w / w.sum()
        except Exception:
            return optimize_equal(n)

    def neg_sharpe(w):
        ret = float(w @ mu)
        vol = float(np.sqrt(max(w @ cov @ w, 1e-18)))
        return -(ret - rf) / vol

    cons = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    bounds = [(0.0, max_w)] * n
    res = minimize(neg_sharpe, optimize_equal(n), method="SLSQP", bounds=bounds, constraints=cons,
                   options={"maxiter": 500, "ftol": 1e-12})
    if not res.success:
        return optimize_min_variance(mu, cov, max_w=max_w)
    w = np.maximum(res.x, 0)
    return w / w.sum()


def optimize_risk_parity(cov, max_w: float = 1.0) -> np.ndarray:
    n = cov.shape[0]
    vol = np.sqrt(np.clip(np.diag(cov), 1e-12, None))
    if not HAS_SCIPY:
        w = 1.0 / vol
        w = w / w.sum()
        w = np.minimum(w, max_w)
        return w / w.sum()

    def sse(w):
        w = np.maximum(w, 1e-12)
        sig = float(np.sqrt(w @ cov @ w))
        if sig < 1e-12:
            return 1e6
        rc = w * (cov @ w) / sig
        return float(np.sum((rc - sig / n) ** 2))

    cons = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    bounds = [(0.0, max_w)] * n
    res = minimize(sse, optimize_equal(n), method="SLSQP", bounds=bounds, constraints=cons,
                   options={"maxiter": 800, "ftol": 1e-14})
    if not res.success:
        w = 1.0 / vol
        w = w / w.sum()
        return np.minimum(w, max_w) / np.minimum(w, max_w).sum()
    w = np.maximum(res.x, 0)
    return w / w.sum()


def optimize_cvar(R: pd.DataFrame, alpha: float = 0.05, max_w: float = 1.0) -> np.ndarray:
    n = R.shape[1]
    rets = R.values
    if not HAS_SCIPY:
        mu, cov = _mu_cov(R)
        return optimize_min_variance(mu, cov, max_w=max_w)

    def obj(w):
        losses = -(rets @ w)
        q = np.quantile(losses, 1 - alpha)
        tail = losses[losses >= q]
        return float(np.mean(tail)) if len(tail) else float(q)

    cons = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    bounds = [(0.0, max_w)] * n
    res = minimize(obj, optimize_equal(n), method="SLSQP", bounds=bounds, constraints=cons,
                   options={"maxiter": 600, "ftol": 1e-10})
    if not res.success:
        mu, cov = _mu_cov(R)
        return optimize_min_variance(mu, cov, max_w=max_w)
    w = np.maximum(res.x, 0)
    return w / w.sum()


def optimize_score_proportional(scores: np.ndarray, max_w: float = 1.0) -> np.ndarray:
    s = np.maximum(scores.astype(float), 0)
    if s.sum() <= 0:
        return optimize_equal(len(s))
    w = s / s.sum()
    w = np.minimum(w, max_w)
    return w / w.sum()


def run_optimizer(
    method: str,
    series: Dict[str, pd.Series],
    rf: float = 0.40,
    max_w: float = 0.40,
    scores: Dict[str, float] = None,
    alpha: float = 0.05,
) -> dict:
    kodlar, R = _returns_matrix(series)
    if len(kodlar) < 2:
        return {"error": "Yeterli ortak getiri serisi yok (en az 2 fon)."}
    mu, cov = _mu_cov(R)
    method = (method or "max_sharpe").lower()

    if method in ("equal", "esit"):
        w = optimize_equal(len(kodlar))
    elif method in ("min_var", "min_variance", "minvar"):
        w = optimize_min_variance(mu, cov, max_w=max_w)
    elif method in ("max_sharpe", "sharpe"):
        w = optimize_max_sharpe(mu, cov, rf=rf, max_w=max_w)
    elif method in ("risk_parity", "rp"):
        w = optimize_risk_parity(cov, max_w=max_w)
    elif method in ("cvar",):
        w = optimize_cvar(R, alpha=alpha, max_w=max_w)
    elif method in ("score", "skor"):
        sc = np.array([float((scores or {}).get(k, 1.0)) for k in kodlar])
        w = optimize_score_proportional(sc, max_w=max_w)
    else:
        w = optimize_max_sharpe(mu, cov, rf=rf, max_w=max_w)
        method = "max_sharpe"

    port_mu = float(w @ mu)
    port_vol = float(np.sqrt(max(w @ cov @ w, 0)))
    port_sharpe = (port_mu - rf) / port_vol if port_vol > 1e-12 else None
    sig = port_vol if port_vol > 1e-12 else 1.0
    rc = w * (cov @ w) / sig
    return {
        "method": method,
        "weights": {kodlar[i]: round(float(w[i]), 4) for i in range(len(kodlar))},
        "risk_contrib_pct": {kodlar[i]: round(float(rc[i]) / sig * 100, 2) for i in range(len(kodlar))},
        "port_mu": round(port_mu * 100, 2),
        "port_vol": round(port_vol * 100, 2),
        "port_sharpe": round(port_sharpe, 3) if port_sharpe is not None else None,
        "kodlar": kodlar,
        "scipy": HAS_SCIPY,
    }


# ─── Analiz ───
class FonAnaliz:
    def __init__(self, fon_tipi: str = "YAT", cfg: dict = None):
        self.cfg = cfg or load_config()
        self.api = TefasAPI(fon_tipi=fon_tipi)
        self.rf = float(self.cfg.get("risk_free", 0.40))
        self.bench = {}

    def tara(
        self, hedef: dict,
        min_1a=None, min_3a=None, min_6a=None, min_1y=None,
        min_sharpe=None, max_vol=None, max_dd=None,
        max_fon=15, tolerans=35.0, max_aday=40,
        kurucu_filtre: str = None, faiz_filtre: str = None,
        min_risk=None, max_risk=None, with_benchmark: bool = True,
        tur_filtre: Optional[List[str]] = None,
        skor_sistemi: str = "gelismis", skor_agirlik: dict = None,
    ) -> pd.DataFrame:
        if with_benchmark and not self.bench:
            self.bench = fetch_benchmarks(self.cfg)

        print("\n1/4 Dağılımlar...")
        dag = self.api.dagilimlar()
        if dag.empty:
            return pd.DataFrame()
        print(f"   {len(dag)} fon")

        print("2/4 Dağılım filtresi (+ tasfiye/yasaklı kurucu eleme)...")
        adaylar = []
        elenen = 0
        for _, row in dag.iterrows():
            ad = row.get("ad") or ""
            if kurucu_yasakli_mi(ad) or unvan_tasfiye_mi(ad):
                elenen += 1
                continue
            ozet = self.api.dagilim_ozet(row)
            # Eskiden 40 idi; birçok fonun dağılım verisi eksik/kısmi olduğu için
            # çok fazla fon eleniyordu. 5'e indirdik (neredeyse sadece tamamen boş olanlar elenir).
            if sum(ozet.values()) < 5:
                continue
            if kurucu_filtre and kurucu_filtre.upper() not in ad.upper():
                continue
            baskin = baskin_varlik_turu(ozet)
            if tur_filtre and baskin not in tur_filtre:
                continue
            mesafe = sum(abs(ozet.get(k, 0) - v) for k, v in hedef.items())
            # tolerans * kategori sayısı; çok yüksek tolerans = neredeyse tüm fonlar geçer
            limit = tolerans * max(len(hedef), 1)
            if mesafe <= limit:
                adaylar.append({
                    "kod": row["kod"], "ad": ad,
                    "kurucu": kurucu_from_ad(ad),
                    "mesafe_skoru": round(mesafe, 1),
                    "baskin_tur": baskin,
                    **{f"d_{k}": round(v, 1) for k, v in ozet.items()},
                })
        if elenen:
            print(f"   Tasfiye/yasaklı kurucu elenen: {elenen}")
        if not adaylar:
            print("   Aday yok.")
            return pd.DataFrame()
        aday_df = pd.DataFrame(adaylar).sort_values("mesafe_skoru")
        print(f"   {len(aday_df)} aday")

        print("3/4 TEFAS açık + tasfiye/güvenlik kontrolü...")
        acik = []
        for i, (_, row) in enumerate(aday_df.head(max_aday * 2).iterrows(), 1):
            print(f"   → {row['kod']} ({i})", end="\r")
            ad = row.get("ad") or ""
            ok, neden, p = self.api.fon_uygun_mu(row["kod"], ad)
            if not ok:
                continue
            o = self.api.ozet(row["kod"])
            faiz = faiz_durum(p, ad)
            ad_l = ad.lower()
            if faiz_filtre == "faizsiz":
                if "katılım" not in ad_l and "katilim" not in ad_l:
                    if "faiz içerir" in faiz.lower():
                        continue
            elif faiz_filtre == "faiz":
                if "katılım" in ad_l or "katilim" in ad_l:
                    continue
            try:
                risk = int(p.get("riskDegeri") or 0) or None
            except (TypeError, ValueError):
                risk = None
            if min_risk is not None and risk is not None and risk < min_risk:
                continue
            if max_risk is not None and risk is not None and risk > max_risk:
                continue
            acik.append({
                **row.to_dict(),
                "tefas_durum": p.get("tefasDurum", ""),
                "risk_degeri": p.get("riskDegeri"),
                "faiz": faiz,
                "giris_kom": p.get("girisKomisyonu"),
                "cikis_kom": p.get("cikisKomisyonu"),
                "islem_saat": f"{p.get('basIsSaat') or '?'}-{p.get('sonIsSaat') or '?'}",
                "kategori": o.get("fonKategori"),
                "kategori_sira": o.get("kategoriDerece"),
                "yatirimci": o.get("yatirimciSayi"),
                "buyukluk": o.get("portBuyukluk"),
            })
            if len(acik) >= max_aday:
                break
        print()
        if not acik:
            print("   Açık fon yok.")
            return pd.DataFrame()
        print(f"   {len(acik)} açık fon")

        print("4/4 Metrikler + anomali + benchmark...")
        sonuclar = []
        for i, row in enumerate(acik, 1):
            kod = row["kod"]
            print(f"   → {kod} ({i}/{len(acik)})", end="\r")
            fd = self.api.fiyat(kod, 12)
            m = full_metrics(fd, self.rf)
            if not m:
                continue
            if min_1a is not None and (m.get("getiri_1a") is None or m["getiri_1a"] < min_1a):
                continue
            if min_3a is not None and (m.get("getiri_3a") is None or m["getiri_3a"] < min_3a):
                continue
            if min_6a is not None and (m.get("getiri_6a") is None or m["getiri_6a"] < min_6a):
                continue
            if min_1y is not None and (m.get("getiri_1y") is None or m["getiri_1y"] < min_1y):
                continue
            if min_sharpe is not None and (m.get("sharpe") is None or m["sharpe"] < min_sharpe):
                continue
            if max_vol is not None and (m.get("volatilite") is None or m["volatilite"] > max_vol):
                continue
            if max_dd is not None and (m.get("max_drawdown") is None or m["max_drawdown"] < max_dd):
                continue
            if self.bench:
                m = attach_benchmark(m, self.bench)
            an = anomaly_from_prices(fd)
            dag_row = {k: row.get(k) for k in row.index if str(k).startswith("d_")} if hasattr(row, "index") else {}
            if not dag_row:
                dag_row = {k: row.get(k) for k in ("d_repo", "d_mevduat", "d_tahvil", "d_hisse", "d_altin", "d_doviz") if k in row}
            liq = likidite_risk(
                {"portBuyukluk": row.get("buyukluk"), "yatirimciSayi": row.get("yatirimci")},
                m,
                dagilim=dag_row,
                profil={},
                anomaly=an,
            )
            risk_skor = (an.get("anomaly_skor") or 0) + (liq.get("likidite_skor_risk") or 0)
            risk_bayrak = detay_rapor_notu(m, birlestir_risk_bayrak(
                an.get("risk_bayrak"), liq.get("likidite_bayrak"),
                f"Likidite {liq.get('likidite_seviye')} ({liq.get('likidite_skor')})",
            ))
            skor_klasik = round(
                (100 - min(row["mesafe_skoru"], 100)) * 0.3
                + max(m.get("sharpe") or 0, -2) * 15
                + min(m.get("getiri_1y") or 0, 100) * 0.4
                - risk_skor * 0.5
                + (liq.get("likidite_skor") or 0) * 0.05, 1,
            )
            sonuclar.append({
                **row, **m, **an,
                "likidite_skor": liq.get("likidite_skor"),
                "likidite_seviye": liq.get("likidite_seviye"),
                "likit_varlik_pct": liq.get("likit_varlik_pct"),
                "valor_geri_alis": liq.get("valor_geri_alis"),
                "risk_skor": round(risk_skor, 1),
                "risk_bayrak": risk_bayrak,
                "skor_klasik": skor_klasik,
            })
        print()
        if not sonuclar:
            return pd.DataFrame()
        df = pd.DataFrame(sonuclar)
        df["getiri_z"] = peer_return_zscore(df, "getiri_1y")
        # Akran dışı aşırı getiri bayrağı
        def _zflag(z):
            if z is None or (isinstance(z, float) and np.isnan(z)):
                return ""
            if z >= 2.5:
                return f"akran-dışı getiri z={z}"
            if z <= -2.5:
                return f"akran-altı getiri z={z}"
            return ""
        df["risk_bayrak"] = [
            birlestir_risk_bayrak(rb, _zflag(z))
            for rb, z in zip(df.get("risk_bayrak", []), df["getiri_z"])
        ]
        if skor_sistemi == "gelismis":
            agirlik = skor_agirlik or GELISMIS_PROFILLER["2"][1]
            df = gelismis_skor_hesapla(df, agirlik, cikti_kolon="skor_gelismis")
            df["skor"] = df["skor_gelismis"]
        else:
            df["skor"] = df["skor_klasik"]
        df = df.sort_values(["skor", "sharpe", "getiri_1y"], ascending=[False, False, False])
        df = df.head(max_fon).reset_index(drop=True)
        self._check_alarms(df)
        return df

    def _check_alarms(self, df: pd.DataFrame):
        al = self.cfg.get("alarmlar") or {}
        for _, row in df.iterrows():
            kod = row.get("kod")
            if al.get("min_getiri_1y") is not None and row.get("getiri_1y") is not None:
                if row["getiri_1y"] < al["min_getiri_1y"]:
                    log_alarm(f"{kod} 1Y %{row['getiri_1y']} < {al['min_getiri_1y']}")
            if al.get("min_sharpe") is not None and row.get("sharpe") is not None:
                if row["sharpe"] < al["min_sharpe"]:
                    log_alarm(f"{kod} Sharpe {row['sharpe']} < {al['min_sharpe']}")
            if al.get("max_drawdown") is not None and row.get("max_drawdown") is not None:
                if row["max_drawdown"] < al["max_drawdown"]:
                    log_alarm(f"{kod} MaxDD %{row['max_drawdown']} < {al['max_drawdown']}")
            if al.get("max_volatilite") is not None and row.get("volatilite") is not None:
                if row["volatilite"] > al["max_volatilite"]:
                    log_alarm(f"{kod} Vol %{row['volatilite']} > {al['max_volatilite']}")

    def fon_detay(self, kod: str) -> dict:
        if not self.bench:
            self.bench = fetch_benchmarks(self.cfg)
        fd = self.api.fiyat(kod, 12)
        m = full_metrics(fd, self.rf)
        if self.bench:
            m = attach_benchmark(m, self.bench)
        return {"kod": kod, "metrikler": m, "profil": self.api.profil(kod),
                "ozet": self.api.ozet(kod), "fiyat_df": fd, "bench": self.bench}

    def portfoy_analiz(self, weights: dict) -> dict:
        total = sum(weights.values())
        if total <= 0:
            return {}
        w = {k: v / total for k, v in weights.items()}
        series = {}
        for kod in w:
            df = self.api.fiyat(kod, 12)
            if not df.empty:
                series[kod] = df.set_index("tarih")["fiyat"]
        m = portfolio_metrics(w, series, self.rf)
        if not self.bench:
            self.bench = fetch_benchmarks(self.cfg)
        if self.bench:
            m = attach_benchmark(m, self.bench)
        dag = self.api.dagilimlar()
        birlesik = {k: 0.0 for k in list(ASSET_MAP) + ["diger"]}
        for kod, ww in w.items():
            row = dag[dag["kod"] == kod]
            if row.empty:
                continue
            for k, v in self.api.dagilim_ozet(row.iloc[0]).items():
                birlesik[k] = birlesik.get(k, 0) + v * ww
        return {"weights": w, "metrikler": m, "dagilim": {k: round(v, 1) for k, v in birlesik.items()}}

    def favorileri_guncelle(self) -> pd.DataFrame:
        favs = load_favs()
        if not favs:
            print("Favori boş.")
            return pd.DataFrame()
        if not self.bench:
            self.bench = fetch_benchmarks(self.cfg)
        rows = []
        for kod in favs:
            print(f"  → {kod}")
            d = self.fon_detay(kod)
            m, o, p = d["metrikler"], d["ozet"], d["profil"]
            rows.append({
                "kod": kod,
                "ad": (o.get("fonUnvan") or p.get("fonUnvan") or kod)[:45],
                "tefas_durum": p.get("tefasDurum"),
                "risk_degeri": p.get("riskDegeri"),
                "faiz": faiz_durum(p, o.get("fonUnvan") or ""),
                "kategori": o.get("fonKategori"),
                **m,
            })
        df = pd.DataFrame(rows)
        self._check_alarms(df)
        return df

    def tum_fon_siralama(
        self,
        sadece_pozitif_1y: bool = True,
        pozitif_3y: bool = False,
        max_liste: int = 50,
        max_tara: int = None,
        with_benchmark: bool = True,
        tur_filtre: Optional[List[str]] = None,
        skor_sistemi: str = "gelismis", skor_agirlik: dict = None,
    ) -> pd.DataFrame:
        """
        Dağılım/filtre kriteri YOK.
        Tüm (veya sınırlı) TEFAS açık fonları çeker:
          1A, 3A, 6A, 1Y, 3Y getiri + risk metrikleri
        Varsayılan: son 1 yılda her dönem (1A,3A,6A,1Y) pozitif.
        Puan = donem_puani → yüksekten düşüğe sırala.
        """
        if with_benchmark and not self.bench:
            self.bench = fetch_benchmarks(self.cfg)

        print("\n1/3 Fon listesi (dağılım tablosu)...")
        dag = self.api.dagilimlar()
        if dag.empty:
            print("Liste alınamadı.")
            return pd.DataFrame()
        # kod → satır (portföy dağılımı için)
        dag_idx = dag.drop_duplicates("kod").set_index("kod")
        kodlar = dag.drop_duplicates("kod")[["kod", "ad"]].to_dict("records")
        print(f"   {len(kodlar)} fon kodu")

        if tur_filtre:
            filtreli = []
            for item in kodlar:
                if item["kod"] not in dag_idx.index:
                    continue
                oz = self.api.dagilim_ozet(dag_idx.loc[item["kod"]])
                if baskin_varlik_turu(oz) in tur_filtre:
                    filtreli.append(item)
            kodlar = filtreli
            print(f"   Tür filtresi sonrası: {len(kodlar)} fon")

        print("2/3 TEFAS açık + tasfiye/yasaklı kurucu eleme + fiyat...")
        sonuclar = []
        tarandi = 0
        acik_say = 0
        elenen = 0
        for i, item in enumerate(kodlar, 1):
            if max_tara and tarandi >= max_tara:
                break
            kod = item["kod"]
            ad0 = item.get("ad") or ""
            print(f"   [{i}/{len(kodlar)}] {kod}  (açık:{acik_say} elenen:{elenen} kayıt:{len(sonuclar)})", end="\r")
            # Unvan/kurucu — API'den önce ele
            if kurucu_yasakli_mi(ad0) or unvan_tasfiye_mi(ad0):
                elenen += 1
                continue
            ok, neden, p = self.api.fon_uygun_mu(kod, ad0)
            if not ok:
                elenen += 1
                continue
            acik_say += 1
            tarandi += 1
            # 36 ay → 3Y getiri için
            fd = self.api.fiyat(kod, periyod=36)
            m = full_metrics(fd, self.rf)
            if not m or m.get("getiri_1y") is None:
                continue
            if sadece_pozitif_1y and not her_donem_pozitif_1y(m):
                continue
            if pozitif_3y:
                g3 = m.get("getiri_3y")
                if g3 is None or g3 <= 0:
                    continue
            if self.bench:
                m = attach_benchmark(m, self.bench)
            o = self.api.ozet(kod)
            ad = ad0 or o.get("fonUnvan") or p.get("fonUnvan") or kod
            an = anomaly_from_prices(fd)
            # Portföy dağılımı (nelere yatırım) — likidite skorundan önce
            d_fields = {}
            yatirim_ozet = ""
            baskin = "diger"
            ozet_d = {}
            if kod in dag_idx.index:
                ozet_d = self.api.dagilim_ozet(dag_idx.loc[kod])
                d_fields = {f"d_{k}": round(v, 1) for k, v in ozet_d.items()}
                baskin = baskin_varlik_turu(ozet_d)
                sirali = sorted(
                    ((k, v) for k, v in ozet_d.items() if v and v >= 1),
                    key=lambda x: -x[1],
                )
                yatirim_ozet = " | ".join(f"{k.capitalize()} %{v:.0f}" for k, v in sirali[:5])

            liq = likidite_risk(o, m, dagilim=d_fields or ozet_d, profil=p, anomaly=an)
            risk_skor = (an.get("anomaly_skor") or 0) + (liq.get("likidite_skor_risk") or 0)
            risk_bayrak = detay_rapor_notu(m, birlestir_risk_bayrak(
                an.get("risk_bayrak"), liq.get("likidite_bayrak"),
                f"Likidite {liq.get('likidite_seviye')} ({liq.get('likidite_skor')})",
            ))
            # Anomali + düşük likidite puanı düşürür
            puan_klasik = round(donem_puani(m) - risk_skor * 0.4 + (liq.get("likidite_skor") or 0) * 0.05, 2)

            sonuclar.append({
                "kod": kod,
                "ad": ad,
                "kurucu": kurucu_from_ad(ad),
                "tefas_durum": p.get("tefasDurum", ""),
                "risk_degeri": p.get("riskDegeri"),
                "likidite_skor": liq.get("likidite_skor"),
                "likidite_seviye": liq.get("likidite_seviye"),
                "likit_varlik_pct": liq.get("likit_varlik_pct"),
                "valor_geri_alis": liq.get("valor_geri_alis"),
                "kategori": o.get("fonKategori"),
                "kategori_sira": o.get("kategoriDerece"),
                "baskin_tur": baskin,
                "giris_kom": p.get("girisKomisyonu"),
                "cikis_kom": p.get("cikisKomisyonu"),
                "yatirimci": o.get("yatirimciSayi"),
                "buyukluk": o.get("portBuyukluk"),
                "yatirim": yatirim_ozet,
                "puan_klasik": puan_klasik,
                "risk_skor": round(risk_skor, 1),
                "risk_bayrak": risk_bayrak,
                **an,
                **d_fields,
                **m,
            })
        print()
        print(f"   Açık taranan: {acik_say} | Elenen (tasfiye/kapalı/yasaklı): {elenen} | Pozitif dönem geçen: {len(sonuclar)}")

        if not sonuclar:
            print("Kriterlere uyan fon yok.")
            return pd.DataFrame()

        df = pd.DataFrame(sonuclar)
        df["getiri_z"] = peer_return_zscore(df, "getiri_1y")

        def _zflag(z):
            if z is None or (isinstance(z, float) and np.isnan(z)):
                return ""
            if z >= 2.5:
                return f"akran-dışı getiri z={z}"
            if z <= -2.5:
                return f"akran-altı getiri z={z}"
            return ""

        df["risk_bayrak"] = [
            birlestir_risk_bayrak(rb, _zflag(z))
            for rb, z in zip(df["risk_bayrak"], df["getiri_z"])
        ]
        # z ekstremlerinde ek risk_skor
        df["risk_skor"] = df["risk_skor"] + df["getiri_z"].abs().fillna(0).clip(lower=2.5).sub(2.5).clip(lower=0) * 4
        df["puan_klasik"] = (df["puan_klasik"] - df["getiri_z"].abs().fillna(0).clip(lower=2.5).sub(2.5).clip(lower=0) * 3).round(2)

        if skor_sistemi == "gelismis":
            agirlik = skor_agirlik or GELISMIS_PROFILLER["2"][1]
            df = gelismis_skor_hesapla(df, agirlik, cikti_kolon="puan_gelismis")
            df["puan"] = df["puan_gelismis"]
        else:
            df["puan"] = df["puan_klasik"]

        df = (
            df.sort_values(["puan", "getiri_1y", "sharpe"], ascending=[False, False, False])
            .head(max_liste)
            .reset_index(drop=True)
        )
        if "sira" in df.columns:
            df = df.drop(columns=["sira"])
        df.insert(0, "sira", range(1, len(df) + 1))
        self._check_alarms(df)
        return df


def excel_kaydet(df: pd.DataFrame, dosya: str, meta: dict = None):
    sutun_map = {
        "kod": "Fon Kodu", "ad": "Fon Adı", "skor": "Skor", "kurucu": "Kurucu",
        "tefas_durum": "TEFAS", "risk_degeri": "Risk", "faiz": "Faiz",
        "kategori": "Kategori", "mesafe_skoru": "Dağılım Sapması", "fiyat": "Fiyat",
        "sira": "Sıra", "puan": "Puan",
        "getiri_1a": "1A %", "getiri_3a": "3A %", "getiri_6a": "6A %", "getiri_1y": "1Y %", "getiri_3y": "3Y %",
        "reel_1y": "Reel 1Y %", "vs_mevduat": "vs Mevduat", "vs_bist100": "vs BIST100", "vs_altin": "vs Altın",
        "volatilite": "Vol %", "max_drawdown": "MaxDD %", "sharpe": "Sharpe", "calmar": "Calmar",
        "d_hisse": "Hisse %", "d_tahvil": "Tahvil %", "d_repo": "Repo %",
        "d_mevduat": "Mevduat %", "d_altin": "Altın %", "d_doviz": "Döviz %",
        "islem_saat": "İşlem Saati", "yatirimci": "Yatırımcı", "buyukluk": "Büyüklük",
        "yatirim": "Portföy yatırımı (özet)",
        "d_fon": "Fon %", "d_diger": "Diğer %",
        "risk_skor": "Risk skor (anomali)",
        "likidite_skor": "Likidite skor (0-100)",
        "likidite_seviye": "Likidite seviye",
        "likit_varlik_pct": "Likit varlık %",
        "valor_geri_alis": "Geri alım valör",
        "risk_bayrak": "Risk bayrakları",
        "max_gunluk_getiri": "Max günlük %",
        "min_gunluk_getiri": "Min günlük %",
        "jump_sayisi": "Sıçrama sayısı",
        "vol_kisa": "Kısa vol %",
        "vol_oran": "Vol oran (kısa/uzun)",
        "getiri_z": "Getiri z-skor (akran)",
        "cagr": "CAGR %",
        "kazanma_orani": "Kazanma %",
        "var95": "VaR %95",
        "cvar95": "cVaR %95",
        "en_iyi_gun": "En iyi gün %",
        "en_kotu_gun": "En kötü gün %",
        "rapor_skor": "Rapor uyumlu skor",
        "sortino": "Sortino",
        "skor_klasik": "Skor (klasik)",
        "skor_gelismis": "Skor (gelişmiş)",
        "puan_klasik": "Puan (klasik)",
        "puan_gelismis": "Puan (gelişmiş)",
        "baskin_tur": "Baskın Tür",
        "akran_grubu": "Akran Grubu",
        "giris_kom": "Giriş Kom.",
        "cikis_kom": "Çıkış Kom.",
        "pct_getiri": "Percentile: Getiri",
        "pct_risk_ayarli": "Percentile: Risk-ayarlı getiri",
        "pct_maxdd": "Percentile: MaxDD",
        "pct_risk_anomali": "Percentile: Risk/Anomali",
        "pct_maliyet": "Percentile: Maliyet",
        "pct_akran_sapma": "Percentile: Akran sapması",
    }
    if df is None or df.empty:
        print("Excel: boş tablo, kayıt yok.")
        return

    mevcut = [c for c in sutun_map if c in df.columns]
    # df'de map'te olmayan ama faydalı sütunlar
    extra = [c for c in df.columns if c not in mevcut and c not in ("fiyat_df",)]
    out = df[mevcut].copy()
    for c in out.columns:
        if pd.api.types.is_numeric_dtype(out[c]):
            out[c] = out[c].replace([np.inf, -np.inf], np.nan)
        elif out[c].dtype == object:
            out[c] = out[c].astype(str).replace({"None": "", "nan": "", "NaN": "", "inf": "", "-inf": ""})
    out = out.rename(columns={c: sutun_map[c] for c in mevcut})

    ozet = [["TEFAS Fon Analiz", ""], ["Tarih", datetime.now().strftime("%d.%m.%Y %H:%M")]]
    if meta:
        for k, v in meta.items():
            ozet.append([str(k), str(v)])
    ozet.append(["Fon sayısı", str(len(df))])

    try:
        from openpyxl import Workbook
        from openpyxl.styles import (
            Font, Fill, PatternFill, Alignment, Border, Side, NamedStyle,
        )
        from openpyxl.formatting.rule import ColorScaleRule, FormulaRule, CellIsRule
        from openpyxl.utils import get_column_letter
        from openpyxl.utils.dataframe import dataframe_to_rows
        from openpyxl.chart import BarChart, Reference

        wb = Workbook()

        # ── renk paleti ──
        NAVY = "1B2838"
        BLUE = "2563EB"
        LIGHT_BLUE = "DBEAFE"
        GREEN = "059669"
        LIGHT_GREEN = "D1FAE5"
        RED = "DC2626"
        LIGHT_RED = "FEE2E2"
        AMBER = "D97706"
        LIGHT_AMBER = "FEF3C7"
        GRAY = "64748B"
        LIGHT_GRAY = "F1F5F9"
        WHITE = "FFFFFF"
        ZEBRA = "F8FAFC"

        thin = Border(
            left=Side(style="thin", color="E2E8F0"),
            right=Side(style="thin", color="E2E8F0"),
            top=Side(style="thin", color="E2E8F0"),
            bottom=Side(style="thin", color="E2E8F0"),
        )
        header_fill = PatternFill("solid", fgColor=NAVY)
        header_font = Font(name="Calibri", bold=True, color=WHITE, size=11)
        title_font = Font(name="Calibri", bold=True, color=NAVY, size=16)
        subtitle_font = Font(name="Calibri", color=GRAY, size=10)
        cell_font = Font(name="Calibri", size=10, color="1E293B")
        meta_label_font = Font(name="Calibri", bold=True, size=10, color=NAVY)
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left_al = Alignment(horizontal="left", vertical="center", wrap_text=True)

        # ═══════ Kapak ═══════
        ws0 = wb.active
        ws0.title = "Özet"
        ws0.sheet_view.showGridLines = False
        ws0.merge_cells("B2:G2")
        ws0["B2"] = "TEFAS Fon Analiz Raporu"
        ws0["B2"].font = title_font
        ws0.merge_cells("B3:G3")
        ws0["B3"] = f"Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')}  ·  {len(df)} fon"
        ws0["B3"].font = subtitle_font

        # meta kartları
        r = 5
        ws0[f"B{r}"] = "Parametre"
        ws0[f"C{r}"] = "Değer"
        for cell in (ws0[f"B{r}"], ws0[f"C{r}"]):
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = center
            cell.border = thin
        r = 6
        for row in ozet:
            ws0[f"B{r}"] = str(row[0])
            ws0[f"C{r}"] = str(row[1]) if len(row) > 1 else ""
            ws0[f"B{r}"].font = meta_label_font
            ws0[f"C{r}"].font = cell_font
            ws0[f"B{r}"].fill = PatternFill("solid", fgColor=LIGHT_GRAY)
            for col in ("B", "C"):
                ws0[f"{col}{r}"].border = thin
                ws0[f"{col}{r}"].alignment = left_al
            r += 1

        # hızlı istatistik
        r += 1
        ws0[f"B{r}"] = "Hızlı bakış"
        ws0[f"B{r}"].font = Font(name="Calibri", bold=True, size=12, color=NAVY)
        r += 1
        stats = []
        if "Puan" in out.columns:
            stats.append(("Ort. puan", f"{pd.to_numeric(out['Puan'], errors='coerce').mean():.1f}"))
        if "1Y %" in out.columns:
            stats.append(("Ort. 1Y %", f"{pd.to_numeric(out['1Y %'], errors='coerce').mean():.1f}"))
        if "Sharpe" in out.columns:
            stats.append(("Ort. Sharpe", f"{pd.to_numeric(out['Sharpe'], errors='coerce').mean():.2f}"))
        if "Likidite skor (0-100)" in out.columns:
            stats.append(("Ort. likidite", f"{pd.to_numeric(out['Likidite skor (0-100)'], errors='coerce').mean():.0f}"))
        if "Risk skor (anomali)" in out.columns:
            stats.append(("Ort. risk skor", f"{pd.to_numeric(out['Risk skor (anomali)'], errors='coerce').mean():.1f}"))
        for label, val in stats:
            ws0[f"B{r}"] = label
            ws0[f"C{r}"] = val
            ws0[f"B{r}"].font = meta_label_font
            ws0[f"C{r}"].font = Font(name="Calibri", bold=True, size=11, color=BLUE)
            ws0[f"B{r}"].fill = PatternFill("solid", fgColor=LIGHT_BLUE)
            ws0[f"C{r}"].fill = PatternFill("solid", fgColor=LIGHT_BLUE)
            for col in ("B", "C"):
                ws0[f"{col}{r}"].border = thin
            r += 1

        ws0.column_dimensions["A"].width = 3
        ws0.column_dimensions["B"].width = 28
        ws0.column_dimensions["C"].width = 36
        for col in "DEFG":
            ws0.column_dimensions[col].width = 14

        # ═══════ Fonlar ═══════
        ws = wb.create_sheet("Fonlar", 1)
        ws.sheet_view.showGridLines = False
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(out.columns))}{len(out)+1}"

        # başlık satırı
        for j, col in enumerate(out.columns, 1):
            cell = ws.cell(1, j, col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = center
            cell.border = thin
        ws.row_dimensions[1].height = 28

        # veri
        num_cols = set()
        for j, col in enumerate(out.columns, 1):
            if pd.api.types.is_numeric_dtype(df[mevcut[j - 1]] if j - 1 < len(mevcut) else object):
                num_cols.add(j)

        for i, row in enumerate(out.itertuples(index=False), 2):
            zebra = (i % 2 == 0)
            for j, val in enumerate(row, 1):
                cell = ws.cell(i, j, None if (isinstance(val, float) and (val != val)) else val)
                cell.font = cell_font
                cell.border = thin
                cell.alignment = center if j in num_cols else left_al
                if zebra:
                    cell.fill = PatternFill("solid", fgColor=ZEBRA)

                # sayı formatı
                col_name = out.columns[j - 1]
                if isinstance(val, (int, float)) and val == val:
                    if any(x in col_name for x in ("%", "Puan", "Skor", "Vol", "DD", "Sharpe", "Sortino", "Calmar", "z-skor", "CAGR", "VaR", "cVaR", "Likidite skor")):
                        cell.number_format = "0.00"
                    elif "Büyüklük" in col_name:
                        cell.number_format = "#,##0"
                    elif "Yatırımcı" in col_name:
                        cell.number_format = "#,##0"

        # sütun genişlikleri
        for j, col in enumerate(out.columns, 1):
            try:
                lengths = out[col].astype(str).map(lambda x: len(x) if isinstance(x, str) else 8)
                mx = int(lengths.max()) if len(lengths) else 8
            except Exception:
                mx = 10
            width = min(max(mx, len(str(col))) + 3, 36)
            if "bayrak" in col.lower() or "yatırım" in col.lower() or "Adı" in col:
                width = min(max(width, 28), 48)
            ws.column_dimensions[get_column_letter(j)].width = width

        # koşullu biçimlendirme yardımcı
        def _col_idx(name_part):
            for j, c in enumerate(out.columns, 1):
                if name_part.lower() in str(c).lower():
                    return j
            return None

        n_rows = len(out) + 1
        # 1Y getiri renk skalası
        for part, low, mid, high in (
            ("1Y %", "FCA5A5", "FEF08A", "86EFAC"),
            ("Sharpe", "FCA5A5", "FEF08A", "86EFAC"),
            ("Puan", "BFDBFE", "93C5FD", "1D4ED8"),
            ("Likidite skor", "FCA5A5", "FEF08A", "86EFAC"),
            ("MaxDD", "86EFAC", "FEF08A", "FCA5A5"),  # daha az negatif = yeşil
            ("Risk skor", "86EFAC", "FEF08A", "FCA5A5"),
        ):
            j = _col_idx(part)
            if j and n_rows > 2:
                letter = get_column_letter(j)
                ws.conditional_formatting.add(
                    f"{letter}2:{letter}{n_rows}",
                    ColorScaleRule(
                        start_type="min", start_color=low,
                        mid_type="percentile", mid_value=50, mid_color=mid,
                        end_type="max", end_color=high,
                    ),
                )

        # Likidite seviye metin renklendirme
        j_sev = _col_idx("Likidite seviye")
        if j_sev and n_rows > 2:
            letter = get_column_letter(j_sev)
            for text, color in (("yüksek", LIGHT_GREEN), ("orta", LIGHT_AMBER), ("düşük", LIGHT_RED)):
                ws.conditional_formatting.add(
                    f"{letter}2:{letter}{n_rows}",
                    FormulaRule(
                        formula=[f'LOWER({letter}2)="{text}"'],
                        fill=PatternFill("solid", fgColor=color),
                    ),
                )

        # ═══════ Parametreler (sade) ═══════
        ws2 = wb.create_sheet("Parametreler", 2)
        ws2["A1"] = "Alan"
        ws2["B1"] = "Değer"
        for cell in (ws2["A1"], ws2["B1"]):
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = center
            cell.border = thin
        for i, row in enumerate(ozet, 2):
            ws2[f"A{i}"] = str(row[0])
            ws2[f"B{i}"] = str(row[1]) if len(row) > 1 else ""
            ws2[f"A{i}"].font = meta_label_font
            ws2[f"B{i}"].font = cell_font
            for col in ("A", "B"):
                ws2[f"{col}{i}"].border = thin
        ws2.column_dimensions["A"].width = 28
        ws2.column_dimensions["B"].width = 40

        wb.save(dosya)
        print(f"✓ Excel: {dosya}")
    except Exception as e:
        csv_ad = dosya.rsplit(".", 1)[0] + ".csv"
        try:
            out.to_csv(csv_ad, index=False, encoding="utf-8-sig")
            print(f"Excel yazılamadı ({e}). CSV kaydedildi: {csv_ad}")
        except Exception as e2:
            print(f"Kayıt hatası: {e} / {e2}")


# ─── Menü ───
def _sayi(soru, default=None, mn=None, mx=None):
    while True:
        ip = f" [{default}]" if default is not None else " [Enter=yok]"
        s = input(f"{soru}{ip}: ").strip().replace(",", ".")
        if s == "":
            return default
        try:
            v = float(s)
        except ValueError:
            print("  Sayı gir.")
            continue
        if mn is not None and v < mn:
            print(f"  En az {mn}")
            continue
        if mx is not None and v > mx:
            print(f"  En fazla {mx}")
            continue
        return v


def _hedef() -> dict:
    print("\nProfil:")
    for k, (ad, _) in PROFILLER.items():
        print(f"  {k}) {ad}")
    sec = input("Seçim [2]: ").strip() or "2"
    if sec == "6":
        hedef = {}
        for kat, ac in KATEGORI_ACIKLAMA.items():
            v = _sayi(f"  {kat}", 0, 0, 100)
            if v and v > 0:
                hedef[kat] = v
        return hedef or PROFILLER["2"][1]
    return PROFILLER.get(sec, PROFILLER["2"])[1]


def _risk_paketi_sec() -> dict:
    print("\nRisk paketi (tek tuş eşikler):")
    for k, p in RISK_PAKETLER.items():
        print(f"  {k}) {p['ad']}: {p['aciklama']}")
    sec = input("Paket [2]: ").strip() or "2"
    return RISK_PAKETLER.get(sec, RISK_PAKETLER["2"])


def _tur_filtre_sec() -> Optional[List[str]]:
    print("\nFon türü filtresi (baskın varlık sınıfına göre):")
    turler = list(TUR_ACIKLAMA.items())
    for i, (k, ac) in enumerate(turler, 1):
        print(f"  {i}) {ac} ({k})")
    print("  0) Hepsi (filtre yok)")
    s = input("Seçim (virgülle birden fazla, örn 1,3) [0]: ").strip() or "0"
    if s == "0":
        return None
    secili = []
    for parca in s.split(","):
        parca = parca.strip()
        if not parca.isdigit():
            continue
        idx = int(parca) - 1
        if 0 <= idx < len(turler):
            secili.append(turler[idx][0])
    return secili or None


def _skor_sistemi_sec() -> str:
    print("\nSkor sistemi:")
    print("  1) Gelişmiş — percentile tabanlı, akran grubuna (fon türü) göre normalize, yapılandırılabilir ağırlık")
    print("  2) Klasik — eski sabit-ağırlıklı formül")
    s = input("Seçim [1]: ").strip() or "1"
    return "klasik" if s == "2" else "gelismis"


def _gelismis_profil_sec() -> dict:
    print("\nGelişmiş skor — risk profili (bileşen ağırlıkları):")
    for k, (ad, agirlik) in GELISMIS_PROFILLER.items():
        if agirlik:
            ozet = ", ".join(f"{GELISMIS_BILESEN_ACIKLAMA[b].split('(')[0].strip()} %{w}" for b, w in agirlik.items())
            print(f"  {k}) {ad}: {ozet}")
        else:
            print(f"  {k}) {ad}: ağırlıkları elle gir")
    sec = input("Seçim [2]: ").strip() or "2"
    if sec == "4":
        agirlik = {}
        print("  Her bileşene 0-100 arası ağırlık ver (toplam otomatik 100'e normalize edilir):")
        for b, ac in GELISMIS_BILESEN_ACIKLAMA.items():
            agirlik[b] = _sayi(f"    {ac}", 0, 0, 100) or 0
        if sum(agirlik.values()) <= 0:
            print("  Tüm ağırlıklar 0 girildi, Dengeli profile dönülüyor.")
            return GELISMIS_PROFILLER["2"][1]
        return agirlik
    return GELISMIS_PROFILLER.get(sec, GELISMIS_PROFILLER["2"])[1]


def menu_tarama(analiz: FonAnaliz):
    print("\n=== FON TARAMA ===")
    tur_filtre = _tur_filtre_sec()
    skor_sistemi = _skor_sistemi_sec()
    skor_agirlik = _gelismis_profil_sec() if skor_sistemi == "gelismis" else None
    paket = _risk_paketi_sec()
    print(f"Seçilen paket: {paket['ad']}")

    # Pakete göre varsayılan profil
    if paket.get("profil") and paket["profil"] in PROFILLER:
        hedef = PROFILLER[paket["profil"]][1]
        print("Önerilen dağılım profili uygulandı:", hedef)
        if input("Farklı profil seç? (e/h) [h]: ").strip().lower() == "e":
            hedef = _hedef()
    else:
        hedef = _hedef()
    print("Hedef:", hedef)

    # Paket eşikleri varsayılan, üzerine yazılabilir
    min_1y = _sayi("Min 1Y %")
    min_sharpe = _sayi("Min Sharpe", paket.get("min_sharpe"))
    max_vol = _sayi("Max vol %", paket.get("max_vol"))
    max_dd = _sayi("Max DD % (örn -25)", paket.get("max_dd"))
    min_risk = _sayi("Min risk 1-7", None, 1, 7)
    max_risk = _sayi("Max risk 1-7", paket.get("max_risk"), 1, 7)
    kurucu = input("Kurucu metni (örn AK PORTFÖY) [yok]: ").strip() or None
    faiz = input("Faiz: (e)hepsi (f)aizli (s)katılım [e]: ").strip().lower() or "e"
    faiz_f = {"f": "faiz", "s": "faizsiz"}.get(faiz)
    tolerans = _sayi("Tolerans", 35, 10, 80) or 35
    max_fon = int(_sayi("Kaç fon", 15, 5, 50) or 15)
    max_aday = int(_sayi("Aday", 40, 10, 80) or 40)

    df = analiz.tara(
        hedef, min_1y=min_1y, min_sharpe=min_sharpe, max_vol=max_vol, max_dd=max_dd,
        max_fon=max_fon, tolerans=tolerans, max_aday=max_aday,
        kurucu_filtre=kurucu, faiz_filtre=faiz_f,
        min_risk=int(min_risk) if min_risk else None,
        max_risk=int(max_risk) if max_risk else None,
        tur_filtre=tur_filtre, skor_sistemi=skor_sistemi, skor_agirlik=skor_agirlik,
    )
    if df.empty:
        print("Sonuç yok.")
        return
    cols = [c for c in [
        "skor", "kod", "ad", "getiri_1y", "reel_1y", "vs_mevduat", "vs_bist100",
        "sharpe", "sortino", "volatilite", "max_drawdown", "risk_degeri", "risk_skor", "risk_bayrak",
        "baskin_tur", "akran_grubu", "kurucu", "yatirim",
    ] if c in df.columns]
    pd.set_option("display.max_colwidth", 40)
    pd.set_option("display.width", 180)
    pd.set_option("display.float_format", "{:.1f}".format)
    print("\n", df[cols].to_string(index=False))
    if skor_sistemi == "gelismis":
        pct_cols = [c for c in ["kod", "pct_getiri", "pct_risk_ayarli", "pct_maxdd",
                                 "pct_risk_anomali", "pct_maliyet", "pct_akran_sapma"] if c in df.columns]
        print("\n--- Bileşen percentile'ları (0-100, akran grubu içinde) ---\n")
        print(df[pct_cols].to_string(index=False))
    if analiz.bench:
        print("\nBenchmark:", "BIST100=", analiz.bench.get("bist100_1y"),
              "TÜFE=", analiz.bench.get("tufe_yillik"), "Mevduat=", analiz.bench.get("mevduat_yillik"))

    if input("\nGrafik rapor (PNG)? (e/h) [e]: ").strip().lower() != "h":
        grafik_rapor(df, baslik=f"Tarama — {paket['ad']} | {datetime.now().strftime('%d.%m.%Y')}")

    if input("Excel? (e/h) [e]: ").strip().lower() != "h":
        excel_kaydet(df, f"tefas_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                     {"hedef": str(hedef), "risk_paketi": paket["ad"], "tip": analiz.api.fon_tipi,
                      "skor_sistemi": skor_sistemi, "tur_filtre": tur_filtre or "hepsi"})


def menu_karsilastir(analiz: FonAnaliz):
    print("\n=== KARŞILAŞTIRMA ===")
    s = input("Kodlar [AAL,AFO]: ").strip() or "AAL,AFO"
    rows = []
    for kod in [k.strip().upper() for k in s.split(",") if k.strip()]:
        d = analiz.fon_detay(kod)
        m, o, p = d["metrikler"], d["ozet"], d["profil"]
        rows.append({"kod": kod, "ad": (o.get("fonUnvan") or p.get("fonUnvan") or kod)[:40],
                     "risk": p.get("riskDegeri"), "faiz": faiz_durum(p), **m})
    df = pd.DataFrame(rows)
    cols = [c for c in ["kod", "ad", "risk", "getiri_1y", "reel_1y", "vs_mevduat", "vs_bist100",
                        "volatilite", "max_drawdown", "sharpe", "fiyat"] if c in df.columns]
    print(df[cols].to_string(index=False))


def _portfoy_ozet_yazdir(analiz: FonAnaliz, w: dict):
    s = analiz.portfoy_analiz(w)
    m = s.get("metrikler") or {}
    print("\nMetrikler:")
    for k in ["getiri_1y", "reel_1y", "vs_mevduat", "vs_bist100", "volatilite", "max_drawdown", "sharpe"]:
        print(f"  {k:14s}: {m.get(k)}")
    print("Ağırlık:", {k: f"%{v*100:.0f}" for k, v in (s.get("weights") or {}).items()})
    print("Dağılım:", {k: v for k, v in (s.get("dagilim") or {}).items() if v > 0.5})
    return s


def menu_portfoy(analiz: FonAnaliz):
    print("\n=== PORTFÖY ===")
    print("  1) Elle fon seç ve ağırlıklandır")
    print("  2) Otomatik oluştur (puanı en yüksek fonlardan)")
    sec = input("Seçim [1]: ").strip() or "1"

    if sec == "2":
        menu_portfoy_otomatik(analiz)
        return

    n = int(_sayi("Kaç fon", 3, 2, 8) or 3)
    w = {}
    for i in range(n):
        kod = input(f"Fon {i+1} kodu: ").strip().upper()
        if kod:
            w[kod] = _sayi(f"  {kod} %", 100 // n, 1, 100) or (100 // n)
    if not w:
        return
    _portfoy_ozet_yazdir(analiz, w)


def menu_portfoy_otomatik(analiz: FonAnaliz):
    """
    Elle fon seçmek yerine, tarama sonucundaki PUANI EN YÜKSEK fonlardan
    otomatik olarak bir portföy kurar. `tara()` sonucu zaten puana (skor)
    göre sıralı döndüğü için max_fon = n vererek doğrudan "en iyi n fon"
    seçilmiş olur.
    """
    print("\n=== OTOMATİK PORTFÖY (Puanı En Yüksek Fonlar) ===")

    paket = _risk_paketi_sec()
    print(f"Seçilen risk paketi: {paket['ad']}")
    if paket.get("profil") and paket["profil"] in PROFILLER:
        hedef = PROFILLER[paket["profil"]][1]
        print("Önerilen dağılım profili uygulandı:", hedef)
        if input("Farklı dağılım hedefi seç? (e/h) [h]: ").strip().lower() == "e":
            hedef = _hedef()
    else:
        hedef = _hedef()

    n = int(_sayi("Portföye kaç fon alınsın", 5, 2, 10) or 5)
    max_aday = int(_sayi("Taranacak aday sayısı", max(40, n * 6), 10, 100) or max(40, n * 6))

    print(f"\nEn yüksek puanlı {n} fon aranıyor (puana göre otomatik sıralama)...")
    df = analiz.tara(
        hedef,
        min_sharpe=paket.get("min_sharpe"),
        max_vol=paket.get("max_vol"),
        max_dd=paket.get("max_dd"),
        max_fon=n,            # tara() sonucu 'skor' azalan sırada döner → ilk n = en yüksek puanlılar
        max_aday=max_aday,
        min_risk=None,
        max_risk=paket.get("max_risk"),
    )
    if df.empty or "skor" not in df.columns:
        print("Uygun fon bulunamadı — filtreleri gevşetip tekrar deneyin.")
        return

    cols = [c for c in ["skor", "kod", "ad", "getiri_1y", "sharpe", "volatilite",
                         "max_drawdown", "risk_degeri", "risk_bayrak"] if c in df.columns]
    print(f"\n--- Seçilen {len(df)} fon (puana göre en yüksekten en düşüğe) ---\n")
    print(df[cols].to_string(index=False))

    print("\nAğırlıklandırma yöntemi:")
    print("  1) Puan-orantılı  (yüksek puanlı fona daha çok ağırlık)")
    print("  2) Eşit ağırlık")
    yontem = input("Seçim [1]: ").strip() or "1"
    max_w = _sayi("Tek fon için max ağırlık (0.50=%50)", 0.50, 0.10, 1.0) or 0.50

    kodlar = df["kod"].tolist()
    if yontem == "2":
        w = {k: 1.0 / len(kodlar) for k in kodlar}
    else:
        skorlar = np.array([float(v) if v is not None else 0.0 for v in df["skor"].tolist()])
        w_arr = optimize_score_proportional(skorlar, max_w=max_w)
        w = {kodlar[i]: float(w_arr[i]) for i in range(len(kodlar))}

    print("\n--- Otomatik oluşturulan portföy ---")
    for k, v in sorted(w.items(), key=lambda x: -x[1]):
        skoru = df.loc[df["kod"] == k, "skor"].values
        print(f"  {k}: %{v*100:.1f}  (puan={skoru[0] if len(skoru) else '-'})")

    s = _portfoy_ozet_yazdir(analiz, w)

    if input("\nGrafik rapor (PNG) — aday fonlar? (e/h) [e]: ").strip().lower() != "h":
        grafik_rapor(df, baslik=f"Otomatik Portföy — {paket['ad']} | {datetime.now().strftime('%d.%m.%Y')}")

    if input("Excel? (e/h) [e]: ").strip().lower() != "h":
        edf = df.copy()
        edf["portfoy_agirlik_%"] = edf["kod"].map(lambda k: round(w.get(k, 0) * 100, 2))
        excel_kaydet(
            edf, f"tefas_oto_portfoy_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            {"mod": "otomatik_portfoy", "risk_paketi": paket["ad"], "yontem": "puan-orantılı" if yontem != "2" else "eşit",
             "agirliklar": w, "metrikler": s.get("metrikler") or {}},
        )

    favs = load_favs()
    if input("Favorilere eklensin mi? (e/h) [h]: ").strip().lower() == "e":
        favs = list(set(favs) | set(kodlar))
        save_favs(favs)
        print(f"✓ {len(kodlar)} fon favorilere eklendi.")


def grafik_rapor(df: pd.DataFrame, baslik: str = "TEFAS Fon Analiz") -> Optional[str]:
    """
    Tarama sonucundan PNG rapor (2x3):
      1) Dönemsel getiri
      2) Risk–getiri (vol x 1Y, renk=Sharpe)
      3) Max drawdown
      4) Puan / skor sıralaması
      5) Anomali risk skoru
      6) Getiri z-skor (akran sapması)
    """
    if not HAS_MPL:
        print("  matplotlib yok — grafik için: pip install matplotlib")
        return None
    if df is None or df.empty:
        return None

    top = df.head(15).copy()
    if "kod" not in top.columns:
        return None

    # ── Görsel tema ──────────────────────────────────────────────
    try:
        plt.style.use("seaborn-v0_8-whitegrid")
    except Exception:
        try:
            plt.style.use("seaborn-whitegrid")
        except Exception:
            pass

    RENK_METIN = "#1c2833"
    RENK_IZGARA = "#d5d8dc"
    RENK_VURGU = "#2e86c1"
    RENK_IYI = "#27ae60"
    RENK_ORTA = "#f39c12"
    RENK_KOTU = "#c0392b"
    DONEM_PALET = ["#2e86c1", "#17a589", "#f39c12", "#8e44ad", "#c0392b"]

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "text.color": RENK_METIN,
        "axes.labelcolor": RENK_METIN,
        "axes.edgecolor": RENK_IZGARA,
        "xtick.color": RENK_METIN,
        "ytick.color": RENK_METIN,
    })

    def _temizle(ax):
        for kenar in ("top", "right"):
            ax.spines[kenar].set_visible(False)
        ax.spines["left"].set_color(RENK_IZGARA)
        ax.spines["bottom"].set_color(RENK_IZGARA)
        ax.tick_params(labelsize=8)

    def _deger_etiketle_h(ax, vals, fmt="{:.1f}"):
        for i, v in enumerate(vals):
            if v is None or (isinstance(v, float) and np.isnan(v)):
                continue
            ofset = (max(vals, default=1) - min(vals, default=0)) * 0.02 or 0.3
            ax.text(v + (ofset if v >= 0 else -ofset), i, fmt.format(v),
                    va="center", ha="left" if v >= 0 else "right", fontsize=7, color=RENK_METIN)

    fig, axes = plt.subplots(2, 3, figsize=(17, 9.5), facecolor="white")
    fig.suptitle(baslik, fontsize=15, fontweight="bold", color=RENK_METIN, y=0.995)
    fig.text(0.5, 0.965, f"En iyi {len(top)} fon · TEFAS verileri",
              ha="center", fontsize=9.5, color="#5d6d7e")

    # 1) Dönemsel getiriler
    ax = axes[0, 0]
    donemler = [
        ("getiri_1a", "1A"), ("getiri_3a", "3A"), ("getiri_6a", "6A"),
        ("getiri_1y", "1Y"), ("getiri_3y", "3Y"),
    ]
    x = list(range(len(top)))
    width = 0.15
    drawn = 0
    for i, (col, lab) in enumerate(donemler):
        if col not in top.columns:
            continue
        vals = pd.to_numeric(top[col], errors="coerce").fillna(0).tolist()
        ax.bar([xi + drawn * width for xi in x], vals, width, label=lab,
               color=DONEM_PALET[drawn % len(DONEM_PALET)], edgecolor="white", linewidth=0.3)
        drawn += 1
    if drawn:
        ax.set_xticks([xi + width * max(drawn - 1, 0) / 2 for xi in x])
        ax.set_xticklabels(top["kod"].tolist(), rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Getiri %", fontsize=9)
    ax.set_title("Dönemsel Getiriler", fontsize=11, fontweight="bold", pad=10)
    ax.legend(fontsize=7.5, ncol=5, loc="upper left", frameon=False)
    ax.axhline(0, color="#7f8c8d", lw=0.9)
    ax.grid(True, axis="y", alpha=0.3, color=RENK_IZGARA)
    _temizle(ax)

    # 2) Risk–getiri
    ax = axes[0, 1]
    if "volatilite" in top.columns and "getiri_1y" in top.columns:
        sub = top.dropna(subset=["volatilite", "getiri_1y"]).copy()
        if not sub.empty:
            color_col = "sharpe" if "sharpe" in sub.columns else None
            sc = ax.scatter(
                sub["volatilite"], sub["getiri_1y"],
                c=sub[color_col] if color_col else RENK_VURGU,
                s=90, cmap="RdYlGn", edgecolors="white", linewidths=0.8, alpha=0.9, zorder=3,
            )
            for _, r in sub.iterrows():
                ax.annotate(
                    str(r["kod"]),
                    (r["volatilite"], r["getiri_1y"]),
                    fontsize=7.5, fontweight="medium", xytext=(5, 5), textcoords="offset points",
                )
            if color_col:
                cb = fig.colorbar(sc, ax=ax, label="Sharpe Oranı", fraction=0.046)
                cb.ax.tick_params(labelsize=7)
    ax.set_xlabel("Volatilite %", fontsize=9)
    ax.set_ylabel("1 Yıllık Getiri %", fontsize=9)
    ax.set_title("Risk – Getiri Dağılımı", fontsize=11, fontweight="bold", pad=10)
    ax.grid(True, alpha=0.3, color=RENK_IZGARA)
    _temizle(ax)

    # 3) Max DD
    ax = axes[0, 2]
    if "max_drawdown" in top.columns:
        vals = pd.to_numeric(top["max_drawdown"], errors="coerce").fillna(0)
        colors = [RENK_KOTU if v < -15 else RENK_ORTA if v < -8 else RENK_IYI for v in vals]
        ax.barh(top["kod"].tolist(), vals.tolist(), color=colors, edgecolor="white", linewidth=0.3)
        _deger_etiketle_h(ax, vals.tolist())
        ax.set_xlabel("Maksimum Düşüş %", fontsize=9)
        ax.set_title("Maksimum Düşüş (Max Drawdown)", fontsize=11, fontweight="bold", pad=10)
        ax.axvline(0, color="#7f8c8d", lw=0.9)
        ax.grid(True, axis="x", alpha=0.3, color=RENK_IZGARA)
    else:
        ax.set_title("Maksimum Düşüş (veri yok)", fontsize=11)
        ax.axis("off")
    _temizle(ax)

    # 4) Puan / skor
    ax = axes[1, 0]
    metric = "puan" if "puan" in top.columns else ("skor" if "skor" in top.columns else "sharpe")
    metric_ad = {"puan": "Puan", "skor": "Skor", "sharpe": "Sharpe Oranı"}.get(metric, metric.capitalize())
    if metric in top.columns:
        vals = pd.to_numeric(top[metric], errors="coerce").fillna(0)
        kodlar = top["kod"].tolist()
        ax.barh(kodlar[::-1], vals.tolist()[::-1], color=RENK_VURGU, edgecolor="white", linewidth=0.3)
        _deger_etiketle_h(ax, vals.tolist()[::-1])
        ax.set_xlabel(metric_ad, fontsize=9)
        ax.set_title(f"Genel Sıralama ({metric_ad})", fontsize=11, fontweight="bold", pad=10)
        ax.grid(True, axis="x", alpha=0.3, color=RENK_IZGARA)
    else:
        ax.set_title("Puan (veri yok)", fontsize=11)
        ax.axis("off")
    _temizle(ax)

    # 5) Anomali risk skoru
    ax = axes[1, 1]
    if "risk_skor" in top.columns:
        vals = pd.to_numeric(top["risk_skor"], errors="coerce").fillna(0)
        colors = [
            RENK_KOTU if v >= 20 else RENK_ORTA if v >= 10 else "#f1c40f" if v >= 5 else RENK_IYI
            for v in vals
        ]
        ax.barh(top["kod"].tolist()[::-1], vals.tolist()[::-1], color=colors[::-1], edgecolor="white", linewidth=0.3)
        _deger_etiketle_h(ax, vals.tolist()[::-1])
        ax.set_xlabel("Risk Skoru (anomali + likidite)", fontsize=9)
        ax.set_title("Anomali Risk Skoru (yüksek = dikkat)", fontsize=11, fontweight="bold", pad=10)
        ax.grid(True, axis="x", alpha=0.3, color=RENK_IZGARA)
    else:
        ax.text(0.5, 0.5, "Risk skoru yok\n(tarama sonrası oluşur)",
                ha="center", va="center", transform=ax.transAxes, fontsize=10, color="#7f8c8d")
        ax.set_title("Anomali Risk Skoru", fontsize=11, fontweight="bold")
        ax.axis("off")
    _temizle(ax)

    # 6) Getiri z-skor
    ax = axes[1, 2]
    if "getiri_z" in top.columns:
        vals = pd.to_numeric(top["getiri_z"], errors="coerce").fillna(0)
        colors = [RENK_KOTU if abs(v) >= 2.5 else RENK_ORTA if abs(v) >= 1.5 else RENK_IYI for v in vals]
        ax.barh(top["kod"].tolist()[::-1], vals.tolist()[::-1], color=colors[::-1], edgecolor="white", linewidth=0.3)
        ax.axvline(0, color="#7f8c8d", lw=0.9)
        ax.axvline(2.5, color=RENK_KOTU, lw=0.9, ls="--", alpha=0.6)
        ax.axvline(-2.5, color=RENK_KOTU, lw=0.9, ls="--", alpha=0.6)
        ax.set_xlabel("Z-Skor (akran grubuna göre sapma)", fontsize=9)
        ax.set_title("Getiri Sapması (±2.5 dışı anormal)", fontsize=11, fontweight="bold", pad=10)
        ax.grid(True, axis="x", alpha=0.3, color=RENK_IZGARA)
    else:
        ax.text(0.5, 0.5, "Getiri z-skoru yok", ha="center", va="center",
                transform=ax.transAxes, fontsize=10, color="#7f8c8d")
        ax.set_title("Getiri Z-Skoru", fontsize=11, fontweight="bold")
        ax.axis("off")
    _temizle(ax)

    fig.text(0.99, 0.01, f"Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
              ha="right", fontsize=7.5, color="#95a5a6", style="italic")

    plt.tight_layout(rect=[0, 0.01, 1, 0.94])
    path = rapor_yolu("rapor_grafik", "png")
    fig.savefig(path, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    plt.rcdefaults()
    print(f"  ✓ Grafik rapor: {path}")
    return str(path)


# ── Quantstats HTML raporunu Türkçeleştirme ────────────────────────────
# NOT: quantstats'ın grafik BAŞLIKLARI matplotlib ile PNG olarak "resme
# gömülü" üretiliyor; bunlar piksel olduğu için metin değiştirme ile
# çevrilemiyor. Aşağıdaki sözlük yalnızca raporun gerçek HTML metnini
# (metrik tablosu satır başlıkları, bölüm başlıkları vb.) çevirir.
# Uzun ifadeler önce gelecek şekilde sıralandı (alt string çakışmasını önlemek için).
QUANTSTATS_TR = [
    ("Key Performance Metrics", "Temel Performans Göstergeleri"),
    ("Strategy Visualization", "Strateji Görselleştirmesi"),
    ("Worst 10 Drawdowns", "En Kötü 10 Düşüş Dönemi"),
    ("Drawdowns Periods", "Düşüş Dönemleri"),
    ("Underwater Plot", "Su Altı Grafiği (Düşüş)"),
    ("Return Quantiles", "Getiri Dağılım Aralıkları"),
    ("Rolling Volatility", "Hareketli Oynaklık"),
    ("Rolling Sortino", "Hareketli Sortino"),
    ("Rolling Sharpe", "Hareketli Sharpe"),
    ("Rolling Beta", "Hareketli Beta"),
    ("Monthly Returns", "Aylık Getiriler"),
    ("Cumulative Returns", "Kümülatif Getiriler"),
    ("Yearly Returns", "Yıllık Getiriler"),
    ("Daily Returns", "Günlük Getiriler"),
    ("Log Returns", "Logaritmik Getiriler"),
    ("EOY Returns", "Yıl Sonu Getirileri"),
    ("Start Period", "Başlangıç Tarihi"),
    ("End Period", "Bitiş Tarihi"),
    ("Risk-Free Rate", "Risksiz Faiz Oranı"),
    ("Time in Market", "Piyasada Kalma Süresi"),
    ("Cumulative Return", "Kümülatif Getiri"),
    ("CAGR%", "Yıllık Bileşik Getiri (CAGR)"),
    ("Prob. Sharpe Ratio", "Olasılıksal Sharpe Oranı"),
    ("Smart Sortino/√2", "Akıllı Sortino/√2"),
    ("Smart Sharpe", "Akıllı Sharpe Oranı"),
    ("Smart Sortino", "Akıllı Sortino Oranı"),
    ("Sortino/√2", "Sortino/√2"),
    ("Sortino", "Sortino Oranı"),
    ("Sharpe", "Sharpe Oranı"),
    ("Omega", "Omega Oranı"),
    ("Max Drawdown", "Maksimum Düşüş"),
    ("Longest DD Days", "En Uzun Düşüş Süresi (Gün)"),
    ("Volatility (ann.)", "Yıllıklandırılmış Oynaklık"),
    ("Calmar", "Calmar Oranı"),
    ("Skew", "Çarpıklık"),
    ("Kurtosis", "Basıklık"),
    ("Expected Daily", "Beklenen Günlük Getiri"),
    ("Expected Monthly", "Beklenen Aylık Getiri"),
    ("Expected Yearly", "Beklenen Yıllık Getiri"),
    ("Kelly Criterion", "Kelly Kriteri"),
    ("Risk of Ruin", "İflas Riski"),
    ("Daily Value-at-Risk", "Günlük Riske Maruz Değer (VaR)"),
    ("Expected Shortfall (cVaR)", "Beklenen Açık (cVaR)"),
    ("Max Consecutive Wins", "En Uzun Kazanç Serisi"),
    ("Max Consecutive Losses", "En Uzun Kayıp Serisi"),
    ("Gain/Pain Ratio", "Kazanç/Acı Oranı"),
    ("Gain/Pain (1M)", "Kazanç/Acı Oranı (1A)"),
    ("Payoff Ratio", "Getiri Oranı"),
    ("Profit Factor", "Kâr Faktörü"),
    ("Common Sense Ratio", "Sağduyu Oranı"),
    ("CPC Index", "CPC Endeksi"),
    ("Tail Ratio", "Kuyruk Oranı"),
    ("Outlier Win Ratio", "Aşırı Kazanç Oranı"),
    ("Outlier Loss Ratio", "Aşırı Kayıp Oranı"),
    ("Recovery Factor", "Toparlanma Faktörü"),
    ("Ulcer Index", "Ülser Endeksi"),
    ("Serenity Index", "Huzur Endeksi"),
    ("Avg. Drawdown Days", "Ort. Düşüş Süresi (Gün)"),
    ("Avg. Drawdown", "Ortalama Düşüş"),
    ("Avg. Up Month", "Ortalama Yükseliş Ayı"),
    ("Avg. Down Month", "Ortalama Düşüş Ayı"),
    ("Best Day", "En İyi Gün"),
    ("Worst Day", "En Kötü Gün"),
    ("Best Month", "En İyi Ay"),
    ("Worst Month", "En Kötü Ay"),
    ("Best Year", "En İyi Yıl"),
    ("Worst Year", "En Kötü Yıl"),
    ("Win Days", "Kazançlı Gün Oranı"),
    ("Win Month", "Kazançlı Ay Oranı"),
    ("Win Quarter", "Kazançlı Çeyrek Oranı"),
    ("Win Year", "Kazançlı Yıl Oranı"),
    ("All-time (ann.)", "Tüm Zamanlar (yıllık)"),
    ("Distribution", "Dağılım"),
    ("Strategy", "Strateji"),
    ("Benchmark", "Karşılaştırma Ölçütü"),
    ("Metric", "Gösterge"),
    ("MTD", "Aybaşından Bugüne"),
    ("YTD", "Yılbaşından Bugüne"),
]


def _quantstats_html_turkcelestir(html_path: Path) -> None:
    """Quantstats HTML raporundaki metin etiketlerini (mümkün olduğunca)
    Türkçeleştirir. Grafik başlıkları matplotlib tarafından resme
    gömüldüğü için (piksel), bunlar bu adımla değiştirilemez — bu bilinen
    bir kütüphane kısıtıdır."""
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception:
        return
    for en, tr in QUANTSTATS_TR:
        html = html.replace(en, tr)
    try:
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        pass


def _safe_num(x):
    """None / NaN / inf → None; aksi halde float."""
    if x is None:
        return None
    try:
        v = float(x)
        if v != v or abs(v) == float("inf"):
            return None
        return v
    except (TypeError, ValueError):
        return None


def turkce_degerlendirme(sharpe=None, sortino=None, max_dd=None, vol=None,
                          cagr=None, calmar=None, getiri_1y=None,
                          reel_1y=None, vs_mevduat=None, vs_bist100=None,
                          kazanma_orani=None, en_kotu_gun=None, bist_korelasyon=None,
                          likidite_skor=None, plain: bool = False) -> str:
    """Risk/getiri metriklerini sade Türkçe ile yorumlar.
    plain=True → Streamlit için markdown metin; False → HTML kutu.
    """
    sharpe = _safe_num(sharpe)
    sortino = _safe_num(sortino)
    max_dd = _safe_num(max_dd)
    vol = _safe_num(vol)
    cagr = _safe_num(cagr)
    calmar = _safe_num(calmar)
    getiri_1y = _safe_num(getiri_1y)
    reel_1y = _safe_num(reel_1y)
    vs_mevduat = _safe_num(vs_mevduat)
    vs_bist100 = _safe_num(vs_bist100)
    kazanma_orani = _safe_num(kazanma_orani)
    en_kotu_gun = _safe_num(en_kotu_gun)
    bist_korelasyon = _safe_num(bist_korelasyon)
    likidite_skor = _safe_num(likidite_skor)

    def seviye(val, iyi, orta, tersine=False):
        if val is None:
            return "veri yok"
        if tersine:
            if val <= iyi:
                return "iyi"
            if val <= orta:
                return "orta"
            return "zayıf"
        if val >= iyi:
            return "iyi"
        if val >= orta:
            return "orta"
        return "zayıf"

    satirlar = []  # (baslik, metin) — HTML'siz

    if sharpe is not None:
        s = seviye(sharpe, 1.0, 0.0)
        yorum = {
            "iyi": "aldığı riske göre oldukça tatmin edici bir getiri sağlamış",
            "orta": "aldığı riske kıyasla ortalama bir getiri sağlamış",
            "zayıf": "aldığı riske göre yetersiz getiri sağlamış (risksiz faizin altında kalmış olabilir)",
        }[s]
        satirlar.append((f"Sharpe Oranı ({sharpe:.2f})", f"{yorum}. 1 üzeri iyi, 0 altı zayıf sayılır."))

    if sortino is not None:
        s = seviye(sortino, 1.5, 0.0)
        yorum = {
            "iyi": "düşüş dönemlerindeki oynaklığa göre performans başarılı",
            "orta": "düşüş risklerine göre performans vasat",
            "zayıf": "düşüş kayıplarına oranla getiri yetersiz",
        }[s]
        satirlar.append((f"Sortino Oranı ({sortino:.2f})", f"{yorum}. Yalnızca olumsuz oynaklığı cezalandırır."))

    if max_dd is not None:
        if max_dd <= -20:
            yorum = "sermayenin beşte birinden fazlasını eritebilecek sert düşüşler yaşanmış; düşük risk iştahı için ağır olabilir"
        elif max_dd <= -10:
            yorum = "orta düzey düşüşler görülmüş, dalgalanmaya hazırlıklı olunmalı"
        else:
            yorum = "görece sınırlı düşüşler; değer kaybı riski düşük tarafta"
        satirlar.append((f"Maksimum Düşüş ({max_dd:.2f}%)", f"{yorum}."))

    if vol is not None:
        if vol >= 25:
            yorum = "fiyat hareketleri sert; kısa vadede değer hızlı değişebilir"
        elif vol >= 12:
            yorum = "orta düzey günlük dalgalanma"
        else:
            yorum = "fiyat hareketleri görece sakin"
        satirlar.append((f"Yıllık Oynaklık (%{vol:.2f})", f"{yorum}."))

    if calmar is not None:
        s = seviye(calmar, 1.0, 0.0)
        yorum = {
            "iyi": "getiri, en sert düşüşe kıyasla dengeli",
            "orta": "getiri / max düşüş dengesi vasat",
            "zayıf": "sert düşüşlere göre getiri zayıf",
        }[s]
        satirlar.append((f"Calmar Oranı ({calmar:.2f})", f"{yorum}."))

    if cagr is not None:
        satirlar.append((
            f"Yıllık Bileşik Getiri (CAGR %{cagr:.2f})",
            "Uzun vadeli ortalama büyüme hızı; enflasyon ve mevduatla birlikte okuyun.",
        ))

    if getiri_1y is not None:
        satirlar.append((
            f"Son 1 Yıllık Getiri (%{getiri_1y:.2f})",
            "Güncel dönem fiili performansı; risk göstergeleriyle birlikte değerlendirin.",
        ))

    if reel_1y is not None:
        if reel_1y > 0:
            yorum = f"son 1 yılda TÜFE'nin %{reel_1y:.2f} puan üzerinde — reel olarak parayı büyütmüş"
        else:
            yorum = f"son 1 yılda TÜFE'nin %{abs(reel_1y):.2f} puan altında — nominal kazanç olsa da alım gücü erimiş olabilir"
        satirlar.append((f"Reel Getiri (TÜFE'ye göre %{reel_1y:.2f})", f"{yorum}."))

    if vs_mevduat is not None:
        if vs_mevduat > 0:
            yorum = f"mevduat faizinin %{vs_mevduat:.2f} puan üzerinde"
        else:
            yorum = f"mevduat faizinin %{abs(vs_mevduat):.2f} puan altında — bu dönemde mevduat daha avantajlı olabilirdi"
        satirlar.append((f"Mevduata Göre Fark (%{vs_mevduat:.2f})", f"{yorum}."))

    if vs_bist100 is not None:
        if vs_bist100 > 0:
            yorum = f"BIST100'ün %{vs_bist100:.2f} puan üzerinde"
        else:
            yorum = f"BIST100'ün %{abs(vs_bist100):.2f} puan altında"
        satirlar.append((f"BIST100'e Göre Fark (%{vs_bist100:.2f})", f"{yorum}."))

    if kazanma_orani is not None:
        if kazanma_orani >= 55:
            yorum = "günlerin çoğu pozitif kapanmış; tutarlılık iyi tarafta"
        elif kazanma_orani >= 48:
            yorum = "kazanan / kaybeden gün dengesi ortalama"
        else:
            yorum = "negatif günler ağır basıyor; dalgalı bir seyir"
        satirlar.append((f"Kazanma Oranı (%{kazanma_orani:.1f})", f"{yorum}."))

    if en_kotu_gun is not None:
        if en_kotu_gun <= -8:
            yorum = "tek bir günde çok sert kayıp yaşanmış; kuyruk riski yüksek"
        elif en_kotu_gun <= -4:
            yorum = "en kötü günde orta şiddette kayıp var"
        else:
            yorum = "en kötü gün görece yumuşak"
        satirlar.append((f"En Kötü Gün (%{en_kotu_gun:.2f})", f"{yorum}."))

    if bist_korelasyon is not None:
        if bist_korelasyon >= 0.8:
            yorum = "BIST100 ile çok yüksek korelasyon — endeks düşünce fon da benzer hareket eder"
        elif bist_korelasyon >= 0.5:
            yorum = "endeksle belirgin ilişki var; çeşitlendirmede sınırlı katkı"
        elif bist_korelasyon >= 0.2:
            yorum = "endeksle ılımlı ilişki; kısmi çeşitlendirme sağlar"
        else:
            yorum = "endeksten görece bağımsız; portföy çeşitlendirmesine daha uygun olabilir"
        satirlar.append((f"BIST100 Korelasyonu ({bist_korelasyon:.2f})", f"{yorum}."))

    if likidite_skor is not None:
        if likidite_skor >= 70:
            yorum = "likidite profili güçlü (büyüklük / yatırımcı / likit varlık)"
        elif likidite_skor >= 45:
            yorum = "likidite orta düzeyde"
        else:
            yorum = "likidite zayıf tarafta; çıkışta zorluk riski düşünülmeli"
        satirlar.append((f"Likidite Skoru ({likidite_skor:.0f}/100)", f"{yorum}."))

    if not satirlar:
        msg = "Değerlendirme için yeterli veri hesaplanamadı. Fiyat serisi kısa veya metrikler eksik olabilir."
        if plain:
            return msg
        return f"<p>{msg}</p>"

    if plain:
        lines = ["### Sade dilde değerlendirme", ""]
        for bas, met in satirlar:
            lines.append(f"**{bas}:** {met}")
            lines.append("")
        lines.append(
            "*Bu yorum geçmiş verilere dayanır; yatırım tavsiyesi değildir.*"
        )
        return "\n".join(lines)

    items = "".join(
        f"<li style='margin:0.45em 0'><b>{bas}</b> — {met}</li>"
        for bas, met in satirlar
    )
    return (
        "<div style='font-family:Segoe UI,Arial,sans-serif;max-width:920px;"
        "margin:16px 0;padding:18px 22px;border:1px solid #cbd5e1;"
        "border-radius:12px;background:#f8fafc;color:#0f172a'>"
        "<h3 style='margin:0 0 12px;font-size:1.15rem;color:#0f172a'>"
        "Sade dilde değerlendirme</h3>"
        f"<ul style='margin:0;padding-left:1.2em;line-height:1.55'>{items}</ul>"
        "<p style='font-size:12px;color:#64748b;margin:14px 0 0'>"
        "Geçmiş verilere dayalı otomatik yorumdur; yatırım tavsiyesi değildir."
        "</p></div>"
    )


_AY_ISIMLERI = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


def _fig_base64(fig) -> str:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def _seviye_renk(val, iyi, orta, tersine=False):
    """Bir metriği iyi/orta/zayıf olarak sınıflar (kart renklendirme için)."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "norm"
    if tersine:  # düşük daha iyi (ör. volatilite, max düşüş zaten negatif verilir)
        if val <= iyi:
            return "iyi"
        if val <= orta:
            return "orta"
        return "zayif"
    if val >= iyi:
        return "iyi"
    if val >= orta:
        return "orta"
    return "zayif"


def _kart(baslik: str, deger: str, seviye: str = "norm", aciklama: str = "") -> str:
    renkler = {
        "iyi": ("#ecfdf5", "#065f46", "#10b981"),
        "orta": ("#fffbeb", "#92400e", "#f59e0b"),
        "zayif": ("#fef2f2", "#991b1b", "#ef4444"),
        "norm": ("#eff6ff", "#1e3a5f", "#3b82f6"),
    }
    bg, fg, border = renkler.get(seviye, renkler["norm"])
    alt = f"<div class='kart-not'>{aciklama}</div>" if aciklama else ""
    return (
        f"<div class='kart' style='background:{bg};border-left:5px solid {border}'>"
        f"<div class='kart-baslik'>{baslik}</div>"
        f"<div class='kart-deger' style='color:{fg}'>{deger}</div>{alt}</div>"
    )


def _fmt_tr(val, digits=2, suffix="", bos="—"):
    """Türkçe okunaklı sayı: binlik nokta, ondalık virgül."""
    if val is None:
        return bos
    try:
        v = float(val)
        if v != v or abs(v) == float("inf"):
            return bos
    except (TypeError, ValueError):
        return bos
    s = f"{v:,.{digits}f}"
    # 1,234.56 → 1.234,56
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s}{suffix}"


def _tablo_satir(label: str, deger: str, seviye: str = "norm") -> str:
    renk = {"iyi": "#065f46", "orta": "#92400e", "zayif": "#991b1b", "norm": "#0f172a"}.get(seviye, "#0f172a")
    bg = {"iyi": "#ecfdf5", "orta": "#fffbeb", "zayif": "#fef2f2", "norm": "transparent"}.get(seviye, "transparent")
    return (
        f"<tr style='background:{bg}'>"
        f"<td class='td-label'>{label}</td>"
        f"<td class='td-val' style='color:{renk}'>{deger}</td></tr>"
    )


def turkce_risk_raporu(
    fiyat_df: pd.DataFrame, kod: str, ad: str = "", rf: float = 0.40,
    benchmark_returns: Optional[pd.Series] = None, benchmark_ad: str = "BIST100",
    ekstra_metrikler: Optional[dict] = None,
    dagilim: Optional[dict] = None,
) -> Optional[str]:
    """
    Quantstats'a HİÇ ihtiyaç duymadan, sadece matplotlib ile üretilen,
    grafik başlıkları dahil UÇTAN UCA %100 TÜRKÇE, tek-dosya, self-contained
    HTML risk/getiri raporu (harici görsel/CDN/JS bağımlılığı yok — dosya
    çift tıklanıp tarayıcıda doğrudan açılabilir).

    ekstra_metrikler: {'getiri_1y','reel_1y','vs_mevduat','vs_bist100'} gibi
    zaten hesaplanmış (full_metrics/attach_benchmark) değerler verilirse,
    değerlendirme kutusuna enflasyon/mevduat/BIST100 karşılaştırması da eklenir.

    dagilim: {'hisse': 40, 'tahvil': 30, ...} portföy varlık dağılımı (%).
    Yoksa ekstra_metrikler içindeki d_hisse, d_tahvil... alanlarından okunur.

    Dönüş: yazılan dosya yolu veya None.
    """
    if not HAS_MPL:
        print("  matplotlib yok — rapor için: pip install matplotlib")
        return None
    if fiyat_df is None or fiyat_df.empty or len(fiyat_df) < 30:
        print("  Yeterli fiyat verisi yok (en az ~30 gün).")
        return None

    s = fiyat_df.set_index("tarih")["fiyat"].sort_index().astype(float)
    s = s[~s.index.duplicated(keep="last")]
    if getattr(s.index, "tz", None) is not None:
        s.index = s.index.tz_localize(None)
    rets = s.pct_change().dropna()
    if len(rets) < 20:
        print("  Getiri serisi çok kısa.")
        return None

    ek = ekstra_metrikler or {}

    # Portföy dağılımı: açık argüman veya d_* alanları
    if not dagilim:
        dagilim = {}
        for k in list(ASSET_MAP.keys()) + ["diger"]:
            v = ek.get(f"d_{k}", ek.get(k))
            v = _safe_num(v)
            if v is not None and v > 0.05:
                dagilim[k] = v
    else:
        dagilim = {k: float(v) for k, v in dagilim.items() if _safe_num(v) and float(v) > 0.05}

    # ── Metrikler ────────────────────────────────────────────────
    n_gun = len(rets)
    yil_sayisi = max(n_gun / 252.0, 0.05)
    kumulatif = (1 + rets).cumprod()
    toplam_getiri = float(kumulatif.iloc[-1] - 1) * 100
    cagr = (float(kumulatif.iloc[-1]) ** (1 / yil_sayisi) - 1) * 100 if kumulatif.iloc[-1] > 0 else None
    yillik_vol = float(rets.std() * np.sqrt(252) * 100) if rets.std() else None
    sharpe = float((rets.mean() * 252 - rf) / (rets.std() * np.sqrt(252))) if rets.std() else None
    downside = rets[rets < 0]
    downside_dev = float(downside.std() * np.sqrt(252)) if len(downside) > 5 and downside.std() else None
    sortino = float((rets.mean() * 252 - rf) / downside_dev) if downside_dev else None
    running_max = kumulatif.cummax()
    drawdown = kumulatif / running_max - 1
    max_dd = float(drawdown.min() * 100)
    calmar = float((rets.mean() * 252) / abs(max_dd / 100)) if max_dd else None
    en_iyi_gun, en_iyi_gun_t = float(rets.max() * 100), rets.idxmax()
    en_kotu_gun, en_kotu_gun_t = float(rets.min() * 100), rets.idxmin()
    kazanma_orani = float((rets > 0).mean() * 100)
    var95 = float(np.percentile(rets, 5) * 100)
    kuyruk = rets[rets <= np.percentile(rets, 5)]
    cvar95 = float(kuyruk.mean() * 100) if len(kuyruk) else None

    # Aylık / yıllık getiriler (resample yerine groupby — pandas sürüm bağımsız)
    tmp = pd.DataFrame({"r": rets})
    tmp["yil"] = tmp.index.year
    tmp["ay"] = tmp.index.month
    aylik = tmp.groupby(["yil", "ay"])["r"].apply(lambda x: (1 + x).prod() - 1).reset_index()
    aylik_pivot = aylik.pivot(index="yil", columns="ay", values="r").reindex(columns=range(1, 13)) * 100
    yillik = (tmp.groupby("yil")["r"].apply(lambda x: (1 + x).prod() - 1) * 100)
    en_iyi_ay = float(aylik["r"].max() * 100) if not aylik.empty else None
    en_kotu_ay = float(aylik["r"].min() * 100) if not aylik.empty else None

    # Benchmark hizalama (opsiyonel)
    bm_kumulatif = None
    bm_ad_guvenli = benchmark_ad or "Karşılaştırma Ölçütü"
    if benchmark_returns is not None and len(benchmark_returns) > 20:
        try:
            bm = benchmark_returns.copy()
            if getattr(bm.index, "tz", None) is not None:
                bm.index = bm.index.tz_localize(None)
            ortak = rets.index.intersection(bm.index)
            if len(ortak) > 20:
                bm_kumulatif = (1 + bm.loc[ortak]).cumprod()
                bm_kumulatif = bm_kumulatif / bm_kumulatif.iloc[0]
        except Exception:
            bm_kumulatif = None

    # ── Görsel tema (grafik_rapor ile tutarlı) ──────────────────
    try:
        plt.style.use("seaborn-v0_8-whitegrid")
    except Exception:
        try:
            plt.style.use("seaborn-whitegrid")
        except Exception:
            pass
    RENK_METIN = "#1c2833"
    RENK_IZGARA = "#d5d8dc"
    RENK_VURGU = "#2e86c1"
    RENK_IYI = "#27ae60"
    RENK_KOTU = "#c0392b"
    plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": RENK_METIN,
                          "axes.labelcolor": RENK_METIN, "axes.edgecolor": RENK_IZGARA,
                          "xtick.color": RENK_METIN, "ytick.color": RENK_METIN})

    def _temizle(ax):
        for kenar in ("top", "right"):
            ax.spines[kenar].set_visible(False)
        ax.spines["left"].set_color(RENK_IZGARA)
        ax.spines["bottom"].set_color(RENK_IZGARA)
        ax.tick_params(labelsize=8)

    grafikler = {}

    # 1) Kümülatif getiri
    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(kumulatif.index, (kumulatif - 1) * 100, color=RENK_VURGU, lw=1.8, label=f"{kod}")
    ax.fill_between(kumulatif.index, (kumulatif - 1) * 100, 0, color=RENK_VURGU, alpha=0.12)
    if bm_kumulatif is not None:
        ax.plot(bm_kumulatif.index, (bm_kumulatif - 1) * 100, color="#7f8c8d", lw=1.3, ls="--", label=bm_ad_guvenli)
    ax.axhline(0, color="#7f8c8d", lw=0.8)
    ax.set_title("Kümülatif Getiri", fontsize=12, fontweight="bold")
    ax.set_ylabel("Getiri %", fontsize=9)
    ax.legend(fontsize=8, frameon=False)
    ax.grid(True, alpha=0.3, color=RENK_IZGARA)
    _temizle(ax)
    grafikler["kumulatif"] = _fig_base64(fig)

    # 2) Düşüş (drawdown / su altı grafiği)
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.fill_between(drawdown.index, drawdown.values * 100, 0, color=RENK_KOTU, alpha=0.35)
    ax.plot(drawdown.index, drawdown.values * 100, color=RENK_KOTU, lw=1.0)
    ax.set_title("Düşüş Grafiği (Su Altı) — Zirveden Ne Kadar Uzak?", fontsize=12, fontweight="bold")
    ax.set_ylabel("Düşüş %", fontsize=9)
    ax.grid(True, alpha=0.3, color=RENK_IZGARA)
    _temizle(ax)
    grafikler["dususu"] = _fig_base64(fig)

    # 3) Aylık getiri ısı haritası
    if not aylik_pivot.empty:
        fig, ax = plt.subplots(figsize=(9, max(2.2, 0.45 * len(aylik_pivot))))
        veri = aylik_pivot.values
        vmax = np.nanmax(np.abs(veri)) if np.isfinite(veri).any() else 1
        im = ax.imshow(veri, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")
        ax.set_xticks(range(12))
        ax.set_xticklabels(_AY_ISIMLERI, fontsize=8)
        ax.set_yticks(range(len(aylik_pivot)))
        ax.set_yticklabels(aylik_pivot.index.astype(int), fontsize=8)
        for i in range(veri.shape[0]):
            for j in range(veri.shape[1]):
                v = veri[i, j]
                if np.isfinite(v):
                    ax.text(j, i, f"{v:.1f}", ha="center", va="center", fontsize=7,
                            color="white" if abs(v) > vmax * 0.55 else RENK_METIN)
        ax.set_title("Aylık Getiri Isı Haritası (%)", fontsize=12, fontweight="bold")
        fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
        grafikler["aylik"] = _fig_base64(fig)
    else:
        grafikler["aylik"] = None

    # 4) Yıllık getiriler
    if not yillik.empty:
        fig, ax = plt.subplots(figsize=(9, 3.2))
        renkler_y = [RENK_IYI if v >= 0 else RENK_KOTU for v in yillik.values]
        ax.bar(yillik.index.astype(str), yillik.values, color=renkler_y, edgecolor="white", linewidth=0.4)
        ofset = max(abs(yillik.values).max() * 0.06, 0.4)
        for i, v in enumerate(yillik.values):
            ax.text(i, v + (ofset if v >= 0 else -ofset), f"{v:.1f}", ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=8)
        ax.margins(y=0.22)
        ax.axhline(0, color="#7f8c8d", lw=0.8)
        ax.set_title("Yıllık Getiriler", fontsize=12, fontweight="bold")
        ax.set_ylabel("Getiri %", fontsize=9)
        ax.grid(True, axis="y", alpha=0.3, color=RENK_IZGARA)
        _temizle(ax)
        grafikler["yillik"] = _fig_base64(fig)
    else:
        grafikler["yillik"] = None

    # 5) Getiri dağılımı
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.hist(rets.values * 100, bins=40, color=RENK_VURGU, alpha=0.75, edgecolor="white")
    ax.axvline(var95, color=RENK_KOTU, lw=1.3, ls="--", label=f"%95 VaR ({var95:.2f}%)")
    ax.axvline(0, color="#7f8c8d", lw=0.8)
    ax.set_title("Günlük Getiri Dağılımı", fontsize=12, fontweight="bold")
    ax.set_xlabel("Günlük Getiri %", fontsize=9)
    ax.legend(fontsize=8, frameon=False)
    ax.grid(True, axis="y", alpha=0.3, color=RENK_IZGARA)
    _temizle(ax)
    grafikler["dagilim"] = _fig_base64(fig)

    # 6) Hareketli oynaklık (63 günlük ≈ 3 ay)
    pencere = min(63, max(10, n_gun // 4))
    roll_vol = rets.rolling(pencere).std() * np.sqrt(252) * 100
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.plot(roll_vol.index, roll_vol.values, color="#8e44ad", lw=1.5)
    ax.fill_between(roll_vol.index, roll_vol.values, 0, color="#8e44ad", alpha=0.12)
    ax.set_title(f"Hareketli Oynaklık ({pencere} Günlük, Yıllıklandırılmış)", fontsize=12, fontweight="bold")
    ax.set_ylabel("Oynaklık %", fontsize=9)
    ax.grid(True, alpha=0.3, color=RENK_IZGARA)
    _temizle(ax)
    grafikler["oynaklik"] = _fig_base64(fig)

    # 7) Portföy varlık dağılımı (pasta + yatay bar)
    grafikler["dagilim_portfoy"] = None
    dagilim_tablo_html = ""
    if dagilim:
        labels_tr = {
            "hisse": "Hisse", "tahvil": "Tahvil/Bono", "repo": "Repo/PP",
            "mevduat": "Mevduat", "altin": "Altın", "doviz": "Döviz",
            "fon": "Diğer fon", "diger": "Diğer",
        }
        items = sorted(
            ((labels_tr.get(k, k), float(v)) for k, v in dagilim.items() if float(v) > 0.05),
            key=lambda x: -x[1],
        )
        if items:
            labs = [x[0] for x in items]
            vals = [x[1] for x in items]
            renkler_pie = ["#1d4ed8", "#059669", "#d97706", "#7c3aed", "#dc2626",
                           "#0891b2", "#65a30d", "#64748b", "#db2777", "#ca8a04"]
            fig, axes = plt.subplots(1, 2, figsize=(10, 4.2))
            # Pasta
            axes[0].pie(
                vals, labels=labs, autopct=lambda p: f"%{p:.1f}" if p >= 3 else "",
                colors=renkler_pie[: len(vals)], startangle=90,
                textprops={"fontsize": 8},
                wedgeprops={"linewidth": 0.5, "edgecolor": "white"},
            )
            axes[0].set_title("Portföy Dağılımı", fontsize=12, fontweight="bold")
            # Yatay bar
            axes[1].barh(labs[::-1], vals[::-1], color=renkler_pie[: len(vals)][::-1],
                         edgecolor="white", linewidth=0.3)
            for i, v in enumerate(vals[::-1]):
                axes[1].text(v + 0.5, i, f"%{v:.1f}", va="center", fontsize=8)
            axes[1].set_xlabel("Pay %", fontsize=9)
            axes[1].set_title("Varlık Kırılımı", fontsize=12, fontweight="bold")
            axes[1].set_xlim(0, max(vals) * 1.18)
            for kenar in ("top", "right"):
                axes[1].spines[kenar].set_visible(False)
            axes[1].grid(True, axis="x", alpha=0.3)
            plt.tight_layout()
            grafikler["dagilim_portfoy"] = _fig_base64(fig)

            # HTML tablo
            sat = []
            toplam = sum(vals) or 1
            for lab, v in items:
                sat.append(
                    f"<tr><td class='td-label'>{lab}</td>"
                    f"<td class='td-val'>{_fmt_tr(v, 1, ' %')}</td>"
                    f"<td style='width:40%;padding:8px 12px'>"
                    f"<div style='background:#e2e8f0;border-radius:6px;height:14px'>"
                    f"<div style='background:#1d4ed8;width:{min(100, v/toplam*100):.0f}%;"
                    f"height:14px;border-radius:6px'></div></div></td></tr>"
                )
            dagilim_tablo_html = (
                "<table class='metrik-tablo'><thead><tr>"
                "<th>Varlık sınıfı</th><th>Pay</th><th>Görsel</th>"
                "</tr></thead><tbody>"
                + "".join(sat) +
                "</tbody></table>"
            )

    plt.rcdefaults()

    # ── Metrik kartları (büyük, okunaklı) ─────────────────────────
    kartlar = [
        _kart("Toplam Getiri", _fmt_tr(toplam_getiri, 2, "%"), _seviye_renk(toplam_getiri, 15, 0)),
        _kart("Yıllık Bileşik (CAGR)", _fmt_tr(cagr, 2, "%"), _seviye_renk(cagr, 15, 0)),
        _kart("Sharpe Oranı", _fmt_tr(sharpe, 2), _seviye_renk(sharpe, 1.0, 0)),
        _kart("Sortino Oranı", _fmt_tr(sortino, 2), _seviye_renk(sortino, 1.5, 0)),
        _kart("Calmar Oranı", _fmt_tr(calmar, 2), _seviye_renk(calmar, 1.0, 0)),
        _kart("Maksimum Düşüş", _fmt_tr(max_dd, 2, "%"), _seviye_renk(max_dd, -10, -20, tersine=True)),
        _kart("Yıllık Oynaklık", _fmt_tr(yillik_vol, 2, "%"), _seviye_renk(yillik_vol, 12, 25, tersine=True)),
        _kart("Kazanma Oranı", _fmt_tr(kazanma_orani, 1, "%"), _seviye_renk(kazanma_orani, 55, 48)),
        _kart("En İyi Gün", _fmt_tr(en_iyi_gun, 2, "%"), "iyi", en_iyi_gun_t.strftime("%d.%m.%Y")),
        _kart("En Kötü Gün", _fmt_tr(en_kotu_gun, 2, "%"), "zayif", en_kotu_gun_t.strftime("%d.%m.%Y")),
        _kart("En İyi Ay", _fmt_tr(en_iyi_ay, 2, "%"), "iyi"),
        _kart("En Kötü Ay", _fmt_tr(en_kotu_ay, 2, "%"), "zayif"),
        _kart("%95 VaR", _fmt_tr(var95, 2, "%"), "zayif" if var95 < -3 else "orta",
              "Normal günde kayıp üst sınırı"),
        _kart("%95 cVaR", _fmt_tr(cvar95, 2, "%"), "zayif", "En kötü %5 gün ortalaması"),
    ]
    # Benchmark kartları (varsa)
    if ek.get("getiri_1y") is not None:
        kartlar.append(_kart("Son 1Y Getiri", _fmt_tr(ek.get("getiri_1y"), 2, "%"),
                             _seviye_renk(ek.get("getiri_1y"), 20, 0)))
    if ek.get("reel_1y") is not None:
        kartlar.append(_kart("Reel 1Y (TÜFE)", _fmt_tr(ek.get("reel_1y"), 2, "%"),
                             _seviye_renk(ek.get("reel_1y"), 0, -5)))
    if ek.get("vs_mevduat") is not None:
        kartlar.append(_kart("vs Mevduat", _fmt_tr(ek.get("vs_mevduat"), 2, "%"),
                             _seviye_renk(ek.get("vs_mevduat"), 0, -5)))
    if ek.get("vs_bist100") is not None:
        kartlar.append(_kart("vs BIST100", _fmt_tr(ek.get("vs_bist100"), 2, "%"),
                             _seviye_renk(ek.get("vs_bist100"), 0, -5)))

    # ── Okunaklı metrik tablosu ───────────────────────────────────
    tablo_satirlari = [
        _tablo_satir("Toplam Getiri", _fmt_tr(toplam_getiri, 2, " %"), _seviye_renk(toplam_getiri, 15, 0)),
        _tablo_satir("Yıllık Bileşik Getiri (CAGR)", _fmt_tr(cagr, 2, " %"), _seviye_renk(cagr, 15, 0)),
        _tablo_satir("Sharpe Oranı", _fmt_tr(sharpe, 2), _seviye_renk(sharpe, 1.0, 0)),
        _tablo_satir("Sortino Oranı", _fmt_tr(sortino, 2), _seviye_renk(sortino, 1.5, 0)),
        _tablo_satir("Calmar Oranı", _fmt_tr(calmar, 2), _seviye_renk(calmar, 1.0, 0)),
        _tablo_satir("Maksimum Düşüş", _fmt_tr(max_dd, 2, " %"), _seviye_renk(max_dd, -10, -20, tersine=True)),
        _tablo_satir("Yıllıklandırılmış Oynaklık", _fmt_tr(yillik_vol, 2, " %"),
                     _seviye_renk(yillik_vol, 12, 25, tersine=True)),
        _tablo_satir("Kazanma Oranı (günlük)", _fmt_tr(kazanma_orani, 1, " %"), _seviye_renk(kazanma_orani, 55, 48)),
        _tablo_satir("En İyi Gün", f"{_fmt_tr(en_iyi_gun, 2, ' %')}  ({en_iyi_gun_t.strftime('%d.%m.%Y')})", "iyi"),
        _tablo_satir("En Kötü Gün", f"{_fmt_tr(en_kotu_gun, 2, ' %')}  ({en_kotu_gun_t.strftime('%d.%m.%Y')})", "zayif"),
        _tablo_satir("En İyi Ay", _fmt_tr(en_iyi_ay, 2, " %"), "iyi"),
        _tablo_satir("En Kötü Ay", _fmt_tr(en_kotu_ay, 2, " %"), "zayif"),
        _tablo_satir("%95 VaR (günlük)", _fmt_tr(var95, 2, " %"), "zayif" if var95 < -3 else "orta"),
        _tablo_satir("%95 cVaR (beklenen açık)", _fmt_tr(cvar95, 2, " %"), "zayif"),
    ]
    if ek.get("getiri_1y") is not None:
        tablo_satirlari.append(_tablo_satir("Son 1 Yıllık Getiri", _fmt_tr(ek.get("getiri_1y"), 2, " %"),
                                            _seviye_renk(ek.get("getiri_1y"), 20, 0)))
    if ek.get("getiri_6a") is not None:
        tablo_satirlari.append(_tablo_satir("Son 6 Aylık Getiri", _fmt_tr(ek.get("getiri_6a"), 2, " %")))
    if ek.get("getiri_3a") is not None:
        tablo_satirlari.append(_tablo_satir("Son 3 Aylık Getiri", _fmt_tr(ek.get("getiri_3a"), 2, " %")))
    if ek.get("getiri_1a") is not None:
        tablo_satirlari.append(_tablo_satir("Son 1 Aylık Getiri", _fmt_tr(ek.get("getiri_1a"), 2, " %")))
    if ek.get("reel_1y") is not None:
        tablo_satirlari.append(_tablo_satir("Reel Getiri (TÜFE'ye göre)", _fmt_tr(ek.get("reel_1y"), 2, " %"),
                                            _seviye_renk(ek.get("reel_1y"), 0, -5)))
    if ek.get("vs_mevduat") is not None:
        tablo_satirlari.append(_tablo_satir("Mevduata Göre Fark", _fmt_tr(ek.get("vs_mevduat"), 2, " %"),
                                            _seviye_renk(ek.get("vs_mevduat"), 0, -5)))
    if ek.get("vs_bist100") is not None:
        tablo_satirlari.append(_tablo_satir("BIST100'e Göre Fark", _fmt_tr(ek.get("vs_bist100"), 2, " %"),
                                            _seviye_renk(ek.get("vs_bist100"), 0, -5)))
    metrik_tablo_html = (
        "<table class='metrik-tablo'><thead><tr>"
        "<th style='width:55%'>Gösterge</th><th style='width:45%'>Değer</th>"
        "</tr></thead><tbody>"
        + "".join(tablo_satirlari) +
        "</tbody></table>"
    )

    # Yıllık getiri tablosu
    yillik_tablo = ""
    if not yillik.empty:
        ysatir = []
        for yil, val in yillik.items():
            sev = "iyi" if val >= 0 else "zayif"
            ysatir.append(_tablo_satir(str(int(yil)), _fmt_tr(val, 2, " %"), sev))
        yillik_tablo = (
            "<table class='metrik-tablo'><thead><tr><th>Yıl</th><th>Getiri</th></tr></thead>"
            f"<tbody>{''.join(ysatir)}</tbody></table>"
        )

    # Aylık getiri HTML tablosu (ısı haritasına ek okunaklı tablo)
    aylik_tablo_html = ""
    if not aylik_pivot.empty:
        baslik_ay = "".join(f"<th>{a}</th>" for a in _AY_ISIMLERI)
        satirlar_ay = []
        for yil, row in aylik_pivot.iterrows():
            hucreler = []
            for ay in range(1, 13):
                v = row.get(ay)
                if v is None or (isinstance(v, float) and (v != v)):
                    hucreler.append("<td class='bos'>—</td>")
                else:
                    if v >= 3:
                        cls = "pos-strong"
                    elif v >= 0:
                        cls = "pos"
                    elif v > -3:
                        cls = "neg"
                    else:
                        cls = "neg-strong"
                    hucreler.append(f"<td class='{cls}'>{_fmt_tr(v, 1)}</td>")
            satirlar_ay.append(f"<tr><td class='td-label'>{int(yil)}</td>{''.join(hucreler)}</tr>")
        aylik_tablo_html = (
            "<div class='tablo-kaydir'><table class='aylik-tablo'><thead>"
            f"<tr><th>Yıl</th>{baslik_ay}</tr></thead>"
            f"<tbody>{''.join(satirlar_ay)}</tbody></table></div>"
        )

    deger_html = turkce_degerlendirme(
        sharpe=sharpe, sortino=sortino, max_dd=max_dd, vol=yillik_vol,
        cagr=cagr, calmar=calmar, getiri_1y=ek.get("getiri_1y"),
        reel_1y=ek.get("reel_1y"), vs_mevduat=ek.get("vs_mevduat"),
        vs_bist100=ek.get("vs_bist100"),
    )

    def _grafik_html(key, alt):
        b64 = grafikler.get(key)
        if not b64:
            return ""
        return f"<div class='grafik-kutu'><img src='data:image/png;base64,{b64}' alt='{alt}'></div>"

    safe = re.sub(r"[^\w\-]+", "_", kod)
    out = rapor_yolu(f"turkce_rapor_{safe}", "html")
    baslik_metin = f"{kod} — {ad}" if ad else kod
    donem_metin = f"{s.index[0].strftime('%d.%m.%Y')} – {s.index[-1].strftime('%d.%m.%Y')} ({n_gun} işlem günü)"

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{baslik_metin} — Risk/Getiri Raporu</title>
<style>
  :root {{
    --metin: #0f172a;
    --altmetin: #475569;
    --cizgi: #e2e8f0;
    --beyaz: #ffffff;
    --zemin: #f1f5f9;
    --vurgu: #1d4ed8;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: "Segoe UI", system-ui, -apple-system, Arial, sans-serif;
    background: var(--zemin); color: var(--metin);
    margin: 0; padding: 20px 14px 48px;
    font-size: 15px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .kapsayici {{ max-width: 1040px; margin: 0 auto; }}
  header {{
    background: linear-gradient(135deg, #0f2744, #1d4ed8);
    color: white; border-radius: 16px; padding: 28px 32px; margin-bottom: 24px;
  }}
  header h1 {{ margin: 0 0 8px; font-size: 1.55rem; font-weight: 700; letter-spacing: -0.02em; }}
  header p {{ margin: 0; opacity: 0.92; font-size: 0.95rem; }}
  h2 {{
    font-size: 1.15rem; font-weight: 700; color: var(--metin);
    border-left: 5px solid var(--vurgu); padding-left: 12px;
    margin: 32px 0 16px;
  }}
  /* Kartlar */
  .kart-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 14px;
  }}
  .kart {{
    border-radius: 12px; padding: 16px 18px;
    box-shadow: 0 1px 2px rgba(15,23,42,.06);
  }}
  .kart-baslik {{
    font-size: 0.8rem; font-weight: 600; color: var(--altmetin);
    margin-bottom: 6px; letter-spacing: 0.01em;
  }}
  .kart-deger {{
    font-size: 1.55rem; font-weight: 800; font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em; line-height: 1.2;
  }}
  .kart-not {{ font-size: 0.78rem; color: var(--altmetin); margin-top: 6px; }}
  /* Tablolar — yüksek kontrast */
  .metrik-tablo, .aylik-tablo {{
    width: 100%; border-collapse: collapse;
    background: var(--beyaz); border-radius: 12px; overflow: hidden;
    box-shadow: 0 1px 3px rgba(15,23,42,.08);
    font-variant-numeric: tabular-nums;
  }}
  .metrik-tablo th, .aylik-tablo th {{
    background: #0f2744; color: #fff;
    font-size: 0.88rem; font-weight: 650;
    padding: 12px 16px; text-align: left;
  }}
  .metrik-tablo td, .aylik-tablo td {{
    padding: 11px 16px; border-bottom: 1px solid var(--cizgi);
    font-size: 0.98rem;
  }}
  .metrik-tablo tr:last-child td {{ border-bottom: none; }}
  .metrik-tablo tr:nth-child(even) {{ background: #f8fafc; }}
  .td-label {{ font-weight: 600; color: #334155; }}
  .td-val {{
    font-weight: 750; font-size: 1.08rem; text-align: right;
    font-variant-numeric: tabular-nums;
  }}
  .tablo-kaydir {{ overflow-x: auto; -webkit-overflow-scrolling: touch; margin-bottom: 8px; }}
  .aylik-tablo {{ min-width: 720px; }}
  .aylik-tablo th, .aylik-tablo td {{ text-align: center; padding: 8px 6px; font-size: 0.88rem; }}
  .aylik-tablo .td-label {{ text-align: left; font-weight: 700; }}
  .aylik-tablo .pos-strong {{ background: #059669; color: #fff; font-weight: 700; }}
  .aylik-tablo .pos {{ background: #d1fae5; color: #065f46; font-weight: 650; }}
  .aylik-tablo .neg {{ background: #fee2e2; color: #991b1b; font-weight: 650; }}
  .aylik-tablo .neg-strong {{ background: #dc2626; color: #fff; font-weight: 700; }}
  .aylik-tablo .bos {{ color: #94a3b8; }}
  .grafik-kutu {{
    background: var(--beyaz); border-radius: 12px; padding: 16px; margin-bottom: 16px;
    box-shadow: 0 1px 3px rgba(15,23,42,.08);
  }}
  .grafik-kutu img {{ width: 100%; height: auto; display: block; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
  @media (max-width: 820px) {{
    .grid-2 {{ grid-template-columns: 1fr; }}
    .kart-deger {{ font-size: 1.35rem; }}
  }}
  footer {{
    text-align: center; font-size: 0.82rem; color: var(--altmetin);
    margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--cizgi);
  }}
  .bolum-kutu {{
    background: var(--beyaz); border-radius: 12px; padding: 8px 0 4px;
    box-shadow: 0 1px 3px rgba(15,23,42,.06); margin-bottom: 8px;
  }}
</style>
</head>
<body>
<div class="kapsayici">
  <header>
    <h1>{baslik_metin}</h1>
    <p>Risk / Getiri Raporu &nbsp;·&nbsp; Dönem: {donem_metin} &nbsp;·&nbsp; Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
  </header>

  {deger_html}

  <h2>📊 Özet Kartlar</h2>
  <div class="kart-grid">{''.join(kartlar)}</div>

  <h2>📋 Metrik Tablosu</h2>
  <div class="bolum-kutu">{metrik_tablo_html}</div>

  <h2>📈 Kümülatif Getiri</h2>
  {_grafik_html("kumulatif", "Kümülatif getiri grafiği")}

  <h2>📉 Düşüş (Drawdown)</h2>
  {_grafik_html("dususu", "Düşüş grafiği")}

  <h2>📅 Aylık Getiri Tablosu (%)</h2>
  {aylik_tablo_html if aylik_tablo_html else "<p style='color:#64748b'>Aylık veri yetersiz.</p>"}

  <div class="grid-2">
    <div>
      <h2>🌡️ Aylık Isı Haritası</h2>
      {_grafik_html("aylik", "Aylık getiri ısı haritası")}
    </div>
    <div>
      <h2>📆 Yıllık Getiriler</h2>
      {_grafik_html("yillik", "Yıllık getiriler")}
      {f"<div style='margin-top:12px'>{yillik_tablo}</div>" if yillik_tablo else ""}
    </div>
  </div>

  <div class="grid-2">
    <div>
      <h2>📐 Getiri Dağılımı</h2>
      {_grafik_html("dagilim", "Getiri dağılımı histogramı")}
    </div>
    <div>
      <h2>🌊 Hareketli Oynaklık</h2>
      {_grafik_html("oynaklik", "Hareketli oynaklık grafiği")}
    </div>
  </div>

  <h2>🥧 Portföy Varlık Dağılımı</h2>
  {f'''
  <p style="color:#475569;font-size:0.92rem;margin:0 0 12px">
    Fonun TEFAS’taki güncel portföy kırılımı (hisse, tahvil, repo, altın vb.).
  </p>
  {_grafik_html("dagilim_portfoy", "Portföy varlık dağılımı")}
  <div class="bolum-kutu" style="margin-top:12px">{dagilim_tablo_html}</div>
  ''' if dagilim_tablo_html else '<p style="color:#64748b">Bu fon için portföy dağılım verisi bulunamadı.</p>'}

  <footer>
    Bu rapor geçmiş verilere dayanılarak otomatik üretilmiştir; <strong>yatırım tavsiyesi değildir</strong>.
    Geçmiş performans gelecekteki sonuçların garantisi değildir. TEFAS Fon Analiz — Türkçe Rapor Modülü.
  </footer>
</div>
</body>
</html>"""

    try:
        out.write_text(html, encoding="utf-8")
    except Exception as e:
        print(f"  Rapor yazılamadı: {e}")
        return None

    print(f"  ✓ %100 Türkçe HTML rapor (quantstats gerekmez): {out}")
    return str(out)


def quantstats_rapor(fiyat_df: pd.DataFrame, kod: str, ad: str = "", benchmark_returns=None,
                      ekstra_metrikler: Optional[dict] = None) -> Optional[str]:
    """
    Fiyat DataFrame'inden quantstats HTML raporu üretir.
    ekstra_metrikler: {'reel_1y':.., 'vs_mevduat':.., 'vs_bist100':.., 'getiri_1y':..}
    varsa (menu_detay'daki full_metrics/attach_benchmark çıktısından) Türkçe
    değerlendirme kutusuna enflasyon/mevduat/BIST100 karşılaştırması da eklenir.
    Dönüş: yazılan dosya yolu veya None.
    """
    ek = ekstra_metrikler or {}
    if not HAS_QUANTSTATS:
        print("  quantstats yüklü değil. Kur: pip install quantstats")
        return None
    if fiyat_df is None or fiyat_df.empty or len(fiyat_df) < 30:
        print("  Yeterli fiyat verisi yok (en az ~30 gün).")
        return None

    s = fiyat_df.set_index("tarih")["fiyat"].sort_index().astype(float)
    s = s[~s.index.duplicated(keep="last")]
    rets = s.pct_change().dropna()
    if len(rets) < 20:
        print("  Getiri serisi çok kısa.")
        return None

    # quantstats timezone-naive index bekleyebilir
    if getattr(rets.index, "tz", None) is not None:
        rets.index = rets.index.tz_localize(None)

    safe = re.sub(r"[^\w\-]+", "_", kod)
    out = rapor_yolu(f"qs_rapor_{safe}", "html")
    title = f"{kod} — {ad}" if ad else kod

    sharpe_v = sortino_v = max_dd_v = vol_v = cagr_v = calmar_v = None
    try:
        sharpe_v = float(qs.stats.sharpe(rets))
        sortino_v = float(qs.stats.sortino(rets))
        max_dd_v = float(qs.stats.max_drawdown(rets) * 100)
        vol_v = float(qs.stats.volatility(rets) * 100)
    except Exception:
        pass
    try:
        cagr_v = float(qs.stats.cagr(rets) * 100)
    except Exception:
        pass
    try:
        calmar_v = float(qs.stats.calmar(rets))
    except Exception:
        pass

    try:
        # Konsol özeti (Türkçe etiketler)
        print("\n  --- Risk/Getiri Özeti ---")
        if sharpe_v is not None:
            print(f"  Sharpe Oranı          : {sharpe_v:.3f}")
        if sortino_v is not None:
            print(f"  Sortino Oranı         : {sortino_v:.3f}")
        if max_dd_v is not None:
            print(f"  Maksimum Düşüş        : %{max_dd_v:.2f}")
        if vol_v is not None:
            print(f"  Yıllıklandırılmış Vol.: %{vol_v:.2f}")
        if cagr_v is not None:
            print(f"  Yıllık Bileşik Getiri : %{cagr_v:.2f}")
        if calmar_v is not None:
            print(f"  Calmar Oranı          : {calmar_v:.3f}")

        # Sade Türkçe değerlendirme metni (konsola da bas)
        print("\n  --- Sade Dilde Değerlendirme ---")
        deger_html = turkce_degerlendirme(
            sharpe=sharpe_v, sortino=sortino_v, max_dd=max_dd_v,
            vol=vol_v, cagr=cagr_v, calmar=calmar_v,
            getiri_1y=ek.get("getiri_1y"), reel_1y=ek.get("reel_1y"),
            vs_mevduat=ek.get("vs_mevduat"), vs_bist100=ek.get("vs_bist100"),
        )
        deger_metin = re.sub(r"<[^>]+>", "", deger_html).replace("&nbsp;", " ")
        for satir in [s.strip() for s in deger_metin.split("•") if s.strip()]:
            print(f"  • {satir}")

        # HTML rapor (quantstats kendi grafiklerini üretir — İngilizce PNG
        # başlıkları kütüphane kısıtı nedeniyle çevrilemez, bkz. not üstte)
        kwargs = {
            "returns": rets,
            "output": str(out),
            "title": title,
            "download_filename": out.name,
        }
        if benchmark_returns is not None and len(benchmark_returns) > 20:
            kwargs["benchmark"] = benchmark_returns
            kwargs["benchmark_title"] = "Karşılaştırma Ölçütü"
        qs.reports.html(**kwargs)

        # Metin etiketlerini Türkçeleştir + üste Türkçe değerlendirme kutusu ekle
        _quantstats_html_turkcelestir(out)
        try:
            html = out.read_text(encoding="utf-8")
            idx = html.lower().find("<body")
            if idx != -1:
                idx = html.find(">", idx) + 1
                html = html[:idx] + deger_html + html[idx:]
                out.write_text(html, encoding="utf-8")
        except Exception:
            pass

        print(f"  ✓ HTML rapor (Türkçe etiketli + değerlendirme eklendi): {out}")
        print("    Not: quantstats grafik başlıkları (Sharpe eğrisi, aylık getiri ısı haritası vb.)")
        print("    matplotlib tarafından resme gömüldüğü için İngilizce kalır; sayısal veriler ve")
        print("    yukarıdaki değerlendirme tamamen Türkçedir.")
        return str(out)
    except Exception as e:
        print(f"  Quantstats rapor hatası: {e}")
        # Yine de metrikleri yazmayı dene
        try:
            print(f"  Sharpe Oranı={sharpe_v:.3f}  Maksimum Düşüş=%{max_dd_v:.2f}")
        except Exception:
            pass
        return None


def portfoy_html_raporu(
    baslik: str,
    metrikler: Optional[dict] = None,
    tablo: Optional[pd.DataFrame] = None,
    diversify_score: Optional[float] = None,
    corr_df: Optional[pd.DataFrame] = None,
    oneriler: Optional[List[dict]] = None,
    ozet_md: str = "",
    fiyat_df: Optional[pd.DataFrame] = None,
    quantstats: bool = True,
    ekstra: Optional[dict] = None,
) -> Optional[str]:
    """Portföy / sepet için Türkçe HTML rapor (+ isteğe bağlı quantstats).

    Dönüş: yazılan dosya yolu.
    """
    safe = re.sub(r"[^\w\-]+", "_", (baslik or "portfoy")[:40])
    out = rapor_yolu(f"portfoy_rapor_{safe}", "html")
    m = metrikler or {}
    ekstra = ekstra or {}

    def _f(v, d=1):
        try:
            if v is None or (isinstance(v, float) and not np.isfinite(v)):
                return "—"
            return f"{float(v):.{d}f}"
        except Exception:
            return "—"

    div_yorum = ""
    if diversify_score is not None:
        if diversify_score >= 55:
            div_yorum = "Fonlar görece bağımsız hareket ediyor; çeşitlendirme iyi tarafta."
        elif diversify_score >= 35:
            div_yorum = "Orta düzeyde birlikte hareket; çeşitlendirme geliştirilebilir."
        else:
            div_yorum = "Yüksek korelasyon — çeşitlendirme zayıf; düşük korelasyonlu fon eklemeyi düşünün."

    oneri_html = ""
    if oneriler:
        rows = []
        for o in oneriler[:12]:
            rows.append(
                f"<tr><td><b>{o.get('kod','')}</b></td><td>{(o.get('ad') or '')[:45]}</td>"
                f"<td>{o.get('tehlike_seviyesi') or '—'}</td>"
                f"<td>{_f(o.get('getiri_1y'), 1)}%</td>"
                f"<td>{_f(o.get('getiri_3a'), 1)}%</td>"
                f"<td>{_f(o.get('ortalama_korelasyon'), 2)}</td>"
                f"<td>{_f(o.get('beklenen_cesitlilik'), 0)}</td>"
                f"<td>{o.get('neden') or ''}</td></tr>"
            )
        oneri_html = f"""
        <h2>Çeşitlendirme önerileri (yalnız TEFAS açık)</h2>
        <p>Sepete eklendiğinde ortalama korelasyonu düşürmesi beklenen adaylar:</p>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px">
        <tr style="background:#1a365d;color:#fff">
          <th>Kod</th><th>Ad</th><th>Tehlike</th><th>Getiri 1Y</th><th>Getiri 3A</th>
          <th>Ort. ρ</th><th>Beklenen çeşitl.</th><th>Neden</th>
        </tr>
        {''.join(rows)}
        </table>
        """

    tablo_html = ""
    if tablo is not None and not tablo.empty:
        cols = [c for c in tablo.columns if c in (
            "kod", "ad", "rol", "getiri_1y", "sharpe", "max_drawdown", "volatilite",
            "kategori", "agirlik_%", "portfoy_agirlik_%", "tutarlilik_skor",
        ) or c in tablo.columns][:14]
        show = tablo[[c for c in cols if c in tablo.columns]].copy()
        try:
            tablo_html = show.to_html(index=False, float_format=lambda x: f"{x:.2f}" if pd.notna(x) else "")
        except Exception:
            tablo_html = show.astype(str).to_html(index=False)

    corr_html = ""
    if corr_df is not None and not corr_df.empty:
        try:
            corr_html = f"<h2>Korelasyon matrisi</h2>{corr_df.round(2).to_html()}"
        except Exception:
            pass

    ozet_block = ""
    if ozet_md:
        # basit markdown → html
        lines = []
        for ln in str(ozet_md).splitlines():
            if ln.startswith("### "):
                lines.append(f"<h2>{ln[4:]}</h2>")
            elif ln.startswith("#### "):
                lines.append(f"<h3>{ln[5:]}</h3>")
            elif ln.startswith("- "):
                lines.append(f"<li>{ln[2:]}</li>")
            elif ln.strip():
                lines.append(f"<p>{ln}</p>")
        ozet_block = "\n".join(lines)

    deger = ""
    try:
        deger = turkce_degerlendirme(
            sharpe=m.get("sharpe"), sortino=m.get("sortino"), max_dd=m.get("max_drawdown"),
            vol=m.get("volatilite"), cagr=m.get("cagr"), calmar=m.get("calmar"),
            getiri_1y=m.get("getiri_1y"), reel_1y=m.get("reel_1y"),
            vs_mevduat=m.get("vs_mevduat"), vs_bist100=m.get("vs_bist100"),
        )
    except Exception:
        deger = ""

    html = f"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="utf-8"/>
<title>{baslik} — Portföy Raporu</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1a202c;background:#f7fafc}}
h1{{color:#1a365d}} h2{{color:#2c5282;margin-top:28px}}
.kpi{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.kpi div{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;min-width:120px}}
.kpi b{{display:block;font-size:20px;color:#2b6cb0}}
table{{background:#fff;border-collapse:collapse}}
th,td{{padding:6px 10px;border:1px solid #e2e8f0}}
.note{{color:#718096;font-size:13px}}
</style></head><body>
<h1>{baslik}</h1>
<p class="note">Oluşturulma: {datetime.now().strftime("%Y-%m-%d %H:%M")} · Yatırım tavsiyesi değildir.</p>
{deger}
<div class="kpi">
  <div>1Y getiri<b>%{_f(m.get('getiri_1y'))}</b></div>
  <div>Sharpe<b>{_f(m.get('sharpe'), 2)}</b></div>
  <div>Max DD<b>%{_f(m.get('max_drawdown'))}</b></div>
  <div>Volatilite<b>%{_f(m.get('volatilite'))}</b></div>
  <div>Çeşitlendirme<b>{_f(diversify_score, 0) if diversify_score is not None else '—'}/100</b></div>
  <div>Ort. tehlike<b>{m.get('ort_tehlike_yazi') or (_f(m.get('ort_tehlike'), 1) if m.get('ort_tehlike') is not None else '—')}</b></div>
</div>
<p>{div_yorum}</p>
{ozet_block}
<h2>Pozisyonlar</h2>
{tablo_html or '<p>Tablo yok</p>'}
{corr_html}
{oneri_html}
</body></html>
"""
    try:
        out.write_text(html, encoding="utf-8")
    except Exception as e:
        print(f"Portföy HTML yazılamadı: {e}")
        return None

    # Quantstats ek raporu (ayrı dosya) — portföy fiyat serisi varsa
    qs_path = None
    if quantstats and fiyat_df is not None and not fiyat_df.empty:
        try:
            bm = None
            try:
                bm = yahoo_daily_returns("XU100.IS")
            except Exception:
                pass
            qs_path = quantstats_rapor(
                fiyat_df, kod=safe[:12] or "PORT", ad=baslik,
                benchmark_returns=bm, ekstra_metrikler={**m, **ekstra},
            )
        except Exception as e:
            print(f"Portföy quantstats atlandı: {e}")

    print(f"  ✓ Portföy HTML: {out}" + (f" | quantstats: {qs_path}" if qs_path else ""))
    return str(out)


def cesitlendirme_onerileri(
    analiz: "FonAnaliz",
    sepet_series: Dict[str, pd.Series],
    aday_df: Optional[pd.DataFrame] = None,
    max_aday: int = 40,
    top_n: int = 8,
    mevcut_kodlar: Optional[set] = None,
) -> List[dict]:
    """Sepete eklendiğinde ortalama |korelasyonu| düşürecek **TEFAS açık** fon önerileri.

    Yalnızca TEFAS'ta işlem gören, tasfiye/yasaklı olmayan fonlar önerilir.
    aday_df: son tarama/sıralama (kod sütunu); yine TEFAS açık kontrolünden geçer.
    """
    if not sepet_series or len(sepet_series) < 1:
        return []
    mevcut = set(str(k).upper() for k in (mevcut_kodlar or sepet_series.keys()))

    # Sepet getiri matrisi
    dfp = pd.DataFrame(sepet_series).dropna(how="all").ffill().dropna()
    if len(dfp) < 40:
        return []
    sepet_rets = dfp.pct_change().dropna()
    if sepet_rets.empty:
        return []

    def _tefas_acik_kontrol(kod: str, ad_hint: str = "") -> tuple:
        """(uygun_mu, ad). TEFAS işlem görüyor + tasfiye/yasaklı değil."""
        try:
            ok, neden, p = analiz.api.fon_uygun_mu(kod, ad=ad_hint or "")
            if not ok:
                return False, ad_hint or ""
            ad = (p.get("fonUnvan") if p else None) or ad_hint or kod
            # Çift kontrol: tefas_acik_mi
            if not analiz.api.tefas_acik_mi(kod):
                return False, ad
            return True, str(ad)[:55]
        except Exception:
            return False, ad_hint or ""

    # Aday havuzu: önce sıralama/tarama, yoksa TEFAS dağılım listesi
    ham_aday = []  # (kod, ad_hint)
    if aday_df is not None and not aday_df.empty and "kod" in aday_df.columns:
        for _, r in aday_df.iterrows():
            k = str(r.get("kod") or "").upper()
            if not k or k in mevcut:
                continue
            ham_aday.append((k, str(r.get("ad") or k)[:55]))
    if len(ham_aday) < 8:
        try:
            dag = analiz.api.dagilimlar()
            if dag is not None and not dag.empty:
                seen = {k for k, _ in ham_aday}
                for _, r in dag.iterrows():
                    k = str(r.get("kod") or "").upper()
                    if not k or k in mevcut or k in seen:
                        continue
                    ad = str(r.get("ad") or r.get("fonUnvan") or "")
                    if kurucu_yasakli_mi(ad) or unvan_tasfiye_mi(ad):
                        continue
                    ham_aday.append((k, ad[:55] if ad else k))
                    seen.add(k)
                    if len(ham_aday) >= max_aday * 3:
                        break
        except Exception:
            pass

    # TEFAS açık filtre — yalnızca işlem gören fonlar
    aday_kodlar = []
    ad_map = {}
    for k, ad_hint in ham_aday:
        if len(aday_kodlar) >= max_aday:
            break
        ok, ad = _tefas_acik_kontrol(k, ad_hint)
        if not ok:
            continue
        aday_kodlar.append(k)
        ad_map[k] = ad or k

    if not aday_kodlar:
        print("Çeşitlendirme: TEFAS açık aday bulunamadı", flush=True)
        return []

    # Mevcut çeşitlilik
    try:
        c0 = sepet_rets.corr()
        mask = np.triu(np.ones(c0.shape), k=1).astype(bool)
        avg0 = float(np.abs(c0.where(mask)).stack().mean()) if mask.any() else 0.5
        div0 = max(0, min(100, (1 - avg0) * 100))
    except Exception:
        avg0, div0 = 0.5, 50.0

    def _tehlike_etiket(risk_degeri) -> str:
        """SPK riskDegeri 1–7 → Türkçe tehlike seviyesi."""
        try:
            r = int(float(risk_degeri))
        except Exception:
            return "Bilinmiyor"
        if r <= 1:
            return "1 · Çok düşük"
        if r == 2:
            return "2 · Düşük"
        if r == 3:
            return "3 · Orta-düşük"
        if r == 4:
            return "4 · Orta"
        if r == 5:
            return "5 · Orta-yüksek"
        if r == 6:
            return "6 · Yüksek"
        return "7 · Çok yüksek"

    def _getiri_pct(s: pd.Series, gun: int) -> Optional[float]:
        if s is None or len(s) < 5:
            return None
        s = s.dropna().sort_index()
        if len(s) < 5:
            return None
        end = float(s.iloc[-1])
        if end <= 0:
            return None
        # yaklaşık takvim günü geriye
        hedef = s.index[-1] - pd.Timedelta(days=gun)
        sub = s[s.index <= hedef]
        if sub.empty:
            start = float(s.iloc[0])
        else:
            start = float(sub.iloc[-1])
        if start <= 0:
            return None
        return round((end / start - 1.0) * 100.0, 1)

    oneriler = []
    for kod in aday_kodlar:
        try:
            if not analiz.api.tefas_acik_mi(kod):
                continue
            # Profil: riskDegeri (tehlike)
            risk_degeri = None
            try:
                p = analiz.api.profil(kod)
                risk_degeri = p.get("riskDegeri")
                if risk_degeri is not None:
                    risk_degeri = int(float(risk_degeri))
            except Exception:
                pass

            fd = analiz.api.fiyat(kod, periyod=12)
            if fd is None or len(fd) < 60:
                continue
            s = fd.set_index("tarih")["fiyat"].astype(float).sort_index()
            s = s[~s.index.duplicated(keep="last")]
            fr = s.pct_change().dropna()
            ortak2 = sepet_rets.index.intersection(fr.index)
            if len(ortak2) < 40:
                continue
            corrs = []
            for col in sepet_rets.columns:
                c = float(fr.loc[ortak2].corr(sepet_rets.loc[ortak2, col]))
                if c == c:
                    corrs.append(abs(c))
            if not corrs:
                continue
            avg_rho = float(np.mean(corrs))
            n = len(sepet_rets.columns)
            old_pairs = n * (n - 1) / 2
            new_pairs = (n + 1) * n / 2
            est_avg = (old_pairs * avg0 + n * avg_rho) / max(new_pairs, 1)
            est_div = max(0, min(100, (1 - est_avg) * 100))
            delta = est_div - div0
            if avg_rho > 0.85 and delta < 1:
                continue

            # Getiri oranları (fiyat serisinden)
            g1a = _getiri_pct(s, 30)
            g3a = _getiri_pct(s, 90)
            g6a = _getiri_pct(s, 180)
            g1y = _getiri_pct(s, 365)
            # Volatilite kaba (tehlike tamamlayıcı)
            vol_y = None
            try:
                if len(fr) >= 40:
                    vol_y = round(float(fr.std() * np.sqrt(252) * 100), 1)
            except Exception:
                pass

            neden = ["TEFAS açık"]
            if avg_rho < 0.3:
                neden.append("sepetle düşük korelasyon")
            elif avg_rho < 0.5:
                neden.append("ılımlı korelasyon")
            if delta > 3:
                neden.append(f"çeşitlendirme +{delta:.0f} puan")
            try:
                ad = ad_map.get(kod, "")
                al = ad.lower()
                if "altın" in al or "gold" in al:
                    neden.append("altın/emtia profili")
                if "para piyas" in al or "likit" in al:
                    neden.append("likit / düşük vol")
                if "borçlanma" in al or "tahvil" in al:
                    neden.append("borçlanma ağırlıklı")
            except Exception:
                pass
            if risk_degeri is not None and risk_degeri >= 6:
                neden.append("yüksek tehlike")
            if g1y is not None and g1y < 0:
                neden.append("1Y negatif getiri")

            oneriler.append({
                "kod": kod,
                "ad": ad_map.get(kod, kod),
                "tehlike_seviyesi": _tehlike_etiket(risk_degeri),
                "risk_degeri": risk_degeri,
                "getiri_1a": g1a,
                "getiri_3a": g3a,
                "getiri_6a": g6a,
                "getiri_1y": g1y,
                "volatilite": vol_y,
                "ortalama_korelasyon": round(avg_rho, 3),
                "beklenen_cesitlilik": round(est_div, 1),
                "delta_cesitlilik": round(delta, 1),
                "neden": " · ".join(neden),
                "tefas": "açık",
            })
        except Exception:
            continue

    oneriler.sort(key=lambda x: (x.get("ortalama_korelasyon", 9), -x.get("delta_cesitlilik", 0)))
    return oneriler[:top_n]


def yahoo_daily_returns(symbol: str = "XU100.IS") -> Optional[pd.Series]:
    """Benchmark günlük getiri (quantstats için)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = requests.get(
            url, params={"range": "2y", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
        )
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        ts = result["timestamp"]
        idx = pd.to_datetime(ts, unit="s")
        s = pd.Series(closes, index=idx).dropna().astype(float)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        if getattr(s.index, "tz", None) is not None:
            s.index = s.index.tz_localize(None)
        return s.pct_change().dropna()
    except Exception as e:
        print(f"  Benchmark getirisi alınamadı ({symbol}): {e}")
        return None


def fon_bist_korelasyon(
    fiyat_df: pd.DataFrame,
    bist_rets: Optional[pd.Series] = None,
    min_ortak: int = 40,
) -> Optional[float]:
    """Fon günlük getirisi ile BIST100 korelasyonu (−1 … +1)."""
    if fiyat_df is None or fiyat_df.empty or len(fiyat_df) < min_ortak:
        return None
    if bist_rets is None or len(bist_rets) < min_ortak:
        bist_rets = yahoo_daily_returns("XU100.IS")
    if bist_rets is None or len(bist_rets) < min_ortak:
        return None
    try:
        s = fiyat_df.set_index("tarih")["fiyat"].sort_index().astype(float)
        s = s[~s.index.duplicated(keep="last")].dropna()
        if getattr(s.index, "tz", None) is not None:
            s.index = s.index.tz_localize(None)
        fr = s.pct_change().dropna()
        bm = bist_rets.copy()
        if getattr(bm.index, "tz", None) is not None:
            bm.index = bm.index.tz_localize(None)
        ortak = fr.index.intersection(bm.index)
        if len(ortak) < min_ortak:
            return None
        corr = float(fr.loc[ortak].corr(bm.loc[ortak]))
        if corr != corr:  # NaN
            return None
        return round(corr, 3)
    except Exception:
        return None


def df_bist_korelasyon_ekle(
    analiz: "FonAnaliz",
    df: pd.DataFrame,
    bist_rets: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Sonuç tablosuna bist_korelasyon sütunu ekler (aday sayısı sınırlı olduğu için uygulanabilir)."""
    if df is None or df.empty or "kod" not in df.columns:
        return df
    out = df.copy()
    if bist_rets is None:
        bist_rets = yahoo_daily_returns("XU100.IS")
    corrs = []
    for kod in out["kod"].astype(str):
        try:
            fd = analiz.api.fiyat(kod.upper(), periyod=12)
            corrs.append(fon_bist_korelasyon(fd, bist_rets))
        except Exception:
            corrs.append(None)
    out["bist_korelasyon"] = corrs
    return out


# ─── Piyasa rejim metrikleri (BIST düşünce artan / kriz / asimetrik) ─────────
# Tarihsel BIST düşüş pencereleri (yaklaşık; günlük veriden de dinamik bulunur)
BIST_KRIZ_PENCERELERI = [
    # (başlangıç, bitiş, etiket) — bilinen sert düşüş dönemleri
    ("2020-02-20", "2020-03-23", "COVID çöküşü"),
    ("2021-11-15", "2021-12-20", "2021 kur/enflasyon şoku"),
    ("2022-08-15", "2022-10-15", "2022 yaz-sonbahar düşüş"),
    ("2023-05-10", "2023-05-31", "Seçim öncesi dalga"),
    ("2024-07-15", "2024-08-20", "2024 yaz düzeltmesi"),
]


def _align_fund_bench(fiyat_df: pd.DataFrame, bist_rets: pd.Series, min_ortak: int = 60):
    """Fon fiyat serisi ile BIST günlük getirilerini hizalar. (fon_rets, bm_rets) veya (None, None)."""
    if fiyat_df is None or fiyat_df.empty or bist_rets is None or len(bist_rets) < min_ortak:
        return None, None
    try:
        s = fiyat_df.set_index("tarih")["fiyat"].sort_index().astype(float)
        s = s[~s.index.duplicated(keep="last")].dropna()
        if getattr(s.index, "tz", None) is not None:
            s.index = s.index.tz_localize(None)
        fr = s.pct_change().dropna()
        bm = bist_rets.copy()
        if getattr(bm.index, "tz", None) is not None:
            bm.index = bm.index.tz_localize(None)
        ortak = fr.index.intersection(bm.index)
        if len(ortak) < min_ortak:
            return None, None
        return fr.loc[ortak], bm.loc[ortak]
    except Exception:
        return None, None


def _kriz_pencereleri_dinamik(bist_rets: pd.Series, min_dd: float = -0.12, pencere: int = 40):
    """BIST günlük getirilerinden sert düşüş pencereleri bulur.
    min_dd: kümülatif getiri eşiği (örn. -0.12 = %12 düşüş).
    Dönüş: list of (start_ts, end_ts, label).
    """
    if bist_rets is None or len(bist_rets) < pencere * 2:
        return []
    try:
        px = (1 + bist_rets).cumprod()
        # Rolling max'tan düşüş
        roll_max = px.cummax()
        dd = px / roll_max - 1.0
        # Eşik altı bölgeler
        mask = dd <= min_dd
        pencereler = []
        if not mask.any():
            return []
        # Ardışık True grupları
        grup = (mask != mask.shift(fill_value=False)).cumsum()
        for g, sub in mask.groupby(grup):
            if not sub.any() or not sub.iloc[0]:
                continue
            idx = sub.index[sub.values]
            if len(idx) < 5:
                continue
            start, end = idx[0], idx[-1]
            # Penceredeki gerçek kümülatif düşüş
            seg = px.loc[start:end]
            if len(seg) < 3:
                continue
            ret = float(seg.iloc[-1] / seg.iloc[0] - 1)
            if ret > min_dd * 0.5:  # yeterince sert değilse atla
                continue
            label = f"Düşüş {start.date()}→{end.date()} (%{ret*100:.0f})"
            pencereler.append((start, end, label))
        # En kötü 5 pencere
        pencereler.sort(key=lambda x: float(px.loc[x[0]:x[1]].iloc[-1] / px.loc[x[0]:x[1]].iloc[0] - 1))
        return pencereler[:5]
    except Exception:
        return []


def fon_rejim_metrikleri(
    fiyat_df: pd.DataFrame,
    bist_rets: Optional[pd.Series] = None,
    min_ortak: int = 60,
) -> dict:
    """Fonun BIST'e göre rejim davranışını ölçer.

    Dönen anahtarlar:
      bist_korelasyon, beta, beta_up, beta_down,
      upside_capture, downside_capture, capture_ratio,
      bist_dusuk_gun_fon (BIST negatif günlerde ort. fon getirisi %),
      kriz_getiri (bilinen/dinamik kriz pencerelerinde ort. kümülatif %),
      asimetri_skor (0–100; yüksek = yükselişte katılır, düşüşte korur),
      dususte_artan_skor (0–100; yüksek = BIST düşünce fon artar/az düşer),
      kriz_dayanik_skor (0–100; yüksek = krizlerde az kaybeder).
    """
    empty = {
        "bist_korelasyon": None, "beta": None, "beta_up": None, "beta_down": None,
        "upside_capture": None, "downside_capture": None, "capture_ratio": None,
        "bist_dusuk_gun_fon": None, "kriz_getiri": None,
        "asimetri_skor": None, "dususte_artan_skor": None, "kriz_dayanik_skor": None,
    }
    if bist_rets is None:
        bist_rets = yahoo_daily_returns("XU100.IS")
    fr, bm = _align_fund_bench(fiyat_df, bist_rets, min_ortak=min_ortak)
    if fr is None:
        return empty

    out = dict(empty)
    try:
        # Korelasyon & beta (tüm dönem)
        out["bist_korelasyon"] = round(float(fr.corr(bm)), 3)
        var_b = float(bm.var())
        if var_b > 1e-12:
            out["beta"] = round(float(fr.cov(bm) / var_b), 3)

        # Up / down gün ayrımı
        up_m = bm > 0
        dn_m = bm < 0
        if up_m.sum() >= 20 and dn_m.sum() >= 20:
            var_up = float(bm[up_m].var())
            var_dn = float(bm[dn_m].var())
            if var_up > 1e-12:
                out["beta_up"] = round(float(fr[up_m].cov(bm[up_m]) / var_up), 3)
            if var_dn > 1e-12:
                out["beta_down"] = round(float(fr[dn_m].cov(bm[dn_m]) / var_dn), 3)

            # Capture: fon ort getiri / bench ort getiri (yıllık değil, günlük ort.)
            bm_up_mean = float(bm[up_m].mean())
            bm_dn_mean = float(bm[dn_m].mean())
            fr_up_mean = float(fr[up_m].mean())
            fr_dn_mean = float(fr[dn_m].mean())
            if abs(bm_up_mean) > 1e-8:
                out["upside_capture"] = round(fr_up_mean / bm_up_mean * 100, 1)
            if abs(bm_dn_mean) > 1e-8:
                # downside capture: fon da negatifse pozitif oran; fon pozitifse negatif (=koruma)
                out["downside_capture"] = round(fr_dn_mean / bm_dn_mean * 100, 1)
            if out["upside_capture"] is not None and out["downside_capture"] is not None:
                # capture_ratio > 1 = asimetrik iyi (yükselişte daha çok katılır)
                if abs(out["downside_capture"]) > 1e-6:
                    out["capture_ratio"] = round(out["upside_capture"] / max(out["downside_capture"], 1e-6), 2)
                else:
                    out["capture_ratio"] = 99.0 if out["upside_capture"] > 0 else 0.0

            # BIST negatif günlerde fonun ortalama günlük getirisi (%)
            out["bist_dusuk_gun_fon"] = round(float(fr[dn_m].mean()) * 100, 3)

        # Kriz pencereleri: sabit liste + dinamik
        kriz_rets = []
        # Sabit pencereler
        for bas, bit, _lab in BIST_KRIZ_PENCERELERI:
            try:
                t0 = pd.Timestamp(bas)
                t1 = pd.Timestamp(bit)
                seg_f = fr.loc[(fr.index >= t0) & (fr.index <= t1)]
                if len(seg_f) >= 5:
                    kriz_rets.append(float((1 + seg_f).prod() - 1) * 100)
            except Exception:
                pass
        # Dinamik (veri içinde en kötü düşüşler)
        for t0, t1, _lab in _kriz_pencereleri_dinamik(bm, min_dd=-0.10, pencere=30):
            try:
                seg_f = fr.loc[(fr.index >= t0) & (fr.index <= t1)]
                if len(seg_f) >= 5:
                    kriz_rets.append(float((1 + seg_f).prod() - 1) * 100)
            except Exception:
                pass
        if kriz_rets:
            out["kriz_getiri"] = round(float(np.median(kriz_rets)), 2)

        # ── Skorlar (0–100) ──────────────────────────────────────────────
        # 1) Düşünce artan: düşük/negatif ρ + BIST- down günlerde pozitif/az negatif fon
        skor_d = 50.0
        rho = out["bist_korelasyon"]
        if rho is not None:
            # ρ=1 → 0 puan, ρ=0 → 50, ρ=-1 → 100
            skor_d = max(0.0, min(100.0, (1.0 - rho) * 50.0))
        bd = out["bist_dusuk_gun_fon"]
        if bd is not None:
            # günlük %0.05 ≈ iyi; +0.1 çok iyi; -0.2 kötü
            skor_d = 0.6 * skor_d + 0.4 * max(0.0, min(100.0, 50 + bd * 400))
        out["dususte_artan_skor"] = round(skor_d, 1)

        # 2) Kriz dayanıklı: kriz pencerelerinde az kayıp / kazanç
        skor_k = 50.0
        kg = out["kriz_getiri"]
        if kg is not None:
            # -30% → ~0, 0% → 60, +10% → 100
            skor_k = max(0.0, min(100.0, 60 + kg * 2.5))
        # Max DD de ek sinyali (varsa fiyat serisinden)
        try:
            cum = (1 + fr).cumprod()
            mdd = float((cum / cum.cummax() - 1).min()) * 100
            # mdd -5 → iyi, -40 → kötü
            skor_k = 0.7 * skor_k + 0.3 * max(0.0, min(100.0, 100 + mdd * 2))
        except Exception:
            pass
        out["kriz_dayanik_skor"] = round(skor_k, 1)

        # 3) Asimetri: yüksek upside capture, düşük downside capture
        skor_a = 50.0
        uc = out["upside_capture"]
        dc = out["downside_capture"]
        if uc is not None and dc is not None:
            # ideal: uc >= 80, dc <= 60
            up_part = max(0.0, min(100.0, uc))  # 100 capture = 100 puan
            # dc düşük = iyi; dc=0 → 100, dc=100 → 0, dc=150 → 0
            dn_part = max(0.0, min(100.0, 100 - dc))
            skor_a = 0.45 * up_part + 0.55 * dn_part
            if out["capture_ratio"] is not None and out["capture_ratio"] > 1.2:
                skor_a = min(100.0, skor_a + 8)
        elif out["beta_up"] is not None and out["beta_down"] is not None:
            # beta_up yüksek, beta_down düşük = iyi
            bu, bdwn = out["beta_up"], out["beta_down"]
            skor_a = max(0.0, min(100.0, 50 + (bu - bdwn) * 40))
        out["asimetri_skor"] = round(skor_a, 1)

    except Exception:
        pass
    return out


def df_rejim_metrikleri_ekle(
    analiz: "FonAnaliz",
    df: pd.DataFrame,
    bist_rets: Optional[pd.Series] = None,
    periyod: int = 36,
) -> pd.DataFrame:
    """Sonuç tablosuna rejim metriklerini ekler (birkaç saniye–dakika sürebilir)."""
    if df is None or df.empty or "kod" not in df.columns:
        return df
    out = df.copy()
    if bist_rets is None:
        bist_rets = yahoo_daily_returns("XU100.IS")
    rows = []
    for kod in out["kod"].astype(str):
        try:
            fd = analiz.api.fiyat(kod.upper(), periyod=periyod)
            rows.append(fon_rejim_metrikleri(fd, bist_rets))
        except Exception:
            rows.append(fon_rejim_metrikleri(None, bist_rets))
    meta_df = pd.DataFrame(rows)
    for c in meta_df.columns:
        out[c] = meta_df[c].values
    return out


def _secim_gerekce(row) -> str:
    """Tek fon için kısa seçim gerekçesi."""
    parts = []
    sh = _safe_num(row.get("sharpe"))
    if sh is not None and sh >= 1:
        parts.append(f"Sharpe {sh:.2f}")
    so = _safe_num(row.get("sortino"))
    if so is not None and so >= 1.2:
        parts.append(f"Sortino {so:.2f}")
    g = _safe_num(row.get("getiri_1y"))
    if g is not None and g > 0:
        parts.append(f"1Y %{g:.0f}")
    reel = _safe_num(row.get("reel_1y"))
    if reel is not None and reel > 0:
        parts.append("reel+")
    liq = _safe_num(row.get("likidite_skor"))
    if liq is not None and liq >= 60:
        parts.append("likit")
    mdd = _safe_num(row.get("max_drawdown"))
    if mdd is not None and mdd >= -12:
        parts.append(f"DD {mdd:.0f}%")
    kaz = _safe_num(row.get("kazanma_orani"))
    if kaz is not None and kaz >= 52:
        parts.append(f"kazanma %{kaz:.0f}")
    bk = _safe_num(row.get("bist_korelasyon"))
    if bk is not None and bk < 0.5:
        parts.append(f"düşük BIST ρ={bk:.2f}")
    tur = row.get("baskin_tur") or row.get("kategori")
    if tur:
        parts.append(str(tur)[:20])
    return " · ".join(parts) if parts else "genel skor"



# Akıllı seçim filtre ön ayarları (Muhafazakâr / Dengeli / Agresif …)
AKILLI_FILTRE_PRESET = {
    "Muhafazakâr": {
        "ad": "Muhafazakâr — düşük risk, yüksek tutarlılık",
        "max_risk_skor": 12.0, "min_liq": 55.0, "min_kazanma": 52.0, "min_en_kotu": -4.0,
        "min_sharpe": 0.3, "min_sortino": 0.5, "max_dd": -10.0, "max_vol": 12.0,
        "max_var95": 2.5, "max_cvar95": 4.0, "min_cagr": None, "min_calmar": 0.5,
        "max_bist_korr": 0.85, "temiz_bayrak": True,
        "n_fon": 8, "max_w": 0.18, "max_ayni_tur": 2, "yontem": "risk_parity",
    },
    "Dengeli": {
        "ad": "Dengeli — getiri / risk dengesi (önerilen)",
        "max_risk_skor": 18.0, "min_liq": 45.0, "min_kazanma": 48.0, "min_en_kotu": -8.0,
        "min_sharpe": 0.0, "min_sortino": None, "max_dd": -20.0, "max_vol": 25.0,
        "max_var95": 4.0, "max_cvar95": 6.0, "min_cagr": None, "min_calmar": None,
        "max_bist_korr": None, "temiz_bayrak": True,
        "n_fon": 10, "max_w": 0.20, "max_ayni_tur": 3, "yontem": "max_sharpe",
    },
    "BIST düşünce artan": {
        "ad": "BIST düşünce artan — düşük / negatif endeks korelasyonu",
        "max_risk_skor": 22.0, "min_liq": 30.0, "min_kazanma": None, "min_en_kotu": None,
        "min_sharpe": None, "min_sortino": None, "max_dd": -35.0, "max_vol": 40.0,
        "max_var95": None, "max_cvar95": None, "min_cagr": None, "min_calmar": None,
        "max_bist_korr": 0.25,  # düşük veya negatif ρ
        "temiz_bayrak": False,
        "sirala": "dususte_artan_skor",  # rejim skoru (yüksek = iyi)
        "min_dususte_artan": 55.0,
        "n_fon": 12, "max_w": 0.20, "max_ayni_tur": 3, "yontem": "risk_parity",
    },
    "Kriz dayanıklı": {
        "ad": "Kriz dayanıklı — tarihsel BIST çöküşlerinde az kaybeden",
        "max_risk_skor": 20.0, "min_liq": 35.0, "min_kazanma": None, "min_en_kotu": -12.0,
        "min_sharpe": None, "min_sortino": None, "max_dd": -25.0, "max_vol": 30.0,
        "max_var95": 5.0, "max_cvar95": 8.0, "min_cagr": None, "min_calmar": None,
        "max_bist_korr": 0.70,
        "temiz_bayrak": False,
        "sirala": "kriz_dayanik_skor",
        "min_kriz_dayanik": 55.0,
        "n_fon": 10, "max_w": 0.18, "max_ayni_tur": 3, "yontem": "risk_parity",
    },
    "Asimetrik (yükselir-korur)": {
        "ad": "Asimetrik — borsa yükselince katılır, düşünce korur",
        "max_risk_skor": 22.0, "min_liq": 30.0, "min_kazanma": None, "min_en_kotu": None,
        "min_sharpe": 0.0, "min_sortino": None, "max_dd": -30.0, "max_vol": 40.0,
        "max_var95": None, "max_cvar95": None, "min_cagr": None, "min_calmar": None,
        "max_bist_korr": None,
        "temiz_bayrak": False,
        "sirala": "asimetri_skor",
        "min_asimetri": 55.0,
        "n_fon": 12, "max_w": 0.20, "max_ayni_tur": 3, "yontem": "max_sharpe",
    },
    "Kısa vade kazanan": {
        "ad": "Kısa vade — son 1A / 3A en çok kazananlar",
        "max_risk_skor": 22.0, "min_liq": 40.0, "min_kazanma": 45.0, "min_en_kotu": -10.0,
        "min_sharpe": None, "min_sortino": None, "max_dd": -30.0, "max_vol": 40.0,
        "max_var95": 5.0, "max_cvar95": 8.0, "min_cagr": None, "min_calmar": None,
        "max_bist_korr": None, "temiz_bayrak": False,
        "min_1a": 3.0, "min_3a": 8.0,  # kısa vade taban getiri %
        "sirala": "getiri_1a",  # birincil sıralama
        "n_fon": 10, "max_w": 0.22, "max_ayni_tur": 3, "yontem": "score",
    },
    "Agresif": {
        "ad": "Agresif — yüksek getiri toleransı",
        "max_risk_skor": 25.0, "min_liq": 35.0, "min_kazanma": 45.0, "min_en_kotu": -12.0,
        "min_sharpe": None, "min_sortino": None, "max_dd": -40.0, "max_vol": 50.0,
        "max_var95": 6.0, "max_cvar95": 10.0, "min_cagr": 15.0, "min_calmar": None,
        "max_bist_korr": None, "temiz_bayrak": False,
        "n_fon": 12, "max_w": 0.25, "max_ayni_tur": 4, "yontem": "max_sharpe",
    },
    "Enflasyon üstü": {
        "ad": "Enflasyon üstü — reel getiri odaklı",
        "max_risk_skor": 18.0, "min_liq": 45.0, "min_kazanma": 48.0, "min_en_kotu": -8.0,
        "min_sharpe": 0.0, "min_sortino": None, "max_dd": -25.0, "max_vol": 30.0,
        "max_var95": 4.0, "max_cvar95": 6.0, "min_cagr": None, "min_calmar": None,
        "max_bist_korr": None, "temiz_bayrak": True, "min_reel": True,
        "n_fon": 10, "max_w": 0.20, "max_ayni_tur": 3, "yontem": "max_sharpe",
    },
    "Düşük risk / likit": {
        "ad": "Düşük risk — yüksek likidite",
        "max_risk_skor": 10.0, "min_liq": 70.0, "min_kazanma": 55.0, "min_en_kotu": -3.0,
        "min_sharpe": 0.2, "min_sortino": 0.8, "max_dd": -6.0, "max_vol": 10.0,
        "max_var95": 2.0, "max_cvar95": 3.0, "min_cagr": None, "min_calmar": 0.8,
        "max_bist_korr": 0.90, "temiz_bayrak": True,
        "n_fon": 8, "max_w": 0.15, "max_ayni_tur": 2, "yontem": "risk_parity",
    },
    "Özel": {
        "ad": "Özel — aşağıdaki alanlardan elle ayarla",
        "max_risk_skor": 18.0, "min_liq": 45.0, "min_kazanma": 48.0, "min_en_kotu": -8.0,
        "min_sharpe": 0.0, "min_sortino": None, "max_dd": -25.0, "max_vol": 35.0,
        "max_var95": 4.0, "max_cvar95": 6.0, "min_cagr": None, "min_calmar": None,
        "max_bist_korr": None, "temiz_bayrak": True,
        "n_fon": 10, "max_w": 0.20, "max_ayni_tur": 3, "yontem": "max_sharpe",
    },
}


AKILLI_SECIM_ACIKLAMA = r"""
### Akıllı seçim nasıl çalışır?

**1. Eleme (filtre kapıları)**  
Önce net eşikleri geçmeyen fonlar çıkarılır. Amaç: “yüksek getiri ama şüpheli / çok riskli / likit değil” adayları elemek.

| Kriter | Ne bakıyor? | Tipik eşik |
|--------|-------------|------------|
| risk_skor | Anomali, ani sıçrama, akran dışı davranış | ≤ 18 |
| likidite_skor | Büyüklük, yatırımcı, likit varlık | ≥ 45 |
| kazanma oranı | Günlük pozitif gün yüzdesi | ≥ %48 |
| en kötü gün | Tek gündeki en sert kayıp | ≥ −8% |
| Sharpe / Sortino | Risk-ayarlı getiri | Sharpe ≥ 0 |
| Max DD / vol | Tarihsel düşüş ve oynaklık | DD ≥ −25%, vol ≤ %35 |
| VaR / cVaR | Kuyruk riski (günlük) | \|VaR\| ≤ 4, \|cVaR\| ≤ 6 |
| BIST ρ | Endeksle birlikte hareket | isteğe bağlı üst sınır |
| kategori | TEFAS kategori metni | örn. Hisse, Borçlanma |

**2. Skor (sıralama)**  
Kalan adaylar **akilli_skor** ile sıralanır:

`akilli_skor = secim_skor (veya rapor skoru) + bonus − ceza`

| Bileşen | Etki |
|---------|------|
| **Temel skor** | Sharpe, Sortino, MaxDD, reel getiri, likidite vb. birleşik rapor skoru |
| **Kazanma bonusu** | %48 üzeri her puan → +0.4 |
| **En kötü gün** | −6%'den yumuşaksa → +4 |
| **Likidite** | 50 üzeri her puan → +0.08 |
| **Düşük BIST ρ** | 0.70 altı korelasyon → çeşitlendirme bonusu (max ~+5.6) |
| **Anomali cezası** | risk_skor 8 üzeri → puan düşer |

**3. Çeşitlendirme**  
Aynı baskın türden (hisse / tahvil / altın…) en fazla N fon alınır (varsayılan 3).  
Liste dolmazsa skor sırasıyla yedek doldurulur.

**4. Ağırlık**  
Seçilen fonlara Max Sharpe / Risk Parity / skor-orantılı / eşit ağırlık uygulanır; tek fon tavanı (varsayılan %20) aşılmaz.

> Bu otomatik bir **seçim asistanıdır**, yatırım tavsiyesi değildir. Geçmiş performans geleceği garanti etmez.
"""


def akilli_skor_kirilim(row) -> dict:
    """Akıllı skorun sayısal kırılımı (şeffaflık için)."""
    base = float(_safe_num(row.get("secim_skor")) or _safe_num(row.get("skor")) or 0)
    out = {
        "temel_skor": round(base, 2),
        "bonus_kazanma": 0.0,
        "bonus_en_kotu_gun": 0.0,
        "bonus_likidite": 0.0,
        "bonus_bist": 0.0,
        "ceza_anomali": 0.0,
    }
    kaz = _safe_num(row.get("kazanma_orani"))
    if kaz is not None:
        out["bonus_kazanma"] = round(max(0, (kaz - 48) * 0.4), 2)
    ek = _safe_num(row.get("en_kotu_gun"))
    if ek is not None and ek > -6:
        out["bonus_en_kotu_gun"] = 4.0
    liq = _safe_num(row.get("likidite_skor"))
    if liq is not None:
        out["bonus_likidite"] = round(max(0, (liq - 50) * 0.08), 2)
    bk = _safe_num(row.get("bist_korelasyon"))
    if bk is not None:
        out["bonus_bist"] = round(max(0, (0.7 - bk) * 8), 2)
    rs = _safe_num(row.get("risk_skor"))
    if rs is not None:
        out["ceza_anomali"] = round(-max(0, rs - 8) * 0.5, 2)
    out["akilli_skor"] = round(
        out["temel_skor"]
        + out["bonus_kazanma"]
        + out["bonus_en_kotu_gun"]
        + out["bonus_likidite"]
        + out["bonus_bist"]
        + out["ceza_anomali"],
        2,
    )
    return out


def akilli_portfoy_sec(
    analiz: "FonAnaliz",
    df: pd.DataFrame,
    n_fon: int = 10,
    yontem: str = "max_sharpe",
    max_w: float = 0.20,
    max_ayni_tur: int = 3,
    max_bist_korr: Optional[float] = None,
    diversify: bool = True,
) -> dict:
    """
    Akıllı seçim: çok kriterli skor + tür çeşitliliği + optimize ağırlık.

    Adımlar: (1) filtre kapıları dışarıda uygulanmış liste beklenir
    (2) akilli_skor ile sırala (3) tür başına max N (4) ağırlık optimizasyonu.
    """
    if df is None or df.empty:
        return {"error": "Aday yok"}
    work = df.copy()
    if "secim_skor" not in work.columns:
        work["secim_skor"] = work.apply(rapor_uyumlu_skor, axis=1)

    kirilimlar = work.apply(akilli_skor_kirilim, axis=1, result_type="expand")
    for col in kirilimlar.columns:
        work[col] = kirilimlar[col]
    work["akilli_skor"] = kirilimlar["akilli_skor"]

    if max_bist_korr is not None and "bist_korelasyon" in work.columns:
        bk = pd.to_numeric(work["bist_korelasyon"], errors="coerce")
        work = work[bk.isna() | (bk <= float(max_bist_korr))]

    work = work.sort_values(
        [c for c in ["akilli_skor", "secim_skor", "sharpe", "getiri_1y"] if c in work.columns],
        ascending=False,
    )

    secilen_rows = []
    tur_say = {}
    gerekceler = {}
    for _, row in work.iterrows():
        if len(secilen_rows) >= n_fon:
            break
        tur = str(row.get("baskin_tur") or row.get("kategori") or "diger")
        if diversify and tur_say.get(tur, 0) >= max_ayni_tur:
            continue
        secilen_rows.append(row)
        tur_say[tur] = tur_say.get(tur, 0) + 1
        gerekceler[str(row.get("kod"))] = _secim_gerekce(row)

    if len(secilen_rows) < n_fon:
        alinan = {str(r.get("kod")) for r in secilen_rows}
        for _, row in work.iterrows():
            if len(secilen_rows) >= n_fon:
                break
            kod = str(row.get("kod"))
            if kod in alinan:
                continue
            secilen_rows.append(row)
            alinan.add(kod)
            gerekceler[kod] = _secim_gerekce(row) + " · yedek doldurma"

    if not secilen_rows:
        return {"error": "Filtre sonrası fon kalmadı"}

    secili = pd.DataFrame(secilen_rows).reset_index(drop=True)
    secili["secim_gerekce"] = secili["kod"].astype(str).map(lambda k: gerekceler.get(k, ""))
    if "rapor_skor" not in secili.columns:
        secili["rapor_skor"] = secili["akilli_skor"] if "akilli_skor" in secili.columns else secili.apply(rapor_uyumlu_skor, axis=1)

    opt = siralamadan_agirlik_hesapla(analiz, secili, yontem=yontem, max_w=max_w)
    if opt.get("error"):
        kodlar = secili["kod"].astype(str).tolist()
        opt = {
            "method": "equal",
            "weights": {k: 1.0 / len(kodlar) for k in kodlar},
            "risk_contrib_pct": {k: 100.0 / len(kodlar) for k in kodlar},
            "port_mu": None, "port_vol": None, "port_sharpe": None,
        }

    # Özet metin
    ozet_satir = []
    for _, r in secili.iterrows():
        w = (opt.get("weights") or {}).get(str(r["kod"]), 0)
        ozet_satir.append(
            f"{r['kod']}: %{w*100:.1f} — {gerekceler.get(str(r['kod']), '')}"
        )

    return {
        "secili": secili,
        "weights": opt.get("weights") or {},
        "risk_contrib_pct": opt.get("risk_contrib_pct") or {},
        "method": opt.get("method"),
        "port_mu": opt.get("port_mu"),
        "port_vol": opt.get("port_vol"),
        "port_sharpe": opt.get("port_sharpe"),
        "tur_dagilim": tur_say,
        "n": len(secili),
        "gerekceler": gerekceler,
        "ozet": ozet_satir,
    }


def df_tutarlilik_skoru_ekle(df: pd.DataFrame) -> pd.DataFrame:
    """Kategori-içi dönem rank tutarlılığı (1A/3A/6A/1Y).

    Her dönem için kategori (veya tüm evren) içinde yüzdelik rank hesaplanır.
    tutarlilik_skor = ortalama rank − 0.5 · rank std  (yüksek = istikrarlı üst yarı).
    tutarlilik_n = kaç dönemde veri var.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    periods = [c for c in ("getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y") if c in out.columns]
    if not periods:
        out["tutarlilik_skor"] = np.nan
        out["tutarlilik_n"] = 0
        return out
    group_col = None
    for c in ("kategori", "baskin_tur", "akran_grubu"):
        if c in out.columns and out[c].notna().sum() >= 8:
            group_col = c
            break
    ranks = []
    for col in periods:
        s = pd.to_numeric(out[col], errors="coerce")
        if group_col:
            r = s.groupby(out[group_col]).rank(pct=True, ascending=True)
        else:
            r = s.rank(pct=True, ascending=True)
        ranks.append(r)
    R = pd.concat(ranks, axis=1)
    out["tutarlilik_n"] = R.notna().sum(axis=1).astype(int)
    out["tutarlilik_skor"] = (
        R.mean(axis=1) - 0.5 * R.std(axis=1, ddof=0).fillna(0)
    ).round(3) * 100  # 0–100 ölçek
    return out


def cekirdek_uydu_portfoy(
    analiz: "FonAnaliz",
    df: pd.DataFrame,
    n_cekirdek: int = 6,
    n_uydu: int = 4,
    cekirdek_agirlik: float = 0.70,
    yontem: str = "risk_parity",
    max_w_cekirdek: float = 0.18,
    max_w_uydu: float = 0.12,
    max_ayni_tur: int = 2,
) -> dict:
    """Çekirdek–uydu portföy.

    Çekirdek: düşük vol, yüksek likidite, iyi tutarlılık / kriz-asimetri skoru.
    Uydu: yüksek getiri / agresif skor (çekirdekte olmayanlar).
    Toplam ağırlık: çekirdek ~%70, uydu ~%30 (cekirdek_agirlik ile ayarlanır).
    """
    if df is None or df.empty:
        return {"error": "Aday yok"}
    work = df.copy()
    if "tutarlilik_skor" not in work.columns:
        work = df_tutarlilik_skoru_ekle(work)
    if "secim_skor" not in work.columns:
        work["secim_skor"] = work.apply(rapor_uyumlu_skor, axis=1)

    # Çekirdek aday skoru
    def _core_score(row):
        s = 0.0
        for col, w, inv in (
            ("volatilite", 0.25, True),
            ("max_drawdown", 0.15, False),  # max_dd negatif; yüksek (az negatif) iyi → abs küçük
            ("likidite_skor", 0.20, False),
            ("tutarlilik_skor", 0.20, False),
            ("kriz_dayanik_skor", 0.10, False),
            ("asimetri_skor", 0.10, False),
        ):
            v = _safe_num(row.get(col))
            if v is None:
                continue
            if col == "max_drawdown":
                # -5 → iyi, -40 → kötü → 0–100
                s += w * max(0, min(100, 100 + v * 2))
            elif inv and col == "volatilite":
                s += w * max(0, min(100, 100 - v * 2))
            else:
                s += w * max(0, min(100, float(v)))
        return s

    work["_core"] = work.apply(_core_score, axis=1)
    work["_uydu"] = pd.to_numeric(work.get("getiri_1y"), errors="coerce").fillna(-999)
    if "secim_skor" in work.columns:
        work["_uydu"] = work["_uydu"] + pd.to_numeric(work["secim_skor"], errors="coerce").fillna(0) * 0.3

    # Çekirdek seç
    core_rows, tur_say = [], {}
    for _, row in work.sort_values("_core", ascending=False).iterrows():
        if len(core_rows) >= n_cekirdek:
            break
        tur = str(row.get("baskin_tur") or row.get("kategori") or "diger")
        if tur_say.get(tur, 0) >= max_ayni_tur:
            continue
        # Çekirdek kapıları: aşırı vol/DD ele
        vol = _safe_num(row.get("volatilite"))
        mdd = _safe_num(row.get("max_drawdown"))
        if vol is not None and vol > 30:
            continue
        if mdd is not None and mdd < -30:
            continue
        core_rows.append(row)
        tur_say[tur] = tur_say.get(tur, 0) + 1

    core_kod = {str(r.get("kod")) for r in core_rows}
    # Uydu: çekirdek dışı, yüksek getiri
    uydu_rows = []
    tur_say_u = {}
    for _, row in work.sort_values("_uydu", ascending=False).iterrows():
        if len(uydu_rows) >= n_uydu:
            break
        kod = str(row.get("kod"))
        if kod in core_kod:
            continue
        tur = str(row.get("baskin_tur") or row.get("kategori") or "diger")
        if tur_say_u.get(tur, 0) >= max_ayni_tur:
            continue
        uydu_rows.append(row)
        tur_say_u[tur] = tur_say_u.get(tur, 0) + 1

    if not core_rows and not uydu_rows:
        return {"error": "Çekirdek/uydu aday bulunamadı"}

    secili = pd.DataFrame(core_rows + uydu_rows).reset_index(drop=True)
    secili["rol"] = (["çekirdek"] * len(core_rows)) + (["uydu"] * len(uydu_rows))

    # Ağırlık: önce grup içi optimize, sonra grup toplamını ölçekle
    weights = {}
    if core_rows:
        core_df = pd.DataFrame(core_rows)
        opt_c = siralamadan_agirlik_hesapla(analiz, core_df, yontem=yontem, max_w=max_w_cekirdek)
        wc = opt_c.get("weights") or {str(r.get("kod")): 1.0 / len(core_rows) for r in core_rows}
        s = sum(wc.values()) or 1.0
        for k, v in wc.items():
            weights[k] = (v / s) * float(cekirdek_agirlik)
    if uydu_rows:
        uydu_df = pd.DataFrame(uydu_rows)
        opt_u = siralamadan_agirlik_hesapla(analiz, uydu_df, yontem="max_sharpe", max_w=max_w_uydu)
        wu = opt_u.get("weights") or {str(r.get("kod")): 1.0 / len(uydu_rows) for r in uydu_rows}
        s = sum(wu.values()) or 1.0
        uydu_tot = 1.0 - float(cekirdek_agirlik) if core_rows else 1.0
        for k, v in wu.items():
            weights[k] = (v / s) * uydu_tot

    # Normalize
    tw = sum(weights.values()) or 1.0
    weights = {k: v / tw for k, v in weights.items()}

    return {
        "secili": secili,
        "weights": weights,
        "method": f"cekirdek_uydu:{yontem}",
        "n_cekirdek": len(core_rows),
        "n_uydu": len(uydu_rows),
        "cekirdek_agirlik_hedef": cekirdek_agirlik,
        "n": len(secili),
        "ozet": [
            f"{r.get('kod')}: {weights.get(str(r.get('kod')), 0)*100:.1f}% [{rol}]"
            for r, rol in zip(core_rows + uydu_rows, ["çekirdek"] * len(core_rows) + ["uydu"] * len(uydu_rows))
        ],
    }


def akilli_karisik_portfoy(
    analiz: "FonAnaliz",
    df: pd.DataFrame,
    n_muhafazakar: int = 3,
    n_dengeli: int = 4,
    n_agresif: int = 3,
    yontem: str = "max_sharpe",
    max_w: float = 0.20,
) -> dict:
    """
    Muhafazakâr + Dengeli + Agresif profillerinden fon seçip
    tek karışık portföy üretir.
    """
    if df is None or df.empty:
        return {"error": "Aday yok"}

    def _filtre(preset_key: str) -> pd.DataFrame:
        pr = AKILLI_FILTRE_PRESET.get(preset_key) or {}
        w = df.copy()
        if "secim_skor" not in w.columns:
            w["secim_skor"] = w.apply(rapor_uyumlu_skor, axis=1)
        mrs = pr.get("max_risk_skor")
        if mrs is not None and "risk_skor" in w.columns:
            w = w[pd.to_numeric(w["risk_skor"], errors="coerce").fillna(999) <= float(mrs)]
        ml = pr.get("min_liq")
        if ml is not None and "likidite_skor" in w.columns:
            w = w[pd.to_numeric(w["likidite_skor"], errors="coerce").fillna(0) >= float(ml)]
        mk = pr.get("min_kazanma")
        if mk is not None and "kazanma_orani" in w.columns:
            w = w[pd.to_numeric(w["kazanma_orani"], errors="coerce").fillna(0) >= float(mk)]
        mek = pr.get("min_en_kotu")
        if mek is not None and "en_kotu_gun" in w.columns:
            w = w[pd.to_numeric(w["en_kotu_gun"], errors="coerce").fillna(-999) >= float(mek)]
        msh = pr.get("min_sharpe")
        if msh is not None and "sharpe" in w.columns:
            w = w[pd.to_numeric(w["sharpe"], errors="coerce").fillna(-999) >= float(msh)]
        mdd = pr.get("max_dd")
        if mdd is not None and "max_drawdown" in w.columns:
            w = w[pd.to_numeric(w["max_drawdown"], errors="coerce").fillna(-999) >= float(mdd)]
        mvol = pr.get("max_vol")
        if mvol is not None and "volatilite" in w.columns:
            w = w[pd.to_numeric(w["volatilite"], errors="coerce").fillna(999) <= float(mvol)]
        if pr.get("temiz_bayrak") and "risk_bayrak" in w.columns:
            rb = w["risk_bayrak"].astype(str).str.lower()
            w = w[~rb.str.contains("aşırı|akran-dışı|sıçrama|yüksek fiyat", na=False)]
        if "secim_skor" in w.columns:
            w = w.sort_values("secim_skor", ascending=False)
        elif "sharpe" in w.columns:
            w = w.sort_values("sharpe", ascending=False)
        return w

    buckets = [
        ("Muhafazakâr", n_muhafazakar),
        ("Dengeli", n_dengeli),
        ("Agresif", n_agresif),
    ]
    secilen = []
    alinan = set()
    profil_map = {}
    for profil, n in buckets:
        if n <= 0:
            continue
        aday = _filtre(profil)
        eklendi = 0
        for _, row in aday.iterrows():
            if eklendi >= n:
                break
            kod = str(row.get("kod"))
            if kod in alinan:
                continue
            r = row.copy()
            r["profil_kaynak"] = profil
            secilen.append(r)
            alinan.add(kod)
            profil_map[kod] = profil
            eklendi += 1
        # yedek: filtre boşsa genel listeden doldur
        if eklendi < n:
            for _, row in df.iterrows():
                if eklendi >= n:
                    break
                kod = str(row.get("kod"))
                if kod in alinan:
                    continue
                r = row.copy()
                r["profil_kaynak"] = profil + " (yedek)"
                secilen.append(r)
                alinan.add(kod)
                profil_map[kod] = profil
                eklendi += 1

    if not secilen:
        return {"error": "Hiçbir profilden fon seçilemedi — filtreleri gevşetin"}

    secili = pd.DataFrame(secilen).reset_index(drop=True)
    if "secim_skor" not in secili.columns:
        secili["secim_skor"] = secili.apply(rapor_uyumlu_skor, axis=1)
    secili["rapor_skor"] = secili["secim_skor"]
    secili["secim_gerekce"] = secili.apply(
        lambda r: f"{r.get('profil_kaynak', '')} · {_secim_gerekce(r)}", axis=1
    )

    opt = siralamadan_agirlik_hesapla(analiz, secili, yontem=yontem, max_w=max_w)
    if opt.get("error"):
        kodlar = secili["kod"].astype(str).tolist()
        opt = {
            "method": "equal",
            "weights": {k: 1.0 / len(kodlar) for k in kodlar},
            "risk_contrib_pct": {k: 100.0 / len(kodlar) for k in kodlar},
            "port_mu": None, "port_vol": None, "port_sharpe": None,
        }

    ozet = []
    wmap = opt.get("weights") or {}
    for _, r in secili.iterrows():
        k = str(r["kod"])
        ozet.append(
            f"{k} ({r.get('profil_kaynak')}): %{wmap.get(k, 0)*100:.1f} — {r.get('secim_gerekce', '')}"
        )

    return {
        "secili": secili,
        "weights": wmap,
        "risk_contrib_pct": opt.get("risk_contrib_pct") or {},
        "method": opt.get("method"),
        "port_mu": opt.get("port_mu"),
        "port_vol": opt.get("port_vol"),
        "port_sharpe": opt.get("port_sharpe"),
        "profil_map": profil_map,
        "n": len(secili),
        "ozet": ozet,
        "mod": "karisik",
    }


def menu_detay(analiz: FonAnaliz):
    print("\n=== DETAY ===")
    kod = input("Kod [AAL]: ").strip().upper() or "AAL"
    d = analiz.fon_detay(kod)
    m, o, p = d["metrikler"], d["ozet"], d["profil"]
    ad = o.get("fonUnvan") or p.get("fonUnvan") or kod
    print(f"\n{ad}")
    print(f"  TEFAS: {p.get('tefasDurum')} | Risk: {p.get('riskDegeri')} | Faiz: {faiz_durum(p, ad)}")
    print(f"  Kategori: {o.get('fonKategori')} | Saat: {p.get('basIsSaat')}-{p.get('sonIsSaat')}")
    for k in ["fiyat", "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y", "getiri_3y",
              "reel_1y", "vs_mevduat", "vs_bist100", "vs_altin", "volatilite", "max_drawdown", "sharpe", "calmar"]:
        print(f"  {k:14s}: {m.get(k)}")

    # Detaylı HTML risk raporu
    if not HAS_MPL:
        print("\n  (matplotlib yok — HTML rapor için: pip install matplotlib)")
        return
    if input("\nDetaylı risk/getiri raporu (HTML) oluşturulsun mu? (e/h) [e]: ").strip().lower() == "h":
        return

    print("\nRapor türü:")
    print("  1) Kendi Türkçe raporumuz  — grafik başlıkları dahil %100 Türkçe, quantstats gerekmez (önerilen)")
    if HAS_QUANTSTATS:
        print("  2) Quantstats             — daha kapsamlı istatistik seti, ancak grafik başlıkları İngilizce kalır")
    tur = input("Seçim [1]: ").strip() or "1"
    if tur == "2" and not HAS_QUANTSTATS:
        print("  quantstats yüklü değil, Türkçe rapor ile devam ediliyor.")
        tur = "1"

    bm = None
    if input("BIST100 ile karşılaştır? (e/h) [e]: ").strip().lower() != "h":
        print("  BIST100 getirisi çekiliyor...")
        bm = yahoo_daily_returns("XU100.IS")
    # 3Y fiyat tercih et
    fd = d.get("fiyat_df")
    if fd is None or len(fd) < 100:
        fd = analiz.api.fiyat(kod, periyod=36)

    if tur == "2":
        quantstats_rapor(fd, kod, ad=ad, benchmark_returns=bm, ekstra_metrikler=m)
    else:
        turkce_risk_raporu(fd, kod, ad=ad, rf=analiz.rf, benchmark_returns=bm,
                            benchmark_ad="BIST100", ekstra_metrikler=m)


def menu_favoriler(analiz: FonAnaliz):
    print("\n=== FAVORİLER ===")
    print("  1) Listele/güncelle  2) Ekle  3) Çıkar  4) Alarm kontrol")
    sec = input("Seçim: ").strip()
    favs = load_favs()
    if sec == "1":
        print("Favoriler:", favs or "(boş)")
        if favs:
            df = analiz.favorileri_guncelle()
            if not df.empty:
                cols = [c for c in ["kod", "ad", "getiri_1y", "reel_1y", "sharpe", "max_drawdown"] if c in df.columns]
                print(df[cols].to_string(index=False))
                if input("Excel? (e/h) [h]: ").strip().lower() == "e":
                    excel_kaydet(df, f"favoriler_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
    elif sec == "2":
        kod = input("Kod: ").strip().upper()
        if kod:
            favs.append(kod)
            save_favs(favs)
            print("Eklendi:", load_favs())
    elif sec == "3":
        kod = input("Kod: ").strip().upper()
        save_favs([f for f in favs if f != kod])
        print("Kaldı:", load_favs())
    elif sec == "4":
        if favs:
            analiz.favorileri_guncelle()
            print("Log:", ALERT_LOG)
        else:
            print("Favori yok.")




def detay_rapor_notu(m: dict, risk_bayrak: str = "") -> str:
    """
    Menü 4/1 rapor kartlarındaki değerleri sıralama notuna yazar.
    Örnek: Sharpe 2.1 | Sortino 2.5 | MaxDD -0.1 | Vol 1.7 | CAGR 48 | VaR -0.2 | Kazanma %62
    """
    parts = []
    def add(label, key, fmt="{:.1f}", suffix=""):
        v = m.get(key)
        if v is None:
            return
        try:
            parts.append(f"{label} {fmt.format(float(v))}{suffix}")
        except (TypeError, ValueError):
            pass
    add("Sharpe", "sharpe", "{:.2f}")
    add("Sortino", "sortino", "{:.2f}")
    add("Calmar", "calmar", "{:.2f}")
    add("MaxDD", "max_drawdown", "{:.1f}", "%")
    add("Vol", "volatilite", "{:.1f}", "%")
    add("CAGR", "cagr", "{:.1f}", "%")
    add("Kazanma", "kazanma_orani", "{:.0f}", "%")
    add("VaR95", "var95", "{:.1f}", "%")
    add("cVaR95", "cvar95", "{:.1f}", "%")
    add("EnİyiGün", "en_iyi_gun", "{:.1f}", "%")
    add("EnKötüGün", "en_kotu_gun", "{:.1f}", "%")
    note = " | ".join(parts)
    if risk_bayrak:
        note = birlestir_risk_bayrak(note, risk_bayrak)
    return note


def rapor_uyumlu_skor(row) -> float:
    """
    Menü 4 → seçenek 1 (Türkçe HTML rapor) kart eşikleriyle uyumlu skor.
    Yüksek = daha tercih edilir.
    """
    def _num(x, default=None):
        try:
            v = float(x)
            if v != v or abs(v) == float("inf"):
                return default
            return v
        except (TypeError, ValueError):
            return default

    skor = 0.0
    # Sharpe: rapor iyi>=1.0, orta>=0
    sh = _num(row.get("sharpe"))
    if sh is not None:
        if sh >= 1.0:
            skor += 25
        elif sh >= 0.5:
            skor += 15
        elif sh >= 0:
            skor += 8
        else:
            skor -= 10

    # Sortino: rapor iyi>=1.5
    so = _num(row.get("sortino"))
    if so is not None:
        if so >= 1.5:
            skor += 15
        elif so >= 0.8:
            skor += 10
        elif so >= 0:
            skor += 4

    # Max DD: rapor iyi ~ -10, orta ~ -20 (daha az negatif daha iyi)
    mdd = _num(row.get("max_drawdown"))
    if mdd is not None:
        if mdd >= -8:
            skor += 20
        elif mdd >= -15:
            skor += 12
        elif mdd >= -25:
            skor += 5
        else:
            skor -= 15

    # Vol: rapor iyi<=12, orta<=25
    vol = _num(row.get("volatilite"))
    if vol is not None:
        if vol <= 12:
            skor += 12
        elif vol <= 25:
            skor += 6
        elif vol > 40:
            skor -= 10

    # Anomali risk
    rs = _num(row.get("risk_skor"), 0) or 0
    if rs < 10:
        skor += 10
    elif rs < 18:
        skor += 4
    else:
        skor -= 12

    # Likidite (0-100 yüksek=iyi)
    liq = _num(row.get("likidite_skor"))
    if liq is not None:
        if liq >= 70:
            skor += 12
        elif liq >= 45:
            skor += 6
        else:
            skor -= 10
    seviye = str(row.get("likidite_seviye") or "")
    if seviye == "düşük":
        skor -= 5

    # Getiri / benchmark (rapor değerlendirme kutusu)
    g1y = _num(row.get("getiri_1y"))
    if g1y is not None:
        if g1y >= 40:
            skor += 12
        elif g1y >= 20:
            skor += 8
        elif g1y > 0:
            skor += 3

    cagr = _num(row.get("cagr"))
    if cagr is not None:
        if cagr >= 15:
            skor += 10
        elif cagr >= 5:
            skor += 5
        elif cagr < 0:
            skor -= 6

    kaz = _num(row.get("kazanma_orani"))
    if kaz is not None:
        if kaz >= 55:
            skor += 6
        elif kaz < 48:
            skor -= 4

    var95 = _num(row.get("var95"))
    if var95 is not None and var95 < -3:
        skor -= 5

    reel = _num(row.get("reel_1y"))
    if reel is not None:
        skor += 8 if reel > 0 else -5

    vs_m = _num(row.get("vs_mevduat"))
    if vs_m is not None:
        skor += 8 if vs_m > 0 else -4

    # Sıralama puanı (varsa)
    puan = _num(row.get("puan") or row.get("skor"))
    if puan is not None:
        skor += min(puan, 100) * 0.15

    # Aşırı günlük hareket bayrağı
    bayrak = str(row.get("risk_bayrak") or "")
    if "aşırı" in bayrak.lower() or "akran-dışı" in bayrak.lower():
        skor -= 8

    return round(skor, 2)


def siralamadan_fon_sec(
    df: pd.DataFrame,
    max_fon: int = 6,
    max_risk_skor: float = 18.0,
    max_dd: float = -25.0,
    max_vol: float = 35.0,
    min_puan: float = None,
    min_likidite: float = 45.0,
) -> pd.DataFrame:
    """
    Türkçe risk raporu (menü 4 / seçenek 1) metrikleriyle fon seç.

    Sert filtreler:
      • risk_skor < max_risk_skor
      • max_drawdown >= max_dd
      • volatilite <= max_vol
    Sıralama: rapor_uyumlu_skor (Sharpe, Sortino, MaxDD, vol, reel, vs mevduat…)
    """
    if df is None or df.empty:
        return pd.DataFrame()
    w = df.copy()
    if "risk_skor" in w.columns:
        w = w[pd.to_numeric(w["risk_skor"], errors="coerce").fillna(0) < max_risk_skor]
    if "max_drawdown" in w.columns:
        mdd = pd.to_numeric(w["max_drawdown"], errors="coerce")
        w = w[(mdd.isna()) | (mdd >= max_dd)]
    if "volatilite" in w.columns:
        vol = pd.to_numeric(w["volatilite"], errors="coerce")
        w = w[(vol.isna()) | (vol <= max_vol)]
    if min_puan is not None and "puan" in w.columns:
        w = w[pd.to_numeric(w["puan"], errors="coerce").fillna(0) >= min_puan]
    if min_likidite is not None and "likidite_skor" in w.columns:
        w = w[pd.to_numeric(w["likidite_skor"], errors="coerce").fillna(0) >= min_likidite]
    elif min_likidite is not None and "likidite_seviye" in w.columns:
        w = w[w["likidite_seviye"].isin(["yüksek", "orta", "yuksek"])]

    if w.empty:
        return w.reset_index(drop=True)

    w = w.copy()
    w["rapor_skor"] = w.apply(rapor_uyumlu_skor, axis=1)
    w = w.sort_values(["rapor_skor", "puan"] if "puan" in w.columns else ["rapor_skor"],
                      ascending=[False, False] if "puan" in w.columns else [False])
    return w.head(max_fon).reset_index(drop=True)


def siralamadan_agirlik_hesapla(
    analiz: "FonAnaliz",
    secili: pd.DataFrame,
    yontem: str = "max_sharpe",
    max_w: float = 0.35,
) -> dict:
    """
    Seçilen fonlara ağırlık: max_sharpe | score | equal | risk_parity
    """
    if secili is None or secili.empty:
        return {"error": "Seçili fon yok"}
    series = {}
    scores = {}
    for _, row in secili.iterrows():
        kod = str(row.get("kod", "")).upper()
        fd = analiz.api.fiyat(kod, periyod=12)
        if fd.empty or len(fd) < 40:
            continue
        series[kod] = fd.set_index("tarih")["fiyat"]
        scores[kod] = float(
            row.get("rapor_skor") or row.get("puan") or row.get("skor") or row.get("sharpe") or 1
        )
    if len(series) < 1:
        return {"error": "Fiyat serisi alınamadı"}
    if len(series) == 1:
        k = list(series.keys())[0]
        return {
            "method": "single",
            "weights": {k: 1.0},
            "risk_contrib_pct": {k: 100.0},
            "port_mu": None, "port_vol": None, "port_sharpe": None,
            "kodlar": [k],
        }
    return run_optimizer(yontem, series, rf=analiz.rf, max_w=max_w, scores=scores)


def turkce_siralama_raporlari(
    analiz: "FonAnaliz",
    df: pd.DataFrame,
    top_n: int = 5,
    with_benchmark: bool = True,
) -> list:
    """Üst sıralama fonları için %100 Türkçe HTML risk raporu."""
    if not HAS_MPL:
        print("  matplotlib yok — pip install matplotlib")
        return []
    if df is None or df.empty:
        return []
    work = df.head(top_n)
    bm = yahoo_daily_returns("XU100.IS") if with_benchmark else None
    paths = []
    for _, row in work.iterrows():
        kod = str(row.get("kod", "")).upper()
        ad = str(row.get("ad") or kod)[:60]
        # sıralama notlarını ekstra metrik olarak geç
        ek = {
            "getiri_1a": row.get("getiri_1a"),
            "getiri_3a": row.get("getiri_3a"),
            "getiri_6a": row.get("getiri_6a"),
            "getiri_1y": row.get("getiri_1y"),
            "getiri_3y": row.get("getiri_3y"),
            "reel_1y": row.get("reel_1y"),
            "vs_mevduat": row.get("vs_mevduat"),
            "vs_bist100": row.get("vs_bist100"),
            "vs_altin": row.get("vs_altin"),
            "volatilite": row.get("volatilite"),
            "max_drawdown": row.get("max_drawdown"),
            "sharpe": row.get("sharpe"),
            "sortino": row.get("sortino"),
            "calmar": row.get("calmar"),
            "puan": row.get("puan"),
            "sira": row.get("sira"),
            "risk_skor": row.get("risk_skor"),
            "risk_bayrak": row.get("risk_bayrak"),
            "yatirim": row.get("yatirim"),
            "getiri_z": row.get("getiri_z"),
            "cagr": row.get("cagr"),
            "kazanma_orani": row.get("kazanma_orani"),
            "var95": row.get("var95"),
            "cvar95": row.get("cvar95"),
            "en_iyi_gun": row.get("en_iyi_gun"),
            "en_kotu_gun": row.get("en_kotu_gun"),
            "sortino": row.get("sortino"),
            "calmar": row.get("calmar"),
        }
        dagilim_row = {}
        for k in list(ASSET_MAP.keys()) + ["diger"]:
            v = row.get(f"d_{k}")
            if v is not None:
                ek[f"d_{k}"] = v
                try:
                    if float(v) > 0.05:
                        dagilim_row[k] = float(v)
                except (TypeError, ValueError):
                    pass
        print(f"\n  [{kod}] Türkçe HTML rapor...")
        if row.get("risk_bayrak"):
            print(f"    Not: {str(row.get('risk_bayrak'))[:90]}")
        fd = analiz.api.fiyat(kod, periyod=36)
        path = turkce_risk_raporu(
            fd, kod, ad=ad, rf=analiz.rf,
            benchmark_returns=bm, benchmark_ad="BIST100",
            ekstra_metrikler=ek, dagilim=dagilim_row or None,
        )
        if path:
            paths.append(path)
    if paths:
        print(f"\n  Toplam {len(paths)} Türkçe HTML rapor yazıldı.")
    return paths


def menu_tum_siralama(analiz: FonAnaliz):
    print("\n=== TÜM FONLAR — DÖNEM SIRALAMASI ===")
    print("Dağılım filtresi YOK. TEFAS açık fonlar taranır.")
    print("Dönemler: 1A · 3A · 6A · 1Y · 3Y")
    print("Varsayılan: son 1 yılda HER dönem pozitif (1A,3A,6A,1Y > 0)")
    print("Puan = dönem getirileri + Sharpe ağırlıklı skor\n")

    tur_filtre = _tur_filtre_sec()
    if tur_filtre:
        print("   (Tür filtresi, taramayı da hızlandırır — sadece seçilen türler taranır)")
    skor_sistemi = _skor_sistemi_sec()
    skor_agirlik = _gelismis_profil_sec() if skor_sistemi == "gelismis" else None

    poz = input("Son 1Y her dönem pozitif zorunlu? (e/h) [e]: ").strip().lower() != "h"
    poz3 = input("3Y de pozitif olsun mu? (e/h) [h]: ").strip().lower() == "e"
    max_liste = int(_sayi("Kaç fon listelensin (üst sıra)", 40, 10, 200) or 40)
    lim = _sayi("En fazla kaç AÇIK fon taransın? (Enter=hepsi, test için 80)", None, 20, 5000)
    max_tara = int(lim) if lim else None

    print("\nUyarı: Tüm fonlar uzun sürer (10–40 dk). Cache sonraki koşuları hızlandırır.")
    if input("Başlat? (e/h) [e]: ").strip().lower() == "h":
        return

    df = analiz.tum_fon_siralama(
        sadece_pozitif_1y=poz,
        pozitif_3y=poz3,
        max_liste=max_liste,
        max_tara=max_tara,
        tur_filtre=tur_filtre, skor_sistemi=skor_sistemi, skor_agirlik=skor_agirlik,
    )
    if df.empty:
        print("Sonuç yok.")
        return

    cols = [c for c in [
        "sira", "puan", "kod", "ad", "yatirim",
        "getiri_1a", "getiri_3a", "getiri_6a", "getiri_1y", "getiri_3y",
        "reel_1y", "vs_mevduat", "vs_bist100",
        "sharpe", "sortino", "cagr", "kazanma_orani", "var95", "cvar95",
        "volatilite", "max_drawdown", "risk_degeri",
        "risk_skor", "likidite_skor", "likidite_seviye", "likit_varlik_pct", "risk_bayrak", "getiri_z", "baskin_tur", "akran_grubu", "buyukluk",
    ] if c in df.columns]
    pd.set_option("display.max_colwidth", 48)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:.1f}".format)
    print("\n--- En iyi fonlar (puan sırası) ---\n")
    print(df[cols].to_string(index=False))
    if skor_sistemi == "gelismis":
        pct_cols = [c for c in ["kod", "pct_getiri", "pct_risk_ayarli", "pct_maxdd",
                                 "pct_risk_anomali", "pct_maliyet", "pct_akran_sapma"] if c in df.columns]
        print("\n--- Bileşen percentile'ları (0-100, akran grubu içinde) ---\n")
        print(df[pct_cols].to_string(index=False))

    # Portföy dağılımı tablosu
    dag_cols = [c for c in [
        "sira", "kod", "yatirim",
        "d_hisse", "d_tahvil", "d_repo", "d_mevduat", "d_altin", "d_doviz", "d_fon", "d_diger",
        "buyukluk", "yatirimci",
    ] if c in df.columns]
    if dag_cols:
        print("\n--- Portföy yatırımı (nelere yatırım) ---\n")
        print(df[dag_cols].to_string(index=False))

    if analiz.bench:
        print("\nBenchmark: BIST100=", analiz.bench.get("bist100_1y"),
              "TÜFE=", analiz.bench.get("tufe_yillik"),
              "Mevduat=", analiz.bench.get("mevduat_yillik"))

    if input("\nGrafik rapor (PNG)? (e/h) [e]: ").strip().lower() != "h":
        grafik_rapor(df, baslik=f"Dönem sıralaması | {datetime.now().strftime('%d.%m.%Y')}")

    if input("Excel? (e/h) [e]: ").strip().lower() != "h":
        excel_kaydet(
            df,
            f"tefas_donem_sira_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            {
                "mod": "tum_fon_siralama",
                "pozitif_1y_donemler": poz,
                "pozitif_3y": poz3,
                "tip": analiz.api.fon_tipi,
                "skor_sistemi": skor_sistemi,
                "tur_filtre": tur_filtre or "hepsi",
            },
        )

    # ── Menü 4 / seçenek 1 ile aynı Türkçe HTML + rapor skoruna göre öneri ──
    print("\n" + "=" * 50)
    print("  Rapor = Menü 4 → 1) Kendi Türkçe risk/getiri raporumuz")
    print("  Öneri = Aynı metrikler (Sharpe, Sortino, MaxDD, vol, reel…)")
    print("=" * 50)

    if HAS_MPL and input("\nÜst fonlar için Türkçe HTML rapor (menü 4/1)? (e/h) [e]: ").strip().lower() != "h":
        n_html = int(_sayi("Kaç üst fon için HTML?", 5, 1, 15) or 5)
        bm_ok = input("BIST100 karşılaştırması? (e/h) [e]: ").strip().lower() != "h"
        turkce_siralama_raporlari(analiz, df, top_n=n_html, with_benchmark=bm_ok)

    if input("\nTürkçe rapor metriklerine göre fon öner? (e/h) [e]: ").strip().lower() != "h":
        print("\nSert filtreler (rapor eşikleri — Enter=varsayılan):")
        print("  risk_skor < 18 | MaxDD >= -25 | vol <= %35")
        print("  Sıralama: rapor_uyumlu_skor (Sharpe/Sortino/MaxDD/vol/reel/vs mevduat)")
        n_port = int(_sayi("Kaç fon önerilsin?", 5, 2, 12) or 5)
        max_rs = _sayi("Max risk_skor", 18, 0, 100)
        if max_rs is None:
            max_rs = 18.0
        max_dd = _sayi("Max DD eşiği (örn -25)", -25.0)
        if max_dd is None:
            max_dd = -25.0
        max_vol = _sayi("Max vol %", 35.0)
        if max_vol is None:
            max_vol = 35.0
        min_liq = _sayi("Min likidite skor (0-100, yüksek=iyi)", 45.0, 0, 100)
        if min_liq is None:
            min_liq = 45.0
        max_w = _sayi("Tek fon max ağırlık", 0.35, 0.1, 1.0) or 0.35

        print("\nAğırlık yöntemi:")
        print("  1) Max Sharpe (önerilen — raporla uyumlu)")
        print("  2) Risk Parity")
        print("  3) Rapor skoru orantılı")
        print("  4) Eşit ağırlık")
        ym = input("Seçim [1]: ").strip() or "1"
        yontem = {"1": "max_sharpe", "2": "risk_parity", "3": "score", "4": "equal"}.get(ym, "max_sharpe")

        secili = siralamadan_fon_sec(
            df, max_fon=n_port, max_risk_skor=max_rs, max_dd=max_dd, max_vol=max_vol,
            min_likidite=min_liq,
        )
        if secili.empty:
            print("Kriterlere uyan fon kalmadı — eşikleri gevşet.")
        else:
            print("\n--- Önerilen fonlar (rapor_uyumlu_skor) ---")
            scols = [c for c in ["sira", "rapor_skor", "puan", "kod", "ad", "getiri_1y",
                                  "sharpe", "sortino", "max_drawdown", "volatilite",
                                  "likidite_skor", "likidite_seviye", "likit_varlik_pct",
                                  "risk_skor", "risk_bayrak", "yatirim"]
                     if c in secili.columns]
            print(secili[scols].to_string(index=False))

            out = siralamadan_agirlik_hesapla(analiz, secili, yontem=yontem, max_w=max_w)
            if out.get("error"):
                print("Ağırlık:", out["error"])
            else:
                print(f"\n--- Ağırlıklar ({out.get('method')}) ---")
                for k, v in sorted(out.get("weights", {}).items(), key=lambda x: -x[1]):
                    if v >= 0.001:
                        rc = out.get("risk_contrib_pct", {}).get(k, 0)
                        print(f"  {k}: %{v*100:.1f}  (risk katkısı %{rc:.1f})")
                if out.get("port_sharpe") is not None:
                    print(f"  Beklenen μ %{out.get('port_mu')} | vol %{out.get('port_vol')} | Sharpe {out.get('port_sharpe')}")

                # Excel
                if input("\nPortföy Excel? (e/h) [e]: ").strip().lower() != "h":
                    rows = []
                    for k, v in out.get("weights", {}).items():
                        r = secili[secili["kod"] == k]
                        ad = r["ad"].iloc[0] if len(r) and "ad" in r.columns else k
                        rows.append({
                            "kod": k, "ad": ad, "agirlik_%": round(v * 100, 2),
                            "risk_katkisi_%": out.get("risk_contrib_pct", {}).get(k),
                            "puan": float(r["puan"].iloc[0]) if len(r) and "puan" in r.columns else None,
                            "risk_skor": float(r["risk_skor"].iloc[0]) if len(r) and "risk_skor" in r.columns else None,
                            "risk_bayrak": str(r["risk_bayrak"].iloc[0]) if len(r) and "risk_bayrak" in r.columns else "",
                            "yatirim": str(r["yatirim"].iloc[0]) if len(r) and "yatirim" in r.columns else "",
                        })
                    edf = pd.DataFrame(rows).sort_values("agirlik_%", ascending=False)
                    adx = f"tefas_portfoy_oneri_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
                    try:
                        edf.to_excel(adx, index=False)
                        print(f"✓ {adx}")
                    except Exception as e:
                        edf.to_csv(adx.replace(".xlsx", ".csv"), index=False, encoding="utf-8-sig")
                        print(f"CSV kaydedildi ({e})")

                # Seçilenler için Türkçe HTML
                if HAS_MPL and input("Seçilen fonlar için Türkçe HTML? (e/h) [e]: ").strip().lower() != "h":
                    turkce_siralama_raporlari(analiz, secili, top_n=len(secili), with_benchmark=True)


def menu_optimizasyon(analiz: FonAnaliz):
    print("\n=== PORTFÖY OPTİMİZASYONU ===")
    print("Yöntemler:")
    print("  1) Max Sharpe")
    print("  2) Min Variance (min vol)")
    print("  3) Risk Parity")
    print("  4) Min CVaR (%5 kuyruk)")
    print("  5) Eşit ağırlık")
    print("  6) Skor orantılı (puan/skor)")
    print("  7) Hepsi (karşılaştır)")
    ym = input("Yöntem [1]: ").strip() or "1"
    method_map = {
        "1": "max_sharpe", "2": "min_var", "3": "risk_parity",
        "4": "cvar", "5": "equal", "6": "score", "7": "all",
    }
    method = method_map.get(ym, "max_sharpe")

    s = input("Fon kodları (virgülle) [AAL,AFO,TTE]: ").strip() or "AAL,AFO,TTE"
    kodlar = [k.strip().upper() for k in s.split(",") if k.strip()]
    if len(kodlar) < 2:
        print("En az 2 fon gerekli.")
        return

    max_w = _sayi("Tek fon max ağırlık (0.40=%40)", 0.40, 0.05, 1.0) or 0.40
    max_risk = _sayi("Max risk_skor (Enter=filtre yok)", None)

    print("Fiyat serileri çekiliyor...")
    series = {}
    scores = {}
    for kod in kodlar:
        p = analiz.api.profil(kod)
        ad = p.get("fonUnvan") or kod
        ok, neden = fon_yatirima_uygun_mu(ad, p.get("tefasDurum") or "")
        if not ok:
            print(f"  ✗ {kod} elendi: {neden}")
            continue
        fd = analiz.api.fiyat(kod, periyod=12)
        if fd.empty:
            print(f"  ✗ {kod} fiyat yok")
            continue
        an = anomaly_from_prices(fd)
        o = analiz.api.ozet(kod)
        m = full_metrics(fd, analiz.rf)
        liq = likidite_risk(o, m, profil=p, anomaly=an)
        risk_skor = (an.get("anomaly_skor") or 0) + (liq.get("likidite_skor_risk") or 0)
        if max_risk is not None and risk_skor > max_risk:
            print(f"  ✗ {kod} risk_skor={risk_skor} > {max_risk}")
            continue
        series[kod] = fd.set_index("tarih")["fiyat"]
        scores[kod] = float(m.get("sharpe") or 0) * 10 + float(m.get("getiri_1y") or 0) - risk_skor
        print(f"  ✓ {kod}  risk_skor={risk_skor:.1f}  sharpe={m.get('sharpe')}")

    if len(series) < 2:
        print("Optimizasyon için en az 2 uygun fon kaldı.")
        return

    methods = (
        ["max_sharpe", "min_var", "risk_parity", "cvar", "equal", "score"]
        if method == "all"
        else [method]
    )
    rows = []
    best = None
    for met in methods:
        print(f"\n--- {met} ---")
        out = run_optimizer(
            met, series, rf=analiz.rf, max_w=max_w, scores=scores, alpha=0.05,
        )
        if out.get("error"):
            print(" ", out["error"])
            continue
        print(f"  Beklenen getiri (yıllık): %{out['port_mu']}")
        print(f"  Volatilite (yıllık):      %{out['port_vol']}")
        print(f"  Sharpe:                   {out['port_sharpe']}")
        print("  Ağırlıklar:")
        for k, v in sorted(out["weights"].items(), key=lambda x: -x[1]):
            if v >= 0.001:
                print(f"    {k}: %{v*100:.1f}  (risk katkısı %{out['risk_contrib_pct'].get(k, 0):.1f})")
        rows.append({
            "yontem": met,
            "mu_%": out["port_mu"],
            "vol_%": out["port_vol"],
            "sharpe": out["port_sharpe"],
            **{f"w_{k}": v for k, v in out["weights"].items()},
        })
        # birleşik tarihsel metrik
        hist = portfolio_metrics(out["weights"], series, rf=analiz.rf)
        if hist:
            print(f"  Tarihsel 1Y: %{hist.get('getiri_1y')}  MaxDD: %{hist.get('max_drawdown')}  Sharpe: {hist.get('sharpe')}")
            rows[-1]["hist_1y"] = hist.get("getiri_1y")
            rows[-1]["hist_mdd"] = hist.get("max_drawdown")
            rows[-1]["hist_sharpe"] = hist.get("sharpe")
        if best is None or (out.get("port_sharpe") or -999) > (best.get("port_sharpe") or -999):
            best = out

    if rows and input("\nExcel kaydet? (e/h) [e]: ").strip().lower() != "h":
        edf = pd.DataFrame(rows)
        ad = f"tefas_opt_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        try:
            edf.to_excel(ad, index=False)
            print(f"✓ Excel: {ad}")
        except Exception as e:
            csv_ad = ad.replace(".xlsx", ".csv")
            edf.to_csv(csv_ad, index=False, encoding="utf-8-sig")
            print(f"Excel yazılamadı ({e}). CSV: {csv_ad}")

    if best and HAS_MPL and input("Ağırlık grafiği? (e/h) [e]: ").strip().lower() != "h":
        try:
            fig, ax = plt.subplots(figsize=(8, 5))
            items = sorted(best["weights"].items(), key=lambda x: -x[1])
            labels = [k for k, v in items if v >= 0.005]
            vals = [v for k, v in items if v >= 0.005]
            ax.pie(vals, labels=labels, autopct="%1.1f%%", startangle=90)
            ax.set_title(f"Optimizasyon: {best['method']}")
            path = ROOT / f"opt_agirlik_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            fig.savefig(path, dpi=140, bbox_inches="tight")
            plt.close(fig)
            print(f"  ✓ {path}")
        except Exception as e:
            print("  Grafik hatası:", e)


def menu_ayarlar(cfg: dict) -> dict:
    print("\n=== AYARLAR ===")
    print(f"  Risksiz faiz: {cfg.get('risk_free')} | TÜFE: {cfg.get('tufe_yillik')} | Mevduat: {cfg.get('mevduat_yillik')}")
    print(f"  Alarmlar: {cfg.get('alarmlar')}")
    if input("Güncelle? (e/h) [h]: ").strip().lower() != "e":
        return cfg
    cfg["risk_free"] = _sayi("Risksiz faiz (0.40=%40)", cfg.get("risk_free"), 0, 1) or cfg.get("risk_free")
    cfg["tufe_yillik"] = _sayi("TÜFE yıllık %", cfg.get("tufe_yillik"), 0, 200) or cfg.get("tufe_yillik")
    cfg["mevduat_yillik"] = _sayi("Mevduat yıllık %", cfg.get("mevduat_yillik"), 0, 200) or cfg.get("mevduat_yillik")
    al = cfg.setdefault("alarmlar", {})
    al["min_getiri_1y"] = _sayi("Alarm min 1Y %", al.get("min_getiri_1y"))
    al["min_sharpe"] = _sayi("Alarm min Sharpe", al.get("min_sharpe"))
    al["max_drawdown"] = _sayi("Alarm max DD %", al.get("max_drawdown"))
    al["max_volatilite"] = _sayi("Alarm max vol %", al.get("max_volatilite"))
    save_config(cfg)
    print("Kaydedildi:", CONFIG_PATH)
    return cfg


def main():
    print("=" * 60)
    print("  TEFAS FON ANALİZ — Tek Program (tam özellik)")
    print("  Benchmark · filtre · favori · alarm · Excel")
    print("=" * 60)
    cfg = load_config()
    tip = input("\nFon tipi YAT/EMK/BYF [YAT]: ").strip().upper() or "YAT"
    if tip not in ("YAT", "EMK", "BYF"):
        tip = "YAT"
    analiz = FonAnaliz(fon_tipi=tip, cfg=cfg)

    while True:
        print("\n" + "-" * 42)
        print("  1) Fon tarama (dağılım + filtre)")
        print("  2) Karşılaştırma")
        print("  3) Portföy")
        print("  4) Fon detay")
        print("  5) Favoriler")
        print("  6) Ayarlar (TÜFE/mevduat/alarm)")
        print("  7) Tüm fonlar — dönem sıralaması (1A/3A/6A/1Y/3Y)")
        print("  8) Portföy optimizasyonu (Sharpe/Vol/Parity/CVaR)")
        print("  0) Çıkış")
        print("-" * 42)
        sec = input("Seçim: ").strip()
        try:
            if sec == "1":
                menu_tarama(analiz)
            elif sec == "2":
                menu_karsilastir(analiz)
            elif sec == "3":
                menu_portfoy(analiz)
            elif sec == "4":
                menu_detay(analiz)
            elif sec == "5":
                menu_favoriler(analiz)
            elif sec == "6":
                cfg = menu_ayarlar(cfg)
                analiz.cfg = cfg
                analiz.rf = float(cfg.get("risk_free", 0.40))
                analiz.bench = {}
            elif sec == "7":
                menu_tum_siralama(analiz)
            elif sec == "8":
                menu_optimizasyon(analiz)
            elif sec == "0":
                print("Güle güle.")
                break
            else:
                print("Geçersiz.")
        except KeyboardInterrupt:
            print("\nİptal.")
        except Exception as e:
            print(f"Hata: {e}")


if __name__ == "__main__":
    main()
