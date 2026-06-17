from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import re
import json
import os
import decimal
import datetime
import sys
import io

# ป้องกัน charmap error บน Windows
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def _resource_path(relative):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative)


def _base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


MAX_WORKERS = 6  # parallel DB connections per side

app = Flask(__name__, template_folder=_resource_path('templates'))
CONFIG_FILE = os.path.join(_base_dir(), 'config.json')
MAX_DISPLAY_RECORDS = 500


_ZERO_DATES = {'0000-00-00 00:00:00', '0000-00-00', '0000-00-00 00:00', '00:00:00'}

def _clean_str_for_pg(s):
    """ตัด character ที่ไม่อยู่ใน WIN874 (Thai Windows) ออก"""
    if not isinstance(s, str):
        return s
    s = s.replace('�', '').replace('\x00', '')
    try:
        s.encode('cp874')
        return s
    except (UnicodeEncodeError, LookupError):
        return s.encode('cp874', errors='ignore').decode('cp874')


_MIN_DATETIME = datetime.datetime(1900, 1, 1, 0, 0, 0)
_MIN_DATE     = datetime.date(1900, 1, 1)


def _sanitize_pg(val):
    """แปลง MySQL zero-date และค่าที่ PostgreSQL ไม่รับ → 1900-01-01 / clean"""
    if val is None:
        return None
    if isinstance(val, str):
        val = _clean_str_for_pg(val)
        s = val.strip()
        if s in _ZERO_DATES or s.startswith('0000-'):
            # ใช้ 1900-01-01 แทน None เพื่อหลีกเลี่ยง NOT NULL violation
            return _MIN_DATETIME
        return val
    if isinstance(val, (bytes, bytearray, memoryview)):
        return bytes(val)  # keep binary as-is for BYTEA columns
    if isinstance(val, datetime.datetime) and val.year < 1:
        return _MIN_DATETIME
    if isinstance(val, datetime.date) and val.year < 1:
        return _MIN_DATE
    return val


def _is_binary_type(type_raw):
    """ตรวจว่า column type เป็น binary/blob หรือไม่"""
    t = (type_raw or '').lower()
    return any(x in t for x in ('blob', 'binary', 'bytea', 'varbinary'))


def _vals_equal(a, b):
    """เปรียบเทียบค่าของสองฟิล (รองรับ None, Decimal, datetime, bytes)"""
    if a == b:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (bytes, bytearray, memoryview)) or isinstance(b, (bytes, bytearray, memoryview)):
        ba = bytes(a) if isinstance(a, (bytes, bytearray, memoryview)) else None
        bb = bytes(b) if isinstance(b, (bytes, bytearray, memoryview)) else None
        return ba == bb
    return str(a).strip() == str(b).strip()


def make_serializable(obj):
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [make_serializable(v) for v in obj]
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, datetime.time):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        total = int(obj.total_seconds())
        h, rem = divmod(abs(total), 3600)
        m, s = divmod(rem, 60)
        return f'{"-" if total < 0 else ""}{h:02d}:{m:02d}:{s:02d}'
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        n = len(obj) if not isinstance(obj, memoryview) else obj.nbytes
        return f'[BINARY: {n:,} bytes]'
    return obj


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'source': {'type': 'postgresql', 'host': 'localhost', 'port': 5432,
                   'database': '', 'username': '', 'password': ''},
        'destination': {'type': 'postgresql', 'host': 'localhost', 'port': 5432,
                        'database': '', 'username': '', 'password': ''}
    }


def load_dst_config():
    """โหลดเฉพาะ config ของปลายทาง — ใช้กับ M9/M10 ที่ไม่ยุ่งกับต้นทาง"""
    cfg = load_config()
    return cfg.get('destination', {
        'type': 'postgresql', 'host': 'localhost', 'port': 5432,
        'database': '', 'username': '', 'password': ''
    })


def save_config_to_file(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def get_connection(db_config):
    db_type = db_config.get('type', 'postgresql')
    host = db_config.get('host', 'localhost')
    port = int(db_config.get('port', 5432))
    database = db_config.get('database', '')
    username = db_config.get('username', '')
    password = db_config.get('password', '')

    if db_type == 'postgresql':
        import psycopg2
        conn = psycopg2.connect(
            host=host, port=port, database=database,
            user=username, password=password,
            connect_timeout=10,
            client_encoding='UTF8'
        )
        return conn, 'postgresql'
    elif db_type == 'mysql':
        import pymysql
        import pymysql.converters
        import pymysql.constants.FIELD_TYPE as FT

        # แปลง zero date → 1900-01-01 แทนที่จะเป็น None
        def _safe_date_conv(s):
            if not s or str(s).startswith('0000'):
                return datetime.date(1900, 1, 1)
            try:
                parts = str(s).split('-')
                return datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
            except Exception:
                return datetime.date(1900, 1, 1)

        def _safe_datetime_conv(s):
            if not s or str(s).startswith('0000'):
                return datetime.datetime(1900, 1, 1, 0, 0, 0)
            try:
                return pymysql.converters.convert_datetime(s)
            except Exception:
                return datetime.datetime(1900, 1, 1, 0, 0, 0)

        conv = pymysql.converters.conversions.copy()
        conv[FT.DATE]      = _safe_date_conv
        conv[FT.NEWDATE]   = _safe_date_conv
        conv[FT.DATETIME]  = _safe_datetime_conv
        conv[FT.TIMESTAMP] = _safe_datetime_conv

        conn = pymysql.connect(
            host=host, port=port, database=database,
            user=username, password=password,
            connect_timeout=10,
            read_timeout=300,
            write_timeout=300,
            cursorclass=pymysql.cursors.DictCursor,
            charset='utf8mb4',
            conv=conv,
        )
        return conn, 'mysql'
    raise ValueError(f"Unsupported database type: {db_type}")


def get_tables(conn, db_type, db_name):
    cur = conn.cursor()
    if db_type == 'postgresql':
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        tables = [row[0] for row in cur.fetchall()]
    else:
        # SHOW TABLES เร็วกว่า information_schema มาก
        cur.execute('SHOW TABLES')
        rows = cur.fetchall()
        key = f'Tables_in_{db_name}'
        tables = sorted([
            row[key] if isinstance(row, dict) and key in row
            else (list(row.values())[0] if isinstance(row, dict) else row[0])
            for row in rows
        ])
    cur.close()
    return tables


def get_primary_keys(conn, db_type, table_name, db_name):
    cur = conn.cursor()
    if db_type == 'postgresql':
        cur.execute("""
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = 'public'
              AND tc.table_name = %s
            ORDER BY kcu.ordinal_position
        """, (table_name,))
        pks = [row[0] for row in cur.fetchall()]
    else:
        # SHOW KEYS เร็วกว่า information_schema มาก
        cur.execute(f'SHOW KEYS FROM `{table_name}` WHERE Key_name = %s', ('PRIMARY',))
        rows = cur.fetchall()
        rows_sorted = sorted(rows, key=lambda r: r['Seq_in_index'] if isinstance(r, dict) else r[3])
        pks = [row['Column_name'] if isinstance(row, dict) else row[4] for row in rows_sorted]
    cur.close()
    return pks


def get_record_count(conn, db_type, table_name):
    try:
        cur = conn.cursor()
        if db_type == 'postgresql':
            cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
            count = cur.fetchone()[0]
        else:
            cur.execute(f'SELECT COUNT(*) as cnt FROM `{table_name}`')
            row = cur.fetchone()
            count = row['cnt'] if isinstance(row, dict) else row[0]
        cur.close()
        return int(count)
    except Exception:
        return -1


def q(name, db_type):
    return f'"{name}"' if db_type == 'postgresql' else f'`{name}`'


def _count_table(db_config, table_name):
    """Thread worker: นับ record + ดึงชื่อ column ในครั้งเดียว"""
    try:
        conn, db_type = get_connection(db_config)
        count    = get_record_count(conn, db_type, table_name)
        col_set  = {c['column'].lower()
                    for c in get_columns(conn, db_type, table_name, db_config.get('database',''))}
        conn.close()
        return count, col_set
    except Exception:
        return -1, set()


def _detect_date_col(cols_info):
    """หาชื่อคอลัมน์วันที่จากรายการ column info (ใช้สำหรับ filter 3 เดือน)"""
    PREFERRED = ['vstdate', 'date_serv', 'regdate', 'bdate', 'order_date',
                 'apdate', 'visit_date', 'created_at', 'updated_at', 'appoint_date']
    col_map = {c['column'].lower(): c['column'] for c in cols_info}
    for name in PREFERRED:
        if name in col_map:
            return col_map[name]
    for lower_name, orig_name in col_map.items():
        if 'date' in lower_name:
            return orig_name
    return None


def _fetch_pks_filtered(db_config, table_name, pks, result_dict, key,
                        date_col=None, cutoff=None):
    """Thread worker: ดึง PK set พร้อมกรองวันที่ถ้ามี"""
    try:
        conn, db_type = get_connection(db_config)
        eff_pks = [pk.lower() if db_type == 'postgresql' else pk for pk in pks]
        eff_tbl = table_name.lower() if db_type == 'postgresql' else table_name
        pk_cols = ', '.join([q(pk, db_type) for pk in eff_pks])
        if date_col and cutoff:
            eff_date = date_col.lower() if db_type == 'postgresql' else date_col
            sql    = (f'SELECT {pk_cols} FROM {q(eff_tbl, db_type)}'
                      f' WHERE {q(eff_date, db_type)} >= %s')
            params = (cutoff,)
        else:
            sql    = f'SELECT {pk_cols} FROM {q(eff_tbl, db_type)}'
            params = ()
        cur = conn.cursor()
        cur.execute(sql, params)
        result = set()
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for row in rows:
                if isinstance(row, dict):
                    pk_tuple = tuple(row[pk] for pk in eff_pks)
                else:
                    pk_tuple = tuple(row) if len(eff_pks) > 1 else (row[0],)
                result.add(pk_tuple)
        cur.close()
        conn.close()
        result_dict[key] = result
    except Exception:
        result_dict[key] = set()


def _fetch_pks(db_config, table_name, pks, result_dict, key):
    """Thread worker: ดึง PK set ด้วย connection แยก"""
    try:
        conn, db_type = get_connection(db_config)
        result_dict[key] = get_all_pks(conn, db_type, table_name, pks)
        conn.close()
    except Exception:
        result_dict[key] = set()


def _count_field_diffs_for_table(config, table_name, cutoff_date, sample_size=300):
    """นับ record ที่ต้องอัพเดทโดยสุ่มตัวอย่าง sample_size record ล่าสุด (เร็ว)"""
    try:
        src_conn, src_type = get_connection(config['source'])
        dst_conn, dst_type = get_connection(config['destination'])

        if not table_exists(dst_conn, dst_type, table_name, config['destination']['database']):
            src_conn.close(); dst_conn.close()
            return 0

        pks = get_primary_keys(src_conn, src_type, table_name, config['source']['database'])
        if not pks:
            src_conn.close(); dst_conn.close()
            return 0

        src_cols   = get_columns(src_conn, src_type, table_name, config['source']['database'])
        dst_cols   = get_columns(dst_conn, dst_type, table_name, config['destination']['database'])
        date_col   = _detect_date_col(src_cols)
        dst_col_set = {c['column'].lower() for c in dst_cols}
        compare_cols = [c['column'] for c in src_cols
                        if c['column'].lower() in dst_col_set
                        and not _is_binary_type(c['type_raw'])]

        # ดึงเฉพาะ sample_size PKs ล่าสุดจากต้นทาง (ไม่ต้องดึงทั้งหมด)
        eff_pks_src = [pk.lower() if src_type == 'postgresql' else pk for pk in pks]
        eff_tbl     = table_name.lower() if src_type == 'postgresql' else table_name
        pk_select   = ', '.join(q(pk, src_type) for pk in eff_pks_src)

        if date_col and cutoff_date:
            eff_date = date_col.lower() if src_type == 'postgresql' else date_col
            sql    = (f'SELECT {pk_select} FROM {q(eff_tbl, src_type)}'
                      f' WHERE {q(eff_date, src_type)} >= %s'
                      f' ORDER BY {q(eff_date, src_type)} DESC LIMIT {sample_size}')
            params = (cutoff_date,)
        else:
            sql    = f'SELECT {pk_select} FROM {q(eff_tbl, src_type)} LIMIT {sample_size}'
            params = ()

        cur = src_conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()

        if not rows:
            src_conn.close(); dst_conn.close()
            return 0

        # แปลงเป็น list of PK tuples
        sample_pks = []
        for row in rows:
            if isinstance(row, dict):
                pk_tuple = tuple(row[pk] for pk in eff_pks_src)
            else:
                pk_tuple = tuple(row) if len(pks) > 1 else (row[0],)
            sample_pks.append(pk_tuple)

        # ดึง records จากทั้งสองฝั่งด้วย PK IN (...)
        src_recs, _ = fetch_records_by_pks(src_conn, src_type, table_name, pks, sample_pks)
        dst_recs, _ = fetch_records_by_pks(dst_conn, dst_type, table_name, pks, sample_pks)

        eff_pks_dst = [pk.lower() if dst_type == 'postgresql' else pk for pk in pks]
        src_by_pk = {tuple(r.get(pk) for pk in eff_pks_src): r for r in src_recs}
        dst_by_pk = {tuple(r.get(pk) for pk in eff_pks_dst): r for r in dst_recs}

        diff_count = 0
        for pk_key, src_rec in src_by_pk.items():
            if pk_key not in dst_by_pk:
                continue
            dst_rec = dst_by_pk[pk_key]
            for col in compare_cols:
                src_val = src_rec.get(col.lower() if src_type == 'postgresql' else col)
                if src_val is None:
                    continue
                if not _vals_equal(src_val,
                                   dst_rec.get(col.lower() if dst_type == 'postgresql' else col)):
                    diff_count += 1
                    break

        src_conn.close(); dst_conn.close()
        return diff_count
    except Exception:
        return -1


def get_tables_with_pks(conn, db_type, table_names, db_name):
    """คืน set ของตารางที่มี Primary Key (batch query เดียว)"""
    if not table_names:
        return set()
    cur = conn.cursor()
    if db_type == 'postgresql':
        cur.execute("""
            SELECT DISTINCT tc.table_name
            FROM information_schema.table_constraints tc
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = 'public'
              AND tc.table_name = ANY(%s)
        """, (list(table_names),))
    else:
        ph = ','.join(['%s'] * len(table_names))
        cur.execute(f"""
            SELECT DISTINCT TABLE_NAME
            FROM information_schema.TABLE_CONSTRAINTS
            WHERE CONSTRAINT_TYPE = 'PRIMARY KEY'
              AND TABLE_SCHEMA = %s
              AND TABLE_NAME IN ({ph})
        """, [db_name] + list(table_names))
    result = {(list(r.values())[0] if isinstance(r, dict) else r[0]) for r in cur.fetchall()}
    cur.close()
    return result


def table_exists(conn, db_type, table_name, db_name):
    cur = conn.cursor()
    if db_type == 'postgresql':
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
                  AND table_type = 'BASE TABLE'
            )
        """, (table_name,))
        exists = bool(cur.fetchone()[0])
    else:
        # SHOW TABLES LIKE เร็วกว่า information_schema มาก
        cur.execute('SHOW TABLES LIKE %s', (table_name,))
        exists = cur.fetchone() is not None
    cur.close()
    return exists


def _find_missing_pks_fast(src_conn, src_type, dst_conn, dst_type,
                           table_name, pks, config, limit):
    """หา missing PKs แบบ batch check — ไม่ดึง dest PKs ทั้งหมด ใช้ IN query แทน"""
    eff_pks_dst = [p.lower() if dst_type == 'postgresql' else p for p in pks]
    pk_cols_src = ', '.join([q(p, src_type) for p in pks])
    tbl_src     = q(table_name, src_type)
    tbl_dst_name = table_name.lower() if dst_type == 'postgresql' else table_name
    BATCH       = 2000
    missing_pks = []

    src_cur = src_conn.cursor()
    src_cur.execute(f'SELECT {pk_cols_src} FROM {tbl_src}')

    while len(missing_pks) < limit:
        rows = src_cur.fetchmany(BATCH)
        if not rows:
            break

        # แปลง source batch เป็น list of PK tuples
        src_batch = []
        for row in rows:
            if isinstance(row, dict):
                k = tuple(row.get(p) for p in pks)
            else:
                k = (row[0],) if len(pks) == 1 else tuple(row)
            src_batch.append(k)

        if not src_batch:
            continue

        # Batch check ว่า PK ไหนมีใน destination
        try:
            dst_cur = dst_conn.cursor()
            if len(pks) == 1:
                ph  = ','.join(['%s'] * len(src_batch))
                col = f'"{eff_pks_dst[0]}"' if dst_type == 'postgresql' else f'`{pks[0]}`'
                dst_cur.execute(
                    f'SELECT {col} FROM {q(tbl_dst_name, dst_type)} WHERE {col} IN ({ph})',
                    [k[0] for k in src_batch])
                found = {(row[0] if not isinstance(row, dict) else list(row.values())[0],)
                         for row in dst_cur.fetchall()}
            else:
                # Composite PK: check row by row (less common)
                found = set()
                for k in src_batch:
                    whr = ' AND '.join([
                        f'"{eff_pks_dst[i]}"=%s' if dst_type == 'postgresql'
                        else f'`{pks[i]}`=%s'
                        for i in range(len(pks))])
                    dst_cur.execute(f'SELECT 1 FROM {q(tbl_dst_name, dst_type)} WHERE {whr}', list(k))
                    if dst_cur.fetchone():
                        found.add(k)
            dst_cur.close()

            for k in src_batch:
                if k not in found:
                    missing_pks.append(k)
                if len(missing_pks) >= limit:
                    break
        except Exception:
            break

    src_cur.close()
    return missing_pks, len(missing_pks)


def get_all_pks(conn, db_type, table_name, pks):
    eff_pks = [pk.lower() if db_type == 'postgresql' else pk for pk in pks]
    pk_cols = ', '.join([q(pk, db_type) for pk in eff_pks])
    cur = conn.cursor()
    tbl = table_name.lower() if db_type == 'postgresql' else table_name
    cur.execute(f'SELECT {pk_cols} FROM {q(tbl, db_type)}')
    result = set()
    while True:
        try:
            rows = cur.fetchmany(5000)   # batch fetch เร็วกว่า fetchone มาก
            if not rows:
                break
            for row in rows:
                if isinstance(row, dict):
                    pk_tuple = tuple(row[pk] for pk in eff_pks)
                else:
                    pk_tuple = tuple(row) if len(eff_pks) > 1 else (row[0],)
                result.add(pk_tuple)
        except UnicodeDecodeError:
            pass  # ข้าม batch นี้ แล้วดึง batch ต่อไป
    cur.close()
    return result


def fetch_records_by_pks(conn, db_type, table_name, pks, pk_values):
    if not pk_values:
        return [], []

    # PostgreSQL ใช้ lowercase column/table names
    eff_pks = [pk.lower() if db_type == 'postgresql' else pk for pk in pks]
    eff_tbl = table_name.lower() if db_type == 'postgresql' else table_name

    params = []
    if len(eff_pks) == 1:
        placeholders = ', '.join(['%s'] * len(pk_values))
        where = f'{q(eff_pks[0], db_type)} IN ({placeholders})'
        params = [v[0] for v in pk_values]
    else:
        conditions = []
        for vals in pk_values:
            cond = ' AND '.join([f'{q(eff_pks[i], db_type)} = %s' for i in range(len(eff_pks))])
            conditions.append(f'({cond})')
            params.extend(vals)
        where = ' OR '.join(conditions)

    if db_type == 'postgresql':
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()

    cur.execute(f'SELECT * FROM {q(eff_tbl, db_type)} WHERE {where}', params)

    col_names = []
    if cur.description:
        col_names = [d[0] for d in cur.description]

    records = []
    try:
        rows = cur.fetchall()   # fetchall เร็วที่สุด
        for row in rows:
            if isinstance(row, dict):
                records.append(dict(row))
            else:
                records.append(dict(zip(col_names, row)))
    except UnicodeDecodeError:
        # fallback: ดึงทีละแถวถ้า fetchall ล้มเหลว
        while True:
            try:
                row = cur.fetchone()
                if row is None:
                    break
                if isinstance(row, dict):
                    records.append(dict(row))
                else:
                    records.append(dict(zip(col_names, row)))
            except UnicodeDecodeError:
                pass

    cur.close()
    return records, col_names


@app.after_request
def add_no_cache_headers(response):
    # ป้องกัน browser cache หน้าเก่าไว้ข้ามเวอร์ชัน exe (ทำให้ JS fix ใหม่ๆ ไม่ถูกโหลด)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(load_config())


@app.route('/api/config', methods=['POST'])
def save_config_route():
    config = request.json
    save_config_to_file(config)
    return jsonify({'status': 'ok'})


@app.route('/api/test-connection', methods=['POST'])
def test_connection():
    db_config = request.json
    try:
        conn, _ = get_connection(db_config)
        conn.close()
        return jsonify({'status': 'ok', 'message': 'เชื่อมต่อสำเร็จ'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400


@app.route('/api/tables', methods=['GET'])
def get_tables_route():
    config = load_config()
    try:
        conn, db_type = get_connection(config['source'])
        tables = get_tables(conn, db_type, config['source']['database'])
        conn.close()
        prefixes = sorted(set(t[0].upper() for t in tables if t))
        return jsonify({'tables': tables, 'prefixes': prefixes})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400


@app.route('/api/compare-table', methods=['POST'])
def compare_table():
    data = request.json
    table_name = data.get('table')
    fetch_all  = data.get('fetch_all', False)   # True = ดึงทั้งหมด ไม่จำกัด
    config = load_config()

    try:
        src_conn, src_type = get_connection(config['source'])
        dst_conn, dst_type = get_connection(config['destination'])

        # ถ้าตารางไม่มีในปลายทาง แจ้งเตือนและหยุด ไม่ต้องเปรียบเทียบ
        if not table_exists(dst_conn, dst_type, table_name, config['destination']['database']):
            src_count_only = get_record_count(src_conn, src_type, table_name)
            src_conn.close()
            dst_conn.close()
            return jsonify({
                'table': table_name,
                'dst_table_missing': True,
                'source_count': src_count_only,
                'destination_count': -1,
                'diff': 0,
                'missing_records': [], 'primary_keys': [], 'columns': [],
                'total_missing': 0, 'warning': ''
            })

        # นับ record ทั้ง 2 ฝั่งพร้อมกัน
        counts = {}
        t_src = threading.Thread(target=lambda: counts.__setitem__(
            'src', get_record_count(src_conn, src_type, table_name)))
        t_dst = threading.Thread(target=lambda: counts.__setitem__(
            'dst', get_record_count(dst_conn, dst_type, table_name)))
        t_src.start(); t_dst.start()
        t_src.join();  t_dst.join()
        src_count = counts.get('src', -1)
        dst_count = counts.get('dst', -1)

        result = {
            'table': table_name,
            'source_count': src_count,
            'destination_count': dst_count,
            'diff': src_count - max(dst_count, 0),
            'missing_records': [],
            'primary_keys': [],
            'columns': [],
            'total_missing': 0,
            'warning': ''
        }

        if src_count > dst_count:
            pks = get_primary_keys(src_conn, src_type, table_name, config['source']['database'])
            result['primary_keys'] = pks

            if not pks:
                result['warning'] = 'ตารางนี้ไม่มี Primary Key ไม่สามารถระบุ record ที่ขาดได้'
            else:
                limit = None if fetch_all else MAX_DISPLAY_RECORDS

                if not fetch_all and (src_count - dst_count) <= 50000:
                    # Fast path: batch approach — ไม่ดึง PK ทั้งหมด แค่หา missing ที่ต้องการ
                    display_pks, total_missing = _find_missing_pks_fast(
                        src_conn, src_type, dst_conn, dst_type,
                        table_name, pks, config, limit or MAX_DISPLAY_RECORDS)
                    result['total_missing'] = total_missing
                else:
                    # Full scan สำหรับตาราง fetch_all หรือ diff ใหญ่มาก
                    pk_results = {}
                    t1 = threading.Thread(target=_fetch_pks,
                        args=(config['source'], table_name, pks, pk_results, 'src'))
                    t2 = threading.Thread(target=_fetch_pks,
                        args=(config['destination'], table_name, pks, pk_results, 'dst'))
                    t1.start(); t2.start()
                    t1.join();  t2.join()
                    src_pk_set = pk_results.get('src', set())
                    dst_pk_set = pk_results.get('dst', set())
                    missing_all = src_pk_set - dst_pk_set
                    result['total_missing'] = len(missing_all)
                    display_pks = list(missing_all) if limit is None else list(missing_all)[:limit]

                if display_pks:
                    records, col_names = fetch_records_by_pks(
                        src_conn, src_type, table_name, pks, display_pks)
                    result['missing_records'] = make_serializable(records)
                    result['columns'] = col_names

        # ตรวจสอบฟิลที่ขาดในปลายทาง (เฉพาะตารางที่มีในปลายทาง)
        try:
            src_col_names = {c['column'].lower()
                             for c in get_columns(src_conn, src_type, table_name,
                                                  config['source']['database'])}
            dst_col_names = {c['column'].lower()
                             for c in get_columns(dst_conn, dst_type, table_name,
                                                  config['destination']['database'])}
            missing_col_count = len(src_col_names - dst_col_names)
        except Exception:
            missing_col_count = 0

        result['has_missing_cols']  = missing_col_count > 0
        result['missing_col_count'] = missing_col_count

        src_conn.close()
        dst_conn.close()

        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


@app.route('/api/field-diff', methods=['POST'])
def field_diff():
    """เปรียบเทียบค่าของแต่ละฟิลสำหรับ record ที่มี PK ตรงกัน (3 เดือนย้อนหลัง)
    คืนเฉพาะ record ที่มีอย่างน้อย 1 ฟิลต่างกัน"""
    data        = request.json
    table_name  = data.get('table')
    config      = load_config()
    MAX_DISPLAY = 500

    # cutoff = 3 เดือนย้อนหลัง
    cutoff_date = (datetime.date.today() - datetime.timedelta(days=90)).strftime('%Y-%m-%d')

    try:
        src_conn, src_type = get_connection(config['source'])
        dst_conn, dst_type = get_connection(config['destination'])

        if not table_exists(dst_conn, dst_type, table_name, config['destination']['database']):
            src_conn.close(); dst_conn.close()
            return jsonify({'status': 'error',
                            'message': f'ตาราง "{table_name}" ไม่มีในปลายทาง'})

        pks = get_primary_keys(src_conn, src_type, table_name, config['source']['database'])
        if not pks:
            src_conn.close(); dst_conn.close()
            return jsonify({'status': 'error',
                            'message': 'ไม่พบ Primary Key สำหรับตารางนี้'})

        # หา columns และ auto-detect date column สำหรับ filter
        src_cols    = get_columns(src_conn, src_type, table_name, config['source']['database'])
        dst_cols    = get_columns(dst_conn, dst_type, table_name, config['destination']['database'])
        date_col    = _detect_date_col(src_cols)

        dst_col_set       = {c['column'].lower() for c in dst_cols}
        compare_col_names = [c['column'] for c in src_cols
                             if c['column'].lower() in dst_col_set
                             and not _is_binary_type(c['type_raw'])]  # ข้าม binary/image

        # ดึง PK sets จากต้นทาง (กรอง 3 เดือน) และปลายทาง (ทั้งหมด) พร้อมกัน
        pk_results = {}
        t1 = threading.Thread(target=_fetch_pks_filtered,
            args=(config['source'], table_name, pks, pk_results, 'src',
                  date_col, cutoff_date if date_col else None))
        t2 = threading.Thread(target=_fetch_pks,
            args=(config['destination'], table_name, pks, pk_results, 'dst'))
        t1.start(); t2.start()
        t1.join();  t2.join()

        src_pks      = pk_results.get('src', set())
        dst_pks      = pk_results.get('dst', set())
        common_pks   = list(src_pks & dst_pks)
        common_count = len(common_pks)

        if not common_pks:
            src_conn.close(); dst_conn.close()
            return jsonify({'status': 'ok', 'diff_records': [], 'columns': compare_col_names,
                            'pks': pks, 'total': 0, 'common_count': 0,
                            'date_col': date_col, 'cutoff': cutoff_date if date_col else None})

        # ชื่อคอลัมน์ PK ที่ใช้ index record ตาม db_type
        eff_pks_src = [pk.lower() if src_type == 'postgresql' else pk for pk in pks]
        eff_pks_dst = [pk.lower() if dst_type == 'postgresql' else pk for pk in pks]

        diff_records = []
        total_diff   = 0
        batch_size   = 200

        for i in range(0, len(common_pks), batch_size):
            batch = common_pks[i:i + batch_size]

            src_recs, _ = fetch_records_by_pks(
                src_conn, src_type, table_name, pks, batch)
            dst_recs, _ = fetch_records_by_pks(
                dst_conn, dst_type, table_name, pks, batch)

            src_by_pk = {tuple(r.get(pk) for pk in eff_pks_src): r for r in src_recs}
            dst_by_pk = {tuple(r.get(pk) for pk in eff_pks_dst): r for r in dst_recs}

            for pk_key, src_rec in src_by_pk.items():
                if pk_key not in dst_by_pk:
                    continue
                dst_rec = dst_by_pk[pk_key]

                diff_fields = []
                for col in compare_col_names:
                    col_src = col.lower() if src_type == 'postgresql' else col
                    col_dst = col.lower() if dst_type == 'postgresql' else col
                    src_val = src_rec.get(col_src)
                    if src_val is None:
                        continue
                    if not _vals_equal(src_val, dst_rec.get(col_dst)):
                        diff_fields.append(col)

                if diff_fields:
                    total_diff += 1
                    if len(diff_records) < MAX_DISPLAY:
                        diff_records.append({
                            'src':         make_serializable(src_rec),
                            'diff_fields': diff_fields
                        })

        src_conn.close()
        dst_conn.close()
        return jsonify({
            'status':       'ok',
            'diff_records': diff_records,
            'columns':      compare_col_names,
            'pks':          pks,
            'total':        total_diff,
            'common_count': common_count,
            'date_col':     date_col,
            'cutoff':       cutoff_date if date_col else None
        })

    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


@app.route('/api/field-diff-count', methods=['POST'])
def field_diff_count():
    """นับ record ที่ต้องอัพเดทสำหรับ 1 ตาราง (เร็ว — ใช้ sample 300 record ล่าสุด)"""
    data        = request.json
    table_name  = data.get('table', '')
    config      = load_config()
    cutoff_date = (datetime.date.today() - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
    count = _count_field_diffs_for_table(config, table_name, cutoff_date)
    return jsonify({'status': 'ok', 'count': count, 'table': table_name})


@app.route('/api/field-update', methods=['POST'])
def field_update():
    """อัพเดทค่าฟิลในปลายทางสำหรับ record ที่ PK ตรงกันแต่ค่าต่างกัน (3 เดือนย้อนหลัง)"""
    data             = request.json
    table_name       = data.get('table')
    selected_pks_raw = data.get('selected_pks')   # [[str_pk1, ...], ...] หรือ None
    config           = load_config()
    cutoff_date      = (datetime.date.today() - datetime.timedelta(days=90)).strftime('%Y-%m-%d')

    # แปลง selected_pks เป็น set of tuples (string) สำหรับ lookup เร็ว
    selected_set = None
    if selected_pks_raw is not None:
        selected_set = {tuple(str(v) if v is not None else '' for v in row)
                        for row in selected_pks_raw}

    def qcol(col, db_type):
        return f'"{col.lower()}"' if db_type == 'postgresql' else f'`{col}`'

    def qtbl(tbl, db_type):
        return f'"{tbl}"' if db_type == 'postgresql' else f'`{tbl}`'

    try:
        src_conn, src_type = get_connection(config['source'])
        dst_conn, dst_type = get_connection(config['destination'])

        if not table_exists(dst_conn, dst_type, table_name, config['destination']['database']):
            src_conn.close(); dst_conn.close()
            return jsonify({'status': 'error',
                            'message': f'ตาราง "{table_name}" ไม่มีในปลายทาง'})

        pks = get_primary_keys(src_conn, src_type, table_name, config['source']['database'])
        if not pks:
            src_conn.close(); dst_conn.close()
            return jsonify({'status': 'error', 'message': 'ไม่พบ Primary Key'})

        src_cols    = get_columns(src_conn, src_type, table_name, config['source']['database'])
        dst_cols    = get_columns(dst_conn, dst_type, table_name, config['destination']['database'])
        date_col    = _detect_date_col(src_cols)
        dst_col_set = {c['column'].lower() for c in dst_cols}
        pk_set_lower = {pk.lower() for pk in pks}

        compare_col_names = [c['column'] for c in src_cols
                             if c['column'].lower() in dst_col_set
                             and not _is_binary_type(c['type_raw'])]  # ข้าม binary/image

        pk_results = {}
        t1 = threading.Thread(target=_fetch_pks_filtered,
            args=(config['source'], table_name, pks, pk_results, 'src',
                  date_col, cutoff_date if date_col else None))
        t2 = threading.Thread(target=_fetch_pks,
            args=(config['destination'], table_name, pks, pk_results, 'dst'))
        t1.start(); t2.start()
        t1.join();  t2.join()

        common_pks = list(pk_results.get('src', set()) & pk_results.get('dst', set()))
        if not common_pks:
            src_conn.close(); dst_conn.close()
            return jsonify({'status': 'ok', 'updated': 0})

        eff_pks_src = [pk.lower() if src_type == 'postgresql' else pk for pk in pks]
        eff_pks_dst = [pk.lower() if dst_type == 'postgresql' else pk for pk in pks]

        dst_cursor    = dst_conn.cursor()
        updated_count = 0
        batch_size    = 200

        for i in range(0, len(common_pks), batch_size):
            batch = common_pks[i:i + batch_size]

            src_recs, _ = fetch_records_by_pks(src_conn, src_type, table_name, pks, batch)
            dst_recs, _ = fetch_records_by_pks(dst_conn, dst_type, table_name, pks, batch)

            src_by_pk = {tuple(r.get(pk) for pk in eff_pks_src): r for r in src_recs}
            dst_by_pk = {tuple(r.get(pk) for pk in eff_pks_dst): r for r in dst_recs}

            for pk_key, src_rec in src_by_pk.items():
                if pk_key not in dst_by_pk:
                    continue
                if selected_set is not None:
                    pk_key_str = tuple(str(v) if v is not None else '' for v in pk_key)
                    if pk_key_str not in selected_set:
                        continue
                dst_rec = dst_by_pk[pk_key]

                diff_fields = []
                for col in compare_col_names:
                    col_src = col.lower() if src_type == 'postgresql' else col
                    col_dst = col.lower() if dst_type == 'postgresql' else col
                    src_val = src_rec.get(col_src)
                    if src_val is None:
                        continue
                    if not _vals_equal(src_val, dst_rec.get(col_dst)):
                        diff_fields.append(col)

                update_fields = [f for f in diff_fields if f.lower() not in pk_set_lower]
                if not update_fields:
                    continue

                set_clause   = ', '.join(f'{qcol(f, dst_type)} = %s' for f in update_fields)
                where_clause = ' AND '.join(f'{qcol(pk, dst_type)} = %s' for pk in pks)
                query = (f'UPDATE {qtbl(table_name, dst_type)} '
                         f'SET {set_clause} WHERE {where_clause}')

                set_vals = [src_rec.get(f.lower() if src_type == 'postgresql' else f)
                            for f in update_fields]
                pk_vals  = [src_rec.get(pk.lower() if src_type == 'postgresql' else pk)
                            for pk in pks]

                dst_cursor.execute(query, set_vals + pk_vals)
                updated_count += dst_cursor.rowcount

        dst_conn.commit()
        src_conn.close()
        dst_conn.close()
        return jsonify({'status': 'ok', 'updated': updated_count})

    except Exception as e:
        import traceback
        try:
            dst_conn.rollback()
        except Exception:
            pass
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


@app.route('/api/sync-selected-pks', methods=['POST'])
def sync_selected_pks():
    """M9: นำเข้าเฉพาะ PK ที่ผู้ใช้เลือก (selected_pks) จากต้นทางไปปลายทาง"""
    data         = request.json
    table_name   = data.get('table')
    selected_raw = data.get('selected_pks', [])   # [[str_pk1,...], ...]
    config       = load_config()

    try:
        src_conn, src_type = get_connection(config['source'])
        dst_conn, dst_type = get_connection(config['destination'])

        pks = get_primary_keys(src_conn, src_type, table_name, config['source']['database'])
        if not pks:
            return jsonify({'status': 'error', 'message': 'ไม่พบ Primary Key'})

        # แปลง selected_pks จาก string → tuple ที่ใช้ได้กับ fetch_records_by_pks
        def _try_int(v):
            try: return int(v)
            except (TypeError, ValueError): return v

        selected_pks = []
        for row in selected_raw:
            if not row:   # ข้าม [] กรณี PK ว่างเปล่า (bug guard)
                continue
            selected_pks.append(tuple(_try_int(v) if v is not None else None for v in row))

        if not selected_pks:
            return jsonify({'status': 'ok', 'inserted': 0, 'skipped': 0,
                            'message': 'ไม่พบ PK ที่เลือก'})

        inserted = 0
        skipped  = 0
        batch_size = 500

        for i in range(0, len(selected_pks), batch_size):
            batch = selected_pks[i:i + batch_size]
            records, _ = fetch_records_by_pks(src_conn, src_type, table_name, pks, batch)

            if not records:
                continue

            # --- FAST PATH: batch insert ---
            try:
                cols_b = list(records[0].keys())
                if dst_type == 'postgresql':
                    from psycopg2.extras import execute_values as _ev
                    col_str_b = ', '.join(f'"{c.lower()}"' for c in cols_b)
                    vals_list = [[_sanitize_pg(rec.get(c)) for c in cols_b]
                                 for rec in records]
                    sql_b = (f'INSERT INTO "{table_name.lower()}" ({col_str_b}) '
                             f'VALUES %s ON CONFLICT DO NOTHING')
                    bcur = dst_conn.cursor()
                    _ev(bcur, sql_b, vals_list, page_size=100)
                    inserted += len(records)
                    bcur.close()
                else:
                    col_str_b = ', '.join(f'`{c}`' for c in cols_b)
                    val_str_b = ', '.join(['%s'] * len(cols_b))
                    sql_b = (f'INSERT IGNORE INTO `{table_name}` ({col_str_b}) '
                             f'VALUES ({val_str_b})')
                    vals_list = [[rec.get(c) for c in cols_b] for rec in records]
                    bcur = dst_conn.cursor()
                    bcur.executemany(sql_b, vals_list)
                    inserted += max(bcur.rowcount, 0)
                    bcur.close()
                dst_conn.commit()
                continue
            except Exception:
                try: dst_conn.rollback()
                except Exception: pass

            # --- SLOW PATH: per-record ---
            for rec in records:
                cols = list(rec.keys())
                vals = [rec[c] for c in cols]
                if dst_type == 'postgresql':
                    vals    = [_sanitize_pg(v) for v in vals]
                    col_str = ', '.join([f'"{c.lower()}"' for c in cols])
                    val_str = ', '.join(['%s'] * len(vals))
                    sql = (f'INSERT INTO "{table_name.lower()}" ({col_str}) '
                           f'VALUES ({val_str}) ON CONFLICT DO NOTHING')
                else:
                    col_str = ', '.join([f'`{c}`' for c in cols])
                    val_str = ', '.join(['%s'] * len(vals))
                    sql = f'INSERT IGNORE INTO `{table_name}` ({col_str}) VALUES ({val_str})'
                try:
                    cur = dst_conn.cursor()
                    cur.execute(sql, vals)
                    dst_conn.commit()
                    if cur.rowcount > 0:
                        inserted += 1
                    else:
                        skipped  += 1
                    cur.close()
                except Exception as e:
                    try: dst_conn.rollback()
                    except: pass
                    skipped += 1

        src_conn.close()
        dst_conn.close()
        return jsonify({'status': 'ok', 'inserted': inserted, 'skipped': skipped})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/sync-table', methods=['POST'])
def sync_table():
    data = request.json
    table_name = data.get('table')
    config = load_config()

    try:
        src_conn, src_type = get_connection(config['source'])
        dst_conn, dst_type = get_connection(config['destination'])

        if not table_exists(dst_conn, dst_type, table_name, config['destination']['database']):
            src_conn.close()
            dst_conn.close()
            return jsonify({'status': 'error',
                            'message': f'ตาราง "{table_name}" ไม่มีในปลายทาง ไม่สามารถ Sync ได้'}), 400

        pks = get_primary_keys(src_conn, src_type, table_name, config['source']['database'])
        if not pks:
            return jsonify({'status': 'error',
                            'message': 'ไม่พบ Primary Key สำหรับตารางนี้'}), 400

        src_pk_set = get_all_pks(src_conn, src_type, table_name, pks)
        try:
            dst_pk_set = get_all_pks(dst_conn, dst_type, table_name, pks)
        except Exception:
            dst_pk_set = set()

        def _pk_sort_key(t):
            v = t[0] if t else None
            if v is None:
                return (0, 0, '')   # None อยู่ท้ายสุดเมื่อ reverse=True
            if isinstance(v, (int, float)):
                return (1, v, '')
            return (1, 0, str(v))

        missing_pks = sorted(src_pk_set - dst_pk_set, key=_pk_sort_key, reverse=True)
        total_missing = len(missing_pks)

        if total_missing == 0:
            src_conn.close()
            dst_conn.close()
            return jsonify({'status': 'ok', 'inserted': 0,
                            'message': 'ไม่มีข้อมูลที่ต้องเพิ่ม'})

        inserted     = 0
        skipped_null = 0     # record ที่ข้ามเพราะ NULL ใน NOT NULL column
        error_details = []
        error_summary = {}
        batch_size = 500

        for i in range(0, len(missing_pks), batch_size):
            batch = missing_pks[i:i + batch_size]
            records, col_names = fetch_records_by_pks(
                src_conn, src_type, table_name, pks, batch)

            if not records:
                continue

            # --- FAST PATH: batch insert (ไม่ commit ทีละ row) ---
            try:
                cols_b = list(records[0].keys())
                if dst_type == 'postgresql':
                    from psycopg2.extras import execute_values as _ev
                    col_str_b = ', '.join(f'"{c.lower()}"' for c in cols_b)
                    vals_list = [[_sanitize_pg(rec.get(c)) for c in cols_b]
                                 for rec in records]
                    sql_b = (f'INSERT INTO "{table_name.lower()}" ({col_str_b}) '
                             f'VALUES %s ON CONFLICT DO NOTHING')
                    bcur = dst_conn.cursor()
                    _ev(bcur, sql_b, vals_list, page_size=100)
                    inserted += len(records)
                    bcur.close()
                else:
                    col_str_b = ', '.join(f'`{c}`' for c in cols_b)
                    val_str_b = ', '.join(['%s'] * len(cols_b))
                    sql_b = (f'INSERT IGNORE INTO `{table_name}` ({col_str_b}) '
                             f'VALUES ({val_str_b})')
                    vals_list = [[rec.get(c) for c in cols_b] for rec in records]
                    bcur = dst_conn.cursor()
                    bcur.executemany(sql_b, vals_list)
                    inserted += max(bcur.rowcount, 0)
                    bcur.close()
                dst_conn.commit()
                continue   # ข้าม slow-path ถ้า batch สำเร็จ
            except Exception:
                try: dst_conn.rollback()
                except Exception: pass
                # fall-through ไปยัง slow-path ด้านล่าง

            # --- SLOW PATH: per-record พร้อม error-handling เดิม ---
            for rec in records:
                cols = list(rec.keys())
                vals = [rec[c] for c in cols]
                pk_val = ', '.join(str(rec.get(p, '?')) for p in pks)

                if dst_type == 'postgresql':
                    vals = [_sanitize_pg(v) for v in vals]
                    col_str = ', '.join([f'"{c.lower()}"' for c in cols])
                    val_str = ', '.join(['%s'] * len(vals))
                    sql = (f'INSERT INTO "{table_name.lower()}" ({col_str}) '
                           f'VALUES ({val_str}) ON CONFLICT DO NOTHING')
                else:
                    col_str = ', '.join([f'`{c}`' for c in cols])
                    val_str = ', '.join(['%s'] * len(vals))
                    sql = (f'INSERT IGNORE INTO `{table_name}` ({col_str}) '
                           f'VALUES ({val_str})')

                try:
                    dst_cur = dst_conn.cursor()
                    dst_cur.execute(sql, vals)
                    dst_conn.commit()
                    inserted += dst_cur.rowcount
                    dst_cur.close()
                except Exception as e:
                    # ถ้า UndefinedColumn — ตัด column ที่ไม่มีใน PG ออกแล้ว retry
                    if ('UndefinedColumn' in type(e).__name__
                            or 'does not exist' in str(e)):
                        try:
                            dst_conn.rollback()
                        except Exception:
                            pass
                        try:
                            import re as _re
                            m = _re.search(r'column "([^"]+)"', str(e))
                            bad_col = m.group(1) if m else None
                            if bad_col:
                                flt = [(c, v) for c, v in zip(cols, vals)
                                       if c.lower() != bad_col]
                                if flt:
                                    fc, fv = zip(*flt)
                                    fc_str = ', '.join([f'"{c.lower()}"' for c in fc])
                                    fv_str = ', '.join(['%s'] * len(fv))
                                    sql2 = (f'INSERT INTO "{table_name.lower()}" ({fc_str}) '
                                            f'VALUES ({fv_str}) ON CONFLICT DO NOTHING')
                                    dst_cur2 = dst_conn.cursor()
                                    dst_cur2.execute(sql2, list(fv))
                                    dst_conn.commit()
                                    inserted += dst_cur2.rowcount
                                    dst_cur2.close()
                                    continue
                        except Exception:
                            try:
                                dst_conn.rollback()
                            except Exception:
                                pass
                    # ถ้า UntranslatableCharacter — ทำความสะอาด string แล้ว retry
                    if ('UntranslatableCharacter' in type(e).__name__
                            or 'has no equivalent in encoding' in str(e)):
                        try:
                            dst_conn.rollback()
                        except Exception:
                            pass
                        try:
                            clean_vals = [_clean_str_for_pg(v) if isinstance(v, str) else v
                                          for v in vals]
                            dst_cur2 = dst_conn.cursor()
                            dst_cur2.execute(sql, clean_vals)
                            dst_conn.commit()
                            inserted += dst_cur2.rowcount
                            dst_cur2.close()
                            continue
                        except Exception:
                            try:
                                dst_conn.rollback()
                            except Exception:
                                pass
                    # ถ้า NotNullViolation — ตัด NULL column ออก ให้ PostgreSQL ใช้ DEFAULT
                    if 'NotNullViolation' in type(e).__name__ or 'null value in column' in str(e):
                        try:
                            dst_conn.rollback()
                        except Exception:
                            pass
                        try:
                            non_null = [(c, v) for c, v in zip(cols, vals) if v is not None]
                            if non_null:
                                rc, rv = zip(*non_null)
                                rc_str = ', '.join([f'"{c.lower()}"' for c in rc])
                                rv_str = ', '.join(['%s'] * len(rv))
                                sql2 = (f'INSERT INTO "{table_name.lower()}" ({rc_str}) '
                                        f'VALUES ({rv_str}) ON CONFLICT DO NOTHING')
                                dst_cur2 = dst_conn.cursor()
                                dst_cur2.execute(sql2, list(rv))
                                dst_conn.commit()
                                inserted += dst_cur2.rowcount
                                dst_cur2.close()
                                continue
                        except Exception as e2:
                            try:
                                dst_conn.rollback()
                            except Exception:
                                pass
                            # ถ้า retry ก็ยัง NotNullViolation → retry ที่ 3: แทน None ด้วย 0
                            if ('NotNullViolation' in type(e2).__name__
                                    or 'null value in column' in str(e2)):
                                try:
                                    if table_name.lower() not in _pg_col_cache:
                                        _get_pg_col_defaults(dst_conn, table_name)
                                    pg_defs = _pg_col_cache.get(table_name.lower(), {})
                                    vals3 = [
                                        pg_defs.get(c.lower(), 0) if v is None else v
                                        for c, v in zip(cols, vals)
                                    ]
                                    dst_cur3 = dst_conn.cursor()
                                    dst_cur3.execute(sql, vals3)
                                    dst_conn.commit()
                                    inserted += dst_cur3.rowcount
                                    dst_cur3.close()
                                    continue
                                except Exception:
                                    try:
                                        dst_conn.rollback()
                                    except Exception:
                                        pass
                                    # ข้อมูลใน MySQL มี NULL ที่ PG ไม่อนุญาต → ข้ามเงียบๆ
                                    skipped_null += 1
                                    continue
                    # ทุก error ที่เหลือ → rollback + บันทึก error type ใน summary
                    try:
                        dst_conn.rollback()
                    except Exception:
                        pass
                    err_type = type(e).__name__
                    err_msg  = str(e).strip().split('\n')[0][:120]
                    full_key = f'{err_type}: {err_msg}'
                    if full_key not in error_summary:
                        error_summary[full_key] = {'count': 0, 'pk_examples': []}
                    error_summary[full_key]['count'] += 1
                    if len(error_summary[full_key]['pk_examples']) < 3:
                        error_summary[full_key]['pk_examples'].append(pk_val)
                    skipped_null += 1

        src_conn.close()
        dst_conn.close()

        total_errors = sum(v['count'] for v in error_summary.values() if v['count'] > 0)
        msg = f'เพิ่มข้อมูลสำเร็จ {inserted} รายการ จากทั้งหมด {total_missing} รายการ'
        if skipped_null:
            msg += f' (ข้าม {skipped_null} รายการ)'

        # สร้าง error_summary list สำหรับ UI
        summary_list = [
            {
                'error': k,
                'count': v['count'],
                'pk_examples': v['pk_examples']
            }
            for k, v in sorted(error_summary.items(),
                               key=lambda x: x[1]['count'], reverse=True)
        ]

        return jsonify({
            'status': 'ok',
            'inserted': inserted,
            'total_missing': total_missing,
            'total_errors': total_errors,
            'error_summary': summary_list,
            'error_details': error_details,
            'message': msg
        })

    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


@app.route('/api/compare-by-prefix', methods=['POST'])
def compare_by_prefix():
    data = request.json
    prefix = data.get('prefix', '')
    config = load_config()

    try:
        # ดึงรายชื่อตารางทั้ง 2 ฝั่งพร้อมกัน
        tbl_results = {}
        def _get_tables_worker(side):
            cfg = config[side]
            conn, db_type = get_connection(cfg)
            tbls = get_tables(conn, db_type, cfg['database'])
            conn.close()
            tbl_results[side] = tbls

        t1 = threading.Thread(target=_get_tables_worker, args=('source',))
        t2 = threading.Thread(target=_get_tables_worker, args=('destination',))
        t1.start(); t2.start()
        t1.join();  t2.join()

        src_tables   = tbl_results.get('source', [])
        dst_table_set = set(tbl_results.get('destination', []))

        filtered = [t for t in src_tables if t.upper().startswith(prefix.upper())] \
            if prefix else src_tables


        dst_to_count = [t for t in filtered if t in dst_table_set]

        # Source: นับ record + ดึง column names พร้อมกัน
        src_data = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(_count_table, config['source'], t): t for t in filtered}
            for f in as_completed(futures):
                src_data[futures[f]] = f.result()

        # Destination: นับ record + ดึง column names พร้อมกัน
        dst_data = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(_count_table, config['destination'], t): t for t in dst_to_count}
            for f in as_completed(futures):
                dst_data[futures[f]] = f.result()

        results = []
        for table in filtered:
            src_count, src_cols = src_data.get(table, (-1, set()))
            if table in dst_table_set:
                dst_count, dst_cols = dst_data.get(table, (-1, set()))
                missing_col_count   = len(src_cols - dst_cols) if src_cols else 0
                status = 'ok' if src_count == dst_count else 'diff'
            else:
                dst_count, dst_cols   = -1, set()
                missing_col_count     = 0
                status = 'missing'

            results.append({
                'table':             table,
                'source_count':      src_count,
                'destination_count': dst_count,
                'diff':              src_count - max(dst_count, 0),
                'status':            status,
                'has_missing_cols':  missing_col_count > 0,
                'missing_col_count': missing_col_count,
            })

        return jsonify({'results': results, 'prefix': prefix})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400


def get_columns(conn, db_type, table_name, db_name):
    """Return list of {column, type_raw, nullable, default, extra}"""
    cur = conn.cursor()
    if db_type == 'mysql':
        cur.execute(f'SHOW FULL COLUMNS FROM `{table_name}`')
        rows = cur.fetchall()
        cols = []
        for row in rows:
            r = row if isinstance(row, dict) else dict(zip(
                ['Field','Type','Collation','Null','Key','Default','Extra','Privileges','Comment'], row))
            cols.append({
                'column':   r.get('Field', ''),
                'type_raw': r.get('Type', ''),
                'nullable': r.get('Null', 'YES') == 'YES',
                'default':  r.get('Default'),
                'extra':    r.get('Extra', ''),
            })
    else:
        cur.execute("""
            SELECT column_name, data_type, character_maximum_length,
                   is_nullable, column_default,
                   numeric_precision, numeric_scale, udt_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            ORDER BY ordinal_position
        """, (table_name,))
        rows = cur.fetchall()
        cols = []
        for row in rows:
            r = row if isinstance(row, dict) else dict(zip(
                ['column_name','data_type','character_maximum_length',
                 'is_nullable','column_default','numeric_precision','numeric_scale','udt_name'], row))
            length    = r.get('character_maximum_length')
            precision = r.get('numeric_precision')
            scale     = r.get('numeric_scale')
            dtype     = r.get('data_type','')
            if length:
                type_raw = f"{dtype}({length})"
            elif precision and scale:
                type_raw = f"{dtype}({precision},{scale})"
            elif precision:
                type_raw = f"{dtype}({precision})"
            else:
                type_raw = dtype
            cols.append({
                'column':   r.get('column_name', ''),
                'type_raw': type_raw,
                'nullable': r.get('is_nullable', 'YES') == 'YES',
                'default':  r.get('column_default'),
                'extra':    '',
            })
    cur.close()
    return cols


def mysql_type_to_pg(mysql_type):
    """แปลง MySQL type string → PostgreSQL type string"""
    t = mysql_type.lower().strip()
    unsigned = 'unsigned' in t
    t = re.sub(r'\s*unsigned\s*', '', t).strip()
    m = re.match(r'(\w+)(?:\(([^)]+)\))?', t)
    if not m:
        return 'TEXT'
    base, params = m.group(1), (m.group(2) or '').strip()

    if base == 'tinyint':
        return 'BOOLEAN' if params == '1' else 'SMALLINT'
    if base == 'bit':
        return 'BOOLEAN' if params == '1' else (f'BIT({params})' if params else 'BIT')
    if base == 'smallint':           return 'SMALLINT'
    if base == 'mediumint':          return 'INTEGER'
    if base in ('int', 'integer'):   return 'INTEGER'
    if base == 'bigint':             return 'BIGINT'
    if base == 'float':              return 'REAL'
    if base in ('double', 'double precision'):  return 'DOUBLE PRECISION'
    if base in ('decimal', 'numeric'):
        return f'NUMERIC({params})' if params else 'NUMERIC'
    if base in ('varchar', 'nvarchar'):
        return f'VARCHAR({params})' if params else 'VARCHAR'
    if base in ('char', 'nchar'):
        return f'CHAR({params})' if params else 'CHAR(1)'
    if base in ('tinytext', 'text', 'mediumtext', 'longtext'):  return 'TEXT'
    if base == 'date':       return 'DATE'
    if base in ('datetime', 'timestamp'):       return 'TIMESTAMP'
    if base == 'time':       return 'TIME'
    if base == 'year':       return 'SMALLINT'
    if base in ('tinyblob', 'blob', 'mediumblob', 'longblob', 'binary', 'varbinary'):
        return 'BYTEA'
    if base in ('enum', 'set'):  return 'TEXT'
    if base == 'json':       return 'JSONB'
    if base in ('bool', 'boolean'):  return 'BOOLEAN'
    return 'TEXT'


def pg_type_to_mysql(type_raw):
    """แปลง PostgreSQL type string → MySQL type string"""
    t = type_raw.lower().strip()
    m = re.match(r'([\w\s]+?)(?:\(([^)]+)\))?$', t)
    base = m.group(1).strip() if m else t
    params = m.group(2) if m and m.group(2) else ''

    if base in ('character varying', 'varchar'):
        return f'VARCHAR({params})' if params else 'VARCHAR(255)'
    if base in ('character', 'char'):
        return f'CHAR({params})' if params else 'CHAR(1)'
    if base == 'text':           return 'LONGTEXT'
    if base in ('integer', 'int', 'int4'):   return 'INT'
    if base in ('smallint', 'int2'):         return 'SMALLINT'
    if base in ('bigint', 'int8'):           return 'BIGINT'
    if base in ('real', 'float4'):           return 'FLOAT'
    if base in ('double precision', 'float8'):  return 'DOUBLE'
    if base in ('numeric', 'decimal'):
        return f'DECIMAL({params})' if params else 'DECIMAL(18,4)'
    if base == 'boolean':        return 'TINYINT(1)'
    if base == 'date':           return 'DATE'
    if 'timestamp' in base:      return 'DATETIME'
    if 'time' in base:           return 'TIME'
    if base == 'bytea':          return 'LONGBLOB'
    if base in ('json', 'jsonb'): return 'JSON'
    return 'TEXT'


@app.route('/api/compare-columns', methods=['POST'])
def compare_columns():
    data       = request.json
    table_name = data.get('table')
    config     = load_config()

    try:
        src_conn, src_type = get_connection(config['source'])
        dst_conn, dst_type = get_connection(config['destination'])

        src_cols = get_columns(src_conn, src_type, table_name, config['source']['database'])
        dst_cols = get_columns(dst_conn, dst_type, table_name, config['destination']['database'])
        src_conn.close()
        dst_conn.close()

        src_names = {c['column'].lower(): c for c in src_cols}
        dst_names = {c['column'].lower(): c for c in dst_cols}

        missing_in_dest = []
        for name, col in src_names.items():
            if name not in dst_names:
                pg_type = (mysql_type_to_pg(col['type_raw'])
                           if src_type == 'mysql' else col['type_raw'].upper())
                my_type = (pg_type_to_mysql(col['type_raw'])
                           if src_type == 'postgresql' else col['type_raw'])
                missing_in_dest.append({
                    'column':   col['column'],
                    'src_type': col['type_raw'],
                    'dst_type': pg_type if dst_type == 'postgresql' else my_type,
                    'nullable': col['nullable'],
                    'default':  str(col['default']) if col['default'] is not None else None,
                    'extra':    col['extra'],
                })

        extra_in_dest = [
            {'column': c['column'], 'dst_type': c['type_raw']}
            for name, c in dst_names.items() if name not in src_names
        ]

        return jsonify({
            'table':           table_name,
            'missing_in_dest': missing_in_dest,
            'extra_in_dest':   extra_in_dest,
            'src_total':       len(src_cols),
            'dst_total':       len(dst_cols),
        })

    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


@app.route('/api/add-columns', methods=['POST'])
def add_columns():
    data       = request.json
    table_name = data.get('table')
    columns    = data.get('columns', [])   # list of {column, dst_type, nullable, default}
    config     = load_config()

    try:
        dst_conn, dst_type = get_connection(config['destination'])
        added, errors = [], []

        for col in columns:
            col_name = col['column']
            col_type = col['dst_type']
            nullable = col.get('nullable', True)
            default  = col.get('default')

            # สร้าง column definition
            null_clause    = '' if nullable else ' NOT NULL'
            default_clause = ''
            if not nullable and default is None:
                # ป้องกัน error: NOT NULL ต้องมี default ถ้าตารางมีข้อมูลอยู่แล้ว
                null_clause    = ''   # เพิ่มเป็น nullable ก่อน ปลอดภัยกว่า

            if default is not None:
                if dst_type == 'postgresql':
                    default_clause = f" DEFAULT '{default}'"
                else:
                    default_clause = f" DEFAULT '{default}'"

            if dst_type == 'postgresql':
                sql = f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {col_type}{null_clause}{default_clause}'
            else:
                sql = f'ALTER TABLE `{table_name}` ADD COLUMN `{col_name}` {col_type}{null_clause}{default_clause}'

            try:
                cur = dst_conn.cursor()
                cur.execute(sql)
                dst_conn.commit()
                cur.close()
                added.append({'column': col_name, 'sql': sql})
            except Exception as e:
                try:
                    dst_conn.rollback()
                except Exception:
                    pass
                errors.append({'column': col_name, 'sql': sql, 'error': str(e)})

        dst_conn.close()

        return jsonify({
            'status':  'ok',
            'added':   added,
            'errors':  errors,
            'message': f'เพิ่มฟิลสำเร็จ {len(added)} ฟิล' +
                       (f' (ผิดพลาด {len(errors)} ฟิล)' if errors else '')
        })

    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


def generate_create_sql(table_name, columns, pks, src_type, dst_type):
    """สร้าง CREATE TABLE SQL สำหรับ database ปลายทาง"""
    lines = []
    for col in columns:
        raw = col['type_raw']
        if dst_type == 'postgresql':
            col_type = mysql_type_to_pg(raw) if src_type == 'mysql' else raw.upper()
            col_name = f'"{col["column"]}"'
        else:
            col_type = pg_type_to_mysql(raw) if src_type == 'postgresql' else raw
            col_name = f'`{col["column"]}`'

        null_clause    = '' if col['nullable'] else ' NOT NULL'
        default_clause = ''
        if col['default'] is not None:
            dv = str(col['default'])
            if dv.upper() in ('CURRENT_TIMESTAMP', 'NOW()', 'NOW'):
                default_clause = ' DEFAULT CURRENT_TIMESTAMP'
            elif dv.upper() in ('NULL',):
                default_clause = ' DEFAULT NULL'
            else:
                default_clause = f" DEFAULT '{dv}'"

        lines.append(f'  {col_name} {col_type}{null_clause}{default_clause}')

    if pks:
        if dst_type == 'postgresql':
            pk_cols = ', '.join([f'"{pk}"' for pk in pks])
        else:
            pk_cols = ', '.join([f'`{pk}`' for pk in pks])
        lines.append(f'  PRIMARY KEY ({pk_cols})')

    tbl = f'"{table_name}"' if dst_type == 'postgresql' else f'`{table_name}`'
    return f'CREATE TABLE {tbl} (\n' + ',\n'.join(lines) + '\n)'


@app.route('/api/get-table-structure', methods=['POST'])
def get_table_structure():
    data       = request.json
    table_name = data.get('table')
    config     = load_config()

    try:
        src_conn, src_type = get_connection(config['source'])
        columns = get_columns(src_conn, src_type, table_name, config['source']['database'])
        pks     = get_primary_keys(src_conn, src_type, table_name, config['source']['database'])
        src_conn.close()

        _, dst_type = get_connection(config['destination'])  # just to know the type
        # close immediately — we only need the type
        tmp, dst_type = get_connection(config['destination'])
        tmp.close()

        create_sql = generate_create_sql(table_name, columns, pks, src_type, dst_type)

        return jsonify({
            'table':      table_name,
            'src_type':   src_type,
            'dst_type':   dst_type,
            'columns':    columns,
            'primary_keys': pks,
            'create_sql': create_sql,
        })

    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


@app.route('/api/create-table', methods=['POST'])
def create_table():
    data       = request.json
    table_name = data.get('table')
    create_sql = data.get('create_sql', '').strip()
    config     = load_config()

    if not create_sql:
        return jsonify({'status': 'error', 'message': 'ไม่มี SQL ที่จะสร้างตาราง'}), 400

    try:
        dst_conn, dst_type = get_connection(config['destination'])

        # ตรวจสอบว่าตารางยังไม่มีอยู่
        if table_exists(dst_conn, dst_type, table_name, config['destination']['database']):
            dst_conn.close()
            return jsonify({'status': 'error',
                            'message': f'ตาราง "{table_name}" มีอยู่ในปลายทางแล้ว'}), 400

        cur = dst_conn.cursor()
        cur.execute(create_sql)
        dst_conn.commit()
        cur.close()
        dst_conn.close()

        return jsonify({'status': 'ok',
                        'message': f'สร้างตาราง "{table_name}" สำเร็จ'})

    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


@app.route('/api/compare-tables-list', methods=['POST'])
def compare_tables_list():
    data       = request.json
    table_list = list(dict.fromkeys(data.get('tables', [])))   # deduplicate, keep order
    config     = load_config()
    try:
        src_conn, src_type = get_connection(config['source'])
        src_all = set(get_tables(src_conn, src_type, config['source']['database']))
        src_conn.close()

        dst_conn, dst_type = get_connection(config['destination'])
        dst_all = set(get_tables(dst_conn, dst_type, config['destination']['database']))
        dst_conn.close()

        to_check    = [t for t in table_list if t in src_all]
        dst_to_count = [t for t in to_check  if t in dst_all]

        src_data = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(_count_table, config['source'], t): t for t in to_check}
            for f in as_completed(futures):
                src_data[futures[f]] = f.result()

        dst_data = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(_count_table, config['destination'], t): t for t in dst_to_count}
            for f in as_completed(futures):
                dst_data[futures[f]] = f.result()

        results = []
        for table in to_check:
            src_count, src_cols = src_data.get(table, (-1, set()))
            if table in dst_all:
                dst_count, dst_cols = dst_data.get(table, (-1, set()))
                missing_col_count   = len(src_cols - dst_cols) if src_cols else 0
                status = 'ok' if src_count == dst_count else 'diff'
            else:
                dst_count, dst_cols = -1, set()
                missing_col_count   = 0
                status = 'missing'
            results.append({
                'table':             table,
                'source_count':      src_count,
                'destination_count': dst_count,
                'diff':              src_count - max(dst_count, 0),
                'status':            status,
                'has_missing_cols':  missing_col_count > 0,
                'missing_col_count': missing_col_count,
            })

        return jsonify({'results': results})
    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


def _get_src_serials(config, items):
    """ดึง serial_no จาก source (MySQL) สำหรับแต่ละ serial_name"""
    result = {}
    try:
        conn, _ = get_connection(config['source'])
        cur = conn.cursor()
        for item in items:
            sn = item.get('serialName', item.get('col', ''))
            try:
                cur.execute('SELECT serial_no FROM serial WHERE name = %s', (sn,))
                row = cur.fetchone()
                val = (list(row.values())[0] if isinstance(row, dict) else row[0]) if row else None
                result[sn] = int(val) if val is not None else None
            except Exception:
                result[sn] = None
        cur.close()
        conn.close()
    except Exception:
        pass
    return result


@app.route('/api/check-sequences', methods=['POST'])
def check_sequences():
    data    = request.json
    items   = data.get('items', [])
    dst_cfg = load_dst_config()
    if dst_cfg.get('type') != 'postgresql':
        return jsonify({'status': 'error',
                        'message': 'รองรับเฉพาะ PostgreSQL ปลายทาง'}), 400
    conn, _ = get_connection(dst_cfg)
    cur = conn.cursor()
    results = []
    for item in items:
        col      = item.get('col', '')
        fix_op   = item.get('fixOp', '')
        seq_name = item.get('seqName', '')
        try:
            if fix_op == 'fix_serial_min':
                cur.execute("SELECT COUNT(*) FROM serial WHERE serial_no = 1")
                count = int(cur.fetchone()[0])
                results.append({'col': col, 'table': 'serial', 'max_val': count,
                                'dst_serial': count, 'was_equal': count == 0,
                                'serial_exists': True, 'seq_name': '', 'seq_exists': False,
                                'status': 'ok'})
                continue
            query_col   = item.get('queryCol', col)
            query_table = item.get('queryTable', '')
            serial_name = item.get('serialName', col)
            query_expr  = item.get('queryExpr', f'"{query_col}"')
            cur.execute(f'SELECT COALESCE(MAX({query_expr}), 0) FROM "{query_table}"')
            max_val    = int(cur.fetchone()[0])
            target_val   = max(max_val + 1, 2)
            cur.execute('SELECT serial_no FROM serial WHERE name = %s', (serial_name,))
            row = cur.fetchone()
            dst_serial   = int(row[0]) if row else None
            was_equal    = (dst_serial == target_val) if dst_serial is not None else False
            serial_ahead = (dst_serial > target_val)  if dst_serial is not None else False
            cur.execute("SELECT 1 FROM pg_class WHERE relname = %s AND relkind = 'S'",
                        (seq_name,))
            seq_exists = cur.fetchone() is not None
            results.append({'col': col, 'table': query_table, 'max_val': max_val,
                            'target_val': target_val,
                            'dst_serial': dst_serial, 'was_equal': was_equal,
                            'serial_ahead': serial_ahead,
                            'serial_exists': dst_serial is not None,
                            'seq_name': seq_name, 'seq_exists': seq_exists,
                            'status': 'ok'})
        except Exception as e:
            results.append({'col': col, 'status': 'error', 'message': str(e)})
    cur.close()
    conn.close()
    return jsonify({'results': results})


@app.route('/api/update-sequences', methods=['POST'])
def update_sequences():
    data    = request.json
    items   = data.get('items', [])
    dst_cfg = load_dst_config()
    if dst_cfg.get('type') != 'postgresql':
        return jsonify({'status': 'error',
                        'message': 'รองรับเฉพาะ PostgreSQL ปลายทาง'}), 400
    conn, _ = get_connection(dst_cfg)
    cur = conn.cursor()
    results = []
    for item in items:
        col      = item.get('col', '')
        fix_op   = item.get('fixOp', '')
        seq_name = item.get('seqName', '')
        try:
            if fix_op == 'fix_serial_min':
                cur.execute("UPDATE serial SET serial_no = 2 WHERE serial_no = 1")
                updated = cur.rowcount
                conn.commit()
                results.append({'col': col, 'table': 'serial', 'max_val': updated,
                                'dst_serial': updated, 'target_val': 2,
                                'was_equal': updated == 0,
                                'serial_action': 'UPDATE', 'seq_name': '', 'seq_action': 'SKIP',
                                'status': 'ok'})
                continue
            query_col   = item.get('queryCol', col)
            query_table = item.get('queryTable', '')
            serial_name = item.get('serialName', col)
            query_expr  = item.get('queryExpr', f'"{query_col}"')
            cur.execute(f'SELECT COALESCE(MAX({query_expr}), 0) FROM "{query_table}"')
            max_val    = int(cur.fetchone()[0])
            target_val   = max(max_val + 1, 2)
            cur.execute('SELECT serial_no FROM serial WHERE name = %s', (serial_name,))
            row = cur.fetchone()
            dst_serial   = int(row[0]) if row else None
            was_equal    = (dst_serial == target_val) if dst_serial is not None else False
            serial_ahead = (dst_serial > target_val)  if dst_serial is not None else False
            # 1+2. UPSERT serial table ด้วย target_val
            cur.execute("""
                INSERT INTO serial (name, serial_no)
                VALUES (%s, %s)
                ON CONFLICT (name) DO UPDATE SET serial_no = EXCLUDED.serial_no
            """, (serial_name, target_val))
            serial_action = 'INSERT' if dst_serial is None else 'UPDATE'
            # ถ้า serial_no = 1 ให้ปรับเป็น 2
            cur.execute('UPDATE serial SET serial_no = 2 WHERE name = %s AND serial_no <= 1',
                        (serial_name,))
            conn.commit()
            # 3+4. CREATE หรือ ALTER SEQUENCE ด้วย target_val
            cur.execute("SELECT 1 FROM pg_class WHERE relname = %s AND relkind = 'S'",
                        (seq_name,))
            seq_exists = cur.fetchone() is not None
            if seq_exists:
                cur.execute(f'ALTER SEQUENCE "{seq_name}" RESTART WITH {target_val}')
                seq_action = 'ALTER'
            else:
                cur.execute(f'CREATE SEQUENCE "{seq_name}" START WITH {target_val}')
                seq_action = 'CREATE'
            conn.commit()
            results.append({'col': col, 'table': query_table, 'max_val': max_val,
                            'dst_serial': dst_serial, 'target_val': target_val,
                            'was_equal': was_equal, 'serial_ahead': serial_ahead,
                            'serial_action': serial_action,
                            'seq_name': seq_name, 'seq_action': seq_action,
                            'status': 'ok'})
        except Exception as e:
            try: conn.rollback()
            except: pass
            results.append({'col': col, 'status': 'error', 'message': str(e)})
    cur.close()
    conn.close()
    return jsonify({'results': results})


def _mysql_row_val(row):
    return int(list(row.values())[0] if isinstance(row, dict) else row[0]) if row else None


@app.route('/api/check-mysql-serials', methods=['POST'])
def check_mysql_serials():
    data    = request.json
    items   = data.get('items', [])
    dst_cfg = load_dst_config()
    if dst_cfg.get('type') != 'mysql':
        return jsonify({'status': 'error', 'message': 'รองรับเฉพาะ MySQL ปลายทาง'}), 400

    conn, _ = get_connection(dst_cfg)
    cur = conn.cursor()
    results = []
    for item in items:
        col      = item.get('col', '')
        fix_op   = item.get('fixOp', '')
        try:
            if fix_op == 'fix_serial_min':
                cur.execute("SELECT COUNT(*) FROM serial WHERE serial_no = 1")
                count = _mysql_row_val(cur.fetchone()) or 0
                results.append({'col': col, 'table': 'serial', 'max_val': count,
                                'dst_serial': count, 'was_equal': count == 0,
                                'serial_exists': True, 'status': 'ok'})
                continue
            query_col   = item.get('queryCol', col)
            query_table = item.get('queryTable', '')
            serial_name = item.get('serialName', col)
            cur.execute(f'SELECT COALESCE(MAX(`{query_col}`), 0) FROM `{query_table}`')
            max_val    = _mysql_row_val(cur.fetchone()) or 0
            target_val = max_val + 1
            cur.execute('SELECT serial_no FROM serial WHERE name = %s', (serial_name,))
            dst_serial = _mysql_row_val(cur.fetchone())
            was_equal  = (dst_serial == target_val) if dst_serial is not None else False
            results.append({'col': col, 'table': query_table, 'max_val': max_val,
                            'target_val': target_val,
                            'dst_serial': dst_serial, 'was_equal': was_equal,
                            'serial_exists': dst_serial is not None,
                            'status': 'ok'})
        except Exception as e:
            results.append({'col': col, 'status': 'error', 'message': str(e)})
    cur.close()
    conn.close()
    return jsonify({'results': results})


@app.route('/api/update-mysql-serials', methods=['POST'])
def update_mysql_serials():
    data    = request.json
    items   = data.get('items', [])
    dst_cfg = load_dst_config()
    if dst_cfg.get('type') != 'mysql':
        return jsonify({'status': 'error', 'message': 'รองรับเฉพาะ MySQL ปลายทาง'}), 400

    conn, _ = get_connection(dst_cfg)
    cur = conn.cursor()
    results = []
    for item in items:
        col    = item.get('col', '')
        fix_op = item.get('fixOp', '')
        try:
            if fix_op == 'fix_serial_min':
                cur.execute("UPDATE serial SET serial_no = 2 WHERE serial_no = 1")
                updated = cur.rowcount
                conn.commit()
                results.append({'col': col, 'table': 'serial', 'max_val': updated,
                                'dst_serial': updated, 'was_equal': updated == 0,
                                'serial_action': 'UPDATE', 'status': 'ok'})
                continue
            query_col   = item.get('queryCol', col)
            query_table = item.get('queryTable', '')
            serial_name = item.get('serialName', col)
            cur.execute(f'SELECT COALESCE(MAX(`{query_col}`), 0) FROM `{query_table}`')
            max_val    = _mysql_row_val(cur.fetchone()) or 0
            target_val = max_val + 1
            cur.execute('SELECT serial_no FROM serial WHERE name = %s', (serial_name,))
            dst_serial = _mysql_row_val(cur.fetchone())
            was_equal  = (dst_serial == target_val) if dst_serial is not None else False
            if dst_serial is None:
                cur.execute('INSERT INTO serial (name, serial_no) VALUES (%s, %s)',
                            (serial_name, target_val))
                serial_action = 'INSERT'
            else:
                cur.execute('UPDATE serial SET serial_no = %s WHERE name = %s',
                            (target_val, serial_name))
                serial_action = 'UPDATE'
            conn.commit()
            results.append({'col': col, 'table': query_table, 'max_val': max_val,
                            'target_val': target_val,
                            'dst_serial': dst_serial, 'was_equal': was_equal,
                            'serial_action': serial_action, 'status': 'ok'})
        except Exception as e:
            try: conn.rollback()
            except: pass
            results.append({'col': col, 'status': 'error', 'message': str(e)})
    cur.close()
    conn.close()
    return jsonify({'results': results})


@app.route('/api/update-mysql-autoincrement', methods=['POST'])
def update_mysql_autoincrement():
    data    = request.json
    columns = data.get('columns', [])
    config  = load_config()
    if config['destination']['type'] != 'mysql':
        return jsonify({'status': 'error',
                        'message': 'ฟีเจอร์นี้รองรับเฉพาะ MySQL ปลายทาง'}), 400
    db_name = config['destination'].get('database', '')
    conn, _ = get_connection(config['destination'])
    cur     = conn.cursor()
    results = []
    for col in columns:
        try:
            cur.execute("""
                SELECT TABLE_NAME FROM information_schema.COLUMNS
                WHERE COLUMN_NAME  = %s
                  AND TABLE_SCHEMA = %s
                  AND EXTRA LIKE '%%auto_increment%%'
            """, (col, db_name))
            rows = cur.fetchall()
            if not rows:
                results.append({'column': col, 'status': 'not_found',
                                'message': 'ไม่พบ AUTO_INCREMENT'})
                continue
            for row in rows:
                table_name = row['TABLE_NAME'] if isinstance(row, dict) else row[0]
                cur.execute(f'SELECT COALESCE(MAX(`{col}`), 0) FROM `{table_name}`')
                r       = cur.fetchone()
                max_val = int(list(r.values())[0] if isinstance(r, dict) else r[0])
                new_val = max_val + 1
                cur.execute(f'ALTER TABLE `{table_name}` AUTO_INCREMENT = %s', (new_val,))
                conn.commit()
                results.append({'column': col, 'table': table_name,
                                'max_data': max_val, 'new_value': new_val,
                                'status': 'ok'})
        except Exception as e:
            try: conn.rollback()
            except: pass
            results.append({'column': col, 'status': 'error', 'message': str(e)})
    cur.close()
    conn.close()
    return jsonify({'results': results})


@app.route('/api/update-changed-records', methods=['POST'])
def update_changed_records():
    data       = request.json
    table_name = data.get('table')
    config     = load_config()
    try:
        src_conn, src_type = get_connection(config['source'])
        dst_conn, dst_type = get_connection(config['destination'])

        pks = get_primary_keys(src_conn, src_type, table_name,
                               config['source'].get('database', ''))
        if not pks:
            src_conn.close(); dst_conn.close()
            return jsonify({'status': 'error', 'message': 'ไม่พบ Primary Key'}), 400

        src_pk_set = get_all_pks(src_conn, src_type, table_name, pks)
        dst_pk_set = get_all_pks(dst_conn, dst_type, table_name, pks)
        common_pks = list(src_pk_set & dst_pk_set)

        updated  = 0
        checked  = 0
        pk_lower = [p.lower() for p in pks]

        for i in range(0, len(common_pks), 200):
            batch = common_pks[i:i + 200]

            src_recs, col_names = fetch_records_by_pks(
                src_conn, src_type, table_name, pks, batch)
            dst_recs, _ = fetch_records_by_pks(
                dst_conn, dst_type, table_name, pks, batch)

            # index dest by lowercase PK values
            dst_idx = {}
            for rec in dst_recs:
                k = tuple(str(rec.get(p.lower(), rec.get(p, ''))) for p in pks)
                dst_idx[k] = rec

            non_pk = [c for c in col_names if c.lower() not in pk_lower]

            for src in src_recs:
                checked += 1
                k = tuple(str(src.get(p, '')) for p in pks)
                dst = dst_idx.get(k)
                if not dst:
                    continue

                changed = {}
                for col in non_pk:
                    sv = src.get(col)
                    dv = dst.get(col.lower(), dst.get(col))
                    s_str = '' if sv is None else str(sv).strip()
                    d_str = '' if dv is None else str(dv).strip()
                    if s_str != d_str:
                        changed[col] = sv

                if not changed:
                    continue

                try:
                    if dst_type == 'postgresql':
                        cv = {c: _sanitize_pg(v) for c, v in changed.items()}
                        set_s  = ', '.join([f'"{c.lower()}"=%s' for c in cv])
                        whr_s  = ' AND '.join([f'"{p.lower()}"=%s' for p in pks])
                        sql    = f'UPDATE "{table_name.lower()}" SET {set_s} WHERE {whr_s}'
                        vals   = list(cv.values()) + [src.get(p) for p in pks]
                    else:
                        set_s  = ', '.join([f'`{c}`=%s' for c in changed])
                        whr_s  = ' AND '.join([f'`{p}`=%s' for p in pks])
                        sql    = f'UPDATE `{table_name}` SET {set_s} WHERE {whr_s}'
                        vals   = list(changed.values()) + [src.get(p) for p in pks]
                    cur = dst_conn.cursor()
                    cur.execute(sql, vals)
                    dst_conn.commit()
                    updated += cur.rowcount
                    cur.close()
                except Exception:
                    try: dst_conn.rollback()
                    except: pass

        src_conn.close(); dst_conn.close()
        return jsonify({'status': 'ok', 'updated': updated, 'checked': checked,
                        'common': len(common_pks),
                        'message': f'ตรวจสอบ {checked} | อัปเดต {updated} รายการ'})
    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


@app.route('/api/truncate-prefix-tables', methods=['POST'])
def truncate_prefix_tables():
    data   = request.json
    prefix = data.get('prefix', '')
    config = load_config()
    if not prefix:
        return jsonify({'status': 'error', 'message': 'ไม่ระบุ prefix'}), 400
    try:
        conn, db_type = get_connection(config['destination'])
        tables = get_tables(conn, db_type, config['destination'].get('database', ''))
        to_truncate = [t for t in tables if t.lower().startswith(prefix.lower())]
        if not to_truncate:
            conn.close()
            return jsonify({'status': 'ok', 'truncated': 0, 'message': 'ไม่พบตารางที่ตรงเงื่อนไข'})
        cur = conn.cursor()
        truncated = 0
        errors = []
        for table in to_truncate:
            try:
                if db_type == 'postgresql':
                    cur.execute(f'TRUNCATE TABLE "{table}" CASCADE')
                else:
                    cur.execute(f'DELETE FROM `{table}`')
                conn.commit()
                truncated += 1
            except Exception as e:
                try: conn.rollback()
                except: pass
                errors.append(f'{table}: {str(e)[:80]}')
        cur.close()
        conn.close()
        msg = f'ล้างข้อมูลสำเร็จ {truncated} ตาราง'
        if errors:
            msg += f' (ผิดพลาด {len(errors)} ตาราง)'
        return jsonify({'status': 'ok', 'truncated': truncated,
                        'total': len(to_truncate), 'errors': errors, 'message': msg})
    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


@app.route('/api/compare-contains', methods=['POST'])
def compare_contains():
    data         = request.json
    pattern      = data.get('pattern', '')
    extra_tables = set(data.get('extra_tables', []))
    config  = load_config()
    try:
        tbl_results = {}
        def _get_tbls(side):
            cfg = config[side]
            conn, db_type = get_connection(cfg)
            tbls = get_tables(conn, db_type, cfg['database'])
            conn.close()
            tbl_results[side] = tbls
        t1 = threading.Thread(target=_get_tbls, args=('source',))
        t2 = threading.Thread(target=_get_tbls, args=('destination',))
        t1.start(); t2.start(); t1.join(); t2.join()

        src_tables    = tbl_results.get('source', [])
        dst_table_set = set(tbl_results.get('destination', []))
        filtered      = [t for t in src_tables
                         if (pattern.lower() in t.lower() if pattern else True)
                         or t in extra_tables]
        dst_to_count  = [t for t in filtered if t in dst_table_set]

        src_data = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(_count_table, config['source'], t): t for t in filtered}
            for f in as_completed(futures): src_data[futures[f]] = f.result()

        dst_data = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(_count_table, config['destination'], t): t for t in dst_to_count}
            for f in as_completed(futures): dst_data[futures[f]] = f.result()

        results = []
        for table in filtered:
            src_count, src_cols = src_data.get(table, (-1, set()))
            if table in dst_table_set:
                dst_count, dst_cols  = dst_data.get(table, (-1, set()))
                missing_col_count    = len(src_cols - dst_cols) if src_cols else 0
                status = 'ok' if src_count == dst_count else 'diff'
            else:
                dst_count, dst_cols  = -1, set()
                missing_col_count    = 0
                status = 'missing'
            results.append({
                'table': table, 'source_count': src_count,
                'destination_count': dst_count,
                'diff': src_count - max(dst_count, 0),
                'status': status,
                'has_missing_cols': missing_col_count > 0,
                'missing_col_count': missing_col_count,
            })
        return jsonify({'results': results, 'pattern': pattern})
    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


_pg_col_cache = {}   # cache {table_name: {col_name: default_val}}

def _get_pg_col_defaults(conn, table_name):
    """ดึง default ที่เหมาะสมสำหรับ NOT NULL column ใน PostgreSQL"""
    key = table_name.lower()
    if key in _pg_col_cache:
        return _pg_col_cache[key]
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT column_name, data_type, is_nullable, udt_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            ORDER BY ordinal_position
        """, (key,))
        rows = cur.fetchall()
        cur.close()
        defaults = {}
        for row in rows:
            col, dtype, nullable, udt = row[0], row[1].lower(), row[2], row[3].lower()
            if nullable != 'NO':
                continue
            if any(t in dtype for t in ('int','numeric','float','double','real','smallint','bigint')):
                defaults[col] = 0
            elif 'bool' in dtype:
                defaults[col] = False
            elif any(t in dtype for t in ('date','timestamp','time')):
                defaults[col] = datetime.datetime(1900, 1, 1, 0, 0, 0)
            elif 'uuid' in dtype or udt == 'uuid':
                defaults[col] = '00000000-0000-0000-0000-000000000000'
            else:
                defaults[col] = ''   # varchar, text, char, etc.
        if rows:   # only cache if query returned results
            _pg_col_cache[key] = defaults
        return defaults
    except Exception:
        return {}


def _check_table_pk(src_config, table_name):
    try:
        conn, db_type = get_connection(src_config)
        pks   = get_primary_keys(conn, db_type, table_name, src_config.get('database', ''))
        count = get_record_count(conn, db_type, table_name)
        conn.close()
        return {'table': table_name, 'has_pk': len(pks) > 0, 'pk_columns': pks, 'record_count': count}
    except Exception as e:
        return {'table': table_name, 'has_pk': None, 'pk_columns': [], 'record_count': -1, 'error': str(e)}


@app.route('/api/tables-no-pk', methods=['GET'])
def get_tables_no_pk():
    config = load_config()
    try:
        conn, db_type = get_connection(config['source'])
        tables = get_tables(conn, db_type, config['source']['database'])
        conn.close()

        results = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(_check_table_pk, config['source'], t): t for t in tables}
            for f in as_completed(futures):
                r = f.result()
                results[r['table']] = r

        no_pk = sorted(
            [r for r in results.values() if r.get('has_pk') is False],
            key=lambda x: x['table']
        )
        return jsonify({
            'tables': no_pk,
            'total_checked': len(tables),
            'total_no_pk': len(no_pk)
        })
    except Exception as e:
        import traceback
        return jsonify({'status': 'error', 'message': str(e),
                        'trace': traceback.format_exc()}), 400


if __name__ == '__main__':
    app.run(debug=True, port=8000, host='0.0.0.0')
