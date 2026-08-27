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
    page_title="CircuitBOM | BOM Intelligence",
    page_icon="bom_analiz_simge.ico",
    layout="wide",
    initial_sidebar_state="expanded"
)

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

# API Anahtarları
GEMINI_API_KEY = secret_veya_env("GEMINI_API_KEY")
# Geliştirme 1: Model adı istendiği gibi güncellendi
GEMINI_MODEL_NAME = "gemini-3.6-flash" 

REQUIRED_COLUMNS = ["MPN", "Manufacturer", "Description", "Qty", "RefDes"]

# Ekom Trial API Endpoint'i (Görselden alınmıştır)
EKOM_API_URL = "https://developer-ekom.azurewebsites.net/api/v1/products/KeywordSearch"
EKOM_OTURUM = requests.Session()
TOPLU_SORGU_WORKER_SAYISI = 5

# ============================================================
# EKOM API İŞLEMLERİ (YENİ)
# ============================================================
def _bos_parca_sonucu(hata=None) -> dict:
    return {
        "bulundu": False, "aciklama": "-", "uretici": "-", 
        "teklifler": [], "yasam_durumu_ham": "-", 
        "yasam_durumu_kategori": "Bilinmiyor", "alternatifler": [], "hata": hata
    }

def _ekom_json_ayristir(data: dict) -> dict:
    """Ekom API'den (DigiKey tabanlı) dönen yanıtı standartlaştırır."""
    # API'nin yanıt şemasına göre esnek veri çekme (Products, Results vb. anahtarları dener)
    urunler = data.get("Products") or data.get("Results") or data.get("products") or []
    if not urunler and isinstance(data, list):
        urunler = data

    if not urunler:
        return _bos_parca_sonucu(None)

    ilk_urun = urunler[0]
    
    # Temel Bilgiler
    aciklama = ilk_urun.get("ProductDescription") or ilk_urun.get("Description") or "-"
    uretici = (ilk_urun.get("Manufacturer") or {}).get("Name") or ilk_urun.get("ManufacturerName") or "-"
    
    # Yaşam Döngüsü (Product Status)
    statu_obj = ilk_urun.get("ProductStatus") or ilk_urun.get("Status") or "Bilinmiyor"
    yasam_durumu_ham = statu_obj.get("Status") if isinstance(statu_obj, dict) else statu_obj
    yasam_kategori = yasam_durumu_kategorisi_belirle(str(yasam_durumu_ham))

    # Fiyat ve Stok
    toplam_stok = ilk_urun.get("QuantityAvailable") or 0
    teklifler = []
    fiyat_listesi = ilk_urun.get("StandardPricing") or ilk_urun.get("Pricing") or []
    
    if fiyat_listesi:
        for f in fiyat_listesi:
            qty = f.get("Quantity") or f.get("Break") or 1
            price = f.get("UnitPrice") or f.get("Price") or 0.0
            teklifler.append(("Ekom/DigiKey", price, "USD", qty, toplam_stok, "-"))
    elif ilk_urun.get("UnitPrice"):
        teklifler.append(("Ekom/DigiKey", ilk_urun.get("UnitPrice"), "USD", 1, toplam_stok, "-"))
    
    teklifler.sort(key=lambda t: (t[1] is None, t[1]))

    # Alternatifler
    alternatifler_ham = ilk_urun.get("Alternates") or ilk_urun.get("SimilarParts") or []
    alternatifler = []
    for alt in alternatifler_ham:
        alt_statu = alt.get("ProductStatus") or {}
        alt_yasam_ham = alt_statu.get("Status") if isinstance(alt_statu, dict) else str(alt_statu)
        
        alternatifler.append({
            "mpn": alt.get("ManufacturerProductNumber") or alt.get("MPN") or "-",
            "isim": alt.get("ProductDescription") or alt.get("Description") or "-",
            "link": alt.get("ProductUrl") or "-",
            "stok": alt.get("QuantityAvailable") or 0,
            "fiyat": alt.get("UnitPrice") or 0.0,
            "yasam": yasam_durumu_kategorisi_belirle(alt_yasam_ham)
        })

    return {
        "bulundu": True, "aciklama": aciklama, "uretici": uretici,
        "teklifler": teklifler, "yasam_durumu_ham": yasam_durumu_ham,
        "yasam_durumu_kategori": yasam_kategori, "alternatifler": alternatifler, "hata": None
    }

def ekom_istek_at(mpn: str) -> dict:
    """Tek bir MPN için Ekom API'ye POST isteği atar (Geliştirme 2, 3, 4, 5)."""
    payload = {
        "keywords": mpn,
        "limit": 5,
        "offset": 0
    }
    try:
        resp = EKOM_OTURUM.post(EKOM_API_URL, json=payload, timeout=15)
        
        # Güvenli JSON ve Hata Yakalama (Geliştirme 4)
        if resp.status_code != 200:
            print(f"[Ekom API HTTP Hatası] MPN: {mpn}, Code: {resp.status_code}, Body: {resp.text[:200]}")
            return _bos_parca_sonucu(f"HTTP Hatası {resp.status_code}")
            
        data = resp.json()
        
        # GraphQL / API Mantıksal Hata Kontrolü (Geliştirme 3)
        if "errors" in data or "Error" in data:
            hata_mesaji = str(data.get("errors") or data.get("Error"))
            print(f"[Ekom API Mantıksal Hata] MPN: {mpn}, Detay: {hata_mesaji[:200]}")
            return _bos_parca_sonucu(f"API Hatası: Kota/Parametre sorunu")

        return _ekom_json_ayristir(data)
        
    except Exception as e: # Bare except düzeltildi (Geliştirme 5)
        print(f"[Ekom Bağlantı/Sistem Hatası] MPN: {mpn}, Exception: {e}")
        return _bos_parca_sonucu(f"Sistem Hatası: {str(e)[:50]}")

@st.cache_data(show_spinner=False, ttl=3600)
def ekom_istek_at_cache(mpn: str) -> dict:
    return ekom_istek_at(mpn)

def toplu_ekom_sorgula(mpn_listesi: list, max_workers: int = TOPLU_SORGU_WORKER_SAYISI) -> dict:
    sonuclar = {}
    benzersiz_mpnler = list(dict.fromkeys(mpn_listesi))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_mpn = {executor.submit(ekom_istek_at_cache, mpn): mpn for mpn in benzersiz_mpnler}
        for future in concurrent.futures.as_completed(future_to_mpn):
            mpn = future_to_mpn[future]
            try: 
                sonuclar[mpn] = future.result()
            except Exception as e: 
                print(f"[Thread Hatası] MPN: {mpn}, Hata: {e}")
                sonuclar[mpn] = _bos_parca_sonucu(f"Çalışma Hatası")
    return sonuclar

def yasam_durumu_kategorisi_belirle(ham_metin: str) -> str:
    if not ham_metin or ham_metin == "-" or ham_metin.lower() == "none": return "Bilinmiyor"
    metin = ham_metin.lower()
    if any(x in metin for x in ["obsolete", "eol", "end of life", "discontinued"]): return "EOL"
    if any(x in metin for x in ["nrnd", "not recommended"]): return "NRND"
    if any(x in metin for x in ["active", "production"]): return "Aktif"
    return "Diğer"

# Ortak Metrik Hesaplama Fonksiyonu (Geliştirme 6 - Zengin Alternatifler İçin)
def parca_metriklerini_hesapla(sonuc: dict, gereken_miktar: int) -> dict:
    """Hem ana tablo hem de alternatifler için risk, stok, fiyat hesaplar."""
    if sonuc.get("hata"): return {"risk": 50, "karsilama": 0, "toplam_stok": 0, "uygun_tedarikci": None, "fiyat_metni": "-", "maliyet_metni": "-"}
    if not sonuc.get("bulundu"): return {"risk": 100, "karsilama": 0, "toplam_stok": 0, "uygun_tedarikci": None, "fiyat_metni": "-", "maliyet_metni": "-"}
    
    yasam = sonuc.get("yasam_durumu_kategori", "Bilinmiyor")
    teklifler = sonuc.get("teklifler", [])
    toplam_stok = sum((t[4] or 0) for t in teklifler)
    karsilama = min((toplam_stok / gereken_miktar * 100) if gereken_miktar > 0 else 0, 999)

    # Risk Mantığı
    if yasam == "EOL": risk = 95
    elif not teklifler: risk = 90
    else:
        stoklu_tedarikci = sum(1 for t in teklifler if (t[4] or 0) > 0)
        if toplam_stok < gereken_miktar: 
            risk = int(60 + (1 - (toplam_stok / gereken_miktar)) * 35)
        else:
            risk = 35 if stoklu_tedarikci == 1 else 10
        if yasam == "NRND": risk = min(risk + 20, 89)

    # Tedarikçi/Fiyat Mantığı
    uygun = [t for t in teklifler if (t[4] or 0) >= gereken_miktar]
    if uygun:
        uygun.sort(key=lambda t: (t[1] is None, t[1]))
        tedarikci, fiyat, birim, stok = uygun[0][0], uygun[0][1], uygun[0][2], uygun[0][4]
        fiyat_metni = f"{fiyat} {birim}"
        maliyet_metni = f"{fiyat * gereken_miktar:.2f} {birim}"
    else:
        tedarikci, fiyat_metni, maliyet_metni = "Kritik (Tek Kaynak Yetersiz)", "-", "-"

    return {
        "risk": risk, "karsilama": karsilama, "toplam_stok": toplam_stok,
        "uygun_tedarikci": tedarikci, "fiyat_metni": fiyat_metni, "maliyet_metni": maliyet_metni
    }

def excele_donustur(df: pd.DataFrame) -> bytes:
    arabellek = io.BytesIO()
    with pd.ExcelWriter(arabellek, engine="openpyxl") as yazici:
        df.to_excel(yazici, index=False, sheet_name="BOM Analizi")
    return arabellek.getvalue()

@st.cache_data(show_spinner=False, ttl=3600)
def ai_yanit_getir(prompt: str, api_key: str, model_name: str) -> str:
    """AI yanıtlarını önbellekler (Geliştirme 3 - Token Optimizasyonu)"""
    genai.configure(api_key=api_key)
    llm = genai.GenerativeModel(model_name)
    return llm.generate_content(prompt).text

# ============================================================
# ARAYÜZ (UI)
# ============================================================
st.title("CircuitBOM | Ekom BOM Intelligence")
st.markdown("BOM verilerinizi **Ekom Trial API** üzerinden zenginleştirin, riskleri analiz edin ve AI ile içgörüler oluşturun.")

# YAN MENÜ
with st.sidebar:
    st.header("⚙️ Proje Ayarları")
    yuklenen_dosya = st.file_uploader("BOM Dosyası Yükle", type=["csv", "xlsx"])
    uretim_adedi = st.number_input("Hedef Üretim Adedi:", min_value=1, value=100, step=1)
    
    if not GEMINI_API_KEY:
        st.info("💡 Gemini API anahtarı eksik. AI asistan devre dışı.")

# ANA VERİ HAZIRLIĞI
konsolide_df = None

# Tabları Başlat (Geliştirme 5 - Manuel Arama eklendi)
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📑 BOM Analiz Tablosu", "🔍 Detaylı Parça Analizi", 
    "🔄 Akıllı Konsolidasyon", "🤖 AI Asistan", "🔎 Manuel MPN Arama"
])

if yuklenen_dosya:
    try:
        df = pd.read_csv(yuklenen_dosya) if yuklenen_dosya.name.endswith(".csv") else pd.read_excel(yuklenen_dosya)
        eksik_sutunlar = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if eksik_sutunlar:
            st.error(f"Eksik Sütunlar: {', '.join(eksik_sutunlar)}")
            st.stop()
        
        konsolide_df = df.groupby(["MPN", "Manufacturer", "Description"], dropna=False).agg({"Qty": "sum", "RefDes": lambda x: ", ".join(map(str, x))}).reset_index()

        with st.spinner("Ekom Trial API'den veriler çekiliyor..."):
            ekom_map = toplu_ekom_sorgula(konsolide_df["MPN"].tolist())

            def satir_isleyici(row):
                mpn, birim_qty = row["MPN"], row["Qty"]
                gereken = int(birim_qty) * int(uretim_adedi)
                sonuc = ekom_map.get(mpn, _bos_parca_sonucu("Sonuç yok"))
                
                metrikler = parca_metriklerini_hesapla(sonuc, gereken)
                durum = f"Hata: {sonuc['hata']}" if sonuc.get("hata") else ("Bulundu" if sonuc.get("bulundu") else "Bulunamadı")
                
                return pd.Series([
                    durum, sonuc.get("yasam_durumu_kategori", "Bilinmiyor"), len(sonuc.get("teklifler", [])),
                    gereken, metrikler["toplam_stok"], f"%{metrikler['karsilama']:.0f}",
                    metrikler["uygun_tedarikci"], metrikler["fiyat_metni"], metrikler["maliyet_metni"], 
                    len(sonuc.get("alternatifler", [])), metrikler["risk"]
                ])

            sutunlar = ["Durum", "Yaşam Döngüsü", "Tedarikçi Sayısı", "İhtiyaç", "Küresel Stok", "Karşılama Oranı", "En Uygun Tedarikçi", "Birim Fiyat", "Toplam Maliyet", "Alternatif", "Risk Skoru"]
            konsolide_df[sutunlar] = konsolide_df.apply(satir_isleyici, axis=1)

        # Dashboard
        toplam_usd = konsolide_df["Toplam Maliyet"].apply(lambda x: float(str(x).replace("USD", "").strip()) if "USD" in str(x) else 0.0).sum()
        karsilanamayan = (konsolide_df["En Uygun Tedarikçi"] == "Kritik (Tek Kaynak Yetersiz)").sum()
        
        st.markdown("### 📊 Genel Görünüm")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Tahmini Maliyet (USD)", f"${toplam_usd:,.2f}")
        m2.metric("Benzersiz Parça", len(konsolide_df))
        m3.metric("Tedarik Edilemeyen", int(karsilanamayan), delta_color="inverse")
        m4.metric("Yüksek Riskli Parça", len(konsolide_df[konsolide_df["Risk Skoru"] >= 70]), delta_color="inverse")
        st.markdown("---")

    except Exception as e:
        st.error(f"Sistem Hatası: {e}")

# SEKME 1: BOM ANALİZ TABLOSU
with tab1:
    if konsolide_df is not None:
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

        # Geliştirme 2: Kritik satır renklendirmesi
        def tedarikci_stili(val):
            if val == "Kritik (Tek Kaynak Yetersiz)": return "background-color: rgba(255, 75, 75, 0.2); color: #ff4b4b; font-weight: bold;"
            return ""

        stil = konsolide_df.style.map(hucre_stili, subset=["Risk Skoru"]) \
                                 .map(yasam_stili, subset=["Yaşam Döngüsü"]) \
                                 .map(tedarikci_stili, subset=["En Uygun Tedarikçi"])
                                 
        st.dataframe(stil, use_container_width=True, height=500)
        
        # Geliştirme 4: Mime parametresi
        st.download_button(
            "📥 Analizi Excel Olarak İndir", 
            data=excele_donustur(konsolide_df), 
            file_name="nexus_bom_analizi.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.info("Lütfen sol menüden BOM dosyası yükleyin.")

# SEKME 2: DETAYLI PARÇA ANALİZİ
with tab2:
    if konsolide_df is not None:
        secilen_mpn = st.selectbox("Analiz Edilecek Parçayı Seçin:", konsolide_df["MPN"].tolist())
        if secilen_mpn:
            sonuc = ekom_map.get(secilen_mpn, {})
            secili_satir = konsolide_df[konsolide_df["MPN"] == secilen_mpn].iloc[0]
            
            c1, c2, c3 = st.columns(3)
            c1.info(f"**Üretici:** {secili_satir['Manufacturer']}\n\n**Açıklama:** {secili_satir['Description']}")
            c2.metric("Yaşam Döngüsü", secili_satir['Yaşam Döngüsü'])
            c3.metric("Hesaplanan Risk Skoru", secili_satir['Risk Skoru'])
            
            st.markdown("#### 🛒 Küresel Tedarikçi Teklifleri")
            teklifler = sonuc.get("teklifler", [])
            if teklifler:
                teklif_df = pd.DataFrame(teklifler, columns=["Tedarikçi", "Birim Fiyat", "Para Birimi", "Min. Sipariş (MOQ)", "Stok Adedi", "Link"])
                st.dataframe(teklif_df, use_container_width=True)
            else:
                st.warning("Bu parça için teklif bulunamadı.")
                
            st.markdown("#### 🔄 Zenginleştirilmiş Alternatifler (Geliştirme 2)")
            alternatifler = sonuc.get("alternatifler", [])
            if alternatifler:
                # Alternatiflerin kendi metriklerini oluştur (Stok, Fiyat vb.)
                alt_veriler = []
                for alt in alternatifler:
                    # Sanal hesaplama, sadece 1 adet üretiliyormuş gibi metrik çıkarıyoruz.
                    sanal_sonuc = {"bulundu": True, "yasam_durumu_kategori": alt["yasam"], "teklifler": [("Alt", alt["fiyat"], "USD", 1, alt["stok"], "-")] if alt["stok"] > 0 else []}
                    metrik = parca_metriklerini_hesapla(sanal_sonuc, 1)
                    alt_veriler.append({
                        "Alternatif MPN": alt["mpn"],
                        "Ürün Adı": alt["isim"],
                        "Yaşam Döngüsü": alt["yasam"],
                        "Stok": alt["stok"],
                        "Birim Fiyat": f"{alt['fiyat']} USD" if alt["fiyat"] else "-",
                        "Risk Skoru": metrik["risk"]
                    })
                
                alt_df = pd.DataFrame(alt_veriler)
                st.dataframe(alt_df.style.map(hucre_stili, subset=["Risk Skoru"]).map(yasam_stili, subset=["Yaşam Döngüsü"]), use_container_width=True)
            else:
                st.info("Bu parça için sistemde alternatif bulunamadı.")

# SEKME 3: AKILLI KONSOLİDASYON
with tab3:
    if konsolide_df is not None:
        st.markdown("Aynı işlevi gören ancak farklı üreticilerden sağlanan parçaları tespit edip birleştirin.")
        konsolide_df["_normalize"] = konsolide_df["Description"].astype(str).str.strip().str.lower()
        konsolidasyon_adaylari = konsolide_df[konsolide_df.groupby("_normalize")["MPN"].transform("nunique") > 1]

        if konsolidasyon_adaylari.empty:
            st.success("Tebrikler! Optimize edilebilecek benzer parça bulunamadı.")
        else:
            for _, grup in konsolidasyon_adaylari.groupby("_normalize"):
                with st.expander(f"📌 {grup.iloc[0]['Description']} ({len(grup)} Varyasyon)"):
                    grup_sirali = grup.copy().sort_values(by=["Risk Skoru"])
                    onerilen = grup_sirali.iloc[0]
                    st.dataframe(grup[["MPN", "Manufacturer", "Birim Fiyat", "Küresel Stok", "Risk Skoru"]], use_container_width=True)
                    st.success(f"**Sistem Önerisi:** Tüm alımları **{onerilen['MPN']}** üzerinden yapın.")

# SEKME 4: YAPAY ZEKA ASİSTANI
with tab4:
    if konsolide_df is not None:
        st.markdown("BOM listenizle ilgili yapay zekaya sorular sorun.")
        soru = st.text_input("💬 Asistana Sorun:", placeholder="Risk skoru 70'in üzerinde olan parçaları özetle.")
        
        if soru:
            if not GEMINI_API_KEY:
                st.error("🔑 Ayarlardan Gemini API Key girmelisiniz.")
            else:
                with st.spinner("🤖 Nexus AI analiz ediyor..."):
                    try:
                        # Geliştirme 3 (Token Optimizasyonu): Yapay zekaya devasa bir CSV göndermek yerine
                        # Sadece gereken sütunları (Özet CSV) filtrelenmiş şekilde iletiyoruz.
                        ozet_df = konsolide_df[["MPN", "Description", "İhtiyaç", "Küresel Stok", "Yaşam Döngüsü", "Risk Skoru", "Birim Fiyat"]]
                        csv_metni = ozet_df.to_csv(index=False)
                        
                        prompt = f"""Sen tedarik zinciri AI asistanısın. Aşağıdaki BOM TABLOSU (özet) verisine dayanarak cevap ver.
                        BOM VERİSİ:\n{csv_metni}\n\nSORU: {soru}"""
                        
                        yanit_metni = ai_yanit_getir(prompt, GEMINI_API_KEY, GEMINI_MODEL_NAME)
                        st.info(yanit_metni)
                    except Exception as e:
                        st.error(f"Yapay zeka hatası: {e}")

# SEKME 5: YENİ - MANUEL MPN ARAMA (Geliştirme 5)
with tab5:
    st.markdown("### 🔍 Hızlı Parça Arama")
    st.write("Excel yüklemeden, anlık olarak Ekom veritabanından parça sorgulayın.")
    
    manuel_girdi = st.text_input("MPN Girin (Virgülle ayırarak birden fazla girebilirsiniz):", placeholder="Örn: BC847, LM324, NE555")
    
    if st.button("Ara", type="primary") and manuel_girdi:
        aranacak_mpnler = [m.strip() for m in manuel_girdi.split(",") if m.strip()]
        
        with st.spinner("Ekom aranıyor..."):
            sonuclar = toplu_ekom_sorgula(aranacak_mpnler)
            
            # Gelen sonuçları ana tablodaki formata dönüştür
            gosterilecek_veriler = []
            for m in aranacak_mpnler:
                s = sonuclar.get(m, _bos_parca_sonucu("Sonuç Yok"))
                metrik = parca_metriklerini_hesapla(s, 1) # Manuel aramada varsayılan miktar: 1 adet
                gosterilecek_veriler.append({
                    "MPN": m,
                    "Durum": "Bulundu" if s["bulundu"] else "Bulunamadı",
                    "Açıklama": s["aciklama"],
                    "Üretici": s["uretici"],
                    "Yaşam Döngüsü": s["yasam_durumu_kategori"],
                    "Küresel Stok": metrik["toplam_stok"],
                    "Birim Fiyat": metrik["fiyat_metni"],
                    "Risk Skoru": metrik["risk"]
                })
                
            manuel_df = pd.DataFrame(gosterilecek_veriler)
            
            def hucre_stili(val):
                try: v = float(val)
                except: return ""
                if v >= 70: return "background-color: rgba(255, 75, 75, 0.2); color: #ff4b4b; font-weight: bold;"
                if v >= 30: return "background-color: rgba(255, 164, 33, 0.2); color: #ffa421;"
                return "background-color: rgba(33, 195, 84, 0.2); color: #21c354;"
                
            st.dataframe(manuel_df.style.map(hucre_stili, subset=["Risk Skoru"]), use_container_width=True)
            
            if len(aranacak_mpnler) == 1:
                st.markdown("#### Alternatifler (Çapraz Referans)")
                tek_sonuc_alt = sonuclar[aranacak_mpnler[0]].get("alternatifler", [])
                if tek_sonuc_alt:
                    st.dataframe(pd.DataFrame(tek_sonuc_alt), use_container_width=True)
                else:
                    st.info("Bu parça için alternatif kayıtlı değil.")