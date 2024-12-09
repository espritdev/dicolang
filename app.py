from flask import Flask, render_template, request, jsonify, g
from deep_translator import GoogleTranslator
from functools import lru_cache
import requests
from bs4 import BeautifulSoup, SoupStrainer
import re
from concurrent.futures import ThreadPoolExecutor
import time
import cachetools
import sqlite3
from datetime import datetime
import os

app = Flask(__name__)
session = requests.Session()
executor = ThreadPoolExecutor(max_workers=4)

# Cache avec expiration après 1 heure
wiktionary_cache = cachetools.TTLCache(maxsize=1000, ttl=3600)
translation_cache = cachetools.TTLCache(maxsize=1000, ttl=3600)

LANGUAGES = {
    'fr': 'Français',
    'en': 'English',
    'de': 'Deutsch',
    'es': 'Español',
    'it': 'Italiano'
}

# Configuration
if os.environ.get('RENDER'):
    DATABASE = '/tmp/search_history.db'
else:
    DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'search_history.db')

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    try:
        with app.app_context():
            db = get_db()
            db.execute('''CREATE TABLE IF NOT EXISTS search_history
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          word TEXT NOT NULL,
                          source_lang TEXT NOT NULL,
                          timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            db.commit()
    except Exception as e:
        print(f"Error initializing database: {e}")

def add_to_history(word, lang):
    try:
        with app.app_context():
            db = get_db()
            db.execute('INSERT INTO search_history (word, source_lang) VALUES (?, ?)', 
                      (word, lang))
            db.commit()
    except Exception as e:
        print(f"Error adding to history: {e}")

def get_search_history():
    try:
        with app.app_context():
            db = get_db()
            cur = db.execute('SELECT * FROM search_history ORDER BY timestamp DESC LIMIT 50')
            return cur.fetchall()
    except Exception as e:
        print(f"Error getting history: {e}")
        return []

def cached_translate_word(word, source_lang, target_lang):
    cache_key = f"{word}:{source_lang}:{target_lang}"
    if cache_key in translation_cache:
        return translation_cache[cache_key]
    try:
        translator = GoogleTranslator(source=source_lang, target=target_lang)
        result = translator.translate(word)
        translation_cache[cache_key] = result
        return result
    except Exception as e:
        print(f"Erreur de traduction: {str(e)}")
        return "Non trouvé"

def translate_to_language(args):
    word, source_lang, target_lang, lang_name = args
    translation = cached_translate_word(word, source_lang, target_lang)
    return (lang_name, translation)

def get_all_translations(word, source_lang):
    translations = {LANGUAGES[source_lang]: word}
    
    translation_tasks = [
        (word, source_lang, lang_code, lang_name)
        for lang_code, lang_name in LANGUAGES.items()
        if lang_code != source_lang
    ]
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = executor.map(translate_to_language, translation_tasks)
        
    for lang_name, translation in results:
        translations[lang_name] = translation
    
    return translations

def get_wiktionary_content(word, lang='fr'):
    cache_key = f"{word}:{lang}"
    if cache_key in wiktionary_cache:
        return wiktionary_cache[cache_key]

    base_url = f'https://{lang}.wiktionary.org/wiki/'
    try:
        # Utiliser SoupStrainer pour parser uniquement les sections pertinentes
        only_content = SoupStrainer(['h2', 'h3', 'h4', 'ol', 'ul', 'p'])
        
        # Timeout court pour la requête
        response = session.get(base_url + word, timeout=2)
        if response.status_code != 200:
            raise Exception("Page non trouvée")

        # Parser uniquement les sections nécessaires
        soup = BeautifulSoup(response.text, 'lxml', parse_only=only_content)
        
        result = {
            'word': word,
            'definitions': [],
            'etymology': None,
            'examples': []
        }

        # Map des IDs de section par langue
        lang_sections = {
            'fr': ['Français', 'français'],
            'en': ['English'],
            'de': ['Deutsch'],
            'es': ['Español'],
            'it': ['Italiano']
        }

        # Trouver la section de langue
        current_section = None
        for heading in soup.find_all(['h2', 'h3', 'h4']):
            if any(lang_id in heading.get_text() for lang_id in lang_sections.get(lang, [])):
                current_section = heading
                break

        if current_section:
            # Parcourir les éléments suivants jusqu'à la prochaine section de langue
            current = current_section.find_next_sibling()
            while current and not (current.name == 'h2' and any(lang_id in current.get_text() for lang_id in sum(lang_sections.values(), []))):
                if current.name == 'ol':
                    for li in current.find_all('li', recursive=False):
                        text = li.get_text().strip()
                        if text and not text.startswith('\\'):
                            result['definitions'].append(text)
                            
                            # Chercher les exemples
                            for example in li.find_all('ul'):
                                for ex in example.find_all('li'):
                                    ex_text = ex.get_text().strip()
                                    if ex_text:
                                        result['examples'].append(ex_text)
                
                elif current.name == 'p' and 'étymologie' in current.get_text().lower():
                    result['etymology'] = current.get_text().strip()
                
                current = current.find_next_sibling()

        # Mettre en cache le résultat
        wiktionary_cache[cache_key] = result
        return result

    except Exception as e:
        print(f"Erreur Wiktionary ({word}): {str(e)}")
        default_result = {
            'word': word,
            'definitions': ["Définition non trouvée"],
            'etymology': None,
            'examples': []
        }
        wiktionary_cache[cache_key] = default_result
        return default_result

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/decouverte')
def decouverte():
    return render_template('decouverte.html')

@app.route('/a-propos')
def a_propos():
    return render_template('a_propos.html')

@app.route('/search', methods=['POST'])
def search():
    data = request.get_json()
    word = data.get('word', '').strip()
    lang = data.get('lang', 'fr')
    
    if not word:
        return jsonify({"error": "Veuillez entrer un mot"})
    
    try:
        # Ajouter la recherche à l'historique
        add_to_history(word, lang)
        
        # Exécuter les requêtes en parallèle
        with ThreadPoolExecutor(max_workers=2) as executor:
            translations_future = executor.submit(get_all_translations, word, lang)
            wiktionary_future = executor.submit(get_wiktionary_content, word, lang)
            
            translations = translations_future.result()
            wiktionary = wiktionary_future.result()
        
        return jsonify({
            "translations": translations,
            "etymology": wiktionary.get('etymology'),
            "definitions": wiktionary.get('definitions', []),
            "examples": wiktionary.get('examples', [])
        })
        
    except Exception as e:
        return jsonify({"error": f"Une erreur s'est produite: {str(e)}"})

@app.route('/historique')
def historique():
    history = get_search_history()
    return render_template('historique.html', history=history)

# Initialize the database at startup
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    app.run(host='0.0.0.0', port=port)
