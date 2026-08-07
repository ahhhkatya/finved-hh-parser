# ФИНВЕД — автоматический парсер вакансий HH → Google Sheets

Проект раз в неделю ищет новые вакансии на hh.ru, анализирует их под услуги ФИНВЕД (управленческий учет / финансовая функция), удаляет дубли и добавляет только новые вакансии в Google Sheets.

## Что происходит автоматически

1. GitHub Actions запускает `parser.py` каждый понедельник в 09:00 по Москве.
2. Парсер берет вакансии за последние 7 дней через официальный API HH.
3. Дубли удаляются по ID вакансии.
4. Вакансии оцениваются по признакам управленческого учета.
5. Сильная бухгалтерия, налоги, ВЭД, банковское/проектное финансирование и конкуренты снижают релевантность или исключаются.
6. В Google Sheets используются два листа:
   - `Целевые лиды` — можно передавать менеджеру;
   - `Ручная проверка` — смешанные случаи.
7. Уже существующие ID вакансий повторно не добавляются.

## 1. Создай Google-таблицу

Создай пустую Google Sheets. Название может быть любым.

Из URL вида:

`https://docs.google.com/spreadsheets/d/1AbCdEfGh123456789/edit`

нужен только ID:

`1AbCdEfGh123456789`

Он станет секретом `GOOGLE_SHEET_ID`.

## 2. Создай Service Account в Google Cloud

1. Открой Google Cloud Console.
2. Создай проект (например `finved-hh-parser`).
3. Включи Google Sheets API и Google Drive API.
4. IAM & Admin → Service Accounts → Create Service Account.
5. Создай JSON key и скачай JSON.
6. В JSON найди `client_email`, например:
   `finved-hh-parser@project.iam.gserviceaccount.com`
7. Открой свою Google-таблицу → Поделиться → добавь этот `client_email` как редактора.

JSON-файл не добавляй в GitHub и не отправляй посторонним.

## 3. Создай GitHub-репозиторий

Создай приватный репозиторий, например `finved-hh-parser`, и загрузи туда содержимое этой папки, включая `.github/workflows/weekly.yml`.

## 4. Добавь Secrets в GitHub

Repository → Settings → Secrets and variables → Actions → New repository secret.

Создай 3 секрета:

### `HH_ACCESS_TOKEN`
Текущий токен приложения HH (client_credentials).

### `GOOGLE_SHEET_ID`
ID Google-таблицы.

### `GOOGLE_SERVICE_ACCOUNT_JSON`
Вставь **весь JSON** сервисного аккаунта Google целиком.

## 5. Первый ручной тест

GitHub → Actions → `Weekly Finved HH Parser` → Run workflow.

После успешного выполнения в Google Sheets появятся листы:
- `Целевые лиды`
- `Ручная проверка`

## 6. Еженедельный запуск

В `.github/workflows/weekly.yml` стоит:

```yaml
- cron: "0 6 * * 1"
```

Это понедельник, 06:00 UTC = 09:00 по Москве.

Если нужен другой день/время — поменяй cron.

## Локальный тест на Mac

```bash
cd ~/Downloads/finved_hh_google
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export HH_ACCESS_TOKEN='...'
export GOOGLE_SHEET_ID='...'
export GOOGLE_SERVICE_ACCOUNT_JSON='...'
python3 parser.py
```

## Что можно менять в `config.json`

- `period_days` — сколько дней вакансий брать;
- `search_queries` — поисковые запросы;
- `target_threshold` — порог целевого лида;
- `review_threshold` — порог ручной проверки;
- `core_management_phrases` — ключевые признаки управленки;
- `negative_rules` — штрафы за бухгалтерию, налоги, ВЭД, банки и т. п.;
- `hard_exclude_title` — должности, которые сразу исключаются.

## Важно

Токены и Google JSON не хранятся в коде. Они передаются только через GitHub Secrets.
