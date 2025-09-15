#!/usr/bin/env python3
"""
Test script for 3D Warehouse collection loading
"""
import urllib.request
import json
import sys

def test_collection_api(collection_id):
    """Test various API endpoints for loading a collection"""
    print(f"Testing collection: {collection_id}")

    # Test URLs
    test_urls = [
        f"https://embed-3dwarehouse.sketchup.com/warehouse/v1.0/entities?fq=collectionId=={collection_id}&contentType=3dw&showBinaryAttributes=true&showBinaryMetadata=true&showAttributes=true&show=all&recordEvent=false",
        f"https://embed-3dwarehouse.sketchup.com/warehouse/v1.0/entities?fq=collectionId%3D%3D{collection_id}&contentType=3dw&showBinaryAttributes=true&showBinaryMetadata=true&showAttributes=true&show=all&recordEvent=false",
        f"https://embed-3dwarehouse.sketchup.com/warehouse/v1.0/entities?collectionId={collection_id}&contentType=3dw&showBinaryAttributes=true&showBinaryMetadata=true&showAttributes=true&show=all&recordEvent=false",
        f"https://embed-3dwarehouse.sketchup.com/warehouse/v1.0/collections/{collection_id}/entities?contentType=3dw&showBinaryAttributes=true&showBinaryMetadata=true&showAttributes=true&show=all",
        f"https://3dwarehouse.sketchup.com/warehouse/v1.0/collections/{collection_id}/entities?contentType=3dw&show=all"
    ]

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://3dwarehouse.sketchup.com/',
    }

    for i, url in enumerate(test_urls):
        print(f"\nTest {i+1}: {url}")
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode('utf-8', errors='replace')
                print(f"Status: {resp.status}")
                print(f"Content length: {len(content)}")

                if len(content.strip()) == 0:
                    print("Empty response")
                    continue

                try:
                    data = json.loads(content)
                    print(f"JSON parsed successfully. Type: {type(data)}")
                    if isinstance(data, dict):
                        print(f"Keys: {list(data.keys())}")
                        for key in ['entries', 'entities', 'items', 'results']:
                            if key in data and isinstance(data[key], list):
                                print(f"Found {len(data[key])} items in '{key}'")
                                if data[key]:
                                    print(f"First item keys: {list(data[key][0].keys()) if isinstance(data[key][0], dict) else 'Not a dict'}")
                                break
                    elif isinstance(data, list):
                        print(f"List with {len(data)} items")
                        if data:
                            print(f"First item keys: {list(data[0].keys()) if isinstance(data[0], dict) else 'Not a dict'}")
                    else:
                        print(f"Unexpected data type: {type(data)}")

                except json.JSONDecodeError as e:
                    print(f"JSON decode error: {e}")
                    print(f"First 500 chars: {content[:500]}")

        except urllib.error.HTTPError as e:
            print(f"HTTP Error: {e.code} - {e.reason}")
            if e.code == 400:
                try:
                    error_content = e.read().decode('utf-8', errors='replace')
                    print(f"Error details: {error_content}")
                except:
                    print("Could not read error response")
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}")

    # Test if collection page exists
    print("\nTesting collection page existence...")
    try:
        collection_url = f"https://3dwarehouse.sketchup.com/collection/{collection_id}"
        req = urllib.request.Request(collection_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Collection page status: {resp.status}")
            if resp.status == 200:
                content = resp.read().decode('utf-8', errors='replace')
                if 'collection' in content.lower():
                    print("Collection page contains 'collection' - likely exists")
                else:
                    print("Collection page exists but may be empty")
    except Exception as e:
        print(f"Collection page error: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        collection_id = sys.argv[1]
    else:
        collection_id = "226feeecf3660eff676bdfa79a976821"  # The failing collection

    test_collection_api(collection_id)