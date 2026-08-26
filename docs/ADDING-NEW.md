# Adding New Features

## Adding a New Strategy

**Time:** ~15 minutes

1. Create `src/backtest/strategies/my_strategy.py`:
```python
import pandas as pd
from backtest.strategy.base import Strategy

class MyStrategy(Strategy):
    name = "my_strategy"
    description = "My custom strategy description"
    version = "1.0"
    author = "Your Name"
    params = {
        "period": {
            "default": 20,
            "min": 2,
            "max": 100,
            "type": "int",
            "label": "Lookback Period",
            "tooltip": "Number of bars to look back",
        },
    }
    
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        sma = candles["close"].rolling(self.period).mean()
        return (candles["close"] > sma).astype(int)
```

2. That's it. It's auto-discovered by the registry. No registration needed.

3. Test it:
```bash
PYTHONPATH=src python -m backtest run --strategy my_strategy --source synthetic --symbol DEMO --from 2024-01-01 --to 2024-12-31
```

## Adding a New Data Source

**Time:** ~30 minutes

1. Create `src/backtest/data/my_source.py`:
```python
import pandas as pd
from backtest.data.base import normalize_candles

class MySource:
    def get_candles(self, symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
        # Fetch your data here
        df = pd.DataFrame({
            "open": [...],
            "high": [...],
            "low": [...],
            "close": [...],
            "volume": [...],
        }, index=pd.DatetimeIndex([...]))
        
        return normalize_candles(df)
```

2. Register it in `runner.py`:
```python
def build_source(name: str, **kwargs):
    source_name = (name or "").lower()
    if source_name == "synthetic":
        return SyntheticSource()
    if source_name == "my_source":  # ADD THIS
        from backtest.data.my_source import MySource
        return MySource()
    # ...
```

3. Update `app.py` help text:
```python
parser.add_argument("--source", default="synthetic", help="synthetic | csv | mstock | my_source")
```

4. Run with:
```bash
PYTHONPATH=src python -m backtest.web.app --source my_source
```

## Adding a New API Endpoint

**Time:** ~20 minutes

1. Create or edit a blueprint in `src/backtest/api/`:
```python
from flask import Blueprint, jsonify

my_bp = Blueprint("my_api", __name__)

@my_bp.get("/api/my-endpoint")
def my_endpoint():
    return jsonify({"data": "value"}), 200
```

2. Register it in `app.py`:
```python
from backtest.api import my_bp
app.register_blueprint(my_bp)
```

3. Access at `http://localhost:5000/api/my-endpoint`

## Adding a New Database Table

**Time:** ~25 minutes

### Option A: SQLAlchemy (recommended)
1. Add model to `src/backtest/db/models.py`:
```python
class MyTable(Base):
    __tablename__ = "my_table"
    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(String(64), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
```

2. Create migration:
```bash
cd db && alembic revision --autogenerate -m "add my_table"
cd db && alembic upgrade head
```

### Option B: Raw SQL
1. Run SQL via psql:
```bash
PGPASSWORD=postgres psql -U postgres -d forward_test -c "CREATE TABLE my_table (...);"
```

2. Access via `DatabaseManager`:
```python
from backtest.db import DatabaseManager
db = DatabaseManager.from_env()
rows = db.fetch_all("SELECT * FROM my_table")
```

## Adding a New Web UI Page

**Time:** ~45 minutes

1. Create template `src/backtest/web/templates/my_page.html`:
```html
{% extends "base.html" %}
{% block title %}My Page{% endblock %}
{% block content %}
<h1>My Page</h1>
<!-- Your HTML here -->
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='js/my_page.js') }}"></script>
{% endblock %}
```

2. Create JS `src/backtest/web/static/js/my_page.js`:
```javascript
// Your page logic here
```

3. Add route in `app.py`:
```python
@app.get("/my-page")
def my_page():
    return render_template("my_page.html", active="my-page")
```

4. Add nav link in `base.html`:
```html
<a href="/my-page" class="nav-link {% if active == 'my-page' %}active{% endif %}">My Page</a>
```

## Testing Checklist

After adding any feature, verify:
- [ ] `PYTHONPATH=src python -m backtest.web.app --source synthetic` starts without errors
- [ ] New feature works via Web UI
- [ ] New feature works via CLI (if applicable)
- [ ] Existing backtests still work (no regression)
- [ ] API endpoint returns correct JSON (if applicable)
