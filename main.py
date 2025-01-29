import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from datetime import datetime, timedelta
import time
import json
import logging
import sqlite3
import re
import requests
from configparser import ConfigParser
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from threading import Lock

# Блок 1: Инициализация логирования и конфигурации
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

class ReloadableConfig:
    def __init__(self, config_file='config.ini'):
        self.config_file = config_file
        self.config = ConfigParser()
        self.lock = Lock()
        self.last_modified = 0
        self.load_config()
        
        # Наблюдатель за изменениями файла
        self.observer = Observer()
        event_handler = FileSystemEventHandler()
        event_handler.on_modified = lambda e: self.load_config()
        self.observer.schedule(event_handler, path='.', recursive=False)
        self.observer.start()

    def load_config(self):
        with self.lock:
            try:
                new_mtime = os.path.getmtime(self.config_file)
                if new_mtime != self.last_modified:
                    self.config.read(self.config_file)
                    self.last_modified = new_mtime
                    logging.info("Конфигурация перезагружена")
            except Exception as e:
                logging.error(f"Ошибка загрузки конфига: {str(e)}")

    def get(self, *args, **kwargs):
        with self.lock:
            return self.config.get(*args, **kwargs)
    
    def getint(self, *args, **kwargs):
        with self.lock:
            return self.config.getint(*args, **kwargs)
    
    def getboolean(self, *args, **kwargs):
        with self.lock:
            return self.config.getboolean(*args, **kwargs)

# Загрузка конфигурации
config = ReloadableConfig()

# Блок 2: Настройки из конфига
class Settings:
    def __init__(self):
        # VK
        self.ACCESS_TOKEN = config.get('VK', 'ACCESS_TOKEN')
        self.TARGET_GROUP_ID = config.getint('VK', 'TARGET_GROUP_ID')
        self.REGION_ID = config.getint('VK', 'REGION_ID')
        
        # Поиск
        self.SEARCH_GROUP_KEYWORDS = json.loads(config.get('SEARCH', 'GROUP_KEYWORDS'))
        self.POST_KEYWORDS = json.loads(config.get('SEARCH', 'POST_KEYWORDS'))
        self.BLACKLIST_GROUPS = json.loads(config.get('SEARCH', 'BLACKLIST_GROUPS', fallback='[]'))
        
        # Фильтры
        self.STOP_WORDS = json.loads(config.get('FILTERS', 'STOP_WORDS', fallback='[]'))
        self.SPAM_REGEX = json.loads(config.get('FILTERS', 'SPAM_REGEX', fallback='[]'))
        
        # Настройки
        self.MAX_GROUPS = config.getint('SETTINGS', 'MAX_GROUPS')
        self.DAYS_AGO = config.getint('SETTINGS', 'DAYS_AGO')
        self.CHECK_INTERVAL = config.getint('SETTINGS', 'CHECK_INTERVAL')
        self.USE_PROXY = config.getboolean('NETWORK', 'USE_PROXY', fallback=False)
        
        # Telegram
        self.TELEGRAM_TOKEN = config.get('TELEGRAM', 'TOKEN', fallback='')
        self.TELEGRAM_CHAT_ID = config.get('TELEGRAM', 'CHAT_ID', fallback='')

settings = Settings()

# Блок 3: Инициализация VK API с прокси
if settings.USE_PROXY:
    proxy = {
        'https': config.get('NETWORK', 'PROXY_URL')
    }
    session = requests.Session()
    session.proxies = proxy
    vk_session = vk_api.VkApi(token=settings.ACCESS_TOKEN, session=session)
else:
    vk_session = vk_api.VkApi(token=settings.ACCESS_TOKEN)

vk = vk_session.get_api()

# Блок 4: База данных и модели
class Database:
    def __init__(self, db_name='posts.db'):
        self.conn = sqlite3.connect(db_name)
        self._create_tables()
        self.stats = {
            'total_posts': 0,
            'published_posts': 0,
            'errors': 0
        }

    def _create_tables(self):
        with self.conn:
            # Таблица опубликованных постов
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS posted_posts (
                    post_id TEXT PRIMARY KEY,
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица статистики
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS stats (
                    date DATE PRIMARY KEY,
                    total_posts INTEGER DEFAULT 0,
                    published_posts INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0
                )
            ''')
            
            # Черный список групп
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS blacklist (
                    group_id INTEGER PRIMARY KEY,
                    reason TEXT
                )
            ''')

    # Методы для работы с постами...
    # Полный код методов базы данных смотрите в предыдущих примерах

db = Database()

# Блок 5: Telegram-интеграция
class TelegramNotifier:
    def __init__(self):
        self.token = settings.TELEGRAM_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        
    def send_message(self, text):
        if not self.enabled:
            return
        
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': text[:4000],
                'parse_mode': 'HTML'
            }
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            logging.error(f"Ошибка отправки в Telegram: {str(e)}")

tg = TelegramNotifier()

# Блок 6: Основная логика с улучшениями
class VKBot:
    def __init__(self):
        self.last_check = datetime.now()
        
    def is_spam(self, text):
        text = text.lower()
        
        # Проверка стоп-слов
        if any(word in text for word in settings.STOP_WORDS):
            return True
            
        # Проверка по регулярным выражениям
        for pattern in settings.SPAM_REGEX:
            if re.search(pattern, text, re.IGNORECASE):
                return True
                
        return False

    def get_group_statistics(self):
        # Новая логика сбора статистики
        return {
            'processed': db.stats['total_posts'],
            'published': db.stats['published_posts'],
            'errors': db.stats['errors']
        }

    def process_comments(self, post):
        if not config.getboolean('SETTINGS', 'PROCESS_COMMENTS', fallback=False):
            return
            
        try:
            comments = vk.wall.getComments(
                owner_id=post['owner_id'],
                post_id=post['id'],
                count=100
            )['items']
            
            for comment in comments:
                if text_contains_keywords(comment['text'], settings.POST_KEYWORDS):
                    self.process_comment(comment)
        except Exception as e:
            logging.error(f"Ошибка обработки комментариев: {str(e)}")

    # Дополнительные методы класса...

# Блок 7: Запуск и управление
def main():
    bot = VKBot()
    while True:
        try:
            start_time = datetime.now()
            
            # Основная логика обработки
            bot.run_processing_cycle()
            
            # Отправка статистики
            stats = bot.get_group_statistics()
            report = (
                "📊 Статистика за последний цикл:\n"
                f"• Обработано постов: {stats['processed']}\n"
                f"• Опубликовано: {stats['published']}\n"
                f"• Ошибок: {stats['errors']}"
            )
            tg.send_message(report)
            
            # Ожидание следующего цикла
            sleep_time = settings.CHECK_INTERVAL - (datetime.now() - start_time).seconds
            if sleep_time > 0:
                time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logging.info("Работа остановлена пользователем")
            break
        except Exception as e:
            logging.critical(f"Критическая ошибка: {str(e)}", exc_info=True)
            tg.send_message(f"🚨 Критическая ошибка: {str(e)}")
            time.sleep(60)

if __name__ == '__main__':
    main()
