"""Optional Google Cloud Storage input/output, separate short strategy namespace."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

def download_inputs(root):
    bucket=os.getenv('SHORT_DATA_BUCKET','').strip()
    if not bucket:
        return
    from google.cloud import storage
    client=storage.Client()
    b=client.bucket(bucket)
    day=datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
    keys={
        'universe.csv':'short/config/universe.csv',
        'baseline_5m.csv':'short/config/baseline_5m.csv',
        'eligibility.csv':f'short/eligibility/{day}.csv',
    }
    for name,key in keys.items():
        blob=b.blob(key)
        if not blob.exists(client):
            raise RuntimeError(f'Missing required gs://{bucket}/{key}')
        blob.download_to_filename(str(root/name))

def upload_results(root):
    bucket=os.getenv('SHORT_DATA_BUCKET','').strip()
    if not bucket:return
    from google.cloud import storage
    client=storage.Client()
    b=client.bucket(bucket)
    day=datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
    for name in ('paper_trades.csv',):
        path=root/name
        if path.exists():b.blob(f'short/results/{day}/{name}').upload_from_filename(str(path))
