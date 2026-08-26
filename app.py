import io
import os
import concurrent.futures

import streamlit as st
import pandas as pd
import requests
import google.generativeai as genai



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


GEMINI_MODEL_NAME = "gemini-3.6-flash"

REQUIRED_COLUMNS = ["MPN", "Manufacturer", "Description", "Qty", "RefDes"]



NEXAR_TOKEN_URL = "https://identity.nexar.com/connect/token"
NEXAR_GRAPHQL_URL = "https://api.nexar.com/graphql"

NEXAR_OTURUM = requests.Session()

# Bir parça sorgusunda istediğimiz alanlar (hem tekli hem toplu sorguda
# kullanılan ortak GraphQL alan bloğu).
NEXAR_PART_ALANLARI = """
    mpn
    shortDescription
    manufacturer { name }
    specs {
      attribute { shortname name }
      displayValue
    }
    similarParts {
      name
      mpn
      octopartUrl
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
"""


NEXAR_TOPLU_SORGU_BOYUTU = 15


@st.cache_data(show_spinner=False, ttl=60 * 30)  # token 30 dakika cache'lensin
def nexar_token_al() -> str:
    """Nexar OAuth2 (client_credentials) akışıyla erişim token'ı alır."""
    if not NEXAR_CLIENT_ID or not NEXAR_CLIENT_SECRET:
        return ""
    try:
        resp = NEXAR_OTURUM.post(
            NEXAR_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": NEXAR_CLIENT_ID,
                "client_secret": NEXAR_CLIENT_SECRET,
                
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


def _bos_parca_sonucu(hata=None) -> dict:
    """Her hata/başarısızlık durumunda aynı 8 alanlı şablonu döndürmek için
    tek bir yerden üretiyoruz -- tutarlılık ve tekrar etmemek için."""
    return {
        "bulundu": False, "aciklama": "-", "uretici": "-", "teklifler": [],
        "yasam_durumu_ham": "-", "yasam_durumu_kategori": "Bilinmiyor",
        "alternatifler": [], "hata": hata,
    }


def _part_json_ayristir(part: dict) -> dict:
    """Nexar'dan dönen ham 'part' JSON nesnesini bizim standart sözlük
    yapımıza çevirir. Hem tekli hem toplu sorgu bu fonksiyonu kullanıyor,
    böylece ayrıştırma mantığını iki yerde tekrar yazmıyoruz."""
    aciklama = part.get("shortDescription") or "-"
    uretici = (part.get("manufacturer") or {}).get("name", "-")

    # Yaşam döngüsü durumunu specs listesinden buluyoruz.
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

   
    # Nexar'ın "similarParts" alanı, aynı işlevi gören farklı MPN'leri döner.
    alternatifler = []
    for alt in part.get("similarParts", []) or []:
        alternatifler.append({
            "mpn": alt.get("mpn") or "-",
            "isim": alt.get("name") or "-",
            "link": alt.get("octopartUrl") or "",
        })

    return {
        "bulundu": True,
        "aciklama": aciklama,
        "uretici": uretici,
        "teklifler": teklifler,
        "yasam_durumu_ham": yasam_durumu_ham,
        "yasam_durumu_kategori": yasam_durumu_kategori,
        "alternatifler": alternatifler,
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
    uygun_teklifler.sort(key=lambda t: (t[1] is None, t[1]))
    en_uygun = uygun_teklifler[0]
    return (en_uygun[0], en_uygun[1], en_uygun[2], en_uygun[4])


def _nexar_toplu_istek_at(mpn_grubu: tuple) -> dict:
    """HIZ İYİLEŞTİRMESİ #2'nin kalbi: Birden fazla MPN'i TEK BİR GraphQL
    isteğinde sorgular. Bunu GraphQL'in 'alias' özelliğiyle yapıyoruz --
    aynı sorgu içinde aynı alanı (supSearchMpn) farklı isimlerle (p0, p1, p2...)
    birden çok kez, her seferinde farklı bir MPN değişkeniyle çağırabiliyoruz.

    Örnek üretilen sorgu (3 parça için):
        query TopluArama($mpn0: String!, $mpn1: String!, $mpn2: String!) {
          p0: supSearchMpn(q: $mpn0, limit: 1) { results { part { ... } } }
          p1: supSearchMpn(q: $mpn1, limit: 1) { results { part { ... } } }
          p2: supSearchMpn(q: $mpn2, limit: 1) { results { part { ... } } }
        }

    Bu sayede 15 parça için 15 ayrı HTTP isteği yerine TEK istek atılıyor.
    """
    token = nexar_token_al()
    if not token:
        return {mpn: _bos_parca_sonucu("Nexar anahtarı eksik/geçersiz") for mpn in mpn_grubu}

    degisken_tanimlari = ", ".join(f"$mpn{i}: String!" for i in range(len(mpn_grubu)))
    alan_bloklari = "\n".join(
        f'p{i}: supSearchMpn(q: $mpn{i}, limit: 1) {{ results {{ part {{ {NEXAR_PART_ALANLARI} }} }} }}'
        for i in range(len(mpn_grubu))
    )
    sorgu = f"query TopluArama({degisken_tanimlari}) {{ {alan_bloklari} }}"
    degiskenler = {f"mpn{i}": mpn for i, mpn in enumerate(mpn_grubu)}

    try:
        resp = NEXAR_OTURUM.post(
            NEXAR_GRAPHQL_URL,
            json={"query": sorgu, "variables": degiskenler},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        return {mpn: _bos_parca_sonucu(f"Bağlantı hatası: {e}") for mpn in mpn_grubu}

    if resp.status_code != 200:
        return {mpn: _bos_parca_sonucu(f"API hatası ({resp.status_code})") for mpn in mpn_grubu}

    try:
        data = resp.json()
    except ValueError:
        return {mpn: _bos_parca_sonucu("Geçersiz yanıt (JSON değil)") for mpn in mpn_grubu}

    veri = data.get("data") or {}
    genel_hata = None
    if data.get("errors") and not veri:
        genel_hata = str(data["errors"])[:200]

    sonuclar = {}
    for i, mpn in enumerate(mpn_grubu):
        if genel_hata:
            sonuclar[mpn] = _bos_parca_sonucu(genel_hata)
            continue
        alan_adi = f"p{i}"
        parca_sonuclari = (veri.get(alan_adi) or {}).get("results", []) or []
        if not parca_sonuclari:
            sonuclar[mpn] = _bos_parca_sonucu(None)  # bulunamadı ama hata değil
            continue
        part = parca_sonuclari[0].get("part", {}) or {}
        sonuclar[mpn] = _part_json_ayristir(part)

    return sonuclar


@st.cache_data(show_spinner=False, ttl=60 * 60)
def _nexar_toplu_istek_at_cache(mpn_grubu: tuple) -> dict:
    """_nexar_toplu_istek_at'in cache'lenmiş hali. Streamlit her etkileşimde
    tüm scripti baştan çalıştırdığı için, aynı parça grubu tekrar istenirse
    (örn. AI asistana soru sorulduğunda) 1 saat boyunca tekrar API'ye
    gitmiyor, hafızadan dönüyor."""
    return _nexar_toplu_istek_at(mpn_grubu)


def toplu_nexar_sorgula(mpn_listesi, max_workers: int = 5) -> dict:
    """Birden çok MPN için Nexar verilerini, gruplar halinde VE paralel çeker.

    İki katmanlı hız stratejisi:
    1. Parçaları NEXAR_TOPLU_SORGU_BOYUTU'luk gruplara böl (örn. 50 parça -> 4 grup)
    2. Bu grupları aynı anda (paralel) sorgula (ThreadPoolExecutor)

    Böylece 50 parçalık bir BOM, tek tek 50 istek yerine yaklaşık 4 istekle,
    üstelik bu 4 istek de birbirini beklemeden aynı anda atılarak sorgulanır.
    """
    sonuclar = {}
    benzersiz_mpnler = list(dict.fromkeys(mpn_listesi))  # sırayı koruyarak tekilleştir

    gruplar = [
        tuple(benzersiz_mpnler[i:i + NEXAR_TOPLU_SORGU_BOYUTU])
        for i in range(0, len(benzersiz_mpnler), NEXAR_TOPLU_SORGU_BOYUTU)
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_grup = {executor.submit(_nexar_toplu_istek_at_cache, grup): grup for grup in gruplar}
        for future in concurrent.futures.as_completed(future_to_grup):
            grup = future_to_grup[future]
            try:
                sonuclar.update(future.result())
            except Exception as e:
                import traceback
                print(f"[Nexar Toplu Hata] grup={grup}: {e}")
                traceback.print_exc()
                for mpn in grup:
                    sonuclar[mpn] = _bos_parca_sonucu(f"Beklenmeyen hata: {e}")

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

    if yasam_kategori in ("Obsolete", "EOL"):
        teklifler = nexar_sonucu.get("teklifler", [])
        toplam_stok = sum((t[4] or 0) for t in teklifler) if teklifler else 0
        karsilama_orani = (toplam_stok / gereken_miktar * 100) if gereken_miktar > 0 else 0
        return 95, toplam_stok, min(karsilama_orani, 999)

    teklifler = nexar_sonucu.get("teklifler", [])
    if not teklifler:
        return 90, 0, 0  # kayıt var ama satılabilir teklif yok

    toplam_stok = sum((t[4] or 0) for t in teklifler)
    stoklu_tedarikci_sayisi = sum(1 for t in teklifler if (t[4] or 0) > 0)

    karsilama_orani = (toplam_stok / gereken_miktar * 100) if gereken_miktar > 0 else 100
    karsilama_orani = min(karsilama_orani, 999)

    if toplam_stok == 0:
        return 95, toplam_stok, 0

    if toplam_stok < gereken_miktar:
        eksik_oran = 1 - (toplam_stok / gereken_miktar)
        risk = int(60 + eksik_oran * 35)
        return risk, toplam_stok, karsilama_orani

    if stoklu_tedarikci_sayisi == 1:
        risk = 35
    else:
        risk = 10

    if yasam_kategori == "NRND":
        risk = min(risk + 20, 89)

    return risk, toplam_stok, karsilama_orani


def excele_donustur(df: pd.DataFrame) -> bytes:
    """REVİZYON 3: Verilen tabloyu, indirilebilir bir .xlsx dosyasının ham
    baytlarına çevirir. Diske geçici dosya yazmak yerine bellek üzerinde
    (io.BytesIO) çalışıyoruz -- Streamlit'in indirme butonu için daha
    temiz ve dosya sistemi izinlerinden bağımsız bir yöntem."""
    arabellek = io.BytesIO()
    with pd.ExcelWriter(arabellek, engine="openpyxl") as yazici:
        df.to_excel(yazici, index=False, sheet_name="BOM Analizi")
    return arabellek.getvalue()


# ARAYÜZ VE ANALİZ

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

            sonuc = nexar_map.get(mpn, _bos_parca_sonucu("Sonuç yok"))
            teklifler = sonuc.get("teklifler", [])
            risk, toplam_stok, karsilama_orani = risk_hesapla(sonuc, gereken_miktar)

            if sonuc.get("hata"):
                durum = f"Hata: {sonuc['hata']}"
            elif not sonuc.get("bulundu"):
                durum = "Bulunamadı"
            else:
                durum = "Bulundu"

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
            alternatif_sayisi = len(sonuc.get("alternatifler", []))

            return pd.Series([
                durum, yasam_durumu, len(teklifler),
                gereken_miktar, toplam_stok, karsilama_metni,
                uygun_tedarikci, uygun_fiyat_metni, uygun_toplam_maliyet,
                alternatif_sayisi, risk,
            ])

        konsolide_df[
            [
                "Durum", "Yaşam Döngüsü", "Tedarikçi Sayısı",
                "Gereken Miktar", "Toplam Stok", "Stok Karşılama",
                "Uygun En Ucuz Tedarikçi", "Birim Fiyat", "Toplam Maliyet",
                "Alternatif Sayısı", "Risk Skoru",
            ]
        ] = konsolide_df.apply(satir_zenginlestir, axis=1)

    
    def risk_renklendir(val):
        try:
            v = float(val)
        except (TypeError, ValueError):
            return ""
        if v >= 70:
            return "background-color: #ffcccc; color: black"
        if v >= 30:
            return "background-color: #fff3cd; color: black"
        return "background-color: #d4edda; color: black"

    def yasam_durumu_renklendir(val):
        if val in ("Obsolete", "EOL"):
            return "background-color: #ffcccc; color: black"
        if val == "NRND":
            return "background-color: #fff3cd; color: black"
        if val == "Aktif":
            return "background-color: #d4edda; color: black"
        return ""

    def uygun_tedarikci_renklendir(val):
        if val == "Tek kaynak yetersiz":
            return "background-color: #ffcccc"
        return ""

    try:
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

    # REVİZYON 3: Düzenlenmiş BOM'u Excel olarak indirme
    
    st.download_button(
        label="Düzenlenmiş BOM'u Excel olarak indir",
        data=excele_donustur(konsolide_df),
        file_name="bom_analiz_sonucu.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    
    # REVİZYON 1: HER PARÇA İÇİN ALTERNATİFLER
    
    st.markdown("---")
    st.subheader("Parça Alternatifleri")
    st.write(
        "Nexar'ın veritabanına göre, seçtiğiniz parçayla aynı işlevi görebilecek "
        "benzer spesifikasyonlu alternatif parçalar."
    )

    alternatif_icin_mpn = st.selectbox(
        "Alternatiflerini görmek istediğiniz parçayı seçin:",
        konsolide_df["MPN"].tolist(),
        key="alternatif_secimi",
    )

    if alternatif_icin_mpn:
        secili_sonuc = nexar_map.get(alternatif_icin_mpn, {})
        alternatifler = secili_sonuc.get("alternatifler", [])
        secili_satir = konsolide_df[konsolide_df["MPN"] == alternatif_icin_mpn].iloc[0]

        st.caption(
            f"Seçili parça: **{alternatif_icin_mpn}** — {secili_satir['Description']} "
            f"| Fiyat: {secili_satir['Birim Fiyat']} | Yaşam Döngüsü: {secili_satir['Yaşam Döngüsü']}"
        )

        if not alternatifler:
            st.info(
                "Bu parça için Nexar veritabanında kayıtlı bir alternatif bulunamadı. "
                "(Not: Alternatif verisi her parça için mevcut olmayabilir.)"
            )
        else:
            alternatif_df = pd.DataFrame(alternatifler)
            alternatif_df.columns = ["Alternatif MPN", "Ürün Adı", "Octopart Linki"]
            st.dataframe(alternatif_df, width='stretch')
            st.caption(
                "Not: Bu alternatiflerin kendi fiyat/stok bilgisini görmek isterseniz, "
                "MPN'lerini kopyalayıp yeni bir BOM satırı olarak ayrıca sorgulayabilirsiniz."
            )

   
    # REVİZYON 2: AYNI İŞLEVİ GÖREN FARKLI MARKA PARÇALARI BİRLEŞTİRME ÖNERİSİ
   
    st.markdown("---")
    st.subheader("Konsolidasyon Önerileri (Aynı İşlevi Gören Farklı Marka Parçaları)")
    st.write(
        "BOM'unuzda, açıklaması (Description) aynı olan ama farklı üretici/MPN'e "
        "sahip parça çiftleri, muhtemelen aynı işlevi görüyor ama farklı "
        "tedarikçilerden alınıyor. Bu parçaları TEK bir markada birleştirerek "
        "hem sipariş karmaşıklığını azaltabilir hem de -- fiyat/stok/yaşam "
        "döngüsü karşılaştırmasına göre -- daha güvenli/ucuz bir seçenek "
        "bulabilirsiniz."
    )

    konsolide_df["_normalize_aciklama"] = (
        konsolide_df["Description"].astype(str).str.strip().str.lower()
    )
    grup_sayaclari = konsolide_df.groupby("_normalize_aciklama")["MPN"].transform("nunique")
    konsolidasyon_adaylari = konsolide_df[grup_sayaclari > 1]

    if konsolidasyon_adaylari.empty:
        st.info(
            "Şu an BOM'unuzda, aynı açıklamaya sahip ama farklı MPN'li bir parça "
            "çifti bulunamadı -- konsolidasyon önerecek bir durum yok."
        )
    else:
        for aciklama_key, grup in konsolidasyon_adaylari.groupby("_normalize_aciklama"):
            with st.expander(f"'{grup.iloc[0]['Description']}' için {len(grup)} farklı seçenek bulundu"):
                karsilastirma_df = grup[
                    [
                        "MPN", "Manufacturer", "Birim Fiyat", "Toplam Stok",
                        "Yaşam Döngüsü", "Risk Skoru",
                    ]
                ].copy()
                st.dataframe(karsilastirma_df, width='stretch')

             
                def _fiyat_sayiya_cevir(deger):
                    try:
                        return float(str(deger).split()[0])
                    except (ValueError, IndexError):
                        return float("inf")

                grup_siralanmis = grup.copy()
                grup_siralanmis["_fiyat_sayi"] = grup_siralanmis["Birim Fiyat"].apply(_fiyat_sayiya_cevir)
                grup_siralanmis = grup_siralanmis.sort_values(by=["Risk Skoru", "_fiyat_sayi"])
                onerilen = grup_siralanmis.iloc[0]
                digerleri = grup_siralanmis.iloc[1:]

                toplam_birlesik_miktar = int(grup["Qty"].sum())
                st.success(
                    f"**Öneri:** Bu {len(grup)} parçayı **{onerilen['MPN']}** "
                    f"({onerilen['Manufacturer']}) üzerinde birleştirin. "
                    f"Gerekçe: Risk Skoru {onerilen['Risk Skoru']} (en düşük), "
                    f"Fiyat {onerilen['Birim Fiyat']}, Yaşam Döngüsü: {onerilen['Yaşam Döngüsü']}. "
                    f"Birleştirilirse tek bir MPN için toplam {toplam_birlesik_miktar} adetlik "
                    "tek bir sipariş kalemi oluşur."
                )
                if not digerleri.empty:
                    vazgecilen_mpnler = ", ".join(digerleri["MPN"].tolist())
                    st.caption(f"Yerine geçecek parçalar: {vazgecilen_mpnler}")

    konsolide_df = konsolide_df.drop(columns=["_normalize_aciklama"])

    
    # TEDARİKÇİ KARŞILAŞTIRMASI (tek bir parça için tüm alternatifler)
   
    st.markdown("---")
    st.subheader("Tedarikçi Karşılaştırması")
    st.write("Bir parça seçin, o parçanın tüm alternatif tedarikçilerdeki fiyatlarını görün.")

    secilen_mpn = st.selectbox("Parça (MPN) seçin:", konsolide_df["MPN"].tolist(), key="tedarikci_secimi")

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

    
    # YAPAY ZEKA ASİSTANI (GEMINI)
    
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