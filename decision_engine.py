# ============================================================
# KARAR DESTEK MOTORU (Decision Support Engine)
# ------------------------------------------------------------
# Mevcut risk/metrik hesaplama (app.py -> parca_metriklerini_hesapla)
# üzerine inşa edilmiş, çok kriterli (maliyet / risk / tedarik)
# ağırlıklı bir karar katmanı. Bu modül UI'dan bağımsızdır; app.py
# içindeki Streamlit sekmesi bu fonksiyonları çağırır.
# ============================================================
from dataclasses import dataclass


# ------------------------------------------------------------
# 1) ORTAK SKORLAMA YARDIMCILARI
# ------------------------------------------------------------
def _fiyat_cikar(fiyat_metni: str):
    """'12.34 USD' -> 12.34 ; '-' -> None"""
    if not fiyat_metni or fiyat_metni == "-":
        return None
    try:
        return float(str(fiyat_metni).split(" ")[0])
    except Exception:
        return None


def maliyet_skoru_hesapla(fiyat: float, min_fiyat: float, max_fiyat: float) -> float:
    """Fiyatı 0-100 arasına normalize eder; DÜŞÜK fiyat -> YÜKSEK skor."""
    if fiyat is None:
        return 0.0
    if max_fiyat is None or min_fiyat is None or max_fiyat <= min_fiyat:
        return 100.0
    oran = (fiyat - min_fiyat) / (max_fiyat - min_fiyat)
    return round((1 - min(max(oran, 0), 1)) * 100, 1)


def tedarik_skoru_hesapla(karsilama_yuzde: float) -> float:
    """Karşılama oranını (%) 0-100 skoruna sıkıştırır (>=100% karşılama = tam puan)."""
    if karsilama_yuzde is None:
        return 0.0
    return round(min(max(karsilama_yuzde, 0), 100), 1)


def risk_skoru_ters_cevir(risk: float) -> float:
    """Risk skoru (0-100, yüksek=kötü) -> uygunluk skoru (yüksek=iyi)."""
    if risk is None:
        return 0.0
    return round(100 - min(max(risk, 0), 100), 1)


def teslim_skoru_hesapla(teslim_suresi, min_teslim, max_teslim) -> float:
    """Teslim süresini (gün) 0-100 arasına normalize eder; KISA süre -> YÜKSEK skor.
    Veri yoksa (None) nötr bir skor (50) döner; bilinmeyen teslim süresi diğer
    kriterlere göre haksız yere cezalandırılmasın diye."""
    if teslim_suresi is None:
        return 50.0
    if min_teslim is None or max_teslim is None or max_teslim <= min_teslim:
        return 100.0
    oran = (teslim_suresi - min_teslim) / (max_teslim - min_teslim)
    return round((1 - min(max(oran, 0), 1)) * 100, 1)


def moq_uygunluk_skoru(moq, gereken_miktar) -> float:
    """Minimum Sipariş Adedi (MOQ) ihtiyaçtan büyükse skoru düşürür.
    MOQ bilinmiyorsa ya da ihtiyacın altındaysa tam puan (100) verir."""
    if not moq or moq <= 0 or not gereken_miktar or gereken_miktar <= 0:
        return 100.0
    if moq <= gereken_miktar:
        return 100.0
    fazlalik_orani = (moq - gereken_miktar) / gereken_miktar
    return round(max(100 - fazlalik_orani * 100, 0), 1)


# ------------------------------------------------------------
# 1.b) DÖVİZ NORMALİZASYONU
# ------------------------------------------------------------
# Not: Bu kurlar sadece varsayılan/örnek değerlerdir. Gerçek karşılaştırma
# için uygulama arayüzündeki "Döviz Kurları" bölümünden GÜNCEL kurları girin.
VARSAYILAN_KUR_TABLOSU = {"USD": 1.0, "EUR": 1.08, "TRY": 0.030, "GBP": 1.27}


def usd_karsiligi(fiyat, para_birimi: str, kur_tablosu: dict = None):
    """Farklı para birimlerindeki fiyatları ortak bir birime (USD) çevirir,
    böylece 'en ucuz teklif' kıyaslaması para birimi karışıklığından etkilenmez."""
    if fiyat is None:
        return None
    tablo = kur_tablosu or VARSAYILAN_KUR_TABLOSU
    # para_birimi boş/None/NaN (örn. data_editor'da doldurulmamış bir hücre) olabilir;
    # bu durumda NaN != NaN olduğu için standart 'or' kontrolü yetmez, ayrıca kontrol et.
    if not para_birimi or para_birimi != para_birimi:
        para_birimi = "USD"
    try:
        carpan = tablo.get(str(para_birimi).strip().upper(), 1.0)
    except Exception:
        carpan = 1.0
    try:
        return round(float(fiyat) * carpan, 4)
    except Exception:
        return None


@dataclass
class Agirliklar:
    maliyet: float = 0.25
    risk: float = 0.25
    tedarik: float = 0.25
    teslim: float = 0.25

    @classmethod
    def normalize_et(cls, maliyet: float, risk: float, tedarik: float, teslim: float):
        toplam = max(maliyet + risk + tedarik + teslim, 1e-9)
        return cls(maliyet / toplam, risk / toplam, tedarik / toplam, teslim / toplam)


def agirlikli_skor(maliyet_skoru: float, risk_skoru: float, tedarik_skoru: float,
                    teslim_skoru: float, agirlik: Agirliklar) -> float:
    return round(
        maliyet_skoru * agirlik.maliyet
        + risk_skoru * agirlik.risk
        + tedarik_skoru * agirlik.tedarik
        + teslim_skoru * agirlik.teslim,
        1,
    )


# ------------------------------------------------------------
# 1.c) AĞIRLIK SEÇİMİNE GÖRE SÖZEL TAVSİYE
# ------------------------------------------------------------
_KRITER_BILGI = {
    "maliyet": {
        "ad": "Maliyet",
        "artan_yarar": "en düşük birim fiyatlı teklifler öne çıkar",
        "olasi_bedel": "stoğu/teslim süresi kısıtlı ya da daha az güvenilir (yüksek riskli) tedarikçiler seçilebilir",
    },
    "risk": {
        "ad": "Risk / Arz Güvenliği",
        "artan_yarar": "düşük riskli ve yaşam döngüsü 'Aktif' olan parçalar/tedarikçiler öne çıkar",
        "olasi_bedel": "daha pahalı ya da stok/teslim süresi görece zayıf seçenekler öne çıkabilir",
    },
    "tedarik": {
        "ad": "Tedarik / Stok",
        "artan_yarar": "yüksek stoklu ve MOQ'su ihtiyacınıza uygun tedarikçiler öne çıkar",
        "olasi_bedel": "fiyat daha yüksek olabilir ve risk skoru kararda daha az belirleyici olur",
    },
    "teslim": {
        "ad": "Teslimat Süresi",
        "artan_yarar": "en hızlı teslimat yapan tedarikçiler öne çıkar",
        "olasi_bedel": "maliyet artabilir ve tedarikçinin güvenilirliği (risk skoru) kararda daha az ağırlık kazanır",
    },
}


def agirlik_yorumla(agirlik: "Agirliklar") -> list:
    """Seçilen ağırlıklara göre kullanıcıya sözel/insan-okunur tavsiyeler üretir.
    Örn: 'Teslimat süresine öncelik verdiniz -> maliyet ve risk toleransınız arttı.'"""
    degerler = {
        "maliyet": agirlik.maliyet, "risk": agirlik.risk,
        "tedarik": agirlik.tedarik, "teslim": agirlik.teslim,
    }
    siralı = sorted(degerler.items(), key=lambda x: x[1], reverse=True)
    en_yuksek_k, en_yuksek_v = siralı[0]
    en_dusuk_k, en_dusuk_v = siralı[-1]

    yorumlar = []
    if (en_yuksek_v - en_dusuk_v) < 0.10:
        yorumlar.append(
            "⚖️ Ağırlıklarınız oldukça dengeli dağılmış. Sistem hiçbir kritere aşırı öncelik "
            "vermeden orta yollu (dengeli) öneriler sunacaktır."
        )
        return yorumlar

    yuksek = _KRITER_BILGI[en_yuksek_k]
    dusuk = _KRITER_BILGI[en_dusuk_k]
    yorumlar.append(
        f"📌 **{yuksek['ad']}** kriterine en yüksek ağırlığı verdiniz (%{en_yuksek_v*100:.0f}). "
        f"Bunun anlamı: {yuksek['artan_yarar']}. Bedeli ise şu olabilir: {dusuk['ad'] if False else yuksek['olasi_bedel']}."
    )
    yorumlar.append(
        f"⚠️ **{dusuk['ad']}** kriterine en düşük ağırlığı verdiniz (%{en_dusuk_v*100:.0f}). "
        f"Bu alandaki zayıf sonuçlar (ör. kötü {dusuk['ad'].lower()} durumu) karar skorunu artık çok az etkiler."
    )

    if agirlik.teslim >= 0.40:
        yorumlar.append(
            "🚚 Teslimat süresine bu denli yüksek öncelik verdiğinizde, sistem bazen daha küçük/yeni "
            "veya sınanmamış tedarikçileri de öne çıkarabilir; kurumsal güvenilirlik ve risk toleransınızın "
            "arttığını unutmayın."
        )
    if agirlik.maliyet >= 0.40:
        yorumlar.append(
            "💰 Maliyete bu denli yüksek öncelik verdiğinizde, en ucuz teklif genelde kazanır; "
            "ancak bu tedarikçinin stok/MOQ veya yaşam döngüsü durumu göz ardı edilme riski artar."
        )
    if agirlik.risk >= 0.40:
        yorumlar.append(
            "🛡️ Risk/Arz Güvenliğine bu denli yüksek öncelik verdiğinizde, sistem daha muhafazakâr "
            "(genelde daha pahalı) tedarikçileri önerecektir."
        )
    return yorumlar


# ------------------------------------------------------------
# 2) TEDARİKÇİ / PARÇA SEÇİMİ + ALTERNATİF KARŞILAŞTIRMA
# ------------------------------------------------------------
def aday_degerlendir(isim: str, fiyat_metni: str, risk: float, karsilama: float,
                      yasam: str, min_fiyat: float, max_fiyat: float,
                      agirlik: Agirliklar, mevcut_mi: bool = False,
                      teslim_suresi=None, min_teslim=None, max_teslim=None,
                      moq=None, gereken_miktar=None,
                      para_birimi: str = "USD", kur_tablosu: dict = None) -> dict:
    """Tek bir adayı (orijinal parça, bir Ekom alternatifi ya da manuel bir
    tedarikçi teklifi) puanlar.

    - teslim_suresi/min_teslim/max_teslim: kısa teslim -> yüksek skor (4. kriter).
    - moq/gereken_miktar: MOQ ihtiyaçtan büyükse tedarik skoru cezalandırılır.
    - para_birimi/kur_tablosu: fiyat, kıyaslama öncesi ortak birime (USD) çevrilir.
    """
    fiyat_ham = _fiyat_cikar(fiyat_metni)
    fiyat = usd_karsiligi(fiyat_ham, para_birimi, kur_tablosu) if fiyat_ham is not None else None

    m_skor = maliyet_skoru_hesapla(fiyat, min_fiyat, max_fiyat)
    r_skor = risk_skoru_ters_cevir(risk)
    t_skor_stok = tedarik_skoru_hesapla(karsilama)
    t_skor_moq = moq_uygunluk_skoru(moq, gereken_miktar)
    t_skor = round((t_skor_stok + t_skor_moq) / 2, 1) if moq else t_skor_stok
    tsl_skor = teslim_skoru_hesapla(teslim_suresi, min_teslim, max_teslim)
    toplam = agirlikli_skor(m_skor, r_skor, t_skor, tsl_skor, agirlik)

    return {
        "Aday": isim, "Mevcut Parça": "Evet" if mevcut_mi else "Alternatif",
        "Fiyat": fiyat_metni, "Fiyat (USD)": fiyat, "Risk Skoru": risk, "Karşılama": karsilama,
        "Teslim Süresi (gün)": teslim_suresi, "MOQ": moq,
        "Yaşam Döngüsü": yasam, "Maliyet Skoru": m_skor, "Risk Uygunluk Skoru": r_skor,
        "Tedarik Skoru": t_skor, "Teslim Skoru": tsl_skor, "Karar Skoru": toplam,
    }


def parca_karar_onerisi(adaylar: list, esik_fark: float = 8.0) -> dict:
    """
    adaylar: aday_degerlendir() çıktılarının listesi (ilk eleman = mevcut parça).
    Karar skoruna göre sıralar; en iyi aday mevcut parçadan belirgin şekilde
    (esik_fark puan) iyiyse "Değiştir" önerir, EOL ise skordan bağımsız zorunlu önerir.
    """
    if not adaylar:
        return {"oneri": "Veri Yok", "detay": [], "en_iyi": None}

    mevcut = adaylar[0]
    siralı = sorted(adaylar, key=lambda a: a["Karar Skoru"], reverse=True)
    en_iyi = siralı[0]

    if mevcut["Yaşam Döngüsü"] == "EOL" and en_iyi["Aday"] != mevcut["Aday"]:
        oneri = f"⚠️ Zorunlu Değişim: '{mevcut['Aday']}' EOL. Önerilen: '{en_iyi['Aday']}'"
    elif mevcut["Yaşam Döngüsü"] == "EOL" and en_iyi["Aday"] == mevcut["Aday"]:
        oneri = f"🛑 Kritik: '{mevcut['Aday']}' EOL ve sistemde kayıtlı alternatif bulunamadı. Manuel araştırma gerekli."
    elif en_iyi["Aday"] != mevcut["Aday"] and (en_iyi["Karar Skoru"] - mevcut["Karar Skoru"]) >= esik_fark:
        oneri = f"🔄 Değiştirmeyi Değerlendirin: '{en_iyi['Aday']}' (+{en_iyi['Karar Skoru'] - mevcut['Karar Skoru']:.1f} puan)"
    else:
        oneri = f"✅ Mevcut Parçayı Koruyun: '{mevcut['Aday']}'"

    return {"oneri": oneri, "detay": siralı, "en_iyi": en_iyi, "mevcut": mevcut}


# ------------------------------------------------------------
# 3) MALİYET OPTİMİZASYONU ÖZETİ
# ------------------------------------------------------------
def maliyet_optimizasyon_ozeti(konsolide_df) -> dict:
    """
    Konsolidasyon (aynı Description, farklı MPN) gruplarından elde edilebilecek
    tasarrufu ve tekli-kaynak / yüksek riskli parçalardan kaynaklı maliyet
    riskini özetler. konsolide_df: app.py'de üretilen ana tablo (pandas DataFrame).
    """
    import pandas as pd

    df = konsolide_df.copy()
    df["_maliyet_num"] = df["Toplam Maliyet"].apply(
        lambda x: float(str(x).replace("USD", "").strip()) if "USD" in str(x) else None
    )
    df["_normalize"] = df["Description"].astype(str).str.strip().str.lower()

    toplam_tasarruf = 0.0
    gruplar = []
    for _, grup in df.groupby("_normalize"):
        if grup["MPN"].nunique() <= 1:
            continue
        gecerli = grup.dropna(subset=["_maliyet_num"])
        if gecerli.empty or len(gecerli) < 2:
            continue
        en_ucuz = gecerli.loc[gecerli["_maliyet_num"].idxmin()]
        mevcut_toplam = gecerli["_maliyet_num"].sum()
        optimize_toplam = en_ucuz["_maliyet_num"] * len(gecerli)
        tasarruf = max(mevcut_toplam - optimize_toplam, 0)
        toplam_tasarruf += tasarruf
        if tasarruf > 0:
            gruplar.append({
                "Ürün": grup.iloc[0]["Description"],
                "Varyasyon Sayısı": len(gecerli),
                "Önerilen MPN": en_ucuz["MPN"],
                "Tahmini Tasarruf (USD)": round(tasarruf, 2),
            })

    kritik_parca_sayisi = int((df["En Uygun Tedarikçi"] == "Kritik (Tek Kaynak Yetersiz)").sum())
    yuksek_riskli_maliyet = df.loc[df["Risk Skoru"] >= 70, "_maliyet_num"].dropna().sum()

    gruplar.sort(key=lambda g: g["Tahmini Tasarruf (USD)"], reverse=True)
    return {
        "toplam_tasarruf": round(toplam_tasarruf, 2),
        "konsolidasyon_firsatlari": gruplar,
        "kritik_parca_sayisi": kritik_parca_sayisi,
        "yuksek_riskli_maliyet": round(float(yuksek_riskli_maliyet or 0), 2),
    }


# ------------------------------------------------------------
# 4) YAP YA DA SATIN AL (İç Üretim vs. Sözleşmeli Üretim/EMS)
# ------------------------------------------------------------
def yap_sat_al_hesapla(adet: int, ic_sabit_maliyet: float, ic_birim_maliyet: float,
                        dis_sabit_maliyet: float, dis_birim_maliyet: float) -> dict:
    """Basit toplam-maliyet karşılaştırması + başabaş (breakeven) hacmi."""
    ic_toplam = ic_sabit_maliyet + ic_birim_maliyet * adet
    dis_toplam = dis_sabit_maliyet + dis_birim_maliyet * adet

    fark_birim = ic_birim_maliyet - dis_birim_maliyet
    fark_sabit = dis_sabit_maliyet - ic_sabit_maliyet
    breakeven = (fark_sabit / fark_birim) if fark_birim not in (0, None) else None
    if breakeven is not None and breakeven < 0:
        breakeven = None

    if ic_toplam < dis_toplam:
        oneri = "🏭 İç Bünyede Üretim önerilir"
    elif dis_toplam < ic_toplam:
        oneri = "🤝 Sözleşmeli Üretim (EMS) önerilir"
    else:
        oneri = "⚖️ İki seçenek de eşdeğer"

    return {
        "ic_toplam": round(ic_toplam, 2), "dis_toplam": round(dis_toplam, 2),
        "fark": round(abs(ic_toplam - dis_toplam), 2), "oneri": oneri,
        "breakeven_adet": round(breakeven) if breakeven else None,
    }


def yap_sat_al_egri_verisi(ic_sabit_maliyet: float, ic_birim_maliyet: float,
                            dis_sabit_maliyet: float, dis_birim_maliyet: float,
                            max_adet: int, nokta_sayisi: int = 30) -> dict:
    """Grafik için: artan üretim adedine göre iki maliyet eğrisi."""
    adimlar = max(1, max_adet // nokta_sayisi) or 1
    adetler = list(range(0, max_adet + adimlar, adimlar))
    ic_egri = [ic_sabit_maliyet + ic_birim_maliyet * a for a in adetler]
    dis_egri = [dis_sabit_maliyet + dis_birim_maliyet * a for a in adetler]
    return {"adetler": adetler, "ic_egri": ic_egri, "dis_egri": dis_egri}


def yap_sat_al_duyarlilik(adet: int, ic_sabit_maliyet: float, ic_birim_maliyet: float,
                           dis_sabit_maliyet: float, dis_birim_maliyet: float,
                           degisim_yuzdesi: float = 15.0) -> dict:
    """Duyarlılık (tornado) analizi: her bir girdiyi tek tek +/- degisim_yuzdesi
    kadar oynatıp başabaş (breakeven) adedinin ne kadar değiştiğini gösterir.
    Böylece 'hangi varsayım kararı en çok etkiliyor' sorusuna cevap verir."""
    taban = yap_sat_al_hesapla(adet, ic_sabit_maliyet, ic_birim_maliyet,
                                dis_sabit_maliyet, dis_birim_maliyet)
    taban_breakeven = taban["breakeven_adet"]

    parametreler = {
        "İç Sabit Maliyet": "ic_sabit_maliyet",
        "İç Birim Maliyet": "ic_birim_maliyet",
        "EMS Sabit Maliyet (NRE)": "dis_sabit_maliyet",
        "EMS Birim Maliyet": "dis_birim_maliyet",
    }
    girdiler = {
        "ic_sabit_maliyet": ic_sabit_maliyet, "ic_birim_maliyet": ic_birim_maliyet,
        "dis_sabit_maliyet": dis_sabit_maliyet, "dis_birim_maliyet": dis_birim_maliyet,
    }

    sonuclar = []
    for etiket, anahtar in parametreler.items():
        yuksek_girdi = dict(girdiler)
        yuksek_girdi[anahtar] = girdiler[anahtar] * (1 + degisim_yuzdesi / 100)
        yuksek = yap_sat_al_hesapla(adet, **yuksek_girdi)

        dusuk_girdi = dict(girdiler)
        dusuk_girdi[anahtar] = girdiler[anahtar] * (1 - degisim_yuzdesi / 100)
        dusuk = yap_sat_al_hesapla(adet, **dusuk_girdi)

        sonuclar.append({
            "Parametre": etiket,
            f"-%{degisim_yuzdesi:.0f}": dusuk["breakeven_adet"],
            "Taban": taban_breakeven,
            f"+%{degisim_yuzdesi:.0f}": yuksek["breakeven_adet"],
        })

    return {"taban_breakeven": taban_breakeven, "detay": sonuclar, "degisim_yuzdesi": degisim_yuzdesi}


def parca_degisim_breakeven(mevcut_birim_fiyat: float, alternatif_birim_fiyat: float,
                             gecis_maliyeti: float = 0.0):
    """Bir parçayı alternatifiyle değiştirmenin kaç adette karlı hale geleceğini
    hesaplar (yeniden nitelendirme/mühendislik/test gibi tek seferlik geçiş
    maliyetini, birim fiyat farkının karşılaması gereken adet).
    Alternatif daha pahalıysa (ya da eşitse) None döner: geçişin hiçbir adette
    'geri ödemesi' olmaz."""
    if mevcut_birim_fiyat is None or alternatif_birim_fiyat is None:
        return None
    fark = mevcut_birim_fiyat - alternatif_birim_fiyat
    if fark <= 0:
        return None
    if not gecis_maliyeti or gecis_maliyeti <= 0:
        return 0
    import math
    return math.ceil(gecis_maliyeti / fark)
