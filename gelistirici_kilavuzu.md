# CircuitBOM Geliştirici Kılavuzu
### Karşılaşılan Teknik Hatalar, Kök Nedenleri ve "Bir Daha Yapma" Listesi

Bu doküman, bu projenin geliştirilmesi sırasında karşılaşılan gerçek hataları, neden oluştuklarını, nasıl çözüldüklerini ve bir sonraki geliştiricinin (veya gelecekteki sizin) aynı hataya tekrar düşmemesi için nelere dikkat etmesi gerektiğini anlatır. Hatalar, oluş sırasına yakın şekilde kategorilere ayrılmıştır.

> **Not (kaynak birleştirme):** Bu kılavuz iki kaynaktan birleştirilmiştir. Birleştirme sırasında, mevcut kod tabanımızın gerçek geçmişiyle **çelişen veya ona ait olmayan** birkaç madde tespit edildi (SSL bypass kararı, Mouser API detayları, SQLite önbellek). Bu maddeler silinmedi, ama net şekilde işaretlendi — "şu an geçerli değil" veya "çelişki" notuyla. Amaç, kılavuzun güvenilir tek doğruluk kaynağı (single source of truth) olarak kalmasını sağlamak.

---

## 1. Streamlit Ortam ve Yapılandırma Hataları

### 1.1 `st.set_page_config` sırası
**Hata:** `set_page_config` başka bir `st.` komutundan sonra çağrılırsa Streamlit hata fırlatır.
**Kural:** `st.set_page_config(...)` dosyanın **en başında**, herhangi bir başka `st.` çağrısından (hatta bir `st.write` veya yorum amaçlı bir uyarı bile) önce olmalı.
**Yapmayın:** Sayfa ayarını dosyanın ortasına veya import bloğunun altına, başka mantıktan sonra koymayın.

### 1.2 `secrets.toml` dosyası yoksa çökme
**Hata:**
```
streamlit.errors.StreamlitSecretNotFoundError: No secrets found...
```
**Kök neden:** `st.secrets.get("KEY")`, normal bir Python sözlüğünün `.get()` metodu gibi davranmıyor — `secrets.toml` dosyası hiç yoksa `None` döndürmek yerine **exception fırlatıyor**.
**Çözüm:** Her zaman `try/except` ile sarmalayın:
```python
def secret_veya_env(anahtar: str) -> str:
    try:
        deger = st.secrets.get(anahtar)
        if deger:
            return deger
    except Exception:
        pass
    return os.environ.get(anahtar, "")
```
**Yapmayın:** `st.secrets["KEY"]` veya çıplak `st.secrets.get("KEY")` çağrısını hiçbir koruma olmadan kullanmayın — geliştirme ortamında dosya olmayabilir.

---

## 2. Üçüncü Parti API Entegrasyon Hataları

### 2.1 Dokümantasyon örneğindeki yer tutucuyu (placeholder) olduğu gibi kopyalamak
**Hata:** İlk Mouser entegrasyonunda `"partSearchOptions": "string"` gönderiliyordu.
**Kök neden:** Bu değer, Swagger/API dokümantasyonundaki **örnek** alan değeriydi ("buraya bir string yazın" anlamında), gerçek bir seçenek değildi. Doğrudan kopyala-yapıştır yapılmıştı.
**Yapmayın:** API dokümantasyonundan örnek bir istek gövdesi kopyalarken, her alanın **gerçek bir değer mi yoksa yer tutucu mu** olduğunu mutlaka ayırt edin. Şüpheliyse boş bırakın veya dokümantasyonun "kabul edilen değerler" bölümüne bakın.

### 2.2 Yanlış/güncel olmayan model veya API adı varsayımı
**Hata:** `GEMINI_MODEL_NAME` birkaç kez yanlış veya artık desteklenmeyen bir model adına ayarlandı (`gemini-2.5-flash`, ardından bir revizyon sırasında yanlışlıkla `gemini-1.5-pro`'ya geri döndü), sürekli `404 model not found` hatalarına yol açtı.
**Kök neden:** Sağlayıcılar (Google, OpenAI, Anthropic vb.) model adlarını zamanla değiştirip eskilerini emekliye ayırıyor. Bir model adını "bilinen doğru cevap" olarak ezbere yazmak, zamanla yanlış hale geliyor.
**Çözüm:** API'nin kendisi 404 hatasında genelde "bunun yerine şu modeli kullanın" diye açıkça söylüyor — **tam olarak o ismi** kullanın, kendi bildiğinizi zannettiğiniz eski ismi ısrarla kullanmayın.
**Yapmayın:**
- Model adını kod içine yayılmış şekilde birden çok yerde tekrar yazmayın — tek bir sabitte (`GEMINI_MODEL_NAME`) tutun ki güncelleme tek satırdan yapılabilsin.
- Bir dosyayı başka bir kaynaktan (örn. bir meslektaşın gönderdiği "düzeltilmiş" sürüm) alıp doğrudan üzerine yazmadan önce, daha önce düzelttiğiniz değerlerin (model adı gibi) yanlışlıkla eskiye dönmediğini kontrol etmeden **birleştirmeyin**.

### 2.3 OAuth2 `scope` parametresini gereksiz yere göndermek
**Hata:** `{"error": "invalid_scope"}`
**Kök neden:** Nexar API'sinde uygulamanın kapsamı (scope) zaten panelde Client ID/Secret'a bağlıydı; token isteğine ayrıca `scope=supply` gönderilmesi çakışmaya (ve reddedilmeye) yol açtı.
**Çözüm:** Parametreyi tamamen kaldırmak.
**Yapmayın:** Bir OAuth2 sağlayıcısının dokümantasyonundaki **genel** örnek isteği, sağlayıcıya özgü panel ayarlarını kontrol etmeden birebir kopyalamayın. Bazı sağlayıcılarda scope, uygulama oluşturma anında sabitlenir ve istekte tekrar belirtilmesi beklenmez.

### 2.4 GraphQL'de HTTP 200 ≠ Hata Yok
**Hata:** Nexar kota aşımını (`"You have exceeded your part limit of 0"`) bildiriyordu ama HTTP durum kodu yine 200'dü. Kod bu durumu kontrol etmediği için, kota hatası sessizce **"parça bulunamadı"** olarak yorumlanıyordu — kullanıcıya yanlış bilgi gösteriliyordu.
**Kök neden:** GraphQL protokolünde hata bilgisi HTTP durum kodunda değil, yanıt gövdesindeki `"errors"` alanında taşınır. REST API alışkanlığıyla sadece `status_code == 200` kontrolü yeterli sanılmıştı.
**Çözüm:**
```python
data = resp.json()
veri = data.get("data") or {}
if data.get("errors"):
    hata_metni = str(data["errors"])[:250]
    if not veri:
        return {mpn: _bos_parca_sonucu(hata_metni) for mpn in mpn_grubu}
```
**Yapmayın:** GraphQL tabanlı bir API'de asla sadece HTTP durum koduna güvenmeyin. `data["errors"]` alanını **her yanıtta** kontrol edin, HTTP 200 dönse bile.

### 2.5 [Referans / Şu An Kullanılmıyor] Mouser API'ye özel notlar
> **Uyarı:** Proje, Mouser entegrasyonunu **tamamen kaldırıp** Nexar'a geçmiştir (bkz. "Mouserı tamamen kaldıralım" kararı). Aşağıdaki iki madde, mevcut kod tabanında **karşılığı olmayan**, muhtemelen ayrı/eski bir Mouser tabanlı daldan gelen notlardır. Mouser API'sine ileride tekrar dönülürse referans olması için saklanmıştır.

**Boş sonuç (0 result) sorunu:** `SearchByKeywordRequest` uç noktası net MPN aramalarında yetersiz kalabiliyor; tam parça numarası aramaları için `v2/search/partnumber` uç noktası ve `SearchByPartRequest` şeması kullanılması öneriliyor:
```python
url = f"https://api.mouser.com/api/v2/search/partnumber?apiKey={api_key}"
payload = {"SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": ""}}
```
**Stokların sürekli 0 görünmesi:** Mouser yanıtında stok bilgisi tek bir formatta gelmeyebiliyor (`Availability` dille karışık metin, `AvailabilityInStock` düz sayısal string). Öneri: önce `AvailabilityInStock`/`FactoryStock` okunmalı, bulunamazsa `Availability` metninden regex ile sayı ayıklanmalı, ayrıca `AvailabilityOnOrder` (yoldaki sipariş) listesi de toplama dahil edilmeli.

---

## 2A. Veri Erişiminde None/KeyError Güvenliği (Genel Kural)

### 2A.1 İç içe sözlüklerde ham köşeli parantez erişimi
**Hata:** `TypeError: 'NoneType' object is not subscriptable` ve `KeyError`.
**Kök neden:** Bir API'den veri gelmediğinde (`bulundu: False` veya bir alan `None`) sonuç, iç içe sözlük yapıları içinde köşeli parantezle doğrudan okunmaya çalışılıyordu, örn: `sonuc["en_iyi"]["Aday"]`. Eğer `sonuc["en_iyi"]` anahtarı mevcut ama değeri açıkça `None` ise, `.get("en_iyi", {})` bile **yetmez** — çünkü anahtar var, sadece değeri `None`; varsayılan değer devreye girmez.
**Standart Çözüm:** Zincirleme fallback kalıbı:
```python
# DOĞRU STANDART
en_iyi_aday = (sonuc.get("en_iyi") or {}).get("Aday", "-")
mevcut_skor = (sonuc.get("mevcut") or {}).get("Karar Skoru", 0)
```
`(x.get(...) or {})` kalıbı, hem anahtar hiç yoksa hem de anahtar var ama değeri `None`/boş ise güvenli çalışır.
**Yapmayın:** İç içe sözlük yapılarında asla ham `dict[...]` erişimi kullanmayın; `dict.get(...)` tek başına da yeterli değildir — `or {}` ile birlikte kullanılmalıdır. Bu projede `_bos_parca_sonucu()` şablonu zaten her zaman sabit anahtarlarla dönmeye özen gösteriyor, ama tüketici (consumer) taraftaki kodun da savunmacı yazılması gerekir.

---

## 3. Performans / Token Verimliliği Hataları

### 3.1 Her parça için ayrı HTTP isteği (N+1 problemi)
**Hata:** İlk sürümde her MPN için ayrı bir `requests.post()` çağrısı yapılıyordu. 50 parçalık bir BOM = 50 ayrı ağ isteği, çok yavaş.
**Çözüm:** GraphQL'in **alias** özelliğiyle, birden çok parçayı tek istekte birleştirmek:
```python
alan_bloklari = "\n".join(
    f'p{i}: supSearchMpn(q: $mpn{i}, limit: 1) {{ ... }}'
    for i in range(len(mpn_grubu))
)
```
15 parça → 1 istek. Ayrıca bu grupları `ThreadPoolExecutor` ile paralel gönderdik.
**Yapmayın:** Bir listedeki her öğe için döngü içinde ayrı ayrı dış API çağrısı yapmayın. API toplu sorgu (batch/alias) destekliyorsa mutlaka kullanın.

### 3.2 Her yeniden çalıştırmada aynı veriyi tekrar çekmek
**Kök neden:** Streamlit, kullanıcının her etkileşiminde (bir metin kutusuna yazması dahil) **tüm script'i baştan çalıştırır**. Cache olmadan, kullanıcı sadece bir soru sorduğunda bile tüm BOM tekrar API'ye sorulur.
**Çözüm:** `@st.cache_data(ttl=...)` dekoratörü — hem token alma hem parça sorgusu fonksiyonlarına eklendi.
**Yapmayın:** Dış API'ye giden bir fonksiyonu, tekrar çağrılma sıklığını düşünmeden cache'siz bırakmayın. Özellikle Streamlit gibi "her etkileşimde yeniden çalışan" framework'lerde bu kritik.

### 3.3 LLM'e her seferinde tüm veriyi tekrar göndermek
**Kök neden:** Aynı soru tekrar sorulduğunda veya sayfa yeniden çalıştığında, aynı BOM verisi + aynı soru tekrar tekrar Gemini'ye gönderiliyordu — gereksiz token tüketimi.
**Çözüm:** `gemini_yanit_al(soru, bom_metni, uretim_adedi)` fonksiyonu `@st.cache_data` ile önbelleklendi; büyük BOM'larda (300+ satır) gönderilen veri miktarına üst sınır konuldu.
**Yapmayın:** LLM'e gönderilen prompt'u/veriyi cache'lemeden bırakmayın, özellikle kullanıcı arayüzünün her etkileşimde yeniden çalıştığı ortamlarda.

### 3.4 Tek kullanımlık bağlantılar
**Çözüm:** `requests.Session()` kullanarak TCP/TLS bağlantısının tekrar tekrar kurulması yerine yeniden kullanılması sağlandı.
**Yapmayın:** Aynı sunucuya onlarca istek atacaksanız, her seferinde `requests.post(...)` ile yeni bir bağlantı açmayın; bir `Session` nesnesi üzerinden gidin.

---

## 4. Hata Yönetimi ve Kod Kalitesi Hataları

### 4.1 Çıplak (bare) `except:` kullanımı — hata bilgisini yok etmek
**Hata:** Bir ara revizyonda tüm hata yakalama blokları şu hale getirilmişti:
```python
except: return ""
except: pass
```
**Neden kötü:** Bu satırlar, **gerçek sorunun ne olduğunu tamamen gizliyor**. `invalid_scope` ve `part limit of 0` hatalarının ikisi de, ancak **detaylı hata mesajını terminale yazdırdığımız** için teşhis edilebildi:
```python
print(f"[Nexar Token Hatası] status={resp.status_code} body={resp.text[:300]}")
```
**Yapmayın:**
- Asla çıplak `except:` kullanmayın — en azından `except Exception as e:` yazın.
- Hata mesajını sessizce yutmayın; en az terminale/log dosyasına yazdırın. Kullanıcıya gösterilen mesaj sade olabilir ama geliştirici için detaylı log her zaman ayrı tutulmalı.
- Bir hata bloğunu "temizlerken" (kod sadeleştirirken) yanlışlıkla teşhis için hayati olan debug çıktılarını silmeyin.

### 4.2 Aynı hesaplama mantığının iki yerde tekrar yazılması
**Hata:** Ana BOM tablosu ve "Parça Alternatifleri" tablosu başlangıçta **ayrı ayrı** kod bloklarında hesaplanıyordu. Zamanla ikisi birbirinden sapmaya başladı (biri risk skorunu hesaplarken diğeri hesaplamıyordu, biri stok bilgisini gösterirken diğeri göstermiyordu).
**Çözüm:** Tek bir ortak fonksiyon (`parca_metriklerini_hesapla`) yazıp her iki tablo için de bu fonksiyonu kullanmak.
**Yapmayın:** "Aynı hesaplamayı iki farklı yerde biraz farklı ihtiyaçlar için tekrar yazayım" demeyin. İlk kopyala-yapıştırın anda, ortak bir fonksiyona çıkarın — aksi halde iki kopya zamanla tutarsızlaşır ve hangisinin doğru olduğunu anlamak zorlaşır.

### 4.3 Deneysel bir değişikliği geri alırken eksik temizlik
**Hata:** SSL doğrulamasını geçici olarak bypass eden bir değişiklik eklenip sonra geri alınırken, `SSL_DOGRULAMAYI_ATLA` değişkenine yapılan **tüm** referanslar (iki ayrı `requests.post` çağrısındaki `verify=not SSL_DOGRULAMAYI_ATLA` parametreleri) silinmemişti. Bu, değişken tanımsız kaldığı için kod çalıştırıldığında `NameError` ile çökecekti.
**Yapmayın:** Bir değişikliği geri alırken sadece "ana" satırı silip bırakmayın. `grep`/arama ile o değişkenin/fonksiyonun **tüm** kullanıldığı yerleri bulup hepsini temizleyin. Geri alma işleminden sonra mutlaka `python -m py_compile` (veya eşdeğeri) ile sözdizimi/tanım kontrolü yapın.

### 4.4 Pandas `groupby` varsayılan davranışı: sessiz veri kaybı
**Hata:** `df.groupby(["MPN", "Manufacturer", "Description"])` çağrısı, bu sütunlardan herhangi birinde boş (NaN) hücre olan satırları **varsayılan olarak sessizce siler** (`dropna=True` varsayılan).
**Çözüm:** `dropna=False` parametresini açıkça eklemek.
**Yapmayın:** `groupby` kullanırken varsayılan `dropna` davranışını unutmayın — özellikle kullanıcı tarafından yüklenen, eksik hücreler içerebilecek dosyalarla çalışırken.

---

## 5. Kütüphane/Framework Sürüm Uyumluluğu Hataları

### 5.1 `Styler.applymap` kaldırıldı
**Hata:** `AttributeError: 'Styler' object has no attribute 'applymap'`
**Kök neden:** Yeni pandas sürümlerinde `Styler.applymap` kaldırılıp yerine `Styler.map` getirildi.
**Çözüm:** İkisini de deneyen bir `try/except AttributeError` bloğu yazarak hangi pandas sürümü kuruluysa onunla çalışmasını sağlamak.
**Yapmayın:** Kütüphanelerin "kesin böyle kullanılır" bildiğiniz API'lerinin, zaman içinde deprecate edilip kaldırılabileceğini unutmayın. Kritik bir API çağrısını, mümkünse geriye dönük uyumlu (hem eski hem yeni sürümü deneyen) şekilde yazın.

### 5.2 `use_container_width` kullanımdan kaldırılıyor
**Hata:** Streamlit terminalde `FutureWarning: Please replace use_container_width with width`.
**Çözüm:** `width='stretch'` / `width='content'` kullanımına geçildi.
**Yapmayın:** Terminaldeki `FutureWarning` mesajlarını görmezden gelmeyin — bunlar gelecekte kırılacak (breaking change) bir API kullandığınızı önceden haber verir.

### 5.3 `google.generativeai` paketinin kullanımdan kaldırılması
**Durum:** Google, `google.generativeai` paketini artık desteklemediğini duyurdu, yerine `google.genai` kullanılması öneriliyor.
**Yapmayın:** Bu tür bir "artık desteklenmiyor" uyarısını görüp de göz ardı etmeyin; kısa vadede kod çalışmaya devam etse bile, orta vadede bir geçiş planı (yeni pakete geçiş) yapılmalı.

---

## 6. Yerel Geliştirme Ortamı (Windows) Hataları

### 6.1 Yanlış dosyayı düzenleyip çalıştırma
**Hata:** Aynı klasörde hem eski (`bom_analiz_araci.py`) hem yeni isimli (`app.py`) dosya bulunuyordu; kullanıcı güncellemeleri birine yaparken terminal diğerini çalıştırıyordu — "değişiklik yapıyorum ama hiçbir şey değişmiyor" kafa karışıklığına yol açtı.
**Çözüm:** `Select-String -Path app.py -Pattern "..."` gibi bir komutla, çalışan dosyanın **gerçekten** beklenen içeriğe sahip olup olmadığı terminalden doğrudan doğrulandı.
**Yapmayın:**
- Aynı işlevi gören birden fazla dosyayı aynı klasörde bırakmayın; kafa karışıklığını önlemek için eskisini silin veya yeniden adlandırın.
- "Dosyayı güncelledim ama hata hâlâ aynı" durumunda, önce **gerçekten doğru dosyayı düzenleyip düzenlemediğinizi** terminalden doğrulayın — editördeki görünüm yanıltıcı olabilir (kaydedilmemiş olabilir, yanlış sekme olabilir).

### 6.2 `.ps1` dosyasına çift tıklamak onu çalıştırmaz
**Hata:** PowerShell betiğine çift tıklandığında Not Defteri'nde açıldı, çalışmadı.
**Kök neden:** Windows, güvenlik nedeniyle `.ps1` dosyalarının çift tıklamayla doğrudan yürütülmesine varsayılan olarak izin vermez.
**Çözüm:** Hedefi `powershell.exe -ExecutionPolicy Bypass -File "yol\dosya.ps1"` olan bir **kısayol** oluşturmak.
**Yapmayın:** Son kullanıcıya "bu dosyaya çift tıkla" demeden önce, işletim sisteminin o dosya tipini varsayılan olarak nasıl ele aldığını kontrol edin.

### 6.3 Uzantısı değiştirilmiş dosya, gerçek formata dönüştürülmüş sayılmaz
**Hata:** Kullanıcının yüklediği `.ico` dosyası, gerçekte hâlâ ham bir PNG'ydi (sadece adı değiştirilmişti) — Windows'un "Simgeyi Değiştir" penceresi bu yüzden dosyayı listede göstermiyordu.
**Çözüm:** Dosyayı `Pillow` (`PIL`) kütüphanesiyle gerçekten çok boyutlu bir ICO'ya dönüştürmek.
**Yapmayın:** Bir dosyanın uzantısını değiştirmenin, onun **gerçek ikili formatını** değiştirdiğini varsaymayın. Şüpheliyseniz `file <dosya>` (Linux/Mac) veya bir hex/format denetleyicisiyle gerçek içeriği kontrol edin.

### 6.4 Kurumsal/okul ağlarında SSL sertifika doğrulama hatası
**Hata:** `SSLCertVerificationError: unable to get local issuer certificate`
**Kök neden:** Ağdaki bir güvenlik duvarı/proxy HTTPS trafiğini "araya girip inceliyor" (SSL inspection); bu sistemin sertifikası Windows'ta güvenilir sayılsa da Python'un kendi sertifika listesinde (certifi) yok.
**Değerlendirilen çözümler ve kararlar:**
1. `pip install pip-system-certs` / `truststore` — Python'u Windows'un sertifika deposunu kullanmaya yönlendirir. **Önerilen, güvenli çözüm.**
2. SSL doğrulamasını kod içinde tamamen kapatmak (`verify=False`) — **sadece son çare**, güvenlik riski taşır (ortadaki adam saldırılarına karşı savunmasız), projede bu yaklaşım denenip **sonradan tamamen geri alınmıştır**.
**Yapmayın:** SSL sertifika doğrulamasını "hızlı çözüm" diye kalıcı olarak kapatmayın. Bu, sadece geçici/kontrollü bir ortamda ve bilinçli bir trade-off olarak, açıkça yorumlanarak yapılmalı — ve iş bittiğinde mutlaka geri alınmalıdır.

---

## 7. Arayüz / Kullanılabilirlik Hataları

### 7.1 Koyu temada okunmayan yazı rengi
**Hata:** Risk skoru ve yaşam döngüsü hücreleri açık renkte (`#ffcccc`, `#d4edda` vb.) arka planla vurgulanıyordu, ama yazı rengi Streamlit'in koyu tema varsayılanından (beyaza yakın) miras alınıyordu — açık arka plan üzerinde neredeyse görünmez oluyordu.
**Çözüm:** Renklendirme fonksiyonlarına açıkça `color: black` eklemek.
**Yapmayın:** Özel arka plan rengi veren bir stil tanımlarken, üzerindeki metnin **hangi temada** (açık/koyu) okunur kalacağını kontrol etmeden bırakmayın. Kontrast her zaman açıkça test edilmeli.

---

## 8. Genel "Bir Daha Yapma" Kontrol Listesi

Yeni bir özellik eklerken veya bir hatayı düzeltirken şu listeyi gözden geçirin:

- [ ] Dış bir API'ye yeni bir istek ekliyorsam: HTTP durum kodunu **ve** yanıt gövdesindeki hata alanını (`errors`, `error`, vb.) kontrol ediyor muyum?
- [ ] Bir hata bloğu yazarken: gerçek hata mesajını en az terminale/log'a yazdırıyor muyum, yoksa sessizce mi yutuyorum?
- [ ] Aynı hesaplamayı iki farklı yerde mi yazıyorum? Ortak bir fonksiyona çıkarmalı mıyım?
- [ ] Döngü içinde dış API çağrısı mı yapıyorum? Toplu (batch) sorgu mümkün mü?
- [ ] Sık tekrarlanacak bir fonksiyon mu bu? Cache'lemem gerekir mi?
- [ ] Bir dokümantasyon örneğini kopyaladıysam: örnekteki her değerin gerçek mi yoksa yer tutucu mu olduğunu kontrol ettim mi?
- [ ] Bir model/API adı yazdıysam: bunun hâlâ güncel olduğundan emin miyim, yoksa "bildiğimi zannettiğim" eski bir isim mi?
- [ ] Deneysel bir değişikliği geri alıyorsam: o değişikliğe ait **tüm** referansları (değişken, import, parametre) temizledim mi?
- [ ] Kütüphane bir uyarı (`FutureWarning`/`DeprecationWarning`) veriyorsa, görmezden gelmek yerine not aldım mı?
- [ ] Renkli/vurgulu bir arayüz elemanı ekliyorsam: hem açık hem koyu temada okunabilir mi?
- [ ] Güvenliği zayıflatan bir "hızlı çözüm" (SSL bypass, sabit kodlanmış anahtar, vb.) uyguladıysam: bunu geçici olarak açıkça işaretledim mi ve iş bitince geri almayı planladım mı?
