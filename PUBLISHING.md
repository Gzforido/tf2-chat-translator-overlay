# Публикация на GitHub

Эта папка — отдельная публичная копия; рабочая папка программы не изменялась. Репозиторий: [Gzforido/tf2-chat-translator-overlay](https://github.com/Gzforido/tf2-chat-translator-overlay).

Рекомендуемое имя репозитория: `tf2-chat-translator-overlay`.

Короткое описание для GitHub: `Open-source TF2 chat translator and inventory value overlays for Windows (PyQt6, DeepL, Steam, backpack.tf)`.

Темы (GitHub Topics): `team-fortress-2`, `tf2`, `chat-translation`, `deepl-api`, `pyqt6`, `steam`, `backpack-tf`, `overlay`, `python`.

Перед каждым обновлением:

1. Проверьте, что в папке нет `config.json`, логов и файлов Steam с ключами.
2. Выполните `git status --short` и `git check-ignore config.json`. Если у вас есть локальный `config.json`, он должен игнорироваться.
3. Просмотрите добавляемые файлы: `git diff --cached --name-only` после `git add .`.
4. Отправляйте изменения в существующий `origin` только после проверки списка файлов в коммите.

Если какой-либо ключ уже публиковался в чате, скриншоте или коммите, исключение файла через `.gitignore` не отменяет утечку: отзовите ключ и выпустите новый до публикации.
