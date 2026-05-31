import pandas as pd
import sqlite3
import requests
import json
import os
import re
import logging
from flask import Flask, render_template, request, jsonify, send_file
import xml.etree.ElementTree as ET
import io
from concurrent.futures import ThreadPoolExecutor

# Configure logging to overwrite log.txt on every run
logging.basicConfig(
    filename='log.txt',
    filemode='w',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("IPTV-App")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24) # Generates a random 24-byte key

DB_PATH = "iptv_data.db"
DOWNLOAD_DIR = "downloads"
DEFAULT_M3U_URL = "https://iptv-org.github.io/iptv/countries/in.m3u"

def _normalize_name(name):
    """Lowercases and strips all non-alphanumeric characters for fuzzy matching."""
    if not name: return ""
    # Remove resolution tags first to avoid leaving 'p' or 'k'
    name = re.sub(r'[\(\[][0-9]+[pi][\)\]]', '', name, flags=re.I)
    name = re.sub(r'\s+(HD|SD|FHD|UHD|4K)\b', '', name, flags=re.I)
    return re.sub(r'[^a-z0-9]', '', name.lower())

def _get_clean_display_name(name):
    """Strips quality and status tags to produce a clean name for EPG matching."""
    if not name: return ""
    # Remove resolution tags: (1080p), [720p], (SD), etc.
    name = re.sub(r'\s*[\(\[].*?[0-9]+[pi].*?[\)\]]', '', name, flags=re.I)
    # Remove quality abbreviations: HD, SD, FHD, UHD, 4K
    name = re.sub(r'\s+(HD|SD|FHD|UHD|4K)\b', '', name, flags=re.I)
    # Remove common status tags
    name = re.sub(r'\s*[\(\[].*?Not 24/7.*?[\)\]]', '', name, flags=re.I)
    name = re.sub(r'\s*[\(\[].*?Geo-blocked.*?[\)\]]', '', name, flags=re.I)
    return name.strip()

def _add_column_if_not_exists(cursor, table_name, column_name, column_type, default_value=None):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [col[1] for col in cursor.fetchall()]
    if column_name not in columns:
        logger.info(f"Adding missing column '{column_name}' to table '{table_name}'")
        default_clause = f" DEFAULT {default_value}" if default_value is not None else ""
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}{default_clause}")

def _initialize_db():
    """Ensures the database exists and has the correct schema on startup."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            url TEXT PRIMARY KEY,
            name TEXT,
            tvg_id TEXT,
            logo TEXT,
            group_title TEXT,
            clean_name TEXT,
            epg_id TEXT,
            is_working INTEGER,
            is_master INTEGER DEFAULT 0
        )
    """)
    # Migration: Add columns to existing databases if they are missing
    _add_column_if_not_exists(cursor, 'channels', 'is_working', 'INTEGER')
    _add_column_if_not_exists(cursor, 'channels', 'clean_name', 'TEXT')
    _add_column_if_not_exists(cursor, 'channels', 'epg_id', 'TEXT')
    _add_column_if_not_exists(cursor, 'channels', 'is_master', 'INTEGER', default_value=0)
    
    # Update any existing records missing clean_name
    cursor.execute("UPDATE channels SET clean_name = name WHERE clean_name IS NULL")
    conn.commit()
    conn.close()

def _check_stream_status(channel):
    """
    Helper function to check if a stream URL is reachable.
    """
    try:
        # Use stream=True and a short timeout to avoid downloading the whole stream
        response = requests.get(channel['url'], timeout=5, stream=True, allow_redirects=True)
        if response.status_code < 400:
            return channel
    except Exception:
        pass
    return None

def _auto_map_epg_ids():
    """Attempts to match channels to EPG IDs based on normalized names from guide.xml."""
    guide_path = os.path.join(DOWNLOAD_DIR, "guide.xml")
    if not os.path.exists(guide_path):
        logger.info("guide.xml not found in downloads. Skipping EPG mapping.")
        return

    try:
        tree = ET.parse(guide_path)
        root = tree.getroot()
        epg_map = {}
        for chan in root.findall('channel'):
            disp = chan.find('display-name')
            if disp is not None:
                epg_map[_normalize_name(disp.text)] = chan.get('id')

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        channels = cursor.execute("SELECT url, name FROM channels").fetchall()
        for url, name in channels:
            norm = _normalize_name(name)
            if norm in epg_map:
                cursor.execute("UPDATE channels SET epg_id = ? WHERE url = ?", (epg_map[norm], url))
        
        conn.commit()
        conn.close()
        logger.info("EPG ID Auto-mapping complete.")
    except Exception as e:
        logger.error(f"EPG Auto-mapping failed: {e}")

def _sync_and_consolidate_data(m3u_url, validate=False):
    """
    Downloads an M3U playlist, parses channel metadata, and stores it in SQLite.
    """
    try:
        logger.info(f"Starting M3U sync from: {m3u_url}")
        if not os.path.exists(DOWNLOAD_DIR):
            os.makedirs(DOWNLOAD_DIR)

        response = requests.get(m3u_url, timeout=30)
        response.raise_for_status()
        content = response.text

        channels = []
        # Improved parsing: Find attributes like key="value"
        attr_re = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')

        lines = content.splitlines()
        for i in range(len(lines)):
            line = lines[i].strip()
            if line.startswith('#EXTINF'):
                # Extract the name (the part after the last comma)
                name_part = ""
                if ',' in line:
                    name_part = line.split(',')[-1].strip()

                # Extract all key="value" pairs into a dict
                attrs = dict(attr_re.findall(line))
                
                # The next line that isn't a comment should be the URL
                url = ""
                for j in range(i + 1, len(lines)):
                    next_line = lines[j].strip()
                    if next_line:
                        if next_line.startswith('#'):
                            continue
                        url = next_line
                        break
                
                if url:
                    channels.append({
                        'name': name_part or attrs.get('tvg-name', 'Unknown'),
                        'tvg_id': attrs.get('tvg-id') or attrs.get('id', ""),
                        'logo': attrs.get('tvg-logo') or attrs.get('logo', ""),
                        'group_title': attrs.get('group-title', "Uncategorized"),
                        'url': url
                    })

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Insert/Update metadata but preserve is_master status
        for ch in channels:
            cursor.execute("""
                INSERT INTO channels (url, name, tvg_id, logo, group_title, clean_name, is_working, is_master)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
                ON CONFLICT(url) DO UPDATE SET
                    name = excluded.name,
                    tvg_id = excluded.tvg_id,
                    logo = excluded.logo,
                    group_title = excluded.group_title,
                    clean_name = excluded.clean_name
            """, (ch['url'], ch['name'], ch['tvg_id'], ch['logo'], ch['group_title'], 
                  _get_clean_display_name(ch['name'])))
        conn.commit()

        # Automatic validation for channels with unknown status (NULL)
        # OR full validation if user checked the box
        query = "SELECT * FROM channels" if validate else "SELECT * FROM channels WHERE is_working IS NULL"
        df_to_check = pd.read_sql_query(query, conn)
        
        if not df_to_check.empty:
            channels_to_check = df_to_check.to_dict('records')
            logger.info(f"Validating {len(channels_to_check)} channels...")
            
            with ThreadPoolExecutor(max_workers=50) as executor:
                results = list(executor.map(_check_stream_status, channels_to_check))
            
            # Update results back to DB
            for original, result in zip(channels_to_check, results):
                status = 1 if result else 0
                cursor.execute("UPDATE channels SET is_working = ? WHERE url = ?", (status, original['url']))
            conn.commit()

        # Run EPG mapping after sync
        _auto_map_epg_ids()

        conn.close()
        return {"status": "success", "message": "Playlist processed, validated, and EPG IDs mapped."}
    except Exception as e:
        logger.exception("M3U sync failed")
        return {"status": "error", "message": str(e)}

def _get_filtered_data(search_query="", category_query="", master_only="false", status_filter="all"):
    if not os.path.exists(DB_PATH):
        return pd.DataFrame(), "Database not found."

    sql = """
    SELECT name, clean_name, group_title as category, logo, url, is_working, is_master, tvg_id, epg_id
    FROM channels
    WHERE (name LIKE :name OR tvg_id LIKE :name)
      AND (:category_query = '' OR group_title LIKE :category)
      AND (:master_only = 'false' OR is_master = 1)
    """
    
    if status_filter == "working":
        sql += " AND is_working = 1"
    elif status_filter == "broken":
        sql += " AND is_working = 0"

    params = {
        'name': f'%{search_query}%',
        'category': f'%{category_query}%',
        'category_query': category_query,
        'master_only': master_only
    }
    try:
        conn = sqlite3.connect(DB_PATH)
        filtered_df = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return filtered_df.fillna(""), ""
    except Exception as e:
        return pd.DataFrame(), f"Query error: {e}"

@app.route('/')
def index():
    """Renders the main web UI page."""
    return render_template('index.html')

@app.route('/api/sync_db', methods=['POST'])
def sync_db():
    """API endpoint to trigger database synchronization."""
    data = request.get_json() or {}
    m3u_url = data.get('url', DEFAULT_M3U_URL)
    validate = data.get('validate', False)
    logger.info(f"Manual sync triggered for: {m3u_url} (Validate: {validate})")
    result = _sync_and_consolidate_data(m3u_url, validate=validate)
    return jsonify(result)

@app.route('/api/filter_data', methods=['GET'])
def filter_data():
    """API endpoint to get filtered channel data."""
    logger.debug(f"Filter data request: {request.args}")
    search_query = request.args.get('search', '')
    category_query = request.args.get('category', '')
    master_only = request.args.get('master_only', 'false')
    status_filter = request.args.get('status', 'all')

    df, error_message = _get_filtered_data(search_query, category_query, master_only, status_filter)

    if error_message:
        return jsonify({"status": "error", "message": error_message}), 500

    # Limit display to first 500 rows for performance in UI
    display_df = df.head(500)
    
    return jsonify({
        "status": "success",
        "data": display_df.to_dict(orient='records'),
        "total_filtered": len(df),
        "displayed_count": len(display_df)
    })

@app.route('/api/categories', methods=['GET'])
def get_categories():
    """API endpoint to get all unique categories available in the database."""
    master_only = request.args.get('master_only', 'false')
    if not os.path.exists(DB_PATH):
        return jsonify({"status": "success", "categories": []})
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        query = "SELECT DISTINCT group_title FROM channels WHERE group_title IS NOT NULL AND group_title != ''"
        if master_only == 'true':
            query += " AND is_master = 1"
        query += " ORDER BY group_title"
        
        cursor.execute(query)
        categories = [row[0] for row in cursor.fetchall()]
        conn.close()
        return jsonify({"status": "success", "categories": categories})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/add_to_master', methods=['POST'])
def add_to_master():
    """Adds the current filtered selection to the master list (working channels only)."""
    data = request.get_json() or {}
    search = data.get('search', '')
    category = data.get('category', '')
    
    df, error = _get_filtered_data(search, category)
    if error or df.empty:
        return jsonify({"status": "error", "message": "No channels found to add."})

    urls = df[df['is_working'] == 1]['url'].tolist()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executemany("UPDATE channels SET is_master = 1 WHERE url = ?", [(u,) for u in urls])
    conn.commit()
    count = cursor.rowcount
    conn.close()
    
    return jsonify({"status": "success", "message": f"Added {count} working channels to Master List."})

@app.route('/api/remove_from_master', methods=['POST'])
def remove_from_master():
    """Removes the current filtered selection from the master list."""
    data = request.get_json() or {}
    search = data.get('search', '')
    category = data.get('category', '')
    
    # We filter within the master list only
    df, error = _get_filtered_data(search, category, master_only='true')
    if error or df.empty:
        return jsonify({"status": "error", "message": "No master channels found to remove."})

    urls = df['url'].tolist()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executemany("UPDATE channels SET is_master = 0 WHERE url = ?", [(u,) for u in urls])
    conn.commit()
    count = cursor.rowcount
    conn.close()
    
    return jsonify({"status": "success", "message": f"Removed {count} channels from Master List."})

@app.route('/api/export_csv', methods=['GET'])
def export_csv():
    """API endpoint to export filtered data as a CSV file."""
    logger.info("CSV Export initiated.")
    search_query = request.args.get('search', '')
    category_query = request.args.get('category', '')

    df, error_message = _get_filtered_data(search_query, category_query)

    if error_message:
        return jsonify({"status": "error", "message": error_message}), 500

    if df.empty:
        return jsonify({"status": "warning", "message": "No data to export."}), 200

    # Create a CSV in memory
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)

    return send_file(
        io.BytesIO(csv_buffer.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name='iptv_filtered_data.csv'
    )

@app.route('/api/export_m3u', methods=['GET'])
def export_m3u():
    """API endpoint to export the master list as an M3U playlist."""
    logger.info("M3U Export initiated.")
    
    # We fetch the full master list for export
    df, error_message = _get_filtered_data(master_only='true')

    if error_message:
        return jsonify({"status": "error", "message": error_message}), 500

    if df.empty:
        return jsonify({"status": "warning", "message": "Master list is empty."}), 200

    # Build the M3U file content
    m3u_content = "#EXTM3U\n"
    for _, row in df.iterrows():
        # Prioritize mapped epg_id over the original tvg_id from the playlist
        final_id = row.get("epg_id") or row.get("tvg_id") or ""
        # Use clean_name for the display name to match EPG providers
        display_name = row.get("clean_name") or row["name"]
        m3u_content += f'#EXTINF:-1 tvg-id="{final_id}" tvg-logo="{row["logo"]}" group-title="{row["category"]}",{display_name}\n'
        m3u_content += f'{row["url"]}\n'

    return send_file(
        io.BytesIO(m3u_content.encode('utf-8')),
        mimetype='application/x-mpegurl',
        as_attachment=True,
        download_name='master_playlist.m3u'
    )

if __name__ == '__main__':
    _initialize_db()
    # Run initial sync only if database was just created (empty table)
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT count(*) FROM channels").fetchone()[0]
    conn.close()
    if count == 0:
        _sync_and_consolidate_data(DEFAULT_M3U_URL)
    app.run(debug=True, use_reloader=False)