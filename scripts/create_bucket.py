"""Creates Attendee's storage bucket in the internal SeaweedFS S3 endpoint (idempotent).

Runs inside the Attendee image (which ships boto3) via `docker compose run attendee-storage-init`.
"""

import os
import sys
import time

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

bucket = os.environ["AWS_RECORDING_STORAGE_BUCKET_NAME"]
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["AWS_ENDPOINT_URL"],
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
)

for attempt in range(30):
    try:
        existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
        if bucket not in existing:
            s3.create_bucket(Bucket=bucket)
            print(f"created bucket {bucket}")
        else:
            print(f"bucket {bucket} already exists")
        sys.exit(0)
    except (EndpointConnectionError, ClientError) as exc:
        print(f"storage not ready yet ({exc}); retrying…")
        time.sleep(2)

print("could not reach the storage endpoint", file=sys.stderr)
sys.exit(1)
