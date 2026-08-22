import streamlit as st
import pandas as pd
import requests
import google.generativeai as genai

# --- AYARLAR VE API ANAHTARLARI ---
st.set_page_config(page_title="BOM Analiz Aracı PRO", layout="wide")

# UYARI: Kendi API anahtarını aşağıya tırnak işaretlerini bozmadan yapıştır!
GEMINI_API_KEY = "BURAYA_GEMINI_API_ANAHTARINI_YAZ"
MOUSER_API_KEY = "BURAYA_MOUSER_API_ANAHTARINI_YAZ"

# --- GERÇEK KOMPONENT API FONKSİYONU (Mouser Örneği) ---
def gercek_api_ile_sorgula(mpn):
    if MOUSER_API_KEY == "BURAYA_MOUSER_API_ANAHTARINI_YAZ":
        return pd.Series(["Bilinmiyor (API Key Eksik)", "-", "-", 0])
        
    url = "https://api.mouser.com/api/v1/search/partnumber"
    headers = {'Content-Type': 'application/json'}
    payload = {
        "SearchByPartRequest": {
            "mouserPartNumber": mpn,
            "partSearchOptions": "string"
        }
    }
    
    try:
        response = requests.post(f"{url}?apiKey={MOUSER_API_KEY}", json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            parcalar = data.get('SearchResults', {}).get('Parts', [])
            
            if parcalar:
                ilk_sonuc = parcalar[0]
                durum = ilk_sonuc.get('LifecycleStatus', 'Active')
                rohs = ilk_sonuc.get('ROHSStatus', 'Bilinmiyor')
                
                risk_skoru = 10
                if durum in ['Obsolete', 'NRND', 'End of Life']:
                    risk_skoru = 90
                
                return pd.Series([durum, rohs, "Mouser'da Bulundu", risk_skoru])
            else:
                return pd.Series(["Bulunamadı", "-", "-", 100])
        else:
            return pd.Series(["API Hatası", "-", "-", 0])
            
    except Exception as e:
        return pd.Series(["Bağlantı Hatası", "-", "-", 0])

# --- ARAYÜZ VE ANALİZ ---
st.title("🛠️ BOM (Bill of Materials) Pro Analiz Aracı")
st.write("Bu uygulama, gerçek API'ler ve LLM (Yapay Zeka) kullanarak BOM listenizi analiz eder.")
st.markdown("---") 

yuklenen_dosya = st.file_uploader("Lütfen BOM dosyanızı (Excel veya CSV) yükleyin", type=["csv", "xlsx"])

if yuklenen_dosya is not None:
    if yuklenen_dosya.name.endswith('.csv'):
        df = pd.read_csv(yuklenen_dosya)
    else:
        df = pd.read_excel(yuklenen_dosya)
        
    konsolide_df = df.groupby(['MPN', 'Manufacturer', 'Description']).agg({
        'Qty': 'sum',
        'RefDes': lambda x: ', '.join(x)
    }).reset_index()
    
    st.subheader("🔍 Gerçek Zamanlı Komponent Analizi (İnternetten Çekiliyor...)")
    
    with st.spinner('İnternet üzerinden komponent verileri sorgulanıyor...'):
        konsolide_df[['Tedarik Durumu', 'RoHS Durumu', 'Bulunabilirlik', 'Risk Skoru']] = konsolide_df['MPN'].apply(gercek_api_ile_sorgula)
    
    st.dataframe(konsolide_df, use_container_width=True)
    
    # --- YAPAY ZEKA ASİSTANI (GEMINI) ---
    st.markdown("---")
    st.subheader("🧠 Yapay Zeka BOM Asistanı")
    st.info("Bu asistan doğrudan BOM verinizi bağlam olarak okur ve mantıksal çıkarım yapar.")
    
    soru = st.text_input("BOM hakkında bir soru sorun:", placeholder="Bu projede kaç farklı 10 kΩ direnç kullanılıyor?")
    
    if soru:
        if GEMINI_API_KEY == "BURAYA_GEMINI_API_ANAHTARINI_YAZ" or GEMINI_API_KEY == "":
            st.error("Yapay Zeka özelliğini kullanmak için kodun içine GEMINI_API_KEY girmelisiniz.")
        else:
            with st.spinner('Yapay Zeka tabloyu inceliyor ve düşünüyor...'):
                try:
                    genai.configure(api_key=GEMINI_API_KEY)
                    
                    # Google API'nin bizden açıkça istediği en güncel modeli yazıyoruz:
                    llm_model = genai.GenerativeModel('gemini-3.6-flash')
                    
                    bom_metni = konsolide_df.to_csv(index=False)
                    
                    prompt = f"""
                    Sen uzman bir elektronik sistem mühendisisin. Aşağıda virgülle ayrılmış bir BOM (Malzeme Listesi) verisi var.
                    Kullanıcının sorusuna SADECE bu tablodaki verilere dayanarak, mühendislik formatında, net ve doğru bir cevap ver.
                    Hesaplama gerekiyorsa Qty (Adet) sütunlarını topla.
                    
                    BOM VERİSİ:
                    {bom_metni}
                    
                    KULLANICI SORUSU:
                    {soru}
                    """
                    
                    response = llm_model.generate_content(prompt)
                    
                    st.success("🤖 AI Yanıtı (Bağlanılan Model: gemini-3.6-flash):")
                    st.write(response.text)
                    
                except Exception as e:
                    st.error(f"Yapay zeka ile iletişim kurulurken bir hata oluştu: {e}")