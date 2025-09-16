import urllib.request, json

query = 'chair'
from urllib.parse import quote
sort_param = 'popularity desc'.replace(' ', '%20')
base = ('https://embed-3dwarehouse.sketchup.com/warehouse/v1.0/entities'
    f'?sortBy={sort_param}&personalizeSearch=false'
    '&contentType=3dw&showBinaryAttributes=true&showBinaryMetadata=true&showAttributes=true'
    '&show=all&recordEvent=false&fq=binaryNames%3Dexists%3Dtrue')
api_url = f"{base}&q={quote(query)}&offset=0"
print('Testing URL:', api_url)
headers = {
    'User-Agent': 'Mozilla/5.0',
    'Accept': 'application/json',
    'Referer': 'https://3dwarehouse.sketchup.com/',
}
req = urllib.request.Request(api_url, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
        print('Status:', resp.status)
        print('Length:', len(raw))
        try:
            data = json.loads(raw)
            print('Top keys:', list(data.keys()) if isinstance(data, dict) else type(data))
        except Exception as e:
            print('JSON parse failed:', e)
except Exception as e:
    print('Request failed:', type(e).__name__, e)
    if hasattr(e, 'code'):
        try:
            body = e.read().decode('utf-8', errors='replace')
            print('Error body:\n', body[:1000])
        except Exception:
            pass
