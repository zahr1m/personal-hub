# Деплой personal-hub на свой сервер

Плейсхолдеры: `<SERVER>` — ваш VPS, `<APP_DIR>` — каталог проекта на нём (например, `/opt/personal-hub`),
`desk.example.com` — домен дашборда, `<VPN_SUBNET>` — подсеть(и) вашего VPN, `<NGINX_CONTAINER>` и
`<CERTBOT_CONTAINER>` — контейнеры reverse proxy и certbot, `<NGINX_CONF_DIR>` — каталог `conf.d` nginx на хосте.

Один контейнер `hub` (бот через long polling + веб-дашборд на :8080). Порт на хост не публикуется:
веб виден только во внешней docker-сети `proxy` (alias `personal-hub`), наружу его отдаёт ваш nginx
на `desk.example.com` и только для VPN-сетей. Если у вас другая сеть прокси — поменяйте имя в `compose.yml`.

## 0. Что нужно заранее

- VPS с Docker и Compose, ~400 МБ свободной памяти;
- nginx в контейнере (или на хосте) с certbot, подключённый к docker-сети `proxy`
  (`docker network create proxy`, если её ещё нет);
- VPN (например, WireGuard), подсеть которого попадёт в allowlist nginx;
- домен с A-записью `desk.example.com` → публичный IP сервера. Нужен настоящий TLS: самоподписанный
  сертификат WebView Telegram не откроет. HTTP-01 через `/.well-known` работает и при allowlist,
  потому что challenge-локация на :80 открыта.

## 1. Бот

Создать бота у @BotFather, получить токен. Узнать свой Telegram ID (например, у @userinfobot) — это `ADMIN_TG_ID`.
Токен никуда не пересылать: он вписывается только в `.env` на сервере.

## 2. Каталог и файлы на сервере

```bash
mkdir -p <APP_DIR>/data
# с локальной машины: Dockerfile, .dockerignore, compose.yml, pyproject.toml, uv.lock, hub/, migrations/,
# scripts/, styles/app.css, tests/ (для тестового образа), ops/nginx/
chown 10001:10001 <APP_DIR>/data     # uid пользователя внутри образа
```

## 3. `.env` (права 600)

За основу взять `.env.example`. Значения:

- `BOT_TOKEN` — токен от @BotFather (не выводить и не логировать);
- `ANTHROPIC_API_KEY` — ключ из Anthropic Console (лучше отдельный workspace со spend limit);
- `ADMIN_TG_ID` — ваш Telegram ID, первый пользователь и админ;
- `ALLOWED_TG_IDS` — белый список tg_id через запятую (пусто = без ограничения, вход только по приглашению);
- `USER_NAMES` — имена вместо Telegram-имён: `tg_id=Имя` через запятую;
- `CONNECTIONS_KEY` — ключ Fernet, генерируется на сервере (см. ниже), не печатается;
- `WEB_BASE_URL=https://desk.example.com`; `BOT_USERNAME` — по желанию (иначе из getMe).

```bash
cd <APP_DIR>
install -m 600 /dev/null .env
# переменные вписать редактором, затем:
chmod 600 .env
```

## 4. Выкладка

Порядок важен: сначала образ и тесты, потом nginx и сертификат, и только потом запуск.
Пока контейнер не запущен, `desk.example.com` отдаёт 502 — это нормально.

1. Бэкап базы (при обновлении существующей установки):

```bash
cd <APP_DIR>
cp -a data/hub.db data/hub.db.pre-update-$(date +%F-%H%M)   # если базы ещё нет, пропустить
```

2. Сборка и Linux-only тесты в одноразовом контейнере (лимит памяти ICS-парсера, SIGTERM):

```bash
cd <APP_DIR>
docker compose build                        # стадия assets качает бинарники по assets.lock, долго в первый раз
docker run --rm personal-hub:latest python -c "import hub.web"
docker run --rm personal-hub:latest ls -l /app/hub/web/static/app.css /app/hub/web/static/htmx.min.js
docker build --target test -t personal-hub:test .
docker run --rm personal-hub:test pytest -q tests/test_main.py tests/test_ics_parse_isolation.py
# весь набор тоже можно: docker run --rm personal-hub:test
```

Ожидание: все тесты passed, ни один Linux-only не skipped (`-rs` покажет причины).
Если тесты красные — дальше не идти.

3. Ключ шифрования и адрес без вывода значений (ключ не должен попасть в историю и логи):

```bash
cd <APP_DIR>
grep -q '^CONNECTIONS_KEY=' .env || docker run --rm personal-hub:latest python -c \
  'from cryptography.fernet import Fernet; print("CONNECTIONS_KEY=" + Fernet.generate_key().decode())' >> .env
grep -q '^WEB_BASE_URL=' .env || echo 'WEB_BASE_URL=https://desk.example.com' >> .env
chmod 600 .env
grep -c '^CONNECTIONS_KEY=.' .env          # ожидается 1; значение не показывать
```

Сохраните копию `.env` в надёжном месте: без `CONNECTIONS_KEY` сохранённые ссылки календарей не расшифровать.
Уже заданный ключ не менять.

4. nginx, этап 1: только порт 80 (если nginx общий с другими сервисами — сначала бэкап каталога):

```bash
cp -a <NGINX_CONF_DIR> <NGINX_CONF_DIR>.bak-$(date +%F)
cp <APP_DIR>/ops/nginx/desk-http-only.conf <NGINX_CONF_DIR>/desk.conf
docker exec <NGINX_CONTAINER> nginx -t && docker exec <NGINX_CONTAINER> nginx -s reload
curl -sI http://desk.example.com/ | head -3     # 301 на https
```

5. Сертификат (webroot `/var/www/certbot` общий для nginx и certbot):

```bash
docker exec <CERTBOT_CONTAINER> certbot certonly --webroot -w /var/www/certbot -d desk.example.com
docker exec <NGINX_CONTAINER> ls /etc/letsencrypt/live/desk.example.com/     # fullchain.pem, privkey.pem
```

6. nginx, этап 2: полный конфиг. Перед копированием впишите свои подсети VPN в `allow` (`<VPN_SUBNET>`):

```bash
cp <APP_DIR>/ops/nginx/desk.conf <NGINX_CONF_DIR>/desk.conf
docker exec <NGINX_CONTAINER> nginx -t && docker exec <NGINX_CONTAINER> nginx -s reload
```

Если `nginx -t` падает: вернуть `desk-http-only.conf` (или удалить `desk.conf`), снова `nginx -t` и reload.

7. Запуск и проверка:

```bash
cd <APP_DIR>
docker compose up -d
docker compose ps                          # hub: running
docker compose logs --tail=100 hub         # без ошибок
docker exec <NGINX_CONTAINER> nginx -s reload   # nginx кэширует IP апстримов после пересоздания контейнера
curl -s https://desk.example.com/healthz   # изнутри VPN: {"ok":true}
# снаружи VPN: ожидается 403
docker network inspect proxy | grep -A2 personal-hub   # контейнер в сети, alias personal-hub
ss -ltn | grep 8080                        # пусто: порт на хост не опубликован
```

8. Память: лимит 384 МБ. Во время синхронизации календаря (кнопка «Проверить» у календаря или ожидание
планировщика) убедиться, что пик ниже лимита и `OOMKilled` нет:

```bash
docker stats --no-stream personal-hub-hub-1
docker inspect -f '{{.State.OOMKilled}} {{.RestartCount}}' personal-hub-hub-1   # false 0
```

9. Дымовая проверка: `/start` → приветствие; «купить билеты до пятницы» → задача с ближайшей пятницей;
`/invite` → ссылка; `/brief` → брифинг; `/web` → ссылка → вход в браузере; `/settings` → кнопка открывает
дашборд в Telegram (VPN на телефоне включён); календарь по секретной ссылке → «Работает»; событие сегодня
на «Сегодня» и в `/brief`; задача из веба видна в боте и наоборот; второй пользователь не видит
нерасшаренных задач первого.

## 5. Обновление

Скопировать новый код (`hub/`, `migrations/`, `scripts/`, `styles/app.css`, `tests/`, `Dockerfile`, `compose.yml`,
`pyproject.toml`, `uv.lock`) в `<APP_DIR>`, затем:

```bash
cd <APP_DIR> && docker compose up -d --build
docker compose logs --tail=50 hub
```

Миграции применяются при старте. `.env` и `data/` не трогать.

## 6. Откат

```bash
cd <APP_DIR> && docker compose down
```

Данные остаются в `<APP_DIR>/data`. Для возврата к прежней версии восстановить прежние файлы кода
и выполнить `docker compose up -d --build`. Базу при необходимости вернуть из `data/hub.db.pre-update-*`
(владелец файла — uid 10001).

Откат nginx:

```bash
rm <NGINX_CONF_DIR>/desk.conf        # или вернуть каталог из <NGINX_CONF_DIR>.bak-*
docker exec <NGINX_CONTAINER> nginx -t && docker exec <NGINX_CONTAINER> nginx -s reload
```

Сертификат при откате не трогать (отзыв не нужен).

## 7. Бэкапы

Планировщик каждую ночь кладёт копию базы в `<APP_DIR>/data/backups` (в контейнере `/data/backups`),
хранятся 7 последних. Восстановление: `docker compose down`, подставить нужный файл вместо `data/hub.db`
(владелец 10001), `docker compose up -d`. Копировать бэкапы с сервера — на ваше усмотрение.
