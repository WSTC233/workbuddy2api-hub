import urllib.request
import json
import urllib.error

payload = json.dumps({
    'model': 'deepseek-v4.1-flash',
    'messages': [{'role': 'user', 'content': 'hi'}],
    'stream': False
}).encode()

req = urllib.request.Request(
    'http://127.0.0.1:8787/v1/chat/completions',
    data=payload,
    headers={'Content-Type': 'application/json'}
)

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print('SUCCESS')
        print(resp.read().decode()[:200])
except urllib.error.HTTPError as e:
    print('Status:', e.code)
    body = e.read().decode()
    print('Body:', body)
    try:
        data = json.loads(body)
        print()
        print('retry_after:', data.get('retry_after', 'N/A'))
        print('message:', data.get('message', 'N/A'))
        print('code:', data.get('code', 'N/A'))
        print('type:', data.get('type', 'N/A'))
    except Exception as ex:
        print('Parse error:', ex)
