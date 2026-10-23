# -*- coding: utf-8 -*-
"""Piyasa Paneli verisini komut satırından günceller (cron / GitHub Actions / elle).

Örnekler:
  python panel_guncelle.py                       # hisse + bilanço + fon
  python panel_guncelle.py --parts hisse,bilanco # sadece hisse ve bilanço
  python panel_guncelle.py --degerleme-yenile    # yeni bilanço varsa BIST değerlemesini de yeniden çalıştır (uzun sürer)
"""
import argparse
import sys

import tefas_panel as P


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parts", default="hisse,bilanco,fon", help="virgülle: hisse,bilanco,fon")
    ap.add_argument("--degerleme-yenile", action="store_true",
                    help="Son rapordan sonra yeni bilanço geldiyse (ayarlardaki eşik kadar) değerlemeyi çalıştırıp bitmesini bekler")
    a = ap.parse_args()
    parts = tuple(x.strip() for x in a.parts.split(",") if x.strip())
    meta = P.refresh_all(parts=parts, wait_degerleme=a.degerleme_yenile,
                         auto_degerleme=True if a.degerleme_yenile else None)
    print("Güncelleme:", meta.get("son_guncelleme"))
    for k, v in (meta.get("hatalar") or {}).items():
        print(f"  [HATA] {k}: {v}")
    bek = P.bekleyen_degerleme()
    print(f"Değerlemesi bekleyen bilanço: {len(bek)}")
    return 0 if meta.get("son_guncelleme") else 1


if __name__ == "__main__":
    sys.exit(main())
