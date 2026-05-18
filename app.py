from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import re
import json
import os
import decimal
import datetime
import sys


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
    if isinstance(obj, bytes):
        try:
            return obj.decode('utf-8')
        except Exception:
            return obj.hex()
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
            connect_timeout=10
        )
        return conn, 'postgresql'
    elif db_type == 'mysql':
        import pymysql
        conn = pymysql.connect(
            host=host, port=port, database=database,
            user=username, password=password,
            connect_timeout=10,
            read_timeout=300,
            write_timeout=300,
            cursorclass=pymysql.cursors.DictCursor,
            charset='utf8mb4'
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


def _fetch_pks(db_config, table_name, pks, result_dict, key):
    """Thread worker: ดึง PK set ด้วย connection แยก"""
    try:
        conn, db_type = get_connection(db_config)
        result_dict[key] = get_all_pks(conn, db_type, table_name, pks)
        conn.close()
    except Exception:
        result_dict[key] = set()


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


def get_all_pks(conn, db_type, table_name, pks):
    pk_cols = ', '.join([q(pk, db_type) for pk in pks])
    cur = conn.cursor()
    cur.execute(f'SELECT {pk_cols} FROM {q(table_name, db_type)}')
    result = set()
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for row in rows:
            if isinstance(row, dict):
                pk_tuple = tuple(row[pk] for pk in pks)
            else:
                pk_tuple = tuple(row) if len(pks) > 1 else (row[0],)
            result.add(pk_tuple)
    cur.close()
    return result


def fetch_records_by_pks(conn, db_type, table_name, pks, pk_values):
    if not pk_values:
        return [], []

    params = []
    if len(pks) == 1:
        placeholders = ', '.join(['%s'] * len(pk_values))
        where = f'{q(pks[0], db_type)} IN ({placeholders})'
        params = [v[0] for v in pk_values]
    else:
        conditions = []
        for vals in pk_values:
            cond = ' AND '.join([f'{q(pks[i], db_type)} = %s' for i in range(len(pks))])
            conditions.append(f'({cond})')
            params.extend(vals)
        where = ' OR '.join(conditions)

    if db_type == 'postgresql':
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()

    cur.execute(f'SELECT * FROM {q(table_name, db_type)} WHERE {where}', params)
    rows = cur.fetchall()

    col_names = []
    if cur.description:
        col_names = [d[0] for d in cur.description]

    records = []
    for row in rows:
        if isinstance(row, dict):
            records.append(dict(row))
        else:
            records.append(dict(zip(col_names, row)))

    cur.close()
    return records, col_names


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
                # ดึง PK ทั้ง 2 ฝั่งพร้อมกันด้วย connection แยก
                pk_results = {}
                t1 = threading.Thread(target=_fetch_pks,
                    args=(config['source'], table_name, pks, pk_results, 'src'))
                t2 = threading.Thread(target=_fetch_pks,
                    args=(config['destination'], table_name, pks, pk_results, 'dst'))
                t1.start(); t2.start()
                t1.join();  t2.join()
                src_pk_set = pk_results.get('src', set())
                dst_pk_set = pk_results.get('dst', set())

                missing_pks = src_pk_set - dst_pk_set
                result['total_missing'] = len(missing_pks)

                limit = None if fetch_all else MAX_DISPLAY_RECORDS
                display_pks = list(missing_pks) if limit is None else list(missing_pks)[:limit]
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

        missing_pks = list(src_pk_set - dst_pk_set)
        total_missing = len(missing_pks)

        if total_missing == 0:
            src_conn.close()
            dst_conn.close()
            return jsonify({'status': 'ok', 'inserted': 0,
                            'message': 'ไม่มีข้อมูลที่ต้องเพิ่ม'})

        inserted = 0
        error_details = []   # เก็บรายละเอียด error แต่ละ record
        error_summary = {}   # จัดกลุ่ม error ประเภทเดียวกัน
        batch_size = 100

        for i in range(0, len(missing_pks), batch_size):
            batch = missing_pks[i:i + batch_size]
            records, col_names = fetch_records_by_pks(
                src_conn, src_type, table_name, pks, batch)

            if not records:
                continue

            for rec in records:
                cols = list(rec.keys())
                vals = [rec[c] for c in cols]
                pk_val = ', '.join(str(rec.get(p, '?')) for p in pks)

                if dst_type == 'postgresql':
                    col_str = ', '.join([f'"{c}"' for c in cols])
                    val_str = ', '.join(['%s'] * len(vals))
                    sql = (f'INSERT INTO "{table_name}" ({col_str}) '
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
                    # PostgreSQL: ต้อง rollback ก่อน ไม่งั้น transaction abort
                    try:
                        dst_conn.rollback()
                    except Exception:
                        pass

                    err_type  = type(e).__name__
                    err_msg   = str(e).strip().split('\n')[0]  # บรรทัดแรกของ error
                    full_key  = f'{err_type}: {err_msg}'

                    # จัดกลุ่ม error ที่เหมือนกัน
                    if full_key not in error_summary:
                        error_summary[full_key] = {'count': 0, 'pk_examples': []}
                    error_summary[full_key]['count'] += 1
                    if len(error_summary[full_key]['pk_examples']) < 3:
                        error_summary[full_key]['pk_examples'].append(pk_val)

                    # เก็บ detail เต็มๆ ของ 20 errors แรก
                    if len(error_details) < 20:
                        # หาค่าที่ผิดปกติ: ลอง serialize แต่ละ field
                        bad_fields = []
                        for col, val in zip(cols, vals):
                            if val is not None and isinstance(val, str) and len(val) > 200:
                                bad_fields.append(f'{col}(ยาวเกิน:{len(val)})')
                        error_details.append({
                            'pk': pk_val,
                            'error_type': err_type,
                            'error_msg': err_msg,
                            'bad_fields': bad_fields
                        })

        src_conn.close()
        dst_conn.close()

        total_errors = sum(v['count'] for v in error_summary.values())
        msg = f'เพิ่มข้อมูลสำเร็จ {inserted} รายการ จากทั้งหมด {total_missing} รายการ'
        if total_errors:
            msg += f' (ผิดพลาด {total_errors} รายการ)'

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
        src_data = {}   # {table: (count, col_set)}
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


if __name__ == '__main__':
    app.run(debug=True, port=8000, host='0.0.0.0')
