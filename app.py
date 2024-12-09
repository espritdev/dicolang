from flask import Flask, render_template, request, jsonify, g
from deep_translator import GoogleTranslator
from functools import lru_cache
import requests
from bs4 import BeautifulSoup, SoupStrainer, Tag
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

def get_wiktionary_data(word, lang='fr'):
    try:
        # Construire l'URL en fonction de la langue
        if lang == 'fr':
            url = f'https://fr.wiktionary.org/wiki/{word}'
        else:
            url = f'https://fr.wiktionary.org/wiki/{word}#{LANGUAGES.get(lang, lang)}'

        print(f"Fetching Wiktionary data from: {url}")

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Trouver la section française
        french_section = None
        for h2 in soup.find_all('h2'):
            if h2.find('span', {'id': 'Français'}) or h2.find('span', {'id': 'français'}):
                french_section = h2
                break
        
        if not french_section:
            print("Section française non trouvée")
            return {'etymology': None, 'definitions': [], 'examples': []}
        
        # Récupérer toutes les sections jusqu'à la prochaine h2
        content = []
        current = french_section.find_next()
        while current and current.name != 'h2':
            if current.name in ['h3', 'h4', 'p', 'ol']:
                content.append(current)
            current = current.find_next()
        
        # Récupérer les définitions
        definitions = []
        examples = []
        etymology = None
        
        for section in content:
            # Chercher l'étymologie
            if section.name == 'h3' and section.find('span', {'id': 'Étymologie'}):
                etym_p = section.find_next('p')
                if etym_p:
                    etymology = etym_p.get_text().strip()
                    print(f"Étymologie trouvée: {etymology}")
            
            # Chercher les définitions
            if section.name == 'ol':
                for li in section.find_all('li', recursive=False):
                    def_text = li.get_text().strip()
                    if def_text and not def_text.startswith('('):
                        # Nettoyer la définition
                        def_text = re.sub(r'\([^)]*\)', '', def_text)  # Supprimer les parenthèses
                        def_text = re.sub(r'\s+', ' ', def_text)  # Normaliser les espaces
                        def_text = def_text.strip()
                        if def_text:
                            definitions.append(def_text)
                            print(f"Définition trouvée: {def_text}")
                        
                        # Chercher les exemples dans cette définition
                        example_ul = li.find('ul')
                        if example_ul:
                            for ex_li in example_ul.find_all('li'):
                                ex_text = ex_li.get_text().strip()
                                if ex_text:
                                    examples.append(ex_text)
                                    print(f"Exemple trouvé: {ex_text}")
        
        result = {
            'etymology': etymology,
            'definitions': definitions[:5],
            'examples': examples[:3]
        }
        
        print(f"Résultat final pour {word}: {result}")
        return result
        
    except Exception as e:
        print(f"Erreur lors de la récupération des données Wiktionary: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'etymology': None,
            'definitions': [],
            'examples': []
        }

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
            wiktionary_future = executor.submit(get_wiktionary_data, word, lang)
            
            translations = translations_future.result()
            wiktionary_data = wiktionary_future.result()
        
        return jsonify({
            "translations": translations,
            "etymology": wiktionary_data.get('etymology'),
            "definitions": wiktionary_data.get('definitions'),
            "examples": wiktionary_data.get('examples')
        })
        
    except Exception as e:
        print(f"Erreur lors de la recherche: {str(e)}")
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
