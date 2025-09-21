from flask import Flask, render_template, request, jsonify
import re

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
#  Helper: convert a single MySQL CREATE TABLE
#  Returns {'drizzle': “…TypeScript…”, 'pgsql': “…raw SQL…”}
# ─────────────────────────────────────────────────────────────
def convert_mysql(sql: str) -> dict[str, str]:
    # Clean input SQL
    sql_clean = (sql or '').strip()
    sql_clean = re.sub(r'`', '', sql_clean)
    # Remove CHARACTER SET / COLLATE
    sql_clean = re.sub(r'CHARACTER SET \w+ COLLATE \w+', '', sql_clean, flags=re.I)
    sql_clean = re.sub(r'\s+', ' ', sql_clean)

    # table name
    m_table = re.search(r'CREATE TABLE (?:IF NOT EXISTS )?([A-Za-z_]\w*)', sql_clean, re.I)
    if not m_table:
        return {"drizzle": "// failed to parse", "pgsql": sql_clean}
    tbl = m_table.group(1)

    # column / constraint list
    start = sql_clean.find('(')
    depth = 0
    end = -1
    for i, ch in enumerate(sql_clean[start:], start):
        depth += (ch == '(') - (ch == ')')
        if depth == 0:
            end = i
            break
    if end == -1:
        return {"drizzle": "// incomplete SQL", "pgsql": sql_clean}

    # split top-level columns/constraints
    items = [s.strip() for s in re.split(r',(?![^()]*\))', sql_clean[start + 1:end]) if s.strip()]

    enums: dict[str, list[str]] = {}
    cols, need_trigger = [], False

    # ── pass 1: parse each line ───────────────────────────────
    for raw in items:
        # ENUM
        if (m := re.match(r'(\w+)\s+enum\s*\(([^)]*)\)', raw, re.I)):
            name, vals = m[1], [v.strip().strip("'\"") for v in m[2].split(',')]
            enums[name] = vals
            cols.append(dict(name=name, kind='enum', default=_def(raw), nn='NOT NULL' in raw.upper(),
                             vals=vals, created=False, on_upd=False))
            continue

        parts = raw.split()
        if not parts:
            continue
        name, sql_type = parts[0], parts[1]
        rest = raw[len(name) + len(sql_type):]

        nn      = 'NOT NULL' in rest.upper() or 'NOT NULL' in sql_type.upper()
        default = _def(rest)
        on_upd  = name.lower() == 'updated_at' and 'ON UPDATE CURRENT_TIMESTAMP' in rest.upper()
        if on_upd: need_trigger = True
        created  = name.lower() == 'created_at'

        kind, length = _kind(sql_type)

        # audit columns → timestamp
        if name.lower() in ('created_at', 'updated_at', 'deleted_at'):
            kind = 'timestamp'

        cols.append(dict(name=name, kind=kind, length=length, nn=nn,
                         default=default, vals=None,
                         created=created, on_upd=on_upd))

    # ── produce Drizzle code ────────────────────────────────
    ts_lines = [f"export const {n}Enum = pgEnum('{n}', {v});" for n, v in enums.items()]
    ts_lines.append(f'export const {tbl} = pgTable("{tbl}", {{')

    for c in cols:
        base = _drizzle_base(c)
        chain = []
        if c['created']:
            chain.append('.defaultNow().notNull()')
        elif c['default'] is not None:
            chain.append(f'.default({c["default"]})')
        if c['nn'] and not c['created']:
            chain.append('.notNull()')
        if c['on_upd']:
            chain.append('.$onUpdate(() => sql`CURRENT_TIMESTAMP`)')
        ts_lines.append(f"  {c['name']}: {base}{''.join(chain)},")
    ts_lines.append('});')
    drizzle_ts = '\n'.join(ts_lines)

    # ── produce PostgreSQL DDL ──────────────────────────────
    pg_enum_sql = [f"CREATE TYPE {n} AS ENUM ({', '.join(repr(x) for x in v)});" for n, v in enums.items()]

    col_defs = []
    for c in cols:
        typ = ('NUMERIC(11,2)' if c['kind'] == 'numeric' else
               f"varchar({c['length']})" if c['kind'] == 'varchar' else
               c['name'] if c['kind'] == 'enum' else
               c['kind'].upper())
        dflt = f" DEFAULT {c['default']}" if c['default'] else ''
        nn   = ' NOT NULL' if c['nn'] else ''
        col_defs.append(f"{c['name']} {typ}{dflt}{nn}")

    pg_table = f'CREATE TABLE "{tbl}" (\n  ' + ',\n  '.join(col_defs) + "\n);"

    trigger_sql = ''
    if need_trigger:
        fn  = f"set_updated_at_{tbl}"
        trg = f"{tbl}_set_updated_at"
        trigger_sql = f"""
CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER {trg}
BEFORE UPDATE ON "{tbl}"
FOR EACH ROW EXECUTE FUNCTION {fn}();
""".strip()

    pg_sql = '\n'.join(pg_enum_sql + [pg_table, trigger_sql]).strip()

    return {'drizzle': drizzle_ts, 'pgsql': pg_sql}


# ── helpers ────────────────────────────────────────────────
def _def(segment: str):
    m = (re.search(r"DEFAULT\s+'([^']*)'", segment, re.I) or
         re.search(r'DEFAULT\s+"([^"]*)"', segment, re.I) or
         re.search(r'DEFAULT\s+([+-]?\d+(?:\.\d+)?)', segment, re.I) or
         re.search(r'DEFAULT\s+(true|false|TRUE|FALSE|0|1)', segment, re.I))
    if not m: return None
    val = m.group(1)
    if val.lower() in ('true', 'false', '0', '1'):
        return 'true' if val.lower() in ('1', 'true') else 'false'
    try:
        float(val)
        return val
    except:
        return f'"{val}"'


def _kind(sql_type: str):
    sql_type = sql_type.upper()
    if sql_type.startswith('DOUBLE'): return 'numeric', None
    if sql_type.startswith(('NUMERIC', 'DECIMAL', 'FLOAT')): return 'numeric', None
    if 'VARCHAR' in sql_type:
        m = re.search(r'\((\d+)\)', sql_type)
        return 'varchar', int(m.group(1)) if m else 255
    if 'CHAR' in sql_type: return 'varchar', 1
    if 'TINYINT(1)' in sql_type: return 'boolean', None
    if 'INT' in sql_type: return 'integer', None
    if 'BIGINT' in sql_type: return 'integer', None
    if 'TEXT' in sql_type or 'LONGTEXT' in sql_type: return 'text', None
    if any(x in sql_type for x in ('TIMESTAMP', 'DATETIME', 'DATE')): return 'timestamp', None
    return 'text', None


def _drizzle_base(c):
    if c['kind'] == 'enum':
        return f"{c['name']}Enum('{c['name']}')"
    if c['kind'] == 'numeric':
        return f"numeric('{c['name']}', {{ precision: 11, scale: 2 }})"
    if c['kind'] == 'varchar':
        return f"varchar('{c['name']}', {{ length: {c['length'] or 255} }})"
    return f"{c['kind']}('{c['name']}')"


# ── Flask routes ─────────────────────────────────────────────
@app.route("/api/drizzle", methods=["POST"])
def api_drizzle():
    data = request.get_json(silent=True) or {}
    sql = data.get("sql", "")
    try:
        res = convert_mysql(sql)
        return jsonify({"ok": True, "drizzle": res["drizzle"], "pgsql": res["pgsql"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/postgres", methods=["POST"])
def api_postgres():
    data = request.get_json(silent=True) or {}
    sql = data.get("sql", "")
    try:
        res = convert_mysql(sql)
        return jsonify({"ok": True, "pgsql": res["pgsql"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")



if __name__ == "__main__":
    app.run(debug=True)
