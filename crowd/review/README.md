# Review bbox-разметки

Отдельный проект проверяет все 400 ответов bbox двумя независимыми исполнителями.
На странице пять рабочих заданий и один контроль. Авторы исходных ответов
исключаются через `unavailable_for`.

## Локальная проверка

```bash
python3 -m crowd.review.project plan
python3 -m crowd.review.project preview
python3 -m http.server 8765 --bind 127.0.0.1
```

Предпросмотр: `http://127.0.0.1:8765/crowd/review/preview.html`.

План содержит 6 учебных примеров, 10 экзаменационных, 80 уникальных контрольных,
8 пилотных и 72 основных страницы. Два review каждой страницы и до 20 попыток
экзамена стоят не более 196 ₽ с комиссией 40%.

## Создание

```bash
export YANDEX_TASKS_OAUTH=...
python3 -m crowd.review.project create --run crowd/review/runs/final
```

Команда идемпотентно создаёт проект, обучение, навык и три закрытых пула.
Она не открывает платную выдачу. После проверки интерфейса команда вручную
отправляет экзамен на модерацию, открывает его и начисляет навык:

```bash
python3 -m crowd.review.project score-exam --run crowd/review/runs/final
```

Затем открывается пилот. После проверки пилота открывается основной пул.
Во время выдачи контролируйте скрытые эталоны; после двух ответов навык остаётся
только при точности не ниже 80%:

```bash
python3 -m crowd.review.project score-controls --run crowd/review/runs/final
```

Токен и временное состояние запуска не коммитятся.

## Результат

```bash
python3 -m crowd.review.project sync --run crowd/review/runs/final
python3 -m crowd.review.export crowd/review/runs/final/assignments.json \
  --source crowd/bbox/runs/final/review.json \
  --output crowd/review/runs/final/reviews.json
```

Экспорт проверяет два разных review на каждый ответ и запрещает самопроверку.
Два `OK` принимают исходную разметку. Любое замечание, расхождение или
`LOAD_ERROR` отправляет фотографию на ручной разбор и исправление. Исправление
проверяет участник, который не создавал исходную рамку.
