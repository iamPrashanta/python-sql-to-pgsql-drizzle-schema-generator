from flask import Flask, render_template, request
import re

app = Flask(__name__)

import re

def sql_to_drizzle_old(sql_text: str) -> str:
    drizzle_code = []
    enums = []

    # --- normalize SQL ---
    sql_text = sql_text.replace("`", "").strip()

    # --- table name ---
    match = re.search(r"CREATE\s+TABLE\s+(\w+)", sql_text, re.IGNORECASE)
    table_name = match.group(1) if match else "unknown"

    drizzle_code.append(f'export const {table_name} = pgTable("{table_name}", {{')

    # --- extract column definitions ---
    inside = re.search(r"\((.*)\)\s*(ENGINE|$)", sql_text, re.DOTALL | re.IGNORECASE)
    if inside:
        raw_cols = re.split(r",(?![^(]*\))", inside.group(1))
        columns = [col.strip() for col in raw_cols if col.strip()]
        primary_keys = []

        for col in columns:
            # Handle table-level PRIMARY KEY
            pk_match = re.match(r"PRIMARY KEY\s*\(([^)]+)\)", col, re.IGNORECASE)
            if pk_match:
                primary_keys = [c.strip() for c in pk_match.group(1).split(",")]
                continue

            parts = col.split()
            col_name = parts[0].strip()
            col_type = parts[1].upper() if len(parts) > 1 else ""

            # Handle AUTO_INCREMENT / GENERATED ALWAYS AS IDENTITY first
            if "AUTO_INCREMENT" in col.upper() or "GENERATED ALWAYS AS IDENTITY" in col.upper():
                drizzle_code.append(
                    f'  {col_name}: integer("{col_name}").primaryKey().generatedAlwaysAsIdentity(),'
                )
                continue

            # Then handle INT types
            if "INT" in col_type or "BIGINT" in col_type or "SMALLINT" in col_type:
                not_null = ".notNull()" if "NOT NULL" in col.upper() else ""
                drizzle_code.append(f'  {col_name}: integer("{col_name}"){not_null},')

            # VARCHAR / CHAR
            elif "VARCHAR" in col_type or "CHAR" in col_type:
                length_match = re.search(r"\((\d+)\)", col_type)
                length_val = length_match.group(1) if length_match else "255"
                not_null = ".notNull()" if "NOT NULL" in col.upper() else ""
                drizzle_code.append(f'  {col_name}: varchar("{col_name}", {{ length: {length_val} }}){not_null},')

            # TEXT / LONGTEXT
            elif "TEXT" in col_type or "LONGTEXT" in col_type:
                not_null = ".notNull()" if "NOT NULL" in col.upper() else ""
                drizzle_code.append(f'  {col_name}: text("{col_name}"){not_null},')

            # FLOAT / DOUBLE / DECIMAL
            elif "DOUBLE" in col_type or "FLOAT" in col_type or "DECIMAL" in col_type:
                default_match = re.search(r"DEFAULT\s+([\d.]+)", col, re.IGNORECASE)
                default_val = f'.default({default_match.group(1)})' if default_match else ""
                not_null = ".notNull()" if "NOT NULL" in col.upper() else ""
                drizzle_code.append(f'  {col_name}: double("{col_name}"){default_val}{not_null},')

            # BOOLEAN / TINYINT(1)
            elif "BOOLEAN" in col_type or "TINYINT(1)" in col_type:
                default_match = re.search(r"DEFAULT\s+(true|false|0|1)", col, re.IGNORECASE)
                default_val = f'.default({default_match.group(1).lower()})' if default_match else ""
                not_null = ".notNull()" if "NOT NULL" in col.upper() else ""
                drizzle_code.append(f'  {col_name}: boolean("{col_name}"){default_val}{not_null},')

            # DATETIME / TIMESTAMP / DATE
            elif "DATETIME" in col_type or "TIMESTAMP" in col_type or "DATE" in col_type:
                default_now = ".defaultNow()" if "DEFAULT CURRENT_TIMESTAMP" in col.upper() else ""
                not_null = ".notNull()" if "NOT NULL" in col.upper() else ""
                drizzle_code.append(f'  {col_name}: timestamp("{col_name}"){default_now}{not_null},')

            # ENUM
            elif "ENUM" in col.upper():
                values_match = re.search(r"ENUM\s*\(([^)]+)\)", col, re.IGNORECASE)
                if values_match:
                    values = [v.strip().strip("'") for v in values_match.group(1).split(",")]
                    enum_name = f"{col_name}Enum"
                    enums.append(f"export const {enum_name} = pgEnum('{col_name}', {values});")
                    default_match = re.search(r"DEFAULT\s+'([^']+)'", col, re.IGNORECASE)
                    default_val = f'.default("{default_match.group(1)}")' if default_match else ""
                    not_null = ".notNull()" if "NOT NULL" in col.upper() else ""
                    drizzle_code.append(f'  {col_name}: {enum_name}("{col_name}"){default_val}{not_null},')
                else:
                    drizzle_code.append(f'  {col_name}: text("{col_name}"),')

            # fallback
            else:
                drizzle_code.append(f'  {col_name}: text("{col_name}"),')

        # handle table-level primary keys
        for pk in primary_keys:
            # check if pk already included via AUTO_INCREMENT; skip if exists
            pk_name = pk.strip()
            if not any(pk_name in line for line in drizzle_code):
                drizzle_code.append(f'  {pk_name}: integer("{pk_name}").primaryKey(),')

    drizzle_code.append("});")
    return "\n".join(enums + drizzle_code)

def sql_to_drizzle(sql_text: str) -> str:
    """
    Convert a single CREATE TABLE SQL definition to Drizzle ORM (pg-core) schema code.

    Handles:
    - Column types (int, varchar, text, boolean, decimal/double/float, timestamp/datetime/date, enum).
    - Column-level constraints: NOT NULL, DEFAULT, PRIMARY KEY, REFERENCES ... (with ON DELETE/ON UPDATE).
    - Table-level PRIMARY KEY (including composite) and FOREIGN KEY (including composite) constraints.
    - Identity/auto-increment: maps to .generatedAlwaysAsIdentity() on integer PK columns (Postgres style).
    - Inference rules requested:
      1) If a column is AUTO_INCREMENT/IDENTITY, force integer + primary key + generatedAlwaysAsIdentity(),
         even if a type is not specified.
      2) If a column-level PRIMARY KEY is present with no explicit type, treat it as integer.
      3) If a table-level PRIMARY KEY targets a column with no explicit type, set that column to varchar(200).
    """

    # --- normalize SQL ---
    sql_text = (sql_text or "").strip()
    # strip backticks and redundant whitespace; unify spaces
    sql_text = re.sub(r"`", "", sql_text)
    sql_text = re.sub(r"\s+", " ", sql_text)

    # --- table name ---
    # capture CREATE TABLE [IF NOT EXISTS] <name>
    m_table = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)", sql_text, re.IGNORECASE)
    table_name = m_table.group(1) if m_table else "unknown"

    # --- extract the (...) block for columns/constraints ---
    # find the first '(' after CREATE TABLE ... and match the corresponding ')'
    start = sql_text.find("(")
    if start == -1:
        return f"// Failed to parse table definition for {table_name}"
    depth = 0
    end = -1
    for i in range(start, len(sql_text)):
        if sql_text[i] == "(":
            depth += 1
        elif sql_text[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return f"// Failed to parse columns for {table_name}"

    inside = sql_text[start + 1 : end].strip()

    # split by commas not inside parentheses
    items = [x.strip() for x in re.split(r",(?![^()]*\))", inside) if x.strip()]

    # structures
    columns = {}  # name -> dict with parsed info
    table_level_pk = []  # PK cols captured from table constraint
    table_level_fks = []  # list of {cols, ref_table, ref_cols, onDelete, onUpdate}

    enums = []  # "export const ... = pgEnum(...)" strings

    # helpers
    def parse_actions(tail_upper: str):
        on_delete = None
        on_update = None
        mdel = re.search(r"ON\s+DELETE\s+(CASCADE|SET\s+NULL|RESTRICT|NO\s+ACTION)", tail_upper, re.IGNORECASE)
        mupd = re.search(r"ON\s+UPDATE\s+(CASCADE|SET\s+NULL|RESTRICT|NO\s+ACTION)", tail_upper, re.IGNORECASE)
        if mdel:
            on_delete = mdel.group(1).lower().replace(" ", "")
        if mupd:
            on_update = mupd.group(1).lower().replace(" ", "")
        # convert noaction -> no action (drizzle expects 'no action'?) Use 'no action' literal:
        if on_delete == "noaction":
            on_delete = "no action"
        if on_update == "noaction":
            on_update = "no action"
        return on_delete, on_update

    # --- pass 1: collect columns + table constraints ---
    for item in items:
        upper = item.upper()

        # Table-level PRIMARY KEY(colA, colB, ...)
        m_tpk = re.match(r"PRIMARY\s+KEY\s*\(([^)]+)\)", upper, re.IGNORECASE)
        if m_tpk:
            table_level_pk = [c.strip().strip('"').strip() for c in m_tpk.group(1).split(",")]
            continue

        # Table-level FOREIGN KEY (colA, colB) REFERENCES refTable(refA, refB) [actions...]
        m_tfk = re.match(
            r"FOREIGN\s+KEY\s*\(([^)]+)\)\s*REFERENCES\s+([A-Za-z_]\w*)\s*\(([^)]+)\)\s*(.*)$",
            item,
            re.IGNORECASE,
        )
        if m_tfk:
            cols = [c.strip() for c in m_tfk.group(1).split(",")]
            ref_table = m_tfk.group(2).strip()
            ref_cols = [c.strip() for c in m_tfk.group(3).split(",")]
            tail = m_tfk.group(4) or ""
            od, ou = parse_actions(tail.upper())
            table_level_fks.append(
                {
                    "cols": cols,
                    "ref_table": ref_table,
                    "ref_cols": ref_cols,
                    "onDelete": od,
                    "onUpdate": ou,
                }
            )
            continue

        # Column definition
        # Format: <name> <type/enum/constraints> ...
        parts = item.split(None, 1)
        if not parts:
            continue
        col_name = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        rest_upper = rest.upper()

        col = {
            "name": col_name,
            "ts_type": "",        # 'integer' | 'varchar' | 'text' | 'double' | 'boolean' | 'timestamp' | 'enum'
            "length": None,       # for varchar/char numeric length
            "not_null": "NOT NULL" in rest_upper,
            "default": None,      # raw default value string (e.g., '1', '"abc"', 'now()')
            "primary": "PRIMARY KEY" in rest_upper,  # column-level PK
            "identity": ("AUTO_INCREMENT" in rest_upper) or ("GENERATED ALWAYS AS IDENTITY" in rest_upper) or ("GENERATED BY DEFAULT AS IDENTITY" in rest_upper),
            "enum_name": None,    # TS enum const to call (pgEnum)
            "enum_values": None,  # list of literal strings
            "references": None,   # { table, column, onDelete, onUpdate }
            "raw_type": "",       # matched raw SQL type token
        }

        # --- detailed checks before type mapping ---
        # 1) ENUM(...) capture and pgEnum emission:
        #    - If ENUM is present, capture values and register `export const <col>Enum = pgEnum('<col>', [...])`.
        #    - The column builder will call <col>Enum('<col>') and apply default/notNull if present.
        m_enum = re.search(r"ENUM\s*\(([^)]*)\)", rest, re.IGNORECASE)
        if m_enum:
            vals = [v.strip().strip("'").strip('"') for v in re.split(r",(?![^()]*\))", m_enum.group(1)) if v.strip()]
            enum_name = f"{col_name}Enum"
            enums.append(f"export const {enum_name} = pgEnum('{col_name}', {vals});")
            col["enum_name"] = enum_name
            col["enum_values"] = vals
            col["ts_type"] = "enum"
            col["raw_type"] = "ENUM"

        # 2) Column-level REFERENCES parsing:
        #    - If "REFERENCES refTable(refCol)" appears, capture ref and optional ON DELETE/ON UPDATE actions.
        m_ref = re.search(r"REFERENCES\s+([A-Za-z_]\w*)\s*\(\s*([A-Za-z_]\w*)\s*\)\s*(.*)$", rest, re.IGNORECASE)
        if m_ref:
            ref_table = m_ref.group(1)
            ref_col = m_ref.group(2)
            tail = m_ref.group(3) or ""
            od, ou = parse_actions(tail.upper())
            col["references"] = {"table": ref_table, "column": ref_col, "onDelete": od, "onUpdate": ou}

        # 3) DEFAULT value extraction:
        #    - Capture DEFAULT literals like numbers, strings, booleans, or functions like now().
        m_def_str = re.search(r"DEFAULT\s+'([^']*)'", rest, re.IGNORECASE)
        m_def_quoted = re.search(r'DEFAULT\s+"([^"]*)"', rest, re.IGNORECASE)
        m_def_num = re.search(r"DEFAULT\s+([+-]?\d+(?:\.\d+)?)", rest, re.IGNORECASE)
        m_def_bool = re.search(r"DEFAULT\s+(true|false|TRUE|FALSE|0|1)", rest, re.IGNORECASE)
        m_def_fn = re.search(r"DEFAULT\s+([A-Za-z_]\w*\s*\([^)]*\))", rest, re.IGNORECASE)
        if m_def_str:
            col["default"] = f'"{m_def_str.group(1)}"'
        elif m_def_quoted:
            col["default"] = f'"{m_def_quoted.group(1)}"'
        elif m_def_bool:
            val = m_def_bool.group(1).lower()
            if val in ("0", "1"):
                val = "true" if val == "1" else "false"
            col["default"] = val
        elif m_def_num:
            col["default"] = m_def_num.group(1)
        elif m_def_fn:
            # preserve SQL-ish func defaults like now()
            col["default"] = m_def_fn.group(1)

        # 4) Type detection and inference:
        #    - If type is explicitly present, map accordingly.
        #    - If AUTO_INCREMENT/IDENTITY, force integer primary key with identity.
        #    - If PRIMARY KEY and no type given at column-level, infer integer.
        #    - Fallback to text if unknown.
        if col["ts_type"] != "enum":
            rest_up = rest_upper
            # explicit numeric ints
            if re.search(r"\b(BIGINT|SMALLINT|INT|INTEGER)\b", rest_up):
                col["ts_type"] = "integer"
                col["raw_type"] = "INT"
            # varchar/char with optional length
            elif "VARCHAR" in rest_up or re.search(r"\bCHAR\b", rest_up):
                m_len = re.search(r"\((\d+)\)", rest, re.IGNORECASE)
                col["ts_type"] = "varchar"
                col["length"] = int(m_len.group(1)) if m_len else 255
                col["raw_type"] = "VARCHAR"
            # text/longtext
            elif "LONGTEXT" in rest_up or re.search(r"\bTEXT\b", rest_up):
                col["ts_type"] = "text"
                col["raw_type"] = "TEXT"
            # double/float/decimal
            elif re.search(r"\b(DOUBLE|FLOAT|DECIMAL|NUMERIC)\b", rest_up):
                col["ts_type"] = "double"
                col["raw_type"] = "DOUBLE"
            # boolean / tinyint(1)
            elif "BOOLEAN" in rest_up or "TINYINT(1)" in rest_up:
                col["ts_type"] = "boolean"
                col["raw_type"] = "BOOLEAN"
            # datetime/timestamp/date
            elif re.search(r"\b(DATETIME|TIMESTAMP|DATE)\b", rest_up):
                col["ts_type"] = "timestamp"
                col["raw_type"] = "TIMESTAMP"
            else:
                # unknown/missing type at column-level -> infer later
                col["ts_type"] = ""
                col["raw_type"] = ""

            # Identity/auto-increment inference takes precedence
            if col["identity"]:
                col["ts_type"] = "integer"
                col["primary"] = True

            # Column-level PK with no explicit type -> infer integer
            if col["primary"] and not col["ts_type"]:
                col["ts_type"] = "integer"

            # If still no type, fallback to text
            if not col["ts_type"]:
                col["ts_type"] = "text"

        columns[col_name] = col

    # --- pass 2: resolve table-level PK inference rules ---
    # If single-column table-level PK and column lacks primary flag, mark it primary.
    # If a PK column has no explicit type at its column line (raw_type empty), set to varchar(200) (requested rule).
    composite_pk = None
    if table_level_pk:
        if len(table_level_pk) == 1:
            pkc = table_level_pk[0]
            if pkc in columns:
                columns[pkc]["primary"] = True
                if not columns[pkc]["raw_type"]:
                    columns[pkc]["ts_type"] = "varchar"
                    columns[pkc]["length"] = 200
        else:
            # composite primary key handled in builder
            composite_pk = [c for c in table_level_pk if c in columns]
            # still, nudge any pk column with missing type to varchar(200) per request
            for pkc in composite_pk:
                if not columns[pkc]["raw_type"]:
                    columns[pkc]["ts_type"] = "varchar"
                    columns[pkc]["length"] = 200

    # --- render Drizzle code ---
    lines = []
    # enums first
    for e in enums:
        lines.append(e)
    # table start
    lines.append(f'export const {table_name} = pgTable("{table_name}", {{')

    # column builders
    def col_builder(c):
        name = c["name"]
        t = c["ts_type"]
        nn = ".notNull()" if c["not_null"] else ""
        dv = f'.default({c["default"]})' if c["default"] is not None else ""
        pk = ".primaryKey()" if c["primary"] else ""
        ident = ".generatedAlwaysAsIdentity()" if c["identity"] else ""
        ref = ""
        if c["references"]:
            on_opts = []
            if c["references"]["onDelete"]:
                on_opts.append(f"onDelete: '{c['references']['onDelete']}'")
            if c["references"]["onUpdate"]:
                on_opts.append(f"onUpdate: '{c['references']['onUpdate']}'")
            opt_str = f", {{ {', '.join(on_opts)} }}" if on_opts else ""
            ref = f'.references(() => {c["references"]["table"]}.{c["references"]["column"]}{opt_str})'

        if t == "enum":
            return f'  {name}: {c["enum_name"]}("{name}"){dv}{nn}{pk}{ident}{ref},'
        if t == "integer":
            return f'  {name}: integer("{name}"){dv}{nn}{pk}{ident}{ref},'
        if t == "varchar":
            length = c["length"] if c["length"] else 255
            return f'  {name}: varchar("{name}", {{ length: {length} }}){dv}{nn}{pk}{ident}{ref},'
        if t == "text":
            return f'  {name}: text("{name}"){dv}{nn}{pk}{ident}{ref},'
        if t == "double":
            return f'  {name}: double("{name}"){dv}{nn}{pk}{ident}{ref},'
        if t == "boolean":
            return f'  {name}: boolean("{name}"){dv}{nn}{pk}{ident}{ref},'
        if t == "timestamp":
            # defaultNow() if DEFAULT CURRENT_TIMESTAMP like behavior was captured as default fn 'now()'
            return f'  {name}: timestamp("{name}"){dv}{nn}{pk}{ident}{ref},'
        # fallback
        return f'  {name}: text("{name}"){dv}{nn}{pk}{ident}{ref},'

    for col in columns.values():
        lines.append(col_builder(col))

    lines.append("}",)

    # third-arg builder for composite PKs and multi-column FKs
    builder_entries = []

    if composite_pk and len(composite_pk) > 1:
        cols_expr = ", ".join([f"table.{c}" for c in composite_pk])
        builder_entries.append(f"pk: primaryKey({cols_expr})")

    # Table-level FKs: if single-col, prefer appending to column if none already; if multi-col or already present, use foreignKey(...)
    for tfk in table_level_fks:
        cols = tfk["cols"]
        ref_table = tfk["ref_table"]
        ref_cols = tfk["ref_cols"]
        od = tfk["onDelete"]
        ou = tfk["onUpdate"]

        if len(cols) == 1 and cols[0] in columns and not columns[cols[0]]["references"]:
            # patch into column via references()
            c = columns[cols[0]]
            c["references"] = {"table": ref_table, "column": ref_cols[0], "onDelete": od, "onUpdate": ou}
        else:
            cols_expr = ", ".join([f"table.{c}" for c in cols if c in columns])
            ref_expr = ", ".join([f"{ref_table}.{rc}" for rc in ref_cols])
            opt_parts = []
            if od:
                opt_parts.append(f"onDelete: '{od}'")
            if ou:
                opt_parts.append(f"onUpdate: '{ou}'")
            opt_str = f", {{ {', '.join(opt_parts)} }}" if opt_parts else ""
            builder_entries.append(
                f"fk_{'_'.join(cols)}_{ref_table}: foreignKey({{ columns: [{cols_expr}], foreignColumns: [{ref_expr}] }}{opt_str})"
            )


    if builder_entries:
        lines.append(", (table) => ({")
        for be in builder_entries:
            lines.append(f"  {be},")
        lines.append("})")

    lines.append(");")
    return "\n".join(lines)

@app.route("/", methods=["GET", "POST"])
def index():
    output = None
    error = None
    if request.method == "POST":
        raw_sql = request.form.get("sql", "")
        try:
            output = sql_to_drizzle(raw_sql)
        except Exception as exc:
            error = f"Failed to convert SQL: {exc}"
    return render_template("index.html", output=output, error=error)

if __name__ == "__main__":
    app.run(debug=True)

@app.route("/", methods=["GET", "POST"])
def index():
    output = None
    if request.method == "POST":
        raw_sql = request.form.get("sql")
        output = sql_to_drizzle(raw_sql)
    return render_template("index.html", output=output)

if __name__ == "__main__":
    app.run(debug=True)
