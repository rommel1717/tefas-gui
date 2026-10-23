# Piyasa Paneli eklentisi (ön sayfa)

Ön sayfanın en üstüne: **🆕 Yeni bilançolar · 📈 Hisseler (günlük/haftalık/aylık en çok artan–düşen) · 💰 Fonlar (aynı)**.
Veri her hafta içi **10:30 ve 19:00 (TSİ)** sonrası kendiliğinden yenilenir.

## Kurulum (2 adım)
1. Bu zip'teki dosyaları uygulamanızın klasörüne (tefas_gui.py'nin yanına) kopyalayın:
   `tefas_panel.py`, `panel_guncelle.py`, `requirements.txt` (mevcutla birleştirin), `.github/workflows/panel_guncelle.yml`
   (tefas_core.py ve vbist_degerleme_v193.py zaten klasörde olmalı).
2. `tefas_gui.py` içinde **iki satır** ekleyin:
   - importların yanına:  `import tefas_panel`
   - "🏠 Ana Sayfa" bloğunda, üstteki `<div class="hero">…</div>` st.markdown(...) çağrısından hemen sonra:
     `tefas_panel.render_ana_panel()`
   Panel kendi hatalarını yakalar; çökse bile ana sayfa açılır.

## Verinin güncel kalması
- **Kendi bilgisayarınız / sunucunuz:** uygulama açıkken arka plan zamanlayıcı kendiliğinden yeniler. Kapalıyken de
  istenirse cron/Görev Zamanlayıcı ile `python panel_guncelle.py` çalıştırın.
- **Streamlit Community Cloud:** disk kalıcı değildir ve uygulama boşta uyur. `.github/workflows/panel_guncelle.yml`
  GitHub Actions ile günde 2 kez `panel_guncelle.py` çalıştırıp `panel_data/` klasörünü repoya işler; uygulama
  push sonrası taze veriyle açılır. (Actions → "panel-guncelle" → Run workflow ile elle de başlatılır.)
  TEFAS/İş Yatırım/KAP yurt dışı IP'lerini engellerse Actions günlüğünde [HATA] görürsünüz; o durumda bu işi
  Türkiye'deki kendi makinenizde cron ile çalıştırın.

## Yeni bilanço → değerleme
- Tespit: KAP "Finansal Rapor" bildirimleri + İş Yatırım MaliTablo'da yeni çeyrek dönemin dolması (dönem sonundan ~85 gün
  boyunca, henüz görülmeyen hisseler yoklanır). İlk çalıştırmada mevcut durum kaydedilir (hepsi "yeni" sayılmaz).
- Panel, son değerleme raporundan **sonra** bilançosu gelenleri "⏳ yenilenmeli" işaretler.
- **♻️ Yeni bilançoları değerlemeye yansıt** düğmesi vbist motorunu yeniden çalıştırır (tüm evreni baştan hesaplar; uzun sürer).
- Otomatik mod: panelde "Otomatik değerleme yenileme" kutusunu açın (en az N şirket eşiği; en fazla 20 saatte bir).
  Komut satırı: `python panel_guncelle.py --degerleme-yenile`.

## Notlar
- Kaynaklar: fon fiyatları TEFAS, hisse fiyatları Yahoo Finance (gecikmeli olabilir), bilanço İş Yatırım + KAP.
- Fon fiyatları çoğunlukla ertesi sabah netleşir; akşam çalışmasında "günlük" fon değişimi bir önceki işlem gününe ait olabilir.
- Aykırı fon değerleri (günlük ±%25 vb.) varsayılan olarak gizlenir; hisselerde düşük hacimli olanlar ciro filtresiyle elenir.
- Veri alınamazsa panel uyarı gösterir ve son başarılı veriyi korur; uydurma veri üretmez.
- Yatırım tavsiyesi değildir.
