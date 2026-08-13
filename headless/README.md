# Headless / protocol edition

Compact signup runner for servers. Copy `config.example.json` to `config.json` (or run `python setup_config.py`), fill in your own proxy and OAuth values, then:

```bash
pip install -r requirements.txt
patchright install chromium
python main.py
```

Full documentation lives in the [repository root README](../README.md).
