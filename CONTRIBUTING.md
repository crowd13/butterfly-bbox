# How to contribute

## Сообщения коммитов

Каждый коммит должен соответствовать [Conventional Commits 1.0.0](https://www.conventionalcommits.org/ru/v1.0.0/).
Сообщения коммитов пишем на английском языке.

Дополнительное правило проекта: в конце заголовка каждого коммита обязательно указываем номер связанного GitHub issue в формате `(#1)`. Используйте номер существующей задачи, к которой относятся изменения.

Формат заголовка:

```text
<type>[optional scope]: <description> (#<issue number>)
```

Контекст (`scope`) необязателен. Для изменений, нарушающих обратную совместимость, используйте `!` перед двоеточием или сноску `BREAKING CHANGE`, как описано в спецификации.

Примеры (номера задач условные):

```text
feat: add dataset download script (#2)
fix: handle missing image URLs (#3)
docs: describe local setup (#1)
chore: configure project dependencies (#1)
```
