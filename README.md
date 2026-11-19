# SABAH Meetbot

SABAH.OS calendar-ında **Notetaker düyməsi açılmış** Google Meet görüşlərinə qoşulan, danışılanları yığan
və sonda **Claude** ilə **ingiliscə** xülasə hazırlayan, öz serverimizdə işləyən bot.
Görüşlər Azərbaycan, rus, ingilis dilində və ya bu üç dilin qarışığında ola bilər.
Bir meetbot həm `sabahos.com`, həm `dev.sabahos.com` üçün işləyir. Dev üçün ayrıca host lazım deyil.

Bot açıq mənbəli [Attendee](https://github.com/attendee-labs/attendee) mühərriki üzərində qurulub
(dəyişdirilmədən, ELv2 lisenziyası ilə). Bu repo onun üstünə SABAH-a xas hissəni əlavə edir:
SABAH.OS inteqrasiyası, transkriptin saxlanması, Claude xülasəsi, e-poçt, API, admin panel.

```
 sabahos.com backend ──┐ MEETBOT_TOKEN_PROD     dev.sabahos.com backend ──┐ MEETBOT_TOKEN_DEV
                       ▼                                                  ▼
               POST /api/events/sync/  (Notetaker düyməsi açılan/dəyişən/silinən tədbirlər)
               GET  /api/events/<id>/  (botun statusu + qeydlər → tədbirin popup-ı)
 ┌───────────────── meetbot (meetbot.sabahos.com) ──────────────────┐
 │ meetbot-web     /api/… /admin/ · /webhooks/attendee/ (daxili)     │
 │ meetbot-worker  başlamağa 30 dəq qalanda bot yarat; bitən görüşü  │
 │                 Claude-a göndər → AWS SES e-poçt                  │
 └───────────┬──────────────────────────────────────▲───────────────┘
             │ POST /api/v1/bots (join_at)          │ imzalı webhook-lar
             ▼                                      │
 ┌──────────────── Attendee engine ─────────────────┴───────────────┐
 │ attendee-worker-bot ×N  headless Chrome → Google Meet            │
 │   hər iştirakçının ayrıca səsi → Gladia (az/ru/en)               │
 │ attendee-app ← attendee.sabahos.com · scheduler · …              │
 └──────────────────────────────────────────────────────────────────┘
   postgres · redis · seaweedfs (daxili S3) — hamısı bir docker compose-da
```

**Necə işləyir:**

1. **Düymə.** SABAH.OS calendar-ında tədbir redaktə olunanda Meet hissəsinə Google Meet linki yapışdırılır
   (və ya "Add Google Meet" ilə yaradılır). Etibarlı link olandan sonra **"Add SABAH Notetaker"** düyməsi görünür.
   Düymə açılıb tədbir yadda saxlananda backend tədbiri meetbot-a göndərir.
2. **Bot planı.** Tədbirin vaxtı, linki dəyişəndə, tədbir silinəndə və ya düymə söndürüləndə backend bunu da dərhal göndərir.
   Meetbot uyğun olaraq botu köçürür və ya ləğv edir. Bundan əlavə, backend hər saat açıq düyməli tədbirləri yenidən
   göndərir ki, buraxılmış dəyişiklik qalmasın. Bot başlamağa 30 dəqiqə qalanda yaradılır və başlanğıcdan 1 dəqiqə əvvəl qoşulur.
   Bir neçə üzvün calendar-ındakı eyni görüş birləşdirilir və ona **bir bot** gedir.
3. **Dev/prod ayrılığı.** Hər SABAH.OS mühiti öz tokeni ilə gəlir (`MEETBOT_TOKEN_PROD`, `MEETBOT_TOKEN_DEV`) və yalnız öz görüşlərini görür.
   Dev-dən gedən e-poçtun mövzusunda `[dev]` yazılır.
4. **Transkripsiya.** Bot Meet-də **hər iştirakçının səsini ayrıca** götürüb Gladia-ya göndərir. Gladia yalnız `az`, `ru`, `en`
   arasında tanıyır və danışan dil dəyişəndə özü də keçir. Danışanın adı Meet-dən gəlir.
5. **Xülasə.** Görüş bitəndə transkript Claude-a gedir. Claude ingiliscə qeydlər yazır: qısa xülasə, əsas məqamlar, qərarlar,
   tapşırıqlar (kim, nə vaxta), açıq suallar, kim nə dedi. Qeydlər təşkilatçıya AWS SES ilə e-poçtla gedir
   və SABAH.OS-da tədbirin popup-ında "Show notes" ilə görünür.

---

## 1. Serverdə açılmalı olanlar

### Subdomenlər (DNS → serverin public IP-si)

| Subdomen | Record | Nə üçün | Məcburi? |
|---|---|---|---|
| `meetbot.sabahos.com` | A | Admin panel (`/admin/`), REST API (`/api/`), e-poçtdakı "full transcript" linki | **Bəli** |
| `attendee.sabahos.com` | A | Attendee dashboard: botun ekran görüntüləri, xətalar, STT açarları | Tövsiyə olunur (IP ilə məhdudlaşdırın). Signed-in bot üçün məcburidir |

**Cloudflare Tunnel istifadə edirsinizsə**, DNS və nginx lazım deyil. Public hostname-lər belə olmalıdır:
`meetbot.sabahos.com` → HTTP `localhost:8100`, `attendee.sabahos.com` → HTTP `localhost:8101`.

### Portlar

| Port | İstiqamət | Kimə açıq | Qeyd |
|---|---|---|---|
| **80/tcp, 443/tcp** | daxil olan | internet | nginx + certbot. Firewall-da yalnız bunları açın |
| 8100/tcp | lokal | yalnız `127.0.0.1` | Meetbot (nginx → `meetbot.sabahos.com`). Firewall-da **açmayın**, sadəcə boş olmalıdır |
| 8101/tcp | lokal | yalnız `127.0.0.1` | Attendee (nginx → `attendee.sabahos.com`). Açmayın, boş olmalıdır |
| 5432/tcp | server daxili | docker şəbəkəsi → SABAH.OS Postgres | Bölmə 4 |
| 443/tcp | çıxan | Google Meet, `api.anthropic.com`, `api.gladia.io`, GitHub, Docker Hub | adətən açıqdır |
| 3478/udp, 19302–19309/udp | çıxan | Google Meet media | Bağlı olsa da işləyir (TCP 443-ə keçir), amma səs keyfiyyəti üçün açıq olsun |
| 587/tcp | çıxan | `email-smtp.eu-central-1.amazonaws.com` | AWS SES (backend ilə eyni) |

Portlar serverdə boşdurmu: `ss -ltnp | grep -E ':(8100|8101)\b'` (boş çıxmalıdır). Doludursa,
`.env`-də `MEETBOT_PORT` / `ATTENDEE_PORT`-u və nginx faylındakı `proxy_pass` portlarını dəyişin.

### Server

x86_64 Linux (Attendee image-i yalnız `linux/amd64`-dür), 4 vCPU, 8 GB RAM, 30 GB disk (eyni anda 2 görüş üçün).
Hər əlavə eyni vaxtlı görüş üçün +1 vCPU və +2 GB RAM lazımdır. Proqramlar: Docker 24+ və `docker compose` plugin, `git`, `openssl`, nginx, certbot.

---

## 2. `.env` — hazırdır

`meetbot/.env` faylı **real dəyərlərlə** doldurulub (`chmod 600`, git-ə düşmür):

- `ANTHROPIC_API_KEY` və model `sabah-os-web-backend/.env`-dəndir, eyni Claude açarıdır.
- `SES_KEY_ID`, `SES_ACCESS_SECRET`, `SES_ENDPOINT_URL`, `SES_DEFAULT_EMAIL` də backend `.env`-dəndir, eyni SES göndəricisidir.
- Domenlər `meetbot.sabahos.com` və `attendee.sabahos.com`, portlar 8100 və 8101-dir.
- Bütün daxili sirlər (DB parolları, Django açarları, Fernet açarı, API token) yaradılıb.

**Yalnız bunu siz doldurmalısınız:**

| Açar | Haradan |
|---|---|
| `GLADIA_API_KEY` | [app.gladia.io](https://app.gladia.io) → API keys. Yeni hesaba 50 € kredit verilir (1 il, Solaria-1 ilə ~185 saat). Sonra 0.27 €/saat, abunəsiz (qiymətlər 2026-11-19 tarixinə görə) |

Bunlar serverdə `bootstrap.sh` tərəfindən avtomatik doldurulur, çünki Attendee bazasında yaranırlar:
`ATTENDEE_API_KEY`, `ATTENDEE_WEBHOOK_SECRET`.

`MEETBOT_TOKEN_PROD` və `MEETBOT_TOKEN_DEV` yaradılıb. Onları SABAH.OS backend-lərinə vermək üçün: `make backend-env`
(bölmə 3a).

`.env.example` eyni faylın sirsiz şablonudur.

---

## 3. Quraşdırma

```bash
# qovluğu serverə köçürün (.env ilə birlikdə)
rsync -av --exclude attendee-engine meetbot/ server:/opt/meetbot/
ssh server && cd /opt/meetbot

nano .env                    # GLADIA_API_KEY
make setup                   # = ./scripts/bootstrap.sh
make superuser               # meetbot.sabahos.com/admin/ üçün login

sudo cp deploy/nginx/meetbot.sabahos.com.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/meetbot.sabahos.com.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d meetbot.sabahos.com -d attendee.sabahos.com
```

`bootstrap.sh` bunları edir (təkrar işlətmək təhlükəsizdir):

- Attendee-ni sabit commit-də yükləyir və image-ləri qurur. İlk build 5–10 dəqiqə çəkir.
- Postgres, Redis və SeaweedFS-i qaldırır, migration-ları işlədir.
- Attendee-də admin hesabını, API açarını və webhook sirrini yaradıb `.env`-ə yazır.
- `GLADIA_API_KEY`-i Attendee-nin şifrələnmiş credentials-ına yazır. Dashboard-da əl ilə daxil etmək lazım deyil.
- Bütün stack-i işə salır.

`GLADIA_API_KEY`-i sonradan əlavə etsəniz və ya dəyişsəniz: `make stt-keys`.

Yoxlama:

```bash
make ps
curl -s https://meetbot.sabahos.com/healthz     # {"ok": true}
make logs
```

---

## 3a. SABAH.OS backend-lərini qoşmaq (Notetaker düyməsi)

```bash
make backend-env
```

Bu əmr iki blok çap edir. Hər bloku uyğun serverdəki backend `.env`-inə əlavə edin və backend-i restart edin:

```
# sabahos.com backend .env
MEETBOT_API_URL=https://meetbot.sabahos.com
MEETBOT_API_TOKEN=<MEETBOT_TOKEN_PROD>

# dev.sabahos.com backend .env
MEETBOT_API_URL=https://meetbot.sabahos.com
MEETBOT_API_TOKEN=<MEETBOT_TOKEN_DEV>
```

Backend-də `python manage.py migrate` (`calendars/0004`) işləməli, Celery worker və beat qalxmalıdır.
Bunlar olmasa düymə görünmür (`/calendar/events/meet-status/` → `notetaker_available: false`).

Cloudflare-də **Bot Fight Mode** açıqdırsa, backend-in serverdən göndərdiyi sorğular challenge-ə düşə bilər.
O halda `meetbot.sabahos.com` üçün `URI Path starts with /api/` → **Skip** qaydası əlavə edin.

## 4. (İstəyə bağlı) Avtomatik rejim: SABAH.OS bazasından oxumaq

**Default olaraq söndürülüb** (`SABAH_DB_HOST=` boşdur), çünki bot düymə ilə işləyir. Bu rejim açılsa, bot düyməyə baxmadan
Meet linki olan **bütün** görüşlərə qoşulur. Yalnız bunu həqiqətən istəsəniz açın.

Bu rejimdə meetbot SABAH.OS Postgres-indən yalnız bunları **oxuyur**:
`calendars_calendarevent`, `calendars_eventattendee` və `users` cədvəlinin `id, email, first_name, last_name` sütunları.
Parol hashı və digər sütunlar bağlıdır. Rol `.env`-dəki parolla yaradılır:

```bash
make sabah-reader-sql                                          # SQL-i göstərir
make sabah-reader-sql | sudo -u postgres psql -d sabah_os_ai_db # Postgres serverdə birbaşa qurulubsa
make sabah-reader-sql | docker exec -i <sabah-postgres-konteyneri> psql -U postgres -d sabah_os_ai_db
```

Docker konteynerindən bazaya şəbəkə yolu belə olmalıdır (`SABAH_DB_HOST=host.docker.internal`):

- **Postgres serverdə birbaşa qurulub** (SABAH backend `.env`-də `DB_HOST=localhost`). Postgres docker bridge-i də dinləməlidir:
  - `postgresql.conf` → `listen_addresses = 'localhost,172.17.0.1'`
  - `pg_hba.conf` → `host  sabah_os_ai_db  meetbot_reader  172.16.0.0/12  scram-sha-256`
  - sonra `sudo systemctl restart postgresql`. `172.17.0.1` docker0 interfeysidir, ona görə Docker Postgres-dən əvvəl başlamalıdır.
    Alternativ: `listen_addresses = '*'`, amma bu halda firewall-da 5432-ni xaricə bağlı saxlayın.
- **Postgres docker-dədir və portu `5432:5432` kimi açıqdır.** Əlavə heç nə lazım deyil.
- **Postgres docker-dədir, amma portu açıq deyil.** `docker-compose.override.yml` ilə `meetbot-worker`-i SABAH şəbəkəsinə qoşun
  və `SABAH_DB_HOST`-a Postgres servisinin adını yazın:
  ```yaml
  services:
    meetbot-worker:
      networks: [meetbot, sabah]
  networks:
    sabah:
      external: true
      name: <docker network ls-də SABAH.OS şəbəkəsi>
  ```
- **Postgres başqa serverdədir.** `SABAH_DB_HOST=<o serverin IP-si>`, onun `pg_hba.conf`-unda bu serverin IP-sinə icazə verin.

Yoxlama: `make sync-now`. Logda `calendar sync` xətası olmamalıdır.

---

## 5. Dillər və transkripsiya

| `MEETBOT_TRANSCRIPTION_PROVIDER` | Qarışıq az/ru/en | Qeyd |
|---|---|---|
| **`gladia`** (default) | ✅ dillər `MEETBOT_LANGUAGES=az,ru,en` ilə məhdudlaşdırılır, code-switching açıqdır | Tövsiyə olunur. Açar: `GLADIA_API_KEY` |
| `elevenlabs` | ✅ dil hər parçada avtomatik tanınır (Scribe v2) | Alternativ. Açar: `ELEVENLABS_API_KEY` |
| `openai` | ⚠️ avtomatik tanıyır, amma bəzən dili səhv seçir | Ən ucuzudur. Açar: `OPENAI_API_KEY` |
| `deepgram` | ❌ Azərbaycan dilini dəstəkləmir | Yalnız ru/en görüşlər üçün |
| `captions` | ❌ bütün görüş üçün bir dil (`MEETBOT_CAPTION_LANGUAGE`) | Pulsuzdur, Meet-in öz altyazıları |

Provayderi dəyişmək üçün `.env`-də dəyəri və açarı yazın, sonra `make stt-keys && make restart`.

Claude transkriptin qarışıq dildə olduğunu bilir: mətni ingiliscə yazır, Kiril yazılan adları latınca verir,
məhsul və layihə adlarını olduğu kimi saxlayır. Xülasənin dilini `MEETBOT_SUMMARY_LANGUAGE` ilə dəyişmək olar (`en | az | ru | tr`).

---

## 6. Bot görüşə necə qəbul olunur — vacib

Bot Google hesabı olmadan, "qonaq" kimi qoşulur (`SABAH Notetaker`). Google Workspace görüşlərində qonaq
**"Asking to join"** ekranında gözləyir, təşkilatçı onu **Admit** etməlidir.

- Bot 15 dəqiqə ərzində qəbul olunmasa, gözləmə otağından çıxır və görüş `no_transcript` statusu alır.
- Bot görüşdə tək qalanda 2 dəqiqəyə, 15 dəqiqə səssizlikdən sonra, ən gec isə 4 saata çıxır.
- Qoşulanda çata ingiliscə xəbərdarlıq yazır (`MEETBOT_JOIN_MESSAGE`).

Bunu tamamilə avtomatlaşdırmaq üçün botu ayrıca Google Workspace hesabı ilə daxil etmək olar ("signed-in bot").
Bunun üçün Workspace-də SAML SSO qurulur: [Attendee sənədi](https://github.com/attendee-labs/attendee/blob/main/docs/signed_in_bots.md#signed-in-google-meet-bots).
SSO ünvanları `https://attendee.sabahos.com/bot_sso/google_meet_sign_in` və `.../google_meet_sign_out` olacaq.
Qurduqdan sonra `.env`-də `MEETBOT_GOOGLE_MEET_USE_LOGIN=true` yazın.

---

## 7. Hansı görüşlərə qoşulur

Yalnız SABAH.OS-da **"Add SABAH Notetaker" düyməsi açılmış** tədbirlərə qoşulur. Şərtlər:

- tədbirin etibarlı Google Meet linki olmalıdır (`https://meet.google.com/xxx-yyyy-zzz`, yapışdırılmış və ya yaradılmış);
- vaxtı müəyyən görüş olmalıdır (bütün günlük hadisə və ya task olmaz);
- tədbir ləğv edilməmiş olmalıdır.

Təkrarlanan tədbirdə düymə seçilən əhatəyə görə tətbiq olunur ("this / following / all").
Hər görüşün botu yalnız həmin görüşdən 30 dəqiqə əvvəl yaradılır.

Google Calendar-dan import edilmiş tədbirdə də düyməni açmaq olar. Onun Meet linkini isə yalnız Google-da dəyişmək olar.

## 8. Nəticələr

- **E-poçt (AWS SES).** Backend-in `SESService`-i ilə eyni yolla gedir: `email-smtp.eu-central-1.amazonaws.com:587`, STARTTLS, eyni SES açarları,
  göndərən `SABAH Meetbot <no-reply@sabahhub.com>`. Mövzu `Meeting notes: <title> (<date>)`-dir. Məktub HTML və mətn variantında olur
  və tam transkriptə link verir. Kimə gedəcəyini `MEETBOT_EMAIL_TO` təyin edir: `organizer` (default), `invitees` (bütün iştirakçılar) və ya `none`.
  `MEETBOT_EMAIL_ALLOWED_DOMAINS=sabahhub.com` daxili qeydlərin kənar qonaqlara getməsinin qarşısını alır.
- **SABAH.OS** — tədbirin popup-ında botun statusu görünür: "will join", "in the meeting", "notes are ready".
  Görüş bitəndən sonra "Show notes" ilə qeydlər açılır. Bunu yalnız görüşün iştirakçıları görür.
- **Admin panel** `https://meetbot.sabahos.com/admin/`: bütün mühitlərin görüşləri (`client` sütunu: prod/dev), status, xülasə, tam transkript.
  Buradan "Xülasəni yenidən hazırla" və "Botu dayandır" əmrləri də işləyir.
- **Webhook.** `MEETBOT_RESULT_WEBHOOK_URL` göstərilsə, hər hazır xülasə JSON kimi POST olunur.
  Başlıq `X-Meetbot-Signature: sha256=<HMAC(MEETBOT_RESULT_WEBHOOK_SECRET, body)>`-dir.

Statuslar: `pending` → `scheduled` → `in_meeting` → `processing` → `summarizing` → `done`.
Bunlardan başqa: `no_transcript`, `cancelled`, `failed` (səbəb `last_error`-dadır). Claude xətası olanda sistem 5 dəfəyə qədər özü təkrar cəhd edir.

## 9. API

Tokenlər:
- `MEETBOT_API_TOKEN` admin tokenidir və bütün mühitləri görür.
- `MEETBOT_TOKEN_PROD` / `MEETBOT_TOKEN_DEV` yalnız öz mühitinin görüşlərini görür.
- SABAH.OS backend-i mühit tokeni ilə bu iki endpoint-i işlədir: `POST /api/events/sync/` və `GET /api/events/<event_id>/`.

Admin tokeni ilə nümunələr:

```bash
T=$(grep ^MEETBOT_API_TOKEN= .env | cut -d= -f2); H="Authorization: Bearer $T"; U=https://meetbot.sabahos.com

curl -s -H "$H" "$U/api/meetings/?status=done&limit=20"          # filtrlər: status, from, to, sabah_event_id, limit
curl -s -H "$H" "$U/api/meetings/?sabah_event_id=1234"           # SABAH.OS calendar hadisəsinin id-si ilə
curl -s -H "$H" -H 'Content-Type: application/json' -X POST "$U/api/meetings/" \
  -d '{"meeting_url": "https://meet.google.com/abc-defg-hij", "title": "Client call"}'   # botu indi göndər
curl -s -H "$H" "$U/api/meetings/<id>/"                          # status + summary (JSON) + summary_markdown
curl -s -H "$H" "$U/api/meetings/<id>/transcript/?format=text"   # [hh:mm:ss] Ad: mətn
curl -s -H "$H" -H 'Content-Type: application/json' -X POST "$U/api/meetings/<id>/ask/" \
  -d '{"question": "Who owns the release notes?"}'
curl -s -H "$H" -H 'Content-Type: application/json' -X POST "$U/api/meetings/<id>/summarize/" \
  -d '{"instructions": "Focus on technical decisions"}'          # yenidən xülasə (202, worker hazırlayır)
curl -s -H "$H" -X POST "$U/api/meetings/<id>/leave/"            # botu görüşdən çıxar
curl -s -H "$H" -X DELETE "$U/api/meetings/<id>/"                # planlanmış botu ləğv et
```

## 10. Claude

- Model `claude-opus-5-5`-dir (backend ilə eyni), `ANTHROPIC_EFFORT=high`. Xülasə JSON schema ilə gəlir, ona görə format həmişə eynidir.
- Streaming istifadə olunur. Kontekst 1M token-dir, ona görə 4 saatlıq görüş də bir sorğuda gedir.
- `ANTHROPIC_FALLBACKS=true`: təhlükəsizlik filtri sorğunu səhvən rədd etsə, sorğu avtomatik başqa modeldə təkrarlanır.
- Transkript `<transcript>` blokunda verilir və iştirakçıların sözləri göstəriş kimi qəbul edilmir (prompt-injection qoruması).
- `/ask`-da transkript prompt cache-də saxlanır, ona görə təkrar suallar ucuz başa gəlir.
- Təxmini qiymət: 1 saatlıq görüşün xülasəsi ~$0.05–0.15. Gladia-nın STT xərci buna daxil deyil.

## 11. Əməliyyat

| Əmr | Nə edir |
|---|---|
| `make logs` | Worker, web və bot logları |
| `make restart` | Yalnız meetbot servislərini yenidən qurub başladır. Bot worker-lərinə toxunmur, gedən görüşlər kəsilmir. `.env` dəyişəndən sonra |
| `make stt-keys` | STT açarlarını `.env`-dən Attendee-yə yazır |
| `make sync-now` | Calendar sinxronizasiyası və xülasə növbəsi dərhal bir dəfə işləyir |
| `make sabah-reader-sql` | SABAH.OS bazası üçün yalnız oxuma rolunun SQL-ini göstərir |
| `make test` | Körpü servisin testləri |
| `make update-attendee` | `ATTENDEE_REF` dəyişəndən sonra. **Bot worker-ləri restart olur, görüş saatlarından kənar edin** |

- **Eyni anda görüş sayı**: `.env`-də `ATTENDEE_BOT_WORKERS=4`, sonra `docker compose up -d`.
  Hər bot öz konteynerində işləyir, səslər qarışmır. Bütün worker-lər məşğul olsa, növbəti bot boşalan worker-i gözləyir.
- **Backup**: `docker compose exec postgres pg_dump -U meetbot meetbot > meetbot.sql`. Transkript və xülasələr buradadır.
- **Xam səs**: STT üçün səs parçaları müvəqqəti olaraq Attendee bazasında saxlanır. `attendee-janitor` 1 gündən köhnələrini saatda bir silir.

## 12. Problemlər

| Əlamət | Səbəb / həll |
|---|---|
| SABAH.OS-da "Add SABAH Notetaker" düyməsi görünmür | Backend `.env`-də `MEETBOT_API_URL`/`MEETBOT_API_TOKEN` yoxdur və ya backend restart olmayıb. Tədbirdə etibarlı Meet linki olmalıdır |
| Düymə açılıb, amma admin-də görüş yoxdur | Backend Celery worker-i işləmir və ya Cloudflare sorğunu bloklayır (bölmə 3a). Backend logunda `[Notetaker]` axtarın |
| Görüş `scheduled`-da qalır, "gözləmə otağındadır" | Botu Meet-də **Admit** edin (bölmə 6) |
| Görüş `no_transcript` | Bot qəbul olunmayıb (`bot_event`-ə baxın) və ya `GLADIA_API_KEY` boşdur/səhvdir (`make stt-keys`). Attendee dashboard → bot → Transcription errors |
| Calendar görüşləri gəlmir | `make logs`-da `calendar sync` xətası. Bölmə 4: rol, `listen_addresses`, `pg_hba.conf` |
| E-poçt gəlmir | Görüşün `last_error`-unda `email: …`. SES açarları və göndərən ünvanın SES-də təsdiqli olması |
| `failed` + Claude xətası | `ANTHROPIC_API_KEY`-i yoxlayın, sonra admin-də "Xülasəni yenidən hazırla" |
| `/admin/`-ə login olmur (HTTP, SSH tunel) | Cookie-lər yalnız HTTPS-də işləyir: domenlə açın və ya `MEETBOT_SECURE_COOKIES=false` + `make restart` |
| Bot ümumiyyətlə qoşulmur | `attendee.sabahos.com` → Bots → həmin bot: hadisələr və debug screenshot. `docker compose logs attendee-worker-bot` |

## 13. Məxfilik

- Bot görüşə açıq adla daxil olur və çata xəbərdarlıq yazır. Bunu söndürməyin, iştirakçıların razılığı vacibdir.
- Bot yalnız kimsə "Add SABAH Notetaker" düyməsini açdığı görüşə qoşulur. Düyməni istənilən vaxt söndürmək olar.
  Görüş zamanı söndürülsə, bot görüşdən çıxır.
- Səs və ya video yazısı saxlanmır (`MEETBOT_RECORDING_FORMAT=none`). Səs yalnız STT üçün Gladia-ya gedir, serverdəki müvəqqəti nüsxə 1 gündən sonra silinir.
- Claude və SES açarları yalnız meetbot konteynerlərindədir. Chrome işlədən bot konteynerlərinə ötürülmür.
- SABAH.OS bazasına çıxış yalnız oxumaq üçündür və 3 cədvəllə məhdudlaşdırılıb.

## Lisenziya

Attendee [Elastic License 2.0](https://github.com/attendee-labs/attendee/blob/main/LICENSE) ilədir. Daxili istifadə üçün öz serverimizdə işlətmək
sərbəstdir. Attendee-ni üçüncü tərəflərə hosted/managed xidmət kimi təqdim etmək qadağandır.
Attendee kodu dəyişdirilmədən `attendee-engine/`-ə yüklənir. Bu qovluqdakı kod (`app/`, `scripts/`) SABAH-ındır.
