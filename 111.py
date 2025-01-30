import json
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from configparser import ConfigParser
import vk_api
from vk_api.exceptions import ApiError

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Загрузка конфигурации
config = ConfigParser()
config.read('config.ini')

# Основные настройки
SETTINGS = {
    'ACCESS_TOKEN': config.get('VK', 'ACCESS_TOKEN'),
    'TARGET_GROUP_ID': config.getint('VK', 'TARGET_GROUP_ID'),
    'REGION_ID': config.getint('VK', 'REGION_ID'),
    'SEARCH_KEYWORDS': json.loads(config.get('SEARCH', 'GROUP_KEYWORDS')),
    'POST_KEYWORDS': json.loads(config.get('SEARCH', 'POST_KEYWORDS')),
    'MAX_GROUPS': config.getint('SETTINGS', 'MAX_GROUPS'),
    'DAYS_AGO': config.getint('SETTINGS', 'DAYS_AGO'),
    'CHECK_INTERVAL': config.getint('SETTINGS', 'CHECK_INTERVAL')
}

# Проверка региона
if SETTINGS['REGION_ID'] != 47:
    logging.warning("REGION_ID должен быть 47 для Нижнего Новгорода. Исправляю...")
    SETTINGS['REGION_ID'] = 47

# Инициализация VK API
vk_session = vk_api.VkApi(token=SETTINGS['ACCESS_TOKEN'])
vk = vk_session.get_api()

# Инициализация базы данных
conn = sqlite3.connect('posts.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS posted_posts (
        post_id TEXT PRIMARY KEY,
        added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')
conn.commit()

def text_contains_keywords(text, keywords):
    """Проверяет наличие ключевых слов в тексте"""
    text = text.lower()
    return any(re.search(rf'\b{kw.lower()}\b', text) for kw in keywords)

def get_groups():
    """Поиск групп по ключевым словам"""
    groups = []
    for keyword in SETTINGS['SEARCH_KEYWORDS']:
        try:
            result = vk.groups.search(
                q=keyword,
                city_id=SETTINGS['REGION_ID'],
                type='group',
                sort='members',
                count=SETTINGS['MAX_GROUPS']
            )
            groups.extend([-group['id'] for group in result['items']])
            time.sleep(0.34)  # Лимит 3 запроса/сек
        except ApiError as e:
            logging.error(f"Ошибка при поиске групп: {e}")
            continue
    return list(set(groups))

def get_recent_posts(group_id):
    """Получение свежих постов из группы"""
    try:
        now = datetime.now()
        time_threshold = now - timedelta(days=SETTINGS['DAYS_AGO'])

        posts = vk.wall.get(
            owner_id=group_id,
            count=100,
            filter='owner'
        )['items']

        return [
            post for post in posts
            if datetime.fromtimestamp(post['date']) >= time_threshold
            and text_contains_keywords(post.get('text', ''), SETTINGS['POST_KEYWORDS'])
        ]
    except ApiError as e:
        logging.error(f"Ошибка при получении постов: {e}")
        return []
    finally:
        time.sleep(0.34)  # Лимит 3 запроса/сек

def post_exists(post_id):
    """Проверяет существование поста в БД"""
    cursor.execute('SELECT 1 FROM posted_posts WHERE post_id = ?', (post_id,))
    return cursor.fetchone() is not None

def process_attachments(post):
    """Обрабатывает вложения к посту"""
    return ",".join(
        f"{attach['type']}{attach[attach['type']]['owner_id']}_{attach[attach['type']]['id']}"
        for attach in post.get('attachments', [])
        if attach['type'] in ['photo', 'video', 'doc']
    )

def repost_post(post):
    """Репост поста в целевую группу"""
    post_id = f"{post['owner_id']}_{post['id']}"
    if post_exists(post_id):
        return False

    try:
        vk.wall.post(
            owner_id=SETTINGS['TARGET_GROUP_ID'],
            message=post['text'][:4000],  # Лимит VK
            attachments=process_attachments(post),
            copyright_link=f"https://vk.com/wall{post_id}"
        )
        cursor.execute('INSERT INTO posted_posts (post_id) VALUES (?)', (post_id,))
        conn.commit()
        return True
    except ApiError as e:
        logging.error(f"Ошибка репоста: {e}")
        return False
    except Exception as e:
        logging.error(f"Общая ошибка: {e}")
        return False

def main_loop():
    """Основной цикл работы бота"""
    while True:
        try:
            groups = get_groups()
            logging.info(f"Найдено групп для анализа: {len(groups)}")

            for group_id in groups:
                try:
                    posts = get_recent_posts(group_id)
                    logging.info(f"Группа {group_id}: найдено {len(posts)} постов")

                    for post in posts:
                        if repost_post(post):
                            logging.info(f"Успешный репост: {post['id']}")
                            time.sleep(1)  # Задержка между постами

                    time.sleep(1)  # Задержка между группами

                except Exception as e:
                    logging.error(f"Ошибка обработки группы {group_id}: {e}")

            logging.info(f"Цикл завершен. Ожидание {SETTINGS['CHECK_INTERVAL']} сек.")
            time.sleep(SETTINGS['CHECK_INTERVAL'])

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logging.error(f"Критическая ошибка: {e}")
            time.sleep(60)

if __name__ == '__main__':
    try:
        logging.info("Запуск бота...")
        main_loop()
    except KeyboardInterrupt:
        logging.info("Работа остановлена пользователем")
    finally:
        conn.close()
        logging.info("Соединение с базой данных закрыто")
