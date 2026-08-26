import io
import os
import concurrent.futures

import streamlit as st
import pandas as pd
import requests
import google.generativeai as genai

# ============================================================
# YAPILANDIRMA & ARAYÜZ AYARLARI
# ============================================================
st.set_page_config(
    page_title="Nexus | BOM Intelligence",
    page_icon="🔌",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Arayüzü daha temiz göstermek için Streamlit varsayılan menülerini gizleyen özel CSS
st.markdown("""
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    .block-container {padding-top: 2rem;}
    .stTabs [data-baseweb="tab-list"] {gap: 24px;}
    .stTabs [data-baseweb="tab"] {height: 50px; white-space: pre-wrap; background-color: transparent; border-radius: 4px; padding-top: 10px; padding-bottom: 10px;}
    .stTabs [aria-selected="true"] {background-color: rgba(28, 131, 225, 0.1) !important;}
    </style>
""", unsafe_allow_html=True)

def secret_veya_env(anahtar: str) -> str:
    try:
        deger = st.secrets.get(anahtar)
        if deger: return deger
    except Exception: pass
    return os.environ.get(anahtar, "")

GEMINI_API_KEY = secret_veya_env("GEMINI_API_KEY")
NEXAR_CLIENT_ID = secret_veya_env("NEXAR_CLIENT_ID")
NEXAR_CLIENT_SECRET = secret_veya_env("NEXAR_CLIENT_SECRET")
GEMINI_MODEL_NAME = "gemini-1.5-pro" # Güncel model adı kullanıldı

REQUIRED_COLUMNS = ["MPN", "Manufacturer", "Description", "Qty", "RefDes"]
NEXAR_TOKEN_URL = "https://identity.nexar.com/connect/token"
NEXAR_GRAPHQL_URL = "https://api.nexar.com/graphql"
NEXAR_OTURUM = requests.Session()
NEXAR_TOPLU_SORGU_BOYUTU = 15

NEXAR_PART_ALANLARI = """
    mpn
    shortDescription
    manufacturer { name }
    specs { attribute { shortname name } displayValue }
    similarParts { name mpn octopartUrl }
    sellers {
      company { name }
      offers {
        sku inventoryLevel moq
        prices { quantity price currency }
        clickUrl
      }
    }
"""

# ============================================================
# NEXAR API İŞLEMLERİ
# ============================================================
@st.cache_data(show_spinner=False, ttl=1800)
def nexar_token_al() -> str:
    if not NEXAR_CLIENT_ID or not NEXAR_CLIENT_SECRET: return ""
    try:
        resp = NEXAR_OTURUM.post(
            NEXAR_TOKEN_URL,
            data={"grant_type": "client_credentials", "client_id": NEXAR_CLIENT_ID, "client_secret": NEXAR_CLIENT_SECRET},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        return resp.json().get("access_token", "") if resp.status_code == 200 else ""
    except: return ""

def _bos_parca_sonucu(hata=None) -> dict:
    return {"bulundu": False, "aciklama": "-", "uretici": "-", "teklifler": [], "yasam_durumu_ham": "-", "yasam_durumu_kategori": "Bilinmiyor", "alternatifler": [], "hata": hata}

def _part_json_ayristir(part: dict) -> dict:
    aciklama = part.get("shortDescription") or "-"
    uretici = (part.get("manufacturer") or {}).get("name", "-")
    
    yasam_durumu_ham = "-"
    for spec in part.get("specs", []) or []:
        if (spec.get("attribute") or {}).get("shortname") == "lifecyclestatus":
            yasam_durumu_ham = spec.get("displayValue") or "-"
            break
            
    yasam_durumu_kategori = yasam_durumu_kategorisi_belirle(yasam_durumu_ham)
    
    teklifler = []
    for satici in part.get("sellers", []) or []:
        tedarikci_adi = (satici.get("company") or {}).get("name", "Bilinmiyor")
        for teklif in satici.get("offers", []) or []:
            fiyatlar = teklif.get("prices") or []
            if not fiyatlar: continue
            en_dusuk = min(fiyatlar, key=lambda p: p.get("quantity", 0))
            teklifler.append((
                tedarikci_adi, en_dusuk.get("price"), en_dusuk.get("currency", "USD"),
                en_dusuk.get("quantity", 1), teklif.get("inventoryLevel", 0) or 0, teklif.get("clickUrl", "")
            ))
    teklifler.sort(key=lambda t: (t[1] is None, t[1]))

    alternatifler = [{"mpn": alt.get("mpn") or "-", "isim": alt.get("name") or "-", "link": alt.get("octopartUrl") or ""} for alt in part.get("similarParts", []) or []]
    
    return {"bulundu": True, "aciklama": aciklama, "uretici": uretici, "teklifler": teklifler, "yasam_durumu_ham": yasam_durumu_ham, "yasam_durumu_kategori": yasam_durumu_kategori, "alternatifler": alternatifler, "hata": None}

def yasam_durumu_kategorisi_belirle(ham_metin: str) -> str:
    if not ham_metin or ham_metin == "-": return "Bilinmiyor"
    metin = ham_metin.lower()
    if "obsolete" in metin: return "Obsolete"
    if any(x in metin for x in ["eol", "end of life", "discontinued"]): return "EOL"
    if any(x in metin for x in ["nrnd", "not recommended for new designs"]): return "NRND"
    if any(x in metin for x in ["active", "production"]): return "Aktif"
    return "Diğer"

def uygun_en_ucuz_teklifi_bul(teklifler: list, gereken_miktar: int):
    uygun = [t for t in teklifler if (t[4] or 0) >= gereken_miktar]
    if not uygun: return None
    uygun.sort(key=lambda t: (t[1] is None, t[1]))
    return uygun[0][0], uygun[0][1], uygun[0][2], uygun[0][4]

def _nexar_toplu_istek_at(mpn_grubu: tuple) -> dict:
    token = nexar_token_al()
    if not token: return {mpn: _bos_parca_sonucu("Yetkilendirme Hatası") for mpn in mpn_grubu}

    degiskenler = {f"mpn{i}": mpn for i, mpn in enumerate(mpn_grubu)}
    degisken_tanimlari = ", ".join(f"$mpn{i}: String!" for i in range(len(mpn_grubu)))
    alan_bloklari = "\n".join(f'p{i}: supSearchMpn(q: $mpn{i}, limit: 1) {{ results {{ part {{ {NEXAR_PART_ALANLARI} }} }} }}' for i in range(len(mpn_grubu)))
    sorgu = f"query TopluArama({degisken_tanimlari}) {{ {alan_bloklari} }}"

    try:
        resp = NEXAR_OTURUM.post(NEXAR_GRAPHQL_URL, json={"query": sorgu, "variables": degiskenler}, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=30)
        data = resp.json()
    except Exception as e: return {mpn: _bos_parca_sonucu(f"Bağlantı/API hatası") for mpn in mpn_grubu}

    veri, sonuclar = data.get("data", {}), {}
    for i, mpn in enumerate(mpn_grubu):
        parca_sonuclari = (veri.get(f"p{i}") or {}).get("results", [])
        sonuclar[mpn] = _part_json_ayristir(parca_sonuclari[0].get("part", {})) if parca_sonuclari else _bos_parca_sonucu(None)
    return sonuclar

@st.cache_data(show_spinner=False, ttl=3600)
def _nexar_toplu_istek_at_cache(mpn_grubu: tuple) -> dict:
    return _nexar_toplu_istek_at(mpn_grubu)

def toplu_nexar_sorgula(mpn_listesi, max_workers: int = 5) -> dict:
    sonuclar = {}
    benzersiz_mpnler = list(dict.fromkeys(mpn_listesi))
    gruplar = [tuple(benzersiz_mpnler[i:i + NEXAR_TOPLU_SORGU_BOYUTU]) for i in range(0, len(benzersiz_mpnler), NEXAR_TOPLU_SORGU_BOYUTU)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_grup = {executor.submit(_nexar_toplu_istek_at_cache, grup): grup for grup in gruplar}
        for future in concurrent.futures.as_completed(future_to_grup):
            try: sonuclar.update(future.result())
            except: pass
    return sonuclar

def risk_hesapla(nexar_sonucu: dict, gereken_miktar: int) -> tuple:
    if nexar_sonucu.get("hata"): return 50, 0, 0
    if not nexar_sonucu.get("bulundu"): return 100, 0, 0
    
    yasam = nexar_sonucu.get("yasam_durumu_kategori", "Bilinmiyor")
    teklifler = nexar_sonucu.get("teklifler", [])
    toplam_stok = sum((t[4] or 0) for t in teklifler)
    karsilama = min((toplam_stok / gereken_miktar * 100) if gereken_miktar > 0 else 0, 999)

    if yasam in ("Obsolete", "EOL"): return 95, toplam_stok, karsilama
    if not teklifler: return 90, 0, 0
    
    stoklu_tedarikci = sum(1 for t in teklifler if (t[4] or 0) > 0)
    if toplam_stok < gereken_miktar: return int(60 + (1 - (toplam_stok / gereken_miktar)) * 35), toplam_stok, karsilama
    
    risk = 35 if stoklu_tedarikci == 1 else 10
    if yasam == "NRND": risk = min(risk + 20, 89)
    return risk, toplam_stok, karsilama

def excele_donustur(df: pd.DataFrame) -> bytes:
    arabellek = io.BytesIO()
    with pd.ExcelWriter(arabellek, engine="openpyxl") as yazici:
        df.to_excel(yazici, index=False, sheet_name="BOM Analizi")
    return arabellek.getvalue()

# ============================================================
# ARAYÜZ (UI) OLUŞTURMA
# ============================================================
st.title("🔌 Nexus | BOM Zeka Platformu")
st.markdown("BOM (Bill of Materials) verilerinizi tedarik zinciri verileriyle zenginleştirin, riskleri analiz edin ve yapay zeka ile içgörüler oluşturun.")

# YAN MENÜ (Sidebar)
with st.sidebar:
    st.header("⚙️ Proje Ayarları")
    yuklenen_dosya = st.file_uploader("BOM Dosyası (CSV/XLSX)", type=["csv", "xlsx"])
    uretim_adedi = st.number_input("Hedef Üretim Adedi:", min_value=1, value=100, step=1)
    
    if not NEXAR_CLIENT_ID:
        st.warning("⚠️ Nexar API anahtarları eksik. Tedarik verisi çekilemeyecek.")
    if not GEMINI_API_KEY:
        st.info("💡 Gemini API anahtarı eksik. AI asistan devre dışı.")

# ANA EKRAN İŞLEMLERİ
if yuklenen_dosya:
    try:
        df = pd.read_csv(yuklenen_dosya) if yuklenen_dosya.name.endswith(".csv") else pd.read_excel(yuklenen_dosya)
    except Exception as e:
        st.error(f"Dosya okuma hatası: {e}")
        st.stop()

    eksik_sutunlar = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if eksik_sutunlar:
        st.error(f"Eksik Sütunlar: {', '.join(eksik_sutunlar)}. Beklenen: {', '.join(REQUIRED_COLUMNS)}.")
        st.stop()

    # Veri Konsolidasyonu
    konsolide_df = df.groupby(["MPN", "Manufacturer", "Description"], dropna=False).agg({"Qty": "sum", "RefDes": lambda x: ", ".join(map(str, x))}).reset_index()

    with st.spinner("Küresel tedarik zinciri taranıyor..."):
        nexar_map = toplu_nexar_sorgula(konsolide_df["MPN"].tolist())

        def satir_zenginlestir(row):
            mpn, birim_qty = row["MPN"], row["Qty"]
            gereken = int(birim_qty) * int(uretim_adedi)
            sonuc = nexar_map.get(mpn, _bos_parca_sonucu("Sonuç yok"))
            teklifler, risk_kriterleri = sonuc.get("teklifler", []), risk_hesapla(sonuc, gereken)
            risk, toplam_stok, karsilama_orani = risk_kriterleri

            durum = f"Hata: {sonuc['hata']}" if sonuc.get("hata") else ("Bulundu" if sonuc.get("bulundu") else "Bulunamadı")
            uygun_teklif = uygun_en_ucuz_teklifi_bul(teklifler, gereken)
            
            if uygun_teklif:
                tedarikci, fiyat, birim, stok = uygun_teklif
                fiyat_metni, maliyet_metni = f"{fiyat} {birim}", f"{fiyat * gereken:.2f} {birim}"
            else:
                tedarikci, fiyat_metni, maliyet_metni = "Kritik (Tek Kaynak Yetersiz)", "-", "-"

            return pd.Series([
                durum, sonuc.get("yasam_durumu_kategori", "Bilinmiyor"), len(teklifler),
                gereken, toplam_stok, f"%{karsilama_orani:.0f}" if teklifler else "-",
                tedarikci, fiyat_metni, maliyet_metni, len(sonuc.get("alternatifler", [])), risk
            ])

        sutunlar = ["Durum", "Yaşam Döngüsü", "Tedarikçi Sayısı", "Toplam İhtiyaç", "Küresel Stok", "Karşılama Oranı", "En Uygun Tedarikçi", "Birim Fiyat", "Toplam Maliyet", "Alternatif", "Risk Skoru"]
        konsolide_df[sutunlar] = konsolide_df.apply(satir_zenginlestir, axis=1)

    # METRİKLER (KPI Dashboard)
    toplam_usd = konsolide_df["Toplam Maliyet"].apply(lambda x: float(x.replace("USD", "").strip()) if isinstance(x, str) and "USD" in x else 0.0).sum()
    karsilanamayan = (konsolide_df["En Uygun Tedarikçi"] == "Kritik (Tek Kaynak Yetersiz)").sum()
    yuksek_riskli_sayisi = len(konsolide_df[konsolide_df["Risk Skoru"] >= 70])
    
    st.markdown("### 📊 Genel Görünüm")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Tahmini Maliyet (USD)", f"${toplam_usd:,.2f}")
    m2.metric("Toplam Benzersiz Parça", len(konsolide_df))
    m3.metric("Tedarik Edilemeyen Kalem", int(karsilanamayan), delta_color="inverse")
    m4.metric("Yüksek Riskli Parça", yuksek_riskli_sayisi, delta_color="inverse")
    st.markdown("---")

    # SEKMELER (Tabs)
    tab1, tab2, tab3, tab4 = st.tabs(["📑 BOM Analiz Tablosu", "🔍 Detaylı Parça Analizi", "🔄 Akıllı Konsolidasyon", "🤖 AI Asistan"])

    # SEKME 1: BOM ANALİZ TABLOSU
    with tab1:
        def hucre_stili(val):
            try: v = float(val)
            except: return ""
            if v >= 70: return "background-color: rgba(255, 75, 75, 0.2); color: #ff4b4b; font-weight: bold;"
            if v >= 30: return "background-color: rgba(255, 164, 33, 0.2); color: #ffa421;"
            return "background-color: rgba(33, 195, 84, 0.2); color: #21c354;"
            
        def yasam_stili(val):
            if val in ("Obsolete", "EOL"): return "background-color: rgba(255, 75, 75, 0.2); color: #ff4b4b; font-weight: bold;"
            if val == "NRND": return "background-color: rgba(255, 164, 33, 0.2); color: #ffa421;"
            if val == "Aktif": return "background-color: rgba(33, 195, 84, 0.2); color: #21c354;"
            return ""

        stil = konsolide_df.style.map(hucre_stili, subset=["Risk Skoru"]).map(yasam_stili, subset=["Yaşam Döngüsü"])
        st.dataframe(stil, use_container_width=True, height=500)
        
        st.download_button("📥 Analizi Excel Olarak İndir", data=excele_donustur(konsolide_df), file_name="nexus_bom_analizi.xlsx")

    # SEKME 2: DETAYLI PARÇA ANALİZİ
    with tab2:
        secilen_mpn = st.selectbox("Analiz Edilecek Parçayı Seçin:", konsolide_df["MPN"].tolist())
        if secilen_mpn:
            sonuc = nexar_map.get(secilen_mpn, {})
            secili_satir = konsolide_df[konsolide_df["MPN"] == secilen_mpn].iloc[0]
            
            c1, c2, c3 = st.columns(3)
            c1.info(f"**Üretici:** {secili_satir['Manufacturer']}\n\n**Açıklama:** {secili_satir['Description']}")
            c2.metric("Yaşam Döngüsü", secili_satir['Yaşam Döngüsü'])
            c3.metric("Hesaplanan Risk Skoru", secili_satir['Risk Skoru'])
            
            st.markdown("#### 🛒 Küresel Tedarikçi Teklifleri")
            teklifler = sonuc.get("teklifler", [])
            if teklifler:
                teklif_df = pd.DataFrame(teklifler, columns=["Tedarikçi", "Birim Fiyat", "Para Birimi", "Min. Sipariş (MOQ)", "Stok Adedi", "Satın Alma Linki"])
                st.dataframe(teklif_df, use_container_width=True)
            else:
                st.warning("Bu parça için aktif tedarikçi teklifi bulunamadı.")
                
            st.markdown("#### 🔄 Uyumlu Alternatifler (Çapraz Referans)")
            alternatifler = sonuc.get("alternatifler", [])
            if alternatifler:
                alt_df = pd.DataFrame(alternatifler)
                alt_df.columns = ["Alternatif MPN", "Ürün Adı", "Veri Sayfası / Link"]
                st.dataframe(alt_df, use_container_width=True)
            else:
                st.info("Bu parça için sistemde doğrudan bir alternatif kaydedilmemiş.")

    # SEKME 3: AKILLI KONSOLİDASYON
    with tab3:
        st.markdown("BOM listenizdeki aynı işlevi gören ancak farklı üreticilerden sağlanan parçaları tespit edip tek bir kalemde birleştirerek maliyeti düşürün.")
        konsolide_df["_normalize"] = konsolide_df["Description"].astype(str).str.strip().str.lower()
        konsolidasyon_adaylari = konsolide_df[konsolide_df.groupby("_normalize")["MPN"].transform("nunique") > 1]

        if konsolidasyon_adaylari.empty:
            st.success("Tebrikler! BOM listenizde optimize edilebilecek kopya/benzer parça bulunamadı.")
        else:
            for _, grup in konsolidasyon_adaylari.groupby("_normalize"):
                with st.expander(f"📌 {grup.iloc[0]['Description']} ({len(grup)} Varyasyon)"):
                    grup_sirali = grup.copy()
                    grup_sirali["_fiyat_sayi"] = grup_sirali["Birim Fiyat"].apply(lambda x: float(str(x).split()[0]) if str(x).split()[0].replace('.','',1).isdigit() else float('inf'))
                    grup_sirali = grup_sirali.sort_values(by=["Risk Skoru", "_fiyat_sayi"])
                    
                    onerilen = grup_sirali.iloc[0]
                    st.dataframe(grup[["MPN", "Manufacturer", "Birim Fiyat", "Küresel Stok", "Yaşam Döngüsü", "Risk Skoru"]], use_container_width=True)
                    st.success(f"**Sistem Önerisi:** Tüm alımları **{onerilen['MPN']}** üzerinden yapın. (Risk: {onerilen['Risk Skoru']}, Fiyat: {onerilen['Birim Fiyat']})")

    # SEKME 4: YAPAY ZEKA ASİSTANI
    with tab4:
        st.markdown("BOM listenizdeki verilerle ilgili serbest formatta sorular sorun. Örn: *'Hangi parçaların EOL riski var?'* veya *'En pahalı 3 komponent hangisi?'*")
        soru = st.text_input("💬 Asistana Sorun:", placeholder="Bu projede toplam kaç adet kondansatör kullanılıyor?")
        
        if soru:
            if not GEMINI_API_KEY:
                st.error("🔑 AI özelliklerini kullanmak için ayarlardan Gemini API Key girmelisiniz.")
            else:
                with st.spinner("🤖 Nexus AI analiz ediyor..."):
                    try:
                        genai.configure(api_key=GEMINI_API_KEY)
                        llm = genai.GenerativeModel(GEMINI_MODEL_NAME)
                        prompt = f"""Sen donanım tedarik zinciri ve elektronik mühendisliği uzmanı bir AI asistansın.
                        Kullanıcının sorusuna AŞAĞIDAKİ BOM TABLOSUNA dayanarak profesyonel, net ve analitik bir cevap ver.
                        
                        BOM VERİSİ (CSV):
                        {konsolide_df.drop(columns=["_normalize"], errors="ignore").to_csv(index=False)}
                        
                        SORU: {soru}"""
                        yanit = llm.generate_content(prompt)
                        st.info(yanit.text)
                    except Exception as e:
                        st.error(f"Yapay zeka servisiyle iletişim kurulamadı: {e}")