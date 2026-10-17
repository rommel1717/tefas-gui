# TEFAS Fon Analiz — Streamlit Arayüz

Türkiye TEFAS fonları için tarama, risk analizi, akıllı portföy seçimi, hisse entegrasyonu ve Türkçe raporlama aracı.

## Kurulum

```bash
python3 -m venv tefas_env
source tefas_env/bin/activate   # Windows: tefas_env\Scripts\activate
pip install requests pandas numpy openpyxl matplotlib scipy streamlit beautifulsoup4 lxml
# opsiyonel:
pip install quantstats
```

## Çalıştırma

```bash
streamlit run tefas_gui.py
# veya
python -m streamlit run /tam/yol/tefas_gui.py
```

Tarayıcı: `http://localhost:8501`

> `tefas_gui.py` ve `tefas_core.py` aynı klasörde olmalıdır.

## Dosyalar

| Dosya | Açıklama |
|-------|----------|
| `tefas_core.py` | API, metrikler, optimizasyon, Excel/HTML rapor |
| `tefas_gui.py` | Streamlit arayüzü (karanlık tema: `.streamlit/config.toml`) |
| `tefas_filtre.py` | 🧪 Filtre Stüdyosu (motor + sayfa) |
| `tefas_bist_filtre.py` | 🧪 BIST Filtre Stüdyosu (BIST'e özel kriterler + hazır profiller + arayüz) |
| `bist_filtre_presetleri.json` | Kayıtlı BIST filtreleri (otomatik) |
| `tefas_bist.py` | 🏦 BIST Değerleme sayfası (çalıştırıcı + rapor görüntüleyici) |
| `vbist_degerleme_v193.py` | BIST değerleme motoru (ayrı süreçte çalışır; tek başına da çalıştırılabilir) |
| `bist_raporlar/`, `bist_history/` | Üretilen Excel raporları ve skor arşivi (otomatik) |
| `tefas_filtre_presetleri.json` | Kayıtlı kişisel filtreler (otomatik) |
| `tefas_cache/` | API önbelleği (otomatik) |
| `tefas_config.json` | TÜFE, mevduat, risksiz faiz |
| `tefas_favoriler.json` | Favori listesi |

## Önerilen kullanım akışı

1. **Fon Tarama** veya **Tüm Fon Sıralaması** ile aday havuzu  
2. **Akıllı filtre profili**: Muhafazakâr / Dengeli / Agresif / Kısa vade / BIST düşünce artan  
3. **Akıllı portföy** veya **Karışık (3 profil)**  
4. **🛒 Portföye ekle** → **Portföy → Sepet** → özet rapor  
5. İstenen fon için **Fon Detay** + HTML risk raporu  

## Menü özeti

| Menü | Özellikler |
|------|------------|
| Ana Sayfa | Akış rehberi, sepet sayacı |
| Fon Tarama | Senaryo, kalite kapıları, canlı filtre, akıllı 10 / karışık portföy |
| Tüm Fon Sıralaması | Dönemsel sıra + akıllı filtre + kısa vade / BIST ρ |
| 🧪 Filtre Stüdyosu | Çok kriterli filtre, huni, yakın kaçanlar, risk-getiri haritası, çeşitlendirilmiş seçim, fon mercek |
| Karşılaştırma | Çoklu fon yan yana |
| Portföy | Sepet özet raporu, elle ağırlık, otomatik |
| Optimizasyon | Max Sharpe, Risk Parity, CVaR… |
| Fon Detay | Kod/ad arama, sade dil, HTML, portföy dağılımı |
| Hisse & Karma | BIST hisse metrikleri, fon+hisse portföy |
| 🏦 BIST Değerleme | Değerleme motorunu parametreli çalıştır (canlı günlük/ilerleme), eski raporları aç, sırala/filtrele, adayları Hisse & Karma sayfasına gönder |
| Favoriler / Ayarlar | Liste, TÜFE/mevduat, cache |

## 🧪 Filtre Stüdyosu

1. **Veri kaynağı:** son sıralama/tarama sonucu, CSV-Excel yükleme veya "Evreni yükle" (pozitif-getiri şartı olmadan geniş evren).
2. **Hazır profil** (Muhafazakâr, Dengeli, Agresif…) tek tıkla eşikleri doldurur; kendi filtrelerinizi **kaydedip** geri yükleyebilirsiniz.
3. **Kriterler:** 38 sayısal ölçüt için alt/üst sınır (boş = sınır yok), kategori (tür, kurucu, kategori, likidite), metin içerir/içermez, risk bayrağı yok, her dönem pozitif. "Verisi eksik fonlar elenmesin" seçeneği.
4. **Sıralama:** evren geneli yüzdelik sıraya dayalı, ağırlığını sizin belirlediğiniz skor (veya mevcut puanlar / herhangi bir metrik).
5. Sekmeler: **Sonuçlar** (favori / sepet / Excel / CSV) · **Huni** (hangi kriter kaç fon eliyor, gevşetirsen kaç fon geri gelir) · **Yakın kaçanlar** · **Risk-getiri haritası** (etkin sınır) · **Çeşitlendirilmiş seçim** (tür/kurucu limitli + ağırlık) · **Fon mercek** (bir fon neden geçti/elendi).

## 🏦 BIST Değerleme

- **Çalıştır:** ke / rf / ERP / enflasyon / likidite / güvenlik marjı gibi parametreleri girin (boş = motorun varsayılanı), **Başlat**'a basın. Motor ayrı süreçte çalışır; ilerleme çubuğu ve günlük canlı akar, **Durdur** ile iptal edilir. Tüm BIST taraması uzun sürer.
- **Rapor görüntüleyici:** `bist_raporlar/` içindeki geçmiş raporlar veya yüklediğiniz Excel. Sıralama (sektör/kova/karar filtreleri, sayısal sınır), Aday Listesi + Portföy, tüm sayfalar, indirme.
- **🧪 Filtre Stüdyosu (rapor görüntüleyici içinde):** Ana Sıralama / Değerleme Özeti evreninde çok kriterli filtre.
  - ~70 bilinen ölçüt (model skorları, adil değer marjları, F/K·PD/DD·EV çarpanları, ROE·ROIC·marjlar, Altman Z, Piotroski F, borç oranları, RSI/ADX…) + rapordaki diğer sayısal sütunlar otomatik eklenir; Sektör, Kova, Nihai Sinyal gibi kategorik filtreler, hisse/sektör metin araması, "Ana sıralamada" ve "Model uyarısı yok" şartları.
  - Hazır profiller: Ucuz & kaliteli (Graham+Buffett), Adil değer iskontosu, Kaliteli büyüme, Temettü & nakit, Savunmacı, Model en iyileri, Teknik teyitli. Birimden bağımsız kurallar ("evrenin en iyi %X'i") güncel veriye göre eşiğe çevrilir; kendi filtrelerinizi kaydedebilirsiniz.
  - Oran sütunları (ROE, ROIC, marjlar, getiriler…) burada **yüzde** olarak gösterilir (0.15 → 15).
  - Huni, yakın kaçanlar, risk-getiri/çarpan haritası (etkin sınır), sektör dengeli seçim, hisse mercek, Excel/CSV indirme.
- **Entegrasyon:** Aday hisseleri tek tıkla **Hisse & Karma Portföy** sayfasına gönderin.
- Skor arşivi için her çalıştırmada aynı `bist_history/` klasörü kullanılır (track record birikir).
- İnternet erişimi gerekir (İş Yatırım, Yahoo, KAP).

## Akıllı seçim (kısa)

- **Eleme:** risk_skor, likidite, kazanma oranı, en kötü gün, Sharpe, DD, vol, VaR/cVaR, BIST ρ  
- **Skor:** temel rapor skoru + tutarlılık/likidite/çeşitlendirme bonusları − anomali cezası  
- **Çeşitlilik:** aynı baskın türden sınırlı fon  
- **Ağırlık:** Max Sharpe / Risk Parity / skor / eşit  

## Uyarı

Geçmiş performans gelecekteki sonuçların garantisi değildir. Araç karar destek amaçlıdır; yatırım tavsiyesi değildir.

CLI: `python tefas_core.py`

## Ek filtreler (tefas_ek.py)

Fon Tarama (canlı filtre), Tüm Fon Sıralaması ve Filtre Stüdyosu'nda "Ek filtreler" bölümü:

| Grup | Ölçüt | Veri kaynağı |
|---|---|---|
| Süreklilik | Fon yaşı, pozitif ay oranı, kategori medyanını geçen ay %, MaxDD sonrası toparlanma, zirveden uzaklık | 36 aylık fiyat geçmişi (📡 hesapla düğmesi) |
| Piyasa ilişkisi | BIST100'e göre alfa / beta | fiyat geçmişi + Yahoo XU100 |
| Kategori içi sıra | Sharpe, Sortino, 6A/1Y getiri, drawdown, volatilite yüzdeliği (100 = en iyi, kategoride ≥5 fon) | mevcut tablo |
| Yoğunlaşma | En büyük varlık payı, yoğunlaşma endeksi, repo+mevduat | portföy dağılımı |
| Akış | Büyüklük / yatırımcı değişimi | günlük anlık görüntü (cache klasöründe fon_snapshot.csv); en az 14 günlük kayıt gerekir |
| İşlem | Geri alım valörü, son işlem saati | profil bilgisi |
| Maliyet | TER, stopaj | TEFAS API'sinde yok → CSV yükleyin (kod, ter, stopaj) |
| Vergi grubu | Hisse yoğun / para piyasası / borçlanma / altın / serbest / diğer | varlık dağılımından yaklaşık etiket |

Filtre Stüdyosu'nda ayrıca "Şu kurucuları HARİÇ tut" seçimi vardır.
Vergi grubu etiketi yaklaşıktır, oran içermez; resmi koşulları izahnameden doğrulayın.

## 💼 Portföyüm (tefas_portfoyum.py)

Menüdeki "💼 Portföyüm" sayfası kendi fon + hisse portföyünüzü izler.

- **Pozisyonlar:** Tür, kod, adet, birim maliyet, (isteğe bağlı) alış tarihi ve manuel fiyat. Tablo düzenlenip kaydedilir; CSV/Excel içe aktarma ve CSV indirme var. Veriler `portfoyum.json` dosyasında saklanır (bu dosyayı yedekleyin).
- **Fiyat:** Fon → TEFAS (cache'li), hisse → Yahoo Finance. Fiyat bulunamazsa manuel fiyat kullanılır.
- **Özet & K/Z:** Toplam değer/maliyet, K/Z, günlük değişim, 1H/1A, ağırlık, yıllıklandırılmış K/Z. Dikkat uyarıları: tek pozisyon >%25, maliyetin %15 altı, günlük ±%5 hareket, eski fiyat. Sayfayı açtığınız her gün portföy değeri kaydedilir.
- **Dağılım:** Fon/Hisse, pozisyon, varlık sınıfı (fonların içine bakarak), fon kategorisi, kurucu.
- **Performans & Risk:** Mevcut adetlerle geriye dönük değer serisi (BIST100 ile karşılaştırmalı), Sharpe, volatilite, MaxDD, beta/alfa, VaR, etkin pozisyon sayısı, pozisyon korelasyon matrisi.
- **KAP:** Portföydeki kodlara ait son bildirimler; yeni olanlar 🆕 ile işaretlenir, konu etiketi (temettü, sermaye, geri alım, finansal rapor…) eklenir, "okundu" işaretlenebilir. Menüde yeni bildirim sayısı gösterilir.
  KAP'ın resmi API'si yoktur; halka açık JSON uç noktaları kullanılır, KAP yapısını değiştirirse çalışmayabilir (sayfa bunu açıkça belirtir).
