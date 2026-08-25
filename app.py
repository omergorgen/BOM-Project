import os
import concurrent.futures

import streamlit as st
import pandas as pd
import requests
import google.generativeai as genai

# ============================================================
# AYARLAR VE API ANAHTARLARI
# ============================================================
# GÜVENLİK: Anahtarları kodun içine ASLA yazmayın.
# Yerelde çalışırken .streamlit/secrets.toml dosyasına şunu ekleyin:
#
#   GEMINI_API_KEY = "..."
#   NEXAR_CLIENT_ID = "..."
#   NEXAR_CLIENT_SECRET = "..."
#
# Streamlit Cloud'da ise "Secrets" bölümünden aynı şekilde tanımlayın.
# Ortam değişkeni olarak da verebilirsiniz (os.environ).
st.set_page_config(page_title="BOM Analiz Aracı", layout="wide")


def secret_veya_env(anahtar: str) -> str:
    """secrets.toml dosyası hiç yoksa st.secrets.get() bile hata fırlatabildiği
    için (StreamlitSecretNotFoundError), bunu güvenli şekilde yakalıyoruz."""
    try:
        deger = st.secrets.get(anahtar)
        if deger:
            return deger
    except Exception:
        pass
    return os.environ.get(anahtar, "")


GEMINI_API_KEY = secret_veya_env("GEMINI_API_KEY")
NEXAR_CLIENT_ID = secret_veya_env("NEXAR_CLIENT_ID")
NEXAR_CLIENT_SECRET = secret_veya_env("NEXAR_CLIENT_SECRET")

# Gemini model adı: Google zaman zaman model isimlerini değiştirip eskilerini
# emekliye ayırıyor. Buraya yazacağınız ismi Google AI Studio / API
# dokümantasyonundan güncel olarak teyit edin (genai.list_models() ile de
# hesabınızın erişebildiği modelleri görebilirsiniz).
GEMINI_MODEL_NAME = "gemini-3.6-flash"

REQUIRED_COLUMNS = ["MPN", "Manufacturer", "Description", "Qty", "RefDes"]

# ============================================================
# NEXAR (OCTOPART) API — TÜM ALTERNATİF TEDARİKÇİLERDEN FİYAT + STOK
# ============================================================
# Nexar, tek bir sorguda Mouser, Digi-Key, Arrow, Farnell, LCSC gibi onlarca
# tedarikçinin fiyat/stok verisini aynı anda döndüren bir "toplayıcı" API'dir.
# Ücretsiz Client ID / Client Secret için: https://nexar.com/
# (Uygulamalar > uygulamanız > Yetkilendirme sekmesinden alınır.)

NEXAR_TOKEN_URL = "https://identity.nexar.com/connect/token"
NEXAR_GRAPHQL_URL = "https://api.nexar.com/graphql"

NEXAR_SORGU = """
query Search($mpn: String!) {
  supSearchMpn(q: $mpn, limit: 1) {
    results {
      part {
        mpn
        shortDescription
        manufacturer { name }
        specs {
          attribute { shortname name }
          displayValue
        }
        sellers {
          company { name }
          offers {
            sku
            inventoryLevel
            moq
            prices { quantity price currency }
            clickUrl
          }
        }
      }
    }
  }
}
"""


@st.cache_data(show_spinner=False, ttl=60 * 30)  # token 30 dakika cache'lensin
def nexar_token_al() -> str:
    """Nexar OAuth2 (client_credentials) akışıyla erişim token'ı alır."""
    if not NEXAR_CLIENT_ID or not NEXAR_CLIENT_SECRET:
        return ""
    try:
        resp = requests.post(
            NEXAR_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": NEXAR_CLIENT_ID,
                "client_secret": NEXAR_CLIENT_SECRET,
                # NOT: "scope" parametresi kasıtlı olarak gönderilmiyor.
                # Nexar uygulamanızın kapsamı (Supply) zaten panelde
                # Client ID/Secret'a bağlı; burada scope="supply" göndermek
                # "invalid_scope" hatasına yol açıyor.
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token", "")
        else:
            print(f"[Nexar Token Hatası] status={resp.status_code} body={resp.text[:300]}")
    except requests.exceptions.RequestException as e:
        print(f"[Nexar Token Bağlantı Hatası] {e}")
    return ""


@st.cache_data(show_spinner=False, ttl=60 * 60)
def nexar_parca_sorgula(mpn: str) -> dict:
    """Verilen MPN için Nexar'dan tüm tedarikçi tekliflerini, temel parça
    bilgisini ve yaşam döngüsü durumunu (Production / NRND / EOL / Obsolete) çeker.

    Döndürdüğü sözlük:
    {
        "bulundu": bool,
        "aciklama": str,
        "uretici": str,
        "teklifler": [(tedarikci, fiyat, para_birimi, adet_kirilimi, stok, link), ...]  # fiyata göre sıralı
        "yasam_durumu_ham": str,       # Nexar'dan gelen orijinal metin, örn. "Obsolete (Last Updated: 3 months ago)"
        "yasam_durumu_kategori": str,  # sadeleştirilmiş: "Aktif" / "NRND" / "EOL" / "Obsolete" / "Bilinmiyor"
        "hata": str veya None
    }
    """
    bos_sonuc = {
        "bulundu": False, "aciklama": "-", "uretici": "-", "teklifler": [],
        "yasam_durumu_ham": "-", "yasam_durumu_kategori": "Bilinmiyor", "hata": None,
    }

    if not isinstance(mpn, str) or not mpn.strip():
        return {**bos_sonuc, "hata": "Geçersiz MPN"}

    token = nexar_token_al()
    if not token:
        return {**bos_sonuc, "hata": "Nexar anahtarı eksik/geçersiz"}

    try:
        resp = requests.post(
            NEXAR_GRAPHQL_URL,
            json={"query": NEXAR_SORGU, "variables": {"mpn": mpn}},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        return {**bos_sonuc, "hata": f"Bağlantı hatası: {e}"}

    if resp.status_code != 200:
        return {**bos_sonuc, "hata": f"API hatası ({resp.status_code})"}

    try:
        data = resp.json()
    except ValueError:
        return {**bos_sonuc, "hata": "Geçersiz yanıt (JSON değil)"}

    if data.get("errors"):
        return {**bos_sonuc, "hata": str(data["errors"])[:200]}

    sonuclar = data.get("data", {}).get("supSearchMpn", {}).get("results", []) or []
    if not sonuclar:
        return {**bos_sonuc, "hata": None}  # bulunamadı ama hata da yok

    part = sonuclar[0].get("part", {}) or {}
    aciklama = part.get("shortDescription") or "-"
    uretici = (part.get("manufacturer") or {}).get("name", "-")

    # Yaşam döngüsü durumunu specs listesinden buluyoruz.
    # Nexar bunu "shortname": "lifecyclestatus" olan bir attribute olarak döner.
    yasam_durumu_ham = "-"
    for spec in part.get("specs", []) or []:
        attribute = spec.get("attribute") or {}
        if attribute.get("shortname") == "lifecyclestatus":
            yasam_durumu_ham = spec.get("displayValue") or "-"
            break

    yasam_durumu_kategori = yasam_durumu_kategorisi_belirle(yasam_durumu_ham)

    teklifler = []
    for satici in part.get("sellers", []) or []:
        tedarikci_adi = (satici.get("company") or {}).get("name", "Bilinmiyor")
        for teklif in satici.get("offers", []) or []:
            fiyatlar = teklif.get("prices") or []
            if not fiyatlar:
                continue
            en_dusuk_miktar_fiyati = min(fiyatlar, key=lambda p: p.get("quantity", 0))
            teklifler.append((
                tedarikci_adi,
                en_dusuk_miktar_fiyati.get("price"),
                en_dusuk_miktar_fiyati.get("currency", "USD"),
                en_dusuk_miktar_fiyati.get("quantity", 1),
                teklif.get("inventoryLevel", 0) or 0,
                teklif.get("clickUrl", ""),
            ))

    teklifler.sort(key=lambda t: (t[1] is None, t[1]))  # en ucuzdan pahalıya

    return {
        "bulundu": True,
        "aciklama": aciklama,
        "uretici": uretici,
        "teklifler": teklifler,
        "yasam_durumu_ham": yasam_durumu_ham,
        "yasam_durumu_kategori": yasam_durumu_kategori,
        "hata": None,
    }


def yasam_durumu_kategorisi_belirle(ham_metin: str) -> str:
    """Nexar'dan gelen serbest metin yaşam döngüsü ifadesini (üreticiden
    üreticiye "Obsolete", "Discontinued", "NRND", "Not Recommended for New
    Designs" gibi farklı ifadeler kullanılabiliyor) sabit birkaç kategoriye
    indirger, böylece tabloda ve risk hesabında tutarlı şekilde kullanılabilir."""
    if not ham_metin or ham_metin == "-":
        return "Bilinmiyor"

    metin = ham_metin.lower()

    if "obsolete" in metin:
        return "Obsolete"
    if "eol" in metin or "end of life" in metin or "discontinued" in metin:
        return "EOL"
    if "nrnd" in metin or "not recommended for new designs" in metin:
        return "NRND"
    if "active" in metin or "production" in metin:
        return "Aktif"
    return "Diğer"


def uygun_en_ucuz_teklifi_bul(teklifler: list, gereken_miktar: int):
    """Teklif listesinden, TEK BAŞINA gereken miktarı karşılayabilecek
    (stok >= gereken_miktar) tedarikçiler arasından en ucuzunu bulur.

    Neden bu ayrım gerekli? "En ucuz teklif" listenin en başındaki teklif
    olabilir, ama o tedarikçide sadece 10 adet stok varken bizim 2000 adete
    ihtiyacımız olabilir. Böyle bir tedarikçiyi "en ucuz" diye önermek
    yanıltıcı olur -- sipariş verilemez ya da parça parça birden fazla
    siparişle tamamlanması gerekir. Bu fonksiyon, gerçekten TEK siparişte
    ihtiyacı karşılayabilecek en uygun (ucuz + yeterli stoklu) seçeneği bulur.

    Döner: (tedarikci, fiyat, para_birimi, stok) ya da hiçbiri yetmiyorsa None.
    """
    uygun_teklifler = [t for t in teklifler if (t[4] or 0) >= gereken_miktar]
    if not uygun_teklifler:
        return None
    # teklifler zaten fiyata göre sıralı geliyor, ama filtrelemeden sonra
    # sıralamayı garantiye almak için tekrar sıralıyoruz.
    uygun_teklifler.sort(key=lambda t: (t[1] is None, t[1]))
    en_uygun = uygun_teklifler[0]
    return (en_uygun[0], en_uygun[1], en_uygun[2], en_uygun[4])


def toplu_nexar_sorgula(mpn_listesi, max_workers: int = 5) -> dict:
    """Birden çok MPN için Nexar verilerini paralel çeker."""
    sonuclar = {}
    benzersiz_mpnler = list(dict.fromkeys(mpn_listesi))  # sırayı koruyarak tekilleştir

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_mpn = {
            executor.submit(nexar_parca_sorgula, mpn): mpn for mpn in benzersiz_mpnler
        }
        for future in concurrent.futures.as_completed(future_to_mpn):
            mpn = future_to_mpn[future]
            try:
                sonuclar[mpn] = future.result()
            except Exception as e:
                import traceback
                print(f"[Nexar Hatası] MPN={mpn}: {e}")
                traceback.print_exc()
                sonuclar[mpn] = {
                    "bulundu": False, "aciklama": "-", "uretici": "-",
                    "teklifler": [], "yasam_durumu_ham": "-",
                    "yasam_durumu_kategori": "Bilinmiyor",
                    "hata": f"Beklenmeyen hata: {e}",
                }

    return sonuclar


def risk_hesapla(nexar_sonucu: dict, gereken_miktar: int) -> tuple:
    """Üretim planına göre stok yeterliliğini VE yaşam döngüsü durumunu
    birlikte değerlendiren risk skoru.

    Mantık:
    1. Nexar'daki tüm tedarikçilerin stoklarını toplayıp TOPLAM bulunabilir
       stok miktarını çıkarıyoruz.
    2. Bu toplamı, BOM'daki birim miktar × üretim adedi ile karşılaştırıyoruz.
    3. Tek bir tedarikçiye bağımlı kalmak ayrı bir risk faktörü.
    4. Parça Obsolete/EOL ise -- şu an stok görünse bile -- üretici artık
       üretmiyor demektir; bu yüzden stok durumundan BAĞIMSIZ olarak yüksek
       risk veriyoruz (mevcut stok tükenince yeniden temin imkânı yok).
       NRND (yeni tasarımlar için önerilmiyor) parçalarda ise orta düzeyde
       ek risk ekliyoruz; hâlâ üretiliyor ama üretici zaten "yeni tasarımda
       kullanmayın" diyor.

    Döndürür: (risk_skoru, toplam_stok, karsilama_orani_yuzde)
    """
    if nexar_sonucu.get("hata"):
        return 50, 0, 0  # veri alınamadı, temkinli orta risk

    if not nexar_sonucu.get("bulundu"):
        return 100, 0, 0  # hiçbir tedarikçide bulunamadı -> muhtemelen obsolete/nadir

    yasam_kategori = nexar_sonucu.get("yasam_durumu_kategori", "Bilinmiyor")

    # Obsolete/EOL: üretici artık üretmiyor. Mevcut stok tükendiğinde tekrar
    # temin edilemeyeceği için stok yeterli görünse bile risk çok yüksek.
    if yasam_kategori in ("Obsolete", "EOL"):
        teklifler = nexar_sonucu.get("teklifler", [])
        toplam_stok = sum((t[4] or 0) for t in teklifler) if teklifler else 0
        karsilama_orani = (toplam_stok / gereken_miktar * 100) if gereken_miktar > 0 else 0
        return 95, toplam_stok, min(karsilama_orani, 999)

    teklifler = nexar_sonucu.get("teklifler", [])
    if not teklifler:
        return 90, 0, 0  # kayıt var ama satılabilir teklif yok

    # Tüm tedarikçilerdeki stokları topluyoruz (aynı parçayı birden fazla
    # yerden alabileceğiniz için stoklar toplanabilir).
    toplam_stok = sum((t[4] or 0) for t in teklifler)
    stoklu_tedarikci_sayisi = sum(1 for t in teklifler if (t[4] or 0) > 0)

    karsilama_orani = (toplam_stok / gereken_miktar * 100) if gereken_miktar > 0 else 100
    karsilama_orani = min(karsilama_orani, 999)  # aşırı büyük sayıları sınırla (görüntü için)

    if toplam_stok == 0:
        return 95, toplam_stok, 0  # hiç stok yok -> çok yüksek risk

    if toplam_stok < gereken_miktar:
        # Kısmen karşılıyor ama yetersiz -> ne kadar eksikse risk o kadar yüksek
        eksik_oran = 1 - (toplam_stok / gereken_miktar)
        risk = int(60 + eksik_oran * 35)  # 60-95 arası kademeli
        return risk, toplam_stok, karsilama_orani

    # Stok yeterli, ama tek tedarikçiye bağımlıysak yine de bir miktar risk var
    if stoklu_tedarikci_sayisi == 1:
        risk = 35
    else:
        risk = 10  # yeterli stok + birden fazla kaynak -> güvenli

    # NRND: hâlâ üretiliyor ama üretici yeni tasarımda kullanılmamasını
    # öneriyor -- bu, orta vadede Obsolete'e dönüşme ihtimalini artırır.
    if yasam_kategori == "NRND":
        risk = min(risk + 20, 89)  # Obsolete/EOL'ün (95) altında kalsın

    return risk, toplam_stok, karsilama_orani


# ============================================================
# ARAYÜZ VE ANALİZ
# ============================================================
st.title("BOM (Bill of Materials) Analiz Aracı")
st.write(
    "Bu uygulama, Nexar (Octopart) API'si ve LLM (Yapay Zeka) kullanarak BOM "
    "listenizi analiz eder: her parçayı onlarca tedarikçide arar, fiyat/stok "
    "karşılaştırması ve risk skoru üretir. Bir yapay zeka modeli olduğu için "
    "hata yapabilir."
)
st.markdown("---")

if not NEXAR_CLIENT_ID or not NEXAR_CLIENT_SECRET:
    st.warning(
        "Nexar API bilgileri bulunamadı. `secrets.toml` dosyasına "
        "`NEXAR_CLIENT_ID` ve `NEXAR_CLIENT_SECRET` eklemeden gerçek "
        "tedarik/fiyat verisi çekilemez. Ücretsiz kayıt: https://nexar.com/"
    )

yuklenen_dosya = st.file_uploader(
    "Lütfen BOM dosyanızı (Excel veya CSV) yükleyin", type=["csv", "xlsx"]
)

if yuklenen_dosya is not None:
    try:
        if yuklenen_dosya.name.endswith(".csv"):
            df = pd.read_csv(yuklenen_dosya)
        else:
            df = pd.read_excel(yuklenen_dosya)
    except Exception as e:
        st.error(f"Dosya okunurken bir hata oluştu: {e}")
        st.stop()

    eksik_sutunlar = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if eksik_sutunlar:
        st.error(
            "Yüklenen dosyada şu sütunlar eksik: "
            f"{', '.join(eksik_sutunlar)}. Beklenen sütunlar: {', '.join(REQUIRED_COLUMNS)}."
        )
        st.stop()

    # dropna=False: MPN/Manufacturer/Description içinde boş hücre olan
    # satırların sessizce silinmesini önler.
    konsolide_df = (
        df.groupby(["MPN", "Manufacturer", "Description"], dropna=False)
        .agg({"Qty": "sum", "RefDes": lambda x: ", ".join(map(str, x))})
        .reset_index()
    )

    st.markdown("---")
    uretim_adedi = st.number_input(
        "Tahmini üretilecek ürün (kart) adedi:",
        min_value=1,
        value=100,
        step=1,
        help=(
            "BOM'daki her parça, tek bir karttaki miktarı (Qty) gösterir. "
            "Burada girdiğiniz sayı ile çarpılarak toplam ihtiyaç hesaplanır "
            "ve stok yeterliliği buna göre değerlendirilir."
        ),
    )

    st.subheader("Gerçek Zamanlı Komponent Analizi (Nexar üzerinden)")

    with st.spinner("Tüm tedarikçilerden fiyat/stok verisi çekiliyor..."):
        nexar_map = toplu_nexar_sorgula(konsolide_df["MPN"].tolist())

        def satir_zenginlestir(row):
            mpn = row["MPN"]
            birim_qty = row["Qty"]
            gereken_miktar = int(birim_qty) * int(uretim_adedi)

            sonuc = nexar_map.get(mpn, {"bulundu": False, "teklifler": [], "hata": "Sonuç yok"})
            teklifler = sonuc.get("teklifler", [])
            risk, toplam_stok, karsilama_orani = risk_hesapla(sonuc, gereken_miktar)

            if sonuc.get("hata"):
                durum = f"Hata: {sonuc['hata']}"
            elif not sonuc.get("bulundu"):
                durum = "Bulunamadı"
            else:
                durum = "Bulundu"

            # Tek bir siparişte ihtiyacımızı (gereken_miktar) tam karşılayabilecek
            # tedarikçiler arasından en ucuzunu buluyoruz -- sadece "listedeki en
            # ucuz" değil, "bizim için gerçekten kullanılabilir en ucuz".
            uygun_teklif = uygun_en_ucuz_teklifi_bul(teklifler, gereken_miktar)
            if uygun_teklif:
                uygun_tedarikci, uygun_fiyat, uygun_para_birimi, uygun_stok = uygun_teklif
                uygun_fiyat_metni = f"{uygun_fiyat} {uygun_para_birimi}" if uygun_fiyat is not None else "-"
                uygun_toplam_maliyet = (
                    f"{uygun_fiyat * gereken_miktar:.2f} {uygun_para_birimi}"
                    if uygun_fiyat is not None else "-"
                )
            else:
                uygun_tedarikci = "Tek kaynak yetersiz"
                uygun_fiyat_metni = "-"
                uygun_toplam_maliyet = "-"

            karsilama_metni = f"%{karsilama_orani:.0f}" if teklifler else "-"
            yasam_durumu = sonuc.get("yasam_durumu_kategori", "Bilinmiyor")

            return pd.Series([
                durum, yasam_durumu, len(teklifler),
                gereken_miktar, toplam_stok, karsilama_metni,
                uygun_tedarikci, uygun_fiyat_metni, uygun_toplam_maliyet,
                risk,
            ])

        konsolide_df[
            [
                "Durum", "Yaşam Döngüsü", "Tedarikçi Sayısı",
                "Gereken Miktar", "Toplam Stok", "Stok Karşılama",
                "Uygun En Ucuz Tedarikçi", "Birim Fiyat", "Toplam Maliyet",
                "Risk Skoru",
            ]
        ] = konsolide_df.apply(satir_zenginlestir, axis=1)

    # Risk skoruna göre görsel vurgulama
    def risk_renklendir(val):
        try:
            v = float(val)
        except (TypeError, ValueError):
            return ""
        if v >= 70:
            return "background-color: #ffcccc"
        if v >= 30:
            return "background-color: #fff3cd"
        return "background-color: #d4edda"

    def yasam_durumu_renklendir(val):
        if val in ("Obsolete", "EOL"):
            return "background-color: #ffcccc"
        if val == "NRND":
            return "background-color: #fff3cd"
        if val == "Aktif":
            return "background-color: #d4edda"
        return ""

    def uygun_tedarikci_renklendir(val):
        if val == "Tek kaynak yetersiz":
            return "background-color: #ffcccc"
        return ""

    try:
        # Pandas >= 2.1: Styler.map, eski sürümlerde: Styler.applymap
        stil = konsolide_df.style.map(risk_renklendir, subset=["Risk Skoru"])
        stil = stil.map(yasam_durumu_renklendir, subset=["Yaşam Döngüsü"])
        stil = stil.map(uygun_tedarikci_renklendir, subset=["Uygun En Ucuz Tedarikçi"])
    except AttributeError:
        stil = konsolide_df.style.applymap(risk_renklendir, subset=["Risk Skoru"])
        stil = stil.applymap(yasam_durumu_renklendir, subset=["Yaşam Döngüsü"])
        stil = stil.applymap(uygun_tedarikci_renklendir, subset=["Uygun En Ucuz Tedarikçi"])

    st.dataframe(stil, width='stretch')

    yuksek_riskli = konsolide_df[konsolide_df["Risk Skoru"] >= 70]
    if not yuksek_riskli.empty:
        st.error(
            f"{len(yuksek_riskli)} parça, {uretim_adedi} adetlik üretim planı için "
            "yetersiz/bulunamayan stok riski taşıyor. Detay için tabloda "
            "'Gereken Miktar' ve 'Toplam Stok' sütunlarına bakın."
        )

    # Uygun tedarikçiden alınabilecek toplam tahmini maliyet (sadece USD
    # cinsinden fiyatı olan ve tek kaynaktan karşılanabilen parçalar toplanır;
    # farklı para birimlerini karıştırmamak için sadece USD baz alınıyor).
    def usd_maliyeti_ayikla(metin):
        if not isinstance(metin, str) or "USD" not in metin:
            return 0.0
        try:
            return float(metin.replace("USD", "").strip())
        except ValueError:
            return 0.0

    toplam_maliyet_usd = konsolide_df["Toplam Maliyet"].apply(usd_maliyeti_ayikla).sum()
    karsilanamayan_parca_sayisi = (konsolide_df["Uygun En Ucuz Tedarikçi"] == "Tek kaynak yetersiz").sum()

    col1, col2 = st.columns(2)
    col1.metric(f"{uretim_adedi} adet üretim için tahmini toplam maliyet (USD)", f"${toplam_maliyet_usd:,.2f}")
    col2.metric("Tek kaynaktan karşılanamayan parça sayısı", int(karsilanamayan_parca_sayisi))

    if karsilanamayan_parca_sayisi > 0:
        st.caption(
            "Not: 'Tek kaynak yetersiz' olan parçalar toplam maliyete dahil edilmedi "
            "çünkü tek bir tedarikçiden tam ihtiyaç kadar alınamıyor; bu parçalar için "
            "birden fazla tedarikçiden parça parça sipariş gerekebilir."
        )

    # --------------------------------------------------------
    # TEDARİKÇİ KARŞILAŞTIRMASI (tek bir parça için tüm alternatifler)
    # --------------------------------------------------------
    st.markdown("---")
    st.subheader("Tedarikçi Karşılaştırması")
    st.write("Bir parça seçin, o parçanın tüm alternatif tedarikçilerdeki fiyatlarını görün.")

    secilen_mpn = st.selectbox("Parça (MPN) seçin:", konsolide_df["MPN"].tolist())

    if secilen_mpn:
        sonuc = nexar_map.get(secilen_mpn, {})
        if sonuc.get("hata"):
            st.error(f"Bu parça sorgulanırken hata oluştu: {sonuc['hata']}")

        yasam_durumu = sonuc.get("yasam_durumu_kategori", "Bilinmiyor")
        yasam_durumu_ham = sonuc.get("yasam_durumu_ham", "-")
        if yasam_durumu in ("Obsolete", "EOL"):
            st.error(f"Yaşam Döngüsü Durumu: **{yasam_durumu}** ({yasam_durumu_ham})")
        elif yasam_durumu == "NRND":
            st.warning(f"Yaşam Döngüsü Durumu: **{yasam_durumu}** ({yasam_durumu_ham})")
        elif yasam_durumu == "Aktif":
            st.success(f"Yaşam Döngüsü Durumu: **{yasam_durumu}** ({yasam_durumu_ham})")
        else:
            st.info(f"Yaşam Döngüsü Durumu: **{yasam_durumu}**")

        teklifler = sonuc.get("teklifler", [])
        if not teklifler:
            st.info("Bu parça için tedarikçi teklifi bulunamadı.")
        else:
            teklif_df = pd.DataFrame(
                teklifler,
                columns=["Tedarikçi", "Birim Fiyat", "Para Birimi", "Adet Kırılımı", "Stok", "Link"],
            )
            st.dataframe(teklif_df, width='stretch')

    # --------------------------------------------------------
    # YAPAY ZEKA ASİSTANI (GEMINI)
    # --------------------------------------------------------
    st.markdown("---")
    st.subheader("Yapay Zeka BOM Asistanı")
    st.info("Bu asistan doğrudan BOM verinizi bağlam olarak okur ve mantıksal çıkarım yapar.")

    soru = st.text_input(
        "BOM hakkında bir soru sorun:",
        placeholder="Bu projede kaç farklı 10 kΩ direnç kullanılıyor?",
    )

    if soru:
        if not GEMINI_API_KEY:
            st.error(
                "Yapay Zeka özelliğini kullanmak için `GEMINI_API_KEY` tanımlamalısınız "
                "(secrets.toml veya ortam değişkeni)."
            )
        else:
            with st.spinner("Yapay Zeka tabloyu inceliyor ve düşünüyor..."):
                try:
                    genai.configure(api_key=GEMINI_API_KEY)
                    llm_model = genai.GenerativeModel(GEMINI_MODEL_NAME)

                    bom_metni = konsolide_df.to_csv(index=False)

                    prompt = f"""
                    Sen uzman bir elektronik sistem mühendisisin. Aşağıda virgülle ayrılmış bir BOM
                    (Malzeme Listesi) verisi var. Bu tablo, {uretim_adedi} adet PCB (kart)
                    üretimi planlanarak hazırlanmıştır; "Gereken Miktar" sütunu bu üretim
                    adedine göre hesaplanmıştır. Kullanıcının sorusuna SADECE bu tablodaki
                    verilere dayanarak, mühendislik formatında, net ve doğru bir cevap ver.
                    Hesaplama gerekiyorsa ilgili sütunları topla/karşılaştır.

                    BOM VERİSİ:
                    {bom_metni}

                    KULLANICI SORUSU:
                    {soru}
                    """

                    response = llm_model.generate_content(prompt)

                    st.success(f"AI Yanıtı (Model: {GEMINI_MODEL_NAME}):")
                    st.write(response.text)

                except Exception as e:
                    st.error(f"Yapay zeka ile iletişim kurulurken bir hata oluştu: {e}")