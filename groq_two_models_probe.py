import os, requests, json
from pathlib import Path

def load_env(path='.env'):
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()
key = os.getenv('GROQ_API_KEY')
if not key:
    print('llama-3.3-70b-versatile | NO_KEY | missing GROQ_API_KEY')
    print('llama-3.1-8b-instant | NO_KEY | missing GROQ_API_KEY')
    print('Summary: no model works (missing key).')
    raise SystemExit(0)

url = 'https://api.groq.com/openai/v1/chat/completions'
headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
models = ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant']
results = []

for m in models:
    body = {
        'model': m,
        'messages': [{'role': 'user', 'content': 'ping'}],
        'max_tokens': 8,
        'temperature': 0,
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=20)
        status = str(r.status_code)
        msg = ''
        try:
            data = r.json()
            if isinstance(data, dict):
                if 'error' in data:
                    err = data['error']
                    msg = (err.get('message') if isinstance(err, dict) else str(err)) or ''
                elif 'choices' in data:
                    msg = 'ok'
                elif 'message' in data:
                    msg = str(data['message'])
        except Exception:
            msg = ''
        if not msg:
            msg = (r.text or 'ok').replace('\n', ' ')[:120]
    except Exception as e:
        status = 'ERR'
        msg = str(e).splitlines()[0][:120]

    print(f'{m} | {status} | {msg[:120]}')
    results.append((m, status))

working = [m for m,s in results if s == '200']
if working:
    print('Summary: works -> ' + ', '.join(working))
else:
    print('Summary: no model works.')
