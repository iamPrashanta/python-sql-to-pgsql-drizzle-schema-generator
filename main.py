from flask import Flask, render_template, request
import sqlparse
import re

app = Flask(__name__)

import re

def sql_to_drizzle(sql_text: str) -> str:
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

            # 1️⃣ Handle AUTO_INCREMENT / GENERATED ALWAYS AS IDENTITY first
            if "AUTO_INCREMENT" in col.upper() or "GENERATED ALWAYS AS IDENTITY" in col.upper():
                drizzle_code.append(
                    f'  {col_name}: integer("{col_name}").primaryKey().generatedAlwaysAsIdentity(),'
                )
                continue

            # 2️⃣ Then handle INT types
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

@app.route("/", methods=["GET", "POST"])
def index():
    output = None
    if request.method == "POST":
        raw_sql = request.form.get("sql")
        output = sql_to_drizzle(raw_sql)
    return render_template("index.html", output=output)

if __name__ == "__main__":
    app.run(debug=True)
