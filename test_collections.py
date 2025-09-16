import urllib.request, json
headers={'User-Agent':'Mozilla/5.0','Accept':'application/json','Referer':'https://3dwarehouse.sketchup.com/'}
collection_id='226feeecf3660eff676bdfa79a976821'
urls=[
 f"https://3dwarehouse.sketchup.com/warehouse/v1.0/entities?fq=parentIds=={collection_id}&contentType=3dw&show=all&showBinaryMetadata=true&showAttributes=true",
 f"https://embed-3dwarehouse.sketchup.com/warehouse/v1.0/entities?collectionId={collection_id}&contentType=3dw&showBinaryAttributes=true&showBinaryMetadata=true&showAttributes=true&show=all&recordEvent=false",
]
for u in urls:
    print('\nTesting:',u)
    try:
        req=urllib.request.Request(u, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw=r.read().decode('utf-8',errors='replace')
            print('Status', r.status, 'len', len(raw))
            try:
                d=json.loads(raw)
                print('Top keys:', list(d.keys()) if isinstance(d, dict) else type(d))
            except Exception as e:
                print('JSON failed',e)
    except Exception as e:
        print('Failed', type(e).__name__, e)
