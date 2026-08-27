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
    page_title="CircuitBOM // Supply Chain Intelligence",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)


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
# emekliye ayırıyor -- API'nin kendisi hata mesajında hangi modele geçmemiz
# gerektiğini söylüyorsa (404 + "use models/X" gibi), o ismi buraya yazın.
GEMINI_MODEL_NAME = "gemini-3.6-flash"

REQUIRED_COLUMNS = ["MPN", "Manufacturer", "Description", "Qty", "RefDes"]

# ============================================================
# ARAYÜZ KİMLİĞİ: "CIRCUITBOM" TEMASI (bakır + camgöbeği, koyu zemin)
# ============================================================
def _arayuz_kimligini_uygula():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap');

        :root {
            --cb-bg: #0A0E14;
            --cb-bg-panel: #10161F;
            --cb-copper: #D9A15B;
            --cb-cyan: #3FE0D0;
            --cb-text: #E7ECF2;
            --cb-text-dim: #8B96A5;
            --cb-border: #202A38;
            --cb-danger: #FF6B6B;
            --cb-warn: #F2C94C;
            --cb-ok: #4ADE9C;
        }

        html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

        .stApp {
            background:
                radial-gradient(circle at 15% 0%, rgba(63,224,208,0.06), transparent 45%),
                radial-gradient(circle at 85% 15%, rgba(217,161,91,0.07), transparent 40%),
                var(--cb-bg);
            color: var(--cb-text);
        }

        h1, h2, h3, .cb-hero-title { font-family: 'Space Grotesk', sans-serif !important; }
        .block-container { padding-top: 1.6rem; max-width: 1300px; }

        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}

        /* --- HERO BANNER --- */
        .cb-hero {
            border: 1px solid var(--cb-border);
            border-radius: 14px;
            padding: 1.8rem 2.2rem;
            margin-bottom: 1.4rem;
            background: linear-gradient(135deg, rgba(217,161,91,0.08), rgba(63,224,208,0.05));
            position: relative;
            overflow: hidden;
        }
        .cb-hero::after {
            content: "";
            position: absolute; inset: 0;
            background-image:
                linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px),
                linear-gradient(0deg, rgba(255,255,255,0.03) 1px, transparent 1px);
            background-size: 28px 28px;
            pointer-events: none;
        }
        .cb-eyebrow {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.72rem; letter-spacing: 0.14em;
            color: var(--cb-copper); text-transform: uppercase;
            margin-bottom: 0.5rem; display: inline-block;
        }
        .cb-hero-title { font-size: 1.9rem; font-weight: 700; color: var(--cb-text); margin: 0 0 0.35rem 0; letter-spacing: -0.01em; }
        .cb-hero-title span { color: var(--cb-cyan); }
        .cb-hero-sub { color: var(--cb-text-dim); font-size: 0.92rem; max-width: 780px; line-height: 1.55; }

        /* --- BÖLÜM BAŞLIKLARI (devre izi ayracı) --- */
        .cb-section { display: flex; align-items: center; gap: 0.8rem; margin: 2rem 0 0.8rem 0; }
        .cb-section-num {
            font-family: 'JetBrains Mono', monospace; font-size: 0.75rem;
            color: var(--cb-bg); background: var(--cb-copper); border-radius: 4px;
            padding: 0.15rem 0.5rem; font-weight: 600; white-space: nowrap;
        }
        .cb-section-title { font-family: 'Space Grotesk', sans-serif; font-size: 1.2rem; font-weight: 600; color: var(--cb-text); white-space: nowrap; }
        .cb-trace {
            flex: 1; height: 2px;
            background: repeating-linear-gradient(90deg, var(--cb-border) 0px, var(--cb-border) 5px, transparent 5px, transparent 11px);
            position: relative;
        }
        .cb-trace::after {
            content: ""; position: absolute; right: 0; top: -3px;
            width: 8px; height: 8px; border-radius: 50%;
            background: var(--cb-cyan); box-shadow: 0 0 8px var(--cb-cyan);
        }
        .cb-section-desc { color: var(--cb-text-dim); font-size: 0.86rem; margin: -0.3rem 0 0.9rem 0; }

        /* --- METRİK KARTLARI --- */
        .cb-metric-row { display: flex; gap: 1rem; margin: 0.6rem 0 1.4rem 0; flex-wrap: wrap; }
        .cb-metric-card {
            flex: 1; min-width: 200px; background: var(--cb-bg-panel);
            border: 1px solid var(--cb-border); border-left: 3px solid var(--cb-copper);
            border-radius: 10px; padding: 0.9rem 1.1rem;
        }
        .cb-metric-card.cyan { border-left-color: var(--cb-cyan); }
        .cb-metric-card.danger { border-left-color: var(--cb-danger); }
        .cb-metric-label { font-family: 'JetBrains Mono', monospace; font-size: 0.68rem; letter-spacing: 0.08em; color: var(--cb-text-dim); text-transform: uppercase; }
        .cb-metric-value { font-family: 'JetBrains Mono', monospace; font-size: 1.6rem; font-weight: 600; color: var(--cb-text); margin-top: 0.1rem; }

        /* --- ROZETLER --- */
        .cb-badge { display: inline-block; font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; padding: 0.2rem 0.6rem; border-radius: 5px; font-weight: 600; letter-spacing: 0.02em; }
        .cb-badge.danger { background: rgba(255,107,107,0.15); color: var(--cb-danger); border: 1px solid rgba(255,107,107,0.35); }
        .cb-badge.warn   { background: rgba(242,201,76,0.15); color: var(--cb-warn); border: 1px solid rgba(242,201,76,0.35); }
        .cb-badge.ok     { background: rgba(74,222,156,0.15); color: var(--cb-ok); border: 1px solid rgba(74,222,156,0.35); }
        .cb-badge.info   { background: rgba(63,224,208,0.12); color: var(--cb-cyan); border: 1px solid rgba(63,224,208,0.3); }

        /* --- SEKMELER (dinamik görünüm) --- */
        .stTabs [data-baseweb="tab-list"] { gap: 6px; border-bottom: 1px solid var(--cb-border); }
        .stTabs [data-baseweb="tab"] {
            height: 44px; background-color: transparent; border-radius: 8px 8px 0 0;
            padding: 0 1.1rem; color: var(--cb-text-dim); font-family: 'Space Grotesk', sans-serif; font-weight: 600;
        }
        .stTabs [aria-selected="true"] {
            background: linear-gradient(180deg, rgba(217,161,91,0.14), rgba(217,161,91,0.03)) !important;
            color: var(--cb-copper) !important;
            border-bottom: 2px solid var(--cb-copper);
        }

        /* --- WIDGETLAR --- */
        div[data-testid="stFileUploader"], div[data-testid="stDataFrame"], div[data-testid="stDataEditor"] {
            border-radius: 10px; overflow: hidden;
        }
        .stButton > button, .stDownloadButton > button {
            font-family: 'Space Grotesk', sans-serif; font-weight: 600;
            border-radius: 8px; border: 1px solid var(--cb-copper);
            background: linear-gradient(135deg, rgba(217,161,91,0.18), rgba(63,224,208,0.10));
            color: var(--cb-text);
        }
        .stButton > button:hover, .stDownloadButton > button:hover { border-color: var(--cb-cyan); color: var(--cb-cyan); }
        [data-testid="stMetricValue"] { font-family: 'JetBrains Mono', monospace; }
        code, .stCode { font-family: 'JetBrains Mono', monospace !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def bolum_basligi(numara: str, baslik: str, aciklama: str = ""):
    st.markdown(
        f"""
        <div class="cb-section">
            <span class="cb-section-num">{numara}</span>
            <span class="cb-section-title">{baslik}</span>
            <span class="cb-trace"></span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if aciklama:
        st.markdown(f'<div class="cb-section-desc">{aciklama}</div>', unsafe_allow_html=True)


def metrik_karti(label: str, deger: str, renk: str = "copper") -> str:
    sinif = {"cyan": "cyan", "danger": "danger"}.get(renk, "")
    return f"""
        <div class="cb-metric-card {sinif}">
            <div class="cb-metric-label">{label}</div>
            <div class="cb-metric-value">{deger}</div>
        </div>
    """


def yasam_rozeti(kategori: str) -> str:
    sinif = {"Obsolete": "danger", "EOL": "danger", "NRND": "warn", "Aktif": "ok"}.get(kategori, "info")
    return f'<span class="cb-badge {sinif}">{kategori}</span>'


# ============================================================
# NEXAR (OCTOPART) API — TÜM ALTERNATİF TEDARİKÇİLERDEN FİYAT + STOK
# ============================================================
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


@st.cache_data(show_spinner=False, ttl=60 * 30)
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
        # DEBUG: gerçek hata mesajı terminale yazdırılıyor -- "invalid_scope",
        # "part limit" gibi sorunları buradan teşhis ettik, bu yüzden bilerek koruyoruz.
        print(f"[Nexar Token Hatası] status={resp.status_code} body={resp.text[:300]}")
    except requests.exceptions.RequestException as e:
        print(f"[Nexar Token Bağlantı Hatası] {e}")
    return ""


def _bos_parca_sonucu(hata=None) -> dict:
    return {
        "bulundu": False, "aciklama": "-", "uretici": "-", "teklifler": [],
        "yasam_durumu_ham": "-", "yasam_durumu_kategori": "Bilinmiyor",
        "alternatifler": [], "hata": hata,
    }


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
            if not fiyatlar:
                continue
            en_dusuk = min(fiyatlar, key=lambda p: p.get("quantity", 0))
            teklifler.append((
                tedarikci_adi, en_dusuk.get("price"), en_dusuk.get("currency", "USD"),
                en_dusuk.get("quantity", 1), teklif.get("inventoryLevel", 0) or 0, teklif.get("clickUrl", ""),
            ))
    teklifler.sort(key=lambda t: (t[1] is None, t[1]))

    alternatifler = [
        {"mpn": alt.get("mpn") or "-", "isim": alt.get("name") or "-", "link": alt.get("octopartUrl") or ""}
        for alt in part.get("similarParts", []) or []
    ]

    return {
        "bulundu": True, "aciklama": aciklama, "uretici": uretici, "teklifler": teklifler,
        "yasam_durumu_ham": yasam_durumu_ham, "yasam_durumu_kategori": yasam_durumu_kategori,
        "alternatifler": alternatifler, "hata": None,
    }


def yasam_durumu_kategorisi_belirle(ham_metin: str) -> str:
    if not ham_metin or ham_metin == "-":
        return "Bilinmiyor"
    metin = ham_metin.lower()
    if "obsolete" in metin:
        return "Obsolete"
    if any(x in metin for x in ["eol", "end of life", "discontinued"]):
        return "EOL"
    if any(x in metin for x in ["nrnd", "not recommended for new designs"]):
        return "NRND"
    if any(x in metin for x in ["active", "production"]):
        return "Aktif"
    return "Diğer"


def uygun_en_ucuz_teklifi_bul(teklifler: list, gereken_miktar: int):
    """Tek başına gereken miktarı karşılayabilecek (stok >= gereken) tedarikçiler
    arasından en ucuzunu bulur. Döner: (tedarikci, fiyat, para_birimi, stok) ya da None."""
    uygun = [t for t in teklifler if (t[4] or 0) >= gereken_miktar]
    if not uygun:
        return None
    uygun.sort(key=lambda t: (t[1] is None, t[1]))
    return uygun[0][0], uygun[0][1], uygun[0][2], uygun[0][4]


def _nexar_toplu_istek_at(mpn_grubu: tuple) -> dict:
    """Birden fazla MPN'i TEK BİR GraphQL isteğinde (alias tekniğiyle) sorgular."""
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
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        print(f"[Nexar Bağlantı Hatası] {e}")
        return {mpn: _bos_parca_sonucu(f"Bağlantı hatası: {e}") for mpn in mpn_grubu}

    if resp.status_code != 200:
        # DEBUG: HTTP durum kodu ve gövdeyi terminale yazdırıyoruz. Örn. "part
        # limit of 0" kota hatası tam olarak bu yolla teşhis edilmişti.
        print(f"[Nexar API Hatası] status={resp.status_code} body={resp.text[:300]}")
        return {mpn: _bos_parca_sonucu(f"API hatası ({resp.status_code})") for mpn in mpn_grubu}

    try:
        data = resp.json()
    except ValueError:
        print(f"[Nexar JSON Hatası] Yanıt JSON formatında değil: {resp.text[:200]}")
        return {mpn: _bos_parca_sonucu("Geçersiz yanıt (JSON değil)") for mpn in mpn_grubu}

    veri = data.get("data") or {}

    # GraphQL, HTTP 200 dönse bile "errors" alanında hata bildirebilir (örn.
    # kota aşımı: "part limit of 0"). Bunu MUTLAKA kontrol ediyoruz --
    # kontrol etmezsek bu durum sessizce "parça bulunamadı" sanılır.
    if data.get("errors"):
        hata_metni = str(data["errors"])[:250]
        print(f"[Nexar GraphQL Hatası] {hata_metni}")
        if not veri:
            return {mpn: _bos_parca_sonucu(hata_metni) for mpn in mpn_grubu}

    sonuclar = {}
    for i, mpn in enumerate(mpn_grubu):
        parca_sonuclari = (veri.get(f"p{i}") or {}).get("results", []) or []
        if not parca_sonuclari:
            sonuclar[mpn] = _bos_parca_sonucu(None)  # bulunamadı ama hata değil
            continue
        part = parca_sonuclari[0].get("part", {}) or {}
        sonuclar[mpn] = _part_json_ayristir(part)

    return sonuclar


@st.cache_data(show_spinner=False, ttl=60 * 60)
def _nexar_toplu_istek_at_cache(mpn_grubu: tuple) -> dict:
    return _nexar_toplu_istek_at(mpn_grubu)


def toplu_nexar_sorgula(mpn_listesi, max_workers: int = 5) -> dict:
    """Birden çok MPN için Nexar verilerini, gruplar halinde VE paralel çeker.
    50 parça -> ~4 istek (15'erli gruplar), bu istekler aynı anda atılır."""
    sonuclar = {}
    benzersiz_mpnler = list(dict.fromkeys(mpn_listesi))

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
    """Üretim planına göre stok yeterliliği + yaşam döngüsü durumunu birlikte
    değerlendiren risk skoru. Döndürür: (risk, toplam_stok, karsilama_%)."""
    if nexar_sonucu.get("hata"):
        return 50, 0, 0
    if not nexar_sonucu.get("bulundu"):
        return 100, 0, 0

    yasam_kategori = nexar_sonucu.get("yasam_durumu_kategori", "Bilinmiyor")
    teklifler = nexar_sonucu.get("teklifler", [])
    toplam_stok = sum((t[4] or 0) for t in teklifler)
    karsilama_orani = min((toplam_stok / gereken_miktar * 100) if gereken_miktar > 0 else 0, 999)

    # Obsolete/EOL: üretici artık üretmiyor -- stok yeterli görünse bile
    # mevcut stok tükenince yeniden temin imkânı yok, risk çok yüksek.
    if yasam_kategori in ("Obsolete", "EOL"):
        return 95, toplam_stok, karsilama_orani

    if not teklifler:
        return 90, 0, 0

    if toplam_stok == 0:
        return 95, toplam_stok, 0

    if toplam_stok < gereken_miktar:
        eksik_oran = 1 - (toplam_stok / gereken_miktar)
        return int(60 + eksik_oran * 35), toplam_stok, karsilama_orani

    stoklu_tedarikci_sayisi = sum(1 for t in teklifler if (t[4] or 0) > 0)
    risk = 35 if stoklu_tedarikci_sayisi == 1 else 10

    # NRND: hâlâ üretiliyor ama üretici "yeni tasarımda kullanmayın" diyor.
    if yasam_kategori == "NRND":
        risk = min(risk + 20, 89)

    return risk, toplam_stok, karsilama_orani


def parca_metriklerini_hesapla(mpn: str, gereken_miktar: int, nexar_map: dict) -> dict:
    """TEK bir yerden hem ana BOM tablosu hem 'Parça Alternatifleri' tablosu
    için aynı zenginlikte satır verisi üretir -- iki bölüm birebir aynı
    mantıkla hesaplanır, kod da tekrar edilmez."""
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
        uygun_tedarikci, uygun_fiyat, uygun_para_birimi, _ = uygun_teklif
        uygun_fiyat_metni = f"{uygun_fiyat} {uygun_para_birimi}" if uygun_fiyat is not None else "-"
        uygun_toplam_maliyet = (
            f"{uygun_fiyat * gereken_miktar:.2f} {uygun_para_birimi}" if uygun_fiyat is not None else "-"
        )
    else:
        uygun_tedarikci = "Kritik (Tek Kaynak Yetersiz)"
        uygun_fiyat_metni = "-"
        uygun_toplam_maliyet = "-"

    return {
        "MPN": mpn,
        "Durum": durum,
        "Yaşam Döngüsü": sonuc.get("yasam_durumu_kategori", "Bilinmiyor"),
        "Tedarikçi Sayısı": len(teklifler),
        "Gereken Miktar": gereken_miktar,
        "Toplam Stok": toplam_stok,
        "Stok Karşılama": f"%{karsilama_orani:.0f}" if teklifler else "-",
        "Uygun En Ucuz Tedarikçi": uygun_tedarikci,
        "Birim Fiyat": uygun_fiyat_metni,
        "Toplam Maliyet": uygun_toplam_maliyet,
        "Alternatif Sayısı": len(sonuc.get("alternatifler", [])),
        "Risk Skoru": risk,
    }


def excele_donustur(df: pd.DataFrame) -> bytes:
    arabellek = io.BytesIO()
    with pd.ExcelWriter(arabellek, engine="openpyxl") as yazici:
        df.to_excel(yazici, index=False, sheet_name="BOM Analizi")
    return arabellek.getvalue()


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
    if val == "Kritik (Tek Kaynak Yetersiz)":
        return "background-color: #ffcccc; color: black"
    return ""


def stil_uygula(df: pd.DataFrame):
    try:
        stil = df.style
        if "Risk Skoru" in df.columns:
            stil = stil.map(risk_renklendir, subset=["Risk Skoru"])
        if "Yaşam Döngüsü" in df.columns:
            stil = stil.map(yasam_durumu_renklendir, subset=["Yaşam Döngüsü"])
        if "Uygun En Ucuz Tedarikçi" in df.columns:
            stil = stil.map(uygun_tedarikci_renklendir, subset=["Uygun En Ucuz Tedarikçi"])
    except AttributeError:
        stil = df.style
        if "Risk Skoru" in df.columns:
            stil = stil.applymap(risk_renklendir, subset=["Risk Skoru"])
        if "Yaşam Döngüsü" in df.columns:
            stil = stil.applymap(yasam_durumu_renklendir, subset=["Yaşam Döngüsü"])
        if "Uygun En Ucuz Tedarikçi" in df.columns:
            stil = stil.applymap(uygun_tedarikci_renklendir, subset=["Uygun En Ucuz Tedarikçi"])
    return stil


@st.cache_data(show_spinner=False, ttl=60 * 60)
def gemini_yanit_al(soru: str, bom_metni: str, uretim_adedi: int) -> str:
    """TOKEN TASARRUFU: Aynı soru + aynı BOM verisiyle tekrar sorulursa
    (örn. kullanıcı sayfayı yenilerse), Gemini'ye tekrar istek atmadan
    önbellekten cevap dönülür."""
    genai.configure(api_key=GEMINI_API_KEY)
    llm_model = genai.GenerativeModel(GEMINI_MODEL_NAME)

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
    return response.text


# ============================================================
# ARAYÜZ
# ============================================================
_arayuz_kimligini_uygula()

st.markdown(
    """
    <div class="cb-hero">
        <span class="cb-eyebrow">// SUPPLY CHAIN INTELLIGENCE</span>
        <div class="cb-hero-title">Circuit<span>BOM</span></div>
        <div class="cb-hero-sub">
            BOM listenizi yükleyin ya da elle girin; onlarca tedarikçide gerçek
            zamanlı fiyat/stok taraması yapalım, yaşam döngüsü riskini
            hesaplayalım, alternatif ve konsolidasyon önerileri çıkaralım.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### ⚙️ Proje Ayarları")
    uretim_adedi = st.number_input(
        "Hedef üretim adedi (kart):", min_value=1, value=100, step=1,
        help="BOM'daki her parçanın Qty (kart başına adet) değeri bu sayı ile çarpılır.",
    )
    st.markdown("---")
    if not NEXAR_CLIENT_ID or not NEXAR_CLIENT_SECRET:
        st.warning("⚠️ Nexar API anahtarları eksik. Gerçek tedarik verisi çekilemeyecek.")
    if not GEMINI_API_KEY:
        st.info("💡 Gemini API anahtarı eksik. AI asistan devre dışı kalacak.")
    st.caption("Anahtarları `.streamlit/secrets.toml` dosyasına ekleyin.")

# ------------------------------------------------------------
# VERİ GİRİŞİ: DOSYA YÜKLEME veya MANUEL GİRİŞ
# ------------------------------------------------------------
bolum_basligi("ADIM 01", "BOM Verisini Girin", "Hazır bir dosyanız varsa yükleyin, yoksa parçaları elle girin.")

giris_tab1, giris_tab2 = st.tabs(["📁 Dosya Yükle (Excel/CSV)", "⌨️ Manuel Giriş"])

df_kaynagi = None

with giris_tab1:
    yuklenen_dosya = st.file_uploader(
        "BOM dosyası", type=["csv", "xlsx"], label_visibility="collapsed", key="dosya_yukleyici"
    )
    if yuklenen_dosya is not None:
        try:
            df_dosya = (
                pd.read_csv(yuklenen_dosya) if yuklenen_dosya.name.endswith(".csv")
                else pd.read_excel(yuklenen_dosya)
            )
            eksik_sutunlar = [c for c in REQUIRED_COLUMNS if c not in df_dosya.columns]
            if eksik_sutunlar:
                st.error(
                    f"Yüklenen dosyada şu sütunlar eksik: {', '.join(eksik_sutunlar)}. "
                    f"Beklenen sütunlar: {', '.join(REQUIRED_COLUMNS)}."
                )
            else:
                st.success(f"{len(df_dosya)} satır okundu.")
                df_kaynagi = df_dosya
        except Exception as e:
            st.error(f"Dosya okunurken bir hata oluştu: {e}")

with giris_tab2:
    st.caption("İlk hücreye Excel'den toplu yapıştırma yapabilirsiniz (Tab ayraçlı). Satır eklemek için tablonun altındaki **+** işaretini kullanın.")

    if "manuel_bom_df" not in st.session_state:
        st.session_state.manuel_bom_df = pd.DataFrame(
            [{"MPN": "", "Manufacturer": "", "Description": "", "Qty": 1, "RefDes": ""} for _ in range(3)]
        )

    duzenlenmis_df = st.data_editor(
        st.session_state.manuel_bom_df,
        num_rows="dynamic",
        width='stretch',
        key="manuel_veri_editoru",
        column_config={
            "MPN": st.column_config.TextColumn("MPN *", required=True, help="Üretici parça numarası"),
            "Manufacturer": st.column_config.TextColumn("Marka", help="Üretici / marka adı"),
            "Description": st.column_config.TextColumn("Açıklama", help="Parça açıklaması"),
            "Qty": st.column_config.NumberColumn("Adet *", min_value=1, step=1, required=True, help="Kart başına kullanılan adet"),
            "RefDes": st.column_config.TextColumn("Referans", help="Örn. R1, R2, U1"),
        },
    )
    st.session_state.manuel_bom_df = duzenlenmis_df

    if st.button("🚀 Bu Listeyi Kullan", key="manuel_kullan_btn"):
        gecerli_satirlar = duzenlenmis_df[
            duzenlenmis_df["MPN"].astype(str).str.strip().ne("") & duzenlenmis_df["Qty"].notna()
        ].copy()
        if gecerli_satirlar.empty:
            st.error("En az bir satırda MPN ve Adet doldurulmalı.")
        else:
            if "RefDes" in gecerli_satirlar.columns:
                gecerli_satirlar["RefDes"] = gecerli_satirlar["RefDes"].fillna("")
            if "Manufacturer" in gecerli_satirlar.columns:
                gecerli_satirlar["Manufacturer"] = gecerli_satirlar["Manufacturer"].fillna("")
            if "Description" in gecerli_satirlar.columns:
                gecerli_satirlar["Description"] = gecerli_satirlar["Description"].fillna("")
            st.session_state["aktif_bom_df"] = gecerli_satirlar
            st.success(f"{len(gecerli_satirlar)} satır kaydedildi. Aşağıda analiz sonuçlarını görebilirsiniz.")

# Dosya yüklendiyse onu, yoksa (varsa) manuel girişten kaydedilmiş olanı kullan.
if df_kaynagi is not None:
    st.session_state["aktif_bom_df"] = df_kaynagi

df = st.session_state.get("aktif_bom_df")

# ------------------------------------------------------------
# ANALİZ AKIŞI
# ------------------------------------------------------------
if df is not None and not df.empty:
    eksik_sutunlar = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if eksik_sutunlar:
        st.error(f"Eksik sütunlar: {', '.join(eksik_sutunlar)}.")
        st.stop()

    konsolide_df = (
        df.groupby(["MPN", "Manufacturer", "Description"], dropna=False)
        .agg({"Qty": "sum", "RefDes": lambda x: ", ".join(map(str, x))})
        .reset_index()
    )

    bolum_basligi("ADIM 02", "Tedarik Zinciri Taraması", "Nexar üzerinden onlarca tedarikçide fiyat, stok ve yaşam döngüsü sorgulanıyor.")

    with st.spinner("Küresel tedarik zinciri taranıyor..."):
        nexar_map = toplu_nexar_sorgula(konsolide_df["MPN"].tolist())

        def satir_zenginlestir(row):
            gereken = int(row["Qty"]) * int(uretim_adedi)
            m = parca_metriklerini_hesapla(row["MPN"], gereken, nexar_map)
            return pd.Series([
                m["Durum"], m["Yaşam Döngüsü"], m["Tedarikçi Sayısı"],
                m["Gereken Miktar"], m["Toplam Stok"], m["Stok Karşılama"],
                m["Uygun En Ucuz Tedarikçi"], m["Birim Fiyat"], m["Toplam Maliyet"],
                m["Alternatif Sayısı"], m["Risk Skoru"],
            ])

        sutunlar = [
            "Durum", "Yaşam Döngüsü", "Tedarikçi Sayısı", "Gereken Miktar",
            "Toplam Stok", "Stok Karşılama", "Uygun En Ucuz Tedarikçi",
            "Birim Fiyat", "Toplam Maliyet", "Alternatif Sayısı", "Risk Skoru",
        ]
        konsolide_df[sutunlar] = konsolide_df.apply(satir_zenginlestir, axis=1)

    # --- KPI PANELİ ---
    toplam_usd = konsolide_df["Toplam Maliyet"].apply(
        lambda x: float(x.replace("USD", "").strip()) if isinstance(x, str) and "USD" in x else 0.0
    ).sum()
    karsilanamayan = int((konsolide_df["Uygun En Ucuz Tedarikçi"] == "Kritik (Tek Kaynak Yetersiz)").sum())
    yuksek_riskli_df = konsolide_df[konsolide_df["Risk Skoru"] >= 70]

    st.markdown(
        f"""
        <div class="cb-metric-row">
            {metrik_karti(f"{uretim_adedi} Adet Üretim İçin Tahmini Maliyet", f"${toplam_usd:,.2f}", "copper")}
            {metrik_karti("Toplam Benzersiz Parça", str(len(konsolide_df)), "cyan")}
            {metrik_karti("Tek Kaynaktan Karşılanamayan", str(karsilanamayan), "danger" if karsilanamayan else "cyan")}
            {metrik_karti("Yüksek Riskli Parça (≥70)", str(len(yuksek_riskli_df)), "danger" if len(yuksek_riskli_df) else "copper")}
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not yuksek_riskli_df.empty:
        st.error(
            f"{len(yuksek_riskli_df)} parça, {uretim_adedi} adetlik üretim planı için "
            "yetersiz/bulunamayan stok ya da Obsolete/EOL riski taşıyor."
        )

    # ------------------------------------------------------------
    # SEKMELİ ANALİZ GÖRÜNÜMÜ
    # ------------------------------------------------------------
    tab_bom, tab_parca, tab_konsol, tab_ai = st.tabs(
        ["📑 BOM Analiz Tablosu", "🔍 Parça Analizi & Alternatifler", "🔄 Akıllı Konsolidasyon", "🤖 AI Asistan"]
    )

    with tab_bom:
        st.dataframe(stil_uygula(konsolide_df), width='stretch', height=460)
        st.download_button(
            "⬇ Analizi Excel Olarak İndir",
            data=excele_donustur(konsolide_df),
            file_name="circuitbom_analiz.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with tab_parca:
        secilen_mpn = st.selectbox("Analiz edilecek parçayı seçin:", konsolide_df["MPN"].tolist(), key="parca_secimi")
        if secilen_mpn:
            sonuc = nexar_map.get(secilen_mpn, {})
            secili_satir = konsolide_df[konsolide_df["MPN"] == secilen_mpn].iloc[0]

            c1, c2, c3 = st.columns(3)
            c1.info(f"**Üretici:** {secili_satir['Manufacturer']}\n\n**Açıklama:** {secili_satir['Description']}")
            with c2:
                st.markdown(f"**Yaşam Döngüsü**  \n{yasam_rozeti(secili_satir['Yaşam Döngüsü'])}", unsafe_allow_html=True)
            c3.metric("Risk Skoru", secili_satir["Risk Skoru"])

            st.markdown("#### 🛒 Küresel Tedarikçi Teklifleri")
            teklifler = sonuc.get("teklifler", [])
            if teklifler:
                teklif_df = pd.DataFrame(
                    teklifler,
                    columns=["Tedarikçi", "Birim Fiyat", "Para Birimi", "Min. Sipariş (MOQ)", "Stok Adedi", "Satın Alma Linki"],
                )
                st.dataframe(teklif_df, width='stretch')
            else:
                st.warning("Bu parça için aktif tedarikçi teklifi bulunamadı.")

            st.markdown("#### 🔄 Uyumlu Alternatifler — BOM Analiziyle Aynı Zenginlikte Veri")
            alternatifler = sonuc.get("alternatifler", [])
            if not alternatifler:
                st.info("Bu parça için sistemde doğrudan bir alternatif kaydedilmemiş.")
            else:
                alt_mpnler = [a["mpn"] for a in alternatifler if a["mpn"] != "-"]
                with st.spinner("Alternatiflerin tedarik verisi çekiliyor..."):
                    alt_nexar_map = toplu_nexar_sorgula(alt_mpnler)

                gereken_miktar_alt = int(secili_satir["Gereken Miktar"])
                isim_map = {a["mpn"]: a["isim"] for a in alternatifler}
                link_map = {a["mpn"]: a["link"] for a in alternatifler}

                alt_satirlari = []
                for alt_mpn in alt_mpnler:
                    m = parca_metriklerini_hesapla(alt_mpn, gereken_miktar_alt, alt_nexar_map)
                    alt_satirlari.append({
                        "MPN": alt_mpn,
                        "Ürün Adı": isim_map.get(alt_mpn, "-"),
                        "Durum": m["Durum"],
                        "Yaşam Döngüsü": m["Yaşam Döngüsü"],
                        "Toplam Stok": m["Toplam Stok"],
                        "Uygun En Ucuz Tedarikçi": m["Uygun En Ucuz Tedarikçi"],
                        "Birim Fiyat": m["Birim Fiyat"],
                        "Risk Skoru": m["Risk Skoru"],
                        "Link": link_map.get(alt_mpn, ""),
                    })
                alt_df = pd.DataFrame(alt_satirlari)
                st.dataframe(stil_uygula(alt_df), width='stretch')
                st.caption("Bu tablo, ana BOM analiziyle birebir aynı hesaplama mantığını (risk skoru, stok karşılaştırması) kullanır.")

    with tab_konsol:
        st.write(
            "BOM listenizdeki aynı işlevi gören ancak farklı üreticilerden sağlanan "
            "parçaları tespit edip tek bir kalemde birleştirerek maliyeti ve sipariş "
            "karmaşıklığını azaltın."
        )
        konsolide_df["_normalize"] = konsolide_df["Description"].astype(str).str.strip().str.lower()
        konsolidasyon_adaylari = konsolide_df[konsolide_df.groupby("_normalize")["MPN"].transform("nunique") > 1]

        if konsolidasyon_adaylari.empty:
            st.success("BOM listenizde optimize edilebilecek kopya/benzer parça bulunamadı.")
        else:
            for _, grup in konsolidasyon_adaylari.groupby("_normalize"):
                with st.expander(f"📌 {grup.iloc[0]['Description']} ({len(grup)} varyasyon)"):
                    st.dataframe(
                        stil_uygula(grup[["MPN", "Manufacturer", "Birim Fiyat", "Toplam Stok", "Yaşam Döngüsü", "Risk Skoru"]]),
                        width='stretch',
                    )

                    def _fiyat_sayiya_cevir(deger):
                        try:
                            return float(str(deger).split()[0])
                        except (ValueError, IndexError):
                            return float("inf")

                    grup_sirali = grup.copy()
                    grup_sirali["_fiyat_sayi"] = grup_sirali["Birim Fiyat"].apply(_fiyat_sayiya_cevir)
                    grup_sirali = grup_sirali.sort_values(by=["Risk Skoru", "_fiyat_sayi"])
                    onerilen = grup_sirali.iloc[0]
                    digerleri = grup_sirali.iloc[1:]

                    st.success(
                        f"**Sistem Önerisi:** Tüm alımları **{onerilen['MPN']}** ({onerilen['Manufacturer']}) "
                        f"üzerinden yapın. Gerekçe: Risk Skoru {onerilen['Risk Skoru']} (en düşük), "
                        f"Fiyat {onerilen['Birim Fiyat']}, Yaşam Döngüsü: {onerilen['Yaşam Döngüsü']}."
                    )
                    if not digerleri.empty:
                        st.caption(f"Yerine geçecek parçalar: {', '.join(digerleri['MPN'].tolist())}")

        konsolide_df = konsolide_df.drop(columns=["_normalize"])

    with tab_ai:
        st.write("BOM listenizdeki verilerle ilgili serbest formatta sorular sorun.")
        st.caption("Örn: *'Hangi parçaların EOL riski var?'* veya *'En pahalı 3 komponent hangisi?'*")

        soru = st.text_input(
            "Asistana sorun:", placeholder="Bu projede toplam kaç adet kondansatör kullanılıyor?",
            label_visibility="collapsed",
        )

        if soru:
            if not GEMINI_API_KEY:
                st.error("🔑 AI özelliklerini kullanmak için kenar çubuğundan / secrets.toml'dan Gemini API Key girmelisiniz.")
            else:
                # TOKEN TASARRUFU: Çok büyük BOM'larda tüm tabloyu göndermek yerine
                # bir eşik koyup aşımda kullanıcıyı bilgilendiriyoruz.
                gonderilecek_df = konsolide_df
                kesildi_mi = False
                if len(konsolide_df) > 300:
                    gonderilecek_df = konsolide_df.head(300)
                    kesildi_mi = True

                bom_metni = gonderilecek_df.to_csv(index=False)

                if kesildi_mi:
                    st.caption(
                        f"Not: BOM {len(konsolide_df)} satır olduğu için, token tasarrufu "
                        "amacıyla yapay zekaya yalnızca ilk 300 satır gönderildi."
                    )

                with st.spinner("🤖 Analiz ediliyor..."):
                    try:
                        yanit = gemini_yanit_al(soru, bom_metni, int(uretim_adedi))
                        st.info(yanit)
                    except Exception as e:
                        st.error(f"Yapay zeka servisiyle iletişim kurulamadı: {e}")
elif df is not None and df.empty:
    st.warning("Girdiğiniz liste boş görünüyor. Lütfen en az bir satır ekleyin.")