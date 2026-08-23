import os
import time
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
#   MOUSER_API_KEY = "..."
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
MOUSER_API_KEY = secret_veya_env("MOUSER_API_KEY")

# Gemini model adı: Google zaman zaman model isimlerini değiştirip eskilerini
# emekliye ayırıyor. Buraya yazacağınız ismi Google AI Studio / API
# dokümantasyonundan güncel olarak teyit edin (genai.list_models() ile de
# hesabınızın erişebildiği modelleri görebilirsiniz).
GEMINI_MODEL_NAME = "gemini-3.6-flash"

REQUIRED_COLUMNS = ["MPN", "Manufacturer", "Description", "Qty", "RefDes"]

# ============================================================
# GERÇEK KOMPONENT API FONKSİYONU (Mouser)
# ============================================================
@st.cache_data(show_spinner=False, ttl=60 * 60)  # 1 saat cache: aynı MPN'i tekrar tekrar sorgulamaz
def gercek_api_ile_sorgula(mpn: str) -> tuple:
    """Tek bir MPN için Mouser API'sinden durum bilgisi çeker.
    Sonucu tuple döner (Streamlit cache'i pandas Series ile bazen sorun çıkarabildiği için)."""
    if not MOUSER_API_KEY:
        return ("Bilinmiyor (API Key Eksik)", "-", "-", 0)

    if not isinstance(mpn, str) or not mpn.strip():
        return ("Geçersiz MPN", "-", "-", 100)

    url = "https://api.mouser.com/api/v1/search/partnumber"
    headers = {"Content-Type": "application/json"}
    payload = {
        "SearchByPartRequest": {
            "mouserPartNumber": mpn,
            # NOT: Mouser API dokümantasyonundaki örnekte bu alan "string" olarak
            # görünür ama bu sadece bir yer tutucudur, gerçek bir değer DEĞİLDİR.
            # Geçerli bir değer vermiyorsanız boş bırakın.
            "partSearchOptions": "",
        }
    }

    try:
        response = requests.post(
            f"{url}?apiKey={MOUSER_API_KEY}",
            json=payload,
            headers=headers,
            timeout=10,
        )
    except requests.exceptions.Timeout:
        return ("Zaman Aşımı", "-", "-", 0)
    except requests.exceptions.RequestException:
        return ("Bağlantı Hatası", "-", "-", 0)

    if response.status_code != 200:
        return (f"API Hatası ({response.status_code})", "-", "-", 0)

    try:
        data = response.json()
    except ValueError:
        return ("Geçersiz Yanıt (JSON değil)", "-", "-", 0)

    parcalar = data.get("SearchResults", {}).get("Parts", []) or []

    # NOT: Mouser'ın /search/partnumber endpoint'i öncelikle Mouser'ın kendi
    # parça numarasına göre eşleşir. Elinizdeki üretici parça numarası (MPN)
    # her zaman birebir eşleşmeyebilir; sonuç boş dönerse /search/keyword
    # endpoint'ini de denemeniz gerekebilir.
    if not parcalar:
        return ("Bulunamadı", "-", "-", 100)

    ilk_sonuc = parcalar[0]
    durum = ilk_sonuc.get("LifecycleStatus") or "Active"
    rohs = ilk_sonuc.get("ROHSStatus", "Bilinmiyor")

    # Kademeli risk skoru
    durum_lower = durum.lower()
    if durum_lower in ("obsolete", "end of life", "eol"):
        risk_skoru = 95
    elif durum_lower in ("nrnd",):  # Not Recommended for New Designs
        risk_skoru = 70
    elif durum_lower in ("not for new designs",):
        risk_skoru = 60
    elif durum_lower in ("active",):
        risk_skoru = 10
    else:
        risk_skoru = 30  # bilinmeyen/tanımsız statü -> temkinli orta risk

    return (durum, rohs, "Mouser'da Bulundu", risk_skoru)


def toplu_sorgula(mpn_listesi, max_workers: int = 5):
    """Birden çok MPN'i paralel (ama sınırlı sayıda işçiyle) sorgular.
    Mouser'ın rate limit'ini zorlamamak için max_workers'ı düşük tutun."""
    sonuclar = {}
    benzersiz_mpnler = list(dict.fromkeys(mpn_listesi))  # sırayı koruyarak tekilleştir

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_mpn = {
            executor.submit(gercek_api_ile_sorgula, mpn): mpn for mpn in benzersiz_mpnler
        }
        for future in concurrent.futures.as_completed(future_to_mpn):
            mpn = future_to_mpn[future]
            try:
                sonuclar[mpn] = future.result()
            except Exception:
                sonuclar[mpn] = ("Beklenmeyen Hata", "-", "-", 0)

    return sonuclar


# ============================================================
# ARAYÜZ VE ANALİZ
# ============================================================
st.title("BOM (Bill of Materials) Analiz Aracı")
st.write(
    "Bu uygulama, gerçek API'ler ve LLM (Yapay Zeka) kullanarak BOM listenizi "
    "analiz eder. Bir yapay zeka modeli olduğu için hata yapabilir."
)
st.markdown("---")

if not MOUSER_API_KEY:
    st.warning(
        "Mouser API anahtarı bulunamadı. `secrets.toml` dosyasına veya ortam "
        "değişkenlerine `MOUSER_API_KEY` eklemeden gerçek tedarik verisi çekilemez."
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

    st.subheader("Gerçek Zamanlı Komponent Analizi")

    with st.spinner("İnternet üzerinden komponent verileri sorgulanıyor..."):
        sonuc_map = toplu_sorgula(konsolide_df["MPN"].tolist())
        sonuc_kolonlari = konsolide_df["MPN"].map(sonuc_map)
        konsolide_df[["Tedarik Durumu", "RoHS Durumu", "Bulunabilirlik", "Risk Skoru"]] = pd.DataFrame(
            sonuc_kolonlari.tolist(), index=konsolide_df.index
        )

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

    try:
        # Pandas >= 2.1: Styler.map, eski sürümlerde: Styler.applymap
        stil = konsolide_df.style.map(risk_renklendir, subset=["Risk Skoru"])
    except AttributeError:
        stil = konsolide_df.style.applymap(risk_renklendir, subset=["Risk Skoru"])

    st.dataframe(stil, use_container_width=True)

    yuksek_riskli = konsolide_df[konsolide_df["Risk Skoru"] >= 70]
    if not yuksek_riskli.empty:
        st.error(f"{len(yuksek_riskli)} parça yüksek risk taşıyor (obsolete / bulunamadı / NRND).")

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
                    (Malzeme Listesi) verisi var. Kullanıcının sorusuna SADECE bu tablodaki verilere
                    dayanarak, mühendislik formatında, net ve doğru bir cevap ver.
                    Hesaplama gerekiyorsa Qty (Adet) sütunlarını topla.

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