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


GEMINI_MODEL_NAME = "gemini-3.6-flash"

REQUIRED_COLUMNS = ["MPN", "Manufacturer", "Description", "Qty", "RefDes"]



# Ücretsiz Client ID / Client Secret için: https://nexar.com/


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
                # NOT: "scope" parametresi kasıtlı olarak gönderilmiyor. eklendiğğinde invslid_scope hatası verdi.
                
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
    """Verilen MPN için Nexar'dan tüm tedarikçi tekliflerini ve temel parça
    bilgisini çeker.

    Döndürdüğü sözlük:
    {
        "bulundu": bool,
        "aciklama": str,
        "uretici": str,
        "teklifler": [(tedarikci, fiyat, para_birimi, adet_kirilimi, stok, link), ...]  # fiyata göre sıralı
        "hata": str veya None
    }
    """
    bos_sonuc = {"bulundu": False, "aciklama": "-", "uretici": "-", "teklifler": [], "hata": None}

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

    teklifler.sort(key=lambda t: (t[1] is None, t[1])) 

    return {
        "bulundu": True,
        "aciklama": aciklama,
        "uretici": uretici,
        "teklifler": teklifler,
        "hata": None,
    }


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
                    "teklifler": [], "hata": f"Beklenmeyen hata: {e}",
                }

    return sonuclar


def risk_hesapla(nexar_sonucu: dict) -> int:
    """ Nexar verisine dayalı risk skoru:
    - Parça hiç bulunamadıysa veya API hatası varsa -> yüksek risk
    - Bulundu ama hiçbir tedarikçide stok yoksa -> orta-yüksek risk
    - Az sayıda tedarikçide (1) stok varsa -> orta risk
    - Birden fazla tedarikçide stok varsa -> düşük risk
    """
    if nexar_sonucu.get("hata"):
        return 50  # veri alınamadı, temkinli orta risk
    if not nexar_sonucu.get("bulundu"):
        return 100  # hiçbir tedarikçide bulunamadı -> muhtemelen obsolete/nadir

    teklifler = nexar_sonucu.get("teklifler", [])
    if not teklifler:
        return 90  # kayıt var ama satılabilir teklif yok

    stoklu_tedarikci_sayisi = sum(1 for t in teklifler if (t[4] or 0) > 0)
    if stoklu_tedarikci_sayisi == 0:
        return 80  # teklif var ama hepsi stoksuz
    if stoklu_tedarikci_sayisi == 1:
        return 40  # tek kaynağa bağımlı, riskli
    return 10  # birden fazla stoklu tedarikçi -> güvenli



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

    # dropna=False: MPN/Manufacturer/Description içinde boş hücre olan
    # satırların sessizce silinmesini önler.
    konsolide_df = (
        df.groupby(["MPN", "Manufacturer", "Description"], dropna=False)
        .agg({"Qty": "sum", "RefDes": lambda x: ", ".join(map(str, x))})
        .reset_index()
    )

    st.subheader("Gerçek Zamanlı Komponent Analizi (Nexar üzerinden)")

    with st.spinner("Tüm tedarikçilerden fiyat/stok verisi çekiliyor..."):
        nexar_map = toplu_nexar_sorgula(konsolide_df["MPN"].tolist())

        def satir_zenginlestir(mpn):
            sonuc = nexar_map.get(mpn, {"bulundu": False, "teklifler": [], "hata": "Sonuç yok"})
            teklifler = sonuc.get("teklifler", [])
            risk = risk_hesapla(sonuc)

            if sonuc.get("hata"):
                durum = f"Hata: {sonuc['hata']}"
            elif not sonuc.get("bulundu"):
                durum = "Bulunamadı"
            else:
                durum = "Bulundu"

            if teklifler:
                en_ucuz = teklifler[0]
                en_ucuz_fiyat = f"{en_ucuz[1]} {en_ucuz[2]}" if en_ucuz[1] is not None else "-"
                en_ucuz_tedarikci = en_ucuz[0]
            else:
                en_ucuz_fiyat = "-"
                en_ucuz_tedarikci = "-"

            return pd.Series([durum, en_ucuz_fiyat, en_ucuz_tedarikci, len(teklifler), risk])

        konsolide_df[
            ["Durum", "En Ucuz Fiyat", "En Ucuz Tedarikçi", "Tedarikçi Sayısı", "Risk Skoru"]
        ] = konsolide_df["MPN"].apply(satir_zenginlestir)

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

    st.dataframe(stil, width='stretch')

    yuksek_riskli = konsolide_df[konsolide_df["Risk Skoru"] >= 70]
    if not yuksek_riskli.empty:
        st.error(f"{len(yuksek_riskli)} parça yüksek risk taşıyor (bulunamadı / stoksuz).")

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