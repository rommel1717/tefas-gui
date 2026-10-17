# -*- coding: utf-8 -*-
"""
BIST Değerleme Motoru v192 — Graham+Buffett ∩ Teknik AL + EPV + Forward + KAP Yeni İş

v7 EKLERİ:
- Residual Income 5Y forecast + Gordon terminal
- Adil Değer (RI), RI Marjı, RI1, ROE-ke spread
- Ağırlıklı adil değere RI dahil (sektöre göre ağırlık)
- Economic Quality: ROE-ke + RI/B0
- Normalize ROE (medyan 4Ç) mean-reversion forecast

v7.1 KALİBRASYON (backtest ters sinyal düzeltmesi):
- Değer ağırlığı düşürüldü; Kalite / Bilanço / Nakit artırıldı
- Aşırı nominal büyüme (YoY > %50) growth skorunda bastırılıyor (enflasyon artefaktı)
- Değer tuzağı cezası: ucuz + düşük kalite / zayıf FCF
- Value skoru içinde saf çarpan (F/K, PD/DD) payı azaltıldı; FCF/OE/RevDCF güçlendirildi
- Risk çarpanı biraz sıkılaştırıldı
- Sektör ağırlıkları backtest-korelasyon yönünde yeniden dengelendi

v9 BİRLEŞİK SÜRÜM:
- v7 çekirdek (RI, DCF, peer, backtest, Excel paket)
- v7.1 kalibrasyon (value↓ quality↑ growth-cap value-trap)
- Aday listesi: Graham+Buffett hard ∩ Teknik AL (Aday Skoru=0.65 temel+0.35 teknik)
- Due Diligence 5 madde + Derin Analiz / Ana Risk / Yatırım Tezi
- Shannon entropy + Kelly pay proxy + NET SİNYAL notu
- Dinamik ke (--ke/--rf/--erp) + senaryo ke spread

v7.2 SHANNON (Claude Shannon bilgi teorisi uyarlaması):
- Bileşen skoru entropy'si H: sinyal dağınıklığı (düşük H = net sinyal)
- Shannon Güven: Güven × (1 - normalize H) — belirsizlik cezası
- Kelly proxy: edge (adil marj/OE) ve belirsizlikten üst sınırlı pozisyon payı
- Shannon Notu: NET SİNYAL / KARIŞIK / GÜRÜLTÜ + pozisyon sınıfı

Kurulum: pip install requests pandas numpy openpyxl beautifulsoup4 lxml
UYARI: Yatırım tavsiyesi değildir.
"""
import argparse
import json
import math
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from openpyxl import load_workbook
from openpyxl.formatting.rule import ColorScaleRule, CellIsRule
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.datavalidation import DataValidation


LIST_URL = "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/default.aspx"
RATIO_URL = "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/Temel-Degerler-Ve-Oranlar.aspx#page-5"
FIN_URL_TMPL = (
    "https://www.isyatirim.com.tr/_layouts/15/IsYatirim.Website/Common/Data.aspx/"
    "MaliTablo?companyCode={code}&exchange=TRY&financialGroup=XI_29"
    "&year1={y1}&period1={p1:02d}&year2={y2}&period2={p2:02d}"
    "&year3={y3}&period3={p3:02d}&year4={y4}&period4={p4:02d}"
)

HEAD = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

THEME = {
    "primary": "6A1B9A", "dark": "35104A", "navy": "172B4D",
    "accent": "00A6A6", "stripe": "F4EFF8", "white": "FFFFFF",
    "green": "2E7D32", "yellow": "F9A825", "red": "C62828",
    "orange": "EF6C00", "title_font": "Aptos Display", "body_font": "Aptos"
}

# Değerleme varsayımları (TR nominal / yüksek enflasyon rejimi için kalibre).
# COST_OF_EQUITY varsayılanı: risksiz + TR equity risk premium yaklaşık bandı.
# Çalıştırma anında resolve_cost_of_equity() ile güncel TR 10Y + CDS'ten türetilebilir;
# --ke CLI ile elle override edilebilir. Fallback = 0.22.
COST_OF_EQUITY = 0.22
RISK_FREE_FALLBACK = 0.28          # TR 10Y yaklaşık (yüksek faiz rejimi)
EQUITY_RISK_PREMIUM_FALLBACK = 0.06  # TR hisse risk primi (CDS-adjusted kabaca)
# Senaryo ke sapmaları (Base ke'ye göre)
BEAR_KE_SPREAD = 0.03   # Bear: +3 puan risk primi
BULL_KE_SPREAD = -0.015 # Bull: -1.5 puan
# v192: NOMİNAL / REEL TUTARLILIK
# ke nominal TL olduğu için nakit akışı büyümesi de nominal olmalı. Nominal büyüme,
# reel büyüme + enflasyon patikasından türetilir (Fisher): (1+g_reel)*(1+π)-1.
# Enflasyon patikası: INFLATION_NOW'dan INFLATION_LT'ye lineer söner (fcff_dcf içinde).
INFLATION_NOW = 0.30        # güncel yıllık enflasyon beklentisi (CLI: --infl-now)
INFLATION_LT = 0.15         # uzun vade nominal enflasyon (terminal) (CLI: --infl-lt)
REAL_TERMINAL_G = 0.02      # terminal REEL büyüme


def _nominal(g_real, infl):
    return (1.0 + g_real) * (1.0 + infl) - 1.0


BASE_GROWTH = _nominal(0.03, INFLATION_NOW)    # ~%33.9 nominal (yedek yöntem)
BEAR_GROWTH = _nominal(-0.05, INFLATION_NOW)   # ~%23.5
BULL_GROWTH = _nominal(0.10, INFLATION_NOW)    # ~%43.0
TERMINAL_GROWTH = _nominal(REAL_TERMINAL_G, INFLATION_LT)  # ~%17.3 nominal
ZERO_REAL_GROWTH_NOM = INFLATION_LT            # EPV / kapitalize kazanç: reel büyüme=0 → nominal g=π_LT
GROWTH_CLIP_LO = -0.10
GROWTH_CLIP_HI = INFLATION_NOW + 0.10          # gözlenen büyüme en fazla enflasyon+10 puan
KE_MIN_SPREAD = 0.06                           # ke ≥ terminal_g + 6 puan (Gordon patlamasın)
# Sektör ke primleri (COST_OF_EQUITY'ye göre fark). v191'de sabit mutlak seviyeler +
# clip(0.14, 0.32) yüzünden 590 hissenin 361'i tavana yapışıyordu.
SECTOR_KE_PREMIUM = {
    'Banka': -0.04, 'Sigorta': -0.04, 'GYO': -0.02, 'Holding/Yatırım': 0.0,
    'Telekom': -0.02, 'Perakende': 0.0, 'Havacılık': 0.02, 'Otomotiv': 0.01, 'Enerji': 0.01,
}
def configure_inflation(now=None, lt=None, real_terminal=None):
    """Enflasyon / terminal büyüme varsayımlarını çalışma anında değiştirir ve türev sabitleri yeniler.
    (CLI: --infl-now, --infl-lt, --real-g)"""
    global INFLATION_NOW, INFLATION_LT, REAL_TERMINAL_G, BASE_GROWTH, BEAR_GROWTH, BULL_GROWTH
    global TERMINAL_GROWTH, ZERO_REAL_GROWTH_NOM, GROWTH_CLIP_HI
    if now is not None: INFLATION_NOW = float(now)
    if lt is not None: INFLATION_LT = float(lt)
    if real_terminal is not None: REAL_TERMINAL_G = float(real_terminal)
    BASE_GROWTH = _nominal(0.03, INFLATION_NOW)
    BEAR_GROWTH = _nominal(-0.05, INFLATION_NOW)
    BULL_GROWTH = _nominal(0.10, INFLATION_NOW)
    TERMINAL_GROWTH = _nominal(REAL_TERMINAL_G, INFLATION_LT)
    ZERO_REAL_GROWTH_NOM = INFLATION_LT
    GROWTH_CLIP_HI = INFLATION_NOW + 0.10
    return TERMINAL_GROWTH


def equity_basis(ke):
    """Özkaynak bazlı yöntemler için (ke_eff, terminal_g_eff, g0_eff).
    real   : ke_eff = REAL_KE_BASE + (ke − COST_OF_EQUITY)  [sektör/kaldıraç primi korunur]; g_terminal = REAL_TERMINAL_G; g0 = 0
    nominal: ke, TERMINAL_GROWTH, ZERO_REAL_GROWTH_NOM"""
    if str(EQUITY_BASIS).lower() == "real":
        ke_eff = REAL_KE_BASE + (float(ke) - float(COST_OF_EQUITY))
        ke_eff = max(ke_eff, REAL_TERMINAL_G + KE_MIN_SPREAD)
        return ke_eff, REAL_TERMINAL_G, 0.0
    return float(ke), TERMINAL_GROWTH, ZERO_REAL_GROWTH_NOM


# Uç değer korumaları
FAIR_VALUE_MAX_MULT = 10.0    # bileşen adil değer > 10×fiyat → bileşen dışı (birim/veri hatası)
FAIR_MARGIN_MIN = -0.95
FAIR_MARGIN_MAX = 3.00        # marj [-%95, +%300] aralığına kırpılır
# Backtest / ters sinyal
INVERSE_SIGNAL_ENABLED = False   # geçmiş getiri × bugünkü skor = geçersiz test → otomatik ceza KAPALI
KELLY_HIST_ENABLED = False       # look-ahead'li arşiv getirilerinden Kelly istatistiği KAPALI
# Aday listesi
CANDIDATE_ALLOW_FILL = True      # Tier A/B < 15 ise listeyi "Tier C (Kriterlere Yakın)" ile 15'e tamamla.
                                 # Tamamlama SERT KAPILARI (H1-H6) yine geçmek zorunda; sıralama: karşılanan kriter sayısı.
CANDIDATE_MIN_COMPLETENESS = 0.75
# Güvenlik marjı (Graham "margin of safety"): modelin kendi adil değer marjı bu eşiklerin altındaysa değer adayı sayılmaz.
# v192.3'te F/K–PD/DD gibi çarpan eşikleri geçen ama adil değerin %5-15 altında/üstünde fiyatlanan hisseler listedeydi.
MOS_TIER_A = 0.30     # CLI: --mos 0.30,0.20,0.15
MOS_TIER_B = 0.20
MOS_TIER_C = 0.15
CANDIDATE_FILL_MAX_MISS = None   # Tier C için en çok kaç kriter kaçırılabilir (None = sınır yok; 2 → daha kısa ama daha sağlam liste)
# Likidite filtresi (v192.1): son LIQ_WINDOW işlem gününün MEDYAN günlük işlem hacmi (TL) alt sınırı.
# Medyan kullanılır çünkü tek günlük blok işlem/hacim patlaması ortalamayı şişirir.
# Özkaynak bazlı yöntemler (RI, kapitalize kazanç, EPV) için iskonto tabanı.
# TMS 29 uygulanan tablolarda ROE/EPS satın alma gücü cinsinden (reel) olduğundan nominal ke ile kıyaslamak
# yöntemleri sistematik aşağı çekiyordu. 'real': reel ke = REAL_KE_BASE + sektör/kaldıraç primi; 'nominal': eski davranış.
EQUITY_BASIS = "real"          # CLI: --equity-basis
REAL_KE_BASE = 0.12            # CLI: --real-ke  (reel rf ~%4 + reel ERP ~%8)
# Teknik katman: zamanlama BİLGİSİ (kapı değil). True → eski davranış (KAÇIN + düşük skor elenir)
TECH_HARD_GATE = False
TECH_RANK_WEIGHT = 0.15        # Aday Skoru'nda teknik ağırlığı (eskiden 0.30)
# Fiyat uyuşmazlığı: İş Yatırım fiyatı ile Yahoo son fiyatı bu orandan fazla ayrışırsa aday olamaz
PRICE_MISMATCH_TOL = 0.05
# Ağırlıklandırma: Kelly yerine risk parity × (skor/tier) eğimi
SIZING_TIER_MULT = {"A": 1.0, "B": 0.9, "C": 0.75}
LIQ_WINDOW = 60
LIQ_MIN_TURNOVER_TL = 10_000_000.0   # CLI: --min-liq (milyon TL)
LIQ_MAX_ZERO_DAYS = 0.20             # işlem görmeyen gün oranı üst sınırı
LIQ_HIGH_TURNOVER_TL = 50_000_000.0
LIQ_EXIT_PARTICIPATION = 0.10        # 1 günde tahliye için günlük hacmin en fazla %10'u
# v12: nominal enflasyon artefaktı için daha agresif growth cap
GROWTH_YOY_SOFT_CAP = 0.40   # %40 üstü growth skoruna yumuşak ceza
GROWTH_YOY_HARD_CAP = 0.55   # %55 üstü growth skorunu sert bastır
CAPEX_AMORT_OE_PENALTY_THR = 2.5  # CapEx/Amortisman > 2.5 → OE güvenini düşür
INVERSE_VALUE_WEIGHT_MULT = 0.65  # ters sinyal aktifken Value ağırlığı çarpanı
FORECAST_YEARS = 5
MAX_WORKERS = 12  # I/O bound (Yahoo + API); CPU değil
BACKTEST_ENABLED = True
BACKTEST_TIMEOUT = 20
KELLY_F_MAX = 0.25
KELLY_HALF = True  # üretimde half-Kelly
KELLY_MIN_N = 30   # tarihsel p,b için min örneklem
SCORE_HISTORY_NAME = "bist_score_history.csv"
VOL_LOOKBACK_DAYS = 126  # ~6 ay iş günü
STOP_ATR_MULT = 2.0          # stop ≈ 2σ günlük * sqrt(horizon)
TRAIL_ATR_MULT = 2.5         # trailing mesafe
SINGLE_NAME_CAP = 0.10       # tek hisse tavan %10 (15 hisse portföy)
SECTOR_CAP = 0.25            # sektör tavanı %25
PORTFOLIO_SIZE = 15           # değer portföyü hedef hisse adedi
N_CRITERIA_PASS = 15          # karşılama oranı için sabit kriter sayısı (X/15)
INVERSE_SIGNAL_CORR_THR = -0.05  # Spearman eşiği
# --- Bütünleşik değerlendirme ağırlıkları (kullanıcı kalibre eder; toplam ≈ 1.0) ---
EVAL_W_DD = 0.16          # Due diligence
EVAL_W_GRAHAM = 0.13      # Graham F/K PD/DD tüm kriterler + net-net + marj
EVAL_W_SCORE = 0.12       # Final skor (temel)
EVAL_W_SHANNON = 0.10     # NET SİNYAL / H_norm
EVAL_W_KELLY = 0.09       # Kelly Final
EVAL_W_SHARPE = 0.07      # Sharpe Proxy (rf=0)
EVAL_W_VALUE = 0.08       # Adil marj + FCF/OE
EVAL_W_STOP = 0.05        # Stop mesafesi
EVAL_W_INVERSE = 0.10     # Ters sinyal cezası
EVAL_W_TIMING = 0.10      # v12: Teknik/Zamanlama skoru (ayrı kolon)
# --- Aday listesi sıralama ağırlıkları (top 15) ---
ADAY_W_BUTUNLESIK = 0.28
ADAY_W_GRAHAM = 0.16
ADAY_W_KELLY = 0.12
ADAY_W_SHANNON = 0.12
ADAY_W_DD = 0.10
ADAY_W_SCORE = 0.10
ADAY_W_KOVA = 0.08
ADAY_W_VALUE = 0.04

# Walk-forward: True ise ek olarak geçmiş anchor tarihlerinde skor-getiri paneli üretir
# (mevcut 8 çeyrek verisiyle sınırlı; tam historical restatement değildir).
WALK_FORWARD_ENABLED = False

# TradingView herkese açık (resmi olmayan) tarayıcı uç noktasından "ikinci görüş"
# çapraz kontrolü. Kendi Teknik Skor'umuzu DEĞİŞTİRMEZ, sadece ek bir bilgi
# kolonu ekler. Resmi bir API değildir; site erişimi keserse/uç nokta değişirse
# sessizce atlanır. Sorun çıkarırsa buradan tek satırla kapatılabilir.
ENABLE_TRADINGVIEW_CROSSCHECK = True
WALK_FORWARD_HORIZONS_DAYS = (180, 365, 730)
# Klasik Graham "net-net" marjı: fiyat, net cari aktif değerinin (NCAV) bu oranından
# düşükse derin değer/net-net fırsatı sayılır.
NET_NET_MARGIN = 0.67
# Risk cezası üst sınırı (puan) — v3'te 20 idi, skorları aşırı bastırıyordu.
RISK_PENALTY_MAX = 12.0
RISK_PENALTY_THRESHOLD = 45.0

# --- Sektör haritası: İş Yatırım temel özet Excel (Kod -> Sektör) ---
SECTOR_XLSX_NAMES = ["temelozet_sektor.xlsx", "temelozet.xlsx", "temelozet(1).xlsx"]
_SECTOR_MAP = None
_SECTOR_WEIGHT_BUCKET = {
    "bankacilik": "Banka",
    "sigorta": "Sigorta",
    "gyo": "GYO",
    "holdingler": "Holding/Yatırım",
    "yatirim ortakliklari": "Holding/Yatırım",
    "araci kurumlar": "Holding/Yatırım",
    "varlik yonetim": "Holding/Yatırım",
    "fin kiralama ve faktoring": "Banka",
    "iletisim": "Telekom",
    "iletisim cihazlari": "Telekom",
    "perakande ticaret": "Perakende",
    "perakende ticaret": "Perakende",
    "havayollari ve hizm": "Havacılık",
    "otomotiv": "Otomotiv",
    "otomotiv parcasi": "Otomotiv",
    "otomotiv lastigi": "Otomotiv",
    "elektrik uretim": "Enerji",
    "elektrik dogalgaz dagitim": "Enerji",
    "elektrik enerji urt tech tesis kurulum": "Enerji",
    "petrol": "Enerji",
}


def fetch_isyatirim_sector_map(session=None):
    """İş Yatırım hisse listesi tablosundan Kod→Sektör (~600+ hisse)."""
    close_session = False
    if session is None:
        session = requests.Session()
        session.headers.update(HEAD)
        close_session = True
    m = {}
    try:
        # Liste sayfası: Kod, Hisse Adı, Sektör, ...
        r = session.get(LIST_URL, headers={**HEAD, "Referer": "https://www.isyatirim.com.tr/"}, timeout=60)
        r.raise_for_status()
        tables = pd.read_html(StringIO(r.text), decimal=",", thousands=".")
        for raw in tables:
            df = flatten(raw.copy())
            cols = {norm(c).replace(" ", ""): c for c in df.columns}
            code_col = next((v for k, v in cols.items() if k in {"kod", "hisse", "hissekodu"}), None)
            sec_col = next((v for k, v in cols.items() if "sektor" in k), None)
            if not code_col or not sec_col:
                continue
            for _, row in df.iterrows():
                code = re.sub(r"[^A-Z0-9]", "", str(row[code_col]).strip().upper())
                sec = str(row[sec_col]).strip() if pd.notna(row[sec_col]) else ""
                if code and sec and sec.lower() not in {"nan", "none", "-", ""}:
                    m[code] = sec
            if len(m) >= 50:
                break
        # Oran sayfasından da dene (yedek)
        if len(m) < 50:
            url = RATIO_URL.split("#", 1)[0]
            r2 = session.get(url, headers={**HEAD, "Referer": "https://www.isyatirim.com.tr/"}, timeout=60)
            r2.raise_for_status()
            for raw in pd.read_html(StringIO(r2.text), decimal=",", thousands="."):
                df = flatten(raw.copy())
                cols = {norm(c).replace(" ", ""): c for c in df.columns}
                code_col = next((v for k, v in cols.items() if k in {"kod", "hisse", "hissekodu"}), None)
                sec_col = next((v for k, v in cols.items() if "sektor" in k), None)
                if not code_col or not sec_col:
                    continue
                for _, row in df.iterrows():
                    code = re.sub(r"[^A-Z0-9]", "", str(row[code_col]).strip().upper())
                    sec = str(row[sec_col]).strip() if pd.notna(row[sec_col]) else ""
                    if code and sec and sec.lower() not in {"nan", "none", "-", ""}:
                        m[code] = sec
    except Exception as e:
        print(f"UYARI: İş Yatırım sektör çekilemedi: {e}", flush=True)
    finally:
        if close_session:
            try:
                session.close()
            except Exception:
                pass
    return m


def load_sector_map(path=None, session=None, force_refresh=False):
    """Kod→Sektör: (1) JSON cache — anında (2) Excel (3) İş Yatırım canlı yalnızca gerekirse.

    force_refresh=True olmadıkça ağ çağrısı YAPILMAZ → değerleme hızlanır.
    """
    global _SECTOR_MAP
    if _SECTOR_MAP is not None and path is None and not force_refresh:
        return _SECTOR_MAP
    import json as _json
    m = {}

    # 1) Yerel JSON (ben hazırladım — ~630 hisse)
    if not force_refresh:
        for cache_p in (
            Path(__file__).with_name("bist_sektor_map.json"),
            Path.cwd() / "bist_sektor_map.json",
            Path("/home/workdir/artifacts/bist_sektor_map.json"),
        ):
            try:
                if cache_p.exists():
                    data = _json.loads(cache_p.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and len(data) >= 50:
                        m = {str(k).upper(): str(v) for k, v in data.items() if v}
                        print(f"Sektör haritası (JSON cache): {cache_p.name} ({len(m)} hisse) — ağ yok", flush=True)
                        break
            except Exception as e:
                print(f"UYARI: sektör JSON: {e}", flush=True)

    # 2) Excel (opsiyonel)
    if len(m) < 50 or path:
        candidates = []
        if path:
            candidates.append(Path(path))
        for name in SECTOR_XLSX_NAMES:
            candidates.append(Path.cwd() / name)
            try:
                candidates.append(Path(__file__).with_name(name))
            except NameError:
                pass
            candidates.append(Path("/home/workdir/attachments") / name)
            candidates.append(Path("/home/workdir/artifacts") / name)
        found = next((c for c in candidates if c and Path(c).exists()), None)
        if found:
            try:
                df = pd.read_excel(found)
                cols = {norm(c).replace(" ", ""): c for c in df.columns}
                code_col = next((v for k, v in cols.items() if k in {"kod", "hisse", "hissekodu"}), None)
                sec_col = next((v for k, v in cols.items() if "sektor" in k), None)
                if code_col and sec_col:
                    n0 = len(m)
                    for _, row in df.iterrows():
                        code = re.sub(r"[^A-Z0-9]", "", str(row[code_col]).strip().upper())
                        sec = str(row[sec_col]).strip() if pd.notna(row[sec_col]) else ""
                        if code and sec and sec.lower() not in {"nan", "none", ""}:
                            m[code] = sec
                    print(f"Sektör haritası (Excel): {found.name} ({len(m)} hisse, +{len(m)-n0})", flush=True)
            except Exception as e:
                print("UYARI: Sektör Excel okunamadı:", e, flush=True)

    # 3) Canlı — sadece cache yoksa veya force
    if force_refresh or len(m) < 80:
        live = fetch_isyatirim_sector_map(session=session)
        if live:
            m.update(live)
            print(f"Sektör haritası (İş Yatırım canlı): {len(live)} → toplam {len(m)}", flush=True)

    for k, v in _BIST_SECTOR_STATIC.items():
        m.setdefault(k, v)

    if not m:
        print("UYARI: Sektör haritası boş — statik/Yahoo yedek kullanılacak.", flush=True)
    _SECTOR_MAP = m
    try:
        Path(__file__).with_name("bist_sektor_map.json").write_text(
            _json.dumps(m, ensure_ascii=False, indent=0), encoding="utf-8"
        )
    except Exception:
        pass
    return _SECTOR_MAP


def sector_weight_bucket(sector_name):
    """İnce Excel sektör adını skor/WACC profil anahtarına çevirir."""
    if not sector_name:
        return "Genel/Sanayi"
    k = norm(sector_name)
    if k in _SECTOR_WEIGHT_BUCKET:
        return _SECTOR_WEIGHT_BUCKET[k]
    for key, bucket in _SECTOR_WEIGHT_BUCKET.items():
        if key in k or k in key:
            return bucket
    if "banka" in k:
        return "Banka"
    if "sigorta" in k:
        return "Sigorta"
    if "gyo" in k or "gayrimenkul" in k:
        return "GYO"
    if "holding" in k or "yatirim" in k or "araci" in k:
        return "Holding/Yatırım"
    if "telekom" in k or "iletisim" in k:
        return "Telekom"
    if "perakende" in k or "perakande" in k:
        return "Perakende"
    if "hava" in k:
        return "Havacılık"
    if "otomotiv" in k:
        return "Otomotiv"
    if "enerji" in k or "elektrik" in k or "petrol" in k or "dogalgaz" in k:
        return "Enerji"
    return "Genel/Sanayi"


ALIASES = {
    "assets": ["toplam varliklar", "aktif toplami"],
    "ca": ["donen varliklar"],
    "cl": ["kisa vadeli yukumlulukler", "kisa vadeli borclar"],
    "liab": ["toplam yukumlulukler", "toplam borclar"],
    "equity": ["ana ortakliga ait ozkaynaklar", "ozkaynaklar", "ozsermaye"],
    "retained": [
        "gecmis yillar karlari zararlari", "gecmis yillar kari zarari",
        "gecmis yillar kar zararlari", "birikmis karlar", "birikmis kar zararlar"
    ],
    "revenue": ["hasilat", "satis gelirleri", "net satislar"],
    "gross": ["brut kar zarar", "brut kar"],
    "ebit": ["finansman gideri oncesi faaliyet kari", "faaliyet kari zarari"],
    "net": ["donem kari zarari", "net donem kari zarari", "ana ortaklik paylari"],
    # DÜZELTME: Gerçek İş Yatırım kalem adı "İşletme Faaliyetlerinden Kaynaklanan Net Nakit"imiş;
    # eski alias ("...nakit akislari") hiçbir şirkette eşleşmiyordu, bu yüzden OCF (ve ona bağlı
    # FCF, DCF, Nakit Akışı Skoru, Piotroski CFO/accrual kriterleri) TÜM hisselerde NaN kalıyordu.
    "ocf": ["isletme faaliyetlerinden kaynaklanan net nakit", "isletme faaliyetlerinden nakit akislari"],
    "cash": ["nakit ve nakit benzerleri"],
    "dep": [
        "amortisman giderleri", "amortisman itfa paylari",
        "amortisman ve itfa gideri ile ilgili duzeltmeler",
        "amortisman ve itfa giderleri", "amortisman ve itfa payi"
    ],
    # DÜZELTME: Gerçek kalem adı "Sabit Sermaye Yatırımları"ymış; eski alias hiçbir şirkette
    # eşleşmiyordu (CapEx hep NaN -> FCF hep NaN).
    "capex": [
        "sabit sermaye yatirimlari",
        "maddi ve maddi olmayan duran varliklarin alimindan kaynaklanan nakit cikislari",
        "maddi ve maddi olmayan duran varliklarin alimi",
        "yatirim faaliyetlerinde kullanilan nakit akislari"
    ],
    # YENİ: İş Yatırım "Serbest Nakit Akım"ı zaten hazır bir kalem olarak veriyor; bu doğrudan
    # kaynak, OCF-CapEx'ten kendi hesapladığımızdan daha güvenilir (birincil kaynak olarak kullanılır).
    "fcf_direct": ["serbest nakit akim"],
    # YENİ: Nakit akış tablosundaki GERÇEK "Dönem Sonu Nakit" kalemi; bilançodaki "Nakit ve Nakit
    # Benzerleri" (daha geniş, kısa vadeli finansal yatırımları da içerebilir) ile karışmasın diye ayrı tutulur.
    "period_end_cash": ["donem sonu nakit"],
    "paid_capital": ["odenmis sermaye", "cikarilmis sermaye"],
    # Net yabancı para pozisyonu (bilanço dipnot / mali tablo kalemi)
    "fx_position": [
        "net yabanci para pozisyonu",
        "net ypp",
        "parasal net yabanci para varlik yukumluluk pozisyonu",
    ],
    "interest_exp": [
        "finansman giderleri",
        "finansal giderler",
        "faiz giderleri",
    ],
    # v192.9: Beneish-lite ve temettü verimi için ek kalemler
    "receivables": ["ticari alacaklar"],
    "inventory": ["stoklar"],
    "ppe": ["maddi duran varliklar"],
    "dividends_paid": ["odenen temettuler", "temettu odemeleri"],
    # v192.10: nakit dönüşüm döngüsü ve GYO NAV modeli için ek kalemler
    "cogs": ["satislarin maliyeti"],
    "payables": ["ticari borclar"],
    "investment_property": ["yatirim amacli gayrimenkuller"],
}

# DÜZELTME: "Finansal Borçlar" kalemi HEM Kısa Vadeli HEM Uzun Vadeli Yükümlülükler altında
# BİREBİR AYNI METİNLE görünüyor (yalnızca itemCode'ları farklı, örn. "2AA" vs "2BA"). Düz metin
# araması (find()) bu durumda her zaman dataframe'de önce gelen satırı (kısa vadeliyi) buluyordu;
# bu yüzden Uzun Vadeli Borç sistematik olarak yanlış/eksik hesaplanıyordu. Çözüm: önce üst kalemin
# (Kısa/Uzun Vadeli Yükümlülükler) itemCode'unu bulup, aramayı yalnızca o itemCode ile BAŞLAYAN
# (aynı hiyerarşik dal içindeki) satırlarla sınırlamak.
SD_ROOT_WORDS = ["kisa vadeli yukumlulukler"]
LD_ROOT_WORDS = ["uzun vadeli yukumlulukler"]
DEBT_ITEM_WORDS = ["finansal borclar", "borclanmalar", "finansal yukumlulukler"]

# "sadece rakam olanlar onluk/binlik ayırıcı ile 2 basamak, oranlar % ile" kuralı için
# tek bir merkezi liste (hem 'Değerleme Özeti' sayfasının genel biçimlendirmesinde hem
# de 'Hisse Analiz' sayfasındaki formül hücrelerinde kullanılır — bkz. style() ve
# create_stock_detail_sheet()).
PCT_COL_NAMES = {
    "Adil Değer Marjı", "Değerleme Uzlaşma Skoru", "ROE", "ROA", "ROIC",
    "Net Kar Marjı", "Faaliyet Kar Marjı", "FAVÖK Marjı", "FCF Marjı",
    "FCF/Net Kar", "Kazanç Getirisi", "FCF Getirisi",
    "Net Kâr Trend Tutarlılığı (8Ç)", "Kârlılık Skoru", "Değer Skoru",
    "Bilanço Skoru", "Nakit Akışı Skoru", "Veri Tamlık",
    "Brüt Kar Marjı", "Graham Kriter Geçiş Oranı", "Graham Güvenlik Marjı",
    "Net Nakit/Piyasa Değeri",
    "FCF Conversion Trend", "Büyüme Skoru", "Economic Quality Skoru",
    "ROIC-WACC Spread", "Accrual Kalitesi", "Brüt Marj İstikrarı", "Seyreltme Skoru",
    "Owner Earnings Getirisi", "Sloan Skoru", "Beneish Skoru",
    "ROIC Trend Skoru", "Brüt Marj Trend Skoru", "Reverse DCF Skoru",
    "WACC (ke)", "Reverse DCF Implied g", "RI Marjı", "EPV Marjı", "ROE-ke Spread", "RI/Defter",
}
# Prim/İskonto sütunları zaten yüzde SAYISI olarak geliyor (-15.2 = -%15.2), kesir değil;
# bu yüzden "0.00%" (kesri 100'le çarpan) yerine ayrı bir sayısal-yüzde formatı kullanılır.
RAW_PCT_COL_NAMES = set()

# "Hisse Analiz" sayfasındaki ÖZET METRİKLER bloğunun bölüm başlıkları ve sırası.
# 'Değerleme Özeti' sayfasında fiilen bulunan sütunlara göre süzülür; script ileride
# yeni sütun eklerse/kaldırırsa burada listelenmeyen alanlar otomatik olarak
# sona, "DİĞER METRİKLER" bölümüne eklenir (hiçbir veri sessizce kaybolmaz).
STOCK_DETAIL_SECTIONS = [
    ("SKOR & DEĞERLEME", [
        "Final Score", "Not", "Yatırım Kararı", "Sektör", "Güven Skoru", "Risk Skoru", "Risk Sınıfı", "Değerleme Sınıfı",
        "Adil Değer (Medyan)", "Adil Değer Marjı", "Adil Değer (RI)", "RI Marjı", "Değerleme Uzlaşma Skoru",
        "Değer Yatırımcılığı Sonucu", "Graham Kriter Geçiş Oranı",
        "Graham Güvenlik Marjı", "Net-Net (NCAV) Durumu",
        "WACC (ke)", "Rejim Notu", "Model Uyarısı", "Zarar Niteliği",
        "Temettü Ödemesi (TTM)", "Temettü Verimi", "Temettü Ödeyen Çeyrek (8Ç)",
        "FAVÖK Marj Trend Skoru", "Marj Yönü", "Nakit Dönüşüm Döngüsü (Gün)", "Alacak Tahsil Süresi (Gün)",
        "Stok Devir Süresi (Gün)", "Borç Ödeme Süresi (Gün)", "GYO NAV/Hisse (Deneysel)", "GYO NAV İskonto/Primi", "Döviz Riski",
    ]),
    ("NAKİT & DCF SENARYOLARI", [
        "Net Nakit (Nakit - Toplam Finansal Borç)", "Net Nakit/Piyasa Değeri",
        "Nakit Bazlı Ucuzluk Notu", "Owner Earnings (TTM)",
        "DCF Bear", "DCF Base", "DCF Bull",
        "Reverse DCF Implied g", "Reverse DCF Skoru",
        "Adil Değer (Graham)", "Adil Değer (NCAV)", "Adil Değer (Kapitalize Kazanç)",
        "Adil Değer (EPV)", "EPV Marjı", "EV/EBIT Band Skoru",
        "Adil Değer (RI)", "RI Marjı", "RI (Yıl1)", "ROE-ke Spread", "RI/Defter",
    ]),
    ("PİYASA VERİSİ & ÇARPANLAR", [
        "Fiyat", "Piyasa Değeri", "Hisse Sayısı", "Hisse Sayısı Kaynağı",
        "F/K", "PD/DD", "F/K (Forward)", "PD/DD (Forward)",
        "Δ F/K (Fwd−TTM)", "Δ PD/DD (Fwd−TTM)",
        "Forward F/K Kaynak", "Forward PD/DD Kaynak", "Forward g varsayımı",
        "EPS (TTM)", "Defter Değeri/Hisse",
        "Kazanç Getirisi", "FCF Getirisi", "Owner Earnings Getirisi",
        "EV", "Net Borç", "EV/EBIT", "EV/FAVÖK", "EV/Sales", "Price/Sales",
    ]),
    ("KARLILIK & BİLANÇO ORANLARI", [
        "ROE", "ROA", "ROIC", "Net Kar Marjı", "Faaliyet Kar Marjı",
        "FAVÖK Marjı", "FCF Marjı", "FCF/Net Kar", "Cari Oran",
        "Borç/Özkaynak", "Net Borç/Özkaynak", "Net Borç/FCF",
        "CapEx/Amortisman", "Kısa Vadeli Borç/Nakit", "Net YP Pozisyon/Özkaynak",
        "Net YP/EBITDA",
    ]),
    ("KALİTE SKORLARI (Piotroski / Graham / Altman / Diğer)", [
        "Piotroski F", "Piotroski Geçerli Kriter", "Graham Puan",
        "Graham Geçerli Kriter", "Altman Z", "Altman Bölge", "Altman Veri Notu",
        "Accrual Kalitesi", "Sloan Ratio", "Sloan Skoru",
        "Beneish M (Proxy)", "Beneish Skoru",
        "Brüt Marj İstikrarı", "ROIC Trend Skoru", "Brüt Marj Trend Skoru",
        "Faiz Karşılama", "Seyreltme Skoru",
        "Skor Sürücüleri (+)", "Skor Sürücüleri (-)",
    ]),
    ("BİLEŞEN SKORLARI", [
        "Net Kâr Trend Tutarlılığı (8Ç)", "Kârlılık Skoru", "Değer Skoru",
        "Bilanço Skoru", "Nakit Akışı Skoru", "Risk Cezası", "Veri Tamlık",
    ]),
    ("TTM FİNANSAL KALEMLER", [
        "Brüt Kar Marjı", "FAVÖK (TTM)", "Net Kar (TTM)", "Hasılat (TTM)",
        "OCF (TTM)", "CapEx (TTM)", "FCF (TTM)", "Normalized EPS",
        "FCF Conversion Trend",
        "Nakit ve Nakit Benzerleri (Bilanço)", "Dönem Sonu Nakit (Nakit Akış Tablosu)",
        "Son 8 Çeyrek Net Kâr (Güncelden Eskiye)",
    ]),
    ("PEER (EVREN İÇİ) KARŞILAŞTIRMA & NİHAİ SKOR", [
        "Peer % ROIC", "Peer % ROE", "Peer % FCF Getirisi", "Peer % F/K",
        "Peer % PD/DD", "Peer % EV/FAVÖK", "Peer % Net Borç/Özkaynak",
        "Peer % Adil Değer Marjı", "Peer % Piotroski F", "Peer % Altman Z",
        "Peer Skor", "Final Score", "Birleşik Skor", "Risk Ayarlı Skor", "Sınıf",
    ]),
]




def norm(x):
    s = str(x or "").translate(str.maketrans("ıİşŞğĞüÜöÖçÇ", "iissgguuoocc")).lower()
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


def num(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return np.nan
    if isinstance(x, (int, float, np.number)):
        return float(x)
    s = str(x).strip().replace("\u00a0", "").replace(" ", "")
    if s in {"", "-", "--", "null", "None", "N/A", "nan"}:
        return np.nan
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9,.-]", "", s.strip("()").replace("TRY", "").replace("TL", "").replace("₺", "").replace("%", ""))
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        a = s.split(",")
        s = "".join(a) if len(a) > 1 and len(a[-1]) == 3 else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        v = float(s)
        return -v if neg else v
    except Exception:
        return np.nan


def div(a, b):
    return np.nan if pd.isna(a) or pd.isna(b) or b == 0 else a / b


def safe_mean(vals):
    vals = [float(x) for x in vals if pd.notna(x) and np.isfinite(x)]
    return float(np.mean(vals)) if vals else np.nan


def pct_rank(series, value, higher_is_better=True):
    s = pd.to_numeric(series, errors="coerce").dropna()
    if pd.isna(value) or s.empty:
        return np.nan
    if len(s) < 4:
        return np.nan
    rank = float((s <= value).mean())
    return rank if higher_is_better else 1.0 - rank


def quarter_periods(start_year, start_period, n=8):
    periods = []
    y, p = start_year, start_period
    for _ in range(n):
        periods.append((y, p))
        p -= 3
        if p <= 0:
            p += 12
            y -= 1
    return periods


# Bilanço açıklama gecikmeleri (SPK/KAP son tarihleri, konservatif): çeyrek bitişinden sonra kaç gün
# geçmeden o dönemin mali tablosu "yayımlanmış" sayılmaz. (9A → Kasım ortası, yıllık → Mart ortası.)
ANCHOR_PUBLISH_LAG_DAYS = {3: 45, 6: 70, 9: 45, 12: 75}
# Canlı doğrulama için kullanılan büyük/likit hisseler (hepsi bu dönemi yayımlamış olmalı).
ANCHOR_PROBE_CODES = ("THYAO", "ASELS", "EREGL", "BIMAS", "KCHOL", "TUPRS")
ANCHOR_PROBE_MIN_HITS = 2          # en az kaç probe hissede dolu veri görülmeli
ANCHOR_MAX_FALLBACK_QUARTERS = 4   # veri bulunamazsa en fazla kaç çeyrek geriye gidilir


def _prev_quarter(y, p):
    p -= 3
    if p <= 0:
        p += 12
        y -= 1
    return y, p


def calendar_anchor_period(today=None):
    """Takvime göre, açıklama gecikmesi GEÇMİŞ en son çeyrek (yıl, dönem-ayı).

    Eski davranış (ay>=10 → 9. ay) bilanço yayımlanmadan 1 Ekim'de Eylül çeyreğini seçiyordu.
    """
    from datetime import timedelta
    today = today or datetime.now()
    month_end_day = {3: 31, 6: 30, 9: 30, 12: 31}
    y, p = today.year, 12
    for _ in range(12):  # en fazla 3 yıl geriye
        q_end = datetime(y, p, month_end_day[p])
        if today >= q_end + timedelta(days=ANCHOR_PUBLISH_LAG_DAYS[p]):
            return y, p
        y, p = _prev_quarter(y, p)
    return y, p


def _anchor_has_data(session, year, period, codes=ANCHOR_PROBE_CODES, min_hits=ANCHOR_PROBE_MIN_HITS):
    """İş Yatırım'da (year, period) için gerçekten dolu mali tablo var mı?

    True  → en az `min_hits` probe hissede dolu değer var
    False → sorgular çalıştı ama dönem boş (henüz yayımlanmamış)
    None  → hiçbir sorgu çalışmadı (ağ/endpoint hatası) — doğrulanamadı
    """
    qs = quarter_periods(year, period, 4)
    hits = errors = 0
    for code in codes:
        try:
            url = FIN_URL_TMPL.format(
                code=code,
                y1=qs[0][0], p1=qs[0][1], y2=qs[1][0], p2=qs[1][1],
                y3=qs[2][0], p3=qs[2][1], y4=qs[3][0], p4=qs[3][1],
            )
            r = session.get(url, headers=HEAD, timeout=30)
            r.raise_for_status()
            rows = r.json().get("value") or []
            filled = sum(1 for it in rows if pd.notna(num(it.get("value1"))))
            if filled >= 10:
                hits += 1
                if hits >= min_hits:
                    return True
        except Exception:
            errors += 1
    if errors == len(codes):
        return None
    return False


def detect_anchor_period(session):
    """Analiz dönemi çapasını (yıl, dönem-ayı) seçer.

    1) Takvim + açıklama gecikmesi ile aday dönem (ör. 2 Ekim → Haziran; Eylül bilançosu Kasım'da gelir).
    2) Aday dönem İş Yatırım'da büyük hisselerde BOŞSA bir önceki çeyreğe geri gider (en fazla 4 çeyrek).
    3) Canlı doğrulama hiç çalışmazsa (ağ hatası) takvim sonucu kullanılır ve uyarı basılır.
    Boş çapa → bilanço kalemleri NaN → Veri Tamlık<0.75 → aday listesi boş kalır; bu yüzden doğrulama şart.
    """
    y, p = calendar_anchor_period()
    for _ in range(ANCHOR_MAX_FALLBACK_QUARTERS):
        ok = _anchor_has_data(session, y, p)
        if ok is True:
            print(f"Çapa dönem: {y}/{p:02d} (doğrulandı)", flush=True)
            return y, p
        if ok is None:
            print(f"UYARI: Çapa dönem doğrulanamadı (ağ/endpoint hatası); takvime göre {y}/{p:02d} kullanılıyor.", flush=True)
            return y, p
        print(f"UYARI: {y}/{p:02d} dönemi henüz yayımlanmamış (probe hisselerde veri yok) — bir önceki çeyreğe geçiliyor.", flush=True)
        y, p = _prev_quarter(y, p)
    print(f"UYARI: {ANCHOR_MAX_FALLBACK_QUARTERS} çeyrek geriye gidildi, dolu dönem bulunamadı; {y}/{p:02d} kullanılıyor.", flush=True)
    return y, p


def flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join(str(v) for v in c if str(v) != "nan").strip() for c in df.columns]
    else:
        df.columns = [str(c).strip() for c in df.columns]
    return df


def get_ratios(session):
    url = RATIO_URL.split("#", 1)[0]
    r = session.get(url, headers={**HEAD, "Referer": "https://www.isyatirim.com.tr/"}, timeout=60)
    r.raise_for_status()
    tables = pd.read_html(StringIO(r.text), decimal=",", thousands=".")
    result = {}
    n_sec = 0
    for raw in tables:
        df = flatten(raw.copy())
        cols = {norm(c).replace(" ", ""): c for c in df.columns}
        code_col = next((v for k, v in cols.items() if k in {"kod", "hisse", "hissekodu"}), None)
        if not code_col:
            continue
        pb_col = next((v for k, v in cols.items() if "pddd" in k or "piyasadegeridefterdegeri" in k), None)
        pe_col = next((v for k, v in cols.items() if k in {"fk", "fiyatkazanc", "fiyatkazancorani"} or "fiyatkazanc" in k), None)
        close_col = next((v for k, v in cols.items() if "kapanis" in k), None)
        cap_col = next((v for k, v in cols.items() if "piyasadegerimntl" in k), None)
        capital_col = next((v for k, v in cols.items() if "sermayemntl" in k), None)
        sec_col = next((v for k, v in cols.items() if k in {"sektor", "sektör", "endustri", "industry"} or "sektor" in k), None)
        name_col = next((v for k, v in cols.items() if k in {"hisseadi", "unvan", "sirketadi", "name"} or "hissead" in k), None)
        for _, row in df.iterrows():
            code = re.sub(r"[^A-Z0-9]", "", str(row[code_col]).strip().upper())
            if not code or code in {"KOD", "NAN"}:
                continue
            rec = result.setdefault(code, {})
            if pb_col: rec["pb"] = num(row[pb_col])
            if pe_col: rec["pe"] = num(row[pe_col])
            if close_col: rec["price"] = num(row[close_col])
            if cap_col: rec["cap"] = num(row[cap_col]) * 1_000_000
            if capital_col: rec["shares"] = num(row[capital_col]) * 1_000_000
            if name_col and pd.notna(row.get(name_col)):
                rec["name"] = str(row[name_col]).strip()
            if sec_col and pd.notna(row.get(sec_col)):
                sec = str(row[sec_col]).strip()
                if sec and sec.lower() not in {"nan", "none", "-", ""}:
                    rec["sector"] = sec
                    n_sec += 1
    if not result:
        raise RuntimeError("İş Yatırım temel değer tablosundan hisse verisi okunamadı.")
    if n_sec:
        print(f"İş Yatırım sektör alanı: {n_sec} kayıt", flush=True)
    return result




def get_codes(session, ratios):
    if len(ratios) >= 100:
        return sorted(ratios)
    r = session.get(LIST_URL, headers=HEAD, timeout=45)
    r.raise_for_status()
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    bad = {"TRY", "BIST", "HTML", "HTTPS", "HTTP", "EXCEL", "FIYAT", "HACIM", "ADET", "DEGISIM"}
    return sorted({x for x in re.findall(r"(?<![A-Z0-9])[A-Z]{4,6}(?![A-Z0-9])", text) if x not in bad})


FIN_GROUP_FALLBACKS = ("UFRS_K", "UFRS")   # bankalar/sigorta/faktoring için denenecek İş Yatırım finansal grup kodları

BANK_ALIASES = {
    "assets": ["toplam varliklar", "varliklar toplami", "aktif toplami", "toplam aktifler"],
    "equity": ["toplam ozkaynaklar", "ana ortakliga ait ozkaynaklar", "ozkaynaklar", "ozsermaye"],
    "net": ["donem net kari veya zarari", "net donem kari zarari", "donem kari zarari", "donem net kari zarari",
            "surdurulen faaliyetler donem net kari zarari", "ana ortaklik paylari"],
    "revenue": ["toplam faiz geliri", "faiz gelirleri", "net faiz geliri", "toplam prim uretimi", "hasilat"],
}


def get_fin(session, code, quarters, group="XI_29"):
    cols = ["itemCode", "itemDescTr", "itemDescEng", "value1", "value2", "value3", "value4"]
    frames = []
    for batch in (quarters[0:4], quarters[4:8]):
        url = FIN_URL_TMPL.replace("financialGroup=XI_29", f"financialGroup={group}").format(
            code=code,
            y1=batch[0][0], p1=batch[0][1],
            y2=batch[1][0], p2=batch[1][1],
            y3=batch[2][0], p3=batch[2][1],
            y4=batch[3][0], p4=batch[3][1],
        )
        r = session.get(url, headers=HEAD, timeout=45)
        r.raise_for_status()
        df = pd.DataFrame(r.json().get("value") or [])
        if df.empty:
            df = pd.DataFrame(columns=cols)
        else:
            for c in cols:
                if c not in df:
                    df[c] = np.nan
            for c in cols[3:]:
                df[c] = df[c].map(num)
            df = df[cols]
        frames.append(df)

    a, b = frames
    if a.empty and b.empty:
        return pd.DataFrame()
    merged = pd.merge(a, b, on="itemCode", how="outer", suffixes=("", "_b"))
    merged["itemDescTr"] = merged["itemDescTr"].fillna(merged.pop("itemDescTr_b"))
    merged["itemDescEng"] = merged["itemDescEng"].fillna(merged.pop("itemDescEng_b"))
    for i in range(1, 5):
        merged[f"value{i+4}"] = merged.pop(f"value{i}_b")
    return merged[["itemCode", "itemDescTr", "itemDescEng"] + [f"value{i}" for i in range(1, 9)]]


def analyze_financial(code, df, r, quarters, group=""):
    """Banka/sigorta/faktoring için PD/DD–ROE tabanlı sade değerleme (DENEYSEL).

    Adil PD/DD = (ROE − g)/(ke − g) [özkaynak bazlı yöntemler için equity_basis()], ayrıca Residual Income.
    Ana sıralama/aday listesine GİRMEZ; 'Finansal Kurumlar' sayfasına yazılır.
    UYARI: İş Yatırım'ın finansal grup kalem adları canlı veride doğrulanmadı; eşleşme yoksa NaN döner ve not düşer."""
    m = {k: find(df, BANK_ALIASES[k]) for k in ("assets", "equity", "net", "revenue")}
    sanitize_flow_items(m)
    sector = classify_sector(code, r)
    price, cap, shares = r.get("price", np.nan), r.get("cap", np.nan), r.get("shares", np.nan)
    if pd.isna(shares) and pd.notna(cap) and pd.notna(price) and price:
        shares = cap / price
    E = m["equity"][0]
    A = m["assets"][0]
    NI = ttm_from_cumulative(m["net"], quarters)
    note = []
    if pd.isna(E) or E <= 0:
        note.append("Özkaynak kalemi eşleşmedi")
    if pd.isna(NI):
        note.append("Net kâr kalemi eşleşmedi")
    if pd.isna(shares) or shares <= 0:
        note.append("Hisse sayısı yok")
    bvps = (E / shares) if pd.notna(E) and E > 0 and pd.notna(shares) and shares > 0 else np.nan
    roe = (NI / E) if pd.notna(NI) and pd.notna(E) and E > 0 else np.nan
    pb = (price / bvps) if pd.notna(price) and pd.notna(bvps) and bvps > 0 else np.nan
    pe = (cap / NI) if pd.notna(cap) and pd.notna(NI) and NI > 0 else np.nan
    ke_nom = sector_wacc("Banka")
    ke_eq, tg_eq, _ = equity_basis(ke_nom)
    just_pb = np.nan
    if pd.notna(roe) and ke_eq > tg_eq + 0.02:
        just_pb = float(max((float(np.clip(roe, -0.2, 0.6)) - tg_eq) / (ke_eq - tg_eq), 0.0))
    fv_pb = just_pb * bvps if pd.notna(just_pb) and pd.notna(bvps) else np.nan
    fv_ri = np.nan
    if pd.notna(E) and pd.notna(NI) and pd.notna(shares) and E > 0 and shares > 0:
        fv_ri = residual_income_value(E, NI, shares, ke_eq, tg_eq, FORECAST_YEARS, 0.30, None)[0]
    vals = [v for v in (fv_pb, fv_ri) if pd.notna(v) and v > 0 and (pd.isna(price) or v <= FAIR_VALUE_MAX_MULT * price)]
    fair = float(np.mean(vals)) if vals else np.nan
    margin = np.nan
    if pd.notna(fair) and pd.notna(price) and price > 0:
        margin = float(np.clip(fair / price - 1.0, FAIR_MARGIN_MIN, FAIR_MARGIN_MAX))
    return {
        "Hisse": code, "Sektör": sector, "Finansal Grup": group, "Fiyat": price, "Piyasa Değeri": cap,
        "Defter Değeri/Hisse": bvps, "Toplam Varlık": A, "Özkaynak": E, "Net Kâr (TTM)": NI,
        "ROE": roe, "PD/DD": pb, "F/K": pe, "Kaldıraç (Varlık/Özkaynak)": (A / E) if pd.notna(A) and pd.notna(E) and E > 0 else np.nan,
        "ke (reel, özkaynak bazlı)": ke_eq, "Gerekçeli PD/DD": just_pb,
        "Adil Değer (PD/DD)": fv_pb, "Adil Değer (RI)": fv_ri, "Adil Değer (Ort.)": fair,
        "Adil Değer Marjı": margin, "Değerleme Sınıfı": valuation_class2(margin),
        "Not": "DENEYSEL — aday listesine girmez. " + ("; ".join(note) if note else "Kalem eşleşmeleri canlı veride doğrulayın."),
    }


def find(df, words):
    desc = df.itemDescTr.fillna("").map(norm)
    for mode in range(3):
        for w in map(norm, words):
            mask = desc.eq(w) if mode == 0 else desc.str.startswith(w) if mode == 1 else desc.str.contains(re.escape(w))
            if mask.any():
                row = df.loc[mask].iloc[0]
                return [num(row[f"value{i}"]) for i in range(1, 9)]
    return [np.nan] * 8


def find_scoped(df, root_words, item_words):
    """Önce root_words ile eşleşen üst kalemin itemCode'unu bulur, sonra aramayı yalnızca o
    itemCode ile BAŞLAYAN (aynı hiyerarşik dal içindeki) satırlarla sınırlar. Bu, "Finansal
    Borçlar" gibi aynı metnin hem Kısa hem Uzun Vadeli Yükümlülükler altında birebir aynı
    şekilde tekrarlandığı durumlarda doğru (kısa/uzun) satırı ayırt etmek için gereklidir —
    düz find() bu durumda her zaman dataframe'de önce gelen satırı (genelde kısa vadeliyi) alır."""
    desc = df.itemDescTr.fillna("").map(norm)
    root_code = None
    for w in map(norm, root_words):
        mask = desc.eq(w)
        if mask.any():
            root_code = str(df.loc[mask].iloc[0]["itemCode"])
            break
    if root_code is None:
        return [np.nan] * 8
    codes = df["itemCode"].astype(str)
    scoped = df[codes.str.startswith(root_code) & (codes != root_code)]
    if scoped.empty:
        return [np.nan] * 8
    return find(scoped, item_words)


def quarterize(series, quarters):
    """Kümülatif dönemleri mümkün olduğunca gerçek çeyrek eşdeğerine çevirir."""
    out = []
    prev = 0.0
    prev_year = None
    for v, (y, p) in zip(series, quarters):
        if pd.isna(v):
            out.append(np.nan)
            continue
        # Aynı yıl içindeki kümülatif gelir/kâr için delta al.
        if p == 3:
            q = v
        elif p in (6, 9, 12):
            prior_p = p - 3
            prior_idx = next((i for i, (yy, pp) in enumerate(quarters) if yy == y and pp == prior_p), None)
            prior = series[prior_idx] if prior_idx is not None else np.nan
            q = v - prior if pd.notna(prior) else v * 3.0 / p
        else:
            q = v
        out.append(q)
        prev = v
        prev_year = y
    return out


def annualized_average(series, quarters):
    q = quarterize(series, quarters)
    vals = [v for v in q if pd.notna(v)]
    return float(np.mean(vals) * 4) if vals else np.nan


def ttm_from_cumulative(series, quarters):
    """Son 4 çeyreğin toplamı (TTM). `quarters` YENİDEN ESKİYE sıralıdır (quarters[0] = son dönem).

    v192.3 DÜZELTME: v191 `vals[-4:]` ile en ESKİ 4 çeyreği topluyordu → tüm TTM metrikleri (hasılat, FAVÖK, net kâr,
    OCF, CapEx, EPS...) bir yıl bayattı. İş Yatırım F/K'sından türetilen net kârla karşılaştırma: doğru toplam ile
    medyan |log oran| 0,03; eski toplam ile 0,29. Son 4 çeyrek ardışık değilse (boşluk) normalize ortalamaya düşer."""
    q = quarterize(series, quarters)
    latest = q[:4]
    if len(latest) == 4 and all(pd.notna(v) for v in latest):
        return float(sum(latest))
    return annualized_average(series, quarters)


def ttm_prev(series, quarters):
    """Bir yıl önceki TTM (quarters[4:8]). Eksikse, geçen yılın aynı dönem kümülatifini yıllıklandırır (12/p).
    v191 geçen yılın YTD kümülatifini (ör. 6 ay) bugünkü TTM (12 ay) ile kıyaslıyordu → büyüme ~%100 şişiyor,
    Piotroski ROA/devir karşılaştırması taraflı oluyordu."""
    q = quarterize(series, quarters)
    prior = q[4:8]
    if len(prior) == 4 and all(pd.notna(v) for v in prior):
        return float(sum(prior))
    try:
        p = quarters[0][1]
        cum = series[4]
        if pd.notna(cum) and p in (3, 6, 9, 12):
            return float(cum) * 12.0 / p
    except Exception:
        pass
    return np.nan


def trend_consistency(series, quarters):
    q = quarterize(series, quarters)
    vals = [v for v in reversed(q) if pd.notna(v)]  # eski -> yeni
    if len(vals) < 3:
        return np.nan
    return sum(vals[i] > vals[i - 1] for i in range(1, len(vals))) / (len(vals) - 1)


def value_from_fcf(fcf, shares, ke, g, years=5, terminal_g=None):
    """FCFE-benzeri (kaldıraçlı) serbest nakitten doğrudan özkaynak/hisse. Net borç düşülmez."""
    tg = TERMINAL_GROWTH if terminal_g is None else terminal_g
    if any(pd.isna(x) for x in [fcf, shares, ke, g]) or shares <= 0 or ke <= tg + 0.02:
        return np.nan
    if fcf <= 0:
        return np.nan
    pv = 0.0
    for t in range(1, years + 1):
        # Basit ama şeffaf büyüme varsayımı; uç büyümeleri sınırla.
        cf = fcf * ((1 + g) ** t)
        pv += cf / ((1 + ke) ** t)
    terminal = fcf * ((1 + g) ** years) * (1 + tg) / (ke - tg)
    pv += terminal / ((1 + ke) ** years)
    return pv / shares


def graham_number(eps, bvps):
    if pd.isna(eps) or pd.isna(bvps) or eps <= 0 or bvps <= 0:
        return np.nan
    return math.sqrt(22.5 * eps * bvps)


def earnings_power_value(ebit, tax_rate, ke, shares, maint_capex=np.nan, da=np.nan, net_debt=0.0, g0=None):
    """Greenblatt/Buffett tarzı Earnings Power Value (büyüme=0).

    v192: nominal ortamda "büyüme=0" reel büyüme=0 demektir → nominal g0=π_LT:
    firma değeri = sürdürülebilir kazanç × (1+g0) / (ke − g0); özkaynak = firma değeri − net borç.
    maint_capex verilirse NOPAT yerine OE benzeri kullanılır.
    """
    g0 = ZERO_REAL_GROWTH_NOM if g0 is None else g0
    if any(pd.isna(x) for x in [ebit, ke, shares]) or shares <= 0 or ke - g0 < 0.04:
        return np.nan
    if ebit <= 0:
        return np.nan
    t = 0.25 if pd.isna(tax_rate) else float(np.clip(tax_rate, 0.0, 0.40))
    nopat = ebit * (1.0 - t)
    # Sürdürülebilir kazanç: NOPAT - bakım capex + D&A kabaca (bakım bilinmiyorsa NOPAT)
    if pd.notna(maint_capex) and maint_capex > 0:
        sustain = nopat - maint_capex
        if pd.notna(da) and da > 0:
            sustain = nopat + da - maint_capex  # FCFF benzeri sıfır büyüme
    else:
        sustain = nopat
    if sustain <= 0:
        return np.nan
    nd = 0.0 if pd.isna(net_debt) else float(net_debt)
    firm_value = sustain * (1.0 + g0) / (ke - g0)
    eq = firm_value - nd
    return (eq / shares) if eq > 0 else np.nan


def ev_ebit_band_score(ev_ebit):
    """Değer yatırımı EV/EBIT bant skoru (0-1). Düşük EV/EBIT ucuz.

    Band: ≤6 çok ucuz, 6-10 ucuz, 10-14 makul, 14-20 pahalı, >20 çok pahalı.
    """
    if pd.isna(ev_ebit) or ev_ebit <= 0:
        return np.nan
    x = float(ev_ebit)
    if x <= 6:
        return 1.0
    if x <= 10:
        return 0.85 - 0.15 * (x - 6) / 4.0
    if x <= 14:
        return 0.70 - 0.25 * (x - 10) / 4.0
    if x <= 20:
        return 0.45 - 0.30 * (x - 14) / 6.0
    return float(np.clip(0.15 - 0.01 * (x - 20), 0.0, 0.15))


def percentile_score(value, peer_values, higher=True):
    return pct_rank(pd.Series(peer_values), value, higher)


def _period_label(y, p):
    q = p // 3
    return f"{y} Ç{q}"


FLOW_KEYS = ("revenue", "gross", "ebit", "net", "ocf", "dep", "capex", "fcf_direct", "interest_exp")


def sanitize_flow_items(m):
    """İş Yatırım API'si raporlanmamış dönemler için 0.0 döndürüyor. Akış kalemlerinde (gelir, kâr, nakit akışı)
    seride en az bir sıfır olmayan değer varsa, sıfırları EKSİK say. Aksi halde çeyrek farkı (Q_n − Q_{n−1}) negatif
    hasılat/kâr üretir (ör. PRDGS, CASA, TRCAS'ta TTM hasılat < 0). Tüm seri sıfırsa (atıl şirket) olduğu gibi kalır.
    Idempotent; m'yi yerinde günceller."""
    for k in FLOW_KEYS:
        ser = m.get(k)
        if ser is None:
            continue
        vals = list(ser)
        if any(pd.notna(v) and v != 0 for v in vals):
            m[k] = [np.nan if (pd.notna(v) and v == 0) else v for v in vals]
    return m


def historical_quarterly_table(code, m, r, quarters):
    sanitize_flow_items(m)
    """Hisse bazlı, çeyrek çeyrek (yıllara göre) detay tablosu üretir.
    - Hasılat/Brüt Kâr/Faaliyet Kârı/Net Kâr: kümülatif değerlerden
      quarterize() ile ayrıştırılmış TEK ÇEYREK değeridir.
    - Altman Z (TTM): o çeyrek sonundaki bilanço + o çeyrekte biten son
      4 çeyreklik (TTM) EBIT/Hasılat kullanılarak hesaplanır. Piyasa
      değeri için GÜNCEL piyasa değeri kullanılır (geçmiş dönem piyasa
      değeri toplanmadığı için); bu nedenle geçmişe dönük Z değerleri
      yaklaşıktır ve sadece TREND amaçlıdır, kesin tarihi değer değildir.
    En yeni dönem index 0, en eski index 7 (quarters ile aynı sıra).
    """
    rev_q = quarterize(m["revenue"], quarters)
    gross_q = quarterize(m["gross"], quarters)
    ebit_q = quarterize(m["ebit"], quarters)
    net_q = quarterize(m["net"], quarters)

    cap = r.get("cap", np.nan)
    n = len(quarters)
    rows = []
    for i, (y, p) in enumerate(quarters):
        rev = rev_q[i]; gross = gross_q[i]; ebit = ebit_q[i]; net = net_q[i]

        # TTM penceresi (i .. i+3) tam doluysa hesapla.
        window_rev = rev_q[i:i + 4]
        window_ebit = ebit_q[i:i + 4]
        rev_ttm_i = float(sum(window_rev)) if len(window_rev) == 4 and all(pd.notna(x) for x in window_rev) else np.nan
        ebit_ttm_i = float(sum(window_ebit)) if len(window_ebit) == 4 and all(pd.notna(x) for x in window_ebit) else np.nan

        A = m["assets"][i]; CA = m["ca"][i]; CL = m["cl"][i]
        L = m["liab"][i]; E = m["equity"][i]; RE = m["retained"][i]
        if pd.isna(L) and pd.notna(A) and pd.notna(E):
            L = A - E
        if pd.isna(RE) and pd.notna(E):
            paid_c = m["paid_capital"][i]
            if pd.notna(paid_c):
                RE = E - paid_c
        wc = CA - CL if pd.notna(CA) and pd.notna(CL) else np.nan

        z = (
            1.2 * div(wc, A) + 1.4 * div(RE, A) + 3.3 * div(ebit_ttm_i, A) +
            0.6 * div(cap, L) + div(rev_ttm_i, A)
        ) if all(pd.notna(x) for x in [wc, A, RE, ebit_ttm_i, cap, L, rev_ttm_i]) and A and L else np.nan

        rows.append({
            "Hisse": code,
            "Dönem": _period_label(y, p),
            "Hasılat (Çeyrek)": rev,
            "Brüt Kâr (Çeyrek)": gross,
            "Faaliyet Kârı (Çeyrek)": ebit,
            "Net Kâr (Çeyrek)": net,
            "Brüt Kar Marjı": div(gross, rev),
            "Faaliyet Kar Marjı": div(ebit, rev),
            "Net Kar Marjı": div(net, rev),
            "Altman Z (TTM, Yaklaşık)": z,
            "Not": "" if pd.notna(z) else ("Yetersiz TTM verisi" if i > n - 4 else "Veri Yok"),
        })
    return rows


def clip01(x):
    return np.nan if pd.isna(x) else float(np.clip(x,0,1))

def score_target(x, low, high):
    return np.nan if pd.isna(x) else clip01((x-low)/(high-low))

def score_lower_better(x, good, bad):
    return np.nan if pd.isna(x) else clip01((bad-x)/(bad-good))

# Geniş BIST sektör haritası (temelozet Excel yokken "Diğer" şişmesini önler)
_BIST_SECTOR_STATIC = {
    # Banka
    "AKBNK": "Bankacılık", "GARAN": "Bankacılık", "ISCTR": "Bankacılık", "YKBNK": "Bankacılık",
    "HALKB": "Bankacılık", "VAKBN": "Bankacılık", "TSKB": "Bankacılık", "ALBRK": "Bankacılık",
    "KLNMA": "Bankacılık", "SKBNK": "Bankacılık", "QNBTR": "Bankacılık", "ICBCT": "Bankacılık",
    "DENGE": "Bankacılık", "QNBFB": "Bankacılık",
    # GYO
    "EKGYO": "GYO", "ISGYO": "GYO", "TRGYO": "GYO", "HLGYO": "GYO", "AKFGY": "GYO", "ALGYO": "GYO",
    "OZKGY": "GYO", "VKGYO": "GYO", "KLGYO": "GYO", "RYGYO": "GYO", "SRVGY": "GYO", "SNGYO": "GYO",
    "PEKGY": "GYO", "MSGYO": "GYO", "YGGYO": "GYO", "NUGYO": "GYO", "AVGYO": "GYO", "DZGYO": "GYO",
    "IDGYO": "GYO", "PAGYO": "GYO", "AKMGY": "GYO", "A1CAP": "GYO",
    # Sigorta
    "ANSGR": "Sigorta", "AGESA": "Sigorta", "TURSG": "Sigorta", "RAYSG": "Sigorta", "AKGRT": "Sigorta",
    # Holding
    "KCHOL": "Holdingler", "SAHOL": "Holdingler", "SISE": "Holdingler", "ALARK": "Holdingler",
    "DOHOL": "Holdingler", "TKFEN": "Holdingler", "GLYHO": "Holdingler", "POLHO": "Holdingler",
    "BERA": "Holdingler", "AGHOL": "Holdingler", "IHLAS": "Holdingler", "METRO": "Holdingler",
    "NTHOL": "Holdingler", "GSDHO": "Holdingler", "ECZYT": "Holdingler", "CLEBI": "Havayolları ve Hizm.",
    # İletişim
    "TCELL": "İletişim", "TTKOM": "İletişim",
    # Perakende
    "BIMAS": "Perakende - Ticaret", "MGROS": "Perakende - Ticaret", "SOKM": "Perakende - Ticaret",
    "MAVI": "Perakende - Ticaret", "BIZIM": "Perakende - Ticaret", "CRFSA": "Perakende - Ticaret",
    # Havacılık
    "THYAO": "Havayolları ve Hizm.", "PGSUS": "Havayolları ve Hizm.", "TAVHL": "Havayolları ve Hizm.",
    # Otomotiv
    "FROTO": "Otomotiv", "TOASO": "Otomotiv", "TTRAK": "Otomotiv", "DOAS": "Otomotiv",
    "KARSN": "Otomotiv", "OTKAR": "Otomotiv", "ASUZU": "Otomotiv", "TMSN": "Otomotiv",
    # Enerji / elektrik
    "AKSEN": "Elektrik Üretim", "ENKAI": "İnşaat", "ODAS": "Elektrik Üretim", "GWIND": "Elektrik Üretim",
    "AYDEM": "Elektrik Üretim", "ALFAS": "Elektrik Üretim", "ASTOR": "Elektrik Enerji Tech",
    "ENJSA": "Elektrik Üretim", "ZOREN": "Elektrik Üretim", "CWENE": "Elektrik Üretim",
    "SMRTG": "Elektrik Üretim", "AKFYE": "Elektrik Üretim", "CANTE": "Elektrik Üretim",
    "ESEN": "Elektrik Üretim", "NATEN": "Elektrik Üretim", "BIOEN": "Elektrik Üretim",
    # Petrol / kimya
    "TUPRS": "Petrol", "PETKM": "Kimya", "SASA": "Kimya", "GUBRF": "Kimya", "HEKTS": "Kimya",
    "BRISA": "Otomotiv Lastiği", "GOODY": "Otomotiv Lastiği", "AKSA": "Kimya",
    # Madencilik / metal
    "EREGL": "Demir-Çelik Ana", "KRDMD": "Demir-Çelik Ana", "CEMTS": "Demir-Çelik Ana",
    "IZMDC": "Demir-Çelik Ana", "BRSAN": "Demir-Çelik Ana", "KOZAA": "Madencilik",
    "KOZAL": "Madencilik", "IPEKE": "Madencilik", "PRKME": "Madencilik",
    # Çimento / inşaat
    "OYAKC": "Çimento", "AKCNS": "Çimento", "CIMSA": "Çimento", "KONYA": "Çimento",
    "NUHCM": "Çimento", "BUCIM": "Çimento", "BTCIM": "Çimento", "CMBTN": "Çimento",
    "ENKAI": "İnşaat", "ANELE": "İnşaat", "EDIP": "İnşaat", "ORGE": "İnşaat",
    # Gıda
    "ULKER": "Gıda", "CCOLA": "Gıda", "AEFES": "Gıda", "TATGD": "Gıda", "BANVT": "Gıda",
    "PINSU": "Gıda", "KERVT": "Gıda", "FRIGO": "Gıda", "KNFRT": "Gıda", "TBORG": "Gıda",
    "OYLUM": "Gıda", "PENGD": "Gıda",
    # Tekstil
    "KORDS": "Tekstil", "YATAS": "Tekstil", "ARSAN": "Tekstil", "DAGI": "Tekstil",
    "HATEK": "Tekstil", "LUKSK": "Tekstil", "SKTAS": "Tekstil",
    # Teknoloji / yazılım
    "ASELS": "Savunma", "LOGO": "Teknoloji", "INDES": "Teknoloji", "NETAS": "Teknoloji",
    "ARENA": "Teknoloji", "PAPIL": "Teknoloji", "KAREL": "Teknoloji", "ESCOM": "Teknoloji",
    "DESPC": "Teknoloji", "SMART": "Teknoloji",
    # Sağlık
    "ECILC": "Sağlık", "LKMNH": "Sağlık", "MPARK": "Sağlık", "DEVA": "Sağlık", "SELEC": "Sağlık",
    # Medya / turizm
    "DOHOL": "Holdingler", "IHLAS": "Holdingler", "MAALT": "Turizm", "MERIT": "Turizm",
    "TEKTU": "Turizm", "PKENT": "Turizm", "AYCES": "Turizm",
    # Cam / seramik
    "SISE": "Holdingler", "TRKCM": "Cam", "KUTPO": "Seramik", "EGSER": "Seramik",
    # Diğer yaygın
    "ARCLK": "Dayanıklı Tüketim", "VESTL": "Dayanıklı Tüketim", "VESBE": "Dayanıklı Tüketim",
    "BFREN": "Otomotiv Parçası", "PARSN": "Otomotiv Parçası", "JANTS": "Otomotiv Parçası",
    "EGEEN": "Makine", "GEREL": "Makine", "KATMR": "Makine",
    "ALCTL": "İnşaat Malzemesi", "DMSAS": "İnşaat Malzemesi",
    "ISDMR": "Demir-Çelik Ana", "KRDMA": "Demir-Çelik Ana", "KRDMB": "Demir-Çelik Ana",
    "GENIL": "Kimya", "BAGFS": "Kimya", "ALKIM": "Kimya",
    "TSPOR": "Spor", "BJKAS": "Spor", "FENER": "Spor", "GSRAY": "Spor",
    "ADESE": "Perakende - Ticaret", "VAKKO": "Perakende - Ticaret",
    "EUREN": "Kimya", "QUAGR": "Madencilik", "CVKMD": "Madencilik",
}


def fetch_yahoo_sector(session, code):
    """Yahoo quoteSummary assetProfile → sector/industry (TR .IS)."""
    try:
        u = (
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{code}.IS"
            f"?modules=assetProfile"
        )
        rr = session.get(u, headers=HEAD, timeout=12)
        if rr.status_code != 200:
            return None
        j = rr.json()
        ap = (j.get("quoteSummary") or {}).get("result") or []
        if not ap:
            return None
        prof = ap[0].get("assetProfile") or {}
        ind = (prof.get("industry") or "").strip()
        sec = (prof.get("sector") or "").strip()
        # Türkçe eşleme (kaba)
        raw = ind or sec
        if not raw:
            return None
        low = raw.lower()
        if any(x in low for x in ("bank", "financial")):
            return "Bankacılık"
        if "real estate" in low or "reit" in low:
            return "GYO"
        if "insurance" in low:
            return "Sigorta"
        if "telecom" in low or "communication" in low:
            return "İletişim"
        if "airline" in low or "airport" in low:
            return "Havayolları ve Hizm."
        if "auto" in low:
            return "Otomotiv"
        if "oil" in low or "gas" in low or "energy" in low or "electric" in low or "utilities" in low:
            return "Elektrik Üretim" if "utilit" in low or "electric" in low else "Petrol"
        if "steel" in low or "metal" in low or "mining" in low:
            return "Madencilik" if "mining" in low else "Demir-Çelik Ana"
        if "food" in low or "beverage" in low:
            return "Gıda"
        if "retail" in low or "store" in low:
            return "Perakende - Ticaret"
        if "software" in low or "technology" in low or "semiconductor" in low:
            return "Teknoloji"
        if "pharma" in low or "health" in low or "biotech" in low:
            return "Sağlık"
        if "cement" in low or "construction" in low or "building" in low:
            return "Çimento" if "cement" in low else "İnşaat"
        if "chemical" in low:
            return "Kimya"
        if "textile" in low or "apparel" in low:
            return "Tekstil"
        if "aerospace" in low or "defense" in low:
            return "Savunma"
        if "conglomerate" in low or "holding" in low:
            return "Holdingler"
        return raw  # İngilizce kalsın, "Diğer" olmasın
    except Exception:
        return None


def classify_sector(code, r, session=None):
    """Öncelik: r['sector'] (İş Yatırım) → harita (Excel+canlı) → statik → Yahoo → Diğer."""
    code_u = re.sub(r"[^A-Z0-9]", "", str(code or "").strip().upper())
    # 1) ratios kaydından gelen sektör (get_ratios)
    raw = str((r or {}).get("sector", "") or "").strip()
    if raw and raw.lower() not in {"nan", "none", "diğer", "diger", "other", ""}:
        return raw
    # 2) harita
    smap = load_sector_map(session=session)
    if code_u in smap:
        return smap[code_u]
    # 3) statik
    if code_u in _BIST_SECTOR_STATIC:
        return _BIST_SECTOR_STATIC[code_u]
    pat = _sector_from_code_pattern(code_u)
    if pat:
        return pat
    # 4) Yahoo
    if session is not None:
        ys = fetch_yahoo_sector(session, code_u)
        if ys:
            return ys
    return "Diğer"


def _sector_from_code_pattern(code_u: str) -> str:
    """Kod soneki ile sektör tahmini (GYO vb.)."""
    if not code_u:
        return ""
    c = code_u.upper()
    if c.endswith("GYO"):
        return "GYO"
    return ""


def classify_sector_full(code, r=None, session=None, force=False):
    """Tek hisse tam sektör eşlemesi: Excel → statik → kalıp → Yahoo."""
    r = r or {}
    code_u = re.sub(r"[^A-Z0-9]", "", str(code or "").strip().upper())
    smap = load_sector_map()
    if code_u in smap:
        return smap[code_u]
    if not force:
        raw = str(r.get("sector", "") or "").strip()
        if raw and raw.lower() not in {"nan", "none", "diğer", "diger", "other", ""}:
            return raw
    if code_u in _BIST_SECTOR_STATIC:
        return _BIST_SECTOR_STATIC[code_u]
    pat = _sector_from_code_pattern(code_u)
    if pat:
        return pat
    if session is not None:
        ys = fetch_yahoo_sector(session, code_u)
        if ys:
            return ys
    return "Diğer"


def fill_missing_sectors(df, session=None, force_all=False):
    """Sektörü boş/Diğer olan (veya force_all=True ise tüm) hisseleri yeniden eşler."""
    if df is None or df.empty or "Hisse" not in df.columns:
        return df
    # Harita: JSON cache öncelikli (ağ yok → hızlı)
    global _SECTOR_MAP
    if not _SECTOR_MAP:
        load_sector_map(session=session, force_refresh=False)
    out = df.copy()
    if "Sektör" not in out.columns:
        out["Sektör"] = "Diğer"
    n_fix = 0
    for i in out.index:
        sec = str(out.at[i, "Sektör"] or "").strip()
        is_empty = (not sec) or sec.lower() in {"nan", "none", "diğer", "diger", "other", ""}
        if not force_all and not is_empty:
            continue
        code = str(out.at[i, "Hisse"] or "").strip().upper()
        code = re.sub(r"[^A-Z0-9]", "", code)
        if not code:
            continue
        new_sec = classify_sector_full(code, session=session, force=True)
        if new_sec and (is_empty or new_sec != sec):
            out.at[i, "Sektör"] = new_sec
            n_fix += 1
    if n_fix:
        print(f"Sektör eşleme: {n_fix} hisse güncellendi (force_all={force_all})", flush=True)
    diger = int((out["Sektör"].astype(str).str.lower().isin({"diğer", "diger", "other", ""})).sum())
    print(f"Sektör özeti: {out['Sektör'].nunique()} benzersiz · Diğer={diger}", flush=True)
    try:
        for name, cnt in out["Sektör"].value_counts().head(12).items():
            print(f"  · {name}: {cnt}", flush=True)
    except Exception:
        pass
    return out


def remap_sectors_in_excel(path, force_all=True, session=None):
    """Mevcut BIST Excel raporunda sektör sütunlarını yeniden yazar → *_sektor_guncel.xlsx"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if session is None:
        try:
            session = requests.Session()
            session.headers.update(HEAD)
        except Exception:
            session = requests.Session()
    xl = pd.ExcelFile(path)
    frames = {}
    sheets_done = []
    for sh in xl.sheet_names:
        try:
            df = pd.read_excel(xl, sheet_name=sh)
        except Exception:
            continue
        if df is None or df.empty or "Hisse" not in getattr(df, "columns", []):
            frames[sh] = df
            continue
        if "Sektör" not in df.columns:
            df = df.copy()
            df["Sektör"] = "Diğer"
        before = int(df["Sektör"].astype(str).str.lower().isin({"diğer", "diger", "other", "", "nan"}).sum())
        df2 = fill_missing_sectors(df, session=session, force_all=force_all)
        after = int(df2["Sektör"].astype(str).str.lower().isin({"diğer", "diger", "other", "", "nan"}).sum())
        frames[sh] = df2
        sheets_done.append(f"{sh}: Diğer {before}→{after}")
    out_path = path.with_name(path.stem + "_sektor_guncel.xlsx")
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        for sh, df in frames.items():
            if df is None:
                continue
            try:
                df.to_excel(w, sheet_name=str(sh)[:31], index=False)
            except Exception as e:
                print(f"Sayfa yazılamadı {sh}: {e}", flush=True)
    print(f"✓ Sektör güncel rapor: {out_path}", flush=True)
    for s in sheets_done:
        print(f"  {s}", flush=True)
    return str(out_path)


def sector_weights(sector):
    # Value, Quality, Growth, Balance, Cash, Peer
    # v7.1: Value düşürüldü (backtest negatif korele), Quality/Balance/Cash yükseltildi.
    d = {
        'Banka': [.12, .32, .08, .28, .08, .12],
        'Sigorta': [.12, .32, .08, .28, .08, .12],
        'GYO': [.22, .18, .08, .25, .15, .12],
        'Holding/Yatırım': [.18, .20, .08, .24, .15, .15],
        'Telekom': [.15, .24, .08, .22, .18, .13],
        'Perakende': [.15, .24, .12, .18, .18, .13],
        'Havacılık': [.12, .18, .12, .24, .22, .12],
        'Otomotiv': [.15, .24, .12, .18, .18, .13],
        'Enerji': [.15, .24, .12, .22, .14, .13],
    }
    bucket = sector_weight_bucket(sector)
    # Genel/Sanayi varsayılanı da value-azaltılmış
    return d.get(bucket, [.15, .26, .10, .20, .16, .13])


def sector_wacc(sector, net_debt_to_ebitda=np.nan):
    """Nominal TL ke = (rf + ERP) [COST_OF_EQUITY] + sektör primi + kaldıraç primi.

    v192: v191'de sabit mutlak sektör seviyeleri (%18-24) ile clip(0.14, 0.32) karışıyor,
    sanayi hisseleri tavana (%32), holding/GYO ise %20-23'e düşüyordu (10 puanlık yapay fark).
    Artık tüm sektörler aynı tabandan (COST_OF_EQUITY) göreli primle türetilir ve alt sınır
    terminal büyümeye bağlıdır (Gordon paydası patlamasın).
    """
    bucket = sector_weight_bucket(sector)
    prem = SECTOR_KE_PREMIUM.get(bucket, 0.0)
    lev = 0.0
    if pd.notna(net_debt_to_ebitda):
        if net_debt_to_ebitda > 4: lev = 0.04
        elif net_debt_to_ebitda > 2.5: lev = 0.02
        elif net_debt_to_ebitda > 1.5: lev = 0.01
    return float(np.clip(COST_OF_EQUITY + prem + lev, TERMINAL_GROWTH + KE_MIN_SPREAD, 0.45))


def resolve_cost_of_equity(session=None, override=None, rf=None, erp=None):
    """Çalıştırma anında ke türet.

    Öncelik:
      1) override (CLI --ke)
      2) rf + erp (CLI veya parametre)
      3) Canlı kaynak (best-effort): Investing/Yahoo tarzı TR 10Y proxy + sabit ERP bandı
      4) RISK_FREE_FALLBACK + EQUITY_RISK_PREMIUM_FALLBACK
      5) COST_OF_EQUITY sabiti

    Not: Canlı fetch başarısız olursa sessizce fallback'e düşer; ağ hatası modeli bozmaz.
    """
    global COST_OF_EQUITY
    if override is not None and np.isfinite(override) and 0.08 < float(override) < 0.45:
        COST_OF_EQUITY = float(override)
        return COST_OF_EQUITY

    rf_v = float(rf) if rf is not None and np.isfinite(rf) else None
    erp_v = float(erp) if erp is not None and np.isfinite(erp) else None

    if rf_v is None and session is not None:
        # Best-effort: Yahoo ^TNX benzeri yok; TR için kabaca USDTRY/CDS proxy yerine
        # sabit kaynak denemesi (Investing TR 10Y HTML — kırılgan, timeout kısa).
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/TUR10Y=RR?range=5d&interval=1d"
            rr = session.get(url, headers=HEAD, timeout=8)
            if rr.ok:
                j = rr.json()
                closes = j["chart"]["result"][0]["indicators"]["quote"][0].get("close") or []
                closes = [c for c in closes if c is not None]
                if closes:
                    # bazı seriler yüzde puan (örn. 28.5), bazıları 0.285
                    last = float(closes[-1])
                    rf_v = last / 100.0 if last > 1.5 else last
        except Exception:
            rf_v = None

    if rf_v is None:
        rf_v = RISK_FREE_FALLBACK
        print(f"UYARI: TR 10Y çekilemedi/verilmedi → rf yedek değer {RISK_FREE_FALLBACK:.2%} kullanılıyor (--rf ile verin).", flush=True)
    if erp_v is None:
        erp_v = EQUITY_RISK_PREMIUM_FALLBACK

    ke = float(np.clip(rf_v + erp_v, 0.12, 0.40))
    COST_OF_EQUITY = ke
    return ke


def risk_class(x):
    if pd.isna(x): return 'Veri Yok'
    return 'Çok Düşük' if x>=85 else 'Düşük' if x>=70 else 'Orta' if x>=50 else 'Yüksek' if x>=30 else 'Çok Yüksek'

def investment_class(x):
    """Mutlak eşik (fallback). Nihai not enrich_summary içinde percentile ile atanır."""
    if pd.isna(x): return ''
    return 'A+' if x>=90 else 'A' if x>=82 else 'B+' if x>=74 else 'B' if x>=66 else 'C+' if x>=58 else 'C' if x>=50 else 'D' if x>=40 else 'F'

def investment_class_from_percentile(p):
    """Evren içi percentile (0-1): top %5=A+, %15=A, %30=B+, %45=B, %60=C+, %75=C, %90=D, rest=F."""
    if pd.isna(p): return ''
    if p >= 0.95: return 'A+'
    if p >= 0.85: return 'A'
    if p >= 0.70: return 'B+'
    if p >= 0.55: return 'B'
    if p >= 0.40: return 'C+'
    if p >= 0.25: return 'C'
    if p >= 0.10: return 'D'
    return 'F'

def valuation_class2(x):
    if pd.isna(x): return 'Hesaplanamadı'
    return 'Derin İskonto' if x>=.50 else 'İskontolu' if x>=.20 else 'Makul' if x>=-.10 else 'Pahalı' if x>=-.30 else 'Çok Pahalı'

def final_verdict(score, val, risk, conf, grade=None):
    """Skor mutlak veya percentile-kalibre; grade verilirse onu da kullanır."""
    if pd.isna(score): return 'Veri Yetersiz'
    # Percentile notlarına göre yumuşak eşikler (v4: A/B bandı yeterli)
    strong = grade in ('A+', 'A') or (pd.notna(score) and score >= 75)
    good = grade in ('A+', 'A', 'B+') or (pd.notna(score) and score >= 65)
    ok = grade in ('A+', 'A', 'B+', 'B', 'C+') or (pd.notna(score) and score >= 55)
    if strong and val in ('Derin İskonto','İskontolu') and risk in ('Çok Düşük','Düşük') and (pd.isna(conf) or conf>=60):
        return 'GÜÇLÜ ADAY'
    if good and val in ('Derin İskonto','İskontolu','Makul') and risk != 'Çok Yüksek':
        return 'POZİTİF'
    if ok and val in ('Makul','İskontolu','Derin İskonto'):
        return 'İZLE / POZİTİF'
    if val in ('Çok Pahalı','Pahalı') and (strong or good):
        return 'KALİTELİ AMA PAHALI'
    if risk in ('Yüksek','Çok Yüksek') and ok:
        return 'YÜKSEK RİSK'
    return 'NÖTR / İZLE'

def wacc_from_ke(ke, cap, gross_debt, tax_rate=0.25, credit_spread=0.02):
    """Sermaye yapısına göre WACC. kd = rf + kredi marjı; rf ≈ ke − ERP.
    Borç ağırlığı [0, %50] aralığında sınırlanır (piyasa değeri bazlı)."""
    if pd.isna(ke):
        return np.nan
    if pd.isna(cap) or cap <= 0 or pd.isna(gross_debt) or gross_debt <= 0:
        return float(ke)
    kd = max(ke - EQUITY_RISK_PREMIUM_FALLBACK + credit_spread, 0.0)
    wd = float(np.clip(gross_debt / (gross_debt + cap), 0.0, 0.50))
    return float((1 - wd) * ke + wd * kd * (1 - tax_rate))


def fcff_dcf(revenue, ebit_margin, tax_rate, da, capex, nwc, shares, wacc, terminal_g, growth, years=5,
             net_debt=0.0, minority=0.0):
    """FCFF DCF: Revenue -> EBIT -> NOPAT + D&A - CapEx - ΔNWC -> EV -> (EV - net borç - azınlık) / hisse.

    v192: (1) net borç/azınlık köprüsü eklendi (v191 EV'yi doğrudan hisse değeri sayıyordu),
          (2) iskonto WACC ile yapılmalı (çağıran wacc_from_ke ile verir),
          (3) büyüme nominal; enflasyon patikası gibi terminal_g'ye doğru lineer söner,
          (4) büyüme kırpması nominal ortama uyarlandı (GROWTH_CLIP_*).
    """
    if any(pd.isna(x) for x in [revenue,ebit_margin,da,capex,nwc,shares,wacc,terminal_g]) or shares<=0 or wacc<=terminal_g+0.02: return np.nan
    # v192: bozuk girdi koruması (ör. çeyreklemeden negatif hasılat çıkması → marj %312 gibi anlamsız değerler)
    if revenue<=0 or abs(ebit_margin)>1.0: return np.nan
    da_pct=da/max(revenue,1e-9); capex_pct=capex/max(revenue,1e-9); nwc_pct=nwc/max(revenue,1e-9)
    growth=float(np.clip(growth,GROWTH_CLIP_LO,GROWTH_CLIP_HI)); pv=0.0; prev_nwc=nwc; last_fcff=np.nan; rev=revenue
    for i in range(1,years+1):
        g_i=terminal_g+(growth-terminal_g)*(1-i/(years+1))
        rev=rev*(1+g_i); ebit=rev*ebit_margin; nopat=ebit*(1-tax_rate)
        da_i=rev*da_pct; capex_i=rev*capex_pct; nwc_i=rev*nwc_pct
        fcff=nopat+da_i-capex_i-(nwc_i-prev_nwc); prev_nwc=nwc_i; last_fcff=fcff
        pv += fcff/((1+wacc)**i)
    tv=last_fcff*(1+terminal_g)/(wacc-terminal_g)
    ev=pv+tv/((1+wacc)**years)
    nd=0.0 if pd.isna(net_debt) else float(net_debt)
    mi=0.0 if pd.isna(minority) else float(minority)
    eq=ev-nd-mi
    return (eq/shares) if eq>0 else np.nan   # negatif özkaynak değeri anlamsız → NaN (yedek yönteme düşer)



def reverse_dcf_growth(price, fcf, shares, ke, terminal_g=None, years=FORECAST_YEARS):
    """Fiyatı açıklayan sabit FCF büyümesi g (basit explicit + Gordon terminal).
    price = sum_t FCF*(1+g)^t/(1+ke)^t + terminal/(1+ke)^years  -> g için grid arama.
    """
    terminal_g = TERMINAL_GROWTH if terminal_g is None else terminal_g
    if any(pd.isna(x) for x in [price, fcf, shares, ke]) or shares <= 0 or fcf <= 0 or price <= 0:
        return np.nan
    if ke <= terminal_g + 0.005:
        return np.nan
    target = price * shares
    best_g, best_err = np.nan, 1e99
    # v192: nominal ortam için geniş grid; v191'de [-%15, +%35] aralığında yarıdan fazlası tavana yapışıyordu.
    _grid = np.linspace(-0.20, 1.20, 141)
    for g in _grid:
        pv = 0.0
        ok = True
        for t in range(1, years + 1):
            cf = fcf * ((1 + g) ** t)
            pv += cf / ((1 + ke) ** t)
        last = fcf * ((1 + g) ** years)
        if ke <= terminal_g:
            ok = False
        else:
            tv = last * (1 + terminal_g) / (ke - terminal_g)
            pv += tv / ((1 + ke) ** years)
        if not ok or not np.isfinite(pv):
            continue
        err = abs(pv - target)
        if err < best_err:
            best_err, best_g = err, g
    # Grid ucuna yapışan çözüm bilgi taşımaz (fiyat aralığın dışında) → NaN
    if not np.isfinite(best_g) or best_g >= _grid[-1] - 1e-9 or best_g <= _grid[0] + 1e-9:
        return np.nan
    return float(best_g)


def sloan_ratio(ni_ttm, ocf_ttm, avg_assets):
    """(NI - OCF) / Avg Assets — yüksek pozitif = accrual kalitesi düşük."""
    return div(ni_ttm - ocf_ttm, avg_assets)


def beneish_m_proxy(m, quarters, c, yoy, Rev_ttm, NI_ttm, OCF_ttm, A, CA, CL, DEP_ttm):
    """Klasik Beneish'in veri kısıtlı proxy'si (DSRI, GMI, AQI, SGI, DEPI, SGAI, LVGI, TATA).
    Tam 8 değişken her şirkette yok; mevcut olanlarla yaklaşık M üretilir.
    M > -1.78 şüpheli (klasik eşik); burada skor 0-1 (düşük = daha temiz).
    """
    try:
        rev0 = m["revenue"][c]; rev1 = m["revenue"][yoy]
        rec0 = m.get("ca", [np.nan]*8)[c]  # alacak ayrı yoksa CA proxy (zayıf)
        # DSRI: alacak/satış değişimi — alacak yoksa 1.0 nötr
        dsri = 1.0
        gmi = 1.0
        gm0 = div(m["gross"][c], rev0) if pd.notna(rev0) and rev0 else np.nan
        gm1 = div(m["gross"][yoy], rev1) if pd.notna(rev1) and rev1 else np.nan
        if pd.notna(gm0) and pd.notna(gm1) and gm0 != 0:
            gmi = gm1 / gm0 if gm0 else 1.0
        sgi = div(rev0, rev1) if pd.notna(rev1) and rev1 else 1.0
        # DEPI: amortisman oranı düşüşü
        dep0 = abs(m["dep"][c]) if pd.notna(m["dep"][c]) else np.nan
        dep1 = abs(m["dep"][yoy]) if pd.notna(m["dep"][yoy]) else np.nan
        ppe_proxy0 = A  # kaba
        depi = 1.0
        if pd.notna(dep0) and pd.notna(dep1) and pd.notna(A) and A and dep0 + dep1 > 0:
            r0 = dep0 / (dep0 + A * 0.3 + 1e-9)
            r1 = dep1 / (dep1 + (m["assets"][yoy] or A) * 0.3 + 1e-9)
            depi = r1 / r0 if r0 else 1.0
        lvgi = 1.0
        if pd.notna(CL) and pd.notna(A) and A and pd.notna(m["cl"][yoy]) and pd.notna(m["assets"][yoy]) and m["assets"][yoy]:
            lvgi = (CL / A) / (m["cl"][yoy] / m["assets"][yoy])
        tata = div(NI_ttm - OCF_ttm, A) if pd.notna(A) and A else 0.0
        # Beneish (1999) katsayıları — eksik DSRI/AQI/SGAI nötr 1.0
        m_score = (
            -4.84
            + 0.920 * dsri
            + 0.528 * (gmi if pd.notna(gmi) else 1)
            + 0.404 * 1.0  # AQI nötr
            + 0.892 * (sgi if pd.notna(sgi) else 1)
            + 0.115 * (depi if pd.notna(depi) else 1)
            - 0.172 * 1.0  # SGAI nötr
            + 4.679 * (tata if pd.notna(tata) else 0)
            - 0.327 * (lvgi if pd.notna(lvgi) else 1)
        )
        return float(m_score)
    except Exception:
        return np.nan


def trend_slope_score(series, quarters=None, higher_is_better=True, already_quarterly=False):
    """Çeyrek serisinde basit doğrusal trend; 0-1 skor.
    already_quarterly=True ise (oran serileri) quarterize uygulanmaz.
    series index 0 = en yeni dönem (quarters ile aynı sıra).
    """
    if already_quarterly:
        q = list(series)
    else:
        q = quarterize(series, quarters) if quarters is not None else list(series)
    vals = [v for v in reversed(q) if pd.notna(v)]  # eski -> yeni
    if len(vals) < 4:
        return np.nan
    x = np.arange(len(vals), dtype=float)
    y = np.array(vals, dtype=float)
    scale = np.mean(np.abs(y)) + 1e-9
    slope = np.polyfit(x, y, 1)[0] / scale
    s = float(np.clip(0.5 + slope * 2.0, 0, 1))
    return s if higher_is_better else 1.0 - s




def residual_income_value(
    book_equity, ni_ttm, shares, ke, terminal_g=TERMINAL_GROWTH,
    years=FORECAST_YEARS, payout=0.30, roe_target=None,
):
    """Residual Income valuation (hisse başı).

    V0 = B0 + Σ PV(RI_t) + PV(TV)
    RI_t = NI_t - ke * B_{t-1}
    NI_t = ROE_t * B_{t-1}
    ROE mean-reverts from current toward roe_target (default: clip current to [ke, ke+0.08])
    B_t = B_{t-1} + NI_t * (1 - payout)
    TV = RI_T * (1+g) / (ke - g)   [Gordon on residual income]
    """
    if any(pd.isna(x) for x in [book_equity, ni_ttm, shares, ke]) or shares <= 0:
        return np.nan, np.nan, np.nan, np.nan
    if book_equity <= 0:
        return np.nan, np.nan, np.nan, np.nan
    if ke <= terminal_g + 0.005:
        return np.nan, np.nan, np.nan, np.nan

    B = float(book_equity)
    roe0 = float(ni_ttm) / B if B else np.nan
    if pd.isna(roe0):
        return np.nan, np.nan, np.nan, np.nan

    # Hedef ROE: aşırı yüksek/düşük ROE'yi ke bandına çek
    if roe_target is None or pd.isna(roe_target):
        roe_target = float(np.clip(roe0, ke * 0.5, ke + 0.10))
    else:
        roe_target = float(np.clip(roe_target, -0.20, 0.50))

    payout = float(np.clip(payout, 0.0, 0.9))
    pv_ri = 0.0
    ri_last = np.nan
    ri1 = np.nan
    Bt = B
    for t in range(1, years + 1):
        # Lineer mean-reversion ROE
        w = t / (years + 1)
        roe_t = roe0 * (1 - w) + roe_target * w
        ni_t = roe_t * Bt
        ri_t = ni_t - ke * Bt
        if t == 1:
            ri1 = ri_t
        pv_ri += ri_t / ((1 + ke) ** t)
        Bt = Bt + ni_t * (1 - payout)
        if Bt <= 0:
            Bt = max(Bt, book_equity * 0.05)  # taban
        ri_last = ri_t

    if ke <= terminal_g:
        return np.nan, ri1, roe0, np.nan
    tv = ri_last * (1 + terminal_g) / (ke - terminal_g)
    # Negatif TV'yi sınırla (sürekli değer yakımı)
    if tv < 0:
        tv = ri_last / ke  # amortize residual without growth
    v_total = B + pv_ri + tv / ((1 + ke) ** years)
    per_share = v_total / shares
    return float(per_share), float(ri1) if pd.notna(ri1) else np.nan, float(roe0), float(roe0 - ke)



def analyze(code, m, r, quarters):
    sanitize_flow_items(m)
    # Son raporlanmış dönem ve 1 yıl önceki aynı dönem.
    c = 0
    yoy = 4
    prior_year = 2 if len(quarters) > 2 else None

    A = m["assets"][c]; CA = m["ca"][c]; CL = m["cl"][c]
    L = m["liab"][c]; E = m["equity"][c]
    NI = m["net"][c]; Rev = m["revenue"][c]; GP = m["gross"][c]
    EBIT = m["ebit"][c]; RE = m["retained"][c]
    LD = m["ld"][c]; SD = m["sd"][c]; Cash = m["cash"][c]
    # Bilançodaki "Nakit ve Nakit Benzerleri" (Cash, EV/Net Borç hesaplarında STANDART olarak
    # kullanılır) ile nakit akış tablosundaki GERÇEK "Dönem Sonu Nakit" kalemi (PeriodEndCash,
    # bazen bilanço rakamından farklı olabilir — örn. vade/kapsam farkı) ayrı tutulur.
    PeriodEndCash = m["period_end_cash"][c]
    NIy = m["net"][prior_year] if prior_year is not None else np.nan
    Ey = m["equity"][prior_year] if prior_year is not None else np.nan

    sector = classify_sector(code, r)
    price = r.get("price", np.nan)
    cap = r.get("cap", np.nan)
    shares = r.get("shares", np.nan)
    shares_est = False

    if pd.isna(shares) and pd.notna(cap) and pd.notna(price) and price:
        shares = cap / price
        shares_est = True
    if pd.isna(shares):
        paid = m["paid_capital"][c]
        if pd.notna(paid) and paid > 0:
            shares = paid
            shares_est = True

    # TTM / normalize edilmiş temel kalemler.
    NI_ttm = ttm_from_cumulative(m["net"], quarters)
    Rev_ttm = ttm_from_cumulative(m["revenue"], quarters)
    EBIT_ttm = ttm_from_cumulative(m["ebit"], quarters)
    OCF_ttm = ttm_from_cumulative(m["ocf"], quarters)
    DEP_ttm = abs(ttm_from_cumulative(m["dep"], quarters)) if pd.notna(ttm_from_cumulative(m["dep"], quarters)) else np.nan
    CAPEX_ttm = abs(ttm_from_cumulative(m["capex"], quarters)) if pd.notna(ttm_from_cumulative(m["capex"], quarters)) else np.nan

    EBITDA_ttm = EBIT_ttm + DEP_ttm if pd.notna(EBIT_ttm) and pd.notna(DEP_ttm) else EBIT_ttm
    # DÜZELTME: Önce İş Yatırım'ın hazır verdiği "Serbest Nakit Akım" kalemi (TTM) kullanılır;
    # bu, kendi OCF-CapEx hesabımızdan daha güvenilirdir (kalem eşleştirme hatalarına karşı daha dayanıklı).
    # Yalnızca bu kalem bulunamazsa OCF-CapEx'e düşülür.
    FCF_direct_ttm = ttm_from_cumulative(m["fcf_direct"], quarters)
    FCF_ttm = FCF_direct_ttm if pd.notna(FCF_direct_ttm) else (
        OCF_ttm - CAPEX_ttm if pd.notna(OCF_ttm) and pd.notna(CAPEX_ttm) else np.nan
    )

    avg_assets = safe_mean([A, m["assets"][yoy]])
    avg_equity = safe_mean([E, m["equity"][yoy]])
    invested_capital = (E if pd.notna(E) else 0) + (SD if pd.notna(SD) else 0) + (LD if pd.notna(LD) else 0) - (Cash if pd.notna(Cash) else 0)

    eps = div(NI_ttm, shares)
    bvps = div(E, shares)
    gn = graham_number(eps, bvps)

    # Çarpanlar: kaynaktan gelen oran öncelikli, ama iç tutarlılık için hesaplananlar da tutulur.
    pe_calc = div(cap, NI_ttm)
    pb_calc = div(cap, E)
    pe = r.get("pe", np.nan) if pd.notna(r.get("pe", np.nan)) else pe_calc
    pb = r.get("pb", np.nan) if pd.notna(r.get("pb", np.nan)) else pb_calc

    net_debt = (SD if pd.notna(SD) else 0) + (LD if pd.notna(LD) else 0) - (Cash if pd.notna(Cash) else 0)
    ev = cap + net_debt if pd.notna(cap) else np.nan
    ev_ebit = div(ev, EBIT_ttm)
    ev_ebitda = div(ev, EBITDA_ttm)

    wc = CA - CL if pd.notna(CA) and pd.notna(CL) else np.nan
    cr = div(CA, CL)
    quick = div(CA, CL)  # İş Yatırım kalemlerinde stok ayrı bulunmuyorsa cari oran proxy olarak bırakılır.

    roe = div(NI_ttm, avg_equity)
    roa = div(NI_ttm, avg_assets)
    roic = div(EBIT_ttm * (1 - 0.25), invested_capital)
    net_margin = div(NI_ttm, Rev_ttm)
    ebit_margin = div(EBIT_ttm, Rev_ttm)
    ebitda_margin = div(EBITDA_ttm, Rev_ttm)
    fcf_margin = div(FCF_ttm, Rev_ttm)
    fcf_to_ni = div(FCF_ttm, NI_ttm)
    earnings_yield = div(NI_ttm, cap)
    fcf_yield = div(FCF_ttm, cap)

    # YoY kalite / trend.
    NI_prev = ttm_prev(m["net"], quarters)          # v192.3: TTM ↔ TTM (v191: TTM ↔ YTD)
    Rev_prev = ttm_prev(m["revenue"], quarters)
    GP_prev = ttm_prev(m["gross"], quarters)
    LD_prev = m["ld"][yoy]
    CA_prev = m["ca"][yoy]
    CL_prev = m["cl"][yoy]

    gm = div(GP, Rev)
    gm_prev = div(GP_prev, Rev_prev)
    at = div(Rev_ttm, avg_assets)
    at_prev = div(Rev_prev, m["assets"][yoy])

    # Piotroski F-score: 9 standart kriter.
    roa_current = div(NI_ttm, avg_assets)
    roa_previous = div(NI_prev, safe_mean([m["assets"][yoy], m["assets"][prior_year]]) if prior_year is not None else np.nan)
    cfo_positive = OCF_ttm > 0 if pd.notna(OCF_ttm) else None
    roa_positive = roa_current > 0 if pd.notna(roa_current) else None
    delta_roa = roa_current > roa_previous if pd.notna(roa_current) and pd.notna(roa_previous) else None
    accrual = OCF_ttm > NI_ttm if pd.notna(OCF_ttm) and pd.notna(NI_ttm) else None
    leverage = net_debt < (((m["sd"][yoy] if pd.notna(m["sd"][yoy]) else 0) + (m["ld"][yoy] if pd.notna(m["ld"][yoy]) else 0) - (m["cash"][yoy] if pd.notna(m["cash"][yoy]) else 0))) if pd.notna(net_debt) else None
    liquidity = cr > div(CA_prev, CL_prev) if pd.notna(cr) and pd.notna(div(CA_prev, CL_prev)) else None
    # Equity issuance: sermaye artışı yoksa olumlu. Veri yoksa nötr/bilinmiyor.
    shares_prev = r.get("shares_prev", np.nan)
    eq_issue = shares <= shares_prev if pd.notna(shares) and pd.notna(shares_prev) else None
    margin_improves = gm > gm_prev if pd.notna(gm) and pd.notna(gm_prev) else None
    turnover_improves = at > at_prev if pd.notna(at) and pd.notna(at_prev) else None

    f_items = [roa_positive, cfo_positive, delta_roa, accrual, leverage, liquidity, eq_issue, margin_improves, turnover_improves]
    f_valid = sum(x is not None for x in f_items)
    f_score = sum(bool(x) for x in f_items if x is not None)
    f_rate = div(f_score, f_valid) if f_valid else np.nan

    # Altman Z (klasik form). Finansal kurumlarda ayrıca işaretlenir.
    # DÜZELTME: L (Toplam Yükümlülükler) ve RE (Geçmiş Yıllar K/Z) bazı
    # şirketlerde kalem adı farklılığı yüzünden bulunamıyordu, Z hep NaN
    # kalıyordu. Bulunamazlarsa muhasebe özdeşliğinden türetilir:
    #   Toplam Varlıklar = Toplam Yükümlülükler + Özkaynak  ->  L = A - E
    #   Özkaynak ≈ Ödenmiş Sermaye + Geçmiş Yıllar K/Z + diğer  ->  RE ≈ E - Ödenmiş Sermaye (yaklaşık)
    L_est = False
    RE_est = False
    if pd.isna(L) and pd.notna(A) and pd.notna(E):
        L = A - E
        L_est = True
    if pd.isna(RE) and pd.notna(E):
        paid_c = m["paid_capital"][c]
        if pd.notna(paid_c):
            RE = E - paid_c
            RE_est = True

    altman = (
        1.2 * div(wc, A) +
        1.4 * div(RE, A) +
        3.3 * div(EBIT_ttm, A) +
        0.6 * div(cap, L) +
        div(Rev_ttm, A)
    ) if all(pd.notna(x) for x in [wc, A, RE, EBIT_ttm, cap, L, Rev_ttm]) and A and L else np.nan
    altman_note = "Yaklaşık (L/RE türetildi)" if pd.notna(altman) and (L_est or RE_est) else ("Tam" if pd.notna(altman) else "Veri Yok")

    # Değerleme: sektör WACC + ağırlıklı yöntemler.
    ncav = div(CA - L, shares)
    _debt_ebitda_tmp = div(net_debt, EBITDA_ttm) if pd.notna(EBITDA_ttm) else np.nan
    ke = sector_wacc(sector, _debt_ebitda_tmp)
    # Senaryo iskonto oranları: sadece büyüme değil, ke de değişir (daha tutarlı).
    ke_bear = float(np.clip(ke + BEAR_KE_SPREAD, TERMINAL_GROWTH + 0.02, 0.45))
    ke_bull = float(np.clip(ke + BULL_KE_SPREAD, TERMINAL_GROWTH + 0.02, 0.40))
    # v192: kapitalize kazanç = EPS × (1+g0)/(ke−g0); g0 = reel büyüme 0'a karşılık gelen nominal g
    ke_eq, tg_eq, g0_eq = equity_basis(ke)
    cap_earnings = (eps * (1.0 + g0_eq) / (ke_eq - g0_eq)
                    if pd.notna(eps) and eps > 0 and ke_eq - g0_eq >= 0.04 else np.nan)
    # v12 EPV (v192: nominal reel-sıfır büyüme + net borç köprüsü)
    epv_ps = earnings_power_value(
        EBIT_ttm, 0.25, ke_eq, shares,
        maint_capex=(DEP_ttm if pd.notna(DEP_ttm) else np.nan),
        da=(DEP_ttm if pd.notna(DEP_ttm) else np.nan),
        net_debt=net_debt, g0=g0_eq,
    )
    epv_margin = div(epv_ps - price, price) if pd.notna(epv_ps) and pd.notna(price) and price > 0 else np.nan
    ev_ebit_s = ev_ebit_band_score(ev_ebit)

    base_growth = (Rev_ttm/Rev_prev-1) if pd.notna(Rev_ttm) and pd.notna(Rev_prev) and Rev_prev>0 else BASE_GROWTH
    base_growth = float(np.clip(base_growth, GROWTH_CLIP_LO, GROWTH_CLIP_HI))
    # v192: üç senaryo AYNI yöntemle (FCFF → WACC → net borç köprüsü); yalnızca büyüme ve ke değişir.
    # Yedek: FCF (kaldıraçlı) tabanlı value_from_fcf.
    _gross_debt = (SD if pd.notna(SD) else 0) + (LD if pd.notna(LD) else 0)
    _da = DEP_ttm if pd.notna(DEP_ttm) else 0
    _cx = CAPEX_ttm if pd.notna(CAPEX_ttm) else 0
    # DCF için OPERASYONEL işletme sermayesi: nakit ve kısa vadeli finansal borç hariç.
    # (v191 CA−CL kullanıyordu; nakit/borç büyümeyle "ΔNWC" sanılıp FCFF'i yapay biçimde eziyordu.)
    _wc = ((CA - (Cash if pd.notna(Cash) else 0)) - (CL - (SD if pd.notna(SD) else 0))) if pd.notna(CA) and pd.notna(CL) else 0
    wacc_base = wacc_from_ke(ke, cap, _gross_debt)
    wacc_bear = wacc_from_ke(ke_bear, cap, _gross_debt)
    wacc_bull = wacc_from_ke(ke_bull, cap, _gross_debt)
    g_bear = min(base_growth, BEAR_GROWTH)
    g_bull = max(base_growth, BULL_GROWTH)
    dcf_base = fcff_dcf(Rev_ttm, ebit_margin, .25, _da, _cx, _wc, shares, wacc_base, TERMINAL_GROWTH, base_growth, FORECAST_YEARS, net_debt=net_debt)
    dcf_bear = fcff_dcf(Rev_ttm, ebit_margin, .25, _da, _cx, _wc, shares, wacc_bear, TERMINAL_GROWTH, g_bear, FORECAST_YEARS, net_debt=net_debt)
    dcf_bull = fcff_dcf(Rev_ttm, ebit_margin, .25, _da, _cx, _wc, shares, wacc_bull, TERMINAL_GROWTH, g_bull, FORECAST_YEARS, net_debt=net_debt)
    if pd.isna(dcf_base): dcf_base = value_from_fcf(FCF_ttm, shares, ke, BASE_GROWTH, FORECAST_YEARS)
    if pd.isna(dcf_bear): dcf_bear = value_from_fcf(FCF_ttm, shares, ke_bear, BEAR_GROWTH, FORECAST_YEARS)
    if pd.isna(dcf_bull): dcf_bull = value_from_fcf(FCF_ttm, shares, ke_bull, BULL_GROWTH, FORECAST_YEARS)

    # Çarpan-implied değer (sektör-bağımsız kaba: F/K=12, PD/DD=1.5 hedef)
    mult_pe = eps * 12 if pd.notna(eps) and eps > 0 else np.nan
    mult_pb = bvps * 1.5 if pd.notna(bvps) and bvps > 0 else np.nan
    mult_val = safe_mean([mult_pe, mult_pb])

    # Residual Income (normalize NI: TTM ile medyan 4Ç karışımı)
    _ni_q_ri = [v for v in quarterize(m["net"], quarters) if pd.notna(v)]
    ni_med_ri = float(np.median(_ni_q_ri[:4])) * 4 if len(_ni_q_ri) >= 4 else NI_ttm   # v192.3: [:4] = SON 4 çeyrek
    ni_for_ri = safe_mean([NI_ttm, ni_med_ri]) if pd.notna(NI_ttm) else ni_med_ri
    # Sektör payout varsayılanı
    payout_map = {'Banka': 0.25, 'Sigorta': 0.20, 'GYO': 0.50, 'Perakende': 0.25, 'Telekom': 0.40}
    payout_i = payout_map.get(sector_weight_bucket(sector), 0.30)
    fair_ri, ri1, roe_for_ri, roe_ke_spread = residual_income_value(
        E, ni_for_ri, shares, ke_eq, tg_eq, FORECAST_YEARS, payout_i, None
    )
    ri_margin = div(fair_ri - price, price) if pd.notna(fair_ri) else np.nan
    ri_on_book = div(ri1, E) if pd.notna(ri1) and pd.notna(E) and E else np.nan

    # Ağırlıklı adil değer — RI eklendi
    _bkt = sector_weight_bucket(sector)
    if _bkt == 'GYO':
        parts = [(dcf_base, 0.18), (gn, 0.09), (ncav, 0.26), (cap_earnings, 0.07), (mult_val, 0.10), (fair_ri, 0.18), (epv_ps, 0.12)]
    elif _bkt == 'Holding/Yatırım':
        parts = [(dcf_base, 0.18), (gn, 0.09), (ncav, 0.18), (cap_earnings, 0.08), (mult_val, 0.12), (fair_ri, 0.22), (epv_ps, 0.13)]
    elif _bkt in ('Banka', 'Sigorta'):
        parts = [(dcf_base, 0.09), (gn, 0.13), (ncav, 0.05), (cap_earnings, 0.12), (mult_val, 0.16), (fair_ri, 0.30), (epv_ps, 0.15)]
    else:
        parts = [(dcf_base, 0.28), (gn, 0.12), (mult_val, 0.15), (ncav, 0.05), (cap_earnings, 0.07), (fair_ri, 0.18), (epv_ps, 0.15)]
    # v192: uç değer koruması — fiyatın FAIR_VALUE_MAX_MULT katından büyük bileşen (birim/veri hatası)
    # ağırlıklı ortalamayı bozmasın; dışlanan bileşenler Model Uyarısı'na yazılır.
    _sanity_notes = []
    def _sane(v, label=None):
        if pd.isna(v) or v <= 0:
            return False
        if pd.notna(price) and price > 0 and v > FAIR_VALUE_MAX_MULT * price:
            if label:
                _sanity_notes.append(label)
            return False
        return True
    _labels = ['DCF', 'Graham', 'NCAV', 'KapKazanç', 'Çarpan', 'RI', 'EPV']
    _vals = [dcf_base, gn, ncav, cap_earnings, mult_val, fair_ri, epv_ps]
    w_sum = 0.0; v_sum = 0.0
    for v, w in parts:
        _lab = next((l for l, x in zip(_labels, _vals) if pd.notna(x) and pd.notna(v) and x == v), None)
        if _sane(v, _lab):
            v_sum += v * w; w_sum += w
    fair_value = (v_sum / w_sum) if w_sum > 0 else np.nan
    # Medyan yedek (eski davranış ile karşılaştırma için)
    valid_values = [v for v in _vals if _sane(v)]
    if pd.isna(fair_value) and valid_values:
        fair_value = float(np.median(valid_values))
    fair_margin = div(fair_value - price, price)
    if pd.notna(fair_margin):
        fair_margin = float(np.clip(fair_margin, FAIR_MARGIN_MIN, FAIR_MARGIN_MAX))

    # Method agreement: birbirinden çok uzak yöntemlerde güveni düşür.
    if len(valid_values) >= 3:
        dispersion = np.std(valid_values) / max(np.mean(valid_values), 1e-9)
        valuation_agreement = max(0.0, 1.0 - min(dispersion, 1.0))
    elif len(valid_values) == 2:
        valuation_agreement = 0.5
    else:
        valuation_agreement = 0.0

    fair_class = (
        "Derin İskonto" if pd.notna(fair_margin) and fair_margin >= 0.50 else
        "İskontolu" if pd.notna(fair_margin) and fair_margin >= 0.25 else
        "Makul" if pd.notna(fair_margin) and fair_margin >= -0.10 else
        "Pahalı" if pd.notna(fair_margin) else "Hesaplanamadı"
    )

    # Graham kriterleri (klasik tarama + net-net / güvenlik marjı)
    # G1 F/K≤15  G2 PD/DD≤1.5  G3 F/K×PD/DD≤22.5  G4 Cari≥2
    # G5 Uzun borç ≤ işletme sermayesi  G6 Net kâr>0  G7 FCF>0
    gtests = [
        pe > 0 and pe <= 15 if pd.notna(pe) else None,
        pb > 0 and pb <= 1.5 if pd.notna(pb) else None,
        pe * pb <= 22.5 if pd.notna(pe) and pd.notna(pb) and pe > 0 and pb > 0 else None,
        cr >= 2 if pd.notna(cr) else None,
        LD <= wc if pd.notna(LD) and pd.notna(wc) else None,
        NI_ttm > 0 if pd.notna(NI_ttm) else None,
        FCF_ttm > 0 if pd.notna(FCF_ttm) else None,
    ]
    g_labels = [
        "G1 F/K≤15",
        "G2 PD/DD≤1.5",
        "G3 F/K×PD/DD≤22.5",
        "G4 Cari≥2",
        "G5 UB≤İS",
        "G6 NetKâr>0",
        "G7 FCF>0",
    ]
    g_detail_parts = []
    for lab, t in zip(g_labels, gtests):
        if t is None:
            g_detail_parts.append(f"{lab}:N/A")
        elif t:
            g_detail_parts.append(f"{lab}:GEÇTİ")
        else:
            g_detail_parts.append(f"{lab}:KALDI")
    graham_kriter_detay = " | ".join(g_detail_parts)
    g_valid = sum(x is not None for x in gtests)
    g_score = sum(bool(x) for x in gtests if x is not None)
    g_rate = div(g_score, g_valid) if g_valid else np.nan

    # --- DEĞER YATIRIMCILIĞI (KLASİK GRAHAM) SONUCU ---
    # Klasik Graham güvenlik marjı: Graham Sayısı (gn) fiyatın ne kadar üzerinde/altında?
    graham_margin = div(gn - price, price) if pd.notna(gn) and pd.notna(price) else np.nan
    value_investing_verdict = (
        "İskontolu (Değer Yatırımcılığına Uygun)" if pd.notna(graham_margin) and graham_margin >= 0.33 and pd.notna(g_rate) and g_rate >= 0.75 else
        "İzlemeye Değer" if pd.notna(graham_margin) and graham_margin >= 0 and pd.notna(g_rate) and g_rate >= 0.50 else
        "Uygun Değil" if pd.notna(g_rate) else "Veri Yetersiz"
    )
    # Klasik "net-net" (derin değer) kontrolü: fiyat, NCAV'ın %67'sinin altındaysa
    # Graham'ın en katı ve en güvenli kategorisi sayılır.
    is_net_net = (price < ncav * NET_NET_MARGIN) if pd.notna(price) and pd.notna(ncav) and ncav > 0 else False
    net_net_note = "Net-Net (Derin Değer)" if is_net_net else ("Net-Net Değil" if pd.notna(ncav) else "Veri Yok")

    # --- NAKİT BAZLI UCUZLUK: Serbest Nakit Akışı ve Dönem Sonu Nakit ---
    # Net Nakit = Dönem Sonu Nakit - (Kısa+Uzun Vadeli Finansal Borç); net_debt zaten bunun negatifi.
    # Piyasa değerine göre yüksek net nakit, klasik Graham tarzı bir "ucuzluk/güvenlik" sinyalidir
    # (şirket piyasada, sahip olduğu net nakite yakın veya altında bir değerle işlem görüyor demektir).
    net_cash = -net_debt if pd.notna(net_debt) else np.nan
    net_cash_to_cap = div(net_cash, cap)
    cash_cheapness_note = (
        "Net Nakit Pozisyonu Güçlü (Piyasa Değerinin >%30'u)" if pd.notna(net_cash_to_cap) and net_cash_to_cap >= 0.30 else
        "Net Nakit Pozitif" if pd.notna(net_cash_to_cap) and net_cash_to_cap > 0 else
        "Net Borçlu" if pd.notna(net_cash_to_cap) else "Veri Yok"
    )

    ni_trend = trend_consistency(m["net"], quarters)



    # Veri kalitesi: 13 ana veri ailesinden kaç tanesi dolu?
    key_series = [A, CA, CL, L, E, NI_ttm, Rev_ttm, EBIT_ttm, OCF_ttm, FCF_ttm, Cash, price, shares]
    completeness = sum(pd.notna(x) for x in key_series) / len(key_series)
    shares_note = "Tahmini" if shares_est else "Kaynak"

    # Şirket bazında skorlar; peer percentile için sonraki aşamada tekrar normalize edilecek.
    # Growth + Economic Quality + gerçek risk motoru.
    rev_growth = div(Rev_ttm, Rev_prev)-1 if pd.notna(Rev_ttm) and pd.notna(Rev_prev) and Rev_prev>0 else np.nan
    ni_growth = div(NI_ttm, NI_prev)-1 if pd.notna(NI_ttm) and pd.notna(NI_prev) and NI_prev>0 else np.nan
    # v7.1: Aşırı nominal YoY (enflasyon/muhasebe artefaktı) growth skorunu şişirmesin.
    def _growth_cap(g, soft=GROWTH_YOY_SOFT_CAP, hard=GROWTH_YOY_HARD_CAP):
        # v12: soft %40 / hard %55 — enflasyonist nominal şişmeyi daha agresif bastır
        if pd.isna(g):
            return np.nan
        if g <= soft:
            return g
        return soft + (hard - soft) * (1.0 - math.exp(-(g - soft) / max(hard - soft, 1e-9)))
    rev_g_adj = _growth_cap(rev_growth)
    ni_g_adj = _growth_cap(ni_growth)
    growth_raw = safe_mean([
        score_target(rev_g_adj, -.05, .20),
        score_target(ni_g_adj, -.10, .25),
        score_target(roic, 0, .20),
    ])
    wacc=ke
    nopat=EBIT_ttm*.75 if pd.notna(EBIT_ttm) else np.nan
    economic_profit=nopat-invested_capital*wacc if pd.notna(nopat) and pd.notna(invested_capital) else np.nan
    roic_wacc_spread=roic-wacc if pd.notna(roic) else np.nan
    economic_score=safe_mean([
        score_target(roic_wacc_spread,-.05,.10),
        score_target(economic_profit/max(abs(invested_capital),1),-.05,.10) if pd.notna(economic_profit) else np.nan,
        score_target(roe_ke_spread, -0.05, 0.10) if pd.notna(roe_ke_spread) else np.nan,
        score_target(ri_on_book, -0.05, 0.10) if pd.notna(ri_on_book) else np.nan,
    ])
    debt_ebitda=div(net_debt,EBITDA_ttm) if pd.notna(EBITDA_ttm) else np.nan
    interest_proxy=div(EBIT_ttm,abs(net_debt)*wacc) if pd.notna(EBIT_ttm) and pd.notna(net_debt) and net_debt>0 else np.nan
    financial_safety=safe_mean([score_lower_better(debt_ebitda,0,4),score_target(cr,.8,2),score_target(altman,1.81,4) if sector_weight_bucket(sector) not in ('Banka','Sigorta','GYO') else np.nan,score_target(interest_proxy,1.5,6) if pd.notna(interest_proxy) else np.nan])
    earnings_safety=safe_mean([score_target(ni_trend,.4,.9),score_target(fcf_to_ni,.5,1.2),score_target(fcf_margin,0,.15)])
    valuation_safety=score_lower_better(abs(fair_margin),0,.8) if pd.notna(fair_margin) else np.nan
    risk_score=100*safe_mean([financial_safety,earnings_safety,valuation_safety])
    risk_penalty=float(np.clip((RISK_PENALTY_THRESHOLD-risk_score)*.35,0,RISK_PENALTY_MAX)) if pd.notna(risk_score) else 0

    # --- YENİ / GENİŞLETİLMİŞ METRİKLER (v5) ---
    net_debt_to_fcf = div(net_debt, FCF_ttm) if pd.notna(FCF_ttm) and FCF_ttm != 0 else np.nan
    _ni_q = [v for v in quarterize(m["net"], quarters) if pd.notna(v)]
    ni_median = float(np.median(_ni_q[:4])) if len(_ni_q) >= 4 else (float(np.median(_ni_q)) if _ni_q else np.nan)
    normalized_eps = div(ni_median * 4, shares) if pd.notna(ni_median) else np.nan

    _fcf_series = m.get("fcf_direct") or m.get("ocf")
    fcf_conv_trend = np.nan
    if _fcf_series is not None:
        fcf_q = quarterize(_fcf_series, quarters)
        ni_q = quarterize(m["net"], quarters)
        pairs = [(f, n) for f, n in zip(fcf_q, ni_q) if pd.notna(f) and pd.notna(n) and n != 0]
        if len(pairs) >= 4:
            recent = [f/n for f, n in pairs[:4]]
            older = [f/n for f, n in pairs[4:8]] if len(pairs) >= 6 else []
            r_mean = float(np.mean(recent))
            o_mean = float(np.mean(older)) if older else r_mean
            fcf_conv_trend = float(np.clip(0.5 + 0.25 * (r_mean - o_mean) + 0.25 * np.clip(r_mean, -1, 2), 0, 1))

    capex_to_dep = div(CAPEX_ttm, DEP_ttm) if pd.notna(CAPEX_ttm) and pd.notna(DEP_ttm) and DEP_ttm > 0 else np.nan
    st_debt_to_cash = div(SD, Cash) if pd.notna(SD) and pd.notna(Cash) and Cash > 0 else np.nan
    fx_pos = m.get("fx_position", [np.nan]*8)[c] if isinstance(m.get("fx_position"), list) else np.nan
    fx_to_equity = div(fx_pos, E) if pd.notna(fx_pos) and pd.notna(E) and E != 0 else np.nan

    # Accrual kalitesi: (NI - OCF) / Assets — yüksek accrual = düşük kalite
    accrual_ratio = div(NI_ttm - OCF_ttm, avg_assets) if pd.notna(NI_ttm) and pd.notna(OCF_ttm) and pd.notna(avg_assets) else np.nan
    accrual_score = score_lower_better(accrual_ratio, -0.05, 0.15) if pd.notna(accrual_ratio) else np.nan

    # Brüt marj istikrarı (8Ç): 1 - CV, yüksek = istikrarlı
    gm_q = [div(g, r) for g, r in zip(quarterize(m["gross"], quarters), quarterize(m["revenue"], quarters))
            if pd.notna(g) and pd.notna(r) and r != 0]
    if len(gm_q) >= 4:
        gm_mean = float(np.mean(gm_q)); gm_std = float(np.std(gm_q))
        gm_stability = float(np.clip(1.0 - (gm_std / abs(gm_mean) if abs(gm_mean) > 1e-9 else 1.0), 0, 1))
    else:
        gm_stability = np.nan

    # Gerçek faiz karşılama (EBIT / |finansman gideri|)
    int_exp_ttm = abs(ttm_from_cumulative(m["interest_exp"], quarters)) if "interest_exp" in m else np.nan
    interest_cover = div(EBIT_ttm, int_exp_ttm) if pd.notna(int_exp_ttm) and int_exp_ttm > 0 else (
        interest_proxy if pd.notna(interest_proxy) else np.nan
    )

    # Seyreltme proxy: ödenmiş sermaye artışı (yoy) — artış = ceza
    paid_now = m["paid_capital"][c]; paid_yoy = m["paid_capital"][yoy] if yoy < len(m["paid_capital"]) else np.nan
    dilution_growth = div(paid_now, paid_yoy) - 1 if pd.notna(paid_now) and pd.notna(paid_yoy) and paid_yoy > 0 else np.nan
    dilution_score = score_lower_better(dilution_growth, 0.0, 0.25) if pd.notna(dilution_growth) else np.nan

    # --- v6 metrikleri ---
    if pd.notna(CAPEX_ttm) and pd.notna(DEP_ttm):
        maint_capex = float(min(abs(CAPEX_ttm), max(abs(DEP_ttm), 0)))
    elif pd.notna(CAPEX_ttm):
        maint_capex = float(abs(CAPEX_ttm) * 0.7)
    elif pd.notna(DEP_ttm):
        maint_capex = float(abs(DEP_ttm))
    else:
        maint_capex = np.nan
    owner_earnings = (OCF_ttm - maint_capex) if pd.notna(OCF_ttm) and pd.notna(maint_capex) else (FCF_ttm if pd.notna(FCF_ttm) else np.nan)
    oe_yield = div(owner_earnings, cap)
    ev_sales = div(ev, Rev_ttm)
    price_sales = div(cap, Rev_ttm)

    sloan = sloan_ratio(NI_ttm, OCF_ttm, avg_assets)
    sloan_score = score_lower_better(abs(sloan), 0.0, 0.20) if pd.notna(sloan) else np.nan
    beneish_m = beneish_m_proxy(m, quarters, c, yoy, Rev_ttm, NI_ttm, OCF_ttm, A, CA, CL, DEP_ttm)
    beneish_score = score_lower_better(beneish_m, -3.0, -1.0) if pd.notna(beneish_m) else np.nan

    # ROIC çeyrek proxy trendi: EBIT*(1-t) / invested capital
    roic_series = []
    for i in range(8):
        ebit_i = m["ebit"][i]
        inv_i = (m["equity"][i] if pd.notna(m["equity"][i]) else 0) + (m["sd"][i] if pd.notna(m["sd"][i]) else 0) + (m["ld"][i] if pd.notna(m["ld"][i]) else 0) - (m["cash"][i] if pd.notna(m["cash"][i]) else 0)
        if pd.notna(ebit_i) and inv_i and inv_i != 0:
            roic_series.append(ebit_i * 0.75 / inv_i)
        else:
            roic_series.append(np.nan)
    roic_trend = trend_slope_score(roic_series, quarters, True, already_quarterly=True)

    gm_series = []
    for i in range(8):
        gm_series.append(div(m["gross"][i], m["revenue"][i]))
    gm_trend = trend_slope_score(gm_series, quarters, True, already_quarterly=True)

    # v192.10: FAVÖK marj trendi (gm_trend'in yanına, aynı yöntemle) + okunabilir "Marj Yönü" etiketi
    ebitda_m_series = []
    for i in range(8):
        _e, _dp, _rv = m["ebit"][i], m["dep"][i], m["revenue"][i]
        ebitda_m_series.append(div((_e + _dp) if pd.notna(_e) and pd.notna(_dp) else _e, _rv))
    ebitda_trend = trend_slope_score(ebitda_m_series, quarters, True, already_quarterly=True)
    _mt = [x for x in (gm_trend, ebitda_trend) if pd.notna(x)]
    margin_yon = np.nan
    if _mt:
        _mtm = float(np.mean(_mt))
        margin_yon = "İyileşiyor" if _mtm >= 0.60 else "Kötüleşiyor" if _mtm <= 0.40 else "Yatay"

    # v192.10: Nakit dönüşüm döngüsü (CCC) — alacak + stok tahsil/devir süresi eksi tedarikçi ödeme süresi.
    # v192.11 DÜZELTME 1: canlı veride GYO/holding gibi sektörlerde (stoklar = geliştirme aşamasındaki gayrimenkul,
    # SMM ise ilgisiz/çok küçük) COGS'a bölme milyonlarca/on binlerce gün gibi anlamsız değerler üretiyordu
    # (ör. KERVN −4.000.000 gün, birkaç GYO +30.000 gün). Standart ticari alacak/stok devri mantığı bu iş
    # modellerine uymuyor: (1) GYO/Holding/finans hariç tutulur, (2) SMM, hasılatın en az %5'i değilse (COGS'a
    # bölme neredeyse sıfıra bölme riski) hesaplanmaz, (3) her bileşen 400 günle sınırlanır — üstü NaN sayılır.
    # v192.11 DÜZELTME 2 (ENFLASYON): v192.10 bu dönem + 1 yıl önceki bakiyenin ORTALAMASINI kullanıyordu.
    # ~%30 enflasyonda bu YANLIŞ: alacak/borç PARASAL kalemlerdir (TMS 29/IAS 29'a göre enflasyona karşı
    # yeniden değerlenmezler — "zaten o günün ölçü birimiyle ifade edilirler"), bu yüzden eski bakiyeyi bugünkü
    # satın alma gücüne "taşımak" kavramsal olarak hatalı olur. Asıl sorun farklı ekonomik dönemlere ait iki
    # nominal rakamı doğrudan ortalamaktır — bu, günleri sistematik olarak OLDUĞUNDAN AZ gösterir (nominal
    # hasılat enflasyonla büyürken eski dönem bakiyesi göreli küçük kalır). Çözüm: ortalama almayı bırakıp
    # yalnızca EN SON dönem sonu bakiyesini kullanmak — yüksek enflasyon ortamlarında tercih edilen ve
    # muhasebe teorisiyle çelişmeyen basit/sağlam yöntem. Stok (parasal olmayan kalem) için de tutarlılık
    # adına aynı yöntem uygulanır.
    ccc = dso = dio = dpo = np.nan
    _ccc_bucket = sector_weight_bucket(sector)
    if _ccc_bucket not in ("GYO", "Holding/Yatırım", "Banka", "Sigorta") and "holding" not in norm(sector):
        try:
            ar_series, inv_series, ap_series, cogs_series = m.get("receivables"), m.get("inventory"), m.get("payables"), m.get("cogs")
            cogs_ttm = abs(ttm_from_cumulative(cogs_series, quarters)) if cogs_series is not None else np.nan
            cogs_ok = pd.notna(cogs_ttm) and pd.notna(Rev_ttm) and Rev_ttm > 0 and cogs_ttm >= 0.05 * Rev_ttm
            _cap_days = lambda x: x if pd.notna(x) and 0 <= x <= 400 else np.nan
            if ar_series is not None and pd.notna(ar_series[0]) and pd.notna(Rev_ttm) and Rev_ttm > 0:
                dso = _cap_days(ar_series[0] / Rev_ttm * 365.0)
            if inv_series is not None and pd.notna(inv_series[0]) and cogs_ok:
                dio = _cap_days(inv_series[0] / cogs_ttm * 365.0)
            if ap_series is not None and pd.notna(ap_series[0]) and cogs_ok:
                dpo = _cap_days(ap_series[0] / cogs_ttm * 365.0)
            if pd.notna(dso) or pd.notna(dio) or pd.notna(dpo):
                ccc = np.nansum([dso, dio]) - (dpo if pd.notna(dpo) else 0.0)
        except Exception:
            pass

    # v192.10: GYO NAV modeli (DENEYSEL) — yalnızca GYO/gayrimenkul yatırım ortaklığı sektöründe hesaplanır.
    # Diğer sektörlerde NaN kalır. NAV = Yatırım Amaçlı Gayrimenkuller (gerçeğe uygun değer) + Nakit − Toplam
    # Yükümlülükler; hisseye bölünür. IFRS'te yatırım amaçlı gayrimenkuller zaten gerçeğe uygun değerle
    # taşındığından bu, Defter Değeri/Hisse'den (BVPS) farklı — diğer varlık/yükümlülük kalemlerini izole eder.
    gyo_nav_ps = gyo_nav_iskonto = np.nan
    if "gyo" in norm(sector) or "gayrimenkul yat" in norm(sector):
        try:
            inv_prop = m.get("investment_property")
            ip0 = inv_prop[0] if inv_prop is not None else np.nan
            A0, E0 = m["assets"][0], m["equity"][0]
            total_liab = (m["liab"][0] if pd.notna(m["liab"][0]) else
                          (A0 - E0) if pd.notna(A0) and pd.notna(E0) else
                          ((m["sd"][0] if pd.notna(m["sd"][0]) else 0) + (m["ld"][0] if pd.notna(m["ld"][0]) else 0)
                           + (m.get("payables", [np.nan]*8)[0] if pd.notna(m.get("payables", [np.nan]*8)[0]) else 0)))
            if pd.notna(ip0) and ip0 > 0 and pd.notna(shares) and shares > 0:
                nav_total = ip0 + (Cash if pd.notna(Cash) else 0) - (total_liab if pd.notna(total_liab) else 0)
                gyo_nav_ps = nav_total / shares
                if pd.notna(gyo_nav_ps) and gyo_nav_ps > 0 and pd.notna(price):
                    gyo_nav_iskonto = float(np.clip(price / gyo_nav_ps - 1.0, -0.95, 3.0))
        except Exception:
            pass

    # v192.10: Döviz riski bayrağı (mevcut fx_to_equity'den; sert eleme değil, bilgi/uyarı)
    doviz_riski = np.nan
    if pd.notna(fx_to_equity):
        doviz_riski = ("Yüksek Açık Riski" if fx_to_equity <= -0.30 else
                       "Orta Açık" if fx_to_equity <= -0.10 else
                       "Net Fazla (Doğal Hedge)" if fx_to_equity >= 0.10 else "Sınırlı")

    fx_to_ebitda = div(fx_pos, EBITDA_ttm) if pd.notna(fx_pos) and pd.notna(EBITDA_ttm) and EBITDA_ttm != 0 else np.nan
    # Aşırı negatif YP/EBITDA risk
    fx_ebitda_score = score_target(fx_to_ebitda, -2.0, 0.5) if pd.notna(fx_to_ebitda) else np.nan

    implied_g = reverse_dcf_growth(price, FCF_ttm if pd.notna(FCF_ttm) and FCF_ttm > 0 else owner_earnings, shares, ke)
    # Piyasanın fiyatladığı büyüme düşükse ucuz (value); çok yüksekse pahalı / iyimser
    # v192: nominal implied g reel büyümeye çevrilerek skorlanır (0 reel = iyi, %20 reel = kötü)
    _implied_real = ((1.0 + implied_g) / (1.0 + INFLATION_NOW) - 1.0) if pd.notna(implied_g) else np.nan
    reverse_dcf_score = score_lower_better(_implied_real, 0.0, 0.20) if pd.notna(implied_g) else np.nan


    # --- v12 Forward F/K & PD/DD (değer yatırımı tamamlayıcı) ---
    # Kaynak önceliği: (1) r içindeki forwardPE/forwardPB varsa (2) model-implied.
    # Implied F/K: Fiyat / (EPS_TTM * (1+g_fwd)); g_fwd = min(rev_growth, ni_growth, 0.25) median-ish.
    # Implied PD/DD: Fiyat / (BVPS * (1 + ROE*(1-payout_proxy))); payout_proxy kabaca 0.3.
    pe_fwd = r.get("forward_pe", np.nan)
    pb_fwd = r.get("forward_pb", np.nan)
    pe_fwd_src = "Konsensüs" if pd.notna(pe_fwd) and pe_fwd > 0 else ""
    pb_fwd_src = "Konsensüs" if pd.notna(pb_fwd) and pb_fwd > 0 else ""
    g_candidates = []
    if pd.notna(rev_growth):
        g_candidates.append(float(np.clip(rev_growth, -0.20, GROWTH_CLIP_HI)))
    if pd.notna(ni_growth):
        g_candidates.append(float(np.clip(ni_growth, -0.20, GROWTH_CLIP_HI)))
    if pd.notna(implied_g):
        g_candidates.append(float(np.clip(implied_g, -0.10, GROWTH_CLIP_HI)))
    g_fwd = float(np.median(g_candidates)) if g_candidates else 0.08
    g_fwd = float(np.clip(g_fwd, -0.15, GROWTH_CLIP_HI))
    if (pd.isna(pe_fwd) or pe_fwd <= 0) and pd.notna(eps) and eps > 0 and pd.notna(price) and price > 0:
        eps_fwd = eps * (1.0 + g_fwd)
        if eps_fwd > 0:
            pe_fwd = price / eps_fwd
            pe_fwd_src = f"Implied(g={g_fwd:.1%})"
    if (pd.isna(pb_fwd) or pb_fwd <= 0) and pd.notna(bvps) and bvps > 0 and pd.notna(price) and price > 0:
        payout_proxy = 0.30
        roe_use = float(roe) if pd.notna(roe) else 0.10
        bv_growth = float(np.clip(roe_use * (1.0 - payout_proxy), -0.10, GROWTH_CLIP_HI))
        bvps_fwd = bvps * (1.0 + bv_growth)
        if bvps_fwd > 0:
            pb_fwd = price / bvps_fwd
            pb_fwd_src = f"Implied(BV g={bv_growth:.1%})"
    delta_pe = (pe_fwd - pe) if pd.notna(pe_fwd) and pd.notna(pe) and pe > 0 and pe_fwd > 0 else np.nan
    delta_pb = (pb_fwd - pb) if pd.notna(pb_fwd) and pd.notna(pb) and pb > 0 and pb_fwd > 0 else np.nan
    # Δ F/K < 0 → kâr artışı beklentisi (forward daha ucuz) → hafif artı
    _pe_fwd_s = score_lower_better(pe_fwd, 5, 22) if pd.notna(pe_fwd) and pe_fwd > 0 else np.nan
    _pb_fwd_s = score_lower_better(pb_fwd, 0.5, 3.0) if pd.notna(pb_fwd) and pb_fwd > 0 else np.nan
    _delta_pe_s = score_lower_better(delta_pe, -8, 5) if pd.notna(delta_pe) else np.nan  # neg delta iyi
    _delta_pb_s = score_lower_better(delta_pb, -0.4, 0.3) if pd.notna(delta_pb) else np.nan

    # Bileşen skorları (mutlak, sonra enrich'te sektör-göreli ince ayar)
    # v7.1: Saf çarpan (F/K, PD/DD, EV/Sales) ağırlığı düşürüldü — backtest'te negatif korele.
    # Nakit getirisi (FCF/OE), RevDCF ve net nakit güçlendirildi.
    # v12: Forward çarpan + Δ düşük ağırlıkla multiples'a eklenir (tek slot içinde).
    _pe_s = score_lower_better(pe, 6, 25) if pd.notna(pe) and pe > 0 else np.nan
    _pb_s = score_lower_better(pb, 0.6, 3.5) if pd.notna(pb) and pb > 0 else np.nan
    _evs_s = score_lower_better(ev_sales, 0.5, 4.0) if pd.notna(ev_sales) and ev_sales > 0 else np.nan
    _ps_s = score_lower_better(price_sales, 0.3, 3.0) if pd.notna(price_sales) and price_sales > 0 else np.nan
    # Çarpan grubu: trailing ağır, forward tamamlayıcı (~%25)
    multiples = safe_mean([
        _pe_s, _pb_s, _evs_s, _ps_s,
        _pe_fwd_s, _pb_fwd_s, _delta_pe_s, _delta_pb_s,
    ])
    value_raw = safe_mean([
        multiples,  # tek slot — önceki 4 ayrı slot yerine (+ forward)
        score_target(fcf_yield, 0, 0.12),
        score_target(oe_yield, 0, 0.12) if pd.notna(oe_yield) else np.nan,
        score_target(earnings_yield, 0.03, 0.15),
        score_target(fair_margin, -0.15, 0.40),
        score_target(ri_margin, -0.15, 0.40) if pd.notna(ri_margin) else np.nan,
        reverse_dcf_score,
        1.0 if is_net_net else (0.5 if pd.notna(ncav) and ncav > 0 else np.nan),
        score_target(net_cash_to_cap, -0.3, 0.25) if pd.notna(net_cash_to_cap) else np.nan,
        score_target(epv_margin, -0.15, 0.40) if pd.notna(epv_margin) else np.nan,
        ev_ebit_s,
    ])
    quality_raw = safe_mean([
        score_target(roe, 0.08, 0.30),
        score_target(roic, 0.05, 0.25),
        score_target(ebitda_margin, 0.05, 0.28),
        score_target(fcf_to_ni, 0.4, 1.3),
        economic_score,
        fcf_conv_trend if pd.notna(fcf_conv_trend) else np.nan,
        accrual_score,
        sloan_score,
        beneish_score,
        gm_stability,
        roic_trend,
        gm_trend,
    ])
    balance_raw = safe_mean([
        financial_safety,
        score_lower_better(net_debt_to_fcf, 0, 6) if pd.notna(net_debt_to_fcf) else np.nan,
        score_lower_better(st_debt_to_cash, 0.3, 3) if pd.notna(st_debt_to_cash) else np.nan,
        score_target(fx_to_equity, -0.5, 0.2) if pd.notna(fx_to_equity) else np.nan,
        fx_ebitda_score,
        score_target(interest_cover, 1.5, 8) if pd.notna(interest_cover) else np.nan,
        dilution_score,
    ])
    cash_raw = safe_mean([
        score_target(fcf_margin, 0, 0.15),
        score_target(fcf_to_ni, 0.4, 1.3),
        score_target(oe_yield, 0, 0.12) if pd.notna(oe_yield) else np.nan,
        score_target(net_cash_to_cap, -0.4, 0.3),
        1.0 if pd.notna(OCF_ttm) and OCF_ttm > 0 else (0.0 if pd.notna(OCF_ttm) else np.nan),
        score_lower_better(capex_to_dep, 0.5, 2.5) if pd.notna(capex_to_dep) else np.nan,
        score_target(div(owner_earnings, NI_ttm), 0.5, 1.3) if pd.notna(owner_earnings) and pd.notna(NI_ttm) and NI_ttm != 0 else np.nan,
    ])
    completeness = sum(pd.notna(x) for x in [A, CA, CL, L, E, NI_ttm, Rev_ttm, EBIT_ttm, OCF_ttm, FCF_ttm, Cash, price, shares]) / 13
    fin_limited = sector_weight_bucket(sector) in ('Banka', 'Sigorta') or any(x in norm(sector) for x in ('banka', 'sigorta', 'faktoring', 'finansal kiralama', 'varlik yonetim', 'araci kurum'))
    confidence = 100 * (
        .45 * completeness
        + .20 * (f_valid / 9 if f_valid else 0)
        + .20 * (valuation_agreement if pd.notna(valuation_agreement) else 0)
        + .15 * (1 if sector_weight_bucket(sector) != 'Genel/Sanayi' else .5)
    )
    if fin_limited:
        confidence = min(confidence, 35.0)
    if shares_est:
        confidence = min(confidence, confidence * 0.9)

    wv, wq, wg, wb, wc, wp = sector_weights(sector)
    # Peer burada yok; enrich'te tavanlı eklenir. İç skor ayakta kalsın diye peer payı 0.
    denom = wv + wq + wg + wb + wc + 0.15
    internal = 100 * safe_mean([
        value_raw * wv, quality_raw * wq, growth_raw * wg,
        balance_raw * wb, cash_raw * wc,
        economic_score * 0.05, f_rate * 0.05, g_rate * 0.05,
    ]) / denom if denom else np.nan
    internal = float(np.clip(internal - risk_penalty, 0, 100)) if pd.notna(internal) else np.nan

    # v7.1 Değer tuzağı cezası: ucuz görünen ama kalite/nakit zayıf → skor bastır
    # (backtest'te saf ucuzluk negatif korele)
    value_trap_pen = 0.0
    if pd.notna(value_raw) and pd.notna(quality_raw):
        cheap = value_raw >= 0.65
        weak_q = quality_raw < 0.40
        weak_cash = pd.notna(cash_raw) and cash_raw < 0.35
        weak_fcf = pd.notna(fcf_yield) and fcf_yield < 0.02 and pd.notna(oe_yield) and (pd.isna(oe_yield) or oe_yield < 0.03)
        if cheap and (weak_q or weak_cash or weak_fcf):
            value_trap_pen = 6.0 if (weak_q and weak_cash) else 3.5
            internal = float(np.clip(internal - value_trap_pen, 0, 100)) if pd.notna(internal) else np.nan

    # Finansal kurum: iç skor bastır (ana sıralamada ayrı işaret)
    if fin_limited:
        internal = float(np.clip(internal * 0.5, 0, 50)) if pd.notna(internal) else np.nan

    val2 = valuation_class2(fair_margin)

    # Skor sürücüleri (en güçlü 3 + / 3 -)
    driver_map = {
        'Değer': value_raw, 'Kalite': quality_raw, 'Büyüme': growth_raw,
        'Bilanço': balance_raw, 'Nakit': cash_raw, 'Ekonomik': economic_score,
        'Piotroski': f_rate, 'Graham': g_rate, 'Accrual': accrual_score,
        'Sloan': sloan_score, 'Beneish': beneish_score,
        'Marjİstikrar': gm_stability, 'ROICTrend': roic_trend,
        'OEGetiri': score_target(oe_yield, 0, 0.12) if pd.notna(oe_yield) else np.nan,
        'RevDCF': reverse_dcf_score,
        'RI': score_target(ri_margin, -0.15, 0.40) if pd.notna(ri_margin) else np.nan,
        'ROE-ke': score_target(roe_ke_spread, -0.05, 0.10) if pd.notna(roe_ke_spread) else np.nan,
    }
    items = [(k, float(v)) for k, v in driver_map.items() if pd.notna(v)]
    items_sorted = sorted(items, key=lambda x: x[1], reverse=True)
    pos_drv = ', '.join(f'{k}:{v:.2f}' for k, v in items_sorted[:3]) if items_sorted else ''
    neg_drv = ', '.join(f'{k}:{v:.2f}' for k, v in items_sorted[-3:][::-1]) if len(items_sorted) >= 3 else ''

    regime_note = 'Standart'
    if pd.notna(fcf_yield) and fcf_yield > 0.12 and pd.notna(roe) and roe < 0.05:
        regime_note = 'Ucuz ama düşük kalite (değer tuzağı riski)'
    elif pd.notna(roe) and roe > 0.20 and pd.notna(fair_margin) and fair_margin < -0.20:
        regime_note = 'Kaliteli ama pahalı'
    elif pd.notna(ni_growth) and ni_growth > 1.0:
        regime_note = 'Yüksek büyüme (muhasebe/enflasyon artefaktı kontrol et)'

    # v192.9: Temettü verimi — nakit akış tablosundaki "Ödenen Temettüler" kalemi (genelde negatif/çıkış)
    div_ttm = np.nan
    try:
        div_series = m.get("dividends_paid")
        if div_series is not None:
            div_ttm = abs(ttm_from_cumulative(div_series, quarters))
    except Exception:
        div_ttm = np.nan
    div_yield = div(div_ttm, cap) if pd.notna(div_ttm) and pd.notna(cap) and cap > 0 else np.nan
    # Kabaca süreklilik: son 8 çeyreğin kaçında temettü ödemesi (kümülatif farkı) sıfırdan büyük?
    div_years_paid = np.nan
    try:
        dq = quarterize(m.get("dividends_paid"), quarters) if m.get("dividends_paid") is not None else []
        div_years_paid = int(sum(1 for x in dq if pd.notna(x) and abs(x) > 0))
    except Exception:
        pass

    loss_kind = ""
    if pd.notna(NI_ttm) and NI_ttm < 0:
        # Net zarar operasyondan mı (EBIT/OCF ≤ 0) yoksa FAVÖK altı kalemlerden mi (finansman gideri, TMS 29 parasal kayıp, kur)?
        loss_kind = ("Finansal/parasal kalem kaynaklı (EBIT>0, OCF>0)"
                     if (pd.notna(EBIT_ttm) and EBIT_ttm > 0 and pd.notna(OCF_ttm) and OCF_ttm > 0) else "Operasyonel")
    model_warn = ''
    if pd.notna(Rev_ttm) and Rev_ttm <= 0:
        model_warn = 'Hasılat ≤ 0 (atıl/yatırım şirketi; DCF uygulanamaz) | '
    if _sanity_notes:
        model_warn += 'Uç değer dışlandı: ' + ','.join(sorted(set(_sanity_notes))) + ' | '
    if fin_limited:
        model_warn += 'Finansal kurum — XI_29 kapsam dışı; skor bastırıldı'
    elif completeness < 0.6:
        model_warn += 'Eksik veri — düşük güven'
    elif pd.isna(FCF_ttm) and pd.isna(dcf_base):
        model_warn += 'DCF/FCF yok — çarpan ağırlıklı değerleme'

    return {
        "Hisse":code,"Sektör":sector,"Final Score (Ön)":internal,"Araştırma Skoru":internal,"Not":investment_class(internal),"Güven Skoru":confidence,
        "Risk Skoru":risk_score,"Risk Sınıfı":risk_class(risk_score),"Yatırım Kararı":final_verdict(internal,val2,risk_class(risk_score),confidence),
        "Değerleme Sınıfı":val2,"Değerleme Sınıfı (Eski)":fair_class,"Adil Değer (Medyan)":fair_value,"Adil Değer Marjı":fair_margin,"Değerleme Uzlaşma Skoru":valuation_agreement,
        "Değer Yatırımcılığı Sonucu":value_investing_verdict,"Graham Kriter Geçiş Oranı":g_rate,"Graham Güvenlik Marjı":graham_margin,"Net-Net (NCAV) Durumu":net_net_note,"Graham Kriter Detay":graham_kriter_detay,
        "Net Nakit (Nakit - Toplam Finansal Borç)":net_cash,"Net Nakit/Piyasa Değeri":net_cash_to_cap,"Nakit Bazlı Ucuzluk Notu":cash_cheapness_note,
        "DCF Bear":dcf_bear,"DCF Base":dcf_base,"DCF Bull":dcf_bull,"Adil Değer (Graham)":gn,"Adil Değer (NCAV)":ncav,"Adil Değer (Kapitalize Kazanç)":cap_earnings,"Adil Değer (EPV)":epv_ps,"EPV Marjı":epv_margin,"EV/EBIT Band Skoru":ev_ebit_s,
        "Adil Değer (RI)":fair_ri,"RI Marjı":ri_margin,"RI (Yıl1)":ri1,"ROE-ke Spread":roe_ke_spread,"RI/Defter":ri_on_book,
        "Fiyat":price,"Piyasa Değeri":cap,"Hisse Sayısı":shares,"Hisse Sayısı Kaynağı":shares_note,"F/K":pe,"PD/DD":pb,"F/K (Forward)":pe_fwd,"PD/DD (Forward)":pb_fwd,"Δ F/K (Fwd−TTM)":delta_pe,"Δ PD/DD (Fwd−TTM)":delta_pb,"Forward F/K Kaynak":pe_fwd_src,"Forward PD/DD Kaynak":pb_fwd_src,"Forward g varsayımı":g_fwd,"EPS (TTM)":eps,"Defter Değeri/Hisse":bvps,"Kazanç Getirisi":earnings_yield,"FCF Getirisi":fcf_yield,"EV":ev,"Net Borç":net_debt,"EV/EBIT":ev_ebit,"EV/FAVÖK":ev_ebitda,
        "ROE":roe,"ROA":roa,"ROIC":roic,"ROIC-WACC Spread":roic_wacc_spread,"Economic Profit":economic_profit,"Net Kar Marjı":net_margin,"Faaliyet Kar Marjı":ebit_margin,"FAVÖK Marjı":ebitda_margin,"FCF Marjı":fcf_margin,"FCF/Net Kar":fcf_to_ni,"Cari Oran":cr,"Borç/Özkaynak":div(L,E),"Net Borç/Özkaynak":div(net_debt,E),"Net Borç/FAVÖK":debt_ebitda,"Net Borç/FCF":net_debt_to_fcf,"Faiz Karşılama Proxy":interest_proxy,
        "Piotroski F":f_score,"Piotroski Geçerli Kriter":f_valid,"Graham Puan":g_score,"Graham Geçerli Kriter":g_valid,"Altman Z":altman,"Altman Bölge":("Güvenli" if pd.notna(altman) and altman>2.99 else "Gri" if pd.notna(altman) and altman>=1.81 else "Riskli" if pd.notna(altman) else "Uygulanamaz/Veri Yok"),"Altman Veri Notu":altman_note,
        "Net Kâr Trend Tutarlılığı (8Ç)":ni_trend,"Kârlılık Skoru":quality_raw,"Değer Skoru":value_raw,"Büyüme Skoru":growth_raw,"Bilanço Skoru":balance_raw,"Nakit Akışı Skoru":cash_raw,"Economic Quality Skoru":economic_score,"Risk Cezası":risk_penalty,"Veri Tamlık":completeness,"Revenue YoY":rev_growth,"Net Kar YoY":ni_growth,
        "Brüt Kar Marjı":gm,"FAVÖK (TTM)":EBITDA_ttm,"Net Kar (TTM)":NI_ttm,"Hasılat (TTM)":Rev_ttm,"OCF (TTM)":OCF_ttm,"CapEx (TTM)":CAPEX_ttm,"FCF (TTM)":FCF_ttm,"Nakit ve Nakit Benzerleri (Bilanço)":Cash,"Dönem Sonu Nakit (Nakit Akış Tablosu)":PeriodEndCash,"Son 8 Çeyrek Net Kâr (Güncelden Eskiye)":" | ".join("-" if pd.isna(v) else f"{v:,.0f}" for v in m["net"]),
        "Net Borç/FCF":net_debt_to_fcf,"Normalized EPS":normalized_eps,"FCF Conversion Trend":fcf_conv_trend,
        "CapEx/Amortisman":capex_to_dep,"Kısa Vadeli Borç/Nakit":st_debt_to_cash,"Net YP Pozisyon/Özkaynak":fx_to_equity,
        "Accrual Kalitesi":accrual_score,"Brüt Marj İstikrarı":gm_stability,"Faiz Karşılama":interest_cover,
        "Seyreltme Skoru":dilution_score,
        "Owner Earnings (TTM)":owner_earnings,"Owner Earnings Getirisi":oe_yield,
        "EV/Sales":ev_sales,"Price/Sales":price_sales,
        "Sloan Ratio":sloan,"Sloan Skoru":sloan_score,
        "Beneish M (Proxy)":beneish_m,"Beneish Skoru":beneish_score,
        "ROIC Trend Skoru":roic_trend,"Brüt Marj Trend Skoru":gm_trend,
        "Net YP/EBITDA":fx_to_ebitda,
        "Reverse DCF Implied g":implied_g,"Reverse DCF Skoru":reverse_dcf_score,
        "WACC (ke)":ke,"Rejim Notu":regime_note,"Model Uyarısı":model_warn,"Zarar Niteliği":loss_kind,
        "Temettü Ödemesi (TTM)":div_ttm,"Temettü Verimi":div_yield,"Temettü Ödeyen Çeyrek (8Ç)":div_years_paid,
        "FAVÖK Marj Trend Skoru":ebitda_trend,"Marj Yönü":margin_yon,
        "Nakit Dönüşüm Döngüsü (Gün)":ccc,"Alacak Tahsil Süresi (Gün)":dso,"Stok Devir Süresi (Gün)":dio,"Borç Ödeme Süresi (Gün)":dpo,
        "GYO NAV/Hisse (Deneysel)":gyo_nav_ps,"GYO NAV İskonto/Primi":gyo_nav_iskonto,
        "Döviz Riski":doviz_riski,
        "Skor Sürücüleri (+)":pos_drv,"Skor Sürücüleri (-)":neg_drv,
        "Kapsam Notu":("Finansal (Sınırlı Kapsam — XI_29)" if fin_limited else "Standart"),
        "Ana Sıralamada":("Hayır — Finansal" if fin_limited else "Evet"),
    }



def enrich_summary(df):
    df = df.copy()
    if 'Ana Sıralamada' not in df.columns:
        kn = df['Kapsam Notu'] if 'Kapsam Notu' in df.columns else pd.Series(['Standart'] * len(df))
        df['Ana Sıralamada'] = kn.map(lambda x: 'Hayır — Finansal' if isinstance(x, str) and 'Sınırlı' in x else 'Evet')

    specs = [
        ("ROIC", True), ("ROE", True), ("FCF Getirisi", True),
        ("F/K", False), ("PD/DD", False), ("F/K (Forward)", False), ("PD/DD (Forward)", False),
        ("EV/FAVÖK", False),
        ("Net Borç/Özkaynak", False), ("Net Borç/FCF", False),
        ("Adil Değer Marjı", True), ("Piotroski F", True), ("Altman Z", True),
        ("FCF Conversion Trend", True), ("Net Nakit/Piyasa Değeri", True),
        ("Accrual Kalitesi", True), ("Brüt Marj İstikrarı", True),
        ("Owner Earnings Getirisi", True), ("Sloan Skoru", True),
        ("Beneish Skoru", True), ("ROIC Trend Skoru", True),
        ("Reverse DCF Skoru", True),
        ("RI Marjı", True), ("ROE-ke Spread", True),
    ]
    for col, high in specs:
        if col not in df.columns:
            continue
        vals = []
        for _, row in df.iterrows():
            pool = df.loc[df.Sektör == row.Sektör, col] if 'Sektör' in df.columns else df[col]
            if pool.notna().sum() < 5:
                pool = df[col]
            if col in {'F/K', 'PD/DD', 'F/K (Forward)', 'PD/DD (Forward)', 'EV/FAVÖK'}:
                pool = pool.where(pool > 0)
            if pool.notna().sum() >= 8:
                lo, hi = pool.quantile(0.02), pool.quantile(0.98)
                pool = pool.clip(lo, hi)
            vals.append(pct_rank(pool, row[col], high))
        df[f'Peer % {col}'] = vals
    pc = [c for c in df.columns if c.startswith('Peer % ')]
    df['Peer Skor'] = df[pc].mean(axis=1, skipna=True) * 100

    for raw_col, out_col in [
        ('Değer Skoru', '_rel_val'), ('Kârlılık Skoru', '_rel_qual'),
        ('Büyüme Skoru', '_rel_gr'), ('Bilanço Skoru', '_rel_bal'),
        ('Nakit Akışı Skoru', '_rel_cash'),
    ]:
        if raw_col not in df.columns:
            df[out_col] = np.nan
            continue
        rel = []
        for _, row in df.iterrows():
            pool = df.loc[df.Sektör == row.Sektör, raw_col] if 'Sektör' in df.columns else df[raw_col]
            if pool.notna().sum() < 5:
                pool = df[raw_col]
            p = pct_rank(pool, row[raw_col], True)
            base = row[raw_col]
            if pd.notna(base) and pd.notna(p):
                rel.append(0.60 * float(base) + 0.40 * float(p))
            else:
                rel.append(base if pd.notna(base) else p)
        df[out_col] = rel

    def final(row):
        w = sector_weights(row.get('Sektör'))
        peer = row['Peer Skor'] / 100 if pd.notna(row.get('Peer Skor')) else np.nan
        vals = [
            row.get('_rel_val', row.get('Değer Skoru')),
            row.get('_rel_qual', row.get('Kârlılık Skoru')),
            row.get('_rel_gr', row.get('Büyüme Skoru')),
            row.get('_rel_bal', row.get('Bilanço Skoru')),
            row.get('_rel_cash', row.get('Nakit Akışı Skoru')),
            peer,
            row.get('Economic Quality Skoru'),
        ]
        # v7.1: Peer tavanı biraz düşürüldü; quality/balance zaten sector_weights'te yüksek
        ws = [w[0], w[1], w[2], w[3], w[4], min(w[5], 0.10), 0.06]
        pairs = [(v, ww) for v, ww in zip(vals, ws) if pd.notna(v)]
        if not pairs:
            return np.nan
        x = sum(v * ww for v, ww in pairs) / sum(ww for _, ww in pairs) * 100
        # Risk çarpanı sıkılaştırıldı (düşük risk skoru daha fazla bastırır)
        if pd.notna(row.get('Risk Skoru')):
            x *= 0.75 + 0.25 * row['Risk Skoru'] / 100
        if pd.notna(row.get('Güven Skoru')) and row['Güven Skoru'] < 55:
            x *= 0.85 + 0.15 * row['Güven Skoru'] / 55
        if row.get('Kapsam Notu') and 'Sınırlı' in str(row.get('Kapsam Notu')):
            x *= 0.70
        # Ek: çok yüksek değer skoru + düşük kalite → final'de de bastır
        v_raw = row.get('Değer Skoru')
        q_raw = row.get('Kârlılık Skoru')
        if pd.notna(v_raw) and pd.notna(q_raw) and v_raw >= 0.70 and q_raw < 0.35:
            x *= 0.92
        return float(np.clip(x, 0, 100))

    df['Final Score'] = df.apply(final, axis=1).round(2)
    fs = pd.to_numeric(df['Final Score'], errors='coerce')
    df['Score Percentile'] = fs.rank(pct=True, method='average')
    df['Not'] = df['Score Percentile'].map(investment_class_from_percentile)
    df['Risk Sınıfı'] = df['Risk Skoru'].map(risk_class)
    df['Değerleme Sınıfı'] = df['Adil Değer Marjı'].map(valuation_class2)
    df['Yatırım Kararı'] = [
        final_verdict(a, b, c, d, g)
        for a, b, c, d, g in zip(
            df['Final Score'], df['Değerleme Sınıfı'], df['Risk Sınıfı'],
            df['Güven Skoru'], df['Not']
        )
    ]
    df['Birleşik Skor'] = df['Final Score']
    df['Risk Ayarlı Skor'] = df['Final Score']
    df['Sınıf'] = df['Not']
    df.drop(columns=[c for c in df.columns if c.startswith('_rel_')], inplace=True, errors='ignore')
    return df.sort_values(['Final Score', 'Güven Skoru', 'Hisse'], ascending=[False, False, True])



def walk_forward_disclaimer():
    """True point-in-time walk-forward için gerekenler (henüz tam uygulanmadı).

    Mevcut backtest: bugünkü skor ↔ [bugün-N, bugün] getiri.
    Bu, skorun N ay önce de aynı olacağını varsayar → look-ahead'e yakın.

    Gerçek walk-forward:
      1) Her anchor tarihte (örn. her çeyrek sonu) o tarihte bilinen mali tablolarla skoru yeniden hesapla
      2) Anchor'dan sonraki 6A/12A/24A getiriyi ölç
      3) Skor bandı × getiri panelini topla

    Engeller (mevcut veri kaynağı):
      - İş Yatırım MaliTablo API'si yalnızca son 8 çeyreği veriyor; 2020-2023 restatement yok
      - Geçmiş piyasa çarpanları / hisse sayısı arşivi yok
      - Yahoo chart fiyat verebilir ama skor girdisi (bilanço) eksik

    Bu yüzden --walk-forward bayrağı şu an sadece uyarı üretir; full historical rescoring
    için ayrı veri deposu gerekir.
    """
    return (
        "WALK-FORWARD NOTU: Mevcut backtest look-ahead'e yakındır (bugünkü skor × geçmiş getiri). "
        "Tam point-in-time için tarihsel mali tablo arşivi + çeyrek-çeyrek rescoring gerekir; "
        "XI_29 API yalnızca son 8 çeyreği sunuyor."
    )


def backtest_component_report(df):
    """Skor bileşenleri ile 6A/12A getiri Spearman korelasyonu + rejim uyarısı."""
    rows = []
    rets = [c for c in ['Backtest 6A İleri Getiri', 'Backtest 12A İleri Getiri'] if c in df.columns]
    comps = [c for c in [
        'Final Score', 'Değer Skoru', 'Kârlılık Skoru', 'Büyüme Skoru',
        'Bilanço Skoru', 'Nakit Akışı Skoru', 'Peer Skor', 'Risk Skoru',
        'Adil Değer Marjı', 'FCF Getirisi', 'ROE', 'Piotroski F',
        'Owner Earnings Getirisi', 'Sloan Skoru', 'Beneish Skoru',
        'Reverse DCF Implied g', 'EV/Sales', 'RI Marjı', 'ROE-ke Spread',
        'Teknik Skor', 'Confluence Skoru (Kısa)', 'Confluence Skoru (Orta)',
        'Confluence Skoru (Uzun)',
    ] if c in df.columns]
    for ret in rets:
        for comp in comps:
            s = df[[ret, comp]].apply(pd.to_numeric, errors='coerce').dropna()
            if len(s) < 30:
                continue
            corr = float(s[ret].corr(s[comp], method='spearman'))
            rows.append({'Getiri': ret, 'Bileşen': comp, 'Spearman': corr, 'N': len(s)})
    out = pd.DataFrame(rows)
    model_note = ''
    if not out.empty:
        fs = out[(out['Bileşen'] == 'Final Score') & (out['Getiri'].astype(str).str.contains('6A'))]
        if not fs.empty and fs.iloc[0]['Spearman'] < -0.05:
            model_note = 'UYARI: Final Score ile 6A getiri negatif korele — model bu pencerede ters sinyal üretiyor olabilir.'
        elif not fs.empty and fs.iloc[0]['Spearman'] < 0.05:
            model_note = 'NOT: Final Score ile 6A getiri zayıf/nötr — sıralamayı teyit için ek analiz kullanın.'
        else:
            model_note = 'Final Score ile 6A getiri pozitif yönde hizalı (bu pencerede).'
        cf = out[(out['Bileşen'] == 'Confluence Skoru (Orta)') & (out['Getiri'].astype(str).str.contains('6A'))]
        if not cf.empty:
            cf_corr = cf.iloc[0]['Spearman']
            if cf_corr > (fs.iloc[0]['Spearman'] if not fs.empty else -1) + 0.03:
                model_note += ' | Confluence Skoru (Orta), Final Score\'dan daha güçlü öngörü gösteriyor — temel+teknik birleşimi bu pencerede edge sağlıyor.'
            elif cf_corr < 0.02:
                model_note += ' | UYARI: Confluence Skoru bu pencerede anlamlı öngörü göstermiyor — teknik katman gürültü ekliyor olabilir, ağırlıkları gözden geçirin.'
    # Look-ahead / walk-forward uyarısı her zaman eklenir
    model_note = (model_note + ' | ' if model_note else '') + walk_forward_disclaimer()
    return out, model_note



def choose_save(default_path=None):
    """Çıktı yolunu GUI varsa seçtirir; headless/Linux sunucuda otomatik üretir.
    --output verilirse GUI hiç açılmaz."""
    if default_path:
        return str(Path(default_path).expanduser().resolve())
    env_out = os.environ.get("BIST_OUTPUT")
    if env_out:
        return str(Path(env_out).expanduser().resolve())
    try:
        if not os.environ.get("DISPLAY") and sys.platform != "win32":
            outdir = Path.cwd()
            return str(outdir / f"BIST_Degerleme_v192_{datetime.now():%Y%m%d_%H%M}.xlsx")
        from tkinter import Tk, filedialog  # lazy import: headless ortamda modül yüklemesi kırılmasın
        r = Tk(); r.withdraw(); r.attributes("-topmost", True)
        p = filedialog.asksaveasfilename(
            parent=r, title="BIST Değerleme v192 raporunu kaydet",
            defaultextension=".xlsx",
            initialfile=f"BIST_Degerleme_v192_{datetime.now():%Y%m%d_%H%M}.xlsx",
            filetypes=[("Excel", "*.xlsx")])
        r.destroy()
        if p:
            return p
    except Exception as e:
        print("GUI kullanılamadı, otomatik çıktı yoluna geçiliyor:", e, flush=True)
    return str(Path.cwd() / f"BIST_Degerleme_v192_{datetime.now():%Y%m%d_%H%M}.xlsx")




def build_takip_rutini_df():
    """Günlük / haftalık / çeyreklik takip rutini (KAP + TradingView + v7)."""
    rows = [
        ("GÜNLÜK", "1", "KAP", "Takip listesindeki kodlarda özel durum, bilanço, sermaye, yönetim açıklaması tarayın"),
        ("GÜNLÜK", "2", "TradingView", "Aynı liste: fiyat, hacim, XU100/XU030 ve USDTRY genel görünüm"),
        ("GÜNLÜK", "3", "v7 Aday Listesi", "Olumsuz KAP haberi skoru bozuyor mu? Gerekirse adaydan çıkarın (v7'yi her gün çalıştırmayın)"),
        ("HAFTALIK", "1", "TradingView", "Aday Listesi: likidite, 1H/1A getiri; aşırı hareketleri not edin"),
        ("HAFTALIK", "2", "KAP / takvim", "Bu hafta bilanço açıklayacak şirketleri işaretleyin"),
        ("HAFTALIK", "3", "v7 Aday Listesi", "Filtre hâlâ geçerli mi? Güven, adil marj, Net Borç/FCF kontrol"),
        ("ÇEYREKLİK", "1", "v7 motor", "Full analiz çalıştırın → yeni Excel + Aday Listesi + TradingView watchlist dosyası"),
        ("ÇEYREKLİK", "2", "Karşılaştırma", "Eski aday vs yeni aday; portföy rotasyonu notları"),
        ("ÇEYREKLİK", "3", "BACKTEST sayfası", "Hangi bileşen getiri ile ilişkili? Rejim notunu okuyun"),
        ("ÇEYREKLİK", "4", "temelozet Excel", "Sektör haritası güncelse yenileyin"),
        ("ROL", "—", "KAP", "Gerçekte ne oldu? (resmi haber / bilanço)"),
        ("ROL", "—", "TradingView", "Fiyat ne yapıyor? (zamanlama, likidite)"),
        ("ROL", "—", "v7", "Temelde kim daha sağlam/ucuz? (aday havuzu)"),
        ("YAPMAYIN", "—", "—", "Her gün v7 skoruna göre al/sat"),
        ("YAPMAYIN", "—", "—", "Sadece TV sinyaliyle KAP okumadan pozisyon"),
        ("YAPMAYIN", "—", "—", "Banka/sigortayı sanayi skoruyla aynı sepete koyup günlük trade"),
        ("UFUK", "—", "—", "Aday Listesi 6–12 ay temel ufuk içindir; kısa vade trade sinyali değildir"),
    ]
    return pd.DataFrame(rows, columns=["Periyot", "Sıra", "Araç", "Aksiyon"])


def build_kap_filtre_df():
    """KAP bildirim / başlık taraması için anahtar kelime seti."""
    rows = [
        ("YÜKSEK ÖNCELİK", "Sermaye artırımı", "Bedelli/bedelsiz; seyreltme riski", "Adayı gözden geçir"),
        ("YÜKSEK ÖNCELİK", "Bedelli", "Nakit sermaye artırımı", "Adayı gözden geçir"),
        ("YÜKSEK ÖNCELİK", "Özel durum", "Genel uyarı — metni okuyun", "Oku"),
        ("YÜKSEK ÖNCELİK", "Finansal rapor", "Bilanço / faaliyet raporu", "v7 çeyreklik yenileme adayı"),
        ("YÜKSEK ÖNCELİK", "Konsolide olmayan finansal", "Bilanço", "v7 çeyreklik yenileme adayı"),
        ("YÜKSEK ÖNCELİK", "Bağımsız denetim", "Denetim görüşü / sınırlı olumlu", "Kalite notu"),
        ("YÜKSEK ÖNCELİK", "İflas", "Olağanüstü risk", "Ele"),
        ("YÜKSEK ÖNCELİK", "Konkordato", "Olağanüstü risk", "Ele"),
        ("YÜKSEK ÖNCELİK", "Kaydi bilanço", "TMS/TFRS değişiklik", "Not al"),
        ("ORTA", "Temettü", "Nakit dağıtım", "Pozitif/ nötr; payout güncelle"),
        ("ORTA", "Kâr payı", "Temettü", "Pozitif/ nötr"),
        ("ORTA", "Pay geri alım", "Geri alım programı", "Genelde pozitif sinyal"),
        ("ORTA", "Geri alım", "Geri alım", "Genelde pozitif sinyal"),
        ("ORTA", "Borçlanma aracı", "Tahvil/bono ihraç", "Kaldıraç kontrol"),
        ("ORTA", "Kredi", "Kredi sözleşmesi", "Borç/FCF kontrol"),
        ("ORTA", "Garanti", "Keﬁl / teminat", "Bilanço riski"),
        ("ORTA", "İmtiyaz", "İmtiyazlı pay", "Kurumsal yönetim"),
        ("ORTA", "Yönetim kurulu", "YK değişiklik", "Bilgi"),
        ("ORTA", "Pay devri", "Ortaklık yapısı", "Bilgi"),
        ("ORTA", "Çağrı", "Zorunlu çağrı / teklif", "Oku"),
        ("ORTA", "Birleşme", "Birleşme/devralma", "Model varsayımları değişir"),
        ("ORTA", "Devir", "Aktif devri", "Oku"),
        ("SEKTÖR", "Lisans", "Enerji/telekom lisans", "Sektör notu"),
        ("SEKTÖR", "Teşvik", "Yatırım teşviki", "Büyüme notu"),
        ("SEKTÖR", "İhale", "Kamu ihalesi", "Haber etkisi"),
        ("SEKTÖR", "NAV", "GYO net aktif", "GYO değerleme"),
        ("SEKTÖR", "Portföy", "GYO/holding portföy", "Bilgi"),
        ("DÜŞÜK", "Genel kurul", "GK daveti", "Takvim"),
        ("DÜŞÜK", "Vekalet", "GK vekalet", "Bilgi"),
        ("DÜŞÜK", "Ticaret unvanı", "Unvan değişikliği", "Bilgi"),
    ]
    return pd.DataFrame(rows, columns=["Öncelik", "Anahtar Kelime", "Anlam", "Aksiyon"])


def export_tradingview_watchlist(cand_df, out_path):
    """Aday kodlarını TradingView watchlist formatında kaydet (BIST: KOD veya KOD.IS).

    TradingView: Watchlist → … → Import list → bu .txt dosyasını seçin.
    """
    if cand_df is None or cand_df.empty or "Hisse" not in cand_df.columns:
        return None
    codes = []
    for c in cand_df["Hisse"].astype(str):
        c = re.sub(r"[^A-Z0-9]", "", c.strip().upper())
        if not c:
            continue
        # TV BIST sembolleri genelde BIST:TUPRS veya TUPRS.IS
        codes.append(f"BIST:{c}")
    if not codes:
        return None
    out_path = Path(out_path)
    out_path.write_text("\n".join(codes) + "\n", encoding="utf-8")
    # Ayrıca düz kod listesi (KAP / Excel kullanımı)
    plain = out_path.with_name(out_path.stem + "_kodlar.txt")
    plain.write_text("\n".join(c.split(":")[-1] for c in codes) + "\n", encoding="utf-8")
    return str(out_path)


def build_aday_watchlist_df(cand_df):
    """Excel içi watchlist sayfası: TV sembolü + kod + sektör + aday sıra."""
    if cand_df is None or cand_df.empty or "Hisse" not in getattr(cand_df, "columns", []):
        return pd.DataFrame([{"Not": "Aday yok — watchlist boş"}])
    rows = []
    for _, r in cand_df.iterrows():
        code = re.sub(r"[^A-Z0-9]", "", str(r.get("Hisse", "")).strip().upper())
        if not code:
            continue
        rows.append({
            "Aday Sıra": r.get("Aday Sıra", ""),
            "Kod": code,
            "TradingView": f"BIST:{code}",
            "Yahoo": f"{code}.IS",
            "Sektör": r.get("Sektör", ""),
            "Aday Skoru": r.get("Aday Skoru", ""),
            "Risk Ayarlı Skor": r.get("Risk Ayarlı Skor", ""),
            "Katalizörler": r.get("Katalizörler", ""),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame([{"Not": "Aday yok"}])




def _dd_val(row, col, default=None):
    try:
        v = row.get(col, default)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        return v
    except Exception:
        return default


def _dd_num(row, col, default=np.nan):
    v = _dd_val(row, col, default)
    try:
        return float(v)
    except Exception:
        return default


def _dd_txt(row, col, default=""):
    v = _dd_val(row, col, default)
    return "" if v is None else str(v)


def _fmt_pct(x, digits=1):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    try:
        return f"{float(x)*100:.{digits}f}%"
    except Exception:
        return "n/a"


def _fmt_num(x, digits=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return "n/a"


def build_due_diligence_for_row(row):
    """Her hisse için 5 maddelik due-diligence + derin değerlendirme yorumu.

    Kolonlar:
      DD1..DD5, DD Özet, DD Geçen, DD Kalan, DD Aksiyon
      Değerlendirme (kısa özet paragraf)
      Derin Analiz (çok satırlı detay)
      Ana Risk
      Yatırım Tezi
    """
    hisse = _dd_txt(row, "Hisse", "?")
    sektor = _dd_txt(row, "Sektör", "")
    fcf_y = _dd_num(row, "FCF Getirisi")
    oe_y = _dd_num(row, "Owner Earnings Getirisi")
    fcf_ni = _dd_num(row, "FCF/Net Kar")
    nd_fcf = _dd_num(row, "Net Borç/FCF")
    net_cash_mcap = _dd_num(row, "Net Nakit/Piyasa Değeri")
    debt_eq = _dd_num(row, "Borç/Özkaynak")
    curr = _dd_num(row, "Cari Oran")
    sloan = _dd_num(row, "Sloan Skoru")
    accrual = _dd_num(row, "Accrual Kalitesi")
    piot = _dd_num(row, "Piotroski F")
    roe = _dd_num(row, "ROE")
    roic = _dd_num(row, "ROIC")
    med_m = _dd_num(row, "Adil Değer Marjı")
    ri_m = _dd_num(row, "RI Marjı")
    fk = _dd_num(row, "F/K")
    pddd = _dd_num(row, "PD/DD")
    regime = _dd_txt(row, "Rejim Notu")
    val_cls = _dd_txt(row, "Değerleme Sınıfı")
    risk_cls = _dd_txt(row, "Risk Sınıfı")
    altman = _dd_txt(row, "Altman Bölge")
    capex_am = _dd_num(row, "CapEx/Amortisman")
    rev_yoy = _dd_num(row, "Revenue YoY")
    ni_yoy = _dd_num(row, "Net Kar YoY")
    fcf_trend = _dd_num(row, "FCF Conversion Trend")
    trend8 = _dd_num(row, "Net Kâr Trend Tutarlılığı (8Ç)")
    bilanco = _dd_num(row, "Bilanço Skoru")
    nakit_sk = _dd_num(row, "Nakit Akışı Skoru")
    deger_sk = _dd_num(row, "Değer Skoru")
    kar_sk = _dd_num(row, "Kârlılık Skoru")
    final_sc = _dd_num(row, "Final Score")
    if np.isnan(final_sc):
        final_sc = _dd_num(row, "Risk Ayarlı Skor")
    not_ = _dd_txt(row, "Not")
    dcf_base = _dd_num(row, "DCF Base")
    graham_m = _dd_num(row, "Graham Güvenlik Marjı")
    bt6 = _dd_num(row, "Backtest 6A İleri Getiri")
    bt12 = _dd_num(row, "Backtest 12A İleri Getiri")
    peer = _dd_num(row, "Peer Skor")
    ke = _dd_num(row, "WACC (ke)")
    roe_ke = _dd_num(row, "ROE-ke Spread")
    pos_drv = _dd_txt(row, "Skor Sürücüleri (+)")
    neg_drv = _dd_txt(row, "Skor Sürücüleri (-)")
    karar = _dd_txt(row, "Yatırım Kararı")

    items = []

    # 1) Nakit
    if (pd.notna(fcf_y) and fcf_y > 0.03) or (pd.notna(oe_y) and oe_y > 0.05):
        if pd.notna(fcf_y) and fcf_y > 0.40:
            items.append("1.NAKİT: DİKKAT | FCF getirisi aşırı yüksek — tek seferlik nakit/aktif satışı olabilir, dipnot kontrol")
        elif pd.notna(fcf_ni) and 0.4 <= fcf_ni <= 1.5:
            items.append("1.NAKİT: GEÇTİ | FCF/OE pozitif ve kâra dönüşüm makul")
        elif pd.notna(oe_y) and oe_y > 0.05:
            items.append("1.NAKİT: GEÇTİ | Owner Earnings getirisi pozitif (FCF zayıf olsa da OE destekliyor)")
        else:
            items.append("1.NAKİT: DİKKAT | Nakit pozitif ama FCF/Net Kâr dönüşümü zayıf veya dengesiz")
    elif pd.notna(fcf_y) and fcf_y < 0 and (pd.isna(oe_y) or oe_y < 0):
        items.append("1.NAKİT: KALDI | FCF ve OE getirisi negatif — nakit üretimi yok")
    else:
        items.append("1.NAKİT: DİKKAT | Nakit sinyali zayıf/eksik — tablo dipnotundan teyit et")

    # 2) Borç
    net_cash = (pd.notna(nd_fcf) and nd_fcf < 0) or (pd.notna(net_cash_mcap) and net_cash_mcap > 0.05)
    if net_cash and (pd.isna(debt_eq) or debt_eq < 1.0):
        items.append("2.BORÇ: GEÇTİ | Net nakit veya düşük kaldıraç — bilanço esnek")
    elif pd.notna(nd_fcf) and 0 <= nd_fcf < 3 and (pd.isna(debt_eq) or debt_eq < 1.5):
        items.append("2.BORÇ: GEÇTİ | Net borç/FCF makul (<3) — kaldıraç kontrol altında")
    elif pd.notna(nd_fcf) and nd_fcf >= 5:
        items.append("2.BORÇ: KALDI | Net borç/FCF yüksek — refinansman/faiz riski")
    elif pd.notna(curr) and curr < 0.8:
        items.append("2.BORÇ: DİKKAT | Cari oran düşük — kısa vadeli likiditeyi kontrol et")
    else:
        items.append("2.BORÇ: DİKKAT | Borç profili nötr — vade ve faiz yükünü oku")

    # 3) Kalite
    if (pd.notna(sloan) and sloan >= 0.7) and (pd.notna(accrual) and accrual >= 0.6) and (pd.isna(piot) or piot >= 6):
        if "enflasyon" in regime.lower() or "artefakt" in regime.lower() or "muhasebe" in regime.lower():
            items.append("3.KALİTE: DİKKAT | Sloan/Accrual iyi ama rejim notu artefakt uyarısı veriyor")
        else:
            items.append("3.KALİTE: GEÇTİ | Sloan/Accrual/Piotroski kâr kalitesini destekliyor")
    elif "enflasyon" in regime.lower() or "artefakt" in regime.lower():
        items.append("3.KALİTE: DİKKAT | Büyüme/kâr enflasyon veya muhasebe artefaktı olabilir")
    elif pd.notna(sloan) and sloan < 0.4:
        items.append("3.KALİTE: KALDI | Sloan zayıf — tahakkuk kalitesi düşük")
    else:
        items.append("3.KALİTE: DİKKAT | Kâr kalitesi karışık — yeniden değerleme/kur farkı ara")

    # 4) İş modeli
    if pd.notna(roe) and roe >= 0.15 and (pd.isna(roic) or roic >= 0.10):
        items.append("4.İŞ MODELİ: GEÇTİ | ROE/ROIC makul — sermaye getirisi kabul edilebilir")
    elif pd.notna(roe) and roe < 0.05:
        items.append("4.İŞ MODELİ: KALDI | ROE çok düşük — ucuzluk tek başına yetmez")
    elif "GYO" in sektor.upper() or "gayrimenkul" in sektor.lower():
        items.append("4.İŞ MODELİ: DİKKAT | GYO — NAV/kira nakdi ile yeniden değerleme kârını ayır")
    elif "dağıtım" in sektor.lower() or "elektrik" in sektor.lower() or "doğalgaz" in sektor.lower():
        items.append("4.İŞ MODELİ: DİKKAT | Regüle dağıtım — tarife/regülasyon riskini oku, büyüme sınırlı olabilir")
    else:
        items.append("4.İŞ MODELİ: DİKKAT | Getiri orta — sektör dinamikleri ve rekabeti kontrol et")

    # 5) Fiyat
    cheap = (pd.notna(med_m) and med_m > 0.10) or val_cls in ("İskontolu", "Derin İskonto")
    quality_ok = (pd.notna(sloan) and sloan >= 0.6) and (pd.isna(piot) or piot >= 6)
    if cheap and quality_ok and ((pd.notna(fcf_y) and fcf_y > 0) or (pd.notna(oe_y) and oe_y > 0)):
        items.append("5.FİYAT: GEÇTİ | İskonto + kalite + nakit birlikte — saf değer tuzağına daha az benziyor")
    elif cheap and (pd.notna(roe) and roe < 0.06):
        items.append("5.FİYAT: KALDI | Ucuz ama düşük ROE — değer tuzağı riski yüksek")
    elif val_cls in ("Pahalı", "Çok Pahalı"):
        items.append("5.FİYAT: DİKKAT | Değerleme pahalı — kalite varsa bekle/kademeli, acele alma")
    elif pd.notna(ri_m) and ri_m < -0.4 and (pd.isna(med_m) or med_m < 0.05):
        items.append("5.FİYAT: DİKKAT | RI negatif/zayıf — medyan iskonto yanıltıcı olabilir")
    else:
        items.append("5.FİYAT: DİKKAT | Fiyat makul; güvenlik marjı sınırlı — katalizör bekle")

    gecen = sum(1 for x in items if "GEÇTİ" in x)
    kalan = sum(1 for x in items if "KALDI" in x)
    dikkat = sum(1 for x in items if "DİKKAT" in x)

    if kalan >= 2:
        aksiyon = "ELE / beklet — kritik maddeler kalıyor"
    elif gecen >= 4 and kalan == 0:
        aksiyon = "GÜÇLÜ ADAY — due-diligence yeşil; yine de dipnot oku"
    elif gecen >= 3 and kalan == 0:
        aksiyon = "İZLEME → şartlı aday — DİKKAT maddelerini kapat"
    else:
        aksiyon = "İZLEME — ek teyit şart"

    ozet = f"{hisse}: {gecen}/5 GEÇTİ, {dikkat} DİKKAT, {kalan} KALDI → {aksiyon}"

    # ---------- Derin değerlendirme metni ----------
    guclu = []
    zayif = []
    celiski = []

    if net_cash:
        guclu.append(f"net nakit/esnek bilanço (NetNakit/PD={_fmt_pct(net_cash_mcap)}, NetBorç/FCF={_fmt_num(nd_fcf,1)})")
    if pd.notna(oe_y) and oe_y > 0.06:
        guclu.append(f"Owner Earnings getirisi güçlü ({_fmt_pct(oe_y)})")
    if pd.notna(fcf_y) and fcf_y > 0.06:
        guclu.append(f"FCF getirisi güçlü ({_fmt_pct(fcf_y)})")
    if pd.notna(fcf_ni) and fcf_ni >= 0.5:
        guclu.append(f"kârın nakde dönüşümü iyi (FCF/NK={_fmt_num(fcf_ni,2)})")
    if pd.notna(sloan) and sloan >= 0.7:
        guclu.append(f"Sloan/accrual kalitesi yüksek ({_fmt_num(sloan,2)})")
    if pd.notna(piot) and piot >= 7:
        guclu.append(f"Piotroski {int(piot)}")
    if pd.notna(roe) and roe >= 0.15:
        guclu.append(f"ROE {_fmt_pct(roe)}")
    if pd.notna(bilanco) and bilanco >= 0.85:
        guclu.append("bilanço skoru çok yüksek")
    if cheap:
        guclu.append(f"çarpan/medyan iskontosu ({val_cls or 'iskontolu'}, marj={_fmt_pct(med_m)})")
    if pd.notna(peer) and peer >= 70:
        guclu.append(f"peer skoru yüksek ({_fmt_num(peer,0)})")
    if pd.notna(bt12) and bt12 > 0.15:
        guclu.append(f"backtest 12A pozitif ({_fmt_pct(bt12)})")

    if pd.notna(fcf_y) and fcf_y < 0.02 and (pd.isna(oe_y) or oe_y < 0.05):
        zayif.append("nakit getirisi zayıf")
    if pd.notna(fcf_y) and fcf_y > 0.35:
        zayif.append("FCF getirisi aşırı yüksek — sürdürülebilirlik şüpheli")
    if pd.notna(capex_am) and capex_am >= 3:
        zayif.append(f"CapEx/Amortisman çok yüksek ({_fmt_num(capex_am,1)}) — FCF baskı altında olabilir")
    if pd.notna(trend8) and trend8 < 0.4:
        zayif.append(f"8Ç kâr trend tutarlılığı düşük ({_fmt_num(trend8,2)})")
    if pd.notna(roe) and roe < 0.08:
        zayif.append(f"ROE düşük ({_fmt_pct(roe)})")
    if pd.notna(roe_ke) and roe_ke < 0:
        zayif.append(f"ROE-ke spread negatif ({_fmt_pct(roe_ke)}) — ekonomik kâr zayıf")
    if pd.notna(ri_m) and ri_m < -0.3:
        zayif.append(f"RI marjı zayıf ({_fmt_pct(ri_m)})")
    if pd.notna(dcf_base) and dcf_base < 0:
        zayif.append("DCF Base negatif — nakit akışı modeli değer üretmiyor")
    if pd.notna(curr) and curr < 0.8:
        zayif.append(f"cari oran düşük ({_fmt_num(curr,2)})")
    if "Riskli" in altman or "Sıkıntı" in altman:
        zayif.append(f"Altman bölgesi: {altman}")
    if "enflasyon" in regime.lower() or "artefakt" in regime.lower():
        zayif.append("rejim notu: büyüme enflasyon/muhasebe artefaktı olabilir")
    if pd.notna(bt12) and bt12 < -0.1:
        zayif.append(f"backtest 12A negatif ({_fmt_pct(bt12)})")
    if "GYO" in sektor.upper():
        zayif.append("GYO: yeniden değerleme kârı ile nakit kira ayrılmalı")

    # Çelişki: medyan ucuz ama RI/DCF kötü
    if cheap and ((pd.notna(ri_m) and ri_m < -0.3) or (pd.notna(dcf_base) and dcf_base < 0)):
        celiski.append("Medyan/Graham iskontolu görünürken RI/DCF zayıf — çarpan ucuzluğu nakit modeliyle tutarsız")
    if (pd.notna(oe_y) and oe_y > 0.08) and (pd.notna(fcf_y) and fcf_y < 0.03):
        celiski.append("OE güçlü / FCF zayıf — yatırım (CapEx) yoğunluğu veya işletme sermayesi etkisi kontrol edilmeli")
    if (pd.notna(deger_sk) and deger_sk >= 0.6) and (pd.notna(kar_sk) and kar_sk < 0.45):
        celiski.append("Değer skoru yüksek ama kârlılık skoru düşük — değer tuzağı riski")

    # Ana risk
    if pd.notna(capex_am) and capex_am >= 3 and (pd.isna(fcf_y) or fcf_y < 0.05):
        ana_risk = "Yüksek CapEx altında FCF erimesi / yatırım döngüsü riski"
    elif pd.notna(trend8) and trend8 < 0.35:
        ana_risk = "Dalgalı/bozulan kâr trendi — momentum ve revizyon riski"
    elif "GYO" in sektor.upper():
        ana_risk = "GYO yeniden değerleme vs nakit kira ayrımı; NAV iskontosu yanıltıcı olabilir"
    elif "dağıtım" in sektor.lower() or "doğalgaz" in sektor.lower() or "elektrik" in sektor.lower():
        ana_risk = "Regülasyon/tarife riski + kısa vadeli likidite"
    elif pd.notna(nd_fcf) and nd_fcf >= 5:
        ana_risk = "Yüksek net borç/FCF — faiz ve refinansman"
    elif cheap and (pd.notna(roe) and roe < 0.08):
        ana_risk = "Ucuz bilanço + düşük getiri = değer tuzağı"
    elif pd.notna(roe_ke) and roe_ke < -0.05:
        ana_risk = "Sermaye maliyetinin altında getiri (ROE<ke)"
    else:
        ana_risk = "Standart piyasa/sektör riski — dipnot ve KAP teyidi gerekir"

    # Yatırım tezi
    if gecen >= 4 and kalan == 0 and (pd.notna(fcf_y) and fcf_y > 0.05 or pd.notna(oe_y) and oe_y > 0.08):
        tez = "Nakit + kalite + makul/iskontolu fiyat; bilanço destekli izleme/şartlı aday"
    elif net_cash and cheap:
        tez = "Net nakit yastıklı bilanço iskontosu; DCF/RI zayıfsa pozisyon küçük tutulmalı"
    elif pd.notna(fcf_y) and fcf_y > 0.08 and not cheap:
        tez = "Nakit getirisi odaklı (büyüme değil); regüle/olgun iş modeli profili"
    elif cheap and (pd.notna(roe) and roe < 0.08):
        tez = "Saf ucuzluk — model backtest'ine göre tek başına zayıf sinyal"
    else:
        tez = "Karma sinyal — kalite/nakit/fiyat üçlüsünden eksik olanı tamamlamadan işlem düşünme"

    guclu_s = "; ".join(guclu) if guclu else "belirgin güç yok"
    zayif_s = "; ".join(zayif) if zayif else "belirgin zayıflık öne çıkmıyor"
    celiski_s = "; ".join(celiski) if celiski else "yok"

    degerlendirme = (
        f"{hisse} ({sektor or '—'}): Skor {_fmt_num(final_sc,1)} {not_} | {val_cls or '—'} | Risk:{risk_cls or '—'} | "
        f"F/K={_fmt_num(fk,1)} PD/DD={_fmt_num(pddd,2)} | FCF={_fmt_pct(fcf_y)} OE={_fmt_pct(oe_y)} | "
        f"ROE={_fmt_pct(roe)} | {aksiyon}"
    )

    derin = (
        f"GÜÇLÜ: {guclu_s}. "
        f"ZAYIF: {zayif_s}. "
        f"ÇELİŞKİ: {celiski_s}. "
        f"ANA RİSK: {ana_risk}. "
        f"TEZ: {tez}. "
        f"Sürücüler(+): {pos_drv or '—'} | Sürücüler(-): {neg_drv or '—'} | "
        f"Karar etiketi: {karar or '—'}."
    )

    return {
        "DD1 Nakit": items[0],
        "DD2 Borç": items[1],
        "DD3 Kalite": items[2],
        "DD4 İş Modeli": items[3],
        "DD5 Fiyat": items[4],
        "DD Özet": ozet,
        "DD Geçen": gecen,
        "DD Kalan": kalan,
        "DD Aksiyon": aksiyon,
        "Değerlendirme": degerlendirme,
        "Derin Analiz": derin,
        "Ana Risk": ana_risk,
        "Yatırım Tezi": tez,
        "Güçlü Yanlar": guclu_s,
        "Zayıf Yanlar": zayif_s,
        "Çelişkiler": celiski_s,
    }


def _dd_column_names():
    return [
        "DD1 Nakit", "DD2 Borç", "DD3 Kalite", "DD4 İş Modeli", "DD5 Fiyat",
        "DD Özet", "DD Geçen", "DD Kalan", "DD Aksiyon",
        "Değerlendirme", "Derin Analiz", "Ana Risk", "Yatırım Tezi",
        "Güçlü Yanlar", "Zayıf Yanlar", "Çelişkiler",
    ]


def _drop_dd_cols(df):
    """Yinelenen DD kolonlarını temizle (DataFrame.map hatasını önler)."""
    if df is None or df.empty:
        return df
    cols = [c for c in df.columns if c in _dd_column_names() or str(c).startswith("DD ")]
    # isim çakışması / duplicate labels
    df = df.drop(columns=cols, errors="ignore")
    # duplicate column names genel temizlik
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()].copy()
    return df




def assign_kova_series(df):
    """assign_kova vektörleştirilmiş (apply yerine)."""
    if df is None or df.empty:
        return pd.Series(dtype=object)
    sh = df["Shannon Notu"].astype(str) if "Shannon Notu" in df.columns else pd.Series("", index=df.index)
    dd = df["DD Aksiyon"].astype(str) if "DD Aksiyon" in df.columns else pd.Series("", index=df.index)
    kf = pd.to_numeric(df["Kelly Final"], errors="coerce") if "Kelly Final" in df.columns else pd.Series(np.nan, index=df.index)
    if kf.isna().all() and "Kelly Pay Proxy" in df.columns:
        kf = pd.to_numeric(df["Kelly Pay Proxy"], errors="coerce")
    sektor = df["Sektör"].astype(str) if "Sektör" in df.columns else pd.Series("", index=df.index)
    kural = df["Kural Sınıfı"].astype(str) if "Kural Sınıfı" in df.columns else pd.Series("", index=df.index)

    dd_strong = dd.str.contains("GÜÇLÜ", na=False)
    dd_ele = dd.str.contains("ELE", na=False)
    dd_izle = dd.str.contains("İZLEME", na=False) | dd_ele
    net = sh.eq("NET SİNYAL")
    noisy = sh.isin(["GÜRÜLTÜ", "VERİ EKSİK"])
    kelly_pos = kf.notna() & (kf > 0.01)
    kelly_zero = kf.isna() | (kf <= 0.01)
    gyo = sektor.str.upper().str.contains("GYO", na=False) | sektor.str.lower().str.contains("gayrimenkul", na=False)

    out = pd.Series("3 BEKLE", index=df.index, dtype=object)
    # order matters: start default 3, then upgrade
    m2 = dd_strong & kelly_pos
    out = out.where(~m2, "2 ŞARTLI")
    m2b = (dd.str.contains("şartlı", case=False, na=False) | dd_izle) & kelly_pos
    out = out.where(~m2b, "2 ŞARTLI")
    m2c = kural.eq("GÜÇLÜ ADAY") & kelly_pos
    out = out.where(~m2c, "2 ŞARTLI")
    m1 = net & dd_strong & kelly_pos & ~gyo
    out = out.where(~m1, "1 ÖNCELİK")
    m1g = net & dd_strong & kelly_pos & gyo
    out = out.where(~m1g, "2 ŞARTLI")
    # force bekle
    out = out.where(~(net & kelly_zero), "3 BEKLE")
    out = out.where(~(noisy | dd_ele), "3 BEKLE")
    return out

def assign_kova(row):
    """3 kova: öncelik sırası (al sinyali değil).

    1 ÖNCELİK  = NET SİNYAL + DD GÜÇLÜ + Kelly > 0
    2 ŞARTLI   = DD iyi ama KARIŞIK / edge zayıf / şartlı izleme
    3 BEKLE    = Kelly≈0 veya GÜRÜLTÜ veya ağır şüphe / ele
    """
    sh = str(row.get("Shannon Notu") or "")
    dd = str(row.get("DD Aksiyon") or "")
    try:
        kelly = float(row.get("Kelly Final"))
        if not np.isfinite(kelly):
            kelly = float(row.get("Kelly Pay Proxy"))
    except Exception:
        try:
            kelly = float(row.get("Kelly Pay Proxy"))
        except Exception:
            kelly = float("nan")
    sektor = str(row.get("Sektör") or "")
    kural = str(row.get("Kural Sınıfı") or "")

    dd_strong = "GÜÇLÜ" in dd
    dd_izle = "İZLEME" in dd or "ELE" in dd
    net = sh == "NET SİNYAL"
    noisy = sh in ("GÜRÜLTÜ", "VERİ EKSİK")
    kelly_pos = np.isfinite(kelly) and kelly > 0.01
    kelly_zero = (not np.isfinite(kelly)) or kelly <= 0.01
    gyo = "GYO" in sektor.upper() or "gayrimenkul" in sektor.lower()

    if noisy or "ELE" in dd:
        return "3 BEKLE"
    if net and dd_strong and kelly_pos and not gyo:
        return "1 ÖNCELİK"
    if net and dd_strong and kelly_pos and gyo:
        return "2 ŞARTLI"  # GYO: net olsa da NAV teyidi
    if net and kelly_zero:
        return "3 BEKLE"  # net ama edge yok
    if dd_strong and kelly_pos:
        return "2 ŞARTLI"
    if "şartlı" in dd.lower() or dd_izle:
        return "2 ŞARTLI" if kelly_pos else "3 BEKLE"
    if kural == "GÜÇLÜ ADAY" and kelly_pos:
        return "2 ŞARTLI"
    return "3 BEKLE"


def assign_kova_aksiyon(kova):
    k = str(kova or "")
    if k.startswith("1"):
        return "Bu hafta detay incele · dipnot + KAP"
    if k.startswith("2"):
        return "Nakit/NAV/regülasyon teyidi · acele alma"
    return "İşlem yok · izleme listesinde tut"


def enrich_candidates_with_dd(cand_df):
    """Aday listesine due-diligence ve derin analiz kolonlarını ekler."""
    if cand_df is None or cand_df.empty:
        return cand_df
    base = _drop_dd_cols(cand_df.copy())
    rows = [build_due_diligence_for_row(row) for _, row in base.iterrows()]
    dd_df = pd.DataFrame(rows)
    out = pd.concat([base.reset_index(drop=True), dd_df.reset_index(drop=True)], axis=1)
    if out.columns.duplicated().any():
        out = out.loc[:, ~out.columns.duplicated()].copy()
    prio = {
        "GÜÇLÜ ADAY — due-diligence yeşil; yine de dipnot oku": 0,
        "İZLEME → şartlı aday — DİKKAT maddelerini kapat": 1,
        "İZLEME — ek teyit şart": 2,
        "ELE / beklet — kritik maddeler kalıyor": 3,
    }
    # 3 kova (Shannon + DD + Kelly)
    out["Kova"] = assign_kova_series(out)
    out["Kova Aksiyon"] = out["Kova"].map(assign_kova_aksiyon)

    kova_prio = {"1 ÖNCELİK": 0, "2 ŞARTLI": 1, "3 BEKLE": 2}
    out["_k"] = out["Kova"].map(kova_prio).fillna(9)
    # v192: Tier A > B > C sırası korunur (Tier C, Tier A/B'nin önüne geçmesin)
    out["_t"] = (out["Aday Modu"].astype(str).map(lambda x: 0 if x.startswith("Tier A") else 1 if x.startswith("Tier B") else 2)
                 if "Aday Modu" in out.columns else 0)
    if "DD Aksiyon" in out.columns:
        aks = out["DD Aksiyon"]
        if isinstance(aks, pd.DataFrame):
            aks = aks.iloc[:, 0]
        out["_p"] = aks.map(lambda x: prio.get(x, 9) if isinstance(x, str) else 9)
    else:
        out["_p"] = 9
    _sk = [c for c in ["_t", "_k", "_p", "Kaçırılan Sayısı", "Aday Skoru", "Risk Ayarlı Skor"] if c in out.columns]
    _asc = {"_t": True, "_k": True, "_p": True, "Kaçırılan Sayısı": True, "Aday Skoru": False, "Risk Ayarlı Skor": False}
    out = out.sort_values(by=_sk, ascending=[_asc[c] for c in _sk])
    out = out.drop(columns=[c for c in ["_p", "_k", "_t"] if c in out.columns])
    if len(out) > 15:
        out = out.head(15).copy()
    out["Aday Sıra"] = range(1, len(out) + 1)
    # Kova kolonunu öne al
    cols = list(out.columns)
    for c in ("Kova Aksiyon", "Kova"):
        if c in cols:
            cols.insert(1, cols.pop(cols.index(c)))
    out = out[cols]
    return out


def enrich_all_with_dd(df):
    """Tüm hisseler için Değerleme Özeti'ne DD + derin değerlendirme kolonlarını ekler."""
    if df is None or df.empty:
        return df
    base = _drop_dd_cols(df.copy())
    rows = [build_due_diligence_for_row(row) for _, row in base.iterrows()]
    dd_df = pd.DataFrame(rows)
    out = pd.concat([base.reset_index(drop=True), dd_df.reset_index(drop=True)], axis=1)
    if out.columns.duplicated().any():
        out = out.loc[:, ~out.columns.duplicated()].copy()
    return out


def build_due_diligence_sheet(cand_df):
    """Due Diligence sayfası: özet + 5 madde + derin analiz."""
    if cand_df is None or cand_df.empty:
        return pd.DataFrame([{"Not": "Aday yok — DD üretilemedi"}])
    cols = [
        "Aday Sıra", "Hisse", "Sektör", "Final Score", "DD Aksiyon", "DD Özet",
        "DD Geçen", "DD Kalan",
        "DD1 Nakit", "DD2 Borç", "DD3 Kalite", "DD4 İş Modeli", "DD5 Fiyat",
        "Değerlendirme", "Yatırım Tezi", "Ana Risk", "Güçlü Yanlar", "Zayıf Yanlar",
        "Çelişkiler", "Derin Analiz",
    ]
    rows = []
    for _, r in cand_df.iterrows():
        base = {c: r.get(c, "") for c in cols}
        if not base.get("Final Score"):
            base["Final Score"] = r.get("Risk Ayarlı Skor", "")
        rows.append(base)
    return pd.DataFrame(rows)



# ---------------------------------------------------------------------------
# Shannon bilgi teorisi katmanı (v7.2)
# H = -sum p log2 p  (bileşen skorları olasılık gibi normalize)
# Düşük entropy = bileşenler bir yöne yığılmış = daha net sinyal
# Kelly proxy = edge / belirsizlik, [0, 0.25] ile sınırlı (tam Kelly değil)
# ---------------------------------------------------------------------------

def _shannon_entropy_from_scores(scores, temperature=0.18):
    """Skor vektöründen softmax-Shannon entropy (bit).

    Ham normalize entropy çoğu hisste ~max çıkar (skorlar benzer ölçekte).
    Softmax(temperature) farkları büyütür: baskın bileşen varsa H düşer.
    """
    arr = np.array([float(x) for x in scores if x is not None and np.isfinite(x)], dtype=float)
    if arr.size < 2:
        return np.nan
    arr = np.clip(arr, 0.0, None)
    if arr.sum() <= 0:
        return np.nan
    z = arr / max(temperature, 1e-6)
    z = z - np.max(z)
    e = np.exp(z)
    p = e / e.sum()
    p = p[p > 0]
    return float(-np.sum(p * np.log2(p)))


def build_shannon_for_row(row):
    """Her hisse için Shannon entropy, bilgi-ayarlı güven ve Kelly proxy."""
    comps = []
    for col in ["Değer Skoru", "Kârlılık Skoru", "Büyüme Skoru", "Bilanço Skoru", "Nakit Akışı Skoru"]:
        try:
            v = row.get(col)
            if v is not None and np.isfinite(float(v)):
                comps.append(float(v))
        except Exception:
            pass
    if len(comps) < 3:
        for col in ["Peer Skor", "Economic Quality Skoru"]:
            try:
                v = row.get(col)
                if v is not None and np.isfinite(float(v)):
                    fv = float(v)
                    comps.append(fv / 100.0 if fv > 1.5 else fv)
            except Exception:
                pass

    H = _shannon_entropy_from_scores(comps)
    n = len(comps)
    H_max = np.log2(n) if n >= 2 else np.nan
    H_norm = float(H / H_max) if (np.isfinite(H) and np.isfinite(H_max) and H_max > 0) else np.nan

    try:
        conf = float(row.get("Güven Skoru")) if row.get("Güven Skoru") is not None else np.nan
    except Exception:
        conf = np.nan
    # Daha yumuşak ceza: yüksek entropy güveni kısar ama yok etmez
    if np.isfinite(conf) and np.isfinite(H_norm):
        shannon_conf = float(np.clip(conf * (1.0 - 0.35 * H_norm), 0, 100))
    else:
        shannon_conf = conf

    edges = []
    for col, w in [
        ("Adil Değer Marjı", 1.0),
        ("Owner Earnings Getirisi", 0.8),
        ("FCF Getirisi", 0.8),
        ("ROE-ke Spread", 0.5),
    ]:
        try:
            v = row.get(col)
            if v is not None and np.isfinite(float(v)):
                edges.append(w * float(v))
        except Exception:
            pass
    edge = float(np.nanmean(edges)) if edges else np.nan

    unc = 0.5
    if np.isfinite(H_norm):
        unc = 0.30 + 0.70 * H_norm
    if np.isfinite(conf):
        unc = 0.5 * unc + 0.5 * (1.0 - conf / 100.0)
    unc = float(np.clip(unc, 0.15, 1.0))

    if np.isfinite(edge) and edge > 0 and unc > 0:
        kelly = float(np.clip(edge / (unc * unc), 0.0, 0.25))
    elif np.isfinite(edge) and edge <= 0:
        kelly = 0.0
    else:
        kelly = np.nan

    # Sınıflandırma: orijinal güven + H_norm (shannon_conf çok düşük kalmasın diye)
    if np.isfinite(H_norm):
        if H_norm <= 0.70 and np.isfinite(conf) and conf >= 60:
            shannon_note = "NET SİNYAL"
        elif H_norm >= 0.92 and (not np.isfinite(conf) or conf < 70):
            shannon_note = "GÜRÜLTÜ"
        elif H_norm >= 0.95:
            shannon_note = "GÜRÜLTÜ"
        else:
            shannon_note = "KARIŞIK"
    else:
        shannon_note = "VERİ EKSİK"

    if np.isfinite(kelly):
        if kelly >= 0.12 and shannon_note == "NET SİNYAL":
            poz = "Kelly: yüksek pay (≤%25 cap)"
        elif kelly >= 0.05:
            poz = "Kelly: orta pay"
        elif kelly > 0:
            poz = "Kelly: düşük pay"
        else:
            poz = "Kelly: pay yok (edge≤0)"
    else:
        poz = "Kelly: hesaplanamadı"

    return {
        "Shannon H (bit)": None if not np.isfinite(H) else round(H, 4),
        "Shannon H norm": None if not np.isfinite(H_norm) else round(H_norm, 4),
        "Shannon Güven": None if not np.isfinite(shannon_conf) else round(shannon_conf, 2),
        "Shannon Edge": None if not np.isfinite(edge) else round(edge, 4),
        "Kelly Pay Proxy": None if not np.isfinite(kelly) else round(kelly, 4),
        "Shannon Notu": shannon_note,
        "Shannon Pozisyon": poz,
    }


def enrich_all_with_shannon(df):
    """Shannon kolonlarını tüm hisselere ekler."""
    if df is None or df.empty:
        return df
    sh_cols = [
        "Shannon H (bit)", "Shannon H norm", "Shannon Güven", "Shannon Edge",
        "Kelly Pay Proxy", "Shannon Notu", "Shannon Pozisyon",
    ]
    base = df.drop(columns=[c for c in sh_cols if c in df.columns], errors="ignore")
    rows = [build_shannon_for_row(row) for _, row in base.iterrows()]
    sh_df = pd.DataFrame(rows)
    out = pd.concat([base.reset_index(drop=True), sh_df.reset_index(drop=True)], axis=1)
    if out.columns.duplicated().any():
        out = out.loc[:, ~out.columns.duplicated()].copy()
    return out


def build_shannon_sheet(df):
    """Shannon özet sayfası."""
    if df is None or df.empty:
        return pd.DataFrame([{"Not": "Shannon veri yok"}])
    cols = [
        "Hisse", "Sektör", "Final Score", "Güven Skoru",
        "Shannon H (bit)", "Shannon H norm", "Shannon Güven",
        "Shannon Edge", "Kelly Pay Proxy", "Shannon Notu", "Shannon Pozisyon",
        "DD Aksiyon", "Adil Değer Marjı", "FCF Getirisi", "Owner Earnings Getirisi",
    ]
    use = [c for c in cols if c in df.columns]
    out = df[use].copy()
    if "Shannon Notu" in out.columns:
        order = {"NET SİNYAL": 0, "KARIŞIK": 1, "GÜRÜLTÜ": 2, "VERİ EKSİK": 3}
        out["_o"] = out["Shannon Notu"].map(order).fillna(9)
        sort_by = [c for c in ["_o", "Kelly Pay Proxy", "Shannon Güven", "Final Score"] if c in out.columns]
        asc = [True] + [False] * (len(sort_by) - 1)
        out = out.sort_values(sort_by, ascending=asc).drop(columns=["_o"])
    return out.reset_index(drop=True)


def build_gyo_nav_list(df, n=None):
    """GYO NAV modeli (DENEYSEL) sayfası: yalnızca GYO'lar, NAV iskontosuna göre sıralı.
    En negatif (en büyük) iskonto = NAV'a göre en ucuz. Aday listelerine girmez (is_gyo hariç tutulur);
    bu, GYO'lar için TEK ayrı değerlendirme yeridir."""
    if df is None or df.empty or "GYO NAV/Hisse (Deneysel)" not in df.columns:
        return pd.DataFrame()
    d = df.copy(); d.attrs = {}
    d = d[pd.to_numeric(d["GYO NAV/Hisse (Deneysel)"], errors="coerce").notna()].copy()
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values("GYO NAV İskonto/Primi", ascending=True).reset_index(drop=True)
    if n:
        d = d.head(n)
    d.insert(0, "Sıra", np.arange(1, len(d) + 1))
    d.attrs["n_total"] = int(len(d))
    return d


GYO_SHEET_COLS = ["Sıra", "Hisse", "Fiyat", "GYO NAV/Hisse (Deneysel)", "GYO NAV İskonto/Primi",
                  "Defter Değeri/Hisse", "PD/DD", "Net Borç/FAVÖK", "Risk Sınıfı", "Likidite Notu"]


def build_rule_set_df():
    """Excel'e yazılan kural seti — v192: kodda GERÇEKTEN uygulanan Tier A/B eşiklerinden ve sert kapılardan türetilir
    (v191'de K3-K8, K20-K23 gibi kurallar sayfada vardı ama kodda uygulanmıyordu)."""
    rows = [
        ("H1", "Sert kapı", "Kova", "3 BEKLE değil (1 ÖNCELİK / 2 ŞARTLI)", "Kelly≈0 / edge yok → aday olmaz", "ELE"),
        ("H2", "Sert kapı", "Shannon Notu", "GÜRÜLTÜ değil", "Dağınık bileşen skoru elenir", "ELE"),
        ("H3", "Sert kapı", "Kelly Final", "> 0 (veri varsa)", "Edge yoksa aday olmaz", "ELE"),
        ("H4", "Sert kapı", "Ters Sinyal", "Evet değil", "v192'de ters sinyal cezası kapalı; açılırsa bu kapı devrede", "ELE"),
        ("H5", "Sert kapı", "Değerleme Sınıfı", "Pahalı / Çok Pahalı değil (marj ≥ –%10)", "Kendi modelinde adil değerinin üstünde fiyatlanan hisse değer adayı olmaz", "ELE"),
        ("H6", "Sert kapı", "Veri Tamlık", f">= {CANDIDATE_MIN_COMPLETENESS:.2f}", "Eksik veri 'geçti' sayılmaz", "ELE"),
        ("H8", "Sert kapı", "Fiyat kaynağı", f"İş Yatırım ↔ Yahoo fiyat farkı <= %{PRICE_MISMATCH_TOL*100:.0f}", "Kaynaklar ayrışıyorsa adil değer marjı güvenilmez (split/bedelsiz/eski fiyat)", "ELE"),
        ("T1", "Bilgi", "Teknik (Uyum Kadranı)", f"Eleme kapısı: {'AÇIK' if TECH_HARD_GATE else 'KAPALI'}", f"Yalnızca zamanlama bilgisi; Aday Skoru'nda ağırlık %{TECH_RANK_WEIGHT*100:.0f}", "—"),
        ("H7", "Sert kapı", "Likidite", f"Medyan günlük hacim (son {LIQ_WINDOW} gün) >= {LIQ_MIN_TURNOVER_TL/1e6:.0f} mn TL · işlemsiz gün <= %{LIQ_MAX_ZERO_DAYS*100:.0f}", "Alınıp satılamayan hisse aday olmaz; veri yoksa geçmez (--min-liq ile değişir)", "ELE"),
        ("M1", "Güvenlik marjı", "Adil Değer Marjı", f"Tier A ≥ %{MOS_TIER_A*100:.0f} · Tier B ≥ %{MOS_TIER_B*100:.0f} · Tier C ≥ %{MOS_TIER_C*100:.0f}", "Modelin kendi adil değerine göre iskonto yoksa değer adayı olmaz (NaN = doğrulanamadı = geçmez)", "ELE"),
        ("A1", "Tier A", "Güven / Risk", "Güven >= 55 · Risk 'Çok Yüksek' değil", "Veri kalitesi ve aşırı risk", "ELE"),
        ("A2", "Tier A", "Piotroski F / Kârlılık", ">= 5 / >= 0.32", "Kalite eşiği (NaN geçmez)", "KALITE"),
        ("A3", "Tier A", "ROIC", "> %2 (varsa)", "Sermaye getirisi", "KALITE"),
        ("A4", "Tier A", "FCF / OE Getirisi", "FCF > %2 veya (OE > %8 ve CapEx/Amort < 2)", "Nakit üretimi", "NAKİT"),
        ("A5", "Tier A", "CapEx/Amortisman · FCF", "< 4 · FCF getirisi < %40", "OE şişirme / anomali", "NAKİT"),
        ("A6", "Tier A", "F/K · PD/DD", "0 < F/K <= 15 · 0 < PD/DD <= 1.55 (varsa)", "Klasik Graham eşikleri", "DEĞER"),
        ("A7", "Tier A", "Cari · EV/EBIT", "0.8-30 · 0-80 (varsa)", "Likidite / anomali", "NAKİT"),
        ("A8", "Tier A", "DD Geçen · Graham oranı", ">= 3 · >= 0.55 (varsa)", "Due diligence ve Graham", "ELE"),
        ("A9", "Tier A", "Marj istikrarı · GYO/Yatırım ortaklığı", "Brüt>=%6 veya (Net>=%4 & FAVÖK>=%8) · GYO/yatırım ortaklığı değil", "BANVT tipi ince marj engeli", "ELE"),
        ("B1", "Tier B", "Tier A'dan gevşek", "Kârlılık>=0.30 · ROIC>0 · FCF>%1.5 · F/K<=16 · PD/DD<=1.6 · DD>=2", "A dolmazsa tamamlar", "ELE"),
        ("V0", "Değer Yatırımı", "Uygunluk", "Pozitif kâr/nakit · adil değer marjı ≥%10 · ROIC>0 · GYO-finans-ortaklık değil · FCF<%40 · CapEx/Amort<4 · likidite · fiyat tutarlı · risk≠Çok Yüksek", "Ayrı 'Değer Yatırımı' sayfası; Aday Listesi'nden bağımsız", "ELE"),
        ("V1", "Değer Yatırımı", "13 kriter", f"F/K≤12 · PD/DD≤1.5 · F/K×PD/DD≤22.5 · EV/EBIT≤10 · nakit≥%7 · Graham marjı≥%20 · adil değer marjı≥%25 · ROIC≥%8 · Piotroski≥6 · cari≥1.2 · ND/FAVÖK≤2.5 · kâr tutarlılığı≥0.5 · Altman riskli değil", f"'Tam' ≥ {VALUE_MIN_CRIT}/13; eksik veri geçmez", "SIRALA"),
        ("MF1", "Magic Formula", "Sıralama", "Kazanç getirisi (EBIT/EV) sırası + ROIC sırası toplamı en düşük 15", "Greenblatt; EV/EBIT>0, ROIC>0, EV/EBIT<40", "SIRALA"),
        ("MO1", "Momentum", "Sıralama", "6A/12A getiri yüzdelik sırası (vol ile hafif ayarlı); 'Çok Pahalı' hariç", "Değer filtresi yok; geçmiş performans, gelecek garantisi değil", "SIRALA"),
        ("D1", "Temettü", "Uygunluk", f"Temettü verimi≥%{DIVIDEND_MIN_YIELD*100:.0f} · son 8Ç'nin ≥{DIVIDEND_MIN_QUARTERS_PAID}'ünde ödeme · NI>0 · payout≤1.0", "Ödenen Temettüler (nakit akış) kaleminden; kâr payı oranı değil", "SIRALA"),
        ("LV1", "Düşük Volatilite", "Sıralama", "En düşük Vol Ann(σ); Piotroski≥4 · 'Çok Pahalı' hariç", "Düşük-volatilite anomalisi; değer taraması değil", "SIRALA"),
        ("SR1", "Sektöre Göre Ucuz", "Sıralama", f"Sektör içi F/K+PD/DD+EV/EBIT yüzdelik ortalaması en düşük; sektör≥{SECTOR_REL_MIN_MEMBERS} üye", "Mutlak eşik yerine göreli değerleme", "SIRALA"),
        ("HC1", "Tarihsel Ucuz", "Sıralama", "F/K ve PD/DD kendi ~3y tarihsel μ/σ z-skoru en düşük (en ucuz) 25 hisse; haftalık örneklem", "Yahoo fiyat + bugünkü EPS/BVPS ile yaklaşık tarihsel çarpan", "SIRALA"),
        ("CCC1", "Nakit Dönüşümü", "Bilgi", "Alacak+Stok gün − Borç ödeme gün (CCC)", "Kısalan CCC operasyonel iyileşme sinyali; sert kapı değildir, bilgi amaçlıdır", "BİLGİ"),
        ("MY1", "Marj Yönü", "Değer Puanı ayarı", "Brüt+FAVÖK marj trend skoru ortalaması ≥0.60 İyileşiyor / ≤0.40 Kötüleşiyor", "Değer Yatırımı Değer Puanı'na ±5 puan", "AYAR"),
        ("FX1", "Döviz Riski", "Değer Puanı ayarı", "Net YP Pozisyon/Özkaynak ≤ −%30 Yüksek Açık · ≥ +%10 Net Fazla", "Değer Yatırımı Değer Puanı'na −5/+2 puan; sert kapı değildir", "AYAR"),
        ("GYO1", "GYO NAV", "Ayrı sayfa", "NAV = Yatırım Amaçlı Gayrimenkul + Nakit − Toplam Yükümlülük", "DENEYSEL; Aday listelerinden bağımsız 'GYO NAV' sayfası", "BİLGİ"),
        ("CN1", "Konsensüs", "Ayrı sayfa", f"6 bağımsız listeden ≥{CONSENSUS_MIN_LISTS}'inde birlikte geçen hisseler", "Kendi eleme kriteri yok; alt listelerin kesişimi", "BİLGİ"),
        ("C1", "Tier C", "Tamamlama", "Tier A/B < 15 ise sert kapıları (H1-H6) geçen ve en çok kriteri karşılayanlarla 15'e tamamla", f"Tamamlama: {'AÇIK' if CANDIDATE_ALLOW_FILL else 'KAPALI'} · Sektör ≤3 · eksik kriter 'Kaçırılan Kriterler' kolonunda", "YAKIN"),
        ("L1", "Liste", "Sektör / adet", "Tier A/B: sektör başına ≤2 · toplam ≤15", "Sıra: Tier A > B > C", "—"),
    ]
    return pd.DataFrame(rows, columns=["Kod", "Grup", "Alan", "Kural", "Açıklama", "Sonuç"])


def _num_series(d, col):
    if col not in d.columns:
        return pd.Series(np.nan, index=d.index)
    return pd.to_numeric(d[col], errors="coerce")


def build_candidate_list(df, top_pct=0.40, min_confidence=55.0, max_cand=15):
    """Değer yatırımı aday listesi — Tier A (sıkı) + Tier B (şartlı) = max 15.

    Tier A: Piot≥5, ROIC>%2, FCF/OE+, CapEx<4, DD≥3, GYO yok, KAÇIN ele
    Tier B: biraz esnek (DD≥2, ROIC>0) — A dolmazsa tamamlar
    Sıra: ValueInvestScore; sektör başına en fazla 2 hisse
    """
    if df is None or df.empty:
        return pd.DataFrame()

    d = df.copy()
    n_universe = len(d)
    score_col = "Risk Ayarlı Skor" if "Risk Ayarlı Skor" in d.columns else "Final Score"
    if score_col not in d.columns:
        return pd.DataFrame()

    if "Ana Sıralamada" in d.columns:
        d = d[d["Ana Sıralamada"].astype(str).str.startswith("Evet")].copy()
    if d.empty:
        return pd.DataFrame()

    conf = _num_series(d, "Güven Skoru")
    score = _num_series(d, score_col)
    qual = _num_series(d, "Kârlılık Skoru")
    val_s = _num_series(d, "Değer Skoru")
    med_m = _num_series(d, "Adil Değer Marjı")
    fcf_y = _num_series(d, "FCF Getirisi")
    oe_y = _num_series(d, "Owner Earnings Getirisi")
    capex_da = _num_series(d, "CapEx/Amortisman")
    nn_pd = _num_series(d, "Net Nakit/Piyasa Değeri")
    nd_fcf = _num_series(d, "Net Borç/FCF")
    cari = _num_series(d, "Cari Oran")
    piot = _num_series(d, "Piotroski F")
    graham = _num_series(d, "Graham Kriter Geçiş Oranı")
    fk = _num_series(d, "F/K")
    pb = _num_series(d, "PD/DD")
    bilanco = _num_series(d, "Bilanço Skoru")
    sloan = _num_series(d, "Sloan Skoru")
    peer = _num_series(d, "Peer Skor")
    roe = _num_series(d, "ROE")
    roic = _num_series(d, "ROIC")
    roe_ke = _num_series(d, "ROE-ke Spread")
    tek = _num_series(d, "Teknik Skor")
    conf_orta = _num_series(d, "Confluence Skoru (Orta)")
    conf_uzun = _num_series(d, "Confluence Skoru (Uzun)")
    dd_gecen = _num_series(d, "DD Geçen")
    ev_ebit = _num_series(d, "EV/EBIT")
    brut_marj = _num_series(d, "Brüt Kar Marjı")
    if brut_marj.isna().all():
        brut_marj = _num_series(d, "Brüt Kâr Marjı")
    net_marj = _num_series(d, "Net Kar Marjı")
    if net_marj.isna().all():
        net_marj = _num_series(d, "Net Kâr Marjı")
    favok_marj = _num_series(d, "FAVÖK Marjı")
    # 8Ç kâr tutarlılığı (varsa)
    kar_tutar = _num_series(d, "Kâr Tutarlılığı")
    if kar_tutar.isna().all():
        kar_tutar = _num_series(d, "8Ç Kâr Tutarlılığı")
    if kar_tutar.isna().all():
        kar_tutar = _num_series(d, "Earnings Stability")

    risk_cls = d["Risk Sınıfı"].astype(str) if "Risk Sınıfı" in d.columns else pd.Series("", index=d.index)
    val_cls = d["Değerleme Sınıfı"].astype(str) if "Değerleme Sınıfı" in d.columns else pd.Series("", index=d.index)
    uyum = d["Uyum Kadranı"].astype(str) if "Uyum Kadranı" in d.columns else pd.Series("", index=d.index)
    st_dir = d["Supertrend Yön"].astype(str) if "Supertrend Yön" in d.columns else pd.Series("", index=d.index)
    tek_aks = d["Teknik Aksiyon"].astype(str) if "Teknik Aksiyon" in d.columns else pd.Series("", index=d.index)
    sektor = d["Sektör"].astype(str) if "Sektör" in d.columns else pd.Series("", index=d.index)
    # v192.4: NAV/portföy bazlı yapılar (GYO, yatırım ortaklıkları, girişim sermayesi): kazanç gerçeğe uygun değer
    # değişimlerinden gelir, DCF/RI/EPV anlamlı değil (ör. PRDGS +%158 marj ile listeye girmişti).
    is_gyo = sektor.str.contains("GYO|Gayrimenkul|Yatırım Ortaklık|Girişim Sermaye|Portföy Yönetim", case=False, na=False, regex=True)

    # ---------- ortak seriler / skor ----------
    mos = (med_m / 0.40).clip(0, 1).fillna(0)
    mos = np.maximum(mos, ((graham.fillna(0) - 0.5) / 0.5).clip(0, 1))
    cash_comp = (
        0.55 * ((fcf_y.fillna(0) - 0.02) / 0.10).clip(0, 1)
        + 0.45 * ((oe_y.fillna(0) - 0.03) / 0.12).clip(0, 1)
    )
    cash_comp = cash_comp * np.where(capex_da.fillna(1) >= 2.5, 0.75, 1.0)
    qual_comp = (
        0.40 * qual.fillna(0.40)
        + 0.40 * (piot.fillna(5) / 9.0).clip(0, 1)
        + 0.20 * ((roic.fillna(0).clip(0, 0.25)) / 0.15).clip(0, 1)
    )
    bal_comp = (
        0.60 * bilanco.fillna(0.50)
        + 0.40 * ((nn_pd.fillna(0) + 0.15) / 0.45).clip(0, 1)
    )
    cheap_comp = (
        0.50 * ((15 - fk.fillna(12).clip(0, 25)) / 15).clip(0, 1)
        + 0.50 * ((1.5 - pb.fillna(1.2).clip(0, 3)) / 1.5).clip(0, 1)
    )
    dd_s = (dd_gecen.fillna(2) / 5.0).clip(0, 1)
    tek_n = (tek.fillna(25) / 100.0).clip(0, 1)
    vis = 100.0 * (
        0.22 * mos + 0.22 * cash_comp + 0.20 * qual_comp
        + 0.12 * bal_comp + 0.14 * cheap_comp + 0.06 * dd_s + 0.04 * tek_n
    )
    vis = vis + np.where(dd_gecen.fillna(0) >= 3, 3.0, 0.0)
    vis = vis + np.where(dd_gecen.fillna(0) >= 4, 2.0, 0.0)
    vis = vis + np.where(piot.fillna(0) >= 7, 2.0, 0.0)
    d["ValueInvestScore"] = vis

    # Graham / Buffett etiket (bilgi)
    g_fk = (fk > 0) & (fk <= 15)
    g_pb = (pb > 0) & (pb <= 1.5)
    g_piot = piot >= 5
    g_cash = (fcf_y > 0.02) | (oe_y > 0.04)
    graham_hard = g_fk & g_pb & g_piot & g_cash & (qual >= 0.32)
    buffett_hard = (
        (roic > 0.02) & ((oe_y > 0.04) | (fcf_y > 0.03))
        & (capex_da.isna() | (capex_da < 3.5)) & (piot >= 5) & (qual >= 0.32)
    )
    d["GrahamHard"] = graham_hard.map({True: "GEÇTİ", False: "KALDI"})
    d["BuffettHard"] = buffett_hard.map({True: "GEÇTİ", False: "KALDI"})

    # Teknik AL skoru
    conf_n = (conf_orta.fillna(conf_uzun.fillna(0)) / 100.0).clip(0, 1)
    st_up = st_dir.str.contains("Yükseliş", case=False, na=False).astype(float)
    uyum_n = pd.Series(0.35, index=d.index)
    uyum_n = np.where(uyum.str.contains("GÜÇLÜ UYUM", na=False), 1.0, uyum_n)
    uyum_n = np.where(uyum.str.contains("ERKEN BİRİKİM", na=False), 0.70, uyum_n)
    uyum_n = np.where(uyum.str.contains("KAÇIN", na=False), 0.10, uyum_n)
    tek_al = 100 * (0.35 * tek_n + 0.25 * conf_n + 0.25 * st_up + 0.15 * pd.Series(uyum_n, index=d.index))
    d["TeknikAlSkoru"] = pd.Series(tek_al, index=d.index).clip(0, 100)
    d["Aday Skoru"] = (1.0 - TECH_RANK_WEIGHT) * d["ValueInvestScore"] + TECH_RANK_WEIGHT * d["TeknikAlSkoru"]

    # ---------- Marj istikrarı (BANVT tipi engel) ----------
    _brut = brut_marj
    _netm = net_marj
    _favm = favok_marj
    _tutar = kar_tutar
    # Brüt≥%6 VEYA (net≥%4 ve FAVÖK≥%8); veri yoksa fail
    _marj_gecer = (
        ((_brut >= 0.06) | ((_netm >= 0.04) & (_favm.isna() | (_favm >= 0.08))))
        & ~(_brut.isna() & _netm.isna())
    )
    _tutar_gecer = _tutar.isna() | (_tutar >= 0.40)
    _marj_istikrar = _marj_gecer & _tutar_gecer
    # v192.3: teknik "KAÇIN" artık varsayılan olarak eleme nedeni değil (TECH_HARD_GATE=False); yalnızca bilgi/sıralama.
    _kacin = uyum.str.contains("KAÇIN", na=False) if TECH_HARD_GATE else pd.Series(False, index=d.index)
    _pahali_kacin = val_cls.str.contains("Pahalı", na=False) & _kacin

    # v192 SERT KAPILAR — "Kural Seti" sayfasında yazıp kodda uygulanmayan kuralların (K16-K19 vb.) gerçek karşılığı.
    kova_s = d["Kova"].astype(str) if "Kova" in d.columns else pd.Series("", index=d.index)
    ters_s = d["Ters Sinyal"].astype(str) if "Ters Sinyal" in d.columns else pd.Series("", index=d.index)
    shn_s = d["Shannon Notu"].astype(str) if "Shannon Notu" in d.columns else pd.Series("", index=d.index)
    kelly_f = _num_series(d, "Kelly Final")
    tamlik = _num_series(d, "Veri Tamlık")
    # H7 Likidite: kolon varsa medyan hacim >= eşik ve işlemsiz gün oranı <= üst sınır; VERİ YOKSA GEÇMEZ.
    # (kolon hiç yoksa — ör. teknik katman kapalı — kapı uygulanmaz)
    if "Medyan Günlük Hacim (TL)" in d.columns:
        _liq_med = _num_series(d, "Medyan Günlük Hacim (TL)")
        _liq_zero = _num_series(d, "İşlemsiz Gün Oranı")
        liq_ok = (_liq_med >= LIQ_MIN_TURNOVER_TL) & (_liq_zero.isna() | (_liq_zero <= LIQ_MAX_ZERO_DAYS))
    else:
        liq_ok = pd.Series(True, index=d.index)
    fiyat_s = d["Fiyat Uyarısı"].astype(str) if "Fiyat Uyarısı" in d.columns else pd.Series("OK", index=d.index)
    _hard_gate = (
        (~fiyat_s.eq("Uyuşmazlık"))                                 # H8 fiyat kaynağı tutarlı
        & (~kova_s.str.contains("3 BEKLE", na=False))                 # K16
        & (~shn_s.str.contains("GÜRÜLTÜ", na=False))                # K17
        & (kelly_f.isna() | (kelly_f > 0))                          # K18
        & (~ters_s.str.strip().eq("Evet"))                          # K19
        & (~val_cls.isin(["Çok Pahalı", "Pahalı"]))                 # K13 (v192.4: 'Pahalı' da elenir; değer listesinde marjı negatif hisse olmaz)
        & (tamlik.isna() | (tamlik >= CANDIDATE_MIN_COMPLETENESS))  # eksik veri "geçti" sayılmasın
        & liq_ok                                                    # H7 likidite
    )
    d["Sert Kapı"] = np.where(_hard_gate, "GEÇTİ", "KALDI")
    _marj = _num_series(d, "Adil Değer Marjı")   # NaN → güvenlik marjı doğrulanamadı → tier'a girmez
    # Tier C (tamamlama) havuzu: yapısal dışlamalar da geçerli — GYO (NAV bazlı, DCF uygun değil),
    # uç değer nedeniyle bileşeni dışlanmış (veri şüpheli) ve marjı +%300 tavanına yapışmış hisseler.
    _mw = d["Model Uyarısı"].astype(str) if "Model Uyarısı" in d.columns else pd.Series("", index=d.index)
    _mg = _num_series(d, "Adil Değer Marjı")
    _fill_ok = _hard_gate & (_marj >= MOS_TIER_C) & (~is_gyo) & (~risk_cls.str.contains("Çok Yüksek", na=False))  # 2. tamamlama bloğu risk süzgecini atlıyordu & (~_mw.str.contains("Uç değer", na=False)) & (_mg.isna() | (_mg < FAIR_MARGIN_MAX - 1e-9))

    # ---------- Tier A (sıkı) ----------
    tier_a = (
        (conf.isna() | (conf >= float(min_confidence)))
        & (~risk_cls.str.contains("Çok Yüksek", na=False))
        & (piot >= 5)
        & (qual >= 0.32)
        & (roic.isna() | (roic > 0.02))
        & ((fcf_y > 0.02) | ((oe_y > 0.08) & (capex_da.isna() | (capex_da < 2.0))))
        & (capex_da.isna() | (capex_da < 4.0))
        & (fcf_y.isna() | (fcf_y < 0.40))
        & (fk.isna() | ((fk > 0) & (fk <= 15)))
        & (pb.isna() | ((pb > 0) & (pb <= 1.55)))
        & (cari.isna() | ((cari >= 0.8) & (cari < 30)))
        & (ev_ebit.isna() | ((ev_ebit > 0) & (ev_ebit < 80)))
        & (dd_gecen.isna() | (dd_gecen >= 3))
        & ~(_kacin & (score.fillna(0) < 58))
        & (~is_gyo)
        & (graham.isna() | (graham >= 0.55))
        & _marj_istikrar
        & (~_pahali_kacin)
        & _hard_gate
        & (_marj >= MOS_TIER_A)
    )

    # ---------- Tier B (şartlı) ----------
    tier_b = (
        (conf.isna() | (conf >= float(min_confidence)))
        & (~risk_cls.str.contains("Çok Yüksek", na=False))
        & (piot >= 5)
        & (qual >= 0.30)
        & (roic.isna() | (roic > 0.0))
        & ((fcf_y > 0.015) | ((oe_y > 0.06) & (capex_da.isna() | (capex_da < 2.5))))
        & (capex_da.isna() | (capex_da < 3.8))
        & (fcf_y.isna() | (fcf_y < 0.40))
        & (fk.isna() | ((fk > 0) & (fk <= 16)))
        & (pb.isna() | ((pb > 0) & (pb <= 1.60)))
        & (cari.isna() | ((cari >= 0.8) & (cari < 30)))
        & (ev_ebit.isna() | ((ev_ebit > 0) & (ev_ebit < 80)))
        & (dd_gecen.isna() | (dd_gecen >= 2))
        & ~(_kacin & (score.fillna(0) < 55))
        & (~is_gyo)
        & _marj_istikrar
        & (~_pahali_kacin)
        & _hard_gate
        & (_marj >= MOS_TIER_B)
    )

    d["Eleme Nedeni"] = ""
    d.loc[~tier_b, "Eleme Nedeni"] = "TierB-fail"
    d.loc[tier_b & ~tier_a, "Eleme Nedeni"] = ""  # B geçer
    d.loc[tier_a, "Eleme Nedeni"] = ""

    def _pick_mask(mask, n_need, exclude, max_per_sec=2):
        sub = d.loc[mask & ~d["Hisse"].astype(str).isin(exclude)].copy()
        sub = sub.sort_values(["ValueInvestScore", "Aday Skoru"], ascending=[False, False])
        out_idx = []
        sec_count = {}
        for idx, row in sub.iterrows():
            sec = str(row.get("Sektör") or "Diğer")[:28]
            if sec_count.get(sec, 0) >= max_per_sec:
                continue
            out_idx.append(idx)
            sec_count[sec] = sec_count.get(sec, 0) + 1
            if len(out_idx) >= n_need:
                break
        return out_idx, sec_count

    # --- Karşılama oranı: sabit N_CRITERIA_PASS = 15 kriter (X/15) ---
    n_crit = int(globals().get("N_CRITERIA_PASS", 15))
    crit_parts = {
        "1_Güven": (conf.isna() | (conf >= float(min_confidence))).astype(float),
        "2_Risk": (~risk_cls.str.contains("Çok Yüksek", na=False)).astype(float),
        "3_Piot≥5": (piot >= 5).astype(float),
        "4_Qual": (qual >= 0.30).astype(float),
        "5_ROIC>0": (roic.isna() | (roic > 0)).astype(float),
        "6_FCF_OE": ((fcf_y > 0.015) | (oe_y > 0.05)).astype(float),
        "7_CapEx<4": (capex_da.isna() | (capex_da < 4.0)).astype(float),
        "8_FK≤16": (fk.isna() | ((fk > 0) & (fk <= 16))).astype(float),
        "9_PDDD≤1.6": (pb.isna() | ((pb > 0) & (pb <= 1.6))).astype(float),
        "10_CariOK": (cari.isna() | ((cari >= 0.8) & (cari < 30))).astype(float),
        "11_EVOK": (ev_ebit.isna() | ((ev_ebit > 0) & (ev_ebit < 80))).astype(float),
        "12_DD≥2": (dd_gecen.isna() | (dd_gecen >= 2)).astype(float),
        "13_NotGYO": (~is_gyo).astype(float),
        "14_Marjİstikrar": _marj_istikrar.astype(float),
        "15_NotPahalıKAÇIN_SertKapı": ((~_pahali_kacin) & _hard_gate).astype(float),
    }
    if len(crit_parts) != n_crit:
        raise ValueError(f"Kriter sayısı {len(crit_parts)} != N_CRITERIA_PASS={n_crit}")
    crit_mat = pd.DataFrame(crit_parts, index=d.index)
    d["Karşılama N"] = crit_mat.sum(axis=1)                    # 0..15
    d["Karşılama Oranı"] = d["Karşılama N"] / float(n_crit)     # 0..1
    # v192: her hisse için kaçırılan kriterlerin adı (Tier C'de "yakın ama tam değil" nedeni görülsün)
    _names = list(crit_parts.keys())
    # 15 kriterin dışında Tier B'ye özgü elemeler (yoksa "15/15 ama Tier B değil" sorusu cevapsız kalır)
    _extra = {
        "FCF getirisi ≥%40 (anomali)": (fcf_y >= 0.40),
        "Nakit üretimi zayıf": ~((fcf_y > 0.015) | ((oe_y > 0.06) & (capex_da.isna() | (capex_da < 2.5)))),
        "CapEx/Amort ≥3.8": (capex_da >= 3.8),
        "Teknik KAÇIN + skor<55": (_kacin & (score.fillna(0) < 55)),
        f"Güvenlik marjı < %{MOS_TIER_B*100:.0f}": ~(_marj >= MOS_TIER_B),
    }
    _miss = []
    for i in d.index:
        lst = [n_.split("_", 1)[1] for n_ in _names if float(crit_mat.loc[i, n_]) < 0.5]
        lst += [k_ for k_, ser_ in _extra.items() if bool(ser_.loc[i]) and k_ not in lst]
        _miss.append(lst)
    d["Kaçırılan Kriterler"] = [", ".join(x) for x in _miss]
    d["Kaçırılan Sayısı"] = [len(x) for x in _miss]
    d["Karşılama"] = d["Karşılama N"].map(lambda x: f"{int(x)}/{n_crit}")

    # Eleme nedeni: en düşük karşılama bileşeni
    def _first_fail(row_idx):
        for name, ser in crit_parts.items():
            if float(ser.loc[row_idx]) < 0.5:
                return name
        return ""
    d["Eleme Nedeni"] = [_first_fail(i) for i in d.index]

    exclude = set()
    idx_a, _ = _pick_mask(tier_a, min(10, max_cand), exclude, max_per_sec=2)
    exclude.update(d.loc[idx_a, "Hisse"].astype(str).tolist())
    need_b = max_cand - len(idx_a)
    idx_b, _ = _pick_mask(tier_b, need_b, exclude, max_per_sec=2)

    selected_idx = list(idx_a) + list(idx_b)
    fallback_used = False
    n_fill = 0

    # --- v192: kriter geçmeyenlerle 15'e TAMAMLAMA varsayılan olarak KAPALI (CANDIDATE_ALLOW_FILL) ---
    if CANDIDATE_ALLOW_FILL and len(selected_idx) < max_cand:
        fallback_used = True
        have = set(d.loc[selected_idx, "Hisse"].astype(str).tolist()) if selected_idx else set()
        rest = d.loc[(~d["Hisse"].astype(str).isin(have)) & _fill_ok].copy()   # sert kapı + yapısal dışlamalar zorunlu
        if CANDIDATE_FILL_MAX_MISS is not None:
            rest = rest[rest["Kaçırılan Sayısı"] <= CANDIDATE_FILL_MAX_MISS]
        rest = rest.sort_values(
            ["Kaçırılan Sayısı", "ValueInvestScore", "Aday Skoru"],
            ascending=[True, False, False],
        )
        sec_count = {}
        if selected_idx and "Sektör" in d.columns:
            for idx in selected_idx:
                sec = str(d.loc[idx].get("Sektör") or "Diğer")[:28]
                sec_count[sec] = sec_count.get(sec, 0) + 1
        for idx, row in rest.iterrows():
            if len(selected_idx) >= max_cand:
                break
            # aşırı bozukları yine de ele ( CapEx≥8, FCF≥%50, Risk Çok Yüksek )
            if str(row.get("Risk Sınıfı") or "").find("Çok Yüksek") >= 0:
                continue
            # Pahalı + KAÇIN tamamlamaada da ele
            if "Pahalı" in str(row.get("Değerleme Sınıfı") or "") and "KAÇIN" in str(row.get("Uyum Kadranı") or ""):
                continue
            cx = row.get("CapEx/Amortisman")
            fy = row.get("FCF Getirisi")
            bm = row.get("Brüt Kar Marjı", row.get("Brüt Kâr Marjı"))
            nm = row.get("Net Kar Marjı", row.get("Net Kâr Marjı"))
            try:
                if cx is not None and float(cx) >= 8.0:
                    continue
                if fy is not None and float(fy) >= 0.50:
                    continue
                # ince marj (BANVT tipi) — tamamlamaada da ele
                if bm is not None and nm is not None and float(bm) < 0.06 and float(nm) < 0.04:
                    continue
            except Exception:
                pass
            sec = str(row.get("Sektör") or "Diğer")[:28]
            # doldururken sektör 3'e kadar izin
            if sec_count.get(sec, 0) >= 3:
                continue
            selected_idx.append(idx)
            sec_count[sec] = sec_count.get(sec, 0) + 1
            n_fill += 1
            d.loc[idx, "Aday Modu"] = "Tier C (Kriterlere Yakın)"

    # hâlâ eksikse sektör limitini kaldır
    if CANDIDATE_ALLOW_FILL and len(selected_idx) < max_cand:
        have = set(d.loc[selected_idx, "Hisse"].astype(str).tolist()) if selected_idx else set()
        rest = d.loc[(~d["Hisse"].astype(str).isin(have)) & _fill_ok].sort_values(
            ["Kaçırılan Sayısı", "ValueInvestScore"], ascending=[True, False]
        )
        if CANDIDATE_FILL_MAX_MISS is not None:
            rest = rest[rest["Kaçırılan Sayısı"] <= CANDIDATE_FILL_MAX_MISS]
        for idx in rest.index:
            if len(selected_idx) >= max_cand:
                break
            selected_idx.append(idx)
            n_fill += 1
            d.loc[idx, "Aday Modu"] = "Tier C (Kriterlere Yakın)"
        fallback_used = True

    if selected_idx:
        d.loc[idx_a, "Aday Modu"] = "Tier A (Sıkı Değer)"
        d.loc[idx_b, "Aday Modu"] = "Tier B (Şartlı)"
        # tamamlama satırları zaten yazıldı; A/B üzerine yazmasın
        for idx in selected_idx:
            if idx not in idx_a and idx not in idx_b:
                if str(d.loc[idx].get("Aday Modu") or "") == "":
                    d.loc[idx, "Aday Modu"] = "Tier C (Kriterlere Yakın)"

    if not selected_idx:
        out = pd.DataFrame()
        out.attrs.update({
            "n_universe": n_universe, "n_candidates": 0, "fallback_used": True,
            "mode": "tier_ab_value", "min_confidence": min_confidence, "max_candidates": max_cand,
        })
        return out

    d_sel = d.loc[selected_idx].copy()
    # v192: önce Tier A → B → C, tier içinde ValueInvestScore
    _tier_rank = d_sel["Aday Modu"].astype(str).map(lambda x: 0 if x.startswith("Tier A") else 1 if x.startswith("Tier B") else 2)
    d_sel = d_sel.assign(_tr=_tier_rank).sort_values(
        ["_tr", "Kaçırılan Sayısı", "ValueInvestScore"], ascending=[True, True, False]).drop(columns="_tr")

    # Kova / sınıf
    classes, kovas, aksiyonlar = [], [], []
    for _, row in d_sel.iterrows():
        mode = str(row.get("Aday Modu") or "")
        vis_i = float(row.get("ValueInvestScore") or 0)
        ta_i = float(row.get("TeknikAlSkoru") or 0)
        dd_i = row.get("DD Geçen")
        st = str(row.get("Supertrend Yön") or "")
        if mode.startswith("Tier A") and vis_i >= 70 and ta_i >= 40 and "Yükseliş" in st:
            classes.append("GÜÇLÜ ADAY")
            kovas.append("1 ÖNCELİK")
            aksiyonlar.append("Tier A + teknik teyit — dipnot/KAP, boyut sınırlı")
        elif mode.startswith("Tier A"):
            classes.append("GÜÇLÜ ADAY")
            kovas.append("2 ŞARTLI")
            aksiyonlar.append("Tier A değer — teknik zayıfsa kademeli / bekle")
        elif vis_i >= 65 and (pd.notna(dd_i) and dd_i >= 3):
            classes.append("İZLEME")
            kovas.append("2 ŞARTLI")
            aksiyonlar.append("Tier B — DD ve nakit dipnotunu kapat")
        else:
            classes.append("İZLEME")
            kovas.append("2 ŞARTLI")
            aksiyonlar.append("Tier B şartlı — kalite/DD teyidi şart")
    d_sel["Kural Sınıfı"] = classes
    d_sel["Kova"] = kovas
    d_sel["Kova Aksiyon"] = aksiyonlar

    flags, reasons = [], []
    for _, row in d_sel.iterrows():
        f = []
        if str(row.get("GrahamHard")) == "GEÇTİ":
            f.append("Graham")
        if str(row.get("BuffettHard")) == "GEÇTİ":
            f.append("Buffett")
        if float(row.get("TeknikAlSkoru") or 0) >= 45:
            f.append("TeknikAL")
        if pd.notna(row.get("FCF Getirisi")) and row["FCF Getirisi"] >= 0.05:
            f.append("FCF+")
        if pd.notna(row.get("Piotroski F")) and row["Piotroski F"] >= 7:
            f.append("Piot≥7")
        if pd.notna(row.get("Net Nakit/Piyasa Değeri")) and row["Net Nakit/Piyasa Değeri"] > 0:
            f.append("NetNakit")
        flags.append(", ".join(f) if f else "—")
        reasons.append(
            f"{row.get('Aday Modu')} · VIS={float(row.get('ValueInvestScore') or 0):.0f} "
            f"TekAL={float(row.get('TeknikAlSkoru') or 0):.0f}"
        )
    d_sel["Katalizörler"] = flags
    d_sel["Kural Gerekçesi"] = reasons
    # Karşılama Oranı / Karşılama N / Karşılama (X/15) zaten d üzerinden gelir

    if "Sektör" in d_sel.columns:
        d_sel["Sektör İçi Sıra"] = d_sel.groupby("Sektör")["ValueInvestScore"].rank(
            ascending=False, method="min"
        )
        d_sel["Sektör İçi Adet"] = d_sel.groupby("Sektör")["ValueInvestScore"].transform("count")
    else:
        d_sel["Sektör İçi Sıra"] = 1
        d_sel["Sektör İçi Adet"] = 1

    prefer = [
        "Hisse", "Sektör", "Kural Sınıfı", "Kova", "Kova Aksiyon", "Aday Modu",
        "Aday Skoru", "ValueInvestScore", "GrahamBuffettScore", "TeknikAlSkoru",
        "GrahamHard", "BuffettHard",
        "Final Score", "Adjusted Score", "Bütünleşik Skor", "Bütünleşik Not",
        "Güven Skoru", "Risk Sınıfı", "Değerleme Sınıfı",
        "Adil Değer Marjı", "F/K", "PD/DD", "EV/EBIT",
        "FCF Getirisi", "Owner Earnings Getirisi", "CapEx/Amortisman",
        "Net Nakit/Piyasa Değeri", "Net Borç/FCF", "Cari Oran",
        "Piotroski F", "Graham Kriter Geçiş Oranı", "Değer Yatırımcılığı Sonucu",
        "ROE", "ROIC", "ROE-ke Spread",
        "Değer Skoru", "Kârlılık Skoru", "Bilanço Skoru", "Nakit Akışı Skoru", "Peer Skor",
        "Teknik Skor", "Teknik Aksiyon", "Uyum Kadranı", "Supertrend Yön",
        "RSI(14)", "Confluence Skoru (Orta)",
        "Shannon Notu", "Kelly Final", "Önerilen Pay %", "Stop Loss %", "Ters Sinyal",
        "Likidite Notu", "Medyan Günlük Hacim (TL)", "1 Gün Tahliye Kapasitesi (TL)", "İşlemsiz Gün Oranı", "Yıllık Devir Oranı",
        "Fiyat Uyarısı", "Fiyat Farkı %",
        "DD Aksiyon", "DD Özet", "DD Geçen", "DD Kalan",
        "DD1 Nakit", "DD2 Borç", "DD3 Kalite", "DD4 İş Modeli", "DD5 Fiyat",
        "Değerlendirme", "Yatırım Tezi", "Ana Risk", "Güçlü Yanlar", "Zayıf Yanlar",
        "Katalizörler", "Kural Gerekçesi", "Karşılama", "Karşılama N", "Karşılama Oranı", "Kaçırılan Sayısı", "Kaçırılan Kriterler", "Eleme Nedeni",
        "Sektör İçi Sıra", "Sektör İçi Adet",
        "Adil Değer (EPV)", "EPV Marjı", "EV/EBIT Band Skoru",
        "F/K (Forward)", "PD/DD (Forward)",
        "Rejim Notu", "Kapsam Notu",
    ]
    # GrahamBuffettScore yoksa üret
    if "GrahamBuffettScore" not in d_sel.columns:
        d_sel["GrahamBuffettScore"] = (
            0.5 * d_sel["ValueInvestScore"].fillna(0)
            + 50 * graham_hard.reindex(d_sel.index).astype(float)
        )

    cols = [c for c in prefer if c in d_sel.columns]
    out = d_sel[cols].head(int(max_cand)).copy()
    out.insert(0, "Aday Sıra", range(1, len(out) + 1))

    out.attrs.update({
        "threshold": float(score.quantile(1.0 - top_pct)) if score.notna().any() else None,
        "top_pct": top_pct,
        "n_universe": n_universe,
        "n_candidates": len(out),
        "min_confidence": min_confidence,
        "max_candidates": max_cand,
        "fallback_used": fallback_used,
        "mode": "tier_ab_value",
        "n_tier_a": int(len(idx_a)),
        "n_tier_b": int(len(idx_b)),
        "n_fill": int(n_fill),
        "always_15": True,
    })
    return out



# ---- Klasik Değer Yatırımı (Graham–Buffett) listesi — v192.8 ----
VALUE_LIST_N = 15
VALUE_MIN_CRIT = 9          # 13 kriterden en az bu kadarını geçen = "Tam" eşleşme; geri kalanlar "Kısmi"
VALUE_MAX_PER_SECTOR = 2    # tamamlamada 3
VALUE_MIN_MARGIN = 0.10     # uygunluk: modelin adil değer marjı en az bu kadar (marjı olmayan hisse "değer" listesine giremez)
VALUE_CRITERIA_LABELS = [
    "F/K≤12", "PD/DD≤1.5", "F/K×PD/DD≤22.5", "EV/EBIT≤10", "Nakit getirisi≥%7", "Graham güv.marjı≥%20",
    "Adil değer marjı≥%25", "ROIC≥%8", "Piotroski≥6", "Cari≥1.2", "NetBorç/FAVÖK≤2.5", "Kâr tutarlılığı≥0.5", "Altman riskli değil",
]


def build_value_investing_list(df, n=None, min_confidence=55.0):
    """Klasik değer yatırımcılığı seçimi (Graham savunmacı kriterleri + Buffett kalite/sahip kazancı).

    Aday Listesi'nden BAĞIMSIZDIR: teknik, Kelly, Kova ve tier mantığı kullanılmaz. Yalnızca
    (1) veri bütünlüğü/işlem edilebilirlik elemesi, (2) 13 değer kriteri, (3) bileşik Değer Puanı.
    NaN = kriter doğrulanamadı = geçmez."""
    n = VALUE_LIST_N if n is None else n
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d.attrs = {}   # out.attrs içinde DataFrame'ler var (backtest_summary...); pd.concat attrs karşılaştırırken patlar
    if "Ana Sıralamada" in d.columns:
        d = d[d["Ana Sıralamada"].astype(str).str.startswith("Evet")].copy()
    if d.empty:
        return pd.DataFrame()
    d = d.reset_index(drop=True)
    N = lambda c: _num_series(d, c)
    fk, pb, ev_ebit = N("F/K"), N("PD/DD"), N("EV/EBIT")
    fcf_y, oe_y = N("FCF Getirisi"), N("Owner Earnings Getirisi")
    gm, med_m = N("Graham Güvenlik Marjı"), N("Adil Değer Marjı")
    roic, roe, piot = N("ROIC"), N("ROE"), N("Piotroski F")
    cari, nd_fav = N("Cari Oran"), N("Net Borç/FAVÖK")
    kt = N("Net Kâr Trend Tutarlılığı (8Ç)")
    capex_da = N("CapEx/Amortisman")
    conf, tamlik = N("Güven Skoru"), N("Veri Tamlık")
    S = lambda c, default="": d[c].astype(str) if c in d.columns else pd.Series(default, index=d.index)
    sektor, risk_cls, bolge = S("Sektör"), S("Risk Sınıfı"), S("Altman Bölge")
    mw, fiyat_u = S("Model Uyarısı"), S("Fiyat Uyarısı", "OK")

    # ---------- 1) Uygunluk (veri bütünlüğü + işlem edilebilirlik; görüş değil) ----------
    ortak = sektor.str.contains("GYO|Gayrimenkul|Yatırım Ortaklık|Girişim Sermaye|Portföy Yönetim|Aracı Kurum|Faktoring|Sigorta|Banka|Finansal Kiralama",
                                case=False, na=False, regex=True)
    if "Medyan Günlük Hacim (TL)" in d.columns:
        lz = N("İşlemsiz Gün Oranı")
        liq_ok = (N("Medyan Günlük Hacim (TL)") >= LIQ_MIN_TURNOVER_TL) & (lz.isna() | (lz <= LIQ_MAX_ZERO_DAYS))
    else:
        liq_ok = pd.Series(True, index=d.index)
    elig = (
        (fk > 0) & (pb > 0) & ((fcf_y > 0) | (oe_y > 0))                 # Graham: pozitif kâr, pozitif nakit
        & (fcf_y.isna() | (fcf_y < 0.40)) & (capex_da.isna() | (capex_da < 4.0))   # anomali (tek seferlik nakit/yatırım şişmesi) koruması
        & (med_m >= VALUE_MIN_MARGIN)                                    # güvenlik marjı zorunlu (NaN geçmez)
        & (roic > 0)                                                     # Buffett: sermaye getirisi pozitif
        & (conf >= float(min_confidence))
        & (tamlik.isna() | (tamlik >= CANDIDATE_MIN_COMPLETENESS))
        & (~ortak) & liq_ok
        & (~fiyat_u.eq("Uyuşmazlık"))
        & (~risk_cls.str.contains("Çok Yüksek", na=False))
        & (~mw.str.contains("Uç değer|Hasılat ≤ 0", na=False, regex=True))
    )

    # ---------- 2) Kriter matrisi (NaN geçmez) ----------
    crit = pd.DataFrame({
        VALUE_CRITERIA_LABELS[0]: (fk > 0) & (fk <= 12),
        VALUE_CRITERIA_LABELS[1]: (pb > 0) & (pb <= 1.5),
        VALUE_CRITERIA_LABELS[2]: (fk > 0) & (pb > 0) & (fk * pb <= 22.5),
        VALUE_CRITERIA_LABELS[3]: (ev_ebit > 0) & (ev_ebit <= 10),
        VALUE_CRITERIA_LABELS[4]: (fcf_y >= 0.07) | (oe_y >= 0.07),
        VALUE_CRITERIA_LABELS[5]: gm >= 0.20,
        VALUE_CRITERIA_LABELS[6]: med_m >= 0.25,
        VALUE_CRITERIA_LABELS[7]: roic >= 0.08,
        VALUE_CRITERIA_LABELS[8]: piot >= 6,
        VALUE_CRITERIA_LABELS[9]: cari >= 1.2,
        VALUE_CRITERIA_LABELS[10]: nd_fav <= 2.5,
        VALUE_CRITERIA_LABELS[11]: kt >= 0.5,
        VALUE_CRITERIA_LABELS[12]: ~bolge.str.contains("Riskli", na=False),
    }, index=d.index).fillna(False).astype(bool)
    d["Kriter N"] = crit.sum(axis=1).astype(int)
    d["Kriter Karşılama"] = d["Kriter N"].astype(str) + "/" + str(len(VALUE_CRITERIA_LABELS))
    d["Kaçırılan Kriterler"] = [", ".join(c for c in crit.columns if not crit.loc[i, c]) for i in d.index]

    # ---------- 3) Değer Puanı (0-100) ----------
    clip01 = lambda s: s.clip(0, 1)
    ey = clip01((1.0 / fk.where(fk > 0)).fillna(0) / 0.25)
    pbc = clip01((1.6 - pb.where(pb > 0)) / 1.6).fillna(0)
    evc = clip01((12 - ev_ebit.where(ev_ebit > 0)) / 12).fillna(0)
    cheap = 0.45 * ey + 0.30 * pbc + 0.25 * evc
    safety = clip01(pd.concat([med_m, gm], axis=1).max(axis=1).fillna(0) / 0.60)    # %60 üstü marj ek puan getirmez (uç değer koruması)
    qual = (0.35 * clip01(roic.fillna(0) / 0.15) + 0.25 * clip01(roe.fillna(0) / 0.20)
            + 0.25 * clip01(piot.fillna(0) / 9.0) + 0.15 * clip01(kt.fillna(0) / 0.7))
    cash = 0.5 * clip01((fcf_y.fillna(0) - 0.02) / 0.10) + 0.5 * clip01((oe_y.fillna(0) - 0.02) / 0.10)
    alt = np.where(bolge.str.contains("Güvenli", na=False), 1.0, np.where(bolge.str.contains("Gri", na=False), 0.5, 0.0))
    bal = 0.4 * clip01((3 - nd_fav.fillna(3)) / 4) + 0.3 * clip01((cari.fillna(0.8) - 0.8) / 1.7) + 0.3 * pd.Series(alt, index=d.index)
    base_score = 100 * (0.30 * cheap + 0.25 * safety + 0.25 * qual + 0.10 * cash + 0.10 * bal)
    # v192.10: marj yönü ve döviz riski küçük bir ayar olarak eklenir (ana bileşenleri ezmesin diye ±5 puanla sınırlı)
    myon = S("Marj Yönü") if "Marj Yönü" in d.columns else pd.Series("", index=d.index)
    doviz = S("Döviz Riski") if "Döviz Riski" in d.columns else pd.Series("", index=d.index)
    adj = np.where(myon == "İyileşiyor", 5, np.where(myon == "Kötüleşiyor", -5, 0)).astype(float)
    adj += np.where(doviz == "Yüksek Açık Riski", -5, np.where(doviz == "Net Fazla (Doğal Hedge)", 2, 0)).astype(float)
    d["Değer Puanı"] = (base_score + adj).clip(0, 100).round(1)

    # ---------- 4) Seçim ----------
    d["Eşleşme"] = np.where(d["Kriter N"] >= VALUE_MIN_CRIT, "Tam", "Kısmi")
    pool = d[elig].copy()
    if pool.empty:
        return pd.DataFrame()
    tam = pool[pool["Eşleşme"] == "Tam"].sort_values("Değer Puanı", ascending=False)
    kis = pool[pool["Eşleşme"] == "Kısmi"].sort_values(["Kriter N", "Değer Puanı"], ascending=[False, False])
    ordered = pd.concat([tam, kis])
    picked, sec_cnt = [], {}
    for cap in (VALUE_MAX_PER_SECTOR, VALUE_MAX_PER_SECTOR + 1):
        for idx, row in ordered.iterrows():
            if len(picked) >= n:
                break
            if idx in picked:
                continue
            sec = str(row.get("Sektör") or "Diğer")[:28]
            if sec_cnt.get(sec, 0) >= cap:
                continue
            picked.append(idx)
            sec_cnt[sec] = sec_cnt.get(sec, 0) + 1
        if len(picked) >= n:
            break
    out = d.loc[picked].copy().reset_index(drop=True)
    out.insert(0, "Sıra", np.arange(1, len(out) + 1))
    out.attrs.update({"n_universe": int(len(d)), "n_eligible": int(elig.sum()), "n_tam": int((pool["Eşleşme"] == "Tam").sum()),
                      "n_selected_tam": int((out["Eşleşme"] == "Tam").sum()), "min_crit": VALUE_MIN_CRIT})
    return out


VALUE_SHEET_COLS = [
    "Sıra", "Hisse", "Sektör", "Eşleşme", "Kriter Karşılama", "Değer Puanı", "Özet", "Fiyat", "Adil Değer (Medyan)",
    "Adil Değer Marjı", "Adil Değer (Graham)", "Graham Güvenlik Marjı", "F/K", "PD/DD", "EV/EBIT", "FCF Getirisi",
    "Owner Earnings Getirisi", "ROIC", "ROE", "Piotroski F", "Cari Oran", "Net Borç/FAVÖK", "Altman Bölge",
    "Net Kâr Trend Tutarlılığı (8Ç)", "Marj Yönü", "Nakit Dönüşüm Döngüsü (Gün)", "Döviz Riski",
    "Risk Sınıfı", "Likidite Notu", "Kaçırılan Kriterler", "Nihai Sinyal", "TV Görünüm",
]


def value_list_for_sheet(v):
    """Excel'e yazılacak sütun düzeni + kısa gerekçe metni."""
    if v is None or v.empty:
        return v
    v = v.copy()
    def _pct(x):
        return "n/a" if pd.isna(x) else f"%{x*100:.0f}"
    v["Özet"] = [
        f"{r['Kriter Karşılama']} kriter · marj {_pct(r.get('Adil Değer Marjı'))} · F/K {r.get('F/K'):.1f} · PD/DD {r.get('PD/DD'):.2f} · ROIC {_pct(r.get('ROIC'))}"
        + (" · ⚠ marj >%150: veriyi doğrulayın" if pd.notna(r.get('Adil Değer Marjı')) and r.get('Adil Değer Marjı') > 1.5 else "")
        for _, r in v.iterrows()
    ]
    cols = [c for c in VALUE_SHEET_COLS if c in v.columns]
    return v[cols]


def _style_value_sheet(wb):
    if "Değer Yatırımı" not in wb.sheetnames:
        return
    ws = wb["Değer Yatırımı"]
    if ws.max_row <= 2 and ws.max_column <= 2:
        ws["A1"].font = Font(bold=True, color="C00000")
        return
    ws.insert_rows(1, 4)
    ws["A1"] = "DEĞER YATIRIMI — 15 HİSSE (GRAHAM SAVUNMACI + BUFFETT KALİTE)"
    ws["A1"].font = Font(bold=True, size=14, color="1F4E79")
    ws["A2"] = ("13 kriter: F/K≤12 · PD/DD≤1.5 · F/K×PD/DD≤22.5 · EV/EBIT≤10 · nakit getirisi≥%7 · Graham güvenlik marjı≥%20 · "
                "adil değer marjı≥%25 · ROIC≥%8 · Piotroski≥6 · cari≥1.2 · NetBorç/FAVÖK≤2.5 · kâr tutarlılığı≥0.5 · Altman riskli değil")
    ws["A3"] = (f"'Tam' = 13 kriterin en az {VALUE_MIN_CRIT}'unu geçer (yeşil); 'Kısmi' = liste 15'e tamamlanırken en çok kriteri geçenler (sarı). "
                "Eksik veri kriteri geçmez. Sektör başına ≤2 (tamamlamada ≤3). Sıra = Değer Puanı (ucuzluk %30 · güvenlik marjı %25 · kalite %25 · nakit %10 · bilanço %10 "
                "± marj yönü/döviz riski ayarı, en fazla ±5+2 puan).")
    ws["A4"] = ("Aday Listesi'nden BAĞIMSIZDIR: teknik zamanlama, Kelly, Kova ve tier kullanılmaz. Uygunluk: GYO/finans/ortaklık hariç, pozitif kâr ve nakit, "
                "FCF getirisi <%40 ve CapEx/Amort <4 (anomali), adil değer marjı ≥%10, ROIC >0, likidite, fiyat tutarlılığı, risk 'Çok Yüksek' değil. Yatırım tavsiyesi değildir; "
                "'Nihai Sinyal' ve 'TV Görünüm' yalnızca zamanlama bilgisidir.")
    for r in range(1, 5):
        ws.cell(r, 1).alignment = Alignment(wrap_text=True, vertical="top")
    try:
        for r in (2, 3, 4):
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
            ws.row_dimensions[r].height = 48
    except Exception:
        pass
    hdr = [str(c.value) for c in ws[5]]
    for cell in ws[5]:
        if cell.value:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
    fills = {"Tam": PatternFill("solid", fgColor="C6EFCE"), "Kısmi": PatternFill("solid", fgColor="FFF2CC")}
    ei = hdr.index("Eşleşme") + 1 if "Eşleşme" in hdr else None
    pct_cols = {"Adil Değer Marjı", "Graham Güvenlik Marjı", "FCF Getirisi", "Owner Earnings Getirisi", "ROIC", "ROE", "Net Kâr Trend Tutarlılığı (8Ç)"}
    dec_cols = {"F/K", "PD/DD", "EV/EBIT", "Cari Oran", "Net Borç/FAVÖK", "Fiyat", "Adil Değer (Medyan)", "Adil Değer (Graham)", "Değer Puanı"}
    for r in range(6, ws.max_row + 1):
        k = str(ws.cell(r, ei).value or "") if ei else ""
        for c in range(1, min(7, ws.max_column + 1)):
            if k in fills:
                ws.cell(r, c).fill = fills[k]
        for ci, name in enumerate(hdr, start=1):
            if name in pct_cols:
                ws.cell(r, ci).number_format = "0.0%"
            elif name in dec_cols:
                ws.cell(r, ci).number_format = "0.00"
    widths = {"Sıra": 6, "Hisse": 10, "Sektör": 24, "Eşleşme": 10, "Kriter Karşılama": 11, "Değer Puanı": 10, "Özet": 60, "Kaçırılan Kriterler": 42,
              "Nihai Sinyal": 34, "Risk Sınıfı": 11, "Likidite Notu": 12}
    for ci, name in enumerate(hdr, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = widths.get(name, 13)
    ws.freeze_panes = "C6"


# =========================================================================================
# v192.9 — Ek seçim teknikleri: Magic Formula, Momentum, Temettü, Düşük Volatilite, Sektöre Göre Ucuzluk
# Hepsi aynı temel uygunluk süzgecini paylaşır; Değer Yatırımı sayfasından BAĞIMSIZDIR (farklı felsefe/kriter).
# =========================================================================================

def _base_eligibility(d, exclude_financials=True, require_liquidity=True, require_price_ok=True, exclude_high_risk=True):
    """Tüm ek seçim listelerinin ortak "işlem edilebilirlik" süzgeci (görüş/strateji değil, veri/likidite hijyeni).
    Döner: bool Series."""
    N = lambda c: _num_series(d, c)
    S = lambda c, default="": d[c].astype(str) if c in d.columns else pd.Series(default, index=d.index)
    elig = pd.Series(True, index=d.index)
    if exclude_financials:
        sektor = S("Sektör")
        elig &= ~sektor.str.contains("GYO|Gayrimenkul|Yatırım Ortaklık|Girişim Sermaye|Portföy Yönetim|Aracı Kurum|Faktoring|Sigorta|Banka|Finansal Kiralama",
                                     case=False, na=False, regex=True)
    if require_liquidity and "Medyan Günlük Hacim (TL)" in d.columns:
        lz = N("İşlemsiz Gün Oranı")
        elig &= (N("Medyan Günlük Hacim (TL)") >= LIQ_MIN_TURNOVER_TL) & (lz.isna() | (lz <= LIQ_MAX_ZERO_DAYS))
    if require_price_ok:
        elig &= (~S("Fiyat Uyarısı", "OK").eq("Uyuşmazlık"))
    if exclude_high_risk:
        elig &= (~S("Risk Sınıfı").str.contains("Çok Yüksek", na=False))
    conf = N("Güven Skoru")
    elig &= (conf.isna() | (conf >= 50.0))
    return elig


def _sector_cap_pick(pool, score_col, n, max_per_sector=2, max_per_sector_fill=3, ascending=False):
    """Skora göre sırala, sektör başına üst sınırla seç; yetmezse sınırı 1 artırıp tamamla."""
    ordered = pool.sort_values(score_col, ascending=ascending)
    picked, sec_cnt = [], {}
    for cap in (max_per_sector, max_per_sector_fill):
        for idx, row in ordered.iterrows():
            if len(picked) >= n or idx in picked:
                continue
            sec = str(row.get("Sektör") or "Diğer")[:28]
            if sec_cnt.get(sec, 0) >= cap:
                continue
            picked.append(idx); sec_cnt[sec] = sec_cnt.get(sec, 0) + 1
        if len(picked) >= n:
            break
    return pool.loc[picked]


# ---------------- Magic Formula (Greenblatt) ----------------
MAGIC_LIST_N = 15


def build_magic_formula_list(df, n=None):
    """Greenblatt Magic Formula: kazanç getirisi (EBIT/EV) ve sermaye getirisi (ROIC/ROIC benzeri) sıralamalarının
    toplamı en iyi olan hisseler. Orijinal formül EV/EBIT ve ön-vergi ROC kullanır; burada elimizdeki EV/EBIT ve
    ROIC ile yaklaşık uygulanır (ROIC vergi sonrası; EBIT/EV vergi öncesi kazanç getirisidir — küçük bir tutarsızlık
    literatürde de kabul edilir)."""
    n = MAGIC_LIST_N if n is None else n
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy(); d.attrs = {}
    if "Ana Sıralamada" in d.columns:
        d = d[d["Ana Sıralamada"].astype(str).str.startswith("Evet")].copy()
    d = d.reset_index(drop=True)
    N = lambda c: _num_series(d, c)
    ev_ebit, roic = N("EV/EBIT"), N("ROIC")
    earn_yield = (1.0 / ev_ebit).where(ev_ebit > 0)
    elig = _base_eligibility(d) & (ev_ebit > 0) & (roic > 0) & (ev_ebit < 40)
    pool = d[elig].copy()
    if pool.empty:
        return pd.DataFrame()
    ey_r = earn_yield.loc[pool.index].rank(ascending=False, method="min")
    roic_r = roic.loc[pool.index].rank(ascending=False, method="min")
    pool["Kazanç Getirisi (EBIT/EV)"] = earn_yield.loc[pool.index]
    pool["Sıralama Toplamı"] = (ey_r + roic_r).astype(int)
    out = _sector_cap_pick(pool, "Sıralama Toplamı", n, ascending=True)
    out = out.sort_values("Sıralama Toplamı").reset_index(drop=True)
    out.insert(0, "Sıra", np.arange(1, len(out) + 1))
    out.attrs.update({"n_universe": int(len(d)), "n_eligible": int(elig.sum())})
    return out


MAGIC_SHEET_COLS = ["Sıra", "Hisse", "Sektör", "Sıralama Toplamı", "Kazanç Getirisi (EBIT/EV)", "EV/EBIT", "ROIC",
                     "Adil Değer Marjı", "Risk Sınıfı", "Likidite Notu", "Nihai Sinyal"]


# ---------------- Momentum ----------------
MOMENTUM_LIST_N = 15


def build_momentum_list(df, n=None):
    """Fiyat momentumu: son 6/12 ay getirisi (yüzdelik sırası), volatiliteye göre ayarlanmış. Değer/kalite
    filtresi YOKTUR — bu bilinçli bir tasarım: momentum stratejileri değerden bağımsız çalışır. Yalnızca
    işlem edilebilirlik süzgeci ve "Çok Pahalı" olmama (momentumun bir balonu takip etmesini önlemek için
    hafif bir değer güvenlik ağı) uygulanır."""
    n = MOMENTUM_LIST_N if n is None else n
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy(); d.attrs = {}
    if "Ana Sıralamada" in d.columns:
        d = d[d["Ana Sıralamada"].astype(str).str.startswith("Evet")].copy()
    d = d.reset_index(drop=True)
    N = lambda c: _num_series(d, c)
    r6, r12 = N("Backtest 6A İleri Getiri"), N("Backtest 12A İleri Getiri")
    vol = N("Vol Ann (σ)")
    val_cls = d["Değerleme Sınıfı"].astype(str) if "Değerleme Sınıfı" in d.columns else pd.Series("", index=d.index)
    elig = _base_eligibility(d) & (r6.notna() | r12.notna()) & (~val_cls.eq("Çok Pahalı")) & (vol.isna() | (vol < 1.20))
    pool = d[elig].copy()
    if pool.empty:
        return pd.DataFrame()
    z6 = (r6.loc[pool.index].rank(pct=True)).fillna(0.5)
    z12 = (r12.loc[pool.index].rank(pct=True)).fillna(0.5)
    vol_pen = (1.0 - vol.loc[pool.index].rank(pct=True)).fillna(0.5)   # düşük vol = daha yüksek puan
    pool["Momentum Skoru"] = (100 * (0.5 * z6 + 0.35 * z12 + 0.15 * vol_pen)).round(1)
    pool["6A Getiri"], pool["12A Getiri"] = r6.loc[pool.index], r12.loc[pool.index]
    out = _sector_cap_pick(pool, "Momentum Skoru", n, ascending=False)
    out = out.sort_values("Momentum Skoru", ascending=False).reset_index(drop=True)
    out.insert(0, "Sıra", np.arange(1, len(out) + 1))
    out.attrs.update({"n_universe": int(len(d)), "n_eligible": int(elig.sum())})
    return out


MOMENTUM_SHEET_COLS = ["Sıra", "Hisse", "Sektör", "Momentum Skoru", "6A Getiri", "12A Getiri", "Vol Ann (σ)",
                       "Değerleme Sınıfı", "Nihai Sinyal", "TV Görünüm", "Likidite Notu"]


# ---------------- Temettü (Gelir Yatırımı) ----------------
DIVIDEND_LIST_N = 15
DIVIDEND_MIN_YIELD = 0.04
DIVIDEND_MIN_QUARTERS_PAID = 4   # son 8 çeyreğin en az yarısında temettü ödemesi


def build_dividend_list(df, n=None):
    """Temettü verimine göre gelir yatırımı listesi. Temettü verimi, nakit akış tablosundaki "Ödenen Temettüler"
    kaleminden hesaplanır (TTM / Piyasa Değeri) — kâr payı ORANI değil, GERÇEK nakit çıkışıdır. Süreklilik,
    son 8 çeyreğin en az yarısında ödeme yapılmış olmasıyla kabaca ölçülür (İş Yatırım'da temettü tarihi/sıklığı
    ayrı bir alan olarak yok, bu yüzden yaklaşık)."""
    n = DIVIDEND_LIST_N if n is None else n
    if df is None or df.empty or "Temettü Verimi" not in df.columns:
        return pd.DataFrame()
    d = df.copy(); d.attrs = {}
    if "Ana Sıralamada" in d.columns:
        d = d[d["Ana Sıralamada"].astype(str).str.startswith("Evet")].copy()
    d = d.reset_index(drop=True)
    N = lambda c: _num_series(d, c)
    dy, dq = N("Temettü Verimi"), N("Temettü Ödeyen Çeyrek (8Ç)")
    ni = N("Net Kar (TTM)")
    payout = div_series = (N("Temettü Ödemesi (TTM)") / ni.where(ni > 0))
    elig = _base_eligibility(d) & (dy >= DIVIDEND_MIN_YIELD) & (dq >= DIVIDEND_MIN_QUARTERS_PAID) & (ni > 0) & (payout.isna() | (payout <= 1.0))
    pool = d[elig].copy()
    if pool.empty:
        return pd.DataFrame()
    pool["Temettü Ödeme Oranı"] = payout.loc[pool.index]
    # Sürdürülebilirlik: yüksek verim ama düşük kalite (ROIC/Piotroski) cezalandırılır
    roic_r = N("ROIC").loc[pool.index].rank(pct=True).fillna(0.5)
    piot_r = N("Piotroski F").loc[pool.index].rank(pct=True).fillna(0.5)
    pool["Gelir Puanı"] = (100 * (0.55 * dy.loc[pool.index].clip(upper=0.20) / 0.20 + 0.25 * roic_r + 0.20 * piot_r)).round(1)
    out = _sector_cap_pick(pool, "Gelir Puanı", n, ascending=False)
    out = out.sort_values("Gelir Puanı", ascending=False).reset_index(drop=True)
    out.insert(0, "Sıra", np.arange(1, len(out) + 1))
    out.attrs.update({"n_universe": int(len(d)), "n_eligible": int(elig.sum())})
    return out


DIVIDEND_SHEET_COLS = ["Sıra", "Hisse", "Sektör", "Gelir Puanı", "Temettü Verimi", "Temettü Ödeme Oranı",
                       "Temettü Ödeyen Çeyrek (8Ç)", "ROIC", "Piotroski F", "Adil Değer Marjı", "Risk Sınıfı"]


# ---------------- Düşük Volatilite ----------------
LOWVOL_LIST_N = 15


def build_low_vol_list(df, n=None):
    """En düşük yıllık volatiliteye sahip, temel kalitesi makul hisseler (düşük-volatilite anomalisi:
    düşük riskli hisseler risk-ayarlı bazda genelde daha iyi performans gösterir). Kalite tabanı olarak
    yalnızca "Çok Pahalı" ve çok zayıf Piotroski elenir — bu bir değer taraması değildir."""
    n = LOWVOL_LIST_N if n is None else n
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy(); d.attrs = {}
    if "Ana Sıralamada" in d.columns:
        d = d[d["Ana Sıralamada"].astype(str).str.startswith("Evet")].copy()
    d = d.reset_index(drop=True)
    N = lambda c: _num_series(d, c)
    vol, piot = N("Vol Ann (σ)"), N("Piotroski F")
    val_cls = d["Değerleme Sınıfı"].astype(str) if "Değerleme Sınıfı" in d.columns else pd.Series("", index=d.index)
    elig = _base_eligibility(d) & vol.notna() & (piot.isna() | (piot >= 4)) & (~val_cls.eq("Çok Pahalı"))
    pool = d[elig].copy()
    if pool.empty:
        return pd.DataFrame()
    pool["Volatilite Yüzdelik"] = vol.loc[pool.index].rank(pct=True)
    out = _sector_cap_pick(pool, "Vol Ann (σ)", n, ascending=True)
    out = out.sort_values("Vol Ann (σ)", ascending=True).reset_index(drop=True)
    out.insert(0, "Sıra", np.arange(1, len(out) + 1))
    out.attrs.update({"n_universe": int(len(d)), "n_eligible": int(elig.sum())})
    return out


LOWVOL_SHEET_COLS = ["Sıra", "Hisse", "Sektör", "Vol Ann (σ)", "Volatilite Yüzdelik", "Piotroski F", "ROE",
                     "Değerleme Sınıfı", "Adil Değer Marjı", "Risk Sınıfı"]


# ---------------- Sektöre Göre Ucuzluk (göreli değerleme) ----------------
SECTOR_REL_MIN_MEMBERS = 5
SECTOR_REL_LIST_N = 15


def build_sector_relative_list(df, n=None, min_members=SECTOR_REL_MIN_MEMBERS):
    """Mutlak eşikler yerine SEKTÖR İÇİ yüzdelik sıraya göre ucuzluk. Bankacılık gibi doğal olarak düşük F/K'lı
    veya teknoloji gibi doğal olarak yüksek F/K'lı sektörlerin mutlak eşiklerle (F/K≤12 gibi) elenmesini önler.
    Yalnızca ≥5 üyesi olan sektörler değerlendirilir (küçük örnekte yüzdelik anlamsız)."""
    n = SECTOR_REL_LIST_N if n is None else n
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy(); d.attrs = {}
    if "Ana Sıralamada" in d.columns:
        d = d[d["Ana Sıralamada"].astype(str).str.startswith("Evet")].copy()
    d = d.reset_index(drop=True)
    N = lambda c: _num_series(d, c)
    fk, pb, ev_ebit = N("F/K"), N("PD/DD"), N("EV/EBIT")
    sektor = d["Sektör"].astype(str) if "Sektör" in d.columns else pd.Series("Diğer", index=d.index)
    sizes = sektor.value_counts()
    ok_sector = sektor.map(lambda s: sizes.get(s, 0) >= min_members)
    fcf_y, capex_da = N("FCF Getirisi"), N("CapEx/Amortisman")
    elig = (
        _base_eligibility(d) & ok_sector & (fk >= 2.0) & (pb > 0)
        & (fcf_y.isna() | (fcf_y < 0.40)) & (capex_da.isna() | (capex_da < 4.0))   # tek seferlik kazanç/nakit anomalisi koruması
    )
    pool = d[elig].copy()
    if pool.empty:
        return pd.DataFrame()
    g = sektor.loc[pool.index]
    fk_p = fk.loc[pool.index].groupby(g).rank(pct=True)
    pb_p = pb.loc[pool.index].groupby(g).rank(pct=True)
    # Negatif EV/EBIT (negatif FAVÖK/EBIT) "ucuz" değil, bilgi eksikliği/kırmızı bayraktır — sektör içi
    # yüzdelik sırasından çıkarılır (nötr 0.5 verilir), yoksa negatif değerler yanlışlıkla en ucuz görünürdü.
    ev_pos = ev_ebit.loc[pool.index].where(ev_ebit.loc[pool.index] > 0)
    ev_p = ev_pos.groupby(g).rank(pct=True) if ev_pos.notna().any() else pd.Series(0.5, index=pool.index)
    ev_p = ev_p.fillna(0.5)
    pool["Sektör İçi Ucuzluk Skoru"] = (100 * (1.0 - (0.4 * fk_p + 0.35 * pb_p + 0.25 * ev_p))).round(1)
    pool["Sektör Üye Sayısı"] = g.map(sizes)
    out = _sector_cap_pick(pool, "Sektör İçi Ucuzluk Skoru", n, max_per_sector=3, max_per_sector_fill=4, ascending=False)
    out = out.sort_values("Sektör İçi Ucuzluk Skoru", ascending=False).reset_index(drop=True)
    out.insert(0, "Sıra", np.arange(1, len(out) + 1))
    out.attrs.update({"n_universe": int(len(d)), "n_eligible": int(elig.sum())})
    return out


SECTORREL_SHEET_COLS = ["Sıra", "Hisse", "Sektör", "Sektör Üye Sayısı", "Sektör İçi Ucuzluk Skoru", "F/K", "PD/DD",
                        "EV/EBIT", "Adil Değer Marjı", "Risk Sınıfı"]


# ---------------- Tarihsel F/K & PD/DD z-skoru (kendi ortalamasına göre en ucuz) ----------------
HIST_CHEAP_LIST_N = 25
HIST_CHEAP_LOOKBACK_DAYS = 750   # ~3 yıl
HIST_CHEAP_MIN_OBS = 40         # haftalık örneklem alt sınırı


def _safe_float(v):
    try:
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return np.nan
        x = float(v)
        return x if np.isfinite(x) else np.nan
    except Exception:
        return np.nan


def build_historical_cheap_list(df, session=None, n=None, lookback_days=None, min_obs=None):
    """F/K ve PD/DD'nin **kendi tarihsel ortalaması ve SD'sine** göre en ucuz 25 hisse.

    Yöntem:
      1) Yahoo ~3y fiyat → tarihsel F/K≈P/EPS, PD/DD≈P/BVPS → z-skor (tercih)
      2) Yahoo yoksa yedek: trailing + forward F/K–PD/DD evren içi ters rank (her zaman dolu liste)

    Forward F/K ve PD/DD skora %50 ağırlıkla girer (varsa).
    """
    n = HIST_CHEAP_LIST_N if n is None else int(n)
    lookback_days = HIST_CHEAP_LOOKBACK_DAYS if lookback_days is None else int(lookback_days)
    min_obs = max(15, int(min_obs if min_obs is not None else min(25, HIST_CHEAP_MIN_OBS)))

    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d.attrs = {}
    if "Ana Sıralamada" in d.columns:
        d = d[d["Ana Sıralamada"].astype(str).str.startswith("Evet")].copy()
    d = d.reset_index(drop=True)

    N = lambda c: _num_series(d, c)
    fk, pb = N("F/K"), N("PD/DD")
    fkf, pbf = N("F/K (Forward)"), N("PD/DD (Forward)")
    price = N("Fiyat")
    eps = N("EPS (TTM)") if "EPS (TTM)" in d.columns else N("EPS")
    bvps = N("Defter Değeri/Hisse")

    # EPS/BVPS yoksa F/K, PD/DD ve fiyattan geri çıkar
    eps_imp = eps.copy().astype(float)
    bvps_imp = bvps.copy().astype(float)
    for i in d.index:
        fk_i, pb_i, p_i = fk.loc[i], pb.loc[i], price.loc[i]
        e_i, b_i = eps_imp.loc[i], bvps_imp.loc[i]
        if (not np.isfinite(e_i) or e_i <= 0) and np.isfinite(fk_i) and np.isfinite(p_i) and fk_i > 0 and p_i > 0:
            eps_imp.loc[i] = p_i / fk_i
        if (not np.isfinite(b_i) or b_i <= 0) and np.isfinite(pb_i) and np.isfinite(p_i) and pb_i > 0 and p_i > 0:
            bvps_imp.loc[i] = p_i / pb_i

    # Temel elig: finansal hariç + pozitif çarpan (likidite/güven gevşek — boş liste olmasın)
    try:
        elig = _base_eligibility(d, require_liquidity=False, require_price_ok=False, exclude_high_risk=False)
    except Exception:
        elig = pd.Series(True, index=d.index)
    elig = elig & (fk > 0) & (pb > 0) & (price > 0)
    # EPS/BVPS en az bir yoldan dolu olmalı
    elig = elig & (eps_imp > 0) & (bvps_imp > 0)
    pool = d[elig].copy()
    if pool.empty:
        # Son çare: sadece F/K PD/DD > 0
        pool = d[(fk > 0) & (pb > 0)].copy()
    if pool.empty:
        print("Tarihsel Ucuz: uygun hisse yok (F/K/PD/DD)", flush=True)
        return pd.DataFrame()

    if session is None:
        try:
            session = requests.Session()
            session.headers.update(HEAD)
        except Exception:
            session = requests.Session()
            session.headers.update({"User-Agent": "Mozilla/5.0"})

    scored = 0
    failed = 0
    rows_meta = []
    for idx, row in pool.iterrows():
        code = str(row.get("Hisse") or "").strip().upper()
        if not code:
            rows_meta.append({"_kaynak": "yok"})
            failed += 1
            continue
        e = float(eps_imp.loc[idx])
        b = float(bvps_imp.loc[idx])
        cur_fk = float(fk.loc[idx])
        cur_pb = float(pb.loc[idx])
        fk_fwd = _safe_float(row.get("F/K (Forward)"))
        pb_fwd = _safe_float(row.get("PD/DD (Forward)"))
        dpe = _safe_float(row.get("Δ F/K (Fwd−TTM)"))

        px = None
        try:
            px = fetch_yahoo_history(session, code, lookback_days=lookback_days)
        except Exception:
            px = None

        meta = {
            "F/K (Forward)": round(fk_fwd, 2) if np.isfinite(fk_fwd) else np.nan,
            "PD/DD (Forward)": round(pb_fwd, 2) if np.isfinite(pb_fwd) else np.nan,
            "FK Ort (tarihsel)": np.nan, "FK SD (tarihsel)": np.nan,
            "FK z-skor": np.nan, "FK yüzdelik": np.nan,
            "PD/DD Ort (tarihsel)": np.nan, "PD/DD SD (tarihsel)": np.nan,
            "PD/DD z-skor": np.nan, "PD/DD yüzdelik": np.nan,
            "FK Fwd z-skor": np.nan, "PD/DD Fwd z-skor": np.nan,
            "Tarihsel Ucuzluk Skoru": np.nan,
            "Gözlem (haftalık)": 0,
            "_kaynak": "yok",
        }

        ok_hist = False
        if px is not None and len(px) >= max(min_obs * 2, 40):
            step = 5
            sample = px[::step]
            if len(sample) < min_obs:
                sample = px
            if len(sample) >= min_obs and e > 0 and b > 0:
                hist_fk = sample / e
                hist_pb = sample / b
                hist_fk = hist_fk[(hist_fk > 0) & (hist_fk < 200)]
                hist_pb = hist_pb[(hist_pb > 0) & (hist_pb < 50)]
                if len(hist_fk) >= min_obs and len(hist_pb) >= min_obs:
                    mu_fk, sd_fk = float(np.mean(hist_fk)), float(np.std(hist_fk, ddof=1))
                    mu_pb, sd_pb = float(np.mean(hist_pb)), float(np.std(hist_pb, ddof=1))
                    if sd_fk >= 1e-6 and sd_pb >= 1e-6:
                        z_fk = (cur_fk - mu_fk) / sd_fk
                        z_pb = (cur_pb - mu_pb) / sd_pb
                        z_fk_fwd = ((fk_fwd - mu_fk) / sd_fk) if np.isfinite(fk_fwd) and fk_fwd > 0 else np.nan
                        z_pb_fwd = ((pb_fwd - mu_pb) / sd_pb) if np.isfinite(pb_fwd) and pb_fwd > 0 else np.nan
                        parts_t = [-0.5 * z_fk, -0.5 * z_pb]
                        parts_f = []
                        if np.isfinite(z_fk_fwd):
                            parts_f.append(-0.5 * z_fk_fwd)
                        if np.isfinite(z_pb_fwd):
                            parts_f.append(-0.5 * z_pb_fwd)
                        if parts_f:
                            cheap_score = 0.5 * float(np.mean(parts_t)) + 0.5 * float(np.mean(parts_f))
                        else:
                            cheap_score = float(np.mean(parts_t))
                        if np.isfinite(dpe) and dpe < 0:
                            cheap_score += min(0.25, abs(dpe) / max(cur_fk, 1) * 0.5)
                        meta.update({
                            "FK Ort (tarihsel)": round(mu_fk, 2),
                            "FK SD (tarihsel)": round(sd_fk, 2),
                            "FK z-skor": round(z_fk, 2),
                            "FK yüzdelik": round(float((hist_fk <= cur_fk).mean()) * 100, 1),
                            "PD/DD Ort (tarihsel)": round(mu_pb, 2),
                            "PD/DD SD (tarihsel)": round(sd_pb, 2),
                            "PD/DD z-skor": round(z_pb, 2),
                            "PD/DD yüzdelik": round(float((hist_pb <= cur_pb).mean()) * 100, 1),
                            "FK Fwd z-skor": round(z_fk_fwd, 2) if np.isfinite(z_fk_fwd) else np.nan,
                            "PD/DD Fwd z-skor": round(z_pb_fwd, 2) if np.isfinite(z_pb_fwd) else np.nan,
                            "Tarihsel Ucuzluk Skoru": round(float(cheap_score), 2),
                            "Gözlem (haftalık)": int(min(len(hist_fk), len(hist_pb))),
                            "_kaynak": "yahoo_z",
                        })
                        ok_hist = True
                        scored += 1

        if not ok_hist:
            failed += 1
            # Yedek skor sonra evren rank ile doldurulacak; şimdilik placeholder
            meta["_kaynak"] = "rank_yedek"
        rows_meta.append(meta)

    meta_df = pd.DataFrame(rows_meta, index=pool.index)
    pool = pool.join(meta_df)

    # Yahoo z-skoru olmayanlara evren içi rank yedek skoru
    need = pool["Tarihsel Ucuzluk Skoru"].isna()
    if need.any():
        def _inv_pct(s):
            s2 = pd.to_numeric(s, errors="coerce")
            s2 = s2.where(s2 > 0)
            return (1.0 - s2.rank(pct=True)).fillna(0.4)

        r_fk = _inv_pct(pool["F/K"])
        r_pb = _inv_pct(pool["PD/DD"])
        r_fkf = _inv_pct(pool["F/K (Forward)"]) if "F/K (Forward)" in pool.columns else pd.Series(0.4, index=pool.index)
        r_pbf = _inv_pct(pool["PD/DD (Forward)"]) if "PD/DD (Forward)" in pool.columns else pd.Series(0.4, index=pool.index)
        # Rank skoru 0–1 → z-benzeri ölçek için *2 - 1 (yaklaşık -1…+1), yüksek = ucuz
        rank_score = (0.3 * r_fk + 0.3 * r_pb + 0.2 * r_fkf + 0.2 * r_pbf) * 2.0 - 0.5
        pool.loc[need, "Tarihsel Ucuzluk Skoru"] = rank_score.loc[need].round(2)
        pool.loc[need, "_kaynak"] = "rank_yedek"

    pool = pool[pool["Tarihsel Ucuzluk Skoru"].notna()].copy()
    if pool.empty:
        print("Tarihsel Ucuz: skor üretilemedi", flush=True)
        return pd.DataFrame()

    # Yumuşak kalite cezası (eleme değil): aşırı borç / negatif FCF skoru düşürür
    fcf = _num_series(pool, "FCF Getirisi")
    nd_eq = _num_series(pool, "Net Borç/Özkaynak")
    penalty = pd.Series(0.0, index=pool.index)
    penalty += np.where(fcf.notna() & (fcf < 0), 0.15, 0.0)
    penalty += np.where(fcf.notna() & (fcf > 0.45), 0.10, 0.0)
    penalty += np.where(nd_eq.notna() & (nd_eq > 2.0), 0.20, 0.0)
    penalty += np.where(nd_eq.notna() & (nd_eq > 1.5) & (nd_eq <= 2.0), 0.08, 0.0)
    pool["Tarihsel Ucuzluk Skoru"] = (pool["Tarihsel Ucuzluk Skoru"] - penalty).round(2)

    n_yahoo = int((pool.get("_kaynak") == "yahoo_z").sum()) if "_kaynak" in pool.columns else 0
    n_rank = int((pool.get("_kaynak") == "rank_yedek").sum()) if "_kaynak" in pool.columns else 0
    print(f"Tarihsel Ucuz: havuz={len(pool)} yahoo_z={n_yahoo} rank_yedek={n_rank} (fail_fetch≈{failed})", flush=True)

    out = pool.sort_values("Tarihsel Ucuzluk Skoru", ascending=False).head(n).reset_index(drop=True)
    if "Sıra" in out.columns:
        out = out.drop(columns=["Sıra"])
    out.insert(0, "Sıra", np.arange(1, len(out) + 1))
    out.attrs.update({
        "n_universe": int(len(d)),
        "n_eligible": int(elig.sum()) if hasattr(elig, "sum") else len(pool),
        "n_scored": int(len(pool)),
        "n_yahoo": n_yahoo,
        "n_rank_yedek": n_rank,
    })
    return out


HISTCHEAP_SHEET_COLS = [
    "Sıra", "Hisse", "Sektör", "Tarihsel Ucuzluk Skoru", "_kaynak",
    "F/K", "F/K (Forward)", "FK Ort (tarihsel)", "FK SD (tarihsel)", "FK z-skor", "FK Fwd z-skor", "FK yüzdelik",
    "PD/DD", "PD/DD (Forward)", "PD/DD Ort (tarihsel)", "PD/DD SD (tarihsel)", "PD/DD z-skor", "PD/DD Fwd z-skor", "PD/DD yüzdelik",
    "FCF Getirisi", "Net Borç/Özkaynak", "Gözlem (haftalık)", "Adil Değer Marjı", "Risk Sınıfı", "Final Score",
]


# ---------------- Faktör kotalı 25 hisse (değer / kalite / savunma / momentum) ----------------
FAKTOR_KOTA = {"deger": 8, "kalite": 6, "savunma": 6, "momentum": 5}  # toplam 25
FAKTOR_LIST_N = 25


def build_faktor_kota_list(df, session=None, n=None, hist_cheap_df=None):
    """Zorunlu faktör kotalı seçim: değer 8 + kalite 6 + savunma 6 + momentum 5 = 25.

    Değer: tarihsel+forward F/K–PD/DD ucuzluk (hist_cheap_df varsa ondan, yoksa trailing+forward çarpan rank)
    Kalite: yüksek ROIC/ROE + makul borç + Piotroski
    Savunma: düşük vol / yüksek temettü / düşük beta proxy (vol)
    Momentum: 6A/12A getiri + teknik skor

    Aynı hisse birden fazla kovaya aday olabilir; önce atanmamışlara öncelik.
    """
    n = FAKTOR_LIST_N if n is None else int(n)
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d.attrs = {}
    if "Ana Sıralamada" in d.columns:
        d = d[d["Ana Sıralamada"].astype(str).str.startswith("Evet")].copy()
    d = d.reset_index(drop=True)
    if d.empty:
        return pd.DataFrame()

    N = lambda c: _num_series(d, c)
    base = _base_eligibility(d)
    pool = d[base].copy()
    if pool.empty:
        return pd.DataFrame()

    # --- Değer skoru ---
    deger_score = pd.Series(np.nan, index=pool.index)
    if hist_cheap_df is not None and not hist_cheap_df.empty and "Hisse" in hist_cheap_df.columns:
        if "Tarihsel Ucuzluk Skoru" in hist_cheap_df.columns:
            m = hist_cheap_df.drop_duplicates("Hisse").set_index("Hisse")["Tarihsel Ucuzluk Skoru"]
            deger_score = pool["Hisse"].astype(str).map(lambda h: float(m[h]) if h in m.index and pd.notna(m[h]) else np.nan)
    if deger_score.notna().sum() < 5:
        fk, pb = N("F/K").loc[pool.index], N("PD/DD").loc[pool.index]
        fkf, pbf = N("F/K (Forward)").loc[pool.index], N("PD/DD (Forward)").loc[pool.index]
        # Düşük çarpan = yüksek skor (yüzdelik ters)
        def _inv_rank(s):
            s2 = s.where(s > 0)
            return 1.0 - s2.rank(pct=True)
        r = 0.3 * _inv_rank(fk).fillna(0.3) + 0.3 * _inv_rank(pb).fillna(0.3)
        r = r + 0.2 * _inv_rank(fkf).fillna(0.3) + 0.2 * _inv_rank(pbf).fillna(0.3)
        deger_score = r

    # --- Kalite skoru ---
    roic = N("ROIC").loc[pool.index]
    roe = N("ROE").loc[pool.index]
    piot = N("Piotroski F").loc[pool.index]
    nd = N("Net Borç/Özkaynak").loc[pool.index]
    fcf = N("FCF Getirisi").loc[pool.index]
    kalite_score = (
        0.30 * roic.rank(pct=True).fillna(0.3)
        + 0.20 * roe.rank(pct=True).fillna(0.3)
        + 0.20 * piot.rank(pct=True).fillna(0.3)
        + 0.15 * (1.0 - nd.clip(lower=0).rank(pct=True)).fillna(0.3)
        + 0.15 * fcf.where(fcf > 0).rank(pct=True).fillna(0.3)
    )

    # --- Savunma skoru ---
    vol = N("Vol Ann (σ)").loc[pool.index]
    div = N("Temettü Verimi").loc[pool.index]
    altman = N("Altman Z").loc[pool.index]
    mdd_col = "Max DD" if "Max DD" in pool.columns else None
    savunma_score = (
        0.35 * (1.0 - vol.rank(pct=True)).fillna(0.3)
        + 0.25 * div.rank(pct=True).fillna(0.3)
        + 0.25 * altman.rank(pct=True).fillna(0.3)
        + 0.15 * (1.0 - N("Net Borç/Özkaynak").loc[pool.index].clip(lower=0).rank(pct=True)).fillna(0.3)
    )

    # --- Momentum skoru ---
    g6 = N("Getiri 6A").loc[pool.index] if "Getiri 6A" in d.columns else N("6A Getiri").loc[pool.index]
    g12 = N("Getiri 12A").loc[pool.index] if "Getiri 12A" in d.columns else N("12A Getiri").loc[pool.index]
    if g6.notna().sum() < 3:
        g6 = N("Backtest 6A İleri Getiri").loc[pool.index]
    if g12.notna().sum() < 3:
        g12 = N("Backtest 12A İleri Getiri").loc[pool.index]
    tech = N("Teknik Skor").loc[pool.index]
    mom_score = (
        0.40 * g6.rank(pct=True).fillna(0.3)
        + 0.35 * g12.rank(pct=True).fillna(0.3)
        + 0.25 * tech.rank(pct=True).fillna(0.3)
    )

    pool = pool.copy()
    pool["_deger"] = deger_score
    pool["_kalite"] = kalite_score
    pool["_savunma"] = savunma_score
    pool["_momentum"] = mom_score

    selected = []
    used = set()
    buckets = [
        ("deger", "_deger", FAKTOR_KOTA["deger"]),
        ("kalite", "_kalite", FAKTOR_KOTA["kalite"]),
        ("savunma", "_savunma", FAKTOR_KOTA["savunma"]),
        ("momentum", "_momentum", FAKTOR_KOTA["momentum"]),
    ]
    for bucket_name, col, quota in buckets:
        cand = pool[~pool["Hisse"].astype(str).isin(used)].copy()
        cand = cand.sort_values(col, ascending=False)
        take = cand.head(quota)
        for _, r in take.iterrows():
            h = str(r["Hisse"])
            if h in used:
                continue
            used.add(h)
            row = r.to_dict()
            row["Faktör Kova"] = bucket_name
            row["Kova Skoru"] = round(float(r[col]), 3) if pd.notna(r[col]) else np.nan
            selected.append(row)
        # Kota dolmazsa diğer kovadan doldurma yok — bilerek eksik bırakılabilir

    if not selected:
        return pd.DataFrame()
    out = pd.DataFrame(selected)
    # Sıra: kova sırası + kova içi skor
    kova_order = {"deger": 0, "kalite": 1, "savunma": 2, "momentum": 3}
    out["_ko"] = out["Faktör Kova"].map(kova_order)
    out = out.sort_values(["_ko", "Kova Skoru"], ascending=[True, False]).reset_index(drop=True)
    out.insert(0, "Sıra", np.arange(1, len(out) + 1))
    out = out.drop(columns=["_ko"], errors="ignore")
    out.attrs.update({"n_universe": int(len(d)), "n_selected": int(len(out)), "kota": dict(FAKTOR_KOTA)})
    return out.head(n)


FAKTOR_SHEET_COLS = [
    "Sıra", "Hisse", "Sektör", "Faktör Kova", "Kova Skoru",
    "F/K", "F/K (Forward)", "PD/DD", "PD/DD (Forward)",
    "ROIC", "ROE", "Piotroski F", "FCF Getirisi", "Net Borç/Özkaynak",
    "Vol Ann (σ)", "Temettü Verimi", "Altman Z",
    "Teknik Skor", "Adil Değer Marjı", "Final Score", "Risk Sınıfı",
]


# ---------------- Ortak: yazım + stil ----------------
def _generic_list_for_sheet(v, cols):
    if v is None or v.empty:
        return v
    return v[[c for c in cols if c in v.columns]]


# ---------------- Çoklu Liste Konsensüsü ----------------
CONSENSUS_LISTS = [
    "Değer Yatırımı", "Magic Formula", "Momentum", "Temettü", "Düşük Volatilite",
    "Sektöre Göre Ucuz", "Tarihsel Ucuz", "Faktör Kota",
]
CONSENSUS_MIN_LISTS = 2


def build_consensus_list(lists_dict, n=15):
    """Altı bağımsız seçim listesinden kaçında bir hissenin geçtiğini sayar. Farklı felsefelerin (değer,
    kalite+ucuzluk, momentum, gelir, düşük risk, göreli değerleme) AYNI ANDA işaret ettiği hisseler, tek bir
    yönteme dayanmaktan daha güçlü bir sinyal taşır. `lists_dict`: {liste_adı: DataFrame} (her biri 'Hisse' ve
    'Sıra' kolonlarına sahip build_*_list çıktısı). Yalnızca bilgi amaçlıdır; kendi eleme kriteri yoktur —
    zaten her alt liste kendi süzgecinden geçmiş hisseleri içerir."""
    rows = {}
    for name, lst in lists_dict.items():
        if lst is None or lst.empty or "Hisse" not in lst.columns:
            continue
        max_sira = float(lst["Sıra"].max()) if "Sıra" in lst.columns and len(lst) else float(len(lst))
        for _, r in lst.iterrows():
            h = r["Hisse"]
            sira = float(r.get("Sıra", np.nan))
            pct = 1.0 - (sira - 1.0) / max_sira if max_sira > 0 and pd.notna(sira) else 0.5  # 1.0 = listenin 1.'si
            e = rows.setdefault(h, {"Hisse": h, "Listeler": [], "_pcts": []})
            e["Listeler"].append(name)
            e["_pcts"].append(pct)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(list(rows.values()))
    out["Kaç Listede"] = out["Listeler"].map(len)
    out["Listeler"] = out["Listeler"].map(lambda ls: ", ".join(ls))
    out["Ort. Sıra Yüzdeliği"] = out["_pcts"].map(lambda ps: round(100 * float(np.mean(ps)), 1))
    out = out.drop(columns=["_pcts"])
    out = out[out["Kaç Listede"] >= CONSENSUS_MIN_LISTS].sort_values(
        ["Kaç Listede", "Ort. Sıra Yüzdeliği"], ascending=[False, False]).reset_index(drop=True)
    if n:
        out = out.head(n)
    out.insert(0, "Sıra", np.arange(1, len(out) + 1))
    return out


CONSENSUS_SHEET_COLS = ["Sıra", "Hisse", "Kaç Listede", "Ort. Sıra Yüzdeliği", "Listeler"]


def _style_generic_list_sheet(wb, sheet, title, desc, cols, pct_cols=(), dec_cols=()):
    if sheet not in wb.sheetnames:
        return
    ws = wb[sheet]
    if ws.max_row <= 2 and ws.max_column <= 2:
        ws["A1"].font = Font(bold=True, color="C00000")
        return
    ws.insert_rows(1, 3)
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14, color="1F4E79")
    ws["A2"] = desc
    ws["A3"] = "Yatırım tavsiyesi değildir. Bu sayfa Aday Listesi ve Değer Yatırımı'ndan BAĞIMSIZDIR; farklı bir strateji/felsefe uygular."
    for r in (1, 2, 3):
        ws.cell(r, 1).alignment = Alignment(wrap_text=True, vertical="top")
    try:
        for r in (2, 3):
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=9)
            ws.row_dimensions[r].height = 32
    except Exception:
        pass
    hdr = [str(c.value) for c in ws[4]]
    for cell in ws[4]:
        if cell.value:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
    for r in range(5, ws.max_row + 1):
        for ci, name in enumerate(hdr, start=1):
            if name in pct_cols:
                ws.cell(r, ci).number_format = "0.0%"
            elif name in dec_cols:
                ws.cell(r, ci).number_format = "0.00"
    for ci, name in enumerate(hdr, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = 24 if name == "Sektör" else (10 if name in ("Sıra", "Hisse") else 15)
    ws.freeze_panes = "C5"


def _style_candidate_sheet(wb, df_cand=None):
    if "Aday Listesi" not in wb.sheetnames:
        return
    ws = wb["Aday Listesi"]
    # If only a note row, skip heavy style
    if ws.max_row <= 2 and ws.max_column <= 2:
        ws["A1"].font = Font(bold=True, color="C00000")
        return
    ws.insert_rows(1, 4)
    ws["A1"] = "ADAY LİSTESİ (MAX 15) — TIER A/B DEĞER · GRAHAM+BUFFETT · SEKTÖR≤2"
    ws["A1"].font = Font(bold=True, size=14, color="1F4E79")
    thr = getattr(df_cand, "attrs", {}).get("threshold") if df_cand is not None else None
    top_pct = getattr(df_cand, "attrs", {}).get("top_pct", 0.30) if df_cand is not None else 0.30
    n_u = getattr(df_cand, "attrs", {}).get("n_universe", "") if df_cand is not None else ""
    n_c = getattr(df_cand, "attrs", {}).get("n_candidates", ws.max_row - 5) if df_cand is not None else ws.max_row - 5
    min_conf = getattr(df_cand, "attrs", {}).get("min_confidence", 55) if df_cand is not None else 55
    fallback_used = getattr(df_cand, "attrs", {}).get("fallback_used", False) if df_cand is not None else False
    n_a = getattr(df_cand, "attrs", {}).get("n_tier_a", 0) if df_cand is not None else 0
    n_b = getattr(df_cand, "attrs", {}).get("n_tier_b", 0) if df_cand is not None else 0
    n_f = getattr(df_cand, "attrs", {}).get("n_fill", 0) if df_cand is not None else 0
    if n_f == 0:
        # style(path) bu fonksiyonu df_cand vermeden, dosyadan çağırır → Tier sayılarını sayfadan oku
        _modes = []
        if df_cand is not None and "Aday Modu" in getattr(df_cand, "columns", []):
            _modes = df_cand["Aday Modu"].astype(str).tolist()
        else:
            _hdr = [str(c.value) for c in ws[5]]
            if "Aday Modu" in _hdr:
                _ci = _hdr.index("Aday Modu") + 1
                _modes = [str(ws.cell(r, _ci).value or "") for r in range(6, ws.max_row + 1)]
        n_a = sum(x.startswith("Tier A") for x in _modes)
        n_b = sum(x.startswith("Tier B") for x in _modes)
        n_f = sum(x.startswith("Tier C") for x in _modes)
        fallback_used = n_f > 0
    _tech_note = ""
    try:
        _hdr2 = [str(c.value) for c in ws[5]]
        def _colvals(name):
            return [str(ws.cell(r, _hdr2.index(name) + 1).value or "") for r in range(6, ws.max_row + 1)] if name in _hdr2 else []
        _tv = _colvals("TV Görünüm"); _uy = _colvals("Uyum Kadranı")
        _n = max(len(_tv), 1)
        _sat = sum(("SAT" in x.upper()) for x in _tv)
        _kac = sum(x.startswith("KAÇIN") for x in _uy)
        if _tv:
            _tech_note = f" · ZAMANLAMA: adayların {_sat}/{_n}'i TradingView SAT, {_kac}/{_n}'i 'KAÇIN' kadranında (teknik eleme kapısı kapalı; kademeli giriş/teyit bekleyin)."
    except Exception:
        _tech_note = ""
    ws["A2"] = f"Kural seti: sert kapılar (H1-H8) + güvenlik marjı (A ≥%{MOS_TIER_A*100:.0f} · B ≥%{MOS_TIER_B*100:.0f} · C ≥%{MOS_TIER_C*100:.0f}) + Tier A/B eşikleri · Güven≥{int(min_conf) if min_conf else 55} · sektör başına ≤2 (Tier C'de ≤3)"
    if fallback_used and n_f:
        ws["A1"] = f"ADAY LİSTESİ ({n_c}) — {n_a} TIER A + {n_b} TIER B + {n_f} TIER C (KRİTERLERE EN YAKIN)"
        ws["A3"] = (
            f"Tier A/B kriterlerini tam geçen {n_a + n_b} hisse var; liste, sert kapıları geçen ve en çok kriteri karşılayan "
            f"{n_f} hisseyle tamamlandı (Tier C). Hangi kriterin kaçırıldığı 'Kaçırılan Kriterler' kolonunda. {('Evren ' + str(n_u) + ' → ') if n_u else ''}Aday {n_c}"
        )
        ws["A4"] = "Renkler: yeşil = Tier A · sarı = Tier B · turuncu = Tier C (yakın ama tam değil). Al-sat sinyali değildir; öncelik A > B > C. Ufuk 6–12 ay. Detay: 'Kural Seti'." + _tech_note
    else:
        ws["A3"] = (
            f"Kalite+Nakit+Değer kapıları · değer tuzağı ele · NetBorç/FCF<5 · "
            f"Mod: Değer Yatırımı · {('Evren ' + str(n_u) + ' → ') if n_u else ''}Aday {n_c}"
        )
        ws["A4"] = "Al-sat sinyali değildir. Ufuk 6–12 ay. Detay için 'Kural Seti' sayfasına bakın."
    for r in range(1, 5):
        ws.cell(r, 1).alignment = Alignment(wrap_text=True)
    try:
        ws.merge_cells("A1:H1")
        ws.merge_cells("A2:H2")
        ws.merge_cells("A3:H3")
        ws.merge_cells("A4:H4")
    except Exception:
        pass
    header_row = 5
    for cell in ws[header_row]:
        if cell.value:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")
    # v192.6: renk artık TIER'a göre (v191: ilk 10 satır sabit yeşildi ve hiçbir şey ifade etmiyordu)
    _tier_fill = {"A": PatternFill("solid", fgColor="C6EFCE"),   # yeşil: Tier A (sıkı değer)
                  "B": PatternFill("solid", fgColor="FFF2CC"),   # sarı: Tier B (şartlı)
                  "C": PatternFill("solid", fgColor="FCE4D6")}   # turuncu: Tier C (kriterlere yakın)
    _hdr_cells = [str(c.value) for c in ws[header_row]]
    _mi = _hdr_cells.index("Aday Modu") + 1 if "Aday Modu" in _hdr_cells else None
    for r in range(header_row + 1, ws.max_row + 1):
        _m = str(ws.cell(r, _mi).value or "") if _mi else ""
        _k = "A" if _m.startswith("Tier A") else "B" if _m.startswith("Tier B") else "C" if _m.startswith("Tier C") else None
        if _k:
            for c in range(1, min(8, ws.max_column + 1)):
                ws.cell(r, c).fill = _tier_fill[_k]
    ws["A5"].comment = None
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 20


def create_dashboard(wb):
    """Kullanışlı tek ekran dashboard: KPI + Top15 aday + Shannon + sınıf + metodoloji özeti."""
    P = THEME
    for name in ("DASHBOARD", "YONETICI_OZETI"):
        if name in wb.sheetnames:
            del wb[name]
    if "Değerleme Özeti" not in wb.sheetnames:
        return

    import pandas as pd
    from openpyxl.chart import BarChart, Reference, PieChart
    from openpyxl.chart.label import DataLabelList
    from openpyxl.utils.dataframe import dataframe_to_rows

    ws_val = wb["Değerleme Özeti"]
    # header row 4 after style insert — but create_dashboard runs after style which inserts 3 rows
    # At style() time create_dashboard is called on already-written data with header at row 4
    headers = [c.value for c in ws_val[4]]
    if not headers or headers[0] is None:
        headers = [c.value for c in ws_val[1]]
        data_start = 2
    else:
        data_start = 5

    # Read values into lists
    col_idx = {h: i for i, h in enumerate(headers) if h}
    def col(name):
        return col_idx.get(name)

    rows_data = []
    for r in range(data_start, ws_val.max_row + 1):
        vals = [ws_val.cell(r, c + 1).value for c in range(len(headers))]
        if vals and vals[0]:
            rows_data.append(vals)
    if not rows_data:
        return
    df = pd.DataFrame(rows_data, columns=headers)

    def series(name):
        if name not in df.columns:
            return pd.Series(dtype=float)
        return pd.to_numeric(df[name], errors="coerce")

    n = len(df)
    score = series("Risk Ayarlı Skor")
    if score.isna().all():
        score = series("Final Score")
    conf = series("Güven Skoru")
    avg_score = float(score.mean()) if score.notna().any() else 0
    avg_conf = float(conf.mean()) if conf.notna().any() else 0
    n_aplus = int((df.get("Not", pd.Series(dtype=str)).astype(str).isin(["A+", "A"])).sum()) if "Not" in df.columns else 0
    n_isk = 0
    if "Değerleme Sınıfı" in df.columns:
        n_isk = int(df["Değerleme Sınıfı"].astype(str).str.contains("İskonto|Derin", case=False, na=False).sum())
    n_altman = 0
    if "Altman Bölge" in df.columns:
        n_altman = int((df["Altman Bölge"].astype(str) == "Güvenli").sum())
    n_net = int((df.get("Shannon Notu", pd.Series(dtype=str)).astype(str) == "NET SİNYAL").sum()) if "Shannon Notu" in df.columns else 0

    # Top 15 candidates from Aday Listesi sheet if exists
    cand_rows = []
    if "Aday Listesi" in wb.sheetnames:
        ac = wb["Aday Listesi"]
        # find header row with Hisse
        hdr_r = None
        for r in range(1, min(12, ac.max_row + 1)):
            vals = [ac.cell(r, c).value for c in range(1, min(20, ac.max_column + 1))]
            if "Hisse" in vals:
                hdr_r = r
                break
        if hdr_r:
            headers_c = [ac.cell(hdr_r, c).value for c in range(1, ac.max_column + 1)]
            for r in range(hdr_r + 1, min(hdr_r + 16, ac.max_row + 1)):
                hisse = ac.cell(r, headers_c.index("Hisse") + 1).value if "Hisse" in headers_c else None
                if not hisse:
                    continue
                def g(name):
                    if name not in headers_c:
                        return None
                    return ac.cell(r, headers_c.index(name) + 1).value
                cand_rows.append({
                    "Sıra": g("Aday Sıra") or len(cand_rows) + 1,
                    "Kova": g("Kova") or "",
                    "Hisse": hisse,
                    "Sektör": g("Sektör"),
                    "Skor": g("Risk Ayarlı Skor") or g("Final Score") or g("Aday Skoru"),
                    "Shannon": g("Shannon Notu"),
                    "Kelly": g("Kelly Pay Proxy"),
                    "DD": g("DD Aksiyon"),
                    "Marj": g("Adil Değer Marjı"),
                })

    dash = wb.create_sheet("DASHBOARD", 0)
    dash.sheet_view.showGridLines = False
    dash.sheet_properties.tabColor = P["primary"]

    # Title
    dash.merge_cells("A1:L1")
    dash["A1"] = "BIST DEĞERLEME v191 — KOMUTA PANELİ"
    dash["A1"].font = Font(name=P["title_font"], size=22, bold=True, color=P["white"])
    dash["A1"].fill = PatternFill("solid", fgColor=P["dark"])
    dash.row_dimensions[1].height = 36

    dash.merge_cells("A2:L2")
    dash["A2"] = f"Tarama özeti · {datetime.now():%d.%m.%Y %H:%M} · Yatırım tavsiyesi değildir · Aday listesi max 15"
    dash["A2"].font = Font(name=P["body_font"], size=10, color=P["white"])
    dash["A2"].fill = PatternFill("solid", fgColor=P["primary"])

    # KPI cards row 4-5
    kpis = [
        ("A4", "B4", "Analiz", n, P["navy"]),
        ("C4", "D4", "Ort. Skor", round(avg_score, 1), P["primary"]),
        ("E4", "F4", "Ort. Güven", round(avg_conf, 1), "1565C0"),
        ("G4", "H4", "A+/A", n_aplus, P["green"]),
        ("I4", "J4", "İskontolu", n_isk, P["accent"]),
        ("K4", "L4", "NET SİNYAL", n_net, "00695C"),
    ]
    for a, b, label, val, color in kpis:
        dash.merge_cells(f"{a}:{b}")
        dash.merge_cells(f"{a[0]}5:{b[0]}5")
        dash[a] = label
        dash[a].font = Font(bold=True, color=P["white"], size=10)
        dash[a].fill = PatternFill("solid", fgColor=color)
        dash[a].alignment = Alignment(horizontal="center")
        dash[f"{a[0]}5"] = val
        dash[f"{a[0]}5"].font = Font(name=P["title_font"], size=18, bold=True, color=P["navy"])
        dash[f"{a[0]}5"].fill = PatternFill("solid", fgColor=P["stripe"])
        dash[f"{a[0]}5"].alignment = Alignment(horizontal="center")

    # Section: Top 15 Aday
    dash.merge_cells("A7:I7")
    dash["A7"] = "TOP 15 ADAY — 3 KOVA (1 ÖNCELİK / 2 ŞARTLI / 3 BEKLE)"
    dash["A7"].font = Font(bold=True, color=P["white"], size=12)
    dash["A7"].fill = PatternFill("solid", fgColor=P["navy"])

    headers15 = ["Sıra", "Kova", "Hisse", "Sektör", "Skor", "Shannon", "Kelly %", "DD", "Marj"]
    for j, h in enumerate(headers15, 1):
        cell = dash.cell(8, j, h)
        cell.font = Font(bold=True, color=P["white"], size=9)
        cell.fill = PatternFill("solid", fgColor=P["primary"])
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    green = PatternFill("solid", fgColor="E8F5E9")
    yellow = PatternFill("solid", fgColor="FFF8E1")
    fill_kova = {
        "1 ÖNCELİK": PatternFill("solid", fgColor="C8E6C9"),
        "2 ŞARTLI": PatternFill("solid", fgColor="FFF9C4"),
        "3 BEKLE": PatternFill("solid", fgColor="FFCDD2"),
    }
    for i, row in enumerate(cand_rows[:15]):
        r = 9 + i
        kova = str(row.get("Kova") or "")
        dash.cell(r, 1, row.get("Sıra"))
        dash.cell(r, 2, kova)
        dash.cell(r, 3, row.get("Hisse"))
        dash.cell(r, 4, row.get("Sektör"))
        sk = row.get("Skor")
        try:
            dash.cell(r, 5, round(float(sk), 1) if sk is not None else None)
        except Exception:
            dash.cell(r, 5, sk)
        dash.cell(r, 6, row.get("Shannon"))
        kel = row.get("Kelly")
        try:
            dash.cell(r, 7, round(float(kel) * 100, 1) if kel is not None else None)
        except Exception:
            dash.cell(r, 7, kel)
        dd = str(row.get("DD") or "")
        dash.cell(r, 8, dd[:36] if dd else "")
        mj = row.get("Marj")
        try:
            dash.cell(r, 9, round(float(mj), 3) if mj is not None else None)
        except Exception:
            dash.cell(r, 9, mj)
        fill = fill_kova.get(kova, yellow)
        for c in range(1, 10):
            dash.cell(r, c).fill = fill
            dash.cell(r, c).font = Font(size=9)
            dash.cell(r, c).alignment = Alignment(vertical="center")

    if not cand_rows:
        dash["A9"] = "Aday listesi boş — eşikleri kontrol edin"
        dash["A9"].font = Font(color=P["red"], italic=True)

    # Right panel: class distribution + weakest
    dash.merge_cells("J7:L7")
    dash["J7"] = "SINIF DAĞILIMI"
    dash["J7"].font = Font(bold=True, color=P["white"])
    dash["J7"].fill = PatternFill("solid", fgColor=P["navy"])
    dash["J8"] = "Sınıf"
    dash["K8"] = "Adet"
    dash["L8"] = "%"
    for c in ("J8", "K8", "L8"):
        dash[c].fill = PatternFill("solid", fgColor=P["primary"])
        dash[c].font = Font(bold=True, color=P["white"], size=9)

    if "Sınıf" in df.columns:
        dist = df["Sınıf"].astype(str).value_counts()
    elif "Not" in df.columns:
        dist = df["Not"].astype(str).value_counts()
    else:
        dist = pd.Series(dtype=int)
    for i, (name, count) in enumerate(dist.items()):
        rr = 9 + i
        dash.cell(rr, 10, name)
        dash.cell(rr, 11, int(count))
        dash.cell(rr, 12, round(100 * count / max(n, 1), 1))
        dash.cell(rr, 10).font = Font(size=9)
        dash.cell(rr, 11).font = Font(size=9)
        dash.cell(rr, 12).font = Font(size=9)

    # Bottom methodology strip
    method_row = 26
    dash.merge_cells(f"A{method_row}:L{method_row}")
    dash[f"A{method_row}"] = (
        "METODOLOJİ (tek bakış): Veri=İş Yatırım+8Ç XI_29 | Değer=DCF/RI/Graham/NCAV | "
        "Skor=Value↓ Quality↑ Growth(cap) Balance Cash Peer | Filtre=Güven≥60 + nakit/kalite + tuzak ele | "
        "Shannon+Kelly | 3 Kova=Öncelik/Şartlı/Bekle | Backtest sınırlı | Tavsiye değildir"
    )
    dash[f"A{method_row}"].font = Font(size=8, italic=True, color="455A64")
    dash[f"A{method_row}"].fill = PatternFill("solid", fgColor="ECEFF1")
    dash.row_dimensions[method_row].height = 32

    # Charts
    if cand_rows:
        n_bar = min(15, len(cand_rows))
        bar = BarChart()
        bar.type = "bar"
        bar.title = "Top Aday Skorları"
        bar.style = 10
        bar.y_axis.title = None
        bar.x_axis.title = "Skor"
        data_ref = Reference(dash, min_col=5, min_row=8, max_row=8 + n_bar)
        cats = Reference(dash, min_col=3, min_row=9, max_row=8 + n_bar)
        bar.add_data(data_ref, titles_from_data=True)
        bar.set_categories(cats)
        bar.shape = 4
        bar.height = 10
        bar.width = 14
        dash.add_chart(bar, "A28")

    if len(dist) > 0:
        pie = PieChart()
        pie.title = "Sınıf payı"
        pie.add_data(Reference(dash, min_col=11, min_row=8, max_row=8 + len(dist)), titles_from_data=True)
        pie.set_categories(Reference(dash, min_col=10, min_row=9, max_row=8 + len(dist)))
        pie.dataLabels = DataLabelList()
        pie.dataLabels.showPercent = True
        pie.dataLabels.showVal = False
        pie.dataLabels.showCatName = False
        pie.height = 8
        pie.width = 10
        dash.add_chart(pie, "H28")

    widths = {"A": 8, "B": 10, "C": 18, "D": 9, "E": 12, "F": 9, "G": 28, "H": 10,
              "I": 10, "J": 10, "K": 8, "L": 8}
    for k, w in widths.items():
        dash.column_dimensions[k].width = w

    # Quick nav note
    dash.merge_cells("A42:L42")
    dash["A42"] = "Sayfalar: Aday · DD · KAP Yeni İş · Model Sağlık · Shannon · Metodoloji · Değerleme · Hisse Analiz"
    dash["A42"].font = Font(size=9, color=P["navy"])


def build_methodology_sheet():
    """Tek sayfalık metodoloji notu (Excel sayfası)."""
    rows = [
        ("BAŞLIK", "BIST Değerleme Motoru v192 — Tek Sayfalık Metodoloji"),
        ("AMAÇ", "BIST hisselerini çok faktörlü skor + kural seti + DD + Shannon ile taramak. Al-sat sinyali değildir."),
        ("VERİ", "İş Yatırım temel alanlar + XI_29 MaliTablo (son 8 çeyrek TTM) + Yahoo fiyat (backtest)."),
        ("DEĞERLEME", "FCFF DCF (Bear/Base/Bull, ke spread) · Residual Income · Graham · NCAV · Kapitalize kazanç · Reverse DCF."),
        ("KE / WACC", "ke = rf + ERP + sektör primi (nominal TL) veya --ke; Bear +3pp, Bull -1.5pp; büyüme = (1+g_reel)(1+π)-1, terminal nominal g = (1+g_reel)(1+π_LT)-1; DCF: FCFF→WACC→(EV−net borç)/hisse."),
        ("SKOR", "Sektör ağırlıklı: Value↓ Quality↑ Growth(cap) Balance↑ Cash↑ Peer + Economic Quality; risk/güven çarpanı."),
        ("KALİBRASYON", "Saf çarpan ağırlığı düşük; FCF/OE/RevDCF yüksek; YoY>%50 growth cap; değer tuzağı cezası."),
        ("KALİTE", "Piotroski · Altman (banka/GYO hariç karar) · Sloan · Accrual · Beneish proxy."),
        ("ADAY KURALI", "Graham hard ∪ Buffett hard → GrahamBuffettScore; TeknikAlSkoru; Aday=0.65×temel+0.35×teknik; 1 ÖNCELİK=temel+teknik AL teyidi"),
        ("3 KOVA", "1 ÖNCELİK=NET+DD GÜÇLÜ+Kelly>0 | 2 ŞARTLI=DD iyi ama karışık/GYO | 3 BEKLE=Kelly≈0 veya gürültü — öncelik sırası, al sinyali değil"),
        ("KELLY", "Proxy=edge/unc² | Vol=m/σ² (Yahoo) | Historical=p,b (arşiv N≥30) | Final=hibrit×Shannon×half-Kelly, f_max=%25"),
        ("STOP", "Stop≈2σ_d√20 | Trailing≈2.5σ_d√20 | tek hisse tavan %8 | sektör %30"),
        ("TERS SİNYAL", "FS↔6A Spearman<0 ve/veya yüksek band <50 bandından zayıf → Kelly×0.5 + uyarı"),
        ("PORTFÖY", "Risk Parity + Markowitz (μ=edge, Σ=vol+ρ0.35) + Blended; short yok"),
        ("SHARPE", "Hisse: (μ-rf)/σ ve μ/σ; Portföy: aynı w ile. μ=edge proxy — tarihsel performans değil"),
        ("BÜTÜNLEŞİK", "DD+Graham(F/K,PD/DD,7 kriter,net-net,marj)+Skor+Shannon+Kelly+Sharpe+Value+Stop+Ters → 0-100; EVAL_W_*"),
        ("GRAHAM KRİTER", "G1 F/K≤15 | G2 PD/DD≤1.5 | G3 ürün≤22.5 | G4 Cari≥2 | G5 UB≤İS | G6 NK>0 | G7 FCF>0 + güvenlik marjı + NCAV"),
        ("SINIF", "GÜÇLÜ ADAY · İZLEME · PAHALI KALİTE · ELE"),
        ("DUE DILIGENCE", "1 Nakit  2 Borç  3 Kalite  4 İş modeli  5 Fiyat + Derin Analiz / Ana Risk / Tez"),
        ("SHANNON", "H = bileşen entropy (softmax); Shannon Güven; Kelly pay proxy ≤ %25; NET SİNYAL / KARIŞIK / GÜRÜLTÜ"),
        ("BACKTEST", "GEÇMİŞ 6/12/24 ay getirisi × bugünkü skor = look-ahead; ters sinyal cezası ve tarihsel Kelly v192'de KAPALI. Gerçek doğrulama: Performans Takibi (arşivlenmiş skor → sonradan gerçekleşen getiri)."),
        ("KULLANIM", "1 Tara  2 NET SİNYAL tercih  3 DD yeşil  4 Top15  5 KAP/dipnot  6 Kelly üst sınır gibi kullan"),
        ("UYARI", "Yatırım tavsiyesi değildir. Model hata ve veri boşlukları içerebilir (özellikle finansal kurumlar)."),
    ]
    return pd.DataFrame(rows, columns=["Başlık", "Açıklama"])


def create_stock_detail_sheet(wb):
    """'Hisse Analiz' sayfası: açılır listeden hisse seçilince, formüllerle
    (INDEX/MATCH) 'Çeyreklik Detay' ve 'Değerleme Özeti' sayfalarından veri
    çekilir; grafikler bu yerel hücrelere bağlı olduğundan seçim değiştikçe
    otomatik güncellenir. Makro/VBA gerekmez, düz .xlsx ile çalışır."""
    P = THEME
    if "Hisse Analiz" in wb.sheetnames:
        del wb["Hisse Analiz"]
    if "Değerleme Özeti" not in wb.sheetnames or "Çeyreklik Detay" not in wb.sheetnames:
        return

    val_sheet = wb["Değerleme Özeti"]
    det_sheet = wb["Çeyreklik Detay"]
    if val_sheet.max_row < 5 or det_sheet.max_row < 2:
        return

    ws = wb.create_sheet("Hisse Analiz", 1)
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = P["accent"]

    ws.merge_cells("A1:I1")
    ws["A1"] = "HİSSE BAZLI DETAY ANALİZ"
    ws["A1"].fill = PatternFill("solid", fgColor=P["dark"])
    ws["A1"].font = Font(name=P["title_font"], size=18, bold=True, color=P["white"])
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 36

    ws["A3"] = "Hisse Seç:"
    ws["A3"].font = Font(name=P["body_font"], bold=True, size=12)
    ws["B3"] = val_sheet.cell(5, 1).value
    ws["B3"].fill = PatternFill("solid", fgColor=P["stripe"])
    ws["B3"].font = Font(name=P["title_font"], size=14, bold=True, color=P["navy"])

    val_last = val_sheet.max_row
    dv = DataValidation(type="list", formula1=f"='Değerleme Özeti'!$A$5:$A${val_last}", allow_blank=True)
    dv.showInputMessage = True
    dv.showErrorMessage = True
    dv.prompt = "Açılır listeden hisse kodu seçin."
    dv.error = "Listeden bir hisse kodu seçin."
    ws.add_data_validation(dv)
    dv.add(ws["B3"])

    # Yardımcı hücre: seçili hissenin 'Çeyreklik Detay' sayfasındaki ilk (en yeni dönem) satırı.
    ws["A2"] = "İlk satır (yardımcı):"
    ws["A2"].font = Font(size=8, italic=True, color="999999")
    ws["M2"] = "=IFERROR(MATCH($B$3,'Çeyreklik Detay'!$A:$A,0),0)"
    ws["M2"].font = Font(size=8, color="CCCCCC")
    ws.column_dimensions["L"].width = 2

    # --- Özet metrik kutusu: 2 eksenli INDEX/MATCH (satır=hisse, sütun=başlık adı) ---
    # DÜZELTME: önceden sadece 14 alanla sınırlıydı; artık 'Değerleme Özeti' sayfasında
    # fiilen var olan TÜM sütunlar (91 metrik) bölüm başlıkları altında gösteriliyor.
    # Sıra/gruplama STOCK_DETAIL_SECTIONS'tan gelir; orada listelenmeyen (ileride
    # eklenebilecek) sütunlar otomatik olarak "DİĞER METRİKLER" bölümüne düşer, hiçbir
    # veri sessizce kaybolmaz. Format, "Değerleme Özeti" sayfasıyla aynı merkezi kurala
    # (PCT_COL_NAMES / RAW_PCT_COL_NAMES) göre belirlenir.
    all_field_names = [c.value for c in val_sheet[4] if c.value and c.value != "Hisse"]
    available = set(all_field_names)
    used = set()
    sections = []
    for title, fields in STOCK_DETAIL_SECTIONS:
        present = [f for f in fields if f in available]
        used.update(present)
        if present:
            sections.append((title, present))
    leftover = [f for f in all_field_names if f not in used]
    if leftover:
        sections.append(("DİĞER METRİKLER", leftover))

    ws["A4"] = "ÖZET METRİKLER (TÜM SÜTUNLAR)"
    ws.merge_cells("A4:F4")
    ws["A4"].fill = PatternFill("solid", fgColor=P["navy"])
    ws["A4"].font = Font(color=P["white"], bold=True)

    row = 4  # ilk bölüm başlığı, ÖZET METRİKLER bandının (A4) hemen altına (5. satır) gelsin
    col_pairs = [("A", "B"), ("C", "D"), ("E", "F")]
    for title, fields in sections:
        row += 1
        ws.cell(row, 1, title)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        ws.cell(row, 1).fill = PatternFill("solid", fgColor=P["primary"])
        ws.cell(row, 1).font = Font(color=P["white"], bold=True, size=10)
        row += 1
        for idx, label in enumerate(fields):
            lcol, vcol = col_pairs[idx % 3]
            if idx % 3 == 0 and idx > 0:
                row += 1
            lc = ws[f"{lcol}{row}"]
            lc.value = label
            lc.font = Font(name=P["body_font"], bold=True, color=P["navy"], size=9)
            lc.alignment = Alignment(wrap_text=True, vertical="center")
            vc = ws[f"{vcol}{row}"]
            vc.value = (
                f'=IFERROR(INDEX(\'Değerleme Özeti\'!$A:$ZZ,MATCH($B$3,\'Değerleme Özeti\'!$A:$A,0),'
                f'MATCH("{label}",\'Değerleme Özeti\'!$4:$4,0)),"")'
            )
            vc.fill = PatternFill("solid", fgColor=P["stripe"])
            if label in PCT_COL_NAMES:
                vc.number_format = "0.00%"
            elif label in RAW_PCT_COL_NAMES:
                vc.number_format = '#,##0.00"%";[Red]-#,##0.00"%"'
            else:
                vc.number_format = "#,##0.00;[Red]-#,##0.00"
        row += 1
    summary_end_row = row

    # --- Çeyreklik detay tablosu (kronolojik: eskiden yeniye, yukarıdan aşağıya) ---
    det_cols = ["B", "C", "D", "E", "F", "G", "H", "I", "J"]  # Çeyreklik Detay'daki sıra (Hisse=A hariç)
    headers = [
        "Dönem", "Hasılat (Çeyrek)", "Brüt Kâr (Çeyrek)", "Faaliyet Kârı (Çeyrek)",
        "Net Kâr (Çeyrek)", "Brüt Kar Marjı", "Faaliyet Kar Marjı", "Net Kar Marjı",
        "Altman Z (TTM, Yaklaşık)",
    ]
    title_row = summary_end_row + 2
    ws.cell(title_row, 1, "ÇEYREKLİK DETAY (Eskiden Yeniye)")
    ws.merge_cells(start_row=title_row, start_column=1, end_row=title_row, end_column=len(headers))
    ws.cell(title_row, 1).fill = PatternFill("solid", fgColor=P["navy"])
    ws.cell(title_row, 1).font = Font(color=P["white"], bold=True)

    hdr_row = title_row + 1
    for j, htext in enumerate(headers, 1):
        c = ws.cell(hdr_row, j, htext)
        c.fill = PatternFill("solid", fgColor=P["primary"])
        c.font = Font(color=P["white"], bold=True)
        c.alignment = Alignment(horizontal="center", wrap_text=True)

    n_periods = 8
    first_row = hdr_row + 1
    for t in range(n_periods):
        r = first_row + t
        offset = n_periods - 1 - t  # t=0(üst)->en eski(offset7); t=7(alt)->en yeni(offset0)
        for j, dcol in enumerate(det_cols, 1):
            ws.cell(r, j, f"=IF($M$2=0,\"\",IFERROR(INDEX('Çeyreklik Detay'!${dcol}:${dcol},$M$2+{offset}),\"\"))")
        for j in (6, 7, 8):
            ws.cell(r, j).number_format = "0.00%"
        for j in (2, 3, 4, 5, 9):
            ws.cell(r, j).number_format = "#,##0.00;[Red]-#,##0.00"
    last_row = first_row + n_periods - 1

    for j in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 17

    chart1 = LineChart()
    chart1.title = "Çeyreklik Kâr Kalemleri Trendi"
    chart1.y_axis.title = "TL"
    chart1.x_axis.title = "Dönem"
    chart1.add_data(Reference(ws, min_col=2, max_col=5, min_row=hdr_row, max_row=last_row), titles_from_data=True)
    chart1.set_categories(Reference(ws, min_col=1, min_row=first_row, max_row=last_row))
    chart1.height = 9; chart1.width = 20
    ws.add_chart(chart1, f"A{last_row + 3}")

    chart2 = LineChart()
    chart2.title = "Altman Z Trend (TTM, Yaklaşık)"
    chart2.y_axis.title = "Z Skoru"
    chart2.x_axis.title = "Dönem"
    chart2.add_data(Reference(ws, min_col=9, max_col=9, min_row=hdr_row, max_row=last_row), titles_from_data=True)
    chart2.set_categories(Reference(ws, min_col=1, min_row=first_row, max_row=last_row))
    chart2.height = 9; chart2.width = 20
    ws.add_chart(chart2, f"A{last_row + 22}")

    note_row = last_row + 40
    ws.cell(note_row, 1,
            "Not: Geçmiş dönem Altman Z hesaplarında güncel piyasa değeri kullanılır "
            "(geçmiş piyasa değeri toplanmadığı için); bu nedenle Z trendi yaklaşıktır, "
            "kesin tarihi değer olarak okunmamalıdır.")
    ws.cell(note_row, 1).font = Font(italic=True, size=9, color="666666")


def style(path):
    wb = load_workbook(path)
    P = THEME
    thin = Side("thin", color="E4E7EC")
    ws = wb["Değerleme Özeti"]

    ws.insert_rows(1, 3)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ws.max_column)
    ws["A1"] = "BIST DEĞERLEME v191 — GRAHAM+BUFFETT ∩ TEKNİK + EPV + FORWARD + KAP"
    ws["A1"].fill = PatternFill("solid", fgColor=P["dark"])
    ws["A1"].font = Font(name=P["title_font"], size=20, bold=True, color=P["white"])
    ws.row_dimensions[1].height = 42
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ws.max_column)
    ws["A2"] = f"İş Yatırım + 8 çeyrek finansal veri | {datetime.now():%d.%m.%Y %H:%M}"
    ws["A2"].fill = PatternFill("solid", fgColor=P["primary"])
    ws["A2"].font = Font(name=P["body_font"], color=P["white"])

    for sh in wb.worksheets:
        header = 4 if sh.title == "Değerleme Özeti" else 1
        sh.sheet_view.showGridLines = False
        sh.freeze_panes = "D5" if header == 4 else "A2"
        sh.sheet_properties.tabColor = P["primary"]
        for c in sh[header]:
            c.fill = PatternFill("solid", fgColor=P["navy"])
            c.font = Font(name=P["title_font"], bold=True, color=P["white"])
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for j in range(1, sh.max_column + 1):
            mx = max(len(str(sh.cell(r, j).value or "")) for r in range(1, min(sh.max_row, 250) + 1))
            sh.column_dimensions[get_column_letter(j)].width = min(max(mx + 2, 11), 31)
        sh.auto_filter.ref = f"A{header}:{get_column_letter(sh.max_column)}{sh.max_row}"
        if sh.max_row > header:
            name = "Tbl" + re.sub(r"[^A-Za-z0-9]", "", sh.title)
            tab = Table(displayName=name[:250], ref=f"A{header}:{get_column_letter(sh.max_column)}{sh.max_row}")
            tab.tableStyleInfo = TableStyleInfo(name="TableStyleMedium4", showRowStripes=True)
            sh.add_table(tab)

    h = {c.value: c.column for c in ws[4]}

    # DÜZELTME: "sadece rakam olanlar onluk ve binlik ayırıcı kullansın, 2 basamak"
    # isteği için tek bir merkezi kural: sütun ismine göre değil, hücrenin
    # gerçek değerine göre biçimlendirme yapılıyor (aşağıdaki genel döngüde,
    # PCT_COL_NAMES/RAW_PCT_COL_NAMES listeleri hariç HER sayısal hücreye
    # "#,##0.00" uygulanır). Bu listeler artık modül seviyesinde tanımlı
    # (bkz. dosyanın başı) ki "Hisse Analiz" sayfası da aynı kuralları kullansın.

    for name in ["Araştırma Skoru", "Risk Ayarlı Skor", "Güven Skoru"]:
        if name in h:
            col = get_column_letter(h[name])
            ws.conditional_formatting.add(
                f"{col}5:{col}{ws.max_row}",
                ColorScaleRule(
                    start_type="min", start_color="F8696B",
                    mid_type="percentile", mid_value=50, mid_color="FFEB84",
                    end_type="max", end_color="63BE7B"
                )
            )

    if "Değerleme Sınıfı" in h:
        colors_class = {
            "Derin İskonto": P["green"], "İskontolu": P["green"],
            "Makul": P["yellow"], "Pahalı": P["red"], "Hesaplanamadı": "7E7E7E"
        }
        for c in ws[get_column_letter(h["Değerleme Sınıfı"])][4:]:
            if c.value in colors_class:
                c.fill = PatternFill("solid", fgColor=colors_class[c.value])
                c.font = Font(bold=True, color=P["white"] if c.value != "Makul" else P["navy"])
                c.alignment = Alignment(horizontal="center")

    if "Değer Yatırımcılığı Sonucu" in h:
        colors_vi = {
            "İskontolu (Değer Yatırımcılığına Uygun)": P["green"],
            "İzlemeye Değer": P["yellow"],
            "Uygun Değil": P["red"],
            "Veri Yetersiz": "7E7E7E",
        }
        for c in ws[get_column_letter(h["Değer Yatırımcılığı Sonucu"])][4:]:
            if c.value in colors_vi:
                c.fill = PatternFill("solid", fgColor=colors_vi[c.value])
                c.font = Font(bold=True, color=P["white"] if c.value != "İzlemeye Değer" else P["navy"])
                c.alignment = Alignment(horizontal="center")

    
    if "Nakit Bazlı Ucuzluk Notu" in h:
        colors_cash = {
            "Net Nakit Pozisyonu Güçlü (Piyasa Değerinin >%30'u)": P["green"],
            "Net Nakit Pozitif": P["yellow"],
            "Net Borçlu": "7E7E7E",
        }
        for c in ws[get_column_letter(h["Nakit Bazlı Ucuzluk Notu"])][4:]:
            if c.value in colors_cash:
                c.fill = PatternFill("solid", fgColor=colors_cash[c.value])
                c.font = Font(bold=True, color=P["white"] if c.value != "Net Nakit Pozitif" else P["navy"])
                c.alignment = Alignment(horizontal="center")

    if "Uyum Kadranı" in h:
        colors_kad = {
            "GÜÇLÜ UYUM (Temel↑ Teknik↑)": P["green"],
            "ERKEN BİRİKİM (Temel↑ Teknik↓ — henüz dönmedi)": P["yellow"],
            "MOMENTUM RİSKİ (Temel↓ Teknik↑ — dikkatli)": P["orange"] if "orange" in P else P["yellow"],
            "KAÇIN (Temel↓ Teknik↓)": P["red"],
        }
        for c in ws[get_column_letter(h["Uyum Kadranı"])][4:]:
            if c.value in colors_kad:
                c.fill = PatternFill("solid", fgColor=colors_kad[c.value])
                c.font = Font(bold=True, color=P["white"] if colors_kad[c.value] not in (P["yellow"],) else P["navy"])
                c.alignment = Alignment(horizontal="center")

    if "Teknik Sinyal" in h:
        colors_tek = {
            "AL (Trend+Momentum Uyumlu)": P["green"],
            "İZLE (Kısmi Teyit)": P["yellow"],
            "BEKLE (Düşüş Trendi)": P["red"],
            "NÖTR": "7E7E7E",
            "Veri Yetersiz": "7E7E7E",
        }
        for c in ws[get_column_letter(h["Teknik Sinyal"])][4:]:
            if c.value in colors_tek:
                c.fill = PatternFill("solid", fgColor=colors_tek[c.value])
                c.font = Font(bold=True, color=P["white"] if c.value != "İZLE (Kısmi Teyit)" else P["navy"])
                c.alignment = Alignment(horizontal="center")

    if "Net-Net (NCAV) Durumu" in h:
        for c in ws[get_column_letter(h["Net-Net (NCAV) Durumu"])][4:]:
            if c.value == "Net-Net (Derin Değer)":
                c.fill = PatternFill("solid", fgColor=P["accent"])
                c.font = Font(bold=True, color=P["white"])
                c.alignment = Alignment(horizontal="center")

    for sh in wb.worksheets:
        if sh.title == "Hisse Analiz":
            continue  # formüllerle kendi biçimini alacak, aşağıda ayrıca işlenir.
        hdr = 4 if sh.title == "Değerleme Özeti" else 1
        sh_headers = {c.value: c.column for c in sh[hdr]}
        pct_letters = {get_column_letter(col) for name, col in sh_headers.items() if name in PCT_COL_NAMES}
        raw_pct_letters = {get_column_letter(col) for name, col in sh_headers.items() if name in RAW_PCT_COL_NAMES}
        for row in sh.iter_rows(min_row=hdr + 1):
            for cell in row:
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                    if cell.column_letter in pct_letters:
                        cell.number_format = "0.00%"
                    elif cell.column_letter in raw_pct_letters:
                        cell.number_format = '#,##0.00"%";[Red]-#,##0.00"%"'
                    else:
                        cell.number_format = "#,##0.00;[Red]-#,##0.00"

    # v12: KAP Yeni İş — KAP Link kolonunu tıklanabilir hyperlink yap
    try:
        if "KAP Yeni İş" in wb.sheetnames:
            ks = wb["KAP Yeni İş"]
            hdr = {c.value: c.column for c in ks[1] if c.value}
            link_col = hdr.get("KAP Link")
            if link_col:
                letter = get_column_letter(link_col)
                for row in range(2, ks.max_row + 1):
                    cell = ks[f"{letter}{row}"]
                    url = str(cell.value or "").strip()
                    if url.startswith("http"):
                        cell.hyperlink = url
                        cell.style = "Hyperlink"
                        cell.font = Font(name=THEME.get("body_font", "Aptos"), color="0563C1", underline="single")
                ks.column_dimensions[letter].width = 42
            # tutar / özet geniş
            for name, w in (("Özet", 48), ("Başlık", 36), ("Şirket", 28), ("Sözleşme Tutarı", 16)):
                if name in hdr:
                    ks.column_dimensions[get_column_letter(hdr[name])].width = w
    except Exception as _kap_style_e:
        print("KAP Yeni İş stil:", _kap_style_e, flush=True)

    create_dashboard(wb)
    try:
        _style_candidate_sheet(wb)
    except Exception as _e:
        print('Aday Listesi stil:', _e, flush=True)
    try:
        _style_value_sheet(wb)
    except Exception as _e:
        print('Değer Yatırımı stil:', _e, flush=True)
    for _key, _title, _desc, _cols, _pct, _dec in [
        ("Magic Formula", "MAGIC FORMULA (GREENBLATT) — 15 HİSSE",
         "Kazanç getirisi (EBIT/EV) ve sermaye getirisi (ROIC) sıralamalarının toplamı en düşük (en iyi) 15 hisse. Sektör başına ≤2 (tamamlamada ≤3).",
         MAGIC_SHEET_COLS, {"Kazanç Getirisi (EBIT/EV)", "Adil Değer Marjı"}, {"EV/EBIT", "ROIC"}),
        ("Momentum", "MOMENTUM — 15 HİSSE",
         "Son 6/12 ay fiyat getirisinin yüzdelik sırası, volatiliteye göre hafif ayarlanmış (%50 6A + %35 12A + %15 düşük-vol). Değer filtresi YOKTUR; yalnızca 'Çok Pahalı' hariç tutulur. Getiriler GEÇMİŞ performanstır, gelecek getiri garantisi vermez.",
         MOMENTUM_SHEET_COLS, {"6A Getiri", "12A Getiri", "Vol Ann (σ)"}, {"Momentum Skoru"}),
        ("Temettü", "TEMETTÜ / GELİR YATIRIMI — 15 HİSSE",
         f"Temettü verimi ≥ %{DIVIDEND_MIN_YIELD*100:.0f} (nakit akış tablosundaki gerçek 'Ödenen Temettüler' kaleminden) ve son 8 çeyreğin en az {DIVIDEND_MIN_QUARTERS_PAID}'ünde ödeme. Gelir Puanı: verim %55 + ROIC yüzdelik %25 + Piotroski yüzdelik %20 (yüksek verim ama düşük kalite cezalandırılır).",
         DIVIDEND_SHEET_COLS, {"Temettü Verimi", "Temettü Ödeme Oranı", "Adil Değer Marjı", "ROIC"}, {}),
        ("Düşük Volatilite", "DÜŞÜK VOLATİLİTE — 15 HİSSE",
         "En düşük yıllık volatiliteli 15 hisse (Piotroski≥4 ve 'Çok Pahalı' hariç dışında filtre yok). Değer taraması DEĞİLDİR; düşük-volatilite anomalisine (düşük riskli hisselerin risk-ayarlı bazda genelde daha iyi performans göstermesi) dayanır.",
         LOWVOL_SHEET_COLS, {"Vol Ann (σ)", "Volatilite Yüzdelik", "Adil Değer Marjı", "ROE"}, {}),
        ("Sektöre Göre Ucuz", "SEKTÖRE GÖRE UCUZLUK (GÖRELİ DEĞERLEME) — 15 HİSSE",
         f"F/K, PD/DD, EV/EBIT mutlak eşik yerine SEKTÖR İÇİ yüzdelik sıraya göre değerlendirilir (bankacılık gibi doğal düşük çarpanlı sektörlerin haksız avantajını, teknoloji gibi yüksek çarpanlı sektörlerin haksız elenmesini önler). Yalnızca ≥{SECTOR_REL_MIN_MEMBERS} üyeli sektörler dahil.",
         SECTORREL_SHEET_COLS, {"Adil Değer Marjı"}, {"F/K", "PD/DD", "EV/EBIT", "Sektör İçi Ucuzluk Skoru"}),
    ]:
        try:
            _style_generic_list_sheet(wb, _key, _title, _desc, _cols, pct_cols=_pct, dec_cols=_dec)
        except Exception as _e:
            print(f'{_key} stil:', _e, flush=True)
    try:
        _style_generic_list_sheet(
            wb, "GYO NAV", "GYO NAV MODELİ (DENEYSEL)",
            "NAV/Hisse = Yatırım Amaçlı Gayrimenkuller (gerçeğe uygun değer) + Nakit − Toplam Yükümlülükler, hisseye bölünmüş. "
            "IFRS'te yatırım amaçlı gayrimenkuller zaten gerçeğe uygun değerle taşındığı için bu, Defter Değeri/Hisse'den farklıdır — "
            "diğer varlık/yükümlülük kalemlerini izole eder. DENEYSELDİR; kalem eşleşmesi doğrulanmadan güvenilir sayılmamalıdır. "
            "En negatif iskonto = NAV'a göre en ucuz. Aday listelerinden bağımsızdır (GYO'lar oradan hariç tutulur).",
            GYO_SHEET_COLS, pct_cols={"GYO NAV İskonto/Primi"}, dec_cols={"Fiyat", "GYO NAV/Hisse (Deneysel)", "Defter Değeri/Hisse", "PD/DD", "Net Borç/FAVÖK"})
    except Exception as _e:
        print('GYO NAV stil:', _e, flush=True)
    try:
        _style_generic_list_sheet(
            wb, "Konsensüs", "ÇOKLU LİSTE KONSENSÜSÜ",
            f"Değer Yatırımı, Magic Formula, Momentum, Temettü, Düşük Volatilite ve Sektöre Göre Ucuz listelerinden en az "
            f"{CONSENSUS_MIN_LISTS}'sinde birlikte geçen hisseler. Farklı stratejilerin AYNI ANDA işaret ettiği hisse, tek "
            "bir yönteme dayanmaktan daha güçlü bir sinyal taşır — ama bu listenin kendi eleme kriteri yoktur, tamamen alt "
            "listelerin kesişimidir. Ort. Sıra Yüzdeliği: bir hissenin geçtiği listelerdeki ortalama konumu (100=hep 1.).",
            CONSENSUS_SHEET_COLS, pct_cols=set(), dec_cols={"Ort. Sıra Yüzdeliği"})
    except Exception as _e:
        print('Konsensüs stil:', _e, flush=True)
    create_stock_detail_sheet(wb)
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True
    wb.calculation.calcMode = "auto"
    wb.save(path)
    wb.close()


def fetch_yahoo_history(session, code, lookback_days=800):
    """Tek Yahoo chart çağrısı: adjclose serisi (eskiden yeniye).
    6A/12A/24A getiri + vol aynı seriden türetilir (4 HTTP → 1).
    """
    try:
        import time
        now = int(time.time())
        start = now - int(lookback_days * 86400)
        u = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.IS"
            f"?period1={start}&period2={now}&interval=1d&events=history&includeAdjustedClose=true"
        )
        rr = session.get(u, headers=HEAD, timeout=BACKTEST_TIMEOUT)
        rr.raise_for_status()
        a = [x for x in rr.json()["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"] if x is not None]
        if len(a) < 20:
            return None
        return np.asarray(a, dtype=float)
    except Exception:
        return None


def _return_from_series(px, days):
    """~days takvim günü geriye yaklaşık getiri (iş günü ~ days*0.7)."""
    if px is None or len(px) < 20:
        return np.nan
    n = len(px)
    # iş günü yaklaşık
    bars = max(15, int(days * 0.7))
    if n <= bars:
        if px[0] == 0:
            return np.nan
        return float(px[-1] / px[0] - 1.0)
    p0 = px[-(bars + 1)]
    if not np.isfinite(p0) or p0 == 0:
        return np.nan
    return float(px[-1] / p0 - 1.0)


def _vol_from_series(px, lookback=None):
    lookback = lookback or VOL_LOOKBACK_DAYS
    if px is None or len(px) < 40:
        return np.nan, (float(px[-1]) if px is not None and len(px) else np.nan)
    tail = px[-min(len(px), int(lookback * 1.2)):]
    if len(tail) < 40:
        return np.nan, float(px[-1])
    rets = np.diff(np.log(tail))
    rets = rets[np.isfinite(rets)]
    if len(rets) < 30:
        return np.nan, float(px[-1])
    sig = float(np.std(rets, ddof=1) * np.sqrt(252))
    return sig, float(px[-1])


def fetch_backtest_return(session, code, days):
    """Geriye dönük uyumluluk: tek horizon."""
    px = fetch_yahoo_history(session, code, lookback_days=max(900, int(days * 1.2)))
    return _return_from_series(px, days)


def fetch_price_vol(session, code, lookback_days=None):
    px = fetch_yahoo_history(session, code, lookback_days=max(400, int((lookback_days or VOL_LOOKBACK_DAYS) * 2)))
    return _vol_from_series(px, lookback_days or VOL_LOOKBACK_DAYS)


def fetch_yahoo_bundle(session, code):
    """Tek istek: r6, r12, r24, sigma, last."""
    px = fetch_yahoo_history(session, code, lookback_days=900)
    if px is None:
        return np.nan, np.nan, np.nan, np.nan, np.nan
    r6 = _return_from_series(px, 180)
    r12 = _return_from_series(px, 365)
    r24 = _return_from_series(px, 730)
    sig, last = _vol_from_series(px)
    return r6, r12, r24, sig, last


def fetch_yahoo_ohlcv(session, code, lookback_days=300):
    """Tek Yahoo chart çağrısı: OHLCV serisi (teknik analiz indikatörleri için).
    Not: ham (adjust edilmemiş) open/high/low/close/volume kullanılır. Nadir
    sermaye artırımı/temettü düzeltmesi RSI/CCI/Supertrend gibi kısa-orta vadeli
    indikatörlerde pratikte ihmal edilebilir düzeydedir; adjclose bazlı getiri
    hesapları için ayrıca fetch_yahoo_history kullanılmaya devam eder.
    """
    try:
        import time
        now = int(time.time())
        start = now - int(lookback_days * 86400)
        u = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.IS"
            f"?period1={start}&period2={now}&interval=1d&events=history"
        )
        rr = session.get(u, headers=HEAD, timeout=BACKTEST_TIMEOUT)
        rr.raise_for_status()
        res = rr.json()["chart"]["result"][0]
        ts = np.asarray(res.get("timestamp", []), dtype="int64")
        q = res["indicators"]["quote"][0]
        o = np.asarray(q.get("open", []), dtype=float)
        h = np.asarray(q.get("high", []), dtype=float)
        l = np.asarray(q.get("low", []), dtype=float)
        c = np.asarray(q.get("close", []), dtype=float)
        v = np.asarray(q.get("volume", []), dtype=float)
        n = min(len(ts), len(o), len(h), len(l), len(c), len(v))
        if n < 40:
            return None
        ts, o, h, l, c, v = ts[:n], o[:n], h[:n], l[:n], c[:n], v[:n]
        idx = pd.to_datetime(ts, unit="s")
        df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)
        if df["close"].notna().sum() < 40:
            return None
        _px = ["open", "high", "low", "close"]
        df[_px] = df[_px].ffill().bfill()
        df["volume"] = df["volume"].fillna(0.0)   # v192.1: eksik hacim = işlem yok (ffill hacmi şişirirdi)
        return df
    except Exception:
        return None


def compute_liquidity(ohlcv, window=None):
    """Likidite metrikleri (TL): ortalama/medyan günlük işlem hacmi, işlem görmeyen gün oranı,
    1 günde tahliye kapasitesi (medyan hacmin %LIQ_EXIT_PARTICIPATION'ı)."""
    try:
        w = LIQ_WINDOW if window is None else window
        if ohlcv is None or len(ohlcv) < 20:
            return {}
        d = ohlcv.tail(w)
        turn = (d["close"].astype(float) * d["volume"].astype(float))
        med = float(turn.median())
        return {
            "Ort. Günlük Hacim (TL)": float(turn.mean()),
            "Medyan Günlük Hacim (TL)": med,
            "İşlemsiz Gün Oranı": float((d["volume"] <= 0).mean()),
            "1 Gün Tahliye Kapasitesi (TL)": med * LIQ_EXIT_PARTICIPATION,
        }
    except Exception:
        return {}


def liquidity_note(med, zero_ratio):
    if med is None or pd.isna(med):
        return "Veri Yok"
    if pd.notna(zero_ratio) and zero_ratio > LIQ_MAX_ZERO_DAYS:
        return "Düşük (sık işlem görmüyor)"
    if med >= LIQ_HIGH_TURNOVER_TL:
        return "Yüksek"
    if med >= LIQ_MIN_TURNOVER_TL:
        return "Orta"
    if med >= LIQ_MIN_TURNOVER_TL / 4:
        return "Düşük"
    return "Çok Düşük"


def calc_rsi(close, period=14):
    close = pd.Series(close).astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_atr(high, low, close, period=14):
    high, low, close = pd.Series(high, dtype=float), pd.Series(low, dtype=float), pd.Series(close, dtype=float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def calc_cci(high, low, close, period=20):
    high, low, close = pd.Series(high, dtype=float), pd.Series(low, dtype=float), pd.Series(close, dtype=float)
    tp = (high + low + close) / 3.0
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - sma) / (0.015 * mad.replace(0, np.nan))


def calc_supertrend(high, low, close, period=10, mult=3.0):
    high, low, close = pd.Series(high, dtype=float), pd.Series(low, dtype=float), pd.Series(close, dtype=float)
    atr = calc_atr(high, low, close, period)
    hl2 = (high + low) / 2.0
    upperband = hl2 + mult * atr
    lowerband = hl2 - mult * atr
    n = len(close)
    st_dir = np.ones(n)
    final_upper = upperband.copy()
    final_lower = lowerband.copy()
    for i in range(1, n):
        if pd.notna(close.iloc[i - 1]) and close.iloc[i - 1] <= final_upper.iloc[i - 1]:
            final_upper.iloc[i] = min(upperband.iloc[i], final_upper.iloc[i - 1]) if pd.notna(final_upper.iloc[i - 1]) else upperband.iloc[i]
        else:
            final_upper.iloc[i] = upperband.iloc[i]
        if pd.notna(close.iloc[i - 1]) and close.iloc[i - 1] >= final_lower.iloc[i - 1]:
            final_lower.iloc[i] = max(lowerband.iloc[i], final_lower.iloc[i - 1]) if pd.notna(final_lower.iloc[i - 1]) else lowerband.iloc[i]
        else:
            final_lower.iloc[i] = lowerband.iloc[i]
        if pd.notna(close.iloc[i]) and pd.notna(final_upper.iloc[i - 1]) and close.iloc[i] > final_upper.iloc[i - 1]:
            st_dir[i] = 1
        elif pd.notna(close.iloc[i]) and pd.notna(final_lower.iloc[i - 1]) and close.iloc[i] < final_lower.iloc[i - 1]:
            st_dir[i] = -1
        else:
            st_dir[i] = st_dir[i - 1]
    st_dir = pd.Series(st_dir, index=close.index)
    # Aktif seviye: yükselişte alt bant (destek/trailing-stop), düşüşte üst bant (direnç)
    st_level = pd.Series(np.where(st_dir > 0, final_lower.values, final_upper.values), index=close.index)
    return st_dir, st_level


def calc_macd(close, fast=12, slow=26, signal=9):
    close = pd.Series(close, dtype=float)
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def calc_bollinger(close, period=20, mult=2.0):
    close = pd.Series(close, dtype=float)
    sma = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = sma + mult * std
    lower = sma - mult * std
    pctb = (close - lower) / (upper - lower).replace(0, np.nan)
    return sma, upper, lower, pctb


def calc_adx(high, low, close, period=14):
    high, low, close = pd.Series(high, dtype=float), pd.Series(low, dtype=float), pd.Series(close, dtype=float)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr = calc_atr(high, low, close, period)
    plus_di = 100 * pd.Series(plus_dm, index=close.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=close.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean(), plus_di, minus_di


def compute_technical_signals(ohlcv):
    """OHLCV DataFrame'den kompozit Teknik Skor (0-100) + AL/BEKLE sinyali üretir.
    Ağırlıklar: Supertrend %30 (trend filtresi), MACD %25, RSI %20, CCI %15,
    Hacim teyidi %10. ADX<20 ise (zayıf/yatay trend) skor %40 azaltılır.
    Tek başına al-sat sinyali değildir; fundamental adaylıkla birlikte okunmalıdır.
    """
    if ohlcv is None or len(ohlcv) < 40:
        return None
    h, l, c, v = ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"]
    rsi = calc_rsi(c, 14)
    cci = calc_cci(h, l, c, 20)
    st_dir, st_level = calc_supertrend(h, l, c, 10, 3.0)
    macd_line, macd_sig, macd_hist = calc_macd(c, 12, 26, 9)
    bb_sma, bb_up, bb_lo, pctb = calc_bollinger(c, 20, 2.0)
    adx, _, _ = calc_adx(h, l, c, 14)
    atr14 = calc_atr(h, l, c, 14)
    sma50 = c.rolling(50).mean()
    vol_sma20 = v.rolling(20).mean()

    i = len(c) - 1

    def _v(s, k=0):
        j = i - k
        if j < 0 or j >= len(s):
            return np.nan
        x = s.iloc[j]
        return float(x) if pd.notna(x) else np.nan

    rsi_v, rsi_prev = _v(rsi), _v(rsi, 1)
    cci_v, cci_prev = _v(cci), _v(cci, 1)
    st_v, st_prev = _v(st_dir), _v(st_dir, 1)
    st_flip_up = pd.notna(st_prev) and pd.notna(st_v) and st_prev < 0 and st_v > 0
    macd_h, macd_h_prev = _v(macd_hist), _v(macd_hist, 1)
    macd_cross_up = pd.notna(macd_h) and pd.notna(macd_h_prev) and macd_h_prev <= 0 and macd_h > 0
    pctb_v = _v(pctb)
    adx_v = _v(adx)
    price_v = _v(c)
    sma50_v = _v(sma50)
    vol_v = _v(v)
    vol_sma_v = _v(vol_sma20)
    st_level_v = _v(st_level)
    bb_up_v = _v(bb_up)
    bb_lo_v = _v(bb_lo)
    atr_v = _v(atr14)
    vol_confirm = pd.notna(vol_v) and pd.notna(vol_sma_v) and vol_sma_v > 0 and vol_v >= 1.2 * vol_sma_v

    parts = {}
    parts["Supertrend"] = 1.0 if pd.notna(st_v) and st_v > 0 else 0.0
    if pd.notna(macd_h):
        parts["MACD"] = 1.0 if macd_cross_up else (0.6 if macd_h > 0 else 0.0)
    if pd.notna(rsi_v):
        if rsi_v >= 70:
            parts["RSI"] = 0.3
        elif 30 <= rsi_v <= 55 and pd.notna(rsi_prev) and rsi_v > rsi_prev:
            parts["RSI"] = 1.0
        elif rsi_v > 50:
            parts["RSI"] = 0.6
        else:
            parts["RSI"] = 0.3
    if pd.notna(cci_v):
        if pd.notna(cci_prev) and cci_prev <= -100 and cci_v > -100:
            parts["CCI"] = 1.0
        elif cci_v > 100:
            parts["CCI"] = 0.4
        elif cci_v > 0:
            parts["CCI"] = 0.6
        else:
            parts["CCI"] = 0.3
    parts["Hacim"] = 1.0 if vol_confirm else 0.4

    wmap = {"Supertrend": 0.30, "MACD": 0.25, "RSI": 0.20, "CCI": 0.15, "Hacim": 0.10}
    num = den = 0.0
    for k, w in wmap.items():
        val = parts.get(k)
        if val is not None and np.isfinite(val):
            num += w * val
            den += w
    teknik_skor = 100.0 * num / den if den > 1e-9 else np.nan
    if pd.notna(adx_v) and adx_v < 20 and np.isfinite(teknik_skor):
        teknik_skor *= 0.6

    trend_durum = "Veri Yok"
    if pd.notna(sma50_v) and pd.notna(price_v):
        if price_v > sma50_v:
            trend_durum = "Yükseliş (Fiyat>SMA50)"
        elif price_v < sma50_v:
            trend_durum = "Düşüş (Fiyat<SMA50)"
        else:
            trend_durum = "Yatay"

    if np.isfinite(teknik_skor):
        if teknik_skor >= 70 and pd.notna(st_v) and st_v > 0:
            sinyal = "AL (Trend+Momentum Uyumlu)"
        elif teknik_skor >= 55:
            sinyal = "İZLE (Kısmi Teyit)"
        elif pd.notna(st_v) and st_v < 0:
            sinyal = "BEKLE (Düşüş Trendi)"
        else:
            sinyal = "NÖTR"
    else:
        sinyal = "Veri Yetersiz"

    return {
        "RSI(14)": round(rsi_v, 1) if np.isfinite(rsi_v) else np.nan,
        "CCI(20)": round(cci_v, 1) if np.isfinite(cci_v) else np.nan,
        "ADX(14)": round(adx_v, 1) if np.isfinite(adx_v) else np.nan,
        "MACD Histogram": round(macd_h, 4) if np.isfinite(macd_h) else np.nan,
        "Supertrend Yön": "Yükseliş" if (pd.notna(st_v) and st_v > 0) else ("Düşüş" if pd.notna(st_v) else np.nan),
        "Supertrend Dönüş": "Evet (Yeni AL)" if st_flip_up else "Hayır",
        "Supertrend Seviye": round(st_level_v, 2) if np.isfinite(st_level_v) else np.nan,
        "Bollinger %B": round(pctb_v, 2) if np.isfinite(pctb_v) else np.nan,
        "Bollinger Üst": round(bb_up_v, 2) if np.isfinite(bb_up_v) else np.nan,
        "Bollinger Alt": round(bb_lo_v, 2) if np.isfinite(bb_lo_v) else np.nan,
        "ATR(14)": round(atr_v, 2) if np.isfinite(atr_v) else np.nan,
        "SMA50 (Teknik)": round(sma50_v, 2) if np.isfinite(sma50_v) else np.nan,
        "Son Kapanış (Yahoo)": round(price_v, 2) if np.isfinite(price_v) else np.nan,
        "Trend Durumu (SMA50)": trend_durum,
        "Hacim Teyidi": "Evet" if vol_confirm else "Hayır",
        "Teknik Skor": round(teknik_skor, 1) if np.isfinite(teknik_skor) else np.nan,
        "Teknik Sinyal": sinyal,
    }


def compute_weekly_confirmation(ohlcv):
    """Günlük OHLCV'yi gerçek takvim haftalarına (Pazartesi-Cuma) resample edip
    haftalık Supertrend yönü + RSI hesaplar — çoklu zaman dilimi teyidi için.
    Ekstra API çağrısı gerektirmez, aynı günlük veriden türetilir."""
    if ohlcv is None or len(ohlcv) < 60 or not isinstance(ohlcv.index, pd.DatetimeIndex):
        return {"Haftalık Trend": np.nan, "Haftalık RSI": np.nan}
    try:
        wk = ohlcv.resample("W-FRI").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna(subset=["close"])
    except Exception:
        return {"Haftalık Trend": np.nan, "Haftalık RSI": np.nan}
    if len(wk) < 25:
        return {"Haftalık Trend": np.nan, "Haftalık RSI": np.nan}
    st_dir, _ = calc_supertrend(wk["high"], wk["low"], wk["close"], 10, 3.0)
    rsi_w = calc_rsi(wk["close"], 14)
    st_v = st_dir.iloc[-1]
    rsi_v = rsi_w.iloc[-1]
    return {
        "Haftalık Trend": "Yükseliş" if st_v > 0 else "Düşüş",
        "Haftalık RSI": round(float(rsi_v), 1) if pd.notna(rsi_v) else np.nan,
    }


def fetch_market_regime(session):
    """XU100 endeksinin SMA50/SMA200 konumuna göre piyasa rejimini tespit eder.
    RISK-OFF rejiminde teknik sinyaller çok daha fazla yalancı-pozitif üretir;
    bu yüzden confluence ağırlıkları ve pozisyon büyüklüğü buna göre ayarlanır.
    Tek seferlik çağrı — hisse başına tekrar edilmez."""
    try:
        ohlcv = fetch_yahoo_ohlcv(session, "XU100", lookback_days=420)
        if ohlcv is None or len(ohlcv) < 210:
            return {"Rejim": "Bilinmiyor", "XU100 Fiyat": np.nan, "XU100 SMA50": np.nan,
                     "XU100 SMA200": np.nan, "Rejim Çarpanı": 1.0}
        c = ohlcv["close"]
        sma50_v = float(c.rolling(50).mean().iloc[-1])
        sma200_v = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else np.nan
        price_v = float(c.iloc[-1])
        if pd.notna(sma200_v):
            if price_v > sma50_v > sma200_v:
                rejim, carpan = "RISK-ON (Güçlü Yükseliş)", 1.10
            elif price_v > sma200_v:
                rejim, carpan = "RISK-ON (Ilımlı Yükseliş)", 1.0
            elif price_v < sma50_v < sma200_v:
                rejim, carpan = "RISK-OFF (Düşüş Trendi)", 0.65
            else:
                rejim, carpan = "NÖTR (Yatay/Karışık)", 0.85
        else:
            rejim, carpan = "Bilinmiyor", 1.0
        return {"Rejim": rejim, "XU100 Fiyat": round(price_v, 2),
                 "XU100 SMA50": round(sma50_v, 2) if pd.notna(sma50_v) else np.nan,
                 "XU100 SMA200": round(sma200_v, 2) if pd.notna(sma200_v) else np.nan,
                 "Rejim Çarpanı": carpan}
    except Exception:
        return {"Rejim": "Bilinmiyor", "XU100 Fiyat": np.nan, "XU100 SMA50": np.nan,
                 "XU100 SMA200": np.nan, "Rejim Çarpanı": 1.0}


def fetch_tradingview_rating(session, code):
    """TradingView'ın herkese açık (resmi olmayan) tarayıcı uç noktasından
    genel Teknik Görünüm (Recommend.All, -1..+1) çeker.

    UYARI: Resmi bir API değildir, TradingView'ın Kullanım Şartları'na
    tabidir ve TradingView bu uç noktayı önceden haber vermeden
    değiştirebilir/kapatabilir. Kendi hesapladığımız Teknik Skor'un yerine
    geçmez — sadece bağımsız bir çapraz kontrol/ikinci görüş sağlar. Hata
    durumunda sessizce None döner, ana rapor akışını etkilemez.
    """
    try:
        url = "https://scanner.tradingview.com/turkey/scan"
        payload = {
            "symbols": {"tickers": [f"BIST:{code}"], "query": {"types": []}},
            "columns": ["Recommend.All", "RSI"],
        }
        rr = session.post(url, json=payload, headers=HEAD, timeout=BACKTEST_TIMEOUT)
        rr.raise_for_status()
        data = rr.json().get("data", [])
        if not data:
            return None
        d = data[0].get("d", [])
        if len(d) < 2:
            return None
        rec_all, rsi_tv = d[0], d[1]

        def _label(x):
            if x is None:
                return np.nan
            x = float(x)
            if x >= 0.5:
                return "GÜÇLÜ AL"
            if x >= 0.1:
                return "AL"
            if x <= -0.5:
                return "GÜÇLÜ SAT"
            if x <= -0.1:
                return "SAT"
            return "NÖTR"

        return {
            "TV Görünüm": _label(rec_all),
            "TV Recommend.All": round(float(rec_all), 3) if rec_all is not None else np.nan,
            "TV RSI": round(float(rsi_tv), 1) if rsi_tv is not None else np.nan,
        }
    except Exception:
        return None


def enrich_tradingview_crosscheck(df, session):
    """Kendi Teknik Skor/Sinyal'imizi TradingView'ın (resmi olmayan) genel
    görünümüyle çapraz kontrol eder: 'TV Görünüm', 'TV Recommend.All',
    'TV RSI', 'TV Uyum Notu'. Kaynak tamamen erişilemezse (blok/uç nokta
    değişikliği) sessizce atlanır — ENABLE_TRADINGVIEW_CROSSCHECK=False ile
    de tamamen kapatılabilir.
    """
    if not ENABLE_TRADINGVIEW_CROSSCHECK:
        return df
    if df is None or df.empty or "Hisse" not in df.columns:
        return df
    codes = df["Hisse"].tolist()
    results = {}
    ok = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_tradingview_rating, session, c): c for c in codes}
        for fut, c in futs.items():
            try:
                r = fut.result()
                results[c] = r
                if r:
                    ok += 1
            except Exception:
                results[c] = None
    if ok == 0:
        print("TradingView çapraz kontrolü atlandı (kaynak erişilemedi).", flush=True)
        return df

    for col in ["TV Görünüm", "TV Recommend.All", "TV RSI"]:
        df[col] = [(results.get(c) or {}).get(col, np.nan) for c in codes]

    def _uyum(row):
        tv = row.get("TV Görünüm")
        if pd.isna(tv):
            return "TV Verisi Yok"
        own = str(row.get("Teknik Sinyal") or "")
        own_bull = own.startswith("AL")
        own_bear = own.startswith("BEKLE")
        tv_bull = tv in ("AL", "GÜÇLÜ AL")
        tv_bear = tv in ("SAT", "GÜÇLÜ SAT")
        if own_bull and tv_bull:
            return "Teyitli (İkisi de AL yönünde)"
        if own_bear and tv_bear:
            return "Teyitli (İkisi de SAT yönünde)"
        if (own_bull and tv_bear) or (own_bear and tv_bull):
            return "ÇELİŞKİ (Kaynaklar Ayrışıyor — Dikkat)"
        return "Kısmi Uyum"
    df["TV Uyum Notu"] = df.apply(_uyum, axis=1)
    return df


def compute_extended_technical(ohlcv, xu_close=None):
    """v192.10 ek teknik göstergeler (compute_technical_signals'tan bağımsız, mevcut davranışı bozmamak için):
    - Endekse göre göreli güç (RS): hisse/XU100 oranının SMA50'sine göre konumu.
    - 52 haftalık yüksek/düşük aralığındaki konum.
    - Hacim anomalisi: son 20 günde hacmin 3x ortalamayı aştığı gün sayısı + en son patlamanın fiyat tepkisi.
    - Bollinger sıkışması: bant genişliğinin son 120 güne göre yüzdelik sırası (düşükse sıkışma → olası kırılma).
    - Kademeli giriş tetikleyicisi: temel açıdan aday ama teknik zayıfsa, somut bir "şu olursa gir" kuralı.
    """
    if ohlcv is None or len(ohlcv) < 60:
        return {}
    c, v = ohlcv["close"], ohlcv["volume"]
    out = {}

    # ---- 52 haftalık yüksek/düşük konumu ----
    win = min(252, len(c))
    hi252, lo252 = float(c.tail(win).max()), float(c.tail(win).min())
    price_v = float(c.iloc[-1])
    if hi252 > lo252 > 0:
        out["52H Yüksekten Uzaklık"] = round(price_v / hi252 - 1.0, 4)
        out["52H Düşükten Uzaklık"] = round(price_v / lo252 - 1.0, 4)
        out["52H Aralık Konumu"] = round((price_v - lo252) / (hi252 - lo252), 3)

    # ---- Hacim anomalisi ----
    vol_sma20 = v.rolling(20).mean()
    recent = ohlcv.tail(20)
    spike_mask = recent["volume"] > 3.0 * vol_sma20.tail(20)
    n_spikes = int(spike_mask.fillna(False).sum())
    out["Hacim Patlaması (Son 20G)"] = n_spikes
    if n_spikes > 0:
        last_spike_idx = spike_mask[spike_mask].index[-1]
        pos = ohlcv.index.get_loc(last_spike_idx)
        if pos + 1 < len(ohlcv):
            ret = float(ohlcv["close"].iloc[pos + 1] / ohlcv["close"].iloc[pos] - 1.0)
        else:
            ret = float(ohlcv["close"].iloc[pos] / ohlcv["close"].iloc[pos - 1] - 1.0) if pos > 0 else np.nan
        out["Son Hacim Patlaması Tepkisi"] = round(ret, 4) if pd.notna(ret) else np.nan
        out["Hacim Sinyali"] = ("Birikim (Patlama + Yükseliş)" if pd.notna(ret) and ret > 0.01 else
                                "Dağıtım (Patlama + Düşüş)" if pd.notna(ret) and ret < -0.01 else "Patlama (Yönsüz)")
    else:
        out["Hacim Sinyali"] = "Normal"

    # ---- Bollinger sıkışması ----
    try:
        bb_sma, bb_up, bb_lo, _ = calc_bollinger(c, 20, 2.0)
        bw = (bb_up - bb_lo) / bb_sma.replace(0, np.nan)
        bw_now = float(bw.iloc[-1]) if pd.notna(bw.iloc[-1]) else np.nan
        hist = bw.tail(120).dropna()
        if pd.notna(bw_now) and len(hist) >= 40:
            pct = float((hist < bw_now).mean())
            out["Bollinger Bant Genişliği Yüzdelik"] = round(pct, 3)
            out["Volatilite Durumu"] = "Sıkışma (Olası Kırılma)" if pct <= 0.15 else ("Genişleme" if pct >= 0.85 else "Normal")
    except Exception:
        pass

    # ---- Endekse göre göreli güç ----
    if xu_close is not None and len(xu_close) >= 60:
        try:
            n = min(len(c), len(xu_close))
            ratio = (c.tail(n).reset_index(drop=True) / xu_close.tail(n).reset_index(drop=True))
            ratio_sma = ratio.rolling(50).mean()
            r_now, r_sma = float(ratio.iloc[-1]), float(ratio_sma.iloc[-1]) if pd.notna(ratio_sma.iloc[-1]) else np.nan
            if pd.notna(r_sma) and r_sma != 0:
                rs = r_now / r_sma - 1.0
                out["Endekse Göre Güç (RS)"] = round(rs, 4)
                out["Endeks Karşısında"] = "Endeksten Güçlü" if rs > 0.02 else ("Endeksten Zayıf" if rs < -0.02 else "Endeksle Paralel")
        except Exception:
            pass

    # ---- Kademeli giriş tetikleyicisi (yalnızca bilgi; eleme/kapı değildir) ----
    try:
        rsi_v = float(calc_rsi(c, 14).iloc[-1])
        sma50_v = float(c.rolling(50).mean().iloc[-1])
        trig = []
        if pd.notna(rsi_v) and rsi_v < 35:
            trig.append(f"RSI 35 üstüne çıkınca (şu an {rsi_v:.0f})")
        if pd.notna(sma50_v) and price_v < sma50_v:
            trig.append(f"fiyat SMA50'yi ({sma50_v:.2f}) yukarı kesince")
        out["Giriş Tetikleyicisi"] = " VEYA ".join(trig) if trig else "Teknik zaten uyumlu (ek tetikleyici gerekmiyor)"
    except Exception:
        pass
    return out


def enrich_technical(df, session):
    """Tüm evrene RSI/CCI/ADX/MACD/Supertrend/Bollinger + kompozit Teknik Skor,
    haftalık teyit ve piyasa rejimi ekler; fundamental (Final Score) ile
    teknik skoru ufka göre ağırlıklı birleştirip Confluence Skoru + Uyum
    Kadranı üretir. Fiyat/hacim tabanlı bu katman, temel analiz kararını
    değiştirmez — giriş zamanlaması için ikinci bir onay/uyarı katmanıdır.
    """
    if df is None or df.empty or "Hisse" not in df.columns:
        return df
    regime = fetch_market_regime(session)
    df.attrs["market_regime"] = regime
    for k, v in regime.items():
        df[f"Piyasa {k}" if k != "Rejim" else "Piyasa Rejimi"] = v

    codes = df["Hisse"].tolist()
    xu_ohlcv = None
    try:
        xu_ohlcv = fetch_yahoo_ohlcv(session, "XU100", lookback_days=420)
    except Exception:
        xu_ohlcv = None
    xu_close = xu_ohlcv["close"] if xu_ohlcv is not None and len(xu_ohlcv) >= 60 else None
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_yahoo_ohlcv, session, c): c for c in codes}
        for fut, c in futs.items():
            try:
                ohlcv = fut.result()
                sig = compute_technical_signals(ohlcv) or {}
                if sig:
                    sig.update(compute_weekly_confirmation(ohlcv))
                sig = {**sig, **compute_liquidity(ohlcv), **compute_extended_technical(ohlcv, xu_close)}
                results[c] = sig
            except Exception:
                results[c] = None

    tcols = ["RSI(14)", "CCI(20)", "ADX(14)", "MACD Histogram", "Supertrend Yön",
             "Supertrend Dönüş", "Supertrend Seviye", "Bollinger %B", "Bollinger Üst",
             "Bollinger Alt", "ATR(14)", "SMA50 (Teknik)", "Son Kapanış (Yahoo)",
             "Trend Durumu (SMA50)", "Hacim Teyidi", "Teknik Skor", "Teknik Sinyal",
             "Haftalık Trend", "Haftalık RSI"]
    ext_cols = ["52H Yüksekten Uzaklık", "52H Düşükten Uzaklık", "52H Aralık Konumu",
                "Hacim Patlaması (Son 20G)", "Son Hacim Patlaması Tepkisi", "Hacim Sinyali",
                "Bollinger Bant Genişliği Yüzdelik", "Volatilite Durumu",
                "Endekse Göre Güç (RS)", "Endeks Karşısında", "Giriş Tetikleyicisi"]
    for col in tcols + ext_cols + ["Ort. Günlük Hacim (TL)", "Medyan Günlük Hacim (TL)", "İşlemsiz Gün Oranı", "1 Gün Tahliye Kapasitesi (TL)"]:
        df[col] = [(results.get(c) or {}).get(col, np.nan) for c in codes]
    df["Likidite Notu"] = [liquidity_note(a, b) for a, b in zip(df["Medyan Günlük Hacim (TL)"], df["İşlemsiz Gün Oranı"])]
    if "Piyasa Değeri" in df.columns:
        _cap = pd.to_numeric(df["Piyasa Değeri"], errors="coerce")
        df["Yıllık Devir Oranı"] = (pd.to_numeric(df["Ort. Günlük Hacim (TL)"], errors="coerce") * 252 / _cap).where(_cap > 0)

    # Çoklu zaman dilimi teyidi: günlük Supertrend ile haftalık Supertrend aynı yönde mi
    def _mtf(row):
        d, w = row.get("Supertrend Yön"), row.get("Haftalık Trend")
        if pd.isna(d) or pd.isna(w):
            return "Veri Yetersiz"
        if d == "Yükseliş" and w == "Yükseliş":
            return "Teyitli (Günlük+Haftalık Uyumlu)"
        if d == "Düşüş" and w == "Düşüş":
            return "Teyitli (İkisi de Düşüş)"
        return "Karışık (Zaman Dilimleri Ayrışıyor)"
    df["Çoklu Zaman Teyidi"] = df.apply(_mtf, axis=1)

    def _confluence(v):
        if v == "AL (Trend+Momentum Uyumlu)":
            return "AL (Temel+Teknik Uyumlu)"
        if v == "İZLE (Kısmi Teyit)":
            return "İZLE (Kısmi Teknik Teyit)"
        if v == "BEKLE (Düşüş Trendi)":
            return "BEKLE (Teknik Trend Zayıf)"
        if v == "NÖTR":
            return "NÖTR (Teknik Belirsiz)"
        return "VERİ YETERSİZ"
    df["Teknik Aksiyon"] = df["Teknik Sinyal"].map(_confluence)

    # --- Ağırlıklı Confluence Skoru (ufka göre değişken ağırlık) + Uyum Kadranı ---
    fs = pd.to_numeric(df.get("Final Score"), errors="coerce")
    ts = pd.to_numeric(df.get("Teknik Skor"), errors="coerce")
    carpan = regime.get("Rejim Çarpanı", 1.0)

    def _conf(w_temel, w_teknik):
        num = w_temel * fs.fillna(fs.median() if fs.notna().any() else 50) + \
              w_teknik * ts.fillna(ts.median() if ts.notna().any() else 50)
        return (num * carpan).clip(0, 100)

    df["Confluence Skoru (Kısa)"] = _conf(0.30, 0.70).round(1)
    df["Confluence Skoru (Orta)"] = _conf(0.50, 0.50).round(1)
    df["Confluence Skoru (Uzun)"] = _conf(0.80, 0.20).round(1)

    def _kadran(row):
        f_hi = pd.notna(row.get("Final Score")) and row["Final Score"] >= 60
        t_hi = pd.notna(row.get("Teknik Skor")) and row["Teknik Skor"] >= 55
        if pd.isna(row.get("Final Score")) or pd.isna(row.get("Teknik Skor")):
            return "Veri Yetersiz"
        if f_hi and t_hi:
            return "GÜÇLÜ UYUM (Temel↑ Teknik↑)"
        if f_hi and not t_hi:
            return "ERKEN BİRİKİM (Temel↑ Teknik↓ — henüz dönmedi)"
        if not f_hi and t_hi:
            return "MOMENTUM RİSKİ (Temel↓ Teknik↑ — dikkatli)"
        return "KAÇIN (Temel↓ Teknik↓)"
    df["Uyum Kadranı"] = df.apply(_kadran, axis=1)
    return df


def build_technical_sheet(df):
    """'Teknik' sayfası için RSI/CCI/Supertrend/MACD/ADX + çoklu zaman dilimi +
    Confluence Skoru + kısa/orta/uzun vade fiyat sinyalleri özet tablosu."""
    if df is None or df.empty:
        return pd.DataFrame([{"Not": "Veri yok"}])
    cols = ["Hisse", "Sektör", "Kural Sınıfı" if "Kural Sınıfı" in df.columns else None,
            "Piyasa Rejimi", "Fiyat", "Final Score", "Teknik Skor",
            "Confluence Skoru (Kısa)", "Confluence Skoru (Orta)", "Confluence Skoru (Uzun)",
            "Uyum Kadranı", "Teknik Sinyal", "Teknik Aksiyon",
            "TV Görünüm", "TV Recommend.All", "TV RSI", "TV Uyum Notu",
            "Çoklu Zaman Teyidi", "Haftalık Trend", "Haftalık RSI",
            "KV Giriş Aralığı", "KV Hedef", "KV Stop",
            "OV Giriş", "OV Hedef", "OV Stop",
            "UV Giriş", "UV Hedef", "UV Stop",
            "RSI(14)", "CCI(20)", "ADX(14)", "MACD Histogram",
            "Supertrend Yön", "Supertrend Seviye", "Supertrend Dönüş",
            "Bollinger Üst", "Bollinger Alt", "ATR(14)", "SMA50 (Teknik)",
            "Trend Durumu (SMA50)", "Hacim Teyidi"]
    cols = [c for c in cols if c and c in df.columns]
    if "Teknik Skor" not in df.columns:
        return pd.DataFrame([{"Not": "Teknik analiz verisi hesaplanamadı (Yahoo OHLCV alınamadı)."}])
    out = df[cols].copy()
    if "Confluence Skoru (Orta)" in out.columns:
        out = out.sort_values("Confluence Skoru (Orta)", ascending=False)
    elif "Teknik Skor" in out.columns:
        out = out.sort_values("Teknik Skor", ascending=False)
    return out


def enrich_price_signals(df):
    """Kısa/Orta/Uzun vade somut fiyat seviyeleri (Giriş / Hedef / Stop).

    - KISA VADE (gün-hafta, teknik): trend yukarıysa Supertrend seviyesi ile
      mevcut fiyat arası "Giriş Aralığı", hedef Bollinger üst bandı (veya
      ATR bazlı), stop Supertrend seviyesinin hemen altı. Trend aşağıysa
      net bir giriş önerilmez ("Bekle").
    - ORTA VADE (hafta-ay, teknik trend + kısmi temel yakınsama): giriş
      SMA50 geri çekilme bölgesi, hedef Adil Değer'in bir kısmı (temkinli
      ara hedef), stop SMA50'nin altı.
    - UZUN VADE (6-12+ ay, temel): giriş mevcut fiyat (ucuzsa kademeli),
      hedef temel Adil Değer (Medyan), taban/stop DCF Bear senaryosu
      (temel tez bozulursa referans).

    Bunlar kesin emir seviyeleri değil, senaryo bazlı referans aralıklarıdır;
    yatırım tavsiyesi değildir — spread/komisyon/slipaj içermez.
    """
    if df is None or df.empty or "Fiyat" not in df.columns:
        return df
    price = pd.to_numeric(df["Fiyat"], errors="coerce")
    atr = pd.to_numeric(df.get("ATR(14)"), errors="coerce") if "ATR(14)" in df.columns else pd.Series(np.nan, index=df.index)
    bb_up = pd.to_numeric(df.get("Bollinger Üst"), errors="coerce") if "Bollinger Üst" in df.columns else pd.Series(np.nan, index=df.index)
    st_level = pd.to_numeric(df.get("Supertrend Seviye"), errors="coerce") if "Supertrend Seviye" in df.columns else pd.Series(np.nan, index=df.index)
    st_dir = df.get("Supertrend Yön") if "Supertrend Yön" in df.columns else pd.Series(np.nan, index=df.index)
    sma50 = pd.to_numeric(df.get("SMA50 (Teknik)"), errors="coerce") if "SMA50 (Teknik)" in df.columns else pd.Series(np.nan, index=df.index)
    fair = pd.to_numeric(df.get("Adil Değer (Medyan)"), errors="coerce") if "Adil Değer (Medyan)" in df.columns else pd.Series(np.nan, index=df.index)
    dcf_bear = pd.to_numeric(df.get("DCF Bear"), errors="coerce") if "DCF Bear" in df.columns else pd.Series(np.nan, index=df.index)

    up = (st_dir == "Yükseliş")

    # --- Kısa vade ---
    kv_alt = np.where(up & pd.notna(st_level), np.minimum(st_level, price), np.nan)
    kv_ust = np.where(up, price, np.nan)
    kv_hedef = np.where(pd.notna(bb_up), bb_up, price + 1.5 * atr)
    kv_hedef = np.where(up, kv_hedef, np.nan)
    kv_stop = np.where(pd.notna(st_level), st_level * 0.99, price - 1.5 * atr)
    kv_stop = np.where(up, kv_stop, np.nan)

    def _fmt_range(a, b):
        if pd.isna(a) or pd.isna(b):
            return "Bekle (Kısa Vade Trend Aşağı)"
        lo, hi = (a, b) if a <= b else (b, a)
        return f"{lo:.2f} - {hi:.2f}"

    df["KV Giriş Aralığı"] = [_fmt_range(a, b) for a, b in zip(kv_alt, kv_ust)]
    df["KV Hedef"] = np.round(kv_hedef, 2)
    df["KV Stop"] = np.round(kv_stop, 2)

    # --- Orta vade ---
    ov_giris = np.where(pd.notna(sma50), sma50, price)
    ov_hedef = fair * 0.75
    ov_stop = np.where(pd.notna(sma50), sma50 * 0.93, price * 0.90)
    df["OV Giriş"] = np.round(ov_giris, 2)
    df["OV Hedef"] = np.round(ov_hedef, 2)
    df["OV Stop"] = np.round(ov_stop, 2)

    # --- Uzun vade ---
    uv_stop = np.where(pd.notna(dcf_bear), dcf_bear, price * 0.80)
    df["UV Giriş"] = np.round(price, 2)
    df["UV Hedef"] = np.round(fair, 2)
    df["UV Stop"] = np.round(uv_stop, 2)

    # Kısa özet notu
    def _ozet(row):
        parts = []
        if isinstance(row.get("KV Giriş Aralığı"), str) and "Bekle" not in row["KV Giriş Aralığı"]:
            parts.append(f"Kısa: {row['KV Giriş Aralığı']} arasında al, {row.get('KV Hedef')} sat, {row.get('KV Stop')} altı stop")
        else:
            parts.append("Kısa: bekle (trend aşağı)")
        if pd.notna(row.get("OV Giriş")) and pd.notna(row.get("OV Hedef")):
            parts.append(f"Orta: {row.get('OV Giriş')} civarı al, {row.get('OV Hedef')} sat, {row.get('OV Stop')} altı stop")
        if pd.notna(row.get("UV Hedef")):
            parts.append(f"Uzun: {row.get('UV Giriş')} kademeli al, {row.get('UV Hedef')} adil değer sat, {row.get('UV Stop')} taban")
        return " | ".join(parts)
    df["Fiyat Sinyali Özeti"] = df.apply(_ozet, axis=1)
    return df


def kelly_from_pb(p, b, f_max=None):
    f_max = KELLY_F_MAX if f_max is None else f_max
    try:
        p = float(p); b = float(b)
    except Exception:
        return np.nan
    if not np.isfinite(p) or not np.isfinite(b) or b <= 1e-9:
        return np.nan
    p = float(np.clip(p, 0.0, 1.0))
    q = 1.0 - p
    f = p - q / b
    return float(np.clip(f, 0.0, f_max))


def kelly_from_mean_var(m, var, f_max=None):
    f_max = KELLY_F_MAX if f_max is None else f_max
    try:
        m = float(m); var = float(var)
    except Exception:
        return np.nan
    if not np.isfinite(m) or not np.isfinite(var) or var <= 1e-12:
        return 0.0 if (np.isfinite(m) and m <= 0) else np.nan
    # v192.3: sert clip yerine tanh yumuşak tavan — tavana yapışan hisseler ayırt edilebilir kalır
    return float(f_max * np.tanh(max(m / var, 0.0) / f_max))


def apply_backtest(df, session):
    if not BACKTEST_ENABLED or df.empty:
        return df
    # Tek Yahoo isteği / hisse → 6A/12A/24A + vol (önceden 4 istek)
    codes = df["Hisse"].tolist()
    bundle = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_yahoo_bundle, session, c): c for c in codes}
        for fut, c in futs.items():
            try:
                bundle[c] = fut.result()
            except Exception:
                bundle[c] = (np.nan, np.nan, np.nan, np.nan, np.nan)
    df["Backtest 6A İleri Getiri"] = [bundle.get(c, (np.nan,) * 5)[0] for c in codes]
    df["Backtest 12A İleri Getiri"] = [bundle.get(c, (np.nan,) * 5)[1] for c in codes]
    df["Backtest 24A İleri Getiri"] = [bundle.get(c, (np.nan,) * 5)[2] for c in codes]
    df["Vol Ann (σ)"] = [bundle.get(c, (np.nan,) * 5)[3] for c in codes]
    df["Yahoo Son Fiyat"] = [bundle.get(c, (np.nan,) * 5)[4] for c in codes]
    # v192.3: iki fiyat kaynağı arasındaki fark (adil değer marjı fiyata çok duyarlı)
    _px = pd.to_numeric(df["Fiyat"], errors="coerce") if "Fiyat" in df.columns else pd.Series(np.nan, index=df.index)
    _yp = pd.to_numeric(df["Yahoo Son Fiyat"], errors="coerce")
    df["Fiyat Farkı %"] = (_px / _yp - 1.0).where((_px > 0) & (_yp > 0))
    df["Fiyat Uyarısı"] = np.where(df["Fiyat Farkı %"].isna(), "Doğrulanamadı",
                                   np.where(df["Fiyat Farkı %"].abs() > PRICE_MISMATCH_TOL, "Uyuşmazlık", "OK"))

    try:
        bins = [-np.inf, 50, 58, 66, 74, 82, 90, np.inf]
        labels = ["<50", "50-58", "58-66", "66-74", "74-82", "82-90", "90+"]
        df["Final Score Band"] = pd.cut(df["Final Score"], bins=bins, labels=labels, right=False).astype(str)
        # v192: N=3 gibi bantlarda ortalama anlamsız; eşit sayılı beşli (quintile) + medyan ver.
        _cols = ["Backtest 6A İleri Getiri", "Backtest 12A İleri Getiri", "Backtest 24A İleri Getiri"]
        _q = pd.qcut(df["Final Score"].rank(method="first"), 5, labels=["Q1 (düşük)", "Q2", "Q3", "Q4", "Q5 (yüksek)"])
        def _wins_mean(x):
            x = pd.to_numeric(x, errors="coerce").dropna()
            return float(x.clip(x.quantile(.05), x.quantile(.95)).mean()) if len(x) >= 5 else np.nan
        band = df.groupby(_q, observed=False).agg(
            N=("Hisse", "count"),
            Medyan_6A=(_cols[0], "median"), Medyan_12A=(_cols[1], "median"), Medyan_24A=(_cols[2], "median"),
            Wins_Ort_6A=(_cols[0], _wins_mean), Wins_Ort_12A=(_cols[1], _wins_mean), Wins_Ort_24A=(_cols[2], _wins_mean),
        ).reset_index().rename(columns={"Final Score": "Skor Beşlisi", "index": "Skor Beşlisi"})
        band.iloc[:, 0] = band.iloc[:, 0].astype(str)
        band.columns = ["Skor Beşlisi"] + list(band.columns[1:])
        band["Not"] = ""
        band.loc[band.index[0], "Not"] = "GEÇMİŞ getiri (son 6/12/24 ay) × bugünkü skor; ileri getiri/backtest DEĞİL. Medyan ve winsorize ortalama kullanılır."
        df.attrs["backtest_summary"] = band
    except Exception:
        df.attrs["backtest_summary"] = pd.DataFrame()
    return df


def _score_history_path(output_path=None):
    """bist_score_history.csv HER ZAMAN aynı dosya adıyla VE aynı sabit
    klasörde tutulur — Excel raporunu farklı bir klasöre/isimle kaydetsen
    bile (farklı --output, farklı çalışma dizini, GUI'de farklı yer seçme)
    arşiv PARÇALANMAZ, tek dosyada birikmeye devam eder.

    Öncelik sırası:
      1) BIST_HISTORY_DIR ortam değişkeni (--history-dir CLI bayrağı bunu set eder)
      2) Script dosyasının bulunduğu klasördeki sabit 'bist_history' alt klasörü

    output_path parametresi artık klasör seçiminde KULLANILMAZ (önceki
    davranışta kullanılıyordu ve bu, farklı klasöre kaydedilen her run'da
    arşivin sessizce yeni bir dosyaya bölünmesine yol açıyordu) — sadece
    geriye dönük uyumluluk için imzada tutuluyor.
    """
    override = os.environ.get("BIST_HISTORY_DIR")
    if override:
        base = Path(override).expanduser().resolve()
    else:
        base = Path(__file__).resolve().parent / "bist_history"
    base.mkdir(parents=True, exist_ok=True)
    return base / SCORE_HISTORY_NAME


CANDIDATE_HISTORY_NAME = "bist_candidate_history.csv"


def append_candidate_history(cand, out=None, output_path=None, today=None, liste="Aday"):
    """Her çalıştırmada aday listesini (tier, sıra, fiyat, XU100) arşivler → sonradan tier bazında ileri getiri ölçülebilir.
    Aynı gün aynı hisse tekrar yazılmaz."""
    if cand is None or len(cand) == 0:
        return None
    path = _score_history_path(output_path).parent / CANDIDATE_HISTORY_NAME
    day = (today or pd.Timestamp.today()).strftime("%Y-%m-%d")
    xu = np.nan
    if out is not None and "Piyasa XU100 Fiyat" in getattr(out, "columns", []):
        xu = pd.to_numeric(out["Piyasa XU100 Fiyat"], errors="coerce").dropna().iloc[0] if out["Piyasa XU100 Fiyat"].notna().any() else np.nan
    modu = cand["Aday Modu"].astype(str) if "Aday Modu" in cand.columns else pd.Series("", index=cand.index)
    _sira = pd.to_numeric(cand["Aday Sıra"], errors="coerce") if "Aday Sıra" in cand.columns else pd.Series(np.nan, index=cand.index)
    tier = modu.map(lambda x: "A" if x.startswith("Tier A") else "B" if x.startswith("Tier B") else "C" if x.startswith("Tier C")
                    else x.replace("Tier ", "") if x.startswith("Tier V-") else "")
    # Aday/Değer dışındaki (Magic Formula, Momentum, Temettü, Düşük Volatilite, Sektöre Göre Ucuz) düz sıralı
    # listelerde "Tier" A/B/C anlamı taşımaz; ilk yarı/ikinci yarı ayrımıyla değiştirilir (Üst/Alt).
    _flat = ~tier.isin(["A", "B", "C"]) & (~modu.str.startswith("Tier V-"))
    _mid = float(np.nanmedian(_sira)) if _sira.notna().any() else np.nan
    tier = tier.where(~_flat, np.where(_sira <= _mid, "Üst", "Alt"))
    new = pd.DataFrame({
        "snapshot_date": day, "Liste": liste, "Hisse": cand["Hisse"].values, "Tier": tier.values,
        "Aday Sıra": cand["Aday Sıra"].values if "Aday Sıra" in cand.columns else np.arange(1, len(cand) + 1),
        "Fiyat": pd.to_numeric(cand["Fiyat"], errors="coerce").values if "Fiyat" in cand.columns else np.nan,
        "XU100": xu,
        "Adil Değer Marjı": pd.to_numeric(cand["Adil Değer Marjı"], errors="coerce").values if "Adil Değer Marjı" in cand.columns else np.nan,
    })
    if path.exists():
        old = pd.read_csv(path)
        if "Liste" not in old.columns:
            old.insert(1, "Liste", "Aday")
        new = pd.concat([old, new], ignore_index=True).drop_duplicates(["snapshot_date", "Liste", "Hisse"], keep="last")
    new.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def evaluate_candidate_history(price_fn, xu_fn=None, output_path=None, min_days=30, today=None):
    """Arşivdeki her aday için (>= min_days önce listelenmiş) fiyat getirisini ve XU100'e göre fazlasını hesaplar;
    tier bazında özetler. price_fn(hisse)->güncel fiyat, xu_fn()->güncel XU100. Ham fiyat oranı (split/temettü düzeltmesi yok)."""
    path = _score_history_path(output_path).parent / CANDIDATE_HISTORY_NAME
    if not path.exists():
        return pd.DataFrame(), pd.DataFrame()
    h = pd.read_csv(path)
    if "Liste" not in h.columns:
        h["Liste"] = "Aday"
    h["Tier"] = np.where(h["Liste"] == "Aday", h["Tier"].astype(str), h["Liste"].astype(str) + "-" + h["Tier"].astype(str))
    h["snapshot_date"] = pd.to_datetime(h["snapshot_date"], errors="coerce")
    now = pd.Timestamp(today or pd.Timestamp.today()).normalize()
    h["Gün"] = (now - h["snapshot_date"]).dt.days
    h = h[h["Gün"] >= min_days].copy()
    if h.empty:
        return pd.DataFrame(), pd.DataFrame()
    cache = {}
    def _px(c):
        if c not in cache:
            try:
                cache[c] = float(price_fn(c))
            except Exception:
                cache[c] = np.nan
        return cache[c]
    xu_now = np.nan
    if xu_fn is not None:
        try:
            xu_now = float(xu_fn())
        except Exception:
            xu_now = np.nan
    h["Güncel Fiyat"] = h["Hisse"].map(_px)
    h["Getiri"] = h["Güncel Fiyat"] / pd.to_numeric(h["Fiyat"], errors="coerce") - 1.0
    h["XU100 Getiri"] = (xu_now / pd.to_numeric(h["XU100"], errors="coerce") - 1.0) if pd.notna(xu_now) else np.nan
    h["Fazla Getiri"] = h["Getiri"] - h["XU100 Getiri"]
    h["Şüpheli (split?)"] = h["Getiri"].abs() > 0.8
    def _summ(g):
        return pd.Series({
            "N": int(g["Getiri"].notna().sum()), "Medyan Getiri": g["Getiri"].median(),
            "Medyan Fazla Getiri (XU100)": g["Fazla Getiri"].median(),
            "İsabet (Getiri>0)": float((g["Getiri"] > 0).mean()) if g["Getiri"].notna().any() else np.nan,
            "İsabet (XU100'ü geçen)": float((g["Fazla Getiri"] > 0).mean()) if g["Fazla Getiri"].notna().any() else np.nan,
            "Ort. Gün": g["Gün"].mean(),
        })
    summ = h.groupby("Tier").apply(_summ).reset_index()
    tot = _summ(h).to_frame().T
    tot.insert(0, "Tier", "Tümü")
    summ = pd.concat([summ, tot], ignore_index=True)
    return h.sort_values(["snapshot_date", "Aday Sıra"], ascending=[False, True]), summ


def append_score_history(df, output_path=None):
    """Her run'da skor anlık görüntüsü append (gelecekte p,b için)."""
    if df is None or df.empty or "Hisse" not in df.columns:
        return None
    cols_want = [
        "Hisse", "Sektör", "Final Score", "Risk Ayarlı Skor", "Güven Skoru",
        "Shannon Notu", "Shannon H norm", "Shannon Edge", "Kelly Pay Proxy",
        "DD Aksiyon", "Kova", "Adil Değer Marjı", "FCF Getirisi", "Owner Earnings Getirisi",
        "Fiyat", "Vol Ann (σ)", "Backtest 6A İleri Getiri", "Backtest 12A İleri Getiri",
        "Final Score Band", "Kural Sınıfı", "Teknik Skor", "Teknik Sinyal",
        "Confluence Skoru (Kısa)", "Confluence Skoru (Orta)", "Confluence Skoru (Uzun)",
        "Uyum Kadranı", "Piyasa Rejimi",
    ]
    use = [c for c in cols_want if c in df.columns]
    snap = df[use].copy()
    snap.insert(0, "snapshot_date", datetime.now().strftime("%Y-%m-%d"))
    snap.insert(1, "snapshot_ts", datetime.now().isoformat(timespec="seconds"))
    path = _score_history_path(output_path)
    try:
        if path.exists():
            old = pd.read_csv(path)
            # aynı gün aynı hisse tekrarını son kayıtla değiştir
            key = ["snapshot_date", "Hisse"]
            if all(k in old.columns for k in key):
                old = old[~old.set_index(key).index.isin(snap.set_index(key).index)]
            out = pd.concat([old, snap], ignore_index=True)
        else:
            out = snap
        out.to_csv(path, index=False, encoding="utf-8-sig")
        return str(path)
    except Exception as e:
        print("score_history yazılamadı:", e, flush=True)
        return None


def evaluate_track_record(session, output_path=None, horizons=((180, "6A"), (365, "12A"))):
    """Skor arşivindeki (bist_score_history.csv) GEÇMİŞ anlık görüntüleri,
    o tarihten BUGÜNE kadar GERÇEKLEŞMİŞ fiyat hareketiyle eşleştirip sinyal
    gruplarına göre isabet oranı / ortalama getiri / XU100'e göre alfa üretir.

    Bunu apply_backtest'teki 'İleri Getiri' kolonlarıyla KARIŞTIRMAYIN:
    - apply_backtest: bugünkü skor × bugünden GERİYE dönük trailing getiri
      (look-ahead'e yakın, bkz. walk_forward_disclaimer — bir skorun N ay
      önce de aynı olacağını varsayar).
    - Bu fonksiyon: geçmişte kaydedilmiş skor/fiyat × o andan BUGÜNE kadar
      gerçekten yaşanmış getiri. Gerçek bir point-in-time track record.

    Sonuç olarak bu fonksiyon, script ilk kez çalıştırıldıktan sonraki ilk
    180 günde boş/"yetersiz veri" döner — bu bir hata değil, sistemin
    kendini kandırmadığının kanıtıdır. Gerçek "başarı oranı" ancak script
    düzenli aralıklarla (örn. haftalık/aylık) çalıştırılıp arşiv biriktikçe
    ortaya çıkar.
    """
    path = _score_history_path(output_path)
    if not path.exists():
        return pd.DataFrame(), "Skor arşivi henüz yok — ilk run sonrası oluşur, değerlendirme biriken geçmişle mümkün olur."
    try:
        hist = pd.read_csv(path)
        hist["snapshot_date"] = pd.to_datetime(hist["snapshot_date"], errors="coerce")
    except Exception as e:
        return pd.DataFrame(), f"Skor arşivi okunamadı: {e}"
    if hist.empty or "Hisse" not in hist.columns or "Fiyat" not in hist.columns:
        return pd.DataFrame(), "Skor arşivi boş veya eksik kolonlu."

    today = pd.Timestamp(datetime.now().date())
    hist["gün_farkı"] = (today - hist["snapshot_date"]).dt.days
    xu100_ohlcv = fetch_yahoo_ohlcv(session, "XU100", lookback_days=800)

    rows_out, notes = [], []
    for horizon_days, label in horizons:
        due = hist[hist["gün_farkı"] >= horizon_days].copy()
        if due.empty:
            notes.append(f"{label}: henüz {horizon_days} gün geçmiş snapshot yok.")
            continue
        codes = due["Hisse"].dropna().unique().tolist()
        price_cache = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(fetch_yahoo_ohlcv, session, c, 800): c for c in codes}
            for fut, c in futs.items():
                try:
                    price_cache[c] = fut.result()
                except Exception:
                    price_cache[c] = None

        def _realized(row):
            ohlcv = price_cache.get(row["Hisse"])
            if ohlcv is None or ohlcv.empty or pd.isna(row.get("Fiyat")):
                return np.nan, np.nan
            try:
                snap_px = float(row["Fiyat"])
                cur_px = float(ohlcv["close"].iloc[-1])
            except Exception:
                return np.nan, np.nan
            if snap_px <= 0:
                return np.nan, np.nan
            stock_ret = (cur_px - snap_px) / snap_px
            bench_ret = np.nan
            if xu100_ohlcv is not None and not xu100_ohlcv.empty and pd.notna(row.get("snapshot_date")):
                pos = int(xu100_ohlcv.index.searchsorted(row["snapshot_date"]))
                pos = min(pos, len(xu100_ohlcv) - 1)
                xu_snap = float(xu100_ohlcv["close"].iloc[pos])
                xu_cur = float(xu100_ohlcv["close"].iloc[-1])
                if xu_snap > 0:
                    bench_ret = (xu_cur - xu_snap) / xu_snap
            return stock_ret, bench_ret

        realized = due.apply(_realized, axis=1, result_type="expand")
        due["Gerçekleşen Getiri"] = realized[0]
        due["XU100 Getiri (Aynı Pencere)"] = realized[1]
        due["Alfa"] = due["Gerçekleşen Getiri"] - due["XU100 Getiri (Aynı Pencere)"]

        for bucket_col in ["Final Score Band", "Kova", "Uyum Kadranı", "Teknik Sinyal"]:
            if bucket_col not in due.columns:
                continue
            g = due.dropna(subset=["Gerçekleşen Getiri"]).groupby(bucket_col)
            if g.ngroups == 0:
                continue
            summ = g.agg(
                N=("Hisse", "count"),
                İsabet_Oranı_Pct=("Gerçekleşen Getiri", lambda x: round(100 * (x > 0).mean(), 1)),
                Ortalama_Getiri_Pct=("Gerçekleşen Getiri", lambda x: round(100 * x.mean(), 2)),
                Medyan_Getiri_Pct=("Gerçekleşen Getiri", lambda x: round(100 * x.median(), 2)),
                Ortalama_Alfa_Pct=("Alfa", lambda x: round(100 * x.mean(), 2)),
            ).reset_index().rename(columns={bucket_col: "Grup"})
            summ.insert(0, "Ufuk", label)
            summ.insert(1, "Grup Tipi", bucket_col)
            rows_out.append(summ)

    if not rows_out:
        note = " | ".join(notes) if notes else "Henüz değerlendirilecek veri yok."
        return pd.DataFrame(), note
    report = pd.concat(rows_out, ignore_index=True)
    note = f"Track record: toplam {int(report['N'].sum())} sinyal-dönem gözlemi değerlendirildi."
    if notes:
        note += " | " + " | ".join(notes)
    return report, note


def load_kelly_band_stats(output_path=None, horizon_col="Backtest 6A İleri Getiri"):
    """Arşiv + (mümkünse) forward getiri ile band/kova p,b,m,var.

    Not: Aynı satırdaki Backtest getiri look-ahead'li olabilir; asıl kalite
    snapshot_date ile gelecekte birleştirilmiş return'lerden gelir.
    Şimdilik: geçmiş snapshot'larda kayıtlı Backtest * İleri Getiri varsa kullan.
    """
    if not KELLY_HIST_ENABLED:
        # v192: arşivdeki "Backtest İleri Getiri" değerleri GEÇMİŞ getiri ve her koşuda tekrar sayılıyor
        # (bağımsız gözlem değil; N=3.596 > 590 hisse). Gerçek ileri getiri birikene kadar kapalı.
        return pd.DataFrame()
    path = _score_history_path(output_path)
    if not path.exists():
        return pd.DataFrame()
    try:
        hist = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if hist.empty or horizon_col not in hist.columns:
        return pd.DataFrame()
    hist[horizon_col] = pd.to_numeric(hist[horizon_col], errors="coerce")
    # sinyal anahtarı: Kova varsa Kova, yoksa Final Score Band
    key = "Kova" if "Kova" in hist.columns and hist["Kova"].notna().any() else (
        "Final Score Band" if "Final Score Band" in hist.columns else None
    )
    if key is None:
        return pd.DataFrame()
    rows = []
    for name, g in hist.dropna(subset=[horizon_col]).groupby(key):
        r = g[horizon_col]
        n = int(r.shape[0])
        if n < 5:
            continue
        wins = r[r > 0]
        losses = r[r <= 0]
        p = float((r > 0).mean())
        mean_win = float(wins.mean()) if len(wins) else np.nan
        mean_loss = float((-losses).mean()) if len(losses) else np.nan
        b = mean_win / mean_loss if (np.isfinite(mean_win) and np.isfinite(mean_loss) and mean_loss > 1e-9) else np.nan
        m = float(r.mean())
        var = float(r.var(ddof=1)) if n >= 3 else np.nan
        f_pb = kelly_from_pb(p, b) if n >= KELLY_MIN_N and np.isfinite(b) else np.nan
        f_mv = kelly_from_mean_var(m, var) if n >= KELLY_MIN_N else np.nan
        rows.append({
            "Sinyal Anahtarı": key,
            "Sinyal": name,
            "N": n,
            "p_win": round(p, 4),
            "b_odds": None if not np.isfinite(b) else round(b, 4),
            "mean_R": round(m, 4),
            "var_R": None if not np.isfinite(var) else round(var, 6),
            "Kelly_pb": None if not np.isfinite(f_pb) else round(f_pb, 4),
            "Kelly_mv": None if not np.isfinite(f_mv) else round(f_mv, 4),
            "Yeterli_N": n >= KELLY_MIN_N,
        })
    return pd.DataFrame(rows)


def enrich_kelly_full(df, session=None, output_path=None, rf_annual=None):
    """Vol tabanlı m/σ² + tarihsel p,b (yeterli N) + hibrit Kelly Final.

    Kelly Pay Proxy  : edge/unc² (Shannon, mevcut)
    Kelly Vol (m/σ²) : edge_ret / σ²_horizon  (Yahoo vol)
    Kelly Historical : band/kova p,b (arşiv N≥KELLY_MIN_N)
    Kelly Final      : hibrit, half-Kelly opsiyonu
    """
    if df is None or df.empty:
        return df
    rf_annual = rf_annual if rf_annual is not None else RISK_FREE_FALLBACK
    # 6A için rf yaklaşık
    rf_6a = float(rf_annual) * 0.5

    edge = pd.to_numeric(df["Shannon Edge"], errors="coerce") if "Shannon Edge" in df.columns else pd.Series(np.nan, index=df.index)
    # edge yoksa adil marj / OE karışımı
    if edge.isna().all():
        parts = []
        for c in ("Adil Değer Marjı", "Owner Earnings Getirisi", "FCF Getirisi"):
            if c in df.columns:
                parts.append(pd.to_numeric(df[c], errors="coerce"))
        if parts:
            edge = pd.concat(parts, axis=1).mean(axis=1)

    sig = pd.to_numeric(df["Vol Ann (σ)"], errors="coerce") if "Vol Ann (σ)" in df.columns else pd.Series(np.nan, index=df.index)
    # 6A varyans ≈ σ_ann² * 0.5
    var_6a = (sig ** 2) * 0.5
    m = edge - rf_6a * 0.0  # edge zaten getiri-benzeri; rf'yi agresif düşme (edge fiyat marjı)
    # Vol Kelly: pozitif edge ve geçerli vol
    kelly_vol = []
    for mi, vi in zip(m.fillna(np.nan), var_6a.fillna(np.nan)):
        if np.isfinite(mi) and mi > 0 and np.isfinite(vi) and vi > 1e-12:
            kelly_vol.append(kelly_from_mean_var(mi, vi))
        elif np.isfinite(mi) and mi <= 0:
            kelly_vol.append(0.0)
        else:
            kelly_vol.append(np.nan)
    df["Kelly Vol (m/σ²)"] = kelly_vol

    # Tarihsel band stats
    stats = load_kelly_band_stats(output_path)
    df.attrs["kelly_band_stats"] = stats
    kelly_hist = []
    p_list, b_list, n_list = [], [], []
    key_col = None
    if not stats.empty:
        key_col = str(stats["Sinyal Anahtarı"].iloc[0])
        stmap = stats.set_index("Sinyal")
        for _, row in df.iterrows():
            sig_key = row.get(key_col) if key_col in df.columns else row.get("Final Score Band")
            if sig_key in stmap.index and bool(stmap.loc[sig_key, "Yeterli_N"]):
                rec = stmap.loc[sig_key]
                if isinstance(rec, pd.DataFrame):
                    rec = rec.iloc[0]
                fh = rec.get("Kelly_pb", np.nan)
                if not np.isfinite(fh):
                    fh = rec.get("Kelly_mv", np.nan)
                kelly_hist.append(float(fh) if np.isfinite(fh) else np.nan)
                p_list.append(rec.get("p_win", np.nan))
                b_list.append(rec.get("b_odds", np.nan))
                n_list.append(rec.get("N", np.nan))
            else:
                kelly_hist.append(np.nan)
                p_list.append(np.nan)
                b_list.append(np.nan)
                n_list.append(np.nan)
    else:
        kelly_hist = [np.nan] * len(df)
        p_list = b_list = n_list = [np.nan] * len(df)

    df["Kelly Historical"] = kelly_hist
    df["Kelly p_win"] = p_list
    df["Kelly b_odds"] = b_list
    df["Kelly N"] = n_list

    # Hibrit Final
    proxy = pd.to_numeric(df["Kelly Pay Proxy"], errors="coerce") if "Kelly Pay Proxy" in df.columns else pd.Series(np.nan, index=df.index)
    h_norm = pd.to_numeric(df["Shannon H norm"], errors="coerce") if "Shannon H norm" in df.columns else pd.Series(0.7, index=df.index)
    finals = []
    notes = []
    for i in range(len(df)):
        parts = []
        src = []
        kv = kelly_vol[i] if i < len(kelly_vol) else np.nan
        kh = kelly_hist[i] if i < len(kelly_hist) else np.nan
        kp = proxy.iloc[i] if i < len(proxy) else np.nan
        if np.isfinite(kh):
            parts.append(kh); src.append("hist")
        if np.isfinite(kv):
            parts.append(kv); src.append("vol")
        if np.isfinite(kp):
            parts.append(kp); src.append("proxy")
        if not parts:
            finals.append(np.nan)
            notes.append("Kelly: veri yok")
            continue
        f = float(np.mean(parts))
        # Shannon cezası
        hn = h_norm.iloc[i] if i < len(h_norm) else 0.7
        if np.isfinite(hn):
            f *= float(np.clip(1.0 - 0.35 * hn, 0.4, 1.0))
        f = float(KELLY_F_MAX * np.tanh(max(f, 0.0) / KELLY_F_MAX))   # yumuşak tavan (sıralamayı korur)
        if KELLY_HALF:
            f = f * 0.5
            src.append("half")
        finals.append(round(f, 4))
        notes.append("Kelly Final=" + "+".join(src) + f" → {f:.2%}")
    df["Kelly Final"] = finals
    df["Kelly Notu"] = notes
    return df



# ---------------------------------------------------------------------------
# Risk eklentileri: Stop / Trailing · Ters sinyal · Markowitz + Risk Parity
# ---------------------------------------------------------------------------


def _clip01(x):
    try:
        x = float(x)
    except Exception:
        return np.nan
    if not np.isfinite(x):
        return np.nan
    return float(np.clip(x, 0.0, 1.0))


def build_composite_evaluation_for_row(row):
    """Tüm metodları birleştiren değerlendirme notu + 0-100 bütünleşik skor.

    Ağırlıklar modül sabitleri EVAL_W_* (kullanıcı değiştirir).
    """
    hisse = str(row.get("Hisse") or "?")
    sektor = str(row.get("Sektör") or "")

    # --- bileşen skorları 0-1 ---
    # DD
    try:
        gecen = float(row.get("DD Geçen"))
    except Exception:
        gecen = np.nan
    try:
        kalan = float(row.get("DD Kalan"))
    except Exception:
        kalan = np.nan
    if np.isfinite(gecen):
        s_dd = _clip01(gecen / 5.0)
        if np.isfinite(kalan) and kalan >= 2:
            s_dd = min(s_dd, 0.35)
    else:
        s_dd = np.nan

    # Final score
    try:
        fs = float(row.get("Risk Ayarlı Skor", row.get("Final Score")))
        s_score = _clip01(fs / 100.0)
    except Exception:
        s_score = np.nan

    # Shannon
    sh = str(row.get("Shannon Notu") or "")
    try:
        hnorm = float(row.get("Shannon H norm"))
    except Exception:
        hnorm = np.nan
    if sh == "NET SİNYAL":
        s_sh = 0.9
    elif sh == "KARIŞIK":
        s_sh = 0.55
    elif sh == "GÜRÜLTÜ":
        s_sh = 0.2
    else:
        s_sh = 0.4
    if np.isfinite(hnorm):
        s_sh = _clip01(0.5 * s_sh + 0.5 * (1.0 - hnorm))

    # Kelly Final (0.25 max full kelly → normalize; half-kelly ~0.125)
    try:
        kf = float(row.get("Kelly Final"))
        s_kelly = _clip01(kf / max(KELLY_F_MAX * (0.5 if KELLY_HALF else 1.0), 1e-6))
        if not np.isfinite(kf) or kf <= 0:
            s_kelly = 0.0
    except Exception:
        s_kelly = np.nan

    # Sharpe rf=0: map roughly [-0.5, 1.5] → [0,1]
    try:
        s0 = float(row.get("Sharpe Proxy (rf=0)"))
        s_sharpe = _clip01((s0 + 0.5) / 2.0)
    except Exception:
        s_sharpe = np.nan

    # Graham: kriter oranı + güvenlik marjı + net-net + F/K PD/DD
    try:
        g_rate = float(row.get("Graham Kriter Geçiş Oranı"))
    except Exception:
        g_rate = np.nan
    try:
        g_margin = float(row.get("Graham Güvenlik Marjı"))
    except Exception:
        g_margin = np.nan
    try:
        g_score_n = float(row.get("Graham Puan"))
        g_valid_n = float(row.get("Graham Geçerli Kriter"))
    except Exception:
        g_score_n = g_valid_n = np.nan
    net_net = str(row.get("Net-Net (NCAV) Durumu") or "")
    g_verdict = str(row.get("Değer Yatırımcılığı Sonucu") or "")
    g_detail = str(row.get("Graham Kriter Detay") or "")
    try:
        pe = float(row.get("F/K"))
    except Exception:
        pe = np.nan
    try:
        pb = float(row.get("PD/DD"))
    except Exception:
        pb = np.nan

    # Tek tek kriter yeniden (detay yoksa)
    if not g_detail or g_detail == "nan":
        parts = []
        if np.isfinite(pe):
            parts.append(f"G1 F/K≤15:{'GEÇTİ' if 0 < pe <= 15 else 'KALDI'}({pe:.1f})")
        else:
            parts.append("G1 F/K≤15:N/A")
        if np.isfinite(pb):
            parts.append(f"G2 PD/DD≤1.5:{'GEÇTİ' if 0 < pb <= 1.5 else 'KALDI'}({pb:.2f})")
        else:
            parts.append("G2 PD/DD≤1.5:N/A")
        if np.isfinite(pe) and np.isfinite(pb) and pe > 0 and pb > 0:
            prod = pe * pb
            parts.append(f"G3 F/K×PD/DD≤22.5:{'GEÇTİ' if prod <= 22.5 else 'KALDI'}({prod:.1f})")
        else:
            parts.append("G3 F/K×PD/DD≤22.5:N/A")
        g_detail = " | ".join(parts)
        # kalan G4-G7 sadece rate'ten
        if np.isfinite(g_rate):
            g_detail += f" | G4-G7 özet geçiş:{g_rate:.0%}"

    s_graham_parts = []
    if np.isfinite(g_rate):
        s_graham_parts.append(g_rate)
    if np.isfinite(g_margin):
        # %33+ marj → 1.0; 0 → 0.5; negatif → düşük
        s_graham_parts.append(_clip01((g_margin + 0.1) / 0.5))
    if "Net-Net (Derin Değer)" in net_net:
        s_graham_parts.append(1.0)
    elif "Net-Net Değil" in net_net:
        s_graham_parts.append(0.4)
    if "İskontolu (Değer Yatırımcılığına Uygun)" in g_verdict:
        s_graham_parts.append(1.0)
    elif "İzlemeye Değer" in g_verdict:
        s_graham_parts.append(0.65)
    elif "Uygun Değil" in g_verdict:
        s_graham_parts.append(0.25)
    # F/K PD/DD doğrudan
    if np.isfinite(pe) and pe > 0:
        s_graham_parts.append(1.0 if pe <= 15 else (0.5 if pe <= 25 else 0.15))
    if np.isfinite(pb) and pb > 0:
        s_graham_parts.append(1.0 if pb <= 1.5 else (0.5 if pb <= 2.5 else 0.15))
    s_graham = float(np.nanmean(s_graham_parts)) if s_graham_parts else np.nan

    # Graham metin notu
    graham_note = (
        f"Graham: {g_verdict or '—'} | geçiş={_fmt_pct(g_rate) if np.isfinite(g_rate) else 'n/a'} "
        f"({_fmt_num(g_score_n,0)}/{_fmt_num(g_valid_n,0)}) | "
        f"güvenlik marjı={_fmt_pct(g_margin) if np.isfinite(g_margin) else 'n/a'} | "
        f"F/K={_fmt_num(pe,1)} PD/DD={_fmt_num(pb,2)} | {net_net or 'NCAV n/a'} | {g_detail}"
    )

    # Value: adil marj + nakit getiri
    try:
        med = float(row.get("Adil Değer Marjı"))
    except Exception:
        med = np.nan
    try:
        fcf = float(row.get("FCF Getirisi"))
    except Exception:
        fcf = np.nan
    try:
        oe = float(row.get("Owner Earnings Getirisi"))
    except Exception:
        oe = np.nan
    vals = []
    if np.isfinite(med):
        vals.append(_clip01((med + 0.2) / 0.6))
    if np.isfinite(fcf):
        vals.append(_clip01(fcf / 0.15))
    if np.isfinite(oe):
        vals.append(_clip01(oe / 0.15))
    s_val = float(np.nanmean(vals)) if vals else np.nan

    # Stop: çok geniş stop (≥20%) zayıf, %5-12 ideal
    try:
        stop = float(row.get("Stop Loss %"))
        if stop <= 0.12:
            s_stop = 0.85
        elif stop <= 0.18:
            s_stop = 0.6
        else:
            s_stop = 0.35
    except Exception:
        s_stop = np.nan

    # Inverse: Evet → 0, Hayır → 1
    inv = str(row.get("Ters Sinyal") or "")
    s_inv = 0.0 if inv == "Evet" else 1.0

    # Kova bonus (bilgi)
    kova = str(row.get("Kova") or "")

    # v12 Zamanlama (teknik)
    try:
        tech = float(row.get("Teknik Skor"))
        s_timing = float(np.clip(tech / 100.0, 0, 1)) if np.isfinite(tech) else np.nan
    except Exception:
        s_timing = np.nan
    weights = {
        "DD": EVAL_W_DD,
        "Graham": EVAL_W_GRAHAM,
        "Skor": EVAL_W_SCORE,
        "Shannon": EVAL_W_SHANNON,
        "Kelly": EVAL_W_KELLY,
        "Sharpe": EVAL_W_SHARPE,
        "Value": EVAL_W_VALUE,
        "Stop": EVAL_W_STOP,
        "TersSinyal": EVAL_W_INVERSE,
        "Zamanlama": EVAL_W_TIMING if "EVAL_W_TIMING" in globals() else 0.10,
    }
    comps = {
        "DD": s_dd,
        "Graham": s_graham,
        "Skor": s_score,
        "Shannon": s_sh,
        "Kelly": s_kelly,
        "Sharpe": s_sharpe,
        "Value": s_val,
        "Stop": s_stop,
        "TersSinyal": s_inv,
        "Zamanlama": s_timing,
    }
    num = den = 0.0
    detail_parts = []
    for k, w in weights.items():
        v = comps[k]
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            detail_parts.append(f"{k}=n/a")
            continue
        num += w * float(v)
        den += w
        detail_parts.append(f"{k}={v:.2f}×{w:.2f}")
    score100 = float(np.clip(100.0 * num / den, 0, 100)) if den > 1e-9 else np.nan

    if np.isfinite(score100):
        if score100 >= 70 and kova.startswith("1"):
            not_lbl = "A — Güçlü bütünleşik"
        elif score100 >= 60:
            not_lbl = "B — İzlemeye değer"
        elif score100 >= 45:
            not_lbl = "C — Şartlı / zayıf edge"
        else:
            not_lbl = "D — Zayıf paket"
    else:
        not_lbl = "N/A"

    # Not metni
    dd_ax = str(row.get("DD Aksiyon") or "")[:40]
    try:
        kf_s = f"{float(row.get('Kelly Final')):.2%}"
    except Exception:
        kf_s = "n/a"
    try:
        sh0 = f"{float(row.get('Sharpe Proxy (rf=0)')):.2f}"
    except Exception:
        sh0 = "n/a"
    try:
        stop_s = f"{float(row.get('Stop Loss %')):.1%}"
    except Exception:
        stop_s = "n/a"

    degerlendirme = (
        f"{hisse} ({sektor or '—'}): Bütünleşik {_fmt_num(score100,1)}/100 ({not_lbl}) | "
        f"Graham:{g_verdict or '—'} F/K={_fmt_num(pe,1)} PD/DD={_fmt_num(pb,2)} geçiş={_fmt_pct(g_rate) if np.isfinite(g_rate) else 'n/a'} | "
        f"Kova:{kova or '—'} | Shannon:{sh or '—'} | Kelly:{kf_s} | Sharpe0:{sh0} | "
        f"Stop:{stop_s} | Ters:{inv or '—'} | DD:{dd_ax}"
    )

    derin = (
        f"BÜTÜNLEŞİK: {_fmt_num(score100,1)} ({not_lbl}). "
        f"Bileşenler: {', '.join(detail_parts)}. "
        f"{graham_note}. "
        f"Kova={kova or '—'}; Shannon={sh or '—'}; Kelly={kf_s}; Sharpe0={sh0}; "
        f"Stop={stop_s}; TersSinyal={inv or '—'}. "
        f"Ağırlıklar: DD={EVAL_W_DD}, Graham={EVAL_W_GRAHAM}, Skor={EVAL_W_SCORE}, "
        f"Shannon={EVAL_W_SHANNON}, Kelly={EVAL_W_KELLY}, Sharpe={EVAL_W_SHARPE}, "
        f"Value={EVAL_W_VALUE}, Stop={EVAL_W_STOP}, Ters={EVAL_W_INVERSE}."
    )

    # Aksiyon önerisi
    if inv == "Evet" and (not np.isfinite(score100) or score100 < 65):
        aks = "Ters sinyal aktif — boyut küçült / bekle"
    elif kova.startswith("1") and np.isfinite(score100) and score100 >= 65:
        aks = "Öncelik inceleme — dipnot + stop mesafesi ile"
    elif kova.startswith("2") or (np.isfinite(score100) and 50 <= score100 < 65):
        aks = "Şartlı — nakit/NAV teyidi, yarım pay"
    else:
        aks = "Bekle / izle — paket zayıf veya edge yok"

    return {
        "Bütünleşik Skor": None if not np.isfinite(score100) else round(score100, 2),
        "Bütünleşik Not": not_lbl,
        "Bütünleşik Aksiyon": aks,
        "Eval DD": None if not np.isfinite(s_dd) else round(float(s_dd), 3),
        "Eval Graham": None if not np.isfinite(s_graham) else round(float(s_graham), 3),
        "Eval Skor": None if not np.isfinite(s_score) else round(float(s_score), 3),
        "Eval Shannon": None if not np.isfinite(s_sh) else round(float(s_sh), 3),
        "Eval Kelly": None if not np.isfinite(s_kelly) else round(float(s_kelly), 3),
        "Eval Sharpe": None if not np.isfinite(s_sharpe) else round(float(s_sharpe), 3),
        "Eval Value": None if not np.isfinite(s_val) else round(float(s_val), 3),
        "Eval Stop": None if not np.isfinite(s_stop) else round(float(s_stop), 3),
        "Eval TersSinyal": s_inv,
        "Graham Notu": graham_note,
        "Graham Kriter Detay": g_detail,
        "Değerlendirme": degerlendirme,
        "Derin Analiz": derin,
    }


def enrich_all_with_composite_eval(df):
    """DD sonrası tüm risk katmanları hazırken bütünleşik not üretir; Değerlendirme/Derin Analiz güncellenir."""
    if df is None or df.empty:
        return df
    rows = [build_composite_evaluation_for_row(row) for _, row in df.iterrows()]
    ev = pd.DataFrame(rows)
    # Üzerine yaz
    for c in ev.columns:
        df[c] = ev[c].values
    return df


def enrich_stop_trailing(df):
    """Vol tabanlı stop-loss ve trailing stop önerisi (fiyat %).

    Stop mesafesi ≈ STOP_ATR_MULT * σ_daily * sqrt(20)  (~1 ay)
    Trailing ≈ TRAIL_ATR_MULT * σ_daily * sqrt(20)
    σ_ann Yahoo'dan geliyorsa σ_daily = σ_ann / sqrt(252)
    """
    if df is None or df.empty:
        return df
    sig = pd.to_numeric(df.get("Vol Ann (σ)", np.nan), errors="coerce")
    stops, trails, notes = [], [], []
    for s in sig.fillna(np.nan):
        if not np.isfinite(s) or s <= 0:
            stops.append(np.nan); trails.append(np.nan); notes.append("Stop: vol yok")
            continue
        sig_d = s / np.sqrt(252.0)
        # ~20 iş günü pencere
        stop_pct = float(STOP_ATR_MULT * sig_d * np.sqrt(20))
        trail_pct = float(TRAIL_ATR_MULT * sig_d * np.sqrt(20))
        stop_pct = float(np.clip(stop_pct, 0.03, 0.25))
        trail_pct = float(np.clip(trail_pct, 0.04, 0.30))
        stops.append(round(stop_pct, 4))
        trails.append(round(trail_pct, 4))
        notes.append(f"Stop -{stop_pct:.1%} | Trail {trail_pct:.1%} (σ_ann={s:.0%})")
    df["Stop Loss %"] = stops
    df["Trailing Stop %"] = trails
    df["Stop Notu"] = notes
    # Kelly ile birleşik önerilen max pay (sert tavan)
    if "Kelly Final" in df.columns:
        kf = pd.to_numeric(df["Kelly Final"], errors="coerce")
        df["Önerilen Pay %"] = [
            round(min(SINGLE_NAME_CAP, float(x)) * 100, 2) if np.isfinite(x) and x > 0 else 0.0
            for x in kf
        ]
    return df


def enrich_sharpe_proxy(df, rf_annual=None):
    """Hisse bazlı Sharpe proxy kolonları.

    Sharpe_rf = (μ_proxy - rf) / σ_ann
    Sharpe_0  = μ_proxy / σ_ann          (rf'siz; yüksek faiz rejiminde karşılaştırma için)
    μ_proxy öncelik: Shannon Edge → Adil Değer Marjı → OE/FCF ort.

    Not: μ edge/iskonto proxy'sidir; tarihsel ortalama getiri değildir.
    """
    if df is None or df.empty:
        return df
    rf = float(rf_annual) if rf_annual is not None else float(RISK_FREE_FALLBACK)

    mu = pd.Series(np.nan, index=df.index, dtype=float)
    if "Shannon Edge" in df.columns:
        mu = pd.to_numeric(df["Shannon Edge"], errors="coerce")
    if mu.isna().all() and "Adil Değer Marjı" in df.columns:
        mu = pd.to_numeric(df["Adil Değer Marjı"], errors="coerce")
    if mu.isna().any():
        parts = []
        for c in ("Owner Earnings Getirisi", "FCF Getirisi", "Adil Değer Marjı"):
            if c in df.columns:
                parts.append(pd.to_numeric(df[c], errors="coerce"))
        if parts:
            alt = pd.concat(parts, axis=1).mean(axis=1)
            mu = mu.fillna(alt)

    sig = pd.to_numeric(df["Vol Ann (σ)"], errors="coerce") if "Vol Ann (σ)" in df.columns else pd.Series(np.nan, index=df.index)

    sharpe_rf, sharpe_0, notes = [], [], []
    for m, s in zip(mu.fillna(np.nan), sig.fillna(np.nan)):
        if not np.isfinite(s) or s <= 1e-8:
            sharpe_rf.append(np.nan); sharpe_0.append(np.nan); notes.append("Sharpe: vol yok")
            continue
        if not np.isfinite(m):
            sharpe_rf.append(np.nan); sharpe_0.append(np.nan); notes.append("Sharpe: μ yok")
            continue
        s0 = float(m / s)
        srf = float((m - rf) / s)
        sharpe_0.append(round(s0, 4))
        sharpe_rf.append(round(srf, 4))
        notes.append(f"μ={m:.3f} σ={s:.3f} rf={rf:.2f} → S0={s0:.2f} Srf={srf:.2f}")
    df["Sharpe Proxy (rf=0)"] = sharpe_0
    df["Sharpe Proxy"] = sharpe_rf
    df["Sharpe Notu"] = notes
    df["Sharpe rf kullanılan"] = rf
    return df


def enrich_inverse_signal(df, corr_df=None):
    """Backtest ters sinyal / look-ahead uyarısını hisse satırına işler.

    - Model geneli: Final Score ↔ 6A Spearman < eşik → TersSinyal=Evet
    - Band: yüksek skor bandı ortalama getiri < düşük band → ek ceza
    - Look-ahead notu sabit (mevcut backtest tasarımı)
    """
    if df is None or df.empty:
        return df
    model_inverse = False
    spearman_fs = np.nan
    if corr_df is not None and not getattr(corr_df, "empty", True):
        try:
            sub = corr_df[
                (corr_df.get("Getiri") == "Backtest 6A İleri Getiri")
                & (corr_df.get("Bileşen") == "Final Score")
            ]
            if len(sub):
                spearman_fs = float(sub.iloc[0]["Spearman"])
                # v192: "Backtest İleri Getiri" aslında GEÇMİŞ getiridir (bugünkü skor × son 6/12/24 ay).
                # Düşmüş hisse bugünkü fiyata göre ucuz görünür → mekanik negatif ilişki. Bu yüzden
                # otomatik ceza yalnızca INVERSE_SIGNAL_ENABLED=True iken uygulanır (gerçek ileri veri gelince).
                model_inverse = bool(INVERSE_SIGNAL_ENABLED and spearman_fs < INVERSE_SIGNAL_CORR_THR)
        except Exception:
            pass
    # Band ortalamaları
    band_penalty = {}
    if INVERSE_SIGNAL_ENABLED and "Final Score Band" in df.columns and "Backtest 6A İleri Getiri" in df.columns:
        try:
            g = df.groupby("Final Score Band")["Backtest 6A İleri Getiri"].mean()
            low = g.get("<50", np.nan)
            for b, m in g.items():
                # yüksek skor bandı düşük banddan kötüyse ceza
                if str(b) in ("66-74", "74-82", "82-90", "90+") and np.isfinite(m) and np.isfinite(low) and m < low:
                    band_penalty[str(b)] = True
                else:
                    band_penalty[str(b)] = False
        except Exception:
            pass

    flags, notes, score_adj = [], [], []
    for _, row in df.iterrows():
        band = str(row.get("Final Score Band") or "")
        inv = model_inverse or band_penalty.get(band, False)
        flags.append("Evet" if inv else "Hayır")
        parts = [
            "Backtest = GEÇMİŞ getiri × bugünkü skor (look-ahead); ters sinyal cezası "
            + ("AÇIK" if INVERSE_SIGNAL_ENABLED else "KAPALI (bilgi amaçlı)")
        ]
        if np.isfinite(spearman_fs):
            parts.append(f"FS↔6A Spearman={spearman_fs:.3f}" + (" → ters sinyal" if model_inverse else ""))
        if band_penalty.get(band):
            parts.append(f"Band {band} 6A ortalaması <50 bandından zayıf")
        if inv:
            parts.append("Öneri: boyutu küçült / kova 3'e kaydır / saf skora güvenme")
        notes.append(" | ".join(parts))
        # skor cezası bilgi amaçlı (Final Score'u bozmadan ayrı kolon)
        score_adj.append(-5.0 if inv else 0.0)
    df["Ters Sinyal"] = flags
    df["Ters Sinyal Notu"] = notes
    df["Ters Sinyal Cezası"] = score_adj
    # Kelly Final varsa ters sinyalde yarıla
    if "Kelly Final" in df.columns:
        kf = pd.to_numeric(df["Kelly Final"], errors="coerce")
        inv_s = df["Ters Sinyal"] == "Evet"
        df["Kelly Final"] = [
            round(float(x) * 0.5, 4) if inv and np.isfinite(x) else (round(float(x), 4) if np.isfinite(x) else np.nan)
            for x, inv in zip(kf, inv_s)
        ]
    return df


def _cov_from_vols(vols, rho=0.35):
    """Sadece vol vektörü varken sabit korelasyonlu kovaryans (yıllık)."""
    v = np.array(vols, dtype=float)
    n = len(v)
    corr = np.full((n, n), rho)
    np.fill_diagonal(corr, 1.0)
    # negatif/NaN vol → medyan
    med = np.nanmedian(v[v > 0]) if np.any(v > 0) else 0.3
    v = np.where(np.isfinite(v) & (v > 0), v, med)
    D = np.diag(v)
    return D @ corr @ D


def build_portfolio_allocation(cand_df, rf=None, n_target=None):
    """Değer portföyü: Tier A/B adaylardan tam PORTFOLIO_SIZE (15) hisse.

    1) Adayları ValueInvestScore / Aday Skoru ile sırala
    2) Tam n_target=15 hisse (Tier A/B + karşılama oranı tamamlaması)
    3) Risk Parity + Markowitz + Blended ağırlık
    4) Kelly + VIS tilt; tek hisse/sektör tavanı
    """
    if cand_df is None or cand_df.empty or "Hisse" not in cand_df.columns:
        return pd.DataFrame([{"Not": "Aday yok — portföy üretilemedi"}]), pd.DataFrame()

    n_target = int(n_target or globals().get("PORTFOLIO_SIZE", 15))
    d = cand_df.copy()

    # --- 15 hisse seçimi ---
    sort_cols = [c for c in [
        "ValueInvestScore", "Aday Skoru", "GrahamBuffettScore", "Final Score", "Kelly Final"
    ] if c in d.columns]
    if sort_cols:
        d = d.sort_values(sort_cols, ascending=[False] * len(sort_cols))
    # Tier A önce
    if "Aday Modu" in d.columns:
        d["_tier"] = d["Aday Modu"].astype(str).str.startswith("Tier A").astype(int)
        d = d.sort_values(["_tier"] + sort_cols, ascending=[False] + [False] * len(sort_cols))
        d = d.drop(columns=["_tier"], errors="ignore")
    # sektör çeşitliliği (max 2)
    if "Sektör" in d.columns and len(d) > n_target:
        picked, sec_n = [], {}
        for _, row in d.iterrows():
            sec = str(row.get("Sektör") or "Diğer")[:28]
            if sec_n.get(sec, 0) >= 2:
                continue
            picked.append(row)
            sec_n[sec] = sec_n.get(sec, 0) + 1
            if len(picked) >= n_target:
                break
        if len(picked) < n_target:
            have = set(str(x["Hisse"]) for x in picked)
            for _, row in d.iterrows():
                if str(row["Hisse"]) in have:
                    continue
                picked.append(row)
                if len(picked) >= n_target:
                    break
        d = pd.DataFrame(picked)
    else:
        d = d.head(n_target).copy()

    if d.empty:
        return pd.DataFrame([{"Not": "Portföy için hisse seçilemedi"}]), pd.DataFrame()

    # Beklenen getiri proxy: VIS/100 + edge/marj
    mu = np.zeros(len(d))
    if "ValueInvestScore" in d.columns:
        mu = mu + 0.5 * (pd.to_numeric(d["ValueInvestScore"], errors="coerce").fillna(50).values / 100.0)
    if "Shannon Edge" in d.columns:
        mu = mu + 0.3 * np.clip(pd.to_numeric(d["Shannon Edge"], errors="coerce").fillna(0.0).values, 0, 1)
    if "Adil Değer Marjı" in d.columns:
        mu = mu + 0.2 * np.clip(pd.to_numeric(d["Adil Değer Marjı"], errors="coerce").fillna(0.0).values, 0, 1)
    if mu.sum() <= 1e-12:
        mu = np.ones(len(d)) / len(d)
    mu = np.clip(mu, 0.0, None)

    if "Vol Ann (σ)" in d.columns:
        vols = pd.to_numeric(d["Vol Ann (σ)"], errors="coerce").values
    else:
        vols = np.full(len(d), 0.35)
    vols = np.where(np.isfinite(vols) & (vols > 0.05), vols, 0.35)

    names = d["Hisse"].astype(str).tolist()
    n = len(names)
    if n == 0:
        return pd.DataFrame([{"Not": "Aday yok"}]), pd.DataFrame()

    # --- Risk Parity (ters vol, sonra sektör tavanı) ---
    inv = 1.0 / vols
    w_rp = inv / inv.sum()

    # --- Markowitz max Sharpe (uzun only, eşit grid / analytic Σ^{-1} μ) ---
    cov = _cov_from_vols(vols, rho=0.35)
    try:
        cov_reg = cov + np.eye(n) * 1e-6
        inv_cov = np.linalg.pinv(cov_reg)
        raw = inv_cov @ mu
        raw = np.clip(raw, 0.0, None)
        if raw.sum() <= 1e-12:
            w_mv = np.ones(n) / n
        else:
            w_mv = raw / raw.sum()
    except Exception:
        w_mv = np.ones(n) / n

    # v192.3: Kelly Final ağırlık için ayırt edici değildi (çoğu 0 veya aynı tavan değeri).
    # Eğim: aday skoru yüzdelik sırası × tier çarpanı; risk parity / Markowitz temeli korunur.
    def _score_tilt(dd):
        sc = pd.to_numeric(dd["Aday Skoru"], errors="coerce") if "Aday Skoru" in dd.columns else pd.Series(np.nan, index=dd.index)
        base = sc.rank(pct=True).fillna(0.5).values
        modu = dd["Aday Modu"].astype(str) if "Aday Modu" in dd.columns else pd.Series("", index=dd.index)
        tm = np.array([SIZING_TIER_MULT["A"] if x.startswith("Tier A") else SIZING_TIER_MULT["B"] if x.startswith("Tier B")
                       else SIZING_TIER_MULT["C"] if x.startswith("Tier C") else 1.0 for x in modu])
        return (0.5 + 0.5 * base) * tm
    _tilt = _score_tilt(d)
    w_rp = w_rp * _tilt
    w_rp = w_rp / w_rp.sum()
    w_mv = w_mv * _tilt
    w_mv = w_mv / w_mv.sum()

    # Tek hisse + sektör tavanı
    def apply_caps(w):
        """Tek hisse/sektör tavanı; aşan kısım NAKİT (yeniden normalize ile tavanı delme)."""
        w = np.clip(np.array(w, dtype=float), 0.0, None)
        if w.sum() <= 0:
            return np.ones(n) / n
        w = w / w.sum()
        sectors = d["Sektör"].astype(str).values if "Sektör" in d.columns else np.array(["_"] * n)
        for _ in range(8):
            w = np.minimum(w, SINGLE_NAME_CAP)
            for sec in np.unique(sectors):
                mask = sectors == sec
                s = w[mask].sum()
                if s > SECTOR_CAP + 1e-12:
                    w[mask] *= SECTOR_CAP / s
            # toplam >1 olmamalı; <1 ise nakit
            if w.sum() > 1.0:
                w = w / w.sum()
        return w

    w_rp = apply_caps(w_rp)
    w_mv = apply_caps(w_mv)
    # Blended
    w_bl = apply_caps(0.5 * w_rp + 0.5 * w_mv)

    rows = []
    rf_use = float(rf) if rf is not None else float(RISK_FREE_FALLBACK)
    for i, name in enumerate(names):
        sig_i = float(vols[i])
        mu_i = float(mu[i])
        s0 = (mu_i / sig_i) if sig_i > 1e-8 else np.nan
        srf = ((mu_i - rf_use) / sig_i) if sig_i > 1e-8 else np.nan
        rows.append({
            "Hisse": name,
            "Sektör": d.iloc[i].get("Sektör", ""),
            "μ proxy": round(mu_i, 4),
            "σ ann": round(sig_i, 4),
            "Sharpe Proxy (rf=0)": None if not np.isfinite(s0) else round(s0, 4),
            "Sharpe Proxy": None if not np.isfinite(srf) else round(srf, 4),
            "Risk Parity %": round(float(w_rp[i]) * 100, 2),
            "Markowitz %": round(float(w_mv[i]) * 100, 2),
            "Blended %": round(float(w_bl[i]) * 100, 2),
            "Kelly Final": d.iloc[i].get("Kelly Final", np.nan),
            "Stop Loss %": d.iloc[i].get("Stop Loss %", np.nan),
            "Trailing Stop %": d.iloc[i].get("Trailing Stop %", np.nan),
            "Ters Sinyal": d.iloc[i].get("Ters Sinyal", ""),
            "Kova": d.iloc[i].get("Kova", ""),
        })
    port = pd.DataFrame(rows)
    # Özet
    def port_vol(w):
        return float(np.sqrt(max(w @ cov @ w, 0.0)))
    def port_sharpe(w):
        vol = port_vol(w)
        er = float(w @ mu)
        if vol <= 1e-12:
            return np.nan, np.nan
        return (er / vol), ((er - rf_use) / vol)
    s0_rp, srf_rp = port_sharpe(w_rp)
    s0_mv, srf_mv = port_sharpe(w_mv)
    s0_bl, srf_bl = port_sharpe(w_bl)
    summary = pd.DataFrame([
        {"Metrik": "N aday", "Risk Parity": n, "Markowitz": n, "Blended": n},
        {"Metrik": "Σ hisse %", "Risk Parity": round(float(w_rp.sum())*100,2), "Markowitz": round(float(w_mv.sum())*100,2), "Blended": round(float(w_bl.sum())*100,2)},
        {"Metrik": "Nakit % (tavan artığı)", "Risk Parity": round((1-float(w_rp.sum()))*100,2), "Markowitz": round((1-float(w_mv.sum()))*100,2), "Blended": round((1-float(w_bl.sum()))*100,2)},
        {"Metrik": "Proxy portföy σ", "Risk Parity": round(port_vol(w_rp), 4),
         "Markowitz": round(port_vol(w_mv), 4), "Blended": round(port_vol(w_bl), 4)},
        {"Metrik": "Proxy E[r]", "Risk Parity": round(float(w_rp @ mu), 4),
         "Markowitz": round(float(w_mv @ mu), 4), "Blended": round(float(w_bl @ mu), 4)},
        {"Metrik": "Sharpe Proxy (rf=0)", "Risk Parity": None if not np.isfinite(s0_rp) else round(s0_rp, 4),
         "Markowitz": None if not np.isfinite(s0_mv) else round(s0_mv, 4),
         "Blended": None if not np.isfinite(s0_bl) else round(s0_bl, 4)},
        {"Metrik": f"Sharpe Proxy (rf={rf_use:.2f})", "Risk Parity": None if not np.isfinite(srf_rp) else round(srf_rp, 4),
         "Markowitz": None if not np.isfinite(srf_mv) else round(srf_mv, 4),
         "Blended": None if not np.isfinite(srf_bl) else round(srf_bl, 4)},
        {"Metrik": "Tek hisse tavan", "Risk Parity": f"%{SINGLE_NAME_CAP*100:.0f}",
         "Markowitz": f"%{SINGLE_NAME_CAP*100:.0f}", "Blended": f"%{SINGLE_NAME_CAP*100:.0f}"},
        {"Metrik": "Sektör tavan", "Risk Parity": f"%{SECTOR_CAP*100:.0f}",
         "Markowitz": f"%{SECTOR_CAP*100:.0f}", "Blended": f"%{SECTOR_CAP*100:.0f}"},
        {"Metrik": "Korelasyon varsayımı", "Risk Parity": 0.35, "Markowitz": 0.35, "Blended": 0.35},
        {"Metrik": "Hedef hisse", "Risk Parity": n_target, "Markowitz": n_target, "Blended": n_target},
        {"Metrik": "Uyarı", "Risk Parity": "Tahmini kovaryans", "Markowitz": "Short yok, μ=VIS+edge",
         "Blended": "15 hisse değer portföyü — tavsiye değildir"},
    ])
    return port, summary



# =============================================================================
# v12: KAP Yeni İş İlişkisi (son N gün) + Model Sağlık + Adjusted Score
# =============================================================================

KAP_YENI_IS_KEYWORDS = (
    "yeni iş ilişkisi", "yeni is iliskisi", "yeni sözleşme", "yeni sozlesme",
    "sözleşme imza", "sozlesme imza", "iş alma", "is alma", "ihale",
    "sözleşme imzalanması", "sözleşme imzalandı", "müşteri sözleşmesi",
    "tedarik sözleşmesi", "taahhütname", "iş ilişkisine başlan",
)

def fetch_kap_disclosures_recent(session, days=2, max_rows=400):
    """KAP son N gün bildirimlerini best-effort çeker.

    Resmi abonelik API'si yoktur; halka açık JSON uç noktaları denenir.
    Başarısız olursa boş DataFrame + uyarı döner (motoru bozmaz).
    """
    from datetime import timedelta
    end = datetime.now().date()
    start = end - timedelta(days=max(1, int(days)))
    headers = {
        **HEAD,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.kap.org.tr",
        "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    }
    rows = []
    errors = []

    # 1) afterDisclosureIndex akışı (kap-notifier tarzı)
    try:
        r = session.get(
            "https://www.kap.org.tr/tr/api/disclosures",
            headers=headers, timeout=18,
        )
        if r.ok:
            data = r.json()
            items = data if isinstance(data, list) else data.get("data") or data.get("disclosures") or []
            for it in items:
                basic = it.get("basic") if isinstance(it.get("basic"), dict) else it
                pub = basic.get("publishDate") or basic.get("disclosureDate") or it.get("publishDate") or ""
                title = (
                    basic.get("title") or basic.get("subject") or basic.get("disclosureTitle")
                    or it.get("title") or it.get("subject") or ""
                )
                stock = (
                    basic.get("stockCodes") or basic.get("stockCode") or basic.get("memberCode")
                    or it.get("stockCodes") or it.get("stockCode") or ""
                )
                company = basic.get("companyName") or basic.get("memberTitle") or basic.get("memberName") or ""
                idx = basic.get("disclosureIndex") or basic.get("id") or it.get("disclosureIndex") or ""
                rows.append({
                    "Tarih": str(pub),
                    "Hisse": str(stock).split(",")[0].strip().upper() if stock else "",
                    "Şirket": company,
                    "Başlık": title,
                    "KAP Index": idx,
                    "URL": f"https://www.kap.org.tr/tr/Bildirim/{idx}" if idx else "",
                    "Kaynak": "api/disclosures",
                })
        else:
            errors.append(f"api/disclosures HTTP {r.status_code}")
    except Exception as e:
        errors.append(f"api/disclosures: {type(e).__name__}: {e}")

    # 2) memberDisclosureQuery POST (tarih aralığı)
    if len(rows) < 20:
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            payload = {
                "fromDate": start.strftime(fmt),
                "toDate": end.strftime(fmt),
            }
            for url in (
                "https://www.kap.org.tr/tr/api/memberDisclosureQuery",
                "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria",
            ):
                try:
                    r = session.post(url, headers=headers, json=payload, timeout=18)
                    if not r.ok:
                        errors.append(f"{url} HTTP {r.status_code}")
                        continue
                    data = r.json()
                    items = data if isinstance(data, list) else (
                        data.get("data") or data.get("result") or data.get("disclosures") or []
                    )
                    for d in items:
                        pub = d.get("publishDate") or d.get("disclosureDate") or d.get("published_at") or ""
                        title = d.get("disclosureTitle") or d.get("subject") or d.get("title") or d.get("kapTitle") or ""
                        stock = d.get("stockCode") or d.get("stockCodes") or d.get("memberCode") or ""
                        company = d.get("companyName") or d.get("memberName") or d.get("memberTitle") or ""
                        idx = d.get("disclosureIndex") or d.get("id") or d.get("kap_id") or ""
                        rows.append({
                            "Tarih": str(pub),
                            "Hisse": str(stock).split(",")[0].strip().upper() if stock else "",
                            "Şirket": company,
                            "Başlık": title,
                            "KAP Index": idx,
                            "URL": f"https://www.kap.org.tr/tr/Bildirim/{idx}" if idx else "",
                            "Kaynak": url.split("/")[-1],
                        })
                    if items:
                        break
                except Exception as e:
                    errors.append(f"{url}: {type(e).__name__}: {e}")
            if rows:
                break

    df = pd.DataFrame(rows)
    if df.empty:
        note = "KAP erişilemedi veya son {0} günde bildirim yok. ".format(days)
        note += (" | ".join(errors[:3]) if errors else "Ağ/timeout.")
        return pd.DataFrame([{
            "Tarih": "", "Hisse": "", "Şirket": "", "Başlık": note,
            "KAP Index": "", "URL": "https://www.kap.org.tr/tr/bildirim-sorgu",
            "Kaynak": "UYARI", "Yeni İş?": "—",
        }]), note

    # dedupe
    if "KAP Index" in df.columns:
        df = df.drop_duplicates(subset=["KAP Index"], keep="first")
    df = df.head(max_rows)

    def _is_yeni_is(title):
        t = norm(title)
        return any(k in t for k in KAP_YENI_IS_KEYWORDS)

    df["Yeni İş?"] = df["Başlık"].map(lambda x: "EVET" if _is_yeni_is(x) else "hayır")
    # tarih sıralama (string; best-effort)
    try:
        df["_ts"] = pd.to_datetime(df["Tarih"], dayfirst=True, errors="coerce")
        df = df.sort_values("_ts", ascending=False).drop(columns=["_ts"])
    except Exception:
        pass
    note = f"KAP son {days} gün: {len(df)} bildirim, Yeni İş={int((df['Yeni İş?']=='EVET').sum())}"
    return df.reset_index(drop=True), note


def parse_kap_yeni_is_detail(session, disclosure_index, timeout=12):
    """Bildirim detayından sözleşme tutarı ve satış payını best-effort çeker.

    KAP HTML/JSON yapısı değişebilir; bulamazsa boş döner.
    """
    out = {
        "Sözleşme Tutarı": "",
        "Para Birimi": "",
        "Satışlara Pay %": np.nan,
        "Müşteri/Karşı Taraf": "",
        "Özet": "",
    }
    if not disclosure_index:
        return out
    idx = str(disclosure_index).strip()
    headers = {
        **HEAD,
        "Accept": "text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    }
    texts = []
    for url in (
        f"https://www.kap.org.tr/tr/Bildirim/{idx}",
        f"https://www.kap.org.tr/tr/api/notification/{idx}",
        f"https://www.kap.org.tr/tr/api/disclosure/detail/{idx}",
    ):
        try:
            r = session.get(url, headers=headers, timeout=timeout)
            if not r.ok or not r.text:
                continue
            body = r.text
            texts.append(body)
            # JSON ise düzleştir
            if "application/json" in (r.headers.get("content-type") or "") or body.lstrip().startswith(("{", "[")):
                try:
                    j = r.json()
                    texts.append(json.dumps(j, ensure_ascii=False))
                except Exception:
                    pass
            if len(body) > 500:
                break
        except Exception:
            continue
    if not texts:
        return out
    blob = "\n".join(texts)
    blob_l = blob.lower()

    # Tutar: 1.234.567,89 TL / USD / EUR veya 5.355.000 ABD Doları
    amount_patterns = [
        r'(?:toplam\s*)?(?:bedel|tutar|sözleşme\s*bedeli|sozlesme\s*bedeli|sözleşmenin\s*toplam\s*bedeli)[^\d]{0,40}([\d\.\,]+)\s*(abd\s*dolar[ıi]|usd|eur|euro|tl|try|₺|\$|€)',
        r'([\d\.\,]+)\s*(abd\s*dolar[ıi]|usd|eur|euro)\s*(?:\+\s*kdv)?',
        r'(?:bedel|tutar)[^\d]{0,20}([\d\.\,]+)\s*(tl|try|₺)',
    ]
    for pat in amount_patterns:
        m = re.search(pat, blob_l, re.I)
        if m:
            out["Sözleşme Tutarı"] = m.group(1).strip()
            cur = m.group(2).upper().replace("İ", "I")
            if "DOLAR" in cur or "USD" in cur or "$" in cur:
                out["Para Birimi"] = "USD"
            elif "EUR" in cur or "EURO" in cur or "€" in cur:
                out["Para Birimi"] = "EUR"
            else:
                out["Para Birimi"] = "TL"
            break

    # Satış payı %
    pct_patterns = [
        r'(?:net\s*satış|hasilat|ciro|satışlar|satislar)[^\d%]{0,60}(?:içindeki\s*pay[ıi]|pay[ıi])[^\d%]{0,20}(%?\s*[\d\,\.]+)\s*%?',
        r'pay[ıi]\s*[:=]?\s*%?\s*([\d\,\.]+)\s*%',
        r'%\s*([\d\,\.]+)\s*(?:oranında|pay)',
    ]
    for pat in pct_patterns:
        m = re.search(pat, blob_l, re.I)
        if m:
            raw = m.group(1).replace("%", "").replace(",", ".").strip()
            try:
                v = float(raw)
                if v > 100:  # bazen 0.15 yazılır
                    v = v / 100.0 if v <= 10000 else np.nan
                if v > 1.5:  # yüzde puan
                    out["Satışlara Pay %"] = v / 100.0 if v <= 100 else np.nan
                else:
                    out["Satışlara Pay %"] = v
            except Exception:
                pass
            if pd.notna(out["Satışlara Pay %"]):
                break

    # Müşteri satırı
    m = re.search(
        r'(?:müşteri|musteri|tedarikçi|tedarikci|karşı\s*taraf|karsi\s*taraf)[^\n<:]{0,40}[:\n]\s*([^\n<]{3,120})',
        blob, re.I,
    )
    if m:
        out["Müşteri/Karşı Taraf"] = re.sub(r'\s+', ' ', m.group(1)).strip()[:120]

    # Kısa özet: "Açıklamalar" sonrası ilk cümle
    m = re.search(r'Açıklamalar\s*[:\n]\s*(.{40,400}?)(?:\n\n|Kamuoyu|Saygılarımız|</)', blob, re.I | re.S)
    if m:
        out["Özet"] = re.sub(r'\s+', ' ', m.group(1)).strip()[:280]
    return out


def build_kap_yeni_is_sheet(session, days=2, cand_tickers=None, max_detail=40):
    """Excel 'KAP Yeni İş' — yalnızca Yeni İş İlişkisi bildirimleri (son N gün).

    Kolonlar: Tarih, Hisse, Şirket, Başlık, Sözleşme Tutarı, Para Birimi,
    Satışlara Pay %, Müşteri, KAP Link (tıklanabilir), Özet.
    """
    all_df, note = fetch_kap_disclosures_recent(session, days=days)
    if all_df.empty or (len(all_df) == 1 and str(all_df.iloc[0].get("Kaynak")) == "UYARI"):
        return pd.DataFrame([{
            "Tarih": "", "Hisse": "", "Şirket": "", "Başlık": note,
            "Sözleşme Tutarı": "", "Para Birimi": "", "Satışlara Pay %": "",
            "Müşteri/Karşı Taraf": "", "KAP Link": "https://www.kap.org.tr/tr/bildirim-sorgu",
            "Özet": "KAP erişim yok veya yeni iş bildirimi yok",
        }]), note

    yeni = all_df[all_df.get("Yeni İş?", pd.Series(dtype=str)) == "EVET"].copy()
    if yeni.empty:
        # Başlıkta yakalanamadıysa anahtar kelimeyi tekrar dene
        mask = all_df["Başlık"].fillna("").map(lambda x: any(k in norm(x) for k in KAP_YENI_IS_KEYWORDS))
        yeni = all_df.loc[mask].copy()
    if yeni.empty:
        return pd.DataFrame([{
            "Tarih": "", "Hisse": "", "Şirket": "", "Başlık": f"Son {days} günde Yeni İş İlişkisi bildirimi yok",
            "Sözleşme Tutarı": "", "Para Birimi": "", "Satışlara Pay %": "",
            "Müşteri/Karşı Taraf": "", "KAP Link": "https://www.kap.org.tr/tr/bildirim-sorgu",
            "Özet": note,
        }]), f"KAP son {days} gün: 0 yeni iş ({note})"

    if cand_tickers:
        tick = set(str(x).upper() for x in cand_tickers)
        yeni["_r"] = yeni["Hisse"].map(lambda h: 0 if str(h).upper() in tick else 1)
        yeni = yeni.sort_values(["_r", "Tarih"], ascending=[True, False]).drop(columns=["_r"], errors="ignore")

    rows = []
    detail_n = 0
    for _, r in yeni.iterrows():
        idx = r.get("KAP Index") or ""
        url = r.get("URL") or (f"https://www.kap.org.tr/tr/Bildirim/{idx}" if idx else "https://www.kap.org.tr/tr/bildirim-sorgu")
        detail = {k: "" for k in ("Sözleşme Tutarı", "Para Birimi", "Satışlara Pay %", "Müşteri/Karşı Taraf", "Özet")}
        if idx and detail_n < max_detail:
            try:
                detail = parse_kap_yeni_is_detail(session, idx)
                detail_n += 1
            except Exception as e:
                detail["Özet"] = f"detay okunamadı: {type(e).__name__}"
        pay = detail.get("Satışlara Pay %", np.nan)
        pay_s = ""
        if pay is not None and pay != "" and pd.notna(pay):
            try:
                pay_s = f"{float(pay):.1%}"
            except Exception:
                pay_s = str(pay)
        rows.append({
            "Tarih": r.get("Tarih", ""),
            "Hisse": r.get("Hisse", ""),
            "Şirket": r.get("Şirket", ""),
            "Başlık": r.get("Başlık", ""),
            "Sözleşme Tutarı": detail.get("Sözleşme Tutarı") or "",
            "Para Birimi": detail.get("Para Birimi") or "",
            "Satışlara Pay %": pay_s,
            "Müşteri/Karşı Taraf": detail.get("Müşteri/Karşı Taraf") or "",
            "KAP Link": url,
            "Özet": detail.get("Özet") or "",
        })
    out = pd.DataFrame(rows)
    note2 = f"KAP son {days} gün — yalnızca Yeni İş: {len(out)} bildirim (detay çekilen {detail_n})"
    return out, note2



def enrich_capex_oe_penalty(df):
    """v12: CapEx/Amortisman yüksekse Owner Earnings güvenini ve nakit skorunu cezalandır."""
    if df is None or df.empty:
        return df
    cap = pd.to_numeric(df.get("CapEx/Amortisman"), errors="coerce")
    oe = pd.to_numeric(df.get("Owner Earnings Getirisi"), errors="coerce")
    fcf = pd.to_numeric(df.get("FCF Getirisi"), errors="coerce")
    notes = []
    adj_oe = []
    for c, o, f in zip(cap.fillna(np.nan), oe.fillna(np.nan), fcf.fillna(np.nan)):
        if np.isfinite(c) and c >= CAPEX_AMORT_OE_PENALTY_THR:
            # OE'yi FCF'ye doğru çek (yüksek capex → OE abartılı olabilir)
            if np.isfinite(o) and np.isfinite(f):
                blended = 0.35 * o + 0.65 * f
            elif np.isfinite(f):
                blended = f
            else:
                blended = o * 0.5 if np.isfinite(o) else np.nan
            adj_oe.append(blended)
            notes.append(f"CapEx/Amort={c:.1f}≥{CAPEX_AMORT_OE_PENALTY_THR} → OE güveni düşürüldü")
        else:
            adj_oe.append(o if np.isfinite(o) else np.nan)
            notes.append("")
    df["OE Getiri (CapEx Ayarlı)"] = adj_oe
    df["CapEx OE Notu"] = notes
    return df


def enrich_adjusted_score_v12(df, corr_df=None):
    """v12: Ters sinyalde Value ağırlığını düşüren Adjusted Score + Temel/Zamanlama ayrımı.

    Final Score bozulmaz; 'Adjusted Score' ve 'Zamanlama Skoru' ek kolonlar üretir.
    """
    if df is None or df.empty:
        return df
    inv = df.get("Ters Sinyal", pd.Series(["Hayır"] * len(df))).astype(str) == "Evet"
    fs = pd.to_numeric(df.get("Final Score"), errors="coerce")
    tech = pd.to_numeric(df.get("Teknik Skor"), errors="coerce")
    conf = pd.to_numeric(df.get("Confluence Skoru (Orta)"), errors="coerce")
    # Adjusted: ters sinyalde skoru kıs (Value bileşeni zayıf edge)
    adj = []
    for score, is_inv in zip(fs.fillna(np.nan), inv):
        if not np.isfinite(score):
            adj.append(np.nan)
        elif is_inv:
            adj.append(round(float(score) * INVERSE_VALUE_WEIGHT_MULT, 2))
        else:
            adj.append(round(float(score), 2))
    df["Adjusted Score"] = adj
    # Zamanlama skoru: teknik + confluence orta
    timing = []
    for t, c in zip(tech.fillna(np.nan), conf.fillna(np.nan)):
        vals = [x for x in (t, c) if np.isfinite(x)]
        timing.append(round(float(np.mean(vals)), 1) if vals else np.nan)
    df["Zamanlama Skoru"] = timing
    # Temel skor = Final Score (açık isim)
    df["Temel Skor"] = fs
    return df


def build_model_health_sheet(out, corr_df, cand, kap_note="", n_fetch_errors=None):
    """Dashboard yanına Model Sağlık özeti."""
    rows = []
    n = len(out) if out is not None else 0
    rows.append(["Analiz edilen hisse", n])
    if out is not None and "Kova" in out.columns:
        vc = out["Kova"].astype(str).value_counts()
        for k, v in vc.items():
            rows.append([f"Kova: {k}", int(v)])
    if cand is not None and not getattr(cand, "empty", True):
        rows.append(["Aday Listesi (adet)", len(cand)])
        if "Kova" in cand.columns:
            rows.append(["Adaylarda Öncelik (1)", int((cand["Kova"].astype(str).str.startswith("1")).sum())])
            rows.append(["Adaylarda Şartlı (2)", int((cand["Kova"].astype(str).str.startswith("2")).sum())])
    if corr_df is not None and not getattr(corr_df, "empty", True):
        try:
            sub = corr_df[(corr_df.get("Getiri") == "Backtest 6A İleri Getiri") & (corr_df.get("Bileşen") == "Final Score")]
            if len(sub):
                sp = float(sub.iloc[0]["Spearman"])
                rows.append(["FS↔6A Spearman", round(sp, 4)])
                rows.append(["Ters sinyal (model)", ("EVET" if sp < INVERSE_SIGNAL_CORR_THR else "HAYIR") + ("" if INVERSE_SIGNAL_ENABLED else " (ceza KAPALI — backtest = geçmiş getiri)")])
        except Exception:
            pass
    if out is not None and "Ters Sinyal" in out.columns:
        rows.append(["Ters sinyal hisse adedi", int((out["Ters Sinyal"] == "Evet").sum())])
    if out is not None and "Ana Sıralamada" in out.columns:
        fin = out[out["Ana Sıralamada"].astype(str).str.startswith("Evet")]
        rows.append(["Finansal kurum / kapsam dışı (evren içi)", int(len(out) - len(fin))])
    try:
        _vl = build_value_investing_list(out) if out is not None else None
        if _vl is not None and len(_vl):
            rows.append(["Değer Yatırımı listesi (adet)", int(len(_vl))])
            rows.append(["  ↳ uygun hisse / 'Tam' eşleşen (≥%d/13)" % VALUE_MIN_CRIT, f"{_vl.attrs.get('n_eligible')} / {_vl.attrs.get('n_tam')}"])
    except Exception:
        pass
    if out is not None and "Marj Yönü" in out.columns:
        _my = out["Marj Yönü"].astype(str).value_counts()
        rows.append(["Marj Yönü: İyileşiyor / Kötüleşiyor / Yatay", f"{_my.get('İyileşiyor', 0)} / {_my.get('Kötüleşiyor', 0)} / {_my.get('Yatay', 0)}"])
    if out is not None and "Döviz Riski" in out.columns:
        _dr = out["Döviz Riski"].astype(str).value_counts()
        rows.append(["Döviz: Yüksek Açık Riski (adet)", int(_dr.get("Yüksek Açık Riski", 0))])
    if out is not None and "GYO NAV/Hisse (Deneysel)" in out.columns:
        rows.append(["GYO NAV hesaplanabilen hisse (adet)", int(pd.to_numeric(out["GYO NAV/Hisse (Deneysel)"], errors="coerce").notna().sum())])
    if out is not None:
        for _key, _builder in [("Magic Formula", build_magic_formula_list), ("Momentum", build_momentum_list),
                                ("Temettü", build_dividend_list), ("Düşük Volatilite", build_low_vol_list),
                                ("Sektöre Göre Ucuz", build_sector_relative_list)]:
            try:
                _l = _builder(out)
                if _l is not None:
                    rows.append([f"{_key} listesi (adet / uygun)", f"{len(_l)} / {_l.attrs.get('n_eligible', '—')}"])
            except Exception:
                pass
        # Tarihsel ucuz özeti (session yoksa atlanır; Excel yazımında hesaplanır)
        try:
            rows.append(["Tarihsel Ucuz listesi", "F/K+PD/DD kendi μ/σ z-skoru — en ucuz 25 (raporda ayrı sayfa)"])
        except Exception:
            pass
    if out is not None and "Zarar Niteliği" in out.columns:
        _zn = out["Zarar Niteliği"].astype(str)
        rows.append(["Net zararlı hisse", int((_zn != "").sum())])
        rows.append(["  ↳ finansal/parasal kalem kaynaklı (EBIT>0 ve OCF>0)", int(_zn.str.startswith("Finansal").sum())])
    if out is not None and "Fiyat Uyarısı" in out.columns:
        _fu = out["Fiyat Uyarısı"].astype(str).value_counts()
        rows.append(["Fiyat uyuşmazlığı (İş Yatırım↔Yahoo >%5)", int(_fu.get("Uyuşmazlık", 0))])
        rows.append(["Fiyat doğrulanamadı (Yahoo yok)", int(_fu.get("Doğrulanamadı", 0))])
    if out is not None and "Likidite Notu" in out.columns:
        _ln = out["Likidite Notu"].astype(str).value_counts()
        for _k in ["Yüksek", "Orta", "Düşük", "Çok Düşük", "Veri Yok"]:
            if _k in _ln.index:
                rows.append([f"Likidite: {_k}", int(_ln[_k])])
    if n_fetch_errors is not None:
        rows.append(["Mali tablo boş / hata (Hata Log — çoğu banka/sigorta)", int(n_fetch_errors)])
    rows.append(["KAP notu", kap_note or "—"])
    rows.append(["Uyarı", "Yatırım tavsiyesi değildir. Adjusted Score ters sinyalde küçültülmüş temel skordur."])
    # Öncelik yoksa açıklama
    if cand is not None and not getattr(cand, "empty", True) and "Kova" in cand.columns:
        if int((cand["Kova"].astype(str).str.startswith("1")).sum()) == 0:
            rows.append([
                "Neden Öncelik yok?",
                "Sert kapıları (Kova≠3, Kelly>0, Çok Pahalı değil, veri tamlığı, likidite, fiyat tutarlılığı) geçen adaylar arasında Kova 1 (Öncelik) koşulunu sağlayan yok."
            ])
    return pd.DataFrame(rows, columns=["Metrik", "Değer"])



def main(output_path=None, no_backtest=False, ke_override=None, rf=None, erp=None, walk_forward=False,
         infl_now=None, infl_lt=None, real_g=None, min_liq=None, equity_basis_mode=None, real_ke=None, mos=None):
    global BACKTEST_ENABLED, WALK_FORWARD_ENABLED, COST_OF_EQUITY, LIQ_MIN_TURNOVER_TL, EQUITY_BASIS, REAL_KE_BASE
    global MOS_TIER_A, MOS_TIER_B, MOS_TIER_C
    if mos:
        _a, _b, _c = [float(x) for x in str(mos).split(",")]
        if not (_a >= _b >= _c):
            raise SystemExit("--mos: A >= B >= C olmalı (örn. 0.30,0.20,0.15)")
        MOS_TIER_A, MOS_TIER_B, MOS_TIER_C = _a, _b, _c
    if equity_basis_mode:
        EQUITY_BASIS = str(equity_basis_mode).lower()
    if real_ke is not None:
        REAL_KE_BASE = float(real_ke)
    if min_liq is not None:
        LIQ_MIN_TURNOVER_TL = float(min_liq) * 1e6
    configure_inflation(infl_now, infl_lt, real_g)
    print(f"Enflasyon: şimdi={INFLATION_NOW:.1%} uzun vade={INFLATION_LT:.1%} | terminal nominal g={TERMINAL_GROWTH:.1%}", flush=True)
    if no_backtest:
        BACKTEST_ENABLED=False
    if walk_forward:
        WALK_FORWARD_ENABLED=True
    final = choose_save(output_path)
    if not final:
        return
    final = str(Path(final).expanduser().resolve())
    Path(final).parent.mkdir(parents=True, exist_ok=True)
    temp = str(Path(final).with_name(Path(final).stem + ".__olusturuluyor__.xlsx"))
    err_csv = str(Path(final).with_name(Path(final).stem + "_hata_log.csv"))
    try:
        session = requests.Session()
        session.headers.update(HEAD)
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            _retry = Retry(total=2, backoff_factor=0.3, status_forcelist=(429, 500, 502, 503, 504))
            _ad = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS, max_retries=_retry)
            session.mount("https://", _ad)
            session.mount("http://", _ad)
        except Exception:
            pass
        ke_used = resolve_cost_of_equity(session, override=ke_override, rf=rf, erp=erp)
        print(f"ke (COST_OF_EQUITY) = {ke_used:.4f}  [override={ke_override}, rf={rf}, erp={erp}]", flush=True)
        print("İş Yatırım temel oranları alınıyor...", flush=True)
        try:
            ratios=get_ratios(session); print(f"Oran verisi: {len(ratios)} hisse", flush=True)
        except Exception as e:
            ratios={}; print("UYARI: Oran sayfası okunamadı:", e, flush=True)
        print("v7: analist tahmin / temeltarihsel akışı kaldırıldı.", flush=True)
        load_sector_map()  # temelozet sektör Excel

        anchor_year,anchor_period=detect_anchor_period(session)
        quarters=quarter_periods(anchor_year,anchor_period,8)
        print("Analiz dönemleri:",quarters,flush=True)
        codes=get_codes(session,ratios); print(f"Bulunan hisse kodu: {len(codes)}",flush=True)
        summary=[]; raw=[]; errors=[]; quarterly=[]; fin_rows=[]; done=0; total=len(codes)
        def work(code):
            try:
                df=get_fin(session,code,quarters)
                if df.empty:
                    # v192.3: bankalar/sigorta/faktoring için alternatif finansal grup kodlarını dene
                    for _g in FIN_GROUP_FALLBACKS:
                        try:
                            _dff=get_fin(session,code,quarters,group=_g)
                        except Exception:
                            continue
                        if not _dff.empty:
                            fin_rows.append(analyze_financial(code,_dff,ratios.get(code,{}),quarters,group=_g))
                            return code,None,None,None,"FIN_OK"
                    return code,None,None,None,"Boş mali tablo"
                m={k:find(df,v) for k,v in ALIASES.items()}
                m['sd']=find_scoped(df,SD_ROOT_WORDS,DEBT_ITEM_WORDS); m['ld']=find_scoped(df,LD_ROOT_WORDS,DEBT_ITEM_WORDS)
                row=analyze(code,m,ratios.get(code,{}),quarters)
                qrows=historical_quarterly_table(code,m,ratios.get(code,{}),quarters)
                rawdf=df.copy(); rawdf.insert(0,'Hisse',code)
                return code,row,rawdf,qrows,None
            except Exception as e:
                return code,None,None,None,f"{type(e).__name__}: {e}"
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures={ex.submit(work,c):c for c in codes}
            for fut in as_completed(futures):
                code=futures[fut]; done+=1
                try:
                    _,row,rawdf,qrows,err=fut.result()
                    if err=="FIN_OK":
                        print(f"[{done}/{total}] {code} (finansal kurum modeli)",flush=True); continue
                    if err: errors.append([code,err]); print(f"[{done}/{total}] {code} HATA: {err}",flush=True)
                    else: summary.append(row); raw.append(rawdf); quarterly.extend(qrows); print(f"[{done}/{total}] {code}",flush=True)
                except Exception as e:
                    msg=f"{type(e).__name__}: {e}"[:500]; errors.append([code,msg]); print(f"[{done}/{total}] {code} HATA: {msg}",flush=True)
        if not summary:
            if errors: pd.DataFrame(errors,columns=['Hisse','Hata']).to_csv(err_csv,index=False,encoding='utf-8-sig')
            raise RuntimeError("Analiz üretilemedi. Hata log: "+err_csv if errors else "Analiz üretilemedi.")
        out=enrich_summary(pd.DataFrame(summary))
        out=fill_missing_sectors(out, session, force_all=True)
        out=enrich_all_with_dd(out)
        out=enrich_all_with_shannon(out)
        # Kelly full: vol + historical (N yeterliyse) + final; arşiv main sonunda da append edilir
        print("Backtest hesaplanıyor...",flush=True)
        out=apply_backtest(out,session)
        print("Teknik analiz hesaplanıyor (RSI/CCI/Supertrend/MACD/ADX)...",flush=True)
        out = enrich_technical(out, session)
        print("TradingView çapraz kontrolü (ikinci görüş)...",flush=True)
        out = enrich_tradingview_crosscheck(out, session)
        out = enrich_price_signals(out)
        out = enrich_kelly_full(out, session=session, output_path=final)
        corr_df_pre, corr_note_pre = backtest_component_report(out)
        out = enrich_inverse_signal(out, corr_df=corr_df_pre)
        out = enrich_stop_trailing(out)
        if "Önerilen Pay %" in out.columns and "Piyasa Rejim Çarpanı" in out.columns:
            out["Rejim Ayarlı Pay %"] = (
                pd.to_numeric(out["Önerilen Pay %"], errors="coerce")
                * pd.to_numeric(out["Piyasa Rejim Çarpanı"], errors="coerce").fillna(1.0)
            ).round(2)
        out = enrich_sharpe_proxy(out, rf_annual=RISK_FREE_FALLBACK)
        # Kova'yı tüm evrene de yaz (arşiv + sıralama için)
        try:
            out["Kova"] = assign_kova_series(out)
            out["Kova Aksiyon"] = out["Kova"].map(assign_kova_aksiyon)
        except Exception as _e:
            print("Kova ataması atlandı:", _e, flush=True)
        out = enrich_all_with_composite_eval(out)
        out = enrich_capex_oe_penalty(out)
        out = enrich_adjusted_score_v12(out, corr_df=corr_df_pre)
        hist_path = append_score_history(out, output_path=final)
        if hist_path:
            print("Skor arşivi:", hist_path, flush=True)
        print("Track record değerlendiriliyor (geçmiş snapshot'lar × gerçekleşen getiri)...", flush=True)
        track_record_df, track_record_note = evaluate_track_record(session, output_path=final)
        print(track_record_note, flush=True)
        bt_summary=out.attrs.get('backtest_summary',pd.DataFrame())
        corr_df, corr_note = backtest_component_report(out)
        if corr_note:
            print(corr_note, flush=True)
        period_labels={f'value{i+1}':f'{y}-{p:02d}' for i,(y,p) in enumerate(quarters)}
        raw_df=pd.concat(raw,ignore_index=True).rename(columns=period_labels) if raw else pd.DataFrame()
        quarterly_df=pd.DataFrame(quarterly)
        vars_df=pd.DataFrame([
            ['Motor','BIST Değerleme Motoru v192'],
            ['Veri kaynağı','İş Yatırım temel değerler + MaliTablo API (XI_29); backtest için Yahoo Finance chart'],
            ['Final Score','Sektör ağırlıklı Value↓ + Quality↑ + Growth(cap) + Balance↑ + Cash↑ + Peer + Economic Quality; risk/güven çarpanı sıkı'],
            ['Not sistemi','EVREN İÇİ PERCENTILE: top %5=A+ | %15=A | %30=B+ | %45=B | %60=C+ | %75=C | %90=D | rest=F'],
            ['WACC / ke','resolve_cost_of_equity(): TR10Y+ERP veya --ke/--rf/--erp; sektör primi göreli; Bear ke+3pp / Bull ke-1.5pp; terminal g nominal=(1+reel)(1+π_LT)-1'],
            ['Yeni metrikler','Net Borç/FCF, Normalized EPS, FCF Conversion Trend, CapEx/Amortisman, KV Borç/Nakit, Net YP/Özkaynak'],
            ['Risk cezası','Eşik 45, max 12 puan; Final Score risk çarpanı 0.75+0.25*(Risk/100)'],
            ['v7.1 kalibrasyon','Value ağırlığı ↓; aşırı YoY growth cap; değer tuzağı cezası; çarpan payı ↓; FCF/OE/RevDCF ↑'],
            ['Finansal kurumlar','Banka/Sigorta XI_29 ile boş tablo → Kapsam Notu + güven üst sınırı 40'],
            ['Altman','Banka/Sigorta/GYO için Z skoruna dayalı karar verilmez'],
            ['Piotroski','9 kriter; eksik kriter N/A, skor mevcutlar üzerinden'],
            ['Peer','Sektör içi percentile + %2/%98 winsorize; <5 peer → evren'],
            ['Backtest','Son 6/12/24 ay GEÇMİŞ getiri (ileri getiri değil) × bugünkü skor: yalnızca bilgi; karar girdisi değil'],
            ['Takip paketi','Aday Listesi + Due Diligence + Kural Seti + TV Watchlist + KAP Filtre + Takip Rutini'],
            ['Due Diligence','Her hisse: DD1-5 + Değerlendirme + Derin Analiz + Ana Risk + Yatırım Tezi (Değerleme Özeti kolonlarında)'],
            ['Teknik Katman','RSI(14)+CCI(20)+Supertrend(10,3)+MACD(12,26,9)+ADX(14)+Hacim; kompozit Teknik Skor 0-100; Aday Listesi Nihai Sinyal = fundamental ∩ teknik'],
            ['Fiyat Sinyalleri','Kısa (gün-hafta): Supertrend/Bollinger/ATR · Orta (hafta-ay): SMA50 + Adil Değerin %75\'i · Uzun (6-12+ ay): mevcut fiyat → Adil Değer (Medyan), taban DCF Bear'],
            ['Confluence Skoru','w_temel×Final Score + w_teknik×Teknik Skor × Rejim Çarpanı; ağırlık Kısa=%30/%70, Orta=%50/%50, Uzun=%80/%20 (temel/teknik)'],
            ['Uyum Kadranı','Temel≥60 & Teknik≥55 kesişimine göre: GÜÇLÜ UYUM / ERKEN BİRİKİM / MOMENTUM RİSKİ / KAÇIN'],
            ['Piyasa Rejimi','XU100 Fiyat vs SMA50/SMA200: RISK-ON(x1.10) / NÖTR(x0.85) / RISK-OFF(x0.65) — Confluence Skoru ve Rejim Ayarlı Pay % bu çarpanla ölçeklenir'],
            ['Çoklu Zaman Teyidi','Günlük Supertrend ile haftalığa resample edilmiş (W-FRI) Supertrend aynı yönde mi — whipsaw filtresi'],
            ['TradingView Çapraz Kontrol','Resmi olmayan TradingView tarayıcı uç noktası; Recommend.All ile kendi Teknik Sinyal\'imiz karşılaştırılır. Bilgilendirme amaçlıdır, karar kaynağı değildir; erişilemezse otomatik atlanır.'],
            ['Performans Takibi (Gerçek Track Record)','Her run skor+fiyatı arşivler (bist_score_history.csv). ≥180/365 gün eskimiş kayıtlar, o tarihten bugüne GERÇEKLEŞEN getiriyle eşleştirilip Final Score Band/Kova/Uyum Kadranı/Teknik Sinyal bazında isabet oranı + XU100\'e göre alfa hesaplanır. apply_backtest\'teki "İleri Getiri" kolonlarından FARKLIDIR — onlar bugünden GERİYE dönük trailing getiridir (look-ahead\'e yakın), bu ise gerçek ileri-zamanlı sonuçtur. İlk ~6 ay "yetersiz veri" döner; bu normaldir.'],
            ['Uyarı','Yatırım tavsiyesi değildir. Teknik katman zamanlama içindir, tek başına al-sat sinyali değildir.'],
            ['v12 CapEx/OE','CapEx/Amortisman≥2.5 ise Owner Earnings getirisi FCF lehine harmanlanır (OE abartısı cezası)'],
            ['v12 Adjusted Score','Ters sinyal aktifken Final Score × 0.65 → Adjusted Score; Temel/Zamanlama ayrı kolon'],
            ['v12 KAP Yeni İş','Son 2 gün KAP bildirimleri; Yeni İş İlişkisi / sözleşme anahtar kelime ile işaretlenir'],
            ['v12 Model Sağlık','Kova dağılımı, Spearman, ters sinyal adedi, Öncelik yoksa açıklama'],
            ['v12 Forward F/K-PD/DD','Konsensüs varsa kullan; yoksa Implied(g≤%25). Δ F/K=Fwd−TTM; Value multiples içinde düşük pay'],
            ['v12 EPV + EV/EBIT','Earnings Power Value (sıfır büyüme NOPAT/ke); EV/EBIT band (≤6 çok ucuz); adil değere EPV ağırlığı'],
            ['v191 Portföy=15','Aday Listesi Tier A/B → Portföy tam 15 hisse; VIS+Kelly tilt; Risk Parity+Markowitz+Blended; sektör≤2'],
            ['v12 Aday=Graham+Buffett∩Teknik','Graham hard ∪ Buffett hard; GrahamBuffettScore + TeknikAlSkoru; Aday Skoru=0.65 temel+0.35 teknik AL; 1 ÖNCELİK=temel+teknik teyit'],
        ],columns=['Başlık','Açıklama'])
        with pd.ExcelWriter(temp,engine='openpyxl') as w:
            out.to_excel(w,sheet_name='Değerleme Özeti',index=False)
            # Ana sıralama sayfası: finansallar hariç
            main_rank = out[out.get('Ana Sıralamada', pd.Series(['Evet']*len(out))) == 'Evet'] if 'Ana Sıralamada' in out.columns else out
            main_rank.to_excel(w, sheet_name='Ana Sıralama', index=False)
            if fin_rows:
                pd.DataFrame(fin_rows).sort_values("Adil Değer Marjı", ascending=False, na_position="last").to_excel(
                    w, sheet_name='Finansal Kurumlar', index=False)
            _cand0 = build_candidate_list(out, top_pct=0.40, min_confidence=55.0, max_cand=15)
            _cand_attrs = dict(_cand0.attrs)
            cand = enrich_candidates_with_dd(_cand0)
            if not cand.empty:
                # stop/ters sinyal kolonları adayda yoksa out'tan merge
                for col in ("Stop Loss %", "Trailing Stop %", "Stop Notu", "Önerilen Pay %", "Rejim Ayarlı Pay %",
                            "Ters Sinyal", "Ters Sinyal Notu", "Vol Ann (σ)", "Kelly Final", "Shannon Edge",
                            "Sharpe Proxy", "Sharpe Proxy (rf=0)", "Sharpe Notu",
                            "RSI(14)", "CCI(20)", "ADX(14)", "MACD Histogram", "Supertrend Yön",
                            "Supertrend Dönüş", "Bollinger %B", "Trend Durumu (SMA50)", "Hacim Teyidi",
                            "Teknik Skor", "Teknik Sinyal", "Teknik Aksiyon",
                            "TV Görünüm", "TV Recommend.All", "TV RSI", "TV Uyum Notu",
                            "Haftalık Trend", "Haftalık RSI", "Çoklu Zaman Teyidi",
                            "Piyasa Rejimi", "Confluence Skoru (Kısa)", "Confluence Skoru (Orta)",
                            "Confluence Skoru (Uzun)", "Uyum Kadranı",
                            "KV Giriş Aralığı", "KV Hedef", "KV Stop",
                            "OV Giriş", "OV Hedef", "OV Stop",
                            "UV Giriş", "UV Hedef", "UV Stop", "Fiyat Sinyali Özeti",
                            "Likidite Notu", "Medyan Günlük Hacim (TL)", "1 Gün Tahliye Kapasitesi (TL)", "İşlemsiz Gün Oranı", "Yıllık Devir Oranı"):
                    if col not in cand.columns and col in out.columns:
                        cand = cand.merge(out[["Hisse", col]].drop_duplicates("Hisse"), on="Hisse", how="left")
                # Nihai Sinyal: fundamental aday sınıfı + teknik onay + Uyum Kadranı + çoklu
                # zaman dilimi teyidinin birleşimi (piyasa rejimi Confluence Skoru'na zaten işlendi).
                if "Teknik Aksiyon" in cand.columns:
                    def _nihai(row):
                        cls = str(row.get("Kural Sınıfı") or "")
                        teknik = str(row.get("Teknik Aksiyon") or "")
                        kadran = str(row.get("Uyum Kadranı") or "")
                        mtf = str(row.get("Çoklu Zaman Teyidi") or "")
                        tv = str(row.get("TV Uyum Notu") or "")
                        fundamental_ok = cls in ("GÜÇLÜ ADAY", "PAHALI KALİTE") or row.get("Aday Modu") == "Karşılama Oranı (Fallback)"
                        if teknik == "AL (Temel+Teknik Uyumlu)" and cls == "GÜÇLÜ ADAY" and kadran.startswith("GÜÇLÜ UYUM") and mtf.startswith("Teyitli (Günlük+Haftalık"):
                            base = "GÜÇLÜ AL (Temel+Teknik+Haftalık Uyumlu)"
                        elif teknik == "AL (Temel+Teknik Uyumlu)" and cls == "GÜÇLÜ ADAY":
                            base = "GÜÇLÜ AL (Temel+Teknik Uyumlu)"
                        elif kadran.startswith("ERKEN BİRİKİM"):
                            base = "İZLE (Erken Birikim — Teknik Henüz Dönmedi)"
                        elif kadran.startswith("MOMENTUM RİSKİ"):
                            base = "DİKKAT (Momentum Riski — Temel Zayıf)"
                        elif teknik.startswith("AL") and fundamental_ok:
                            base = "AL (Teknik Onaylı)"
                        elif teknik.startswith("İZLE") or cls == "İZLEME":
                            base = "İZLE (Kısmi Onay)"
                        elif teknik.startswith("BEKLE"):
                            base = "BEKLE (Teknik Zayıf, Fiyat Trendi Aşağı)"
                        else:
                            base = "VERİ YETERSİZ"
                        if tv.startswith("ÇELİŞKİ"):
                            base += " ⚠ TV ile Çelişkili"
                        return base
                    cand["Nihai Sinyal"] = cand.apply(_nihai, axis=1)
                    front = [c for c in ["Aday Sıra", "Hisse", "Sektör", "Nihai Sinyal", "Uyum Kadranı", "Fiyat Sinyali Özeti"] if c in cand.columns]
                    rest = [c for c in cand.columns if c not in front]
                    cand = cand[front + rest]
                cand.attrs.update(_cand_attrs)   # merge/sort sırasında kaybolan meta (Tier sayıları, evren) geri yüklenir
                cand.to_excel(w, sheet_name='Aday Listesi', index=False)
                try:
                    append_candidate_history(cand, out, output_path=final)
                    _cdet, _csum = evaluate_candidate_history(
                        lambda c: fetch_yahoo_bundle(session, c)[4],
                        lambda: fetch_yahoo_bundle(session, "XU100")[4], output_path=final)
                    if _csum is not None and not _csum.empty:
                        _csum.to_excel(w, sheet_name='Aday Takip', index=False)
                        _cdet.to_excel(w, sheet_name='Aday Takip', index=False, startrow=len(_csum) + 3)
                        print("Aday Takip: geçmiş adayların ileri getirisi hesaplandı.", flush=True)
                    else:
                        print("Aday Takip: henüz >=30 günlük aday arşivi yok (arşivleniyor).", flush=True)
                except Exception as _e:
                    print(f"UYARI: Aday Takip atlandı: {_e}", flush=True)
                print(f"Aday Listesi: {len(cand)} hisse (max 15 · kural seti)", flush=True)
                port_df, port_sum = build_portfolio_allocation(cand, rf=RISK_FREE_FALLBACK, n_target=PORTFOLIO_SIZE)
                port_df.to_excel(w, sheet_name='Portföy', index=False)
                port_sum.to_excel(w, sheet_name='Portföy Özet', index=False)
                print(f"Portföy: {len(port_df)} satır (Risk Parity + Markowitz + Blended)", flush=True)
            else:
                cand = pd.DataFrame()
                pd.DataFrame([{'Not': 'Filtreden geçen aday yok — eşikleri gevşetin veya veri kalitesini kontrol edin.'}]).to_excel(
                    w, sheet_name='Aday Listesi', index=False
                )
                pd.DataFrame([{'Not': 'Aday yok'}]).to_excel(w, sheet_name='Portföy', index=False)
                print("Aday Listesi: boş", flush=True)
            # v192.8: Klasik değer yatırımı listesi (Aday Listesi'nden bağımsız)
            try:
                value_list = build_value_investing_list(out)
                if value_list is not None and len(value_list):
                    value_list_for_sheet(value_list).to_excel(w, sheet_name='Değer Yatırımı', index=False)
                    _va = value_list.attrs
                    print(f"Değer Yatırımı: {len(value_list)} hisse (uygun {_va.get('n_eligible')}, tam eşleşen {_va.get('n_tam')})", flush=True)
                    try:
                        append_candidate_history(value_list.assign(**{"Aday Modu": value_list["Eşleşme"].map(lambda x: "Tier V-" + str(x)),
                                                                        "Aday Sıra": value_list["Sıra"]}), out, output_path=final, liste="Değer")
                    except Exception as _e2:
                        print(f"UYARI: Değer listesi arşivi atlandı: {_e2}", flush=True)
                else:
                    pd.DataFrame([{'Not': 'Değer kriterlerini karşılayan uygun hisse yok.'}]).to_excel(w, sheet_name='Değer Yatırımı', index=False)
                    print("Değer Yatırımı: boş", flush=True)
            except Exception as _e:
                value_list = pd.DataFrame()
                print(f"UYARI: Değer Yatırımı listesi atlandı: {_e}", flush=True)
            # v192.9: Ek seçim teknikleri (her biri Aday Listesi ve Değer Yatırımı'ndan bağımsız)
            _extra_lists = {}
            for _key, _title, _builder, _cols in [
                ("Magic Formula", "MAGIC FORMULA (GREENBLATT) — 15 HİSSE",
                 lambda: build_magic_formula_list(out), MAGIC_SHEET_COLS),
                ("Momentum", "MOMENTUM — 15 HİSSE",
                 lambda: build_momentum_list(out), MOMENTUM_SHEET_COLS),
                ("Temettü", "TEMETTÜ / GELİR YATIRIMI — 15 HİSSE",
                 lambda: build_dividend_list(out), DIVIDEND_SHEET_COLS),
                ("Düşük Volatilite", "DÜŞÜK VOLATİLİTE — 15 HİSSE",
                 lambda: build_low_vol_list(out), LOWVOL_SHEET_COLS),
                ("Sektöre Göre Ucuz", "SEKTÖRE GÖRE UCUZLUK (GÖRELİ DEĞERLEME) — 15 HİSSE",
                 lambda: build_sector_relative_list(out), SECTORREL_SHEET_COLS),
                ("Tarihsel Ucuz", "TARİHSEL+FORWARD F/K & PD/DD Z-SKORU — EN UCUZ 25 (FCF+borç filtreli)",
                 lambda: build_historical_cheap_list(out, session=session, n=25), HISTCHEAP_SHEET_COLS),
            ]:
                try:
                    _lst = _builder()
                    _extra_lists[_key] = _lst
                    if _lst is not None and len(_lst):
                        _generic_list_for_sheet(_lst, _cols).to_excel(w, sheet_name=_key, index=False)
                        print(f"{_key}: {len(_lst)} hisse (uygun {_lst.attrs.get('n_eligible')})", flush=True)
                        try:
                            append_candidate_history(
                                _lst.assign(**{"Aday Modu": "Tier " + _key, "Aday Sıra": _lst["Sıra"]}),
                                out, output_path=final, liste=_key)
                        except Exception as _e3:
                            print(f"UYARI: {_key} arşivi atlandı: {_e3}", flush=True)
                    else:
                        pd.DataFrame([{'Not': f'{_key}: uygun hisse yok.'}]).to_excel(w, sheet_name=_key, index=False)
                        print(f"{_key}: boş", flush=True)
                except Exception as _e:
                    _extra_lists[_key] = pd.DataFrame()
                    print(f"UYARI: {_key} listesi atlandı: {_e}", flush=True)
            # Faktör kotalı 25 (değer/kalite/savunma/momentum) — Tarihsel Ucuz çıktısını kullanır
            try:
                _hist = _extra_lists.get("Tarihsel Ucuz")
                _fkota = build_faktor_kota_list(out, session=session, n=25, hist_cheap_df=_hist)
                _extra_lists["Faktör Kota"] = _fkota
                if _fkota is not None and len(_fkota):
                    _generic_list_for_sheet(_fkota, FAKTOR_SHEET_COLS).to_excel(w, sheet_name="Faktör Kota", index=False)
                    print(f"Faktör Kota: {len(_fkota)} hisse (kota {FAKTOR_KOTA})", flush=True)
                    try:
                        append_candidate_history(
                            _fkota.assign(**{"Aday Modu": "Tier Faktör Kota", "Aday Sıra": _fkota["Sıra"]}),
                            out, output_path=final, liste="Faktör Kota")
                    except Exception as _e3:
                        print(f"UYARI: Faktör Kota arşivi atlandı: {_e3}", flush=True)
                else:
                    pd.DataFrame([{"Not": "Faktör Kota: uygun hisse yok."}]).to_excel(w, sheet_name="Faktör Kota", index=False)
                    print("Faktör Kota: boş", flush=True)
            except Exception as _e:
                _extra_lists["Faktör Kota"] = pd.DataFrame()
                print(f"UYARI: Faktör Kota listesi atlandı: {_e}", flush=True)
            # v192.10: GYO NAV modeli (DENEYSEL) — tüm GYO'lar, adaylıktan bağımsız ayrı bir sayfa
            try:
                gyo_list = build_gyo_nav_list(out)
                if gyo_list is not None and len(gyo_list):
                    gyo_list[[c for c in GYO_SHEET_COLS if c in gyo_list.columns]].to_excel(w, sheet_name='GYO NAV', index=False)
                    print(f"GYO NAV: {len(gyo_list)} GYO", flush=True)
                else:
                    pd.DataFrame([{'Not': 'GYO NAV hesaplanabilen hisse yok (yatırım amaçlı gayrimenkul kalemi eşleşmedi).'}]).to_excel(w, sheet_name='GYO NAV', index=False)
                    print("GYO NAV: boş", flush=True)
            except Exception as _e:
                gyo_list = pd.DataFrame()
                print(f"UYARI: GYO NAV listesi atlandı: {_e}", flush=True)
            # v192.10: Çoklu liste konsensüsü — 6 bağımsız seçim listesinden en az 2'sinde geçen hisseler
            try:
                _consensus_input = {"Değer Yatırımı": value_list if 'value_list' in dir() else pd.DataFrame()}
                _consensus_input.update(_extra_lists)
                cons_list = build_consensus_list(_consensus_input, n=15)
                if cons_list is not None and len(cons_list):
                    cons_list[[c for c in CONSENSUS_SHEET_COLS if c in cons_list.columns]].to_excel(w, sheet_name='Konsensüs', index=False)
                    print(f"Konsensüs: {len(cons_list)} hisse (≥{CONSENSUS_MIN_LISTS} listede)", flush=True)
                else:
                    pd.DataFrame([{'Not': f'{CONSENSUS_MIN_LISTS}+ listede birlikte geçen hisse yok.'}]).to_excel(w, sheet_name='Konsensüs', index=False)
                    print("Konsensüs: boş", flush=True)
            except Exception as _e:
                print(f"UYARI: Konsensüs listesi atlandı: {_e}", flush=True)
            # Takip paketi: TradingView watchlist + KAP filtre + rutin
            build_rule_set_df().to_excel(w, sheet_name='Kural Seti', index=False)
            build_methodology_sheet().to_excel(w, sheet_name='Metodoloji', index=False)
            build_shannon_sheet(out).to_excel(w, sheet_name='Shannon', index=False)
            build_technical_sheet(out).to_excel(w, sheet_name='Teknik', index=False)
            if track_record_df is not None and not track_record_df.empty:
                track_record_df.to_excel(w, sheet_name='Performans Takibi', index=False)
            else:
                pd.DataFrame([{"Durum": track_record_note}]).to_excel(w, sheet_name='Performans Takibi', index=False)
            ks = out.attrs.get('kelly_band_stats') if hasattr(out, 'attrs') else None
            if ks is None:
                ks = load_kelly_band_stats(final)
            if ks is not None and not getattr(ks, 'empty', True):
                ks.to_excel(w, sheet_name='Kelly Band Stats', index=False)
            else:
                pd.DataFrame([{'Not': f'Tarihsel Kelly için arşiv birikmeli (min N={KELLY_MIN_N}). score_history.csv her run append.'}]).to_excel(w, sheet_name='Kelly Band Stats', index=False)
            # Tüm ana sıralama + adaylar için DD (özet zaten Değerleme Özeti'nde)
            dd_all = out[out['Ana Sıralamada'].astype(str).str.startswith('Evet')] if 'Ana Sıralamada' in out.columns else out
            dd_sheet = build_due_diligence_sheet(dd_all.assign(**{
                'Aday Sıra': range(1, len(dd_all)+1)
            }) if 'Aday Sıra' not in dd_all.columns else dd_all)
            # Önce adayları üste al
            if cand is not None and not cand.empty and 'Hisse' in cand.columns:
                top = set(cand['Hisse'].astype(str))
                dd_sheet = pd.concat([
                    dd_sheet[dd_sheet['Hisse'].astype(str).isin(top)],
                    dd_sheet[~dd_sheet['Hisse'].astype(str).isin(top)],
                ], ignore_index=True)
            dd_sheet.to_excel(w, sheet_name='Due Diligence', index=False)
            build_aday_watchlist_df(cand).to_excel(w, sheet_name='TV Watchlist', index=False)
            build_kap_filtre_df().to_excel(w, sheet_name='KAP Filtre', index=False)
            # v12: KAP Yeni İş İlişkisi (son 2 gün)
            try:
                _cand_tickers = list(cand['Hisse'].astype(str)) if (cand is not None and not cand.empty and 'Hisse' in cand.columns) else None
                kap_df, kap_note = build_kap_yeni_is_sheet(session, days=2, cand_tickers=_cand_tickers)
                kap_df.to_excel(w, sheet_name='KAP Yeni İş', index=False)
                print('KAP Yeni İş:', kap_note, flush=True)
            except Exception as _kap_e:
                kap_note = f'KAP hata: {_kap_e}'
                pd.DataFrame([{'Not': kap_note, 'URL': 'https://www.kap.org.tr/tr/bildirim-sorgu'}]).to_excel(
                    w, sheet_name='KAP Yeni İş', index=False)
                print(kap_note, flush=True)
            try:
                health = build_model_health_sheet(out, corr_df, cand if 'cand' in dir() else None, kap_note=kap_note if 'kap_note' in dir() else '', n_fetch_errors=len(errors))
                health.to_excel(w, sheet_name='Model Sağlık', index=False)
            except Exception as _h_e:
                print('Model Sağlık atlandı:', _h_e, flush=True)
            build_takip_rutini_df().to_excel(w, sheet_name='Takip Rutini', index=False)
            quarterly_df.to_excel(w,sheet_name='Çeyreklik Detay',index=False)
            raw_df.to_excel(w,sheet_name='Ham Mali Tablolar',index=False)
            vars_df.to_excel(w,sheet_name='Varsayımlar',index=False)
            if not bt_summary.empty: bt_summary.to_excel(w,sheet_name='BACKTEST',index=False)
            if not corr_df.empty:
                corr_df.to_excel(w, sheet_name='Backtest Korelasyon', index=False)
                pd.DataFrame([{'Not': corr_note}]).to_excel(w, sheet_name='Model Notu', index=False)
            if errors: pd.DataFrame(errors,columns=['Hisse','Hata']).to_excel(w,sheet_name='Hata Log',index=False)
        style(temp)
        os.replace(temp,final)
        if errors: pd.DataFrame(errors,columns=['Hisse','Hata']).to_csv(err_csv,index=False,encoding='utf-8-sig')
        # TradingView watchlist dosyaları (Excel yanına)
        try:
            cand_final = cand if "cand" in dir() else pd.DataFrame()  # optimize: ikinci aday üretimi yok
            tv_path = str(Path(final).with_name(Path(final).stem + "_TV_Watchlist.txt"))
            saved = export_tradingview_watchlist(cand_final if (cand_final is not None and len(cand_final)) else cand, tv_path)
            if saved:
                print("TradingView watchlist:", saved, flush=True)
                print("Düz kod listesi:", Path(saved).with_name(Path(saved).stem + "_kodlar.txt"), flush=True)
        except Exception as _e:
            print("Watchlist dışa aktarım atlandı:", _e, flush=True)
        print("RAPOR TAMAMLANDI:",final,flush=True)
        print("Toplam analiz:",len(out),"| Hata:",len(errors),flush=True)
        print("Sayfalar: DASHBOARD | Aday(15) | Portföy | DD | Shannon | Teknik | Performans Takibi | Kelly | Metodoloji | ...", flush=True)
        return final
    except Exception:
        if os.path.exists(temp): os.remove(temp)
        raise


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description="BIST Değerleme Motoru v192")
    parser.add_argument("--output",help="Excel çıktı yolu")
    parser.add_argument("--no-backtest",action="store_true",help="Backtest HTTP çağrılarını kapat")
    parser.add_argument("--ke", type=float, default=None, help="Cost of equity override (örn. 0.24)")
    parser.add_argument("--rf", type=float, default=None, help="Risk-free rate (TR 10Y, örn. 0.28)")
    parser.add_argument("--erp", type=float, default=None, help="Equity risk premium (örn. 0.06)")
    parser.add_argument("--infl-now", type=float, default=None, help="Güncel yıllık enflasyon beklentisi (örn. 0.30)")
    parser.add_argument("--infl-lt", type=float, default=None, help="Uzun vade nominal enflasyon / terminal (örn. 0.15)")
    parser.add_argument("--real-g", type=float, default=None, help="Terminal REEL büyüme (örn. 0.02)")
    parser.add_argument("--min-liq", type=float, default=None, help="Likidite alt sınırı: medyan günlük hacim, MİLYON TL (varsayılan 10)")
    parser.add_argument("--equity-basis", choices=["real", "nominal"], default=None, help="RI/EPV/kapitalize kazanç iskonto tabanı (varsayılan real)")
    parser.add_argument("--real-ke", type=float, default=None, help="Reel özkaynak maliyeti tabanı (varsayılan 0.12)")
    parser.add_argument("--mos", type=str, default=None, help="Güvenlik marjı eşikleri Tier A,B,C (varsayılan 0.30,0.20,0.15)")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Ek walk-forward panel (sınırlı: mevcut skor + çoklu getiri ufku; full restatement değil)")
    parser.add_argument("--history-dir", default=None,
                        help="bist_score_history.csv arşivinin sabit klasörü (varsayılan: script yanındaki 'bist_history/'). "
                             "Track record'un doğru birikmesi için her run'da AYNI değeri kullanın.")
    args=parser.parse_args()
    if args.history_dir:
        os.environ["BIST_HISTORY_DIR"] = args.history_dir
    print("Skor arşivi konumu:", _score_history_path(), flush=True)
    try:
        main(args.output, args.no_backtest, ke_override=args.ke, rf=args.rf, erp=args.erp,
             walk_forward=args.walk_forward, infl_now=args.infl_now, infl_lt=args.infl_lt, real_g=args.real_g, min_liq=args.min_liq,
             equity_basis_mode=args.equity_basis, real_ke=args.real_ke, mos=args.mos)
    except Exception as e:
        print("HATA:",e,flush=True)
        sys.exit(1)
