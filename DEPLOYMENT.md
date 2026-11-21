# Deployment SIAB1

Kontrak deployment generik ini menggunakan `<your-domain>` dengan WAF sebagai ingress dan terminator TLS. Ganti semua placeholder seperti `<your-domain>`, `<deploy-dir>`, `<waf-dir>`, `<compose-project>`, dan `<database-name>` dengan nilai Anda sendiri sebelum deployment. Jangan commit hostname, alamat IP, path server, atau kredensial ke repositori publik ini.

## Topologi

```text
Internet -> WAF :80/:443 -> loopback Nginx -> API
```

- WAF dan SIAB1 berjalan pada host yang sama.
- WAF adalah satu-satunya ingress HTTP/HTTPS publik.
- Origin SIAB1 harus tetap bind ke loopback.
- Dashboard WAF harus tetap bind ke loopback dan diakses melalui SSH tunnel; jangan pernah mengeksposnya secara publik.

## Prasyarat

- Docker Engine dan Docker Compose plugin.
- DNS untuk `<your-domain>` mengarah ke server Anda setelah origin siap.
- Sertifikat publik yang valid dikelola pada WAF.
- File `.env` production berada di luar version control.
- Backup dan rollback plan telah diuji sebelum data production dimigrasikan.
- Host memiliki firewall aktif.

## Identitas Runtime

- WAF directory: `<waf-dir>`
- SIAB1 directory: `<deploy-dir>`
- WAF management: loopback only, diakses melalui SSH tunnel; jangan pernah mengeksposnya secara publik.
- SIAB1 origin: loopback only.
- Compose project SIAB1: `<compose-project>`
- Database SIAB1: `<database-name>`

## Konfigurasi SIAB1

Salin `.env.example` menjadi `.env` pada host dan isi melalui prosedur secret management. Jangan commit file `.env` ke version control.

Nilai minimum mencakup:

```env
APP_ENV=production
DEBUG=false
FORCE_HTTPS=true
DOMAIN=<your-domain>
PROTOCOL=https
CORS_ORIGINS=https://<your-domain>
DATABASE_URL=postgresql+asyncpg://examuser:<password>@pgbouncer:6432/<database-name>
POSTGRES_DB=<database-name>
DB_PASSWORD=<secret>
SECRET_KEY=<secret>
JWT_SECRET_KEY=<secret>
SEB_DEFAULT_CONFIG_KEY=<secret>
SEB_DEFAULT_BROWSER_EXAM_KEY=<secret>
```

TLS tidak dikonfigurasi pada Nginx SIAB1. WAF meneruskan `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`, dan `X-Forwarded-Port` ke Nginx SIAB1.

## Konfigurasi WAF

Template Compose yang telah dipin tersedia di `infrastructure/safeline/`. File `.env` WAF hanya diisi melalui secret management. Jalankan validasi konfigurasi sebelum deployment:

```bash
SAFELINE_DIR=<waf-dir> POSTGRES_PASSWORD=<secret> docker compose -f infrastructure/safeline/docker-compose.yml config --quiet
```

Konfigurasi aplikasi melalui dashboard:

| Field | Value |
|---|---|
| Domain | `<your-domain>` |
| Port | `80` |
| Upstream | SIAB1 origin (loopback) |
| Mode | `block` |

Pastikan upstream WAF mengarah ke origin SIAB1 pada loopback, bukan ke alamat publik. Akses dashboard WAF hanya melalui SSH tunnel; jangan pernah mengeksposnya secara publik.

## DNS

Arahkan DNS Anda ke server Anda sendiri:

| Type | Name | Target |
|---|---|---|
| A | `<your-domain>` | `<your-server-ip>` |

- Buat record A untuk `<your-domain>` yang mengarah ke alamat IP publik server Anda.
- Mode DNS-only atau proxied adalah pilihan Anda sendiri; sesuaikan dengan penyedia DNS Anda.
- Jangan mengarahkan DNS sebelum origin SIAB1 siap dan WAF berjalan.

## Validasi

Validasi origin, konfigurasi WAF, dan kesiapan runtime sebelum deployment:

```bash
scripts/prepare_runtime_dirs.sh <deploy-dir>
docker compose -p <compose-project> -f docker-compose.production.yml config --quiet
bash scripts/verify_stable_release.sh <deploy-dir> <commit>
bash scripts/verify_stable_release_vps.sh <deploy-dir>
```

Periksa health endpoint setelah origin berjalan:

```bash
curl --fail https://<your-domain>/health
```

## Start

```bash
docker compose -p <compose-project> -f docker-compose.production.yml up -d
docker compose -p <compose-project> -f docker-compose.production.yml ps
docker compose -p <compose-project> -f docker-compose.production.yml logs -f api
```

Jangan pernah menjalankan `docker compose down -v` pada production tanpa backup yang terverifikasi dan persetujuan eksplisit. Perintah tersebut menghapus persistent volumes.

## Client Release

Rilis client Android dengan server URL generik berikut:

```bash
SIAB1_SERVER_URL=https://<your-domain>/
SIAB1_RELEASE_KEYSTORE=/secure/path/release.jks
SIAB1_RELEASE_STORE_PASSWORD=<secret>
SIAB1_RELEASE_KEY_PASSWORD=<secret>
SIAB1_RELEASE_KEY_ALIAS=<secret>
./gradlew assembleRelease
```

Jangan pernah merilis client yang masih menggunakan placeholder `siab1.invalid`.

Karena deployment berbasis file, buat bukti release setelah sinkronisasi file:

```bash
OUTPUT_DIR=<deploy-dir>/releases bash scripts/generate_release_manifest.sh <deploy-dir> <commit>
```

## Backup dan Rollback

Pasang backup harian dan uji restore sebelum data production dimigrasikan:

```bash
sudo bash scripts/install_backup_systemd.sh <deploy-dir>
sudo systemctl start <compose-project>-backup.service
sudo systemctl status <compose-project>-backup.service
sudo systemctl list-timers | grep <compose-project>-backup
```

Backup harus mencakup:

- File `.env` WAF di `<waf-dir>/.env`, konfigurasi WAF, dan database `<database-name>`.
- Database SIAB1, resource directory, dan file `.env` SIAB1 (disimpan terpisah, di luar version control).
- Manifest release di `<deploy-dir>/releases`.

Uji restore secara berkala pada environment terpisah, tanpa mengganti database `<database-name>` production sebelum restore terverifikasi.

Rollback dilakukan dengan mengembalikan release sebelumnya dan menjalankan kembali validasi pada bagian Validasi di atas. Jangan pernah menjalankan `docker compose down -v` tanpa backup yang terverifikasi. Jika database perlu dipulihkan, pulihkan dari backup yang telah diuji, lalu jalankan kembali validasi dan pemeriksaan health endpoint.
