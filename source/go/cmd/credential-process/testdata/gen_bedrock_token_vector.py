"""Regenerate botocoreVectorSignature in bedrock_token_test.go with botocore's SigV4QueryAuth.

This is the signing path aws-bedrock-token-generator uses (POST presign of Action=CallWithBearerToken
to bedrock.amazonaws.com, 12-hour expiry, "&Version=1", base64, "bedrock-api-key-" prefix).
Run: python3 testdata/gen_bedrock_token_vector.py  (needs botocore)
"""

import datetime
from unittest import mock

import botocore.auth
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

AT = datetime.datetime(2026, 10, 27, 10, 0, 0)
CREDS = Credentials(
    "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "IQoJb3JpZ2luX2VjEXAMPLE/token+with=chars"
)

with mock.patch.object(botocore.auth, "get_current_datetime", return_value=AT):
    req = AWSRequest(
        method="POST",
        url="https://bedrock.amazonaws.com/",
        headers={"host": "bedrock.amazonaws.com"},
        params={"Action": "CallWithBearerToken"},
    )
    SigV4QueryAuth(CREDS, "bedrock", "us-east-1", expires=43200).add_auth(req)
url = req.url.replace("https://", "") + "&Version=1"
print(dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))["X-Amz-Signature"])
