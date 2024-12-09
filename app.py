from flask import Flask, render_template, request, jsonify, g, Response
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
from urllib.parse import quote

app = Flask(__name__)

# Cache avec expiration après 1 heure
wiktionary_cache = cachetools.TTLCache(maxsize=1000, ttl=3600)
translation_cache = cachetools.TTLCache(maxsize=1000, ttl=3600)

# Session pour réutiliser les connexions HTTP
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
})

executor = ThreadPoolExecutor(max_workers=4)

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

def delete_from_history(word_id):
    try:
        with app.app_context():
            db = get_db()
            db.execute('DELETE FROM search_history WHERE id = ?', (word_id,))
            db.commit()
            return True
    except Exception as e:
        print(f"Error deleting from history: {e}")
        return False

def clear_history():
    try:
        with app.app_context():
            db = get_db()
            db.execute('DELETE FROM search_history')
            db.commit()
            return True
    except Exception as e:
        print(f"Error clearing history: {e}")
        return False

def get_history_as_text():
    try:
        history = get_search_history()
        text_content = "Historique des recherches:\n\n"
        for entry in history:
            timestamp = datetime.strptime(entry['timestamp'], '%Y-%m-%d %H:%M:%S')
            formatted_date = timestamp.strftime('%d/%m/%Y %H:%M:%S')
            text_content += f"Mot: {entry['word']}\n"
            text_content += f"Langue: {entry['source_lang']}\n"
            text_content += f"Date: {formatted_date}\n"
            text_content += "-" * 40 + "\n"
        return text_content
    except Exception as e:
        print(f"Error generating history text: {e}")
        return "Erreur lors de la génération du fichier"

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
        results = list(executor.map(translate_to_language, translation_tasks))
        
    translations.update(dict(results))
    return translations

def get_wiktionary_data(word, lang='fr'):
    cache_key = f"wiktionary:{word}:{lang}"
    if cache_key in wiktionary_cache:
        return wiktionary_cache[cache_key]

    try:
        if lang == 'fr':
            url = f'https://fr.wiktionary.org/wiki/{word}'
        else:
            url = f'https://fr.wiktionary.org/wiki/{word}#{LANGUAGES.get(lang, lang)}'

        print(f"[Wiktionary] Fetching data from: {url}")

        response = session.get(url, timeout=5)
        response.raise_for_status()
        
        print(f"[Wiktionary] Response status code: {response.status_code}")
        
        soup = BeautifulSoup(response.text, 'lxml', parse_only=SoupStrainer(['h2', 'h3', 'h4', 'p', 'ol']))
        
        print("[Wiktionary] HTML parsed successfully")
        
        french_section = None
        for h2 in soup.find_all('h2'):
            span = h2.find('span', {'id': ['Français', 'français']})
            if span:
                french_section = h2
                print(f"[Wiktionary] Found French section with id: {span.get('id')}")
                break
        
        if not french_section:
            print("[Wiktionary] French section not found in the page")
            if "Wiktionnaire ne possède pas d'article avec ce nom" in response.text:
                print("[Wiktionary] Page does not exist")
                return {'etymology': None, 'definitions': [], 'examples': []}
            
            definitions_list = soup.find('ol')
            if definitions_list:
                print("[Wiktionary] Found definitions list without French section")
                content = [definitions_list]
            else:
                return {'etymology': None, 'definitions': [], 'examples': []}
        else:
            content = []
            current = french_section.find_next()
            while current and current.name != 'h2':
                if current.name in ['h3', 'h4', 'p', 'ol']:
                    content.append(current)
                current = current.find_next()
        
        definitions = []
        examples = []
        etymology = None
        
        for section in content:
            if section.name == 'h3' and section.find('span', {'id': 'Étymologie'}):
                etym_p = section.find_next('p')
                if etym_p:
                    etymology = etym_p.get_text().strip()
                    print(f"[Wiktionary] Etymology found: {etymology[:50]}...")
            
            if section.name == 'ol':
                for li in section.find_all('li', recursive=False):
                    def_text = li.get_text().strip()
                    if def_text and not def_text.startswith('('):
                        def_text = re.sub(r'\([^)]*\)', '', def_text)
                        def_text = re.sub(r'\s+', ' ', def_text)
                        def_text = def_text.strip()
                        if def_text:
                            definitions.append(def_text)
                            print(f"[Wiktionary] Definition found: {def_text[:50]}...")
                        
                        example_ul = li.find('ul')
                        if example_ul:
                            for ex_li in example_ul.find_all('li'):
                                ex_text = ex_li.get_text().strip()
                                if ex_text:
                                    examples.append(ex_text)
                                    print(f"[Wiktionary] Example found: {ex_text[:50]}...")
        
        result = {
            'etymology': etymology,
            'definitions': definitions[:5],
            'examples': examples[:3]
        }
        
        wiktionary_cache[cache_key] = result
        print(f"[Wiktionary] Final result for {word}: {len(definitions)} definitions, {len(examples)} examples")
        return result
        
    except requests.Timeout:
        print("[Wiktionary] Request timed out")
        return {'etymology': None, 'definitions': [], 'examples': []}
    except requests.RequestException as e:
        print(f"[Wiktionary] Network error: {str(e)}")
        return {'etymology': None, 'definitions': [], 'examples': []}
    except Exception as e:
        print(f"[Wiktionary] Unexpected error: {str(e)}")
        import traceback
        print(f"[Wiktionary] Traceback: {traceback.format_exc()}")
        return {'etymology': None, 'definitions': [], 'examples': []}

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
        original_word = word
        
        # Rechercher d'abord avec le mot tel quel
        wiktionary_url = f"https://fr.wiktionary.org/wiki/{quote(original_word)}"
        response = requests.get(wiktionary_url, headers=session.headers)
        
        if response.status_code == 404:
            # Si non trouvé, essayer en minuscules
            word_lower = word.lower()
            wiktionary_url = f"https://fr.wiktionary.org/wiki/{quote(word_lower)}"
            response = requests.get(wiktionary_url, headers=session.headers)
            
            if response.status_code == 404:
                # Si toujours non trouvé, essayer avec la première lettre en majuscule
                word_capitalized = word.capitalize()
                wiktionary_url = f"https://fr.wiktionary.org/wiki/{quote(word_capitalized)}"
                response = requests.get(wiktionary_url, headers=session.headers)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Sauvegarder dans l'historique avec le mot original (avec majuscules si présentes)
            add_to_history(original_word, lang)
            
            # Exécuter les requêtes en parallèle
            with ThreadPoolExecutor(max_workers=2) as executor:
                translations_future = executor.submit(get_all_translations, original_word, lang)
                wiktionary_future = executor.submit(get_wiktionary_data, original_word, lang)
                
                translations = translations_future.result()
                wiktionary_data = wiktionary_future.result()
            
            return jsonify({
                "translations": translations,
                "etymology": wiktionary_data.get('etymology'),
                "definitions": wiktionary_data.get('definitions'),
                "examples": wiktionary_data.get('examples')
            })
            
        else:
            return jsonify({
                'error': True,
                'message': f"Le mot '{original_word}' n'a pas été trouvé dans le Wiktionnaire."
            })
            
    except Exception as e:
        print(f"Erreur lors de la recherche : {str(e)}")
        return jsonify({
            'error': True,
            'message': "Une erreur s'est produite lors de la recherche. Veuillez réessayer."
        })

@app.route('/historique')
def historique():
    history = get_search_history()
    return render_template('historique.html', history=history)

@app.route('/delete_history_entry/<int:word_id>', methods=['POST'])
def delete_history_entry(word_id):
    if delete_from_history(word_id):
        return jsonify({'success': True})
    return jsonify({'success': False}), 500

@app.route('/clear_history', methods=['POST'])
def clear_all_history():
    if clear_history():
        return jsonify({'success': True})
    return jsonify({'success': False}), 500

@app.route('/download_history')
def download_history():
    text_content = get_history_as_text()
    return Response(
        text_content,
        mimetype='text/plain',
        headers={'Content-Disposition': 'attachment;filename=historique_recherches.txt'}
    )

# Initialize the database at startup
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    app.run(host='0.0.0.0', port=port)
