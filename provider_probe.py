import os, json, requests
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
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)

load_env()

timeout = 20
results = []

def short_msg(resp=None, err=None):
    if err is not None:
        return str(err).splitlines()[0][:120]
    if resp is None:
        return 'no response'
    try:
        data = resp.json()
        if isinstance(data, dict):
            for key in ('error','message'):
                if key in data:
                    val = data[key]
                    if isinstance(val, dict):
                        msg = val.get('message') or json.dumps(val)
                    else:
                        msg = str(val)
                    return msg.splitlines()[0][:120]
            if 'candidates' in data:
                return 'ok'
            if 'choices' in data:
                return 'ok'
        return (resp.text or 'ok').replace('\n',' ')[:120]
    except Exception:
        return (resp.text or 'ok').replace('\n',' ')[:120]

# Gemini
status = 'NO_KEY'
msg = 'missing GEMINI_API_KEY'
key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
if key:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}"
    body = {"contents":[{"parts":[{"text":"ping"}]}]}
    try:
        r = requests.post(url, json=body, timeout=timeout)
        status = str(r.status_code)
        msg = short_msg(r)
    except Exception as e:
        status = 'ERR'
        msg = short_msg(err=e)
print(f"Gemini | {status} | {msg}")
results.append(('Gemini', status, msg))

# Groq
status = 'NO_KEY'
msg = 'missing GROQ_API_KEY'
key = os.getenv('GROQ_API_KEY')
if key:
    url = 'https://api.groq.com/openai/v1/chat/completions'
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    body = {"model":"llama-3.1-8b-instant","messages":[{"role":"user","content":"ping"}],"max_tokens":4}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
        status = str(r.status_code)
        msg = short_msg(r)
    except Exception as e:
        status = 'ERR'
        msg = short_msg(err=e)
print(f"Groq | {status} | {msg}")
results.append(('Groq', status, msg))

# OpenAI
status = 'NO_KEY'
msg = 'missing OPENAI_API_KEY'
key = os.getenv('OPENAI_API_KEY')
if key:
    url = 'https://api.openai.com/v1/chat/completions'
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    body = {"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}],"max_tokens":4}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
        status = str(r.status_code)
        msg = short_msg(r)
    except Exception as e:
        status = 'ERR'
        msg = short_msg(err=e)
print(f"OpenAI | {status} | {msg}")
results.append(('OpenAI', status, msg))

usable = [name for name, st, _ in results if st == '200']
if usable:
    rec = usable[0] if len(usable)==1 else ', '.join(usable)
    print(f"Recommendation: {rec} appears usable now.")
else:
    print("Recommendation: no provider appears usable now.")
